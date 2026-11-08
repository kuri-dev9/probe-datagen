"""권장 시작 시간대 — `measured.hours` · `_arc` · `recommended_hours` · `add` 의 `start_at` 유지 · `cli._fault_start` · `cmd_measure` 경고 (2026-11-07).

실제 `config/measured_durations.json` 은 건드리지 않는다 — `measured.PATH` 를 임시 파일로 바꿔 돈다. DB 는 가짜다.
"""
from __future__ import annotations

import hashlib
import inspect
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pymysql
import pytest

from datagen import cli, measured
from datagen import scenarios as S

REAL = Path(measured.PATH)
KIND = "ran_radio"

# 0~7시 낮음 · 8~21시 높음 · 22~23시 중간 — 시별 배율을 흉내 낸 곡선
CURVE = [0.2] * 8 + [1.0] * 14 + [0.6] * 2


def _digest():
    return hashlib.sha256(REAL.read_bytes()).hexdigest() if REAL.exists() else None


@pytest.fixture(autouse=True)
def tmp_measured(tmp_path, monkeypatch):
    before = _digest()
    p = tmp_path / "measured.json"
    monkeypatch.setattr(measured, "PATH", p)
    monkeypatch.setitem(measured._cache, "mtime", None)
    monkeypatch.setitem(measured._cache, "data", {})
    yield p
    assert _digest() == before, "실제 실측 기록 파일을 건드렸다"


def _write(p, rows):
    p.write_text(json.dumps({KIND: rows}), encoding="utf-8")


def _r(fault, hour, result="correct", day=7):
    return {"fault": fault, "result": result, "to_correct_min": 10 if result == "correct" else None,
            "to_l2_min": 12 if result == "correct" else None,
            "start_at": f"2026-11-{day:02d}T{hour:02d}:30:00" if hour is not None else None}


# ---------------------------------------------------------------- _arc

def test_arc_of_a_plain_range():
    assert measured._arc(set(range(8, 22))) == (8, 22)


def test_arc_across_midnight():
    assert measured._arc({22, 23, 0, 1, 2}) == (22, 3), "자정을 넘는 구간은 22시에서 03시까지다"


def test_arc_of_a_single_hour_ends_at_the_next_hour():
    assert measured._arc({5}) == (5, 6)


def test_arc_ignores_the_gap_inside_when_a_bigger_gap_exists():
    assert measured._arc({22, 23, 0, 1, 10, 11}) == (22, 12), "가장 긴 빈 구간(12~21시) 바깥이 구간이다 — 안의 빈 구간(2~9시)은 덮는다"


def test_arc_of_the_whole_day_but_one_hour():
    first, end = measured._arc(set(range(24)) - {3})
    assert (first, end) == (4, 3)


# ---------------------------------------------------------------- hours

def test_hours_without_any_correct_record_has_n_zero(tmp_measured):
    _write(tmp_measured, [_r("F1", 10, "missed"), _r("F2", 11, "wrong")])
    assert measured.hours(KIND, CURVE) == {"n": 0}


def test_hours_without_any_record_has_n_zero():
    assert measured.hours("no_such_kind", CURVE) == {"n": 0}


def test_hours_is_every_hour_at_or_above_the_lowest_level_that_was_correct(tmp_measured):
    _write(tmp_measured, [_r("F1", 9)])                  # 낮은 수준 = 1.0
    h = measured.hours(KIND, CURVE)
    assert (h["n"], h["low_hour"], h["all"], h["from"], h["to"]) == (1, 9, False, 8, 22)


def test_a_correct_run_at_a_lower_level_widens_the_range(tmp_measured):
    _write(tmp_measured, [_r("F1", 9), _r("F2", 23)])    # 23시 = 0.6
    h = measured.hours(KIND, CURVE)
    assert h["low_hour"] == 23 and (h["from"], h["to"]) == (8, 0), "0.6 이상이면 8~23시 — 끝은 그 시의 끝(0시)"


def test_a_correct_run_at_the_night_floor_makes_every_hour_ok(tmp_measured):
    _write(tmp_measured, [_r("F1", 3)])
    assert measured.hours(KIND, CURVE) == {"n": 1, "low_hour": 3, "all": True}


def test_a_miss_or_a_wrong_result_never_changes_the_hours(tmp_measured):
    _write(tmp_measured, [_r("F1", 9)])
    base = measured.hours(KIND, CURVE)
    _write(tmp_measured, [_r("F1", 9), _r("F2", 3, "missed"), _r("F3", 4, "wrong")])
    measured._cache["mtime"] = None
    assert measured.hours(KIND, CURVE) == base


def test_a_correct_record_without_a_start_time_is_left_out(tmp_measured):
    _write(tmp_measured, [_r("F1", None), {"fault": "F2", "result": "correct", "to_correct_min": 5,
                                           "start_at": "not-a-date"}])
    assert measured.hours(KIND, CURVE)["n"] == 0


