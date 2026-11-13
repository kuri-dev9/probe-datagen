"""27번 Emergency 접속 — 폭주 호 일부에 RRC Establishment Cause 0 (2026-11-05 사용자 결정).

graphio R-46 이 세 신호(호 폭주 · eNB 무호 · **Emergency 접속 증가**)로 "자연재해 발생"을 판정한다. 평시에는
Emergency 가 없다(현장 표본에 0번이 없다). 비율 `pemdb1.fault.disaster_emergency_share`(초기값 0.02)는
근거 없는 초기값이다. 단말이 거는 발신(call_type 3·7)에만 찍는다 — 착신(4·8)의 접속 사유는 MT_ACCESS 다.

`test_disaster.py` 와 같은 하니스(합성 마스터 · DB 없음)를 쓴다.
"""
from __future__ import annotations

import collections
from datetime import timedelta

import pytest

from datagen import pemdb1 as P
from datagen import scenarios as S
from datagen.pemdb1 import PemdbGen
from test_disaster import ROOT, START, T0, _col, _master, _row, make  # noqa: F401  (make 는 픽스처)

EMG = "InitialUEMessage_RRC_Establishment_Cause"
SURGE = range(START, START + 13)            # 폭주 0~12분(장애 시작 기준)
MINUTES = START + 30                        # 정상화(23분) 뒤까지


def _emergency_rows(g, runs, minutes):
    i, ct = _col(g, "lte_call", EMG), _col(g, "lte_call", "call_type")
    return [(m, r[ct], r) for m in minutes for r in runs[m][0] if r[i] == "0"]


def _run(g, share=None, rows=None, minutes=MINUTES):
    if share is not None:
        g.fault.disaster_emergency_share = share
    scs, bad = g.set_fault_rows(rows or [_row(dur=25)])
    assert scs and not bad
    out = []
    for m in range(minutes):
        ts = T0 + timedelta(minutes=m)
        out.append((g.records(m, ts), g.uplane_records(m, ts)))
    return out, scs[0]


@pytest.fixture
def g(make):
    gen = make()
    saved = gen.fault.disaster_emergency_share
    yield gen
    gen.fault.disaster_emergency_share = saved


# ---------------------------------------------------------------- 설정

def test_default_share_is_two_percent_in_code_and_in_yaml():
    from pathlib import Path

    import yaml

    assert S.FaultCfg().disaster_emergency_share == 0.02
    cfg = yaml.safe_load((Path(ROOT) / "config" / "datagen.yaml").read_text(encoding="utf-8"))
    assert cfg["pemdb1"]["fault"]["disaster_emergency_share"] == 0.02


def test_share_is_read_from_the_config_block_and_unknown_keys_still_stop():
    assert S.FaultCfg.from_cfg({"disaster_emergency_share": 0.3}).disaster_emergency_share == 0.3
    with pytest.raises(RuntimeError):
        S.FaultCfg.from_cfg({"disaster_emergency_shar": 0.3})


def test_emergency_call_types_are_originating_only():
    assert S.DISASTER_EMERGENCY_CALL_TYPES == frozenset(("3", "7"))


# ---------------------------------------------------------------- 평시 · 구간 밖에는 0 이 없다

def test_no_emergency_before_during_nowhere_outside_the_surge(g):
    runs, sc = _run(g, share=1.0)
    outside = [m for m in range(MINUTES) if m not in SURGE]
    assert not _emergency_rows(g, runs, outside), "평시·폭주가 끝난 뒤에 Emergency 가 찍혔다"
    assert _emergency_rows(g, runs, SURGE), "폭주 구간에 Emergency 가 하나도 없다(share=1.0)"


def test_zero_share_means_zero_emergency_anywhere(g):
    runs, _ = _run(g, share=0.0)
    assert not _emergency_rows(g, runs, range(MINUTES))


def test_a_run_without_any_fault_has_no_emergency(g):
    g.fault.disaster_emergency_share = 1.0
    runs = [(g.records(m, T0 + timedelta(minutes=m)), None) for m in range(8)]
    assert not _emergency_rows(g, runs, range(8)), "장애가 없으면 비율이 얼마든 0 이 나오지 않는다"


def test_other_kinds_never_get_emergency_even_with_full_share(g):
    runs, _ = _run(g, share=1.0, rows=[_row(kind="enb_s1_outage", dur=10)], minutes=START + 14)
    assert not _emergency_rows(g, runs, range(START + 14))


