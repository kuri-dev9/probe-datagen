"""시나리오 대상 셀 선택 — 깨면 채점이 RCA 품질을 재지 않는다.

정답 셀 40개 중 **4개가 어떤 창에서도 분모를 못 채웠다**(실측 2026-10-26).
관측 불가한 셀에 장애를 넣으면 채점이 "그 셀이 보였는가"를 재게 되므로,
`pemdb1.scenario_pool` 이 `traffic_weight` 하한으로 후보를 좁힌다. 근거는
`pemdb1.WEIGHT_FLOOR` 주석에 있다.

**DB 를 쓰지 않는다** — `tests/test_session_model.py` 머리말의 집안 규칙이고,
여기서는 특히 중요하다: 이 선택이 **창고와 무관하다는 것**이 설계의 핵이다
(`패킷/세션` 은 셀의 속성이 아니라 그 실행의 우연이라 창고로 고르면 그
시드에만 맞는다). 창고를 읽는 테스트를 쓰면 그 성질을 검증하지 못한다.
"""

from __future__ import annotations

import collections
import math
import random

from datagen import pemdb1 as P
from datagen.cells import Cell
from datagen.scenarios import _cells_in_one_sgg


def _cell(enb: str, cellnum: int, sgg: str = "12000") -> Cell:
    """`scenario_pool` · `_cells_in_one_sgg` 가 읽는 필드만 채운다.

    `Cell` 은 필드가 25개다. 전부 채우면 테스트가 생성기 스키마에 묶이고,
    늘어나면 여기서 `TypeError` 가 나므로 조용히 어긋나지 않는다.
    """
    return Cell(
        cell_key=f"{enb}-{cellnum}", enb_id=enb, cell_id=cellnum,
        enb_seq=0, c_uid="", tac=0, plmn="45005", vlan_id="",
        latitude=37.5, longitude=127.0,
        adm_sido_cd="12", adm_sido_nm="", adm_sgg_cd=sgg, adm_sgg_nm="",
        adm_emd_cd="12000011", adm_emd_nm="", geo_status="MAPPED",
        rat="EUTRAN", mme_id="0001", sgw_id="0037", pgw_id="0037",
        ims_pgw_id="0037", traffic_weight=P._weight(enb, cellnum),
        display_name="")


def _many(n_enb: int = 60, per_enb: int = 36) -> list[Cell]:
    return [_cell(f"13{5000 + e}", c)
            for e in range(n_enb) for c in range(per_enb)]


# --------------------------------------------------------------------------
# 하한 자체
# --------------------------------------------------------------------------


def test_pool_is_exactly_the_cells_above_the_floor():
    cells = _many()
    pool = P.scenario_pool(cells)
    assert {c.cell_key for c in pool} == {
        c.cell_key for c in cells
        if P._weight(c.enb_id, c.cell_id) >= P.WEIGHT_FLOOR}
    # **순서를 바꾸지 않는다** — 교체분이 목록 순서에서 나온다
    # (`_cells_in_one_sgg`). 정렬하면 그 선택이 조용히 달라진다.
    assert pool == [c for c in cells if c in pool]


def test_pool_does_not_depend_on_any_seed():
    """**이 선택이 시드와 무관하다는 것이 설계의 핵이다.**

    `패킷/세션` 은 셀의 속성이 아니라 "단일 스트림에서 몇 번째로 뽑았나"다
    (`generator.py:197` 계열당 `Sampler` 하나 · `profile.py:337` 에 셀 키가
    들어가지 않는다). 그래서 창고 조회로 고르면 그 시드에만 맞고(시드 4개
    모의에서 미달 집합이 `0/4/3/0`), 사전 모의는 순환이다. `_weight` 는
    sha256 이라 **인자가 셀 키뿐**이다 — 그 성질을 서명으로 고정한다.
    """
    assert P._weight.__code__.co_varnames[:2] == ("enb_id", "cellnum")
    cells = _many()
    first = [c.cell_key for c in P.scenario_pool(cells)]
    for seed in (1, 2, 20260903):
        random.seed(seed)      # 전역 난수를 흔들어도 바뀌지 않아야 한다
        assert [c.cell_key for c in P.scenario_pool(cells)] == first


