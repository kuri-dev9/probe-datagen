"""시연 리플레이 API(`ui/replay_api.py` · 2026-11-24)의 불변 규칙.

고정하는 것 — 응답 키 · 「판정 없음」은 0 이 아니라 빠진다(불변규칙 17) · 행 없는 분의 흐름은 `null`(모름) ·
자동 노선은 장애 셀과 가장 많이 겹치는 것 · 외선·내선만 접고 `RAIL-xxxx` 는 접지 않는다 · 옛 이름 호환층이
기존 모듈의 이름을 덮지 않는다. DB·graphio 는 가짜로 바꿔 끼운다(실제 표를 읽지 않는다).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from datagen import ui
from datagen.ui import replay_api as R

T0 = datetime(2026, 11, 24, 14, 40, 6)      # 장애 시작(초가 있다 — 분 단위로 내림하는지 본다)
FAULT = {"fault_id": 604, "kind": "ho_interruption", "scenario_no": 15, "note": "t", "start_ts": T0,
         "duration_min": 9, "status": "DONE", "virtual_offset_sec": 0, "created_at": T0 - timedelta(seconds=30),
         "end_ts": T0 + timedelta(minutes=9)}


class _Cur:
    def __init__(self, handler):
        self.h, self.rows = handler, []

    def execute(self, sql, params=None):
        self.rows = self.h(" ".join(sql.split()), list(params or []))

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, handler):
        self.h = handler

    def cursor(self):
        return _Cur(self.h)

    def close(self):
        pass


@pytest.fixture
def patch(monkeypatch):
    """`handler(sql, params)` 로 DB 를 바꿔 끼우고, 장애·사건·셀·좌표를 고정한다."""
    st = SimpleNamespace(handler=lambda s, p: [], events=[], cells=["1-0", "2-0", "3-0"], lines=[],
                         faults=[], owner={}, track=None, truth=None)
    monkeypatch.setattr(R, "_fault", lambda fid: dict(FAULT, fault_id=fid))
    monkeypatch.setattr(R, "_events", lambda fid: (st.events, None))
    # 이 장애 것 고르기(`_own_events`)가 부르는 graphio·정답 읽기 — 사건 행의 `_c`(셀)·`_a`(축)로 대신한다. 없으면 장애 셀 그대로(겹친다)
    monkeypatch.setattr(R, "_truth_of", lambda fid: st.truth if st.truth is not None else {"cells": list(st.cells)})
    monkeypatch.setattr(R, "_ev_cells", lambda e: set(e.get("_c", st.cells)))
    monkeypatch.setattr(R, "_ev_axis", lambda e: e.get("_a", {}))
    monkeypatch.setattr(R, "_fault_cells", lambda fid, evs: (list(st.cells), {k: ["truth"] for k in st.cells}))
    monkeypatch.setattr(R, "_connect", lambda: _Conn(lambda s, p: st.handler(s, p)))
    monkeypatch.setattr(R, "_coords", lambda keys: {k: [139.7, 35.6] for k in keys if k != "9-9"})
    monkeypatch.setattr(R, "_rp_route_cells", lambda: st.lines)
    # 겹치는 주입 장애(F-586·F-587 처럼 한 사건에 걸린 것)와 셀 소유 — 기본은 받은 장애 하나뿐이다
    monkeypatch.setattr(R, "_overlapping", lambda lo, hi: st.faults or [dict(FAULT)])
    monkeypatch.setattr(R, "_truth_owner", lambda fs: st.owner)
    monkeypatch.setattr(R, "_track", lambda rid: st.track if rid.upper() == "YMT" else None)
    return st


def _t(minutes, sec=0):
    return T0.replace(second=0) + timedelta(minutes=minutes, seconds=sec)


# ── C. timeline ─────────────────────────────────────────────────────────────────

def test_timeline_has_the_documented_keys_and_leads_from_the_fault_start(patch):
    # 조각(+2분)이 L2(+4분)보다 먼저 열렸어도, 통합장애가 있으면 **L2 만** 이 장애 것이다 — 조각은 다른 판정 단위(not_in_l2)다
    patch.events = [{"unit": "L2_EVENT", "event_id": "EVT-1", "rep_incident_id": "IX1", "opened_time": _t(4).isoformat(),
                     "verdict_time": _t(9).isoformat(), "root_code": "RAN_HO_INTERRUPTION", "confidence": 0.615},
                    {"unit": "PIECE", "event_id": "EVT-2", "rep_incident_id": "IX2", "opened_time": _t(2).isoformat(),
                     "verdict_time": _t(15).isoformat(), "root_code": "NSA_NR_LEG_FAILURE", "confidence": 0.568}]
    patch.handler = lambda s, p: [(_t(2), "CELL", "1-0", "U-07", "MAJOR")] if "AI_ANOMALY_EVENT" in s else []
    d = R.api_replay_timeline(604)
    assert set(d) >= {"fault", "steps", "summary", "missing", "error", "events", "other_events"}
    assert set(d["summary"]) == {"first_anomaly_min", "first_open_min", "l2_verdict_min", "l2_root_code"}
    s = d["summary"]
    assert s["first_anomaly_min"] == 2.0 and s["first_open_min"] == 4.0 and s["l2_verdict_min"] == 9.0
    assert s["l2_root_code"] == "RAN_HO_INTERRUPTION"
    assert [e["event_id"] for e in d["events"]] == ["EVT-1"]
    assert [(e["event_id"], e["why"]) for e in d["other_events"]] == [("EVT-2", "not_in_l2")]
    kinds = [x["step"] for x in d["steps"]]
    assert kinds[0] == "inject" and "fault_start" in kinds and "fault_end" in kinds and "first_anomaly" in kinds
    assert "open:L2_EVENT" in kinds and "verdict:L2_EVENT" in kinds
    assert "open:PIECE" not in kinds and "verdict:PIECE" not in kinds        # 다른 장애 사건은 점 하나(other_event)로만
    assert kinds.count("other_event") == 1
    assert [x["at"] for x in d["steps"]] == sorted(x["at"] for x in d["steps"]), "시각 순이다"


def test_timeline_without_an_l2_keeps_pieces_and_a_late_verdict_goes_after_the_end(patch):
    patch.events = [{"unit": "PIECE", "event_id": "EVT-2", "rep_incident_id": "IX2", "opened_time": _t(2).isoformat(),
                     "verdict_time": _t(15).isoformat(), "root_code": "NSA_NR_LEG_FAILURE", "confidence": 0.568}]
    d = R.api_replay_timeline(604)
    kinds = [x["step"] for x in d["steps"]]
    assert kinds[-1] == "verdict:PIECE", "장애가 끝난 뒤에 난 판정(+15분)은 끝 점 뒤에 온다"
    assert d["summary"]["first_open_min"] == 2.0 and d["other_events"] == []


def test_timeline_does_not_invent_what_the_db_does_not_keep(patch):
    d = R.api_replay_timeline(604)
    assert [m.split(" — ")[0] for m in d["missing"]] == ["active", "ai_ask"]
    assert not any(x["step"] in ("active", "ai_ask") for x in d["steps"])
    assert d["summary"] == {"first_anomaly_min": None, "first_open_min": None, "l2_verdict_min": None,
                            "l2_root_code": None}, "재료가 없으면 None(모름) — 0분이 아니다"


def test_timeline_without_cells_never_asks_the_anomaly_table(patch):
    patch.cells = []
    asked = []
    patch.handler = lambda s, p: asked.append(s) or []
    assert R.api_replay_timeline(604)["summary"]["first_anomaly_min"] is None
    assert not asked


# ── B. timelapse ────────────────────────────────────────────────────────────────

def _anoms(rows):
    def h(sql, params):
        if "GROUP BY EVENT_TIME" in sql:
            return [(t, n) for t, n in rows.get("others", [])]
        return rows.get("cells", []) if "AI_ANOMALY_EVENT" in sql else []
    return h


def test_timelapse_keys_and_minute_grid(patch):
    d = R.api_replay_timelapse(604, pre=5, post=10)
    assert set(d) >= {"fault", "window", "cells", "cells_src", "frames", "error", "note"}
    assert d["window"]["from"].startswith("2026-11-24T14:35") and d["window"]["to"].startswith("2026-11-24T14:59")
    assert len(d["frames"]) == 25
    assert set(d["frames"][0]) == {"t", "in_fault", "active", "cells", "kpi_counts", "others"}
    infault = [f["t"][11:16] for f in d["frames"] if f["in_fault"]]
    # 14:40:06~14:49:06 — 끝 분(14:49)에도 6초가 걸쳐 있어 그 분까지 장애 중이다
    assert infault[0] == "14:40" and infault[-1] == "14:49" and len(infault) == 10


def test_timelapse_cells_without_a_verdict_are_absent_not_zero(patch):
    patch.handler = _anoms({"cells": [(_t(3), "1-0", "U-07", "CRITICAL", 31.2, 1.8, 900)]})
    d = R.api_replay_timelapse(604)
    f = {x["t"][11:16]: x for x in d["frames"]}
    assert set(f["14:43"]["cells"]) == {"1-0"}
    c = f["14:43"]["cells"]["1-0"]
    assert c["state"] == "severe" and c["value"] == 31.2 and c["baseline"] == 1.8 and c["denom"] == 900
    assert f["14:42"]["cells"] == {}, "판정 없는 분은 빈 칸이다 — 정상 확인으로 채우지 않는다"
    assert all("2-0" not in x["cells"] and "3-0" not in x["cells"] for x in d["frames"])
    assert "정상 확인이 아니다" in d["note"]


def test_timelapse_state_precedence_and_kpi_union(patch):
    patch.handler = _anoms({"cells": [
        (_t(3), "1-0", "U-07", "MAJOR", 22.0, 2.0, 500),
        (_t(3), "1-0", "N-02", "CRITICAL", 37.0, 45.0, 500),         # 같은 분 둘 — 더 높은 등급
        (_t(3), "2-0", "U-07", "CRITICAL", 40.0, 2.0, 500),
        (_t(3), "2-0", "C-01", "MINOR", 0.0, 1.0, 30)]})             # 무호(C-01)는 등급과 상관없이 회색
    d = R.api_replay_timelapse(604)
    cells = {x["t"][11:16]: x for x in d["frames"]}["14:43"]["cells"]
    assert cells["1-0"]["state"] == "severe" and cells["1-0"]["kpis"] == ["N-02", "U-07"]
    assert cells["2-0"]["state"] == "silent"
    assert {x["t"][11:16]: x for x in d["frames"]}["14:43"]["kpi_counts"] == {"N-02": 1, "U-07": 2, "C-01": 1}


def test_timelapse_other_cells_are_only_counted(patch):
    patch.handler = _anoms({"others": [(_t(4), 7)]})
    d = R.api_replay_timelapse(604)
    f = {x["t"][11:16]: x for x in d["frames"]}
    assert f["14:44"]["others"] == 7 and f["14:43"]["others"] == 0
    assert all(not x["cells"] for x in d["frames"]), "장애 셀 밖의 이상은 지도에 칠하지 않는다"


def test_timelapse_cell_without_coordinates_is_kept_with_none(patch):
    patch.cells = ["1-0", "9-9"]
    cs = {c["key"]: c for c in R.api_replay_timelapse(604)["cells"]}
    assert cs["1-0"]["lon"] == 139.7 and cs["9-9"]["lon"] is None and cs["9-9"]["src"] == ["truth"]


def test_timelapse_clamps_the_padding(patch):
    d = R.api_replay_timelapse(604, pre=999, post=999)
    assert d["window"]["pre"] == 30 and d["window"]["post"] == 60
    d = R.api_replay_timelapse(604, pre=-3, post=-3)
    assert d["window"]["pre"] == 0 and d["window"]["post"] == 0


# ── A. route ────────────────────────────────────────────────────────────────────

def _ln(i, cells, loop=False):
    return {"id": i, "name": i, "loop": loop, "cells": cells}


def _subs(rows):
    return lambda sql, params: rows if "PM_COR_XP_SUB_1M" in sql else []


def test_route_picks_the_line_that_shares_the_most_fault_cells(patch):
    patch.cells = ["1-0", "2-0", "3-0"]
    patch.lines = [_ln("A", ["1-0", "9-0", "8-0", "7-0"]), _ln("B", ["1-0", "2-0", "3-0"]),
                   _ln("C", ["2-0", "3-0", "4-0", "5-0", "6-0"]), _ln("D", ["6-0"])]
    d = R.api_replay_route(604)
    assert [r["id"] for r in d["routes"]] == ["B"], "겹침 3 — 셀이 더 긴 C(겹침 2)가 아니다"
    assert d["route_candidates"][0] == {"id": "B", "name": "B", "overlap": 3, "cells": 3}
    assert "D" not in [c["id"] for c in d["route_candidates"]], "장애 셀을 안 지나는 노선은 후보가 아니다"


def test_route_auto_pick_prefers_overlap_then_length(patch):
    patch.cells = ["1-0", "2-0"]
    patch.lines = [_ln("A", ["1-0", "2-0"]), _ln("B", ["1-0", "2-0", "5-0", "6-0"]), _ln("C", ["1-0"])]
    d = R.api_replay_route(604)
    assert [r["id"] for r in d["routes"]] == ["B"]                      # 겹침 2 가 같으면 셀이 긴 쪽
    assert [c["id"] for c in d["route_candidates"]] == ["B", "A", "C"]
    assert d["route_candidates"][0]["overlap"] == 2


def test_route_explicit_line_wins_and_is_case_insensitive(patch):
    patch.cells = ["1-0", "2-0"]
    patch.lines = [_ln("YMT", ["1-0"]), _ln("OTHER", ["1-0", "2-0"])]
    assert [r["id"] for r in R.api_replay_route(604, line="ymt")["routes"]] == ["YMT"]


def test_route_frame_without_any_row_is_unknown_not_empty(patch):
    patch.lines = [_ln("L", ["1-0", "2-0"])]
    patch.handler = _subs([(_t(3), 1, 0, 2, 0, 5, 2)])
    d = R.api_replay_route(604)
    f = {x["t"][11:16]: x for x in d["frames"]}
    assert f["14:43"]["flows"] == [["1-0", "2-0", 5, 2]]
    assert f["14:42"]["flows"] is None and f["14:42"]["cells"] is None, "행이 없는 분은 모름(null)이다"
    assert set(d) >= {"fault", "window", "routes", "route_candidates", "fault_cells", "coords", "frames",
                      "flow_cols", "note"}


def test_route_counts_who_left_and_who_arrived_and_stalled_leavers(patch):
    patch.lines = [_ln("L", ["1-0", "2-0", "3-0"])]
    patch.handler = _subs([(_t(3), 1, 0, 2, 0, 5, 2),      # 1-0 → 2-0: 5명 · 정체 2
                           (_t(3), 1, 0, 3, 0, 4, 1),      # 1-0 → 3-0: 4명 · 정체 1
                           (_t(3), 2, 0, 3, 0, 3, 0)])
    cells = {x["t"][11:16]: x for x in R.api_replay_route(604)["frames"]}["14:43"]["cells"]
    assert cells["1-0"] == [9, 0, 3]                          # 떠난 5+4 · 들어온 0 · 떠나며 멈춘 2+1
    assert cells["2-0"] == [3, 5, 0]
    assert cells["3-0"] == [0, 7, 0]


def test_route_drops_pairs_outside_the_fault_and_the_line_and_same_cell_pairs(patch):
    patch.cells = ["1-0"]
    patch.lines = [_ln("L", ["1-0", "2-0"])]
    patch.handler = _subs([(_t(3), 7, 0, 8, 0, 9, 9),       # 장애 셀도 노선 셀도 아니다
                           (_t(3), 1, 0, 1, 0, 6, 0),       # 같은 셀 — 전환이 아니다
                           (_t(3), 8, 0, 2, 0, 3, 1)])      # 도착이 노선 셀 — 남긴다
    fl = {x["t"][11:16]: x for x in R.api_replay_route(604)["frames"]}["14:43"]["flows"]
    assert fl == [["8-0", "2-0", 3, 1]]


def test_route_flows_are_sorted_by_people_descending(patch):
    patch.lines = [_ln("L", ["1-0", "2-0", "3-0"])]
    patch.handler = _subs([(_t(3), 1, 0, 2, 0, 2, 0), (_t(3), 2, 0, 3, 0, 9, 0), (_t(3), 1, 0, 3, 0, 5, 0)])
    fl = {x["t"][11:16]: x for x in R.api_replay_route(604)["frames"]}["14:43"]["flows"]
    assert [x[2] for x in fl] == [9, 5, 2]


# ── 노선 파일: 외선·내선만 접는다 ───────────────────────────────────────────────

def test_outer_and_inner_fold_into_one_line_but_numbered_rail_lines_do_not(tmp_path, monkeypatch):
    routes = [{"id": "YMT-OUTER", "name": "야마노테선 외선순환(시계)", "loop": True, "cells": ["1-0", "2-0"]},
              {"id": "YMT-INNER", "name": "야마노테선 내선순환(반시계)", "loop": True, "cells": ["2-0", "1-0"]},
              {"id": "RAIL-0001", "name": "노선 하나", "cells": ["5-0"]},
              {"id": "RAIL-0002", "name": "노선 둘", "cells": ["6-0"]},
              {"id": "EMPTY", "name": "셀 없음", "cells": []}]
    (tmp_path / "lines.json").write_text(json.dumps(routes, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(R, "_pem_cfg", lambda: {"lines_dir": str(tmp_path)})
    out = R._rp_route_cells()
    assert [r["id"] for r in out] == ["YMT", "RAIL-0001", "RAIL-0002"]
    assert out[0]["name"] == "야마노테선" and out[0]["cells"] == ["1-0", "2-0"], "먼저 나온 방향만 남긴다"
    assert out[0]["loop"] is True


# ── 옛 이름 호환층 ──────────────────────────────────────────────────────────────

def test_replay_api_is_registered_and_its_paths_exist():
    paths = {r.path for r in R.router.routes}
    assert {"/api/replay/{fault_id}/timeline", "/api/replay/{fault_id}/timelapse",
            "/api/replay/{fault_id}/route"} <= paths


def test_replay_names_never_shadow_names_of_the_other_modules():
    """`ui/__init__` 이 부분 모듈 이름을 패키지에 다시 싣는다 — 늦게 오는 모듈이 같은 이름을 가지면 앞 것을 덮는다."""
    own = {k for k, v in vars(R).items()
           if not k.startswith("__") and k not in ("router", "annotations")
           and getattr(v, "__module__", R.__name__) == R.__name__ and not isinstance(v, type(ui))}
    others = [m for m in ui._PARTS if m is not R]
    clash = {k: m.__name__ for m in others for k in own if k in vars(m) and vars(m)[k] is not vars(R)[k]}
    assert not clash, clash
    for k in own:
        assert vars(ui).get(k) is vars(R)[k] or k not in vars(ui)


def test_compat_layer_still_serves_the_old_window_and_route_cells_names():
    """기존 모듈의 `_window`·`_route_cells` 가 패키지 이름으로 그대로 읽힌다(맥스가 `_rp_*` 로 바꾼 이유)."""
    for name in ("_window", "_route_cells"):
        owners = [m for m in ui._PARTS if m is not R and name in vars(m)]
        if owners:
            assert getattr(ui, name) is vars(owners[-1])[name], name


# ── 한 사건에 걸린 여러 주입 장애 (F-586 · F-587) ────────────────────────────────

def _fx(fid, kind, start, minutes, no=None):
    return dict(FAULT, fault_id=fid, kind=kind, scenario_no=no, start_ts=start, duration_min=minutes,
                end_ts=start + timedelta(minutes=minutes))


def _two_faults(patch):
    """F-604(14:40:06~14:49:06) 뒤 4분 만에 F-587(14:53:30~14:58:30)이 걸린 모양."""
    patch.faults = [dict(FAULT), _fx(587, "signal_storm", datetime(2026, 11, 24, 14, 53, 30), 5, no=34)]


def test_faults_list_is_in_every_route_of_the_api(patch):
    _two_faults(patch)
    patch.events = [{"unit": "L2_EVENT", "event_id": "E", "opened_time": _t(2).isoformat(),
                     "closed_time": _t(20).isoformat()}]
    for d in (R.api_replay_timeline(604), R.api_replay_timelapse(604), R.api_replay_route(604)):
        assert [f["id"] for f in d["faults"]] == [604, 587]
        assert set(d["faults"][1]) == {"id", "kind", "scenario_no", "start", "end"}
        assert d["faults"][1]["kind"] == "signal_storm" and d["faults"][1]["scenario_no"] == 34


def test_active_lists_the_faults_running_in_that_minute(patch):
    _two_faults(patch)
    for d in (R.api_replay_timelapse(604), R.api_replay_route(604)):
        a = {x["t"][11:16]: x["active"] for x in d["frames"]}
        assert a["14:39"] == [] and a["14:40"] == [604] and a["14:49"] == [604]
        assert a["14:50"] == [] and a["14:52"] == [], "사이 공백 — 어느 주입 장애도 진행 중이 아니다"
        assert a["14:53"] == [587] and a["14:58"] == [587] and a["14:59"] == []


def test_active_overlap_lists_both_and_single_fault_lists_only_itself(patch):
    patch.faults = [dict(FAULT), _fx(585, "x", T0 + timedelta(minutes=2), 3)]
    a = {x["t"][11:16]: x["active"] for x in R.api_replay_timelapse(604)["frames"]}
    assert a["14:43"] == [604, 585] and a["14:41"] == [604]
    patch.faults = []
    assert all(x["active"] in ([], [604]) for x in R.api_replay_timelapse(604)["frames"])


def test_cell_first_fault_is_the_fault_running_when_it_first_lit(patch):
    _two_faults(patch)
    patch.handler = _anoms({"cells": [(_t(2), "1-0", "U-07", "MAJOR", 22.0, 2.0, 500),   # 14:42 — F-604 중
                                      (_t(2), "2-0", "U-07", "MAJOR", 22.0, 2.0, 500),
                                      (_t(15), "2-0", "U-07", "MAJOR", 25.0, 2.0, 500)]})  # 나중 값 — 처음이 아니다
    cs = {c["key"]: c for c in R.api_replay_timelapse(604, pre=5, post=20)["cells"]}
    assert cs["1-0"]["first_fault"] == [604] and cs["1-0"]["first_t"].startswith("2026-11-24T14:42")
    assert cs["2-0"]["first_fault"] == [604], "처음 켜진 분 기준 — 나중에 또 켜져도 안 바뀐다"
    assert set(cs["1-0"]) >= {"key", "lon", "lat", "src", "fault", "first_t", "first_fault"}


def test_cell_first_lit_after_the_fault_end_plus_grace_is_not_painted_but_counted_as_others(patch):
    """F-597: DNS 사건 셀 411개 중 일부가 07:52 **다음 장애**(야마노테)의 이상으로 켜졌다 — 이 장애 셀이라도 칠하지 않는다."""
    _two_faults(patch)
    patch.handler = _anoms({"cells": [(_t(2), "1-0", "U-07", "MAJOR", 22.0, 2.0, 500),      # 장애 중 — 칠한다
                                      (_t(14), "3-0", "U-07", "MAJOR", 30.0, 2.0, 500)]})   # 14:54 = 끝(14:49:06)+유예(1분) 뒤 첫 점등
    d = R.api_replay_timelapse(604, pre=5, post=20)
    f = {x["t"][11:16]: x for x in d["frames"]}
    assert "3-0" not in f["14:54"]["cells"] and f["14:54"]["others"] == 1       # others 로만 센다
    assert "1-0" in f["14:42"]["cells"]
    c3 = {c["key"]: c for c in d["cells"]}["3-0"]
    assert c3["first_t"] is None and c3["first_fault"] is None                  # 칠해진 적이 없다


def test_cell_lit_inside_the_grace_minute_is_still_this_faults(patch):
    patch.handler = _anoms({"cells": [(_t(10), "1-0", "U-07", "MAJOR", 22.0, 2.0, 500)]})      # 14:50 = 끝 분(14:49)+1 — 유예 안
    f = {x["t"][11:16]: x for x in R.api_replay_timelapse(604)["frames"]}
    assert "1-0" in f["14:50"]["cells"]


def test_cell_that_never_lit_has_no_first_fault_not_an_empty_one(patch):
    c = {x["key"]: x for x in R.api_replay_timelapse(604)["cells"]}["1-0"]
    assert c["first_t"] is None and c["first_fault"] is None


def test_cell_fault_is_none_without_a_truth_file_and_the_owner_with_one(patch):
    """정답 파일은 가장 최근 장애만 남는다 — 지난 장애의 셀은 소유를 모른다(null). 시각 대조(first_fault)와 따로 둔다."""
    cs = {c["key"]: c for c in R.api_replay_timelapse(604)["cells"]}
    assert all(c["fault"] is None for c in cs.values())
    patch.owner = {"1-0": 606}
    cs = {c["key"]: c for c in R.api_replay_timelapse(604)["cells"]}
    assert cs["1-0"]["fault"] == 606 and cs["2-0"]["fault"] is None


def test_timeline_puts_the_other_faults_start_and_end_on_the_time_axis(patch):
    _two_faults(patch)
    patch.events = [{"unit": "L2_EVENT", "event_id": "E", "opened_time": _t(2).isoformat(),
                     "closed_time": _t(20).isoformat()}]
    d = R.api_replay_timeline(604)
    other = [s for s in d["steps"] if s["step"] in ("other_start", "other_end")]
    assert [(s["step"], s["fault_id"]) for s in other] == [("other_start", 587), ("other_end", 587)]
    assert other[0]["label"] == "다른 장애 F-587 시작" and other[1]["label"] == "다른 장애 F-587 끝"
    assert other[0]["kind"] == "signal_storm"
    assert not any(s["fault_id"] == 604 for s in other if "fault_id" in s), "받은 장애 자신은 other 가 아니다"
    assert [x["at"] for x in d["steps"]] == sorted(x["at"] for x in d["steps"])


def test_timeline_without_other_faults_has_none_of_them(patch):
    d = R.api_replay_timeline(604)
    assert not any(s["step"].startswith("other_") for s in d["steps"])
    assert [f["id"] for f in d["faults"]] == [604]


# ── 열차 철로 · 운행 간격 ───────────────────────────────────────────────────────

def test_route_carries_the_track_only_for_the_yamanote_line(patch):
    track = {"ring": [[139.7, 35.6]], "length_m": 34476, "stations": [], "cells": {}}
    patch.track = track
    patch.lines = [_ln("YMT", ["1-0", "2-0"])]
    assert R.api_replay_route(604)["track"] == track
    patch.lines = [_ln("RAIL-0001", ["1-0", "2-0"])]
    assert R.api_replay_route(604)["track"] is None, "야마노테선이 아닌 노선은 철로 고리가 없다 — 지어내지 않는다"
    patch.lines = []
    assert R.api_replay_route(604)["track"] is None


def test_track_is_none_when_the_file_is_missing_or_broken(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "HERE", tmp_path)
    assert R._track("YMT") is None                       # 파일 없음
    (tmp_path / "data" / "tracks").mkdir(parents=True)
    (tmp_path / "data" / "tracks" / "yamanote_track.json").write_text("{깨진", encoding="utf-8")
    assert R._track("YMT") is None                       # 깨진 파일
    (tmp_path / "data" / "tracks" / "yamanote_track.json").write_text('{"ring": []}', encoding="utf-8")
    assert R._track("ymt") == {"ring": []}               # 대소문자 무관
    assert R._track("OTHER") is None


def test_headway_follows_the_virtual_hour(patch):
    rush, day = list(R.HEADWAY["rush"]), list(R.HEADWAY["day"])
    base = datetime(2026, 11, 24, 0, 0)
    for h, want in ((8, rush), (18, rush), (7, rush), (9, rush), (12, day), (10, day), (22, day), (5, day),
                    (1, None), (4, None), (0, day)):
        assert R._headway(base.replace(hour=h)) == want, h
    assert rush == [120, 180] and day == [240, 300]


def test_route_frames_use_the_virtual_time_for_the_headway(patch):
    """주입은 실시각 14시대여도 가상 시각이 08시대면 출퇴근 간격이다."""
    patch.lines = [_ln("YMT", ["1-0"])]
    f = dict(FAULT, virtual_offset_sec=-(6 * 3600 + 32 * 60))             # 14:40 → 가상 08:08
    patch.faults = [f]
    import datagen.ui.replay_api as mod
    orig = mod._fault
    mod._fault = lambda fid: dict(f, fault_id=fid)
    try:
        fr = R.api_replay_route(604)["frames"]
    finally:
        mod._fault = orig
    by = {x["t"][11:16]: x for x in fr}
    assert by["14:42"]["vt"].startswith("2026-11-24T08:10") and by["14:42"]["headway_sec"] == [120, 180]


# ── `data/lines/` 는 노선 목록만 담는다 ─────────────────────────────────────────

def test_lines_dir_holds_only_route_lists():
    """생성기(`pemdb1`)와 지도가 이 디렉터리의 `*.json` 을 전부 노선 목록으로 읽는다 — 다른 파일이 들어가면 깨진다(11-24)."""
    from pathlib import Path
    d = Path(R.HERE) / "data" / "lines"
    files = sorted(d.glob("*.json"))
    assert files, "노선 파일이 하나도 없다"
    for p in files:
        doc = json.loads(p.read_text(encoding="utf-8"))
        assert isinstance(doc, list) and doc, f"{p.name} 은 노선 목록이 아니다"
        for r in doc:
            assert isinstance(r, dict) and r.get("id") and isinstance(r.get("cells"), list), (p.name, str(r)[:80])


def test_track_file_lives_outside_the_lines_dir_and_is_a_ring():
    from pathlib import Path
    rel = R.TRACK_FILES["YMT"]
    assert rel.startswith("data/tracks/") and "lines" not in rel
    p = Path(R.HERE) / rel
    assert p.exists()
    t = json.loads(p.read_text(encoding="utf-8"))
    assert set(t) >= {"ring", "length_m", "stations", "cells"}
    assert 34000 < t["length_m"] < 35000 and len(t["stations"]) == 30 and len(t["ring"]) > 1000
    assert not (Path(R.HERE) / "data" / "lines" / p.name).exists()


# ── 이 장애 것 고르기 `_own_events` · `_scope_hit` (2026-11-24 F-597 사용자 「누가 봐도 다른 게 묶였다」) ─────────────────

def _ev(eid, unit, opened_min, **kw):
    return {"unit": unit, "event_id": eid, "opened_time": _t(opened_min).isoformat(), **kw}


def test_own_events_splits_by_open_time_with_a_grace_minute(patch):
    f = dict(FAULT)                                   # 14:40:06 ~ 14:49:06
    evs = [_ev("early", "PIECE", -6), _ev("pad", "PIECE", -5), _ev("mid", "PIECE", 3),
           _ev("grace", "PIECE", 10), _ev("late", "PIECE", 11)]
    own, other = R._own_events(f, evs)
    assert [e["event_id"] for e in own] == ["pad", "mid", "grace"]               # 시작 −5분 ~ 끝+1분(14:50:06) 안
    assert {(e["event_id"], e["why"]) for e in other} == {("early", "time"), ("late", "time")}
    assert R.OWN_GRACE_MIN == 1 and R.PAD_BEFORE_MIN == 5


def test_own_events_splits_by_scope_with_truth_cells(patch):
    patch.truth = {"cells": ["1-0", "2-0"]}
    evs = [_ev("hit", "PIECE", 2, _c=["2-0", "9-9"]), _ev("miss", "PIECE", 2, _c=["7-7"])]
    own, other = R._own_events(dict(FAULT), evs)
    assert [e["event_id"] for e in own] == ["hit"] and own[0]["why"] == "truth_cells"
    assert [(e["event_id"], e["why"]) for e in other] == [("miss", "scope")]


def test_own_events_truth_node_matches_the_event_axis(patch):
    patch.truth = {"cells": [], "expect_node": "PGW-3"}
    evs = [_ev("dns", "L2_EVENT", 0, _a={"NODE": {"PGW-3"}}), _ev("other", "L2_EVENT", 0, _a={"NODE": {"PGW-4"}})]
    own, other = R._own_events(dict(FAULT, kind="dns_degradation"), evs)
    assert [e["event_id"] for e in own] == ["dns"] and own[0]["why"] == "truth_node"
    assert [e["event_id"] for e in other] == ["other"]


def test_own_events_keeps_only_the_l2_when_there_is_one(patch):
    evs = [_ev("p1", "PIECE", 1), _ev("l2", "L2_EVENT", 2), _ev("b1", "L1_BUNDLE", 3)]
    own, other = R._own_events(dict(FAULT), evs)
    assert [e["event_id"] for e in own] == ["l2"]
    assert {(e["event_id"], e["why"]) for e in other} == {("p1", "not_in_l2"), ("b1", "not_in_l2")}


def test_own_events_never_invents_a_node_when_the_fault_has_no_truth(patch):
    """정답 파일은 가장 최근 장애만 남는다 — 지난 DNS 장애는 노드를 모른다. 노드 축이 PGW 종류인지만 본다(어느 노드인지 지어내지 않는다)."""
    patch.truth = {}
    f = dict(FAULT, kind="dns_degradation", target="")
    own, other = R._own_events(f, [_ev("pgw", "L2_EVENT", 0, _a={"NODE": {"PGW-3"}}),
                                   _ev("cellish", "L2_EVENT", 0, _a={}, _c=["1-0"])])
    assert [e["event_id"] for e in own] == ["pgw"] and own[0]["why"] == "kind_node"
    assert [(e["event_id"], e["why"]) for e in other] == [("cellish", "scope")]


def test_scope_hit_by_explicit_target(patch):
    patch.truth = {}
    ev = _ev("x", "L2_EVENT", 0, _a={"NODE": {"PGW-3"}}, _c=["131-0"])
    assert R._scope_hit(dict(FAULT, kind="dns_degradation", target="PGW-3"), {}, ev) == "target"
    assert R._scope_hit(dict(FAULT, kind="dns_degradation", target="PGW-9"), {}, ev) is None
    assert R._scope_hit(dict(FAULT, target="ENB:131"), {}, ev) == "target"
    assert R._scope_hit(dict(FAULT, target="ENB:999"), {}, ev) is None


def test_scope_hit_cell_kind_needs_cells_and_unknown_kind_is_time_only(patch):
    patch.truth = {}
    assert R._scope_hit(dict(FAULT, kind="ho_interruption", target=""), {}, _ev("a", "PIECE", 0, _c=["1-0"])) == "kind_cell"
    assert R._scope_hit(dict(FAULT, kind="ho_interruption", target=""), {}, _ev("a", "PIECE", 0, _c=[])) is None


def test_other_events_are_not_in_the_first_anomaly_or_the_window(patch):
    """다른 장애 사건(F-597 의 07:45 조각)이 첫 이상징후·창 끝을 늘리지 않는다."""
    patch.events = [_ev("own", "L2_EVENT", 0, verdict_time=_t(20).isoformat(), closed_time=_t(21).isoformat()),
                    _ev("late", "PIECE", 20, verdict_time=_t(60).isoformat(), closed_time=_t(61).isoformat())]
    d = R.api_replay_timeline(604)
    assert [e["event_id"] for e in d["events"]] == ["own"] and [e["event_id"] for e in d["other_events"]] == [("late")]
    tl = R.api_replay_timelapse(604)
    assert tl["window"]["to"] < _t(40).isoformat()            # 무관 사건의 판정(+60분)까지 창이 늘어나지 않는다


def test_event_rows_do_not_leak_internal_material(patch):
    patch.events = [_ev("own", "L2_EVENT", 1)]
    d = R.api_replay_timeline(604)
    assert all(not k.startswith("_") for e in d["events"] + d["other_events"] for k in e)


def test_f597_like_dns_fault_ignores_unrelated_events_opened_after_it_ended(patch, monkeypatch):
    """F-597: DNS 장애(PGW-3) 뒤 07:45~48 무관 조각 셋과 07:52 야마노테 통합장애가 시간만 겹쳐 끌려 왔다.
    첫 이상징후는 15분이 아니라 0분이고, 장애 셀로 칠해지는 것은 없다."""
    patch.truth = {}
    patch.cells = ["a-0"]
    f597 = dict(FAULT, fault_id=597, kind="dns_degradation", target="PGW-3", start_ts=_t(0, 0) - timedelta(minutes=0),
                end_ts=_t(0, 0) + timedelta(minutes=13))
    monkeypatch.setattr(R, "_fault", lambda fid: dict(f597))
    if True:
        patch.events = [_ev("dns", "L2_EVENT", 0, verdict_time=_t(20).isoformat(), root_code="DNS_DEGRADATION", _a={"NODE": {"PGW-3"}}),
                        _ev("p1", "PIECE", 15), _ev("p2", "PIECE", 17), _ev("p3", "PIECE", 18),
                        _ev("ymt", "L2_EVENT", 22, root_code="RAN_HO_INTERRUPTION", _c=["x-1", "x-2"])]
        # 장애 셀(a-0)에는 판정이 없고, 첫 이상징후는 이 장애 사건의 노드 축(PGW-3)에서 0분에 선다
        patch.handler = lambda s, p: [(_t(0), "NODE", "PGW-3", "U-29", "MAJOR")] if "AI_ANOMALY_EVENT" in s and "SCOPE_TYPE, SCOPE_ID) IN" in s else []
        d = R.api_replay_timeline(597)
        assert d["summary"]["first_anomaly_min"] == 0.0 and d["summary"]["first_open_min"] == 0.0
        assert d["summary"]["l2_root_code"] == "DNS_DEGRADATION"
        assert [e["event_id"] for e in d["events"]] == ["dns"]
        assert sorted(e["event_id"] for e in d["other_events"]) == ["p1", "p2", "p3", "ymt"]
        assert {e["why"] for e in d["other_events"]} == {"time"}
        kinds = [x["step"] for x in d["steps"]]
        assert kinds.count("open:L2_EVENT") == 1, "「통합장애 열림」이 두 번 나오지 않는다"
        tl = R.api_replay_timelapse(597)
        assert all(not fr["cells"] for fr in tl["frames"]), "노드 장애는 셀로 칠해지는 것이 없다 — 0 이 사실이다"
