"""5G 필드를 현장 추출의 모양으로 (2026-11-05 · `5G-CLASSIFICATION.md`) — End 의 `ue_dcnr` · eNB 고정 NR ·
호 길이별 NR · NR 시각 · `ModifyBearer_success_count` · 13/13b/15 번 뮤테이터의 NR 처리.

근거는 현장 추출 516호(96명 · 약 4분 · 전환하는 5G 단말에 치우침)다. **판정의 뜻**: `5G 사용 호 = End ·
ue_dcnr=1 · initial_nr_conn_time>0`, NR 변화 = `ModifyBearer − (PathSwitch − PathSwitchFailure) − 1`.
값(분포)은 모두 **초기값**이라 정확한 숫자가 아니라 **모양과 불변식**을 고정한다. DB 를 쓰지 않는다.
"""
from __future__ import annotations

import collections
import math
import random
import zlib
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from datagen import scenarios as S
from datagen import session as SM
from datagen.cells import load_cells
from datagen.generator import Generator
from datagen.pemdb1 import PemdbGen
from datagen.profile import load as load_profiles

DATA = Path(__file__).resolve().parents[1] / "data"
RATES = {"lte_call": 2000, "transport": 2200, "dns": 900, "http": 1500}
TS = datetime(2026, 10, 14, 19, 0)
START = 1_760_000_000.0


def _gen(end_only: bool) -> Generator:
    cells = load_cells(DATA / "cells.json")
    profiles = load_profiles(DATA / "profile.json")
    keep = {s: profiles[s] for s in ("lte_call", "transport", "dns", "http")}
    return Generator(cells=cells, profiles=keep, scenarios=[], rates=RATES, seed=20260903,
                     diurnal=True, subscriber_pool=3000, success_only=True, end_only=end_only)


@pytest.fixture(scope="module")
def gen() -> Generator:
    return _gen(True)


class _Harness(PemdbGen):
    """`_nr_fields` 만 돌리는 부분 객체 — 필요한 속성만 채운다(메서드 서명이 바뀌면 여기서 걸린다)."""

    def __init__(self, gen, share=0.4, seed=1):
        self.gen = gen
        self._nr_enb_share = share
        self._nr_rng = random.Random(seed + 5050)


@pytest.fixture()
def hz(gen):
    return _Harness(gen)


def _on_cell(hz, gen, want=True):
    for c in gen.cells:
        if hz._nr_on(str(c.enb_id)) == want:
            return c
    pytest.skip("조건에 맞는 셀이 없다")


def _sub(gen, cap):
    return next(s for s in gen.subs if s.dcnr == cap)


def _rec(gen, sub, *, ct="3", dur=60.0, suc=True, ps=0, psf=0):
    from datagen.scenarios import Rec
    rec = Rec(gen.templates["lte_call"].copy(), gen.index["lte_call"], "lte_call")
    rec.set("IMSI", sub.imsi)
    rec.set("ue_dcnr", sub.dcnr)
    rec.set("call_type", ct)
    rec.set("call_start_time", f"{START:.6f}")
    rec.set("call_end_time", f"{START + dur:.6f}")
    rec.set("call_duration_time", int(dur * 1e6))
    rec.set("attempt_flag", "1")
    rec.set("success_flag", "1" if suc else "0")
    rec.set("PathSwitch_count", ps)
    rec.set("PathSwitchFailure_count", psf)
    rec.set("initial_nr_conn_time", "0.0")
    rec.set("ModifyBearer_success_count", 0)
    return rec


def _flags(suc=True):
    return {"att": True, "suc": suc}


def _diff(rec):
    return int(rec.num("ModifyBearer_success_count")) - (
        int(rec.num("PathSwitch_count")) - int(rec.num("PathSwitchFailure_count")))


# --------------------------------------------------------------------------
# 1. End 의 ue_dcnr — -1 이 없다 (end_only) / 호마다 섞인다 (그렇지 않을 때)
# --------------------------------------------------------------------------


def test_end_only_records_never_carry_minus_one_and_equal_the_device_capability(gen):
    slot = TS.timestamp()
    seen = collections.defaultdict(set)
    for _ in range(4000):
        rec, _c = gen.base_lte_call(TS, slot)
        v = rec.get("ue_dcnr")
        assert v in ("0", "1"), v
        seen[rec.get("IMSI")].add(v)
        si = gen.sub_by_imsi[rec.get("IMSI")]
        assert v == gen.subs[si].dcnr
    assert all(len(v) == 1 for v in seen.values()), "같은 IMSI 에 0·1 공존"


