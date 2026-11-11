"""틱 경로가 장애 메모(`NOTE`)를 읽는다 — 2026-11-11 (19번 MME-3·20번 SGW-7 연속 · 맥스 수정 고정).

1초 틱이 활성 장애를 읽는 질의(`FAULT_SELECT_ACTIVE`)에 `NOTE` 가 없어, kind 를 나눠 쓰는 18·19·20·26번이
원본 번호(1·10·12·8)로 읽혔다. 그 결과 순환 몫이 `scn:10` 처럼 남의 번호가 되어 장비가 회차마다 안 바뀌었다.
DB 없이 — 가짜 커넥션과 `faults` 함수만 쓴다.
"""
from __future__ import annotations

from datetime import datetime

import pytest

from datagen import control, faults

NOW = datetime(2026, 11, 11, 7, 24, 3)
#: 번호를 나눠 쓰는 네 쌍 — (번호, kind, 메모 없이 읽혔을 때의 원본 번호)
SHARED = [(18, "gw_resource", 1), (19, "mme_overload", 10), (20, "ims_pdn", 12), (26, "ue_model", 8)]


class _Cur:
    """`poll_transitions` 가 쓰는 만큼만 흉내 — 마지막 SELECT 의 컬럼 별칭을 그대로 `description` 에 노출한다."""

    def __init__(self, rows_by_cols):
        self._rows = rows_by_cols
        self.description = None
        self._cur = []
        self.sql = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.sql.append(sql)
        if sql.startswith("SELECT"):
            head = sql.split(" FROM ", 1)[0]
            cols = [p.split(" AS ")[-1].strip() for p in head[len("SELECT "):].split(", ")]
            self.description = [(c,) for c in cols]
            self._cur = [tuple(r.get(c) for c in cols) for r in self._rows]

    def fetchall(self):
        return self._cur


class _Conn:
    def __init__(self, rows):
        self.cur = _Cur(rows)

    def cursor(self):
        return self.cur

    def commit(self):
        pass


def _row(fid, kind, note):
    return {"fault_id": fid, "kind": kind, "target": "auto", "start_ts": NOW, "duration_min": 11,
            "intensity": 1.0, "note": note}


# ── 질의 모양 ────────────────────────────────────────────────────────────────

def test_active_query_selects_note():
    assert "NOTE AS note" in control.FAULT_SELECT_ACTIVE


def test_active_query_columns_are_the_eight_the_tick_reads():
    """일곱에서 여덟이 됐다 — `scenario_no` 가 더해졌다(2026-11-11 `CM_DATAGEN_FAULT` 조건 칸).
    칸이 비면(기록 전 장애) `faults.scenario_no` 가 메모 `note` 를 읽으므로 `note` 는 그대로 있어야 한다."""
    head = control.FAULT_SELECT_ACTIVE[len("SELECT "):]
    cols = [p.split(" AS ")[-1].strip() for p in head.split(", ")]
    assert cols == ["fault_id", "kind", "target", "start_ts", "duration_min", "intensity", "note",
                    "scenario_no"]


def test_poll_transitions_returns_note_in_each_active_row():
    conn = _Conn([_row(275, "mme_overload", "#19 no19-v7"), _row(276, "ims_pdn", "#20 no20-v7")])
    active = control.poll_transitions(conn, NOW)
    assert [a["note"] for a in active] == ["#19 no19-v7", "#20 no20-v7"]


# ── 틱 경로에서 번호 읽기 ────────────────────────────────────────────────────

@pytest.mark.parametrize("no,kind,_org", SHARED)
def test_tick_row_with_note_is_read_as_its_own_number(no, kind, _org):
    (row,) = control.poll_transitions(_Conn([_row(300 + no, kind, f"#{no} no{no}-v7")]), NOW)
    assert faults.scenario_no(row) == no


@pytest.mark.parametrize("no,kind,_org", SHARED)
def test_tick_row_gets_its_own_rotation_share(no, kind, _org):
    (row,) = control.poll_transitions(_Conn([_row(300 + no, kind, f"#{no} no{no}-v7")]), NOW)
    assert faults._own_group(row) == f"scn:{no}"


@pytest.mark.parametrize("no,kind,org", SHARED)
def test_without_note_the_shared_number_falls_back_to_the_original(no, kind, org):
    """메모가 없으면 원본 번호로 읽힌다 — 질의에서 `note` 가 빠졌던 때의 증상이다."""
    assert faults.scenario_no({"kind": kind, "note": None}) == org
    assert faults._own_group({"kind": kind}) == f"scn:{org}"


def test_four_shared_numbers_get_four_distinct_shares():
    rows = control.poll_transitions(
        _Conn([_row(300 + no, kind, f"#{no} x") for no, kind, _ in SHARED]), NOW)
    assert [faults._own_group(r) for r in rows] == [f"scn:{no}" for no, _, _ in SHARED]
