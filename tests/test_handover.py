"""핸드오버 — 전환이 셀만 바꾸는 일이 아님을 고정한다.

규칙과 배경은 `src/datagen/handover.py` 머리말, 그리고 graphio
`docs/jp/XDR규격-확장항목.md` §1 이다.

여기서 지키는 것은 **일관성**이다. 전환은 셀·eNB·TAC·장비·VLAN·PLMN·
`interval_First_*`·해제 Cause 를 함께 움직이고, 한쪽만 바뀌면 에러가 아니라
**조용히 틀린 레코드**가 남는다 — "셀은 바뀌었는데 VLAN 은 옛 셀" 같은 모양.
"""

from __future__ import annotations

import collections
import random
from pathlib import Path

import pytest

from datagen import handover as HO
from datagen.cells import load_cells
from datagen.generator import Generator
from datagen.pemdb1 import Baseline, Distributions, PemdbGen, _CT_GROUP
from datagen.profile import load as load_profiles
from datagen.scenarios import Rec

HERE = Path(__file__).resolve().parent.parent
DATA = HERE / "data"
RATES = {"lte_call": 2000, "transport": 2200, "dns": 900, "http": 1500}


@pytest.fixture(scope="module")
def cells():
    return load_cells(DATA / "cells.json")


@pytest.fixture(scope="module")
def topo(cells):
    return HO.Topology(cells)


@pytest.fixture(scope="module")
def cp(cells):
    """`_coerce` 의 핸드오버 부분만 돌리는 최소 하네스.

    `PemdbGen` 은 DB 를 받으므로 통째로 세우지 않는다 — 필요한 속성 넷만
    채운 부분 객체다. 그래서 **메서드 서명이 바뀌면 여기서 걸린다.**
    """
    profiles = load_profiles(DATA / "profile.json")
    keep = {s: profiles[s] for s in ("lte_call", "transport", "dns", "http")}
    gen = Generator(cells=cells, profiles=keep, scenarios=[], rates=RATES,
                    seed=1, diurnal=True, subscriber_pool=500,
                    success_only=True, end_only=True)

    class Harness(PemdbGen):
        def __init__(self):
            self.baseline = Baseline()
            self.ho_topo = HO.Topology(cells)
            self.dist = Distributions(DATA / "pemdb1" / "_distributions.json",
                                      random.Random(7), baseline=self.baseline)
            self.gen = gen

    return Harness(), gen


def _rec(gen, cell, *, ct="3", rel="300", suc=1):
    vals = gen.templates["lte_call"].copy()
    rec = Rec(vals, gen.index["lte_call"], "lte_call")
    for k, v in (("First_eNB_ID", cell.enb_id),
                 ("First_Cell_ID", cell.cell_id),
                 ("First_eNB_VLAN_ID", cell.vlan_id),
                 ("First_eNB_PLMN", cell.plmn),
                 ("Last_eNB_ID", cell.enb_id),
                 ("Last_Cell_ID", cell.cell_id),
                 ("Last_eNB_VLAN_ID", cell.vlan_id),
                 ("Last_eNB_PLMN", cell.plmn),
                 ("interval_First_eNB_ID", cell.enb_id),
                 ("interval_First_Cell_ID", cell.cell_id),
                 ("interval_First_TAC", cell.tac),
                 ("MME_ID", cell.mme_id), ("SGW_ID", cell.sgw_id),
                 ("PGW_ID", cell.pgw_id),
                 ("call_start_time", "1760000000.0"),
                 ("call_end_time", "1760000240.0"),
                 ("UEContextRelease_Cause", rel)):
        rec.set(k, v)
    rec.set("call_type", ct)
    rec.set("success_flag", suc)
    return rec


# --------------------------------------------------------------------------
# 토폴로지
# --------------------------------------------------------------------------


def test_every_cell_can_hand_over(cells, topo):
    """**이웃이 없는 셀이 없다.**

    고정 반경으로 이웃을 찾으면 외딴 사이트가 비는데(실측: 1.3km 반경이면
    200 사이트 중 95개가 이웃 0개), 그 셀에서는 전환이 영원히 일어나지 않고
    Q-08 이 **늘 0** 이다. 0 은 "정체가 없다"로 읽힌다(불변규칙 17). 그래서
    반경이 아니라 k-최근접이다.
    """
    for c in cells:
        assert topo.intra[c.cell_key], c.cell_key
        assert topo.inter[c.cell_key], c.cell_key
        assert set(topo.kinds(c)) == set(HO.KIND_W), c.cell_key


def test_target_is_never_the_same_cell(cells, topo):
    """상대가 자기 자신이면 `None` 이다 — 전환을 **취소해야** 한다.

    옛 구현은 상대가 없으면 같은 셀을 돌려줬고, 호출부가 그것으로 `flag=4`
    를 냈다(실측 10.7%). 집계는 그것을 일어나지 않은 전환으로 센다.
    """
    rng = random.Random(3)
    for c in cells[:120]:
        for kind in HO.KIND_W:
            for _ in range(20):
                t = topo.target(c, kind, rng)
                if t is None:
                    continue
                assert t is not c
                assert t.cell_key != c.cell_key
                if kind == HO.HO_INTRA:
                    assert t.enb_id == c.enb_id, (c.cell_key, t.cell_key)
                else:
                    assert t.enb_id != c.enb_id, (c.cell_key, t.cell_key)


