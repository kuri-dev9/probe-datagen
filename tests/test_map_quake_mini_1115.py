"""gen-ui 미니 일본 전도 — 27번 진앙 ★ 과 규모 (2026-11-15 사용자 요청 · 맥스 구현).

진앙이 도쿄 밖 바다라 장비 지도 화면 밖이다. 구석에 확대·끌기를 받지 않는 일본 전도를 얹어 진앙 ★ 과 규모만 찍는다.
서버(`/api/map/quake`)가 어느 지진인지 정한다 — 진행 중인 27번, 없으면 고른 장애의 시작 시각을 품은 27번, 없으면 None.

DB 를 쓰지 않는다 — 연결은 가짜이고, 일본 윤곽은 graphio 의 실제 경계 파일이 있으면 그것으로(없으면 건너뜀) 본다.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import pymysql
import pytest
from fastapi import HTTPException

from datagen.ui import map_api
from datagen.ui.i18n_ja import _JA_MAP

SRC_DIR = Path(map_api.__file__).resolve().parent
MAP_HTML = (SRC_DIR / "static" / "map.html").read_text(encoding="utf-8")
PREF = Path(__file__).resolve().parents[2] / "graphio-probe-rca" / "data" / "geo" / "jp" / "prefectures.json"
NODE = shutil.which("node")

T0 = datetime(2026, 11, 15, 5, 37)
ROW = (348, 35.734531, 140.543731, 5.9, T0)


class _Cur:
    def __init__(self, log, active, by_time, boom):
        self.log, self.active, self.by_time, self.boom, self._row = log, active, by_time, boom, None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        if self.boom:
            raise self.boom
        self.log.append((sql, params))
        if "STATUS = 'active'" in sql:
            self._row = self.active
        else:
            ts = params[1]
            self._row = self.by_time(ts)

    def fetchone(self):
        return self._row


class _Conn:
    def __init__(self, **kw):
        self.log, self.kw, self.closed = [], kw, False

    def cursor(self):
        return _Cur(self.log, self.kw.get("active"), self.kw.get("by_time", lambda ts: None), self.kw.get("boom"))

    def close(self):
        self.closed = True


def _use(monkeypatch, **kw):
    c = _Conn(**kw)
    monkeypatch.setattr(map_api, "_connect", lambda: c)
    return c


def _row(start=T0, dur=16, mag=5.9, status="done"):
    """구간 안이면 지진 행을 돌려주는 by_time — SQL 의 START_TIME <= at <= START_TIME+DURATION 을 흉내낸다."""
    def f(ts):
        return (348, 35.734531, 140.543731, mag, start, status) if start <= ts <= start + timedelta(minutes=dur) else None
    return f


# ---------------------------------------------------------------- api_map_quake

def test_active_quake_wins_regardless_of_at(monkeypatch):
    _use(monkeypatch, active=(349, 36.0, 141.0, 6.1, T0, "active"), by_time=_row())
    q = map_api.api_map_quake(at="2026-11-15T05:40:00")["quake"]
    assert q["fault_id"] == 349 and q["active"] is True
    q = map_api.api_map_quake()["quake"]               # at 이 없어도 진행 중이면 보인다
    assert q["fault_id"] == 349


def test_at_inside_injection_window_picks_that_quake(monkeypatch):
    _use(monkeypatch, by_time=_row())
    q = map_api.api_map_quake(at="2026-11-15T05:45:00")["quake"]
    assert q == {"fault_id": 348, "lat": 35.734531, "lon": 140.543731, "magnitude": 5.9,
                 "origin_time": "2026-11-15T05:37:00", "active": False}


def test_window_edges_are_inclusive(monkeypatch):
    _use(monkeypatch, by_time=_row(dur=16))
    assert map_api.api_map_quake(at="2026-11-15T05:37:00")["quake"] is not None
    assert map_api.api_map_quake(at="2026-11-15T05:53:00")["quake"] is not None
    assert map_api.api_map_quake(at="2026-11-15T05:53:01")["quake"] is None
    assert map_api.api_map_quake(at="2026-11-15T05:36:59")["quake"] is None


def test_at_outside_window_is_none(monkeypatch):
    _use(monkeypatch, by_time=_row())
    assert map_api.api_map_quake(at="2026-11-15T06:40:00")["quake"] is None


def test_no_active_and_no_at_is_none(monkeypatch):
    c = _use(monkeypatch, by_time=_row())
    assert map_api.api_map_quake()["quake"] is None
    assert len(c.log) == 1                             # at 이 없으면 구간 질의는 하지 않는다


def test_bad_at_is_400_not_500(monkeypatch):
    _use(monkeypatch)
    with pytest.raises(HTTPException) as e:
        map_api.api_map_quake(at="어제")
    assert e.value.status_code == 400


def test_z_suffix_at_is_accepted(monkeypatch):
    _use(monkeypatch, by_time=_row())
    assert map_api.api_map_quake(at="2026-11-15T05:45:00Z")["quake"] is not None


def test_missing_quake_table_gives_none_with_note(monkeypatch):
    _use(monkeypatch, boom=pymysql.err.ProgrammingError(1146, "Table 'CM_QUAKE_EVENT' doesn't exist"))
    out = map_api.api_map_quake(at="2026-11-15T05:45:00")
    assert out["quake"] is None and "CM_QUAKE_EVENT" in out["note"]


def test_null_magnitude_stays_none_not_zero(monkeypatch):
    _use(monkeypatch, active=(348, 35.7, 140.5, None, T0, "active"))
    q = map_api.api_map_quake()["quake"]
    assert q["magnitude"] is None                      # 불변규칙 17 — 모름을 0 으로 적지 않는다


def test_connection_closed_and_sql_is_select_only(monkeypatch):
    c = _use(monkeypatch, by_time=_row())
    map_api.api_map_quake(at="2026-11-15T05:45:00")
    assert c.closed
    for sql, _ in c.log:
        assert sql.lstrip().upper().startswith("SELECT")
        assert not re.search(r"\b(INSERT|UPDATE|DELETE|ALTER|DROP)\b", sql, re.I)


def test_response_carries_only_star_and_magnitude_fields(monkeypatch):
    _use(monkeypatch, by_time=_row())
    q = map_api.api_map_quake(at="2026-11-15T05:45:00")["quake"]
    assert set(q) == {"fault_id", "lat", "lon", "magnitude", "origin_time", "active"}   # 지명·진도 등은 넣지 않는다


def test_outline_only_when_asked(monkeypatch):
    _use(monkeypatch)
    monkeypatch.setattr(map_api, "_mini_outline", lambda: {"rings": [], "bbox": None})
    assert "outline" not in map_api.api_map_quake()
    assert "outline" in map_api.api_map_quake(outline=1)


# ---------------------------------------------------------------- _mini_view · _mini_outline

def test_outline_missing_or_empty_path_is_error_not_exception(monkeypatch):
    monkeypatch.setattr(map_api, "_pem_cfg", lambda: {"geo_prefectures": ""})
    assert map_api._mini_outline()["rings"] == [] and map_api._mini_outline()["error"]
    monkeypatch.setattr(map_api, "_pem_cfg", lambda: {"geo_prefectures": "/nonexistent/x.json"})
    out = map_api._mini_outline()
    assert out["rings"] == [] and "없다" in out["error"]


REGIONS = [{"name": "千葉県東方沖", "lat": 35.65, "lon": 140.64}, {"name": "千葉県北西部", "lat": 35.58, "lon": 140.14},
           {"name": "茨城県南部", "lat": 36.06, "lon": 139.93}, {"name": "神奈川・埼玉西部", "lat": 35.56, "lon": 139.33}]
TOKYO_STATION = (35.68, 139.77)


def _cfg_with(monkeypatch, **fault):
    monkeypatch.setattr(map_api, "_pem_cfg", lambda: {"geo_prefectures": str(PREF), "fault": fault})


def test_view_is_the_four_regions_plus_jitter_and_margin(monkeypatch):
    _cfg_with(monkeypatch, quake_regions=REGIONS, quake_jitter_deg=0.1)
    s, w, n, e = map_api._mini_view()
    m = 0.1 + map_api._MINI_MARGIN_DEG
    assert (s, w, n, e) == (round(35.56 - m, 3), round(139.33 - m, 3), round(36.06 + m, 3), round(140.64 + m, 3))
    assert [s, w, n, e] == [34.46, 138.23, 37.16, 141.74]            # 지금 설정의 값(케이 지시 범위와 맞다)
    for lat, lon in [(r["lat"], r["lon"]) for r in REGIONS] + [TOKYO_STATION]:
        assert s < lat < n and w < lon < e                           # 네 진원역과 도쿄역이 모두 안에 있다


def test_view_without_regions_is_none_and_jitter_defaults_to_zero(monkeypatch):
    _cfg_with(monkeypatch)
    assert map_api._mini_view() is None
    _cfg_with(monkeypatch, quake_regions=[])
    assert map_api._mini_view() is None
    _cfg_with(monkeypatch, quake_regions=[{"lat": 35.0, "lon": 139.0}, {"name": "좌표 없음"}])
    assert map_api._mini_view() == [34.0, 138.0, 36.0, 140.0]         # 좌표 없는 진원역은 건너뛰고 흩뜨림 0 이면 여백만


def test_shipped_regions_fit_the_view_with_room_on_the_sea_side():
    """설정 파일의 진원역을 직접 읽어 범위 계산을 대조한다 — 진원역을 고치면 범위가 따라가야 한다."""
    import re
    text = (Path(__file__).resolve().parents[1] / "config" / "datagen.yaml").read_text(encoding="utf-8")
    regs = re.findall(r"\{name: (\S+), lat: ([\d.]+), lon: ([\d.]+)", text)
    assert len(regs) == 4
    east = max(float(lon) for _, _, lon in regs)
    assert east + map_api._MINI_MARGIN_DEG >= 141.6                  # 千葉県東方沖 바다 쪽 여유(케이 지시 141.6 이상)


@pytest.fixture
def real_outline(monkeypatch):
    if not PREF.exists():
        pytest.skip("graphio 도도부현 경계 파일이 없다")
    map_api._FILE_CACHE.clear()
    _cfg_with(monkeypatch, quake_regions=REGIONS, quake_jitter_deg=0.1)
    return map_api._mini_outline()


def test_outline_bbox_is_the_view_and_size_is_small(real_outline):
    import json
    o = real_outline
    assert o["bbox"] == [34.46, 138.23, 37.16, 141.74]
    assert len(json.dumps(o, ensure_ascii=False)) <= 30_000
    assert 5 <= len(o["rings"]) <= 40


def test_outline_keeps_only_rings_touching_the_view(real_outline):
    s, w, n, e = real_outline["bbox"]
    for r in real_outline["rings"]:
        xs = [x for x, _ in r]
        ys = [y for _, y in r]
        assert not (max(xs) < w or min(xs) > e or max(ys) < s or min(ys) > n)    # 범위 밖 고리는 보내지 않는다
    allx = [x for r in real_outline["rings"] for x, _ in r]
    ally = [y for r in real_outline["rings"] for _, y in r]
    assert not any(36.0 < y < 37.5 and x < 120 for x, y in zip(allx, ally))      # 먼 서쪽(규슈 등) 없음
    assert max(ally) < 46 and min(ally) > 24                                     # 홋카이도 북단·오키나와는 이 범위 고리가 아니다


def test_outline_has_tokyo_and_chiba_coast_but_not_hokkaido_or_okinawa(real_outline):
    pts = [(x, y) for r in real_outline["rings"] for x, y in r]
    assert any(139.6 < x < 139.9 and 35.55 < y < 35.8 for x, y in pts)           # 도쿄만 안쪽
    assert any(x > 140.7 and 35.6 < y < 35.8 for x, y in pts)                     # 치바 동쪽 해안
    assert not any(y > 41 for _, y in pts)                                       # 홋카이도 없음
    assert not any(y < 30 for _, y in pts)                                       # 오키나와 없음


def test_outline_is_simplified_finer_than_the_old_overview():
    assert map_api._MINI_TOL_M == 800.0 and map_api._MINI_TOL_M < 3000.0         # 축척이 커져 허용오차를 낮췄다
    assert not hasattr(map_api, "_ring_km2") and not hasattr(map_api, "_MINI_MIN_KM2")


def test_outline_cache_key_depends_on_the_view(monkeypatch):
    if not PREF.exists():
        pytest.skip("graphio 도도부현 경계 파일이 없다")
    map_api._FILE_CACHE.clear()
    _cfg_with(monkeypatch, quake_regions=REGIONS, quake_jitter_deg=0.1)
    a = map_api._mini_outline()
    _cfg_with(monkeypatch, quake_regions=[{"lat": 43.0, "lon": 141.3}], quake_jitter_deg=0.0)   # 홋카이도 쪽
    b = map_api._mini_outline()
    assert a["bbox"] != b["bbox"] and a is not b                                 # 범위가 바뀌면 옛 윤곽을 다시 쓰지 않는다


def test_outline_without_regions_falls_back_to_the_ring_extent(monkeypatch):
    if not PREF.exists():
        pytest.skip("graphio 도도부현 경계 파일이 없다")
    map_api._FILE_CACHE.clear()
    _cfg_with(monkeypatch)
    o = map_api._mini_outline()
    la0, lo0, la1, lo1 = o["bbox"]
    assert o["rings"] and (lo1 - lo0) > 10                                       # 범위가 없으면 전체 윤곽 범위


def test_outline_is_cached_for_same_file(real_outline):
    assert map_api._mini_outline() is real_outline


def test_outline_cites_source_and_license(real_outline):
    assert "CC BY 4.0" in real_outline["source"]


def test_config_has_prefecture_path():
    root = Path(__file__).resolve().parents[1] / "config"
    for name in ("datagen.yaml", "local.yaml"):
        assert "geo_prefectures" in (root / name).read_text(encoding="utf-8"), name


# ---------------------------------------------------------------- map.html 소스

def test_mini_blocks_events_like_other_panels():
    assert re.search(r"\[\s*'sel'\s*,\s*'fbar'\s*,\s*'incs'\s*,\s*'flist'\s*,\s*'mini'\s*\]", MAP_HTML)
    assert "#sel,#fbar,#incs,#legend,#flist,#mini" in MAP_HTML


def test_mini_follows_big_map_and_focus():
    assert "syncKs(); miniView();" in MAP_HTML
    focus = MAP_HTML[MAP_HTML.index("FOCUS===id"):][:400]
    assert focus.count("pollQuake()") == 2              # 풀기·고르기 두 갈래 모두


def test_mini_hidden_without_quake():
    fn = MAP_HTML[MAP_HTML.index("function drawMini"):MAP_HTML.index("function miniView")]
    assert "style.display='none'" in fn and "classList.remove('mini-on')" in fn
    assert "classList.add('mini-on')" in fn
    assert "#mini{" in MAP_HTML and "display:none" in MAP_HTML[MAP_HTML.index("#mini{"):][:200]


def test_mini_uses_same_projection_as_big_map():
    fn = MAP_HTML[MAP_HTML.index("function drawMini"):MAP_HTML.index("function miniView")]
    assert "-my(q.lat)" in fn and "-my(p[1])" in fn and "q.lon" in fn
    assert "x=경도" in MAP_HTML                          # 큰 지도와 같은 체계라고 적혀 있다


def _draw():
    return MAP_HTML[MAP_HTML.index("function drawMini"):MAP_HTML.index("function miniView")]


def test_mini_draws_star_and_magnitude_only():
    fn = _draw()
    assert 'class="ml"' in fn and 'class="mv"' in fn and 'class=ms' in fn and 'class="mm"' in fn
    assert fn.count("<text") == 1                       # 글자는 규모 하나 — 지명은 넣지 않는다
    assert "magnitude.toFixed(1)" in fn
    assert "hypo" not in fn and "name" not in fn.replace("className", "")


def test_regression_no_unquoted_attribute_value_swallowing_the_self_close():
    """11-15 첫 판 — `<rect class=mv id=miniView/>` 는 `/` 까지 id 값이 되어 rect 가 닫히지 않았고, ★·규모가 rect 의 자식이
    되어 그려지지 않았다(rect 안의 도형은 SVG 가 그리지 않는다). 따옴표 없는 속성값 바로 뒤에 `/>` 를 두지 않는다."""
    fn = "\n".join(l for l in _draw().splitlines() if not l.lstrip().startswith("//"))   # 이유를 적은 주석은 뺀다
    assert not re.search(r"=[^\"'\s>]+/>", fn), re.findall(r"\S*=[^\"'\s>]+/>", fn)
    assert 'id=miniView' not in fn and 'id="miniView"' in fn


def test_regression_rect_is_closed_before_the_star_and_text():
    fn = _draw()
    html = fn[fn.index("svg.innerHTML="):fn.index("box.title=")]
    assert '<rect class="mv" id="miniView"></rect>' in html          # 명시적으로 닫는다
    assert html.index("</rect>") < html.index("+ mark") < html.index("<text")     # ★(mark)·규모는 rect 뒤 형제다
    assert '<path class="ml"' in html and html.index('<path class="ml"') < html.index("<rect")   # 윤곽이 맨 아래


@pytest.mark.skipif(NODE is None, reason="node 없음")
def test_regression_drawmini_output_has_siblings_not_children(tmp_path):
    """실제 `drawMini` 를 가짜 DOM 으로 돌려 innerHTML 을 얻고, 문자열 수준에서 rect 가 비어 있고 ★·글자가 형제인지 본다."""
    start = MAP_HTML.index("function drawMini")
    end = MAP_HTML.index("//: 큰 지도의 지금 범위")
    js = tmp_path / "t.js"
    js.write_text("""
