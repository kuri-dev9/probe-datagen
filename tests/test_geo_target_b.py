"""B안 대상 고르기 (2026-11-07) — 정답지 `expect_geo` 모양이 되도록 대상을 고른다.

* `scenarios.geo_shape` — graphio `detect/detector.geo_pattern` 과 같은 판정(문턱 0.9·2.5·3.2·3.0·60km)
* `faults._sgg_of_shape` — 2·7번: 시군구 셀 전부의 모양이 `expect_geo` 인 시군구
* `faults._auto_target` — 25번은 노선 구간, 없으면 `corridor_cells`, 2·7번은 시군구 셀 전부
* `faults._disaster_area` · `_busy_disaster_area` — 27번
DB 없이 돈다 — 합성 셀. graphio 가 import 되는 환경에서는 `geo_pattern` 과 직접 대조한다.
"""
from __future__ import annotations

import math
import random

import pytest

from datagen import faults
from datagen import scenarios as S
from datagen.cells import Cell

KM_LAT = 111.0
LAT0, LON0 = 35.68, 139.70
KM_LON = 111.320 * math.cos(math.radians(LAT0))


def _cell(key, lat, lon, sgg="13104", mapped=True, enb=None):
    e, _, n = key.partition("-")
    return Cell(cell_key=key, enb_id=enb or e, cell_id=int(n or 0), enb_seq=0, c_uid="", tac=100, plmn="44010",
                vlan_id="", latitude=lat, longitude=lon, adm_sido_cd="13", adm_sido_nm="", adm_sgg_cd=sgg,
                adm_sgg_nm="", adm_emd_cd=sgg + "001", adm_emd_nm="",
                geo_status="MAPPED" if mapped else "UNMAPPED", rat="EUTRAN", mme_id="0001", sgw_id="0001",
                pgw_id="0001", ims_pgw_id="0001", traffic_weight=1.0, display_name="")


def _ring(prefix, n, radius_km, sgg="13104", lat0=LAT0, lon0=LON0, start=0):
    return [_cell(f"{prefix}{start + i}-0",
                  lat0 + radius_km * math.sin(2 * math.pi * i / n) / KM_LAT,
                  lon0 + radius_km * math.cos(2 * math.pi * i / n) / KM_LON, sgg=sgg) for i in range(n)]


def _line(prefix, n, length_km, lat0=LAT0, lon0=LON0):
    return [_cell(f"{prefix}{i}-0", lat0, lon0 + (i * length_km / max(1, n - 1)) / KM_LON) for i in range(n)]


# ---------------------------------------------------------------- geo_shape

def test_geo_shape_no_mapped_cells_is_unknown():
    assert S.geo_shape([]) == "UNKNOWN"
    assert S.geo_shape([_cell("1-0", LAT0, LON0, mapped=False)]) == "UNKNOWN"


def test_geo_shape_single_and_tight_group_is_point():
    assert S.geo_shape([_cell("1-0", LAT0, LON0)]) == "POINT"
    assert S.geo_shape(_ring("a", 8, 0.5)) == "POINT"


def test_geo_shape_unmapped_cells_are_ignored():
    cells = _ring("a", 6, 0.4) + [_cell("far-0", LAT0 + 1.0, LON0, mapped=False)]
    assert S.geo_shape(cells) == "POINT"


def test_geo_shape_point_cluster_boundary_at_0_9km():
    assert S.geo_shape(_ring("a", 8, 0.85)) == "POINT"
    assert S.geo_shape(_ring("b", 8, 0.95)) == "CLUSTER"


def test_geo_shape_cluster_scatter_boundary_at_3km():
    assert S.geo_shape(_ring("a", 12, 2.9)) == "CLUSTER"
    assert S.geo_shape(_ring("b", 12, 3.2)) == "SCATTER"


def test_geo_shape_long_thin_group_is_corridor():
    assert S.geo_shape(_line("l", 10, 9.0)) == "CORRIDOR"


def test_geo_shape_corridor_needs_five_cells_and_radius():
    assert S.geo_shape(_line("l", 4, 9.0)) != "CORRIDOR"
    assert S.geo_shape(_line("m", 10, 3.0)) != "CORRIDOR", "반경 2.5km 이하는 선형이 아니다"


def test_geo_shape_very_wide_group():
    far = [_cell(f"w{i}-0", LAT0 + 1.5 * i / 11, LON0) for i in range(12)]
    assert S.geo_shape(far) == "WIDE"
    assert S.geo_shape(far[:11]) == "SCATTER", "광역은 셀 12개 이상일 때만"


