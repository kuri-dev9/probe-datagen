"""15번 출퇴근 권장(`rush_hours`) · 25번 `select_geo` 와 `expect_geo` 분리 · 메시 칩 묶기 (2026-11-09 · 동구·케이 구현 고정).

* 15번은 출퇴근 시간대에만 서는 장애다 — 시나리오 선택 때 권장 시간대(07~09·17~19시, 초기값)와 가상 시각 버튼을 보인다. 안내일 뿐 막지 않는다.
* 25번 정답 지리는 POINT 인데 대상은 노선 구간(CORRIDOR)으로 고른다 — 고르는 모양(`select_geo`)과 정답 모양(`expect_geo`)을 나눴다.
* 지도 칩: 250m·500m·1km 메시 조각은 "체감 메시 · KPI" 한 칩이다(사용자 11-09 "응 묶어").
DB·네트워크 없음 — 페이지 스크립트는 `node` 로만 본다(없으면 건너뛴다).
"""
from __future__ import annotations

import json
import random
import re
import shutil
import subprocess
from dataclasses import fields
from pathlib import Path

import pytest

from datagen import faults, scenarios as S, ui
from test_disaster import _row, make  # noqa: F401  (make 는 픽스처)

NODE = shutil.which("node")
ROOT = Path(__file__).resolve().parent.parent


# ── 1. recommended_hours ─────────────────────────────────────────────────────

def test_ho_interruption_gets_the_rush_window_instead_of_measured_hours():
    h = S.recommended_hours("ho_interruption")
    assert h["rush"] == [[7, 9], [17, 19]] and h["clock"] == ["08:00", "18:00"]
    assert h["text"] == "출퇴근 시간대 07~09·17~19시 권장" and h["text_ja"] == "通勤時間帯 07〜09·17〜19時を推奨"


def test_other_kinds_keep_the_measured_hours():
    h = S.recommended_hours("mobility")
    assert "rush" not in h and "clock" not in h and "text" in h


def test_a_window_across_midnight_gets_a_midnight_clock(monkeypatch):
    spec = dict(S.KIND_SPECS["ho_interruption"], rush_hours=((23, 1),))
    monkeypatch.setitem(S.KIND_SPECS, "ho_interruption", spec)
    assert S.recommended_hours("ho_interruption")["clock"] == ["00:00"]


def test_unknown_kind_does_not_crash():
    assert "text" in S.recommended_hours("no_such_kind")


# ── 2. Scenario 필드 ─────────────────────────────────────────────────────────

def test_scenario_has_rush_hours_and_select_geo_with_empty_defaults():
    f = {x.name: x for x in fields(S.Scenario)}
    assert f["rush_hours"].default == () and f["select_geo"].default is None


def test_only_15_has_rush_hours_and_only_25_has_select_geo():
    assert [k for k, v in S.KIND_SPECS.items() if v.get("rush_hours")] == ["ho_interruption"]
    assert [k for k, v in S.KIND_SPECS.items() if v.get("select_geo")] == ["son_param_change"]
    sp = S.KIND_SPECS["son_param_change"]
    assert sp["expect_geo"] == "POINT" and sp["select_geo"] == "CORRIDOR"
    assert S.KIND_SPECS["ho_interruption"]["expect_geo"] == "CORRIDOR"


# ── 3. _route_target ─────────────────────────────────────────────────────────

def _seg(shape):
    seg = [object(), object(), object()]
    return seg, {"line": "L", "from": "a", "to": "b"}, shape


class _Cell:
    def __init__(self, k):
        self.cell_key = k


def _patch(monkeypatch, shape):
    cells = [_Cell("1-1"), _Cell("2-1"), _Cell("3-1")]
    monkeypatch.setattr(S, "route_segment", lambda routes, rng: (cells, {"line": "L"}))
    monkeypatch.setattr(S, "geo_shape", lambda seg: shape)


def test_25_picks_a_route_segment_when_the_segment_is_a_corridor(monkeypatch):
    _patch(monkeypatch, "CORRIDOR")
    t = faults._route_target(["L1"], random.Random(0), "son_param_change")
    assert t is not None and t.cell_keys == {"1-1", "2-1", "3-1"}


@pytest.mark.parametrize("shape", ["POINT", "CLUSTER"])
def test_25_falls_back_when_the_segment_is_not_a_corridor(monkeypatch, shape):
    _patch(monkeypatch, shape)
    assert faults._route_target(["L1"], random.Random(0), "son_param_change") is None   # select_geo 가 CORRIDOR 를 요구


def test_a_kind_with_only_expect_geo_still_checks_expect_geo(monkeypatch):
    _patch(monkeypatch, "POINT")
    assert faults._route_target(["L1"], random.Random(0), "ho_interruption") is None      # expect_geo CORRIDOR
    _patch(monkeypatch, "CORRIDOR")
    assert faults._route_target(["L1"], random.Random(0), "ho_interruption") is not None


