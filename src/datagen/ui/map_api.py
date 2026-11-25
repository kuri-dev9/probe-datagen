"""지도(`/map`) API — 셀·노선·레이어·장애 오버레이·사건 범위·행정 경계."""

from __future__ import annotations

import collections
import hashlib
import json
import math
import queue
import re
import threading
from datetime import datetime, timedelta
from pathlib import Path

import pymysql
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from ..control import T_FAULT
from ..geo_lite import dms_to_decimal
from . import common
from .common import HERE, _cells, _connect, _enb_names, _pem_cfg


router = APIRouter()


# ── 지도 — 셀과 **가입자가 실제로 나타난 자리** ──────────────────────────────


def _map_cells(conn) -> list[dict]:
    """생성 대상 셀의 좌표·장비. `_cells` 를 그대로 쓴다.

    **좌표가 없는 셀은 담지 않는다.** 지도에 찍을 수 없고, `0,0` 으로 두면
    기니만 바다에 점이 생긴다(불변규칙 17 · `tables.dms_to_decimal` 머리말).
    그 셀이 몇 개인지는 응답의 `unmapped` 가 말한다 — 숨기지 않는다
    (불변규칙 12).
    """
    out = []
    for c in _cells(conn):
        if not c.latitude or not c.longitude:
            continue
        out.append({
            "key": c.cell_key, "enb": c.enb_id, "cid": c.cell_id,
            "lat": round(c.latitude, 6), "lon": round(c.longitude, 6),
            "tac": c.tac, "ta_list": c.ta_list,
            "sgw": c.sgw_id, "mme": c.mme_id,
            "gu": c.adm_sgg_cd, "name": c.display_name,
        })
    return out


@router.get("/api/map")
def api_map():
    """**장비 위치** — 셀과 그것을 맡는 코어.

    ## 가입자를 보여주지 않는다 (2026-10-27 사람 결정)

    처음에는 가입자 자취를 함께 그렸는데 두 가지가 틀렸다 —

    1. **자취에 시간 순서가 없었다.** 창 안에 나온 셀을 모아 이었더니 순서가
       임의라 지도를 가로지르는 지그재그가 됐다. 원시 레코드에 시각이 있으니
       정렬할 수는 있지만, 그 전에 **보여줄 이유**가 먼저다.
    2. **같은 모양에 다른 뜻을 담았다.** 셀 점은 TA 색, 가입자 점은
       초록/노랑 — 둘 다 "점"이라 무엇을 보는지 섞였다.

    지금 알고 싶은 것은 **장비가 어디 있고 어떻게 묶였나**다. 가입자 이동은
    필요해지면 **별 화면**으로 둔다 — 한 화면에 두 축을 겹치지 않는다.

    ## 무엇을 내리나

    셀마다 좌표와 **네 가지 묶음 키**를 함께 내린다(`tac`·`ta_list`·`sgw`·
    `mme`). 화면이 그중 하나를 골라 색을 칠한다 — 축을 고를 수 있어야
    "이 점이 무슨 색인가"가 헷갈리지 않는다.

    코어 장비는 **자기가 맡은 셀의 중심**에 찍는다. 장비에 좌표가 따로 없고
    (레지스트리는 ID·이름만 들고 있다), 실제로 중요한 것도 "어디 놓였나"가
    아니라 **"어디를 덮나"** 다.
    """
    conn = _connect()
    try:
        cells = _map_cells(conn)
        allnm = _enb_names(conn)
    finally:
        conn.close()
    # 화면이 eNB 를 번호 대신 이름으로 부른다 — 그려지는 eNB 것만
    enb_names = {e: allnm[e] for e in {str(c["enb"]) for c in cells} if e in allnm}

    def centers(key: str) -> list[dict]:
        acc: dict[str, list[dict]] = collections.defaultdict(list)
        for c in cells:
            if c[key]:
                acc[str(c[key])].append(c)
        out = []
        for k, v in sorted(acc.items()):
            out.append({
                "id": k, "kind": key.upper(), "cells": len(v),
                "enb": len({x["enb"] for x in v}),
                "lat": round(sum(x["lat"] for x in v) / len(v), 6),
                "lon": round(sum(x["lon"] for x in v) / len(v), 6),
            })
        return out

    sc = (_pem_cfg().get("scope") or {})
    gu = [str(x) for x in (sc.get("gu") or [])]
    names = {}
    if gu:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT GU_ID, GU_NAME FROM CM_GU_JP WHERE GU_ID IN ("
                            + ",".join(["%s"] * len(gu)) + ")", gu)
                names = {str(a): b for a, b in cur.fetchall()}
        finally:
            conn.close()
    # 구가 많으면 이름을 다 적지 않는다 — 12개면 머리말이 화면을 덮는다.
    # **몇 곳인지는 적는다**(잘랐다는 사실을 숨기지 않는다 · 불변규칙 12)
    gn = [names.get(g, g) for g in gu]
    parts = gn if len(gn) <= 3 else [f"{gn[0]} 외 {len(gn) - 1}곳"]
    if sc.get("dong"):
        parts.append(f"동 {len(sc['dong'])}곳")
    if sc.get("route"):
        parts.append(str(sc["route"]))
    stations, lines = _map_stations(cells)
    stations = _station_labels(stations)
    # **바이트로 내보낸다**(2026-11-24 사용자 「전체 지도 로딩이 시간 좀 걸린다」) — 1.1MB 를 FastAPI 기본
    # 직렬화(`jsonable_encoder`)로 내면 135ms, `json.dumps` 는 16ms 다. 첫 화면은 이 응답을 기다린다
    doc = {
        "scope": " + ".join(parts) or "전량",
        "cells": cells,
        "stations": stations,
        "enb_names": enb_names,
        "lines": lines,
        "unmapped": max(0, len(common._CELLS or ()) - len(cells)),
        "nodes": {"sgw": centers("sgw"), "mme": centers("mme")},
        "counts": {
            "enb": len({c["enb"] for c in cells}),
            "tac": len({c["tac"] for c in cells if c["tac"]}),
            "ta_list": len({c["ta_list"] for c in cells if c["ta_list"]}),
            "sgw": len({c["sgw"] for c in cells if c["sgw"]}),
            "mme": len({c["mme"] for c in cells if c["mme"]}),
        },
    }
    return Response(json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode(),
                    media_type="application/json")


#: 노선 색 — JR 공식 라인컬러. 화면은 `.rl-ymt` 가 같은 값을 들고 있다
#: (실제 선로를 칠하는 쪽이라 CSS 에 있어야 한다). 여기 값은 다른 노선을
#: 추가할 때의 자리표다.
_LINE_COLOR = {"YMT": "#9acd32"}


def _map_stations(cells: list[dict]) -> tuple[list[dict], list[dict]]:
    """역 위치와 **노선 선** — 노선 파일이 이미 들고 있다.

    `data/lines/*.json` 의 `station_of` 가 칸마다 역 id 를 적고 `cells` 가
    같은 순서로 셀 키를 적으므로, 역에 딸린 셀들의 **좌표 평균**이 곧 역
    자리다. 정확한 역사 좌표가 아니라 **그 역을 덮는 셀의 중심**이다 —
    실측으로 실제 역사와 **중앙값 73m · 최대 238m** 떨어져 있다
    (`tools/build_line_route.py` 의 `ST` 와 대조). 지도에서 "여기가
    신주쿠쯤"을 읽는 용도고, 그 이상으로 쓰면 안 된다.

    `lines` 는 역을 `station_pos` 순서로 이은 선이다. **이제 이것을 그리지
    않는다**(2026-10-28) — 실제 선로 레이어(`/api/map/layer/rail`)가 들어온
    뒤로 둘이 겹쳐 보이는데 직선 쪽이 틀린 그림이다. 지금 쓰임은 **실제
    선로에서 야마노테 구간을 집는 자**다(화면의 `markYamanote`).

    노선 파일이 없으면 빈 목록이다. 곁다리라 없어도 화면이 선다.
    """
    pos = {c["key"]: c for c in cells}
    acc: dict[str, list[dict]] = collections.defaultdict(list)
    routes = []
    try:
        d = Path(str(_pem_cfg().get("lines_dir") or "data/lines"))
        if not d.is_absolute():
            d = (HERE / d).resolve()
        for f in sorted(d.glob("*.json")):
            routes += json.loads(f.read_text(encoding="utf-8"))
        for route in routes:
            names = route.get("stations") or {}
            keys = route.get("cells") or []
            for key, sid in zip(keys, route.get("station_of") or []):
                c = pos.get(key)
                if c and names.get(sid):
                    acc[names[sid]].append(c)
    except Exception:
        return [], []      # 지도의 곁다리다 — 실패해도 장비는 보여야 한다

    st = {k: (round(sum(x["lat"] for x in v) / len(v), 6),
              round(sum(x["lon"] for x in v) / len(v), 6), len(v))
          for k, v in acc.items()}

    lines, seen = [], set()
    for route in routes:
        names = route.get("stations") or {}
        order = sorted((route.get("station_pos") or {}).items(),
                       key=lambda kv: kv[1])
        pts = [[st[names[sid]][1], st[names[sid]][0]]
               for sid, _ in order if names.get(sid) in st]
        if len(pts) < 2:
            continue
        # 외선·내선은 **같은 선로다.** 둘 다 그리면 두 겹이 된다
        rid = str(route.get("id") or "").split("-")[0]
        if rid in seen:
            continue
        seen.add(rid)
        # 외선·내선 꼬리를 뗀다 — 선은 하나고 방향은 선의 성질이 아니다
        nm = re.sub(r"\s*[가-힣]*순환\([^)]*\)", "",
                    str(route.get("name") or rid).split(" — ")[0]).strip()
        lines.append({"id": rid, "name": nm or rid,
                      "color": _LINE_COLOR.get(rid, "#6b7684"),
                      "loop": bool(route.get("loop")), "pts": pts})

    return ([{"name": k, "lat": v[0], "lon": v[1], "cells": v[2]}
             for k, v in sorted(st.items())], lines)


#: 배경 레이어 — 설정 키 ↔ 파일. **경로는 각자 자기 키로 받는다.**
#: 전에 `geo_crosswalk` 의 형제에서 찾다가 로케일별로 디렉터리가 달라
#: 컨테이너에서 파일을 못 찾았다(§1.15). 같은 실수를 늘리지 않는다.
#: 배경 레이어 — 설정 키와 **출처 문구**. 출처는 레이어마다 다르다
#: (약관 의무라 화면이 받은 것만 표기한다). 도로·녹지는 OSM 이다 —
#: 국토수치정보의 도로(`N01`)가 1995년판 0.12MB 라 쓸 수 없었다
#: (`tools/build_jp_geo_layers.py` 머리말).
_LAYER = {
    "rail":   ("geo_rail",   "国土数値情報 (国土交通省) · 鉄道 N02"),
    "rivers": ("geo_rivers", "国土数値情報 (国土交通省) · 河川 W05"),
    "roads":  ("geo_roads",  "© OpenStreetMap contributors"),
    "green":  ("geo_green",  "© OpenStreetMap contributors"),
    "station_bldg": ("geo_station_bldg", "© OpenStreetMap contributors"),
    # 역 승강장 선 (2026-11-24) — 철도와 같은 N02 라 역 위치·길이가 철로와 맞는다
    "station_n02": ("geo_station_n02", "国土数値情報 (国土交通省) · 鉄道 N02"),
    # 역 모양 (2026-11-24 저녁) — OSM 승강장 면·구내·건물, 없는 역만 N02 승강장 선
    "station_osm": ("geo_station_osm", "© OpenStreetMap contributors"),
    # 상세 배경 (2026-10-30) — 굽는 자리는 graphio `tools/build_jp_geo_layers.py`
    "water":  ("geo_water",  "© OpenStreetMap contributors"),
    "chome":  ("geo_chome",  "e-Stat 境界データ (令和2年国勢調査 小地域)"),
}

