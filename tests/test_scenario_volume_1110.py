"""시나리오 화면 생성량 탭 (2026-11-10 · 15% 정본 / 50% 따로).

* `/api/scenario-report?volume=` — 없으면 15 · 50 은 27건에 11번만 판정 · 모르는 값은 사유 `volume` · 파일이 없거나 깨지면 사유.
* 화면 — 탭을 오갈 때 늦게 온 옛 탭 응답을 그리지 않는다 · `?vol=50#no=11` · 일본어 문구 · 스크립트 문법.
DB·네트워크 없음 — 서버 함수는 직접 부르고 화면 스크립트는 `node` 로만 본다(없으면 건너뛴다).
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from datagen import ui

ROOT = Path(__file__).resolve().parents[1]
CONF = ROOT / "config"
NODE = shutil.which("node")


def load(name: str) -> dict:
    return json.loads((CONF / name).read_text(encoding="utf-8"))


# ── API ─────────────────────────────────────────────────────────────────────

def test_no_volume_is_the_same_as_15_and_is_the_base_file():
    a, b = ui.api_scenario_report(), ui.api_scenario_report(15)
    assert a["ok"] and a == b
    assert a["report"] == load("scenario_report.json")
    assert len(a["report"]["scenarios"]) == 35


def test_50_has_27_scenarios_and_judged_ones_carry_50_runs():
    d = ui.api_scenario_report(50)
    assert d["ok"] and d["report"] == load("scenario_report_v50.json")
    sc = d["report"]["scenarios"]
    assert len(sc) == 36 and sorted(s["no"] for s in sc) == list(range(1, 37))   # 28~35 신규(2026-11-19) · 36 신호 폭주(11-21)
    judged = [s for s in sc if s.get("latest") or s.get("history")]
    assert judged, "50% 재주행 결과가 하나도 없다"
    # 50% 회차는 `noN-` 으로 시작하는 이름이고(15번 러시 재주행은 `no15-r50-rush2` — runs.md 행 이름과 같다) 통과 기준(네 단계 모두 정상)은 15% 탭과 같다 — 통과가 있어도 되고 단계 상태가 뒷받침해야 한다
    for s in judged:
        assert str(s["latest"]["run"]).startswith(f"no{s['no']}-"), s["no"]
        if s.get("status") == "pass":
            assert all(v["state"] in ("ok", "na") for v in s["stages"].values()), s["no"]
    assert all(s.get("status") == "not_run" for s in sc if s not in judged)


def test_50_does_not_leak_15_judgements_and_15_does_not_get_50():
    p15 = {s["no"]: s for s in ui.api_scenario_report(15)["report"]["scenarios"]}
    p50 = {s["no"]: s for s in ui.api_scenario_report(50)["report"]["scenarios"]}
    assert sum(1 for s in p15.values() if s.get("status") == "pass") > 0
    runs15 = {s["latest"]["run"] for s in p15.values() if s.get("latest")}
    runs50 = {s["latest"]["run"] for s in p50.values() if s.get("latest")}
    assert runs50 and not (runs15 & runs50), "15% 회차와 50% 회차가 섞였다"
    for no in (1, 2, 5):
        # `what`·`setup` 은 뺀다 — 11-26 검수로 50% 카드(v50)의 설명·대상 문구만 현재 코드에 맞게 고쳤다(SGW→PGW 등).
        # 15% 기록(`scenario_report.json`)은 지난 회차 그대로라 문구가 갈라졌다. 이름·kind·기대 판정은 계속 같아야 한다
        for k in ("name", "kind", "expect"):
            assert p15[no][k] == p50[no][k], "설정·기준은 15% 와 같다"


def test_50_japanese_has_nothing_missing_or_stale():
    d = ui.api_scenario_report(50)
    assert d["ja_left"] == 0 and d["report_ja"]["scenarios"]
    assert ui.api_scenario_report(15)["ja_left"] == 0


@pytest.mark.parametrize("v", [30, 0, -1, 100])
def test_unknown_volume_gives_reason_volume(v):
    d = ui.api_scenario_report(v)
    assert d["ok"] is False and d["reason"] == "volume" and d["volume"] == v
    assert d["volumes"] == [15, 50] and "report" not in d


def test_route_declares_volume_as_int_query_parameter():
    import inspect
    sig = inspect.signature(ui.api_scenario_report)
    assert sig.parameters["volume"].default == 15 and sig.parameters["volume"].annotation in (int, "int")


def test_missing_50_file_gives_reason_not_exception(monkeypatch, tmp_path):
    monkeypatch.setitem(ui.SCENARIO_VOLUMES, 50, (tmp_path / "nope.json", tmp_path / "nope_ja.json"))
    d = ui.api_scenario_report(50)
    assert d == {"ok": False, "reason": "missing", "path": "config/nope.json"}


@pytest.mark.parametrize("body", ["{not json", '{"scenarios": 3}', "[]"])
def test_broken_50_file_gives_reason(monkeypatch, tmp_path, body):
    f = tmp_path / "v50.json"
    f.write_text(body, encoding="utf-8")
    monkeypatch.setitem(ui.SCENARIO_VOLUMES, 50, (f, tmp_path / "x_ja.json"))
    d = ui.api_scenario_report(50)
    assert d["ok"] is False and d["reason"] == "broken" and d["path"] == "config/v50.json"


def test_broken_50_file_does_not_break_the_15_tab(monkeypatch, tmp_path):
    f = tmp_path / "v50.json"
    f.write_text("{", encoding="utf-8")
    monkeypatch.setitem(ui.SCENARIO_VOLUMES, 50, (f, f))
    assert ui.api_scenario_report(15)["ok"] is True


def test_missing_50_translation_only_drops_the_japanese(monkeypatch, tmp_path):
    monkeypatch.setitem(ui.SCENARIO_VOLUMES, 50, (CONF / "scenario_report_v50.json", tmp_path / "no_ja.json"))
    d = ui.api_scenario_report(50)
    assert d["ok"] and "report_ja" not in d and len(d["report"]["scenarios"]) == 36   # 28~35 신규(2026-11-19) · 36(11-21)


# ── 화면 ────────────────────────────────────────────────────────────────────

def test_page_has_the_volume_note_and_both_definition_lines_with_japanese():
    h = ui._HTML_SCEN
    assert "volnote" in h
    assert "분당 XDR 약 3천 건" in h and "분당 XDR 약 1만 건 · 15%의 약 3.3배" in h


def _script() -> str:
    m = re.findall(r"<script[^>]*>(.*?)</script>", ui._HTML_SCEN, re.S)
    return max(m, key=len)


def _run_with(tmp_path, search, hash_, body):
    code = _script()
    tab = code[code.index("function mergeVols("):code.index("function render(")]       # 탭이 없다 — 두 생성량을 합치는 함수(2026-11-20)
    ld = code[code.index("async function load()"):code.index("// 주입 칸")]
    go = code[code.index("function goHash()"):code.index("addEventListener('hashchange'")]
    pre = f"""
