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
from tests.test_topo import FakeConn, _inserted_rows

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
    # `include_extra=False` — 이 테스트는 **1:1 상한 자체**를 본다.
    # 기본값(`True`, 2026-10-26 사용자 지시)은
    # `test_code_map_include_extra_adds_real_names_beyond_273` 가 본다.
    doc = topo.build_jp_code_map(gd, _kr_gu(), include_extra=False)

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
    # 1:1 이면 추가분이 없다
    assert doc["counts"] == {"kr_gu": 273, "extra": 0, "total": 273}
    assert not doc["no_room"] and not doc["dense_gu_ids"]
    assert all(not r["new"] for rows in doc["picked"].values() for r in rows)


def test_code_map_include_extra_adds_real_names_beyond_273(tmp_path, monkeypatch):
    """**1:1 상한을 풀면(`include_extra=True`, 기본값) KR 273 을 그대로 둔
    채 남는 시구정촌을 실제 지명으로 더 담는다** — 사용자 지시(2026-10-26)
    *"진짜 1:1 매핑 아니어도 돼. 없는 장비 추가해도 좋아."*
    """
    gd = _jp_sgg(tmp_path, monkeypatch)
    doc = topo.build_jp_code_map(gd, _kr_gu())      # include_extra 기본값

    kr_ids = {gu for _c, gu in _kr_gu()}
    assert len(doc["sgg"]) > 273
    assert doc["counts"]["kr_gu"] == 273
    assert doc["counts"]["extra"] == doc["counts"]["total"] - 273
    assert doc["counts"]["total"] == len(doc["sgg"])
    # KR 273 과 겹치는 대응은 1:1 때와 **그대로**다 — 44만 셀의 GU_ID 가
    # 그 273 안에만 있으므로 흔들리면 이미 만든 대응이 깨진다
    kr_only = topo.build_jp_code_map(gd, _kr_gu(), include_extra=False)
    assert doc["sgg"].items() >= kr_only["sgg"].items()
    # 추가 `GU_ID` 는 KR 에 없는 새 번호다 — `new: true` 로 갈린다
    extra_ids = {int(r["gu_id"]) for rows in doc["picked"].values()
                 for r in rows if r["new"]}
    assert extra_ids and not (extra_ids & kr_ids)
    assert isinstance(doc["no_room"], dict) and isinstance(doc["dense_gu_ids"], dict)
    # 새 구역도 실제 지명이다 — 생성 이름이 아니다
    assert all(r["name"] for rows in doc["picked"].values()
               for r in rows if r["new"])


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
    # `include_extra=False` — 이 테스트의 관심은 `seed_geo`/emd 생성이다,
    # 1:1 상한 밖 추가 지명 수까지 고정하면 `_jp_sgg` 합성 데이터의 우연한
    # 수(여기선 546)를 테스트에 박게 된다.
    topo.save_code_map(
        topo.build_jp_code_map(gd, _kr_gu(), include_extra=False), "jp")

    conn = _conn(locale="jp")
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

    conn = _conn(locale="jp")
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


# --------------------------------------------------------------------------
# 별도 스키마 — "DB 째로 이식" (사용자 방침 2026-10-26)
# --------------------------------------------------------------------------


def test_locale_picks_the_schema_with_override_winning():
    """`--database` > `pemdb1.locale_database.<locale>` > `pemdb1.database`.

    **기본이 현 DB 여야 한다** — 설정을 안 고친 환경이 바뀌면 안 된다.
    그리고 설정에 없는 locale 은 **현 DB 를 건드린다** — 그게 에러가
    아니므로 `cmd_topo` 가 어느 스키마인지 항상 먼저 찍는다.
    """
    cfg = {"pemdb1": {"database": "pemdb6",
                      "locale_database": {"jp": "pemdb6_jp"}}}
    assert topo.locale_database(cfg, "kr") == "pemdb6"
    assert topo.locale_database(cfg, "jp") == "pemdb6_jp"
    assert topo.locale_database(cfg, "xx") == "pemdb6"          # 없으면 현 DB
    assert topo.locale_database(cfg, "jp", "other") == "other"  # override 가 세다
    assert topo.locale_database({}, "jp") == "pemdb6"           # 설정이 없어도 돈다


class _CloneConn(FakeConn):
    """`clone_schema` 가 던지는 질의 넷에 답한다."""

    def __init__(self, tables, target_exists=False, grants=()):
        super().__init__()
        self.tables = list(tables)      # (name, rows, mb)
        self.target_exists = target_exists
        self.grants = list(grants) or ["GRANT ALL PRIVILEGES ON `pemdb6`.* TO `pem`@`%`"]

    def answer(self, sql, params):
        if "SCHEMATA" in sql and "DEFAULT_CHARACTER_SET_NAME" in sql:
            return [("utf8mb3", "utf8mb3_general_ci")]
        if "SCHEMATA" in sql:
            return [(1,)] if self.target_exists else []
        if "information_schema.TABLES" in sql:
            return list(self.tables)
        if "SHOW GRANTS" in sql:
            return [(g,) for g in self.grants]
        if "CURRENT_USER()" in sql:
            return [("pem@%",)]
        if "SELECT COUNT(*)" in sql:
            return [(0,)]            # 대상이 비어 있다 → 복사한다
        if sql.strip().startswith(("CREATE", "INSERT")):
            return []
        raise AssertionError(f"_CloneConn 이 모르는 질의다: {sql}")


#: 실측 분류(2026-10-26 · pemdb6 75테이블) — 마스터·코드는 데이터까지,
#: 1분 집계와 RCA 산출물은 구조만.
SAMPLE_TABLES = [
    ("CM_CITY", 17, 0.0), ("CM_GU", 273, 0.0), ("CM_DONG", 8079, 1.5),
    ("CM_CELL_REP_RU_LOC", 438156, 76.1), ("CM_EPC_INFO", 391, 0.0),
    ("CM_DATAGEN_CONFIG", 8, 0.0), ("CM_DATAGEN_HISTORY", 637, 0.2),
    ("CL_CELL_MAP", 559265, 68.1), ("CL_MME", 56, 0.0), ("CL_PGW", 169, 0.0),
    ("CD_CAUSE", 1199, 0.1), ("CB_VENDOR", 26, 0.0), ("CAUSE_DEF", 364, 0.0),
    ("PM_COR_LTE_CALL_1M", 173749, 2600.3), ("PM_ENB_KPI_1M", 104788, 15.3),
    ("AI_INCIDENT", 3, 0.0), ("AI_ANOMALY_EVENT", 3, 3.4),
]


