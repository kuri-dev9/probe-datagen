"""매일 같은 시각 부하 — 생성 범위 밖 역(2026-11-21 · 阿佐ヶ谷 F-493~495, 18역).

1. `/api/stations` 가 역마다 생성 범위 안 셀 수(`cells_gen`)와 안에 든 ID(`ids_live`)를 낸다 — 생성기와 같은 셀 마스터 기준.
2. 취소 사유(`faults.daily_cancel_note`) — 범위 밖과 역 매핑에 없음을 가른다. 매일 부하가 아니면 None(일반 사유).
3. 화면(`main.html`)이 범위 밖 역을 추가 목록에서 빼고 저장된 칩은 흐리게 보인다. 일본어 문구가 있다.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from datagen import faults
from datagen.ui import i18n_ja, status_api

HTML = (Path(status_api.__file__).parent / "static" / "main.html").read_text(encoding="utf-8")
IN, OUT = "35.1-139.1", "35.2-139.2"          # 역 ID(짧은 꼴) — 셀이 생성 범위 안 / 밖


class _Cur:
    def __init__(self, rows):
        self.rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, *a):
        assert "CM_CELL_STATION_MAP" in sql

    def fetchall(self):
        return self.rows


class _Conn:
    def __init__(self, rows):
        self.rows = rows
        self.closed = False

    def cursor(self):
        return _Cur(self.rows)

    def close(self):
        self.closed = True


def _stations(monkeypatch, rows, gen_cells):
    conn = _Conn(rows)
    monkeypatch.setattr(status_api, "_connect", lambda: conn)
    monkeypatch.setattr(status_api, "_cells", lambda c: [SimpleNamespace(cell_key=k) for k in gen_cells])
    monkeypatch.setattr(status_api, "_station_names", lambda: {})
    out = status_api.api_stations()
    assert conn.closed
    return {g["name"]: g for g in out["stations"]}, out


def test_stations_count_cells_inside_the_generation_scope(monkeypatch):
    rows = [("新宿", f"STN-{IN}", "100", 1), ("新宿", f"STN-{IN}", "100", 2), ("新宿", f"STN-{IN}", "100", 3),
            ("阿佐ヶ谷", f"STN-{OUT}", "195779", 1), ("阿佐ヶ谷", f"STN-{OUT}", "195779", 2)]
    by, _ = _stations(monkeypatch, rows, {"100-1", "100-2"})
    assert (by["新宿"]["cells"], by["新宿"]["cells_gen"]) == (3, 2)            # 일부만 범위 안
    assert by["新宿"]["ids_live"] == [IN]
    assert (by["阿佐ヶ谷"]["cells"], by["阿佐ヶ谷"]["cells_gen"]) == (2, 0)    # 전부 밖 — 화면이 목록에서 뺀다
    assert by["阿佐ヶ谷"]["ids_live"] == [] and by["阿佐ヶ谷"]["ids"] == [OUT]  # 이미 저장된 ID 는 이름으로 보인다


def test_stations_do_not_guess_scope_from_recent_calls(monkeypatch):
    """범위 판정은 생성 셀 마스터뿐이다 — 셀 키가 마스터에 있으면 호가 없어도 안이다"""
    by, _ = _stations(monkeypatch, [("渋谷", f"STN-{IN}", "100", 9)], {"100-9"})
    assert by["渋谷"]["cells_gen"] == 1


def test_a_name_with_several_ids_keeps_only_live_ids_in_ids_live(monkeypatch):
    rows = [("阿佐ヶ谷", "STN-1-1", "1", 1), ("阿佐ヶ谷", "STN-2-2", "2", 1)]
    by, _ = _stations(monkeypatch, rows, {"2-1"})
    assert by["阿佐ヶ谷"]["ids"] == ["1-1", "2-2"] and by["阿佐ヶ谷"]["ids_live"] == ["2-2"]
    assert by["阿佐ヶ谷"]["cells_gen"] == 1


def test_stations_sorted_by_generated_cells_first(monkeypatch):
    rows = [("A", "STN-a", "1", 1), ("B", "STN-b", "2", 1), ("B", "STN-b", "2", 2), ("C", "STN-c", "3", 1)]
    _, out = _stations(monkeypatch, rows, {"2-1", "2-2", "1-1"})
    assert [g["name"] for g in out["stations"]] == ["B", "A", "C"]            # 생성 셀 많은 순, 0 은 맨 뒤


def test_stations_returns_an_error_instead_of_raising_when_the_map_is_missing(monkeypatch):
    def boom():
        raise RuntimeError("no table")
    monkeypatch.setattr(status_api, "_connect", boom)
    with pytest.raises(RuntimeError):                                          # 연결 자체 실패는 호출부가 본다
        status_api.api_stations()
    conn = _Conn([])
    monkeypatch.setattr(status_api, "_connect", lambda: conn)
    monkeypatch.setattr(status_api, "_cells", lambda c: (_ for _ in ()).throw(RuntimeError("마스터 없음")))
    out = status_api.api_stations()
    assert out["stations"] == [] and "마스터 없음" in out["error"] and conn.closed


# ---------------------------------------------------------------- 취소 사유

@pytest.fixture()
def station_cells(monkeypatch):
    monkeypatch.setattr(faults, "STATION_CELLS", {IN: {"100-1"}})


def test_cancel_note_out_of_range_vs_unknown_station(station_cells):
    # 역 매핑에는 있는데 이 gen 의 셀이 없다 → STATION_CELLS 는 마스터 전량에서 만든 매핑이라 키는 있다
    faults.STATION_CELLS[OUT] = {"195779-1"}
    assert "생성 범위 밖" in faults.daily_cancel_note(f"DAILY:ST:{OUT}")
    assert "생성 범위 밖" in faults.daily_cancel_note(f"DAILY:ST:STN-{OUT}")    # 긴 꼴도 같다
    note = faults.daily_cancel_note("DAILY:ST:9.9-9.9")
    assert "역 매핑에 없는 역" in note and "생성 범위 밖" not in note


@pytest.mark.parametrize("raw", [None, "", "ENB:100", "CELL:100-1", "SITE:x", "DAILY:ENB:100", "DAILY:CELL:100-1"])
def test_cancel_note_is_none_for_everything_but_a_daily_station(station_cells, raw):
    assert faults.daily_cancel_note(raw) is None                               # 호출부의 일반 사유를 쓴다


def test_cancel_note_is_called_from_the_cancel_branch_with_the_general_fallback():
    src = (Path(faults.__file__).parent / "cli.py").read_text(encoding="utf-8")
    i = src.index("daily_cancel_note(tgt.get(int(fid)))")
    assert "or _CANCEL_NOTE" in src[i:i + 80]                                   # None 이면 기존 사유


def test_partial_out_of_range_station_still_resolves_only_inside_cells(station_cells):
    """역 하나의 셀 일부만 범위 안이면 걸리는 셀은 범위 안 셀뿐이다(취소 아님)"""
    faults.STATION_CELLS[IN] = {"100-1", "100-2", "100-3"}
    cells = [SimpleNamespace(cell_key="100-1"), SimpleNamespace(cell_key="100-2")]
    assert faults._daily_cells(f"ST:{IN}", cells) == {"100-1", "100-2"}
    assert faults._daily_cells(f"ST:{OUT}", cells) == set()                    # 전부 밖 → 빈 집합 → 취소 사유가 갈린다


# ---------------------------------------------------------------- 화면 · 일본어

def test_page_hides_out_of_range_stations_from_the_add_list_and_dims_saved_chips():
    assert 'STATIONS.filter(g => g.cells_gen > 0)' in HTML                     # 추가 목록
    assert 'x.cells_gen > 0 && [x.name_ko' in HTML                              # 이름으로 추가할 때도 범위 안만
    assert 'chip${g.out ? " out" : ""}' in HTML and "생성 범위 밖 — 부하가 걸리지 않음" in HTML
    assert "ids_live" in HTML                                                   # 추가할 때 범위 안 ID 만


def test_page_warns_on_save_and_never_removes_the_chip_for_the_user():
    assert "저장됨 — 다만 생성 범위 밖 역은 부하가 걸리지 않음" in HTML


def test_japanese_strings_exist_for_the_new_messages():
    ja = i18n_ja.__dict__
    blob = "".join(str(v) for v in ja.values() if isinstance(v, dict))
    for ko in ("생성 범위 밖 — 부하가 걸리지 않음", "저장됨 — 다만 생성 범위 밖 역은 부하가 걸리지 않음: {s}"):
        assert ko in blob
    assert "{s}" in blob
