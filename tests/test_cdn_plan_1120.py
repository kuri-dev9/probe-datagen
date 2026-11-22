"""21번 CDN 장애 세션 `scenarios._cdn_plan` — 서버에 붙지 못한다 · 다운 방향은 건드리지 않는다 (2026-11-20 · 맥스 구현).

사용자: 「CDN 에서 장애가 났는데 어떻게 다운링크가 발생하겠어. 없는 게 정상이거나 UP 쪽이 문제」.
22번은 `_app_session_broken` 을 그대로 쓴다(다운 열화). DB 없이 — 세션 계획(`Plan`) 객체에 직접 부른다.
"""
from __future__ import annotations

import json
import random
from pathlib import Path
from types import SimpleNamespace

import pytest

from datagen import scenarios as S

ROOT = Path(__file__).resolve().parents[1]


def _plan():
    return S.Plan({"Traffic_Total_Dnlink_PayloadSize": 1000.0, "Traffic_Total_Dnlink_PayloadPacketCount": 100.0,
                   "Traffic_Total_Dnlink_RetransPacketCount": 1.0, "Traffic_Total_Uplink_PayloadSize": 500.0,
                   "Traffic_Total_Uplink_PayloadPacketCount": 50.0, "Traffic_Total_Uplink_RetransPacketCount": 0.0,
                   "DnlinkRTTCount": 20.0, "DnlinkRTTTime": 0.2, "UplinkRTTCount": 20.0, "UplinkRTTTime": 0.2},
                  {}, "OK", 3)


def _many(intensity=1.0, n=400, fn=None):
    fn = fn or S._cdn_plan
    out = []
    for seed in range(n):
        p = _plan()
        before = dict(p.totals)
        fn(p, random.Random(seed), intensity, SimpleNamespace())
        out.append((before, p))
    return out


def _down(d):
    return {k: v for k, v in d.items() if "Dnlink" in k}


def _up(d):
    return {k: v for k, v in d.items() if "Uplink" in k}


def test_downlink_values_are_never_touched():
    for before, p in _many():
        assert _down(p.totals) == _down(before)


def test_about_half_of_the_sessions_fail_to_connect_at_full_intensity():
    got = _many()
    frac = sum(1 for _b, p in got if p.syn_fail) / len(got)
    assert 0.42 <= frac <= 0.58, frac


def test_connect_failure_scales_with_the_intensity():
    hi = sum(p.syn_fail for _b, p in _many(1.0)) / 400
    lo = sum(p.syn_fail for _b, p in _many(0.4)) / 400
    assert lo < hi and 0.12 <= lo <= 0.28, (lo, hi)


def test_a_session_that_connected_gets_uplink_delay_and_retransmission():
    got = [(b, p) for b, p in _many() if not p.syn_fail]
    assert got
    for before, p in got:
        assert p.totals["Traffic_Total_Uplink_RetransPacketCount"] > before["Traffic_Total_Uplink_RetransPacketCount"]
        assert p.totals["UplinkRTTTime"] / p.totals["UplinkRTTCount"] > before["UplinkRTTTime"] / before["UplinkRTTCount"]


def test_a_failed_session_only_gets_the_syn_flag():
    got = [(b, p) for b, p in _many() if p.syn_fail]
    assert got
    for before, p in got:
        assert p.totals == before


def test_uplink_retransmission_is_capped():
    for before, p in _many(1.0):
        if not p.syn_fail:
            rate = p.totals["Traffic_Total_Uplink_RetransPacketCount"] / before["Traffic_Total_Uplink_PayloadPacketCount"]
            assert rate <= 0.30 + 1e-9


def test_fade_is_registered_like_the_22_function():
    p = _plan()
    sc = SimpleNamespace()
    S._cdn_plan(p, random.Random(1), 0.8, sc)
    assert p.fade is not None and p.fade[0] is sc and p.fade[1] == 0.8
    p2 = _plan()
    S._cdn_plan(p2, random.Random(1), 0.0, SimpleNamespace())
    assert p2.fade is None
    p3 = _plan()
    S._cdn_plan(p3, random.Random(1), 1.0)           # sc 없음 — 옛 동작과 같게 걷히지 않는다
    assert p3.fade is None


def test_retries_follow_the_22_rule():
    got = [p.retries for _b, p in _many(1.0)]
    assert any(r for r in got) and max(got) <= 4


def test_22_still_degrades_the_downlink():
    """22번은 그대로다 — 다운 방향이 나빠진다(21번과 갈라졌다는 반대 증거)"""
    changed = 0
    for seed in range(100):
        p = _plan()
        before = dict(p.totals)
        S._app_session_broken(p, random.Random(seed), 1.0)
        changed += _down(p.totals) != _down(before)
    assert changed > 50


def test_expect_kpi_is_u01_in_the_reports_and_the_code():
    for name in ("scenario_report.json", "scenario_report_v50.json"):
        d = json.loads((ROOT / "config" / name).read_text(encoding="utf-8"))
        s21 = [x for x in d["scenarios"] if x["no"] == 21][0]
        assert s21["expect"]["kpi"] == "U-01", name
    src = (ROOT / "src/datagen/scenarios.py").read_text(encoding="utf-8")
    assert '"cdn_degradation"' in src and "U-01" in src
