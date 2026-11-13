"""자동 대상 순환 (2026-11-07 사용자 "하나로 고정하지 마라") — `TargetHistory` · `_rotating` · `_rotate_group` · 네 고르기.

2번과 7번이 연달아 같은 시군구 `11110` 을 골랐다(후보 5곳). 앞선 회차가 쓴 대상은 뒤로 미루고, 다 썼으면
가장 오래전에 쓴 것부터 다시 쓴다. **기록은 대상 고르기에만** 쓴다 — 판정·정답에는 쓰지 않는다(시나리오는 단독으로 돈다).
DB 없이 돈다 — 합성 셀과 임시 파일.
"""
from __future__ import annotations

import json
import math
import random
from datetime import datetime, timedelta

import pytest

from datagen import faults
from datagen import scenarios as S
from datagen.cells import Cell

KM_LAT = 111.0
LAT0, LON0 = 35.68, 139.70
KM_LON = 111.320 * math.cos(math.radians(LAT0))
T0 = datetime(2026, 11, 7, 23, 0)


def _cell(key, lat, lon, sgg="A", enb=None):
    e, _, n = key.partition("-")
    return Cell(cell_key=key, enb_id=enb or e, cell_id=int(n or 0), enb_seq=0, c_uid="", tac=100, plmn="44010",
                vlan_id="", latitude=lat, longitude=lon, adm_sido_cd="13", adm_sido_nm="", adm_sgg_cd=sgg,
                adm_sgg_nm="", adm_emd_cd=sgg + "001", adm_emd_nm="", geo_status="MAPPED", rat="EUTRAN",
                mme_id="0001", sgw_id="0001", pgw_id="0001", ims_pgw_id="0001", traffic_weight=1.0,
                display_name="")


def _ring(prefix, n, radius_km, sgg, lon0, start=0):
    return [_cell(f"{prefix}{start + i}-0", LAT0 + radius_km * math.sin(2 * math.pi * i / n) / KM_LAT,
                  lon0 + radius_km * math.cos(2 * math.pi * i / n) / KM_LON, sgg=sgg) for i in range(n)]


def _city(n_sgg=4):
    """셀 24개 CLUSTER 시군구 `n_sgg` 곳 — 서로 50km 이상 떨어져 있다."""
    cells = []
    for i in range(n_sgg):
        cells += _ring(f"s{i}_", 24, 2.0, f"S{i}", LON0 + i * 0.7)
    return cells


# ---------------------------------------------------------------- TargetHistory

def test_history_used_returns_only_earlier_faults_of_the_same_group(tmp_path):
    h = faults.TargetHistory(tmp_path / "t.json")
    h.record(10, "sgg", "k1")
    h.record(11, "route", "k2")
    h.record(12, "sgg", "k3")
    assert h.used("sgg", 12) == {"k1": 10}, "자기 회차와 이후는 보지 않는다(결정론)"
    assert h.used("sgg", 13) == {"k1": 10, "k3": 12}
    assert h.used("route", 99) == {"k2": 11}
    assert h.used("disaster", 99) == {}


def test_history_same_key_reports_the_latest_fault_number(tmp_path):
    h = faults.TargetHistory(tmp_path / "t.json")
    h.record(3, "sgg", "k")
    h.record(8, "sgg", "k")
    assert h.used("sgg", 99) == {"k": 8}


def test_history_records_a_fault_only_once(tmp_path):
    h = faults.TargetHistory(tmp_path / "t.json")
    h.record(5, "sgg", "first")
    h.record(5, "sgg", "second")
    assert json.loads((tmp_path / "t.json").read_text())["5:sgg"]["key"] == "first"


def test_history_missing_file_is_empty_and_silent(tmp_path, capsys):
    assert faults.TargetHistory(tmp_path / "none.json").used("sgg", 9) == {}
    assert capsys.readouterr().out == ""