const els={{}}; const el=id=>els[id]||(els[id]={{innerHTML:'',textContent:'',style:{{}},listeners:{{}},
  addEventListener(t,f){{this.listeners[t]=f;}}}});
globalThis.document={{getElementById:el, querySelector:()=>null}};
globalThis.location={{search:{json.dumps(search)},hash:{json.dumps(hash_)},pathname:'/scenarios'}};
globalThis.history={{replaceState(a,b,u){{globalThis.lastUrl=u;}}}};
globalThis.scrollBy=()=>{{}};
const esc=s=>String(s), T=(s,o)=>s;
let DOC=null,HOW={{}},TIMING={{}},RAW=null,PICK='all',JA_LEFT=0,HASH_DONE=false,L3LOG=[],L3BROKEN=0,LANG='ko';
const OPEN=new Set(); const drawn=[]; const warns=[];
function render(){{ drawn.push(DOC.scenarios.map(s=>s.tag)); }}
function warn(t){{ warns.push(t); }} function setGloss(){{}} function stamp(){{return '';}}
globalThis.addEventListener=()=>{{}};
"""
    f = tmp_path / "t.js"
    f.write_text(pre + tab + ld + go + "\n" + body, encoding="utf-8")
    r = subprocess.run([NODE, str(f)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr[:600]
    return json.loads(r.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(NODE is None, reason="node 가 없다")
def test_script_passes_node_check(tmp_path):
    f = tmp_path / "s.js"
    f.write_text(_script(), encoding="utf-8")
    r = subprocess.run([NODE, "--check", str(f)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr[:400]


def _doc(*nos, tag="x", at=None):
    return {"ok": True, "report": {"scenarios": [{"no": n, "tag": tag, **({"latest": {"at": at}} if at else {})} for n in nos]},
            "how": {}, "timing": {}, "l3": [], "l3_broken": 0}


@pytest.mark.skipif(NODE is None, reason="node 가 없다")
def test_the_two_volumes_are_merged_by_number_and_the_later_judgement_leads_the_list(tmp_path):
    out = _run_with(tmp_path, "", "", f"""
const m=mergeVols({{'15':{{scenarios:[{{no:1,latest:{{at:'2026-11-19 10:00'}}}},{{no:2,latest:{{at:'2026-11-20 03:00'}}}},{{no:3}}]}},
                    '50':{{scenarios:[{{no:1,latest:{{at:'2026-11-20 01:00'}}}},{{no:2,latest:{{at:'2026-11-19 12:00'}}}},{{no:4}}]}}}});
