"""gen-ui 미니 일본 전도 — 27번 진앙 ★ 과 규모 (2026-11-15 사용자 요청 · 맥스 구현).

진앙이 도쿄 밖 바다라 장비 지도 화면 밖이다. 구석에 확대·끌기를 받지 않는 일본 전도를 얹어 진앙 ★ 과 규모만 찍는다.
서버(`/api/map/quake`)가 어느 지진인지 정한다 — 진행 중인 27번, 없으면 고른 장애의 시작 시각을 품은 27번, 없으면 None.

DB 를 쓰지 않는다 — 연결은 가짜이고, 일본 윤곽은 graphio 의 실제 경계 파일이 있으면 그것으로(없으면 건너뜀) 본다.
"""
from __future__ import annotations

import json
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
    monkeypatch.setattr(map_api, "_quake_down", lambda fid: None)      # 정답지 파일을 읽지 않는다(아래 별도 시험)
    return c


def _row(start=T0, dur=16, mag=5.9, status="done"):
    """구간 안이면 지진 행을 돌려주는 by_time — SQL 의 START_TIME <= at <= START_TIME+DURATION 을 흉내낸다."""
    def f(ts):
        return (348, 35.734531, 140.543731, mag, start, status) if start <= ts <= start + timedelta(minutes=dur) else None
    return f


# ---------------------------------------------------------------- api_map_quake

def test_at_given_means_window_match_only_even_when_a_quake_is_active(monkeypatch):
    """11-15 사용자 "지진 시나리오 누를 때만 나와야" — 진행 중인 27번이 있어도 고른 장애가 그 구간 밖이면 None.
    (전에는 active 를 먼저 봐서 DNS 장애를 골라도 지진이 떴다.) at 이 있으면 active 질의를 아예 하지 않는다."""
    c = _use(monkeypatch, active=(349, 36.0, 141.0, 6.1, T0, "active"), by_time=_row())
    assert map_api.api_map_quake(at="2026-11-15T09:10:00")["quake"] is None
    assert not any("STATUS = 'active'" in s for s, _ in c.log) and len(c.log) == 1


def test_at_inside_the_window_picks_that_quake_not_the_active_one(monkeypatch):
    _use(monkeypatch, active=(349, 36.0, 141.0, 6.1, T0 + timedelta(hours=5), "active"), by_time=_row())
    assert map_api.api_map_quake(at="2026-11-15T05:40:00")["quake"]["fault_id"] == 348


def test_without_at_the_active_quake_is_still_returned_by_the_api(monkeypatch):
    """화면은 고른 장애가 없으면 묻지 않지만 API 는 at 없이 부르면 진행 중인 지진을 준다(남긴 동작)."""
    _use(monkeypatch, active=(349, 36.0, 141.0, 6.1, T0, "active"), by_time=_row())
    q = map_api.api_map_quake()["quake"]
    assert q["fault_id"] == 349 and q["active"] is True


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
    focus = MAP_HTML[MAP_HTML.index("if(FOCUS===id){ FOCUS=null;"):][:400]
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
    assert 'class="ml"' in fn and 'class="mv"' in fn and 'class="ms"' in fn and 'class="mm"' in fn
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
const els={mini:{style:{},parentNode:{classList:{add(){},remove(){}}}},miniSvg:{attrs:{},dataset:{},setAttribute(k,v){this.attrs[k]=v},innerHTML:''}};
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
        assert html.index("</rect>") < html.index('<path class="ms"') < html.index("<text")
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


# ══ 11-15 오후 — ★ 물결 · 정지 기지국 음영 · 지진을 골랐을 때만 · 정답지 읽기 전용 마운트 ══════════════════════════

from datagen.ui import faults_api  # noqa: E402

DOWN_TRUTH = {"scenario_id": "F-355", "down_enbs": ["132644", "134429"],
              "down_cells": ["132644-0", "132644-1", "134429-0"]}


class _DownCur:
    def __init__(self, c):
        self.c = c

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.c.log.append((sql, list(params or [])))
        if self.c.boom:
            raise pymysql.err.ProgrammingError(1146, "no table")

    def fetchall(self):
        return self.c.rows


class _DownConn:
    def __init__(self, rows=(), boom=False):
        self.rows, self.boom, self.log, self.closed = list(rows), boom, [], False

    def cursor(self):
        return _DownCur(self)

    def close(self):
        self.closed = True


def _down(monkeypatch, truth, rows=(), boom=False):
    c = _DownConn(rows, boom)
    monkeypatch.setattr(faults_api, "_truth_of", lambda fid: truth)
    monkeypatch.setattr(map_api, "_connect", lambda: c)
    return c


# ── _quake_down ────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("truth", [{}, None, {"scenario_id": "F-355"}])
def test_down_is_none_when_the_answer_sheet_has_no_such_fault_or_no_down_field(monkeypatch, truth):
    """정답지는 최근 장애만 담는다 — 지난 회차는 None(모름)이다. 0곳과 다르다(불변규칙 17)."""
    c = _down(monkeypatch, truth)
    assert map_api._quake_down(355) is None and c.log == []        # 질의도 하지 않는다


def test_down_with_zero_enbs_is_an_empty_list_not_none(monkeypatch):
    """F-348 — 정지 0곳. None(모름)이 아니라 빈 목록이다. 질의 없이 돌려준다."""
    c = _down(monkeypatch, {"down_enbs": []})
    out = map_api._quake_down(348)
    assert out == {"enbs": [], "cells": [], "source": "ground_truth"} and c.log == []


def test_down_uses_down_cells_when_the_sheet_has_them(monkeypatch):
    _down(monkeypatch, DOWN_TRUTH, rows=[("132644", 0, 353.0), ("132644", 1, 410.0), ("132644", 7, 500.0),
                                        ("134429", 0, 441.0)])
    out = map_api._quake_down(355)
    assert out["enbs"] == ["132644", "134429"]
    assert [c["key"] for c in out["cells"]] == ["132644-0", "132644-1", "134429-0"]      # 정답지가 준 키만 — -7 은 아니다
    assert [c["r_m"] for c in out["cells"]] == [353.0, 410.0, 441.0]


def test_down_without_down_cells_falls_back_to_every_cell_of_the_down_enbs(monkeypatch):
    _down(monkeypatch, {"down_enbs": ["132644"]}, rows=[("132644", 1, 400.0), ("132644", 0, 353.0)])
    out = map_api._quake_down(355)
    assert [c["key"] for c in out["cells"]] == ["132644-0", "132644-1"]                  # 옛 정답지 — 정렬


