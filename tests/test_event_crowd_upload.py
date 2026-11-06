"""23번 — 행사장 SNS 사진 업로드(`_sns_photo`)는 **처리율을 바꾸지 않는다** (2026-11-03 M-3).

업로드 크기를 키운 것은 graphio 업링크 속도 표본(1MB 하한)에 들어오게 하려는
것이 아니라 행사장 SNS 의 실제 모양이다. 처리율은 변형기가 정한
(1+I)/(1+3I) 배 그대로여야 한다 — 크기로 처리율을 움직이면 Q-09 가 서는지가
혼잡 세기가 아니라 업로드 크기에 달리게 된다.

**DB 를 쓰지 않는다.** `Plan` 만 만든다.
"""

from __future__ import annotations

import random
from types import SimpleNamespace

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


def test_only_sns_sessions_grow_and_rate_matches_others(monkeypatch):
    """같은 난수로 돌려 SNS 만 up 바이트·시간이 늘고 처리율은 다른 등급과 같다.

    혼잡 몫(`crowd_user_share`)은 0 으로 둔다 — 그 사람들은 처리율을 범위에서 **뽑아** 정하므로(`_crowd_choke`)
    SNS 가 사진 때문에 난수를 더 쓰면 같은 시드라도 뽑는 값이 달라진다. 이 테스트가 지키는 것은 사진이 처리율을
    바꾸지 않는다는 것이고, 몫 경로는 아래 별도 테스트가 본다."""
    monkeypatch.setattr(S.FAULT, "crowd_user_share", 0.0)
    mut = S.PLAN_MUTATORS["event_crowd"]
    # 혼잡 몫(`congested_user`)이 장애 ID 와 IMSI 로 사람을 고정하므로 둘 다 있어야 한다(11-06 · 몫 60% 추가).
    # 같은 사람을 두 등급에 태워야 "SNS 여부가 발동을 바꾸지 않는다"를 볼 수 있다
    sc = SimpleNamespace(id="F-T")
    grew = 0
    for seed in range(200):
        sns, pay = _plan("SNS"), _plan("PAY")
        sns.imsi = pay.imsi = f"4401{seed:011d}"
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


def test_a_congested_person_is_choked_into_the_up_range_whatever_the_grade(monkeypatch):
    """몫에 든 사람(`crowd_user_share` 1.0)은 등급과 무관하게 업링크가 설정 범위(50~150kbps)로 떨어진다."""
    monkeypatch.setattr(S.FAULT, "crowd_user_share", 1.0)
    mut = S.PLAN_MUTATORS["event_crowd"]
    sc = SimpleNamespace(id="F-T")
    lo, hi = S.FAULT.crowd_up_bps
    n = 0
    for seed in range(100):
        for g in ("SNS", "PAY"):
            p = _plan(g)
            p.imsi = f"4401{seed:011d}"
            if not mut(p, sc, 1.0, random.Random(seed), None):
                continue
            size, _, t = _up(p)
            bps = size * 8 / (t / 1e6)
            assert lo * 0.999 <= bps <= hi * 1.001, f"{g} {bps:.0f}bps 가 범위 밖"
            n += 1
    assert n >= 100
