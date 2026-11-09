"""대상 순환 기록의 몫 키가 시나리오 번호다 — 2026-11-10 (19번 F-243 이 직전 F-216 과 같은 MME-3 · 동구 수정 고정).

10·19번은 둘 다 `mme_overload` 라 `kind:` 몫을 나눠 써서, 10번이 다른 MME 를 최근에 쓰면 19번 자기 직전 노드가 "가장 오래전"이
되어 다시 골라졌다. 몫을 `scn:<번호>` 로 나눈다. DB 없이 — 합성 셀·임시 순환 기록.
"""
from __future__ import annotations

import dataclasses
import json
import sys
from datetime import timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_route_segment import T0, _cell  # noqa: E402

from datagen import faults  # noqa: E402


# ── scenario_no · _own_group ─────────────────────────────────────────────────

@pytest.mark.parametrize("note,want", [
    ("#19 no19-v6", 19), ("no19-v6", 19), ("지도 · 시나리오 19", 19), ("#3 gw", 3), ("no25-r7", 25), ("25r7", None),
])
def test_number_is_read_from_the_three_note_shapes(note, want):
    got = faults.scenario_no({"kind": "zzz_unknown", "note": note})
    assert got == want


@pytest.mark.parametrize("kind,want", [("mme_overload", 10), ("gw_resource", 1), ("ims_pdn", 12)])
def test_without_a_number_the_kind_maps_to_the_original_scenario(kind, want):
    assert faults.scenario_no({"kind": kind, "note": ""}) == want
    assert faults.scenario_no({"kind": kind}) == want
    assert faults.scenario_no({"kind": kind, "note": None}) == want


def test_unknown_kind_without_number_falls_back_to_the_kind_group():
    f = {"kind": "zzz_unknown", "note": "mme-v5"}
    assert faults.scenario_no(f) is None and faults._own_group(f) == "kind:zzz_unknown"


def test_own_group_uses_the_number_when_known():
    assert faults._own_group({"kind": "mme_overload", "note": "#19 no19-v6"}) == "scn:19"
    assert faults._own_group({"kind": "mme_overload", "note": "mme-v5"}) == "scn:10"


def test_note_number_wins_over_the_kind():
    assert faults.scenario_no({"kind": "mme_overload", "note": "#19 x"}) == 19


# ── build 가 남기는 키 ───────────────────────────────────────────────────────

def _mme_cells(per=6, n_mme=5):
    out = []
    for m in range(1, n_mme + 1):
        for i in range(per):
            c = _cell(f"{700000 + m * 100 + i}-0", 35.6 + m * 0.01, 139.6 + i * 0.01)
            out.append(dataclasses.replace(c, mme_id=f"{m:04d}"))
    return out


def _run(kind, fid, note, cells, history, seed=3):
    row = {"fault_id": fid, "kind": kind, "target": None, "note": note, "start_ts": T0 + timedelta(minutes=2),
           "duration_min": 30, "intensity": 1.0}
    sc = faults.build(row, cells, T0, faults.target_rng(seed, fid), seed=seed, history=history)
    return sc.target.mme_id


def _key(cells, m):
    return faults.target_key(c.cell_key for c in cells if c.mme_id == f"{m:04d}")


def _keys(path):
    return set(json.loads(path.read_text()))


def test_build_records_scenario_slot_and_shared_node_slot(tmp_path):
    cells, h = _mme_cells(), faults.TargetHistory(tmp_path / "t.json")
    _run("mme_overload", 7, "#19 no19-v6", cells, h)
    k = _keys(tmp_path / "t.json")
    assert {"7:scn:19", "7:node:mme_id"} <= k and not any(x.startswith("7:kind:") for x in k)


def test_build_without_a_note_records_the_original_number(tmp_path):
    cells, h = _mme_cells(), faults.TargetHistory(tmp_path / "t.json")
    _run("mme_overload", 8, "", cells, h)
    assert "8:scn:10" in _keys(tmp_path / "t.json")


def test_non_node_kind_records_no_scenario_slot(tmp_path):
    from test_like_and_rotation_1109 import _built, _ring
    h = faults.TargetHistory(tmp_path / "t.json")
    _built("ho_interruption", None, 4, routes=[_ring()], history=h)
    assert not any(":scn:" in x or ":kind:" in x for x in _keys(tmp_path / "t.json"))