#: 3D 재생만 받는 레이어 (2026-11-25) — 지도 첫 로딩의 `LAYERS` 에도, `/api/map/geo` 의 범위·캐시 키에도
#: 넣지 않는다. 높은 건물은 셀 장애 범위 둘레를 세울 때만 쓴다
_LAYER_3D = {
    "building_tall": ("geo_building_tall", "© OpenStreetMap contributors"),
}


#: 낮은 건물 칸 — 한 번에 받을 수 있는 칸 수 상한. 1km 축척 × 반경 5km 면 약 120칸이다. **초기값이다**
_BLDG_MAX_CELLS = 400
_BLDG_CODE = re.compile(r"^\d{8}$")


def _bldg_dir():
    """`pemdb1.geo_bldg_dir` → (디렉터리, 오류). 굽는 자리는 graphio `tools/build_jp_geo_layers.py --only-building-grid`"""
    raw = str(_pem_cfg().get("geo_bldg_dir") or "")
    if not raw:
        return None, "`pemdb1.geo_bldg_dir` 가 비어 있다"
    d = Path(raw)
    if not d.is_absolute():
        d = (HERE / d).resolve()
    if not (d / "index.json").exists():
        return None, f"건물 칸 색인이 없다: {d / 'index.json'}"
    return d, None


def _gz(request: Request, body: bytes) -> Response:
    """받는 쪽이 gzip 을 받으면 눌러 보낸다 — 칸 파일은 정수 좌표라 약 1/4 로 준다. 1단계 압축(빠르다)"""
    import gzip
    if "gzip" in (request.headers.get("accept-encoding") or ""):
        return Response(gzip.compress(body, 1), media_type="application/json",
                        headers={"Content-Encoding": "gzip", "Vary": "Accept-Encoding"})
    return Response(body, media_type="application/json")


@router.get("/api/map/bldg/index")
def api_map_bldg_index(request: Request):
    """낮은 건물 칸 색인 — `{mesh, min_m, min_levels, q, total, cells: {메시: [건물 수, 높은 건물 수]}}` (2026-11-25).

    3D 재생 전용이다(`_LAYER_3D` 와 같은 결 — 지도 첫 로딩에 넣지 않는다). 3D 가 보이는 범위의 칸 코드를 정하고
    `/api/map/bldg?cells=` 로 그 칸만 받는다. 칸 = JIS 3차 메시(약 1km)"""
    d, err = _bldg_dir()
    if err:
        return {"error": err, "cells": {}}
    return _gz(request, _file_bytes(d / "index.json"))


@router.get("/api/map/bldg")
def api_map_bldg(request: Request, cells: str = ""):
    """낮은 건물 — 고른 칸 파일들을 한 응답으로 `{"cells": {메시: 칸 파일}, "missing": [...], "source"}`.

    칸 파일 모양은 graphio `_build_building_grid` 머리말(남서 모서리 `o` + 정수 좌표). 건물이 없는 칸(물·공원)은
    파일이 없어 `missing` 에 든다 — 오류가 아니다. 파일을 파싱하지 않고 그대로 이어 붙인다(`/api/map/layer` 와 같은 까닭)"""
    d, err = _bldg_dir()
    if err:
        return {"error": err, "cells": {}}
    codes = [c for c in dict.fromkeys(x.strip() for x in cells.split(",")) if c]
    bad = [c for c in codes if not _BLDG_CODE.match(c)]
    if bad:
        raise HTTPException(400, f"칸은 JIS 3차 메시 8자리입니다: {bad[:5]}")
    if len(codes) > _BLDG_MAX_CELLS:
        raise HTTPException(400, f"한 번에 {_BLDG_MAX_CELLS}칸까지입니다: {len(codes)}")
    parts, missing = [], []
    for c in codes:
        f = d / f"{c}.json"
        if f.exists():
            parts.append(json.dumps(c).encode() + b":" + _file_bytes(f))
        else:
            missing.append(c)
    body = (b'{"cells":{' + b",".join(parts) + b'},"missing":' + json.dumps(missing).encode()
            + b',"source":' + json.dumps("© OpenStreetMap contributors", ensure_ascii=False).encode() + b'}')
    return _gz(request, body)


@router.get("/api/map/layer/{name}")
def api_map_layer(name: str):
    """철도·하천 — **국토수치정보를 구워 둔 로컬 파일이다.**

    행정경계 한 장만으로는 "지도 같지 않다"는 말을 들었다. 같은 출처가
    같은 약관으로 철도(N02)·하천(W05)을 내므로 `tools/build_jp_geo_layers.py`
    가 한 번 받아 굽고 여기서는 읽기만 한다 — 실행 시점에 외부를 부르지
    않는 원칙은 그대로다(`/api/map/geo` 머리말).

    **철도는 실제 선형이다.** 화면의 야마노테선 초록 선은 역을 직선으로
    이은 **모사 경로**고, 이 레이어가 그 아래 깔리는 **실제 선로**다.
    둘을 겹쳐 두면 모사가 어디서 눌렸는지 보인다.

    **도로·녹지만 출처가 다르다**(2026-10-28). 국토수치정보의 도로는
    `N01` 이 1995년판 0.12MB 라 간선 몇 줄뿐이고, 최신 `N13` 은 긴급수송
    도로라 가로망이 아니다. 그래서 그 둘은 OSM 에서 굽는다 — ODbL 이라
    화면이 `© OpenStreetMap contributors` 를 함께 적는다. 받는 것은 빌드
    시점 한 번이고 **런타임이 외부를 부르지 않는 원칙은 그대로다.**
    """
    if name == "station":
        # **화면은 이 이름 하나로 받는다** — 어느 역 레이어를 그릴지는 설정
        # 한 줄(`pemdb1.map_station_layer`)이 정한다(2026-11-24). OSM 건물
        # 윤곽은 東京·上野·品川 본 역사가 빠져 N02 로 바꿨고, 되돌릴 길로 남긴다
        # 기본은 OSM 역 모양이다 — N02 승강장 선은 「역 위치에 선만 그은 것」이었다(사용자 11-24)
        name = str(_pem_cfg().get("map_station_layer") or "station_osm")
        if name not in ("station_osm", "station_n02", "station_bldg"):
            raise HTTPException(404, f"역 레이어는 station_osm · station_n02 · station_bldg 중 하나입니다: {name}")
    hit = _LAYER.get(name) or _LAYER_3D.get(name)
    if not hit:
        raise HTTPException(404, f"그런 레이어가 없습니다: {name}")
    key, source = hit
    src, err = _layer_path(key)
    if err:
        # 빈 배열을 돌려주지 않는다 — "데이터가 없다"와 "파일이 없다"는
        # 다른 사실이고, 전자로 보이면 왜 비었는지 알 수 없다
        return {"error": err, "items": []}
    # **파싱하지 않고 그대로 싼다**(2026-11-03 실측). 전에는 `json.loads` 로
    # 읽어 FastAPI 가 다시 직렬화했다 — 일곱 개를 동시에 받으면 서로 GIL 을
    # 다퉈 철도 1MB 하나에 1초가 걸렸다. 파일이 이미 JSON 배열이다
    body = b'{"items":' + _file_bytes(src) + b',"source":' \
        + json.dumps(source, ensure_ascii=False).encode() + b'}'
    return Response(body, media_type="application/json")


#: 역 글자를 이 안의 같은 이름 글자 자리로 옮긴다(m). 셀 중심은 실제 역에서 최대 약 700m(浜松町) 떨어져 있었다
_LABEL_MATCH_M = 1500.0


def _label_extra(lb: dict) -> dict:
    """글자 자리에서 함께 싣는 것 — 한글 이름(`ko` · OSM `name:ko`)과 야마노테선 여부(`ymt` · 글자 색).

    2026-11-24 사용자 「역명이 한글로 안 나온다」·「야마노테선만 녹색, 나머지 역은 회색」. 없으면 싣지 않는다."""
    ex = {}
    if lb.get("ko"):
        ex["ko"] = lb["ko"]
    if "山手線" in (lb.get("lines") or ()):
        ex["ymt"] = True
    # 등급(`tier`) — 축척에 따라 역 이름을 나눠 보인다(사용자 11-24 「역 이름이 너무 많다, 중요도로 3등분」).
    # 1 = 야마노테(화면이 노선 자료의 30역으로도 1로 올린다) · 2 = 서로 다른 노선 `_TIER2_LINES` 개 이상 · 3 = 나머지
    nl = len(_line_names(lb.get("lines") or ()))
    ex["tier"] = 1 if ex.get("ymt") else 2 if nl >= _TIER2_LINES else 3
    # 노선 수만으로 본 등급 — 야마노테 역도 환승역(2)인지 아닌지(3). 3D 재생이 「1km 축척 안에서는 전부, 그 밖은 주요 역만」을
    # 야마노테 역에도 건다(사용자 11-25). 2D 지도는 `tier` 만 본다
    ex["tl"] = 2 if nl >= _TIER2_LINES else 3
    return ex


#: 2등급(환승역)으로 보는 서로 다른 노선 수. **초기값이다**
_TIER2_LINES = 3


def _line_names(lines) -> set[str]:
    """노선 이름 정리 — N02 는 같은 노선의 갈래를 「…分岐線」으로 따로 적는다(丸ノ内線·丸ノ内線分岐線). 한 노선으로 센다."""
    return {str(x).removesuffix("分岐線") for x in lines if x}


def _station_labels(stations: list[dict]) -> list[dict]:
    """역 이름 **글자 자리**(`at`)를 붙인다 — 실제 역 모양의 가운데 (2026-11-24).

    `lat`·`lon` 은 그 역을 덮는 셀들의 중심 그대로 둔다 — 노선 선(`lines`)과 야마노테 집기가
    그 값을 쓴다. 글자만 graphio `station_label.json`(N02 역 + OSM 모양 면적 중심)으로 옮긴다.

    **목록에 없는 역**(셀이 안 붙은 야마노테 6역, 생성 범위 밖의 역)은 글자만
    더한다(`label_only`). 노선 파일·셀 귀속과는 무관한 화면용 항목이다. 파일이 없으면 그대로 돌려준다.
    """
    src, err = _layer_path("geo_station_label")
    if err:
        return stations
    labels = _cached(src, "json", lambda p: json.loads(p.read_text(encoding="utf-8")))
    by: dict[str, list] = collections.defaultdict(list)
    for lb in labels:
        by[lb["name"]].append(lb)

    def dm(a, b):
        return math.hypot((a[0] - b[0]) * 90400, (a[1] - b[1]) * 111000)

    out = []
    for st in stations:
        st = dict(st)
        c = [(dm((lb["lon"], lb["lat"]), (st["lon"], st["lat"])), lb) for lb in by.get(st["name"], ())]
        c = [x for x in c if x[0] <= _LABEL_MATCH_M]
        if c:
            lb = min(c, key=lambda x: x[0])[1]
            st["at"] = [lb["lon"], lb["lat"]]
            st.update(_label_extra(lb))
        out.append(st)
    # 목록에 없는 역은 **모두** 글자만 더한다(2026-11-24 — 생성 범위 밖 江戸川·中野 등은 역 모양만 있고 이름이
    # 없었다). 같은 이름이 `_LABEL_MATCH_M` 안에 이미 있으면 같은 역이라 더하지 않는다 — 멀리 떨어진 동명 역은 더한다
    have: dict[str, list] = collections.defaultdict(list)
    for st in out:
        have[st["name"]].append((st["lon"], st["lat"]))
    for lb in labels:
        p = (lb["lon"], lb["lat"])
        if any(dm(p, q) <= _LABEL_MATCH_M for q in have.get(lb["name"], ())):
            continue
        out.append({"name": lb["name"], "lat": lb["lat"], "lon": lb["lon"], "cells": 0,
                    "at": [lb["lon"], lb["lat"]], "label_only": True, **_label_extra(lb)})
        have[lb["name"]].append(p)
    return out


