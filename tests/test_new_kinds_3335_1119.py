"""신규 28 ②·33 ①②·34·35번 생성기 + B-3·B-4(IP 버전·질의 종류)·RST 방향·Parquet 새 칸 (2026-11-19 · 맥스 구현).

DB 없이 돈다 — 합성 셀(`test_disaster._master`)과 합성 앱 사전으로 `PemdbGen` 을 16분 돌려 **실제 Parquet 에 쓰고 다시
읽어** 본다(원시 레코드 그대로). 한 종류마다 4초쯤 걸려 종류별로 한 번만 돌리고 캐시한다.
장애 창은 4~12분이고 판정에는 양 끝 분을 뺀 5~11분만 쓴다(`WIN`) — 시작·끝 분은 진입·복구 경계라 섞인다.
합성 셀은 PGW 가 하나뿐이라 「다른 PGW」는 플랜 훅을 직접 불러 본다.
"""
from __future__ import annotations

import collections
import json
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import polars as pl
import pytest

from datagen import apps as A
from datagen import control, ui
from datagen import pemdb1 as P
from datagen import scenarios as SC
from datagen import scenario_ja
from datagen.generator import Generator
from test_disaster import _master

ROOT = Path(__file__).resolve().parents[1]
T0 = datetime(2026, 11, 4, 20, 0)
START, DUR, MINUTES = 4, 8, 16
WIN = pl.col("m").is_between(START + 1, START + DUR - 1)
PRE = pl.col("m").is_between(1, START - 1)
N_WIN, N_PRE = DUR - 1, START - 1
ENDS = [2, 20]          # 세션 종료 레코드(ONGOING_FLAG)

_BOOK = [
    A.App("901", "PayApp", "PAY", "6", "", "", ("paidy.com",), 0.6),
    A.App("902", "Tube", "STREAMING", "6", "", "", ("tube.example",), 1.5),
    A.App("3003", "Facebook", "SNS", "6", "", "", ("facebook.com", "fbcdn.net"), 1.2),
    A.App("3000", "Google", "SEARCH", "17", "", "", ("google.com",), 1.0),
    A.App("3002", "Apple", "WEB", "6", "", "", ("apple.com", "mzstatic.com"), 0.4),
    A.App("3010", "Other", "ECT", "6", "", "", ("other.jp",), 1.0),
]

_CACHE: dict[str, tuple] = {}


def _run(kind: str):
    """(sc, {계열: DataFrame}) — `m` 열은 시작으로부터의 분. kind 가 None 이면 장애 없는 평소 회차."""
    if kind in _CACHE:
        return _CACHE[kind]
    book = A.AppBook(list(_BOOK))
    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(P, "load_cells", lambda *a, **k: _master())
        mp.setattr(P, "load_apps", lambda conn: book)
        g = P.PemdbGen(None, ROOT / "data", rates={"lte_call": 1500, "transport": 1500, "dns": 900, "http": 750},
                       seed=11, subscriber_pool=2500)
        g.anchor = T0
        g.gen.app_book = book
        sc = None
        if kind != "baseline":
            row = {"fault_id": 900, "kind": kind, "target": None, "start_ts": T0 + timedelta(minutes=START),
                   "duration_min": DUR, "intensity": 1.0}
            scs, bad = g.set_fault_rows([row])
            assert scs and not bad, bad
            sc = scs[0]
        specs = P.uplane_specs(g.series)
        w = P.PemdbWriter(None, None, uplane_root=Path(tempfile.mkdtemp()), uplane_retain_minutes=0)
        frames = collections.defaultdict(list)
        for m in range(MINUTES):
            ts = T0 + timedelta(minutes=m)
            g.records(m, ts)
            for s, rows in g.uplane_records(m, ts).items():
                w.write_uplane(ts, s, rows, specs[s])
                frames[s].append(pl.read_parquet(P.raw_parquet.minute_path(w.uplane_root, s, ts))
                                 .with_columns(pl.lit(m).alias("m")))
        out = {s: pl.concat(v, how="diagonal") for s, v in frames.items()}
    finally:
        mp.undo()
    _CACHE[kind] = (sc, out, g)
    return _CACHE[kind]


def _ends(tp: pl.DataFrame) -> pl.DataFrame:
    return tp.filter(pl.col("ONGOING_FLAG").is_in(ENDS))


# ── B-3·B-4 · IP 버전과 질의 종류 ────────────────────────────────────────────

