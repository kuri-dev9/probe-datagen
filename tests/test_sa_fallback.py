"""SA→LTE 폴백 급증(17번 · `sa_fallback`) — 숨은 SA 상주 단말이 LTE 로 넘어와 처음 나타난다 (2026-11-05).

평소 SA 에 붙어 EPC 레코드가 없던 5G 단말(`PemdbGen.sa_hidden` · 가입자 풀 밖의 별도 집합)이 장애 지역에서
몇 분에 걸쳐 TAU(5)·Attach(1)로 들어오고, 들어온 뒤 평소처럼 호를 내며, 장애가 끝나면 SA 로 돌아가 사라진다.
13번(`nsa_nr_leg`)과 달리 **5G 능력 단말 수와 Attach·TAU 가 늘어난다**.

**DB 를 쓰지 않는다** — `test_disaster.py` 와 같은 하니스(합성 셀 마스터).
수치 허용 범위는 에스터가 정했다(합성 규모 · 단일 시드) — 현장 값이 아니다.
"""

from __future__ import annotations

import collections
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from datagen import control
from datagen import pemdb1 as P
from datagen import scenarios as S
from datagen.cells import Cell

ROOT = Path(__file__).resolve().parents[1]
T0 = datetime(2026, 11, 5, 8, 0)
START = 4                                       # 장애 시작 분
DUR = 8                                         # 장애 길이(분)


def _cell(enb: str, n: int, lat: float, lon: float) -> Cell:
    return Cell(
        cell_key=f"{enb}-{n}", enb_id=enb, cell_id=n, enb_seq=0, c_uid="",
        tac=100, plmn="44010", vlan_id="", latitude=lat, longitude=lon,
        adm_sido_cd="11", adm_sido_nm="", adm_sgg_cd="11200", adm_sgg_nm="",
        adm_emd_cd="11200011", adm_emd_nm="", geo_status="MAPPED",
        rat="EUTRAN", mme_id="0001", sgw_id="0037", pgw_id="0037",
        ims_pgw_id="0037", traffic_weight=1.0, display_name="")


