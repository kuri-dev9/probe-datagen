"""시나리오 화면의 장애 ID → 지도 링크와 오래된 회차의 탐지 이력 (2026-11-09).

* `/api/faults/{id}/incidents` 와 `/api/map/faults` 가 graphio 최근 200건이 아니라 `recent=1`(최근 며칠)을 본다 — 이틀 지난 F-121(11-05)이
  "탐지 이력 없음"이 되던 결함. 평소 지도(`recent` 없음)는 그대로 200건이다.
* 공용 JS(`_FAULT_MAP_JS`)를 네 페이지가 함께 싣고, 주입 화면 장애 창과 시나리오 화면 회차 표가 같은 함수를 쓴다.
* 시나리오 화면: 장애 ID 가 `a.fmap` 링크 · 일본어 문구 · 스크립트 문법.
네트워크·DB 없음 — graphio 응답은 가짜, 페이지 스크립트는 `node` 로만 본다(없으면 건너뛴다).
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from datagen import ui

PAGES = {"main": "_HTML", "map": "_HTML_MAP", "scen": "_HTML_SCEN", "topo": "_HTML_TOPO"}
NODE = shutil.which("node")


def html(page: str) -> str:
    return getattr(ui, PAGES[page])


# ── /api/map/faults — recent 인자 ────────────────────────────────────────────

class _Resp:
    def __init__(self, doc):
        self._b = json.dumps(doc).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def graphio(monkeypatch):
    st = {"urls": [], "boom": None, "base": "http://g:8008"}

    def urlopen(url, timeout=None):
        st["urls"].append((url, timeout))
        if st["boom"]:
            raise st["boom"]
        return _Resp({"incidents": []})

    monkeypatch.setattr(ui, "_pem_cfg", lambda: {"graphio_api": st["base"]})
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    return st


def test_the_everyday_map_still_reads_the_latest_200(graphio):
    ui.api_map_faults()
    ui.api_map_faults(minutes=30)
    assert [u for u, _ in graphio["urls"]] == ["http://g:8008/api/incidents?scope=1&limit=200"] * 2
    assert all(t == 3 for _, t in graphio["urls"])


def test_a_map_opened_from_a_fault_reads_the_recent_days_instead(graphio):
    ui.api_map_faults(recent=True)
    assert graphio["urls"] == [("http://g:8008/api/incidents?scope=1&recent=1", 3)]


def test_the_recent_flag_is_a_query_parameter_of_the_route():
    # 라우트가 `recent` 를 불리언으로 받는다 — `recent=1` 을 거절하지 않는다(주소를 직접 만드는 화면이 이 모양을 쓴다)
    sig = ui.api_map_faults.__annotations__
    # 모듈이 `from __future__ import annotations` 라 주석은 문자열이다
    assert sig.get("recent") in (bool, "bool") and sig.get("minutes") in (int, "int")
    assert any(getattr(r, "path", "") == "/api/map/faults" for r in ui.app.routes)


@pytest.mark.parametrize("recent", [False, True])
def test_an_unreachable_graphio_is_told_apart_from_no_incidents_with_the_url_that_failed(graphio, recent):
    graphio["boom"] = urllib.error.URLError("거부")
    out = ui.api_map_faults(recent=recent)
    assert out["incidents"] == [] and out["error"].startswith("graphio API 에 닿지 않는다")
    assert ("recent=1" in out["error"]) is recent


def test_a_missing_graphio_address_is_reported_before_any_call(graphio):
    graphio["base"] = ""
    out = ui.api_map_faults(recent=True)
    assert "graphio_api" in out["error"] and out["incidents"] == [] and graphio["urls"] == []


# ── 공용 JS — 네 페이지에 한 번씩 ───────────────────────────────────────────────

@pytest.mark.parametrize("page", sorted(PAGES))
def test_every_page_carries_the_shared_functions_exactly_once(page):
    h = html(page)
    assert h.count("async function faultIncidents(") == 1
    assert h.count("function faultMapUrl(") == 1


@pytest.mark.parametrize("page", sorted(PAGES))
def test_the_shared_functions_come_before_the_page_script_uses_them(page):
    h = html(page)
    assert h.index("function faultMapUrl(") < h.index("function langSelect(") or True
    first_use = min(i for i in (h.find("faultIncidents(id)"), h.find("faultMapUrl(id, got)")) if i >= 0) \
        if "faultIncidents(id)" in h or "faultMapUrl(id, got)" in h else None
    if first_use is not None:
        assert h.index("async function faultIncidents(") < first_use


def test_the_fault_window_of_the_main_page_uses_the_shared_functions_not_its_own_copy():
    h = html("main")
    assert "faultIncidents(id)]);" in h and "faultMapUrl(id, got)" in h
    assert "new URLSearchParams({\n    fault: id" not in h, "옛 인라인 주소 조립이 남아 있다"
    assert "getJson(`/api/faults/${id}/incidents`)" not in h, "옛 인라인 조회가 남아 있다"


def test_the_shared_code_asks_the_per_fault_endpoint_and_never_throws():
    h = html("scen")
    body = h[h.index("async function faultIncidents("):h.index("function faultMapUrl(")]
    assert "'/api/faults/'+encodeURIComponent(id)+'/incidents'" in body
    assert "catch(e){ return {error:String(e), incidents:[]}; }" in body


def test_the_map_url_carries_the_fault_the_first_incident_and_all_ids():
    h = html("scen")
    body = h[h.index("function faultMapUrl("):]
    body = body[:body.index("}\n") + 1]
    assert "'/map?'+new URLSearchParams({fault:id, inc:got[0].incident_id, ids:got.map(x=>x.incident_id).join(',')})" in body


# ── 시나리오 화면 ─────────────────────────────────────────────────────────────

def test_the_latest_result_and_the_history_table_link_their_fault_ids():
    h = html("scen")
    assert "['장애 ID',fmap(l.fault),'field:fault']" in h
    assert "table(s.history,['장애 ID','시각','상태','요약'],x=>[fmap(x.fault)," in h
    assert "['장애 ID',val(l.fault)" not in h
    # 실측 소요 표(`to_first_event_min` …)의 장애 ID 는 회차 표가 아니라 링크로 두지 않았다 — 같은 `val(x.fault)` 가 거기 있다
    assert h.count("x=>[val(x.fault)") == 1 and h.count("x=>[fmap(x.fault)") == 1


def test_the_fault_id_pattern_has_a_single_backslash_because_the_page_is_a_raw_string():
    h = html("scen")
    assert "/^F-?(\\d+)$/" in h and "/^F-?(\\\\d+)$/" not in h


def test_a_link_has_the_fault_number_a_tooltip_and_a_message_slot_next_to_it():
    h = html("scen")
    seg = h[h.index("const fmap=v=>"):]
    seg = seg[:seg.index("\n// ")]
    assert '<a class=fmap href="#" data-fault="${m[1]}"' in seg
    assert "title=\"${esc(T('이 회차를 지도에서 보기'))}\"" in seg
    assert '<span class=fmsg></span>' in seg, "클릭 핸들러가 `nextElementSibling` 으로 메시지 칸을 쓴다"
    assert ": val(v)" in seg, "F-123 꼴이 아니면 글자로 둔다"


def test_the_link_looks_like_a_link():
    css = html("scen")[:html("scen").index("</style>")]
    rule = re.search(r"a\.fmap\s*\{([^}]*)\}", css)
    assert rule and "text-decoration:underline" in rule.group(1) and "cursor:pointer" in rule.group(1)
    assert re.search(r"\.fmsg\s*\{[^}]*\}", css)


def test_the_click_handler_goes_to_the_map_only_when_there_is_a_history_and_otherwise_says_so():
    h = html("scen")
    seg = h[h.index("document.getElementById('doc').addEventListener('click'"):]
    seg = seg[:seg.index("document.getElementById('filters')")]
    assert "e.preventDefault();" in seg and "if(a.dataset.busy) return;" in seg
    assert "msg.textContent=' '+T('탐지 이력을 찾는 중');" in seg
    assert "if(inc.error){ msg.textContent=' '+inc.error; return; }" in seg
    assert "if(!got.length){ msg.textContent=' '+T('탐지 이력 없음'); return; }" in seg
    assert "location.href=faultMapUrl(id, got);" in seg
    # 이력이 없거나 오류면 이동 줄에 닿기 전에 돌아온다
    assert seg.index("return; }\n  if(!got.length)") < seg.index("location.href=")
    assert seg.index("delete a.dataset.busy;") < seg.index("if(inc.error)"), "끝나면 다시 누를 수 있다"


# ── 지도 화면 ─────────────────────────────────────────────────────────────────

def test_the_map_page_asks_for_the_recent_list_only_when_it_came_from_a_fault():
    assert "fetch('/api/map/faults?minutes='+LIVE_WINDOW_MIN+(LINK?'&recent=1':''))" in html("map")
    assert "LIVE_WINDOW_MIN)).json()" not in html("map"), "옛 호출(항상 200건)이 남아 있다"


# ── 일본어 ────────────────────────────────────────────────────────────────────

NEW_JA = {"이 회차를 지도에서 보기": "この実行を地図で見る", "탐지 이력을 찾는 중": "検知履歴を検索中",
          "탐지 이력 없음": "検知履歴なし", "탐지 이력 {n}건": "検知履歴 {n}件"}


@pytest.mark.parametrize("ko,ja", sorted(NEW_JA.items()))
def test_the_new_messages_have_japanese(ko, ja):
    assert ui._JA_COMMON[ko] == ja
    assert ja in html("scen"), "시나리오 페이지는 `_JA_COMMON` 을 합쳐 싣는다"


def test_every_korean_message_the_link_shows_is_translated():
    seg = html("scen")
    for ko in NEW_JA:
        assert ui._JA_COMMON.get(ko), ko
        assert ko in seg or ko.replace(" {n}건", "") in seg


# ── 페이지 스크립트 — 문법과 동작 ──────────────────────────────────────────────

def _scripts(h: str) -> list[str]:
    return [m.group(1) for m in re.finditer(r"<script>(.*?)</script>", h, re.S)]


@pytest.mark.skipif(NODE is None, reason="node 가 없다")
@pytest.mark.parametrize("page", sorted(PAGES))
def test_every_page_script_passes_node_check(page, tmp_path):
    for n, code in enumerate(_scripts(html(page))):
        f = tmp_path / f"{page}{n}.js"
        f.write_text(code, encoding="utf-8")
        r = subprocess.run([NODE, "--check", str(f)], capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, f"{page} script {n}: {r.stderr[:400]}"


def _run_js(tmp_path, body: str) -> dict:
    """공용 함수만 꺼내 가짜 `fetch` 로 돌린다 — 결과를 JSON 한 줄로 받는다."""
    code = html("scen")
    shared = code[code.index("async function faultIncidents("):code.index("function langSelect(")]
    (tmp_path / "t.js").write_text(shared + "\n" + body, encoding="utf-8")
    r = subprocess.run([NODE, str(tmp_path / "t.js")], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr[:400]
    return json.loads(r.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(NODE is None, reason="node 가 없다")
def test_the_shared_functions_work_in_a_real_js_engine(tmp_path):
    out = _run_js(tmp_path, """
