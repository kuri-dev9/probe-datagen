"""일본어 화면의 원인 이름·조각 제목 (2026-11-25 · 맥스 구현 · 에스터 시험).

사용자 지적: 「다른 건 다 일본어인데 장애 목록이 한국어다」. 화면은 원인 **코드**로 일본어 이름을 찾고(`causeName`),
조각 제목은 정해진 한국어 조각을 옮긴다(`titleName`). 두 표는 graphio 의 사본이라 graphio 가 늘면 여기도 늘어야 한다 —
어긋나면 그 코드·지표만 **조용히** 한국어로 남는다. 이 시험이 그 어긋남을 실패로 바꾼다.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from datagen.ui.i18n_ja import _JA_CAUSE, _JA_MAP, _JA_TITLE_PARTS

ROOT = Path(__file__).resolve().parents[1]
GP = ROOT.parent / "graphio-probe-rca" / "src" / "graphio"
DETECTOR = GP / "detect" / "detector.py"
HTML = (ROOT / "src" / "datagen" / "ui" / "static" / "map.html").read_text(encoding="utf-8")
HANGUL = re.compile(r"[가-힣]")


@pytest.fixture(scope="module")
def tax():
    return pytest.importorskip("graphio.rca.taxonomy")


# ── 원인 이름 (코드 → 일본어) ────────────────────────────────────────────────

def test_cause_keys_cover_every_graphio_cause_code(tax):
    assert {k.split(":", 1)[1] for k in _JA_CAUSE} == set(tax.BY_CODE)       # 코드가 늘면 여기도 늘어야 한다


def test_cause_values_are_graphio_cause_labels_ja(tax):
    for code, ja in tax.CAUSE_LABELS_JA.items():
        assert _JA_CAUSE.get("cause:" + code) == ja, code
    # 정본에 없는 코드를 여기서 지어내지 않는다
    assert {k.split(":", 1)[1] for k in _JA_CAUSE} <= set(tax.CAUSE_LABELS_JA) | set(tax.BY_CODE)


def test_every_cause_has_a_japanese_name_with_no_hangul(tax):
    for code in tax.BY_CODE:
        ja = _JA_CAUSE.get("cause:" + code)
        assert ja, code
        assert not HANGUL.search(ja), (code, ja)


def test_cause_names_are_in_the_screen_map_and_unique_enough():
    assert all(_JA_MAP[k] == v for k, v in _JA_CAUSE.items())
    assert len(set(_JA_CAUSE.values())) >= len(_JA_CAUSE) - 1                  # 서로 다른 원인이 같은 이름으로 합쳐지지 않는다


# ── 조각 제목 (graphio `detector.title_for` 의 한국어 조각) ─────────────────────

def _title_for_pieces():
    tree = ast.parse(DETECTOR.read_text(encoding="utf-8"))
    kpi_label, consts = {}, set()
    node_names = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name == "title_for":
            for sub in ast.walk(n):
                if isinstance(sub, ast.Assign) and any(getattr(t, "id", "") == "kpi_label" for t in sub.targets):
                    kpi_label = ast.literal_eval(sub.value)
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str) and HANGUL.search(sub.value):
                    consts.add(sub.value)
        if isinstance(n, ast.Assign) and any(getattr(t, "id", "") == "NODE_TITLE_NAME" for t in n.targets):
            node_names = ast.literal_eval(n.value)
    return kpi_label, consts, node_names


@pytest.mark.skipif(not DETECTOR.exists(), reason="graphio 소스가 없다")
def test_title_parts_cover_every_kpi_label_of_title_for():
    kpi_label, _, _ = _title_for_pieces()
    assert len(kpi_label) >= 20                                                # 사전이 비어 읽혔으면 아래 검사가 헛돈다
    missing = [v for v in kpi_label.values() if v not in _JA_TITLE_PARTS]
    assert missing == []


@pytest.mark.skipif(not DETECTOR.exists(), reason="graphio 소스가 없다")
def test_title_parts_cover_node_names_and_the_two_fallback_words():
    _, consts, node_names = _title_for_pieces()
    assert set(node_names) == {"MME", "SGW", "PGW"}
    for v in node_names.values():
        assert v in _JA_TITLE_PARTS, v
    for w in ("판별 불가", "Core 장비"):
        assert w in _JA_TITLE_PARTS, w


def test_title_parts_have_no_hangul_and_are_in_the_screen_map():
    for k, v in _JA_TITLE_PARTS.items():
        assert not HANGUL.search(v), (k, v)
        assert _JA_MAP["title:" + k] == v


def test_title_parts_do_not_swallow_each_other_when_applied_longest_first():
    """`titleName` 은 긴 것부터 바꾼다 — 짧은 조각(`앱 축`)이 긴 조각(`앱 분류 축`)을 먼저 먹으면 한국어가 남는다."""
    def apply(s):
        for k in sorted(_JA_TITLE_PARTS, key=len, reverse=True):
            s = s.replace(k, _JA_TITLE_PARTS[k])
        return s
    for t in ("앱 분류 축 앱 지연", "Core 노드 축 DNS 실패 급증", "접속 관리 장비(MME-1) 외 2대 NR 레그 연결 실패",
              "시군구(ADM2) 소통률 저하 · 절단률 급증"):
        out = apply(t)
        left = HANGUL.findall(re.sub(r"외 \d+(대|곳)|\(MME-1\)", "", out))
        assert left == [] or set(left) <= set("외대곳"), (t, out)             # 숫자 든 꼬리(「외 n대」)는 화면 정규식이 맡는다


# ── 화면 연결 ───────────────────────────────────────────────────────────────

def test_screen_looks_up_cause_by_code_only_on_the_japanese_screen():
    assert "const causeName=(code,label)=>(LANG==='ja'&&code&&JA['cause:'+code])||label||code;" in HTML
    # 없는 코드는 graphio 의 한국어 이름으로 남는다(`||label`) — 조용히 빈 칸이 되지 않는다. 한국어 화면(LANG!=='ja')은 그대로다
    assert "TITLE_JA=LANG==='ja'" in HTML                                       # 조각 제목 번역도 일본어 화면에서만 만든다
    i = HTML.index("function titleName")
    assert "TITLE_JA" in HTML[i:i + 400]


def test_every_list_panel_and_replay_spot_goes_through_the_name_functions():
    # 장애 목록 줄 · 위 패널 · 구성 조각 버튼 툴팁 · 사건 카드 제목이 모두 `headOf`/`titleName`/`causeName` 을 거친다
    assert HTML.count("esc(headOf(") >= 2
    assert HTML.count("titleName(") >= 4
    assert "causeName(" in HTML[HTML.index("const headOf="):HTML.index("const headOf=") + 200]
    assert not re.search(r"esc\(h\.root_label\)", HTML) and not re.search(r"esc\(i\.title\)", HTML)   # 번역 없이 그대로 내는 자리가 남지 않았다
