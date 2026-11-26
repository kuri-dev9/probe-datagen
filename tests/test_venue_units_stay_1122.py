"""2026-11-22 맥스 gen 작업 세 가지의 **새 동작** 시험 — 기존 시험은 옛 동작이 안 깨졌다는 것만 보였다.

가. 23번 자동 대상 = 역 하나 근처 eNB 5~6개(`faults._station_venue`)
나. 대상 형식 `CELL:`·`ST:`·`ADM3:`(`_resolve_target`) · 허용 단위 표(`TARGET_UNITS`) · `/api/scenarios` 의 `units`·`unit_warn`
다. 머무는 셀(`home_locality`) 켠 상태의 주민 몫·반경 · 군중 몫 유지(`Generator.visitor`)

**eNB·셀·역은 전부 합성**이다(PUBLIC 저장소). DB 없음.
"""
from __future__ import annotations

import random
from pathlib import Path

import pytest

from datagen import faults
from datagen import scenarios as S
from datagen.cells import Cell
from datagen.generator import Generator
from datagen.profile import load as load_profiles

DATA = Path(__file__).resolve().parents[1] / "data"
RATES = {"lte_call": 300, "transport": 300, "dns": 100, "http": 100}


def _cell(enb, n, lat, lon, emd="131010"):
    return Cell(cell_key=f"{enb}-{n}", enb_id=str(enb), cell_id=n, enb_seq=0, c_uid="", tac=100, plmn="44010", vlan_id="",
                latitude=lat, longitude=lon, adm_sido_cd="13", adm_sido_nm="", adm_sgg_cd="13101", adm_sgg_nm="",
                adm_emd_cd=emd, adm_emd_nm="", geo_status="MAPPED", rat="EUTRAN", mme_id="0001",
                sgw_id="0001", pgw_id="0001", ims_pgw_id="0001", traffic_weight=1.0, display_name="")


def _line(n_enb=20, per=3):
    """eNB 20개를 위도 방향 일렬로(0.01도 ≈ 1.1km 간격) · 셀 3개씩"""
    return [_cell(100 + i, j, 35.60 + i * 0.01, 139.70 + j * 0.0004) for i in range(n_enb) for j in range(per)]


def _keys(cells, enb):
    return {c.cell_key for c in cells if c.enb_id == str(enb)}


@pytest.fixture
def stations(monkeypatch):
    cells = _line()
    monkeypatch.setattr(faults, "STATION_CELLS", {"A": _keys(cells, 100), "B": _keys(cells, 119), "N": _keys(cells, 110)})
    monkeypatch.setattr(faults, "STATION_NAMES", {"A": "역A", "B": "역B", "N": "역N"})
    monkeypatch.setattr(faults, "DAILY_TOKENS", [])
    return cells


# ── 가. 역 근처 묶음 ─────────────────────────────────────────────────────────

def test_venue_is_five_or_six_enbs_with_whole_enbs(stations):
    sizes = set()
    for seed in range(30):
        t = faults._station_venue(stations, random.Random(seed))
        enbs = {c.enb_id for c in stations if c.cell_key in t.cell_keys}
        sizes.add(len(enbs))
        for e in enbs:                                        # 셀을 덜지 않는다 — eNB 의 셀 전부
            assert _keys(stations, e) <= t.cell_keys
    assert sizes <= {5, 6} and sizes


def test_venue_enb_count_follows_the_config_value(stations, monkeypatch):
    monkeypatch.setattr(S.FAULT, "venue_enbs", (3, 3))
    t = faults._station_venue(stations, random.Random(1))
    assert len({c.enb_id for c in stations if c.cell_key in t.cell_keys}) == 3


def test_venue_is_off_the_station_by_at_least_the_minimum_path_and_never_contains_the_station_enb(stations):
    """11-26 사용자 지시 「역에서 행사장까지 길이 3셀 이상」 — 역 eNB 는 행사장에서 빠지고, 역에서 가까운 eNB 부터 잇되
    걷는 길이 `VENUE_MIN_PATH` 이상인 첫 곳을 고른다(전에는 역 eNB 100 부터 바로 붙은 묶음이었다)."""
    t = faults._station_venue(stations, random.Random(1), only="A")        # 역A = eNB 100 (선 끝)
    enbs = sorted({int(c.enb_id) for c in stations if c.cell_key in t.cell_keys})
    assert 100 not in enbs                                                 # 역 eNB 제외
    assert enbs == list(range(enbs[0], enbs[0] + len(enbs)))               # 이어진 eNB 묶음(역에 가까운 것부터)
    assert enbs[0] - 100 >= faults.VENUE_MIN_PATH                           # 역에서 최소 길이 이상 떨어졌다


def test_venue_avoids_the_daily_load_station_and_its_enb(stations, monkeypatch):
    monkeypatch.setattr(faults, "DAILY_TOKENS", ["ST:N"])
    for seed in range(30):
        t = faults._station_venue(stations, random.Random(seed))
        assert not t.cell_keys & _keys(stations, 110)
        # 역N 자체가 후보에서 빠지므로 대상 eNB 가 110 을 품을 일이 없다
        assert all(c.enb_id != "110" for c in stations if c.cell_key in t.cell_keys)


