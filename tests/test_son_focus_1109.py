"""25번 `son_param_change` — 전환 실패를 셀 하나의 이웃 관계 1~2개에 몰아 넣는다 (2026-11-09 · 사용자 "특정 셀 쌍").

* `scenarios.son_focus` — 대상을 출발 셀 하나와 도착 셀 1~n 곳으로 좁힌다(노선 사슬은 가운데 칸 = 출발).
* `_son_param_change` — `focus_pairs` 가 있으면 그 쌍의 **출발 셀(호의 First)** 에서 시작한 호만 끊고, `_handover_failure(pairs=…)` 가 도착 셀을
  Last 로 적는다. 강도 0.50·106 절단·PSF 50%·Last 80% 는 그대로.
* `faults.build` — 순환 기록은 좁히기 **전** 키로, 25번만 좁힌다. 9·15번(`mobility`·`ho_interruption`)은 이 분기를 지나지 않는다.
DB 없이.
"""
from __future__ import annotations

import dataclasses
import json
import random
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from datagen import faults
from datagen import scenarios as S
from datagen.scenarios import KIND_SPECS, Scenario, Target

import test_nr_fields as NF
import test_route_segment as RS

HERE = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def gen():
    return NF._gen(True)


# ── son_focus ────────────────────────────────────────────────────────────────

def chain(*keys):
    return [(a, b) for a, b in zip(keys, keys[1:])]


def T(pairs, **kw):
    cells = {k for p in pairs for k in p}
    return Target(cell_keys=cells, cell_pairs=list(pairs), **kw)


def test_a_route_chain_takes_the_middle_cell_as_the_source_and_its_two_neighbours_as_destinations():
    t = S.son_focus(T(chain("a", "b", "c", "d", "e")), 2)
    assert t.cell_pairs == [("c", "d"), ("c", "b")]
    assert t.cell_keys == {"b", "c", "d"}


@pytest.mark.parametrize("keys,src,nbrs", [
    (("a", "b", "c"), "b", ["c", "a"]),
    (("a", "b", "c", "d"), "c", ["d", "b"]),
    (("a", "b", "c", "d", "e", "f"), "d", ["e", "c"]),
    (tuple("abcdefg"), "d", ["e", "c"]),
])
def test_chain_lengths_even_and_odd_stay_inside_the_chain(keys, src, nbrs):
    t = S.son_focus(T(chain(*keys)), 2)
    assert t.cell_pairs == [(src, n) for n in nbrs]
    assert t.cell_keys == {src, *nbrs} and t.cell_keys <= set(keys)


def test_a_single_pair_chain_stays_as_it_is():
    t = S.son_focus(T([("a", "b")]), 2)
    assert t.cell_pairs == [("a", "b")] and t.cell_keys == {"a", "b"}


def test_n_one_keeps_only_the_first_neighbour():
    t = S.son_focus(T(chain("a", "b", "c", "d", "e")), 1)
    assert t.cell_pairs == [("c", "d")] and t.cell_keys == {"c", "d"}


def test_a_band_from_one_anchor_keeps_the_first_n_pairs():
    band = [("x", "y"), ("x", "z"), ("x", "w")]
    t = S.son_focus(T(band), 2)
    assert t.cell_pairs == [("x", "y"), ("x", "z")] and t.cell_keys == {"x", "y", "z"}
    assert S.son_focus(T(band), 1).cell_pairs == [("x", "y")]
    assert S.son_focus(T(band), 9).cell_pairs == band                 # n 이 쌍 수보다 크면 전부


@pytest.mark.parametrize("n", [0, -1])
def test_n_zero_or_negative_returns_the_original_object(n):
    t = T(chain("a", "b", "c"))
    assert S.son_focus(t, n) is t


def test_no_pairs_returns_the_original_object():
    t = Target(cell_keys={"a", "b"})
    assert S.son_focus(t, 2) is t