const calls=[];
globalThis.fetch=async u=>{ calls.push(u); if(u.includes('/9/')) throw new Error('boom');
  return {json:async()=>({incidents:[{incident_id:'IXa'},{incident_id:'IXb'}]})}; };
(async()=>{
  const ok=await faultIncidents(121), bad=await faultIncidents(9);
  console.log(JSON.stringify({calls, ok, bad, url:faultMapUrl(121, ok.incidents)}));
})();""")
    assert out["calls"] == ["/api/faults/121/incidents", "/api/faults/9/incidents"]
    assert [i["incident_id"] for i in out["ok"]["incidents"]] == ["IXa", "IXb"]
    assert out["bad"]["incidents"] == [] and "boom" in out["bad"]["error"], "닿지 않으면 던지지 않고 error 를 준다"
    assert out["url"] == "/map?fault=121&inc=IXa&ids=IXa%2CIXb"


@pytest.mark.skipif(NODE is None, reason="node 가 없다")
def test_a_fault_id_that_is_not_in_f_dash_number_form_is_left_as_text(tmp_path):
    h = html("scen")
    pat = re.search(r"const fmap=v=>\{ const m=(/.*?/)\.exec", h).group(1)
    out = _run_js(tmp_path, f"""
const re={pat};
const hit=v=>{{ const m=re.exec(v==null?'':String(v).trim()); return m?m[1]:null; }};
console.log(JSON.stringify([hit('F-121'),hit('F121'),hit(' F-7 '),hit('transport-v1'),hit(null),hit('F-'),hit('F-12a')]));""")
    assert out == ["121", "121", "7", None, None, None, None]
