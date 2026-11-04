"""장애 시나리오 불변 규칙 — V3 명세(§4·§10.1 A1~A6)를 코드로 고정한다.

graphio-probe-rca `docs/XDR-FAULT-SCENARIOS-V3.md` 가 정본이고, 여기서는 그
문서의 **A. XDR 생성 품질** 검사 항목 중 **환경에 의존하지 않는 것**만 본다 —

| V3 | 항목 | 여기서 |
| --- | --- | --- |
| A1 | 필드 존재 | `StrictRec` 이 모르는 필드명을 **예외로** 올린다 |
| A2 | 저장 여부 | `fields.SERIES_FIELDS` = Parquet 컬럼 집합이므로 A1 과 같은 검사다 |
| A3 | 절차 정합 | Cause·Message 를 V3 §2.3·§2.2 표와 대조 |
| A4 | 상태 정합 | 규칙 C-2 ~ C-4 (접속실패/절단 배타 · 플래그 포함관계) |
| A5 | 시각 정합 | 규칙 T-1 ~ T-4 |
| A6 | U-Plane 정합 | 규칙 U-2(단조) · U-3(음수) · U-4(재전송 ≤ 페이로드) |

**DB 를 쓰지 않는다.** `test_session_model.py` 와 같은 이유다 — 셀 마스터는
`data/cells.json` 에서 읽는다. 환경에 의존해 건너뛰어지는 테스트는 아무것도
지키지 못한다.

**A7~A10 과 B(RCA 품질)는 여기 없다.** 그것들은 1분 집계·탐지·RCA 를 돌려야
나오고, 그러면 MySQL·Parquet·graphio 가 필요하다. 통합 검증 절차는
`docs/XDR-FAULT-SCENARIOS-V3.md` §10 과 작업 보고에 있다.
"""

from __future__ import annotations

import collections
import random
from datetime import datetime
from pathlib import Path

import pytest

from datagen import scenarios as SC
from datagen.cells import load_cells
from datagen.fields import SERIES_FIELDS
from datagen.generator import Generator
from datagen.pemdb1 import (Baseline, Distributions, _CT_GROUP, _DROP_CAUSES,
                            SUPPORTED)
from datagen.profile import load as load_profiles
from datagen import control, ui

HERE = Path(__file__).resolve().parents[1]
DATA = HERE / "data"
ANCHOR = datetime(2026, 10, 22, 19, 0)
RATES = {"lte_call": 500, "transport": 500, "dns": 200, "http": 300}


# --------------------------------------------------------------------------
# 픽스처
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def gen() -> Generator:
    cells = load_cells(DATA / "cells.json")
    profiles = load_profiles(DATA / "profile.json")
    keep = {s: profiles[s] for s in ("lte_call", "transport", "dns", "http")}
    return Generator(cells=cells, profiles=keep, scenarios=[], rates=RATES,
                     seed=20261022, diurnal=False, subscriber_pool=800,
                     success_only=True, end_only=True)


class StrictRec(SC.Rec):
    """모르는 필드명에 **예외를 올리는** `Rec`.

    `Rec.set` 은 이름이 틀리면 **조용히 무시한다.** 생성 경로에서는 그것이
    맞다(상용 155컬럼과 자체 필드 목록의 교집합만 쓰는 자리가 있다). 하지만
    mutator 의 오타는 "장애를 주입했는데 아무 일도 없었다"로만 나타나고
    에러가 없다 — 그래서 테스트에서만 엄격하게 본다(V3 A1).
    """

    __slots__ = ()

    def set(self, name, value) -> None:  # type: ignore[override]
        assert name in self.index, (
            f"{self.series} 계열에 없는 필드: {name!r} — "
            f"`fields.SERIES_FIELDS[{self.series!r}]` 에 없으면 Parquet 에도 "
            f"저장되지 않는다(A1·A2)"
        )
        super().set(name, value)


def _strict(rec: SC.Rec) -> StrictRec:
    return StrictRec(rec.values, rec.index, rec.series)


# --------------------------------------------------------------------------
# V3 §2.2 · §2.3 — 쓰는 Cause·Message 전수
#
# **여기 적힌 값만 써야 한다.** 목록 밖의 값이 나오면 graphio
# `codes.is_known_cause` 가 `UNKNOWN_CAUSE_*` 로 표기하고 **우리가 만든
# 데이터가 `H-17`(미정의 코드 출현률) 자기감시 지표를 더럽힌다.**
#
# 표를 베껴 두는 것은 불변규칙 1("산식은 한 곳에")에 어긋나 보이지만 다르다 —
# 이것은 산식이 아니라 **다른 저장소의 코드 테이블에 대한 계약**이고, 그쪽을
# import 하면 테스트가 형제 디렉터리의 존재에 의존한다.
# --------------------------------------------------------------------------

ALLOWED_CAUSE = {
    # `5003`·`15001`·`15420`·`14181` — 정답지 인증 거절 15호의 구성(Experimental
    # Result-Code 는 +10000 으로 적힌다 · graphio `DIAMETER_EXPERIMENTAL_RANGE`)
    1: {900, 3002, 3004, 3005, 5003, 5012, 5030, 5420,
        14181, 15001, 15420},                              # S6a Diameter
    # `100` — PathSwitch 실패의 First(실측 R1 PSF 1호 · SC-09/J-SC-25)
    2: {100, 106, 110, 112, 121, 125, 126, 200, 201,       # S1AP
        301, 303, 304, 400, 401, 402, 403, 404, 405, 406,
        500, 501, 502, 503, 504},
    # `68` — 서비스 미지원. 실측 6호 전부 한 APN 의 서비스 호(SC-11)
    # `78` — Missing APN. ESM 27 의 실측 상류(정답지 Attach 4호 · SC-19)
    3: {68, 72, 73, 78, 89, 93, 900},                      # S11 GTPv2-C
    # `8`·`11`·`15` — S6a 거절의 하류(정답지 · callflow `downstream_map`)
    5: {8, 11, 15, 17, 22, 26},                                       # EMM
    # `33` — S11 93 의 실측 하류(정답지 Attach 3호 · 서비스 1호 · SC-19)
    6: {26, 27, 30, 33, 96, 99},                           # ESM
}

ALLOWED_MESSAGE = {
    1: {318},                   # AIR/AIA
    # `18` UEContextReleaseRequest — 절단의 First 다(실측 R1 절단 81호 전부).
    # `3` PathSwitchRequest — PathSwitch 실패의 First 다(실측 R1 PSF 1호).
    2: {3, 9, 18, 23},          # PathSwitch · InitialContextSetup · …ReleaseRequest · UEContextRelease
    3: {32, 33, 179},           # CreateSessionRequest/Response · PGW_RESTART
    5: {68, 75, 78},            # ATTACH_REJECT · TAU_REJECT · SERVICE_REJECT
    6: {195, 209},              # ...BEARER...REJECT(IN) · PDN_CONNECTIVITY_REJECT(OUT)
}

#: §2.2 방향. **MME 가 보내는 거절은 OUT, UE·노드가 보내는 거절은 IN** 이다.
#: `195` 만 IN 이고 그 자리는 SC-08(단말 기종) 하나다.
IN_MESSAGES = {(3, 33), (3, 179), (6, 195), (6, 208)}


# --------------------------------------------------------------------------
# kind 등록 — 다섯 자리가 어긋나면 조용히 빠진다
# --------------------------------------------------------------------------


def test_kind_registry_is_single_source():
    """`KIND_SPECS` 가 정본이고 나머지가 그것과 같아야 한다.

    한 곳을 빠뜨리면 **UI 목록에서 사라지거나 `add_fault` 가 거부**하는데,
    왜 그 kind 가 안 보이는지가 코드 어디에도 적혀 있지 않다.
    """
    specs = set(SC.KIND_SPECS)
    assert specs == set(control.KINDS), specs ^ set(control.KINDS)
    assert specs == set(ui.KIND_LABELS), specs ^ set(ui.KIND_LABELS)


def test_every_kind_has_a_hook():
    """kind 마다 변형 체계가 하나는 있어야 한다.

    `pgw_uplane_degradation` 은 `PLAN_MUTATORS` 로만 돌고
    `enb_s1_outage` 는 `SUPPRESSORS` 가 주력이다 — `MUTATORS` 만 보면 그 둘이
    "등록됐는데 아무 일도 안 하는" 상태와 구분되지 않는다.
    """
    hooks = set(SC.MUTATORS) | set(SC.PLAN_MUTATORS) | set(SC.SUPPRESSORS)
    assert set(SC.KIND_SPECS) == hooks, set(SC.KIND_SPECS) ^ hooks


def test_kind_specs_declare_scoring_meta():
    """채점 메타가 비면 그 시나리오는 Incident 와 짝지어지지 않는다."""
    for kind, s in SC.KIND_SPECS.items():
        if not s.get("scored", True):
            # 장애가 아닌 재현(`j-sc-24`) — 정답 원인이 `—` 라 비어 있어야 한다.
            # 채우면 `write_truth` 의 제외와 어긋나 "원인이 있는 문항"처럼 읽힌다
            assert not s["truth_code"], kind
            continue
        assert s["truth_code"], kind
        assert s["truth_category"], kind
        # `rule_pending` — 룰 번호가 아직 배정되지 않은 일본 시나리오
        # (확정대장 §2.3 "번호는 구현 시점에"). 지어낸 번호를 적지 않는다
        assert s["expect_rules"] or s.get("rule_pending"), kind
        assert s.get("scope_kind"), kind
        assert s.get("expect_kpi"), f"{kind}: expect_kpi 가 없으면 매칭이 시간만으로 돈다"
        assert s.get("grade", "TOP1") in ("TOP1", "TOP3", "LAYER", "UNDETERMINED"), kind
        for series in s.get("series", ("lte_call",)):
            assert series in SERIES_FIELDS, f"{kind}: 모르는 계열 {series}"