def test_hour_reads_the_hour_and_gives_none_for_broken_values():
    assert measured._hour({"start_at": "2026-11-07T11:22:22"}) == 11
    assert measured._hour({}) is None and measured._hour({"start_at": ""}) is None
    assert measured._hour({"start_at": "xx"}) is None


# ---------------------------------------------------------------- recommended_hours

def _hours_text(tmp_measured, rows, monkeypatch):
    _write(tmp_measured, rows)
    measured._cache["mtime"] = None
    from datagen import generator
    monkeypatch.setattr(generator, "DIURNAL", CURVE)
    return S.recommended_hours(KIND)


def test_the_text_without_a_measurement(tmp_measured, monkeypatch):
    t = _hours_text(tmp_measured, [], monkeypatch)
    assert t["text"] == "시작 시간대 실측 없음" and t["text_ja"] == "開始時間帯の実測なし" and t["n"] == 0


def test_the_text_for_a_busy_range(tmp_measured, monkeypatch):
    t = _hours_text(tmp_measured, [_r("F1", 9)], monkeypatch)
    assert t["text"] == "호가 많은 08~22시" and t["text_ja"] == "呼が多い08〜22時"


def test_the_text_for_no_limit(tmp_measured, monkeypatch):
    t = _hours_text(tmp_measured, [_r("F1", 3)], monkeypatch)
    assert t["text"] == "시작 시간대 제약 없음" and t["text_ja"] == "開始時間帯の制約なし"


def test_a_range_over_sixteen_hours_names_the_excluded_hours_instead(tmp_measured, monkeypatch):
    t = _hours_text(tmp_measured, [_r("F1", 23)], monkeypatch)       # 0.6 이상 = 8~24시(16시간)
    assert t["text"] == "호가 많은 08~00시", "정확히 16시간은 아직 구간으로 말한다"
    t = _hours_text(tmp_measured, [_r("F1", 23), _r("F2", 7)], monkeypatch)   # 0.2 → 전부
    assert t["text"] == "시작 시간대 제약 없음"
    wide = [0.2, 0.2, 0.2] + [1.0] * 21
    from datagen import generator
    monkeypatch.setattr(generator, "DIURNAL", wide)
    _write(tmp_measured, [_r("F1", 12)])
    measured._cache["mtime"] = None
    t = S.recommended_hours(KIND)
    assert t["text"] == "03~00시 제외".replace("03~00", "00~03") and t["text_ja"] == "00〜03時を除く"


# ---------------------------------------------------------------- add

def test_a_corrected_record_keeps_its_start_time(tmp_measured):
    measured.add(KIND, {"fault": "F9", "result": "wrong", "start_at": "2026-11-07T10:00:00"})
    measured.add(KIND, {"fault": "F9", "result": "correct", "to_correct_min": 9, "start_at": None})
    rows = measured.load()[KIND]
    assert len(rows) == 1 and rows[0]["start_at"] == "2026-11-07T10:00:00" and rows[0]["result"] == "correct"


def test_a_new_start_time_replaces_the_old_one(tmp_measured):
    measured.add(KIND, {"fault": "F9", "result": "correct", "to_correct_min": 9, "start_at": "2026-11-07T10:00:00"})
    measured.add(KIND, {"fault": "F9", "result": "correct", "to_correct_min": 9, "start_at": "2026-11-07T12:00:00"})
    assert measured.load()[KIND][0]["start_at"] == "2026-11-07T12:00:00"


def test_a_record_without_any_start_time_stays_without_one(tmp_measured):
    measured.add(KIND, {"fault": "F8", "result": "missed", "start_at": None})
    assert measured.load()[KIND][0]["start_at"] is None


def test_runs_are_sorted_by_start_time_and_the_unknown_ones_come_first(tmp_measured):
    _write(tmp_measured, [_r("F3", 12), _r("F1", None), _r("F2", 9)])
    assert [r["fault"] for r in measured.runs(KIND)] == ["F1", "F2", "F3"]


# ---------------------------------------------------------------- _fault_start

def _cfg(monkeypatch):
    monkeypatch.setattr(cli, "load_cfg", lambda _p: {"mysql": {"host": "h", "port": 3306, "user": "u", "password": "p"}})


class _Cur:
    def __init__(self, row):
        self.row = row
        self.sql = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.sql.append((sql, params))

    def fetchone(self):
        return self.row


class _Conn:
    def __init__(self, row):
        self.cur, self.closed = _Cur(row), False

    def cursor(self):
        return self.cur

    def close(self):
        self.closed = True


def test_fault_start_imports_pymysql_inside_the_function():
    assert "import pymysql" in inspect.getsource(cli._fault_start), "11-07 에 임포트가 빠져 NameError 를 삼켰다"