def _layer_path(key: str):
    """설정 키 → (경로, 오류). 상대 경로는 이 파일 기준이다."""
    raw = str(_pem_cfg().get(key) or "")
    if not raw:
        return None, f"`pemdb1.{key}` 가 비어 있다"
    src = Path(raw)
    if not src.is_absolute():
        src = (HERE / src).resolve()
    if not src.exists():
        return None, f"레이어 파일이 없다: {src}"
    return src, None


#: 지도 배경 파일 캐시 — (경로, mtime) 가 같으면 다시 읽지 않는다. 파일을
#: 다시 구우면 mtime 이 바뀌어 저절로 새로 읽는다
_FILE_CACHE: dict = {}


def _cached(src: Path, tag: str, make):
    k = (str(src), tag)
    mt = src.stat().st_mtime
    hit = _FILE_CACHE.get(k)
    if hit and hit[0] == mt:
        return hit[1]
    v = make(src)
    _FILE_CACHE[k] = (mt, v)
    return v


def _file_bytes(src: Path) -> bytes:
    return _cached(src, "bytes", lambda p: p.read_bytes())


def _layer_extent():
    """레이어 전부의 좌표 범위 (lat0, lon0, lat1, lon1) — 없으면 None.

    땅(행정경계)은 그 위에 얹힌 레이어를 다 덮어야 한다(`/api/map/geo` 의
    `cover`). 화면이 레이어를 다 받은 뒤 범위를 재서 다시 묻던 때는 경계가
    레이어 뒤에 줄을 서 0.4초가 늦었다 — 서버가 파일에서 직접 잰다
    """
    box = [1e9, 1e9, -1e9, -1e9]

    def ext(p: Path):
        b = [1e9, 1e9, -1e9, -1e9]

        def walk(o):
            if isinstance(o, list):
                if len(o) == 2 and all(isinstance(v, (int, float)) for v in o):
                    b[0] = min(b[0], o[1]); b[2] = max(b[2], o[1])
                    b[1] = min(b[1], o[0]); b[3] = max(b[3], o[0])
                else:
                    for v in o:
                        walk(v)
            elif isinstance(o, dict):
                for v in o.values():
                    walk(v)
        walk(json.loads(p.read_text(encoding="utf-8")))
        return b

    for key, _ in _LAYER.values():
        src, err = _layer_path(key)
        if err:
            continue
        b = _cached(src, "extent", ext)
        box = [min(box[0], b[0]), min(box[1], b[1]), max(box[2], b[2]), max(box[3], b[3])]
    return None if box[0] > box[2] else box


#: 심각도 순위 — 한 장비에 여러 Incident 가 걸리면 **가장 심한 것**이
#: 색을 정한다. graphio `AI_INCIDENT.SEVERITY` 의 값 집합이다.
_SEV_RANK = {"INFO": 1, "WARN": 2, "WARNING": 2, "MINOR": 2,
             "MAJOR": 3, "CRITICAL": 4}


@router.get("/api/map/faults")
def api_map_faults(minutes: int = 15, recent: bool = False):
    """지도에 뿌릴 **장애 현황** — graphio 에서 HTTP 로 받는다.

    ## 왜 DB 를 직접 읽지 않나

    `AI_INCIDENT`·`AI_RCA_RESULT` 는 **graphio 소유**다(`pemdb1.py` 의
    "분석 산출물은 넣지 않는다"). 같은 `pemdb6` 에 있어 SELECT 는 되지만,
    그러면 두 저장소가 한 스키마를 공유하게 되고 graphio 가 컬럼을 바꿀 때
    **에러 없이 빈 화면**이 된다. API 를 거치면 민감 필드 차단(불변규칙 13)도
    그쪽 경계가 그대로 한다.

    ## graphio 가 없어도 지도는 선다

    `web` 컨테이너는 `profiles: ["svc"]` 라 안 떠 있을 수 있다. 닿지 않으면
    `error` 를 담아 내리고 **화면은 장애 표시만 비운다** — 레이어 파일이
    없을 때와 같은 방식이고, 빈 배열로 내리면 "장애가 없다"로 읽힌다
    (불변규칙 17).

    ## `ver` 는 **장비별 심각도**의 해시다

    화면이 10초마다 부르는데 매번 다시 그리면 눌러 둔 선택이 풀리고 깜빡인다.
    `ver` 가 같으면 화면은 아무것도 하지 않는다. Incident 본문이 아니라
    **장비→심각도** 만 넣는다 — 서술이 L3 로 갱신돼도 지도 그림은 같다.
    """
    base = str(_pem_cfg().get("graphio_api") or "").rstrip("/")
    if not base:
        return {"error": "`pemdb1.graphio_api` 가 비어 있습니다 — graphio API "
                         "주소를 설정에 적어야 합니다", "incidents": []}

    import urllib.error
    import urllib.request

    # `recent=1` — 장애·시나리오 화면에서 회차를 눌러 넘어온 지도만 쓴다. 최근 200건으로는 며칠 지난
    # 회차가 목록에 없어 지도가 그 이력을 고르지 못한다(2026-11-09). 평소 지도는 그대로 200건이다
    url = (f"{base}/api/incidents?scope=1&recent=1" if recent
           else f"{base}/api/incidents?scope=1&limit=200")
    # **기다림은 8초다**(2026-11-24 · 사용자 「자주 뜬다」). 3초였는데 web 은 평소 0.2초여도 동시 요청 10개에 1.2초까지
    # 늘고, L3 질의가 증거를 모으는 동안은 더 늦어 3초를 넘겼다. 같은 graphio 를 부르는 사건 지도(`/map` 8초)와
    # 장애 목록(`faults_api` 10초)에 맞춘다 — 화면은 10초마다 부르므로 8초면 다음 호출과 겹치지 않는다
    try:
        with urllib.request.urlopen(url, timeout=8) as r:
            doc = json.loads(r.read())
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        # **무엇이 안 되는지 말한다.** "장애 0건"과 "물어볼 데가 없다"는
        # 다른 사실이고, 전자로 보이면 아무도 이상하다고 생각하지 않는다
        return {"error": f"graphio API 에 닿지 않습니다 ({url}): {e}",
                "incidents": []}

    # ## "지금 장애"는 **마지막으로 관측된 시각**으로 가른다 (2026-10-28)
    #
    # graphio 는 Incident 를 닫지 않는다 — 주입이 끝나 이상이 사라져도
    # `status` 는 OPEN 이고 `closed_ts` 는 비어 있다. 그래서 상태로 거르면
    # **끝난 장애가 계속 장애로 남았다.** `updated_ts` 는 이상이 마지막으로
    # 관측된 분이다(graphio 불변규칙 9 — 관측 사실). 그것이 창 안에 있는
    # 것만 "지금"으로 본다.
    #
    # 전에는 `opened_time`·`updated_time` 을 읽고 있었는데 graphio 응답
    # 키는 `*_ts` 다. **에러 없이 전부 None** 이라 시각 필터가 한 번도
    # 돌지 않았다. 영향 가입자(`affected_subscribers`)도 같은 이유로 늘
    # "산출 불가"였다
    cutoff = datetime.now() - timedelta(minutes=minutes)
    latest = None
    out, enb, node, sgg = [], {}, {}, {}
    # **장애 이력** — 창과 무관하게 graphio 가 준 것 전부(최근 연 순 200건).
    # 지도 색은 "지금" 것만 칠하지만, 목록은 끝난 장애도 눌러서 **그때
    # 어느 장비가 걸렸는지** 다시 볼 수 있어야 한다(사람 요청 2026-10-30).
    # 건마다 걸린 eNB·코어를 미리 풀어 둔다 — 화면이 scopes 를 다시 해석하면
    # 위 `mark` 규칙과 두 벌이 된다
    hist = []
    for i in doc.get("incidents", []):
        ts = str(i.get("closed_ts") or i.get("updated_ts") or i.get("opened_ts") or "")[:19]
        try:
            at = datetime.fromisoformat(ts) if ts else None
        except ValueError:
            at = None
        if at and (latest is None or at > latest):
            latest = at
        h_enb, h_node = set(), set()
        for sc in i.get("scopes") or []:
            kind, sid = sc.get("scope_type"), str(sc.get("scope_id") or "")
            if kind == "CELL" and "-" in sid:
                h_enb.add(sid.rsplit("-", 1)[0])
        for ax in (i.get("detail") or {}).get("axis_ids") or []:
            t, _, nid = str(ax).partition("-")
            if t.lower() in ("sgw", "mme", "pgw") and nid:
                h_node.add(f"{t.lower()}:{nid}")
        # 시각을 모르면 **지금으로 본다** — 모르는 것을 "끝났다"로 읽지 않는다
        live = not (at and at < cutoff)
        # **지리 축** — 체감(XP) 장애는 장비가 아니라 구역(메시·행정구역)으로
        # 온다(graphio `detector` 의 `detail.axis`·`axis_ids`). 장비 범위가
        # 없다고 "지도에 없음"으로 두던 것을 면으로 그린다(사람 결정 2026-10-30)
        det = i.get("detail") or {}
        axis = str(det.get("axis") or "")
        geo_ids = ([str(x) for x in det.get("axis_ids") or []]
                   if axis in _GEO_AXES else [])
        hist.append({**{k: i.get(k) for k in (
            "incident_id", "severity", "status", "title", "trigger_kpi",
            "opened_ts", "updated_ts", "closed_ts", "root_code", "root_label",
            "centroid_lat", "centroid_lon", "cell_cnt", "rca_id",
            # 판정 단위 — 값은 graphio 목록 그대로다(L2 사건·L1 묶음·조각)
            "confidence", "event_unit", "event_shape", "event_members_cnt")},
            "live": live, "enbs": sorted(h_enb), "nodes": sorted(h_node),
            "axis": axis if geo_ids else None, "geo_ids": geo_ids,
            # 위치 없는 축도 **무슨 축인지는** 화면이 말해야 한다
            "axis_raw": axis or None,
            "axis_vals": [str(x) for x in det.get("axis_ids") or []][:GEO_LIST]})
        if not live:
            continue
        iid, sev = i.get("incident_id"), (i.get("severity") or "")
        out.append({k: i.get(k) for k in (
            "incident_id", "severity", "status", "title", "trigger_kpi",
            "opened_ts", "updated_ts", "closed_ts",
            "root_code", "root_label", "statement_ko", "confidence",
            "centroid_lat", "centroid_lon", "radius_km", "cell_cnt",
            "affected_subscribers", "impact_ratio", "spread_trend")})

        def mark(bucket: dict, key: str) -> None:
            bucket.setdefault(key, {"sev": sev, "ids": []})
            # 한 장비에 여러 건이면 **가장 심한 것**이 색을 정한다
            if _SEV_RANK.get(sev, 0) > _SEV_RANK.get(bucket[key]["sev"], 0):
                bucket[key]["sev"] = sev
            bucket[key]["ids"].append(iid)

        for sc in i.get("scopes") or []:
            kind, sid = sc.get("scope_type"), str(sc.get("scope_id") or "")
            if kind == "CELL" and "-" in sid:
                mark(enb, sid.rsplit("-", 1)[0])    # 셀 → 그 자리(eNB)
            elif kind == "SGG":
                mark(sgg, sid)
        # **코어는 `scopes` 에 없다.** 축이 NODE 인 Incident 는 장비 ID 를
        # `detail.axis_ids` 에 `SGW-0032` 꼴로 담는다
        # (`detector._node_key` = `node_type || '-' || node_id`)
        for ax in (i.get("detail") or {}).get("axis_ids") or []:
            t, _, nid = str(ax).partition("-")
            if t.lower() in ("sgw", "mme", "pgw") and nid:
                mark(node, f"{t.lower()}:{nid}")

    _attach_dong(hist)
    _attach_bias(base, hist)
    # 편향이 없는 APN 은 **PGW 에 붙인다**(사람 결정 2026-11-05) — 지역이 없고 APN 은
    # PGW 에서 끝나는 경로다. 편향이 있으면 그 코어 장비가 먼저다
    for h in hist:
        if h.get("axis_raw") == "APN" and not h.get("bias"):
            h["egress"] = True
    # 장비→심각도만 해시한다 (위 docstring)
    # 건 목록도 넣는다 — 범위 없는 Incident 가 들고 나도 건수는 바뀐다
    stamp = json.dumps(
        [{k: v["sev"] for k, v in sorted(b.items())} for b in (enb, node, sgg)]
        + [sorted(str(o["incident_id"]) for o in out)]
        + [[h["incident_id"] for h in hist]]
        # 뒤에서 채워진 편향·DNS 도 넣는다 — 안 넣으면 `ver` 가 같아 화면이 받지 않는다
        + [[h["incident_id"] for h in hist if h.get("bias") or h.get("dns")]],
        ensure_ascii=False, sort_keys=True)
    return {"ver": hashlib.sha1(stamp.encode()).hexdigest()[:12],
            "at": datetime.now().isoformat(timespec="seconds"),
            "minutes": minutes,
            # graphio 가 마지막으로 이상을 본 시각 — 창이 비었을 때 "장애가
            # 없다"와 "분석이 멈췄다"를 사람이 가를 수 있게
            "latest": latest.isoformat(timespec="minutes") if latest else None,
            "incidents": out, "enb": enb, "node": node, "sgg": sgg,
            "history": hist}


