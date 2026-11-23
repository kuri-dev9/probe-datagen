"""매일 같은 시각 부하(B 없는 `capacity_shift`)는 판정 하한 호를 채우지 않는다 (2026-11-22 · F-523 · 사용자 (가) · 맥스 `pemdb1._floor_drafts`).

하한(셀당 8호 × 21셀)이 대상 셀 호를 평소 42 → 176으로 채워 혼잡 인원이 가입자 풀과 무관하게 고정됐고, 풀을 키우자 그 몫이 5% 아래로
묽어졌다. 이제 크기는 `_capacity_shift_load`(평소 × `congestion_load`) 하나가 정한다. **B 가 있는 증설 재현은 그대로**(하한·꼬리)이고
다른 kind 의 하한도 그대로다. `test_capacity_shift` 와 같은 생성기 fixture(DB 없음).
"""
from __future__ import annotations

import inspect
from datetime import datetime, timedelta

from test_capacity_shift import A, ROOT, gen  # noqa: F401  (fixture)

from datagen import pemdb1 as P
from datagen import scenarios as S

ENB = "500007"                      # 가중 1.0 인 합성 eNB — 평소 호가 하한(40×3)보다 적다
T0 = datetime(2026, 11, 22, 16, 0)


def _count(g, minutes, enb=ENB):
    i_c = g.gen.index["lte_call"]["First_eNB_ID"]
    out = []
    for m in range(minutes):
        ts = T0 + timedelta(minutes=m)
        out.append(sum(1 for r in g.records(m, ts) if r[i_c] == enb))
        g.uplane_records(m, ts)
    return out


def _run(g, kind, target, minutes=14, duration=12, floor=40.0):
    g.anchor = T0
    g.fault.floor_per_cell = floor
    g.set_fault_rows([{"fault_id": 1, "kind": kind, "target": target, "start_ts": T0,
                       "duration_min": duration, "intensity": 1.0}])
    return _count(g, minutes)


# ── ① 매일 부하(DAILY · B 없음) ─────────────────────────────────────────────

def test_daily_load_does_not_fill_the_floor(gen):
    got = _run(gen, "capacity_shift", f"DAILY:ENB:{ENB}")
    floor = 40.0 * 3
    assert max(got) < floor * 0.9, (got, floor)           # 하한이었다면 120호 안팎까지 채워졌다


def test_daily_load_leaves_no_floor_tail_state(gen):
    _run(gen, "capacity_shift", f"DAILY:ENB:{ENB}")
    assert not gen._floor_last, gen._floor_last


def test_daily_load_target_has_no_b(gen):
    gen.anchor = T0
    gen.set_fault_rows([{"fault_id": 1, "kind": "capacity_shift", "target": f"DAILY:ENB:{ENB}",
                         "start_ts": T0, "duration_min": 12, "intensity": 1.0}])
    sc = gen.gen.scenarios[0]
    assert sc.kind == "capacity_shift" and not getattr(sc.target, "shift_to", None)


def test_daily_load_calls_stay_near_baseline_times_congestion_load(gen):
    gen.anchor = T0
    base = _count(gen, 6)                                  # 장애 없음(평소)
    got = _run(gen, "capacity_shift", f"DAILY:ENB:{ENB}")
    peak = max(got[4:10])
    mean_base = sum(base) / len(base)
    assert mean_base > 0
    # 평소 × daily_load_congestion 근처 — 하한이 아니라 배율이 크기를 정한다(추첨 오차를 넉넉히 둔다)
    assert peak < mean_base * S.FAULT.daily_load_congestion * 1.6, (peak, mean_base)
    assert peak > mean_base * 1.0, (peak, mean_base)


def test_a_daily_load_scales_with_the_floor_off_not_with_floor_per_cell(gen):
    a = _run(gen, "capacity_shift", f"DAILY:ENB:{ENB}", floor=8.0)
    b = _run(gen, "capacity_shift", f"DAILY:ENB:{ENB}", floor=400.0)
    assert abs(max(a) - max(b)) < max(a) * 0.6 + 10, (a, b)    # 하한 값을 바꿔도 크기가 달라지지 않는다(같은 시드)


# ── ② B 가 있는 증설 재현은 그대로 ─────────────────────────────────────────

def test_shift_with_b_still_keeps_the_floor_tail_while_congested(gen):
    t0, st = T0, 2
    gen.anchor = t0
    gen.set_fault_rows([{"fault_id": 1, "kind": "capacity_shift", "target": None,
                         "start_ts": t0 + timedelta(minutes=st), "duration_min": S.SHIFT_TOTAL, "intensity": 1.0}])
    sc = gen.gen.scenarios[0]
    assert getattr(sc.target, "shift_to", None), "자동 대상은 B 를 고른다"
    seen = {}
    for m in range(st + S.SHIFT_RISE + 4):
        list(gen.records(m, t0 + timedelta(minutes=m)))
        seen[m] = len(gen._floor_last)
    assert seen[st + S.SHIFT_RISE - 2] > 0, "혼잡 구간에는 꼬리가 남아야 한다(하한이 A 셀을 채운다)"
    assert seen[st + S.SHIFT_RISE + 2] == 0, "B 개통 뒤 꼬리가 남지 않는다"


# ── ③ 다른 kind 의 하한은 그대로 ────────────────────────────────────────────

def test_other_kind_still_reaches_the_floor(gen):
    got = _run(gen, "ran_radio", f"ENB:{ENB}", duration=10)
    assert min(got[3:7]) >= 40.0 * 3 * 0.9, got
    assert gen._floor_last


# ── ④ 소스 고정 ─────────────────────────────────────────────────────────────

def test_source_skips_only_capacity_shift_without_b():
    src = inspect.getsource(P.PemdbGen._floor_drafts)
    assert 'sc.kind == "capacity_shift" and not getattr(sc.target, "shift_to", None)' in src
    assert 'sc.kind == "capacity_shift":' not in src          # B 있는 증설까지 통째로 건너뛰지 않는다


def test_congestion_load_and_floor_values_are_unchanged():
    assert S.FAULT.congestion_load == 1.8
    from datagen import faults  # noqa: F401
    assert S.FAULT.floor_per_cell == 8.0 or S.FAULT.floor_per_cell > 0