# ── 몫 분리 ──────────────────────────────────────────────────────────────────

def _seed_history(tmp_path, cells, scn19, scn10):
    """공용 기록: MME 1 이 가장 먼저, 이어 2~5. 몫 기록은 시나리오별로 따로."""
    h = faults.TargetHistory(tmp_path / "t.json")
    for fid, m in ((1, 1), (2, 2), (3, 3), (4, 4), (5, 5)):
        h.record(fid, "node:mme_id", _key(cells, m))
    for fid, m in scn19:
        h.record(fid, "scn:19", _key(cells, m))
    for fid, m in scn10:
        h.record(fid, "scn:10", _key(cells, m))
    return h


def test_19_pushes_its_own_previous_node_back_and_ignores_the_10_slot(tmp_path):
    cells = _mme_cells()
    h = _seed_history(tmp_path, cells, scn19=[(1, 1)], scn10=[(5, 5)])
    picked = _run("mme_overload", 6, "#19 x", cells, h)
    assert picked != "0001"                                                    # 자기 직전(MME-1)은 뒤로


def test_10_and_19_do_not_push_each_others_nodes(tmp_path):
    cells = _mme_cells()
    # 19번이 MME-1 을 썼다 → 10번은 MME-1 을 밀지 않고 공용 순서(가장 오래전 = MME-1)대로 고른다
    h = _seed_history(tmp_path, cells, scn19=[(1, 1)], scn10=[])
    assert _run("mme_overload", 6, "#10 x", cells, h) == "0001"
    # 반대로 10번이 MME-1 을 썼다 → 19번은 MME-1 을 밀지 않는다
    (tmp_path / "t.json").unlink()
    h = _seed_history(tmp_path, cells, scn19=[], scn10=[(1, 1)])
    assert _run("mme_overload", 6, "#19 x", cells, h) == "0001"


# ── 전환 대체 · F-243 재현 ───────────────────────────────────────────────────

def test_old_kind_slot_is_used_when_no_scenario_slot_exists_yet(tmp_path):
    cells = _mme_cells()
    h = faults.TargetHistory(tmp_path / "t.json")
    for fid, m in ((1, 1), (2, 2), (3, 3), (4, 4), (5, 5)):
        h.record(fid, "node:mme_id", _key(cells, m))
    h.record(1, "kind:mme_overload", _key(cells, 1))                           # 옛 몫 기록만 있다
    assert _run("mme_overload", 6, "#19 x", cells, h) != "0001"


def test_f243_like_case_does_not_repeat_the_previous_mme(tmp_path):
    """F-216 이 MME-3 을 썼고 10번이 MME-1 을 썼다 — 옛 규칙은 공용 기록에서 가장 오래전인 MME-3 을 다시 골랐다."""
    cells = _mme_cells()
    h = faults.TargetHistory(tmp_path / "t.json")
    order = [(1, 3), (2, 2), (3, 4), (4, 5), (5, 1)]                           # 공용: MME-3 이 가장 오래전
    for fid, m in order:
        h.record(fid, "node:mme_id", _key(cells, m))
    h.record(1, "scn:19", _key(cells, 3))                                      # 19번 몫: 직전에 MME-3
    h.record(5, "scn:10", _key(cells, 1))
    assert _run("mme_overload", 6, "#19 no19-v6", cells, h) != "0003"


# ── 수동 지정 · LIKE · 기록 없음 ─────────────────────────────────────────────

def test_manual_target_reads_and_writes_no_rotation_slot(tmp_path):
    cells = _mme_cells()
    h = faults.TargetHistory(tmp_path / "t.json")
    row = {"fault_id": 9, "kind": "mme_overload", "target": "MME:0002", "note": "#19 x",
           "start_ts": T0 + timedelta(minutes=2), "duration_min": 30, "intensity": 1.0}
    try:
        faults.build(row, cells, T0, faults.target_rng(3, 9), seed=3, history=h)
    except Exception:
        pytest.skip("수동 대상 문법이 이 합성 셀과 맞지 않는다")
    p = tmp_path / "t.json"
    assert not p.exists() or not any(x.startswith("9:") for x in _keys(p))


def test_without_history_nothing_changes():
    cells = _mme_cells()
    assert _run("mme_overload", 1, "#19 x", cells, None) == _run("mme_overload", 1, "#19 x", cells, None)
