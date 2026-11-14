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
from fastapi import APIRouter, HTTPException
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
    return {
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
    # 상세 배경 (2026-10-30) — 굽는 자리는 graphio `tools/build_jp_geo_layers.py`
    "water":  ("geo_water",  "© OpenStreetMap contributors"),
    "chome":  ("geo_chome",  "e-Stat 境界データ (令和2年国勢調査 小地域)"),
}


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
    hit = _LAYER.get(name)
    if not hit:
        raise HTTPException(404, f"그런 레이어가 없다: {name}")
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
        return {"error": "`pemdb1.graphio_api` 가 비어 있다 — graphio API "
                         "주소를 설정에 적어야 한다", "incidents": []}

    import urllib.error
    import urllib.request

    # `recent=1` — 장애·시나리오 화면에서 회차를 눌러 넘어온 지도만 쓴다. 최근 200건으로는 며칠 지난
    # 회차가 목록에 없어 지도가 그 이력을 고르지 못한다(2026-11-09). 평소 지도는 그대로 200건이다
    url = (f"{base}/api/incidents?scope=1&recent=1" if recent
           else f"{base}/api/incidents?scope=1&limit=200")
    try:
        with urllib.request.urlopen(url, timeout=3) as r:
            doc = json.loads(r.read())
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        # **무엇이 안 되는지 말한다.** "장애 0건"과 "물어볼 데가 없다"는
        # 다른 사실이고, 전자로 보이면 아무도 이상하다고 생각하지 않는다
        return {"error": f"graphio API 에 닿지 않는다 ({url}): {e}",
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
def api_map_event(incident_id: str):
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
        return {"error": "`pemdb1.graphio_api` 가 비어 있다", "enbs": [], "pts": []}
    # 여기서 쓰는 것은 셀과 칸 코드뿐이다 — 둘레·사건 지리 계산을 시키지 않는다
    url = (f"{base}/api/incidents/{urllib.parse.quote(incident_id)}/map"
           "?cells_only=true&with_areas=true&with_members=true")
    try:
        with urllib.request.urlopen(url, timeout=8) as r:
            doc = json.loads(r.read())
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        return {"error": f"graphio API 에 닿지 않는다 ({url}): {e}", "enbs": [], "pts": []}
    at: dict[str, list] = {}
    cells = doc.get("cells") or []
    for c in cells:
        at.setdefault(str(c.get("key") or "").rsplit("-", 1)[0], [c.get("lon"), c.get("lat")])
    return {"enbs": sorted(at), "pts": list(at.values()), "cells": len(cells),
            "unmapped": doc.get("unmapped"), "areas": _event_areas(doc.get("areas") or []),
            "areas_capped": bool(doc.get("areas_capped")),
            "members": _event_members(doc.get("members") or [], cells + list(doc.get("detached_cells") or []))}


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
    """ADM3(읍면동) 축의 **중심점과 모양**을 붙인다 — `CM_DONG_JP` 는 이름·중심좌표뿐이다.

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
    """읍면동 코드 → 이름·중심점(+ 모양이 있으면 `rings`). DB 가 안 닿으면 빈 채로 돌려준다."""
    at: dict[str, dict] = {}
    try:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT DONG_ID, DONG_NAME, LATITUDE, LONGITUDE "
                            "FROM CM_DONG_JP WHERE DONG_ID IN ("
                            + ",".join(["%s"] * len(ids)) + ")", ids)
                for d, name, lat, lon in cur.fetchall():
                    if lat and lon:
                        at[str(d)] = {"id": str(d), "name": name,
                                      "lat": round(float(lat), 6),
                                      "lon": round(float(lon), 6)}
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
    st = {e: "off" for e in cfg.standby_enbs}
    if st:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT START_TIME FROM {T_FAULT} "
                            "WHERE FAULT_KIND='capacity_shift' AND STATUS='active'")
                rows = cur.fetchall()
        finally:
            conn.close()
        now = datetime.now()
        for (start,) in rows:
            b = S.shift_pick(int(start.timestamp() // 60), cfg.standby_enbs)
            el = (now - start).total_seconds() / 60
            if b is None or not 0 <= el < S.SHIFT_TOTAL:
                continue
            st[b] = "on" if el >= S.SHIFT_RISE else ("next" if st[b] == "off" else st[b])
    stamp = json.dumps([cfg.from_enb, sorted(st.items())])
    return {"ver": hashlib.sha1(stamp.encode()).hexdigest()[:12],
            "from": cfg.from_enb, "enbs": st}


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
        return {"error": "`pemdb1.geo_boundary` 가 비어 있다 — 행정경계 파일 "
                         "경로를 설정에 적어야 한다", "features": []}
    src = Path(raw)
    if not src.is_absolute():
        src = (HERE / src).resolve()
    if not src.exists():
        # **빈 배열을 돌려주지 않는다** — "경계가 없다"와 "파일이 없다"는
        # 다른 사실이고, 전자로 보이면 지도가 왜 비었는지 알 수 없다
        return {"error": f"경계 파일이 없다: {src}", "features": []}

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