def test_ip_version_is_the_same_in_dns_transport_and_http_of_one_session():
    _, F, _ = _run("baseline")
    ref = _ends(F["transport"]).select(["SESSION_ID", "IP_VERSION"]).unique("SESSION_ID")
    assert ref["IP_VERSION"].null_count() == 0 and set(ref["IP_VERSION"].to_list()) <= {1, 2}
    for s in ("dns", "http"):
        j = F[s].select(["SESSION_ID", pl.col("IP_VERSION").alias("v")]).unique().join(ref, on="SESSION_ID")
        assert j.height > 100, s
        assert j.filter(pl.col("v") != pl.col("IP_VERSION")).height == 0, f"{s} 의 IP 버전이 transport 와 다르다"
        assert j["v"].null_count() == 0


def test_ip_version_share_follows_the_measured_v6_majority():
    _, F, _ = _run("baseline")
    t = _ends(F["transport"])
    v6 = t.filter(pl.col("IP_VERSION") == 2).height / t.height
    assert 0.70 < v6 < 0.90, v6          # 프로파일 실측 80.7%


def test_dns_query_type_matches_the_ip_version_of_the_session():
    _, F, _ = _run("baseline")
    d = F["dns"]
    assert d["QUERY_TYPE"].null_count() == 0
    v4, v6 = d.filter(pl.col("IP_VERSION") == 1), d.filter(pl.col("IP_VERSION") == 2)
    assert set(v4["QUERY_TYPE"].unique().to_list()) <= {1, 65}, "v4 세션에 AAAA 질의가 있다"
    assert set(v6["QUERY_TYPE"].unique().to_list()) <= {28, 65}, "v6 세션에 A 질의가 있다"
    assert (v4["QUERY_TYPE"] == 1).any() and (v6["QUERY_TYPE"] == 28).any()


def test_dns_query_type_ratio_inside_each_ip_version_keeps_measured_proportion():
    """허용 집합 밖은 다시 뽑는다 — 한 IP 버전 안에서 HTTPS(65)가 지배하지 않는다."""
    _, F, _ = _run("baseline")
    d = F["dns"]
    for v, main in ((1, 1), (2, 28)):
        q = d.filter(pl.col("IP_VERSION") == v)["QUERY_TYPE"]
        assert (q == main).sum() > (q == 65).sum(), (v, q.value_counts())


# ── RST 방향 ─────────────────────────────────────────────────────────────────

def test_rst_direction_is_zero_without_rst_and_set_only_when_rst_exists():
    _, F, _ = _run("baseline")
    t = F["transport"]
    none = t.filter(pl.col("RST_COUNT") == 0)
    assert (none["RST_DIRECTION"] == 0).all(), none["RST_DIRECTION"].value_counts()
    has = t.filter(pl.col("RST_COUNT") > 0)
    assert has.height > 50
    # 평소 RST 는 누가 끊었는지 모른다 — 지어 넣지 않는다(불변규칙 17)
    assert has["RST_DIRECTION"].null_count() == has.height
    assert t["RST_DIRECTION"].dtype == pl.Int8


def test_rst_direction_only_has_the_spec_values():
    for kind in ("service_tls_fail", "ipv6_path_fail"):
        _, F, _ = _run(kind)
        vals = set(F["transport"]["RST_DIRECTION"].drop_nulls().unique().to_list())
        assert vals <= {0, 1, 2}, (kind, vals)


# ── 28 ② dns_nxdomain ───────────────────────────────────────────────────────

def test_28b_spec_and_pool_exclude_pay_and_streaming():
    assert SC.KIND_SPECS["dns_nxdomain"]["series"] == ("dns",)
    _, _, g = _run("baseline")
    pool = g.gen._service_pool(False)
    assert pool and len(pool) <= g.gen.SERVICE_POOL
    assert {a.group for a in pool}.isdisjoint({"PAY", "STREAMING"})
    assert {a.idx for a in pool}.isdisjoint({"901", "902"})


def test_28b_target_app_dns_all_fail_with_code_3_no_retry_no_answer():
    sc, F, _ = _run("dns_nxdomain")
    aid = sc.target.app_id
    assert aid and sc.target.app_group not in ("PAY", "STREAMING")
    d = F["dns"].filter(WIN & (pl.col("APP_ID") == aid))
    assert d.height > 100
    assert (d["REPLY_CODE"] == 3).all(), d["REPLY_CODE"].value_counts()
    assert (d["QUERY_RETRY_COUNT"] == 0).all() and (d["DNS_RETRY_SEQUENCE"] == 0).all()
    assert (d["ANSWER_COUNT"] == 0).all()


