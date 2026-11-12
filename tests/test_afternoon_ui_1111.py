"""11-11 오후 gen-ui 수정분 고정 — 시나리오 링크의 생성량 탭 · 지도 범위 선(격자 닫기) · 지정시간 입력 검사 (맥스 수정 · 에스터 시험).

* `/api/scenario-fault-volume` — 그 장애의 판정 기록이 있는 생성량 탭(없으면 None).
* `map.html` 의 `evtClose`/`evtPath` — 점이 띄엄띄엄해도 한 덩어리 · 멀리 떨어진 무리는 두 덩어리 · 빈 점은 빈 선.
* `main.html` 의 `whenAt` — 빈 칸 · 없는 날짜 · 범위 밖 시·분 · 지난 시각 거부, `start_ts` 형식.
JS 는 `node` 로 함수만 떼어 돌린다(없으면 건너뛴다). DB·네트워크 없음.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from datagen import ui
from datagen.ui import scenarios_api

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "datagen" / "ui" / "static"
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(not NODE, reason="node 가 없다")


# ── 1. /api/scenario-fault-volume ───────────────────────────────────────────

def _report(tmp_path, name, faults):
    p = tmp_path / name
    p.write_text(json.dumps({"scenarios": [{"no": 1, "latest": {"fault": faults[0]},
                                            "history": [{"fault": f} for f in faults[1:]]}]}), encoding="utf-8")
    return p


@pytest.fixture
def two_tabs(tmp_path, monkeypatch):
    monkeypatch.setattr(scenarios_api, "SCENARIO_VOLUMES", {
        15: (_report(tmp_path, "r15.json", ["F-245", "F-226"]), None),
        50: (_report(tmp_path, "r50.json", ["F-278", "F-277"]), None)})


def test_fault_in_the_latest_of_50_is_found_on_50(two_tabs):
    assert scenarios_api.api_scenario_fault_volume("F-278") == {"fault": "F-278", "volume": 50, "volumes": [50]}


def test_fault_in_the_history_of_15_is_found_on_15(two_tabs):
    assert scenarios_api.api_scenario_fault_volume("F-226")["volume"] == 15


def test_unjudged_fault_has_no_volume(two_tabs):
    assert scenarios_api.api_scenario_fault_volume("F-9999") == {"fault": "F-9999", "volume": None, "volumes": []}


@pytest.mark.parametrize("raw", ["278", "f-278", " F-278 ", "f-278"])
def test_fault_id_is_normalised(two_tabs, raw):
    assert scenarios_api.api_scenario_fault_volume(raw)["fault"] == "F-278"
    assert scenarios_api.api_scenario_fault_volume(raw)["volume"] == 50


def test_fault_on_two_tabs_returns_all_and_uses_the_first(tmp_path, monkeypatch):
    monkeypatch.setattr(scenarios_api, "SCENARIO_VOLUMES", {
        15: (_report(tmp_path, "a.json", ["F-1"]), None), 50: (_report(tmp_path, "b.json", ["F-1"]), None)})
    assert scenarios_api.api_scenario_fault_volume("F-1") == {"fault": "F-1", "volume": 15, "volumes": [15, 50]}


def test_a_missing_or_broken_report_file_is_skipped(tmp_path, monkeypatch):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(scenarios_api, "SCENARIO_VOLUMES", {
        15: (bad, None), 50: (tmp_path / "nope.json", None), 99: (_report(tmp_path, "ok.json", ["F-7"]), None)})
    assert scenarios_api.api_scenario_fault_volume("F-7")["volume"] == 99


def test_real_files_place_the_known_runs():
    assert scenarios_api.api_scenario_fault_volume("F-278")["volume"] == 50
    assert scenarios_api.api_scenario_fault_volume("F-245")["volume"] == 15
    assert scenarios_api.api_scenario_fault_volume("F-999999")["volume"] is None


# ── 2. map.html 범위 선 ─────────────────────────────────────────────────────

def _slice(path: Path, start: str, end: str) -> str:
    s = path.read_text(encoding="utf-8")
    i = s.index(start)
    return s[i:s.index(end, i)]


def _run_js(code: str):
    r = subprocess.run([NODE, "-e", code], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


MAP_JS = None


def _map_runner(points_sets):
    src = _slice(STATIC / "map.html", "const EVT_R=", "//: 윤곽의 물결")
    return f"""
