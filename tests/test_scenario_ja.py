"""시나리오 현황의 일본어 번역 병합 (`scenario_ja` · `/api/scenario-report`, 2026-11-06).

한국어 `scenario_report.json` 은 판정이 끝날 때마다 고친다. 번역은 칸(경로)마다 **원문 해시**와 따로 두고 요청 때 겹친다.
원문이 바뀌면 해시가 어긋나므로 **옛 번역을 내지 않고 한국어를 그대로 보이며 낡음으로 센다** — 다른 판정의 번역이 붙어
나오는 것보다 한국어가 남는 쪽이 낫다. 목록 앞에 한 줄이 끼어 칸 번호가 밀린 것은 원문이 바뀐 것이 아니라 같은 원문의
번역을 쓴다.

DB 없이 돈다 — 임시 파일을 가리키게 바꿔 끼운다.
"""
from __future__ import annotations

import json

import pytest

from datagen import scenario_ja as J
from datagen import ui


def _report(summary="원인을 맞혔다", issues=("범위가 넓다",)):
    return {"notes": ["기준 한 줄"], "scenarios": [
        {"no": 1, "name": "SGW 자원 소진", "latest": {"summary": summary, "issues": list(issues)},
         "evidence": ["runs.md 한글 근거"], "count": 3, "kind": "gw_resource"}]}


def _ja_of(report, ja_text=lambda t: "JA:" + t):
    table: dict = {}
    for g, cell, _, text in J.strings(report):
        table.setdefault(g, {})[cell] = {"h": J.src_hash(text), "ja": ja_text(text)}
    return {"scenarios": table}


# ---------------------------------------------------------------- 해시·대상

def test_hash_is_ten_hex_chars_and_changes_with_the_text():
    a, b = J.src_hash("가"), J.src_hash("나")
    assert len(a) == 10 and int(a, 16) >= 0 and a != b and a == J.src_hash("가")


def test_strings_lists_only_korean_text_with_group_cell_and_path():
    got = {(g, c): t for g, c, _, t in J.strings(_report())}
    assert got[("1", "name")] == "SGW 자원 소진"
    assert got[("1", "latest.summary")] == "원인을 맞혔다"
    assert got[("1", "latest.issues.0")] == "범위가 넓다"
    assert got[("notes", "0")] == "기준 한 줄"


def test_evidence_and_non_korean_values_are_not_translation_targets():
    cells = {c for _, c, _, _ in J.strings(_report())}
    assert not any(c.startswith("evidence") for c in cells)
    assert "count" not in cells and "kind" not in cells and "no" not in cells


# ---------------------------------------------------------------- merge

def test_a_matching_hash_is_replaced_by_the_japanese():
    rep = _report()
    out, info = J.merge(rep, _ja_of(rep))
    assert out["scenarios"][0]["latest"]["summary"] == "JA:원인을 맞혔다"
    assert out["scenarios"][0]["name"] == "JA:SGW 자원 소진"
    assert info == {"missing": [], "stale": []}


def test_the_original_report_is_not_mutated():
    rep = _report()
    J.merge(rep, _ja_of(rep))
    assert rep["scenarios"][0]["latest"]["summary"] == "원인을 맞혔다"


def test_a_changed_source_keeps_the_korean_and_counts_as_stale():
    old = _report()
    ja = _ja_of(old)
    new = _report(summary="원인을 못 맞혔다")
    out, info = J.merge(new, ja)
    assert out["scenarios"][0]["latest"]["summary"] == "원인을 못 맞혔다", "옛 번역이 새 판정에 붙어 나왔다"
    assert info["stale"] == ["1:latest.summary"] and info["missing"] == []
    assert out["scenarios"][0]["name"] == "JA:SGW 자원 소진", "바뀌지 않은 칸은 번역이 그대로 나온다"


def test_a_cell_without_any_translation_is_missing_and_stays_korean():
    rep = _report()
    ja = _ja_of(rep)
    del ja["scenarios"]["1"]["latest.issues.0"]
    out, info = J.merge(rep, ja)
    assert out["scenarios"][0]["latest"]["issues"] == ["범위가 넓다"]
    assert info["missing"] == ["1:latest.issues.0"] and info["stale"] == []


def test_a_line_inserted_before_shifts_cells_but_the_same_text_still_translates():
    """`issues` 앞에 한 줄이 끼면 뒤 칸 번호가 밀린다 — 원문이 바뀐 것이 아니다."""
    old = _report(issues=("첫째", "둘째"))
    ja = _ja_of(old)
    new = _report(issues=("새로 낀 줄", "첫째", "둘째"))
    out, info = J.merge(new, ja)
    got = out["scenarios"][0]["latest"]["issues"]
    assert got[1:] == ["JA:첫째", "JA:둘째"]
    # 새 줄이 놓인 칸에는 옛 번역("첫째")이 있던 자리라 번역이 없는 칸이 아니라 낡은 칸으로 센다
    assert got[0] == "새로 낀 줄" and info == {"missing": [], "stale": ["1:latest.issues.0"]}


def test_empty_translation_table_leaves_everything_korean_and_missing():
    rep = _report()
    out, info = J.merge(rep, {"scenarios": {}})
    assert out == rep and len(info["missing"]) == len(list(J.strings(rep)))


def test_a_report_without_scenarios_does_not_crash():
    assert J.merge({}, {})[1] == {"missing": [], "stale": []}


# ---------------------------------------------------------------- todo · apply · scan (임시 파일)

@pytest.fixture
def files(tmp_path, monkeypatch):
    rep, ja = tmp_path / "report.json", tmp_path / "ja.json"
    monkeypatch.setattr(J, "REPORT", rep)
    monkeypatch.setattr(J, "REPORT_JA", ja)
    rep.write_text(json.dumps(_report(), ensure_ascii=False), encoding="utf-8")
    return rep, ja


