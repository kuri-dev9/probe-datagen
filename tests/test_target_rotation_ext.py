"""자동 대상 순환 확장 (2026-11-08) — 기종·APN·노드 + `TargetHistory` 의 `회차:묶음` 키.

8번 F-195 와 26번 F-145 가 둘 다 기종 `100SS/A3VU`(순위 3위 고정)였다. 기종·APN·노드도 시군구·노선·재해 지역과 같은
`TargetHistory`·`_rotating` 으로 돈다. 기록은 고르기에만 쓴다. DB 없이 — 가짜 가입자와 임시 파일.
"""
from __future__ import annotations

import json
import random
from types import SimpleNamespace

from datagen import faults
from datagen.cells import Cell
from datagen.generator import Generator


# ---------------------------------------------------------------- TargetHistory — `회차:묶음` 키

def test_history_one_fault_can_record_two_groups(tmp_path):
    h = faults.TargetHistory(tmp_path / "t.json")
    h.record(194, "node:sgw_id", "kSGW")
    h.record(194, "ue_model", "kUE")
    data = json.loads((tmp_path / "t.json").read_text())
    assert set(data) == {"194:node:sgw_id", "194:ue_model"}
    assert h.used("ue_model", 999) == {"kUE": 194} and h.used("node:sgw_id", 999) == {"kSGW": 194}


def test_history_reads_the_old_key_shape_and_does_not_rewrite_the_same_group(tmp_path):
    p = tmp_path / "t.json"
    p.write_text(json.dumps({"194": {"group": "sgg", "key": "old"}}))
    h = faults.TargetHistory(p)
    assert h.used("sgg", 999) == {"old": 194}
    h.record(194, "sgg", "new")
    assert json.loads(p.read_text()) == {"194": {"group": "sgg", "key": "old"}}, "옛 키와 같은 묶음은 다시 쓰지 않는다"
    h.record(194, "apn", "x")
    assert "194:apn" in json.loads(p.read_text())


def test_history_before_filter_applies_to_the_fault_number_part_of_the_key(tmp_path):
    h = faults.TargetHistory(tmp_path / "t.json")
    h.record(10, "apn", "a")
    h.record(20, "apn", "b")
    assert h.used("apn", 20) == {"a": 10}


# ---------------------------------------------------------------- _rotate_group — 노드

def test_rotate_group_node_kinds_share_by_node_field():
    assert faults._rotate_group("dns_degradation") == faults._rotate_group("ims_pdn") == "node:sgw_id"
    assert faults._rotate_group("gw_resource") == faults._rotate_group("ue_model") == "node:sgw_id"
    assert faults._rotate_group("mme_overload") == faults._rotate_group("auth_failure") == "node:mme_id"
    assert faults._rotate_group("gw_pgw_restart") == "node:pgw_id"


def test_rotate_group_node_group_name_follows_auto_node_for_every_kind():
    for kind, field in faults._AUTO_NODE.items():
        assert faults._rotate_group(kind) == f"node:{field}", kind


# ---------------------------------------------------------------- _pick_node 의 used

def _cell(enb, sgw):
    return Cell(cell_key=f"{enb}-0", enb_id=str(enb), cell_id=0, enb_seq=0, c_uid="", tac=100, plmn="44010",
                vlan_id="", latitude=35.6, longitude=139.7, adm_sido_cd="13", adm_sido_nm="", adm_sgg_cd="13104",
                adm_sgg_nm="", adm_emd_cd="13104001", adm_emd_nm="", geo_status="MAPPED", rat="EUTRAN",
                mme_id="0001", sgw_id=sgw, pgw_id=sgw, ims_pgw_id=sgw, traffic_weight=1.0, display_name="")


def _sgws(n_each=10, names=("A", "B", "C")):
    cells, e = [], 100
    for s in names:
        for _ in range(n_each):
            cells.append(_cell(e, s))
            e += 1
    return cells


def _key(cells, sgw):
    return faults.target_key(c.cell_key for c in cells if c.sgw_id == sgw)


def test_pick_node_without_used_is_unchanged():
    cells = _sgws()
    assert {faults._pick_node(cells, "sgw_id", random.Random(s), 0.5) for s in range(40)} == {"A", "B", "C"}
    assert {faults._pick_node(cells, "sgw_id", random.Random(s), 0.5, {}) for s in range(40)} == {"A", "B", "C"}


def test_pick_node_pushes_a_used_node_back():
    cells = _sgws()
    used = {_key(cells, "A"): 3, _key(cells, "B"): 4}
    assert {faults._pick_node(cells, "sgw_id", random.Random(s), 0.5, used) for s in range(40)} == {"C"}


def test_pick_node_reuses_the_oldest_when_all_used():
    cells = _sgws()
    used = {_key(cells, "A"): 9, _key(cells, "B"): 2, _key(cells, "C"): 5}
    assert faults._pick_node(cells, "sgw_id", random.Random(0), 0.5, used) == "B"


def test_pick_node_key_equals_what_build_records(tmp_path):
    """`build` 가 기록하는 값(대상 셀 집합 지문)과 `_pick_node` 가 보는 키가 같아야 순환이 성립한다."""
    cells = _sgws()
    h = faults.TargetHistory(tmp_path / "t.json")
    row = {"fault_id": 1, "kind": "gw_resource", "target": None, "start_ts": __import__("datetime").datetime(2026, 11, 8, 1, 0),
           "duration_min": 11, "intensity": 1.0}
    anchor = __import__("datetime").datetime(2026, 11, 8, 0, 0)
    sc = faults.build(row, cells, anchor, faults.target_rng(7, 1), seed=7, history=h)
    node = sc.target.sgw_id
    assert h.used("node:sgw_id", 99) == {_key(cells, node): 1}