@pytest.mark.parametrize("fault", [None, "", "RF87B", "F167", "F-", "F-abc", "f-167"])
def test_a_fault_that_is_not_f_number_never_touches_the_db(monkeypatch, fault):
    monkeypatch.setattr(pymysql, "connect", lambda *a, **k: (_ for _ in ()).throw(AssertionError("DB 를 열었다")))
    assert cli._fault_start(fault) is None


def test_the_start_time_is_read_from_the_fault_table(monkeypatch):
    _cfg(monkeypatch)
    conn = _Conn((datetime(2026, 11, 7, 11, 22, 22),))
    monkeypatch.setattr(pymysql, "connect", lambda *a, **k: conn)
    assert cli._fault_start("F-167") == "2026-11-07T11:22:22"
    sql, params = conn.cur.sql[0]
    assert "CM_DATAGEN_FAULT" in sql and params == (167,) and conn.closed


def test_a_missing_fault_row_or_time_is_none(monkeypatch, capsys):
    _cfg(monkeypatch)
    monkeypatch.setattr(pymysql, "connect", lambda *a, **k: _Conn(None))
    assert cli._fault_start("F-999999") is None
    monkeypatch.setattr(pymysql, "connect", lambda *a, **k: _Conn((None,)))
    assert cli._fault_start("F-1") is None


@pytest.mark.parametrize("exc", [pymysql.err.OperationalError(2003, "연결 실패"), ConnectionRefusedError("거부")])
def test_a_db_failure_is_swallowed_into_none_with_a_warning(monkeypatch, capsys, exc):
    _cfg(monkeypatch)

    def boom(*a, **k):
        raise exc

    monkeypatch.setattr(pymysql, "connect", boom)
    assert cli._fault_start("F-167") is None
    assert "F-167 시작 시각을 못 읽었다" in capsys.readouterr().err


