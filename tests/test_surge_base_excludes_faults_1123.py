"""평소 호율 `enb_call_rate` 가 창 안의 다른 폭주 장애 분을 뺀다 (2026-11-23 · F-550).

F-550 4번 지진 발생 2회의 기준 호 854 는 평소(약 300)의 3배였다. 기준 창(05:40 직전 60분)에 같은 지역 F-548
폭주 13분(분당 약 3,000건)이 들어 있었다. 결정 2 「장애 없는 평소 × 진도 배율」이 지켜지도록 생성기 원장
(`CM_DATAGEN_FAULT`)의 폭주 계열 장애(`SURGE_KINDS`)가 활성이던 분을 평균과 λ 에서 뺀다. DB 를 쓰지 않고
SQL 문구로 갈라 답하는 가짜 연결을 쓴다.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta

import pytest

from datagen import pemdb1 as P

END = datetime(2026, 11, 23, 5, 40)


class _Cur:
    def __init__(self, conn):
        self.c = conn

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=()):
        self.c.log.append((sql, params))
        if "CM_DATAGEN_FAULT" in sql:
            if self.c.ledger_error:
                raise RuntimeError("ledger down")
            self.rows = self.c.ledger
        elif " IN (" in sql and "EVENT_TIME" in sql:
            self.rows = self.c.cut
        else:
            self.rows = self.c.main

    def fetchall(self):
        return self.rows


class _Conn:
    def __init__(self, main, ledger=(), cut=(), ledger_error=False):
        self.main, self.ledger, self.cut, self.ledger_error = main, list(ledger), list(cut), ledger_error
        self.log = []

    def cursor(self):
        return _Cur(self)

    def pm_queries(self):
        return [s for s, _ in self.log if "CM_DATAGEN_FAULT" not in s]


def _fault(start, minutes):
    return (start, minutes)


# 1. 창 안 지진 장애 — 그 분의 호가 빠지고 평균·λ 가 남은 분 수로 계산된다
def test_surge_minutes_are_removed_from_mean_and_lambda():
    # 60분 합 53,100 = 평소 47분 × 300(14,100) + 폭주 13분 × 3,000(39,000)
    main = [("A", 60, 53_100.0)]
    # 뺄 분 13: 모두 호가 있었고 합 39,000 → 남은 47분 합 14,100, 평균 300
    cut = [("A", 13, 39_000.0)]
    conn = _Conn(main, ledger=[_fault(END - timedelta(minutes=37), 13)], cut=cut)
    out = P.enb_call_rate(conn, END)
    assert out.mean["A"] == pytest.approx(14_100 / 47)
    assert out.mean["A"] == pytest.approx(300.0)
    # 남은 분이 전부 호가 있어 무호 분 비율 0 → λ 는 평균으로 돌아간다
    assert out["A"] == pytest.approx(out.mean["A"])


def test_lambda_uses_remaining_minutes_for_idle_ratio():
    # 60분 중 호 있는 분 40 · 뺄 분 10 중 호 있는 분 10 → 남은 50분 중 30분이 호 있음 → idle 0.4
    conn = _Conn([("A", 40, 1_000.0)], ledger=[_fault(END - timedelta(minutes=30), 10)],
                 cut=[("A", 10, 400.0)])
    out = P.enb_call_rate(conn, END)
    assert out.mean["A"] == pytest.approx(600 / 50)
    assert out["A"] == pytest.approx(min(600 / 50, -math.log(20 / 50)))


# 2. 매일 부하와 취소된 장애는 빠지지 않는다 — 원장 질의가 폭주 계열 + active/done 만 고른다
def test_ledger_query_selects_only_surge_kinds_and_not_cancelled():
    conn = _Conn([("A", 30, 300.0)], ledger=[])
    P.enb_call_rate(conn, END)
    sql, params = next((s, p) for s, p in conn.log if "CM_DATAGEN_FAULT" in s)
    assert set(params[:2]) == {"natural_disaster", "signaling_storm"}
    assert "capacity_shift" not in params
    assert "'active', 'done'" in sql and "cancelled" not in sql


def test_ledger_rows_of_other_shapes_are_ignored():
    # 질의가 거른 뒤에도 모양이 다른 행(가짜 커서·이상 행)은 건너뛴다
    conn = _Conn([("A", 30, 300.0)], ledger=[("x",), ("a", "b", "c"), (None, 5)])
    assert P._surge_minutes(conn, END - timedelta(minutes=60), END) == set()


# 3. 창보다 앞에서 시작했거나 창 끝을 넘으면 창 안의 분만 뺀다
def test_minutes_are_clipped_to_the_window():
    lo = END - timedelta(minutes=60)
    before = _fault(lo - timedelta(minutes=10), 30)      # 창 앞 10분은 밖 · 창 안 20분
    after = _fault(END - timedelta(minutes=5), 30)       # 창 끝 5분만 안
    got = P._surge_minutes(_Conn([], ledger=[before]), lo, END)
    assert got == {lo + timedelta(minutes=i) for i in range(20)}
    got = P._surge_minutes(_Conn([], ledger=[after]), lo, END)
    assert got == {END - timedelta(minutes=i) for i in range(1, 6)}
    assert all(lo <= m < END for m in got)


def test_overlapping_faults_do_not_double_count_minutes():
    lo = END - timedelta(minutes=60)
    a = _fault(lo + timedelta(minutes=10), 10)
    b = _fault(lo + timedelta(minutes=15), 10)           # 5분 겹침
    assert len(P._surge_minutes(_Conn([], ledger=[a, b]), lo, END)) == 15


# 4. 뺄 분이 창 전체(60분)면 빼지 않고 지금처럼 평균을 낸다 — 0 이 아니다
def test_whole_window_is_not_removed():
    main = [("A", 60, 18_000.0)]
    conn = _Conn(main, ledger=[_fault(END - timedelta(minutes=90), 120)], cut=[("A", 60, 18_000.0)])
    out = P.enb_call_rate(conn, END)
    assert out.mean["A"] == pytest.approx(300.0) and out["A"] == pytest.approx(300.0)
    assert len(conn.pm_queries()) == 1, "빼지 않으면 뺄 분 질의도 없다"


# 5. 원장 조회가 예외를 내면 지금과 같은 결과다
def test_ledger_error_falls_back_to_plain_average():
    main = [("A", 30, 300.0), ("B", 60, 6_000.0)]
    err = _Conn(main, ledger_error=True)
    plain = _Conn(main, ledger=[])
    a, b = P.enb_call_rate(err, END), P.enb_call_rate(plain, END)
    assert dict(a) == dict(b) and a.mean == b.mean
    assert a.mean["A"] == pytest.approx(5.0) and a.mean["B"] == pytest.approx(100.0)
    assert P._surge_minutes(err, END - timedelta(minutes=60), END) == set()


# 6. 뺄 분이 없으면 질의가 늘지 않고 값이 같다 (F-548 회귀)
def test_no_surge_minutes_means_no_extra_query_and_same_values():
    conn = _Conn([("A", 30, 300.0), ("B", 59, 59.0), ("C", 60, 120.0)], ledger=[])
    out = P.enb_call_rate(conn, END)
    assert len(conn.pm_queries()) == 1
    assert out["A"] == pytest.approx(-math.log(0.5)) and out["B"] == pytest.approx(59 / 60)
    assert out["C"] == pytest.approx(2.0)
    assert out.mean == {"A": pytest.approx(5.0), "B": pytest.approx(59 / 60), "C": pytest.approx(2.0)}


def test_fault_outside_window_removes_nothing():
    conn = _Conn([("A", 30, 300.0)], ledger=[_fault(END - timedelta(minutes=200), 30)])
    P.enb_call_rate(conn, END)
    assert len(conn.pm_queries()) == 1


def test_enb_missing_from_cut_keeps_its_own_values():
    # 뺄 분에 호 기록이 없는 eNB 는 본 질의 값 그대로(분 수만 남은 분으로 나눈다)
    conn = _Conn([("A", 20, 200.0), ("B", 20, 400.0)], ledger=[_fault(END - timedelta(minutes=20), 10)],
                 cut=[("B", 10, 300.0)])
    out = P.enb_call_rate(conn, END)
    assert out.mean["A"] == pytest.approx(200 / 50)
    assert out.mean["B"] == pytest.approx(100 / 50)