def test_clone_copies_masters_and_only_the_structure_of_traffic():
    """**마스터는 데이터까지, 집계·산출물은 구조만.**

    가르는 기준은 "일본에서 새로 생성되는가"다. 셀 키(`ENB_ID`·`SECTOR`·
    `CELLNUM`)를 그대로 가져가야 **테이블 스위칭이 성립한다** — 그래서
    `CL_CELL_MAP`·`CM_CELL_REP_RU_LOC` 는 데이터까지다. `PM_*` 3.9GB 는
    일본 트래픽이 만들고, `AI_*` 는 그것에서 나온다.

    `CM_DATAGEN_HISTORY` 는 접두가 맞아도 **구조만**이다 — 로그라서, 한국
    에서 돌린 이력이 일본 DB 에 섞이면 "언제 무엇을 넣었나"가 거짓이 된다.
    """
    conn = _CloneConn(SAMPLE_TABLES)
    r = topo.clone_schema(conn, "pemdb6", "pemdb6_jp", dry_run=True)
    plan = {p["table"]: p["data"] for p in r["plan"]}

    for t in ("CM_CITY", "CM_GU", "CM_DONG", "CM_CELL_REP_RU_LOC",
              "CM_EPC_INFO", "CM_DATAGEN_CONFIG", "CL_CELL_MAP", "CL_MME",
              "CL_PGW", "CD_CAUSE", "CB_VENDOR", "CAUSE_DEF"):
        assert plan[t] is True, t
    for t in ("PM_COR_LTE_CALL_1M", "PM_ENB_KPI_1M", "AI_INCIDENT",
              "AI_ANOMALY_EVENT", "CM_DATAGEN_HISTORY"):
        assert plan[t] is False, t
    # 복사량이 집계를 빼고 세어진다 — 실측 153MB 대 3.9GB
    assert r["copy_mb"] < r["skip_mb"] / 10
    assert r["with_data"] + r["structure_only"] == r["tables"] == len(SAMPLE_TABLES)
    # dry-run 은 쓰지 않는다
    joined = " ".join(conn.sql).upper()
    assert "CREATE" not in joined and "INSERT" not in joined


def test_clone_refuses_to_write_into_the_source():
    """원본과 대상이 같으면 멈춘다 — **그게 KR 을 덮는 경로다.**

    설정에 없는 locale 은 현 DB 로 떨어지므로(`locale_database`) 이 가지가
    실제로 밟힌다.
    """
    with pytest.raises(RuntimeError, match="원본과 대상이 같다"):
        topo.clone_schema(_CloneConn(SAMPLE_TABLES), "pemdb6", "pemdb6")


def test_clone_tells_the_schema_must_be_created_by_a_human():
    """이 계정은 **스키마를 만들 수 없다** — 미리 말해야 한다.

    `CREATE DATABASE` 는 전역 권한이고 실측으로 `pem` 의 전역 권한은
    `PROCESS` 하나다. dry-run 이 말하지 않으면 실행 때 `1044 Access
    denied` 로 멈추는데, 그 메시지는 **"계정이 틀렸다"처럼 읽혀** 엉뚱한
    데를 찾게 만든다.
    """
    r = topo.clone_schema(_CloneConn(SAMPLE_TABLES), "pemdb6", "pemdb6_jp",
                          dry_run=True)
    assert r["target_exists"] is False
    assert r["can_create_schema"] is False
    assert r["has_target_grant"] is False
    sql = " ".join(r["prepare_sql"])
    assert "CREATE DATABASE `pemdb6_jp`" in sql
    assert "utf8mb3_general_ci" in sql          # 원본 collation 을 따라간다
    assert "GRANT ALL PRIVILEGES ON `pemdb6_jp`.*" in sql
    assert "'pem'@'%'" in sql

    # 전역 권한이 있으면 만들 수 있다고 말한다
    ok = topo.clone_schema(
        _CloneConn(SAMPLE_TABLES, target_exists=True,
                   grants=["GRANT ALL PRIVILEGES ON *.* TO `root`@`%`"]),
        "pemdb6", "pemdb6_jp", dry_run=True)
    assert ok["can_create_schema"] and ok["has_target_grant"]


def test_clone_keeps_the_table_names():
    """`clone_schema` **자신은** 접미사를 붙이지 않는다 — 스키마를 통째로
    가르는 옛 설계(별도 `pemdb6_jp`)의 산물이고 그 안에서는 맞는 말이었다.

    **이 함수는 2026-10-26 에 폐기 대기로 남았다** — 사용자가 범위를
    "지역 테이블만 JP 벌을 만들고 나머지는 원본을 공유한다"로 줄이면서,
    로케일 축이 DB 이름이 아니라 테이블 이름(`table_name()`)으로 내려갔다.
    `clone_schema` 는 지우지 않았을 뿐 더는 쓰이지 않는다 — 실제로 쓰이는
    쪽의 네 가지 결정은 바로 아래 넷이 고정한다.
    """
    r = topo.clone_schema(_CloneConn(SAMPLE_TABLES), "pemdb6", "pemdb6_jp",
                          dry_run=True)
    assert {p["table"] for p in r["plan"]} == {t for t, _n, _m in SAMPLE_TABLES}
    assert not any(p["table"].endswith(("_JP", "_jp")) for p in r["plan"])


def test_table_name_rejects_cellmap_for_non_kr_locale():
    """**`CL_CELL_MAP` 에는 로케일 축이 없다.** 조용히 `CL_CELL_MAP_JP` 를
    만들면 셀 키의 정본이 둘이 되어 어느 쪽으로 생성했는지 알 수 없다 —
    그래서 `cellmap` 을 `kr` 아닌 로케일로 부르면 **멈춘다.**"""
    assert topo.table_name("cellmap", "kr") == "CL_CELL_MAP"
    with pytest.raises(RuntimeError, match="로케일 축이 없다"):
        topo.table_name("cellmap", "jp")


def test_ensure_locale_tables_rejects_kr():
    """**`locale == "kr"` 은 거부한다.** KR 은 원본이고 `LIKE` 를 돌릴 대상이
    아니다 — 받아 주면 `wipe` 계열과 엮였을 때 운영 테이블이 위험해진다.
    질의를 하나도 쏘지 않고 그 자리에서 멈춰야 한다."""
    conn = FakeConn()
    with pytest.raises(RuntimeError, match="원본이다"):
        topo.ensure_locale_tables(conn, "kr")
    assert not conn.sql


