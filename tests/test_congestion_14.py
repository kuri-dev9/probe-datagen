"""14번 도심 역 저녁 러시(`ran_congestion`) — 기존 가입자의 일부가 나빠진다 (2026-11-05 · 사용자 결정).

처음에는 활성 가입자 1.8배(`congestion_load`)였다. F-129 에서 판정 하한이 덮어 7배가 됐고, 호를 늘리면 판정
표본까지 부풀어 "잡혔다"가 착시였다. 지금은 **호·가입자를 늘리지 않고 원래 있던 사람의 약 60%
(`congestion_user_share`)가 주입 내내 나쁜 상태**다 — IMSI 와 장애 ID 로 가입자를 고정해 뽑는다. 24번 `capacity_shift`
A 와 재해 주변 셀은 `congestion_load`·세션 추첨을 그대로 쓴다.

DB 없이 합성 마스터 + `PemdbGen`(test_disaster 와 같은 하니스).
"""
from __future__ import annotations

import random
from datetime import timedelta
from pathlib import Path

import pytest
import yaml

from datagen import faults
from datagen import scenarios as S
from datagen.pemdb1 import PemdbGen
from test_disaster import ROOT, START, T0, _col, _master, _row, make  # noqa: F401

KIND = "ran_congestion"
DUR = 25
START14 = 20                    # 평소 20분 — 셀당 평소 호가 적어 짧으면 배율이 출렁인다


# ---------------------------------------------------------------- 정답지 · 설정

def test_truth_expects_q11_and_keeps_the_cause_code():
    spec = S.KIND_SPECS[KIND]
    assert spec["expect_kpi"] == "Q-11"
    assert spec["truth_code"] == "RAN_CAPACITY_CONGESTION" and spec["expect_geo"] == "CLUSTER"
    assert spec["expect_rules"] == () and spec["rule_pending"] is True, "2단계 전까지 판단 보류"


def test_defaults_in_code_and_yaml_and_unknown_keys_still_stop():
    assert S.FaultCfg().congestion_load == 1.8 and S.FaultCfg().congestion_user_share == 0.6
    cfg = yaml.safe_load((Path(ROOT) / "config" / "datagen.yaml").read_text(encoding="utf-8"))["pemdb1"]["fault"]
    assert cfg["congestion_load"] == 1.8 and cfg["congestion_user_share"] == 0.6
    assert S.FaultCfg.from_cfg({"congestion_user_share": 0.3}).congestion_user_share == 0.3
    for bad in ({"congestion_loadd": 2.5}, {"congestion_user_shar": 0.3}):
        with pytest.raises(RuntimeError):
            S.FaultCfg.from_cfg(bad)


def test_the_old_module_constant_and_the_14_booster_are_gone():
    assert not hasattr(S, "CONGESTION_LOAD"), "상수는 설정으로 옮겼다 — 두 벌이면 한쪽만 고쳐진다"
    assert not hasattr(S, "_ran_congestion_load")
    assert KIND not in S.BOOSTERS, "14번은 호·가입자를 늘리지 않는다"
    assert KIND in S.PLAN_MUTATORS and S.PLAN_MUTATORS[KIND] is S._ran_congestion_users


@pytest.mark.parametrize("load", [1.8, 3.0])
def test_capacity_shift_a_reads_the_same_setting(monkeypatch, load):
    monkeypatch.setattr(S, "FAULT", S.FaultCfg.from_cfg({"congestion_load": load}))
    monkeypatch.setattr(S, "_shift_elapsed", lambda sc: S.SHIFT_RISE + 5)
    assert S._capacity_shift_load(object(), 1.0) == pytest.approx(load)
    monkeypatch.setattr(S, "_shift_elapsed", lambda sc: 0)
    assert S._capacity_shift_load(object(), 1.0) == pytest.approx(1.0 + (load - 1.0) / S.SHIFT_RISE), "첫 분은 상승 1/RISE"


# ---------------------------------------------------------------- 호 수 배율 · 하한 꼬리

