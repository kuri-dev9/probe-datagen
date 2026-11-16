"""27번 지진 — 정지 eNB 를 상한 대신 흔들림 비례 확률로 (2026-11-16 사용자 가안 · 에스터 시험).

`p = min(1, quake_down_pct/100 × 10^((I−4.5)/1.72))` 로 후보 eNB 를 eNB 째 끈다. `quake_down_max_cells` 가
None(yaml `~`)이면 상한 없음, 값을 주면 그 셀 수에서 멈춘다. 상한이 없으면 `q["max_cells"]` 에 후보 eNB 셀 수를 적는다
(CM_QUAKE_EVENT.MAX_CELLS NOT NULL). 같은 장애 ID(시드)면 같은 eNB 가 꺼진다. DB 를 쓰지 않는다.
"""
from __future__ import annotations

import random
import re
from pathlib import Path

import pytest
import yaml

from datagen import scenarios as S
from datagen.cells import Cell

ROOT = Path(__file__).resolve().parents[1]


def _cell(enb, n, lat, lon):
    return Cell(cell_key=f"{enb}-{n}", enb_id=enb, cell_id=n, enb_seq=0, c_uid="", tac=100, plmn="44010", vlan_id="",
                latitude=lat, longitude=lon, adm_sido_cd="11", adm_sido_nm="", adm_sgg_cd="11200", adm_sgg_nm="",
                adm_emd_cd="11200011", adm_emd_nm="", geo_status="MAPPED", rat="EUTRAN", mme_id="0001",
                sgw_id="0037", pgw_id="0037", ims_pgw_id="0037", traffic_weight=1.0, display_name="")


def _cells(n_enb=400, per=3):
    """진앙 둘레에 eNB 를 깔되 모두 같은 진도가 되게 하려고 한 점에 둔다(거리 0)."""
    return [_cell(f"{800000 + i}", n, 35.0, 139.0) for i in range(n_enb) for n in range(per)]


def _q(**kw):
    q = {"lat": 35.0, "lon": 139.0, "depth_km": 20.0, "magnitude": 5.4, "fault_type_d": 0.12}
    q.update(kw)
    return q


@pytest.fixture
def cfg(monkeypatch):
    monkeypatch.setattr(S.FAULT, "quake_down_pct", 30.0)
    monkeypatch.setattr(S.FAULT, "quake_down_max_cells", None)
    return S.FAULT


def _at(monkeypatch, inten):
    """모든 셀이 정확히 이 진도가 되게 고정한다 — 확률식만 따로 본다."""
    monkeypatch.setattr(S, "quake_cell_intensity", lambda cells, q, arv=None: {c.cell_key: inten for c in cells})


def _share(monkeypatch, inten, seeds=40):
    _at(monkeypatch, inten)
    cells, tot, dead = _cells(), 0, 0
    for k in range(seeds):
        _a, d, _dc = S.quake_plan(cells, _q(), random.Random(k), 25, {})
        tot += 400
        dead += len(d)
    return dead / tot


# ── 1 · 확률식 ────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("inten, expect", [(4.5, 0.30), (4.7, 0.30 * 10 ** (0.2 / 1.72)), (5.0, 0.30 * 10 ** (0.5 / 1.72))])
def test_down_share_follows_the_formula(cfg, monkeypatch, inten, expect):
    got = _share(monkeypatch, inten)
    assert got == pytest.approx(expect, abs=0.03), f"진도 {inten} → {got:.3f} (식 {expect:.3f})"


def test_slope_constant_and_cap_of_the_probability(cfg, monkeypatch):
    assert S.QUAKE_I_SLOPE == 1.72
    assert _share(monkeypatch, 5.4) == 1.0, "0.30×10^(0.9/1.72)=1.0 → 5.4 이상은 전부 꺼진다"
    assert _share(monkeypatch, 7.0) == 1.0, "1 을 넘지 않는다(min)"


def test_below_the_down_intensity_nothing_goes_down(cfg, monkeypatch):
    assert _share(monkeypatch, 4.4, seeds=5) == 0.0


def test_the_base_pct_scales_the_share(cfg, monkeypatch):
    monkeypatch.setattr(S.FAULT, "quake_down_pct", 60.0)
    assert _share(monkeypatch, 4.5) == pytest.approx(0.60, abs=0.03)


def test_a_bigger_quake_downs_more_enbs_than_a_smaller_one(cfg):
    """상한을 없앤 이유 — 규모가 커지면 꺼지는 수도 는다(전엔 둘 다 40)."""
    cells = _cells(120)
    arv = {c.enb_id: 1.0 for c in cells}
    near = lambda m: len(S.quake_plan(cells, _q(magnitude=m, depth_km=10.0), random.Random(1), 25, arv)[2])
    assert near(6.8) > near(6.0) >= near(5.2)


# ── 2 · 결정론 ────────────────────────────────────────────────────────────────────────────────

def test_same_seed_same_enbs_and_cells_order_does_not_matter(cfg, monkeypatch):
    _at(monkeypatch, 4.7)
    cells = _cells(200)
    a = S.quake_plan(cells, _q(), random.Random(11), 25, {})
    assert a == S.quake_plan(cells, _q(), random.Random(11), 25, {})
    assert a[1:] == S.quake_plan(list(reversed(cells)), _q(), random.Random(11), 25, {})[1:]
    assert a[1] != S.quake_plan(cells, _q(), random.Random(12), 25, {})[1], "시드가 다르면 다른 eNB"