class _CopyCellsConn(FakeConn):
    """`copy_cells` 가 던지는 질의에 답한다. `dst_rows` 로 "이미 채워진
    대상"과 "빈 대상"을 가른다."""

    def __init__(self, dst_rows: int = 0, src_rows: int = 442_689):
        super().__init__(ddl=("ENB_ID", "SECTOR", "CITY_ID", "GU_ID",
                              "DONG_ID", "LATITUDE", "LONGITUDE", "RU_NAME"))
        self.dst_rows = dst_rows
        self.src_rows = src_rows

    def answer(self, sql, params):
        if "is_nullable" in sql:
            return []        # 비울 컬럼 중 NOT NULL 인 것이 없다
        if "GROUP BY" in sql:
            # `copy_locale_rows` 의 원본 키 중복 수(`n_uniq`) — 중복 0 으로
            # 둔다. 중복 보고 자체는 `copy_locale_rows` 의 docstring 실측
            # (`CL_ENB_ORG` 45,955/45,954)으로 이미 고정되어 있다.
            return [(self.src_rows,)]
        if "SELECT COUNT(*) FROM `CM_CELL_REP_RU_LOC_JP`" in sql:
            return [(self.dst_rows,)]
        if "SELECT COUNT(*) FROM `CM_CELL_REP_RU_LOC`" in sql:
            return [(self.src_rows,)]
        return super().answer(sql, params)


def test_copy_cells_does_not_double_insert_into_a_populated_target():
    """**이미 행이 있으면 다시 넣지 않는다.** `CM_CELL_REP_RU_LOC_JP` 에는
    PK 가 없어(`ix_local_ru_loc_enb_sector` 인덱스뿐) 중복 `INSERT` 가
    에러 없이 쌓인다 — 그러면 한 셀이 좌표를 둘 갖는다. 그 판단은 사람이
    먼저 비우는 것으로만 되돌릴 수 있다(코드가 대신 비우지 않는다)."""
    conn = _CopyCellsConn(dst_rows=442_689)
    stats = topo.copy_cells(conn, "jp", write=True)
    assert stats["inserted"] == 0
    assert stats["skipped"] and "이미" in stats["skipped"]
    assert not any(q.upper().startswith("INSERT") for q in conn.sql)


def test_copy_cells_requires_write_even_on_an_empty_target():
    """빈 대상이라도 `write=False` 면 **세어 보기만** 한다."""
    conn = _CopyCellsConn(dst_rows=0)
    stats = topo.copy_cells(conn, "jp", write=False)
    assert stats["inserted"] == 0
    assert stats["skipped"] and "--write" in stats["skipped"]
    assert not conn.inserted


def test_table_names_are_hardcoded_only_in_the_tables_registry():
    """**`CM_CITY`·`CM_GU`·`CM_DONG`·`CM_CELL_REP_RU_LOC`·`CL_CELL_MAP`
    리터럴이 `TABLES` 정의 한 곳에만 있어야 한다.** 다른 자리에 하드코딩이
    남으면 `table_name()` 간접화가 깨져 그 자리만 조용히 KR 테이블을
    계속 읽는데, 조인은 성립하고 에러도 나지 않는다(이번 작업이 고치려는
    바로 그 종류다).
    """
    import re
    import subprocess

    root = Path(__file__).resolve().parents[1]
    src_path = root / "src" / "datagen" / "topo.py"
    lines = src_path.read_text(encoding="utf-8").splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith("TABLES"))
    end = next(i for i in range(start, len(lines)) if lines[i].rstrip() == "}")
    allowed = set(range(start + 1, end + 2))      # grep 은 1-based 라인 번호

    out = subprocess.run(
        ["grep", "-rn", "-E",
         r'"(CM_CITY|CM_GU|CM_DONG|CM_CELL_REP_RU_LOC|CL_CELL_MAP|CL_ENB_ORG)"',
         str(root / "src" / "datagen")],
        capture_output=True, text=True, check=True).stdout

    hits = [line for line in out.strip().splitlines() if line]
    assert hits, "grep 이 아무것도 못 찾았다 — 패턴이 깨졌을 수 있다"
    for line in hits:
        path, lineno, _rest = line.split(":", 2)
        assert Path(path).name == "topo.py", line
        assert int(lineno) in allowed, (
            f"TABLES 정의({start + 1}~{end + 1}) 밖에서 하드코딩됐다: {line}")


def test_clone_creates_the_schema_with_the_source_collation():
    """**원본의 collation 을 따라간다.**

    MySQL 기본값(`utf8mb4_0900_ai_ci`)으로 만들면 조인 키가
    `Illegal mix of collations` 로 깨지거나, `_ci` 끼리라도 **이름이 조용히
    합쳐진다**(불변규칙 14 — `RU_NAME` 229,148종에서 14건 충돌 실측).
    상용이 `utf8mb3_general_ci` 이므로 그것을 그대로 쓴다.

    그리고 `CREATE TABLE … LIKE` 여야 한다 — DDL 을 다시 적으면 인덱스·
    기본값이 상용과 달라져 두 환경을 대조할 수 없다.
    """
    conn = _CloneConn(SAMPLE_TABLES)
    r = topo.clone_schema(conn, "pemdb6", "pemdb6_jp", dry_run=False)
    sql = " ".join(conn.sql)
    assert "CREATE DATABASE IF NOT EXISTS `pemdb6_jp`" in sql
    assert "CHARACTER SET utf8mb3 COLLATE utf8mb3_general_ci" in sql
    assert "utf8mb4" not in sql
    # 구조는 LIKE 로, 데이터는 INSERT … SELECT 로
    assert "LIKE `pemdb6`.`CM_GU`" in sql
    assert "INSERT INTO `pemdb6_jp`.`CM_GU` SELECT * FROM `pemdb6`.`CM_GU`" in sql
    # 집계는 구조만 — INSERT 가 없어야 한다
    assert "INSERT INTO `pemdb6_jp`.`PM_COR_LTE_CALL_1M`" not in sql
    assert r["created"] == len(SAMPLE_TABLES)
    assert r["copied"] == r["with_data"]


# --------------------------------------------------------------------------
# 조직 이름 — **지리가 아니다.** 셋이 다른 규칙이다
# --------------------------------------------------------------------------

#: 실측(2026-10-26). `BRANCH_ID = 1200`(대구)이 `CM_BRANCH` 에 **없고**
#: 그 밑에 `CM_REGION` 2줄이 있다 — 부모를 못 찾는 경우가 실재한다.
ORG_BRANCH = [(1000, "수도권Infra"), (1100, "동부Infra"), (1300, "서부Infra"),
              (1400, "중부Infra"), (1500, "Core Infra"), (9999, "기타")]
