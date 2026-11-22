"""24번 A안(11-17 사용자 「너무 길다」로 붐빔 20분 · 총 30분 · 11-15 판은 30/50) · 판정용 정답지 `write_unscored_truth` · `_live_truth` · `tools/inject.py` 가상 시각 — 2026-11-15 (F-343).

F-343: 칸 조각(Q-11)이 00:45(정점 00:39 뒤 5분 버킷 3칸)에 서고 B 는 00:50 에 열려 붐빔을 보일 시간이 5분뿐이었다.
그래서 `SHIFT_RISE` 20→30 · `SHIFT_TOTAL` 40→50. 14번(`_congest`)·27번(`_ran_congestion`)은 바뀌면 안 된다.
DB 에는 쓰지 않는다(`test_capacity_shift.py` 와 같은 더블).
"""
from __future__ import annotations

import ast
import importlib.util
import json
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from datagen import control, faults
from datagen import pemdb1 as P
from datagen import scenarios as S
from test_capacity_shift import A, POOL, _master, _row

ROOT = Path(__file__).resolve().parents[1]
T0 = datetime(2026, 11, 15, 0, 30)


def _sc(minute: int):
    return SimpleNamespace(start_minute=0, _minute=minute, peak=1.0, target=SimpleNamespace(shift_to="B"))  # B 있음=증설 곡선(없으면 매일 부하, 창 끝 기준)


# ── 1 · 국면 ─────────────────────────────────────────────────────────────

def test_constants_are_twenty_and_thirty_with_the_recover_unchanged():
    assert (S.SHIFT_RISE, S.SHIFT_TOTAL, S.SHIFT_RAMP, S.SHIFT_RECOVER) == (20, 30, 5, 5)
    assert S.SHIFT_RISE - S.SHIFT_RAMP >= 15                                # 정점 뒤 15분 — Q-11 5분 칸 3칸 연속
    assert S.SHIFT_RISE + S.SHIFT_RECOVER < S.SHIFT_TOTAL                    # A 회복 뒤에도 B 가 켜져 있는 구간이 남는다


def _sc_b(minute: int, shift_to="B"):
    """증설 기지국 B 가 있는 24번 — 11-21 부터 `shift_split` 은 `target.shift_to` 가 있을 때만 호를 넘긴다"""
    return SimpleNamespace(start_minute=0, _minute=minute, peak=1.0, target=SimpleNamespace(shift_to=shift_to))


def test_split_is_zero_before_twenty_minutes_and_sixty_percent_from_twenty():
    assert [S.shift_split(_sc_b(m)) for m in (0, 10, 19)] == [0.0, 0.0, 0.0]
    assert S.shift_split(_sc_b(20)) == S.SHIFT_SPLIT == 0.6 and S.shift_split(_sc_b(29)) == 0.6


def test_congestion_rises_for_five_minutes_holds_until_twenty_then_falls_to_zero_by_twenty_five():
    c = [S.shift_congestion(_sc(m)) for m in range(30)]
    assert c[0] == pytest.approx(0.2) and c[4] == pytest.approx(1.0)
    assert all(v == pytest.approx(1.0) for v in c[4:20])                      # 정점 15분
    assert c[20] == pytest.approx(0.8) and c[23] == pytest.approx(0.2)
    assert all(v == 0.0 for v in c[24:])                                       # 25분(e=25)에는 0
    assert c[19] > c[20] > c[21] > c[22] > c[23] >= c[24]


def test_the_same_standby_comes_back_only_after_one_hundred_twenty_minutes():
    assert len(POOL) == 4
    for s in range(0, 24 * 60, 13):
        b = S.shift_pick(s, POOL)
        for d in (30, 60, 90):
            assert S.shift_pick(s + d, POOL) != b, (s, d)
        assert S.shift_pick(s + 120, POOL) == b                              # 4개 × 30분


def test_no_run_reuses_a_standby_inside_the_d4_window():
    """앞 회차 B 가 켜진 구간이 뒤 회차 B 소등 직전 60분 밖이어야 한다 — 같은 B 는 200분 이상 떨어진다."""
    last = {}
    for s in range(0, 24 * 60):
        b = S.shift_pick(s, POOL)
        if b in last and s - last[b] < S.SHIFT_TOTAL:
            continue
        if b in last:
            assert s - last[b] >= 60
        last[b] = s


def test_build_holds_thirty_minutes_and_the_recommended_length_is_thirty():
    S.set_shift_cfg({"from_enb": A, "standby_enbs": list(POOL)})
    try:
        sc = faults.build(_row(), [c for c in _master() if c.enb_id not in POOL], T0, random.Random(0))
        assert sc.duration == 30
    finally:
        S.set_shift_cfg(None)
    row = next(r for r in control.SCENARIOS if r[0] == 24)
    assert any("30분 고정" in str(x) for x in row)                                    # 화면 설명문도 30분