def test_geo_shape_matches_graphio_geo_pattern_on_the_same_coordinates():
    det = pytest.importorskip("graphio.detect.detector")
    groups = [_ring("a", 8, 0.5), _ring("b", 8, 0.95), _ring("c", 12, 2.9), _ring("d", 12, 3.2),
              _line("l", 10, 9.0), _line("m", 4, 9.0), [_cell("p-0", LAT0, LON0)],
              [_cell(f"w{i}-0", LAT0 + 1.5 * i / 11, LON0) for i in range(12)]]
    for cells in groups:
        want = det.geo_pattern([(c.latitude, c.longitude) for c in cells])[0]
        assert S.geo_shape(cells) == want, [c.cell_key for c in cells][:3]


# ---------------------------------------------------------------- _sgg_of_shape

def _sgg_city():
    """A·C = 셀 24개 CLUSTER · B = 셀 24개 SCATTER · D = 셀 10개 CLUSTER(너무 작다)."""
    return (_ring("a", 24, 2.0, sgg="A") + _ring("b", 24, 4.5, sgg="B", lon0=LON0 + 0.5, start=100)
            + _ring("c", 24, 2.2, sgg="C", lon0=LON0 + 1.0, start=200)
            + _ring("d", 10, 2.0, sgg="D", lon0=LON0 + 1.5, start=300))


@pytest.mark.parametrize("kind", ["ran_radio", "transport_loss"])
def test_sgg_of_shape_picks_only_sggs_whose_whole_shape_matches(kind):
    cells = _sgg_city()
    picked = {faults._sgg_of_shape(kind, cells, random.Random(s)) for s in range(60)}
    assert picked == {"A", "C"}, "B 는 SCATTER · D 는 20셀 미만"


def test_sgg_of_shape_rotates_with_the_rng():
    cells = _sgg_city()
    assert len({faults._sgg_of_shape("ran_radio", cells, random.Random(s)) for s in range(40)}) == 2


def test_sgg_of_shape_same_rng_same_pick():
    cells = _sgg_city()
    assert (faults._sgg_of_shape("ran_radio", cells, random.Random(5))
            == faults._sgg_of_shape("ran_radio", cells, random.Random(5)))


def test_sgg_of_shape_none_when_nothing_matches(capsys):
    cells = _ring("b", 24, 4.5, sgg="B")
    assert faults._sgg_of_shape("ran_radio", cells, random.Random(0)) is None
    assert "시군구가 없다" in capsys.readouterr().out, "조용히 넘어가지 않고 남긴다"


def test_sgg_of_shape_ignores_unmapped_cells():
    cells = _ring("a", 24, 2.0, sgg="A")
    cells += [_cell(f"u{i}-0", LAT0, LON0, sgg="U", mapped=False) for i in range(30)]
    assert faults._sgg_of_shape("ran_radio", cells, random.Random(0)) == "A"


# ---------------------------------------------------------------- _auto_target — 2·7번

@pytest.mark.parametrize("kind", ["ran_radio", "transport_loss"])
def test_auto_target_takes_every_cell_of_the_chosen_sgg(kind):
    cells = _sgg_city()
    t = faults._auto_target(kind, cells, random.Random(1))
    code = next(iter(t.sgg_codes))
    assert code in {"A", "C"}
    assert t.cell_keys == {c.cell_key for c in cells if c.adm_sgg_cd == code}


def test_auto_target_falls_back_to_a_sgg_when_no_shape_matches():
    cells = _ring("b", 24, 4.5, sgg="B")
    t = faults._auto_target("ran_radio", cells, random.Random(0))
    assert t is not None and t.sgg_codes == {"B"}


# ---------------------------------------------------------------- _auto_target — 25번

def _route_cells():
    return _line("r", 40, 14.0)


def test_son_param_change_uses_a_route_segment_with_adjacent_pairs(monkeypatch):
    cells = _route_cells()
    calls = {}

    def fake_segment(routes, rng, *a, **k):
        calls["routes"] = routes
        seg = cells[:15]
        return seg, {"route": "L1", "cells": len(seg)}

    monkeypatch.setattr(S, "route_segment", fake_segment)
    t = faults._auto_target("son_param_change", cells, random.Random(0), routes=["L1"])
    assert calls["routes"] == ["L1"]
    assert t.cell_keys == {c.cell_key for c in cells[:15]}
    assert t.cell_pairs == [(a.cell_key, b.cell_key) for a, b in zip(cells[:14], cells[1:15])]
    assert t.route_seg == {"route": "L1", "cells": 15}


def test_son_param_change_falls_back_to_corridor_cells_without_routes(capsys):
    cells = _route_cells()
    t = faults._auto_target("son_param_change", cells, random.Random(0), routes=None)
    assert t is not None and t.cell_pairs, "방위각 띠 경로"
    assert "노선 구간을 못 골랐다" in capsys.readouterr().out