def test_floor_keeps_about_a_third_of_the_cells():
    """풀이 **필요한 40개보다 충분히 크고, 전부는 아니다.**

    `_weight` 는 `0.25 ~ 4.0` 의 **로그 균등**이므로(그 함수 주석) 하한
    `w` 이상의 비율은 `1 - log(w/0.25)/log(16)` 이다 — 1.56 이면 약 34%.
    실측도 `766/2161 = 35.4%` 로 같다. 이 수가 크게 달라지면 하한이
    의도와 다른 자리에 가 있다는 뜻이다(너무 높으면 다양성을 잃고, 너무
    낮으면 미달이 끼어 테스트가 간헐 실패한다).
    """
    cells = _many()
    got = len(P.scenario_pool(cells)) / len(cells)
    want = 1.0 - math.log(P.WEIGHT_FLOOR / 0.25) / math.log(16.0)
    assert abs(got - want) < 0.03, (got, want)
    assert 0.25 < got < 0.45


def test_floor_is_not_the_intermittent_value():
    """**`1.20` 을 쓰지 않는다.** 그 하한의 풀은 미달 0.1% 라 40셀을 뽑으면
    약 4% 확률로 미달이 끼고, 그러면 이 테스트들이 **간헐 실패**한다 —
    "가끔 빨간 것"은 결국 아무도 보지 않는다. 실측 미달 4개의 weight 는
    `0.26 · 0.30 · 0.31 · 0.86` 이고 30분 창 2개는 `0.30 · 0.69` 다."""
    assert P.WEIGHT_FLOOR >= 1.56
    for w in (0.26, 0.30, 0.31, 0.69, 0.86):
        assert w < P.WEIGHT_FLOOR


def test_pool_does_not_collapse_onto_a_few_enbs():
    """풀이 특정 eNB·시군구에 몰리면 시나리오가 다양성을 잃는다.

    `_weight` 가 `(enb_id, cellnum)` 해시라 eNB 안에서도 섞이고, 그래서
    모든 eNB 가 풀에 남는다. 한 eNB 로 쏠리면 `CLUSTER` 를 만들려는
    시나리오가 사실상 "그 eNB 장애"가 된다.
    """
    cells = _many()
    pool = P.scenario_pool(cells)
    enb = collections.Counter(c.enb_id for c in pool)
    assert len(enb) == len({c.enb_id for c in cells})      # eNB 60 전부 남는다
    assert max(enb.values()) / len(pool) < 0.05


# --------------------------------------------------------------------------
# 추첨 — **고른 뒤 교체한다**. 풀을 좁히지 않는다
# --------------------------------------------------------------------------


def test_eligible_keeps_the_same_sgg_and_only_swaps_cells():
    """**추첨을 바꾸지 않는다.**

    후보 풀 자체를 좁히면 시군구별 셀 수가 달라져 `rng.choice` 가 **다른
    시군구**를 고르고, 정답 셀이 통째로 바뀐다 — 실측으로 40개 중 유지
    0개였다. 바뀌어야 하는 것은 "미달 셀이 빠지는 것"뿐이므로, 고른 뒤
    교체한다.
    """
    # 시군구마다 eNB 6개(216셀) — 하한 위가 약 34% 라 교체분이 40개를
    # 채울 수 있다. 모자라면 아래 `..._shrinks_when_the_sgg_runs_out` 쪽이다.
    cells = [_cell(f"13{5000 + e}", c, sgg=f"1200{e % 3}")
             for e in range(18) for c in range(36)]
    pool = {c.cell_key for c in P.scenario_pool(cells)}

    plain, sgg_a = _cells_in_one_sgg(cells, random.Random(777), 40)
    swapped, sgg_b = _cells_in_one_sgg(cells, random.Random(777), 40, pool)

    assert sgg_a == sgg_b                       # 같은 시군구
    assert len(swapped) == len(plain)           # 같은 개수
    assert all(c.cell_key in pool for c in swapped)
    assert all(c.adm_sgg_cd == sgg_a for c in swapped)
    # 전과 겹치는 것이 있어야 한다 — 전부 달라지면 "교체"가 아니라 재추첨이다
    assert {c.cell_key for c in swapped} & {c.cell_key for c in plain}


