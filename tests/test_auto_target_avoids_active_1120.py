"""자동 대상은 지금 걸린 다른 장애의 셀을 피한다 — `faults._AVOID`·`_free_units` (2026-11-20 · 사용자 결정 · 동구 구현 · 에스터 시험).

F-461(27번 지진)이 자동 선택으로 활성이던 F-460(30번 荒川区) 셀 19개 위에 서서 분석이 둘을 못 갈랐다. 두 단계뿐이다 —
먼저 지금처럼 고르고, **겹칠 때만** 피한 후보로 다시 고른다. 못 피하면 처음 것을 쓰고 로그에 남긴다. DB 없이 합성 셀 마스터.
"""
from __future__ import annotations

import contextlib
import io
from datetime import datetime

import pytest

from datagen import faults as F
from datagen.cells import Cell

T0 = datetime(2026, 11, 20, 13, 0)
SEED = 20260903


def _cell(enb, n, lat, lon, sgg, sgw):
    return Cell(cell_key=f"{enb}-{n}", enb_id=str(enb), cell_id=n, enb_seq=0, c_uid="", tac=100, plmn="44010", vlan_id="",
                latitude=lat, longitude=lon, adm_sido_cd="13", adm_sido_nm="", adm_sgg_cd=str(sgg), adm_sgg_nm="",
                adm_emd_cd=f"{sgg}0010", adm_emd_nm="", geo_status="MAPPED", rat="EUTRAN", mme_id="0001",
                sgw_id=f"{sgw:04d}", pgw_id=f"{sgw:04d}", ims_pgw_id="0001", traffic_weight=1.0, display_name="")


def _master(one_sgw=False):
    """12구 × eNB 100 × 셀 3 = 3,600셀. SGW 는 구마다 1~4(`one_sgw` 면 전부 1)."""
    out, e = [], 100000
    for gi in range(4):
        for gj in range(3):
            sgg = 13101 + gi * 3 + gj
            for i in range(10):
                for j in range(10):
                    e += 1
                    for n in range(3):
                        out.append(_cell(e, n, 35.58 + gi * 0.05 + i * 0.005, 139.60 + gj * 0.10 + j * 0.010, sgg,
                                         1 if one_sgw else 1 + (gi * 3 + gj) % 4))
    return out


CELLS = _master()
BY = {c.cell_key: c for c in CELLS}


def _row(fid, kind, target=None):
    return {"fault_id": fid, "kind": kind, "target": target, "start_ts": T0, "duration_min": 10, "intensity": 1.0}


def _resolve(cells, rows, cache=None, seed=SEED):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        scs, bad = F.resolve_active(cells, T0, seed, rows, {} if cache is None else cache)
    return scs, bad, buf.getvalue()


def _solo(kind, fid, cells=CELLS):
    scs, bad, _ = _resolve(cells, [_row(fid, kind)])
    assert scs and not bad
    return scs[0].target.cell_keys


def _enb_of(keys, cells=BY):
    return sorted({cells[k].enb_id for k in keys if k in cells})[0]


KINDS = ["transport_loss", "ran_congestion", "enb_s1_outage", "son_param_change", "event_crowd", "gw_resource"]


@pytest.fixture(autouse=True)
def _avoid_is_empty_around_every_test():
    assert getattr(F, "_AVOID", set()) == set()
    yield
    assert getattr(F, "_AVOID", set()) == set(), "build 가 끝나면 피할 셀은 비어야 한다 — 다음 장애의 해석이 이것을 물려받는다"


# ── ① 겹치지 않는 경우는 수정 전과 같다 ────────────────────────────────────

@pytest.mark.parametrize("kind", KINDS + ["natural_disaster"])
def test_without_other_active_faults_the_auto_target_is_the_plain_pick(kind):
    a, b = _solo(kind, 502), _solo(kind, 502)
    assert a == b and a, "같은 입력은 같은 대상(결정론)"


@pytest.mark.parametrize("kind", KINDS)
def test_a_non_overlapping_neighbour_leaves_the_target_unchanged(kind):
    base = _solo(kind, 502)
    away = next(c.enb_id for c in CELLS if c.cell_key not in base and c.adm_sgg_cd not in {BY[k].adm_sgg_cd for k in base}
                and c.sgw_id not in {BY[k].sgw_id for k in base})
    first = _row(501, "ran_radio", f"ENB:{away}")
    scs, bad, log = _resolve(CELLS, [first, _row(502, kind)])
    assert scs[1].target.cell_keys == base, "겹치지 않던 장애의 대상이 바뀌었다"
    assert "겹쳐 다른 대상" not in log and "겹침을 허용" not in log


