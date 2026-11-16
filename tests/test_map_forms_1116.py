"""gen-ui 지도 — 표식 형태별 탭 (2026-11-16 사용자 "각각의 표식 자체로 묶어. AREA·시군구·읍면동… 한 번에 하나만" · L2·L1·D 모두).

조각을 지도에 그리는 모양(축)으로 묶는다 — 셀 · AREA(메시 세 크기) · 읍면동 · 시군구 · 시도 · 코어 장비 · APN · 서비스 도메인 ·
전국 · 단말 기종. 「전체」 탭은 없고, 처음엔 대표 조각이 든 탭. node 가 없으면 건너뛴다. DB 를 쓰지 않는다.
"""
from __future__ import annotations

import json
import re

import pytest

from test_map_quake_mini_1115 import MAP_HTML, NODE, _node

PRE = r"""
const T=(s,o)=>o?s.replace(/\{(\w+)\}/g,(_,k)=>o[k]):s; const LANG='ko', esc=x=>x;
let PART=null, FOCUS=null, SIL={}, EVT={}; const H={};
const evtOf=h=>h.ev||h, histOf=id=>H[id], evtKey=h=>h.incident_id, nid=x=>x;
const PART_NAME={}, AXIS_NAME={}, GEO_NAME={};
let KPI_GL={'C-24':{ko:{s:'통화 몰림'}},'C-01':{ko:{s:'호 시도수'}},'U-12':{ko:{s:'HTTP 성공률'}}};
"""


def _src():
    a = MAP_HTML.index("const MESH_AXES=")
    b = MAP_HTML.index("const hm=v=>")
    c = MAP_HTML.index("const kpiName=")
    d = MAP_HTML.index("//: 원인 — graphio 가 준 이름")
    return MAP_HTML[a:b] + "\n" + MAP_HTML[c:d].replace("const kpiTag=", "const _kpiTag=")


def _run(tmp_path, body):
    return _node(tmp_path, PRE + _src() + body)


# 조각 만들기 — JS 쪽 헬퍼
MK = r"""
const mk=(id,axis,o={})=>Object.assign({incident_id:id,axis,trigger_kpi:'C-24',title:'',pts:[],enbs:[],keys:[],cells:0,areas:[],nodes:[],axis_vals:[],
  dpts:[],denbs:[],detached:0,detached_kpis:[],severity:'Major'},o);
const ev=(members,o={})=>Object.assign({incident_id:'E1',event_unit:'L2_EVENT',members,pts:[],enbs:[],cells:0,areas:[]},o);
"""


@pytest.mark.skipif(NODE is None, reason="node 없음")
def test_form_of_maps_axes_to_the_drawing_form(tmp_path):
    out = _run(tmp_path, """
console.log(JSON.stringify(['GEO',null,undefined,'','MESH250','MESH500','MESH1K','ADM3','ADM2','ADM1','NODE','APN','DOMAIN','GLOBAL','UE'].map(formOf)));""")
    assert out == ["CELL", "CELL", "CELL", "CELL", "AREA", "AREA", "AREA", "ADM3", "ADM2", "ADM1", "NODE", "APN", "DOMAIN", "GLOBAL", "UE"]


@pytest.mark.skipif(NODE is None, reason="node 없음")
def test_a_single_piece_form_comes_from_axis_raw_then_axis(tmp_path):
    out = _run(tmp_path, """
console.log(JSON.stringify([pieceForm({axis_raw:'MESH500',axis:'MESH500'}), pieceForm({axis:'ADM2'}), pieceForm({}), pieceForm({axis_raw:'APN',axis:null})]));""")
    assert out == ["AREA", "ADM2", "CELL", "APN"]


@pytest.mark.skipif(NODE is None, reason="node 없음")
def test_partgroups_make_one_tab_per_form_in_fixed_order_and_merge_the_three_mesh_sizes(tmp_path):
    out = _run(tmp_path, MK + """
const h=ev([mk('a','NODE'),mk('b','MESH250'),mk('c','GEO',{trigger_kpi:'C-01'}),mk('d','MESH1K'),mk('e','MESH500',{trigger_kpi:'Q-11'}),mk('f','APN'),mk('g','GEO')]);
const gs=partGroups(h); console.log(JSON.stringify({g:gs.map(x=>[x.form,x.label,x.idx]), same:partGroups(h)===gs}));""")
    assert [x[0] for x in out["g"]] == ["CELL", "AREA", "NODE", "APN"]                  # FORM_ORDER — 셀 · AREA · … · 코어 장비 · APN
    assert out["g"][0] == ["CELL", "셀", [2, 6]]                                         # 지표가 달라도(C-24 · C-01) 같은 모양이면 한 탭
    assert out["g"][1][2] == [1, 3, 4]                                                  # 메시 세 크기 + 지표가 다른 조각도 한 탭
    assert out["same"] is True                                                          # 한 번 만들고 붙잡는다(다시 그릴 때 탭이 흔들리지 않는다)


