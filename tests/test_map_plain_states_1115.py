"""27번 지도 — 상단 바 쉬운 말 · 셀 상태 음영(회색>빨강>노랑) · 흔들림 층 끄기 (2026-11-15 · 맥스 구현 · 에스터 시험).

화면은 새 사실을 만들지 않는다: 문장의 숫자는 받은 값뿐이고, 색은 탐지가 매긴 심각도 등급 그대로다. node 가 없으면 건너뛴다.
"""
from __future__ import annotations

import json
import re
import urllib.request

import pymysql
import pytest

from datagen.ui import map_api
from datagen.ui.i18n_ja import _JA_MAP
from test_map_quake_mini_1115 import MAP_HTML, NODE, _down2, _fn, _node

def _glossary():
    from pathlib import Path
    d = json.loads((Path(__file__).resolve().parents[1] / "config" / "scenario_glossary.json").read_text(encoding="utf-8"))
    return d.get("terms", d)


# ── 용어집 ───────────────────────────────────────────────────────────────

def test_glossary_has_c24_as_a_call_rush_and_keeps_c01():
    g = _glossary()
    assert g["C-24"]["kind"] == "kpi" and g["C-24"]["ko"]["s"] == "통화 몰림" and g["C-24"]["ja"]["s"] == "通話集中"
    assert g["C-24"]["ko"]["l"].strip() and g["C-24"]["ja"]["l"].strip()
    assert g["C-01"]["ko"]["s"] == "호 시도수"                    # 기존 이름은 그대로(탭 쪽이 따로 푼다)


# ── 쉬운 말 함수 (node) ──────────────────────────────────────────────────

PLAIN = r"""
const T=(s,o)=>o?s.replace(/\{(\w+)\}/g,(_,k)=>o[k]):s; const LANG='ko';
const KPI_GL={'C-24':{ko:{s:'통화 몰림'}},'C-01':{ko:{s:'호 시도수'}},'U-12':{ko:{s:'HTTP 성공률'}}};
const esc=x=>x;
""" + "{PLAINSRC}"


def _plain(tmp_path, body):
    a = MAP_HTML.index("const kpiName=")
    b = MAP_HTML.index("//: 원인 — graphio 가 준 이름")
    src = MAP_HTML[a:b].replace("const kpiTag=", "const _kpiTag=")
    return _node(tmp_path, PLAIN.replace("{PLAINSRC}", src) + body)


M = lambda kpi, sil=(), title="": json.dumps({"trigger_kpi": kpi, "silent": list(sil), "title": title}, ensure_ascii=False)  # noqa: E731


@pytest.mark.skipif(NODE is None, reason="node 없음")
def test_part_plain_c01_with_silent_cells_is_a_cut_and_without_is_neutral(tmp_path):
    out = _plain(tmp_path, f"""
const a=partPlain([{M('C-01',['1-0','1-1'])}]), b=partPlain([{M('C-01')}]);
console.log(JSON.stringify({{a,b}}));""")
    assert out["a"]["names"] == ["통화 끊김(셀 정지 의심)"] and out["a"]["sil"] == 2
    assert out["b"]["names"] == ["통화 시도 변화"] and out["b"]["sil"] == 0       # 방향을 모르면 중립


@pytest.mark.skipif(NODE is None, reason="node 없음")
def test_part_plain_orders_rush_before_cut_and_unions_silent_cells(tmp_path):
    out = _plain(tmp_path, f"""
const p=partPlain([{M('C-01',['1-0','1-1'])},{M('C-01,C-24',['1-1','2-0'])},{M('U-12')}]);
console.log(JSON.stringify(p));""")
    assert out["codes"] == ["C-24", "C-01", "U-12"]                           # C-24 → C-01 → 나머지
    assert out["sil"] == 3                                                    # 합집합(1-1 은 한 번)
    assert out["names"] == ["통화 몰림", "통화 끊김(셀 정지 의심)", "HTTP 성공률"]