def test_todo_lists_cells_without_a_valid_translation(files):
    got = J.todo()
    assert got["1:latest.summary"] == "원인을 맞혔다" and "notes:0" in got


def test_apply_writes_the_hash_of_the_current_source_and_clears_the_todo(files):
    rep, ja = files
    done = {k: "JA" + k for k in J.todo()}
    assert J.apply(done) == len(done)
    doc = json.loads(ja.read_text(encoding="utf-8"))
    assert doc["scenarios"]["1"]["latest.summary"] == {"h": J.src_hash("원인을 맞혔다"), "ja": "JA1:latest.summary"}
    assert J.todo() == {}


def test_apply_drops_translations_of_cells_that_no_longer_exist(files):
    rep, ja = files
    J.apply({k: "x" for k in J.todo()})
    rep.write_text(json.dumps({"notes": [], "scenarios": [{"no": 1, "name": "SGW 자원 소진"}]}, ensure_ascii=False), encoding="utf-8")
    J.apply({})
    assert set(json.loads(ja.read_text(encoding="utf-8"))["scenarios"]["1"]) == {"name"}


def test_changing_the_report_after_apply_makes_that_cell_todo_again(files):
    rep, _ = files
    J.apply({k: "x" for k in J.todo()})
    rep.write_text(json.dumps(_report(summary="다른 판정"), ensure_ascii=False), encoding="utf-8")
    assert list(J.todo()) == ["1:latest.summary"]


# ---------------------------------------------------------------- why_ja (권장 시간 산정 근거)

@pytest.mark.parametrize("ko,ja", [
    ("계단 마지막 구간까지", "階段の最終区間まで"),
    ("국면 3개(몰림·분산·분석)", "3局面(集中・分散・分析)"),
    ("열화·회복 3주기(3분 주기)", "劣化・回復 3周期(3分周期)"),
    ("실측 2회 · 정답까지 0분 + 하강 3", "実測 2回 · 正解まで 0分 + 下降 3"),
    ("실측 1회 정답 없음", "実測 1回 正解なし"),
    ("상승 3 + 정점 5 + 하강 3", "上昇 3 + ピーク 5 + 下降 3"),
])
def test_why_ja_translates_every_phrase_the_calculator_writes(ko, ja):
    assert J.why_ja(ko) == ja


def test_why_ja_leaves_no_hangul_for_any_kind_in_the_registry():
    from datagen.scenarios import KIND_SPECS, recommended_duration

    left = [k for k in KIND_SPECS if J.HANGUL.search(J.why_ja((recommended_duration(k) or {}).get("why") or ""))]
    assert left == [], f"일본어 화면에 한글이 남는 산정 근거: {left}"


# ---------------------------------------------------------------- /api/scenario-report

@pytest.fixture
def api(tmp_path, monkeypatch):
    def put(name, doc):
        p = tmp_path / name
        p.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        return p

    rep = _report()
    monkeypatch.setattr(ui, "SCENARIO_REPORT", put("report.json", rep))
    monkeypatch.setattr(ui, "SCENARIO_HOW", put("how.json", {"scenarios": {"1": {"brief": "b"}}}))
    monkeypatch.setattr(ui, "SCENARIO_REPORT_JA", put("ja.json", _ja_of(rep)))
    monkeypatch.setattr(ui, "SCENARIO_GLOSSARY", put("g.json", {"terms": {"R-05": {"ko": "룰"}}}))
    return tmp_path


def test_the_endpoint_returns_report_how_japanese_and_glossary(api):
    out = ui.api_scenario_report()
    assert out["ok"] and out["report"]["scenarios"][0]["no"] == 1
    assert out["how"] == {"1": {"brief": "b"}} and out["glossary"] == {"R-05": {"ko": "룰"}}
    assert out["report_ja"]["scenarios"][0]["name"] == "JA:SGW 자원 소진" and out["ja_left"] == 0
    assert out["report"]["scenarios"][0]["name"] == "SGW 자원 소진", "한국어 원문은 그대로 내려간다"


def test_the_endpoint_counts_stale_cells_when_the_korean_changes(api):
    (api / "report.json").write_text(json.dumps(_report(summary="새 판정"), ensure_ascii=False), encoding="utf-8")
    out = ui.api_scenario_report()
    assert out["ja_left"] == 1
    assert out["report_ja"]["scenarios"][0]["latest"]["summary"] == "새 판정"


def test_a_missing_report_is_a_reason_not_an_empty_list(api):
    (api / "report.json").unlink()
    assert ui.api_scenario_report() == {"ok": False, "reason": "missing", "path": "config/report.json"}


def test_a_broken_report_is_reported_as_broken(api):
    (api / "report.json").write_text("{깨짐", encoding="utf-8")
    out = ui.api_scenario_report()
    assert out["ok"] is False and out["reason"] == "broken"


def test_missing_translation_and_glossary_only_drop_their_parts(api):
    (api / "ja.json").unlink()
    (api / "g.json").unlink()
    out = ui.api_scenario_report()
    assert out["ok"] and "report_ja" not in out and "glossary" not in out and "how_error" not in out


def test_a_broken_translation_file_is_reported_without_losing_the_report(api):
    (api / "ja.json").write_text("not json", encoding="utf-8")
    out = ui.api_scenario_report()
    assert out["ok"] and out["how_error"]["reason"] == "broken" and "report_ja" not in out


def test_a_missing_how_file_still_shows_the_results(api):
    (api / "how.json").unlink()
    out = ui.api_scenario_report()
    assert out["ok"] and out["how"] == {} and "how_error" not in out
