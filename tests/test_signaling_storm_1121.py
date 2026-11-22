"""36번 신호 폭주 생성기 — `scenarios._signaling_storm` · `STORM_*` · `surge_mix` · 등록 (2026-11-21 · 맥스).

DB 없이 돈다 — 합성 셀(`test_disaster._master`)로 `PemdbGen` 을 14분 돌려 lte_call 레코드를 그대로 본다.
합성 마스터는 MME 가 하나(0001)라, 「대상 MME 밖」은 대상 MME 를 다른 값으로 바꿔 한 번 더 돌려 본다.
장애는 4분에 시작하고 경과 e = 분 − 4 이다. 단계: ① 폭주 e=0 · ② MME 과부하 e≥1 · ③ 인증 무응답 e≥2 · ④ IMS 거절 e≥3.
"""
from __future__ import annotations

import collections
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from datagen import apps as A
from datagen import control
from datagen import pemdb1 as P
from datagen import scenarios as SC
from datagen.ui import common, scenarios_api
from test_disaster import _master

ROOT = Path(__file__).resolve().parents[1]
T0 = datetime(2026, 11, 21, 20, 0)
START, DUR, MINUTES = 4, 20, 14

_BOOK = [A.App("3000", "Google", "SEARCH", "17", "", "", ("google.com",), 1.0),
         A.App("3010", "Other", "ECT", "6", "", "", ("other.jp",), 1.0)]
_CACHE: dict = {}


def _run(kind="signaling_storm", other_mme=False):
    key = (kind, other_mme)
    if key in _CACHE:
        return _CACHE[key]
    book = A.AppBook(list(_BOOK))
    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(P, "load_cells", lambda *a, **k: _master())
        mp.setattr(P, "load_apps", lambda conn: book)
        g = P.PemdbGen(None, ROOT / "data", rates={"lte_call": 1500, "transport": 1500, "dns": 900, "http": 750},
                       seed=11, subscriber_pool=2500)
        g.anchor = T0
        g.gen.app_book = book
        row = {"fault_id": 900, "kind": kind, "target": None, "start_ts": T0 + timedelta(minutes=START),
               "duration_min": DUR, "intensity": 1.0}
        scs, bad = g.set_fault_rows([row])
        assert scs and not bad, bad
        sc = scs[0]
        if other_mme:
            sc.target.mme_id = "9999"                     # 합성 셀의 MME 는 0001 — 대상 밖으로 만든다
            sc.target.mme_group = {"9999"}
        idx = g.gen.index["lte_call"]
        stats = {}
        for m in range(MINUTES):
            rows = g.records(m, T0 + timedelta(minutes=m))
            c = collections.Counter()
            for r in rows:
                c["n"] += 1
                if r[idx["auth_attempt_flag"]] == "1":
                    c["auth"] += 1
                if str(r[idx["AuthenticationInformation_Cause"]]) == "900":
                    c["auth900"] += 1
                if str(r[idx["esm_error_Cause"]]) == "26":
                    c["ims26"] += 1
                if str(r[idx["first_error_cause"]]) == "500":
                    c["ctx500"] += 1
                if r[idx["call_type"]] in ("1", "5"):
                    c["att_tau"] += 1
            stats[m] = c
    finally:
        mp.undo()
    _CACHE[key] = (sc, stats)
    return _CACHE[key]


E_ = lambda e: START + e        # noqa: E731 — 경과 분 → 분 번호


# ---------------------------------------------------------------- ① 단계별 시각

def test_nothing_fails_before_the_start_and_at_e0_only_calls_are_added():
    _sc, st = _run()
    for m in range(0, START):
        assert not (st[m]["auth900"] or st[m]["ims26"] or st[m]["ctx500"]), m
    s0 = st[E_(0)]
    assert s0["n"] > 2 * st[START - 1]["n"], "e=0 부터 폭주 호가 더해진다"
    assert not (s0["auth900"] or s0["ims26"] or s0["ctx500"]), "e=0 은 폭주뿐 — 실패 단계는 아직 없다"


