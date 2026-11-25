"""재생 연결(사건 ↔ 주입) 수정의 불변 규칙 — 2026-11-25 맥스 구현 · 에스터 시험.

사용자 지적: 4G↔5G 통합장애(07:52~08:26)의 재생 버튼이 F-597(DNS)에 붙었다. 고친 뒤 고정하는 것 —
  * `common.truth_codes()` 가 종류별 정답 코드의 **한 곳**이다(`/api/scenarios` 와 `replay_api` 가 같은 함수를 부른다)
  * 화면 `rpMatch`: 사건 [열림~갱신] 구간으로 주입 창과 겹침을 보고, 종류가 맞는 주입을 먼저 고른다.
    맞는 것이 없으면 시각으로 고르되 `miss`(⚠)를 단다. 사건 원인이 보류·없음이면 종류로 가르지 않는다
  * `_own_events`: **경쟁하는 주입이 있을 때만** 종류로 가른다(F-595 ↔ F-596). 하나뿐이면 원인이 달라도 그 주입 것
  * `route`: 노선 주입(`ROUTE%:…`)이면 주입한 노선, 아니면 셀 겹침
DB·graphio 는 가짜로 바꿔 끼운다. 화면 함수는 `map.html` 에서 잘라 node 로 돌린다(node 가 없으면 건너뛴다).
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

import pytest

from datagen.ui import common, faults_api
from datagen.ui import replay_api as R
from test_map_quake_mini_1115 import MAP_HTML, NODE, _node
from test_replay_api_1124 import FAULT, T0, _ev, _ln, _t, patch  # noqa: F401  (patch 는 픽스처)

# ── truth_codes — 한 곳 ───────────────────────────────────────────────────────

def test_truth_codes_come_from_kind_specs_and_drop_undetermined():
    from datagen.scenarios import KIND_SPECS
    assert common.truth_codes("ho_interruption") == ["RAN_HO_INTERRUPTION"]
    assert common.truth_codes("mobility") == ["RAN_MOBILITY_ISSUE"]
    assert common.truth_codes("signaling_storm") == ["SIGNALING_STORM", "MME_OVERLOAD"]   # 정답 + 예상 경쟁 후보
    for k, spec in KIND_SPECS.items():
        codes = common.truth_codes(k)
        assert "UNDETERMINED" not in codes and len(codes) == len(set(codes)), k
        if spec.get("truth_code") and spec["truth_code"] != "UNDETERMINED":
            assert codes[0] == spec["truth_code"], k


@pytest.mark.parametrize("kind", ["", None, "no_such_kind"])
def test_truth_codes_of_unknown_kind_is_empty_so_the_screen_does_not_filter(kind):
    assert common.truth_codes(kind) == []


def test_truth_codes_has_exactly_one_definition_and_both_sides_call_it():
    import inspect
    src = "".join(inspect.getsource(m) for m in (faults_api, R))
    assert "from .common import" in inspect.getsource(faults_api) and "truth_codes" in inspect.getsource(faults_api)
    assert "truth_codes" in inspect.getsource(R) and "truth_codes" in inspect.getsource(R._own_events)
    assert "def truth_codes" not in src                                  # 두 번째 정의(복사본)가 없다
    assert "expect_alternatives" not in src                              # 출처를 직접 읽지 않는다 — 공용 함수만


def test_api_scenarios_carries_truth_codes_from_the_shared_function(monkeypatch):
    rows = [{"kind": "ho_interruption", "no": 15}, {"kind": "no_such_kind", "no": 99}]
    monkeypatch.setattr(faults_api, "list_scenarios", lambda *a, **k: rows, raising=False)
    src = (faults_api.__file__ and open(faults_api.__file__, encoding="utf-8").read())
    i = src.index("def api_scenarios")
    body = src[i:src.index("\n@router", i) if "\n@router" in src[i:] else None]
    assert 'r["truth_codes"] = truth_codes(r.get("kind") or "")' in body


# ── `_own_events` — 경쟁 주입 사이에서만 종류로 ────────────────────────────────────

F595 = {"fault_id": 595, "kind": "ho_interruption", "start_ts": T0, "end_ts": T0 + timedelta(minutes=9), "status": "done"}
F596 = {"fault_id": 596, "kind": "mobility", "start_ts": T0, "end_ts": T0 + timedelta(minutes=9), "status": "done"}


def _ownset(f, evs):
    own, other = R._own_events(f, evs)
    return [e["event_id"] for e in own], {e["event_id"]: e["why"] for e in other}


def test_competing_fault_of_the_right_kind_takes_the_event_and_it_is_left_as_why_kind(patch):
    """F-595(4G↔5G)와 F-596(이동성)이 같은 시각에 시작했다 — 이동성 사건은 F-596 것이다."""
    patch.faults = [dict(F595), dict(F596)]
    ev = _ev("mob", "L2_EVENT", 2, root_code="RAN_MOBILITY_ISSUE")
    own, other = _ownset(dict(F595), [ev])
    assert own == [] and other == {"mob": "kind"}                         # F-595 는 「자기 사건 없음」
    own, other = _ownset(dict(F596, kind="mobility"), [ev])
    assert own == ["mob"] and other == {}


def test_a_lone_fault_keeps_an_event_whose_cause_differs_from_its_kind(patch):
    """잡는 주입이 하나뿐이면 graphio 가 원인을 틀려도 그 주입의 사건이다 — 틀린 판정일수록 재생에서 보여야 한다."""
    patch.faults = [dict(F595)]
    own, other = _ownset(dict(F595), [_ev("wrong", "L2_EVENT", 2, root_code="EXTERNAL_SERVER")])
    assert own == ["wrong"] and other == {}


def test_a_rival_that_does_not_fit_the_cause_either_does_not_take_the_event(patch):
    patch.faults = [dict(F595), dict(F596, kind="signaling_storm")]      # 경쟁 종류가 이동성이 아니다
    own, _ = _ownset(dict(F595), [_ev("mob", "L2_EVENT", 2, root_code="RAN_MOBILITY_ISSUE")])
    assert own == ["mob"]


def test_cause_undetermined_or_missing_is_never_split_by_kind(patch):
    patch.faults = [dict(F595), dict(F596)]
    evs = [_ev("u", "L2_EVENT", 1, root_code="UNDETERMINED"), _ev("n", "L1_BUNDLE", 1), _ev("e", "L1_BUNDLE", 1, root_code="")]
    own, other = _ownset(dict(F595), evs)
    assert "u" in own and not any(w == "kind" for w in other.values())


def test_the_rival_must_also_claim_the_event_by_time(patch):
    """다른 종류 주입이 있어도 시각이 안 닿으면 사건을 가져가지 못한다."""
    far = dict(F596, start_ts=T0 + timedelta(hours=3), end_ts=T0 + timedelta(hours=3, minutes=9))
    patch.faults = [dict(F595), far]
    own, _ = _ownset(dict(F595), [_ev("mob", "L2_EVENT", 2, root_code="RAN_MOBILITY_ISSUE")])
    assert own == ["mob"]


def test_a_cancelled_rival_is_not_a_rival(patch):
    patch.faults = [dict(F595), dict(F596, status="cancelled")]
    own, _ = _ownset(dict(F595), [_ev("mob", "L2_EVENT", 2, root_code="RAN_MOBILITY_ISSUE")])
    assert own == ["mob"]


def test_events_of_the_right_kind_stay_with_their_fault_even_with_a_rival(patch):
    patch.faults = [dict(F595), dict(F596)]
    own, other = _ownset(dict(F595), [_ev("ho", "L2_EVENT", 2, root_code="RAN_HO_INTERRUPTION")])
    assert own == ["ho"] and other == {}


def test_timelapse_of_the_fault_without_its_own_event_paints_no_borrowed_cells(patch, monkeypatch):
    """F-595 는 4G↔5G 판정이 없다 — 남의 사건(이동성)의 셀을 자기 셀로 칠하지 않는다. 셀이 없으면 3D 가 안내만 띄운다."""
    patch.faults = [dict(F595), dict(F596)]
    patch.events = [_ev("mob", "L2_EVENT", 2, root_code="RAN_MOBILITY_ISSUE")]
    monkeypatch.setattr(R, "_fault", lambda fid: dict(FAULT, fault_id=fid, kind="ho_interruption"))
    kept = {}

    def fault_cells(fid, evs):
        kept["evs"] = [e["event_id"] for e in evs]
        return [], {}
    monkeypatch.setattr(R, "_fault_cells", fault_cells)
    patch.truth = {}
    d = R.api_replay_timelapse(595)
    assert kept["evs"] == [] and d["cells"] == []


# ── `route` — 노선 주입이면 주입한 노선 ───────────────────────────────────────────

def test_route_prefers_the_injected_line_over_cell_overlap(patch, monkeypatch):
    patch.cells = ["1-0", "2-0", "3-0"]
    patch.lines = [_ln("YMT", ["9-0", "8-0"]), _ln("RAIL-0081", ["1-0", "2-0", "3-0"])]
    monkeypatch.setattr(R, "_fault", lambda fid: dict(FAULT, fault_id=fid, target="ROUTE%:50"))
    monkeypatch.setattr(R, "_injected_line", lambda f: "YMT")
    assert [r["id"] for r in R.api_replay_route(595)["routes"]] == ["YMT"]
    # 노선 주입이 아니면 셀 겹침이 고른다
    monkeypatch.setattr(R, "_injected_line", lambda f: "")
    assert [r["id"] for r in R.api_replay_route(595)["routes"]] == ["RAIL-0081"]


def test_route_explicit_line_still_wins_over_the_injected_one(patch, monkeypatch):
    patch.lines = [_ln("YMT", ["1-0"]), _ln("OTHER", ["1-0"])]
    monkeypatch.setattr(R, "_injected_line", lambda f: "YMT")
    assert [r["id"] for r in R.api_replay_route(604, line="other")["routes"]] == ["OTHER"]


def _routes_file(tmp_path, monkeypatch, ids):
    p = tmp_path / "routes.json"
    p.write_text(json.dumps([{"id": i} for i in ids]), encoding="utf-8")
    monkeypatch.setattr(R, "_pem_cfg", lambda: {"mobility": {"routes_file": str(p)}})


def test_injected_line_reads_the_generators_routes_file_and_folds_outer_inner(tmp_path, monkeypatch):
    from datagen.faults import ROUTE_RANGE_HEAD
    _routes_file(tmp_path, monkeypatch, ["YMT-OUTER", "YMT-INNER"])
    assert R._injected_line({"target": f"{ROUTE_RANGE_HEAD}:50"}) == "YMT"
    assert R._injected_line({"target": "DNS-1"}) == "" and R._injected_line({"target": ""}) == ""   # 노선 주입이 아니다


def test_injected_line_does_not_guess_when_the_file_has_several_lines_or_is_missing(tmp_path, monkeypatch):
    from datagen.faults import ROUTE_RANGE_HEAD
    t = {"target": f"{ROUTE_RANGE_HEAD}:50"}
    _routes_file(tmp_path, monkeypatch, ["YMT-OUTER", "CHUO"])
    assert R._injected_line(t) == ""                                      # 어느 것인지 모르면 짓지 않는다
    monkeypatch.setattr(R, "_pem_cfg", lambda: {"mobility": {"routes_file": str(tmp_path / "none.json")}})
    assert R._injected_line(t) == ""
    monkeypatch.setattr(R, "_pem_cfg", lambda: {})
    assert R._injected_line(t) == ""


# ── 화면 `rpMatch` ──────────────────────────────────────────────────────────

def _slice(start, end):
    i = MAP_HTML.index(start)
    return MAP_HTML[i:MAP_HTML.index(end, i)]


def _rp_src():
    return (re.search(r"const RP_SLACK_MIN=\d+;", MAP_HTML).group(0) + "\n"
            + re.search(r"const rpT=[^\n]+", MAP_HTML).group(0) + "\n"
            + _slice("function rpCodesOf", "async function rpBind"))


KINDS = ("ho_interruption", "mobility", "dns_degradation", "transport_loss", "ipv6_path_fail", "signaling_storm")


def _run(tmp_path, cases):
    scn = [{"kind": k, "truth_codes": common.truth_codes(k)} for k in KINDS]
    body = ("let LINK=null; const SCN=" + json.dumps(scn) + ";\n" + _rp_src()
            + "\nconst L=" + json.dumps(cases["faults"]) + ";\nconst out=[];\nfor (const h of " + json.dumps(cases["hits"])
            + ") out.push(rpMatch(h, L));\nconsole.log(JSON.stringify(out));")
    return _node(tmp_path, body)


def _f(fid, kind, start, mins, status="done"):
    return {"fault_id": fid, "kind": kind, "start_ts": start, "duration_min": mins, "status": status}


def _h(opened, updated, code, iid="X"):
    return {"incident_id": iid, "opened_ts": opened, "updated_ts": updated, "root_code": code}


FAULTS_1124 = [_f(597, "dns_degradation", "2026-11-24 07:30:00", 13), _f(595, "ho_interruption", "2026-11-24 07:00:06", 9),
               _f(598, "ho_interruption", "2026-11-24 08:03:00", 9), _f(588, "transport_loss", "2026-11-24 03:45:00", 18),
               _f(589, "ipv6_path_fail", "2026-11-24 03:45:00", 18), _f(593, "signaling_storm", "2026-11-24 05:53:00", 22)]


@pytest.mark.skipif(NODE is None, reason="node 없음")
def test_rpmatch_the_0752_4g5g_event_goes_to_f598_not_the_dns_fault(tmp_path):
    """열린 시각(07:52)은 DNS F-597 의 창(~07:53) 안이지만 종류가 다르다. 사건이 08:05 조각 때문에 F-598 창과 겹친다."""
    out = _run(tmp_path, {"faults": FAULTS_1124, "hits": [_h("2026-11-24 07:52:00", "2026-11-24 08:26:00", "RAN_HO_INTERRUPTION")]})
    assert out[0] == {"fid": 598, "miss": None}


@pytest.mark.skipif(NODE is None, reason="node 없음")
def test_rpmatch_same_start_picks_the_fault_whose_kind_fits_the_cause(tmp_path):
    code = common.truth_codes("transport_loss")[0]
    out = _run(tmp_path, {"faults": FAULTS_1124, "hits": [_h("2026-11-24 03:45:00", "2026-11-24 04:04:00", code)]})
    assert out[0] == {"fid": 588, "miss": None}                           # 같은 03:45 의 IPv6(F-589)가 아니다


@pytest.mark.skipif(NODE is None, reason="node 없음")
def test_rpmatch_wrong_cause_still_links_by_time_and_carries_the_warning(tmp_path):
    out = _run(tmp_path, {"faults": FAULTS_1124, "hits": [_h("2026-11-24 05:54:00", "2026-11-24 06:19:00", "CORE_NODE_NO_RESPONSE")]})
    assert out[0]["fid"] == 593 and out[0]["miss"]["code"] == "CORE_NODE_NO_RESPONSE" and out[0]["miss"]["f"]["fault_id"] == 593


@pytest.mark.skipif(NODE is None, reason="node 없음")
def test_rpmatch_undetermined_or_missing_cause_is_not_filtered_by_kind_and_has_no_warning(tmp_path):
    hits = [_h("2026-11-24 07:30:00", "2026-11-24 07:44:00", "UNDETERMINED"), _h("2026-11-24 07:31:00", "2026-11-24 07:44:00", None)]
    out = _run(tmp_path, {"faults": FAULTS_1124, "hits": hits})
    assert [o["fid"] for o in out] == [597, 597] and [o["miss"] for o in out] == [None, None]


@pytest.mark.skipif(NODE is None, reason="node 없음")
def test_rpmatch_uses_the_whole_event_span_and_the_unchanged_slack(tmp_path):
    assert re.search(r"const RP_SLACK_MIN=10;", MAP_HTML)                  # 새 임계값을 만들지 않았다
    # 열린 시각만 보면 창 밖(F-598 시작 08:03 − 1분 이전)이지만 사건이 그때까지 이어진다 → 겹친다
    out = _run(tmp_path, {"faults": [_f(598, "ho_interruption", "2026-11-24 08:03:00", 9)],
                          "hits": [_h("2026-11-24 07:52:00", "2026-11-24 08:26:00", "RAN_HO_INTERRUPTION"),
                                   _h("2026-11-24 07:20:00", "2026-11-24 07:40:00", "RAN_HO_INTERRUPTION"),     # 끝나도 겹치지 않는다
                                   _h("2026-11-24 08:30:00", "2026-11-24 08:40:00", "RAN_HO_INTERRUPTION"),     # 창 끝 + 10분을 넘는다
                                   _h("2026-11-24 08:20:00", "2026-11-24 08:21:00", "RAN_HO_INTERRUPTION")]})   # 끝 + 여유 안
    assert [o["fid"] for o in out] == [598, None, None, 598]


@pytest.mark.skipif(NODE is None, reason="node 없음")
def test_rpmatch_prefers_one_open_inside_the_window_then_the_latest_start_and_skips_cancelled(tmp_path):
    fl = [_f(1, "ho_interruption", "2026-11-24 10:00:00", 30), _f(2, "ho_interruption", "2026-11-24 10:10:00", 30),
          _f(3, "ho_interruption", "2026-11-24 10:12:00", 30, "cancelled")]
    out = _run(tmp_path, {"faults": fl, "hits": [_h("2026-11-24 10:15:00", "2026-11-24 10:20:00", "RAN_HO_INTERRUPTION")]})
    assert out[0]["fid"] == 2                                              # 열린 시각이 창 안인 것 가운데 가장 늦은 시작(취소 제외)


@pytest.mark.skipif(NODE is None, reason="node 없음")
def test_rpmatch_kind_unknown_to_the_scenario_list_is_not_filtered(tmp_path):
    out = _run(tmp_path, {"faults": [_f(7, "brand_new_kind", "2026-11-24 10:00:00", 10)],
                          "hits": [_h("2026-11-24 10:02:00", "2026-11-24 10:05:00", "SOME_CODE")]})
    assert out[0] == {"fid": 7, "miss": None}                              # truth_codes 를 모르면 종류로 거르지 않는다


@pytest.mark.skipif(NODE is None, reason="node 없음")
def test_rpmatch_link_from_the_server_wins_and_no_time_means_no_link(tmp_path):
    scn = json.dumps([{"kind": k, "truth_codes": common.truth_codes(k)} for k in KINDS])
    body = ("let LINK={fault:5, ids:new Set(['A'])}; const SCN=" + scn + ";\n" + _rp_src()
            + "\nconsole.log(JSON.stringify([rpMatch({incident_id:'A',opened_ts:'2026-01-01 00:00:00'},[]),"
              "rpMatch({incident_id:'B',opened_ts:null},[]), rpFaultOf({incident_id:'B',opened_ts:null},[])]));")
    a, b, c = _node(tmp_path, body)
    assert a == {"fid": 5, "miss": None} and b == {"fid": None, "miss": None} and c is None


def test_button_shows_the_warning_mark_and_the_tooltip_on_both_buttons_with_japanese():
    body = _slice("async function rpBind", "//: 3D 재생")
    assert "(miss?' ⚠':'')" in body
    assert body.count("+mw") == 2                                           # 재생·3D 재생 버튼 툴팁 둘 다
    assert "원인 판정이 주입 종류와 다릅니다 (판정 {c} · 주입 {k})" in body
    from datagen.ui.i18n_ja import _JA_MAP
    ja = _JA_MAP["원인 판정이 주입 종류와 다릅니다 (판정 {c} · 주입 {k})"]
    assert "{c}" in ja and "{k}" in ja and not re.search(r"[가-힣]", ja)
    assert "await loadScenarios()" in body                                  # 시나리오 목록이 아직 없으면 먼저 받는다