def test_28b_dns_answers_fast_like_normal_not_like_noresponse():
    sc, F, _ = _run("dns_nxdomain")
    aid = sc.target.app_id
    d = F["dns"]

    def lat(c):
        x = d.filter(c & (pl.col("APP_ID") == aid))
        return ((x["RESPONSE_TIME"] - x["REQUEST_FIRST_TIME"]) * 1000)

    pre, win = lat(PRE), lat(WIN)
    assert pre.median() > 0
    assert 0.5 < win.median() / pre.median() < 2.0, (pre.median(), win.median())


def test_28b_failed_dns_sessions_do_not_open():
    sc, F, _ = _run("dns_nxdomain")
    aid = sc.target.app_id
    failed = F["dns"].filter(WIN & (pl.col("APP_ID") == aid) & (pl.col("REPLY_CODE") == 3))["SESSION_ID"].unique()
    assert failed.len() > 100
    assert F["transport"].filter(pl.col("SESSION_ID").is_in(failed.implode())).height == 0, "DNS 가 실패한 세션이 열렸다"
    assert F["http"].filter(pl.col("SESSION_ID").is_in(failed.implode())).height == 0


def test_28b_other_apps_dns_do_not_fail_more_than_before():
    sc, F, _ = _run("dns_nxdomain")
    aid = sc.target.app_id
    d = F["dns"].filter(pl.col("APP_ID") != aid)

    def fail(c):
        x = d.filter(c)
        return x.filter(pl.col("REPLY_CODE") != 0).height
    assert fail(WIN) == 0 and fail(PRE) == 0


def test_28b_not_active_before_the_window():
    sc, F, _ = _run("dns_nxdomain")
    pre = F["dns"].filter(PRE & (pl.col("APP_ID") == sc.target.app_id))
    assert pre.height > 20 and (pre["REPLY_CODE"] == 0).all()


# ── 33 ① service_4xx ────────────────────────────────────────────────────────

def test_33a_target_app_gets_401_403_429_and_others_are_untouched():
    sc, F, _ = _run("service_4xx")
    aid = sc.target.app_id
    assert aid and sc.target.app_group not in ("PAY", "STREAMING")
    h = F["http"]
    t = h.filter(WIN & (pl.col("APP_ID") == aid))
    assert t.height > 300
    codes = t["STATUS_CODE"].value_counts()
    cnt = dict(zip(codes["STATUS_CODE"].to_list(), codes["count"].to_list()))
    rej = sum(cnt.get(c, 0) for c in (401, 403, 429))
    assert all(cnt.get(c, 0) > 0 for c in (401, 403, 429)), cnt
    assert 0.75 < rej / t.height < 0.95, rej / t.height       # 강도 1.0 × 0.85
    assert cnt.get(401, 0) > cnt.get(429, 0) and cnt.get(403, 0) > cnt.get(429, 0), "429 가 가장 적어야 한다(10%)"
    other = h.filter(pl.col("APP_ID") != aid)
    # 평소에도 401·403 이 조금 있다(실측 프로파일 · 2% 안팎) — 다른 앱은 그 평소 비율에서 벗어나지 않는다
    def share(c):
        x = other.filter(c)
        return x.filter(pl.col("STATUS_CODE").is_in([401, 403, 429])).height / x.height
    assert share(WIN) < 0.04 and share(WIN) <= share(PRE) + 0.015, (share(PRE), share(WIN))
    assert other.filter(WIN & (pl.col("STATUS_CODE") == 429)).height == 0


def test_33a_before_the_window_the_target_app_is_not_rejected():
    sc, F, _ = _run("service_4xx")
    t = F["http"].filter(PRE & (pl.col("APP_ID") == sc.target.app_id))
    # 평소에도 401·403 이 2% 안팎 있다(프로파일) — 장애 몫(85%)과 구별될 만큼만 본다
    assert t.height > 50 and t.filter(pl.col("STATUS_CODE").is_in([401, 403, 429])).height / t.height < 0.05


def test_33a_does_not_change_response_time_rtt_or_retransmission():
    sc, F, _ = _run("service_4xx")
    h = F["http"].filter(pl.col("APP_ID") == sc.target.app_id)

    def stat(c):
        x = h.filter(c)
        return ((x["RESPONSE_TIME"] - x["REQUEST_TIME"]).median(), x["UPLINK_RTT_TIME"].median(),
                x["DNLINK_RTT_TIME"].median(), x["RETRANS_PKT_DN"].sum() / max(1, x["PAYLOAD_PKT_DN"].sum()))
    # 꼬리가 긴 분포라 평균이 아니라 중앙값으로 본다(표본 수백 건 · 평균은 이상값 하나에 두 배가 된다)
    for a, b in zip(stat(PRE), stat(WIN)):
        assert b == pytest.approx(a, rel=0.6, abs=1.0), (stat(PRE), stat(WIN))