@pytest.mark.skipif(NODE is None, reason="node 없음")
def test_unknown_axis_goes_after_the_known_forms_and_global_egress_is_named_hq(tmp_path):
    out = _run(tmp_path, MK + """
const h=ev([mk('a','WHATEVER'),mk('b','GEO'),mk('c','GLOBAL')],{egress:true});
console.log(JSON.stringify(partGroups(h).map(g=>[g.form,g.label])));""")
    assert [x[0] for x in out] == ["CELL", "GLOBAL", "WHATEVER"] and out[1][1] == "도코모 본사"


@pytest.mark.skipif(NODE is None, reason="node 없음")
def test_there_is_no_all_tab_and_the_default_is_the_tab_holding_the_representative_piece(tmp_path):
    out = _run(tmp_path, MK + """
const h=ev([mk('a','MESH250'),mk('b','GEO'),mk('E1','NODE')]);     // 대표 조각 = 사건 ID(E1)와 같은 조각
const r={dflt:curPart(h), form:curForm(h)};
PART=0; r.chosen=curPart(h); r.chosenForm=curForm(h);
PART=9; r.badIndex=curPart(h); PART=null;
const lone=mk('X','GEO'); r.noTabs=curPart(lone); r.loneForm=curForm({incident_id:'D1',axis_raw:'MESH1K'});
const h2=ev([mk('a','MESH250'),mk('b','GEO')],{incident_id:'ZZ'}); r.firstTab=curPart(h2);
console.log(JSON.stringify(r));""")
    assert out["dflt"] == 2 and out["form"] == "NODE"                  # 정렬 뒤(셀·AREA·장비) 대표 조각 E1 이 든 NODE 탭
    assert out["chosen"] == 0 and out["chosenForm"] == "CELL"          # 고른 탭은 유지
    assert out["badIndex"] == 2                                         # 없는 탭 번호는 기본으로
    assert out["noTabs"] is None and out["loneForm"] == "AREA"          # 조각 하나(D)는 탭이 없고 형태는 그 조각의 축
    assert out["firstTab"] == 0                                         # 대표 조각이 없으면 첫 탭


@pytest.mark.skipif(NODE is None, reason="node 없음")
def test_form_count_and_tag_per_form(tmp_path):
    out = _run(tmp_path, MK + """
const A=(axis,ids)=>({axis,geo_ids:ids,dong:null});
const h=ev([mk('c1','GEO',{keys:['1-0','1-1','2-0'],cells:3}),mk('c2','GEO',{keys:['1-1','3-0'],cells:2}),
  mk('a1','MESH250',{areas:[A('MESH250',['x','y']),A('ADM3',['t1'])]}),mk('a2','MESH500',{areas:[A('MESH500',['y','z'])]}),
  mk('d1','ADM3',{areas:[A('ADM3',['t1','t2'])]}),
  mk('n1','NODE',{nodes:['mme:1','sgw:2']}),mk('n2','NODE',{nodes:['sgw:2','pgw:4']}),
  mk('p1','APN',{axis_vals:['ims','internet']}),mk('p2','APN',{axis_vals:['ims']}), mk('u1','UE',{axis_vals:[]})]);
const f=form=>{ const g=partGroups(h).find(x=>x.form===form); return [formCount(h,g), formTag(form, formCount(h,g))]; };
console.log(JSON.stringify({cell:f('CELL'),area:f('AREA'),adm3:f('ADM3'),node:f('NODE'),apn:f('APN'),ue:f('UE'),
  lone:formCount({incident_id:'D',axis_raw:'GEO',cell_cnt:7,enbs:['1']},null), loneNoCnt:formCount({incident_id:'D',axis_raw:'GEO',enbs:['1','2']},null)}));""")
    assert out["cell"] == [4, "셀 · 4"]                                   # 합집합(1-1 은 한 번)
    # 칸은 「크기:코드」로 센다 — 250m 의 y 와 500m 의 y 는 다른 칸이다. 읍면동 칸(ADM3)은 이 탭 몫이 아니다
    assert out["area"] == [4, "AREA · 4칸"]
    assert out["adm3"] == [2, "읍면동 · 2칸"]                              # d1 의 t1 · t2 (a1 의 ADM3 칸은 메시 조각이라 MESH 탭이 아닌 ADM3 형태로 셈하지 않는다)
    assert out["node"] == [3, "코어 장비 · 3대"] and out["apn"] == [2, "APN · 2"] and out["ue"] == [0, "단말 기종"]
    assert out["lone"] == 7 and out["loneNoCnt"] == 2                     # 조각 하나(D) — cell_cnt, 없으면 eNB 수(셀 수를 모를 때의 대용)