def test_tau_records_in_end_only_mode_also_have_no_minus_one(gen):
    slot = TS.timestamp()
    cells = gen.cells
    for i in range(1500):
        sub = gen.subs[i % len(gen.subs)]
        rec, _ = gen.tau_rec(TS, slot, sub, cells[i % len(cells)], cells[(i + 1) % len(cells)])
        assert rec.get("ue_dcnr") == sub.dcnr


def test_without_end_only_minus_one_still_follows_the_profile():
    g = _gen(False)
    dist = g.samplers["lte_call"].p.categorical["ue_dcnr"]
    tot = sum(v for k, v in dist.items() if k != "__other__")
    want = dist.get("-1", 0.0) / tot
    slot = TS.timestamp()
    n = 5000
    absent = sum(g.base_lte_call(TS, slot)[0].get("ue_dcnr") == "-1" for _ in range(n))
    assert abs(absent / n - want) < 0.03, (absent / n, want)
    assert want > 0.5, "프로파일의 -1 은 Start·Interim 몫(약 61%)"


def test_device_capability_share_is_unchanged_by_end_only(gen):
    caps = [s.dcnr for s in gen.subs]
    assert set(caps) <= {"0", "1"}
    assert abs(caps.count("1") / len(caps) - gen.sa_device_share) < 0.03


# --------------------------------------------------------------------------
# 3. eNB 별 NR 가능 여부 — eNB 에 고정
# --------------------------------------------------------------------------


def test_nr_on_is_a_pure_function_of_enb_id_and_share(gen):
    a, b = _Harness(gen, seed=1), _Harness(gen, seed=999)
    ids = [str(c.enb_id) for c in gen.cells][:300]
    assert [a._nr_on(i) for i in ids] == [b._nr_on(i) for i in ids], "난수 시드와 무관(해시)"
    assert [a._nr_on(i) for i in ids] == [a._nr_on(i) for i in ids]


def test_nr_on_share_follows_nr_enb_share(gen):
    ids = [f"{700000 + i}" for i in range(6000)]
    for share in (0.0, 0.2, 0.4, 0.8, 1.0):
        h = _Harness(gen, share=share)
        got = sum(h._nr_on(i) for i in ids) / len(ids)
        assert abs(got - share) < 0.025, (share, got)


def test_nr_on_matches_the_crc_rule_documented_in_the_code(gen):
    h = _Harness(gen, share=0.4)
    for i in ("123456", "987654", "555"):
        assert h._nr_on(i) == (zlib.crc32(f"nr:{i}".encode()) % 1000 < 400)


def test_a_cell_in_an_nr_off_enb_never_gets_an_nr_time(gen, hz):
    cell = _on_cell(hz, gen, want=False)
    cap = _sub(gen, "1")
    for _ in range(300):
        rec = _rec(gen, cap, dur=120.0)
        hz._nr_fields(rec, "3", cell, _flags())
        assert rec.get("initial_nr_conn_time") == "0.0"
        assert _diff(rec) == 1


# --------------------------------------------------------------------------
# 4. NR 조건 — 절차 · 성공 · 단말 · 풀
# --------------------------------------------------------------------------


@pytest.mark.parametrize("ct,allowed", [("3", True), ("4", True), ("7", True), ("1", False), ("5", False),
                                        ("6", False), ("9", False), ("10", False)])
def test_nr_only_on_service_procedures(gen, hz, ct, allowed):
    cell, cap = _on_cell(hz, gen), _sub(gen, "1")
    got = 0
    for _ in range(200):
        rec = _rec(gen, cap, ct=ct, dur=300.0)
        hz._nr_fields(rec, ct, cell, _flags())
        got += rec.num("initial_nr_conn_time") > 0
    assert (got > 0) is allowed, (ct, got)


