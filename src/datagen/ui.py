"""장애 주입 제어 UI — 포트 8009, FastAPI + 단일 내장 HTML.

관제 대시보드(graphio, 8008)와 물리적으로 분리된 "뒤 화면"이다. 운영자는 여기서
장애를 주입하고, 생성 데몬이 datagen_* 제어 테이블을 1분마다 읽어 반영한다.
UI 는 **쓰기만** 한다 — 상태 전이(pending→active→done)는 데몬의 몫이다
(control.poll_transitions). UI 가 전이까지 하면 데몬과 두 주체가 같은 행을
다투게 된다.

HTML 을 파일이 아니라 문자열로 내장한 이유: 폐쇄망 서버에 .py 하나만 올리면
끝나야 한다. 정적 파일 경로 설정이 배포 사고의 단골이다. 같은 이유로 외부
CDN 참조도 없다.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

import pymysql
import yaml
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from . import raw_parquet
from .generator import diurnal_factor
from .pemdb1 import BLOCKABLE_SERIES, CPLANE_ROOT_DEFAULT, CPLANE_SERIES
from .control import (FAULT_SELECT, KINDS, T_CONFIG, T_FAULT, T_HISTORY,
                      add_fault, cancel_fault, ensure_tables, get_config,
                      list_faults, set_config)

HERE = Path(__file__).resolve().parents[2]

#: kind → 운영자용 한국어 라벨. KINDS(control.py)와 1:1 — 빠진 키가 있으면
#: 셀렉트에서 영문 코드가 그대로 보여 금방 눈에 띈다.
KIND_LABELS = {
    "gw_resource": "SGW 자원 소진",
    "gw_node_down": "SGW 노드 다운",
    "ran_radio": "무선 구간 열화",
    "mme_overload": "MME 과부하",
    "auth_failure": "인증(S6a) 이상",
    "dns_degradation": "DNS 열화",
    "external_server": "외부 서버 5xx",
    "transport_loss": "전송망 손실",
    "ue_model": "특정 단말 기종",
    "mobility": "핸드오버 이상",
    "change_suspected": "작업/변경 의심",
    "ims_pdn": "IMS PDN 이상",
    "nsa_nr_leg": "NSA NR leg 실패",
}

# ── MySQL 접속 ──────────────────────────────────────────────────────────────
#
# **상용 스키마(`pemdb1`)를 본다.** 전환 전에는 생성기 자체 창고(`graphio` DB 의
# `kpi_cell_min`·`file_ledger`·`cell_master`)를 읽었는데, graphio 가 `pemdb1` 으로
# 옮겨간 뒤 그 DB 는 **아무도 쓰지도 읽지도 않는다.** 화면은 멀쩡히 떠서 멈춘
# 숫자를 보여 줬고 에러는 없었다 — 그 조용함이 문제였다.
#
# 제어 테이블(`datagen_*`)도 같은 DB 에 둔다. UI 가 `graphio` 에 쓰고 데몬이
# `pemdb1` 에서 읽으면 **주입한 장애가 영원히 적용되지 않는다** — 양쪽 다
# 정상으로 보이고 그 사이에 아무 신호가 없다.

_CFG: dict | None = None  # main() 이 --config 로 채운다. 미설정이면 기본 yaml.


def _cfg() -> dict:
    """설정. `main()` 이 ``--config`` 로 채우고, 없으면 기본 yaml 이다.

    **`cli.load_cfg` 를 쓴다** (2026-10-14). 전에는 `datagen.yaml` 을 직접
    읽었고 ``--config`` 는 그 파일을 **통째로 대체**했다 — 얇은 환경별 파일
    (`config/container.yaml` 처럼 `mysql.host` 만 적은 것)을 주면 `pemdb1:`
    절 전체를 잃고, 그러면 UI 가 **생성기와 다른 셀 집합·요율을 보여 준다.**
    주입 대상이 생성 대상과 어긋나도 에러가 나지 않는 종류다.
    """
    global _CFG
    if _CFG is None:
        from .cli import load_cfg
        _CFG = load_cfg(None)
    return _CFG


def _pem_cfg() -> dict:
    return _cfg().get("pemdb1") or {}


def _cplane_root() -> Path:
    """C-Plane 원시 Parquet 루트. **설정이 정본이다.**

    `cli._cplane_store` 와 같은 규칙으로 읽는다 — 두 곳에서 다르게 풀면
    화면이 데몬이 쓰는 디렉터리와 **다른 자리**를 보고 "적재가 멈췄다"로
    보인다. 에러는 나지 않는다.
    """
    sec = (_pem_cfg().get("cplane_parquet") or {})
    raw = Path(str(sec.get("root") or CPLANE_ROOT_DEFAULT))
    return raw if raw.is_absolute() else (HERE / raw).resolve()


def _connect():
    """요청마다 새 커넥션 — pymysql 커넥션은 스레드 전용이라 공유하면
    커서 상태가 섞여 엉뚱한 자리에서 깨진다(FastAPI 는 동기 엔드포인트를
    스레드풀에서 돌린다). 요청당 연결 비용은 로컬/폐쇄망에서 무시할 수준."""
    m = _cfg()["mysql"]
    return pymysql.connect(
        host=m["host"], port=int(m["port"]), user=m["user"],
        password=m["password"],
        database=_pem_cfg().get("database", "pemdb6"),
        # 상용 collation 이 utf8mb3_general_ci 다. utf8mb4 로 열면 리터럴
        # 비교에서 Illegal mix of collations 가 날 수 있다 (pemfill 과 동일).
        charset="utf8mb3",
    )


#: 셀 목록 캐시. `pemdb1.load_cells` 는 결정론적이고 `enb_limit`·`drawable` 이
#: 같으면 **생성기와 글자까지 같은 집합**을 준다 — 그래서 별도 테이블 없이
#: 여기서 다시 계산한다. 셀→노드(MME/SGW/PGW) 배정은 DB 컬럼이 아니라
#: `load_cells` 의 eNB 해시로 정해지므로, 테이블을 조회해서는 알 수 없다.
_CELLS: list | None = None


def _cells(conn) -> list:
    """생성기와 **같은 셀 집합**. 캐시한다 — 요청마다 마스터를 다시 읽을 이유가 없다."""
    global _CELLS
    if _CELLS is None:
        from . import pemdb1 as P

        pem = _pem_cfg()
        drawable = None
        xw = pem.get("geo_crosswalk")
        if xw:
            q = Path(xw)
            if not q.is_absolute():
                q = (HERE / q).resolve()
            if q.exists():
                drawable = P.load_drawable_dongs(q)
        # 생성기(`cli.cmd_run`)와 **같은 인자**여야 같은 집합이 나온다
        _CELLS = P.load_cells(
            conn, enb_limit=int(pem.get("enb_limit", 0)), drawable=drawable)
    return _CELLS


# ── API ─────────────────────────────────────────────────────────────────────

app = FastAPI(title="probe-datagen 제어 UI")


class FaultReq(BaseModel):
    kind: str
    target: str | None = None
    duration_min: int = 30
    intensity: float = 1.0
    start_ts: str | None = None  # ISO 문자열, 없으면 now
    note: str | None = None


@app.get("/api/status")
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
        diurnal = str(get_config(conn, "diurnal", "true")).lower() != "false"
    finally:
        conn.close()
    return {
        "gen_enabled": on,
        "blocked": blocked,
        "diurnal": diurnal,
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


@app.get("/api/ingest")
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
    }


@app.get("/api/series")
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


@app.get("/api/settings")
def api_settings():
    """생성 설정 — CM_DATAGEN_CONFIG(DB) 가 우선, 없으면 datagen.yaml 기본값.

    데몬이 틱마다 그 테이블을 읽으므로 여기서 바꾸면 다음 분부터 반영된다
    (재기동 불필요). 장비 추가·삭제는 이 화면의 범위가 아니다 — 셀 재배정이
    필요해 설계가 따로 붙는다.
    """
    # **데몬과 같은 함수로 환산한다** — 두 곳에서 따로 계산하면 화면이
    # 데몬과 다른 보존 기간을 말한다. 지연 임포트는 이 모듈의 관례다
    # (`cli` 가 `ui` 를 부르므로 모듈 최상단에서는 순환이 된다).
    from .cli import _retention_default
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
                  "volume_pct", "reassign", "blocked", "diurnal"):
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
        "diurnal": str(over.get("diurnal", "true")).lower() != "false",
        #: 화면이 곡선을 그릴 수 있게 배율표를 함께 내린다 — 숫자를 화면에
        #: 다시 적으면 두 벌이 되고, 곡선을 고칠 때 화면만 옛 모양을 그린다.
        "diurnal_curve": [
            {"h": h,
             "call": round(diurnal_factor(h, "lte_call"), 3),
             "sess": round(diurnal_factor(h, "uplane_sessions"), 3),
             "byte": round(diurnal_factor(h, "transport"), 3)}
            for h in range(24)],
        "topology": {"cells": len(cells_list), **nodes},
        "note": "장비 추가·삭제는 미지원 — 셀 재배정 설계가 선행되어야 한다",
    }


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
    #: 시간대 곡선 on/off. 끄면 모든 시각이 피크(19시) 수준으로 평탄해진다.
    diurnal: bool | None = None


@app.post("/api/settings")
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
    finally:
        conn.close()
    return {"ok": True}


@app.get("/api/targets")
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
    finally:
        conn.close()

    def tally(attr: str) -> list[dict]:
        cnt: dict[str, int] = {}
        for c in cells_list:
            v = getattr(c, attr)
            if v:
                cnt[v] = cnt.get(v, 0) + 1
        return [{"id": k, "cells": n}
                for k, n in sorted(cnt.items(), key=lambda kv: (-kv[1], kv[0]))]

    sgg: dict[str, int] = {}
    for c in cells_list:
        if c.adm_sgg_cd:
            sgg[c.adm_sgg_cd] = sgg.get(c.adm_sgg_cd, 0) + 1
    return {
        "sgw": tally("sgw_id"), "mme": tally("mme_id"), "pgw": tally("pgw_id"),
        # 시군구 이름은 추출 단계에서 한글이 깨져 비어 있다 — 지어내지 않고
        # 코드를 그대로 보여 준다(불변규칙 17).
        "sgg": [{"code": k, "name": "", "cells": n}
                for k, n in sorted(sgg.items(), key=lambda kv: (-kv[1], kv[0]))],
    }


@app.get("/api/faults")
def api_faults(limit: int = 50, date: str | None = None):
    """장애 목록. `date` 가 있으면 **그날 시작한 것**만 본다.

    control.list_faults 는 제어 계약이라 건드리지 않고, 날짜 필터는 여기서 건다
    (화면 사정으로 공유 모듈의 시그니처를 바꾸지 않는다).
    """
    conn = _connect()
    try:
        if not date:
            return list_faults(conn, limit=limit)
        with conn.cursor() as cur:
            cur.execute(
                FAULT_SELECT + f" FROM {T_FAULT} "
                "WHERE START_TIME >= %s "
                "AND START_TIME < DATE_ADD(%s, INTERVAL 1 DAY) "
                "ORDER BY FAULT_ID DESC LIMIT %s", [date, date, limit])
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        conn.close()


@app.post("/api/faults")
def api_add_fault(req: FaultReq):
    if req.kind not in KINDS:
        raise HTTPException(400, f"알 수 없는 kind: {req.kind!r}")
    if req.duration_min <= 0:
        raise HTTPException(400, "duration_min 은 1 이상이어야 한다")
    try:
        start = datetime.fromisoformat(req.start_ts) if req.start_ts else datetime.now()
    except ValueError:
        raise HTTPException(400, f"start_ts 형식 오류: {req.start_ts!r}")
    target = req.target or None  # 빈 문자열 = 자동 선택 → NULL 로 저장
    conn = _connect()
    try:
        ensure_tables(conn)  # 새 DB 에 UI 만 먼저 떠도 동작하도록
        fid = add_fault(conn, req.kind, target, start,
                        duration_min=req.duration_min, intensity=req.intensity,
                        mode="manual", note=req.note)
    finally:
        conn.close()
    return {"fault_id": fid}


@app.post("/api/faults/{fault_id}/cancel")
def api_cancel_fault(fault_id: int):
    conn = _connect()
    try:
        ok = cancel_fault(conn, fault_id)
    finally:
        conn.close()
    if not ok:
        # pending/active 가 아니면 UPDATE 0행 — 이미 끝났거나 없는 id
        raise HTTPException(404, f"취소할 수 없는 fault: {fault_id}")
    return {"ok": True}


# ── 화면 ────────────────────────────────────────────────────────────────────

# 운영자용 단일 페이지. 상태 바·주입 폼은 즉시 그리고, 목록·슬롯은 5초 폴링 —
# 데몬 틱이 1분이라 그보다 촘촘할 이유가 없지만, 주입 직후 피드백은 빨라야 한다.
_HTML = """<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<title>probe-datagen 제어</title>
<style>
  :root { --bg:#14181f; --panel:#1d242e; --line:#2c3542; --fg:#d7dde6;
          --dim:#8a94a3; --accent:#4e9af1; --red:#e5534b; --yellow:#d8a22a;
          --green:#4caf7d; }
  * { box-sizing:border-box; }
  body { margin:0; padding:20px; background:var(--bg); color:var(--fg);
         font:14px/1.5 "Pretendard","Apple SD Gothic Neo","Malgun Gothic",sans-serif; }
  h1 { font-size:17px; margin:0 0 14px; }
  h1 small { color:var(--dim); font-weight:normal; margin-left:8px; }
  .grid2 { display:grid; grid-template-columns:1fr 1fr; gap:14px; }
  @media (max-width:980px) { .grid2 { grid-template-columns:1fr; } }
  .kv { display:flex; justify-content:space-between; padding:5px 0;
        border-bottom:1px solid var(--line); }
  .kv:last-child { border-bottom:none; }
  .kv b { font-variant-numeric:tabular-nums; }
  .sw { display:inline-flex; align-items:center; gap:6px; cursor:pointer;
        user-select:none; }
  .sw input { accent-color:var(--accent); }
  .row { display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
  /* 한 카드 안의 소제목. 카드를 더 쪼개지 않고 두 조작을 담으려면 이게 필요하다
     — 재배정과 장애 주입은 같은 카드이지만 **다른 일**이다. */
  .panel h3 { margin:0 0 8px; font-size:13px; font-weight:600; color:var(--fg);
              letter-spacing:.2px; }
  .panel h3 small { font-weight:400; color:var(--dim); margin-left:4px; }
  .sect { margin-top:16px; border-top:1px solid var(--line); padding-top:14px; }
  /* 카드 제목 **바로 뒤**의 섹션에는 구분선을 넣지 않는다 — h2 아래 선이
     한 번 더 그어져 카드가 두 장처럼 보인다. HTML 주석은 요소가 아니라서
     인접 선택자를 끊지 않는다. */
  .panel h2 + .sect { margin-top:0; border-top:0; padding-top:0; }
  /* ── 스위치 ──────────────────────────────────────────────────────────
     체크박스였다. "가동/정지"는 저장을 눌러야 하는 **설정값**이 아니라
     지금의 상태이고, 저장 버튼과 묶여 있으면 생성량까지 함께 반영된다 —
     끄려다가 조절 중인 비율을 같이 적용하게 된다(2026-10-15 사람 지시).
     이제 클릭이 곧 반영이다. */
  .tg { position:relative; display:inline-block; width:46px; height:24px;
        flex:0 0 auto; }
  .tg input { position:absolute; inset:0; opacity:0; margin:0; z-index:2;
              cursor:pointer; }
  .tg i { position:absolute; inset:0; background:#2c3542; border-radius:999px;
          transition:background .18s; pointer-events:none; }
  .tg i::after { content:""; position:absolute; top:3px; left:3px;
                 width:18px; height:18px; border-radius:50%; background:#8a94a3;
                 transition:transform .18s, background .18s; }
  .tg input:checked + i { background:#1d4e7f; }
  .tg input:checked + i::after { transform:translateX(22px);
                                 background:var(--accent); }
  .tg input:disabled + i { opacity:.45; }
  /* ── 상태등 ──────────────────────────────────────────────────────────
     회색=정지 · 파랑=생성 중 · 빨강=생성 중인데 장애가 주입돼 있다.
     **색만으로 말하지 않는다** — 바로 옆에 글자를 함께 둔다. 색각 이상이면
     파랑/빨강 구분이 안 되고, 그러면 "정상인데 빨간 줄 알았다"가 된다. */
  .lamp { display:inline-block; width:10px; height:10px; border-radius:50%;
          background:var(--dim); margin-right:7px; vertical-align:middle; }
  /* **녹색이다. 파랑이 아니다**(2026-10-15 사람 교정). 파랑은 이 화면에서
     `--accent` = 조작 가능한 것(버튼·슬라이더·링크)에 쓰고 있어서, 상태에
     같은 색을 쓰면 "눌러서 뭔가 하는 것"으로 읽힌다. */
  .lamp.on { background:var(--green);
             box-shadow:0 0 0 3px rgba(76,175,125,.20);
             animation:lamp 2.4s ease-in-out infinite; }
  /* 경고 — 계열이 끊겨 있다. 고장이 아니라 **미연동**이라 빨강이 아니다. */
  .lamp.warn { background:var(--yellow);
               box-shadow:0 0 0 3px rgba(216,162,42,.22);
               animation:lamp 1.8s ease-in-out infinite; }
  .lamp.fault { background:var(--red);
                box-shadow:0 0 0 3px rgba(229,83,75,.22);
                animation:lamp 1.1s ease-in-out infinite; }
  @keyframes lamp { 0%,100% { opacity:1 } 50% { opacity:.4 } }
  .daysep td { background:#222b36; color:var(--dim); font-size:12px;
               letter-spacing:.3px; padding:4px 8px; }
  .chart { width:100%; height:190px; display:block; }
  /* 툴팁은 SVG 밖 div 다 — SVG 안에 그리면 viewBox 가
     preserveAspectRatio="none" 로 늘어나 글자가 가로로 찌그러진다. */
  .chartwrap { position:relative; }
  .tip { position:absolute; pointer-events:none; opacity:0; transition:opacity .08s;
         background:#0f141b; border:1px solid #2c3542; border-radius:6px;
         padding:8px 10px; font-size:12px; line-height:1.55; color:var(--fg);
         box-shadow:0 6px 20px rgba(0,0,0,.5); white-space:nowrap; z-index:5; }
  .tip b { font-weight:600; }
  .tip .t-ts { color:var(--dim); font-size:11px; margin-bottom:4px; }
  .tip .t-row { display:flex; gap:10px; justify-content:space-between; }
  .tip .t-row span:first-child { display:flex; align-items:center; gap:6px; }
  .tip i { width:8px; height:8px; border-radius:2px; display:inline-block; }
  .tip .t-sum { border-top:1px solid #2c3542; margin-top:4px; padding-top:4px; }
  .tip .t-f { color:#e5534b; margin-top:4px; }
  .lg { display:flex; gap:12px; flex-wrap:wrap; font-size:12px; color:var(--dim);
        margin-top:6px; }
  .lg i { width:10px; height:10px; border-radius:2px; display:inline-block;
          margin-right:4px; vertical-align:-1px; }
  .rates { display:grid; grid-template-columns:repeat(4,1fr); gap:10px; }
  .rates label { font-size:12px; color:var(--dim); }
  .rates input { width:100%; }
  .nav { display:inline-flex; align-items:center; gap:6px; }
  .nav button { background:var(--panel); color:var(--fg); border:1px solid var(--line);
                border-radius:6px; padding:3px 9px; cursor:pointer; font-size:13px; }
  .nav button:hover:not(:disabled) { border-color:var(--accent); }
  .nav button:disabled { opacity:.35; cursor:default; }
  .nav button.ghost { color:var(--dim); }
  .nav b { min-width:132px; text-align:center; font-variant-numeric:tabular-nums; }
  .past { color:var(--yellow); }
  .bar { display:flex; gap:24px; align-items:center; background:var(--panel);
         border:1px solid var(--line); border-radius:8px; padding:10px 16px;
         margin-bottom:14px; }
/* 상태바 항목 — 세로 구분선으로 끊어 읽기 쉽게. 항목이 여섯이라 그냥 나열하면
   어디까지가 한 덩어리인지 안 보인다. */
.bar .st { display:inline-flex; align-items:baseline; gap:6px;
           padding-right:14px; margin-right:4px;
           border-right:1px solid var(--line); }
.bar .st:last-of-type { border-right:0; }
.bar .st small { font-size:11px; }
  .bar b { color:var(--accent); }
  .bar .active-cnt b { color:var(--red); }
  .panel { background:var(--panel); border:1px solid var(--line);
           border-radius:8px; padding:16px; margin-bottom:14px; }
  .panel h2 { font-size:14px; margin:0 0 12px; color:var(--dim); }
  form { display:grid; grid-template-columns:repeat(6,minmax(0,1fr)); gap:10px;
         align-items:end; }
  label { display:flex; flex-direction:column; gap:4px; font-size:12px;
          color:var(--dim); }
  select,input { background:#111620; color:var(--fg); border:1px solid var(--line);
                 border-radius:6px; padding:7px 8px; font-size:13px; width:100%; }
  input[type=range] { padding:0; accent-color:var(--accent); }
  button { background:var(--accent); color:#0c1017; border:0; border-radius:6px;
           padding:8px 14px; font-size:13px; font-weight:600; cursor:pointer; }
  button:hover { filter:brightness(1.12); }
  button.cancel { background:transparent; color:var(--red);
                  border:1px solid var(--red); padding:3px 10px; font-weight:normal; }
  table { width:100%; border-collapse:collapse; font-size:13px; }
  th,td { padding:7px 10px; text-align:left; border-bottom:1px solid var(--line); }
  th { color:var(--dim); font-weight:normal; }
  .st { padding:2px 8px; border-radius:10px; font-size:12px; }
  .st-active    { background:rgba(229,83,75,.18);  color:var(--red); }
  .st-pending   { background:rgba(216,162,42,.18); color:var(--yellow); }
  .st-done      { background:rgba(138,148,163,.18);color:var(--dim); }
  .st-cancelled { color:var(--dim); }
  tr.cancelled td { text-decoration:line-through; color:var(--dim); }
  #msg { margin-top:8px; font-size:12px; color:var(--green); min-height:18px; }
  #msg.err { color:var(--red); }
  .dim { color:var(--dim); }
</style>
</head>
<body>
<h1>probe-datagen 장애 주입 제어<small>생성 데몬이 1분 틱으로 반영한다</small></h1>

<!-- 상태바 — **오늘만 본다. 항상 실시간이다** (2026-10-15 사람 지시).
     날짜 이동과 실시간 토글을 뺐다: 과거를 볼 일이 없고, 끌 이유도 없다.
     대신 "지금 어떤 설정으로 돌고 있나"를 적는다 — 전에는 슬롯 시각과 활성
     장애 수뿐이라 **생성이 꺼져 있어도 화면만 보고는 알 수 없었다.** -->
<div class="bar">
  <span class="st" id="st-gen"><i class="lamp" id="lamp"></i><b>—</b></span>
  <span class="st">마지막 슬롯 <b id="last-slot">—</b>
    <small class="dim" id="slot-age"></small></span>
  <span class="st">생성량 <b id="st-pct">—</b>
    <small class="dim" id="st-rate"></small></span>
  <span class="st">직전 틱 <b id="st-last-n">—</b></span>
  <span class="st active-cnt">활성 장애 <b id="active-cnt">0</b> 건</span>
  <!-- **"오늘"만 적혀 있었고 채우는 코드가 없었다** — 영원히 "—" 였고,
       무엇의 오늘인지도 알 수 없었다(2026-10-15). 뜻은 "오늘 생성한 분 수와
       건수"다. 그 값은 `/api/ingest` 의 `by_day` 가 이미 들고 있다. -->
  <span class="st">오늘 생성 <b id="st-today">—</b>
    <small class="dim" id="st-today-n"></small></span>
  <span class="dim" id="poll-err" style="margin-left:auto"></span>
</div>

<div class="panel">
  <h2>생성 시계열 <small class="dim">분당 건수 · 최근 3시간 · 오른쪽 끝이 현재</small></h2>
  <div class="chartwrap" id="chartwrap">
    <!-- viewBox 는 `renderChart` 가 실제 폭으로 세운다 — 고정 폭을 늘려
         그리면(`preserveAspectRatio="none"`) 눈금 글자가 가로로 찌그러진다. -->
    <svg class="chart" id="chart"></svg>
    <div class="tip" id="tip"></div>
  </div>
  <div class="lg" id="legend"></div>
</div>

<!-- 설정 두 장을 나란히 — 생성량과 장애를 한 눈에 놓고 조절한다 -->
<div class="grid2">
  <div class="panel">
    <h2>생성 설정 <small class="dim">스위치는 즉시 · 나머지는 다음 틱부터</small></h2>
    <!-- 스위치는 **누르는 순간 반영된다.** 저장 버튼에 묶여 있으면 끄려다가
         조절 중인 생성량까지 함께 적용된다. -->
    <div class="row">
      <label class="tg" title="생성 가동/정지 — 즉시 반영">
        <input type="checkbox" id="gen-on" onchange="toggleGen(this)"><i></i>
      </label>
      <b id="gen-on-label" style="min-width:52px">—</b>
      <span class="dim" style="font-size:12px">끄면 그 분은 비어 있다 — 0 으로 채우지 않는다</span>
    </div>
    <div style="margin-top:14px">
      <label class="dim" style="font-size:12px">생성량
        <b id="pct-label" style="color:var(--fg)">—</b> %
      </label>
      <input id="pct" type="range" min="0" max="100" step="5" value="70"
             style="width:100%; margin:6px 0"
             oninput="document.getElementById('pct-label').textContent=this.value;
                      renderApplied(Number(this.value))">
      <div class="rates" id="rates"></div>
    </div>
    <!-- ── 시간대 곡선 ─────────────────────────────────────────────────
         **원래부터 켜져 있었다.** 빠진 기능이 아니고, 스위치가 없어서 켜고
         끌 수 없었을 뿐이다. 눈에 안 띄는 이유는 진폭이다 — 호 수에 걸리는
         가중이 0.25 라 04시↔19시가 1.14배고, 차트 창이 3시간이면 곡선의
         1/8 만 보인다. 총 트래픽 기준으로는 4배다
         (`generator.DIURNAL_WEIGHT` 머리말). -->
    <div class="sect">
      <h3>시간대 곡선 <small>— 04시 최저 · 19시 최고</small></h3>
      <div class="row">
        <label class="tg" title="시간대 곡선 on/off — 즉시 반영">
          <input type="checkbox" id="diurnal" onchange="toggleDiurnal(this)"><i></i>
        </label>
        <b id="diurnal-label" style="min-width:52px">—</b>
        <span class="dim" style="font-size:12px">끄면 모든 시각이 피크 수준으로 평탄</span>
      </div>
      <div class="dim" id="diurnal-note" style="margin-top:8px; font-size:12px;
           line-height:1.6"></div>
      <!-- **viewBox 를 박지 않는다**(2026-10-16). 320 폭 좌표계를
           `preserveAspectRatio="none"` 으로 1,100px 까지 늘려서 가로만 3배가
           됐다 — 눈금 글자가 뭉개지고 현재 시각 점이 타원이 되고 0시·23시
           끝점의 선이 가장자리에서 잘렸다. `renderDiurnal` 이 실제 폭을 읽어
           1:1 로 세운다. 높이는 아래 눈금 글자 자리를 포함한 값이다. -->
      <svg id="dcurve" style="width:100%; height:64px; margin-top:6px;
           display:block"></svg>
    </div>
    <!-- ── 데이터 보존 ─────────────────────────────────────────────────
         **시간 단위다**(2026-10-15 사람 지시). 전에는 "보존일수"였고 그 값은
         일 파티션에만 걸렸다 — 원시 xDR 은 코드 상수 24시간이라, 화면에서
         "1일"을 보면서 왜 원시가 따로 도는지 알 길이 없었다. 지금은 한 값이
         두 입도를 함께 자른다(`cli._purge_pemdb1`). -->
    <div class="sect">
      <h3>데이터 보존 <small>— 지난 파티션을 떨어뜨린다</small></h3>
      <div class="row">
        <label class="tg" title="보존 정리 on/off">
          <input type="checkbox" id="ret-on" onchange="renderRetNote()"><i></i>
        </label>
        <label class="dim" style="font-size:12px">보존
          <input id="ret" type="number" min="1" max="2160" style="width:72px"
                 oninput="renderRetNote()"> 시간</label>
        <button type="button" onclick="saveSettings()">설정 저장</button>
        <span class="dim" id="set-msg"></span>
      </div>
      <div class="dim" id="ret-note" style="margin-top:8px; font-size:12px;
           line-height:1.6"></div>
    </div>
    <div class="dim" id="topo" style="margin-top:10px; font-size:12px"></div>
  </div>
  <div class="panel">
    <!-- 한 카드에 둘을 담는다(2026-10-15 사람 지시) — 둘 다 "망을 건드려
         무슨 일이 나는지 본다"이고, 재배정이 위다. **같은 일이 아니라는 것은
         소제목이 말한다**: 재배정은 총량을 유지하는 쏠림이고 장애는 품질을
         떨어뜨린다. 섞어 읽으면 "쏠림을 넣었는데 왜 장애 목록에 없나"가 된다. -->
    <h2>망 조작 <small class="dim">재배정은 장애가 아니다 — 아래와 구분한다</small></h2>

    <!-- 장비 재배정 — 호 수는 그대로 두고 셀이 어느 노드를 거쳤다고
         기록하는지만 바꾼다. 비운 쪽이 0 이 되고 받은 쪽으로 몰린다.
         마스터(CL_CELL_MAP)는 건드리지 않는다. -->
    <div class="sect">
      <h3>장비 재배정 <small>— 한쪽으로 몰아 본다. 총량은 그대로</small></h3>
      <div style="display:flex; gap:8px; align-items:center">
        <select id="ra-kind" style="width:76px">
          <option>SGW</option><option>MME</option><option>PGW</option>
        </select>
        <select id="ra-src" style="flex:1"></select>
        <span class="dim">→</span>
        <select id="ra-dst" style="flex:1"></select>
        <button type="button" onclick="addReassign()">추가</button>
      </div>
      <div id="ra-list" class="dim" style="margin-top:8px; font-size:12px"></div>
      <div class="row" style="margin-top:8px">
        <button type="button" onclick="saveReassign()">재배정 저장</button>
        <span class="dim" id="ra-msg" style="font-size:12px">다음 틱부터 반영된다</span>
      </div>
    </div>

    <!-- ── 계열 차단 ───────────────────────────────────────────────────
         **장애가 아니라 미연동이다.** 상용에서 C-Plane 프로브와 U-Plane
         프로브가 따로 붙으므로 한쪽만 안 오는 상태가 실제로 있다. 그 상태에서
         화면·탐지가 "품질 0" 이 아니라 "확인할 수 없음"을 말하는지 보려면
         원천을 끊어 볼 수 있어야 한다(불변규칙 12·17).

         요율 0 으로 눌러도 비슷해 보이지만 **다른 상태다** — 그쪽은 "이 환경은
         원래 안 만든다"이고, 비율을 바꾸면 되살아난다. -->
    <div class="sect">
      <h3>계열 차단 <small>— 그 xDR 을 아예 안 만든다. 즉시 반영</small></h3>
      <div class="row" id="blk"></div>
      <div class="dim" id="blk-note" style="margin-top:8px; font-size:12px;
           line-height:1.6"></div>
    </div>

    <div class="sect">
    <h3>장애 주입</h3>
    <form id="f" onsubmit="return inject(event)">
      <label>종류
        <select id="kind"></select>
      </label>
      <label>대상
        <select id="target"><option value="">자동 선택</option></select>
      </label>
      <label>지속시간(분)
        <input id="dur" type="number" min="1" value="30">
      </label>
      <label>강도 <span id="iv">1.0</span>
        <input id="intensity" type="range" min="0.1" max="1.0" step="0.1" value="1.0"
               oninput="document.getElementById('iv').textContent=this.value">
      </label>
      <label>메모
        <input id="note" type="text" placeholder="(옵션)">
      </label>
      <button type="submit">주입</button>
    </form>
    <div id="msg"></div>
    </div>
  </div>
</div>

<!-- 결과 두 장 — 같은 레벨이어야 "설정 → 결과"로 읽힌다 -->
<div class="grid2">
  <div class="panel">
    <h2>인입 현황 <small class="dim" id="ing-range">최근 60분</small></h2>
    <div id="ingest" class="dim">불러오는 중…</div>
  </div>
  <div class="panel">
    <h2>장애 목록</h2>
    <table>
      <thead><tr><th>ID</th><th>종류</th><th>대상</th><th>시작</th>
                 <th>지속</th><th>상태</th><th></th></tr></thead>
      <tbody id="rows"><tr><td colspan="7" class="dim">불러오는 중…</td></tr></tbody>
    </table>
  </div>
</div>

<script>
const LABELS = __KIND_LABELS__;
const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"]/g,
  c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const ts = s => s ? String(s).replace("T"," ").slice(0,16) : "—";
// 날짜 구분 행이 날짜를 들고 있으므로 각 행은 시각만 보인다
const hm = s => s ? String(s).replace("T"," ").slice(11,16) : "—";
const DOW = ["일","월","화","수","목","금","토"];
const dow = d => DOW[new Date(d + "T00:00:00").getDay()] || "";

async function loadKinds() {
  // kind 목록의 정본은 서버(control.KINDS) — 하드코딩하면 종류가 늘 때 UI 가 뒤처진다
  const st = await (await fetch("/api/status")).json();
  $("kind").innerHTML = st.kinds.map(k =>
    `<option value="${k}">${esc(LABELS[k] || k)}</option>`).join("");
}

async function loadTargets() {
  const t = await (await fetch("/api/targets")).json();
  const og = (label, items) =>
    `<optgroup label="${label}">` + items.map(i =>
      `<option value="${esc(i.v)}">${esc(i.t)}</option>`).join("") + "</optgroup>";
  $("target").innerHTML = '<option value="">자동 선택</option>'
    + og("SGW", t.sgw.map(x => ({v:`SGW-${x.id}`, t:`SGW-${x.id} (셀 ${x.cells})`})))
    + og("MME", t.mme.map(x => ({v:`MME-${x.id}`, t:`MME-${x.id} (셀 ${x.cells})`})))
    + og("PGW", t.pgw.map(x => ({v:`PGW-${x.id}`, t:`PGW-${x.id} (셀 ${x.cells})`})))
    + og("시군구", t.sgg.map(x => ({v:`SGG:${x.code}`, t:`${x.name} (셀 ${x.cells})`})));
}

// ── 상태바 ──────────────────────────────────────────────────────────────
// "지금 어떤 설정으로, 얼마나 최근에 돌았나"를 한 줄로. 생성이 꺼져 있거나
// 비율이 내려가 있으면 **슬롯이 안 늘어나는 이유가 그것**인데, 전에는 그걸
// 화면에서 알 수 없어 데몬이 죽은 줄 알았다.
let LAST_TICK_N = null;
function renderBar(st) {
  const on = !!st.gen_enabled;
  // ── 상태등 — 세 가지 뜻 (2026-10-15 사람 지시) ──────────────────────
  //   회색  정지
  //   파랑  생성 중 · 장애 없음
  //   빨강  생성 중인데 장애가 주입돼 있다
  // **정지 중에 장애가 걸려 있어도 회색이다.** 주입은 남아 있지만 그 분에
  // 데이터를 안 만들므로 어디에도 나타나지 않는다 — 빨강으로 두면
  // "장애가 지금 재현되고 있다"로 읽힌다.
  const nf = (st.active || []).length;
  const bl = st.blocked || [];
  // **스위치도 여기서 맞춘다.** 전에는 기동 때 한 번만 읽어서, 다른 창(또는
  // `gp.sh`)에서 바꾸면 이 화면의 스위치가 반대로 남았다. 요청이 떠 있는
  // 동안(`disabled`)은 건드리지 않는다 — 내 클릭을 서버 응답 전에 덮는다.
  const sw = $("gen-on");
  if (!sw.disabled && sw.checked !== on) {
    sw.checked = on;
    $("gen-on-label").textContent = on ? "가동" : "정지";
    $("gen-on-label").style.color = on ? "var(--green)" : "var(--dim)";
  }
  //
  // **네 색의 순서는 심각도다.** 정지(회색) → 장애(빨강) → 계열 차단(노랑)
  // → 정상(녹색). 장애와 차단이 겹치면 빨강이다 — 둘 다 적되 더 센 쪽을
  // 색으로 말한다. 차단을 빨강으로 두지 않는 이유는 그것이 **고장이 아니라
  // 미연동**이고, 둘을 같은 색으로 두면 "무엇이 일어났나"를 색으로 못 가린다.
  const lamp = $("lamp");
  const kind = !on ? "" : nf ? " fault" : bl.length ? " warn" : " on";
  lamp.className = "lamp" + kind;
  // 색만으로 말하지 않는다 — 옆 글자가 같은 내용을 적는다(색각 이상).
  const parts = [];
  if (nf) parts.push(`장애 ${nf}`);
  if (bl.length) parts.push(`끊김 ${bl.join("·")}`);
  if (st.diurnal === false) parts.push("곡선 off");
  // 이름을 `b` 로 두면 아래 `const b = st.base_rates` 와 **같은 함수 스코프에서
  // 중복 선언**이 되고, 그러면 `<script>` 전체가 파싱되지 않아 화면이 통째로
  // 빈다. 전에 그렇게 한 번 비었다(2026-10-15) — `node --check` 가 잡았다.
  const lbl = $("st-gen").querySelector("b");
  lbl.textContent = !on ? "정지"
    : "생성 중" + (parts.length ? " · " + parts.join(" · ") : "");
  lbl.style.color = !on ? "var(--dim)" : nf ? "var(--red)"
                  : bl.length ? "var(--yellow)" : "var(--green)";
  $("last-slot").textContent = ts(st.last_slot);
  // **몇 분 전인지 적는다.** 슬롯 시각만으로는 "방금"인지 "한 시간째 멈춤"인지
  // 구분이 안 된다 — 멈춘 것을 정상으로 읽는 쪽이 가장 위험하다.
  if (st.last_slot && st.now) {
    const age = Math.round((new Date(st.now.replace(" ", "T"))
                          - new Date(String(st.last_slot).replace(" ", "T"))) / 60000);
    $("slot-age").textContent = age <= 1 ? "(방금)" : `(${age}분 전)`;
    // **`var(--warn)` 은 정의된 적이 없다**(`:root` 에 없다). 그래서 슬롯이
    // 세 분을 넘겨도 색이 바뀌지 않았다 — 조용히 아무 일도 안 하는 종류다.
    $("slot-age").style.color = age > 3 ? "var(--yellow)" : "";
  } else { $("slot-age").textContent = ""; }
  $("st-pct").textContent = `${(st.volume_pct ?? 100).toFixed(0)}%`;
  const r = st.rates || {}, b = st.base_rates || {};
  $("st-rate").textContent = b.lte_call
    ? `(${(r.lte_call ?? 0).toLocaleString()} / ${b.lte_call.toLocaleString()} 호·분)` : "";
  $("st-last-n").textContent =
    LAST_TICK_N == null ? "—" : LAST_TICK_N.toLocaleString() + " 건";
}

async function poll() {
  try {
    const [st, faults] = await Promise.all([
      (await fetch("/api/status")).json(),
      (await fetch("/api/faults" + q({limit:50}))).json(),
    ]);
    renderBar(st);
    $("active-cnt").textContent = st.active.length;
    $("poll-err").textContent = "";
    // 날짜가 바뀌는 자리에 구분 행을 넣는다. 목록이 하루를 넘기면 "16:01" 만
    // 보고는 어제 것인지 오늘 것인지 알 수 없다.
    let prevDay = null;
    const html = faults.map(f => {
      const day = String(f.start_ts || "").slice(0, 10);
      let sep = "";
      if (day && day !== prevDay) {
        sep = `<tr class="daysep"><td colspan="7">${esc(day)} (${dow(day)})</td></tr>`;
        prevDay = day;
      }
      return sep + `
      <tr class="${f.status}">
        <td>${f.fault_id}</td>
        <td>${esc(LABELS[f.kind] || f.kind)}</td>
        <td>${esc(f.target || "자동")}</td>
        <td>${hm(f.start_ts)}</td>
        <td>${f.duration_min}분</td>
        <td><span class="st st-${f.status}">${f.status}</span></td>
        <td>${["pending","active"].includes(f.status)
              ? `<button class="cancel" onclick="cancelFault(${f.fault_id})">취소</button>` : ""}</td>
      </tr>`;
    }).join("");
    $("rows").innerHTML = html ||
      '<tr><td colspan="7" class="dim">장애 이력이 없다</td></tr>';
  } catch (e) {
    $("poll-err").textContent = "서버 응답 없음";
  }
}

async function inject(ev) {
  ev.preventDefault();
  const body = {
    kind: $("kind").value,
    target: $("target").value || null,
    duration_min: parseInt($("dur").value, 10) || 30,
    intensity: parseFloat($("intensity").value),
    note: $("note").value || null,
  };
  const m = $("msg");
  try {
    const r = await fetch("/api/faults", {
      method: "POST", headers: {"content-type": "application/json"},
      body: JSON.stringify(body),
    });
    const j = await r.json();
    if (!r.ok) throw new Error(j.detail || r.status);
    m.className = ""; m.textContent = `주입됨 — fault #${j.fault_id} (다음 틱부터 반영)`;
    $("note").value = "";
    poll();
  } catch (e) {
    m.className = "err"; m.textContent = "주입 실패: " + e.message;
  }
  return false;
}

async function cancelFault(id) {
  const r = await fetch(`/api/faults/${id}/cancel`, {method: "POST"});
  if (!r.ok) {
    const j = await r.json().catch(() => ({}));
    alert("취소 실패: " + (j.detail || r.status));
  }
  poll();
}

loadKinds(); loadTargets(); poll();
// ── 오늘만 · 항상 실시간 (2026-10-15) ───────────────────────────────────
// 날짜 이동과 실시간 토글을 걷어냈다. 과거를 볼 일이 없고(사람 지시), 끄면
// 화면이 멈춘 건지 생성이 멈춘 건지 구분이 안 돼 오히려 혼란스러웠다.
//
// `q()` 는 남긴다 — API 가 아직 `date` 를 받고(`_window`), 나중에 과거 조회가
// 필요해지면 여기만 되살리면 된다. 지금은 항상 빈 문자열이다.
let DAYS = [];                 // /api/ingest 의 by_day — "오늘 N분" 표시용
const q = extra => {
  const qs = new URLSearchParams(extra || {}).toString();
  return qs ? "?" + qs : "";
};
const isLive = () => true;

// ── 인입 현황 ───────────────────────────────────────────────────────────
function renderIngest(d) {
  const tot = d.total || {};
  // **계열별로 보여 준다** (2026-10-15). 전에는 `lte_call` 한 줄뿐이었는데
  // U-Plane 건수가 `SERIES_STAT` 에 이미 있었다 — DB 에 있는 값을 안 쓰고
  // "그 계열은 없다"고 말하고 있었다.
  //
  // 바이트는 **있는 계열만** 적는다. C-Plane 은 시그널링이라 페이로드가 없고
  // 0 으로 적으면 "0바이트를 날랐다"로 읽힌다.
  const rows = (d.series || []).map(r =>
    `<div class="kv"><span>${esc(r.series)}</span>
       <b>${(r.recs ?? 0).toLocaleString()} <span class="dim">건 ·
       ${r.per_min}/분</span></b></div>`).join("");
  const today = (d.by_day || [])[0];
  const cum = (d.by_day || []).reduce((a, x) => a + (x.slots || 0), 0);
  $("ingest").innerHTML = rows +
    `<div class="kv" style="margin-top:6px; border-top:1px solid var(--line)">
       <span>합계</span><b>${(tot.records ?? 0).toLocaleString()} 건 ·
       ${tot.per_min ?? 0}/분</b></div>` +
    `<div class="dim" style="margin-top:8px; font-size:12px">
       오늘 ${today ? today.slots : 0}분 · 누적 ${cum}분
       ${(d.by_day || []).length > 1 ? "(" + d.by_day.length + "일)" : ""}</div>`;
  $("ing-range").textContent = d.date ? `${d.date} 하루` : `최근 ${d.minutes}분`;
  // 데이터가 있는 날짜는 여기서만 알 수 있다 — 별도 조회를 만들지 않는다.
  const days = (d.by_day || []).map(x => String(x.d).slice(0, 10));
  if (days.length && days.join() !== DAYS.join()) DAYS = days;
  // **상태바의 "오늘 생성"을 여기서 채운다.** 전에는 자리만 있고 채우는 곳이
  // 없어 영원히 "—" 였다 — `by_day` 가 이미 분 수와 건수를 들고 있으므로
  // 질의를 더 늘리지 않는다. `by_day` 는 날짜 내림차순이라 [0] 이 오늘이다.
  //
  // 오늘 행이 **아예 없을 수도 있다**(자정 직후, 또는 하루 내내 정지).
  // 그때는 0 이 사실이다 — 생성 이력이 0분이라는 뜻이고, 추정이 아니다.
  const t0 = (d.by_day || [])[0];
  // **`toISOString()` 을 쓰지 않는다 — 그것은 UTC 다.** KST 는 +9 라
  // 자정부터 09시까지 "오늘"이 어제로 판정되고, 그 아홉 시간 동안 화면이
  // "0분"이라고 말한다. 서버·데몬·DB 가 모두 로컬 시각이므로 여기도 로컬이다.
  const lx = new Date();
  const ymd = `${lx.getFullYear()}-${String(lx.getMonth()+1).padStart(2,"0")}`
            + `-${String(lx.getDate()).padStart(2,"0")}`;
  const isToday = t0 && String(t0.d).slice(0, 10) === ymd;
  $("st-today").textContent = isToday ? `${t0.slots}분` : "0분";
  $("st-today-n").textContent =
    isToday ? `(${Number(t0.recs || 0).toLocaleString()}건)` : "";
}

// ── 시계열 (인라인 SVG — 폐쇄망이라 외부 라이브러리를 쓰지 않는다) ─────
//
// **고정 격자다.** 예전에는 `bw = W / points.length` 로 봉 폭을 데이터 수에
// 맞췄다. 그래서 막 띄웠을 때 2분치가 화면을 반씩 채우는 거대한 봉이 되고,
// 분이 쌓일수록 봉이 좁아져 **같은 화면이 매 분 다르게 보였다.**
//
// 지금은 창(3시간=180분)을 분 단위 슬롯으로 고정하고 **오른쪽 끝을 현재
// 시각**으로 둔다. 분이 지나면 한 칸씩 왼쪽으로 밀린다(Grafana 형태).
// 데이터가 없는 슬롯은 비워 둔다 — 0 으로 그리면 "생성했는데 0건"과
// "아직 안 돌았다"가 같아 보인다(불변규칙 17).
const COLORS = { lte_call:"#4e9af1", transport:"#4caf7d",
                 http:"#d8a22a", dns:"#a872e0" };
//: `W` 는 **레이아웃 전(폭 0)에만 쓰는 대비값**이다. 실제 폭은 그릴 때
//: `clientWidth` 로 읽어 viewBox 를 1:1 로 세운다 — 창 크기가 바뀌어도 글자와
//: 선이 늘어나지 않는다. `H` 는 CSS `.chart` 의 높이와 같아야 한다.
const CHART = { W:1000, H:190, PAD_B:22, PAD_T:8 };
let CHART_SLOTS = [];          // 마우스 추적이 쓰는 슬롯 메타

function fmtBytes(n) {
  if (n === null || n === undefined) return null;
  const u = ["B","KB","MB","GB","TB"]; let i = 0, v = Number(n);
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return (v >= 100 || i === 0 ? Math.round(v) : v.toFixed(1)) + " " + u[i];
}

let LAST_SERIES = null;
function renderChart(d) {
  LAST_SERIES = d;
  // **상태바의 "직전 틱"을 여기서 채운다.** 선언만 해 두고 채우는 곳이 없어
  // 영원히 "—" 였다(2026-10-15). 시계열이 이미 분당 건수를 들고 있으므로
  // 질의를 더 늘리지 않는다 — 마지막 점이 곧 방금 끝난 틱이다.
  const pts = d.points || [];
  if (pts.length) {
    const last = pts[pts.length - 1];
    LAST_TICK_N = last && last.total != null ? last.total : null;
  }
  const svg = $("chart"), P = d.points || [];
  const { H, PAD_B, PAD_T } = CHART;
  const W = Math.max(Math.round(svg.clientWidth || 0), 320);
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  const names = d.series_names || [];

  // 슬롯 축을 **현재 분**에서 역산한다. 서버 응답이 비어 있어도 축은 선다 —
  // 축이 데이터에 의존하면 빈 화면에서 눈금이 사라져 "고장"처럼 보인다.
  const hours = d.hours || 3, slots = hours * 60;
  const now = new Date(); now.setSeconds(0, 0);
  const t0 = now.getTime() - (slots - 1) * 60000;
  const bw = W / slots;

  // 분 → 슬롯 인덱스. `ts` 는 서버가 로컬 시각으로 내려준 ISO 문자열이다.
  const byIdx = new Map();
  P.forEach(p => {
    const t = new Date(p.ts).getTime();
    const i = Math.round((t - t0) / 60000);
    if (i >= 0 && i < slots) byIdx.set(i, p);
  });

  const max = Math.max(1, ...P.map(p => p.total || 0));
  const y = v => PAD_T + (H - PAD_B - PAD_T) * (1 - v / max);
  let out = "";

  // ── 장애 구간 — **잘 보여야 한다** (2026-10-15) ────────────────────
  // 전에는 opacity .10 의 배경 띠 하나뿐이라 **막대에 묻혀 안 보였다.**
  // 주입한 구간을 못 찾으면 "생성량 변화와 겹쳐 본다"는 목적 자체가 사라진다.
  // 셋을 겹친다: 진한 배경 · 상단 두꺼운 막대 · 시작 지점의 세로선+캡션.
  //
  // 장애 판정이 아니라 **주입 이력의 표시**다. 품질 판단은 관제 화면(8008)의
  // 일이고 여기서 색으로 단정하면 두 화면이 다른 말을 한다.
  const fseg = [];               // 연속 구간으로 묶는다 — 칸마다 선을 그으면 숲이 된다
  byIdx.forEach((p, i) => {
    const on = !!(p.faults && p.faults.length);
    const last = fseg[fseg.length - 1];
    if (on && last && last.to === i - 1) { last.to = i; }
    else if (on) fseg.push({ from: i, to: i, ids: p.faults });
  });
  fseg.forEach(seg => {
    const x = seg.from * bw, w = Math.max((seg.to - seg.from + 1) * bw, 2);
    out += `<rect x="${x.toFixed(2)}" y="${PAD_T}" width="${w.toFixed(2)}"
             height="${H-PAD_B-PAD_T}" fill="#e5534b" opacity=".18"/>`;
    // 상단 막대 — 배경만으로는 막대 색에 묻힌다
    out += `<rect x="${x.toFixed(2)}" y="${(PAD_T-6).toFixed(1)}"
             width="${w.toFixed(2)}" height="4" fill="#e5534b"/>`;
    // 시작 지점 — 세로 점선 + 라벨. "언제 주입했나"가 한눈에 보여야 한다
    out += `<line x1="${x.toFixed(2)}" y1="${(PAD_T-6).toFixed(1)}"
             x2="${x.toFixed(2)}" y2="${(H-PAD_B).toFixed(1)}"
             stroke="#e5534b" stroke-width="1.5" stroke-dasharray="3 2"/>`;
    out += `<text x="${(x+3).toFixed(2)}" y="${(PAD_T+9).toFixed(1)}"
             fill="#e5534b" font-size="9">▼ ${esc(String(seg.ids[0] ?? ""))}</text>`;
  });

  // 계열별 적층 막대. 폭이 1px 미만이면 틈이 생기지 않게 간격을 두지 않는다.
  const gap = bw > 3 ? 0.5 : 0;
  byIdx.forEach((p, i) => {
    let acc = 0;
    names.forEach(n => {
      const v = (p.series || {})[n] || 0; if (!v) return;
      const y0 = y(acc + v), y1 = y(acc);
      out += `<rect x="${(i*bw).toFixed(2)}" y="${y0.toFixed(1)}"
               width="${Math.max(bw-gap, 0.6).toFixed(2)}"
               height="${Math.max(y1-y0, 0.5).toFixed(1)}"
               fill="${COLORS[n] || "#6b7684"}"/>`;
      acc += v;
    });
  });

  // 눈금 — 정시마다. 슬롯이 촘촘하면 30분 간격으로 줄인다.
  const everyMin = bw < 4 ? 30 : 60;
  for (let i = 0; i < slots; i++) {
    const t = new Date(t0 + i * 60000);
    if (t.getMinutes() % everyMin !== 0) continue;
    const x = (i * bw).toFixed(2);
    const hh = String(t.getHours()).padStart(2,"0"),
          mm = String(t.getMinutes()).padStart(2,"0");
    const midnight = t.getHours() === 0 && t.getMinutes() === 0;
    out += midnight
      ? `<line x1="${x}" y1="${PAD_T}" x2="${x}" y2="${H-PAD_B}" stroke="#4e9af1"/>
         <text x="${+x+4}" y="${H-8}" fill="#4e9af1" font-size="11">${t.getMonth()+1}/${t.getDate()}</text>`
      : `<line x1="${x}" y1="${H-PAD_B}" x2="${x}" y2="${H-PAD_B+4}" stroke="#2c3542"/>
         <text x="${+x+3}" y="${H-8}" fill="#8a94a3" font-size="11">${hh}:${mm}</text>`;
  }
  // 오른쪽 끝 = 현재. 눈금 라벨은 왼쪽으로 붙인다(잘리지 않게).
  out += `<line x1="${W-0.5}" y1="${PAD_T}" x2="${W-0.5}" y2="${H-PAD_B}"
           stroke="#4e9af1" stroke-width="1" opacity=".55"/>`;
  out += `<text x="${W-4}" y="${H-8}" fill="#4e9af1" font-size="11"
           text-anchor="end">now</text>`;
  out += `<text x="4" y="${PAD_T+10}" fill="#8a94a3" font-size="11">${max.toLocaleString()}</text>`;
  if (!P.length) out += `<text x="${(W/2).toFixed(0)}" y="95" fill="#8a94a3" text-anchor="middle"
                          font-size="12">데이터 없음</text>`;
  svg.innerHTML = out;

  CHART_SLOTS = { slots, t0, bw, byIdx, names };
  $("legend").innerHTML = names.map(n =>
    `<span><i style="background:${COLORS[n] || "#6b7684"}"></i>${esc(n)}</span>`).join("")
    + '<span><i style="background:#e5534b;opacity:.35"></i>장애 주입 구간</span>';
}

// ── 툴팁 ────────────────────────────────────────────────────────────────
// **viewBox 좌표가 아니라 비율로 집는다.** 화면 픽셀을 `box.width` 로 나눠
// 슬롯을 구하므로 viewBox 폭이 얼마든(지금은 실제 폭과 1:1) 결과가 같다 —
// 좌표계를 직접 쓰면 폭이 바뀔 때마다 여기도 같이 고쳐야 한다.
function chartHover(ev) {
  const tip = $("tip"), svg = $("chart");
  if (!CHART_SLOTS || !CHART_SLOTS.slots) { tip.style.opacity = 0; return; }
  const { slots, t0, byIdx, names } = CHART_SLOTS;
  const box = svg.getBoundingClientRect();
  const px = ev.clientX - box.left;
  const i = Math.floor(px / box.width * slots);
  if (i < 0 || i >= slots) { tip.style.opacity = 0; return; }

  const t = new Date(t0 + i * 60000);
  const hh = String(t.getHours()).padStart(2,"0"),
        mm = String(t.getMinutes()).padStart(2,"0");
  const p = byIdx.get(i);
  let html = `<div class="t-ts">${t.getFullYear()}-${String(t.getMonth()+1).padStart(2,"0")}`
           + `-${String(t.getDate()).padStart(2,"0")} ${hh}:${mm}</div>`;
  if (!p) {
    html += '<div class="dim">생성 기록 없음</div>';
  } else {
    const bytes = p.bytes || {};
    names.forEach(n => {
      const v = (p.series || {})[n]; if (v === undefined) return;
      // 바이트가 없는 계열(C-Plane)은 크기를 적지 않는다 — 0 으로 적으면
      // "안 흘렀다"로 읽힌다(불변규칙 17).
      const b = fmtBytes(bytes[n]);
      html += `<div class="t-row"><span><i style="background:${COLORS[n]||"#6b7684"}"></i>`
            + `${esc(n)}</span><span><b>${v.toLocaleString()}</b> 건`
            + (b ? ` · ${b}` : "") + `</span></div>`;
    });
    html += `<div class="t-row t-sum"><span>합계</span><span><b>`
          + `${(p.total||0).toLocaleString()}</b> 건`
          + (p.total_bytes ? ` · ${fmtBytes(p.total_bytes)}` : "") + `</span></div>`;
    if (p.faults && p.faults.length)
      html += `<div class="t-f">장애 주입 #${p.faults.join(", #")}</div>`;
  }
  tip.innerHTML = html;
  tip.style.opacity = 1;
  // 오른쪽 끝에서 툴팁이 패널 밖으로 나가지 않게 뒤집는다.
  const wrap = $("chartwrap").getBoundingClientRect();
  const tw = tip.offsetWidth, left = ev.clientX - wrap.left;
  tip.style.left = (left + tw + 16 > wrap.width ? left - tw - 12 : left + 12) + "px";
  tip.style.top = Math.max(0, ev.clientY - wrap.top - tip.offsetHeight - 10) + "px";
}

// ── 생성 설정 ───────────────────────────────────────────────────────────
// **비율이 정본이다.** 계열별 절대값은 기준(100%) × 비율로 유도해 보여 주기만
// 한다 — 입력으로 두면 두 경로가 생기고, 데몬이 읽는 것은 비율 하나다.
let BASE_RATES = {};
function renderApplied(pct) {
  const keys = Object.keys(BASE_RATES);
  $("rates").innerHTML = keys.map(k => {
    const base = BASE_RATES[k] || 0;
    const now = Math.max(0, Math.round(base * pct / 100));
    return `<div><label>${esc(k)} (분당)</label>
      <div style="font-variant-numeric:tabular-nums">
        <b>${now.toLocaleString()}</b>
        <span class="dim" style="font-size:11px"> / ${base.toLocaleString()}</span>
      </div></div>`;
  }).join("");
}
function renderSettings(d) {
  BASE_RATES = d.base_rates || {};
  const pct = Number(d.volume_pct ?? 100);
  $("pct").value = pct;
  $("pct-label").textContent = pct;
  renderApplied(pct);
  $("gen-on").checked = !!d.enabled;
  $("gen-on-label").textContent = d.enabled ? "가동" : "정지";
  $("gen-on-label").style.color = d.enabled ? "var(--green)" : "var(--dim)";
  $("ret").value = d.retention_hours;
  $("ret-on").checked = !!d.retention_on;
  renderRetNote();
  DCURVE = d.diurnal_curve || [];
  $("diurnal").checked = d.diurnal !== false;
  renderDiurnal();
  BLOCKABLE = d.blockable || [];
  BLOCKED = new Set(d.blocked || []);
  renderBlocked();
  REASSIGN = d.reassign || {};
  const t = d.topology || {};
  NODES = { MME: t.MME_IDS || [], SGW: t.SGW_IDS || [], PGW: t.PGW_IDS || [] };
  renderReassign();
  $("topo").textContent =
    `토폴로지 — 셀 ${t.cells} · SGW ${t.SGW} · MME ${t.MME} · PGW ${t.PGW}` +
    (d.note ? ` · ${d.note}` : "");
}
// ── 생성 가동 스위치 — **누르는 순간 반영한다** ─────────────────────────
// 저장 버튼에 묶어 두면 끄려다가 조절 중인 생성량까지 함께 적용된다.
// `enabled` 만 보내는 이유다 — 다른 키는 `null` 로 가고 서버가 건드리지 않는다.
//
// **되돌린다.** 실패했는데 스위치가 켜진 채로 남으면 화면과 데몬이 다른 말을
// 하고, 그 상태로 다음 틱을 기다리게 된다.
async function toggleGen(el) {
  const want = el.checked;
  el.disabled = true;
  $("gen-on-label").textContent = "…";
  try {
    const r = await fetch("/api/settings", { method:"POST",
      headers:{"content-type":"application/json"},
      body: JSON.stringify({ enabled: want }) });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || "실패");
    $("set-msg").textContent = want ? "생성 가동" : "생성 정지";
  } catch (e) {
    el.checked = !want;
    $("set-msg").textContent = "실패 — " + e.message;
  } finally {
    el.disabled = false;
    $("gen-on-label").textContent = el.checked ? "가동" : "정지";
    $("gen-on-label").style.color = el.checked ? "var(--green)" : "var(--dim)";
    setTimeout(() => { $("set-msg").textContent = ""; }, 4000);
  }
}

// ── 보존 안내 — **입도의 한계를 화면이 말한다** ─────────────────────────
// 6시간을 적어도 집계(일 파티션)는 그만큼 줄지 않는다. 적지 않으면 "설정이
// 안 먹는다"로 읽히고, 그 다음에 하는 일은 더 짧은 값을 넣어 보는 것이다.
function renderRetNote() {
  const on = $("ret-on").checked, h = Number($("ret").value || 0);
  const el = $("ret-note");
  $("ret").disabled = !on;
  if (!on) {
    el.innerHTML = '<b style="color:var(--yellow)">정리하지 않는다</b> — '
                 + '파티션이 계속 쌓인다. 디스크를 직접 봐야 한다';
    return;
  }
  const d = h / 24;
  el.innerHTML =
    `원시 xDR(시간 파티션) <b>${h}시간</b>치만 남는다<br>`
    + `집계 <code>PM_*_1M</code>(일 파티션)은 <b>하루 입도</b>로만 떨어진다 — `
    + (d < 1 ? `${h}시간을 줘도 최대 하루치가 남는다`
             : `약 ${d.toFixed(d < 10 ? 1 : 0)}일치`)
    + `<br><span style="color:var(--dim)">원시 Parquet 는 이 값과 별개다 — `
    + `생성 설정의 <code>retain_minutes</code>(U-Plane 10분 · C-Plane 1440분)</span>`;
}

// ── 시간대 곡선 ─────────────────────────────────────────────────────────
// **배율표를 화면에 적지 않는다.** 서버가 `generator.diurnal_factor` 로 계산해
// 내려 준다 — 숫자를 여기 복사하면 곡선을 고칠 때 화면만 옛 모양을 그리고,
// 그 상태는 에러가 나지 않는다.
let DCURVE = [];
async function toggleDiurnal(el) {
  const want = el.checked;
  el.disabled = true;
  try {
    const r = await fetch("/api/settings", { method:"POST",
      headers:{"content-type":"application/json"},
      body: JSON.stringify({ diurnal: want }) });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || "실패");
  } catch (e) { el.checked = !want; }
  finally { el.disabled = false; renderDiurnal(); }
}
//
// **주기적으로 다시 부른다**(2026-10-16). 이 함수는 `new Date()` 로 현재 시를
// 직접 읽는데 `renderSettings` 안에서만 불렸다 — 즉 **페이지를 연 시각에
// 박제돼** 몇 시간이 지나도 "지금 N시"와 점이 그 자리에 머물렀다. 상단
// 시계열이 멀쩡했던 건 축 재계산 타이머가 따로 있었기 때문이다.
function renderDiurnal() {
  const on = $("diurnal").checked;
  $("diurnal-label").textContent = on ? "켜짐" : "평탄";
  $("diurnal-label").style.color = on ? "var(--green)" : "var(--dim)";
  const h = new Date().getHours();
  const c = DCURVE[h];
  const note = $("diurnal-note"), svg = $("dcurve");
  // 폭을 그릴 때마다 읽어 viewBox 를 1:1 로 세운다. 패널이 접혀 있거나
  // 레이아웃 전이면 0 이 오므로 하한을 둔다 — 0 으로 나누지 않게.
  const W = Math.max(Math.round(svg.clientWidth || 0), 240), H = 64;
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  if (!on) {
    note.innerHTML = '모든 시각이 <b>피크(19시)</b> 수준이다 — '
                   + '야간에도 주간만큼 만든다';
    svg.innerHTML = `<line x1="5" y1="${(H-16+8)/2}" x2="${W-5}" y2="${(H-16+8)/2}"
      stroke="var(--dim)" stroke-width="1.5" stroke-dasharray="3 3"/>`;
    return;
  }
  // 세 자리에 나눠 걸린 곡선을 다 적는다 — 하나만 보여 주면 "왜 호 수는
  // 거의 안 변하나"의 답이 화면에 없다.
  note.innerHTML = c
    ? `지금 <b>${h}시</b> — 호 <b>${c.call}</b> · 호당 세션 <b>${c.sess}</b> `
      + `· 세션 바이트 <b>${c.byte}</b> → 총 트래픽 `
      + `<b>${(c.call * c.sess * c.byte).toFixed(3)}</b> (19시 = 1.000)<br>`
      + `<span style="color:var(--dim)">호 수의 진폭은 일부러 작다(04↔19시 `
      + `1.14배) — 야간에 셀 표본이 마르면 탐지가 통째로 선다(불변규칙 5)</span>`
    : "";
  if (!DCURVE.length) { svg.innerHTML = ""; return; }
  // 총 트래픽 곡선 한 줄. **하루는 24칸이다** — 전에는 `i/23` 으로 펴서
  // 23시가 오른쪽 끝에 붙었고, 그만큼 모든 시각의 x 가 오른쪽으로 밀려
  // 현재 시각 점이 눈금과 어긋났다. 끝은 0시 값으로 한 번 더 찍어 하루를
  // 닫는다 — 23시→0시 구간도 실제로 있는 구간이다.
  const padX = 5, padT = 8, padB = 16;      // padB 는 아래 눈금 글자 자리
  const v = DCURVE.map(x => x.call * x.sess * x.byte);
  const vv = v.concat(v[0]);
  const lo = Math.min(...vv), hi = Math.max(...vv);
  const px = i => padX + (i / 24) * (W - padX * 2);
  const py = n => H - padB - ((n - lo) / (hi - lo || 1)) * (H - padT - padB);
  const d = vv.map((n, i) => `${i ? "L" : "M"}${px(i).toFixed(1)},${py(n).toFixed(1)}`).join("");
  // 현재 시각 — 점만으로는 곡선 위에서 눈에 잘 안 띈다. 세로 안내선을 함께
  // 둬서 "지금 어디"가 한눈에 잡히게 한다. 점은 **정시 단위로만** 움직인다 —
  // `generator.diurnal_factor` 가 `DIURNAL[hour]` 계단이라, 화면에서 분 단위로
  // 보간하면 생성기가 실제로 쓰는 배율과 다른 말을 하게 된다.
  svg.innerHTML =
    `<line x1="${px(h).toFixed(1)}" y1="${padT - 4}" x2="${px(h).toFixed(1)}"
       y2="${H - padB}" stroke="var(--yellow)" stroke-width="1"
       opacity=".35" stroke-dasharray="2 2"/>`
    + `<path d="${d}" fill="none" stroke="var(--green)" stroke-width="1.6"/>`
    + `<circle cx="${px(h).toFixed(1)}" cy="${py(v[h]).toFixed(1)}" r="3.2"
         fill="var(--yellow)"/>`
    + [0, 6, 12, 18].map(t =>
        `<text x="${px(t).toFixed(1)}" y="${H - 3}" fill="#6b7684"
           font-size="10">${t}시</text>`).join("");
}

// ── 계열 차단 ───────────────────────────────────────────────────────────
// **체크가 곧 반영이다.** 상태등이 1초 안에 노랑으로 바뀌는 것이 이 조작의
// 확인 수단이다 — 저장 버튼을 거치면 "눌렀는데 아무 일도 안 보인다"가 된다.
let BLOCKABLE = [], BLOCKED = new Set();
function renderBlocked() {
  $("blk").innerHTML = BLOCKABLE.map(s2 =>
    `<label class="sw"><input type="checkbox" value="${esc(s2)}"
       ${BLOCKED.has(s2) ? "checked" : ""} onchange="toggleBlock(this)">
       <span style="${BLOCKED.has(s2) ? "color:var(--yellow)" : ""}">${esc(s2)}</span>
     </label>`).join("");
  const note = $("blk-note");
  if (!BLOCKED.size) { note.textContent = "없음 — 네 계열 모두 생성한다"; return; }
  // **lte_call 차단의 연쇄를 숨기지 않는다.** U-Plane 세션이 성공 호에서
  // 파생되므로 호를 끊으면 넷이 다 빈다 — 적지 않으면 "생성기가 죽었다"로
  // 읽히고, 그 다음에 하는 일은 컨테이너를 재시작하는 것이다.
  note.innerHTML =
    `<b style="color:var(--yellow)">${[...BLOCKED].sort().join(" · ")}</b> 끊김 — `
    + "그 분에 파일을 만들지 않는다(0 건이 아니라 <b>없음</b>)"
    + (BLOCKED.has("lte_call")
       ? '<br><b style="color:var(--red)">lte_call 을 끊으면 U-Plane 도 멈춘다</b>'
         + ' — 세션이 성공 호에서 파생된다'
       : "");
}
async function toggleBlock(el) {
  const v = el.value, want = new Set(BLOCKED);
  el.checked ? want.add(v) : want.delete(v);
  const box = $("blk").querySelectorAll("input");
  box.forEach(b => b.disabled = true);
  try {
    const r = await fetch("/api/settings", { method:"POST",
      headers:{"content-type":"application/json"},
      body: JSON.stringify({ blocked: [...want] }) });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || "실패");
    BLOCKED = want;
  } catch (e) { el.checked = !el.checked; }
  finally { box.forEach(b => b.disabled = false); renderBlocked(); }
}

// ── 장비 재배정 ─────────────────────────────────────────────────────────
// 화면이 들고 있는 현재 맵. 저장 시 통째로 보낸다 — 부분 갱신을 만들면
// "지웠는데 남아 있다" 같은 상태가 생긴다.
let REASSIGN = {};
let NODES = { MME: [], SGW: [], PGW: [] };
function renderReassign() {
  const items = [];
  for (const [kind, pairs] of Object.entries(REASSIGN))
    for (const [src, dst] of Object.entries(pairs || {}))
      items.push(`<span class="chip">${esc(kind)} ${esc(src)} → ${esc(dst)}
        <a href="#" onclick="dropReassign('${esc(kind)}','${esc(src)}');return false"
           style="margin-left:6px">✕</a></span>`);
  $("ra-list").innerHTML = items.length ? items.join(" ")
    : '<span class="dim">없음 — 모든 셀이 원래 장비를 쓴다</span>';
  // 드롭다운은 현재 종류의 노드 목록으로
  const kind = $("ra-kind").value;
  const opts = (NODES[kind] || []).map(n => `<option>${esc(n)}</option>`).join("");
  $("ra-src").innerHTML = opts; $("ra-dst").innerHTML = opts;
}
function addReassign() {
  const k = $("ra-kind").value, a = $("ra-src").value, b = $("ra-dst").value;
  if (!a || !b || a === b) return;
  (REASSIGN[k] = REASSIGN[k] || {})[a] = b;
  renderReassign();
}
function dropReassign(k, a) {
  if (REASSIGN[k]) { delete REASSIGN[k][a];
    if (!Object.keys(REASSIGN[k]).length) delete REASSIGN[k]; }
  renderReassign();
}
$("ra-kind").addEventListener("change", renderReassign);

// **재배정만 보낸다.** 왼쪽 카드의 `설정 저장` 을 쓰게 하면 조절 중인
// 생성량 슬라이더까지 같이 적용된다 — 다른 카드의 버튼을 눌러야 하는 것도
// 설명이 필요한 동작이었다.
async function saveReassign() {
  const r = await fetch("/api/settings", { method:"POST",
    headers:{"content-type":"application/json"},
    body: JSON.stringify({ reassign: REASSIGN }) });
  const j = await r.json().catch(() => ({}));
  $("ra-msg").textContent = r.ok ? "저장됨 — 다음 틱부터" : (j.detail || "실패");
  setTimeout(() => { $("ra-msg").textContent = "다음 틱부터 반영된다"; }, 4000);
}

async function saveSettings() {
  // **`enabled` 를 보내지 않는다.** 스위치가 이미 반영했고, 여기서 또 보내면
  // 그 사이 다른 창에서 켠 것을 이 화면의 낡은 값으로 되돌린다.
  const body = { volume_pct: Number($("pct").value), reassign: REASSIGN,
                 retention_hours: Number($("ret").value || 24),
                 retention_on: $("ret-on").checked };
  const r = await fetch("/api/settings", { method:"POST",
    headers:{"content-type":"application/json"}, body: JSON.stringify(body) });
  const j = await r.json().catch(() => ({}));
  $("set-msg").textContent = r.ok ? "저장됨 — 다음 틱부터" : (j.detail || "실패");
  setTimeout(() => { $("set-msg").textContent = ""; }, 4000);
}

// ── 무거운 조회는 느린 주기로 ───────────────────────────────────────────
// 인입·시계열은 file_ledger 를 훑는다. 5초마다 돌리면 데이터가 쌓일수록
// 부담이 커지므로 분리한다.
async function loadHeavy(force) {
  // 과거 날짜나 토글 OFF 에서도 **한 번은** 그려야 화면이 빈 채로 남지 않는다.
  // 주기 호출만 막는다(setInterval 쪽에서 isLive 로 거른다).
  if (!force && !isLive()) return;
  try {
    const [ing, ser] = await Promise.all([
      fetch("/api/ingest" + q({minutes:60})).then(r => r.json()),
      fetch("/api/series" + q({hours:3})).then(r => r.json()),
    ]);
    renderIngest(ing); renderChart(ser);
  } catch (e) { /* 다음 주기에 다시 */ }
}

(async () => {
  try { renderSettings(await (await fetch("/api/settings")).json()); } catch (e) {}
  loadHeavy(true);
})();

// 툴팁은 래퍼에 건다 — SVG 에 걸면 막대 사이 빈 공간에서 이벤트가 끊겨
// 커서를 가로로 움직일 때 툴팁이 깜빡인다.
$("chartwrap").addEventListener("mousemove", chartHover);
$("chartwrap").addEventListener("mouseleave", () => { $("tip").style.opacity = 0; });

// **1분마다 다시 그린다.** 축이 현재 시각 기준이라 데이터가 안 바뀌어도
// 분이 지나면 한 칸 밀려야 한다 — 안 그러면 "now" 선이 과거에 머문다.
// 축만 다시 그린다(현재 분이 오른쪽 끝에 오도록). 데이터는 loadHeavy 가 준다.
setInterval(() => { if (LAST_SERIES) renderChart(LAST_SERIES); }, 5000);

// **시간대 곡선도 같이 다시 그린다** (2026-10-16). `renderDiurnal` 은 현재 시를
// 스스로 읽지만 `renderSettings` 안에서만 불려서, 화면을 켜 둔 채 몇 시간이
// 지나도 "지금 N시"와 점이 처음 그 자리였다. 서버를 다시 부르지 않는다 —
// `DCURVE` 는 24칸 고정표라 바뀌는 것은 현재 시각뿐이다.
setInterval(() => { if (DCURVE.length) renderDiurnal(); }, 5000);

// 창 폭이 바뀌면 viewBox 를 다시 세워야 한다 — 둘 다 실제 폭에 1:1 로 맞춰
// 그리므로, 늘린 채 두면 다음 주기까지 좌표가 옛 폭에 머문다. 연속으로
// 들어오는 resize 이벤트는 마지막 것 하나로 묶는다.
let RESIZE_T = null;
window.addEventListener("resize", () => {
  clearTimeout(RESIZE_T);
  RESIZE_T = setTimeout(() => {
    if (LAST_SERIES) renderChart(LAST_SERIES);
    if (DCURVE.length) renderDiurnal();
  }, 120);
});

// **아끼지 않는다** (2026-10-15 사람 지시). 보는 사람이 한둘인 제어 화면이고
// 질의도 분 단위 요약이라, 주기를 줄여 얻는 것(반영이 즉시로 보인다)이
// 잃는 것(DB 질의 몇 번)보다 훨씬 크다. 전에는 5초/15초였다.
setInterval(poll, 1000);
setInterval(() => loadHeavy(true), 2000);
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def index():
    # 라벨 정본은 파이썬 쪽 KIND_LABELS 하나 — HTML 에 중복 정의하지 않는다
    return _HTML.replace("__KIND_LABELS__", json.dumps(KIND_LABELS, ensure_ascii=False))


# ── 기동 ────────────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="datagen.ui", description="장애 주입 제어 UI")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8009)
    ap.add_argument("--config", help="datagen.yaml 경로 (기본: config/datagen.yaml)")
    args = ap.parse_args(argv)

    if args.config:
        # **설정 전체를 들고 있는다.** `mysql:` 절만 떼어 두면 `pemdb1:`(대상 DB ·
        # enb_limit · 분당 호 수)를 못 보고, 그러면 UI 가 생성기와 다른 셀 집합을
        # 보여 준다 — 주입 대상이 생성 대상과 어긋나도 에러가 나지 않는다.
        #
        # `load_cfg` 가 기본 yaml 위에 겹치므로(2026-10-14) **얇은 환경별 파일을
        # 줘도 그 위험이 없다.** 전에는 여기서 통째로 대체해서, 컨테이너용
        # `container.yaml`(mysql.host 와 parquet root 만 적은 것)을 주면 생성
        # 모수를 전부 잃었다.
        from .cli import load_cfg
        global _CFG
        _CFG = load_cfg(args.config)

    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