def test_supported_ids_cover_every_kind():
    """`SUPPORTED` 와 `build_scenarios` 가 같은 집합을 만든다."""
    rng = random.Random(1)
    cells = load_cells(DATA / "cells.json")
    scs = SC.build_scenarios(cells, 2000, rng, selected="all")
    assert {sc.id for sc in scs} == set(SUPPORTED)
    # 채점 문항이 아닌 재현(`j-sc-24`)은 배치에 없다 — 소등 eNB 설정이
    # 있어야 성립하고, 정답 파일에 들어가지 않는다
    assert {sc.kind for sc in scs} == {
        k for k, s in SC.KIND_SPECS.items() if s.get("scored", True)}


# --------------------------------------------------------------------------
# 시간 파형
# --------------------------------------------------------------------------


def _sc(kind: str, **over) -> SC.Scenario:
    s = dict(SC.KIND_SPECS[kind])
    s["kind"] = kind
    s.setdefault("target", SC.Target())
    s.setdefault("duration", 20)
    s.update(over)
    return SC.Scenario(id=f"T-{kind}", start_minute=0, **s)


def test_steps_shape_holds_each_level():
    """`STEPS` — 구간마다 적어 둔 강도가 그대로 나와야 한다.

    `partial_recovery` 의 실패율이 `I` 그대로이므로(게이트 계수 1.0) 여기가
    틀리면 "80% → 50% → 20%" 계단이 데이터에 나타나지 않는다.
    """
    sc = _sc("partial_recovery", duration=20)
    assert sc.intensity(0) == pytest.approx(0.80)
    assert sc.intensity(4) == pytest.approx(0.80)
    assert sc.intensity(5) == pytest.approx(0.50)
    assert sc.intensity(11) == pytest.approx(0.50)
    assert sc.intensity(12) == pytest.approx(0.20)
    assert sc.intensity(19) == pytest.approx(0.20)
    assert sc.intensity(20) == 0.0


def test_outage_steps_split_suppressor_and_mutator():
    """SC-17 — 전조 구간은 `I < 0.5`, 단절 구간은 `I >= 0.5` 다.

    두 체계가 같은 `I` 를 받으므로 이 경계가 어긋나면 **레코드가 없는데 전조
    신호가 심긴** 모순이 생긴다. 그리고 전조 구간이 `0.0` 이면 `covers()` 가
    거짓이 되어 시나리오가 통째로 비활성이다.
    """
    sc = _sc("enb_s1_outage", duration=20)
    assert 0.0 < sc.intensity(0) < SC.OUTAGE_GATE
    assert sc.intensity(5) >= SC.OUTAGE_GATE
    assert sc.intensity(18) >= SC.OUTAGE_GATE
    assert 0.0 < sc.intensity(19) < SC.OUTAGE_GATE
    assert sc.covers(0)


def test_flap_shape_is_a_square_wave():
    """`FLAP` — 3분 열화 / 3분 회복. 회복 구간은 **정확히 0** 이어야 한다.

    0 이 아니면 `_dns_flap` 의 `intensity <= 0.0` 조기 반환이 안 걸려 회복
    구간에도 실패가 섞이고, SC-05(지속)와 SC-20(간헐)의 구분이 사라진다.
    """
    sc = _sc("dns_flap", duration=30)
    highs = [m for m in range(30) if sc.intensity(m) > 0]
    lows = [m for m in range(30) if sc.intensity(m) == 0]
    assert highs[:3] == [0, 1, 2]
    assert lows[:3] == [3, 4, 5]
    assert len(highs) == 15 and len(lows) == 15


def test_series_offset_delays_only_that_series():
    """SC-21 — dns 가 transport 보다 2분 늦다."""
    sc = _sc("compound_causal_failure", duration=22)
    assert sc.intensity_for("transport", 0) > 0
    assert sc.intensity_for("dns", 0) == 0.0
    assert sc.intensity_for("dns", 1) == 0.0
    assert sc.intensity_for("dns", 2) > 0
    assert sc.intensity_for("dns", 2) == pytest.approx(sc.intensity_for("transport", 0))


def test_series_offset_duration_is_extended():
    """⚠️ `duration` 에 최대 오프셋을 더해야 늦은 계열이 끝에서 잘리지 않는다.

    `covers()` 는 **오프셋 없는** `intensity(minute)` 를 보므로, 더하지 않으면
    `minute = start + 20` 에서 시나리오가 통째로 비활성이 되고 dns 는 18분만
    돈다.
    """
    rng = random.Random(3)
    cells = load_cells(DATA / "cells.json")
    scs = {sc.kind: sc for sc in SC.build_scenarios(cells, 2000, rng)}
    sc = scs["compound_causal_failure"]
    off = max(sc.series_offset.values())
    last = sc.start_minute + sc.duration - 1
    assert sc.intensity_for("dns", last) > 0, "마지막 분에 dns 가 죽어 있다"
    assert off > 0


# --------------------------------------------------------------------------
# A1·A2·A3·A4·A5 — mutator 전수
# --------------------------------------------------------------------------


def _ready_lte(gen: Generator, rng: random.Random, attempt: int | None = None):
    """mutator 가 자격 조건으로 보는 플래그를 **모두 세운 뒤** 돌린다.

    실제 경로에서는 `_coerce` 가 평시 배경으로 세운다. 여기서는 DB 없이 돌리므로
    손으로 세운다 — 세우지 않으면 모든 mutator 가 `False` 로 빠져 **아무것도
    검사하지 못하고 통과한다**(가장 나쁜 테스트 실패 방식이다).

    ``attempt`` 를 주면 `attempt_flag`/`success_flag` 를 그 값으로 둔다.
    **`0` 을 섞어야 하는 이유**: `_coerce` 는 실측 분포로 `attempt_flag` 를
    뽑고(약 4% 가 0 이다) `success_flag` 를 그것에 묶는다. 전부 1 로만
    검사하면 "시도 없는 호에 절단 갈래가 `success_flag=1` 을 세우는" 버그를
    놓친다 — 실제로 놓쳤고 통합 테스트에서 `C-02 = 100.023%` 로 드러났다.
    """
    rec, cell = gen.base_lte_call(ANCHOR, ANCHOR.timestamp())
    r = _strict(rec)
    r.set("call_type", rng.choice(["1", "3", "4"]))
    att = rng.choice([1, 1, 1, 1, 0]) if attempt is None else attempt
    for f in ("attempt_flag", "success_flag"):
        r.set(f, att)
    for f in ("data_attempt_flag", "data_success_flag",
              "ims_attempt_flag", "ims_success_flag",
              "auth_attempt_flag", "auth_success_flag"):
        r.set(f, 1)
    r.set("drop_flag", 0)
    r.set("ue_dcnr", 1)
    r.set("initial_access_duration", 120_000)
    r.set("initial_core_duration", 90_000)
    for side in ("first", "last"):
        r.set(f"{side}_error_interface_protocol", 0)
        r.set(f"{side}_error_cause", 0)
        r.set(f"{side}_error_message", 0)
        r.set(f"{side}_error_time", "0.0")
    return r, cell


def _fit_target(kind: str, rec: SC.Rec, cell) -> SC.Target:
    """그 레코드가 **반드시 걸리도록** Target 을 맞춰 준다.

    대상 선별이 빗나가면 mutator 가 돌지 않고, 돌지 않은 것은 통과와
    구분되지 않는다.
    """
    t = SC.Target(cell_keys={cell.cell_key}, enb_id=cell.enb_id)
    t.sgw_id = rec.get("SGW_ID") or cell.sgw_id
    t.mme_id = rec.get("MME_ID") or cell.mme_id
    t.mme_group = {t.mme_id}
    t.apn = rec.get("APN")
    t.vendor_model = (rec.get("Vendor"), rec.get("Model"))
    t.domain = ""
    if kind == "ims_pdn":
        t.pgw_id = rec.get("ims_PGW_ID") or cell.ims_pgw_id
    else:
        t.pgw_id = rec.get("PGW_ID") or cell.pgw_id
    if kind == "mobility":
        t.cell_pairs = [(cell.cell_key, cell.cell_key)]
    return t


LTE_KINDS = [k for k, s in SC.KIND_SPECS.items()
             if "lte_call" in s.get("series", ("lte_call",)) and k in SC.MUTATORS]

#: 검사에 쓸 강도. 기본은 최대(1.0)인데 SC-17 은 **`I >= 0.5` 에서 mutator 가
#: 돌지 않는다** — 그 구간은 suppressor 의 몫이고 레코드 자체가 없다.
PROBE_INTENSITY = {"enb_s1_outage": 0.30}


@pytest.mark.parametrize("kind", LTE_KINDS)
def test_lte_mutator_respects_record_invariants(gen, kind):
    """C-Plane mutator 22종 — A1·A3·A4·A5 를 전건 확인한다.

    같은 mutator 를 **여러 번** 돌린다. 갈래가 확률로 갈리므로(SC-02 는 20/30/50)
    한 번만 돌리면 한 갈래만 보고 지나간다.
    """
    rng = random.Random(4242)
    hit = 0
    for _ in range(400):
        rec, cell = _ready_lte(gen, rng)
        sc = _sc(kind, target=_fit_target(kind, rec, cell))
        sc.active_cells_cache = {cell.cell_key}
        sc._minute = 1
        intensity = PROBE_INTENSITY.get(kind, 1.0)
        if not SC.MUTATORS[kind](rec, sc, intensity, rng, cell):
            continue
        hit += 1
        _check_flags(rec, kind)
        _check_codes(rec, kind)
        _check_times(rec, kind)
    assert hit >= 20, (
        f"{kind}: 400회 중 {hit}회만 발동했다 — 대상 선별이 빗나가면 "
        "검사가 통과와 구분되지 않는다"
    )