ORG_LAREA = [(1000, "수도권", 1000), (1100, "부산", 1100), (1200, "대구", 1200),
             (1301, "전남", 1300), (1302, "전북", 1300), (1303, "제주", 1300),
             (1401, "충남", 1400), (1402, "충북", 1400), (1403, "강원", 1400)]
ORG_CITY = [(11, "서울"), (12, "세종"), (26, "부산"), (27, "대구"), (28, "인천"),
            (29, "광주"), (30, "대전"), (31, "울산"), (41, "경기"), (42, "강원"),
            (43, "충북"), (44, "충남"), (45, "전북"), (46, "전남"), (47, "경북"),
            (48, "경남"), (49, "제주")]
ORG_REGION = ([(1000 + i, 1000) for i in range(1, 22)]        # 수도권 21줄
              + [(1100 + i, 1100) for i in range(1, 4)]
              + [(1200 + i, 1200) for i in range(1, 3)]       # 부모 없음
              + [(1300 + i, 1300) for i in range(1, 5)]
              + [(1400 + i, 1400) for i in range(1, 5)])


class _OrgConn(FakeConn):
    def __init__(self):
        super().__init__()
        self.updates: list[tuple[str, list]] = []

    def answer(self, sql, params):
        if "FROM CM_BRANCH" in sql:
            return list(ORG_BRANCH)
        if "FROM CM_LAREA" in sql:
            return [(i, n) for i, n, _b in ORG_LAREA]
        if "FROM `CM_CITY`" in sql:
            return list(ORG_CITY)
        if "FROM CM_REGION" in sql:
            return list(ORG_REGION)
        raise AssertionError(f"_OrgConn 이 모르는 질의다: {sql}")


def _org(dry_run=False):
    conn = _OrgConn()
    # `allow_shared=True` — 이 셋(CM_BRANCH·CM_LAREA·CM_REGION)은 로케일
    # 축이 없는 한 벌이라 기본이 거부다(2026-10-26). 실제 생성 동작을
    # 검증하는 테스트들은 그 사람의 결정이 이미 내려진 상태를 전제한다 —
    # 거부 자체는 `test_org_names_refuses_without_allow_shared` 가 본다.
    stats = topo.org_names(conn, locale="jp", dry_run=dry_run, allow_shared=True)
    out: dict[str, list] = {}
    for sql, data in conn.inserted:
        for t in ("CM_BRANCH", "CM_LAREA", "CM_REGION"):
            if f"UPDATE {t} " in sql:
                out.setdefault(t, []).extend(tuple(r) for r in data)
    return conn, stats, out


def test_org_names_keep_every_id_and_parent_link():
    """**ID·부모 연결을 건드리지 않는다.**

    `CL_ENB_ORG.REGION_ID`·`BRANCH_ID` 와 `CM_EPC_INFO` 가 그 숫자를
    참조하고 graphio `kpi.py:532·587` 이 그것으로 조인한다 — 바꾸면 조직
    축이 통째로 끊어진다. **이름만** 쓴다.
    """
    conn, _s, out = _org()
    sql = " ".join(conn.sql)
    assert "SET BRANCH_NAME = %s WHERE BRANCH_ID = %s" in sql
    assert "SET LAREA_NAME = %s WHERE LAREA_ID = %s" in sql
    assert "SET REGION_NAME = %s WHERE REGION_ID = %s" in sql
    for forbidden in ("SET BRANCH_ID", "SET LAREA_ID", "SET REGION_ID",
                      "SET TEAM_ID", "SET REGION_TYPE"):
        assert forbidden not in sql, forbidden
    # 키가 원래 ID 그대로다
    assert {i for _n, i in out["CM_BRANCH"]} == {b for b, _n in ORG_BRANCH}
    assert {i for _n, i in out["CM_LAREA"]} == {i for i, _n, _b in ORG_LAREA}


def test_branch_comes_from_the_constant_table():
    """6줄은 **사람이 정한 표**다 — 광역 묶음이라 유도할 근거가 없다.

    키가 `BRANCH_ID` 여야 한다 — 이름으로 키를 잡으면 한 번 바꾼 뒤 다시
    돌릴 때 못 찾는다(이 함수는 멱등이어야 한다).
    """
    _c, _s, out = _org()
    assert dict((i, n) for n, i in out["CM_BRANCH"]) == topo.BRANCH_MAP_JP
    assert set(topo.BRANCH_MAP_JP) == {b for b, _n in ORG_BRANCH}
    # `Core Infra` 는 중립이라 그대로 둔다
    assert topo.BRANCH_MAP_JP[1500] == "Core Infra"


def test_larea_is_derived_from_the_sido_map():
    """9줄은 **`SIDO_MAP_JP` 에서 유도**한다 — 별도 표를 만들지 않는다.

    두 표가 어긋나면 코어 장비 권역과 지리가 갈린다. 이름이 거의 시도명
    이라 `CM_CITY` 로 찾아가면 되고, `수도권`(서울·경기·인천 묶음)만
    대응 시도가 없어 `LAREA_EXTRA_JP` 한 줄이다.
    """
    _c, stats, out = _org()
    got = dict((i, n) for n, i in out["CM_LAREA"])
    city = {n: c for c, n in ORG_CITY}
    for lid, lname, _b in ORG_LAREA:
        if lname in topo.LAREA_EXTRA_JP:
            assert got[lid] == topo.LAREA_EXTRA_JP[lname]
        else:
            assert got[lid] == topo.SIDO_MAP_JP[str(city[lname])], lname
    assert got[1100] == topo.SIDO_MAP_JP["26"] == "福岡県"      # 부산
    assert got[1303] == topo.SIDO_MAP_JP["49"] == "沖縄県"      # 제주
    assert stats["larea_no_sido"] == []
    # **`SIDO_MAP_JP` 를 중복해 적지 않았다**
    assert len(topo.LAREA_EXTRA_JP) == 1


def test_region_takes_the_parent_name_with_a_deterministic_serial():
    """34줄은 `<부모 BRANCH 이름><연번>` 으로 **생성**한다.

    지리와 독립이라 유도할 근거가 없고(`영동2` 가 15개 시도에 걸친다)
    어차피 자유 명명이다. 연번은 `REGION_ID` 정렬 순이라 결정론적이다.

    그리고 **`CM_LAREA` 와 같은 규칙으로 만들지 않는다** — `kpi.py:574` 가
    *"코어 9종과 eNB 34종을 섞지 않는다"* 고 못 박는다.
    """
    _c, stats, out = _org()
    got = dict((i, n) for n, i in out["CM_REGION"])
    # 수도권 밑 21줄이 1~21 로 붙는다
    seoul = [got[1000 + i] for i in range(1, 22)]
    assert seoul == [f"{topo.BRANCH_MAP_JP[1000]}{i}" for i in range(1, 22)]
    # 부모가 다르면 접두가 다르다
    assert got[1101].startswith(topo.BRANCH_MAP_JP[1100])
    # 연번이 부모 안에서만 센다 — 전역 연번이면 1101 이 22 가 된다
    assert got[1101] == f"{topo.BRANCH_MAP_JP[1100]}1"
    # LAREA 이름과 겹치지 않는다 (두 축이 섞이면 안 된다)
    assert not (set(got.values()) & {n for n, _i in out["CM_LAREA"]})
    # 두 번 돌려도 같다
    _c2, _s2, out2 = _org()
    assert out == out2
    assert stats["region"] == len(ORG_REGION) - 2