@pytest.mark.skipif(NODE is None, reason="node 없음")
def test_view_of_draws_only_the_selected_form(tmp_path):
    out = _run(tmp_path, MK + """
const A=(axis,ids)=>({axis,geo_ids:ids,dong:null});
const h=ev([mk('c1','GEO',{pts:[[139.7,35.6]],enbs:['1'],cells:5,keys:['1-0']}),
  mk('a1','MESH250',{pts:[[139.8,35.7]],enbs:['9'],areas:[A('MESH250',['m1']),A('ADM3',['t1'])]}),
  mk('n1','NODE',{nodes:['mme:1']}), mk('p1','APN',{axis_vals:['ims']})],{egress:false});
const v=f=>{ PART=partGroups(h).findIndex(g=>g.form===f); return viewOf(h); };
const c=v('CELL'), a=v('AREA'), n=v('NODE'), p=v('APN');
console.log(JSON.stringify({cell:[c.form,c.enbs,c.nodes,c.axis,c.form_areas,c.event_unit], area:[a.form,a.enbs,a.axis,(a.form_areas||[]).map(x=>x.axis),a.event_unit],
  node:[n.form,n.enbs,n.nodes], apn:[p.form,p.axis_raw,p.axis_vals,p.egress,p.nodes], cached:v('CELL')===c, evt:Object.keys(EVT)}));""")
    assert out["cell"][:5] == ["CELL", ["1"], [], None, None] and out["cell"][5] == "L1_BUNDLE"      # 셀 탭: 셀만
    assert out["area"][0] == "AREA" and out["area"][1] == [] and out["area"][2] == "MESH250"          # 지역 탭: 셀 없음(메시 조각에 셀이 실려 와도)
    assert out["area"][3] == ["MESH250"] and out["area"][4] is None                                     # 그 단계의 칸만(읍면동 칸은 뺀다)
    assert out["node"] == ["NODE", [], ["mme:1"]]                                                       # 장비 탭: 장비만
    assert out["apn"][0] == "APN" and out["apn"][1] == "APN" and out["apn"][2] == ["ims"] and out["apn"][3] is True and out["apn"][4] == []
    assert out["cached"] is True


@pytest.mark.skipif(NODE is None, reason="node 없음")
def test_view_of_without_tabs_returns_the_piece_itself(tmp_path):
    out = _run(tmp_path, """
const d={incident_id:'D1',axis_raw:'GEO',members:undefined};
console.log(JSON.stringify([viewOf(d)===d, partGroups(d).length]));""")
    assert out == [True, 0]


@pytest.mark.skipif(NODE is None, reason="node 없음")
def test_focus_sets_have_no_fire_rings_and_light_cells_only_on_a_cell_tab_and_equipment_only_on_equipment_tabs(tmp_path):
    out = _run(tmp_path, MK + """
const h=ev([mk('c1','GEO',{pts:[[139.7,35.6]],enbs:['1','2'],cells:5,keys:['1-0']}),
  mk('a1','MESH250',{areas:[{axis:'MESH250',geo_ids:['m'],dong:null}]}), mk('n1','NODE',{nodes:['mme:1','sgw:2']})],{enbs:['1','2']});
H.E1=h; FOCUS='E1';
const r={};
for(const f of ['CELL','AREA','NODE']){ PART=partGroups(h).findIndex(g=>g.form===f); const s=focusSets(); r[f]={form:s.form,E:[...s.E],V:[...s.V],N:[...s.N]}; }
FOCUS=null; r.none=focusSets();
console.log(JSON.stringify(r));""")
    for f in ("CELL", "AREA", "NODE"):
        assert out[f]["E"] == [], f                                       # 깜빡이 고리·후광(E)은 어느 탭에서도 비어 있다
    assert out["CELL"]["V"] == ["1", "2"] and out["CELL"]["N"] == []
    assert out["AREA"]["V"] == [] and out["AREA"]["N"] == []              # 지역 탭: 셀도 장비도 밝히지 않는다
    assert out["NODE"]["V"] == [] and out["NODE"]["N"] == ["mme:1", "sgw:2"]
    assert out["none"] is None