@router.get("/api/map/event/{incident_id}")
def api_map_event(incident_id: str, states: bool = False):
    """사건 **전체** 범위 — 대표 조각이 아니라 구성 조각 합집합.

    이력 목록의 `enbs` 는 대표 조각 하나의 범위라, 조각 4건·셀 118개 사건이 5건으로
    보이고 셀 6,872개 사건은 0건으로 보였다. graphio `/api/incidents/{id}/map` 은
    셀을 자르지 않고 준다(`event.cells` 는 500 에서 잘린다). 사건마다 수십 KB 라
    목록에 싣지 않고 **누를 때만** 묻는다. 같은 eNB 의 셀은 좌표가
    같아 eNB 하나당 점 하나만 내린다.
    """
    import urllib.error
    import urllib.parse
    import urllib.request

    base = str(_pem_cfg().get("graphio_api") or "").rstrip("/")
    if not base:
        return {"error": "`pemdb1.graphio_api` 가 비어 있습니다", "enbs": [], "pts": []}
    # 여기서 쓰는 것은 셀과 칸 코드뿐이다 — 둘레·사건 지리 계산을 시키지 않는다
    url = (f"{base}/api/incidents/{urllib.parse.quote(incident_id)}/map"
           "?cells_only=true&with_areas=true&with_members=true")
    try:
        with urllib.request.urlopen(url, timeout=8) as r:
            doc = json.loads(r.read())
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        return {"error": f"graphio API 에 닿지 않습니다 ({url}): {e}", "enbs": [], "pts": []}
    at: dict[str, list] = {}
    cells = doc.get("cells") or []
    for c in cells:
        at.setdefault(str(c.get("key") or "").rsplit("-", 1)[0], [c.get("lon"), c.get("lat")])
    raw = doc.get("members") or []
    members = _event_members(raw, cells + list(doc.get("detached_cells") or []))
    silent, radius = _silent_cells(raw)
    for m in members:
        m["silent"] = silent.get(str(m.get("incident_id")), [])
    out = {"enbs": sorted(at), "pts": list(at.values()), "cells": len(cells),
           "unmapped": doc.get("unmapped"), "areas": _event_areas(doc.get("areas") or []),
           "areas_capped": bool(doc.get("areas_capped")),
           "members": members, "silent_r": radius}
    # 셀 상태 음영(지진 화면만 묻는다 — `?states=1`). 사건 셀 전부를 DB 에 묻게 되므로 셀 수천 개짜리
    # DNS 사건 목록 요청에는 싣지 않는다
    if states:
        sev, src, cell_r = _cell_severity(raw)
        for m in members:
            iid = str(m.get("incident_id"))
            m["cell_sev"] = {k: v["sev"] for k, v in sev.get(iid, {}).items()}
            m["cell_kpi"] = {k: v["kpi"] for k, v in sev.get(iid, {}).items()}
            m["sev_src"] = src.get(iid, {})
        out["cell_r"] = cell_r
    return out


def _silent_cells(rows: list[dict]) -> tuple[dict[str, list[str]], dict[str, float | None]]:
    """구성 조각마다 **무호(C-01)로 판정된 셀** — 지도의 「정지 판정 영역」(2026-11-15 사용자 "음영을 기지국
    장애와 함께 묶어서, 발생 위치로부터 어디까지 피해가 보는지").

    graphio 가 쓴 D4 이벤트(`AI_ANOMALY_EVENT` · `DETECTOR='D4'` · `KPI='C-01'` · 셀 단위)를 **읽기만** 한다 —
    트리거에 C-01 이 있는 조각의 셀 중 그 조각의 구간(`opened_ts`~`updated_ts`)에 무호 이벤트가 선 셀이다.
    폭주(C-24)만 있는 셀은 꺼진 것이 아니라 붐비는 것이라 들어가지 않는다. 화면이 진도·거리로 추정하지 않는다.
    반경은 정답 음영과 같은 셀별 추정 커버리지(`_cell_radius`). 못 읽으면 빈 값이다 — 칠하지 않는다
    """
    want = []
    for m in rows:
        if "C-01" not in str(m.get("trigger_kpi") or "").split(","):
            continue
        keys = sorted({str(k) for k in m.get("cells") or ()})
        lo = str(m.get("opened_ts") or "").replace("T", " ")[:19]
        hi = str(m.get("updated_ts") or "").replace("T", " ")[:19]
        if keys and lo:
            want.append((str(m.get("incident_id")), keys, lo, hi))
    out: dict[str, list[str]] = {}
    if not want:
        return out, {}
    conn = _connect()
    try:
        with conn.cursor() as cur:
            for iid, keys, lo, hi in want:
                cur.execute("SELECT DISTINCT SCOPE_ID FROM AI_ANOMALY_EVENT "
                            "WHERE DETECTOR = 'D4' AND KPI = 'C-01' AND SCOPE_TYPE = 'CELL' "
                            "AND COALESCE(CANCELLED, 0) = 0 AND EVENT_TIME >= %s"
                            + (" AND EVENT_TIME <= %s" if hi else "")
                            + " AND SCOPE_ID IN (" + ",".join(["%s"] * len(keys)) + ")",
                            [lo, *([hi] if hi else []), *keys])
                out[iid] = sorted(str(r[0]) for r in cur.fetchall())
        radius = _cell_radius(conn, sorted({k for v in out.values() for k in v}))
    except pymysql.err.ProgrammingError:
        return {}, {}                          # 표가 없는 현장 — 모른다(칠하지 않는다)
    finally:
        conn.close()
    return out, radius


def _cell_severity(rows: list[dict]) -> tuple[dict, dict, dict]:
    """구성 조각마다 **셀별 이상 심각도** — 지도의 셀 상태 음영(2026-11-15 사용자 "기지국이 꺼지면 회색, 통화
    품질이 적당히 나빠지면 노랑, 심하게 나빠지면 빨강").

    graphio 가 쓴 셀 단위 이상 판정(`AI_ANOMALY_EVENT` · `SCOPE_TYPE='CELL'` · 취소 제외)을 **읽기만** 한다 —
    그 조각의 셀 · 그 조각의 구간(`opened_ts`~`updated_ts`) · **그 조각의 트리거 KPI** 행 중 가장 높은 등급.
    KPI 를 좁히는 것은 조각이 그 KPI 로 묶였기 때문이다(graphio 불변규칙 10 의 결) — 같은 셀의 다른 KPI 이상을
    끌어오면 이 사건이 아닌 일이 칠해진다. 셀 행이 없는 셀은 **그 조각의 `severity`** 를 쓰고, 어느 쪽인지
    `src` 에 센다(`rows` · `member`). 화면이 새 기준값으로 나누지 않는다 — 등급은 탐지가 매긴 그대로다.
    표가 없는 현장이면 전부 조각 등급이다.

    셀마다 **KPI 별 등급**(`kpi`)도 준다 — 지진 화면이 탭을 증상(통화 끊김 C-01 · 통화 몰림 C-24)으로 묶을 때
    C-01·C-24 가 섞인 조각을 셀 단위로 가른다(2026-11-15 사용자 "통화몰림 하나, 통화 끊김 하나로 묶어야지").
    값은 셀 행에서만 온다. 셀 행이 없는 셀은 트리거 KPI 가 하나인 조각일 때만 그 KPI(조각 등급)를 주고, 둘
    이상이면 비운다(어느 증상인지 모른다 — `src.kpi_unknown` 에 센다)
    """
    rank = {"CRITICAL": 4, "MAJOR": 3, "MINOR": 2, "WARNING": 2, "WARN": 2, "INFO": 1}
    sev: dict[str, dict[str, str]] = {}
    src: dict[str, dict[str, int]] = {}
    allk: set[str] = set()
    conn = None
    try:
        conn = _connect()
        for m in rows:
            iid = str(m.get("incident_id"))
            keys = sorted({str(k) for k in m.get("cells") or ()})
            kpis = [k.strip() for k in str(m.get("trigger_kpi") or "").split(",") if k.strip()]
            lo = str(m.get("opened_ts") or "").replace("T", " ")[:19]
            hi = str(m.get("updated_ts") or "").replace("T", " ")[:19]
            got: dict[str, str] = {}
            per: dict[str, dict[str, str]] = {}
            if keys and kpis and lo:
                try:
                    with conn.cursor() as cur:
                        cur.execute("SELECT SCOPE_ID, SEVERITY, KPI FROM AI_ANOMALY_EVENT "
                                    "WHERE SCOPE_TYPE = 'CELL' AND COALESCE(CANCELLED, 0) = 0 "
                                    "AND EVENT_TIME >= %s" + (" AND EVENT_TIME <= %s" if hi else "")
                                    + " AND KPI IN (" + ",".join(["%s"] * len(kpis)) + ")"
                                    + " AND SCOPE_ID IN (" + ",".join(["%s"] * len(keys)) + ")",
                                    [lo, *([hi] if hi else []), *kpis, *keys])
                        for k, v, kp in cur.fetchall():
                            k, v, kp = str(k), str(v or "").upper(), str(kp)
                            if rank.get(v, 0) > rank.get(got.get(k, ""), 0):
                                got[k] = v
                            pk = per.setdefault(k, {})
                            if rank.get(v, 0) > rank.get(pk.get(kp, ""), 0):
                                pk[kp] = v
                except pymysql.err.ProgrammingError:
                    got, per = {}, {}          # 표가 없는 현장 — 조각 등급으로 내린다
            msev = str(m.get("severity") or "").upper()
            n_rows = len(got)
            unk = 0
            for k in keys:
                if k not in got and msev:
                    got[k] = msev
                    if len(kpis) == 1:
                        per[k] = {kpis[0]: msev}
                    else:
                        unk += 1
            sev[iid] = {k: {"sev": v, "kpi": per.get(k, {})} for k, v in got.items()}
            src[iid] = {"rows": n_rows, "member": len(got) - n_rows, "kpi_unknown": unk}
            allk.update(got)
        cell_r = _cell_radius(conn, sorted(allk))
    except pymysql.err.OperationalError:
        return {}, {}, {}                      # DB 에 닿지 않는다 — 모른다(칠하지 않는다)
    finally:
        if conn is not None:
            conn.close()
    return sev, src, cell_r


