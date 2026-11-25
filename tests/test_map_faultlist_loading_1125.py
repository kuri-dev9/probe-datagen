"""지도 「장애 이력」 목록의 불러오는 중·오류 표시 — 2026-11-25 사용자 「장애 목록 불러오는데 왜 이리 오래 걸려?」 · 맥스 구현 · 에스터 시험.

원인: 지도를 열면 용어집 응답이 graphio 장애 목록보다 먼저 와서 `renderList` 를 불렀고, 그때 목록(`FAULT`)이 아직 없어
「graphio 가 연 장애가 없습니다」가 떴다 — 「활성 장애 4」인데 목록 0 이 되는 그 상태. 고친 뒤 고정하는 것 —
  * 첫 목록을 받기 전: 「graphio 에 묻는 중」 (「없습니다」·0건이 아니다)
  * 첫 목록 전에 못 받으면(`doc.error` · 통신 예외 모두): 「받지 못했습니다 — 10초 뒤 다시 묻습니다」 + 이유
  * 한 번이라도 받은 뒤: 마지막 목록을 그대로 두고 두 번 연속 실패할 때만 위쪽에 경고
  * 일본어 문구가 있다
node 가 없으면 함수는 건너뛰고 문자열 시험만 돈다. DB·graphio 는 쓰지 않는다.
"""
from __future__ import annotations

import re

import pytest
from test_map_quake_mini_1115 import MAP_HTML, NODE, _fn, _node

from datagen.ui.i18n_ja import _JA_MAP as JA  # noqa: E402  (없으면 아래에서 건너뛴다)

LOADING = "graphio 에 묻는 중"
FAILED = "graphio 에서 장애 목록을 받지 못했습니다 — 10초 뒤 다시 묻습니다"
NONE = "graphio 가 연 장애가 없습니다"


def test_the_first_html_already_says_loading_and_both_new_phrases_have_japanese():
    assert f"<div class=empty>{LOADING}</div>" in MAP_HTML
    for ko in (LOADING, FAILED, NONE):
        assert ko in JA and not re.search(r"[가-힣]", JA[ko]), ko


def test_render_list_says_none_only_after_a_list_arrived():
    body = _fn("renderList")
    i = body.index("if(!FAULT){")
    pre = body[i:body.index("return;\n  }", i)]
    assert LOADING in pre and FAILED in pre and NONE not in pre, "목록이 없을 때는 「없습니다」를 말하지 않는다"
    assert NONE in body[body.index("const H="):], "목록을 받은 뒤 정말 비었을 때만"
    assert "esc(FAULT_ERR)" in pre, "이유는 이스케이프해서 보인다"
    assert "document.getElementById('fcnt').textContent=''" in pre.replace(" ", "") or "fcnt').textContent=''" in pre


def test_poll_reports_failures_only_before_the_first_list_and_keeps_the_last_list_after():
    p = _fn("pollFaults", is_async=True)
    assert "catch(e){ FAULT_ERR=String(e); if(!FAULT) renderList(); return; }" in p                 # 통신 예외
    assert "if(doc.error){ FAULT_ERR=doc.error; if(!FAULT) renderList();" in p                     # graphio 가 준 오류
    assert "++FAULT_FAILS>=2" in p and "FAULT_ERR='';" in p and "FAULT_FAILS=0;" in p               # 두 번 연속일 때만 경고 · 받으면 되돌린다


POLL = """
const T=(s,o)=>o?s.replace(/\\{(\\w+)\\}/g,(_,k)=>o[k]):s, esc=x=>String(x).replace(/</g,'&lt;');
const els={}; const el=id=>els[id]||(els[id]={id,innerHTML:'',textContent:'',title:'',classList:{remove(){},add(){}}});
const document={getElementById:el};
let FAULT=null, FAULT_VER=null, LINK=null, PICK=null, FOCUS=null, LIVE_WINDOW_MIN=30, FTAB='all', FAULT_FAILS=0, FAULT_ERR='';
const FTABS=[['all','전체']], tabOf=()=> 'all', SEV_CLASS={}, coreOf=()=>'', renderTabs=()=>{};
let RENDERED=0; const applyFaults=()=>{}, drawPick=()=>{}, drawFocus=()=>{}, applyLink=()=>{};
let REPLY=null; const fetch=async u=>{ if(REPLY==='boom') throw new Error('network down'); return {json:async()=>REPLY}; };
"""


@pytest.mark.skipif(NODE is None, reason="node 없음")
def test_in_node_loading_then_error_then_empty_then_rows(tmp_path):
    out = _node(tmp_path, POLL + _fn("renderList") + _fn("pollFaults", is_async=True) + """
(async()=>{
  const r={};
  renderList(); r.loading=[el('fbody').innerHTML, el('fcnt').textContent];                    // 용어집이 먼저 와서 부른 경우
  REPLY='boom'; await pollFaults(); r.thrown=el('fbody').innerHTML;                           // 통신 예외
  REPLY={error:'graphio API <x> 에 닿지 않습니다'}; await pollFaults(); r.docerr=el('fbody').innerHTML;
  r.note1=el('note').textContent;                                                             // 연속 실패 1회째(앞 예외는 doc.error 가 아니라 세지 않음) — 아직 경고 아님
  await pollFaults(); r.note2=el('note').textContent;                                         // doc.error 두 번째 — 경고
  REPLY={ver:'v1', at:'2026-11-25T18:00:00', history:[]}; await pollFaults();
  r.empty=el('fbody').innerHTML; r.cntEmpty=el('fcnt').textContent; r.err=FAULT_ERR; r.fails=FAULT_FAILS;
  console.log(JSON.stringify(r));
})();
""")
    assert out["loading"] == ['<div class=empty>graphio 에 묻는 중</div>', ""], "첫 목록 전에는 불러오는 중"
    assert "받지 못했습니다" in out["thrown"] and "network down" in out["thrown"] and "없습니다" not in out["thrown"].replace("받지 못했습니다", "")
    assert "받지 못했습니다" in out["docerr"] and "&lt;x>" in out["docerr"] and "<x>" not in out["docerr"], "오류 문구는 이스케이프해서 넣는다"
    assert out["note1"] == "" and "graphio API" in out["note2"], "두 번 연속 실패해야 위쪽에 경고"
    assert out["empty"] == '<div class=empty>graphio 가 연 장애가 없습니다</div>' and out["err"] == "" and out["fails"] == 0, "받은 뒤 비었으면 그제서야 없다고 말한다"


@pytest.mark.skipif(NODE is None, reason="node 없음")
def test_in_node_after_a_list_arrived_a_failure_leaves_the_last_list_alone(tmp_path):
    out = _node(tmp_path, POLL + _fn("renderList") + _fn("pollFaults", is_async=True) + """
(async()=>{
  FAULT={history:[]}; FAULT_VER='v0'; el('fbody').innerHTML='KEEP';
  REPLY='boom'; await pollFaults(); const a=el('fbody').innerHTML;
  REPLY={error:'down'}; await pollFaults(); const b=el('fbody').innerHTML, n1=el('note').textContent;
  await pollFaults(); const n2=el('note').textContent;
  console.log(JSON.stringify({a,b,n1,n2}));
})();
""")
    assert out["a"] == "KEEP" and out["b"] == "KEEP", "받은 적이 있으면 실패해도 마지막 목록을 그대로 둔다"
    assert out["n1"] == "" and out["n2"] == "down"
