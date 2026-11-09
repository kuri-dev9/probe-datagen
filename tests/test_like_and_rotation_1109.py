"""15번 `LIKE:` 재생 결함 수정 · 노드 자동 대상 시나리오 몫 순환 (2026-11-09 · 맥스 수정 고정).

* `control._check_like` — 저장할 대상 문자열을 돌려준다. 15번(범위 kind): 빈 대상 → `ROUTE%:50`, 원본이 `ROUTE%:N` 이면
  `ROUTE%:N:LIKE:<원본>`, 범위 도입 전 원본(빈 대상) → `LIKE:<원본>`. 틀린 조합은 거부. 다른 kind 는 예전 그대로.
* `faults.build` — 재생은 원본 번호의 시드와 순환 기록으로 같은 셀을 고른다(범위 배치도 · 범위 도입 전 연속 구간도).
* 노드 자동 대상 — `kind:<kind>` 몫 기록이 쌓이고, 그 시나리오가 앞서 쓴 노드는 다른 시나리오가 쓴 노드보다 뒤로 밀린다
  (20번 F-209·F-226 이 둘 다 SGW-6). 후보가 하나면 그대로. DB 없이 — 합성 셀·가짜 커서·임시 파일.
"""
from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_route_range_1109 import _ring  # noqa: E402
from test_route_segment import T0, _cell, _grid_cells  # noqa: E402

from datagen import control, faults  # noqa: E402
from datagen import scenarios as S  # noqa: E402


# ── 1. _check_like — 가짜 커서 ───────────────────────────────────────────────

ROWS = {9: ("ho_interruption", "ROUTE%:70"), 10: ("ho_interruption", None), 11: ("ho_interruption", "ENB:1"),
        12: ("ho_interruption", "ROUTE%:50:ENB:1234"), 20: ("mobility", None), 21: ("mobility", "ENB:2")}


class _Cur:
    def __init__(self):
        self.row = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params):
        self.row = ROWS.get(int(params[0]))

    def fetchone(self):
        return self.row


class _Conn:
    def cursor(self):
        return _Cur()


@pytest.mark.parametrize("kind,target,want", [
    ("ho_interruption", None, "ROUTE%:50"),
    ("ho_interruption", "", "ROUTE%:50"),
    ("ho_interruption", "ROUTE%:30", "ROUTE%:30"),
    ("ho_interruption", "ROUTE%:999", "ROUTE%:100"),
    ("ho_interruption", "ENB:123", "ENB:123"),
    ("ho_interruption", "LIKE:9", "ROUTE%:70:LIKE:9"),            # 범위 원본 — 범위를 대상 문자열에 싣는다
    ("ho_interruption", "ROUTE%:70:LIKE:9", "ROUTE%:70:LIKE:9"),   # 같은 범위를 함께 주면 그대로
    ("ho_interruption", "LIKE:10", "LIKE:10"),                     # 범위 도입 전 원본(빈 대상) — 그대로
    ("mobility", "LIKE:20", "LIKE:20"),                            # 다른 kind 는 예전과 같다
    ("mobility", None, None),
    ("mobility", "ENB:7", "ENB:7"),
])
def test_check_like_normalizes_what_gets_stored(kind, target, want):
    assert control._check_like(_Conn(), kind, target) == want


@pytest.mark.parametrize("kind,target,msg", [
    ("ho_interruption", "ROUTE%:50:LIKE:9", "70%"),               # 원본과 다른 범위
    ("ho_interruption", "ROUTE%:50:LIKE:10", "범위 도입 전"),      # 범위 도입 전 원본에 범위를 줌
    ("ho_interruption", "LIKE:11", "지정"),                        # 원본이 eNB 지정
    ("ho_interruption", "LIKE:12", "지정"),                        # 원본이 범위 + eNB
    ("ho_interruption", "LIKE:99", "없다"),
    ("ho_interruption", "LIKE:20", "kind"),
    ("ho_interruption", "LIKE:x", "형식"),
    ("mobility", "LIKE:21", "지정"),
])
def test_check_like_rejects_wrong_combinations(kind, target, msg):
    with pytest.raises(ValueError, match=msg):
        control._check_like(_Conn(), kind, target)


def test_add_fault_stores_the_normalized_target(monkeypatch):
    got = {}

    class _C:
        lastrowid = 77

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params):
            if sql.startswith("INSERT"):
                got["params"] = params
                return
            self.row = ROWS.get(int(params[0]))

        def fetchone(self):
            return self.row

    class _Cn:
        def cursor(self):
            return _C()

        def commit(self):
            got["commit"] = True

    fid = control.add_fault(_Cn(), "ho_interruption", "LIKE:9", T0)
    assert fid == 77 and got["params"][1] == "ROUTE%:70:LIKE:9" and got["commit"]
    control.add_fault(_Cn(), "ho_interruption", None, T0)
    assert got["params"][1] == "ROUTE%:50"


# ── 2. 재생이 원본과 같은 셀을 고른다 ────────────────────────────────────────

def _built(kind, target, fid, seed=5, routes=None, history=None, intensity=1.0):
    row = {"fault_id": fid, "kind": kind, "target": target, "start_ts": T0 + timedelta(minutes=2),
           "duration_min": 30, "intensity": intensity}
    return faults.build(row, _grid_cells(), T0, faults.target_rng(seed, fid), seed=seed, routes=routes, history=history)


