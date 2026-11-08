"""창고를 보고 후보를 좁히는 자동 대상의 순환 (2026-11-08) — 14·17번(메시) · 16번 · eNB 정지 · 23번.

13번 F-197 이 2·7번과 같은 시군구를 다시 고른 것을 보고 훑었다. 네 고르기(`_congestion_target`·`_anchor_target`·
`_outage_target`·`_venue_target`)는 후보 안에서 rng 로만 골라 연달아 같은 곳이 나올 수 있었다. 이제 `used` 를 받아
`_rotating` 으로 돈다. 순환 키는 `build` 가 기록하는 대상 셀 지문이다. DB 없이 — 가짜 메시와 호율.
"""
from __future__ import annotations

import datetime as dt
import random

import pytest

from datagen import faults
from datagen.cells import Cell


def _cell(enb: str, n: int, lat: float, lon: float, sgg: str = "11200") -> Cell:
    return Cell(
        cell_key=f"{enb}-{n}", enb_id=enb, cell_id=n, enb_seq=0, c_uid="",
        tac=100, plmn="44010", vlan_id="", latitude=lat, longitude=lon,
        adm_sido_cd="11", adm_sido_nm="", adm_sgg_cd=sgg, adm_sgg_nm="",
        adm_emd_cd=sgg + "011", adm_emd_nm="", geo_status="MAPPED",
        rat="EUTRAN", mme_id="0001", sgw_id="0037", pgw_id="0037",
        ims_pgw_id="0037", traffic_weight=1.0, display_name="")


#: 메시 번호 → (격자 위치) — 두 무리(0~3 · 4~7)를 멀리 떼어 놓아 `around`(가장 가까운 4칸)가 무리를 가른다
def _world(n_mesh: int = 8):
    """메시 `n_mesh` 칸 × eNB 3개 × 셀 3개(= 9셀) — 앵커 후보 조건(8셀·40명)을 넘는다."""
    cells, meshes = [], {}
    for m in range(n_mesh):
        group, idx = divmod(m, 4)
        lat0 = 35.0 + group * 5.0 + idx * 0.01
        keys = []
        for e in range(3):
            enb = f"{m}{e}"
            for n in range(3):
                c = _cell(enb, n, lat0 + e * 0.001, 139.0 + n * 0.0005)
                cells.append(c)
                keys.append(c.cell_key)
        meshes[f"M{m}"] = (100.0, keys)
    return cells, meshes


FAULT = {"fault_id": 1, "start_ts": dt.datetime(2026, 11, 8, 3, 0)}


def _load(meshes):
    return lambda ts: meshes


def _fp(target) -> str:
    return faults.target_key(target.cell_keys)


# ---------------------------------------------------------------- _rotate_group

def test_rotate_group_warehouse_kinds():
    assert faults._rotate_group("ran_congestion") == faults._rotate_group("sa_fallback") == "mesh"
    assert faults._rotate_group("anchor_saturation") == "anchor"
    assert faults._rotate_group("enb_s1_outage") == "outage"
    assert faults._rotate_group("event_crowd") == "venue"
    assert faults._rotate_group("natural_disaster") == "disaster"
    assert faults._rotate_group("nsa_nr_leg") == "sgg"


def test_rotate_group_unrotated_kinds_stay_none():
    for k in ("dns_flap", "capacity_shift", "earthquake"):
        assert faults._rotate_group(k) is None, k


def test_warehouse_group_does_not_shadow_other_groups():
    # 시군구·노선·노드 묶음이 먼저다 — 표는 그 뒤의 것만 다룬다
    for k in faults._WAREHOUSE_GROUP:
        assert k not in faults._AUTO_NODE and k not in faults._AUTO_SGG and k not in faults._ROUTE_KINDS, k


# ---------------------------------------------------------------- _outage_target

def _outage_world():
    cells = []
    for e in range(5):
        cells += [_cell(f"E{e}", n, 35.0 + e * 0.01, 139.0 + n * 0.0005) for n in range(3)]
    rate = {f"E{e}": 5.0 for e in range(5)}
    return cells, rate