def test_region_without_a_parent_is_left_alone():
    """부모를 못 찾은 줄은 **건드리지 않고 수를 보고한다.**

    실측으로 `BRANCH_ID = 1200`(대구)이 `CM_BRANCH` 에 **없고** 그 밑에
    2줄이 있다. 이름을 지어내면 그 둘이 "있는 지사"처럼 보인다.
    """
    _c, stats, out = _org()
    assert stats["region_no_parent"] == 2
    assert 1201 not in dict((i, n) for n, i in out["CM_REGION"])


def test_kr_locale_changes_nothing():
    """`kr` 은 **원본이다** — "생성"으로 덮으면 반출본과 대조할 수 없다."""
    conn = _OrgConn()
    stats = topo.org_names(conn, locale="kr", dry_run=False)
    assert stats["branch"] == stats["larea"] == stats["region"] == 0
    assert not conn.inserted and not conn.sql
    assert "원본" in stats["skipped"]


def test_untouched_tables_are_named_with_a_reason():
    """**건드리지 않는 자리를 적어 둔다.**

    적지 않으면 다음 사람이 "한국어가 남았다"고 고친다. 앞 셋은 graphio 가
    **이름을 조인하지 않고**(EMS 는 `EMS_ID` 숫자만 쓴다), `CB_VENDOR` 는
    읽히지만 **제조사 축이 지리 전환과 다른 축**이다.
    """
    conn, stats, out = _org()
    assert set(stats["untouched"]) == {
        "CL_MME.NET_NAME", "CM_EMS.EMS_NAME", "CM_COMPANY",
        "CB_VENDOR.VENDOR_NAME"}
    # 실제로 SQL 을 쏘지 않는다
    sql = " ".join(conn.sql).upper()
    for t in ("CL_MME", "CM_EMS", "CM_COMPANY", "CB_VENDOR"):
        assert t not in sql, t
    assert set(out) == {"CM_BRANCH", "CM_LAREA", "CM_REGION"}
    # 이유가 모듈에 적혀 있어야 한다 — 상수만 있으면 다음 사람이 고친다
    root = Path(__file__).resolve().parents[1]
    src = (root / "src" / "datagen" / "topo.py").read_text(encoding="utf-8")
    i = src.index("ORG_UNTOUCHED")
    head = src[max(0, i - 1500):i]
    assert "조인하지 않는다" in head and "다른 축" in head


def test_org_names_dry_run_writes_nothing():
    conn = _OrgConn()
    stats = topo.org_names(conn, locale="jp", dry_run=True, allow_shared=True)
    assert stats["branch"] == 6 and stats["larea"] == 9
    assert not conn.inserted


# --------------------------------------------------------------------------
# 적재 전 안전장치 — 전부 **되돌릴 수 없는 사고**를 막는 자리다
# --------------------------------------------------------------------------


def _ignored(path: str) -> bool:
    """`git check-ignore` 실측. 추측하지 않는다 — `.gitignore` 의 되살리기
    사슬은 읽어서 맞히기 어렵고, 틀리면 **PUBLIC 저장소에 자산이 올라간다.**"""
    import subprocess

    root = Path(__file__).resolve().parents[1]
    return subprocess.run(["git", "check-ignore", "-q", "--", path],
                          cwd=root, capture_output=True).returncode == 0


def test_only_the_code_map_is_tracked_under_locale_jp():
    """**`topo dump jp` 산출물이 git 에 들어가면 안 된다.**

    디렉터리 예외(`!…/jp/`)만 두면 **나중에 들어오는 파일이 자동으로
    추적된다** — 그 덤프는 클론 직후라 `CM_CELL_REP_RU_LOC.json.gz`
    16MB·44만행이 **한국 실제 자산 대장**이고, PUBLIC 저장소의 git
    히스토리에서는 지워지지 않는다.

    그래서 디렉터리를 다시 막고 **파일 하나만** 되살린다. 새 파일을
    추적하려면 `.gitignore` 에 한 줄을 더해야 하고, 그 한 줄이 "공개해도
    되는가"를 묻는 자리다.
    """
    base = "data/pemdb1/locale"
    assert not _ignored(f"{base}/jp/code_map.json"), "코드 맵은 추적 대상이다"
    for name in ("CM_CITY", "CM_GU", "CM_DONG", "CM_CELL_REP_RU_LOC"):
        assert _ignored(f"{base}/jp/{name}.json.gz"), name
        assert _ignored(f"{base}/kr/{name}.json.gz"), name
    # 앞으로 생길 이름도 막혀 있어야 한다 — 그게 "파일 단위"의 뜻이다
    assert _ignored(f"{base}/jp/SOMETHING_NEW.json.gz")
    assert _ignored(f"{base}/jp/dump.sql")


def test_writing_commands_need_the_write_flag():
    """**`--write` 없이는 아무것도 적재하지 않는다.**

    전에는 `--dry-run` 이 `store_true` 라 **기본이 적재**였다 — 함수
    기본값(`dry_run=True`)만 보고 "기본이 dry-run" 이라 믿었고, 실제로는
    `topo seed-geo --replace` 한 줄이 그대로 적재였다. 오타 하나
    (`--locale` 누락)가 KR 세 테이블을 덮는 자리다.

    목록에 `dump`·`geo-fetch`·`list` 가 **없어야** 한다 — 전자는 사본을
    뜨는 것이 목적이고 나머지는 읽기다.
    """
    from datagen.cli import TOPO_WRITING

    assert {"clone", "seed-geo", "seed-enb", "relocate-cells", "org-names",
            "code-map", "add", "rm"} <= set(TOPO_WRITING)
    assert not ({"list", "dump", "geo-fetch", "restore"} & set(TOPO_WRITING))

    # 파서가 둘을 다 받는다 — `--dry-run` 을 쓰던 호출이 깨지지 않아야 한다
    from datagen.cli import main as cli_main      # noqa: F401
    import argparse
    import inspect

    from datagen import cli as cli_mod

    src = inspect.getsource(cli_mod)
    assert '"--write", action="store_true"' in src
    assert '"--dry-run", action="store_true"' in src
    # 쓰기 여부가 **한 자리**에서 정해진다 — 명령마다 따로 보면 하나를 빠뜨린다
    assert 'write = bool(getattr(args, "write", False))' in src
    assert "dry = not write" in src
    assert "dry_run=args.dry_run" not in src, "옛 경로가 남았다"
    assert isinstance(argparse.ArgumentParser(), argparse.ArgumentParser)


