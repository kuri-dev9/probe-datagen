"""장애 주입·취소·목록, 장애 → graphio 사건 대조, `EVT-…` 단위 조회 API."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..control import FAULT_SELECT, FLOOR_TAIL_KEY, KINDS, T_FAULT, add_fault, cancel_fault, ensure_tables, get_config, list_faults, list_scenarios
from .common import HERE, _connect, _pem_cfg, truth_codes


router = APIRouter()


class FaultReq(BaseModel):
    kind: str
    target: str | None = None
    duration_min: int = 30
    intensity: float = 1.0
    start_ts: str | None = None  # ISO 문자열, 없으면 now
    note: str | None = None
    scenario_no: int | None = None  # 없으면 메모 앞 `#n` (control.add_fault)



_ENSURED = False


def _ensure_once(conn) -> None:
    global _ENSURED
    if not _ENSURED:
        ensure_tables(conn)
        _ENSURED = True


@router.get("/api/scenarios")
def api_scenarios():
    """장애 시나리오 목록과 발생 장비 마스크 — `CM_ERROR_SCENARIO`.

    화면은 이것을 그대로 그린다. 시나리오를 고르면 마스크가 `Y` 인 장비만
    대상 후보로 보인다.
    """
    conn = _connect()
    try:
        _ensure_once(conn)  # 표가 아직 없는 DB 에서 UI 만 먼저 떠도 보이게
        rows = list_scenarios(conn)
    finally:
        conn.close()
    # 권장 지속시간(`scenarios.recommended_duration`) — 주입 칸 기본값과 확정 대기 표시
    from ..scenario_ja import why_ja
    from ..scenarios import recommended_duration, recommended_hours
    from ..faults import TARGET_UNITS, UNIT_WARN
    for r in rows:
        # 이 종류가 내야 할 원인 코드 — 지도 재생이 사건과 주입을 이을 때 종류를 맞춘다
        # (2026-11-25 · 4G↔5G 사건에 DNS 주입이 붙었다). `replay_api._own_events` 와 같은 함수다
        r["truth_codes"] = truth_codes(r.get("kind") or "")
        # 거는 단위(2026-11-22 단위 표 · `faults.TARGET_UNITS`) — 있으면 화면이 장비 칸 대신 이 단위만 보인다.
        # `unit_warn` 단위(셀·eNB 하나)를 고르면 「탐지 안 될 수 있음」을 띄운다 — 막지 않는다
        r["units"] = list(TARGET_UNITS.get(r.get("kind") or "", ()))
        r["unit_warn"] = [u for u in r["units"] if u in UNIT_WARN]
        r["dur"] = recommended_duration(r.get("kind") or "")
        if r["dur"]:
            r["dur"]["why_ja"] = why_ja(r["dur"]["why"])
            r["dur"]["hours"] = recommended_hours(r["kind"])
    return rows


@router.get("/api/faults")
def api_faults(limit: int = 50, date: str | None = None):
    """장애 목록. `date` 가 있으면 **그날 시작한 것**만 본다.

    control.list_faults 는 제어 계약이라 건드리지 않고, 날짜 필터는 여기서 건다
    (화면 사정으로 공유 모듈의 시그니처를 바꾸지 않는다).
    """
    conn = _connect()
    try:
        if not date:
            rows = list_faults(conn, limit=limit)
        else:
            with conn.cursor() as cur:
                cur.execute(
                    FAULT_SELECT + f" FROM {T_FAULT} "
                    "WHERE START_TIME >= %s "
                    "AND START_TIME < DATE_ADD(%s, INTERVAL 1 DAY) "
                    "ORDER BY FAULT_ID DESC LIMIT %s", [date, date, limit])
                cols = [d[0] for d in cur.description]
                rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        # 하한 꼬리가 끝나는 시각(2026-11-02 · 카드 15) — 데몬의 **실제 상태**다
        # (`PemdbGen.floor_tail_until`). 규칙으로 추정하지 않는다: 셀 대상처럼
        # 보여도 해석 결과가 전국이면 꼬리가 없다(16번 `anchor_saturation`).
        # 값이 없으면 꼬리가 없거나 끝났다. 같은 셀을 다시 노리려면 이 시각 뒤에
        try:
            tail = json.loads(get_config(conn, FLOOR_TAIL_KEY) or "{}")
        except ValueError:
            tail = {}
        for r in rows:
            r["tail_until"] = tail.get(str(r.get("fault_id")))
            # json 칸은 드라이버가 글로 준다 — 화면이 객체로 읽게 푼다. 깨졌으면 글 그대로 둔다
            if isinstance(r.get("inject_context"), str):
                try:
                    r["inject_context"] = json.loads(r["inject_context"])
                except ValueError:
                    pass
        return rows
    finally:
        conn.close()


def _truth_of(fault_id: int) -> dict:
    """정답지의 그 장애 항목 — 파일은 최근 장애만 담는다. 없으면 빈 값이고 종류 기대값만 본다."""
    q = Path(str(_pem_cfg().get("truth_out") or "data/truth/ground_truth.json"))
    p = q if q.is_absolute() else HERE / q
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return next((x for x in doc.get("scenarios") or [] if x.get("scenario_id") == f"F-{fault_id}"), {})


def _match_score(i: dict, spec: dict, truth: dict, target: str) -> int:
    """이력 하나가 그 장애의 탐지로 얼마나 맞나 — 원인·대상이 앞서고 지표·판정 단위가 잇는다."""
    n = 0
    code, kpi = truth.get("truth_code") or spec.get("truth_code"), truth.get("expect_kpi") or spec.get("expect_kpi")
    if code and i.get("root_code") == code:
        n += 4
    if kpi and i.get("trigger_kpi") == kpi:
        n += 2
    cells = {str(sc.get("scope_id")) for sc in i.get("scopes") or [] if sc.get("scope_type") == "CELL"}
    axis = {str(x) for x in (i.get("detail") or {}).get("axis_ids") or []}
    # `ENB:a` — 그 eNB 의 셀이면 맞는 장애다(eNB 둘을 받던 25번 SON 은 2026-11-21 에 지웠다)
    enbs = ({e.strip() for e in target.split(":", 1)[1].split(",") if e.strip() and e.strip() != "+"}
            if target.upper().startswith("ENB:") else set())
    if (cells & set(truth.get("cells") or ()) or truth.get("expect_node") in axis or target in axis
            or any(c.startswith(e + "-") for e in enbs for c in cells)):
        n += 3
    return n + {"L2_EVENT": 2, "L1_BUNDLE": 1}.get(i.get("event_unit"), 0)


#: `EVT-…` 조회 결과 재사용(초) — 끝난 사건의 단위·대표 조각은 바뀌지 않는다. 열린 사건도 몇 분 늦는 것은 괜찮다. **초기값이다**
EVT_LOOKUP_TTL_SEC = 300
_EVT_RE = re.compile(r"^EVT-[0-9a-f]{10}$")
_EVT_CACHE: dict[str, tuple[float, dict]] = {}


@router.get("/api/evt-lookup")
def api_evt_lookup(ids: str = ""):
    """시나리오 화면의 `EVT-…` → 단위·대표 조각·겹친 장애(2026-11-10 사용자 — "EVT 를 직접 쓰지 말고 L1 장애 같은
    이름에 지도 링크를"). 사건 표는 graphio 것이라 `/api/events/{id}` 로 받는다(직접 읽지 않는다). 합쳐진 사건
    (`MERGED`)은 `merged_into` 를 따라간다(최대 3번). 받지 못한 ID 는 응답에 없다 — 화면은 ID 를 그대로 둔다
    """
    import time as _time
    import urllib.error
    import urllib.parse
    import urllib.request

    want = [x for x in dict.fromkeys(i.strip() for i in ids.split(",")) if _EVT_RE.match(x)][:40]
    base = str(_pem_cfg().get("graphio_api") or "").rstrip("/")
    if not want or not base:
        return {"events": {}, **({} if base else {"error": "`pemdb1.graphio_api` 가 비어 있습니다"})}

    def one(eid: str) -> dict | None:
        try:
            with urllib.request.urlopen(f"{base}/api/events/{urllib.parse.quote(eid)}", timeout=5) as r:
                return json.loads(r.read())
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            return None

    now, out = _time.time(), {}
    for eid in want:
        hit = _EVT_CACHE.get(eid)
        if hit and now - hit[0] < EVT_LOOKUP_TTL_SEC:
            out[eid] = hit[1]
            continue
        cur, path = one(eid), [eid]
        while cur and cur.get("status") == "MERGED" and cur.get("merged_into") and len(path) < 4:
            path.append(str(cur["merged_into"]))
            cur = one(path[-1])
        if not cur or not cur.get("unit") or not cur.get("rep_incident_id"):
            continue
        out[eid] = {"unit": cur["unit"], "rep": cur["rep_incident_id"], "event_id": cur.get("event_id") or path[-1],
                    "merged_into": path[-1] if len(path) > 1 else None, "opened": cur.get("opened_time"),
                    "fault": _fault_at(cur.get("opened_time"))}
        _EVT_CACHE[eid] = (now, out[eid])
    return {"events": out}


def _fault_at(opened: str | None) -> int | None:
    """사건이 열린 시각에 걸쳐 있던 장애(시작 ≤ 열림 ≤ 끝 + 확정 대기) — 지도 링크의 `fault` 인자. 없으면 None.

    **분 단위로 비교한다.** 사건 열림은 1분 집계 시각(초 0)이고 장애 시작은 초까지 있다 — 02:36:22 에
    넣은 장애의 첫 사건은 02:36:00 에 열린다(3번 F-235). 시작·끝을 분으로 내려 같은 분이면 걸친 것으로 본다."""
    from ..scenarios import SETTLE_MIN
    try:
        at = datetime.fromisoformat(str(opened or "")[:19]).replace(second=0, microsecond=0)
    except ValueError:
        return None
    try:
        conn = _connect()
    except Exception:  # noqa: BLE001 — 장애 번호만 모름
        return None
    try:
        with conn.cursor() as cur:
            st = "CAST(DATE_FORMAT(START_TIME, '%%Y-%%m-%%d %%H:%%i:00') AS DATETIME)"
            cur.execute(f"SELECT FAULT_ID FROM {T_FAULT} WHERE {st} <= %s AND "
                        f"DATE_ADD({st}, INTERVAL DURATION_MIN + %s MINUTE) >= %s "
                        "ORDER BY START_TIME DESC LIMIT 1", [at, SETTLE_MIN, at])
            row = cur.fetchone()
    except Exception:  # noqa: BLE001
        return None
    finally:
        conn.close()
    return int(row[0]) if row else None


@router.get("/api/faults/{fault_id}/incidents")
def api_fault_incidents(fault_id: int):
    """그 장애로 탐지된 이력 — 주입 시간(+ 확정 대기)과 겹치는 것을 맞는 순으로.

    지도가 고를 수 있는 이력만 본다 — 장애에서 넘어간 지도는 graphio `recent=1`(최근
    `console.incident_list_days` 일, 기본 7일)을 받으므로 여기도 같은 범위다(2026-11-09). 전에는 최근
    200건이라 이틀만 지나도(F-121 · 11-05) 이력이 있는데 "없음"이 됐다. graphio 에 못 닿은 것과
    이력이 없는 것은 다른 사실이라 `error` 로 가른다.
    """
    import urllib.error
    import urllib.parse
    import urllib.request

    from ..scenarios import KIND_SPECS, SETTLE_MIN
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(FAULT_SELECT + f" FROM {T_FAULT} WHERE FAULT_ID = %s", [fault_id])
            cols = [d[0] for d in cur.description]
            row = cur.fetchone()
    finally:
        conn.close()
    if row is None:
        return {"error": f"장애 {fault_id} 가 없습니다", "incidents": []}
    f = dict(zip(cols, row))
    base = str(_pem_cfg().get("graphio_api") or "").rstrip("/")
    if not base:
        return {"error": "`pemdb1.graphio_api` 가 비어 있습니다", "incidents": []}
    lo = f["start_ts"]
    hi = lo + timedelta(minutes=int(f["duration_min"] or 0) + SETTLE_MIN)
    # 그 장애 시간대만 받는다(2026-11-09) — 전에는 7일치 사건 전부(512건 · 2.2MB)를 받아 여기서 걸렀다.
    # 아래 시간 거름은 그대로 둔다: `since`·`until` 을 모르는 옛 graphio 는 인자를 무시하고 전부 준다
    q = urllib.parse.urlencode({"scope": 1, "recent": 1, "since": lo.isoformat(timespec="seconds"),
                                "until": hi.isoformat(timespec="seconds")})
    try:
        with urllib.request.urlopen(f"{base}/api/incidents?{q}", timeout=10) as r:
            doc = json.loads(r.read())
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return {"error": f"graphio API 에 닿지 않습니다: {e}", "incidents": []}

    def at(v) -> datetime | None:
        try:
            return datetime.fromisoformat(str(v or "")[:19])
        except ValueError:
            return None

    spec, truth, target = KIND_SPECS.get(f["kind"]) or {}, _truth_of(fault_id), str(f.get("target") or "")
    out = []
    for i in doc.get("incidents") or []:
        a, b = at(i.get("opened_ts")), at(i.get("closed_ts") or i.get("updated_ts"))
        if a is None or a > hi or (b or a) < lo:
            continue
        out.append({**{k: i.get(k) for k in ("incident_id", "title", "severity", "opened_ts",
                                             "root_code", "trigger_kpi", "event_unit")},
                    "score": _match_score(i, spec, truth, target)})
    out.sort(key=lambda x: (-x["score"], str(x["opened_ts"])))
    return {"incidents": out}


@router.post("/api/faults")
def api_add_fault(req: FaultReq):
    if req.kind not in KINDS:
        raise HTTPException(400, f"알 수 없는 kind 입니다: {req.kind!r}")
    if req.duration_min <= 0:
        raise HTTPException(400, "duration_min 은 1 이상이어야 합니다")
    try:
        start = datetime.fromisoformat(req.start_ts) if req.start_ts else datetime.now()
    except ValueError:
        raise HTTPException(400, f"start_ts 형식이 틀렸습니다: {req.start_ts!r}")
    target = req.target or None  # 빈 문자열 = 자동 선택 → NULL 로 저장
    conn = _connect()
    try:
        _ensure_once(conn)  # 새 DB 에 UI 만 먼저 떠도 동작하도록
        warn, warn_ja = _rush_warnings(conn, req.kind, start)
        # 메모에도 남긴다 — 응답은 주입한 사람만 보고, 판정하는 사람은 메모를 본다
        note = " ".join(x for x in [req.note or "", *(f"⚠ {w}" for w in warn)] if x) or None
        try:
            fid = add_fault(conn, req.kind, target, start,
                            duration_min=req.duration_min, intensity=req.intensity,
                            mode="manual", note=note, scenario_no=req.scenario_no)
        except ValueError as e:
            # `LIKE:` 원본 검사(`control._check_like`) — 500 이 아니라 사유를 돌려준다
            raise HTTPException(400, str(e))
    finally:
        conn.close()
    return {"fault_id": fid, "warnings": warn, "warnings_ja": warn_ja}


def _rush_warnings(conn, kind: str, start: datetime) -> tuple[list[str], list[str]]:
    """출퇴근 시간대에만 서는 시나리오(`rush_hours`)를 곡선 끔·가상 시각 구간 밖에 넣으면 경고한다.

    11-24 아침 3번(F-595)이 곡선 끔으로 들어가 무효가 됐다 — 균등 모드에서는 노선 탑승자가
    움직이지 않아 전환이 없고, 효과가 날 세션이 없다. **막지 않는다**(사용자 2026-11-24 「그래」 —
    경고만). 곡선·오프셋은 데몬과 같은 자리(`status_api.api_status`)에서 읽고, 시간대는 시작
    시각 + 가상 오프셋으로 본다(가상 시각이 곡선을 정한다 · `virtual-time-rush-hours`).
    """
    from ..scenarios import KIND_SPECS
    from .status_api import _clock_defaults

    rush = [(int(a), int(b)) for a, b in (KIND_SPECS.get(kind) or {}).get("rush_hours") or ()]
    if not rush:
        return [], []
    span = "·".join(f"{a:02d}~{b:02d}" for a, b in rush)
    di_d, vo_d = _clock_defaults()
    if str(get_config(conn, "diurnal", di_d)).lower() == "false":
        return ([f"시간대 곡선이 꺼져 있습니다 — 출퇴근 시간대({span}시)에만 서는 시나리오라 효과가 나지 않을 수 있습니다"],
                [f"時間帯曲線が切れています — 通勤時間帯({span.replace('~', '〜')}時)にだけ立つシナリオなので効果が出ないことがあります"])
    v = start + timedelta(seconds=float(get_config(conn, "virtual_offset_sec", vo_d) or 0))
    h = v.hour + v.minute / 60
    if any((a <= h < b) if a < b else (h >= a or h < b) for a, b in rush):
        return [], []
    return ([f"시작 가상 시각 {v:%H:%M} 이 출퇴근 시간대({span}시) 밖입니다 — 효과가 나지 않을 수 있습니다"],
            [f"開始の仮想時刻 {v:%H:%M} が通勤時間帯({span.replace('~', '〜')}時)の外です — 効果が出ないことがあります"])


@router.post("/api/faults/{fault_id}/cancel")
def api_cancel_fault(fault_id: int):
    conn = _connect()
    try:
        ok = cancel_fault(conn, fault_id)
    finally:
        conn.close()
    if not ok:
        # pending/active 가 아니면 UPDATE 0행 — 이미 끝났거나 없는 id
        raise HTTPException(404, f"취소할 수 없는 장애입니다: {fault_id}")
    return {"ok": True}