def test_history_broken_file_warns_and_gives_nothing(tmp_path, capsys):
    p = tmp_path / "t.json"
    p.write_text("{not json")
    assert faults.TargetHistory(p).used("sgg", 9) == {}
    assert "대상 기록을 못 읽었다" in capsys.readouterr().out


def test_history_unwritable_path_warns_and_does_not_raise(tmp_path, capsys):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    faults.TargetHistory(blocker / "sub" / "t.json").record(1, "sgg", "k")
    assert "대상 기록을 못 썼다" in capsys.readouterr().out


# ---------------------------------------------------------------- _rotating

def test_rotating_without_history_takes_the_first_or_a_random_one():
    assert faults._rotating(["a", "b", "c"], lambda x: x, None) == "a"
    assert faults._rotating(["a", "b", "c"], lambda x: x, {}) == "a"
    picked = {faults._rotating(["a", "b", "c"], lambda x: x, {}, random.Random(s)) for s in range(30)}
    assert picked == {"a", "b", "c"}


def test_rotating_skips_used_candidates():
    assert faults._rotating(["a", "b", "c"], lambda x: x, {"a": 1, "b": 2}) == "c"
    picked = {faults._rotating(["a", "b", "c", "d"], lambda x: x, {"a": 1}, random.Random(s)) for s in range(40)}
    assert picked == {"b", "c", "d"}


def test_rotating_reuses_the_oldest_when_everything_was_used():
    assert faults._rotating(["a", "b", "c"], lambda x: x, {"a": 9, "b": 3, "c": 7}) == "b"


def test_rotating_empty_candidates_is_none():
    assert faults._rotating([], lambda x: x, {"a": 1}) is None


# ---------------------------------------------------------------- _rotate_group

@pytest.mark.parametrize("kind,want", [("ran_radio", "sgg"), ("transport_loss", "sgg"), ("mobility", "route"),
                                       ("ho_interruption", "route"), ("son_param_change", "son"),
                                       ("natural_disaster", "disaster"), ("gw_resource", "node:sgw_id"),
                                       ("auth_failure", "node:mme_id"), ("dns_flap", None),
                                       ("external_server", None)])
def test_rotate_group_by_kind(kind, want):
    assert faults._rotate_group(kind) == want


def test_target_key_is_order_independent_and_distinguishes_sets():
    assert faults.target_key(["a", "b"]) == faults.target_key(["b", "a"])
    assert faults.target_key(["a", "b"]) != faults.target_key(["a", "c"])


# ---------------------------------------------------------------- 시군구 순환

def test_sgg_of_shape_avoids_a_used_sgg_and_cycles_through_all():
    cells = _city(4)
    keys = {f"S{i}": faults.target_key(faults._sgg_all(cells, f"S{i}")) for i in range(4)}
    used, seen = {}, []
    for fid in range(1, 5):
        pick = faults._sgg_of_shape("ran_radio", cells, random.Random(fid), used)
        seen.append(pick)
        used[keys[pick]] = fid
    assert len(set(seen)) == 4, "후보 4곳을 한 바퀴"
    assert all(a != b for a, b in zip(seen, seen[1:])), "연속 중복 없음"


def test_sgg_of_shape_after_a_full_round_restarts_from_the_oldest():
    cells = _city(3)
    keys = {f"S{i}": faults.target_key(faults._sgg_all(cells, f"S{i}")) for i in range(3)}
    used = {keys["S0"]: 7, keys["S1"]: 3, keys["S2"]: 5}
    assert faults._sgg_of_shape("ran_radio", cells, random.Random(0), used) == "S1"


def test_sgg_of_shape_without_used_is_unchanged():
    cells = _city(4)
    assert {faults._sgg_of_shape("ran_radio", cells, random.Random(s)) for s in range(40)} == {"S0", "S1", "S2", "S3"}


# ---------------------------------------------------------------- 노선 구간 순환