def _cell_radius(conn, keys: list[str]) -> dict[str, float | None]:
    """셀 → 추정 커버리지 반경(m) — `CM_CELL_GEO_MAP.RADIUS_M`(MESH1K 행 · METHOD DENSITY). 체감 칸을 셀에 나눌 때
    graphio 가 쓴 **같은 원**이다. 행정 행(ADM*)에는 반경이 없다. 모르면 None(그리지 않는다)"""
    enbs = sorted({k.rsplit("-", 1)[0] for k in keys})
    if not enbs:
        return {}
    got: dict[str, float | None] = {}
    with conn.cursor() as cur:
        cur.execute("SELECT ENB_ID, CELLNUM, MAX(RADIUS_M) FROM CM_CELL_GEO_MAP "
                    "WHERE AREA_LEVEL = 'MESH1K' AND ENB_ID IN (" + ",".join(["%s"] * len(enbs)) + ") "
                    "GROUP BY ENB_ID, CELLNUM", enbs)
        for e, n, r in cur.fetchall():
            got[f"{e}-{n}"] = float(r) if r is not None else None
    return {k: got.get(k) for k in keys}


def _event_members(rows: list[dict], cells: list[dict]) -> list[dict]:
    """구성 조각 하나씩 — 지도 바에서 조각을 골라 **그 조각만** 보게 한다(사용자 11-09).

    좌표는 사건 셀 목록(`cells`)에서 찾는다. 거기 없는 셀은 좌표가 없는 것이다 — 점을 짓지 않고
    수만 센다(graphio 불변규칙 12). 코어 장비는 노드 축 조각의 `axis_ids`(`SGW-0032`)에서 온다.
    """
    xy = {str(c.get("key")): [c.get("lon"), c.get("lat")] for c in cells}
    out = []
    for m in rows:
        at: dict[str, list] = {}
        for k in m.get("cells") or []:
            if k in xy:
                at.setdefault(k.rsplit("-", 1)[0], xy[k])
        # 사건에서 뗀 셀(graphio `detached`) — 이 조각의 셀이지만 사건 범위는 아니다(사용자 11-09)
        gone: dict[str, list] = {}
        for k in m.get("detached_cells") or []:
            if k in xy:
                gone.setdefault(k.rsplit("-", 1)[0], xy[k])
        axis = str(m.get("axis") or "")
        nodes = []
        for ax in m.get("axis_ids") or []:
            t, _, nid = str(ax).partition("-")
            if t.lower() in ("sgw", "mme", "pgw") and nid:
                nodes.append(f"{t.lower()}:{nid}")
        out.append({k: m.get(k) for k in (
            "incident_id", "severity", "title", "trigger_kpi", "opened_ts", "updated_ts",
            "root_code", "root_label", "confidence")}
            | {"axis": axis, "axis_vals": [str(x) for x in m.get("axis_ids") or []][:GEO_LIST],
               "cells": len(m.get("cells") or []), "enbs": sorted(at), "pts": list(at.values()),
               # 셀 키 — 지도의 「셀」 탭이 여러 조각의 셀을 겹치지 않게 센다(2026-11-16 표식 형태별 탭)
               "keys": sorted({str(k) for k in m.get("cells") or ()}),
               "nodes": nodes, "areas": _event_areas(m.get("areas") or []),
               "detached": len(m.get("detached_cells") or []), "dpts": list(gone.values()),
               "denbs": sorted(gone), "detached_reason": m.get("detached_reason"),
               "detached_kpis": [str(x) for x in m.get("detached_kpis") or []]})
    return out


def _event_areas(rows: list[dict]) -> list[dict]:
    """구성 조각의 칸·구역을 축별로 모은다 — 화면의 `geoShapes` 가 조각 하나처럼 그대로 받는 모양이다.

    넓은 축부터 준다. 나중에 그린 것이 위에 오므로 구 안의 메시가 구에 가리지 않는다.
    """
    by: dict[str, list[str]] = {}
    for r in rows:
        by.setdefault(str(r.get("level")), []).append(str(r.get("code")))
    out = [{"axis": lv, "geo_ids": by[lv]} for lv in _AREA_ORDER if lv in by]
    for a in out:
        if a["axis"] == "ADM3":
            at = _dong_at(a["geo_ids"])
            a["dong"] = [at[g] for g in a["geo_ids"] if g in at]
    return out


#: 지도에 면으로 그릴 수 있는 graphio 축. 메시는 코드만으로 칸이 계산되고
#: (JIS X 0410 — 화면이 한다), ADM2 는 화면이 이미 든 행정경계 폴리곤의
#: `N03_007` 과 같은 코드다. ADM3(丁目)은 폴리곤이 없어 중심점뿐이다
_GEO_AXES = ("MESH1K", "MESH500", "MESH250", "ADM1", "ADM2", "ADM3", "GLOBAL")
#: 사건의 칸을 그리는 순서 — 넓은 것부터
_AREA_ORDER = ("ADM1", "ADM2", "ADM3", "MESH1K", "MESH500", "MESH250")
#: 위치 없는 축의 값(단말 기종·APN·도메인)은 선택 바에 적을 만큼만
GEO_LIST = 10

#: 편향 노드를 짚는 축 — 단말 기종·APN 장애는 위치가 없지만 graphio 가
#: EV-12 로 "실패가 어느 코어 장비로 쏠렸나"를 판정해 둔다(사람 결정 2026-10-30)
_BIAS_AXES = ("UE", "APN")
#: **이 비중 이상일 때만 짚는다.** EV-12 `top_node` 는 편향이 없어도 늘
#: 채워진다 — 도메인 장애가 `MME-1 · 실패 0 · 비중 0%` 로 왔다. 그것을 짚으면
#: 지도가 없는 사실을 만든다(graphio 불변규칙 18)
BIAS_MIN_SHARE = 50.0
#: incident_id → (rca_id, 편향·DNS). 뒤 일꾼(`_bias_worker`)이 채운다. RCA 가 다시 돌면
#: rca_id 가 달라져 새로 묻되, 새 값이 올 때까지 **옛 값을 쓴다**
_BIAS_CACHE: dict[str, tuple] = {}
#: 일꾼에게 맡긴 것 — 10초 폴링이 같은 건을 두 번 넣지 않게
_BIAS_WAIT: set[tuple] = set()
_BIAS_Q: "queue.SimpleQueue[tuple]" = queue.SimpleQueue()
_BIAS_LOCK = threading.Lock()
_BIAS_THREAD: threading.Thread | None = None


def _is_egress(h: dict) -> bool:
    """**망 밖으로 나가는 쪽** 장애인가 — 서비스 도메인 축이거나 DNS 지표(U-29).

    이것들은 셀·구가 아니라 인터넷 출구(PGW 너머)의 일이라, 지도가 도코모
    건물(PGW)에 붙인다. graphio 가 PGW 를 지목한 것은 **아니다** — 화면의 배치다.
    """
    kpis = {k.strip() for k in str(h.get("trigger_kpi") or "").split(",")}
    return h.get("axis_raw") == "DOMAIN" or "U-29" in kpis


def _attach_bias(base: str, hist: list[dict]) -> None:
    """UE·APN·망 밖 장애에 **EV-12 편향 노드와 DNS** 를 붙인다 — 있는 것만, 기다리지 않는다.

    목록 API 에 증거가 없어 건마다 `/api/incidents/{id}` 를 물어야 한다. 전에는 응답
    안에서 순서대로 물어, 캐시가 빈 첫 호출이 **15.6초**였다(200건 중 56건 × 0.19초 +
    재시작마다 반복 · 2026-11-05 실측). 그동안 목록 전체가 비어 있었다. 그래서 묻는 일은
    뒤 일꾼에게 넘기고 응답은 지금 아는 값만 싣는다 — 다음 10초 폴링에 채워진다.
    """
    global _BIAS_THREAD
    for h in hist:
        dns_case = _is_egress(h)
        h["egress"] = dns_case or any(n.startswith("pgw:") for n in h.get("nodes") or [])
        if h.get("axis_raw") not in _BIAS_AXES and not dns_case:
            continue
        iid, rid = h["incident_id"], h.get("rca_id")
        rid0, got = _BIAS_CACHE.get(iid, (None, None))
        if got:
            h["bias"], h["dns"] = got["bias"], got["dns"]
        if got is None or rid0 != rid:
            with _BIAS_LOCK:
                if (iid, rid) not in _BIAS_WAIT:
                    _BIAS_WAIT.add((iid, rid))
                    _BIAS_Q.put((base, iid, rid, h.get("axis_raw"), dns_case))
    with _BIAS_LOCK:
        if _BIAS_THREAD is None:
            _BIAS_THREAD = threading.Thread(target=_bias_worker, daemon=True)
            _BIAS_THREAD.start()


def _bias_worker() -> None:
    """한 건씩 묻는다 — 한꺼번에 던지면 graphio 화면 요청과 다툰다."""
    while True:
        base, iid, rid, axis_raw, dns_case = _BIAS_Q.get()
        try:
            got = _bias_fetch(base, iid, axis_raw, dns_case)
            if got is not None:
                _BIAS_CACHE[iid] = (rid, got)
        finally:
            with _BIAS_LOCK:
                _BIAS_WAIT.discard((iid, rid))    # 실패면 다음 폴링이 다시 넣는다


def _bias_fetch(base: str, iid: str, axis_raw: str | None, dns_case: bool) -> dict | None:
    """상세 한 건 → {bias, dns}. 닿지 않으면 None — 그 건만 비고 지도는 선다."""
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(f"{base}/api/incidents/{iid}", timeout=3) as r:
            evs = (json.loads(r.read()).get("rca") or {}).get("evidence") or {}
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None
    got: dict = {"bias": None, "dns": None}
    ev = evs.get("EV-12") or {}
    t = ev.get("top_node") or {}
    share = float(t.get("failure_share") or 0)
    if (axis_raw in _BIAS_AXES
            and t.get("node_type") and t.get("node_id")
            and int(t.get("failures") or 0) > 0
            and share >= BIAS_MIN_SHARE):
        got["bias"] = {"node": f"{str(t['node_type']).lower()}:{t['node_id']}",
                       "share": share, "failures": int(t["failures"]),
                       "verdict": ev.get("verdict"),
                       "concentration": ev.get("concentration")}
    # **망 밖 장애는 DNS 를 나란히 둔다**(EV-08b) — "이름 풀이가 안 되나,
    # 서버·백본이 느리나"를 운영자가 가르게. 값은 증거를 옮길 뿐이다
    if dns_case:
        rows = [d for d in (evs.get("EV-08b") or {}).get("dns") or []
                if isinstance(d, dict)]
        rows.sort(key=lambda d: (-float(d.get("share_of_failures") or 0),
                                 -int(d.get("queries") or 0)))
        got["dns"] = [{k: d.get(k) for k in (
            "host_site", "queries", "fail_rate", "response_ms", "slow",
            "baseline_fail_rate", "baseline_response_ms", "low_sample")}
            for d in rows[:3]]
    return got


