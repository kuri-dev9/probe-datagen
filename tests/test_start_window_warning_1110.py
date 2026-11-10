"""주입 화면 시작 시간대 경고 `renderWindow` · `_fault_at` 분 단위 비교 (2026-11-10 · 동구 수정 고정).

* `renderWindow(h)` — `recommended_hours` 의 `from`·`to` 밖일 때만 경고, 가상 시각 오프셋이 0 이 아니면 「+N분 적용 중」.
  읽을 수 없으면(`n` 없음 · `all` · `from`/`to` 없음) · 곡선 꺼짐 · 종류 미선택이면 말하지 않는다(지어내지 않는다).
* 페이지 스크립트 전체를 돌리지 않고 `renderWindow` 소스만 떼어 가짜 `$`·`vNow`·`VO`·`T` 로 돌린다(node).
* `_fault_at` — 열림 시각의 초를 버리고 분 단위로 비교한다. SQL 의 시작·끝 분 단위 비교는 운영 DB 로 동구가 확인했다
  (3번 02:36:00 → F-235). 여기서는 DB 에 넘기는 인자를 고정한다.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from datetime import datetime

import pytest

from datagen import ui

NODE = shutil.which("node")


def _fn_src() -> str:
    html = ui._HTML
    m = re.search(r"function renderWindow\(h\) \{.*?\n\}\n", html, re.S)
    assert m, "renderWindow 를 찾지 못했다"
    return m.group(0)


def _run(h, *, hour, minute=0, vo=0, on=True, kind="23", lang="ko"):
    """`renderWindow(h)` 를 돌려 `{hidden, html}` 을 받는다."""
    if not NODE:
        pytest.skip("node 가 없다")
    ja = {k: v for k, v in {**ui._JA_MAP, **ui._JA_MAIN}.items() if isinstance(v, str)}
    js = f"""