@pytest.mark.skipif(NODE is None, reason="node 없음")
def test_what_plain_six_branches_and_numbers_are_inputs_only(tmp_path):
    out = _plain(tmp_path, f"""
const w=(l,c)=>whatPlain(partPlain(l),c);
console.log(JSON.stringify({{
 surge:w([{M('C-24')}],120),
 both:w([{M('C-24')},{M('C-01',[f'c{i}' for i in range(40)])}],120),
 cut:w([{M('C-01',['a','b'])}],24),
 nosil:w([{M('C-01')}],5),
 other:w([{M('U-12')}],7),
 zero:w([{M('U-12')}],0),
 big:w([{M('C-24')}],1200)}}));""")
    assert out["surge"] == "셀 120개에서 통화가 평소보다 몰림"
    assert out["both"] == "셀 120개에서 통화가 평소보다 몰림, 그중 셀 40개는 통화가 끊김(셀 정지 의심)"
    assert out["cut"] == "셀 2개에서 통화가 끊김(셀 정지 의심)"           # 끊김만이면 「그중」 없이 무호 셀 수
    assert out["nosil"] == "셀 5개에서 통화 시도가 평소와 달라짐"
    assert out["other"] == "셀 7개에서 HTTP 성공률 이상" and out["zero"] == "HTTP 성공률 이상"
    assert out["big"] == "셀 1,200개에서 통화가 평소보다 몰림"
    # 문장에 나오는 숫자는 입력(셀 수 · 무호 셀 수)뿐이다
    assert set(re.findall(r"\d[\d,]*", out["both"])) == {"120", "40"}


@pytest.mark.skipif(NODE is None, reason="node 없음")
def test_where_plain_strips_prefecture_and_other_marker_and_limits_to_three(tmp_path):
    out = _plain(tmp_path, r"""
console.log(JSON.stringify({
 a:wherePlain(['東京都 中央区, 東京都 港区 외 2곳 (시군구 5곳, 셀 96개) …']),
 b:wherePlain(['東京都 中央区, 東京都 港区 (시군구 2곳)']),
 c:wherePlain(['東京都 中央区, 東京都 港区, 東京都 台東区, 東京都 品川区 (시군구 4곳)']),
 d:wherePlain(['東京都 中央区 (a)','千葉県 千葉市 (b)','東京都 中央区 (c)']),
 e:wherePlain(['꼴이 다른 제목']), f:wherePlain([]), g:wherePlain([null,'']) }));""")
    assert out["a"] == "中央区, 港区 외"
    assert out["b"] == "中央区, 港区"                                          # 외 표시가 없고 3곳 이하면 붙이지 않는다
    assert out["c"] == "中央区, 港区, 台東区 외"                               # 3곳을 넘으면 외
    assert out["d"] == "中央区, 千葉市"                                        # 중복 제거 · 도도부현 뗌
    assert out["e"] == "" and out["f"] == "" and out["g"] == ""               # 지어내지 않는다


# ── 셀 심각도 API ────────────────────────────────────────────────────────

def _sev_db(monkeypatch, per_member, radius=(), boom=None):
    """`_cell_severity` 용 가짜 DB — 조각마다 AI_ANOMALY_EVENT 질의에 per_member[i] 를, 반경 질의에 radius 를 돌려준다."""
    log, answers = [], list(per_member)

    class Cur:
        def __enter__(s): return s
        def __exit__(s, *a): return False
        def execute(s, sql, params=None):
            log.append((" ".join(sql.split()), list(params or [])))
            if "CM_CELL_GEO_MAP" in sql:
                s.k = "r"
            else:
                s.k = "s"
                if boom == "table":
                    raise pymysql.err.ProgrammingError(1146, "no table")
        def fetchall(s):
            if s.k == "r":
                return list(radius)
            return [r if len(r) == 3 else (*r, "C-01") for r in answers.pop(0)]       # (SCOPE_ID, SEVERITY, KPI)

    class Conn:
        closed = False
        def cursor(s): return Cur()
        def close(s): s.closed = True

    c = Conn()
    if boom == "connect":
        def bad():
            raise pymysql.err.OperationalError(2003, "down")
        monkeypatch.setattr(map_api, "_connect", bad)
    else:
        monkeypatch.setattr(map_api, "_connect", lambda: c)
    return log, c


def _mem(iid, kpi, cells, sev="MAJOR", lo="2026-11-15 14:05:00", hi="2026-11-15 14:27:00"):
    return {"incident_id": iid, "trigger_kpi": kpi, "cells": cells, "opened_ts": lo, "updated_ts": hi, "severity": sev}