# ── 설명 줄 (formPlain) ───────────────────────────────────────────────────

F356 = r"""
const cells=n=>Array.from({length:n},(_, i)=>'c'+i);
// 몰림 115(80 + 꺼진 셀 중 35) · 꺼진 40 · 겹침 35 — F-356 값. eNB ID 는 셀 키 앞부분(c 키를 7 개씩 묶는다)
const key=i=>Math.floor(i/3)+'-'+(i%3);
const all=Array.from({length:120},(_, i)=>key(i));
const rush=all.slice(0,115), cut=all.slice(80,120);
const m1=mk('P1','GEO',{trigger_kpi:'C-24,C-01',keys:all,cells:120,title:'東京都 中央区, 東京都 港区 외 2곳 (시군구 5곳, 셀 120개)'});
const h=ev([m1]); H.E1=h;
const sm=[{incident_id:'P1',keys:all,silent:cut,cell_kpi:Object.fromEntries(rush.map(k=>[k,{'C-24':'MAJOR'}]))}];
"""


@pytest.mark.skipif(NODE is None, reason="node 없음")
def test_form_plain_counts_symptoms_overlap_and_enb_ids_from_the_cell_rows(tmp_path):
    out = _run(tmp_path, MK + F356 + """
SIL.E1={doc:{members:sm}}; PART=null;
const p=formPlain(h, partGroups(h)[0]);
console.log(JSON.stringify(p));""")
    assert out["what"] == "셀 120개 — 통화 몰림 115 · 통화 끊김(셀 정지 의심) 40 · 겹친 셀 35개"
    assert out["enb"] == 40                                               # 셀 키 앞부분(eNB ID) 묶음 수 — 위치가 아니다
    assert out["where"] == "中央区, 港区 외"
    assert not re.search(r"증상을 가르지 못한", out["what"])


@pytest.mark.skipif(NODE is None, reason="node 없음")
def test_form_plain_before_the_cell_states_arrive_has_names_only_and_never_a_made_up_count(tmp_path):
    out = _run(tmp_path, MK + F356 + """
const p=formPlain(h, partGroups(h)[0]);          // SIL 에 아직 없음
console.log(JSON.stringify(p));""")
    assert out["what"] == "셀 120개 — 통화 몰림 · 통화 시도 변화"              # 무호 셀을 모르니 C-01 은 중립 이름(끊김이라 단정하지 않는다)
    assert not re.search(r"\d+\s*[·]|겹친", out["what"].split("—")[1])


@pytest.mark.skipif(NODE is None, reason="node 없음")
def test_form_plain_names_a_kpi_without_cell_rows_without_a_zero_and_counts_unsplit_cells(tmp_path):
    out = _run(tmp_path, MK + """
const m=mk('P1','GEO',{trigger_kpi:'C-24,U-12',keys:['1-0','1-1','2-0'],cells:3}); const h=ev([m]);
SIL.E1={doc:{members:[{incident_id:'P1',keys:['1-0','1-1','2-0'],silent:[],cell_kpi:{'1-0':{'C-24':'MAJOR'}}}]}};
console.log(JSON.stringify(formPlain(h, partGroups(h)[0])));""")
    # U-12 는 셀 행이 없다 → 「HTTP 성공률 0」이 아니라 이름만. 1-1 · 2-0 은 어느 증상인지 가를 수 없다
    assert out["what"] == "셀 3개 — 통화 몰림 1 · HTTP 성공률 · 증상을 가르지 못한 셀 2개"


