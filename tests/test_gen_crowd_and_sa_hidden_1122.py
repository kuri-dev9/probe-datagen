"""gen 수정 둘의 고정 (2026-11-22 · 맥스 수정 · 케이 승인).

1. 23번 `scenarios._event_crowd` — 정체를 **세션마다 추첨하지 않고 사람(IMSI)으로 고정**한다(F-501 22:56~58 정체 꺼짐).
   몫은 그대로 `0.5×강도`이고 `congested_user`(14번과 같은 함수)가 정한다. 재시도 세션·다른 앱은 끊지 않는다.
2. 17번 `pemdb1._make_sa_hidden` · `_sa_live_n` · `_sa_seat` — 넘어오는 5G 단말 기준이 「정식 풀 + 지금 추가 가입자」다
   (F-506 미탐: 추가 가입자 5만이 몫을 `sa_resident_share` 35%→약 13% 로 묽혔다). 최대치 기준으로 미리 만들고 지금 수만 쓴다.
DB·네트워크 없이 돈다.
"""
from __future__ import annotations

import dataclasses
import random
from types import SimpleNamespace as N

import pytest

from datagen import pemdb1 as P
from datagen import scenarios as S
from datagen.generator import Subscriber
from datagen.identity import synth_imsi

# ── 23번 ─────────────────────────────────────────────────────────────────────


class _Plan:
    def __init__(self, imsi, app="3035", retry=False, outcome="ok"):
        self.imsi, self.retry, self.outcome = imsi, retry, outcome
        self.retries, self.dn_stall = 0, False
        self.v = {"App_ID": app, "Traffic_Total_Dnlink_PayloadSize": 900, "Traffic_Total_Dnlink_PayloadPacketCount": 9,
                  "Traffic_Total_Dnlink_RetransPacketCount": 1}

    def text(self, k):
        return str(self.v.get(k, ""))

    def set(self, k, v):
        self.v[k] = v

    def scale(self, name, factor):        # 11-26 — 시나리오 적용이 수치를 곱하는 길(`scenarios.Plan.scale`)을 가짜도 갖는다
        self.v[name] = self.v.get(name, 0) * factor


SC = N(id="F-501", target=S.Target(cell_keys={"x"}, app_id="3035"))
IMSIS = [f"44010{i:010d}" for i in range(4000)]


def _hit(imsi, intensity=1.0, sc=SC, seed=1, **kw):
    return S._event_crowd(_Plan(imsi, **kw), sc, intensity, random.Random(seed), None)


@pytest.mark.parametrize("intensity", [0.4, 0.5, 1.0])
def test_the_share_is_half_of_the_intensity(intensity):
    share = sum(_hit(m, intensity) for m in IMSIS) / len(IMSIS)
    assert abs(share - 0.5 * intensity) < 0.05, share


def test_the_same_imsi_always_gets_the_same_result_whatever_the_random_stream():
    for m in IMSIS[:300]:
        assert len({_hit(m, 1.0, seed=k) for k in range(15)}) == 1


def test_a_lower_intensity_picks_a_subset_of_the_higher_one():
    low = {m for m in IMSIS if _hit(m, 0.4)}
    high = {m for m in IMSIS if _hit(m, 1.0)}
    assert low and low <= high and len(low) < len(high)


def test_different_faults_pick_different_people():
    a = {m for m in IMSIS if _hit(m, 1.0)}
    b = {m for m in IMSIS if _hit(m, 1.0, sc=N(id="F-502", target=SC.target))}
    assert len(a & b) / len(a) < 0.7


def test_a_picked_session_is_cut_the_way_it_was_before():
    m = next(m for m in IMSIS if _hit(m))
    p = _Plan(m)
    assert S._event_crowd(p, SC, 1.0, random.Random(1), None) is True
    assert p.dn_stall and p.retries == 1
    assert p.v["Traffic_Total_Dnlink_PayloadSize"] == 0 and p.v["Traffic_Total_Dnlink_PayloadPacketCount"] == 0
    assert p.v["Traffic_Total_Dnlink_RetransPacketCount"] == 0           # down 재전송은 넣지 않는다(U-12 가 오르면 R-41 이 양보)


def test_retry_sessions_other_apps_and_already_stalled_sessions_are_left_alone():
    assert not any(_hit(m, retry=True) for m in IMSIS[:300])             # 재시도는 끊지 않는다(판정 표본을 남긴다)
    assert not any(_hit(m, app="9") for m in IMSIS[:300])
    assert not any(_hit(m, outcome="stall") for m in IMSIS[:300])
    assert not S._event_crowd(_Plan(IMSIS[0]), N(id="F-1", target=S.Target(cell_keys={"x"})), 1.0, random.Random(1), None)   # 대상 앱 없음


def test_the_picker_is_the_shared_congested_user_with_half_share():
    for m in IMSIS[:200]:
        assert _hit(m, 0.7) == S.congested_user(m, "F-501", 0.7, share=0.5)


# ── 17번 숨은 5G 단말 ────────────────────────────────────────────────────────

BASE, EXMAX, SHARE = 600, 2000, 0.35


def _sub(i):
    kw = {f.name: "0" for f in dataclasses.fields(Subscriber)
          if f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING}
    s = Subscriber(**kw)
    s.imsi, s.mcc_mnc, s.vendor, s.model = f"44010{i:010d}", "4401012", "Apple", "iPhone"
    return s