def _check_flags(rec: SC.Rec, kind: str) -> None:
    """규칙 C-2 · C-4 — 상태 정합(A4)."""
    att = rec.get("attempt_flag")
    suc = rec.get("success_flag")
    drop = rec.get("drop_flag")
    # ★ **시도 없는 호에 절단을 심지 않는다.** `_coerce` 가 실측 분포로
    #   `attempt_flag=0` 을 뽑는 호가 약 4% 있고, 절단 갈래가 그 호에
    #   `success_flag=1` 을 세우면 `C-02` 가 100% 를 넘는다(실측 100.023%).
    assert not (att != "1" and drop == "1"), (
        f"{kind}: 시도 없는 호(attempt={att})에 절단을 심었다"
    )
    # C-2 — 접속 실패(`0/0`)와 연결 후 절단(`1/1`)을 한 레코드에 두지 않는다
    assert not (suc == "0" and drop == "1"), (
        f"{kind}: success=0 인데 drop=1 — 접속하지 못한 호를 절단으로도 센다"
    )
    # C-4 — 포함 관계
    assert not (att == "0" and suc == "1"), f"{kind}: 시도 없이 성공"
    for key in ("data", "ims", "paging", "auth", "location", "mecheck"):
        a, s = rec.get(f"{key}_attempt_flag"), rec.get(f"{key}_success_flag")
        assert not (a != "1" and s == "1"), f"{kind}: {key} 시도 없이 성공"
    # C-3 — 절단이면 해제 Cause 가 정상 종료 밖이어야 `DROP_CNT` 가 세어진다
    if drop == "1":
        rel = int(rec.num("UEContextRelease_Cause"))
        assert rel not in (0, 102, 104, 105, 117, 118, 120, 122, 123, 124,
                           133, 134, 135, 300, 302), (
            f"{kind}: 절단 호의 해제 Cause {rel} 가 정상 종료다 — "
            "DROP_CNT 가 세어지지 않는다"
        )
    # PathSwitch — 실패가 시도를 넘으면 PSF 가 100% 를 넘는다
    ps = int(rec.num("PathSwitch_count"))
    psf = int(rec.num("PathSwitchFailure_count"))
    assert psf <= ps, f"{kind}: PathSwitchFailure {psf} > PathSwitch {ps}"


def _check_codes(rec: SC.Rec, kind: str) -> None:
    """규칙 A3 — Cause·Message 가 코드 테이블 안에 있고 방향이 맞는가."""
    for side in ("first", "last"):
        iface = int(rec.num(f"{side}_error_interface_protocol"))
        cause = int(rec.num(f"{side}_error_cause"))
        msg = int(rec.num(f"{side}_error_message"))
        if not iface:
            assert cause == 0 and msg == 0, (
                f"{kind}: {side} 구간이 0 인데 Cause/Message 가 있다"
            )
            continue
        assert iface in ALLOWED_CAUSE, f"{kind}: 모르는 구간 {iface}"
        assert cause in ALLOWED_CAUSE[iface], f"{kind}: {side} iface{iface} cause {cause}"
        if msg:
            assert msg in ALLOWED_MESSAGE[iface], f"{kind}: {side} iface{iface} msg {msg}"
    # 구간별 Cause 컬럼
    for code, prefix in SC.IFACE_PREFIX.items():
        if code not in ALLOWED_CAUSE:
            continue
        c = int(rec.num(f"{prefix}_error_Cause"))
        m = int(rec.num(f"{prefix}_error_Message"))
        if c:
            assert c in ALLOWED_CAUSE[code], f"{kind}: {prefix}_error_Cause {c}"
        if m:
            assert m in ALLOWED_MESSAGE[code], f"{kind}: {prefix}_error_Message {m}"
    # 규칙 D-2 — 프로브 Cause 900 과 Diameter Result-Code 를 한 호에 함께 적지 않는다
    aic = int(rec.num("AuthenticationInformation_Cause"))
    if aic == 900:
        assert int(rec.num("s6a_error_Cause")) == 0, (
            f"{kind}: TIMEOUT(900) 인데 s6a_error_Cause 가 있다 — "
            "D.2 · INTERFACE_TIMEOUT_EXCLUDED 위반"
        )
    # 규칙 C-1 — S6a·S13 구간 Cause 에 900 을 적지 않는다
    assert int(rec.num("s6a_error_Cause")) != 900, f"{kind}: s6a_error_Cause=900"
    assert int(rec.num("s13_error_Cause")) != 900, f"{kind}: s13_error_Cause=900"


def _check_times(rec: SC.Rec, kind: str) -> None:
    """규칙 T-1 · T-4 — 시각 정합(A5)."""
    start = rec.num("call_start_time")
    end = rec.num("call_end_time")
    first = rec.num("first_error_time")
    last = rec.num("last_error_time")
    if first:
        assert start <= first + 1e-6, f"{kind}: first_error_time < call_start_time"
    if first and last:
        assert first <= last + 1e-6, f"{kind}: last_error_time < first_error_time"
    rel_t = rec.num("UEContextRelease_Time")
    if rel_t:
        # T-4 — 해제 시각이 있으면 해제 Cause 도 있어야 한다
        assert int(rec.num("UEContextRelease_Cause")) != 0, (
            f"{kind}: UEContextRelease_Time 이 있는데 Cause 가 0 이다"
        )
        assert rel_t <= end + 1e-6 or end == 0.0
    del start, end


def test_timeout_scenarios_leave_no_downstream_nas(gen):
    """SC-03 — TIMEOUT 의 하류 NAS 는 **`ESM 30` 하나로 제한한다.**

    V3 §9.1 의 *"하류를 만들지 않는다"* 를 2026-10-22 에 완화했다(사용자
    조건부 승인). 운영 XDR 실측에서 S11 TIMEOUT **7건 전부 하류가 남았기**
    때문이다 — 분포와 근거는 `scenarios._gw_node_down` 머리말.

    **완화의 경계를 여기서 고정한다:**

      · `first_error` 는 `S11 / 900 / 32`(요청 메시지, OUT) 그대로다
      · 하류는 `ESM 30`(PDN_CONNECTIVITY_REJECT) **만** — EMM 은 만들지 않는다
      · ESM 30 을 만든 호만 `last_error = (6, 30, 209)` 다
      · 안 만든 호는 `last_error = first_error` — **두 갈래가 다 나와야 한다**

    경계를 안 지키면 "응답을 못 받았다"와 "거절 응답을 받았다"가 데이터에서
    구분되지 않는다. 그 구분이 SC-03 과 SC-01 을 가르는 축이다.
    """
    rng = random.Random(9)
    seen = with_esm = without_esm = 0
    for _ in range(300):
        rec, cell = _ready_lte(gen, rng)
        rec.set("call_type", "3")
        sc = _sc("gw_node_down", target=_fit_target("gw_node_down", rec, cell))
        sc.active_cells_cache = {cell.cell_key}
        if not SC.MUTATORS["gw_node_down"](rec, sc, 1.0, rng, cell):
            continue
        seen += 1
        esm = int(rec.num("esm_error_Cause"))
        assert esm in (0, 30), f"하류는 ESM 30 만 — 받은 값 {esm}"
        assert int(rec.num("emm_error_Cause")) == 0, "EMM 은 만들지 않는다"
        # **구간은 결국 받은 거절 응답이다**(2026-10-28 정정) — 정답지 S11
        # `900` 10호가 전부 `s11_error = (응답 Cause, msg 33)` 이다. 요청
        # `32` 는 First 가 들고 있고, "응답을 못 받았다"는 First 의 `900` 이
        # 말한다 — SC-01(First 가 `73`)과 가르는 축은 그대로다.
        assert int(rec.num("s11_error_Message")) == 33, "구간은 응답(msg 33)이다"
        assert int(rec.num("first_error_message")) == 32, "First 는 요청(msg 32)이다"
        assert int(rec.num("first_error_interface_protocol")) == 3, "구간을 적는다(C-1)"
        assert int(rec.num("first_error_cause")) == 900
        li, lc = (int(rec.num("last_error_interface_protocol")),
                  int(rec.num("last_error_cause")))
        if esm == 30:
            with_esm += 1
            assert (li, lc) == (6, 30), "ESM 30 을 만들었으면 Last 가 그것이다"
            assert int(rec.num("last_error_message")) == 209
            assert rec.num("esm_error_Time") >= rec.num("s11_error_Time"), (
                "하류는 First Error **뒤** 다"
            )
        else:
            without_esm += 1
            assert (li, lc) == (3, 900), "하류가 없으면 Last = First"
        # T-2 — **실측 범위다**(2026-10-28 정정). 전에는 "5초 대기 이상" 을
        # 봤는데 정답지 S11 `900` 에 그런 호가 없다 — core 는 Attach
        # 0.7~3.0초 · 서비스 30~45ms 이고 이 검사는 서비스 호(ct3)다.
        assert 2_000 <= rec.num("initial_core_duration") <= 150_000
        assert (rec.num("first_error_time") <= rec.num("last_error_time")
                <= rec.num("call_end_time"))
    assert seen >= 20
    assert with_esm > 0 and without_esm > 0, (
        f"두 갈래가 모두 나와야 한다 — esm {with_esm} / 없음 {without_esm}"
    )


def test_downstream_nas_relaxation_is_scoped_to_sc03(gen):
    """SC-03 의 완화가 **다른 TIMEOUT 시나리오로 번지지 않는다.**

    사용자 조건(2026-10-22): *"모든 Timeout 에 ESM 30 을 강제하는 전역 규칙은
    만들지 않음."* SC-18(`partial_recovery`)은 SC-03 과 같은
    `S11 / 900 / 32` 를 쓰지만 **하류를 만들지 않는다** — 그 분리를 고정한다.
    """
    rng = random.Random(17)
    seen = 0
    for _ in range(300):
        rec, cell = _ready_lte(gen, rng)
        rec.set("call_type", "3")
        sc = _sc("partial_recovery",
                 target=_fit_target("partial_recovery", rec, cell))
        sc.active_cells_cache = {cell.cell_key}
        if not SC.MUTATORS["partial_recovery"](rec, sc, 1.0, rng, cell):
            continue
        seen += 1
        assert int(rec.num("esm_error_Cause")) == 0, "SC-18 은 하류를 만들지 않는다"
        assert int(rec.num("emm_error_Cause")) == 0
        assert (int(rec.num("last_error_interface_protocol")),
                int(rec.num("last_error_cause"))) == (3, 900), "Last = First"
    assert seen >= 20