def test_eligible_does_not_shift_later_draws():
    """`rng` 소비 횟수가 같아야 한다 — 달라지면 뒤따르는 시나리오 배치가
    전부 밀린다(같은 시드인데 다른 구간에 깔린다)."""
    cells = [_cell(f"13{5000 + e}", c, sgg=f"1200{e % 3}")
             for e in range(9) for c in range(36)]
    pool = {c.cell_key for c in P.scenario_pool(cells)}

    a = random.Random(777)
    _cells_in_one_sgg(cells, a, 40)
    b = random.Random(777)
    _cells_in_one_sgg(cells, b, 40, pool)
    assert a.random() == b.random()


def test_eligible_shrinks_when_the_sgg_runs_out():
    """그 시군구에 관측 가능한 셀이 모자라면 **적은 수로 돌려준다.**

    없는 셀을 지어내거나 미달 셀로 채우지 않는다 — 후자면 하한을 둔 의미가
    사라진다. 수가 줄어든 것은 `write_truth` 의 `cells` 길이로 드러난다.
    """
    cells = [_cell(f"13{5000 + e}", c) for e in range(3) for c in range(36)]
    pool = {c.cell_key for c in P.scenario_pool(cells)}
    got, _ = _cells_in_one_sgg(cells, random.Random(777), 40, pool)
    assert 0 < len(got) < 40
    assert all(c.cell_key in pool for c in got)


def test_eligible_falls_back_rather_than_returning_nothing():
    """교체할 셀이 없으면 **빈손으로 돌려주지 않는다.**

    대상이 0개면 시나리오가 통째로 비고, 그것은 "주입했는데 아무 일도
    없었다"로 나타나 원인을 찾기 어렵다(`corridor_cells` 머리말과 같은 선택).
    """
    cells = [_cell("135000", c) for c in range(36)]
    got, _ = _cells_in_one_sgg(cells, random.Random(777), 40, set())
    assert got == []        # 빈 집합이면 교체분이 없다 — 그 사실이 드러난다
    got2, _ = _cells_in_one_sgg(cells, random.Random(777), 40, None)
    assert len(got2) == 36  # 거르지 않으면 전량이 후보다


# --------------------------------------------------------------------------
# 하한은 **트리거 지표의 분모 종류**에 따라 걸린다
# --------------------------------------------------------------------------


def test_floor_applies_only_to_packet_denominator_kpis():
    """호 수 분모 시나리오에 하한을 걸면 **불필요한 쏠림**이다.

    `U-12`·`U-14` 의 분모는 패킷이고 `min_denom=800` 이라(graphio
    `kpi.py:178·184` · `detector.py:832`) 셀당 U-Plane 표본이 얇아 30분
    창에서도 미달이 나온다. 반면 `C-01`~`C-05`·`N-02`·`PSF` 는 `min_sample`
    기본값(20)이고 셀당 호가 분당 수 건이라 창 확대로 찬다 — 세션 31개인
    셀도 호는 수백 건이다.

    실측: 전체 셀 축에 걸었을 때 `SC-02`(C-02)의 60셀 중 **39개**가 불필요
    하게 교체됐고, 지표별로 바꾼 뒤 **0개**다. `SC-07`(U-12)은 양쪽 23개로
    같다 — 그쪽은 필요한 교체다.
    """
    from datagen.scenarios import KIND_SPECS, PACKET_DENOM_KPIS, _needs_weight_floor

    assert _needs_weight_floor("transport_loss") is True
    assert _needs_weight_floor("compound_causal_failure") is True
    assert _needs_weight_floor("ran_radio") is False
    assert _needs_weight_floor("nsa_nr_leg") is False
    assert _needs_weight_floor("gw_resource", "gw_node_down") is False
    # 하나라도 패킷 분모면 그 추첨은 하한을 받는다 (한 목록이 여러
    # 시나리오를 먹인다 — 보수적으로 거는 쪽이 맞다)
    assert _needs_weight_floor("ran_radio", "transport_loss") is True
    # 모르는 kind 에 걸려 터지지 않는다 — `KIND_SPECS` 에 없으면 거짓이다
    assert _needs_weight_floor("없는_kind") is False
    # 집합이 graphio 의 `denom=paypkt_dn` 지표와 같아야 한다
    assert PACKET_DENOM_KPIS == frozenset({"U-12", "U-14"})
    # kind→지표 대응을 호출부에 적지 않는다(사본 방지)
    assert KIND_SPECS["transport_loss"]["expect_kpi"] in PACKET_DENOM_KPIS
    assert KIND_SPECS["ran_radio"]["expect_kpi"] not in PACKET_DENOM_KPIS