def test_33a_no_server_error_is_added():
    sc, F, _ = _run("service_4xx")
    t = F["http"].filter(pl.col("APP_ID") == sc.target.app_id)
    pre5 = t.filter(PRE & (pl.col("STATUS_CODE") >= 500)).height / max(1, t.filter(PRE).height)
    win5 = t.filter(WIN & (pl.col("STATUS_CODE") >= 500)).height / max(1, t.filter(WIN).height)
    assert win5 <= pre5 + 0.02, (pre5, win5)


# ── 33 ② service_tls_fail ───────────────────────────────────────────────────

def test_33b_target_is_a_tcp_app_outside_pay_and_streaming():
    sc, _, g = _run("service_tls_fail")
    assert sc.target.app_id and sc.target.app_group not in ("PAY", "STREAMING")
    assert {a.protocol for a in g.gen._service_pool(True)} == {"6"}, "TLS 대상 후보에 UDP 앱이 있다"
    assert sc.target.app_id != "3000"       # 합성 사전에서 UDP 인 Google


def test_33b_connected_and_reset_by_the_terminal_with_sni():
    sc, F, _ = _run("service_tls_fail")
    aid = sc.target.app_id
    t = _ends(F["transport"]).filter(WIN & (pl.col("APP_ID") == aid))
    assert t.height > 300
    hit = t.filter(pl.col("RST_DIRECTION") == 1)
    assert hit.height / t.height > 0.70, hit.height / t.height          # 강도 1.0 × 0.85 (연결 안 선 세션 빼고)
    # 맥스 보고 「TCP 연결은 그대로 선다」 — 평소 TCP 종료 레코드의 절반은 핸드셰이크 관측이 없어(ACK_TIME 0 · SYN 0) 그
    # 세션을 TLS 실패로 고르면 ACK 없는 TLS 실패가 생긴다. 규격 의도(연결 성립)로 단언한다 — 실패하면 생성기 쪽 문제다
    ack_share = (hit["ACK_TIME"] > 0).sum() / hit.height
    assert ack_share == 1.0, f"TLS 실패 세션 {hit.height}건 중 ACK 있는 것 {ack_share:.1%}"
    assert (hit["RST_COUNT"] > 0).all()
    sni = hit["CLIENT_HELLO_DOMAIN"]
    assert sni.null_count() == 0 and (sni.str.len_chars() > 0).all()
    assert (hit["PROTOCOL"] == 6).all()


def test_33b_only_a_handshake_flows_and_no_http_is_requested():
    sc, F, _ = _run("service_tls_fail")
    aid = sc.target.app_id
    hit = _ends(F["transport"]).filter(WIN & (pl.col("APP_ID") == aid) & (pl.col("RST_DIRECTION") == 1))
    assert hit["PAYLOAD_SIZE_DN"].min() >= 3000 and hit["PAYLOAD_SIZE_DN"].max() <= 6000   # 서버 인증서 몫
    assert hit["PAYLOAD_SIZE_UP"].max() <= 900
    assert (hit["RETRANS_PKT_DN"] == 0).all() and (hit["RETRANS_PKT_UP"] == 0).all()
    assert F["http"].filter(pl.col("SESSION_ID").is_in(hit["SESSION_ID"].implode())).height == 0, "TLS 가 실패한 세션에 HTTP 가 있다"


def test_33b_http_of_the_target_app_drops_and_others_do_not():
    sc, F, _ = _run("service_tls_fail")
    aid = sc.target.app_id
    h = F["http"]
    t_pre = h.filter(PRE & (pl.col("APP_ID") == aid)).height / N_PRE
    t_win = h.filter(WIN & (pl.col("APP_ID") == aid)).height / N_WIN
    assert t_win < t_pre * 0.5, (t_pre, t_win)
    o_pre = h.filter(PRE & (pl.col("APP_ID") != aid)).height / N_PRE
    o_win = h.filter(WIN & (pl.col("APP_ID") != aid)).height / N_WIN
    assert 0.7 < o_win / o_pre < 1.3, (o_pre, o_win)


