"""15번 강도 → 범위(%) (2026-11-09 · 사용자 결정 · 맥스 구현) — `route_range` · `route_spread` · `route_range_of` · `faults.build`.

강도는 224건 모두 1.0 이었고 쓰인 적이 없다. 15번만 강도 대신 **범위(%)** — 노선 셀 중 몇 %에 넣나 — 로 바꾼다.
기본 50 · 하한 10 · 상한 100, 100 은 한 바퀴 전체, 미만은 역 1~2개 조각을 **사이에 최소 한 역을 비우고** 흩어 놓는다.
세션별 세기는 1.0 값으로 고정(`peak`), 정답지 `expect_geo` 는 고른 셀 전체의 실제 모양. 저장은 DDL 없이 `ROUTE%:50`.
합성 노선만 쓴다 — DB 없음.
"""
from __future__ import annotations

import math
import random
import sys
from datetime import timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_route_segment import T0, _build, _cell, _grid_cells, _line_route  # noqa: E402,F401

from datagen import faults  # noqa: E402
from datagen import handover as HO  # noqa: E402
from datagen import scenarios as S  # noqa: E402

UI = Path(__file__).resolve().parent.parent / "src" / "datagen" / "ui.py"


def _ring(n=90, stations=30, rid="YMT"):
    """야마노테선 모양 순환선 — 역 30개 · 칸 90개 · 칸마다 셀 하나."""
    cells = []
    for i in range(n):
        a = 2 * math.pi * i / n
        cells.append(_cell(f"{720000 + i}-0", 35.68 + 4.0 * math.sin(a) / 111.0, 139.70 + 4.0 * math.cos(a) / 90.5,
                           sgw=f"{(i // 10) % 3 + 1:04d}"))
    per = n // stations
    return HO.Route(rid, rid, tuple(cells), tuple([60] * n), loop=True,
                    station_of=tuple(f"S{i // per}" for i in range(n)),
                    stations={f"S{i}": f"역{i}" for i in range(stations)})


# ── route_range ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,want", [
    (50, 50), ("70", 70), (100, 100), (10, 10), (0, 10), (5, 10), (-3, 10), (250, 100), (99.6, 100),
    (None, 50), ("", 50), ("abc", 50), ([], 50),
])
def test_route_range_clamps_and_defaults(raw, want):
    assert S.route_range(raw) == want


def test_range_constants():
    assert (S.ROUTE_RANGE_DEFAULT, S.ROUTE_RANGE_MIN, S.ROUTE_RANGE_MAX) == (50, 10, 100)
    assert (S.ROUTE_PIECE_MIN, S.ROUTE_PIECE_MAX) == (3, 6)


# ── route_spread ─────────────────────────────────────────────────────────────

def test_spread_without_routes_is_none():
    assert S.route_spread([], random.Random(1)) is None
    assert S.route_spread(None, random.Random(1)) is None


def test_hundred_percent_is_the_whole_loop_in_one_piece_with_the_closing_pair():
    rt = _ring()
    cells, pairs, info = S.route_spread([rt], random.Random(1), 100)
    assert info["whole"] is True and len(info["pieces"]) == 1
    assert {c.cell_key for c in cells} == {c.cell_key for c in rt.cells}
    assert len(pairs) == len(cells)                         # 순환이라 끝→처음 쌍이 하나 더 있다
    assert (cells[-1].cell_key, cells[0].cell_key) in pairs
    assert info["range_pct"] == 100


@pytest.mark.parametrize("pct", [10, 30, 50, 70])
@pytest.mark.parametrize("seed", range(12))
def test_under_hundred_pieces_are_separated_by_at_least_one_station(pct, seed):
    rt = _ring()
    cells, _, info = S.route_spread([rt], random.Random(seed), pct)
    assert info["whole"] is False and len(info["pieces"]) >= 2
    have = {(int(c.cell_key.split("-")[0]) - 720000) // 3 for c in cells}      # 역 번호(칸 3개 = 한 역)
    # 연속한 역 묶음(순환)의 수 = 조각 수 — 서로 다른 묶음 사이에는 빈 역이 끼어 있다
    runs = sum(1 for s in have if (s - 1) % 30 not in have)
    assert runs == len(info["pieces"])


@pytest.mark.parametrize("pct", [10, 30, 50, 70, 90])
@pytest.mark.parametrize("seed", range(8))
def test_cell_count_meets_the_target_and_cells_do_not_overlap(pct, seed):
    rt = _ring()
    cells, pairs, info = S.route_spread([rt], random.Random(seed), pct)
    want = max(S.ROUTE_SEG_CELLS, round(90 * pct / 100))
    keys = [c.cell_key for c in cells]
    assert len(keys) == len(set(keys))                      # 같은 셀 두 번 없음
    assert len(keys) >= want and info["cells"] == len(keys)
    assert all(a in keys and b in keys for a, b in pairs)
    assert info["route_cells"] == 90 and info["range_pct"] == pct


def test_ten_percent_has_the_fifteen_cell_floor():
    _, _, info = S.route_spread([_ring()], random.Random(3), 10)
    assert info["cells"] >= S.ROUTE_SEG_CELLS


def test_pairs_are_neighbours_inside_a_piece_never_across_pieces():
    rt = _ring()
    keys = [c.cell_key for c in rt.cells]
    cells, pairs, info = S.route_spread([rt], random.Random(5), 50)
    for a, b in pairs:
        assert (keys.index(b) - keys.index(a)) % 90 == 1
    assert len(pairs) == len(cells) - len(info["pieces"])   # 조각마다 쌍 = 셀 − 1


def test_duplicate_route_cells_are_used_once():
    rt = _ring()
    dup = HO.Route("D", "D", rt.cells + rt.cells[:6], tuple([60] * 96), loop=True,
                   station_of=rt.station_of + rt.station_of[:6], stations=rt.stations)
    cells, _, info = S.route_spread([dup], random.Random(2), 100)
    assert len(cells) == len({c.cell_key for c in cells}) == 90 and info["route_cells"] == 90


@pytest.mark.parametrize("pct", [10, 50, 100])
def test_same_seed_same_result(pct):
    a = S.route_spread([_ring()], random.Random(11), pct)
    b = S.route_spread([_ring()], random.Random(11), pct)
    assert [c.cell_key for c in a[0]] == [c.cell_key for c in b[0]] and a[1] == b[1]
    assert a[2] == b[2]


def test_different_seeds_pick_different_places():
    picks = {tuple(sorted(c.cell_key for c in S.route_spread([_ring()], random.Random(s), 50)[0])) for s in range(6)}
    assert len(picks) > 1


def test_info_carries_the_real_shape_of_the_picked_cells():
    cells, _, info = S.route_spread([_ring()], random.Random(1), 100)
    assert info["shape"] == S.geo_shape(cells)
    assert info["shape"] != "CORRIDOR"                      # 고리는 길쭉하지 않다
    assert sum(p["cells"] for p in info["pieces"]) == info["cells"]


# ── route_range_of ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,want", [
    ("ROUTE%:50", (50, None)),
    ("ROUTE%:50:ENB:1234", (50, "ENB:1234")),
    ("route%:70", (70, None)),
    ("ROUTE%:999", (100, None)),
    ("ROUTE%:x", (50, None)),
    ("ENB:1234", (None, "ENB:1234")),
    ("", (None, None)),
    (None, (None, None)),
])
def test_route_range_of(raw, want):
    assert faults.route_range_of(raw) == want


