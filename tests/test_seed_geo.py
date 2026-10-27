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

#: `TL` 은 `kr` 이 아니므로 `table_name()` 이 접미사를 붙인다
#: (2026-10-26 방식 변경 — 로케일 축이 테이블 이름으로 내려온다). 테스트가
#: 리터럴 `"CM_CITY"` 를 쓰면 실제 SQL(`CM_CITY_TL`)과 어긋난다.
CITY_TBL = topo.table_name("city", TL)
GU_TBL = topo.table_name("gu", TL)
DONG_TBL = topo.table_name("dong", TL)


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


def _conn(empty: bool = True, locale: str = TL, kr_dongs: dict | None = None) -> FakeConn:
    """`locale` 은 기본이 `TL` 이다 — `test_locale_jp.py` 가 같은 픽스처를
    `locale="jp"` 로 재사용한다. 접미사가 로케일마다 다르므로(`table_name`)
    키를 그때그때 만든다 — `CM_CITY_TL` 로 박아 두면 jp 호출에서 안 맞는다.

    `kr_dongs` — `kr_dong_ids()` 가 읽는 KR `CM_DONG` 의 `{GU_ID: [DONG_ID…]}`.
    기본은 **빈 집합**이다 — 이 테스트들의 `GU_ID`(`11110` 등)는 KR 에 실제로
    대응하는 동 데이터가 없는 합성값이라, 비워 두면 전부 "KR 에 없는 구역"
    분기로 떨어져 **이전과 같은 `dongs_per_gu` 생성 동작**이 유지된다. KR
    재사용 자체(순번 보존)를 보는 테스트는 이 인자로 직접 채운다.

    `seed_geo` 는 생성과 무관하게 `dong_join_check` 로 **셀 벌 존재**를
    먼저 묻는다 — 이 픽스처들은 셀 벌을 다루지 않으므로 "없다"로 답해
    그 진단이 조용히 `None`(산출 불가)에서 끝나게 한다. 응답하지 않으면
    `COUNT(*)` 질의가 이어져 `FakeConn` 미지의 질의로 깨지고, `seed_geo`
    가 그 예외를 진단 실패로 삼켜 겉으로는 다른 단정(`conn.sql`)에서만
    터진다 — 원인을 찾기 어려운 자리다.
    """
    conn = FakeConn(ddl=CITY_DDL + GU_DDL + DONG_DDL)
    city, gu, dong, cell = (topo.table_name(k, locale)
                           for k in ("city", "gu", "dong", "cell"))
    conn.counts = {city: 0, gu: 0, dong: 0} if empty else \
        {city: 17, gu: 273, dong: 8079}
    conn.kr_dongs = dict(kr_dongs or {})
    base = conn.answer

    def answer(sql, params):
        if "SELECT COUNT(*) FROM" in sql:
            for t, n in conn.counts.items():
                if f"`{t}`" in sql:
                    return [(n,)]
        if "DELETE FROM" in sql:
            return []
        if "information_schema.tables" in sql and params and params[0] == cell:
            return []           # 셀 벌 없음 — dong_join_check 가 None 을 낸다
        if "SELECT GU_ID, DONG_ID FROM `CM_DONG`" in sql:
            return [(g, d) for g, ds in sorted(conn.kr_dongs.items())
                    for d in ds]
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

    cities = _inserted_rows(conn, CITY_TBL)
    assert {int(c["CITY_ID"]) for c in cities} == {11, 26}
    gus = _inserted_rows(conn, GU_TBL)
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

    gu = {int(g["GU_ID"]): g for g in _inserted_rows(conn, GU_TBL)}
    assert float(gu[11110]["LATITUDE"]) == pytest.approx(37.5, abs=1e-6)
    assert float(gu[11110]["LONGITUDE"]) == pytest.approx(127.0, abs=1e-6)
    # **좌표 결손 0** — 하나라도 비면 `gu_points()` 가 그 시군구를 빼고
    # `seed_enb` 가 거기에 장비를 놓지 못한다
    assert all(g["LATITUDE"] is not None and g["LONGITUDE"] is not None
               for g in _inserted_rows(conn, GU_TBL))
    assert all(d["LATITUDE"] is not None
               for d in _inserted_rows(conn, DONG_TBL))


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

    for c in _inserted_rows(conn, CITY_TBL):
        assert isinstance(c["LATITUDE"], str)
        assert ":" not in c["LATITUDE"] and "/" not in c["LATITUDE"]  # DMS 아니다
        float(c["LATITUDE"])
    for g in _inserted_rows(conn, GU_TBL):
        assert isinstance(g["LATITUDE"], float)
    for d in _inserted_rows(conn, DONG_TBL):
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
    for d in _inserted_rows(conn, DONG_TBL):
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
            for d in _inserted_rows(conn, DONG_TBL)}
    for city, gu, dong in made:
        assert dong in drawable.get((city, gu), []), (city, gu, dong)
    # 생성한 시군구 전부에 그릴 수 있는 동이 있어야 한다 — 하나라도 비면
    # `seed_enb` 가 그 시군구의 셀을 넣지 않는다
    for g in _inserted_rows(conn, GU_TBL):
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
    for g in _inserted_rows(conn, GU_TBL):
        assert len(drawable[(int(g["CITY_ID"]), int(g["GU_ID"]))]) == 5