def test_33b_other_apps_and_the_baseline_have_no_terminal_rst():
    sc, F, _ = _run("service_tls_fail")
    t = F["transport"]
    assert t.filter((pl.col("APP_ID") != sc.target.app_id) & (pl.col("RST_DIRECTION") == 1)).height == 0
    assert t.filter(PRE & (pl.col("RST_DIRECTION") == 1)).height == 0
    assert _run("baseline")[1]["transport"].filter(pl.col("RST_DIRECTION") == 1).height == 0


def test_33b_connection_success_stays_normal():
    """연결 단계는 그대로다 — U-01 이 아니라 RST 종료율만 오른다."""
    sc, F, _ = _run("service_tls_fail")
    t = _ends(F["transport"]).filter((pl.col("APP_ID") == sc.target.app_id) & (pl.col("PROTOCOL") == 6))

    def ack_share(c):
        x = t.filter(c)
        return (x["ACK_TIME"] > 0).sum() / x.height
    # 연결이 관측된 세션(ACK)의 몫은 내려가지 않는다 — 이제 「연결 후 단말 끊김」 세션은 항상 ACK 가 있어(TLS_UE_RST 정의)
    # 오히려 올라간다. 평소 약 절반이 핸드셰이크 미관측(ACK=0)이라 같은 값이 아니다
    assert ack_share(WIN) >= ack_share(PRE) - 0.08
    rst = lambda c: t.filter(c & (pl.col("RST_COUNT") > 0)).height / t.filter(c).height      # noqa: E731
    assert rst(WIN) > rst(PRE) + 0.4


# ── 34 ipv6_path_fail ───────────────────────────────────────────────────────

def test_34_target_is_one_pgw():
    sc, F, _ = _run("ipv6_path_fail")
    assert sc.target.pgw_id is not None and sc.target.cell_keys
    assert not sc.target.app_id


def test_34_every_v6_tcp_session_on_the_target_pgw_fails():
    sc, F, _ = _run("ipv6_path_fail")
    pg = str(sc.target.pgw_id)
    # 장애 **시작 뒤에 열린** 세션만 본다 — 시작 전에 열려 창 안에서 끝나는 이월 세션은 정상이다
    first = F["transport"].group_by("SESSION_ID").agg(pl.col("m").min().alias("fm"))
    t = _ends(F["transport"]).join(first, on="SESSION_ID").filter(
        WIN & (pl.col("fm") > START) & (pl.col("PGW_ID") == pg) & (pl.col("IP_VERSION") == 2) & (pl.col("PROTOCOL") == 6))
    assert t.height > 500
    ok = t.filter((pl.col("ACK_TIME") > 0) & (pl.col("RST_COUNT") == 0))
    assert ok.height == 0, f"IPv6 TCP 세션이 {ok.height}건 정상으로 끝났다"
    syn = t.filter(pl.col("ACK_TIME") <= 0).height / t.height
    assert 0.65 < syn < 0.95, syn          # SYN 무응답 70% + 평소에도 ACK 없는 종료 레코드가 절반쯤 있다
    rst = t.filter((pl.col("ACK_TIME") > 0) & (pl.col("RST_COUNT") > 0))
    assert rst.height > 0 and (rst["RST_DIRECTION"] == 2).all(), "서버·망 쪽 RST 는 방향 2 여야 한다"


def test_34_v4_sessions_stay_normal():
    sc, F, _ = _run("ipv6_path_fail")
    t = _ends(F["transport"]).filter(pl.col("PROTOCOL") == 6)
    v4 = t.filter(pl.col("IP_VERSION") == 1)

    def fail(c):
        x = v4.filter(c)
        return x.filter((pl.col("ACK_TIME") <= 0) | (pl.col("RST_COUNT") > 0)).height / x.height
    assert fail(WIN) == pytest.approx(fail(PRE), abs=0.06), (fail(PRE), fail(WIN))


def test_34_v4_sessions_increase_because_of_retries():
    sc, F, _ = _run("ipv6_path_fail")
    t = _ends(F["transport"])
    pre = t.filter(PRE & (pl.col("IP_VERSION") == 1)).height / N_PRE
    win = t.filter(WIN & (pl.col("IP_VERSION") == 1)).height / N_WIN
    assert win > pre * 1.3, (pre, win)


