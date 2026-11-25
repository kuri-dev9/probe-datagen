"""지도 배경 역 표시 — N02 승강장 선 레이어 (2026-11-24 · 맥스 구현 · 에스터 시험).

OSM 「역 건물」 윤곽은 東京·上野·品川 본 역사가 빠져 N02 승강장 선으로 바꿨다. 화면은 `station` 한 이름으로 받고
어느 레이어가 나갈지는 설정 `map_station_layer` 가 정한다. 여기서는 그 고르기, 파일 모양, 야마노테 30역이 철로 위에
맞는지, 화면 연결을 본다.
"""
from __future__ import annotations

import json
import math
import re
import statistics
from pathlib import Path

import pytest
import yaml
from fastapi import HTTPException

from datagen.ui import map_api

ROOT = Path(__file__).resolve().parents[1]
OSM = ROOT.parent / "graphio-probe-rca" / "data" / "geo" / "jp" / "station_osm.json"
N02 = ROOT.parent / "graphio-probe-rca" / "data" / "geo" / "jp" / "station_n02.json"
MAP_HTML = ROOT / "src" / "datagen" / "ui" / "static" / "map.html"
TRACK = ROOT / "data" / "tracks" / "yamanote_track.json"

pytestmark = pytest.mark.skipif(not (N02.exists() and OSM.exists()), reason="역 레이어 json 이 구워져 있지 않다")


@pytest.fixture(scope="module")
def items():
    return json.loads(N02.read_text(encoding="utf-8"))


def _call(monkeypatch, layer):
    """설정 `map_station_layer` 를 `layer` 로 두고 `/api/map/layer/station` 을 부른다."""
    cfg = {"map_station_layer": layer, "geo_station_n02": str(N02),
           "geo_station_bldg": str(N02.with_name("station_bldg.json"))}
    monkeypatch.setattr(map_api, "_pem_cfg", lambda: cfg)
    return map_api.api_map_layer("station")


def _body(resp):
    # 파일이 없으면 Response 가 아니라 {"error", "items"} dict 가 나간다
    return resp if isinstance(resp, dict) else json.loads(resp.body)


# ── 고르기 ───────────────────────────────────────────────────────────────

def test_station_follows_the_config_line_n02(monkeypatch):
    b = _body(_call(monkeypatch, "station_n02"))
    assert len(b["items"]) == 461
    assert "N02" in b["source"] and "OpenStreetMap" not in b["source"]


def test_station_follows_the_config_line_bldg(monkeypatch):
    # 되돌릴 길 — OSM 건물 파일이 남아 있으면 그 출처 문구가 나간다
    cfg = {"map_station_layer": "station_bldg", "geo_station_bldg": str(N02)}
    monkeypatch.setattr(map_api, "_pem_cfg", lambda: cfg)
    b = _body(map_api.api_map_layer("station"))
    assert "OpenStreetMap" in b["source"]


def test_default_without_the_line_is_osm(monkeypatch):
    # 기본이 N02 승강장 선에서 OSM 역 모양으로 바뀌었다(사용자 11-24 「선만 그어둔 것 같다」)
    monkeypatch.setattr(map_api, "_pem_cfg", lambda: {"geo_station_osm": str(OSM)})
    b = _body(map_api.api_map_layer("station"))
    assert "OpenStreetMap" in b["source"] and len(b["items"]) == len(json.loads(OSM.read_text(encoding="utf-8")))


@pytest.mark.parametrize("bad", ["rail", "station", "nonsense", "roads", "station_label"])
def test_other_values_are_404(monkeypatch, bad):
    with pytest.raises(HTTPException) as e:
        _call(monkeypatch, bad)
    assert e.value.status_code == 404


def test_missing_file_is_an_error_not_an_empty_list(monkeypatch):
    monkeypatch.setattr(map_api, "_pem_cfg", lambda: {"map_station_layer": "station_n02",
                                                      "geo_station_n02": "/nonexistent/x.json"})
    b = _body(map_api.api_map_layer("station"))
    assert b.get("error") and b["items"] == []


def test_both_station_layers_are_registered():
    assert map_api._LAYER["station_n02"][0] == "geo_station_n02"
    assert map_api._LAYER["station_bldg"][0] == "geo_station_bldg"
    assert map_api._LAYER["station_osm"][0] == "geo_station_osm"


# ── 파일 모양 ───────────────────────────────────────────────────────────

def test_every_item_has_name_lines_and_segs(items):
    for x in items:
        assert x["name"] and x["lines"] and x["segs"], x
        assert all(len(s) >= 2 and all(len(p) == 2 for p in s) for s in x["segs"]), x["name"]


def test_group_codes_do_not_overlap(items):
    gs = [x["g"] for x in items]
    assert len(gs) == len(set(gs))


def test_ops_present_and_lines_unique_per_item(items):
    for x in items:
        assert x["ops"], x["name"]
        assert len(x["lines"]) == len(set(x["lines"])), x["name"]