def test_s6a_timeout_leaves_stage_cause_empty(gen):
    """SC-14 — `s6a_error_*` 를 **비운다**(규칙 C-1 · D.2).

    `EV-03.by_stage['s6a']` 가 빈 배열인 것이 준수의 관측 증거이고, SC-15
    (3xxx 거절)와 갈리는 지점이다.
    """
    rng = random.Random(11)
    seen = 0
    for _ in range(300):
        rec, cell = _ready_lte(gen, rng)
        sc = _sc("auc_no_response", target=_fit_target("auc_no_response", rec, cell))
        sc.active_cells_cache = {cell.cell_key}
        if not SC.MUTATORS["auc_no_response"](rec, sc, 1.0, rng, cell):
            continue
        seen += 1
        assert int(rec.num("AuthenticationInformation_Cause")) == 900
        assert int(rec.num("s6a_error_Cause")) == 0
        assert int(rec.num("s6a_error_Message")) == 0
        # **First 는 하류 EMM 이 받는다**(2026-10-28 정정) — §3.2.6 이 S6a
        # TIMEOUT 을 Call Error 에서 빼고, 정답지 `auth = 900` 3호가 전부
        # `first_error = (5, 17)` 이다. 무응답은 절차 Cause(`900`)가 말한다
        assert int(rec.num("first_error_interface_protocol")) == 5
        assert int(rec.num("first_error_cause")) == 17
    assert seen >= 20


def test_s6a_route_failure_records_3xxx_only(gen):
    """SC-15 — Cause 가 **3000~3999 전용**이고 `FE_TIMEOUT` 이 0 이다."""
    rng = random.Random(13)
    seen = 0
    for _ in range(300):
        rec, cell = _ready_lte(gen, rng)
        sc = _sc("dra_route_failure",
                 target=_fit_target("dra_route_failure", rec, cell))
        sc.active_cells_cache = {cell.cell_key}
        if not SC.MUTATORS["dra_route_failure"](rec, sc, 1.0, rng, cell):
            continue
        seen += 1
        c = int(rec.num("AuthenticationInformation_Cause"))
        assert 3000 <= c < 4000, c
        assert int(rec.num("s6a_error_Cause")) == c, "거절은 구간 Cause 를 기록한다"
        assert int(rec.num("first_error_cause")) != 900
    assert seen >= 20


def test_ims_pdn_does_not_plant_first_error(gen):
    """SC-12 — `success_flag=1` 인 호에 First Error 를 심지 않는다.

    심으면 `cell_agg` 가 `FE_RECOVERED`(오류 있고 성공)로 계상해
    **`R-24`(First Error 상승 + 회복률 높음)가 유발된다.** "IMS 만 실패"는
    호 수준 오류가 아니다.
    """
    rng = random.Random(17)
    seen = 0
    for _ in range(300):
        # **연결된 호로 고정한다** — 이 테스트가 보는 것은 "성공 호의
        # `success_flag` 를 건드리지 않는다"이고, 시도 없는 호를 섞으면
        # 검사 대상이 아닌 쪽에서 실패한다
        rec, cell = _ready_lte(gen, rng, attempt=1)
        sc = _sc("ims_pdn", target=_fit_target("ims_pdn", rec, cell))
        sc.active_cells_cache = {cell.cell_key}
        if not SC.MUTATORS["ims_pdn"](rec, sc, 1.0, rng, cell):
            continue
        seen += 1
        assert rec.get("success_flag") == "1"
        assert rec.get("data_success_flag") == "1"
        assert rec.get("ims_success_flag") == "0"
        assert int(rec.num("first_error_interface_protocol")) == 0
        # Last 는 적는다 — 실측 "호 성공 · 부가 PDN 거절" 38건이 전부 그렇다
        assert int(rec.num("last_error_interface_protocol")) == 6
        assert rec.get("last_error_time") == rec.get("esm_error_Time")
        assert int(rec.num("esm_error_Cause")) == 26
    assert seen >= 20


def test_ue_model_rejects_with_in_direction(gen):
    """SC-08 — 단말이 거절하므로 `195`(IN)이고 Cause 는 프로토콜 오류다.

    `30`/`31`(망측 거절 사유)을 쓰면 방향과 의미가 어긋나고, `R-03`
    (`esm 26/30`)과 겹쳐 Top-1 이 흐려진다.
    """
    rng = random.Random(19)
    seen = 0
    for _ in range(300):
        rec, cell = _ready_lte(gen, rng)
        sc = _sc("ue_model", target=_fit_target("ue_model", rec, cell))
        sc.active_cells_cache = {cell.cell_key}
        if not SC.MUTATORS["ue_model"](rec, sc, 1.0, rng, cell):
            continue
        seen += 1
        assert rec.get("success_flag") == "0"
        if rec.get("call_type") == "1":
            assert int(rec.num("esm_error_Message")) == 195
            assert int(rec.num("esm_error_Cause")) in (96, 99)
        else:
            # 서비스 호는 단말 ESM 거절이 호 실패가 되지 않는다(실측 4호) —
            # 단말이 무선 절차를 끝내지 못한 Context Setup 실패로 둔다
            assert int(rec.num("first_error_interface_protocol")) == 2
            assert int(rec.num("first_error_cause")) == 126
    assert seen >= 20


def test_apn_failure_uses_only_apn_counters(gen):
    """SC-19 — `27`/`93` 만 쓴다. 다른 Cause 면 `R-07` 이 발동하지 않는다.

    그 둘이 APN 축에 전용 카운터(`ESM27`·`S11_93`)를 가진 유일한 Cause 다.
    """
    rng = random.Random(23)
    seen = {27: 0, 93: 0}
    for _ in range(400):
        rec, cell = _ready_lte(gen, rng)
        sc = _sc("apn_pdn_failure", target=_fit_target("apn_pdn_failure", rec, cell))
        sc.active_cells_cache = {cell.cell_key}
        if not SC.MUTATORS["apn_pdn_failure"](rec, sc, 1.0, rng, cell):
            continue
        esm, s11 = int(rec.num("esm_error_Cause")), int(rec.num("s11_error_Cause"))
        # 카운터는 **구간 Cause** 를 센다. ESM 27 은 S11 78 의 하류로만
        # 나온다(정답지 Attach 4호) — 2026-10-28 정정
        assert (esm, s11) in ((27, 78), (33, 93)), (esm, s11)
        seen[27 if esm == 27 else 93] += 1
        assert rec.get("success_flag") == "0", "R-07 의 분모가 움직여야 한다"
    assert seen[27] > 0 and seen[93] > 0, f"두 갈래가 다 나와야 한다: {seen}"


def test_nsa_pair_is_mutually_exclusive(gen):
    """SC-13 ↔ SC-13b — `mme_restrict_dcnr` 유무로 갈린다.

    섞으면 `R-33`("N-04 정상")과 `R-34`("N-04 급증")가 동시에 후보가 되어
    쌍 비교가 성립하지 않는다.
    """
    rng = random.Random(29)
    for kind, want in (("nsa_nr_leg", 0), ("nsa_policy_restrict", 1)):
        seen = 0
        for _ in range(300):
            rec, cell = _ready_lte(gen, rng, attempt=1)
            sc = _sc(kind, target=_fit_target(kind, rec, cell))
            sc.active_cells_cache = {cell.cell_key}
            if not SC.MUTATORS[kind](rec, sc, 1.0, rng, cell):
                continue
            seen += 1
            assert int(rec.num("mme_restrict_dcnr")) == want, kind
            assert rec.num("initial_nr_conn_time") == 0.0
            assert rec.get("success_flag") == "1", f"{kind}: LTE 접속은 유지된다"
        assert seen >= 20, kind


# --------------------------------------------------------------------------
# U-Plane 계열 mutator
# --------------------------------------------------------------------------


def test_dns_mutators_split_fail_and_noresp(gen):
    """SC-05 · SC-20 — 서버 오류와 무응답을 가른다. **갈림 기준은 `ReplyCode` 다.**

    graphio 가 `DNS_FAIL`/`DNS_NORESP` 를 다른 칸에 센다 — 조치가 다르기
    때문이다(서버 장애 vs 경로 단절). 한쪽으로만 만들면 그 구분이 한 번도
    검증되지 않는다.

    **무응답의 rcode 는 `900` 이다** (2026-10-22 운영 XDR 실측 교정).
    구 구현은 rcode 를 건드리지 않아 `rcode=0 & RT=0` 을 만들었는데 운영
    DNS 10만 행에 **그 모양이 0건**이다. 근거는 `scenarios._dns_fail` 머리말.

        rcode=900 & RT=0   무응답      401건
        rcode≠0   & RT>0   서버 오류    131건
        rcode=0   & RT=0                0건  ← 구 구현이 만들던 모양
    """
    rng = random.Random(31)
    cell = gen.cells[0]
    for kind in ("dns_degradation", "dns_flap"):
        kinds = {"fail": 0, "noresp": 0}
        for _ in range(400):
            rec = _strict(gen.uplane_rec("dns", ANCHOR, 1.0, cell,
                                         ANCHOR.timestamp(), ("",) * 11, "apn",
                                         "20", "sid"))
            sc = _sc(kind)
            sc.active_cells_cache = {cell.cell_key}
            if not SC.MUTATORS[kind](rec, sc, 1.0, rng, cell):
                continue
            code = rec.get("ReplyCode")
            rt = rec.num("ResponseTime")
            if code == "900":
                kinds["noresp"] += 1
                assert rt <= 0, "무응답은 응답 시각이 **없다**"
                assert int(rec.num("AnswerCount")) == 0
            elif code not in ("", "0"):
                kinds["fail"] += 1
                assert rt > 0, "서버 오류는 응답이 **왔다** — ResponseTime 을 유지한다"
                assert int(rec.num("AnswerCount")) == 0
            else:
                # **`rcode=0 & RT=0` 은 만들지 않는다** — 운영에 없는 모양이다
                assert rt > 0, "rcode 가 0 인데 응답 시각이 없다 — 운영에 없는 조합"
            if code not in ("", "0") or rt <= 0:
                retry = int(rec.num("QueryRetryCount"))
                assert retry >= 1
                assert int(rec.num("dns_retry_sequence")) == retry
        assert kinds["fail"] > 0 and kinds["noresp"] > 0, f"{kind}: {kinds}"