def test_kr_matched_gus_reuse_the_kr_dong_ids_gaps_and_all(tmp_path, monkeypatch):
    """**KR 에 대응하는 구는 KR 의 `DONG_ID` 값을 그대로 받는다** —
    순번을 1..n 으로 다시 세지 않는다(2026-10-27).

    셀의 `DONG_ID` 가 KR 체계로 박혀 있어서, 구마다 다른 순번 범위를
    1..n 으로 뭉개면 조인이 깨진다 — 그래서 KR 쪽에 빈 번호가 있어도
    (`1·2·5·9` 처럼) **그 모양까지** 재사용해야 한다. `11110` 만 KR
    대응을 주고 나머지 둘(`11140`·`26110`)은 비워 둬 "KR 에 없는 구역은
    `dongs_per_gu` 를 쓴다"를 같은 호출에서 함께 확인한다.
    """
    gd = _geo_dir(tmp_path, monkeypatch, with_emd=False)
    gaps = [11110001, 11110002, 11110005, 11110009]   # 3·4·6·7·8 이 빠졌다
    conn = _conn(kr_dongs={11110: gaps})
    r = topo.seed_geo(conn, gd, locale=TL, dongs_per_gu=5, dry_run=False)

    assert r["dong_from_kr_gus"] == 1 and r["dong_new_gus"] == 2
    assert r["dong"] == len(gaps) + 5 + 5

    by_gu: dict[int, set[int]] = {}
    for d in _inserted_rows(conn, DONG_TBL):
        by_gu.setdefault(int(d["GU_ID"]), set()).add(int(d["DONG_ID"]))

    # KR 대응 구 — 값이 **정확히 같다**. 1..4 로 뭉개지 않았다
    assert by_gu[11110] == set(gaps)
    # KR 에 없는 구 — `dongs_per_gu` 만큼, `GU_ID*1000 + 1..5`
    assert by_gu[11140] == {11140 * topo.DONG_CODE_STEP + i for i in range(1, 6)}
    assert by_gu[26110] == {26110 * topo.DONG_CODE_STEP + i for i in range(1, 6)}


def test_kr_locale_never_reads_kr_dong_ids(tmp_path, monkeypatch):
    """**`locale == "kr"` 은 KR 자신을 읽지 않는다** — 자기 자신을 읽어
    자기 자신을 다시 만드는 순환이 되고, 한국은 읍면동 폴리곤이 있어
    원래 이 분기(생성)로 오지 않는다. 그래도 번들이 없는 비정상 상태를
    가정해 **종전 동작(균일 생성)이 유지되는지**를 회귀로 고정한다 —
    `kr_dong_ids` 의 질의가 단 한 번도 나가지 않아야 한다.
    """
    names = {"sido": "kr-sido.json", "sgg": "kr-sgg.json"}   # emd 없음
    gd = tmp_path / "geo"
    gd.mkdir()
    monkeypatch.setitem(topo.GEO_SOURCES, "kr", names)
    monkeypatch.setattr(topo, "LOCALE_DIR", tmp_path / "locale")
    (gd / names["sido"]).write_text(json.dumps(_fc([
        ("01", "가도", 127.0, 37.5)])), encoding="utf-8")
    (gd / names["sgg"]).write_text(json.dumps(_fc([
        ("01010", "가시", 127.0, 37.5)])), encoding="utf-8")
    (tmp_path / "locale" / "kr").mkdir(parents=True)
    topo.code_map_path("kr").write_text(json.dumps(
        {"sido": {"01": "11"}, "sgg": {"01010": "11110"}}), encoding="utf-8")

    class _NoKrDongConn(FakeConn):
        """`dry_run=True` 라 `dong_join_check` 의 `table_exists(cell)` 하나만
        답하면 된다 — 셀 벌이 없다고 답해 그 진단을 `None` 으로 끝낸다."""

        def answer(self, sql, params):
            assert "GU_ID, DONG_ID FROM `CM_DONG`" not in sql, \
                "locale=kr 인데 kr_dong_ids 가 KR CM_DONG 을 읽었다"
            if "information_schema.tables" in sql:
                return []
            return super().answer(sql, params)

    conn = _NoKrDongConn(ddl=CITY_DDL + GU_DDL + DONG_DDL)
    r = topo.seed_geo(conn, gd, locale="kr", dongs_per_gu=5, dry_run=True)
    assert r["dong_from_kr_gus"] == 0 and r["dong_new_gus"] == 1
    assert r["dong"] == 5


