"""자연재해 자동 대상 — 정지 eNB 호율이 가장 높은 지역 (2026-11-05 · F-111 후속).

15% 생산량에서는 eNB 대부분이 분당 1호 미만이라 graphio D4 가 20분 정지를 확정하지 못했다(λ 0.76~0.88 →
N* 24~27분). 자동 대상이 **정지할 eNB 의 평소 호율이 가장 높은 지역**을 고르게 했다(임계·생산량은 그대로).
정답지에는 정지 eNB 별 호율 `down_enbs_rate` 가 실린다.

**2026-11-14 — 자동 대상이 "호율이 높은 지역"을 고르던 `faults._busy_disaster_area` 는 걷혔다**(지역은 진원역 지진의 진도 3 이상
셀이 정한다 · `test_quake_1113.py`). 그 함수를 시험하던 9개(지역 선택 · 결정성 · 이동 · 정답지 호율 키)는 이 파일에서 뺐고, 호율을
읽는 공급자·정답지 `down_enbs_rate` 의 남은 시험만 둔다.

DB 를 쓰지 않는다 — 합성 셀 마스터 + 가짜 커서. 호율 공급자는 장애 시작 시각을 받아 `{eNB: λ}` 를 준다.
"""

from __future__ import annotations

import math
import random
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from datagen import faults
from datagen import pemdb1 as P
from datagen import scenarios as S
from datagen.cells import Cell

ROOT = Path(__file__).resolve().parents[1]
T0 = datetime(2026, 11, 5, 9, 0)
START = 5


def _cell(enb: str, n: int, lat: float, lon: float) -> Cell:
    return Cell(
        cell_key=f"{enb}-{n}", enb_id=enb, cell_id=n, enb_seq=0, c_uid="",
        tac=100, plmn="44010", vlan_id="", latitude=lat, longitude=lon,
        adm_sido_cd="11", adm_sido_nm="", adm_sgg_cd="11200", adm_sgg_nm="",
        adm_emd_cd="11200011", adm_emd_nm="", geo_status="MAPPED",
        rat="EUTRAN", mme_id="0001", sgw_id="0037", pgw_id="0037",
        ims_pgw_id="0037", traffic_weight=1.0, display_name="")


def _master() -> list[Cell]:
    """eNB 60개 × 3셀, 6행 × 10열 격자 — 아래 두 행(eNB 50~59)이 붐비는 지역이다."""
    out = []
    for e in range(60):
        lat = 35.60 + (e // 10) * 0.012
        lon = 139.60 + (e % 10) * 0.012
        out += [_cell(f"{600000 + e}", n, lat + n * 0.0015, lon + n * 0.0015) for n in range(3)]
    return out


def _enb(e: int) -> str:
    return str(600000 + e)


def _rates(busy=range(50, 60), hi=3.0, lo=0.4) -> dict[str, float]:
    """붐비는 무리는 λ 가 높되 eNB 마다 조금씩 다르다(최솟값이 갈리도록) · 나머지는 한산하다."""
    return {_enb(e): (hi + (e % 7) * 0.2 if e in busy else lo) for e in range(60)}


def _fault(kind="natural_disaster", target=None, fid=27):
    return {"fault_id": fid, "kind": kind, "target": target, "start_ts": T0 + timedelta(minutes=START),
            "duration_min": 25, "intensity": 1.0}


def _build(fault, rate=None, seed=7, anchor=T0):
    calls = []

    def provider(ts):
        calls.append(ts)
        if isinstance(rate, Exception):
            raise rate
        return rate

    sc = faults.build(fault, _master(), anchor, faults.target_rng(seed, fault["fault_id"]), seed=seed,
                      enb_rate=provider)
    return sc, calls


def _build_plain(fault, seed=7):
    """공급자 없이(`enb_rate=None`) — 예전 무작위 지역."""
    return faults.build(fault, _master(), T0, faults.target_rng(seed, fault["fault_id"]), seed=seed)


def _enbs(keys):
    return {k.rsplit("-", 1)[0] for k in keys}


# --------------------------------------------------------------------------
# 1. enb_call_rate — 가짜 커서
# --------------------------------------------------------------------------


class _Cur:
    def __init__(self, rows, log):
        self.rows, self.log = rows, log

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=()):
        self.log.append((sql, params))

    def fetchall(self):
        return self.rows


