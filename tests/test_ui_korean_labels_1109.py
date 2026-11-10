"""한국어 화면에 일본어 낱말(本社·丁目)이 새지 않게 한다 (2026-11-09).

지명이 아닌 낱말은 한국어(본사·정목)로 쓰고 일본어는 번역표(`_JA_MAP`)가 맡는다. 소스 원문과 번역표 키가
한 글자라도 어긋나면 일본어 화면에 한국어가 그대로 나오므로 둘을 맞춰 본다.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from datagen import ui

from ui_src import ui_source  # noqa: E402

SRC = ui_source()
PAGES = {"main": "_HTML", "map": "_HTML_MAP", "scen": "_HTML_SCEN", "topo": "_HTML_TOPO"}
NODE = shutil.which("node")

HQ_TITLE = ("NTT docomo 본사 (千代田区永田町2-11-1)\nPGW 16대를 한 점으로 — 실제 PGW 위치가 아니다\n"
            "누르면 SGW 에서 모이는 선이 나온다")
KEYS = {
    HQ_TITLE: "NTT docomo 本社 (千代田区永田町2-11-1)\nPGW 16台を1点に集約 — 実際のPGW位置ではない\nクリックするとSGWから集まる線が出る",
    "본사": "本社",
    "정목": "丁目",
    "APN · docomo 본사": "APN · docomo 本社",
    "외부(APP·PAY) · docomo 본사": "外部(APP·PAY) · docomo 本社",
    "정목 경계 없음 — 중심점 근사": "丁目境界なし — 中心点で近似",
    "NTT docomo 본사 · PGW (대표)": "NTT docomo 本社 · PGW (代表)",
    "망 밖 · docomo 본사": "網外 · docomo 本社",
}


def ja_map_src() -> str:
    a = SRC.index("_JA_MAP = {")
    return SRC[a:SRC.index("\n}\n", a)]


def test_ja_map_is_a_dict_with_new_keys():
    for k, v in KEYS.items():
        assert ui._JA_MAP.get(k) == v, k


@pytest.mark.parametrize("key", ["본사", "정목"])
def test_bare_keys_appear_once_in_the_map_source(key):
    # 딕셔너리 리터럴은 중복 키를 조용히 덮는다 — 소스에서 센다
    n = len(re.findall(r'(?m)^\s*"%s":|[{,]\s*"%s":' % (key, key), ja_map_src()))
    assert n == 1


def test_no_japanese_word_keys_left_in_the_map():
    for k in ui._JA_MAP:
        assert "本社" not in k and "丁目" not in k, k


def test_japanese_values_are_kept():
    assert "本社" in ui._JA_MAP["본사"] and ui._JA_MAP["정목"] == "丁目"


def _page_text(name):
    return getattr(ui, PAGES[name])


def test_hq_title_and_cap_are_korean_and_match_key():
    t = ui._HTML_MAP
    m = re.search(r'<div id=hq title="([^"]*)"', t)
    assert m and m.group(1).replace("&#10;", "\n") == HQ_TITLE
    assert "<div class=cap><b>NTT docomo</b><br>본사</div>" in t
    assert "<br>本社" not in t


def test_source_strings_match_keys():
    for lit in ["T('APN · docomo 본사')", "T('외부(APP·PAY) · docomo 본사')", "T('정목 경계 없음 — 중심점 근사')",
                "T('NTT docomo 본사 · PGW (대표)')", "ADM3:T('정목')"]:
        assert lit in SRC, lit
        assert lit[lit.index("'") + 1:lit.rindex("'")] in ui._JA_MAP if lit.startswith("T(") else True


def _code_lines():
    """주석·번역 값·ja 분기를 뺀 줄 — 한국어 화면에 나올 수 있는 줄만."""
    out = []
    for i, ln in enumerate(SRC.splitlines(), 1):
        s = ln.strip()
        if s.startswith(("#", "//", "/*", "*")) or "LANG==='ja'" in ln:
            continue
        out.append((i, ln))
    return out


def test_no_hon_sha_or_chome_in_korean_screen_strings():
    a = SRC.index("_JA_MAP = {")
    ja_start = SRC[:a].count("\n") + 1
    bad = []
    for i, ln in _code_lines():
        if i >= ja_start and re.search(r'^\s*"[^"]*":\s*$|^\s*"[^"]*":\s*"|^\s+"', ln):
            continue  # 번역표(키·값)
        if re.search(r"本社|丁目", ln):
            if re.search(r'"""|丁目\)|丁目 코드|丁目 을', ln):  # docstring
                continue
            bad.append((i, ln.strip()[:100]))
    assert bad == []


@pytest.mark.skipif(NODE is None, reason="node 없음")
@pytest.mark.parametrize("name", list(PAGES))
def test_node_check_pages(name, tmp_path):
    scripts = re.findall(r"<script[^>]*>(.*?)</script>", _page_text(name), re.S)
    assert scripts
    for i, s in enumerate(scripts):
        p = tmp_path / f"{name}{i}.js"
        p.write_text(s, encoding="utf-8")
        r = subprocess.run([NODE, "--check", str(p)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr[:300]
