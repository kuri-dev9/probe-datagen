"""U-Plane 세션 모델 불변 규칙 — 깨면 조용히 틀린 값이 나온다.

구 `test_backfill_fault.py` 를 대체한다(2026-10-14). 그 파일은
`datagen_fault`(→`CM_DATAGEN_FAULT` 개명) · `cli._backfill`(→ 차단) ·
`cause_min`(구 창고) 를 쓰는 **세 세대 전** 경로를 검증하고 있었고, pytest 가
수집조차 못 했다(`main()` 스크립트였다). 같은 자리에 세션 모델 검증을 둔다 —
`docs/UPLANE-SESSION-MODEL.md` §7 의 검증 항목이 여기 들어 있다.

**DB 를 쓰지 않는다.** 셀 마스터는 `data/cells.json`(자체 생성 350셀)에서
읽는다. 상용 마스터(`CL_CELL_MAP`)로 돌리려면 MySQL 이 떠 있어야 하고, 그러면
"테스트가 환경에 의존해 건너뛰어지는" 상태가 되어 아무것도 지키지 못한다.
세션 모델이 검증해야 하는 것은 **인과와 분 경계**이고 거기에 상용 셀 값은
필요 없다.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from datagen import session as S
from datagen.cells import load_cells
from datagen.generator import USER_FIELDS, Generator
from datagen.profile import load as load_profiles
from datagen.scenarios import Rec

HERE = Path(__file__).resolve().parents[1]
DATA = HERE / "data"

#: 검증용 분당 호 수·세션 수. 실제 설정(20,000/22,000)보다 작게 둔다 — 비율과
#: 분 경계는 규모에 의존하지 않고, 테스트는 몇 초 안에 끝나야 한다.
RATES = {"lte_call": 2000, "transport": 2200, "dns": 900, "http": 1500}
ANCHOR = datetime(2026, 10, 14, 19, 0)


# --------------------------------------------------------------------------
# 픽스처 — C-Plane 호를 만들고 세션 엔진을 돌리는 최소 장치
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def gen() -> Generator:
    cells = load_cells(DATA / "cells.json")
    profiles = load_profiles(DATA / "profile.json")
    # `lte_call` 이 첫 자리여야 한다 — `Generator` 가 계열별 샘플러 시드를
    # `seed + i` 로 주므로 순서가 바뀌면 같은 seed 로도 재현되지 않는다.
    keep = {s: profiles[s] for s in ("lte_call", "transport", "dns", "http")}
    return Generator(cells=cells, profiles=keep, scenarios=[], rates=RATES,
                     seed=20260903, diurnal=True, subscriber_pool=3000,
                     success_only=True, end_only=True)


def _calls(gen: Generator, eng: S.SessionEngine,
           ts: datetime) -> tuple[list[S.Call], list[list[str]]]:
    """한 분의 C-Plane 호와 그중 성공 호의 `Call` 목록."""
    slot = ts.timestamp()
    idx = gen.index["lte_call"]
    i_user = tuple(idx[f] for f in USER_FIELDS)
    rows: list[list[str]] = []
    calls: list[S.Call] = []
    for _ in range(gen._volume("lte_call", ts)):
        rec, cell = gen.base_lte_call(ts, slot)
        rows.append(rec.values)
        outcome = eng.classify(rec)
        if outcome is None:
            continue
        calls.append(S.Call(
            cell=cell, user=tuple(rec.values[i] for i in i_user),
            apn=rec.get("APN"), t=float(rec.get("call_end_time") or slot),
            outcome=outcome, rel=rec.get("UEContextRelease_Cause")))
    return calls, rows


def _run(gen: Generator, minutes: int, *, shape: S.Shape | None = None,
         mutate=None) -> list[dict]:
    """``minutes`` 분을 돌려 분별 산출을 모은다. 세션은 분 사이를 이월한다."""
    eng = S.SessionEngine(gen, RATES, shape, seed=20260903)
    out = []
    for m in range(minutes):
        ts = ANCHOR + timedelta(minutes=m)
        calls, rows = _calls(gen, eng, ts)
        got = eng.minute(ts, calls, mutate)
        out.append({"ts": ts, "calls": calls, "cplane": rows, "up": got,
                    "engine": eng})
    return out


def _flags(rows: list[list[str]], gen: Generator,
           series: str = "transport") -> dict[str, int]:
    i = gen.index[series]["OngoingFlag"]
    out: dict[str, int] = {}
    for r in rows:
        out[r[i]] = out.get(r[i], 0) + 1
    return out


# --------------------------------------------------------------------------
# §7.1 비율 재현 — 집계가 세는 수가 설정을 재현한다
# --------------------------------------------------------------------------


def test_counted_records_reproduce_configured_rates(gen):
    """`ONGOINGFLAG ∈ {2,20}` 레코드 수가 `rates` 를 재현한다 (±10%).

    **집계는 종료 레코드만 센다**(graphio `codes.COUNTED_ONGOING_FLAGS`). 그래서
    `rates.transport` 가 뜻하는 것은 원시 레코드 수가 아니라 **분당 새 세션
    수 = 종료 레코드 수**다. 이 테스트가 깨지면 `GP_KPI_U_CELL_1M` 의 모든
    분모가 함께 움직이고, 최소 표본 규칙(불변규칙 5)의 판정 기준이 달라진다.

    정상 상태에 도달한 뒤를 본다 — 기동 직후에는 이월 세션이 없어 종료
    레코드가 적다(`SessionEngine` 머리말).
    """
    runs = _run(gen, 8)
    tail = runs[4:]
    counted = sum(
        sum(1 for r in m["up"]["transport"]
            if r[gen.index["transport"]["OngoingFlag"]] in S.COUNTED)
        for m in tail) / len(tail)
    dns = sum(len(m["up"]["dns"]) for m in tail) / len(tail)
    http = sum(len(m["up"]["http"]) for m in tail) / len(tail)

    assert RATES["transport"] * 0.9 <= counted <= RATES["transport"] * 1.1, counted
    assert RATES["dns"] * 0.9 <= dns <= RATES["dns"] * 1.1, dns
    assert RATES["http"] * 0.9 <= http <= RATES["http"] * 1.1, http


def test_raw_transport_rows_track_mean_session_span(gen):
    """원시 transport 레코드 수는 **활성 세션 수**(= 새 세션 × 평균 수명)다.

    늘어난 쪽은 전부 Interim 이라 집계에 들어가지 않는다. 이 관계가 깨지면
    Parquet 용량과 생성 시간이 예상과 다르게 움직인다.
    """
    shape = S.Shape(span_mean=2.0)
    runs = _run(gen, 10, shape=shape)
    raw = sum(len(m["up"]["transport"]) for m in runs[6:]) / 4
    want = RATES["transport"] * shape.span_mean
    assert want * 0.85 <= raw <= want * 1.15, (raw, want)


# --------------------------------------------------------------------------
# §7.2 인과 — 고아 레코드 0
# --------------------------------------------------------------------------


def test_no_orphan_uplane_records(gen):
    """U-Plane 의 `IMSI`·`eNB_ID`·`Cell_ID` 가 **같은 분의 C-Plane 호에 있다.**

    고아가 생기면 SCR-07(가입자 추적)이 "이 가입자가 무엇을 했나"를 못 잇고,
    RCA 증거팩의 셀 축이 C-Plane 과 어긋난다.

    첫 분만 본다 — 둘째 분부터는 **이월 세션**이 섞이고, 그 세션의 호는 이전
    분 것이라 "같은 분"이 아니다. 이월은 설계이고 고아가 아니다(세션 하나가
    여러 분에 걸친다).
    """
    runs = _run(gen, 1)
    minute = runs[0]
    want = {(c.user[0], c.cell.enb_id, str(c.cell.cell_id))
            for c in minute["calls"]}
    # 핸드오버로 셀이 바뀐 레코드도 허용해야 한다 — 같은 eNB 의 다른 섹터다
    enbs = {c.cell.enb_id for c in minute["calls"]}
    imsis = {c.user[0] for c in minute["calls"]}
    for series, rows in minute["up"].items():
        idx = gen.index[series]
        i_imsi, i_enb, i_cell = idx["IMSI"], idx["eNB_ID"], idx["Cell_ID"]
        for r in rows:
            key = (r[i_imsi], r[i_enb], r[i_cell])
            if key in want:
                continue
            # 핸드오버 — IMSI·eNB 는 호의 것이고 섹터만 바뀐다
            assert r[i_imsi] in imsis and r[i_enb] in enbs, (series, key)


def test_failed_calls_make_no_traffic(gen):
    """`SUCCESS_FLAG='0'` 호는 U-Plane 을 만들지 않는다 (§1.1).

    베어러가 서지 않았으므로 흘릴 데이터가 없다. 뒤집히면 "접속 실패인데
    트래픽이 흐른다"가 되어 R-21·R-27 의 증거가 모순된다.
    """
    eng = S.SessionEngine(gen, RATES, seed=1)
    values = gen.templates["lte_call"].copy()
    rec = Rec(values, gen.index["lte_call"])
    rec.set("success_flag", 0)
    assert eng.classify(rec) is None
    rec.set("success_flag", 1)
    assert eng.classify(rec) is not None


# --------------------------------------------------------------------------
# §7.3 ONGOINGFLAG 분포 — 열리면 닫힌다
# --------------------------------------------------------------------------


def test_every_session_opens_and_closes_exactly_once(gen):
    """세션 하나가 `Start`(또는 `Start/End`) 1회 · `End` 1회만 낸다.

    `SESSION_ID` 로 묶어 확인한다. 열림·닫힘이 어긋나면 집계 분모가 조용히
    비거나 부푼다 — `End` 가 두 번이면 세션 총량이 두 번 세어진다.
    """
    runs = _run(gen, 6)
    idx = gen.index["transport"]
    i_sid, i_flag = idx["SessionID"], idx["OngoingFlag"]
    seen: dict[str, list[str]] = {}
    for m in runs:
        for r in m["up"]["transport"]:
            seen.setdefault(r[i_sid], []).append(r[i_flag])

    # 마지막 분에 아직 진행 중인 세션은 `End` 가 없다 — 그게 정상이다
    open_now = {s.sid for s in runs[-1]["engine"].active}
    closed = 0
    for sid, flags in seen.items():
        assert flags.count(S.F_START) + flags.count(S.F_SINGLE) == 1, (sid, flags)
        n_end = flags.count(S.F_END) + flags.count(S.F_SINGLE)
        if sid in open_now:
            assert n_end == 0, (sid, flags)
            continue
        assert n_end == 1, (sid, flags)
        closed += 1
        # 중간 레코드는 Interim 또는 H/O 뿐이다
        assert set(flags) <= {S.F_START, S.F_INTERIM,
                              S.F_END, S.F_SINGLE}, (sid, flags)
    assert closed > 1000


def test_end_record_carries_the_session_total(gen):
    """`End` 의 `Traffic_Total_*` = 그 세션의 `Delta` 합.

    **이것이 `Total`/`Delta` 가 둘 다 있는 이유다.** 종료 레코드의 `Total` 이
    세션 전체를 대표하고 집계는 그것만 센다. 어긋나면 집계 바이트가 실제로
    흘러간 양과 달라진다 — 에러 없이 숫자만 틀린다.
    """
    runs = _run(gen, 8)
    idx = gen.index["transport"]
    i_sid, i_flag = idx["SessionID"], idx["OngoingFlag"]
    i_tot = idx["Traffic_Total_Dnlink_PayloadSize"]
    i_del = idx["Traffic_Delta_Dnlink_PayloadSize"]
    acc: dict[str, int] = {}
    ends: dict[str, int] = {}
    for m in runs:
        for r in m["up"]["transport"]:
            sid = r[i_sid]
            acc[sid] = acc.get(sid, 0) + int(r[i_del])
            if r[i_flag] in S.COUNTED:
                ends[sid] = int(r[i_tot])
    assert ends
    bad = [(s, v, acc[s]) for s, v in ends.items() if v != acc[s]]
    assert not bad, bad[:5]


# --------------------------------------------------------------------------
# §7.5 DNS 실패 세션에는 TCP·HTTP 가 없다
# --------------------------------------------------------------------------


def test_dns_failure_kills_the_session(gen):
    """DNS 가 실패하면 그 `SESSION_ID` 의 transport·http 가 **없다** (§1.2).

    `R-14 DNS_DEGRADATION` 이 성립하는 유일한 근거다. 지금까지는 DNS 와 HTTP 가
    독립이라 **DNS 장애 중에도 HTTP 가 정상**이었다.

    뮤테이터 자리에 DNS 전량 실패를 걸어 확인한다 — `SC-05` 가 하는 일과 같은
    모양이고, 그 변형이 세션 생성보다 **먼저** 돌아야 성립한다.
    """
    def mutate(series, rec, cell):
        if series == "dns":
            rec.set("ReplyCode", 2)       # SERVFAIL
            rec.set("AnswerCount", 0)

    runs = _run(gen, 3, mutate=mutate)
    dns_sids = set()
    tcp_sids = set()
    http_sids = set()
    for m in runs:
        dns_sids |= {r[gen.index["dns"]["SessionID"]] for r in m["up"]["dns"]}
        tcp_sids |= {r[gen.index["transport"]["SessionID"]]
                     for r in m["up"]["transport"]}
        http_sids |= {r[gen.index["http"]["SessionID"]] for r in m["up"]["http"]}
    assert dns_sids, "DNS 레코드가 하나도 없다 — p_dns 가 0 인가"
    assert not (dns_sids & tcp_sids), sorted(dns_sids & tcp_sids)[:5]
    assert not (dns_sids & http_sids), sorted(dns_sids & http_sids)[:5]


def test_dns_success_leaves_the_session_alive(gen):
    """DNS 가 성공하면 그 세션의 TCP 가 **있다** — TRANSPORT 는 베이스다.

    위 테스트의 짝이다. 실패만 확인하면 "항상 죽는" 구현도 통과한다.
    """
    runs = _run(gen, 1)
    dns_sids = {r[gen.index["dns"]["SessionID"]] for r in runs[0]["up"]["dns"]}
    tcp_sids = {r[gen.index["transport"]["SessionID"]]
                for r in runs[0]["up"]["transport"]}
    assert dns_sids
    assert dns_sids <= tcp_sids, sorted(dns_sids - tcp_sids)[:5]


# --------------------------------------------------------------------------
# §7.6 핸드오버 — 세션이 끊기지 않는다 (불변규칙 6)
# --------------------------------------------------------------------------


def test_handover_changes_the_cell_without_breaking_the_session(gen):
    """`ONGOINGFLAG=4` 뒤에 `Interim`/`End` 가 이어지고 셀이 바뀐다.

    **핸드오버는 절단이 아니다**(불변규칙 6, 정본은 graphio
    `codes.is_normal_release()`). 끊어 버리면 이동이 잦은 지역의 서비스품질이
    상시 나쁘게 나온다 — C-Plane 에서 막아 둔 함정을 U-Plane 에서 다시 만드는
    꼴이다.
    """
    runs = _run(gen, 8, shape=S.Shape(handover=1.0))
    idx = gen.index["transport"]
    i_sid, i_flag = idx["SessionID"], idx["OngoingFlag"]
    i_enb, i_cell = idx["eNB_ID"], idx["Cell_ID"]
    seq: dict[str, list[tuple[str, str, str]]] = {}
    for m in runs:
        for r in m["up"]["transport"]:
            seq.setdefault(r[i_sid], []).append(
                (r[i_flag], r[i_enb], r[i_cell]))

    # 마지막 분에 **아직 진행 중인** 세션은 창에 잘려 있다 — H/O 가 마지막
    # 레코드로 보일 수 있고 그것은 끊긴 것이 아니다. 닫힌 세션만 본다.
    open_now = {s.sid for s in runs[-1]["engine"].active}

    def moved_at(v):
        """셀이 바뀐 **첫 자리**. 없으면 `None`.

        **`OngoingFlag` 로 찾지 않는다** (2026-10-28). `4 H/O Interim` 은
        규격에 없는 확장이었고, *"프로브가 그것을 어떻게 아느냐"* 는 이유로
        빼기로 했다(`session._SPLIT_AT_HO`). 전환의 증거는 flag 가 아니라
        **셀이 실제로 바뀐 사실**이고, 그 쪽이 더 강한 기준이다.
        """
        for i in range(1, len(v)):
            if (v[i][1], v[i][2]) != (v[i - 1][1], v[i - 1][2]):
                return i
        return None

    ho = [v for sid, v in seq.items()
          if sid not in open_now and moved_at(v) is not None]
    assert ho, "핸드오버 세션이 하나도 없다"
    for v in ho:
        k = moved_at(v)
        assert k > 0, v                       # Start 가 먼저다
        # 세션이 **이어진다** — 전환 뒤에 레코드가 더 있고 끝은 End 다
        assert v[k][0] in (S.F_INTERIM, S.F_END), v
        assert v[-1][0] == S.F_END, v
        # `k` 는 **새 셀의 첫 줄**이고 `k-1` 이 떠난 셀의 마지막 줄이다.
        # 전에는 `flag=4` 로 떠난 줄을 찾아 "앞 줄과 eNB 가 같다"를 봤는데,
        # 기준을 **셀 변화**로 옮기면서 그 단언은 의미가 뒤집혔다 —
        # 지금 `v[k]` 는 바뀐 쪽이다.
        assert (v[k][1], v[k][2]) != (v[k - 1][1], v[k - 1][2]), v
        # **eNB 가 바뀔 수 있다.** 섹터만 바뀌는 것으로 고정하면 장비 ID 와
        # TAC 가 구조적으로 못 바뀐다(셀 마스터 실측: 같은 eNB 안 장비
        # 갈림 0 · TAC 갈림 0). `handover.Topology` 가 인접 eNB 도 준다.


def test_handover_sessions_still_close(gen):
    """핸드오버를 겪은 세션도 `End` 로 닫힌다 — H/O 가 종료를 삼키지 않는다."""
    runs = _run(gen, 10, shape=S.Shape(handover=1.0))
    idx = gen.index["transport"]
    i_sid, i_flag = idx["SessionID"], idx["OngoingFlag"]
    i_enb, i_cell = idx["eNB_ID"], idx["Cell_ID"]
    # **셀도 함께 담는다** — 전환을 `OngoingFlag` 가 아니라 셀 변화로 찾기
    # 때문이다(`4` 를 빼기로 했다 · `session._SPLIT_AT_HO`).
    seq: dict[str, list[tuple]] = {}
    for m in runs:
        for r in m["up"]["transport"]:
            seq.setdefault(r[i_sid], []).append(
                (r[i_flag], r[i_enb], r[i_cell]))
    open_now = {s.sid for s in runs[-1]["engine"].active}
    done = [v for sid, v in seq.items()
            if sid not in open_now
            and any(v[i][1:] != v[i - 1][1:] for i in range(1, len(v)))]
    assert done
    for v in done:
        assert [f for f, _, _ in v].count(S.F_END) == 1, v


# --------------------------------------------------------------------------
# §3 절단 case — C-Plane 의 결말이 U-Plane 에 드러난다
# --------------------------------------------------------------------------


def test_drop_cause_splits_rst_from_stall(gen):
    """무선 단절(RAN Cause)은 즉시 끊김, 그 외 절단은 한쪽만 지속이다 (§3).

    `ran_radio`(SC-01)가 심는 `121`(Radio Connection With UE Lost)과
    `mme_overload`(SC-03)가 심는 `500`(Control Processing Overload)이 서로
    다른 U-Plane 모양을 만들어야 조치가 갈린다.
    """
    eng = S.SessionEngine(gen, RATES, seed=7)
    rec = Rec(gen.templates["lte_call"].copy(), gen.index["lte_call"])
    rec.set("success_flag", 1)
    rec.set("drop_flag", 1)
    rec.set("UEContextRelease_Cause", 121)
    assert eng.classify(rec) == S.OUT_RST
    rec.set("UEContextRelease_Cause", 500)
    assert eng.classify(rec) == S.OUT_STALL


def test_handover_release_is_not_a_drop(gen):
    """핸드오버 유발 해제는 절단 분기에 들어오지 않는다 (불변규칙 6).

    `drop_flag` 가 서지 않은 호는 Cause 가 `102`(Successful Handover)여도
    정상 종료다 — 그 14종을 절단으로 세면 이동이 잦은 지역이 상시 붉어진다.
    """
    eng = S.SessionEngine(gen, RATES, seed=11)
    rec = Rec(gen.templates["lte_call"].copy(), gen.index["lte_call"])
    rec.set("success_flag", 1)
    rec.set("drop_flag", 0)
    for cause in ("102", "117", "120", "135", "300"):
        rec.set("UEContextRelease_Cause", cause)
        assert eng.classify(rec) in (S.OUT_NORMAL, S.OUT_ABORT), cause


def test_stall_raises_down_retransmission(gen):
    """"한쪽만 지속" 세션의 down 재전송이 정상 세션보다 **분명히 높다** (§3.2).

    `U-12`(down 재전송률)가 이것을 집는다. 비율이 실제로 움직여야 탐지가
    성립하므로 분모(Payload 패킷)는 건드리지 않고 분자만 올린다.
    """
    eng = S.SessionEngine(gen, RATES, seed=3)
    ts = ANCHOR
    slot = ts.timestamp()
    idx = gen.index["transport"]
    i_r, i_p = (idx["Traffic_Total_Dnlink_RetransPacketCount"],
                idx["Traffic_Total_Dnlink_PayloadPacketCount"])

    def ratio(outcome: str) -> float:
        out = {"transport": [], "dns": [], "http": []}
        cell = gen.cells[0]
        user = tuple("x" for _ in USER_FIELDS)
        retr = pay = 0
        for i in range(400):
            call = S.Call(cell=cell, user=user, apn="", t=slot + 1.0,
                          outcome=outcome, rel="120")
            eng._open(ts, slot, 1.0, call, out, None)
        for r in out["transport"]:
            retr += int(r[i_r])
            pay += int(r[i_p])
        return retr / max(1, pay)

    normal = ratio(S.OUT_NORMAL)
    stall = ratio(S.OUT_STALL)
    assert stall > normal * 3, (normal, stall)


def test_app_abort_shows_up_as_http_no_response(gen):
    """"U 에 의한 종료" 는 HTTP 응답이 없다 — `U-26` 이 집는다 (§3).

    `U-23`(HTTP 성공률)의 분모는 무응답을 빼므로(graphio `kpi.py` U-23) 성공률
    자체는 흔들리지 않는다. 그래서 이 case 를 넣어도 평시가 붉어지지 않는다.
    """
    eng = S.SessionEngine(gen, RATES, S.Shape(app_abort=1.0), seed=5)
    out = {"transport": [], "dns": [], "http": []}
    ts, slot = ANCHOR, ANCHOR.timestamp()
    user = tuple("x" for _ in USER_FIELDS)
    for _ in range(600):
        eng._open(ts, slot, 1.0,
                  S.Call(cell=gen.cells[0], user=user, apn="", t=slot + 1.0,
                         outcome=S.OUT_ABORT, rel="120"), out, None)
    assert out["http"]
    i_rt = gen.index["http"]["ResponseTime"]
    assert all(float(r[i_rt]) == 0.0 for r in out["http"])


# --------------------------------------------------------------------------
# §4 시간대 곡선 — 총 트래픽 4배
# --------------------------------------------------------------------------


def test_diurnal_curve_keeps_total_traffic_at_four_times(gen):
    """04시 ↔ 19시 총 트래픽이 약 4배다 (§4).

    곡선이 **세 자리**에 걸린다 — 호 수(1.14) × 호당 세션 수(1.75) × 세션당
    바이트(2.0) = 4.0. 한 자리만 고쳐지면 배수가 조용히 달라진다.
    """
    from datagen.generator import diurnal_factor

    f_call = diurnal_factor(4, "lte_call") / diurnal_factor(19, "lte_call")
    f_sess = (diurnal_factor(4, "uplane_sessions")
              / diurnal_factor(19, "uplane_sessions"))
    f_byte = diurnal_factor(4, "transport") / diurnal_factor(19, "transport")
    assert 3.7 <= 1 / (f_call * f_sess * f_byte) <= 4.3


def test_night_has_fewer_sessions_per_call(gen):
    """야간에는 **호당** 세션 수도 줄어든다 — 호 수만 줄지 않는다."""
    eng = S.SessionEngine(gen, RATES, seed=13)
    day = eng.session_lambda(ANCHOR.replace(hour=19), 1000)
    night = eng.session_lambda(ANCHOR.replace(hour=4), 1000)
    assert night < day * 0.65


# --------------------------------------------------------------------------
# 분포 모수 — 평균이 설정대로 나온다
# --------------------------------------------------------------------------


def test_truncated_geometric_mean_matches_the_target(gen):
    """`geom_p` 가 **잘린** 기하분포의 평균을 맞춘다.

    꼬리를 자른 만큼 평균이 내려가는 것을 보정하지 않으면 분당 세션 수가
    설정보다 적게 나온다 — 집계 분모가 조용히 줄어드는 형태다.
    """
    rng = random.Random(0)
    for mean, kmax in ((1.16, 3), (0.67, 3), (1.0, 9), (2.5, 9)):
        p = S.geom_p(mean, kmax)
        n = 40_000
        total = 0
        for _ in range(n):
            k = 0
            while k < kmax and rng.random() < p:
                k += 1
            total += k
        got = total / n
        assert abs(got - mean) < 0.03, (mean, kmax, got)


def test_unknown_shape_key_is_rejected(gen):
    """설정 오타를 조용히 삼키지 않는다 — 기본값으로 돌면 알 수 없다."""
    with pytest.raises(RuntimeError):
        S.Shape.from_cfg({"span_meen": 3})


# --------------------------------------------------------------------------
# §4 핸드오버 — 레코드를 끊는 **넷째 자리**
#
# 규칙과 요청서는 graphio `docs/jp/XDR규격-확장항목.md` §1 이다. `OngoingFlag=4`
# 는 규격서에 없는 확장이고, 운영 프로브가 주지 않을 수 있다 — 그래서 생성기
# 쪽에서만이라도 **맞는 모양**을 고정해 둔다.
# --------------------------------------------------------------------------


def _ho_minutes(gen, runs):
    """`(sid, 분)` → 그 분의 transport 레코드. 핸드오버가 있던 것만."""
    idx = gen.index["transport"]
    i_sid, i_flag = idx["SessionID"], idx["OngoingFlag"]
    per = {}
    for m_i, m in enumerate(runs):
        for r in m["up"]["transport"]:
            per.setdefault((r[i_sid], m_i), []).append(r)
    i_enb, i_cell = idx["eNB_ID"], idx["Cell_ID"]
    # **셀이 바뀐 분**이 핸드오버 분이다 — `OngoingFlag` 로 찾지 않는다
    # (`4` 를 빼기로 했다 · `session._SPLIT_AT_HO`). 한 분에 두 줄이고
    # 그 둘의 셀이 다르면 그 자리에서 끊긴 것이다.
    return {k: v for k, v in per.items()
            if len({(r[i_enb], r[i_cell]) for r in v}) > 1}


def test_handover_cuts_the_record_at_the_event_not_the_minute(gen):
    """핸드오버 분은 transport **두 줄**이고, 앞 줄이 떠난 셀이다.

    분 경계까지 미루면 한 줄에 셀이 하나뿐이라 **전환 전 트래픽이 새 셀로
    넘어간다.** 이동이 잦은 구간에서 그 오차는 상쇄되지 않고 떠나는 셀을
    과소, 들어오는 셀을 과대로 만든다. 그래서 사건 시각에 끊는다.
    """
    runs = _run(gen, 8, shape=S.Shape(handover=1.0))
    idx = gen.index["transport"]
    i_flag, i_enb, i_cell = idx["OngoingFlag"], idx["eNB_ID"], idx["Cell_ID"]
    i_t = idx["SummaryCreateTime"]
    ho = _ho_minutes(gen, runs)
    assert ho, "핸드오버가 하나도 없다"
    for v in ho.values():
        assert len(v) == 2, v                      # 그 분만 두 줄
        a, b = sorted(v, key=lambda r: float(r[i_t]))
        # **둘 다 규격 안의 값이다** — `4` 를 적지 않는다. 전환은
        # *"같은 분·같은 세션에 셀이 다른 두 줄"* 이라는 사실로만 남는다.
        assert a[i_flag] in (S.F_START, S.F_INTERIM), v
        assert b[i_flag] in (S.F_INTERIM, S.F_END), v
        # **셀이 실제로 바뀐다.** 안 바뀌면 일어나지 않은 전환이고,
        # 집계가 그것을 세면 Q-08 이 부푼다
        assert (a[i_enb], a[i_cell]) != (b[i_enb], b[i_cell]), v
        assert float(a[i_t]) <= float(b[i_t])


def test_handover_split_preserves_delta_sum(gen):
    """쪼갠 두 줄의 `Delta` 합 = 쪼개기 전 한 줄 — `Σ Delta ≈ Total`(H-23).

    끊는 자리를 늘리면서 양이 늘거나 줄면 집계 바이트가 실제로 흘러간 양과
    달라진다. **에러 없이 숫자만 틀린다.**
    """
    runs = _run(gen, 10, shape=S.Shape(handover=1.0))
    idx = gen.index["transport"]
    i_sid, i_flag = idx["SessionID"], idx["OngoingFlag"]
    seq = {}
    for m in runs:
        for r in m["up"]["transport"]:
            seq.setdefault(r[i_sid], []).append(r)
    open_now = {s.sid for s in runs[-1]["engine"].active}

    checked = 0
    for name in ("Dnlink_PayloadSize", "Uplink_PayloadSize",
                 "Dnlink_PayloadPacketCount"):
        i_del = idx[f"Traffic_Delta_{name}"]
        i_tot = idx[f"Traffic_Total_{name}"]
        for sid, rows in seq.items():
            if sid in open_now:
                continue
            end = [r for r in rows if r[i_flag] in (S.F_END, S.F_SINGLE)]
            if not end:
                continue
            # 정수 절단 때문에 1 까지는 둔다 — 쪼개기와 무관한 오차다
            assert abs(int(end[0][i_tot])
                       - sum(int(r[i_del]) for r in rows)) <= 1, (sid, name)
            checked += 1
    assert checked > 1000


def test_handover_minute_splits_http_cell_too(gen):
    """같은 분 HTTP 도 전환 시각 전이면 **떠난 셀**이다.

    transport 만 가르고 HTTP 를 `s.cell` 하나로 적으면 전환 전 요청이 새 셀로
    넘어간다 — 그 분의 HTTP 응답시간이 엉뚱한 셀에 쌓인다.
    """
    runs = _run(gen, 10, shape=S.Shape(handover=1.0))
    idx, hidx = gen.index["transport"], gen.index["http"]
    i_t, i_enb, i_cell = (idx["SummaryCreateTime"], idx["eNB_ID"],
                          idx["Cell_ID"])
    h_sid, h_t = hidx["SessionID"], hidx["SummaryCreateTime"]
    h_enb, h_cell = hidx["eNB_ID"], hidx["Cell_ID"]

    seen = 0
    for (sid, m_i), v in _ho_minutes(gen, runs).items():
        a, b = sorted(v, key=lambda r: float(r[i_t]))
        t_ho = float(a[i_t])
        want = {True: (a[i_enb], a[i_cell]), False: (b[i_enb], b[i_cell])}
        for h in runs[m_i]["up"]["http"]:
            if h[h_sid] != sid:
                continue
            seen += 1
            assert (h[h_enb], h[h_cell]) == want[float(h[h_t]) < t_ho]
    assert seen > 100, seen


def test_fixed_subscriber_handover_stays_in_the_same_site(gen):
    """**노선을 타지 않는 가입자는 섹터 전환만 한다.**

    그 사람은 움직이고 있지 않다 — 집·사무실에서 통신하는 중이고, 전환이
    난다면 섹터 경계에 걸쳐 있기 때문이다. eNB 간 전환을 주면 **장비가 옮겨
    가고 TAU 까지 따라붙는다**(§1.11). 이동하지 않는 사람에게 그건 사실이
    아니고, 그 TAU 가 분모를 채워 "이동이 많은 망"으로 보이게 한다.

    eNB 간 전환과 코어 재배치는 **경로를 타는 가입자**의 것이다
    (2026-10-27 사람 결정).
    """
    runs = _run(gen, 10, shape=S.Shape(handover=1.0))
    idx = gen.index["transport"]
    i_t, i_enb, i_cell = (idx["SummaryCreateTime"], idx["eNB_ID"],
                          idx["Cell_ID"])
    i_mme, i_sgw = idx["MME_ID"], idx["SGW_ID"]
    ho = _ho_minutes(gen, runs)
    assert ho, "핸드오버가 하나도 없다"
    for v in ho.values():
        a, b = sorted(v, key=lambda r: float(r[i_t]))
        # 셀은 바뀐다 — 안 바뀌면 일어나지 않은 전환이다
        assert (a[i_enb], a[i_cell]) != (b[i_enb], b[i_cell]), v
        # **eNB·코어는 바뀌지 않는다**
        assert a[i_enb] == b[i_enb], v
        assert a[i_mme] == b[i_mme] and a[i_sgw] == b[i_sgw], v


def test_route_subscriber_handover_can_cross_enb_and_core(gen):
    """경로를 타는 가입자는 **eNB 를 넘고 코어도 재배치된다.**

    고정 가입자에게 막은 것을 여기서는 허용해야 한다 — 그러지 않으면 TAC·
    MME·SGW 가 구조적으로 바뀌지 않아 TAU 도 S10 도 데이터에 없다(§1.6).
    """
    routes, runs, route_of = _mobile_run(gen, 12)
    idx = gen.index["transport"]
    i_imsi, i_sid = idx["IMSI"], idx["SessionID"]
    i_t, i_enb, i_cell = (idx["SummaryCreateTime"], idx["eNB_ID"],
                          idx["Cell_ID"])
    i_mme, i_sgw = idx["MME_ID"], idx["SGW_ID"]
    seq: dict[str, list[tuple]] = {}
    for m in runs:
        for r in sorted(m["up"]["transport"], key=lambda x: float(x[i_t])):
            if route_of.get(r[i_imsi], -1) < 0:
                continue
            seq.setdefault(r[i_sid], []).append(
                (r[i_enb], r[i_cell], r[i_mme], r[i_sgw]))
    enb = core = moves = 0
    for rows in seq.values():
        for a, b in zip(rows, rows[1:]):
            if a[:2] == b[:2]:
                continue
            moves += 1
            if a[0] != b[0]:
                enb += 1
            if a[2:] != b[2:]:
                core += 1
                assert a[0] != b[0], (a, b)   # 코어 재배치는 eNB 간이다
    assert moves > 500, moves
    assert enb / moves > 0.3, enb / moves
    assert core > 0, "코어 재배치가 한 번도 없다"


def test_core_is_carried_by_the_session_not_the_cell(gen):
    """코어는 **세션이 들고 다닌다** — 셀에서 매번 읽지 않는다.

    `Cell` 이 들고 있는 장비 ID 를 레코드마다 그대로 쓰면, eNB 가 바뀌는
    전환에서 **무조건** MME·SGW 가 바뀐다. 그렇게 두었을 때 전환 4,773건 중
    1,634건이 MME 를 갈았다 — X2 핸드오버는 코어를 건드리지 않으므로 틀린
    모양이고, 노드 축 집계가 세션 안에서 근거 없이 갈린다.

    그래서 코어는 **세션이 들고 다닌다**(`Session.core`).
    """
    runs = _run(gen, 10, shape=S.Shape(handover=1.0))
    idx = gen.index["transport"]
    i_t, i_enb = idx["SummaryCreateTime"], idx["eNB_ID"]
    i_mme, i_sgw = idx["MME_ID"], idx["SGW_ID"]
    ho = _ho_minutes(gen, runs)
    mme = sgw = intra_moved = 0
    for v in ho.values():
        a, b = sorted(v, key=lambda r: float(r[i_t]))
        if a[i_mme] != b[i_mme]:
            mme += 1
        if a[i_sgw] != b[i_sgw]:
            sgw += 1
        # 섹터 전환(같은 eNB)은 코어가 **절대** 바뀌지 않는다
        if a[i_enb] == b[i_enb]:
            assert a[i_mme] == b[i_mme] and a[i_sgw] == b[i_sgw], v
            intra_moved += 1
    assert intra_moved, "섹터 전환이 하나도 없다"
    # 재배치는 소수다 — `S1` 12% · `S1_MME` 3%. 이웃이 같은 코어를 쓰면
    # 관측되지 않으므로 상한만 본다
    assert mme / len(ho) < 0.10, mme / len(ho)
    assert sgw / len(ho) < 0.30, sgw / len(ho)
    assert mme <= sgw, (mme, sgw)   # MME 재배치는 SGW 재배치를 동반한다


# --------------------------------------------------------------------------
# §5 이동 궤적 — 전환이 "움직였기 때문에" 일어난다
#
# 경로가 없으면 전환 상대를 추첨한다. 한 전환만 보면 맞지만 이어 놓고 보면
# **가입자가 순간이동한다** — A→B 갔다가 다음엔 A 반대쪽 C 로. 그래서 셀 쌍
# 표(장표 9)에 의미 있는 쌍이 쌓이지 않고 노선 코리도 롤업이 서지 않는다.
# 배경은 `handover.Route` 머리말, 요청서는 graphio
# `docs/jp/XDR규격-확장항목.md` §1.6.
# --------------------------------------------------------------------------


def _mobile_run(gen, minutes, *, share=0.30, call_share=0.6, n_routes=10):
    """경로를 깔고 돌린다. **`Shape.handover=0.0`** — 추첨 전환을 끈다.

    끄는 이유: 전환이 전부 경로에서 왔음을 확인하려는 것이다. 섞여 있으면
    "경로를 따랐다"와 "우연히 이웃이었다"를 가를 수 없다.
    """
    import random as _r
    from datagen import handover as _HO
    rng = _r.Random(5)
    topo = _HO.Topology(gen.cells)
    routes = _HO.build_routes(topo, gen.cells, n_routes, rng, length=8)
    gen.seat_routes(routes, share, rng)
    gen.mobility_share = call_share
    try:
        eng = S.SessionEngine(gen, RATES, S.Shape(handover=0.0), seed=20260903)
        out = []
        for m in range(minutes):
            ts = ANCHOR + timedelta(minutes=m)
            gen.advance_routes(ts.timestamp())
            calls, _rows = _calls(gen, eng, ts)
            out.append({"ts": ts, "up": eng.minute(ts, calls, None),
                        "engine": eng})
        # **태운 상태를 여기서 찍어 돌려준다.** 끄고 나면 `sub.route` 가
        # 전부 `-1` 이라 호출부가 누가 이동 가입자였는지 알 수 없다
        route_of = {s.imsi: s.route for s in gen.subs if s.route >= 0}
        return routes, out, route_of
    finally:
        # 모듈 스코프 fixture 다 — 끄지 않으면 뒤 테스트가 이동 상태를 물려받는다
        gen.seat_routes([], 0.0, rng)
        gen.mobility_share = 0.0


def test_mobile_session_follows_the_route_in_order(gen):
    """한 세션 안에서 연속한 셀은 경로의 **인접 칸**이다.

    추첨이면 경로를 깔아도 순서가 지켜지지 않는다. 그러면 "이 구간에서
    전환 직후 정체"라는 말이 성립하지 않는다 — 구간이 없으니까.
    """
    routes, runs, route_of = _mobile_run(gen, 12)
    idx = gen.index["transport"]
    i_sid, i_imsi = idx["SessionID"], idx["IMSI"]
    i_enb, i_cell, i_t = idx["eNB_ID"], idx["Cell_ID"], idx["SummaryCreateTime"]

    seq = {}
    for m in runs:
        for r in sorted(m["up"]["transport"], key=lambda x: float(x[i_t])):
            seq.setdefault(r[i_sid], []).append(
                (r[i_imsi], f"{r[i_enb]}-{r[i_cell]}"))

    moves = 0
    for rows in seq.values():
        ri = route_of.get(rows[0][0], -1)
        if ri < 0:
            continue
        rt = routes[ri]
        keys = [c.cell_key for c in rt.cells]
        n, prev = len(keys), None
        for _imsi, k in rows:
            assert k in keys, (k, rt.id)      # 경로를 벗어나지 않는다
            pos = keys.index(k)
            if prev is not None and pos != prev:
                moves += 1
                assert (abs(pos - prev) == 1
                        or (rt.loop and abs(pos - prev) == n - 1)), (prev, pos)
            prev = pos
    assert moves > 500, moves


def test_mobile_calls_happen_where_the_subscriber_is(gen):
    """이동 가입자의 호는 **그 사람 위치 셀**에서 생긴다.

    호 생성이 셀을 먼저 고르고 가입자를 나중에 고르는 구조라, 고른 뒤에
    셀을 옮기지 않으면 이동 가입자가 임의 셀에 나타난다 — 실측으로 925명 중
    **863명이 경로를 벗어났다.** 궤적이 깨지면 경로를 깐 의미가 없다.
    """
    routes, runs, route_of = _mobile_run(gen, 8)
    idx = gen.index["transport"]
    i_imsi, i_enb, i_cell = idx["IMSI"], idx["eNB_ID"], idx["Cell_ID"]
    on = off = 0
    for m in runs:
        for r in m["up"]["transport"]:
            ri = route_of.get(r[i_imsi], -1)
            if ri < 0:
                continue
            keys = {c.cell_key for c in routes[ri].cells}
            if f"{r[i_enb]}-{r[i_cell]}" in keys:
                on += 1
            else:
                off += 1
    assert on > 1000, on
    assert off == 0, (on, off)


def test_route_concentrates_cell_pairs(gen):
    """전환이 **같은 구간에 쌓인다** — 그게 셀 쌍 표가 서는 조건이다.

    추첨이면 쌍이 흩어져 모든 쌍이 한두 건이고, "이 구간이 문제다"를 말할
    수 없다.
    """
    routes, runs, _route_of = _mobile_run(gen, 12)
    idx = gen.index["transport"]
    i_sid, i_enb, i_cell = idx["SessionID"], idx["eNB_ID"], idx["Cell_ID"]
    i_t = idx["SummaryCreateTime"]
    seg: dict[tuple[str, str], int] = {}
    for m in runs:
        cur: dict[str, str] = {}
        for r in sorted(m["up"]["transport"], key=lambda x: float(x[i_t])):
            k = f"{r[i_enb]}-{r[i_cell]}"
            prev = cur.get(r[i_sid])
            if prev is not None and prev != k:
                key = (prev, k) if prev < k else (k, prev)
                seg[key] = seg.get(key, 0) + 1
            cur[r[i_sid]] = k
    assert seg
    # 경로 구간 수(최대 `Σ len`)보다 많은 쌍이 나오면 추첨이 섞인 것이다
    assert len(seg) <= sum(len(r) for r in routes), (len(seg), len(routes))
    top = sorted(seg.values(), reverse=True)
    assert top[0] > 50, top[:5]


def test_mobility_is_off_by_default(gen):
    """경로를 깔지 않으면 **아무도 움직이지 않는다.**

    역호환이다 — 경로를 깔면 전환 수와 셀 쌍 분포가 바뀌어 기존 기준선과
    전후 비교가 안 된다. 켜는 것이 사람의 결정이어야 한다.
    """
    assert gen.routes == []
    assert gen.mobility_share == 0.0
    assert all(s.route < 0 for s in gen.subs)
    assert gen.advance_routes(ANCHOR.timestamp()) == 0
    assert gen.cell_of(gen.subs[0]) is None


def test_journey_stays_inside_the_defined_segment(gen):
    """여정을 적으면 가입자가 **그 구간 안에서만** 움직인다.

    끝없이 순환하게 두면 정의한 구간이 의미를 잃는다 — 실측에서 칸 구간을
    적었는데 구간 밖 관측이 **18.4%** 였다. 원인은 한 여정이 끝난 뒤 임의
    목적지를 새로 뽑은 것이었다.
    """
    import random as _r
    from datagen import handover as _HO
    rng = _r.Random(11)
    routes = _HO.build_routes(_HO.Topology(gen.cells), gen.cells, 1, rng,
                              length=10)
    assert routes, "경로 생성 실패"
    assert len(routes[0]) >= 3, len(routes[0])
    a, b = 1, len(routes[0]) - 1
    gen.journeys = [{"route": routes[0].id, "from": a, "to": b, "share": 1.0}]
    gen.off_sec = (120, 300)
    try:
        gen.seat_routes(routes, 1.0, rng)
        gen.mobility_share = 0.9
        t0 = ANCHOR.timestamp()
        seen = set()
        for m in range(240):
            gen.advance_routes(t0 + m * 60)
            seen |= {s.route_pos for s in gen.subs if s.route >= 0}
        assert seen <= set(range(a, b + 1)), sorted(seen)
        assert a in seen and b in seen, sorted(seen)
    finally:
        gen.journeys = []
        gen.seat_routes([], 0.0, rng)
        gen.mobility_share = 0.0


def test_journey_is_one_way_only(gen):
    """한 가입자는 **한 방향으로만** 간다 — 왕복하지 않는다.

    `5→9` 를 적은 가입자는 늘 칸 번호가 늘어나는 쪽으로 가고, `21→19` 를
    적은 가입자는 늘 줄어드는 쪽으로 간다. 왕복하게 두면 그 사람의 전환
    구간이 양방향으로 섞여, **장애를 한 방향 구간에 주입했을 때 영향 가입자가
    반대 방향 통행자까지 포함한다.**

    감소 방향(`to < from`)도 똑같이 성립해야 한다 — 순환선에서 반대로 돌면
    같은 두 칸 사이인데 지나는 구간이 전혀 다르다.
    """
    import random as _r
    from datagen import handover as _HO
    for up in (True, False):
        rng = _r.Random(23)
        routes = _HO.build_routes(_HO.Topology(gen.cells), gen.cells, 1, rng,
                                  length=10)
        # **실제 길이에서 구간을 잡는다.** `build_routes` 는 이웃이 끊기면
        # 요청보다 짧은 경로를 준다 — 상수로 적으면 그때 범위를 벗어난다
        n = len(routes[0])
        assert n >= 3, n
        lo, hi = 0, n - 1
        a, b = (lo, hi) if up else (hi, lo)
        gen.journeys = [{"route": routes[0].id, "from": a, "to": b,
                         "share": 1.0}]
        gen.off_sec = (120, 240)
        try:
            gen.seat_routes(routes, 1.0, rng)
            t0 = ANCHOR.timestamp()
            prev = {id(s): s.route_pos for s in gen.subs if s.route >= 0}
            step = 1 if b > a else -1
            moves = 0
            for m in range(300):
                gen.advance_routes(t0 + m * 60)
                for sub in gen.subs:
                    if sub.route < 0:
                        continue
                    was = prev[id(sub)]
                    now = sub.route_pos
                    if now != was:
                        if now == a and was == b:
                            pass          # 재승차 — 구간 시작으로 되돌아간다
                        else:
                            # **진행은 늘 같은 방향 한 칸이다**
                            assert now - was == step, (a, b, was, now)
                            moves += 1
                        prev[id(sub)] = now
            assert moves > 50, (a, b, moves)
        finally:
            gen.journeys = []
            gen.seat_routes([], 0.0, rng)
            gen.mobility_share = 0.0


def test_session_does_not_teleport_across_a_reboarding(gen):
    """재승차 경계를 **세션이 넘지 않는다.**

    하차 칸에서 승차 칸으로 돌아가는 것은 셀의 불연속이다. 그때 열려 있던
    세션이 경로를 계속 따르면 **인접하지 않은 셀로 순간이동한 핸드오버**가
    레코드에 남는다 — 셀 쌍 표에 있을 수 없는 쌍이 생긴다.
    """
    import random as _r
    from datagen import handover as _HO
    rng = _r.Random(31)
    routes = _HO.build_routes(_HO.Topology(gen.cells), gen.cells, 1, rng,
                              length=8)
    rt = routes[0]
    assert len(rt) >= 3, len(rt)
    a, b = 0, len(rt) - 1
    gen.journeys = [{"route": rt.id, "from": a, "to": b, "share": 1.0}]
    # **일부러 짧게 둔다** — 세션 수명(최대 10분) 안에 재승차가 일어나
    # 경계를 세션이 넘을 기회를 만든다
    gen.off_sec = (60, 120)
    try:
        gen.seat_routes(routes, 1.0, rng)
        gen.mobility_share = 0.9
        eng = S.SessionEngine(gen, RATES, S.Shape(handover=0.0), seed=31)
        idx = gen.index["transport"]
        i_sid, i_enb, i_cell = (idx["SessionID"], idx["eNB_ID"],
                                idx["Cell_ID"])
        i_t = idx["SummaryCreateTime"]
        keys = [c.cell_key for c in rt.cells]
        seq: dict[str, list[str]] = {}
        for m in range(60):
            ts = ANCHOR + timedelta(minutes=m)
            gen.advance_routes(ts.timestamp())
            calls, _rows = _calls(gen, eng, ts)
            got = eng.minute(ts, calls, None)
            for r in sorted(got["transport"], key=lambda x: float(x[i_t])):
                seq.setdefault(r[i_sid], []).append(
                    f"{r[i_enb]}-{r[i_cell]}")
        jumps = checked = 0
        for rows in seq.values():
            pos = [keys.index(k) for k in rows if k in keys]
            for x, y in zip(pos, pos[1:]):
                if x == y:
                    continue
                checked += 1
                if abs(x - y) != 1:
                    jumps += 1
        assert checked > 200, checked
        assert jumps == 0, (jumps, checked)
    finally:
        gen.journeys = []
        gen.seat_routes([], 0.0, rng)
        gen.mobility_share = 0.0


def test_journey_dwells_at_the_destination(gen):
    """하차하면 **머문다** — 그 동안 셀이 고정이고 전환이 없다.

    역 주변 체류가 없으면 역세권 혼잡(`j-sc-14` 도심 역 저녁 러시)이
    생기지 않는다. 모든 가입자가 늘 이동 중이기 때문이다.
    """
    import random as _r
    from datagen import handover as _HO
    rng = _r.Random(13)
    routes = _HO.build_routes(_HO.Topology(gen.cells), gen.cells, 1, rng,
                              length=6)
    gen.journeys = [{"route": routes[0].id, "from": 0,
                     "to": len(routes[0]) - 1, "share": 1.0}]
    gen.off_sec = (600, 900)
    try:
        gen.seat_routes(routes, 1.0, rng)
        t0 = ANCHOR.timestamp()
        waited = False
        for m in range(90):
            gen.advance_routes(t0 + m * 60)
            if any(s.route_wait > 0 for s in gen.subs if s.route >= 0):
                waited = True
                break
        assert waited, "아무도 하차하지 않았다"
        wait = [s for s in gen.subs if s.route >= 0 and s.route_wait > 0]
        pos = {id(s): s.route_pos for s in wait}
        gen.advance_routes(t0 + 91 * 60)
        for s in wait:
            if s.route_wait > 0:
                assert s.route_pos == pos[id(s)]
    finally:
        gen.journeys = []
        gen.seat_routes([], 0.0, rng)
        gen.mobility_share = 0.0


def test_bad_journey_spec_stops(gen):
    """칸 범위를 벗어나거나 모르는 이름·노선이면 **멈춘다.**

    조용히 자르면 여정이 엉뚱한 구간이 되고, "정의한 구간에 가입자가 없다"는
    결과가 나오는데 원인이 설정 오타다.
    """
    import random as _r
    import pytest as _p
    from datagen import handover as _HO
    rng = _r.Random(17)
    routes = _HO.build_routes(_HO.Topology(gen.cells), gen.cells, 1, rng,
                              length=5)
    rid, n = routes[0].id, len(routes[0])
    cases = [
        ({"route": rid, "from": 0, "to": n, "share": 1.0}, "칸 범위"),
        ({"route": rid, "from": -1, "to": 2, "share": 1.0}, "칸 범위"),
        ({"route": rid, "from": 2, "to": 2, "share": 1.0}, "같은 칸"),
        ({"route": rid, "from": 0, "to": "NOPE", "share": 1.0}, "못 찾는다"),
        ({"route": "NO-SUCH", "from": 0, "to": 1, "share": 1.0}, "모르는 노선"),
    ]
    try:
        for spec, msg in cases:
            gen.journeys = [spec]
            with _p.raises(RuntimeError, match=msg):
                gen.seat_routes(routes, 1.0, rng)
    finally:
        gen.journeys = []
        gen.seat_routes([], 0.0, rng)
        gen.mobility_share = 0.0


def test_home_locality_is_off_by_default(gen):
    """**기본은 전체 균일 추첨이다 — 샘플 XDR 이 이 축을 규정하지 않는다.**

    샘플 분포(`_distributions.json`)의 축은 call_type · flags ·
    release_cause · first_error · durations · apn · vendor_model 이고,
    `profile.json` 의 `lte_call` 범주형 51개 중 셀·가입자를 잇는 것은 없다
    (`First_eNB_PLMN`·`imsi_mcc_mnc_info` 는 PLMN 접두어다).

    규정하지 않는 축에 모델을 넣는 것은 **사람의 결정**이어야 한다. 기본값이
    바뀌면 셀·분 `SUBS` 가 -22% 움직여 기존 기준선과 전후 비교가 끊긴다.
    """
    assert gen.home_locality is False
    assert gen.mobility_share == 0.0
    assert gen.routes == []


def test_home_locality_keeps_subscribers_near_home_when_on(gen):
    """켜면 **그 셀 또는 반경 안**에 머문다 — 전국을 순간이동하지 않는다.

    `Subscriber.home_cell` 과 그것을 보는 `_sub_for()` 가 있는데 `_lte_call`
    이 부르지 않아 **죽은 코드였다**(2026-10-27 연결). 매 호가 가입자를 균일
    추첨해서, 한 가입자가 60분에 서로 다른 셀 **25개**(전체 348개 중)에
    나타났고 home 적중률이 7.1% — 무작위 기대값과 같았다.

    세션 **안**의 핸드오버는 그래도 멀쩡했다(세션당 최대 1회 인접 전환).
    깨지는 것은 **가입자 단위 궤적**이고, 그것이 일별 체감(S-01)과 민원
    조회(SCR-07' "이 가입자가 어디를 다녔나")의 근거다.
    """
    gen.home_locality = True
    try:
        runs = _run(gen, 20)
    finally:
        gen.home_locality = False
    idx = gen.index["transport"]
    i_imsi, i_enb, i_cell = idx["IMSI"], idx["eNB_ID"], idx["Cell_ID"]
    seen: dict[str, set[str]] = {}
    for m in runs:
        for r in m["up"]["transport"]:
            seen.setdefault(r[i_imsi], set()).add(f"{r[i_enb]}-{r[i_cell]}")
    from datagen.generator import _dist_m
    home = {s.imsi: gen.cells[s.home_cell] for s in gen.subs}
    by_key = {c.cell_key: c for c in gen.cells}
    hit = sum(1 for i, v in seen.items() if home[i].cell_key in v)
    assert seen
    # **home 에 나타나는 것이 압도적이어야 한다.** 무작위면 셀 수의 역수다
    assert hit / len(seen) > 0.80, hit / len(seen)
    # 그래도 **꼬리가 있어야 한다** — 전부 home 이면 "옆 셀에서도 통신한다"가
    # 사라져 이동이 없는 망이 된다(`_sub_for` 의 72%)
    away = sum(1 for i, v in seen.items()
               if len(v - {home[i].cell_key}) > 0)
    assert away > 0, "home 밖 통신이 하나도 없다"
    # **그 꼬리가 "주변" 이어야 한다.** 자르지 않으면 두 자리에서 멀리 간다 —
    # k-최근접 이웃이 희소 지역에서 수십 km 를 잡고(실측 90km), 주민 없는
    # 셀의 폴백이 전체 추첨이면 **제주 가입자가 서울 셀에 나타난다**
    # (실측 471km · 관측의 2.94%).
    far = [_dist_m(home[i], by_key[k]) for i, v in seen.items()
           for k in v if k in by_key]
    # 반경은 **거주지 선택**에 걸린다. 그 뒤 섹터 전환이 사이트 오프셋만큼
    # 더 가므로(실측 +145m) 약간의 여유를 둔다 — 조이면 섹터 좌표가 흔들릴
    # 때 테스트가 이유 없이 깨진다
    assert max(far) <= gen.home_radius_m * 1.05, max(far)
    far.sort()
    assert far[len(far) // 2] < gen.home_radius_m / 2, far[len(far) // 2]


def test_wiring_home_cell_does_not_touch_route_subscribers(gen):
    """`home_cell` 연결이 **이동 가입자를 건드리지 않는다.**

    두 경로가 같은 자리에서 갈린다(`_lte_call`). 이동 가입자는 "지금 어디
    있는지"가 먼저이고, 고정 가입자는 "셀을 고른 뒤 그 셀 사람"이다. 한쪽을
    고치다 다른 쪽이 섞이면 경로를 깔아도 궤적이 깨진다.
    """
    routes, runs, route_of = _mobile_run(gen, 10)
    idx = gen.index["transport"]
    i_imsi, i_enb, i_cell = idx["IMSI"], idx["eNB_ID"], idx["Cell_ID"]
    off = 0
    for m in runs:
        for r in m["up"]["transport"]:
            ri = route_of.get(r[i_imsi], -1)
            if ri < 0:
                continue
            keys = {c.cell_key for c in routes[ri].cells}
            if f"{r[i_enb]}-{r[i_cell]}" not in keys:
                off += 1
    assert off == 0, off


def test_total_restarts_when_the_core_is_relocated(gen):
    """장비가 재배치되면 **누적이 0 부터 다시 시작한다.**

    XDR 은 SGW·MME 단위로 생성된다. 그래서 재배치 뒤의 장비는 **이 세션을
    처음 본다** — 누적(`Traffic_Total_*`)이 거기서 다시 시작하고,
    `Total.FirstTime` 이 그 시각이 된다(사람 확인 2026-10-27).

    그 결과 **장비가 바뀐 세션의 종료 레코드 `Total` 은 세션 전체가 아니다** —
    재배치 뒤 구간만이다. 조각을 이어 붙이는 것은 통계 쪽 일이다.

    `INTRA_ENB`·`X2` 는 리셋하지 않는다 — 장비가 그대로이고, 리셋하면 같은
    프로브가 자기가 센 것을 잊는 셈이 된다.
    """
    routes, runs, route_of = _mobile_run(gen, 20, share=0.8, call_share=0.9)
    idx = gen.index["transport"]
    i_sid, i_flag = idx["SessionID"], idx["OngoingFlag"]
    i_t = idx["SummaryCreateTime"]
    i_tot = idx["Traffic_Total_Dnlink_PayloadSize"]
    i_del = idx["Traffic_Delta_Dnlink_PayloadSize"]
    i_ft = idx["Traffic_Total_Dnlink_FirstTime"]
    i_sgw, i_enb = idx["SGW_ID"], idx["eNB_ID"]

    seq: dict[str, list] = {}
    for m in runs:
        for r in sorted(m["up"]["transport"], key=lambda x: float(x[i_t])):
            seq.setdefault(r[i_sid], []).append(r)

    reset = plain = 0
    for rows in seq.values():
        if rows[-1][i_flag] not in (S.F_END, S.F_SINGLE):
            continue
        # **리셋 지점은 `Total.FirstTime` 이 바뀐 자리다.** 누적이 내려간
        # 자리로 찾으면 누락한다 — 전환이 분 앞쪽이면 리셋 뒤 누적이 더 클
        # 수 있다. SGW 번호로도 안 된다(번호가 적어 재배치가 안 보인다)
        ft = [r[i_ft] for r in rows]
        cut = [i for i in range(1, len(ft)) if ft[i] != ft[i - 1]]
        tots = [int(r[i_tot]) for r in rows]
        dels = [int(r[i_del]) for r in rows]
        if cut:
            reset += 1
            # 마지막 리셋 이후 Delta 합 == 종료 레코드 Total. **정확히** 같아야
            # 한다 — `Total` 과 `Delta` 가 같은 절단 급수의 차이이기 때문이다
            assert tots[-1] == sum(dels[cut[-1]:]), (tots, dels, cut)
            # 리셋은 **eNB 가 바뀐 자리에서만** 난다
            for i in cut:
                assert rows[i][i_enb] != rows[i - 1][i_enb], rows[i][i_enb]
        else:
            plain += 1
            assert tots[-1] == sum(dels), (tots, dels)
        # 리셋 구간 안에서는 누적이 단조 증가다
        for a, b in zip([0] + cut, cut + [len(tots)]):
            seg = tots[a:b]
            assert all(y >= x for x, y in zip(seg, seg[1:])), seg
    assert reset > 100, reset
    assert plain > 100, plain


def test_sector_handover_does_not_restart_the_total(gen):
    """**섹터 전환은 리셋하지 않는다** — 장비가 그대로다.

    고정 가입자는 `INTRA_ENB` 만 하므로(2026-10-27) 그 경로에는 리셋이
    한 번도 없어야 한다. 있으면 같은 프로브가 자기가 센 것을 잊는 것이다.
    """
    runs = _run(gen, 12, shape=S.Shape(handover=1.0))
    idx = gen.index["transport"]
    i_sid, i_t = idx["SessionID"], idx["SummaryCreateTime"]
    i_ft = idx["Traffic_Total_Dnlink_FirstTime"]
    seq: dict[str, list[str]] = {}
    for m in runs:
        for r in sorted(m["up"]["transport"], key=lambda x: float(x[i_t])):
            seq.setdefault(r[i_sid], []).append(r[i_ft])
    assert seq
    for sid, ft in seq.items():
        assert len(set(ft)) == 1, (sid, ft)