def _attach_dong(hist: list[dict]) -> None:
    """ADM3(읍면동) 축의 **중심점과 모양**을 붙인다 — 읍면동 표(`CM_GEO_UNIT` ADM3)는 이름·중심좌표뿐이다.

    모양(`rings`)은 graphio `geo-chome` 이 실제 정목 경계를 읍면동마다 이어 붙인 것이다(2026-11-14 ·
    graphio 지도와 같은 파일). 모양이 없는 읍면동만 화면이 중심점 원으로 그리고 "경계 없음"으로 센다
    (graphio 불변규칙 12). DB 가 안 닿으면 **빈 채로** 둔다 — 지도는 서고, 그 장애만 "좌표 없음"이 된다.
    """
    ids = sorted({g for h in hist if h.get("axis") == "ADM3" for g in h["geo_ids"]})
    if not ids:
        return
    at = _dong_at(ids)
    for h in hist:
        if h.get("axis") == "ADM3":
            h["dong"] = [at[g] for g in h["geo_ids"] if g in at]


def _dong_shapes() -> dict[str, list]:
    """읍면동 코드 → 고리 목록(`[[경도, 위도], …]` 들). 설정이 비었거나 파일이 없으면 빈 사전 — 원으로 그린다.

    고리를 한 면으로 이어 그린다(구멍 고리는 방향이 반대라 nonzero 로 비어 보인다 · graphio `geo_chome`)
    """
    src, err = _layer_path("geo_dong_shapes")
    if err:
        return {}
    try:
        return _cached(src, "json", lambda p: json.loads(p.read_bytes()))
    except (OSError, ValueError):
        return {}


def _dong_at(ids: list[str]) -> dict[str, dict]:
    """읍면동 코드 → 이름·중심점(+ 모양이 있으면 `rings`). DB 가 안 닿으면 빈 채로 돌려준다.

    **읍면동 정본은 graphio 의 `CM_GEO_UNIT` ADM3 다**(2026-11-15 B안). 그날부터 읍면동이 실제 町(9자리 ·
    e-Stat)이라 `CM_DONG_JP`(생성기의 합성 동 · 8자리)에 없다. 옛 합성 동도 `CM_GEO_UNIT` 에 `USE_YN='N'`
    로 남아 지난 장애도 여기서 찾는다. 거기 없는 코드만 `CM_DONG_JP` 로 한 번 더 본다.
    """
    at: dict[str, dict] = {}

    def put(d, name, lat, lon) -> None:
        if lat and lon and str(d) not in at:
            at[str(d)] = {"id": str(d), "name": name,
                          "lat": round(float(lat), 6), "lon": round(float(lon), 6)}

    try:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT AREA_CD, AREA_NAME, CENTROID_LAT, CENTROID_LON FROM CM_GEO_UNIT "
                            "WHERE AREA_LEVEL = 'ADM3' AND AREA_CD IN ("
                            + ",".join(["%s"] * len(ids)) + ")", ids)
                for row in cur.fetchall():
                    put(*row)
                rest = [d for d in ids if d not in at]
                if rest:
                    cur.execute("SELECT DONG_ID, DONG_NAME, LATITUDE, LONGITUDE FROM CM_DONG_JP "
                                "WHERE DONG_ID IN (" + ",".join(["%s"] * len(rest)) + ")", rest)
                    for row in cur.fetchall():
                        put(*row)
        finally:
            conn.close()
    except pymysql.MySQLError:
        return {}
    shapes = _dong_shapes()
    for d, v in at.items():
        if shapes.get(d):
            v["rings"] = shapes[d]
    return at


@router.get("/api/map/standby")
def api_map_standby():
    """**소등 eNB**(24번 B 후보)의 지금 상태 — 지도가 회색 / 켜짐으로 칠한다.

    지도는 생성기와 같은 셀 목록(`_cells`)을 그리는데, 그 목록에는 소등
    eNB 도 들어 있다(마스터에 있으므로). 상태를 따로 주지 않으면 **평소 호가
    없는 eNB 가 평범한 기지국처럼** 보인다.

    상태는 생성기와 **같은 함수**로 정한다(`scenarios.shift_pick`·국면 상수).
    화면이 자기 규칙으로 B 를 고르면 지도가 켜졌다고 말하는 eNB 와 실제로
    호를 받는 eNB 가 어긋나고, 에러는 나지 않는다.

    `off` 평소 소등 · `next` 진행 중인 24번이 10분 시점에 켤 eNB · `on` 켜짐.
    `faults` 와 따로 두는 이유: 그쪽은 graphio API 가 없으면 일찍 끝나는데,
    이 상태는 생성기 것이라 graphio 와 무관하게 보여야 한다.
    """
    from .. import scenarios as S
    from ..cli import _capacity_shift

    cfg = S.ShiftCfg.from_cfg(_capacity_shift(_pem_cfg()))
    st = {e: "off" for e in cfg.all_standby}
    if st:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT START_TIME, TARGET FROM {T_FAULT} "
                            "WHERE FAULT_KIND='capacity_shift' AND STATUS='active'")
                rows = cur.fetchall()
        finally:
            conn.close()
        now = datetime.now()
        for start, target in rows:
            # 그 장애의 자리 안에서 고른다 — 생성기 `faults._shift_target` 과 같은 함수(`site_for`·`shift_pick`)
            site = cfg.site_for(target)
            b = S.shift_pick(int(start.timestamp() // 60), site.standby_enbs) if site else None
            el = (now - start).total_seconds() / 60
            if b is None or not 0 <= el < S.SHIFT_TOTAL:
                continue
            st[b] = "on" if el >= S.SHIFT_RISE else ("next" if st[b] == "off" else st[b])
    #: B → 그 자리의 A — 켜진 B 의 설명(「eNB A 의 부하를 받는다」)이 자기 자리 A 를 말하게
    from_of = {b: s.from_enb for s in cfg.sites for b in s.standby_enbs}
    stamp = json.dumps([cfg.from_enb, sorted(st.items()), sorted(from_of.items())])
    return {"ver": hashlib.sha1(stamp.encode()).hexdigest()[:12],
            "from": cfg.from_enb, "from_of": from_of, "enbs": st,
            #: B 가 켜지는 경과 분 — 지도 설명 「n분 시점에 켜진다」가 상수를 따르게(`scenarios.SHIFT_RISE`)
            "b_open_min": S.SHIFT_RISE,
            "sites": [{"name": s.name, "from_enb": s.from_enb, "standby_enbs": list(s.standby_enbs)}
                      for s in cfg.sites]}


@router.get("/api/map/all")
def api_map_all():
    """**마스터 전량** — 생성 대상이 아닌 셀까지.

    지도에 생성 대상만 그렸더니 "신주쿠에만 기지국이 있나?"로 읽혔다.
    아니다 — `pemdb1.scope` 가 좁힌 것이고 마스터에는 도쿄 전역이 있다.
    **좁힌 사실이 화면에 보여야 한다**(불변규칙 12 의 결).

    좌표만 내린다. 2만 개가 넘어 장비 정보까지 실으면 응답이 커지고,
    배경으로 깔 것이라 누를 일도 없다.
    """
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT LATITUDE, LONGITUDE, ENB_ID "
                        "FROM CM_CELL_REP_RU_LOC_JP")
            rows = cur.fetchall()
    finally:
        conn.close()
    # **좌표를 겹쳐 보내지 않는다.** 한 자리에 섹터가 6개씩 서 있어
    # 23,039셀이 좌표로는 5,036자리다 — 그대로 보내면 4.5배를 헛보낸다.
    # 화면의 점 하나는 **셀이 아니라 자리**다(그래서 수를 따로 내린다).
    seen, pts, enb, n = set(), [], set(), 0
    for la, lo, e in rows:
        a, o = dms_to_decimal(la), dms_to_decimal(lo)
        if not (a and o):
            continue
        n += 1
        enb.add(e)
        k = (round(o, 5), round(a, 5))
        if k not in seen:
            seen.add(k)
            pts.append([k[0], k[1]])
    return {"pts": pts, "cells": n, "enb": len(enb), "spots": len(pts)}


@router.get("/api/map/geo")
def api_map_geo(bbox: str = "", cover: int = 0):
    """행정경계 GeoJSON — **로컬 파일이다. 외부 타일을 쓰지 않는다.**

    처음에 CARTO 타일 CDN 으로 배경을 깔았는데 셋이 걸렸다 —

    1. **CARTO 는 API 키를 요구한다.** 키 없이 쓰면 언제 끊길지 모른다.
    2. **현장 PoC 가 폐쇄망일 수 있다.** 외부 CDN 이면 지도가 통째로 안 뜬다.
    3. **이 저장소의 다른 화면은 외부 의존이 없다.** 저 셋(Leaflet CSS·JS·
       타일)이 `ui.py` 의 **유일한** 외부 URL 이었다.

    그래서 graphio 가 이미 들고 있는 경계를 쓴다 — `data/geo/jp/` 의
    국토수치정보 행정구역(1.87MB · CC BY 4.0 · 그쪽 `SOURCE.txt`).

    `bbox` 는 `"lat0,lon0,lat1,lon1"` 이다. 전국 1,897개를 다 내리면 브라우저가
    느려진다 — 보는 자리만 내린다.

    `cover=1` 이면 **레이어(철도·하천…)가 덮는 범위까지 넓힌다**(2026-11-03).
    기지국 범위만 받으면 간토 전역에 걸친 철도가 바다 위에 떴다. 넓힌 쪽은
    **점을 줄인다**(`_GEO_FAR_M`) — 상세본 그대로 넓혔더니 점이 9.1만 개 중
    2.1만 개가 늘어 `draw` 가 +10ms, 날아가는 동안 매 프레임 그리는 양도
    30% 늘었다. 그쪽은 철도 밑에 깔리는 바탕일 뿐이다
    """
    # **자기 설정 키를 쓴다.** 처음엔 `geo_crosswalk` 의 형제에서 찾았는데,
    # 그 키가 로케일별로 다른 디렉터리를 가리켜(`data/geo/` vs
    # `data/geo/jp/`) 컨테이너에서 **파일을 못 찾았다** — 두 값이 늘 같은
    # 디렉터리에 있으리라는 가정이 틀렸다. 경로는 경로대로 받는다.
    # **지도 전용 상세본이 먼저다**(`geo_boundary_map`, 2026-10-30). `geo_boundary`
    # 는 graphio 의 셀 귀속과 같은 파일이라 갈아 끼울 수 없다 — 비우면 그쪽으로 간다
    raw = str(_pem_cfg().get("geo_boundary_map") or _pem_cfg().get("geo_boundary") or "")
    if not raw:
        return {"error": "`pemdb1.geo_boundary` 가 비어 있습니다 — 행정경계 파일 "
                         "경로를 설정에 적어야 합니다", "features": []}
    src = Path(raw)
    if not src.is_absolute():
        src = (HERE / src).resolve()
    if not src.exists():
        # **빈 배열을 돌려주지 않는다** — "경계가 없다"와 "파일이 없다"는
        # 다른 사실이고, 전자로 보이면 지도가 왜 비었는지 알 수 없다
        return {"error": f"경계 파일이 없습니다: {src}", "features": []}

    # **응답 바이트를 만들어 둔다**(2026-11-24 사용자 「전체 지도 로딩이 시간 좀 걸린다」). 같은 인자면 결과가
    # 같다 — 고리 거르기 + FastAPI 기본 직렬화(1.7MB)가 혼자서 0.2초, 화면이 레이어 7개와 동시에 부르면
    # GIL 을 다퉈 0.88초였다. 경계 파일·레이어 파일이 다시 구워지면(mtime) 새로 만든다
    key = (str(src), src.stat().st_mtime, bbox, int(cover),
           tuple(sorted((k, p.stat().st_mtime) for k, p in
                        ((k, _layer_path(k)[0]) for k, _ in _LAYER.values()) if p)) if cover else ())
    body = _GEO_BODY.get(key)
    if body is None:
        body = json.dumps(_geo_body(src, bbox, cover), ensure_ascii=False, separators=(",", ":")).encode()
        if len(_GEO_BODY) >= _GEO_BODY_MAX:
            _GEO_BODY.pop(next(iter(_GEO_BODY)))
        _GEO_BODY[key] = body
    return Response(body, media_type="application/json")


#: `/api/map/geo` 응답 바이트 — 인자·파일 시각별. 화면은 bbox 하나로 부르므로 몇 개면 된다. **초기값이다**
_GEO_BODY: dict = {}
_GEO_BODY_MAX = 8


def _geo_body(src: Path, bbox: str, cover: int) -> dict:
    """`/api/map/geo` 의 본문 — 머리말은 그 함수에 있다."""
    lo_la = lo_lo = -1e9
    hi_la = hi_lo = 1e9
    if bbox:
        try:
            lo_la, lo_lo, hi_la, hi_lo = (float(x) for x in bbox.split(","))
        except ValueError:
            lo_la = lo_lo = -1e9
            hi_la = hi_lo = 1e9
    core = (lo_la, lo_lo, hi_la, hi_lo)
    want = core
    ext = _layer_extent() if cover else None
    if ext:
        want = (min(lo_la, ext[0] - .05), min(lo_lo, ext[1] - .05),
                max(hi_la, ext[2] + .05), max(hi_lo, ext[3] + .05))

    def hit(b, box):
        return not (b[2] < box[0] or b[0] > box[2] or b[3] < box[1] or b[1] > box[3])

    out = []
    for pr, rings in _geo_doc(src):
        keep = []
        for ring, b, far in rings:
            if not hit(b, want):
                continue
            # 기지국 범위에 걸치지 않는 고리는 줄인 쪽을 쓴다
            keep.append(ring if hit(b, core) else far)
        if keep:
            # 이름을 놓을 자리 — **가장 큰 고리**의 가운데. 섬이 딸린 구는
            # 작은 고리에 이름이 붙으면 엉뚱한 바다 위에 뜬다
            big = max(keep, key=len)
            xs = [c[0] for c in big]
            ys = [c[1] for c in big]
            out.append({
                "name": pr.get("N03_004") or pr.get("N03_003") or "",
                "city": pr.get("N03_003") or "",
                "code": pr.get("N03_007") or "",
                "at": [round((min(xs) + max(xs)) / 2, 6),
                       round((min(ys) + max(ys)) / 2, 6)],
                "rings": keep,
            })
    return {"features": out, "source": "国土数値情報 行政区域 · CC BY 4.0"}


#: 넓힌 쪽 경계의 단순화 허용오차(m). **처음엔 점 4개 중 1개를 골랐는데**
#: (`_GEO_THIN=4`) 이미 점 20개인 시가 삼각형이 되고, 맞닿은 두 시가 같은 선을
#: 다른 점으로 솎아 사이에 틈이 났다. 더글라스-포이커는 꺾이는 점을 남기므로
#: 모양이 서고 이웃의 선도 허용오차 안에서 겹친다
_GEO_FAR_M = 50.0


def _simplify(pts, tol_m: float):
    """더글라스-포이커 — `graphio-probe-rca/tools/build_jp_geo_layers.py` 와 같은 식.

    좌표는 도, 허용오차는 m(첫 점 위도에서 환산). 닫힌 고리는 첫 점·끝 점이
    같아 그대로 닫힌다
    """
    n = len(pts)
    if n < 5:
        return pts
    kx = 111320 * math.cos(math.radians(pts[0][1]))
    P = [(x * kx, y * 110574) for x, y in pts]
    keep = [False] * n
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        a, b = stack.pop()
        ax, ay = P[a]
        bx, by = P[b]
        dx, dy = bx - ax, by - ay
        L = dx * dx + dy * dy
        best, bi = 0.0, -1
        for i in range(a + 1, b):
            px, py = P[i]
            t = 0.0 if L == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L))
            d = math.hypot(px - (ax + t * dx), py - (ay + t * dy))
            if d > best:
                best, bi = d, i
        if best > tol_m:
            keep[bi] = True
            stack += [(a, bi), (bi, b)]
    out = [pts[i] for i in range(n) if keep[i]]
    # 닫힌 고리는 첫 점과 끝 점이 같아 한 변의 양 끝이 겹친다 — 그 자리에서
    # 가운데 점이 하나도 안 남으면 고리가 선이 된다. 그때는 원래대로 둔다
    return out if len(out) >= 4 else pts


