"""27번 지진 — 정지 eNB 고르기(11-15 A안 → 11-16 가안으로 바뀜)와 영향 지역 API (맥스 구현 · 에스터 시험).

**11-16 가안**: 정지는 후보 eNB 마다 확률 `p=min(1, pct/100×10^((I−4.5)/1.72))` 로 끈다(상한 없음이 기본). 새 규칙의 시험은
`test_quake_down_prob_1116.py`. 이 파일의 정지 선택 시험은 그 규칙(상한은 `cap` 을 줄 때만)에 맞췄고, 영향 API 시험은 그대로다.

(아래는 11-15 A안 설명 — 지금은 확률 관문이 다시 쓰인다)

A안: 진도 5약(`quake_down_intensity`) 이상 후보 전체를 **흔들림에 비례한 가중 무작위 순서**(`10^((I−4.5)/1.72)` ·
비복원)로 줄 세워 `quake_down_max_cells` 가 찰 때까지 끈다. `quake_down_pct` 관문은 걷었다(개수는 상한까지).
영향 API: 생성 범위 eNB 마다 추정 진도(진도 3 이상) — 화면이 옅은 띠로 넓게 칠한다. DB 를 쓰지 않는다(가짜 연결).
"""
from __future__ import annotations

import random
import re
from pathlib import Path

import pytest

from datagen import scenarios as S
from datagen.cells import Cell
from datagen.ui import map_api

ROOT = Path(__file__).resolve().parents[1]


def _cell(enb: str, n: int, lat: float, lon: float) -> Cell:
    return Cell(
        cell_key=f"{enb}-{n}", enb_id=enb, cell_id=n, enb_seq=0, c_uid="",
        tac=100, plmn="44010", vlan_id="", latitude=lat, longitude=lon,
        adm_sido_cd="11", adm_sido_nm="", adm_sgg_cd="11200", adm_sgg_nm="",
        adm_emd_cd="11200011", adm_emd_nm="", geo_status="MAPPED",
        rat="EUTRAN", mme_id="0001", sgw_id="0037", pgw_id="0037",
        ims_pgw_id="0037", traffic_weight=1.0, display_name="")


