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
import collections
import json
import re
from datetime import datetime, timedelta
from pathlib import Path

import pymysql
import yaml
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from . import raw_parquet, topo
from .geo_lite import dms_to_decimal
from .generator import diurnal_factor
from .pemdb1 import BLOCKABLE_SERIES, CPLANE_ROOT_DEFAULT, CPLANE_SERIES
from .control import (FAULT_SELECT, KINDS, T_CONFIG, T_FAULT, T_HISTORY,
                      add_fault, cancel_fault, ensure_tables, get_config,
                      list_faults, set_config)

HERE = Path(__file__).resolve().parents[2]

#: kind → 운영자용 한국어 라벨. KINDS(control.py)와 1:1 — 빠진 키가 있으면
#: 셀렉트에서 영문 코드가 그대로 보여 금방 눈에 띈다.
KIND_LABELS = {
    # ── 기존 13종 ────────────────────────────────────────────────────────
    "gw_resource": "SGW 자원 소진",
    "gw_node_down": "SGW 노드 다운 (무응답)",
    "ran_radio": "무선 구간 열화",
    "mme_overload": "MME 과부하",
    "auth_failure": "인증(S6a) 거절",
    "dns_degradation": "DNS 열화",
    "external_server": "외부 서버 5xx",
    "transport_loss": "전송망 손실",
    "ue_model": "특정 단말 기종",
    "mobility": "핸드오버 이상",
    "change_suspected": "작업/변경 의심 (MME×APN)",
    "ims_pdn": "IMS PDN 이상",
    "nsa_nr_leg": "NSA NR leg 실패",
    # ── 하위 변형 ────────────────────────────────────────────────────────
    "gw_pgw_restart": "PGW 재기동 통보",
    "external_path_latency": "외부 경로 지연 (백본)",
    "nsa_policy_restrict": "DCNR 정책 제한",
    "anchor_saturation": "5G 체감 품질 저하 (앵커 경합)",
    # ── 신규 8종 ─────────────────────────────────────────────────────────
    "auc_no_response": "S6a 무응답 (TIMEOUT)",
    "dra_route_failure": "S6a 라우팅 계층 이상",
    "s6a_undetermined": "S6a 무응답 (장비 미특정)",
    "pgw_uplane_degradation": "PGW 사용자평면 저하",
    "enb_s1_outage": "eNB 전송 단절 (무호)",
    "partial_recovery": "부분 복구 (단계적 회복)",
    "apn_pdn_failure": "APN 접속 실패",
    "dns_flap": "DNS 간헐 실패",
    "compound_causal_failure": "복합 장애 (전송→DNS)",
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
    (`config/local.yaml` 처럼 `mysql.host` 만 적은 것)을 주면 `pemdb1:`
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
        # 생성기(`cli.cmd_run`)와 **같은 인자**여야 같은 집합이 나온다.
        #
        # **인자가 늘면 여기도 늘려야 한다.** 2026-10-27 에 `locale`·
        # `tac_only`·`sectors_per_enb`·`scope` 가 생겼는데 여기만 안 넘겨서
        # UI 가 **다른 셀 집합**을 봤다 — 도쿄로 돌린 뒤엔 KR 벌을 읽어
        # 0행이었다. 에러가 아니라 화면이 비는 모양이다.
        _CELLS = P.load_cells(
            conn, enb_limit=int(pem.get("enb_limit", 0)), drawable=drawable,
            locale=str(pem.get("locale", "kr")),
            tac_only=bool(pem.get("tac_only", False)),
            sectors_per_enb=pem.get("sectors_per_enb"),
            scope=P.Scope.from_cfg(pem.get("scope")))
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


@app.get("/api/region")
def api_region():
    """지역축 — **설정 파일 값 · UI 저장값 · 어긋난 자리**를 함께 내린다.

    셋을 다 보여 주는 이유: 저장값이 실제로 적용되는 것은 **재시작 뒤**라
    "지금 무엇으로 돌고 있나" 와 "다음에 무엇이 될까" 가 다를 수 있다.
    하나만 보여 주면 화면이 둘 중 하나에 대해 거짓말을 한다.
    """
    from . import region
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


@app.post("/api/region")
def api_set_region(req: RegionReq):
    """지역을 저장한다. **지금 도는 생성에는 영향이 없다.**

    `volume_pct` 처럼 다음 틱부터 반영되지 않는다 — 데몬이 기동 시 한 번만
    읽는다(`cli.apply_region_override`). 운영 중에 바꾸지 않는다는 전제
    (사용자 확정 2026-10-28)이고, 바꿔도 되게 만들면 같은 분 Parquet 에
    두 나라 IMSI 가 섞인다.
    """
    from . import region
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



# ── 마스터 토폴로지 (`/topo`) ───────────────────────────────────────────────
#
# **CLI(`python -m datagen topo`)와 같은 함수를 부른다.** 화면이 자기 SQL 을
# 쓰면 한쪽만 고쳐질 때 두 경로가 다른 마스터를 만들고, 그것은 에러가 아니라
# 값이 어긋나는 종류다(`topo.py` 머리말).
#
# **파괴적 경로는 화면이 CLI 보다 느슨하지 않다.** `rm --all` 은 CLI 와 똑같이
# `locale/<나라>` 사본을 선행 조건으로 요구하고, 여기서는 **실적재 전부**
# (`seed-enb`·`fill-coords`)에도 같은 조건을 건다 — 화면은 눌리기 쉽고
# `pemdb6` 가 그대로 운영이라 되돌릴 원본이 없다. 더 엄한 쪽으로만 다르다.
#
# ## 사본 게이트는 **KR 에만** 걸린다 (2026-10-26 접미사 벌 도입)
#
# JP 벌(`CM_GU_JP` …)은 네 단계로 **재생성된다** — 벌 만들기 → 셀 키 복사 →
# 행정구역 생성 → 좌표 재배치. 되돌릴 원본이 없는 쪽은 KR 이고, JP 에 KR
# 사본을 요구하면 **JP 를 만들기 전에는 아무 버튼도 눌리지 않는다.** 그렇게
# 선 게이트는 사람이 우회할 길을 찾게 만든다(`cli._topo_gate` 와 같은 판단).
#
# ## 관리는 **이 화면이 정본이다** (사용자 지시 2026-10-26)
#
# *"gen 스크립트로 명령 수행할 생각 없어. ui에다 새로운 화면 만들어서
# 거기서 추가 수정 삭제 관리 가능하도록 만들어줘."* 그래서 CRUD 그리드와
# JP 벌 생성 4단계가 전부 여기 있다. CLI(`python -m datagen topo`)는 같은
# 함수를 부르는 **두 번째 입구**로 남는다 — 화면이 자기 SQL 을 쓰지 않는
# 한 둘이 갈라지지 않는다.


def _topo_xw():
    """crosswalk 경로 — **`pemfill` 과 같은 키**(`pemdb1.geo_crosswalk`).

    다른 키를 보거나 기본값을 지어내면 화면이 놓은 동과 생성기가 그릴 수
    있다고 믿는 동이 다른 집합이 되고, 에러 없이 셀이 버려진다. 설정에
    없으면 `None` 이고 그 경우 `pemfill` 도 전량을 후보로 둔다.
    """
    raw = _pem_cfg().get("geo_crosswalk")
    if not raw:
        return None
    q = Path(str(raw))
    return q if q.is_absolute() else (HERE / q).resolve()


def _require_dump(conn, locale: str) -> None:
    """쓰기 전 공통 관문 — **`restore` 가 실제로 넣을 수 있는가.**

    **파일의 존재는 복귀를 뜻하지 않는다.** 전에는 `topo.dump_exists()` 가
    관문이었는데, 그때 그 JSON 을 DB 로 되넣는 코드가 없었다 — 통과 조건이
    "되돌릴 수 있다"가 아니라 "파일 4개가 존재한다"였고 그것은 안심만 준다.
    지금은 **읽어서 넣을 수 있는지**를 묻는다(`dry_run=True` 는 쓰지 않는다).

    409 로 돌려 화면이 "무엇을 먼저 해야 하는지"를 말하게 한다 — 그냥 막으면
    버튼이 안 먹는 것처럼 보인다. 실패한 테이블을 함께 실어, 넷 중 어느
    사본이 반쪽인지 사람이 알 수 있게 한다.
    """
    if locale != "kr":
        # JP 벌은 재생성 경로가 있다 (위 머리말) — KR 사본을 요구하지 않는다
        return
    rep = topo.restore(conn, locale, dry_run=True)
    if rep["ok"]:
        return
    bad = {i["table"]: i["error"] for i in rep["tables"].values() if i.get("error")}
    raise HTTPException(409, f"복귀 경로가 서 있지 않다: "
                             f"{topo.LOCALE_DIR / locale} — 먼저 `사본 뜨기`를"
                             f" 누른다 · {bad}")


@app.get("/api/topo")
def api_topo(locale: str = "kr"):
    """마스터 현황 + **시뮬레이션 선택 통과 여부**.

    `locale` 은 **어느 벌을 보는가**다(`topo.table_name`). 선택 통과
    (`pick`)는 **항상 KR 기준**이다 — 생성기가 KR 벌을 읽기 때문이고
    (`pemdb1.load_cells` 의 `locale="kr"` 기본), 창고를 다시 굽지 않는
    것이 사용자 방침이다. 그 사실을 `pick_locale` 로 적는다.

    둘을 한 응답에 담는 이유: "마스터에 행이 있다"와 "그 eNB 가 트래픽을
    받는다"는 **다른 사실**이고, 화면에서 떨어뜨려 두면 사람이 전자만 보고
    "넣었다"고 판단한다. 실측으로 3셀 eNB 는 셀 수 내림차순 22,557등이라
    `enb_limit` 축으로는 들어가지 못했다 — 지금은 `_pick_enbs` 가 대역을
    한도 밖에서 포함하므로 통과해야 하고, 통과하지 않으면 그것이 버그 신호다.
    """
    conn = _connect()
    try:
        info = topo.survey(conn, locale)
        active = info.pop("_active")
        xw = _topo_xw()
        limit = int(_pem_cfg().get("enb_limit", 0))
        # **선택 통과는 KR 기준이므로 KR crosswalk 가 맞다** — 생성기가
        # 그것을 본다(`pemfill` 과 같은 키). 아래 `no_dong` 과 다른 파일을
        # 쓰는 것이 의도다.
        pick = topo.pick_report(conn, xw, limit)
        # ── **그 로케일의 crosswalk 를 본다** (2026-10-26 결함 A)
        #
        # 전에는 설정의 단일 crosswalk(= KR 것)로 JP 구역을 판정해서, jp
        # 뷰가 *"949 구역 중 731곳에 그릴 수 있는 동이 없다"* 는 **거짓
        # 숫자**를 띄웠다. 실제로는 `CM_DONG_JP` 가 949 구역 전부에 동을
        # 갖고 있다. `_topo_xw()` 자체는 `pemfill` 과 같은 키를 봐야 하는
        # 올바른 함수이고 섞인 것은 이 호출 쪽이었다 —
        # `gu_points`/`gu_keys` 때와 같은 모양의 실수다.
        #
        # **파일이 없으면 `None`(산출 불가)이다.** 0 으로 적으면 "전부
        # 그릴 수 있다", 전수로 적으면 "전부 못 그린다"로 읽힌다 — 둘 다
        # 재지 못한 것과 다른 말이다(불변규칙 17).
        lc_xw = topo.crosswalk_path(_geo_dir(), locale) if xw else None
        drawable = (topo.drawable_dongs(lc_xw)
                    if lc_xw and lc_xw.exists() else None)
        # 벌이 아직 없으면 조회할 것이 없다 — **0 으로 적지 않는다**
        gus = (topo.gu_points(conn, locale)
               if topo.table_exists(conn, topo.table_name("gu", locale)) else [])
        no_dong = (None if drawable is None else
                   [g for _c, g, _n, _a, _o in gus if not drawable.get((_c, g))])
        gate = topo.restore(conn, "kr", dry_run=True)
        added = topo.load_added_enbs(locale)
        # ── **벌마다 행 수를 함께 내린다** (결함 B)
        #
        # 전에는 존재 여부만 실어서, kr 뷰에서 jp 다섯 줄이 `— / 있다` 로
        # 나와 **"없다"처럼 읽혔다.** 벌이 없으면 `None` 이고 0 이 아니다.
        locales = {}
        for lc in topo.GEO_SOURCES:
            arr = []
            for k in topo.LOCALE_TABLE_KEYS:
                t = topo.table_name(k, lc)
                ex = topo.table_exists(conn, t)
                arr.append({
                    "key": k, "table": t, "exists": ex,
                    "rows": (topo._rows(conn, f"SELECT COUNT(*) FROM `{t}`")[0][0]
                             if ex else None),
                })
            locales[lc] = arr
    finally:
        conn.close()
    return {
        "tables": info, "active": active, "pick": pick,
        "locale": locale, "locales": locales,
        # **원장 상태를 화면에 올린다.** 이 파일이 KR 선택에서 JP 전용
        # 장비를 빼는 근거이자 되돌림 수단인데 추적 밖(`data/`)이다 —
        # 안 보이면 지워진 것을 아무도 모른다(`topo.seed_enb_jp` 머리말).
        "added": {"enbs": len(added.get("enbs") or []),
                  "path": str(topo.added_enbs_path(locale)),
                  "band_start": added.get("band_start")},
        "locale_keys": list(topo.LOCALE_TABLE_KEYS),
        # ── **좌표 컬럼이 있는 테이블** (결함 C)
        #
        # `CL_ENB_ORG`·`CL_CELL_MAP` 에는 그 컬럼이 아예 없다. 화면이
        # `no_coord == null` 을 "산출 불가"로 찍고 있었는데, 그것은
        # **"모름"** 이고 여기 필요한 말은 **"해당 없음"** 이다. 둘을 한
        # 문자열로 뭉개면 불변규칙 17 이 지키려던 구분 자체가 무의미해진다.
        "coord_keys": list(topo.COORD_TABLE_KEYS),
        # **선택 통과는 KR 기준이다** — 생성기가 KR 벌을 읽는다
        "pick_locale": "kr",
        "enb_limit": limit, "topo_weight": topo.TOPO_WEIGHT,
        "band": list(topo.NEW_BAND),
        "crosswalk": str(xw) if xw else None,
        # 좌표가 있어 seed 대상이 되는 시군구와, 그중 **그릴 수 있는 동이
        # 없어 `DONG_ID` 를 비울 수밖에 없는** 곳. 0 으로 채우면
        # `_filter_drawable` 이 조용히 버린다(불변규칙 12·17).
        "seed_gus": len(gus),
        # `None` = 그 로케일 crosswalk 가 없어 재지 못했다 (0 과 다르다)
        "gus_without_drawable_dong": (None if no_dong is None else len(no_dong)),
        "drawable_crosswalk": str(lc_xw) if lc_xw else None,
        "drawable_crosswalk_exists": bool(lc_xw and lc_xw.exists()),
        # **`exists` 는 표시용이고 `restorable` 이 관문이다.** 둘을 함께
        # 내리는 이유: 파일은 있는데 못 읽는 상태(중간에 끊긴 gzip)를
        # 화면이 "사본 없음"으로만 말하면 사람이 또 뜬다.
        "dump": {"locale": "kr", "exists": topo.dump_exists("kr"),
                 "restorable": gate["ok"], "dir": str(topo.LOCALE_DIR / "kr"),
                 "suffix": topo.DUMP_SUFFIX,
                 "files": [{"table": i["table"], "rows": i.get("rows"),
                            "error": i.get("error")}
                           for i in gate["tables"].values()],
                 # **복구 범위를 함께 내린다** — "되돌릴 수 있다"가 실제보다
                 # 넓게 읽히면 안 된다(불변규칙 17 의 결)
                 "scope": gate["scope"],
                 "not_restored": gate["not_restored"]},
    }


class TopoReq(BaseModel):
    #: **기본이 `True` 다.** 화면에서 실행 버튼을 잘못 눌러도 세어 보기만
    #: 한다 — 실적재는 "실제로 넣기"를 명시적으로 켜야 한다.
    dry_run: bool = True
    locale: str = "kr"
    per_gu: int = 1
    cells: int = 3
    spread_km: float = 0.35
    #: `add` 용 — 넣을 컬럼. DDL 에 없는 키는 `insert_rows` 가 빼고 알린다.
    row: dict | None = None
    #: `rm` 용 — 지울 행의 키(AND). `all=True` 면 무시된다.
    key: dict | None = None
    all: bool = False
    #: `rm --all` 확인 문구. CLI 의 타이핑 확인과 같은 자리다.
    confirm: str | None = None
    target: str = "cell"
    #: `seed-geo` — 읍면동 폴리곤이 없을 때 시군구당 생성할 동 수
    dongs: int = 8
    #: `seed-geo` 는 세 테이블을 **비우고** 다시 넣는다. `geo-fetch` 는
    #: 이미 받은 파일을 다시 받는다. 둘 다 사람이 의도를 말해야 한다.
    replace: bool = False
    #: `relocate-cells` — 활성 셀이 아니라 전수. `update` 용 값
    row_only: bool = False
    #: `seed-enb-jp` — JP 전용 `ENB_ID` 대역의 시작. **기본값이 없다**:
    #: 어느 번호가 비어 있는지는 그 DB 의 사실이고, 코드에 숫자를 두면
    #: "비어 있다"는 가정이 남아 다른 DB 에서 조용히 겹친다.
    band_start: int | None = None


@app.post("/api/topo/dump")
def api_topo_dump(req: TopoReq):
    """현 마스터를 `data/pemdb1/locale/<나라>/` 로 떠낸다 — **복귀의 유일한 보장**."""
    conn = _connect()
    try:
        counts = topo.dump(conn, req.locale)
    finally:
        conn.close()
    return {"ok": True, "dir": str(topo.LOCALE_DIR / req.locale), "counts": counts}


@app.post("/api/topo/restore")
def api_topo_restore(req: TopoReq):
    """사본을 DB 로 되넣는다. **`dry_run` 기본 `True` 라 점검만 한다.**"""
    conn = _connect()
    try:
        return topo.restore(conn, req.locale, dry_run=req.dry_run)
    finally:
        conn.close()


@app.post("/api/topo/seed-enb")
def api_topo_seed(req: TopoReq):
    xw = _topo_xw()
    if xw is None:
        raise HTTPException(400, "pemdb1.geo_crosswalk 가 설정에 없다 —"
                                 " 그릴 수 있는 동을 모르는 채로 놓지 않는다")
    conn = _connect()
    try:
        if not req.dry_run:
            _require_dump(conn, req.locale)
        r = topo.seed_enb(conn, xw, per_gu=req.per_gu, cells_per_enb=req.cells,
                          spread_km=req.spread_km,
                          seed=int(_cfg()["generate"].get("seed", 20260903)),
                          dry_run=req.dry_run)
        # 놓은 eNB 가 **실제로 선택되는지** 같은 응답에 담는다. 떨어뜨려
        # 두면 "행은 생겼는데 트래픽이 0" 인 상태가 화면에서 안 보인다.
        pick = topo.pick_report(conn, xw, int(_pem_cfg().get("enb_limit", 0)),
                                extra_cells=r["keys"]) if r["keys"] else None
    finally:
        conn.close()
    r.pop("keys", None)
    return {"ok": True, "dry_run": req.dry_run, "result": r, "pick": pick}


@app.post("/api/topo/fill-coords")
def api_topo_fill(req: TopoReq):
    """좌표 결손 **점검** — 적재 경로가 아니다 (2026-10-26).

    적재는 생성기가 한다 — `cmd_run`·`cmd_pemfill` 이
    `load_cells(ensure_coords=True)` 로 매 틱 채운다
    (`topo.ensure_sector_coords`). 화면에 버튼으로 두면 사람이 눌러야 하고,
    **적재 경로가 둘이면 어느 쪽이 넣었는지 모른다.**

    세어 주는 것이 둘이다 — 좌표 있는 eNB 의 **섹터 결손**(생성기가 채운다)
    과 **좌표 없는 eNB**(`_pick_enbs` 가 불변규칙 12 검증용으로 일부러
    남긴다 — 채우지 않는다). 섞어 보면 "결손 804" 로 읽힌다.
    """
    conn = _connect()
    try:
        # 생성기와 **같은 인자**다 — crosswalk·`enb_limit` 를 빼면 후보
        # 집합이 달라져 점검이 다른 수를 말한다(실측 768/36 대 696/108).
        r = topo.fill_coords(conn, _topo_xw(),
                             int(_pem_cfg().get("enb_limit", 0)))
    finally:
        conn.close()
    return {"ok": True, "check_only": True, "result": r}


# ── locale 벌 — **4단계**. 순서가 전제다 ────────────────────────────────────
#
#   1  ensure-tables   CREATE TABLE … LIKE 로 벌 넷을 만든다 (DDL)
#   2  copy-cells      셀 키·지역 참조를 DB 안에서 복사 (좌표는 NULL)
#   3  code-map        JP 경계 코드 → KR 코드 맵 (DB 의 CM_GU 가 입력)
#   4  seed-geo        행정구역 셋을 경계 GeoJSON 에서 만든다
#   5  relocate-cells  좌표·RU_NAME 을 그 나라로 쓴다
#
# **순서를 바꾸면 조용히 빈다.** `seed-geo` 가 `code-map` 없이 돌면 코드
# 대응이 없어 전량 제외되고, `relocate-cells` 가 `seed-geo` 전에 돌면
# `CM_GU_JP` 에 좌표가 없어 멈춘다(그쪽은 에러가 나므로 안전한 편이다).


def _geo_dir():
    """경계 GeoJSON 디렉터리 — **CLI 와 같은 함수를 부른다.**

    `cli._topo_geo_dir` 가 `pemdb1.geo_crosswalk` 의 디렉터리를 쓴다. 여기서
    따로 조립하면 화면이 받은 파일과 커맨드가 읽는 파일이 갈라진다.
    """
    from .cli import _topo_geo_dir

    return _topo_geo_dir(_cfg())


@app.post("/api/topo/ensure-tables")
def api_topo_ensure(req: TopoReq):
    """지역 테이블 넷을 `CREATE TABLE … LIKE` 로 만든다 — **1단계**.

    `dry_run` 기본이 `True` 라 무엇을 만들지만 말한다. collation·인덱스·
    타입이 원본에서 그대로 오는 것이 `LIKE` 를 쓰는 이유다(불변규칙 14).
    """
    conn = _connect()
    try:
        return {"ok": True, "dry_run": req.dry_run,
                "result": topo.ensure_locale_tables(
                    conn, req.locale, write=not req.dry_run)}
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    finally:
        conn.close()


@app.post("/api/topo/copy-cells")
def api_topo_copy_cells(req: TopoReq):
    """셀 키·지역 참조를 DB 안에서 복사한다 — **2단계**. 좌표·이름은 NULL."""
    conn = _connect()
    try:
        return {"ok": True, "dry_run": req.dry_run,
                "result": topo.copy_cells(conn, req.locale,
                                          write=not req.dry_run)}
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    finally:
        conn.close()


@app.post("/api/topo/copy-enb-org")
def api_topo_copy_enb_org(req: TopoReq):
    """eNB 조직 벌 복사 — **2b 단계**. `BTS_NAME` 만 NULL 로 남긴다.

    안 만들면 JP 지도에서 셀 이름은 일본어인데 eNB 이름은 한국어로 뜬다 —
    라벨이 `RU_NAME` → `BTS_NAME` 순으로 떨어지기 때문이다.
    """
    conn = _connect()
    try:
        return {"ok": True, "dry_run": req.dry_run,
                "result": topo.copy_enb_org(conn, req.locale,
                                            write=not req.dry_run)}
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    finally:
        conn.close()


@app.post("/api/topo/seed-enb-jp")
def api_topo_seed_enb_jp(req: TopoReq):
    """추가 구역에 장비를 놓는다 — **6단계**. KR 선택에서 빠지는 장비다.

    `CL_CELL_MAP` 에 행이 들어간다 — **KR 과 공유이고 `DUMP_TABLES` 밖**
    이라 사본이 없다. 그래서 원장(`topo.ADDED_FILE`)을 먼저 쓰고, 그 파일이
    KR 선택에서 빼는 근거이자 되돌림 수단이 된다(`rm-added`).
    """
    if req.band_start is None:
        raise HTTPException(400,
                            "ENB_ID 대역 시작 번호가 필요하다 — 빈 대역을 "
                            "실측해서 넣는다. 기본값을 두지 않는 이유: 어느 "
                            "번호가 비어 있는지는 그 DB 의 사실이고, 가정을 "
                            "코드에 남기면 다른 DB 에서 조용히 겹친다")
    conn = _connect()
    try:
        return {"ok": True, "dry_run": req.dry_run,
                "result": topo.seed_enb_jp(
                    conn, req.locale, int(req.band_start),
                    per_gu=req.per_gu, cells_per_enb=req.cells,
                    spread_km=req.spread_km,
                    seed=int(_cfg()["generate"].get("seed", 20260903)),
                    write=not req.dry_run)}
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    finally:
        conn.close()


@app.post("/api/topo/rm-added")
def api_topo_rm_added(req: TopoReq):
    """원장에 적힌 JP 전용 eNB 를 세 테이블에서 지운다 — `CL_CELL_MAP` 되돌림."""
    conn = _connect()
    try:
        return {"ok": True, "dry_run": req.dry_run,
                "result": topo.remove_added_enbs(conn, req.locale,
                                                 write=not req.dry_run)}
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    finally:
        conn.close()


@app.post("/api/topo/geo-fetch")
def api_topo_geo_fetch(req: TopoReq):
    """경계 GeoJSON 을 받는다. **없으면 무엇이 없는지 말하며 멈춘다.**

    빈 테이블을 만들어 두면 화면이 조용히 빈다 — 그래서 `seed-geo` 전에
    이것이 서야 한다. 크레딧(`SOURCE.txt`)을 파일과 함께 쓴다(번들
    디렉터리가 추적 밖이라 저장소에 적어 두면 반출본에서 사라진다).
    """
    try:
        if req.dry_run:
            # **세어 보기는 내려받지 않는다.** 무엇이 없는지만 말한다 —
            # 폐쇄망에서 버튼 하나가 수십 MB 를 끌어오면 안 된다.
            gd = _geo_dir()
            have = []
            for lv in [k for k in topo.GEO_SOURCES[req.locale]
                       if not k.startswith("_")]:
                dest = topo.geo_level_path(gd, req.locale, lv)
                have.append({"level": lv, "file": dest.name,
                             "exists": dest.exists(),
                             "bytes": dest.stat().st_size if dest.exists() else None})
            return {"ok": True, "dry_run": True, "files": have,
                    "dir": str(topo.geo_level_path(gd, req.locale, "sgg").parent),
                    "note": "받지 않았다 — 실행을 누르면 없는 것만 받는다"}
        got = topo.fetch_boundaries(_geo_dir(), req.locale, force=req.replace)
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    except OSError as e:      # 폐쇄망·DNS — "받을 수 없다"를 그대로 말한다
        raise HTTPException(502, f"경계 파일을 받을 수 없다: {e}")
    return {"ok": True, "files": got,
            "dir": str(topo.geo_level_path(_geo_dir(), req.locale, "sgg").parent)}


@app.post("/api/topo/code-map")
def api_topo_code_map(req: TopoReq):
    """JP 경계 코드 → **KR 코드** 맵 — 3단계.

    입력은 **DB 의 `(CITY_ID, GU_ID)`** 다. 번들에서 유도하지 않는다 —
    트래픽이 참조하는 것은 DB 의 코드이고, 번들과 DB 가 다르면 전환 후
    그 차이만큼 조용히 미매핑이 된다.
    """
    if req.locale == "kr":
        raise HTTPException(400, "코드 맵은 KR 코드를 **받는** 쪽이 만든다 —"
                                 " locale kr 은 대상이 아니다")
    conn = _connect()
    try:
        kr_gu = [(int(c), int(g)) for c, g in topo._rows(
            conn, f"SELECT CITY_ID, GU_ID "
                  f"FROM `{topo.table_name('gu', 'kr')}` ORDER BY GU_ID")]
        doc = topo.build_jp_code_map(_geo_dir(), kr_gu)
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    finally:
        conn.close()
    out = {"ok": True, "dry_run": req.dry_run, "kr_gu": len(kr_gu),
           "sido": len(doc["sido"]), "sgg": len(doc["sgg"]),
           "unused_prefectures": doc["unused_prefectures"],
           "sample": {str(c): rows[:2] for c, rows in
                      sorted(doc["picked"].items())[:3]}}
    if not req.dry_run:
        out["path"] = str(topo.save_code_map(doc, req.locale))
    return out


@app.post("/api/topo/seed-geo")
def api_topo_seed_geo(req: TopoReq):
    """행정구역 셋을 경계 GeoJSON 에서 만든다 — **4단계**.

    `replace` 는 세 테이블을 **비우고** 다시 넣는다. KR 대상이면 사본
    게이트가 걸린다(`_require_dump`) — 44만 셀이 참조하는 코드를 덮는 일이다.
    """
    conn = _connect()
    try:
        if not req.dry_run and req.replace:
            _require_dump(conn, req.locale)
        r = topo.seed_geo(conn, _geo_dir(), locale=req.locale,
                          dongs_per_gu=req.dongs, spread_km=3.0,
                          seed=int(_cfg()["generate"].get("seed", 20260903)),
                          replace=req.replace, dry_run=req.dry_run)
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    finally:
        conn.close()
    return {"ok": True, "dry_run": req.dry_run, "result": r}


@app.post("/api/topo/relocate-cells")
def api_topo_relocate(req: TopoReq):
    """셀 좌표와 `RU_NAME` 을 그 나라로 다시 쓴다 — **5단계**.

    **키를 바꾸지 않는다** — `UPDATE` 만 한다. `all=False`(기본)이면 1분
    집계에 나타나는 활성 셀만이고, `all=True` 면 44만행 전부다. 비(非)KR 에서
    `CL_ENB_ORG.BTS_NAME` 은 **건드리지 않는다**(한 벌 공유 — 응답의
    `org_untouched_reason` 이 그 사실을 말한다).
    """
    conn = _connect()
    try:
        if not req.dry_run:
            _require_dump(conn, req.locale)
        r = topo.relocate_cells(conn, locale=req.locale,
                                spread_km=req.spread_km,
                                seed=int(_cfg()["generate"].get("seed", 20260903)),
                                only_active=not req.all, dry_run=req.dry_run)
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    finally:
        conn.close()
    return {"ok": True, "dry_run": req.dry_run, "result": r}


# ── 행 CRUD 그리드 ─────────────────────────────────────────────────────────


@app.get("/api/topo/rows")
def api_topo_rows(table: str = "gu", locale: str = "kr", q: str = "",
                  limit: int = 50, offset: int = 0):
    """행 조회 — 검색·페이지. **전체 수를 함께 내린다.**

    `CM_DONG` 7,847 · 셀 44만을 통째로 내리면 화면이 멈춘다. `total` 을
    주어 화면이 페이지 수를 추측하지 않게 한다 — 추측하면 마지막 페이지가
    비어 "데이터가 없다"로 보인다.
    """
    if table not in topo.TABLES:
        raise HTTPException(400, f"대상은 {', '.join(topo.TABLES)} 다")
    conn = _connect()
    try:
        return topo.browse(conn, table, locale, q, limit, offset)
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    finally:
        conn.close()


@app.post("/api/topo/update")
def api_topo_update(req: TopoReq):
    """한 행의 **키가 아닌 컬럼**을 고친다.

    키 컬럼은 받지 않는다 — 1분 집계·원시 Parquet·`ground_truth.json`·
    `CL_CELL_MAP` 이 그 키를 참조하고, 화면에서 바꾸면 그 행만 옮겨지고
    나머지는 **에러 없이** 끊어진다(`topo.update_row` 머리말).
    """
    if req.target not in topo.TABLES:
        raise HTTPException(400, f"대상은 {', '.join(topo.TABLES)} 다")
    if not req.key:
        raise HTTPException(400, "고칠 행의 키가 필요하다")
    if not req.row:
        raise HTTPException(400, "고칠 컬럼이 비어 있다")
    conn = _connect()
    try:
        _require_dump(conn, req.locale)
        return {"ok": True, **topo.update_row(conn, req.target, req.locale,
                                              req.key, req.row)}
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    finally:
        conn.close()


@app.post("/api/topo/add")
def api_topo_add(req: TopoReq):
    if req.target not in topo.TABLES:
        raise HTTPException(400, f"대상은 {', '.join(topo.TABLES)} 다")
    if not req.row:
        raise HTTPException(400, "넣을 컬럼이 비어 있다")
    # **대상 테이블은 로케일이 정한다** — `gu` + `jp` = `CM_GU_JP`
    try:
        table = topo.table_name(req.target, req.locale)
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    conn = _connect()
    try:
        _require_dump(conn, req.locale)
        n = topo.insert_rows(conn, table, [req.row])
    finally:
        conn.close()
    return {"ok": True, "table": table, "rows": n}


@app.post("/api/topo/rm")
def api_topo_rm(req: TopoReq):
    if req.target not in topo.TABLES:
        raise HTTPException(400, f"대상은 {', '.join(topo.TABLES)} 다")
    try:
        table = topo.table_name(req.target, req.locale)
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    keys = topo.TABLES[req.target][1]
    if req.all and not topo.wipeable(req.target, req.locale):
        # **`cellmap` 은 받지 않는다** — `topo` 는 위치 마스터를 다루고
        # `CL_CELL_MAP` 574,404행은 **셀 대장**이다. 전량 삭제할 이유가 없고
        # `DUMP_TABLES` 에도 없어서 지우면 되돌릴 원본이 아예 없다. 전에는
        # 관문이 `CM_*` 네 파일만 보고 이 삭제를 통과시켰다.
        raise HTTPException(400, f"{table} 는 전체 삭제 대상이 아니다 — 셀 "
                                 "대장을 통째로 지울 이유가 없고 복귀 사본도 "
                                 "없다. 단건 삭제는 된다")
    conn = _connect()
    try:
        _require_dump(conn, req.locale)
        if req.all:
            # CLI 와 **같은 문구**를 요구한다. 화면의 확인 입력을 생략하면
            # 버튼 하나로 수십만 행이 사라진다.
            want = f"{table} 전체 삭제"
            if (req.confirm or "").strip() != want:
                raise HTTPException(400, f"확인 문구가 다르다 — '{want}' 를 "
                                         "그대로 입력한다")
            return {"ok": True, "table": table, "deleted": topo.wipe(conn, table)}
        if not req.key:
            raise HTTPException(400, f"지울 행의 키가 필요하다: {', '.join(keys)}")
        where = " AND ".join(f"`{k}` = %s" for k in req.key)
        with conn.cursor() as cur:
            n = cur.execute(f"DELETE FROM `{table}` WHERE {where}",
                            list(req.key.values()))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "table": table, "deleted": n}

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
<h1>probe-datagen 장애 주입 제어<small>생성 데몬이 1분 틱으로 반영한다 · <a href="/topo" style="color:var(--accent)">마스터 토폴로지</a></small></h1>

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
    <!-- ── 지역축 ──────────────────────────────────────────────────────
         **다른 설정과 성격이 다르다.** 여기 나머지는 즉시이거나 다음
         틱부터인데 지역은 **재시작 뒤**다 — 데몬이 기동 시 한 번만 읽는다
         (`cli.apply_region_override`). 운영 중에 바꾸지 않는다는 전제이고,
         바꿔도 되게 만들면 같은 분 Parquet 에 한국·일본 IMSI 가 섞여 그
         분이 **어느 쪽으로도 집계되지 않는다.**

         그래서 맨 위에 두고 저장 버튼을 따로 준다 — 아래 "설정 저장" 에
         묶이면 생성량을 조절하다가 지역까지 함께 저장된다. -->
    <div class="sect" style="margin-top:0">
      <h3>지역 <small>— 재시작 후 반영</small></h3>
      <div class="row">
        <select id="region-sel" style="min-width:160px"></select>
        <button type="button" onclick="saveRegion()">지역 저장</button>
        <span class="dim" id="region-msg"></span>
      </div>
      <div class="dim" id="region-note" style="margin-top:8px; font-size:12px;
           line-height:1.6"></div>
    </div>
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
        <span id="pct-msg" class="dim" style="margin-left:8px"></span>
      </label>
      <!-- `value` 는 **로드 전에 잠깐 보이는 자리**다. 정본은 서버의
           `volume_pct` 이고 `renderSettings` 가 덮는다. -->
      <input id="pct" type="range" min="0" max="100" step="5" value="70"
             style="width:100%; margin:6px 0"
             oninput="document.getElementById('pct-label').textContent=this.value;
                      renderApplied(Number(this.value))"
             onchange="savePct(Number(this.value))">
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
    + og("eNB (무호 전용)", (t.enb || []).map(x => ({v:`ENB:${x.id}`, t:`eNB-${x.id} (셀 ${x.cells})`})))
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