def test_cell_severity_query_is_narrowed_to_the_piece_kpis_window_cells_and_skips_cancelled(monkeypatch):
    log, c = _sev_db(monkeypatch, [[("1-0", "MAJOR")]])
    map_api._cell_severity([_mem("A", "C-01, C-24", ["1-1", "1-0"])])
    sql, params = log[0]
    for need in ("FROM AI_ANOMALY_EVENT", "SCOPE_TYPE = 'CELL'", "COALESCE(CANCELLED, 0) = 0", "EVENT_TIME >= %s",
                 "EVENT_TIME <= %s", "KPI IN (%s,%s)", "SCOPE_ID IN (%s,%s)"):
        assert need in sql, need
    assert "SELECT SCOPE_ID, SEVERITY, KPI FROM AI_ANOMALY_EVENT" in sql
    assert params == ["2026-11-15 14:05:00", "2026-11-15 14:27:00", "C-01", "C-24", "1-0", "1-1"]
    assert all(s.startswith("SELECT") for s, _ in log) and c.closed


def test_cell_severity_picks_the_highest_per_cell_and_falls_back_to_the_piece_severity(monkeypatch):
    rows = [("1-0", "MAJOR"), ("1-0", "CRITICAL"), ("1-0", "MINOR"), ("1-1", "WARNING"), ("1-1", "INFO")]
    log, c = _sev_db(monkeypatch, [rows], radius=[("1", 0, 300.0)])
    sev, src, cell_r = map_api._cell_severity([_mem("A", "C-01", ["1-0", "1-1", "1-2"], sev="Minor")])
    assert {k: v["sev"] for k, v in sev["A"].items()} == {"1-0": "CRITICAL", "1-1": "WARNING", "1-2": "MINOR"}   # 셀 행 없는 1-2 는 조각 등급
    assert src["A"] == {"rows": 2, "member": 1, "kpi_unknown": 0}
    assert cell_r["1-0"] == 300.0 and cell_r["1-1"] is None and "1-2" in cell_r


def test_cell_severity_gives_the_per_kpi_grade_only_from_cell_rows_or_a_single_trigger(monkeypatch):
    """11-16 셀 탭의 증상별 셀 수 — 셀 행의 KPI 별 최고 등급. 셀 행이 없으면 트리거가 하나일 때만 그 KPI, 여럿이면 비운다."""
    rows = [("1-0", "MAJOR", "C-24"), ("1-0", "CRITICAL", "C-01"), ("1-0", "MINOR", "C-24"), ("1-1", "MAJOR", "C-24")]
    _sev_db(monkeypatch, [rows])
    sev, src, _ = map_api._cell_severity([_mem("A", "C-01,C-24", ["1-0", "1-1", "1-2"], sev="Major")])
    assert sev["A"]["1-0"] == {"sev": "CRITICAL", "kpi": {"C-24": "MAJOR", "C-01": "CRITICAL"}}   # KPI 별 가장 높은 등급
    assert sev["A"]["1-1"]["kpi"] == {"C-24": "MAJOR"}
    assert sev["A"]["1-2"] == {"sev": "MAJOR", "kpi": {}}                        # 셀 행 없음 + 트리거 둘 → 어느 증상인지 모른다
    assert src["A"] == {"rows": 2, "member": 1, "kpi_unknown": 1}
    _sev_db(monkeypatch, [[]])
    sev, src, _ = map_api._cell_severity([_mem("B", "C-24", ["2-0"], sev="Major")])
    assert sev["B"]["2-0"] == {"sev": "MAJOR", "kpi": {"C-24": "MAJOR"}} and src["B"]["kpi_unknown"] == 0   # 트리거 하나 → 그 KPI


def test_cell_severity_missing_table_uses_the_piece_severity_and_connection_failure_is_unknown(monkeypatch):
    _sev_db(monkeypatch, [[]], boom="table")
    sev, src, _ = map_api._cell_severity([_mem("A", "C-24", ["1-0"], sev="MAJOR")])
    assert sev == {"A": {"1-0": {"sev": "MAJOR", "kpi": {"C-24": "MAJOR"}}}} and src == {"A": {"rows": 0, "member": 1, "kpi_unknown": 0}}
    _sev_db(monkeypatch, [], boom="connect")
    assert map_api._cell_severity([_mem("A", "C-24", ["1-0"])]) == ({}, {}, {})       # 모름 — 칠하지 않는다


