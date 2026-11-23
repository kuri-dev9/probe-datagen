"""매일 같은 시각 부하 — 셀이 0개인 대상은 등록하지 않는다 (2026-11-22 · F-524~529 · 맥스 `cli._daily_load`).

묶음 1 에 생성 범위 밖 역(杉並 쪽 좌표 셋)이 들어 있어, 매시 등록 → 해석 때 「셀 0개」 취소를 되풀이하며 장애 목록을 채웠다.
이제 등록 전에 해석과 같은 함수(`faults._daily_cells`)로 셀을 세어 0개면 건너뛰고 로그는 토큰마다 한 번만 남긴다. `cells` 를 안 주면 옛 동작.
"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest
from test_daily_load_groups_1121 import ST1, ST2, _Conn

from datagen import cli, faults

OUT1 = "ST:35.70494-139.63541"                    # 생성 범위 밖 역(셀 0개)
OUT2 = "ST:35.70484-139.63577"
CELLS = [SimpleNamespace(cell_key="1-0", enb_id="1"), SimpleNamespace(cell_key="2-0", enb_id="2")]


def _at(h, m=0, s=3):
    return datetime(2026, 11, 22, h, m, s)


@pytest.fixture(autouse=True)
def _stations(monkeypatch):
    # 역 표에는 셋이 다 있다 — 新宿(1-0)·ST2(2-0)는 이 gen 의 셀이고, 범위 밖 역은 셀 마스터에 없는 셀이다
    monkeypatch.setattr(faults, "STATION_CELLS", {
        faults.station_key(ST1[3:]): {"1-0"}, faults.station_key(ST2[3:]): {"2-0"},
        faults.station_key(OUT1[3:]): {"900-0"}, faults.station_key(OUT2[3:]): {"901-0"}}, raising=False)
    monkeypatch.setattr(faults, "load_stations", lambda conn: None, raising=False)
    monkeypatch.setattr(faults, "ADM3_CELLS", {"x": {"1-0"}}, raising=False)
    monkeypatch.setattr(faults, "load_adm3", lambda conn: None, raising=False)
    monkeypatch.setattr(cli, "_DAILY_NO_CELLS", set())


def _targets(c):
    return sorted(f["target"] for f in c.faults)


# ── ① 등록 여부 ────────────────────────────────────────────────────────────

def test_a_station_without_cells_is_not_registered_but_the_others_are():
    c = _Conn([f"on|18|{ST1},{OUT1},{OUT2}"])
    fid = cli._daily_load(c, _at(18), 0, CELLS)
    assert _targets(c) == [f"{cli.DAILY_PREFIX}:{ST1}"] and fid is not None


def test_only_out_of_range_stations_register_nothing_and_return_none():
    c = _Conn([f"on|18|{OUT1},{OUT2}"])
    assert cli._daily_load(c, _at(18), 0, CELLS) is None and c.faults == []


def test_every_station_with_cells_still_registers_with_the_same_length():
    c = _Conn([f"on|18,19|{ST1},{ST2}"])
    cli._daily_load(c, _at(18), 0, CELLS)
    assert _targets(c) == sorted(f"{cli.DAILY_PREFIX}:{t}" for t in (ST1, ST2))
    assert all(f["dur"] == 120 and f["mode"] == "daily" for f in c.faults)


def test_enb_and_cell_tokens_without_cells_are_skipped_too():
    c = _Conn(["on|18|ENB:999,CELL:999-1,ENB:2"])
    cli._daily_load(c, _at(18), 0, CELLS)
    assert _targets(c) == [f"{cli.DAILY_PREFIX}:ENB:2"]


def test_a_station_missing_from_the_station_table_is_skipped():
    c = _Conn(["on|18|ST:1.0-2.0"])                    # 역 표에 없는 좌표
    assert cli._daily_load(c, _at(18), 0, CELLS) is None and c.faults == []


# ── ② 로그는 토큰마다 한 번 ────────────────────────────────────────────────

def test_the_no_cell_log_is_printed_once_per_token_across_hours(capsys):
    c = _Conn([f"on|18,20|{ST1},{OUT1}"])
    cli._daily_load(c, _at(18), 0, CELLS)
    cli._daily_load(c, _at(20), 0, CELLS)
    out = capsys.readouterr().out
    assert out.count("셀 0개") == 1 and OUT1 in out


def test_each_out_of_range_token_logs_once_separately(capsys):
    c = _Conn([f"on|18|{OUT1},{OUT2}"])
    cli._daily_load(c, _at(18), 0, CELLS)
    cli._daily_load(c, _at(18, 0, 30), 0, CELLS)       # 같은 분의 다음 틱
    out = capsys.readouterr().out
    assert out.count("셀 0개") == 2 and OUT1 in out and OUT2 in out


def test_no_log_for_stations_that_have_cells(capsys):
    c = _Conn([f"on|18|{ST1}"])
    cli._daily_load(c, _at(18), 0, CELLS)
    assert "셀 0개" not in capsys.readouterr().out


# ── ③ 옛 호출 · 기타 동작 보존 ─────────────────────────────────────────────

def test_without_cells_the_old_behaviour_registers_every_station(capsys):
    c = _Conn([f"on|18|{ST1},{OUT1},{OUT2}"])
    cli._daily_load(c, _at(18), 0)                     # cells 안 줌
    assert len(c.faults) == 3 and "셀 0개" not in capsys.readouterr().out


def test_an_empty_cells_list_skips_everything():
    c = _Conn([f"on|18|{ST1},{ST2}"])
    assert cli._daily_load(c, _at(18), 0, []) is None and c.faults == []


def test_pending_duplicates_and_off_lines_are_still_handled_after_the_skip():
    c = _Conn([f"on|18|{ST1},{ST2}"], existing=[f"{cli.DAILY_PREFIX}:{ST1}"])
    cli._daily_load(c, _at(18), 0, CELLS)
    assert _targets(c) == [f"{cli.DAILY_PREFIX}:{ST2}"]
    off = _Conn([f"off|18|{ST1}"])
    assert cli._daily_load(off, _at(18), 0, CELLS) is None and off.faults == []


def test_the_tick_passes_the_cell_list():
    import inspect
    src = inspect.getsource(cli)
    assert '_aside("매일 부하", _daily_load, conn, ts, vo_now, gen.cells)' in src
