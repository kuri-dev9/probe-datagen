"""노드 자동 대상 추첨 · 23번 eNB 하나 · eNB 정지 호율 조건 · 옮겨 온 하한 호 (2026-11-07 사용자 결정).

* `_pick_node` — 가장 큰 노드 대비 셀 수 몫이 하한 이상인 노드 중에서 주입마다 돌려 고른다. 같은 rng(= 같은 fault)는 같은 노드다.
* `_WIDE_AXIS_KINDS` — 전국 축으로 판정하는 kind 는 더 좁은 하한(`auto_wide_min_share`)을 쓴다.
* `_outage_target` — 셀 수와 평소 호율(λ) 하한, 호율을 못 읽으면 None.
* `_venue_target` — 평소 사람이 있는 메시의 eNB 하나(셀 `crowd_enb_cells`), 후보 없으면 None.
* `_moved_floor_calls` — 평소 호에서 옮겨 오는 하한: 전국 호의 몫을 넘지 않는다. 꼬리를 남기지 않는다.
DB 없이 돈다.
"""
from __future__ import annotations

import dataclasses
import inspect
import random
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import yaml

from datagen import faults
from datagen import pemdb1 as P
from datagen import scenarios as S
from datagen.cells import Cell

ROOT = Path(__file__).resolve().parents[1]
T = datetime(2026, 11, 7, 10, 0)


def _cell(enb, n, sgw="0001", mme="0001", lat=35.6):
    return Cell(
        cell_key=f"{enb}-{n}", enb_id=str(enb), cell_id=n, enb_seq=0, c_uid="",
        tac=100, plmn="44010", vlan_id="", latitude=lat, longitude=139.70,
        adm_sido_cd="11", adm_sido_nm="", adm_sgg_cd="11200", adm_sgg_nm="",
        adm_emd_cd="11200011", adm_emd_nm="", geo_status="MAPPED",
        rat="EUTRAN", mme_id=mme, sgw_id=sgw, pgw_id=sgw,
        ims_pgw_id=sgw, traffic_weight=1.0, display_name="")


def _sgw_cells(counts: dict[str, int]):
    out, e = [], 100
    for sgw, n in counts.items():
        out += [_cell(e + i, 0, sgw=sgw) for i in range(n)]
        e += n
    return out


def _picked(cells, field, share, seeds=80):
    return {faults._pick_node(cells, field, random.Random(s), share) for s in range(seeds)}


# ---------------------------------------------------------------- _pick_node

def test_pick_node_chooses_only_nodes_at_or_above_the_share_of_the_biggest():
    cells = _sgw_cells({"A": 100, "B": 50, "C": 49, "D": 10})
    assert _picked(cells, "sgw_id", 0.5) == {"A", "B"}, "C(49%)·D 는 후보가 아니다 — B(50%)는 경계라 후보다"


def test_pick_node_rotates_and_is_not_the_biggest_node():
    cells = _sgw_cells({"A": 100, "B": 80, "C": 60})
    assert _picked(cells, "sgw_id", 0.5) == {"A", "B", "C"}


def test_pick_node_is_stable_for_the_same_rng():
    cells = _sgw_cells({"A": 100, "B": 80, "C": 60})
    assert len({faults._pick_node(cells, "sgw_id", random.Random(7), 0.5) for _ in range(10)}) == 1


def test_pick_node_with_a_single_node_returns_it():
    assert faults._pick_node(_sgw_cells({"A": 5}), "sgw_id", random.Random(1), 0.5) == "A"


def test_pick_node_works_on_mme_and_enb_fields_too():
    cells = [_cell(1, n, mme="M1") for n in range(4)] + [_cell(2, n, mme="M2") for n in range(2)]
    assert _picked(cells, "mme_id", 0.5) == {"M1", "M2"}
    assert _picked(cells, "enb_id", 0.75) == {"1"}, "셀 4개 대 2개 — 0.5 는 하한 아래다"


def test_busiest_is_gone():
    assert not hasattr(faults, "_busiest")


# ---------------------------------------------------------------- _WIDE_AXIS_KINDS · _auto_target

def test_the_wide_axis_kinds_are_auto_node_kinds_and_include_the_national_ones():
    assert set(faults._WIDE_AXIS_KINDS) <= set(faults._AUTO_NODE)
    assert {"dns_degradation", "ue_model", "cdn_degradation"} <= set(faults._WIDE_AXIS_KINDS)


def test_a_wide_axis_kind_uses_the_narrower_share():
    cells = _sgw_cells({"A": 100, "B": 60, "C": 80})
    got = {faults._auto_target("dns_degradation", cells, random.Random(s)).sgw_id for s in range(80)}
    assert got == {"A", "C"}, "B(60%)는 전국 축 하한 0.75 아래다"