def test_dong_join_check_is_none_not_zero_when_the_cell_bay_is_missing():
    """**셀 벌이 없으면 `None`**(산출 불가)이다 — `0`(미매핑 없음)이 아니다
    (불변규칙 17). `0` 으로 적으면 "전수 조인된다"로 읽히는데 실제로는
    "셀이 몇 개인지조차 몰라 재지 못했다"다. `planned` 유무(쓰기 전·후)
    양쪽에서 같다."""
    class _NoCellConn(FakeConn):
        def answer(self, sql, params):
            if "information_schema.tables" in sql:
                return []
            return super().answer(sql, params)

    assert topo.dong_join_check(_NoCellConn(), "jp") is None
    assert topo.dong_join_check(_NoCellConn(), "jp", planned=set()) is None


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
           for g in _inserted_rows(seeded, GU_TBL)]

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
    for t in (CITY_TBL, GU_TBL, DONG_TBL):
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
    assert _inserted_rows(conn, GU_TBL)


def test_dry_run_writes_nothing(tmp_path, monkeypatch):
    """세어 보기만 한다 — crosswalk 파일도 건드리지 않는다.

    **읽기는 한다 · 쓰지 않는다** (2026-10-27). `dong_join` 진단
    (`dong_join_check`)이 쓰기 전에 "넣으면 몇 개가 빌 것인가"를 알아야
    적재 판단이 서므로, `dry_run=True` 도 이제 DB 를 **읉는다** — 그래서
    `conn.sql` 이 비어 있다고 단정하지 않는다. 좁혀야 하는 것은 **쓰기가
    없다**는 사실이다: `conn.inserted` 뿐 아니라 로그된 SQL 에
    INSERT·DELETE·UPDATE 가 전혀 없는지까지 본다 — 이 구분이 흐려지면
    다음 사람이 세어 보기 단계(읽기)까지 막아 버릴 수 있다.
    """
    gd = _geo_dir(tmp_path, monkeypatch)
    conn = _conn()
    r = topo.seed_geo(conn, gd, locale=TL, dry_run=True)
    assert r["gu"] == 3
    assert not conn.inserted
    assert not any(q.upper().startswith(("INSERT", "DELETE", "UPDATE"))
                   for q in conn.sql)
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
    """`relocate_cells` 가 던지는 질의에 답하고 `UPDATE` 를 기록한다.

    `CL_ENB_ORG` 는 **전수**가 대상이라(이름 마스터) 셀 목록과 따로 받는다.
    """

    def __init__(self, gus, cells, org=None, has_org=True, scoped=None):
        super().__init__(ddl=("ENB_ID", "SECTOR", "LATITUDE", "LONGITUDE"),
                         gus=gus)
        self.cells = list(cells)      # (ENB_ID, SECTOR, GU_ID) — 전수
        #: `only_active` 로 좁힌 결과. `None` 이면 전수와 같다. 전수와
        #: 가를 수 있어야 "조직 이름은 전수"를 검증할 수 있다.
        self.scoped = list(scoped) if scoped is not None else None
        self.org = list(org) if org is not None else \
            sorted({(e,) for e, _s, _g in self.cells})
        self.has_org = has_org

    def answer(self, sql, params):
        if "SELECT ENB_ID, GU_ID FROM `CM_CELL_REP_RU_LOC`" in sql:
            return [(e, g) for e, _s, g in self.cells]
        if "FROM `CM_CELL_REP_RU_LOC`" in sql and "SELECT l.ENB_ID" in sql:
            return list(self.cells if self.scoped is None else self.scoped)
        if "SELECT COUNT(*) FROM `CM_CELL_REP_RU_LOC`" in sql:
            return [(len(self.cells),)]
        if "information_schema.tables" in sql:
            # `relocate_cells` 가 이 질의를 **둘**에 쓴다 — 셀 벌 자체와
            # `CL_ENB_ORG`. `has_org` 는 후자만 가린다 — 하나로 뭉치면
            # `has_org=False` 가 셀 벌까지 "없다"로 보여 좌표 갱신도
            # 막힌다(`test_relocate_works_without_the_org_table` 가 그
            # 구분을 요구한다).
            # `table_exists()` 는 `bool(_rows(...))` 로 판정한다 — 행이
            # 있는지만 보고 **값은 보지 않는다.** `[(0,)]` 을 돌려주면
            # "없다"가 아니라 "행 하나 있다"로 읽혀 늘 참이 된다.
            tbl = params[0] if params else None
            if tbl == "CL_ENB_ORG":
                return [(1,)] if self.has_org else []
            return [(1,)]
        if "SELECT ENB_ID FROM `CL_ENB_ORG`" in sql:
            return list(self.org)
        return super().answer(sql, params)