def test_seed_geo_replace_passes_the_restore_gate():
    """`--replace` 는 세 테이블을 비운다 — **사본을 요구해야 한다.**

    게이트 호출부가 `rm --all` 과 `relocate-cells` 둘뿐이었고 `seed-geo`
    에는 없었다. `--locale` 을 빠뜨리면 KR 세 테이블이 사본 없이 날아간다.
    """
    import inspect

    from datagen import cli as cli_mod

    src = inspect.getsource(cli_mod.cmd_topo)

    def block_of(act: str) -> str:
        """그 action 블록만. **다음 `if act ==` 까지** 자른다.

        전에는 `seed-enb` 까지 잘랐는데 그 사이에 `org-names`·
        `relocate-cells` 블록이 끼어 있어서, `seed-geo` 의 게이트를 떼도
        **옆 블록의 게이트에 걸려 통과**했다(변이로 확인).
        """
        head = f'if act == "{act}":'
        rest = src[src.index(head) + len(head):]
        nxt = rest.find("if act == ")
        return rest if nxt < 0 else rest[:nxt]

    seed = block_of("seed-geo")
    assert "_topo_gate(conn, args.locale, db)" in seed, seed[:200]
    assert "args.replace" in seed
    # 세 쓰기 명령 전부 게이트를 지난다 — 각자의 블록 안에서 본다
    for act in ("relocate-cells", "org-names", "add", "rm"):
        assert "_topo_gate(" in block_of(act), act


def test_org_names_stops_when_the_geo_layer_is_already_japanese():
    """**`org-names` 는 `seed-geo` 앞이다.**

    `CM_LAREA` 유도가 `CM_CITY` 의 **한국 이름**을 읽어 `SIDO_MAP_JP` 를
    찾는다. 지리를 먼저 바꾸면 전량 무매칭이 되어 **9줄이 조용히 빠지고**
    권역 이름만 한국어로 남는다 — 에러가 아니다. 그래서 멈춘다.
    """
    class _JpCityConn(_OrgConn):
        def answer(self, sql, params):
            if "FROM `CM_CITY`" in sql:
                # 이미 일본 이름이다 — `seed-geo` 가 먼저 돈 상태
                return [(11, "東京都"), (26, "福岡県"), (30, "大阪府")]
            return super().answer(sql, params)

    with pytest.raises(RuntimeError, match="seed-geo"):
        topo.org_names(_JpCityConn(), locale="jp", dry_run=True, allow_shared=True)
    # 한국 이름이면 그대로 돈다
    assert topo.org_names(_OrgConn(), locale="jp", dry_run=True,
                          allow_shared=True)["larea"] == 9


def test_org_names_refuses_without_allow_shared():
    """**`CM_BRANCH`·`CM_LAREA`·`CM_REGION` 에는 로케일 축이 없다** — 기본은
    거부다(2026-10-26). 접미사 방식에서는 이 셋을 덮으면 KR 화면의 조직
    축도 함께 바뀌고, 복귀 사본(`DUMP_TABLES`)도 없어 되돌릴 수 없다. 그래서
    `allow_shared=True` 를 **사람이 명시해야** 돈다 — 쿼리를 하나도 쏘지
    않고 그 자리에서 멈춘다."""
    conn = _OrgConn()
    with pytest.raises(RuntimeError, match="allow_shared"):
        topo.org_names(conn, locale="jp", dry_run=True)
    assert not conn.sql, "거부는 질의 전에 일어나야 한다"


# --------------------------------------------------------------------------
# `CL_ENB_ORG_JP` · JP 전용 eNB — 2026-10-27 추가 (5번째 벌 + 273 상한 해제)
# --------------------------------------------------------------------------


class _CopyEnbOrgConn(FakeConn):
    """`copy_locale_rows(key="enb_org")` 가 던지는 질의에 답한다."""

    def __init__(self, dst_rows: int = 0, src_rows: int = 45_955):
        super().__init__(ddl=("ENB_ID", "C_UID", "COMPANY_ID", "REGION_ID",
                              "BRANCH_ID", "OPTEAM_ID", "PART_ID", "EMS_ID",
                              "MME_GRP_ID", "BTS_NAME"))
        self.dst_rows = dst_rows
        self.src_rows = src_rows

    def answer(self, sql, params):
        if "is_nullable" in sql:
            return []
        if "GROUP BY" in sql:
            return [(self.src_rows,)]      # 중복 0 으로 둔다
        if "SELECT COUNT(*) FROM `CL_ENB_ORG_JP`" in sql:
            return [(self.dst_rows,)]
        if "SELECT COUNT(*) FROM `CL_ENB_ORG`" in sql:
            return [(self.src_rows,)]
        return super().answer(sql, params)


def test_copy_enb_org_does_not_double_insert_into_a_populated_target():
    """`copy_cells` 와 **같은 가드가 `enb_org` 에도 선다.** `CL_ENB_ORG` 는
    PK 도 인덱스도 없어(`copy_locale_rows` docstring 실측) 두 번 돌리면
    중복이 에러 없이 쌓인다 — `copy_locale_rows` 가 key 를 받는 한 벌로
    일반화됐으므로, 이 가드가 `cell` 뿐 아니라 `enb_org` 에도 적용되는지
    따로 확인한다."""
    conn = _CopyEnbOrgConn(dst_rows=45_955)
    stats = topo.copy_enb_org(conn, "jp", write=True)
    assert stats["inserted"] == 0
    assert stats["skipped"] and "이미" in stats["skipped"]
    assert not any(q.upper().startswith("INSERT") for q in conn.sql)
    # BTS_NAME 만 NULL 로 비운다 — 조직 ID 9개는 KR 값 그대로다
    assert stats["null_columns"] == ["BTS_NAME"]