def _geo_doc(src: Path):
    """경계 파일 → [(properties, [(ring, (lat0, lon0, lat1, lon1), far), ...]), ...].

    3.7MB 를 요청마다 `json.loads` 하고 고리마다 범위를 다시 쟀다. 한 번 읽어 둔다.
    `far` 는 넓힌 쪽에 쓸 단순화본(`_GEO_FAR_M`) — 읽을 때 한 번 만든다
    """
    def make(p: Path):
        doc = json.loads(p.read_text(encoding="utf-8"))
        out = []
        for f in doc.get("features", []):
            g = f.get("geometry") or {}
            polys = ([g.get("coordinates")] if g.get("type") == "Polygon"
                     else g.get("coordinates") or [])
            rings = []
            for poly in polys:
                if not poly:
                    continue
                ring = poly[0]
                xs = [c[0] for c in ring]
                ys = [c[1] for c in ring]
                rings.append((ring, (min(ys), min(xs), max(ys), max(xs)),
                              _simplify(ring, _GEO_FAR_M)))
            out.append((f.get("properties") or {}, rings))
        return out
    return _cached(src, "geo", make)


# ── 미니 지도 · 진앙 (2026-11-15 사용자) ───────────────────────────────────
#
# 27번 진앙은 도쿄 밖 실제 진원역(바다·땅속 깊은 곳)이라 장비 지도 화면 밖이다. 큰 지도를 넓히면 기지국이
# 점 하나로 뭉개진다 — 구석에 **움직이지 않는 작은 지도**를 얹어 진앙 ★ 과 규모만 찍는다(사용자 안).
# 범위는 **관동과 그 앞바다**다(같은 날 사용자 — "100% 전도는 필요없고 도쿄와 먼바다쪽, 실제 지진 나는
# 쪽으로만"). 생성기 진원역(`pemdb1.fault.quake_regions`)이 모두 들어가게 그 설정에서 계산한다 — 진원역을
# 고치면 범위도 따라간다.
# 윤곽은 graphio 가 이미 들고 있는 도도부현 경계(`data/geo/jp/prefectures.json` · 국토수치정보 N03 ·
# CC BY 4.0 · 그쪽 `SOURCE.txt`)에서 범위에 걸치는 고리만 줄여 내린다 — 외부를 부르지 않는다.
# 범위 밖으로 삐져나온 땅은 화면(SVG viewBox)이 자른다.

#: 미니 지도 윤곽의 단순화 허용오차(m) — 화면 표시 상수다. 관동 폭 약 320km 를 190px 에 그리면 1px 이
#: 1.7km 남짓이라 그 절반 아래 굴곡은 보이지 않는다
_MINI_TOL_M = 800.0
#: 진원역 묶음 바깥 여백(도) — 화면 표시 상수다. 진원역은 도쿄 100km 안이라 여백 없이 그리면 千葉県東方沖
#: 바다 쪽이 가장자리에 붙고, 1° 면 앞바다·이즈 오시마·관동 산지까지 보인다(범위 약 경도 138.2~141.8)
_MINI_MARGIN_DEG = 1.0


def _mini_view():
    """미니 지도 범위 [남, 서, 북, 동] — 진원역 묶음 ± 흩뜨림 폭(`quake_jitter_deg`) ± 여백.

    진원역 설정이 없으면 None(윤곽 전체 범위를 쓴다)."""
    f = _pem_cfg().get("fault") or {}
    regs = [r for r in (f.get("quake_regions") or []) if isinstance(r, dict)]
    pts = [(float(r["lat"]), float(r["lon"])) for r in regs if r.get("lat") is not None and r.get("lon") is not None]
    if not pts:
        return None
    m = float(f.get("quake_jitter_deg") or 0) + _MINI_MARGIN_DEG
    return [round(min(p[0] for p in pts) - m, 3), round(min(p[1] for p in pts) - m, 3),
            round(max(p[0] for p in pts) + m, 3), round(max(p[1] for p in pts) + m, 3)]


def _mini_outline() -> dict:
    src, err = _layer_path("geo_prefectures")
    if err:
        return {"error": err, "rings": []}
    view = _mini_view()

    def make(p: Path):
        doc = json.loads(p.read_text(encoding="utf-8"))
        rings = []
        for f in doc.get("features", []):
            g = f.get("geometry") or {}
            polys = [g.get("coordinates")] if g.get("type") == "Polygon" else g.get("coordinates") or []
            for poly in polys:
                if not poly or len(poly[0]) < 4:
                    continue
                if view:
                    xs = [x for x, _ in poly[0]]
                    ys = [y for _, y in poly[0]]
                    if max(xs) < view[1] or min(xs) > view[3] or max(ys) < view[0] or min(ys) > view[2]:
                        continue
                r = _simplify(poly[0], _MINI_TOL_M)
                rings.append([[round(x, 3), round(y, 3)] for x, y in r])
        xs = [x for r in rings for x, _ in r]
        ys = [y for r in rings for _, y in r]
        bbox = view or ([min(ys), min(xs), max(ys), max(xs)] if xs else None)
        return {"rings": rings, "bbox": bbox,
                "source": "国土数値情報 行政区域 (国土交通省) · CC BY 4.0"}
    return _cached(src, f"mini:{view}", make)