const JA={json.dumps(ja, ensure_ascii=False)}; const LANG={json.dumps(lang)};
function T(ko,a){{let s=(LANG==='ja'&&JA[ko]!=null)?JA[ko]:ko;return a?s.replace(/\\{{(\\w+)\\}}/g,(m,k)=>(k in a?a[k]:m)):s;}}
const esc=s=>String(s); let VO={vo};
const _d=new Date(2026,10,10,{hour},{minute}); const vNow=()=>_d;
const el={{hidden:true,innerHTML:''}};
const $=id=>id==='dur-rush'?el:id==='diurnal'?{{checked:{json.dumps(on)}}}:{{value:{json.dumps(kind)}}};
{_fn_src()}
renderWindow({json.dumps(h, ensure_ascii=False)});
console.log(JSON.stringify({{hidden:el.hidden,html:el.innerHTML}}));
"""
    r = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr[-600:]
    return json.loads(r.stdout)


H = {"n": 4, "from": 8, "to": 22, "text": "호가 많은 08~22시", "text_ja": "通話の多い08~22時"}
WARN = "시작 시간대(호가 많은 08~22시) 밖"


@pytest.mark.parametrize("hour,warn", [(7, True), (8, False), (21, False), (22, True), (0, True), (16, False)])
def test_window_edges_08_22(hour, warn):
    r = _run(H, hour=hour)
    assert (WARN in r["html"]) is warn
    assert r["hidden"] is (not warn)               # 안이고 오프셋 0 이면 아무것도 안 보인다


@pytest.mark.parametrize("hour,warn", [(3, False), (4, True), (5, False), (23, False), (0, False)])
def test_window_crossing_midnight_warns_only_inside_the_gap(hour, warn):
    # from 5 · to 4 — 05시부터 다음날 04시 전까지가 안, 04시대만 밖(22번 「04~05시 제외」 모양)
    h = {"n": 3, "from": 5, "to": 4, "text": "04~05시 제외"}
    assert ("밖" in _run(h, hour=hour)["html"]) is warn


@pytest.mark.parametrize("h", [
    {"n": 0, "from": 8, "to": 22, "text": "x"},                 # 실측 없음
    {"n": 4, "all": True, "from": 8, "to": 22},                 # 제약 없음
    {"n": 4, "text": "x"},                                      # from·to 없음
    {"n": 4, "from": None, "to": None},
    None,
])
def test_unreadable_window_says_nothing(h):
    r = _run(h, hour=3)
    assert r["hidden"] is True and r["html"] == ""


def test_curve_off_or_no_kind_says_nothing():
    assert _run(H, hour=3, on=False)["hidden"] is True
    assert _run(H, hour=3, kind="")["hidden"] is True


@pytest.mark.parametrize("vo,want", [(18712, "+312분"), (-3600, "-60분"), (0, None)])
def test_offset_line(vo, want):
    r = _run(H, hour=12, vo=vo)                    # 12시는 시간대 안
    if want is None:
        assert r["hidden"] is True and "적용 중" not in r["html"]
    else:
        assert f"가상 시각 {want} 적용 중" in r["html"] and r["hidden"] is False


def test_offset_and_warning_together():
    r = _run(H, hour=3, vo=18712)
    assert WARN in r["html"] and "+312분 적용 중" in r["html"] and "<br>" in r["html"]


def test_offset_line_hidden_when_curve_off():
    # 곡선이 꺼져 있으면 가상 시각이 판정에 안 쓰인다 — 오프셋 줄도 말하지 않는다(동구 구현 그대로)
    assert _run(H, hour=3, vo=18712, on=False)["hidden"] is True


def test_ja_texts():
    r = _run(H, hour=3, vo=18712, lang="ja", minute=5)
    assert "現在の仮想時刻 03:05" in r["html"] and "通話の多い08~22時" in r["html"]
    assert "仮想時刻 +312分を適用中" in r["html"]


def test_ko_and_ja_keys_exist_in_dictionary():
    for k in ("지금 가상 시각 {t} — 시작 시간대({h}) 밖이라 탐지되지 않을 수 있다 · 주입은 된다",
              "가상 시각 {n}분 적용 중 — 끝나면 「지금으로」로 되돌린다"):
        assert ui._JA_MAIN.get(k)


def test_rush_scenarios_keep_their_own_guidance():
    """15번 출퇴근(`h.clock` 있음) 은 `renderRush` 가 그리고 `renderWindow` 로 내려가지 않는다."""
    html = ui._HTML
    m = re.search(r"function renderRush\(h\) \{.*?\n\}\n", html, re.S)
    assert m and "if (!clock.length) { renderWindow(h); return; }" in m.group(0)
    assert "출퇴근 시간대 권장" in m.group(0)


# ── _fault_at ────────────────────────────────────────────────────────────────

class _Cur:
    def __init__(self, row=(235,)):
        self.row, self.sql, self.args = row, None, None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, args=None):
        self.sql, self.args = sql, args

    def fetchone(self):
        return self.row


class _Conn:
    def __init__(self, cur):
        self.cur = cur

    def cursor(self):
        return self.cur

    def close(self):
        pass


@pytest.mark.parametrize("opened", ["2026-11-10T02:36:00", "2026-11-10 02:36:00", "2026-11-10T02:36:59"])
def test_fault_at_compares_by_minute(monkeypatch, opened):
    from datagen.scenarios import SETTLE_MIN
    conn = _Conn(_Cur())
    monkeypatch.setattr(ui, "_connect", lambda: conn)
    assert ui._fault_at(opened) == 235
    sql, args = conn.cur.sql, conn.cur.args
    assert args == [datetime(2026, 11, 10, 2, 36), SETTLE_MIN, datetime(2026, 11, 10, 2, 36)]     # 초는 버린다
    assert sql.count("DATE_FORMAT(START_TIME") == 2         # 시작·끝 모두 분으로 내려 비교한다
    assert "%%i:00" in sql


def test_fault_at_settle_minutes_are_passed(monkeypatch):
    from datagen.scenarios import SETTLE_MIN
    conn = _Conn(_Cur(None))
    monkeypatch.setattr(ui, "_connect", lambda: conn)
    assert ui._fault_at("2026-11-10 09:30:00") is None
    assert conn.cur.args[1] == SETTLE_MIN


@pytest.mark.skipif(not NODE, reason="node 가 없다")
def test_fault_at_real_db_if_available():
    """운영 DB 를 읽기만 한다(SELECT). 접속이 안 되면 건너뛴다."""
    try:
        ui._connect().close()
    except Exception:  # noqa: BLE001
        pytest.skip("DB 접속 불가")
    assert ui._fault_at("2026-11-10 09:30:00") is None or isinstance(ui._fault_at("2026-11-10 09:30:00"), int)
