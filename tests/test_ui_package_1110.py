"""gen-ui 패키지 분리(2026-11-10) — 옛 `ui.X` 이름 바꿔 끼우기가 실제로 부르는 모듈에 닿는지 · 화면·정적 파일 · 진입점."""
from __future__ import annotations

import importlib

import pytest

from datagen import ui
from datagen.ui import common, pages, scenarios_api


def test_patching_a_name_on_the_package_reaches_the_module_that_uses_it(monkeypatch, tmp_path):
    f = tmp_path / "r.json"
    f.write_text('{"scenarios": []}', encoding="utf-8")
    monkeypatch.setattr(ui, "SCENARIO_REPORT", f)
    assert scenarios_api.SCENARIO_REPORT == f
    assert ui.api_scenario_report(15)["report"] == {"scenarios": []}


def test_patching_connect_reaches_common_and_every_module_that_has_it(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(ui, "_connect", sentinel)
    assert common._connect is sentinel
    for name in ("status_api", "faults_api", "map_api", "topo_api", "scenarios_api"):
        m = importlib.import_module(f"datagen.ui.{name}")
        if hasattr(m, "_connect"):
            assert m._connect is sentinel, name


def test_monkeypatch_is_undone_after_the_test():
    assert ui._connect is common._connect and callable(ui._connect)


@pytest.mark.parametrize("name", ["_HTML", "_HTML_MAP", "_HTML_TOPO", "_HTML_SCEN", "_SHELL_CSS", "_SHELL_JS", "_FAULT_MAP_JS"])
def test_page_strings_are_still_module_attributes(name):
    v = getattr(ui, name)
    assert isinstance(v, str) and len(v) > 100


def test_static_files_exist_and_are_packaged():
    from pathlib import Path
    d = Path(ui.pages.__file__).parent / "static"
    for n in ("main.html", "map.html", "topo.html", "scenarios.html", "shell.css", "shell.js", "i18n.js", "fault_map.js"):
        assert (d / n).is_file(), n
    pp = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    assert "datagen.ui" in pp and "static/*" in pp


def test_entry_points_import():
    from datagen.cli import main as _cli  # noqa: F401
    assert callable(ui.main) and ui.app is not None
    from pathlib import Path
    m = (Path(ui.pages.__file__).parent / "__main__.py").read_text(encoding="utf-8")
    assert "main" in m, "python -m datagen.ui 진입점"


def test_the_four_pages_are_served():
    """httpx 가 없어 TestClient 를 쓰지 않는다 — 화면 함수가 내는 본문과 API 경로를 직접 본다."""
    assert "/api/scenario-report" in ui.app.openapi()["paths"]
    for name in ("_HTML", "_HTML_MAP", "_HTML_TOPO", "_HTML_SCEN"):
        assert "<html" in getattr(ui, name).lower() or "<!doctype" in getattr(ui, name).lower(), name


# ── base.css — 주입·토폴로지 화면만 `<style>` 맨 앞에 싣는다 (2026-11-10) ─────────

def _style_head(html: str) -> str:
    a = html.index("<style>")
    return html[a + len("<style>"):].lstrip()


def _base_css() -> str:
    from pathlib import Path
    return (Path(ui.__file__).parent / "static" / "base.css").read_text(encoding="utf-8")


@pytest.mark.parametrize("name", ["_HTML", "_HTML_TOPO"])
def test_base_css_leads_the_style_of_main_and_topo(name):
    assert _style_head(getattr(ui, name)).startswith(_base_css().lstrip())


@pytest.mark.parametrize("name", ["_HTML_MAP", "_HTML_SCEN"])
def test_base_css_is_not_loaded_by_map_and_scenarios(name):
    html = getattr(ui, name)
    assert _base_css().strip() not in html
    assert not _style_head(html).startswith(_base_css().lstrip()[:60])


def test_base_css_is_carried_once_in_each_page():
    for name in ("_HTML", "_HTML_TOPO"):
        assert getattr(ui, name).count(_base_css().strip()) == 1
