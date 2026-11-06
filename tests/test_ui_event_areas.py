"""gen-ui 사건 지도 — 구성 조각의 칸·구역 (`ui._event_areas` · `api_map_event`, 2026-11-06 사용자 결정 "메시 넣어").

L1·L2 사건을 누르면 윤곽 안에 구성 조각 **전체**의 메시·구역을 칠한다. graphio `/map?with_areas=true` 가 `{level, code}` 목록을
주고, 이 함수가 축별로 모아 화면의 `geoShapes` 가 조각 하나처럼 받는 모양으로 낸다.

지키는 것 — ① **넓은 축부터** 준다(나중에 그린 것이 위에 와서 구 안의 메시가 구에 가리지 않는다) ② 칸 정보가 없는 사건
(옛 graphio 응답 · 칸 없는 사건)은 **빈 목록**이다 — 화면은 지금처럼 윤곽만 그린다 ③ 메시·구역이 아닌 축은 그리지 않는다.
DB·네트워크 없이 돈다 — 정목 중심점 조회와 `urlopen` 은 스텁이다.
"""
from __future__ import annotations

import io
import json

import pytest

from datagen import ui


@pytest.fixture(autouse=True)
def _no_dong_query(monkeypatch):
    """ADM3(丁目) 중심점 조회는 DB 를 읽는다 — 스텁으로 바꾸고 부른 인자를 남긴다."""
    calls = []

    def fake(ids):
        calls.append(list(ids))
        return {g: {"id": g, "lon": 139.7, "lat": 35.6} for g in ids if g != "NO-CENTER"}

    monkeypatch.setattr(ui, "_dong_at", fake)
    return calls


# ---------------------------------------------------------------- 축 순서

def test_axes_come_out_widest_first_whatever_the_input_order():
    rows = [{"level": lv, "code": f"{lv}-1"} for lv in ("MESH250", "ADM2", "MESH1K", "ADM1", "MESH500", "ADM3")]
    assert [a["axis"] for a in ui._event_areas(rows)] == ["ADM1", "ADM2", "ADM3", "MESH1K", "MESH500", "MESH250"]
    assert [a["axis"] for a in ui._event_areas(rows)] == list(ui._AREA_ORDER)


def test_a_wide_axis_is_never_after_a_narrow_one():
    """구(ADM2)가 메시 뒤에 오면 구 면이 메시를 덮는다."""
    out = [a["axis"] for a in ui._event_areas([{"level": "MESH500", "code": "533945"}, {"level": "ADM2", "code": "13104"}])]
    assert out.index("ADM2") < out.index("MESH500")


def test_only_present_axes_are_returned():
    out = ui._event_areas([{"level": "MESH1K", "code": "53394566"}])
    assert [a["axis"] for a in out] == ["MESH1K"]


# ---------------------------------------------------------------- 비어 있을 때

def test_no_rows_is_an_empty_list():
    assert ui._event_areas([]) == []


def test_axes_that_cannot_be_drawn_are_dropped():
    """GLOBAL·APN 같은 위치 없는 축이나 모르는 값은 그리지 않는다."""
    assert ui._event_areas([{"level": "GLOBAL", "code": "ALL"}, {"level": "APN", "code": "x"}, {"level": None, "code": "?"}]) == []


def test_the_dong_lookup_is_skipped_when_there_is_no_adm3(_no_dong_query):
    ui._event_areas([{"level": "MESH500", "code": "533945752"}])
    assert _no_dong_query == []


# ---------------------------------------------------------------- 코드 모으기

def test_codes_are_collected_per_axis_in_input_order_as_strings():
    rows = [{"level": "MESH500", "code": 533945752}, {"level": "MESH500", "code": "533945654"},
            {"level": "ADM2", "code": 13104}]
    out = {a["axis"]: a["geo_ids"] for a in ui._event_areas(rows)}
    assert out == {"ADM2": ["13104"], "MESH500": ["533945752", "533945654"]}


def test_adm3_carries_centres_only_for_codes_that_were_found():
    out = ui._event_areas([{"level": "ADM3", "code": "11500006"}, {"level": "ADM3", "code": "NO-CENTER"}])
    a = out[0]
    assert a["geo_ids"] == ["11500006", "NO-CENTER"]
    assert [d["id"] for d in a["dong"]] == ["11500006"], "중심점이 없는 정목은 그리지 않는다(경계가 없다)"


def test_only_adm3_gets_a_dong_list():
    out = ui._event_areas([{"level": "ADM3", "code": "1"}, {"level": "MESH1K", "code": "2"}, {"level": "ADM2", "code": "3"}])
    assert {a["axis"]: ("dong" in a) for a in out} == {"ADM3": True, "MESH1K": False, "ADM2": False}


# ---------------------------------------------------------------- api_map_event

class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _serve(monkeypatch, doc):
    """graphio `/map` 응답을 흉내 낸다 — 부른 주소를 남긴다."""
    urls = []

    def fake_urlopen(url, timeout=None):
        urls.append(url)
        return _Resp(json.dumps(doc).encode())

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr(ui, "_pem_cfg", lambda: {"graphio_api": "http://graphio.test/"})
    return urls


def test_the_request_asks_for_cells_only_plus_areas(monkeypatch):
    urls = _serve(monkeypatch, {"cells": [], "unmapped": 0})
    ui.api_map_event("IX2611061355d97e")
    assert urls == ["http://graphio.test/api/incidents/IX2611061355d97e/map?cells_only=true&with_areas=true"]


def test_an_old_graphio_answer_without_areas_gives_an_empty_list(monkeypatch):
    """web 재시작 전이거나 칸 없는 사건 — 화면은 지금처럼 윤곽만 그린다."""
    _serve(monkeypatch, {"cells": [{"key": "131157-1", "lat": 35.6, "lon": 139.7}], "unmapped": 0})
    out = ui.api_map_event("IX1")
    assert out["areas"] == [] and out["areas_capped"] is False


def test_areas_and_the_capped_flag_are_passed_through(monkeypatch):
    _serve(monkeypatch, {"cells": [], "unmapped": 0, "areas_capped": True,
                         "areas": [{"level": "MESH250", "code": "5339455634"}, {"level": "ADM1", "code": "13"}]})
    out = ui.api_map_event("IX1")
    assert [a["axis"] for a in out["areas"]] == ["ADM1", "MESH250"]
    assert out["areas_capped"] is True


def test_one_point_per_enb_and_the_cell_count_is_kept(monkeypatch):
    cells = [{"key": "131157-1", "lat": 35.60, "lon": 139.70}, {"key": "131157-2", "lat": 35.61, "lon": 139.71},
             {"key": "140421-0", "lat": 35.70, "lon": 139.80}]
    _serve(monkeypatch, {"cells": cells, "unmapped": 2})
    out = ui.api_map_event("IX1")
    assert out["enbs"] == ["131157", "140421"] and len(out["pts"]) == 2
    assert out["cells"] == 3 and out["unmapped"] == 2


def test_a_missing_graphio_address_is_an_error_not_an_empty_map(monkeypatch):
    monkeypatch.setattr(ui, "_pem_cfg", lambda: {})
    out = ui.api_map_event("IX1")
    assert "graphio_api" in out["error"] and out["enbs"] == [] and out["pts"] == []