# ── ② 겹치면 피한다 (구·군집·eNB·SON·행사) ─────────────────────────────

@pytest.mark.parametrize("fid", [510, 520])
@pytest.mark.parametrize("kind", ["transport_loss", "ran_congestion", "enb_s1_outage", "son_param_change", "event_crowd"])
def test_a_forced_overlap_is_avoided_and_logged(kind, fid):
    base = _solo(kind, fid + 1)
    first = _row(fid, "ran_radio", f"ENB:{_enb_of(base)}")
    cache = {}
    a = _resolve(CELLS, [first], cache)[0][0].target.cell_keys
    assert a & base, "시험 전제 — 혼자 고르면 먼저 건 장애와 겹친다"
    scs, bad, log = _resolve(CELLS, [first, _row(fid + 1, kind)], cache)
    b = scs[1].target.cell_keys
    assert not (a & b) and b, f"{kind} 가 겹침을 못 피했다"
    assert f"F-{fid + 1} 자동 대상 — 활성 장애 셀" in log and "다른 대상으로 골랐다" in log


def test_the_unit_kind_moves_to_another_district_not_a_truncated_one():
    base = _solo("transport_loss", 531)
    first = _row(530, "ran_radio", f"ENB:{_enb_of(base)}")
    scs, _, _ = _resolve(CELLS, [first, _row(531, "transport_loss")])
    got = scs[1].target.cell_keys
    assert len({BY[k].adm_sgg_cd for k in got}) == 1 and len(got) == 300, "구 전 셀을 거는 장애가 잘렸다"
    assert {BY[k].adm_sgg_cd for k in got} != {BY[k].adm_sgg_cd for k in base}


# ── ③ 노드: 피할 노드가 있으면 그쪽 · 전부 겹치면 처음 대상과 「허용」 로그 ──────

def test_a_node_kind_picks_a_node_the_active_fault_does_not_touch():
    base = _solo("gw_resource", 541)
    first = _row(540, "ran_radio", f"ENB:{_enb_of(base)}")
    scs, _, log = _resolve(CELLS, [first, _row(541, "gw_resource")])
    a = _resolve(CELLS, [first])[0][0].target.cell_keys
    b = scs[1].target.cell_keys
    assert not (a & b) and {BY[k].sgw_id for k in b}.isdisjoint({BY[k].sgw_id for k in a})


def test_when_every_node_overlaps_the_first_target_is_kept_and_the_overlap_is_logged():
    one = _master(one_sgw=True)
    by = {c.cell_key: c for c in one}
    base = _solo("gw_resource", 551, one)
    first = _row(550, "ran_radio", f"ENB:{_enb_of(base, by)}")
    scs, _, log = _resolve(one, [first, _row(551, "gw_resource")])
    assert scs[1].target.cell_keys == base, "피할 후보가 없으면 처음 대상 그대로"
    assert "F-551 자동 대상이" in log and "피할 후보가 없어 겹침을 허용한다" in log
    assert "다른 대상으로 골랐다" not in log


# ── ④ 지진 자동 ──────────────────────────────────────────────────────────

def _quake_name(sc):
    return sc._quake["region"] if isinstance(getattr(sc, "_quake", None), dict) and "region" in sc._quake else None


def test_an_automatic_quake_moves_to_another_epicentre_when_its_area_touches_an_active_fault():
    base = _solo("natural_disaster", 561)
    first = _row(560, "ran_radio", f"ENB:{_enb_of(base)}")
    a = _resolve(CELLS, [first])[0][0].target.cell_keys
    plain = _resolve(CELLS, [_row(561, "natural_disaster")])[0][0]
    assert a & plain.target.cell_keys, "시험 전제 — 겹침"
    scs, _, log = _resolve(CELLS, [first, _row(561, "natural_disaster")])
    q = scs[1]
    assert not (q.target.cell_keys & a)
    assert _quake_name(q) and _quake_name(q) != _quake_name(plain), "진원역이 옮겨지지 않았다"
    again, _, _ = _resolve(CELLS, [first, _row(561, "natural_disaster")])
    assert again[1].target.cell_keys == q.target.cell_keys and _quake_name(again[1]) == _quake_name(q), "같은 지진이어야 한다"


