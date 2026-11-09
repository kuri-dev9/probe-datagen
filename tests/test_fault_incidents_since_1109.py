"""장애 팝업 탐지 이력 — graphio `/api/incidents` 에 `since`·`until` (2026-11-09 · 맥스 수정 고정).

gen-ui `api_fault_incidents` 가 7일치 사건 512건(2.2MB)을 받아 스스로 거르던 것을 그 장애 시간대만 받게 했다.
여기서는 gen-ui 쪽(URL 인자 · 옛 graphio 가 인자를 무시해도 같은 결과)과 graphio 의 겹침 판정 `_overlap` 을 고정한다.
엔드포인트 본체(SQL)는 운영 DB 로 전후 비교했고(맥스) 이 테스트는 DB 없이 돈다.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from datagen import ui

GRAPHIO = Path(__file__).resolve().parents[2] / "graphio-probe-rca"
START = datetime(2026, 11, 9, 18, 35, 22)


def _inc(iid, opened, closed=None, updated=None):
    return {"incident_id": iid, "title": "t", "severity": "Critical", "opened_ts": opened, "closed_ts": closed,
            "updated_ts": updated, "root_code": "X", "trigger_kpi": "C-02", "event_unit": "L2_EVENT"}


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
def api(monkeypatch):
    """`api_fault_incidents` — DB·graphio 를 가짜로. 보낸 URL 을 모은다."""
    seen = []

    class _Cur:
        description = [("fault_id",), ("kind",), ("target",), ("start_ts",), ("duration_min",)]

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params):
            pass

        def fetchone(self):
            return (227, "gw_resource", None, START, 11)

    class _Conn:
        def cursor(self):
            return _Cur()

        def close(self):
            pass

    monkeypatch.setattr(ui, "_connect", lambda: _Conn())
    monkeypatch.setattr(ui, "_pem_cfg", lambda: {"graphio_api": "http://g.invalid"})
    monkeypatch.setattr(ui, "_truth_of", lambda fid: {})
    monkeypatch.setattr(ui, "_match_score", lambda i, spec, truth, target: 1)

    def run(doc):
        def fake_open(url, timeout=10):
            seen.append(url)
            return _Resp(doc)
        monkeypatch.setattr("urllib.request.urlopen", fake_open)
        return ui.api_fault_incidents(227)
    return run, seen


def test_url_carries_since_and_until_with_the_fault_window(api):
    run, seen = api
    run({"incidents": []})
    q = parse_qs(urlparse(seen[0]).query)
    assert urlparse(seen[0]).path == "/api/incidents"
    assert q["scope"] == ["1"] and q["recent"] == ["1"]
    assert q["since"] == ["2026-11-09T18:35:22"]
    # 끝 = 시작 + 지속 11분 + 확정 대기
    from datagen.scenarios import SETTLE_MIN
    assert q["until"] == [(START + timedelta(minutes=11 + SETTLE_MIN)).isoformat(timespec="seconds")]


def test_url_value_is_urlencoded_and_has_no_raw_colons_in_the_query(api):
    run, seen = api
    run({"incidents": []})
    assert "18%3A35%3A22" in seen[0]


def test_an_old_graphio_that_ignores_the_args_gives_the_same_result(api):
    """옛 graphio 는 모르는 인자를 무시하고 전부 준다 — gen-ui 가 같은 규칙으로 다시 거른다."""
    run, _ = api
    inside = _inc("IN", "2026-11-09T18:36:00", closed="2026-11-09T18:50:00")
    before = _inc("OLD", "2026-11-09T10:00:00", closed="2026-11-09T10:20:00")
    after = _inc("NEW", "2026-11-09T22:00:00")
    only_inside = run({"incidents": [inside]})
    everything = run({"incidents": [before, inside, after]})
    assert [i["incident_id"] for i in only_inside["incidents"]] == ["IN"]
    assert [i["incident_id"] for i in everything["incidents"]] == ["IN"]


@pytest.mark.parametrize("opened,closed,updated,want", [
    ("2026-11-09T18:20:00", "2026-11-09T18:35:22", None, True),     # 끝이 시작과 같다 — 포함
    ("2026-11-09T18:20:00", "2026-11-09T18:35:21", None, False),    # 한 초 앞
    ("2026-11-09T18:30:00", None, "2026-11-09T18:40:00", True),     # closed 가 없으면 updated
    ("2026-11-09T19:00:00", None, None, False),                     # 끝 밖에서 시작
    ("2026-11-09T18:00:00", "2026-11-09T19:30:00", None, True),     # 구간을 통째로 덮는다
])
def test_gen_ui_filter_edges(api, opened, closed, updated, want):
    run, _ = api
    got = run({"incidents": [_inc("X", opened, closed, updated)]})["incidents"]
    assert bool(got) is want


def test_unreadable_times_are_dropped_by_gen_ui_and_graphio_error_is_reported(api, monkeypatch):
    run, _ = api
    assert run({"incidents": [_inc("X", "???")]})["incidents"] == []
    import urllib.error

    def boom(url, timeout=10):
        raise urllib.error.URLError("down")
    monkeypatch.setattr("urllib.request.urlopen", boom)
    out = ui.api_fault_incidents(227)
    assert out["incidents"] == [] and "닿지 않는다" in out["error"]


# ── graphio `_overlap` ───────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def overlap():
    import subprocess
    import sys
    code = ("import json,sys;from datetime import datetime;"
            "from graphio.api.app import _overlap;"
            "cases=json.loads(sys.stdin.read());"
            "f=lambda v:datetime.fromisoformat(v) if v else None;"
            "print(json.dumps([_overlap(o,e,f(lo),f(hi)) for o,e,lo,hi in cases]))")
    venv = GRAPHIO / ".venv" / "bin" / "python"
    if not venv.exists():
        pytest.skip("graphio venv 가 없다")

    def call(cases):
        r = subprocess.run([str(venv), "-c", code], input=json.dumps(cases), capture_output=True, text=True, cwd=GRAPHIO)
        assert r.returncode == 0, r.stderr[-400:]
        return json.loads(r.stdout)
    return call


LO, HI = "2026-11-09T18:00:00", "2026-11-09T19:00:00"


def test_graphio_overlap_boundaries_and_open_ends(overlap):
    cases = [
        ["2026-11-09T17:00:00", "2026-11-09T18:00:00", LO, HI],    # 끝 = since → 포함
        ["2026-11-09T17:00:00", "2026-11-09T17:59:59", LO, HI],    # 한 초 앞 → 제외
        ["2026-11-09T19:00:00", "2026-11-09T19:30:00", LO, HI],    # 시작 = until → 포함
        ["2026-11-09T19:00:01", "2026-11-09T19:30:00", LO, HI],    # 시작이 until 뒤 → 제외
        ["2026-11-09T18:30:00", None, LO, HI],                     # 끝이 없으면 열린 시각으로 → 포함
        ["2026-11-09T17:00:00", None, LO, HI],                     # 열린 시각이 since 앞 → 제외
        ["2026-11-09T17:00:00", "2026-11-09T20:00:00", LO, HI],    # 통째로 덮음 → 포함
        ["2026-11-09T18:30:00", "2026-11-09T18:31:00", LO, None],  # until 없음
        ["2026-11-09T18:30:00", "2026-11-09T18:31:00", None, HI],  # since 없음
        ["2026-11-09T10:00:00", "2026-11-09T10:05:00", None, None],  # 둘 다 없음 → 항상 포함
    ]
    assert overlap(cases) == [True, False, True, False, True, False, True, True, True, True]


def test_graphio_overlap_does_not_drop_rows_with_unreadable_or_missing_open_time(overlap):
    cases = [["???", "2026-11-09T18:30:00", LO, HI], [None, None, LO, HI], ["", "x", LO, HI]]
    assert overlap(cases) == [True, True, True]          # 모르는 것을 빼면 "없음"으로 읽힌다


def test_graphio_overlap_reads_only_the_first_19_chars(overlap):
    assert overlap([["2026-11-09T18:30:00.123456", "2026-11-09T18:31:00+09:00", LO, HI]]) == [True]
