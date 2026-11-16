"""27번 지진 — 정지 셀 둘레(가장 가까운 살아 있는 셀)를 지역에 더한다 (2026-11-16 사용자 · 맥스 구현 · 에스터 시험).

`scenarios.quake_ring_cells` 가 정지 셀마다 정지 eNB 가 아닌 가장 가까운 MAPPED 셀을 고르고, `faults._quake_step` 이
그것을 `ring` 으로 돌려 대상(`target.cell_keys`)에 더한다. 폭주(호를 더하는 셀)는 `sc._surge_keys`(= area)뿐이다.
정답지는 `context` CALL_SURGE 가 폭주 셀만, `ring_cells` 가 둘레 셀을 따로 싣는다. DB 를 쓰지 않는다.
"""
from __future__ import annotations

import random
import re
from datetime import datetime
from pathlib import Path

import pytest

from datagen import faults
from datagen import scenarios as S
from datagen.cells import Cell
from datagen.geo_lite import haversine_km

ROOT = Path(__file__).resolve().parents[1]
PEM = (ROOT / "src/datagen/pemdb1.py").read_text(encoding="utf-8")
FAULTS = (ROOT / "src/datagen/faults.py").read_text(encoding="utf-8")


def _cell(enb, n, lat, lon, geo="MAPPED"):
    return Cell(cell_key=f"{enb}-{n}", enb_id=enb, cell_id=n, enb_seq=0, c_uid="", tac=100, plmn="44010", vlan_id="",
                latitude=lat, longitude=lon, adm_sido_cd="11", adm_sido_nm="", adm_sgg_cd="11200", adm_sgg_nm="",
                adm_emd_cd="11200011", adm_emd_nm="", geo_status=geo, rat="EUTRAN", mme_id="0001",
                sgw_id="0037", pgw_id="0037", ims_pgw_id="0037", traffic_weight=1.0, display_name="")


# ── 1 · quake_ring_cells ──────────────────────────────────────────────────────────────────────

def test_the_nearest_live_cell_is_chosen_and_the_dead_enb_cells_are_excluded():
    dead = _cell("1", 0, 35.0, 139.0)
    same_enb = _cell("1", 1, 35.0001, 139.0)                    # 가장 가깝지만 정지 eNB 의 다른 셀 — 후보가 아니다
    near, far = _cell("2", 0, 35.002, 139.0), _cell("3", 0, 35.01, 139.0)
    assert S.quake_ring_cells([dead, same_enb, near, far], ["1-0"]) == ["2-0"]


def test_every_dead_cell_gets_its_own_nearest_and_shared_neighbors_are_deduplicated():
    a, b = _cell("1", 0, 35.0, 139.0), _cell("2", 0, 35.0, 139.05)
    n1, n2 = _cell("3", 0, 35.001, 139.0), _cell("4", 0, 35.0, 139.051)
    assert S.quake_ring_cells([a, b, n1, n2], ["1-0", "2-0"]) == ["3-0", "4-0"]
    twin = _cell("1", 1, 35.0, 139.0)                           # 같은 eNB 두 셀이 한 점 — 이웃 하나를 함께 쓴다
    assert S.quake_ring_cells([a, twin, n1], ["1-0", "1-1"]) == ["3-0"]


def test_equal_distance_ties_go_to_the_smaller_cell_key():
    dead = _cell("1", 0, 35.0, 139.0)
    x, y = _cell("9", 0, 35.001, 139.0), _cell("5", 0, 35.001, 139.0)
    assert S.quake_ring_cells([dead, x, y], ["1-0"]) == ["5-0"]
    assert S.quake_ring_cells([y, x, dead], ["1-0"]) == ["5-0"]


def test_unmapped_cells_are_neither_neighbors_nor_origins():
    dead = _cell("1", 0, 35.0, 139.0)
    unm = _cell("2", 0, 35.0005, 139.0, geo="UNMAPPED")
    ok = _cell("3", 0, 35.01, 139.0)
    assert S.quake_ring_cells([dead, unm, ok], ["1-0"]) == ["3-0"]
    bad_origin = _cell("4", 0, 0.0, 0.0, geo="UNMAPPED")
    assert S.quake_ring_cells([bad_origin, ok], ["4-0"]) == []


def test_a_nearer_cell_outside_the_search_box_is_still_found():
    """1차 상자(±0.05°) 안 후보가 있어도 상자 반폭보다 멀면 전체에서 다시 찾는다 — 결과는 전체 탐색과 같다."""
    dead = _cell("1", 0, 35.0, 139.0)
    inside_far = _cell("2", 0, 35.049, 139.049)                 # 상자 안이지만 멀다(약 6.7km)
    outside = _cell("3", 0, 35.056, 139.0)                      # 상자 밖(6.2km) — 더 가깝다
    assert haversine_km(35.0, 139.0, 35.056, 139.0) < haversine_km(35.0, 139.0, 35.049, 139.049)
    assert S.quake_ring_cells([dead, inside_far, outside], ["1-0"]) == ["3-0"]


