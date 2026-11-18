"""걷히는 망가짐 — 앱 장애 세션(22번 결제 DNS · 21번 CDN)이 **레코드를 내는 분의 장애 강도**를 따른다 (2026-11-19 · 22번 F-416 꼬리).

전에는 `_app_session_broken` 이 세션을 연 분의 강도로 재전송·RTT·처리율을 **세션 수명(최대 10분) 내내** 고정했다 — DNS 가 평소로 돌아온
뒤에도 셀 하나가 같은 재전송을 매 분 냈다(F-416 `135530-1` 05:11~05:16). 이제 `Plan.fade` 에 망가지기 전 총량을 남기고 `session._put_faded` 가
분 가중(`그 분 강도 / 열 때 강도`, 0~1)으로 두 총량 사이를 잇는다. 누적은 줄지 않고 `ΣDelta == Total` 이다. 연결 결말·재시도는 세션을 열 때의
사건이라 그대로다. DB 없이 — 합성 셀 마스터(`test_disaster` 하니스). 한 분 값은 합성 마스터라 실제 셀과 양이 다르다 — **모양**을 고정한다.
"""
from __future__ import annotations

import random
from datetime import timedelta
from types import SimpleNamespace

import pytest

from datagen import pemdb1 as P
from datagen import scenarios as S
from datagen import session as SE
from datagen.apps import App, AppBook

import test_disaster as TD

START, DUR, MINUTES = 10, 11, 32                     # 장애 10~21분 (RAMP · 마지막 3분 ramp_out)
END = START + DUR                                   # 21 — 이 분부터 강도 0


# ── 단위: `_app_session_broken` ─────────────────────────────────────────

def _plan():
    return S.Plan({"Traffic_Total_Dnlink_PayloadSize": 1000.0, "Traffic_Total_Dnlink_PayloadPacketCount": 100.0,
                   "Traffic_Total_Dnlink_RetransPacketCount": 1.0, "Traffic_Total_Uplink_PayloadSize": 500.0,
                   "Traffic_Total_Uplink_PayloadPacketCount": 50.0, "Traffic_Total_Uplink_RetransPacketCount": 0.0},
                  {}, "OK", 3)


def test_plan_has_a_fade_slot_defaulting_to_none():
    assert _plan().fade is None
    assert "fade" in S.Plan.__slots__


def test_without_sc_the_old_behaviour_is_kept():
    p = _plan()
    S._app_session_broken(p, random.Random(1), 1.0)
    assert p.fade is None


def test_with_sc_the_pre_break_totals_are_kept_and_the_open_intensity():
    p = _plan()
    before = dict(p.totals)
    sc = SimpleNamespace()
    S._app_session_broken(p, random.Random(1), 0.8, sc)
    assert p.fade is not None
    got_sc, i0, base = p.fade
    assert got_sc is sc and i0 == 0.8
    assert base == before                                                      # 망가지기 전 총량
    assert p.totals != before                                                  # 망가진 총량은 따로


def test_zero_intensity_leaves_no_fade():
    p = _plan()
    S._app_session_broken(p, random.Random(1), 0.0, SimpleNamespace())
    assert p.fade is None


def test_fade_is_recorded_only_once():
    p = _plan()
    first = SimpleNamespace(tag="first")
    S._app_session_broken(p, random.Random(1), 0.9, first)
    S._app_session_broken(p, random.Random(2), 0.5, SimpleNamespace(tag="second"))
    assert p.fade[0] is first and p.fade[1] == 0.9


def test_only_pay_and_cdn_pass_the_scenario():
    import inspect
    assert "_app_session_broken(plan, rng, intensity, sc)" in inspect.getsource(S._pay_session)
    assert "_app_session_broken(plan, rng, intensity, sc)" in inspect.getsource(S._cdn_degradation_plan)
    src = inspect.getsource(S)
    assert src.count("_app_session_broken(") == 3                              # 정의 + 두 호출 — 다른 곳은 옛 동작


# ── 단위: `_fade_g` · 기본값 ────────────────────────────────────────────

