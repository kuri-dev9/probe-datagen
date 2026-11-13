"""27번 지진 — 진도 → 음영 셀 (2026-11-13 사용자 결정 · 동구 구현 고정).

진앙·진도·모양을 정하고(`quake_new`) 반경 안 셀을 거리 확률로 뽑아 **eNB 째** 끈다(`quake_plan`).
값(반경·확률·최대 셀·SMS 비율)은 모두 결과를 보고 조정할 **초기값**이라, 이 시험은 값 자체보다
**관계**(보간·거리·최대 셀·같은 eNB·재현성)를 고정한다. DB 를 쓰지 않는다(가짜 연결).
"""
from __future__ import annotations

import random
from types import SimpleNamespace

import pytest

from datagen import control, faults
from datagen import scenarios as S
from datagen.cells import Cell


def _cell(enb: str, n: int, lat: float, lon: float, geo: str = "MAPPED") -> Cell:
    return Cell(
        cell_key=f"{enb}-{n}", enb_id=enb, cell_id=n, enb_seq=0, c_uid="",
        tac=100, plmn="44010", vlan_id="", latitude=lat, longitude=lon,
        adm_sido_cd="11", adm_sido_nm="", adm_sgg_cd="11200", adm_sgg_nm="",
        adm_emd_cd="11200011", adm_emd_nm="", geo_status=geo,
        rat="EUTRAN", mme_id="0001", sgw_id="0037", pgw_id="0037",
        ims_pgw_id="0037", traffic_weight=1.0, display_name="")