def test_http_mutators_keep_down_axis(gen):
    """SC-06 ↔ SC-06b — 5xx 와 up 축 지연을 **섞지 않는다**.

    섞으면 `EV-08` 의 방향 축 판별이 무너져 "외부 서버"와 "백본 지연"이
    구분되지 않고, down 을 건드리면 `R-09d`(양 축 동시)로 빠진다.
    """
    rng = random.Random(37)
    cell = gen.cells[0]
    for kind, want5xx in (("external_server", True), ("external_path_latency", False)):
        seen = 0
        for _ in range(300):
            rec = _strict(gen.uplane_rec("http", ANCHOR, 1.0, cell,
                                         ANCHOR.timestamp(), ("",) * 11, "apn",
                                         "20", "sid"))
            before = {
                f: rec.num(f) for f in
                ("Traffic_Total_Dnlink_RetransPacketCount", "DnlinkRTTTime",
                 "Traffic_Total_Dnlink_PayloadSize")
            }
            sc = _sc(kind, target=SC.Target(domain=""))
            sc.active_cells_cache = {cell.cell_key}
            if not SC.MUTATORS[kind](rec, sc, 1.0, rng, cell):
                continue
            seen += 1
            status = int(rec.num("StatusCode"))
            if want5xx:
                assert 500 <= status < 600, status
            else:
                assert status < 500, f"{kind}: 5xx 를 만들면 R-14b 와 섞인다"
                assert rec.num("ResponseTime") > rec.num("RequestTime")
            for f, v in before.items():
                assert rec.num(f) == v, f"{kind}: down 축 {f} 를 건드렸다"
        assert seen >= 20, kind


# --------------------------------------------------------------------------
# A6 — 세션 총량 (U-2 · U-3 · U-4)
# --------------------------------------------------------------------------


PLAN_KINDS = sorted(SC.PLAN_MUTATORS)


@pytest.mark.parametrize("kind", PLAN_KINDS)
def test_plan_mutator_keeps_quantity_invariants(gen, kind):
    """`PLAN_MUTATORS` — U-3(음수 금지) · U-4(재전송 ≤ 페이로드) · up 축 보존."""
    from datagen.session import CUM_PLAIN, TRAFFIC_PAIRS

    rng = random.Random(41)
    cell = gen.cells[0]
    keys = {cell.cell_key}
    if kind == "earthquake":
        # 지진은 **eNB 단위로** 멈춘다(2026-11-03 M-5). 셀 하나짜리 대상이면
        # 그 eNB 가 곧 정지 eNB 라 주변 혼잡(`_quake_ring`)이 설 셀이 없다 —
        # 살아 있는 쪽이 되도록 다른 eNB 를 대상에 더한다
        i = 0
        while SC.quake_down(_sc(kind, target=SC.Target(cell_keys=keys)), cell, 1.0):
            keys.add(f"9{i:05d}-0")
            i += 1
    hit = 0
    for _ in range(300):
        rec = gen.uplane_rec("transport", ANCHOR, 1.0, cell, ANCHOR.timestamp(),
                             ("",) * 11, "apn", "2", "sid")
        totals = {f"Traffic_Total_{n}": rec.num(f"Traffic_Total_{n}")
                  for n in TRAFFIC_PAIRS}
        totals.update({n: rec.num(n) for n in CUM_PLAIN})
        before = dict(totals)
        # `App_Group_Code` — 앱으로 고르는 장애(22번 결제)가 발동할 자리
        const = {"ClientHelloDomain": ".example.com", "APN": "apn",
                 "App_Group_Code": "PAY"}
        plan = SC.Plan(totals, const, "normal", 3)
        # SA 세션으로 준다 — `anchor_saturation` 은 SA 만 고른다(2026-11-02).
        # 다른 kind 는 망 모드를 보지 않는다
        plan.net_mode = "SA"
        sc = _sc(kind, target=SC.Target(cell_keys=set(keys),
                                        pgw_id=cell.pgw_id, domain=""))
        sc.active_cells_cache = {cell.cell_key}
        # `capacity_shift` 는 강도를 인자가 아니라 **국면**에서 읽는다 — 시작
        # 분(강도 0.1)이면 거의 발동하지 않으니 혼잡이 최고인 분을 준다
        sc._minute = SC.SHIFT_RISE - 1
        if not SC.PLAN_MUTATORS[kind](plan, sc, 1.0, rng, cell):
            continue
        hit += 1
        for k, v in totals.items():
            assert v >= 0.0, f"{kind}: {k} 가 음수다 (U-3)"
        for d in ("Uplink", "Dnlink"):
            r = totals[f"Traffic_Total_{d}_RetransPacketCount"]
            pay = totals[f"Traffic_Total_{d}_PayloadPacketCount"]
            assert r <= pay + 1e-6, f"{kind}: {d} 재전송 {r} > 페이로드 {pay} (U-4)"
        # 불변규칙 8 — up 축은 Service/Backbone 축이다. 지역 품질 판정에 쓰는
        # 시나리오가 up 을 건드리면 외부 서비스 지연을 지역 문제로 오독한다.
        if kind in ("transport_loss", "pgw_uplane_degradation",
                    "compound_causal_failure"):
            for n in TRAFFIC_PAIRS:
                if n.startswith("Uplink"):
                    assert totals[f"Traffic_Total_{n}"] == before[f"Traffic_Total_{n}"], (
                        f"{kind}: up 축 {n} 을 건드렸다"
                    )
    assert hit >= 20, f"{kind}: {hit}회만 발동했다"


def test_plan_mutator_skips_stall_sessions(gen):
    """겹침 방지 — `OUT_STALL` 세션에는 추가 배율을 걸지 않는다 (V3 §7.2).

    `classify()` 가 이미 재전송을 8~20배로 올려 두었으므로 여기서 또 곱하면
    두 번 곱해진다.
    """
    from datagen.session import CUM_PLAIN, TRAFFIC_PAIRS

    rng = random.Random(43)
    cell = gen.cells[0]
    rec = gen.uplane_rec("transport", ANCHOR, 1.0, cell, ANCHOR.timestamp(),
                         ("",) * 11, "apn", "2", "sid")
    totals = {f"Traffic_Total_{n}": rec.num(f"Traffic_Total_{n}")
              for n in TRAFFIC_PAIRS}
    totals.update({n: rec.num(n) for n in CUM_PLAIN})
    before = dict(totals)
    plan = SC.Plan(totals, {}, "stall", 3)
    sc = _sc("transport_loss", target=SC.Target(cell_keys={cell.cell_key}))
    sc.active_cells_cache = {cell.cell_key}
    assert SC.PLAN_MUTATORS["transport_loss"](plan, sc, 1.0, rng, cell) is False
    assert totals == before


def test_plan_rejects_unstored_fields(gen):
    """`Plan.set` 은 **모르는 이름에 예외를 올린다.**

    `Traffic_Delta_*` 는 Parquet 에 없으므로(V3 §4.4) 거기에 쓰면 조용히
    사라진다 — `Rec.set` 과 반대로 동작해야 하는 이유다.
    """
    plan = SC.Plan({"Traffic_Total_Dnlink_PayloadSize": 10.0}, {}, "normal", 1)
    with pytest.raises(KeyError):
        plan.set("Traffic_Delta_Dnlink_PayloadSize", 5.0)
    plan.set("Traffic_Total_Dnlink_PayloadSize", -3.0)
    assert plan.get("Traffic_Total_Dnlink_PayloadSize") == 0.0, "U-3 음수 금지"


# --------------------------------------------------------------------------
# SUPPRESSORS — SC-17 무호
# --------------------------------------------------------------------------


def test_suppressor_only_hits_target_enb(gen):
    """SC-17 — 대상 eNB 만, 그리고 `I >= 0.5` 구간에서만 막는다."""
    rng = random.Random(47)
    target = gen.cells[0]
    other = next(c for c in gen.cells if c.enb_id != target.enb_id)
    sc = _sc("enb_s1_outage", target=SC.Target(enb_id=target.enb_id))
    fn = SC.SUPPRESSORS["enb_s1_outage"]
    assert not any(fn(sc, other, 1.0, rng) for _ in range(200)), "다른 eNB 를 막았다"
    assert not any(fn(sc, target, 0.30, rng) for _ in range(200)), "전조 구간을 막았다"
    blocked = sum(1 for _ in range(400) if fn(sc, target, 1.0, rng))
    assert blocked == 400, "단절 구간(I=1.0)은 전건 막아야 한다"


def test_outage_mutator_is_silent_during_blackout(gen):
    """단절 구간에는 mutator 가 돌지 않는다 — 가짜 실패 XDR 을 만들지 않는다.

    사용자 지시 §5: "관측되지 않은 호를 가짜 실패 XDR 로 생성하지 않는다."
    """
    rng = random.Random(53)
    rec, cell = _ready_lte(gen, rng)
    sc = _sc("enb_s1_outage", target=SC.Target(enb_id=cell.enb_id))
    assert SC.MUTATORS["enb_s1_outage"](rec, sc, 1.0, rng, cell) is False
    assert rec.get("success_flag") == "1"
    # 전조 구간에서는 돈다
    hit = 0
    for _ in range(300):
        r, c = _ready_lte(gen, rng)
        s2 = _sc("enb_s1_outage", target=SC.Target(enb_id=c.enb_id))
        if SC.MUTATORS["enb_s1_outage"](r, s2, 0.30, rng, c):
            hit += 1
            assert int(r.num("s1ap_error_Cause")) == 200
    assert hit >= 20


# --------------------------------------------------------------------------
# 평시 배경 설정
# --------------------------------------------------------------------------


