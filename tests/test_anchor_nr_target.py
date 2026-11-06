"""16번(앵커 경합) 대상은 NR 가능 eNB 안에서만 고른다 (2026-11-05 사용자 결정).

NR 불가 eNB 에는 SA(NR 사용) 세션이 없어 걸어도 아무 일이 없다. 자동·`ENB:`·`BOX:`·`TAC:`·`SGG:`·`LIKE:`·
배치(`build_scenarios`)가 한 길로 지난다. 판정은 `pemdb1.PemdbGen._nr_on` 을 그대로 넘긴다 — 여기서는
결정적인 가짜 판정(`nr_on`)으로 길만 본다. 나중에 NR gNB 를 실제 값으로 고정하면 13·15·16번은 그 eNB 에만
주입해야 할 수 있다(`5G-CLASSIFICATION.md`).

DB 없이 합성 마스터로 돈다.
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta

import pytest

from datagen import faults
from datagen import scenarios as S
from datagen.cells import Cell

T0 = datetime(2026, 11, 5, 9, 0)
KIND = "anchor_saturation"


def _cell(enb: str, n: int, lat: float, lon: float, tac: int = 100, sgg: str = "11200") -> Cell:
    return Cell(
        cell_key=f"{enb}-{n}", enb_id=enb, cell_id=n, enb_seq=0, c_uid="",
        tac=tac, plmn="44010", vlan_id="", latitude=lat, longitude=lon,
        adm_sido_cd="11", adm_sido_nm="", adm_sgg_cd=sgg, adm_sgg_nm="",
        adm_emd_cd=sgg + "011", adm_emd_nm="", geo_status="MAPPED",
        rat="EUTRAN", mme_id="0001", sgw_id="0037", pgw_id="0037",
        ims_pgw_id="0037", traffic_weight=1.0, display_name="")


def _master() -> list[Cell]:
    """eNB 60개 × 3셀 · 6행 × 10열 격자. TAC 은 행마다, 시군구는 두 개(앞 30개 · 뒤 30개)."""
    out = []
    for e in range(60):
        lat = 35.60 + (e // 10) * 0.004
        lon = 139.60 + (e % 10) * 0.004
        out += [_cell(str(600000 + e), n, lat + n * 0.0005, lon + n * 0.0005,
                      tac=100 + e // 10, sgg="11200" if e < 30 else "11210") for n in range(3)]
    return out


def nr_on(enb: str) -> bool:
    """결정적 가짜 — 번호가 3 의 배수인 eNB 만 NR 가능(40%보다 후하게 1/3)."""
    return int(enb) % 3 == 0


def _fault(target=None, fid=66, kind=KIND):
    return {"fault_id": fid, "kind": kind, "target": target, "start_ts": T0 + timedelta(minutes=5),
            "duration_min": 20, "intensity": 1.0}


def _build(target=None, fid=66, seed=7, nr=nr_on, cells=None):
    f = _fault(target, fid)
    return faults.build(f, cells or _master(), T0, faults.target_rng(seed, fid), seed=seed, nr_on=nr)


def _enbs(sc) -> set[str]:
    return {k.rsplit("-", 1)[0] for k in sc.target.cell_keys}


def _all_nr(sc) -> bool:
    return all(nr_on(e) for e in _enbs(sc))


# ---------------------------------------------------------------- 대상 형식별

@pytest.mark.parametrize("seed", range(12))
def test_auto_target_picks_only_nr_enbs(seed):
    sc = _build(seed=seed)
    assert sc is not None and sc.target.cell_keys
    assert _all_nr(sc), sorted(e for e in _enbs(sc) if not nr_on(e))


def test_enb_target_on_an_nr_enb_is_accepted_and_clusters_only_nr_enbs():
    sc = _build("ENB:600003")
    assert sc is not None and sc.target.enb_id == "600003"
    assert _all_nr(sc)


def test_enb_target_on_a_non_nr_enb_is_cancelled(capsys):
    assert _build("ENB:600001") is None
    out = capsys.readouterr().out
    assert "NR 가능 eNB 셀이 없어" in out, "조용히 취소하지 않는다 — 로그에 이유가 남는다"


def test_box_target_keeps_only_nr_cells():
    # 위도 35.600~35.604 = 첫 행(eNB 0~9) 전부를 감싼다
    sc = _build("BOX:35.599,139.599,35.602,139.650")
    assert sc is not None and _all_nr(sc)
    assert _enbs(sc) == {str(600000 + e) for e in range(10) if nr_on(str(600000 + e))}


def test_tac_target_keeps_only_nr_cells():
    sc = _build("TAC:101")
    assert sc is not None and _all_nr(sc)
    assert _enbs(sc) == {str(600000 + e) for e in range(10, 20) if nr_on(str(600000 + e))}


def test_sgg_target_keeps_only_nr_cells_and_the_sgg_code():
    sc = _build("SGG:11210")
    assert sc is not None and _all_nr(sc) and sc.target.sgg_codes == {"11210"}
    assert _enbs(sc) == {str(600000 + e) for e in range(30, 60) if nr_on(str(600000 + e))}


def test_auto_target_with_no_nr_enb_at_all_is_cancelled(capsys):
    """NR 가능 eNB 가 하나도 없으면 `IndexError` 없이 취소하고 이유를 남긴다(맥스 수정 `faults.py:177-179`)."""
    assert _build(nr=lambda e: False) is None
    assert "NR 가능 eNB 셀이 없어" in capsys.readouterr().out


def test_explicit_target_with_no_nr_enb_at_all_is_cancelled():
    assert _build("SGG:11200", nr=lambda e: False) is None
    assert _build("ENB:600003", nr=lambda e: False) is None


def test_like_reinjection_selects_the_same_target_as_the_original():
    orig = _build(fid=66, seed=7)
    again = _build("LIKE:66", fid=67, seed=7)
    assert orig is not None and again is not None
    assert again.target.cell_keys == orig.target.cell_keys
    assert _all_nr(again)


# ---------------------------------------------------------------- nr_on 없이 · 다른 kind

@pytest.mark.parametrize("seed", range(6))
def test_without_nr_on_the_target_is_what_it_was_before(seed):
    """`nr_on=None`(구 호출부 · 테스트 하니스)은 전과 같다 — 후보를 좁히지 않는다."""
    a = _build(seed=seed, nr=None)
    b = faults.build(_fault(), _master(), T0, faults.target_rng(seed, 66), seed=seed)
    assert a.target.cell_keys == b.target.cell_keys


def test_without_nr_on_a_non_nr_enb_is_accepted():
    assert _build("ENB:600001", nr=None) is not None


@pytest.mark.parametrize("kind", ["ran_congestion", "enb_s1_outage", "nsa_nr_leg"])
def test_other_kinds_are_not_narrowed_by_nr_on(kind):
    """16번이 아닌 kind 는 `nr_on` 이 주어져도 후보를 좁히지 않는다."""
    f = _fault("ENB:600001", kind=kind)
    sc = faults.build(f, _master(), T0, faults.target_rng(7, 66), seed=7, nr_on=nr_on)
    assert sc is not None, kind
    assert not nr_on("600001") and "600001" in _enbs(sc)


# ---------------------------------------------------------------- 배치 경로

SC16 = "J-SC-16"


def _batch(nr, seed=3, sel=(SC16,)):
    return S.build_scenarios(_master(), 2000, random.Random(seed), selected=list(sel), nr_on=nr)


def test_batch_placement_anchors_the_16_target_on_nr_enbs_only():
    for seed in range(6):
        got = [sc for sc in _batch(nr_on, seed) if sc.kind == KIND]
        assert got, f"시드 {seed}: 배치가 16번을 세우지 못했다"
        for sc in got:
            assert all(nr_on(e) for e in _enbs(sc)), sorted(_enbs(sc))


def test_batch_placement_without_nr_on_is_unchanged():
    a, b = _batch(None), S.build_scenarios(_master(), 2000, random.Random(3), selected=[SC16])
    assert a and [sorted(s.target.cell_keys) for s in a] == [sorted(s.target.cell_keys) for s in b]


def test_batch_narrowing_keeps_other_scenarios_targets_and_the_random_stream():
    """16번만 좁힌다 — 다른 시나리오 대상은 `nr_on` 유무와 무관하다(난수 순서 유지)."""
    sel = (SC16, "SC-03", "J-SC-14", "J-SC-15")
    a, b = _batch(None, 5, sel), _batch(nr_on, 5, sel)
    other = lambda xs: sorted((s.id, tuple(sorted(s.target.cell_keys))) for s in xs if s.kind != KIND)
    assert other(a) and other(a) == other(b)