def test_release_cause_maps_to_every_kind():
    """해제 Cause → 종류 표가 **어떤 종류도 빠뜨리지 않는다.**

    `{c: k for k, cs in ...}` 로 역방향을 만들면 두 종류에 걸친 Cause 에서
    뒤의 것이 앞을 조용히 덮는다. `102` 가 `INTRA_ENB`·`X2` 양쪽에 있어
    실제로 그랬고, C-Plane 에서 **섹터 전환이 한 건도 나오지 않았다**.
    """
    covered = {k for ks in HO.KINDS_BY_REL.values() for k in ks}
    assert covered == set(HO.KIND_W)
    assert HO.KINDS_BY_REL["102"] == (HO.HO_INTRA, HO.HO_X2)
    # U-Plane 쪽 집합과 같아야 한다 — 어긋나면 C 와 U 가 서로 다른 호를
    # 핸드오버로 본다
    from datagen import session as S
    assert HO.HANDOVER_CAUSES == S.HANDOVER_CAUSES


# --------------------------------------------------------------------------
# C-Plane 일관성
# --------------------------------------------------------------------------


def test_cplane_handover_moves_everything_together(cp, cells):
    """셀이 바뀌면 VLAN·PLMN·TAC·`interval_First_*` 도 그 셀 것이다."""
    h, gen = cp
    by_key = {c.cell_key: c for c in cells}
    rng = random.Random(11)
    moved = 0
    for _ in range(4000):
        cell = cells[rng.randrange(len(cells))]
        rel = rng.choice(["300", "120", "102", "135", "133", "134", "0"])
        rec = _rec(gen, cell, ct=rng.choice(["3", "3", "10"]), rel=rel)
        h._baseline_handover(rec, rec.get("call_type"), cell, rel)

        key = f"{rec.get('Last_eNB_ID')}-{rec.get('Last_Cell_ID')}"
        if key == cell.cell_key:
            continue
        moved += 1
        dst = by_key.get(key)
        assert dst is not None, key          # 마스터에 없는 셀을 쓰지 않는다
        assert rec.get("Last_eNB_VLAN_ID") == dst.vlan_id
        assert rec.get("Last_eNB_PLMN") == dst.plmn

        # `interval_First_*` 는 그 Interim 구간이 시작된 셀이다 — 전환 전후
        # 둘 중 하나이고, 그 셀의 TAC·VLAN 과 묶여 있어야 한다
        iv = by_key.get(f"{rec.get('interval_First_eNB_ID')}"
                        f"-{rec.get('interval_First_Cell_ID')}")
        assert iv is not None
        assert iv.cell_key in (cell.cell_key, dst.cell_key)
        assert str(rec.get("interval_First_TAC")) == str(iv.tac)
        assert rec.get("interval_First_eNB_VLAN_ID") == iv.vlan_id
    assert moved > 500, moved


def test_cplane_core_relocation_only_on_s1(cp, cells):
    """`PathSwitch` 와 코어 재배치는 **같이 오지 않는다.**

    `PathSwitch_count > 0` 은 X2 핸드오버의 `PathSwitchRequest` 다. X2 는
    코어를 건드리지 않는다 — S1 핸드오버는 MME 가 중개하므로 그 메시지가
    없다. 둘이 같이 있으면 절차가 섞인 레코드다.
    """
    h, gen = cp
    rng = random.Random(13)
    ps = reloc = 0
    for _ in range(6000):
        cell = cells[rng.randrange(len(cells))]
        rel = rng.choice(["300", "102", "133", "134"])
        rec = _rec(gen, cell, ct="3", rel=rel)
        h._baseline_handover(rec, "3", cell, rel)
        n = int(rec.num("PathSwitch_count") or 0)
        sgw = str(rec.get("SGW_ID")) != str(cell.sgw_id)
        mme = str(rec.get("MME_ID")) != str(cell.mme_id)
        if n:
            ps += 1
            assert not sgw and not mme, (n, sgw, mme)
        if sgw or mme:
            reloc += 1
            assert rec.get("Last_eNB_ID") != cell.enb_id   # 코어 재배치는 eNB 간
        # **"MME 가 바뀌면 SGW 도 바뀐다"를 필드로 단정할 수 없다.**
        # `S1_MME` 는 둘 다 새 셀 값으로 쓰지만, 이웃이 같은 SGW 를 쓰면
        # (실측 SGW 45대 / eNB 333) 그 변화가 **관측되지 않는다.** 구현은
        # 종류로 가르고, 테스트는 관측 가능한 것만 본다
    assert ps > 100 and reloc > 100, (ps, reloc)