def test_flags_are_semantically_consistent():
    """평시 생성 경로의 플래그가 **의미상 모순되지 않는다** (2026-10-22).

    `Distributions.flags()` 가 플래그를 정하는 **유일한 자리**다
    (`_coerce` 가 그 결과를 그대로 `rec.set` 한다 — `pemdb1.py:1718~1730`).
    테스트 헬퍼 `_ready_lte` 는 자격 조건을 맞추려고 모든 플래그를 1 로
    세우므로 **실제 경로가 아니다** — 그래서 여기서 직접 돈다.

    운영 XDR 10만 행의 조합 33종을 기준으로 삼는다. `att=0` 인 조합 5종은
    전부 `data`·`ims`·`drop` 이 0 이고 `paging`·`auth` 만 선다 — 페이징은
    망이 UE 를 부르는 것이라 접속 시도와 독립이기 때문이다.
    """
    d_json = DATA / "pemdb1" / "_distributions.json"
    if not d_json.exists():
        pytest.skip("_distributions.json 없음")
    base = Baseline.from_cfg({})
    for success_only in (True, False):
        d = Distributions(d_json, random.Random(7), success_only=success_only,
                          call_type_floor=None, baseline=base)
        for _ in range(8000):
            f = d.flags(d.call_type())
            # C-4 — 성공 ≤ 시도
            assert not (f["suc"] and not f["att"]), "success=1 인데 attempt=0"
            for k in ("data", "ims", "pg"):
                assert not (f[f"{k}_suc"] and not f[f"{k}_att"]), (
                    f"{k}_success=1 인데 {k}_attempt=0"
                )
            # 시도하지 않은 호에는 Data·IMS 절차도 절단도 없다
            if not f["att"]:
                assert not f["data_att"], "attempt=0 인데 data_attempt=1"
                assert not f["ims_att"], "attempt=0 인데 ims_attempt=1"
                assert not f["drop"], "attempt=0 인데 drop=1"
            # NPR 은 페이징을 했을 때만 성립한다
            assert not (f["npr"] and not f["pg_att"]), "npr=1 인데 paging 시도 없음"


def test_baseline_rejects_typos():
    """모르는 키는 **멈춘다** — 오타가 "기본값으로 돌았다"로 끝나지 않게."""
    with pytest.raises(RuntimeError, match="모르는 키"):
        Baseline.from_cfg({"auth_sucess": 0.99})
    with pytest.raises(RuntimeError, match="모르는 키"):
        Baseline.from_cfg({"auth_attempt": {"atach": 1.0}})


def test_baseline_requires_mix_to_sum_to_one():
    """`drop_cause_mix` 의 합이 1 이 아니면 그룹 비율이 적어 둔 값과 달라진다."""
    with pytest.raises(RuntimeError, match="합이"):
        Baseline.from_cfg({"drop_cause_mix": {"radio": 0.9}})


def test_baseline_merges_partial_dict():
    """dict 는 **덮지 않고 합친다** — 한 그룹만 적어도 나머지가 살아 있어야 한다."""
    b = Baseline.from_cfg({"auth_attempt": {"service": 0.05}})
    assert b.auth_attempt == {"attach": 1.00, "tau": 0.35, "service": 0.05}


def test_drop_cause_groups_match_aggregation_buckets():
    """평시 절단 Cause 가 **집계 칸과 1:1 로 맞는다** (2026-10-22 교정).

    구 테스트는 *"절단 Cause 가 정상 종료 안에 있으면 `DROP_CNT` 가 세어지지
    않는다"* 를 고정했는데 **그 전제가 틀렸다** — `cell_agg.py:491` 의
    `DROP_CNT` 는 `DROP_FLAG` 만 보고 Cause 를 보지 않는다. 정상 종료는
    `DROP_NORMAL`(:536)이 따로 센다.

    그래서 고정하는 것이 바뀐다 — **`normal` 그룹만 정상 종료 안이고
    나머지는 밖이다.** 섞이면 `DROP_NORMAL` 과 `DROP_RADIO` 등이 같은 호를
    양쪽에서 세거나(이중 계상) 어느 쪽에도 안 들어간다.
    """
    from datagen.pemdb1 import NORMAL_RELEASE

    normal = {int(c) for c in NORMAL_RELEASE}
    assert "normal" in _DROP_CAUSES, (
        "`normal` 그룹이 없으면 `DROP_NORMAL` 이 항상 0 이다 — 운영 실측은 45.9%"
    )
    for c in _DROP_CAUSES["normal"]:
        assert c in normal, f"normal 그룹의 {c} 가 정상 종료가 아니다 (불변규칙 6)"
    for group, causes in _DROP_CAUSES.items():
        if group == "normal":
            continue
        for c in causes:
            assert c not in normal, f"{group}: {c} 가 정상 종료다 (불변규칙 6)"


def test_drop_cause_mix_covers_every_group():
    """`drop_cause_mix` 의 키가 `_DROP_CAUSES` 와 **같아야 한다.**

    한쪽에만 있으면 `drop_cause()` 가 `KeyError` 로 죽거나(mix 에만 있는 키)
    그 그룹이 **한 번도 안 뽑힌다**(`_DROP_CAUSES` 에만 있는 키) — 후자는
    에러 없이 집계 칸 하나가 상시 0 이 된다.
    """
    b = Baseline.from_cfg({})
    assert set(b.drop_cause_mix) == set(_DROP_CAUSES), (
        f"mix {sorted(b.drop_cause_mix)} vs 그룹 {sorted(_DROP_CAUSES)}"
    )


def test_ct_group_covers_spec_call_types():
    """`_CT_GROUP` 의 키가 **규격 체계 `1`~`11`** 이어야 한다.

    `CD_CALL_TYPE` 2자리 체계(`"00"`~`"60"`)로 적으면 레코드의 `call_type` 과
    한 개도 맞지 않아 **모든 호가 `"service"` 그룹으로 떨어진다** — Attach 의
    인증 시도 100% 가 2% 가 되고 `P-01` 의 분모가 1/50 로 준다.
    """
    assert set(_CT_GROUP) == {str(i) for i in range(1, 12)}
    assert _CT_GROUP["1"] == "attach" and _CT_GROUP["3"] == "service"


def test_ct_session_weight_keys_match_record_values():
    """`session.CT_SESSION_W` 도 규격 체계여야 한다 (§0.1 의 후속).

    키가 안 맞으면 `_ct_weight` 가 전부 기본 1.0 을 돌려주고 **Paging·TAU 호도
    데이터 세션을 연다** — 에러는 나지 않고 U-Plane 분포만 틀린다.
    """
    from datagen.session import CT_SESSION_W

    assert set(CT_SESSION_W) == {str(i) for i in range(1, 12)}
    assert CT_SESSION_W["5"] == 0.0 and CT_SESSION_W["6"] == 0.0   # TAU · Paging
    assert CT_SESSION_W["3"] == 1.0                                # Service MO


def test_call_type_index_key_is_lowercase():
    """🆕 §0.1 회귀 가드 — `records()` 가 보는 키는 `call_type` 이다.

    `CALL_TYPE` 은 **Parquet 컬럼명**이고 필드 목록에는 없다. 전에
    `idx.get("CALL_TYPE", -1)` 로 적혀 있어서 `Call.call_type` 이 항상 `""`
    였고, `CT_SESSION_W` 가 **한 번도 적용되지 않았다.**
    """
    f = SERIES_FIELDS["lte_call"]
    assert "call_type" in f
    assert "CALL_TYPE" not in f


def test_fault_cfg_rejects_typos():
    with pytest.raises(RuntimeError, match="모르는 키"):
        SC.FaultCfg.from_cfg({"timeout_wat": {"s6a": 1.0}})
    with pytest.raises(RuntimeError, match="모르는 키"):
        SC.FaultCfg.from_cfg({"timeout_wait": {"s1u": 1.0}})
    f = SC.FaultCfg.from_cfg({"timeout_wait": {"s6a": 2.5}})
    assert f.timeout_wait_s6a == 2.5 and f.timeout_wait_s11 == 5.0


# --------------------------------------------------------------------------
# ground truth
# --------------------------------------------------------------------------


def test_truth_never_names_a_device_for_s6a():
    """SC-14·15·15b 의 `expect_node` 는 **`null`** 이다 (V3 §9.2).

    규격 부록 A-31 이 `S6a_Equip_Type` 의 `6:HSS`·`7:DRA` 를 삭제했고 상용
    실측이 전부 `'0'` 이다 — 장비 구분이 원천적으로 불가능하다. 정답에 장비를
    적으면 RCA 가 **근거 없이 찍는 쪽**으로 채점이 기울어진다.
    """
    rng = random.Random(59)
    cells = load_cells(DATA / "cells.json")
    scs = {sc.id: sc for sc in SC.build_scenarios(cells, 2000, rng)}
    for sid in ("SC-14", "SC-15", "SC-15b"):
        truth = scs[sid].as_truth()
        assert truth["expect_node"] is None, sid
        assert truth["mme_group"], f"{sid}: 대상 MME 집합이 비었다"


def test_s6a_pair_targets_differ():
    """SC-14 ↔ SC-15 ↔ SC-15b — 세 MME 집합이 서로 달라야 한다.

    겹치면 "무응답 vs 3xxx 거절" 쌍 비교가 대상 차이로 오염되고, SC-15b 는
    "대상만 다른 같은 장애"라는 전제가 사라진다.
    """
    rng = random.Random(61)
    cells = load_cells(DATA / "cells.json")
    scs = {sc.id: sc for sc in SC.build_scenarios(cells, 2000, rng)}
    g = {sid: scs[sid].target.mme_group for sid in ("SC-14", "SC-15", "SC-15b")}
    assert not (g["SC-14"] & g["SC-15"]), g
    assert g["SC-14"] != g["SC-15b"], g


def test_truth_records_grade_and_waveform():
    """채점이 읽는 확장 필드가 정답에 실려야 한다."""
    rng = random.Random(67)
    cells = load_cells(DATA / "cells.json")
    scs = {sc.id: sc for sc in SC.build_scenarios(cells, 2000, rng)}
    assert scs["SC-15"].as_truth()["grade"] == "LAYER"
    assert scs["SC-15b"].as_truth()["expect_undetermined"] is True
    assert scs["SC-11"].as_truth()["grade"] == "TOP3"
    assert scs["SC-18"].as_truth()["steps"] == [[5, 0.8], [12, 0.5], [20, 0.2]]
    assert scs["SC-21"].as_truth()["series_offset"] == {"dns": 2}


