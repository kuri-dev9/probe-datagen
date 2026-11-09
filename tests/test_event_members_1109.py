"""gen-ui 사건 지도 — 구성 조각별 정보 (`ui._event_members` · `api_map_event`, 2026-11-09 사용자 결정 C안).

지도 바에서 조각을 하나씩 골라 볼 때 그 조각의 위치·장비·에러 코드를 보인다. graphio `/map?with_members=true` 가 준
조각 행을 이 함수가 화면 모양으로 바꾼다.

지키는 것 — ① 사건 셀 목록(`cells`)에 **좌표가 없는 셀은 점을 만들지 않는다**(수만 센다 · graphio 불변규칙 12)
② `SGW-0032` 꼴 노드 축 값은 `sgw:0032` 노드로 바뀐다 ③ 같은 eNB 의 셀은 점 하나 ④ 지리 축 조각의 칸은 `_event_areas` 를
거친다. DB·네트워크 없이 돈다 — 정목 중심점 조회는 스텁이다.
"""
from __future__ import annotations

import pytest

from datagen import ui


@pytest.fixture(autouse=True)
def _no_dong_query(monkeypatch):
    monkeypatch.setattr(ui, "_dong_at", lambda ids: {g: {"id": g, "lon": 139.7, "lat": 35.6} for g in ids})


def _row(**kw):
    base = {"incident_id": "IX1", "severity": "Critical", "title": "t", "trigger_kpi": "C-02",
            "opened_ts": "2026-11-09T15:34:00", "updated_ts": "2026-11-09T15:44:00",
            "root_code": "CHANGE_SUSPECTED", "root_label": "구성·설정 변화 의심", "confidence": 0.65,
            "axis": "CELL", "axis_ids": [], "areas": [], "cells": []}
    return base | kw


CELLS = [{"key": "100-1", "lon": 139.1, "lat": 35.1}, {"key": "100-2", "lon": 139.1, "lat": 35.1},
         {"key": "200-1", "lon": 139.2, "lat": 35.2}]


def test_cells_without_coordinates_make_no_point():
    out = ui._event_members([_row(cells=["100-1", "999-1", "888-3"])], CELLS)[0]
    assert out["cells"] == 3                       # 수는 그대로 센다
    assert out["enbs"] == ["100"] and out["pts"] == [[139.1, 35.1]]


def test_cells_of_one_enb_make_one_point():
    out = ui._event_members([_row(cells=["100-1", "100-2", "200-1"])], CELLS)[0]
    assert out["enbs"] == ["100", "200"]
    assert out["pts"] == [[139.1, 35.1], [139.2, 35.2]]


def test_no_cells_at_all_is_empty_not_an_error():
    out = ui._event_members([_row()], [])[0]
    assert out["cells"] == 0 and out["enbs"] == [] and out["pts"] == []


def test_node_axis_value_becomes_a_node_key():
    out = ui._event_members([_row(axis="NODE", axis_ids=["SGW-0032"])], [])[0]
    assert out["nodes"] == ["sgw:0032"]
    assert out["axis_vals"] == ["SGW-0032"]


def test_all_three_node_kinds_are_accepted_and_other_axis_values_are_not():
    out = ui._event_members([_row(axis_ids=["MME-0006", "pgw-0015", "SGW-0011", "KR", "mopera.net", "SGW-"])], [])[0]
    assert out["nodes"] == ["mme:0006", "pgw:0015", "sgw:0011"]


def test_axis_values_are_capped_for_the_bar():
    vals = [f"d{i}.net" for i in range(ui.GEO_LIST + 5)]
    assert len(ui._event_members([_row(axis="DOMAIN", axis_ids=vals)], [])[0]["axis_vals"]) == ui.GEO_LIST


def test_geo_piece_areas_go_through_event_areas():
    rows = [{"level": "MESH1K", "code": "53394630"}, {"level": "ADM2", "code": "13104"}]
    out = ui._event_members([_row(axis="MESH1K", areas=rows)], [])[0]
    assert out["areas"] == ui._event_areas(rows)
    assert [a["axis"] for a in out["areas"]] == ["ADM2", "MESH1K"]