def _target_calls(g, runs, sc, minutes):
    i, c = _col(g, "lte_call", "First_eNB_ID"), _col(g, "lte_call", "First_Cell_ID")
    keys = sc.target.cell_keys
    return {m: sum(1 for r in runs[m] if f"{r[i]}-{r[c]}" in keys) for m in minutes}


def _run(g, load=None, minutes=START14 + DUR + 8, intensity=1.0):
    if load is not None:
        g.fault.congestion_load = load
    row = _row(kind=KIND, start=START14, dur=DUR)
    row["intensity"] = intensity
    scs, bad = g.set_fault_rows([row])
    assert scs and not bad
    runs = [g.records(m, T0 + timedelta(minutes=m)) for m in range(minutes)]
    return runs, scs[0]


@pytest.fixture
def gen(make):                                                  # noqa: F811
    g = make(rate=600)                       # 셀당 평소 약 3호 — 하한(8호)이 걸리면 눈에 띄게 부푼다
    saved = (g.fault.congestion_load, g.fault.congestion_user_share)
    yield g
    g.fault.congestion_load, g.fault.congestion_user_share = saved


def _mean(d, rng):
    xs = [d[m] for m in rng]
    return sum(xs) / len(xs)


def test_call_volume_on_target_cells_is_unchanged(gen):
    """호를 늘리지 않는다 — 대상 셀 호 수가 평소와 같다(하한도 없다)."""
    runs, sc = _run(gen)
    d = _target_calls(gen, runs, sc, range(len(runs)))
    base = _mean(d, range(0, START14))
    peak = _mean(d, range(START14 + 5, START14 + DUR - 5))
    assert base > 5, f"대상 셀 평소 호가 너무 적어 비교가 안 된다: {base}"
    assert 0.8 <= peak / base <= 1.25, f"배율 {peak / base:.2f} (기준 {base:.1f} → 정점 {peak:.1f}) — 1.0 이어야 한다"


def test_no_floor_tail_remains_after_the_window(gen):
    runs, sc = _run(gen)
    d = _target_calls(gen, runs, sc, range(len(runs)))
    base = _mean(d, range(0, START14))
    after = _mean(d, range(START14 + DUR + 3, len(runs)))
    assert 0.75 <= after / base <= 1.3, f"끝난 뒤 호가 평소와 다르다: 평소 {base:.1f} → {after:.1f}"


@pytest.mark.parametrize("load", [1.0, 3.0])
def test_congestion_load_setting_does_not_move_14(gen, load):
    """`congestion_load` 는 24번 A 의 것이다 — 14번은 따르지 않는다."""
    runs, sc = _run(gen, load=load)
    d = _target_calls(gen, runs, sc, range(len(runs)))
    ratio = _mean(d, range(START14 + 5, START14 + DUR - 5)) / _mean(d, range(0, START14))
    assert 0.8 <= ratio <= 1.25, f"load={load} → 배율 {ratio:.2f}"


def test_other_kinds_still_get_the_floor(make):
    """하한을 끈 것은 14·17·27번뿐이다 — 다른 kind(ran_radio · eNB 지정)는 대상 셀이 셀당 8호까지 채워진다."""
    g = make(rate=300)                                          # 셀당 평소 약 1.7호
    scs, bad = g.set_fault_rows([_row(kind="ran_radio", start=START14, dur=DUR, target="ENB:600005")])
    assert scs and not bad
    sc = scs[0]
    runs = [g.records(m, T0 + timedelta(minutes=m)) for m in range(START14 + 12)]
    d = _target_calls(g, runs, sc, range(len(runs)))
    per_cell = _mean(d, range(START14 + 5, START14 + 12)) / len(sc.target.cell_keys)
    assert per_cell >= 6.0, f"하한이 사라졌다: 셀당 {per_cell:.1f}호"


# ---------------------------------------------------------------- 대상 지정 (읽기 전용 확인)

def _build(target):
    row = {"fault_id": 14, "kind": KIND, "target": target, "start_ts": T0 + timedelta(minutes=5),
           "duration_min": DUR, "intensity": 1.0}
    return faults.build(row, _master(), T0, faults.target_rng(7, 14), seed=7)