def test_truth_has_no_ground_truth_in_xdr_fields():
    """정답은 **어떤 XDR 필드에도 들어가지 않는다** (V3 §4.5).

    `truth_code` 가 레코드에 실리면 RCA 가 정답을 보고 맞히게 된다.
    """
    codes = {s["truth_code"] for s in SC.KIND_SPECS.values()}
    for series, fields in SERIES_FIELDS.items():
        assert not (codes & set(fields)), series


# --------------------------------------------------------------------------
# A6 — 세션 엔진 전 경로 (U-2 단조성)
# --------------------------------------------------------------------------


def test_session_totals_stay_monotonic_under_fault(gen):
    """장애 훅이 걸린 세션의 `Traffic_Total_*` 이 **역전하지 않는다** (U-2).

    이것이 규칙 U-1("양 필드는 `_plan` 의 세션 총량에 건다")의 **이유**다.
    분 레코드에서 직접 고치면 `_emit` 의 `now = (i+1)/span` 쪼개기와 어긋나
    같은 세션의 누적이 분 경계에서 거꾸로 가고, 그러면 `U-12` 가 음수가 된다 —
    에러는 나지 않는다.

    `mutate` 대신 `plan_mutate` 를 거는 전 경로를 돌린다 — `minute()` →
    `_open()` → `_plan()` → `_emit()` 배선이 실제로 이어져 있는지가 검증 대상
    이고, mutator 단위 테스트로는 알 수 없다.
    """
    from datetime import timedelta

    from datagen import session as S
    from datagen.generator import USER_FIELDS

    rng = random.Random(71)
    eng = S.SessionEngine(gen, RATES, S.Shape(), seed=71)
    sc = _sc("transport_loss",
             target=SC.Target(cell_keys={c.cell_key for c in gen.cells}))
    sc.active_cells_cache = sc.target.cell_keys

    def plan_mutate(cell, plan):
        SC.PLAN_MUTATORS["transport_loss"](plan, sc, 1.0, rng, cell)

    idx = gen.index["lte_call"]
    i_user = tuple(idx[f] for f in USER_FIELDS)
    i_apn, i_end = idx["APN"], idx["call_end_time"]
    i_rel, i_ct = idx["UEContextRelease_Cause"], idx["call_type"]
    tr = gen.index["transport"]
    i_sid, i_flag = tr["SessionID"], tr["OngoingFlag"]
    i_rdn = tr["Traffic_Total_Dnlink_RetransPacketCount"]
    i_pdn = tr["Traffic_Total_Dnlink_PayloadPacketCount"]
    i_rup = tr["Traffic_Total_Uplink_RetransPacketCount"]
    i_pup = tr["Traffic_Total_Uplink_PayloadPacketCount"]

    seen: dict[str, float] = {}
    ended: list[float] = []
    up_rates: list[float] = []
    for m in range(6):
        ts = ANCHOR + timedelta(minutes=m)
        slot = ts.timestamp()
        calls = []
        for _ in range(300):
            rec, cell = gen.base_lte_call(ts, slot)
            outcome = eng.classify(rec)
            if outcome is None:
                continue
            v = rec.values
            calls.append(S.Call(
                cell=cell, user=tuple(v[i] for i in i_user), apn=v[i_apn],
                t=float(v[i_end] or slot), outcome=outcome, rel=v[i_rel],
                call_type=v[i_ct]))
        got = eng.minute(ts, calls, None, plan_mutate)
        for row in got["transport"]:
            sid = row[i_sid]
            tot = float(row[i_rdn] or 0)
            assert tot >= seen.get(sid, -1.0), f"세션 {sid} 의 누적이 역전했다 (U-2)"
            seen[sid] = tot
            pay = float(row[i_pdn] or 0)
            assert tot <= pay, f"재전송 {tot} > 페이로드 {pay} (U-4)"
            assert tot >= 0 and pay >= 0, "음수 (U-3)"
            if row[i_flag] in S.COUNTED:
                ended.append(tot / max(1.0, pay))
                up_rates.append(float(row[i_rup] or 0) / max(1.0, float(row[i_pup] or 0)))

    assert len(seen) > 500, f"세션이 너무 적다: {len(seen)}"
    assert ended, "종료 레코드가 없다 — 집계가 세는 것은 그것뿐이다"
    mean_dn = sum(ended) / len(ended)
    mean_up = sum(up_rates) / len(up_rates)
    # `U-12` 임계는 `>3%/3분` Major 다. 평시 0.5% 에서 그 위로 올라가야 Incident
    # 가 생기고, 30% 를 넘으면 U-4 여유가 사라진다
    assert 0.03 <= mean_dn <= 0.30, f"down 재전송률 {mean_dn:.3f} — 임계를 못 넘거나 과하다"
    # 불변규칙 8 — up 은 Service/Backbone 축이다. 함께 오르면 `EV-08` 의 방향
    # 축 판별이 "공통 구간 기인"으로 떨어져 `R-09d` 가 발동한다
    assert mean_up <= 0.01, f"up 재전송률 {mean_up:.3f} — up 축을 건드렸다"


# --------------------------------------------------------------------------
# 평시 U-Plane — 신호가 될 수 있는 값이어야 한다
# --------------------------------------------------------------------------


def test_baseline_dns_has_no_retry(gen):
    """평시 DNS 질의에 **재시도가 남지 않는다** (2026-10-22 회귀 가드).

    실 샘플의 `QueryRetryCount` 는 **99.3% 가 `'1'`** 이다. 그대로 뽑으면
    평시 `U-33`(DNS 재시도율)이 99.6% 가 되고 —

    * 그 지표가 **장애를 가리킬 수 없다.** 평시와 장애가 같은 값이면 신호가
      아니다(불변규칙 17 의 취지).
    * "rcode 0 · 답 있음 · 그런데 1회 재시도"는 **레코드 내부 모순**이다.
      같은 블록이 `ReplyCode` 를 성공으로 누르고 `AnswerCount` 를 맞춰 두는데
      재시도만 빠져 있었다.
    * SC-05·SC-20 의 통과 조건 `DNS_RETRY/DNS_Q ≥ 0.3` 이 평시에 이미
      충족되어 **주입 검증이 성립하지 않는다.**

    `success_only=False`(실측 재현 모드)는 분포를 그대로 쓴다 — 그 모드는
    "평소 전부 녹색"이 아니다.
    """
    cell = gen.cells[0]
    retried = 0
    for k in range(400):
        rec = gen.uplane_rec("dns", ANCHOR, 1.0, cell, ANCHOR.timestamp() + k,
                             ("",) * 11, "apn", "20", f"s{k}")
        assert rec.get("ReplyCode") == "0", "평시 rcode 는 0 이다"
        # **빈 값도 안 된다.** `DNS_OK` 는 `REPLY_CODE=0 AND ANSWER_COUNT>0`
        # 이라 답이 비면 성공으로 세어지지 않고, 평시 `U-29` 가 99.6% 에서
        # 멈춘다. 실 샘플에서 빈 `AnswerCount` 는 빈 `ReplyCode` 와 짝인데
        # `success_only` 가 rcode 만 눌러 그 짝이 깨져 있었다.
        assert rec.get("AnswerCount").strip().isdigit(), "답 개수가 비었다"
        assert int(rec.num("AnswerCount")) >= 1, "성공 응답은 답을 갖는다"
        if int(rec.num("QueryRetryCount")) > 0:
            retried += 1
        assert rec.num("dns_retry_interval") == 0.0
        assert int(rec.num("dns_retry_sequence")) == 0
    assert retried == 0, f"평시 400건 중 {retried}건에 재시도가 남았다"


def test_fault_dns_does_set_retry(gen):
    """반대로 **장애 질의에는 재시도가 남아야 한다** — 음성 짝이다."""
    rng = random.Random(97)
    cell = gen.cells[0]
    hit = retried = 0
    for k in range(400):
        rec = gen.uplane_rec("dns", ANCHOR, 1.0, cell, ANCHOR.timestamp() + k,
                             ("",) * 11, "apn", "20", f"s{k}")
        sc = _sc("dns_degradation")
        sc.active_cells_cache = {cell.cell_key}
        if not SC.MUTATORS["dns_degradation"](rec, sc, 1.0, rng, cell):
            continue
        hit += 1
        if int(rec.num("QueryRetryCount")) > 0:
            retried += 1
    assert hit >= 20
    assert retried == hit, f"{hit}건 중 {retried}건만 재시도가 섰다"


def test_ramp_rise_follows_ramp_in_not_duration():
    """`RAMP` — 상승 시간은 `ramp_in` 이 정한다. **`duration` 과 무관하다.**

    2026-10-27 교정 전에는 `duration * 0.6` 이었다. 30분짜리를 주입하면
    **18분 동안 거의 무증상**이라, 장애를 넣고도 화면에 아무것도 안 나왔다
    (실측: 10분 경과 시점 누적 인증 실패 0.7%). 지속시간과 악화속도는
    다른 축이다 — 장비가 죽는 사건은 오래 간다고 천천히 나빠지지 않는다.

    `ramp_in` 필드는 그때도 있었고 STEP 계열만 쓰고 있었다.
    """
    for dur in (10, 20, 30):
        sc = _sc("s6a_undetermined", duration=dur)
        assert sc.ramp_in == 3, "RAMP 기본 상승은 3분이다"
        # 길이가 달라도 같은 자리에서 피크에 닿는다
        assert sc.intensity(2) == pytest.approx(sc.peak), f"duration={dur}"
        assert sc.intensity(0) < sc.intensity(1) < sc.intensity(2)