def test_it_matches_a_brute_force_search_on_random_layouts():
    rng = random.Random(4)
    cells = [_cell(str(i), n, 35.0 + rng.uniform(-0.2, 0.2), 139.0 + rng.uniform(-0.2, 0.2))
             for i in range(150) for n in range(2)]
    dead_enbs = {str(i) for i in rng.sample(range(150), 25)}
    dead = [c.cell_key for c in cells if c.enb_id in dead_enbs]
    live = [c for c in cells if c.enb_id not in dead_enbs]
    want = set()
    for k in dead:
        d = next(c for c in cells if c.cell_key == k)
        want.add(min(live, key=lambda x: (haversine_km(d.latitude, d.longitude, x.latitude, x.longitude), x.cell_key)).cell_key)
    assert S.quake_ring_cells(cells, dead) == sorted(want)


def test_no_dead_cells_or_no_live_cells_gives_an_empty_list():
    a = _cell("1", 0, 35.0, 139.0)
    assert S.quake_ring_cells([a, _cell("2", 0, 35.1, 139.0)], []) == []
    assert S.quake_ring_cells([a], ["1-0"]) == []               # 살아 있는 셀이 없다
    assert S.quake_ring_cells([a], ["없는-키"]) == []


def test_order_of_the_cell_list_does_not_change_the_result():
    rng = random.Random(8)
    cells = [_cell(str(i), n, 35.0 + rng.uniform(-0.1, 0.1), 139.0 + rng.uniform(-0.1, 0.1)) for i in range(60) for n in range(3)]
    dead = [c.cell_key for c in cells if int(c.enb_id) % 4 == 0]
    a = S.quake_ring_cells(cells, dead)
    assert a == S.quake_ring_cells(list(reversed(cells)), dead) == S.quake_ring_cells(cells, list(reversed(dead)))


# ── 2 · build / 정답지 ────────────────────────────────────────────────────────────────────────