def test_outage_without_history_matches_previous_choice():
    cells, rate = _outage_world()
    a = faults._outage_target(cells, random.Random(3), rate)
    b = faults._outage_target(cells, random.Random(3), rate, used=None)
    assert a.enb_id == b.enb_id


def test_outage_defers_used_enb():
    cells, rate = _outage_world()
    first = faults._outage_target(cells, random.Random(3), rate)
    used = {_fp(first): 10}
    for s in range(20):
        t = faults._outage_target(cells, random.Random(s), rate, used)
        assert t.enb_id != first.enb_id


def test_outage_all_used_takes_the_oldest():
    cells, rate = _outage_world()
    used = {faults.target_key(c.cell_key for c in cells if c.enb_id == f"E{e}"): 10 + e for e in range(5)}
    assert faults._outage_target(cells, random.Random(1), rate, used).enb_id == "E0"


def test_outage_skips_low_rate_and_small_enbs():
    cells, rate = _outage_world()
    rate["E0"] = 0.1
    cells = [c for c in cells if c.enb_id != "E1"] + [_cell("E1", 0, 35.5, 139.0)]
    for s in range(15):
        assert faults._outage_target(cells, random.Random(s), rate).enb_id in {"E2", "E3", "E4"}


def test_outage_without_rate_is_none():
    cells, _ = _outage_world()
    assert faults._outage_target(cells, random.Random(1), None) is None


def test_outage_key_equals_what_build_records():
    cells, rate = _outage_world()
    t = faults._outage_target(cells, random.Random(1), rate)
    assert _fp(t) == faults.target_key(c.cell_key for c in cells if c.enb_id == t.enb_id)


# ---------------------------------------------------------------- _venue_target

def _venue_world():
    cells, meshes = _world(4)       # eNB 당 셀 3개 — crowd_enb_cells (2, 3) 안
    return cells, meshes


def test_venue_defers_used_enb_and_takes_oldest_when_all_used():
    cells, meshes = _venue_world()
    first = faults._venue_target(cells, random.Random(2), FAULT, _load(meshes))
    t2 = faults._venue_target(cells, random.Random(2), FAULT, _load(meshes), {_fp(first): 9})
    assert t2.enb_id != first.enb_id
    every = {faults.target_key(c.cell_key for c in cells if c.enb_id == e): 100 + i
             for i, e in enumerate(sorted({c.enb_id for c in cells}))}
    oldest = sorted(every, key=every.get)[0]
    got = faults._venue_target(cells, random.Random(2), FAULT, _load(meshes), every)
    assert _fp(got) == oldest


def test_venue_without_busy_mesh_is_none():
    cells, meshes = _venue_world()
    quiet = {m: (1.0, k) for m, (_p, k) in meshes.items()}
    assert faults._venue_target(cells, random.Random(2), FAULT, _load(quiet)) is None


def test_venue_without_loader_is_none():
    cells, _ = _venue_world()
    assert faults._venue_target(cells, random.Random(2), FAULT, None) is None


# ---------------------------------------------------------------- _congestion_target

def test_congestion_rotates_between_the_two_groups():
    cells, meshes = _world(8)
    t1 = faults._congestion_target(cells, random.Random(1), FAULT, _load(meshes))
    t2 = faults._congestion_target(cells, random.Random(1), FAULT, _load(meshes), {_fp(t1): 5})
    assert _fp(t2) != _fp(t1) and not (t1.cell_keys & t2.cell_keys)
    t3 = faults._congestion_target(cells, random.Random(1), FAULT, _load(meshes), {_fp(t1): 5, _fp(t2): 7})
    assert _fp(t3) == _fp(t1), "다 썼으면 가장 오래전 것"


def test_congestion_key_is_fingerprint_of_returned_cells():
    cells, meshes = _world(8)
    t = faults._congestion_target(cells, random.Random(4), FAULT, _load(meshes))
    assert len(t.cell_keys) == 4 * 9


