"""`topo seed-geo` — 행정구역을 **처음부터** 만든다. 깨면 조용히 틀린다.

지금까지 `CM_CITY`·`CM_GU`·`CM_DONG` 은 상용 반출 덤프였고 `topo` 에는
단건 CRUD 와 `dump`/`restore` 뿐이었다 — **"없는 상태에서 만들기"가 0** 이다.
`seed_enb` 가 `CM_GU` 의 좌표와 `CM_DONG` 의 동 목록을 전제로 돈다.

여기서 지키는 것 넷 —

1. 코드를 **지어내지 않는다.** 코드 맵에 없는 폴리곤은 넣지 않고 수를 말한다
2. 좌표는 **폴리곤 centroid** 다. bbox 중심이 아니다(해안선이 뻗은 시군구에서
   중심이 바다로 나간다)
3. 컬럼 타입이 테이블마다 다르다 — `CM_CITY` 는 varchar, 나머지는 float
4. **crosswalk 를 함께 쓴다.** 안 쓰면 `drawable_dongs` 가 비어 `seed_enb` 의
   `DONG_ID` 가 **에러 없이 전부 NULL** 이 된다

**DB 를 쓰지 않는다**(집안 규칙 — `tests/test_session_model.py` 머리말).
`FakeConn` 이 SQL 을 받아 기록하므로 "DELETE 를 몇 번 쏘았나"까지 단정한다.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

from datagen import topo
from tests.test_topo import FakeConn, _inserted_keys, _inserted_rows

CITY_DDL = ("CITY_ID", "CITY_NAME", "MAP_X", "MAP_Y", "MAP_CODE",
            "LONGITUDE", "LATITUDE")
GU_DDL = ("CITY_ID", "GU_ID", "CITY_NAME", "GU_NAME", "P_GU_ID", "MAP_X",
          "MAP_Y", "USE_YN", "MAP_CODE", "LONGITUDE", "LATITUDE")
DONG_DDL = ("CITY_ID", "GU_ID", "DONG_ID", "CITY_NAME", "GU_NAME", "DONG_NAME",
            "P_DONG_ID", "MAP_X", "MAP_Y", "USE_YN", "MAP_CODE",
            "LONGITUDE", "LATITUDE", "LATITUDE_IMSI")

#: 테스트 locale. 실제 번들 이름(`skorea-*`)을 쓰면 테스트가 60MB 파일을
#: 요구한다 — `GEO_SOURCES` 에 작은 것을 끼워 넣는다.
TL = "tl"


def _square(lon: float, lat: float, d: float = 0.1) -> dict:
    """정사각형 폴리곤. centroid 가 `(lon, lat)` 이어야 한다 — 계산 검증용."""
    return {"type": "Polygon", "coordinates": [[
        [lon - d, lat - d], [lon + d, lat - d],
        [lon + d, lat + d], [lon - d, lat + d], [lon - d, lat - d]]]}


def _fc(feats: list[tuple[str, str, float, float]]) -> dict:
    return {"type": "FeatureCollection", "features": [
        {"type": "Feature",
         "properties": {"code": c, "name": n},
         "geometry": _square(lon, lat)} for c, n, lon, lat in feats]}


def _geo_dir(tmp_path: Path, monkeypatch, *, with_emd: bool = True) -> Path:
    gd = tmp_path / "geo"
    gd.mkdir()
    names = {"sido": "tl-sido.json", "sgg": "tl-sgg.json", "emd": "tl-emd.json"}
    monkeypatch.setitem(topo.GEO_SOURCES, TL, names)
    monkeypatch.setattr(topo, "LOCALE_DIR", tmp_path / "locale")

    (gd / names["sido"]).write_text(json.dumps(_fc([
        ("01", "가도", 127.0, 37.5), ("02", "나도", 128.0, 36.0),
        ("09", "코드맵밖도", 129.0, 35.0),
    ])), encoding="utf-8")
    (gd / names["sgg"]).write_text(json.dumps(_fc([
        ("01010", "가시", 127.0, 37.5), ("01020", "나시", 127.2, 37.6),
        ("02010", "다시", 128.0, 36.0),
        ("09010", "코드맵밖시", 129.0, 35.0),
    ])), encoding="utf-8")
    if with_emd:
        (gd / names["emd"]).write_text(json.dumps(_fc([
            ("0101001", "가일동", 127.01, 37.51),
            ("0101002", "가이동", 127.02, 37.52),
            ("0102001", "나일동", 127.21, 37.61),
            ("0201001", "다일동", 128.01, 36.01),
        ])), encoding="utf-8")

    (tmp_path / "locale" / TL).mkdir(parents=True)
    topo.code_map_path(TL).write_text(json.dumps({
        # geojson 코드 → DB 코드. `09` 는 **일부러 빼 둔다**
        "sido": {"01": "11", "02": "26"},
        "sgg": {"01010": "11110", "01020": "11140", "02010": "26110"},
    }), encoding="utf-8")
    return gd


def _conn(empty: bool = True) -> FakeConn:
    conn = FakeConn(ddl=CITY_DDL + GU_DDL + DONG_DDL)
    conn.counts = {"CM_CITY": 0, "CM_GU": 0, "CM_DONG": 0} if empty else \
        {"CM_CITY": 17, "CM_GU": 273, "CM_DONG": 8079}
    base = conn.answer

    def answer(sql, params):
        if "SELECT COUNT(*) FROM" in sql:
            for t, n in conn.counts.items():
                if f"`{t}`" in sql:
                    return [(n,)]
        if "DELETE FROM" in sql:
            return []
        return base(sql, params)

    conn.answer = answer
    return conn


# --------------------------------------------------------------------------
# 생성
# --------------------------------------------------------------------------


def test_seeds_three_tables_from_an_empty_db(tmp_path, monkeypatch):
    """빈 DB 에서 셋이 만들어지고 **코드를 지어내지 않는다.**

    코드 맵에 없는 폴리곤(`09`·`09010`)은 넣지 않고 수를 보고한다 — 코드를
    지어내면 그 지역 셀이 영원히 조인되지 않는데 에러가 나지 않는다.
    """
    gd = _geo_dir(tmp_path, monkeypatch)
    conn = _conn()
    r = topo.seed_geo(conn, gd, locale=TL, dry_run=False)

    assert (r["city"], r["gu"]) == (2, 3)
    assert (r["skipped_sido"], r["skipped_sgg"]) == (1, 1)
    assert r["emd_source"] == topo.EMD_FROM_POLYGON
    assert r["dong"] == 4

    cities = _inserted_rows(conn, "CM_CITY")
    assert {int(c["CITY_ID"]) for c in cities} == {11, 26}
    gus = _inserted_rows(conn, "CM_GU")
    assert {int(g["GU_ID"]) for g in gus} == {11110, 11140, 26110}
    # 시도는 시군구 코드 앞 2자리다 (상용 실측)
    assert all(int(g["CITY_ID"]) == int(str(g["GU_ID"])[:2]) for g in gus)
    # 상위 이름이 내려와 있어야 한다 — 비면 화면이 코드만 보여준다
    assert all(g["CITY_NAME"] for g in gus)


def test_coordinates_come_from_the_polygon_centroid(tmp_path, monkeypatch):
    """좌표는 **폴리곤 centroid** 다 — 사람이 찍어 넣는 경로가 없다.

    bbox 중심을 쓰면 해안선이 한쪽으로 뻗은 시군구에서 중심이 바다로 나가고,
    그 기준으로 셀을 뿌리면 `seed_enb` 가 만든 eNB 가 물 위에 선다.
    """
    gd = _geo_dir(tmp_path, monkeypatch)
    conn = _conn()
    topo.seed_geo(conn, gd, locale=TL, dry_run=False)

    gu = {int(g["GU_ID"]): g for g in _inserted_rows(conn, "CM_GU")}
    assert float(gu[11110]["LATITUDE"]) == pytest.approx(37.5, abs=1e-6)
    assert float(gu[11110]["LONGITUDE"]) == pytest.approx(127.0, abs=1e-6)
    # **좌표 결손 0** — 하나라도 비면 `gu_points()` 가 그 시군구를 빼고
    # `seed_enb` 가 거기에 장비를 놓지 못한다
    assert all(g["LATITUDE"] is not None and g["LONGITUDE"] is not None
               for g in _inserted_rows(conn, "CM_GU"))
    assert all(d["LATITUDE"] is not None
               for d in _inserted_rows(conn, "CM_DONG"))


def test_column_types_match_each_table(tmp_path, monkeypatch):
    """컬럼 타입이 테이블마다 다르다(실측).

        CM_CITY   varchar(20)  '126.991794'
        CM_GU     float        126.787
        CM_DONG   float

    섞으면 `gu_points()` 의 `float(la)` 가 깨지거나 좌표가 0 이 된다.
    그리고 **DMS 가 아니다** — `CM_CELL_REP_RU_LOC` 만 DMS 문자열이다.
    """
    gd = _geo_dir(tmp_path, monkeypatch)
    conn = _conn()
    topo.seed_geo(conn, gd, locale=TL, dry_run=False)

    for c in _inserted_rows(conn, "CM_CITY"):
        assert isinstance(c["LATITUDE"], str)
        assert ":" not in c["LATITUDE"] and "/" not in c["LATITUDE"]  # DMS 아니다
        float(c["LATITUDE"])
    for g in _inserted_rows(conn, "CM_GU"):
        assert isinstance(g["LATITUDE"], float)
    for d in _inserted_rows(conn, "CM_DONG"):
        assert isinstance(d["LATITUDE"], float)


def test_dong_code_follows_the_production_shape(tmp_path, monkeypatch):
    """`DONG_ID = GU_ID * 1000 + 순번`. 상용 실측과 같은 모양이다
    (`12000` 구의 동이 `12000035`). 자리수를 바꾸면 `int` 컬럼은 받지만
    화면·조인이 상용과 다른 폭을 보게 된다."""
    gd = _geo_dir(tmp_path, monkeypatch)
    conn = _conn()
    topo.seed_geo(conn, gd, locale=TL, dry_run=False)
    # **상용 실측값으로 고정한다.** 상수로만 검증하면 상수를 바꿀 때 테스트가
    # 함께 움직여 아무것도 지키지 못한다 — `12000` 구의 동이 `12000035` 였다.
    assert topo.DONG_CODE_STEP == 1000
    assert 12000 * topo.DONG_CODE_STEP + 35 == 12000035
    for d in _inserted_rows(conn, "CM_DONG"):
        gid, did = int(d["GU_ID"]), int(d["DONG_ID"])
        assert did // topo.DONG_CODE_STEP == gid
        assert 0 < did % topo.DONG_CODE_STEP
        # 5자리 시군구 → 8자리 동. 폭이 바뀌면 화면·조인이 상용과 다른
        # 자리수를 보게 된다
        assert len(str(did)) == len(str(gid)) + 3


# --------------------------------------------------------------------------
# crosswalk — **가장 깨지기 쉬운 자리**
# --------------------------------------------------------------------------


def test_crosswalk_is_rewritten_so_dongs_stay_drawable(tmp_path, monkeypatch):
    """**`seed_geo` 가 자기가 쓴 대응을 그대로 적는다.**

    `CM_DONG` 을 생성하면 상용 동 이름에 맞춰진 `code_crosswalk.json` 과
    매칭이 끊기고, `drawable_dongs` 가 비어 `seed_enb` 의 `DONG_ID` 가
    **에러 없이 전부 NULL** 이 된다. 그래서 생성 직후의 crosswalk 로
    `drawable_dongs` 를 돌려 **생성한 동이 전부 그려지는지** 본다.

    이름 매칭이 아니라 생성의 산물이라 추측이 없다 — graphio `geo_xw` 의
    세 겹 함정(도 약칭 · `수원시장안구` 분리 · 행정동/법정동 체계 차이)이
    여기서는 생기지 않는다.
    """
    gd = _geo_dir(tmp_path, monkeypatch)
    conn = _conn()
    topo.seed_geo(conn, gd, locale=TL, dry_run=False)

    drawable = topo.drawable_dongs(gd / topo.XW_FILE)
    assert drawable, "crosswalk 가 비었다 — seed_enb 의 DONG_ID 가 전부 NULL 이 된다"
    made = {(int(d["CITY_ID"]), int(d["GU_ID"]), int(d["DONG_ID"]))
            for d in _inserted_rows(conn, "CM_DONG")}
    for city, gu, dong in made:
        assert dong in drawable.get((city, gu), []), (city, gu, dong)
    # 생성한 시군구 전부에 그릴 수 있는 동이 있어야 한다 — 하나라도 비면
    # `seed_enb` 가 그 시군구의 셀을 넣지 않는다
    for g in _inserted_rows(conn, "CM_GU"):
        assert drawable.get((int(g["CITY_ID"]), int(g["GU_ID"])))


def test_generated_dongs_still_get_an_identity_crosswalk(tmp_path, monkeypatch):
    """읍면동 폴리곤이 **없을 때**도 맵을 비우지 않는다.

    비우면 `drawable_dongs` 가 비고 셀의 `DONG_ID` 가 조용히 `NULL` 이 된다 —
    **"안 만들어도 되는 상태"와 "없는 상태"는 다른 것이다**(불변규칙 17 의 결).
    그래서 우리가 매긴 코드를 **항등**으로 적고, 출처를 `generated` 로 남긴다.
    """
    gd = _geo_dir(tmp_path, monkeypatch, with_emd=False)
    conn = _conn()
    r = topo.seed_geo(conn, gd, locale=TL, dongs_per_gu=5, dry_run=False)

    assert r["emd_source"] == topo.EMD_GENERATED
    assert r["dong"] == 3 * 5
    doc = json.loads((gd / topo.XW_FILE).read_text(encoding="utf-8"))
    assert doc["map"]["emd"], "emd 맵이 비었다"
    assert "generated" in doc["source"]
    drawable = topo.drawable_dongs(gd / topo.XW_FILE)
    for g in _inserted_rows(conn, "CM_GU"):
        assert len(drawable[(int(g["CITY_ID"]), int(g["GU_ID"]))]) == 5


def test_seed_enb_finds_a_dong_for_every_gu_after_seed_geo(tmp_path, monkeypatch):
    """생성 직후 `seed_enb` 가 **`DONG_ID NULL` 없이** 돈다.

    이것이 ①(`seed-geo`)과 기존 생성(`seed-enb`)을 잇는 자리다. 지금까지
    `seed_enb` 는 상용 `CM_GU`·crosswalk 를 전제로 돌았고, 그 전제가
    생성으로 바뀌어도 성립해야 한다.
    """
    gd = _geo_dir(tmp_path, monkeypatch)
    seeded = _conn()
    topo.seed_geo(seeded, gd, locale=TL, dry_run=False)
    gus = [(int(g["CITY_ID"]), int(g["GU_ID"]), str(g["GU_NAME"]),
            float(g["LATITUDE"]), float(g["LONGITUDE"]))
           for g in _inserted_rows(seeded, "CM_GU")]

    conn = FakeConn(ddl=DONG_DDL + ("ENB_ID", "SECTOR", "LATITUDE", "LONGITUDE",
                                    "RU_NAME", "RU_CUID", "FLAG", "DECODE",
                                    "CELLNUM", "CELL_ID", "C_UID",
                                    "MME_GRP_ID", "FA", "PCI", "ADM_STATE"),
                    gus=gus)
    r = topo.seed_enb(conn, gd / topo.XW_FILE, per_gu=1, cells_per_enb=3,
                      dry_run=False)
    assert r["gus_without_drawable_dong"] == []
    assert r["skipped_no_dong"] == 0
    assert r["cells"] == len(gus) * 3
    rows = _inserted_rows(conn, "CM_CELL_REP_RU_LOC")
    assert rows and all(row["DONG_ID"] is not None for row in rows)


# --------------------------------------------------------------------------
# 재실행 · 안전장치
# --------------------------------------------------------------------------


def test_running_twice_gives_the_same_rows(tmp_path, monkeypatch):
    """두 번 돌려도 같은 결과다 — **전량 교체**이고 결정론이다.

    부분 갱신이면 옛 코드가 남아 어느 세대인지 알 수 없다. 그래서 세
    테이블을 비우고 다시 넣는다(자식부터 비운다).
    """
    gd = _geo_dir(tmp_path, monkeypatch)
    a, b = _conn(), _conn()
    topo.seed_geo(a, gd, locale=TL, dry_run=False)
    topo.seed_geo(b, gd, locale=TL, dry_run=False)
    for t in ("CM_CITY", "CM_GU", "CM_DONG"):
        assert _inserted_rows(a, t) == _inserted_rows(b, t), t
    # 자식부터 비운다 — 세 번 DELETE
    assert sum(1 for q in a.sql if "DELETE FROM" in q) == 3


def test_refuses_to_overwrite_without_replace(tmp_path, monkeypatch):
    """행이 있으면 **거부한다.** 44만 셀이 참조하는 코드를 덮어쓰는 일이라
    사람이 의도를 말해야 한다. 그리고 거부할 때 **아무것도 지우지 않는다** —
    `wipe` 가 먼저라면 "지웠는데 못 넣는" 상태가 된다."""
    gd = _geo_dir(tmp_path, monkeypatch)
    conn = _conn(empty=False)
    with pytest.raises(RuntimeError, match="replace"):
        topo.seed_geo(conn, gd, locale=TL, dry_run=False)
    assert not conn.inserted
    assert not [q for q in conn.sql if "DELETE FROM" in q]
    # `--replace` 를 주면 돈다
    topo.seed_geo(conn, gd, locale=TL, replace=True, dry_run=False)
    assert _inserted_rows(conn, "CM_GU")


def test_dry_run_writes_nothing(tmp_path, monkeypatch):
    """세어 보기만 한다 — crosswalk 파일도 건드리지 않는다."""
    gd = _geo_dir(tmp_path, monkeypatch)
    conn = _conn()
    r = topo.seed_geo(conn, gd, locale=TL, dry_run=True)
    assert r["gu"] == 3
    assert not conn.inserted and not conn.sql
    assert not (gd / topo.XW_FILE).exists()


def test_refuses_to_guess_codes(tmp_path, monkeypatch):
    """코드 맵이 없으면 **멈춘다.**

    코드를 추측으로 매기면 `CM_CELL_REP_RU_LOC` 44만행과 1분 집계가
    참조하는 코드와 어긋나고, 그것은 에러가 아니라 "그 지역에 트래픽이
    없다"로 보인다.
    """
    gd = _geo_dir(tmp_path, monkeypatch)
    topo.code_map_path(TL).unlink()
    with pytest.raises(RuntimeError, match="코드 맵이 없다"):
        topo.seed_geo(conn := _conn(), gd, locale=TL, dry_run=True)
    assert not conn.inserted


def test_refuses_an_unknown_locale(tmp_path, monkeypatch):
    """`GEO_SOURCES` 에 없는 나라는 멈춘다 — 파일 이름을 지어내지 않는다.

    `seed_geo` 는 코드 맵을 먼저 보므로 모르는 나라에서도 **그쪽 사유로**
    멈춘다. 둘 다 "멈춘다"라 순서는 상관없지만, 파일 이름을 지어내지
    않는다는 것은 `read_boundaries` 에서 직접 확인한다.
    """
    gd = _geo_dir(tmp_path, monkeypatch)
    with pytest.raises(RuntimeError, match="GEO_SOURCES"):
        topo.read_boundaries(gd, "없는나라", "sgg")
    with pytest.raises(RuntimeError):      # seed_geo 도 멈춘다
        topo.seed_geo(_conn(), gd, locale="없는나라", dry_run=True)
    # 경계 파일이 없을 때도 멈춘다 — 빈 목록으로 돌려주면 세 테이블이
    # 조용히 0행이 되고, 그러면 `seed_enb` 가 "대상 없음"으로만 보인다
    (gd / topo.GEO_SOURCES[TL]["sgg"]).unlink()
    with pytest.raises(RuntimeError, match="경계 데이터가 없다"):
        topo.read_boundaries(gd, TL, "sgg")


def test_centroid_uses_the_largest_piece(tmp_path, monkeypatch):
    """`MultiPolygon` 은 **가장 넓은 조각**의 중심을 쓴다.

    조각 전체를 면적 가중 평균하면 본토와 먼 섬이 있는 시군구(옹진군 등)에서
    중심이 둘 사이 바다로 간다.
    """
    big = _square(127.0, 37.5, 0.2)["coordinates"]
    small = _square(130.0, 34.0, 0.01)["coordinates"]
    # **양쪽 순서를 다 본다** — 한 순서만 보면 "마지막 조각을 쓴다"는 구현도
    # 통과한다(변이로 실제로 통과했다)
    for order in ([small, big], [big, small]):
        lon, lat = topo.geom_centroid(
            {"type": "MultiPolygon", "coordinates": order})
        assert (lon, lat) == pytest.approx((127.0, 37.5), abs=1e-6), order
    with pytest.raises(ValueError):
        topo.geom_centroid({"type": "Polygon", "coordinates": []})


# --------------------------------------------------------------------------
# 셀 좌표 재배치 — **키를 유지하고 좌표만 다시 쓴다**
# --------------------------------------------------------------------------
#
# 전환의 마지막 조각이다. 코드 맵이 KR 코드를 그대로 쓰므로 `seed-geo
# --locale jp` 뒤에도 `(ENB_ID, SECTOR)`·`GU_ID` 는 유효하고, 그 44만행에
# 들어 있는 **한국 좌표**만 바뀌어야 한다 — 안 바꾸면 시군구는 오사카인데
# 셀은 대전 앞산에 찍히고 **에러가 나지 않는다.**


class _RelocConn(FakeConn):
    """`relocate_cells` 가 던지는 질의 둘에 답하고 `UPDATE` 를 기록한다."""

    def __init__(self, gus, cells):
        super().__init__(ddl=("ENB_ID", "SECTOR", "LATITUDE", "LONGITUDE"),
                         gus=gus)
        self.cells = list(cells)      # (ENB_ID, SECTOR, GU_ID)
        self.updates: list[tuple] = []

    def answer(self, sql, params):
        if "FROM CM_CELL_REP_RU_LOC" in sql and "SELECT l.ENB_ID" in sql:
            return list(self.cells)
        return super().answer(sql, params)


class _RelocCursor:
    pass


def _reloc(gus, cells, **kw):
    conn = _RelocConn(gus, cells)
    stats = topo.relocate_cells(conn, dry_run=False, **kw)
    rows = []
    for sql, data in conn.inserted:
        if "UPDATE CM_CELL_REP_RU_LOC" in sql:
            rows += [tuple(r) for r in data]
    return conn, stats, rows


def test_relocate_only_updates_and_keeps_the_key_set():
    """`UPDATE` 만 한다 — **행을 넣거나 지우지 않는다.**

    `CL_CELL_MAP`·1분 집계·원시 Parquet·`ground_truth.json` 이
    `(ENB_ID, SECTOR)` 를 참조한다. 그 집합이 바뀌면 전환이 아니라 다른
    세대가 된다 — `ensure_sector_coords` 가 이미 지키는 불변이다.
    """
    gus = [(41, 41110, "가", 37.5, 127.0)]
    cells = [("811110", 0, 41110), ("811110", 1, 41110), ("811130", 0, 41110)]
    conn, stats, rows = _reloc(gus, cells)

    assert stats["updated"] == 3 and stats["cells"] == 3
    joined = " ".join(conn.sql).upper()
    assert "INSERT" not in joined and "DELETE" not in joined
    # 갱신 대상 키가 읽은 것과 **정확히 같다**
    assert {(r[2], r[3]) for r in rows} == {(e, s) for e, s, _g in cells}


def test_relocate_moves_cells_to_the_new_gu_centre():
    """좌표가 **그 `GU_ID` 의 현 `CM_GU` 중심** 주변으로 간다.

    이것이 "테이블 스위칭"의 뜻이다 — `CM_GU` 가 일본 좌표로 바뀌면 셀도
    따라간다. `locale` 을 인자로 받는 이유는 같은 커맨드가 KR 재생성에도
    쓰이게 하려는 것이고, 기준은 **그때 DB 에 있는 `CM_GU`** 다.
    """
    from datagen.geo_lite import dms_to_decimal, haversine_km

    gus = [(27, 27110, "오사카", 34.68, 135.52)]      # 일본 좌표
    cells = [("811110", s, 27110) for s in range(3)]
    _conn_, _stats, rows = _reloc(gus, cells, spread_km=0.35)
    for lat_dms, lon_dms, _enb, _sec in rows:
        la, lo = dms_to_decimal(lat_dms), dms_to_decimal(lon_dms)
        assert haversine_km(34.68, 135.52, la, lo) <= 0.35 + 1e-6


def test_relocate_keeps_one_enb_together():
    """한 eNB 의 섹터가 흩어지면 **없는 광역 장애**가 만들어진다.

    섹터가 두 시군구에 걸친 경계 eNB 에서 섹터마다 다른 중심을 쓰면 그
    eNB 가 `POINT` 가 아니라 `SCATTER` 로 보인다 — 그래서 **다수결로 하나**
    를 고른다.
    """
    from datagen.geo_lite import dms_to_decimal, haversine_km

    gus = [(41, 41110, "가", 37.5, 127.0), (41, 41130, "나", 37.9, 127.6)]
    cells = [("811110", 0, 41110), ("811110", 1, 41110), ("811110", 2, 41130)]
    _c, stats, rows = _reloc(gus, cells, spread_km=0.35)
    pts = [(dms_to_decimal(a), dms_to_decimal(o)) for a, o, _e, _s in rows]
    assert topo.pattern_of(pts)[0] == "POINT"
    # 다수결 쪽(41110)으로 갔다 — 두 중심은 40km 떨어져 있다
    assert all(haversine_km(37.5, 127.0, la, lo) <= 0.35 + 1e-6
               for la, lo in pts)
    assert stats["patterns"] == {"POINT": 1}


def test_relocate_leaves_cells_whose_gu_has_no_coordinates():
    """시군구를 모르거나 그 시군구에 좌표가 없으면 **건드리지 않는다.**

    아무 데나 놓으면 미매핑이 "매핑된 거짓 좌표"로 바뀌어 `H-07`(미매핑
    감시)이 그 셀을 놓친다(불변규칙 12). 그리고 그 수를 **보고한다** —
    실측으로 활성 셀 1,357 중 **278개**가 여기 걸렸다. `CM_GU` 의
    `12000`(세종)에 좌표가 없고 **거기가 `ground_truth` 시나리오 자리**다.
    """
    gus = [(41, 41110, "가", 37.5, 127.0)]
    cells = [("811110", 0, 41110),
             ("822222", 0, 99999),      # CM_GU 에 없는 시군구
             ("833333", 0, None)]       # GU_ID 가 NULL
    _c, stats, rows = _reloc(gus, cells)
    assert stats["updated"] == 1 and stats["skipped_no_gu"] == 2
    assert {r[2] for r in rows} == {"811110"}


def test_relocate_is_deterministic_and_dry_run_writes_nothing():
    """같은 시드면 같은 좌표다 — 돌릴 때마다 지도가 움직이면 회귀를
    비교할 수 없다. 그리고 `dry_run` 이 기본이고 그때는 쓰지 않는다."""
    gus = [(41, 41110, "가", 37.5, 127.0)]
    cells = [("811110", s, 41110) for s in range(5)]
    _a, _sa, ra = _reloc(gus, cells)
    _b, _sb, rb = _reloc(gus, cells)
    assert ra == rb

    conn = _RelocConn(gus, cells)
    stats = topo.relocate_cells(conn)          # 기본이 dry_run=True
    assert stats["updated"] == 5 and not conn.inserted
    assert not [q for q in conn.sql if "UPDATE" in q.upper()]


def test_relocate_stops_when_there_is_no_geo_layer():
    """`CM_GU` 에 좌표가 하나도 없으면 멈춘다 — 기준점 없이 뿌리면
    0,0(기니만 바다)이 된다(불변규칙 17)."""
    with pytest.raises(RuntimeError, match="seed-geo"):
        topo.relocate_cells(_RelocConn([], [("811110", 0, 41110)]))


def test_relocate_default_spread_stays_inside_the_point_threshold():
    """기본 분산이 graphio `POINT` 임계 **안쪽**이어야 한다.

        detector.py:1512   if radius < 0.9:   return "POINT"
        detector.py:1533   if radius <= 3.0:  return "CLUSTER"

    올리면 같은 eNB 가 `CLUSTER`·`SCATTER` 로 보여 `geo_hit` 채점이
    흔들린다. **기본값을 인자로 확인한다** — 호출부가 매번 주는 값만 보면
    기본값을 바꿔도 테스트가 통과한다.
    """
    import inspect

    from datagen.geo_lite import dms_to_decimal

    default = inspect.signature(topo.relocate_cells).parameters["spread_km"].default
    assert default == 0.35
    assert default < topo.GEO_POINT_KM           # 0.9
    assert topo.GEO_POINT_KM < topo.GEO_CLUSTER_KM

    gus = [(41, 41110, "가", 37.5, 127.0)]
    cells = [("811110", s, 41110) for s in range(12)]
    _c, _s, rows = _reloc(gus, cells)            # 기본값으로 돈다
    pts = [(dms_to_decimal(a), dms_to_decimal(o)) for a, o, _e, _x in rows]
    pat, radius = topo.pattern_of(pts)
    assert pat == "POINT" and radius < topo.GEO_POINT_KM