def test_cell_keys_are_only_the_source_and_destinations_and_other_fields_survive():
    t = T(chain("a", "b", "c", "d", "e"), mme_id="0001", sgg_codes={"13104"}, route_seg={"route_id": "L1"})
    f = S.son_focus(t, 2)
    assert f.cell_keys == {"b", "c", "d"} and f.mme_id == "0001" and f.sgg_codes == {"13104"}
    assert f.route_seg == {"route_id": "L1"}
    assert t.cell_pairs == chain("a", "b", "c", "d", "e") and len(t.cell_keys) == 5     # 원본은 그대로


# ── 주입 ────────────────────────────────────────────────────────────────────

SRC, D1, D2, OTHER = "600001-0", "600002-0", "600003-0", "600009-0"


def _sc(focus=2, pairs=None, active=None):
    pairs = pairs if pairs is not None else [(SRC, D1), (SRC, D2)]
    return SimpleNamespace(focus_pairs=focus, target=SimpleNamespace(cell_pairs=pairs, cell_keys={SRC, D1, D2}),
                           active_cells_cache=set(active if active is not None else {SRC, D1, D2}))


def _cell(key):
    return SimpleNamespace(cell_key=key)


def _call(gen, sc, cell_key, seed, intensity=1.0):
    rec = NF._nr_rec(gen, change=3, ps=1)
    rec.set("success_flag", "1")
    rec.set("UEContextReleaseRequest_Time", f"{NF.START + 50:.6f}")
    before = (rec.get("Last_eNB_ID"), rec.get("Last_Cell_ID"))
    return rec, before, S._son_param_change(rec, sc, intensity, random.Random(seed), _cell(cell_key))


def test_only_calls_that_start_in_the_source_cell_are_cut(gen):
    sc = _sc()
    for k in (D1, D2, OTHER):
        assert not any(_call(gen, sc, k, s)[2] for s in range(120)), k


def test_a_source_cell_call_is_cut_with_probability_intensity_times_half(gen):
    sc = _sc()
    cut = sum(_call(gen, sc, SRC, s)[2] for s in range(400))
    assert 100 <= cut <= 300                                # 0.50 · 일부는 연결·절차 조건으로 빠진다


def test_intensity_zero_never_cuts_and_lower_intensity_cuts_less(gen):
    sc = _sc()
    assert not any(_call(gen, sc, SRC, s, intensity=0.0)[2] for s in range(100))
    hi = sum(_call(gen, sc, SRC, s, intensity=1.0)[2] for s in range(300))
    lo = sum(_call(gen, sc, SRC, s, intensity=0.4)[2] for s in range(300))
    assert lo < hi


def test_an_inactive_source_cell_is_not_cut(gen):
    sc = _sc(active={D1, D2})
    assert not any(_call(gen, sc, SRC, s)[2] for s in range(100))


def test_last_cell_is_one_of_the_destinations_or_untouched(gen):
    sc = _sc()
    dests = {D1, D2}
    hits, same = set(), 0
    for s in range(400):
        rec, before, ok = _call(gen, sc, SRC, s)
        if not ok:
            continue
        now = (rec.get("Last_eNB_ID"), rec.get("Last_Cell_ID"))
        if now == before:
            same += 1
            continue
        key = f"{now[0]}-{now[1]}"
        assert key in dests, key
        hits.add(key)
    assert hits == dests                                   # 두 쌍 모두 쓰인다
    assert same > 0                                        # 20% 는 출발 셀에서 끊긴다


def test_the_cut_call_keeps_the_old_failure_shape(gen):
    sc = _sc()
    psf = cuts = 0
    for s in range(400):
        rec, _b, ok = _call(gen, sc, SRC, s)
        if not ok:
            continue
        cuts += 1
        assert rec.get("drop_flag") == "1" and int(rec.num("PathSwitch_count")) >= 1
        assert rec.get("last_error_cause") == "106" or rec.get("first_error_cause") in ("106", "100")
        psf += int(rec.num("PathSwitchFailure_count") or 0)
    assert cuts > 50 and 0.3 < psf / cuts < 0.7             # PSF 50%


def test_without_focus_pairs_the_old_behaviour_is_kept(gen):
    """9번과 같다 — 출발 셀 제한이 없고 쌍 목록 전체에서 도착 셀을 뽑는다."""
    pairs = [(SRC, D1), (SRC, D2), (OTHER, D1)]
    sc = _sc(focus=0, pairs=pairs, active={SRC, D1, D2, OTHER})
    assert any(_call(gen, sc, OTHER, s)[2] for s in range(120)), "쌍의 출발이 아니어도 끊는다"
    assert any(_call(gen, sc, D1, s)[2] for s in range(120))


