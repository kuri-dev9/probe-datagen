"""gen 의 「과거 데이터 요청」(`cli._history_request` · 2026-11-21) — 계약대로 한 번 보내고, 실패해도 주입을 멈추지 않는다.

계약: `POST {graphio_api}/api/history/copy` 본문 `{"fault_id", "cells", "start", "end", "offsets"}`. gen 은 XDR 만 만든다 —
과거 표에는 쓰지 않고 요청만 보낸다. **DB 도 네트워크도 쓰지 않는다**(`urlopen` 을 가짜로 바꾼다).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime
from types import SimpleNamespace

import pytest

from datagen import cli
from datagen import scenarios as S

ANCHOR = datetime(2026, 11, 21, 17, 3)
HIST = {"default_offsets": [1], "repeat_offsets": [1, 2, 3, 4, 5, 6, 7], "repeat_after_min": 3, "max_tries": 3}
PEM = {"graphio_api": "http://web:8008", "history": HIST}


def _sc(fid="F-491", off=(1, 7), cells=("132154-14", "132154-10", "132154-23"), start=3, dur=25, scored=True,
        kind="event_crowd", **target):
    return SimpleNamespace(id=fid, kind=kind, history_offsets=off, start_minute=start, duration=dur, scored=scored,
                           target=S.Target(cell_keys=set(cells), **target))


def _gen(anchor=ANCHOR):
    return SimpleNamespace(anchor=anchor)


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

    def fake(req, timeout=None):
        calls.append({"url": req.full_url, "method": req.get_method(), "body": json.loads(req.data.decode("utf-8")),
                      "headers": dict(req.header_items()), "timeout": timeout})
        return _Res()

    monkeypatch.setattr(urllib.request, "urlopen", fake)
    return calls


def test_the_request_follows_the_contract(sent):
    cli._history_request(PEM, _gen(), [_sc()])
    assert len(sent) == 1
    c = sent[0]
    assert c["url"] == "http://web:8008/api/history/copy" and c["method"] == "POST"
    assert c["body"] == {"fault_id": 491, "cells": ["132154-10", "132154-14", "132154-23"],
                         "start": "2026-11-21 17:06:00", "end": "2026-11-21 17:31:00", "offsets": [1, 7]}
    assert c["headers"].get("Content-type") == "application/json" and c["timeout"] == 10


def test_a_trailing_slash_in_the_graphio_address_is_tolerated(sent):
    cli._history_request({**PEM, "graphio_api": "http://web:8008/"}, _gen(), [_sc()])
    assert sent[0]["url"] == "http://web:8008/api/history/copy"


def test_a_successful_fault_is_sent_only_once(sent):
    g = _gen(); sc = _sc()
    cli._history_request(PEM, g, [sc]); cli._history_request(PEM, g, [sc]); cli._history_request(PEM, g, [sc])
    assert len(sent) == 1 and 491 in g._history_done


def test_a_failed_request_raises_for_aside_and_is_retried_next_tick(monkeypatch):
    g, sc, n = _gen(), _sc(), {"calls": 0}

    def boom(req, timeout=None):
        n["calls"] += 1
        raise urllib.error.HTTPError(req.full_url, 500, "boom", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    with pytest.raises(RuntimeError, match="F-491"):
        cli._history_request(PEM, g, [sc])
    assert 491 not in g.__dict__.get("_history_done", set())
    with pytest.raises(RuntimeError, match="F-491"):
        cli._history_request(PEM, g, [sc])
    assert n["calls"] == 2                                                       # 실패는 완료로 세지 않는다


def test_aside_swallows_the_failure_so_the_injection_goes_on(monkeypatch, capsys):
    def boom(req, timeout=None):
        raise urllib.error.URLError("refused")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    cli._ASIDE_FAILED.clear()
    cli._aside("과거 데이터 요청", cli._history_request, PEM, _gen(), [_sc()])      # 예외가 새어 나오면 이 시험이 죽는다


@pytest.mark.parametrize("sc", [
    _sc(cells=()),                                # 장비·전국 대상 — 셀 목록이 없다
    _sc(fid="T-3"),                               # 채점 장애(F-)가 아니다
    _sc(fid="F-x"),
    _sc(scored=False),
])
def test_faults_without_a_reason_to_ask_are_not_sent(sent, sc):
    cli._history_request(PEM, _gen(), [sc])
    assert sent == []


@pytest.mark.parametrize("target", [{"sgw_id": "7"}, {"mme_id": "6"}, {"pgw_id": "3"}, {"mme_group": "G1"}])
def test_equipment_targets_with_cells_are_sent_now(sent, target):
    """2026-11-23 사용자 「장비가 평가 대상이면 그 장비 아래 셀의 과거 정상」 — 전에는 장비 대상을 뺐다(5번 F-553 「사건 셀의 과거
    같은 시각 복사본 없음」). 셀 목록이 있는 장비 대상은 이제 보낸다. 셀 목록이 없으면 그대로 보내지 않는다(위 시험)"""
    cli._history_request(PEM, _gen(), [_sc(**target)])
    assert len(sent) == 1, target


def test_no_anchor_means_nothing_is_sent_yet(sent):
    cli._history_request(PEM, _gen(anchor=None), [_sc()])
    assert sent == []


def test_an_empty_graphio_address_is_an_error_not_a_silent_skip(sent):
    with pytest.raises(RuntimeError):
        cli._history_request({"history": HIST}, _gen(), [_sc()])
    assert sent == []


def test_each_fault_is_sent_separately(sent):
    cli._history_request(PEM, _gen(), [_sc("F-491"), _sc("F-492", off=(7,), start=10)])
    assert [c["body"]["fault_id"] for c in sent] == [491, 492]
    assert sent[1]["body"]["offsets"] == [7] and sent[1]["body"]["start"] == "2026-11-21 17:13:00"


# ───────────────────────────────────────────────────────── 시나리오 정의

def test_event_crowd_keeps_its_own_offsets_and_others_fall_back_to_the_config_default(sent):
    # 11-23 사용자 「전일이 필요하면 전일, 7일 전이 필요하면 7일 전 — 하나만」: 행사장도 전일 하루
    assert S.KIND_SPECS["event_crowd"]["history_offsets"] == (1,)
    cli._history_request(PEM, _gen(), [_sc("F-1", off=(), kind="ran_radio", start=0)])    # 시나리오 날짜 없음 → 설정 기본
    assert sent[0]["body"]["offsets"] == [1] and "mode" not in sent[0]["body"]
    sent.clear()
    cli._history_request({**PEM, "history": {**HIST, "default_offsets": [1, 3]}}, _gen(), [_sc("F-2", off=())])
    assert sent[0]["body"]["offsets"] == [1, 3]
    sent.clear()
    cli._history_request(PEM, _gen(), [_sc("F-3", off=(7,))])                               # 시나리오 날짜가 이긴다
    assert sent[0]["body"]["offsets"] == [7]


def test_scenario_default_is_no_history_request():
    assert S.Scenario.__dataclass_fields__["history_offsets"].default == ()


# ───────────────────────────────────────────────────────── 시험 중 실제 graphio 로 새지 않게

def test_the_suite_guard_blocks_real_graphio_port_8008():
    """tests/conftest.py — 가짜로 바꾸지 않은 `urlopen` 이 8008 로 가면 거부한다(진짜 과거 표에 복사되는 것을 막는다)."""
    with pytest.raises(RuntimeError, match="실제 graphio"):
        urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:8008/api/history/copy", data=b"{}", method="POST"))
    with pytest.raises(RuntimeError, match="실제 graphio"):
        urllib.request.urlopen("http://web:8008/api/x")


def test_local_yaml_points_gen_at_the_web_container_not_the_host():
    import yaml
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "config"
    assert yaml.safe_load((root / "local.yaml").read_text(encoding="utf-8"))["pemdb1"]["graphio_api"] == "http://web:8008"
    ex = (root / "prod.yaml.example").read_text(encoding="utf-8")
    assert "graphio_api: http://web:8008" in ex


# ───────────────────────────────────────────────────────── 24번 매일 부하 — 끝나고 3분 뒤 repeat 한 번

def _daily(fid="F-495", start=0, dur=120, scored=False):
    return _sc(fid, off=(), kind="capacity_shift", cells=("5-1", "5-2"), start=start, dur=dur, scored=scored, enb_id="5")


def _at(m):
    from datetime import timedelta
    return ANCHOR + timedelta(minutes=m)


def test_daily_load_is_not_sent_while_it_runs_even_when_unscored(sent):
    g = _gen()
    cli._history_request(PEM, g, [_daily()], now=_at(0))
    cli._history_request(PEM, g, [_daily()], now=_at(60))
    assert sent == []                                      # 오늘 행이 아직 쌓이는 중 — 끝난 뒤에 보낸다


def test_daily_load_goes_once_as_repeat_three_minutes_after_the_last_active_minute(sent):
    g = _gen()
    cli._history_request(PEM, g, [_daily()], now=_at(0))
    cli._history_request(PEM, g, [_daily()], now=_at(69))  # 취소로 69분째가 마지막 활성이었다
    for m in (70, 71, 72):                                 # 마지막 분 + 1 + 3 = 73분 전에는 아무것도 없다
        cli._history_request(PEM, g, [], now=_at(m))
        assert sent == []
    cli._history_request(PEM, g, [], now=_at(73))
    assert len(sent) == 1
    b = sent[0]["body"]
    assert b["mode"] == "repeat" and b["offsets"] == [1, 2, 3, 4, 5, 6, 7] and b["fault_id"] == 495
    assert b["cells"] == ["5-1", "5-2"]
    assert b["start"] == "2026-11-21 17:03:00" and b["end"] == "2026-11-21 18:13:00"   # 실제로 만든 구간(0~69분 + 1)
    cli._history_request(PEM, g, [], now=_at(80))
    cli._history_request(PEM, g, [_daily()], now=_at(90))
    assert len(sent) == 1                                  # 한 번만


def test_daily_load_that_ran_to_its_full_length_ends_at_the_scheduled_minute(sent):
    g = _gen()
    cli._history_request(PEM, g, [_daily(dur=30)], now=_at(10))
    cli._history_request(PEM, g, [_daily(dur=30)], now=_at(29))
    cli._history_request(PEM, g, [], now=_at(32))
    assert sent == []
    cli._history_request(PEM, g, [], now=_at(33))
    assert sent[0]["body"]["end"] == "2026-11-21 17:33:00"


def test_repeat_is_off_without_repeat_offsets(sent):
    g, pem = _gen(), {**PEM, "history": {**HIST, "repeat_offsets": []}}
    cli._history_request(pem, g, [_daily(dur=5)], now=_at(0))
    cli._history_request(pem, g, [], now=_at(60))
    assert sent == []


def test_a_load_with_a_shift_target_b_is_a_normal_capacity_fault_not_daily(sent):
    sc = _sc("F-7", off=(1, 7), kind="capacity_shift", scored=True, shift_to="9-1")
    cli._history_request(PEM, _gen(), [sc], now=_at(0))
    assert len(sent) == 1 and "mode" not in sent[0]["body"]


def test_repeat_stops_after_three_failures_and_the_other_faults_still_go(monkeypatch):
    g, calls = _gen(), []

    def boom(req, timeout=None):
        body = json.loads(req.data.decode("utf-8"))
        calls.append(body["fault_id"])
        if body["fault_id"] == 495:
            raise urllib.error.HTTPError(req.full_url, 500, "boom", {}, None)
        return _Res()

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    cli._history_request(PEM, g, [_daily(dur=5)], now=_at(0))
    for i in range(6):
        try:
            cli._history_request(PEM, g, [_sc("F-8", start=0)] if i == 0 else [], now=_at(10 + i))
        except RuntimeError:
            pass
    assert calls.count(495) == 3 and g._history_tries[495] == 3 and 495 not in g._history_done
    assert calls.count(8) == 1 and 8 in g._history_done


def test_normal_request_stops_after_max_tries_too(monkeypatch):
    g, n = _gen(), {"c": 0}

    def boom(req, timeout=None):
        n["c"] += 1
        raise urllib.error.URLError("refused")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    for _ in range(6):
        try:
            cli._history_request(PEM, g, [_sc()])
        except RuntimeError:
            pass
    assert n["c"] == 3


@pytest.mark.parametrize("exc", [TimeoutError("timed out"), urllib.error.URLError(TimeoutError("timed out"))])
def test_a_timeout_is_not_sent_again_and_counts_as_done(monkeypatch, capsys, exc):
    g, n = _gen(), {"c": 0}

    def slow(req, timeout=None):
        n["c"] += 1
        raise exc

    monkeypatch.setattr(urllib.request, "urlopen", slow)
    cli._history_request(PEM, g, [_sc()])
    cli._history_request(PEM, g, [_sc()])
    assert n["c"] == 1 and 491 in g._history_done and "응답 시간 초과" in capsys.readouterr().out


def test_a_repeat_timeout_is_not_sent_again_either(monkeypatch):
    g, n = _gen(), {"c": 0}

    def slow(req, timeout=None):
        n["c"] += 1
        raise TimeoutError("timed out")

    monkeypatch.setattr(urllib.request, "urlopen", slow)
    cli._history_request(PEM, g, [_daily(dur=5)], now=_at(0))
    cli._history_request(PEM, g, [], now=_at(20))
    cli._history_request(PEM, g, [], now=_at(30))
    assert n["c"] == 1 and 495 in g._history_done and 495 not in g._history_repeat_wait


def test_datagen_yaml_has_the_history_settings_the_request_reads():
    import yaml
    from pathlib import Path
    h = yaml.safe_load((Path(__file__).resolve().parents[1] / "config" / "datagen.yaml").read_text(encoding="utf-8"))["pemdb1"]["history"]
    assert h["default_offsets"] == [1] and h["repeat_offsets"] == [1]       # 11-23 하루만 복사
    assert h["repeat_after_min"] == 3 and h["max_tries"] == 3


def test_an_app_fault_sends_the_apps_only_and_no_cells(sent):
    """11-23 사용자 「앱 장애인데 왜 셀 단위로 복사를 해」 — 16번은 대상에 셀 464개가 있어도 앱만 보낸다"""
    cli._history_request(PEM, _gen(), [_sc("F-570", off=(), kind="cdn_degradation", cells=("1-1", "1-2"), app_id="3001")])
    body = sent[0]["body"]
    assert body["apps"] == ["3001"] and body["cells"] == [] and body["offsets"] == [1]


def test_a_cell_fault_still_sends_its_cells_and_no_apps_key(sent):
    cli._history_request(PEM, _gen(), [_sc("F-2", off=())])
    body = sent[0]["body"]
    assert body["cells"] and "apps" not in body