def test_wipeable_rejects_cellmap_in_either_locale():
    """`CL_CELL_MAP` 은 **어느 로케일로도 `rm --all` 대상이 아니다** — 로케일
    축이 없는 공유 셀 대장이라 비우면 언제나 KR 자체가 지워지고, 사본도
    없다."""
    assert topo.wipeable("cellmap", "kr") is False
    assert topo.wipeable("cellmap", "jp") is False
    # 비교 대조 — 같은 locale="jp" 에서 나머지 넷은 다시 만들 수 있어 허용
    for key in ("city", "gu", "dong", "cell", "enb_org"):
        assert topo.wipeable(key, "jp") is True, key


def test_next_gu_ids_stops_when_the_city_prefix_changes():
    """**`GU_ID` 접두 2자리가 바뀌면 그 자리에서 발급을 끊는다.**

    `seed_geo` 가 `CITY_ID` 를 `GU_ID` 앞 2자리에서 유도하므로, `41990`
    다음 10 단위(`42000`)는 **다른 시도**로 읽힌다 — 에러 없이 조용히
    섞인다. 그래서 접두가 깨지는 순간 멈추고 못 담은 수를 호출부가
    말하게 한다.
    """
    # 바로 끊기는 경우 — 다음 번호가 이미 다른 시도다
    assert topo.next_gu_ids(41, {41990}, want=3, step=10) == []
    # 일부만 담고 끊기는 경우 — 41980·41990 은 41 대 안, 42000 은 42 대
    got = topo.next_gu_ids(41, {41970}, want=3, step=10)
    assert got == [41980, 41990]
    assert len(got) < 3
    # 자리가 넉넉하면 그만큼 다 나온다
    assert topo.next_gu_ids(41, {41100}, want=2, step=10) == [41110, 41120]


def test_remove_added_enbs_refuses_without_a_ledger(tmp_path, monkeypatch):
    """**원장이 없거나 비어 있으면 아무것도 하지 않고 멈춘다.** `CL_CELL_MAP`
    은 `DUMP_TABLES` 밖이라 사본이 없다 — 지울 대상을 아는 유일한 근거가
    원장이고, 대역으로 추측해 지우면 그 번호를 쓰는 다른 장비가 함께
    사라진다."""
    monkeypatch.setattr(topo, "LOCALE_DIR", tmp_path)
    conn = FakeConn()
    with pytest.raises(RuntimeError, match="원장"):
        topo.remove_added_enbs(conn, "jp")
    assert not conn.sql, "거부는 질의 전에 일어나야 한다"


def test_seed_enb_jp_requires_band_start():
    """**`--band-start` 없이는 `seed-enb-jp` 가 멈춘다.** `topo.seed_enb_jp`
    의 `band_start` 는 Python 시그니처 자체에 기본값이 없어(위치 인자)
    빠뜨리면 `TypeError` 가 나는데, CLI 는 그 전에 **사람이 읽을 수 있는
    메시지**로 먼저 멈춰야 한다 — 어느 대역이 비어 있는지는 그 DB 의
    사실이라 코드에 기본값을 둘 수 없다는 설명과 함께."""
    import inspect

    from datagen import cli as cli_mod

    src = inspect.getsource(cli_mod)
    i = src.index('if act == "seed-enb-jp":')
    block = src[i:src.index('if act == ', i + 30)]
    assert "args.band_start is None" in block
    assert "--band-start" in block
    # 함수 자체에도 기본값이 없다 — CLI 가드가 없어도 조용히 통과하지 않는다
    assert inspect.signature(topo.seed_enb_jp).parameters["band_start"].default \
        is inspect.Parameter.empty


# --------------------------------------------------------------------------
# `seed_enb_jp` — KR 대응 판정은 `gu_keys`(전수) · 좌표는 `gu_points`
# (2026-10-27 수정 — 전에는 `gu_points` 로 존재까지 물어 좌표 없는 KR 2구
# 가 "신규 구역"으로 잘못 분류됐다. 676 이 678 로 나온 2 차이가 유일한
# 신호였다)
# --------------------------------------------------------------------------


class _SeedEnbJpConn(FakeConn):
    """`seed_enb_jp` 가 던지는 질의에 답한다.

    `kr_gu` · `jp_gu` 는 `(CITY_ID, GU_ID, GU_NAME, lat_or_None, lon_or_None)`
    5-튜플 목록이다 — **존재(`gu_keys`)는 좌표 유무와 무관하게 전수**를
    돌려주고, **좌표(`gu_points`)는 `lat`·`lon` 이 둘 다 있는 행만** 돌려줘
    실제 `WHERE LATITUDE IS NOT NULL AND LONGITUDE IS NOT NULL` 을 흉내낸다
    — 이 구분이 흐려지면 테스트가 버그를 재현하지 못한다.
    """

    def __init__(self, kr_gu, jp_gu, dongs=None, locale="jp"):
        super().__init__(ddl=("ENB_ID", "SECTOR", "CITY_ID", "GU_ID",
                              "DONG_ID", "LATITUDE", "LONGITUDE", "RU_NAME",
                              "C_UID", "COMPANY_ID", "REGION_ID", "BRANCH_ID",
                              "OPTEAM_ID", "PART_ID", "EMS_ID", "MME_GRP_ID",
                              "BTS_NAME"))
        self.kr_gu = list(kr_gu)
        self.jp_gu = list(jp_gu)
        self.dongs = dict(dongs or {})
        self.gu_tbl = topo.table_name("gu", locale)
        self.dong_tbl = topo.table_name("dong", locale)

    def answer(self, sql, params):
        if "information_schema.tables" in sql:
            return [(1,)]
        if "GU_NAME, LATITUDE, LONGITUDE" in sql:
            # `gu_points` — 좌표 있는 행만. KR 호출도 올 수 있어 테이블로 가른다
            src = self.kr_gu if "`CM_GU`" in sql else self.jp_gu
            return [(c, g, n, la, lo) for c, g, n, la, lo in src
                    if la is not None and lo is not None]
        if "SELECT CITY_ID, GU_ID FROM `CM_GU`" in sql:
            return [(c, g) for c, g, *_ in self.kr_gu]      # gu_keys(kr) — 전수
        if f"SELECT CITY_ID, GU_ID FROM `{self.gu_tbl}`" in sql:
            return [(c, g) for c, g, *_ in self.jp_gu]       # gu_keys(jp) — 전수
        if f"GU_ID, DONG_ID FROM `{self.dong_tbl}`" in sql:
            return [(g, d) for g, ds in sorted(self.dongs.items()) for d in ds]
        if "SELECT DISTINCT `ENB_ID`" in sql:
            return []                                        # ENB_ID 충돌 없음
        if "o.COMPANY_ID" in sql:
            return []                                        # _org_defaults — 없음
        return super().answer(sql, params)


