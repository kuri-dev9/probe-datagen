"""정답지 `expect_geo` 를 고른 셀로 계산한다 (`faults.build` → `Scenario.as_truth` · 2026-11-11 사용자 결정 B).

kind 의 고정 기대 지리(WIDE 등)는 대표 모양이라 실제로 고른 도쿄 셀(반경 7~8km — WIDE 60km 를 넘을 수 없다)과 어긋났다(5·11·21·22번).
셀이 있는 kind 는 대상 셀로 `scenarios.geo_shape`(= graphio `geo_pattern` 과 같은 판정)를 재 `expect_geo` 에 넣고 고정값은 `expect_geo_static`
에 남긴다. 셀이 없는 축 kind(도메인·APN·외부 경로)와 좌표가 없는 대상은 고정값 그대로다. 판정 문턱(0.9·2.5·3.0·3.2·60km)은 바꾸지 않는다. DB 없이.
"""
from __future__ import annotations

import json
import random
from datetime import datetime

import pytest

from datagen import faults
from datagen import scenarios as S
from test_geo_target_b import KM_LAT, LAT0, LON0, _cell, _line, _ring

T = datetime(2026, 11, 11, 1, 0)
ANCHOR = datetime(2026, 11, 10)


def _master():
    return _ring("a", 30, 2.0) + _ring("b", 20, 1.0, sgg="13105", lat0=35.70, lon0=139.75, start=100)


def _build(kind, cells=None, target="", fid=900, seed=3):
    row = {"fault_id": fid, "kind": kind, "intensity": 1.0, "duration_min": 11, "start_ts": T, "target": target}
    return faults.build(row, cells or _master(), ANCHOR, random.Random(seed), seed=seed)


def _truth(kind, **kw):
    sc = _build(kind, **kw)
    assert sc is not None
    return sc, sc.as_truth()


def _target_cells(sc, cells=None):
    by = {c.cell_key: c for c in (cells or _master())}
    return [by[k] for k in sc.target.cell_keys if k in by]


# ── 1 · 셀이 있는 kind 는 고른 셀로 잰다 ────────────────────────────────────

@pytest.mark.parametrize("kind", ["ran_radio", "transport_loss", "ims_pdn", "dns_degradation", "gw_resource", "mme_overload"])
def test_truth_expect_geo_is_geo_shape_of_the_chosen_cells(kind):
    sc, t = _truth(kind)
    assert sc.target.cell_keys, kind
    assert t["expect_geo"] == S.geo_shape(_target_cells(sc))


@pytest.mark.parametrize("kind", ["ran_radio", "dns_degradation", "ims_pdn", "mme_overload"])
def test_static_value_is_kept_beside_it_and_is_the_kind_spec_value(kind):
    sc, t = _truth(kind)
    assert t["expect_geo_static"] == S.KIND_SPECS[kind]["expect_geo"] == sc.expect_geo


def test_dns_degradation_static_wide_becomes_scatter_for_tokyo_sized_cells():
    sc, t = _truth("dns_degradation")
    assert t["expect_geo_static"] == "WIDE" and t["expect_geo"] == "SCATTER"       # 5번 no5-v9 모양


def test_truth_is_json_serialisable_with_both_keys():
    _, t = _truth("ims_pdn")
    back = json.loads(json.dumps(t))
    assert "expect_geo" in back and "expect_geo_static" in back


def test_other_truth_fields_are_unchanged():
    _, t = _truth("ims_pdn")
    for key in ("scenario_id", "kind", "truth_code", "expect_rules", "expect_alternatives", "start_minute", "duration", "shape", "grade"):
        assert key in t


# ── 2 · 셀이 없거나 좌표가 없으면 고정값 ────────────────────────────────────

@pytest.mark.parametrize("kind", ["external_server"])
def test_axis_kinds_without_cells_keep_the_static_value(kind):
    sc, t = _truth(kind)
    assert not sc.target.cell_keys
    assert t["expect_geo"] == t["expect_geo_static"] == "WIDE"
    assert sc.__dict__.get("_geo_shape") is None


