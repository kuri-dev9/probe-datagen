"""25번 `son_param_change` — 파라미터를 넣은 **eNB 1~2개의 전 셀**이 출발 셀이다 (2026-11-13 사용자 결정 · 전에는 셀 하나의 이웃 관계 1~2개).

* `scenarios.son_focus(cells, enbs, n)` — eNB 의 전 셀 = 출발 셀, 출발 셀마다 도착 셀 ``n`` 곳(**거리순** · 이웃 관계 표가 없다). 고른 eNB 의 셀은
  도착 셀에서 뺀다. 셀별 세기는 `son_gain`(0.5~1.0 해시 × 하한 0.5 의 평소 호 비율 · **초기값**), 계단 시작 분은 모든 셀이 같다.
* `_son_param_change` — 쌍의 **출발 셀(호의 First)** 에서 시작한 호만 `intensity × 셀 배율 × 0.50` 으로 끊는다. 106 절단·PSF 50%·Last 80% 는 그대로.
* `faults` — 자동은 `traffic_weight` 합 상위 eNB 에서 돌려 하나, `ENB:a` · `ENB:a,b` · `ENB:a,+`(가장 가까운 eNB). 순환 묶음은 `son`.
  9·15번(`mobility`·`ho_interruption`)은 이 분기를 지나지 않는다.
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


# ── son_focus · son_gain ──────────────────────────────────────────────────────

def enb_cells(n_enb=6, per=3, weights=None):
    """eNB 마다 셀 ``per`` 개 — eNB 는 0.01° 씩 북쪽으로, 셀은 0.0006° 씩 동쪽으로 떨어진다(`traffic_weight` 는 ``weights``)."""
    out = []
    for e in range(n_enb):
        for n in range(per):
            c = RS._cell(f"{600000 + e}-{n}", 35.60 + e * 0.01, 139.60 + n * 0.0006)
            if weights:
                c = dataclasses.replace(c, traffic_weight=weights[n % len(weights)])
            out.append(c)
    return out


def test_one_enb_makes_every_cell_a_source_with_nearest_destinations():
    cells = enb_cells()
    t = S.son_focus(cells, ["600001"], 2)
    src = {f"600001-{n}" for n in range(3)}
    assert {a for a, _ in t.cell_pairs} == src and len(t.cell_pairs) == 3 * 2
    assert t.son_enbs == ("600001",) and set(t.son_gain) == src
    assert src < t.cell_keys and t.cell_keys == src | {b for _, b in t.cell_pairs}


def test_destinations_exclude_the_chosen_enbs_own_cells_and_are_nearest_first():
    cells = enb_cells()
    t = S.son_focus(cells, ["600002"], 2)
    for a, b in t.cell_pairs:
        assert not b.startswith("600002-"), (a, b)
    # 이웃 eNB(남 600001 · 북 600003) 셀이 도착 셀이다 — 가장 가까운 순
    assert {b.split("-")[0] for _, b in t.cell_pairs} <= {"600001", "600003"}
    first = [(a, b) for a, b in t.cell_pairs if a == "600002-0"]
    assert len(first) == 2 and first[0][1] != first[1][1]


def test_two_enbs_both_become_sources_and_neither_is_a_destination_of_the_other():
    cells = enb_cells()
    t = S.son_focus(cells, ["600001", "600002"], 1)
    src = {a for a, _ in t.cell_pairs}
    assert src == {f"6000{e:02d}-{n}" for e in (1, 2) for n in range(3)} and len(t.cell_pairs) == 6
    assert t.son_enbs == ("600001", "600002")
    assert all(b.split("-")[0] not in ("600001", "600002") for _, b in t.cell_pairs)


def test_n_destinations_per_source_and_n_zero_makes_no_pairs():
    cells = enb_cells()
    assert len(S.son_focus(cells, ["600001"], 1).cell_pairs) == 3
    assert len(S.son_focus(cells, ["600001"], 4).cell_pairs) == 12
    t0 = S.son_focus(cells, ["600001"], 0)
    assert t0.cell_pairs == [] and len(t0.cell_keys) == 3                                # 출발 셀은 대상에 남는다


def test_unknown_enb_returns_none_and_unmapped_sources_stay_without_pairs():
    cells = enb_cells()
    assert S.son_focus(cells, ["999999"], 2) is None and S.son_focus(cells, [], 2) is None
    cells = [dataclasses.replace(c, geo_status="UNMAPPED") if c.cell_key == "600001-1" else c for c in cells]
    t = S.son_focus(cells, ["600001"], 2)
    assert "600001-1" in t.cell_keys and "600001-1" not in {a for a, _ in t.cell_pairs}   # 숨기지 않는다


def test_son_gain_is_in_range_fixed_per_cell_and_load_floored():
    cells = enb_cells(weights=[1.0, 0.2, 0.05])
    src = [c for c in cells if c.enb_id == "600001"]
    g = S.son_gain(src)
    lo, hi = S.SON_GAIN_RANGE
    assert set(g) == {c.cell_key for c in src}
    for k, v in g.items():
        assert lo * S.SON_LOAD_FLOOR - 1e-9 <= v <= hi + 1e-9, (k, v)                      # 호 비율 하한 0.5 라 한산한 셀도 0 에 가깝지 않다
    assert g == S.son_gain(list(reversed(src)))                                            # 순서·회차와 무관하게 같은 셀은 같은 배율
    assert len(set(g.values())) > 1                                                        # 셀마다 다르다


def test_son_gain_load_ratio_is_per_enb_and_defaults_to_full_without_weight():
    cells = [dataclasses.replace(c, traffic_weight=0.0) for c in enb_cells()]
    g = S.son_gain([c for c in cells if c.enb_id == "600001"])
    assert all(S.SON_GAIN_RANGE[0] <= v <= S.SON_GAIN_RANGE[1] for v in g.values())         # 가중이 없으면 호 비율 1.0


def test_son_near_enb_is_the_closest_other_enb_by_site_centre():
    cells = enb_cells()
    assert S.son_near_enb(cells, "600000") == "600001"
    assert S.son_near_enb(cells, "600005") == "600004"
    assert S.son_near_enb(cells, "nope") is None
    assert S.son_near_enb([c for c in cells if c.enb_id == "600000"], "600000") is None     # 다른 eNB 가 없다


def test_the_target_label_names_both_enbs():
    t = S.son_focus(enb_cells(), ["600001", "600002"], 1)
    assert "eNB-600001·600002" in t.label()


# ── 주입 ────────────────────────────────────────────────────────────────────

SRC, D1, D2, OTHER = "600001-0", "600002-0", "600003-0", "600009-0"


def _sc(focus=2, pairs=None, active=None, gain=None):
    pairs = pairs if pairs is not None else [(SRC, D1), (SRC, D2)]
    return SimpleNamespace(focus_pairs=focus, target=SimpleNamespace(cell_pairs=pairs, cell_keys={SRC, D1, D2}, son_gain=gain or {}),
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
    assert "intensity * gain * 0.50" in src and "_handover_failure(rec, sc, rng, pairs=pairs)" in src
    assert "callflow.drop(rec, 106, rng)" in inspect.getsource(S._handover_failure)


# ── faults.build ────────────────────────────────────────────────────────────

def test_the_son_target_is_all_cells_of_one_enb_in_the_fault_build():
    sc = RS._build("son_param_change", routes=[RS._line_route()])
    t = sc.target
    assert len(t.son_enbs) == 1 and t.route_seg is None                 # 노선 구간이 아니다
    src = {a for a, _ in t.cell_pairs}
    assert src == {c.cell_key for c in RS._grid_cells() if c.enb_id == t.son_enbs[0]} and len(src) == 3
    assert len(t.cell_pairs) == 3 * 2 and t.cell_keys == src | {b for _, b in t.cell_pairs}
    assert set(t.son_gain) == src


def test_son_target_without_routes_is_the_same_kind_of_target():
    sc = RS._build("son_param_change", routes=None)
    assert len(sc.target.son_enbs) == 1 and len({a for a, _ in sc.target.cell_pairs}) == 3


@pytest.mark.parametrize("kind", ["mobility", "ho_interruption"])
def test_mobility_and_ho_interruption_targets_are_not_son_targets(kind):
    sc = RS._build(kind, routes=[RS._line_route()])
    assert len(sc.target.cell_pairs) >= 4 and len(sc.target.cell_keys) > 3 and not sc.target.son_enbs


def test_other_kinds_are_not_touched():
    for kind in ("ran_radio", "transport_loss", "mme_overload"):
        sc = RS._build(kind)
        assert sc is not None and sc.focus_pairs == 0 and not sc.target.cell_pairs and not sc.target.son_enbs


def test_explicit_one_enb_target():
    sc = RS._build("son_param_change", target="ENB:600000")
    assert sc.target.son_enbs == ("600000",) and len({a for a, _ in sc.target.cell_pairs}) == 3


@pytest.mark.parametrize("target,enbs", [("ENB:600000,600001", ("600000", "600001")), ("ENB:600000,+", None)])
def test_explicit_two_enb_targets(target, enbs):
    sc = RS._build("son_param_change", target=target)
    t = sc.target
    assert len(t.son_enbs) == 2 and t.son_enbs[0] == "600000"
    if enbs:
        assert t.son_enbs == enbs
    else:
        assert t.son_enbs[1] == S.son_near_enb(RS._grid_cells(), "600000")                # `+` = 가장 가까운 eNB
    assert len({a for a, _ in t.cell_pairs}) == 6


def test_explicit_targets_the_rule_rejects(capsys):
    assert RS._build("son_param_change", target="ENB:600000,600001,600002") is None       # eNB 는 1~2개
    assert RS._build("son_param_change", target="ENB:+,600001") is None                    # `+` 는 둘째 자리뿐
    assert RS._build("son_param_change", target="ENB:999999") is None                      # 없는 eNB
    assert RS._build("ran_radio", target="ENB:600000,600001") is None                      # 쉼표 목록은 25번만
    out = capsys.readouterr().out
    assert "1~2개" in out and "25번만 받는다" in out


def test_box_target_uses_all_cells_of_the_first_mapped_enb_in_the_range():
    cells = RS._grid_cells()
    c0 = next(c for c in cells if c.enb_id == "600003")
    box = f"BOX:{c0.latitude - 0.0003},{c0.longitude - 0.0003},{c0.latitude + 0.0003},{c0.longitude + 0.0003}"
    sc = RS._build("son_param_change", target=box)
    assert sc is not None and len(sc.target.son_enbs) == 1 and len({a for a, _ in sc.target.cell_pairs}) == 3


def test_like_copies_the_earlier_target():
    orig = RS._build("son_param_change", routes=[RS._line_route()], fid=1)
    row = {"fault_id": 30, "kind": "son_param_change", "target": "LIKE:1", "start_ts": RS.T0 + timedelta(minutes=2),
           "duration_min": 30, "intensity": 1.0}
    sc = faults.build(row, RS._grid_cells(), RS.T0, faults.target_rng(7, 30), seed=7, routes=[RS._line_route()])
    assert sc.target.cell_keys == orig.target.cell_keys


def test_auto_target_picks_among_the_heaviest_enbs_and_rotates(tmp_path):
    cells = [dataclasses.replace(c, traffic_weight=(5.0 if c.enb_id in ("600001", "600002") else 1.0)) for c in RS._grid_cells()]
    h = faults.TargetHistory(tmp_path / "t.json")
    seen = []
    for fid in (1, 2, 3):
        row = {"fault_id": fid, "kind": "son_param_change", "target": None,
               "start_ts": RS.T0 + timedelta(minutes=2), "duration_min": 30, "intensity": 1.0}
        sc = faults.build(row, cells, RS.T0, faults.target_rng(7, fid), seed=7, history=h)
        seen.append(sc.target.son_enbs[0])
    # 후보는 `traffic_weight` 합 상위 `SON_AUTO_TOP_ENBS` 개다 — 무거운 둘은 반드시 후보 안이고, 고른 eNB 를 거듭 고르지 않는다
    per = {}
    for c in cells:
        per[c.enb_id] = per.get(c.enb_id, 0.0) + c.traffic_weight
    cand = set(sorted(per, key=lambda e: (-per[e], e))[:S.SON_AUTO_TOP_ENBS])
    assert {"600001", "600002"} <= cand and set(seen) <= cand and len(set(seen)) == 3


def test_the_rotation_history_records_the_target_key_under_the_son_group(tmp_path):
    h = faults.TargetHistory(tmp_path / "t.json")
    row = {"fault_id": 5, "kind": "son_param_change", "target": None,
           "start_ts": RS.T0 + timedelta(minutes=2), "duration_min": 30, "intensity": 1.0}
    sc = faults.build(row, RS._grid_cells(), RS.T0, faults.target_rng(7, 5), seed=7, history=h)
    rec = json.loads((tmp_path / "t.json").read_text())
    (slot, val), = rec.items()
    assert slot.startswith("5:") and val["key"] == faults.target_key(sc.target.cell_keys)     # 11-13: 좁히기 전후 키가 같다(대상을 만들 때 이미 좁다)


# ── 정답지 ──────────────────────────────────────────────────────────────────

def test_the_truth_carries_son_enbs_cells_roles_gains_and_pairs():
    sc = RS._build("son_param_change", target="ENB:600000,+")
    tr = sc.as_truth()
    assert tr["son_enbs"] == list(sc.target.son_enbs) and tr["son_pairs"] == [list(p) for p in sc.target.cell_pairs]
    src = [c for c in tr["son_cells"] if c["role"] == "src"]
    dst = [c for c in tr["son_cells"] if c["role"] == "dst"]
    assert len(src) == 6 and all(0 < c["gain"] <= 1.0 for c in src) and all(c["gain"] is None for c in dst)
    assert {c["cell"] for c in src}.isdisjoint({c["cell"] for c in dst})
    assert set(tr["cells"]) >= {c["cell"] for c in tr["son_cells"]} if "cells" in tr else True


def test_other_kinds_carry_no_son_keys():
    tr = RS._build("mobility", routes=[RS._line_route()]).as_truth()
    assert "son_enbs" not in tr and "son_cells" not in tr


def test_expect_geo_is_measured_from_the_chosen_cells():
    one = RS._build("son_param_change", target="ENB:600000").as_truth()
    two = RS._build("son_param_change", target="ENB:600000,600059").as_truth()           # 멀리 떨어진 두 eNB
    assert one["expect_geo_static"] == "POINT"
    assert two["expect_geo"] != "POINT" or two["expect_geo_static"] == "POINT"             # 잰 값이 우선 — 고정값은 못 잴 때만


# ── 배치 J-SC-25 ────────────────────────────────────────────────────────────

def test_the_batch_scenario_uses_the_same_son_focus_as_the_ui_injection():
    import inspect
    src = inspect.getsource(S.build_scenarios)
    assert "son_focus(cells, [son_anchor.enb_id]" in src
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
