"""16번 자동 대상 추첨 · SA 호 옮기기 · 권장 길이 · 5G 비율 yaml (2026-11-06).

* `_anchor_target` — 후보 조건(평소 인원·NR 셀 수), 주입마다 추첨, 군집은 한 메시 안, 후보 없음·조회 실패면 None.
* `_sa_floor_calls` — `floor_sa_kinds` 에 든 kind 만 센다(기본은 비어 있어 0).
* `recommended_duration` — 탐지 최소 + 여유 5분 (16번 30 · 17번 25).
* `datagen.yaml` — `nr_by_length` 를 PemdbGen 이 읽는다.

DB 없이 돈다 — 메시 인원 조회(`load`)는 함수로 갈아 끼운다.
"""
from __future__ import annotations

import random
from datetime import datetime
from types import SimpleNamespace
from pathlib import Path

import yaml

from datagen import faults
from datagen import pemdb1 as P
from datagen import scenarios as S
from datagen.cells import Cell

ROOT = Path(__file__).resolve().parents[1]
T = datetime(2026, 11, 6, 21, 40)


def _cell(enb: int, n: int, lat: float) -> Cell:
    return Cell(
        cell_key=f"{enb}-{n}", enb_id=str(enb), cell_id=n, enb_seq=0, c_uid="",
        tac=100, plmn="44010", vlan_id="", latitude=lat, longitude=139.70,
        adm_sido_cd="11", adm_sido_nm="", adm_sgg_cd="11200", adm_sgg_nm="",
        adm_emd_cd="11200011", adm_emd_nm="", geo_status="MAPPED",
        rat="EUTRAN", mme_id="0001", sgw_id="0037", pgw_id="0037",
        ims_pgw_id="0037", traffic_weight=1.0, display_name="")


def _cells(enb0: int, n_enb: int, lat0: float) -> list[Cell]:
    return [_cell(enb0 + e, n, lat0 + e * 0.001) for e in range(n_enb) for n in range(3)]


A, B, C = _cells(1000, 6, 35.60), _cells(2000, 6, 35.70), _cells(3000, 1, 35.80)
ALL = A + B + C


def _keys(cs):
    return {c.cell_key for c in cs}


def _load(people_a=60, people_b=50, people_c=500):
    """메시 → (평소 인원, 셀 키). C 는 사람은 많은데 셀이 3개뿐이다."""
    return lambda ts: {"M-A": (people_a, _keys(A)), "M-B": (people_b, _keys(B)), "M-C": (people_c, _keys(C))}


def _fault():
    return {"start_ts": T}


# ---------------------------------------------------------------- _anchor_target

def test_candidates_need_both_people_and_cells():
    seen = set()
    for s in range(60):
        t = faults._anchor_target(ALL, random.Random(s), _fault(), _load())
        assert t is not None
        seen.add(frozenset(k.split("-")[0][0] for k in t.cell_keys))
    # C 는 셀 3개 < 8, 그래서 한 번도 뽑히지 않는다. A·B 는 뽑힌다
    assert seen == {frozenset("1"), frozenset("2")}


def test_a_mesh_below_the_people_floor_is_not_a_candidate():
    for s in range(30):
        t = faults._anchor_target(ALL, random.Random(s), _fault(), _load(people_a=39))
        assert _keys(B) >= t.cell_keys


def test_the_pick_rotates_between_injections_and_is_not_the_busiest_mesh():
    """가장 붐비는 칸(A=60)으로 고정하지 않는다 — 사람이 더 적은 B 도 뽑힌다."""
    hit = {next(iter(faults._anchor_target(ALL, random.Random(s), _fault(), _load()).cell_keys))[0] for s in range(60)}
    assert hit == {"1", "2"}


def test_the_cluster_stays_inside_one_mesh():
    for s in range(60):
        t = faults._anchor_target(ALL, random.Random(s), _fault(), _load())
        assert t.cell_keys <= _keys(A) or t.cell_keys <= _keys(B)