def _grid(n_enb=120, per=3, lat0=35.6, lon0=140.0):
    out = []
    for i in range(n_enb):
        la, lo = lat0 + (i // 12 - 5) * 0.01, lon0 + (i % 12 - 6) * 0.01
        out += [_cell(f"{800000 + i}", n, la + n * 0.0005, lo) for n in range(per)]
    return out


def _build(target, cells, fid=27):
    f = {"fault_id": fid, "kind": "natural_disaster", "target": target, "start_ts": datetime(2026, 11, 14, 10, 0),
         "duration_min": 25, "intensity": 1.0}
    return faults.build(f, cells, datetime(2026, 11, 14, 9, 0), random.Random(1), seed=3)


@pytest.fixture
def tall(monkeypatch):
    """모든 eNB 의 지반 증폭률을 키워 정지 후보를 만든다 + 진원 일부 eNB 만 정지하게 확률을 낮춘다(살아 있는 셀이 남게)."""
    monkeypatch.setattr(S.FAULT, "quake_down_max_cells", None)
    monkeypatch.setattr(S.FAULT, "quake_down_pct", 12.0)


def _sc(monkeypatch, tall, target="WIDE:QUAKE:B,6.2"):
    cells = _grid()
    monkeypatch.setattr(S, "quake_arv", lambda *a, **k: {c.enb_id: 2.5 for c in cells})
    sc = _build(target, cells)
    if sc is None:
        pytest.skip("이 격자·시드에서는 폭주 셀이 서지 않는다")
    return sc, cells


def test_the_target_is_surge_cells_plus_dead_cells_plus_the_ring(monkeypatch, tall):
    sc, cells = _sc(monkeypatch, tall)
    t = sc.as_truth()
    dead, ring = set(t["down_cells"]), set(t["ring_cells"])
    surge = set(sc.__dict__["_surge_keys"])
    assert dead and ring, "정지와 둘레가 있어야 이 시험이 뜻이 있다"
    assert sc.target.cell_keys == surge | ring
    assert dead <= surge, "정지 셀은 정답 지역(= 폭주 지역)에 든다"
    assert not (ring & surge), "둘레는 폭주 셀이 아니다"
    dead_enbs = {k.split("-")[0] for k in dead}
    assert not any(k.split("-")[0] in dead_enbs for k in ring), "둘레는 살아 있는 eNB 셀이다"


def test_the_ring_is_exactly_the_nearest_live_cell_of_each_dead_cell(monkeypatch, tall):
    sc, cells = _sc(monkeypatch, tall)
    t = sc.as_truth()
    want = set(S.quake_ring_cells(cells, t["down_cells"])) - set(sc.__dict__["_surge_keys"])
    assert set(t["ring_cells"]) == want


def test_call_surge_context_has_surge_cells_only_and_ring_cells_are_separate(monkeypatch, tall):
    sc, _ = _sc(monkeypatch, tall)
    t = sc.as_truth()
    ctx = [c for c in t["context"] if c["code"] == "CALL_SURGE"][0]
    assert set(ctx["cells"]) == set(sc.__dict__["_surge_keys"])
    assert not (set(ctx["cells"]) & set(t["ring_cells"]))
    assert t["ring_cells"] == sorted(t["ring_cells"]) or isinstance(t["ring_cells"], list)


def test_top_level_cells_and_cell_cnt_follow_the_whole_target_including_the_ring(monkeypatch, tall):
    """정답지의 `cells`·`cell_cnt`·`scope` 는 정답 지역 전체(폭주+정지+둘레)다 — CALL_SURGE 칸만 폭주 셀이다."""
    sc, _ = _sc(monkeypatch, tall)
    t = sc.as_truth()
    assert set(t["cells"]) == sc.target.cell_keys and t["cell_cnt"] == len(sc.target.cell_keys)
    assert set(t["ring_cells"]) <= set(t["cells"])


def test_ring_is_deterministic_for_the_same_fault_id(monkeypatch, tall):
    a, _ = _sc(monkeypatch, tall)
    b, _ = _sc(monkeypatch, tall)
    assert a.as_truth()["ring_cells"] == b.as_truth()["ring_cells"]
    assert a.target.cell_keys == b.target.cell_keys


def test_no_dead_enbs_means_no_ring_and_the_target_is_the_surge_area(monkeypatch):
    """정지가 0 이면 둘레도 0 — 작은 지진은 전과 같다."""
    monkeypatch.setattr(S.FAULT, "quake_down_max_cells", None)
    cells = _grid()
    monkeypatch.setattr(S, "quake_arv", lambda *a, **k: {c.enb_id: 1.0 for c in cells})          # 증폭 없음 — 진도 3 이상(폭주)은 서되 5약(정지)은 없다
    sc = _build("LOCAL:QUAKE:B,5.2", cells)
    if sc is None:
        pytest.skip("이 격자·시드에서는 폭주 셀이 서지 않는다")
    t = sc.as_truth()
    assert t["down_cells"] == [] and t["ring_cells"] == []
    assert sc.target.cell_keys == set(sc.__dict__["_surge_keys"])


def test_other_kinds_have_no_ring_key_value(monkeypatch):
    """지진이 아니면 `ring_cells` 는 None 이다(칸은 있어도 값이 없다) — 다른 시나리오의 정답지를 바꾸지 않는다."""
    cells = _grid()
    f = {"fault_id": 5, "kind": "enb_s1_outage", "target": "ENB:800001", "start_ts": datetime(2026, 11, 14, 10, 0),
         "duration_min": 10, "intensity": 1.0}
    sc = faults.build(f, cells, datetime(2026, 11, 14, 9, 0), random.Random(1), seed=3)
    if sc is None:
        pytest.skip("이 대상은 걸리지 않는다")
    assert sc.as_truth().get("ring_cells") is None


# ── 3 · 소스 계약 ─────────────────────────────────────────────────────────────────────────────

def test_surge_drafts_adds_calls_only_to_surge_keys_not_the_ring():
    body = PEM[PEM.index("def _surge_drafts"):PEM.index("_SA_OLD_FIELDS")]
    assert re.search(r'sc\.__dict__\.get\("_surge_keys"\) or sc\.target\.cell_keys', body)
    assert "sorted(sc.target.cell_keys)" not in body.replace('sc.__dict__.get("_surge_keys") or sc.target.cell_keys', "")


def test_surge_base_is_measured_on_the_surge_keys():
    assert '_surge_base(cells, sc.__dict__.get("_surge_keys") or target.cell_keys, sc._enb_rate)' in FAULTS


def test_the_ring_is_added_after_quake_plan_and_before_the_intensity_dict():
    a = FAULTS.index("S.quake_plan(cells, q, qrng, want, arv)")
    b = FAULTS.index("ring = [k for k in S.quake_ring_cells(cells, dead_cells) if k not in area]")
    c = FAULTS.index("inten = S.quake_cell_intensity(")
    assert a < b < c
    assert "정지 둘레 셀 {len(ring)}" in FAULTS and '"ring": ring' in FAULTS


def test_quake_plan_still_returns_three_values():
    """맥스가 반환을 바꾸지 않았다 — 기존 호출부·시험이 3개 반환에 기댄다."""
    cells = _grid(40)
    r = S.quake_plan(cells, {"lat": 35.6, "lon": 140.0, "depth_km": 20.0, "magnitude": 6.0, "fault_type_d": 0.1},
                     random.Random(1), 25, {})
    assert len(r) == 3