def test_venue_never_centres_on_a_same_named_other_station(stations, monkeypatch):
    """매일 부하 역과 이름이 같은 다른 역사는 **중심 역 후보**에서 빠진다(그 eNB 가 근처 후보로 드는 것은 별개)"""
    monkeypatch.setattr(faults, "STATION_CELLS", {"N": _keys(stations, 110), "N2": _keys(stations, 105)})
    monkeypatch.setattr(faults, "STATION_NAMES", {"N": "역N", "N2": "역N"})       # 이름이 같은 다른 역사
    monkeypatch.setattr(faults, "DAILY_TOKENS", ["ST:N"])
    assert faults._station_venue(stations, random.Random(1)) is None             # 남은 후보 역이 없다


def test_venue_does_not_use_cells_the_caller_already_removed(stations):
    """활성 장애 셀은 `build` 가 후보 셀에서 eNB 째 덜어 넘긴다 — 덜린 eNB 는 대상이 되지 못한다"""
    left = [c for c in stations if c.enb_id not in ("101", "102")]
    for seed in range(20):
        t = faults._station_venue(left, random.Random(seed))
        assert not t.cell_keys & (_keys(stations, 101) | _keys(stations, 102))


def test_venue_without_a_station_table_gives_none(monkeypatch):
    monkeypatch.setattr(faults, "STATION_CELLS", {})
    assert faults._station_venue(_line(), random.Random(1)) is None


def test_the_auto_target_of_event_crowd_falls_back_to_one_enb_without_the_table(monkeypatch):
    monkeypatch.setattr(faults, "STATION_CELLS", {})
    cells = _line()
    t = faults._resolve_target("", "event_crowd", cells, random.Random(1))
    if t is None:
        pytest.skip("역 표 없이 자동 대상이 서지 않는 구성(부하 표 필요)")
    assert len({c.enb_id for c in cells if c.cell_key in t.cell_keys}) >= 1


# ── 나. 대상 형식 · 허용 단위 표 ─────────────────────────────────────────────

def test_cell_target_is_one_cell(stations):
    t = faults._resolve_target("CELL:105-1", "ran_radio", stations, random.Random(1))
    assert t.cell_keys == {"105-1"}


def test_station_target_is_the_venue_of_that_station(stations):
    t = faults._resolve_target("ST:A", "event_crowd", stations, random.Random(1))
    enbs = {c.enb_id for c in stations if c.cell_key in t.cell_keys}
    assert "100" not in enbs and 5 <= len(enbs) <= 6                       # 11-26 — 역 eNB 는 행사장에서 뺀다(역에서 3셀 이상)


def test_station_target_accepts_the_stn_prefix(stations):
    a = faults._resolve_target("ST:STN-A", "event_crowd", stations, random.Random(1))
    b = faults._resolve_target("ST:A", "event_crowd", stations, random.Random(1))
    assert a.cell_keys == b.cell_keys


def test_adm3_target_is_every_cell_of_the_town(stations, monkeypatch):
    mine = {"104-0", "104-1", "104-2", "105-0"}
    monkeypatch.setattr(faults, "ADM3_CELLS", {"131010": mine})
    t = faults._resolve_target("ADM3:131010", "ran_radio", stations, random.Random(1))
    assert t.cell_keys == mine


@pytest.mark.parametrize("raw", ["CELL:999-9", "ST:없는역", "ADM3:000000"])
def test_missing_targets_are_cancelled(stations, monkeypatch, raw):
    monkeypatch.setattr(faults, "ADM3_CELLS", {})
    assert faults._resolve_target(raw, "ran_radio", stations, random.Random(1)) is None


def test_target_units_match_the_approved_table():
    U = faults.TARGET_UNITS
    for k in ("ran_radio", "ran_congestion", "anchor_saturation", "sa_fallback", "nsa_nr_leg", "ran_drop_surge"):
        assert U[k] == ("CELL", "ENB", "ST", "ADM3", "SGG"), k
    assert U["event_crowd"] == ("ENB", "ST")
    for k in ("transport_loss", "transport_cut", "compound_causal_failure"):
        assert U[k] == ("ADM3", "SGG", "ST"), k
    assert "mobility" not in U and "ho_interruption" not in U        # 노선 그대로 — 사용자 답 대기(11-22)
    assert faults.UNIT_WARN == ("CELL", "ENB")