def _g(segs, x):
    return SE.SessionEngine._fade_g(SimpleNamespace(fade_seg=segs), x)


def test_fade_g_integrates_weight_times_span():
    segs = [(0.0, 0.25, 1.0), (0.25, 0.5, 0.5), (0.5, 0.75, 0.0)]
    assert _g(segs, 0.0) == 0.0
    assert _g(segs, 0.25) == pytest.approx(0.25)
    assert _g(segs, 0.5) == pytest.approx(0.25 + 0.125)
    assert _g(segs, 0.75) == pytest.approx(0.375)                              # 가중 0 구간은 늘지 않는다
    assert _g(segs, 0.375) == pytest.approx(0.25 + 0.0625)


def test_fade_g_is_non_decreasing_and_bounded_by_x():
    segs = [(0.0, 0.2, 1.0), (0.2, 0.5, 0.3), (0.5, 0.9, 0.0), (0.9, 1.0, 0.7)]
    xs = [i / 50 for i in range(51)]
    gs = [_g(segs, x) for x in xs]
    assert all(b >= a - 1e-12 for a, b in zip(gs, gs[1:]))
    assert all(g <= x + 1e-9 for g, x in zip(gs, xs))                           # 가중 ≤ 1 이면 g ≤ x


def test_fade_g_beyond_known_segments_uses_the_last_weight():
    segs = [(0.0, 0.5, 0.5)]
    assert _g(segs, 1.0) == pytest.approx(0.25 + 0.5 * 0.5)
    assert _g([], 0.4) == pytest.approx(0.4)                                    # 아직 아무 분도 안 냈으면 가중 1


def test_session_defaults_mean_no_fade():
    import dataclasses
    f = {x.name: x for x in dataclasses.fields(SE.Session)}
    assert f["fade_sc"].default is None and f["fade_i0"].default == 0.0
    assert f["norm_traffic"].default == () and f["norm_plain"].default == ()
    assert f["fade_seg"].default_factory is list


# ── 모의 가동 ───────────────────────────────────────────────────────────

BOOK = AppBook([App("901", "PayApp", "PAY", "6", "", "", ("paidy.com",), 10.0),
                App("902", "VideoApp", "VIDEO", "6", "", "", ("kakaocdn.net",), 10.0),
                App("903", "WebApp", "WEB", "6", "", "", ("example.jp",), 80.0)])


def _run(kind: str, fade: bool, monkeypatch) -> dict:
    """합성 마스터에 장애를 넣고 32분 돌려 분별 값을 돌려준다. ``fade=False`` 는 `sc` 를 떼어 옛 동작."""
    monkeypatch.setattr(P, "load_cells", lambda *a, **k: TD._master())
    monkeypatch.setattr(P, "load_apps", lambda conn: BOOK)
    if not fade:
        orig = S._app_session_broken
        monkeypatch.setattr(S, "_app_session_broken", lambda plan, rng, i, sc=None: orig(plan, rng, i))
    weights: list[tuple[int, float, float]] = []                                  # (분, 열 때 강도, 가중)
    real_emit = SE.SessionEngine._emit

    def spy(self, ts, slot, s, out, mutate):
        n = len(s.fade_seg)
        real_emit(self, ts, slot, s, out, mutate)
        if s.fade_sc is not None and len(s.fade_seg) > n:
            weights.append((self.fade_minute, s.fade_i0, s.fade_seg[-1][2]))

    monkeypatch.setattr(SE.SessionEngine, "_emit", spy)
    g = P.PemdbGen(None, TD.ROOT / "data", rates={"lte_call": 1500, "transport": 1500}, seed=11, subscriber_pool=2500)
    g.anchor = TD.T0
    g.gen.app_book = BOOK
    row = {"fault_id": 1, "kind": kind, "target": None,
           "start_ts": TD.T0 + timedelta(minutes=START), "duration_min": DUR, "intensity": 1.0}
    scs, bad = g.set_fault_rows([row])
    assert scs and not bad
    sc = scs[0]
    ix = g.gen.index["transport"]
    grp = "PAY" if kind == "pay_dns_delay" else "VIDEO"
    seen: dict = {}
    dsum: dict = {}
    per = {m: {"re": 0, "pk": 0, "i": sc.intensity_for("transport", m)} for m in range(MINUTES)}
    inversions = ends = bad_ends = 0
    for m in range(MINUTES):
        ts = TD.T0 + timedelta(minutes=m)
        g.records(m, ts)
        for r in g.uplane_records(m, ts).get("transport", []):
            if r[ix["App_Group_Code"]] != grp:
                continue
            d_re = int(r[ix["Traffic_Delta_Dnlink_RetransPacketCount"]] or 0)
            per[m]["re"] += d_re
            per[m]["pk"] += int(r[ix["Traffic_Delta_Dnlink_PayloadPacketCount"]] or 0)
            sid, flag = r[ix["SessionID"]], r[ix["OngoingFlag"]]
            tot = int(r[ix["Traffic_Total_Dnlink_RetransPacketCount"]] or 0)
            prev = seen.get(sid)
            if prev is not None and tot < prev and flag not in ("0", "20"):
                inversions += 1
            seen[sid] = tot
            dsum[sid] = d_re if flag in ("0", "20") else dsum.get(sid, 0) + d_re
            if flag in ("2", "20"):
                ends += 1
                bad_ends += dsum[sid] != tot
    return {"per": per, "weights": weights, "inversions": inversions, "ends": ends, "bad_ends": bad_ends}