// ── 지역축 ─────────────────────────────────────────────────────────────
// **저장해도 지금 도는 생성은 안 바뀐다.** 다른 설정과 다른 자리이므로
// 화면이 그 사실을 먼저 말한다 — "저장했습니다"만 띄우면 운영자는 반영된
// 줄 알고, 그 오해는 IMSI 를 들여다봐야 풀린다.
let REGION = null;
async function loadRegion() {
  try { REGION = await (await fetch("/api/region")).json(); }
  catch (e) { return; }
  const sel = $("region-sel");
  // **목록은 서버가 준다**(`region.LOCALES`). 화면이 자기 목록을 들면
  // 지역이 늘 때 한쪽만 고쳐지고, 그때 "선택지가 없다"가 아니라
  // **선택해도 안 먹는다**가 된다.
  sel.innerHTML = (REGION.locales || []).map(l =>
    `<option value="${esc(l.value)}">${esc(l.label)}</option>`).join("");
  sel.value = REGION.pending || REGION.file;
  renderRegionNote();
}
function renderRegionNote() {
  if (!REGION) return;
  const sel = $("region-sel").value;
  const file = REGION.file, saved = REGION.saved, pend = REGION.pending;
  const L = {};
  (REGION.locales || []).forEach(l => { L[l.value] = l.label; });
  const out = [];
  // ① 지금 이 프로세스가 들고 있는 값 — 데몬과 같을 수도 다를 수도 있다
  out.push(`설정 파일 <b>${esc(L[file] || file)}</b>`
         + (saved ? ` · UI 저장값 <b>${esc(L[saved] || saved)}</b>` : "")
         + ` → 재시작하면 <b>${esc(L[pend] || pend)}</b>`);
  out.push(`PLMN <code>${esc(REGION.plmn.home || "?")}</code>`
         + ` · IMSI 접두 <code>${esc((REGION.plmn.home||"") + (REGION.plmn.imsi_msin_head||""))}</code>`
         + ` · 마스터 <code>${esc(REGION.pemdb1_locale || "?")}</code>`);
  // ② 고르기만 하고 아직 저장 안 한 상태를 구분해 적는다
  if (sel !== pend)
    out.push(`<span style="color:var(--warn,#e8a33d)">고른 값 <b>${esc(L[sel]||sel)}</b>`
           + ` — 아직 저장하지 않았다</span>`);
  // ③ **어긋남을 숨기지 않는다.** 사람이 yaml 에 적은 칸은 지역을 바꿔도
  //    따라오지 않는다 — 그걸 모르면 "바꿨는데 왜 그대로냐"가 된다.
  const cf = REGION.conflicts || [];
  if (cf.length) {
    out.push(`<span style="color:var(--warn,#e8a33d)">⚠ 설정 파일에 적힌 값이`
           + ` 있어 지역을 바꿔도 따라오지 않는다 —</span>`);
    cf.forEach(c => out.push(`&nbsp;&nbsp;<code>${esc(c.path)}</code> = `
      + `<b>${esc(c.configured)}</b> (${esc(c.locale)} 이면 ${esc(c.expected)})`));
    out.push(`&nbsp;&nbsp;<span class="dim">따라오게 하려면 datagen.yaml 의 그 칸을 지운다</span>`);
  }
  out.push(`<span class="dim">운영 중에는 바꾸지 않는다 — 같은 분에 두 나라`
         + ` IMSI 가 섞이면 그 분은 어느 쪽으로도 집계되지 않는다</span>`);
  $("region-note").innerHTML = out.join("<br>");
}
async function saveRegion() {
  const loc = $("region-sel").value;
  const msg = $("region-msg");
  msg.textContent = "저장 중…";
  try {
    const r = await fetch("/api/region", { method:"POST",
      headers:{"Content-Type":"application/json"},
      body: JSON.stringify({ locale: loc }) });
    if (!r.ok) { msg.textContent = "실패 — " + (await r.text()).slice(0, 120); return; }
    // **"저장됨" 으로 끝내지 않는다.** 반영 시점을 함께 적는다.
    msg.innerHTML = `<span style="color:var(--warn,#e8a33d)">저장됨 —`
                  + ` <b>재시작해야 반영된다</b></span>`;
    await loadRegion();
  } catch (e) { msg.textContent = "실패 — " + e; }
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

// **슬라이더가 스스로 저장한다**(2026-10-27). 전에는 `oninput` 이 라벨과
// 미리보기만 바꾸고 실제 저장은 **보존 섹션의 `설정 저장`** 버튼이 했다.
// 그 버튼이 보존 전용으로 보이는 자리에 있어서, 생성량을 조절하고 누르지
// 않으면 **화면만 바뀌고 서버 값은 그대로**였다 — 다음 기동에 원래 값으로
// 돌아와 "바꾼 적이 없는" 것처럼 보인다. 실제로 그 혼란을 겪었다.
//
// **`volume_pct` 하나만 보낸다.** `POST /api/settings` 는 `is not None` 으로
// 부분 갱신하므로, 조절 중에 다른 창에서 바꾼 보존·재배정을 이 화면의 낡은
// 값으로 되돌리지 않는다(`saveReassign` 과 같은 이유).
//
// `onchange` 다 — 드래그 중(`oninput`)마다 보내면 한 번 조절에 수십 번
// 쓴다. 놓는 순간 한 번이면 된다.
async function savePct(v) {
  const el = $("pct-msg");
  try {
    const r = await fetch("/api/settings", { method:"POST",
      headers:{"content-type":"application/json"},
      body: JSON.stringify({ volume_pct: v }) });
    const j = await r.json().catch(() => ({}));
    el.textContent = r.ok ? "저장됨 — 다음 틱부터" : ("실패 — " + (j.detail || r.status));
    el.style.color = r.ok ? "" : "var(--red)";
  } catch (e) {
    el.textContent = "실패 — " + e;
    el.style.color = "var(--red)";
  }
  setTimeout(() => { el.textContent = ""; el.style.color = ""; }, 4000);
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
  // 지역축은 **주기 폴링에 넣지 않는다** — 재시작 전에는 바뀌지 않는 값이라
  // 1초마다 물으면 질의만 늘고 화면은 그대로다. 저장할 때 다시 읽는다.
  loadRegion();
  loadHeavy(true);
})();