def test_seed_enb_jp_does_not_treat_a_coordless_kr_gu_as_a_new_region(
        tmp_path, monkeypatch):
    """**이번 버그의 재현 조건.** KR `CM_GU` 에 좌표 없는 구(세종 모양)가
    있어도, `(CITY_ID, GU_ID)` 가 KR 에 있으면 신규 구역으로 잡히지 않아야
    한다 — `gu_points` 로 존재를 물었을 때 이 구가 "KR 에 없다"로 읽혀
    이미 셀이 있는 자리에 JP 전용 eNB 를 또 놓으려 했던 자리다.

    `write=True` 로 돌려 **실제로 어느 `GU_ID` 에 놓였는지**까지 확인한다
    — 반환 통계(`gus`)만 보면 "하나가 신규"라는 사실은 맞지만 "세종이
    아니라 무사시노시였다"는 것은 삽입된 행을 봐야 안다.
    """
    monkeypatch.setattr(topo, "LOCALE_DIR", tmp_path / "locale")
    kr_gu = [(12, 12000, "세종", None, None),          # 좌표 없음 — 그래도 KR 전수다
             (11, 11110, "종로구", 37.57, 126.98)]
    jp_gu = [(12, 12000, "大津市", 35.0, 135.8),        # KR 과 같은 키 — 신규가 아니다
             (11, 11110, "千代田区", 35.69, 139.75),
             (99, 99999, "武蔵野市", 35.70, 139.57)]    # KR 에 없는 유일한 신규 구역
    conn = _SeedEnbJpConn(kr_gu, jp_gu, dongs={99999: [99999001]})

    r = topo.seed_enb_jp(conn, "jp", band_start=900_000, write=True)

    assert r["gus"] == 1 and r["gus_jp_only"] == 1 and r["gus_no_coord"] == 0
    cell_tbl = topo.table_name("cell", "jp")
    placed = {int(row["GU_ID"]) for row in _inserted_rows(conn, cell_tbl)}
    assert placed == {99999}, placed       # 세종(12000)·종로구(11110) 는 없다


def test_seed_enb_jp_does_not_merge_gu_ids_across_different_cities(
        tmp_path, monkeypatch):
    """**`GU_ID` 단독 비교는 유일성을 보장하지 않는다.** `CITY_ID` 가 다르고
    `GU_ID` 만 같은 두 구역이 있을 때, KR 의 `(11, 99999)` 때문에 JP 의
    `(27, 99999)` 가 "이미 KR 에 있다"로 잘못 걸러지면 안 된다 — 둘은
    `(CITY_ID, GU_ID)` 쌍이 다른 서로 다른 구역이다.
    """
    monkeypatch.setattr(topo, "LOCALE_DIR", tmp_path / "locale")
    kr_gu = [(11, 99999, "임의구", 37.5, 127.0)]
    jp_gu = [(27, 99999, "福岡区", 33.5, 130.4)]        # GU_ID 만 같다 — 신규여야 한다
    conn = _SeedEnbJpConn(kr_gu, jp_gu, dongs={99999: [99999001]})

    r = topo.seed_enb_jp(conn, "jp", band_start=900_000, write=True)

    assert r["gus"] == 1 and r["gus_jp_only"] == 1 and r["enbs"] == 1
    cell_tbl = topo.table_name("cell", "jp")
    placed = {(int(row["CITY_ID"]), int(row["GU_ID"]))
              for row in _inserted_rows(conn, cell_tbl)}
    assert placed == {(27, 99999)}, placed


def test_seed_enb_jp_skips_coordless_jp_regions_and_reports_the_count():
    """**좌표 없는 JP 구역은 장비 대상에서 빠지고, 그 수가 보고된다**
    (불변규칙 12 의 결) — 조용히 빠지면 왜 비었는지 알 수 없다.
    """
    kr_gu = [(11, 11110, "종로구", 37.57, 126.98)]
    jp_gu = [(99, 99001, "좌표있음", 35.0, 135.0),
             (99, 99002, "좌표없음", None, None)]       # 신규지만 좌표가 없다
    conn = _SeedEnbJpConn(kr_gu, jp_gu, dongs={99001: [99001001], 99002: [99002001]})

    r = topo.seed_enb_jp(conn, "jp", band_start=900_000, write=False)

    assert r["gus_jp_only"] == 2        # 신규 구역은 둘
    assert r["gus"] == 1                # 장비를 놓은 것은 하나뿐
    assert r["gus_no_coord"] == 1
    assert r["gus_no_coord_sample"] == [(99, 99002)]


def test_gu_keys_and_gu_points_split_existence_from_coordinates():
    """**존재 판정은 `gu_keys` · 좌표는 `gu_points`.** 다음 사람이 같은
    자리에서 `gu_points` 로 존재를 물으면 좌표 없는 구가 "없다"로 읽힌다
    — 그 혼동이 676 을 678 로 되돌렸던 유일한 신호였다.
    """
    conn = _SeedEnbJpConn(
        kr_gu=[(12, 12000, "세종", None, None), (11, 11110, "종로구", 37.57, 126.98)],
        jp_gu=[])
    assert topo.gu_keys(conn, "kr") == {(12, 12000), (11, 11110)}
    assert {(c, g) for c, g, *_ in topo.gu_points(conn, "kr")} == {(11, 11110)}


def test_seed_enb_jp_regression_matches_the_production_count():
    """**회귀값** — 2026-10-27 적재 때 실측한 KR 273 · JP 949 구성에서
    추가 구역은 정확히 **676** 이어야 한다(`949 - 273`). `gu_points` 로
    존재를 물었을 때는 좌표 없는 KR 2구(세종·고양 일산구)가 신규로 잘못
    잡혀 678 이 나왔었다.
    """
    kr_gu = ([(c, 10000 + c * 100 + i, f"KR{c}-{i}", 37.0, 127.0)
              for c in range(1, 18) for i in range(16)]  # 17×16=272
             + [(1, 19999, "좌표없음", None, None)])      # +1 = 273, 좌표 없음
    jp_gu = (kr_gu[:272] + [kr_gu[272][:3] + (35.0, 135.0)]   # KR 273 과 같은 키(좌표는 채움)
             + [(50, 50000 + i, f"JP신규-{i}", 35.0, 135.0) for i in range(676)])
    conn = _SeedEnbJpConn(kr_gu, jp_gu,
                          dongs={g: [g * 1000 + 1] for _c, g, *_ in jp_gu})

    r = topo.seed_enb_jp(conn, "jp", band_start=900_000, write=False)

    assert r["gus_jp_only"] == 676
    assert r["gus"] == 676
    assert r["gus_no_coord"] == 0
    assert r["cells"] == 676 * 3            # cells_per_enb 기본값 3
