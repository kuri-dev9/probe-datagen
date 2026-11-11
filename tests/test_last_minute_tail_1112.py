"""장애 마지막 1분 누락 수정과 꼬리 끝 수정 — 2026-11-12 (맥스 수정 고정 · 8번 F-264·F-284).

* 실시간 틱은 방금 닫힌 분(`ts`)을 그 분 + 3초에 만든다. 활성 목록을 벽시계로 고르면 마지막 분을 만들 때 장애가 이미
  `done` 이라 **모든 장애가 마지막 1분을 잃었다**(정답지 11분 · 실제 효과 10분). `poll_transitions(slot=ts)` 가 그 분이 창 안인
  장애를 고른다.
* `floor_tail_until` 은 진행 중인 장애를 "지금 끝나면"으로 세서 주입 직후 값이 주입 끝보다 앞섰다(F-286 · 75분 주입에 16:13).
  계획된 끝 분까지 간다고 보고 센다. 취소되면 활성 목록에서 빠져 실제 마지막 분으로 돌아가고, 24번은 넣지 않는다.
DB 없이 — 가짜 커서와 `PemdbGen` fixture 를 쓴다.
"""
from __future__ import annotations

import inspect
import re
from datetime import datetime, timedelta

from datagen import cli, control
from test_capacity_shift import gen  # noqa: F401 — 생성기 fixture
from test_fault_conditions_1111 import _Conn, _stmts

NOW = datetime(2026, 11, 11, 13, 57, 3)
SLOT = datetime(2026, 11, 11, 13, 56)


def _select(conn):
    return next((s, p) for s, p in conn.cur.sql if s.startswith("SELECT"))


# ── 항목 1 — `poll_transitions(slot=)` ────────────────────────────────────────
def test_slot_also_picks_faults_that_are_done_but_still_cover_the_slot():
    conn = _Conn()
    control.poll_transitions(conn, NOW, slot=SLOT)
    sql, params = _select(conn)
    assert "STATUS='active' OR (STATUS='done'" in sql
    assert "DATE_ADD(START_TIME, INTERVAL DURATION_MIN MINUTE) > %s" in sql        # 끝 분 + 1 이 슬롯보다 뒤인 것만
    assert params == [SLOT]


def test_last_minute_slot_is_inside_and_the_end_slot_is_outside():
    """`끝 > 슬롯` 이다 — 마지막 분(시작+길이−1분)은 들어오고 시작+길이 분은 들어오지 않는다."""
    start, dur = datetime(2026, 11, 11, 13, 46), 11
    end = start + timedelta(minutes=dur)
    last_minute, end_slot = end - timedelta(minutes=1), end
    assert end > last_minute and not (end > end_slot)       # SQL 의 `> %s` 와 같은 비교


def test_slot_never_adds_cancelled_faults():
    conn = _Conn()
    control.poll_transitions(conn, NOW, slot=SLOT)
    sql, _ = _select(conn)
    assert "cancelled" not in sql.lower().replace("-- ", "")     # 사람이 끊은 장애는 넣지 않는다


def test_without_slot_only_active_faults_come_back_as_before():
    conn = _Conn()
    control.poll_transitions(conn, NOW)
    sql, params = _select(conn)
    assert "STATUS='active' ORDER BY FAULT_ID" in sql and "done" not in sql and not params


def test_state_transitions_still_use_the_wall_clock_not_the_slot():
    conn = _Conn()
    control.poll_transitions(conn, NOW, slot=SLOT)
    (done_sql, done_params), = [x for x in _stmts(conn, "UPDATE") if "SET STATUS='done'" in x[0]]
    assert done_params == [NOW, NOW]                         # 전이는 벽시계 — 슬롯이 아니다
    (act_sql, act_params), = [x for x in _stmts(conn, "UPDATE") if "SET STATUS='active'" in x[0]]
    assert act_params == [NOW, NOW]


# ── 항목 2 — `cmd_run` 이 슬롯을 넘긴다 ────────────────────────────────────────
def test_cmd_run_passes_the_slot_being_made():
    src = inspect.getsource(cli.cmd_run)
    assert re.search(r"control\.poll_transitions\(conn, datetime\.now\(\),.*?slot=ts\)", src, re.S)


# ── 항목 3 — `floor_tail_until` ───────────────────────────────────────────────
T0 = datetime(2026, 10, 29, 16, 0)
ROW = {"fault_id": 8, "kind": "ran_radio", "target": "ENB:500007", "start_ts": T0, "duration_min": 11, "intensity": 1.0}


def _running(gen, rows=(ROW,), minutes=3):
    gen.anchor = T0
    gen.fault.floor_tail_min = 6
    gen.set_fault_rows([dict(r) for r in rows])
    for m in range(minutes):
        gen.records(m, T0 + timedelta(minutes=m))


def _at(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d %H:%M")


def test_running_fault_tail_is_never_before_the_planned_end(gen):
    _running(gen)
    got = _at(gen.floor_tail_until()["8"])
    assert got >= T0 + timedelta(minutes=11), got                  # 주입 끝(16:11) 이후 — 전에는 16:09 였다


def test_running_fault_tail_adds_the_tail_length_of_the_end_minute_floor(gen):
    from datagen.scenarios import visible_level

    _running(gen)
    sc = gen.gen.scenarios[0]
    fin = sc.start_minute + sc.duration - 1
    w = gen.fault.floor_by_kind.get(sc.kind, gen.fault.floor_per_cell) * visible_level(sc, fin)
    want = T0 + timedelta(minutes=fin + gen._tail_len(w) + 1)
    assert _at(gen.floor_tail_until()["8"]) >= want


def test_a_dropped_fault_falls_back_to_the_last_made_minute(gen):
    _running(gen)
    gen.set_fault_rows([])                                          # 취소 — 활성 목록에서 빠진다
    got = _at(gen.floor_tail_until()["8"])
    assert T0 + timedelta(minutes=3) <= got < T0 + timedelta(minutes=11), got


def test_capacity_shift_is_not_forecast_and_its_phase_is_untouched(gen):
    gen.anchor = T0
    gen.fault.floor_tail_min = 6
    gen.set_fault_rows([{"fault_id": 1, "kind": "capacity_shift", "target": None, "start_ts": T0,
                         "duration_min": 40, "intensity": 1.0}])
    sc = gen.gen.scenarios[0]
    for m in range(3):
        gen.records(m, T0 + timedelta(minutes=m))
    before = getattr(sc, "_minute", None)
    got = gen.floor_tail_until()
    assert getattr(sc, "_minute", None) == before                   # `visible_level` 이 국면(`_minute`)을 바꾸지 않았다
    if "1" in got:
        assert _at(got["1"]) < T0 + timedelta(minutes=40)           # 계획된 끝까지 가정하지 않는다