def test_box_target_takes_every_cell_in_the_box():
    cells = _master()
    lo, hi = 35.60, 35.6035
    want = {c.cell_key for c in cells if lo <= c.latitude <= hi and 139.59 <= c.longitude <= 139.70}
    sc = _build(f"BOX:{lo},139.59,{hi},139.70")
    assert sc is not None and sc.target.cell_keys == want and len(want) > 8, "역 일대 칸의 셀 전부를 지정할 수 있다"
    assert len(f"BOX:{lo},139.59,{hi},139.70") <= 64, "TARGET 컬럼(64자)에 들어간다"


def test_enb_target_takes_that_enb_plus_nearby_busy_cells():
    sc = _build("ENB:600005")
    assert sc is not None and any(k.startswith("600005-") for k in sc.target.cell_keys)


def test_tac_target_and_auto_target_work_too():
    assert _build("TAC:100") is not None
    auto = _build(None)
    assert auto is not None and 1 <= len(auto.target.cell_keys) <= 16


def test_unknown_box_is_cancelled_not_spread_nationwide():
    assert _build("BOX:10,10,11,11") is None


# ---------------------------------------------------------------- 가입자 고정 선택

IMSIS = [f"4400{i:011d}" for i in range(4000)]


@pytest.fixture
def share(monkeypatch):
    def set_(v):
        monkeypatch.setattr(S, "FAULT", S.FaultCfg.from_cfg({"congestion_user_share": v}))
    return set_


def _picked(sid, intensity=1.0):
    return {i for i in IMSIS if S.congested_user(i, sid, intensity)}


def test_same_imsi_and_fault_always_gives_the_same_answer(share):
    share(0.6)
    assert _picked("F-14") == _picked("F-14") == _picked("F-14", 1.0)


def test_different_fault_ids_pick_different_people(share):
    share(0.6)
    a, b = _picked("F-14"), _picked("F-15")
    assert a != b and len(a & b) < len(a), "장애마다 다른 사람이 뽑힌다"
    assert abs(len(a & b) / len(a) - 0.6) < 0.06, "독립이면 겹침은 약 60%"


@pytest.mark.parametrize("v", [0.0, 0.3, 0.6, 1.0])
def test_ratio_follows_the_setting_and_zero_means_nobody(share, v):
    share(v)
    n = len(_picked("F-14"))
    assert (n == 0) if v == 0 else abs(n / len(IMSIS) - v) < 0.04, n / len(IMSIS)


def test_lower_intensity_picks_a_subset_and_it_grows_with_intensity(share):
    share(0.6)
    sets = [_picked("F-14", i) for i in (0.0, 0.25, 0.5, 0.75, 1.0)]
    assert not sets[0], "강도 0 에는 아무도 없다"
    for lo, hi in zip(sets, sets[1:]):
        assert lo <= hi and len(lo) < len(hi), "강도가 오르면 같은 순위의 사람이 더해질 뿐 빠지지 않는다"
    assert abs(len(sets[2]) / len(IMSIS) - 0.3) < 0.04
    assert _picked("F-14", 3.0) == sets[-1], "강도는 1 에서 막힌다"


# ---------------------------------------------------------------- 세션 총량 변형 (plan mutator)

def _plan(imsi, outcome="normal", span=3):
    p = S.Plan({
        "Traffic_Total_Dnlink_PayloadSize": 1_000_000.0, "Traffic_Total_Dnlink_ThroughputTime": 2_000_000.0,
        "Traffic_Total_Dnlink_RetransPacketCount": 50.0, "Traffic_Total_Dnlink_PayloadPacketCount": 10_000.0,
        "Traffic_Total_Uplink_PayloadSize": 250_000.0, "Traffic_Total_Uplink_ThroughputTime": 500_000.0,
        "Traffic_Total_Uplink_RetransPacketCount": 50.0, "Traffic_Total_Uplink_PayloadPacketCount": 2_500.0,
        "DnlinkRTTTime": 30_000.0, "DnlinkRTTCount": 10.0, "UplinkRTTTime": 30_000.0, "UplinkRTTCount": 10.0,
    }, {}, outcome, span)
    p.imsi = imsi
    return p