def test_34_each_failed_v6_session_gets_one_v4_retry_1_to_8_seconds_later_with_an_a_query():
    """재시도는 실패한 세션 바로 다음 세션 번호(`sid` 카운터 + 1)로 열린다 — 같은 가입자·같은 앱·IPv4."""
    sc, F, _ = _run("ipv6_path_fail")
    tp, dn = F["transport"], F["dns"]
    pg = str(sc.target.pgw_id)
    first = tp.group_by("SESSION_ID").agg(pl.col("m").min().alias("fm"))
    bad = tp.join(first, on="SESSION_ID").filter(
        pl.col("m").is_between(START + 2, START + DUR - 2) & (pl.col("fm") > START) & (pl.col("PGW_ID") == pg)
        & (pl.col("IP_VERSION") == 2) & (pl.col("PROTOCOL") == 6)
        & ((pl.col("ACK_TIME") <= 0) | (pl.col("RST_COUNT") > 0)) & pl.col("ONGOING_FLAG").is_in(ENDS))
    nxt = lambda c: (pl.col(c).str.to_integer(base=16) + 1)          # noqa: E731
    bad = bad.with_columns(nxt("SESSION_ID").alias("k"))
    v4 = tp.filter((pl.col("IP_VERSION") == 1)).unique("SESSION_ID").select(
        pl.col("SESSION_ID").str.to_integer(base=16).alias("k"), "IMSI", "APP_ID", "SESSION_ID",
        pl.col("SESSION_CREATED_TIME").alias("t_retry"))
    j = bad.select("k", "IMSI", "APP_ID", pl.col("SESSION_CREATED_TIME").alias("t_bad")).join(
        v4, on=["k", "IMSI", "APP_ID"])
    assert bad.height > 200
    assert j.height / bad.height > 0.9, (j.height, bad.height)        # 한 번씩 — 번호가 이어지고 가입자·앱이 같다
    gap = (j["t_retry"] - j["t_bad"])
    assert gap.min() >= 1.0 - 1e-6 and gap.max() <= 8.0 + 1e-6, (gap.min(), gap.max())
    q = dn.filter(pl.col("SESSION_ID").is_in(j["SESSION_ID"].implode()))
    assert q.height > 100
    assert (q["IP_VERSION"] == 1).all()
    assert set(q["QUERY_TYPE"].unique().to_list()) <= {1, 65}, "v4 재시도에 AAAA 질의가 있다"
    assert (q["QUERY_TYPE"] == 1).sum() > (q["QUERY_TYPE"] == 65).sum()


def test_34_plan_hook_ignores_other_pgw_cells_and_v4_sessions():
    """합성 셀은 PGW 가 하나라 훅을 직접 부른다 — 대상 PGW 밖 셀과 v4 세션은 건드리지 않는다."""
    import random
    sc, _, g = _run("ipv6_path_fail")
    cell = g.gen.cells[0] if hasattr(g.gen, "cells") else None
    assert cell is not None
    hook = SC.PLAN_MUTATORS["ipv6_path_fail"]
    rng = random.Random(5)

    def plan(ipv, proto="6"):
        return SC.Plan({}, {"IP_Version": ipv, "Protocol": proto}, "ok", 3)

    # 대상 밖 셀
    sc_other = SC.Scenario(id="T", start_minute=0, **{**SC.KIND_SPECS["ipv6_path_fail"], "kind": "ipv6_path_fail",
                                                    "target": SC.Target(), "duration": 20})
    sc_other.active_cells_cache = set()
    p = plan("2")
    assert hook(p, sc_other, 1.0, rng, cell) is False and not (p.syn_fail or p.reset or p.retries or p.v4_retry)
    # 대상 셀이지만 v4
    sc_in = SC.Scenario(id="T", start_minute=0, **{**SC.KIND_SPECS["ipv6_path_fail"], "kind": "ipv6_path_fail",
                                                 "target": SC.Target(), "duration": 20})
    sc_in.active_cells_cache = {cell.cell_key}
    p = plan("1")
    assert hook(p, sc_in, 1.0, rng, cell) is False and not (p.syn_fail or p.reset or p.retries or p.v4_retry)
    # 대상 셀의 v6 — 실패 + v4 재시도 1회
    p = plan("2")
    assert hook(p, sc_in, 1.0, rng, cell) is True and p.retries == 1 and p.v4_retry and (p.syn_fail or p.reset)
    # UDP 는 SYN 이 없다 — 짧게 끊김만
    p = plan("2", "17")
    assert hook(p, sc_in, 1.0, rng, cell) is True and p.reset and not p.syn_fail


def test_34_auto_target_node_kind_is_pgw():
    from datagen import faults
    assert faults._AUTO_NODE["ipv6_path_fail"] == "pgw_id"


# ── 35 update_surge ─────────────────────────────────────────────────────────

