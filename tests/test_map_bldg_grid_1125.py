"""`/api/map/bldg/index` · `/api/map/bldg?cells=` — 3D 재생이 보이는 칸만 받는 낮은 건물 서버 (2026-11-25 맥스 · 에스터 시험).

고정하는 것 — 칸 코드는 JIS 3차 메시 8자리만 · 한 번에 최대 400칸(`_BLDG_MAX_CELLS`) · 건물이 없는 칸은 오류가 아니라 `missing` ·
칸 파일은 파싱하지 않고 그대로 이어 붙인다(응답이 유효한 JSON) · gzip 은 받는 쪽이 원할 때만 · 색인이 없으면 `{"error", "cells": {}}`
(3D 가 높은 건물만으로 돌아간다) · 3D 전용이라 지도 첫 로딩과 `/api/map/layer` 에 섞이지 않는다 · 설정 키는 두 yaml 에 있다.
임시 폴더만 쓴다 — 실제 `data/geo` 를 읽지 않는다.
"""
from __future__ import annotations

import gzip
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from datagen.ui import map_api

ROOT = Path(__file__).resolve().parent.parent


def _req(gz=False):
    return SimpleNamespace(headers={"accept-encoding": "gzip, deflate" if gz else ""})


def _body(resp):
    if isinstance(resp, dict):
        return resp
    raw = resp.body
    if resp.headers.get("content-encoding") == "gzip":
        raw = gzip.decompress(raw)
    return json.loads(raw)


@pytest.fixture
def grid(tmp_path, monkeypatch):
    d = tmp_path / "bldg"; d.mkdir()
    (d / "index.json").write_text(json.dumps({"mesh": "JIS3", "min_m": 8.0, "min_levels": 3, "q": 1e-5, "total": 3,
                                              "cells": {"53394526": [2, 1], "53394527": [1, 0]}}), encoding="utf-8")
    (d / "53394526.json").write_text(json.dumps({"o": [139.7, 35.68], "q": 1e-5, "b": [{"h": 35.0, "r": [[0, 0, 20, 0, 20, 18]], "t": 1}, {"h": 10.0, "r": [[5, 5, 9, 5, 9, 9]]}]}), encoding="utf-8")
    (d / "53394527.json").write_text(json.dumps({"o": [139.7, 35.69], "q": 1e-5, "b": [{"h": 12.0, "r": [[0, 0, 3, 0, 3, 3]]}]}), encoding="utf-8")
    monkeypatch.setattr(map_api, "_pem_cfg", lambda: {"geo_bldg_dir": str(d)})
    return d


def test_index_is_served_as_is(grid):
    d = _body(map_api.api_map_bldg_index(_req()))
    assert d["mesh"] == "JIS3" and d["total"] == 3 and d["cells"]["53394526"] == [2, 1]


def test_cells_come_back_as_valid_json_with_missing_listed_not_failed(grid):
    d = _body(map_api.api_map_bldg(_req(), cells="53394526,53394527,53394528"))
    assert set(d["cells"]) == {"53394526", "53394527"} and d["missing"] == ["53394528"]
    assert d["cells"]["53394526"]["b"][0]["t"] == 1 and d["cells"]["53394527"]["o"] == [139.7, 35.69]
    assert "OpenStreetMap" in d["source"]
    assert _body(map_api.api_map_bldg(_req(), cells="")) == {"cells": {}, "missing": [], "source": d["source"]}


def test_duplicates_and_spaces_are_ignored(grid):
    d = _body(map_api.api_map_bldg(_req(), cells=" 53394526 ,53394526,, 53394527"))
    assert set(d["cells"]) == {"53394526", "53394527"} and d["missing"] == []


@pytest.mark.parametrize("bad", ["abc", "5339452", "533945260", "53394526;rm", "../index", "5339452a"])
def test_only_eight_digit_mesh_codes_are_accepted_so_no_path_can_escape(grid, bad):
    with pytest.raises(HTTPException) as e:
        map_api.api_map_bldg(_req(), cells=bad)
    assert e.value.status_code == 400


def test_at_most_400_cells_per_request(grid):
    assert map_api._BLDG_MAX_CELLS == 400
    ok = ",".join(f"{53390000 + i:08d}" for i in range(400))
    assert len(_body(map_api.api_map_bldg(_req(), cells=ok))["missing"]) == 400
    with pytest.raises(HTTPException) as e:
        map_api.api_map_bldg(_req(), cells=ok + ",53399999")
    assert e.value.status_code == 400 and "400" in e.value.detail


def test_gzip_only_when_the_client_accepts_it_and_it_round_trips(grid):
    plain = map_api.api_map_bldg(_req(), cells="53394526,53394527")
    z = map_api.api_map_bldg(_req(gz=True), cells="53394526,53394527")
    assert "content-encoding" not in plain.headers and z.headers["content-encoding"] == "gzip" and z.headers["vary"] == "Accept-Encoding"
    assert _body(z) == _body(plain) and len(z.body) < len(plain.body) * 2          # 작은 시험 자료라 크기 이득은 보지 않는다
    zi = map_api.api_map_bldg_index(_req(gz=True))
    assert zi.headers["content-encoding"] == "gzip" and _body(zi)["total"] == 3


def test_without_an_index_both_endpoints_say_why_and_the_3d_falls_back(tmp_path, monkeypatch):
    monkeypatch.setattr(map_api, "_pem_cfg", lambda: {"geo_bldg_dir": str(tmp_path / "none")})
    for r in (map_api.api_map_bldg_index(_req()), map_api.api_map_bldg(_req(), cells="53394526")):
        assert r["cells"] == {} and "색인이 없다" in r["error"]
    monkeypatch.setattr(map_api, "_pem_cfg", lambda: {})
    assert "가 비어 있다" in map_api.api_map_bldg_index(_req())["error"]


def test_it_is_3d_only_and_config_keys_exist_in_both_yaml():
    assert "bldg" not in map_api._LAYER and "bldg" not in map_api._LAYER_3D, "`/api/map/layer/{name}` 에 섞이지 않는다 — 칸 전용 경로"
    for name in ("datagen.yaml", "local.yaml"):
        assert "geo_bldg_dir:" in (ROOT / "config" / name).read_text(encoding="utf-8"), name
    html = (ROOT / "src/datagen/ui/static/map.html").read_text(encoding="utf-8")
    assert "api/map/bldg" not in html, "지도 첫 로딩은 건물 칸을 받지 않는다"