class _Sc:
    id = "F-14"


class _Cell:
    cell_key = "1-1"


def _snap(p):
    return dict(p.totals), p.stall


def test_chosen_people_get_the_congested_quality_and_others_are_untouched(share):
    share(0.6)
    fn = S.PLAN_MUTATORS[KIND]
    chosen = next(i for i in IMSIS if S.congested_user(i, "F-14", 1.0))
    other = next(i for i in IMSIS if not S.congested_user(i, "F-14", 1.0))
    p0, p1 = _plan(chosen), _plan(other)
    before0, before1 = _snap(p0), _snap(p1)
    assert fn(p0, _Sc(), 1.0, random.Random(1), _Cell()) is True
    assert fn(p1, _Sc(), 1.0, random.Random(1), _Cell()) is False
    assert _snap(p1) == before1, "고르지 않은 사람의 품질은 평시 그대로다"
    t = p0.totals
    assert t["Traffic_Total_Dnlink_PayloadSize"] < before0[0]["Traffic_Total_Dnlink_PayloadSize"] * 0.7, "처리율 하락"
    assert t["DnlinkRTTTime"] / t["DnlinkRTTCount"] > 3.0 * 1000, "RTT 상승(평균 3ms → 수십~수백 ms)"
    assert t["Traffic_Total_Dnlink_ThroughputTime"] == before0[0]["Traffic_Total_Dnlink_ThroughputTime"]
    assert t["Traffic_Total_Uplink_PayloadSize"] == before0[0]["Traffic_Total_Uplink_PayloadSize"], "up 축은 건드리지 않는다"


def test_every_session_of_a_chosen_person_is_changed_not_a_draw(share):
    """세션마다 추첨하지 않는다 — 같은 사람의 세션 100개가 전부 바뀐다(`stall` 결말 제외)."""
    share(0.6)
    fn = S.PLAN_MUTATORS[KIND]
    chosen = next(i for i in IMSIS if S.congested_user(i, "F-14", 1.0))
    hits = [fn(_plan(chosen), _Sc(), 1.0, random.Random(k), _Cell()) for k in range(100)]
    assert all(hits)


def test_stall_outcome_sessions_are_left_alone(share):
    share(1.0)
    p = _plan(IMSIS[0], outcome="stall")
    before = _snap(p)
    assert S.PLAN_MUTATORS[KIND](p, _Sc(), 1.0, random.Random(1), _Cell()) is False and _snap(p) == before


def test_plan_without_imsi_is_not_chosen_by_accident(share):
    share(0.6)
    # imsi 가 빈 세션도 결정적이다 — 빈 값이 항상 뽑히거나 항상 빠질 수는 있으나 예외는 없다
    assert S.PLAN_MUTATORS[KIND](_plan(""), _Sc(), 1.0, random.Random(1), _Cell()) in (True, False)


def test_24_and_disaster_neighbours_still_use_the_session_draw(share):
    """세션 추첨 함수는 그대로다 — 강도 1 이면 약 80% 세션이 걸린다."""
    n = sum(S._ran_congestion(_plan(IMSIS[0]), _Sc(), 1.0, random.Random(k), _Cell()) for k in range(1000))
    assert 740 <= n <= 860, n
    assert S._ran_congestion(_plan(IMSIS[0], outcome="stall"), _Sc(), 1.0, random.Random(1), _Cell()) is False


# ---------------------------------------------------------------- 엔진 연결 (대상 셀 · 주입 전 세션 · Plan.imsi)

def _transport(g, minutes, start, dur):
    """분마다 C-Plane 을 먼저 만들고(U-Plane 은 그 성공 호에서 파생된다) 세션 계열을 모은다."""
    scs, bad = g.set_fault_rows([_row(kind=KIND, start=start, dur=dur)])
    assert scs and not bad
    rows = []
    for m in range(minutes):
        ts = T0 + timedelta(minutes=m)
        g.records(m, ts)
        rows += g.uplane_records(m, ts).get("transport", [])
    return rows, scs[0]