def test_handover_release_cause_implies_the_cell_moved(cp, cells):
    """해제 Cause 가 핸드오버면 **셀이 반드시 바뀐다.**

    해제 사유는 실측 분포에서 **독립으로** 뽑힌다. 묶지 않으면 "X2 Handover
    triggered 로 끝났는데 First == Last" 인 호가 남고, 그건 사람이 읽는
    모순이며 불변규칙 6 의 테스트 자료로도 쓸 수 없다.
    """
    h, gen = cp
    rng = random.Random(17)
    n = 0
    for rel in sorted(HO.HANDOVER_CAUSES):
        for _ in range(200):
            cell = cells[rng.randrange(len(cells))]
            rec = _rec(gen, cell, ct="3", rel=rel)
            h._baseline_handover(rec, "3", cell, rel)
            assert (f"{rec.get('Last_eNB_ID')}-{rec.get('Last_Cell_ID')}"
                    != cell.cell_key), rel
            # Cause 와 종류가 어긋나면 Cause 를 종류에 맞춘다
            got = rec.get("UEContextRelease_Cause")
            assert got in HO.HANDOVER_CAUSES, (rel, got)
            n += 1
    assert n == 200 * len(HO.HANDOVER_CAUSES)


def test_non_handover_release_cause_is_not_overwritten(cp, cells):
    """핸드오버가 아닌 해제 사유를 **덮지 않는다.**

    절단 호의 Cause 를 정상 종료로 바꾸면 절단율이 조용히 내려간다.
    """
    h, gen = cp
    rng = random.Random(19)
    for rel in ("300", "120", "121", "0"):
        for _ in range(400):
            cell = cells[rng.randrange(len(cells))]
            rec = _rec(gen, cell, ct="3", rel=rel)
            h._baseline_handover(rec, "3", cell, rel)
            assert rec.get("UEContextRelease_Cause") == rel


# --------------------------------------------------------------------------
# 직전 호
# --------------------------------------------------------------------------


def test_handover_in_call_says_where_it_came_from(cp, cells):
    """`call_type 10/11` 은 `old_call_Last_*` 로 **출발 셀**을 남긴다.

    비면 "넘어왔다"는 말만 있고 어디서 왔는지가 없다 — 셀 쌍 분석의 자료가
    U-Plane 쪽에만 남는다.
    """
    h, gen = cp
    by_key = {c.cell_key: c for c in cells}
    rng = random.Random(23)
    n = 0
    for _ in range(2000):
        cell = cells[rng.randrange(len(cells))]
        rec = _rec(gen, cell, ct="10")
        h._baseline_old_call(rec, "10", cell, {"att": 1, "suc": 1})
        src_key = f"{rec.get('old_call_Last_eNB_ID')}" \
                  f"-{rec.get('old_call_Last_Cell_ID')}"
        assert src_key != cell.cell_key              # 자기 자신에서 올 수 없다
        src = by_key.get(src_key)
        assert src is not None, src_key
        assert str(rec.get("old_call_Last_TAC")) == str(src.tac)
        assert rec.get("old_call_Last_eNB_PLMN") == src.plmn
        assert rec.get("old_call_s1ap_release_cause") in HO.HANDOVER_CAUSES
        # 정상 전환이다 — 오류 기록이 아니다(불변규칙 6·7)
        assert (rec.get("old_call_error_cause") or "0") == "0"
        # 직전 호는 이 호보다 **먼저** 끝났다
        assert (float(rec.get("old_call_end_time"))
                < float(rec.get("call_start_time")))
        n += 1
    assert n == 2000


def test_old_call_error_only_on_failed_calls(cp, cells):
    """직전 호 실패 기록은 **실패한 호에만** 남는다.

    `cell_agg` 의 `OLD_FAIL_CNT`·`OLD_SAME_IFACE` 가 `SUCCESS_FLAG != 1` 을
    조건에 달고 있다. 성공 호에 남기면 그 분자가 "재시도 성공"까지 세어
    "같은 원인의 반복"이라는 뜻이 흐려진다.

    **그리고 비어 있으면 안 된다** — 생성기가 이 필드를 한 번도 쓰지 않아
    그 두 컬럼이 구조적으로 늘 0 이었고, 0 은 "직전 호 실패가 없다"로
    읽힌다(불변규칙 17).
    """
    h, gen = cp
    rng = random.Random(29)
    fail_with = ok_with = 0
    for _ in range(6000):
        cell = cells[rng.randrange(len(cells))]
        suc = rng.random() < 0.5
        rec = _rec(gen, cell, ct="3", suc=1 if suc else 0)
        rec.set("first_error_interface_protocol", "0" if suc else "3")
        h._baseline_old_call(rec, "3", cell, {"att": 1, "suc": 1 if suc else 0})
        has = (rec.get("old_call_error_cause") or "0") != "0"
        if suc:
            ok_with += has
        else:
            fail_with += has
    assert ok_with == 0, ok_with
    assert fail_with > 100, fail_with       # 분자가 비면 안 된다


# --------------------------------------------------------------------------
# 이동 궤적 — 전환이 추첨이 아니라 **경로**를 따른다
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def routes(cells, topo):
    return HO.build_routes(topo, cells, 10, random.Random(5), length=8)


