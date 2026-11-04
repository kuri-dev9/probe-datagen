"""망 모드(LTE · SA) — 5G 능력은 단말 고정, `-1` 은 호 단위 IE 부재 (2026-11-02).

깨면 같은 IMSI 가 어떤 호에서는 5G 단말, 어떤 호에서는 LTE 단말이 된다.
graphio 가 IMSI 로 CP–UP 를 이어 `NET_MODE` 를 정하므로(`xp_agg`) 그 모순은
SA·LTE 처리율 비교(R-39)를 조용히 오염시킨다.

16번(`anchor_saturation`)의 자동 대상과 "SA 세션만 느리다"도 여기서 본다.

**DB 를 쓰지 않는다** — `test_session_model.py` 와 같은 `data/cells.json`.
"""

from __future__ import annotations

import collections
import random
from datetime import datetime
from pathlib import Path

import pytest

from datagen import faults
from datagen import scenarios as S
from datagen import session as SM
from datagen.cells import load_cells
from datagen.generator import Generator
from datagen.profile import load as load_profiles

DATA = Path(__file__).resolve().parents[1] / "data"
RATES = {"lte_call": 2000, "transport": 2200, "dns": 900, "http": 1500}
TS = datetime(2026, 10, 14, 19, 0)


@pytest.fixture(scope="module")
def gen() -> Generator:
    cells = load_cells(DATA / "cells.json")
    profiles = load_profiles(DATA / "profile.json")
    keep = {s: profiles[s] for s in ("lte_call", "transport", "dns", "http")}
    return Generator(cells=cells, profiles=keep, scenarios=[], rates=RATES,
                     seed=20260903, diurnal=True, subscriber_pool=3000,
                     success_only=True, end_only=True)


def _profile_absent(gen) -> float:
    dist = gen.samplers["lte_call"].p.categorical["ue_dcnr"]
    tot = sum(v for k, v in dist.items() if k != "__other__")
    return dist.get("-1", 0.0) / tot


def test_capability_is_fixed_per_device_and_minus_one_is_per_call(gen):
    """IMSI 안에서 0·1 공존 0건 · SA 단말 비율 ≈ 설정 · `-1` 비율 ≈ 프로파일.

    **TAU 도 본다.** `tau_rec` 이 다른 가입자로 레코드를 만든 뒤 신원만
    바꾸어, 그 가입자의 `ue_dcnr` 가 남던 것이 공존의 원인이었다.
    """
    caps = [s.dcnr for s in gen.subs]
    assert set(caps) <= {"0", "1"}, "단말 능력은 0/1 뿐 — `-1` 은 단말 속성이 아니다"
    share = caps.count("1") / len(caps)
    assert abs(share - gen.sa_device_share) < 0.03, share

    slot = TS.timestamp()
    seen: dict[str, set[str]] = collections.defaultdict(set)
    absent = total = 0
    for _ in range(6000):
        rec, _cell = gen.base_lte_call(TS, slot)
        v = rec.get("ue_dcnr")
        total += 1
        absent += v == "-1"
        seen[rec.get("IMSI")].add(v)
    cells = gen.cells
    for i in range(3000):
        sub = gen.subs[i % len(gen.subs)]
        rec, _ = gen.tau_rec(TS, slot, sub, cells[i % len(cells)],
                             cells[(i + 1) % len(cells)])
        assert rec.get("IMSI") == sub.imsi
        assert rec.get("ue_dcnr") in ("-1", sub.dcnr), (sub.dcnr, rec.get("ue_dcnr"))
        seen[sub.imsi].add(rec.get("ue_dcnr"))
    mixed = [k for k, v in seen.items() if {"0", "1"} <= v]
    assert mixed == [], mixed[:5]
    want = _profile_absent(gen)
    assert abs(absent / total - want) < 0.03, (absent / total, want)


def test_net_mode_reads_the_device_not_the_record(gen):
    """`-1`(IE 부재)인 호도 단말이 5G 이고 NR 연결 시각이 있으면 `SA` 다."""
    cap = next(s for s in gen.subs if s.dcnr == "1")
    lte = next(s for s in gen.subs if s.dcnr == "0")
    assert SM.net_mode_of(gen, cap.imsi, "1760000000.35") == "SA"
    assert SM.net_mode_of(gen, cap.imsi, "0.0") == "LTE"    # NR 레그가 풀렸다
    assert SM.net_mode_of(gen, cap.imsi, "") == "LTE"
    assert SM.net_mode_of(gen, lte.imsi, "1760000000.35") == "LTE"
    assert SM.net_mode_of(gen, "없는IMSI", "1760000000.35") == "LTE"


# --------------------------------------------------------------------------
# 16번 — anchor_saturation
# --------------------------------------------------------------------------


def test_anchor_saturation_auto_target_is_an_enb_cluster(gen):
    """자동 대상이 **인접 eNB 10~15셀 군집**이다 (2026-11-03 · 16번 군집화).

    전에는 시군구 안 50셀을 흩어 뿌려 정답지 `CLUSTER` 와 어긋났다.
    eNB 를 자르지 않는다.
    """
    t = faults._auto_target("anchor_saturation", gen.cells, random.Random(67))
    assert t is not None and t.cell_keys
    by = {c.cell_key: c for c in gen.cells}
    enbs = {by[k].enb_id for k in t.cell_keys}
    assert 10 <= len(t.cell_keys) <= 15, len(t.cell_keys)
    whole = {c.cell_key for c in gen.cells if c.enb_id in enbs and c.geo_status == "MAPPED"}
    assert whole == t.cell_keys, "eNB 를 자르지 않는다"
    assert len(t.sgg_codes) == 1


def _plan(net_mode: str, outcome: str = "normal") -> S.Plan:
    totals = {"Traffic_Total_Dnlink_PayloadSize": 1000.0,
              "Traffic_Total_Uplink_PayloadSize": 500.0,
              "Traffic_Total_Dnlink_ThroughputTime": 9.0e6}
    p = S.Plan(totals, {}, outcome, 3)
    p.net_mode = net_mode
    return p


def test_anchor_saturation_slows_only_sa_sessions(gen):
    """SA 세션만 down payload 가 0.4배 쪽으로 줄고 LTE 세션은 그대로다.

    처리시간·up·RTT·NR 연결은 건드리지 않는다(mutator 머리말).
    """
    cell = gen.cells[0]
    sc = S.Scenario.__new__(S.Scenario)
    sc.active_cells_cache = {cell.cell_key}
    mut = S.PLAN_MUTATORS["anchor_saturation"]
    rng = random.Random(1)
    sa_hit = 0
    for _ in range(200):
        sa, lte = _plan("SA"), _plan("LTE")
        sa_hit += bool(mut(sa, sc, 1.0, rng, cell))
        assert not mut(lte, sc, 1.0, rng, cell)
        assert lte.totals == _plan("LTE").totals
        if sa.totals["Traffic_Total_Dnlink_PayloadSize"] != 1000.0:
            assert sa.totals["Traffic_Total_Dnlink_PayloadSize"] == pytest.approx(400.0)
            assert sa.totals["Traffic_Total_Dnlink_ThroughputTime"] == 9.0e6
            assert sa.totals["Traffic_Total_Uplink_PayloadSize"] == 500.0
    assert sa_hit == 200, sa_hit                         # 2026-11-03 · 전부(1.0)
