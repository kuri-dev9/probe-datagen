"""매일 같은 시각 부하 — 「망 조작」 묶음(이 시간들 × 이 역들 · 1시간 단위) (2026-11-21 오후 · 사용자 요청 · 맥스 구현).

* `control.parse_daily_line`·`format_daily_line`·`daily_run` — 저장 형식 `on|08,18,19|ST:<id>,ST:<id>`
* `cli._daily_load` — 켜진 묶음의 시가 가상 시각의 정시이면 역마다 하나씩, 이어진 시간은 한 부하로
* `status_api` — `POST daily_lines` 검사(400)·저장·옛 스위치 끄기, `GET` 의 묶음 목록, `/api/stations`
* `scenarios._shift_rise` — B 없는 부하(DAILY)의 회복 시작은 「길이 − 5분」, 증설(B 있음)은 `SHIFT_RISE` 그대로
옛 스위치(`daily_load`·`daily_load_at`)와 1차 판 형식(`on|08:00|30|…`)은 사라졌다.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from datagen import cli, control, faults
from datagen import scenarios as S
from datagen.ui import status_api

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "src" / "datagen" / "ui" / "static" / "main.html").read_text(encoding="utf-8")
ST1, ST2, ST3 = "ST:35.68968-139.70035", "ST:35.7141-139.7774", "ST:35.6812-139.7671"


# ---------------------------------------------------------------- 저장 형식

def test_a_line_round_trips_and_hours_are_sorted_and_deduplicated():
    d = control.parse_daily_line(f"on|18,08,18,19|{ST1},{ST2},{ST1}")
    assert d == {"on": True, "hours": [8, 18, 19], "targets": [ST1, ST2]}
    assert control.format_daily_line(d) == f"on|08,18,19|{ST1},{ST2}"
    assert control.parse_daily_line(control.format_daily_line(d)) == d
    assert control.parse_daily_line(f"off|00|{ST3}")["on"] is False


@pytest.mark.parametrize("bad", [
    None, "", "on", f"on|08|", f"on||{ST1}", f"on|24|{ST1}", f"on|8|{ST1}", f"on|ab|{ST1}", "on|08|", "on|08|XX:1",
    f"maybe|08|{ST1}", f"on|08:00|30|{ST1}",           # 1차 판 형식(시각|길이|대상) — 사라졌다
    f"on|08|{ST1}|extra"])
def test_a_malformed_line_is_none_so_that_group_is_never_fired(bad):
    assert control.parse_daily_line(bad) is None


def test_enb_and_cell_tokens_are_still_accepted_by_the_server():
    d = control.parse_daily_line("on|09|ENB:190885,CELL:190885-3")
    assert d and d["targets"] == ["ENB:190885", "CELL:190885-3"]


def test_daily_run_counts_the_consecutive_hours_across_midnight():
    hs = [18, 19, 23, 0]
    assert control.daily_run(hs, 18) == 2 and control.daily_run(hs, 19) == 1
    assert control.daily_run(hs, 23) == 2                      # 23 → 0 자정 넘김
    assert control.daily_run(hs, 5) == 0
    assert control.daily_run(list(range(24)), 3) == 24
    assert control.DAILY_HOUR_MIN == 60


# ---------------------------------------------------------------- _daily_load

class _Conn:
    """`config_prefix`·중복 검사·`add_fault` 가 쓰는 질의만 받는 가짜 — 건 장애를 `faults` 에 쌓는다"""

    def __init__(self, lines=None, existing=()):
        self.cfg = {f"daily_load.{i}": v for i, v in enumerate(lines or [], 1)}
        self.existing = list(existing)              # 이미 대기·진행 중인 대상
        self.faults = []
        self._rows = []
        self._next = 900

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        s = " ".join(str(sql).split())
        if s.startswith("SELECT CONFIG_KEY, CONFIG_VALUE"):
            self._rows = [(k, v) for k, v in self.cfg.items() if k.startswith("daily_load.")]
        elif "STATUS IN ('pending','active')" in s:
            # 대기·진행 중인 매일 부하 대상 전체를 한 번 읽는다(역 ID 가 겹치는지는 호출부가 `faults.daily_parts` 로 본다 · 2026-11-25)
            assert "TARGET LIKE 'DAILY%'" in s and params in (None, []), s
            self._rows = [(t,) for t in self.existing] + [(f["target"],) for f in self.faults]
        elif s.startswith("INSERT INTO"):
            kind, target, start, dur, inten, mode, note, no = params[:8]
            self.faults.append(dict(kind=kind, target=target, mode=mode, note=note, scenario_no=no, dur=dur))
            self._next += 1
            self.lastrowid = self._next
        return None

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows

    def commit(self):
        pass


@pytest.fixture(autouse=True)
def _stations(monkeypatch):
    """역 표 읽기는 DB 가 필요하다 — 채워진 것으로 둔다(`_daily_load` 가 비었을 때만 읽는다)"""
    monkeypatch.setattr(faults, "STATION_CELLS", {"35.68968-139.70035": {"1-0"}}, raising=False)
    monkeypatch.setattr(faults, "STATION_NAMES", {}, raising=False)        # 이름을 모르면 ID 마다 한 칸(묶지 않는다) — 다른 시험이 채운 표가 새지 않게
    monkeypatch.setattr(faults, "load_stations", lambda conn: None, raising=False)


def _at(h, m=0, s=3):
    return datetime(2026, 11, 21, h, m, s)


def test_no_lines_means_nothing_fires_and_the_old_switch_is_not_read():
    c = _Conn([])
    c.cfg.update({"daily_load": "true", "daily_load_at": "08:00"})          # 옛 스위치가 켜져 있어도
    assert cli._daily_load(c, _at(8), 0) is None and c.faults == []


def test_a_due_line_fires_one_fault_per_station_name_with_the_hour_length():
    c = _Conn([f"on|08|{ST1},{ST2},{ST3}"])
    fid = cli._daily_load(c, _at(8), 0)
    assert fid is not None and len(c.faults) == 3
    assert {f["target"] for f in c.faults} == {f"{cli.DAILY_PREFIX}:{t}" for t in (ST1, ST2, ST3)}
    for f in c.faults:
        assert f["kind"] == "capacity_shift" and f["dur"] == 60 and f["mode"] == "daily"
        assert f["note"].startswith("매일 같은 시각 부하 · ") and "묶음 1" in f["note"]


def test_it_fires_only_on_the_hour_and_only_for_chosen_hours_and_only_when_on():
    c = _Conn([f"on|08,18|{ST1}"])
    for t in (_at(8, 1), _at(9), _at(17, 59), _at(7, 59, 59)):
        assert cli._daily_load(c, t, 0) is None
    assert c.faults == []
    assert cli._daily_load(c, _at(18), 0) is not None and len(c.faults) == 1
    off = _Conn([f"off|08|{ST1}"])
    assert cli._daily_load(off, _at(8), 0) is None and off.faults == []


def test_the_virtual_clock_offset_decides_the_hour():
    c = _Conn([f"on|13|{ST1}"])
    assert cli._daily_load(c, _at(8, 0, 5), 5 * 3600 - 5) is not None          # 08:00:05 + 4:59:55 = 13:00:00
    assert len(c.faults) == 1


def test_consecutive_hours_become_one_longer_load_and_the_next_hour_is_skipped():
    c = _Conn([f"on|08,09,18|{ST1}"])
    assert cli._daily_load(c, _at(8), 0) is not None
    assert [f["dur"] for f in c.faults] == [120]                              # 08~10시 한 부하
    assert cli._daily_load(c, _at(9), 0) is None and len(c.faults) == 1       # 앞 부하가 진행 중이라 건너뜀
    c.faults.clear()                                                          # 08~10시 부하는 그 사이 끝났다
    assert cli._daily_load(c, _at(18), 0) is not None and c.faults[-1]["dur"] == 60


def test_a_missed_first_hour_fires_only_the_remaining_hours():
    """재기동으로 08:00 을 놓쳤으면 09:00 에 남은 1시간만 건다."""
    c = _Conn([f"on|08,09|{ST1}"])
    assert cli._daily_load(c, _at(9), 0) is not None and c.faults[0]["dur"] == 60


def test_two_groups_choosing_the_same_station_and_hour_fire_once():
    c = _Conn([f"on|08|{ST1}", f"on|08|{ST1},{ST2}"])
    cli._daily_load(c, _at(8), 0)
    assert sorted(f["target"] for f in c.faults) == sorted(f"{cli.DAILY_PREFIX}:{t}" for t in (ST1, ST2))


def test_a_station_that_is_already_pending_or_active_is_skipped():
    c = _Conn([f"on|08|{ST1},{ST2}"], existing=[f"{cli.DAILY_PREFIX}:{ST1}"])
    cli._daily_load(c, _at(8), 0)
    assert [f["target"] for f in c.faults] == [f"{cli.DAILY_PREFIX}:{ST2}"]


def test_twice_in_the_same_minute_fires_once():
    c = _Conn([f"on|08|{ST1}"])
    assert cli._daily_load(c, _at(8), 0) is not None
    assert cli._daily_load(c, _at(8, 0, 25), 0) is None and len(c.faults) == 1


# ---------------------------------------------------------------- POST daily_lines

@pytest.fixture
def saved(monkeypatch):
    box = {"cfg": {}}

    class _C:
        def close(self):
            pass

        def cursor(self):
            return self

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def commit(self):
            pass

    monkeypatch.setattr(status_api, "_connect", lambda: _C())
    monkeypatch.setattr(status_api, "set_config", lambda conn, k, v: box["cfg"].__setitem__(k, v))
    monkeypatch.setattr(control, "config_prefix", lambda conn, p: {k: v for k, v in box["cfg"].items() if k.startswith(p)})
    return box


def _post(rows):
    status_api.api_set_settings(status_api.SettingsReq(daily_lines=rows))


@pytest.mark.parametrize("row", [
    {"on": True, "hours": [], "targets": [ST1]},                 # 시간 없음
    {"on": True, "hours": [8], "targets": []},                   # 역 없음
    {"on": True, "hours": [25], "targets": [ST1]},               # 25시
    {"on": True, "hours": ["x"], "targets": [ST1]},
    {"on": True, "hours": [8], "targets": ["BAD"]},
    {"on": True, "hours": 8, "targets": [ST1]},                  # 목록이 아님
])
def test_a_bad_group_is_400_and_saves_nothing(saved, row):
    with pytest.raises(HTTPException) as e:
        _post([row])
    assert e.value.status_code == 400
    assert not any(k.startswith("daily_load.") for k in saved["cfg"])


def test_too_many_stations_is_400_with_a_split_message(saved):
    many = [f"ST:35.{i:05d}-139.{i:05d}" for i in range(14)]               # 한 칸 255자를 넘긴다
    with pytest.raises(HTTPException) as e:
        _post([{"on": True, "hours": [8], "targets": many}])
    assert e.value.status_code == 400 and "묶음을 나눠" in e.value.detail


def test_good_groups_are_saved_one_key_each_and_leftovers_are_blanked_and_the_old_switch_goes_off(saved):
    saved["cfg"].update({"daily_load": "true", "daily_load.1": f"on|07|{ST1}", "daily_load.2": f"on|09|{ST2}",
                         "daily_load.3": f"on|10|{ST3}"})
    _post([{"on": True, "hours": [8, 18, 19], "targets": [ST1, ST2]}])
    assert saved["cfg"]["daily_load.1"] == f"on|08,18,19|{ST1},{ST2}"
    assert saved["cfg"]["daily_load.2"] == "" and saved["cfg"]["daily_load.3"] == ""        # 행을 지우지 않고 비운다
    assert saved["cfg"]["daily_load"] == "false"


def test_an_empty_list_clears_every_group(saved):
    saved["cfg"].update({"daily_load.1": f"on|07|{ST1}"})
    _post([])
    assert saved["cfg"]["daily_load.1"] == "" and saved["cfg"]["daily_load"] == "false"


def test_the_old_settings_fields_are_gone():
    fields = status_api.SettingsReq.model_fields
    assert "daily_lines" in fields and "daily_load" not in fields and "daily_load_at" not in fields


def test_the_view_lists_the_groups_and_the_one_hour_unit(monkeypatch):
    monkeypatch.setattr(control, "daily_lines", lambda conn: [(1, {"on": True, "hours": [8], "targets": [ST1]})])
    v = status_api._daily_view(object())
    assert v == {"daily_lines": [{"n": 1, "on": True, "hours": [8], "targets": [ST1]}], "daily_hour_min": 60}
    assert "daily_load_at" not in v and "daily_len_default" not in v


# ---------------------------------------------------------------- 부하 모양

def _sc(duration, minute, shift_to=None, start=100):
    return SimpleNamespace(target=SimpleNamespace(shift_to=shift_to), duration=duration, peak=1.0,
                           start_minute=start, _minute=start + minute)


def test_a_daily_load_without_b_recovers_five_minutes_before_the_end():
    assert S._shift_rise(_sc(60, 0)) == 55 and S._shift_rise(_sc(120, 0)) == 115
    assert S._shift_rise(_sc(8, 0)) == S.SHIFT_RAMP                     # 아주 짧으면 몰림 시간은 지킨다
    assert S.shift_congestion(_sc(60, 54)) == 1.0                        # 55분 전까지 정점
    assert S.shift_congestion(_sc(60, 55)) < 1.0 and S.shift_congestion(_sc(60, 59)) == 0.0
    assert S.shift_congestion(_sc(60, 0)) == pytest.approx(1 / S.SHIFT_RAMP)    # 오름 5분


def test_the_24_expansion_with_b_keeps_the_fixed_rise():
    sc = _sc(S.SHIFT_TOTAL, 0, shift_to="700002")
    assert S._shift_rise(sc) == S.SHIFT_RISE == 20
    assert S.shift_split(_sc(60, 30)) == 0.0                              # B 없는 부하는 호를 넘기지 않는다


# ---------------------------------------------------------------- 화면

def test_the_screen_places_daily_load_under_the_reassign_card_and_not_in_the_settings():
    assert 'id="daily-on"' not in HTML and 'id="daily-at"' not in HTML and 'id="dl-at"' not in HTML
    i_net = HTML.index("장비 재배정")
    assert "매일 같은 시각 부하" in HTML[i_net:], "망 조작(장비 재배정) 뒤에 있어야 한다"
    assert HTML.index("function renderDaily") > 0 and "daily_lines" in HTML and "/api/stations" in HTML
    for fn in ("toggleDailyHour", "addDailyStation", "dropDailyStation", "addDaily", "saveDaily"):
        assert f"function {fn}" in HTML, fn


def test_the_screen_has_no_length_or_cell_input_any_more():
    for gone in ('id="dl-len"', 'id="dl-mode"', "daily-len"):
        assert gone not in HTML, gone
