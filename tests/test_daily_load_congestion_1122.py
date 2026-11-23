"""매일 같은 시각 부하의 혼잡 배율 `daily_load_congestion` (2026-11-22 · 사용자 (가) · F-532 몫 1.9% · 맥스).

하한 호를 끈 뒤 평소의 1.8배만으로는 호가 1.6배 · Q-11 불량 몫 1.9% 라 장애가 서지 않았다. 그래서 **B 없는 매일 부하**에만 3배를 건다.
B 가 있는 증설 재현은 `congestion_load`(1.8) 그대로다. 값이 없으면(yaml 에 키 없음) 둘 다 1.8 — 옛 동작.
"""
from __future__ import annotations

import inspect
from datetime import datetime, timedelta
from pathlib import Path

import yaml
from test_capacity_shift import gen  # noqa: F401  (fixture)

from datagen import scenarios as S

ROOT = Path(__file__).resolve().parents[1]
T0 = datetime(2026, 11, 22, 16, 0)
ENB = "500007"


def _yaml_fault():
    return yaml.safe_load((ROOT / "config" / "datagen.yaml").read_text(encoding="utf-8"))["pemdb1"]["fault"]


# ── ① 값과 읽기 ────────────────────────────────────────────────────────────

def test_yaml_sets_daily_3_and_keeps_shift_1_8():
    f = _yaml_fault()
    assert f["daily_load_congestion"] == 3.0 and f["congestion_load"] == 1.8


def test_dataclass_defaults_are_the_old_behaviour_when_yaml_has_no_key():
    c = S.FaultCfg.from_cfg({})
    assert c.congestion_load == 1.8 and c.daily_load_congestion == 1.8


def test_from_cfg_reads_the_new_key_as_float_and_leaves_the_other_alone():
    c = S.FaultCfg.from_cfg({"daily_load_congestion": 3})
    assert c.daily_load_congestion == 3.0 and isinstance(c.daily_load_congestion, float)
    assert c.congestion_load == 1.8


def test_real_yaml_loads_through_set_fault_cfg_and_is_restored(monkeypatch):
    monkeypatch.setattr(S, "FAULT", S.FAULT)
    c = S.FaultCfg.from_cfg(_yaml_fault())
    assert c.daily_load_congestion == 3.0 and c.congestion_load == 1.8


# ── ② 어느 장애에 어느 배율이 걸리나 ───────────────────────────────────────

def _load_at_peak(g, monkeypatch, target, daily=3.0, shift=1.8, start=0, minute=8, duration=60):
    monkeypatch.setattr(S, "FAULT", S.FaultCfg.from_cfg({"daily_load_congestion": daily, "congestion_load": shift}))
    g.anchor = T0
    g.set_fault_rows([{"fault_id": 1, "kind": "capacity_shift", "target": target,
                       "start_ts": T0 + timedelta(minutes=start), "duration_min": duration, "intensity": 1.0}])
    sc = g.gen.scenarios[0]
    list(g.records(minute, T0 + timedelta(minutes=minute)))
    return sc, S._capacity_shift_load(sc, 1.0)


def test_daily_load_without_b_uses_daily_load_congestion(gen, monkeypatch):
    sc, v = _load_at_peak(gen, monkeypatch, f"DAILY:ENB:{ENB}")
    assert not getattr(sc.target, "shift_to", None)
    assert abs(v - 3.0) < 1e-9, v


def test_shift_with_b_uses_congestion_load(gen, monkeypatch):
    sc, v = _load_at_peak(gen, monkeypatch, None, start=2, minute=8, duration=S.SHIFT_TOTAL)
    assert getattr(sc.target, "shift_to", None)
    assert abs(v - 1.8) < 1e-9, v


def test_changing_the_daily_value_does_not_move_the_shift_value(gen, monkeypatch):
    _, v = _load_at_peak(gen, monkeypatch, None, daily=5.0, start=2, minute=8, duration=S.SHIFT_TOTAL)
    assert abs(v - 1.8) < 1e-9


def test_changing_the_shift_value_does_not_move_the_daily_value(gen, monkeypatch):
    _, v = _load_at_peak(gen, monkeypatch, f"DAILY:ENB:{ENB}", shift=1.2)
    assert abs(v - 3.0) < 1e-9


