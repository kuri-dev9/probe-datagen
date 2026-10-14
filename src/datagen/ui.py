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

from .pemdb1 import XDR_TABLE
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
    global _CFG
    if _CFG is None:
        p = HERE / "config" / "datagen.yaml"
        _CFG = yaml.safe_load(p.read_text(encoding="utf-8"))
    return _CFG


def _pem_cfg() -> dict:
    return _cfg().get("pemdb1") or {}


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
        with conn.cursor() as cur:
            # 생성이 전진하는지는 **상용 원시 테이블**로 본다. 구 `kpi_cell_min`
            # 은 아무도 쓰지 않아 영원히 같은 값을 돌려줬다.
            # **테이블명을 박지 않는다.** 개명(`XDR_LTE_CALL_KPI` →
            # `GP_RAW_LTE_CALL_KPI`, 2026-10-14) 때 이 한 줄이 남아서 상태 바가
            # "마지막 적재 슬롯 —"·"서버 응답 없음"으로 떴다 — 화면에는 에러가
            # 아니라 **빈 값**으로 보여서 원인을 짚기 어려웠다.
            cur.execute(f"SELECT MAX(EVENT_TIME) FROM {XDR_TABLE}")
            last_slot = cur.fetchone()[0]
        faults = list_faults(conn, limit=100)
    finally:
        conn.close()
    return {
        "last_slot": last_slot,
        "active": [f for f in faults if f["status"] == "active"],
        "pending": [f for f in faults if f["status"] == "pending"],
        "recent_done": [f for f in faults if f["status"] == "done"][:10],
        "kinds": list(KINDS),
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
                date, "SLOT_TIME >= DATE_SUB(NOW(), INTERVAL %s MINUTE)", [minutes])
            cur.execute(
                "SELECT 'lte_call' series, COUNT(*) slots, "
                "       COALESCE(SUM(RECORD_CNT),0) recs, "
                "       MIN(SLOT_TIME) a, MAX(SLOT_TIME) b "
                f"FROM {T_HISTORY} WHERE {where}", args)
            rows = [dict(zip([d[0] for d in cur.description], r)) for r in cur.fetchall()]
            rows = [r for r in rows if r["slots"]]      # 빈 창에는 계열을 만들지 않는다
            cur.execute(
                "SELECT COUNT(*) slots, COALESCE(SUM(RECORD_CNT),0) recs "
                f"FROM {T_HISTORY} WHERE {where}", args)
            tot_slots, tot_recs = cur.fetchone()
            # 날짜별 슬롯 수 — 상단에 "오늘 N분 / 누적 M분"
            cur.execute(
                f"SELECT DATE(SLOT_TIME) d, COUNT(*) slots, SUM(RECORD_CNT) recs "
                f"FROM {T_HISTORY} GROUP BY 1 ORDER BY 1 DESC LIMIT 7")
            days = [dict(zip([d[0] for d in cur.description], r)) for r in cur.fetchall()]
    finally:
        conn.close()
    for r in rows:
        r["per_min"] = round(r["recs"] / r["slots"], 1) if r["slots"] else 0
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
                date, "SLOT_TIME >= DATE_SUB(NOW(), INTERVAL %s HOUR)", [hours])
            # `file_ledger` 를 쓰지 않는다 — 상용 스키마에 대응이 없다.
            # 계열은 `lte_call` 하나이므로 `CM_DATAGEN_HISTORY` 로 충분하다.
            cur.execute(
                f"SELECT SLOT_TIME, RECORD_CNT, FAULT_IDS, SERIES_STAT "
                f"FROM {T_HISTORY} WHERE {where} ORDER BY SLOT_TIME", args)
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
    cfg = _cfg()
    gen = dict(cfg.get("generate") or {})
    # **분당 호 수는 `pemdb1.rates` 가 정본이다.** 구 `rates:`(lte 2000 ·
    # transport/dns/http)는 자체 창고용이고, 상용 경로는 `lte_call` 하나를
    # 20,000/분으로 돌린다. 여기서 구 절을 보여 주면 운영자가 2000 을 저장해
    # 생성량이 10분의 1로 떨어진다 — 에러 없이 표본이 말라 탐지가 멈춘다.
    rates = {"lte_call": int((_pem_cfg().get("rates") or {}).get("lte_call", 2000))}
    conn = _connect()
    try:
        over = {}
        for k in list(rates) + ["enabled", "retention_days"]:
            v = get_config(conn, f"rate.{k}" if k in rates else k)
            if v is not None:
                over[k] = v
        cells_list = _cells(conn)
    finally:
        conn.close()
    nodes = {
        "SGW": len({c.sgw_id for c in cells_list}),
        "MME": len({c.mme_id for c in cells_list}),
        "PGW": len({c.pgw_id for c in cells_list}),
    }
    for k in rates:
        if k in over:
            rates[k] = int(over[k])
    return {
        "rates": rates,
        "enabled": str(over.get("enabled", "true")).lower() != "false",
        # 로컬 PoC 는 1일이다(사람 지시). yaml 기본값과 어긋나지 않게 1 로 둔다.
        "retention_days": int(over.get("retention_days", gen.get("retention_days", 1))),
        "topology": {"cells": len(cells_list), **nodes},
        "note": "장비 추가·삭제는 미지원 — 셀 재배정 설계가 선행되어야 한다",
    }