$("region-sel").addEventListener("change", renderRegionNote);

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



# ── 마스터 토폴로지 화면 (`/topo`) ──────────────────────────────────────────
#
# **별도 페이지다** (사람 지시 2026-10-26: "기존 UI 에 별도 화면으로 제어").
# 장애 주입 화면과 섞지 않는 이유: 그쪽은 **상태를 주입**하고(데몬이 1분마다
# 읽어 되돌린다) 이쪽은 **마스터를 영구히 바꾼다.** 같은 화면에 두면 "취소"가
# 한쪽에만 있다는 것이 보이지 않는다.
_HTML_TOPO = """<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<title>probe-datagen 마스터 토폴로지</title>
<style>
  :root { --bg:#14181f; --panel:#1d242e; --line:#2c3542; --fg:#d7dde6;
          --dim:#8a94a3; --accent:#4e9af1; --red:#e5534b; --yellow:#d8a22a;
          --green:#4caf7d; }
  * { box-sizing:border-box; }
  body { margin:0; padding:20px; background:var(--bg); color:var(--fg);
         font:14px/1.5 "Pretendard","Apple SD Gothic Neo","Malgun Gothic",sans-serif; }
  a { color:var(--accent); }
  h1 { font-size:17px; margin:0 0 14px; }
  h1 small { color:var(--dim); font-weight:normal; margin-left:8px; }
  h2 { font-size:14px; margin:0 0 10px; }
  h3 { margin:0 0 8px; font-size:13px; font-weight:600; }
  h3 small, h2 small { font-weight:400; color:var(--dim); margin-left:4px; }
  .panel { background:var(--panel); border:1px solid var(--line); border-radius:8px;
           padding:14px; margin-bottom:14px; }
  .grid2 { display:grid; grid-template-columns:1fr 1fr; gap:14px; }
  @media (max-width:980px) { .grid2 { grid-template-columns:1fr; } }
  .sect { margin-top:16px; border-top:1px solid var(--line); padding-top:14px; }
  .row { display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
  .dim { color:var(--dim); }
  table { width:100%; border-collapse:collapse; font-size:13px; }
  th, td { text-align:left; padding:5px 6px; border-bottom:1px solid var(--line); }
  th { color:var(--dim); font-weight:600; }
  td.n, th.n { text-align:right; font-variant-numeric:tabular-nums; }
  button { background:var(--accent); color:#0d1117; border:0; border-radius:6px;
           padding:6px 12px; font-weight:600; cursor:pointer; }
  button.ghost { background:var(--panel); color:var(--fg);
                 border:1px solid var(--line); font-weight:400; }
  button.danger { background:var(--red); color:#fff; }
  button:disabled { opacity:.4; cursor:default; }
  input, select { background:#121720; color:var(--fg); border:1px solid var(--line);
                  border-radius:5px; padding:5px 7px; }
  .ok { color:var(--green); } .bad { color:var(--red); } .warn { color:var(--yellow); }
  pre { background:#121720; border:1px solid var(--line); border-radius:6px;
        padding:10px; overflow:auto; font-size:12px; white-space:pre-wrap;
        max-height:280px; }
</style>
</head>
<body>
<h1>마스터 토폴로지
  <small>행정구역 · 장비 · 셀 — <a href="/">장애 주입 화면</a></small>
  <!-- **로케일은 테이블 이름이다.** 어느 벌을 보고 있는지가 모든 조작의
       대상이므로 화면 맨 위에 둔다 — 아래에 묻어 두면 KR 을 보면서 JP 를
       고친다고 믿는 상태가 생긴다. -->
  <span style="float:right; font-size:13px; font-weight:400">
    보는 벌 <select id="locale" onchange="switchLocale()"></select>
    <span id="locale-tag" class="dim"></span>
  </span>
</h1>

<!-- **경고를 화면 맨 위에 둔다.** 여기서 넣는 행은 데몬이 되돌리지 않는다 —
     장애 주입과 가장 다른 점이고, 아래 버튼을 누르기 전에 읽혀야 한다. -->
<div class="panel" style="border-color:var(--yellow)">
  <b class="warn">이 화면의 쓰기는 되돌아오지 않는다.</b>
  <span class="dim">장애 주입은 데몬이 1분마다 읽어 끝내지만, 마스터는 영구히
  바뀐다. <code>pemdb6</code> 가 그대로 운영이라 원본이 따로 없다 —
  <b>실적재·삭제는 아래 사본이 있어야 눌린다.</b></span>
</div>

<div class="grid2">
  <div class="panel">
    <h2>마스터 현황 <small>좌표 없음은 0 이 아니라 모름이다 ·
      <span id="tbl-locale" class="dim"></span></small></h2>
    <table>
      <thead><tr><th>테이블</th><th class="n">행</th><th class="n">좌표 없음</th></tr></thead>
      <tbody id="tbl"><tr><td colspan="3" class="dim">불러오는 중…</td></tr></tbody>
    </table>
    <div class="sect">
      <h3>활성 셀 <small>— 1분 집계에 실제로 나타나는 것</small></h3>
      <div id="active" class="dim">불러오는 중…</div>
    </div>
  </div>

  <div class="panel">
    <h2>시뮬레이션 선택 <small>마스터에 있는 것과 트래픽을 받는 것은 다르다</small></h2>
    <table>
      <thead><tr><th>대역</th><th class="n">마스터</th><th class="n">선택</th><th>통과</th></tr></thead>
      <tbody id="pick"><tr><td colspan="4" class="dim">불러오는 중…</td></tr></tbody>
    </table>
    <div id="pick-note" class="dim" style="margin-top:10px; font-size:12px"></div>
  </div>
</div>

<div class="grid2">
  <div class="panel">
    <h2>복귀 사본 <small>— 파괴적 조작의 선행 조건</small></h2>
    <div id="dump" class="dim">불러오는 중…</div>
    <div id="dump-scope" class="dim" style="font-size:12px; margin-top:6px"></div>
    <div class="row" style="margin-top:10px">
      <button onclick="run('dump', {})">사본 뜨기</button>
      <button class="ghost" onclick="run('restore', {dry_run:true})">복귀 점검</button>
      <button class="danger" id="rs-go" disabled
              onclick="run('restore', {dry_run:false})">사본으로 되돌리기</button>
    </div>
    <div class="dim" style="font-size:12px; margin-top:6px">
      CM_CITY · CM_GU · CM_DONG · CM_CELL_REP_RU_LOC 를
      <code>.json.gz</code> 로 떠낸다 (<code>CL_CELL_MAP</code> 은 좌표가
      없어 제외 — 그래서 <b>전체 삭제 대상도 아니다</b>).
      <b>파일이 있다는 것과 되돌릴 수 있다는 것은 다르다</b> — 아래 잠금은
      "읽어서 넣을 수 있는가"(<code>restore</code> 점검)를 본다.
    </div>

    <div class="sect">
      <h3>좌표 결손 <small>— 적재는 생성기가 한다. 여기서는 센다</small></h3>
      <div class="dim" style="font-size:12px; margin-bottom:8px">
        <b>버튼이 없는 것이 의도다.</b> 좌표 생성은
        <code>load_cells(ensure_coords=True)</code> 가 <b>매 틱</b> 한다
        (<code>datagen run</code> · <code>pemfill</code>) — 사람이 기억해서
        치는 커맨드로 두면 현장에서 아무도 치지 않고, 적재 경로가 둘이면
        어느 쪽이 넣었는지 모른다.
        <br>아래 둘을 <b>섞어 보지 않는다</b> — "좌표 없는 eNB" 는 결손이
        아니라 <code>_pick_enbs</code> 가 <b>불변규칙 12</b>(H-07 · UNMAPPED
        패널) 검증용으로 일부러 남기는 것이고 <b>채우지 않는다</b>.
      </div>
      <div class="row">
        <button class="ghost" onclick="run('fill-coords', {})">결손 세어 보기</button>
      </div>
    </div>
  </div>

  <div class="panel">
    <h2>장비 놓기 <small>— 시군구마다 eNB 하나</small></h2>
    <div class="row">
      <label class="dim" style="font-size:12px">시군구당 eNB
        <input id="se-pergu" type="number" min="1" max="2" value="1" style="width:56px"></label>
      <label class="dim" style="font-size:12px">eNB 당 셀
        <input id="se-cells" type="number" min="1" max="36" value="3" style="width:56px"></label>
      <button class="ghost" onclick="run('seed-enb', {dry_run:true})">세어 보기</button>
      <button class="danger" id="se-go" disabled
              onclick="run('seed-enb', {dry_run:false})">실제로 넣기</button>
    </div>
    <div class="dim" style="font-size:12px; margin-top:8px">
      <code>ENB_ID = 800000 + GU_ID</code> — 기존과 겹치지 않는 대역이고 ID 만 보고
      어느 시군구인지 알 수 있다. <b>셀 수를 늘려 선택 규칙을 통과시키려 하지
      않는다</b> — 그것은 자산 대장을 거짓으로 만든다. 대역은
      <code>enb_limit</code> 과 무관하게 포함된다.
    </div>

    <div class="sect">
      <h3>행 관리 <small>— 아래 <b>행 관리</b> 패널에서 한다</small></h3>
      <div class="dim" style="font-size:12px">
        추가 · 수정 · 삭제는 조회 그리드와 **같은 자리**에 둔다. 떨어뜨려
        두면 어느 행을 고치는지 보지 않고 키를 손으로 적게 되고, 그렇게
        틀린 키는 <b>0행 수정이 "성공"으로</b> 돌아온다.
      </div>
    </div>
  </div>
</div>

<!-- ── 로케일 벌 ─────────────────────────────────────────────────────────
     **순서가 전제다.** 1→5 를 건너뛰면 조용히 빈다 — `seed-geo` 가 코드
     맵 없이 돌면 전량 제외되고, 그 상태는 "일본 폴리곤이 비었다"로만
     보인다. 그래서 단계를 번호와 함께 한 줄씩 세워 둔다. -->
<div class="panel">
  <h2>로케일 벌 <small>— 지역 테이블만 나라별로 만든다. 나머지는 한 벌 공유다</small></h2>
  <div class="dim" style="font-size:12px; margin-bottom:10px">
    <code>CM_CITY_JP</code> · <code>CM_GU_JP</code> · <code>CM_DONG_JP</code> ·
    <code>CM_CELL_REP_RU_LOC_JP</code> · <code>CL_ENB_ORG_JP</code> 다섯을 만든다.
    <code>ENB_ID</code>·<code>SECTOR</code>·<code>CITY_ID</code>·
    <code>GU_ID</code>·<code>DONG_ID</code>·조직 ID 는 <b>KR 값을 그대로
    복사</b>한다 — ID 체계를 공유하기로 한 결정이 여기서 지켜진다.
    <code>PM_*</code>·<code>AI_*</code>·권역 테이블(<code>CM_BRANCH</code>·
    <code>CM_LAREA</code>·<code>CM_REGION</code>)은 <b>한 벌 공유</b>라
    <b>건드리지 않는다</b>.
    <br><b>셀 벌을 함께 만드는 이유:</b> 지역 셋만 바꾸면 조인은 되고 에러도
    없지만 <b>좌표가 셀 쪽에 있다</b> — 일본 지명에 한국 좌표가 붙어
    폴리곤이 비어 보이고 <code>geo_pattern</code> 반경이 한국에서 계산된다.
    <br><b><code>CL_ENB_ORG</code> 를 벌로 뜨는 이유:</b> 지도 라벨이
    <code>RU_NAME</code> → <code>BTS_NAME</code> 순으로 떨어져, 없으면 셀
    27.6% 가 한국 eNB 이름으로 뜬다. 공유 한 벌을 덮어 해결할 수는 없다 —
    <code>dump</code> 대상이 아니라 <b>KR 이 되돌아오지 않는다</b>.
    <br><b><code>CL_CELL_MAP</code> 은 벌을 뜨지 않는다</b> — 셀 키의 정본이고
    나라와 무관하다. 다만 8번(없는 장비 추가)은 <b>그 공유 테이블에 행을
    넣는다</b>: 유일하게 사본으로 되돌릴 수 없는 자리이고, 그래서 원장
    파일이 되돌림을 맡는다.
  </div>
  <table>
    <thead><tr><th>벌</th><th>테이블</th><th class="n">행</th><th>상태</th></tr></thead>
    <tbody id="sets"><tr><td colspan="4" class="dim">불러오는 중…</td></tr></tbody>
  </table>
  <div id="added" class="dim" style="font-size:12px; margin-top:8px"></div>
  <div class="sect">
    <h3>단계 <small>— 번호 순서를 건너뛰면 조용히 빈다</small></h3>
    <div id="steps"></div>
    <div class="row" style="margin-top:10px">
      <span class="dim" style="font-size:12px">되돌림 —</span>
      <button class="ghost" onclick="step('rm-added', true)">추가 장비 세어 보기</button>
      <button class="danger" onclick="step('rm-added', false)">추가 장비 지우기</button>
    </div>
    <div class="dim" style="font-size:12px; margin-top:8px">
      <b>세어 보기</b>는 아무것도 쓰지 않는다(<code>dry_run</code>).
      KR 벌을 대상으로 한 실적재·삭제는 <b>복귀 사본</b>이 있어야 눌린다 —
      JP 벌은 위 단계로 다시 만들 수 있어 그 조건을 걸지 않는다.
      <br><b>8번만 되돌릴 수 없다.</b> <code>CL_CELL_MAP</code> 은 로케일
      축이 없는 공유 테이블이고 <code>dump</code> 대상도 아니다 — 그래서
      넣은 <code>ENB_ID</code> 목록을 <b>원장 파일</b>로 남기고, 그 목록이
      ⓐ KR 생성에서 그 장비를 빼는 근거이자 ⓑ 되돌림 수단이다.
      <b>원장을 지우면 KR 이 그 장비를 후보로 본다</b>(미매핑 셀로 드러난다).
    </div>
  </div>
</div>

<!-- ── 행 관리 (CRUD) ───────────────────────────────────────────────────
     사용자 지시(2026-10-26): *"ui에다 새로운 화면 만들어서 거기서 추가 수정
     삭제 관리 가능하도록 만들어줘."* 그리드가 **같은 함수**(`topo.browse` ·
     `topo.update_row` · `insert_rows` · `rm`)를 부른다 — 화면이 자기 SQL 을
     쓰면 커맨드와 다른 행을 보게 된다. -->
<div class="panel">
  <h2>행 관리 <small>— 조회 · 추가 · 수정 · 삭제</small></h2>
  <div class="row">
    <select id="g-table" onchange="gridReset()">
      <option value="city">CM_CITY</option>
      <option value="gu" selected>CM_GU</option>
      <option value="dong">CM_DONG</option>
      <option value="cell">CM_CELL_REP_RU_LOC</option>
      <option value="enb_org">CL_ENB_ORG</option>
      <option value="cellmap">CL_CELL_MAP (한 벌 공유)</option>
    </select>
    <input id="g-q" placeholder="이름·키 검색" style="width:200px"
           onkeydown="if(event.key==='Enter')gridReset()">
    <button class="ghost" onclick="gridReset()">조회</button>
    <button class="ghost" onclick="gridPage(-1)">◀</button>
    <span id="g-page" class="dim"></span>
    <button class="ghost" onclick="gridPage(1)">▶</button>
    <span id="g-tag" class="dim" style="font-size:12px"></span>
  </div>
  <div style="overflow:auto; margin-top:10px">
    <table id="g-tbl"><tbody><tr><td class="dim">조회를 누른다.</td></tr></tbody></table>
  </div>
  <div class="dim" style="font-size:12px; margin-top:8px">
    <b>키 컬럼은 회색이고 고칠 수 없다.</b> 1분 집계 · 원시 Parquet ·
    <code>ground_truth.json</code> · <code>CL_CELL_MAP</code> 이 그 키를
    참조한다 — 화면에서 바꾸면 그 행만 옮겨지고 나머지는 <b>에러 없이</b>
    끊어진다. 키를 바꾸는 것은 <b>삭제 + 추가</b>다.
  </div>
  <div class="sect">
    <h3>행 추가 <small>— <code>COL=VAL, COL=VAL</code></small></h3>
    <div class="row">
      <input id="g-add" placeholder='CITY_ID=41, GU_ID=41110, GU_NAME=…'
             style="flex:1; min-width:260px">
      <button class="ghost" id="g-add-go" disabled onclick="gridAdd()">추가</button>
    </div>
    <div class="row" style="margin-top:8px">
      <input id="g-confirm" placeholder='전체 삭제 확인 문구 (아래 안내 그대로)'
             style="flex:1; min-width:260px">
      <button class="danger" id="g-wipe" disabled onclick="gridWipe()">전체 삭제</button>
      <span id="g-wipe-tag" class="dim" style="font-size:12px"></span>
    </div>
  </div>
</div>

<div class="panel">
  <h2>결과</h2>
  <pre id="out" class="dim">아직 실행하지 않았다.</pre>
</div>

<script>
const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"]/g,
  c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const n = v => (v == null ? "—" : Number(v).toLocaleString());
// **"모름"을 0 으로 적지 않는다**(graphio 불변규칙 17). 서버가 `null` 을 주면
// 화면도 "산출 불가"로 말한다 — 0 으로 보이면 운영자는 "없다"로 읽는다.
const imp = v => (v == null ? '<span class="dim">산출 불가</span>' : n(v));
// `key=value, key=value` 를 dict 로. 값은 전부 문자열로 보낸다 — DDL 타입을
// 화면이 추측하면 틀리고, MySQL 이 컬럼 타입대로 받는다.
function kv(text) {
  const out = {};
  for (const part of String(text || "").split(",")) {
    const i = part.indexOf("=");
    if (i > 0) out[part.slice(0, i).trim()] = part.slice(i + 1).trim();
  }
  return out;
}
let STATE = null;
let LOCALE = "kr";
// 전체 삭제를 받아도 되는가 — **서버 `topo.wipeable` 과 같은 규칙**이다.
// 되돌릴 수단이 있는 쪽만 받는다: KR 원본은 `dump` 사본이 있고, 비(非)kr
// 벌은 단계판으로 다시 만들 수 있다. `cellmap` 은 어느 쪽도 아니다 —
// 로케일 축이 없어 비울 때 지워지는 것은 언제나 KR 셀 대장이다.
const DUMPED = ["city", "gu", "dong", "cell"];
const LOCALE_KEYS = ["city", "gu", "dong", "cell", "enb_org"];
function canWipe(t) {
  if (t === "cellmap") return false;
  return DUMPED.includes(t) || (LOCALE !== "kr" && LOCALE_KEYS.includes(t));
}
// **로케일 축이 없는 테이블.** `kr` 이 아닌 벌에서 고르면 한 벌 공유라고
// 말한다 — 조용히 KR 을 고치면 "JP 를 고쳤다"고 믿는 상태가 된다.
const SHARED = ["cellmap"];

// 로케일 벌 생성 다섯 단계. **순서가 전제다** — 서버 주석과 같은 순서이고,
// 건너뛰면 에러가 아니라 빈 화면이 된다.
const STEPS = [
  {api: "ensure-tables", note: "벌 <b>다섯</b>을 <code>CREATE TABLE … LIKE</code> 로 만든다 (collation·인덱스가 원본에서 온다)"},
  {api: "copy-cells", note: "셀 44만행의 키·지역 참조를 DB 안에서 복사 — 좌표·<code>RU_NAME</code> 은 <b>NULL</b>"},
  {api: "copy-enb-org", note: "eNB 조직 4.6만행 복사 — <code>BTS_NAME</code> 만 <b>NULL</b>. 없으면 지도에 한국 eNB 이름이 섞인다"},
  {api: "geo-fetch", note: "경계 GeoJSON 을 받는다 (없으면 멈춘다 — 빈 테이블을 만들지 않는다)"},
  {api: "code-map", note: "JP 경계 코드 → <b>KR 코드</b> 맵 + <b>추가 구역</b>(KR 에 없는 새 <code>GU_ID</code>)"},
  {api: "seed-geo", note: "행정구역 셋을 경계에서 만든다 (<code>replace</code> 가 비우고 다시 넣는다). 동 수는 <b>KR 구별 범위를 그대로</b> 따른다 — 균일값이면 셀 54.3% 가 동 미매핑"},
  {api: "relocate-cells", note: "좌표·<code>RU_NAME</code>·<code>BTS_NAME</code> 을 그 나라로 쓴다 — 키는 그대로"},
  {api: "seed-enb-jp", note: "추가 구역에 <b>없는 장비</b>를 놓는다 — <code>CL_CELL_MAP</code>(KR 공유·사본 없음)에 행이 들어간다"},
];

// 쓰기 잠금은 **KR 에서만** 사본을 본다. JP 벌은 다섯 단계로 다시 만들 수
// 있어 복귀 사본이 선행 조건이 아니다(서버 `_require_dump` 와 같은 판단).
function canWrite() {
  return LOCALE !== "kr" || !!(STATE && STATE.dump.restorable);
}

// **`await` 한다**(결함 D). 전에는 fire-and-forget 이라 두 갱신이 언제
// 끝나는지 알 수 없고, 그 사이의 실패가 조용히 삼켜졌다. 벌을 바꾸는 것은
// **모든 조작의 대상이 바뀌는 일**이라 그 경합을 남겨 둘 자리가 아니다.
async function switchLocale() {
  LOCALE = $("locale").value;
  try { await Promise.all([gridLoad0(), load()]); }
  catch (e) { show("벌 전환에 실패했다: " + (e.message || e), "bad"); }
}

function show(obj, cls) {
  $("out").className = cls || "";
  $("out").textContent = typeof obj === "string" ? obj
    : JSON.stringify(obj, null, 2);
}

async function load() {
  try {
    const d = await (await fetch("/api/topo?locale=" + encodeURIComponent(LOCALE))).json();
    STATE = d;
    // 로케일 셀렉트는 **서버가 아는 목록**으로 채운다 (`GEO_SOURCES`) —
    // 화면에 적어 두면 나라를 추가할 때 한 곳이 남는다.
    if (!$("locale").options.length) {
      $("locale").innerHTML = Object.keys(d.locales).map(lc =>
        `<option value="${lc}"${lc === LOCALE ? " selected" : ""}>${lc}</option>`).join("");
    }
    $("locale-tag").textContent = LOCALE === "kr"
      ? " 원본 (접미사 없음)" : " 접미사 _" + LOCALE.toUpperCase();
    $("tbl-locale").textContent = "locale " + d.locale;
    // **"모름"과 "해당 없음"은 다른 것이다**(결함 C). 좌표 컬럼이 아예
    // 없는 테이블(`CL_ENB_ORG`·`CL_CELL_MAP`)에 "산출 불가"를 찍으면
    // 불변규칙 17 이 지키려던 구분 자체가 무의미해진다.
    const COORD = d.coord_keys || [];
    $("tbl").innerHTML = Object.entries(d.tables).map(([k, v]) =>
      `<tr><td>${esc(v.table)}</td><td class="n">${n(v.rows)}</td>` +
      `<td class="n">${COORD.includes(k) ? imp(v.no_coord)
        : '<span class="dim" title="이 테이블에는 좌표 컬럼이 없다">—</span>'}` +
      `</td></tr>`).join("");
    const a = d.active;
    $("active").innerHTML =
      `eNB <b>${n(a.enbs)}</b> · 셀 <b>${n(a.cells)}</b> · ` +
      `위치 마스터에 행 없음 <b class="${a.unmapped ? "warn" : "ok"}">` +
      `${n(a.unmapped)}</b>` +
      (a.unmapped ? ` <span class="dim">(좌표는 생성기가 매 틱 만든다 —` +
        ` 아래 "좌표 결손" 참조)</span>` : "");

    const p = d.pick, others = p.candidates - p.band_master,
          opick = p.picked_enbs - p.band_picked;
    const mark = !p.band_master ? '<span class="dim">—</span>'
      : (p.band_missing.length ? '<b class="bad">✗</b>' : '<b class="ok">✓</b>');
    $("pick").innerHTML =
      `<tr><td>기존 (topo 대역 밖)</td><td class="n">${n(others)}</td>` +
      `<td class="n">${n(opick)}</td><td>${opick ? '<b class="ok">✓</b>' : '<b class="bad">✗</b>'}</td></tr>` +
      `<tr><td>topo (${p.band_master ? "" : "아직 없음 · "}${d.band[0]}~${d.band[1]})</td>` +
      `<td class="n">${n(p.band_master)}</td><td class="n">${n(p.band_picked)}</td>` +
      `<td>${mark}</td></tr>`;
    $("pick-note").innerHTML =
      `후보 셀 ${n(p.candidate_cells)} → 선택 셀 ${n(p.picked_cells)} · ` +
      `<code>enb_limit</code> ${n(d.enb_limit)} · 신규 셀 트래픽 가중 ` +
      `×${d.topo_weight}<br>seed 대상 시군구 ${n(d.seed_gus)} 중 ` +
      // **그 로케일의 crosswalk 로 재고, 없으면 "산출 불가"다**(결함 A).
      // 전에는 KR 대응표로 JP 구역을 판정해 "731곳에 동이 없다"는 거짓
      // 숫자를 띄웠다. 0 으로도 전수로도 적지 않는다(불변규칙 17).
      (d.gus_without_drawable_dong == null
        ? `<b class="warn">그릴 수 있는 동은 산출 불가</b> — ` +
          `${esc(d.drawable_crosswalk || "crosswalk 경로 미설정")} 가 없다 ` +
          `(seed-geo 가 그 파일을 만든다)`
        : `<b>${n(d.gus_without_drawable_dong)}곳은 그릴 수 있는 동이 없다</b> — ` +
          `그 셀은 <code>DONG_ID</code> 를 비워야 하고 적재에서 빠진다 ` +
          `(0 으로 채우지 않는다)`) +
      (p.band_missing.length ? `<br><b class="bad">대역 eNB ${p.band_missing.length}개가
         선택되지 않았다 — include 가 깨졌다는 뜻이다 (한도를 올려 해결하지 않는다)</b>` : "");

    const dm = d.dump;
    const head = dm.restorable
      ? `<b class="ok">되돌릴 수 있다</b>`
      : (dm.exists ? `<b class="bad">파일은 있는데 되돌릴 수 없다</b>`
                   : `<b class="bad">없다</b>`);
    $("dump").innerHTML = head + ` <span class="dim">${esc(dm.dir)}` +
      ` (${esc(dm.suffix)})</span>` +
      `<table style="margin-top:8px"><tbody>` + (dm.files || []).map(f =>
        `<tr><td>${esc(f.table)}</td><td class="n">${imp(f.rows)}</td>` +
        `<td class="${f.error ? "bad" : "dim"}">${esc(f.error || "읽힘")}</td></tr>`
      ).join("") + `</tbody></table>`;
    $("dump-scope").innerHTML =
      `복구 범위 <b>${(dm.scope || []).map(esc).join(" · ")}</b>` +
      (dm.not_restored || []).map(t =>
        `<br><span class="bad">복구 대상 아님</span> — ${esc(t)}`).join("");
    // 로케일 벌 — 테이블별 존재 여부. **"없다"와 "0행"을 가른다**
    // **벌마다 행 수를 받는다**(결함 B). 전에는 보고 있는 벌만 수가 있어
    // 다른 벌이 `— / 있다` 로 나왔고 그것이 "없다"처럼 읽혔다. 벌이 없을
    // 때만 `—` 이고, 그때는 **상태 칸도 "없다"** 라 둘이 어긋나지 않는다.
    $("sets").innerHTML = Object.entries(d.locales).map(([lc, arr]) =>
      arr.map((t, i) =>
        `<tr><td>${i ? "" : esc(lc) + (lc === d.locale ? " ◀" : "")}</td>` +
        `<td><code>${esc(t.table)}</code></td>` +
        `<td class="n">${t.exists ? n(t.rows) : '<span class="dim">—</span>'}</td>` +
        `<td class="${t.exists ? "ok" : "dim"}">${t.exists ? "있다" : "없다"}</td></tr>`
      ).join("")).join("");
    const ad = d.added || {};
    $("added").innerHTML = ad.enbs
      ? `JP 전용 장비 <b>${n(ad.enbs)}</b>대 · 대역 시작 ` +
        `<code>${esc(ad.band_start)}</code> <span class="dim">— 원장 ` +
        `${esc(ad.path)}</span><br><span class="warn">이 파일이 KR 생성에서 ` +
        `그 장비를 빼는 근거이자 되돌림 수단이다. 지우면 KR 이 후보로 본다.</span>`
      : `<span class="dim">추가한 JP 전용 장비가 없다 (원장 ${esc(ad.path)})</span>`;
    $("steps").innerHTML = STEPS.map((s, i) =>
      `<div class="row" style="margin-bottom:6px">` +
      `<b style="width:16px">${i + 1}</b><code style="width:140px">${esc(s.api)}</code>` +
      `<span class="dim" style="flex:1; min-width:220px; font-size:12px">${s.note}</span>` +
      `<button class="ghost" onclick="step('${s.api}', true)">세어 보기</button>` +
      `<button class="danger" onclick="step('${s.api}', false)"` +
      `${canWrite() ? "" : " disabled"}>실행</button></div>`).join("");

    // **쓰기 버튼은 "되돌릴 수 있는가"로 잠근다** — 파일 존재가 아니다.
    // 서버도 409 로 막지만(이쪽이 정본이다), 눌린 뒤 거부되면 "왜 안
    // 되는지"를 사람이 찾아야 한다.
    for (const id of ["se-go", "rs-go"]) $(id).disabled = !dm.restorable;
    // `seed-enb` 는 **KR 전용이다** — `CL_CELL_MAP` 에도 넣고 그 테이블은
    // 한 벌 공유라, JP 벌에만 장비를 놓을 방법이 없다(서버도 KR 에 넣는다).
    if (LOCALE !== "kr") {
      $("se-go").disabled = true;
      $("se-go").title = "seed-enb 는 KR 전용 — 셀 대장(CL_CELL_MAP)이 한 벌 공유다";
    }
    $("g-add-go").disabled = !canWrite();
    syncWipe();
  } catch (e) { show("불러오지 못했다: " + e, "bad"); }
}

async function post(path, body) {
  // **로케일을 항상 실어 보낸다.** 빠뜨리면 서버 기본값(`kr`)이 들어가고,
  // 화면은 JP 를 보여 주면서 KR 을 고치게 된다 — 에러가 나지 않는다.
  body = Object.assign({locale: LOCALE}, body || {});
  const r = await fetch("/api/topo/" + path, {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify(body)});
  const d = await r.json();
  if (!r.ok) throw new Error(d.detail || JSON.stringify(d));
  return d;
}

async function run(what, opt) {
  const body = Object.assign({
    per_gu: Number($("se-pergu").value) || 1,
    cells: Number($("se-cells").value) || 3,
  }, opt);
  if (opt.dry_run === false &&
      !confirm("마스터에 실제로 넣는다. 되돌리려면 사본을 직접 복구해야 한다. 계속?"))
    return;
  show("실행 중…", "dim");
  try { show(await post(what, body)); await load(); }
  catch (e) { show(String(e.message || e), "bad"); }
}

// ── 로케일 벌 단계 실행 ─────────────────────────────────────────────────
//
// `dry` 가 참이면 아무것도 쓰지 않는다. `seed-geo` 만 `replace` 를 함께
// 묻는다 — 세 테이블을 **비우고** 다시 넣는 유일한 단계다.
async function step(api, dry) {
  const body = {dry_run: dry, dongs: 8, replace: false};
  if (api === "seed-geo" && !dry) {
    body.replace = confirm(
      "행정구역 세 테이블을 **비우고** 다시 넣는다(replace). " +
      "확인을 누르면 전량 교체, 취소를 누르면 빈 테이블에만 넣는다.");
  }
  if (api === "geo-fetch") body.replace = false;
  if (api === "seed-enb-jp") {
    // **번호를 화면에도 박지 않는다.** 빈 대역은 그 DB 의 사실이라 사람이
    // 실측해서 넣는다 — 서버가 받은 번호로 만든 ID 전부가 세 테이블에서
    // 비어 있는지 확인한 뒤에 쓴다(겹치면 멈춘다).
    const v = prompt("JP 전용 ENB_ID 대역의 시작 번호 — 빈 대역을 실측해서 " +
                     "넣는다 (기본값 없음)");
    if (!v) return;
    body.band_start = Number(v);
  }
  if (api === "relocate-cells") {
    // **범위를 묻는다.** 활성 셀만 고치면 나머지 44만행의 좌표·이름은
    // 한국으로 남는다 — "DB 째 이식" 이면 전수가 맞고, 지도·탐지만 보려면
    // 활성 셀로 충분하다(실측 2,161). 조용히 하나를 고르지 않는다.
    body.all = confirm(
      "전수(44만행)로 돌릴까? 확인=전수 · 취소=활성 셀만(1분 집계에 " +
      "나타나는 셀). 활성만 고치면 나머지 좌표·이름은 한국으로 남는다.");
  }
  if (!dry && !confirm(api + " 를 실제로 실행한다. 계속?")) return;
  show("실행 중…", "dim");
  try { show(await post(api, body)); await load(); }
  catch (e) { show(String(e.message || e), "bad"); }
}

// ── 행 관리 그리드 ──────────────────────────────────────────────────────
let GRID = null, OFFSET = 0;
const LIMIT = 50;

// `gridReset` 은 버튼이 부르는 자리(반환을 안 쓴다), `gridLoad0` 은
// **기다려야 하는** 자리가 부르는 자리다 — 같은 일을 두 이름으로 두지 않고
// 전자가 후자를 부른다.
function gridLoad0() { OFFSET = 0; return gridLoad(); }
function gridReset() { gridLoad0(); }
function gridPage(d) {
  if (!GRID) return;
  const next = OFFSET + d * LIMIT;
  if (next < 0 || (GRID.total != null && next >= GRID.total)) return;
  OFFSET = next; gridLoad();
}

function syncWipe() {
  const t = $("g-table").value, can = canWipe(t);
  $("g-wipe").disabled = !can || !canWrite();
  $("g-wipe-tag").textContent = can
    ? (GRID && GRID.table ? "확인 문구: " + GRID.table + " 전체 삭제" : "")
    : (t === "cellmap"
       ? "셀 대장은 전체 삭제 대상이 아니다 (로케일 축이 없고 사본도 없다)"
       : "KR 원본이고 복귀 사본 대상이 아니다 — 지우면 되돌릴 수 없다");
}

async function gridLoad() {
  const t = $("g-table").value;
  const u = `/api/topo/rows?table=${t}&locale=${encodeURIComponent(LOCALE)}` +
            `&q=${encodeURIComponent($("g-q").value)}&limit=${LIMIT}&offset=${OFFSET}`;
  try {
    const d = await (await fetch(u)).json();
    if (d.detail) throw new Error(d.detail);
    GRID = d;
    // **"테이블이 없다"를 "0행"으로 적지 않는다**(불변규칙 17) — 전자는
    // 벌을 먼저 만들라는 뜻이고 후자는 조건이 좁다는 뜻이다.
    if (!d.exists) {
      $("g-tbl").innerHTML = `<tbody><tr><td class="bad">${esc(d.table)} 가 없다` +
        ` — 위 <b>로케일 벌</b>의 1단계를 먼저 돈다.</td></tr></tbody>`;
      $("g-page").textContent = "—"; $("g-tag").textContent = ""; syncWipe();
      return;
    }
    const keys = d.keys;
    $("g-tbl").innerHTML =
      `<thead><tr><th></th>` + d.columns.map(c =>
        `<th${keys.includes(c) ? ' class="dim"' : ""}>${esc(c)}` +
        `${keys.includes(c) ? " 🔑" : ""}</th>`).join("") + `</tr></thead><tbody>` +
      d.rows.map((r, i) =>
        `<tr><td style="white-space:nowrap">` +
        `<button class="ghost" onclick="gridSave(${i})"` +
        `${canWrite() ? "" : " disabled"}>저장</button> ` +
        `<button class="danger" onclick="gridDel(${i})"` +
        `${canWrite() ? "" : " disabled"}>삭제</button></td>` +
        d.columns.map(c => keys.includes(c)
          ? `<td class="dim">${esc(r[c])}</td>`
          : `<td><input id="g-${i}-${c}" value="${esc(r[c] == null ? "" : r[c])}"` +
            ` style="width:${Math.min(240, Math.max(70, String(r[c] ?? "").length * 9 + 24))}px"` +
            `${r[c] == null ? ' placeholder="NULL"' : ""}></td>`).join("") +
        `</tr>`).join("") + `</tbody>`;
    $("g-page").textContent =
      `${n(OFFSET + 1)}–${n(OFFSET + d.rows.length)} / ${n(d.total)}`;
    $("g-tag").innerHTML = `<code>${esc(d.table)}</code>` +
      (d.shared ? ' <b class="warn">한 벌 공유 — 이 행은 KR 에도 그대로 나온다</b>' : "") +
      ` · 검색 대상 ${d.searched.map(esc).join(", ")}`;
    syncWipe();
  } catch (e) { show("행을 불러오지 못했다: " + (e.message || e), "bad"); }
}

// **바뀐 컬럼만 보낸다.** 전 컬럼을 보내면 손대지 않은 값까지 UPDATE 에
// 실려, 빈 입력이 `''` 로 들어가 NULL(모름)을 덮는다.
function gridSave(i) {
  const r = GRID.rows[i], row = {}, key = {};
  for (const c of GRID.keys) key[c] = r[c];
  for (const c of GRID.columns) {
    if (GRID.keys.includes(c)) continue;
    const el = $(`g-${i}-${c}`);
    if (!el) continue;
    const was = r[c] == null ? "" : String(r[c]);
    if (el.value !== was) row[c] = el.value;
  }
  if (!Object.keys(row).length) { show("바뀐 컬럼이 없다.", "dim"); return; }
  (async () => {
    try {
      show(await post("update", {target: $("g-table").value, key, row}));
      await gridLoad();
    } catch (e) { show(String(e.message || e), "bad"); }
  })();
}

function gridDel(i) {
  const r = GRID.rows[i], key = {};
  for (const c of GRID.keys) key[c] = r[c];
  if (!confirm("이 행을 지운다: " + JSON.stringify(key))) return;
  (async () => {
    try {
      show(await post("rm", {target: $("g-table").value, key}));
      await gridLoad(); await load();
    } catch (e) { show(String(e.message || e), "bad"); }
  })();
}

async function gridAdd() {
  const row = kv($("g-add").value);
  if (!Object.keys(row).length) { show("넣을 컬럼이 비어 있다.", "bad"); return; }
  try {
    show(await post("add", {target: $("g-table").value, row}));
    $("g-add").value = ""; await gridLoad(); await load();
  } catch (e) { show(String(e.message || e), "bad"); }
}

async function gridWipe() {
  if (!confirm("테이블을 통째로 지운다. 계속?")) return;
  try {
    show(await post("rm", {target: $("g-table").value, all: true,
                           confirm: $("g-confirm").value}));
    await gridLoad(); await load();
  } catch (e) { show(String(e.message || e), "bad"); }
}

load();
gridLoad();
</script>
</body>
</html>
"""