def test_route_steps_never_fall_off_the_end(routes):
    """끝에서 **사라지지 않는다** — 순환선은 돌고 왕복선은 뒤집힌다.

    사라지게 두면 이동 가입자가 시간이 갈수록 줄어, 몇 시간 뒤에는 전환이
    거의 없다. 에러가 아니라 **조용히 멈추는** 형태다.
    """
    for rt in routes:
        pos, fwd = 0, True
        seen = set()
        for _ in range(len(rt) * 4):
            seen.add(pos)
            pos, fwd = rt.step(pos, fwd)
            assert 0 <= pos < len(rt), (rt.id, pos)
        assert seen == set(range(len(rt))), (rt.id, sorted(seen))


def test_route_kind_is_stable_per_pair(routes, cells):
    """같은 셀 쌍은 **늘 같은 종류**다 — 방향이 달라도.

    추첨하면 "같은 구간인데 어떤 호는 X2, 어떤 호는 S1" 이 되어 셀 쌍 표가
    "이 쌍은 이런 전환"이라고 말할 수 없다. 상행과 하행은 같은 구간이다.
    """
    for rt in routes:
        for i in range(len(rt) - 1):
            a, b = rt.cells[i], rt.cells[i + 1]
            k = HO.kind_of(a, b)
            assert HO.kind_of(b, a) == k, (a.cell_key, b.cell_key)
            assert HO.kind_of(a, b) == k
            if a.enb_id == b.enb_id:
                assert k == HO.HO_INTRA


def test_kind_falls_back_to_a_pair_hash_without_a_ta(cells):
    """**TA 가 없으면 마스터를 읽지 않는다** — 쌍 해시로 비중만 재현한다.

    `TAC` 가 비어 있으면 장비가 eNB 해시로 배정된다(옛 경로). 그러면 인접
    eNB 가 거의 늘 다른 MME 이고, 그대로 읽었더니 경로 구간 73개 중
    **55개가 MME 재배치**(X2 는 4개)로 나왔다 — 실제 망은 MME 풀이 지리적이라
    이웃은 보통 같은 MME 를 쓴다.

    TA 가 있으면 그 전제가 성립하므로 마스터를 읽는다
    (`test_kind_reads_the_master_when_a_ta_is_present`).
    """
    # **TA 를 비운 사본으로 본다.** `data/cells.json` 은 구 시뮬레이터 TAC
    # (1000·1007…)를 들고 있어서 그냥 쓰면 마스터 경로로 간다
    import dataclasses
    flat = [dataclasses.replace(c, tac=0) for c in cells[:40]]
    inter = [(a, b) for a in flat for b in flat if a.enb_id != b.enb_id]
    got = collections.Counter(HO.kind_of(a, b) for a, b in inter)
    n = sum(got.values())
    assert HO.HO_INTRA not in got
    # 비중(`KIND_W`)을 eNB 간 셋으로 정규화한 값에 가깝다
    assert 0.55 < got[HO.HO_X2] / n < 0.80, got
    assert 0.15 < got[HO.HO_S1] / n < 0.40, got
    assert got[HO.HO_S1_MME] / n < 0.15, got
    # 마스터가 MME 가 다르다고 말하는 쌍이 대부분인데도 S1_MME 가 소수다 —
    # 그게 추론하지 않는다는 증거다
    diff_mme = sum(1 for a, b in inter if a.mme_id != b.mme_id) / n
    assert diff_mme > 0.5, diff_mme


def test_kind_reads_the_master_when_a_ta_is_present(cells):
    """**TA 가 있으면 마스터가 말하는 것이 사실이다.**

    장비 배정이 TA 에서 유도되면(도쿄 세트) 인접 eNB 가 대개 같은 MME 를
    쓴다 — 실측 86%. 그때 해시로 덮으면 지리와 맞는 사실을 지우는 셈이다.
    """
    a, b = cells[0], next(c for c in cells if c.enb_id != cells[0].enb_id)
    a.tac, b.tac = 1000, 1000
    try:
        a.mme_id, a.sgw_id = 1, 1
        b.mme_id, b.sgw_id = 1, 1
        assert HO.kind_of(a, b) == HO.HO_X2        # 둘 다 같다
        b.sgw_id = 2
        assert HO.kind_of(a, b) == HO.HO_S1        # SGW 만 다르다
        b.mme_id = 2
        assert HO.kind_of(a, b) == HO.HO_S1_MME    # MME 까지 다르다
        # 한쪽이라도 TA 가 없으면 **읽지 않는다**
        b.tac = 0
        assert HO.kind_of(a, b) in (HO.HO_X2, HO.HO_S1, HO.HO_S1_MME)
    finally:
        a.tac = b.tac = 0


