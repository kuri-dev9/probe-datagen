"""15번 `ho_interruption` 노선 구간 대상 (2026-11-05 · 사용자 승인) — `route_segment` · `corridor_shape`.

F-133 대상은 노선 셀과 0/15 로 겹쳤고 반경 1.07km 에 몰렸다(`corridor_cells` 는 노선을 보지 않고,
앵커 300개 시뮬레이션에서 CORRIDOR 조건을 넘는 경우 2%). 이제 대상을 비우면 노선의 연속 구간을 쓴다.
여기서는 (1) 합성 노선으로 구간 선택 규칙, (2) `corridor_shape` 가 graphio `geo_pattern` 과 같은 식인지,
(3) `faults.build` 연결과 `mobility`·`son_param_change` 회귀, (4) 정답지 `route_seg`·`cell_pairs` 를 고정한다.
실제 노선·셀 마스터는 파일이 있을 때만 확인한다. DB 는 쓰지 않는다.
"""
from __future__ import annotations

import json
import math
import random
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from datagen import faults
from datagen import handover as HO
from datagen import scenarios as S
from datagen.cells import Cell, load_cells

HERE = Path(__file__).resolve().parent.parent
T0 = datetime(2026, 11, 5, 3, 0)
GRAPHIO = HERE.parent / "graphio-probe-rca"

# 위도 35.68° 의 경도 1° ≈ 90.5km
KM_LAT = 111.0
KM_LON = 111.320 * math.cos(math.radians(35.68))


def _cell(key, lat, lon, sgw="0001", mapped=True):
    enb, _, n = key.partition("-")
    return Cell(cell_key=key, enb_id=enb, cell_id=n, enb_seq=0, c_uid="", tac=100, plmn="44010",
                vlan_id="", latitude=lat, longitude=lon, adm_sido_cd="13", adm_sido_nm="", adm_sgg_cd="13104",
                adm_sgg_nm="", adm_emd_cd="13104001", adm_emd_nm="",
                geo_status="MAPPED" if mapped else "UNMAPPED", rat="EUTRAN",
                mme_id="0001", sgw_id=sgw, pgw_id=sgw, ims_pgw_id=sgw, traffic_weight=1.0, display_name="")


