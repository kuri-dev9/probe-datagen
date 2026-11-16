"""27번 대상 문자열 — `WIDE:QUAKE:B,6.2` (넓은 지역 80셀 + 진원 지정 · 2026-11-15 사용자 "일단 80개로 해보자").

`faults.build` 가 `LOCAL:`·`WIDE:` 앞머리를 **`QUAKE:` 지정보다 먼저** 뗀다. 전에는 `QUAKE:` 가 먼저라 둘을 함께 줄 수 없었다.
기존 꼴(`QUAKE:…` · `LOCAL:QUAKE:…` · `WIDE` · `WIDE:BOX:…` · 빈 값)은 전과 같아야 한다. DB 를 쓰지 않는다.
"""
from __future__ import annotations

import random
import re
from datetime import datetime
from pathlib import Path

import pytest

from datagen import faults
from datagen import scenarios as S
from datagen.cells import Cell

SRC = (Path(faults.__file__)).read_text(encoding="utf-8")


def _cell(enb: str, n: int, lat: float, lon: float) -> Cell:
    return Cell(
        cell_key=f"{enb}-{n}", enb_id=enb, cell_id=n, enb_seq=0, c_uid="",
        tac=100, plmn="44010", vlan_id="", latitude=lat, longitude=lon,
        adm_sido_cd="11", adm_sido_nm="", adm_sgg_cd="11200", adm_sgg_nm="",
        adm_emd_cd="11200011", adm_emd_nm="", geo_status="MAPPED",
        rat="EUTRAN", mme_id="0001", sgw_id="0037", pgw_id="0037",
        ims_pgw_id="0037", traffic_weight=1.0, display_name="")


