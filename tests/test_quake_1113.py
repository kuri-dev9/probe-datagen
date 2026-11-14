"""27번 지진 — 진원역 → 규모·깊이 → 셀별 진도 → 폭주·정지 (2026-11-14 사용자 결정 B · 에스터 시험).

전(11-13)에는 진도(2.0~4.0)를 뽑아 표(`QUAKE_TABLE`)로 반경·정지 확률을 정하고 진앙 둘레를 껐다. 진앙이 도쿄 한가운데였고
진도 2.8 에 eNB 6곳이 꺼졌다(F-332). 이제 **실제 진원역 4곳** 중 하나에서 지진 한 건(규모·깊이)을 정하고, 셀마다 진원
거리로 진도를 계산해(graphio EV-19 와 같은 식 + eNB 지반 증폭률 ARV) **폭주는 진도 3 이상 · 정지는 진도 4.5(5약) 이상**
에서만 만든다. 값(정지 확률 · 최대 셀)은 초기값이라 이 시험은 값 자체보다 **관계**를 고정한다. DB 를 쓰지 않는다(가짜 연결).
옛 진도표·반경·LINEAR 모양·SMS 시험은 그 동작이 걷혀 이 파일에서 뺐다(`QUAKE_DOWN` 쪽 옛 `earthquake` 재생 경로는 그대로다).
"""
from __future__ import annotations

import random
from datetime import datetime
from types import SimpleNamespace

import pytest

from datagen import control, faults
from datagen import scenarios as S
from datagen.cells import Cell
from datagen.geo_lite import haversine_km

REGIONS = {r[0]: r for r in S.FAULT.quake_regions}


def _cell(enb: str, n: int, lat: float, lon: float, geo: str = "MAPPED") -> Cell:
    return Cell(
        cell_key=f"{enb}-{n}", enb_id=enb, cell_id=n, enb_seq=0, c_uid="",
        tac=100, plmn="44010", vlan_id="", latitude=lat, longitude=lon,
        adm_sido_cd="11", adm_sido_nm="", adm_sgg_cd="11200", adm_sgg_nm="",
        adm_emd_cd="11200011", adm_emd_nm="", geo_status=geo,
        rat="EUTRAN", mme_id="0001", sgw_id="0037", pgw_id="0037",
        ims_pgw_id="0037", traffic_weight=1.0, display_name="")