def test_truth_records_how_the_cells_were_chosen(tmp_path):
    """정답 파일이 **하한을 적었는지**. 없으면 채점이 탐지율을 무조건으로 읽는다.

    graphio `pipeline._truth_diagnostics` 가 이 블록을 읽어 표 앞 경고로
    낸다. **값을 넘기므로 그쪽 문구에 숫자가 없다** — 하한을 고칠 때 경고가
    따라오지 않으면 사본이 둘이 된다(불변규칙 17 의 결).
    """
    import datetime

    doc = P.write_truth(tmp_path / "t.json", datetime.datetime(2026, 10, 22, 3, 0),
                        35, [], {"weight_floor": P.WEIGHT_FLOOR,
                                 "applies_to_kpi": ["U-12"],
                                 "cells_total": 2161, "cells_eligible": 766})
    assert doc["cell_selection"]["weight_floor"] == P.WEIGHT_FLOOR
    assert doc["cell_selection"]["applies_to_kpi"] == ["U-12"]
    # 안 넘기면 **키가 아예 없어야** 한다 — 빈 dict 를 넣으면 채점이
    # "하한 0 으로 골랐다"로 읽는다(불변규칙 17)
    plain = P.write_truth(tmp_path / "t2.json",
                          datetime.datetime(2026, 10, 22, 3, 0), 35, [])
    assert "cell_selection" not in plain


def test_each_draw_is_wired_to_the_right_floor():
    """**판정 함수가 맞는 것과 추첨마다 그것을 쓰는 것은 다른 명제다.**

    `_needs_weight_floor` 만 검증하면 호출부에서 인자를 빼먹어도 통과한다 —
    변이 테스트에서 `compound_cells` 추첨의 하한을 떼었을 때 아무 테스트도
    실패하지 않았다. 그래서 `build_scenarios` 의 **결과**로 확인한다.

    `SC-07`(transport_loss · U-12)은 하한을 받고, `SC-02`(ran_radio · C-02)는
    받지 않는다 — 후자가 하한 아래 셀을 들고 있어야 "안 걸렸다"가 증명된다.
    """
    from datagen.scenarios import build_scenarios

    cells = [_cell(f"13{5000 + e}", c, sgg=f"1200{e % 2}")
             for e in range(24) for c in range(36)]
    pool = {c.cell_key for c in P.scenario_pool(cells)}
    # **네 추첨을 다 덮는다.** `_cells_in_one_sgg` 호출이 넷이고
    # (cluster / loss / nsa / compound) 각각 다른 시나리오를 먹인다 —
    # 하나만 보면 다른 자리에서 인자가 빠져도 통과한다. 구간을 넉넉히 줘야
    # 넷이 다 배치된다(35분이면 앞쪽 셋만 깔린다).
    scs = build_scenarios(cells, 240, random.Random(777),
                          "SC-02,SC-07,SC-13,SC-21", eligible=pool)
    got = {sc.id: set(sc.target.cell_keys or ()) for sc in scs}
    assert {"SC-02", "SC-07", "SC-13", "SC-21"} <= set(got), sorted(got)

    # 패킷 분모 — 하한을 받는다
    for sid in ("SC-07", "SC-21"):
        assert got[sid] <= pool, (sid, sorted(got[sid] - pool)[:5])
    # 호 수 분모 — 걸러지지 않는다. 하한 아래 셀이 남아 있어야 "안 걸렸다"가
    # 증명된다(그냥 부분집합이 아닌 것만 보면 우연히 통과할 수 있다)
    for sid in ("SC-02", "SC-13"):
        assert got[sid] - pool, f"{sid}(호 수 분모)에 하한이 걸렸다"