def test_route_target_draws_again_to_find_a_segment_not_used(monkeypatch):
    cells = [_cell(f"r{i}-0", LAT0, LON0 + i * 0.7 / KM_LON) for i in range(60)]
    segs = [cells[0:15], cells[15:30], cells[30:45]]
    calls = iter(range(100))
    monkeypatch.setattr(S, "route_segment", lambda routes, rng, *a, **k: (segs[next(calls) % 3], {"i": 1}))
    monkeypatch.setattr(S, "geo_shape", lambda seg: "CORRIDOR")
    used = {faults.target_key(c.cell_key for c in segs[0]): 4, faults.target_key(c.cell_key for c in segs[1]): 5}
    t = faults._route_target(["L"], random.Random(0), "mobility", used)
    assert t.cell_keys == {c.cell_key for c in segs[2]}, "앞선 회차가 안 쓴 구간"


def test_route_target_reuses_the_oldest_when_every_drawn_segment_was_used(monkeypatch):
    cells = [_cell(f"r{i}-0", LAT0, LON0 + i * 0.7 / KM_LON) for i in range(30)]
    segs = [cells[0:15], cells[15:30]]
    calls = iter(range(100))
    monkeypatch.setattr(S, "route_segment", lambda routes, rng, *a, **k: (segs[next(calls) % 2], {"i": 1}))
    monkeypatch.setattr(S, "geo_shape", lambda seg: "CORRIDOR")
    monkeypatch.setattr(faults, "ROUTE_ROTATE_DRAWS", 6)
    used = {faults.target_key(c.cell_key for c in segs[0]): 9, faults.target_key(c.cell_key for c in segs[1]): 2}
    t = faults._route_target(["L"], random.Random(0), "mobility", used)
    assert t.cell_keys == {c.cell_key for c in segs[1]}


def test_route_target_without_used_takes_the_first_draw(monkeypatch):
    cells = [_cell(f"r{i}-0", LAT0, LON0 + i * 0.7 / KM_LON) for i in range(15)]
    n = {"calls": 0}

    def seg(routes, rng, *a, **k):
        n["calls"] += 1
        return cells, {"i": 1}

    monkeypatch.setattr(S, "route_segment", seg)
    monkeypatch.setattr(S, "geo_shape", lambda s: "CORRIDOR")
    assert faults._route_target(["L"], random.Random(0), "mobility", None) is not None
    assert n["calls"] == 1, "기록이 없으면 지금처럼 한 번만 뽑는다"


# ---------------------------------------------------------------- 재해 지역 순환

def _groups(n_groups, per=5):
    return [_cell(f"{g:02d}{k}-0", LAT0 + g * 0.5, LON0 + k * 0.1 / KM_LON, enb=f"{g:02d}{k}")
            for g in range(n_groups) for k in range(per)]


def _patch_disaster(monkeypatch, lam):
    monkeypatch.setattr(S, "geo_shape", lambda picked: "CLUSTER")
    monkeypatch.setattr(S, "disaster_rank_of", lambda keys, picked, mode, sid: {f"d{picked[0].enb_id[:2]}": 0})
    monkeypatch.setattr(S, "disaster_dead_n", lambda n: n)
    return {f"d{g:02d}": v for g, v in lam.items()}


def _gkeys(cells, g):
    return {c.cell_key for c in cells if c.enb_id.startswith(f"{g:02d}")}


def test_busy_disaster_area_skips_the_best_area_when_it_was_used(monkeypatch):
    cells = _groups(4)
    rate = {c.enb_id: 100.0 - int(c.enb_id[:2]) for c in cells}
    rate.update(_patch_disaster(monkeypatch, {0: 5.0, 1: 9.0, 2: 7.0, 3: 1.0}))
    assert faults._busy_disaster_area(cells, rate, 5, "local", "F-1") == _gkeys(cells, 1)
    used = {faults.target_key(_gkeys(cells, 1)): 3}
    assert faults._busy_disaster_area(cells, rate, 5, "local", "F-2", used=used) == _gkeys(cells, 2), "점수 순서를 지키되 안 쓴 첫 지역"