def test_ramp_reaches_peak_even_when_short():
    """짧은 장애도 피크에 닿아야 한다 — `ramp_in` 이 `duration` 을 넘지 않는다.

    2026-11-03 부터 회복도 `ramp_out`(기본 = `ramp_in`)을 쓰므로, 둘의 합이
    `duration-1` 을 넘으면 **같은 비율로 함께 줄인다** — 3+3 → 2+2.
    """
    sc = _sc("s6a_undetermined", duration=5)
    assert [sc.intensity(m) for m in range(5)] == [
        pytest.approx(1 / 2), pytest.approx(1.0), pytest.approx(1.0),
        pytest.approx(2 / 3), pytest.approx(1 / 3),
    ]


def test_ramp_recovery_stays_short_regardless_of_length():
    """회복은 길이와 무관하게 `ramp_out` 분이다 (2026-11-03 · 기본 = `ramp_in`)."""
    for dur in (10, 30):
        sc = _sc("s6a_undetermined", duration=dur)
        assert sc.intensity(dur - 1) < sc.intensity(dur - 2) < sc.peak
        assert sc.intensity(dur) == 0.0


# --------------------------------------------------------------------------
# `j-sc-16` 「アンテナは立つが遅い」 — anchor_saturation
# --------------------------------------------------------------------------


def _anchor_plan(payload=1_000_000.0, thr=2_000_000.0, retrans=50.0,
                 paypkt=10_000.0, rtt=30_000.0):
    """`anchor_saturation` 에 넘길 세션 총량."""
    p = SC.Plan(
        {
            "Traffic_Total_Dnlink_PayloadSize": payload,
            "Traffic_Total_Dnlink_ThroughputTime": thr,
            "Traffic_Total_Dnlink_RetransPacketCount": retrans,
            "Traffic_Total_Dnlink_PayloadPacketCount": paypkt,
            "Traffic_Total_Uplink_PayloadSize": payload / 4,
            "Traffic_Total_Uplink_ThroughputTime": thr / 4,
            "Traffic_Total_Uplink_RetransPacketCount": retrans,
            "Traffic_Total_Uplink_PayloadPacketCount": paypkt / 4,
            "DnlinkRTTTime": rtt,
            "UplinkRTTTime": rtt,
        },
        {}, "normal", 3,
    )
    # SA 세션이다 — 이 mutator 는 SA 만 고른다(2026-11-02 · 테스트
    # `test_net_mode.py::test_anchor_saturation_slows_only_sa_sessions`)
    p.net_mode = "SA"
    return p


class _Cell:
    cell_key = "1234-5"


class _Sc:
    active_cells_cache = {"1234-5"}


def test_anchor_saturation_drops_throughput_only():
    """**처리율만 떨어뜨린다.**

    `U-15 = PayloadSize / ThroughputTime` 이므로 분자만 줄인다. 분모를 함께
    줄이면 비율이 그대로라 `g-sc-16` 의 docstring 이 적은 함정에 빠진다.
    """
    fn = SC.PLAN_MUTATORS["anchor_saturation"]
    plan = _anchor_plan()
    before = (plan.get("Traffic_Total_Dnlink_PayloadSize")
              / plan.get("Traffic_Total_Dnlink_ThroughputTime"))
    assert fn(plan, _Sc(), 1.0, random.Random(0), _Cell())
    after = (plan.get("Traffic_Total_Dnlink_PayloadSize")
             / plan.get("Traffic_Total_Dnlink_ThroughputTime"))
    assert after < before * 0.7, f"{before} → {after}"
    assert plan.get("Traffic_Total_Dnlink_ThroughputTime") == 2_000_000.0


def test_anchor_saturation_leaves_the_signals_r39_requires_normal():
    """**`R-39` 가 "정상" 을 요구하는 것들을 건드리지 않는다.**

    그 룰은 재전송(`U-12`)·RTT(`U-09`)·up 축이 **정상일 때만** 발동한다.
    하나라도 올리면 `R-09`(`TRANSPORT_LOSS`)와 경쟁하고, 경쟁에서 지든
    이기든 §9.4 가 경계한 "같은 증거에서 두 원인" 이 된다.
    """
    fn = SC.PLAN_MUTATORS["anchor_saturation"]
    plan = _anchor_plan()
    keep = {k: plan.get(k) for k in plan.totals
            if "Uplink" in k or "Retrans" in k or k.endswith("RTTTime")}
    fn(plan, _Sc(), 1.0, random.Random(0), _Cell())
    for k, v in keep.items():
        assert plan.get(k) == v, f"{k} 가 움직였다: {v} → {plan.get(k)}"


def test_anchor_saturation_never_touches_the_nr_leg():
    """**`initial_nr_conn_time` 을 건드리지 않는다** — `R-33` 과의 배타성.

    그 값이 `0` 이 되면 `N-02` 가 떨어져 `R-33` 이 깨어난다. 그리고 SA
    관점에서 `= 0` 은 **다른 시나리오**(`j-sc-17` 폴백)의 것이다
    (확정대장 §9.8.1).

    `plan_mutator` 는 C-Plane 필드에 손이 닿지 않지만, 누가 `MUTATORS`
    쪽에 짝을 추가하면 조용히 깨진다 — 소스에서 직접 막는다.
    """
    import inspect

    # docstring 안에서는 그 이름을 **설명하므로** 본문만 본다.
    body = inspect.getsource(SC._anchor_saturation).split('"""')[2]
    assert "nr_conn" not in body, body
    assert "anchor_saturation" not in SC.MUTATORS, (
        "C-Plane mutator 가 생겼다 — NR 레그를 건드리는지 다시 본다"
    )


def test_anchor_saturation_skips_stalled_sessions():
    """`OUT_STALL` 은 제외한다 (V3 §7.2).

    그 세션은 이미 재전송이 8~20배라 `U-12` 가 움직이고, 그러면 `R-39` 의
    "재전송 정상" 조건에 걸려 **자기 룰이 안 붙는다.**
    """
    fn = SC.PLAN_MUTATORS["anchor_saturation"]
    plan = SC.Plan(_anchor_plan().totals, {}, "stall", 3)
    assert not fn(plan, _Sc(), 1.0, random.Random(0), _Cell())


def test_anchor_saturation_uses_its_own_cells():
    """**`g-sc-13` 과 셀을 나눈다.**

    같은 셀에 둘이 걸리면 같은 분의 `N-02` 가 섞여 `R-39` 의 "N-02 정상"
    조건이 거짓이 된다 — 두 시나리오가 서로의 발동을 막는다.
    """
    rng = random.Random(1)
    cells = load_cells(DATA / "cells.json")
    scs = {sc.kind: sc for sc in SC.build_scenarios(cells, 2000, rng,
                                                    selected="all")}
    anchor = scs["anchor_saturation"].target.cell_keys
    nsa = scs["nsa_nr_leg"].target.cell_keys
    assert anchor and nsa
    assert not (anchor & nsa), f"셀 {len(anchor & nsa)}개가 겹친다"


# --------------------------------------------------------------------------
# 셀 전환 — `PathSwitch` 는 전환의 **결과**다
# --------------------------------------------------------------------------


def test_pathswitch_never_appears_without_a_cell_change():
    """**`PathSwitch` 가 있으면 셀이 바뀌어 있어야 한다.**

    `PathSwitchRequest` 는 X2 핸드오버에서 타겟 eNB 가 MME 로 보내는
    메시지다 — 전환이 일어나야 생긴다. 그런데 구 구현은 `PathSwitch` 를
    먼저 적고 **그 뒤에** 전환 상대를 찾았고, 못 찾으면 그대로 돌아갔다.
    "셀은 그대로인데 X2 가 있는" 레코드가 남는다.

    집계가 그것을 잡아냈다 (2026-10-28 · `PM_COR_LTE_CALL_1M` 재집계) —

        HO_CNT 506 · HO_X2_CNT 741 · HO_INTER_CNT 0
        → X2 가 전환보다 **많다**(146%) · 파생 `S1 계열` 이 **음수**

    `scenarios` 쪽 mutator 가 아니라 `pemdb1._baseline_handover` 의 일이라
    소스를 직접 본다 — `PemdbGen` 은 DB 가 있어야 뜬다.
    """
    import inspect

    from datagen import pemdb1

    src = inspect.getsource(pemdb1.PemdbGen._baseline_handover)
    i_target = src.index("ho_topo.target(")
    i_ps = src.index('rec.set("PathSwitch_count"')
    assert i_target < i_ps, (
        "`PathSwitch_count` 를 전환 상대(`target`)보다 먼저 적는다 — "
        "상대가 없으면 전환이 취소되는데 메시지만 남는다")


def test_handover_kind_is_drawn_not_fixed_to_x2():
    """전환 종류를 **`KIND_W` 로 뽑는다** — X2 로 고정하지 않는다.

    `PathSwitch > 0 ⟹ X2` 는 맞지만 **역이 아니다.** 구 구현은
    `pick_kind(only=(HO_X2,))` 로 고정해서 `S1`·`S1_MME` 가 구조적으로
    나올 수 없었고, 그러면 **MME·SGW 재배치가 영원히 일어나지 않아**
    `s10_error_*` 구간이 데이터에 없다(`handover` 머리말이 고치려던 문제).

        실측 141,200호  셀 변경 9,465 · eNB 간 9,456(99.9%)
                        섹터 간 9건(0.01%)   `KIND_W` 선언은 55%
    """
    import inspect

    from datagen import handover as HO
    from datagen import pemdb1

    src = inspect.getsource(pemdb1.PemdbGen._baseline_handover)
    assert "only=(HO.HO_X2,)" not in src, "종류가 X2 로 고정돼 있다"

    # 비중이 실제로 네 종류를 다 낸다.
    rng = random.Random(3)
    cells = load_cells(DATA / "cells.json")
    topo = HO.Topology(cells)
    got = collections.Counter()
    for _ in range(20000):
        k = topo.pick_kind(cells[rng.randrange(len(cells))], rng)
        if k:
            got[k] += 1
    for kind, want in HO.KIND_W.items():
        share = got[kind] / sum(got.values())
        assert abs(share - want) < 0.05, f"{kind} {share:.3f} vs 선언 {want}"