def test_no_nr_for_failed_non_5g_or_unknown_subscribers(gen, hz):
    cell, cap, lte = _on_cell(hz, gen), _sub(gen, "1"), _sub(gen, "0")
    for _ in range(200):
        r1 = _rec(gen, cap, dur=300.0, suc=False)
        hz._nr_fields(r1, "3", cell, _flags(False))
        r2 = _rec(gen, lte, dur=300.0)
        hz._nr_fields(r2, "3", cell, _flags())
        r3 = _rec(gen, cap, dur=300.0)
        r3.set("IMSI", "없는IMSI")                       # 풀 밖(17번 숨은 단말 등)
        hz._nr_fields(r3, "3", cell, _flags())
        for r in (r1, r2, r3):
            assert r.get("initial_nr_conn_time") == "0.0", r.get("IMSI")


def test_calls_under_five_seconds_never_get_nr(gen, hz):
    cell, cap = _on_cell(hz, gen), _sub(gen, "1")
    for _ in range(400):
        rec = _rec(gen, cap, dur=4.9)
        hz._nr_fields(rec, "3", cell, _flags())
        assert rec.get("initial_nr_conn_time") == "0.0"


def test_nr_probability_rises_with_call_length_and_matches_the_table(gen, hz):
    cell, cap = _on_cell(hz, gen), _sub(gen, "1")
    frac = {}
    for dur in (3.0, 10.0, 40.0, 120.0):
        n = 1500
        k = 0
        for _ in range(n):
            rec = _rec(gen, cap, dur=dur)
            hz._nr_fields(rec, "3", cell, _flags())
            k += rec.num("initial_nr_conn_time") > 0
        frac[dur] = k / n
    assert frac[3.0] == 0.0
    assert frac[3.0] < frac[10.0] < frac[40.0] < frac[120.0]
    for dur, want in ((10.0, 0.45), (40.0, 0.90), (120.0, 1.0)):
        assert abs(frac[dur] - want) < 0.05, (dur, frac[dur], want)
    assert [v for _lim, v in PemdbGen.NR_BY_LENGTH] == [0.0, 0.45, 0.90, 1.0], "초기값 표"


# --------------------------------------------------------------------------
# 5. NR 시각
# --------------------------------------------------------------------------


def test_nr_time_is_after_call_start_and_before_call_end(gen, hz):
    cell, cap = _on_cell(hz, gen), _sub(gen, "1")
    delays = []
    for _ in range(2000):
        dur = random.choice((8.0, 30.0, 90.0, 400.0))
        rec = _rec(gen, cap, dur=dur)
        hz._nr_fields(rec, "3", cell, _flags())
        t = rec.num("initial_nr_conn_time")
        if t > 0:
            assert START < t <= START + 0.9 * dur + 1e-6, (dur, t - START)
            assert t < rec.num("call_end_time")
            delays.append(t - START)
    assert len(delays) > 500
    assert all(d <= PemdbGen.NR_DELAY_MAX_S + 1e-6 for d in delays)