# ── 2 · write_unscored_truth ─────────────────────────────────────────────

class _C:
    def __init__(self, k): self.cell_key = k


def _built(fid, start):
    S.set_shift_cfg({"from_enb": A, "standby_enbs": list(POOL)})
    row = {**_row(fid=fid), "start_ts": start, "duration_min": 30}
    return faults.build(row, [c for c in _master() if c.enb_id not in POOL], T0, random.Random(fid))


def test_unscored_truth_holds_cells_phases_and_what_to_judge(tmp_path):
    try:
        sc = _built(343, T0)
        b = sc.target.shift_to
        standby = {b: [_C(f"{b}-{n}") for n in range(3)]}
        doc = P.write_unscored_truth(tmp_path / "u.json", T0, [sc], standby)
    finally:
        S.set_shift_cfg(None)
    row = doc["runs"][sc.id]
    assert row["scored"] is False and row["a_enb"] == A and row["a_cells"] == sorted(f"{A}-{n}" for n in range(3))
    assert row["b_enb"] == b and row["b_cells"] == [f"{b}-{n}" for n in range(3)]
    assert row["phases"] == {"start": "2026-11-15 00:30:00", "peak": "2026-11-15 00:35:00", "b_open": "2026-11-15 00:50:00",
                             "a_recovered": "2026-11-15 00:55:00", "end": "2026-11-15 01:00:00"}
    assert row["start"] == "2026-11-15 00:30:00" and row["end"] == "2026-11-15 01:00:00"
    assert len(row["judge"]) == 4 and "원인 이름은 판정하지 않는다" in row["judge"][-1]
    assert "score" in doc["_note"] and json.loads((tmp_path / "u.json").read_text(encoding="utf-8")) == doc


def test_unscored_truth_stacks_runs_by_fault_id_and_skips_scored_ones(tmp_path):
    path = tmp_path / "u.json"
    try:
        a = _built(343, T0)
        b = _built(350, T0 + timedelta(minutes=300))
        scored = SimpleNamespace(scored=True, id="F-999", start_minute=0, duration=10, kind="x")
        P.write_unscored_truth(path, T0, [a], {})
        doc = P.write_unscored_truth(path, T0, [b, scored], {})
    finally:
        S.set_shift_cfg(None)
    assert set(doc["runs"]) == {a.id, b.id} and "F-999" not in doc["runs"]              # 앞 회차를 지우지 않았다
    assert doc["runs"][b.id]["start"] == "2026-11-15 05:30:00" and doc["runs"][b.id]["b_cells"] == []   # 스탠바이를 모르면 빈 목록(지어내지 않는다)


def test_a_broken_unscored_file_is_rewritten_not_fatal(tmp_path):
    path = tmp_path / "u.json"
    path.write_text("{깨진", encoding="utf-8")
    try:
        doc = P.write_unscored_truth(path, T0, [_built(343, T0)], {})
    finally:
        S.set_shift_cfg(None)
    assert len(doc["runs"]) == 1


# ── 3 · _live_truth ──────────────────────────────────────────────────────

def _fakeP(calls):
    return SimpleNamespace(UNSCORED_TRUTH_FILE="unscored_truth.json",
                           write_unscored_truth=lambda path, anchor, free, standby: calls.append(("u", path.name, [s.id for s in free])),
                           write_truth=lambda path, anchor, minutes, scs, sel: calls.append(("t", path.name, [s.id for s in scs])))


def _gen(tmp_path):
    return SimpleNamespace(truth_path=tmp_path / "ground_truth.json", anchor=T0, standby={"9": []})


def _free(i="F-343"):
    return SimpleNamespace(scored=False, id=i, start_minute=0, duration=30, kind="capacity_shift")


def _scored(i="F-342"):
    return SimpleNamespace(scored=True, id=i, start_minute=0, duration=18, kind="ran_congestion")


def test_live_truth_writes_only_the_unscored_file_when_only_24_is_active_and_never_the_ground_truth(tmp_path):
    from datagen import cli
    calls, g = [], _gen(tmp_path)
    cli._live_truth(_fakeP(calls), g, [_free()])
    assert calls == [("u", "unscored_truth.json", ["F-343"])]                          # 10-29 빈 목록 사고 — 채점 파일은 안 건드린다
    assert not (tmp_path / "ground_truth.json").exists()


def test_live_truth_does_not_rewrite_for_the_same_active_set(tmp_path):
    from datagen import cli
    calls, g = [], _gen(tmp_path)
    for _ in range(3):
        cli._live_truth(_fakeP(calls), g, [_free()])
    assert len(calls) == 1
    cli._live_truth(_fakeP(calls), g, [_free("F-350")])                               # 활성 집합이 바뀌면 다시 쓴다
    assert len(calls) == 2