def test_target_cells_without_coordinates_keep_the_static_value():
    cells = [_cell(f"u{i}-0", LAT0, LON0, mapped=False) for i in range(30)]
    sc, t = _truth("ims_pdn", cells=cells)
    assert sc.__dict__.get("_geo_shape") is None
    assert t["expect_geo"] == t["expect_geo_static"] == S.KIND_SPECS["ims_pdn"]["expect_geo"]


def test_unmapped_cells_are_ignored_by_the_shape():
    cells = _master() + [_cell(f"u{i}-0", LAT0 + 1.0, LON0, mapped=False) for i in range(20)]
    sc, t = _truth("ims_pdn", cells=cells)
    assert t["expect_geo"] == S.geo_shape([c for c in _target_cells(sc, cells) if c.geo_status == "MAPPED"])


# ── 3 · 25번은 좁힌 셀 · 15번 범위 배치는 노선 값이 이긴다 ────────────────────

def test_son_focus_uses_the_narrowed_cells():
    sc, t = _truth("son_param_step") if "son_param_step" in S.KIND_SPECS else (None, None)
    if sc is None:
        pytest.skip("25번 kind 이름이 다르다")
    assert len(sc.target.cell_keys) <= 3
    assert t["expect_geo"] == S.geo_shape(_target_cells(sc))


def test_chosen_shape_never_overrides_a_later_explicit_geo():
    sc = _build("ims_pdn")
    sc.expect_geo = "CLUSTER"
    sc._geo_shape = "CORRIDOR"
    assert sc.as_truth()["expect_geo"] == "CORRIDOR" and sc.as_truth()["expect_geo_static"] == "CLUSTER"


# ── 4 · geo_shape 경계 — graphio geo_pattern 과 같은 판정 ────────────────────

CASES = {
    "point_single": [_cell("p-0", LAT0, LON0)],
    "point_tight": _ring("a", 8, 0.5),
    "cluster_0_95": _ring("b", 8, 0.95),
    "cluster_2_9": _ring("c", 12, 2.9),
    "scatter_3_2": _ring("d", 12, 3.2),
    "scatter_7": _ring("e", 20, 7.0),
    "corridor_9": _line("l", 10, 9.0),
    "corridor_few": _line("m", 4, 9.0),
    "short_line": _line("n", 10, 3.0),
    "wide_12": [_cell(f"w{i}-0", LAT0 + 1.5 * i / 11, LON0) for i in range(12)],
    "wide_11": [_cell(f"x{i}-0", LAT0 + 1.5 * i / 11, LON0) for i in range(11)],
}


@pytest.mark.parametrize("name", sorted(CASES))
def test_geo_shape_matches_graphio_geo_pattern(name):
    det = pytest.importorskip("graphio.detect.detector")
    cells = CASES[name]
    assert S.geo_shape(cells) == det.geo_pattern([(c.latitude, c.longitude) for c in cells])[0], name


@pytest.mark.parametrize("name,want", [("point_single", "POINT"), ("point_tight", "POINT"), ("cluster_0_95", "CLUSTER"),
                                       ("cluster_2_9", "CLUSTER"), ("scatter_3_2", "SCATTER"), ("scatter_7", "SCATTER"),
                                       ("corridor_9", "CORRIDOR"), ("corridor_few", "SCATTER"), ("wide_12", "WIDE"), ("wide_11", "SCATTER")])
def test_geo_shape_boundary_cases_have_the_documented_shapes(name, want):
    assert S.geo_shape(CASES[name]) == want


def test_thresholds_are_the_documented_ones():
    doc = S.geo_shape.__doc__
    assert "0.9·2.5·3.2·3.0·60km" in doc
    assert S.CORRIDOR_ELONG and S.CORRIDOR_RADIUS_KM == 2.5 and S.CORRIDOR_ELONG == 3.2


# ── 5 · 새 키가 알려진 키를 해치지 않는다(graphio 채점 대조는 아래 별도 확인) ──────

def test_extra_static_key_does_not_break_consumers_reading_known_keys():
    _, t = _truth("ims_pdn")
    known = {"scenario_id", "kind", "scope_kind", "expect_kpi", "expect_node", "title", "truth_code", "truth_category",
             "expect_rules", "expect_alternatives", "expect_geo", "start_minute", "duration", "shape", "grade"}
    assert known <= set(t) and "expect_geo_static" in t