def _reloc(gus, cells, conn=None, **kw):
    """`(conn, stats, 셀 UPDATE 행)`. 행은 `(lat, lon, ru_name, enb, sector)`."""
    conn = conn or _RelocConn(gus, cells)
    stats = topo.relocate_cells(conn, dry_run=False, **kw)
    rows = []
    for sql, data in conn.inserted:
        if "UPDATE `CM_CELL_REP_RU_LOC`" in sql:
            rows += [tuple(r) for r in data]
    return conn, stats, rows


def _org_updates(conn) -> list[tuple]:
    out = []
    for sql, data in conn.inserted:
        if "UPDATE `CL_ENB_ORG`" in sql:
            out += [tuple(r) for r in data]
    return out


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
    assert {(r[3], r[4]) for r in rows} == {(e, s) for e, s, _g in cells}


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
    for lat_dms, lon_dms, _nm, _enb, _sec in rows:
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
    pts = [(dms_to_decimal(a), dms_to_decimal(o)) for a, o, _n, _e, _s in rows]
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
    assert {r[3] for r in rows} == {"811110"}


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
    pts = [(dms_to_decimal(a), dms_to_decimal(o)) for a, o, _n, _e, _x in rows]
    pat, radius = topo.pattern_of(pts)
    assert pat == "POINT" and radius < topo.GEO_POINT_KM


# --------------------------------------------------------------------------
# 이름도 함께 바꾼다 — 안 바꾸면 **지도는 오사카인데 이름은 한국**이다
# --------------------------------------------------------------------------


def test_names_follow_the_gu_name_in_that_schema():
    """`RU_NAME`·`BTS_NAME` 이 **그 DB 의 `CM_GU` 이름**을 쓴다.

    번역이 아니라 생성이다 — `CM_GU` 가 이미 그 나라 이름을 들고 있고
    (`seed-geo` 가 폴리곤에서 받는다), 사용자 방침이 *"짧게 간결하게"* 다.
    locale 인자를 보지 않으므로 **KR 재생성에도 같은 커맨드가 쓰인다.**
    """
    gus = [(27, 27110, "大阪市中央区", 34.68, 135.52)]
    cells = [("811110", s, 27110) for s in range(3)]
    conn, stats, rows = _reloc(gus, cells)

    names = sorted(r[2] for r in rows)
    assert names == ["大阪市中央区_1-0", "大阪市中央区_1-1", "大阪市中央区_1-2"]
    assert _org_updates(conn) == [("大阪市中央区_1", "811110")]
    assert stats["renamed_cells"] == 3 and stats["renamed_enbs"] == 1
    # 한국 이름이 하나도 남지 않았다
    assert not any("동" in n or "[L9]" in n for n in names)
    # **SQL 에 그 컬럼이 들어 있어야 한다.** 인자만 보면 컬럼을 빼도
    # 통과한다(변이로 실제로 통과했다) — 그때 이름은 조용히 안 바뀐다.
    cell_sql = [q for q in conn.sql if "UPDATE `CM_CELL_REP_RU_LOC`" in q]
    assert cell_sql and "RU_NAME = %s" in cell_sql[0]
    assert "UPDATE `CL_ENB_ORG` SET BTS_NAME = %s" in " ".join(conn.sql)


def test_cell_and_enb_names_share_the_enb_prefix():
    """한 eNB 의 섹터가 **같은 접두**를 갖고, eNB 이름과도 묶인다.

    화면에서 셀과 eNB 가 따로 보이면 "이 셀이 어느 장비인가"를 사람이
    맞춰야 한다. `BTS_NAME` 과 `RU_NAME` 의 접두를 같게 두면 그 일이 없다.
    """
    gus = [(41, 41110, "가", 37.5, 127.0), (41, 41130, "나", 37.9, 127.6)]
    cells = [("811110", 0, 41110), ("811110", 1, 41110),
             ("811130", 0, 41130), ("811130", 1, 41130)]
    conn, _s, rows = _reloc(gus, cells)

    by_enb: dict[str, set[str]] = {}
    for _la, _lo, name, enb, _sec in rows:
        by_enb.setdefault(enb, set()).add(name.rsplit("-", 1)[0])
    assert all(len(v) == 1 for v in by_enb.values()), by_enb
    org = dict((e, n) for n, e in _org_updates(conn))
    for enb, prefixes in by_enb.items():
        assert org[enb] == next(iter(prefixes)), (enb, org[enb], prefixes)


