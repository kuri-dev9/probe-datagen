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
let MINI=null, MINI_Q=null, MINI_VB=null, DOWN=null, DOWN_KEY=null, DOWN_N=0;
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
  FOCUS='I1'; H.I1={opened_ts:'2026-11-15 11:40:00'};
  REPLY={quake:{fault_id:355}, down:{enbs:['1'],cells:[]}, outline:{bbox:[1,2,3,4],rings:[]}};
  await pollQuake(); const first=FETCHED[0];
  await pollQuake(); const second=FETCHED[1];
  console.log(JSON.stringify({first, second, q:MINI_Q, d:DOWN, calls:calls.slice(), mini:!!MINI}));
})();""")
    assert "at=2026-11-15+11%3A40%3A00" in out["first"] and "at=2026-11-15+11%3A40%3A00" in out["second"]
    assert "outline=1" in out["first"] and "outline" not in out["second"]
    assert out["q"] == {"fault_id": 355} and out["d"]["enbs"] == ["1"] and out["mini"] is True
    assert out["calls"] == ["mini", "down", "mini", "down"]


@pytest.mark.skipif(NODE is None, reason="node 없음")
def test_poll_ignores_down_when_the_server_gave_no_quake_and_survives_a_failed_fetch(tmp_path):
    out = _node(tmp_path, POLL + _fn("pollQuake", is_async=True) + """
(async()=>{
  FOCUS='I1'; H.I1={opened_ts:'2026-11-15 09:10:00'};
  REPLY={quake:null, down:{enbs:['9'],cells:[{key:'9-0',r_m:300}]}};        // DNS 장애 — 지진 없음
  await pollQuake(); const r1={q:MINI_Q, d:DOWN};
  REPLY='boom'; calls.length=0; MINI_Q={keep:1}; DOWN={keep:1};
  await pollQuake();                                                         // 실패하면 그리지도 지우지도 않는다
  console.log(JSON.stringify({r1, after:{q:MINI_Q, d:DOWN, calls:calls.slice()}}));
})();""")
    assert out["r1"] == {"q": None, "d": None}                       # 지진 없으면 down 이 와도 쓰지 않는다
    assert out["after"] == {"q": {"keep": 1}, "d": {"keep": 1}, "calls": []}


DRAW_DOWN = """
const mkEl=tag=>({tag,attrs:{},setAttribute(k,v){this.attrs[k]=v}});
const el=mkEl, mx=x=>x, my=y=>y;
let D=null, DOWN=null, DOWN_KEY=null, DOWN_N=0, LEG=0;
const T=(s,o)=>o?s.replace(/\\{(\\w+)\\}/g,(_,k)=>o[k]):s;
const g={children:[],replaceCount:0,replaceChildren(){this.children=[];this.replaceCount++},appendChild(c){this.children.push(c)}};
const legendBox={children:[],style:{display:''},rows:[],querySelector(){return {remove:()=>{this.children=this.children.filter(c=>c.className!=='lg-down')}}},
  appendChild(c){this.children.push(c)}};
const document={getElementById:id=>id==='gDown'?g:id==='legend'?legendBox:null,createElement:t=>({tag:t,className:'',innerHTML:''})};
"""


@pytest.mark.skipif(NODE is None, reason="node 없음")
def test_draw_down_paints_only_cells_with_position_and_radius_and_the_legend_counts_them(tmp_path):
    out = _node(tmp_path, DRAW_DOWN + _fn("drawDown", "function legendDown") + _fn("legendDown") + """
D={cells:[{key:'1-0',lat:35.7,lon:139.8},{key:'1-1',lat:35.71,lon:139.81},{key:'2-0',lat:35.72,lon:139.82}]};
DOWN={enbs:['1','2'],cells:[{key:'1-0',r_m:353},{key:'1-1',r_m:null},{key:'2-0',r_m:441},{key:'9-9',r_m:300}]};
drawDown();
const e=g.children[0];
console.log(JSON.stringify({n:g.children.length, rx:+e.attrs.rx, ry:+e.attrs.ry, cx:e.attrs.cx,
  legend:legendBox.children.map(c=>c.innerHTML), display:legendBox.style.display, N:DOWN_N}));""")
    assert out["n"] == 2 and out["N"] == 2                          # r_m null 한 셀 · 지도에 없는 셀 하나는 그리지 않는다
    assert out["rx"] > 0 and out["ry"] > 0 and out["cx"] == 139.8
    assert len(out["legend"]) == 1 and "정지 기지국 영역" in out["legend"][0] and "eNB 2 · 셀 2" in out["legend"][0]
    assert out["display"] == ""                                      # 범례가 보인다


@pytest.mark.skipif(NODE is None, reason="node 없음")
def test_draw_down_draws_nothing_and_no_legend_row_when_down_is_null_or_empty(tmp_path):
    out = _node(tmp_path, DRAW_DOWN + _fn("drawDown", "function legendDown") + _fn("legendDown") + """
