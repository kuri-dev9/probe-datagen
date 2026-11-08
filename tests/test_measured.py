"""권장 지속시간의 실측 기록 (2026-11-05 · `datagen.measured` · `scenarios.recommended_duration` · `datagen measure`).

모양으로 계산한 권장값은 탐지가 몇 분을 요구하는지 모른다 — 주입마다 잰 값을 시나리오(kind)별로 쌓고 정답 기록이
있으면 그것을 권장으로 쓴다. 늘리는 것은 1회 정답으로 바로, **줄이는 것은 `MEASURED_SHRINK_RUNS`(2)회부터**.

**실제 `config/measured_durations.json` 은 건드리지 않는다** — `measured.PATH` 를 임시 파일로 바꿔 돌린다.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from datagen import cli, measured
from datagen import scenarios as S

REAL = Path(measured.PATH)
RAMP = "ho_interruption"                         # 모양 기준 권장 11 · 최소 6 · 하강 3 (`detect_min` 없는 RAMP — 14번은 detect_min 15 로 20분이 됐다)


def _digest():
    return hashlib.sha256(REAL.read_bytes()).hexdigest() if REAL.exists() else None


@pytest.fixture(autouse=True)
def tmp_path_measured(tmp_path, monkeypatch):
    before = _digest()
    p = tmp_path / "measured.json"
    monkeypatch.setattr(measured, "PATH", p)
    monkeypatch.setitem(measured._cache, "mtime", None)
    monkeypatch.setitem(measured._cache, "data", {})
    yield p
    assert _digest() == before, "실제 실측 기록 파일을 건드렸다"


def _write(p, data):
    p.write_text(json.dumps(data), encoding="utf-8")


def _run(result="correct", correct=20, settle=None, fault="F1", **kw):
    return {"fault": fault, "result": result, "to_correct_min": correct if result == "correct" else kw.pop("c", None),
            "end_to_final_min": settle, "duration_min": None, "intensity": None, "to_first_event_min": None,
            "to_incident_min": None, **kw}


def _rec(kind=RAMP):
    return S.recommended_duration(kind)


def test_the_shape_baseline_is_what_these_tests_assume():
    d = _rec()
    assert (d["recommended"], d["min"], d["settle"], d["basis"]) == (11, 6, S.SETTLE_MIN, "shape")
    assert S.MEASURED_SHRINK_RUNS == 2 and S.recommended_duration("no_such_kind") is None


# ---------------------------------------------------------------- 네 경우

def test_no_file_means_shape(tmp_path_measured):
    assert not tmp_path_measured.exists()
    d = _rec()
    assert d["basis"] == "shape" and d["recommended"] == 11 and "실측" not in d["why"]


def test_broken_file_falls_back_to_shape_with_a_warning(tmp_path_measured, capsys):
    tmp_path_measured.write_text("{ not json", encoding="utf-8")
    d = _rec()
    assert d["basis"] == "shape" and d["recommended"] == 11
    assert "실측 기록을 못 읽었다" in capsys.readouterr().out


#: 모양이 틀린 JSON(리스트·숫자·kind 값이 list[dict] 가 아님)도 `recommended_duration` 이 죽지 않고 모양 기준으로 돌아간다
#: (맥스 수정 `measured._well_formed` · 11-05). 전에는 `load`/`summary` 가 JSON 오류만 막아 예외가 났다
_PAYLOADS = ["[]", "null", "42", '"text"', '{"%s": 5}' % RAMP, '{"%s": "x"}' % RAMP, '{"%s": [1, 2]}' % RAMP,
             '{"%s": [{"result": "correct"}]}' % RAMP]


@pytest.mark.parametrize("payload", _PAYLOADS)
def test_valid_json_of_the_wrong_shape_does_not_crash(tmp_path_measured, payload):
    """파일이 깨졌을 때(JSON 오류)만이 아니라 **JSON 은 맞는데 모양이 틀려도** 권장이 모양 기준으로 돌아가야 한다."""
    tmp_path_measured.write_text(payload, encoding="utf-8")
    assert _rec()["basis"] == "shape"


def test_only_wrong_and_missed_records_do_not_count(tmp_path_measured):
    _write(tmp_path_measured, {RAMP: [_run("wrong", fault="A", c=30), _run("missed", fault="B", c=40)]})
    d = _rec()
    assert d["basis"] == "shape" and d["recommended"] == 11 and "실측 2회 정답 없음" in d["why"]


def test_correct_record_is_used(tmp_path_measured):
    _write(tmp_path_measured, {RAMP: [_run(correct=20)]})
    d = _rec()
    assert (d["recommended"], d["basis"]) == (23, "measured"), "20분 + 하강 3"
    assert d["why"] == "실측 1회 · 정답까지 20분 + 하강 3" and d["min"] == 6


def test_wrong_and_missed_never_enter_the_measured_value(tmp_path_measured):
    _write(tmp_path_measured, {RAMP: [_run(correct=20, fault="A"), _run("wrong", fault="B", c=90),
                                       _run("missed", fault="C", c=99)]})
    d = _rec()
    assert d["recommended"] == 23 and "실측 1회" in d["why"], "정답 한 건만 센다"
    assert measured.summary(RAMP) == {"runs": 3, "n": 1, "to_correct": 20, "settle": None}


def test_correct_without_to_correct_is_not_usable(tmp_path_measured):
    _write(tmp_path_measured, {RAMP: [{"fault": "A", "result": "correct", "to_correct_min": None}]})
    assert _rec()["basis"] == "shape" and measured.summary(RAMP)["n"] == 0


# ---------------------------------------------------------------- 1회 조건 · MEASURED_SHRINK_RUNS

def test_one_correct_run_that_is_shorter_keeps_the_shape_value(tmp_path_measured):
    _write(tmp_path_measured, {RAMP: [_run(correct=3)]})                  # 3 + 3 = 6 < 11
    d = _rec()
    assert (d["recommended"], d["basis"]) == (11, "shape")
    assert d["why"].startswith("실측 1회 · 정답까지 3분(2회부터 반영)")


def test_one_correct_run_that_is_longer_is_used_immediately(tmp_path_measured):
    _write(tmp_path_measured, {RAMP: [_run(correct=9)]})                  # 9 + 3 = 12 > 11
    d = _rec()
    assert (d["recommended"], d["basis"]) == (12, "measured")


def test_two_correct_runs_can_shrink(tmp_path_measured):
    _write(tmp_path_measured, {RAMP: [_run(correct=5, fault="A"), _run(correct=3, fault="B")]})
    d = _rec()
    assert (d["recommended"], d["basis"]) == (8, "measured") and d["why"] == "실측 2회 · 정답까지 5분 + 하강 3"


def test_equal_to_the_shape_value_counts_as_measured(tmp_path_measured):
    _write(tmp_path_measured, {RAMP: [_run(correct=8)]})                  # 8 + 3 = 11
    d = _rec()
    assert (d["recommended"], d["basis"]) == (11, "measured")


def test_threshold_follows_the_named_constant(tmp_path_measured, monkeypatch):
    _write(tmp_path_measured, {RAMP: [_run(correct=3, fault="A"), _run(correct=3, fault="B")]})
    monkeypatch.setattr(S, "MEASURED_SHRINK_RUNS", 3)
    assert _rec()["basis"] == "shape", "3회부터면 2회는 줄이지 않는다"
    monkeypatch.setattr(S, "MEASURED_SHRINK_RUNS", 1)
    _write(tmp_path_measured, {RAMP: [_run(correct=3)]})
    os.utime(tmp_path_measured, ns=(1, 2))
    d = _rec()
    assert (d["recommended"], d["basis"]) == (6, "measured"), "1회부터면 1회로도 줄어든다(최소 6 아래로는 안 간다)"


def test_never_below_the_scenario_minimum(tmp_path_measured):
    _write(tmp_path_measured, {RAMP: [_run(correct=0, fault="A"), _run(correct=1, fault="B")]})
    d = _rec()
    assert d["basis"] == "measured" and d["recommended"] == d["min"] == 6


def test_fractional_minutes_round_up(tmp_path_measured):
    _write(tmp_path_measured, {RAMP: [_run(correct=21.2)]})
    assert measured.summary(RAMP)["to_correct"] == 22 and _rec()["recommended"] == 25


# ---------------------------------------------------------------- 최소 길이 보호

@pytest.mark.parametrize("kind,floor", [("capacity_shift", 40), ("natural_disaster", 30), ("earthquake", 20),
                                        ("event_crowd", 25), ("enb_s1_outage", 20), ("dns_flap", 12)])
def test_phase_step_and_flap_scenarios_are_never_cut_short(tmp_path_measured, kind, floor):
    _write(tmp_path_measured, {kind: [_run(correct=5, fault="A", to_l2_min=5), _run(correct=4, fault="B", to_l2_min=4)]})
    d = _rec(kind)
    assert d["basis"] == "measured" and d["recommended"] >= floor, d
    assert d["recommended"] == max(d["min"], floor if kind != "dns_flap" else 12)


def test_a_long_measurement_extends_even_a_phase_scenario(tmp_path_measured):
    _write(tmp_path_measured, {"capacity_shift": [_run(correct=40)]})
    assert _rec("capacity_shift")["recommended"] == 40


def test_instant_offset_has_zero_ramp_out(tmp_path_measured):
    """즉시 종료형(하강 0)은 `정답까지 + 0` 이다 — 하강을 더하지 않는다."""
    _write(tmp_path_measured, {"auth_failure": [_run(correct=9, to_l2_min=9)]})   # L2 kind 는 L2 가 선 회차만 근거다
    d = _rec("auth_failure")
    assert d["recommended"] == 9 and d["why"].endswith("하강 0")


# ---------------------------------------------------------------- settle

def test_settle_default_and_measured(tmp_path_measured):
    assert _rec()["settle"] == S.SETTLE_MIN
    _write(tmp_path_measured, {RAMP: [_run(correct=20, settle=None)]})
    assert _rec()["settle"] == S.SETTLE_MIN, "끝→판정이 null 이면 기본값"
    _write(tmp_path_measured, {RAMP: [_run(correct=20, settle=5.2, fault="A"), _run(correct=20, settle=9.1, fault="B")]})
    os.utime(tmp_path_measured, ns=(3, 4))
    assert _rec()["settle"] == 10, "정답 기록의 가장 긴 값(올림)"


def test_settle_ignores_wrong_and_missed_runs(tmp_path_measured):
    _write(tmp_path_measured, {RAMP: [_run(correct=20, settle=6, fault="A"),
                                       _run("wrong", settle=99, fault="B"), _run("missed", settle=88, fault="C")]})
    assert _rec()["settle"] == 6


def test_settle_is_used_even_when_the_recommended_value_stays_on_the_shape(tmp_path_measured):
    _write(tmp_path_measured, {RAMP: [_run(correct=3, settle=6)]})
    d = _rec()
    assert d["basis"] == "shape" and d["recommended"] == 11 and d["settle"] == 6


# ---------------------------------------------------------------- 재시작 없이 반영 · 덮어쓰기

def test_file_change_is_picked_up_without_a_restart(tmp_path_measured):
    _write(tmp_path_measured, {RAMP: [_run(correct=20)]})
    assert _rec()["recommended"] == 23
    _write(tmp_path_measured, {RAMP: [_run(correct=30)]})
    os.utime(tmp_path_measured, ns=(10, 20))                    # 수정 시각이 달라진 것으로 만든다
    assert _rec()["recommended"] == 33
    tmp_path_measured.unlink()
    assert _rec()["basis"] == "shape", "파일이 사라지면 모양 기준"


def test_unchanged_file_is_not_parsed_twice(tmp_path_measured, monkeypatch):
    _write(tmp_path_measured, {RAMP: [_run(correct=20)]})
    measured.load()
    monkeypatch.setattr(json, "loads", lambda *a, **k: pytest.fail("수정 시각이 같은데 다시 읽었다"))
    measured.load()


def test_same_fault_overwrites_instead_of_counting_twice(tmp_path_measured):
    rec = {"fault": "F9", "result": "correct", "to_correct_min": 10, "end_to_final_min": 5}
    measured.add(RAMP, rec)
    measured.add(RAMP, {**rec, "to_correct_min": 12})
    rows = json.loads(tmp_path_measured.read_text(encoding="utf-8"))[RAMP]
    assert len(rows) == 1 and rows[0]["to_correct_min"] == 12 and rows[0]["recorded_at"]
    measured.add(RAMP, {**rec, "fault": "F10"})
    assert len(json.loads(tmp_path_measured.read_text(encoding="utf-8"))[RAMP]) == 2


def test_add_creates_the_file_and_leaves_no_temp_file(tmp_path_measured):
    measured.add("dns_flap", {"fault": "A", "result": "missed"})
    assert tmp_path_measured.exists() and not list(tmp_path_measured.parent.glob("*.tmp"))
    assert measured.RESULTS == ("correct", "wrong", "missed")


def test_other_kinds_are_not_touched_by_a_new_record(tmp_path_measured):
    measured.add(RAMP, {"fault": "A", "result": "correct", "to_correct_min": 9})
    before = _rec("dns_degradation")
    measured.add("nsa_nr_leg", {"fault": "B", "result": "correct", "to_correct_min": 30})
    assert _rec("dns_degradation") == before and _rec(RAMP)["recommended"] == 12


# ---------------------------------------------------------------- 옮겨 온 22번 기록

def test_the_real_file_holds_only_the_migrated_pay_dns_delay_record_and_keeps_11_minutes():
    """실제 파일을 읽기만 한다 — 22번 1회 기록(정답까지 3분)은 줄이는 근거가 못 되어 권장 11 이 그대로다."""
    data = json.loads(REAL.read_text(encoding="utf-8"))
    runs = data["pay_dns_delay"]
    assert len(runs) == 1 and runs[0]["result"] == "correct" and runs[0]["to_correct_min"] == 3
    measured.PATH = REAL                                     # (fixture 가 되돌린다)
    measured._cache.update(mtime=None, data={})
    d = S.recommended_duration("pay_dns_delay")
    # pay_dns_delay 는 L2 kind 다 — L2 까지 재지 않은 이 기록은 근거가 못 되어 끝→판정 값도 기본값(SETTLE_MIN)이다
    assert (d["recommended"], d["basis"], d["settle"]) == (11, "shape", S.SETTLE_MIN)


# ---------------------------------------------------------------- CLI

def _cli(*argv):
    return cli.main(["measure", *argv])


def test_cli_rejects_unknown_kind_and_missing_required(capsys):
    assert _cli("--kind", "nope", "--fault", "F1", "--result", "missed") == 2
    assert "모르는 장애 종류" in capsys.readouterr().err
    assert _cli("--kind", RAMP, "--result", "missed") == 2 and "--fault" in capsys.readouterr().err
    assert _cli("--kind", RAMP, "--fault", "F1") == 2 and "--result" in capsys.readouterr().err
    assert not measured.PATH.exists(), "거부한 입력은 파일에 쓰지 않는다"


def test_cli_rejects_a_correct_record_without_to_correct(capsys):
    assert _cli("--kind", RAMP, "--fault", "F1", "--result", "correct") == 2
    assert "--correct" in capsys.readouterr().err and not measured.PATH.exists()


def test_cli_rejects_an_invalid_result():
    with pytest.raises(SystemExit) as e:
        _cli("--kind", RAMP, "--fault", "F1", "--result", "maybe")
    assert e.value.code == 2


def test_cli_records_and_unmeasured_values_stay_null(capsys):
    assert _cli("--kind", RAMP, "--fault", "F136", "--result", "correct", "--correct", "20") == 0
    out = capsys.readouterr().out
    assert "권장 23분 (measured)" in out
    row = json.loads(measured.PATH.read_text(encoding="utf-8"))[RAMP][0]
    assert row["fault"] == "F136" and row["to_correct_min"] == 20.0
    for k in ("intensity", "duration_min", "to_first_event_min", "to_incident_min", "end_to_final_min"):
        assert row[k] is None, f"재지 못한 값 {k} 는 0 이 아니라 null 이어야 한다"


def test_cli_records_every_value(capsys):
    assert _cli("--kind", RAMP, "--fault", "F2", "--result", "correct", "--intensity", "1.0", "--duration", "30",
                "--first", "15", "--incident", "18", "--correct", "20", "--settle", "7") == 0
    row = json.loads(measured.PATH.read_text(encoding="utf-8"))[RAMP][0]
    assert (row["intensity"], row["duration_min"], row["to_first_event_min"], row["to_incident_min"],
            row["to_correct_min"], row["end_to_final_min"]) == (1.0, 30.0, 15.0, 18.0, 20.0, 7.0)
    capsys.readouterr()


def test_cli_missed_needs_no_to_correct_and_does_not_move_the_recommendation(capsys):
    assert _cli("--kind", RAMP, "--fault", "F3", "--result", "missed") == 0
    assert "(shape)" in capsys.readouterr().out


def test_cli_same_fault_twice_overwrites(capsys):
    _cli("--kind", RAMP, "--fault", "F4", "--result", "correct", "--correct", "10")
    _cli("--kind", RAMP, "--fault", "F4", "--result", "correct", "--correct", "14")
    capsys.readouterr()
    rows = json.loads(measured.PATH.read_text(encoding="utf-8"))[RAMP]
    assert len(rows) == 1 and rows[0]["to_correct_min"] == 14.0


def test_cli_list_shows_records_and_current_recommendation(capsys):
    _cli("--kind", RAMP, "--fault", "F5", "--result", "correct", "--correct", "20", "--intensity", "1.0")
    _cli("--kind", "dns_flap", "--fault", "F6", "--result", "missed")
    capsys.readouterr()
    assert _cli("--list") == 0
    out = capsys.readouterr().out
    assert f"{RAMP}  권장 23분 (measured)" in out and "F5 correct 정답까지 20.0분" in out and "dns_flap" in out
    assert _cli("--list", "--kind", RAMP) == 0
    only = capsys.readouterr().out
    assert RAMP in only and "dns_flap" not in only


def test_cli_list_on_an_empty_store_prints_nothing_and_succeeds(capsys):
    assert _cli("--list") == 0 and capsys.readouterr().out == ""


def test_non_numeric_minutes_are_ignored_not_fatal(tmp_path_measured):
    """`to_correct_min`·`end_to_final_min` 이 문자열이어도 죽지 않는다 — 그 기록은 정답으로 쓰지 않는다."""
    _write(tmp_path_measured, {RAMP: [_run(correct="x", fault="A"), _run(correct=20, settle="y", fault="B")]})
    d = _rec()
    assert d["basis"] == "measured" and d["recommended"] == 23, "숫자인 기록만 쓴다"
    assert d["settle"] == S.SETTLE_MIN, "끝→판정이 숫자가 아니면 기본값"
    _write(tmp_path_measured, {RAMP: [_run(correct="x", fault="A")]})
    os.utime(tmp_path_measured, ns=(5, 6))
    assert _rec()["basis"] == "shape"


def test_wrong_shape_prints_a_warning_and_other_kinds_survive(tmp_path_measured, capsys):
    _write(tmp_path_measured, {RAMP: "x", "dns_flap": [_run(correct=20, fault="A")]})
    assert _rec(RAMP)["basis"] == "shape"
    assert _rec("dns_flap")["basis"] == "measured", "모양이 틀린 kind 하나가 다른 kind 의 기록을 지우지 않는다"
    assert capsys.readouterr().out.strip(), "경고가 남아야 한다"