def test_down_unknown_radius_is_none_not_zero(monkeypatch):
    _down(monkeypatch, DOWN_TRUTH, rows=[("132644", 0, None), ("132644", 1, 410.0)])
    cells = {c["key"]: c["r_m"] for c in map_api._quake_down(355)["cells"]}
    assert cells["132644-0"] is None and cells["134429-0"] is None and cells["132644-1"] == 410.0


def test_down_missing_table_gives_unknown_radii_without_raising(monkeypatch):
    c = _down(monkeypatch, DOWN_TRUTH, boom=True)
    out = map_api._quake_down(355)
    assert [x["r_m"] for x in out["cells"]] == [None, None, None] and c.closed


def test_down_query_is_a_select_on_the_mesh_rows_only(monkeypatch):
    c = _down(monkeypatch, DOWN_TRUTH, rows=[])
    map_api._quake_down(355)
    sql, params = c.log[0]
    assert sql.lstrip().upper().startswith("SELECT") and "AREA_LEVEL = 'MESH1K'" in sql and "RADIUS_M" in sql
    assert not re.search(r"\b(INSERT|UPDATE|DELETE|ALTER|DROP)\b", sql, re.I)
    assert params == ["132644", "134429"] and sql.count("%s") == 2 and c.closed          # eNB 는 바인딩, 연결은 닫는다


# ── api_map_quake 의 down ──────────────────────────────────────────────────────────────────

def test_api_attaches_down_only_when_there_is_a_quake(monkeypatch):
    _use(monkeypatch, by_time=_row())
    monkeypatch.setattr(map_api, "_quake_down", lambda fid: {"enbs": ["1"], "cells": [], "fid": fid})
    assert map_api.api_map_quake(at="2026-11-15T05:45:00")["down"] == {"enbs": ["1"], "cells": [], "fid": 348}
    out = map_api.api_map_quake(at="2026-11-15T09:10:00")           # 구간 밖 — 지진 없음
    assert out["quake"] is None and "down" not in out


# ── gen-ui 가 정답지를 읽는다 — 읽기 전용 마운트 ────────────────────────────────────────────

GRAPHIO = Path(__file__).resolve().parents[2] / "graphio-probe-rca"


def _service(text, name):
    m = re.search(rf"^  {re.escape(name)}:\n(.*?)(?=^  [A-Za-z0-9_-]+:\n|^[A-Za-z]|\Z)", text, re.S | re.M)
    assert m, name
    return m.group(1)


@pytest.mark.parametrize("compose", ["docker-compose.yml", "docker-compose.prod.yml"])
def test_gen_ui_mounts_the_answer_sheet_read_only_and_gen_keeps_rw(compose):
    f = GRAPHIO / compose
    if not f.exists():
        pytest.skip("graphio 저장소가 옆에 없다")
    text = f.read_text(encoding="utf-8")
    ui = _service(text, "gen-ui")
    assert "./data/truth:/app/data/truth:ro" in ui and "/app/data/truth:rw" not in ui
    assert "./data/truth:/app/data/truth:rw" in _service(text, "gen")          # 쓰는 쪽은 gen 하나다
    for other in ("web", "corr"):
        assert "./data/truth:/app/data/truth" not in _service(text, other)         # 다른 서비스에는 걸지 않는다


@pytest.mark.parametrize("cfg", ["local.yaml", "prod.yaml.example"])
def test_gen_ui_config_points_the_answer_sheet_inside_the_mount(cfg):
    text = (Path(__file__).resolve().parents[1] / "config" / cfg).read_text(encoding="utf-8")
    assert re.search(r"^\s*truth_out:\s*/app/data/truth/ground_truth\.json\s*$", text, re.M)


# ── pollQuake · drawDown · legendDown — node 로 실제 함수를 돌린다 ─────────────────────────────

def _fn(name, nxt=None, is_async=False):
    start = MAP_HTML.index(("async function " if is_async else "function ") + name)
    end = MAP_HTML.index(nxt, start) if nxt else MAP_HTML.index("\n}\n", start) + 3
    return MAP_HTML[start:end]


