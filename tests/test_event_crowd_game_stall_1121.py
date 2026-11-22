"""23번 「행사장 특정 앱 먹통」(2026-11-21 오후 재정의) — 대상 게임의 내려받기만 끊긴다.

**DB 를 쓰지 않는다.** `Plan` 과 `Scenario` 만 만든다. 판정 가능 조건(`PAYPKT_DN ≥ 10`)은 graphio 몫이라
여기서는 「끊긴 세션은 재시도를 하나 열고 재시도는 끊지 않는다」까지만 본다.
"""

from __future__ import annotations

import random
from types import SimpleNamespace

from datagen import scenarios as S
from datagen.session import CUM_PLAIN, TRAFFIC_PAIRS

GAME = "3035"        # 대상 게임 앱 ID (문자열 — Target.app_id 가 str)


def _plan(app_id: int = GAME, outcome: str = "normal") -> S.Plan:
    totals = {f"Traffic_Total_{n}": 0.0 for n in TRAFFIC_PAIRS}
    totals.update({n: 0.0 for n in CUM_PLAIN})
    totals.update({
        "Traffic_Total_Uplink_PayloadSize": 200_000.0,
        "Traffic_Total_Uplink_PayloadPacketCount": 200.0,
        "Traffic_Total_Uplink_ThroughputTime": 2_000_000.0,
        "Traffic_Total_Dnlink_PayloadSize": 1_000_000.0,
        "Traffic_Total_Dnlink_PayloadPacketCount": 800.0,
        "Traffic_Total_Dnlink_RetransPacketCount": 5.0,
        "Traffic_Total_Dnlink_ThroughputTime": 1_000_000.0,
    })
    return S.Plan(totals, {"App_ID": str(app_id), "App_Group_Code": "GAME"}, outcome, 3)


def _sc(app_id=GAME):
    return SimpleNamespace(id="F-T", target=SimpleNamespace(app_id=app_id))


def test_spec_is_q04_with_r41_and_no_detect_floor_for_q09():
    sp = S.KIND_SPECS["event_crowd"]
    assert sp["expect_kpi"] == "Q-04" and "R-41" in sp["expect_rules"]
    assert "transport" in sp["series"] and sp["scope_kind"] == "CELL"
    assert "detect_min" not in sp                       # Q-09 용 하한은 걷었다


def test_target_game_session_loses_down_but_keeps_up_and_time():
    mut = S.PLAN_MUTATORS["event_crowd"]
    hit = 0
    for seed in range(200):
        p = _plan()
        p.imsi = f"44010{seed * 7919:010d}"       # 정체는 사람(IMSI)으로 고정이다(11-22) — 시드마다 다른 사람
        up0 = (p.get("Traffic_Total_Uplink_PayloadSize"), p.get("Traffic_Total_Uplink_PayloadPacketCount"))
        t0 = p.get("Traffic_Total_Dnlink_ThroughputTime")
        if not mut(p, _sc(), 1.0, random.Random(seed), None):
            continue
        hit += 1
        assert p.dn_stall is True and p.retries >= 1
        for f in ("Traffic_Total_Dnlink_PayloadSize", "Traffic_Total_Dnlink_PayloadPacketCount",
                  "Traffic_Total_Dnlink_RetransPacketCount"):
            assert p.get(f) == 0, f           # 재전송도 0 — 늘면 U-12 가 올라 R-09 로 양보한다
        assert p.get("Traffic_Total_Dnlink_ThroughputTime") == t0       # 기다린 시간은 남는다
        assert (p.get("Traffic_Total_Uplink_PayloadSize"), p.get("Traffic_Total_Uplink_PayloadPacketCount")) == up0
    assert 70 <= hit <= 130, f"절반 안팎이어야 한다: {hit}/200"       # 0.5 × 강도


def test_other_apps_and_unset_target_are_untouched():
    mut = S.PLAN_MUTATORS["event_crowd"]
    for seed in range(50):
        p = _plan(app_id="3001")
        assert not mut(p, _sc(), 1.0, random.Random(seed), None) and p.dn_stall is False
        q = _plan()
        assert not mut(q, _sc(app_id=None), 1.0, random.Random(seed), None)


def test_a_retry_session_and_a_stall_outcome_are_never_stalled_again():
    mut = S.PLAN_MUTATORS["event_crowd"]
    for seed in range(100):
        r = _plan(); r.retry = True
        assert not mut(r, _sc(), 1.0, random.Random(seed), None)
        o = _plan(outcome="stall")
        assert not mut(o, _sc(), 1.0, random.Random(seed), None)


def test_zero_intensity_never_stalls():
    mut = S.PLAN_MUTATORS["event_crowd"]
    assert not any(mut(_plan(), _sc(), 0.0, random.Random(s), None) for s in range(100))


def test_app_load_grows_only_the_target_game_and_matches_the_crowd_share():
    sc = SimpleNamespace(target=SimpleNamespace(app_id=GAME))
    other = SimpleNamespace(idx="3001")
    game = SimpleNamespace(idx=GAME)
    assert S._event_app_load(sc, 1.0, other) == 1.0                    # SNS 3배는 걷었다
    assert S._event_app_load(sc, 1.0, game) == 3.0                     # 원래 몫을 모르면 사람 배율 그대로
    sc._app_share = 0.1                                                # 몫 10% → 늘어난 사람 2/3 가 그 게임
    x = S._event_app_load(sc, 1.0, game)
    share = 0.1 * x / (0.1 * x + 0.9)
    assert abs(share - 2 / 3) < 1e-9


def test_old_upload_devices_remain_but_are_not_called_by_the_mutator(monkeypatch):
    called = []
    for name in ("_crowd_uplink", "_crowd_choke", "_sns_photo"):
        assert hasattr(S, name)
        monkeypatch.setattr(S, name, lambda *a, _n=name, **k: called.append(_n))
    mut = S.PLAN_MUTATORS["event_crowd"]
    for seed in range(30):
        mut(_plan(), _sc(), 1.0, random.Random(seed), None)
    assert called == []


# ---------------------------------------------------------------- 23번은 하한을 채우지 않는다 (11-21 오후 · F-490·F-491)

def _floor_drafts_src():
    import inspect
    from datagen import pemdb1
    cls = next(c for c in vars(pemdb1).values() if isinstance(c, type) and hasattr(c, "_floor_drafts"))
    return inspect.getsource(cls._floor_drafts)


def test_floor_drafts_skips_event_crowd_so_the_size_is_the_crowd_load_alone():
    """옛 하한 셀당 120호가 행사장 셀 사람을 평소 3배가 아니라 약 77배로 부풀렸다(新宿区 U-15 조각의 뿌리)."""
    src = _floor_drafts_src()
    i = src.index('if sc.kind == "event_crowd":')
    block = src[i:src.index('if sc.kind == "ran_congestion":')]
    assert block.rstrip().endswith("continue")            # 하한을 채우는 분기로 내려가지 않는다


def test_event_crowd_load_is_three_times_at_full_intensity_and_scales_with_it():
    assert S._event_crowd_load(None, 1.0) == 3.0 and S._event_crowd_load(None, 0.5) == 2.0 and S._event_crowd_load(None, 0.0) == 1.0