const T=(s,o)=>s; const my=lat=>lat; let VB=null; 
const els={mini:{style:{},parentNode:{classList:{add(){},remove(){}}}},miniSvg:{attrs:{},setAttribute(k,v){this.attrs[k]=v},innerHTML:''}};
const document={getElementById:id=>els[id]||null};
let MINI={bbox:[34.46,138.23,37.16,141.74],rings:[[[139,35],[140,35],[140,36],[139,35]]]}, MINI_Q=null, MINI_VB=null;
function miniView(){}
"""+MAP_HTML[start:end]+"""
MINI_Q={lat:35.7345,lon:140.5437,magnitude:5.9}; drawMini(); const inside=els.miniSvg.innerHTML;
MINI_Q={lat:33.9,lon:142.6,magnitude:6.5}; drawMini(); const outside=els.miniSvg.innerHTML;
console.log(JSON.stringify({inside,outside,title:els.mini.title}));
""", encoding="utf-8")
    import json
    r = subprocess.run([NODE, str(js)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    for html in (out["inside"], out["outside"]):
        assert html.count("<rect") == 1 and html.count("</rect>") == 1
        assert re.search(r"<rect[^>]*></rect>", html)               # rect 는 비어 있다(자식 없음)
        assert html.index("</rect>") < html.index('<path class=ms') < html.index("<text")
        assert "M" in html and "5.9" in html or "6.5" in html
    assert "M5.9" in out["inside"] and "M6.5" in out["outside"]
    assert out["inside"].count("L") > 0


def test_out_of_view_epicenter_gets_an_arrow_and_its_own_tooltip():
    fn = _draw()
    assert "const out=ex!==qx||ey!==qy" in fn                        # 범위 밖이면 가장자리 안쪽으로 붙인다
    assert "Math.atan2(qy-ey,qx-ex)" in fn                           # 진원 쪽을 가리키는 방향
    arrow = fn[fn.index("if(out){"):fn.index("}else{")]
    assert arrow.count("L") == 2 and "Z" in arrow                    # 삼각형(점 셋)
    assert "관동 지도 밖 (화살표 방향)" in fn and "관동 지도 (움직이지 않는다)" in fn


def test_mini_range_comes_from_the_server_not_a_padded_guess():
    fn = _draw()
    assert "const x0=lo0, x1=lo1, y0=-my(la1), y1=-my(la0);" in fn   # 서버 여백만 쓴다(3% 패딩은 뺐다)
    assert "pad*" not in fn.split("const pad")[0]


def test_mini_css_clips_what_leaves_the_view():
    assert re.search(r"#mini svg\{[^}]*overflow:hidden", MAP_HTML)


def test_mini_has_no_zoom_or_drag_handlers():
    block = MAP_HTML[MAP_HTML.index("let MINI="):MAP_HTML.index("function miniView")]
    for bad in ("wheel", "mousedown", "pointerdown", "addEventListener"):
        assert bad not in block


def test_mini_polls_on_the_fault_interval_and_asks_outline_once():
    assert "setInterval(pollQuake, FAULT_POLL_MS)" in MAP_HTML
    assert "if(!MINI) q.set('outline','1')" in MAP_HTML
    assert "h.opened_ts" in MAP_HTML


def test_mini_css_uses_theme_variables():
    css = MAP_HTML[MAP_HTML.index("#mini{"):MAP_HTML.index("#mini .mm")][:900]
    for v in ("--sea", "--land", "--land-line"):
        assert v in css


@pytest.mark.skipif(NODE is None, reason="node 없음")
def test_map_script_syntax(tmp_path):
    scripts = re.findall(r"<script[^>]*>(.*?)</script>", MAP_HTML, re.S)
    js = tmp_path / "map.js"
    js.write_text("\n".join(scripts), encoding="utf-8")
    r = subprocess.run([NODE, "--check", str(js)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


# ---------------------------------------------------------------- 번역

NEW = ("진앙 — 관동 지도 (움직이지 않는다)", "진앙 {m} — 관동 지도 (움직이지 않는다) · 네모 = 지금 보는 범위",
       "진앙 {m} — 관동 지도 밖 (화살표 방향) · 네모 = 지금 보는 범위", "규모 모름")


def test_new_phrases_translated_and_present_in_source():
    for k in NEW[1:]:
        assert k in _JA_MAP and _JA_MAP[k].strip()
        assert k in MAP_HTML, k                         # 소스 원문과 사전 키가 한 글자도 어긋나지 않는다
    assert NEW[0] in _JA_MAP and NEW[0].split("—")[0].strip() == "진앙"
    for k in NEW[1:3]:
        assert "{m}" in _JA_MAP[k]                      # 자리표시자 유지
    for k in NEW:
        assert re.search(r"[\u3040-\u30ff\u4e00-\u9fff]", _JA_MAP[k]) if k in _JA_MAP else True
        assert not re.search(r"[\u3040-\u30ff]", k)   # 한국어 쪽에는 가나가 없다


def test_old_japan_overview_phrases_are_gone():
    assert "일본 전도" not in MAP_HTML.replace("일본 전도를 두고", "")      # 머리 주석의 사용자 인용문만 남는다
    assert not any("일본 전도" in k or "日本全図" in v for k, v in _JA_MAP.items())
    assert "관동" in MAP_HTML and "関東地図" in _JA_MAP["진앙 {m} — 관동 지도 (움직이지 않는다) · 네모 = 지금 보는 범위"]