const mx=x=>x, my=y=>y; let LAST=null;
{src}
let RAW=null; const _ec=evtClose; evtClose=function(f,W,H){{ RAW=[Float32Array.from(f,v=>v>EVT_LV?1:0),W,H]; const g=_ec(f,W,H); LAST=[g,W,H]; return g; }};
function comps(){{ const [g,W,H]=LAST, seen=new Uint8Array(W*H); let n=0, area=0;
  for(let q=0;q<W*H;q++){{ if(g[q]>.5) area++; if(g[q]<=.5||seen[q]) continue; n++; const st=[q]; seen[q]=1;
    while(st.length){{ const v=st.pop(), i=v%W, j=(v/W)|0;
      for(const [a,b] of [[1,0],[-1,0],[0,1],[0,-1]]){{ const ii=i+a, jj=j+b; if(ii<0||jj<0||ii>=W||jj>=H) continue;
        const w=jj*W+ii; if(g[w]>.5&&!seen[w]){{ seen[w]=1; st.push(w); }} }} }} }} return [n,area]; }}
const out=[]; for(const pts of {json.dumps(points_sets)}){{ const ev={{pts}}; const d=evtPath(ev);
  const cl=LAST?comps():null; const sv=LAST; LAST=RAW; const raw=sv?comps():null; LAST=null; out.push({{d:d.length, fill:ev.fill.length, comp:cl, raw}}); }}
console.log(JSON.stringify(out));
"""


@needs_node
def test_sparse_points_along_a_road_become_one_blob():
    """점 사이가 원 둘레보다 벌어져(0.011°) 등고선만으로는 여럿으로 갈리는 경우 — 닫기가 한 덩어리로 잇는다."""
    chain = [[139.70 + 0.011 * i, 35.69] for i in range(6)]
    (r,) = _run_js(_map_runner([chain]))
    assert r["raw"][0] > 1, "닫기 전에는 여러 덩어리여야 이 시험이 뜻이 있다"
    assert r["comp"][0] == 1 and r["d"] > 0 and r["fill"] > 0


@needs_node
def test_two_far_apart_groups_stay_two_blobs():
    a = [[139.70 + 0.004 * i, 35.69] for i in range(3)]
    b = [[139.80 + 0.004 * i, 35.69] for i in range(3)]          # 0.1° ≈ 9km 떨어짐
    (r,) = _run_js(_map_runner([a + b]))
    assert r["comp"][0] == 2


@needs_node
def test_a_single_point_makes_a_small_closed_blob_not_nothing():
    (r,) = _run_js(_map_runner([[[139.70, 35.69]]]))
    assert r["comp"][0] == 1 and r["d"] > 0


@needs_node
def test_a_ring_keeps_its_hole():
    """노선 고리(15번)는 안쪽이 메워지지 않아야 한다 — 큰 반경의 점 고리는 구멍이 남는다."""
    import math
    ring = [[139.70 + 0.06 * math.cos(t / 60 * 2 * math.pi), 35.69 + 0.06 * math.sin(t / 60 * 2 * math.pi)] for t in range(60)]
    (r,) = _run_js(_map_runner([ring]))
    # 원 반지름 0.06° 에 점 반경 0.005° — 구멍 면적(칸)이 남아 있으면 전체 면적이 원판 면적보다 훨씬 작다
    disk = math.pi * (0.06 / 0.0018) ** 2
    assert r["comp"][0] == 1 and r["comp"][1] < disk * 0.6


@needs_node
def test_empty_or_null_points_give_an_empty_path():
    out = _run_js(_map_runner([[], [[None, 35.0], [139.0, None]]]))
    assert [o["d"] for o in out] == [0, 0]


@needs_node
def test_closing_adds_little_area_to_a_dense_blob():
    """점이 촘촘하면 닫기가 면적을 거의 늘리지 않는다 — 모양은 지키고 틈만 메운다."""
    dense = [[139.70 + 0.003 * i, 35.69 + 0.003 * j] for i in range(6) for j in range(6)]
    (r,) = _run_js(_map_runner([dense]))
    assert r["comp"][0] == 1 and r["raw"][0] == 1
    assert r["comp"][1] <= r["raw"][1] * 1.2


def test_graphio_and_gen_ui_share_the_closing_constants():
    """두 지도는 같은 수식이어야 한다 — 상수와 닫기 반경이 한쪽만 바뀌면 같은 사건이 다르게 그려진다."""
    web = (ROOT.parent / "graphio-probe-rca" / "web" / "src" / "components" / "IncidentMap.tsx")
    if not web.exists():
        pytest.skip("graphio 저장소가 옆에 없다")
    m = (STATIC / "map.html").read_text(encoding="utf-8")
    t = web.read_text(encoding="utf-8")
    for token in ("EVT_CLOSE = 2", "0.006", "0.0018", "0.35"):
        norm_t = t.replace(" ", "")
        assert token.replace(" ", "") in norm_t or token in t, token
    assert re.search(r"EVT_R=\.006, EVT_G=\.0018, EVT_LV=\.35, EVT_CLOSE=2", m)
    assert "else if (n === 5)" in t and t.count("(a + b + c + d) / 4 > LV") == 2     # 안장 칸 처리(5·나머지 10)가 둘 다 있다
    assert "case 5:" in m and "case 10:" in m and "/4>LV" in m


# ── 3. main.html 지정시간 검사 ──────────────────────────────────────────────

def _when_runner(vals, when="at", now="2026-11-11T11:30:00"):
    src = _slice(STATIC / "main.html", "function whenAt()", "loadKinds();")
    return f"""
