"""`pemdb1.fault` 설정 — kind 별 호 하한(`floor_by_kind`)과 23번 혼잡 몫(`crowd_*`), 가입자 고정(`congested_user`) (2026-11-06).

* 9번 `mobility` 의 대상 셀 호 하한만 8 → 16 으로 올렸다(셀당 5분 전환 표본을 판정 최소 20 에 닿게). 다른 시나리오의 하한과
  전체 생산량은 그대로다. **모르는 kind 는 멈춘다** — 오타가 "기본값으로 돌았다"로 끝나면 하한이 조용히 틀린다.
* 23번 혼잡 몫은 14번처럼 **사람(IMSI + 장애 ID) 단위로 고정**한다. 세션마다 추첨하면 같은 사람이 분마다 좋았다 나빴다 한다.
* 저장소의 `config/datagen.yaml` 을 코드가 받아들이는지도 본다 — 코드와 설정이 따로 배포되면(컨테이너는 코드를 이미지에 굽고
  설정은 마운트한다) 모르는 키에서 멈춘다. 11-06 에 실제로 그 어긋남이 있었다.

DB 없이 돈다.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from datagen import scenarios as S

ROOT = Path(__file__).resolve().parents[1]


def _repo_fault_cfg() -> dict:
    return yaml.safe_load((ROOT / "config" / "datagen.yaml").read_text(encoding="utf-8"))["pemdb1"]["fault"]


@pytest.fixture
def fresh_fault(monkeypatch):
    """모듈 전역 `FAULT` 를 기본값으로 — 다른 테스트가 건 설정에 기대지 않는다."""
    f = S.FaultCfg()
    monkeypatch.setattr(S, "FAULT", f)
    return f


# ---------------------------------------------------------------- 저장소 설정 ↔ 코드

def test_repo_yaml_fault_block_is_accepted_by_the_code():
    """설정에 있는 모든 키를 코드가 안다 — 모르는 키는 `RuntimeError` 로 멈춘다."""
    S.FaultCfg.from_cfg(_repo_fault_cfg())


def test_repo_yaml_raises_mobility_floor_to_16_and_nothing_else():
    f = S.FaultCfg.from_cfg(_repo_fault_cfg())
    assert f.floor_by_kind == {"mobility": 16.0}
    assert f.floor_per_cell == S.FaultCfg().floor_per_cell, "다른 시나리오의 하한을 건드리면 안 된다"


def test_repo_yaml_crowd_values_match_the_decision():
    f = S.FaultCfg.from_cfg(_repo_fault_cfg())
    assert f.crowd_user_share == 0.6
    assert f.crowd_up_bps == (50_000.0, 150_000.0) and f.crowd_dn_bps == (100_000.0, 300_000.0)


# ---------------------------------------------------------------- floor_by_kind

def test_default_has_no_per_kind_floor():
    assert S.FaultCfg().floor_by_kind == {}


def test_floor_by_kind_is_read_as_floats():
    assert S.FaultCfg.from_cfg({"floor_by_kind": {"mobility": 16}}).floor_by_kind == {"mobility": 16.0}


def test_unknown_kind_in_floor_by_kind_stops_with_its_name():
    with pytest.raises(RuntimeError, match="no_such_kind"):
        S.FaultCfg.from_cfg({"floor_by_kind": {"no_such_kind": 3}})


def test_one_unknown_kind_among_valid_ones_still_stops():
    with pytest.raises(RuntimeError):
        S.FaultCfg.from_cfg({"floor_by_kind": {"mobility": 16, "mobilty": 16}})


@pytest.mark.parametrize("v", [None, {}])
def test_empty_floor_by_kind_is_accepted(v):
    assert S.FaultCfg.from_cfg({"floor_by_kind": v}).floor_by_kind == {}


def test_an_unknown_key_still_stops():
    with pytest.raises(RuntimeError, match="모르는 키"):
        S.FaultCfg.from_cfg({"floor_by_kinds": {"mobility": 16}})


# ---------------------------------------------------------------- crowd_*

@pytest.mark.parametrize("key", ["crowd_up_bps", "crowd_dn_bps"])
def test_crowd_rate_range_is_read_as_a_float_pair(key):
    f = S.FaultCfg.from_cfg({key: [60_000, 90_000]})
    assert getattr(f, key) == (60_000.0, 90_000.0)


@pytest.mark.parametrize("key", ["crowd_up_bps", "crowd_dn_bps"])
@pytest.mark.parametrize("bad", [[0, 100], [-1, 100], [200, 100]])
def test_crowd_rate_range_rejects_non_positive_or_inverted(key, bad):
    with pytest.raises(RuntimeError, match=key):
        S.FaultCfg.from_cfg({key: bad})


def test_crowd_rate_range_equal_bounds_is_allowed():
    assert S.FaultCfg.from_cfg({"crowd_up_bps": [100_000, 100_000]}).crowd_up_bps == (100_000.0, 100_000.0)


def test_crowd_user_share_is_read_as_a_float():
    assert S.FaultCfg.from_cfg({"crowd_user_share": "0.3"}).crowd_user_share == 0.3


def test_crowd_defaults_in_code_are_the_decision_values():
    f = S.FaultCfg()
    assert f.crowd_user_share == 0.6 and f.crowd_up_bps == (50_000.0, 150_000.0) and f.crowd_dn_bps == (100_000.0, 300_000.0)


# ---------------------------------------------------------------- congested_user

IMSIS = [f"4401{n:011d}" for n in range(2000)]


def test_congested_user_is_deterministic_per_person_and_fault(fresh_fault):
    a = [S.congested_user(i, "F-148", 1.0) for i in IMSIS[:200]]
    b = [S.congested_user(i, "F-148", 1.0) for i in IMSIS[:200]]
    assert a == b, "같은 사람은 몇 번을 물어도 같은 쪽이다"


def test_congested_user_differs_between_faults(fresh_fault):
    a = [S.congested_user(i, "F-1", 1.0) for i in IMSIS[:300]]
    b = [S.congested_user(i, "F-2", 1.0) for i in IMSIS[:300]]
    assert a != b


def test_default_share_is_the_14_share(fresh_fault):
    """`share` 를 안 주면 14번 몫(`congestion_user_share` 0.6)이다 — 23번 설정을 바꿔도 14번이 움직이지 않는다."""
    fresh_fault.crowd_user_share = 0.1
    got = sum(S.congested_user(i, "F-136", 1.0) for i in IMSIS) / len(IMSIS)
    assert got == pytest.approx(0.6, abs=0.05)


def test_explicit_share_overrides_the_default(fresh_fault):
    got = sum(S.congested_user(i, "F-132", 1.0, share=0.3) for i in IMSIS) / len(IMSIS)
    assert got == pytest.approx(0.3, abs=0.05)


def test_zero_share_means_nobody_and_full_share_means_everybody(fresh_fault):
    assert not any(S.congested_user(i, "F-1", 1.0, share=0.0) for i in IMSIS[:300])
    assert all(S.congested_user(i, "F-1", 1.0, share=1.0) for i in IMSIS[:300])


def test_higher_intensity_only_adds_people(fresh_fault):
    """강도가 오르면 같은 순위의 사람이 차례로 더해진다 — 이미 나빠진 사람이 좋아지지 않는다."""
    low = {i for i in IMSIS[:600] if S.congested_user(i, "F-9", 0.4, share=0.6)}
    high = {i for i in IMSIS[:600] if S.congested_user(i, "F-9", 1.0, share=0.6)}
    assert low <= high and len(low) < len(high)


def test_intensity_above_one_is_capped(fresh_fault):
    one = [S.congested_user(i, "F-9", 1.0, share=0.6) for i in IMSIS[:300]]
    two = [S.congested_user(i, "F-9", 2.0, share=0.6) for i in IMSIS[:300]]
    assert one == two


def test_zero_intensity_means_nobody(fresh_fault):
    assert not any(S.congested_user(i, "F-9", 0.0, share=1.0) for i in IMSIS[:300])


# ---------------------------------------------------------------- sa_fallback_call_boost (17번 · 11-06)
#
# 넘어온 단말(폴백 직후 재등록·쌓인 전송)의 호를 평소보다 늘려 eNB 15분 표본이 N-01 판정 하한(20)에 닿게 한다.
# 일반 호로 채우면 11-05 처럼 구성비 신호가 묻히므로 **넘어온 단말의 호만** 늘린다. 2배는 초기값이다.

def test_sa_fallback_call_boost_default_is_the_initial_value_2():
    assert S.FaultCfg().sa_fallback_call_boost == 2.0


def test_repo_yaml_sa_fallback_call_boost_is_accepted_and_2():
    assert S.FaultCfg.from_cfg(_repo_fault_cfg()).sa_fallback_call_boost == 2.0


def test_sa_fallback_call_boost_is_read_as_a_float():
    assert S.FaultCfg.from_cfg({"sa_fallback_call_boost": "3"}).sa_fallback_call_boost == 3.0


@pytest.mark.parametrize("bad", [0, 0.5, 0.99, -1])
def test_a_boost_under_one_is_refused(bad):
    """1 미만이면 넘어온 단말의 호가 오히려 줄어 신호가 약해진다 — 오타로 조용히 돌지 않게 멈춘다."""
    with pytest.raises(RuntimeError, match="sa_fallback_call_boost"):
        S.FaultCfg.from_cfg({"sa_fallback_call_boost": bad})


def test_a_boost_of_exactly_one_means_no_boost_and_is_allowed():
    assert S.FaultCfg.from_cfg({"sa_fallback_call_boost": 1}).sa_fallback_call_boost == 1.0