def test_a_node_axis_kind_uses_the_wider_share():
    kind = next(k for k in faults._AUTO_NODE if k not in faults._WIDE_AXIS_KINDS
                and faults._AUTO_NODE[k] == "sgw_id")
    cells = _sgw_cells({"A": 100, "B": 60, "C": 30})
    got = {faults._auto_target(kind, cells, random.Random(s)).sgw_id for s in range(80)}
    assert got == {"A", "B"}


def test_the_auto_target_covers_every_cell_of_the_picked_node():
    cells = _sgw_cells({"A": 6, "B": 5})
    t = faults._auto_target("gw_resource", cells, random.Random(3))
    assert t.cell_keys == {c.cell_key for c in cells if c.sgw_id == t.sgw_id}


def test_the_mme_group_kinds_keep_their_fixed_sets():
    assert faults._AUTO_MME_GROUP == {"auc_no_response": (0, 3), "dra_route_failure": (3, 5),
                                      "s6a_undetermined": (8, 3)}


def test_the_enb_outage_pick_uses_the_node_share_and_follows_the_picked_enb():
    cells = [_cell(1, n) for n in range(6)] + [_cell(2, n) for n in range(3)] + [_cell(3, 0)]
    got = {faults._auto_target("enb_s1_outage", cells, random.Random(s)).enb_id for s in range(80)}
    assert got == {"1", "2"}


# ---------------------------------------------------------------- _outage_target

def _enbs(spec):
    """{enb: 셀 수}"""
    return [_cell(e, n) for e, k in spec.items() for n in range(k)]


def test_the_outage_target_needs_cells_and_a_busy_enb():
    cells = _enbs({1: 3, 2: 2, 3: 3, 4: 4})
    rate = {"1": 2.0, "2": 9.0, "3": 1.4, "4": 1.5}
    got = {faults._outage_target(cells, random.Random(s), rate).enb_id for s in range(60)}
    assert got == {"1", "4"}, "2 는 셀이 모자라고 3 은 호율이 모자란다(경계 1.5 는 후보)"


def test_the_outage_target_takes_the_whole_enb():
    cells = _enbs({1: 3, 2: 4})
    t = faults._outage_target(cells, random.Random(1), {"1": 5.0, "2": 5.0})
    assert t.cell_keys == {c.cell_key for c in cells if c.enb_id == t.enb_id}


def test_the_outage_target_falls_back_without_a_rate_or_a_candidate():
    cells = _enbs({1: 3})
    assert faults._outage_target(cells, random.Random(1), None) is None
    assert faults._outage_target(cells, random.Random(1), {}) is None
    assert faults._outage_target(cells, random.Random(1), {"1": 0.2}) is None


def test_the_outage_target_rotates_between_injections():
    cells = _enbs({1: 3, 2: 3, 3: 3})
    rate = {"1": 5.0, "2": 5.0, "3": 5.0}
    assert {faults._outage_target(cells, random.Random(s), rate).enb_id for s in range(60)} == {"1", "2", "3"}


# ---------------------------------------------------------------- _venue_target

A = [_cell(1000 + e, n, lat=35.60 + e * 0.001) for e in range(4) for n in range(3)]        # 셀 3개 eNB 4대
B = [_cell(2000, n, lat=35.7) for n in range(6)]                                           # 셀 6개 eNB
C = [_cell(3000, 0, lat=35.8)]                                                              # 셀 1개 eNB
ALL = A + B + C


def _keys(cs):
    return {c.cell_key for c in cs}


def _load(people_a=60, people_b=60, people_c=60):
    return lambda ts: {"M-A": (people_a, _keys(A)), "M-B": (people_b, _keys(B)), "M-C": (people_c, _keys(C))}


def _venue(seed=1, load=None):
    return faults._venue_target(ALL, random.Random(seed), {"start_ts": T}, load or _load())


def test_the_venue_target_is_one_enb_with_two_or_three_cells():
    for s in range(40):
        t = _venue(s)
        assert t is not None and t.enb_id and len({k.split("-")[0] for k in t.cell_keys}) == 1
        assert 2 <= len(t.cell_keys) <= 3
        assert t.enb_id in {str(1000 + e) for e in range(4)}, "셀 6개·1개 eNB 는 범위 밖이다"


def test_the_venue_target_rotates_between_injections():
    assert {_venue(s).enb_id for s in range(80)} == {str(1000 + e) for e in range(4)}


def test_the_venue_target_needs_people_in_the_mesh():
    assert _venue(1, _load(people_a=S.FAULT.anchor_min_people - 1)) is None


