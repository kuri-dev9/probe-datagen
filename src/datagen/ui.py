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

from .control import (KINDS, add_fault, cancel_fault, ensure_tables, get_config,
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

_MYSQL: dict | None = None  # main() 이 --config 로 채운다. 미설정이면 기본 yaml.


def _mysql_cfg() -> dict:
    global _MYSQL
    if _MYSQL is None:
        p = HERE / "config" / "datagen.yaml"
        _MYSQL = yaml.safe_load(p.read_text(encoding="utf-8"))["mysql"]
    return _MYSQL


def _connect():
    """요청마다 새 커넥션 — pymysql 커넥션은 스레드 전용이라 공유하면
    커서 상태가 섞여 엉뚱한 자리에서 깨진다(FastAPI 는 동기 엔드포인트를
    스레드풀에서 돌린다). 요청당 연결 비용은 로컬/폐쇄망에서 무시할 수준."""
    m = _mysql_cfg()
    return pymysql.connect(
        host=m["host"], port=int(m["port"]), user=m["user"],
        password=m["password"], database=m["database"], charset="utf8mb4",
    )


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
            cur.execute("SELECT MAX(slot_ts) FROM kpi_cell_min")
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

    출처는 `file_ledger` 다 — `datagen_history.records` 는 총합이라 계열별로
    쪼갤 수 없다.
    """
    conn = _connect()
    try:
        with conn.cursor() as cur:
            where, args = _window(
                date, "slot_ts >= DATE_SUB(NOW(), INTERVAL %s MINUTE)", [minutes])
            cur.execute(
                "SELECT series, COUNT(*) slots, SUM(records) recs, "
                "       MIN(slot_ts) a, MAX(slot_ts) b "
                f"FROM file_ledger WHERE {where} "
                "GROUP BY series ORDER BY recs DESC", args)
            rows = [dict(zip([d[0] for d in cur.description], r)) for r in cur.fetchall()]
            cur.execute(
                "SELECT COUNT(*) slots, COALESCE(SUM(records),0) recs "
                f"FROM datagen_history WHERE {where}", args)
            tot_slots, tot_recs = cur.fetchone()
            # 날짜별 슬롯 수 — 상단에 "오늘 N분 / 누적 M분"
            cur.execute(
                "SELECT DATE(slot_ts) d, COUNT(*) slots, SUM(records) recs "
                "FROM datagen_history GROUP BY 1 ORDER BY 1 DESC LIMIT 7")
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
                date, "slot_ts >= DATE_SUB(NOW(), INTERVAL %s HOUR)", [hours])
            cur.execute(
                f"SELECT slot_ts, series, records FROM file_ledger WHERE {where} "
                "ORDER BY slot_ts", args)
            raw = cur.fetchall()
            cur.execute(
                f"SELECT slot_ts, records, faults FROM datagen_history WHERE {where} "
                "ORDER BY slot_ts", args)
            hist = cur.fetchall()
    finally:
        conn.close()

    by_slot: dict = {}
    for slot_ts, series, records in raw:
        k = slot_ts.isoformat()
        by_slot.setdefault(k, {})[series] = int(records or 0)
    points = []
    hist_map = {h[0].isoformat(): h for h in hist}
    for k in sorted(by_slot):
        h = hist_map.get(k)
        points.append({
            "ts": k,
            "series": by_slot[k],
            "total": sum(by_slot[k].values()),
            "faults": (h[2].split(",") if h and h[2] else []),
        })
    return {"hours": hours, "date": date, "points": points,
            "series_names": sorted({s for v in by_slot.values() for s in v})}


@app.get("/api/settings")
def api_settings():
    """생성 설정 — datagen_config(DB) 가 우선, 없으면 datagen.yaml 기본값.

    데몬이 틱마다 datagen_config 를 읽으므로 여기서 바꾸면 다음 분부터 반영된다
    (재기동 불필요). 장비 추가·삭제는 이 화면의 범위가 아니다 — 셀 재배정이
    필요해 설계가 따로 붙는다.
    """
    cfg = yaml.safe_load((HERE / "config" / "datagen.yaml").read_text(encoding="utf-8"))
    rates = dict(cfg.get("rates") or {})
    gen = dict(cfg.get("generate") or {})
    conn = _connect()
    try:
        over = {}
        for k in list(rates) + ["enabled", "retention_days"]:
            v = get_config(conn, f"rate.{k}" if k in rates else k)
            if v is not None:
                over[k] = v
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 'SGW', COUNT(DISTINCT sgw_id) FROM cell_master UNION ALL "
                "SELECT 'MME', COUNT(DISTINCT mme_id) FROM cell_master UNION ALL "
                "SELECT 'PGW', COUNT(DISTINCT pgw_id) FROM cell_master")
            nodes = {r[0]: r[1] for r in cur.fetchall()}
            cur.execute("SELECT COUNT(*) FROM cell_master")
            cells = cur.fetchone()[0]
    finally:
        conn.close()
    for k in rates:
        if k in over:
            rates[k] = int(over[k])
    return {
        "rates": rates,
        "enabled": str(over.get("enabled", "true")).lower() != "false",
        "retention_days": int(over.get("retention_days", gen.get("retention_days", 7))),
        "topology": {"cells": cells, **nodes},
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
    """대상 후보를 cell_master 에서 집계한다. core_node 가 아니라 cell_master 를
    보는 이유: 셀이 하나도 안 걸린 노드에 장애를 넣어 봐야 지표에 아무것도
    안 나타난다 — 영향 셀 수가 있는 것만 고르게 한다. 셀 수 내림차순이라
    "효과가 잘 보이는" 대상이 위에 온다."""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            out: dict = {}
            for key, col in (("sgw", "sgw_id"), ("mme", "mme_id"), ("pgw", "pgw_id")):
                cur.execute(
                    f"SELECT {col}, COUNT(*) FROM cell_master "
                    f"WHERE {col} IS NOT NULL GROUP BY {col} ORDER BY COUNT(*) DESC"
                )
                out[key] = [{"id": r[0], "cells": r[1]} for r in cur.fetchall()]
            cur.execute(
                "SELECT adm_sgg_cd, adm_sgg_nm, COUNT(*) FROM cell_master "
                "WHERE adm_sgg_cd IS NOT NULL GROUP BY adm_sgg_cd, adm_sgg_nm "
                "ORDER BY COUNT(*) DESC"
            )
            out["sgg"] = [{"code": r[0], "name": r[1], "cells": r[2]}
                          for r in cur.fetchall()]
    finally:
        conn.close()
    return out


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
                "SELECT fault_id,kind,target,start_ts,duration_min,intensity,"
                "mode,status,note,created_at,updated_at FROM datagen_fault "
                "WHERE start_ts >= %s AND start_ts < DATE_ADD(%s, INTERVAL 1 DAY) "
                "ORDER BY fault_id DESC LIMIT %s", [date, date, limit])
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
  <h2>생성 시계열 <small class="dim">분당 건수 · 최근 3시간</small></h2>
  <svg class="chart" id="chart" viewBox="0 0 1000 190" preserveAspectRatio="none"></svg>
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
const COLORS = { lte_call:"#4e9af1", transport:"#4caf7d",
                 http:"#d8a22a", dns:"#a872e0" };
function renderChart(d) {
  const P = d.points || [], svg = $("chart");
  if (!P.length) { svg.innerHTML =
    '<text x="500" y="95" fill="#8a94a3" text-anchor="middle">데이터 없음</text>';
    return; }
  const names = d.series_names || [];
  const W = 1000, H = 190, PAD_B = 22, PAD_T = 8;
  const max = Math.max(1, ...P.map(p => p.total));
  const bw = W / P.length;
  const y = v => PAD_T + (H - PAD_B - PAD_T) * (1 - v / max);
  let out = "";

  // 장애가 있던 분은 배경을 깔아 둔다 — 생성량 변화와 겹쳐 보려는 것이지,
  // 장애 판정을 보여주려는 게 아니다(판정은 관제 화면 몫).
  P.forEach((p, i) => { if (p.faults && p.faults.length)
    out += `<rect x="${(i*bw).toFixed(1)}" y="${PAD_T}" width="${Math.max(bw,1).toFixed(1)}"
             height="${H-PAD_B-PAD_T}" fill="#e5534b" opacity=".10"/>`; });

  // 계열별 적층 막대
  P.forEach((p, i) => {
    let acc = 0;
    names.forEach(n => {
      const v = (p.series || {})[n] || 0; if (!v) return;
      const y0 = y(acc + v), y1 = y(acc);
      out += `<rect x="${(i*bw).toFixed(1)}" y="${y0.toFixed(1)}"
               width="${Math.max(bw-0.4,0.6).toFixed(1)}" height="${Math.max(y1-y0,0.5).toFixed(1)}"
               fill="${COLORS[n] || "#6b7684"}"/>`;
      acc += v;
    });
  });

  // 날짜가 바뀌는 지점에 경계선 + 라벨, 그 외엔 정시마다 시각
  let prevDay = null;
  P.forEach((p, i) => {
    const day = p.ts.slice(0, 10), hhmm = p.ts.slice(11, 16);
    const x = (i * bw).toFixed(1);
    if (prevDay && day !== prevDay) {
      out += `<line x1="${x}" y1="${PAD_T}" x2="${x}" y2="${H-PAD_B}"
               stroke="#4e9af1" stroke-width="1"/>
              <text x="${+x+4}" y="${H-8}" fill="#4e9af1" font-size="11">${day}</text>`;
    } else if (hhmm.endsWith(":00")) {
      out += `<line x1="${x}" y1="${H-PAD_B}" x2="${x}" y2="${H-PAD_B+4}" stroke="#2c3542"/>
              <text x="${+x+3}" y="${H-8}" fill="#8a94a3" font-size="11">${hhmm}</text>`;
    }
    prevDay = day;
  });
  out += `<text x="4" y="${PAD_T+10}" fill="#8a94a3" font-size="11">${max.toLocaleString()}</text>`;
  svg.innerHTML = out;
  $("legend").innerHTML = names.map(n =>
    `<span><i style="background:${COLORS[n] || "#6b7684"}"></i>${esc(n)}</span>`).join("")
    + '<span><i style="background:#e5534b;opacity:.35"></i>장애 주입 구간</span>';
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
        global _MYSQL
        _MYSQL = yaml.safe_load(
            Path(args.config).read_text(encoding="utf-8"))["mysql"]

    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
