"""한 통합장애에 묶인 두 주입을 정답 셀로 가르기 · 주입별 정답 원장 · 높은 건물 레이어 — 2026-11-25 맥스 구현 · 에스터 시험.

사건: 9번 F-612(전파 약함)와 8번 F-613(연결 끊김)이 시간이 겹쳐 graphio 가 통합장애 하나로 묶었고 두 재생이 서로의 셀을 칠했다.
원인 코드가 같아 종류로는 못 가른다 — 가르는 근거는 주입 범위(정답 셀)뿐인데 `ground_truth.json` 은 그때 걸린 주입만 남는다.
그래서 생성기가 `truth_by_fault.json`(주입별 원장)을 쌓고 재생이 `_truth_any` 로 거기서 읽는다.
  * `_split_shared`: 이 장애 정답 셀(또는 `ENB:` 지정)이 있으면 겹치는 셀만 · 없으면 같이 잡은 주입의 정답 셀을 뺀다 · 둘 다 모르면 그대로(짐작하지 않는다)
  * `_truth_any`: 지금 정답 파일이 먼저, 없으면 원장. 원장이 없거나 번호가 없으면 빈 값
  * `pemdb1.archive_truth`: 번호별로 쌓고(같은 번호는 덮는다) 최근 N건만 · 임시 파일에 쓴 뒤 rename · 실패해도 정답 기록을 막지 않는다
  * 높은 건물 레이어는 지도 첫 로딩에 들어가지 않고 3D 전용 이름으로만 서빙한다
DB·graphio 는 건드리지 않는다(임시 폴더와 가짜 설정).
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

import pytest

from datagen import pemdb1
from datagen.ui import map_api
from datagen.ui import replay_api as R
from test_replay_api_1124 import FAULT, patch  # noqa: F401  (patch 는 픽스처 — 이 파일은 `_ev_cells` 만 따로 가짜로 쓴다)

ROOT = Path(__file__).resolve().parent.parent


# ── _split_shared ─────────────────────────────────────────────────────────────

def _f(fid, **kw):
    return {**FAULT, "fault_id": fid, **kw}


def _ev(cells):
    return {"unit": "L2_EVENT", "event_id": "EVT-x", "_c": set(cells)}


@pytest.fixture
def cells_of(monkeypatch):
    monkeypatch.setattr(R, "_ev_cells", lambda e: set(e.get("_cells", e.get("_c", ()))))


NINE = ["678404-14", "790173-0", "786648-1", "678966-2"]      # F-612 (9번) — eNB 4곳
EIGHT = ["190935-0", "191034-1"]                              # F-613 (8번) — eNB 2곳


def test_split_keeps_only_cells_in_this_faults_own_truth(cells_of):
    e = _ev(NINE + EIGHT)
    out = R._split_shared(_f(612), {"cells": NINE}, e, [_f(613)])
    assert out["_cells"] == set(NINE) and out["shared_with"] == [613] and out["shared_split"] == "own_scope"
    out = R._split_shared(_f(613), {"cells": EIGHT}, e, [_f(612)])
    assert out["_cells"] == set(EIGHT) and out["shared_split"] == "own_scope"
    assert e["_c"] == set(NINE + EIGHT), "원본 사건 행을 바꾸지 않는다(복사본을 돌려준다)"


def test_split_with_an_enb_target_uses_the_enbs_when_truth_is_gone(cells_of):
    out = R._split_shared(_f(613, target="ENB:190935,+,191034"), {}, _ev(NINE + EIGHT), [_f(612)])
    assert out["_cells"] == set(EIGHT) and out["shared_split"] == "own_scope"


def test_split_subtracts_the_other_faults_truth_when_own_truth_is_unknown(cells_of, monkeypatch):
    monkeypatch.setattr(R, "_truth_any", lambda fid: {"cells": EIGHT} if fid == 613 else {})
    out = R._split_shared(_f(612), {}, _ev(NINE + EIGHT), [_f(613)])
    assert out["_cells"] == set(NINE) and out["shared_split"] == "minus_others"


def test_split_leaves_the_event_alone_when_nobody_knows_the_scope(cells_of, monkeypatch):
    """옛 주입 — 정답도 지정도 없다. 조각 조합으로 짐작하지 않고 그대로 두되, 못 갈랐다고 `shared_split: None` 으로 말한다"""
    monkeypatch.setattr(R, "_truth_any", lambda fid: {})
    e = _ev(NINE + EIGHT)
    out = R._split_shared(_f(612), {}, e, [_f(613)])
    assert "_cells" not in out and out["shared_split"] is None and out["shared_with"] == [613]
    assert R._split_shared(_f(612), {"cells": NINE}, e, []) is e, "같이 잡은 주입이 없으면 손대지 않는다"


def test_own_events_splits_two_overlapping_faults_through_the_ledger(patch, monkeypatch):
    """끝에서 끝까지 — 같은 사건을 두 주입이 시각·범위로 잡으면 각자 자기 정답 셀만 받는다(원인 코드가 같아도)"""
    truth = {612: {"cells": NINE}, 613: {"cells": EIGHT}}
    monkeypatch.setattr(R, "_truth_any", lambda fid: truth.get(fid, {}))
    monkeypatch.setattr(R, "_truth_of", lambda fid: truth.get(fid, {}))
    f12, f13 = _f(612, kind="ran_radio"), _f(613, kind="ran_drop_surge")
    patch.faults = [f12, f13]
    ev = {"unit": "L2_EVENT", "event_id": "EVT-f78", "opened_time": FAULT["start_ts"].isoformat(), "root_code": "RAN_RADIO_QUALITY",
          "_c": set(NINE + EIGHT)}
    monkeypatch.setattr(R, "_ev_cells", lambda e: set(e.get("_cells", e.get("_c", ()))))
    own12, _ = R._own_events(f12, [ev])
    own13, _ = R._own_events(f13, [ev])
    assert own12 and own13
    assert R._ev_cells(own12[0]) == set(NINE) and R._ev_cells(own13[0]) == set(EIGHT)


def test_claims_never_calls_graphio_directly_only_through_ev_doc():
    """`_claims` 가 모든 자기 사건에 대해 돈다 — graphio 사건 문서를 묻는 길은 `_ev_doc`(시험이 막는 자리)을 거쳐야 한다"""
    import inspect
    for fn in (R._claims, R._split_shared, R._own_events):
        assert "_graphio(" not in inspect.getsource(fn) and "urlopen" not in inspect.getsource(fn), fn.__name__
    assert "_graphio(" in __import__("inspect").getsource(R._ev_doc)


# ── _truth_any ────────────────────────────────────────────────────────────────

def _ledger(tmp_path, faults):
    (tmp_path / "truth_by_fault.json").write_text(json.dumps({"faults": faults}), encoding="utf-8")
    return tmp_path / "ground_truth.json"


@pytest.fixture
def any_env(monkeypatch, tmp_path):
    cur = {}
    monkeypatch.setattr(R, "_truth_of", lambda fid: cur.get(fid, {}))
    monkeypatch.setattr(R, "_pem_cfg", lambda: {"truth_out": str(tmp_path / "ground_truth.json")})
    R._TBF.update(mtime=None, faults={})
    yield cur
    R._TBF.update(mtime=None, faults={})


def test_truth_any_prefers_the_current_truth_file_then_the_ledger(any_env, tmp_path):
    _ledger(tmp_path, {"F-612": {"cells": NINE}, "F-613": {"cells": ["old"]}})
    any_env[613] = {"cells": EIGHT}                                  # 지금 정답 파일에 있는 것이 이긴다
    assert R._truth_any(613) == {"cells": EIGHT}
    assert R._truth_any(612) == {"cells": NINE}                      # 없으면 원장
    assert R._truth_any(999) == {}                                   # 원장에도 없으면 빈 값 — 지금처럼


def test_truth_any_without_a_ledger_behaves_as_before(any_env):
    assert R._truth_any(612) == {}
    any_env[612] = {"cells": NINE}
    assert R._truth_any(612) == {"cells": NINE}


def test_truth_any_rereads_the_ledger_only_when_the_file_changes(any_env, tmp_path):
    import os
    p = tmp_path / "truth_by_fault.json"
    _ledger(tmp_path, {"F-612": {"cells": ["a"]}})
    assert R._truth_any(612) == {"cells": ["a"]}
    st = p.stat()
    p.write_text(json.dumps({"faults": {"F-612": {"cells": ["b"]}}}), encoding="utf-8")
    os.utime(p, (st.st_atime, st.st_mtime))                         # 같은 mtime — 캐시를 쓴다
    assert R._truth_any(612) == {"cells": ["a"]}
    os.utime(p, (st.st_atime, st.st_mtime + 5))
    assert R._truth_any(612) == {"cells": ["b"]}


def test_truth_any_survives_a_broken_ledger(any_env, tmp_path):
    (tmp_path / "truth_by_fault.json").write_text("{not json", encoding="utf-8")
    assert R._truth_any(612) == {}


def test_replay_uses_truth_any_everywhere_but_the_shared_pairing_score_keeps_truth_of():
    src = Path(R.__file__).read_text(encoding="utf-8")
    body = re.sub(r"(?m)^\s*#.*$", "", src)
    assert len(re.findall(r"_truth_any\(", body)) >= 6
    assert "def _truth_of" not in src, "이름을 겹쳐 쓰지 않는다(공유 함수 faults_api._truth_of 와 별개)"


# ── pemdb1.archive_truth ──────────────────────────────────────────────────────

START = datetime(2026, 11, 25, 14, 0, 0)


def _sc(n, minute=5, **kw):
    return {"scenario_id": f"F-{n}", "kind": "ran_radio", "cells": [f"{n}-0"], "start_minute": minute, "duration": 11, **kw}


def test_archive_stacks_by_fault_number_and_overwrites_the_same_number(tmp_path):
    p = tmp_path / "truth_by_fault.json"
    pemdb1.archive_truth(p, START, [_sc(612), _sc(613, 7)])
    pemdb1.archive_truth(p, START, [_sc(613, 9, cells=["new-0"])])
    book = json.loads(p.read_text(encoding="utf-8"))["faults"]
    assert list(book) == ["F-612", "F-613"]
    assert book["F-612"]["start_ts"] == "2026-11-25T14:05:00"      # 상대 분이 절대 시각이 된다
    assert book["F-613"]["cells"] == ["new-0"] and book["F-613"]["start_ts"] == "2026-11-25T14:09:00"
    assert book["F-612"]["archived_at"] and not list(tmp_path.glob("*.tmp")), "임시 파일이 남지 않는다(rename)"


def test_archive_keeps_only_the_most_recent_n_by_number(tmp_path, monkeypatch):
    monkeypatch.setattr(pemdb1, "TRUTH_BY_FAULT_KEEP", 3)
    p = tmp_path / "truth_by_fault.json"
    for n in (610, 611, 612, 613, 614):
        pemdb1.archive_truth(p, START, [_sc(n)])
    assert list(json.loads(p.read_text(encoding="utf-8"))["faults"]) == ["F-612", "F-613", "F-614"]   # 번호가 큰 쪽이 남는다
    assert pemdb1.TRUTH_BY_FAULT_KEEP == 3 and ROOT.joinpath("src/datagen/pemdb1.py").read_text(encoding="utf-8").count("TRUTH_BY_FAULT_KEEP = 500") == 1


def test_archive_skips_non_fault_rows_and_recovers_from_a_corrupt_book(tmp_path):
    p = tmp_path / "truth_by_fault.json"
    p.write_text("{broken", encoding="utf-8")
    pemdb1.archive_truth(p, START, [{"scenario_id": "S-1", "start_minute": 0}, _sc(612)])
    assert list(json.loads(p.read_text(encoding="utf-8"))["faults"]) == ["F-612"]
    before = p.read_bytes()
    pemdb1.archive_truth(p, START, [])
    assert p.read_bytes() == before


def test_archive_failure_never_breaks_the_truth_record(tmp_path):
    pemdb1.archive_truth(tmp_path / "no_such_dir" / "truth_by_fault.json", START, [_sc(612)])   # 예외 없이 지나간다


def test_write_truth_still_writes_ground_truth_unchanged_and_archives_after_it():
    src = (ROOT / "src/datagen/pemdb1.py").read_text(encoding="utf-8")
    i = src.index("def write_truth")
    body = src[i:src.index("\nTRUTH_BY_FAULT_FILE", i)]
    assert body.index("path.write_text(") < body.index("archive_truth(path.parent / TRUTH_BY_FAULT_FILE, start, doc[\"scenarios\"])")
    assert '"scenarios": [sc.as_truth() for sc in scenarios if sc.scored]' in body, "채점 형식은 그대로"
    # 채점(graphio)은 원장을 읽지 않는다
    gp = ROOT.parent / "graphio-probe-rca" / "src"
    if gp.exists():
        assert not any("truth_by_fault" in f.read_text(encoding="utf-8", errors="ignore") for f in gp.rglob("*.py"))


# ── 높은 건물 레이어 ──────────────────────────────────────────────────────────

def test_building_tall_is_served_for_3d_only_and_not_in_the_first_map_load(monkeypatch, tmp_path):
    assert "building_tall" in map_api._LAYER_3D and "building_tall" not in map_api._LAYER
    f = tmp_path / "building_tall.json"
    f.write_text(json.dumps([{"h": 332.6, "min_h": 100, "rings": [[[139.7, 35.6], [139.71, 35.6], [139.7, 35.61]]], "name": "x"}]), encoding="utf-8")
    monkeypatch.setattr(map_api, "_pem_cfg", lambda: {"geo_building_tall": str(f)})
    d = json.loads(map_api.api_map_layer("building_tall").body)
    assert set(d) == {"items", "source"} and d["items"][0]["h"] == 332.6 and "OpenStreetMap" in d["source"]
    monkeypatch.setattr(map_api, "_pem_cfg", lambda: {"geo_building_tall": str(tmp_path / "none.json")})
    r = map_api.api_map_layer("building_tall")
    assert isinstance(r, dict) and r["items"] == [] and r["error"], "파일이 없으면 빈 배열이 아니라 이유를 말한다"
    with pytest.raises(Exception):
        map_api.api_map_layer("no_such_layer")


def test_building_tall_config_keys_exist_in_both_yaml_and_the_2d_map_never_asks_for_it():
    for name in ("datagen.yaml", "local.yaml"):
        assert "geo_building_tall:" in (ROOT / "config" / name).read_text(encoding="utf-8"), name
    assert "building_tall" not in (ROOT / "src/datagen/ui/static/map.html").read_text(encoding="utf-8")
    geo = Path(map_api.__file__).read_text(encoding="utf-8")
    i = geo.index("def api_map_geo")
    assert "building_tall" not in geo[i:i + 6000], "지도 첫 로딩(`/api/map/geo`)의 범위·캐시 키에 넣지 않는다"