# ── faults.build ─────────────────────────────────────────────────────────────

def _built(kind, target=None, intensity=1.0, routes=None, seed=7, fid=1):
    row = {"fault_id": fid, "kind": kind, "target": target, "start_ts": T0 + timedelta(minutes=2),
           "duration_min": 30, "intensity": intensity}
    return faults.build(row, _grid_cells(), T0, faults.target_rng(seed, fid), seed=seed, routes=routes)


def _peak(sc):
    return getattr(sc, "peak", None)


def test_ho_interruption_peak_is_pinned_to_one_whatever_the_intensity():
    rt = _ring()
    assert _peak(_built("ho_interruption", intensity=0.4, routes=[rt])) == 1.0
    assert _peak(_built("ho_interruption", intensity=1.0, routes=[rt])) == 1.0


def test_other_kinds_keep_their_intensity():
    assert _peak(_built("mobility", intensity=0.4, routes=[_line_route()])) == 0.4


@pytest.mark.parametrize("target,pct", [("ROUTE%:100", 100), ("ROUTE%:50", 50), ("ROUTE%:10", 10), (None, 50)])
def test_empty_target_and_prefix_pick_a_range_target(target, pct):
    sc = _built("ho_interruption", target=target, routes=[_ring()])
    assert sc is not None and sc.target.route_seg["range_pct"] == pct
    assert len(sc.target.cell_keys) == sc.target.route_seg["cells"]


def test_truth_uses_the_real_shape_and_records_the_range():
    t = _built("ho_interruption", target="ROUTE%:100", routes=[_ring()]).as_truth()
    assert t["range_pct"] == 100 and t["expect_geo"] == t["route_seg"]["shape"] != "CORRIDOR"
    assert t["cell_pairs"]


def test_enb_target_keeps_the_old_corridor_truth():
    enb = _grid_cells()[0].enb_id
    t = _built("ho_interruption", target=f"ENB:{enb}", routes=[_ring()]).as_truth()
    assert "range_pct" not in t


# ── 화면 코드 ────────────────────────────────────────────────────────────────

def test_ui_hides_intensity_and_shows_range_only_for_range_kinds():
    src = UI.read_text()
    assert 'const RANGE_KINDS = new Set(["ho_interruption"]);' in src
    assert '$("f-int").style.display = on ? "none" : "";' in src
    assert '$("f-rng").style.display = on ? "" : "none";' in src
    assert "`ROUTE%:${parseInt($(\"range\").value, 10) || 50}`" in src
    assert 'intensity: ranged ? 1.0 : parseFloat($("intensity").value)' in src
    assert tuple(faults.ROUTE_RANGE_KINDS) == ("ho_interruption",)


def test_scenario_screen_shows_range_instead_of_intensity_when_present():
    assert "u.range_pct!=null ? ['범위',esc(u.range_pct+'%')] : ['강도',val(u.intensity)]" in UI.read_text()


def test_like_replay_of_a_range_run_reproduces_the_original_cells():
    """저장되는 대상은 `ROUTE%:N:LIKE:<원본>` 이다(`control._check_like`) — 원본의 범위를 싣는다."""
    rt = _ring()
    orig = _built("ho_interruption", target="ROUTE%:50", routes=[rt], seed=5, fid=9)
    row = {"fault_id": 30, "kind": "ho_interruption", "target": "ROUTE%:50:LIKE:9", "start_ts": T0 + timedelta(minutes=2),
           "duration_min": 30, "intensity": 1.0}
    like = faults.build(row, _grid_cells(), T0, faults.target_rng(5, 30), seed=5, routes=[rt])
    assert like.target.cell_keys == orig.target.cell_keys