class SettingsReq(BaseModel):
    rates: dict[str, int] | None = None
    enabled: bool | None = None
    retention_days: int | None = None


@app.post("/api/settings")
def api_set_settings(req: SettingsReq):
    conn = _connect()
    try:
        if req.rates:
            for k, v in req.rates.items():
                if int(v) < 0:
                    raise HTTPException(400, f"분당 호 수는 음수가 될 수 없다: {k}")
                set_config(conn, f"rate.{k}", str(int(v)))
        if req.enabled is not None:
            set_config(conn, "enabled", "true" if req.enabled else "false")
        if req.retention_days is not None:
            set_config(conn, "retention_days", str(int(req.retention_days)))
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

<div class="bar">
  <span>마지막 적재 슬롯 <b id="last-slot">—</b></span>
  <span class="active-cnt">활성 장애 <b id="active-cnt">0</b> 건</span>
  <span class="dim" id="poll-err"></span>
  <span class="nav">
    <button type="button" id="d-prev" title="이전 날짜">◀</button>
    <b id="d-label">—</b>
    <button type="button" id="d-next" title="다음 날짜">▶</button>
    <button type="button" id="d-today" class="ghost">오늘</button>
  </span>
  <span style="margin-left:auto">
    <label class="sw" title="끄면 화면이 멈춘다 — 생성은 계속된다">
      <input type="checkbox" id="live" checked> 실시간 갱신
    </label>
  </span>
</div>

<div class="panel">
  <h2>장애 주입</h2>
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

<div class="grid2">
  <div class="panel">
    <h2>인입 현황 <small class="dim" id="ing-range">최근 60분</small></h2>
    <div id="ingest" class="dim">불러오는 중…</div>
  </div>
  <div class="panel">
    <h2>생성 설정 <small class="dim">다음 틱부터 반영된다</small></h2>
    <div class="rates" id="rates"></div>
    <div style="margin-top:12px; display:flex; gap:14px; align-items:center">
      <label class="sw"><input type="checkbox" id="gen-on"> 생성 가동</label>
      <label class="dim">보존일수 <input id="ret" type="number" min="1" max="90"
             style="width:64px"></label>
      <button type="button" onclick="saveSettings()">저장</button>
      <span class="dim" id="set-msg"></span>
    </div>
    <div class="dim" id="topo" style="margin-top:10px; font-size:12px"></div>
  </div>
</div>

<div class="panel">
  <h2>생성 시계열 <small class="dim">분당 건수 · 최근 3시간 · 오른쪽 끝이 현재</small></h2>
  <div class="chartwrap" id="chartwrap">
    <svg class="chart" id="chart" viewBox="0 0 1000 190" preserveAspectRatio="none"></svg>
    <div class="tip" id="tip"></div>
  </div>
  <div class="lg" id="legend"></div>
</div>

<div class="panel">
  <h2>장애 목록</h2>
  <table>
    <thead><tr><th>ID</th><th>종류</th><th>대상</th><th>시작</th>
               <th>지속</th><th>상태</th><th></th></tr></thead>
    <tbody id="rows"><tr><td colspan="7" class="dim">불러오는 중…</td></tr></tbody>
  </table>
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

