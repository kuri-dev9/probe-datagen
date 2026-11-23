"""24번 A 혼잡은 세션 추첨이 아니라 셀·분 단위로 거는다 (2026-11-23 · 결정 12 · 동구 구현 · 에스터 시험).

F-534 는 대상 21셀 안 RTT 불량이 45% 인데 세션마다 `intensity × 0.8` 동전을 던져 같은 사람이 분마다 좋았다 나빴다 했다.
혼잡한 분에는 그 셀의 세션이 전부 `_congest(lv)` 를 받고 강도 `lv` 는 얼마나 나빠지느냐에만 쓴다. 지진·재해 둘레는 그대로 동전이다.
8번(`ran_drop_surge`)의 하한 호는 0 이다(결정 15 — `test_fault_cfg_floor_crowd` 가 yaml 값을 본다).
"""
from __future__ import annotations

import random
from types import SimpleNamespace

import pytest

from datagen import scenarios as S


def _sc(minute, peak=1.0):
    return SimpleNamespace(kind="capacity_shift", start_minute=0, _minute=minute, peak=peak,
                           target=SimpleNamespace(shift_to="B"))


def _plan(outcome="normal"):
    return SimpleNamespace(outcome=outcome, span=5, stall=False)


@pytest.fixture
def rec(monkeypatch):
    calls = {"congest": [], "fade": 0}
    monkeypatch.setattr(S, "_congest", lambda plan, rng, lv: calls["congest"].append(lv))
    monkeypatch.setattr(S, "_fade_on", lambda plan, sc: calls.__setitem__("fade", calls["fade"] + 1))
    return calls


class _NoCoin(random.Random):
    """난수를 한 번이라도 쓰면 실패한다 — 셀·분 단위 혼잡은 세션 동전이 없다"""

    def random(self):
        raise AssertionError("세션마다 동전을 던졌다")


# 1. 혼잡한 분에는 세션이 전부 `_congest` 를 받는다 — 동전 없음
def test_every_session_is_congested_in_a_congested_minute(rec):
    sc = _sc(10)
    for _ in range(300):
        assert S._capacity_shift(_plan(), sc, 1.0, _NoCoin(), None) is True
    assert len(rec["congest"]) == 300 and rec["fade"] == 300


# 2. 강도 lv 는 「얼마나」에만 쓰인다 — lv 가 곧 `_congest` 의 인자
def test_intensity_only_scales_how_bad_not_who(rec):
    for minute, peak in ((0, 1.0), (4, 1.0), (10, 0.5), (22, 1.0)):
        sc = _sc(minute, peak)
        rec["congest"].clear()
        assert S._capacity_shift(_plan(), sc, 0.1, _NoCoin(), None) is True      # 인자 intensity 는 무시한다
        assert rec["congest"] == [pytest.approx(S.shift_congestion(sc))]
    # lv = 0(혼잡 끝 · 시작 전)이면 걸리지 않는다
    rec["congest"].clear()
    assert S._capacity_shift(_plan(), _sc(S.SHIFT_RISE + S.SHIFT_RECOVER + 2), 1.0, _NoCoin(), None) is False
    assert rec["congest"] == []


# 3. stall 세션은 건너뛴다(겹침 방지)
def test_stall_sessions_are_skipped(rec):
    assert S._capacity_shift(_plan("stall"), _sc(10), 1.0, _NoCoin(), None) is False
    assert rec["congest"] == [] and rec["fade"] == 0


# 4. 지진·재해 둘레는 그대로 동전 방식이다(회귀)
def test_quake_and_disaster_rings_still_flip_a_coin(rec, monkeypatch):
    sc = _sc(3)
    # `_ran_congestion`: rng.random() >= intensity × 0.8 이면 걸지 않는다
    class _Hi(random.Random):
        def random(self):
            return 0.99

    class _Lo(random.Random):
        def random(self):
            return 0.0

    assert S._ran_congestion(_plan(), sc, 1.0, _Hi(), None) is False and rec["congest"] == []
    assert S._ran_congestion(_plan(), sc, 1.0, _Lo(), None) is True and rec["congest"] == [1.0]
    assert S._ran_congestion(_plan("stall"), sc, 1.0, _Lo(), None) is False
    # 두 둘레는 `_ran_congestion` 을 0.6 배 강도로 부른다 — 이름 바인딩이 `_capacity_shift` 로 바뀌지 않았다
    seen = []
    monkeypatch.setattr(S, "_ran_congestion", lambda plan, sc, inten, rng, cell: seen.append(inten) or True)
    monkeypatch.setattr(S, "quake_down", lambda sc, cell, inten: False)
    monkeypatch.setattr(S, "disaster_dead_enbs", lambda sc, m: {"999"})
    cell = SimpleNamespace(enb_id="1", latitude=0.0, longitude=0.0)
    assert S._quake_ring(_plan(), sc, 1.0, random.Random(1), cell) is True
    assert S._disaster_ring(_plan(), sc, 1.0, random.Random(1), cell) is True
    assert seen == [pytest.approx(0.6), pytest.approx(0.6)]


# 5. 8번 하한 호는 0 · 9번 16호는 그대로(yaml) · 23번·다른 시나리오 하한은 늘지 않는다
def test_ran_drop_surge_floor_is_zero_and_mobility_stays():
    import yaml
    from pathlib import Path
    cfg = yaml.safe_load((Path(__file__).resolve().parents[1] / "config" / "datagen.yaml").read_text(encoding="utf-8"))
    f = S.FaultCfg.from_cfg(cfg["pemdb1"]["fault"])
    assert f.floor_by_kind.get("ran_drop_surge") == 0.0 and f.floor_by_kind.get("mobility") == 16.0
    assert set(f.floor_by_kind) == {"mobility", "ran_drop_surge"}