def test_build_rotates_nodes_across_consecutive_faults(tmp_path):
    import datetime as dt
    cells = _sgws(names=("A", "B", "C", "D"))
    h = faults.TargetHistory(tmp_path / "t.json")
    anchor = dt.datetime(2026, 11, 8, 0, 0)
    seen = []
    for fid in range(1, 9):
        row = {"fault_id": fid, "kind": "gw_resource" if fid % 2 else "dns_degradation", "target": None,
               "start_ts": dt.datetime(2026, 11, 8, 1, 0), "duration_min": 11, "intensity": 1.0}
        seen.append(faults.build(row, cells, anchor, faults.target_rng(7, fid), seed=7, history=h).target.sgw_id)
    assert all(a != b for a, b in zip(seen, seen[1:])), "같은 종류 노드를 쓰는 kind 끼리 연달아 같은 노드가 없다"
    assert len(set(seen)) == 4


# ---------------------------------------------------------------- Generator._rotate_pick · 기종 · APN

def _gen(history=None, subs=(), apn_pool=None, scenarios=()):
    g = object.__new__(Generator)
    g.target_history = history
    g.subs = list(subs)
    g.scenarios = list(scenarios)
    g.apn_pool = apn_pool
    g.app_book = None
    g.profiles = {}
    g.rng = random.Random(1)
    return g


def _sc(fid, kind):
    return SimpleNamespace(id=f"F-{fid}", kind=kind, target=SimpleNamespace(
        vendor_model=None, apn=None, domain=None, app_id=None, app_group=None, app_hosts=None))


def _subs(counts):
    """`{(제조사, 모델): 인원}` — 인원 내림차순이 순위다."""
    return [SimpleNamespace(vendor=v, model=m) for (v, m), n in counts.items() for _ in range(n)]


def test_rotate_pick_none_without_history_or_numeric_id(tmp_path):
    g = _gen(history=None)
    assert g._rotate_pick(_sc(1, "ue_model"), "ue_model", ["a", "b"], lambda c: c) is None
    h = faults.TargetHistory(tmp_path / "t.json")
    g = _gen(history=h)
    assert g._rotate_pick(SimpleNamespace(id="X-1"), "ue_model", ["a", "b"], lambda c: c) is None
    assert g._rotate_pick(_sc(1, "ue_model"), "ue_model", [], lambda c: c) is None


def test_rotate_pick_picks_an_unused_candidate_and_records_it(tmp_path):
    h = faults.TargetHistory(tmp_path / "t.json")
    g = _gen(history=h)
    key = lambda c: f"APN:{c}"      # noqa: E731
    h.record(1, "apn", faults.target_key([key("a")]))
    pick = g._rotate_pick(_sc(2, "x"), "apn", ["a", "b"], key)
    assert pick == "b"
    assert h.used("apn", 99)[faults.target_key([key("b")])] == 2


def test_rotate_pick_is_deterministic_for_the_same_fault(tmp_path):
    h = faults.TargetHistory(tmp_path / "t.json")
    g = _gen(history=h)
    a = g._rotate_pick(_sc(5, "x"), "ue_model", ["a", "b", "c", "d"], lambda c: c)
    b = g._rotate_pick(_sc(5, "x"), "ue_model", ["a", "b", "c", "d"], lambda c: c)
    assert a == b


def test_ue_model_rotates_through_the_pool_starting_from_rank_three(tmp_path):
    models = {("V", f"M{i}"): 100 - i for i in range(8)}              # M0 이 1위
    h = faults.TargetHistory(tmp_path / "t.json")
    seen = []
    for fid in range(1, 6):
        g = _gen(history=h, subs=_subs(models), scenarios=[_sc(fid, "ue_model")])
        g._bind_scenario_targets()
        seen.append(g.scenarios[0].target.vendor_model)
    pool = [("V", f"M{i}") for i in range(2, 2 + Generator.UE_MODEL_POOL)]
    assert set(seen) == set(pool), "3위부터 5개를 한 바퀴"
    assert all(a != b for a, b in zip(seen, seen[1:]))
    assert ("V", "M0") not in seen and ("V", "M1") not in seen, "표본이 가장 두터운 상위 둘은 쓰지 않는다(지금 기준)"


def test_ue_model_without_history_keeps_rank_three(tmp_path):
    models = {("V", f"M{i}"): 100 - i for i in range(8)}
    g = _gen(history=None, subs=_subs(models), scenarios=[_sc(1, "ue_model")])
    g._bind_scenario_targets()
    assert g.scenarios[0].target.vendor_model == ("V", "M2")


def test_ue_model_with_fewer_than_three_models_uses_the_last(tmp_path):
    g = _gen(history=None, subs=_subs({("V", "A"): 5, ("V", "B"): 3}), scenarios=[_sc(1, "ue_model")])
    g._bind_scenario_targets()
    assert g.scenarios[0].target.vendor_model == ("V", "B")


def test_apn_rotates_inside_the_top_three_and_falls_back_without_history(tmp_path):
    h = faults.TargetHistory(tmp_path / "t.json")
    pool = ["a", "b", "c", "d", "e"]
    seen = []
    for fid in range(1, 7):
        g = _gen(history=h, apn_pool=pool, scenarios=[_sc(fid, "change_suspected")])
        g._bind_scenario_targets()
        seen.append(g.scenarios[0].target.apn)
    assert set(seen) == {"a", "b", "c"}, "상위 3 안에서만"
    assert all(x != y for x, y in zip(seen, seen[1:]))
    g = _gen(history=None, apn_pool=pool, scenarios=[_sc(1, "change_suspected")])
    g._bind_scenario_targets()
    assert g.scenarios[0].target.apn in {"a", "b", "c"}, "기록이 없으면 지금처럼 `_top_apn`"