def test_busy_disaster_area_reuses_the_oldest_when_all_used(monkeypatch):
    cells = _groups(3)
    rate = {c.enb_id: 100.0 for c in cells}
    rate.update(_patch_disaster(monkeypatch, {0: 5.0, 1: 9.0, 2: 7.0}))
    used = {faults.target_key(_gkeys(cells, g)): n for g, n in {0: 8, 1: 6, 2: 7}.items()}
    assert faults._busy_disaster_area(cells, rate, 5, "local", "F-9", used=used) == _gkeys(cells, 1)


def test_disaster_area_redraws_past_a_used_area(monkeypatch):
    cells = _groups(6)
    monkeypatch.setattr(S, "geo_shape", lambda picked: "CLUSTER")
    first = faults._disaster_area(cells, random.Random(3), 5)
    used = {faults.target_key(first): 1}
    second = faults._disaster_area(cells, random.Random(3), 5, used)
    assert second != first


# ---------------------------------------------------------------- build — 기록과 결정론

def _build_row(kind, fid, target=None):
    return {"fault_id": fid, "kind": kind, "target": target, "start_ts": T0 + timedelta(minutes=2),
            "duration_min": 11, "intensity": 1.0}


def _build(cells, kind, fid, history, target=None, seed=7):
    return faults.build(_build_row(kind, fid, target), cells, T0, faults.target_rng(seed, fid), seed=seed,
                        history=history)


def test_build_records_automatic_targets_only(tmp_path):
    cells = _city(4)
    h = faults.TargetHistory(tmp_path / "t.json")
    sc = _build(cells, "ran_radio", 1, h)
    assert sc is not None
    rec = json.loads((tmp_path / "t.json").read_text())
    assert rec["1:sgg"]["group"] == "sgg" and rec["1:sgg"]["key"] == faults.target_key(sc.target.cell_keys)


def test_build_does_not_record_or_rotate_a_specified_target(tmp_path):
    cells = _city(4)
    h = faults.TargetHistory(tmp_path / "t.json")
    _build(cells, "ran_radio", 1, h, target="SGG:S2")
    _build(cells, "ran_radio", 2, h, target="LIKE:1")
    assert not (tmp_path / "t.json").exists() or json.loads((tmp_path / "t.json").read_text()) == {}


def test_build_consecutive_faults_do_not_repeat_the_target(tmp_path):
    cells = _city(5)
    h = faults.TargetHistory(tmp_path / "t.json")
    seen = []
    for fid in range(1, 11):
        seen.append(frozenset(_build(cells, "ran_radio", fid, h).target.cell_keys))
    assert all(a != b for a, b in zip(seen, seen[1:])), "연속 10회 중복 없음"
    assert len(set(seen)) == 5, "후보 5곳을 돌려 쓴다"


def test_build_same_fault_is_deterministic_across_ticks_and_restarts(tmp_path):
    cells = _city(5)
    h = faults.TargetHistory(tmp_path / "t.json")
    for fid in range(1, 4):
        _build(cells, "ran_radio", fid, h)
    a = _build(cells, "ran_radio", 3, faults.TargetHistory(tmp_path / "t.json")).target.cell_keys
    b = _build(cells, "ran_radio", 3, faults.TargetHistory(tmp_path / "t.json")).target.cell_keys
    assert a == b, "같은 회차는 자기 이전 기록만 봐서 틱마다 같은 대상이다"


def test_build_without_history_behaves_as_before():
    cells = _city(4)
    assert _build(cells, "ran_radio", 1, None) is not None


def test_non_rotating_kinds_are_not_recorded(tmp_path):
    cells = _city(4)
    h = faults.TargetHistory(tmp_path / "t.json")
    _build(cells, "dns_flap", 1, h)
    assert not (tmp_path / "t.json").exists() or json.loads((tmp_path / "t.json").read_text()) == {}
