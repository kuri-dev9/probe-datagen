"""장애 → 지도의 탐지 이력 연결 (2026-11-07 · `ui._truth_of` · `ui._match_score` · `/api/faults/{id}/incidents`).

장애 창의 "지도로 보기"가 그 장애로 탐지된 이력을 찾는다. 주입 시간(+ 확정 대기 `SETTLE_MIN`)과 겹치는 이력만, 맞는 순서로 — 원인 4 · 대상 3 · 지표 2 ·
판정 단위(L2 2 · L1 묶음 1). 정답지는 최근 장애만 담으므로 없으면 종류 기대값으로만 본다. graphio 에 못 닿은 것과 이력이 없는 것은 다른 사실이다(불변규칙 17).

DB·네트워크를 쓰지 않는다 — 장애 행과 graphio 응답은 가짜다.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime, timedelta

import pytest

from datagen import ui
from datagen.scenarios import KIND_SPECS, SETTLE_MIN

T0 = datetime(2026, 11, 7, 12, 0)
SPEC = KIND_SPECS["gw_resource"]
TRUTH = {"truth_code": "GW_RESOURCE_EXHAUSTION", "expect_kpi": "C-02", "expect_node": "SGW-7",
         "cells": ["100-1", "100-2"]}


def _inc(**kw):
    base = {"incident_id": "IX1", "title": "t", "severity": "Major", "opened_ts": "2026-11-07 12:05:00",
            "closed_ts": None, "updated_ts": "2026-11-07 12:10:00", "root_code": None, "trigger_kpi": None,
            "event_unit": "PIECE", "scopes": [], "detail": {}}
    return {**base, **kw}


# ---------------------------------------------------------------- _truth_of

@pytest.fixture
def truth_file(tmp_path, monkeypatch):
    p = tmp_path / "ground_truth.json"
    monkeypatch.setattr(ui, "_pem_cfg", lambda: {"truth_out": str(p)})
    return p


def _doc(*ids):
    return {"scenarios": [{"scenario_id": f"F-{i}", "truth_code": f"C{i}"} for i in ids]}


def test_truth_of_reads_that_fault_only(truth_file):
    truth_file.write_text(json.dumps(_doc(167, 168)), encoding="utf-8")
    assert ui._truth_of(168) == {"scenario_id": "F-168", "truth_code": "C168"}
    assert ui._truth_of(167)["truth_code"] == "C167"


def test_truth_of_is_empty_for_a_fault_the_file_does_not_hold(truth_file):
    truth_file.write_text(json.dumps(_doc(168)), encoding="utf-8")
    assert ui._truth_of(100) == {}


def test_truth_of_is_empty_when_the_file_is_missing_or_broken(truth_file):
    assert ui._truth_of(1) == {}
    truth_file.write_text("{깨짐", encoding="utf-8")
    assert ui._truth_of(1) == {}


def test_truth_of_tolerates_a_file_without_scenarios(truth_file):
    truth_file.write_text("{}", encoding="utf-8")
    assert ui._truth_of(1) == {}
    truth_file.write_text(json.dumps({"scenarios": None}), encoding="utf-8")
    assert ui._truth_of(1) == {}


def test_truth_of_resolves_a_relative_path_against_the_repo_root(monkeypatch):
    monkeypatch.setattr(ui, "_pem_cfg", lambda: {"truth_out": "data/truth/does_not_exist_x.json"})
    assert ui._truth_of(1) == {}


# ---------------------------------------------------------------- _match_score

def test_a_perfect_match_is_cause_kpi_target_and_the_unit_bonus():
    i = _inc(root_code="GW_RESOURCE_EXHAUSTION", trigger_kpi="C-02", event_unit="L2_EVENT",
             detail={"axis_ids": ["SGW-7"]})
    assert ui._match_score(i, SPEC, TRUTH, "") == 4 + 2 + 3 + 2


@pytest.mark.parametrize("kw,want", [
    ({"root_code": "GW_RESOURCE_EXHAUSTION"}, 4),
    ({"trigger_kpi": "C-02"}, 2),
    ({"event_unit": "L2_EVENT"}, 2),
    ({"event_unit": "L1_BUNDLE"}, 1),
    ({"event_unit": "PIECE"}, 0),
    ({"root_code": "OTHER", "trigger_kpi": "Q-02"}, 0),
])
def test_each_part_scores_by_itself(kw, want):
    assert ui._match_score(_inc(**kw), SPEC, TRUTH, "") == want


def test_a_target_is_matched_by_the_truth_cells():
    i = _inc(scopes=[{"scope_type": "CELL", "scope_id": "100-2"}])
    assert ui._match_score(i, SPEC, TRUTH, "") == 3
    assert ui._match_score(_inc(scopes=[{"scope_type": "CELL", "scope_id": "999-1"}]), SPEC, TRUTH, "") == 0


def test_only_cell_scopes_count_for_the_cells():
    assert ui._match_score(_inc(scopes=[{"scope_type": "SGG", "scope_id": "100-2"}]), SPEC, TRUTH, "") == 0


def test_a_target_is_matched_by_the_expected_node_or_the_target_text_in_the_axis():
    assert ui._match_score(_inc(detail={"axis_ids": ["SGW-7", "MME-3"]}), SPEC, TRUTH, "") == 3
    assert ui._match_score(_inc(detail={"axis_ids": ["SGW-9"]}), SPEC, TRUTH, "") == 0
    assert ui._match_score(_inc(detail={"axis_ids": ["MME-3"]}), SPEC, {}, "MME-3") == 3


def test_an_enb_target_is_matched_by_the_cells_of_that_enb():
    i = _inc(scopes=[{"scope_type": "CELL", "scope_id": "678598-3"}])
    assert ui._match_score(i, SPEC, {}, "ENB:678598") == 3
    assert ui._match_score(i, SPEC, {}, "enb: 678598") == 3, "대소문자·공백을 가리지 않는다"
    assert ui._match_score(i, SPEC, {}, "ENB:1") == 0
    assert ui._match_score(i, SPEC, {}, "") == 0


def test_the_cause_and_kpi_fall_back_to_the_kind_spec_when_the_truth_is_gone():
    i = _inc(root_code=SPEC["truth_code"], trigger_kpi=SPEC["expect_kpi"])
    assert ui._match_score(i, SPEC, {}, "") == 4 + 2


def test_nothing_known_scores_zero_without_crashing():
    assert ui._match_score({}, {}, {}, "") == 0
    assert ui._match_score(_inc(scopes=None, detail=None), {}, {}, "") == 0


def test_a_cause_match_outranks_a_kpi_match_and_ties_with_kpi_plus_l2_unit():
    cause = ui._match_score(_inc(root_code="GW_RESOURCE_EXHAUSTION"), SPEC, TRUTH, "")
    assert cause > ui._match_score(_inc(trigger_kpi="C-02"), SPEC, TRUTH, "")
    assert cause > ui._match_score(_inc(event_unit="L2_EVENT"), SPEC, TRUTH, "")
    assert cause == ui._match_score(_inc(trigger_kpi="C-02", event_unit="L2_EVENT"), SPEC, TRUTH, "")


# ---------------------------------------------------------------- /api/faults/{id}/incidents

class _Cur:
    def __init__(self, row, cols):
        self._row, self.description = row, [(c,) for c in cols]
        self.sql = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.sql.append((sql, params))

    def fetchone(self):
        return self._row


class _Conn:
    COLS = ["fault_id", "kind", "target", "start_ts", "duration_min", "intensity", "mode", "status", "note",
            "created_at", "updated_at"]

    def __init__(self, row):
        self.cur, self.closed = _Cur(row, self.COLS), False

    def cursor(self):
        return self.cur

    def close(self):
        self.closed = True


def _fault_row(kind="gw_resource", target=None, start=T0, minutes=11):
    return (168, kind, target, start, minutes, 1.0, "manual", "done", None, start, start)


class _Resp:
    def __init__(self, doc):
        self._b = json.dumps(doc).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def api(monkeypatch):
    """장애 행·graphio 응답을 갈아 끼운다. 호출한 URL 도 남긴다."""
    st = {"row": _fault_row(), "doc": {"incidents": []}, "urls": [], "conn": None, "boom": None, "base": "http://g:8008"}

    def connect():
        st["conn"] = _Conn(st["row"])
        return st["conn"]

    def urlopen(url, timeout=None):
        st["urls"].append((url, timeout))
        if st["boom"]:
            raise st["boom"]
        return _Resp(st["doc"])

    monkeypatch.setattr(ui, "_connect", connect)
    monkeypatch.setattr(ui, "_pem_cfg", lambda: {"graphio_api": st["base"], "truth_out": "/nonexistent/x.json"})
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    return st


def _ids(out):
    return [i["incident_id"] for i in out["incidents"]]


def test_an_unknown_fault_is_reported_not_empty(api):
    api["row"] = None
    out = ui.api_fault_incidents(999)
    assert out["incidents"] == [] and "999" in out["error"] and api["conn"].closed


def test_a_missing_graphio_address_is_reported(api):
    api["base"] = ""
    out = ui.api_fault_incidents(168)
    assert out["incidents"] == [] and "graphio_api" in out["error"] and not api["urls"]


@pytest.mark.parametrize("exc", [urllib.error.URLError("거부"), TimeoutError("느림"), ConnectionResetError("끊김")])
def test_a_graphio_that_cannot_be_reached_is_told_apart_from_no_history(api, exc):
    api["boom"] = exc
    out = ui.api_fault_incidents(168)
    assert out["incidents"] == [] and out["error"].startswith("graphio API 에 닿지 않는다")


def test_the_history_is_read_from_the_map_list_with_a_timeout(api):
    ui.api_fault_incidents(168)
    # 최근 200건이 아니라 `recent=1`(최근 `console.incident_list_days` 일) — 며칠 지난 회차(F-121)도 찾는다(2026-11-09)
    # 그 장애 시간대만 받는다 — `since`·`until`(2026-11-09 · 7일치 512건 2.2MB 를 받아 거르던 것). 상세는 test_fault_incidents_since_1109
    base_q = "http://g:8008/api/incidents?scope=1&recent=1&since=2026-11-07T12%3A00%3A00&until=2026-11-07T12%3A18%3A00"
    assert api["urls"] == [(base_q, 10)]
    api["base"] = "http://g:8008/"
    ui.api_fault_incidents(168)
    assert api["urls"][1][0] == base_q, "끝 슬래시는 떼고 붙인다"


def test_the_fault_is_read_by_id(api):
    ui.api_fault_incidents(168)
    sql, params = api["conn"].cur.sql[0]
    assert "FAULT_ID = %s" in sql and params == [168]


def test_an_empty_history_has_no_error(api):
    assert ui.api_fault_incidents(168) == {"incidents": []}


def _times(opened, closed=None, updated=None):
    return {"opened_ts": opened, "closed_ts": closed, "updated_ts": updated}


def test_only_incidents_overlapping_the_injection_and_the_settle_wait_are_listed(api):
    end = T0 + timedelta(minutes=11 + SETTLE_MIN)                       # 12:18
    api["doc"] = {"incidents": [
        _inc(incident_id="before", **_times("2026-11-07 11:00:00", "2026-11-07 11:59:00")),
        _inc(incident_id="ends_at_start", **_times("2026-11-07 11:50:00", "2026-11-07 12:00:00")),
        _inc(incident_id="inside", **_times("2026-11-07 12:05:00", "2026-11-07 12:12:00")),
        _inc(incident_id="opens_at_end", **_times(end.strftime("%Y-%m-%d %H:%M:%S"), None, "2026-11-07 12:30:00")),
        _inc(incident_id="after", **_times("2026-11-07 12:18:01", None, "2026-11-07 12:30:00")),
        _inc(incident_id="spans_all", **_times("2026-11-07 11:00:00", "2026-11-07 13:00:00")),
    ]}
    assert sorted(_ids(ui.api_fault_incidents(168))) == ["ends_at_start", "inside", "opens_at_end", "spans_all"]


def test_the_close_time_falls_back_to_the_update_time_then_to_the_open_time(api):
    api["doc"] = {"incidents": [
        _inc(incident_id="upd_in", **_times("2026-11-07 11:00:00", None, "2026-11-07 12:05:00")),
        _inc(incident_id="upd_out", **_times("2026-11-07 11:00:00", None, "2026-11-07 11:30:00")),
        _inc(incident_id="open_only_in", **_times("2026-11-07 12:03:00", None, None)),
        _inc(incident_id="open_only_out", **_times("2026-11-07 11:03:00", None, None)),
    ]}
    assert sorted(_ids(ui.api_fault_incidents(168))) == ["open_only_in", "upd_in"]


def test_an_incident_without_a_readable_open_time_is_left_out(api):
    api["doc"] = {"incidents": [_inc(incident_id="bad", opened_ts="어제"), _inc(incident_id="none", opened_ts=None),
                                _inc(incident_id="ok")]}
    assert _ids(ui.api_fault_incidents(168)) == ["ok"]


def test_a_missing_incident_list_is_an_empty_one(api):
    api["doc"] = {}
    assert ui.api_fault_incidents(168) == {"incidents": []}
    api["doc"] = {"incidents": None}
    assert ui.api_fault_incidents(168) == {"incidents": []}


def test_the_list_is_ordered_by_score_then_by_open_time(api):
    api["doc"] = {"incidents": [
        _inc(incident_id="late_none", opened_ts="2026-11-07 12:09:00"),
        _inc(incident_id="cause", opened_ts="2026-11-07 12:08:00", root_code=SPEC["truth_code"]),
        _inc(incident_id="early_none", opened_ts="2026-11-07 12:02:00"),
        _inc(incident_id="bundle", opened_ts="2026-11-07 12:07:00", event_unit="L1_BUNDLE"),
    ]}
    out = ui.api_fault_incidents(168)
    assert _ids(out) == ["cause", "bundle", "early_none", "late_none"]
    assert [i["score"] for i in out["incidents"]] == [4, 1, 0, 0]


def test_each_row_carries_only_the_fields_the_map_needs(api):
    api["doc"] = {"incidents": [_inc(incident_id="x", scopes=[{"scope_type": "CELL", "scope_id": "1-1"}], detail={"a": 1})]}
    row = ui.api_fault_incidents(168)["incidents"][0]
    assert set(row) == {"incident_id", "title", "severity", "opened_ts", "root_code", "trigger_kpi", "event_unit", "score"}


def test_the_enb_target_of_the_fault_row_scores_the_cells(api):
    api["row"] = _fault_row(target="ENB:678598")
    api["doc"] = {"incidents": [
        _inc(incident_id="mine", scopes=[{"scope_type": "CELL", "scope_id": "678598-3"}]),
        _inc(incident_id="other", scopes=[{"scope_type": "CELL", "scope_id": "1-1"}])]}
    out = ui.api_fault_incidents(168)
    assert _ids(out) == ["mine", "other"] and out["incidents"][0]["score"] == 3


def test_an_unknown_kind_still_lists_by_overlap(api):
    api["row"] = _fault_row(kind="no_such_kind")
    api["doc"] = {"incidents": [_inc(incident_id="x")]}
    assert _ids(ui.api_fault_incidents(168)) == ["x"]


def test_the_connection_is_closed(api):
    ui.api_fault_incidents(168)
    assert api["conn"].closed


def test_the_zero_duration_is_read_as_the_settle_wait_only(api):
    api["row"] = _fault_row(minutes=None)
    api["doc"] = {"incidents": [_inc(incident_id="in", opened_ts="2026-11-07 12:06:00"),
                                _inc(incident_id="out", opened_ts="2026-11-07 12:08:00", updated_ts="2026-11-07 12:09:00")]}
    assert _ids(ui.api_fault_incidents(168)) == ["in"]