@pytest.mark.parametrize("pct", [10, 50, 100])
def test_like_replay_of_a_range_run_reproduces_cells_and_pairs(pct):
    rt = [_ring()]
    orig = _built("ho_interruption", f"ROUTE%:{pct}", 9, routes=rt)
    like = _built("ho_interruption", f"ROUTE%:{pct}:LIKE:9", 30, routes=rt)
    assert like.target.cell_keys == orig.target.cell_keys and like.target.cell_pairs == orig.target.cell_pairs
    assert like.as_truth()["range_pct"] == pct


def test_like_replay_of_a_pre_range_run_is_the_old_continuous_segment():
    rt = [_ring()]
    orig = _built("ho_interruption", None, 9, routes=rt)                    # 옛 회차 — 대상이 비어 있고 범위 배치
    like_pre = _built("ho_interruption", "LIKE:9", 30, routes=rt)
    # 범위 도입 전 해석은 예전 연속 구간(`_route_target`)이다 — 정답지에 `range_pct` 가 없다
    t = like_pre.as_truth()
    assert "range_pct" not in t and t["route_seg"]
    keys = [c.cell_key for c in rt[0].cells]
    cells = sorted(keys.index(k) for k in like_pre.target.cell_keys)
    assert len(cells) >= S.ROUTE_SEG_CELLS and orig is not None


def test_like_replay_uses_the_original_fault_seed_not_its_own():
    rt = [_ring()]
    a = _built("ho_interruption", "ROUTE%:50:LIKE:9", 30, routes=rt)
    b = _built("ho_interruption", "ROUTE%:50:LIKE:9", 31, routes=rt)       # 재생 번호가 달라도 원본이 같으면 같은 셀
    assert a.target.cell_keys == b.target.cell_keys


def test_like_replay_follows_the_original_rotation_history(tmp_path):
    rt = [_ring()]
    h = faults.TargetHistory(tmp_path / "t.json")
    h.record(3, "route", faults.target_key({"x-0"}))                         # 앞선 회차가 쓴 구간 — 순환이 비켜 가게 한다
    orig = _built("ho_interruption", "ROUTE%:50", 9, routes=rt, history=h)
    like = _built("ho_interruption", "ROUTE%:50:LIKE:9", 30, routes=rt, history=h)
    assert like.target.cell_keys == orig.target.cell_keys


# ── 3. 노드 자동 대상 시나리오 몫 순환 ───────────────────────────────────────

def _sgw_cells(per=6, n_sgw=5):
    out = []
    for s in range(1, n_sgw + 1):
        for i in range(per):
            c = _cell(f"{800000 + s * 100 + i}-0", 35.6 + s * 0.01, 139.6 + i * 0.01, sgw=f"{s:04d}")
            out.append(c)
    return out


def _node_run(kind, fid, cells, history, seed=3):
    row = {"fault_id": fid, "kind": kind, "target": None, "start_ts": T0 + timedelta(minutes=2),
           "duration_min": 30, "intensity": 1.0}
    sc = faults.build(row, cells, T0, faults.target_rng(seed, fid), seed=seed, history=history)
    return sc.target.sgw_id


def test_node_kinds_record_a_kind_slot_next_to_the_group_slot(tmp_path):
    h = faults.TargetHistory(tmp_path / "t.json")
    _node_run("gw_resource", 4, _sgw_cells(), h)
    keys = set(__import__("json").loads((tmp_path / "t.json").read_text()))
    assert {"4:node:sgw_id", "4:kind:gw_resource"} <= keys


def test_non_node_kinds_record_no_kind_slot(tmp_path):
    h = faults.TargetHistory(tmp_path / "t.json")
    _built("ho_interruption", None, 4, routes=[_ring()], history=h)
    assert not any(":kind:" in k for k in __import__("json").loads((tmp_path / "t.json").read_text()))


def test_own_node_is_pushed_behind_nodes_other_scenarios_used(tmp_path):
    cells = _sgw_cells()
    key = lambda s: faults.target_key(c.cell_key for c in cells if c.sgw_id == f"{s:04d}")   # noqa: E731
    h = faults.TargetHistory(tmp_path / "t.json")
    # 20번(ims_pdn)이 SGW 1 을 가장 먼저 썼고, 다른 시나리오가 나머지 넷을 나중에 썼다 — 예전 규칙은 가장 오래전에
    # 쓴 SGW 1 로 돌아온다
    h.record(1, "node:sgw_id", key(1))
    h.record(1, "kind:ims_pdn", key(1))
    for fid, s in ((2, 2), (3, 3), (4, 4), (5, 5)):
        h.record(fid, "node:sgw_id", key(s))
    picked = _node_run("ims_pdn", 6, cells, h)
    assert picked != "0001" and picked in {"0002", "0003", "0004", "0005"}


def test_with_a_single_candidate_the_node_is_still_chosen(tmp_path):
    cells = _sgw_cells(n_sgw=1)
    h = faults.TargetHistory(tmp_path / "t.json")
    assert _node_run("gw_resource", 1, cells, h) == "0001"
    assert _node_run("gw_resource", 2, cells, h) == "0001"          # 후보가 하나뿐이면 같은 노드


def test_without_history_or_own_records_nothing_changes():
    cells = _sgw_cells()
    assert _node_run("gw_resource", 1, cells, None) == _node_run("gw_resource", 1, cells, None)


def test_offset_constant_exceeds_any_fault_number():
    assert faults.OWN_ROTATE_OFFSET > 10 ** 6
