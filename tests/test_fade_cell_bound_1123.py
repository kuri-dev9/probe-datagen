"""셀 범위 장애의 세션 손실이 **대상 셀을 떠나면 · 장애가 끝나면 걷힌다** — 2026-11-23 (F-535 · 22번 연쇄 복합 장애 재주입).

전에는 손실을 세션이 열리는 순간 정해 수명 내내 고정했다. 대상 셀에서 열린 세션이 핸드오버로 다른 셀에 옮겨 가면 그 셀에서 재전송이 났고
(정답 밖 셀 125개 · F-534 新宿 셀 혼입), 장애가 끝난 뒤에도 몇 분 더 남았다(`_main_end` 미작동). 이제 `scenarios.FADE_CELL_BOUND` kind 는
`Plan.fade` 를 등록하고 `session` 이 그 분 셀이 대상 밖이면 가중 0 을 쓴다. 새 임계값은 없다.
DB 없이 — 합성 셀 마스터(`test_disaster` 하니스). 한 분 값은 합성이라 실제와 양이 다르다 — **모양**을 고정한다."""
from __future__ import annotations

import dataclasses
from datetime import timedelta
from types import SimpleNamespace

import pytest

from datagen import pemdb1 as P
from datagen import scenarios as S
from datagen import session as SE

import test_disaster as TD
import test_session_fade_1119 as TF

START, DUR, MINUTES = 10, 11, 32
END = START + DUR


# ── 단위: 목록 · `_fade_on` ─────────────────────────────────────────────

def test_the_cell_bound_kinds_are_exactly_the_documented_six():
    assert S.FADE_CELL_BOUND == {"transport_loss", "compound_causal_failure", "ran_congestion", "anchor_saturation",
                                 "capacity_shift", "natural_disaster"}


@pytest.mark.parametrize("kind", ["cdn_degradation", "pay_dns_delay", "event_crowd", "earthquake", "ho_interruption",
                                  "mobility", "gw_resource", "ue_model"])
def test_other_kinds_do_not_register_a_fade(kind):
    """장비·앱·기종 범위는 셀로 걷지 않는다. `event_crowd`(손실이 세션 표시)·옛 `earthquake`(지난 기록 재현)도 그대로다."""
    assert kind not in S.FADE_CELL_BOUND
    p = TF._plan()
    S._fade_on(p, SimpleNamespace(kind=kind, start_minute=3, intensity_for=lambda *a: 0.8))
    assert p.fade is None


@pytest.mark.parametrize("kind", sorted(S.FADE_CELL_BOUND))
def test_a_cell_bound_kind_registers_the_pre_break_totals_and_the_open_intensity(kind):
    p = TF._plan()
    before = dict(p.totals)
    sc = SimpleNamespace(kind=kind, start_minute=3, intensity_for=lambda *a: 0.8)
    S._fade_on(p, sc)
    assert p.fade is not None
    got_sc, i0, base = p.fade
    assert got_sc is sc and i0 == 0.8 and base == before


def test_the_first_registered_scenario_wins_and_zero_intensity_registers_nothing():
    """한 세션에는 걷는 장애가 하나다(알려진 한계) — 뒤의 것은 덮지 않는다."""
    p = TF._plan()
    first = SimpleNamespace(kind="transport_loss", start_minute=3, intensity_for=lambda *a: 0.5)
    S._fade_on(p, first)
    S._fade_on(p, SimpleNamespace(kind="ran_congestion", start_minute=3, intensity_for=lambda *a: 0.9))
    assert p.fade[0] is first and p.fade[1] == 0.5
    q = TF._plan()
    S._fade_on(q, SimpleNamespace(kind="transport_loss", start_minute=3, intensity_for=lambda *a: 0.0))
    assert q.fade is None
    S._fade_on(q, None)
    assert q.fade is None


# ── 모의 가동 ───────────────────────────────────────────────────────────

