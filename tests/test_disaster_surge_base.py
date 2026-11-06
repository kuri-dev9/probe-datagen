"""자연재해 폭주 기준값 — 장애 시작 앞 60분 평균 + 포아송 추첨 (2026-11-05 · F-112).

F-112 에서 폭주량이 "그 분 지역 평소 호 × 39" 라 평소(6~15건)의 출렁임이 그대로 커져 분당 21~62배를 오갔다.
이제 기준은 **장애 시작 앞 60분의 지역 분당 평균**(`faults._surge_base`)이고 더하는 수는 `기준 × 39` 의 포아송
추첨이다 — 흔들림은 남기되 평소의 흔들림을 곱하지 않는다. 기준값이 없으면 전 방식으로 돌아간다.

DB 를 쓰지 않는다 — 합성 셀 마스터 + 가짜 커서/공급자. 허용 범위는 에스터가 정했다(합성 규모 · 고정 시드).
"""

from __future__ import annotations

import json
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
T0 = datetime(2026, 11, 5, 11, 0)
START = 5
ENB_MEAN = 2.0                                   # 합성 공급자의 eNB 당 분당 평균


def _cell(enb: str, n: int, lat: float, lon: float) -> Cell:
    return Cell(
        cell_key=f"{enb}-{n}", enb_id=enb, cell_id=n, enb_seq=0, c_uid="",
        tac=100, plmn="44010", vlan_id="", latitude=lat, longitude=lon,
        adm_sido_cd="11", adm_sido_nm="", adm_sgg_cd="11200", adm_sgg_nm="",
        adm_emd_cd="11200011", adm_emd_nm="", geo_status="MAPPED",
        rat="EUTRAN", mme_id="0001", sgw_id="0037", pgw_id="0037",
        ims_pgw_id="0037", traffic_weight=1.0, display_name="")