def test_the_stages_open_one_minute_apart_mme_then_auth_then_ims():
    _sc, st = _run()
    assert st[E_(0)]["ctx500"] == 0 and st[E_(1)]["ctx500"] > 0                       # ② MME 과부하 +1분
    assert st[E_(1)]["auth900"] == 0 and st[E_(2)]["auth900"] > 0                     # ③ 인증 무응답 +2분
    assert st[E_(2)]["ims26"] == 0 and st[E_(3)]["ims26"] > 0                         # ④ IMS 거절(ESM 26) +3분
    for m in range(E_(3), MINUTES):
        assert st[m]["ctx500"] and st[m]["auth900"] and st[m]["ims26"], m             # 이후 세 갈래가 함께 이어진다


def test_the_stage_offsets_are_the_pinned_values():
    assert SC.STORM_STAGE_AT == {"mme": 1, "auth": 2, "ims": 3}


# ---------------------------------------------------------------- ② 폭주 호 · 인증 분모

def test_added_calls_are_attach_and_tau_half_and_half():
    assert SC.STORM_SURGE_MIX == (("1", 0.5), ("5", 0.5))
    assert SC.surge_mix("signaling_storm") == SC.STORM_SURGE_MIX
    assert SC.surge_mix("natural_disaster") == SC.DISASTER_SURGE_MIX
    _sc, st = _run()
    added = st[E_(3)]["n"] - st[START - 1]["n"]
    assert st[E_(3)]["att_tau"] - st[START - 1]["att_tau"] >= 0.8 * added, "더한 호는 거의 전부 Attach·TAU"


def test_about_two_thirds_of_the_added_calls_count_in_the_auth_denominator():
    """Attach 1.00 · TAU 0.35 반반 → 약 67% 가 P-01 분모(`auth_attempt_flag`). 폭주가 인증 실패와 같은 호에서 이어진다."""
    _sc, st = _run()
    added = st[E_(0)]["n"] - st[START - 1]["n"]
    added_auth = st[E_(0)]["auth"] - st[START - 1]["auth"]
    assert 0.5 <= added_auth / added <= 0.8, added_auth / added
    assert st[E_(0)]["auth"] > 10 * st[START - 1]["auth"], "평소 인증 분모(대부분 Service 2%)보다 크게 늘어난다"


# ---------------------------------------------------------------- ③ 대상 MME 밖은 0건

def test_calls_of_other_mmes_never_get_a_failure():
    _sc, st = _run(other_mme=True)
    for m in range(MINUTES):
        assert not (st[m]["auth900"] or st[m]["ims26"] or st[m]["ctx500"]), m


# ---------------------------------------------------------------- ④ 폭주 곡선 · 27번 불변

def test_the_storm_curve_rises_with_the_stages_and_fades_slowly_until_the_fault_ends():
    """36번 폭주는 지진(27) 곡선을 빌리지 않는다 — 단계 시차(3분)에 맞춰 오르고 장애 끝까지 서서히 준다
    (2026-11-21 사용자 「서서히 움직이게」 · `storm_surge_shape`). 정점 배율만 지진과 같다."""
    class S:                                           # `disaster_surge_mult` 가 읽는 것만
        kind, peak, start_minute = "signaling_storm", 1.0, 100
        duration = 25
    m = [SC.disaster_surge_mult(S, 100 + e) for e in range(-1, 27)]
    assert m[0] == 1.0 and SC.disaster_surge_mult(S, 100 + S.duration) == 1.0     # 시작 전 · 끝난 뒤는 평소
    top = max(m)
    assert top == SC.DISASTER_SURGE_PEAK == SC.disaster_surge_mult(S, 100 + 2)    # 마지막 단계 시작 분에 정점
    rise = [SC.disaster_surge_mult(S, 100 + e) for e in range(0, 3)]
    assert rise == sorted(rise) and rise[0] > 1.0 and rise[0] < top               # 한꺼번에 오르지 않는다
    fall = [SC.disaster_surge_mult(S, 100 + e) for e in range(2, S.duration)]
    assert fall == sorted(fall, reverse=True) and min(fall) > 1.0                 # 끝까지 서서히 준다 · 13분째에 끊기지 않는다
    assert SC.disaster_surge_mult(S, 100 + 14) > 1.0 > 0                          # 지진은 13분에 끝난다 — 36번은 그 뒤에도 남는다
    q = type("Q", (), {"kind": "natural_disaster", "peak": 1.0, "start_minute": 100, "duration": 25})
    assert SC.disaster_surge_mult(q, 100 + 2) == SC.DISASTER_SURGE_PEAK            # 27번은 그대로
    assert SC.disaster_surge_mult(q, 100 + SC.DISASTER_SURGE_END) == 1.0
    assert SC.disaster_surge_mult(type("N", (), {"kind": "mme_overload", "peak": 1.0, "start_minute": 0, "duration": 9}), 3) == 1.0


