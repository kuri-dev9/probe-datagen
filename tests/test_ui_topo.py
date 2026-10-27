"""`/api/topo` — **로케일 crosswalk 결함(A) 회귀.**

2026-10-26 결함: `api_topo` 가 "그릴 수 있는 동" 판정에 설정의 단일
crosswalk(= KR 것)를 썼다. jp 뷰가 *"949 구역 중 731곳에 동이 없다"* 는
**거짓 숫자**를 띄웠다 — 실제로는 `CM_DONG_JP` 가 949 구역 전부에 동을
가졌다. `gu_points`/`gu_keys` 때와 같은 모양의 실수였다(존재 판정에 다른
로케일의 자료를 썼다).

여기서 고정하는 것 둘 —

1. **crosswalk 파일이 없으면 `None`(산출 불가)** 이다 — `0`(전부 그릴 수
   있다)도 전수(전부 못 그린다)도 아니다(불변규칙 17).
2. **`locale` 이 그 로케일 자신의 crosswalk 를 읽는다** — 다른 로케일의
   파일을 읽으면 조인은 성립하고 에러도 나지 않는다. 수만 거짓이다.

**DB 를 쓰지 않는다**(집안 규칙). `FakeConn` 이 SQL 을 받아 기록한다.
`topo.pick_report`·`topo.restore` 는 **스텁으로 갈아친다** — 이 테스트의
관심은 crosswalk 로케일 분리뿐이고, 그 둘(`pemdb1.load_cells` 전체 체인)을
실제로 돌리는 것은 다른 테스트의 몫이다.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from datagen import topo, ui
from tests.test_topo import FakeConn

#: `survey()`·벌표 루프가 두드리는 테이블 — kr·jp 양쪽 전부 필요하다.
#: `GEO_SOURCES` 가 실제로 "kr"·"jp" 둘만 갖고 있다는 전제다(바뀌면 이
#: 테스트가 `KeyError` 없이 조용히 좁아지지 않도록, 아래에서 한 번
#: 더 확인한다).
_ALL_TABLES = (
    [topo.table_name(k, "kr") for k in topo.LOCALE_TABLE_KEYS]
    + [topo.table_name(k, "jp") for k in topo.LOCALE_TABLE_KEYS]
    + [topo.table_name("cellmap", "kr")]
)


class _ApiTopoConn(FakeConn):
    """`topo.survey`·`topo.gu_points`·`/api/topo` 의 벌표 루프가 던지는
    질의에 답한다. `gu_rows[locale]` 가 그 로케일 `CM_GU_<L>` 의 전수다."""

    def __init__(self, gu_rows: dict[str, list[tuple]]):
        super().__init__(ddl=("CITY_ID", "GU_ID", "GU_NAME",
                              "LATITUDE", "LONGITUDE", "CITY_NAME"))
        self.gu_rows = dict(gu_rows)

    def answer(self, sql, params):
        if "GU_NAME, LATITUDE, LONGITUDE" in sql:          # gu_points
            for locale, rows in self.gu_rows.items():
                if f"`{topo.table_name('gu', locale)}`" in sql:
                    return [(c, g, n, la, lo) for c, g, n, la, lo in rows
                            if la is not None and lo is not None]
            return []
        if "PM_COR_LTE_CALL_1M" in sql:                     # 활성·결손 집계
            return [(0,)]
        if "WHERE LATITUDE IS NULL OR LONGITUDE IS NULL" in sql:   # no_coord
            return [(0,)]
        if "SELECT COUNT(*) FROM" in sql:
            for t in _ALL_TABLES:
                if f"`{t}`" in sql:
                    return [(0,)]
        return super().answer(sql, params)


def _stub_pick_and_restore(monkeypatch):
    """`pick_report`·`restore` 는 이 테스트의 관심 밖이다 — 전체
    `load_cells` 체인을 흉내 내지 않고 고정 응답으로 건너뛴다."""
    monkeypatch.setattr(topo, "pick_report", lambda conn, xw, limit,
                         extra_cells=(): {
                             "candidates": 0, "band_master": 0,
                             "band_picked": 0, "band_missing": [],
                             "candidate_cells": 0, "picked_cells": 0,
                             "picked_enbs": 0})
    monkeypatch.setattr(topo, "restore", lambda conn, loc, dry_run=True: {
        "ok": True, "tables": {}, "scope": [], "not_restored": []})


def _write_crosswalk(path: Path, emd: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"source": "test", "map": {"emd": emd}}),
                    encoding="utf-8")


@pytest.fixture
def _api_topo_env(tmp_path, monkeypatch):
    """`_connect`·`_geo_dir`·`_topo_xw`·`LOCALE_DIR` 를 전부 갈아친다 —
    실 설정·실 DB·실 `data/` 디렉터리를 보지 않는다."""
    monkeypatch.setattr(topo, "LOCALE_DIR", tmp_path / "locale")
    monkeypatch.setattr(ui, "_geo_dir", lambda: tmp_path / "geo")
    # **선택 통과는 KR 기준**이다(`api_topo` 머리말) — KR crosswalk 를
    # 따로 하나 두어, "jp 가 KR 파일을 본다"는 실수가 나도 이 경로는
    # 영향받지 않는다는 것을 보여준다(섞인 것은 `no_dong` 쪽이었다).
    kr_xw = tmp_path / "geo" / topo.XW_FILE
    _write_crosswalk(kr_xw, {})
    monkeypatch.setattr(ui, "_topo_xw", lambda: kr_xw)
    _stub_pick_and_restore(monkeypatch)
    assert set(topo.GEO_SOURCES) == {"kr", "jp"}, (
        "GEO_SOURCES 가 kr·jp 둘이 아니면 이 테스트의 _ALL_TABLES 를 "
        "같이 넓혀야 한다")
    return tmp_path / "geo"


def test_missing_crosswalk_is_none_not_zero(_api_topo_env, monkeypatch):
    """**crosswalk 파일이 없으면 `None`** 이다 — `0`(전부 그릴 수 있다)이
    아니다. `0` 으로 찍히면 운영자는 "949 구역 전부 동이 있다"로 읽는데,
    실제로는 **재지 못한 것**이다.
    """
    gd = _api_topo_env
    conn = _ApiTopoConn({"jp": [(27, 99999, "福岡区", 33.5, 130.4)]})
    monkeypatch.setattr(ui, "_connect", lambda: conn)

    r = ui.api_topo(locale="jp")

    assert r["drawable_crosswalk_exists"] is False
    assert r["gus_without_drawable_dong"] is None
    assert r["seed_gus"] == 1          # 구는 조회됐다 — "0 개"가 아니다


def test_locale_reads_its_own_crosswalk_not_kr(_api_topo_env, monkeypatch):
    """**`locale="jp"` 는 jp crosswalk 를 읽는다** — KR 것을 읽으면 조인은
    성립하고 에러도 나지 않는 채로 수만 거짓이 된다(이번 결함의 재현
    조건). KR 파일에는 `(27, 99999)` 가 없고 JP 파일에만 있다 — 틀리게
    읽으면 이 구역이 "그릴 수 있는 동이 없다"로 거짓 집계된다.
    """
    gd = _api_topo_env
    _write_crosswalk(topo.crosswalk_path(gd, "kr"),
                     {"X": "11-11110-11110001"})          # KR 전용 — 다른 구역
    _write_crosswalk(topo.crosswalk_path(gd, "jp"),
                     {"Y": "27-99999-99999001"})           # JP 전용
    conn = _ApiTopoConn({"jp": [(27, 99999, "福岡区", 33.5, 130.4)]})
    monkeypatch.setattr(ui, "_connect", lambda: conn)

    r = ui.api_topo(locale="jp")

    assert r["drawable_crosswalk"] == str(topo.crosswalk_path(gd, "jp"))
    assert r["drawable_crosswalk_exists"] is True
    # KR 파일을 봤다면 (27,99999) 가 안 그려져 1 이 나왔을 자리다
    assert r["gus_without_drawable_dong"] == 0


def test_kr_view_is_unaffected_by_the_locale_split(_api_topo_env, monkeypatch):
    """KR 뷰(`locale="kr"`)는 바뀌지 않는다 — `_topo_xw()`(선택 통과 기준)
    와 `crosswalk_path(geo_dir, "kr")`(그릴 수 있는 동 판정)가 **같은
    파일**로 수렴하므로, 둘을 가른 것이 KR 쪽 값을 흔들 이유가 없다.
    """
    gd = _api_topo_env
    kr_xw_path = topo.crosswalk_path(gd, "kr")
    assert kr_xw_path == gd / topo.XW_FILE == gd / topo.XW_FILE  # kr 은 루트
    _write_crosswalk(kr_xw_path, {"X": "11-11110-11110001"})
    conn = _ApiTopoConn({"kr": [(11, 11110, "종로구", 37.57, 126.98),
                                (11, 11120, "중구", 37.56, 126.99)]})
    monkeypatch.setattr(ui, "_connect", lambda: conn)

    r = ui.api_topo(locale="kr")

    assert r["seed_gus"] == 2
    assert r["gus_without_drawable_dong"] == 1       # 중구만 그릴 동이 없다


# --------------------------------------------------------------------------
# B·C — **서버 필드와 그것을 읽는 JS 가 어긋나지 않는지** (2026-10-27)
#
# 서버만 고치고 화면이 옛 필드를 읽는 자리는 pytest 로는 못 잡는다 —
# 한 번 그렇게 되어(`coord_keys`·`locales[].rows` 가 응답에는 있었지만
# 렌더 코드가 아직 안 읽던 시점이 실제로 있었다) 소스 문자열로라도
# 고정해 둔다. DOM·브라우저가 없으니 **"있다/없다"** 까지만 본다 —
# 시각적 배치는 이 테스트의 범위 밖이다.
# --------------------------------------------------------------------------


def test_coord_keys_is_both_sent_and_read():
    """**C 의 서버·클라이언트 계약.** 응답에 `coord_keys` 가 있고, 화면
    JS 가 `d.coord_keys` 를 참조해 그 목록 밖이면 `imp()` 를 거치지 않아야
    한다 — 참조하지 않으면 `enb_org`·`cellmap` 의 `no_coord` 가 여전히
    "산출 불가"로 찍힌다(이번에 실제로 그런 시점이 있었다).
    """
    src = ui._HTML_TOPO
    assert "const COORD = d.coord_keys" in src
    assert "COORD.includes(k)" in src


def test_locales_rows_is_both_sent_and_read():
    """**B 의 서버·클라이언트 계약.** `api_topo` 가 `locales[].rows` 를
    싣고, 화면 JS 가 `t.rows` 를 직접 쓴다 — `d.tables`(현재 보는 로케일
    뿐인 자리)에서 다시 끌어오면 다른 로케일 줄이 전부 `—` 로 보여
    "없다"처럼 읽힌다(이번에 실제로 그런 시점이 있었다).
    """
    api_src = inspect.getsource(ui.api_topo)
    assert '"rows": (' in api_src and "if ex else None" in api_src

    ui_src = ui._HTML_TOPO
    assert "n(t.rows)" in ui_src
    # 옛 계약(현재 로케일에서만 값을 끌어오던 모양)이 남아 있지 않다
    assert "d.locale === lc" not in ui_src
