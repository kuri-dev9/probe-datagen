"""9번(이동 중 끊김) 자동 대상이 철도 노선 구간을 따른다 (2026-11-22 · 맥스 · 사용자 「기차길 따라서」).

`handover.load_routes_in_range`(범위 밖 셀에서 끊어 읽기) · `faults._auto_target("mobility")`(`RAIL_ROUTES`가 있으면 그 노선) ·
15번(`ho_interruption`)은 그대로 야마노테선 · `Mobility.rail_routes_file`. DB 없이 합성 셀로 본다.
"""
from __future__ import annotations

import json
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_route_segment import _cell, _line_route  # noqa: E402

from datagen import faults  # noqa: E402
from datagen import handover as HO  # noqa: E402


def _cells(n=60, start=800000):
    return [_cell(f"{start + i}-0", 35.68, 139.70 + i * 0.0039) for i in range(n)]


def _write(tmp_path, entries):
    p = tmp_path / "rail_lines.json"
    p.write_text(json.dumps(entries), encoding="utf-8")
    return p


def _entry(rid, keys, name=None):
    return {"id": rid, "name": name or rid, "cells": keys, "station_of": [f"S{i // 3}" for i in range(len(keys))],
            "stations": {f"S{i}": f"역{i}" for i in range(len(keys) // 3 + 1)}}


# ── 1. load_routes_in_range ──────────────────────────────────────────────────

def test_a_route_is_cut_at_a_cell_outside_the_range_and_both_sides_remain(tmp_path):
    cs = _cells(10)
    keys = [c.cell_key for c in cs[:4]] + ["999999-0"] + [c.cell_key for c in cs[4:9]]
    routes = HO.load_routes_in_range(_write(tmp_path, [_entry("R1", keys)]), cs)
    assert [len(r.cells) for r in routes] == [4, 5], "범위 밖 셀에서 끊고 앞뒤를 따로 둔다(건너뛰어 잇지 않는다)"
    assert len({r.id for r in routes}) == 2
    assert all(not r.loop for r in routes)


def test_pieces_shorter_than_two_cells_are_dropped(tmp_path):
    cs = _cells(8)
    keys = [cs[0].cell_key, "999-0", cs[1].cell_key, cs[2].cell_key, "998-0", cs[3].cell_key]
    routes = HO.load_routes_in_range(_write(tmp_path, [_entry("R1", keys)]), cs)
    assert [len(r.cells) for r in routes] == [2]


def test_unknown_cell_keys_do_not_stop_the_loader_unlike_load_routes(tmp_path):
    cs = _cells(4)
    p = _write(tmp_path, [_entry("R1", ["x-0", "y-0", "z-0"])])
    assert HO.load_routes_in_range(p, cs) == []
    with pytest.raises(Exception):
        HO.load_routes(p, cs)                 # 옛 로더는 모르는 셀 키에서 멈춘다


def test_the_order_and_stations_of_a_kept_piece_follow_the_file(tmp_path):
    cs = _cells(6)
    keys = [c.cell_key for c in cs]
    r = HO.load_routes_in_range(_write(tmp_path, [_entry("R1", keys, name="가나선")]), cs)[0]
    assert [c.cell_key for c in r.cells] == keys and r.name == "가나선"
    assert r.station_of[0] == "S0" and r.stations["S0"] == "역0"


# ── 2~3. 자동 대상 ───────────────────────────────────────────────────────────

@pytest.fixture
def rails():
    old = list(faults.RAIL_ROUTES)
    faults.RAIL_ROUTES[:] = [_line_route(n=60, rid="RAIL-X")]
    yield faults.RAIL_ROUTES
    faults.RAIL_ROUTES[:] = old


def test_mobility_picks_from_the_rail_routes_when_they_exist(rails):
    ymt = _line_route(n=60, rid="YMT")
    t = faults._auto_target("mobility", list(ymt.cells), random.Random(3), routes=[ymt])
    assert t is not None and t.route_seg is not None
    assert str(t.route_seg["route_id"]).startswith("RAIL-")


def test_mobility_falls_back_to_the_old_routes_when_rail_routes_are_empty():
    old = list(faults.RAIL_ROUTES)
    faults.RAIL_ROUTES[:] = []
    try:
        ymt = _line_route(n=60, rid="YMT")
        t = faults._auto_target("mobility", list(ymt.cells), random.Random(3), routes=[ymt])
        assert t is not None and t.route_seg["route_id"] == "YMT"
    finally:
        faults.RAIL_ROUTES[:] = old


def test_ho_interruption_keeps_using_the_yamanote_routes_even_with_rail_routes(rails):
    ymt = _line_route(n=60, rid="YMT")
    t = faults._auto_target("ho_interruption", list(ymt.cells), random.Random(5), routes=[ymt])
    assert t is not None and t.route_seg["route_id"] == "YMT"


def test_the_picked_segment_is_a_corridor_of_neighbouring_cells(rails):
    ymt = _line_route(n=60, rid="YMT")
    t = faults._auto_target("mobility", list(ymt.cells), random.Random(11), routes=[ymt])
    assert len(t.cell_pairs) == len(t.cell_keys) - 1, "쌍은 이웃 칸끼리다"
    assert all(a in t.cell_keys and b in t.cell_keys for a, b in t.cell_pairs)


# ── 4. 설정 칸 ───────────────────────────────────────────────────────────────

def test_mobility_accepts_the_rail_routes_file_key_and_still_rejects_typos():
    from datagen import pemdb1
    cls = pemdb1.Mobility
    m = cls.from_cfg({"rail_routes_file": "data/lines/rail_lines.json"})
    assert m.rail_routes_file == "data/lines/rail_lines.json"
    assert cls.from_cfg({}).rail_routes_file == ""
    with pytest.raises(Exception):
        cls.from_cfg({"rail_route_file": "x"})


def test_the_datagen_yaml_names_the_rail_routes_file_and_it_is_not_the_boarding_file():
    import yaml
    cfg = yaml.safe_load((Path(__file__).resolve().parents[1] / "config" / "datagen.yaml").read_text(encoding="utf-8"))
    mb = cfg["pemdb1"]["mobility"]
    assert mb["rail_routes_file"].endswith("rail_lines.json")
    assert mb.get("routes_file") != mb["rail_routes_file"], "탑승자는 야마노테선 파일을 그대로 탄다"