def test_explicit_route_file_is_used_as_written(cells, tmp_path):
    """`routes.json` 에 적은 셀 순서를 **그대로** 쓴다.

    실제 노선을 쓰는 길이다. 적은 대로 안 쓰면 "이 구간을 정의했는데 데이터에
    없다"가 되고, 사람은 생성기가 아니라 자기 설정을 의심한다.
    """
    import json
    want = [c.cell_key for c in cells[:5]]
    f = tmp_path / "routes.json"
    f.write_text(json.dumps([{"id": "T1", "name": "손으로 적은 노선",
                              "loop": True, "cells": want,
                              "dwell_sec": [30, 40, 50, 60, 70]}]),
                 encoding="utf-8")
    got = HO.load_routes(f, cells)
    assert len(got) == 1
    assert [c.cell_key for c in got[0].cells] == want
    assert got[0].dwell_sec == (30, 40, 50, 60, 70)
    assert got[0].loop is True


def test_unknown_cell_key_in_route_file_stops(cells, tmp_path):
    """모르는 셀 키는 **멈춘다** — 조용히 건너뛰지 않는다.

    건너뛰면 노선이 짧아지고, 그 구간의 전환이 없는 원인이 설정 오타인데
    데이터 문제처럼 보인다.
    """
    import json
    f = tmp_path / "bad.json"
    f.write_text(json.dumps([{"id": "T", "cells": [cells[0].cell_key,
                                                   "NO-SUCH-CELL-9"]}]),
                 encoding="utf-8")
    with pytest.raises(ValueError, match="셀 마스터에 없는 키"):
        HO.load_routes(f, cells)


def test_route_rejects_mismatched_dwell(cells):
    """`dwell_sec` 길이가 다르면 멈춘다.

    짧으면 뒤쪽 칸이 기본값으로 떨어져 **노선 끝이 항상 빠르게 지나가는**
    편향이 생긴다 — 값은 나오고 분포만 틀린다.
    """
    with pytest.raises(ValueError, match="dwell_sec"):
        HO.Route("X", "x", tuple(cells[:3]), (10, 20))
    with pytest.raises(ValueError, match="2개 미만"):
        HO.Route("X", "x", tuple(cells[:1]), (10,))


# --------------------------------------------------------------------------
# 여정 — **특정 역에서 특정 역으로**
# --------------------------------------------------------------------------


YAMANOTE = HERE / "data" / "lines" / "yamanote.json"


@pytest.fixture(scope="module")
def ymt_cells():
    """노선 파일의 셀 키를 가진 가짜 셀 집합.

    실제 야마노테선 셀은 **JP 벌**에 있고 테스트 데이터(`cells.json`)에는
    없다. 여정 로직은 좌표를 쓰지 않으므로(칸 순서가 경로를 정한다) 키만
    맞춰 검증한다 — DB 없이 돌리기 위한 것이다.
    """
    import json
    from datagen.cells import Cell, load_cells as _load
    proto = _load(DATA / "cells.json")[0]
    spec = json.loads(YAMANOTE.read_text(encoding="utf-8"))[0]
    out = []
    for i, key in enumerate(spec["cells"]):
        enb, _, sec = key.rpartition("-")
        d = {f: getattr(proto, f) for f in proto.__dataclass_fields__}
        d.update(cell_key=key, enb_id=enb, cell_id=int(sec), enb_seq=9000 + i,
                 tac=7000 + i, mme_id=1 + i % 7, sgw_id=1 + i % 11,
                 pgw_id=1 + i % 11, latitude=35.6 + i * 0.001,
                 longitude=139.7 + i * 0.001)
        out.append(Cell(**d))
    return out


@pytest.mark.skipif(not YAMANOTE.exists(), reason="노선 파일이 없다")
def test_yamanote_file_addresses_all_30_stations(ymt_cells):
    """30역 **전부** 지정 가능하다 — 칸이 줄어도.

    칸은 셀 중복 제거로 줄어든다(실측 65칸 / 서로 다른 29칸). `station_of`
    의 역방향으로 역을 찾으면 그때 역이 통째로 빠지고, "시나가와에서 타는
    가입자가 없다"는 결과가 나오는데 원인이 노선 생성 쪽이라 **설정을 봐도
    안 보인다.** 그래서 노선 파일이 `station_pos` 색인을 따로 들고 온다.
    """
    routes = HO.load_routes(YAMANOTE, ymt_cells)
    assert {r.id for r in routes} == {"YMT-OUTER", "YMT-INNER"}
    for r in routes:
        assert len(r.station_pos) == 30, r.id
        assert all(0 <= p < len(r) for p in r.station_pos.values())
        for code in r.station_pos:
            assert r.pos_of(code) is not None
            assert code in r.stations          # 표시명이 있다
    assert routes[0].pos_of("NO-SUCH") is None


@pytest.mark.skipif(not YAMANOTE.exists(), reason="노선 파일이 없다")
def test_outer_loop_runs_in_the_published_direction(ymt_cells):
    """외선순환은 **시나가와→시부야→신주쿠→이케부쿠로→우에노→도쿄** 다.

    JY 번호가 **감소**하는 방향이고, 칸 번호는 그 순서로 **증가**해야 한다.
    뒤집히면 같은 두 역 사이인데 지나는 구간이 전혀 달라진다 — 순환선이라
    에러가 나지 않는다.
    """
    outer, inner = HO.load_routes(YAMANOTE, ymt_cells)
    seq = ["JY25", "JY20", "JY17", "JY13", "JY05", "JY01"]   # 공표 방향
    pos = [outer.pos_of(c) for c in seq]
    assert pos == sorted(pos), list(zip(seq, pos))
    # 내선은 반대다
    ipos = [inner.pos_of(c) for c in seq]
    assert ipos == sorted(ipos, reverse=True), list(zip(seq, ipos))