def test_serial_numbers_are_deterministic():
    """연번이 `ENB_ID` 정렬 순이다 — **난수면 돌릴 때마다 이름이 바뀐다.**

    그러면 회귀를 비교할 수 없고 화면 캡처도 매번 달라진다. 그리고 같은
    시군구 안에서 **중복되지 않아야** 한다.
    """
    gus = [(41, 41110, "가", 37.5, 127.0)]
    cells = [(f"8111{i:02d}", s, 41110) for i in range(5) for s in range(2)]
    a_conn, _sa, ra = _reloc(gus, cells)
    _b_conn, _sb, rb = _reloc(gus, cells)
    assert ra == rb

    org = dict((e, n) for n, e in _org_updates(a_conn))
    nos = [int(org[f"8111{i:02d}"].rsplit("_", 1)[1]) for i in range(5)]
    assert nos == [1, 2, 3, 4, 5]               # 정렬 순
    assert len(set(org.values())) == len(org)   # 중복 없음


def test_enb_names_cover_the_whole_org_table():
    """`CL_ENB_ORG` 는 **전수**다 — 활성 셀로 좁히지 않는다.

    이름 마스터를 반만 고치면 화면에 두 나라가 섞인다. 그쪽은 좌표가
    없어 범위를 좁힐 이유도 없다. 좁힌 만큼 **남는 셀 수를 보고한다.**
    """
    gus = [(41, 41110, "가", 37.5, 127.0)]
    full = [("811110", 0, 41110), ("899990", 0, 41110)]
    # `899990` 은 셀이 있지만 **범위 밖**(활성이 아니다)이다. 조직 이름은
    # 전수라 그쪽도 바뀌어야 한다 — 좌표는 안 바뀐다.
    conn = _RelocConn(gus, full, scoped=[("811110", 0, 41110)],
                      org=[("811110",), ("899990",), ("822220",)])
    _c, stats, rows = _reloc(gus, full, conn=conn)

    assert {r[3] for r in rows} == {"811110"}          # 좌표는 범위 안만
    org = {e for _n, e in _org_updates(conn)}
    assert org == {"811110", "899990"}, org            # 이름은 전수
    assert stats["renamed_enbs"] == 2 and stats["renamed_cells"] == 1
    # `822220` 은 셀이 없어 시군구를 모른다 → 건드리지 않는다
    assert "822220" not in org
    assert stats["cells_out_of_scope"] == 1


def test_org_map_is_left_alone():
    """**`CL_ENB_ORG_MAP` 을 건드리지 않는다.**

    `CL_ENB_ORG`(45,483) 의 6.4%(2,904행)뿐이라 전수 미달이고
    (graphio `store/tables.py:249` 가 그것을 경고한다) 관례상 이름·권역은
    `CL_ENB_ORG` 를 본다. `LATITUDE_NORM` 은 전 행 NULL 이라 좌표 소비처도
    아니다 — 고치면 **두 테이블이 서로 다른 이름을 갖게** 된다.
    """
    gus = [(41, 41110, "가", 37.5, 127.0)]
    conn, _s, _r = _reloc(gus, [("811110", 0, 41110)])
    joined = " ".join(conn.sql).upper()
    assert "CL_ENB_ORG_MAP" not in joined
    assert "`CL_ENB_ORG`" in " ".join(conn.sql)


def test_relocate_works_without_the_org_table():
    """`CL_ENB_ORG` 가 없는 스키마에서도 돈다 — 좌표·`RU_NAME` 은 바뀐다.

    빈 JP 스키마를 `clone` 전에 만지는 경우가 있고, 그때 `1146 table
    doesn't exist` 로 죽으면 "좌표도 못 바꿨다"로 읽힌다.
    """
    gus = [(41, 41110, "가", 37.5, 127.0)]
    conn = _RelocConn(gus, [("811110", 0, 41110)], has_org=False)
    _c, stats, rows = _reloc(gus, [("811110", 0, 41110)], conn=conn)
    assert stats["renamed_enbs"] == 0 and stats["renamed_cells"] == 1
    assert rows and not _org_updates(conn)