def test_cell_severity_without_trigger_kpi_does_not_query_and_uses_piece_severity(monkeypatch):
    log, _ = _sev_db(monkeypatch, [])
    sev, src, _ = map_api._cell_severity([_mem("A", "", ["1-0"], sev="CRITICAL")])
    assert sev["A"]["1-0"]["sev"] == "CRITICAL" and not any("AI_ANOMALY_EVENT" in s for s, _ in log)
    assert src["A"]["kpi_unknown"] == 1                                              # 트리거 KPI 가 없으니 증상을 모른다


def test_cell_severity_unknown_piece_severity_paints_nothing_extra(monkeypatch):
    _sev_db(monkeypatch, [[]])
    sev, _, _ = map_api._cell_severity([_mem("A", "C-24", ["1-0"], sev="")])
    assert sev["A"] == {}


# ── api_map_event ?states ────────────────────────────────────────────────

class _Resp:
    def __init__(self, doc): self.doc = doc
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def read(self): return json.dumps(self.doc).encode()


def _event(monkeypatch):
    doc = {"cells": [{"key": "1-0", "lat": 35.7, "lon": 139.8}],
           "members": [_mem("A", "C-24", ["1-0"])]}
    monkeypatch.setattr(map_api, "_pem_cfg", lambda: {"graphio_api": "http://g:8008"})
    monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout=0: _Resp(doc))
    monkeypatch.setattr(map_api, "_silent_cells", lambda raw: ({}, {}))
    calls = []
    monkeypatch.setattr(map_api, "_cell_severity", lambda raw: (calls.append(1), ({"A": {"1-0": {"sev": "MAJOR", "kpi": {"C-24": "MAJOR"}}}},
                                                                {"A": {"rows": 1, "member": 0, "kpi_unknown": 0}}, {"1-0": 300.0}))[1])
    return calls


def test_api_map_event_without_states_asks_nothing_extra(monkeypatch):
    calls = _event(monkeypatch)
    out = map_api.api_map_event("X")
    assert calls == [] and "cell_r" not in out
    assert all(not ({"cell_sev", "cell_kpi", "sev_src"} & set(m)) for m in out["members"])


def test_api_map_event_with_states_adds_cell_sev_sev_src_and_cell_r(monkeypatch):
    calls = _event(monkeypatch)
    out = map_api.api_map_event("X", states=True)
    m = out["members"][0]
    assert calls == [1] and m["cell_sev"] == {"1-0": "MAJOR"} and m["cell_kpi"] == {"1-0": {"C-24": "MAJOR"}}
    assert m["sev_src"] == {"rows": 1, "member": 0, "kpi_unknown": 0} and m["keys"] == ["1-0"]
    assert out["cell_r"] == {"1-0": 300.0} and out["enbs"] == ["1"]            # 기존 칸은 그대로


# ── 셀 상태 분류 (node) ──────────────────────────────────────────────────

def _cls(tmp_path, members, part="null", groups="[]", cr="{}", sr="{}"):
    body = f"""
const ms={json.dumps(members)};
H.I1={{incident_id:'I1',closed_ts:'x',members:ms.map(m=>({{incident_id:m.incident_id}})),groups:{groups}}};
SIL.I1={{t:Date.now(),doc:{{members:ms,silent_r:{sr},cell_r:{cr}}}}};
PART={part};
const r=silentOf(H.I1); const k=a=>a.map(x=>x.key).sort();
console.log(JSON.stringify({{gray:k(r.gray),red:k(r.red),yellow:k(r.yellow),raw:r}}));"""
    return _down2(tmp_path, body)


@pytest.mark.skipif(NODE is None, reason="node 없음")
def test_states_gray_beats_red_beats_yellow_and_one_cell_gets_one_color(tmp_path):
    out = _cls(tmp_path, [{"incident_id": "A", "silent": ["s1"], "cell_sev": {"s1": "CRITICAL", "r1": "CRITICAL", "y1": "MAJOR", "y2": "MINOR", "y3": "WARNING"}},
                          {"incident_id": "B", "silent": [], "cell_sev": {"y1": "CRITICAL", "y2": "MAJOR"}}],
               cr='{"r1":300,"y1":300,"y2":300,"y3":300}', sr='{"s1":353}')
    assert out["gray"] == ["s1"]                      # 무호 셀은 CRITICAL 이어도 회색
    assert out["red"] == ["r1", "y1"]                 # 여러 조각이면 높은 쪽(y1: MAJOR·CRITICAL → 빨강)
    assert out["yellow"] == ["y2", "y3"]
    allk = out["gray"] + out["red"] + out["yellow"]
    assert len(allk) == len(set(allk))                # 한 셀이 두 색에 들지 않는다