def test_without_the_key_both_use_1_8(gen, monkeypatch):
    c = {"congestion_load": 1.8}                      # yaml 에 daily 키가 없는 경우
    monkeypatch.setattr(S, "FAULT", S.FaultCfg.from_cfg(c))
    gen.anchor = T0
    gen.set_fault_rows([{"fault_id": 1, "kind": "capacity_shift", "target": f"DAILY:ENB:{ENB}",
                         "start_ts": T0, "duration_min": 60, "intensity": 1.0}])
    sc = gen.gen.scenarios[0]
    list(gen.records(8, T0 + timedelta(minutes=8)))
    assert abs(S._capacity_shift_load(sc, 1.0) - 1.8) < 1e-9


def test_daily_load_ramps_from_one_toward_the_daily_value(gen, monkeypatch):
    monkeypatch.setattr(S, "FAULT", S.FaultCfg.from_cfg({"daily_load_congestion": 3.0}))
    gen.anchor = T0
    gen.set_fault_rows([{"fault_id": 1, "kind": "capacity_shift", "target": f"DAILY:ENB:{ENB}",
                         "start_ts": T0, "duration_min": 60, "intensity": 1.0}])
    sc = gen.gen.scenarios[0]
    vals = []
    for m in range(10):
        list(gen.records(m, T0 + timedelta(minutes=m)))
        vals.append(S._capacity_shift_load(sc, 1.0))
    assert vals[0] < vals[-1] and 1.0 <= vals[0] < 3.0 and abs(vals[-1] - 3.0) < 1e-9, vals


# ── ③ 호가 실제로 더 오른다 · 하한은 여전히 안 찬다 ────────────────────────

def _peak_calls(g, monkeypatch, daily):
    monkeypatch.setattr(S, "FAULT", S.FaultCfg.from_cfg({"daily_load_congestion": daily, "floor_per_cell": 40.0}))
    g.anchor = T0
    g.fault.floor_per_cell = 40.0
    g.set_fault_rows([{"fault_id": 1, "kind": "capacity_shift", "target": f"DAILY:ENB:{ENB}",
                       "start_ts": T0, "duration_min": 20, "intensity": 1.0}])
    i_c = g.gen.index["lte_call"]["First_eNB_ID"]
    out = []
    for m in range(14):
        ts = T0 + timedelta(minutes=m)
        out.append(sum(1 for r in g.records(m, ts) if r[i_c] == ENB))
        g.uplane_records(m, ts)
    return out


def test_higher_daily_value_makes_more_calls_and_the_floor_still_does_not_fill(gen, monkeypatch):
    lo = _peak_calls(gen, monkeypatch, 1.8)
    hi = _peak_calls(gen, monkeypatch, 3.0)
    floor = 40.0 * 3
    assert max(hi[5:12]) > max(lo[5:12]), (lo, hi)
    assert max(hi) < floor * 0.9 * 1.5, (hi, floor)            # 하한이었다면 120호에서 멈췄을 값이 아니라 배율 몫이다


# ── ④ 소스 고정 ────────────────────────────────────────────────────────────

def test_source_picks_the_value_by_shift_to():
    src = inspect.getsource(S._capacity_shift_load)
    assert "daily_load_congestion" in src and "congestion_load" in src and "shift_to" in src


def test_congestion_load_is_read_nowhere_else_in_scenarios():
    src = (ROOT / "src" / "datagen" / "scenarios.py").read_text(encoding="utf-8")
    body = src.split("def _capacity_shift_load", 1)[1].split("@plan_mutator", 1)[0]
    others = src.replace(body, "")
    # 1번 퇴근길 역 앞 속도 저하의 부스터(`_ran_congestion_load`)도 읽는다(2026-11-23 결정 3 · 사람을 congestion_load 배로)
    booster = src.split("def _ran_congestion_load", 1)[1].split("\n\n\n", 1)[0]
    others = others.replace(booster, "")
    assert "FAULT.congestion_load" not in others, "읽는 곳은 매일 부하(`_capacity_shift_load`)와 1번 부스터 둘뿐이다"
