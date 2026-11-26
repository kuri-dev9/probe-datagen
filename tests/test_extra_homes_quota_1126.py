"""추가 가입자 셀별 정원 배정(`PemdbGen._extra_homes` · 2026-11-26 동구) — 새 동작 시험.

사용자 지적: 「0명 271곳은 명백한 버그 · 상위 셀에 몰리게 하지 마 · 대부분 비슷하게 · 야마노테선과 주택가 위주」.
전에는 `CellRing` 가중 추첨이라 가상 노드 반올림으로 271셀이 0명, 한 셀이 808명이었다. 지금은 정원대로 나눈다.
셀은 전부 합성이고 DB·생성기를 만들지 않는다(`_extra_homes` 는 `self.gen.cells` 만 쓴다).
"""
from __future__ import annotations

from collections import Counter
from types import SimpleNamespace

from datagen.pemdb1 import PemdbGen


def _stub(n_cells):
    cells = [SimpleNamespace(cell_key=f"{1000 + i // 3}-{i % 3}") for i in range(n_cells)]
    return SimpleNamespace(gen=SimpleNamespace(cells=cells), _extra_home_w=None)


def _homes(n, n_cells=300, resid=None, line=None, boost=1.5):
    s = _stub(n_cells)
    out = PemdbGen._extra_homes(s, n, resid, line or set(), boost)
    return s, out


def test_no_cell_is_left_empty_and_the_total_is_exact():
    s, out = _homes(80_000 // 8)                       # 셀 300개 · 1만 명
    c = Counter(out)
    assert len(out) == 10_000 and len(c) == 300        # 0명 셀 없음 · 합 정확
    assert max(c.values()) - min(c.values()) <= 1      # 몫이 같으면 정원 차이 1명 이내


def test_boosted_cells_get_their_share_and_everyone_else_is_even():
    line = {f"{1000 + i // 3}-{i % 3}" for i in range(0, 60)}     # 야마노테선 셀 60개
    s, out = _homes(10_000, line=line)
    c = Counter(out)
    w = s._extra_home_w
    total_w = sum(w)
    for i, wi in enumerate(w):
        assert abs(c[i] - 10_000 * wi / total_w) <= 1.0            # 모든 셀이 정원 ±1명(몫 비례)
    assert w.count(1.5) == 60 and sum(1 for x in w if x == 1.0) == 240


def test_resident_cells_above_the_mean_are_boosted_and_both_conditions_boost_only_once():
    cells_n = 12
    keys = [f"{1000 + i // 3}-{i % 3}" for i in range(cells_n)]
    resid = {k: (10.0 if i < 4 else 1.0) for i, k in enumerate(keys)}          # 앞 4셀이 평균보다 많은 주택가
    s, _ = _homes(1200, n_cells=cells_n, resid=resid, line={keys[0], keys[5]})   # 0번은 주택가이자 노선
    w = s._extra_home_w
    assert w[0] == 1.5 and w[1] == w[2] == w[3] == 1.5 and w[5] == 1.5          # 주택가 4 + 노선 1(겹침은 한 번)
    assert w.count(1.5) == 5 and w.count(1.0) == 7


def test_any_prefix_stays_proportional_because_the_bar_decides_how_many_are_used():
    """막대(`extra_n`)가 앞에서부터 몇 명을 쓰는지 정한다 — 어느 앞 순번에서 잘라도 정원 비례다."""
    s, out = _homes(5_000, n_cells=120)
    for k in (130, 777, 2_501, 4_999):
        c = Counter(out[:k])
        assert len(out[:k]) == k
        for i, wi in enumerate(s._extra_home_w):
            assert abs(c[i] - k * wi / sum(s._extra_home_w)) <= 1.0


def test_the_same_inputs_give_the_same_homes_regardless_of_cell_order_side_effects():
    a = _homes(3_000)[1]
    b = _homes(3_000)[1]
    assert a == b                                                              # 난수를 쓰지 않는다(CRC 동률 순)