def _grid(n_enb: int = 120, per: int = 3, lat0: float = 35.6, lon0: float = 140.0) -> list[Cell]:
    out = []
    for i in range(n_enb):
        la, lo = lat0 + (i // 12 - 5) * 0.01, lon0 + (i % 12 - 6) * 0.01
        out += [_cell(f"{800000 + i}", n, la + n * 0.0005, lo) for n in range(per)]
    return out


def _build(target, fid=27, cells=None):
    f = {"fault_id": fid, "kind": "natural_disaster", "target": target,
         "start_ts": datetime(2026, 11, 14, 10, 0), "duration_min": 25, "intensity": 1.0}
    return faults.build(f, cells or _grid(), datetime(2026, 11, 14, 9, 0), random.Random(1), seed=3)


def _surge(sc):
    return sc.target.cell_keys - set(sc.as_truth()["down_cells"])


# ── 1 · WIDE:QUAKE:B,6.0 ───────────────────────────────────────────────────────────────────

def test_wide_plus_quake_gives_wide_mode_the_given_epicenter_and_eighty_surge_cells():
    sc = _build("WIDE:QUAKE:B,6.0")
    assert sc is not None, "WIDE:QUAKE: 꼴이 받아들여져야 한다"
    t = sc.as_truth()
    assert sc.target.outage_mode == "wide" and t["outage_mode"] == "wide"
    assert t["quake"]["region"] == "千葉県北西部" == t["quake"]["hypo_name"] and t["quake"]["magnitude"] == 6.0
    n = len(_surge(sc))
    assert S.DISASTER_AREA_CELLS["local"] < n <= S.DISASTER_AREA_CELLS["wide"]
    assert n == S.DISASTER_AREA_CELLS["wide"], "이 격자는 진도 3 이상 셀이 충분해 80셀이 서야 한다"


def test_wide_plus_quake_takes_lat_lon_form_and_is_case_insensitive():
    a = _build("wide:quake:35.6,140.0,5.9,40")
    assert a is not None and a.target.outage_mode == "wide"
    q = a.as_truth()["quake"]
    assert (q["lat"], q["lon"], q["magnitude"], q["depth_km"]) == (35.6, 140.0, 5.9, 40.0)


def test_surrounding_spaces_are_tolerated():
    sc = _build("  WIDE:QUAKE:B,6.0 ")                      # 앞뒤 공백은 strip — (앞머리와 콜론 사이 공백은 전부터 받지 않는다)
    assert sc is not None and sc.target.outage_mode == "wide"


# ── 2 · 기존 꼴은 전과 같다 ───────────────────────────────────────────────────────────────────

def test_quake_alone_and_local_quake_are_the_same_local_quake():
    a, b = _build("QUAKE:B,6.0"), _build("LOCAL:QUAKE:B,6.0")
    assert a is not None and b is not None
    assert a.target.outage_mode == b.target.outage_mode == "local"
    assert a.as_truth()["quake"] == b.as_truth()["quake"]
    assert a.target.cell_keys == b.target.cell_keys and a.as_truth()["down_cells"] == b.as_truth()["down_cells"]
    assert len(_surge(a)) == S.DISASTER_AREA_CELLS["local"]


def test_wide_alone_has_no_given_epicenter_and_is_wide():
    sc = _build("WIDE")
    if sc is None:
        pytest.skip("이 격자·시드에서는 폭주 80셀이 서지 않는다")
    assert sc.target.outage_mode == "wide" and len(_surge(sc)) <= S.DISASTER_AREA_CELLS["wide"]


def test_local_alone_and_empty_target_are_local():
    for tgt in ("LOCAL", "", None):
        sc = _build(tgt)
        if sc is None:
            continue
        assert sc.target.outage_mode == "local", tgt
        assert len(_surge(sc)) <= S.DISASTER_AREA_CELLS["local"]


def test_wide_box_still_takes_the_box_as_the_target():
    sc = _build("WIDE:BOX:35.55,139.95,35.62,140.05")
    if sc is None:
        pytest.skip("이 격자·시드에서는 폭주 셀이 서지 않는다")
    assert sc.target.outage_mode == "wide"
    assert sc.as_truth()["quake"]["region"]                                  # 셀 대상 → 가까운 진원역으로 정한다(11-14)


def test_the_given_epicenter_is_independent_of_the_mode():
    """같은 진원 지정이면 지진 자체(진원·규모·깊이)는 모드와 무관하다 — 모드는 폭주 셀 수만 바꾼다."""
    loc, wide = _build("LOCAL:QUAKE:B,6.0").as_truth()["quake"], _build("WIDE:QUAKE:B,6.0").as_truth()["quake"]
    keys = ("region", "lat", "lon", "magnitude", "depth_km", "fault_type_d", "origin_time")
    assert {k: loc[k] for k in keys} == {k: wide[k] for k in keys}


# ── 3 · 정지 eNB 선택은 모드에 따라 바뀌지 않는다 ───────────────────────────────────────────────

def test_down_selection_does_not_depend_on_the_mode_and_obeys_the_cap_only_when_one_is_given(monkeypatch):
    cells = _grid()
    arv = {c.enb_id: 2.5 for c in cells}                                     # 정지 후보가 생기게
    monkeypatch.setattr(S, "quake_arv", lambda *a, **k: arv)
    loc, wide = _build("LOCAL:QUAKE:B,6.0", cells=cells), _build("WIDE:QUAKE:B,6.0", cells=cells)
    dl, dw = loc.as_truth()["down_cells"], wide.as_truth()["down_cells"]
    assert dl and dl == dw, "정지 eNB 는 같은 진원이면 같아야 한다(A안 — 모드와 무관)"
    # 11-16 가안 — 상한은 기본이 없음(None)이고, 값을 주면 그 수에서 멈춘다(그때만 상한을 따진다)
    assert S.FAULT.quake_down_max_cells is None
    cap = S.FAULT.quake_down_max_cells
    assert cap is None or len(dw) <= cap
    monkeypatch.setattr(S.FAULT, "quake_down_max_cells", 6)
    assert len(_build("WIDE:QUAKE:B,6.0", cells=cells).as_truth()["down_cells"]) <= 6
    monkeypatch.setattr(S.FAULT, "quake_down_max_cells", None)
    assert loc.as_truth()["down_enbs"] == wide.as_truth()["down_enbs"]
    assert set(dw) <= wide.target.cell_keys                                  # 정답 지역 = 폭주 셀 + 정지 셀
    assert len(wide.target.cell_keys) > len(loc.target.cell_keys)           # 넓은 쪽이 더 많은 폭주 셀


# ── 4 · 잘못된 꼴 ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("bad", ["WIDE:QUAKE:Z", "LOCAL:QUAKE:Z", "QUAKE:Z", "WIDE:QUAKE:", "WIDE:QUAKE:35.1",
                                 "WIDE:QUAKE:B,abc"])
def test_malformed_quake_target_builds_nothing_with_or_without_a_head(bad):
    assert _build(bad) is None


# ── 5 · 소스 계약 ─────────────────────────────────────────────────────────────────────────────

def test_the_head_is_stripped_before_the_quake_target_is_read():
    body = SRC[SRC.index("    mode = None\n    raw_target = fault.get(\"target\")"):]
    body = body[:body.index("rng_pct = None")]
    assert body.index("_disaster_mode(raw_target)") < body.index('.startswith("QUAKE:")')
    assert len(re.findall(r"_disaster_mode\(raw_target\)", body)) == 1       # 한 번만 뗀다(두 번 떼면 앞머리가 이중으로 먹힌다)


def test_other_kinds_do_not_get_the_head_stripped():
    """27번(natural_disaster)만 `LOCAL:`·`WIDE:` 를 뗀다 — 다른 시나리오의 대상 문자열은 건드리지 않는다."""
    body = SRC[SRC.index("    mode = None\n    raw_target = fault.get(\"target\")"):]
    code = "\n".join(l for l in body.splitlines() if not l.strip().startswith("#"))
    guard = code[:code.index("mode, raw_target = _disaster_mode(raw_target)")].rstrip().splitlines()[-1].strip()
    assert guard == 'if fault["kind"] == "natural_disaster":'
