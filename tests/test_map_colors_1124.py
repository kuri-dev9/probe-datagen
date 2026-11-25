"""지도 색 — 지명 안·밖 구분과 강·바다 같은 색 (2026-11-24 · 동구·케이 구현 · 에스터 시험).

사용자 지시: 외각 지명은 지금보다 어둡게, 내부 지명은 지금 색, 역명만 녹색. 강과 바다는 같은 색.
역 표시 모양은 다시 바뀔 예정이라 여기서 보지 않는다.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

MAP_HTML = Path(__file__).resolve().parents[1] / "src" / "datagen" / "ui" / "static" / "map.html"
HTML = MAP_HTML.read_text(encoding="utf-8")


def _theme(selector_re: str) -> str:
    m = re.search(selector_re + r"\s*\{(.*?)\n \}", HTML, re.S)
    assert m, selector_re
    return m.group(1)


DARK = _theme(r"body,body\[data-theme=dark\]")
LIGHT = _theme(r"body\[data-theme=light\]")


def _var(block: str, name: str) -> str:
    m = re.search(rf"{re.escape(name)}\s*:\s*([^;]+);", block)
    assert m, name
    return m.group(1).strip()


def _lum(hexv: str) -> float:
    h = hexv.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


# ── 지명 안·밖 ───────────────────────────────────────────────────────────

def test_outer_label_color_exists_in_both_themes():
    for blk in (DARK, LIGHT):
        assert re.fullmatch(r"#[0-9a-fA-F]{6}", _var(blk, "--lb-o"))


def test_dark_theme_outer_is_darker_than_inner():
    assert _lum(_var(DARK, "--lb-o")) < _lum(_var(DARK, "--lb-a"))


def test_light_theme_outer_is_lighter_than_inner():
    # 밝은 화면에서는 어둡게가 아니라 배경 쪽으로 물러나게 — 더 옅은 회색이다
    assert _lum(_var(LIGHT, "--lb-o")) > _lum(_var(LIGHT, "--lb-a"))


def test_inner_label_color_is_unchanged():
    # 내부 지명은 「지금 색 그대로」 — 값을 바꾸면 사용자가 정한 기준이 흔들린다
    assert _var(DARK, "--lb-a") == "#7d8fa6"
    assert _var(LIGHT, "--lb-a") == "#6b7787"


def test_station_name_stays_green_and_differs_from_place_names():
    for blk in (DARK, LIGHT):
        h = _var(blk, "--lb-s").lstrip("#")
        r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
        assert g > r and g > b          # 녹색 계열
    assert re.search(r"\.lb-s\{[^}]*color:var\(--lb-s\)", HTML)


def test_lb_o_rule_only_changes_color_and_follows_lb_a():
    assert re.search(r"\.lb-o\{color:var\(--lb-o\)\}", HTML)
    a = HTML.index(".lb-a{"); b = HTML.index(".lb-o{")
    assert a < b      # 뒤에 와야 같은 요소(`lb-a lb-o`)에서 색만 덮는다


def test_labels_are_split_by_cells_gu_not_by_a_new_computation():
    assert "const inGu=new Set((D.cells||[]).map(c=>String(c.gu)))" in HTML
    assert re.search(r"cls:inGu\.has\(String\(f\.code\)\)\?'lb-a':'lb-a lb-o'", HTML)


# ── 강·바다 ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("blk", [DARK, LIGHT], ids=["dark", "light"])
def test_river_and_water_are_the_sea_color(blk):
    assert _var(blk, "--river") == "var(--sea)"
    assert _var(blk, "--water") == "var(--sea)"


def test_river_line_is_opaque_so_it_does_not_look_different_from_the_sea():
    m = re.search(r"\.rv\{[^}]*\}", HTML)
    assert m and "stroke:var(--river)" in m.group(0)
    assert re.search(r"stroke-opacity:\s*1\b", m.group(0))


def test_water_polygon_uses_the_water_variable():
    assert re.search(r"\.gr-water\{fill:var\(--water\)\}", HTML)


def test_map_background_is_the_sea_color():
    assert re.search(r"background:var\(--sea\)", HTML)
