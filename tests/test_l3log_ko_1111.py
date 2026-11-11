"""L3 문답 기록의 한국어 해석·생성량별 현황 파일 (`l3log` · `cli l3` · `scenario_ja.SKIP_KEYS` · 2026-11-11 · 맥스 구현 고정).

* 각 turn 의 선택 칸 `q_ko`·`answer_ko` — 있으면 문자열. `summary` 는 `q_ko`·`a_head_ko`(`HEAD_CHARS` 로 자름)를 있을 때만 싣는다.
* `annotate` — 원문 줄은 그대로 두고 **해석만 담은 줄을 이어 붙인다**. `load` 가 원래 기록에 합친다. 원문·판정 칸은 못 바꾼다.
* `volume`(15·50) 으로 `latest.l3` 를 쓸 현황 파일을 고른다 — 50 은 `scenario_report_v50.json`, 그 밖은 `scenario_report.json`.
* `scenario_ja.SKIP_KEYS` — 해석 칸은 번역 대상이 아니다(번역 없음으로 세면 `ja_left` 가 0 이 못 된다).
임시 디렉터리만 쓴다 — 운영 `data/l3`·현황 파일은 건드리지 않는다.
"""
from __future__ import annotations

import copy
import json
from datetime import date
from pathlib import Path

import pytest

from datagen import cli, l3log
from datagen import scenario_ja as J

TODAY = date(2026, 11, 11)
AT = "2026-11-11T10:37:00"


def _turn(n=1, kind="first", **kw):
    t = {"n": n, "kind": kind, "route": "rca", "q": f"질문{n}", "answer": f"答え{n} " + "あ" * 200,
         "verdict": "ok", "note": "메모"}
    t.update(kw)
    return t


def _rec(**kw):
    r = {"no": 1, "fault": "F-277", "incident_id": "IX1", "event_id": "IX1", "at": AT, "status": "pass",
         "checks": {"numbers": "ok", "cause": "ok", "refs": "ok", "guide": "ok", "ja": "ok", "filtered": 0},
         "by": "에스터", "turns": [_turn(1), _turn(2, "context", route="general")]}
    r.update(kw)
    return r


def _note(fault="F-277", turns=None, at=None, **kw):
    a = {"fault": fault, **({"at": at} if at else {})}
    return {"annotate": a, "turns": turns if turns is not None else [{"n": 1, "q_ko": "질문 해석", "answer_ko": "답 해석"}], **kw}


