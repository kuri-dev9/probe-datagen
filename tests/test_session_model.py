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
        assert set(flags) <= {S.F_START, S.F_INTERIM, S.F_HO,
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
    ho = [v for sid, v in seq.items()
          if sid not in open_now and any(f == S.F_HO for f, _, _ in v)]
    assert ho, "핸드오버 세션이 하나도 없다"
    for v in ho:
        k = next(i for i, (f, _, _) in enumerate(v) if f == S.F_HO)
        assert k > 0, v                       # Start 가 먼저다
        # 세션이 **이어진다** — H/O 뒤에 레코드가 더 있고 끝은 End 다
        assert len(v) > k + 1, v
        assert v[k + 1][0] in (S.F_INTERIM, S.F_HO, S.F_END), v
        assert v[-1][0] == S.F_END, v
        # 같은 eNB 안에서 섹터만 바뀐다(노드 축이 세션 안에서 갈리지 않는다)
        assert v[k][1] == v[0][1], v


def test_handover_sessions_still_close(gen):
    """핸드오버를 겪은 세션도 `End` 로 닫힌다 — H/O 가 종료를 삼키지 않는다."""
    runs = _run(gen, 10, shape=S.Shape(handover=1.0))
    idx = gen.index["transport"]
    i_sid, i_flag = idx["SessionID"], idx["OngoingFlag"]
    flags: dict[str, list[str]] = {}
    for m in runs:
        for r in m["up"]["transport"]:
            flags.setdefault(r[i_sid], []).append(r[i_flag])
    open_now = {s.sid for s in runs[-1]["engine"].active}
    done = [v for sid, v in flags.items()
            if S.F_HO in v and sid not in open_now]
    assert done
    for v in done:
        assert v.count(S.F_END) == 1, v


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