# ── 지도 — 셀과 **가입자가 실제로 나타난 자리** ──────────────────────────────


def _map_cells(conn) -> list[dict]:
    """생성 대상 셀의 좌표·장비. `_cells` 를 그대로 쓴다.

    **좌표가 없는 셀은 담지 않는다.** 지도에 찍을 수 없고, `0,0` 으로 두면
    기니만 바다에 점이 생긴다(불변규칙 17 · `tables.dms_to_decimal` 머리말).
    그 셀이 몇 개인지는 응답의 `unmapped` 가 말한다 — 숨기지 않는다
    (불변규칙 12).
    """
    out = []
    for c in _cells(conn):
        if not c.latitude or not c.longitude:
            continue
        out.append({
            "key": c.cell_key, "enb": c.enb_id, "cid": c.cell_id,
            "lat": round(c.latitude, 6), "lon": round(c.longitude, 6),
            "tac": c.tac, "ta_list": c.ta_list,
            "sgw": c.sgw_id, "mme": c.mme_id,
            "gu": c.adm_sgg_cd, "name": c.display_name,
        })
    return out


@app.get("/api/map")
def api_map():
    """**장비 위치** — 셀과 그것을 맡는 코어.

    ## 가입자를 보여주지 않는다 (2026-10-27 사람 결정)

    처음에는 가입자 자취를 함께 그렸는데 두 가지가 틀렸다 —

    1. **자취에 시간 순서가 없었다.** 창 안에 나온 셀을 모아 이었더니 순서가
       임의라 지도를 가로지르는 지그재그가 됐다. 원시 레코드에 시각이 있으니
       정렬할 수는 있지만, 그 전에 **보여줄 이유**가 먼저다.
    2. **같은 모양에 다른 뜻을 담았다.** 셀 점은 TA 색, 가입자 점은
       초록/노랑 — 둘 다 "점"이라 무엇을 보는지 섞였다.

    지금 알고 싶은 것은 **장비가 어디 있고 어떻게 묶였나**다. 가입자 이동은
    필요해지면 **별 화면**으로 둔다 — 한 화면에 두 축을 겹치지 않는다.

    ## 무엇을 내리나

    셀마다 좌표와 **네 가지 묶음 키**를 함께 내린다(`tac`·`ta_list`·`sgw`·
    `mme`). 화면이 그중 하나를 골라 색을 칠한다 — 축을 고를 수 있어야
    "이 점이 무슨 색인가"가 헷갈리지 않는다.

    코어 장비는 **자기가 맡은 셀의 중심**에 찍는다. 장비에 좌표가 따로 없고
    (레지스트리는 ID·이름만 들고 있다), 실제로 중요한 것도 "어디 놓였나"가
    아니라 **"어디를 덮나"** 다.
    """
    conn = _connect()
    try:
        cells = _map_cells(conn)
    finally:
        conn.close()

    def centers(key: str) -> list[dict]:
        acc: dict[str, list[dict]] = collections.defaultdict(list)
        for c in cells:
            if c[key]:
                acc[str(c[key])].append(c)
        out = []
        for k, v in sorted(acc.items()):
            out.append({
                "id": k, "kind": key.upper(), "cells": len(v),
                "enb": len({x["enb"] for x in v}),
                "lat": round(sum(x["lat"] for x in v) / len(v), 6),
                "lon": round(sum(x["lon"] for x in v) / len(v), 6),
            })
        return out

    sc = (_pem_cfg().get("scope") or {})
    gu = [str(x) for x in (sc.get("gu") or [])]
    names = {}
    if gu:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT GU_ID, GU_NAME FROM CM_GU_JP WHERE GU_ID IN ("
                            + ",".join(["%s"] * len(gu)) + ")", gu)
                names = {str(a): b for a, b in cur.fetchall()}
        finally:
            conn.close()
    # 구가 많으면 이름을 다 적지 않는다 — 12개면 머리말이 화면을 덮는다.
    # **몇 곳인지는 적는다**(잘랐다는 사실을 숨기지 않는다 · 불변규칙 12)
    gn = [names.get(g, g) for g in gu]
    parts = gn if len(gn) <= 3 else [f"{gn[0]} 외 {len(gn) - 1}곳"]
    if sc.get("dong"):
        parts.append(f"동 {len(sc['dong'])}곳")
    if sc.get("route"):
        parts.append(str(sc["route"]))
    stations, lines = _map_stations(cells)
    return {
        "scope": " + ".join(parts) or "전량",
        "cells": cells,
        "stations": stations,
        "lines": lines,
        "unmapped": max(0, len(_CELLS or ()) - len(cells)),
        "nodes": {"sgw": centers("sgw"), "mme": centers("mme")},
        "counts": {
            "enb": len({c["enb"] for c in cells}),
            "tac": len({c["tac"] for c in cells if c["tac"]}),
            "ta_list": len({c["ta_list"] for c in cells if c["ta_list"]}),
            "sgw": len({c["sgw"] for c in cells if c["sgw"]}),
            "mme": len({c["mme"] for c in cells if c["mme"]}),
        },
    }