def test_same_seed_same_target():
    a = faults._anchor_target(ALL, random.Random(7), _fault(), _load())
    b = faults._anchor_target(ALL, random.Random(7), _fault(), _load())
    assert a.cell_keys == b.cell_keys


def test_no_candidate_returns_none_so_the_caller_falls_back_to_the_cluster_pick():
    assert faults._anchor_target(ALL, random.Random(1), _fault(), _load(people_a=1, people_b=1)) is None
    assert faults._anchor_target(ALL, random.Random(1), _fault(), lambda ts: {}) is None
    assert faults._anchor_target(ALL, random.Random(1), _fault(), None) is None


def test_a_failing_lookup_does_not_stop_generation():
    def boom(ts):
        raise RuntimeError("창고 끊김")
    assert faults._anchor_target(ALL, random.Random(1), _fault(), boom) is None


def test_unmapped_cells_are_not_counted_toward_the_cell_floor():
    import dataclasses
    un = [dataclasses.replace(c, geo_status="UNMAPPED") for c in A]
    assert faults._anchor_target(un + B, random.Random(1), _fault(), _load(people_b=1)) is None


# ---------------------------------------------------------------- _sa_floor_calls

def _active(kind, cells=3, level=1.0):
    sc = SimpleNamespace(kind=kind, target=SimpleNamespace(cell_keys={"x"}), peak=1.0,
                         active_cells_cache=set(range(cells)), intensity=lambda m: level)
    return sc


def _gen(kinds, per_kind):
    g = SimpleNamespace(fault=S.FaultCfg())
    g.fault.floor_sa_kinds = tuple(kinds)
    g.fault.floor_by_kind = dict(per_kind)
    return g


def test_sa_floor_calls_are_zero_when_no_kind_is_listed():
    """기본 yaml 은 `floor_sa_kinds: []` — 호를 옮기지 않는다."""
    g = _gen([], {"anchor_saturation": 12})
    assert P.PemdbGen._sa_floor_calls(g, 5, [_active("anchor_saturation")]) == 0


def test_sa_floor_calls_count_only_the_listed_kind_per_cell():
    g = _gen(["anchor_saturation"], {"anchor_saturation": 12})
    act = [_active("anchor_saturation", cells=5), _active("mobility", cells=5)]
    assert P.PemdbGen._sa_floor_calls(g, 5, act) == 60


def test_sa_floor_calls_follow_the_visible_level_and_skip_wide_targets():
    g = _gen(["anchor_saturation"], {"anchor_saturation": 12})
    assert P.PemdbGen._sa_floor_calls(g, 5, [_active("anchor_saturation", cells=4, level=0.5)]) == 24
    wide = _active("anchor_saturation")
    wide.target.cell_keys = set(range(g.fault.floor_max_cells + 1))
    assert P.PemdbGen._sa_floor_calls(g, 5, [wide]) == 0


def test_repo_yaml_leaves_the_sa_floor_off():
    f = yaml.safe_load((ROOT / "config" / "datagen.yaml").read_text(encoding="utf-8"))["pemdb1"]["fault"]
    assert not f.get("floor_sa_kinds")
    assert "anchor_saturation" not in (f.get("floor_by_kind") or {})


# ---------------------------------------------------------------- 권장 길이

def test_recommended_is_detect_min_plus_margin_for_16_and_17():
    assert S.DETECT_MARGIN_MIN == 5
    for kind in ("anchor_saturation", "sa_fallback"):
        spec = S.KIND_SPECS[kind]
        assert spec["detect_min"] > 0
        r = S.recommended_duration(kind)
        assert r["min"] >= spec["detect_min"]
        assert r["recommended"] >= spec["detect_min"] + S.DETECT_MARGIN_MIN or r["basis"] == "measured"


def test_shape_based_recommendation_for_16_is_30_and_17_is_25(monkeypatch):
    """실측이 있으면 실측이 앞선다 — 모양 근거만 본다."""
    from datagen import measured
    monkeypatch.setattr(measured, "summary", lambda k, **kw: {"n": 0, "runs": 0, "to_correct": 0, "settle": None})
    assert S.recommended_duration("anchor_saturation")["recommended"] == 30
    assert S.recommended_duration("anchor_saturation")["min"] == 25
    assert S.recommended_duration("sa_fallback")["recommended"] == 25