def test_pieces_keep_their_order_and_carry_the_conclusion_fields():
    out = ui._event_members([_row(incident_id="A"), _row(incident_id="B", root_code="UNDETERMINED",
                                                          root_label="판단 보류", confidence=0.0)], [])
    assert [m["incident_id"] for m in out] == ["A", "B"]
    for k in ("severity", "title", "trigger_kpi", "opened_ts", "updated_ts", "root_code", "root_label", "confidence", "axis"):
        assert k in out[0]
    assert out[1]["root_code"] == "UNDETERMINED" and out[1]["confidence"] == 0.0


def test_missing_lists_are_treated_as_empty():
    row = _row()
    for k in ("cells", "axis_ids", "areas"):
        row[k] = None
    out = ui._event_members([row], CELLS)[0]
    assert out["cells"] == 0 and out["nodes"] == [] and out["areas"] == []


def test_no_rows_is_an_empty_list():
    assert ui._event_members([], CELLS) == []


# ---------------------------------------------------------------- 뗀 셀 (2026-11-09 16:30)
# 사건에서 뗀 셀은 그 조각의 셀이다 — 셀 1개짜리 조각이 "장비 범위가 없다"로 보이지 않게 점·eNB 를 따로 낸다.

GONE = [{"key": "804633-1", "lon": 139.74, "lat": 35.63}]


def test_detached_cell_gets_its_own_point_and_enb():
    out = ui._event_members([_row(cells=[], detached_cells=["804633-1"], detached_kpis=["C-01/D4"],
                                  detached_reason="사건 KPI 와 겹치지 않는다")], CELLS + GONE)[0]
    assert out["detached"] == 1 and out["denbs"] == ["804633"] and out["dpts"] == [[139.74, 35.63]]
    assert out["cells"] == 0 and out["pts"] == [] and out["enbs"] == []     # 사건 범위로 세지 않는다
    assert out["detached_kpis"] == ["C-01/D4"] and out["detached_reason"] == "사건 KPI 와 겹치지 않는다"


def test_detached_cell_without_coordinates_is_counted_but_has_no_point():
    out = ui._event_members([_row(detached_cells=["999-9"])], CELLS)[0]
    assert out["detached"] == 1 and out["dpts"] == [] and out["denbs"] == []


def test_detached_cells_of_one_enb_make_one_point():
    cells = GONE + [{"key": "804633-2", "lon": 139.74, "lat": 35.63}]
    out = ui._event_members([_row(detached_cells=["804633-1", "804633-2"])], cells)[0]
    assert out["detached"] == 2 and out["denbs"] == ["804633"] and len(out["dpts"]) == 1


def test_piece_without_detached_fields_gets_zero_and_empty():
    """옛 graphio 응답(뗀 셀 필드 없음) — 오류 없이 0 이다."""
    out = ui._event_members([_row()], CELLS)[0]
    assert out["detached"] == 0 and out["dpts"] == [] and out["denbs"] == []
    assert out["detached_reason"] is None and out["detached_kpis"] == []


def test_detached_kpis_are_strings():
    out = ui._event_members([_row(detached_cells=["804633-1"], detached_kpis=["C-01", 3])], GONE)[0]
    assert out["detached_kpis"] == ["C-01", "3"]


def test_api_map_event_looks_up_detached_cells_among_the_top_level_detached_coordinates(monkeypatch):
    """조각의 뗀 셀 좌표는 사건 셀이 아니라 최상위 `detached_cells` 에서 온다."""
    import io, json
    doc = {"cells": CELLS, "unmapped": 0, "detached_cells": GONE,
           "members": [_row(cells=[], detached_cells=["804633-1"])]}

    class R(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr("urllib.request.urlopen", lambda url, timeout=None: R(json.dumps(doc).encode()))
    monkeypatch.setattr(ui, "_pem_cfg", lambda: {"graphio_api": "http://graphio.test"})
    m = ui.api_map_event("IX1")["members"][0]
    assert m["dpts"] == [[139.74, 35.63]] and m["denbs"] == ["804633"]