@pytest.mark.skipif(NODE is None, reason="node 없음")
def test_states_info_and_unknown_grades_are_not_painted(tmp_path):
    out = _cls(tmp_path, [{"incident_id": "A", "silent": [], "cell_sev": {"a": "INFO", "b": "???", "c": "", "d": "MAJOR"}}],
               cr='{"d":300}')
    assert out["red"] == [] and out["yellow"] == ["d"]


@pytest.mark.skipif(NODE is None, reason="node 없음")
def test_states_tab_selects_only_that_pieces_cells(tmp_path):
    ms = [{"incident_id": "A", "silent": ["s"], "cell_sev": {"y": "MAJOR"}}, {"incident_id": "B", "silent": [], "cell_sev": {"r": "CRITICAL"}}]
    out = _cls(tmp_path, ms, part="1", groups="[{idx:[0]},{idx:[1]}]", cr='{"y":1,"r":1}', sr='{"s":1}')
    assert out["gray"] == [] and out["red"] == ["r"] and out["yellow"] == []      # 탭 2 = 조각 B 만
    out = _cls(tmp_path, ms, part="0", groups="[{idx:[0]},{idx:[1]}]", cr='{"y":1,"r":1}', sr='{"s":1}')
    assert out["gray"] == ["s"] and out["red"] == [] and out["yellow"] == ["y"]


@pytest.mark.skipif(NODE is None, reason="node 없음")
def test_states_radius_falls_back_from_silent_r_to_cell_r_and_cells_without_radius_or_coord_are_not_drawn(tmp_path):
    out = _down2(tmp_path, """
D={cells:CELLS}; MINI_Q={fault_id:355}; FOCUS='I1';
H.I1={incident_id:'I1',closed_ts:'x',members:[{incident_id:'A'}],groups:[]};
SIL.I1={t:Date.now(),doc:{members:[{incident_id:'A',silent:['1-0','1-1'],cell_sev:{'2-0':'MAJOR','9-9':'MAJOR','1-0':'MAJOR'}}],
  silent_r:{'1-0':353}, cell_r:{'1-1':200,'2-0':null,'9-9':300}}};
drawDown();
console.log(JSON.stringify({gray:gN(),yellow:gy.children.length,red:gr.children.length,SEV_N,
  rows:legendBox.children.map(c=>c.innerHTML)}));""")
    # 1-0 은 silent_r, 1-1 은 cell_r 로 반경을 얻는다. 2-0 은 반경 없음 · 9-9 는 지도에 없음 → 그리지 않는다
    assert out["gray"] == 2 and out["yellow"] == 0 and out["red"] == 0
    assert out["SEV_N"] == {"gray": 2, "red": 0, "yellow": 0}
    assert len(out["rows"]) == 3 and "셀 2" in out["rows"][0] and "셀 0" in out["rows"][1] and "셀 0" in out["rows"][2]


@pytest.mark.skipif(NODE is None, reason="node 없음")
def test_states_are_drawn_into_their_own_layers_and_counted_in_the_legend(tmp_path):
    out = _down2(tmp_path, """
D={cells:CELLS}; MINI_Q={fault_id:355}; FOCUS='I1';
H.I1={incident_id:'I1',closed_ts:'x',members:[{incident_id:'A'}],groups:[]};
SIL.I1={t:Date.now(),doc:{members:[{incident_id:'A',silent:['1-0'],cell_sev:{'1-1':'CRITICAL','2-0':'MAJOR'}}],
  silent_r:{'1-0':353}, cell_r:{'1-1':300,'2-0':300}}};
DOWN={enbs:['1'],cells:[{key:'1-0',r_m:353}]}; TRUTH_ON=true;
drawDown();
console.log(JSON.stringify({gray:gN(),red:gr.children.length,yellow:gy.children.length,truth:gt.children.length,
  rows:legendBox.children.map(c=>[c.className,c.innerHTML])}));""")
    assert (out["gray"], out["red"], out["yellow"], out["truth"]) == (1, 1, 1, 1)
    labels = [r[1] for r in out["rows"]]
    assert "통화 끊김(셀 정지 의심)" in labels[0] and "심하게 나빠짐" in labels[1] and "나빠짐" in labels[2] and "실제 정지(정답)" in labels[3]
    assert [("ssevr" in l, "ssevy" in l, "sdownt" in l) for l in labels] == [(False,) * 3, (True, False, False), (False, True, False), (False, False, True)]