class _Conn:
    def __init__(self, rows):
        self.rows, self.log = rows, []

    def cursor(self):
        return _Cur(self.rows, self.log)


def test_rate_is_the_lower_of_mean_and_minus_ln_of_idle_fraction():
    # (eNB, 호가 있던 분, 시도 합) — 60분 창
    rows = [("A", 30, 300), ("B", 59, 59), ("C", 60, 120)]
    out = P.enb_call_rate(_Conn(rows), T0)
    assert out["A"] == pytest.approx(-math.log(30 / 60)), "평균 5 보다 무호 분 비율 값(0.693)이 낮다"
    assert out["B"] == pytest.approx(59 / 60), "평균 0.983 이 −ln(1/60)=4.09 보다 낮다"


def test_rate_without_idle_minutes_is_the_mean():
    out = P.enb_call_rate(_Conn([("C", 60, 120)]), T0)
    assert out["C"] == pytest.approx(2.0)


def test_rate_of_enbs_with_no_rows_is_absent():
    out = P.enb_call_rate(_Conn([("A", 10, 20)]), T0)
    assert set(out) == {"A"} and "D" not in out
    assert P.enb_call_rate(_Conn([]), T0) == {}


def test_rate_tolerates_nulls_and_clips_busy_minutes_to_the_window():
    out = P.enb_call_rate(_Conn([("N", None, None), ("X", 75, 600)]), T0)
    assert out["N"] == pytest.approx(0.0)
    assert out["X"] == pytest.approx(10.0), "무호 분이 없으면 평균(600/60)이다 — 창보다 많은 분 수는 창으로 자른다"


def test_rate_reads_the_hour_before_the_given_time_and_the_enb_table():
    conn = _Conn([])
    P.enb_call_rate(conn, T0)
    sql, params = conn.log[0]
    assert params == (T0 - timedelta(minutes=60), T0), "장애 시작 시각 앞 60분이어야 한다"
    assert P.PM_ENB in sql and "ATTEMPT_CNT" in sql


# --------------------------------------------------------------------------
# 2. _busy_disaster_area
# --------------------------------------------------------------------------


@pytest.mark.parametrize("rate", [None, {}, RuntimeError("창고 끊김")])
def test_missing_empty_or_failing_provider_falls_back_to_the_old_random_area(rate, capsys):
    sc, _calls = _build(_fault(), rate=rate)
    old = _build_plain(_fault())
    assert sc is not None and sc.target.cell_keys == old.target.cell_keys, "예전 무작위 지역이어야 한다"
    if isinstance(rate, Exception):
        assert "호율 조회 실패" in capsys.readouterr().out, "실패는 조용히 넘기지 않고 로그를 남긴다"


@pytest.mark.parametrize("target", ["ENB:600003", "BOX:35.59,139.59,35.63,139.65"])
def test_explicit_targets_ignore_the_rates(target):
    with_rate, calls = _build(_fault(target=target), rate=_rates())
    plain = _build_plain(_fault(target=target))
    assert with_rate.target.cell_keys == plain.target.cell_keys, "지정 대상은 호율과 무관하다"
    assert len(calls) <= 1


def test_provider_is_called_once_per_fault_even_when_built_again():
    fault = _fault()
    calls = []

    def provider(ts):
        calls.append(ts)
        return _rates()

    for _ in range(3):
        faults.build(fault, _master(), T0, faults.target_rng(7, 27), seed=7, enb_rate=provider)
    assert len(calls) == 1, f"공급자가 {len(calls)}번 불렸다 — 한 fault 에 한 번이어야 한다"
    assert calls[0] == fault["start_ts"], "장애 시작 시각으로 부른다"