@pytest.mark.skipif(not YAMANOTE.exists(), reason="노선 파일이 없다")
def test_every_inter_station_leg_takes_the_same_time(ymt_cells):
    """역간 이동시간이 **모든 구간에서 같다.**

    전에는 칸을 노선 전체에서 고정거리(400m)로 뽑고 체류를 거리에 비례시켰다.
    그러면 같은 "역간 한 구간"인데 소요가 28~176초로 들쭉날쭉하고, **위치가
    분마다 찍힌다는 보장이 없다.** 지금은 역간을 등분하므로 칸당 체류가
    균일하다.

    공표 한 바퀴는 60분(역간 2분)인데 여기는 **역간 3분**이다 — 사람이 정한
    값이고(2026-10-27), 그래서 한 바퀴가 90분이다. 공표값과 다른 것은
    의도이고, 빌더의 `SEG_SEC` 하나가 그 값을 정한다.
    """
    for r in HO.load_routes(YAMANOTE, ymt_cells):
        assert len(set(r.dwell_sec)) == 1, sorted(set(r.dwell_sec))
        per = r.dwell_sec[0]
        # 칸 수가 역 수의 배수여야 등분이 성립한다
        assert len(r) % 30 == 0, len(r)
        sub = len(r) // 30
        assert per * sub == 180, (per, sub)          # 역간 3분
        assert sum(r.dwell_sec) == 30 * 180, sum(r.dwell_sec)


@pytest.mark.skipif(not YAMANOTE.exists(), reason="노선 파일이 없다")
def test_position_is_stamped_every_minute(ymt_cells):
    """칸당 체류가 **60초**다 — 분마다 위치가 한 번 찍힌다.

    `advance_routes` 는 분마다 한 번 불리고 한 번에 **한 칸만** 간다. 체류가
    분보다 길면 어떤 분에는 위치가 안 움직이고, 짧으면 중간 칸을 건너뛰어
    그 구간의 전환이 데이터에 없다.
    """
    for r in HO.load_routes(YAMANOTE, ymt_cells):
        assert r.dwell_sec[0] == 60, r.dwell_sec[0]


@pytest.mark.skipif(not YAMANOTE.exists(), reason="노선 파일이 없다")
def test_each_station_gets_its_own_slot(ymt_cells):
    """역마다 **서로 다른 칸**이다.

    고정거리로 뽑을 때는 칸이 역과 무관한 자리에 떨어져 역 둘이 한 칸을
    공유했다(시나가와·다카나와). 그러면 그 역을 구간 경계로 쓸 수 없다.
    """
    for r in HO.load_routes(YAMANOTE, ymt_cells):
        assert len(r.station_pos) == 30, r.id
        assert len(set(r.station_pos.values())) == 30, r.id


@pytest.mark.skipif(not YAMANOTE.exists(), reason="노선 파일이 없다")
def test_consecutive_slots_may_share_a_cell(ymt_cells):
    """같은 셀이 이어지는 칸이 있다 — **접지 않는다.**

    접으면 "1/3 지점마다 위치가 찍힌다"가 깨지고 칸당 체류가 다시 들쭉날쭉
    해진다. 같은 셀이 이어지면 그 구간에서 전환이 없는 것이고 그게 사실이다 —
    위치는 분마다 찍히고 **셀은 바뀔 때만 바뀐다**(실측 90칸 중 19칸).
    """
    r = HO.load_routes(YAMANOTE, ymt_cells)[0]
    keys = [c.cell_key for c in r.cells]
    dup = sum(1 for a, b in zip(keys, keys[1:]) if a == b)
    assert dup > 0, "같은 셀이 이어지는 칸이 하나도 없다 — 접고 있는 것 아닌가"
    # 그래도 **대부분은 바뀐다** — 전부 같으면 전환이 없다
    assert dup < len(keys) // 2, (dup, len(keys))


# --------------------------------------------------------------------------
# TAI List — **TA 가 바뀐다고 TAU 가 나는 것이 아니다**
# --------------------------------------------------------------------------


def _with_tacs(cells, per_ta=6):
    """TAC 를 붙인 사본. 실데이터(OpenCellID)의 모양을 흉내낸다.

    `data/cells.json` 의 TAC 는 eNB 당 하나라(구 시뮬레이터) 실제와 다르다 —
    실제 도쿄는 TA 하나가 eNB 여러 개를 덮고, 도심은 2km 안에 TAC 가 49개다.
    """
    import dataclasses
    # **지리 순으로 붙인다.** 삽입 순서로 붙이면 `cells.json` 이 전국에
    # 흩어져 있어(위도 33.2~37.8) TA 하나가 격자 한 칸을 차지하고, 목록 수가
    # TA 수와 같아져 묶기를 검증하지 못한다
    by, t = {}, 40000
    for c in sorted(cells, key=lambda c: (round(c.latitude, 2),
                                          round(c.longitude, 2), c.enb_id)):
        if c.enb_id not in by:
            if len(by) % per_ta == 0:
                t += 1
            by[c.enb_id] = t
    return [dataclasses.replace(c, tac=by[c.enb_id], ta_list=0) for c in cells]


