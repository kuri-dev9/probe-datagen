"""지도 — OSM 역 모양(승강장·구내·덩어리·건물), 역 글자 자리, 이름 겹침, 휠 한 칸 잠금 (2026-11-24 · 맥스·동구 구현 · 에스터 시험).

사용자 지시: 역 모양이 「선만 그은 것」이면 안 된다 / 역사는 승강장을 묶어 조금 키워서 / 역 이름은 진하게 · 지명은 흐리게 /
같은 역이 두 이름으로 찍히면 안 된다 / 휠은 첫 신호 하나만 받고 0.5초 잠근다. node 가 없으면 동작 시험은 건너뛴다.
"""
from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from datagen.ui import map_api
from datagen.ui.i18n_ja import _JA_MAP

ROOT = Path(__file__).resolve().parents[1]
GEO = ROOT.parent / "graphio-probe-rca" / "data" / "geo" / "jp"
OSM, LABEL = GEO / "station_osm.json", GEO / "station_label.json"
HTML = (ROOT / "src" / "datagen" / "ui" / "static" / "map.html").read_text(encoding="utf-8")
NODE = shutil.which("node")
needs_geo = pytest.mark.skipif(not (OSM.exists() and LABEL.exists()), reason="역 json 이 구워져 있지 않다")


def _ymt_names():
    m = re.search(r"const RP_YMT_JA=\{([^}]*)\}", HTML)
    return re.findall(r"JY\d\d:'([^']+)'", m.group(1))


def _m(a, b):
    return math.hypot((a[0] - b[0]) * 111320 * math.cos(math.radians(35.68)), (a[1] - b[1]) * 110540)


def _area(ring):
    return abs(sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(ring, ring[1:]))) / 2 * 111320 * math.cos(math.radians(35.68)) * 110540