def test_live_truth_mixed_run_writes_each_file_with_its_own_members(tmp_path):
    from datagen import cli
    calls, g = [], _gen(tmp_path)
    cli._live_truth(_fakeP(calls), g, [_free(), _scored()])
    assert ("u", "unscored_truth.json", ["F-343"]) in calls and ("t", "ground_truth.json", ["F-342"]) in calls
    assert len(calls) == 2


def test_live_truth_without_a_path_or_anchor_writes_nothing(tmp_path):
    from datagen import cli
    calls = []
    cli._live_truth(_fakeP(calls), SimpleNamespace(truth_path=None, anchor=T0), [_free(), _scored()])
    cli._live_truth(_fakeP(calls), SimpleNamespace(truth_path=tmp_path / "g.json", anchor=None), [_free(), _scored()])
    assert calls == []


# ── 4 · tools/inject.py — 가상 시각 ──────────────────────────────────────

def _inject():
    spec = importlib.util.spec_from_file_location("inject_tool_1115", ROOT / "tools/inject.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules["inject_tool_1115"] = m
    spec.loader.exec_module(m)
    return m


INJ = _inject()
OFF = timedelta(seconds=31245)                                                         # +8시간 40분 45초


@pytest.fixture
def offset():
    old = list(INJ._OFFSET)
    INJ._OFFSET[:] = [OFF]
    yield
    INJ._OFFSET[:] = old


def _s(a, b):
    return {"dur": {"hours": {"from": a, "to": b}}, "name": "t", "kind": "x"}


def test_in_hours_compares_the_virtual_hour(offset):
    assert INJ.in_hours(_s(8, 21), datetime(2026, 11, 15, 1, 0))                       # 01:00 + 8:40:45 = 09:40
    assert not INJ.in_hours(_s(8, 21), datetime(2026, 11, 14, 16, 0))                  # 16:00 → 00:40
    assert INJ.in_hours(_s(6, 2), datetime(2026, 11, 14, 21, 20))                       # 21:20 → 06:00:45 (14번 권장 6~2시)
    assert not INJ.in_hours(_s(6, 2), datetime(2026, 11, 14, 20, 50))                   # 05:30


def test_in_hours_midnight_crossing_and_all_day(offset):
    s = _s(5, 3)                                                                       # 05시~다음날 03시
    assert INJ.in_hours(s, datetime(2026, 11, 14, 20, 20))                              # 05:00:45
    assert not INJ.in_hours(s, datetime(2026, 11, 14, 18, 40))                          # 03:20 — 17번 때 경계
    assert INJ.in_hours({"dur": {"hours": {"all": True}}}, datetime(2026, 11, 14, 3, 0))
    assert INJ.in_hours({"dur": {}}, datetime(2026, 11, 14, 3, 0))                      # 시간대가 없으면 안(15번처럼 `dur.hours` 에 없는 번호)


def test_warning_text_shows_the_virtual_time_and_says_when_it_is_unknown(offset, monkeypatch):
    out = INJ._warn(23, _s(8, 22), datetime(2026, 11, 14, 16, 0), 25)
    txt = [w for w in out if "권장 시간대 밖" in w][0]
    assert "가상 00:40~01:05" in txt and "8~22" in txt.replace("시", "").replace(" ", "") or "08" in txt
    INJ._OFFSET[:] = []
    monkeypatch.setattr(INJ, "_call", lambda *a, **k: (_ for _ in ()).throw(SystemExit("gen-ui 에 닿지 않는다")))
    out = INJ._warn(23, _s(8, 22), datetime(2026, 11, 14, 3, 0), 25)               # 오프셋을 몰라 실시각 03시로 비교 → 밖
    assert any("가상 시각 모름 — 실시각으로 비교" in w for w in out)                      # 못 읽으면 실시각으로 비교하고 그렇다고 말한다


def test_offset_is_read_once_from_the_gen_status(monkeypatch):
    INJ._OFFSET[:] = []
    seen = []
    monkeypatch.setattr(INJ, "_call", lambda path, body=None: seen.append(path) or {"virtual_offset_sec": 31245})
    assert INJ.offset() == OFF and INJ.offset() == OFF and seen == ["/api/status"]
    INJ._OFFSET[:] = []


def test_line_format_adds_the_virtual_window(offset):
    src = (ROOT / "tools/inject.py").read_text(encoding="utf-8")
    assert "[가상 {virtual(start):%H:%M}~{virtual(end):%H:%M}]" in src


# ── 5 · 14번·27번이 바뀌지 않았다 ────────────────────────────────────────

def _src(name):
    tree = ast.parse((ROOT / "src/datagen/scenarios.py").read_text(encoding="utf-8"))
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
    return ast.dump(node)


@pytest.mark.parametrize("name", ["_congest", "_ran_congestion", "_ran_congestion_users", "congested_user"])
def test_other_scenarios_functions_still_exist_and_do_not_read_the_shift_constants(name):
    body = _src(name)
    assert "SHIFT_RISE" not in body and "SHIFT_TOTAL" not in body                      # 24번 전용 상수에 기대지 않는다