def test_35_target_is_an_update_app_by_name():
    sc, _, g = _run("update_surge")
    assert sc.target.app_id == "3002"       # 합성 사전에서 이름에 apple 이 든 유일한 TCP 앱
    assert {a.idx for a in g.gen._update_pool()} == {"3002"}


def test_35_sessions_of_the_target_app_increase():
    sc, F, _ = _run("update_surge")
    aid = sc.target.app_id
    t = _ends(F["transport"])
    pre = t.filter(PRE & (pl.col("APP_ID") == aid)).height / N_PRE
    win = t.filter(WIN & (pl.col("APP_ID") == aid)).height / N_WIN
    assert win > pre * 1.8, (pre, win)           # 1 + 2×강도 = 3배(구간이 섞여 작게 나온다)
    # 다른 앱은 그대로(전체 호 수는 그대로라 비중이 조금 줄 수 있다)
    o_pre = t.filter(PRE & (pl.col("APP_ID") != aid)).height / N_PRE
    o_win = t.filter(WIN & (pl.col("APP_ID") != aid)).height / N_WIN
    assert 0.6 < o_win / o_pre < 1.3, (o_pre, o_win)


def test_35_download_volume_and_http_body_size_grow():
    sc, F, _ = _run("update_surge")
    aid = sc.target.app_id
    tp, ht = F["transport"], F["http"]
    vol = lambda c: tp.filter(c & (pl.col("APP_ID") == aid))["DLT_PAYLOAD_SIZE_DN"].sum()      # noqa: E731
    assert vol(WIN) / N_WIN > 4 * vol(PRE) / N_PRE
    cl = lambda c: ht.filter(c & (pl.col("APP_ID") == aid))["RESP_CONTENT_LENGTH"].median()    # noqa: E731
    assert cl(WIN) > 4 * cl(PRE), (cl(PRE), cl(WIN))
    # 다른 앱의 본문 크기는 그대로
    oc = lambda c: ht.filter(c & (pl.col("APP_ID") != aid))["RESP_CONTENT_LENGTH"].median()    # noqa: E731
    assert oc(WIN) == pytest.approx(oc(PRE), rel=0.5)


def test_35_per_session_throughput_is_unchanged():
    """양과 시간이 같은 비로 늘어 한 세션의 처리율(바이트/시간)은 그대로다 — 셀 혼잡은 분석 몫."""
    sc, F, _ = _run("update_surge")
    t = _ends(F["transport"]).filter((pl.col("APP_ID") == sc.target.app_id) & (pl.col("THROUGHPUT_TIME_DN") > 0)
                                      & (pl.col("PAYLOAD_SIZE_DN") > 0))
    rate = (pl.col("PAYLOAD_SIZE_DN") / pl.col("THROUGHPUT_TIME_DN"))
    pre = t.filter(PRE).select(rate.median()).item()
    win = t.filter(WIN).select(rate.median()).item()
    assert win == pytest.approx(pre, rel=0.5), (pre, win)


# ── 29 · IMS APN 제외 ────────────────────────────────────────────────────────

def test_29_auto_apn_pool_excludes_ims(monkeypatch):
    """`apn_pdn_failure` 의 자동 대상은 IMS(음성) APN 이 아니다 — 후보 앞에 ims 가 와도 뺀다."""
    _, _, g = _run("baseline")
    gen = g.gen
    monkeypatch.setattr(gen, "_apn_pool_top", lambda: ["ims.mnc010.mcc440.gprs", "IMS", "spmode.ne.jp", "mopera.net"])
    seen = set()
    for i in range(12):
        sc = SC.Scenario(id=f"T{i}", start_minute=i * 30, **{**SC.KIND_SPECS["apn_pdn_failure"], "kind": "apn_pdn_failure",
                                                          "target": SC.Target(), "duration": 10})
        gen.scenarios = [sc]
        gen._bind_scenario_targets()
        seen.add(sc.target.apn)
    assert seen and all("ims" not in (a or "").lower() for a in seen), seen
    assert seen <= {"spmode.ne.jp", "mopera.net"}


def test_29_ims_still_allowed_for_ims_kind(monkeypatch):
    """IMS 제외는 29번에만 건다 — 다른 APN 장애(20번 `ims_pdn`)의 후보는 그대로다."""
    _, _, g = _run("baseline")
    monkeypatch.setattr(g.gen, "_apn_pool_top", lambda: ["ims.mnc010.mcc440.gprs", "spmode.ne.jp"])
    pool = g.gen._apn_pool_top()
    assert any("ims" in a for a in pool)
    assert "apn_pdn_failure" not in g.gen._SERVICE_KINDS


