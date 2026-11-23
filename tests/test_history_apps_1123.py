"""gen 의 앱 대상 과거 복사 요청(`cli._history_apps` · `_history_request` apps) — 2026-11-23 (동구 구현 · 에스터 시험).

사용자 「앱이 평가 대상이면 그 앱 사용자의 과거 정상을 복사」「시나리오 주입 때에만」. gen 은 요청만 보내고(복사 원본은 graphio 가 정한다)
앱만 대상이어도(셀 목록 없음) 요청한다. DB 도 네트워크도 쓰지 않는다(`urlopen` 을 가짜로).
"""
from __future__ import annotations

import json
import urllib.request
from datetime import datetime
from types import SimpleNamespace

import pytest

from datagen import cli
from datagen import scenarios as S

ANCHOR = datetime(2026, 11, 23, 11, 0)
PEM = {"graphio_api": "http://web:8008", "history": {"default_offsets": [1, 7], "max_tries": 3}}


def _app(idx, group):
    return SimpleNamespace(idx=idx, group=group)


def _gen(apps=(), nested=False):
    book = SimpleNamespace(apps=list(apps))
    g = SimpleNamespace(anchor=ANCHOR)
    if nested:
        g.gen = SimpleNamespace(app_book=book)          # 바깥 생성기 안쪽에 앱 사전이 있다(`PemdbGen.gen`)
    else:
        g.app_book = book
    return g


BOOK = [_app(2001, "PAY"), _app(2002, "PAY"), _app(3001, "VIDEO"), _app(2003, "PAY"), _app(4001, "GAME")]


# ── _history_apps ────────────────────────────────────────────────────────────

def test_single_app_target_is_that_app_only():
    t = S.Target(app_id="3001")
    assert cli._history_apps(_gen(BOOK), t) == ["3001"]


def test_single_app_wins_over_group():
    assert cli._history_apps(_gen(BOOK), S.Target(app_id="3001", app_group="PAY")) == ["3001"]


def test_group_target_lists_every_app_of_the_group_sorted():
    assert cli._history_apps(_gen(BOOK), S.Target(app_group="PAY")) == ["2001", "2002", "2003"]


def test_group_with_no_app_in_book_is_empty():
    assert cli._history_apps(_gen(BOOK), S.Target(app_group="NOPE")) == []


def test_group_target_reads_book_from_inner_generator():
    assert cli._history_apps(_gen(BOOK, nested=True), S.Target(app_group="PAY")) == ["2001", "2002", "2003"]


def test_group_target_without_any_book_is_empty_not_error():
    assert cli._history_apps(SimpleNamespace(anchor=ANCHOR), S.Target(app_group="PAY")) == []


@pytest.mark.parametrize("target", [S.Target(), S.Target(cell_keys={"1-1"}), S.Target(domain="x.jp"), S.Target(apn="a")])
def test_non_app_target_has_no_apps(target):
    assert cli._history_apps(_gen(BOOK), target) == []


def test_app_ids_are_strings():
    assert all(isinstance(a, str) for a in cli._history_apps(_gen(BOOK), S.Target(app_group="PAY")))


# ── 요청 ─────────────────────────────────────────────────────────────────────

class _Res:
    def read(self):
        return b"{}"

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def sent(monkeypatch):
    calls = []
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda req, timeout=None: calls.append(json.loads(req.data.decode("utf-8"))) or _Res())
    return calls


def _sc(fid, kind, start=5, dur=10, scored=True, **target):
    return SimpleNamespace(id=fid, kind=kind, history_offsets=(1, 7), start_minute=start, duration=dur, scored=scored,
                           target=S.Target(**target))


def test_app_only_target_is_requested_with_apps_and_empty_cells(sent):
    cli._history_request(PEM, _gen(BOOK), [_sc("F-600", "cdn_degradation", app_id="3001")])
    assert len(sent) == 1
    b = sent[0]
    assert b["fault_id"] == 600 and b["cells"] == [] and b["apps"] == ["3001"] and b["offsets"] == [1, 7]
    assert b["start"] == "2026-11-23 11:05:00" and b["end"] == "2026-11-23 11:15:00"


def test_app_group_target_sends_every_group_app(sent):
    cli._history_request(PEM, _gen(BOOK), [_sc("F-601", "pay_dns_delay", app_group="PAY")])
    assert sent[0]["apps"] == ["2001", "2002", "2003"] and sent[0]["cells"] == []


def test_cell_target_without_app_does_not_send_apps_key(sent):
    cli._history_request(PEM, _gen(BOOK), [_sc("F-602", "ran_degradation", cell_keys={"1-1", "1-2"})])
    assert "apps" not in sent[0] and sent[0]["cells"] == ["1-1", "1-2"]


def test_app_target_sends_apps_only_no_cells(sent):
    # 11-23 사용자 결정: 앱 장애는 앱 표만 복사한다 — 셀 키가 있어도 cells 는 비운다
    cli._history_request(PEM, _gen(BOOK), [_sc("F-603", "x", cell_keys={"1-1"}, app_id="3001")])
    assert sent[0]["cells"] == [] and sent[0]["apps"] == ["3001"]


def test_target_without_cells_or_apps_sends_nothing(sent):
    cli._history_request(PEM, _gen(BOOK), [_sc("F-604", "x", domain="x.jp")])
    assert sent == []


def test_unscored_app_target_is_not_requested(sent):
    # 앱 복사도 채점 문항 주입 때만 — 채점하지 않는 회차(매일 부하 등)는 보내지 않는다
    cli._history_request(PEM, _gen(BOOK), [_sc("F-605", "pay_dns_delay", scored=False, app_group="PAY")])
    assert sent == []


def test_group_with_no_listed_apps_and_no_cells_sends_nothing(sent):
    cli._history_request(PEM, _gen(BOOK), [_sc("F-606", "pay_dns_delay", app_group="NOPE")])
    assert sent == []


def test_app_request_is_sent_once(sent):
    g, sc = _gen(BOOK), _sc("F-607", "cdn_degradation", app_id="3001")
    cli._history_request(PEM, g, [sc])
    cli._history_request(PEM, g, [sc])
    assert len(sent) == 1
