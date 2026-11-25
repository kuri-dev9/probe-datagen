"""3D 재생 — 정적 시험 (2026-11-25 · 동구 구현 · 에스터 시험).

사용자 지시: 「평소에는 3D 를 하지 않고 플레이를 누르면」 · 「기존 데이터로 보여 주는 형태만 3D」 · 「기존 재생은 그대로」.
그래서 고정하는 것 — three.js 는 **3D 버튼을 누를 때만** 받는다(처음 로딩에 없다) · CDN 을 쓰지 않는다(현장 폐쇄망) ·
`/ui3d/` 는 이름을 정해 둔 파일만 내보낸다 · 새 API 없이 기존 재생 API 세 개만 쓴다 · **모든 장애에서 열린다**(11-25 사용자
「미리 만든 시나리오에 데이터만 바꿔 쓰는 것」).
브라우저에서 실제로 그려지는 것은 이 시험이 보지 않는다(동구의 헤드리스 캡처로 확인).
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
from fastapi import HTTPException
from test_map_quake_mini_1115 import NODE

from datagen.ui import pages
from datagen.ui.i18n_ja import _JA_MAP

STATIC = Path(pages.__file__).resolve().parent / "static"
HTML = (STATIC / "map.html").read_text(encoding="utf-8")
JS = (STATIC / "replay3d.js").read_text(encoding="utf-8")
THREE = STATIC / "vendor" / "three"


# ── `/ui3d/` — 이름을 정해 둔 파일만 ─────────────────────────────────────────

FIVE = {"replay3d.js", "vendor/three/three.module.js", "vendor/three/three.core.js", "vendor/three/addons/OrbitControls.js",
        "vendor/three/LICENSE"}


def test_ui3d_serves_exactly_the_five_named_files():
    assert set(pages._UI3D) == FIVE                                      # 자유 카메라 부속 파일 하나만 늘었다
    for name in pages._UI3D:
        r = pages.ui3d(name)
        assert Path(r.path).resolve() == (STATIC / name).resolve() and Path(r.path).exists()


def test_orbit_controls_reads_only_the_local_three_module():
    oc = (THREE / "addons" / "OrbitControls.js").read_text(encoding="utf-8")
    imports = re.findall(r"^(?:\}|import[^\n]*)\s*from\s*'([^']+)'", oc, re.M)         # 주석 속 안내 문구는 빼고 실제 import 만
    assert imports == ["../three.module.js"] and "import(" not in re.sub(r"(?s)/\*.*?\*/", "", oc), imports                     # `from 'three'`(import map·CDN 필요)가 아니다
    assert not re.search(r"https?://(?!threejs\.org|github\.com)[^\s'\"]*\.js", oc)
    assert "0.187.0" in (THREE / "VERSION.txt").read_text(encoding="utf-8") and "OrbitControls" in (THREE / "VERSION.txt").read_text(encoding="utf-8")
    assert re.search(r"import\s*\{\s*OrbitControls\s*\}\s*from\s*'\./vendor/three/addons/OrbitControls\.js'", JS)


@pytest.mark.parametrize("bad", ["map.html", "../pages.py", "../static/map.html", "vendor/three/VERSION.txt", "vendor", "vendor/three",
                                 "vendor/three/addons/../three.core.js", "vendor/three/addons", "vendor/three/addons/Other.js",
                                 "", "scenarios.html", "replay3d.js/../map.html", "vendor/three/../../map.html", "%2e%2e/pages.py"])
def test_ui3d_refuses_everything_else(bad):
    with pytest.raises(HTTPException) as e:
        pages.ui3d(bad)
    assert e.value.status_code == 404


def test_media_types_are_javascript_for_modules():
    assert all(v == "application/javascript" for k, v in pages._UI3D.items() if k.endswith(".js"))
    assert pages._UI3D["vendor/three/LICENSE"].startswith("text/plain")


# ── three.js 는 눌렀을 때만, CDN 은 쓰지 않는다 ──────────────────────────────────

def test_map_page_does_not_load_three_or_the_3d_module_up_front():
    assert not re.search(r"<script[^>]+(three|replay3d|ui3d)", HTML)
    assert not re.search(r"<link[^>]+(modulepreload|preload)[^>]+(three|ui3d)", HTML)
    assert not re.search(r"^\s*import\s[^\n]*(three|ui3d)", HTML, re.M)                  # 정적 import 가 없다
    refs = re.findall(r"/ui3d/[\w./-]*", HTML)
    assert refs == ["/ui3d/replay3d.js"]                                                  # 한 곳뿐이고


def test_the_only_reference_is_a_dynamic_import_inside_the_button_handler():
    i = HTML.index("async function rp3dOpen")
    body = HTML[i:HTML.index("\n}\n", i)]
    # 주소에 내용 해시(`?v=`)를 붙인다 — 파일이 바뀌면 브라우저가 옛 캐시를 쓰지 못한다(사용자 11-25 「역 이름 필터링 안 된다」의 한 원인)
    assert "await import('/ui3d/replay3d.js?v=__R3D_V__')" in body
    assert HTML.count("import('/ui3d/replay3d.js") == 1


def test_the_import_version_is_the_content_hash_of_replay3d_js_filled_in_by_pages():
    """새 프로세스에서 확인한다 — 다른 시험이 `pages` 모듈 상태를 건드려도(전체 실행에서 그랬다) 시험이 흔들리지 않게"""
    import hashlib
    import os
    import sys
    want = hashlib.sha1((STATIC / "replay3d.js").read_bytes()).hexdigest()[:10]
    code = "from datagen.ui import pages; h = pages._HTML_MAP; print('__R3D_V__' in h, \"import('/ui3d/replay3d.js?v=%s')\" in h)" % want
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(p for p in sys.path if p)}
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, check=True).stdout.split()
    assert out == ["False", "True"]
    src = (STATIC.parent / "pages.py").read_text(encoding="utf-8")
    assert '.replace("__R3D_V__", hashlib.sha1((_STATIC / "replay3d.js").read_bytes()).hexdigest()[:10])' in src


def test_no_cdn_anywhere_in_the_3d_code():
    for text in (JS, HTML[HTML.index("async function rp3dOpen"):HTML.index("function rpStop")]):
        urls = [u for u in re.findall(r"https?://[^\s'\"`)]+", text) if "w3.org" not in u]
        assert urls == [], urls
    assert not re.search(r"unpkg|jsdelivr|cdnjs|googleapis", HTML + JS, re.I)
    assert re.search(r"from\s+'\./vendor/three/three\.module\.js'", JS)                    # 저장소 안의 파일만 읽는다


def test_three_is_vendored_with_license_and_version():
    assert (THREE / "three.module.js").stat().st_size > 100_000 and (THREE / "three.core.js").stat().st_size > 100_000
    lic = (THREE / "LICENSE").read_text(encoding="utf-8")
    assert "MIT License" in lic and "three.js authors" in lic
    ver = (THREE / "VERSION.txt").read_text(encoding="utf-8")
    assert "0.187.0" in ver and "MIT" in ver and "CDN" in ver
    assert "./three.core.js" in (THREE / "three.module.js").read_text(encoding="utf-8")[:20000]   # 두 파일이 한 벌이다


# ── 기존 재생 그대로 · 새 API 없음 · 예시는 하나 ──────────────────────────────────

def test_only_existing_replay_api_endpoints_are_used():
    assert re.findall(r"/api/[^\s`'\"]*", JS) and set(re.findall(r"/api/[a-z]+/", JS)) == {"/api/replay/", "/api/map/"}
    assert set(re.findall(r"/api/map/[a-z]+", JS)) == {"/api/map/layer", "/api/map/bldg"}   # 지도 레이어(높은 건물·역 띠)와 낮은 건물 칸만 — 새 API 는 없다
    got = set(re.findall(r"get\('([a-z]+)'\)", JS)) | set(re.findall(r"\bget\(\s*'([a-z]+)'", JS))
    assert got <= {"timeline", "timelapse", "route"} and got                              # 새 API 를 만들지 않았다
    from datagen.ui import replay_api as R
    names = {re.sub(r"\{\w+\}", "{id}", r.path) for r in R.router.routes}                # 서버에 그 세 길이 있다
    assert {"/api/replay/{id}/timeline", "/api/replay/{id}/timelapse", "/api/replay/{id}/route"} <= names


def test_3d_button_sits_next_to_the_existing_replay_button_which_is_unchanged():
    assert re.search(r"id=rpbtn class=rpb disabled>\$\{T\('▶ 재생'\)\}</button><button id=rp3dbtn class=rpb disabled>\$\{T\('▶ 3D 재생'\)\}</button>", HTML)
    assert "b.onclick=()=>{ if(RP&&RP.fid===fid) rpStop(); else rpStart(fid); };" in HTML       # 기존 재생 동작 그대로
    assert "rpStop(); }" in HTML[HTML.index("async function rp3dOpen"):HTML.index("function rpStop")] or "if(RP) rpStop();" in HTML


RP3D = HTML[HTML.index("async function rp3dOpen"):HTML.index("function rpStop")]
CODE = re.sub(r"(?m)//.*$", "", JS)                    # 주석을 뺀 코드 — 주석에 남은 옛 말이 시험을 속이지 않게


def test_every_fault_with_a_replay_opens_in_3d_and_nothing_is_tied_to_f607():
    assert "RP3D_READY" not in HTML and "예시 준비 중" not in HTML + JS             # 장애 제한과 그 안내가 없다
    body = HTML[HTML.index("async function rp3dOpen"):HTML.index("function rpStop")]
    assert not re.search(r"\b607\b", body) and not re.search(r"\b607\b", CODE)    # F-607 에만 맞춘 상수가 없다
    assert "b3.onclick=()=>rp3dOpen(fid,h)" in HTML and "b3.disabled=false" in HTML   # 재생이 이어지는 장애면 3D 버튼도 켠다
    assert "if(fid==null){ b.disabled=true;" in HTML                              # 이어지는 주입이 없을 때만 막힌다(그때 3D 도 비활성)


def test_a_fault_without_any_lit_cell_draws_the_route_only_for_route_injection_otherwise_just_a_notice():
    """셀 판정이 없을 때 — 노선 주입(`ROUTE`·궤도 있음)이면 그리고 안내 띠를 얹는다. 아니면 그리기 도구를 만들기 전에 안내만 띄우고 끝낸다."""
    assert "const noCell = !lit.size, expired = !!data.ta.expired;" in CODE
    assert re.search(r"const routeOk = /\^ROUTE/i\.test\(String\(opt\.target \|\| ''\)\) && !!\(data\.rt\.track && data\.rt\.track\.ring && data\.rt\.track\.ring\.length\);", CODE)
    i = CODE.index("if (noCell && !routeOk)")
    line = CODE[i:CODE.index("\n", i)]
    assert "return st" in line and "showMsg(" in line                                 # 노선 주입이 아니면 여기서 끝난다
    assert i < CODE.index("new THREE.WebGLRenderer")                                  # 그리기 도구를 만들기 전에
    assert "T('이 장애는 셀 판정이 없어 3D로 보여 줄 것이 없습니다')" in line
    assert re.search(r"lit\.add\(k\)", CODE) and "f.cells[k].state" in CODE             # 「켜진 셀」 = 판정 상태가 있는 셀
    # 노선 주입이면 계속 간다 — 안내 띠를 얹고 가운데 구간을 판정 대신 주입 구간으로
    j = CODE.index("if (noCell) { const d = document.createElement('div'); d.className = 'r3d-note';")
    assert "wrap.appendChild(d)" in CODE[j:j + 400]
    assert "const s0 = noCell ? (inF.length ? inF[0] : 0) : firstI" in CODE and "frames.map((f, i) => f.in_fault ? i : -1)" in CODE


def test_expired_and_missed_notices_are_never_mixed():
    exp = "이 장애의 판정 기록은 보관 기간이 지나 셀 그림이 없습니다 (남아 있는 기록: {e}부터)"
    miss = "장애를 주입했지만 이상으로 잡힌 셀이 없습니다 — 노선과 열차만 보여 줍니다"
    none = "이 장애는 셀 판정이 없어 3D로 보여 줄 것이 없습니다"
    for ko in (exp, miss, none):
        assert f"'{ko}'" in CODE and ko in _JA_MAP and not re.search(r"[가-힣]", _JA_MAP[ko])
    assert len({_JA_MAP[exp], _JA_MAP[miss], _JA_MAP[none]}) == 3
    assert "const expired = " in CODE or "expired = !!data.ta.expired" in CODE and "T('" + exp + "', { e: dt16(data.ta.anomaly_earliest) })" in CODE
    assert "showMsg(expired ? expMsg : T('" + none + "'))" in CODE                    # 노선이 아닌 장애 — 기록 지움이 먼저, 없으면 「셀 판정 없음」
    assert "d.textContent = expired ? expMsg : T('" + miss + "')" in CODE            # 노선 주입 — 같은 갈래
    assert "「7일」" not in CODE and not re.search(r"\b7일\b", exp + miss + none)       # 재생 자료에 없는 값(7일)을 문구에 쓰지 않는다
    assert "{t0}〜{t1} 주입({m}분) · 판정 기록 보관 기간 지남" in CODE and "{t0}〜{t1} 주입({m}분) · 탐지되지 않은 주입" in CODE


def test_route_and_trains_only_for_route_injection_with_a_track_file():
    assert "const onRoute = /^ROUTE/i.test(String(opt.target || ''))" in CODE
    assert "rt.track && rt.track.ring && rt.track.ring.length" in CODE
    assert "const ring = onRoute ? rt.track.ring : []" in CODE                   # 노선이 아니면 궤도를 아예 쓰지 않는다
    assert "const lineName = onRoute &&" in CODE
    # 열차 수·범례의 열차 줄이 노선 장면에만 — 노선 없는 장면에 열차나 노선 이름표가 없다
    assert re.search(r"onRoute\s*\?|!onRoute|if\s*\(\s*onRoute|onRoute\s*&&", CODE.replace("const lineName", ""))
    assert "target:f.target||''" in HTML                                           # 주입 대상을 3D 로 넘긴다


def test_map_hands_the_open_call_only_the_data_it_already_has():
    for k in ("fid:+fid", "target:", "geo:GEO", "rail:RAIL", "view:visRect(VB)", "snap:rp3dSnap", "wide:rp3dWide", "panels:rp3dPanels", "lang:LANG", "labels:LABELS"):
        assert k in RP3D, k
    assert "fetch(" not in RP3D                                                    # 새 요청이 없다


# ── 열고 닫기 · 자원 해제 · 조작 ───────────────────────────────────────────────

def test_open_close_api_and_escape_closes():
    assert re.search(r"export function close\(\)", JS) and re.search(r"export async function open\(", JS) and re.search(r"export function isOpen\(\)", JS)
    assert "e.key === 'Escape'" in JS and "st.close()" in JS
    assert "removeEventListener('keydown'" in JS or "removeEventListener(\"keydown\"" in JS     # 닫으면 키 감시도 푼다


def test_close_releases_controls_listeners_timers_and_gpu_resources():
    i = CODE.rindex("teardown = () => {\n")                                              # 앞의 빈 자리(`() => {}`)가 아니라 실제 정의
    block = CODE[i:CODE.index("\n  };", i)]
    for needle in ("cancelAnimationFrame(raf)", "ro.disconnect()", "clearTimeout(faTimer)", "removeEventListener('pointermove'",
                   "removeEventListener('pointerup'", "removeEventListener('pointerdown'", "ctl.dispose()",
                   "o.dispose", "disposables", "renderer.dispose()", "forceContextLoss"):
        assert needle in block, needle
    assert "teardown();" in CODE[CODE.index("const finish = () =>"):][:200]               # 닫을 때 반드시 부른다
    assert "wrap.remove(); style.remove();" in CODE and "CUR = null" in CODE              # 얹은 화면·스타일을 걷고 열린 것이 없게 한다
    assert "window.removeEventListener('keydown', onKey, true)" in CODE                   # 키 감시도 푼다


def test_dispose_list_collects_every_geometry_texture_and_material_it_creates():
    made = len(re.findall(r"new THREE\.(?:CanvasTexture|PlaneGeometry|CylinderGeometry|SphereGeometry|BufferGeometry|MeshBasicMaterial|ShaderMaterial|SpriteMaterial|LineBasicMaterial|PointsMaterial)\(", CODE))
    kept = len(re.findall(r"keep\(\s*new THREE\.", CODE))
    assert made > 5 and kept >= made - 3, (made, kept)                                    # 거의 전부 `keep` 으로 해제 목록에 든다


def test_wheel_and_mouse_on_the_3d_layer_do_not_reach_the_map():
    assert re.search(r"addEventListener\('wheel'[^)]*stopPropagation|stopPropagation[^;]*wheel|wheel[^\n]{0,80}stopPropagation", JS)
    assert re.search(r"(mousedown|pointerdown)[^\n]{0,120}stopPropagation", JS)


def test_closing_early_during_loading_is_safe():
    """자료를 받는 도중에 닫아도 이어서 그리지 않는다 — 구조(함수 모양)가 아니라 의미: 닫으면 alive 를 내리고, 기다림 뒤마다 본다."""
    assert re.search(r"const st = \{ close: \(\) => \{\s*if \(!alive\) return; alive = false;", CODE)
    assert re.search(r"if \(ready && closing\) closing\(\); else finish\(\);", CODE)   # 장면이 서기 전이면 틀만 걷는다
    awaits = [m.start() for m in re.finditer(r"\bawait\b|Promise\.all", CODE)]
    guards = [m.start() for m in re.finditer(r"if \(!alive\)|if \(alive\)", CODE)]
    assert awaits and guards
    first = CODE.index("await Promise.all")
    assert any(g > first and g - first < 900 for g in guards)                            # 자료를 받은 직후에 가드가 있다
    nearp = CODE.index("const nearCv = await nearP;")
    assert "if (!alive) { renderer.dispose(); return st; }" in CODE[nearp:nearp + 200]     # 바닥 그림을 기다린 뒤에도(그리기 도구를 풀고 나간다)
    assert "if (alive) showMsg(" in CODE                                                  # 실패 안내도 닫혔으면 띄우지 않는다
    assert "setTimeout(() => { if (alive) loadWide(); }" in CODE and "if (!alive) return;" in CODE


def test_all_constants_are_marked_as_initial_values():
    assert JS.count("초기값") >= 4


# ── 일본어 ────────────────────────────────────────────────────────────────────

def _korean_literals(code):
    return set(re.findall(r"'([^'\n]*[가-힣][^'\n]*)'", code))


def test_every_3d_phrase_has_a_japanese_translation_without_hangul():
    """3D 화면 코드와 지도 쪽 3D 진입부의 한글 문구는 모두 일본어가 있고, 일본어 값에 한글이 없다(일본어 화면의 한글 0)."""
    keys = _korean_literals(CODE) | _korean_literals(re.sub(r"(?m)//.*$", "", RP3D)) | {"▶ 3D 재생"}
    assert len(keys) > 40
    missing = sorted(k for k in keys if k not in _JA_MAP)
    assert missing == [], missing
    assert not [k for k in keys if re.search(r"[가-힣]", _JA_MAP.get(k, ""))]
    for k in keys:                                                                       # 자리표시자가 번역에서 빠지지 않는다
        assert sorted(re.findall(r"\{\w+\}", k)) == sorted(re.findall(r"\{\w+\}", _JA_MAP[k])), k


def test_the_old_obsolete_phrases_are_gone_from_the_code():
    for old in ("예시 준비 중", "이상 판정이 더 이상 없습니다 회복", "L2 통합장애 열림"):
        assert old not in CODE + HTML


# ── 판정이 꺼지는 분 — 「회복」이 아니라 「판정이 더 이상 없다」 · 기둥은 서서히 ───────────────────

def test_the_end_caption_says_no_more_judgements_never_recovered():
    assert "T('{t} 이상 판정이 더 이상 없습니다'" in CODE
    assert _JA_MAP["{t} 이상 판정이 더 이상 없습니다"] == "{t} 異常判定がなくなりました"
    assert not [k for k in _korean_literals(CODE) if "회복" in k]                        # 「회복」이 한 곳도 남지 않았다
    assert not [v for k, v in _JA_MAP.items() if k in _korean_literals(CODE) and "回復" in v]
    assert "T('{t} 장애 끝'" in CODE                                                       # 판정 끝을 못 찾으면 장애 끝 시각만 말한다


def test_a_lit_column_that_goes_out_falls_slowly_and_rises_fast():
    m = re.search(r"const FALL_MS = (\d+);", CODE)
    assert m and int(m.group(1)) >= 3000                                                  # 몇 초에 걸쳐
    assert "const b = c.hT < c.h ? fall : a;" in CODE                                     # 내려갈 때만 느린 계수
    assert "fall = 1 - Math.exp(-dt / (FALL_MS / 3))" in CODE
    assert "const a = 1 - Math.exp(-dt / 180)" in CODE                                    # 켜질 때는 지금처럼 빠르게
    assert "초기값" in JS[JS.index("const FALL_MS") - 400:JS.index("const FALL_MS")]


# ── 판정 기준 ≠ 평소 값 — timelapse 에 없는 평소 값을 만들지 않는다 ─────────────────────────

def test_baseline_is_labelled_judgement_standard_not_usual_value():
    ko = _korean_literals(CODE)
    assert [k for k in ko if "평소" in k] == ["{name} — 평소 상태"]                       # 장애 전 모습을 말하는 자막 하나뿐
    assert "T('판정 기준')" in CODE and _JA_MAP["판정 기준"] == "判定基準"
    assert not [k for k in ko if k != "{name} — 평소 상태" and "平常" in _JA_MAP[k]]       # 일본어에도 「平常 n」이 없다
    assert _JA_MAP["{name} — 평소 상태"] == "{name} — 平常時"
    # 이름표와 상자 모두 기준선은 프레임의 `baseline` 이고, 상자는 등급(심각·나빠짐)을 함께 적는다
    assert re.search(r"esc\(T\('판정 기준'\)\)\} \$\{fmtB\(r\.v\.baseline\)\}", CODE)
    assert re.search(r"T\('판정 기준'\)\)\} \$\{fmtB\(v\.baseline\)\}\(\$\{esc\(T\(st === 'severe' \? '심각' : '나빠짐'\)\)\}\)", CODE)
    assert not re.search(r"usual|normalValue|평소값|평상시 값", CODE)


def test_values_shown_come_from_frame_cells_only():
    i = CODE.index("const openTip") if "const openTip" in CODE else CODE.index("openTip")
    block = CODE[i:i + 3500]
    for field in ("v.value", "v.baseline", "v.denom", "kpis"):
        assert field in CODE
    assert "fetch(" not in block and "Math.random" not in CODE                           # 값을 지어내거나 따로 받지 않는다
    assert "T('이 분 판정 없음')" in CODE                                                  # 그 분 판정이 없으면 그렇게 말한다


# ── 지도와 이어지는 전환 · 창 숨기기 · VB 복원 ──────────────────────────────────────────

def test_panels_hide_on_open_and_come_back_on_close_through_the_map_helper():
    fn = HTML[HTML.index("function rp3dPanels"):HTML.index("function rpStop")]
    assert "b.add('r3d-tr','r3d-on')" in fn and "if(st==='lab') b.add('r3d-lab')" in fn
    assert "b.remove('r3d-on','r3d-lab')" in fn                                          # 되살림
    css = HTML[HTML.index("body.r3d-tr :is("):][:700]
    for panel in ("#fbar", "#rp", "#sel", "#incs", "#flist", "#legend", "#scale", "#mini"):
        assert panel in css, panel                                                        # 지도 위 창 목록
    assert "opacity:0;pointer-events:none" in css and "body.r3d-lab #labels{opacity:0}" in css
    # 3D 쪽 호출 순서 — 열 때 on → 첫 그림 뒤 lab → 닫을 때 off(교차 페이드 뒤 finish 에서도 off)
    assert CODE.index("panels('on')") < CODE.index("panels('lab')")
    assert CODE.count("panels('off')") >= 3
    assert "const panels = st => { try { opt.panels && opt.panels(st); } catch" in CODE        # 지도 쪽 사정으로 3D 가 죽지 않는다


def test_wide_floor_moves_the_map_only_for_a_moment_and_puts_it_back():
    fn = HTML[HTML.index("async function rp3dWide"):HTML.index("//: 지도 위 창 숨기기")]
    assert "const v0={...VB}, f0=VB_FREE;" in fn
    assert "try{ setVB(box,{free:true}); return await rp3dSnap(box, px, kk); }" in fn
    assert re.search(r"finally\{\s*setVB\(v0,\{free:f0\}\);\s*\}", fn)                 # 끝나면 — 실패해도 — 열기 전 보기로


def test_open_and_close_never_touch_the_map_view_directly():
    assert not re.search(r"\bsetVB\b|\bVB\b\s*=", CODE)                                  # 3D 쪽은 지도 보기를 바꾸지 않는다
    assert "opt.view" in CODE and "const view = opt.view && opt.view.w > 0 ? opt.view : null;" in CODE
    snap = HTML[HTML.index("async function rp3dSnap"):HTML.index("async function rp3dWide")]
    assert "getComputedStyle(" in snap and "tagName==='text'" in snap and "b[i].remove()" in snap     # 글자는 뺀다
    assert "gStn" in snap or "RP3D_GROUPS" in snap
    groups = re.search(r"const RP3D_GROUPS=\[([^\]]*)\]", HTML).group(1)
    assert "gAdm" in groups and "gRail" in groups and "gYmt" in groups and "gStn" in groups
    for notbg in ("gCells", "gMark", "gSel", "gPts"):
        assert notbg not in groups                                                         # 셀·표식·선택 그룹은 굽지 않는다


def test_closing_takes_three_seconds_interpolating_centre_distance_tilt_and_bearing_back_to_the_map_view():
    """닫기 — 3초에 걸쳐 가운데·거리·기울기·방위를 함께 풀어 위에서 본 지도 보기(원래 `VB`)로 돌아온 뒤 0.5초 교차 페이드(사용자 11-25 「3초로, 축척도」)."""
    assert re.search(r"const XFADE_MS = \d+, TILT_MS = \d+, BACK_MS = 3000;", CODE)         # 2초 안에 끝나던 것을 3초로
    assert re.search(r"const CLOSE_XFADE_MS = 500;", CODE)
    i = CODE.index("closing = () =>")
    block = CODE[i:CODE.index("teardown = () => {\n", i)]
    assert "if (phase === 'top')" in block                                                  # 아직 위에서 본 그대로면 바로 넘긴다
    assert "phase = 'back'" in block and "ctl.enabled = false" in block                      # 돌아오는 동안 손으로 못 움직인다
    assert "from = {" in block and all(k in block.split("from = {")[1][:260] for k in ("q,", "d:", "el:", "az:", "off:"))   # 지금 가운데·거리·기울기·방위·시야 밀기
    assert "Math.atan2(Math.sin(az0), Math.cos(az0))" in block                              # 방위는 가까운 쪽으로 돈다
    tail = block[block.index("phase = 'back'"):]
    assert tail.index("setTimeout(() => {") < tail.index("panels('off')") < tail.index("setTimeout(finish, CLOSE_XFADE_MS)")
    assert "}, BACK_MS);" in tail                                                           # 3초가 지난 뒤에 지도로 넘긴다
    assert "wrap.classList.add('pre')" in tail                                              # 교차 페이드
    j = CODE.index("} else if (phase === 'back') {")
    back = CODE[j:CODE.index("} else if (back) {", j)]
    assert "const k = ease(Math.min(1, (now - ph0) / BACK_MS));" in back                    # 3초 ease-in-out
    assert "pb.lerpVectors(from.q, topC, k)" in back                                         # 가운데 → 지도 가운데
    assert "Math.exp(Math.log(from.d) + (Math.log(dTop) - Math.log(from.d)) * k)" in back     # 거리는 로그 보간 — 축척이 자연스럽게
    assert "from.el + (EL_TOP - from.el) * k" in back and "from.az * (1 - k)" in back         # 기울기를 풀고 위에서 본 방위로
    assert "offK = from.off * (1 - k)" in back
    assert "dTop = mapH / 2 / Math.tan(fov / 2 * RAD)" in CODE                              # 도착점 = 열기 전 지도 보기가 꼭 들어오는 높이
    assert "from = null" in CODE or "let az = 0" in CODE


def test_first_frame_matches_the_map_view_then_tilts_with_the_transition_constants():
    assert re.search(r"const EL = 50, EL_TOP = 89\.9, LAP_CAM_MS = 60000;", CODE)       # 위에서 본 시점 · 50° · 한 바퀴 60초
    assert re.search(r"const XFADE_MS = \d+, TILT_MS = \d+, BACK_MS = \d+;", CODE)
    assert "mapH" in CODE and "view.h * KX" in CODE and "S(view.x + view.w / 2, view.y + view.h / 2)" in CODE
    assert "wrap.style.background = mapBg" in CODE and "getComputedStyle(host).backgroundColor" in CODE   # 바탕색도 지도 것
    assert "const web = " not in CODE and "KX = 111320 * Math.cos(lat0 * RAD)" in CODE     # 지도와 같은 웹 메르카토르 배율


def test_theme_follows_the_map_and_light_theme_uses_opaque_columns():
    assert "const light = opt.theme === 'light';" in CODE and "(light ? ' lt' : '')" in CODE
    assert ".r3d.lt{" in CODE and "document.body.dataset.theme" in RP3D
    assert re.search(r"light\s*\?[^\n]*(NormalBlending|AdditiveBlending)|AdditiveBlending[^\n]*light|light[^\n]*AdditiveBlending", CODE)
    assert "AdditiveBlending" in CODE                                                       # 어두운 배색은 가산 혼합 빛 기둥
    assert "0xdc2626" in CODE and "0xff4d4d" in CODE                                       # 밝음 진한 빨강 · 어둠 밝은 빨강


# ── 조작 — OrbitControls · 시간 이동 · 속도 · 3D 위 입력은 지도로 새지 않는다 ─────────────────────

def test_orbit_controls_limits_damping_and_auto_orbit_stops_on_touch():
    assert "const ctl = new OrbitControls(cam, renderer.domElement)" in CODE
    assert "ctl.enableDamping = true" in CODE
    assert "ctl.maxPolarAngle = 84 * RAD" in CODE                                           # 지면 아래로 못 내려간다(84° < 90°)
    assert "ctl.screenSpacePanning = false" in CODE                                         # 이동은 지면을 따라
    assert re.search(r"ctl\.minDistance = R \* [\d.]+; ctl\.maxDistance = Math\.max\(", CODE)   # 거리 한계는 장면 범위에서
    assert "ctl.addEventListener('start', () => { auto = false;" in CODE                     # 손대면 자동 공전을 멈춘다
    assert "ctl.enabled = false" in CODE and "ctl.enabled = true" in CODE                   # 전환이 끝나야 움직인다
    i = CODE.index("const goHome = () =>")
    assert "Math.atan2(cam.position.x - ctl.target.x, cam.position.z - ctl.target.z)" in CODE[i:i + 400]   # 가장 가까운 공전 각
    assert "if (phase === 'run') goHome()" in CODE and "auto = true" in CODE                # ⟲ 시점은 공전을 다시 켠다


def test_time_seek_by_bar_chart_and_keys_and_the_caption_is_picked_again():
    assert "goTo(barAt(e), true)" in CODE and "bar.addEventListener('pointerdown'" in CODE   # 진행 막대
    assert "ch.addEventListener('pointerdown'" in CODE and "goTo(Math.floor(" in CODE         # 추이 그래프
    assert "e.key === 'ArrowLeft' || e.key === 'ArrowRight'" in CODE and "(e.key === 'ArrowLeft' ? -1 : 1)" in CODE
    assert "e.key === ' ' || e.code === 'Space'" in CODE and "togglePlay()" in CODE
    assert "if (e.key === 'Escape')" in CODE and "st.close()" in CODE
    assert re.search(r"onKey[^\n]*\n[^\n]*Escape", CODE) and "window.addEventListener('keydown', onKey, true)" in CODE    # 캡처 단계에서 먼저
    assert "e.preventDefault(); e.stopPropagation();" in CODE[CODE.index("const onKey"):][:400]   # 지도로 키가 새지 않는다
    # 분을 옮기면 자막을 그 분의 장으로 다시 고른다
    j = CODE.index("const applyFrame")
    assert "showCap(" in CODE[j:CODE.index("const goTo")] and "drawChart(" in CODE[j:CODE.index("const goTo")]     # 옮긴 분의 자막·그래프
    k = CODE.index("const showCap")
    assert "if (c.at <= upto) k = i" in CODE[k:k + 300]                                     # 그 시각까지 온 가장 뒤의 장 — 지난 장도 다시 보인다


def test_speed_buttons_and_restart():
    assert "const SPEEDS = [.5, 1, 2, 4]" in CODE
    assert "tick += dt * speed" in CODE and "speed = +b.dataset.v" in CODE
    assert "$('.r3d-home').onclick = () => goTo(0, true)" in CODE                           # ⏮ 처음부터
    assert "if (ended) { showHint(false); goTo(0, true); setPlay(true); return; }" in CODE   # 끝난 뒤 ↺ — 조작 안내를 걷고 3D 를 다시 열지 않는다
    assert "outro.t += dt * speed" in CODE                                                    # 속도 버튼은 끝마무리에도 곱해진다
    assert "outro.t >= OUT_HOLD_MS + OUT_PULL_MS) { ended = true; setPlay(false); showHint(true); }" in CODE


def test_the_outro_holds_the_peak_pulls_back_then_stops_without_closing():
    """끝마무리 — 가장 넓던 분을 2.5초 붙잡고 7초 동안 멀어진 뒤 ↺ 로 멈춘다. 자동으로 닫지 않는다."""
    assert re.search(r"const OUT_HOLD_MS = 2500, OUT_PULL_MS = 7000, OUT_ZOOM = 1\.4;", CODE)
    i = CODE.index("const startOutro = () =>")
    start = CODE[i:CODE.index("\n", i)]
    assert "goTo(noCell ? (mPk >= 0 ? mPk : s1) : peakI)" in start and "outro = { t: 0 }" in start and "capOver = sumCap" in start
    assert "if (fi < last) goTo(fi + 1); else startOutro();" in CODE                       # 마지막 분 뒤에 끝마무리
    k = CODE.index("else if (outro) {")
    block = CODE[k:CODE.index("} else {", k)]
    assert "ended = true; setPlay(false)" in block and "close" not in block and "finish" not in block   # 멈추기만 한다
    assert re.search(r"const zT = outro \? 1 \+ \(OUT_ZOOM - 1\) \* ease\(", CODE)          # 붙잡은 뒤 멀어진다(ease)
    assert "outro = null;" in CODE[CODE.index("const goTo"):CODE.index("const startOutro")]   # 시간을 옮기면 끝마무리가 풀린다
    assert "capI = 'over'" in CODE                                                           # 요약 자막은 한 장으로 붙는다


def test_summary_caption_is_built_from_timeline_and_frames_with_a_place_and_the_cause():
    i = CODE.index("const sumCap = noCell ?")
    block = CODE[i:i + 1800]
    assert "T('{t0}〜{t1}({m}분) · 최대 {n}셀'" in block and "t0: hhmm(frames[firstI].t), t1: hhmm(frames[lastI].t), m: nB, n: cnt[peakI].n" in block
    assert "T('주로 {p} 부근'" in block and "T('원인: {c}', { c: causeNm })" in block
    assert "placesOf(litAt(peakI), 3)" in block                                              # 가장 넓던 분의 장소
    for k in ("{t0}〜{t1}({m}분) · 최대 {n}셀", "주로 {p} 부근", "원인: {c}"):
        assert k in _JA_MAP


# ── 세 구간 시간 배분 ───────────────────────────────────────────────────────────────────────

def _const(name):
    m = re.search(rf"\b{name} = ([\d.]+)\b", CODE)                                           # 한 줄에 여러 상수를 선언하는 곳이 있다
    assert m, name
    return float(m.group(1)) if "." in m.group(1) else int(m.group(1))


def _plan(nA, nB, nC, noCell=False):
    seg, ac, b = _const("SEG_MS"), [500, 3000], [2400, 4500]
    ac = [float(x) for x in re.search(r"const AC_STEP = \[(\d+), (\d+)\]", CODE).groups()]
    b = [float(x) for x in re.search(r"B_STEP = \[(\d+), (\d+)\]", CODE).groups()]
    clamp = lambda v, r: min(r[1], max(r[0], v))                                              # noqa: E731
    sa = clamp(seg / nA, ac) if nA else 0
    sb = clamp(seg / nB, [ac[0], b[1]] if noCell else b)
    sc = clamp(seg / nC, ac) if nC else 0
    return nA * sa / 1000, nB * sb / 1000, nC * sc / 1000, sb / 1000


def test_three_segments_get_about_the_same_screen_time_and_b_is_never_faster_than_half_speed():
    assert _const("SEG_MS") == 13000 and _const("C_TAIL_MIN") == 2
    a, b, c, stepb = _plan(7, 7, 6)                                                           # F-607 — 헤드리스 보고의 설계값
    assert (round(a, 1), round(b, 1), round(c, 1)) == (13.0, 16.8, 13.0) and round(stepb, 1) == 2.4
    a2, b2, c2, _ = _plan(8, 5, 6)                                                            # F-600
    assert (round(a2, 1), round(b2, 1), round(c2, 1)) == (13.0, 13.0, 13.0)
    out = (_const("OUT_HOLD_MS") + _const("OUT_PULL_MS")) / 1000
    assert round(a + b + c + out, 1) == 52.3 and round(a2 + b2 + c2 + out, 1) == 48.5       # 보고한 시간표
    for n in (1, 3, 7, 15, 40):                                                              # B 는 한 분 2.4초(0.5배)보다 빠르지 않다
        assert _plan(5, n, 5)[3] >= 2.4
    assert _plan(5, 40, 5)[1] > 13                                                           # 분이 많으면 B 가 길어지는 쪽을 택한다(하한 우선)
    assert _plan(40, 7, 40)[0] <= 40 * 3.0 and _plan(40, 7, 40)[0] > 13                      # A·C 는 빠르게 하되 0.5초/분 아래로는 안 간다


def test_empty_minutes_after_the_verdict_are_cut_and_the_timeline_keeps_clock_minutes():
    assert "const endI = Math.min(frames.length - 1, vI > s1 ? vI : s1 + C_TAIL_MIN);" in CODE   # 원인 판정 분까지 · 먼저 났으면 판정 끝 뒤 2분
    assert "const NF = endI + 1;" in CODE and "nC = endI - s1;" in CODE
    assert "const stepOf = i => i < s0 ? stepA : i <= s1 ? stepB : stepC;" in CODE
    assert "const last = " in CODE and "if (tick >= stepOf(fi))" in CODE                       # 분마다 그 구간의 한 분 시간
    assert "cut: frames.length - NF" in CODE                                                    # 잘라 낸 분 수를 기록한다
    assert "const recI = lastI >= 0 && lastI < frames.length - 1 ? lastI + 1 : -1;" in CODE     # 판정 끝 = 마지막 판정 분의 다음 분
    assert "[AC_STEP[0], B_STEP[1]]" in CODE                                                   # 셀 없음: 읽을 이름표가 없어 0.5배 하한을 두지 않는다
    nocell_b = _plan(7, 10, 5, noCell=True)[1]
    assert abs(nocell_b - 13.0) < .01                                                          # 목표 길이에 맞춘다
    assert "A·B·C" in JS or "세 구간" in JS


def test_progress_bar_shows_three_segments_in_clock_minutes():
    i = CODE.index("$('.r3d-bar').style.backgroundImage")
    line = CODE[i:CODE.index("\n", i)]
    assert "128,128,128" in line and "239,68,68" in line and "34,197,94" in line                 # A 회색 · B 빨강 · C 초록
    assert "${pa}%" in line and "${pb}%" in line                                                  # 구간 경계는 분 비율(시각 기준)
    assert ".r3d-bar{top:50px;height:7px;cursor:pointer}" in CODE
    j = CODE.index("const barAt")
    assert "/ r.width * NF" in CODE[j:j + 200]                                                    # 끌어서 가는 분은 잘라 낸 범위(NF) 안


# ── 이름표·자막 속도 ──────────────────────────────────────────────────────────────────────

def test_tags_and_captions_stay_long_enough_to_read_and_swap_slowly():
    assert _const("TAG_MIN_MS") >= 4000 and _const("CAP_MIN_MS") >= 4000                      # 최소 4초
    assert _const("TAG_SWAP_MS") >= 500                                                         # 하나씩, 천천히
    # 이름표 페이드는 CSS 전환이 아니라 JS(매 프레임)가 한다 — 나타남과 사라짐이 같은 시간(TAG_FADE_MS)·같은 거리(TAG_RISE_PX)
    assert _const("TAG_FADE_MS") == 800 and _const("TAG_RISE_PX") == 24 and _const("CAP_RISE_PX") == 16
    assert _const("TAG_SLIDE_MS") >= 400 and _const("TAG_BLOCK_MS") >= 300
    assert "t.a = Math.max(0, Math.min(1, t.a + (want && q ? 1 : -1) * pdt / TAG_FADE_MS));" in CODE   # 올라올 때와 내려갈 때 같은 걸음
    assert "t.py + (1 - ea) * TAG_RISE_PX" in CODE                                              # 아래에서 올라오고, 같은 거리만큼 내려간다
    assert "old.leave = now + TAG_FADE_MS" in CODE
    assert "transition:opacity .45s,transform .45s" in CODE[CODE.index(".r3d-cap b{"):CODE.index(".r3d-cap b{") + 300]   # 자막 페이드 + 위아래 움직임
    assert "translateY(${CAP_RISE_PX}px)" in CODE[CODE.index(".r3d-cap.fade"):][:140]
    # 한 번 뜬 이름표는 최소 시간이 지난 것부터만 바꾼다 · 같은 장소는 다시 띄우지 않고 값만 바꾼다
    assert "now - t.since >= TAG_MIN_MS" in CODE and "now - lastSwap < TAG_SWAP_MS" in CODE
    assert re.search(r"!wantK\.has\(t\.k\)", CODE) and "t.since = now - TAG_MIN_MS" in CODE
    assert "if (now - capAt < CAP_MIN_MS) return;" in CODE                                    # 자막도 최소 4초
    assert "capI = capI >= 0 && capI < capWant ? capI + 1 : capWant;" in CODE                  # 밀려도 장을 건너뛰지 않고 하나씩
    assert "capI = -2" in CODE and "capAt = -1e9" in CODE                                       # 시간을 옮기면 기다리지 않고 바로


# ── 열차 — 양방향 · 역 정차 · 가감속 ───────────────────────────────────────────────────────

def test_trains_run_both_ways_stop_at_stations_and_ease_between_them():
    assert _const("TRAINS") == 8 and _const("DWELL_MS") == 600
    assert "dir: i % 2 ? -1 : 1" in CODE                                                        # 반은 시계 방향 · 반은 반대
    assert "off: LAP_MS * (Math.floor(i / 2) + (i % 2) * .5) / Math.ceil(TRAINS / 2)" in CODE   # 서로 엇갈린 자리에서 출발
    assert "return tr.dir > 0 ? runAt(fwd, tm) : acc - runAt(rev, tm);" in CODE                  # 역방향은 거꾸로 달린다
    assert "l.kind === 's1'" in CODE and "projD(l.x, l.z)" in CODE and "off < 400" in CODE       # 역 위치 = 야마노테 역 글자를 궤도에 투영
    assert "d - stD[i - 1] > 150" in CODE                                                       # 가까운 두 점은 한 역
    assert ".5 - .5 * Math.cos(Math.PI * k)" in CODE                                            # 역 사이는 ease-in-out(천천히→빠르게→천천히)
    assert "(tm - s.t0 - DWELL_MS) / s.run" in CODE and "t0 += DWELL_MS + run" in CODE           # 역마다 DWELL_MS 서 있다
    assert "if (!fwd) return tr.dir * tm / LAP_MS * acc;" in CODE                                # 역을 못 찾으면 등속으로 돈다(죽지 않는다)
    assert "Math.max(LAP_MS * .5, LAP_MS - n * DWELL_MS)" in CODE                                # 정차 시간만큼 달리는 시간을 줄여 한 바퀴를 유지


def test_train_clock_is_separate_from_the_replay_minute_clock():
    assert "const tt = now / LAP_MS * acc;" in CODE or "trainD(tr, now)" in CODE
    assert "tick" not in CODE[CODE.index("const trainD"):CODE.index("const trainD") + 200]          # 재생 분 시계(tick)와 무관 — B 가 느려져도 열차는 그대로


# ── 셀 없는 노선 주입 — `measured` 곡선 ─────────────────────────────────────────────────────

def test_measured_curve_comes_only_from_the_api_and_breaks_at_missing_minutes():
    assert "const MS = data.ta.measured && data.ta.measured.rows > 0 && data.ta.measured.points ? data.ta.measured : null;" in CODE
    assert "const mv = MS ? frames.map(f => { const v = mByT.get(f.t); return v == null || !isFinite(v) ? null : +v; }) : null;" in CODE
    assert "if (v == null) { pen = false; continue; }" in CODE                                    # 빈 분(None)은 선을 끊는다 — 0 으로 잇지 않는다
    assert "const mThr = MS && MS.threshold ? MS.threshold : {};" in CODE                         # 기준선은 응답의 `threshold`
    for hard in (r"\b11\.3\b", r"mThr\.\w+\s*[<>=]+\s*\d", r"(major|critical)\s*[:=]\s*\d"):
        assert not re.search(hard, CODE), hard                                                    # 숫자를 코드에 박지 않았다
    assert "mThr.major" in CODE and "fmtP(mv[mPk])" in CODE                                       # 자막 숫자는 measured 에서만
    assert "T('노선 전체 평균(참고)')" in CODE and "T('셀 판정 기준')" in CODE                      # 합산 값은 참고 눈금 · 기준선 이름은 「셀 판정 기준」
    assert "T('선 = 노선 전체 평균(참고)')" in CODE                                                # 셀이 있는 장애에는 막대 위에 옅게 겹친다


def test_not_reaching_the_threshold_is_told_differently_from_crossing_it_without_a_cell_judgement():
    a = "셀 판정 기준({b}%)에 닿지 않아 이상으로 잡히지 않았습니다"
    b = "셀 판정 기준({b}%)을 넘었지만 셀 단위 판정은 없었습니다"
    assert f"mv[mPk] < mThr.major ? T('{a}', a) : T('{b}', a)" in CODE
    assert "mThr.major == null ? ''" in CODE                                                       # 기준을 모르면 말하지 않는다(지어내지 않는다)
    assert a in _JA_MAP and b in _JA_MAP and _JA_MAP[a] != _JA_MAP[b]
    assert "{t} 노선 셀 {k} 최고 {v}%" in _JA_MAP and "노선 셀 {k} 최고 {v}% · 셀 판정 기준 {b}%" in _JA_MAP
    assert "{t} 장애 주입" in _JA_MAP and "{t} 장애 시작" in _JA_MAP


def test_chart_click_and_hover_use_the_same_clock_for_measured_only_faults():
    assert "mPk" in CODE and "for (let i = s0; i <= s1; i++) if (mv[i] != null && (mPk < 0 || mv[i] > mv[mPk])) mPk = i;" in CODE   # 주입 구간 안의 최고점
    assert "startOutro = () => { goTo(noCell ? (mPk >= 0 ? mPk : s1) : peakI)" in CODE            # 셀이 없으면 끝마무리는 곡선 최고점에서
def test_click_versus_drag_and_cell_tooltip():
    assert re.search(r"const SPEEDS = \[[^\]]*\], CLICK_PX = 5;", CODE)
    assert "if (moved > CLICK_PX) return;" in CODE                                          # 끌기였다 — 상자를 열지 않는다
    assert "Raycaster" in CODE and "gl.addEventListener('pointerdown', onDown)" in CODE
    for needle in ("T('시각')", "T('등급')", "T('지표')", "T('값')", "T('표본')", "T('이 장애 원인')", "T('이 분 판정 없음')"):
        assert needle in CODE, needle
    assert "closeTip()" in CODE and "tip.el.querySelector('.x').onclick" in CODE


def test_input_on_the_3d_layer_never_reaches_the_map():
    assert re.search(r"wrap\.addEventListener\('wheel', e => \{ e\.preventDefault\(\); e\.stopPropagation\(\); onTouch\(\); \}, \{ passive: false \}\)", CODE)   # 휠은 지도로 새지 않고 안내를 걷는다
    assert "for (const t of ['mousedown', 'pointerdown', 'click', 'dblclick', 'contextmenu']) wrap.addEventListener(t, e => e.stopPropagation())" in CODE
    assert "let onTouch = () => {};" in CODE                                                   # 장면이 서기 전 손대도 죽지 않는다


# ── 끝난 뒤 — 하이라이트에서 계속 돌고, 조작 안내 ────────────────────────────────────────

def test_after_the_outro_it_keeps_orbiting_the_highlight_and_shows_a_hint_until_touched():
    i = CODE.index("else if (outro) {")
    block = CODE[i:CODE.index("} else {", i)]
    assert "ended = true; setPlay(false); showHint(true);" in block and "auto = false" not in block and "close" not in block   # 멈추는 것은 재생뿐 — 공전은 계속
    assert "goTo(noCell ? (mPk >= 0 ? mPk : s1) : peakI)" in CODE                              # 끝마무리는 가장 넓게 번진 분(하이라이트)
    assert "} else if (auto) {" in CODE and "auto = true" in CODE                               # 자동 공전은 `ended` 와 무관하게 돈다
    assert "ctl.addEventListener('start', () => { auto = false; back = null; onTouch(); });" in CODE   # 끌면 공전이 멈추고 안내가 걷힌다
    assert "onTouch = () => showHint(false);" in CODE
    # 안내 — 십자(동서남북) 화살표 + 문구, 은은하게 깜빡이고 서서히 사라진다
    hint = CODE[CODE.index("hint.innerHTML"):CODE.index("wrap.appendChild(hint)")]
    assert "M0-22V22M-22 0H22" in hint and "T('드래그해서 둘러보세요 · 휠로 확대')" in hint   # 네 방향 화살표
    assert ".r3d-hint{" in CODE and "transition:opacity 1.2s" in CODE and "animation:r3dPulse" in CODE and ".r3d-hint.on{opacity:1}" in CODE
    assert "width:40px!important;height:40px!important" in CODE                                 # 지도 CSS(`svg{width:100%}`)에 눌려 커지지 않게
    assert "pointer-events:none" in CODE[CODE.index(".r3d-hint{"):CODE.index(".r3d-hint{") + 200]   # 안내가 끌기를 막지 않는다
    assert _JA_MAP["드래그해서 둘러보세요 · 휠로 확대"] == "ドラッグで見回せます · ホイールで拡大"
    assert "showHint(false);" in CODE[CODE.index("closing = () =>"):CODE.index("teardown = () => {\n")]   # 닫을 때도 걷는다


# ── 이름표·장소·자막 ───────────────────────────────────────────────────────────────────

def test_labels_use_the_maps_own_station_grades_and_never_a_new_api():
    assert "tier" in CODE and re.search(r"const T2_MPP = \d+, T3_MPP = \d+, AREA_MPP = \d+;", CODE)
    assert "labels:LABELS.filter(l=>/\\blb-(s|s2|a)\\b/.test(l.cls||''))" in RP3D          # 역(1·2·3등급)·지명만 넘긴다
    assert re.search(r"const CELL_TAGS = 4,", CODE)                                           # 이상 셀 이름표는 몇 개만
    assert "NEAR_STN_M" in CODE and "STN_PEN" in CODE                                         # 가까운 역 + 큰 역 우선
    assert "T('주로 {p} 부근'" in CODE and _JA_MAP["주로 {p} 부근"]


def test_legend_lists_only_what_is_in_the_scene():
    assert "T(light ? '기둥 = 이상이 잡힌 셀' : '빛 기둥 = 이상이 잡힌 셀')" in CODE
    assert "T('점 = 이상이 잡히지 않은 셀')" in CODE and "정상 셀" not in CODE                 # 판정 없음을 정상이라 하지 않는다
    assert re.search(r"onRoute[^\n]*(열차|tr\b)|열차[^\n]*onRoute", CODE) or "r3d-leg" in CODE
    assert "T('화면 밖 {n}셀 — 동떨어져 있어 범위에서 뺐습니다', { n: outN })" in CODE
    assert "if (outN)" in CODE                                                                # 뺀 셀이 있을 때만 적는다
    assert "const band = v =>" in CODE and "Math.floor(a.length * .01)" in CODE and "Math.ceil(a.length * .99) - 1" in CODE   # 지도와 같은 1~99% 띠


# ── 주입 시각 ≠ 판정 시각 — 자막을 확실히 나눈다 ───────────────────────────────────────────

def test_injection_and_first_judgement_captions_are_separate_and_the_gap_comes_from_timeline():
    assert "T('{t} 장애 주입 (정답 시각)'" in CODE                                              # 「장애 시작」이 아니다
    assert "실제 운영에서는 알 수 없는 시각입니다" in CODE and "'{t} 장애 시작'" not in CODE
    assert "if (fStart) a.m = Math.max(0, Math.round((fmin(firstI) - ms(fStart.at)) / 60000));" in CODE   # n = 주입 시각과 첫 판정 분의 차
    for ko in ("{t} {p} 부근에서 처음으로 이상 판정 — 주입 {m}분 뒤 (셀 {n}개)", "{t} 처음으로 이상 판정 — 주입 {m}분 뒤 (셀 {n}개)",
               "{t} 장애 주입 (정답 시각)", "실제 운영에서는 알 수 없는 시각입니다"):
        assert ko in CODE and ko in _JA_MAP and not re.search(r"[가-힣]", _JA_MAP[ko])
    assert "{m}" in _JA_MAP["{t} 처음으로 이상 판정 — 주입 {m}분 뒤 (셀 {n}개)"]
    # 주입 시각(fault_start) 정보가 없으면(옛 응답) 「주입 n분 뒤」를 지어내지 않고 옛 문구로
    assert "const k = fStart ? (p ?" in CODE and "'{t} 처음으로 이상이 잡혔습니다 (셀 {n}개)'" in CODE


# ── 장애 종류 표식 ──────────────────────────────────────────────────────────────────────────

def _mark_of(kind):
    i = CODE.index("const mType = onRoute || noCell ? null")
    blk = CODE[i:CODE.index(";", CODE.index("'nx'", i))]
    for pat, name in re.findall(r"(/\^.*?/)\.test\(kind\) \? '(\w+)'", blk):
        if re.search(pat[1:-1], kind):
            return name
    return "nx"


def test_marker_kind_follows_the_injection_kind_for_the_users_examples():
    for kind, want in {"ran_drop_surge": "drop", "mobility": "drop", "ims_pdn": "drop", "transport_cut": "data", "transport_loss": "data",
                       "apn_pdn_failure": "data", "gw_node_down": "data", "dns_degradation": "data", "ipv6_path_fail": "data",
                       "cdn_degradation": "data", "external_server": "data", "ran_radio": "radio", "ran_congestion": "crowd",
                       "event_crowd": "crowd", "capacity_shift": "crowd", "signaling_storm": "storm", "mme_overload": "nx"}.items():
        assert _mark_of(kind) == want, kind
    assert _mark_of("some_future_kind") == "nx"                                               # 모르는 종류는 카드의 ✕ 배지 하나로 통일(경고 삼각형은 카드에 없다)


def test_every_kind_the_generator_can_inject_gets_a_marker_and_a_translated_name():
    from datagen.scenarios import KIND_SPECS
    names = {"drop": "통화 끊김", "data": "데이터 안 됨", "radio": "전파 약함", "crowd": "혼잡", "storm": "단말 동시 재접속", "nx": "장애"}
    assert "const mName = { drop: '통화 끊김', data: '데이터 안 됨', radio: '전파 약함', crowd: '혼잡', storm: '단말 동시 재접속', nx: '장애' }[mType] || '';" in CODE
    assert "warn" not in CODE[CODE.index("const mType"):CODE.index("const mName")]            # 시나리오 카드에 없는 경고 삼각형은 쓰지 않는다
    for kind in KIND_SPECS:
        assert _mark_of(kind) in names, kind
    for ko in names.values():
        assert ko in _JA_MAP and not re.search(r"[가-힣]", _JA_MAP[ko]), ko
    assert "표식 = 장애 종류: {k}" in _JA_MAP and "표식" in _JA_MAP


def test_no_marker_for_route_faults_or_faults_without_cells_and_nothing_external():
    assert "const mType = onRoute || noCell ? null" in CODE                                    # 지하철 장면은 그대로 · 셀이 없으면 띄울 자리가 없다
    assert "t.mk = mType ? markOf() : null;" in CODE and "if (mType) {" in CODE                  # 표식·범례 줄·셀 상자 행 모두 mType 이 있을 때만
    assert "if (mType) h +=" in CODE
    body = CODE[CODE.index("const markOf"):CODE.index("const markOf") + 2500]
    assert "ExtrudeGeometry" in CODE or "CylinderGeometry" in CODE or "BoxGeometry" in CODE
    assert not re.search(r"GLTFLoader|OBJLoader|FBXLoader|\.glb|\.gltf|\.obj\b|https?://", CODE)    # 외부 모델 파일·CDN 없음
    assert "MARK_BOB_MS" in CODE and "m.g.quaternion.copy(cam.quaternion)" in CODE               # 흔들리며 카메라를 본다
    assert re.search(r"mSize \* \(t\.sev \? 1 : \.8\)", CODE)                                  # 크기는 심각도에 맞춘다
    assert "표식 = 장애 종류" in CODE and body


def test_marker_sits_on_the_label_slots_and_fades_with_them():
    assert "const CELL_TAGS = 4" in CODE
    # 이름표는 표식 위로 — 표식이 있으면 화면에서 표식 지름만큼 위로 띄워 둘 사이에 표식 자리를 남긴다(3D 높이로 띄우면 낮은 각도에서 어긋난다)
    assert "const top = Math.max(c.h * vis, 60 * hk), mk = t.mk && t.mk.g.visible;" in CODE
    assert "offs = [mk ? -(2 * mr + 6) : 0, 0, 0, 0]" in CODE and "const vis = BEAM_VIS[light ? 1 : 0];" in CODE
    assert re.search(r"const BEAM_VIS = \[\.5, \.9\];", CODE)                                    # 빛 기둥이 보이는 높이 비율(어두운·밝은 배색)
    assert re.search(r"const MARK_K = \.038, MARK_M = \[25, 300\], MARK_BOB_MS = 3200;", CODE)   # 바탕 원판 없이 더 작게(범위 반경 3.8%)
    assert "disc" not in CODE.lower().replace("disconnect", "")                                 # 원판 바탕은 없다
    assert "초기값" in JS[JS.index("//: 장애 종류 표식"):JS.index("const MARK_K")]


# ── 국소 장애 — 가까이 · 10도 낮게 ─────────────────────────────────────────────────────────

def test_local_faults_get_a_closer_and_lower_camera_with_initial_value_constants():
    assert re.search(r"const RMIN_LOCAL = 600, HK_LOCAL = \.12, EL_LOCAL_DROP = 10, LOCAL_PLATE_K = 6;", CODE)
    assert _const("RMIN_LOCAL") < _const("RMIN") and _const("EL_LOCAL_DROP") == 10                 # 더 가까이 · 10도 낮게
    assert "const local = !onRoute && R0 < RMIN;" in CODE                                         # 노선 장애는 국소가 아니다
    assert "const R = local ? Math.max(RMIN_LOCAL, R0) : Math.max(RMIN, R0);" in CODE
    assert "const elRun = onRoute ? EL : EL_CELL;" in CODE                                       # 노선은 그대로 · 셀 장애는 더 낮게(국소 포함)
    assert _const("EL_CELL") == 30 and _const("EL_CELL") <= _const("EL") - _const("EL_LOCAL_DROP")   # 국소의 옛 10도 낮춤(40°)보다 더 낮다
    assert _const("DIST_CELL_K") == 1.35 and _const("G_CELL_K") == 5
    assert "const hk = Math.min(1, Math.max(local ? HK_LOCAL : .3, R / 6500));" in CODE            # 기둥 크기 하한도 줄인다
    assert _const("CLUSTER_SHARE") == .15 and _const("CLUSTER_MIN_MIN") == 3                     # 덩어리 규칙(옛 LOCAL_SHARE 75% 를 대신한다)
    assert "LOCAL_SHARE" not in CODE
    for name in ("RMIN_LOCAL", "EL_LOCAL_DROP", "EL_CELL", "CLUSTER_SHARE", "CLUSTER_MIN_MIN", "TAG_FADE_MS", "MARK_K"):
        j = JS.index(f"{name} = ")
        assert "초기값" in JS[max(0, j - 450):j + 80], name                                      # 새 값은 모두 「초기값」 주석


def _local_cluster(cells, share=.15, min_min=3, rmin=1500):
    """JS 와 같은 판단 — 판정 셀-분이 많은 셀부터 rmin 안의 덩어리(씨앗 셀 기준)에 붙이고, 셀-분 몫이 `share` 이상이거나
    셀마다 평균 `min_min` 분 이상 켜진 덩어리를 **모두** 범위로 삼는다. cells = [(x_m, y_m, 켜진 분 수)]"""
    import math
    cls = []
    for c in sorted(cells, key=lambda c: -c[2]):
        g = next((g for g in cls if math.hypot(g["seed"][0] - c[0], g["seed"][1] - c[1]) <= rmin), None)
        if g is None:
            g = {"seed": c, "cells": [], "w": 0}; cls.append(g)
        g["cells"].append(c); g["w"] += c[2]
    tot = sum(g["w"] for g in cls)
    keep = [g for g in cls if g["w"] >= tot * share or g["w"] / len(g["cells"]) >= min_min]
    return [c for g in keep for c in g["cells"]] if keep else cells


def test_one_minute_stray_cells_do_not_widen_the_span_but_two_real_clusters_are_both_kept():
    """F-583 꼴 — 가미나카자토 12셀(분마다 켜짐) + 아카사카 4셀(1분만): 덩어리만 범위, 나머지는 「화면 밖 n셀」.
    F-611 꼴 — 신주쿠와 시부야 두 곳이 모두 진짜 장애 지역이면 둘 다 범위에 담는다."""
    cluster = [(i * 20, i * 15, 10) for i in range(12)]
    stray = [(4600 + i * 10, 4000, 1) for i in range(4)]
    assert sorted(_local_cluster(cluster + stray)) == sorted(cluster)
    # F-611 — 시부야는 셀-분 몫이 15% 아래(13%)여도 셀마다 평균 10분이라 담긴다. 1분짜리 외톨이는 여전히 뺀다
    shinjuku = [(i * 30, 0, 8) for i in range(60)]
    shibuya = [(6000 + i * 30, -4000, 10) for i in range(8)]
    both = _local_cluster(shinjuku + shibuya + stray)
    assert set(shinjuku) <= set(both) and set(shibuya) <= set(both) and not set(stray) & set(both)
    assert sum(c[2] for c in shibuya) < .15 * sum(c[2] for c in shinjuku + shibuya + stray)   # 몫 규칙만으로는 빠졌을 경우
    # 덩어리가 하나도 규칙을 못 넘으면 전부를 범위로 둔다
    few = [(0, 0, 1), (9000, 0, 1)]
    assert _local_cluster(few, share=.9, min_min=3) == few
    assert "clKeep = cls.filter(g => g.w >= tot * CLUSTER_SHARE || g.w / g.cells.length >= CLUSTER_MIN_MIN)" in CODE
    assert "if (clKeep.length) inSpan = clKeep.flatMap(g => g.cells);" in CODE
    assert "const outN = fc.length - inSpan.length;" in CODE and "화면 밖 {n}셀 — 동떨어져 있어 범위에서 뺐습니다" in CODE
    assert "if (!(/^ROUTE/i.test(String(opt.target || ''))) && fc.length)" in CODE                  # 노선 장애에는 쓰지 않는다


def test_local_floor_is_re_rendered_closer_so_it_is_not_blurry():
    assert "const L = R * LOCAL_PLATE_K" in CODE and "lb = { x: lon0 - L / 2 / KX" in CODE            # 범위 둘레만 한 장 더 촘촘히
    assert "opt.snap" in CODE or "opt.wide" in CODE


# ── 이름표·표식·자막은 즉시 감추지 않는다 — 올라오며 나타나고 같은 속도로 내려가며 사라진다 ──────────────────────────

def test_cell_tags_never_hide_instantly_they_fade_down_at_the_speed_they_rose():
    i = CODE.index("if (t.cellTop) {"); blk = CODE[i:CODE.index("continue;\n      }", i)]
    assert blk.count("style.display = 'none'") == 1 and blk.index("if (t.a <= .005)") < blk.index("style.display = 'none'")   # 다 사라진 뒤에만 감춘다
    assert "if (t.a <= .005) {" in blk                                                           # 감추는 조건은 투명도가 0 에 닿은 것 하나뿐
    assert "(want && q ? 1 : -1) * pdt / TAG_FADE_MS" in blk                                     # 오를 때와 내릴 때 같은 걸음
    assert "const ea = t.a * t.a * (3 - 2 * t.a)" in blk and "(1 - ea) * TAG_RISE_PX" in blk      # 같은 곡선·같은 거리
    assert "TAG_BLOCK_MS" in blk and "TAG_SLIDE_MS" in blk                                       # 막혀도 버티고, 옮길 땐 미끄러진다
    assert "t.leave = t.inAt = 0" in CODE                                                        # 시간을 옮길 때만 바로 바뀐다


def test_markers_use_the_same_fade_time_and_rise_as_the_tags():
    i = CODE.index("const m = t.mk; if (!m) continue;")
    blk = CODE[i:i + 1400]
    assert "(want ? 1 : -1) * dt / TAG_FADE_MS" in blk and "m.g.visible = m.a > .02" in blk
    assert "sz * (.85 + .15 * ea)" in blk and "x.userData.op * ea" in blk                         # 커지며 나타나고 작아지며 사라진다(자리는 `place()` 가 정한다)
    assert "자리(위치)는 place() 가 화면 배치로 정한다" in JS and "m.g.position" not in blk          # 표식 자리는 이 루프가 아니라 배치 함수가 정한다


def test_caption_card_slides_while_fading_and_still_swaps_after_the_fade():
    assert ".r3d-cap.fade b,.r3d-cap.fade small{opacity:0;transform:translateY(${CAP_RISE_PX}px)}" in CODE
    assert "box.classList.add('fade');" in CODE and "box.classList.remove('fade')" in CODE and "}, 450);" in CODE   # 내용은 페이드가 끝난 뒤에 바뀐다


# ── 높은 건물·역 건물 ─────────────────────────────────────────────────────────────────────

def test_tall_buildings_and_stations_are_fetched_once_for_3d_and_cached():
    assert "const layerOf = name => LAYER_C[name] || (LAYER_C[name] = fetch(`/api/map/layer/${name}`)" in CODE     # 처음 한 번만 받고 캐시
    assert "layerOf('building_tall')" in CODE and "opt.stations ? Promise.resolve(opt.stations) : layerOf('station')" in CODE   # 역은 지도가 받아 둔 것을 쓴다
    assert "stations:STBL" in HTML                                                                  # 지도가 넘겨 준다
    assert "building_tall" not in HTML                                                              # 2D 지도 첫 로딩은 건물을 받지 않는다
    assert ".catch(() => {})" in CODE[CODE.index("layerOf('building_tall')"):][:300]                # 못 받아도 3D 는 그대로


def test_buildings_stand_from_min_h_to_h_and_stations_are_low_bands():
    assert "add(b.rings, b.min_h || 0, b.h || 0, cB)" in CODE                                       # 도쿄타워 층별 고리 — min_h 부터 세운다
    assert "b.kind === 'band' && b.ring) add([b.ring], 0, STN_H, cS)" in CODE                       # 역 띠는 땅에서 낮게
    assert _const("STN_H") == 12 and _const("BLDG_K") == 1 and _const("BLDG_LB_H") == 150 and _const("BLDG_LB_N") == 4   # 배율 1 = 실제 높이 그대로(사용자 11-25)
    assert "!inBox(...rs[0][0])" in CODE and "const bHalf = G;" in CODE                                # 넓은 바닥 범위 안의 건물만
    assert "(b.h || 0) > (b.m || 0)" in CODE and "prism(rs, b.m || 0, b.h, cB, pos, col)" in CODE        # 낮은 건물 칸도 min_h(m) 부터 h 까지
    for name in ("BLDG_K", "STN_H", "BLDG_LB_H"):
        j = JS.index(f"{name} = "); assert "초기값" in JS[max(0, j - 450):j + 80], name


def test_buildings_are_real_scale_hand_built_prisms_that_never_hide_pillars_or_markers():
    assert "BufferGeometryUtils" not in CODE and "ExtrudeGeometry(sh, { depth: h1 - h0" not in CODE      # 새 vendor 파일 없음 · 돌출 대신 직접 각기둥
    assert _const("BLDG_PILLAR") == 0 and _const("BLDG_REF_H") == 250                                  # 기둥 높이 상한 없음(0)
    assert "bS = BLDG_PILLAR ? Math.min(BLDG_K, BLDG_PILLAR * H_DEG * hkH / BLDG_REF_H) : BLDG_K;" in CODE   # 상한은 0 이면 꺼진다 · 배율은 그대로
    assert "THREE.ShapeUtils.triangulateShape(v2(rs[0]), rs.slice(1).map(v2))" in CODE                 # 지붕은 삼각 분할(구멍=중정 포함)
    assert "put(all[j][0], h1, all[j][1], 1.22)" in CODE and "put(x1, h0, z1, k)" in CODE               # 지붕 밝게 · 벽은 빛 방향으로 어둡게
    assert "(i === 0) === (area2(r) > 0)" in CODE                                                        # 바깥/구멍의 감는 방향을 맞춘다
    # 깊이만 먼저 쓰고 색을 칠한 뒤 깊이를 지운다 — 기둥·번짐·표식이 건물 뒤에 있어도 위에. 칸이 여럿이라 지우기는 **한 번만**
    assert "colorWrite: false" in CODE and "pre.renderOrder = -1.6; paint.renderOrder = -1.5" in CODE
    assert "clr.renderOrder = -1.4; clr.onAfterRender = r => r.clearDepth();" in CODE
    assert CODE.count("clearDepth()") == 1, "칠할 때마다 지우면 칸끼리 앞뒤가 깨진다 — 모든 건물 뒤에 한 번"
    assert "depthWrite: false" in CODE[CODE.index("const paintMat"):][:260] and "const bOp = light ? .8 : .72" in CODE   # 반투명
    # 메인 스레드를 오래 쥐지 않는다
    assert _const("BLDG_SLICE_MS") == 12 and "if (t - tS < BLDG_SLICE_MS) return true;" in CODE


def test_building_names_are_few_dim_and_lose_to_station_and_place_labels():
    assert "(b.h || 0) < BLDG_LB_H" in CODE and "seen.has(t)" in CODE                                # 150m 이상 · 이름 있는 것 · 같은 이름은 한 번
    assert "opt.lang === 'ja' ? b.ja || b.name : b.ko || b.name" in CODE                             # 한국어는 ko → name, 일본어는 ja → name
    assert "kind: 'b', tier: 9" in CODE and "nb < BLDG_LB_N" in CODE                                  # 한 화면 BLDG_LB_N 개 · 가장 낮은 순위


# ── 낮은 건물 칸(JIS 3차 메시 · 8m·3층 이상) — 보이는 범위만 받아 그린다 ─────────────────────────────────

def test_grid_constants_are_initial_values():
    assert _const("BLDG_VIEW_K") == 5 and _const("BLDG_TICK_MS") == 400 and _const("BLDG_BATCH") == 16
    for name in ("BLDG_VIEW_K", "BLDG_SLICE_MS", "BLDG_K"):
        j = JS.index(f"{name} = "); assert "초기값" in JS[max(0, j - 700):j + 80], name


def test_grid_is_on_only_when_the_center_scale_is_within_the_1km_bar_and_radius_is_bar_times_five():
    assert "const close = grid.bar <= (opt.stScale && opt.stScale.t3 || 1000);" in CODE                    # 2D 와 같은 경계(ST_T3_M)
    assert "const r = grid.bar * BLDG_VIEW_K, want = close ? cellsAround(ctl.target.x, ctl.target.z, r)" in CODE   # 가운데에서 막대 값 × 5
    assert ".filter(c => grid.idx.cells[c.code])" in CODE                                                   # 색인에 있는 칸만 묻는다
    assert "grid.bar = SC ? SC.nice(mppC) : mppC * 100;" in CODE                                           # 막대 값은 가운데 축척(역 이름과 같은 값)
    assert "return out.sort((a, b) => a.d - b.d);" in CODE                                                  # 가까운 칸부터
    assert ".slice(0, BLDG_BATCH)" in CODE and "fetch(`/api/map/bldg?cells=${need.join(',')}`)" in CODE


def test_cells_out_of_range_fade_out_then_are_dropped_and_memory_is_returned():
    assert "for (const [k, c] of grid.cells) if (!ws.has(k)) c.leave = true; else c.leave = false;" in CODE
    assert "c.a = Math.max(0, Math.min(1, c.a + (c.leave ? -1 : 1) * dt / TAG_FADE_MS));" in CODE            # 이름표와 같은 걸음으로 나타나고 사라진다
    assert "if (c.leave && c.a <= 0) { dropCell(k, c); continue; }" in CODE
    assert "const dropMesh = m => { scene.remove(m.pre, m.paint); m.g.dispose(); m.paint.material.dispose();" in CODE   # 지오메트리·재질을 돌려준다
    assert "keep({ dispose: () => { for (const [k, c] of [...grid.cells]) dropCell(k, c); } });" in CODE     # 닫을 때 전부
    assert "if (b.t) continue;" in CODE                                                                    # 높은 건물(t:1)은 ①이 그린다 — 두 번 그리지 않는다


def test_without_an_index_it_falls_back_to_the_tall_buildings_only():
    assert ".then(r => r.ok ? r.json() : null)" in CODE and "Object.keys(j.cells).length ? j : null" in CODE   # 404·빈 색인 → null
    assert ".catch(() => { BIDX = null; return null; })" in CODE
    assert "if (!grid.idx || now - grid.at < BLDG_TICK_MS) return;" in CODE                                 # 색인이 없으면 칸 겹은 아무 것도 안 한다
    assert "bldgIndex().then(ix => { if (alive && ix) tallP.then(() => { grid.idx = ix; }); });" in CODE   # 높은 건물 다음에 칸


@pytest.mark.skipif(NODE is None, reason="node 없음")
def test_mesh_code_in_js_equals_graphio_mesh_codes_for_random_points(tmp_path):
    """JS `meshCode` 를 잘라 실제로 돌려 graphio `mesh_codes(MESH1K)` 와 비교한다 — 칸을 잘못 고르면 에러 없이 건물이 안 선다"""
    import json
    import random
    try:
        import numpy as np
        from graphio.geo_coverage import mesh_codes
    except ImportError:
        pytest.skip("graphio·numpy 없음")
    i = JS.index("const meshCode = "); fn = JS[i:JS.index("\n};\n", i) + 4]
    rnd = random.Random(7)
    pts = [(139.62 + rnd.random() * .24, 35.56 + rnd.random() * .25) for _ in range(300)] + [(139.7, 35.6833), (139.75, 35.68), (139.7000001, 35.6833333)]
    (tmp_path / "t.js").write_text(fn + f"\nconsole.log(JSON.stringify({json.dumps(pts)}.map(([o,a])=>meshCode(o,a))));\n", encoding="utf-8")
    out = json.loads(subprocess.run([NODE, str(tmp_path / "t.js")], capture_output=True, text=True, check=True).stdout)
    lon = np.array([p[0] for p in pts]); lat = np.array([p[1] for p in pts])
    want = [str(int(c)).zfill(8) for c in mesh_codes(lat, lon)["MESH1K"]]
    assert out == want


# ── 「첫 이상」·역 이름 ──────────────────────────────────────────────────────────────────────

def test_the_first_anomaly_tag_is_never_hidden_by_overlap_and_sits_on_its_card():
    assert ".r3d-tag.fa{z-index:3;" in CODE                                                                # 맨 위 겹
    i = CODE.index("if (t === faTag) {"); blk = CODE[i:CODE.index("continue;\n      }", i)]
    assert "boxes.push([x0 - 3" in blk and "free(" not in blk, "겹침 검사(free)로 숨기지 않는다 — 같은 기둥의 표식 자리에 걸려 늘 숨겨졌다"
    assert "t.p.y = Math.max(faCell.h * vis, H_DEG * hkH * vis) + 200 * hk;" in blk                                    # 기둥 꼭대기를 따라간다
    assert "const ord = faTag ? [...(faCard ? [faCard] : []), faTag, ...tags.filter(t => t !== faTag && t !== faCard)] : tags;" in CODE   # 배치 1순위
    assert "(t.cell === faCell || t.k === keyOf(faCell)) && t.a > .05 && t.slot >= 0" in CODE              # 카드는 셀이 아니라 장소로 찾는다(F-619 渋谷)
    assert "y0 = faCard.py + (1 - ea) * TAG_RISE_PX - t.h - 3" in CODE                                     # 카드 바로 위 · 카드와 함께 오르내린다


def test_station_names_are_decided_by_the_center_scale_with_the_2d_boundary():
    assert "const mppC = gnd(ctl.target, 2 * cam.position.distanceTo(ctl.target) * tanH / H);" in CODE      # 화면 가운데 하나로 잰다
    assert "const allSt = SC ? SC.nice(mppC) <= SC.t3 : mppC <= T3_MPP;" in CODE
    assert "l.major || allSt && (SC ? SC.nice(mppG) <= SC.t3 : mppG <= T3_MPP)" in CODE                     # 주요 역은 늘 · 나머지는 가운데 1km 안이고 그 자리도 1km 안일 때만
    assert "const gnd = (p, d) => d / Math.max(.05, Math.abs(cam.position.y - p.y) / Math.max(1, cam.position.distanceTo(p)));" in CODE   # 비스듬히 눌린 방향 보정(기하)
    assert "stScale:{nice:scaleNiceM, t2:ST_T2_M, t3:ST_T3_M}" in HTML                                       # 기준값은 2D 가 쓰는 것을 그대로 넘긴다 — 3D 에 새 숫자 없음