def test_ta_list_groups_tacs_and_never_splits_one(cells):
    """**같은 TA 의 셀은 같은 목록이다.**

    TA 하나가 두 목록에 걸치면 그 TA 안에서 움직이는 것만으로 TAU 가 난다 —
    목록은 TA 의 묶음이지 셀의 묶음이 아니다.
    """
    cs = _with_tacs(cells)
    n = HO.build_ta_lists(cs)
    assert n > 0
    by_tac = collections.defaultdict(set)
    for c in cs:
        by_tac[c.tac].add(c.ta_list)
    for t, ls in by_tac.items():
        assert len(ls) == 1, (t, ls)
    assert all(c.ta_list for c in cs), "목록이 안 붙은 셀이 있다"
    # 목록은 TA 보다 **적어야** 한다 — 같으면 묶는 의미가 없다
    assert n < len(by_tac), (n, len(by_tac))


def test_ta_list_is_deterministic(cells):
    """두 번 돌려도 같은 목록이다 — 난수를 쓰지 않는다.

    목록이 실행마다 바뀌면 같은 전환이 어떤 날에는 TAU 를 내고 어떤 날에는
    안 낸다.
    """
    a, b = _with_tacs(cells), _with_tacs(cells)
    HO.build_ta_lists(a)
    HO.build_ta_lists(b)
    assert [c.ta_list for c in a] == [c.ta_list for c in b]


