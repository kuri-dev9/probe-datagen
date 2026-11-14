"""`_dong_at` — 읍면동 정본이 `CM_GEO_UNIT` ADM3(町) 이다(2026-11-15 B안). 없는 코드만 `CM_DONG_JP`. DB 에는 쓰지 않는다."""
from __future__ import annotations

import pymysql

from datagen.ui import map_api

UNIT = {"131010010": ("千代田区 丸の内", 35.68, 139.76), "131010020": ("千代田区 神田", 35.69, 139.77)}
DONG = {"13101001": ("千代田区1丁目", 35.1, 139.1), "13101002": ("옛 동", 35.2, 139.2)}


class _Cur:
    def __init__(self, log):
        self.log, self.rows = log, []

    def __enter__(self): return self
    def __exit__(self, *a): return False

    def execute(self, sql, params=()):
        self.log.append((sql, list(params)))
        if "CM_GEO_UNIT" in sql:
            self.rows = [(c, *UNIT[c]) for c in params if c in UNIT]
        else:
            self.rows = [(c, *DONG[c]) for c in params if c in DONG]

    def fetchall(self): return self.rows


class _Conn:
    def __init__(self, log): self.log = log
    def cursor(self): return _Cur(self.log)
    def close(self): pass


def _run(monkeypatch, ids):
    log = []
    monkeypatch.setattr(map_api, "_connect", lambda: _Conn(log))
    monkeypatch.setattr(map_api, "_dong_shapes", lambda: {})
    return map_api._dong_at(ids), log


def test_town_codes_are_read_from_geo_unit_and_synthetic_dong_table_is_not_asked(monkeypatch):
    at, log = _run(monkeypatch, ["131010010", "131010020"])
    assert at["131010010"] == {"id": "131010010", "name": "千代田区 丸の内", "lat": 35.68, "lon": 139.76}
    assert len(log) == 1 and "CM_GEO_UNIT" in log[0][0] and "AREA_LEVEL = 'ADM3'" in log[0][0]


def test_codes_missing_from_geo_unit_fall_back_to_cm_dong_jp(monkeypatch):
    at, log = _run(monkeypatch, ["131010010", "13101001", "99999999"])
    assert at["13101001"]["name"] == "千代田区1丁目" and "99999999" not in at           # 어디에도 없으면 빠진다(지어내지 않는다)
    assert len(log) == 2 and "CM_DONG_JP" in log[1][0] and log[1][1] == ["13101001", "99999999"]


def test_geo_unit_wins_over_dong_jp_for_the_same_code(monkeypatch):
    UNIT["13101002"] = ("町 쪽 이름", 36.0, 140.0)
    try:
        at, log = _run(monkeypatch, ["13101002"])
    finally:
        del UNIT["13101002"]
    assert at["13101002"]["name"] == "町 쪽 이름" and len(log) == 1


def test_rows_without_coordinates_are_skipped_not_zero_filled(monkeypatch):
    UNIT["131010030"] = ("좌표 없음", None, None)
    try:
        at, _ = _run(monkeypatch, ["131010030"])
    finally:
        del UNIT["131010030"]
    assert at == {}


def test_rings_attach_by_code_and_db_error_gives_an_empty_result(monkeypatch):
    log = []
    monkeypatch.setattr(map_api, "_connect", lambda: _Conn(log))
    monkeypatch.setattr(map_api, "_dong_shapes", lambda: {"131010010": [[[1, 1], [2, 1], [2, 2], [1, 1]]]})
    at = map_api._dong_at(["131010010", "131010020"])
    assert "rings" in at["131010010"] and "rings" not in at["131010020"]

    def boom():
        raise pymysql.MySQLError("down")
    monkeypatch.setattr(map_api, "_connect", boom)
    assert map_api._dong_at(["131010010"]) == {}