def _grid(n_enb: int = 100, per: int = 3) -> list[Cell]:
    out = []
    for i in range(n_enb):
        la, lo = 35.0 + (i // 10 - 5) * 0.01, 139.0 + (i % 10 - 5) * 0.01
        out += [_cell(f"{800000 + i}", n, la + n * 0.0005, lo) for n in range(per)]
    return out


def _q(**kw) -> dict:
    q = {"lat": 35.0, "lon": 139.0, "depth_km": 20.0, "magnitude": 5.4, "fault_type_d": 0.12}
    q.update(kw)
    return q


@pytest.fixture
def cfg(monkeypatch):
    monkeypatch.setattr(S.FAULT, "quake_down_pct", 30.0)
    monkeypatch.setattr(S.FAULT, "quake_down_max_cells", 40)
    return S.FAULT


# ══ 1 · quake_plan A안 ═══════════════════════════════════════════════════════════════════════

def test_same_seed_gives_the_same_down_enbs_and_different_seeds_give_different_ones(cfg):
    cells, arv = _grid(), {c.enb_id: 2.5 for c in _grid()}
    a = S.quake_plan(cells, _q(), random.Random(7), 25, arv)
    assert a == S.quake_plan(cells, _q(), random.Random(7), 25, arv)                 # 결정론 — 장애 ID 시드
    others = {tuple(S.quake_plan(cells, _q(), random.Random(k), 25, arv)[1]) for k in range(20)}
    assert len(others) > 10, "시드마다 다른 eNB 가 꺼져야 한다(항상 같은 곳이면 A안이 아니다)"


def test_input_order_does_not_change_the_choice(cfg):
    """후보를 정렬해 난수를 뽑는다 — 셀 목록 순서에 기대면 같은 시드라도 다른 eNB 가 꺼진다."""
    cells, arv = _grid(), {c.enb_id: 2.5 for c in _grid()}
    a = S.quake_plan(cells, _q(), random.Random(3), 25, arv)
    shuffled = cells[:]
    random.Random(1).shuffle(shuffled)
    assert S.quake_plan(shuffled, _q(), random.Random(3), 25, arv)[1:] == a[1:]


def test_only_candidates_at_or_above_the_down_intensity_ever_go_down(cfg):
    cells = _grid()
    enbs = sorted({c.enb_id for c in cells})
    arv = {e: (2.5 if i % 2 == 0 else 1.0) for i, e in enumerate(enbs)}              # 절반은 4.5 아래
    inten = S.quake_cell_intensity(cells, _q(), arv)
    by = {}
    for k, v in inten.items():
        by[k.rsplit("-", 1)[0]] = max(by.get(k.rsplit("-", 1)[0], 0.0), v)
    cand = {e for e, v in by.items() if v >= cfg.quake_down_intensity}
    assert cand and cand < set(enbs)
    for seed in range(30):
        assert set(S.quake_plan(cells, _q(), random.Random(seed), 25, arv)[1]) <= cand


def test_cells_never_exceed_the_cap_and_fill_it_when_every_candidate_is_certain(cfg, monkeypatch):
    """11-16 — 상한은 값을 줄 때만 있다. pct 100 이면 p=1 이라 후보 순서대로 상한까지 채운다(3 의 배수까지)."""
    monkeypatch.setattr(S.FAULT, "quake_down_pct", 100.0)
    cells, arv = _grid(), {c.enb_id: 2.5 for c in _grid()}                          # 후보 100곳 · 300셀
    for cap in (3, 7, 40, 41, 299, 300, 500):
        monkeypatch.setattr(S.FAULT, "quake_down_max_cells", cap)
        _a, dead, dc = S.quake_plan(cells, _q(), random.Random(cap), 25, arv)
        assert len(dc) <= cap and len(dc) == 3 * len(dead)
        assert len(dc) == min(cap // 3 * 3, 300), f"cap={cap} 에서 채우지 못했다: {len(dc)}"

def test_an_enb_that_would_overflow_is_skipped_and_a_smaller_one_still_fits(cfg, monkeypatch):
    """3셀 eNB 와 1셀 eNB 가 섞여 있고 상한 4 — 넘는 eNB 는 건너뛰고 남은 작은 eNB 로 4 를 채운다(p=1 이라 결정적)."""
    monkeypatch.setattr(S.FAULT, "quake_down_pct", 100.0)
    cells = []
    for i in range(5):
        cells += [_cell(f"{810000 + i}", n, 35.0 + i * 0.01, 139.0) for n in range(3)]
    for i in range(6):
        cells += [_cell(f"{820000 + i}", 0, 35.0 + i * 0.01, 139.05)]
    arv = {c.enb_id: 2.5 for c in cells}
    monkeypatch.setattr(S.FAULT, "quake_down_max_cells", 4)
    for seed in range(20):
        _a, dead, dc = S.quake_plan(cells, _q(), random.Random(seed), 25, arv)
        assert len(dc) == 4

def test_the_down_probability_is_used_again_scaled_by_the_shaking(cfg, monkeypatch):
    """11-15 A안은 pct 를 걷었지만 11-16 가안이 되살렸다 — pct 0 이면 아무도 안 꺼지고 100 이면 후보가 모두 꺼진다."""
    monkeypatch.setattr(S.FAULT, "quake_down_max_cells", None)                         # 이 파일의 cfg 는 상한 40 을 준다 — 비운다
    cells, arv = _grid(), {c.enb_id: 2.5 for c in _grid()}
    monkeypatch.setattr(S.FAULT, "quake_down_pct", 0.0)
    assert S.quake_plan(cells, _q(), random.Random(5), 25, arv)[1] == []
    monkeypatch.setattr(S.FAULT, "quake_down_pct", 100.0)
    assert len(S.quake_plan(cells, _q(), random.Random(5), 25, arv)[1]) == 100

def test_no_candidates_means_zero_down_enbs_and_surge_only(cfg):
    cells = _grid()
    area, dead, dc = S.quake_plan(cells, _q(), random.Random(0), 25, {})              # 지반 1.0 — 4.5 아래
    assert dead == [] and dc == [] and len(area) == 25
    assert S.quake_plan(cells, _q(magnitude=2.0), random.Random(0), 25, {}) == (set(), [], [])


def test_higher_intensity_enbs_go_down_more_often(cfg, monkeypatch):
    """확률 p ∝ 10^((I−4.5)/1.72): 증폭 10 대 2.5(진도 1.03 차 → 4배) 인 두 무리 중 센 쪽이 이론 비율대로 더 꺼진다."""
    monkeypatch.setattr(S.FAULT, "quake_down_max_cells", None)
    cells = _grid(40)
    enbs = sorted({c.enb_id for c in cells})
    strong = set(enbs[:20])
    arv = {e: (6.0 if e in strong else 2.5) for e in enbs}
    inten = S.quake_cell_intensity(cells, _q(), arv)
    by = {}
    for k, v in inten.items():
        e = k.rsplit("-", 1)[0]
        by[e] = max(by.get(e, 0.0), v)
    p = {e: min(1.0, 0.30 * 10 ** ((v - 4.5) / S.QUAKE_I_SLOPE)) for e, v in by.items() if v >= 4.5}
    assert set(p) == set(enbs) and sum(p[e] for e in strong) > sum(p[e] for e in enbs if e not in strong)
    n_s = n_w = 0
    seeds = 400
    for seed in range(seeds):
        dead = set(S.quake_plan(cells, _q(), random.Random(seed), 25, arv)[1])
        n_s += len(dead & strong)
        n_w += len(dead - strong)
    exp_s, exp_w = seeds * sum(p[e] for e in strong), seeds * sum(p[e] for e in enbs if e not in strong)
    assert abs(n_s - exp_s) < 0.05 * exp_s and abs(n_w - exp_w) < 0.05 * exp_w and n_s > n_w

def test_the_down_enbs_are_spread_not_clustered_at_the_top_intensity(cfg, monkeypatch):
    """확률이 진도에 비례해 오르지만 1 이 아니면 약간 낮은 후보도 꺼진다(가장 센 곳에만 몰리지 않는다)."""
    cells = _grid()
    enbs = sorted({c.enb_id for c in cells})
    arv = {e: 1.0 + 0.04 * i for i, e in enumerate(enbs)}                              # 진도가 eNB 순으로 점점 높다
    inten = S.quake_cell_intensity(cells, _q(), arv)
    by = {}
    for k, v in inten.items():
        e = k.rsplit("-", 1)[0]
        by[e] = max(by.get(e, 0.0), v)
    cand = sorted((e for e, v in by.items() if v >= S.FAULT.quake_down_intensity), key=lambda e: -by[e])
    assert len(cand) > 30
    top = set(cand[:13])
    picked = set()
    for seed in range(40):
        picked |= set(S.quake_plan(cells, _q(), random.Random(seed), 25, arv)[1])
    assert len(picked - top) > 10

def test_the_slope_constant_is_the_one_the_intensity_formula_uses():
    src = (ROOT / "src" / "datagen" / "scenarios.py").read_text(encoding="utf-8")
    assert "QUAKE_I_SLOPE = 1.72" in src
    plan = src[src.index("def quake_plan"):src.index("def quake_mag_floor")]
    assert "1.72" not in plan.split('"""')[2] and "QUAKE_I_SLOPE" in plan             # 선택 코드는 상수를 쓴다(docstring 설명은 예외)
    assert S.quake_intensity.__module__ == S.__name__
    # 진도 식이 상수로 계산하는지 — 진도 차 1.72 는 지반 속도 10 배
    a, b = S.quake_intensity(6.0, 10.0, 5.0, 0.0, 1.0), S.quake_intensity(6.0, 10.0, 5.0, 0.0, 10.0)
    assert abs((b - a) - S.QUAKE_I_SLOPE) < 1e-9


def test_the_config_comment_and_pct_value_describe_the_probability_rule():
    text = (ROOT / "config" / "datagen.yaml").read_text(encoding="utf-8")
    assert "quake_down_pct: 30" in text and "quake_down_max_cells: ~" in text
    assert "사용자 가안" in text and "상한 없음" in text
    assert "가안" in S.quake_plan.__doc__ and "상한 대신 흔들림 비례 확률" in S.quake_plan.__doc__

# ══ 2 · 영향 API ═════════════════════════════════════════════════════════════════════════════

class _Cur:
    def __init__(self, c):
        self.c, self._last = c, None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.c.log.append((sql, list(params or [])))
        if "CM_CELL_GEO_MAP" in sql:
            self._last = "radius"
        else:
            self._last = "quake"

    def fetchone(self):
        return self.c.quake

    def fetchall(self):
        return self.c.radius


class _Conn:
    def __init__(self, quake, radius):
        self.quake, self.radius, self.log, self.closed = quake, radius, [], False

    def cursor(self):
        return _Cur(self)

    def close(self):
        self.closed = True


@pytest.fixture
def impact(monkeypatch):
    map_api._IMPACT_CACHE.clear()
    cells = [_cell("1", 0, 35.7, 139.8), _cell("1", 1, 35.7, 139.8), _cell("2", 0, 35.7, 139.9),
             _cell("3", 0, 35.7, 140.0), _cell("4", 0, 35.7, 140.1), _cell("5", 0, 35.7, 140.2),
             _cell("6", 0, 35.7, 140.3)]
    inten = {"1-0": 3.1, "1-1": 4.6, "2-0": 3.0, "3-0": 3.5, "4-0": 4.49, "5-0": 4.5, "6-0": 2.99}
    calls = []

    def fake_inten(cs, q, arv):
        calls.append(q)
        return dict(inten)
    monkeypatch.setattr(S, "quake_cell_intensity", fake_inten)
    monkeypatch.setattr(S, "quake_arv", lambda *a, **k: {"1": 1.0})
    monkeypatch.setattr(map_api, "_cells", lambda conn: cells)
    monkeypatch.setattr(map_api, "_pem_cfg", lambda: {})
    ns = type("NS", (), {})()
    ns.calls, ns.inten = calls, inten

    def use(quake=(35.5, 140.0, 65.4, 6.2, "千葉県北西部"),
            radius=(("1", 400.0), ("2", 300.0), ("3", 300.0), ("4", 300.0), ("5", 300.0), ("6", 300.0))):
        c = _Conn(quake, list(radius))
        monkeypatch.setattr(map_api, "_connect", lambda: c)
        return c
    ns.use = use
    return ns


def test_impact_keeps_cells_at_three_or_more_and_bands_split_at_3_5_and_4_5(impact):
    impact.use()
    out = map_api.api_map_quake_impact(355)
    by = {(p[0], p[3]) for p in out["pts"]}
    assert out["cells"] == 6 and out["enb"] == 5 and out["threshold"] == 3.0          # 2.99 (eNB 6) 는 빠진다
    bands = {p[0]: p[3] for p in out["pts"]}
    assert bands == {139.8: "5-", 139.9: "3", 140.0: "4", 140.1: "4", 140.2: "5-"}     # 3.0→3 · 3.5→4 · 4.49→4 · 4.5→5약
    assert len(by) == len(out["pts"]) == 5
    assert out["max"] == 4.6 and out["arv"] is False                                   # 지반 표 경로가 없으면 모든 eNB 1.0


def test_impact_takes_the_strongest_cell_of_an_enb_and_the_largest_radius(impact):
    impact.use(radius=(("1", 410.4), ("2", 300.0)))
    out = map_api.api_map_quake_impact(355)
    p1 = next(p for p in out["pts"] if p[0] == 139.8)
    assert p1[2] == 410 and p1[3] == "5-"                                              # eNB 1 — 셀 3.1/4.6 중 큰 값, 반경 반올림
    assert [p[0] for p in out["pts"]] == [139.8, 139.9]                                # 반경을 모르는 eNB 는 그리지 않는다(지어내지 않는다)
    assert out["enb"] == 5                                                             # 세는 수는 반경과 무관


def test_impact_query_asks_radius_by_enb_once_with_bound_ids_and_closes(impact):
    c = impact.use()
    map_api.api_map_quake_impact(355)
    qs = [(s, p) for s, p in c.log if "CM_CELL_GEO_MAP" in s]
    assert len(qs) == 1 and "AREA_LEVEL = 'MESH1K'" in qs[0][0] and "GROUP BY ENB_ID" in qs[0][0]
    assert qs[0][1] == ["1", "2", "3", "4", "5"] and qs[0][0].count("%s") == 5           # eNB 는 바인딩이고 3 미만은 묻지도 않는다
    assert c.closed
    for s, _ in c.log:
        assert s.lstrip().upper().startswith("SELECT") and not re.search(r"\b(INSERT|UPDATE|DELETE|ALTER|DROP)\b", s, re.I)


def test_impact_finds_d_by_hypo_name_and_falls_back_to_default(impact):
    fc = S.FaultCfg.from_cfg({})
    name, d = fc.quake_regions[0][0], fc.quake_regions[0][4]
    impact.use(quake=(35.5, 140.0, 65.4, 6.2, name))
    out = map_api.api_map_quake_impact(1)
    assert out["d"] == d and out["d_source"] == "region" and impact.calls[-1]["fault_type_d"] == d
    map_api._IMPACT_CACHE.clear()
    impact.use(quake=(35.5, 140.0, 65.4, 6.2, "없는 진원역"))
    out = map_api.api_map_quake_impact(2)
    assert out["d"] is None and out["d_source"] == "default" and impact.calls[-1]["fault_type_d"] == 0.0
    map_api._IMPACT_CACHE.clear()
    impact.use(quake=(35.5, 140.0, 65.4, 6.2, None))
    assert map_api.api_map_quake_impact(3)["d_source"] == "default"


def test_impact_passes_the_quake_in_floats_to_the_generators_intensity_function(impact):
    impact.use()
    map_api.api_map_quake_impact(355)
    q = impact.calls[-1]
    assert (q["lat"], q["lon"], q["depth_km"], q["magnitude"]) == (35.5, 140.0, 65.4, 6.2)


@pytest.mark.parametrize("row", [(35.5, 140.0, None, 6.2, "x"), (35.5, 140.0, 65.4, None, "x")])
def test_impact_unknown_magnitude_or_depth_is_an_error_with_no_calculation(impact, row):
    """규모·깊이를 모르면 진도를 계산하지 않는다 — 0 으로 두지 않는다(불변규칙 17)."""
    c = impact.use(quake=row)
    out = map_api.api_map_quake_impact(9)
    assert "error" in out and out["enbs"] == [] and "pts" not in out
    assert impact.calls == [] and c.closed and 9 not in map_api._IMPACT_CACHE


def test_impact_unknown_fault_is_an_error(impact):
    c = impact.use(quake=None)
    out = map_api.api_map_quake_impact(404)
    assert "error" in out and out["enbs"] == [] and c.closed


def test_impact_is_cached_per_fault_and_the_second_call_does_not_connect(impact, monkeypatch):
    impact.use()
    first = map_api.api_map_quake_impact(355)

    def boom():
        raise AssertionError("두 번째 호출은 DB 에 가지 않는다")
    monkeypatch.setattr(map_api, "_connect", boom)
    assert map_api.api_map_quake_impact(355) is first
    impact.use()
    assert map_api.api_map_quake_impact(356) is not first                              # 다른 장애는 따로


def test_impact_with_no_cell_at_or_above_three_gives_empty_points_not_error(impact, monkeypatch):
    monkeypatch.setattr(S, "quake_cell_intensity", lambda cs, q, arv: {"1-0": 2.0, "2-0": 1.5})
    impact.use()
    out = map_api.api_map_quake_impact(5)
    assert out["pts"] == [] and out["cells"] == 0 and out["enb"] == 0 and "error" not in out and out["max"] == 2.0


def test_impact_missing_table_is_an_error_not_an_exception(impact, monkeypatch):
    import pymysql

    class Bad(_Conn):
        def cursor(self):
            raise pymysql.err.ProgrammingError(1146, "no table")
    c = Bad(None, [])
    monkeypatch.setattr(map_api, "_connect", lambda: c)
    out = map_api.api_map_quake_impact(1)
    assert "error" in out and out["enbs"] == [] and c.closed


def test_impact_route_is_registered_and_the_response_has_no_per_cell_keys(impact):
    paths = {getattr(r, "path", "") for r in map_api.router.routes}
    assert "/api/map/quake/{fault_id}/impact" in paths
    impact.use()
    out = map_api.api_map_quake_impact(355)
    assert set(out) == {"fault_id", "threshold", "cells", "enb", "pts", "max", "arv", "d", "d_source"}
    assert all(len(p) == 4 for p in out["pts"])                                         # [lon, lat, 반경 m, 띠] 뿐


def test_impact_reads_the_ground_table_from_the_fault_config_and_reports_it(impact, monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(map_api, "_pem_cfg", lambda: {"fault": {"quake_arv_path": str(tmp_path / "a.csv")}})
    monkeypatch.setattr(S, "quake_arv", lambda path=None, *a, **k: seen.append(path) or {"1": 2.0})
    impact.use()
    out = map_api.api_map_quake_impact(7)
    assert seen == [str(tmp_path / "a.csv")] and out["arv"] is True