@pytest.fixture(scope="module")
def osm():
    return json.loads(OSM.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def labels():
    return json.loads(LABEL.read_text(encoding="utf-8"))


# ── 데이터: OSM 역 모양 ───────────────────────────────────────────────────

@needs_geo
def test_osm_kinds_are_exactly_five(osm):
    assert {x["kind"] for x in osm} == {"platform", "area", "band", "bldg", "n02"}      # `hull` 은 11-24 저녁 노선 띠(`band`)로 바뀌었다


@needs_geo
def test_osm_polygons_are_closed_rings_and_n02_fallbacks_have_segs(osm):
    for x in osm:
        if x["kind"] == "n02":
            assert x["segs"] and x["name"], x
        else:
            r = x["ring"]
            assert len(r) >= 4 and r[0] == r[-1], x["kind"]


@needs_geo
def test_bands_are_named_strips_and_the_old_hulls_are_gone(osm):
    assert not [x for x in osm if x["kind"] == "hull"]                       # 덩어리는 노선을 따라가는 띠로 바뀌었다(사용자 11-24 저녁 「라인을 따라」)
    bands = [x for x in osm if x["kind"] == "band"]
    assert len(bands) > 500 and all(x["name"] for x in bands)
    assert min(_area(x["ring"]) for x in bands) > 100                         # 점처럼 찌그러진 띠가 없다


@needs_geo
def test_band_ends_are_right_angles(osm):
    """띠의 양 끝은 직각이다(사용자 「끝을 직각으로」 → 노선 띠에서도 같다) — 거의 모든 띠에 직각 모서리가 4개 이상(양 끝 × 2)."""
    k = math.cos(math.radians(35.68))
    good = tot = 0
    for x in (x for x in osm if x["kind"] == "band"):
        r = x["ring"][:-1]
        n = len(r)
        c = 0
        for i in range(n):
            a, b, d = r[i - 1], r[i], r[(i + 1) % n]
            v1, v2 = ((a[0] - b[0]) * k, a[1] - b[1]), ((d[0] - b[0]) * k, d[1] - b[1])
            ln = math.hypot(*v1) * math.hypot(*v2)
            if ln:
                c += abs(math.degrees(math.acos(max(-1, min(1, (v1[0] * v2[0] + v1[1] * v2[1]) / ln)))) - 90) <= 3
        tot += 1
        good += c >= 4
    assert tot > 500 and good / tot >= 0.99, (good, tot)                      # 지금 99.8%


@needs_geo
def test_band_exists_for_all_30_yamanote_stations(osm):
    bands = {x["name"] for x in osm if x["kind"] == "band"}
    names = _ymt_names()
    assert len(names) == 30
    assert [n for n in names if n not in bands] == []                          # 有楽町도 N02 승강장 선을 따라 띠를 만든다


@needs_geo
def test_every_n02_fallback_station_also_has_a_band(osm):
    bands = {x["name"] for x in osm if x["kind"] == "band"}
    assert [x["name"] for x in osm if x["kind"] == "n02" and x["name"] not in bands] == []


@needs_geo
def test_tokyo_and_shinjuku_bands_add_up_to_a_station_sized_shape(osm):
    for n in ("東京", "新宿"):
        assert sum(_area(x["ring"]) for x in osm if x["kind"] == "band" and x["name"] == n) > 80_000, n     # 지금 16만 · 15만 m²


@needs_geo
def test_no_station_area_is_left_over_a_station_that_has_a_hull(osm):
    # 덩어리가 구내 영역을 대신한다 — 같은 자리를 두 번 칠하지 않는다(야마노테 30역 기준)
    areas = {x["name"] for x in osm if x["kind"] == "area"}
    assert not (areas & set(_ymt_names()))


@needs_geo
def test_every_yamanote_station_has_platform_polygons_near_the_track(osm):
    t = json.loads((ROOT / "data" / "tracks" / "yamanote_track.json").read_text(encoding="utf-8"))
    ring, step = t["ring"], t["step_m"]
    plats = [x for x in osm if x["kind"] == "platform"]
    for s in t["stations"]:
        p = ring[int(s["pos_m"] // step) % len(ring)]
        d = min(min(_m(v, p) for v in x["ring"]) for x in plats)
        assert d < 300, (s["id"], d)       # 가장 가까운 승강장 면까지 — 有楽町(228m)이 가장 멀다


# ── 데이터: 글자 자리 ───────────────────────────────────────────────────────

@needs_geo
def test_label_file_has_all_30_yamanote_stations_within_150m_of_the_track(labels):
    t = json.loads((ROOT / "data" / "tracks" / "yamanote_track.json").read_text(encoding="utf-8"))
    ring, step = t["ring"], t["step_m"]
    by = {}
    for lb in labels:
        by.setdefault(lb["name"], []).append(lb)
    for n, s in zip(_ymt_names(), t["stations"]):
        p = ring[int(s["pos_m"] // step) % len(ring)]
        d = min(_m((lb["lon"], lb["lat"]), p) for lb in by[n])
        assert d <= 150, (n, d)             # 맥스 실측 최대 97m(池袋)


def _cfg(monkeypatch):
    monkeypatch.setattr(map_api, "_pem_cfg", lambda: {"geo_station_label": str(LABEL)})


@needs_geo
def test_station_labels_attach_at_and_keep_lat_lon(monkeypatch):
    _cfg(monkeypatch)
    sts = [{"name": "東京", "lat": 35.6700, "lon": 139.7670, "cells": 12}]
    out = map_api._station_labels(sts)
    tokyo = next(s for s in out if s["name"] == "東京" and not s.get("label_only"))
    assert tokyo["lat"] == 35.6700 and tokyo["lon"] == 139.7670 and tokyo["cells"] == 12   # 셀 중심은 그대로
    assert tokyo["at"] and _m(tokyo["at"], (139.7670, 35.6700)) < 1500
    assert sts[0].get("at") is None                                                          # 받은 목록을 건드리지 않는다


@needs_geo
def test_six_label_only_yamanote_stations_are_added_without_cells(monkeypatch):
    _cfg(monkeypatch)
    out = map_api._station_labels([{"name": "東京", "lat": 35.67, "lon": 139.767, "cells": 5}])
    only = {s["name"] for s in out if s.get("label_only")}
    assert {"駒込", "巣鴨", "大塚", "目白", "新大久保", "原宿"} <= only
    for s in out:
        if s.get("label_only"):
            assert s["cells"] == 0 and s["at"]


@needs_geo
def test_label_only_does_not_duplicate_a_station_already_in_the_list(monkeypatch):
    _cfg(monkeypatch)
    out = map_api._station_labels([{"name": "巣鴨", "lat": 35.733, "lon": 139.739, "cells": 3}])
    assert sum(1 for s in out if s["name"] == "巣鴨") == 1
    assert not next(s for s in out if s["name"] == "巣鴨").get("label_only")


def test_station_labels_missing_file_returns_the_list_unchanged(monkeypatch):
    monkeypatch.setattr(map_api, "_pem_cfg", lambda: {"geo_station_label": "/nonexistent/l.json"})
    sts = [{"name": "東京", "lat": 1, "lon": 2, "cells": 3}]
    assert map_api._station_labels(sts) == sts


# ── 화면: 그리는 순서·옅기·숨김 ────────────────────────────────────────────

def test_stn_draws_only_the_band_by_default_and_the_other_kinds_are_the_way_back():
    m = re.search(r"const ORDER=\[([^\]]*)\]", HTML)
    assert re.findall(r"'([^']+)'", m.group(1)) == ["stk stk-band", "stb", "stn"]     # 노선을 따라가는 띠 하나만(사용자 11-24 저녁)
    for gone in ("'stk stk-hull'", "'stk stk-area'", "'stk stk-platform'", "'stk stk-bldg'", "'stn stn-fb'"):
        assert gone not in m.group(1)


def test_band_is_an_opaque_gray_that_is_brighter_than_land_on_dark_and_darker_on_light():
    assert re.search(r"\.stk-band\{fill:var\(--stbd\);fill-opacity:1\}", HTML)             # 투명도 없는 회색(사용자 「셀 아래에 깐다」)
    dark = re.search(r"--stbd:(#[0-9a-fA-F]{6})", HTML.split("body[data-theme=light]")[0]).group(1)
    light = re.search(r"--stbd:(#[0-9a-fA-F]{6})", HTML.split("body[data-theme=light]")[1]).group(1)
    lum = lambda h: sum(int(h[i:i + 2], 16) for i in (1, 3, 5)) / 3
    land_dark = re.search(r"--land:(#[0-9a-fA-F]{6})", HTML).group(1)
    land_light = re.search(r"body\[data-theme=light\]\{[^}]*--land:(#[0-9a-fA-F]{6})", HTML, re.S).group(1)
    assert lum(dark) > lum(land_dark) and lum(light) < lum(land_light)
    # 되돌릴 때를 위해 나머지 종류의 CSS 는 남아 있다 — 옅기 순서는 그대로
    op = {k: float(re.search(rf"\.stk-{k}\{{fill-opacity:([\d.]+)\}}", HTML).group(1)) for k in ("area", "platform", "bldg")}
    assert op["area"] < op["platform"] < op["bldg"]


def test_n02_fallback_line_is_faint_and_stn_has_no_pointer_or_nonscaling():
    assert re.search(r"\.stn\.stn-fb\{stroke-opacity:\.28\}", HTML)
    stn = re.search(r"\n \.stn\{[^}]*\}", HTML).group(0)
    assert "pointer-events:none" in stn and "non-scaling-stroke" not in stn


def test_stn_hides_at_1500m_with_display_none_not_opacity():
    assert re.search(r"const STN_HIDE_M=1500;", HTML)
    vis = HTML[HTML.index("function stnVis"):HTML.index("function stnBuild")]
    assert "style.display" in vis and "'none'" in vis and "opacity" not in vis
    assert "scaleNiceM(mppOf(VB))<STN_HIDE_M" in HTML          # 열차와 같은 축척 계산


def test_stn_build_groups_by_kind_and_grid_cell_and_reads_only_stbl():
    b = HTML[HTML.index("function stnBuild"):HTML.index("function stnTip")]
    assert "BASE_CELL" in b and "by.get(cls)" in b             # 종류 × 격자 칸마다 path 하나
    assert "for(const b of STBL)" in b
    assert "createElementNS" not in b.split("for(const cls of ORDER)")[0]   # 항목마다 요소를 만들지 않는다(옛 건물 윤곽 `stb` 한 줄 제외)


def test_stn_tip_returns_empty_when_hidden_and_uses_only_named_rings():
    i = HTML.index("function stnTip")
    tip = HTML[i:HTML.index("function draw(){", i)]
    assert "!stnShown()" in tip and "return ''" in tip
    b = HTML[HTML.index("function stnBuild"):HTML.index("function stnTip")]
    assert "if(b.name) for(const p of b.ring) STN_PTS.push" in b      # 이름 없는 승강장 면이 많다
    assert "stnTip(e)" in HTML


# ── 화면: 역 이름 · 지명 색 ──────────────────────────────────────────────────

def test_station_name_is_bold_enough_to_read_but_not_heavy_and_opaque():
    css = re.search(r"\n \.lb-s\{[^}]*\}", HTML).group(0)
    assert "font-weight:500" in css and "opacity:1" in css      # 사용자 「진하게」 → 700 → 「두께 줄여」 500
    assert "--lb-s:#a6e04f" in HTML                              # 어두운 화면: 역사 면(연녹색)과 갈리는 짙은 녹색


# ── 화면: 이름 겹침 (node 로 거르기 코드를 그대로 돌린다) ───────────────────────

def _dedupe_js():
    a = HTML.index("const LB_DUP_M=400;")
    b = HTML.index("    for(const st of SL){", a)
    c = HTML.index("const AREA_DUP_M=400;")
    d = HTML.index("    for(const f of GEO.features){", c)
    return HTML[a:b], HTML[c:d]


def _run(tmp_path, stations, areas):
    s1, s2 = _dedupe_js()
    js = f"""
const JA={json.dumps({"도쿄": "東京", "신주쿠": "新宿", "간다": "神田", "고탄다": "五反田"}, ensure_ascii=False)};
const D={{stations:{json.dumps(stations, ensure_ascii=False)}}};
const stnVis=()=>{{}};
{s1}
}}
{s2}
const gone=D.stations.filter(s=>stDup.has(s)).map(s=>s.name+(s.at?'@':''));
const kept=D.stations.filter(s=>!stDup.has(s)).map(s=>s.name+(s.at?'@':''));
const areaGone={json.dumps(areas, ensure_ascii=False)}.filter(f=>areaDup(f)).map(f=>f.name);
console.log(JSON.stringify({{gone,kept,areaGone}}));
"""
    f = tmp_path / "t.js"
    f.write_text(js, encoding="utf-8")
    r = subprocess.run([NODE, str(f)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


needs_node = pytest.mark.skipif(NODE is None, reason="node 없음")


@needs_node
def test_same_station_in_korean_and_japanese_keeps_only_the_one_with_at(tmp_path):
    st = [{"name": "신주쿠", "lon": 139.7000, "lat": 35.6900},                       # 노선 자료(한글 · 셀 중심 · at 없음)
          {"name": "新宿", "lon": 139.7005, "lat": 35.6905, "at": [139.7003, 35.6902]},   # N02·OSM(원문 · 실제 역 가운데)
          {"name": "東京", "lon": 139.7671, "lat": 35.6812, "at": [139.7671, 35.6812]}]
    r = _run(tmp_path, st, [])
    assert r["gone"] == ["신주쿠"] and sorted(r["kept"]) == ["新宿@", "東京@"]


@needs_node
def test_order_of_the_two_does_not_matter(tmp_path):
    st = [{"name": "新宿", "lon": 139.7005, "lat": 35.6905, "at": [139.7003, 35.6902]},
          {"name": "신주쿠", "lon": 139.7000, "lat": 35.6900}]
    assert _run(tmp_path, st, [])["gone"] == ["신주쿠"]


@needs_node
def test_two_different_stations_far_apart_are_both_kept(tmp_path):
    st = [{"name": "新宿", "lon": 139.70, "lat": 35.69, "at": [139.70, 35.69]},
          {"name": "신주쿠", "lon": 139.80, "lat": 35.69}]          # 이름은 같아도 9km 떨어졌다 — 다른 역
    assert _run(tmp_path, st, [])["gone"] == []


@needs_node
def test_japanese_translation_key_matches_even_when_names_differ_in_script(tmp_path):
    st = [{"name": "간다", "lon": 139.771, "lat": 35.692},
          {"name": "神田", "lon": 139.771, "lat": 35.692, "at": [139.7712, 35.6921]}]
    assert _run(tmp_path, st, [])["gone"] == ["간다"]


@needs_node
def test_area_label_with_the_same_name_as_a_nearby_station_is_dropped_others_stay(tmp_path):
    st = [{"name": "新宿", "lon": 139.70, "lat": 35.69, "at": [139.70, 35.69]}]
    areas = [{"name": "新宿", "at": [139.7004, 35.6903]},      # 같은 이름이 400m 안 — 뺀다
             {"name": "千代田区", "at": [139.7004, 35.6903]},   # 이름이 다르면 남는다
             {"name": "新宿", "at": [139.90, 35.90]}]          # 같은 이름이라도 멀면 남는다
    r = _run(tmp_path, st, areas)
    assert r["areaGone"] == ["新宿"]
    assert "千代田区" not in r["areaGone"]


def test_area_dup_is_applied_before_the_area_label_is_pushed():
    i = HTML.index("const AREA_DUP_M=400;")
    seg = HTML[i:HTML.index("cls:inGu.has", i)]
    assert seg.index("if(areaDup(f)) continue;") < seg.index("LABELS.push(")     # 판정만 있고 쓰이지 않으면 겹침이 그대로다


def test_dup_distances_are_constants_marked_as_initial_values():
    assert re.search(r"const LB_DUP_M=400;", HTML) and re.search(r"const AREA_DUP_M=400;", HTML)
    assert HTML.count("초기값이다") >= 2


# ── 번역 키 ────────────────────────────────────────────────────────────────

def test_ja_map_has_kanda_and_gotanda_with_the_right_keys():
    assert _JA_MAP["간다"] == "神田" and _JA_MAP["고탄다"] == "五反田"
    # 존댓말 일괄 변환이 역 이름 키를 바꾼 흔적 — 「갑니다」 키는 다른 문구의 「갑니다」까지 神田 으로 바꾼다
    assert "갑니다" not in _JA_MAP and "고탑니다" not in _JA_MAP


def test_ja_map_covers_all_30_yamanote_stations_in_korean():
    t = json.loads((ROOT / "data" / "tracks" / "yamanote_track.json").read_text(encoding="utf-8"))
    ja = dict(zip((s["id"] for s in t["stations"]), _ymt_names()))
    for s in t["stations"]:
        assert s["name"] in _JA_MAP, s["name"]
        assert _JA_MAP[s["name"]] == ja[s["id"]] or s["id"] in ("JY26",), (s["name"], _JA_MAP[s["name"]])


# ── 휠 ──────────────────────────────────────────────────────────────────────

def test_wheel_takes_one_signal_then_locks_500ms():
    assert re.search(r"const WHEEL_LOCK_MS=500;", HTML) and re.search(r"const WHEEL_MIN_DY=4;", HTML)
    h = HTML[HTML.index("wrap.addEventListener('wheel'"):HTML.index("window.addEventListener('resize'")]
    assert "wheelBusy()||performance.now()-WHEEL_AT<WHEEL_LOCK_MS) return" in h     # 진행 중이거나 잠금 안이면 버린다
    assert "Math.abs(dy)<WHEEL_MIN_DY) return" in h                                    # 떨림 한 번이 칸이 되지 않는다
    assert h.count("flyTo(") == 1                                                      # 신호 하나에 한 칸 — 쌓지 않는다
    assert "WHEEL_AT=performance.now()" in h


@pytest.mark.parametrize("gone", ["WHEEL_ARMED", "WHEEL_QUIET_MS", "WHEEL_NOTCH_MS", "WHEEL_STEP", "WHEEL_TGT", "wheelStep",
                                  "WHEEL_ACC", "WHEEL_ABS", "FLY_NEAR", "opt.near"])
def test_old_wheel_queue_and_gap_logic_is_gone(gone):
    assert gone not in HTML


def test_flyto_arrival_cleans_up_even_if_drawing_throws():
    f = HTML[HTML.index("function flyTo"):HTML.index("// 고른 장애를 선택 바에 적고")]
    tail = f[f.index("gestEnd();"):]
    assert "try{ const lod=LOD; setVB(v, {free:!!opt.free}); if(LOD===lod) draw(); }" in tail
    assert re.search(r"catch\(err\)\{ try\{ placeLabels\(\); \}catch\(_\)\{\} console\.error\(err\); \}", tail)   # 예외를 숨기지 않는다
    assert "finally{ if(opt.done) opt.done(); }" in tail         # 휠 잠금(WHEEL_RUN)을 반드시 푼다
    assert f.index("FLY=null") < f.index("gestEnd();") < f.index("setVB(v")     # 정리 → 제스처 끝 → 다시 그리기 순서


def test_wheel_lock_is_released_by_done_callback_only_for_its_own_flight():
    assert "done:()=>{ if(WHEEL_RUN===id) WHEEL_RUN=null; }" in HTML
    assert "const wheelBusy=()=>!!WHEEL_RUN&&FLY===WHEEL_RUN;" in HTML      # 다른 이동이 끼어들면 휠 잠금은 풀린다


# ── 역 이름 목록 확대 (map_api._station_labels) — 목록에 없는 역은 모두 글자만 더한다 ───────────────────

@needs_geo
def test_every_label_in_the_file_gets_a_station_entry_when_the_list_is_empty(monkeypatch, labels):
    _cfg(monkeypatch)
    out = map_api._station_labels([])
    assert out and all(s["label_only"] and s["cells"] == 0 and s["at"] == [s["lon"], s["lat"]] for s in out)
    # 같은 이름이 1.5km 안에 둘 있으면 한 역이다 — 이름·거리로 한 칸씩만 남는다
    by = {}
    for s in out:
        by.setdefault(s["name"], []).append((s["lon"], s["lat"]))
    for n, ps in by.items():
        for i in range(len(ps)):
            for j in range(i + 1, len(ps)):
                assert math.hypot((ps[i][0] - ps[j][0]) * 90400, (ps[i][1] - ps[j][1]) * 111000) > map_api._LABEL_MATCH_M, n
    # 파일의 모든 글자 자리는 출력의 같은 이름에서 1.5km 안에 있다(빠진 역이 없다)
    for lb in labels:
        assert any(math.hypot((lb["lon"] - x) * 90400, (lb["lat"] - y) * 111000) <= map_api._LABEL_MATCH_M for x, y in by[lb["name"]]), lb


def test_same_name_far_apart_stays_two_stations_and_same_name_close_is_one(monkeypatch, tmp_path):
    lf = tmp_path / "l.json"
    lf.write_text(json.dumps([
        {"name": "新町", "lon": 139.70, "lat": 35.60, "src": "osm", "n": 1, "lines": ["A線"]},
        {"name": "新町", "lon": 139.90, "lat": 35.80, "src": "osm", "n": 1, "lines": ["B線"]},        # 같은 이름·20km 밖 — 다른 역
        {"name": "新町", "lon": 139.7005, "lat": 35.6004, "src": "n02", "n": 1, "lines": ["A線"]}]),   # 같은 이름·60m — 같은 역
        encoding="utf-8")
    monkeypatch.setattr(map_api, "_pem_cfg", lambda: {"geo_station_label": str(lf)})
    out = map_api._station_labels([])
    assert [(s["lon"], s["lat"]) for s in out] == [(139.70, 35.60), (139.90, 35.80)]
    # 이미 목록에 있는 같은 이름·가까운 역은 더하지 않고, 멀리 있는 같은 이름은 더한다
    out2 = map_api._station_labels([{"name": "新町", "lat": 35.6001, "lon": 139.7001, "cells": 7}])
    assert [(s.get("label_only", False), s["cells"]) for s in out2] == [(False, 7), (True, 0)]
    assert out2[0]["at"] == [139.70, 35.60] and (out2[0]["lon"], out2[0]["lat"]) == (139.7001, 35.6001)


@needs_geo
def test_label_only_is_no_longer_limited_to_the_yamanote_line(monkeypatch, labels):
    _cfg(monkeypatch)
    out = map_api._station_labels([])
    non_ymt = [lb for lb in labels if "山手線" not in lb.get("lines", ())]
    assert non_ymt and len(out) > len([lb for lb in labels if "山手線" in lb.get("lines", ())]) + 100     # 江戸川·中野 같은 범위 밖 역도 들어간다
    assert any(s["name"] == "中野" for s in out)


# ── 글자 자리 옮기기 (`placeLabels`) — node 로 코드를 그대로 돌린다 ───────────────────────────────────

def _place(tmp_path, blockers, extra="", sc=500, tier=None):
    a = HTML.index("function placeLabels(){")
    b = HTML.index("\n}\n", a) + 3
    t = HTML.index("const ST_T2_M=2000, ST_T3_M=1000;")
    tier_js = "" if tier is None else f", tier:{tier}"
    js = f"""
const LABELS=[]; let GEST=false, RP=false, VB={{}};
const mppOf=()=>1, scaleNiceM=()=>{sc};
{HTML[t:HTML.index(chr(10), t)]}
const els=[]; const box={{replaceChildren:(...x)=>{{els.length=0; els.push(...x);}}}};
const rect={{left:0,top:0,width:1000,height:800}}, far={{left:-900,top:-900,right:-800,bottom:-800}};
const document={{getElementById:id=>id==='labels'?box:id==='wrap'?{{getBoundingClientRect:()=>rect}}:{{getBoundingClientRect:()=>far}},
  createElement:()=>({{style:{{}},className:'',textContent:''}})}};
const svg={{getScreenCTM:()=>({{a:1,d:1,e:0,f:0}})}};
const placeHq=()=>null, placeFx=()=>{{}};
{HTML[a:b]}
{extra}
for(const bl of {json.dumps(blockers)}) LABELS.push({{x:bl[0],y:bl[1],t:'AAAA',cls:'lb-n'}});
LABELS.push({{x:500,y:400,t:'東京',cls:'lb-s',dx:11{tier_js}}});
placeLabels();
const st=els.find(e=>e.className==='lb lb-s');
console.log(JSON.stringify(st?{{left:+parseFloat(st.style.left).toFixed(1),top:+parseFloat(st.style.top).toFixed(1)}}:null));
"""
    f = tmp_path / "p.js"
    f.write_text(js, encoding="utf-8")
    r = subprocess.run([NODE, str(f)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


RIGHT, LEFT, ABOVE, BELOW = [528, 400], [472, 400], [500, 380], [500, 419]


@needs_node
def test_station_name_goes_right_when_free(tmp_path):
    assert _place(tmp_path, []) == {"left": 516.0, "top": 400.0}


@needs_node
def test_station_name_moves_left_then_above_then_below_when_blocked_in_that_order(tmp_path):
    assert _place(tmp_path, [RIGHT]) == {"left": 460.4, "top": 400.0}
    assert _place(tmp_path, [RIGHT, LEFT]) == {"left": 488.2, "top": 381.0}
    assert _place(tmp_path, [RIGHT, LEFT, ABOVE]) == {"left": 488.2, "top": 419.0}


@needs_node
def test_station_name_is_dropped_only_when_all_four_places_are_blocked(tmp_path):
    assert _place(tmp_path, [RIGHT, LEFT, ABOVE, BELOW]) is None


@needs_node
def test_equipment_label_placed_first_keeps_its_place_over_the_station_name(tmp_path):
    # 장비 번호가 주인공이라는 기존 결정 — LABELS 순서가 우선순위다. 막힌 것은 역 이름이 비켜 간다(위 시험)
    assert _place(tmp_path, [RIGHT]) != {"left": 516.0, "top": 400.0}


@needs_node
@pytest.mark.parametrize("tier,sc,shown", [
    (1, 50000, True), (1, 5000, True),                          # 1등급(야마노테)은 늘 보인다
    (2, 2500, False), (2, 2000, True), (2, 1500, True),         # 2등급(환승역)은 `ST_T2_M`=2000 이하
    (3, 1500, False), (3, 1000, True), (3, 500, True),          # 3등급(나머지)은 `ST_T3_M`=1000 이하
    (None, 5000, True),                                          # 등급이 없는 글자(장비·지명 등)는 거르지 않는다
])
def test_station_name_tiers_show_by_scale(tmp_path, tier, sc, shown):
    got = _place(tmp_path, [], sc=sc, tier=tier)
    assert (got is not None) is shown, (tier, sc, got)


def test_tier_thresholds_are_initial_values_and_the_filter_is_in_place_labels_not_draw():
    assert re.search(r"const ST_T2_M=2000, ST_T3_M=1000;", HTML)
    assert "**초기값이다**" in HTML[HTML.index("const ST_T2_M") - 220:HTML.index("const ST_T2_M")]
    a = HTML.index("function placeLabels(){")
    body = HTML[a:HTML.index("\n}\n", a)]
    assert "L.tier===2&&sc>ST_T2_M || L.tier===3&&sc>ST_T3_M" in body            # 단계가 안 바뀌어도 축척 경계에서 바로 거른다


def test_station_tier_comes_from_the_api_and_the_screen_lifts_the_30_yamanote_stations_to_tier1():
    assert "tier:stYmt(st)?1:(st.tier||3)" in HTML                               # 노선 자료 30역은 항상 1등급, 등급이 없으면 3등급
    from datagen.ui import map_api
    assert map_api._TIER2_LINES == 3                                             # 노선 3개 이상 환승역 — 초기값


def test_place_labels_moves_only_station_names_not_equipment_or_area_labels():
    a = HTML.index("function placeLabels(){")
    body = HTML[a:HTML.index("\n}\n", a)]
    assert "const cand=left ? [[x+dxp, y], [x-dxp-bw, y], [x-bw/2, y-bh], [x-bw/2, y+bh]] : [[x-bw/2, y]];" in body
    assert "if(x0===null) continue;" in body


# ── 화면: 그리기 안전 · 지명 투명 · 경계 점선 · 丁目 끔 ──────────────────────────────────────────────────

@needs_node
def test_stn_build_draws_only_bands_and_survives_unknown_kinds(tmp_path):
    a = HTML.index("function stnBuild(g){")
    b = HTML.index("//: 마우스 아래 승강장", a)
    js = f"""
let STN_PTS=[], STN_MPP=0; const BASE_CELL=0.01; const mx=x=>x, my=y=>-y;
const el=n=>({{cls:null,d:null,setAttribute(k,v){{this[k==='class'?'cls':k]=v;}}}});
const ring=(x,y)=>[[x,y],[x+.001,y],[x+.001,y+.001],[x,y+.001],[x,y]];
const STBL=[{{kind:'band',name:'東京',ring:ring(139.76,35.68)}}, {{kind:'hull',name:'옛덩어리',ring:ring(139.78,35.70)}}, {{kind:'platform',name:'',ring:ring(139.7601,35.6801)}},
  {{kind:'bldg',name:'東京駅',ring:ring(139.7602,35.6802)}}, {{kind:'area',name:'x',ring:ring(139.7603,35.6803)}},
  {{kind:'zzz',name:'모르는',ring:ring(139.77,35.69)}}, {{kind:'n02',name:'有楽町',lines:['a'],segs:[[[139.76,35.67],[139.761,35.67]]]}}];
const g={{kids:[],appendChild(x){{this.kids.push(x);}}}};
{HTML[a:b]}
stnBuild(g);
console.log(JSON.stringify({{cls:g.kids.map(k=>k.cls), pts:STN_PTS.map(p=>p[2])}}));
"""
    f = tmp_path / "b.js"
    f.write_text(js, encoding="utf-8")
    r = subprocess.run([NODE, str(f)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr                      # 모르는 종류(`zzz`)가 와도 예외가 나지 않는다 — 11-24 지도 멈춤 사고
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["cls"] == ["stk stk-band"]                     # 승강장·건물·구내·N02 선·옛 덩어리(hull)·모르는 종류는 그리지 않는다
    assert set(out["pts"]) == {"東京"}                        # 마우스 표시에도 그리는 것만 들어간다(이름 없는·안 그리는 것은 빠진다)


def test_place_names_are_translucent_so_station_names_read_first():
    css = re.search(r"\n \.lb-a\{[^}]*\}", HTML).group(0)
    assert re.search(r"opacity:\s*\.6\b", css)


def test_district_border_is_dashed_between_districts_and_solid_on_the_coast_and_chome_is_off():
    adm = re.search(r"\n \.adm\{[^}]*\}", HTML).group(0)
    assert "stroke:none" in adm                                                  # 면은 칠만 한다(선은 따로 긋는다)
    lines = re.search(r"\n \.adm-in,\.adm-out\{[^}]*\}", HTML).group(0)
    assert "pointer-events:none" in lines and "vector-effect:non-scaling-stroke" in lines
    assert re.search(r"\n \.adm-in\{stroke-dasharray:4 3\}", HTML)             # 두 구가 맞닿은 선 — 점선
    assert not re.search(r"\.adm-out\{[^}]*dasharray", HTML)                    # 해안·강변(한쪽만 땅) — 실선
    assert re.search(r"const SHOW_CHOME=false;", HTML)
    assert "if(SHOW_CHOME && CHOME && LOD>=2) tiled('gChome','ch'" in HTML       # 데이터·`.ch` 는 남고 한 줄로 되돌린다


@needs_node
def test_adm_edges_split_shared_borders_from_coast_lines(tmp_path):
    """`admEdges` — 이웃 구 경계가 30m 안에 붙어 있으면 「구 사이」(in), 아니면 「땅 끝」(out). 번호가 앞선 구만 긋는다."""
    a = HTML.index("const ADM_SHARE_M=30;")
    b = HTML.index("\n}\n", HTML.index("function admEdges(geo){", a)) + 3
    js = f"""
const mx=x=>x, my=y=>-y;
{HTML[a:b]}
const sq=(x,y)=>[[x,y],[x+.01,y],[x+.01,y+.01],[x,y+.01]];
// 두 구가 x=.01 에서 맞닿는다(꼭짓점은 일부러 20m 어긋나게 — `N03_map` 이 구마다 따로 단순화한 모양)
const geo={{features:[{{rings:[sq(139.70,35.60)]}}, {{rings:[[[139.71,35.60002],[139.72,35.60002],[139.72,35.61002],[139.71,35.61002]]]}}]}};
const r=admEdges(geo);
const R=(r&&r.runs)||ADM_EDGES&&ADM_EDGES.runs||[];
console.log(JSON.stringify({{keys:Object.keys(r||{{}}), n:R.length, cls:[...new Set(R.map(x=>x.cls))]}}));
"""
    f = tmp_path / "e.js"
    f.write_text(js, encoding="utf-8")
    res = subprocess.run([NODE, str(f)], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout.strip().splitlines()[-1])
    assert out["n"] > 0 and set(out["cls"]) == {"in", "out"}, out                # 맞닿은 변은 in, 바깥 변은 out 이 함께 나온다
