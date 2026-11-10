"""시나리오 화면 `EVT-…` → 「L1 장애」 링크 조회 — `ui.api_evt_lookup` · `ui._fault_at` (2026-11-10 · 동구 수정 고정).

* 단위·대표 조각·`event_id` 를 graphio `/api/events/{id}` 로 받는다. 합쳐진 사건(MERGED)은 `merged_into` 를 따라가되 3번까지.
* 받지 못한 ID(404·연결 실패·단위 없음)와 꼴이 틀린 ID 는 응답에서 빠진다 — 화면은 ID 를 그대로 둔다(불변규칙 18).
* 40개 상한 · TTL 안 재사용 · 성공만 캐시 · `graphio_api` 빈 값이면 `error`.
* `_fault_at` — 시각 형식·DB 오류는 None. 장애 번호만 모를 뿐 링크는 나간다.
DB·네트워크 없이 — 가짜 `urlopen`·`_connect`.
"""
from __future__ import annotations

import json
import urllib.error
from datetime import datetime

import pytest

from datagen import ui

A, B, C, D, E = (f"EVT-{c * 10}" for c in "abcde")


class _Resp:
    def __init__(self, doc):
        self._b = json.dumps(doc).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _ev(eid, unit="L1_BUNDLE", rep="IX1", status="OPEN", merged=None, opened="2026-11-08 08:04:00"):
    return {"event_id": eid, "unit": unit, "rep_incident_id": rep, "status": status, "merged_into": merged,
            "opened_time": opened}


@pytest.fixture
def net(monkeypatch):
    """`docs` 의 ID → 응답. 값이 예외면 던진다. 부른 ID 를 `calls` 에 쌓는다."""
    calls: list[str] = []
    docs: dict = {}

    def fake(url, timeout=0):
        eid = url.rsplit("/", 1)[1]
        calls.append(eid)
        v = docs.get(eid)
        if v is None:
            raise urllib.error.HTTPError(url, 404, "nf", None, None)
        if isinstance(v, Exception):
            raise v
        return _Resp(v)

    monkeypatch.setattr("urllib.request.urlopen", fake)
    monkeypatch.setattr(ui, "_pem_cfg", lambda: {"graphio_api": "http://g:8008"})
    monkeypatch.setattr(ui, "_fault_at", lambda opened: 203 if opened else None)
    ui._EVT_CACHE.clear()
    yield type("N", (), {"calls": calls, "docs": docs})
    ui._EVT_CACHE.clear()


def test_unit_rep_and_fault_are_returned(net):
    net.docs[A] = _ev(A, "L1_BUNDLE", "IX9")
    net.docs[B] = _ev(B, "L2_EVENT", "IX8")
    out = ui.api_evt_lookup(f"{A},{B}")["events"]
    assert out[A]["unit"] == "L1_BUNDLE" and out[A]["rep"] == "IX9" and out[A]["fault"] == 203
    assert out[A]["event_id"] == A and out[A]["merged_into"] is None
    assert out[B]["unit"] == "L2_EVENT"


def test_merged_chain_is_followed_to_the_living_event(net):
    net.docs[A] = _ev(A, status="MERGED", merged=B)
    net.docs[B] = _ev(B, "L2_EVENT", "IX2")
    r = ui.api_evt_lookup(A)["events"][A]
    assert r["unit"] == "L2_EVENT" and r["rep"] == "IX2" and r["merged_into"] == B and r["event_id"] == B


def test_merged_chain_stops_after_three_hops(net):
    ids = [A, B, C, D, E]
    for x, nxt in zip(ids, ids[1:]):
        net.docs[x] = _ev(x, status="MERGED", merged=nxt)
    net.docs[E] = _ev(E, "L2_EVENT", "IXE")
    ui.api_evt_lookup(A)
    assert net.calls == [A, B, C, D]            # E 까지는 가지 않는다 — 4번째 호출에서 멈춘다