def test_nr_delay_distribution_has_a_half_second_median_and_a_long_tail(gen, hz):
    cell, cap = _on_cell(hz, gen), _sub(gen, "1")
    delays = []
    while len(delays) < 1500:
        rec = _rec(gen, cap, dur=500.0)
        hz._nr_fields(rec, "3", cell, _flags())
        if rec.num("initial_nr_conn_time") > 0:
            delays.append(rec.num("initial_nr_conn_time") - START)
    delays.sort()
    med, p90 = delays[len(delays) // 2], delays[int(len(delays) * 0.9)]
    assert 0.35 < med < 0.75, med
    assert 3.0 < p90 < 12.0, p90
    assert delays[-1] > 20.0, "꼬리가 있다(현장 최대 109초)"
    assert (PemdbGen.NR_DELAY_MEDIAN_S, PemdbGen.NR_DELAY_SIGMA, PemdbGen.NR_DELAY_MAX_S) == (0.5, 1.94, 100.0)


# --------------------------------------------------------------------------
# 6. Modify Bearer
# --------------------------------------------------------------------------


def test_without_nr_modify_bearer_is_exactly_one_plus_path_switch_success(gen, hz):
    cell, lte = _on_cell(hz, gen), _sub(gen, "0")
    for ps, psf in ((0, 0), (3, 0), (5, 2), (1, 1)):
        rec = _rec(gen, lte, ps=ps, psf=psf)
        hz._nr_fields(rec, "3", cell, _flags())
        assert int(rec.num("ModifyBearer_success_count")) == 1 + ps - psf
        assert _diff(rec) == 1


def test_with_nr_modify_bearer_exceeds_the_base_and_diff_is_two_or_more(gen, hz):
    cell, cap = _on_cell(hz, gen), _sub(gen, "1")
    diffs = []
    for _ in range(1500):
        rec = _rec(gen, cap, dur=200.0, ps=random.choice((0, 1, 4)), psf=0)
        hz._nr_fields(rec, "3", cell, _flags())
        if rec.num("initial_nr_conn_time") > 0:
            diffs.append(_diff(rec))
        else:
            assert _diff(rec) == 1
    assert diffs and min(diffs) >= 2 and max(diffs) <= 1 + 4 + 20
    c = collections.Counter(min(d, 5) for d in diffs)
    assert c[2] > 0 and c[3] > 0 and c[5] > 0, c


def test_nr_change_grows_with_call_length(gen, hz):
    cell, cap = _on_cell(hz, gen), _sub(gen, "1")

    def mean_change(dur):
        v = []
        while len(v) < 600:
            rec = _rec(gen, cap, dur=dur)
            hz._nr_fields(rec, "3", cell, _flags())
            if rec.num("initial_nr_conn_time") > 0:
                v.append(_diff(rec) - 1)
        return sum(v) / len(v)

    assert mean_change(15.0) < mean_change(40.0) < mean_change(300.0)


@pytest.mark.parametrize("ct", ["6", "9"])
def test_detach_and_paging_procedures_have_no_modify_bearer(gen, hz, ct):
    cell, cap = _on_cell(hz, gen), _sub(gen, "1")
    rec = _rec(gen, cap, ct=ct, ps=2)
    hz._nr_fields(rec, ct, cell, _flags())
    assert int(rec.num("ModifyBearer_success_count")) == 0


def test_failed_attach_has_neither_nr_nor_modify_bearer(gen, hz):
    cell, cap = _on_cell(hz, gen), _sub(gen, "1")
    rec = _rec(gen, cap, dur=200.0, suc=False)
    hz._nr_fields(rec, "3", cell, _flags(False))
    assert int(rec.num("ModifyBearer_success_count")) == 0 and rec.get("initial_nr_conn_time") == "0.0"


def test_tau_has_a_difference_of_one(gen, hz):
    cell, cap = _on_cell(hz, gen), _sub(gen, "1")
    for _ in range(100):
        rec = _rec(gen, cap, ct="5", dur=200.0, ps=2)
        hz._nr_fields(rec, "5", cell, _flags())
        assert _diff(rec) == 1


def test_nr_fields_do_not_consume_the_main_random_stream(gen, hz):
    cell, cap = _on_cell(hz, gen), _sub(gen, "1")
    before = gen.rng.getstate()
    for _ in range(200):
        hz._nr_fields(_rec(gen, cap, dur=100.0), "3", cell, _flags())
    assert gen.rng.getstate() == before


def test_changing_sa_device_share_leaves_other_nr_draws_alone(gen):
    # 같은 시드의 하네스 둘이 같은 입력에 같은 NR 추첨을 낸다(난수가 격리됐다)
    a, b = _Harness(gen, seed=3), _Harness(gen, seed=3)
    cell, cap = _on_cell(a, gen), _sub(gen, "1")
    out = []
    for h in (a, b):
        row = []
        for _ in range(100):
            rec = _rec(gen, cap, dur=90.0)
            h._nr_fields(rec, "3", cell, _flags())
            row.append((rec.get("initial_nr_conn_time"), rec.get("ModifyBearer_success_count")))
        out.append(row)
    assert out[0] == out[1]


# --------------------------------------------------------------------------
# 7. 변형 뒤 정합 — nr_settle · nr_clear
# --------------------------------------------------------------------------


def _nr_rec(gen, change=3, ps=1, dur=100.0):
    rec = _rec(gen, _sub(gen, "1"), dur=dur, ps=ps)
    rec.set("initial_nr_conn_time", f"{START + 0.5:.6f}")
    S.set_nr_change(rec, change)
    return rec


def test_nr_change_helpers_roundtrip(gen):
    rec = _nr_rec(gen, change=3, ps=2)
    assert S.modify_bearer_base(rec) == 3 and S.nr_change(rec) == 3
    assert int(rec.num("ModifyBearer_success_count")) == 6
    S.set_nr_change(rec, 0)
    assert S.nr_change(rec) == 0 and _diff(rec) == 1


def test_nr_clear_removes_time_and_change_together(gen):
    rec = _nr_rec(gen, change=4, ps=1)
    S.nr_clear(rec)
    assert rec.get("initial_nr_conn_time") == "0.0" and _diff(rec) == 1


def test_nr_clear_keeps_zero_modify_bearer_at_zero(gen):
    rec = _nr_rec(gen, change=0, ps=0)
    rec.set("ModifyBearer_success_count", 0)
    S.nr_clear(rec)
    assert int(rec.num("ModifyBearer_success_count")) == 0


def test_settle_zeroes_both_for_a_call_turned_into_an_access_failure(gen):
    rec = _nr_rec(gen)
    rec.set("success_flag", "0")
    S.nr_settle(rec)
    assert rec.get("initial_nr_conn_time") == "0.0" and int(rec.num("ModifyBearer_success_count")) == 0


def test_settle_clears_nr_that_would_start_after_the_call_ended(gen):
    rec = _nr_rec(gen, dur=100.0)
    rec.set("call_end_time", f"{START + 0.2:.6f}")          # 절단으로 짧아졌다
    S.nr_settle(rec)
    assert rec.get("initial_nr_conn_time") == "0.0" and _diff(rec) == 1


def test_settle_leaves_a_consistent_call_untouched(gen):
    rec = _nr_rec(gen)
    before = (rec.get("initial_nr_conn_time"), rec.get("ModifyBearer_success_count"))
    S.nr_settle(rec)
    assert (rec.get("initial_nr_conn_time"), rec.get("ModifyBearer_success_count")) == before


def test_mobility_failure_keeps_the_nr_share_when_path_switch_changes(gen):
    """9번이 PathSwitch 를 바꿔도 NR 변화 몫은 유지된다(`set_nr_change(rec, nr)`)."""
    from datagen import callflow
    sc = SimpleNamespace(target=SimpleNamespace(cell_pairs=[], cell_keys=set()),
                         active_cells_cache=set())
    kept = 0
    for s in range(60):
        rec = _nr_rec(gen, change=3, ps=0)
        rec.set("success_flag", "1")
        rec.set("UEContextReleaseRequest_Time", f"{START + 50:.6f}")
        rng = random.Random(s)
        if S._handover_failure(rec, sc, rng):
            kept += 1
            assert S.nr_change(rec) == 3, "PathSwitch 가 바뀌었는데 NR 변화 몫이 변했다"
            assert S.modify_bearer_base(rec) == 1 + (int(rec.num("PathSwitch_count"))
                                                    - int(rec.num("PathSwitchFailure_count")))
    assert kept > 0


# --------------------------------------------------------------------------
# 7b. 13 · 13b · 15 번
# --------------------------------------------------------------------------


def _sc(cell):
    return SimpleNamespace(active_cells_cache={cell.cell_key}, target=SimpleNamespace(mme_id="x"))


def test_nsa_nr_leg_clears_nr_and_the_change_in_the_target(gen):
    cell, cap = gen.cells[0], _sub(gen, "1")
    hit = 0
    for s in range(200):
        rec = _nr_rec(gen, change=3, ps=1)
        rec.set("ue_dcnr", "1")
        if S.MUTATORS["nsa_nr_leg"](rec, _sc(cell), 1.0, random.Random(s), cell):
            hit += 1
            assert rec.get("initial_nr_conn_time") == "0.0" and _diff(rec) == 1
    assert 170 <= hit <= 200, "강도 1.0 이면 95%"


def test_nsa_nr_leg_skips_non_5g_and_cells_outside_the_target(gen):
    cell, other = gen.cells[0], gen.cells[1]
    rec = _nr_rec(gen)
    rec.set("ue_dcnr", "0")
    assert not S.MUTATORS["nsa_nr_leg"](rec, _sc(cell), 1.0, random.Random(1), cell)
    rec2 = _nr_rec(gen)
    assert not S.MUTATORS["nsa_nr_leg"](rec2, _sc(cell), 1.0, random.Random(1), other)
    assert rec2.num("initial_nr_conn_time") > 0 and S.nr_change(rec2) == 3


def test_nsa_policy_restrict_clears_nr_and_sets_the_restrict_flag(gen):
    cell = gen.cells[0]
    sc = SimpleNamespace(active_cells_cache=set(), target=SimpleNamespace(mme_id="0007"))
    hit = 0
    for s in range(100):
        r = _nr_rec(gen)
        r.set("MME_ID", "0007")
        if S.MUTATORS["nsa_policy_restrict"](r, sc, 1.0, random.Random(s), cell):
            hit += 1
            assert r.get("mme_restrict_dcnr") == "1" and r.get("initial_nr_conn_time") == "0.0"
            assert _diff(r) == 1
    assert hit > 70
    other = _nr_rec(gen)
    other.set("MME_ID", "0099")
    assert not S.MUTATORS["nsa_policy_restrict"](other, sc, 1.0, random.Random(1), cell)


def test_normal_end_records_keep_mme_restrict_at_zero_for_5g_devices(gen):
    slot = TS.timestamp()
    vals = collections.Counter()
    for _ in range(2000):
        rec, _c = gen.base_lte_call(TS, slot)
        if rec.get("ue_dcnr") == "1":
            vals[rec.get("mme_restrict_dcnr")] += 1
    assert vals and set(vals) <= {"0", "-1"}, vals


@pytest.mark.parametrize("intensity,factor", [(1.0, 3.0), (0.5, 2.0)])
def test_ho_interruption_multiplies_the_nr_change_of_calls_that_keep_nr(gen, intensity, factor):
    cell = gen.cells[0]
    kept = []
    for s in range(600):
        rec = _nr_rec(gen, change=4, ps=0)
        before = S.nr_change(rec)
        S.MUTATORS["ho_interruption"](rec, SimpleNamespace(), intensity, random.Random(s), cell)
        if rec.num("initial_nr_conn_time") > 0:
            kept.append(S.nr_change(rec) / before)
    assert kept and all(abs(k - factor) < 0.3 for k in kept), (factor, set(kept))


def test_ho_interruption_clears_nr_for_about_a_third_at_full_intensity(gen):
    cell = gen.cells[0]
    cleared = 0
    n = 1500
    for s in range(n):
        rec = _nr_rec(gen, change=4, ps=0)
        S.MUTATORS["ho_interruption"](rec, SimpleNamespace(), 1.0, random.Random(s), cell)
        if rec.num("initial_nr_conn_time") == 0:
            cleared += 1
            assert _diff(rec) == 1
    assert abs(cleared / n - 0.35) < 0.05


def test_ho_interruption_leaves_non_5g_and_nr_free_calls_alone(gen):
    cell = gen.cells[0]
    rec = _nr_rec(gen)
    rec.set("ue_dcnr", "0")
    assert not S.MUTATORS["ho_interruption"](rec, SimpleNamespace(), 1.0, random.Random(0), cell)
    free = _rec(gen, _sub(gen, "1"), ps=1)
    S.set_nr_change(free, 0)
    S.MUTATORS["ho_interruption"](free, SimpleNamespace(), 1.0, random.Random(1), cell)
    assert _diff(free) == 1 and free.get("initial_nr_conn_time") == "0.0"


def test_ho_churn_constant_is_a_marked_default():
    assert S.HO_NR_CHURN == 3.0


# --------------------------------------------------------------------------
# 10. net_mode_of — 분류식은 그대로, SA 는 NR 이 있는 호에서만
# --------------------------------------------------------------------------


def test_net_mode_follows_the_nr_time_written_by_nr_fields(gen, hz):
    cell, cap, lte = _on_cell(hz, gen), _sub(gen, "1"), _sub(gen, "0")
    sa = lt = 0
    for _ in range(400):
        rec = _rec(gen, cap, dur=300.0)
        hz._nr_fields(rec, "3", cell, _flags())
        mode = SM.net_mode_of(gen, rec.get("IMSI"), rec.get("initial_nr_conn_time"))
        has = rec.num("initial_nr_conn_time") > 0
        assert mode == (SM.NET_SA if has else SM.NET_LTE)
        sa += has
        lt += not has
        r2 = _rec(gen, lte, dur=300.0)
        hz._nr_fields(r2, "3", cell, _flags())
        assert SM.net_mode_of(gen, r2.get("IMSI"), r2.get("initial_nr_conn_time")) == SM.NET_LTE
    assert sa > 0 and lt >= 0


def test_config_default_for_nr_enb_share_is_documented_as_forty_percent():
    text = (Path(__file__).resolve().parents[1] / "config" / "datagen.yaml").read_text(encoding="utf-8")
    assert "nr_enb_share: 0.4" in text