# ── 흔들림 층 끄기 ───────────────────────────────────────────────────────

def test_impact_switch_is_a_single_const_false_and_the_api_and_code_remain():
    assert len(re.findall(r"const IMPACT_ON=false;", MAP_HTML)) == 1
    assert "function impactOf(" in MAP_HTML and "/impact" in MAP_HTML
    assert "imp=q&&IMPACT_ON?impactOf(MINI_Q.fault_id):null" in MAP_HTML        # 꺼져 있으면 묻지도 그리지도 않는다
    assert any(r.path.endswith("/impact") for r in map_api.router.routes)        # 서버 API 는 그대로


@pytest.mark.skipif(NODE is None, reason="node 없음")
def test_impact_off_asks_nothing_draws_nothing_and_has_no_legend_row(tmp_path):
    out = _down2(tmp_path, """
D={cells:CELLS}; MINI_Q={fault_id:355}; FOCUS='I1'; IMPACT_ON=false;
H.I1={incident_id:'I1',closed_ts:'x',members:[],groups:[]};
SIL.I1={t:Date.now(),doc:{members:[],silent_r:{}}};
drawDown(); drawDown();
setTimeout(()=>{ console.log(JSON.stringify({asked:FETCHED.filter(u=>u.includes('/impact')).length, imp:gi.children.length,
  rows:legendBox.children.map(c=>c.innerHTML)})); process.exit(0); },30);""")
    assert out["asked"] == 0 and out["imp"] == 0
    assert len(out["rows"]) == 3 and not any("영향 지역" in r for r in out["rows"])


@pytest.mark.skipif(NODE is None, reason="node 없음")
def test_legend_rows_absent_without_a_quake_and_no_sev_layers_painted(tmp_path):
    out = _down2(tmp_path, """
D={cells:CELLS}; MINI_Q=null; FOCUS=null;
H.I1={incident_id:'I1',closed_ts:'x',members:[],groups:[]};
SIL.I1={t:Date.now(),doc:{members:[{incident_id:'A',silent:['1-0'],cell_sev:{'2-0':'MAJOR'}}],silent_r:{'1-0':3},cell_r:{'2-0':3}}};
drawDown();
console.log(JSON.stringify({g:gN(),gy:gy.children.length,gr:gr.children.length,rows:legendBox.children.length,display:legendBox.style.display}));""")
    assert out == {"g": 0, "gy": 0, "gr": 0, "rows": 0, "display": "none"}


# ── 번역 ─────────────────────────────────────────────────────────────────

NEW_KEYS = ["통화 끊김(셀 정지 의심)", "통화 시도 변화", "셀 {c}개에서 통화가 평소보다 몰림",
            "그중 셀 {n}개는 통화가 끊김(셀 정지 의심)", "셀 {n}개에서 통화가 끊김(셀 정지 의심)",
            "셀 {c}개에서 통화 시도가 평소와 달라짐", "셀 {c}개에서 {k} 이상", "{k} 이상", " 외", "무엇", "어디서",
            "통화 끊김(셀 정지 의심)", "심하게 나빠짐", "나빠짐", "실제 정지(정답)", "셀 {c} · eNB ID {n}", "eNB ID {n}",
            "셀 {c}개", "겹친 셀 {n}개", "증상을 가르지 못한 셀 {n}개", "{n}칸", "{n}대", "코어 장비"]


@pytest.mark.parametrize("k", NEW_KEYS)
def test_new_phrases_are_translated_keep_placeholders_and_are_used_by_the_screen(k):
    assert k in _JA_MAP and _JA_MAP[k].strip(), k
    assert set(re.findall(r"\{\w+\}", k)) == set(re.findall(r"\{\w+\}", _JA_MAP[k])), k        # 자리표시자 유지
    assert f"T('{k}'" in MAP_HTML, k                                                          # 화면이 쓴다
    assert re.search(r"[぀-ヿ一-龯]", _JA_MAP[k]) or k in ("eNB ID {n}", "eNB {n}")                           # 일본어다