@pytest.fixture(scope="module")
def runs():
    mp = pytest.MonkeyPatch()
    out = {}
    try:
        for kind in ("pay_dns_delay", "cdn_degradation"):
            for fade in (True, False):
                with mp.context() as m:
                    out[(kind, fade)] = _run(kind, fade, m)
    finally:
        mp.undo()
    return out


def _rate(per, m):
    return 100.0 * per[m]["re"] / max(1, per[m]["pk"])


KINDS = ("pay_dns_delay", "cdn_degradation")


# ── 장애 창 안은 그대로 ─────────────────────────────────────────────────

@pytest.mark.parametrize("kind", KINDS)
def test_the_full_intensity_minutes_match_the_old_behaviour(runs, kind):
    new, old = runs[(kind, True)]["per"], runs[(kind, False)]["per"]
    for m in range(12, 18):                                                       # 강도 1.0 — 가중 1 이라 같다
        assert new[m]["i"] == pytest.approx(1.0)
        assert _rate(new, m) == pytest.approx(_rate(old, m), rel=0.02), m


@pytest.mark.parametrize("kind", KINDS)
def test_before_the_fault_nothing_changes(runs, kind):
    new, old = runs[(kind, True)]["per"], runs[(kind, False)]["per"]
    for m in range(0, 10):
        assert new[m] == old[m], m


# ── 창 다음 분부터 평소 ─────────────────────────────────────────────────

@pytest.mark.parametrize("kind", KINDS)
def test_after_the_fault_window_carried_sessions_return_to_the_normal_rate(runs, kind):
    new, old = runs[(kind, True)]["per"], runs[(kind, False)]["per"]
    for m in range(END, MINUTES):
        assert new[m]["i"] == 0.0
        assert _rate(new, m) < 0.02, (m, _rate(new, m))                          # 평소(0.00%대)
    assert _rate(old, END) > 1.0                                                  # 옛 동작은 창 끝 다음 분에도 높았다


def test_pay_old_behaviour_really_had_the_tail(runs):
    """전제 확인 — 고치기 전에는 창이 끝난 뒤 두 분까지 1% 넘게 남았다(F-416 꼬리의 모양)."""
    old = runs[("pay_dns_delay", False)]["per"]
    assert _rate(old, END) > 5.0 and _rate(old, END + 1) > 0.5


@pytest.mark.parametrize("kind", KINDS)
def test_ramp_out_minutes_are_below_the_old_values(runs, kind):
    new, old = runs[(kind, True)]["per"], runs[(kind, False)]["per"]
    for m in (19, 20):
        assert new[m]["i"] < 1.0
        assert _rate(new, m) <= _rate(old, m) + 1e-9, m