@pytest.mark.skipif(NODE is None, reason="node 없음")
def test_form_plain_for_region_equipment_and_other_forms(tmp_path):
    out = _run(tmp_path, MK + """
const A=(axis,ids)=>({axis,geo_ids:ids,dong:null});
const h=ev([mk('a','MESH250',{trigger_kpi:'U-12',areas:[A('MESH250',['m1','m2'])],title:'東京都 品川区 (시군구 1곳)'}),
  mk('n','NODE',{trigger_kpi:'C-24',nodes:['mme:2','pgw:4','sgw:5']}), mk('p','APN',{trigger_kpi:'U-12',axis_vals:['ims','internet','x','y']})]);
const w=f=>formPlain(h, partGroups(h).find(g=>g.form===f)).what;
console.log(JSON.stringify({area:w('AREA'), node:w('NODE'), apn:w('APN')}));""")
    assert out["area"] == "AREA 2칸 — HTTP 성공률"
    assert out["node"] == "코어 장비 MME 2, PGW 4, SGW 5 — 통화 몰림"
    assert out["apn"] == "APN ims, internet, x — HTTP 성공률"             # 축 값은 앞 3개만


@pytest.mark.skipif(NODE is None, reason="node 없음")
def test_form_plain_for_a_single_piece_uses_its_own_fields(tmp_path):
    out = _run(tmp_path, """
const d={incident_id:'D1',axis_raw:'GEO',trigger_kpi:'C-01',title:'東京都 台東区 (셀 1개)',cell_cnt:1,enbs:['5']};
SIL.D1={doc:{members:[{incident_id:'D1',keys:['5-0'],silent:['5-0'],cell_kpi:{'5-0':{'C-01':'CRITICAL'}}}]}};
console.log(JSON.stringify(formPlain(d,null)));""")
    assert out["what"] == "셀 1개 — 통화 끊김(셀 정지 의심) 1" and out["enb"] == 1 and out["where"] == "台東区"


# ── 막대(drawFocus) — 소스 구조 ──────────────────────────────────────────

def _focus_src():
    a = MAP_HTML.index("function drawFocus()")
    return MAP_HTML[a:a + 9000]


def test_bar_has_no_all_button_one_tab_per_form_and_codes_stay_in_tooltips():
    f = _focus_src()
    assert "T('전체')" not in f and "data-part=\"\" class=\"on\"" in f                         # 「전체」 단추는 없다(조각 하나일 때의 한 탭만)
    assert "formTag(g.form, formCount(base, g))" in f and "formTag(pieceForm(base), formCount(base, null))" in f
    assert "title=\"${esc(list.map(m=>(m.trigger_kpi||'')+' '+(m.title||'')).join('\\n'))}\"" in f     # 코드와 원래 제목은 툴팁
    assert "const ck=curPart(base), cg=ck!=null?gs[ck]:null;" in f and "formPlain(base, cg)" in f
    assert "<b>${T('무엇')}</b> ${esc(plain.what)}" in f and "<b>${T('어디서')}</b> ${esc(plain.where)}" in f
    assert "T('eNB ID {n}',{n:plain.enb.toLocaleString()})" in f                                  # eNB 가 아니라 eNB ID
    assert "기지국" not in f.split("bar.innerHTML")[1][:2500]


def test_bar_draws_no_extent_outline_or_wave_for_a_cell_tab():
    f = _focus_src()
    assert "const F=fs.form, areas=h.form_areas||(AREA_FORMS.has(F)?null:[]);" in f           # 지역 탭이 아니면 칸을 그리지 않는다
    assert "drawEvt(" not in f                                                                  # 셀 범위 윤곽선 없음


def test_the_tab_click_handler_selects_a_form_and_redraws_without_an_all_option():
    src = MAP_HTML
    assert re.search(r"data-part", src) and "PART=" in src
    handler = src[src.index("[data-part]") - 200:src.index("[data-part]") + 700] if "[data-part]" in src else ""
    assert "T('전체')" not in handler


# ── 이식 메모 가 말한 소스 이름이 실제로 있는가 ───────────────────────────

@pytest.mark.parametrize("name", ["formOf", "pieceForm", "partGroups", "curPart", "curForm", "formCount", "formTag", "formPlain",
                                  "viewOf", "focusSets", "silentOf", "drawDown", "legendDown", "downShape", "hatchFit"])
def test_functions_named_in_the_porting_memo_exist_exactly_once(name):
    n = len(re.findall(rf"^(?:async )?function {name}\(|^const {name}=", MAP_HTML, re.M))
    assert n == 1, (name, n)