def _master() -> list[Cell]:
    out = []
    for e in range(60):
        lat = 35.60 + (e // 10) * 0.012
        lon = 139.60 + (e % 10) * 0.012
        out += [_cell(f"{600000 + e}", n, lat + n * 0.0015, lon + n * 0.0015) for n in range(3)]
    return out


def _row(kind="sa_fallback", start=START, dur=DUR, target=None, intensity=1.0):
    return {"fault_id": 17, "kind": kind, "target": target, "start_ts": T0 + timedelta(minutes=start),
            "duration_min": dur, "intensity": intensity}


@pytest.fixture
def make(monkeypatch):
    def build(rate: int = 3000, share: float | None = None, pool: int = 2500):
        for f in ("pemdb1/_distributions.json", "profile.json"):
            if not (ROOT / "data" / f).exists():
                pytest.fail(f"data/{f} 가 없다")
        monkeypatch.setattr(P, "load_cells", lambda *a, **k: _master())
        monkeypatch.setattr(P, "load_apps", lambda conn: None)
        kw = {} if share is None else {"net_mode": {"sa_resident_share": share}}
        g = P.PemdbGen(None, ROOT / "data", rates={"lte_call": rate, "transport": rate}, seed=11,
                       subscriber_pool=pool, **kw)
        g.anchor = T0
        return g
    return build


def _run(g, minutes: int, rows: list[dict] | None = None):
    sc = None
    if rows:
        scs, bad = g.set_fault_rows(rows)
        assert scs and not bad, f"장애가 해석되지 않았다: {bad}"
        sc = scs[0]
    out = []
    for m in range(minutes):
        ts = T0 + timedelta(minutes=m)
        out.append((g.records(m, ts), g.uplane_records(m, ts)))
    return out, sc


def _ix(g, name, series="lte_call"):
    return g.gen.index[series][name]


def _area(g, rows, sc):
    i = _ix(g, "First_eNB_ID")
    enbs = {k.rsplit("-", 1)[0] for k in sc.target.cell_keys}
    return [r for r in rows if r[i] in enbs]


def _hidden_imsis(g):
    return {s.imsi for subs in g.sa_hidden.values() for s in subs}


def _hidden_in_area(g, sc):
    return sum(len(g.sa_hidden.get(k, ())) for k in sc.target.cell_keys)


# --------------------------------------------------------------------------
# 1. 평소 불변 — 숨은 집합은 풀 밖이다
# --------------------------------------------------------------------------


def test_normal_records_are_identical_with_the_hidden_population_on_or_off(make):
    """장애가 없으면 `sa_resident_share` 를 켜도 같은 시드의 레코드가 같다(난수 흐름을 흔들지 않는다)."""
    on, _ = _run(make(share=0.15), 4)
    off, _ = _run(make(share=0.0), 4)
    for m in range(4):
        assert on[m][0] == off[m][0], f"{m}분: C-Plane 레코드가 달라졌다"
        assert on[m][1] == off[m][1], f"{m}분: U-Plane 레코드가 달라졌다"


def test_hidden_imsis_are_outside_the_pool_and_all_5g_capable(make):
    g = make(share=0.15)
    hidden = _hidden_imsis(g)
    pool = {s.imsi for s in g.gen.subs}
    assert len(hidden) == sum(len(v) for v in g.sa_hidden.values()), "숨은 단말 IMSI 가 서로 겹친다"
    assert hidden and not (hidden & pool), "숨은 IMSI 가 가입자 풀과 겹친다"
    assert not (hidden & set(g.gen.sub_by_imsi)), "숨은 단말이 sub_by_imsi 에 들어 있다"
    assert all(s.dcnr == "1" for v in g.sa_hidden.values() for s in v)
    assert len(hidden) == int(0.15 * len(g.gen.subs))


def test_zero_share_makes_no_hidden_population_and_out_of_range_is_rejected(make):
    assert make(share=0.0).sa_hidden == {}
    with pytest.raises(RuntimeError):
        make(share=1.5)


def test_hidden_terminals_sit_in_cells_that_exist(make):
    g = make()
    keys = {c.cell_key for c in g.cells}
    assert set(g.sa_hidden) <= keys


def test_hidden_terminals_never_make_records_without_the_fault(make):
    g = make()
    runs, _ = _run(g, 6)
    ix = _ix(g, "IMSI")
    hidden = _hidden_imsis(g)
    assert not {r[ix] for m in range(6) for r in runs[m][0]} & hidden


# --------------------------------------------------------------------------
# 2. 넘어오는 수와 시간
# --------------------------------------------------------------------------


def _arrivals(g, runs, sc):
    """분 → 그 분의 지역 숨은 단말 레코드. 숨은 단말만 추린다."""
    ix = _ix(g, "IMSI")
    hidden = _hidden_imsis(g)
    return {m: [r for r in _area(g, runs[m][0], sc) if r[ix] in hidden] for m in range(len(runs))}


def test_fallback_count_is_hidden_in_area_times_intensity(make):
    g = make()
    runs, sc = _run(g, START + DUR + 5, [_row()])
    n_hidden = _hidden_in_area(g, sc)
    assert n_hidden >= 8, f"지역 숨은 단말이 너무 적어 검증이 안 된다: {n_hidden}"
    assert sc.__dict__.get("_sa_n") == n_hidden, "강도 1.0 이면 지역 숨은 단말 전부가 넘어온다"
    assert len(g._sa_in) + 0 <= n_hidden


@pytest.mark.parametrize("intensity", [0.5, 0.25])
def test_intensity_scales_the_number_of_fallback_terminals(make, intensity):
    g = make()
    _runs, sc = _run(g, START + 2, [_row(intensity=intensity)])
    n = _hidden_in_area(g, sc)
    assert sc._sa_n == int(round(intensity * n))


def test_arrivals_spread_over_zero_to_three_minutes_and_stop_after(make):
    g = make()
    _runs, sc = _run(g, START + DUR - 1, [_row()])
    t0 = (T0 + timedelta(minutes=START)).timestamp()
    ats = [st[3] for st in g._sa_in.values() if st[0] == sc.id]
    assert ats
    assert all(t0 - 1e-6 <= t < t0 + S.SA_FALLBACK_RAMP_MIN * 60 for t in ats), "도착 시각이 [0, 3분) 밖이다"
    assert len({int((t - t0) // 60) for t in ats}) == S.SA_FALLBACK_RAMP_MIN, "도착이 세 분에 고루 퍼지지 않았다"


def test_no_hidden_terminal_appears_before_the_start(make):
    g = make()
    runs, sc = _run(g, START + 3, [_row()])
    arr = _arrivals(g, runs, sc)
    assert all(not arr[m] for m in range(START)), "장애 전에 숨은 단말이 나타났다"
    assert arr[START] or arr[START + 1] or arr[START + 2], "장애 시작 뒤에도 나타나지 않는다"


def test_hidden_terminals_outside_the_area_never_appear(make):
    g = make()
    runs, sc = _run(g, START + DUR + 4, [_row()])
    ix = _ix(g, "IMSI")
    fe = _ix(g, "First_eNB_ID")
    enbs = {k.rsplit("-", 1)[0] for k in sc.target.cell_keys}
    hidden = _hidden_imsis(g)
    out = [r for m in range(len(runs)) for r in runs[m][0] if r[ix] in hidden and r[fe] not in enbs]
    assert not out, f"지역 밖에 숨은 단말 레코드 {len(out)}건"


# --------------------------------------------------------------------------
# 3. 들어옴 기록 — TAU·Attach
# --------------------------------------------------------------------------

_OLD = ("old_call_type", "old_call_end_time", "old_call_Last_eNB_ID", "old_call_Last_Cell_ID",
        "old_call_Last_TAC", "old_call_Last_eNB_PLMN", "old_call_s1ap_release_cause",
        "old_call_error_interface_protocol", "old_call_error_cause")


def _entries(g, runs, sc):
    """숨은 단말마다 **첫 레코드**(들어옴) — IMSI → (분, 레코드)."""
    ix = _ix(g, "IMSI")
    hidden = _hidden_imsis(g)
    first: dict = {}
    for m in range(len(runs)):
        for r in runs[m][0]:
            if r[ix] in hidden and r[ix] not in first:
                first[r[ix]] = (m, r)
    return first


def test_each_terminal_enters_at_most_once_as_tau_or_attach_about_seven_to_three(make):
    g = make(rate=6000, pool=4000)
    # 들어옴 기록은 `_sa_entry_fix` 가 한 번 거친다 — 단말당 몇 번 불리는지 센다. (들어온 뒤의 호는 평소
    # 절차 분포라 5·1 이 섞일 수 있어 call_type 으로 세면 안 된다)
    seen = collections.Counter()
    orig = g._sa_entry_fix
    ii = _ix(g, "IMSI")
    g._sa_entry_fix = lambda rec: (seen.update([rec.values[ii]]), orig(rec))[1]
    runs, sc = _run(g, START + 4, [_row()])
    ix, ct = _ix(g, "IMSI"), _ix(g, "call_type")
    hidden = _hidden_imsis(g)
    first = _entries(g, runs, sc)
    assert len(first) >= 15, f"들어옴 표본이 적다: {len(first)}"
    kinds = collections.Counter(r[ct] for _m, r in first.values())
    assert set(kinds) <= {"5", "1"}, f"들어옴의 call_type 이 5·1 이 아니다: {dict(kinds)}"
    tau = kinds["5"] / sum(kinds.values())
    assert 0.5 <= tau <= 0.9, f"TAU 몫 {tau:.2f} (기대 0.7 근처)"
    assert seen and max(seen.values()) == 1, f"한 단말의 들어옴 기록이 여러 건이다: {seen.most_common(1)}"
    assert set(seen) <= hidden
    assert set(seen) == set(first), "첫 레코드가 들어옴 기록이 아닌 단말이 있다"


def test_entry_records_have_empty_old_call_fields(make):
    g = make(rate=6000, pool=4000)
    runs, sc = _run(g, START + 4, [_row()])
    ix, ct = _ix(g, "IMSI"), _ix(g, "call_type")
    tpl = g.gen.templates["lte_call"]
    entries = [r for _m, r in _entries(g, runs, sc).values() if r[ct] in ("5", "1")]
    assert entries
    for r in entries:
        for name in _OLD:
            j = _ix(g, name)
            assert r[j] == tpl[j], f"{name} 이 비어 있지 않다: {r[j]!r}"


def test_entry_end_time_is_not_before_arrival_and_inside_its_minute(make):
    g = make(rate=6000, pool=4000)
    runs, sc = _run(g, START + 4, [_row()])
    ct, ce = _ix(g, "call_type"), _ix(g, "call_end_time")
    arrive = {imsi: st[3] for imsi, st in g._sa_in.items()}
    n = 0
    for imsi, (m, r) in _entries(g, runs, sc).items():
        if r[ct] not in ("5", "1"):
            continue
        n += 1
        slot = (T0 + timedelta(minutes=m)).timestamp()
        end = float(r[ce])
        assert end >= arrive[imsi] - 1e-3, f"{imsi}: 끝 시각이 도착 시각보다 앞선다 ({end - arrive[imsi]:.2f}초)"
        assert slot - 1e-6 <= end < slot + 60 + 1e-6, f"{imsi}: 끝 시각이 그 분 밖이다"
    assert n >= 15


def test_entry_failure_rate_matches_the_normal_distribution_and_is_not_inflated(make):
    g = make(rate=6000, pool=4000)
    runs, sc = _run(g, START + 4, [_row()])
    ct, sf = _ix(g, "call_type"), _ix(g, "success_flag")
    ok = lambda r: r[sf] in ("1", "True")
    base = [r for m in range(START) for r in runs[m][0] if r[ct] in ("5", "1")]
    ent = [r for _m, r in _entries(g, runs, sc).values() if r[ct] in ("5", "1")]
    assert len(base) >= 100 and len(ent) >= 15
    fb = 1 - sum(map(ok, base)) / len(base)
    fe = 1 - sum(map(ok, ent)) / len(ent)
    assert fe <= fb + 0.2, f"들어옴 실패율 {fe:.2f} 가 평소 {fb:.2f} 보다 부풀었다"


# --------------------------------------------------------------------------
# 4. 들어온 뒤 — N-01 상승 · NR 레그 없음 · LTE 세션
# --------------------------------------------------------------------------


def test_dcnr_share_rises_in_the_area_but_not_outside(make):
    g = make(rate=6000, pool=4000)
    runs, sc = _run(g, START + 6, [_row()])
    dc = _ix(g, "ue_dcnr")
    fe = _ix(g, "First_eNB_ID")
    enbs = {k.rsplit("-", 1)[0] for k in sc.target.cell_keys}

    def share(rows):
        # N-01 은 IE 가 있는 레코드(-1 제외)만 센다
        rows = [r for r in rows if r[dc] in ("0", "1")]
        return sum(1 for r in rows if r[dc] == "1") / max(1, len(rows))

    def area(m):
        return [r for r in runs[m][0] if r[fe] in enbs]

    def out(m):
        return [r for r in runs[m][0] if r[fe] not in enbs]

    base_a = sum(share(area(m)) for m in range(START)) / START
    base_o = sum(share(out(m)) for m in range(START)) / START
    rise = sum(share(area(m)) for m in range(START + 3, START + 6)) / 3 - base_a
    print(f"N-01 area base={base_a:.3f} rise={rise:.3f}")
    assert rise > 0.01, f"지역 N-01 이 오르지 않았다 (평소 {base_a:.3f}, 상승 {rise:.3f})"
    for m in range(START, START + 6):
        assert abs(share(out(m)) - base_o) < 0.05, f"{m}분: 지역 밖 N-01 이 움직였다"


def test_fallback_terminals_have_no_nr_leg_and_are_5g_capable(make):
    g = make(rate=6000, pool=4000)
    runs, sc = _run(g, START + 6, [_row()])
    arr = _arrivals(g, runs, sc)
    nr, dc = _ix(g, "initial_nr_conn_time"), _ix(g, "ue_dcnr")
    tpl = g.gen.templates["lte_call"]
    rows = [r for m in arr for r in arr[m]]
    assert len(rows) >= 15
    for r in rows:
        assert r[nr] == tpl[nr], f"NR 연결 시각이 비어 있지 않다: {r[nr]!r}"
        # -1 은 그 절차에 IE 가 없다는 뜻이다(능력 없음이 아니다 · 현업 답 11-02) — 0 만 아니면 된다
        assert r[dc] in ("1", "-1"), f"넘어온 단말의 ue_dcnr 이 {r[dc]!r} 이다"


def test_net_mode_of_treats_fallback_terminals_as_lte(make):
    """"SA" 의 뜻(`net_mode_of`: NR 연결 = SA)을 바꾸지 않는다 — 넘어온 단말은 NR 레그가 없어 LTE 다."""
    from datagen import session as SE
    g = make(rate=6000, pool=4000)
    runs, sc = _run(g, START + 5, [_row()])
    arr = _arrivals(g, runs, sc)
    rows = [r for m in arr for r in arr[m]]
    assert rows
    ii, nr = _ix(g, "IMSI"), _ix(g, "initial_nr_conn_time")
    for r in rows:
        assert SE.net_mode_of(g.gen, r[ii], r[nr]) == "LTE"


def test_sessions_of_fallback_terminals_are_lte(make):
    g = make(rate=6000, pool=4000)
    runs, sc = _run(g, START + 6, [_row()])
    tix = g.gen.index["transport"]
    hidden = _hidden_imsis(g)
    sess = [r for m in range(len(runs)) for r in runs[m][1].get("transport", []) if r[tix["IMSI"]] in hidden]
    assert sess, "넘어온 단말의 U-Plane 세션이 없다"
    nrc = tix.get("initial_nr_conn_time") if hasattr(tix, "get") else None
    if nrc is not None:
        tpl = g.gen.templates["transport"]
        assert all(r[nrc] == tpl[nrc] for r in sess)


# --------------------------------------------------------------------------
# 5. 상한
# --------------------------------------------------------------------------


def test_added_records_stay_under_half_of_the_normal_calls(make):
    g = make(rate=6000, pool=4000)
    runs, sc = _run(g, START + DUR + 4, [_row()])
    ix = _ix(g, "IMSI")
    hidden = _hidden_imsis(g)
    for m in range(len(runs)):
        added = sum(1 for r in runs[m][0] if r[ix] in hidden)
        normal = len(runs[m][0]) - added
        assert added <= S.SA_FALLBACK_CAP * normal * 1.05 + 1, f"{m}분: 더한 레코드 {added} > 평소 {normal} 의 0.5배"


def test_national_total_stays_close_to_the_normal_level(make):
    g = make(rate=6000, pool=4000)
    runs, _ = _run(g, START + 6, [_row()])
    base = sum(len(runs[m][0]) for m in range(START)) / START
    for m in range(START, START + 6):
        assert len(runs[m][0]) <= 1.6 * base, f"{m}분: 전국 호가 평소의 {len(runs[m][0]) / base:.2f}배"


# --------------------------------------------------------------------------
# 6. 돌아감
# --------------------------------------------------------------------------


def test_after_the_fault_ends_hidden_terminals_stop_within_three_minutes(make):
    g = make(rate=6000, pool=4000)
    end = START + DUR
    runs, sc = _run(g, end + 8, [_row()])
    ix = _ix(g, "IMSI")
    hidden = _hidden_imsis(g)
    last = max((m for m in range(len(runs)) for r in runs[m][0] if r[ix] in hidden), default=-1)
    assert last >= end - 1, "장애 중에 호가 이어지지 않았다"
    assert last <= end + S.SA_FALLBACK_RAMP_MIN, f"장애 끝 {end}분 뒤 3분이 지나도 숨은 단말 호가 남았다 (마지막 {last}분)"
    for m in range(end + S.SA_FALLBACK_RAMP_MIN + 1, len(runs)):
        assert not any(r[ix] in hidden for r in runs[m][0]), f"{m}분: 돌아간 뒤에도 호가 있다"
    assert not g._sa_in, "돌아간 단말이 상태에 남아 있다"


def test_sessions_of_returning_terminals_are_cut_without_fin(make):
    g = make(rate=6000, pool=4000)
    end = START + DUR
    _run(g, end + 6, [_row()])
    hidden = _hidden_imsis(g)
    live = [s for s in g.sessions.active if s.user[0] in hidden and s.cut_t < 0]
    assert not live, f"돌아간 단말의 세션 {len(live)}개가 열려 있다"


def test_returning_makes_no_cplane_records(make):
    """돌아갈 때 C-Plane 레코드가 없다 — 장애 중 마지막 레코드 이후 그 단말의 C-Plane 행은 호(3·4)뿐이고
    해제·Detach 성격의 call_type(2·…)을 새로 내지 않는다."""
    g = make(rate=6000, pool=4000)
    end = START + DUR
    runs, sc = _run(g, end + 6, [_row()])
    ix, ct = _ix(g, "IMSI"), _ix(g, "call_type")
    hidden = _hidden_imsis(g)
    # 돌아가는 구간(끝~끝+3)에 숨은 단말이 낸 레코드의 call_type 은 장애 중에 이미 쓰인 것들뿐이다
    during = {r[ct] for m in range(START, end) for r in runs[m][0] if r[ix] in hidden}
    after = {r[ct] for m in range(end, end + 6) for r in runs[m][0] if r[ix] in hidden}
    assert after <= during | {"3", "4", "7", "8"}, f"돌아가는 구간에 새 절차가 보인다: {after - during}"
    assert not (after & {"2", "6"}), f"Detach·해제 성격의 레코드가 있다: {after}"


# --------------------------------------------------------------------------
# 7. 정답지 · 번호표
# --------------------------------------------------------------------------


def _truth(make, kind="sa_fallback", **kw):
    g = make()
    scs, bad = g.set_fault_rows([_row(kind=kind, **kw)])
    assert scs and not bad
    return g, scs[0]


def test_truth_of_17_carries_context_phases_and_fallback_ue(make):
    g, sc = _truth(make)
    _run(g, START + 2)
    t = sc.as_truth()
    assert t["context"] == [{"code": "SA_FALLBACK", "from_min": 0, "to_min": DUR, "cells": sorted(sc.target.cell_keys)}]
    assert t["phases"] == {"arrive": [0, S.SA_FALLBACK_RAMP_MIN], "back": [DUR, DUR + S.SA_FALLBACK_RAMP_MIN]}
    assert t["fallback_ue"] == sc._sa_n and t["fallback_ue"] > 0
    assert t["expect_kpi"] == "N-01" and t["truth_code"] == "SA_FALLBACK_SUSPECTED"


def test_truth_fallback_ue_is_unknown_before_the_first_minute(make):
    _g, sc = _truth(make)
    assert sc.as_truth()["fallback_ue"] is None, "첫 분 전에는 모르는 값이다(0 이 아니다)"


@pytest.mark.parametrize("kind", ["gw_node_down", "enb_s1_outage", "natural_disaster", "earthquake", "nsa_nr_leg"])
def test_other_kinds_have_no_sa_fallback_keys(make, kind):
    g = make()
    scs, bad = g.set_fault_rows([_row(kind=kind)])
    assert scs and not bad
    t = scs[0].as_truth()
    assert "fallback_ue" not in t
    assert not any(c.get("code") == "SA_FALLBACK" for c in t.get("context", []))
    assert "arrive" not in (t.get("phases") or {})


def test_truth_is_written_by_the_live_truth_path(make, tmp_path):
    import json
    g, sc = _truth(make)
    _run(g, START + 2)
    path = tmp_path / "truth.json"
    P.write_truth(path, T0, 40, [sc])
    sc0 = json.loads(path.read_text())["scenarios"][0]
    assert sc0["fallback_ue"] == sc._sa_n and sc0["phases"]["arrive"] == [0, S.SA_FALLBACK_RAMP_MIN]


def test_number_17_points_to_sa_fallback_and_has_evidence():
    row = next(r for r in control.SCENARIOS if r[0] == 17)
    assert row[2] == "sa_fallback", row
    assert 17 not in control.NO_EVIDENCE
    assert "sa_fallback" in control.KINDS


def test_kind_spec_is_registered_and_target_form_works(make):
    assert "sa_fallback" in S.KIND_SPECS
    g = make()
    cell = g.cells[3]
    scs, bad = g.set_fault_rows([_row(target=f"ENB:{cell.enb_id}")])
    assert scs and not bad
    assert scs[0].target.cell_keys, "ENB 대상이 해석되지 않았다"


# --------------------------------------------------------------------------
# 8. 하한 제외 · 실운영 밀도 (2026-11-05 · F-115)
# --------------------------------------------------------------------------
#
# F-115 에서 하한 채움(셀당 8호)이 17번에도 걸려 대상 25셀이 평소 6.3호 → 200호/분이 됐고, 넘어온 단말 몇 명의 레코드가
# 평소 구성의 호에 묻혀 `dcnr=1` 비중이 지역 밖과 같아졌다(29.9% 대 29.8%). 이제 17번은 하한을 채우지 않는다.
# 합성 하니스는 위(`_master` · 셀 180개)와 달리 **셀 1,800개** — 셀당 평소 호가 1건 미만인 실운영 밀도다.

_SPARSE: dict = {}


def _sparse_master() -> list[Cell]:
    out = []
    for e in range(600):
        lat = 35.20 + (e // 30) * 0.012
        lon = 139.20 + (e % 30) * 0.012
        out += [_cell(f"{600000 + e}", n, lat + n * 0.0015, lon + n * 0.0015) for n in range(3)]
    return out


def _sparse(monkeypatch, kind="sa_fallback", target=None, share=None, minutes=START + DUR + 6, intensity=1.0):
    key = (kind, target, share, minutes, intensity)
    if key not in _SPARSE:
        for f in ("pemdb1/_distributions.json", "profile.json"):
            if not (ROOT / "data" / f).exists():
                pytest.fail(f"data/{f} 가 없다")
        monkeypatch.setattr(P, "load_cells", lambda *a, **k: _sparse_master())
        monkeypatch.setattr(P, "load_apps", lambda conn: None)
        kw = {} if share is None else {"net_mode": {"sa_resident_share": share}}
        g = P.PemdbGen(None, ROOT / "data", rates={"lte_call": 1500, "transport": 1500}, seed=11,
                       subscriber_pool=2500, **kw)
        g.anchor = T0
        runs, sc = _run(g, minutes, [_row(kind=kind, target=target, intensity=intensity)])
        _SPARSE[key] = (g, runs, sc)
    return _SPARSE[key]


def _series(g, runs, sc):
    return [len(_area(g, runs[m][0], sc)) for m in range(len(runs))]


def test_sparse_harness_really_is_below_the_floor(monkeypatch):
    g, runs, sc = _sparse(monkeypatch)
    per_cell = sum(_series(g, runs, sc)[:START]) / START / len(sc.target.cell_keys)
    assert per_cell < 1.5, f"셀당 평소 호 {per_cell:.2f} — 실운영 밀도가 아니다"


def test_sa_fallback_leaves_no_floor_tail(monkeypatch):
    g, _runs, _sc = _sparse(monkeypatch)
    assert not g._floor_last, f"17번이 하한 꼬리를 남겼다: {list(g._floor_last)[:3]}"


def test_area_calls_are_normal_plus_the_fallback_share_not_the_floor(monkeypatch):
    """대상 지역 호가 하한(셀당 8 × 25셀 ≈ 200)으로 오르지 않는다 — 평소에 넘어온 단말 몫만 얹힌다."""
    g, runs, sc = _sparse(monkeypatch)
    s = _series(g, runs, sc)
    base = sum(s[:START]) / START
    floor = 8 * len(sc.target.cell_keys)
    assert max(s) < floor / 2, f"하한(≈{floor}) 수준의 분이 있다: 최대 {max(s)}"
    during = sum(s[START:START + DUR]) / DUR
    assert during <= base * 2.5 + 5, f"장애 중 지역 호 {during:.0f} (평소 {base:.0f})"
    hid = _hidden_imsis(g)
    ii = _ix(g, "IMSI")
    added = sum(1 for m in range(START, START + DUR) for r in _area(g, runs[m][0], sc) if r[ii] in hid)
    assert during - base <= added / DUR + 12, "넘어온 단말 몫 말고 늘어난 호가 있다"


def test_dcnr_share_rises_in_the_area_at_realistic_density(monkeypatch):
    """하한이 빠졌으니 넘어온 단말이 지역 구성에서 보인다 — 숨은 단말 몫을 크게 한 하니스(0.9)에서."""
    g, runs, sc = _sparse(monkeypatch, share=0.9)
    dc = _ix(g, "ue_dcnr")

    def share(m):
        rows = [r for r in _area(g, runs[m][0], sc) if r[dc] in ("0", "1")]
        return sum(1 for r in rows if r[dc] == "1") / max(1, len(rows))

    base = sum(share(m) for m in range(START)) / START
    during = sum(share(m) for m in range(START + 3, START + DUR)) / (DUR - 3)
    assert during - base > 0.08, f"지역 N-01 이 오르지 않았다 (평소 {base:.3f} → {during:.3f})"
    fe = _ix(g, "First_eNB_ID")
    enbs = {k.rsplit("-", 1)[0] for k in sc.target.cell_keys}
    outside = [r for m in range(START + 3, START + DUR) for r in runs[m][0] if r[fe] not in enbs and r[dc] in ("0", "1")]
    assert abs(sum(1 for r in outside if r[dc] == "1") / len(outside) - base) < 0.15, "지역 밖 구성도 움직였다"


# 14번(ran_congestion)은 11-05 F-129 에서 하한을 끄도록 바뀌었다 — 아래 별도 테스트가 그 사실을 고정한다
@pytest.mark.parametrize("kind", ["earthquake"])
def test_other_kinds_keep_the_floor_on_the_same_harness(monkeypatch, kind):
    g, runs, sc = _sparse(monkeypatch, kind=kind, minutes=START + 8)
    assert g._floor_last, f"{kind}: 하한이 걸리지 않았다"
    s = _series(g, runs, sc)
    assert sum(s[START + 1:START + 6]) / 5 >= 3 * max(sum(s[:START]) / START, 1), f"{kind}: 하한 채움이 보이지 않는다"

def test_congestion_14_no_longer_gets_the_floor(monkeypatch):
    """14번은 하한을 채우지 않는다(11-05 F-129 · 맥스) — 부하는 `congestion_load` 배율이 곧 주입이다.
    같은 희소 하니스에서 하한이 대상 셀을 평소 3배 이상으로 채우면 안 된다."""
    g, runs, sc = _sparse(monkeypatch, kind="ran_congestion", minutes=START + 8)
    assert not g._floor_last, "14번에 하한 꼬리가 생겼다"
    s = _series(g, runs, sc)
    base = sum(s[:START]) / START
    peak = sum(s[START + 1:START + 6]) / 5
    assert peak < 3 * max(base, 1), f"하한 채움이 보인다 (평소 {base:.0f} → {peak:.0f})"


def test_natural_disaster_still_has_no_floor(monkeypatch):
    g, _runs, _sc = _sparse(monkeypatch, kind="natural_disaster", target="LOCAL", minutes=START + 8)
    assert not g._floor_last


# --------------------------------------------------------------------------
# 9. 정답지 fallback_imsis
# --------------------------------------------------------------------------


def _truth_run(make, minutes=START + 2, **kw):
    g, sc = _truth(make, **kw)
    _run(g, minutes)
    return g, sc


def test_fallback_imsis_has_the_same_length_as_fallback_ue_and_is_sorted(make):
    _g, sc = _truth_run(make)
    t = sc.as_truth()
    assert t["fallback_ue"] > 0 and len(t["fallback_imsis"]) == t["fallback_ue"]
    assert t["fallback_imsis"] == sorted(t["fallback_imsis"]) and len(set(t["fallback_imsis"])) == len(t["fallback_imsis"])


def test_fallback_imsis_are_hidden_terminals_of_the_area_and_not_in_the_pool(make):
    g, sc = _truth_run(make)
    ims = set(sc.as_truth()["fallback_imsis"])
    assert ims <= _hidden_imsis(g), "숨은 단말 집합 밖의 IMSI 가 있다"
    assert not (ims & {s.imsi for s in g.gen.subs}) and not (ims & set(g.gen.sub_by_imsi)), "가입자 풀과 겹친다"
    in_area = {s.imsi for k in sc.target.cell_keys for s in g.sa_hidden.get(k, ())}
    assert ims <= in_area, "대상 지역 밖 단말이 목록에 있다"


def test_fallback_imsis_equal_the_terminals_the_generator_actually_seated(make):
    g, sc = _truth_run(make)
    assert set(sc.as_truth()["fallback_imsis"]) == {i for i, st in g._sa_in.items() if st[0] == sc.id}


def test_fallback_imsis_scale_with_intensity(make):
    g = make()
    scs, _bad = g.set_fault_rows([_row(intensity=0.5)])
    _run(g, START + 2)
    full_g, full_sc = _truth_run(make)
    half, full = scs[0].as_truth()["fallback_imsis"], full_sc.as_truth()["fallback_imsis"]
    assert len(half) == round(0.5 * len(full)) or abs(len(half) - 0.5 * len(full)) <= 1


def test_fallback_imsis_is_unknown_before_the_first_minute(make):
    _g, sc = _truth(make)
    t = sc.as_truth()
    assert t["fallback_imsis"] is None and t["fallback_ue"] is None, "첫 분 전에는 모른다(빈 목록이 아니다)"


@pytest.mark.parametrize("kind", ["gw_node_down", "enb_s1_outage", "natural_disaster", "earthquake", "nsa_nr_leg"])
def test_other_kinds_have_no_fallback_imsis_key(make, kind):
    g = make()
    scs, bad = g.set_fault_rows([_row(kind=kind)])
    assert scs and not bad
    assert "fallback_imsis" not in scs[0].as_truth()


def test_every_hidden_terminal_record_belongs_to_the_listed_imsis(make):
    """장애 중 레코드에 나오는 숨은 단말은 모두 목록 안이다 — 목록 밖의 숨은 단말은 0건."""
    g, sc = _truth(make)
    runs, _ = _run(g, START + DUR + 5)
    ims = set(sc.as_truth()["fallback_imsis"])
    ii = _ix(g, "IMSI")
    seen = {r[ii] for m in range(len(runs)) for r in runs[m][0] if r[ii] in _hidden_imsis(g)}
    assert seen, "숨은 단말이 레코드를 하나도 내지 않았다"
    assert seen <= ims, f"목록 밖 숨은 단말 레코드 {len(seen - ims)}명"


def test_most_listed_imsis_show_up_in_records_over_the_fault(make):
    """목록의 모든 단말이 레코드를 내지는 않는다(들어옴 기록은 생산량 비율 · 호는 평소 빈도) — 그래도 **일부는 보인다**."""
    g, sc = _truth(make)
    runs, _ = _run(g, START + DUR)
    ims = set(sc.as_truth()["fallback_imsis"])
    ii = _ix(g, "IMSI")
    seen = {r[ii] for m in range(START, START + DUR) for r in runs[m][0] if r[ii] in ims}
    assert 0 < len(seen) <= len(ims)


def test_fallback_imsis_are_written_by_the_live_truth_path(make, tmp_path):
    import json
    g, sc = _truth_run(make)
    path = tmp_path / "truth.json"
    P.write_truth(path, T0, 40, [sc])
    sc0 = json.loads(path.read_text())["scenarios"][0]
    assert sc0["fallback_imsis"] == sc.as_truth()["fallback_imsis"] and sc0["fallback_ue"] == sc._sa_n
