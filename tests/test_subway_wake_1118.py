"""운행 시작·끝 깨는 몫 — `Groups.subway_ramp` · `Groups.shares` 회귀 · `Generator.advance_routes` (2026-11-18 · graphio 14번 F-391).

호 비율(`shares`)만 `blend_min` 에 걸쳐 섞고 노선 이동은 06:00 에 전원을 풀어, 밤새 서 있던 탑승자가 첫 분에 일제히
칸을 옮겼다 — 역 eNB 10곳의 TAU 가 분당 0 → 122 로 튀어 D5 호 폭주(C-24)가 섰다. 이제 계수(`subway_ramp`)를 둘이 같이 부르고,
탑승자는 자기 키 난수가 계수보다 작을 때만 움직인다. 합성 셀·합성 경로만 쓴다.
"""
from __future__ import annotations

import random
from datetime import datetime
from pathlib import Path

import pytest

from datagen import handover as HO
from datagen.cells import load_cells
from datagen.generator import Generator, Groups, _keyed_rng
from datagen.profile import load as load_profiles

HERE = Path(__file__).resolve().parents[1]
DATA = HERE / "data"
HOURLY = [[0, 5, 90, 10, 0], [5, 7, 60, 40, 10], [7, 9, 30, 70, 50],
          [9, 17, 10, 90, 30], [17, 20, 30, 70, 50], [20, 24, 60, 40, 10]]
DAY = datetime(2026, 11, 18)


def _t(h, m=0, s=0):
    return DAY.replace(hour=h, minute=m, second=s)


# ── subway_ramp ─────────────────────────────────────────────────────────

@pytest.fixture
def g():
    return Groups(HOURLY, (6, 24), blend_min=60)


@pytest.mark.parametrize("h,m,want", [
    (0, 0, 0.0), (3, 30, 0.0), (5, 59, 0.0), (6, 0, 0.0),                      # 운행 전
    (6, 15, 0.25), (6, 30, 0.5), (6, 45, 0.75), (7, 0, 1.0), (12, 0, 1.0),     # 시작 경사 · 한창
    (22, 59, 1.0), (23, 0, 1.0), (23, 30, 0.5), (23, 45, 0.25),                # 끝 경사
])
def test_ramp_values(g, h, m, want):
    assert g.subway_ramp(_t(h, m)) == pytest.approx(want)


def test_ramp_just_before_the_end_is_near_zero_and_at_midnight_zero(g):
    assert g.subway_ramp(_t(23, 59)) == pytest.approx(1 / 60)
    assert g.subway_ramp(_t(0, 0)) == 0.0                                       # 24:00 은 운행 밖(`a <= h < b`)


def test_ramp_is_monotone_up_then_down(g):
    up = [g.subway_ramp(_t(6, m)) for m in range(0, 60, 5)]
    down = [g.subway_ramp(_t(23, m)) for m in range(0, 60, 5)]
    assert up == sorted(up) and down == sorted(down, reverse=True)


def test_ramp_without_blend_is_one_while_running():
    g0 = Groups(HOURLY, (6, 24), blend_min=0)
    assert g0.subway_ramp(_t(5, 59)) == 0.0
    assert g0.subway_ramp(_t(6, 0)) == 1.0
    assert g0.subway_ramp(_t(23, 59)) == 1.0
    assert g0.subway_ramp(_t(0, 0)) == 0.0


def test_ramp_for_all_day_service_is_always_one():
    g24 = Groups(HOURLY, (0, 24), blend_min=60)
    for h, m in ((0, 0), (3, 0), (6, 0), (12, 0), (23, 59)):
        assert g24.subway_ramp(_t(h, m)) == 1.0


def test_ramp_accepts_epoch_seconds(g):
    assert g.subway_ramp(_t(6, 30).timestamp()) == pytest.approx(0.5)


# ── shares 회귀 — 예전 식과 하루 전체가 같다 ────────────────────────────

