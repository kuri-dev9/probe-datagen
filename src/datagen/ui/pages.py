"""화면 네 개(`/`·`/map`·`/topo`·`/scenarios`) — 정적 파일(`static/`)을 읽어 껍데기·번역을 끼운다.

HTML·JS·CSS 는 2026-11-10 까지 이 모듈 안 문자열이었다. 시작할 때 한 번 읽어 그때와 **글자까지
같은** 문자열을 만들고 같은 이름(`_HTML` 등)으로 둔다 — 시험이 그 이름을 읽는다.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from .common import KIND_LABELS
from .i18n_ja import _JA_COMMON, _JA_MAIN, _JA_MAP, _JA_SCEN, _JA_SHELL


router = APIRouter()


#: 정적 파일 — 시작할 때 한 번 읽는다. 줄바꿈을 바꾸지 않게 `newline=""` 로 연다
_STATIC = Path(__file__).with_name("static")


def _static(name: str) -> str:
    with open(_STATIC / name, encoding="utf-8", newline="") as f:
        return f.read()


# ── 화면 ────────────────────────────────────────────────────────────────────

# 운영자용 단일 페이지. 상태 바·주입 폼은 즉시 그리고, 목록·슬롯은 5초 폴링 —
# 데몬 틱이 1분이라 그보다 촘촘할 이유가 없지만, 주입 직후 피드백은 빨라야 한다.
_HTML = _static("main.html")


@router.get("/", response_class=HTMLResponse)
def index():
    # 라벨 정본은 파이썬 쪽 KIND_LABELS 하나 — HTML 에 중복 정의하지 않는다
    return _HTML.replace("__KIND_LABELS__", json.dumps(KIND_LABELS, ensure_ascii=False))



# ── 마스터 토폴로지 화면 (`/topo`) ──────────────────────────────────────────
#
# **별도 페이지다** (사람 지시 2026-10-26: "기존 UI 에 별도 화면으로 제어").
# 장애 주입 화면과 섞지 않는 이유: 그쪽은 **상태를 주입**하고(데몬이 1분마다
# 읽어 되돌린다) 이쪽은 **마스터를 영구히 바꾼다.** 같은 화면에 두면 "취소"가
# 한쪽에만 있다는 것이 보이지 않는다.
_HTML_TOPO = _static("topo.html")

_HTML_MAP = _static("map.html")


# ── 화면 언어 — 한국어 / 日本語 (사람 결정 2026-11-03) ─────────────────────
#
# **나라(`locale`)와 따로 고른다.** 일본 PoC 를 한국 쪽 사람이 한국어로 볼 수
# 있어야 한다. 고른 값은 브라우저에 두고(`localStorage`) 바꾸면 페이지를
# 새로 연다 — 지도가 1초 남짓에 뜨므로 그리던 것을 다 되돌리는 것보다 낫다.
#
# **사전의 키는 한국어 원문이다.** 원문이 곧 코드에 적힌 문구라 키를 따로
# 지어 두 벌을 맞출 일이 없고, 사전에 없는 문구는 **원문 그대로** 나온다 —
# 빠뜨린 것이 깨지지 않고 한국어로 보여 눈에 띈다.
#
# * 정적 HTML — `applyLang` 이 글자 마디와 `title`·`placeholder`·`aria-label`
#   을 **통째로 같을 때만** 바꾼다(앞뒤 공백 무시)
# * JS 가 만드는 문구 — `T('셀 {n}', {n})`. 자리표시는 `{이름}`
# * graphio 가 내려 준 문구(장애 제목·원인 설명·`statement_ko`)는 **바꾸지
#   않는다** — 그쪽 데이터다

_I18N_JS = _static("i18n.js")


def _with_i18n(html: str, ja: dict) -> str:
    """`/*__I18N__*/` 자리에 번역 함수와 사전을 넣는다."""
    js = "const JA=" + json.dumps(ja, ensure_ascii=False) + ";\n" + _I18N_JS
    assert "/*__I18N__*/" in html
    return html.replace("/*__I18N__*/", js, 1)





# ── 껍데기 — 좌측 레일 + 상단 바 (사람 결정 2026-11-03) ───────────────────
#
# graphio 콘솔(`web/src/App.tsx` 의 `rail`·`TopBar`)과 같은 모양이다. 세 화면이
# 따로 된 HTML 이라 **한 벌을 만들어 셋에 끼운다** — 화면마다 손으로 적으면
# 메뉴가 하나 늘 때 한 화면만 고쳐진다.
#
# * 레일 — 화면 이동. 지금 화면에 불이 들어온다
# * 상단 바 — 생성기 상태(`/api/status`, 5초) · 생성 지역 · 시계 · **언어**.
#   지도·토폴로지에서도 "생성이 돌고 있나"가 보이게 한다
# * 각 화면은 위 44px · 왼쪽 60px 를 비운다(화면마다 배치가 달라 그쪽 CSS 에)
#
# CSS 는 `gp-` 로 시작하는 이름만 쓴다. 장애 주입 화면의 `select{width:100%}`
# 같은 전역 규칙이 상단 바까지 늘어나 언어 선택이 한 줄을 다 먹었다

_SHELL_CSS = _static("shell.css")

#: 레일 아이콘 — 외부 자원을 쓰지 않으므로 SVG 를 직접 적는다
_SHELL_NAV = (
    ("main", "/", "주입", "장애 주입 제어",
     '<path d="M13 2 4 14h7l-1 8 9-12h-7z"/>'),
    ("map", "/map", "지도", "장비 지도",
     '<path d="M9 4 3 6v14l6-2 6 2 6-2V4l-6 2z"/><path d="M9 4v14M15 6v14"/>'),
    ("scen", "/scenarios", "시나리오", "시나리오 현황",
     '<path d="M6 3h9l4 4v14H6z"/><path d="M15 3v4h4M9 12h7M9 16h7"/>'),
    ("topo", "/topo", "토폴로지", "마스터 토폴로지",
     '<circle cx="12" cy="5" r="2.2"/><circle cx="5" cy="19" r="2.2"/>'
     '<circle cx="19" cy="19" r="2.2"/><path d="M12 7.2v4.3M12 11.5 6.4 17.2M12 11.5l5.6 5.7"/>'),
)

#: 레일에 **보이지 않는** 화면 — 주소로만 들어간다(2026-11-03 사람 결정).
#: 토폴로지는 xDR 생성이 아니라 마스터를 영구히 바꾸는 화면이고(gen 은 XDR 만
#: 만든다), 시연 화면 옆에 파괴적 조작이 있으면 실수로 누른다. 일본 마스터
#: 구성이 끝나 쓸 일도 드물다. 코드와 `/topo` 는 남긴다 — 다시 필요하면 주소로
_RAIL_HIDDEN = {"topo"}


def _shell_html(page: str) -> str:
    title = {k: t for k, _, _, t, _ in _SHELL_NAV}[page]
    btns = "".join(
        f'<a class="gp-rbtn{" on" if k == page else ""}" href="{href}" title="{tip}"'
        f'{" aria-current=\"page\"" if k == page else ""}>'
        f'<svg viewBox="0 0 24 24">{icon}</svg>{label}</a>'
        for k, href, label, tip, icon in _SHELL_NAV
        if k not in _RAIL_HIDDEN or k == page)
    return (
        '<nav class="gp-rail" aria-label="화면"><div class="gp-logo">PD</div>'
        + btns + '</nav>'
        '<div class="gp-top" role="banner">'
        f'<div class="gp-brand">probe-datagen<em>{title}</em></div>'
        '<div class="gp-sums">'
        '<div class="gp-sum"><i>생성</i><b id="gp-gen"><span class="gp-dot" id="gp-dot"></span>'
        '<span id="gp-gen-t">—</span></b></div>'
        '<div class="gp-sum"><i>마지막 슬롯</i><b id="gp-slot">—</b></div>'
        '<div class="gp-sum"><i>생성량</i><b id="gp-pct">—</b></div>'
        '<div class="gp-sum"><i>활성 장애</i><b id="gp-act">—</b></div>'
        # 오늘 생성 — 인입 현황 카드가 여기로 왔다(2026-11-04). 상세는 마우스를 올리면
        '<div class="gp-sum gp-today" tabindex="0"><i>오늘 생성</i><b id="gp-today">—</b>'
        '<div class="gp-pop" id="gp-today-pop">불러오는 중…</div></div>'
        '</div>'
        '<div class="gp-top-r">'
        '<span class="gp-badge" id="gp-loc" title="생성 지역 — gen-ui 의 지역 설정">—</span>'
        '<span class="gp-clock" id="gp-clock">—</span>'
        '<select id="gp-lang" title="화면 언어 — 고른 것은 이 브라우저에 기억한다">'
        '<option value="ko">한국어</option><option value="ja">日本語</option></select>'
        '</div></div>'
    )


_SHELL_JS = _static("shell.js")



def _page(html: str, page: str, ja: dict) -> str:
    """껍데기(CSS·레일·상단 바)와 번역을 끼운다."""
    assert html.count("</style>") >= 1 and "<body>" in html
    html = html.replace("</style>", _SHELL_CSS + "</style>", 1)
    html = html.replace("<body>", "<body>\n" + _shell_html(page), 1)
    html = _with_i18n(html, {**_JA_COMMON, **ja, **_JA_SHELL})
    # 껍데기 동작은 번역 함수 바로 뒤 — 페이지 스크립트보다 먼저 선다
    return html.replace("function langSelect(", _SHELL_JS_HOOK + _FAULT_MAP_JS + "function langSelect(", 1)


_SHELL_JS_HOOK = "window.addEventListener('DOMContentLoaded',()=>{" + _SHELL_JS + "});\n"

#: 장애 → 지도(2026-11-09). 주입 화면의 장애 창(`fm-go`)과 시나리오 화면의 회차 표가 함께 쓴다 —
#: 화면마다 따로 두면 "탐지 이력 없음"을 가르는 기준이 두 벌이 된다. 모든 페이지에 실린다(`_page`)
_FAULT_MAP_JS = _static("fault_map.js")

# ── 시나리오 현황 화면 (`/scenarios`) ───────────────────────────────────────
#
# 1~27번의 설정·판정 기준·최신 결과를 누구나 보게 한다(사용자 11-06). 자료는 코드가 아니라
# `config/scenario_report.json` 이다 — 판정이 끝날 때마다 그 파일만 고치면 되고, 로컬은
# `config` 가 마운트라 재시작 없이 다음 요청부터 보인다. 화면은 값을 계산하지 않고 적힌 대로
# 보인다(불변규칙 18). 값이 `null` 이면 "미확인"이다 — 0 이나 없음으로 읽히면 안 된다(불변규칙 17)
_HTML_SCEN = _static("scenarios.html")


#: 주입·토폴로지 화면이 함께 쓰는 바탕(색 변수·본문 여백·제목·두 칸 격자). 토폴로지 화면이 주입 화면을
#: 베껴 만들어져 같은 규칙을 두 벌 갖고 있었다. 지도·시나리오 화면은 바탕이 달라 싣지 않는다 —
#: 공통 껍데기(`shell.css`)에 넣으면 `* { box-sizing }`·`body` 여백이 그 두 화면에도 걸린다.
#: `<style>` 맨 앞에 끼워 원래 자리(규칙 순서)를 지킨다
_BASE_CSS = _static("base.css")


def _with_base(html: str) -> str:
    assert html.count("<style>\n") >= 1
    return html.replace("<style>\n", "<style>\n" + _BASE_CSS, 1)


_HTML = _page(_with_base(_HTML), "main", {**_JA_MAP, **_JA_MAIN})
_HTML_MAP = _page(_HTML_MAP, "map", _JA_MAP)
_HTML_TOPO = _page(_with_base(_HTML_TOPO), "topo", {})
_HTML_SCEN = _page(_HTML_SCEN, "scen", _JA_SCEN)


@router.get("/map", response_class=HTMLResponse)
def map_page():
    return _HTML_MAP


@router.get("/topo", response_class=HTMLResponse)
def topo_page():
    return _HTML_TOPO


@router.get("/scenarios", response_class=HTMLResponse)
def scenarios_page():
    return _HTML_SCEN