#: 노선 색 — JR 공식 라인컬러. 화면은 `.rl-ymt` 가 같은 값을 들고 있다
#: (실제 선로를 칠하는 쪽이라 CSS 에 있어야 한다). 여기 값은 다른 노선을
#: 추가할 때의 자리표다.
_LINE_COLOR = {"YMT": "#9acd32"}


def _map_stations(cells: list[dict]) -> tuple[list[dict], list[dict]]:
    """역 위치와 **노선 선** — 노선 파일이 이미 들고 있다.

    `data/lines/*.json` 의 `station_of` 가 칸마다 역 id 를 적고 `cells` 가
    같은 순서로 셀 키를 적으므로, 역에 딸린 셀들의 **좌표 평균**이 곧 역
    자리다. 정확한 역사 좌표가 아니라 **그 역을 덮는 셀의 중심**이다 —
    실측으로 실제 역사와 **중앙값 73m · 최대 238m** 떨어져 있다
    (`tools/build_line_route.py` 의 `ST` 와 대조). 지도에서 "여기가
    신주쿠쯤"을 읽는 용도고, 그 이상으로 쓰면 안 된다.

    `lines` 는 역을 `station_pos` 순서로 이은 선이다. **이제 이것을 그리지
    않는다**(2026-10-28) — 실제 선로 레이어(`/api/map/layer/rail`)가 들어온
    뒤로 둘이 겹쳐 보이는데 직선 쪽이 틀린 그림이다. 지금 쓰임은 **실제
    선로에서 야마노테 구간을 집는 자**다(화면의 `markYamanote`).

    노선 파일이 없으면 빈 목록이다. 곁다리라 없어도 화면이 선다.
    """
    pos = {c["key"]: c for c in cells}
    acc: dict[str, list[dict]] = collections.defaultdict(list)
    routes = []
    try:
        d = Path(str(_pem_cfg().get("lines_dir") or "data/lines"))
        if not d.is_absolute():
            d = (HERE / d).resolve()
        for f in sorted(d.glob("*.json")):
            routes += json.loads(f.read_text(encoding="utf-8"))
        for route in routes:
            names = route.get("stations") or {}
            keys = route.get("cells") or []
            for key, sid in zip(keys, route.get("station_of") or []):
                c = pos.get(key)
                if c and names.get(sid):
                    acc[names[sid]].append(c)
    except Exception:
        return [], []      # 지도의 곁다리다 — 실패해도 장비는 보여야 한다

    st = {k: (round(sum(x["lat"] for x in v) / len(v), 6),
              round(sum(x["lon"] for x in v) / len(v), 6), len(v))
          for k, v in acc.items()}

    lines, seen = [], set()
    for route in routes:
        names = route.get("stations") or {}
        order = sorted((route.get("station_pos") or {}).items(),
                       key=lambda kv: kv[1])
        pts = [[st[names[sid]][1], st[names[sid]][0]]
               for sid, _ in order if names.get(sid) in st]
        if len(pts) < 2:
            continue
        # 외선·내선은 **같은 선로다.** 둘 다 그리면 두 겹이 된다
        rid = str(route.get("id") or "").split("-")[0]
        if rid in seen:
            continue
        seen.add(rid)
        # 외선·내선 꼬리를 뗀다 — 선은 하나고 방향은 선의 성질이 아니다
        nm = re.sub(r"\s*[가-힣]*순환\([^)]*\)", "",
                    str(route.get("name") or rid).split(" — ")[0]).strip()
        lines.append({"id": rid, "name": nm or rid,
                      "color": _LINE_COLOR.get(rid, "#6b7684"),
                      "loop": bool(route.get("loop")), "pts": pts})

    return ([{"name": k, "lat": v[0], "lon": v[1], "cells": v[2]}
             for k, v in sorted(st.items())], lines)


#: 배경 레이어 — 설정 키 ↔ 파일. **경로는 각자 자기 키로 받는다.**
#: 전에 `geo_crosswalk` 의 형제에서 찾다가 로케일별로 디렉터리가 달라
#: 컨테이너에서 파일을 못 찾았다(§1.15). 같은 실수를 늘리지 않는다.
#: 배경 레이어 — 설정 키와 **출처 문구**. 출처는 레이어마다 다르다
#: (약관 의무라 화면이 받은 것만 표기한다). 도로·녹지는 OSM 이다 —
#: 국토수치정보의 도로(`N01`)가 1995년판 0.12MB 라 쓸 수 없었다
#: (`tools/build_jp_geo_layers.py` 머리말).
_LAYER = {
    "rail":   ("geo_rail",   "国土数値情報 (国土交通省) · 鉄道 N02"),
    "rivers": ("geo_rivers", "国土数値情報 (国土交通省) · 河川 W05"),
    "roads":  ("geo_roads",  "© OpenStreetMap contributors"),
    "green":  ("geo_green",  "© OpenStreetMap contributors"),
}


@app.get("/api/map/layer/{name}")
def api_map_layer(name: str):
    """철도·하천 — **국토수치정보를 구워 둔 로컬 파일이다.**

    행정경계 한 장만으로는 "지도 같지 않다"는 말을 들었다. 같은 출처가
    같은 약관으로 철도(N02)·하천(W05)을 내므로 `tools/build_jp_geo_layers.py`
    가 한 번 받아 굽고 여기서는 읽기만 한다 — 실행 시점에 외부를 부르지
    않는 원칙은 그대로다(`/api/map/geo` 머리말).

    **철도는 실제 선형이다.** 화면의 야마노테선 초록 선은 역을 직선으로
    이은 **모사 경로**고, 이 레이어가 그 아래 깔리는 **실제 선로**다.
    둘을 겹쳐 두면 모사가 어디서 눌렸는지 보인다.

    **도로·녹지만 출처가 다르다**(2026-10-28). 국토수치정보의 도로는
    `N01` 이 1995년판 0.12MB 라 간선 몇 줄뿐이고, 최신 `N13` 은 긴급수송
    도로라 가로망이 아니다. 그래서 그 둘은 OSM 에서 굽는다 — ODbL 이라
    화면이 `© OpenStreetMap contributors` 를 함께 적는다. 받는 것은 빌드
    시점 한 번이고 **런타임이 외부를 부르지 않는 원칙은 그대로다.**
    """
    hit = _LAYER.get(name)
    if not hit:
        raise HTTPException(404, f"그런 레이어가 없다: {name}")
    key, source = hit
    raw = str(_pem_cfg().get(key) or "")
    if not raw:
        return {"error": f"`pemdb1.{key}` 가 비어 있다", "items": []}
    src = Path(raw)
    if not src.is_absolute():
        src = (HERE / src).resolve()
    if not src.exists():
        # 빈 배열을 돌려주지 않는다 — "데이터가 없다"와 "파일이 없다"는
        # 다른 사실이고, 전자로 보이면 왜 비었는지 알 수 없다
        return {"error": f"레이어 파일이 없다: {src}", "items": []}
    return {"items": json.loads(src.read_text(encoding="utf-8")),
            "source": source}


@app.get("/api/map/all")
def api_map_all():
    """**마스터 전량** — 생성 대상이 아닌 셀까지.

    지도에 생성 대상만 그렸더니 "신주쿠에만 기지국이 있나?"로 읽혔다.
    아니다 — `pemdb1.scope` 가 좁힌 것이고 마스터에는 도쿄 전역이 있다.
    **좁힌 사실이 화면에 보여야 한다**(불변규칙 12 의 결).

    좌표만 내린다. 2만 개가 넘어 장비 정보까지 실으면 응답이 커지고,
    배경으로 깔 것이라 누를 일도 없다.
    """
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT LATITUDE, LONGITUDE, ENB_ID "
                        "FROM CM_CELL_REP_RU_LOC_JP")
            rows = cur.fetchall()
    finally:
        conn.close()
    # **좌표를 겹쳐 보내지 않는다.** 한 자리에 섹터가 6개씩 서 있어
    # 23,039셀이 좌표로는 5,036자리다 — 그대로 보내면 4.5배를 헛보낸다.
    # 화면의 점 하나는 **셀이 아니라 자리**다(그래서 수를 따로 내린다).
    seen, pts, enb, n = set(), [], set(), 0
    for la, lo, e in rows:
        a, o = dms_to_decimal(la), dms_to_decimal(lo)
        if not (a and o):
            continue
        n += 1
        enb.add(e)
        k = (round(o, 5), round(a, 5))
        if k not in seen:
            seen.add(k)
            pts.append([k[0], k[1]])
    return {"pts": pts, "cells": n, "enb": len(enb), "spots": len(pts)}