# ---------------------------------------------------------------- 절차 · 비율

def test_only_originating_call_types_carry_emergency(g):
    runs, _ = _run(g, share=1.0)
    rows = _emergency_rows(g, runs, SURGE)
    assert rows and {ct for _m, ct, _r in rows} <= {"3", "7"}, collections.Counter(ct for _m, ct, _r in rows)


def test_share_scales_the_count_monotonically(g):
    """같은 시드에서 비율만 올리면 건수가 단조 증가한다(추첨 흐름이 비율과 무관하게 같다)."""
    counts = {}
    for share in (0.0, 0.02, 0.5, 1.0):
        gen = g if share == 0.0 else None
        if gen is None:
            # 새 생성기 — 같은 시드 · 같은 마스터
            gen = PemdbGen(None, ROOT / "data", rates={"lte_call": 1500, "transport": 1500}, seed=11,
                           subscriber_pool=2500)
            gen.anchor = T0
        runs, _ = _run(gen, share=share)
        counts[share] = len(_emergency_rows(gen, runs, SURGE))
    assert counts[0.0] == 0 < counts[0.02] < counts[0.5] < counts[1.0], counts
    assert counts[1.0] > 10 * counts[0.02], f"비율 0.02→1.0 은 약 50배여야 한다: {counts}"


# ---------------------------------------------------------------- 정지 eNB 의 폭주 호

def test_marked_surge_calls_survive_relocation_off_the_dead_enbs(g, monkeypatch):
    """정지 eNB 로 가던 폭주 호는 이웃 셀로 옮겨 걸린다 — 표시가 같이 가야 한다.

    정지 국면이 없는 대조군(`disaster_dead_n → 0`)과 다운 국면 분(3~12)의 건수를 견준다. 표시가 옮겨 가며
    사라지면 약 40%(정지 비율)가 빠진다.
    """
    down = range(START + 3, START + 13)
    runs, sc = _run(g, share=1.0)
    dead = S.disaster_dead_enbs(sc, START + 5)
    assert dead, "다운 국면에 정지 eNB 가 있어야 비교가 성립한다"
    marked = _emergency_rows(g, runs, down)
    i = _col(g, "lte_call", "First_eNB_ID")
    assert marked and not [r for _m, _c, r in marked if r[i] in dead], "정지 eNB 에서 걸린 Emergency 호가 있다"

    # 대조군 — 정지 eNB 없음. 2026-11-13 부터 정지 eNB 는 지진(`quake_plan`)이 정한다(옛 40% 규칙은 지난 기록용)
    _plan = S.quake_plan
    monkeypatch.setattr(S, "quake_plan", lambda *a, **k: (lambda ar, d, dc: (ar, [], []))(*_plan(*a, **k)))
    monkeypatch.setattr(S, "disaster_dead_n", lambda n: 0)
    ctl = PemdbGen(None, ROOT / "data", rates={"lte_call": 1500, "transport": 1500}, seed=11, subscriber_pool=2500)
    ctl.anchor = T0
    cruns, csc = _run(ctl, share=1.0)
    assert not S.disaster_dead_enbs(csc, START + 5), "대조군에 정지 eNB 가 남았다"
    base = len(_emergency_rows(ctl, cruns, down))
    assert len(marked) >= 0.8 * base, f"정지 국면 {len(marked)} vs 대조 {base} — 표시가 재배치에서 사라졌다"


# ---------------------------------------------------------------- 정답지

def test_truth_carries_the_disaster_cause_rule_and_category(g):
    scs, bad = g.set_fault_rows([_row(dur=25)])
    assert scs and not bad
    t = scs[0].as_truth()
    assert (t["truth_code"], t["truth_category"]) == ("NATURAL_DISASTER_SUSPECTED", "EXTERNAL")
    assert t["expect_rules"] == ["R-46"] and t["expect_alternatives"] == {"R-36": "RAN_NODE_DOWN"}
    assert t["expect_kpi"] == "C-01"


def test_no_rule_pending_flag_remains_on_27():
    spec = S.KIND_SPECS["natural_disaster"]
    assert not spec.get("rule_pending") and spec["expect_rules"] == ("R-46",)


def test_the_word_earthquake_is_not_in_the_27_truth():
    import json

    spec = S.KIND_SPECS["natural_disaster"]
    text = json.dumps({k: str(v) for k, v in spec.items()}, ensure_ascii=False)
    assert "지진" not in text and "earthquake" not in text.lower()