# ── 4. 정답지 ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("kind,want", [("son_param_change", "POINT"), ("ho_interruption", "CORRIDOR")])
def test_truth_expect_geo(make, kind, want):
    g = make()
    scs, bad = g.set_fault_rows([_row(kind=kind, dur=15)])
    assert scs and not bad
    truth = scs[0].as_truth()
    assert truth["expect_geo_static"] == want                    # 고정값은 그대로 남는다(2026-11-11 사용자 결정 B)
    assert truth["expect_geo"] == (scs[0].__dict__.get("_geo_shape") or want)       # 고른 셀로 잰 모양이 있으면 그것


# ── 5. UI ────────────────────────────────────────────────────────────────────

def test_the_scenarios_api_sends_clock_for_15(monkeypatch):
    rows = [{"kind": "ho_interruption"}, {"kind": "mobility"}]
    monkeypatch.setattr(ui, "_connect", lambda: type("C", (), {"close": lambda s: None})())
    monkeypatch.setattr(ui, "_ensure_once", lambda c: None)
    monkeypatch.setattr(ui, "list_scenarios", lambda c: rows)
    out = ui.api_scenarios()
    by = {r["kind"]: r for r in out}
    assert by["ho_interruption"]["dur"]["hours"]["clock"] == ["08:00", "18:00"]
    assert "clock" not in by["mobility"]["dur"]["hours"]


def test_inject_page_has_the_rush_widgets_and_japanese_phrases():
    h = ui._HTML
    for needle in ('id="dur-rush"', "function renderRush", "async function rushClock", "renderRush(h)",
                   "renderRush((curDur() || {}).hours)"):
        assert needle in h, needle
    ja = ui._JA if hasattr(ui, "_JA") else None
    if ja is not None:
        for k in ("출퇴근 시간대 권장", "가상 {t}"):
            assert k in ja, k


@pytest.mark.skipif(not NODE, reason="node 가 없다")
@pytest.mark.parametrize("page", ["_HTML", "_HTML_MAP", "_HTML_SCEN", "_HTML_TOPO"])
def test_every_page_script_parses(page, tmp_path):
    html = getattr(ui, page)
    js = "\n".join(re.findall(r"<script>(.*?)</script>", html, re.S))
    f = tmp_path / "p.js"
    f.write_text(js, encoding="utf-8")
    r = subprocess.run([NODE, "--check", str(f)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr[-500:]


# ── 6. 시나리오 현황 문서 ────────────────────────────────────────────────────

def _report():
    return json.loads((ROOT / "config/scenario_report.json").read_text(encoding="utf-8"))["scenarios"]


def test_report_15_has_the_rush_param_last_and_25_expects_point():
    s15 = next(x for x in _report() if x["no"] == 15)
    assert "권장 시작 시간대" in json.dumps(s15["setup"]["params"][-1], ensure_ascii=False)
    s25 = next(x for x in _report() if x["no"] == 25)
    assert s25["expect"]["geo"] == "POINT"
    assert "방향 맞춘 띠 15개" not in json.dumps(s25["setup"], ensure_ascii=False)


# ── 7. 지도 칩 묶기 ─────────────────────────────────────────────────────────

def _mesh_js():
    src = ui._HTML_MAP
    a = src.index("const MESH_AXES")
    b = src.index("function mergeParts")
    return src[a:b]


@pytest.mark.skipif(not NODE, reason="node 가 없다")
def test_mesh_pieces_of_every_size_share_one_chip_and_other_axes_do_not():
    js = ("const T=s=>s, PART_NAME={}, AXIS_NAME={}, GEO_NAME={}, evtOf=h=>h.ev;" + _mesh_js() + """
const m=(axis,k)=>({axis, trigger_kpi:k});
const h={ev:{members:[m('MESH250','Q-02'),m('MESH500','Q-02'),m('MESH1K','Q-02'),m('MESH250','Q-11'),m('APN','Q-02')]}};
console.log(JSON.stringify([partGroups(h).map(g=>[g.label,g.idx]), partLabel(m('GLOBAL','C-02'),{egress:false})]));
""")
    r = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=20)
    assert r.returncode == 0, r.stderr[-600:]
    groups, glob = json.loads(r.stdout)
    assert groups[0] == ["체감 메시 · Q-02", [0, 1, 2]]                       # 세 크기가 한 칩
    assert ["체감 메시 · Q-11", [3]] in groups                                 # KPI 가 다르면 따로
    assert not any(g[0].startswith("체감 메시") and 4 in g[1] for g in groups)  # 다른 축은 안 섞인다
    assert glob.endswith(" · C-02")


def test_the_expanded_line_prefixes_the_original_mesh_size():
    src = ui._HTML_MAP
    assert "MESH_AXES.has(m.axis)?esc(GEO_NAME[m.axis])+' '" in src