const p2=n=>String(n).padStart(2,"0");
const T=(s,o)=>o?s.replace(/\\{{(\\w+)\\}}/g,(_,k)=>o[k]):s;
const msg={{className:"",textContent:""}};
const V={json.dumps(vals)};
const $=id=>id==="msg"?msg:({{value:V[id]}});
const document={{querySelector:()=>({{value:{json.dumps(when)}}})}};
const _now=new Date({json.dumps(now)}).getTime(); Date.now=()=>_now;
{src}
const r=whenAt(); console.log(JSON.stringify({{r, msg:msg.textContent, cls:msg.className}}));
"""


def _vals(y="2026", mo="11", d="11", h="11", mi="41"):
    return {"at-y": y, "at-mo": mo, "at-d": d, "at-h": h, "at-mi": mi}


@needs_node
def test_a_valid_future_time_returns_start_ts_in_the_server_format():
    r = _run_js(_when_runner(_vals()))
    assert r["r"] == "2026-11-11T11:41:00" and r["cls"] == ""


@needs_node
def test_values_are_zero_padded_in_start_ts():
    assert _run_js(_when_runner(_vals(mo="12", d="3", h="4", mi="5")))["r"] == "2026-12-03T04:05:00"


@needs_node
def test_now_mode_returns_null_and_touches_no_message():
    r = _run_js(_when_runner({}, when="now"))
    assert r["r"] is None and r["msg"] == ""


@needs_node
@pytest.mark.parametrize("empty", ["at-y", "at-mo", "at-d", "at-h", "at-mi"])
def test_an_empty_field_is_rejected(empty):
    v = _vals(); v[empty] = ""
    r = _run_js(_when_runner(v))
    assert r["r"] is False and "지정 시각을 고르세요" in r["msg"] and r["cls"] == "err"


@needs_node
@pytest.mark.parametrize("y,mo,d", [("2026", "2", "30"), ("2026", "13", "1"), ("2026", "11", "31"), ("2027", "2", "29"), ("2026", "0", "5")])
def test_a_nonexistent_date_is_rejected(y, mo, d):
    r = _run_js(_when_runner(_vals(y=y, mo=mo, d=d, h="12", mi="0"), now="2026-01-01T00:00:00"))
    assert r["r"] is False and "은 없는 날짜입니다" in r["msg"]


@needs_node
def test_leap_day_is_accepted():
    r = _run_js(_when_runner(_vals(y="2028", mo="2", d="29", h="12", mi="0"), now="2026-01-01T00:00:00"))
    assert r["r"] == "2028-02-29T12:00:00"


@needs_node
@pytest.mark.parametrize("h,mi", [("24", "0"), ("-1", "0"), ("12", "60"), ("12", "-1")])
def test_hour_and_minute_out_of_range_are_rejected(h, mi):
    r = _run_js(_when_runner(_vals(h=h, mi=mi)))
    assert r["r"] is False and "00~23" in r["msg"]


@needs_node
@pytest.mark.parametrize("h,mi", [("0", "0"), ("23", "59")])
def test_hour_and_minute_edges_are_accepted(h, mi):
    assert _run_js(_when_runner(_vals(y="2026", mo="11", d="12", h=h, mi=mi)))["r"].startswith("2026-11-12T")


@needs_node
def test_a_past_time_is_rejected_and_the_current_minute_counts_as_past():
    r = _run_js(_when_runner(_vals(h="11", mi="29")))
    assert r["r"] is False and "이미 지났습니다" in r["msg"]
    r = _run_js(_when_runner(_vals(h="11", mi="30")))                  # 지금 이 분 — 분 단위라 지난 것으로 본다
    assert r["r"] is False and "이미 지났습니다" in r["msg"]
    assert _run_js(_when_runner(_vals(h="11", mi="31")))["r"] == "2026-11-11T11:31:00"


@needs_node
def test_non_integer_values_are_rejected():
    r = _run_js(_when_runner(_vals(h="11.5")))
    assert r["r"] is False


# ── 4. 마감 수정 — 장애 창 링크(`scnHref`)·판정 전 안내(`fromFault`)·`EVT-…` 이름만(`evtn`) ──────────────────

ESC_JS = "const esc=s=>String(s).replace(/[&<>\"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;'}[c]));"
T_JS = "const T=(s,o)=>o?s.replace(/\\{(\\w+)\\}/g,(_,k)=>o[k]):s;"


def _scn_href(no, vol, fault=None):
    src = _slice(STATIC / "main.html", "const scnHref", "async function howOf")
    return _run_js(f"{src}\nconsole.log(JSON.stringify(scnHref({json.dumps(no)},{json.dumps(vol)},{json.dumps(fault)})));")


@needs_node
@pytest.mark.parametrize("no,vol,fault,want", [
    (9, 50, "F-285", "/scenarios?vol=50#no=9&f=F-285"),
    (9, 15, "F-265", "/scenarios#no=9&f=F-265"),
    (9, 50, None, "/scenarios?vol=50#no=9"),
    (9, 15, None, "/scenarios#no=9"),
    ("23", "50", "F-249", "/scenarios?vol=50#no=23&f=F-249"),      # 문자열 생성량도 50 으로 읽는다
])
def test_scn_href_carries_the_fault_id_and_the_volume_tab(no, vol, fault, want):
    assert _scn_href(no, vol, fault) == want


@needs_node
def test_scn_href_encodes_what_it_is_given():
    assert "%26" in _scn_href("1&2", 15, "F-1")


def _from_fault(hash_, s):
    src = _slice(STATIC / "scenarios.html", "let FROM_FAULT=null;", "function warn(")
    return _run_js(f"""{ESC_JS}\n{T_JS}\nconst unk=()=>'?';\n{src}