def test_a_pinned_quake_is_never_moved():
    plain = _resolve(CELLS, [_row(571, "natural_disaster", "QUAKE:千葉県北西部")])[0][0]
    first = _row(570, "ran_radio", f"ENB:{_enb_of(plain.target.cell_keys)}")
    a = _resolve(CELLS, [first])[0][0].target.cell_keys
    assert a & plain.target.cell_keys
    scs, _, _ = _resolve(CELLS, [first, _row(571, "natural_disaster", "QUAKE:千葉県北西部")])
    assert scs[1].target.cell_keys == plain.target.cell_keys and _quake_name(scs[1]) == "千葉県北西部"


# ── ⑤ 지정 대상은 바뀌지 않는다 ─────────────────────────────────────────

@pytest.mark.parametrize("target_of", [lambda k: f"ENB:{_enb_of(k)}",
                                       lambda k: f"SGG:{BY[sorted(k)[0]].adm_sgg_cd}"])
def test_pinned_enb_and_sgg_targets_keep_their_cells_even_when_they_overlap(target_of):
    base = _solo("transport_loss", 580)
    pin = target_of(base)
    alone = _resolve(CELLS, [_row(581, "ran_radio", pin)])[0][0].target.cell_keys
    first = _row(580, "ran_radio", f"ENB:{_enb_of(base)}")
    scs, _, log = _resolve(CELLS, [first, _row(581, "ran_radio", pin)])
    assert scs[1].target.cell_keys == alone and "겹쳐 다른 대상" not in log


def test_like_target_reuses_the_original_cells_even_when_they_overlap():
    # LIKE 는 원본 대상(이력)을 다시 쓴다 — 합성 마스터에는 원본 이력이 없어 동작 시험은 못 한다. 대신 소스에서
    # 피할 셀이 자동 대상(`auto`)일 때만 채워지는 것을 지킨다(`avoid = ... if auto else set()`)
    src = open(F.__file__, encoding="utf-8").read()
    assert "avoid = set(busy or ()) if auto else set()" in src
    i = src.index("auto = ")
    assert "LIKE" in src[max(0, i - 600):i + 600], "auto 판정에 LIKE 가 빠지는 자리"


# ── ⑥ 먼저 정해진 대상은 바뀌지 않는다(cache) ───────────────────────────

def test_a_cached_target_is_stable_when_another_fault_starts_or_ends():
    cache = {}
    solo = _resolve(CELLS, [_row(601, "ran_congestion")], cache)[0][0].target.cell_keys
    first = _row(600, "ran_radio", f"ENB:{_enb_of(solo)}")
    # 뒤 장애(602)가 시작해도 601 은 처음 대상 그대로 — 자기 해석은 한 번뿐이다
    scs, _, _ = _resolve(CELLS, [first, _row(601, "ran_congestion"), _row(602, "transport_loss")], cache)
    assert scs[1].target.cell_keys == solo
    # 앞 장애(600)가 끝나 목록에서 빠져도 그대로
    scs2, _, _ = _resolve(CELLS, [_row(601, "ran_congestion"), _row(602, "transport_loss")], cache)
    assert scs2[0].target.cell_keys == solo and scs2[1].target.cell_keys == scs[2].target.cell_keys


def test_the_busy_set_only_holds_other_faults_that_are_live():
    cache = {}
    _resolve(CELLS, [_row(610, "ran_radio", "ENB:100001")], cache)
    gone = cache[610][1].target.cell_keys
    scs, _, _ = _resolve(CELLS, [_row(611, "ran_radio", f"ENB:{_enb_of(gone)}"), _row(612, "ran_congestion")], cache)
    # 610 은 활성 목록에 없다 — 그 셀은 피할 셀이 아니다(끝난 장애를 피하지 않는다)
    plain = _solo("ran_congestion", 612)
    assert scs[1].target.cell_keys == plain


# ── ⑦ 예외가 나도 `_AVOID` 는 비어야 한다 ─────────────────────────────────

def test_avoid_is_cleared_even_when_the_second_pick_raises(monkeypatch):
    base = _solo("ran_congestion", 621)
    first = _row(620, "ran_radio", f"ENB:{_enb_of(base)}")
    cache = {}
    _resolve(CELLS, [first], cache)
    real = F._resolve_target
    seen = []

    def boom(*a, **k):
        if F._AVOID:
            seen.append(set(F._AVOID))
            raise RuntimeError("pick failed")
        return real(*a, **k)
    monkeypatch.setattr(F, "_resolve_target", boom)
    with pytest.raises(RuntimeError), contextlib.redirect_stdout(io.StringIO()):
        F.resolve_active(CELLS, T0, SEED, [first, _row(621, "ran_congestion")], cache)
    assert seen, "다시 고르는 단계에서 `_AVOID` 가 채워져 있어야 시험이 의미 있다"
    assert F._AVOID == set()