def _line_route(n=60, step_km=0.35, loop=False, rid="L1", sgw_every=10, bend=0.0):
    """동서로 곧은 합성 노선 — 칸마다 셀 하나, `step_km` 간격. `sgw_every` 칸마다 SGW 가 바뀐다."""
    cells = tuple(_cell(f"{700000 + i}-0", 35.68 + bend * math.sin(i / 4.0) / KM_LAT,
                        139.70 + i * step_km / KM_LON,
                        sgw=f"{(i // sgw_every) % 2 + 1:04d}") for i in range(n))
    return HO.Route(rid, rid, cells, tuple([60] * n), loop=loop,
                    station_of=tuple(f"S{i // 3}" for i in range(n)),
                    stations={f"S{i}": f"역{i}" for i in range(n // 3 + 1)})


def _ring_route(n=60, radius_km=4.0, rid="R1"):
    """원형 순환선 — 끝을 넘어 이어지는 구간이 생긴다."""
    cells = []
    for i in range(n):
        a = 2 * math.pi * i / n
        cells.append(_cell(f"{710000 + i}-0", 35.68 + radius_km * math.sin(a) / KM_LAT,
                           139.70 + radius_km * math.cos(a) / KM_LON, sgw=f"{(i // 10) % 2 + 1:04d}"))
    return HO.Route(rid, rid, tuple(cells), tuple([60] * n), loop=True)


def _ref_shape(cells):
    """graphio `detector.geo_pattern` 의 반경·길이비 식을 따로 다시 쓴 기준값(독립 구현)."""
    lat = sum(c.latitude for c in cells) / len(cells)
    lon = sum(c.longitude for c in cells) / len(cells)

    def hav(a1, o1, a2, o2):
        p = math.pi / 180
        h = math.sin((a2 - a1) * p / 2) ** 2 + math.cos(a1 * p) * math.cos(a2 * p) * math.sin((o2 - o1) * p / 2) ** 2
        return 2 * 6371.0 * math.asin(math.sqrt(h))

    radius = max(hav(lat, lon, c.latitude, c.longitude) for c in cells)
    kx = 111.320 * math.cos(math.radians(lat))
    xs = [(c.longitude - lon) * kx for c in cells]
    ys = [(c.latitude - lat) * 111.0 for c in cells]
    sxx, syy = sum(x * x for x in xs) / len(xs), sum(y * y for y in ys) / len(ys)
    sxy = sum(x * y for x, y in zip(xs, ys)) / len(xs)
    tr, det = sxx + syy, sxx * syy - sxy * sxy
    disc = max(0.0, tr * tr / 4 - det)
    return radius, math.sqrt((tr / 2 + math.sqrt(disc)) / max(1e-9, tr / 2 - math.sqrt(disc)))


# --------------------------------------------------------------------------
# 1. route_segment — 합성 노선
# --------------------------------------------------------------------------


def test_segment_is_a_consecutive_run_of_route_cells():
    rt = _line_route()
    seg, info = S.route_segment([rt], random.Random(1))
    keys = [c.cell_key for c in rt.cells]
    pos = keys.index(seg[0].cell_key)
    assert [c.cell_key for c in seg] == keys[pos:pos + len(seg)]
    assert info["route_id"] == "L1" and info["cells"] == len(seg)


def test_straight_route_clears_the_graphio_corridor_thresholds_with_the_margin():
    seg, info = S.route_segment([_line_route()], random.Random(2))
    radius, elong = _ref_shape(seg)
    assert radius > S.CORRIDOR_RADIUS_KM * S.ROUTE_SEG_MARGIN
    assert elong > S.CORRIDOR_ELONG * S.ROUTE_SEG_MARGIN
    assert (info["radius_km"], info["elong"]) == (round(radius, 2), round(elong, 2))


def test_segment_starts_at_the_requested_size_when_the_shape_already_fits():
    # 0.35km 간격 15셀 = 길이 4.9km → 반경 2.45km 라 모자란다 → 늘어난다. 0.6km 면 15셀로 충분
    seg, _ = S.route_segment([_line_route(step_km=0.6)], random.Random(3))
    assert len(seg) == S.ROUTE_SEG_CELLS


def test_segment_grows_one_cell_at_a_time_until_the_shape_fits():
    seg, info = S.route_segment([_line_route(step_km=0.35)], random.Random(4))
    assert S.ROUTE_SEG_CELLS < len(seg) <= S.ROUTE_SEG_MAX_CELLS
    # 한 칸 짧은 같은 시작 구간은 문턱을 못 넘는다 — "가장 짧은 구간"
    rt = _line_route(step_km=0.35)
    keys = [c.cell_key for c in rt.cells]
    p = keys.index(seg[0].cell_key)
    short = list(rt.cells[p:p + len(seg) - 1])
    r, e = _ref_shape(short)
    assert r <= 2.5 * 1.2 or e <= 3.2 * 1.2


def test_returns_none_when_no_segment_up_to_the_limit_has_the_shape():
    # 칸 간격 0.05km 면 30셀도 1.5km — 반경 문턱 3.0km 를 못 넘는다
    assert S.route_segment([_line_route(step_km=0.05)], random.Random(5)) is None


def test_returns_none_for_a_short_or_empty_route_list():
    assert S.route_segment([], random.Random(1)) is None
    assert S.route_segment([_line_route(n=10, step_km=0.35)], random.Random(1)) is None


def test_non_loop_route_never_runs_past_its_end():
    rt = _line_route(n=40, step_km=0.5)
    keys = [c.cell_key for c in rt.cells]
    for s in range(30):
        got = S.route_segment([rt], random.Random(s))
        assert got is not None
        seg = got[0]
        assert keys.index(seg[-1].cell_key) == keys.index(seg[0].cell_key) + len(seg) - 1, "끝을 넘어 이어졌다"


def test_loop_route_can_wrap_past_the_end_and_stay_consecutive():
    rt = _ring_route()
    keys = [c.cell_key for c in rt.cells]
    wrapped = 0
    for s in range(60):
        seg, _ = S.route_segment([rt], random.Random(s))
        idx = [keys.index(c.cell_key) for c in seg]
        assert all((b - a) % len(keys) == 1 for a, b in zip(idx, idx[1:])), "순환 연속이 아니다"
        wrapped += idx != sorted(idx)
    assert wrapped > 0, "순환선인데 끝을 넘는 구간이 한 번도 안 나왔다"


def test_same_seed_gives_the_same_segment_and_different_seeds_can_differ():
    rt = _line_route(n=120, step_km=0.5)
    a = [c.cell_key for c in S.route_segment([rt], random.Random(11))[0]]
    b = [c.cell_key for c in S.route_segment([rt], random.Random(11))[0]]
    assert a == b
    others = {S.route_segment([rt], random.Random(s))[0][0].cell_key for s in range(20)}
    assert len(others) > 1


def test_unmapped_cells_are_skipped_not_used():
    rt = _line_route(n=40, step_km=0.5)
    cells = list(rt.cells)
    cells[10] = _cell("999999-0", 35.68, 139.70 + 10 * 0.5 / KM_LON, mapped=False)
    rt2 = HO.Route("U", "U", tuple(cells), tuple([60] * 40), loop=False)
    for s in range(20):
        got = S.route_segment([rt2], random.Random(s))
        if got:
            assert "999999-0" not in {c.cell_key for c in got[0]}


def test_sgw_changes_counts_neighbouring_cells_with_different_sgw():
    seg, info = S.route_segment([_line_route(sgw_every=7)], random.Random(6))
    assert info["sgw_changes"] == sum(a.sgw_id != b.sgw_id for a, b in zip(seg, seg[1:]))
    assert info["sgw_changes"] >= 1


def test_sgw_is_never_used_to_choose_the_segment():
    # SGW 가 모두 같은 노선과 칸마다 다른 노선에서 같은 시드가 같은 위치를 고른다
    flat = _line_route(sgw_every=1000)
    mixed = _line_route(sgw_every=1)
    a = [c.cell_key for c in S.route_segment([flat], random.Random(8))[0]]
    b = [c.cell_key for c in S.route_segment([mixed], random.Random(8))[0]]
    assert a == b
    assert S.route_segment([flat], random.Random(8))[1]["sgw_changes"] == 0


def test_stations_in_info_are_the_first_and_last_cell_stations():
    rt = _line_route()
    seg, info = S.route_segment([rt], random.Random(9))
    keys = [c.cell_key for c in rt.cells]
    p = keys.index(seg[0].cell_key)
    q = keys.index(seg[-1].cell_key)
    assert info["stations"] == [rt.stations[rt.station_of[p]], rt.stations[rt.station_of[q]]]


def test_a_route_without_station_info_gives_empty_stations():
    rt = _line_route()
    rt2 = HO.Route("N", "N", rt.cells, rt.dwell_sec, loop=False)
    assert S.route_segment([rt2], random.Random(1))[1]["stations"] == []


def test_new_constants_are_marked_defaults_and_do_not_touch_graphio_thresholds():
    assert (S.ROUTE_SEG_CELLS, S.ROUTE_SEG_MAX_CELLS, S.ROUTE_SEG_MARGIN) == (15, 30, 1.2)
    assert (S.CORRIDOR_RADIUS_KM, S.CORRIDOR_ELONG) == (2.5, 3.2)


# --------------------------------------------------------------------------
# 2. corridor_shape — graphio geo_pattern 과 같은 식
# --------------------------------------------------------------------------


@pytest.mark.parametrize("seed", range(6))
def test_corridor_shape_matches_the_independent_reference(seed):
    rng = random.Random(seed)
    cells = [_cell(f"{800000 + i}-0", 35.6 + rng.random() * 0.05, 139.7 + rng.random() * 0.06) for i in range(18)]
    r, e = S.corridor_shape(cells)
    rr, ee = _ref_shape(cells)
    assert r == pytest.approx(rr, rel=1e-9) and e == pytest.approx(ee, rel=1e-9)


def test_corridor_shape_of_a_line_has_high_elongation_and_of_a_blob_low():
    line = [_cell(f"{1 + i}-0", 35.68, 139.70 + i * 0.4 / KM_LON) for i in range(12)]
    blob = [_cell(f"{50 + i}-0", 35.68 + (i % 4) * 0.3 / KM_LAT, 139.70 + (i // 4) * 0.3 / KM_LON) for i in range(12)]
    assert S.corridor_shape(line)[1] > 3.2 > S.corridor_shape(blob)[1]


@pytest.mark.skipif(not (GRAPHIO / ".venv/bin/python").exists(), reason="graphio venv 가 없다")
def test_corridor_shape_agrees_with_the_real_graphio_geo_pattern():
    """같은 좌표를 graphio 의 `detector.geo_pattern` 에 넣어 반경과 CORRIDOR 판정을 대조한다(값 대조)."""
    rt = _line_route(step_km=0.5)
    seg, info = S.route_segment([rt], random.Random(3))
    coords = [(c.latitude, c.longitude) for c in seg]
    code = ("import json,sys\nfrom graphio.detect.detector import geo_pattern\n"
            "c=json.loads(sys.stdin.read())\nprint(json.dumps(geo_pattern([tuple(x) for x in c])[:2]))")
    out = subprocess.run([str(GRAPHIO / ".venv/bin/python"), "-I", "-c", code], input=json.dumps(coords),
                         capture_output=True, text=True, cwd=str(GRAPHIO), timeout=60,
                         env={"PYTHONPATH": str(GRAPHIO / "src"), "PATH": "/usr/bin:/bin"})
    if out.returncode != 0:
        pytest.skip(f"graphio import 실패: {out.stderr[-120:]}")
    pattern, radius = json.loads(out.stdout)
    assert pattern == "CORRIDOR"
    assert radius == pytest.approx(S.corridor_shape(seg)[0], abs=0.01)
    assert radius == pytest.approx(info["radius_km"], abs=0.01)


# --------------------------------------------------------------------------
# 3. faults.build 연결과 회귀
# --------------------------------------------------------------------------


def _grid_cells():
    out = []
    for e in range(60):
        out += [_cell(f"{600000 + e}-{n}", 35.60 + (e // 10) * 0.012 + n * 0.0015,
                      139.60 + (e % 10) * 0.012 + n * 0.0015) for n in range(3)]
    return out


def _build(kind, routes=None, target=None, seed=7, fid=1):
    row = {"fault_id": fid, "kind": kind, "target": target, "start_ts": T0 + timedelta(minutes=2),
           "duration_min": 30, "intensity": 1.0}
    return faults.build(row, _grid_cells(), T0, faults.target_rng(seed, fid), seed=seed, routes=routes)


def test_ho_interruption_without_target_uses_the_route_segment():
    rt = _line_route()
    sc = _build("ho_interruption", routes=[rt])
    assert sc is not None and sc.target.route_seg
    assert sc.target.cell_keys <= {c.cell_key for c in rt.cells}
    assert len(sc.target.cell_keys) == sc.target.route_seg["cells"]


def test_pairs_are_neighbouring_cells_and_count_is_cells_minus_one():
    sc = _build("ho_interruption", routes=[_line_route()])
    pairs = sc.target.cell_pairs
    assert len(pairs) == len(sc.target.cell_keys) - 1
    keys = [c.cell_key for c in _line_route().cells]
    for a, b in pairs:
        assert keys.index(b) - keys.index(a) == 1


def test_no_routes_falls_back_to_corridor_cells_with_one_warning_line(capsys):
    sc = _build("ho_interruption", routes=None)
    out = capsys.readouterr().out
    assert sc is not None and sc.target.route_seg is None
    assert out.count("ho_interruption 노선 구간을 못 골랐다") == 1 and "노선 없음" in out
    assert sc.target.cell_pairs, "후퇴해도 셀 쌍은 있다(corridor_cells)"


def test_route_without_the_shape_falls_back_and_says_so(capsys):
    sc = _build("ho_interruption", routes=[_line_route(step_km=0.05)])
    out = capsys.readouterr().out
    assert sc.target.route_seg is None and "모양 미달" in out


def test_explicit_enb_and_box_targets_are_unchanged_by_routes():
    cells = _grid_cells()
    enb = cells[0].enb_id
    a = _build("ho_interruption", routes=None, target=f"ENB:{enb}")
    b = _build("ho_interruption", routes=[_line_route()], target=f"ENB:{enb}")
    assert a.target.cell_keys == b.target.cell_keys and b.target.route_seg is None
    assert a.target.cell_pairs == b.target.cell_pairs


def test_mobility_uses_the_route_segment_like_ho_interruption():
    """9번도 노선 구간이다(2026-11-07 · B안) — 정답 CORRIDOR 인데 방위각 띠는 셀 15개가 SCATTER 3.07km 가 됐다."""
    b = _build("mobility", routes=[_line_route()])
    assert b.target.route_seg is not None
    assert len(b.target.cell_pairs) >= 4 and all(x != y for x, y in b.target.cell_pairs)


def test_mobility_without_routes_keeps_the_azimuth_band_and_warns(capsys):
    b = _build("mobility", routes=None)
    assert b.target.route_seg is None and len(b.target.cell_pairs) == 8, "기존 방식: 앵커 → 띠 안 8셀"
    assert "mobility 노선 구간을 못 골랐다" in capsys.readouterr().out


def test_son_param_change_uses_the_route_segment_like_ho_interruption():
    """25번도 노선 구간이다(2026-11-07 · B안) — 정답 CORRIDOR 인데 방위각 띠는 도심에서 POINT 가 됐다."""
    b = _build("son_param_change", routes=[_line_route()])
    assert b.target.route_seg is not None
    assert all(x != y for x, y in b.target.cell_pairs) and len(b.target.cell_pairs) >= 4


def test_same_fault_id_and_seed_give_the_same_segment_across_builds():
    rt = _line_route(n=120, step_km=0.5)
    a = _build("ho_interruption", routes=[rt], seed=3, fid=21).target.cell_keys
    b = _build("ho_interruption", routes=[rt], seed=3, fid=21).target.cell_keys
    c = _build("ho_interruption", routes=[rt], seed=3, fid=22).target.cell_keys
    assert a == b
    assert a != c or len(rt) < 40


def test_like_fault_reproduces_the_original_target_resolution():
    rt = _line_route(n=120, step_km=0.5)
    orig = _build("ho_interruption", routes=[rt], seed=5, fid=9)
    row = {"fault_id": 30, "kind": "ho_interruption", "target": "LIKE:9", "start_ts": T0 + timedelta(minutes=2),
           "duration_min": 30, "intensity": 1.0}
    like = faults.build(row, _grid_cells(), T0, faults.target_rng(5, 30), seed=5, routes=[rt])
    assert like.target.cell_keys == orig.target.cell_keys


def test_other_kinds_with_routes_given_are_unaffected():
    for kind in ("ran_radio", "transport_loss", "ue_model", "mme_overload"):
        a, b = _build(kind, routes=None), _build(kind, routes=[_line_route()])
        assert a.target.cell_keys == b.target.cell_keys and b.target.route_seg is None, kind


# --------------------------------------------------------------------------
# 4. 정답지
# --------------------------------------------------------------------------


def test_truth_carries_route_seg_and_cell_pairs_only_for_a_route_target():
    sc = _build("ho_interruption", routes=[_line_route()])
    t = sc.as_truth()
    assert t["route_seg"] == sc.target.route_seg
    assert t["cell_pairs"] == [list(p) for p in sc.target.cell_pairs]
    assert set(t["route_seg"]) == {"route_id", "stations", "cells", "sgw_changes", "radius_km", "elong"}
    assert t["expect_geo"] == "CORRIDOR" and t["cell_cnt"] == len(sc.target.cell_keys)
    json.dumps(t)                                       # 직렬화 가능(튜플이 남지 않는다)


def test_truth_has_no_route_fields_without_a_route_target():
    for kind, routes in (("ho_interruption", None), ("mobility", None), ("transport_loss", None)):
        t = _build(kind, routes=routes).as_truth()
        assert "route_seg" not in t, kind


def test_truth_sgw_changes_equals_real_neighbour_sgw_differences():
    rt = _line_route(sgw_every=6)
    sc = _build("ho_interruption", routes=[rt])
    by = {c.cell_key: c for c in rt.cells}
    diff = sum(by[a].sgw_id != by[b].sgw_id for a, b in sc.target.cell_pairs)
    assert sc.as_truth()["route_seg"]["sgw_changes"] == diff


def test_truth_cells_are_sorted_and_equal_the_target_cells():
    sc = _build("ho_interruption", routes=[_line_route()])
    t = sc.as_truth()
    assert t["cells"] == sorted(sc.target.cell_keys)


def test_truth_consumers_still_get_the_old_required_fields():
    # graphio score · gen-ui 가 읽는 기존 키가 그대로 있다
    t = _build("ho_interruption", routes=[_line_route()]).as_truth()
    for k in ("scenario_id", "kind", "scope_kind", "expect_kpi", "expect_node", "truth_code", "expect_rules",
              "expect_alternatives", "expect_geo", "start_minute", "duration", "shape", "cells", "cell_cnt",
              "series", "grade"):
        assert k in t, k


# --------------------------------------------------------------------------
# 5. 실제 노선 · 셀 마스터 (파일이 있을 때만)
# --------------------------------------------------------------------------

REAL = (HERE / "data/lines/yamanote.json").exists() and (HERE / "data/cells.json").exists()


@pytest.fixture(scope="module")
def real():
    """노선 파일은 **일본 셀 마스터**(DB 의 `CM_CELL_REP_RU_LOC_JP`)의 키를 쓴다. `data/cells.json` 은 한국
    마스터라 키가 안 맞으면(`ValueError`) 이 절은 건너뛴다 — DB 를 열지 않는다."""
    cells = load_cells(HERE / "data/cells.json")
    try:
        return cells, HO.load_routes(HERE / "data/lines/yamanote.json", cells)
    except ValueError as e:
        pytest.skip(f"노선 파일의 셀이 이 마스터에 없다(일본 마스터 필요): {str(e)[:60]}")


@pytest.mark.skipif(not REAL, reason="노선 파일/셀 마스터가 없다")
def test_real_yamanote_always_gives_a_corridor_segment_of_15_cells(real):
    cells, routes = real
    ok = 0
    for s in range(60):
        seg, info = S.route_segment(routes, random.Random(s))
        r, e = _ref_shape(seg)
        ok += r > 2.5 and e > 3.2
        assert len(seg) == 15, "늘리지 않고 15셀이면 된다는 맥스 보고(300회)와 다르다"
    assert ok == 60


@pytest.mark.skipif(not REAL, reason="노선 파일/셀 마스터가 없다")
def test_real_segment_cells_are_route_cells_and_unlike_corridor_cells_overlap_with_route(real):
    cells, routes = real
    route_keys = {c.cell_key for rt in routes for c in rt.cells}
    seg, _ = S.route_segment(routes, random.Random(1))
    assert {c.cell_key for c in seg} <= route_keys
    # 옛 방식(corridor_cells) 대상은 노선 셀과 거의 안 겹친다 — F-133 0/15
    mapped_overlap = []
    for s in range(30):
        anchor, band = S.corridor_cells(cells, random.Random(s), want=18)
        pick = {anchor.cell_key} | {c.cell_key for c in band[:14]}
        mapped_overlap.append(len(pick & route_keys) / len(pick))
    assert sum(mapped_overlap) / len(mapped_overlap) < 0.5