def _node(tmp_path, body):
    js = tmp_path / "t.js"
    js.write_text(body, encoding="utf-8")
    r = subprocess.run([NODE, str(js)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    import json
    return json.loads(r.stdout.strip().splitlines()[-1])


POLL = """
const calls=[]; let FOCUS=null, H={}; const histOf=id=>H[id]||null;
let MINI=null, MINI_Q=null, MINI_VB=null, DOWN=null, DOWN_KEY=null, DOWN_N=0, QFOR=null;   // QFOR: 11-16 — 지진이 어느 장애 것인지
const drawMini=()=>calls.push('mini'), drawDown=()=>calls.push('down');
let REPLY=null, FETCHED=[]; const fetch=async u=>{FETCHED.push(u); if(REPLY==='boom') throw new Error('x'); return {json:async()=>REPLY};};
"""


@pytest.mark.skipif(NODE is None, reason="node 없음")
def test_poll_without_a_focused_incident_asks_nothing_and_clears_both(tmp_path):
    out = _node(tmp_path, POLL + _fn("pollQuake", is_async=True) + """
(async()=>{
  MINI_Q={x:1}; DOWN={cells:[1]};
  await pollQuake();                              // FOCUS 없음
  const a={fetched:FETCHED.length, q:MINI_Q, d:DOWN, calls:calls.slice()};
  calls.length=0; FOCUS='I1'; H.I1={};            // 고른 장애에 시작 시각이 없다
  MINI_Q={x:1}; DOWN={cells:[1]}; await pollQuake();
  console.log(JSON.stringify({a, b:{fetched:FETCHED.length, q:MINI_Q, d:DOWN, calls:calls.slice()}}));
})();""")
    for k in ("a", "b"):
        assert out[k]["fetched"] == 0 and out[k]["q"] is None and out[k]["d"] is None
        assert out[k]["calls"] == ["mini", "down"]                  # 둘 다 다시 그려 지운다


@pytest.mark.skipif(NODE is None, reason="node 없음")
def test_poll_with_a_focused_incident_always_sends_at_and_asks_the_outline_once(tmp_path):
    out = _node(tmp_path, POLL + _fn("pollQuake", is_async=True) + """
(async()=>{
  FOCUS='I1'; H.I1={incident_id:'I1',opened_ts:'2026-11-15 11:40:00'};
  REPLY={quake:{fault_id:355}, down:{enbs:['1'],cells:[]}, outline:{bbox:[1,2,3,4],rings:[]}};
  await pollQuake(); const first=FETCHED[0];
  await pollQuake(); const second=FETCHED[1];
  console.log(JSON.stringify({first, second, q:MINI_Q, d:DOWN, calls:calls.slice(), mini:!!MINI, qfor:QFOR}));
})();""")
    assert "at=2026-11-15+11%3A40%3A00" in out["first"] and "at=2026-11-15+11%3A40%3A00" in out["second"]
    assert "outline=1" in out["first"] and "outline" not in out["second"]
    assert out["q"] == {"fault_id": 355} and out["d"]["enbs"] == ["1"] and out["mini"] is True
    assert out["qfor"] == "I1"                                       # 지진이 어느 장애 것인지 기억한다(다른 장애로 넘어가면 쓰지 않는다)
    assert out["calls"] == ["mini", "down", "mini", "down"]


@pytest.mark.skipif(NODE is None, reason="node 없음")
def test_poll_ignores_down_when_the_server_gave_no_quake_and_survives_a_failed_fetch(tmp_path):
    out = _node(tmp_path, POLL + _fn("pollQuake", is_async=True) + """
(async()=>{
  FOCUS='I1'; H.I1={incident_id:'I1',opened_ts:'2026-11-15 09:10:00'};
  REPLY={quake:null, down:{enbs:['9'],cells:[{key:'9-0',r_m:300}]}};        // DNS 장애 — 지진 없음
  await pollQuake(); const r1={q:MINI_Q, d:DOWN};
  REPLY='boom'; calls.length=0; MINI_Q={keep:1}; DOWN={keep:1};
  await pollQuake();                                                         // 실패하면 그리지도 지우지도 않는다
  console.log(JSON.stringify({r1, after:{q:MINI_Q, d:DOWN, calls:calls.slice()}}));
})();""")
    assert out["r1"] == {"q": None, "d": None}                       # 지진 없으면 down 이 와도 쓰지 않는다
    assert out["after"] == {"q": {"keep": 1}, "d": {"keep": 1}, "calls": []}


# ── 소스 검사 — 층 · 범례 · 물결 ───────────────────────────────────────────────────────────

def test_legend_keeps_the_down_row_in_both_branches():
    leg = MAP_HTML[MAP_HTML.index("function legend("):MAP_HTML.index("// ── 야마노테선 집기")]
    assert "if(axis==='none'){ L.innerHTML=''; L.style.display='none'; legendDown(); return; }" in leg
    assert leg.rstrip().rstrip("}").rstrip().endswith("legendDown();")        # 축이 있을 때도 끝에서 붙인다


def test_down_shading_is_one_layer_of_three_overlapped_rings_distinct_from_the_orange_surge():
    """11-15 밤 — 「음영지역 느낌이 덜하다」: 같은 원을 세 겹(둘레 · 바탕 · 빗금)으로 그리고 투명도는 층 하나에만 준다."""
    css = MAP_HTML[MAP_HTML.index("#gDown{pointer-events:none}"):][:1600]
    assert "#gDown{--dn-base:#05070a;--dn-edge:#d8dee9;--dn-hatch:#aeb6c2;opacity:.82}" in css
    assert "body[data-theme=light] #gDown{--dn-base:#4b5563;--dn-edge:#111827;--dn-hatch:#111827;opacity:.6}" in css
    assert "#gDown .do ellipse{fill:var(--dn-base);stroke:var(--dn-edge);stroke-width:3px" in css      # 둘레
    assert "#gDown .df ellipse{fill:var(--dn-base)}" in css and "#gDown .dh ellipse{fill:url(#downHatch)}" in css
    assert "<pattern id=downHatch" in MAP_HTML and MAP_HTML.count('id=downHatch') == 1            # 빗금 무늬 ID 는 하나
    assert "ff" not in css.split("--dn-base:")[1][:8].lower()                                       # 주황(폭주)과 다른 색


def test_wave_is_three_thin_faint_circles_only_for_an_in_view_star():
    fn = _draw()
    assert fn.count("[0,1,2].map") == 1 and '<g class="mw">' in fn
    assert fn.index('<g class="mw">') > fn.index("}else{")                    # 범위 밖 화살표 갈래에는 물결이 없다
    arrow = fn[fn.index("if(out){"):fn.index("}else{")]
    assert "mw" not in arrow
    css = MAP_HTML[MAP_HTML.index("#mini .mw circle{"):][:700]
    assert "stroke-width:1px" in css and "animation:miniWave 3s ease-out infinite" in css
    assert "animation-delay:-1s" in css and "animation-delay:-2s" in css      # 3 초 주기 · 시차 셋
    assert re.search(r"@keyframes miniWave\{0%\{transform:scale\(\.15\);opacity:\.45\}100%\{transform:scale\(1\);opacity:0\}\}", css)


def test_wave_respects_reduced_motion():
    assert "@media (prefers-reduced-motion:reduce){#mini .mw{display:none}}" in MAP_HTML


def test_mini_is_not_redrawn_when_the_same_quake_comes_again():
    fn = _draw()
    assert "const key=JSON.stringify([q,MINI.bbox]);" in fn
    assert "if(svg.dataset.k===key){ miniView(); return; }" in fn
    assert fn.index("if(svg.dataset.k===key)") < fn.index("svg.innerHTML=")    # 같으면 innerHTML 을 다시 쓰기 전에 나간다


@pytest.mark.skipif(NODE is None, reason="node 없음")
def test_drawmini_same_quake_twice_does_not_rewrite_the_svg_and_a_new_one_does(tmp_path):
    start = MAP_HTML.index("function drawMini")
    end = MAP_HTML.index("//: 큰 지도의 지금 범위")
    out = _node(tmp_path, """
const T=(s,o)=>s; const my=lat=>lat; let VB=null; let writes=0;
const svg={attrs:{},dataset:{},setAttribute(k,v){this.attrs[k]=v}, set innerHTML(v){writes++; this._h=v}, get innerHTML(){return this._h}};
const els={mini:{style:{},parentNode:{classList:{add(){},remove(){}}}},miniSvg:svg};
const document={getElementById:id=>els[id]||null};
let MINI={bbox:[34.46,138.23,37.16,141.74],rings:[[[139,35],[140,35],[140,36],[139,35]]]}, MINI_Q=null, MINI_VB=null;
function miniView(){}
"""+MAP_HTML[start:end]+"""
MINI_Q={lat:35.5,lon:140.0,magnitude:6.2}; drawMini(); drawMini(); const same=writes;
MINI_Q={lat:35.6,lon:140.0,magnitude:6.2}; drawMini(); const waves=(svg.innerHTML.match(/<circle/g)||[]).length;
MINI_Q={lat:33.9,lon:142.6,magnitude:6.5}; drawMini(); const arrowWaves=(svg.innerHTML.match(/<circle/g)||[]).length;
console.log(JSON.stringify({same, after:writes, waves, arrowWaves}));""")
    assert out == {"same": 1, "after": 2 + 1, "waves": 3, "arrowWaves": 0}    # 같은 지진 둘째 호출은 쓰지 않는다 · 별 아래 물결 3 · 화살표는 0


def test_down_phrases_are_translated_and_the_old_wording_is_gone():
    """11-15 오후 — 영향 층 · 정지 한 줄로 묶음. 새 4개는 번역이 있고 화면에 쓰이며, 지운 3개는 소스에 남지 않는다."""
    for k in ("영향 지역(추정 진도)", "셀 {c}", "통화 끊김(셀 정지 의심)", "심하게 나빠짐", "나빠짐", "실제 정지(정답)",
              "셀 {c} · eNB ID {n}"):
        assert k in _JA_MAP and _JA_MAP[k].strip() and k in MAP_HTML, k
    assert "{c}" in _JA_MAP["셀 {c}"] and {"{c}", "{n}"} <= set(re.findall(r"\{\w+\}", _JA_MAP["셀 {c} · eNB ID {n}"]))
    assert "T('통화 끊김(정지)')" not in MAP_HTML and "기지국 정지 의심" not in MAP_HTML and "기지국 정지 의심" not in "".join(_JA_MAP)
    # 11-15 오후 맥스 — 「기지국 정지」·「탐지 eNB … 실제 eNB …」 한 줄은 네 줄(끊김·심함·나쁨·정답)로 갈렸다
    assert "T('기지국 정지')" not in MAP_HTML and "T('탐지 eNB {e} · 실제 eNB {t}'" not in MAP_HTML
    for old in ("정지 판정 영역", "눌러서 켜고 끈다 — 생성기가 실제로 끈 셀(정답 · 비교용)", "eNB {e} · 셀 {c}"):
        assert old not in _JA_MAP and f"T('{old}'" not in MAP_HTML, old      # 주석에 옛 말이 남는 것은 상관없다 — 화면에 쓰는 것만
    assert "T('정지 기지국 영역')" not in MAP_HTML and "정지 기지국 영역" not in _JA_MAP


# ══ 11-15 낮 — 정지 음영 = 탐지(D4 C-01) 셀 · 정답은 점선 비교 · 첫 로드 회귀 ══════════════════════════════════

def test_layers_are_ordered_kde_then_detected_down_then_truth_then_geo_and_draw_does_not_clear_them():
    assert "<g id=gKde></g><g id=gImpact></g><g id=gSevY></g><g id=gSevR></g><g id=gDown></g><g id=gDownT></g><g id=gGeo></g>" in MAP_HTML
    draw = MAP_HTML[MAP_HTML.index("function draw()"):][:3500]
    assert "drawDown();" in draw and all(x not in draw for x in ("gDown", "gImpact", "gSev"))


def test_truth_layer_is_a_green_solid_ring_only_and_the_detected_layer_is_the_hatched_fill():
    """11-16 사용자 — 흰 점선이 안 보여 녹색 실선 고리로 바꿨다. 채우지 않아야 아래 탐지 음영이 보인다."""
    css = MAP_HTML[MAP_HTML.index("#gDown{pointer-events:none}"):][:2400]
    truth = css[css.index("#gDownT ellipse{"):][:200]
    assert "fill:none" in truth and "stroke:#22c55e" in truth and "stroke-width:2.5px" in truth
    assert "stroke-dasharray" not in truth                           # 점선이 아니다
    assert "body[data-theme=light] #gDownT ellipse{stroke:#15803d}" in css   # 밝은 테마는 짙은 녹색
    assert "pointer-events:none" in css.split("#gDownT")[1][:60]


def _rows_with_silent(monkeypatch, per_incident, radius_rows=()):
    """`_silent_cells` 용 가짜 DB — 조각마다 D4 질의에 per_incident[i] 를 돌려주고, 반경 질의에 radius_rows 를 돌려준다."""
    log = []
    answers = list(per_incident)

    class Cur:
        def __enter__(s): return s
        def __exit__(s, *a): return False
        def execute(s, sql, params=None):
            log.append((" ".join(sql.split()), list(params or [])))
            if s_boom[0]:
                raise pymysql.err.ProgrammingError(1146, "no table")
            s.kind = "radius" if "CM_CELL_GEO_MAP" in sql else "d4"
        def fetchall(s):
            return list(radius_rows) if s.kind == "radius" else [(k,) for k in answers.pop(0)]

    s_boom = [False]

    class Conn:
        closed = False
        def cursor(s): return Cur()
        def close(s): s.closed = True

    c = Conn()
    monkeypatch.setattr(map_api, "_connect", lambda: c)
    return log, c, s_boom


def _member(iid, kpi, cells, lo="2026-11-15 11:40:00", hi="2026-11-15 12:03:00"):
    return {"incident_id": iid, "trigger_kpi": kpi, "cells": cells, "opened_ts": lo, "updated_ts": hi}


def test_silent_cells_ignore_surge_only_pieces_and_do_not_query_for_them(monkeypatch):
    log, c, _ = _rows_with_silent(monkeypatch, [])
    out, radius = map_api._silent_cells([_member("A", "C-24", ["1-0"]), _member("B", "U-12", ["2-0"])])
    assert out == {} and radius == {} and log == []                  # 폭주(C-24)만 있는 조각은 꺼진 것이 아니다


def test_silent_cells_read_d4_c01_cell_events_inside_the_piece_window(monkeypatch):
    log, c, _ = _rows_with_silent(monkeypatch, [["1-0", "1-1"], ["5-0"]],
                                  radius_rows=[("1", 0, 353.0), ("1", 1, None), ("5", 0, 441.0)])
    out, radius = map_api._silent_cells([_member("A", "C-01,C-24", ["1-1", "1-0", "1-9"]),
                                         _member("B", "C-01", ["5-0"], lo="2026-11-15T11:48:00", hi=None)])
    assert out == {"A": ["1-0", "1-1"], "B": ["5-0"]}
    assert radius == {"1-0": 353.0, "1-1": None, "5-0": 441.0}       # 반경을 모르면 None
    sql, params = log[0]
    for need in ("DETECTOR = 'D4'", "KPI = 'C-01'", "SCOPE_TYPE = 'CELL'", "COALESCE(CANCELLED, 0) = 0",
                 "EVENT_TIME >= %s", "EVENT_TIME <= %s"):
        assert need in sql, need
    assert params == ["2026-11-15 11:40:00", "2026-11-15 12:03:00", "1-0", "1-1", "1-9"]    # 구간 + 정렬된 셀
    sql2, params2 = log[1]
    assert "EVENT_TIME <= %s" not in sql2 and params2 == ["2026-11-15 11:48:00", "5-0"]     # 끝 시각이 없으면 상한 없음 · T 를 공백으로
    assert all(s.startswith("SELECT") for s, _ in log) and c.closed


def test_silent_cells_are_unknown_when_the_table_is_missing(monkeypatch):
    log, c, boom = _rows_with_silent(monkeypatch, [["1-0"]])
    boom[0] = True
    assert map_api._silent_cells([_member("A", "C-01", ["1-0"])]) == ({}, {}) and c.closed


def test_cell_radius_maps_every_asked_key_and_asks_by_enb_once(monkeypatch):
    log, c, _ = _rows_with_silent(monkeypatch, [], radius_rows=[("1", 0, 353.0), ("2", 4, 500.0)])
    got = map_api._cell_radius(c, ["1-0", "1-1", "2-4"])
    assert got == {"1-0": 353.0, "1-1": None, "2-4": 500.0}           # 표에 없는 셀은 None — 지어내지 않는다
    assert len(log) == 1 and log[0][1] == ["1", "2"] and "AREA_LEVEL = 'MESH1K'" in log[0][0]
    assert map_api._cell_radius(c, []) == {} and len(log) == 1        # 빈 입력은 질의하지 않는다


class _Resp:
    def __init__(self, doc): self.doc = doc
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def read(self): return json.dumps(self.doc).encode()


def test_api_map_event_adds_silent_per_member_and_the_radius_table(monkeypatch):
    import urllib.request
    graphio_doc = {"cells": [{"key": "1-0", "lat": 35.7, "lon": 139.8}, {"key": "5-0", "lat": 35.71, "lon": 139.81}],
                   "members": [_member("A", "C-01,C-24", ["1-0"]), _member("B", "C-24", ["5-0"])]}
    monkeypatch.setattr(map_api, "_pem_cfg", lambda: {"graphio_api": "http://g:8008"})
    monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout=0: _Resp(graphio_doc))
    monkeypatch.setattr(map_api, "_silent_cells", lambda raw: ({"A": ["1-0"]}, {"1-0": 353.0}))
    out = map_api.api_map_event("IX1")
    by = {m["incident_id"]: m for m in out["members"]}
    assert by["A"]["silent"] == ["1-0"] and by["B"]["silent"] == []   # 폭주 전용 조각은 빈 목록(없음 · 모름이 아니다)
    assert out["silent_r"] == {"1-0": 353.0}
    assert out["enbs"] == ["1", "5"] and out["cells"] == 2             # 기존 칸은 그대로


# ── 화면 함수 — node ───────────────────────────────────────────────────────────────────────

DOWN2 = """
const mkEl=tag=>({tag,attrs:{},children:[],setAttribute(k,v){this.attrs[k]=v},append(...c){this.children.push(...c)},appendChild(c){this.children.push(c)},cloneNode(){const e=mkEl(this.tag); e.attrs={...this.attrs}; return e;}});
const el=mkEl, mx=x=>x, my=y=>y;
const T=(s,o)=>o?s.replace(/\\{(\\w+)\\}/g,(_,k)=>o[k]):s;
let D=null, DOWN=null, DOWN_KEY=null, DOWN_N=0, DOWN_E=0, DOWN_T_N=0, DOWN_TE=0, IMPACT={}, IMPACT_WAIT={}, IMPACT_N=0, MINI_Q=null, FOCUS=null;
let IMPACT_ON=false, SEV_N={gray:0,red:0,yellow:0}, TRUTH_ON=false, DOWN_TOFF=false;
Object.defineProperty(globalThis,'QFOR',{get:()=>FOCUS});   // 지진은 늘 지금 고른 장애 것 — 아니면 MINI_Q=null 로 시험한다
const MESH_AXES=new Set(['MESH250','MESH500','MESH1K']); const curPart=h=>PART, curForm=h=>h.form||'CELL', drawFocus=()=>{}; let VB=null; let SIL={}, SIL_WAIT={}; const SIL_TTL_MS=30000; let PART=null, FETCHED=[];
const H={}; const histOf=id=>H[id]||null; const isEvent=h=>!!h.members; const partGroups=h=>h.groups||[]; const evtOf=h=>h;
let IMPACT_REPLY={fault_id:355,cells:0,pts:[]};
const fetch=async u=>{FETCHED.push(u); if(IMPACT_REPLY==='boom'){ await new Promise(r=>setTimeout(r,1)); throw new Error('x'); }   // 실제 네트워크 실패처럼 한 틱 뒤
  return {json:async()=>u.includes('/impact')?IMPACT_REPLY:({members:[]})};};
const box=()=>({children:[],replaceCount:0,replaceChildren(){this.children=[];this.replaceCount++},appendChild(c){this.children.push(c)}});
const g=box(), gt=box(), gi=box(), gy=box(), gr=box();
const gN=()=>g.children[0]?g.children[0].children.length:0;   // 정지는 세 겹(둘레·바탕·빗금)에 같은 원 — 셀 수는 한 겹으로 센다
const legendBox={children:[],style:{display:''},
  querySelectorAll(){return {forEach:f=>this.children.filter(c=>c.className.startsWith('lg-down')).forEach(f)}},
  appendChild(c){this.children.push(c)}};
const mkDiv=()=>({tag:'div',className:'',innerHTML:'',title:'',remove(){legendBox.children=legendBox.children.filter(c=>c!==this)}});
const document={getElementById:id=>id==='gDown'?g:id==='gDownT'?gt:id==='gImpact'?gi:id==='gSevY'?gy:id==='gSevR'?gr:id==='legend'?legendBox:null,createElement:mkDiv,
  createDocumentFragment:()=>box()};
const CELLS=[{key:'1-0',enb:'1',lat:35.7,lon:139.8},{key:'1-1',enb:'1',lat:35.71,lon:139.81},{key:'2-0',enb:'2',lat:35.72,lon:139.82}];
"""


def _down2(tmp_path, body):
    fns = (_fn("formOf") + _fn("silentOf") + _fn("downShape") + _fn("impactOf") + _fn("drawDown", "function legendDown") + _fn("legendDown"))
    return _node(tmp_path, DOWN2 + fns + body)


@pytest.mark.skipif(NODE is None, reason="node 없음")
def test_main_shading_is_the_detected_silent_cells_with_radius_only(tmp_path):
    out = _down2(tmp_path, """
D={cells:CELLS}; MINI_Q={fault_id:355}; FOCUS='I1'; IMPACT[355]={fault_id:355,cells:0,pts:[]};
H.I1={incident_id:'I1',closed_ts:'x',members:[{incident_id:'A'}],groups:[]};
SIL.I1={t:Date.now(),doc:{members:[{incident_id:'A',silent:['1-0','1-1','2-0','9-9']}],silent_r:{'1-0':353,'1-1':null,'2-0':441,'9-9':300}}};
drawDown();
console.log(JSON.stringify({n:gN(), truth:gt.children.length, N:DOWN_N, E:DOWN_E,
  rows:legendBox.children.map(c=>c.innerHTML), display:legendBox.style.display, fetched:FETCHED.length}));""")
    assert out["n"] == 2 and out["N"] == 2 and out["E"] == 2          # 반경 null · 지도에 없는 셀 하나씩은 그리지 않는다
    assert out["truth"] == 0 and len(out["rows"]) == 3               # 정답은 「정답 표시」를 켜야 나온다 · 줄은 끊김·심함·나쁨
    assert "통화 끊김(셀 정지 의심)" in out["rows"][0] and "셀 2" in out["rows"][0] and out["display"] == ""
    assert out["fetched"] == 0                                        # 끝난 장애는 캐시가 있으면 다시 묻지 않는다


@pytest.mark.skipif(NODE is None, reason="node 없음")
def test_tab_selects_only_that_pieces_silent_cells_by_incident_id_not_by_number(tmp_path):
    out = _down2(tmp_path, """
const doc={members:[{incident_id:'A',silent:['1-0','1-1']},{incident_id:'B',silent:['2-0']},{incident_id:'C',silent:[]}],
  silent_r:{'1-0':300,'1-1':300,'2-0':300}};
H.I1={incident_id:'I1',closed_ts:'x',members:[{incident_id:'A'},{incident_id:'B'},{incident_id:'C'}],
  groups:[{idx:[2]},{idx:[1,0]}]};
SIL.I1={t:Date.now(),doc};
const keys=(p)=>{PART=p; return silentOf(H.I1).gray.map(x=>x.key).sort();};
console.log(JSON.stringify({all:keys(null), t0:keys(0), t1:keys(1), none:keys(5)}));""")
    assert out["all"] == ["1-0", "1-1", "2-0"]                       # 전체 = 합집합
    assert out["t0"] == []                                           # 폭주 전용 조각 탭 — 칠할 것이 없다
    assert out["t1"] == ["1-0", "1-1", "2-0"]                        # 탭 2 = 조각 B·A (번호가 아니라 ID)
    assert out["none"] == ["1-0", "1-1", "2-0"]                      # 없는 탭 번호는 전체로 본다(기존 동작)


@pytest.mark.skipif(NODE is None, reason="node 없음")
def test_silent_cells_are_fetched_once_and_refreshed_every_30s_only_while_live(tmp_path):
    out = _down2(tmp_path, """
H.L={incident_id:'L',members:[{incident_id:'A'}],groups:[]};                // 진행 중(closed_ts 없음)
H.E={incident_id:'E',closed_ts:'x',members:[{incident_id:'A'}],groups:[]}; // 끝남
const r0=silentOf(H.L); const r1=silentOf(H.L);                              // 첫 호출 — 모름(null) · 대기 중이라 한 번만 묻는다
const asked1=FETCHED.length;
(async()=>{
  await new Promise(r=>setTimeout(r,20));
  const cached=silentOf(H.L); const asked2=FETCHED.length;                  // 캐시가 생겼다 — 30초 안이라 다시 묻지 않는다
  SIL.L.t=Date.now()-31000; silentOf(H.L); const asked3=FETCHED.length;     // 낡았다 — 진행 중이라 다시 묻는다
  await new Promise(r=>setTimeout(r,20));
  SIL.E={t:Date.now()-99999,doc:{members:[]}}; silentOf(H.E); const asked4=FETCHED.length;   // 끝난 장애는 낡아도 묻지 않는다
  console.log(JSON.stringify({r0, r1, asked1, asked2, asked3, asked4, cached:!!cached&&Array.isArray(cached.gray), url:FETCHED[0]}));
})();""")
    assert out["r0"] is None and out["r1"] is None and out["asked1"] == 1
    assert out["asked2"] == 1 and out["cached"] is True and out["asked3"] == 2 and out["asked4"] == 2
    assert out["url"] == "/api/map/event/L?states=1"           # 셀 상태(심각도)도 함께 받는다


@pytest.mark.skipif(NODE is None, reason="node 없음")
def test_truth_layer_and_its_legend_row_appear_only_when_the_truth_checkbox_is_on(tmp_path):
    """11-16 사용자 "정답이라는 게 함께 나오네? 빼줘. 위에 체크박스 만들고 정답 표시 누를 때만" — 기본은 꺼짐."""
    out = _down2(tmp_path, """
D={cells:CELLS}; MINI_Q={fault_id:355}; FOCUS='I1'; IMPACT[355]={fault_id:355,cells:0,pts:[]};
H.I1={incident_id:'I1',closed_ts:'x',members:[{incident_id:'A'}],groups:[]};
SIL.I1={t:Date.now(),doc:{members:[{incident_id:'A',silent:['1-0']}],silent_r:{'1-0':353}}};
DOWN={enbs:['1','2'],cells:[{key:'1-0',r_m:353},{key:'1-1',r_m:353},{key:'2-0',r_m:441}]};
const snap=()=>({truth:gt.children.length, main:gN(), T:DOWN_T_N, rows:legendBox.children.map(c=>[c.className,c.innerHTML,!!c.onclick])});
drawDown(); const off=snap();
TRUTH_ON=true; drawDown(); const on=snap();
TRUTH_ON=false; drawDown(); const off2=snap();
console.log(JSON.stringify({off,on,off2}));""")
    for k in ("off", "off2"):                                        # 켜지 않으면 점선도 정답 줄도 없다
        assert out[k]["truth"] == 0 and out[k]["main"] == 1 and len(out[k]["rows"]) == 3
        assert not any("정답" in r[1] for r in out[k]["rows"])
    assert out["on"]["truth"] == 3 and out["on"]["T"] == 3 and out["on"]["main"] == 1      # 켜면 점선 세 셀
    rows = out["on"]["rows"]
    assert len(rows) == 4 and all(r[0] == "lg-down" and r[2] is False for r in rows)       # 눌러도 아무 일 없다
    assert "셀 3 · eNB ID 2" in rows[3][1] and "실제 정지(정답)" in rows[3][1]
    assert "DOWN_T_ON" not in MAP_HTML and "lg-down-t" not in MAP_HTML


@pytest.mark.skipif(NODE is None, reason="node 없음")
def test_truth_row_shows_a_question_mark_without_an_answer_sheet_and_nothing_without_a_quake(tmp_path):
    out = _down2(tmp_path, """
D={cells:CELLS}; MINI_Q={fault_id:355}; FOCUS='I1'; TRUTH_ON=true; IMPACT[355]={fault_id:355,cells:0,pts:[]};
H.I1={incident_id:'I1',closed_ts:'x',members:[],groups:[]};
SIL.I1={t:Date.now(),doc:{members:[],silent_r:{}}};
drawDown(); const noSheet=legendBox.children.map(c=>c.innerHTML);
MINI_Q=null; DOWN=null; DOWN_KEY=null; DOWN={enbs:['1'],cells:[{key:'1-0',r_m:353}]};   // 지진이 아닌 장애 — 켜도 아무것도 그리지 않는다
drawDown();
console.log(JSON.stringify({noSheet, truth:gt.children.length, rows:legendBox.children.length}));""")
    assert len(out["noSheet"]) == 4 and "eNB ID" not in out["noSheet"][3] and out["noSheet"][3].endswith("<b>?</b>")
    assert out["truth"] == 0 and out["rows"] == 3                      # 지진이 아니면 정답 줄 없이 셀 음영 줄만 남는다


def test_truth_checkbox_is_in_the_top_bar_off_by_default_forgotten_and_translated():
    assert "<input type=checkbox id=truthOn>" in MAP_HTML and "checked" not in MAP_HTML.split("id=truthOn>")[0][-40:]
    assert "document.getElementById('truthOn').onchange=e=>{ TRUTH_ON=e.target.checked; drawDown(); };" in MAP_HTML
    assert "let TRUTH_ON=false;" in MAP_HTML
    assert "gp-map-truth" not in MAP_HTML                                            # 기억하지 않는다
    assert "정답 표시" in _JA_MAP and _JA_MAP["정답 표시"] == "正解表示"
    assert re.search(r"<label[^>]*><input type=checkbox id=truthOn> 정답 표시</label>", MAP_HTML)


@pytest.mark.skipif(NODE is None, reason="node 없음")
def test_impact_is_drawn_per_band_in_one_layer_each_and_counted_in_the_legend(tmp_path):
    out = _down2(tmp_path, """
IMPACT_ON=true;   // 기본은 꺼짐 — 켜 둔 코드가 그대로 도는지 본다
D={cells:CELLS}; MINI_Q={fault_id:355}; FOCUS='I1';
H.I1={incident_id:'I1',closed_ts:'x',members:[],groups:[]};
SIL.I1={t:Date.now(),doc:{members:[],silent_r:{}}};
IMPACT[355]={fault_id:355,cells:9,enb:4,pts:[[139.8,35.7,300,'3'],[139.8,35.7,300,'4'],[139.8,35.7,300,'4'],[139.8,35.7,300,'5-']]};
drawDown();
console.log(JSON.stringify({layers:gi.children.map(c=>[c.attrs['class'],c.children.length]), N:IMPACT_N,
  rows:legendBox.children.map(c=>c.innerHTML)}));""")
    assert out["layers"] == [["ib3", 1], ["ib4", 2], ["ib5m", 1]]          # 띠 순서 3 → 4 → 5약 · 위로 갈수록 진하게
    assert out["N"] == 9 and "셀 9" in out["rows"][0] and "영향 지역(추정 진도)" in out["rows"][0]
    assert "통화 끊김(셀 정지 의심)" in out["rows"][1]


@pytest.mark.skipif(NODE is None, reason="node 없음")
def test_impact_is_asked_once_per_fault_and_redrawn_when_it_arrives(tmp_path):
    out = _down2(tmp_path, """
IMPACT_ON=true;   // 기본은 꺼짐 — 켜 둔 코드가 그대로 도는지 본다
D={cells:CELLS}; MINI_Q={fault_id:355}; FOCUS='I1';
H.I1={incident_id:'I1',closed_ts:'x',members:[],groups:[]};
SIL.I1={t:Date.now(),doc:{members:[],silent_r:{}}};
IMPACT_REPLY={fault_id:355,cells:2,pts:[[139.8,35.7,300,'4']]};
drawDown(); drawDown(); const early=gi.children.length, asked=FETCHED.filter(u=>u.includes('/impact')).length;
(async()=>{ await new Promise(r=>setTimeout(r,30));
  const late=gi.children.length; drawDown(); drawDown();
  console.log(JSON.stringify({early,asked,late,total:FETCHED.filter(u=>u.includes('/impact')).length,url:FETCHED[0],
    rows:legendBox.children.length})); })();""")
    assert out["early"] == 0 and out["asked"] == 1 and out["late"] == 1 and out["total"] == 1
    assert out["url"] == "/api/map/quake/355/impact" and out["rows"] == 4


@pytest.mark.skipif(NODE is None, reason="node 없음")
def test_impact_failure_or_server_error_draws_no_impact_and_keeps_the_down_row(tmp_path):
    out = _down2(tmp_path, """
IMPACT_ON=true;   // 기본은 꺼짐 — 켜 둔 코드가 그대로 도는지 본다
D={cells:CELLS}; MINI_Q={fault_id:355}; FOCUS='I1';
H.I1={incident_id:'I1',closed_ts:'x',members:[],groups:[]};
SIL.I1={t:Date.now(),doc:{members:[],silent_r:{}}};
IMPACT_REPLY={error:'규모·깊이를 모른다',enbs:[]};
drawDown();
(async()=>{ await new Promise(r=>setTimeout(r,30));
  console.log(JSON.stringify({imp:gi.children.length, N:IMPACT_N, cached:IMPACT[355]===null,
    rows:legendBox.children.map(c=>c.innerHTML)})); })();""")
    assert out["imp"] == 0 and out["N"] == 0 and out["cached"] is True      # 모름 — 지어내지 않는다
    assert len(out["rows"]) == 3 and "통화 끊김(셀 정지 의심)" in out["rows"][0]      # 정지 줄은 그대로


@pytest.mark.skipif(NODE is None, reason="node 없음")
def test_impact_network_failure_is_not_retried_in_a_tight_loop(tmp_path):
    """받기가 실패(서버 내려감 · 네트워크)하면 한 번만 묻고 포기해야 한다 — 실패한 뒤 `finally` 가 `drawDown` 을 부르는데
    캐시가 비어 있으면 다시 묻고 또 실패하고… 를 무한히 되풀이한다(11-15 맥스 구현 · 에스터 시험이 잡음)."""
    out = _down2(tmp_path, """
IMPACT_ON=true;   // 기본은 꺼짐 — 켜 둔 코드가 그대로 도는지 본다
D={cells:CELLS}; MINI_Q={fault_id:355}; FOCUS='I1'; IMPACT_REPLY='boom';
H.I1={incident_id:'I1',closed_ts:'x',members:[],groups:[]};
SIL.I1={t:Date.now(),doc:{members:[],silent_r:{}}};
drawDown();
setTimeout(()=>{ console.log(JSON.stringify({asked:FETCHED.filter(u=>u.includes('/impact')).length, imp:gi.children.length}));
  process.exit(0); },60);""")
    assert out["imp"] == 0 and out["asked"] <= 2, f"실패한 영향 요청을 {out['asked']}번 되풀이했다"


@pytest.mark.skipif(NODE is None, reason="node 없음")
def test_legend_rows_are_kept_with_zero_on_a_cell_tab_and_absent_on_other_forms_or_without_focus(tmp_path):
    """11-16 표식 형태별 탭 — 셀 탭이면(지진이든 아니든) 세 줄이 0 까지 나오고, 셀 탭이 아니거나 고른 장애가 없으면 없다."""
    out = _down2(tmp_path, """
D={cells:CELLS}; H.I1={incident_id:'I1',closed_ts:'x',members:[],groups:[]}; IMPACT[355]={fault_id:355,cells:0,pts:[]};
SIL.I1={t:Date.now(),doc:{members:[],silent_r:{}}};
FOCUS='I1'; H.I1.form='CELL'; MINI_Q={fault_id:355}; drawDown(); const quake=legendBox.children.map(c=>c.innerHTML);
MINI_Q=null; DOWN_KEY=null; drawDown(); const plain=legendBox.children.map(c=>c.innerHTML);   // 지진이 아닌 셀 탭 — 정답 줄만 없다
H.I1.form='ADM3'; DOWN_KEY=null; drawDown(); const area={rows:legendBox.children.length, main:gN(), display:legendBox.style.display};
FOCUS=null; DOWN_KEY=null; drawDown(); const none={rows:legendBox.children.length, main:gN()};
console.log(JSON.stringify({quake,plain,area,none}));""")
    assert len(out["quake"]) == 3 and all("셀 0" in r for r in out["quake"])           # 0곳도 적는다 — 아직 못 잡은 것과 음영 없음의 구분
    assert out["plain"] == out["quake"]
    assert out["area"] == {"rows": 0, "main": 0, "display": "none"}                    # 지역 탭에는 셀 음영도 줄도 없다
    assert out["none"] == {"rows": 0, "main": 0}


@pytest.mark.skipif(NODE is None, reason="node 없음")
def test_draw_down_skips_when_nothing_changed_and_waits_for_the_map(tmp_path):
    out = _down2(tmp_path, """
MINI_Q={fault_id:355}; FOCUS='I1'; IMPACT[355]={fault_id:355,cells:0,pts:[]};
H.I1={incident_id:'I1',closed_ts:'x',members:[{incident_id:'A'}],groups:[]};
SIL.I1={t:Date.now(),doc:{members:[{incident_id:'A',silent:['1-0']}],silent_r:{'1-0':353}}};
drawDown(); const early=gN();                           // 지도 셀(D) 아직 없음 — 그리지 않는다
D={cells:CELLS}; drawDown(); const late=gN(), rc=g.replaceCount;
drawDown(); drawDown();
console.log(JSON.stringify({early, late, same:g.replaceCount===rc}));""")
    assert out == {"early": 0, "late": 1, "same": True}


# ── 첫 로드 회귀 — 함수가 사라지면 지도가 「불러오는 중」에서 멈춘다(11-15 miniView 사건) ─────────────────────

def _defs(html):
    names = set(re.findall(r"(?:^|\n)\s*(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(", html))
    names |= set(re.findall(r"(?:^|\n)\s*(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>", html))
    return names


def test_every_function_the_first_load_calls_is_defined_exactly_once():
    """`setVB` 가 `miniView()` 를 부르는데 그 함수가 지워져 첫 로드에서 ReferenceError 가 났다 — 첫 로드 경로의 함수는 모두 있어야 한다."""
    defs = re.findall(r"(?:^|\n)\s*(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(", MAP_HTML)
    for name in ("setVB", "miniView", "drawMini", "drawDown", "legendDown", "silentOf", "downShape", "pollQuake",
                 "drawScale", "syncKs", "draw", "legend"):
        assert defs.count(name) == 1, (name, defs.count(name))
    setvb = MAP_HTML[MAP_HTML.index("function setVB"):][:1200]
    for called in re.findall(r"\b(miniView|drawScale|syncKs)\(", setvb):
        assert called in defs


def test_no_function_defined_in_the_last_commit_was_lost():
    f = Path(__file__).resolve().parents[1] / "src" / "datagen" / "ui" / "static" / "map.html"
    r = subprocess.run(["git", "show", "HEAD:src/datagen/ui/static/map.html"], cwd=f.parents[4], capture_output=True, text=True)
    if r.returncode != 0:
        pytest.skip("git 이력을 읽을 수 없다")
    lost = _defs(r.stdout) - _defs(MAP_HTML)
    assert lost == set(), f"직전 커밋에 있던 함수가 사라졌다: {sorted(lost)}"