FROM_FAULT={json.dumps(hash_)}; console.log(JSON.stringify(fromFault({json.dumps(s)})));""")


_CARD = {"no": 9, "latest": {"fault": "F-265"}, "history": [{"fault": "F-151"}]}


@needs_node
def test_from_fault_warns_when_the_fault_has_no_record_yet():
    out = _from_fault({"no": "9", "f": "F-285"}, _CARD)
    assert "F-285" in out and "아직 판정 기록이 없다(판정 전)" in out and "F-265" in out and out.startswith("<p class=unk>")


@needs_node
@pytest.mark.parametrize("f", ["F-265", "F-151"])
def test_from_fault_is_silent_when_the_fault_is_in_latest_or_history(f):
    assert _from_fault({"no": "9", "f": f}, _CARD) == ""


@needs_node
def test_from_fault_is_silent_for_another_card_and_without_a_fault():
    assert _from_fault({"no": "8", "f": "F-285"}, _CARD) == ""
    assert _from_fault(None, _CARD) == ""


@needs_node
def test_from_fault_says_unknown_when_the_card_has_no_latest():
    out = _from_fault({"no": "9", "f": "F-285"}, {"no": 9})
    assert "미확인" in out


@needs_node
def test_from_fault_does_not_match_a_fault_that_only_shares_a_prefix():
    """`F-28` 이 `F-285` 에 걸리지 않는다 — 숫자 전체로 센다"""
    out = _from_fault({"no": "9", "f": "F-26"}, _CARD)
    assert "아직 판정 기록이 없다" in out


def test_hash_pattern_reads_no_and_optional_fault_only():
    src = (STATIC / "scenarios.html").read_text(encoding="utf-8")
    m = re.search(r"const no=(/.*?/)\.exec\(location\.hash\)", src)
    assert m, "goHash 의 해시 정규식을 찾지 못했다"
    if not NODE:
        pytest.skip("node 가 없다")
    out = _run_js(f"const re={m.group(1)}; console.log(JSON.stringify(['#no=9','#no=9&f=F-285','#no=9&f=285','#no=&f=F-1','#x=9','#no=9&f=F-285&z=1']"
                  ".map(h=>{const r=re.exec(h); return r&&[r[1],r[2]||null];})));")
    assert out == [["9", None], ["9", "F-285"], None, None, None, None]


@needs_node
def test_go_hash_remembers_the_fault_and_opens_the_card():
    src = _slice(STATIC / "scenarios.html", "let FROM_FAULT=null;", "addEventListener('hashchange'")
    src = src.replace(src[src.index("function fromFault"):src.index("function warn(")], "") if "function warn(" in src else src
    code = f"""
