"""27번 지진 — 폭주 지역은 모양과 상관없이 진앙 둘레다 (2026-11-14 · 동구 `quake_plan` 수정 고정).

11-13 결정은 "통화 폭주는 이전과 같다, **진앙 중심** 25/80셀"이었다. 그런데 LINEAR 면 `quake_dist_km` 이 선분까지 거리라
선분 위 셀이 모두 거리 0 으로 같아져 25셀이 선분 끝에 몰렸다(F-329 · 진앙에서 5~7.6km). 이제 폭주 지역은 진앙까지
`haversine_km` 순이고, 선분 거리는 **정지 eNB 를 고를 때만** 쓴다. DB 를 쓰지 않는다(가짜 연결).
"""
from __future__ import annotations

import random

import pytest

from datagen import scenarios as S
from datagen.geo_lite import haversine_km
from test_quake_1113 import _cell, _grid, _q


def _line(**kw):
    """남북으로 긴 LINEAR — 진앙(35.0, 139.0)에서 북·남 10km씩."""
    return _q(shape="LINEAR", azimuth_deg=0.0, length_km=20.0, radius_km=10.0, **kw)


def _surge(area, dc):
    return area - set(dc)


def _epi_km(c, q):
    return haversine_km(q["lat"], q["lon"], c.latitude, c.longitude)


# ── 1 · LINEAR — 폭주는 진앙에서 가까운 want 셀 ─────────────────────────────────────────

@pytest.mark.parametrize("want", (25, 80))
@pytest.mark.parametrize("seed", range(4))
def test_linear_surge_is_the_nearest_cells_to_the_epicenter_not_to_the_line(want, seed):
    cells, q = _grid(), _line(base_pct=60.0)
    area, dead, dc = S.quake_plan(cells, q, random.Random(seed), want)
    surge = _surge(area, dc)
    assert len(surge) <= want
    by = {c.cell_key: c for c in cells}
    inn = max(_epi_km(by[k], q) for k in surge)
    out = min(_epi_km(c, q) for c in cells if c.cell_key not in area)
    assert inn <= out + 1e-9                                      # 들어온 셀이 안 들어온 셀보다 진앙에 가깝다


def test_linear_surge_is_not_dragged_to_the_far_end_of_the_line():
    """진앙에서 먼(선분 끝) 셀이 폭주 지역에 섞이지 않는다 — F-329 에서 25셀이 5~7.6km 밖에 잡혔다."""
    cells, q = _grid(), _line(base_pct=0.0)                       # 정지 없음 — 폭주만 본다
    area, dead, dc = S.quake_plan(cells, q, random.Random(0), 25)
    assert dead == [] and len(area) == 25
    by = {c.cell_key: c for c in cells}
    assert max(_epi_km(by[k], q) for k in area) < 3.0             # 격자 간격 1km — 25셀은 진앙 둘레 3km 안


def test_the_old_line_distance_order_would_have_pulled_cells_far_away():
    """수정 전 순서(선분 거리)를 그대로 쓰면 25셀이 진앙에서 멀리 간다 — 위 시험이 헐렁하지 않음을 보인다."""
    cells, q = _grid(), _line(base_pct=0.0)
    old = sorted((S.quake_dist_km(q, c.latitude, c.longitude), c.cell_key) for c in cells)[:25]
    by = {c.cell_key: c for c in cells}
    assert max(_epi_km(by[k], q) for _d, k in old) > 3.0


# ── 2 · ROUND — 수정 전과 같다 ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("want", (25, 80))
def test_round_surge_is_unchanged(want):
    cells, q = _grid(), _q(base_pct=60.0)
    area, _d, dc = S.quake_plan(cells, q, random.Random(1), want)
    old = {k for _d2, k in sorted((S.quake_dist_km(q, c.latitude, c.longitude), c.cell_key)
                                  for c in cells if c.geo_status == "MAPPED")[:want]} | set(dc)
    assert area == old


# ── 3 · 정지 eNB — 선분 거리 기준 그대로 ─────────────────────────────────────────────────

def test_dead_enbs_do_not_depend_on_the_surge_ordering(monkeypatch):
    cells, q = _grid(), _line(base_pct=70.0, max_cells=30)
    ref = [S.quake_plan(cells, q, random.Random(s), 25)[1:] for s in range(6)]
    # 폭주 순서 계산이 쓰는 haversine 을 흔들어도(뒤집어도) 정지 eNB·정지 셀은 같다 — 정지는 선분 거리·rng 가 정한다
    import datagen.geo_lite as G
    real = G.haversine_km
    monkeypatch.setattr(G, "haversine_km", lambda a, b, c, d: -real(a, b, c, d))
    assert [S.quake_plan(cells, q, random.Random(s), 25)[1:] for s in range(6)] == ref


def test_dead_enbs_are_within_the_radius_of_the_line():
    cells, q = _grid(), _line(base_pct=100.0, max_cells=300)
    _a, dead, _dc = S.quake_plan(cells, q, random.Random(0), 25)
    assert dead
    by_enb = {}
    for c in cells:
        by_enb.setdefault(c.enb_id, []).append(c)
    for e in dead:                                                # 한 셀이라도 선분 반경 안이어서 뽑혔다
        assert min(S.quake_dist_km(q, c.latitude, c.longitude) for c in by_enb[e]) <= q["radius_km"]


# ── 4 · 폭주 지역 크기 ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("want", (25, 80))
def test_surge_size_is_want_plus_the_dead_cells(want):
    assert S.DISASTER_AREA_CELLS == {"local": 25, "wide": 80}
    cells = _grid()
    for shape_q in (_q(base_pct=60.0), _line(base_pct=60.0)):
        area, _d, dc = S.quake_plan(cells, shape_q, random.Random(2), want)
        assert want <= len(area) <= want + len(dc) and set(dc) <= area


# ── 5 · 좌표 없는 셀 ───────────────────────────────────────────────────────────────────

def test_unmapped_cells_stay_out_of_the_surge_unless_their_enb_is_dead():
    cells = _grid()
    cells += [_cell("999001", n, 35.0, 139.0, geo="UNMAPPED") for n in range(3)]   # 진앙 한가운데 좌표 없는 셀
    area, dead, dc = S.quake_plan(cells, _line(base_pct=0.0), random.Random(0), 25)
    assert not any(k.startswith("999001-") for k in area)
    area2, dead2, dc2 = S.quake_plan(cells, _line(base_pct=100.0, max_cells=300), random.Random(0), 25)
    assert not any(k.startswith("999001-") for k in area2 - set(dc2))