def test_the_venue_target_is_none_without_a_candidate_enb():
    assert faults._venue_target(B + C, random.Random(1), {"start_ts": T},
                                lambda ts: {"M-B": (60, _keys(B)), "M-C": (60, _keys(C))}) is None


def test_the_venue_cell_range_comes_from_the_config():
    f = S.FaultCfg.from_cfg({"crowd_enb_cells": [6, 6]})
    assert f.crowd_enb_cells == (6, 6)
    try:
        S.FaultCfg.from_cfg({"crowd_enb_cells": [3, 2]})
        raise AssertionError("뒤집힌 범위는 거부해야 한다")
    except RuntimeError:
        pass


def test_event_crowd_is_a_venue_kind_with_a_point_answer_and_a_short_staircase():
    spec = S.KIND_SPECS["event_crowd"]
    assert "event_crowd" in faults._VENUE_KINDS
    assert spec["expect_geo"] == "POINT" and spec["steps"] == ((3, 0.50), (23, 1.00), (25, 0.40))


# ---------------------------------------------------------------- _moved_floor_calls · 꼬리

def _gen(per_kind=120.0, share=0.2, moved=("event_crowd",), floor_max=60):
    g = SimpleNamespace(fault=S.FaultCfg(), _moved_scale=1.0)
    g.fault.floor_by_kind = {"event_crowd": per_kind}
    g.fault.floor_moved_share, g.fault.floor_moved_kinds = share, tuple(moved)
    g.fault.floor_max_cells = floor_max
    return g


def _act(kind="event_crowd", cells=3, level=1.0):
    return SimpleNamespace(kind=kind, target=SimpleNamespace(cell_keys={"x"}), peak=1.0,
                           active_cells_cache=set(range(cells)), intensity=lambda m: level)


def test_the_moved_calls_are_the_per_cell_floor_times_cells_when_the_nation_is_busy():
    g = _gen()
    assert P.PemdbGen._moved_floor_calls(g, 5, [_act(cells=3)], 3000) == 360
    assert g._moved_scale == 1.0


def test_the_moved_calls_are_capped_at_the_share_of_the_national_calls():
    g = _gen()
    assert P.PemdbGen._moved_floor_calls(g, 5, [_act(cells=3)], 900) == 180, "900 의 20%"
    assert abs(g._moved_scale - 0.5) < 1e-9


def test_the_moved_calls_follow_the_visible_level():
    g = _gen()
    assert P.PemdbGen._moved_floor_calls(g, 5, [_act(cells=2, level=0.5)], 3000) == 120


def test_only_listed_kinds_are_moved_and_wide_targets_are_skipped():
    g = _gen()
    assert P.PemdbGen._moved_floor_calls(g, 5, [_act("mobility")], 3000) == 0
    wide = _act()
    wide.target.cell_keys = set(range(g.fault.floor_max_cells + 1))
    assert P.PemdbGen._moved_floor_calls(g, 5, [wide], 3000) == 0
    g2 = _gen(moved=())
    assert P.PemdbGen._moved_floor_calls(g2, 5, [_act()], 3000) == 0


def test_without_a_want_the_scale_stays_one():
    g = _gen()
    P.PemdbGen._moved_floor_calls(g, 5, [], 3000)
    assert g._moved_scale == 1.0


def test_a_moved_kind_leaves_no_floor_tail():
    src = inspect.getsource(P.PemdbGen)
    i = src.index("if sc.kind in cfg.floor_moved_kinds:\n                    # 옮겨 온 호다")
    assert "self._floor_last.pop(k, None)" in src[i:i + 600], "꼬리를 남기지 않는다"


# ---------------------------------------------------------------- 저장소 yaml

def _repo_fault():
    return yaml.safe_load((ROOT / "config" / "datagen.yaml").read_text(encoding="utf-8"))["pemdb1"]["fault"]


def test_repo_yaml_carries_the_initial_values():
    f = S.FaultCfg.from_cfg(_repo_fault())
    assert f.floor_by_kind.get("event_crowd") == 120.0
    assert f.floor_moved_kinds == ("event_crowd",) and f.floor_moved_share == 0.2
    assert f.crowd_enb_cells == (2, 3)
    assert (f.auto_node_min_share, f.auto_wide_min_share) == (0.5, 0.75)
    assert (f.outage_min_cells, f.outage_min_rate) == (3, 1.5)


def test_an_unknown_moved_kind_stops_the_config():
    try:
        S.FaultCfg.from_cfg({"floor_moved_kinds": ["no_such_kind"]})
        raise AssertionError("모르는 kind 는 멈춰야 한다")
    except RuntimeError:
        pass