def _old_shares(g: Groups, ts):
    """맥스가 바꾸기 전 `shares` — 경계 섞기 뒤 운행 경사를 곱하는 식을 그대로 옮긴 기준선."""
    h = g._hour(ts)
    mv, sw = g._band(h)
    if g.blend_h > 0:
        half = g.blend_h / 2
        for h0, *_ in g.bands:
            for edge in (h0, h0 + 24, h0 - 24):
                d = h - edge
                if -half <= d < half:
                    w = (d + half) / g.blend_h
                    pm, ps = g._band(edge - 1e-6)
                    nm, ns = g._band(edge + 1e-6)
                    mv = pm + (nm - pm) * w
                    sw = ps + (ns - ps) * w
                    break
    if not g.subway_running(ts):
        sw = 0.0
    elif g.blend_h > 0 and g.sub_h[1] - g.sub_h[0] < 24:
        a, b = g.sub_h
        sw *= max(0.0, min(1.0, (h - a) / g.blend_h, (b - h) / g.blend_h))
    return sw, mv - sw


@pytest.mark.parametrize("hours,blend", [((6, 24), 60), ((6, 24), 0), ((0, 24), 60), ((5, 23), 30)])
def test_shares_match_the_previous_formula_over_the_whole_day(hours, blend):
    g = Groups(HOURLY, hours, blend_min=blend)
    for sec in range(0, 86400, 30):
        ts = datetime(2026, 11, 18) .replace(hour=sec // 3600, minute=sec % 3600 // 60, second=sec % 60)
        assert g.shares(ts) == pytest.approx(_old_shares(g, ts)), ts


def test_shares_still_ramp_the_subway_share(g):
    assert g.shares(_t(6, 0))[0] == 0.0
    assert g.shares(_t(6, 30))[0] == pytest.approx(0.05)
    assert g.shares(_t(5, 0))[0] == 0.0


# ── advance_routes ──────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def gen() -> Generator:
    cells = load_cells(DATA / "cells.json")
    profiles = load_profiles(DATA / "profile.json")
    keep = {s: profiles[s] for s in ("lte_call", "transport", "dns", "http")}
    return Generator(cells=cells, profiles=keep, scenarios=[],
                     rates={"lte_call": 400, "transport": 400, "dns": 200, "http": 200},
                     seed=20260903, diurnal=True, subscriber_pool=1500,
                     success_only=True, end_only=True)


def _prep(gen, share=1.0, seed=7):
    """경로를 깔고 모든 탑승자를 「체류 시간이 한참 지난 상태」로 둔다 — 깨기만 하면 움직인다."""
    rng = random.Random(seed)
    routes = HO.build_routes(HO.Topology(gen.cells), gen.cells, 6, rng, length=8)
    assert routes
    gen.seat_routes(routes, share, rng)
    gen.groups = Groups(HOURLY, (6, 24), blend_min=60)
    gen.virtual_offset = 0
    gen.flat_mode = False
    return seat_state(gen)


def seat_state(gen):
    for s in gen.subs:
        if s.route >= 0:
            s.route_wait = 0.0
            s.route_t = 1.0
            s.route_to = -1
    return [i for i, s in enumerate(gen.subs) if s.route >= 0]


def _movers(gen, now_dt, riders):
    """한 번 부르고 이번에 움직인 탑승자 번호 집합(`route_t` 가 now 로 바뀐 사람)."""
    now = now_dt.timestamp()
    before = {i: gen.subs[i].route_t for i in riders}
    gen.advance_routes(now)
    return {i for i in riders if gen.subs[i].route_t == now and before[i] != now}


def test_route_base_default_is_zero_and_seat_routes_sets_it():
    cells = load_cells(DATA / "cells.json")
    profiles = load_profiles(DATA / "profile.json")
    keep = {s: profiles[s] for s in ("lte_call", "transport", "dns", "http")}
    g2 = Generator(cells=cells, profiles=keep, scenarios=[],
                   rates={"lte_call": 100, "transport": 100, "dns": 50, "http": 50},
                   seed=1, diurnal=True, subscriber_pool=200, success_only=True, end_only=True)
    assert g2._route_base == 0
    rng = random.Random(3)
    routes = HO.build_routes(HO.Topology(g2.cells), g2.cells, 3, rng, length=6)
    expect = random.Random(3)
    # `build_routes` 가 rng 를 쓴 만큼 같이 소비해 같은 씨를 얻는다
    HO.build_routes(HO.Topology(g2.cells), g2.cells, 3, expect, length=6)
    base = expect.getrandbits(64)
    g2.seat_routes(routes, 0.5, rng)
    assert g2._route_base == base


def test_nobody_moves_before_service_starts(gen):
    riders = _prep(gen)
    assert riders
    assert _movers(gen, _t(5, 59), riders) == set()
    assert _movers(gen, _t(6, 0), riders) == set()                              # 06:00 계수 0 — 06:00 계단이 없다


def test_half_ramp_moves_only_riders_whose_keyed_random_is_below_it(gen):
    riders = _prep(gen)
    moved = _movers(gen, _t(6, 30), riders)
    base = gen._route_base
    want = {i for i in riders if _keyed_rng(base, "wake", i).random() < 0.5}
    assert moved == want
    assert 0 < len(moved) < len(riders)


def test_same_seed_wakes_the_same_riders(gen):
    riders = _prep(gen, seed=11)
    a = _movers(gen, _t(6, 20), riders)
    riders = _prep(gen, seed=11)
    b = _movers(gen, _t(6, 20), riders)
    assert a == b and a


def test_awake_set_only_grows_as_the_ramp_rises(gen):
    riders = _prep(gen)
    sets = []
    for m in (6, 15, 30, 45, 59):
        riders = seat_state(gen)                                               # 매번 같은 출발 상태
        sets.append(_movers(gen, _t(6, m), riders))
    for small, big in zip(sets, sets[1:]):
        assert small <= big                                                    # 먼저 깬 사람이 계속 깨어 있다
    assert len(sets[0]) < len(sets[-1])


def test_everyone_awake_from_seven(gen):
    riders = _prep(gen)
    assert _movers(gen, _t(7, 0), riders) == set(riders)


def test_riders_fall_asleep_in_the_same_order_at_the_end(gen):
    riders = _prep(gen)
    morning = _movers(gen, _t(6, 30), riders)                                  # 계수 0.5
    riders = seat_state(gen)
    evening = _movers(gen, _t(23, 30), riders)                                 # 계수 0.5 — 같은 사람
    assert morning == evening


def test_after_midnight_nobody_moves(gen):
    riders = _prep(gen)
    assert _movers(gen, _t(0, 0), riders) == set()


def test_without_groups_everyone_moves_as_before(gen):
    riders = _prep(gen)
    gen.groups = None
    assert _movers(gen, _t(3, 0), riders) == set(riders)


def test_ramp_zero_leaves_route_state_untouched(gen):
    riders = _prep(gen)
    before = {i: (gen.subs[i].route_pos, gen.subs[i].route_t, gen.subs[i].route_wait) for i in riders}
    gen.advance_routes(_t(5, 30).timestamp())
    after = {i: (gen.subs[i].route_pos, gen.subs[i].route_t, gen.subs[i].route_wait) for i in riders}
    assert before == after


def test_tau_style_step_is_gone_first_minute_moves_nobody_not_everybody(gen):
    # F-391 의 06:00 — 옛 코드는 전원이 풀려 첫 분에 일제히 움직였다
    riders = _prep(gen)
    assert len(_movers(gen, _t(6, 0), riders)) == 0
    riders = seat_state(gen)
    assert len(_movers(gen, _t(6, 1), riders)) < 0.05 * len(riders)