def test_coordinates_are_inside_the_city_bbox(items):
    # BBOX_CITY(야마노테 주변) 밖이 섞이면 굽는 범위가 틀린 것이다 — 도쿄 도심 대략 범위
    for x in items:
        for s in x["segs"]:
            for lon, lat in s:
                assert 139.3 < lon < 140.1 and 35.3 < lat < 36.0, x["name"]


def test_big_stations_group_many_platform_segments(items):
    # 노선 이름 수가 아니라 승강장 구간 수 — 東京은 총무·도카이도·도호쿠·신칸센·마루노우치 등이 한 항목이다
    by = {x["name"]: x for x in items if len(x["segs"]) > 1}
    assert len(by["東京"]["segs"]) >= 8 and len(by["東京"]["lines"]) >= 5
    assert len(by["上野"]["segs"]) >= 5 and len(by["品川"]["segs"]) >= 4


# ── 야마노테 30역 ─────────────────────────────────────────────────────────

def _ja_names():
    m = re.search(r"const RP_YMT_JA=\{([^}]*)\}", MAP_HTML.read_text(encoding="utf-8"))
    return re.findall(r"JY\d\d:'([^']+)'", m.group(1))


def _seg_lens(segs):
    return [sum(math.hypot((c - a) * 111320 * math.cos(math.radians(35.68)), (d - b) * 110540)
                for (a, b), (c, d) in zip(s, s[1:])) for s in segs]


def _len_m(segs):
    """승강장 선 길이의 가운데값(m) — 한 역 항목은 노선마다 선이 여럿이고, 상야의 신칸센 구간처럼 하나가
    725m 로 길게 잡히는 것이 있어 최댓값으로 보면 승강장 길이가 아니다."""
    return statistics.median(_seg_lens(segs))


def test_all_30_yamanote_stations_exist(items):
    names = _ja_names()
    assert len(names) == 30
    have = {x["name"] for x in items}
    assert [n for n in names if n not in have] == []


def test_yamanote_stations_are_jr_east_and_platform_length_is_realistic(items):
    by = {}
    for x in items:
        by.setdefault(x["name"], []).append(x)
    for n in _ja_names():
        cands = by[n]
        # N02 는 법정 노선명이라 山手線 이름이 안 붙는 역이 많다(東京은 東北線·東海道線) — 운영 회사로 본다
        assert any("東日本旅客鉄道" in x["ops"] for x in cands), n
        L = _len_m([sg for x in cands for sg in x["segs"]])
        assert 100 <= L <= 450, (n, L)   # 30역 가운데값 141(田町)~346m(東京)


def test_the_three_missing_in_osm_now_have_their_main_station(items):
    # OSM 에서는 본 역사가 빠져 있던 세 역 — 승강장 선이 철로 고리 위 역 위치 가까이 있어야 한다
    t = json.loads(TRACK.read_text(encoding="utf-8"))
    assert len(t["stations"]) == 30
    by = {}
    for x in items:
        by.setdefault(x["name"], []).append(x)
    for n in ("東京", "上野", "品川"):
        assert max(max(_seg_lens(x["segs"])) for x in by[n]) >= 250, n


# ── 화면 연결 ───────────────────────────────────────────────────────────

def test_map_html_asks_for_the_station_layer_not_the_osm_one():
    h = MAP_HTML.read_text(encoding="utf-8")
    assert "['station',v=>STBL=v]" in h
    assert "'station_bldg'" not in h and "layer/station_bldg" not in h


def test_map_html_draws_segs_as_round_bars():
    h = MAP_HTML.read_text(encoding="utf-8")
    assert "segs" in h and ".stn" in h
    assert re.search(r"\.stn\s*\{[^}]*stroke-linecap\s*:\s*round|\.stn\s*\{[^}]*linecap", h) or "round" in h[h.index(".stn"):h.index(".stn") + 200]


# ── 설정 ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("f,key", [("datagen.yaml", "geo_station_n02"), ("local.yaml", "geo_station_n02")])
def test_config_has_the_n02_path(f, key):
    assert key in (ROOT / "config" / f).read_text(encoding="utf-8")


def test_datagen_yaml_default_layer_is_osm_and_paths_point_to_real_files():
    txt = (ROOT / "config" / "datagen.yaml").read_text(encoding="utf-8")
    assert re.search(r"^\s*map_station_layer:\s*station_osm\b", txt, re.M)
    # 되돌릴 길 — N02·OSM 건물 경로도 남아 있다
    assert "geo_station_n02" in txt and "geo_station_bldg" in txt
    for key, f in (("geo_station_osm", OSM), ("geo_station_n02", N02)):
        m = re.search(rf"^\s*{key}:\s*(\S+)", txt, re.M)
        assert m and (map_api.HERE / m.group(1)).resolve() == f.resolve(), key   # 경로는 설정 파일이 아니라 `_layer_path` 기준(HERE)