def _run(kind: str, monkeypatch):
    monkeypatch.setattr(P, "load_cells", lambda *a, **k: TD._master())
    monkeypatch.setattr(P, "load_apps", lambda conn: TF.BOOK)
    rec = {"weights": [], "moved": []}                  # (분, 대상 안?, 가중) · (분, 옮긴 뒤 안?, 가중)
    real_emit = SE.SessionEngine._emit
    count = [0]

    def spy(self, ts, slot, s, out, mutate):
        sc = s.fade_sc
        moved = False
        if sc is not None and self.fade_minute is not None and sc.target.cell_keys:
            count[0] += 1
            if count[0] % 2 == 0 and s.cell.cell_key in sc.active_cells_cache and START < self.fade_minute < END:
                outside = dataclasses.replace(s.cell, cell_key="999999-9")   # 대상에 없는 셀(합성 마스터는 대상이 전부일 수 있다)
                s.cell, moved = outside, True              # 이 세션이 대상 밖 셀로 옮겨 갔다고 본다(하니스에는 이동 이용자가 없다)
        n = len(s.fade_seg)
        real_emit(self, ts, slot, s, out, mutate)
        if sc is not None and len(s.fade_seg) > n:
            inside = s.cell.cell_key in sc.active_cells_cache
            (rec["moved"] if moved else rec["weights"]).append((self.fade_minute, inside, s.fade_seg[-1][2]))

    monkeypatch.setattr(SE.SessionEngine, "_emit", spy)
    g = P.PemdbGen(None, TD.ROOT / "data", rates={"lte_call": 1500, "transport": 1500}, seed=11, subscriber_pool=2500)
    g.anchor = TD.T0
    g.gen.app_book = TF.BOOK
    row = {"fault_id": 1, "kind": kind, "target": None,
           "start_ts": TD.T0 + timedelta(minutes=START), "duration_min": DUR, "intensity": 1.0}
    scs, bad = g.set_fault_rows([row])
    assert scs and not bad
    sc = scs[0]
    ix = g.gen.index["transport"]
    seen, dsum = {}, {}
    inversions = ends = bad_ends = 0
    for m in range(MINUTES):
        ts = TD.T0 + timedelta(minutes=m)
        g.records(m, ts)
        for r in g.uplane_records(m, ts).get("transport", []):
            sid, flag = r[ix["SessionID"]], r[ix["OngoingFlag"]]
            tot = int(r[ix["Traffic_Total_Dnlink_RetransPacketCount"]] or 0)
            d_re = int(r[ix["Traffic_Delta_Dnlink_RetransPacketCount"]] or 0)
            prev = seen.get(sid)
            if prev is not None and tot < prev and flag not in ("0", "20"):
                inversions += 1
            seen[sid] = tot
            dsum[sid] = d_re if flag in ("0", "20") else dsum.get(sid, 0) + d_re
            if flag in ("2", "20"):
                ends += 1
                bad_ends += dsum[sid] != tot
    return {"rec": rec, "sc": sc, "inv": inversions, "ends": ends, "bad": bad_ends}


@pytest.fixture(scope="module")
def runs():
    mp = pytest.MonkeyPatch()
    out = {}
    try:
        # 16번 `cdn_degradation` 은 2026-11-23 부터 대상 세션이 모두 연결 요청만 쌓이고 흐른 양이 없어(`_udp_no_answer`) 걷을 것이 없다 — 앱 범위 하니스는 `pay_dns_delay` 로 바꿨다
        for kind in ("compound_causal_failure", "pay_dns_delay"):
            with mp.context() as m:
                out[kind] = _run(kind, m)
    finally:
        mp.undo()
    return out


def test_sessions_that_left_the_target_cells_get_weight_zero(runs):
    got = runs["compound_causal_failure"]["rec"]["moved"]
    assert got, "옮긴 세션이 하나도 없다 — 하니스가 세션을 못 잡았다"
    assert all(not inside and w == 0.0 for _, inside, w in got), got[:5]


def test_sessions_that_stay_inside_keep_a_positive_weight_while_the_fault_runs(runs):
    inside = [w for m, ins, w in runs["compound_causal_failure"]["rec"]["weights"] if ins and START + 1 <= m < END - 3]
    assert inside and all(w > 0 for w in inside)


def test_after_the_fault_ends_every_weight_is_zero(runs):
    """장애가 끝난 분(강도 0)부터 대상 안에 남은 세션도 가중 0 — 평소 양이 흐른다."""
    r = runs["compound_causal_failure"]
    end = r["sc"].start_minute + r["sc"].duration          # 연쇄 복합은 연쇄 단계가 있어 창이 13분이다
    late = [w for m, _, w in r["rec"]["weights"] if m >= end]
    assert late and all(w == 0.0 for w in late)


def test_the_cumulative_total_never_drops_and_deltas_sum_to_the_total(runs):
    r = runs["compound_causal_failure"]
    assert r["inv"] == 0 and r["ends"] > 0 and r["bad"] == 0


def test_a_device_or_app_scope_kind_keeps_its_weight_when_the_cell_changes(runs):
    """앱 범위(결제 앱 지연)는 셀과 무관하다 — 장애 창 안의 가중은 세션이 어느 셀에 있든 양수다. 셀 조건은 목록의 kind 에만 건다.
    (전에는 16번 CDN 으로 세션을 대상 밖 셀로 옮겨 봤다 — 16번은 2026-11-23 부터 흐른 양이 없고 앱 범위 장애는 대상 셀 목록이 없어
    하니스가 세션을 옮길 수 없다. 옮김 자체는 `test_other_kinds_do_not_register_a_fade` 가 「걷힘이 셀에 걸리지 않는다」로 지킨다)"""
    r = runs["pay_dns_delay"]["rec"]
    live = [w for m, _inside, w in r["weights"] if START + 1 <= m < END - 3]
    assert live and all(w > 0 for w in live)
    assert S.FADE_CELL_BOUND.isdisjoint({"pay_dns_delay", "cdn_degradation"})