def _master() -> list[Cell]:
    """셀 1,800개(eNB 600) — 셀당 평소 호가 1건 미만인 실운영 밀도(`test_disaster.py` 10절과 같다)."""
    out = []
    for e in range(600):
        lat = 35.20 + (e // 30) * 0.012
        lon = 139.20 + (e % 30) * 0.012
        out += [_cell(f"{600000 + e}", n, lat + n * 0.0015, lon + n * 0.0015) for n in range(3)]
    return out


def _mean_rate(value: float = ENB_MEAN, lam: float = 1.5) -> P.EnbRate:
    r = P.EnbRate()
    for e in range(600):
        r[str(600000 + e)] = lam
        r.mean[str(600000 + e)] = value
    return r


def _row(target="LOCAL", kind="natural_disaster", start=START, dur=25):
    return {"fault_id": 27, "kind": kind, "target": target, "start_ts": T0 + timedelta(minutes=start),
            "duration_min": dur, "intensity": 1.0}


@pytest.fixture
def make(monkeypatch):
    def build(seed: int = 11):
        for f in ("pemdb1/_distributions.json", "profile.json"):
            if not (ROOT / "data" / f).exists():
                pytest.fail(f"data/{f} 가 없다")
        monkeypatch.setattr(P, "load_cells", lambda *a, **k: _master())
        monkeypatch.setattr(P, "load_apps", lambda conn: None)
        g = P.PemdbGen(None, ROOT / "data", rates={"lte_call": 1500, "transport": 1500}, seed=seed,
                       subscriber_pool=2500)
        g.anchor = T0
        return g
    return build


def _count_surge(g):
    """분 → `_surge_drafts` 가 낸 호 수(더한 호만 센다)."""
    log: dict[int, int] = {}
    orig = g._surge_drafts

    def wrapped(minute, *a, **k):
        n = 0
        for x in orig(minute, *a, **k):
            n += 1
            yield x
        log[minute] = n

    g._surge_drafts = wrapped
    return log


def _run(g, provider, minutes=START + 14, target="LOCAL"):
    log = _count_surge(g)
    scs, bad = g.set_fault_rows([_row(target=target)], enb_rate=provider)
    assert scs and not bad, bad
    rows = []
    for m in range(minutes):
        ts = T0 + timedelta(minutes=m)
        rows.append(g.records(m, ts))
    return scs[0], log, rows


def _peak(log, lo=1, hi=13):
    return [log[START + e] for e in range(lo, hi) if (START + e) in log]


def _stats(xs):
    mean = sum(xs) / len(xs)
    var = sum((x - mean) ** 2 for x in xs) / (len(xs) - 1)
    return mean, var


# --------------------------------------------------------------------------
# 1. _poisson
# --------------------------------------------------------------------------


@pytest.mark.parametrize("lam", [3.0, 49.0, 51.0, 380.0])
def test_poisson_mean_and_variance_follow_lambda(lam):
    rng = random.Random(5)
    xs = [P._poisson(rng, lam) for _ in range(20000)]
    mean, var = _stats(xs)
    assert mean == pytest.approx(lam, rel=0.03), f"평균 {mean:.2f}"
    assert var == pytest.approx(lam, rel=0.10), f"분산 {var:.2f}"
    assert all(isinstance(x, int) and x >= 0 for x in xs)


def test_poisson_of_380_has_a_standard_deviation_of_about_19_5():
    rng = random.Random(9)
    xs = [P._poisson(rng, 380.0) for _ in range(20000)]
    assert math.sqrt(_stats(xs)[1]) == pytest.approx(19.5, rel=0.05)


@pytest.mark.parametrize("lam", [0.0, -1.0, -380.0])
def test_poisson_of_zero_or_negative_is_zero(lam):
    rng = random.Random(1)
    assert [P._poisson(rng, lam) for _ in range(50)] == [0] * 50


# --------------------------------------------------------------------------
# 2. _surge_base — 셀 몫
# --------------------------------------------------------------------------


def test_cell_share_is_enb_mean_divided_by_its_cell_count():
    cells = _master()
    rate = _mean_rate()
    rate.mean["600003"] = 6.0
    keys = {"600003-0", "600003-1", "600003-2", "600004-0"}
    out = faults._surge_base(cells, keys, rate)
    assert out["600003-0"] == pytest.approx(2.0) and out["600003-2"] == pytest.approx(2.0)
    assert out["600004-0"] == pytest.approx(ENB_MEAN / 3)
    assert set(out) == keys, "대상 밖 셀이 키에 들어 있다"


def test_partial_enb_target_counts_only_the_target_cells_share():
    """`BOX:` 처럼 eNB 를 자르면 대상 안 셀 몫만 — eNB 평균 전체가 아니다."""
    out = faults._surge_base(_master(), {"600010-0", "600010-1"}, _mean_rate(6.0))
    assert sum(out.values()) == pytest.approx(4.0)       # 6 × 2/3


def test_unknown_cells_and_enbs_without_a_mean_are_handled():
    rate = _mean_rate()
    del rate.mean["600020"]
    out = faults._surge_base(_master(), {"600020-0", "NOPE-1"}, rate)
    assert out == {"600020-0": 0.0}, "평균이 없는 eNB 는 0 이고 모르는 셀은 빠진다"


@pytest.mark.parametrize("rate", [None, {}, {"600003": 1.0}, P.EnbRate({"600003": 1.0})])
def test_no_mean_means_no_base(rate):
    assert faults._surge_base(_master(), {"600003-0"}, rate) == {}


# --------------------------------------------------------------------------
# 3. 정답지 surge_base_per_min
# --------------------------------------------------------------------------


def _build(target=None, kind="natural_disaster", rate=None):
    f = _row(target=target, kind=kind)
    return faults.build(f, _master(), T0, faults.target_rng(7, 27), seed=7, enb_rate=(lambda ts: rate))


def test_truth_surge_base_equals_the_sum_of_the_cell_bases():
    sc = _build(rate=_mean_rate())
    base = sum(sc._surge_cell_base.values())
    assert base > 0
    assert sc.as_truth()["surge_base_per_min"] == pytest.approx(round(base, 2))
    assert set(sc._surge_cell_base) == sc.target.cell_keys


def test_truth_surge_base_for_an_explicit_target_is_also_carried():
    sc = _build(target="ENB:600055", rate=_mean_rate())
    assert sc.as_truth()["surge_base_per_min"] == pytest.approx(round(sum(sc._surge_cell_base.values()), 2))


@pytest.mark.parametrize("rate", [None, {}, {str(600000 + e): 1.0 for e in range(600)}])
def test_truth_surge_base_is_null_without_a_mean(rate):
    sc = _build(rate=rate)
    assert sc is not None and sc.as_truth()["surge_base_per_min"] is None


def test_truth_surge_base_is_null_when_all_means_are_zero():
    sc = _build(rate=_mean_rate(0.0))
    assert sc.as_truth()["surge_base_per_min"] is None, "모름을 0 으로 적었다(불변규칙 17)"


@pytest.mark.parametrize("kind", ["earthquake", "gw_node_down", "enb_s1_outage", "sa_fallback"])
def test_other_kinds_have_no_surge_base_key(kind):
    sc = _build(kind=kind, rate=_mean_rate())
    assert sc is not None and "surge_base_per_min" not in sc.as_truth()


# --------------------------------------------------------------------------
# 4. 실제 폭주 — 포아송 흩어짐 · 평소 흔들림을 곱하지 않는다
# --------------------------------------------------------------------------


def test_surge_adds_about_base_times_39_each_peak_minute_with_poisson_spread(make):
    pooled_var, pooled_mean, n = 0.0, 0.0, 0
    for seed in (11, 12, 13):                              # 한 시드는 12분뿐이라 분산 추정이 흔들린다
        g = make(seed)
        sc, log, _rows = _run(g, lambda ts: _mean_rate())
        xs = _peak(log)
        assert len(xs) == 12
        base = sum(sc._surge_cell_base.values())
        mean, var = _stats(xs)
        assert mean == pytest.approx(base * (S.DISASTER_SURGE_PEAK - 1), rel=0.08), \
            f"seed {seed}: 더한 호 평균 {mean:.0f} (기대 {base * 39:.0f})"
        pooled_var += var * (len(xs) - 1)
        pooled_mean += mean * len(xs)
        n += len(xs)
    ratio = (pooled_var / (n - 3)) / (pooled_mean / n)
    print(f"poisson ratio {ratio:.2f}")
    assert 0.6 <= ratio <= 1.6, f"분산/평균 {ratio:.2f} — 포아송(약 1)이 아니다"


def test_surge_is_flat_where_the_old_method_swung_with_the_natural_wobble(make):
    """회귀 — 같은 데이터에서 **기준값이 없는 경로(전 방식)** 는 분산/평균이 훨씬 크다. 평소 호의 흔들림이 ×39 로
    커지기 때문이다. 기준값 경로는 그렇지 않다."""
    g_new = make()
    _sc, log_new, _ = _run(g_new, lambda ts: _mean_rate())
    g_old = make()
    sc_old, log_old, _ = _run(g_old, lambda ts: {str(600000 + e): 1.5 for e in range(600)})   # plain dict → 평균 없음
    assert sc_old.as_truth()["surge_base_per_min"] is None
    mn, vn = _stats(_peak(log_new))
    mo, vo = _stats(_peak(log_old))
    assert vn / mn < 3.0, f"기준값 경로 분산/평균 {vn / mn:.1f}"
    print(f"new {vn / mn:.2f} old {vo / mo:.1f}")
    assert vo / mo > 5.0, f"전 방식은 분산/평균이 커야 한다({vo / mo:.1f}) — 전제가 어긋났다"


def test_fallback_when_the_provider_is_absent_fails_or_has_no_mean(make):
    """후퇴 — 공급자 없음 · 예외 · 평균 없는 dict · 평균이 모두 0: 전 방식(그 분 평소 호 × 39)으로 돌고 폭주는 계속 난다."""
    def boom(ts):
        raise RuntimeError("창고 끊김")

    cases = {"없음": None, "예외": boom, "평균 없는 dict": lambda ts: {str(600000 + e): 1.5 for e in range(600)},
             "평균 0": lambda ts: _mean_rate(0.0)}
    for name, provider in cases.items():
        g = make()
        sc, log, _rows = _run(g, provider)
        xs = _peak(log)
        assert len(xs) == 12 and sum(xs) > 0, f"{name}: 폭주가 나지 않았다"
        assert sc.__dict__.get("_surge_cell_base") in (None, {}) or name == "평균 0"
        # 그 분 평소 호 × 39 근처다 — 평균 20건 안팎의 지역에서 수백 건
        assert 100 <= sum(xs) / len(xs) <= 2500, f"{name}: 평균 {sum(xs) / len(xs):.0f}"


def test_no_surge_before_the_start_and_after_the_end(make):
    g = make()
    _sc, log, _ = _run(g, lambda ts: _mean_rate())
    assert all(log.get(START + e, 0) == 0 for e in (-2, -1)), "시작 전에 더한 호가 있다"
    assert all(log.get(START + e, 0) == 0 for e in (13, 14)), "13분 뒤에도 폭주가 남았다"
    assert 0 < log[START] < log[START + 1], "0분째는 차오르는 중이라 정점보다 작다"


def test_surge_still_respects_the_national_cap(make):
    g = make()
    _sc, log, _ = _run(g, lambda ts: _mean_rate(500.0))        # 일부러 매우 큰 기준
    # 상한은 그 분 평소 호 수(`n_calls` ≈ 분당 1,500)의 `SURGE_CAP` 배다
    assert max(log.values()) <= P.PemdbGen.SURGE_CAP * 1500 * 1.1, max(log.values())
    assert max(log.values()) > 1500, "기준이 크면 상한에 닿아야 한다 — 상한 검사가 아무것도 가르지 못한다"


# --------------------------------------------------------------------------
# 5. enb_call_rate 회귀 — 값은 그대로 · .mean 은 SUM ÷ 60
# --------------------------------------------------------------------------


class _Cur:
    def __init__(self, rows):
        self.rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=()):
        pass

    def fetchall(self):
        return self.rows


class _Conn:
    def __init__(self, rows):
        self.rows = rows

    def cursor(self):
        return _Cur(self.rows)


def test_enb_call_rate_values_are_unchanged_and_mean_is_sum_over_sixty():
    rows = [("A", 30, 300), ("B", 59, 59), ("C", 60, 120)]
    out = P.enb_call_rate(_Conn(rows), T0)
    assert out["A"] == pytest.approx(-math.log(0.5)) and out["B"] == pytest.approx(59 / 60)
    assert out["C"] == pytest.approx(2.0)
    assert out.mean == {"A": pytest.approx(5.0), "B": pytest.approx(59 / 60), "C": pytest.approx(2.0)}


def test_enb_rate_still_behaves_as_a_plain_dict():
    out = P.enb_call_rate(_Conn([("A", 30, 300)]), T0)
    assert isinstance(out, dict) and out == {"A": pytest.approx(-math.log(0.5))}
    assert json.loads(json.dumps(out)) == json.loads(json.dumps({"A": -math.log(0.5)}))
    assert not P.enb_call_rate(_Conn([]), T0), "비면 falsy 여야 한다(`_rate_at` 의 `or None`)"