def test_a_cap_does_not_change_the_random_stream(cfg, monkeypatch):
    """상한이 있어도 후보마다 난수를 하나씩 뽑는다 — 상한 유무로 같은 시드의 뽑힘이 달라지면 안 된다."""
    _at(monkeypatch, 4.7)
    cells = _cells(200)
    free = S.quake_plan(cells, _q(), random.Random(5), 25, {})[1]
    monkeypatch.setattr(S.FAULT, "quake_down_max_cells", 30)
    capped = S.quake_plan(cells, _q(), random.Random(5), 25, {})[1]
    assert set(capped) <= set(free)


# ── 3 · 상한 ──────────────────────────────────────────────────────────────────────────────────

def test_none_means_no_cap_and_a_value_is_the_cap(cfg, monkeypatch):
    _at(monkeypatch, 6.0)                                                    # 전부 꺼질 진도
    cells = _cells(100)
    assert len(S.quake_plan(cells, _q(), random.Random(1), 25, {})[2]) == 300   # None → 상한 없음(40 을 넘는다)
    monkeypatch.setattr(S.FAULT, "quake_down_max_cells", 40)
    got = S.quake_plan(cells, _q(), random.Random(1), 25, {})[2]
    assert 0 < len(got) <= 40, "값을 주면 그 셀 수에서 멈춘다"
    monkeypatch.setattr(S.FAULT, "quake_down_max_cells", 0)
    assert S.quake_plan(cells, _q(), random.Random(1), 25, {})[2] == [], "0 은 None 이 아니다 — 아무것도 안 끈다"


def test_the_cap_never_splits_an_enb(cfg, monkeypatch):
    _at(monkeypatch, 6.0)
    monkeypatch.setattr(S.FAULT, "quake_down_max_cells", 10)                 # eNB 당 3셀 → 3곳 9셀(넘는 eNB 는 건너뜀)
    _a, enbs, cs = S.quake_plan(_cells(50), _q(), random.Random(2), 25, {})
    assert len(cs) == 3 * len(enbs) and len(cs) <= 10


# ── 4 · MAX_CELLS ─────────────────────────────────────────────────────────────────────────────

def test_max_cells_records_the_candidate_cell_count_when_there_is_no_cap(cfg, monkeypatch):
    _at(monkeypatch, 4.7)
    q = _q()
    S.quake_plan(_cells(50), q, random.Random(1), 25, {})
    assert q["max_cells"] == 150, "후보 eNB 50곳 × 3셀 — 꺼질 수 있는 최대"
    q0 = _q()
    _at(monkeypatch, 4.0)                                                    # 후보 없음
    S.quake_plan(_cells(50), q0, random.Random(1), 25, {})
    assert q0["max_cells"] == 0 and isinstance(q0["max_cells"], int), "NOT NULL 칸에 None 이 가지 않는다"


def test_max_cells_is_untouched_when_a_cap_is_given(cfg, monkeypatch):
    monkeypatch.setattr(S.FAULT, "quake_down_max_cells", 40)
    _at(monkeypatch, 4.7)
    q = _q(max_cells=40)
    S.quake_plan(_cells(50), q, random.Random(1), 25, {})
    assert q["max_cells"] == 40


def test_quake_new_puts_the_cap_or_none_and_the_plan_fills_none(cfg):
    q = S.quake_new(random.Random(1), None, None, None, 6.0, 40.0)
    assert q is not None and q["max_cells"] is None, "plan 이 채우기 전의 None — faults 가 plan 뒤에 행을 만든다"
    src = (ROOT / "src/datagen/faults.py").read_text(encoding="utf-8")
    assert src.index("S.quake_plan(cells, q") < src.index('"quake_id": f"Q-F{fid}"') < src.index("**q, \"down_enb_cnt\"")


# ── 5 · 설정 읽기 ─────────────────────────────────────────────────────────────────────────────

def test_yaml_default_is_null_and_loads_as_none():
    doc = yaml.safe_load((ROOT / "config/datagen.yaml").read_text(encoding="utf-8"))
    raw = doc["pemdb1"]["fault"]
    assert "quake_down_max_cells" in raw and raw["quake_down_max_cells"] is None
    assert raw["quake_down_pct"] == 30


@pytest.mark.parametrize("v, want", [(None, None), ("", None), (40, 40), ("40", 40), (0, 0)])
def test_fault_cfg_reads_the_cap(v, want):
    assert S.FaultCfg().quake_down_max_cells is None, "기본값은 상한 없음"
    assert S.FaultCfg.from_cfg({"quake_down_max_cells": v}).quake_down_max_cells == want


def test_other_quake_keys_still_convert_and_unknown_keys_still_stop():
    f = S.FaultCfg.from_cfg({"quake_down_pct": "45", "quake_down_intensity": 4.6})
    assert f.quake_down_pct == 45.0 and f.quake_down_intensity == 4.6
    with pytest.raises(Exception):
        S.FaultCfg.from_cfg({"quake_down_max_cell": 40})                     # 오타는 멈춘다