D={cells:[{key:'1-0',lat:35.7,lon:139.8}]};
DOWN=null; drawDown(); const a={n:g.children.length, rows:legendBox.children.length, display:legendBox.style.display};
DOWN={enbs:[],cells:[]}; DOWN_KEY=null; drawDown(); const b={n:g.children.length, rows:legendBox.children.length};
console.log(JSON.stringify({a,b}));""")
    assert out["a"] == {"n": 0, "rows": 0, "display": "none"}       # 비어 있고 다른 범례도 없으면 범례 상자 자체를 숨긴다
    assert out["b"] == {"n": 0, "rows": 0}                          # F-348 — 정지 0곳이면 아무것도 그리지 않는다


@pytest.mark.skipif(NODE is None, reason="node 없음")
def test_draw_down_does_not_repaint_the_same_data_and_clears_when_deselected(tmp_path):
    out = _node(tmp_path, DRAW_DOWN + _fn("drawDown", "function legendDown") + _fn("legendDown") + """
D={cells:[{key:'1-0',lat:35.7,lon:139.8}]};
DOWN={enbs:['1'],cells:[{key:'1-0',r_m:353}]};
drawDown(); drawDown(); const same={replace:g.replaceCount, n:g.children.length};
DOWN=null; drawDown(); const cleared={n:g.children.length, rows:legendBox.children.length};
console.log(JSON.stringify({same,cleared}));""")
    assert out["same"] == {"replace": 1, "n": 1}                    # 10초마다 같은 값이 와도 다시 칠하지 않는다
    assert out["cleared"] == {"n": 0, "rows": 0}


@pytest.mark.skipif(NODE is None, reason="node 없음")
def test_draw_down_waits_for_the_map_cells(tmp_path):
    out = _node(tmp_path, DRAW_DOWN + _fn("drawDown", "function legendDown") + _fn("legendDown") + """
DOWN={enbs:['1'],cells:[{key:'1-0',r_m:353}]};
drawDown(); const early=g.children.length;                     // 지도 셀(D)이 아직 없다
D={cells:[{key:'1-0',lat:35.7,lon:139.8}]}; drawDown();
console.log(JSON.stringify({early, late:g.children.length}));""")
    assert out == {"early": 0, "late": 1}


# ── 소스 검사 — 층 · 범례 · 물결 ───────────────────────────────────────────────────────────

def test_down_layer_sits_between_the_kde_and_the_geo_layers_and_is_not_cleared_by_draw():
    assert "<g id=gKde></g><g id=gDown></g><g id=gGeo></g>" in MAP_HTML       # 장애 윤곽·셀 점보다 아래에 깔린다
    draw = MAP_HTML[MAP_HTML.index("function draw()"):][:3500]
    assert "drawDown();" in draw and "gDown" not in draw                      # draw 가 부르되 비우지는 않는다(스스로 관리)


def test_legend_keeps_the_down_row_in_both_branches():
    leg = MAP_HTML[MAP_HTML.index("function legend("):MAP_HTML.index("// ── 야마노테선 집기")]
    assert "if(axis==='none'){ L.innerHTML=''; L.style.display='none'; legendDown(); return; }" in leg
    assert leg.rstrip().rstrip("}").rstrip().endswith("legendDown();")        # 축이 있을 때도 끝에서 붙인다


def test_down_shading_is_one_dark_layer_distinct_from_the_orange_surge():
    css = MAP_HTML[MAP_HTML.index("#gDown{"):][:400]
    assert "opacity:.5" in css and "pointer-events:none" in css and "fill:#05070a" in css
    assert "body[data-theme=light] #gDown{opacity:.35}" in MAP_HTML
    assert "stroke:none" in css.split("#gDown ellipse")[1][:60]                # 원마다 투명도 없이 층에만 — 겹쳐도 안 진해진다
    assert "ff" not in css.lower().split("fill:")[1][:8]                       # 주황(폭주)과 다른 색


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


def test_down_legend_and_wave_phrases_are_translated():
    for k in ("정지 기지국 영역", "eNB {e} · 셀 {c}"):
        assert k in _JA_MAP and _JA_MAP[k].strip() and k in MAP_HTML, k
    assert "停止基地局エリア" in _JA_MAP["정지 기지국 영역"] and "{e}" in _JA_MAP["eNB {e} · 셀 {c}"] and "{c}" in _JA_MAP["eNB {e} · 셀 {c}"]
