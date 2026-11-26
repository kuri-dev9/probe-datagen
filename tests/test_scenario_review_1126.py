"""11-26 시나리오 검수 반영 — 생성기 쪽 동작을 지킨다 (DB 를 쓰지 않는다).

* 17번(내부 29) 특정 서비스망 접속 불가 — 기본 인터넷 APN(spモード)을 후보에서 뺀다
* 21번 접속 관리 장비 과부하 — MME 가 EMM 22 로 거절하고 기지국 해제 요청은 없다
* 22번 연쇄 복합 장애 — DNS 실패는 무응답(900)만, SERVFAIL(2)은 없다
* 13번 5G 불가로 4G 집중 — 그 지역의 기존 5G 단말도 5G 연결을 잃는다
"""
from __future__ import annotations

import random
from types import SimpleNamespace

import pytest

from datagen import generator as G
from datagen import scenarios as S

import test_nr_fields as NF

gen = NF.gen


def _sc(cell, **tgt):
    return SimpleNamespace(active_cells_cache={cell.cell_key}, target=SimpleNamespace(mme_id="0007", **tgt))


# ---- 17번 ------------------------------------------------------------------

@pytest.mark.parametrize("apn,expect", [
    ("spmode.ne.jp", True), ("spmode.ne.jp.mnc010.mcc440.gprs", True), ("SPMODE.ne.jp.mnc010.mcc440.gprs", True),
    ("mopera.net", False), ("spmode.ne.jp.evil", True), ("xspmode.ne.jp", False), ("", False), (None, False),
])
def test_default_internet_apn_is_recognised_by_its_ni_prefix(apn, expect):
    got = G._is_default_internet_apn(apn)
    # 접두 일치만 본다 — `spmode.ne.jp.evil` 도 NI 뒤가 `.` 로 이어지면 같은 기본 APN 이다
    assert got is expect


def test_the_filter_keeps_the_old_pool_when_every_candidate_is_default():
    pool = ["spmode.ne.jp", "spmode.ne.jp.mnc010.mcc440.gprs"]
    assert [a for a in pool if not G._is_default_internet_apn(a)] == []
    assert ([a for a in pool if not G._is_default_internet_apn(a)] or pool) == pool       # 후보가 0개면 전처럼 둔다
    mixed = pool + ["mopera.net"]
    assert [a for a in mixed if not G._is_default_internet_apn(a)] == ["mopera.net"]


# ---- 21번 ------------------------------------------------------------------

def test_mme_overload_rejects_with_emm22_and_sends_no_enb_release_request(gen):
    cell = gen.cells[0]
    sc = _sc(cell)
    emm = drop = 0
    for s in range(300):
        rec = NF._rec(gen, NF._sub(gen, "1"), ct="1")
        rec.set("MME_ID", "0007")
        if not S._mme_overload(rec, sc, 1.0, random.Random(s), cell):
            continue
        # 어느 갈래든 기지국이 해제를 요청한 호는 없다(Cause 500 은 MME 가 보낸 Release Command 쪽)
        assert rec.get("UEContextReleaseRequest_Cause") in ("0", 0, "", None), rec.get("UEContextReleaseRequest_Cause")
        assert rec.get("UEContextRelease_Cause") == "500"
        if rec.get("emm_error_Cause") == "22":
            emm += 1
            assert rec.get("first_error_cause") == "22"
        else:
            drop += 1
    assert emm > 0 and drop > 0, (emm, drop)
    assert S._MME_CONGESTION_SPEC == {"1": "emm_congestion_attach", "3": "emm_congestion_service",
                                      "4": "emm_congestion_service", "5": "emm_congestion_tau"}


def test_mme_overload_leaves_other_mme_and_cells_alone(gen):
    cell, other = gen.cells[0], gen.cells[1]
    rec = NF._rec(gen, NF._sub(gen, "1"), ct="1")
    rec.set("MME_ID", "0009")
    assert not S._mme_overload(rec, _sc(cell), 1.0, random.Random(1), cell)
    rec2 = NF._rec(gen, NF._sub(gen, "1"), ct="1")
    rec2.set("MME_ID", "0007")
    assert not S._mme_overload(rec2, _sc(cell), 1.0, random.Random(1), other)


# ---- 22번 ------------------------------------------------------------------

def _dns_rec(gen):
    return S.Rec(gen.templates["dns"].copy(), gen.index["dns"], "dns")


def test_compound_failure_makes_dns_no_answer_only_never_servfail(gen):
    cell = gen.cells[0]
    sc = _sc(cell)
    fails, n = [], 6000
    for s in range(n):
        rec = _dns_rec(gen)
        if S._compound_causal_failure(rec, sc, 1.0, random.Random(s), cell):
            fails.append(rec)
    assert fails
    assert {r.get("ReplyCode") for r in fails} == {"900"}, "SERVFAIL(2) 은 DNS 서버가 보낸 응답이라 쓰지 않는다"
    assert all(r.get("ResponseTime") == "0.0" and int(r.num("AnswerCount")) == 0 for r in fails)
    # 실패율은 고정 70% 가 아니라 1차 손실(≈19%)에서 나온다
    assert 0.12 < len(fails) / n < 0.28, len(fails) / n


def test_compound_failure_only_touches_dns_series_in_target_cells(gen):
    cell, other = gen.cells[0], gen.cells[1]
    tr = S.Rec(gen.templates["transport"].copy(), gen.index["transport"], "transport")
    assert not S._compound_causal_failure(tr, _sc(cell), 1.0, random.Random(1), cell)
    assert not S._compound_causal_failure(_dns_rec(gen), _sc(cell), 1.0, random.Random(1), other)


# ---- 13번 ------------------------------------------------------------------

def test_sa_fallback_takes_the_5g_leg_from_existing_5g_devices(gen):
    cell = gen.cells[0]
    sc = _sc(cell)
    hit = 0
    for s in range(200):
        rec = NF._nr_rec(gen, change=3, ps=1)
        rec.set("ue_dcnr", "1")
        assert rec.num("initial_nr_conn_time") > 0
        if S._sa_fallback_nr_down(rec, sc, 1.0, random.Random(s), cell):
            hit += 1
            assert rec.get("initial_nr_conn_time") == "0.0" and NF._diff(rec) == 1
    assert hit == 200, "강도 1.0 이면 5G 능력 단말 호 전부"
    half = sum(S._sa_fallback_nr_down(NF._nr_rec(gen), sc, 0.5, random.Random(s), cell) for s in range(400))
    assert 140 < half < 260, half


def test_sa_fallback_ignores_non_5g_and_other_cells(gen):
    cell, other = gen.cells[0], gen.cells[1]
    rec = NF._nr_rec(gen)
    rec.set("ue_dcnr", "0")
    assert not S._sa_fallback_nr_down(rec, _sc(cell), 1.0, random.Random(1), cell)
    rec2 = NF._nr_rec(gen)
    assert not S._sa_fallback_nr_down(rec2, _sc(cell), 1.0, random.Random(1), other)
    assert rec2.num("initial_nr_conn_time") > 0
