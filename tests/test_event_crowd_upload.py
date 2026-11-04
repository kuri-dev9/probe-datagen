"""23번 — 행사장 SNS 사진 업로드(`_sns_photo`)는 **처리율을 바꾸지 않는다** (2026-11-03 M-3).

업로드 크기를 키운 것은 graphio 업링크 속도 표본(1MB 하한)에 들어오게 하려는
것이 아니라 행사장 SNS 의 실제 모양이다. 처리율은 변형기가 정한
(1+I)/(1+3I) 배 그대로여야 한다 — 크기로 처리율을 움직이면 Q-09 가 서는지가
혼잡 세기가 아니라 업로드 크기에 달리게 된다.

**DB 를 쓰지 않는다.** `Plan` 만 만든다.
"""

from __future__ import annotations

import random

from datagen import scenarios as S
from datagen.session import CUM_PLAIN, TRAFFIC_PAIRS


def _plan(group: str) -> S.Plan:
    totals = {f"Traffic_Total_{n}": 0.0 for n in TRAFFIC_PAIRS}
    totals.update({n: 0.0 for n in CUM_PLAIN})
    totals.update({
        "Traffic_Total_Uplink_PayloadSize": 200_000.0,
        "Traffic_Total_Uplink_PayloadPacketCount": 200.0,
        "Traffic_Total_Uplink_ThroughputTime": 2_000_000.0,
        "Traffic_Total_Dnlink_PayloadSize": 1_000_000.0,
        "Traffic_Total_Dnlink_PayloadPacketCount": 800.0,
        "Traffic_Total_Dnlink_ThroughputTime": 1_000_000.0,
    })
    return S.Plan(totals, {"App_Group_Code": group}, "normal", 3)


def _up(plan):
    return (plan.get("Traffic_Total_Uplink_PayloadSize"),
            plan.get("Traffic_Total_Uplink_PayloadPacketCount"),
            plan.get("Traffic_Total_Uplink_ThroughputTime"))


def test_photo_keeps_rate_and_bytes_per_packet():
    p = _plan("SNS")
    size0, pkts0, t0 = _up(p)
    S._sns_photo(p, random.Random(1))
    size, pkts, t = _up(p)
    add = size - size0
    lo, hi = S.SNS_PHOTO_BYTES
    assert lo <= add <= hi
    assert abs(size / t - size0 / t0) < 1e-9 * (size0 / t0), "처리율이 바뀌었다"
    assert abs(size / pkts - size0 / pkts0) < 1e-6, "바이트/패킷 비가 바뀌었다"


def test_photo_skips_when_no_packets():
    p = _plan("SNS")
    p.set("Traffic_Total_Uplink_PayloadPacketCount", 0)
    before = _up(p)
    S._sns_photo(p, random.Random(1))
    assert _up(p) == before


def test_only_sns_sessions_grow_and_rate_matches_others():
    """같은 난수로 돌려 SNS 만 up 바이트·시간이 늘고 처리율은 다른 등급과 같다."""
    mut = S.PLAN_MUTATORS["event_crowd"]
    sc = object()
    grew = 0
    for seed in range(200):
        sns, pay = _plan("SNS"), _plan("PAY")
        a = mut(sns, sc, 1.0, random.Random(seed), None)
        b = mut(pay, sc, 1.0, random.Random(seed), None)
        assert a == b, "SNS 여부가 발동 여부를 바꿨다"
        if not a:
            continue
        s_sz, _, s_t = _up(sns)
        p_sz, _, p_t = _up(pay)
        assert s_sz > p_sz and s_t > p_t, "SNS 세션 업로드가 늘지 않았다"
        assert abs(s_sz / s_t - p_sz / p_t) < 1e-9 * (p_sz / p_t), "처리율이 갈라졌다"
        grew += 1
    assert grew >= 100, f"{grew}회만 발동했다"