def test_a_missing_config_key_is_swallowed_into_none(monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_cfg", lambda _p: {})
    assert cli._fault_start("F-167") is None and "못 읽었다" in capsys.readouterr().err


@pytest.mark.parametrize("exc", [NameError("pymysql"), TypeError("버그"), AttributeError("버그")])
def test_a_code_defect_is_not_swallowed(monkeypatch, exc):
    _cfg(monkeypatch)

    def boom(*a, **k):
        raise exc

    monkeypatch.setattr(pymysql, "connect", boom)
    with pytest.raises(type(exc)):
        cli._fault_start("F-167")


# ---------------------------------------------------------------- cmd_measure

def _args(**kw):
    base = dict(list=False, kind=KIND, fault="F-170", result="correct", intensity=1.0, duration=20,
                first=5.0, incident=5.0, correct=7.0, settle=None, start=None, l2=None)
    return SimpleNamespace(**{**base, **kw})


def test_measure_warns_when_there_is_no_start_time_and_still_records(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_fault_start", lambda f: None)
    assert cli.cmd_measure(_args()) == 0
    err = capsys.readouterr().err
    assert "F-170 주입 시각이 없다" in err and "--start" in err
    assert measured.load()[KIND][0]["start_at"] is None


def test_measure_is_quiet_and_records_the_time_when_it_has_one(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_fault_start", lambda f: "2026-11-07T11:22:22")
    assert cli.cmd_measure(_args()) == 0
    assert capsys.readouterr().err == ""
    assert measured.load()[KIND][0]["start_at"] == "2026-11-07T11:22:22"


def test_measure_with_start_does_not_read_the_db(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_fault_start", lambda f: (_ for _ in ()).throw(AssertionError("DB 를 읽었다")))
    assert cli.cmd_measure(_args(start="2026-11-07T08:00:00")) == 0
    assert measured.load()[KIND][0]["start_at"] == "2026-11-07T08:00:00"


# ---------------------------------------------------------------- L2 kind 는 L2 가 선 회차만 근거다 (2026-11-07)

LOCAL_KIND = "ho_interruption"             # L2 가 필요 없는 국소 kind


def test_the_l2_kinds_are_the_wide_shape_ones_and_not_the_local_ones():
    assert {"gw_resource", "mme_overload", "dns_degradation", "natural_disaster"} <= S.L2_KINDS
    assert not ({"ran_congestion", "anchor_saturation", "sa_fallback", "event_crowd", LOCAL_KIND} & S.L2_KINDS)
    assert S.L2_KINDS <= set(S.KIND_SPECS)


def _rec_run(fault, correct, l2=None, hour=10):
    r = _r(fault, hour)
    r["to_correct_min"], r["to_l2_min"] = correct, l2
    return r


def test_summary_counts_only_runs_with_l2_when_asked(tmp_measured):
    _write(tmp_measured, [_rec_run("A", 3, None), _rec_run("B", 5, 9)])
    assert measured.summary(KIND)["to_correct"] == 5 and measured.summary(KIND)["n"] == 2
    got = measured.summary(KIND, need_l2=True)
    assert got["n"] == 1 and got["to_correct"] == 9, "값도 L2 까지의 시간이다"


def test_summary_with_l2_but_no_l2_run_has_no_value(tmp_measured):
    _write(tmp_measured, [_rec_run("A", 3, None)])
    got = measured.summary(KIND, need_l2=True)
    assert got["n"] == 0 and got["to_correct"] is None and got["runs"] == 1


def test_a_missed_run_never_counts_even_with_a_l2_time(tmp_measured):
    r = _rec_run("A", 3, 5)
    r["result"] = "missed"
    _write(tmp_measured, [r])
    assert measured.summary(KIND, need_l2=True)["n"] == 0


def test_hours_with_l2_leave_out_runs_that_only_reached_l1(tmp_measured):
    _write(tmp_measured, [_rec_run("A", 3, None, hour=23), _rec_run("B", 5, 9, hour=9)])
    every = measured.hours(KIND, CURVE)
    assert every["n"] == 2 and every["low_hour"] == 23, "L1 만 맞은 새벽 가까운 회차가 구간을 넓힌다"
    got = measured.hours(KIND, CURVE, need_l2=True)
    assert got["n"] == 1 and got["low_hour"] == 9 and (got["from"], got["to"]) == (8, 22)


def test_hours_with_l2_but_only_l1_runs_is_no_measurement(tmp_measured):
    _write(tmp_measured, [_rec_run("A", 3, None, hour=9)])
    assert measured.hours(KIND, CURVE, need_l2=True) == {"n": 0}


def test_the_recommended_duration_of_an_l2_kind_ignores_an_l1_only_run(tmp_measured):
    kind = "gw_resource"
    base = S.recommended_duration(kind)
    tmp_measured.write_text(json.dumps({kind: [_rec_run("A", 0, None)]}), encoding="utf-8")
    measured._cache["mtime"] = None
    assert S.recommended_duration(kind)["recommended"] == base["recommended"], "L1 만 맞은 6분 회차로 줄이지 않는다"
    assert S.recommended_duration(kind)["basis"] == "shape"


def test_the_recommended_duration_of_an_l2_kind_uses_a_run_with_l2(tmp_measured):
    kind = "gw_resource"
    runs = [_rec_run("A", 4, 20), _rec_run("B", 4, 20)]
    tmp_measured.write_text(json.dumps({kind: runs}), encoding="utf-8")
    measured._cache["mtime"] = None
    d = S.recommended_duration(kind)
    assert d["basis"] == "measured" and d["recommended"] >= 20


def test_a_local_kind_still_counts_an_l1_only_run(tmp_measured):
    tmp_measured.write_text(json.dumps({LOCAL_KIND: [_rec_run("A", 9, None)]}), encoding="utf-8")
    measured._cache["mtime"] = None
    assert S.recommended_duration(LOCAL_KIND)["basis"] == "measured"


def test_recommended_hours_of_an_l2_kind_needs_a_l2_run(tmp_measured, monkeypatch):
    from datagen import generator
    monkeypatch.setattr(generator, "DIURNAL", CURVE)
    kind = "gw_resource"
    tmp_measured.write_text(json.dumps({kind: [_rec_run("A", 4, None, hour=10)]}), encoding="utf-8")
    measured._cache["mtime"] = None
    assert S.recommended_hours(kind)["text"] == "시작 시간대 실측 없음"
    tmp_measured.write_text(json.dumps({kind: [_rec_run("A", 4, 11, hour=10)]}), encoding="utf-8")
    measured._cache["mtime"] = None
    assert S.recommended_hours(kind)["text"] == "호가 많은 08~22시"


def test_measure_records_the_l2_time_and_leaves_it_null_when_l2_did_not_stand(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_fault_start", lambda f: "2026-11-07T12:32:22")
    assert cli.cmd_measure(_args(kind="mme_overload", fault="F-169", l2=7.1)) == 0
    assert cli.cmd_measure(_args(kind="mme_overload", fault="F-170", l2=None)) == 0
    rows = {r["fault"]: r for r in measured.load()["mme_overload"]}
    assert rows["F-169"]["to_l2_min"] == 7.1 and rows["F-170"]["to_l2_min"] is None
    assert "F-169" in capsys.readouterr().out


def test_measure_list_shows_the_l2_time(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_fault_start", lambda f: "2026-11-07T12:32:22")
    cli.cmd_measure(_args(kind="mme_overload", fault="F-169", l2=7.1))
    capsys.readouterr()
    assert cli.cmd_measure(SimpleNamespace(list=True, kind="mme_overload")) == 0
    assert "L2까지 7.1분" in capsys.readouterr().out