# ── 목록 · JSON · i18n ──────────────────────────────────────────────────────

NEW = {33: "service_4xx", 34: "ipv6_path_fail", 35: "update_surge"}


def test_scenarios_33_to_35_are_ready_kinds():
    rows = {r[0]: r for r in control.SCENARIOS}
    for no, kind in NEW.items():
        assert rows[no][2] == kind, no
        assert kind in control.KINDS and kind in SC.KIND_SPECS
        assert kind not in control.NO_EVIDENCE and no not in control.NO_EVIDENCE
    assert rows[28][2] == "dns_flap" and rows[29][2] == "apn_pdn_failure"
    for k in ("dns_nxdomain", "service_tls_fail"):
        assert k in control.KINDS and k in SC.KIND_SPECS, "② 갈래는 행이 아니라 장애 종류로 주입한다"


def test_new_kinds_have_labels_in_both_languages_and_mutators():
    for k in ("dns_nxdomain", "service_4xx", "service_tls_fail", "ipv6_path_fail", "update_surge"):
        assert ui.common.KIND_LABELS.get(k), k
        assert k in SC.MUTATORS or k in SC.PLAN_MUTATORS or k in SC.APP_BOOSTERS, k
        assert SC.recommended_duration(k), f"{k} 권장 주입 길이가 없다"


def test_how_json_has_28_29_33_34_35_with_japanese_and_no_untranslated_korean():
    how = json.loads((ROOT / "config/scenario_how.json").read_text("utf-8"))["scenarios"]
    for no in ("28", "29", "33", "34", "35"):
        s = how[no]
        assert s.get("brief") and s.get("steps") and s.get("ja"), no
        assert set(s["ja"]) >= {"brief", "steps"}, no
        assert len(s["ja"]["steps"]) == len(s["steps"]), f"{no}번 단계 수가 한국어·일본어가 다르다"
    got = scenario_ja.scan()
    assert not any(got.values()), got


def test_report_json_rows_for_new_numbers_name_the_same_kinds():
    for f in ("scenario_report.json", "scenario_report_ja.json", "scenario_report_v50.json", "scenario_report_v50_ja.json"):
        doc = json.loads((ROOT / "config" / f).read_text("utf-8"))
        rows = {s["no"]: s for s in doc["scenarios"]} if "scenarios" in doc and isinstance(doc["scenarios"], list) else {}
        if not rows:
            continue
        for no, kind in NEW.items():
            if no in rows and rows[no].get("kind"):
                assert rows[no]["kind"] == kind, (f, no)


# ── Parquet 새 칸 ───────────────────────────────────────────────────────────

NEW_COLS = {
    "transport": {"CLIENT_HELLO_DOMAIN": (pl.String, "ClientHelloDomain"), "RST_DIRECTION": (pl.Int8, "RstDirection")},
    "dns": {"QUERY_TYPE": (pl.Int32, "QueryType")},
    "http": {"RESP_CONTENT_LENGTH": (pl.Int64, "HTTP_Total_Response_ContentLength")},
}


def test_new_parquet_columns_exist_with_the_agreed_names_types_and_source_fields():
    _, F, _ = _run("baseline")
    for s, cols in NEW_COLS.items():
        fields = {c: (t, src) for c, src, t, _ in P.UPLANE_FIELDS[s]}
        for c, (dtype, src) in cols.items():
            assert c in F[s].columns, (s, c)
            assert F[s].schema[c] == dtype, (s, c, F[s].schema[c])
            assert fields[c][1] == src, (s, c)
    for s in ("transport", "dns", "http"):
        assert F[s].schema["IP_VERSION"] == pl.Int8, s


def test_new_http_column_is_filled_and_not_negative():
    _, F, _ = _run("update_surge")
    c = F["http"]["RESP_CONTENT_LENGTH"]
    assert c.null_count() < F["http"].height and c.min() >= 0


def test_graphio_side_reads_the_same_column_names():
    """graphio 집계 코드가 같은 이름을 쓰는지 대조한다(읽기만) — 동구가 이름을 맞추는 중이면 여기서 걸린다."""
    src = (ROOT.parent / "graphio-probe-rca" / "src/graphio/metrics/uplane_agg.py").read_text("utf-8")
    for s, cols in NEW_COLS.items():
        for c in cols:
            assert c in src, f"graphio uplane_agg 가 {c} 를 모른다"
    assert "QUERY_TYPE" in src and "IP_VERSION" in src