def test_resolve_active_calls_the_provider_once_across_ticks():
    cache, calls = {}, []

    def provider(ts):
        calls.append(ts)
        return _rates()

    row = _fault()
    for _ in range(4):                                       # 틱마다 같은 행이 다시 온다(새 사전으로)
        scs, bad = faults.resolve_active(_master(), T0, 7, [dict(row)], cache, enb_rate=provider)
        assert scs and not bad
    assert len(calls) == 1, "같은 fault 를 틱마다 다시 조회한다"
    assert len({tuple(sorted(s.target.cell_keys)) for s in scs}) == 1


def test_other_kinds_never_use_the_rates_for_their_target():
    """`earthquake`·`sa_fallback` 의 자동 대상은 예전 그대로다."""
    for kind in ("earthquake", "sa_fallback"):
        a = faults.build(_fault(kind=kind), _master(), T0, faults.target_rng(7, 27), seed=7,
                         enb_rate=lambda ts: _rates())
        b = _build_plain(_fault(kind=kind))
        assert a.target.cell_keys == b.target.cell_keys, kind


# --------------------------------------------------------------------------
# 4. 정답지 down_enbs_rate
# --------------------------------------------------------------------------


def test_truth_down_enbs_rate_is_none_without_a_provider_or_data():
    sc = _build_plain(_fault())
    assert sc.as_truth()["down_enbs_rate"] is None, "창고를 못 읽었으면 None 이다(0 이 아니다 · 불변규칙 17)"
    sc2, _ = _build(_fault(), rate={})
    assert sc2.as_truth()["down_enbs_rate"] is None


def test_truth_down_enbs_rate_equals_the_actual_dead_set():
    sc, _ = _build(_fault(), rate=_rates())
    dead = S.disaster_dead_enbs(sc, sc.start_minute + 5)
    assert set(sc.as_truth()["down_enbs_rate"]) == dead


@pytest.mark.parametrize("kind", ["gw_node_down", "enb_s1_outage", "earthquake", "sa_fallback"])
def test_other_kinds_have_no_down_enbs_rate(kind):
    sc = faults.build(_fault(kind=kind), _master(), T0, faults.target_rng(7, 27), seed=7,
                      enb_rate=lambda ts: _rates())
    assert sc is not None
    assert "down_enbs_rate" not in sc.as_truth()


# --------------------------------------------------------------------------
# 5. 회귀 — 정지 순위를 함수로 뺀 뒤에도 같다
# --------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["local", "wide"])
def test_disaster_rank_of_matches_the_scenario_rank(mode):
    cells = _master()[:75]
    spec = dict(S.KIND_SPECS["natural_disaster"])
    spec.update(kind="natural_disaster", target=S.Target(cell_keys={c.cell_key for c in cells}))
    sc = S.Scenario(id="F-27", start_minute=START, duration=25, **spec)
    sc.target.outage_mode = mode
    sc.__dict__["_pool"] = cells
    assert S._disaster_rank(sc) == S.disaster_rank_of(sc.target.cell_keys, cells, mode, "F-27")
    n = len(S._disaster_rank(sc))
    assert S.disaster_dead_n(n) == math.ceil(0.4 * n)
    assert len({e for e, r in S._disaster_rank(sc).items() if r < S.disaster_dead_n(n)}) == S.disaster_dead_n(n)


def test_local_rank_puts_the_enbs_nearest_the_center_first_and_wide_is_hash_order():
    cells = _master()[:75]
    keys = {c.cell_key for c in cells}
    local = S.disaster_rank_of(keys, cells, "local", "F-27")
    wide = S.disaster_rank_of(keys, cells, "wide", "F-27")
    assert sorted(local.values()) == list(range(len(local)))
    assert local != wide
    # wide 는 좌표를 보지 않는다 — 셀 풀을 비워도 같다
    assert S.disaster_rank_of(keys, [], "wide", "F-27") == wide
    import zlib
    expected = sorted(local, key=lambda e: zlib.crc32(f"F-27:{e}".encode()))
    assert [e for e, _ in sorted(wide.items(), key=lambda x: x[1])] == expected
