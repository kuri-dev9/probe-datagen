"""일본 locale — **코드는 한국 것을 그대로 쓴다.** 깨면 스위칭이 성립하지 않는다.

사용자 방침은 *"동일한 ID 체계로 가고 테이블 스위칭으로 전환"* 이다. 그래서
JIS 코드는 **선정에만** 쓰고 DB 에는 한국 코드가 들어간다 — 트래픽
(`CM_CELL_REP_RU_LOC` 44만행 · 1분 집계)이 그 코드를 참조하므로 그대로
남아야 전환이 성립한다.

**DB 와 번들 파일을 쓰지 않는다**(집안 규칙). 받은 번들은 `.gitignore` 안이라
환경마다 있고 없고, 그것에 의존하면 테스트가 조용히 건너뛰어진다 — 작은
합성 feature 로 같은 성질을 단정한다.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from datagen import topo

#: KR 시도별 시군구 수 — `CM_GU` 실측(2026-10-26 · 합계 273).
#: **세종이 `12`, 제주가 `49`** 다. 행정표준코드(36·50)가 아니다.
KR_SHAPE = {11: 25, 12: 1, 26: 16, 27: 9, 28: 10, 29: 5, 30: 5, 31: 5,
            41: 52, 42: 18, 43: 16, 44: 18, 45: 16, 46: 22, 47: 24, 48: 27,
            49: 4}


def _kr_gu() -> list[tuple[int, int]]:
    """`(CITY_ID, GU_ID)` — 상용과 같은 모양(`41110` = 시도 2자리 + 3자리)."""
    out = []
    for city, n in sorted(KR_SHAPE.items()):
        for i in range(n):
            out.append((city, city * 1000 + 110 + i * 10))
    return out


def _jp_sgg(tmp_path: Path, monkeypatch, *, counts: dict[str, int] | None = None,
            broken: bool = False) -> Path:
    """합성 시구정촌 GeoJSON. `counts` 는 도도부현명 → feature 수.

    기본은 `SIDO_MAP_JP` 의 17곳에 **필요한 수의 두 배**를 주고, 쓰지 않는
    도도부현 하나를 섞는다 — "묶지 않는다"를 확인할 대상이다.
    """
    need = {topo.SIDO_MAP_JP[str(c)]: n for c, n in KR_SHAPE.items()}
    counts = counts or {p: n * 2 for p, n in need.items()}
    counts = dict(counts)
    counts.setdefault("青森県", 5)        # 대응표에 없는 곳
    pref_code = {p: f"{i + 1:02d}" for i, p in enumerate(sorted(counts))}

    feats = []
    for pref, n in sorted(counts.items()):
        for i in range(n):
            # 절반은 정령시의 区(`N03_004` 있음 = 도심), 나머지는 市·町
            ward = i % 2 == 0
            props = {
                "N03_001": pref,
                "N03_003": f"{pref[:-1]}市" if ward or i % 3 else None,
                "N03_004": f"第{i}区" if ward else None,
                "N03_007": f"{pref_code[pref]}{200 + i:03d}",
            }
            if not ward and not props["N03_003"]:
                props["N03_003"] = f"{pref[:-1]}{i}町"
            feats.append({
                "type": "Feature", "properties": props,
                "geometry": {"type": "Polygon", "coordinates": [[
                    [135.0 + i * 0.1, 35.0], [135.1 + i * 0.1, 35.0],
                    [135.1 + i * 0.1, 35.1], [135.0 + i * 0.1, 35.1],
                    [135.0 + i * 0.1, 35.0]]]},
            })
    if broken:
        # 경계 미정 — 코드가 없다. **버려야 한다**
        feats.append({"type": "Feature",
                      "properties": {"N03_001": "東京都", "N03_003": "所属未定地",
                                     "N03_004": None, "N03_007": None},
                      "geometry": feats[0]["geometry"]})

    gd = tmp_path / "geo"
    monkeypatch.setattr(topo, "LOCALE_DIR", tmp_path / "locale")
    d = gd / topo.GEO_SOURCES["jp"]["_dir"]
    d.mkdir(parents=True, exist_ok=True)
    (d / topo.GEO_SOURCES["jp"]["sgg"]).write_text(
        json.dumps({"type": "FeatureCollection", "features": feats}),
        encoding="utf-8")
    return gd


# --------------------------------------------------------------------------
# 시도 대응표 — 사람이 정한 17줄
# --------------------------------------------------------------------------


def test_sido_map_covers_every_kr_sido_with_the_db_codes():
    """17개 전부 있고 **키가 DB 실측 코드**다.

    세종은 `36` 이 아니라 **`12`**, 제주는 `50` 이 아니라 **`49`** 다.
    상용 반출본이 그렇게 왔고 트래픽이 그 코드를 참조하므로, 표준코드로
    "고치면" 그 두 시도가 `build_jp_code_map` 에서 빠진다(실제로 처음에
    `36`·`50` 으로 적어 `KR 시도 12 의 일본 대응이 없다`로 멈췄다).
    """
    assert set(topo.SIDO_MAP_JP) == {str(c) for c in KR_SHAPE}
    assert topo.SIDO_MAP_JP["12"] and topo.SIDO_MAP_JP["49"]
    assert "36" not in topo.SIDO_MAP_JP and "50" not in topo.SIDO_MAP_JP
    # 한 도도부현을 두 시도에 쓰지 않는다 — 떨어진 두 지역이 섞인다
    assert len(set(topo.SIDO_MAP_JP.values())) == len(topo.SIDO_MAP_JP)


def test_user_specified_pairs_are_kept():
    """사용자 지정 둘은 **인구 순위로 바뀌지 않는다.**

    인구 순위 자동 대응이면 KR 3위 부산 → JP 3위 大阪(지정은 福岡),
    KR 13위 대전 → JP 13위 京都(지정은 大阪)가 된다. 그래서 상수다 —
    나중에 "인구 순위로 바꾸자"가 나올 때 이것이 결정이었음을 고정한다.
    """
    assert topo.SIDO_MAP_JP["26"] == "福岡県"      # 부산
    assert topo.SIDO_MAP_JP["30"] == "大阪府"      # 대전


def test_rank_uses_the_suffix_as_the_urban_signal():
    """이 파일에 **인구가 없다.** 접미사가 도시 위계 신호다 —
    정령시의 区(0) → 市(1) → 町·村(2)."""
    assert topo._jp_rank("大阪市中央区", True) == 0
    assert topo._jp_rank("大津市", False) == 1
    assert topo._jp_rank("〇〇町", False) == 2
    assert topo._jp_rank("〇〇村", False) == 2


# --------------------------------------------------------------------------
# 코드 맵
# --------------------------------------------------------------------------


def test_code_map_fills_every_kr_sgg_in_the_kr_code_space(tmp_path, monkeypatch):
    """**273개가 17개 시도에 KR 과 같은 분포로 채워진다.**

    그리고 값이 전부 **KR 코드 공간 안**이어야 한다 — JIS 가 DB 로 새면
    트래픽이 가리키는 코드와 어긋나 그 지역이 조용히 미매핑이 된다.
    """
    gd = _jp_sgg(tmp_path, monkeypatch)
    doc = topo.build_jp_code_map(gd, _kr_gu())

    kr_ids = {gu for _c, gu in _kr_gu()}
    assert {int(v) for v in doc["sgg"].values()} == kr_ids
    assert len(doc["sgg"]) == 273
    # 시도별 수가 KR 과 같다
    got = {c: len(rows) for c, rows in doc["picked"].items()}
    assert got == KR_SHAPE
    # 시도 맵의 값도 KR 코드다
    assert {int(v) for v in doc["sido"].values()} == set(KR_SHAPE)
    # JIS 는 키 쪽에만 있다 — 값으로 새지 않는다
    assert all(len(k) == 5 and k.isdigit() for k in doc["sgg"])


def test_code_map_prefers_the_city_wards(tmp_path, monkeypatch):
    """도심(정령시의 区)이 먼저 배정된다 — 한국 273도 구 위주다."""
    gd = _jp_sgg(tmp_path, monkeypatch)
    doc = topo.build_jp_code_map(gd, _kr_gu())
    seoul = doc["picked"][11]
    assert all("区" in r["name"] for r in seoul[:5]), [r["name"] for r in seoul[:5]]


def test_unused_prefectures_are_not_folded_in(tmp_path, monkeypatch):
    """**쓰지 않는 도도부현을 묶지 않는다.**

    떨어진 두 지역이 한 시도 코드에 들어가면 지리 롤업이 하위 평균이 아니라
    원시 재집계라서(불변규칙 2) `geo_pattern` 반경이 거짓으로 커지고
    `WIDE` 가 상시로 난다. 그래서 목록으로만 남긴다 — 회색 배경으로 그릴
    자리다(불변규칙 12 의 결).
    """
    gd = _jp_sgg(tmp_path, monkeypatch)
    doc = topo.build_jp_code_map(gd, _kr_gu())
    assert "青森県" in doc["unused_prefectures"]
    # 그 현의 시구정촌이 어느 시도에도 들어가지 않았다
    used = {r["jis"] for rows in doc["picked"].values() for r in rows}
    aomori = [f["properties"]["N03_007"] for f in json.loads(
        topo.geo_level_path(gd, "jp", "sgg").read_text(encoding="utf-8")
    )["features"] if f["properties"]["N03_001"] == "青森県"]
    assert aomori and not (set(aomori) & used)


def test_code_map_stops_when_supply_is_short(tmp_path, monkeypatch):
    """공급이 모자라면 **멈춘다.**

    부족분을 다른 도도부현에서 끌어오면 위와 같은 거짓 반경이 생긴다 —
    표를 고치라고 말하는 쪽이 맞다.
    """
    need = {topo.SIDO_MAP_JP[str(c)]: n for c, n in KR_SHAPE.items()}
    short = dict(need)
    short["北海道"] = 10                      # 경기 52 를 못 채운다
    gd = _jp_sgg(tmp_path, monkeypatch, counts=short)
    with pytest.raises(RuntimeError, match="北海道"):
        topo.build_jp_code_map(gd, _kr_gu())


def test_features_without_a_code_are_dropped(tmp_path, monkeypatch):
    """코드 없는 feature(경계 미정 · 실측 3건)는 **버린다.**

    넣으면 코드 없는 시군구가 생기고 그 지역 셀은 영원히 조인되지 않는다.
    `read_boundaries` 와 `build_jp_code_map` 이 **같은 판정**이어야 한다 —
    한쪽만 버리면 둘이 다른 집합을 본다.
    """
    gd = _jp_sgg(tmp_path, monkeypatch, broken=True)
    got = topo.read_boundaries(gd, "jp", "sgg")
    assert all(g["code"] and g["name"] for g in got)
    assert not any("未定" in g["name"] for g in got)
    doc = topo.build_jp_code_map(gd, _kr_gu())
    assert not any("未定" in r["name"] for rows in doc["picked"].values()
                   for r in rows)


def test_names_join_both_property_keys(tmp_path, monkeypatch):
    """이름은 `(N03_003 or '') + (N03_004 or '')` 다.

    도쿄 23구는 `N03_003=None`(구 이름만), 일반 시는 `N03_004=None`(시
    이름만)이라 **한쪽만 보면 빈 이름이 나온다.** 이름 없는 시군구는 화면에서
    코드로만 보이고, 그것은 "데이터가 왔는데 이름이 없다"로 읽힌다.
    """
    assert topo._props_jp(
        {"N03_003": None, "N03_004": "千代田区", "N03_007": "13101"},
        "sgg") == ("13101", "千代田区")
    assert topo._props_jp(
        {"N03_003": "大津市", "N03_004": None, "N03_007": "25201"},
        "sgg") == ("25201", "大津市")
    assert topo._props_jp(
        {"N03_003": "札幌市", "N03_004": "中央区", "N03_007": "01101"},
        "sgg") == ("01101", "札幌市中央区")


def test_prefecture_codes_are_derived_from_the_municipalities(tmp_path, monkeypatch):
    """`prefectures.json` 에 **코드가 없다**(`N03_001` 하나뿐) — 시구정촌의
    `N03_007[:2]` 에서 역산한다. 역산할 파일이 없으면 멈춘다."""
    gd = _jp_sgg(tmp_path, monkeypatch)
    sgg = json.loads(topo.geo_level_path(gd, "jp", "sgg").read_text(encoding="utf-8"))
    prefs = sorted({f["properties"]["N03_001"] for f in sgg["features"]})
    topo.geo_level_path(gd, "jp", "sido").write_text(json.dumps({
        "type": "FeatureCollection",
        "features": [{"type": "Feature", "properties": {"N03_001": p},
                      "geometry": sgg["features"][0]["geometry"]} for p in prefs],
    }), encoding="utf-8")

    got = {g["name"]: g["code"] for g in topo.read_boundaries(gd, "jp", "sido")}
    assert set(got) == set(prefs)
    assert all(len(c) == 2 and c.isdigit() for c in got.values())
    # 시구정촌 파일이 없으면 멈춘다 — 코드를 지어내지 않는다
    topo.geo_level_path(gd, "jp", "sgg").unlink()
    with pytest.raises(RuntimeError, match="역산할"):
        topo.read_boundaries(gd, "jp", "sido")


# --------------------------------------------------------------------------
# `seed-geo --locale jp`
# --------------------------------------------------------------------------


def test_seed_geo_runs_for_jp_on_an_empty_db(tmp_path, monkeypatch):
    """빈 DB 에서 돌고, **읍면동은 받지 않고 생성한다.**

    町丁・字는 e-Stat 에 있지만 수십만 건이고 사용자 방침이 시군구급이다.
    그래서 `emd` 레벨이 `GEO_SOURCES["jp"]` 에 **없고**, 그때 crosswalk 의
    emd 는 **항등**이어야 한다 — 비우면 `drawable_dongs` 가 비고 셀의
    `DONG_ID` 가 조용히 `NULL` 이 된다.
    """
    from tests.test_seed_geo import CITY_DDL, DONG_DDL, GU_DDL, _conn

    gd = _jp_sgg(tmp_path, monkeypatch)
    sgg = json.loads(topo.geo_level_path(gd, "jp", "sgg").read_text(encoding="utf-8"))
    prefs = sorted({f["properties"]["N03_001"] for f in sgg["features"]})
    topo.geo_level_path(gd, "jp", "sido").write_text(json.dumps({
        "type": "FeatureCollection",
        "features": [{"type": "Feature", "properties": {"N03_001": p},
                      "geometry": sgg["features"][0]["geometry"]} for p in prefs],
    }), encoding="utf-8")
    topo.save_code_map(topo.build_jp_code_map(gd, _kr_gu()), "jp")

    conn = _conn()
    conn.ddl = set(CITY_DDL + GU_DDL + DONG_DDL)
    r = topo.seed_geo(conn, gd, locale="jp", dongs_per_gu=8, dry_run=False)

    assert (r["city"], r["gu"]) == (17, 273)
    assert r["dong"] == 273 * 8
    assert r["emd_source"] == topo.EMD_GENERATED
    # 쓰지 않는 도도부현은 **제외로 보고된다** — 조용히 사라지지 않는다
    assert r["skipped_sido"] >= 1 and r["skipped_sgg"] >= 1
    # **crosswalk 도 `jp/` 아래다** — 루트가 아니다(아래 분리 테스트 참조)
    drawable = topo.drawable_dongs(topo.crosswalk_path(gd, "jp"))
    assert len(drawable) == 273
    assert all(len(v) == 8 for v in drawable.values())


def test_jp_bundle_is_a_separate_directory_from_kr():
    """**한국 번들을 옮기지 않는다.** graphio `geo.py`·`/api/geojson` 이
    `geo_dir` 바로 아래를 읽으므로 하위로 옮기면 지도가 503 이 된다.
    일본만 `jp/` 로 가른다."""
    assert topo.GEO_SOURCES["kr"]["_dir"] == "."
    assert topo.GEO_SOURCES["jp"]["_dir"] == "jp"
    gd = Path("/tmp/geo")
    assert topo.geo_level_path(gd, "kr", "sgg").parent == gd
    assert topo.geo_level_path(gd, "jp", "sgg").parent == gd / "jp"


def test_credit_travels_with_the_files():
    """출처가 **파일과 함께 간다.** 번들 디렉터리가 `.gitignore` 안이라
    저장소에만 적어 두면 반출본에서 사라진다 — `fetch_boundaries` 가
    `SOURCE.txt` 를 쓴다. 원 출처는 국토교통성 국토수치정보(CC BY 4.0)다."""
    cr = topo.GEO_CREDIT["jp"]
    assert "국토수치정보" in cr["credit"] and "CC BY 4.0" in cr["credit"]
    assert "smartnews-smri" in cr["credit"]
    assert cr["base"].startswith("https://")
    # 한국은 받는 자리가 없다 — graphio 가 받는다
    assert "kr" not in topo.GEO_CREDIT


def test_code_map_stops_when_a_kr_sido_has_no_pair(tmp_path, monkeypatch):
    """대응표에 없는 KR 시도는 **멈춘다 — 아무 현이나 끌어오지 않는다.**

    끌어오면 이미 다른 시도가 쓰는 현이거나 떨어진 지역이 되고, 그 어느
    쪽이든 한 시도 코드 아래에 두 지역이 섞여 `geo_pattern` 반경이 거짓으로
    커진다. 표를 고치라고 말하는 쪽이 맞다.
    """
    gd = _jp_sgg(tmp_path, monkeypatch)
    extra = _kr_gu() + [(99, 99110)]        # 대응표에 없는 시도
    with pytest.raises(RuntimeError, match="SIDO_MAP_JP"):
        topo.build_jp_code_map(gd, extra)


def test_duplicate_codes_keep_the_larger_polygon(tmp_path, monkeypatch):
    """같은 코드가 두 폴리곤이면 **큰 쪽**을 쓴다.

    받은 판본에서는 **0건**이다(고유 1,894 + 무코드 3 = 1,897). 그런데
    다음 판본에서 섬이 따로 떨어지면 생기고, 그때 뒤의 것이 앞을 덮으면
    **중심이 섬으로 간다** — 그 좌표로 셀을 뿌리면 eNB 가 바다에 선다.
    """
    def sq(lon, lat, d):
        return {"type": "Polygon", "coordinates": [[
            [lon - d, lat - d], [lon + d, lat - d], [lon + d, lat + d],
            [lon - d, lat + d], [lon - d, lat - d]]]}

    gd = _jp_sgg(tmp_path, monkeypatch)
    path = topo.geo_level_path(gd, "jp", "sgg")
    doc = json.loads(path.read_text(encoding="utf-8"))
    props = {"N03_001": "東京都", "N03_003": "島市", "N03_004": None,
             "N03_007": "13999"}
    # 본토(큰 것)를 **먼저**, 섬(작은 것)을 나중에 — 뒤를 쓰면 섬이 된다
    doc["features"] += [
        {"type": "Feature", "properties": props, "geometry": sq(139.7, 35.7, 0.3)},
        {"type": "Feature", "properties": props, "geometry": sq(142.0, 27.0, 0.01)},
    ]
    path.write_text(json.dumps(doc), encoding="utf-8")

    got = {g["code"]: g for g in topo.read_boundaries(gd, "jp", "sgg")}
    assert got["13999"]["lat"] == pytest.approx(35.7, abs=1e-6)
    assert got["13999"]["lon"] == pytest.approx(139.7, abs=1e-6)


def test_seeding_jp_does_not_touch_the_kr_crosswalk(tmp_path, monkeypatch):
    """**`--locale jp` 가 KR 의 crosswalk 를 덮지 않는다.**

    경계 파일은 locale 로 갈라 뒀는데 crosswalk 출력이 루트 한 곳이었다.
    그 상태로 일본을 만들면 `data/geo/code_crosswalk.json` 이 일본 대응이
    되고, graphio 는 **한국 폴리곤 + 일본 crosswalk** 를 보게 된다 →
    `remap` 전량 unresolved → **지도 전체 회색**. 에러가 나지 않는다.

    게다가 `restore kr` 은 DB 네 테이블만 되돌리고 `data/geo/` 를 보지
    않으므로 **복구되지도 않는다.** 그래서 경로를 가른다.
    """
    from tests.test_seed_geo import CITY_DDL, DONG_DDL, GU_DDL, _conn

    gd = _jp_sgg(tmp_path, monkeypatch)
    sgg = json.loads(topo.geo_level_path(gd, "jp", "sgg").read_text(encoding="utf-8"))
    prefs = sorted({f["properties"]["N03_001"] for f in sgg["features"]})
    topo.geo_level_path(gd, "jp", "sido").write_text(json.dumps({
        "type": "FeatureCollection",
        "features": [{"type": "Feature", "properties": {"N03_001": p},
                      "geometry": sgg["features"][0]["geometry"]} for p in prefs],
    }), encoding="utf-8")
    topo.save_code_map(topo.build_jp_code_map(gd, _kr_gu()), "jp")

    # 한국 crosswalk 가 이미 있다고 두고 — 내용을 알아볼 수 있게 표식을 넣는다
    kr_xw = topo.crosswalk_path(gd, "kr")
    kr_before = json.dumps({"source": "KR", "map": {"sido": {"11": "11"}}},
                           ensure_ascii=False)
    kr_xw.write_text(kr_before, encoding="utf-8")

    conn = _conn()
    conn.ddl = set(CITY_DDL + GU_DDL + DONG_DDL)
    topo.seed_geo(conn, gd, locale="jp", dongs_per_gu=4, dry_run=False)

    assert kr_xw.read_text(encoding="utf-8") == kr_before, "KR crosswalk 가 덮였다"
    jp_xw = topo.crosswalk_path(gd, "jp")
    assert jp_xw != kr_xw and jp_xw.exists()
    assert "locale=jp" in json.loads(jp_xw.read_text(encoding="utf-8"))["source"]


def test_crosswalk_path_follows_the_boundary_path_rule():
    """crosswalk 와 경계 파일이 **같은 디렉터리 규칙**을 쓴다.

    `_dir` 기본값이 어긋나면 둘이 다른 디렉터리에 떨어지고, 그러면
    `drawable_dongs` 가 빈 집합을 받아 `seed_enb` 의 `DONG_ID` 가 조용히
    전부 NULL 이 된다.
    """
    gd = Path("/x/data/geo")
    for loc in ("kr", "jp"):
        assert (topo.crosswalk_path(gd, loc).parent
                == topo.geo_level_path(gd, loc, "sgg").parent), loc
    # kr 은 루트 그대로여야 한다 — graphio `geo_xw.load` 가 그 자리를 읽는다
    assert topo.crosswalk_path(gd, "kr") == gd / topo.XW_FILE
    assert topo.crosswalk_path(gd, "jp") == gd / "jp" / topo.XW_FILE