def test_son_param_change_falls_back_when_the_route_has_no_valid_segment(monkeypatch, capsys):
    monkeypatch.setattr(S, "route_segment", lambda *a, **k: None)
    t = faults._auto_target("son_param_change", _route_cells(), random.Random(0), routes=["L1"])
    assert t is not None
    assert "모양 미달" in capsys.readouterr().out


def test_route_target_none_when_no_routes(capsys):
    assert faults._route_target(None, random.Random(0), "son_param_change") is None
    assert "없음" in capsys.readouterr().out


# ---------------------------------------------------------------- 27번 — _disaster_area · _busy_disaster_area

def _disaster_master():
    """eNB 40개(셀 3개씩) — 격자 간격이 넓은 지역(CLUSTER 가 되는)과 촘촘한 지역(POINT)이 섞여 있다."""
    cells = []
    for i in range(20):                    # 촘촘 — 0.1km 간격
        for n in range(3):
            cells.append(_cell(f"{100 + i}-{n}", LAT0 + (i % 5) * 0.1 / KM_LAT, LON0 + (i // 5) * 0.1 / KM_LON,
                               enb=str(100 + i)))
    for i in range(20):                    # 듬성 — 0.5km 간격이라 25셀이 0.9km 를 넘는다
        for n in range(3):
            cells.append(_cell(f"{200 + i}-{n}", LAT0 + 0.5 + (i % 5) * 0.5 / KM_LAT,
                               LON0 + 0.5 + (i // 5) * 0.5 / KM_LON, enb=str(200 + i)))
    return cells


def test_disaster_area_retries_until_the_shape_matches(monkeypatch):
    seen = []

    def fake_shape(picked):
        seen.append(1)
        return "CLUSTER" if len(seen) >= 4 else "POINT"

    monkeypatch.setattr(S, "geo_shape", fake_shape)
    keys = faults._disaster_area(_disaster_master(), random.Random(0), 25)
    assert len(seen) == 4 and keys, "세 번 어긋나고 네 번째에 맞는다"


def test_disaster_area_gives_the_first_draw_and_warns_when_nothing_matches(monkeypatch, capsys):
    monkeypatch.setattr(S, "geo_shape", lambda picked: "POINT")
    monkeypatch.setattr(faults, "DISASTER_SHAPE_TRIES", 5)
    keys = faults._disaster_area(_disaster_master(), random.Random(0), 25)
    assert keys
    assert "못 찾았다" in capsys.readouterr().out


def test_busy_disaster_area_only_considers_areas_of_the_expected_shape(monkeypatch):
    cells = _disaster_master()
    rate = {c.enb_id: 5.0 for c in cells}
    shapes = []

    def spy(picked):
        r = S.geo_shape.__wrapped__(picked) if hasattr(S.geo_shape, "__wrapped__") else real(picked)
        shapes.append(r)
        return r

    real = S.geo_shape
    monkeypatch.setattr(S, "geo_shape", spy)
    keys = faults._busy_disaster_area(cells, rate, 25, "local", "F-1")
    assert keys is not None and real([c for c in cells if c.cell_key in keys]) == "CLUSTER"


def test_busy_disaster_area_falls_back_when_no_area_matches(monkeypatch, capsys):
    cells = _disaster_master()
    monkeypatch.setattr(S, "geo_shape", lambda picked: "POINT")
    keys = faults._busy_disaster_area(cells, {c.enb_id: 5.0 for c in cells}, 25, "local", "F-9")
    assert keys is not None
    assert "F-9 자연재해" in capsys.readouterr().out


def test_busy_disaster_area_is_deterministic_and_needs_rates():
    cells = _disaster_master()
    rate = {c.enb_id: 5.0 + int(c.enb_id) % 7 for c in cells}
    a = faults._busy_disaster_area(cells, rate, 25, "local", "F-1")
    assert a == faults._busy_disaster_area(cells, rate, 25, "local", "F-1")
    assert faults._busy_disaster_area(cells, {}, 25, "local", "F-1") is None, "호율이 없으면 후보가 없다"


# ---------------------------------------------------------------- 9번 · _ROUTE_KINDS · _route_target 모양 검사

def test_route_kinds_cover_the_three_corridor_scenarios():
    assert set(faults._ROUTE_KINDS) == {"ho_interruption", "son_param_change", "mobility"}
    for k in faults._ROUTE_KINDS:
        # 구간을 고르는 모양(`select_geo`)이 CORRIDOR 다. 25번은 정답 모양(`expect_geo`)이 POINT 로 갈렸다(2026-11-09)
        sp = faults.KIND_SPECS[k]
        assert (sp.get("select_geo") or sp["expect_geo"]) == "CORRIDOR", k
    assert faults.KIND_SPECS["son_param_change"]["expect_geo"] == "POINT"


def test_mobility_auto_target_uses_the_route_segment(monkeypatch):
    cells = _route_cells()
    monkeypatch.setattr(S, "route_segment", lambda routes, rng, *a, **k: (cells[:15], {"route": "L1"}))
    monkeypatch.setattr(S, "geo_shape", lambda seg: "CORRIDOR")
    t = faults._auto_target("mobility", cells, random.Random(0), routes=["L1"])
    assert t.route_seg == {"route": "L1"} and t.cell_keys == {c.cell_key for c in cells[:15]}
    assert t.cell_pairs == [(a.cell_key, b.cell_key) for a, b in zip(cells[:14], cells[1:15])]


def test_mobility_auto_target_falls_back_without_routes(capsys):
    t = faults._auto_target("mobility", _route_cells(), random.Random(0), routes=None)
    assert t is not None and t.route_seg is None and t.cell_pairs
    assert "mobility 노선 구간을 못 골랐다" in capsys.readouterr().out


def test_route_target_rejects_a_segment_whose_shape_differs_from_expect_geo(monkeypatch, capsys):
    cells = _route_cells()
    monkeypatch.setattr(S, "route_segment", lambda routes, rng, *a, **k: (cells[:15], {"route": "L1"}))
    monkeypatch.setattr(S, "geo_shape", lambda seg: "SCATTER")
    assert faults._route_target(["L1"], random.Random(0), "mobility") is None
    out = capsys.readouterr().out
    assert "모양이 SCATTER" in out and "정답 CORRIDOR" in out


def test_route_target_skips_the_shape_check_for_kinds_without_expect_geo(monkeypatch):
    cells = _route_cells()
    monkeypatch.setattr(S, "route_segment", lambda routes, rng, *a, **k: (cells[:15], {"route": "L1"}))
    monkeypatch.setattr(S, "geo_shape", lambda seg: "SCATTER")
    monkeypatch.setitem(faults.KIND_SPECS, "no_geo_kind", {})
    t = faults._route_target(["L1"], random.Random(0), "no_geo_kind")
    assert t is not None and t.route_seg == {"route": "L1"}


# ---------------------------------------------------------------- 27번 — 모양 맞는 지역은 40곳을 넘어도 전부 본다

def _groups(n_groups, per=5):
    """eNB `per`개씩 묶인 지역 `n_groups` 곳 — 지역끼리 50km 이상 떨어져 있어 `area_cells` 가 자기 지역만 고른다."""
    cells = []
    for g in range(n_groups):
        for k in range(per):
            cells.append(_cell(f"{g:02d}{k}-0", LAT0 + g * 0.5, LON0 + k * 0.1 / KM_LON, enb=f"{g:02d}{k}"))
    return cells


def _patch_rank(monkeypatch, lam_by_group):
    """정지 eNB 를 지역당 `lam_by_group[g]` 개(호율 값들)로 둔다 — rate 사전에 `dead<g>_<j>` 로 실린다."""
    monkeypatch.setattr(S, "geo_shape", lambda picked: "CLUSTER")
    monkeypatch.setattr(S, "disaster_rank_of", lambda keys, picked, mode, sid: {
        f"dead{picked[0].enb_id[:2]}_{j}": 0 for j in range(len(lam_by_group.get(int(picked[0].enb_id[:2]), [1.0])))})
    monkeypatch.setattr(S, "disaster_dead_n", lambda n: n)
    return {f"dead{g:02d}_{j}": v for g, vals in lam_by_group.items() for j, v in enumerate(vals)}


def _group_keys(cells, g):
    return {c.cell_key for c in cells if c.enb_id.startswith(f"{g:02d}")}


def test_busy_disaster_area_looks_at_every_matching_area_not_only_the_first_40(monkeypatch):
    cells = _groups(60)
    rate = {c.enb_id: 1000.0 - int(c.enb_id[:2]) for c in cells}           # 앵커 순서 = 지역 순서
    rate.update(_patch_rank(monkeypatch, {50: [9.0]}))                     # 앵커 250번째 안팎(상위 40곳 밖)
    keys = faults._busy_disaster_area(cells, rate, 5, "local", "F-1")
    assert keys == _group_keys(cells, 50), "앵커 상위 40곳 밖이어도 정지 eNB 호율이 가장 높은 지역"


def test_busy_disaster_area_maximises_the_minimum_dead_rate(monkeypatch):
    cells = _groups(12)
    rate = {c.enb_id: 100.0 - int(c.enb_id[:2]) for c in cells}
    # 지역 2: 최솟값 5(합 55) · 지역 7: 최솟값 6(합 12) — 최솟값이 큰 쪽을 고른다
    rate.update(_patch_rank(monkeypatch, {2: [5.0, 50.0], 7: [6.0, 6.0]}))
    assert faults._busy_disaster_area(cells, rate, 5, "local", "F-1") == _group_keys(cells, 7)
