"""`Groups.shares` — 지하철 운행 시작·끝의 선형 경사 (2026-11-12 · graphio 27번 F-303).

운행 시작 정시(06:00)에 0 → 10% 로 켜면 노선 eNB 의 분당 호가 1분 만에 5~24배가 되어
D5 호 폭주(C-24)가 매일 같은 시각에 섰다. 시작부터 `blend_min` 동안 0 → 1, 끝 `blend_min`
전부터 1 → 0 으로 잇고, 빠진 몫은 그냥 이동이 가져간다(이동 총량은 표대로).
"""
from datetime import datetime

import pytest

from datagen.generator import Groups

HOURLY = [[0, 5, 90, 10, 0], [5, 7, 60, 40, 10], [7, 9, 30, 70, 50],
          [9, 17, 10, 90, 30], [17, 20, 30, 70, 50], [20, 24, 60, 40, 10]]


def _at(g, h, m=0):
    return g.shares(datetime(2026, 11, 12, h, m))


@pytest.fixture
def ramped():
    return Groups(HOURLY, (6, 24), blend_min=60)


@pytest.fixture
def reference():
    # 운행이 하루 종일(0~24)이면 경사 분기가 꺼진다 — 경사 없는 값의 기준선
    return Groups(HOURLY, (0, 24), blend_min=60)


def test_zero_at_the_start_of_service(ramped):
    sw, wk = _at(ramped, 6, 0)
    assert sw == 0.0
    assert wk == pytest.approx(0.40), "빠진 지하철 몫은 그냥 이동이 가져간다"


def test_half_of_the_band_value_half_an_hour_in(ramped):
    # 06시대는 5–7 띠(지하철 10%) — 30분이면 blend 60분의 절반
    sw, wk = _at(ramped, 6, 30)
    assert sw == pytest.approx(0.05)
    assert wk == pytest.approx(0.35)


def test_full_band_value_from_one_blend_after_the_start(ramped, reference):
    # 07:00 이후부터 끝 경사 앞(23:00)까지는 경사가 없다 — 기준선과 같다
    for h in range(7, 23):
        for m in (0, 15, 30, 45):
            assert _at(ramped, h, m) == pytest.approx(_at(reference, h, m)), (h, m)


def test_approaches_zero_at_the_end_of_service(ramped):
    sw, wk = _at(ramped, 23, 59)
    assert 0.0 <= sw < 0.002, "끝 직전 지하철 몫은 0 에 가깝다"
    assert wk > 0.2, "빠진 몫은 그냥 이동으로 간다"
    # 끝으로 갈수록 단조 감소
    seq = [_at(ramped, 23, m)[0] for m in (0, 15, 30, 45, 59)]
    assert seq == sorted(seq, reverse=True)


def test_ramp_is_linear_over_the_blend(ramped):
    sw0 = _at(ramped, 6, 0)[0]
    # 06:30 부터는 7시 경계 혼합(06:30~07:30)이 겹치므로 06:00~06:29 에서 본다
    sws = [_at(ramped, 6, m)[0] for m in (0, 10, 20, 29)]
    assert sw0 == 0.0
    steps = [(b - a) / dm for a, b, dm in zip(sws, sws[1:], (10, 10, 9))]
    assert steps == pytest.approx([steps[0]] * 3), "분당 같은 폭으로 오른다"
    assert steps[0] > 0


def test_total_movers_unchanged_by_the_ramp(ramped, reference):
    # 지하철 + 그냥 이동 = 이동 총량 — 경사가 몫만 옮기고 총량은 건드리지 않는다
    for h in range(0, 24):
        for m in (0, 10, 30, 50, 59):
            sw, wk = _at(ramped, h, m)
            rsw, rwk = _at(reference, h, m)
            if h < 6:
                # 운행 전: 지하철 0, 몫은 그냥 이동 — 기준선(하루 종일 운행)과 합만 같다
                assert sw == 0.0
            assert sw + wk == pytest.approx(rsw + rwk), (h, m)


def test_full_day_service_has_no_ramp(reference):
    # [0, 24] 는 경사 분기에 들지 않는다 — 06:00 에도 띠 값 그대로
    assert _at(reference, 6, 0)[0] == pytest.approx(0.10)
    assert _at(reference, 3, 0)[0] == 0.0, "0–5 띠의 지하철은 0"
    assert _at(reference, 23, 59)[0] > 0.04


def test_zero_blend_keeps_the_step():
    # blend_min=0 이면 경사도 없다 — 정시 계단(옛 동작)
    g = Groups(HOURLY, (6, 24), blend_min=0)
    assert _at(g, 6, 0)[0] == pytest.approx(0.10)
    assert _at(g, 5, 59)[0] == 0.0


def test_outside_service_still_zero(ramped):
    assert _at(ramped, 5, 59)[0] == 0.0
    assert _at(ramped, 3, 0)[0] == 0.0
