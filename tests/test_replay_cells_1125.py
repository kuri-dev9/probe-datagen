"""재생 자료(`replay_api`) 11-25 오후 수정의 불변 규칙 — 맥스 구현 · 에스터 시험.

사용자 「야마노테선 장애는 전부 보여야 한다」 — 3번 11건 가운데 F-598(통합장애가 07:52 출퇴근 통화 집중 조각으로 먼저 열렸다)·
F-595(탐지가 놓침)·F-365 외(판정 기록 보관 7일 지남)가 비었다. 고친 뒤 고정하는 것:
  * `_members_in` — 사건이 창 앞에서 열렸어도 구간이 겹치고 **창 안에서 열린 조각**이 있으면 그 조각들만으로 본다.
    셀은 그 조각들 것(대표 조각 C-24 340셀은 칠하지 않는다) · 지표도 그 조각들 것 · 열린 시각은 첫 조각, 원래 시각은 따로
  * `_kpi_filter` — **일부만 인정한 셀**만 걸린다. 정답 셀·전체를 인정한 사건의 셀은 거르지 않는다(지금까지의 동작)
  * timelapse 판정·timeline 첫 이상징후가 같은 거르기를 쓴다
  * `expired`·`anomaly_earliest` — 판정 기록이 지워진 것과 놓친 것을 가른다 · 가장 이른 기록 조회는 캐시한다
  * `measured` — 노선 주입의 노선 셀 합산 지표 곡선: 분자·분모 합산 뒤 나눔 · 빈 분은 None(0 으로 채우지 않는다) ·
    기준은 graphio 설정에서 읽는다(새 임계값을 만들지 않는다)
DB·graphio 는 가짜로 바꿔 끼운다.
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timedelta

import pytest

from datagen.ui import replay_api as R
from test_replay_api_1124 import FAULT, T0, _anoms, _ev, _ln, _t, patch  # noqa: F401  (patch 는 픽스처)

LO = _t(-5)                                      # F-598 창 같은 꼴 — 시작 −5분
HI = T0 + timedelta(minutes=10)


def _iso(m):
    return _t(m).isoformat()


# ── 가짜 graphio — 사건 문서·조각 지도·조각 문서 ──────────────────────────────────────────

@pytest.fixture
def gio(monkeypatch):
    """07:52(창 앞)에 열린 통합장애 EVT-1 — 대표 IX1(C-24 · 셀 340), 창 안 조각 IX2(N-02·U-07) · IX3(U-07)."""
    calls: list[str] = []
    big = [{"key": f"9-{i}"} for i in range(340)]
    maps = {"IX1": big, "IX2": [{"key": "1-0"}, {"key": "2-0"}], "IX3": [{"key": "2-0"}, {"key": "3-0"}]}
    kpis = {"IX1": "C-24", "IX2": "N-02, U-07", "IX3": "U-07"}
    opened = {"IX1": _iso(-8), "IX2": _iso(3), "IX3": _iso(4)}

    def graphio(path, timeout=8):
        calls.append(path)
        if path.startswith("/api/events/"):
            return {"members": [{"incident_id": i, "opened": opened[i]} for i in ("IX1", "IX2", "IX3")]}, None
        m = re.match(r"/api/incidents/(IX\d)/map\?cells_only=true", path)
        if m:
            return {"cells": maps[m.group(1)]}, None
        m = re.match(r"/api/incidents/(IX\d)$", path)
        if m:
            return {"incident": {"trigger_kpi": kpis[m.group(1)]}}, None
        return None, "x"
    monkeypatch.setattr(R, "_graphio", graphio)
    calls_ref = calls
    return calls_ref


def _evt(opened=-8, updated=10, **kw):
    return {"unit": "L2_EVENT", "event_id": "EVT-1", "rep_incident_id": "IX1", "opened_time": _iso(opened),
            "updated_time": _iso(updated), "root_code": "RAN_HO_INTERRUPTION", **kw}


# ── `_members_in` ──────────────────────────────────────────────────────────────────────────

def test_members_in_takes_only_the_pieces_opened_inside_the_window_and_never_the_representative(gio):
    e = R._members_in(_evt(), LO, HI)
    assert e["_cells"] == {"1-0", "2-0", "3-0"}                              # IX2 ∪ IX3 — 대표 IX1 의 340셀은 없다
    assert not any(c.startswith("9-") for c in e["_cells"])
    assert e["_kpis"] == {"N-02", "U-07"} and e["kpis_in"] == ["N-02", "U-07"]  # 조각의 트리거 지표만(대표 C-24 가 아니다)
    assert e["members_in"] == ["IX2", "IX3"]
    assert not any("IX1" in c for c in gio)                                   # 대표 조각 문서·지도를 아예 묻지 않는다


def test_members_in_times_the_event_by_its_first_piece_and_keeps_the_original_open_time(gio):
    e = R._members_in(_evt(), LO, HI)
    assert e["opened_time"] == _iso(3) and e["event_opened_time"] == _iso(-8)   # 열림 = 첫 조각 · 원래 열린 시각은 따로
    assert e["unit"] == "L2_EVENT" and e["event_id"] == "EVT-1"                 # 나머지는 그대로 싣는다


@pytest.mark.parametrize("opened, updated", [
    (2, 10),      # 이미 창 안에서 열렸다 — 지금까지의 일반 경로(조각 규칙을 쓰지 않는다)
    (-8, -6),     # 구간이 창과 겹치지 않는다(창 앞에서 끝났다)
    (20, 30),     # 창 뒤에서 열렸다
    (-8, None),   # 갱신 시각을 모른다 — 겹친다고 지어내지 않는다
])
def test_members_in_is_none_unless_the_span_overlaps_and_the_event_opened_before_the_window(gio, opened, updated):
    e = _evt(opened, updated if updated is not None else 0)
    if updated is None:
        e.pop("updated_time")
    assert R._members_in(e, LO, HI) is None


def test_members_in_is_none_when_no_piece_opened_inside_the_window(gio, monkeypatch):
    base = R._graphio
    monkeypatch.setattr(R, "_graphio", lambda p, timeout=8: ({"members": [{"incident_id": "IX1", "opened": _iso(-8)}]}, None)
                        if p.startswith("/api/events/") else base(p, timeout))
    assert R._members_in(_evt(), LO, HI) is None                              # 지금처럼 시각으로 거른다(`why=time`)


def test_members_in_asks_at_most_event_cell_lookups_pieces(monkeypatch):
    n = R.EVENT_CELL_LOOKUPS
    ids = [f"IX{i}" for i in range(n + 4)]
    asked = []

    def graphio(path, timeout=8):
        asked.append(path)
        if path.startswith("/api/events/"):
            return {"members": [{"incident_id": i, "opened": _iso(2)} for i in ids]}, None
        return ({"cells": []} if "map?" in path else {"incident": {"trigger_kpi": "U-07"}}), None
    monkeypatch.setattr(R, "_graphio", graphio)
    R._members_in(_evt(), LO, HI)
    assert len([a for a in asked if "map?" in a]) == n and len([a for a in asked if re.fullmatch(r"/api/incidents/IX\d+", a)]) == n


def test_members_in_tolerates_graphio_not_answering(monkeypatch):
    monkeypatch.setattr(R, "_graphio", lambda p, timeout=8: (None, "down"))
    assert R._members_in(_evt(), LO, HI) is None                              # 조각을 모르면 걸러진 채로 둔다 — 지어내지 않는다


# ── `_own_events` — 일부만 인정한 사건 ─────────────────────────────────────────────────────

def test_own_events_accepts_an_event_that_opened_before_the_window_through_its_pieces(patch, gio, monkeypatch):
    monkeypatch.setattr(R, "_ev_cells", R._ev_cells.__wrapped__ if hasattr(R._ev_cells, "__wrapped__") else
                        (lambda e: set(e.get("_cells") or e.get("_c") or patch.cells)))
    monkeypatch.setattr(R, "_scope_hit", lambda f, truth, e: "kind_cell")
    own, other = R._own_events(dict(FAULT), [_evt()])
    assert [e["event_id"] for e in own] == ["EVT-1"] and own[0]["members_in"] == ["IX2", "IX3"]
    assert own[0]["event_opened_time"] == _iso(-8) and own[0]["_cells"] == {"1-0", "2-0", "3-0"}
    assert other == []


def test_own_events_keeps_an_event_without_in_window_pieces_as_time(patch, monkeypatch):
    monkeypatch.setattr(R, "_graphio", lambda p, timeout=8: ({"members": []}, None))
    own, other = R._own_events(dict(FAULT), [_evt()])
    assert own == [] and [(e["event_id"], e["why"]) for e in other] == [("EVT-1", "time")]


def test_a_fully_accepted_event_is_not_split_into_pieces(patch, gio):
    """창 안에서 열린 사건은 일반 경로 그대로다 — 조각 문서를 묻지도 않고 `_kpis` 도 없다(지표로 거르지 않는다)."""
    own, _ = R._own_events(dict(FAULT), [_ev("whole", "L2_EVENT", 2)])
    assert [e["event_id"] for e in own] == ["whole"] and "_kpis" not in own[0] and "members_in" not in own[0]
    assert gio == []


def test_event_rows_expose_the_new_columns_but_hide_the_internal_ones(patch, gio):
    row = R._ev_out(R._members_in(_evt(), LO, HI))
    assert {"event_opened_time", "kpis_in", "members_in"} <= set(row)
    assert not any(k.startswith("_") for k in row)                            # `_cells`·`_kpis` 는 응답에 나가지 않는다


def test_timeline_marks_the_original_open_time_on_the_open_step(patch, gio, monkeypatch):
    monkeypatch.setattr(R, "_scope_hit", lambda f, truth, e: "kind_cell")
    patch.events = [_evt()]
    patch.handler = lambda s, p: []
    d = R.api_replay_timeline(604)
    opened = [x for x in d["steps"] if x["step"] == "open:L2_EVENT"]
    assert len(opened) == 1 and opened[0]["at"] == _iso(3)                    # 시간표의 「열림」은 창 안 첫 조각(08:05 꼴)
    assert opened[0]["event_opened_at"] == _iso(-8)                           # 07:52 에 열렸다는 사실은 따로


# ── `_kpi_filter` ──────────────────────────────────────────────────────────────────────────

def test_kpi_filter_only_restricts_cells_that_came_only_from_a_partial_event(monkeypatch):
    monkeypatch.setattr(R, "_truth_of", lambda fid: {"cells": ["4-0"]})
    partial = {"_cells": {"1-0", "2-0", "4-0"}, "_kpis": {"U-07", "N-02"}}
    whole = {"_cells": {"2-0", "5-0"}}
    kf = R._kpi_filter(1, [partial, whole])
    assert kf == {"1-0": {"U-07", "N-02"}}                                    # 2-0(전체 인정 사건 셀)·4-0(정답 셀)은 거르지 않는다
    assert R._kpi_filter(1, [whole]) == {} and R._kpi_filter(1, []) == {}      # 일반 경로는 동작 그대로(거르지 않는다)


def test_kpi_filter_unions_the_kpis_of_two_partial_events(monkeypatch):
    monkeypatch.setattr(R, "_truth_of", lambda fid: {})
    a = {"_cells": {"1-0"}, "_kpis": {"U-07"}}
    b = {"_cells": {"1-0", "2-0"}, "_kpis": {"N-02"}}
    assert R._kpi_filter(1, [a, b]) == {"1-0": {"U-07", "N-02"}, "2-0": {"N-02"}}


def test_timelapse_paints_only_the_pieces_kpis_on_cells_of_a_partial_event(patch, monkeypatch):
    """F-598: 같은 셀에 07:58 부터 C-24 판정이 서 있었다 — 조각 지표(U-07)만 칠한다. 거르지 않는 셀은 그대로."""
    patch.cells = ["1-0", "2-0"]
    monkeypatch.setattr(R, "_kpi_filter", lambda fid, evs: {"1-0": {"U-07"}})
    patch.handler = _anoms({"cells": [(_t(-2), "1-0", "C-24", "MAJOR", 270, 28.6, 900), (_t(3), "1-0", "C-24", "MAJOR", 270, 28.6, 900),
                                      (_t(3), "1-0", "U-07", "MAJOR", 30, 20, 900), (_t(3), "2-0", "C-24", "MAJOR", 270, 28.6, 900)]})
    f = {x["t"][11:16]: x for x in R.api_replay_timelapse(604)["frames"]}
    assert "1-0" not in f[_t(-2).strftime("%H:%M")]["cells"]                  # 주입 전부터 켜지던 C-24 는 칠하지 않는다
    assert f[_t(3).strftime("%H:%M")]["cells"]["1-0"]["kpi"] == "U-07" and f[_t(3).strftime("%H:%M")]["cells"]["1-0"]["kpis"] == ["U-07"]
    assert f[_t(3).strftime("%H:%M")]["cells"]["2-0"]["kpi"] == "C-24"        # 거르지 않는 셀은 그대로


def test_first_anomaly_skips_rows_of_other_kpis_only_when_filtering_and_asks_more_rows(patch, monkeypatch):
    patch.cells = ["1-0"]
    rows = [(_t(-2), "CELL", "1-0", "C-24", "MAJOR"), (_t(5), "CELL", "1-0", "U-07", "MAJOR")]
    seen = []
    patch.handler = lambda s, p: (seen.append(p[-1]) or rows) if "AI_ANOMALY_EVENT" in s else []
    monkeypatch.setattr(R, "_kpi_filter", lambda fid, evs: {"1-0": {"U-07"}})
    d = R.api_replay_timeline(604)
    first = [x for x in d["steps"] if x["step"] == "first_anomaly"][0]
    assert first["detail"]["kpi"] == "U-07" and first["lead_min"] == 5.0 and seen[-1] == R.FIRST_SCAN_ROWS   # C-24 는 건너뛴다
    seen.clear()
    monkeypatch.setattr(R, "_kpi_filter", lambda fid, evs: {})
    d = R.api_replay_timeline(604)
    first = [x for x in d["steps"] if x["step"] == "first_anomaly"][0]
    assert first["detail"]["kpi"] == "C-24" and seen[-1] == 1                  # 거르지 않을 때는 전처럼 1행만 읽는다


# ── `expired` · `anomaly_earliest` ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("earliest, expect", [(T0 + timedelta(days=1), True), (T0 - timedelta(days=1), False), (None, False)])
def test_expired_means_the_fault_started_before_the_oldest_judgement_row(patch, monkeypatch, earliest, expect):
    monkeypatch.setattr(R, "_anomaly_earliest", lambda: earliest)
    d = R.api_replay_timelapse(604)
    assert d["expired"] is expect
    assert d["anomaly_earliest"] == (earliest.isoformat() if earliest else None)


def test_anomaly_earliest_is_one_min_query_cached_for_the_ttl(monkeypatch):
    sqls = []

    class Cur:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            sqls.append(" ".join(sql.split()))

        def fetchone(self):
            return (datetime(2026, 11, 18, 1, 40),)

    class Conn:
        def cursor(self):
            return Cur()

        def close(self):
            pass
    monkeypatch.setattr(R, "_connect", lambda: Conn())
    monkeypatch.setattr(R, "_EARLIEST", {"at": None, "t": 0.0})
    assert R._anomaly_earliest() == datetime(2026, 11, 18, 1, 40) and R._anomaly_earliest() == datetime(2026, 11, 18, 1, 40)
    assert sqls == ["SELECT MIN(EVENT_TIME) FROM AI_ANOMALY_EVENT"]            # 인덱스 끝 한 번 · 두 번째는 캐시
    R._EARLIEST["t"] = time.time() - R.EARLIEST_TTL_SEC - 1
    R._anomaly_earliest()
    assert len(sqls) == 2                                                      # TTL 이 지나면 다시 묻는다
    assert R.EARLIEST_TTL_SEC >= 60 and "초기값" in open(R.__file__, encoding="utf-8").read()


# ── `measured` ─────────────────────────────────────────────────────────────────────────────

TH = {"static": {"zerowin_down": {"major": {"above": 15}, "critical": {"above": 33}}}}   # 일부러 20·40 이 아닌 값 — 설정에서 읽는지


@pytest.fixture
def route_fault(patch, monkeypatch):
    patch.lines = [_ln("YMT", ["1-0", "2-0"]), _ln("OTHER", ["9-0"])]
    monkeypatch.setattr(R, "_injected_line", lambda f: "YMT")
    monkeypatch.setattr(R, "_graphio", lambda p, timeout=8: (TH, None) if p == "/api/settings/thresholds" else (None, "x"))
    sqls = []

    def h(sql, params):
        if "PM_COR_UPLANE_CELL_1M" in sql:
            sqls.append((sql, params))
            return [(_t(3), 6, 60), (_t(4), 0, 0), (_t(5), 9, 90)]
        return []
    patch.handler = h
    patch.sqls = sqls
    return patch


def test_measured_is_the_route_cells_pooled_ratio_with_gaps_left_as_none(route_fault):
    d = R.api_replay_timelapse(604)["measured"]
    pts = {p["t"][11:16]: p for p in d["points"]}
    assert pts[_t(3).strftime("%H:%M")] == {"t": pts[_t(3).strftime("%H:%M")]["t"], "num": 6, "den": 60, "value": 10.0}   # 분자 ÷ 분모 × 100
    assert pts[_t(4).strftime("%H:%M")]["value"] is None and pts[_t(4).strftime("%H:%M")]["den"] == 0          # 분모 0 — 0% 가 아니다
    assert pts[_t(2).strftime("%H:%M")] == {"t": pts[_t(2).strftime("%H:%M")]["t"], "num": None, "den": None, "value": None}   # 행이 없는 분 — 모름
    assert d["rows"] == 3 and d["cells"] == 2 and d["route"] == "YMT" and d["kpi"] == "U-07"
    assert [p["t"] for p in d["points"]] == sorted(p["t"] for p in d["points"]) and len(d["points"]) == len(R.api_replay_timelapse(604)["frames"])


def test_measured_sums_numerator_and_denominator_in_sql_never_averages_ratios(route_fault):
    R.api_replay_timelapse(604)
    sql, params = route_fault.sqls[-1]
    assert "SUM(ZEROWIN_DN)" in sql and "SUM(SESSIONS)" in sql and "GROUP BY EVENT_TIME" in sql
    assert "AVG(" not in sql.upper()                                            # 비율의 평균은 틀린다(불변규칙 2)
    assert "SERIES = %s" in sql and params[0] == "transport"                    # transport 계열(분모 `sessions_tp`)
    assert "(ENB_ID, CELLNUM) IN ((%s, %s),(%s, %s))" in sql                    # 노선 셀만 — 그 노선 두 셀


def test_measured_threshold_is_read_from_graphio_and_never_invented(route_fault, monkeypatch):
    d = R.api_replay_timelapse(604)["measured"]
    assert d["threshold"] == {"major": 15, "critical": 33}                      # 20·40 이 아니라 설정에서 온 값
    assert d["threshold_src"].startswith("graphio /api/settings/thresholds") and d["threshold_error"] is None
    monkeypatch.setattr(R, "_graphio", lambda p, timeout=8: (None, "no route"))
    d = R.api_replay_timelapse(604)["measured"]
    assert d["threshold"] == {} and d["threshold_error"] == "no route"          # 못 읽으면 비운다 — 기본값을 지어내지 않는다


def test_measured_rows_zero_means_the_one_minute_table_is_gone(route_fault):
    route_fault.handler = lambda s, p: []
    d = R.api_replay_timelapse(604)["measured"]
    assert d["rows"] == 0 and all(p["value"] is None for p in d["points"])      # 곡선이 비는 것을 「낮았다」로 읽지 않게 rows 로 구분


def test_measured_is_none_without_a_route_injection_or_a_main_kpi(patch, monkeypatch):
    assert R.api_replay_timelapse(604)["measured"] is None                      # 노선 주입이 아니다(`_injected_line` ""·대상 없음)
    monkeypatch.setattr(R, "_injected_line", lambda f: "YMT")
    monkeypatch.setattr(R, "_fault", lambda fid: dict(FAULT, fault_id=fid, kind="dns_degradation"))
    assert R.api_replay_timelapse(604)["measured"] is None                      # 주 지표가 정해진 종류가 아니다
    patch.lines = []
    monkeypatch.setattr(R, "_fault", lambda fid: dict(FAULT, fault_id=fid))
    assert R.api_replay_timelapse(604)["measured"] is None                      # 노선 셀이 없다


def test_measured_spec_is_the_graphio_u07_definition():
    assert R.MEASURED_KPI["ho_interruption"] == ("U-07", "zerowin_down", "transport", "ZEROWIN_DN", "SESSIONS")
    # graphio 모듈이 없는 환경(probe-datagen `.venv`)에서도 같은 결과여야 한다 — 소스 파일을 경로로 읽는다. 저장소가 없으면 건너뛴다
    from pathlib import Path
    src = Path(R.__file__).resolve().parents[4] / "graphio-probe-rca" / "src" / "graphio" / "metrics" / "kpi.py"
    if not src.exists():
        pytest.skip("graphio 소스가 옆에 없다")
    text = src.read_text(encoding="utf-8")
    m = re.search(r'KpiDef\("U-07",[^\n]*\n?[^\n]*"(?P<expr>100\.0 \* zerowin_dn / NULLIF\(sessions_tp, 0\))"[^)]*denom="sessions_tp"\)', text)
    assert m, "U-07 정의가 바뀌었다 — `MEASURED_KPI` 의 분자·분모를 다시 대조한다"
    assert "ZEROWIN_DN" == R.MEASURED_KPI["ho_interruption"][3] and "SESSIONS" == R.MEASURED_KPI["ho_interruption"][4]