def test_rule_pending_is_released_for_14_15_17():
    for kind, rule in (("ran_congestion", "R-47"), ("ho_interruption", "R-40"), ("sa_fallback", "R-48")):
        spec = S.KIND_SPECS[kind]
        assert rule in spec["expect_rules"] and not spec.get("rule_pending")


# ---------------------------------------------------------------- 5G 비율 yaml

def _net_mode():
    return yaml.safe_load((ROOT / "config" / "datagen.yaml").read_text(encoding="utf-8"))["pemdb1"]["net_mode"]


def test_yaml_ratios_follow_the_user_decision_of_1106():
    nm = _net_mode()
    assert 0.5 <= nm["sa_device_share"] <= 0.7
    assert nm["nr_enb_share"] == 0.9 and nm["sa_resident_share"] == 0.35
    assert [p for _, p in nm["nr_by_length"]] == [0.8, 0.85, 0.95, 1.0]


def test_nr_by_length_in_yaml_is_ascending_and_ends_open():
    by_len = [(float(a), float(b)) for a, b in _net_mode()["nr_by_length"]]
    assert by_len[-1][0] == float("inf")
    assert all(x[0] < y[0] for x, y in zip(by_len, by_len[1:]))


# ---------------------------------------------------------------- _congestion_target (14번 · F-155)

D = _cells(4000, 2, 35.612)          # A 옆 — 사람이 적고 셀이 6개뿐이라 후보는 아니지만 "가까운 칸"이다
E = _cells(5000, 6, 35.900)          # 멀리 있는 큰 칸
ALL14 = A + B + C + D + E


def _load14(people_a=60, people_b=50):
    return lambda ts: {"M-A": (people_a, _keys(A)), "M-B": (people_b, _keys(B)), "M-C": (500, _keys(C)),
                       "M-D": (5, _keys(D)), "M-E": (80, _keys(E))}


def _meshes_of(t):
    return {m for m, cs in (("A", A), ("B", B), ("C", C), ("D", D), ("E", E)) if t.cell_keys & _keys(cs)}


def test_the_congestion_target_takes_whole_meshes_not_a_few_cells():
    """F-155 — 8셀이 6칸에 흩어져 칸 비율이 묽어졌다. 칸의 셀을 **전부** 잡는다."""
    t = faults._congestion_target(ALL14, random.Random(3), _fault(), _load14())
    for m, cs in (("A", A), ("B", B), ("C", C), ("D", D), ("E", E)):
        got = t.cell_keys & _keys(cs)
        assert not got or got == _keys(cs), f"{m} 를 일부만 잡았다"


def test_the_congestion_target_takes_congestion_meshes_nearest_meshes(monkeypatch):
    monkeypatch.setattr(S.FAULT, "congestion_meshes", 2)
    for s in range(30):
        t = faults._congestion_target(ALL14, random.Random(s), _fault(), _load14())
        assert len(_meshes_of(t)) == 2
        if "A" in _meshes_of(t):
            assert "D" in _meshes_of(t), "A 의 가장 가까운 이웃은 D 다"


def test_congestion_meshes_one_and_larger_counts_follow_the_setting(monkeypatch):
    for n in (1, 3, 5):
        monkeypatch.setattr(S.FAULT, "congestion_meshes", n)
        t = faults._congestion_target(ALL14, random.Random(1), _fault(), _load14())
        assert len(_meshes_of(t)) == n


def test_the_congestion_pick_rotates_and_the_start_mesh_is_a_candidate(monkeypatch):
    monkeypatch.setattr(S.FAULT, "congestion_meshes", 1)
    hit = {next(iter(_meshes_of(faults._congestion_target(ALL14, random.Random(s), _fault(), _load14()))))
           for s in range(80)}
    assert hit == {"A", "B", "E"}, "후보(인원·셀 조건)만 뽑히고 C(셀 3)·D(인원 5)는 뽑히지 않는다"