console.log(JSON.stringify(m.scenarios.map(s=>[s.no,s._vol,Object.keys(s._by).sort()])));""")
    got = {str(n): [v, by] for n, v, by in out}
    assert got["1"] == ["50", ["15", "50"]] and got["2"] == ["15", ["15", "50"]]       # 늦은 판정이 목록 줄
    assert got["3"] == ["15", ["15"]] and got["4"] == ["50", ["50"]]                    # 한쪽만 있으면 그쪽


@pytest.mark.skipif(NODE is None, reason="node 가 없다")
def test_load_reads_both_volumes_once_and_ignores_the_old_vol_query(tmp_path):
    out = _run_with(tmp_path, "?vol=50", "", """
const urls=[];
globalThis.fetch=async u=>{ urls.push(u); return {json:async()=>({ok:true,report:{scenarios:[{no:1,tag:u}]},how:{},timing:{},l3:[],l3_broken:0})}; };
(async()=>{ await load(); console.log(JSON.stringify({urls:urls.sort(),drawn,warns:warns.filter(Boolean)})); })();""")
    assert out["urls"] == ["/api/scenario-report", "/api/scenario-report?volume=50"]
    assert len(out["drawn"]) == 1 and out["warns"] == [], "탭이 없으므로 한 번만 그리고 ?vol= 은 받기만 한다"


@pytest.mark.skipif(NODE is None, reason="node 가 없다")
def test_one_volume_failing_draws_the_other_and_says_so(tmp_path):
    out = _run_with(tmp_path, "", "", """
globalThis.fetch=async u=>u.includes('volume')?{json:async()=>({ok:true,report:{scenarios:[{no:1,tag:'v50'}]},how:{},timing:{},l3:[],l3_broken:0})}
  :{json:async()=>{throw new Error('x');}};
(async()=>{ await load(); console.log(JSON.stringify({drawn,warns:warns.filter(Boolean)})); })();""")
    assert out["drawn"] == [["v50"]] and len(out["warns"]) == 1


@pytest.mark.skipif(NODE is None, reason="node 가 없다")
def test_hash_opens_the_card_of_the_merged_list(tmp_path):
    out = _run_with(tmp_path, "?vol=50", "#no=11", """
globalThis.fetch=async u=>({json:async()=>({ok:true,report:{scenarios:[{no:11,tag:u}]},how:{},timing:{},l3:[],l3_broken:0})});
(async()=>{ await load(); console.log(JSON.stringify({open:[...OPEN],drawn:drawn.length,pick:PICK})); })();""")
    assert out["open"] == ["11"] and out["pick"] == "all" and out["drawn"] >= 1


@pytest.mark.skipif(NODE is None, reason="node 가 없다")
def test_server_reason_volume_is_shown_on_screen(tmp_path):
    out = _run_with(tmp_path, "", "", """
globalThis.fetch=async u=>({json:async()=>({ok:false,reason:'volume',volume:30})});
(async()=>{ await load(); console.log(JSON.stringify({warns,drawn})); })();""")
    assert out["drawn"] == [] and any("30" in w or "생성량" in w for w in out["warns"])


def test_page_has_no_volume_tab_but_the_detail_boxes():
    h = ui._HTML_SCEN
    # 11-22 사용자 「그냥 표시하지 마」 — 생성량 칩·두 상자 구분(`VOL_KEYS`)을 뺐다. 목록과 같은 판정 한 벌만 보인다
    assert "id=vol>" not in h.replace('"', "") and "VOL_KEYS" not in h and "function mergeVols(" in h


def test_detail_has_no_verdict_card_and_no_found_card_but_the_paragraph():
    """11-24 사용자 「판정 카드와 최근 회차에서 시스템이 찾은 것을 카드로 놓지 말고 아래 상세 내용에 추가」.

    판정 칸(`volBoxes`)은 상세에서 부르지 않는다(정의만 남아 있다) — 판정은 아래 `volResult` 가 같은 값을 보인다.
    찾은 것은 별도 칸이 아니라 시나리오 설명 구역 안의 단락(`foundBox`)이다.
    """
    h = ui._HTML_SCEN
    assert h.count("volBoxes(") == 1, "판정 칸을 상세에서 다시 부르면 카드가 돌아온다(정의 한 번만 남아야 한다)"
    assert "sec(esc(T('시나리오 설명')),desc+foundBox(s))" in h.replace(" ", "") or "desc+foundBox(s)" in h
    assert h.count("foundBox(") == 2, "정의 1 + 시나리오 설명 구역 안 호출 1"
    assert "function volResult(" in h and "volResult(" in h.split("function volResult(", 1)[1]


def test_japanese_texts_of_the_tab_exist_in_the_page_dictionary():
    h = ui._HTML_SCEN
    for ko in ("생성량 15%", "생성량 50%", "생성량", "분당 XDR 약 1만 건 · 15%의 약 3.3배"):
        assert re.search(re.escape(ko) + r"['\"]\s*:\s*['\"]", h), f"{ko} 의 일본어가 없다"