def _grid() -> list[Cell]:
    """eNB 100개 × 3셀 — 0.01도(약 1km) 간격 격자. 진앙(35.0, 139.0) 주변에 촘촘하다."""
    out = []
    for i in range(100):
        la, lo = 35.0 + (i // 10 - 5) * 0.01, 139.0 + (i % 10 - 5) * 0.01
        out += [_cell(f"{800000 + i}", n, la + n * 0.0005, lo) for n in range(3)]
    return out


def _q(**kw) -> dict:
    q = {"lat": 35.0, "lon": 139.0, "intensity": 4.0, "shape": "ROUND", "azimuth_deg": None,
         "length_km": None, "radius_km": 10.0, "base_pct": 50.0, "max_cells": 40}
    q.update(kw)
    return q


# ── quake_params ──────────────────────────────────────────────────────────

def test_params_hit_table_values_at_the_knots():
    assert S.quake_params(2.0) == (3.0, 10.0, 10)
    assert S.quake_params(3.0) == (6.0, 30.0, 25)
    assert S.quake_params(4.0) == (10.0, 50.0, 40)


def test_params_interpolate_linearly_and_clamp_outside():
    r, p, m = S.quake_params(2.5)
    assert (r, p, m) == (4.5, 20.0, 18)          # 10 + (25-10)*0.5 = 17.5 → 반올림 18
    assert S.quake_params(1.0) == S.quake_params(2.0)
    assert S.quake_params(9.0) == S.quake_params(4.0)


def test_params_grow_monotonically_with_intensity():
    xs = [S.quake_params(i / 10) for i in range(20, 41)]
    for a, b in zip(xs, xs[1:]):
        assert all(y >= x for x, y in zip(a, b))


# ── quake_new ─────────────────────────────────────────────────────────────

def test_new_draws_within_range_one_decimal_and_is_seed_stable():
    for s in range(30):
        q = S.quake_new(35.0, 139.0, random.Random(f"quake:{s}"))
        assert 2.0 <= q["intensity"] <= 4.0 and q["intensity"] == round(q["intensity"], 1)
        assert q["shape"] in S.QUAKE_SHAPES
    a = S.quake_new(35.0, 139.0, random.Random("quake:7"))
    assert a == S.quake_new(35.0, 139.0, random.Random("quake:7"))


def test_new_given_values_win_and_linear_gets_azimuth_and_length():
    q = S.quake_new(35.0, 139.0, random.Random(1), 3.5, "LINEAR")
    assert q["intensity"] == 3.5 and q["shape"] == "LINEAR"
    assert 0 <= q["azimuth_deg"] < 360
    assert q["length_km"] == round(q["radius_km"] * S.QUAKE_LINEAR_LEN_X, 1)
    r = S.quake_new(35.0, 139.0, random.Random(1), 3.5, "ROUND")
    assert r["azimuth_deg"] is None and r["length_km"] is None


def test_new_unknown_shape_falls_back_to_a_known_one():
    assert S.quake_new(35.0, 139.0, random.Random(1), 3.0, "SQUARE")["shape"] in S.QUAKE_SHAPES


# ── quake_dist_km ─────────────────────────────────────────────────────────

def test_dist_round_is_distance_to_epicenter():
    q = _q()
    assert S.quake_dist_km(q, 35.0, 139.0) == pytest.approx(0.0, abs=1e-6)
    assert S.quake_dist_km(q, 35.09, 139.0) == pytest.approx(10.0, abs=0.1)


def test_dist_linear_is_zero_along_the_line_and_grows_across_it():
    q = _q(shape="LINEAR", azimuth_deg=0, length_km=20.0)       # 북-남 선분 ±10km
    assert S.quake_dist_km(q, 35.05, 139.0) == pytest.approx(0.0, abs=0.01)   # 선 위(북 5.5km)
    across = S.quake_dist_km(q, 35.0, 139.05)
    assert across == pytest.approx(0.05 * 111.32 * 0.8192, abs=0.1)           # 동 4.5km
    # 선분 끝을 넘으면 끝점까지의 거리
    beyond = S.quake_dist_km(q, 35.18, 139.0)
    assert beyond == pytest.approx(0.18 * 111.0 - 10.0, abs=0.2)


def test_dist_linear_azimuth_rotates_the_line():
    ew = _q(shape="LINEAR", azimuth_deg=90, length_km=20.0)     # 동-서
    assert S.quake_dist_km(ew, 35.0, 139.05) == pytest.approx(0.0, abs=0.01)
    assert S.quake_dist_km(ew, 35.05, 139.0) > 5.0


# ── quake_plan ────────────────────────────────────────────────────────────

def test_plan_turns_off_whole_enbs_only():
    cells = _grid()
    area, dead, dead_cells = S.quake_plan(cells, _q(), random.Random(3))
    assert dead, "진도 4 격자에서는 꺼지는 eNB 가 있어야 한다"
    per = {}
    for c in cells:
        per.setdefault(c.enb_id, set()).add(c.cell_key)
    assert set(dead_cells) == {k for e in dead for k in per[e]}
    assert set(dead_cells) <= area


def test_plan_never_exceeds_max_cells():
    cells = _grid()
    for mx in (3, 6, 10, 40):
        for s in range(10):
            _a, _d, dc = S.quake_plan(cells, _q(max_cells=mx, base_pct=100.0), random.Random(s))
            assert len(dc) <= mx
    # 한 eNB 가 3셀이므로 최대 셀 2면 어떤 eNB 도 못 끈다
    assert S.quake_plan(cells, _q(max_cells=2, base_pct=100.0), random.Random(0))[1] == []


def test_plan_is_reproducible_for_the_same_seed_and_differs_across_seeds():
    cells = _grid()
    a = S.quake_plan(cells, _q(), random.Random("quake:5"))
    assert a == S.quake_plan(cells, _q(), random.Random("quake:5"))
    assert len({tuple(S.quake_plan(cells, _q(), random.Random(s))[1]) for s in range(8)}) > 1


def test_plan_only_looks_inside_radius_and_skips_unmapped_cells():
    cells = _grid() + [_cell("999999", 0, 0.0, 0.0, geo="UNMAPPED"),
                       _cell("999998", 0, 36.0, 140.0)]                      # 반경 밖
    area, dead, _dc = S.quake_plan(cells, _q(base_pct=100.0, max_cells=300), random.Random(0))
    assert "999999-0" not in area and "999998-0" not in area
    assert "999999" not in dead and "999998" not in dead


def test_plan_probability_falls_with_distance_and_zero_base_turns_nothing_off():
    cells = _grid()
    assert S.quake_plan(cells, _q(base_pct=0.0), random.Random(0))[1] == []
    near = far = 0
    for s in range(40):
        for e in S.quake_plan(cells, _q(base_pct=60.0, max_cells=300), random.Random(s))[1]:
            la = [c for c in cells if c.enb_id == e][0]
            d = S.quake_dist_km(_q(), la.latitude, la.longitude)
            near += d < 5
            far += d >= 5
    assert near > 0 and far > 0
    # 반경 안 격자 면적은 가까운 쪽(<5km)이 작은데도 비슷한 수가 꺼지려면 가까운 쪽 확률이 높아야 한다
    assert near / max(far, 1) > 0.5


def test_plan_nothing_turns_off_when_no_cell_in_radius_but_surge_area_is_still_the_nearest_cells():
    # 꺼지는 것만 반경 기준이다 — 폭주 지역은 반경과 무관하게 가까운 want 셀이다(11-13 정정)
    area, dead, dc = S.quake_plan(_grid(), _q(lat=10.0, lon=100.0), random.Random(0))
    assert dead == [] and dc == [] and len(area) == 25


def test_surge_area_is_want_nearest_cells_plus_whole_dead_enbs_regardless_of_radius():
    cells = _grid()
    for want in (25, 80):
        for s in range(6):
            area, dead, dc = S.quake_plan(cells, _q(base_pct=80.0), random.Random(s), want)
            assert set(dc) <= area
            assert want <= len(area) <= want + len(dc), (want, len(area), len(dc))   # 꺼진 셀이 want 안에 겹치면 덜 늘어난다
            assert len(area - set(dc)) <= want
    # 반경(1km)보다 먼 셀도 want 에 닿을 때까지 들어온다
    small = _q(radius_km=1.0, intensity=2.0, base_pct=10.0, max_cells=10)
    area, _d, _dc = S.quake_plan(cells, small, random.Random(0), 80)
    far = [c for c in cells if c.cell_key in area and S.quake_dist_km(small, c.latitude, c.longitude) > 1.0]
    assert far, "폭주 지역은 반경에 묶이지 않는다"
    # 가까운 셀부터다 — 들어온 셀의 최대 거리가 안 들어온 셀의 최소 거리(꺼진 eNB 셀 제외)를 넘지 않는다
    inn = max(S.quake_dist_km(small, c.latitude, c.longitude) for c in cells if c.cell_key in area - set(_dc))
    out = min(S.quake_dist_km(small, c.latitude, c.longitude) for c in cells if c.cell_key not in area)
    assert inn <= out + 1e-9


def test_surge_area_has_the_wide_size_for_wide_mode_in_the_built_scenario():
    import random as _r
    from datetime import datetime
    sc_l = faults.build({"fault_id": 27, "kind": "natural_disaster", "target": "QUAKE:35.0,139.0,3.0,ROUND",
                         "start_ts": datetime(2026, 11, 13, 10, 0), "duration_min": 25, "intensity": 1.0},
                        _grid(), datetime(2026, 11, 13, 9, 0), _r.Random(1), seed=3)
    dead = set(sc_l.as_truth()["down_cells"])
    assert len(sc_l.target.cell_keys - dead) <= S.DISASTER_AREA_CELLS["local"]
    sc_w = faults.build({"fault_id": 27, "kind": "natural_disaster", "target": "WIDE",
                         "start_ts": datetime(2026, 11, 13, 10, 0), "duration_min": 25, "intensity": 1.0},
                        _grid(), datetime(2026, 11, 13, 9, 0), _r.Random(1), seed=3)
    assert sc_w.target.outage_mode == "wide"
    wd = set(sc_w.as_truth()["down_cells"])
    assert S.DISASTER_AREA_CELLS["local"] < len(sc_w.target.cell_keys - wd) <= S.DISASTER_AREA_CELLS["wide"]
    ctx = sc_w.as_truth()["context"][0]["cells"]
    assert set(ctx) == sc_w.target.cell_keys                              # 폭주 지역 목록
    assert wd <= set(ctx)                                                 # 꺼진 셀 목록은 따로, 폭주 지역 안에 있다


# ── _quake_arg ────────────────────────────────────────────────────────────

def test_quake_arg_parses_all_forms():
    assert faults._quake_arg("QUAKE:35.1,139.2") == {"lat": 35.1, "lon": 139.2, "intensity": None, "shape": None}
    assert faults._quake_arg("QUAKE:35.1,139.2,3.5")["intensity"] == 3.5
    a = faults._quake_arg("quake:35.1, 139.2, 3.5, linear")
    assert a == {"lat": 35.1, "lon": 139.2, "intensity": 3.5, "shape": "LINEAR"}
    assert faults._quake_arg("QUAKE:35.1,139.2,ROUND")["shape"] == "ROUND"


@pytest.mark.parametrize("bad", ["QUAKE:", "QUAKE:35.1", "QUAKE:abc,139", "QUAKE:35.1,139.2,x", "QUAKE:35.1,139.2,3,SQUARE"])
def test_quake_arg_rejects_malformed(bad):
    assert faults._quake_arg(bad) is None


# ── disaster_sms_error ────────────────────────────────────────────────────

def _dsc(dead=None):
    sc = SimpleNamespace(kind="natural_disaster", start_minute=0)
    if dead is not None:
        sc.__dict__["_quake_dead"] = set(dead)
    return sc


def _share(sc, minute, ct, n=4000):
    rng = random.Random(1)
    hits = [S.disaster_sms_error(sc, minute, ct, rng) for _ in range(n)]
    return [h for h in hits if h], n


def test_sms_surge_phase_is_mo_only_at_the_surge_share():
    sc = _dsc(dead=["E1"])
    hits, n = _share(sc, 0, "3")                              # 폭주 국면(정지 전)
    assert abs(len(hits) / n - S.DISASTER_SMS_SHARE["surge"]) < 0.02
    assert {h for h in hits} <= set(S.DISASTER_SMS_MO)
    mt, _ = _share(sc, 0, "4")                                # 착신은 음영 전에는 없다
    assert mt == []


def test_sms_down_phase_raises_share_and_adds_mt_unreachable():
    sc = _dsc(dead=["E1"])
    m = S.DISASTER_DOWN_AT + 1
    assert S.disaster_dead_enbs(sc, m) == {"E1"}
    mo, n = _share(sc, m, "3")
    assert abs(len(mo) / n - S.DISASTER_SMS_SHARE["down"]) < 0.02
    mt, n = _share(sc, m, "8")
    assert abs(len(mt) / n - S.DISASTER_SMS_SHARE["down"]) < 0.02
    assert {h for h in mt} <= set(S.DISASTER_SMS_MT)
    assert {c for _f, c in mt} == {"27", "28"}


def test_sms_causes_match_spec_values():
    assert {c for _f, c in S.DISASTER_SMS_MO} == {"42", "22", "192"}


# ── 정지 eNB 의 출처 ──────────────────────────────────────────────────────

def test_dead_enbs_use_the_quake_set_and_only_during_down_phase():
    sc = _dsc(dead=["A", "B"])
    assert S.disaster_dead_enbs(sc, 0) == set()
    assert S.disaster_dead_enbs(sc, S.DISASTER_DOWN_AT) == {"A", "B"}
    assert S.disaster_dead_enbs(sc, S.DISASTER_DOWN_END) == set()


def test_dead_enbs_empty_quake_set_means_no_outage_not_old_rule():
    sc = _dsc(dead=[])                                       # 약한 지진 — 꺼진 eNB 0개
    assert S.disaster_dead_enbs(sc, S.DISASTER_DOWN_AT + 1) == set()


def test_truth_carries_quake_and_down_cells():
    sc = _dsc(dead=["A"])
    sc.__dict__.update(_quake={"intensity": 3.0}, _quake_dead_cells=["A-0", "A-1"],
                       target=S.Target(cell_keys={"A-0"}), id="J-SC-27")
    t = S._disaster_truth(sc)
    assert t["quake"] == {"intensity": 3.0} and t["down_cells"] == ["A-0", "A-1"]
    assert t["down_enbs"] == ["A"]


# ── record_quakes · DDL ───────────────────────────────────────────────────

class _Cur:
    def __init__(self, log):
        self.log = log

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, args=None):
        self.log.append((sql, args))