def _reports(tmp_path, fault="F-277", no=1):
    paths = {}
    for vol, name in ((15, "scenario_report.json"), (50, "scenario_report_v50.json")):
        p = tmp_path / name
        p.write_text(json.dumps({"scenarios": [{"no": no, "latest": {"fault": fault, "summary": f"s{vol}"}}]},
                                ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        paths[vol] = p
    return paths


@pytest.fixture
def box(tmp_path, monkeypatch):
    reps = _reports(tmp_path)
    monkeypatch.setitem(l3log.REPORTS, 15, reps[15])
    monkeypatch.setitem(l3log.REPORTS, 50, reps[50])
    return tmp_path / "l3", reps


def _latest(rep):
    return json.loads(rep.read_text(encoding="utf-8"))["scenarios"][0]["latest"]


def _lines(root, day="2026-11-11"):
    return (root / f"{day}.jsonl").read_text(encoding="utf-8").splitlines()


# ── 1 · validate ─────────────────────────────────────────────────────────────

def test_korean_fields_are_optional_strings_on_a_turn():
    r = _rec()
    r["turns"][0].update(q_ko="해석", answer_ko="답 해석")
    assert l3log.validate(r) == [] and l3log.validate(_rec()) == []


@pytest.mark.parametrize("bad", [3, ["x"], {"a": 1}, True])
@pytest.mark.parametrize("key", ["q_ko", "answer_ko"])
def test_non_string_korean_fields_are_rejected(key, bad):
    r = _rec()
    r["turns"][0][key] = bad
    assert l3log.validate(r)


@pytest.mark.parametrize("vol", [None, 15, 50])
def test_volume_15_50_or_absent_is_valid(vol):
    r = _rec()
    if vol is not None:
        r["volume"] = vol
    assert l3log.validate(r) == []


@pytest.mark.parametrize("vol", [99, 0, "50", True, 50.5])
def test_other_volume_values_are_rejected(vol):
    assert l3log.validate(_rec(volume=vol))


# ── 2 · summary ──────────────────────────────────────────────────────────────

def test_summary_carries_q_ko_and_a_head_ko_cut_at_head_chars():
    long = "가" * 300
    r = _rec()
    r["turns"][0].update(q_ko="질문 해석", answer_ko="  " + long)
    t = l3log.summary(r)["turns"][0]
    assert t["q_ko"] == "질문 해석" and t["a_head_ko"] == ("  " + long)[:l3log.HEAD_CHARS].strip()
    assert "answer_ko" not in t and "answer" not in t


def test_summary_leaves_the_korean_keys_out_when_absent_or_empty():
    r = _rec()
    r["turns"][1].update(q_ko="", answer_ko=None)
    for t in l3log.summary(r)["turns"]:
        assert "q_ko" not in t and "a_head_ko" not in t


# ── 3 · 현황 파일 고르기 ──────────────────────────────────────────────────────

def test_report_of_picks_by_volume_and_an_explicit_report_wins(tmp_path):
    assert l3log.REPORTS[50].name == "scenario_report_v50.json" and l3log.REPORTS[15].name == "scenario_report.json"
    assert l3log.report_of({"volume": 50}) == l3log.REPORTS[50]
    assert l3log.report_of({"volume": 15}) == l3log.REPORTS[15] == l3log.report_of({}) == l3log.report_of({"volume": None})
    mine = tmp_path / "x.json"
    assert l3log.report_of({"volume": 50}, mine) == mine


def test_add_with_volume_50_updates_only_the_v50_report(box):
    root, reps = box
    before15 = reps[15].read_text(encoding="utf-8")
    got = l3log.add(_rec(volume=50), root, today=TODAY)
    assert got["latest_updated"] and Path(got["report"]) == reps[50]
    assert "l3" in _latest(reps[50]) and reps[15].read_text(encoding="utf-8") == before15


def test_add_without_volume_updates_the_15_report_only(box):
    root, reps = box
    before50 = reps[50].read_text(encoding="utf-8")
    l3log.add(_rec(), root, today=TODAY)
    assert "l3" in _latest(reps[15]) and reps[50].read_text(encoding="utf-8") == before50


def test_add_of_an_older_fault_leaves_the_chosen_report_alone(box):
    root, reps = box
    before = reps[50].read_text(encoding="utf-8")
    got = l3log.add(_rec(fault="F-1", volume=50), root, today=TODAY)
    assert got["latest_updated"] is False and reps[50].read_text(encoding="utf-8") == before


# ── 4 · annotate ─────────────────────────────────────────────────────────────

def test_annotate_appends_a_line_and_never_touches_the_original_line(box):
    root, _ = box
    l3log.add(_rec(volume=50), root, today=TODAY)
    original = _lines(root)[0]
    l3log.annotate(_note(), root)
    lines = _lines(root)
    assert lines[0] == original and len(lines) == 2
    note = json.loads(lines[1])
    assert note["annotate"] == {"fault": "F-277", "at": AT} and "q_ko" not in json.loads(original)["turns"][0]


def test_load_merges_the_note_into_the_record_and_leaves_other_turns_alone(box):
    root, _ = box
    l3log.add(_rec(volume=50), root, today=TODAY)
    l3log.annotate(_note(), root)
    (rec,), broken = l3log.load(root=root, today=TODAY)
    assert broken == 0 and rec["turns"][0]["q_ko"] == "질문 해석" and rec["turns"][0]["answer_ko"] == "답 해석"
    assert "q_ko" not in rec["turns"][1] and rec["turns"][0]["answer"].startswith("答え1")      # 원문 그대로


def test_annotate_refreshes_latest_l3_in_the_report_the_original_record_names(box):
    root, reps = box
    l3log.add(_rec(volume=50), root, today=TODAY)
    got = l3log.annotate(_note(), root)                      # 보충에 volume 이 없어도 원래 기록(50)을 따른다
    assert Path(got["report"]) == reps[50] and got["latest_updated"]
    t = _latest(reps[50])["l3"]["turns"][0]
    assert t["q_ko"] == "질문 해석" and t["a_head_ko"] == "답 해석"
    assert "l3" not in _latest(reps[15])


def test_annotate_note_volume_must_match_the_original_record(box):
    root, _ = box
    l3log.add(_rec(volume=50), root, today=TODAY)
    with pytest.raises(ValueError, match="50%"):
        l3log.annotate(_note(volume=15), root)
    assert len(_lines(root)) == 1


@pytest.mark.parametrize("note,why", [
    (_note(fault="F-999"), "붙일 기록이 없다"),
    (_note(turns=[{"n": 9, "q_ko": "x"}]), "없는 turn 번호"),
])
def test_annotate_that_cannot_attach_raises_and_writes_nothing(box, note, why):
    root, _ = box
    l3log.add(_rec(volume=50), root, today=TODAY)
    with pytest.raises(ValueError, match=why):
        l3log.annotate(note, root)
    assert len(_lines(root)) == 1


def test_annotate_without_any_log_dir_raises(tmp_path):
    with pytest.raises(ValueError, match="붙일 기록이 없다"):
        l3log.annotate(_note(), tmp_path / "none")
    assert not (tmp_path / "none").exists()


@pytest.mark.parametrize("extra", [{"q": "새 질문"}, {"answer": "새 답"}, {"verdict": "issue"}, {"note": "n"}, {"kind": "data"}])
def test_note_cannot_change_the_original_or_the_verdict(extra):
    turns = [{"n": 1, "q_ko": "해석", **extra}]
    err = l3log.validate_note({**_note(turns=turns, at=AT), "at": AT})
    assert err and "해석 밖의 칸" in err[0]


@pytest.mark.parametrize("turns", [[], None, [{"n": 1}], [{"n": 1, "q_ko": "  ", "answer_ko": ""}],
                                   [{"n": "1", "q_ko": "x"}], [{"n": True, "q_ko": "x"}], [{"n": 1, "q_ko": 3}],
                                   [{"n": 1, "answer_ko": ["x"]}], ["x"]])
def test_note_with_no_usable_translation_is_invalid(turns):
    note = {**_note(at=AT), "at": AT, "turns": turns}                                # `_note(turns=None)` 은 기본 해석이 된다
    assert l3log.validate_note(note)


@pytest.mark.parametrize("bad", [{"annotate": None, "turns": [{"n": 1, "q_ko": "x"}]}, {"turns": [{"n": 1, "q_ko": "x"}]},
                                 _note(fault="F-"), _note(fault=None), _note(at="어제"), _note(volume=99)])
def test_malformed_notes_are_invalid(bad):
    assert l3log.validate_note({**bad, "at": AT} if "at" not in bad else bad)


def test_note_without_annotate_at_is_valid_and_gets_attached_to_the_latest_record(box):
    root, _ = box
    l3log.add(_rec(at="2026-11-11T09:00:00", volume=50), root, today=TODAY)
    l3log.add(_rec(at="2026-11-11T10:37:00", volume=50), root, today=TODAY)
    got = l3log.annotate(_note(), root)
    note = json.loads(_lines(root)[-1])
    assert note["annotate"]["at"] == "2026-11-11T10:37:00" and got["latest_updated"]
    recs, broken = l3log.load(root=root, today=TODAY)
    by_at = {r["at"]: r for r in recs}
    assert broken == 0 and "q_ko" in by_at["2026-11-11T10:37:00"]["turns"][0] and "q_ko" not in by_at["2026-11-11T09:00:00"]["turns"][0]


def test_annotate_with_an_explicit_at_targets_that_record_even_if_older(box):
    root, _ = box
    l3log.add(_rec(at="2026-11-11T09:00:00", volume=50), root, today=TODAY)
    l3log.add(_rec(at="2026-11-11T10:37:00", volume=50), root, today=TODAY)
    l3log.annotate(_note(at="2026-11-11T09:00:00"), root)
    recs, _ = l3log.load(root=root, today=TODAY)
    by_at = {r["at"]: r for r in recs}
    assert "q_ko" in by_at["2026-11-11T09:00:00"]["turns"][0] and "q_ko" not in by_at["2026-11-11T10:37:00"]["turns"][0]


def test_a_later_note_wins_over_an_earlier_one(box):
    root, _ = box
    l3log.add(_rec(volume=50), root, today=TODAY)
    l3log.annotate(_note(turns=[{"n": 1, "q_ko": "처음"}], at=AT, ), root)
    l3log.annotate({**_note(turns=[{"n": 1, "q_ko": "고침"}], at=AT), "at": "2026-11-11T12:00:00"}, root)
    (rec,), _ = l3log.load(root=root, today=TODAY)
    assert rec["turns"][0]["q_ko"] == "고침"


def test_annotating_one_field_keeps_the_other_field_from_an_earlier_note(box):
    root, _ = box
    r = _rec(volume=50)
    r["turns"][0]["q_ko"] = "원래 질문 해석"
    l3log.add(r, root, today=TODAY)
    l3log.annotate(_note(turns=[{"n": 1, "answer_ko": "답만"}]), root)
    (rec,), _ = l3log.load(root=root, today=TODAY)
    assert rec["turns"][0]["q_ko"] == "원래 질문 해석" and rec["turns"][0]["answer_ko"] == "답만"


def test_a_note_with_no_original_is_counted_as_a_broken_line_not_dropped_silently(box):
    root, _ = box
    root.mkdir()
    orphan = {**_note(fault="F-888", at=AT), "at": AT}
    (root / "2026-11-11.jsonl").write_text(json.dumps(orphan, ensure_ascii=False) + "\n", encoding="utf-8")
    recs, broken = l3log.load(root=root, today=TODAY)
    assert recs == [] and broken == 1


def test_a_malformed_note_line_is_counted_as_broken(box):
    root, _ = box
    l3log.add(_rec(volume=50), root, today=TODAY)
    with (root / "2026-11-11.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"annotate": {"fault": "F-277"}, "turns": [{"n": 1, "q": "원문 변조"}], "at": AT}) + "\n")
    recs, broken = l3log.load(root=root, today=TODAY)
    assert broken == 1 and "q_ko" not in recs[0]["turns"][0] and recs[0]["turns"][0]["q"] == "질문1"


def test_the_note_goes_to_the_same_day_file_as_its_original_so_pruning_ends_together(box):
    root, _ = box
    l3log.add(_rec(at="2026-11-10T23:59:00", volume=50), root, today=TODAY)
    l3log.annotate(_note(), root)
    assert [p.name for p in root.iterdir()] == ["2026-11-10.jsonl"] and len(_lines(root, "2026-11-10")) == 2


# ── 5 · CLI ──────────────────────────────────────────────────────────────────

class _A:
    def __init__(self, op, file=None, volume=None):
        self.op, self.file, self.volume = op, file, volume


@pytest.fixture
def cli_box(box, monkeypatch):
    root, reps = box
    real_add, real_ann, real_prune = l3log.add, l3log.annotate, l3log.prune
    monkeypatch.setattr(l3log, "add", lambda rec: real_add(rec, root, today=TODAY))
    monkeypatch.setattr(l3log, "annotate", lambda note: real_ann(note, root))
    monkeypatch.setattr(l3log, "prune", lambda r=root, t=None: real_prune(r, t))
    return root, reps


def _write(tmp_path, name, obj):
    p = tmp_path / name
    p.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    return str(p)


def test_cli_volume_fills_a_missing_volume_and_picks_the_v50_report(cli_box, tmp_path, capsys):
    root, reps = cli_box
    assert cli.cmd_l3(_A("add", _write(tmp_path, "r.json", _rec()), 50)) == 0
    assert json.loads(_lines(root)[0])["volume"] == 50 and "scenario_report_v50.json" in capsys.readouterr().out
    assert "l3" in _latest(reps[50]) and "l3" not in _latest(reps[15])


def test_cli_volume_conflicting_with_the_record_is_refused_and_writes_nothing(cli_box, tmp_path, capsys):
    root, _ = cli_box
    assert cli.cmd_l3(_A("add", _write(tmp_path, "r.json", _rec(volume=15)), 50)) == 1
    assert "거부" in capsys.readouterr().out and not root.exists()


def test_cli_annotate_attaches_a_translation_and_prints_the_report_name(cli_box, tmp_path, capsys):
    root, reps = cli_box
    cli.cmd_l3(_A("add", _write(tmp_path, "r.json", _rec()), 50))
    capsys.readouterr()
    assert cli.cmd_l3(_A("annotate", _write(tmp_path, "n.json", _note()))) == 0
    out = capsys.readouterr().out
    assert "F-277 해석 1턴" in out and "scenario_report_v50.json" in out
    assert _latest(reps[50])["l3"]["turns"][0]["a_head_ko"] == "답 해석"


def test_cli_annotate_accepts_a_list_and_rejects_a_bad_one_with_rc1(cli_box, tmp_path, capsys):
    root, _ = cli_box
    cli.cmd_l3(_A("add", _write(tmp_path, "r.json", _rec()), 50))
    two = _write(tmp_path, "two.json", [_note(), _note(turns=[{"n": 2, "answer_ko": "둘째"}])])
    assert cli.cmd_l3(_A("annotate", two)) == 0
    n = len(_lines(root))
    assert cli.cmd_l3(_A("annotate", _write(tmp_path, "bad.json", _note(fault="F-999")))) == 1
    assert "거부" in capsys.readouterr().out and len(_lines(root)) == n


def test_cli_annotate_volume_mismatch_is_refused(cli_box, tmp_path, capsys):
    root, _ = cli_box
    cli.cmd_l3(_A("add", _write(tmp_path, "r.json", _rec()), 50))
    n = len(_lines(root))
    assert cli.cmd_l3(_A("annotate", _write(tmp_path, "n.json", _note()), 15)) == 1
    assert len(_lines(root)) == n


def test_cli_parser_knows_annotate_and_volume_choices():
    import argparse
    src = (Path(cli.__file__)).read_text(encoding="utf-8")
    assert 'choices=("add", "annotate", "prune")' in src and 'choices=(15, 50)' in src
    with pytest.raises(SystemExit):
        cli.main(["l3", "add", "x.json", "--volume", "99"])


# ── 6 · scenario_ja ──────────────────────────────────────────────────────────

def test_skip_keys_leave_the_korean_translation_fields_out_of_the_translation_targets():
    assert {"q_ko", "a_head_ko"} <= J.SKIP_KEYS and "evidence" in J.SKIP_KEYS
    report = {"scenarios": [{"no": 1, "latest": {"l3": {"turns": [
        {"n": 1, "q": "질문", "a_head": "あ", "q_ko": "질문 해석", "a_head_ko": "답 해석", "note": "메모"}]}}}], "notes": []}
    cells = {cell for _, cell, _, _ in J.strings(report)}
    assert "latest.l3.turns.0.q" in cells and "latest.l3.turns.0.note" in cells
    assert not any(c.endswith("q_ko") or c.endswith("a_head_ko") for c in cells)


def test_merged_report_keeps_the_korean_translation_fields_without_counting_them_as_missing():
    report = {"scenarios": [{"no": 1, "latest": {"l3": {"turns": [{"n": 1, "q_ko": "해석", "a_head_ko": "해석"}]}}}], "notes": []}
    merged, info = J.merge(copy.deepcopy(report), {"scenarios": {}})
    assert info == {"missing": [], "stale": []}
    assert merged["scenarios"][0]["latest"]["l3"]["turns"][0]["q_ko"] == "해석"