@app.get("/api/map/geo")
def api_map_geo(bbox: str = ""):
    """행정경계 GeoJSON — **로컬 파일이다. 외부 타일을 쓰지 않는다.**

    처음에 CARTO 타일 CDN 으로 배경을 깔았는데 셋이 걸렸다 —

    1. **CARTO 는 API 키를 요구한다.** 키 없이 쓰면 언제 끊길지 모른다.
    2. **현장 PoC 가 폐쇄망일 수 있다.** 외부 CDN 이면 지도가 통째로 안 뜬다.
    3. **이 저장소의 다른 화면은 외부 의존이 없다.** 저 셋(Leaflet CSS·JS·
       타일)이 `ui.py` 의 **유일한** 외부 URL 이었다.

    그래서 graphio 가 이미 들고 있는 경계를 쓴다 — `data/geo/jp/` 의
    국토수치정보 행정구역(1.87MB · CC BY 4.0 · 그쪽 `SOURCE.txt`).

    `bbox` 는 `"lat0,lon0,lat1,lon1"` 이다. 전국 1,897개를 다 내리면 브라우저가
    느려진다 — 보는 자리만 내린다.
    """
    # **자기 설정 키를 쓴다.** 처음엔 `geo_crosswalk` 의 형제에서 찾았는데,
    # 그 키가 로케일별로 다른 디렉터리를 가리켜(`data/geo/` vs
    # `data/geo/jp/`) 컨테이너에서 **파일을 못 찾았다** — 두 값이 늘 같은
    # 디렉터리에 있으리라는 가정이 틀렸다. 경로는 경로대로 받는다.
    raw = str(_pem_cfg().get("geo_boundary") or "")
    if not raw:
        return {"error": "`pemdb1.geo_boundary` 가 비어 있다 — 행정경계 파일 "
                         "경로를 설정에 적어야 한다", "features": []}
    src = Path(raw)
    if not src.is_absolute():
        src = (HERE / src).resolve()
    if not src.exists():
        # **빈 배열을 돌려주지 않는다** — "경계가 없다"와 "파일이 없다"는
        # 다른 사실이고, 전자로 보이면 지도가 왜 비었는지 알 수 없다
        return {"error": f"경계 파일이 없다: {src}", "features": []}

    lo_la = lo_lo = -1e9
    hi_la = hi_lo = 1e9
    if bbox:
        try:
            lo_la, lo_lo, hi_la, hi_lo = (float(x) for x in bbox.split(","))
        except ValueError:
            lo_la = lo_lo = -1e9
            hi_la = hi_lo = 1e9

    doc = json.loads(src.read_text(encoding="utf-8"))
    out = []
    for f in doc.get("features", []):
        g = f.get("geometry") or {}
        rings = ([g.get("coordinates")] if g.get("type") == "Polygon"
                 else g.get("coordinates") or [])
        keep = []
        for poly in rings:
            if not poly:
                continue
            ring = poly[0]
            xs = [c[0] for c in ring]
            ys = [c[1] for c in ring]
            if (max(ys) < lo_la or min(ys) > hi_la
                    or max(xs) < lo_lo or min(xs) > hi_lo):
                continue
            keep.append(ring)
        if keep:
            pr = f.get("properties") or {}
            # 이름을 놓을 자리 — **가장 큰 고리**의 가운데. 섬이 딸린 구는
            # 작은 고리에 이름이 붙으면 엉뚱한 바다 위에 뜬다
            big = max(keep, key=len)
            xs = [c[0] for c in big]
            ys = [c[1] for c in big]
            out.append({
                "name": pr.get("N03_004") or pr.get("N03_003") or "",
                "city": pr.get("N03_003") or "",
                "code": pr.get("N03_007") or "",
                "at": [round((min(xs) + max(xs)) / 2, 6),
                       round((min(ys) + max(ys)) / 2, 6)],
                "rings": keep,
            })
    return {"features": out, "source": "国土数値情報 行政区域 · CC BY 4.0"}


