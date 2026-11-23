"""추가 가입자 기록은 장애 변형을 받지 않는다 (2026-11-24 · 사용자 「장애와 모든 활동은 기본 가입자가 한다 · 추가 가입자는 연결만 유지」).

8번 연결 끊김 증가 F-581 에서 절단 45건이 추가 가입자 TAU(호 유형 5)에 실리고 실제 통화(유형 3·4)에는 9건뿐이라 C-04 가 서지 않았다.
`extra_drafts` 의 기록이 공통 루프의 `mutate("lte_call", …)`(절단·실패 Cause·지연)을 받던 것을 뺐다(맥스 · `pemdb1.py`).
**서비스가 없어지는 일**(장비 정지 거름 · 정지 끊김 명부 · 재연결)은 변형이 아니라 「그 셀에 접속할 수 없다」는 사실이라 그대로 받는다.

합성 셀 마스터 + 고정 시드 · DB 없음 — `test_extra_equipment_down_1121` 의 하니스를 쓴다(맥스의 임시 확인 `/tmp/dg_s/xchk/chk.py` 를 옮겼다).
기준은 **같은 시드의 장애 없는 실행**이라 수정 전 백업 파일에 기대지 않는다.

1. 장애를 걸어도 추가 가입자 기록이 장애 없을 때와 한 건도 다르지 않다(7종).
2. 그 장애는 실제로 기본 가입자 기록을 바꾼다(시험이 공허하지 않다).
3. 기본 가입자 기록은 추가 가입자가 0명일 때와 같다.
4. 서비스가 없어지는 장애(SGW·eNB 정지)는 추가 가입자에게 계속 걸린다.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
import test_extra_equipment_down_1121 as X

from datagen import pemdb1 as P

#: C-Plane(lte_call)에 변형이 걸리는 종류 — 화면 8·9·10·17·20·21·14번(`ran_drop_surge`·`ran_radio`·`mobility`·`apn_pdn_failure`·
#: `gw_resource`·`mme_overload`·`ue_model`)
KINDS = ["ran_drop_surge", "ran_radio", "mobility", "gw_resource", "mme_overload", "apn_pdn_failure", "ue_model"]
TARGET = "ENB:500025"
MINUTES = 10
DOWN_KINDS = {"gw_node_down": dict(start=2, dur=6, target=None),
              "enb_s1_outage": dict(start=2, dur=8, target=TARGET)}


def _run(extra_n: int, kind: str | None, **fk):
    g = X._make(P, extra_n)
    if kind:
        scs, bad = g.set_fault_rows([X._fault(kind, **(fk or dict(start=2, dur=8, target=TARGET)))])
        assert scs and not bad, bad
    out = [g.records(m, X.T0 + timedelta(minutes=m)) for m in range(MINUTES)]
    i = g.gen.index["lte_call"]["IMSI"]
    ex = [[r for r in rows if r[i] in g.extra_by_imsi] for rows in out]
    base = [[r for r in rows if r[i] not in g.extra_by_imsi] for rows in out]
    return g, ex, base


@pytest.fixture(scope="module")
def plain():
    """장애 없는 실행 — 추가 5천 명 · 0명"""
    return _run(X.XN, None), _run(0, None)


@pytest.fixture(scope="module", params=KINDS)
def faulted(request, plain):
    return request.param, _run(X.XN, request.param), _run(0, request.param)


def _diff(a, b) -> int:
    """분마다 기록 목록이 다른 건수 — 길이가 달라도 센다"""
    return (sum(len(x) != len(y) for x, y in zip(a, b))
            + sum(p != q for x, y in zip(a, b) for p, q in zip(x, y)))


def test_extras_exist_and_sit_in_the_faulted_enb(plain):
    """시험 자료가 의미가 있다 — 추가 가입자 기록이 충분하고 장애 대상 eNB 의 셀에도 나온다"""
    (g, ex, _b), _ = plain
    f, c = g.gen.index["lte_call"]["First_eNB_ID"], g.gen.index["lte_call"]["First_Cell_ID"]
    assert sum(map(len, ex)) > 1000
    assert any(str(r[f]) == "500025" for rows in ex for r in rows), "장애 대상 eNB 에 추가 가입자 기록이 없다"


def test_fault_does_not_change_a_single_extra_record(faulted, plain):
    kind, (_g, ex_f, _bf), _zero = faulted
    (_gp, ex_n, _bn), _ = plain
    assert _diff(ex_f, ex_n) == 0, f"{kind}: 추가 가입자 기록이 장애 때문에 달라졌다"


def test_the_same_fault_does_change_the_regular_records(faulted, plain):
    """공허 방지 — 같은 장애가 기본 가입자 기록은 실제로 바꾼다(절단·실패·지연이 걸렸다)"""
    kind, (_g, _ef, base_f), _zero = faulted
    (_gp, _en, base_n), _ = plain
    assert _diff(base_f, base_n) > 0, f"{kind}: 장애가 기본 가입자 기록을 바꾸지 못했다 — 비교가 공허하다"


def test_regular_records_are_the_same_with_zero_or_many_extras(faulted):
    """기본 가입자 쪽은 추가 가입자 유무와 무관하다 — 추가 가입자가 난수를 써서 기본 가입자 값을 흔들지 않는다"""
    kind, (_g, _ex, base_x), (_g0, _e0, base_0) = faulted
    assert _diff(base_x, base_0) == 0, f"{kind}: 기본 가입자 기록이 추가 가입자 유무에 따라 달라졌다"


@pytest.mark.parametrize("kind", list(DOWN_KINDS))
def test_service_removal_still_reaches_the_extras(kind):
    """장비 정지는 변형이 아니라 접속 불가라는 사실 — 추가 가입자 기록도 장애 없을 때와 달라져야 한다(끊김·재연결 · 억제)"""
    (_g, ex_n, _b), (_g2, ex_f, _b2) = _run(X.XN, None), _run(X.XN, kind, **DOWN_KINDS[kind])
    assert _diff(ex_f, ex_n) > 0, f"{kind}: 서비스가 없어지는 장애가 추가 가입자에게 걸리지 않았다"