class _Conn:
    def __init__(self):
        self.log, self.commits = [], 0

    def cursor(self):
        return _Cur(self.log)

    def commit(self):
        self.commits += 1


def _row(fid=7):
    return {"quake_id": f"Q-F{fid}", "fault_id": fid, "source": "SCENARIO", "origin_time": "2026-11-13 10:00:00",
            "hypo_name": "x" * 100, "lat": 35.0, "lon": 139.0, "intensity": 3.0, "shape": "ROUND",
            "azimuth_deg": None, "length_km": None, "radius_km": 6.0, "base_pct": 30.0, "max_cells": 25}


def test_record_quakes_uses_insert_ignore_and_returns_fault_ids():
    c = _Conn()
    assert control.record_quakes(c, [_row(7), _row(8)]) == [7, 8]
    assert len(c.log) == 2 and c.commits == 1
    sql, args = c.log[0]
    assert sql.startswith(f"INSERT IGNORE INTO {control.T_QUAKE}")
    assert sql.count("%s") == len(args) == 17
    assert len(args[4]) == 64                                  # HYPO_NAME 길이 자름
    assert args[0] == "Q-F7" and args[9] == 3.0


def test_record_quakes_empty_is_a_noop():
    c = _Conn()
    assert control.record_quakes(c, []) == [] and c.log == []


def test_quake_ddl_is_one_clean_statement_without_semicolons_in_comments():
    assert control.T_QUAKE == "CM_QUAKE_EVENT"
    a = control.DDL.index(f"CREATE TABLE IF NOT EXISTS `{control.T_QUAKE}`")
    stmt = control.DDL[a:].split(";", 1)[0]
    assert ") ENGINE=InnoDB" in stmt and "COMMENT='지진 정보" in stmt
    for col in ("QUAKE_ID", "FAULT_ID", "SOURCE", "ORIGIN_TIME", "HYPO_NAME", "LAT", "LON", "DEPTH_KM",
                "MAGNITUDE", "INTENSITY", "SHAPE", "AZIMUTH_DEG", "LENGTH_KM", "RADIUS_KM", "BASE_PCT",
                "MAX_CELLS", "UPDATED_TIME"):
        assert f"`{col}`" in stmt, col
    assert "CM_QUAKE_CELL" not in control.DDL
