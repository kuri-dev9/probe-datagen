"""머무는 탑승자가 역이 아닌 칸에 있어도 gen 이 죽지 않는다 (2026-11-24 · 곡선 켠 07:52 뒤 1시간에 8번 재시작).

`advance_routes` 가 하차 대기를 마친 탑승자의 다음 여정을 `st.index(route_pos)` 로 찾았다. 이동 상태 이어 받기
(`seat_from_cells`)가 역이 아닌 칸으로 되돌린 사람은 그 칸이 역 목록에 없어 `ValueError: 28 is not in list` 로
gen 이 죽었고, 컨테이너가 다시 뜨는 동안 분이 비었다. 곡선 끔(균등 모드)에는 아무도 움직이지 않아 드러나지 않았다.
합성 셀·합성 경로만 쓴다.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from datagen import handover as HO
from datagen.cells import load_cells
from datagen.generator import Generator
from datagen.profile import load as load_profiles

DATA = Path(__file__).resolve().parents[1] / "data"
pytestmark = pytest.mark.skipif(not (DATA / "profile.json").exists(), reason="data/profile.json 없음")


@pytest.mark.parametrize("pos,loop,want", [
    (7, True, 2),      # 역 칸이면 그 역
    (5, True, 2),      # 역 사이면 진행 방향 다음 역
    (13, True, 0),     # 마지막 역 뒤 — 순환선은 처음 역
    (13, False, 3),    # 순환이 아니면 마지막 역
])
def test_station_idx(pos, loop, want):
    assert Generator._station_idx([0, 3, 7, 12], pos, loop) == want


def _gen_with_route():
    cells = load_cells(DATA / "cells.json")
    profiles = load_profiles(DATA / "profile.json")
    keep = {s: profiles[s] for s in ("lte_call", "transport", "dns", "http")}
    g = Generator(cells=cells, profiles=keep, scenarios=[],
                  rates={"lte_call": 400, "transport": 400, "dns": 200, "http": 200},
                  seed=20261124, diurnal=True, subscriber_pool=300,
                  success_only=True, end_only=True)
    cs = tuple(cells[:10])
    rt = HO.Route("T", "T", cs, (60,) * len(cs), loop=True,
                  station_pos={"A": 0, "B": 4, "C": 8})
    g.routes = [rt]
    g.flat_mode = False
    g.groups = None
    return g


def test_waiting_rider_on_a_non_station_cell_boards_from_the_next_station():
    g = _gen_with_route()
    sub = g.subs[0]
    sub.route, sub.route_hops = 0, (1, 1)
    sub.route_pos = 6                      # 역(0·4·8)이 아닌 칸 — 이어 받기가 남기던 상태
    sub.route_wait = 100.0                 # 머무는 중 · 이미 끝난 시각
    sub.route_t = 0.0
    g._mobile = [0]
    g.advance_routes(1000.0)               # 전에는 ValueError
    assert sub.route_wait == 0.0
    assert (sub.route_from, sub.route_to) == (8, 0)    # 다음 역 C(8)에서 한 역 뒤 A(0)


def test_restore_onto_a_non_station_cell_is_riding_not_waiting():
    g = _gen_with_route()
    sub = g.subs[0]
    sub.route, sub.route_hops = 0, (1, 1)
    sub.route_from, sub.route_to = 4, 8
    sub.route_wait = 500.0
    cell = g.routes[0].cells[6]
    g.seat_from_cells({sub.imsi: (cell.cell_key, False)})
    if sub.route_pos == 6:                 # 여정 칸에서 찾았을 때만 옮긴다
        assert sub.route_wait == 0.0