def test_handover_failure_pairs_none_uses_the_whole_pair_list(gen):
    sc = _sc(pairs=[(SRC, D1), (OTHER, D2)])
    seen = set()
    for s in range(300):
        rec = NF._nr_rec(gen, change=3, ps=1)
        rec.set("success_flag", "1")
        rec.set("UEContextReleaseRequest_Time", f"{NF.START + 50:.6f}")
        before = (rec.get("Last_eNB_ID"), rec.get("Last_Cell_ID"))
        if S._handover_failure(rec, sc, random.Random(s)):
            now = (rec.get("Last_eNB_ID"), rec.get("Last_Cell_ID"))
            if now != before:
                seen.add(f"{now[0]}-{now[1]}")
    assert seen == {D1, D2}


def test_handover_failure_with_pairs_restricts_the_destinations(gen):
    sc = _sc(pairs=[(SRC, D1), (OTHER, D2)])
    seen = set()
    for s in range(300):
        rec = NF._nr_rec(gen, change=3, ps=1)
        rec.set("success_flag", "1")
        rec.set("UEContextReleaseRequest_Time", f"{NF.START + 50:.6f}")
        before = (rec.get("Last_eNB_ID"), rec.get("Last_Cell_ID"))
        if S._handover_failure(rec, sc, random.Random(s), pairs=[(SRC, D1)]):
            now = (rec.get("Last_eNB_ID"), rec.get("Last_Cell_ID"))
            if now != before:
                seen.add(f"{now[0]}-{now[1]}")
    assert seen == {D1}


# ── KIND_SPECS · 9·15번 ──────────────────────────────────────────────────────

def test_only_son_param_change_has_focus_pairs():
    assert KIND_SPECS["son_param_change"]["focus_pairs"] == 2
    for kind, spec in KIND_SPECS.items():
        if kind != "son_param_change":
            assert not spec.get("focus_pairs"), kind
    assert "focus_pairs" not in KIND_SPECS["mobility"] and "focus_pairs" not in KIND_SPECS["ho_interruption"]


def test_scenario_default_is_zero_and_the_son_scenario_carries_two():
    assert {f.name: f.default for f in dataclasses.fields(Scenario)}["focus_pairs"] == 0
    assert RS._build("son_param_change", routes=[RS._line_route()]).focus_pairs == 2
    assert RS._build("mobility", routes=[RS._line_route()]).focus_pairs == 0
    assert RS._build("ho_interruption", routes=[RS._line_route()]).focus_pairs == 0


def test_the_son_step_shape_and_failure_constants_are_unchanged():
    sp = KIND_SPECS["son_param_change"]
    assert sp["shape"] == "STEP" and sp["ramp_in"] == 1 and sp["onset"] == "instant" and sp["offset"] == "instant"
    assert sp["scope_kind"] == "CELL" and sp["expect_kpi"] == "C-04" and sp["grade"] == "TOP3"
    import inspect
    src = inspect.getsource(S._son_param_change)
    assert "intensity * 0.50" in src and "_handover_failure(rec, sc, rng, pairs=pairs)" in src
    assert "callflow.drop(rec, 106, rng)" in inspect.getsource(S._handover_failure)


# ── faults.build ────────────────────────────────────────────────────────────

def test_the_son_target_is_focused_in_the_fault_build():
    sc = RS._build("son_param_change", routes=[RS._line_route()])
    assert len(sc.target.cell_pairs) == 2 and len(sc.target.cell_keys) == 3
    src = {a for a, _ in sc.target.cell_pairs}
    assert len(src) == 1 and sc.target.cell_keys == src | {b for _, b in sc.target.cell_pairs}
    assert sc.target.route_seg is not None                  # 노선 구간 정보는 정답지에 남는다


def test_son_band_target_without_routes_is_focused_too():
    sc = RS._build("son_param_change", routes=None)
    assert len(sc.target.cell_pairs) == 2 and len({a for a, _ in sc.target.cell_pairs}) == 1
    assert len(sc.target.cell_keys) == 3