def _grid(lat0: float = 35.0, lon0: float = 139.0) -> list[Cell]:
    """eNB 100개 × 3셀 — 0.01도(약 1km) 간격 격자. 중심 (lat0, lon0) 둘레에 촘촘하다."""
    out = []
    for i in range(100):
        la, lo = lat0 + (i // 10 - 5) * 0.01, lon0 + (i % 10 - 5) * 0.01
        out += [_cell(f"{800000 + i}", n, la + n * 0.0005, lo) for n in range(3)]
    return out


def _q(**kw) -> dict:
    """격자 한가운데에서 난 얕은 M6.2 — 진도 5 안팎(지반 1.0). 키는 `quake_new` 가 주는 것과 같은 뜻이다."""
    q = {"lat": 35.0, "lon": 139.0, "depth_km": 10.0, "magnitude": 6.2, "fault_type_d": 0.12}
    q.update(kw)
    return q


def _weak(**kw) -> dict:
    """같은 자리 M5.4 · 깊이 20km — 격자 전체가 진도 4.0 안팎(지반 1.0 이면 4.5 미만 · 증폭 2.5 면 4.7 이상)."""
    return _q(magnitude=5.4, depth_km=20.0, **kw)


def _arv_file(tmp_path, table: dict[str, float]):
    p = tmp_path / "arv.csv"
    p.write_text("enb_id,arv\n" + "".join(f"{e},{v}\n" for e, v in table.items()), encoding="utf-8")
    return str(p)


@pytest.fixture
def down_cfg(monkeypatch):
    """정지 확률 100% · 최대 셀 크게 — 후보(진도 4.5 이상)가 전부 꺼지는지로 **문턱**만 본다."""
    monkeypatch.setattr(S.FAULT, "quake_down_pct", 100.0)
    monkeypatch.setattr(S.FAULT, "quake_down_max_cells", 300)


# ── 1 · 진앙 — 늘 진원역 안 ───────────────────────────────────────────────────────────────

def test_region_table_is_the_four_real_source_regions():
    assert [r[0] for r in S.FAULT.quake_regions] == ["千葉県東方沖", "千葉県北西部", "茨城県南部", "神奈川・埼玉西部"]
    assert [(r[1], r[2], r[3], r[4]) for r in S.FAULT.quake_regions] == [
        (35.65, 140.64, 44.0, -0.02), (35.58, 140.14, 64.0, 0.12),
        (36.06, 139.93, 65.0, 0.12), (35.56, 139.33, 83.0, 0.12)]
    assert (S.FAULT.quake_jitter_deg, S.FAULT.quake_jitter_depth_km, S.FAULT.quake_magnitude) == (0.1, 10.0, (4.5, 6.2))


def test_the_epicenter_is_always_inside_one_of_the_four_regions_and_never_central_tokyo():
    seen = set()
    for s in range(300):
        q = S.quake_new(random.Random(f"quake:{s}"))
        r = REGIONS[q["region"]]
        seen.add(q["region"])
        assert abs(q["lat"] - r[1]) <= 0.1 + 1e-6 and abs(q["lon"] - r[2]) <= 0.1 + 1e-6, (s, q)
        assert abs(q["depth_km"] - r[3]) <= 10.0 + 0.05 and q["depth_km"] >= 1.0
        assert q["fault_type_d"] == r[4]
        assert 4.5 <= q["magnitude"] <= 6.2 and q["magnitude"] == round(q["magnitude"], 1)
        # 도쿄역(35.681, 139.767)에서 20km 이상 — 도쿄 한가운데가 아니다(가장 가까운 진원역이 흩어져도 약 27km)
        assert haversine_km(35.681, 139.767, q["lat"], q["lon"]) > 20.0
    assert seen == set(REGIONS), "300회에 네 진원역이 모두 나와야 한다"


def test_new_is_seed_stable_and_given_values_win():
    a = S.quake_new(random.Random("quake:7"))
    assert a == S.quake_new(random.Random("quake:7"))
    q = S.quake_new(random.Random(1), region="B", magnitude=5.5, depth_km=70.0)
    assert q["region"] == "千葉県北西部" and q["magnitude"] == 5.5 and q["depth_km"] == 70.0
    p = S.quake_new(random.Random(1), lat=35.60, lon=139.40)            # 좌표 지정 → 가까운 진원역(D)의 d
    assert (p["lat"], p["lon"]) == (35.6, 139.4) and p["region"] == "神奈川・埼玉西部" and p["fault_type_d"] == 0.12
    assert S.quake_region(name="千葉県東方沖")[0] == "千葉県東方沖" and S.quake_region(name="a")[0] == "千葉県東方沖"
    with pytest.raises(ValueError):
        S.quake_region(name="Z")


def test_the_event_row_always_carries_magnitude_and_depth_and_the_epicenter_intensity():
    """graphio EV-19 가 MAGNITUDE·DEPTH_KM 를 쓴다 — 늘 채운다. INTENSITY 는 진앙(지표 거리 0 · 지반 1.0)의 계산 진도."""
    for s in range(60):
        q = S.quake_new(random.Random(f"quake:{s}"))
        assert q["magnitude"] is not None and q["depth_km"] is not None
        assert q["intensity"] == round(S.quake_intensity(q["magnitude"], q["depth_km"], 0.0, q["fault_type_d"]), 1)
        assert q["shape"] == S.QUAKE_SHAPE == "ROUND" and q["azimuth_deg"] is None and q["length_km"] is None


# ── 2 · 셀별 진도 — graphio EV-19 와 같은 식 ──────────────────────────────────────────────

GOLDEN = [  # (규모, 깊이, 진앙 거리, d) → 진도. graphio `evidence.quake_intensity` 가 같은 값을 낸다(그쪽 시험이 같은 표를 고정)
    ((6.0, 75.0, 33.112, 0.12), 3.801329888569895),
    ((5.0, 40.0, 10.0, 0.0), 3.0314042372295047),
    ((6.2, 10.0, 0.0, 0.12), 5.194781251487208),
    ((4.5, 83.0, 120.0, -0.02), 1.4884647342322956),
]


@pytest.mark.parametrize("args,want", GOLDEN)
def test_intensity_equals_the_graphio_ev19_formula(args, want):
    assert S.quake_intensity(*args) == pytest.approx(want, abs=1e-9)


def test_arv_multiplies_the_velocity_before_the_conversion():
    base = S.quake_intensity(5.0, 30.0, 10.0, 0.0)
    assert S.quake_intensity(5.0, 30.0, 10.0, 0.0, arv=1.0) == base
    assert S.quake_intensity(5.0, 30.0, 10.0, 0.0, arv=2.0) == pytest.approx(base + 1.72 * 0.30103, abs=1e-3)


def test_cell_intensity_uses_the_epicenter_distance_and_the_enb_arv():
    cells = _grid()
    q = _q()
    arv = {"800000": 2.0}
    inten = S.quake_cell_intensity(cells, q, arv)
    c = cells[0]
    want = S.quake_intensity(q["magnitude"], q["depth_km"], haversine_km(q["lat"], q["lon"], c.latitude, c.longitude),
                             q["fault_type_d"], 2.0)
    assert inten[c.cell_key] == pytest.approx(want)
    other = cells[-1]
    assert inten[other.cell_key] == pytest.approx(S.quake_intensity(
        q["magnitude"], q["depth_km"], haversine_km(q["lat"], q["lon"], other.latitude, other.longitude), q["fault_type_d"]))
    unmapped = _cell("999999", 0, 0.0, 0.0, geo="UNMAPPED")
    assert unmapped.cell_key not in S.quake_cell_intensity(cells + [unmapped], q, arv)    # 좌표 없는 셀은 모른다


# ── 3 · 지반 표 — 없으면 1.0 · 정지 0곳 / 있으면 4.5 이상만 ─────────────────────────────────

def test_without_a_ground_table_arv_is_empty_and_nothing_goes_down(monkeypatch, down_cfg):
    monkeypatch.setattr(S.FAULT, "quake_arv_path", "")
    assert S.quake_arv() == {}
    cells = _grid()
    area, dead, dc = S.quake_plan(cells, _weak(), random.Random(0), 25)           # 확률 100% 여도 후보가 없다
    assert dead == [] and dc == [] and len(area) == 25                               # 폭주만 — 0곳이어도 정상이다
    inten = S.quake_cell_intensity(cells, _weak(), {})
    assert max(inten.values()) < S.FAULT.quake_down_intensity


def test_a_missing_table_file_warns_and_falls_back_to_one(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(S.FAULT, "quake_arv_path", str(tmp_path / "없는-표.csv"))
    assert S.quake_arv() == {}
    assert "지반 증폭률 표를 못 읽었다" in capsys.readouterr().out


def test_with_a_ground_table_only_enbs_at_or_above_4_5_go_down(monkeypatch, tmp_path, down_cfg):
    cells = _grid()
    enbs = sorted({c.enb_id for c in cells})
    soft = set(enbs[::2])                                                          # 무른 땅 — 증폭률 2.5
    arv = {e: 2.5 for e in soft}
    q = _weak()
    area, dead, dc = S.quake_plan(cells, q, random.Random(0), 25, arv)
    by_enb: dict[str, float] = {}
    for k, v in S.quake_cell_intensity(cells, q, arv).items():
        e = k.rsplit("-", 1)[0]
        by_enb[e] = max(by_enb.get(e, 0.0), v)
    eligible = {e for e, v in by_enb.items() if v >= S.FAULT.quake_down_intensity}
    assert eligible and eligible < set(enbs), "시험 설정이 문턱 양쪽에 eNB 를 가져야 한다"
    assert set(dead) == eligible                                                   # 확률 100% — 후보 전부, 그리고 후보만
    assert set(dead) <= soft                                                       # 단단한 땅(증폭 1.0)은 4.5 에 못 닿는다
    per = {}
    for c in cells:
        per.setdefault(c.enb_id, set()).add(c.cell_key)
    assert set(dc) == {k for e in dead for k in per[e]} and set(dc) <= area        # eNB 째 꺼지고 정답 지역에 든다


def test_the_down_threshold_is_exactly_4_5_and_the_probability_and_cap_apply(monkeypatch):
    cells = _grid()
    q = _weak()
    arv = {c.enb_id: 2.5 for c in cells}
    monkeypatch.setattr(S.FAULT, "quake_down_pct", 0.0)
    assert S.quake_plan(cells, q, random.Random(0), 25, arv)[1] == []              # 확률 0 이면 후보여도 안 꺼진다
    monkeypatch.setattr(S.FAULT, "quake_down_pct", 100.0)
    monkeypatch.setattr(S.FAULT, "quake_down_max_cells", 7)
    _a, dead, dc = S.quake_plan(cells, q, random.Random(0), 25, arv)
    assert dead and len(dc) <= 7 and len(dc) == 3 * len(dead)                      # 최대 셀을 넘기는 eNB 는 건너뛴다(3셀씩)
    monkeypatch.setattr(S.FAULT, "quake_down_max_cells", 300)
    monkeypatch.setattr(S.FAULT, "quake_down_intensity", 99.0)
    assert S.quake_plan(cells, q, random.Random(0), 25, arv)[1] == []              # 문턱을 올리면 0곳


# ── 4 · 폭주 — 진도가 높은 셀부터 ────────────────────────────────────────────────────────

@pytest.mark.parametrize("want", (25, 80))
def test_surge_cells_are_the_highest_intensity_cells_not_the_nearest(want, monkeypatch):
    monkeypatch.setattr(S.FAULT, "quake_down_pct", 0.0)
    cells = _grid()
    q = _weak()
    far_soft = {c.enb_id: 1.8 for c in cells if haversine_km(35.0, 139.0, c.latitude, c.longitude) > 4.0}
    area, dead, _ = S.quake_plan(cells, q, random.Random(0), want, far_soft)
    assert dead == [] and len(area) == want
    inten = S.quake_cell_intensity(cells, q, far_soft)
    assert min(inten[k] for k in area) >= max(v for k, v in inten.items() if k not in area) - 1e-12
    assert any(haversine_km(35.0, 139.0, c.latitude, c.longitude) > 4.0 for c in cells if c.cell_key in area), \
        "무른 땅의 먼 셀이 가까운 단단한 셀보다 먼저 들어와야 거리순이 아니라 진도순임이 보인다"


def test_surge_needs_intensity_three_or_more_and_is_empty_when_the_quake_is_too_small():
    cells = _grid()
    tiny = _q(magnitude=4.5, depth_km=60.0, lat=40.0, lon=145.0)                    # 격자에서 수백 km
    assert S.quake_plan(cells, tiny, random.Random(0), 25, {}) == (set(), [], [])
    inten = S.quake_cell_intensity(cells, tiny, {})
    assert max(inten.values()) < S.FAULT.quake_surge_intensity


def test_the_area_is_the_surge_cells_plus_the_whole_dead_enbs(down_cfg):
    cells = _grid()
    arv = {c.enb_id: 2.5 for c in cells[:60]}
    area, dead, dc = S.quake_plan(cells, _weak(), random.Random(3), 25, arv)
    assert dead and set(dc) <= area
    assert 25 <= len(area) <= 25 + len(dc) and len(area - set(dc)) <= 25


# ── 5 · 규모 하한 — 폭주 셀 want 개가 서는 가장 작은 규모 ─────────────────────────────────────

def _count(cells, lat, lon, depth, d, m):
    return sum(1 for c in cells if S.quake_intensity(m, depth, haversine_km(lat, lon, c.latitude, c.longitude), d)
               >= S.FAULT.quake_surge_intensity)


def test_magnitude_floor_is_the_smallest_magnitude_that_raises_want_surge_cells():
    cells = _grid(35.6, 139.8)                       # 도쿄 쪽 격자 — 진원역에서 수십 km
    mid = 0
    for reg in S.FAULT.quake_regions:
        _n, lat, lon, depth, d = reg
        f = S.quake_mag_floor(cells, 25, {})(lat, lon, depth, d)
        lo, hi = S.FAULT.quake_magnitude
        if f is None:
            assert _count(cells, lat, lon, depth, d, lo) >= 25                       # 범위 시작부터 선다
        elif f > hi:
            assert _count(cells, lat, lon, depth, d, hi) < 25                        # 범위 끝에서도 안 선다
        else:
            mid += 1
            assert _count(cells, lat, lon, depth, d, f) >= 25
            assert _count(cells, lat, lon, depth, d, round(f - 0.1, 1)) < 25         # 한 칸 아래는 안 선다 = 하한
            assert f == round(f, 1)
    assert mid >= 1, "시험 설정에 하한이 범위 안에 있는 진원역이 있어야 한다"


def test_quake_new_never_draws_below_the_floor_and_returns_none_above_the_range():
    cells = _grid(35.6, 139.8)
    reg = S.FAULT.quake_regions[0]                    # 千葉県東方沖 — 도쿄에서 가장 멀다
    floor = S.quake_mag_floor(cells, 25, {})
    for s in range(80):
        q = S.quake_new(random.Random(f"quake:{s}"), region=reg[0], mag_floor=floor)
        if q is None:
            continue
        assert q["magnitude"] >= (floor(q["lat"], q["lon"], q["depth_km"], q["fault_type_d"]) or 0) - 1e-9
    assert S.quake_new(random.Random(0), region=reg[0], mag_floor=lambda *a: 9.9) is None


# ── 6 · 빌드 — 정답지 · 결론 코드 · `QUAKE:` 지정 ─────────────────────────────────────────────

def _fault(target, fid=27):
    return {"fault_id": fid, "kind": "natural_disaster", "target": target,
            "start_ts": datetime(2026, 11, 14, 10, 0), "duration_min": 25, "intensity": 1.0}


def _build(target, fid=27, cells=None):
    return faults.build(_fault(target, fid), cells or _grid(35.6, 140.0), datetime(2026, 11, 14, 9, 0),
                        random.Random(1), seed=3)


def test_built_truth_is_the_earthquake_with_a_full_event_row_and_cell_intensities():
    sc = _build("QUAKE:B,6.0")
    assert sc is not None
    t = sc.as_truth()
    assert t["truth_code"] == "EARTHQUAKE_IMPACT" and t["truth_category"] == "EXTERNAL"
    q = t["quake"]
    assert q["region"] == "千葉県北西部" == q["hypo_name"] and q["magnitude"] == 6.0
    assert q["depth_km"] is not None and q["fault_type_d"] == 0.12 and q["source"] == "SCENARIO"
    assert q["intensity"] == round(S.quake_intensity(6.0, q["depth_km"], 0.0, 0.12), 1)
    ci = t["cell_intensity"]
    assert ci and set(ci) <= sc.target.cell_keys
    assert all(v >= S.FAULT.quake_surge_intensity - 1e-9 for v in ci.values())          # 정답 지역은 모두 진도 3 이상
    assert set(t["context"][0]["cells"]) == sc.target.cell_keys                     # 정답 지역 = 폭주 셀 + 꺼진 셀


def test_every_auto_built_event_row_has_magnitude_and_depth_and_lands_in_a_region():
    got = 0
    for fid in range(40):
        sc = _build(None, fid=fid)
        if sc is None:                                                              # 폭주 셀이 안 서는 회차는 걸지 않는다
            continue
        got += 1
        q = sc.as_truth()["quake"]
        r = REGIONS[q["region"]]
        assert q["magnitude"] is not None and q["depth_km"] is not None
        assert abs(q["lat"] - r[1]) <= 0.1 + 1e-6 and abs(q["lon"] - r[2]) <= 0.1 + 1e-6
    assert got >= 20


def test_quake_arg_parses_the_new_forms():
    assert faults._quake_arg("QUAKE:B") == {"region": "B", "lat": None, "lon": None, "magnitude": None, "depth_km": None}
    assert faults._quake_arg("QUAKE:千葉県東方沖,5.5")["magnitude"] == 5.5
    a = faults._quake_arg("quake:35.1, 139.2, 5.8, 60")
    assert a == {"region": None, "lat": 35.1, "lon": 139.2, "magnitude": 5.8, "depth_km": 60.0}
    assert faults._quake_arg("QUAKE:35.1,139.2")["magnitude"] is None


@pytest.mark.parametrize("bad", ["QUAKE:", "QUAKE:Z", "QUAKE:35.1", "QUAKE:abc,139", "QUAKE:35.1,139.2,x",
                                 "QUAKE:35.1,139.2,5,60,9", "QUAKE:B,5.5,6"])
def test_quake_arg_rejects_malformed(bad):
    try:
        assert faults._quake_arg(bad) is None
    except ValueError:
        pass                                           # 모르는 진원역 이름은 ValueError — `build` 가 거부한다


def test_a_malformed_quake_target_builds_nothing():
    assert _build("QUAKE:Z") is None


def test_wide_mode_keeps_the_eighty_cell_cap():
    sc = _build("WIDE")
    if sc is None:
        pytest.skip("이 격자·시드에서는 폭주 80셀이 서지 않는다")
    dead = set(sc.as_truth()["down_cells"])
    assert sc.target.outage_mode == "wide"
    assert S.DISASTER_AREA_CELLS["local"] < len(sc.target.cell_keys - dead) <= S.DISASTER_AREA_CELLS["wide"]


# ── 7 · 문자(SMS) — 27번에서 뺐다 ─────────────────────────────────────────────────────────

def test_the_sms_injection_is_gone_from_the_scenario_module():
    for name in ("disaster_sms_error", "DISASTER_SMS_SHARE", "DISASTER_SMS_MO", "DISASTER_SMS_MT"):
        assert not hasattr(S, name), name
    for name in ("QUAKE_TABLE", "quake_params", "quake_dist_km", "QUAKE_SHAPES", "QUAKE_LINEAR_LEN_X"):
        assert not hasattr(S, name), f"옛 진도표·모양 상수 {name} 가 남아 있다"


def _dsc(dead=None):
    sc = SimpleNamespace(kind="natural_disaster", start_minute=0)
    if dead is not None:
        sc.__dict__["_quake_dead"] = set(dead)
    return sc


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