def test_ta_list_id_encodes_its_equipment_group(cells):
    """`id // 16` 이 **지리적으로 붙은 목록 묶음**이다.

    장비를 목록 번호에서 유도한다(`pemdb1._TA_PER_MME = 16`). Z-order 로
    정렬해 순위를 매기는 것만으로는 그 묶음이 번호와 어긋났다 — 실측
    `S1` 7.8% · `S1_MME` 16.9% 로 뒤집혔다(기대 26.7 · 6.7). 그래서 상위
    묶음을 **먼저** 세우고 그 안에서 0~15 를 준다.
    """
    cs = _with_tacs(cells)
    HO.build_ta_lists(cs)
    groups = collections.defaultdict(set)
    for c in cs:
        groups[(c.ta_list - HO.TA_LIST_BASE) // 16].add(c.ta_list)
    # 한 묶음에 16개를 넘지 않는다 — 넘으면 번호와 묶음이 어긋난 것이다
    for g, ls in groups.items():
        assert len(ls) <= 16, (g, sorted(ls))


def test_cells_without_coordinates_get_no_ta_list(cells):
    """좌표가 없으면 목록이 `0` 이다 — **지어내지 않는다.**

    위치를 모르면 목록도 모른다(불변규칙 12·17). 그 셀에서는 목록 경계가
    없는 것으로 읽혀 TAU 가 나지 않는다.
    """
    import dataclasses
    cs = _with_tacs(cells)
    # TAC 가 없는 셀은 애초에 묶기 대상이 아니다
    flat = [dataclasses.replace(c, tac=0, ta_list=0) for c in cs[:20]]
    assert HO.build_ta_lists(flat) == 0
    assert all(c.ta_list == 0 for c in flat)


# --------------------------------------------------------------------------
# 코어 장비 계층 — eNB → TAC → SGW → MME
# --------------------------------------------------------------------------


def test_master_equipment_wins_over_derivation(cells):
    """마스터가 `SGW_ID`·`MME_ID` 를 지정했으면 **그것이 이긴다.**

    산술 유도(`ta_list // N`)는 마스터가 말하지 않을 때의 대안이다. 묶는
    쪽(graphio `tools/build_tokyo_core.py`)과 읽는 쪽(`pemdb1.load_cells`)이
    **서로 다른 저장소**라 같은 코드를 쓸 수 없다 — 각자 구현하면 한쪽만
    고쳐질 때 **에러 없이** 셀과 장비의 대응이 갈린다. 그래서 결과를 컬럼에
    적고 읽는다.

    여기서는 `load_cells` 를 DB 없이 돌릴 수 없으므로 **질의가 그 컬럼을
    고르는지**와 **언패킹이 그 수를 세는지**만 고정한다. 둘이 어긋나면
    `not enough values to unpack` 으로 죽는다(실제로 겪었다).
    """
    import inspect

    from datagen import pemdb1 as P

    src = inspect.getsource(P.load_cells)
    assert "r.TAC, r.SGW_ID, r.MME_ID" in src
    assert "tac_raw, sgw_raw, mme_raw" in src
    # 마스터 값이 **나중에** 덮어야 이긴다
    i_deriv = src.index("c.mme_id = all_mme[")
    i_master = src.index("c.sgw_id = sg")
    assert i_master > i_deriv, "유도가 마스터를 덮고 있다"


def test_equipment_list_stays_in_step_with_cells(cells):
    """`equip` 과 `cells` 가 **같은 순서**로 쌓인다.

    `zip(cells, equip)` 으로 맞추므로 한쪽만 늘면 **조용히 밀린다** — 그러면
    어떤 셀이 남의 장비를 받고, 에러는 나지 않는다. 두 `append` 가 같은
    분기 안에 붙어 있어야 한다(필터 `continue` 뒤).
    """
    import inspect

    from datagen import pemdb1 as P

    src = inspect.getsource(P.load_cells)
    i_eq = src.index("equip.append(")
    i_cell = src.index("cells.append(Cell(")
    # 둘 사이에 `continue` 가 없어야 한다 — 있으면 한쪽만 건너뛴다
    assert "continue" not in src[i_eq:i_cell], src[i_eq:i_cell]
    assert i_eq < i_cell


# --------------------------------------------------------------------------
# 생성 범위 — 분당 호를 어느 지역에 흘리나
# --------------------------------------------------------------------------


def test_scope_is_a_union_not_an_intersection():
    """`gu`·`dong`·`route` 를 함께 주면 **합친다.**

    "이 구 + 이 노선"이 자연스러운 요구다. 교집합이면 노선이 그 구를 지나는
    칸만 남아 거의 빈 결과가 된다 — 실측에서 "노선 + 신주쿠구"가 **10셀**
    이었다(노선 88 · 구 1,114 인데). 질의가 먼저 좁혀 버린 탓이었다.

    고친 방식: 노선이 있으면 **질의로 좁히지 않고** 전량을 끌어와 파이썬에서
    합친다. 그래서 `_scope_sql` 이 노선이 있을 때 빈 문자열을 돌려줘야 한다.
    """
    from datagen.pemdb1 import Scope, _scope_sql

    assert _scope_sql(None) == ""
    assert _scope_sql(Scope()) == ""
    only_gu = _scope_sql(Scope(gu=[11200]))
    assert "GU_ID IN (11200)" in only_gu
    # 구 + 동은 **OR** 다 — AND 면 동이 구 안에 있으니 늘 동만 남는다
    both = _scope_sql(Scope(gu=[11200], dong=[11200011]))
    assert " OR " in both
    # **노선이 있으면 질의가 손대지 않는다**
    assert _scope_sql(Scope(gu=[11200], route="YMT-OUTER")) == ""


def test_unknown_route_in_scope_stops(tmp_path):
    """모르는 노선 id 는 **멈춘다.**

    조용히 전량으로 넘어가면 범위를 좁혔다고 믿는 채로 전 도쿄에 호가
    흩어지고, 셀당 0.12호/분이라 **탐지가 한 번도 서지 않는다**
    (불변규칙 5). 그 원인은 설정 오타인데 데이터 문제처럼 보인다.
    """
    import json

    from datagen.pemdb1 import Scope, _scope_route

    (tmp_path / "x.json").write_text(
        json.dumps([{"id": "A", "cells": ["1-1"]}]), encoding="utf-8")
    with pytest.raises(RuntimeError, match="모르는 노선"):
        _scope_route([], Scope(route="NOPE", lines_dir=str(tmp_path)))


def test_scope_rejects_unknown_keys():
    """모르는 키는 멈춘다 — `Shape`·`Baseline`·`Mobility` 와 같은 태도."""
    from datagen.pemdb1 import Scope

    assert Scope.from_cfg(None).empty()
    assert Scope.from_cfg({"gu": [11200]}).gu == [11200]
    with pytest.raises(RuntimeError, match="모르는 키"):
        Scope.from_cfg({"gugu": [1]})


def test_map_ui_has_no_external_dependency():
    """지도 화면이 **외부 URL 을 쓰지 않는다.**

    처음에 CARTO 타일 CDN + Leaflet(unpkg)으로 만들었는데 셋이 걸렸다 —

    1. **CARTO 는 API 키를 요구한다.** 키 없이 쓰면 언제 끊길지 모른다.
    2. **현장 PoC 가 폐쇄망일 수 있다.** 외부 CDN 이면 지도가 통째로 안 뜬다.
    3. **이 저장소의 다른 화면은 외부 의존이 없다.** 저 셋이 `ui.py` 의
       유일한 외부 URL 이었다 — 내가 넣은 것이다.

    그래서 graphio 가 이미 들고 있는 경계(`data/geo/jp/`)를 읽고 순수 SVG 로
    그린다. 이 테스트가 그 자리를 지킨다.
    """
    from pathlib import Path

    from ui_src import ui_source
    src = ui_source()
    bad = [ln.strip() for ln in src.splitlines()
           if "https://" in ln or "http://" in ln]
    # 주석 안의 설명은 허용한다 — 코드가 부르는 자리만 막는다
    live = [ln for ln in bad
            if not ln.lstrip().startswith("#") and "src=" in ln or "href=" in ln]
    assert not live, live