@pytest.mark.parametrize("kind", ["mobility", "ho_interruption"])
def test_mobility_and_ho_interruption_targets_are_not_focused(kind):
    sc = RS._build(kind, routes=[RS._line_route()])
    assert len(sc.target.cell_pairs) >= 4 and len(sc.target.cell_keys) > 3


def test_other_kinds_are_not_touched():
    for kind in ("ran_radio", "transport_loss", "mme_overload"):
        sc = RS._build(kind)
        assert sc is not None and sc.focus_pairs == 0 and not sc.target.cell_pairs


@pytest.mark.parametrize("target", ["ENB:600000", "LIKE:1"])
def test_explicit_targets_are_focused_for_the_son_kind(target):
    cells = RS._grid_cells()
    if target.startswith("LIKE"):
        orig = RS._build("son_param_change", routes=[RS._line_route()], fid=1)
        row = {"fault_id": 30, "kind": "son_param_change", "target": "LIKE:1", "start_ts": RS.T0 + timedelta(minutes=2),
               "duration_min": 30, "intensity": 1.0}
        sc = faults.build(row, cells, RS.T0, faults.target_rng(7, 30), seed=7, routes=[RS._line_route()])
        assert sc.target.cell_keys == orig.target.cell_keys
    else:
        sc = RS._build("son_param_change", target=target)
    assert sc is not None and len(sc.target.cell_keys) <= 3


def test_the_rotation_history_keeps_the_pre_focus_key(tmp_path):
    rt = RS._line_route()
    h = faults.TargetHistory(tmp_path / "t.json")
    row = {"fault_id": 5, "kind": "son_param_change", "target": None,
           "start_ts": RS.T0 + timedelta(minutes=2), "duration_min": 30, "intensity": 1.0}
    sc = faults.build(row, RS._grid_cells(), RS.T0, faults.target_rng(7, 5), seed=7, routes=[rt], history=h)
    rec = json.loads((tmp_path / "t.json").read_text())
    (slot, val), = rec.items()
    assert slot.startswith("5:")
    # 기록된 키는 좁힌 3셀이 아니라 고른 구간 전체의 키다 — 같은 구간을 또 고르지 않는다
    assert val["key"] != faults.target_key(sc.target.cell_keys)
    assert len(sc.target.cell_keys) == 3


# ── 배치 J-SC-25 ────────────────────────────────────────────────────────────

def test_the_batch_scenario_has_three_cells_and_two_pairs():
    import inspect
    src = inspect.getsource(S.build_scenarios)
    assert 'son_focus(Target(cell_keys={son_anchor.cell_key}' in src
    assert 'int(KIND_SPECS["son_param_change"].get("focus_pairs") or 0)' in src


# ── 화면 설명 ───────────────────────────────────────────────────────────────

def test_scenario_how_json_is_valid_and_ko_ja_counts_match_for_25():
    d = json.loads((HERE / "config/scenario_how.json").read_text(encoding="utf-8"))["scenarios"]["25"]
    ja = d["ja"]
    assert len(d["detail"]) == len(ja["detail"]) == 7
    assert len(d["steps"]) == len(ja["steps"]) == 10
    assert len(d["traces"]) == len(ja["traces"]) == 2
    for k in ("brief", "unchanged", "limits"):
        assert d[k] and ja[k]
    txt = json.dumps(d, ensure_ascii=False)
    assert "출발 셀을 확인하지 않아" not in txt and "셀 쌍 8개" not in txt
    assert "쌍 2개" in d["steps"][2] and "모두 3셀" in d["steps"][2] and "셀 3개" in d["limits"]
    assert "出発セル" in json.dumps(ja, ensure_ascii=False)


def test_scenario_how_of_9_and_15_still_describes_the_old_pair_list():
    d = json.loads((HERE / "config/scenario_how.json").read_text(encoding="utf-8"))["scenarios"]
    for n in ("9", "15"):
        assert "focus" not in json.dumps(d[n], ensure_ascii=False)
        assert "출발 셀 하나" not in json.dumps(d[n], ensure_ascii=False)