def _gen(extra_max, extra_n=0):
    cells = [N(cell_key=f"{e}-{c}") for e in range(30) for c in range(4)]
    g = object.__new__(P.PemdbGen)
    g.gen = N(subs=[_sub(i) for i in range(BASE)], cells=cells, cell_weights=[1.0 + (i % 7) for i in range(len(cells))])
    g.extra_subs = [None] * extra_max
    g.extra_n = extra_n
    g.sa_hidden = g._make_sa_hidden(SHARE, 7)
    return g


def _placed(g):
    return {s.imsi: k for k, subs in g.sa_hidden.items() for s in subs}


def test_the_hidden_count_follows_regular_plus_max_extra():
    assert sum(len(v) for v in _gen(0).sa_hidden.values()) == int(SHARE * BASE)
    assert sum(len(v) for v in _gen(EXMAX).sa_hidden.values()) == int(SHARE * (BASE + EXMAX))


def test_the_original_hidden_people_keep_their_imsi_and_cell():
    """앞 `share × 정식 풀` 명은 추가 가입자 최대치와 무관하게 같은 사람·같은 셀이다 — 평소 레코드가 그대로다."""
    before, after = _placed(_gen(0)), _placed(_gen(EXMAX))
    assert before and all(after.get(i) == c for i, c in before.items())


def test_hidden_imsis_never_collide_with_regular_or_extra_subscribers_or_each_other():
    g = _gen(EXMAX)
    hidden = [s.imsi for subs in g.sa_hidden.values() for s in subs]
    regular = {synth_imsi("4401012", i) for i in range(BASE)}
    extra = {synth_imsi("4401012", 2 * BASE + j) for j in range(EXMAX)}
    assert len(hidden) == len(set(hidden))
    assert not regular & set(hidden) and not extra & set(hidden)


def test_every_hidden_subscriber_is_five_g_capable_and_not_a_pool_member():
    g = _gen(EXMAX)
    subs = [s for v in g.sa_hidden.values() for s in v]
    assert all(s.dcnr == "1" for s in subs) and all(g._sa_rank[s.imsi] >= 0 for s in subs)
    assert sorted(g._sa_rank.values()) == list(range(len(subs)))         # 순번은 0..n-1 한 번씩


@pytest.mark.parametrize("extra_n", [0, 1000, EXMAX])
def test_the_live_share_of_all_people_stays_at_the_configured_share(extra_n):
    g = _gen(EXMAX, extra_n)
    live = sum(1 for v in g.sa_hidden.values() for s in v if g._sa_rank[s.imsi] < g._sa_live_n())
    assert live == g._sa_live_n() == int(SHARE * (BASE + extra_n))
    assert abs(live / (BASE + extra_n) - SHARE) < 0.002


def test_with_no_extra_subscribers_the_old_number_is_used():
    assert _gen(EXMAX, 0)._sa_live_n() == int(SHARE * BASE)


def test_live_count_without_a_share_is_zero():
    g = object.__new__(P.PemdbGen)
    g.gen = N(subs=[_sub(0)])
    assert g._sa_live_n() == 0


def _seat(g, peak=1.0, cells=None):
    keys = sorted(cells or {c.cell_key for c in g.gen.cells})
    by_key = {k: N(cell_key=k) for k in keys}
    g._sa_in = {}
    g.anchor = None
    sc = type("Sc", (), {})()
    sc.id, sc.start_minute, sc.peak, sc.target = "F-506", 0, peak, N(cell_keys=set(keys))
    g._sa_seat(sc, by_key)
    return sc


@pytest.mark.parametrize("extra_n", [0, EXMAX])
def test_seating_picks_only_people_inside_the_live_count(extra_n):
    g = _gen(EXMAX, extra_n)
    sc = _seat(g)
    assert sc._sa_n == g._sa_live_n() == len(g._sa_in)
    assert all(g._sa_rank[i] < g._sa_live_n() for i in g._sa_in)
    assert sc._sa_imsis == sorted(g._sa_in)


def test_more_extra_subscribers_bring_more_people_across_in_the_same_proportion():
    small, big = _seat(_gen(EXMAX, 0))._sa_n, _seat(_gen(EXMAX, EXMAX))._sa_n
    assert big > small and abs(big / (BASE + EXMAX) - small / BASE) < 0.01


def test_seating_is_deterministic_for_a_fault_and_scales_with_peak():
    a, b = _gen(EXMAX, 500), _gen(EXMAX, 500)
    assert _seat(a)._sa_imsis == _seat(b)._sa_imsis
    full, half = _seat(_gen(EXMAX, 500), 1.0)._sa_n, _seat(_gen(EXMAX, 500), 0.5)._sa_n
    assert abs(half - round(full * 0.5)) <= 1


def test_zero_share_makes_no_hidden_people():
    g = object.__new__(P.PemdbGen)
    g.gen = N(subs=[_sub(i) for i in range(10)], cells=[], cell_weights=[])
    g.extra_subs = []
    assert g._make_sa_hidden(0.0, 7) == {}
    with pytest.raises(RuntimeError):
        g._make_sa_hidden(1.5, 7)