async function poll() {
  try {
    const [st, faults] = await Promise.all([
      (await fetch("/api/status")).json(),
      (await fetch("/api/faults" + q({limit:50}))).json(),
    ]);
    $("last-slot").textContent = ts(st.last_slot);
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
// ── 날짜 이동 ───────────────────────────────────────────────────────────
// null 이면 "오늘(흐르는 창)", 문자열이면 그 하루를 고정해서 본다.
// 과거는 더 변하지 않으므로 그때는 실시간 갱신을 멈춘다 — 5초마다 다시 그려도
// 같은 그림이고 DB 만 때린다.
let DAY = null;          // "2026-10-12" | null
let DAYS = [];           // 데이터가 있는 날짜 (최신순) — /api/ingest 의 by_day
const q = extra => {
  const u = new URLSearchParams(extra || {});
  if (DAY) u.set("date", DAY);
  const qs = u.toString();
  return qs ? "?" + qs : "";
};
const isPast = () => DAY !== null;

function renderNav() {
  const label = DAY || (DAYS[0] ?? "—");
  $("d-label").textContent = label === "—" ? "—" : `${label} (${dow(label)})`;
  $("d-label").className = isPast() ? "past" : "";
  // DAYS 는 최신순이다. 이전 = 인덱스 +1, 다음 = -1.
  const cur = DAY ? DAYS.indexOf(DAY) : 0;
  $("d-prev").disabled = cur < 0 || cur + 1 >= DAYS.length;
  $("d-next").disabled = !DAY || cur <= 0;
  $("d-today").disabled = !DAY;
  $("live").disabled = isPast();
  $("live").parentElement.title = isPast()
    ? "과거 날짜를 보는 중 — 갱신할 것이 없다"
    : "끄면 화면이 멈춘다 — 생성은 계속된다";
}
function goDay(d) {
  DAY = d;                       // null 이면 오늘로 복귀
  renderNav();
  poll(); loadHeavy(true);
}
$("d-prev").addEventListener("click", () => {
  const cur = DAY ? DAYS.indexOf(DAY) : 0;
  if (cur + 1 < DAYS.length) goDay(DAYS[cur + 1]);
});
$("d-next").addEventListener("click", () => {
  // 날짜로만 이동한다. 실시간(흐르는 창)으로 돌아가는 건 [오늘] 버튼의 몫 —
  // 가장 최신 날짜가 반드시 오늘이라는 보장이 없다(생성이 멈춰 있었을 수 있다).
  const cur = DAYS.indexOf(DAY);
  if (cur > 0) goDay(DAYS[cur - 1]);
});
$("d-today").addEventListener("click", () => goDay(null));

// ── 실시간 토글 ─────────────────────────────────────────────────────────
// 끄면 화면만 멈춘다 — 생성 데몬은 계속 돈다. 시연 중 화면을 고정해 두고
// 설명할 때 쓴다. 선택은 브라우저에 기억시킨다.
const LIVE_KEY = "datagen.live";
function isLive() { return $("live").checked && !isPast(); }
$("live").checked = localStorage.getItem(LIVE_KEY) !== "0";
$("live").addEventListener("change", () => {
  localStorage.setItem(LIVE_KEY, isLive() ? "1" : "0");
  if (isLive()) { poll(); loadHeavy(true); }
});

// ── 인입 현황 ───────────────────────────────────────────────────────────
function renderIngest(d) {
  const tot = d.total || {};
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
  if (days.length && days.join() !== DAYS.join()) { DAYS = days; renderNav(); }
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
  const svg = $("chart"), P = d.points || [];
  const { W, H, PAD_B, PAD_T } = CHART;
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

  // 장애 구간 배경 — 생성량 변화와 겹쳐 보려는 것이지 장애 판정이 아니다.
  byIdx.forEach((p, i) => { if (p.faults && p.faults.length)
    out += `<rect x="${(i*bw).toFixed(2)}" y="${PAD_T}" width="${bw.toFixed(2)}"
             height="${H-PAD_B-PAD_T}" fill="#e5534b" opacity=".10"/>`; });

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
  if (!P.length) out += `<text x="500" y="95" fill="#8a94a3" text-anchor="middle"
                          font-size="12">데이터 없음</text>`;
  svg.innerHTML = out;

  CHART_SLOTS = { slots, t0, bw, byIdx, names };
  $("legend").innerHTML = names.map(n =>
    `<span><i style="background:${COLORS[n] || "#6b7684"}"></i>${esc(n)}</span>`).join("")
    + '<span><i style="background:#e5534b;opacity:.35"></i>장애 주입 구간</span>';
}

// ── 툴팁 ────────────────────────────────────────────────────────────────
// SVG 는 `preserveAspectRatio="none"` 로 가로가 늘어나므로 **픽셀 좌표를
// viewBox 좌표로 환산**해야 한다. 안 그러면 마우스 위치와 집히는 슬롯이
// 오른쪽으로 갈수록 어긋난다.
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
let RATE_KEYS = [];
function renderSettings(d) {
  RATE_KEYS = Object.keys(d.rates || {});
  $("rates").innerHTML = RATE_KEYS.map(k =>
    `<div><label>${esc(k)} (분당)</label>
       <input id="rate-${esc(k)}" type="number" min="0" step="100"
              value="${d.rates[k]}"></div>`).join("");
  $("gen-on").checked = !!d.enabled;
  $("ret").value = d.retention_days;
  const t = d.topology || {};
  $("topo").textContent =
    `토폴로지 — 셀 ${t.cells} · SGW ${t.SGW} · MME ${t.MME} · PGW ${t.PGW}` +
    (d.note ? ` · ${d.note}` : "");
}
async function saveSettings() {
  const rates = {};
  RATE_KEYS.forEach(k => { rates[k] = Number($("rate-" + k).value || 0); });
  const body = { rates, enabled: $("gen-on").checked,
                 retention_days: Number($("ret").value || 7) };
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
  renderNav();
  loadHeavy(true);
})();

// 툴팁은 래퍼에 건다 — SVG 에 걸면 막대 사이 빈 공간에서 이벤트가 끊겨
// 커서를 가로로 움직일 때 툴팁이 깜빡인다.
$("chartwrap").addEventListener("mousemove", chartHover);
$("chartwrap").addEventListener("mouseleave", () => { $("tip").style.opacity = 0; });

// **1분마다 다시 그린다.** 축이 현재 시각 기준이라 데이터가 안 바뀌어도
// 분이 지나면 한 칸 밀려야 한다 — 안 그러면 "now" 선이 과거에 머문다.
setInterval(() => { if (isLive() && LAST_SERIES) renderChart(LAST_SERIES); }, 60000);

setInterval(() => { if (isLive()) poll(); }, 5000);
setInterval(() => { if (isLive()) loadHeavy(true); }, 15000);
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
        global _CFG
        _CFG = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))

    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
