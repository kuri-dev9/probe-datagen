"""시나리오 목록 줄(11-16 사용자) — 장비 장애 배지·설명 칸 삭제·한 줄 이름.

장비 장애는 `KIND_SPECS.truth_category` 가 CORE_UP·CORE_CP 인 종류다(새 기준 없음). 나중에 제외될 수
있다고 해서 **번호를 박지 않고 분류로 고른다** — 이 시험은 그 분류와 지금 걸리는 번호(1·3·4·10·18·19)를 함께 고정한다.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from datagen.faults import KIND_SPECS
from datagen.ui import scenarios_api
from datagen.ui.i18n_ja import _JA_SCEN

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "src" / "datagen" / "ui" / "static" / "scenarios.html").read_text(encoding="utf-8")
FILES = ["scenario_report.json", "scenario_report_v50.json", "scenario_report_ja.json", "scenario_report_v50_ja.json"]
EQUIP_NOS = [1, 3, 4, 10, 18, 19]


def _rows(fn):
    d = json.loads((ROOT / "config" / fn).read_text(encoding="utf-8"))
    rows = d.get("scenarios", d) if isinstance(d, dict) else d
    if isinstance(rows, dict):
        rows = [dict(v, no=int(k)) for k, v in rows.items()]
    return rows


def _cat(sp):
    return sp.get("truth_category") if isinstance(sp, dict) else getattr(sp, "truth_category", None)


def test_equip_kinds_are_exactly_core_categories():
    got = scenarios_api._equip_kinds()
    want = {k for k, sp in KIND_SPECS.items() if _cat(sp) in ("CORE_UP", "CORE_CP")}
    assert set(got) == want and got
    assert got == sorted(got)
    assert scenarios_api.EQUIP_CATEGORIES == ("CORE_UP", "CORE_CP")


@pytest.mark.parametrize("fn", FILES[:2])
def test_badge_numbers(fn):
    eq = set(scenarios_api._equip_kinds())
    assert [int(r["no"]) for r in _rows(fn) if r.get("kind") in eq] == EQUIP_NOS
    # 경계 번호(변경 의심·IMS)는 넣지 않는다 — 사용자 결정 전
    for r in _rows(fn):
        if int(r["no"]) in (11, 12, 20):
            assert r.get("kind") not in eq


def test_api_returns_equip_kinds():
    out = scenarios_api.api_scenario_report()
    assert out["ok"] and out["equip_kinds"] == scenarios_api._equip_kinds()


def test_equip_kinds_falls_back_to_empty_when_specs_unreadable(monkeypatch):
    import datagen.faults as f
    monkeypatch.delattr(f, "KIND_SPECS")
    assert scenarios_api._equip_kinds() == []


def test_html_badge_and_no_description_column():
    assert "EQUIP.has(s.kind)" in HTML and "T('장비 장애')" in HTML
    # 설명 칸(.ln)은 줄에서 빠졌다
    assert "summary .ln" not in HTML and "class=ln" not in HTML
    render = HTML.split("function render()")[1].split("\nfunction ")[0]
    assert "'brief'" not in render
    # 판정 앞에 배지: eq 가 st 보다 먼저, 같은 칸(.stc) 안
    row = re.search(r"<span class=stc>.*?</span></span>", HTML).group(0)
    assert row.index("${eq}") < row.index('class="st ')
    # 그림·카드(펼침)는 그대로
    assert "${fromFault(s)}${detail(s)}" in HTML
    # 이름은 한 줄
    assert "summary .nm { font-weight:600; white-space:nowrap" in HTML


def test_ja_badge_label():
    assert _JA_SCEN["장비 장애"] == "装置障害"


def _no_name(doc):
    rows = doc.get("scenarios", doc) if isinstance(doc, dict) else doc
    items = rows.items() if isinstance(rows, dict) else ((str(r.get("no")), r) for r in rows)
    return {k: ({kk: vv for kk, vv in v.items() if kk != "name"} if isinstance(v, dict) else v) for k, v in items}, \
        ({k: v for k, v in doc.items() if k != "scenarios"} if isinstance(doc, dict) else {})


@pytest.mark.parametrize("fn", FILES)
def test_only_name_changed_vs_head(fn):
    """정의 파일은 시나리오 줄의 `name` 만 바뀌었다 — 커밋 전(HEAD)과 대조. 커밋된 뒤에는 같으니 통과한다"""
    r = subprocess.run(["git", "show", f"HEAD:config/{fn}"], cwd=ROOT, capture_output=True, text=True)
    if r.returncode:
        pytest.skip("HEAD 에 파일 없음")
    old = _no_name(json.loads(r.stdout))
    new = _no_name(json.loads((ROOT / "config" / fn).read_text(encoding="utf-8")))
    assert old == new


@pytest.mark.parametrize("fn", FILES[:2])
def test_names_fit_one_line(fn):
    for r in _rows(fn):
        assert r["name"] and len(r["name"]) <= 28 and "\n" not in r["name"], r["no"]


@pytest.mark.parametrize("ko,ja", [("scenario_report.json", "scenario_report_ja.json"),
                                   ("scenario_report_v50.json", "scenario_report_v50_ja.json")])
def test_ja_scan_is_zero_for_both_pairs(monkeypatch, ko, ja):
    """`scan()` 은 모듈 상수 경로를 읽는다 — 두 판을 번갈아 걸어 빠진 칸·낡은 칸·한글 잔재가 0 인지 본다"""
    from datagen import scenario_ja
    monkeypatch.setattr(scenario_ja, "REPORT", ROOT / "config" / ko)
    monkeypatch.setattr(scenario_ja, "REPORT_JA", ROOT / "config" / ja)
    res = scenario_ja.scan()
    assert res["report"] == 0 and res["missing"] == 0 and res["stale"] == 0, res