_HTML_MAP = """<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<title>지도 · 장비 위치</title>
<style>
 /* **외부 CSS·JS 를 쓰지 않는다.** 현장 PoC 가 폐쇄망일 수 있고, 이
    저장소의 다른 화면도 외부 의존이 없다 — `/api/map/geo` 머리말 */
 html,body{margin:0;height:100%;font:13px/1.5 -apple-system,BlinkMacSystemFont,
   "Hiragino Sans","Noto Sans JP",sans-serif;background:#0f1216;color:#d8dee9}
 /* **두 줄로 고정한다.** 한 줄에 몰아넣던 때는 창이 1200px 일 때
    `.stat` 혼자 856px 를 먹어 라벨이 "철\n도·하천" 으로 접히고 숫자는
    헤더 밖으로 삐져나갔다. 줄바꿈 자리를 브라우저에 맡기지 않는다 —
    `.stat` 이 폭 전부를 차지해 **반드시 둘째 줄로 내려간다** */
 header{height:70px;display:flex;flex-wrap:wrap;align-content:center;
   align-items:center;gap:5px 14px;padding:0 14px;
   background:#161b22;border-bottom:1px solid #2a313a}
 header>*{flex:none;white-space:nowrap}
 header b{font-size:14px} label{opacity:.85}
 select,button{background:#222933;color:#d8dee9;border:1px solid #39414c;
   border-radius:4px;padding:3px 7px;font:inherit}
 /* 폭을 다 차지해 줄을 가른다. 넘치면 **자른다** — 줄을 늘리면 지도가
    그만큼 줄어든다 */
 .stat{flex-basis:100%;display:flex;gap:14px;overflow:hidden;
   font-variant-numeric:tabular-nums}
 .stat>span{flex:none}
 .stat #scope,.stat #note{flex:1;min-width:0;overflow:hidden;
   text-overflow:ellipsis}
 .stat i{font-style:normal;opacity:.55;margin-right:4px}
 .warn{color:#e0a458}
 .dimtag{opacity:.55;font-size:12px}
 /* 바다색 바탕 위에 육지를 얹는다 — 전에는 둘 다 거의 같은 검정이라
    경계가 나와도 "지도가 안 나온다"로 보였다 */
 #wrap{position:absolute;inset:70px 0 0 0;overflow:hidden;cursor:grab;
   background:#0d1a26}
 #wrap.drag{cursor:grabbing}
 svg{width:100%;height:100%;display:block}
 .adm{fill:#223040;stroke:#4a5c72;stroke-width:1px;
   vector-effect:non-scaling-stroke}
 /* TA 묶음 히트맵 — `screen` 이라 **겹칠수록 밝아진다.** 어두운 바탕에서
    밀도를 읽는 방식이고, 두 묶음이 맞닿는 자리는 색이 섞여 경계가 보인다.
    볼록껍질이 바깥쪽 몇 점만 이어 안쪽을 비워 두던 것과 다르다 */
 .kde{mix-blend-mode:screen;pointer-events:none}
 /* **라벨은 SVG 밖 div 다** — 툴팁과 같은 이유이고, 한 번 더 데였다.
    이 viewBox 의 단위는 **도(degree)** 라서 SVG 안에서 `stroke-width:3px`
    는 3px 가 아니라 **3도 ≈ 330km** 가 된다. 그 검은 테두리가 지도를
    통째로 덮어 화면이 새까맣게 나왔다. 글자는 확대해도 같은 크기여야
    하므로 애초에 지도 좌표계에 둘 것이 아니었다. */
 /* 노선은 **배경 레이어**다 — 장비를 가리면 안 되니 굵되 흐리게 */
 /* **23,039개를 노드 하나로 그린다.** circle 을 그만큼 만들면 브라우저가
    버벅인다 — 길이 0 짜리 선분을 둥근 캡으로 찍으면 점이 된다 */
 /* 도쿄 바깥 eNB. 1.4 → 2.2px 로는 **여전히 안 보인다**는 말을 들어
    3.4px 로 올렸다. 생성 대상(`#f7eec7` 노랑)과 색이 갈려 있으므로
    굵어져도 둘을 혼동하지 않는다 */
 .allpt{fill:none;stroke:#828d9c;stroke-width:3.4px;stroke-opacity:.8;
   stroke-linecap:round;vector-effect:non-scaling-stroke;pointer-events:none}
 /* 실제 선로 — **배경이다.** 장비를 읽는 데 방해가 되면 안 되므로
    가늘고 흐리게. JR 만 조금 밝게 해 뼈대가 보이게 한다 */
 /* 노선 색은 **JS 가 이름 해시로 준다**(`lineColor`). 129개 노선에 손으로
    색표를 매기면 노선이 하나 늘 때마다 표가 틀어진다. 여기서는 굵기와
    세기만 정한다 — 전보다 진하지만(.6 → .85) 채도를 낮춰 두므로
    장비를 이기지 않는다 */
 .rl{fill:none;stroke-width:1.4px;stroke-opacity:.85;
   stroke-linecap:round;vector-effect:non-scaling-stroke;pointer-events:none}
 .rv{fill:none;stroke:#2d5f7a;stroke-width:1.6px;stroke-opacity:.8;
   stroke-linecap:round;vector-effect:non-scaling-stroke;pointer-events:none}
 /* 공원·수역 — **면이다.** 육지(`.adm` 의 #223040) 위에 색만 얹는다.
    테두리를 주면 작은 공원이 점처럼 보여 장비와 섞인다 */
 .gr{stroke:none;pointer-events:none}
 .gr-park{fill:#27402f}
 .gr-water{fill:#19354a}
 /* 도로 — **가장 아래 배경이다.** 철도(`.rl`)보다 어둡게 둬야 뼈대가
    철도 쪽으로 읽힌다. 등급마다 굵기를 달리하지 않으면 가로망이
    회색 덩어리가 된다 */
 .rd{fill:none;stroke-linecap:round;stroke-linejoin:round;
   vector-effect:non-scaling-stroke;pointer-events:none}
 /* 색을 **푸른 쪽으로** 둔다. 처음엔 중성 회색이었는데 장비 점
    (`.allpt` #555e6b)과 명도가 겹쳐 어디까지가 도로인지 모호했다 */
 /* **철도보다 연해야 한다.** 노선이 색을 얻으면서 도로까지 눈에
    들어오면 둘이 다툰다 — 도로는 자리를 알려주는 바탕이다 */
 .rd-motorway{stroke:#5a6574;stroke-width:2.2px}
 .rd-trunk{stroke:#4f5968;stroke-width:1.8px}
 .rd-primary{stroke:#464f5d;stroke-width:1.4px}
 .rd-secondary{stroke:#3e4654;stroke-width:1.1px}
 .rd-tertiary{stroke:#38404d;stroke-width:.9px}
 /* **확대하면 굵어진다.** `non-scaling-stroke` 라 굵기가 화면 px 로
    고정인데, 1km 폭 화면에서 1px 짜리 간선은 실제 도로폭(20m ≈ 24px)과
    너무 달라 "가느다란 실"로 보였다. LOD 단계를 그대로 굵기 단계로 쓴다 */
 svg[data-lod="2"] .rd-motorway{stroke-width:3.2px}
 svg[data-lod="2"] .rd-trunk{stroke-width:2.6px}
 svg[data-lod="2"] .rd-primary{stroke-width:2px}
 svg[data-lod="2"] .rd-secondary{stroke-width:1.5px}
 svg[data-lod="2"] .rd-tertiary{stroke-width:1.1px}
 svg[data-lod="3"] .rd-motorway{stroke-width:5px}
 svg[data-lod="3"] .rd-trunk{stroke-width:4px}
 svg[data-lod="3"] .rd-primary{stroke-width:3px}
 svg[data-lod="3"] .rd-secondary{stroke-width:2.2px}
 svg[data-lod="3"] .rd-tertiary{stroke-width:1.6px}
 /* 야마노테선 — JR 라인컬러(黄緑). 이 화면의 주인공 노선이다.
    **`.rl` 뒤에 와야 이긴다** — 특이도가 같아 순서가 정한다 */
 .rl-ymt{stroke:#9acd32;stroke-width:2.6px;stroke-opacity:.95}
 .pick{fill:none;stroke:#ffd43b;stroke-width:.9px;stroke-opacity:.8;
   vector-effect:non-scaling-stroke}
 /* 고른 계통에 **불이 들어온다.** 전에는 노란 테두리 원만 그려 어느
    장비가 걸렸는지 세어야 했다 — TA 히트맵과 같은 방식으로 영역째
    밝힌다(사람 결정 2026-10-28) */
 .glow{mix-blend-mode:screen;pointer-events:none}
 #labels{position:absolute;inset:0;pointer-events:none;overflow:hidden}
 .lb{position:absolute;transform:translate(-50%,-50%);white-space:nowrap;
   font-weight:700;color:#fff;text-shadow:0 0 3px #0b0e12,0 0 3px #0b0e12,
   0 1px 2px #0b0e12}
 .lb-h{font-size:13px;opacity:.9}
 /* **상자를 없앴다**(2026-10-28). 검은 배경에 테두리까지 두르니 장비
    번호가 역 이름보다 튀어, 배경이어야 할 것이 주인공이 됐다. 글자
    그림자(`.lb`)만으로 어두운 지도 위에서 충분히 읽힌다 */
 /* **두껍게를 푼다.** `.lb` 가 700 이라 장비 번호가 역 이름을 눌렀다 */
 .lb-n{font-size:12px;font-weight:400}
 /* 지명은 **배경이다** — 장비를 읽는 데 방해가 되면 안 된다 */
 .lb-a{font-size:12px;font-weight:500;color:#7d8fa6;letter-spacing:.04em}
 /* 점은 SVG 가 찍는다 — 여기서는 글자만. 왼쪽 끝을 점에 맞춘다 */
 /* 역은 **어디인지 읽는 수단**이라 장비 다음으로 또렷해야 한다.
    11px 흐린 회색일 때는 장비 번호 상자에 묻혔다 */
 .lb-s{font-size:13px;font-weight:600;color:#fff;transform:translateY(-50%)}
 #tip{position:absolute;pointer-events:none;background:#0b0e12f2;
   border:1px solid #39414c;border-radius:4px;padding:5px 8px;font-size:12px;
   white-space:pre;display:none;z-index:5}
 #legend{position:absolute;left:10px;bottom:10px;z-index:3;background:#0b0e12e8;
   border:1px solid #2a313a;border-radius:6px;padding:8px 10px;font-size:12px;
   max-height:46%;overflow:auto;min-width:148px}
 #legend h4{margin:0 0 6px;font-size:12px;opacity:.7;font-weight:600}
 #legend div{display:flex;align-items:center;gap:6px;line-height:1.7}
 #legend b{font-variant-numeric:tabular-nums}
 #legend s{width:10px;height:10px;border-radius:50%;display:inline-block;
   text-decoration:none;flex:none}
 #sel{position:absolute;left:50%;transform:translateX(-50%);top:10px;z-index:4;
   display:none;gap:14px;align-items:center;background:#0b0e12f2;
   border:1px solid #ffd43b66;border-radius:6px;padding:7px 12px;font-size:12px;
   white-space:nowrap}
 #sel b{color:#ffd43b;margin-right:2px} #sel span{opacity:.8;margin-right:12px}
 #sel button{margin-left:4px}
 #src{position:absolute;right:8px;bottom:6px;font-size:11px;opacity:.45}
 /* 축척 — 출처 줄 위에 둔다. 셀 간격이 수백 m 라 "지금 몇 m 를 보고
    있나"가 장비 밀도를 읽는 데 필요하다 */
 #scale{position:absolute;right:8px;bottom:24px;z-index:3;font-size:11px;
   display:flex;align-items:center;gap:6px;opacity:.75;pointer-events:none}
 #scale i{height:7px;border:1px solid #c6cedb;border-top:0;display:block}
 #scale b{font-weight:600;font-variant-numeric:tabular-nums}
</style></head><body>
<header>
  <b>장비 위치</b>
  <label>보기 <select id=mode>
    <option value=enb>전체 eNB</option>
    <option value=ta>TA 목록</option>
    <option value=sgw>SGW</option>
    <option value=mme>MME</option>
  </select></label>
  <button id=reload>새로고침</button>
  <button id=reset>전체 보기</button>
  <div class=stat>
    <span><i>셀</i><b id=ncell>0</b></span>
    <span><i>eNB</i><b id=nenb>0</b></span>
    <span><i>TAC</i><b id=ntac>0</b></span>
    <span><i>TA목록</i><b id=ntal>0</b></span>
    <span><i>SGW</i><b id=nsgw>0</b></span>
    <span><i>MME</i><b id=nmme>0</b></span>
    <span id=scope class=dimtag></span>
    <span id=note class=warn></span>
  </div>
</header>
<div id=wrap>
  <svg id=svg>
    <defs>
      <!-- 커널. `primitiveUnits` 기본이 userSpaceOnUse 라 **도(degree) 단위**다 —
           0.0022도 ≈ 200m. 줌해도 지리적 폭이 유지되는 쪽이 맞다(KDE 대역폭은
           화면 거리가 아니라 지상 거리다) -->
      <filter id=kdeblur x="-25%" y="-25%" width="150%" height="150%">
        <feGaussianBlur stdDeviation="0.0022"/>
      </filter>
    </defs>
    <g id=gAdm></g><g id=gGreen></g><g id=gRoad></g><g id=gRiv></g><g id=gRail></g><g id=gYmt></g><g id=gAll></g><g id=gKde></g><g id=gCell></g>
    <g id=gPick></g><g id=gStn></g><g id=gNode></g></svg>
  <div id=sel></div>
  <div id=labels></div>
  <div id=tip></div><div id=legend></div><div id=scale></div><div id=src></div>
</div>
<script>
// ── 투영 — 웹 메르카토르. 도시 축척이라 간단한 식으로 충분하다 ──────────
const R=180/Math.PI;
const mx=(lon)=>lon, my=(lat)=>R*Math.log(Math.tan(Math.PI/4+lat*Math.PI/360));
const el=(n)=>document.createElementNS('http://www.w3.org/2000/svg',n);
const svg=document.getElementById('svg'), tip=document.getElementById('tip');
let VB=null, HOME=null, D=null, GEO=null, ALL=null, RAIL=null, RIV=null;
let ROAD=null, GREEN=null;

// ── 보기 방식 — **무엇을 찍고 무엇으로 가를지** (사람 결정 2026-10-28) ──
//
// 전에는 `색` 셀렉트가 점 **색만** 바꿨다. 점은 늘 셀 7,694개였고 묶음은
// 늘 볼록껍질이었다. 보는 사람이 바꾸고 싶은 것은 색이 아니라 **무엇을
// 보느냐**였다.
//
//  unit  점을 무엇으로 찍나 (eNB 자리 / 셀)
//  axis  무엇으로 색을 가르나 ('none' 이면 단일 색)
//  kde   묶음을 밀도 면으로 칠하나
//  focus 코어를 주인공으로 두나 — 그 종류만 크고 진하게, 셀은 물러난다
//
// **TAC 는 아직 없다** — TA 목록과 묶을지 검토 중이다(보류, 2026-10-28).
// **`전체 Cell` 은 없다**(2026-10-28 사람 결정). 같은 eNB 의 셀은 좌표가
// 같아서(2,177국 전부) 셀로 찍어도 eNB 모드와 **그림이 똑같았다** — 같은
// 자리에 3.7겹이 쌓일 뿐이다. 셀 단위로 봐야 할 일은 점을 눌러서 한다.
const MODE={
  enb:  {unit:'enb',  axis:'none',    kde:false, focus:null},
  ta:   {unit:'enb',  axis:'ta_list', kde:true,  focus:null},
  sgw:  {unit:'enb',  axis:'sgw',     kde:false, focus:'sgw'},
  mme:  {unit:'enb',  axis:'mme',     kde:false, focus:'mme'},
};
const modeOf=()=>MODE[document.getElementById('mode').value]||MODE.enb;

//: eNB 대표점. **같은 eNB 의 셀은 좌표가 같다** — 2,177국 전부가 그렇고
//: 평균 3.7섹터, 6섹터국이 744개다(실측 2026-10-28). 셀로 찍으면 같은
//: 자리에 3.7겹이 쌓여 `d` 만 길어지고 무엇을 눌렀는지도 모른다.
//: TAC·TA목록·SGW·MME 도 eNB 안에서 갈리지 않아(실측 0건) 대표 셀의
//: 값을 그대로 쓴다.
let ENB=null;
function enbPoints(){
  if(ENB) return ENB;
  const m=new Map();
  for(const c of D.cells){
    const e=m.get(c.enb);
    if(e) e.n++; else m.set(c.enb, {...c, key:c.enb, n:1});
  }
  return ENB=[...m.values()];
}

//: 커널 대역폭(도). 0.0045도 ≈ 400m — eNB 간격(수백 m)보다 조금 커야
//: 낱낱의 점이 아니라 "영역"으로 읽힌다
const KDE_BW=0.0045;
function drawKde(axis){
  const box=document.getElementById('gKde');
  const grp={};
  for(const c of enbPoints()){
    const v=c[axis]; if(v===null||v==='') continue;
    (grp[v]=grp[v]||[]).push(c);
  }
  // **묶음마다 그룹을 하나씩** 둔다. 번짐(`filter`)은 그룹 단위로 한 번
  // 걸리고, 그 안에서 원들이 겹쳐 밀도가 쌓인다
  for(const [v,pts] of Object.entries(grp)){
    const wrap=el('g');
    wrap.setAttribute('class','kde');
    wrap.setAttribute('filter','url(#kdeblur)');
    for(const c of pts){
      const q=el('circle');
      q.setAttribute('cx',mx(c.lon)); q.setAttribute('cy',-my(c.lat));
      q.setAttribute('r',KDE_BW); q.setAttribute('fill',PAL[v]);
      q.setAttribute('fill-opacity','.3');
      wrap.appendChild(q);
    }
    box.appendChild(wrap);
  }
}

// ── 축척 LOD — **멀리서 다 그리면 아무것도 안 보인다** ──────────────────
//
// 도로를 등급 구분 없이 깔았더니 전체 뷰가 회색 덩어리가 되고, 정작
// 확대했을 때 새로 나올 것이 남지 않았다. 등급마다 "이보다 넓게 보면
// 그리지 않는다"를 정해 축척이 내려갈수록 가로망이 채워지게 한다.
//
// viewBox 단위는 **도(degree)** 다. 도쿄 위도에서 경도 1도는 약 90.4km.
const kmSpan=()=>VB?VB.w*90.4:1e9;
const ROAD_LOD={motorway:1e9, trunk:1e9, primary:14, secondary:7, tertiary:3};
//: 지금 화면이 몇 단계인지. 바뀔 때만 다시 그린다 — 줌할 때마다 17,708개를
//: 다시 만들면 손이 멈춘다
const lodOf=()=>{const k=kmSpan(); return k>14?0:k>7?1:k>3?2:3;};
let LOD=-1;

function setVB(v){VB=v; svg.setAttribute('viewBox',`${v.x} ${v.y} ${v.w} ${v.h}`);
  drawScale();
  // **단계가 바뀌면 draw 가 placeLabels 까지 한다** — 여기서 또 부르면 두 번이다
  if(D && lodOf()!==LOD){ draw(); return; }
  placeLabels();}

// ── 라벨 — 지도 좌표를 화면 픽셀로 옮겨 div 로 놓는다 ────────────────────
//
// `getScreenCTM()` 으로 변환한다. viewBox 를 직접 나누면 틀린다 —
// preserveAspectRatio 기본값이 `xMidYMid meet` 이라 남는 축에 여백이 붙는다.
let LABELS=[];
function placeLabels(){
  const box=document.getElementById('labels'); if(!box) return;
  const m=svg.getScreenCTM(); if(!m){ box.replaceChildren(); return; }
  const r=document.getElementById('wrap').getBoundingClientRect();
  // **겹치면 접는다.** 도쿄 중심부는 코어가 몰려 있어 라벨을 다 그리면
  // 서로 덮어 아무것도 못 읽는다. 먼저 놓인 것이 이긴다 — LABELS 순서가
  // 곧 우선순위다(묶음 → MME → SGW)
  // 범례가 덮는 자리는 애초에 비운다 — 반쯤 가린 글자는 읽다가 틀린다
  const lgb=document.getElementById('legend').getBoundingClientRect();
  const out=[], put=[[lgb.left-r.left-6, lgb.top-r.top-6,
                      lgb.right-r.left+6, lgb.bottom-r.top+6]];
  for(const L of LABELS){
    // 작아진 묶음은 라벨을 접는다 — 줌아웃하면 겹쳐서 못 읽는다
    if(L.w!==undefined && L.w*m.a < 46) continue;
    const x=L.x*m.a+m.e-r.left, y=L.y*m.d+m.f-r.top;
    if(x<-80||y<-40||x>r.width+80||y>r.height+40) continue;
    const fs=L.cls==='lb-h'?13:11, bw=L.t.length*fs*.62+10, bh=fs+8;
    // `dx` 가 있으면 왼쪽 정렬이다(역 이름) — 상자가 점 오른쪽에 선다
    const left=L.dx!==undefined, x0=left?x+L.dx*m.a+5:x-bw/2;
    const b=[x0, y-bh/2, x0+bw, y+bh/2];
    if(put.some(o=>b[0]<o[2]&&b[2]>o[0]&&b[1]<o[3]&&b[3]>o[1])) continue;
    put.push(b);
    const d=document.createElement('div');
    d.className='lb '+L.cls;
    d.style.left=(left?x0:x)+'px'; d.style.top=y+'px';
    d.textContent=L.t; out.push(d);
  }
  box.replaceChildren(...out);
}

//: 노선 색 — **이름 해시**로 판다. 129개 노선에 손으로 색표를 매기면
//: 노선이 하나 늘 때마다 표가 틀어진다. 채도 32% · 명도 64% 로 묶어
//: 어느 값이 걸려도 파스텔을 벗어나지 않는다 — 노선은 배경이고,
//: 장비(노랑)를 이기면 안 된다.
function lineColor(name){
  let h=0;
  for(let i=0;i<String(name).length;i++) h=(h*31+String(name).charCodeAt(i))|0;
  return `hsl(${((h%360)+360)%360} 32% 64%)`;
}

// ── 축척 ────────────────────────────────────────────────────────────
//
// 가로 1px 이 몇 m 인가로 센다. viewBox 의 x 는 **경도 그대로**라
// (`mx` 가 항등 함수다) 도쿄 위도에서 1도 ≈ 90.4km 를 곱하면 된다.
// 세로는 메르카토르라 늘어나 있지만, 축척 막대는 가로로만 읽는다.
function drawScale(){
  const box=document.getElementById('scale');
  if(!VB||!box) return;
  const w=document.getElementById('wrap').getBoundingClientRect().width;
  if(!w) return;
  const mpp=VB.w*90400/w;
  // 1·2·5 계열에서 **140px 를 넘지 않는** 가장 큰 눈금을 고른다
  const nice=[10,20,50,100,200,500,1000,2000,5000,10000,20000,50000];
  let m=nice[0];
  for(const v of nice) if(v/mpp<=140) m=v;
  box.innerHTML=`<i style="width:${(m/mpp).toFixed(1)}px"></i>`
    +`<b>${m>=1000?(m/1000)+' km':m+' m'}</b>`;
}

// **색은 축 안에서만 뜻이 있다.** 값 목록을 정렬해 순번으로 색을 준다 —
// 축을 바꾸면 색이 통째로 바뀌는 것이 맞다. 같은 색이 늘 같은 TA 를
// 가리키는 척하면 축을 바꿨을 때 사람이 옛 뜻으로 읽는다.
let PAL={};
function buildPal(axis){
  if(axis==='none'){ PAL={}; return []; }
  const vals=[...new Set(D.cells.map(c=>c[axis]).filter(v=>v!==null&&v!==''))]
    .sort((a,b)=>String(a).localeCompare(String(b),undefined,{numeric:true}));
  // 채도 68% 는 셀이 7,694개가 되자 눈이 아팠다 — 구분만 되면 된다
  PAL={}; vals.forEach((v,i)=>PAL[v]=`hsl(${Math.round(i*360/Math.max(1,vals.length))} 42% 62%)`);
  return vals;
}
function legend(axis, vals){
  const L=document.getElementById('legend');
  // **단일 색일 때는 범례가 거짓말이 된다** — 색이 아무 뜻도 없는데
  // 색표를 띄우면 뜻이 있는 것처럼 읽힌다
  if(axis==='none'){ L.style.display='none'; return; }
  L.style.display='';
  const label={ta_list:'TA 목록', tac:'TAC', sgw:'SGW', mme:'MME'}[axis];
  // **몇 개인지가 아니라 얼마나 쏠렸는지가 중요하다.** 색만 9개 보이면
  // 고르게 나뉜 것처럼 읽히는데, 실제로는 한 값이 89% 를 먹고 있었다
  const cnt={}; for(const c of D.cells){ const v=c[axis];
    if(v!==null&&v!=='') cnt[v]=(cnt[v]||0)+1; }
  const tot=Object.values(cnt).reduce((a,b)=>a+b,0)||1;
  const ord=[...vals].sort((a,b)=>(cnt[b]||0)-(cnt[a]||0));
  const head=`<h4>${label} · ${vals.length}개</h4>`;
  // 많으면 상위만 — 다 적으면 범례가 화면을 덮는다. **자른 사실을 말한다**
  const top=ord.slice(0,18);
  L.innerHTML = head + top.map(v=>{
    const pc=Math.round((cnt[v]||0)*100/tot);
    return `<div><s style="background:${PAL[v]}"></s>${v}`
      + `<b style="margin-left:auto;opacity:${pc>=50?'.95':'.5'};font-weight:${pc>=50?700:400}">`
      + `${cnt[v]||0}${pc>=10?` · ${pc}%`:''}</b></div>`;
  }).join('')
    + (vals.length>top.length ? `<div style="opacity:.55">… 외 ${vals.length-top.length}개</div>` : '');
}

// ── 야마노테선 집기 ─────────────────────────────────────────────────────
//
// **노선명만으로는 안 된다.** N02 의 `山手線` 은 공식 선명이라 品川→渋谷→
// 新宿→池袋→田端 뿐이고, 운행상의 순환선 나머지는 `東海道線`(東京~品川)과
// `東北線`(田端~東京)이다. 반대로 이름을 셋 다 넣으면 센다이·아타미까지
// 딸려 온다.
//
// 그래서 **이름 ∧ 코리더** 로 집는다 — 역 30개를 이은 선에서 400m 안.
// 실측(2026-10-28): 山手 46/50 · 東北 37/124 · 東海道 30/114 구간.
// 지하철도 코리더에 들어오지만 이름에서 걸러진다.
//: 山手線 은 **통째로** 순환선이다 — 코리더를 적용하지 않는다.
//: 나머지 둘은 센다이·아타미까지 가므로 코리더로 잘라야 한다.
const YMT_WHOLE = ['山手線'];
const YMT_CUT = ['東北線', '東海道線'];
const YMT_R = 700;       // m. 코리더는 역을 이은 **직선**이라 곡선 구간에서 벌어진다

function markYamanote(){
  const loop = (D.lines && D.lines[0] && D.lines[0].pts) || [];
  if(!RAIL || loop.length < 3) return;
  const seg = loop.map((p,i)=>[p, loop[(i+1)%loop.length]]);
  const near = (p)=>{
    const cy=Math.cos(p[1]*Math.PI/180);
    let best=1e9;
    for(const [a,b] of seg){
      const ax=a[0]*cy, bx=b[0]*cy, px=p[0]*cy;
      const dx=bx-ax, dy=b[1]-a[1], L=dx*dx+dy*dy;
      const t=L? Math.max(0,Math.min(1,((px-ax)*dx+(p[1]-a[1])*dy)/L)) : 0;
      best=Math.min(best, Math.hypot(px-(ax+t*dx), p[1]-(a[1]+t*dy)));
    }
    return best*111320;
  };
  for(const r of RAIL){
    r.ymtSegs = null;
    if(!/旅客鉄道/.test(r.op)) continue;
    if(YMT_WHOLE.includes(r.name)){ r.ymtSegs = r.segs; continue; }
    if(!YMT_CUT.includes(r.name)) continue;
    // **점 단위로 자른다.** 구간 전체가 코리더 안이어야 통과시켰더니
    // 조금만 벗어나도 통째로 빠져 **선이 끊어진 채로** 나왔다.
    const keep = [];
    for(const sg of r.segs){
      let cur = [];
      for(const p of sg){
        if(near(p) < YMT_R) cur.push(p);
        else { if(cur.length > 1) keep.push(cur); cur = []; }
      }
      if(cur.length > 1) keep.push(cur);
    }
    r.ymtSegs = keep;
  }
}

function draw(){
  if(!D) return;
  LOD=lodOf();                       // 이 단계로 그린다 — `setVB` 가 비교한다
  svg.setAttribute('data-lod', LOD);  // 도로 굵기를 CSS 가 단계별로 집는다
  for(const id of ['gAdm','gGreen','gRoad','gRiv','gRail','gYmt','gAll','gKde','gCell','gPick','gStn','gNode'])
    document.getElementById(id).replaceChildren();
  LABELS=[];
  if(GEO) for(const f of GEO.features) for(const ring of f.rings){
    const q=el('path'); q.setAttribute('class','adm');
    q.setAttribute('d','M'+ring.map(c=>`${mx(c[0])} ${-my(c[1])}`).join('L')+'Z');
    q.dataset.t=f.name; document.getElementById('gAdm').appendChild(q);
  }
  // 하천·철도 — **국토수치정보 실측 선형.** 수가 많아 레이어마다
  // path 하나로 합친다(셀 2만 개를 노드 하나로 그리는 것과 같은 이유)
  {    // 배경·마스터·코어·지명은 **늘 그린다** — 끄는 자리를 없앴다(2026-10-28)
    const put=(gid,cls,items)=>{
      if(!items||!items.length) return;
      const d=items.map(sg=>'M'+sg.map(p=>`${mx(p[0])} ${-my(p[1])}`).join('L')).join('');
      if(!d) return;
      const q=el('path'); q.setAttribute('class',cls); q.setAttribute('d',d);
      document.getElementById(gid).appendChild(q);
      return q;
    };
    // 공원·수역 — **면이라 축척을 안 탄다.** 368개뿐이고, 멀리서도
    // 녹지·물은 "거기가 어디인지"를 알려주는 표식이다
    for(const g of (GREEN||[])){
      const d=(g.rings||[]).map(r=>'M'+r.map(p=>`${mx(p[0])} ${-my(p[1])}`)
        .join('L')+'Z').join('');
      if(!d) continue;
      const q=el('path'); q.setAttribute('class','gr gr-'+g.kind);
      q.setAttribute('d',d); document.getElementById('gGreen').appendChild(q);
    }
    // 도로 — 등급마다 보이기 시작하는 축척이 다르다(`ROAD_LOD`)
    const km=kmSpan();
    for(const r of (ROAD||[])){
      if(km > (ROAD_LOD[r.cls]??0)) continue;
      put('gRoad','rd rd-'+r.cls, r.segs);
    }
    put('gRiv','rv', (RIV||[]).flatMap(r=>r.segs));
    // JR 과 사철을 갈라 그린다 — 한 색이면 뼈대가 안 보인다.
    // 야마노테 구간은 빼서 **위 레이어에** 따로 그린다
    // 야마노테로 집힌 것은 위 레이어가 그리므로 아래에서는 뺀다.
    // `山手線` 은 통째로 올라가고, 나머지 둘은 잘린 조각만 올라간다
    const other=(r)=>(YMT_WHOLE.includes(r.name) && r.ymtSegs) ? [] : (r.segs||[]);
    // **노선마다 색이 다르다**(사람 결정 2026-10-28). 전에는 JR 과 사철
    // 둘로만 갈라 회색 두 톤이었고, 어느 선이 어디로 가는지 눈으로
    // 따라갈 수 없었다. 야마노테선만 JR 라인컬러를 그대로 쓴다
    for(const r of (RAIL||[])){
      const segs=other(r);
      if(!segs.length) continue;
      const q=put('gRail','rl', segs);
      if(q){ q.setAttribute('stroke', lineColor(r.name||r.op));
             q.dataset.t=`${r.op}  ${r.name}`; }
    }
    put('gYmt','rl rl-ymt', (RAIL||[]).flatMap(r=>r.ymtSegs||[]));
  }

  // 마스터 전량 — **좁힌 사실이 보여야 한다.** 생성 대상만 그렸더니
  // "신주쿠에만 기지국이 있나"로 읽혔다
  if(ALL){
    const q=el('path'); q.setAttribute('class','allpt');
    q.setAttribute('d', ALL.pts.map(p=>`M${mx(p[0])} ${-my(p[1])}l0 0`).join(''));
    document.getElementById('gAll').appendChild(q);
  }
  // **모사 경로(역을 직선으로 이은 선)는 그리지 않는다** (2026-10-28).
  // 실제 선로 레이어가 들어온 뒤로는 둘이 겹쳐 보이는데 직선 쪽이 틀린
  // 그림이다. `D.lines` 는 **야마노테 구간을 집는 자**로만 쓴다.
  const M=modeOf(), axis=M.axis;
  const vals=buildPal(axis); legend(axis, vals);
  const k = VB ? VB.w/1000 : 1;        // 확대해도 점 크기가 유지되게

  // TA 묶음 — **선이 아니라 면이다**(사람 결정 2026-10-28).
  // 볼록껍질은 바깥쪽 몇 점만 이어 안이 비고, 두 묶음이 겹치면 어디까지가
  // 어느 쪽인지 읽히지 않았다. 기지국마다 커널을 얹어 밀도로 칠한다.
  if(M.kde){ drawKde(axis); }
  // 셀 → 코어로 잇는 선은 **지웠다**(2026-10-27). 셀이 촘촘해 부채꼴이
  // 화면을 덮기만 했고, "어디를 덮나"는 묶음 테두리가 이미 말한다.

  // **코어가 주인공인 모드에서는 셀이 물러난다.** SGW·MME 를 보려는데
  // eNB 2천 개가 같은 세기로 깔리면 무엇이 주인지 안 보인다
  const base = M.focus ? .3 : .8;
  for(const c of (M.unit==='enb' ? enbPoints() : D.cells)){
    const d=el('circle');
    d.setAttribute('cx',mx(c.lon)); d.setAttribute('cy',-my(c.lat));
    d.setAttribute('r',(M.unit==='enb'?4:4.5)*k);
    // 축이 없을 때의 기본색은 **마스터 전량과 또렷이 갈려야 한다** —
    // 둘 다 회색이면 무엇이 생성 대상인지 안 보인다
    // **파스텔 노랑이 기본이다.** 회색(#9aa8bb)이던 때는 도로와 명도가
    // 겹쳐 가로망 위에서 점이 사라졌다. 색축이 있는 모드는 팔레트가 이긴다
    d.setAttribute('fill',PAL[c[axis]] || '#f7eec7');
    d.setAttribute('fill-opacity',String(base));
    d.dataset.kind=M.unit; d.dataset.id=c.key; d.dataset.enb=c.enb;
    d.dataset.t = (M.unit==='enb'
      ? `eNB ${c.enb}  (셀 ${c.n})`
      : `${c.key}  (${c.name})`)
      + `\nTAC ${c.tac} · TA목록 ${c.ta_list}\nSGW ${c.sgw} · MME ${c.mme}`;
    document.getElementById('gCell').appendChild(d);
  }
  for(const kind of ['mme','sgw']) for(const n of D.nodes[kind]){
    // 주인공은 키우고, 곁다리는 물린다. MME 모드에서 SGW 를 **지우지는
    // 않는다** — 누르면 그 아래 SGW 까지 함께 진해지는 것이 보여야 한다
    const hot = M.focus===kind;
    const g=el('g'), r=(kind==='mme'?15:10)*(hot?1.45:1)*k;
    if(M.focus && !hot) g.setAttribute('opacity','.3');
    const sh=el(kind==='mme'?'rect':'circle');
    if(kind==='mme'){ sh.setAttribute('x',mx(n.lon)-r); sh.setAttribute('y',-my(n.lat)-r);
      sh.setAttribute('width',2*r); sh.setAttribute('height',2*r); }
    else { sh.setAttribute('cx',mx(n.lon)); sh.setAttribute('cy',-my(n.lat));
      sh.setAttribute('r',r); }
    sh.setAttribute('fill', kind==='mme' ? '#c9797f' : '#5b8fbf');

    sh.dataset.kind=kind; sh.dataset.id=n.id;
    sh.dataset.t=`${kind.toUpperCase()} ${n.id}\n셀 ${n.cells} · eNB ${n.enb}\n(위치 = 맡은 셀의 중심)`;
    g.appendChild(sh); document.getElementById('gNode').appendChild(g);
    LABELS.push({x:mx(n.lon), y:-my(n.lat)-r*2.1,
                 t:`${kind.toUpperCase()} ${n.id}`, cls:'lb-n'});
  }
  // **장비가 먼저, 지명·역은 남은 자리에.** 이 화면의 주인공은 장비고
  // 역·구 이름은 "거기가 어디냐"를 알려주는 배경이다. 반대로 했더니
  // 역 이름이 SGW 31개 중 26개를 밀어냈다
  {
    for(const st of (D.stations||[])){
      // **점은 SVG 에 찍는다.** 전에는 HTML 라벨의 ::before 로 찍었는데,
      // 라벨 상자가 `translate(-50%,-50%)` 로 중앙정렬이라 **글자가 길수록
      // 점이 왼쪽으로 밀렸다** — 좌표가 틀린 것처럼 보인 원인이다
      // **건물 모양이다**(사람 결정 2026-10-28). 노란 점이던 때는 장비
      // 점과 섞여 "이것도 장비인가"로 읽혔다 — 색을 달리하는 것보다
      // 형태를 달리하는 쪽이 확실히 갈린다
      const sx=mx(st.lon), sy=-my(st.lat), sw=5*k, sh2=6*k;
      const c=el('path');
      c.setAttribute('d', `M${sx-sw} ${sy+sh2*.5} L${sx-sw} ${sy-sh2*.1} `
        + `L${sx} ${sy-sh2*.75} L${sx+sw} ${sy-sh2*.1} L${sx+sw} ${sy+sh2*.5} Z`);
      // **야마노테선 색을 띤다**(사람 결정 2026-10-28). 지금 역은 전부
      // 야마노테선 역이라(`data/lines/`) 선과 같은 계열로 두면 "이 선
      // 위의 역"이 한 묶음으로 읽힌다. 선(#9acd32)보다 흰색 쪽으로
      // 당겨 역이 선보다 밝게 뜬다
      c.setAttribute('fill','#e2efc4');
      c.dataset.t=`${st.name}역\n덮는 셀 ${st.cells}`;
      document.getElementById('gStn').appendChild(c);
      // 글자는 점 **오른쪽**에 — 왼쪽 끝을 점에 맞춘다
      LABELS.push({x:mx(st.lon), y:-my(st.lat), t:st.name, cls:'lb-s',
                   dx:11*k});
    }
  }
  if(GEO){
    for(const f of GEO.features){
      if(!f.name || !f.at) continue;
      LABELS.push({x:mx(f.at[0]), y:-my(f.at[1]),
                   t:(f.city && f.city!==f.name ? f.city+' '+f.name : f.name),
                   cls:'lb-a'});
    }
  }
  placeLabels(); drawPick();
}

async function load(){
  D=await (await fetch('/api/map')).json();
  ENB=null;                           // 셀이 바뀌면 eNB 묶음도 다시 만든다
  const c=D.counts;
  document.getElementById('ncell').textContent=D.cells.length;
  document.getElementById('nenb').textContent=c.enb;
  document.getElementById('ntac').textContent=c.tac;
  document.getElementById('ntal').textContent=c.ta_list;
  document.getElementById('nsgw').textContent=c.sgw;
  document.getElementById('nmme').textContent=c.mme;
  document.getElementById('note').textContent =
    D.unmapped ? `좌표 없는 셀 ${D.unmapped}` : '';
  if(!HOME && D.cells.length){
    const xs=D.cells.map(c=>mx(c.lon)), ys=D.cells.map(c=>-my(c.lat));
    const x0=Math.min(...xs),x1=Math.max(...xs),y0=Math.min(...ys),y1=Math.max(...ys);
    const px=(x1-x0)*.12+.002, py=(y1-y0)*.12+.002;
    HOME={x:x0-px,y:y0-py,w:(x1-x0)+2*px,h:(y1-y0)+2*py}; setVB({...HOME});
    // **마스터를 먼저 받아 그 범위로 경계를 받는다.** 생성 범위로만 받으면
    // `전체 보기` 에서 도쿄 바깥이 경계 없는 검은 바다로 남는다
    ALL=await (await fetch('/api/map/all')).json();
    const la=[...D.cells.map(c=>c.lat), ...(ALL.pts||[]).map(p=>p[1])];
    const lo=[...D.cells.map(c=>c.lon), ...(ALL.pts||[]).map(p=>p[0])];
    const g=await (await fetch(`/api/map/geo?bbox=${Math.min(...la)-.15},`
      +`${Math.min(...lo)-.15},${Math.max(...la)+.15},${Math.max(...lo)+.15}`)).json();
    GEO=g.features?g:null;
    const src=[g.source||g.error||''];
    for(const [nm,set] of [['rail',v=>RAIL=v],['rivers',v=>RIV=v],
                          ['roads',v=>ROAD=v],['green',v=>GREEN=v]]){
      const r=await (await fetch(`/api/map/layer/${nm}`)).json();
      set(r.items||[]);
      if(r.source && !src.includes(r.source)) src.push(r.source);
      // 레이어가 없어도 지도는 선다 — 다만 **왜 없는지는 말한다**
      if(r.error) document.getElementById('note').textContent=r.error;
    }
    // 출처 표기는 **약관 의무다.** 레이어를 늘리면 여기도 늘어야 한다
    document.getElementById('src').textContent=src.filter(Boolean).join('  ·  ');
    markYamanote();
  }
  // **점 하나는 셀이 아니라 자리(eNB)다.** 셀 수만 적으면 "2만 개가 왜
  // 이것밖에 안 보이나"가 된다 — 겹친 섹터는 한 점이다
  document.getElementById('scope').textContent =
    `생성 범위 ${D.scope} — ` +
    (ALL ? `마스터 ${ALL.enb.toLocaleString()} eNB / `
         + `${ALL.cells.toLocaleString()}셀 중 ` : '')
    + `${D.counts.enb} eNB / ${D.cells.length}셀`;
  if(true){
  }
  draw();
}

// ── 고르면 계통을 잇는다 ────────────────────────────────────────────────
//
// 선을 **상시로** 긋던 때는 셀이 촘촘해 부채꼴이 화면을 덮기만 했다
// (2026-10-27 에 지웠다). 고른 하나만 이으면 같은 정보가 읽힌다 —
// eNB·셀 → SGW → MME. 레지스트리에 PGW 가 셀 단위로 없어 거기까지다.
let PICK=null;
function pickOf(kind,id){
  if(!D) return null;
  let cells=[];
  if(kind==='cell'||kind==='enb'){
    // **같은 eNB 의 셀을 다 집는다** — 한 자리에 섹터가 6개씩 겹쳐 있어
    // 셀 하나만 집으면 무엇을 골랐는지 화면에서 안 보인다
    const e = kind==='enb' ? id : (D.cells.find(c=>c.key===id)||{}).enb;
    if(!e) return null;
    cells=D.cells.filter(x=>String(x.enb)===String(e)); }
  else cells=D.cells.filter(c=>String(c[kind])===String(id));
  if(!cells.length) return null;
  const sgw=[...new Set(cells.map(c=>c.sgw).filter(Boolean))];
  const mme=[...new Set(cells.map(c=>c.mme).filter(Boolean))];
  return {kind, id, cells, sgw, mme,
          enb:[...new Set(cells.map(c=>c.enb))],
          tac:[...new Set(cells.map(c=>c.tac).filter(Boolean))],
          ta:[...new Set(cells.map(c=>c.ta_list).filter(Boolean))]};
}
function drawPick(){
  const g=document.getElementById('gPick'); g.replaceChildren();
  const bar=document.getElementById('sel');
  // 고른 것이 없으면 `draw` 가 이미 모드별 기본 세기로 그려 뒀다
  if(!PICK){ bar.style.display='none'; return; }
  const k=VB?VB.w/1000:1;
  const by={}; for(const kind of ['sgw','mme']) for(const n of D.nodes[kind])
    by[kind+':'+n.id]=n;
  const seg=(a,b)=>{ if(!a||!b) return;
    const l=el('line'); l.setAttribute('class','pick');
    l.setAttribute('x1',mx(a.lon)); l.setAttribute('y1',-my(a.lat));
    l.setAttribute('x2',mx(b.lon)); l.setAttribute('y2',-my(b.lat));
    g.appendChild(l); };
  // ── 고른 계통에 불을 넣는다 (사람 결정 2026-10-28) ──────────────
  //
  // 전에는 고른 셀마다 노란 테두리 원을 그렸다. 섹터가 한 자리에 겹쳐
  // 원이 포개지고, **어느 장비가 걸렸는지 눈으로 세어야 했다.** TA
  // 히트맵과 같은 방식으로 영역째 밝힌다.
  const hotN=new Set([...PICK.sgw.map(v=>'sgw:'+v), ...PICK.mme.map(v=>'mme:'+v)]);
  const lit=el('g');
  lit.setAttribute('class','glow'); lit.setAttribute('filter','url(#kdeblur)');
  const litAt=(x,y,r)=>{ const q=el('circle');
    q.setAttribute('cx',x); q.setAttribute('cy',y); q.setAttribute('r',r);
    q.setAttribute('fill','#ffd43b'); q.setAttribute('fill-opacity','.5');
    lit.appendChild(q); };
  const seen=new Set();
  for(const c of PICK.cells){
    // **한 자리는 한 번만** — 6섹터국에 여섯 번 쌓으면 그 국만 타오른다
    if(seen.has(c.enb)) continue; seen.add(c.enb);
    litAt(mx(c.lon), -my(c.lat), KDE_BW*.75);
  }
  // 코어도 같이 — MME 를 고르면 그 아래 SGW 까지 밝아져야 한다
  for(const key of hotN){ const n=by[key]; if(n) litAt(mx(n.lon), -my(n.lat), KDE_BW*1.6); }
  g.appendChild(lit);

  // **셀은 자기 SGW 에만 잇는다.** 전에는 선택에 걸린 SGW 전부에 이어
  // MME 를 고르면 셀×SGW 개의 선이 나왔다 — 계통이 아니라 그물이었다
  const up=new Set();
  for(const c of PICK.cells){
    seg(c, by['sgw:'+c.sgw]);
    if(c.sgw && c.mme) up.add(c.sgw+'>'+c.mme);
  }
  for(const e of up){ const [a,b]=e.split('>');
    seg(by['sgw:'+a], by['mme:'+b]); }
  // **eNB 로 견준다** — 모드에 따라 점이 셀이기도 eNB 이기도 한데, 어느
  // 쪽이든 한 자리는 한 eNB 다. 셀 키로 보면 eNB 모드에서 하나도 안 걸린다
  const inPick=new Set(PICK.enb.map(String));
  for(const d of document.getElementById('gCell').children)
    d.setAttribute('fill-opacity', inPick.has(d.dataset.enb)?1:.12);
  // 코어도 함께 진해진다 — MME 를 고르면 **그 아래 SGW 까지**가
  // 사람이 보려던 것이다
  for(const n of document.getElementById('gNode').children){
    const sh=n.firstChild; if(!sh) continue;
    n.setAttribute('opacity',
      hotN.has(sh.dataset.kind+':'+sh.dataset.id) ? 1 : .18);
  }
  const lbl={cell:'eNB', enb:'eNB', sgw:'SGW', mme:'MME'}[PICK.kind];
  const head = (PICK.kind==='cell'||PICK.kind==='enb')
    ? `eNB ${PICK.enb[0]}` : `${lbl} ${PICK.id}`;
  bar.style.display='flex';   // CSS 기본이 none 이라 '' 로는 안 열린다
  bar.innerHTML=`<b>${head}</b>`
    + `<span>셀 ${PICK.cells.length}</span>`
    + (PICK.kind==='cell'||PICK.kind==='enb'?'':`<span>eNB ${PICK.enb.length}</span>`)
    + `<span>TAC ${PICK.tac.join(', ')||'—'}</span>`
    + `<span>TA목록 ${PICK.ta.join(', ')||'—'}</span>`
    + `<span>SGW ${PICK.sgw.join(', ')||'—'}</span>`
    + `<span>MME ${PICK.mme.join(', ')||'—'}</span>`
    + `<button id=unpick>해제</button>`;
  document.getElementById('unpick').onclick=()=>{PICK=null; draw();};
}

// ── 이동·확대 ────────────────────────────────────────────────────────────
const wrap=document.getElementById('wrap'); let drag=null;
let moved=0;
wrap.addEventListener('mousedown',e=>{drag={x:e.clientX,y:e.clientY,vb:{...VB}};
  moved=0; wrap.classList.add('drag');});
addEventListener('mouseup',()=>{drag=null;wrap.classList.remove('drag');});
addEventListener('mousemove',e=>{
  if(drag){ const r=wrap.getBoundingClientRect();
    moved=Math.max(moved,Math.abs(e.clientX-drag.x)+Math.abs(e.clientY-drag.y));
    setVB({...drag.vb, x:drag.vb.x-(e.clientX-drag.x)*drag.vb.w/r.width,
                       y:drag.vb.y-(e.clientY-drag.y)*drag.vb.h/r.height}); return; }
  const t=e.target.dataset&&e.target.dataset.t;
  if(t){ tip.style.display='block'; tip.textContent=t;
    tip.style.left=(e.clientX+12)+'px'; tip.style.top=(e.clientY-10)+'px'; }
  else tip.style.display='none';
});
wrap.addEventListener('wheel',e=>{
  e.preventDefault(); if(!VB) return;
  const r=wrap.getBoundingClientRect();
  const fx=(e.clientX-r.left)/r.width, fy=(e.clientY-r.top)/r.height;
  const z=e.deltaY>0?1.15:1/1.15;
  setVB({w:VB.w*z,h:VB.h*z,x:VB.x+(VB.w-VB.w*z)*fx,y:VB.y+(VB.h-VB.h*z)*fy});
  draw();
},{passive:false});

wrap.addEventListener('click',e=>{
  if(moved>4) return;        // 끌고 놓은 것은 클릭이 아니다
  const t=e.target.dataset||{};
  if(!t.kind){ return; }      // 빈 곳은 **해제하지 않는다** — 끌다 놓으면 지워진다
  const p=pickOf(t.kind,t.id);
  PICK = (PICK && PICK.kind===t.kind && String(PICK.id)===String(t.id)) ? null : p;
  // **다시 그린다.** 세기를 되돌리는 일을 `drawPick` 이 떠안으면 모드마다
  // 기본값이 달라 어긋난다 — 기본은 `draw` 한 곳이 정한다
  draw();
});
document.getElementById('reload').onclick=load;
// 보기를 바꾸면 **고른 것을 푼다.** SGW 를 고른 채 MME 보기로 넘어가면
// 바뀐 화면에 옛 선택의 테두리만 남아 무엇이 골라졌는지 안 읽힌다
document.getElementById('mode').onchange=()=>{ PICK=null; draw(); };
// **보이는 것 전체**에 맞춘다 — 마스터까지. 생성 범위로만 맞추면
// "전체 보기"가 전체를 안 보여준다
document.getElementById('reset').onclick=()=>{
  const xs=[], ys=[];
  for(const c of D.cells){ xs.push(mx(c.lon)); ys.push(-my(c.lat)); }
  if(ALL) for(const p of ALL.pts){ xs.push(mx(p[0])); ys.push(-my(p[1])); }
  if(!xs.length){ if(HOME){setVB({...HOME}); draw();} return; }
  // **동떨어진 몇 점이 시야를 통째로 늘린다.** 도쿄도에는 이즈제도가
  // 들어 있어 23,039셀 중 2셀이 120km 남쪽에 있고, 최대·최소로 맞추면
  // 화면이 거기까지 벌어져 정작 볼 것이 점 하나가 된다.
  // 1~99 백분위 폭의 두 배 밖은 뺀다 — **대신 몇 개를 뺐는지 적는다**
  const band=(v)=>{ const a=[...v].sort((p,q)=>p-q);
    const q0=a[Math.floor(a.length*.01)], q1=a[Math.ceil(a.length*.99)-1];
    const w=(q1-q0)||.01; return [q0-w, q1+w]; };
  const [bx0,bx1]=band(xs), [by0,by1]=band(ys);
  let out=0, x0=1e9,x1=-1e9,y0=1e9,y1=-1e9;
  for(let i=0;i<xs.length;i++){
    if(xs[i]<bx0||xs[i]>bx1||ys[i]<by0||ys[i]>by1){ out++; continue; }
    x0=Math.min(x0,xs[i]); x1=Math.max(x1,xs[i]);
    y0=Math.min(y0,ys[i]); y1=Math.max(y1,ys[i]);
  }
  const px=(x1-x0)*.06+.002, py=(y1-y0)*.06+.002;
  setVB({x:x0-px,y:y0-py,w:(x1-x0)+2*px,h:(y1-y0)+2*py}); draw();
  document.getElementById('note').textContent =
    out ? `화면 밖 ${out}셀 — 동떨어져 있어 범위에서 뺐다` : '';
};
load();
</script></body></html>
"""


@app.get("/map", response_class=HTMLResponse)
def map_page():
    return _HTML_MAP


@app.get("/topo", response_class=HTMLResponse)
def topo_page():
    return _HTML_TOPO

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
        # `local.yaml`(mysql.host 와 parquet root 만 적은 것)을 주면 생성
        # 모수를 전부 잃었다.
        from .cli import load_cfg
        global _CFG
        _CFG = load_cfg(args.config)

    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
