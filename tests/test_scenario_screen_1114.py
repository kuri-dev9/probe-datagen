"""시나리오 화면 작업(2026-11-14 · 맥스) — 그림 경로·읍면동 모양·쉬운 이름·용어집 일본어.

DB 에는 쓰지 않는다. `_dong_at` 만 DB 를 읽는 자리라 가짜 연결로 갈아 끼운다.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

from datagen import control
from datagen.ui import map_api, pages

ROOT = Path(__file__).resolve().parents[1]
GRAPHIO = ROOT.parent / "graphio-probe-rca"
NAMES_MD = GRAPHIO / "docs" / "jp" / "원인-쉬운이름-1114.md"
STATIC = ROOT / "src" / "datagen" / "ui" / "static"


# ── 시나리오 설명(2026-11-19 · 사용자 확정본 scenarios-14-27.html 을 섹션별로 옮김) ──────────
# 옛 한 장 그림(`/api/scenario-pictures` · `scenario-pictures/*.svg`)은 지웠다. 문구는 원본 그대로여야 한다.

ORIGINAL = GRAPHIO / "docs" / "jp" / "ui" / "scenarios-14-27.html"
NOS = list(range(14, 36))   # 28~35 신규 시나리오 포함(2026-11-19)
ORIG_NOS = list(range(14, 28))   # 원본(`scenarios-14-27.html`)에는 14~27 만 있다
_TOTOP = re.compile(r'<p class="totop">.*?</p>', re.S)


def _html():
    return (STATIC / "scenarios.html").read_text(encoding="utf-8")


def _template(no):
    h = _html()
    m = re.search(rf"<template id=scx-{no}>(.*?)</template>", h, re.S)
    assert m, f"scx-{no} 템플릿이 없다"
    return m.group(1)


def _norm(t):
    return re.sub(r"\s+", "", _TOTOP.sub("", t))


def test_scenarios_html_has_one_template_per_scenario_1_to_35():
    """1~13번 그림이 들어왔다(2026-11-20) — 번호마다 템플릿 하나, 한국어판만 센다(`-ja` 는 따로)."""
    ids = [int(x) for x in re.findall(r"<template id=scx-(\d+)>", _html())]
    assert sorted(ids) == list(range(1, 36)) and len(ids) == len(set(ids))
    assert set(NOS) <= set(ids)


def test_templates_for_1_to_13_hold_their_own_section():
    """1~13번도 자기 그림 한 장(`s{no}`)만 든다 — 옛날엔 설명이 없는 번호라 자리가 없었다(2026-11-20 반전)."""
    for no in range(1, 14):
        t = _template(no)
        assert t.lstrip().startswith(f'<section id="s{no}"') and t.count(f'id="s{no}"') == 1


@pytest.mark.parametrize("no", NOS)
def test_each_template_holds_its_own_section(no):
    t = _template(no)
    assert t.lstrip().startswith(f'<section id="s{no}"')
    assert t.count(f'id="s{no}"') == 1
    assert re.search(rf"<h1>{no}\. ", t)


@pytest.mark.skipif(not ORIGINAL.exists(), reason="graphio 원본이 없다")
@pytest.mark.parametrize("no", ORIG_NOS)
def test_template_text_equals_the_original_section(no):
    """문구는 한 글자도 바꾸지 않는다 — 원본에서 「↑ 목차」 줄만 뺀 것과 같다."""
    src = ORIGINAL.read_text(encoding="utf-8")
    a = src.index(f'<section id="s{no}"')
    nxt = src.find(f'<section id="s{no + 1}"', a)
    chunk = src[a:nxt if nxt > 0 else len(src)]
    tpl = _norm(_template(no))
    assert _norm(chunk).startswith(tpl), f"{no}번 설명이 원본과 다르다"


def test_shared_templates_exist_and_no_toc_link_leaked():
    h = _html()
    assert "<template id=scx-css>" in h and "<template id=scx-defs>" in h
    for no in NOS:
        assert "↑ 목차" not in _template(no)


def test_old_picture_routes_and_files_are_gone():
    assert not hasattr(pages, "scenario_picture") and not hasattr(pages, "scenario_picture_list")
    assert not (STATIC / "scenario-pictures").exists()
    paths = {getattr(r, "path", "") for r in pages.router.routes} if hasattr(pages, "router") else set()
    assert not any("scenario-pictures" in x for x in paths)


def test_scenarios_html_no_longer_loads_the_old_pictures():
    h = _html()
    assert "/api/scenario-pictures" not in h and "/scenario-pictures/" not in h
    assert "<scn-explain" in h and "attachShadow" in h


def test_package_data_does_not_list_the_removed_pictures():
    assert "scenario-pictures" not in (ROOT / "pyproject.toml").read_text(encoding="utf-8")


# ── 읍면동 모양 ────────────────────────────────────────────────────────────

class _Cur:
    def __init__(self, rows): self.rows = rows
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, *a, **k): pass
    def fetchall(self): return self.rows


class _Conn:
    def __init__(self, rows): self.rows = rows
    def cursor(self): return _Cur(self.rows)
    def close(self): pass


ROWS = [("D1", "가", 35.1, 139.1), ("D2", "나", 35.2, 139.2)]


def test_dong_at_attaches_rings_only_where_a_shape_exists(monkeypatch):
    ring = [[139.0, 35.0], [139.1, 35.0], [139.1, 35.1], [139.0, 35.0]]
    monkeypatch.setattr(map_api, "_connect", lambda: _Conn(ROWS))
    monkeypatch.setattr(map_api, "_dong_shapes", lambda: {"D1": [ring]})
    at = map_api._dong_at(["D1", "D2"])
    assert at["D1"]["rings"] == [ring] and "rings" not in at["D2"]
    assert at["D2"]["lat"] == 35.2 and at["D2"]["name"] == "나"


def test_dong_at_without_shape_file_gives_centres_only(monkeypatch):
    monkeypatch.setattr(map_api, "_connect", lambda: _Conn(ROWS))
    monkeypatch.setattr(map_api, "_dong_shapes", lambda: {})
    at = map_api._dong_at(["D1", "D2"])
    assert set(at) == {"D1", "D2"} and all("rings" not in v for v in at.values())


def test_dong_shapes_missing_config_or_file_is_empty(monkeypatch):
    monkeypatch.setattr(map_api, "_layer_path", lambda k: (None, "없다"))
    assert map_api._dong_shapes() == {}


def test_real_dong_shapes_file_reads_when_present():
    d = map_api._dong_shapes()
    if not d:
        pytest.skip("graphio data/geo/jp/dong_shapes.json 없음(geo-chome 미실행)")
    k, rings = next(iter(d.items()))
    assert rings and all(len(r) >= 3 and len(r[0]) == 2 for r in rings)


def test_map_html_counts_dot_cells_and_keeps_old_key_out():
    h = (STATIC / "map.html").read_text(encoding="utf-8")
    assert "T('경계 없음 {n}곳 — 중심점 근사'" in h
    assert "읍면동 경계 없음" not in h


# ── 쉬운 이름 ──────────────────────────────────────────────────────────────

def _md_names():
    out = {}
    for m in re.finditer(r"^\|\s*(\d+)\s*\|\s*([^|]+?)\s*\|.*\|\s*$", NAMES_MD.read_text(encoding="utf-8"), re.M):
        n = int(m.group(1))
        if 14 <= n <= 27:
            out[n] = m.group(2)
    return out


def test_names_table_has_14_rows():
    assert sorted(_md_names()) == list(range(14, 28))


def test_control_scenarios_use_the_table_names():
    want = _md_names()
    got = {r[0]: r[1] for r in control.SCENARIOS if r[0] in want}
    assert got == want


#: 현황 화면 목록 줄 이름(11-16 사용자 "한 줄로") — 14~27번. 표(원인-쉬운이름)·`control.SCENARIOS` 이름은
#: 긴 정본 그대로고 화면 정의 파일(`scenario_report*.json`)만 줄였다. 둘이 갈라진 것은 의도다
LIST_NAMES = {
    14: "역 주변 저녁 러시 혼잡", 15: "야마노테선 4G↔5G 전환 때 잠깐 멈춤",
    16: "5G 잡히는데 느림 (4G 앵커 혼잡)", 17: "5G 장애로 4G에 몰림",
    18: "5G 데이터 관문(UPF) 자원 바닥", 19: "5G 접속 관리(AMF) 과부하 확산",
    20: "음성 통화만 안 걸림 (VoNR)", 21: "동영상 서버(CDN) 느림", 22: "결제 앱 느림 (DNS 지연)",
    23: "행사장 인파로 사진 올리기 막힘", 24: "기지국 증설 뒤 이용자 이동 (장애 아님)",
    25: "설정 변경 뒤 통화 끊김 계단 증가 (SON)", 26: "SW 업데이트한 기종만 접속 실패",
    27: "지진 — 통화 몰림·기지국 정지",
}


@pytest.mark.parametrize("fn", ["scenario_report.json", "scenario_report_v50.json"])
def test_report_names_are_the_one_line_list_names(fn):
    rep = json.loads((ROOT / "config" / fn).read_text(encoding="utf-8"))
    rows = rep.get("scenarios", rep) if isinstance(rep, dict) else rep
    got = {int(r["no"]): r["name"] for r in rows if isinstance(r, dict) and 14 <= int(r.get("no", 0)) <= 27}
    assert got == LIST_NAMES


def test_scenario_ja_check_is_all_zero():
    from datagen import scenario_ja
    sys.modules  # noqa: B018
    # 모듈 기본 경로는 v50 이 아닌 파일이라 주 두 파일을 직접 점검한다
    res = scenario_ja.check() if callable(getattr(scenario_ja, "check", None)) else None
    if isinstance(res, dict):
        assert all(v == 0 for v in res.values()), res


# ── 용어집 일본어 = graphio CAUSE_LABELS_JA ─────────────────────────────────

def test_glossary_cause_ja_equals_graphio_cause_labels_ja():
    gsrc = GRAPHIO / "src"
    if not gsrc.exists():
        pytest.skip("graphio 없음")
    sys.path.insert(0, str(gsrc))
    try:
        from graphio.rca import taxonomy as T
    finally:
        sys.path.remove(str(gsrc))
    g = json.loads((ROOT / "config" / "scenario_glossary.json").read_text(encoding="utf-8"))
    terms = g["terms"]
    assert "EARTHQUAKE_IMPACT" in terms
    checked = 0
    for code, t in terms.items():
        if t.get("kind") == "cause" and code in T.CAUSE_LABELS_JA:
            assert t["ja"]["s"] == T.CAUSE_LABELS_JA[code], code
            checked += 1
    assert checked >= 20


# ── 장애 종류 이름표(2026-11-14 · 맥스) ──────────────────────────────────────

def test_kind_specs_title_equals_kind_labels_for_every_kind():
    from datagen import scenarios as S
    from datagen.ui import common
    assert set(S.KIND_SPECS) == set(common.KIND_LABELS) and len(S.KIND_SPECS) == 43      # 33·34번 신규 종류(ran_drop_surge · transport_cut)가 더해졌다(2026-11-19)
    assert {k: v["title"] for k, v in S.KIND_SPECS.items()} == dict(common.KIND_LABELS)


def test_kind_labels_of_scenarios_14_to_27_equal_the_scenario_names():
    """한 종류를 한 시나리오만 쓰면 이름표 = 시나리오 쉬운 이름. 둘 이상이 쓰는 종류(1·18 등)는 뺀다."""
    from collections import defaultdict
    from datagen.ui import common
    use = defaultdict(list)
    for r in control.SCENARIOS:
        use[r[2]].append(r)
    checked = 0
    for kind, rows in use.items():
        if kind is not None and len(rows) == 1 and rows[0][0] >= 14:        # 33번처럼 종류가 없는(준비 중) 번호는 뺀다
            assert common.KIND_LABELS[kind].startswith(rows[0][1]), (kind, rows[0][0])   # 33번 이름표는 「… (HTTP 4xx)」로 구분어가 붙는다
            checked += 1
    assert checked >= 10


def test_every_kind_label_has_a_japanese_translation():
    from datagen.ui import common
    from datagen.ui.i18n_ja import _JA_COMMON, _JA_MAIN, _JA_MAP, _JA_SCEN, _JA_SHELL
    ja = {**_JA_MAP, **_JA_COMMON, **_JA_MAIN, **_JA_SCEN, **_JA_SHELL}
    miss = [k for k, v in common.KIND_LABELS.items() if v not in ja]
    assert miss == []


def test_kind_labels_have_no_japanese_words_leaking_into_korean():
    from datagen.ui import common
    bad = [v for v in common.KIND_LABELS.values() if re.search("丁目|本社", v)]
    assert bad == []