@router.get("/api/map/quake")
def api_map_quake(at: str = "", outline: int = 0):
    """미니 지도(관동)에 찍을 지진 하나 — `CM_QUAKE_EVENT`(생성기가 쓰는 표)를 **읽기만** 한다.

    * ``at``(지도에서 고른 장애의 시작 시각)이 있으면 **그 시각을 주입 구간 안에 품은 27번**의 지진**만**이다
      — 시작 `START_TIME` ~ `START_TIME + DURATION_MIN`. 새 시간창을 정하지 않으려고 장애 자신의 구간을 쓴다.
      진행 중인 27번이 있어도 고른 장애가 그 구간 밖이면(지진이 아닌 장애) None 이다(2026-11-15 사용자
      "지진 시나리오 누를 때만 나와야").
    * ``at`` 이 없으면 진행 중인 27번(`STATUS='active'`)의 지진이다 — 화면은 고른 장애가 없으면 묻지 않는다.
    * 해당이 없으면 ``quake`` 가 None 이고 화면은 미니 지도를 숨긴다.

    미니 지도에 찍는 값은 진앙·규모뿐이다(사용자 — "별표시랑 매그니튜드만"). 지명·깊이·진앙 진도는 통합 장애 바의
    「지진」 줄 몫이다(2026-11-16). 규모를 모르면 None 이고 화면은
    「M ?」 대신 규모를 비운다(불변규칙 17). ``outline=1`` 이면 윤곽도 함께 준다 — 정적이라 화면이 한 번만 받는다

    ``down`` 은 그 지진으로 **꺼진 기지국 셀**이다(같은 날 사용자 "기지국이 꺼졌으니 영향받는 지형을 음영으로")
    — `_quake_down` 참조. 정답지에 그 장애가 없으면 None(모름)이고 화면은 아무것도 그리지 않는다
    """
    out: dict = {"quake": None}
    if outline:
        out["outline"] = _mini_outline()
    try:
        ts = datetime.fromisoformat(at.replace("Z", "")) if at else None
    except ValueError:
        raise HTTPException(400, f"at 이 시각이 아닙니다: {at}")
    from ..control import T_QUAKE
    sel = (f"SELECT q.FAULT_ID, q.LAT, q.LON, q.MAGNITUDE, q.ORIGIN_TIME, f.STATUS, "
           f"q.HYPO_NAME, q.DEPTH_KM, q.INTENSITY "
           f"FROM {T_QUAKE} q JOIN {T_FAULT} f ON f.FAULT_ID = q.FAULT_ID ")
    conn = _connect()
    try:
        with conn.cursor() as cur:
            if ts is not None:
                # 장애 시각은 **분 단위**(06:51:00)이고 주입은 분 가운데(06:51:11)에 선다 — 그 분 안의 시작도 품는다.
                # 전에는 `START_TIME <= at` 이라 첫 분에 열린 장애(F-641)에서 지진을 못 찾아 미니 지도·진앙이 통째로 빠졌다(11-26 사용자)
                cur.execute(sel + "WHERE f.START_TIME < %s + INTERVAL 1 MINUTE "
                            "AND f.START_TIME + INTERVAL f.DURATION_MIN MINUTE >= %s "
                            "ORDER BY q.ORIGIN_TIME DESC LIMIT 1", [ts, ts])
            else:
                cur.execute(sel + "WHERE f.STATUS = 'active' ORDER BY q.ORIGIN_TIME DESC LIMIT 1")
            row = cur.fetchone()
    except pymysql.err.ProgrammingError as e:
        # 표가 없는 환경(27번을 한 번도 안 넣은 현장) — 지진이 없는 것과 같다. 사유는 남긴다
        out["note"] = f"지진 정보 표를 읽지 못했다: {e.args[-1] if e.args else e}"
        return out
    finally:
        conn.close()
    if row:
        fid, lat, lon, mag, t, st, hypo, depth, inten = row
        # 진원 지명·깊이·진앙 진도는 통합 장애 바의 「지진」 줄에 쓴다(2026-11-16 사용자 "통합장애 표시에 지진 정보도").
        # 생성기가 적은 값 그대로이고 모르면 None — 화면은 그 칸을 비운다(불변규칙 17)
        out["quake"] = {"fault_id": int(fid), "lat": float(lat), "lon": float(lon),
                        "magnitude": float(mag) if mag is not None else None,
                        "origin_time": t.isoformat() if t else None, "active": st == "active",
                        "hypo_name": hypo or None,
                        "depth_km": float(depth) if depth is not None else None,
                        "intensity": float(inten) if inten is not None else None}
        out["down"] = _quake_down(int(fid))
    return out


def _quake_down(fault_id: int) -> dict | None:
    """그 27번에서 꺼진 기지국 셀과 셀별 추정 커버리지 반경 — 큰 지도의 「정지 기지국 영역」(음영).

    * **꺼진 셀은 생성기가 남긴 사실만 쓴다** — 정답지(`truth_out`)의 `down_cells`(없으면 `down_enbs` 의 모든
      셀). 화면이 진도로 다시 추정하지 않는다. `CM_QUAKE_EVENT` 는 셀 목록을 두지 않는다(그 표 설명).
      정답지는 **최근 장애만** 담으므로 지난 회차는 None(모름)이다 — 0곳과 다르다(불변규칙 17).
    * 반경은 `CM_CELL_GEO_MAP.RADIUS_M`(MESH1K 행 · METHOD DENSITY) — 체감 칸을 셀에 나눌 때 graphio 가 쓴
      **같은 추정 커버리지 원**이다. 행정 행(ADM*)에는 반경이 없다. 반경을 모르는 셀은 ``r_m`` None 이고
      화면이 그리지 않는다(지어내지 않는다)
    """
    from .faults_api import _truth_of
    t = _truth_of(fault_id)
    if not t or "down_enbs" not in t:
        return None
    enbs = sorted({str(e) for e in t.get("down_enbs") or ()})
    keys = [str(k) for k in t.get("down_cells") or ()]
    # 꺼진 시각·eNB 별 다시 켜진 시각(epoch 초) — 3D 재생이 그 분부터 그 분까지만 「꺼짐」 기둥을 세운다. 정답지 값 그대로, 없으면 None
    out = {"enbs": enbs, "cells": [], "source": "ground_truth",
           "at_ts": t.get("down_at_ts"), "up_ts": t.get("down_up_ts") or {}}
    if not enbs:
        return out
    radius: dict[str, float | None] = {}
    conn = _connect()
    try:
        if keys:
            radius = _cell_radius(conn, keys)
        else:
            # 옛 정답지(셀 목록 없음) — 꺼진 eNB 의 셀 전부. 셀 번호를 모르니 eNB 로 묻는다
            with conn.cursor() as cur:
                cur.execute("SELECT ENB_ID, CELLNUM, MAX(RADIUS_M) FROM CM_CELL_GEO_MAP "
                            "WHERE AREA_LEVEL = 'MESH1K' AND ENB_ID IN (" + ",".join(["%s"] * len(enbs)) + ") "
                            "GROUP BY ENB_ID, CELLNUM", enbs)
                for e, n, r in cur.fetchall():
                    radius[f"{e}-{n}"] = float(r) if r is not None else None
    except pymysql.err.ProgrammingError:
        pass                                   # 표가 없는 현장 — 반경 모름으로 내린다
    finally:
        conn.close()
    if not keys:
        keys = sorted(radius)                  # 옛 정답지(셀 목록 없음) — 꺼진 eNB 의 셀 전부
    out["cells"] = [{"key": k, "r_m": radius.get(k)} for k in keys]
    return out


#: 진도 띠 — 기상청 계급 경계(계측 진도 3.5 = 震度4 · 4.5 = 震度5弱). 화면 색 단계만 가른다(판정 아님)
_JMA_BANDS = ((4.5, "5-"), (3.5, "4"))
_IMPACT_CACHE: dict = {}


@router.get("/api/map/quake/{fault_id}/impact")
def api_map_quake_impact(fault_id: int):
    """그 27번 지진의 **영향 지역** — 생성 범위 eNB 마다 추정 진도(2026-11-15 사용자 "영향받았다고 표시되는 게
    훨씬 넓어야 하고, 음영과 실제 정지는 좁아야").

    * 진도는 생성기와 **같은 식·같은 지반 표**다(`scenarios.quake_cell_intensity` · `quake_arv_path`). 진원역 d 는
      진원 지명(`HYPO_NAME`)으로 `quake_regions` 에서 찾고, 없으면 0 이다(graphio `fault_type_d` 기본과 같다).
    * 폭주 문턱 `quake_surge_intensity`(진도 3) 이상인 셀이 영향 지역이다 — 새 기준이 아니라 생성기가 폭주
      지역을 고르는 값이다. eNB 하나에 셀 진도가 여럿이면 가장 큰 값, 그리는 원은 그 eNB 셀 중 가장 큰 추정 커버리지
      반경(`_cell_radius` 와 같은 MESH1K 행)이다. 반경을 모르는 eNB 는 그리지 않는다(지어내지 않는다).
    * 규모·깊이를 모르면(옛 행) 계산하지 않는다 — ``error`` 와 빈 목록(불변규칙 17).
    정적이라 장애마다 한 번 계산해 캐시한다
    """
    from .. import scenarios as S
    from ..control import T_QUAKE

    if fault_id in _IMPACT_CACHE:
        return _IMPACT_CACHE[fault_id]
    fc = S.FaultCfg.from_cfg(_pem_cfg().get("fault") or {})
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT LAT, LON, DEPTH_KM, MAGNITUDE, HYPO_NAME FROM {T_QUAKE} WHERE FAULT_ID = %s "
                        "ORDER BY ORIGIN_TIME DESC LIMIT 1", [fault_id])
            row = cur.fetchone()
        if not row:
            return {"error": f"F-{fault_id} 의 지진 정보가 없습니다", "enbs": []}
        lat, lon, depth, mag, hypo = row
        if depth is None or mag is None:
            return {"error": "규모·깊이를 모릅니다 — 진도를 계산하지 않습니다", "enbs": []}
        d = next((r[4] for r in fc.quake_regions if r[0] == hypo), None)
        q = {"lat": float(lat), "lon": float(lon), "depth_km": float(depth), "magnitude": float(mag),
             "fault_type_d": d or 0.0}
        cells = _cells(conn)
        arv_path = fc.quake_arv_path
        if arv_path and not Path(arv_path).is_absolute():
            arv_path = str((HERE / arv_path).resolve())
        arv = S.quake_arv(arv_path) if arv_path else {}
        inten = S.quake_cell_intensity(cells, q, arv)
        thr = fc.quake_surge_intensity
        by_enb: dict[str, list] = {}
        n_cells = 0
        for c in cells:
            v = inten.get(c.cell_key)
            if v is None or v < thr:
                continue
            n_cells += 1
            e = by_enb.setdefault(str(c.enb_id), [c.longitude, c.latitude, v, 0])
            e[2] = max(e[2], v)
            e[3] += 1
        enbs = sorted(by_enb)
        radius: dict[str, float] = {}
        if enbs:
            with conn.cursor() as cur:
                cur.execute("SELECT ENB_ID, MAX(RADIUS_M) FROM CM_CELL_GEO_MAP WHERE AREA_LEVEL = 'MESH1K' "
                            "AND ENB_ID IN (" + ",".join(["%s"] * len(enbs)) + ") GROUP BY ENB_ID", enbs)
                radius = {str(e): float(r) for e, r in cur.fetchall() if r is not None}
    except pymysql.err.ProgrammingError as e:
        return {"error": f"표를 읽지 못했습니다: {e.args[-1] if e.args else e}", "enbs": []}
    finally:
        conn.close()

    def band(v: float) -> str:
        return next((b for lo, b in _JMA_BANDS if v >= lo), "3")
    pts = [[round(by_enb[e][0], 6), round(by_enb[e][1], 6), round(radius[e]), band(by_enb[e][2])]
           for e in enbs if e in radius]
    vals = [v for v in inten.values()]
    out = {"fault_id": fault_id, "threshold": thr, "cells": n_cells, "enb": len(enbs), "pts": pts,
           "max": round(max(vals), 2) if vals else None, "arv": bool(arv),
           "d": d, "d_source": "region" if d is not None else "default"}
    _IMPACT_CACHE[fault_id] = out
    return out