def test_missing_failed_or_unitless_ids_are_dropped(net):
    net.docs[A] = _ev(A)
    net.docs[B] = urllib.error.URLError("down")
    net.docs[C] = {"event_id": C, "unit": None, "rep_incident_id": "IX1"}
    net.docs[D] = {"event_id": D, "unit": "L1_BUNDLE", "rep_incident_id": None}
    out = ui.api_evt_lookup(f"{A},{B},{C},{D},{E}")["events"]       # E 는 404
    assert list(out) == [A]


def test_merged_target_missing_drops_the_id(net):
    net.docs[A] = _ev(A, status="MERGED", merged=B)                  # B 는 404
    assert ui.api_evt_lookup(A)["events"] == {}


@pytest.mark.parametrize("bad", ["EVT-xyz", "EVT-AAAAAAAAAA", "EVT-aaaaaaaaa", "EVT-aaaaaaaaaaa", "IX1234", "", " "])
def test_malformed_ids_are_ignored_without_calling_graphio(net, bad):
    assert ui.api_evt_lookup(bad)["events"] == {}
    assert net.calls == []


def test_only_first_forty_ids_are_used_and_duplicates_collapse(net):
    ids = [f"EVT-{i:010x}" for i in range(45)]
    for i in ids:
        net.docs[i] = _ev(i)
    out = ui.api_evt_lookup(",".join(ids + ids[:3]))["events"]
    assert len(out) == 40 and set(out) == set(ids[:40]) and len(net.calls) == 40


def test_empty_graphio_api_reports_error(monkeypatch):
    monkeypatch.setattr(ui, "_pem_cfg", lambda: {})
    r = ui.api_evt_lookup(A)
    assert r["events"] == {} and "graphio_api" in r["error"]


def test_ttl_reuse_and_expiry(net, monkeypatch):
    net.docs[A] = _ev(A)
    ui.api_evt_lookup(A)
    ui.api_evt_lookup(A)
    assert net.calls == [A]                                          # 두 번째는 캐시
    ts, val = ui._EVT_CACHE[A]
    ui._EVT_CACHE[A] = (ts - ui.EVT_LOOKUP_TTL_SEC - 1, val)
    ui.api_evt_lookup(A)
    assert net.calls == [A, A]                                       # 만료되면 다시 묻는다


def test_failures_are_not_cached(net):
    assert ui.api_evt_lookup(A)["events"] == {}                      # 404
    net.docs[A] = _ev(A)
    assert A in ui.api_evt_lookup(A)["events"]                       # 다음엔 받는다


# ── _fault_at ────────────────────────────────────────────────────────────────

class _Cur:
    def __init__(self, row, boom=False):
        self.row, self.boom, self.args = row, boom, None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, args=None):
        if self.boom:
            raise RuntimeError("db")
        self.args = args

    def fetchone(self):
        return self.row


class _Conn:
    def __init__(self, cur):
        self.cur, self.closed = cur, False

    def cursor(self):
        return self.cur

    def close(self):
        self.closed = True


def test_fault_at_returns_the_fault_number_and_closes(monkeypatch):
    conn = _Conn(_Cur((203,)))
    monkeypatch.setattr(ui, "_connect", lambda: conn)
    assert ui._fault_at("2026-11-08T08:04:00") == 203
    assert conn.cur.args[0] == datetime(2026, 11, 8, 8, 4) and conn.closed


def test_fault_at_none_when_no_row(monkeypatch):
    monkeypatch.setattr(ui, "_connect", lambda: _Conn(_Cur(None)))
    assert ui._fault_at("2026-11-08 03:00:00") is None


@pytest.mark.parametrize("bad", [None, "", "not a time", "2026-13-45"])
def test_fault_at_none_on_bad_time(monkeypatch, bad):
    def boom():
        raise AssertionError("DB 를 열면 안 된다")
    monkeypatch.setattr(ui, "_connect", boom)
    assert ui._fault_at(bad) is None


def test_fault_at_none_on_db_errors(monkeypatch):
    def refuse():
        raise OSError("no db")
    monkeypatch.setattr(ui, "_connect", refuse)
    assert ui._fault_at("2026-11-08 08:04:00") is None
    conn = _Conn(_Cur(None, boom=True))
    monkeypatch.setattr(ui, "_connect", lambda: conn)
    assert ui._fault_at("2026-11-08 08:04:00") is None and conn.closed