def test_api_scenarios_carries_units_and_unit_warn(monkeypatch):
    from datagen.ui import faults_api as A

    class _Conn:
        def close(self):
            pass

    rows = [{"kind": "event_crowd"}, {"kind": "ran_radio"}, {"kind": "transport_loss"}, {"kind": "mobility"}]
    monkeypatch.setattr(A, "_connect", lambda: _Conn())
    monkeypatch.setattr(A, "_ensure_once", lambda c: None)
    monkeypatch.setattr(A, "list_scenarios", lambda c: rows)
    got = {r["kind"]: r for r in A.api_scenarios()}
    assert got["event_crowd"]["units"] == ["ENB", "ST"] and got["event_crowd"]["unit_warn"] == ["ENB"]
    assert got["ran_radio"]["unit_warn"] == ["CELL", "ENB"]
    assert got["transport_loss"]["units"] == ["ADM3", "SGG", "ST"] and got["transport_loss"]["unit_warn"] == []
    assert got["mobility"]["units"] == [] and got["mobility"]["unit_warn"] == []


# ── 다. 머무는 셀 ────────────────────────────────────────────────────────────

def _cluster_cells():
    """셀 200개 — 0.002도(≈220m) 격자 10×10 근접 군집 둘(서로 5km 이상): 1km 안은 군집 안, 군집 밖은 반경 밖"""
    out = []
    for g, (la, lo) in enumerate(((35.60, 139.60), (35.70, 139.80))):
        for i in range(10):
            for j in range(10):
                out.append(_cell(1000 + g * 100 + i * 10 + j, 0, la + i * 0.002, lo + j * 0.002))
    return out


@pytest.fixture(scope="module")
def stay_gen():
    if not (DATA / "profile.json").exists():
        pytest.skip("data/profile.json 없음")
    profiles = load_profiles(DATA / "profile.json")
    keep = {s: profiles[s] for s in ("lte_call", "transport", "dns", "http")}
    g = Generator(cells=_cluster_cells(), profiles=keep, scenarios=[], rates=RATES, seed=20260903, diurnal=False,
                  subscriber_pool=4000, success_only=True, end_only=True)
    g.home_locality, g.home_radius_m, g.home_stay_share = True, 1000.0, 0.72
    g._near_cell_cache.clear()
    return g


def test_residents_are_the_cell_dwellers_at_about_the_configured_share(stay_gen):
    g = stay_gen
    idx = next(i for i, p in g._subs_by_cell.items() if len(p) >= 5)
    pool = {g.subs[k].imsi for k in g._subs_by_cell[idx]}
    g.rng = random.Random(7)
    hit = sum(g._sub_for(idx).imsi in pool for _ in range(2000)) / 2000
    assert 0.72 <= hit <= 0.80, hit            # 머무는 몫 0.72 + 주변 추첨이 우연히 같은 셀일 몫


def test_the_rest_comes_from_cells_within_the_radius(stay_gen):
    g = stay_gen
    idx = next(i for i, p in g._subs_by_cell.items() if len(p) >= 5)
    g.rng = random.Random(8)
    from datagen.generator import _dist_m
    here = g.cells[idx]
    far = 0
    for _ in range(1500):
        s = g._sub_for(idx)
        if _dist_m(here, g.cells[s.home_cell]) > 1000.0:
            far += 1
    assert far / 1500 <= 0.02, far             # 반경 밖(다른 군집)은 마지막 수단일 때만


def test_near_cells_are_within_one_km_and_exclude_the_other_cluster(stay_gen):
    g = stay_gen
    from datagen.generator import _dist_m
    near = g.near_cells(0)
    assert near and all(_dist_m(g.cells[0], g.cells[i]) <= 1000.0 for i in near)
    assert all(i < 100 for i in near)          # 0번 셀의 군집 안만


# ── 다. 군중 몫 유지 ─────────────────────────────────────────────────────────

def test_visitor_is_the_extra_share_of_a_boosted_cell(stay_gen):
    g = stay_gen
    c = g.cells[0]
    rng = random.Random(3)
    g._boost = None
    assert not any(g.visitor(c, rng) for _ in range(500))                       # 평시 — 주민만
    g._boost = {c.cell_key: 3.0}
    share = sum(g.visitor(c, rng) for _ in range(6000)) / 6000
    assert abs(share - (1 - 1 / 3.0)) < 0.03, share                            # ×3 이면 2/3 가 찾아온 사람
    g._boost = {c.cell_key: 1.0}
    assert not any(g.visitor(c, rng) for _ in range(500))
    g._boost = None


def test_crowd_people_do_not_thin_out_with_stay_on(stay_gen):
    """행사장 ×3 — 머무는 셀을 켜도 1분 고유 인원이 주민 수에 갇히지 않는다(찾아온 몫은 전체에서 뽑는다)"""
    g = stay_gen
    c = g.cells[0]
    idx = g.cell_index[c.cell_key]
    n = 600
    g.rng = random.Random(11)
    g._boost = None
    stay = len({g._local_sub(c).imsi if g._local_sub(c) else None for _ in range(n)} - {None})
    g._boost = {c.cell_key: 3.0}
    g.rng = random.Random(11)
    crowd_calls = [g._local_sub(c) for _ in range(n * 3)]
    visitors = sum(s is None for s in crowd_calls)                             # None = 호출부가 전체에서 뽑는다
    assert visitors / (n * 3) > 0.55, visitors
    assert stay >= 1 and idx == 0
    g._boost = None