def test_the_earthquake_surge_mix_is_unchanged():
    mix = dict(SC.DISASTER_SURGE_MIX)
    assert set(mix) == {"7", "8", "3", "4"} and abs(sum(mix.values()) - 1.0) < 1e-9
    assert SC.SURGE_KINDS == ("natural_disaster", "signaling_storm")
    assert SC.DISASTER_SURGE_PEAK == 40.0 and SC.DISASTER_SURGE_RAMP == 2 and SC.DISASTER_SURGE_END == 13


# ---------------------------------------------------------------- ⑤ 등록 · 길이 · 정답지

def test_the_three_registries_agree_and_the_label_is_the_easy_name():
    assert "signaling_storm" in control.KINDS and "signaling_storm" in SC.KIND_SPECS
    assert common.KIND_LABELS["signaling_storm"] == "단말 동시 재접속"
    assert control.KIND_SHOWN["signaling_storm"] == "단말 동시 재접속"
    row = next(r for r in control.SCENARIOS if r[0] == 36)
    assert row[2] == "signaling_storm" and row[4] == ("MME",)
    assert control.SCENARIO_RANK[36] == 11 and 36 not in control.RETIRED


def test_the_answer_sheet_points_at_r50_with_r23_as_the_alternative():
    sp = SC.KIND_SPECS["signaling_storm"]
    assert (sp["truth_code"], sp["truth_category"]) == ("SIGNALING_STORM", "CORE_CP")
    assert tuple(sp["expect_rules"]) == ("R-50",) and sp["expect_alternatives"] == {"R-23": "MME_OVERLOAD"}
    assert sp["expect_kpi"] == "C-24" and sp["scope_kind"] == "NODE" and sp["detect_min"] == 15


def test_recommended_length_is_twenty_minutes():
    from datagen import faults
    rec = SC.recommended_duration("signaling_storm")
    assert rec and rec["recommended"] == 20 and rec["min"] == 15, rec
    assert SC.KIND_SPECS["signaling_storm"]["detect_min"] + getattr(SC, "DETECT_MARGIN_MIN", 5) == 20
    assert faults._AUTO_NODE["signaling_storm"] == "mme_id"


def test_the_length_gets_the_last_stage_offset_and_the_target_is_one_mme():
    sc, _ = _run()
    assert sc.duration == DUR + max(SC.STORM_STAGE_AT.values())                       # 20 + 3
    assert sc.target.mme_id and sc.target.mme_group == {str(sc.target.mme_id)}


# ---------------------------------------------------------------- ⑥ 현황 화면 「미실행」 줄

def test_scenarios_html_shows_a_not_run_row_for_a_live_number_without_a_record():
    html = (ROOT / "src" / "datagen" / "ui" / "static" / "scenarios.html").read_text(encoding="utf-8")
    assert "미실행" in html
    out = scenarios_api.api_scenario_report(volume=50)
    assert out["ok"] is True
    ranks = {int(k) for k in out["ranks"]}
    assert 36 in ranks and not (ranks & control.RETIRED), "폐기 번호는 줄로 세우지 않는다"
    sh = out["shown"]["36"]
    assert sh["kind"] == "signaling_storm" and sh["name"] == "단말 동시 재접속" and sh["name_ja"]
