"""생성기 상태·인입·계열·생성 지역·설정·주입 대상 API."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import raw_parquet
from ..control import KINDS, T_HISTORY, get_config, list_faults, set_config
from ..generator import Groups, diurnal_factor, set_curve
from ..pemdb1 import BLOCKABLE_SERIES, CPLANE_SERIES
from .common import _cells, _cfg, _connect, _cplane_root, _enb_names, _pem_cfg


router = APIRouter()



# ── API ─────────────────────────────────────────────────────────────────────



@router.get("/api/status")
def api_status():
    conn = _connect()
    try:
        # 생성이 전진하는지는 **원시 C-Plane 으로 본다.** 구 `kpi_cell_min`
        # 은 아무도 쓰지 않아 영원히 같은 값을 돌려줬다.
        #
        # **원시가 MySQL 을 떠났다**(2026-10-14) — `SELECT MAX(EVENT_TIME)
        # FROM GP_RAW_LTE_CALL_KPI` 자리가 디렉터리 훑기로 바뀌었다. 전에
        # 개명(`XDR_LTE_CALL_KPI` → `GP_RAW_*`) 때 이 한 줄이 남아서 상태
        # 바가 "마지막 적재 슬롯 —"으로 떴다 — 화면에는 에러가 아니라 **빈
        # 값**으로 보여서 원인을 짚기 어려웠다. 그래서 이름·경로를 여기
        # 박지 않고 설정에서 받는다(`_cplane_root`).
        last_slot = raw_parquet.last_minute(_cplane_root(), CPLANE_SERIES)
        faults = list_faults(conn, limit=100)
    finally:
        conn.close()
    return {
        "last_slot": last_slot,
        "active": [f for f in faults if f["status"] == "active"],
        "pending": [f for f in faults if f["status"] == "pending"],
        "recent_done": [f for f in faults if f["status"] == "done"][:10],
        "kinds": list(KINDS),
        # 상태바가 "지금 어떤 설정으로 돌고 있나"를 말할 수 있게 함께 내린다
        # (2026-10-15). 전에는 마지막 슬롯과 활성 장애 수뿐이어서, 생성이
        # 꺼져 있거나 30% 로 줄어 있어도 **화면만 보고는 알 수 없었다** —
        # 슬롯이 안 늘면 "데몬이 죽었나"부터 의심하게 된다.
        **_gen_state(),
    }


def _gen_state() -> dict:
    """지금 생성이 어떤 상태인가 — 가동 여부 · 비율 · 실제 분당 건수.

    `CM_DATAGEN_CONFIG` 가 정본이고 없으면 설정 기본값이다(데몬과 같은 규칙 —
    `cli.cmd_run` 이 매 분 같은 자리를 읽는다). 여기서 다르게 읽으면 화면과
    데몬이 서로 다른 값을 말한다.
    """
    pem_rates = _pem_cfg().get("rates") or {}
    base = {s: int(pem_rates.get(s, 0) or 0)
            for s in ("lte_call", "transport", "dns", "http")}
    conn = _connect()
    try:
        pct = float(get_config(conn, "volume_pct",
                               str(pem_rates.get("volume_pct", 100))))
        on = str(get_config(conn, "enabled", "true")).lower() != "false"
        # 상태등이 쓴다 — 끊긴 계열이 있으면 노란불이다. **1초 폴링이 이 함수를
        # 부르므로** 질의를 늘리지 않고 같은 KV 테이블에서 두 키를 더 읽는다.
        blocked = [x for x in str(get_config(conn, "blocked", "") or "").split(",")
                   if x.strip()]
        di_d, vo_d = _clock_defaults()
        diurnal = str(get_config(conn, "diurnal", di_d)).lower() != "false"
        vo = float(get_config(conn, "virtual_offset_sec", vo_d) or 0)
    finally:
        conn.close()
    return {
        "gen_enabled": on,
        "blocked": blocked,
        "diurnal": diurnal,
        # 가상 시각 — 곡선 켬일 때만 뜻이 있다(끔이면 시간대를 안 본다)
        "virtual_offset_sec": vo if diurnal else 0.0,
        "virtual_now": _virtual(vo) if diurnal else None,
        "volume_pct": pct,
        "base_rates": base,
        "rates": {s: max(0, round(v * pct / 100.0)) for s, v in base.items()},
        # 마지막 틱이 **언제** 돌았나. 슬롯 시각만으로는 "1분 전"인지 "한
        # 시간째 멈춰 있는지" 구분이 안 된다.
        "now": datetime.now().replace(microsecond=0).isoformat(sep=" "),
    }


def _window(date: str | None, span_sql: str, params: list):
    """조회 범위를 만든다 — 날짜가 주어지면 그 하루, 아니면 지금 기준 상대 창.

    과거 날짜는 더 변하지 않으므로 하루를 통째로 고정해 본다. 날짜 없이 쓰는
    쪽(기본)은 지금까지와 같은 "흐르는 창"이다.
    """
    if date:
        return ("slot_ts >= %s AND slot_ts < DATE_ADD(%s, INTERVAL 1 DAY)",
                [date, date])
    return (span_sql, params)


@router.get("/api/ingest")
def api_ingest(minutes: int = 60, date: str | None = None):
    """인입 현황 — 계열별 생성량. **장애 여부는 보지 않는다.**

    이 화면은 "생성이 고르게 돌고 있나"만 본다. 품질 판정은 관제 화면(8008)의
    일이고, 여기서 또 보여주면 두 화면이 같은 사실을 다르게 말할 위험이 있다.

    출처는 `CM_DATAGEN_HISTORY` 다. 구 구현은 `file_ledger` 로 계열을 쪼갰지만
    그 테이블은 자체 창고 소유였고 상용 스키마에 대응이 없다
    (`graphio/store/tables.py` 의 `LEGACY_TABLE_MAP["file_ledger"] = None`).
    **계열은 `lte_call` 하나다** — Transport·DNS·HTTP XDR 은 운영 DB 가 적재하지
    않는다(영구 미연동, `pemdb1.UNSUPPORTED`). 없는 계열을 0건으로 그려 넣으면
    "그 계열은 돌고 있는데 양이 없다"로 읽히므로 아예 내리지 않는다.
    """
    conn = _connect()
    try:
        with conn.cursor() as cur:
            where, args = _window(
                date, "EVENT_TIME >= DATE_SUB(NOW(), INTERVAL %s MINUTE)", [minutes])
            # **계열별 건수는 `SERIES_STAT` 에 이미 있다**(2026-10-15).
            # 전에는 `RECORD_CNT`(= lte_call) 하나만 보여 주고 "계열은
            # lte_call 뿐"이라고 적어 두었는데, 그건 U-Plane 이 MySQL 에
            # 적재되던 시절의 사실이다. 지금은 Parquet 로 나가면서 건수·바이트를
            # 이 json 컬럼에 함께 남긴다(`pemdb1.series_stat`) — DB 에 있는
            # 값을 안 쓰고 "그 계열은 없다"고 말하고 있었다.
            cur.execute(
                f"SELECT EVENT_TIME, RECORD_CNT, SERIES_STAT FROM {T_HISTORY} "
                f"WHERE {where}", args)
            hist = cur.fetchall()
            agg: dict[str, dict] = {}
            for _slot, cnt, stat in hist:
                blob = stat if isinstance(stat, dict) else json.loads(stat or "{}")
                if not blob:                      # 구 행 — SERIES_STAT 이 없다
                    blob = {"lte_call": {"n": int(cnt or 0)}}
                for series, v in blob.items():
                    a = agg.setdefault(series, {"series": series, "slots": 0,
                                                "recs": 0, "bytes": 0})
                    a["slots"] += 1
                    a["recs"] += int((v or {}).get("n", 0) or 0)
                    # **바이트가 없는 계열이 있다.** C-Plane 은 시그널링이라
                    # 페이로드가 없고 `series_stat` 이 키 자체를 두지 않는다 —
                    # 0 으로 적으면 "데이터를 안 나른다"가 아니라 "0바이트를
                    # 날랐다"로 읽힌다(불변규칙 17).
                    if "bytes" in (v or {}):
                        a["bytes"] += int(v["bytes"] or 0)
                    else:
                        a["bytes"] = None
            # 표시 순서를 고정한다 — 계열이 json 키 순서로 흔들리면 화면이
            # 갱신될 때마다 줄이 뒤바뀐다.
            order = ["lte_call", "transport", "dns", "http"]
            rows = [agg[k] for k in order if k in agg] + \
                   [v for k, v in sorted(agg.items()) if k not in order]
            rows = [r for r in rows if r["slots"]]      # 빈 창에는 계열을 만들지 않는다
            cur.execute(
                "SELECT COUNT(*) slots, COALESCE(SUM(RECORD_CNT),0) recs "
                f"FROM {T_HISTORY} WHERE {where}", args)
            tot_slots, tot_recs = cur.fetchone()
            # 날짜별 슬롯 수 — 상단에 "오늘 N분 / 누적 M분"
            cur.execute(
                f"SELECT DATE(EVENT_TIME) d, COUNT(*) slots, SUM(RECORD_CNT) recs "
                f"FROM {T_HISTORY} GROUP BY 1 ORDER BY 1 DESC LIMIT 7")
            days = [dict(zip([d[0] for d in cur.description], r)) for r in cur.fetchall()]
            # **오늘 계열별 합계** (2026-11-04) — 상단 바 "오늘 생성"용. `by_day.recs` 는
            # `RECORD_CNT`(= lte_call)뿐이라 U-Plane 이 빠진다. 하루 최대 1,440행이다
            cur.execute(f"SELECT SERIES_STAT FROM {T_HISTORY} "
                        "WHERE EVENT_TIME >= CURDATE() AND EVENT_TIME < CURDATE() + INTERVAL 1 DAY")
            today: dict[str, int] = {}
            for (stat,) in cur.fetchall():
                blob = stat if isinstance(stat, dict) else json.loads(stat or "{}")
                for series, v in (blob or {}).items():
                    today[series] = today.get(series, 0) + int((v or {}).get("n", 0) or 0)
            today_series = ([{"series": k, "recs": today[k]} for k in order if k in today]
                            + [{"series": k, "recs": n} for k, n in sorted(today.items())
                               if k not in order])
    finally:
        conn.close()
    for r in rows:
        r["per_min"] = round(r["recs"] / r["slots"], 1) if r["slots"] else 0
        # **바이트는 내리되 화면에 쓰지 않는다** (2026-10-15).
        # `SERIES_STAT.bytes` 의 원천(`Traffic_Delta_*_PayloadSize`)이
        # 비정상이다 — transport 한 분 1,988행에 평균 up 239MB · dn 63MB ·
        # 최대 dn 3.5GB 로, 세션 하나가 1분에 수백 MB 를 나른 셈이 된다.
        # 생성기 분포 문제라 UI 에서 고칠 자리가 아니고, 틀린 값을 그리면
        # "데이터가 이만큼 흐른다"로 읽힌다. 고친 뒤 화면에 되살린다.
        r["mb_per_min"] = (round(r["bytes"] / r["slots"] / 2**20, 1)
                           if r["slots"] and r["bytes"] is not None else None)
    return {
        "minutes": minutes, "date": date, "series": rows,
        "total": {"slots": tot_slots or 0, "records": int(tot_recs or 0),
                  "per_min": round((tot_recs or 0) / tot_slots, 1) if tot_slots else 0},
        "by_day": days,
        "today_series": today_series,
    }


@router.get("/api/series")
def api_series(hours: int = 3, date: str | None = None):
    """시계열 — 분당 생성량(계열별)과 그 분에 활성이던 fault.

    한 번의 조회로 그래프에 필요한 것을 다 내린다. 5초 폴링이 이 질의를 매번
    돌면 무거우므로 화면은 이쪽을 더 긴 주기로 부른다.
    """
    conn = _connect()
    try:
        with conn.cursor() as cur:
            where, args = _window(
                date, "EVENT_TIME >= DATE_SUB(NOW(), INTERVAL %s HOUR)", [hours])
            # `file_ledger` 를 쓰지 않는다 — 상용 스키마에 대응이 없다.
            # 계열은 `lte_call` 하나이므로 `CM_DATAGEN_HISTORY` 로 충분하다.
            cur.execute(
                f"SELECT EVENT_TIME, RECORD_CNT, FAULT_IDS, SERIES_STAT "
                f"FROM {T_HISTORY} WHERE {where} ORDER BY EVENT_TIME", args)
            hist = cur.fetchall()
    finally:
        conn.close()

    by_slot: dict = {}
    points = []
    for slot_ts, records, tags, stat in hist:
        k = slot_ts.isoformat()
        # `SERIES_STAT` 는 json 컬럼이다. 커넥터 설정에 따라 `str`·`bytes`·
        # 이미 파싱된 `dict` 중 하나로 온다 — 바로 `json.loads` 를 부르면
        # 마지막 경우에 `TypeError` 가 난다(graphio `rca/evidence.json_col`
        # 과 같은 함정).
        if isinstance(stat, (bytes, bytearray)):
            stat = stat.decode("utf-8", "replace")
        if isinstance(stat, str):
            try:
                stat = json.loads(stat)
            except ValueError:
                stat = None
        if not isinstance(stat, dict):
            stat = None

        # **계열별 건수의 정본은 `SERIES_STAT` 다.** 없으면(구 행) `RECORD_CNT`
        # 로 `lte_call` 하나만 세운다 — 0 으로 채우지 않는다(불변규칙 17).
        series = ({k2: int((v or {}).get("n") or 0) for k2, v in stat.items()}
                  if stat else {"lte_call": int(records or 0)})
        by_slot[k] = series
        point = {
            "ts": k,
            "series": series,
            "total": sum(series.values()),
            "faults": (tags.split(",") if tags else []),
        }
        if stat:
            # 바이트가 있는 계열만 싣는다. C-Plane 은 페이로드가 없어 키가
            # 아예 없고, 화면은 그 계열에 크기를 표시하지 않는다.
            b = {k2: int(v["bytes"]) for k2, v in stat.items()
                 if isinstance(v, dict) and v.get("bytes") is not None}
            if b:
                point["bytes"] = b
                point["total_bytes"] = sum(b.values())
        points.append(point)
    return {"hours": hours, "date": date, "points": points,
            "series_names": sorted({s for v in by_slot.values() for s in v})}


@router.get("/api/region")
def api_region():
    """지역축 — **설정 파일 값 · UI 저장값 · 어긋난 자리**를 함께 내린다.

    셋을 다 보여 주는 이유: 저장값이 실제로 적용되는 것은 **재시작 뒤**라
    "지금 무엇으로 돌고 있나" 와 "다음에 무엇이 될까" 가 다를 수 있다.
    하나만 보여 주면 화면이 둘 중 하나에 대해 거짓말을 한다.
    """
    from .. import region
    cfg = _cfg()
    file_loc = region.resolve(cfg)
    conn = _connect()
    try:
        saved = (get_config(conn, "locale") or "").strip().lower()
    finally:
        conn.close()
    # DB 에 이상한 값이 있으면 **없는 것으로 본다** — 데몬도 같은 판단을
    # 한다(`cli.apply_region_override`). 화면과 데몬이 갈리면 안 된다.
    if saved and saved not in region.LOCALES:
        saved = ""
    pending = saved or file_loc
    # 저장값을 적용했을 때의 어긋남을 미리 계산한다 — 저장 **전에** 보여
    # 주려는 것이다. 누른 뒤에 알려 주면 되돌리는 수밖에 없다.
    probe = json.loads(json.dumps({k: v for k, v in cfg.items()
                                   if k in ("locale", "plmn", "pemdb1")}))
    region.reapply(probe, pending)
    return {
        "locales": region.names(),
        # 설정 파일(`datagen.yaml`)의 값
        "file": file_loc,
        # UI 가 저장해 둔 값. 빈 문자열이면 저장된 적이 없다
        "saved": saved,
        # 재시작하면 이것이 된다
        "pending": pending,
        # **지금 이 프로세스가 들고 있는 값** — 데몬과 다를 수 있다
        "plmn": dict(cfg.get("plmn") or {}),
        "pemdb1_locale": str((cfg.get("pemdb1") or {}).get("locale", "")),
        # `pending` 을 적용했을 때 남는 어긋남. 사람이 yaml 에 적은 칸은
        # 지역을 바꿔도 따라오지 않는다(`region` 규칙 ②)
        "conflicts": region.conflicts(probe),
        "restart_required": True,
    }


class RegionReq(BaseModel):
    #: `kr` | `jp`. 목록의 정본은 `region.LOCALES` 다
    locale: str


@router.post("/api/region")
def api_set_region(req: RegionReq):
    """지역을 저장한다. **지금 도는 생성에는 영향이 없다.**

    `volume_pct` 처럼 다음 틱부터 반영되지 않는다 — 데몬이 기동 시 한 번만
    읽는다(`cli.apply_region_override`). 운영 중에 바꾸지 않는다는 전제
    (사용자 확정 2026-10-28)이고, 바꿔도 되게 만들면 같은 분 Parquet 에
    두 나라 IMSI 가 섞인다.
    """
    from .. import region
    loc = str(req.locale or "").strip().lower()
    if loc not in region.LOCALES:
        raise HTTPException(
            400, f"모르는 지역이다: {req.locale!r} "
                 f"(가능: {', '.join(region.LOCALES)})")
    conn = _connect()
    try:
        set_config(conn, "locale", loc)
    finally:
        conn.close()
    return {"ok": True, "locale": loc, "restart_required": True}


@router.get("/api/settings")
def api_settings():
    """생성 설정 — CM_DATAGEN_CONFIG(DB) 가 우선, 없으면 datagen.yaml 기본값.

    데몬이 틱마다 그 테이블을 읽으므로 여기서 바꾸면 다음 분부터 반영된다
    (재기동 불필요). 장비 추가·삭제는 이 화면의 범위가 아니다 — 셀 재배정이
    필요해 설계가 따로 붙는다.
    """
    # **데몬과 같은 함수로 환산한다** — 두 곳에서 따로 계산하면 화면이
    # 데몬과 다른 보존 기간을 말한다. 지연 임포트는 이 모듈의 관례다
    # (`cli` 가 `ui` 를 부르므로 모듈 최상단에서는 순환이 된다).
    from ..cli import _retention_default
    cfg = _cfg()
    gen = dict(cfg.get("generate") or {})
    # **분당 호 수는 `pemdb1.rates` 가 정본이다.** 구 `rates:`(lte 2000 ·
    # transport/dns/http)는 자체 창고용이고, 상용 경로는 `lte_call` 하나를
    # 20,000/분으로 돌린다. 여기서 구 절을 보여 주면 운영자가 2000 을 저장해
    # 생성량이 10분의 1로 떨어진다 — 에러 없이 표본이 말라 탐지가 멈춘다.
    pem_rates = _pem_cfg().get("rates") or {}
    #: 100% 기준 — 설정의 절대값이다. **읽기 전용으로 보여 준다.**
    base = {s: int(pem_rates.get(s, 0) or 0)
            for s in ("lte_call", "transport", "dns", "http")}
    pct_default = float(pem_rates.get("volume_pct", 100))
    conn = _connect()
    try:
        over = {}
        for k in ("enabled", "retention_hours", "retention_on",
                  "volume_pct", "reassign", "blocked", "diurnal", "virtual_offset_sec"):
            v = get_config(conn, k)
            if v is not None:
                over[k] = v
        cells_list = _cells(conn)
    finally:
        conn.close()
    pct = float(over.get("volume_pct", pct_default))
    #: 실제 생성량 — 기준 × 비율. `PemdbGen.set_volume_pct` 와 **같은 식이어야
    #: 한다**(`round`). 다르면 화면이 데몬과 다른 수를 말한다.
    applied = {s: max(0, round(v * pct / 100.0)) for s, v in base.items()}
    nodes = {
        "SGW": len({c.sgw_id for c in cells_list}),
        "MME": len({c.mme_id for c in cells_list}),
        "PGW": len({c.pgw_id for c in cells_list}),
        # 재배정 드롭다운이 쓴다. **문자열로 내린다** — `'0015'` 의 0채움을
        # 숫자로 접으면 `15` 가 되어 생성기의 비교(문자열)와 어긋난다.
        "SGW_IDS": sorted({str(c.sgw_id) for c in cells_list}),
        "MME_IDS": sorted({str(c.mme_id) for c in cells_list}),
        "PGW_IDS": sorted({str(c.pgw_id) for c in cells_list}),
    }
    return {
        # **비율이 정본이다**(2026-10-14). 전에는 `rate.lte_call` 절대값을
        # 입력으로 받았는데 **데몬이 그 키를 읽지 않았다** — 기동 시 yaml 에서
        # 한 번 읽고 끝이라, 저장해도 생성량이 바뀌지 않으면서 화면은
        # "다음 틱부터 반영된다"고 말하고 있었다. 지금은 데몬이 매 분
        # `volume_pct` 를 다시 읽는다(`cli.cmd_run`).
        "volume_pct": pct,
        "reassign": json.loads(over.get("reassign") or "{}" ),
        "base_rates": base,        # 100% 기준 (읽기 전용)
        "rates": applied,          # 지금 실제 생성량
        "enabled": str(over.get("enabled", "true")).lower() != "false",
        # ── 보존 — **시간 단위 한 값이다**(2026-10-15) ──────────────────
        # 전에는 일 단위였고, 그 값은 **일 파티션에만** 걸렸다(원시는 코드
        # 상수 24시간). 화면에서 "보존 1일"을 보면서 원시가 24시간인 이유를
        # 알 길이 없었다. 지금은 두 입도가 같은 값을 쓴다
        # (`cli._purge_pemdb1`).
        #
        # 기본값 환산은 **데몬과 같은 함수**로 한다 — 두 곳에서 따로 계산하면
        # 화면이 데몬과 다른 보존 기간을 말한다.
        "retention_hours": int(over.get("retention_hours", _retention_default(gen))),
        # 정리를 아예 멈출 수 있다. **끄면 창고가 무한히 자란다** — 화면이
        # 그 사실을 적고, 데몬도 상태가 바뀔 때 로그에 남긴다.
        "retention_on": str(over.get("retention_on", "true")).lower() != "false",
        # ── 계열 차단 (2026-10-15) ───────────────────────────────────────
        # **끊을 수 있는 목록의 정본은 생성기다**(`pemdb1.BLOCKABLE_SERIES`).
        # 화면이 자기 목록을 들면 계열이 늘 때 한쪽만 고쳐지고, 그때 "체크할
        # 자리가 없다"가 아니라 **체크해도 안 끊긴다**가 된다.
        "blockable": list(BLOCKABLE_SERIES),
        "blocked": [x for x in str(over.get("blocked", "")).split(",") if x.strip()],
        # ── 시간대 곡선 ─────────────────────────────────────────────────
        # **원래부터 켜져 있었다**(`pemdb1.PemdbGen` 이 `diurnal=True` 로
        # 생성기를 만든다). 화면에 스위치가 없어서 "빠진 기능"으로 보였을 뿐이고,
        # 보이지 않는 이유는 진폭이다 — 호 수에 걸리는 가중이 0.25 라
        # 04시↔19시가 1.14배다(`generator.DIURNAL_WEIGHT` 머리말).
        "diurnal": str(over.get("diurnal", _clock_defaults()[0])).lower() != "false",
        # 가상 시각 — 곡선·그룹 비율만 옮긴다. 레코드 시각은 실제 시각(`Generator.curve_time`)
        "virtual_offset_sec": float(over.get("virtual_offset_sec", _clock_defaults()[1]) or 0),
        #: 화면이 곡선을 그릴 수 있게 배율표를 함께 내린다 — 숫자를 화면에
        #: 다시 적으면 두 벌이 되고, 곡선을 고칠 때 화면만 옛 모양을 그린다.
        "diurnal_curve": _curve_rows(),
        #: 시각별 특징 문장의 재료 — 그룹 비율은 생성기와 **같은 클래스**(`Groups`)로 계산한다
        "clock_bands": _clock_bands(),
        "topology": {"cells": len(cells_list), **nodes},
        "note": "장비 추가·삭제는 미지원 — 셀 재배정 설계가 선행되어야 한다",
    }


def _curve_rows() -> list[dict]:
    """시간대 곡선 24칸 — **설정 곡선(`pemdb1.clock.curve`)을 먼저 얹는다.** 데몬과 같은
    규칙이다(`cli` 가 기동 때 `set_curve`). 안 얹으면 yaml 을 고쳐도 화면만 코드 기본값을 그린다."""
    set_curve((_pem_cfg().get("clock") or {}).get("curve"))
    return [{"h": h,
             "call": round(diurnal_factor(h, "lte_call"), 3),
             "sess": round(diurnal_factor(h, "uplane_sessions"), 3),
             "byte": round(diurnal_factor(h, "transport"), 3)}
            for h in range(24)]


def _clock_defaults() -> tuple[str, str]:
    """시간대 곡선·가상 시각 기본값 — 설정 `pemdb1.clock`(UI 저장값이 없을 때). 데몬과 같은 규칙."""
    clk = _pem_cfg().get("clock") or {}
    return ("false" if clk.get("diurnal") is False else "true",
            str(60.0 * float(clk.get("virtual_offset_min", 0) or 0)))


_ROUTE_CELLS: dict = {}


def _route_cells(path: str) -> int | None:
    """노선 파일의 고유 셀 수(외선·내선 합집합). 못 읽으면 None — 화면이 그 줄을 뺀다."""
    if path not in _ROUTE_CELLS:
        import json as _json
        try:
            rows = _json.loads(Path(path).read_text(encoding="utf-8"))
            _ROUTE_CELLS[path] = len({k for r in rows for k in r.get("cells", [])}) or None
        except (OSError, ValueError):
            _ROUTE_CELLS[path] = None
    return _ROUTE_CELLS[path]


def _clock_bands() -> dict | None:
    """시간대 곡선 칸의 특징 문장 재료 (2026-11-04 사용자 요청).

    **비율을 화면이 계산하지 않는다.** 경계 보간(`blend_min`)·지하철 운행 시각까지
    생성기와 같은 `Groups.shares` 로 10분 간격 144칸을 내린다 — 화면이 `hourly` 를
    따로 풀면 보간 규칙이 두 벌이 되고, 고칠 때 화면만 옛 비율을 말한다.
    `hourly` 가 없으면(그룹 없이 `call_share` 하나) None 이고 화면은 비율 줄을 뺀다.
    """
    mb = _pem_cfg().get("mobility") or {}
    rows = mb.get("hourly") or []
    if not rows:
        return None
    try:
        g = Groups(rows, mb.get("subway_hours", [6, 24]), mb.get("blend_min", 60))
    except RuntimeError:
        return None
    day = datetime(2000, 1, 1)
    steps = []
    for k in range(144):
        sw, wk = g.shares(day + timedelta(minutes=10 * k))
        steps.append([round(sw, 3), round(wk, 3)])
    notes = {int(n.get("h", -1)): n for n in (mb.get("hourly_notes") or []) if isinstance(n, dict)}
    bands = []
    for r in sorted(rows):
        n = notes.get(int(r[0]), {})
        bands.append({"h0": r[0], "h1": r[1],
                      **{k: str(n.get(k, "")) for k in
                         ("name", "name_ja", "note", "note_ja", "warn", "warn_ja")}})
    return {"steps": steps, "bands": bands,
            "blend_min": int(mb.get("blend_min", 60)),
            "subway_hours": list(mb.get("subway_hours", [6, 24])),
            "walk_warmup_min": int(mb.get("walk_warmup_min", 0) or 0),
            "route_cells": _route_cells(str(mb.get("routes_file") or "")) if mb.get("routes_file") else None}


def _virtual(offset_sec: float) -> str | None:
    """가상 시각 HH:MM — 오프셋이 0 이면 None(지금 시각)."""
    if not offset_sec:
        return None
    return (datetime.now() + timedelta(seconds=offset_sec)).strftime("%H:%M")


class SettingsReq(BaseModel):
    #: 생성량 비율(%). 0~100. **이것이 정본이고 절대값은 유도값이다.**
    volume_pct: float | None = None
    #: 장비 재배정 — `{"SGW": {"0015": "0103"}}`. 빈 dict 면 해제.
    reassign: dict | None = None
    enabled: bool | None = None
    #: 보존 기간(시간). 일 파티션(`PM_*_1M`)은 하루 입도로만 떨어진다.
    retention_hours: int | None = None
    #: 보존 정리 자체의 on/off. `False` 면 파티션을 떨어뜨리지 않는다.
    retention_on: bool | None = None
    #: 끊을 계열. 빈 리스트면 해제. `pemdb1.BLOCKABLE_SERIES` 안의 값만.
    blocked: list[str] | None = None
    #: 시간대 곡선 on/off. 끄면 생성량은 피크(19시) 수준으로 평탄, 그룹·이동 없이 전체
    #: eNB 에 골고루(2026-11-04 사용자 결정 — `Generator.flat_mode`)
    diurnal: bool | None = None
    #: 가상 시각 "HH:MM" — 곡선·그룹 비율이 볼 시각. 빈 문자열이면 해제(지금 시각).
    #: **레코드 시각은 바꾸지 않는다** — 데몬은 오프셋(초)만 받는다(`virtual_offset_sec`)
    virtual_clock: str | None = None


@router.post("/api/settings")
def api_set_settings(req: SettingsReq):
    conn = _connect()
    try:
        if req.reassign is not None:
            # **검증은 모양만 한다.** 없는 노드 ID 를 주면 일치 셀이 0 개라
            # 아무 일도 일어나지 않고, 그 사실은 데몬 로그에 "0셀"로 남는다 —
            # 여기서 노드 목록을 다시 조회해 막으면 UI 와 생성기가 서로 다른
            # 토폴로지를 들고 판정하게 된다.
            bad = [k for k in req.reassign if str(k).upper()
                   not in ("MME", "SGW", "PGW")]
            if bad:
                raise HTTPException(400, f"장비 종류는 MME·SGW·PGW 다: {bad}")
            set_config(conn, "reassign",
                       json.dumps(req.reassign) if req.reassign else "")
        if req.volume_pct is not None:
            pct = float(req.volume_pct)
            if not 0 <= pct <= 100:
                raise HTTPException(400, f"생성량 비율은 0~100 이다: {pct}")
            # 0 은 받는다 — "만들지 않는다"는 뜻이고 그 구간은 **비어 있는 것이
            # 사실이다**(불변규칙 4). `enabled=false` 와 다르다: 그쪽은 틱
            # 자체를 건너뛰고 이쪽은 틱이 돌면서 0건을 만든다.
            set_config(conn, "volume_pct", f"{pct:g}")
        if req.enabled is not None:
            set_config(conn, "enabled", "true" if req.enabled else "false")
        if req.retention_hours is not None:
            h = int(req.retention_hours)
            # 1시간 미만은 받지 않는다 — 원시 파티션이 시간 입도라 0 을 주면
            # **방금 쓴 칸까지 떨어뜨린다**(상한 ≤ cutoff). 상한 2160 = 90일.
            if not 1 <= h <= 2160:
                raise HTTPException(400, f"보존 시간은 1~2160 이다: {h}")
            set_config(conn, "retention_hours", str(h))
        if req.retention_on is not None:
            set_config(conn, "retention_on", "true" if req.retention_on else "false")
        if req.blocked is not None:
            # **여기서는 목록을 검증한다.** 재배정(위)과 다른 이유: 저쪽은
            # 없는 노드를 주면 "0셀 일치"로 아무 일도 안 일어나지만, 이쪽은
            # 오타가 들어가면 `PemdbGen.set_blocked` 가 **틱에서** 예외를
            # 던져 데몬이 죽는다. UI 에서 막는 쪽이 싸다.
            bad = [x for x in req.blocked if x not in BLOCKABLE_SERIES]
            if bad:
                raise HTTPException(
                    400, f"끊을 수 없는 계열이다: {bad} "
                         f"(가능: {', '.join(BLOCKABLE_SERIES)})")
            set_config(conn, "blocked", ",".join(sorted(set(req.blocked))))
        if req.diurnal is not None:
            set_config(conn, "diurnal", "true" if req.diurnal else "false")
        if req.virtual_clock is not None:
            v = req.virtual_clock.strip()
            if not v:
                set_config(conn, "virtual_offset_sec", "0")
            else:
                try:
                    hh, mm = (int(x) for x in v.split(":"))
                    assert 0 <= hh < 24 and 0 <= mm < 60
                except (ValueError, AssertionError):
                    raise HTTPException(400, f"가상 시각은 HH:MM 이다: {v!r}")
                now = datetime.now()
                # 오늘 그 시각까지의 차(초)를 ±12시간으로 접는다 — 곡선은 시·분만 보므로
                # 날짜는 뜻이 없다. 지금부터 실제 시간과 같은 속도로 흐른다(오프셋 방식)
                d = ((hh * 3600 + mm * 60) - (now.hour * 3600 + now.minute * 60 + now.second)) % 86400
                if d > 43200:
                    d -= 86400
                set_config(conn, "virtual_offset_sec", str(int(d)))
    finally:
        conn.close()
    return {"ok": True}


@router.get("/api/targets")
def api_targets():
    """대상 후보 — 셀이 **실제로 걸린** 노드·시군구만.

    셀이 하나도 안 걸린 노드에 장애를 넣어 봐야 지표에 아무것도 안 나타난다.
    셀 수 내림차순이라 "효과가 잘 보이는" 대상이 위에 온다.

    구 구현은 `cell_master` 를 조회했지만 그 테이블은 자체 창고 소유이고 상용에
    없다. 더 중요한 것은 **셀→노드 배정이 DB 컬럼이 아니라는 점**이다 —
    `pemdb1.load_cells` 가 eNB 해시로 정한다(`CL_CELL_MAP` 에 셀→노드 컬럼이
    없다). 그래서 어떤 테이블을 조회해도 알 수 없고, 생성기와 **같은 함수**를
    불러야 같은 답이 나온다. 다른 값을 쓰면 주입 대상이 생성 대상과 어긋나
    장애가 아무 셀에도 걸리지 않는다.
    """
    conn = _connect()
    try:
        cells_list = _cells(conn)
        names = _enb_names(conn)
    finally:
        conn.close()

    # **장비 단위로 센다**(2026-11-03 사람 지시) — 노드 아래 eNB 수. 셀 수는
    # 화면이 셀 군집 시나리오에서만 쓴다
    def tally(attr: str) -> list[dict]:
        cnt: dict[str, int] = {}
        enbs: dict[str, set] = {}
        for c in cells_list:
            v = getattr(c, attr)
            if v:
                cnt[v] = cnt.get(v, 0) + 1
                enbs.setdefault(v, set()).add(c.enb_id)
        return [{"id": k, "cells": n, "enbs": len(enbs[k]),
                 **({"name": names.get(str(k))} if attr == "enb_id" else {})}
                for k, n in sorted(cnt.items(), key=lambda kv: (-kv[1], kv[0]))]

    sgg: dict[str, int] = {}
    for c in cells_list:
        if c.adm_sgg_cd:
            sgg[c.adm_sgg_cd] = sgg.get(c.adm_sgg_cd, 0) + 1
    return {
        "sgw": tally("sgw_id"), "mme": tally("mme_id"), "pgw": tally("pgw_id"),
        # eNB — `enb_s1_outage`(무호) 전용 축이다. **상위 30개만 내린다**:
        # 로컬은 60개지만 현장 마스터는 22,722개여서 전량을 내리면 select 가
        # 쓸 수 없고 응답도 무겁다. 셀 수가 많은 eNB 라야 D4 의 베이스라인
        # 조건(직전 60분 중앙값 ≥ 20호)을 넘긴다.
        "enb": tally("enb_id")[:30],
        # 시군구 이름은 추출 단계에서 한글이 깨져 비어 있다 — 지어내지 않고
        # 코드를 그대로 보여 준다(불변규칙 17).
        "sgg": [{"code": k, "name": "", "cells": n}
                for k, n in sorted(sgg.items(), key=lambda kv: (-kv[1], kv[0]))],
    }
