"""27번 지진 — 진앙은 진원역에서, 셀 대상은 가까운 진원역만 정한다 (2026-11-14 · 맥스 `_quake_step` 시험).

전에는 고른 지역(자동이면 호가 많은 곳)의 셀 중심이 진앙이었다 — 도쿄 한가운데에 찍혔다(F-332). 이제 `ENB:`·`BOX:` 같은 셀 대상은
**가까운 진원역**을 고르는 데만 쓰고, 진앙·깊이·규모는 진원역에서 정한다. 폭주 지역은 대상이 아니라 지진이 정한다.
DB 를 쓰지 않는다(가짜 연결).
"""
from __future__ import annotations

import random
from datetime import datetime

import pytest

from datagen import faults
from datagen import scenarios as S
from datagen.geo_lite import haversine_km
from test_quake_1113 import _cell, _grid

REGION_B = S.FAULT.quake_regions[1]                 # 千葉県北西部 35.58/140.14
REGION_D = S.FAULT.quake_regions[3]                 # 神奈川・埼玉西部 35.56/139.33


def _build(target, cells, fid=27):
    return faults.build({"fault_id": fid, "kind": "natural_disaster", "target": target,
                         "start_ts": datetime(2026, 11, 14, 10, 0), "duration_min": 25, "intensity": 1.0},
                        cells, datetime(2026, 11, 14, 9, 0), random.Random(1), seed=3)


@pytest.mark.parametrize("fid", range(6))
def test_a_cell_target_only_picks_the_nearest_region_and_the_epicenter_stays_in_that_region(fid):
    east = _grid(35.6, 140.0)                       # 동쪽 격자 — 가까운 진원역은 B
    west = _grid(35.6, 139.4)                       # 서쪽 격자 — 가까운 진원역은 D
    for cells, reg in ((east, REGION_B), (west, REGION_D)):
        target = f"ENB:{cells[0].enb_id}"
        sc = _build(target, cells, fid)
        assert sc is not None
        q = sc.as_truth()["quake"]
        assert q["region"] == reg[0]
        assert abs(q["lat"] - reg[1]) <= 0.1 + 1e-6 and abs(q["lon"] - reg[2]) <= 0.1 + 1e-6
        # 진앙은 고른 셀의 중심이 아니라 진원역에서 흩뜨린 값이다(셀 중심에 찍히던 옛 동작이 아니다). 서쪽 격자는 진원역 D 가
        # 셀 0 에서 2km 라 거리 문턱은 줄 수 없다 — 셀 중심과 **같지 않음**만 본다
        assert haversine_km(q["lat"], q["lon"], cells[0].latitude, cells[0].longitude) > 0.05


def test_the_surge_area_is_decided_by_the_quake_not_by_the_chosen_cells():
    cells = _grid(35.6, 140.0)
    sc = _build(f"ENB:{cells[0].enb_id}", cells)
    area = sc.target.cell_keys
    assert len(area) > 3                            # 고른 eNB(셀 3개)가 아니라 진도 3 이상 셀 25개 + 꺼진 셀
    inten = sc.as_truth()["cell_intensity"]
    assert set(inten) == set(area)


def test_an_explicit_epicenter_and_magnitude_are_used_as_given_with_the_nearest_region_depth_and_d():
    cells = _grid(35.6, 140.0)
    sc = _build("QUAKE:35.6,140.0,6.0", cells)
    q = sc.as_truth()["quake"]
    assert (q["lat"], q["lon"], q["magnitude"]) == (35.6, 140.0, 6.0)
    assert q["region"] == REGION_B[0] and q["fault_type_d"] == REGION_B[4]
    assert abs(q["depth_km"] - REGION_B[3]) <= 10.0 + 0.05
    deep = _build("QUAKE:35.6,140.0,6.0,70", cells).as_truth()["quake"]
    assert deep["depth_km"] == 70.0


def test_no_event_is_built_when_no_cell_can_reach_the_surge_intensity():
    far = [_cell(f"{900000 + i}", 0, 43.0 + i * 0.01, 142.0) for i in range(30)]   # 홋카이도 — 진원역에서 600km 넘게
    assert _build(None, far) is None


def test_cells_without_coordinates_are_never_in_the_area():
    cells = _grid(35.6, 140.0) + [_cell("999999", 0, 0.0, 0.0, geo="UNMAPPED")]
    sc = _build(None, cells)
    assert sc is not None and "999999-0" not in sc.target.cell_keys
