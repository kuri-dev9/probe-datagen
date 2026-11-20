"""`identity.synth_mdn` — 번호의 나라는 그 가입자 IMSI 의 MCC 가 정한다 (2026-11-20 · 사용자 「일본 100%」 · 맥스).

일본(MCC 440·441)은 090/080 + 8자리, 그 밖(한국 450 · 없음)은 010 + 8자리. 일본 망의 한국 로머는 한국 번호여야 맞아 설정이 아니라 IMSI 를 본다.
"""
from __future__ import annotations

import re

import pytest

from datagen import identity as I


def test_japanese_mcc_gives_090_or_080_with_eleven_digits_and_a_nonzero_first_subscriber_digit():
    for mcc in ("44010", "44011", "44020", "441"):
        for i in (0, 1, 99, 10 ** 7, 9 * 10 ** 7, 123456789):
            n = I.synth_mdn(i, mcc + "0" * (7 - len(mcc)) if len(mcc) < 7 else mcc)
            assert re.fullmatch(r"0[89]0[1-9]\d{7}", n), n


def test_korea_or_unknown_keeps_010():
    for mcc in (None, "", "4500500", "45005"):
        n = I.synth_mdn(12345, mcc)
        assert re.fullmatch(r"010[2-9]\d{7}", n), n


def test_default_without_mcc_is_unchanged():
    assert I.synth_mdn(7) == I.synth_mdn(7, None) and I.synth_mdn(7).startswith("010")


def test_japanese_numbers_are_injective_over_a_range_and_both_prefixes_occur():
    seen = {I.synth_mdn(i, "4401000") for i in range(0, 200_000, 3)}
    assert len(seen) == len(range(0, 200_000, 3)), "서로 다른 인덱스는 서로 다른 번호"
    heads = {n[:3] for n in (I.synth_mdn(i * 9 * 10 ** 7 // 2, "4401000") for i in range(4))}
    assert heads == {"090", "080"}


def test_roaming_korean_imsi_in_a_japan_run_still_gets_a_korean_number():
    assert I.synth_mdn(5, "4500500").startswith("010") and I.synth_mdn(5, "4401000").startswith("09")


# --- `PemdbGen._reseat_subscribers` · `_make_sa_hidden` 뒤의 번호 (11-20 10:42 실측 결함 · 에스터) -------------

import dataclasses
import random
from types import SimpleNamespace

from datagen import pemdb1 as P

JP, KR = "4401000", "4500500"


class _Dist:
    def vendor_model(self):
        return "V", "M"


def _fake(n, first_mcc, home=JP, roam=KR):
    """가입자 `n` 명을 **일부러 어긋난 MCC**(`first_mcc`)로 만든 가짜 생성기 — `_make_subscribers` 직후 상태."""
    subs = [SimpleNamespace(imsi=I.synth_imsi(first_mcc, i), mdn=I.synth_mdn(i, first_mcc), imei="", vendor="", model="",
                            mcc_mnc=first_mcc) for i in range(n)]
    return SimpleNamespace(gen=SimpleNamespace(subs=subs, sub_by_imsi={}), dist=_Dist(),
                           plmn=SimpleNamespace(imsi_prefix=home, roaming=roam))


def _country_ok(sub):
    return sub.mdn[:3] in (("090", "080") if sub.mcc_mnc[:3] in ("440", "441") else ("010",))


@pytest.mark.parametrize("first", [KR, JP])      # 먼저 한국으로 만들었든 일본으로 만들었든
def test_reseat_makes_every_number_follow_the_final_mcc(first):
    f = _fake(4000, first)
    P.PemdbGen._reseat_subscribers(f, random.Random(1))
    subs = f.gen.subs
    assert all(_country_ok(s) for s in subs)
    assert {s.mcc_mnc for s in subs} == {JP, KR}, "일본 가입자와 로머가 둘 다 있어야 시험이 의미 있다"
    assert not [s for s in subs if s.mcc_mnc == JP and s.mdn.startswith("010")]      # 10:42 실측: 일본인데 010
    assert not [s for s in subs if s.mcc_mnc == KR and s.mdn.startswith("09")]       # 10:42 실측: 로머인데 090


def test_reseat_numbers_are_distinct_and_imsi_follows_the_same_index_and_reverse_index_is_rebuilt():
    f = _fake(3000, KR)
    P.PemdbGen._reseat_subscribers(f, random.Random(2))
    subs = f.gen.subs
    assert len({s.mdn for s in subs}) == len(subs)
    assert all(s.imsi == I.synth_imsi(s.mcc_mnc, i) and s.mdn == I.synth_mdn(i, s.mcc_mnc) for i, s in enumerate(subs))
    assert f.gen.sub_by_imsi == {s.imsi: i for i, s in enumerate(subs)}


def test_hidden_sa_terminal_number_follows_the_source_mcc(monkeypatch):
    from datagen import generator as G
    cells = [SimpleNamespace(cell_key=f"c{i}") for i in range(5)]
    f = _fake(400, KR)
    P.PemdbGen._reseat_subscribers(f, random.Random(3))
    f.gen.cells, f.gen.cell_weights = cells, [1.0] * len(cells)

    class _Ring:
        def __init__(self, *a, **k): pass
        def pick(self, i): return i % 5
    monkeypatch.setattr(G, "CellRing", _Ring)
    # `_make_sa_hidden` 는 `dataclasses.replace` 로 본뜨므로 진짜 dataclass 가 필요하다
    f.gen.subs = [G.Subscriber(imsi=s.imsi, mdn=s.mdn, imei=s.imei, service_code="x", pay_code="p", gender="", age="",
                               vendor="V", model="M", mcc_mnc=s.mcc_mnc, exchange="", dcnr="0", home_cell=0,
                               route=-1) for s in f.gen.subs]
    out = P.PemdbGen._make_sa_hidden(f, 0.5, 7)
    hidden = [s for v in out.values() for s in v]
    assert len(hidden) == 200 and all(_country_ok(s) for s in hidden)
    assert {s.mcc_mnc for s in hidden} == {JP, KR} or {s.mcc_mnc for s in hidden} == {JP}