def test_the_congestion_target_falls_back_when_there_is_no_candidate():
    assert faults._congestion_target(ALL14, random.Random(1), _fault(), lambda ts: {}) is None
    assert faults._congestion_target(ALL14, random.Random(1), _fault(), None) is None

    def boom(ts):
        raise RuntimeError("창고 끊김")
    assert faults._congestion_target(ALL14, random.Random(1), _fault(), boom) is None


def test_congestion_meshes_is_read_from_yaml_and_overridable():
    f = yaml.safe_load((ROOT / "config" / "datagen.yaml").read_text(encoding="utf-8"))["pemdb1"]["fault"]
    assert f["congestion_meshes"] == 4 and S.FaultCfg.from_cfg(f).congestion_meshes == 4
    assert S.FaultCfg.from_cfg({"congestion_meshes": 2}).congestion_meshes == 2


def test_congestion_recommendation_follows_detect_min():
    from datagen import measured
    assert S.KIND_SPECS["ran_congestion"]["detect_min"] == 15
    r = S.recommended_duration("ran_congestion")
    assert r["min"] >= 15 and (r["recommended"] >= 15 + S.DETECT_MARGIN_MIN or r["basis"] == "measured")


# ---------------------------------------------------------------- 대상 추첨 경로 · 정답지 지리 (11-06 밤)

ANCHOR0 = datetime(2026, 11, 6, 0, 0)


def _row(kind, target=""):
    return {"fault_id": 901, "kind": kind, "intensity": 1.0, "duration_min": 30,
            "start_ts": T, "target": target}


def test_sa_fallback_auto_target_goes_through_the_congestion_target(monkeypatch):
    """17번 — 25셀 eNB 통째 대상이 칸 일부만 덮는 운을 없앤다(F-146). 14번과 같은 추첨을 탄다."""
    called = []
    orig = faults._congestion_target
    monkeypatch.setattr(faults, "_congestion_target", lambda *a, **k: called.append("c") or orig(*a, **k))
    monkeypatch.setattr(faults, "_anchor_target", lambda *a, **k: called.append("a"))
    sc = faults.build(_row("sa_fallback"), ALL14, ANCHOR0, random.Random(5), mesh_load=_load14())
    assert called == ["c"] and sc is not None
    got = sc.target.cell_keys
    for cs in (A, B, C, D, E):
        assert not (got & _keys(cs)) or got >= _keys(cs), "칸을 통째로 잡는다"


def test_ran_congestion_and_anchor_use_their_own_target_functions(monkeypatch):
    called = []
    monkeypatch.setattr(faults, "_congestion_target", lambda *a, **k: called.append("c"))
    monkeypatch.setattr(faults, "_anchor_target", lambda *a, **k: called.append("a"))
    faults.build(_row("ran_congestion"), ALL14, ANCHOR0, random.Random(5), mesh_load=_load14())
    faults.build(_row("anchor_saturation"), ALL14, ANCHOR0, random.Random(5), mesh_load=_load14())
    assert called[0] == "c" and called[1] == "a"


def test_an_explicit_target_skips_the_mesh_pick(monkeypatch):
    called = []
    monkeypatch.setattr(faults, "_congestion_target", lambda *a, **k: called.append("c"))
    faults.build(_row("sa_fallback", "ENB:1000"), ALL14, ANCHOR0, random.Random(5), mesh_load=_load14())
    assert called == []


def test_sa_fallback_falls_back_to_the_old_pick_when_there_is_no_candidate():
    sc = faults.build(_row("sa_fallback"), ALL14, ANCHOR0, random.Random(5), mesh_load=lambda ts: {})
    assert sc is not None and sc.target.cell_keys


def test_anchor_saturation_truth_geo_is_point():
    """대상을 메시 한 칸에서 뽑아 반경이 기준(0.9km) 아래라 POINT 가 맞다(F-156 · 11-06)."""
    assert S.KIND_SPECS["anchor_saturation"]["expect_geo"] == "POINT"
    assert S.KIND_SPECS["ran_congestion"]["expect_geo"] == "CLUSTER"