const location={{hash:'#no=9&f=F-285'}}; let DOC={{}}, PICK='x', rendered=0; const OPEN=new Set(['3','notes']);
function render(){{ rendered++; }}
const document={{querySelector:()=>null}}; const window={{scrollBy(){{}}}};
{_slice(STATIC / "scenarios.html", "let FROM_FAULT=null;", "function fromFault")}
{_slice(STATIC / "scenarios.html", "function goHash(){", "addEventListener('hashchange'")}
goHash(); console.log(JSON.stringify({{ff:FROM_FAULT, open:[...OPEN].sort(), pick:PICK, rendered}}));
"""
    assert _run_js(code) == {"ff": {"no": "9", "f": "F-285"}, "open": ["9", "notes"], "pick": "all", "rendered": 1}


@needs_node
def test_go_hash_without_a_fault_clears_the_notice():
    code = f"""
const location={{hash:'#no=9'}}; let DOC={{}}, PICK='x'; const OPEN=new Set();
function render(){{}} const document={{querySelector:()=>null}}; const window={{scrollBy(){{}}}};
let FROM_FAULT={{no:'9',f:'F-285'}};
{_slice(STATIC / "scenarios.html", "function goHash(){", "addEventListener('hashchange'")}
goHash(); console.log(JSON.stringify(FROM_FAULT));
"""
    assert _run_js(code) is None


def _upgrade(els, events):
    src = _slice(STATIC / "scenarios.html", "const EVT_INFO={}", "new MutationObserver")
    return _run_js(f"""{ESC_JS}\n{T_JS}
const els={json.dumps(els)}.map(id=>({{dataset:{{evt:id}}, innerHTML:id}}));
const document={{querySelectorAll:()=>els.filter(e=>!e.dataset.done), querySelector:()=>null}};
async function fetch(u){{ return {{json:async()=>({{events:{json.dumps(events)}}})}}; }}
{src}
upgradeEvt().then(()=>console.log(JSON.stringify(els.map(e=>e.innerHTML))));""")


@needs_node
def test_evt_id_becomes_a_unit_name_without_a_link():
    (out,) = _upgrade(["EVT-aaaaaaaaaa"], {"EVT-aaaaaaaaaa": {"unit": "L1_BUNDLE"}})
    assert out == '<span class=evtn title="EVT-aaaaaaaaaa">장애</span>'
    assert "<a" not in out and "href" not in out


@needs_node
@pytest.mark.parametrize("unit,name", [("L1_BUNDLE", "장애"), ("L2_EVENT", "통합 장애"), ("PIECE", "이상징후")])
def test_each_unit_has_its_name(unit, name):
    (out,) = _upgrade(["EVT-bbbbbbbbbb"], {"EVT-bbbbbbbbbb": {"unit": unit}})
    assert f">{name}</span>" in out


@needs_node
def test_merged_event_tooltip_shows_where_it_went():
    (out,) = _upgrade(["EVT-cccccccccc"], {"EVT-cccccccccc": {"unit": "L2_EVENT", "merged_into": "EVT-dddddddddd"}})
    assert 'title="EVT-cccccccccc → EVT-dddddddddd"' in out


@needs_node
def test_unknown_event_or_unit_keeps_the_raw_id():
    out = _upgrade(["EVT-eeeeeeeeee", "EVT-ffffffffff"], {"EVT-ffffffffff": {"unit": "WHAT"}})
    assert out == ["EVT-eeeeeeeeee", "EVT-ffffffffff"]


@needs_node
def test_same_id_appearing_twice_is_looked_up_once_and_both_become_names():
    out = _upgrade(["EVT-1111111111", "EVT-1111111111"], {"EVT-1111111111": {"unit": "PIECE"}})
    assert out == ['<span class=evtn title="EVT-1111111111">이상징후</span>'] * 2


def test_event_names_have_a_dotted_underline_and_help_cursor_and_ja_text_for_the_notice():
    src = (STATIC / "scenarios.html").read_text(encoding="utf-8")
    assert re.search(r"\.evtn\s*\{[^}]*dotted[^}]*cursor:help", src)
    from datagen.ui import i18n_ja
    text = json.dumps(vars(i18n_ja), ensure_ascii=False, default=str)
    assert "아직 판정 기록이 없다(판정 전)" in text
