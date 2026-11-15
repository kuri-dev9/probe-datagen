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


# ---------------------------------------------------------------- _mini_outline · _ring_km2

def test_ring_km2_square_at_35_degrees():
    ring = [[139.0, 35.0], [139.1, 35.0], [139.1, 35.1], [139.0, 35.1], [139.0, 35.0]]
    assert map_api._ring_km2(ring) == pytest.approx(100, rel=0.1)     # 0.1°×0.1° ≈ 9.1×11.1km
    assert map_api._ring_km2(ring[:3]) == 0.0                         # 고리가 못 되면 0


def test_outline_missing_or_empty_path_is_error_not_exception(monkeypatch):
    monkeypatch.setattr(map_api, "_pem_cfg", lambda: {"geo_prefectures": ""})
    assert map_api._mini_outline()["rings"] == [] and map_api._mini_outline()["error"]
    monkeypatch.setattr(map_api, "_pem_cfg", lambda: {"geo_prefectures": "/nonexistent/x.json"})
    out = map_api._mini_outline()
    assert out["rings"] == [] and "없다" in out["error"]


@pytest.fixture
def real_outline(monkeypatch):
    if not PREF.exists():
        pytest.skip("graphio 도도부현 경계 파일이 없다")
    monkeypatch.setattr(map_api, "_pem_cfg", lambda: {"geo_prefectures": str(PREF)})
    return map_api._mini_outline()


def test_outline_is_small_and_bounded(real_outline):
    import json
    o = real_outline
    assert len(json.dumps(o, ensure_ascii=False)) <= 50_000
    la0, lo0, la1, lo1 = o["bbox"]
    assert lo1 < 150 and la0 > 24                       # 오가사와라·오키노토리시마 제외
    assert 45 < la1 < 46 and lo0 > 122                  # 홋카이도 북단 · 서쪽 끝


def test_outline_keeps_okinawa_main_island(real_outline):
    assert any(26.0 < y < 26.9 and 127.5 < x < 128.4 for r in real_outline["rings"] for x, y in r)


def test_outline_drops_small_islands(real_outline):
    for r in real_outline["rings"]:
        assert map_api._ring_km2(r) > map_api._MINI_MIN_KM2 * 0.5      # 단순화로 줄어도 거의 문턱 이상


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


def test_mini_draws_star_and_magnitude_only():
    fn = MAP_HTML[MAP_HTML.index("function drawMini"):MAP_HTML.index("function miniView")]
    assert "class=ms" in fn and "class=mm" in fn and "class=ml" in fn and "miniView" in fn
    assert fn.count("<text") == 1                       # 글자는 규모 하나 — 지명은 넣지 않는다
    assert "magnitude.toFixed(1)" in fn
    assert "hypo" not in fn and "name" not in fn.replace("className", "")


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

NEW = ("진앙 — 일본 전도 (움직이지 않는다)", "진앙 {m} — 일본 전도 (움직이지 않는다) · 네모 = 지금 보는 범위", "규모 모름")


def test_new_phrases_translated_and_present_in_source():
    for k in NEW:
        assert k in _JA_MAP and _JA_MAP[k].strip()
        assert k in MAP_HTML, k                         # 소스 원문과 사전 키가 한 글자도 어긋나지 않는다
    assert "{m}" in _JA_MAP[NEW[1]]                     # 자리표시자 유지
    assert re.search(r"[぀-ヿ一-鿿]", _JA_MAP[NEW[0]])
    for k in NEW:
        assert not re.search(r"[぀-ヿ]", k)     # 한국어 쪽에는 가나가 없다
