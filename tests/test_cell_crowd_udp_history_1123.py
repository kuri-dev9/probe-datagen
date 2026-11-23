"""1번 셀·분 단위 혼잡 · 16번 UDP 응답 없음 · 장비 대상 과거 복사 요청 (2026-11-23 · 동구 구현 · 에스터 시험).

사용자 「사람 단위로 하지 마 — 대부분 셀 단위」「16번은 down 이 거의 0 이고 연결 요청만 쌓이는 모양」「장비가 평가 대상이면 그 장비
아래 셀의 과거 정상」. 1번은 대상 셀의 세션이 전부 `_congest(강도)` 를 받는다(`congested_user` 60% 추첨 없음). 행사장 앱 먹통만 50% 사람
단위가 남았다(결정 대기). 16번은 UDP(QUIC) 세션의 `syn_fail` 이 「응답 없음」 갈래다. DB 없이.
"""
from __future__ import annotations

import inspect
import random
from types import SimpleNamespace

import pytest

from datagen import cli, session
from datagen import scenarios as S


@pytest.fixture
def rec(monkeypatch):
    calls = {"congest": [], "fade": 0}
    monkeypatch.setattr(S, "_congest", lambda plan, rng, lv: calls["congest"].append(lv))
    monkeypatch.setattr(S, "_fade_on", lambda plan, sc: calls.__setitem__("fade", calls["fade"] + 1))
    return calls


class _NoCoin(random.Random):
    def random(self):
        raise AssertionError("세션마다 동전을 던졌다")


def _plan(outcome="normal"):
    return SimpleNamespace(outcome=outcome, span=5, stall=False, imsi="440000000000001")


# 1. 1번 — 대상 셀의 세션이 전부 혼잡하고 강도는 「얼마나」에만 쓰인다
def test_every_session_of_the_target_cell_is_congested_for_the_rush_hour_fault(rec):
    sc = SimpleNamespace(id="F-1")
    for i in range(300):
        p = _plan()
        p.imsi = f"44000000000{i:04d}"                    # 사람이 달라도 같다 — IMSI 로 고르지 않는다
        assert S._ran_congestion_users(p, sc, 0.7, _NoCoin(), None) is True
    assert rec["congest"] == [0.7] * 300 and rec["fade"] == 300


def test_zero_intensity_and_stall_sessions_are_skipped(rec):
    sc = SimpleNamespace(id="F-1")
    assert S._ran_congestion_users(_plan(), sc, 0.0, _NoCoin(), None) is False
    assert S._ran_congestion_users(_plan("stall"), sc, 1.0, _NoCoin(), None) is False
    assert rec["congest"] == [] and rec["fade"] == 0


# 2. 행사장 특정 앱 먹통은 그대로 사람 단위 50% (회귀 — 결정 대기)
def test_event_crowd_keeps_the_person_level_half(monkeypatch):
    src = inspect.getsource(S)
    assert "if not congested_user(plan.imsi, sc.id, intensity, share=0.5):" in src
    got = [S.congested_user(f"44000000000{i:05d}", "F-9", 1.0, share=0.5) for i in range(2000)]
    assert 0.42 < sum(got) / len(got) < 0.58
    assert S.congested_user("440000000000001", "F-9", 1.0, share=0.5) == S.congested_user("440000000000001", "F-9", 1.0, share=0.5)
    assert "congested_user(" not in inspect.getsource(S._ran_congestion_users), "1번은 더 이상 사람으로 고르지 않는다"


# 3. UDP 응답 없음 — up 1~3개·약 1,200B·재전송 = 개수 − 1 · down 전부 0
def _traffic():
    return [10.0] * len(session.TRAFFIC_PAIRS)


def test_udp_no_answer_has_only_connection_requests_and_no_downlink():
    for seed in range(200):
        out = dict(zip(session.TRAFFIC_PAIRS, session._udp_no_answer(_traffic(), random.Random(seed))))
        assert all(v == 0.0 for k, v in out.items() if k.startswith("Dnlink")), out
        n = out["Uplink_PacketCount"]
        assert 1 <= n <= 3 and n == out["Uplink_PayloadPacketCount"]
        assert out["Uplink_RetransPacketCount"] == n - 1
        assert 1200 * n <= out["Uplink_PayloadSize"] <= 1252 * n
        assert out["Uplink_ThroughputTime"] == 0.0


# 4. 16번 — 대상 앱 세션은 모두 syn_fail 이고 재시도는 1~(1+3·강도), 재시도의 재시도는 없다
def _cdn_plan_obj():
    return S.Plan({k: 10.0 for k in session.TRAFFIC_PAIRS} | {"DnlinkRTTCount": 20.0, "DnlinkRTTTime": 0.2,
                                                          "UplinkRTTCount": 20.0, "UplinkRTTTime": 0.2}, {}, "OK", 3)


@pytest.mark.parametrize("intensity", [0.4, 1.0])
def test_cdn_plan_fails_every_session_and_retries_within_range(intensity):
    seen = set()
    for seed in range(300):
        p = _cdn_plan_obj()
        S._cdn_plan(p, random.Random(seed), intensity, SimpleNamespace())
        assert p.syn_fail is True
        assert 1 <= p.retries <= 1 + int(3 * intensity), p.retries
        seen.add(p.retries)
    assert len(seen) >= 2 or intensity < 0.5
    assert "retries" in inspect.getsource(session) and "_open" in inspect.getsource(session)


def test_tcp_syn_fail_path_is_still_there_and_udp_branch_is_only_for_syn_fail():
    src = inspect.getsource(session)
    assert "elif syn_fail:" in src and "_udp_no_answer(traffic, rng)" in src
    assert "if not (syn_fail or (tcp and tls_fail)) and rng.random() < self.p_http:" in src, "응답 없음 세션은 HTTP 기록이 없다"


# 5. 과거 복사 요청 — 셀 목록이 있으면 장비 대상도 보낸다 · 셀 목록이 없으면 보내지 않는다
def _t(cells, **kw):
    base = dict(cell_keys=list(cells), sgw_id=None, pgw_id=None, mme_id=None, mme_group=None)
    return SimpleNamespace(**(base | kw))


def test_history_request_goes_for_equipment_targets_with_cells_only():
    assert cli._history_cell_target(_t(["1-1", "1-2"])) is True
    for eq in ({"pgw_id": "0014"}, {"sgw_id": "0037"}, {"mme_id": "0001"}, {"mme_group": ["0001", "0002"]}):
        assert cli._history_cell_target(_t(["1-1"], **eq)) is True, eq
        assert cli._history_cell_target(_t([], **eq)) is False, eq
    assert cli._history_cell_target(_t([])) is False