def _spy(monkeypatch, sink):
    orig = S.PLAN_MUTATORS[KIND]

    def spy(plan, sc, intensity, rng, cell):
        sink.append((plan.imsi, sc._minute, intensity, cell.cell_key, cell.cell_key in sc.active_cells_cache))
        return orig(plan, sc, intensity, rng, cell)

    monkeypatch.setitem(S.PLAN_MUTATORS, KIND, spy)


def test_plan_imsi_equals_the_record_imsi(make, monkeypatch):
    """`session._plan` 이 넣는 IMSI 가 레코드의 IMSI 와 같다 — 장애가 "같은 사람"을 고를 수 있다."""
    g = make(rate=600)
    seen = []
    _spy(monkeypatch, seen)
    rows, _sc = _transport(g, START14 + 12, START14, DUR)
    i = g.gen.index["transport"]["IMSI"]
    rec_imsis = {r[i] for r in rows}
    plan_imsis = {x[0] for x in seen}
    assert plan_imsis, "plan mutator 가 한 번도 불리지 않았다"
    assert "" not in plan_imsis, "빈 IMSI 가 들어갔다"
    assert plan_imsis <= rec_imsis, f"레코드에 없는 IMSI {len(plan_imsis - rec_imsis)}개"


def test_mutator_runs_only_inside_the_window_and_only_on_target_cells(make, monkeypatch):
    """주입 전 분에는 불리지 않고(강도 0 구간 포함 시작 분 이후만), 대상 셀 밖 세션에는 자격이 없다."""
    g = make(rate=600)
    seen = []
    _spy(monkeypatch, seen)
    _transport(g, START14 + 6, START14, DUR)
    assert seen
    assert min(x[1] for x in seen) >= START14, "주입 전 분에 불렸다"
    assert all(x[4] for x in seen), "대상 셀 밖 세션에 불렸다"
    assert all(0.0 <= x[2] <= 1.0 for x in seen)


def test_sessions_of_chosen_people_are_degraded_in_the_records(make):
    """끝까지: 고른 가입자의 세션 레코드는 RTT 평균이 높고, 고르지 않은 가입자는 평시와 같다."""
    g = make(rate=900)
    rows, sc = _transport(g, START14 + 14, START14, DUR)
    ix = g.gen.index["transport"]
    t_open = (T0 + timedelta(minutes=START14 + 3)).timestamp()       # 상승 구간(강도 오르는 3분) 뒤에 열린 세션만
    n_chosen = bad_chosen = n_other = bad_other = 0
    for r in rows:
        cnt = float(r[ix["DnlinkRTTCount"]] or 0)
        if cnt <= 0 or f"{r[ix['eNB_ID']]}-{r[ix['Cell_ID']]}" not in sc.target.cell_keys:
            continue
        if float(r[ix["SessionCreatedTime"]] or 0) < t_open:
            continue
        ms = float(r[ix["DnlinkRTTTime"]] or 0) / cnt / 1000.0
        if S.congested_user(r[ix["IMSI"]], sc.id, 1.0):
            n_chosen += 1
            bad_chosen += ms > 150
        else:
            n_other += 1
            bad_other += ms > 150
    assert n_chosen and n_other
    assert bad_other / n_other < 0.05, f"고르지 않은 사람의 초과 비율 {bad_other / n_other:.2%}"
    assert bad_chosen / n_chosen > 0.3, f"고른 사람 {bad_chosen}/{n_chosen} 이 고르지 않은 사람 {bad_other}/{n_other} 보다 나빠야 한다"


# ---------------------------------------------------------------- 정답지 · BOX 대상 (새 방식)

BOX = "BOX:35.6833,139.6875,35.7,139.725"


def test_the_box_string_fits_the_target_column_and_is_parsed():
    assert len(BOX) <= 64
    head, _, arg = BOX.partition(":")
    s_, w_, n_, e_ = (float(v) for v in arg.split(","))
    assert head == "BOX" and (s_ < n_) and (w_ < e_)
    assert S.KIND_SPECS[KIND]["expect_geo"] == "CLUSTER"
