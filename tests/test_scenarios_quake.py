"""27번 — 지진(`earthquake`)은 **eNB 단위로** 멈춘다 (2026-11-03 M-5).

지진은 사이트 단위 정지다(사용자 결정 — 셀 일부 정지는 다른 장애).
graphio D4 는 eNB 의 호 합으로 무호를 판정하므로, 같은 eNB 의 셀 하나라도
호를 내면 서지 않는다. 이 파일이 지키는 것은 —

1. **대상이 eNB 경계를 자르지 않는다** — `area_cells` 가 고른 셀이 걸친
   eNB 의 셀 전부가 대상이다(`whole_enbs` · `faults._auto_target`).
2. **정지 eNB 의 셀은 모두 정지**하고, 강도 1 에서 정지 eNB 가 1개 이상이다.
3. **강도가 내려가면 정지 집합이 줄기만 한다** — 같은 eNB 가 꺼졌다
   켜졌다 하지 않는다.

**DB 를 쓰지 않는다**(`test_session_model.py` 머리말). eNB 번호는 합성이다.
"""

from __future__ import annotations

import random
from types import SimpleNamespace

from datagen import faults
from datagen import scenarios as S
from datagen.cells import Cell


def _cell(enb: str, n: int, lat: float, lon: float) -> Cell:
    return Cell(
        cell_key=f"{enb}-{n}", enb_id=enb, cell_id=n, enb_seq=0, c_uid="",
        tac=100, plmn="44010", vlan_id="", latitude=lat, longitude=lon,
        adm_sido_cd="11", adm_sido_nm="", adm_sgg_cd="11200", adm_sgg_nm="",
        adm_emd_cd="11200011", adm_emd_nm="", geo_status="MAPPED",
        rat="EUTRAN", mme_id="0001", sgw_id="0037", pgw_id="0037",
        ims_pgw_id="0037", traffic_weight=1.0, display_name="")


def _master() -> list[Cell]:
    """eNB 30개 × 4셀. **셀마다 좌표를 조금씩 벌린다** — 거리순 25셀이 eNB
    경계를 자르는 실제 모양을 만들기 위해서다."""
    return [_cell(f"{700000 + e}", n,
                  35.69 + (e % 6) * 0.01 + n * 0.004,
                  139.70 + (e // 6) * 0.01 + n * 0.004)
            for e in range(30) for n in range(4)]


def _sc(cells: list[Cell]):
    return SimpleNamespace(id="J-SC-27", target=S.Target(
        cell_keys={c.cell_key for c in cells}))


def test_area_target_covers_whole_enbs():
    cells = _master()
    picked = S.area_cells(cells, random.Random(3), want=25)
    enbs = {c.enb_id for c in picked}
    # 전제 — 거리순 25셀이 실제로 eNB 를 자른다. 자르지 않으면 이 테스트는
    # 아무것도 지키지 못한다
    assert len(picked) < 4 * len(enbs)
    whole = S.whole_enbs(cells, picked)
    assert {c.enb_id for c in whole} == enbs
    assert len(whole) == 4 * len(enbs), "걸친 eNB 의 셀 일부가 빠졌다"

    t = faults._auto_target("earthquake", cells, random.Random(3))
    by_enb: dict[str, int] = {}
    for k in t.cell_keys:
        e = k.rsplit("-", 1)[0]
        by_enb[e] = by_enb.get(e, 0) + 1
    assert set(by_enb.values()) == {4}, f"자동 대상이 eNB 를 잘랐다: {by_enb}"


def test_down_is_whole_enb_and_at_least_one():
    cells = S.whole_enbs(_master(), S.area_cells(_master(), random.Random(3), want=25))
    sc = _sc(cells)
    down = {c.cell_key for c in cells if S.quake_down(sc, c, 1.0)}
    assert down, "강도 1 에서 정지 eNB 가 0개다"
    down_enbs = {k.rsplit("-", 1)[0] for k in down}
    for c in cells:
        if c.enb_id in down_enbs:
            assert c.cell_key in down, f"정지 eNB {c.enb_id} 의 셀 {c.cell_key} 가 살아 있다"
    # 비율 0.40 은 그대로 — 대상 eNB 수의 ceil(0.4·n)
    n = len({c.enb_id for c in cells})
    import math
    assert len(down_enbs) == math.ceil(S.QUAKE_DOWN * n)
    # 대상 밖 셀은 멈추지 않는다
    other = _cell("799999", 0, 35.0, 139.0)
    assert not S.quake_down(sc, other, 1.0)


def test_down_set_only_shrinks_as_intensity_falls():
    cells = S.whole_enbs(_master(), S.area_cells(_master(), random.Random(3), want=25))
    sc = _sc(cells)
    prev = None
    for i in (1.0, 0.8, 0.6, 0.4, 0.2, 0.05, 0.0):
        now = {c.cell_key for c in cells if S.quake_down(sc, c, i)}
        if prev is not None:
            assert now <= prev, f"I={i} 에서 새로 꺼진 셀이 있다: {now - prev}"
        prev = now
    assert prev == set(), "강도 0 인데 정지 셀이 남았다"
