"""지진 폭주 배율이 셀 진도를 따른다 — 2026-11-23 사용자 결정 2(재점검 §2 · 4번 지진 발생).

``1 + (DISASTER_SURGE_PEAK − 1) × quake_surge_weight(진도)`` — 진도 3 약 3.7배 · 4 약 11배 · 4.5 약 21배 · 5 이상 40배.
새 기준값은 없다(기준 진도 5.0 = 40배를 잰 도쿄 진도 · 기울기 1.72 = 이미 있던 환산 상수)."""
import pytest

from datagen import scenarios as S


def _mult(i):
    return 1 + (S.DISASTER_SURGE_PEAK - 1) * S.quake_surge_weight(i)


@pytest.mark.parametrize("intensity,expected", [(3.0, 3.7), (4.0, 11.2), (4.5, 21.0)])
def test_the_multiplier_follows_the_cell_intensity(intensity, expected):
    assert _mult(intensity) == pytest.approx(expected, abs=0.3)


@pytest.mark.parametrize("intensity", [5.0, 5.5, 6.0, 6.9])
def test_from_the_reference_intensity_up_it_is_the_full_peak(intensity):
    assert S.quake_surge_weight(intensity) == 1.0
    assert _mult(intensity) == S.DISASTER_SURGE_PEAK


def test_unknown_intensity_keeps_the_old_full_peak():
    """진도를 모르면(지진 정보 없이 만든 옛 경로) 전처럼 정점 그대로다 — 조용히 약해지지 않는다."""
    assert S.quake_surge_weight(None) == 1.0


def test_it_never_leaves_zero_to_one_and_rises_with_intensity():
    ws = [S.quake_surge_weight(i / 10) for i in range(0, 70)]
    assert all(0 < w <= 1 for w in ws)
    assert all(a <= b for a, b in zip(ws, ws[1:]))


def test_the_constants_are_the_documented_ones_not_new_thresholds():
    assert S.DISASTER_SURGE_REF_INTENSITY == 5.0 and S.QUAKE_I_SLOPE == 1.72 and S.DISASTER_SURGE_PEAK == 40
