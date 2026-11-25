"""시연 리플레이(`/api/replay/*`) — 장애 하나를 시간축으로 다시 보여 주는 재료 (2026-11-24 사용자 「A·B·C 해 보자」).

| 경로 | 화면 | 재료 |
| --- | --- | --- |
| `/api/replay/{fid}/timeline` | C. 탐지 추적 리플레이 | 장애 행(`CM_DATAGEN_FAULT`) · graphio `/api/events/by-fault` · 첫 이상징후 |
| `/api/replay/{fid}/timelapse` | B. 장애 타임랩스(1분) | graphio 셀 이상 판정(`AI_ANOMALY_EVENT`) |
| `/api/replay/{fid}/route` | A. 전철을 따라 번지는 장애 | 가입자 1분 행의 전환 쌍(`PM_COR_XP_SUB_1M` 떠난 셀 → 분 끝 셀) |

**새 산식을 만들지 않는다**(graphio 불변규칙 1). 셀 상태는 graphio 탐지가 매긴 등급 그대로이고(지도 셀 음영
`map_api._cell_severity` 와 같은 표 · 같은 색 규칙), 전환은 사람 수를 셀 뿐 비율을 새로 짓지 않는다. 이상 판정이
없는 분·셀은 「정상」이 아니라 **「판정 없음」(null)** 이다 — 표본 부족으로 판정하지 않은 분과 갈리지 않는다
(불변규칙 17).

**열차 위치는 없다.** 생성기의 탑승자 상태는 gen 프로세스 메모리에만 있고 남지 않는다. 원시 Parquet 는 10분만
남는다. 그래서 A 는 **실제로 셀을 넘어간 가입자 흐름**(떠난 셀 → 도착 셀 · 1분)을 내린다 — 화면이 그 쌍을 따라 점을
움직이면 전철 무리가 노선을 따라 이동하는 모습이 되고, 그 점 하나하나가 데이터에 있는 사실이다. 지어낸 열차가 아니다.

**장애 셀**은 정답(`truth_out` · 가장 최근 장애만 남는다)과 그 장애에 걸친 graphio 사건의 셀을 합친 것이다.
지난 장애는 정답 파일에 없어 사건 셀만 남는다 — 응답의 `cells_src` 가 어느 쪽인지 말한다.

graphio 표는 **읽기만** 한다. 사건은 HTTP 로 받는다(`map_api.api_map_faults` 머리말의 이유). 셀 이상 판정과 가입자
1분 행은 지도 셀 음영이 이미 직접 읽는 것과 같은 방식이다.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, HTTPException

from ..control import FAULT_SELECT, T_FAULT
from .common import HERE, _connect, _pem_cfg, truth_codes
from .faults_api import _truth_of
from .map_api import _map_cells


router = APIRouter()

#: 앞뒤로 더 보는 분 — 화면이 평소 상태에서 시작해 끝난 뒤 가라앉는 모습까지 보이게. **초기값이다**
PAD_BEFORE_MIN = 5
PAD_AFTER_MIN = 10
#: 사건 셀을 묻는 사건 수 상한 — 사건마다 graphio 를 한 번씩 부른다(1초 안팎 목표)
EVENT_CELL_LOOKUPS = 6
#: 장애 끝 뒤에도 「이 장애 것」으로 보는 사건 열림 유예(분) — 이상징후는 1분 집계의 분이라 끝 분을 덮는 폭이다.
#: 끝난 뒤 새로 열린 사건은 이 장애 것이 아니다(2026-11-24 F-597 · 07:45~07:52 무관 조각·야마노테 통합장애). **초기값이다**
OWN_GRACE_MIN = 1
#: 등급 순위 — 지도(`map.html` `_SEV_RANK`)와 같다
_RANK = {"CRITICAL": 4, "MAJOR": 3, "MINOR": 2, "WARNING": 2, "WARN": 2, "INFO": 1}
#: 통화 끊김(무호) 지표 — 지도 셀 음영의 회색이다(`map.html` 셀 상태 규칙)
SILENT_KPI = "C-01"
#: 이상 판정 표의 가장 이른 기록을 다시 묻는 간격(초) — 보관(graphio `partitions.retain_days`)은 하루 칸 단위로 떨어진다. **초기값이다**
EARLIEST_TTL_SEC = 600
#: 첫 이상징후를 고를 때 지표로 거르는 경우 훑는 판정 행 수 상한 — 걸러질 행(앞 조각 지표)이 이보다 많으면 못 찾는다. **초기값이다**
FIRST_SCAN_ROWS = 2000
_EARLIEST: dict = {"at": None, "t": 0.0}
#: 노선 주입의 **실제 지표 곡선**(노선 셀 합산 분별 값) — 종류 → (KPI, graphio 임계 키, 계열, 분자 열, 분모 열).
#: 산식은 graphio `kpi.py` U-07(`100 × zerowin_dn / sessions_tp`)과 셀 탐지 스펙(transport 계열 · 임계 `zerowin_down`)을
#: 옮긴 것이다. 임계값은 여기 두지 않고 graphio `/api/settings/thresholds` 에서 읽는다(2026-11-25 · F-595 최고 11.3%)
MEASURED_KPI = {"ho_interruption": ("U-07", "zerowin_down", "transport", "ZEROWIN_DN", "SESSIONS")}


#: 주입별 정답 원장(생성기 `pemdb1.archive_truth`) — 지금 정답 파일과 같은 폴더. 파일이 바뀔 때만 다시 읽는다
_TBF: dict = {"mtime": None, "faults": {}}


def _truth_any(fault_id: int) -> dict:
    """그 장애의 정답 — 지금 정답 파일(`faults_api._truth_of` · 그때 걸린 주입만)에 없으면 주입별 원장에서 찾는다
    (2026-11-25 · 지난 주입 F-612·F-613 을 정답 셀로 가르려고). 원장이 없거나 그 번호가 없으면 빈 값 — 지금처럼"""
    t = _truth_of(fault_id)
    if t:
        return t
    from ..pemdb1 import TRUTH_BY_FAULT_FILE
    q = Path(str(_pem_cfg().get("truth_out") or "data/truth/ground_truth.json"))
    p = (q if q.is_absolute() else HERE / q).with_name(TRUTH_BY_FAULT_FILE)
    try:
        mt = p.stat().st_mtime
        if mt != _TBF["mtime"]:
            _TBF.update(mtime=mt, faults=(json.loads(p.read_text(encoding="utf-8")).get("faults") or {}))
    except (OSError, ValueError):
        return {}
    return _TBF["faults"].get(f"F-{fault_id}") or {}


def _state(sev: str, kpi: str) -> str:
    """지도 셀 음영과 같은 세 갈래 — 회색(무호 C-01) > 빨강(CRITICAL) > 노랑(그 밖)."""
    if kpi == SILENT_KPI:
        return "silent"
    return "severe" if sev == "CRITICAL" else "degraded"


def _iso(v) -> str | None:
    return v.isoformat(timespec="seconds") if isinstance(v, datetime) else (str(v) if v else None)


def _at(v) -> datetime | None:
    try:
        return datetime.fromisoformat(str(v or "")[:19])
    except ValueError:
        return None


def _fault(fid: int) -> dict:
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(FAULT_SELECT + f" FROM {T_FAULT} WHERE FAULT_ID = %s", [fid])
            cols = [d[0] for d in cur.description]
            row = cur.fetchone()
    finally:
        conn.close()
    if row is None:
        raise HTTPException(404, f"장애 F-{fid} 가 없습니다")
    f = dict(zip(cols, row))
    f["end_ts"] = f["start_ts"] + timedelta(minutes=int(f.get("duration_min") or 0))
    return f


def _graphio(path: str, timeout: float = 8) -> tuple[dict | None, str | None]:
    base = str(_pem_cfg().get("graphio_api") or "").rstrip("/")
    if not base:
        return None, "`pemdb1.graphio_api` 가 비어 있습니다"
    try:
        with urllib.request.urlopen(base + path, timeout=timeout) as r:
            return json.loads(r.read()), None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        return None, f"graphio API 에 닿지 않습니다 ({path}): {e}"


def _events(fid: int) -> tuple[list[dict], str | None]:
    """그 장애와 시간이 겹치는 graphio 사건(합쳐진 것 제외) — 열린 순."""
    doc, err = _graphio(f"/api/events/by-fault/{fid}")
    evs = sorted((doc or {}).get("events") or [], key=lambda e: str(e.get("opened_time") or ""))
    return evs, err


def _ev_cells(e: dict) -> set[str]:
    """사건 대표 조각의 셀 — graphio 사건 지도(`cells_only`). 한 번 물은 것은 사건 행에 얹어 다시 묻지 않는다."""
    if "_cells" not in e:
        rep = e.get("rep_incident_id")
        doc, _ = (_graphio(f"/api/incidents/{urllib.parse.quote(str(rep))}/map?cells_only=true", timeout=6)
                  if rep else (None, None))
        e["_cells"] = {str(c["key"]) for c in (doc or {}).get("cells") or () if c.get("key")}
    return e["_cells"]


def _ev_doc(e: dict) -> dict:
    """graphio 사건 한 건(`/api/events/{id}`) — 축과 구성 조각을 둘 다 여기서 읽는다. 한 번 물은 것은 사건 행에 얹는다"""
    if "_doc" not in e:
        doc, _ = _graphio(f"/api/events/{urllib.parse.quote(str(e.get('event_id') or ''))}", timeout=6)
        e["_doc"] = doc or {}
    return e["_doc"]


def _ev_axis(e: dict) -> dict[str, set]:
    """사건의 축(`DETAIL.axis_ids` · 예 `{"NODE": {"PGW-3"}}`) — 축 장애(DNS·APN 노드)가 같은 노드인지 보는 재료"""
    if "_axis" not in e:
        ax = (_ev_doc(e).get("detail") or {}).get("axis_ids") or {}
        e["_axis"] = {str(t): {str(x) for x in v or ()} for t, v in ax.items()}
    return e["_axis"]


def _members_in(e: dict, lo: datetime, hi: datetime) -> dict | None:
    """사건이 창 **앞에서 열렸지만** 구간이 창과 겹칠 때 — 창 안에서 열린 구성 조각만으로 본 사건(2026-11-25 · 3번 F-598).

    07:52 출퇴근 통화 집중(C-24) 조각이 먼저 연 통합장애에 08:05 주입의 U-07 조각 둘이 합쳐졌다. 열린 시각만 보면
    「주입 전 사건」으로 걸러지고, 사건을 통째로 받으면 대표 조각(C-24) 셀 340개를 칠한다. 그래서 창 안 조각이
    있으면 그 조각들의 셀만 `_cells` 로 얹고, 열린 시각은 첫 조각 시각으로 · 사건이 원래 열린 시각은
    `event_opened_time` 으로 따로 둔다. 창 안 조각이 없으면 None — 지금처럼 시각으로 거른다."""
    t, u = _at(e.get("opened_time")), _at(e.get("updated_time") or e.get("closed_time"))
    if t is None or t >= lo or u is None or u < lo or t > hi:
        return None
    ms = [(m, _at(m.get("opened"))) for m in _ev_doc(e).get("members") or ()]
    ins = sorted(((m, mt) for m, mt in ms if mt is not None and lo <= mt <= hi), key=lambda x: x[1])
    if not ins:
        return None
    cells: set[str] = set()
    kpis: set[str] = set()
    for m, _mt in ins[:EVENT_CELL_LOOKUPS]:
        iid = urllib.parse.quote(str(m.get("incident_id")))
        doc, _ = _graphio(f"/api/incidents/{iid}/map?cells_only=true", timeout=6)
        cells |= {str(c["key"]) for c in (doc or {}).get("cells") or () if c.get("key")}
        # 조각의 트리거 지표 — 같은 셀에 앞 조각(C-24 통화 몰림) 판정도 서 있어, 셀만 좁히면 주입 전 07:58 부터
        # 노랗게 켜졌다(동구 11-25). 사건 문서 `members` 에는 지표가 없어 조각 문서에서 읽는다
        inc, _ = _graphio(f"/api/incidents/{iid}", timeout=6)
        kpis |= {x.strip() for x in str(((inc or {}).get("incident") or {}).get("trigger_kpi") or "").split(",")
                 if x.strip()}
    return {**e, "_cells": cells, "_kpis": kpis, "opened_time": _iso(ins[0][1]),
            "event_opened_time": e.get("opened_time"), "kpis_in": sorted(kpis),
            "members_in": [str(m.get("incident_id")) for m, _mt in ins]}


def _scope_hit(f: dict, truth: dict, e: dict) -> str | None:
    """사건 범위가 이 장애와 겹치나 — 겹치면 근거 낱말, 아니면 None.

    정답(주입 범위)이 있으면 셀이 겹치거나 정답 노드가 사건 축에 있어야 한다. 정답은 가장 최근 장애만 남으므로
    지난 장애는 지정 대상(`TARGET`)으로, 그것도 없으면(자동 선택) kind 가 말하는 범위 **종류**로만 본다 —
    노드 장애면 같은 종류 노드 축(`PGW-n`), 셀 장애면 셀이 있는 사건. 어느 노드였는지 모르는 것을 지어내지 않는다.
    """
    from ..faults import _AUTO_NODE
    from ..scenarios import KIND_SPECS
    ids = {x for v in _ev_axis(e).values() for x in v}
    tc, node = {str(c) for c in truth.get("cells") or ()}, truth.get("expect_node")
    if tc or node:
        if node and str(node) in ids:
            return "truth_node"
        return "truth_cells" if tc & _ev_cells(e) else None
    target = str(f.get("target") or "")
    if target.upper().startswith("ENB:"):
        enbs = {x.strip() for x in target.split(":", 1)[1].split(",") if x.strip() and x.strip() != "+"}
        return "target" if any(c.startswith(b + "-") for b in enbs for c in _ev_cells(e)) else None
    if target and ":" not in target:   # `PGW-3`·`SGW-2` 같은 장비 지정
        return "target" if target in ids else None
    fld = _AUTO_NODE.get(str(f.get("kind")))
    if fld:
        pre = fld.split("_")[0].upper() + "-"
        return "kind_node" if any(x.startswith(pre) for x in _ev_axis(e).get("NODE", ())) else None
    if (KIND_SPECS.get(f.get("kind")) or {}).get("scope_kind") == "CELL":
        return "kind_cell" if _ev_cells(e) else None
    return "time_only"


def _claims(g: dict, e: dict) -> bool:
    """주입 `g` 도 사건 `e`(graphio 가 준 원래 행)를 시각·범위로 잡나 — `_own_events` 의 앞 두 조건과 같은 규칙"""
    lo = g["start_ts"].replace(second=0, microsecond=0) - timedelta(minutes=PAD_BEFORE_MIN)
    hi = g["end_ts"] + timedelta(minutes=OWN_GRACE_MIN)
    t = _at(e.get("opened_time"))
    if t is None or not lo <= t <= hi:
        e = _members_in(e, lo, hi)
        if e is None:
            return False
    return bool(_scope_hit(g, _truth_any(int(g["fault_id"])), e))


def _split_shared(f: dict, truth: dict, e: dict, sharers: list[dict]) -> dict:
    """한 사건을 이 장애와 **다른 주입도** 잡을 때 셀을 가른다(2026-11-25 · 9번 F-612 와 8번 F-613).

    graphio 가 두 주입을 한 통합장애로 묶으면(대표 조각 하나에 두 주입 셀이 다 있다) 양쪽 재생이 서로의 셀을 칠했다.
    둘 다 원인 코드가 같아(`RAN_RADIO_QUALITY`) 종류로도 못 가른다. 가르는 근거는 **주입 범위**뿐이다 —
    이 장애 정답 셀(또는 지정 eNB)이 있으면 그것과 겹치는 셀만, 없으면 같이 잡은 주입의 정답 셀을 뺀다.
    둘 다 모르면 그대로 둔다(옛 주입 — 정답 파일은 최근 장애만 남는다). 조각 조합으로 짐작하지 않는다."""
    if not sharers:
        return e
    cells = set(_ev_cells(e))
    mine = {str(c) for c in truth.get("cells") or ()}
    target = str(f.get("target") or "")
    if target.upper().startswith("ENB:"):
        enbs = {x.strip() for x in target.split(":", 1)[1].split(",") if x.strip() and x.strip() != "+"}
        mine |= {c for c in cells if c.split("-", 1)[0] in enbs}
    if mine:
        keep, how = cells & mine, "own_scope"
    else:
        theirs = {str(c) for g in sharers for c in _truth_any(int(g["fault_id"])).get("cells") or ()}
        if not theirs:
            return {**e, "shared_with": [int(g["fault_id"]) for g in sharers], "shared_split": None}
        keep, how = cells - theirs, "minus_others"
    return {**e, "_cells": keep, "shared_with": [int(g["fault_id"]) for g in sharers], "shared_split": how}


def _own_events(f: dict, evs: list[dict]) -> tuple[list[dict], list[dict]]:
    """시간이 겹친 graphio 사건을 **이 장애 것**과 **다른 장애**로 가른다(2026-11-24 F-597 사용자 「누가 봐도 다른 게 묶였다」).

    이 장애 것 = 열린 시각이 [장애 시작 − `PAD_BEFORE_MIN`, 장애 끝 + `OWN_GRACE_MIN`] 안이고 범위가 겹친다
    (`_scope_hit`). 그중 L2 통합장애가 있으면 **그 L2 만** 남긴다 — L2 에 들지 않은 조각·L1 은 따로 떨어진 다른
    판정 단위다. 다른 장애는 `why`(time·scope·kind·not_in_l2)를 달아 돌려준다 — 숨기지 않고 따로 보인다.

    **경쟁 주입 사이에서만 종류로 가른다**(2026-11-25 케이 · 3번 F-595 와 F-596 이 같은 07:00:06 에 시작해 F-595 가
    F-596 의 이동성 사건을 자기 것으로 잡았다). 같은 사건을 시각·범위로 다른 주입도 잡고, 사건 원인이 그쪽
    종류(`common.truth_codes`)에는 맞는데 이 장애 종류에는 안 맞으면 그쪽에 준다(`why=kind`). 잡는 주입이
    이 장애 하나뿐이면 원인이 달라도 이 장애 것이다 — graphio 가 원인을 틀린 사건도 재생에서 보여야 한다.
    """
    lo = f["start_ts"].replace(second=0, microsecond=0) - timedelta(minutes=PAD_BEFORE_MIN)
    hi = f["end_ts"] + timedelta(minutes=OWN_GRACE_MIN)
    truth = _truth_any(int(f["fault_id"]))
    mine = truth_codes(str(f.get("kind") or ""))
    rivals = None   # 처음 필요할 때 한 번 묻는다
    own, other = [], []
    for raw in evs:
        e = raw
        t = _at(e.get("opened_time"))
        if t is None or not lo <= t <= hi:
            part = _members_in(e, lo, hi) if len(own) < EVENT_CELL_LOOKUPS else None
            if part is None:
                other.append({**e, "why": "time"})
                continue
            e = part   # 창 안 조각으로만 본 사건 — 셀도 그 조각들 것
        why = _scope_hit(f, truth, e) if len(own) < EVENT_CELL_LOOKUPS else None
        code = str(e.get("root_code") or "")
        if why and mine and code and code != "UNDETERMINED" and code not in mine:
            if rivals is None:
                rivals = [g for g in _overlapping(lo, hi)
                          if int(g["fault_id"]) != int(f["fault_id"]) and g.get("status") != "cancelled"]
            if any(code in truth_codes(str(g.get("kind") or "")) and _claims(g, raw) for g in rivals):
                other.append({**e, "why": "kind"})
                continue
        if why:
            if rivals is None:
                rivals = [g for g in _overlapping(lo, hi)
                          if int(g["fault_id"]) != int(f["fault_id"]) and g.get("status") != "cancelled"]
            e = _split_shared(f, truth, e, [g for g in rivals if _claims(g, raw)])
        (own if why else other).append({**e, "why": why or "scope"})
    if any(e.get("unit") == "L2_EVENT" for e in own):
        other += [{**e, "why": "not_in_l2"} for e in own if e.get("unit") != "L2_EVENT"]
        own = [e for e in own if e.get("unit") == "L2_EVENT"]
    return own, other


def _fault_cells(fid: int, evs: list[dict]) -> tuple[list[str], dict]:
    """장애 셀 = 정답 셀 ∪ 사건 셀. `src` 는 셀마다 어디서 왔는지(`truth`·`event`·둘 다).

    `evs` 는 **이 장애 것으로 고른 사건**(`_own_events`)이다 — 다른 장애 사건의 셀을 칠하지 않는다."""
    src: dict[str, set] = {}
    for k in _truth_any(fid).get("cells") or ():
        src.setdefault(str(k), set()).add("truth")
    for e in evs[:EVENT_CELL_LOOKUPS]:
        for k in _ev_cells(e):
            src.setdefault(k, set()).add("event")
    return sorted(src), {k: sorted(v) for k, v in src.items()}


def _kpi_filter(fid: int, evs: list[dict]) -> dict[str, set]:
    """셀 → 칠해도 되는 지표. **사건 일부만 인정한 셀**(`_members_in`)만 걸린다 — 그 조각들의 트리거 지표만 칠한다.
    정답 셀이거나 사건 전체를 인정한 사건의 셀이면 거르지 않는다(키가 없다 · 지금까지의 동작 그대로)"""
    free = {str(k) for k in _truth_any(fid).get("cells") or ()}
    for e in evs[:EVENT_CELL_LOOKUPS]:
        if "_kpis" not in e:
            free |= _ev_cells(e)
    out: dict[str, set] = {}
    for e in evs[:EVENT_CELL_LOOKUPS]:
        if "_kpis" in e:
            for k in e["_cells"] - free:
                out.setdefault(k, set()).update(e["_kpis"])
    return out


def _ev_out(e: dict) -> dict:
    """응답에 내리는 사건 행 — 내부 재료(`_cells`·`_axis`)는 뺀다"""
    return {k: v for k, v in e.items() if not k.startswith("_")}


def _rp_window(f: dict, pre: int, post: int) -> tuple[datetime, datetime]:
    lo = f["start_ts"].replace(second=0, microsecond=0) - timedelta(minutes=pre)
    hi = f["end_ts"].replace(second=0, microsecond=0) + timedelta(minutes=post)
    return lo, hi


def _coords(keys) -> dict[str, list]:
    conn = _connect()
    try:
        pos = {c["key"]: [c["lon"], c["lat"]] for c in _map_cells(conn)}
    finally:
        conn.close()
    return {k: pos[k] for k in keys if k in pos}


def _fault_head(f: dict) -> dict:
    return {"fault_id": int(f["fault_id"]), "kind": f.get("kind"), "scenario_no": f.get("scenario_no"),
            "note": f.get("note"), "start": _iso(f["start_ts"]), "end": _iso(f["end_ts"]),
            "duration_min": f.get("duration_min"), "status": f.get("status"),
            "virtual_offset_sec": f.get("virtual_offset_sec")}


def _overlapping(lo: datetime, hi: datetime) -> list[dict]:
    """창 `[lo, hi]` 와 시간이 겹치는 주입 장애 전부 — 시작 순(2026-11-24 F-586·F-587).

    한 통합장애에 장애 둘이 묶이면(graphio 조각 이어 붙이기) 받은 장애 하나만 기준으로는 뒤 장애의 셀이
    「장애 뒤」로 보였다. 겹치는 장애를 숨기지 않고 모두 내려 화면이 나란히 그리게 한다."""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(FAULT_SELECT + f" FROM {T_FAULT} WHERE START_TIME <= %s "
                        "AND DATE_ADD(START_TIME, INTERVAL DURATION_MIN MINUTE) >= %s ORDER BY START_TIME", [hi, lo])
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        conn.close()
    out = []
    for f in rows:
        f["end_ts"] = f["start_ts"] + timedelta(minutes=int(f.get("duration_min") or 0))
        out.append(f)
    return out


def _faults_brief(fs: list[dict]) -> list[dict]:
    return [{"id": int(f["fault_id"]), "kind": f.get("kind"), "scenario_no": f.get("scenario_no"),
             "start": _iso(f["start_ts"]), "end": _iso(f["end_ts"])} for f in fs]


def _active_at(fs: list[dict], m: datetime) -> list[int]:
    """분 `m` 에 진행 중인 장애 — 시작·끝을 분으로 내려 본다(`faults_api._fault_at` 과 같은 규칙)."""
    return [int(f["fault_id"]) for f in fs
            if f["start_ts"].replace(second=0, microsecond=0) <= m < f["end_ts"]]


def _truth_owner(fs: list[dict]) -> dict[str, int]:
    """셀 → 그 셀을 정답으로 가진 장애. 정답 파일은 **가장 최근 장애만** 남아 지난 장애는 비어 있다 — 없으면 키가 없다"""
    own: dict[str, int] = {}
    for f in fs:
        for k in _truth_any(int(f["fault_id"])).get("cells") or ():
            own.setdefault(str(k), int(f["fault_id"]))
    return own


# ── C. 탐지 추적 리플레이 ────────────────────────────────────────────────────


@router.get("/api/replay/{fault_id}/timeline")
def api_replay_timeline(fault_id: int):
    """장애 하나의 시각 목록 — 주입 → 장애 시작 → 첫 이상징후 → 조각·L1 묶음·L2 사건 열림 → 판정.

    「몇 분 만에 잡았나」(`lead_min`)는 **장애 시작**에서 첫 이상징후·첫 판정까지다. 이상징후 시각은 1분 집계의
    분이라(초 0) 분 단위로 센다. 활성 시각과 AI 질의 시각은 DB 에 남지 않는다 — 지어내지 않고 `missing` 에 적는다.
    """
    f = _fault(fault_id)
    all_evs, err = _events(fault_id)
    evs, others = _own_events(f, all_evs)
    keys, _src = _fault_cells(fault_id, evs)
    kf = _kpi_filter(fault_id, evs)
    # 첫 이상징후 = 이 장애 셀(CELL) 또는 이 장애 사건의 축(노드·APN …)에 선 가장 이른 이상 판정. 전국 합(GLOBAL)은
    # 어느 장애 것인지 가를 수 없어 뺀다. 장애 끝 + 유예 뒤의 판정은 「첫 탐지」가 아니다(F-597 07:45 무관 조각이 15분으로 잡혔다)
    pairs = [("CELL", k) for k in keys] + sorted({(t, x) for e in evs for t, v in _ev_axis(e).items()
                                                  if t != "GLOBAL" for x in v})
    first = None
    if pairs:
        lo = f["start_ts"].replace(second=0, microsecond=0)
        hi = f["end_ts"] + timedelta(minutes=OWN_GRACE_MIN)
        conn = _connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT EVENT_TIME, SCOPE_TYPE, SCOPE_ID, KPI, SEVERITY FROM AI_ANOMALY_EVENT "
                            "WHERE COALESCE(CANCELLED, 0) = 0 AND EVENT_TIME >= %s AND EVENT_TIME <= %s "
                            "AND (SCOPE_TYPE, SCOPE_ID) IN (" + ",".join(["(%s, %s)"] * len(pairs))
                            + ") ORDER BY EVENT_TIME LIMIT %s", [lo, hi, *(x for pr in pairs for x in pr),
                                                                  FIRST_SCAN_ROWS if kf else 1])
                # 일부만 인정한 셀은 그 조각 지표의 판정만 첫 이상징후다(`_kpi_filter`)
                row = next((r for r in cur.fetchall()
                            if not (r[1] == "CELL" and r[2] in kf and r[3] not in kf[r[2]])), None)
        finally:
            conn.close()
        if row:
            first = {"at": _iso(row[0]), "scope_type": row[1], "cell": row[2], "kpi": row[3], "severity": row[4]}

    start = f["start_ts"].replace(second=0, microsecond=0)

    def lead(v) -> float | None:
        t = _at(v)
        return round((t - start).total_seconds() / 60, 1) if t else None

    steps = [{"step": "inject", "at": _iso(f.get("created_at")), "label": "주입 등록"},
             {"step": "fault_start", "at": _iso(f["start_ts"]), "label": "장애 시작"}]
    if first:
        steps.append({"step": "first_anomaly", "at": first["at"], "label": "첫 이상징후",
                      "detail": first, "lead_min": lead(first["at"])})
    unit_label = {"PIECE": "장애 조각", "L1_BUNDLE": "L1 묶음", "L2_EVENT": "L2 통합장애"}
    for e in evs:
        u = str(e.get("unit") or "")
        steps.append({"step": f"open:{u}", "at": e.get("opened_time"), "label": f"{unit_label.get(u, u)} 열림",
                      "event_id": e.get("event_id"), "rep": e.get("rep_incident_id"),
                      # 창 앞에서 열린 사건을 창 안 조각으로 받았으면(`_members_in`) 사건이 원래 열린 시각
                      "event_opened_at": e.get("event_opened_time"),
                      "lead_min": lead(e.get("opened_time"))})
        if e.get("verdict_time"):
            steps.append({"step": f"verdict:{u}", "at": e.get("verdict_time"),
                          "label": f"{unit_label.get(u, u)} 원인 판정", "event_id": e.get("event_id"),
                          "root_code": e.get("root_code"), "confidence": e.get("confidence"),
                          "lead_min": lead(e.get("verdict_time"))})
    steps.append({"step": "fault_end", "at": _iso(f["end_ts"]), "label": "장애 끝"})
    # **같은 사건 창에 걸린 다른 장애**도 시간축에 올린다 — 사건이 닫힌 시각까지 본다. 묶인 장애를 숨기면 뒤 장애의
    # 이상징후가 이 장애의 「끝난 뒤」로 읽힌다(F-586 재생에 F-587 셀 1,517개)
    ev_hi = max([_at(e.get("closed_time") or e.get("updated_time")) for e in evs
                 if _at(e.get("closed_time") or e.get("updated_time"))] + [f["end_ts"]])
    fs = _overlapping(f["start_ts"], ev_hi)
    # 시간만 겹친 **다른 장애 사건** — 점 하나로만 올린다(이 장애의 첫 이상징후·판정 계산과 셀 칠하기에서는 뺐다)
    for e in others:
        steps.append({"step": "other_event", "at": e.get("opened_time"), "event_id": e.get("event_id"),
                      "unit": e.get("unit"), "root_code": e.get("root_code"), "why": e.get("why")})
    for o in fs:
        if int(o["fault_id"]) == int(f["fault_id"]):
            continue
        steps.append({"step": "other_start", "at": _iso(o["start_ts"]), "label": f"다른 장애 F-{o['fault_id']} 시작",
                      "fault_id": int(o["fault_id"]), "kind": o.get("kind")})
        steps.append({"step": "other_end", "at": _iso(o["end_ts"]), "label": f"다른 장애 F-{o['fault_id']} 끝",
                      "fault_id": int(o["fault_id"]), "kind": o.get("kind")})
    steps.sort(key=lambda s: str(s.get("at") or ""))

    l2 = [e for e in evs if e.get("unit") == "L2_EVENT"]
    return {"fault": _fault_head(f), "steps": steps, "faults": _faults_brief(fs),
            "events": [_ev_out(e) for e in evs], "other_events": [_ev_out(e) for e in others],
            "summary": {
                "first_anomaly_min": lead(first["at"]) if first else None,
                "first_open_min": min((lead(e.get("opened_time")) for e in evs
                                       if lead(e.get("opened_time")) is not None), default=None),
                "l2_verdict_min": min((lead(e.get("verdict_time")) for e in l2
                                       if lead(e.get("verdict_time")) is not None), default=None),
                "l2_root_code": l2[0].get("root_code") if l2 else None},
            "missing": ["active — 생성기가 장애를 켠 시각은 DB 에 남지 않는다(장애 시작 뒤 첫 1분 슬롯)",
                        "ai_ask — AI 질의 기록은 graphio 가 저장하지 않는다"],
            "error": err}


# ── B. 장애 타임랩스 ─────────────────────────────────────────────────────────


def _anomaly_earliest() -> datetime | None:
    """`AI_ANOMALY_EVENT` 의 가장 이른 분 — 기본 키 첫 열이 `EVENT_TIME` 이라 인덱스 끝 한 번이다. 캐시한다"""
    if _EARLIEST["at"] is None or time.time() - _EARLIEST["t"] > EARLIEST_TTL_SEC:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT MIN(EVENT_TIME) FROM AI_ANOMALY_EVENT")
                row = cur.fetchone()
        finally:
            conn.close()
        _EARLIEST.update(at=row[0] if row else None, t=time.time())
    return _EARLIEST["at"]


def _measured(f: dict, lo: datetime, hi: datetime) -> dict | None:
    """노선 주입이면 노선 셀 합산 지표의 분별 값과 graphio 판정 기준 — 「올랐지만 기준 아래라 못 잡았다」를 보이는 재료.

    합산은 분자·분모를 더한 뒤 나눈다(graphio 불변규칙 2). 행이 없거나 분모가 0 인 분은 `value: None` 이다 — 0 으로
    채우지 않는다(불변규칙 4). 판정 기준은 셀 하나의 판정 문턱이라 합산 곡선에 대는 것은 **눈금**이지 판정이 아니다"""
    spec, line = MEASURED_KPI.get(str(f.get("kind") or "")), _injected_line(f)
    if not spec or not line:
        return None
    kpi, tkey, series, num, den = spec
    cells = [k for r in _rp_route_cells() if r["id"].upper() == line for k in r["cells"]]
    pairs = [tuple(k.split("-", 1)) for k in cells if "-" in k]
    if not pairs:
        return None
    rows: dict = {}
    conn = _connect()
    try:
        with conn.cursor() as cur:
            # 열 이름은 위 상수에서만 온다(사용자 입력이 아니다)
            cur.execute(f"SELECT EVENT_TIME, SUM({num}), SUM({den}) FROM PM_COR_UPLANE_CELL_1M "
                        "WHERE SERIES = %s AND EVENT_TIME >= %s AND EVENT_TIME <= %s AND (ENB_ID, CELLNUM) IN ("
                        + ",".join(["(%s, %s)"] * len(pairs)) + ") GROUP BY EVENT_TIME",
                        [series, lo, hi, *(x for pr in pairs for x in pr)])
            rows = {t.replace(second=0, microsecond=0): (n, d) for t, n, d in cur.fetchall()}
    finally:
        conn.close()
    pts = []
    m = lo
    while m <= hi:
        n, d = rows.get(m, (None, None))
        pts.append({"t": _iso(m), "num": int(n) if n is not None else None, "den": int(d) if d is not None else None,
                    "value": round(100.0 * float(n) / float(d), 2) if n is not None and d else None})
        m += timedelta(minutes=1)
    doc, terr = _graphio("/api/settings/thresholds", timeout=6)
    th = ((doc or {}).get("static") or {}).get(tkey) or {}
    # `rows` 0 = 그 창의 1분 집계가 남아 있지 않다(집계 보관이 지났다) — 곡선이 비는 것이 「값이 낮았다」가 아니다
    return {"kpi": kpi, "route": line, "cells": len(pairs), "series": series, "rows": len(rows),
            "threshold": {sev: v.get("above") for sev, v in th.items() if isinstance(v, dict) and "above" in v},
            "threshold_src": f"graphio /api/settings/thresholds static.{tkey}", "threshold_error": terr,
            "points": pts}


@router.get("/api/replay/{fault_id}/timelapse")
def api_replay_timelapse(fault_id: int, pre: int = PAD_BEFORE_MIN, post: int = PAD_AFTER_MIN):
    """매 분의 셀 상태 — graphio 가 그 분에 그 셀을 이상으로 본 등급(가장 높은 것)과 그 값.

    `frames[i].cells` 에 없는 셀은 **판정 없음**이다. 장애 셀 밖에서 선 셀 이상징후는 수만 센다(`others`) —
    다른 장애·평소 잡음까지 칠하면 이 장애의 그림이 아니다.
    """
    pre, post = max(0, min(pre, 30)), max(0, min(post, 60))
    f = _fault(fault_id)
    all_evs, err = _events(fault_id)
    evs, _others = _own_events(f, all_evs)
    keys, src = _fault_cells(fault_id, evs)
    kf = _kpi_filter(fault_id, evs)
    lo, hi = _rp_window(f, pre, post)
    # 창 끝 = 장애 끝 + post 와 **이 장애 사건**의 마지막 판정 가운데 늦은 쪽 — 판정까지 보이게. 다른 장애 판정으로 늘지 않는다
    last_v = max((t for t in (_at(e.get("verdict_time")) for e in evs) if t), default=None)
    if last_v and last_v.replace(second=0, microsecond=0) > hi:
        hi = last_v.replace(second=0, microsecond=0)
    mins = [lo + timedelta(minutes=i) for i in range(int((hi - lo).total_seconds() // 60) + 1)]
    per: dict[datetime, dict] = {m: {} for m in mins}
    others: dict[datetime, int] = {m: 0 for m in mins}
    conn = _connect()
    try:
        with conn.cursor() as cur:
            if keys:
                cur.execute("SELECT EVENT_TIME, SCOPE_ID, KPI, SEVERITY, KPI_VALUE, BASELINE, DENOM_CNT "
                            "FROM AI_ANOMALY_EVENT WHERE SCOPE_TYPE = 'CELL' AND COALESCE(CANCELLED, 0) = 0 "
                            "AND EVENT_TIME >= %s AND EVENT_TIME <= %s AND SCOPE_ID IN ("
                            + ",".join(["%s"] * len(keys)) + ")", [lo, hi, *keys])
                for t, k, kpi, sev, val, base, den in cur.fetchall():
                    m = t.replace(second=0, microsecond=0)
                    if m not in per or (k in kf and kpi not in kf[k]):
                        continue
                    sev = str(sev or "").upper()
                    cur_c = per[m].get(k)
                    if cur_c is None or _RANK.get(sev, 0) > _RANK.get(cur_c["sev"], 0) \
                            or (kpi == SILENT_KPI and cur_c["kpi"] != SILENT_KPI):
                        per[m][k] = {"sev": sev, "kpi": kpi, "state": _state(sev, kpi),
                                     "value": float(val) if val is not None else None,
                                     "baseline": float(base) if base is not None else None,
                                     "denom": int(den) if den is not None else None,
                                     "kpis": (cur_c or {}).get("kpis", set())}
                    per[m][k].setdefault("kpis", set()).add(kpi)
            cur.execute("SELECT EVENT_TIME, COUNT(DISTINCT SCOPE_ID) FROM AI_ANOMALY_EVENT "
                        "WHERE SCOPE_TYPE = 'CELL' AND COALESCE(CANCELLED, 0) = 0 "
                        "AND EVENT_TIME >= %s AND EVENT_TIME <= %s"
                        + (" AND SCOPE_ID NOT IN (" + ",".join(["%s"] * len(keys)) + ")" if keys else "")
                        + " GROUP BY EVENT_TIME", [lo, hi, *keys])
            for t, n in cur.fetchall():
                m = t.replace(second=0, microsecond=0)
                if m in others:
                    others[m] += int(n)
    finally:
        conn.close()
    fs = _overlapping(lo, hi)
    own = _truth_owner(fs)
    # 셀이 **처음 켜진 분**과 그때 진행 중이던 장애 — 정답이 없는 지난 장애에서 「어느 장애 때 켜졌나」를 가르는
    # 재료다. 정답이 아니라 시각 대조라 `first_fault` 로 따로 둔다(둘 이상 진행 중이면 목록)
    first_on: dict[str, datetime] = {}
    for m in mins:
        for k in per[m]:
            first_on.setdefault(k, m)
    # 장애 끝 + 유예 **뒤에 처음 켜진** 셀은 이 장애 셀이라도 칠하지 않는다 — 같은 셀을 나눠 쓰는 다음 장애의 이상이다
    # (F-597 DNS 사건 셀 411개 중 30개가 07:52 야마노테 장애 C-24 로 켜졌다). 장애 중 켜진 셀이 가라앉는 모습은 그대로 둔다
    cut = (f["end_ts"] + timedelta(minutes=OWN_GRACE_MIN)).replace(second=0, microsecond=0)
    for k in [k for k, t in first_on.items() if t > cut]:
        del first_on[k]
        for m in mins:
            if per[m].pop(k, None) is not None:
                others[m] += 1
    frames = []
    for m in mins:
        cells = {k: {**v, "kpis": sorted(v.get("kpis", ()))} for k, v in per[m].items()}
        kc: dict[str, int] = {}
        for v in cells.values():
            for kp in v["kpis"]:
                kc[kp] = kc.get(kp, 0) + 1
        frames.append({"t": _iso(m), "in_fault": f["start_ts"].replace(second=0) <= m < f["end_ts"],
                       "active": _active_at(fs, m),
                       "cells": cells, "kpi_counts": kc, "others": others[m]})
    earliest = _anomaly_earliest()
    pos = _coords(keys)

    def cell_row(k: str) -> dict:
        lon, lat = pos.get(k, (None, None))
        fo = first_on.get(k)
        return {"key": k, "lon": lon, "lat": lat, "src": src[k],
                # 정답 파일로 아는 소유 장애 — 모르면 None(지난 장애는 정답이 남지 않는다)
                "fault": own.get(k),
                "first_t": _iso(fo) if fo else None,
                "first_fault": _active_at(fs, fo) if fo else None}

    return {"fault": _fault_head(f), "faults": _faults_brief(fs),
            "window": {"from": _iso(lo), "to": _iso(hi), "pre": pre, "post": post},
            "cells": [cell_row(k) for k in keys],
            "cells_src": sorted({s for v in src.values() for s in v}),
            "frames": frames, "error": err,
            # 판정 기록 보관이 지난 장애 — 셀이 비는 것이 「놓쳤다」가 아니라 「기록이 지워졌다」임을 알린다
            "expired": bool(earliest and f["start_ts"] < earliest), "anomaly_earliest": _iso(earliest) if earliest else None,
            "measured": _measured(f, lo, hi),
            "note": "cells 에 없는 셀·분은 이상 판정 없음(정상 확인이 아니다). 등급은 graphio 탐지 그대로"}


# ── A. 전철을 따라 번지는 장애 ───────────────────────────────────────────────


def _injected_line(f: dict) -> str:
    """노선 주입 장애가 건 노선 id(외선·내선 꼬리 뗀 것 · 예 `YMT`) — 노선 주입이 아니거나 모르면 ``""``.

    생성기는 `ROUTE%:…` 를 설정 `mobility.routes_file` 의 노선에서 고른다(`faults.route_range_of` ·
    `scenarios.route_spread`). 그 파일의 노선이 하나로 접힐 때만 답한다 — 여럿이면 어느 것인지 모르므로 짓지 않는다."""
    from ..faults import ROUTE_RANGE_HEAD
    if not str(f.get("target") or "").upper().startswith(ROUTE_RANGE_HEAD + ":"):
        return ""
    p = Path(str((_pem_cfg().get("mobility") or {}).get("routes_file") or ""))
    if not str(p) or str(p) == ".":
        return ""
    if not p.is_absolute():
        p = (HERE / p).resolve()
    try:
        ids = {re.sub(r"-(OUTER|INNER)$", "", str(r.get("id") or "")).upper()
               for r in json.loads(p.read_text(encoding="utf-8"))}
    except (OSError, ValueError):
        return ""
    ids.discard("")
    return ids.pop() if len(ids) == 1 else ""


def _rp_route_cells() -> list[dict]:
    """노선 파일의 셀 순서 — 외선·내선 같은 선로는 한 번만. `[{id, name, loop, cells:[key…]}]`"""
    d = Path(str(_pem_cfg().get("lines_dir") or "data/lines"))
    if not d.is_absolute():
        d = (HERE / d).resolve()
    out, seen = [], set()
    for p in sorted(d.glob("*.json")):
        try:
            routes = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for r in routes:
            # 외선·내선(`YMT-OUTER`·`YMT-INNER`)은 같은 선로다 — 그 꼬리만 뗀다. `RAIL-0001` 처럼 번호가 붙은 노선은
            # 각자 다른 노선이라 그대로 둔다(앞부분만 보면 152개 노선이 하나로 접힌다)
            rid = re.sub(r"-(OUTER|INNER)$", "", str(r.get("id") or ""))
            if not rid or rid in seen or not r.get("cells"):
                continue
            seen.add(rid)
            # 외선·내선 꼬리를 뗀다(`map_api._map_stations` 와 같은 규칙) — 선은 하나고 방향은 선의 성질이 아니다
            nm = re.sub(r"\s*[가-힣]*순환\([^)]*\)", "", str(r.get("name") or rid).split(" — ")[0]).strip()
            out.append({"id": rid, "name": nm or rid,
                        "loop": bool(r.get("loop")), "cells": [str(k) for k in r["cells"]]})
    return out


#: 노선 → 철로 고리 파일(graphio `tools/build_line_track.py` 가 만든다). **`data/lines/` 밖이다** — 그 디렉터리의
#: `*.json` 은 생성기와 지도가 전부 노선 목록으로 읽는다
TRACK_FILES = {"YMT": "data/tracks/yamanote_track.json"}

#: 야마노테선 운행 간격(초) — 화면이 열차를 몇 대 그릴지 정하는 재료다(데이터에 열차가 없다 · 모듈 머리말).
#: 출처: JR東日本 山手線 평일 시각표의 공개 운행 간격 — 아침·저녁 러시 2~3분, 낮 4~5분, 한 바퀴 약 60분.
#: 2026-11-24 케이 정리값이고 **시각표 원문과 분 단위로 대조하지는 않았다.** 01~04시는 운행이 없다(None).
#: 시각은 **가상 시각**으로 본다 — 3번은 가상 08시대에 넣는다(`virtual_offset_sec`)
HEADWAY = {"rush": (120, 180), "day": (240, 300)}
RUSH_HOURS = (7, 8, 9, 17, 18, 19)
NO_SERVICE_HOURS = (1, 2, 3, 4)
LAP_SEC = 3600


def _headway(t: datetime) -> list[int] | None:
    if t.hour in NO_SERVICE_HOURS:
        return None
    return list(HEADWAY["rush" if t.hour in RUSH_HOURS else "day"])


def _track(rid: str) -> dict | None:
    rel = TRACK_FILES.get(rid.upper())
    if not rel:
        return None
    try:
        return json.loads((HERE / rel).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None          # 파일이 없으면 화면은 옛 방식(셀→셀)으로 그린다 — 지어내지 않는다


@router.get("/api/replay/{fault_id}/route")
def api_replay_route(fault_id: int, pre: int = PAD_BEFORE_MIN, post: int = PAD_AFTER_MIN,
                     line: str = ""):
    """매 분, 셀을 넘어간 가입자 흐름 — `(떠난 셀 → 도착 셀, 사람 수, 그중 전환 직후 정체 사람 수)`.

    graphio 가입자 1분 행(`PM_COR_XP_SUB_1M`)의 `PREV_*`(그 분 마지막 전환의 떠난 셀)와 분 끝 셀이다. 정체는
    bit 32(`CLS_HO_STALL`) — 3번 R-40 이 보는 같은 표시다. 장애 셀 또는 노선 셀이 한쪽이라도 낀 쌍만 내린다.
    `line` 을 주면 그 노선(파일 `id`, 외선·내선 꼬리 뗀 것 · 예 `YMT`)만, 없으면 장애 셀을 지나는 노선을 노선 셀로 쓴다. 노선 셀 순서(`routes[].cells`)로 화면이
    물결 방향을 그린다.
    """
    pre, post = max(0, min(pre, 30)), max(0, min(post, 60))
    f = _fault(fault_id)
    all_evs, err = _events(fault_id)
    keys, src = _fault_cells(fault_id, _own_events(f, all_evs)[0])
    # 노선은 `line` 으로 고르고, 없으면 **장애 셀을 가장 많이 지나는 노선 하나**다(같으면 셀이 긴 쪽). 도쿄 셀은 여러
    # 노선이 나눠 써서 「지나는 노선 전부」는 3번 F-604 에서 21개였다. 나머지는 `candidates` 로 내려 화면이 고르게 한다
    allr = _rp_route_cells()
    fk = set(keys)
    cand = sorted(({"id": r["id"], "name": r["name"], "overlap": len(set(r["cells"]) & fk), "cells": len(r["cells"])}
                   for r in allr if set(r["cells"]) & fk), key=lambda x: (-x["overlap"], -x["cells"]))
    # 노선 주입(`ROUTE%:…`)이면 **주입한 노선**이다(2026-11-25 케이 · 3번 F-595 가 셀 겹침으로 浅草線 을 골랐다).
    # 셀 겹침으로 고르는 것은 노선 주입이 아닌 장애뿐이다
    inj = _injected_line(f)
    pick = line.upper() if line else inj or (cand[0]["id"].upper() if cand else "")
    routes = [r for r in allr if r["id"].upper() == pick]
    route_keys = {k for r in routes for k in r["cells"]}
    want = set(keys) | route_keys
    lo, hi = _rp_window(f, pre, post)
    flows: dict[str, list] = {}
    totals: dict[str, dict[str, list]] = {}
    conn = _connect()
    try:
        with conn.cursor() as cur:
            # 전환한 가입자만(`HO_EVENTS > 0`). 사람 수는 IMSI 고유 수 — 한 분에 한 사람은 한 행이다
            cur.execute("SELECT EVENT_TIME, PREV_ENB_ID, PREV_CELLNUM, ENB_ID, CELLNUM, COUNT(DISTINCT IMSI), "
                        "COUNT(DISTINCT CASE WHEN CLS_HO_STALL > 0 THEN IMSI END) "
                        "FROM PM_COR_XP_SUB_1M WHERE EVENT_TIME >= %s AND EVENT_TIME <= %s "
                        "AND HO_EVENTS > 0 AND PREV_ENB_ID IS NOT NULL "
                        "GROUP BY EVENT_TIME, PREV_ENB_ID, PREV_CELLNUM, ENB_ID, CELLNUM", [lo, hi])
            for t, pe, pc, e, c, n, s in cur.fetchall():
                a, b = f"{pe}-{pc}", f"{e}-{c}"
                if a == b or (a not in want and b not in want):
                    continue
                m = _iso(t.replace(second=0, microsecond=0))
                flows.setdefault(m, []).append([a, b, int(n), int(s or 0)])
                for k, col in ((a, 0), (b, 1)):
                    if k in want:
                        row = totals.setdefault(m, {}).setdefault(k, [0, 0, 0])
                        row[col] += int(n)
                        if col == 0:
                            row[2] += int(s or 0)
    finally:
        conn.close()
    mins = [lo + timedelta(minutes=i) for i in range(int((hi - lo).total_seconds() // 60) + 1)]
    fs = _overlapping(lo, hi)
    frames = []
    for m in mins:
        t = _iso(m)
        fl = sorted(flows.get(t, []), key=lambda x: -x[2])
        # 그 분에 전환 행이 하나도 없으면 **모름**이다(gen 재시작·아직 안 구운 분) — 빈 목록은 「아무도 안 움직였다」로 읽힌다
        vt = m + timedelta(seconds=float(f.get("virtual_offset_sec") or 0))
        frames.append({"t": t, "in_fault": f["start_ts"].replace(second=0) <= m < f["end_ts"],
                       "active": _active_at(fs, m), "vt": _iso(vt), "headway_sec": _headway(vt),
                       "flows": fl if t in flows else None,
                       # 셀별 [떠난 사람, 들어온 사람, 떠나며 멈춘 사람] — 깜빡임의 세기
                       "cells": totals.get(t, {}) if t in flows else None})
    pos = _coords(want)
    return {"fault": _fault_head(f), "faults": _faults_brief(fs),
            "window": {"from": _iso(lo), "to": _iso(hi), "pre": pre, "post": post},
            "routes": routes, "route_candidates": cand, "fault_cells": keys,
            # 철로 고리 — 열차를 철로 위에 그리는 재료. 셀 위치 `cells[key].pos_m` 으로 깜빡일 구간을 안다
            "track": _track(routes[0]["id"]) if routes else None,
            "headway": {"bands": {k: list(v) for k, v in HEADWAY.items()}, "rush_hours": list(RUSH_HOURS),
                        "no_service_hours": list(NO_SERVICE_HOURS), "lap_sec": LAP_SEC,
                        "basis": "가상 시각", "source": "JR東日本 山手線 공개 운행 간격(원문 미대조)"}, "fault_cells_src": src,
            "coords": pos, "frames": frames, "error": err,
            "flow_cols": ["from_cell", "to_cell", "subs", "ho_stall_subs"],
            "note": "열차 위치는 데이터에 없다 — track·headway 로 그린 그림이다. flows 는 실제로 셀을 넘어간 가입자 흐름이다(gen 탑승자 상태는 남지 않는다). "
                    "전환 정체는 SGW 가 바뀐 전환만 잰다(graphio xp_agg) — 같은 SGW 전환은 사람 수에만 든다"}
