"""평시 배경 절단의 요청 시각과 `callflow.drop` 간격 회귀 (2026-11-05).

평시 절단이 해제 Cause 만 적고 `UEContextReleaseRequest_Time` 을 `0.0` 으로 남겼다(평시 절단 전부).
노드 다운 테스트의 504 필터가 이 호를 다운 절단으로 잘못 잡아 시드에 따라 실패했다. 평시 절단에는 요청
시각과 Cause 를 채우되 `first_error_*` 는 비운다 — 평시 호에 오류 기록을 세우면 룰이 증상만 보고
발동한다(불변규칙 7).

`callflow.drop` 은 간격 계산만 `request_gap` 으로 뺐다 — 난수 흐름과 Cause 200 의 지연(0.7~3.7초)이 그대로여야 한다.
"""
from __future__ import annotations

import random
from datetime import timedelta

import pytest

from datagen import callflow
from test_disaster import T0, _col, make  # noqa: F401  (make 는 픽스처)

EMPTY = ("", "0", "0.0", "0.000000", None)


# ---------------------------------------------------------------- request_gap · drop 회귀

def test_gap_of_cause_200_is_between_0_7_and_3_7_seconds():
    rng = random.Random(1)
    gaps = [callflow.request_gap(200, rng) for _ in range(2000)]
    assert min(gaps) >= 0.7 and max(gaps) <= 3.7
    assert max(gaps) > 3.0 and min(gaps) < 1.4, "범위 전체를 쓴다"


@pytest.mark.parametrize("cause", [18, 20, 504, 1])
def test_gap_of_other_causes_is_one_to_twelve_milliseconds(cause):
    rng = random.Random(2)
    gaps = [callflow.request_gap(cause, rng) for _ in range(500)]
    assert min(gaps) >= 0.001 and max(gaps) <= 0.012


def test_gap_uses_exactly_one_random_draw():
    """난수 흐름이 이 함수 때문에 밀리지 않는다 — 옛 인라인 계산도 한 번이었다."""
    a, b = random.Random(9), random.Random(9)
    callflow.request_gap(200, a)
    b.random()
    assert a.random() == b.random()


def _success_rec(g, ct="3"):
    """생성기가 낸 연결 호(절단 아님)를 `Rec` 로 감싼다 — 호 시각과 단계 길이가 채워져 있다."""
    from datagen.scenarios import Rec

    i = g.gen.index["lte_call"]
    for m in range(20):
        for r in g.records(m, T0 + timedelta(minutes=m)):
            if (r[i["call_type"]] == ct and r[i["success_flag"]] in ("1", "True")
                    and r[i["drop_flag"]] in ("0", "False", "") and float(r[i["call_duration_time"]] or 0) > 0):
                return Rec(list(r), g.gen.index["lte_call"], "lte_call")
    pytest.fail("연결된 호를 못 만들었다")


@pytest.mark.parametrize("cause,lo,hi", [(200, 0.7, 3.7), (504, 0.001, 0.012), (20, 0.001, 0.012)])
def test_drop_keeps_the_request_to_release_gap_per_cause(make, cause, lo, hi):
    g = make()
    for seed in range(40):
        rec = _success_rec(g)
        assert callflow.drop(rec, cause, random.Random(seed)) is True
        req, rel = rec.num("UEContextReleaseRequest_Time"), rec.num("UEContextRelease_Time")
        assert lo - 1e-5 <= rel - req <= hi + 1e-5, (cause, seed, rel - req)
        assert rec.get("UEContextReleaseRequest_Cause") == rec.get("UEContextRelease_Cause") == str(cause)
        assert rec.num("call_start_time") < req


def test_drop_gap_equals_the_helper_applied_to_the_same_stream(make):
    """`drop` 이 `request_gap` 과 같은 값을 쓴다 — 같은 시드의 두 번째 추첨이 간격이다."""
    g = make()
    rec = _success_rec(g)
    lo, hi = (float(v) for v in callflow._spec().drop.get("duration_scale", (0.15, 0.6)))
    expected = random.Random(5)
    expected.uniform(lo, hi)
    gap = callflow.request_gap(200, expected)
    callflow.drop(rec, 200, random.Random(5))
    assert (rec.num("UEContextRelease_Time") - rec.num("UEContextReleaseRequest_Time")) == pytest.approx(gap, abs=2e-6)


def test_drop_leaves_unconnected_calls_alone(make):
    g = make()
    rec = _success_rec(g)
    rec.set("success_flag", "0")
    assert callflow.drop(rec, 200, random.Random(1)) is False


# ---------------------------------------------------------------- 평시 절단 레코드

@pytest.fixture
def cuts(make):
    g = make()
    rows = []
    for m in range(14):
        rows += g.records(m, T0 + timedelta(minutes=m))
    i = g.gen.index["lte_call"]
    cut = [r for r in rows if r[i["drop_flag"]] in ("1", "True")]
    return g, i, cut


def test_plain_cuts_exist_in_a_run_without_any_fault(cuts):
    _g, _i, cut = cuts
    assert len(cut) > 20, f"평시 절단이 너무 적다: {len(cut)}"


def test_every_plain_cut_has_a_request_time(cuts):
    _g, i, cut = cuts
    zero = [r for r in cut if float(r[i["UEContextReleaseRequest_Time"]] or 0) == 0.0]
    assert not zero, f"요청 시각이 0.0 인 평시 절단 {len(zero)}/{len(cut)}"


def test_request_is_between_call_start_and_release(cuts):
    _g, i, cut = cuts
    bad = []
    for r in cut:
        t0, req, rel = (float(r[i[f]] or 0) for f in
                        ("call_start_time", "UEContextReleaseRequest_Time", "UEContextRelease_Time"))
        if not (t0 <= req <= rel + 1e-6):
            bad.append((t0, req, rel))
    assert not bad, bad[:3]


def test_request_cause_equals_the_final_release_cause(cuts):
    """전환이 해제 Cause 를 바꾼 호도 최종 Cause 를 따른다(`_baseline_handover` 뒤에 읽는다)."""
    _g, i, cut = cuts
    bad = [(r[i["UEContextReleaseRequest_Cause"]], r[i["UEContextRelease_Cause"]]) for r in cut
           if str(r[i["UEContextReleaseRequest_Cause"]]).split(".")[0] != str(r[i["UEContextRelease_Cause"]]).split(".")[0]]
    assert not bad, bad[:5]


def test_plain_cuts_carry_no_first_error(cuts):
    _g, i, cut = cuts
    for f in ("first_error_interface_protocol", "first_error_message", "first_error_time", "first_error_cause"):
        got = [r[i[f]] for r in cut if r[i[f]] not in EMPTY]
        assert not got, (f, got[:3])


def test_a_plain_cut_never_looks_like_a_node_down_cut(cuts):
    """다운 테스트가 First Error 504 로 다운 절단을 가린다 — 평시 504 가 거기 섞이지 않는다."""
    _g, i, cut = cuts
    assert not [r for r in cut if r[i["first_error_cause"]] in ("504", "504.0")]


def test_plain_cuts_are_deterministic_for_the_same_seed(make):
    def run():
        g = make()
        return [r[g.gen.index["lte_call"]["UEContextReleaseRequest_Time"]] for m in range(3)
                for r in g.records(m, T0 + timedelta(minutes=m))
                if r[g.gen.index["lte_call"]["drop_flag"]] in ("1", "True")]
    assert run() == run()