# ── 가중치 ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("kind", KINDS)
def test_weights_are_between_zero_and_one(runs, kind):
    w = runs[(kind, True)]["weights"]
    assert w and all(0.0 <= x[2] <= 1.0 for x in w)


@pytest.mark.parametrize("kind", KINDS)
def test_ramp_out_weights_follow_the_intensity_ratio(runs, kind):
    """열 때 강도 1.0 인 세션은 분 가중이 그 분 강도(0.75 · 0.5 · 0.25)와 같고 창 뒤는 0 이다."""
    w = [x for x in runs[(kind, True)]["weights"] if x[1] == pytest.approx(1.0)]
    by_min: dict[int, set] = {}
    for m, _, wt in w:
        by_min.setdefault(m, set()).add(round(wt, 6))
    for m, want in ((18, 0.75), (19, 0.5), (20, 0.25)):
        assert by_min.get(m) == {want}, (m, by_min.get(m))
    assert all(by_min[m] == {0.0} for m in by_min if m >= END)
    assert all(by_min[m] == {1.0} for m in by_min if 12 <= m <= 17)


@pytest.mark.parametrize("kind", KINDS)
def test_weight_is_capped_at_one_for_sessions_opened_during_the_ramp_up(runs, kind):
    low = [x for x in runs[(kind, True)]["weights"] if x[1] < 0.99]
    assert low, "램프 업 중에 연 세션이 있어야 한다"
    assert all(x[2] <= 1.0 for x in low)
    assert any(x[2] == 1.0 for x in low)                                           # 강도가 오르면 1 로 막힌다 — 열 때보다 더 망가지지 않는다


# ── 누적 규칙 ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("kind", KINDS)
def test_cumulative_totals_never_decrease(runs, kind):
    assert runs[(kind, True)]["inversions"] == 0


@pytest.mark.parametrize("kind", KINDS)
def test_sum_of_deltas_equals_total_at_the_end_record(runs, kind):
    r = runs[(kind, True)]
    assert r["ends"] > 1000 and r["bad_ends"] == 0


# ── 옛 동작 호환 ────────────────────────────────────────────────────────

def test_without_fade_minute_the_weight_is_one(monkeypatch):
    """`fade_minute` 가 없으면(옛 호출자) 가중 1 — 열 때 정한 양이 그대로다."""
    monkeypatch.setattr(P, "load_cells", lambda *a, **k: TD._master())
    monkeypatch.setattr(P, "load_apps", lambda conn: BOOK)
    eng = SE.SessionEngine
    assert eng.__init__.__code__.co_names.count("fade_minute") >= 1              # 엔진 생성 때 None 으로 둔다
    seen = []
    real_emit = eng._emit

    def spy(self, ts, slot, s, out, mutate):
        n = len(s.fade_seg)
        saved, self.fade_minute = self.fade_minute, None
        try:
            real_emit(self, ts, slot, s, out, mutate)
        finally:
            self.fade_minute = saved
        if s.fade_sc is not None and len(s.fade_seg) > n:
            seen.append(s.fade_seg[-1][2])

    monkeypatch.setattr(eng, "_emit", spy)
    g = P.PemdbGen(None, TD.ROOT / "data", rates={"lte_call": 800, "transport": 800}, seed=11, subscriber_pool=1500)
    g.anchor = TD.T0
    g.gen.app_book = BOOK
    row = {"fault_id": 1, "kind": "pay_dns_delay", "target": None, "start_ts": TD.T0 + timedelta(minutes=2),
           "duration_min": 11, "intensity": 1.0}
    scs, bad = g.set_fault_rows([row])
    assert scs and not bad
    for m in range(9):
        ts = TD.T0 + timedelta(minutes=m)
        g.records(m, ts)
        g.uplane_records(m, ts)
    assert seen and all(w == 1.0 for w in seen)


def test_engine_default_fade_minute_is_none():
    from datagen.session import SessionEngine
    import inspect
    assert "self.fade_minute = None" in inspect.getsource(SessionEngine.__init__)