def test_congestion_without_candidates_is_none():
    cells, meshes = _world(8)
    quiet = {m: (1.0, k) for m, (_p, k) in meshes.items()}
    assert faults._congestion_target(cells, random.Random(1), FAULT, _load(quiet)) is None
    assert faults._congestion_target(cells, random.Random(1), FAULT, None) is None


# ---------------------------------------------------------------- _anchor_target

def test_anchor_same_rng_same_cluster():
    cells, meshes = _world(6)
    a = faults._anchor_target(cells, random.Random(5), FAULT, _load(meshes))
    b = faults._anchor_target(cells, random.Random(5), FAULT, _load(meshes))
    assert a.cell_keys == b.cell_keys and a.sgg_codes == b.sgg_codes == {"11200"}


def test_anchor_defers_used_mesh_cluster():
    cells, meshes = _world(6)
    first = faults._anchor_target(cells, random.Random(5), FAULT, _load(meshes))
    for s in range(15):
        t = faults._anchor_target(cells, random.Random(s), FAULT, _load(meshes), {_fp(first): 9})
        assert _fp(t) != _fp(first)


def test_anchor_walks_all_meshes_before_repeating():
    cells, meshes = _world(6)
    used, seen = {}, []
    for i in range(6):
        t = faults._anchor_target(cells, random.Random(i), FAULT, _load(meshes), used)
        seen.append(_fp(t))
        used[_fp(t)] = i
    assert len(set(seen)) == 6


def test_anchor_without_busy_mesh_is_none():
    cells, meshes = _world(6)
    quiet = {m: (1.0, k) for m, (_p, k) in meshes.items()}
    assert faults._anchor_target(cells, random.Random(1), FAULT, _load(quiet)) is None


# ---------------------------------------------------------------- build — 기록

def _row(fid, kind):
    return {"fault_id": fid, "kind": kind, "target": None,
            "start_ts": dt.datetime(2026, 11, 8, 3, 0), "duration_min": 11, "intensity": 1.0}


@pytest.mark.parametrize("kind,group", [("enb_s1_outage", "outage"), ("event_crowd", "venue"),
                                        ("ran_congestion", "mesh"), ("anchor_saturation", "anchor")])
def test_build_records_the_group_for_auto_target(tmp_path, kind, group):
    cells, meshes = _world(8)
    rate = {c.enb_id: 5.0 for c in cells}
    h = faults.TargetHistory(tmp_path / "t.json")
    sc = faults.build(_row(7, kind), cells, dt.datetime(2026, 11, 8, 0, 0), faults.target_rng(7, 7), seed=7,
                      enb_rate=lambda ts: rate, mesh_load=_load(meshes), nr_on=lambda e: True, history=h)
    assert sc is not None
    assert h.used(group, 99) == {faults.target_key(sc.target.cell_keys): 7}


def test_build_with_explicit_target_does_not_record(tmp_path):
    cells, meshes = _world(8)
    rate = {c.enb_id: 5.0 for c in cells}
    h = faults.TargetHistory(tmp_path / "t.json")
    row = _row(8, "enb_s1_outage")
    row["target"] = f"ENB:{cells[0].enb_id}"
    faults.build(row, cells, dt.datetime(2026, 11, 8, 0, 0), faults.target_rng(7, 8), seed=7,
                 enb_rate=lambda ts: rate, mesh_load=_load(meshes), history=h)
    assert h.used("outage", 99) == {}


def test_build_outage_never_repeats_across_consecutive_faults(tmp_path):
    cells, meshes = _world(8)
    rate = {c.enb_id: 5.0 for c in cells}
    h = faults.TargetHistory(tmp_path / "t.json")
    seen = []
    for fid in range(1, 13):
        sc = faults.build(_row(fid, "enb_s1_outage"), cells, dt.datetime(2026, 11, 8, 0, 0),
                          faults.target_rng(7, fid), seed=7, enb_rate=lambda ts: rate, history=h)
        seen.append(sc.target.enb_id)
    assert all(a != b for a, b in zip(seen, seen[1:]))
    assert len(set(seen[:24])) == len(seen)       # 후보 24개 > 12회 — 한 바퀴 안에서는 겹치지 않는다
