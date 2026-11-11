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
    assert len(a["report"]["scenarios"]) == 27


def test_50_has_27_scenarios_and_judged_ones_carry_50_runs():
    d = ui.api_scenario_report(50)
    assert d["ok"] and d["report"] == load("scenario_report_v50.json")
    sc = d["report"]["scenarios"]
    assert len(sc) == 27 and sorted(s["no"] for s in sc) == list(range(1, 28))
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
        for k in ("name", "kind", "what", "setup", "expect"):
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
    assert d["ok"] and "report_ja" not in d and len(d["report"]["scenarios"]) == 27


# ── 화면 ────────────────────────────────────────────────────────────────────

def test_page_has_tabs_and_both_definition_lines_with_japanese():
    h = ui._HTML_SCEN
    assert 'id=vol' in h.replace('"', "") and "volnote" in h
    assert "분당 XDR 약 3천 건" in h and "분당 XDR 약 1만 건 · 15%의 약 3.3배" in h


def _script() -> str:
    m = re.findall(r"<script[^>]*>(.*?)</script>", ui._HTML_SCEN, re.S)
    return max(m, key=len)


def _run_with(tmp_path, search, hash_, body):
    code = _script()
    tab = code[code.index("const VOLS="):code.index("function render(")]
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


@pytest.mark.skipif(NODE is None, reason="node 가 없다")
def test_a_late_response_of_the_old_tab_is_not_drawn(tmp_path):
    out = _run_with(tmp_path, "", "", """
const pend={};
globalThis.fetch=u=>new Promise(res=>{ pend[u]=()=>res({json:async()=>({ok:true,
  report:{scenarios:[{no:1,tag:u}]},how:{},timing:{},l3:[],l3_broken:0})}); });
(async()=>{
  const p15=load();                                   // 15% 요청이 늦게 온다
  el('vol').listeners.click({target:{closest:()=>({dataset:{vol:'50'}})}});   // 그 사이 50% 탭
  await new Promise(r=>setTimeout(r,0));
  pend['/api/scenario-report?volume=50']();
  await new Promise(r=>setTimeout(r,0));
  pend['/api/scenario-report']();                     // 이제 15% 응답이 도착
  await p15; await new Promise(r=>setTimeout(r,0));
  console.log(JSON.stringify({drawn,VOL,urls:Object.keys(pend),lastUrl}));
})();""")
    assert out["VOL"] == "50" and out["lastUrl"] == "/scenarios?vol=50"
    assert out["drawn"] == [["/api/scenario-report?volume=50"]], "옛 15% 응답이 그려지면 안 된다"


@pytest.mark.skipif(NODE is None, reason="node 가 없다")
def test_late_failure_of_the_old_tab_does_not_warn(tmp_path):
    out = _run_with(tmp_path, "", "", """
globalThis.fetch=u=>u.includes('volume')?Promise.resolve({json:async()=>({ok:true,report:{scenarios:[{no:1,tag:'v50'}]},how:{},timing:{},l3:[],l3_broken:0})})
  :new Promise((_,rej)=>setTimeout(()=>rej(new Error('x')),5));
(async()=>{
  const p=load(); el('vol').listeners.click({target:{closest:()=>({dataset:{vol:'50'}})}});
  await p; await new Promise(r=>setTimeout(r,20));
  console.log(JSON.stringify({warns:warns.filter(Boolean),drawn}));
})();""")
    assert out["warns"] == [] and out["drawn"] == [["v50"]]


@pytest.mark.skipif(NODE is None, reason="node 가 없다")
def test_vol_50_with_hash_opens_the_11_card_on_the_50_tab(tmp_path):
    out = _run_with(tmp_path, "?vol=50", "#no=11", """
globalThis.fetch=async u=>({json:async()=>({ok:true,report:{scenarios:[{no:11,tag:u}]},how:{},timing:{},l3:[],l3_broken:0})});
(async()=>{ await load(); console.log(JSON.stringify({VOL,open:[...OPEN],drawn,pick:PICK})); })();""")
    assert out["VOL"] == "50" and out["open"] == ["11"]
    assert out["drawn"][0] == ["/api/scenario-report?volume=50"]


@pytest.mark.skipif(NODE is None, reason="node 가 없다")
@pytest.mark.parametrize("search,want", [("", "15"), ("?vol=15", "15"), ("?vol=30", "15"), ("?vol=abc", "15"), ("?vol=50", "50")])
def test_initial_tab_from_url(tmp_path, search, want):
    out = _run_with(tmp_path, search, "", "console.log(JSON.stringify({VOL}));")
    assert out["VOL"] == want


@pytest.mark.skipif(NODE is None, reason="node 가 없다")
def test_server_reason_volume_is_shown_on_screen(tmp_path):
    out = _run_with(tmp_path, "", "", """
globalThis.fetch=async u=>({json:async()=>({ok:false,reason:'volume',volume:30})});
(async()=>{ await load(); console.log(JSON.stringify({warns,drawn})); })();""")
    assert out["drawn"] == [] and any("30" in w or "생성량" in w for w in out["warns"])


def test_japanese_texts_of_the_tab_exist_in_the_page_dictionary():
    h = ui._HTML_SCEN
    for ko in ("생성량 15%", "생성량 50%", "생성량", "분당 XDR 약 1만 건 · 15%의 약 3.3배"):
        assert re.search(re.escape(ko) + r"['\"]\s*:\s*['\"]", h), f"{ko} 의 일본어가 없다"
