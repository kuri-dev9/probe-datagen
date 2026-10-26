"""마스터 토폴로지 관리 — 행정구역 · 장비 · 셀 (`python -m datagen topo`).

`CM_CITY` · `CM_GU` · `CM_DONG` · `CM_CELL_REP_RU_LOC` · `CL_CELL_MAP` 을
사람이 의도적으로 고치거나 생성기가 채우는 자리다. UI(`ui.py`)가 같은 함수를
부른다 — 화면과 커맨드가 각자 SQL 을 쓰면 한쪽만 고쳐질 때 조용히 갈라진다.

## 왜 `pemfill` 이 아니라 별도 커맨드인가

`pemfill` · `run` 은 **1분마다 도는 상주 경로**이고 마스터를 **읽기만** 한다
(`pemdb1.load_cells`). 거기에 쓰기를 넣으면 gen 이 둘 떴을 때 마스터가
흔들리고, 그것은 에러가 아니라 **값이 어긋나는** 종류다. `gp.sh start` 가
"같은 일을 두 주체가 하면 안 된다"고 막는 이유와 같다 — 지금은 gen 이 둘
떠도 마스터가 안전한데, 그 안전을 깨지 않는다.

## charset 은 `utf8mb3` 다 — `load_master.py` 와 다르다

`docker/mysql/load_master.py` 는 `utf8mb4` 로 연다. 여기서는 **`utf8mb3`**
를 쓴다. 상용 테이블 collation 이 `utf8mb3_general_ci` 이고, `utf8mb4` 로
열면 리터럴 비교에서 `Illegal mix of collations` 가 날 수 있다 —
`cli.cmd_pemfill` 과 `ui._connect` 가 이미 그 이유로 `utf8mb3` 다. 이
모듈은 그 둘과 **같은 프로세스 계열**이므로 그쪽에 맞춘다. 한글은 BMP 안이라
둘 다 손실이 없다.

## 좌표 분산 반경은 graphio 의 임계와 묶여 있다

`--spread-km` 기본 0.35km 는 **graphio `detect/detector.py` 의 임계에서
나온 값**이다 —

    detector.py:1512   if radius < 0.9:        return "POINT"
    detector.py:1533   if radius <= 3.0:       return "CLUSTER"

한 eNB 의 셀들을 반경 0.35km 안에 두면 `geo_pattern` 이 그 eNB 를 `POINT`
로 본다. 그것이 `RAN_NODE_DOWN` 의 `expect_geo` 와 일치해 "그 RAN 노드
자체"라는 해석이 나온다(`detector.py:1197` 주석).

**이 숫자는 네 번째 사본이다.** 이미 세 곳이 같은 임계를 전제하고 있다 —
`scenarios.py:1744`(CORRIDOR 조건) · `faults.py:119` · `pemdb1.py:1047`.
한쪽만 고쳐지면 **에러 없이** 의도한 패턴이 안 나오고 채점(`geo_hit`)만
어긋난다. `tests/test_topo.py` 가 graphio 를 임포트해 대조하며, **임포트
실패는 `skip` 이 아니라 `fail`** 이다 — skip 이면 CI 에서 조용히 사라진다.

## 복귀는 사본 추적이 아니라 `dump` 가 보장한다

`.gitignore` 가 `data/` 를 디렉터리째 막는다. `probe-datagen` 은 PUBLIC
저장소이고 그 안의 마스터는 **통신사의 실제 기지국 자산 대장**이므로
`locale/kr/` 에 예외를 넣어선 안 된다. 그래서 파괴적 경로(`rm --all`)는
**적재 직전에 뜬 사본을 선행 조건으로 요구**한다 — 없으면 멈춘다.
`pemdb1.load_drawable_dongs` 가 crosswalk 없이 멈추는 것과 같은 모양이다.
"""

from __future__ import annotations

import gzip
import json
import math
import random
from pathlib import Path

import pymysql

from .geo_lite import decimal_to_dms, dms_to_decimal, haversine_km

HERE = Path(__file__).resolve().parents[2]
LOCALE_DIR = HERE / "data" / "pemdb1" / "locale"

#: graphio `detect/detector.py` 의 지리 패턴 임계. **사본이다** — 머리말의
#: "네 번째 사본" 절 참조. 값을 고치려면 graphio 쪽을 먼저 보고, 테스트가
#: 두 값을 대조하므로 한쪽만 고치면 깨진다.
GEO_POINT_KM = 0.9      # detector.py:1512
GEO_CLUSTER_KM = 3.0    # detector.py:1533

#: 새 eNB 의 ID 대역. 기존과 겹치지 않는 자리를 **실측으로** 골랐다
#: (2026-10-26) — `CL_CELL_MAP` · `CM_CELL_REP_RU_LOC` · `CL_ENB_ORG` ·
#: `PM_COR_LTE_CALL_1M` 네 테이블 모두 `800000~899999` 에 0행이다.
#:
#: `900000+` 은 **쓸 수 없다** — `CM_CELL_REP_RU_LOC` 에 이미 13,483행이
#: 있다(기존 ID 최대값은 171,023,875 로 20bit eNB 범위를 넘는 값까지 섞여
#: 있어서, 단순히 "최대값 + 1" 로 잡으면 varchar(9) 한도에 걸린다).
#:
#: `ENB_ID = ENB_BASE + GU_ID` 로 정한다. `GU_ID` 가 11110~49720 이라
#: 811110~849720 이 되어 대역 안이고, **시군구마다 유일**하며 사람이
#: ID 만 보고 어느 시군구인지 알 수 있다.
ENB_BASE = 800_000


#: 테이블별 키 컬럼 — `rm` 과 중복 판정이 이것을 쓴다. `CM_CELL_REP_RU_LOC`
#: 에는 **PK 가 없고** `ix_local_ru_loc_enb_sector` 인덱스만 있으므로,
#: 키를 코드가 알고 있어야 한다(DDL 에서 읽어낼 수 없다).
TABLES: dict[str, tuple[str, tuple[str, ...]]] = {
    "city": ("CM_CITY", ("CITY_ID",)),
    "gu": ("CM_GU", ("CITY_ID", "GU_ID")),
    "dong": ("CM_DONG", ("CITY_ID", "GU_ID", "DONG_ID")),
    "cell": ("CM_CELL_REP_RU_LOC", ("ENB_ID", "SECTOR")),
    "cellmap": ("CL_CELL_MAP", ("ENB_ID", "CELLNUM")),
}

#: `dump` 가 떠내는 집합. `cellmap` 은 167MB 라 제외한다 — 전환 대상이
#: 아니고(좌표가 없다) 떠내면 복귀 사본이 쓸 수 없는 크기가 된다.
DUMP_TABLES = ("city", "gu", "dong", "cell")

#: `rm --all` 이 받는 테이블. **`cellmap` 이 없다** — `topo` 는 위치 마스터를
#: 다루고 `CL_CELL_MAP` 574,404행은 **셀 대장**이다. 전량 삭제할 이유가 없고,
#: `DUMP_TABLES` 에도 없어서 지우면 되돌릴 원본이 아예 없다. 게이트가 그
#: 제외를 모르던 것이 사고의 모양이었다(`dump_exists` 는 `CM_*` 네 파일만
#: 봤다). 단건(`add`·`rm --key`)은 `TABLES` 로 계속 받는다.
WIPEABLE = DUMP_TABLES

#: 사본 확장자. **지금 정하는 이유:** 복원 함수를 넣는 시점이 포맷을 정할
#: 유일한 자리다 — 나중에 바꾸면 이미 뜬 사본이 **복원 불가**가 된다
#: (`restore` 가 다른 이름을 찾는다). 비압축 159MB 가 `fill-coords` 전마다
#: 뜨라고 하기엔 무겁고, 느리면 사람이 게이트를 우회할 길을 찾는다.
DUMP_SUFFIX = ".json.gz"


# --------------------------------------------------------------------------
# 접속 · 적재 — `docker/mysql/load_master.py` 의 규율을 재사용한다
# --------------------------------------------------------------------------


def connect(cfg: dict, database: str | None = None):
    my = cfg["mysql"]
    db = database or (cfg.get("pemdb1") or {}).get("database", "pemdb6")
    return pymysql.connect(
        host=my["host"], port=int(my["port"]), user=my["user"],
        password=my["password"], database=db,
        # 머리말 "charset 은 utf8mb3 다" 참조. 어긋나면 한글이 '?' 로 들어가고
        # 그것은 에러가 아니라 **성공한 INSERT** 로 돌아온다.
        charset="utf8mb3", autocommit=False,
    )


def ddl_columns(conn, table: str) -> set[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = DATABASE() AND table_name = %s", [table])
        return {r[0] for r in cur.fetchall()}


def insert_rows(conn, table: str, rows: list[dict]) -> int:
    """행 추가. **DDL 에 있는 컬럼만 넣는다.**

    `load_master.insert` 와 같은 이유다 — 반출 JSON·생성 dict 에 DDL 에 없는
    키가 섞이면 `Unknown column` 으로 적재가 통째로 깨지고, DDL 에 컬럼을
    더하면 로컬이 상용과 다른 구조가 되어 두 환경을 대조할 수 없다.

    **`DELETE` 를 하지 않는다.** 전량 재적재는 `wipe()` 가 따로 하고, 그쪽은
    `rm --all` 에서만 불린다.
    """
    if not rows:
        return 0
    have = ddl_columns(conn, table)
    cols = [c for c in rows[0] if c in have]
    dropped = [c for c in rows[0] if c not in have]
    if dropped:
        print(f"  ⚠ DDL 에 없는 컬럼 제외: {', '.join(dropped)}")
    marks = ",".join(["%s"] * len(cols))
    collist = ",".join(f"`{c}`" for c in cols)
    data = [tuple(r.get(c) for c in cols) for r in rows]
    with conn.cursor() as cur:
        for i in range(0, len(data), 5000):
            cur.executemany(
                f"INSERT INTO `{table}` ({collist}) VALUES ({marks})", data[i:i + 5000])
    conn.commit()
    return len(data)


def wipe(conn, table: str) -> int:
    with conn.cursor() as cur:
        n = cur.execute(f"DELETE FROM `{table}`")
    conn.commit()
    return n


# --------------------------------------------------------------------------
# crosswalk — 그릴 수 있는 동만 쓴다
# --------------------------------------------------------------------------


def drawable_dongs(path: Path) -> dict[tuple[int, int], list[int]]:
    """`(CITY_ID, GU_ID)` → 그 시군구의 **폴리곤 있는** `DONG_ID` 목록.

    `pemdb1.load_drawable_dongs` 와 **같은 파일·같은 집합**을 읽는다. 그쪽은
    `set` 하나를 주고 여기는 시군구별로 가른다 — `seed-enb` 가 "이 시군구에서
    고를 수 있는 동"을 물어야 하기 때문이다.

    **왜 필요한가:** `pemdb1._filter_drawable:985` 가 `"<city>-<gu>-<dong>"`
    이 이 집합에 없는 셀을 **버린다.** 설정 4개 전부에 `geo_crosswalk` 가
    켜져 있어 상시 적용이므로, 여기 없는 동에 셀을 놓으면 `pemfill` 이
    조용히 쓰지 않는다 — 에러가 아니라 "그 지역에 트래픽이 없다"로 보인다.

    **파일이 없으면 멈춘다** (`load_drawable_dongs` 와 같은 선택). 없는 집합을
    추측으로 만들면 지도가 어긋나고 어디서 어긋났는지 알 방법이 없다.
    """
    if not path.exists():
        raise RuntimeError(
            f"행정코드 crosswalk 가 없다: {path}\n"
            "  graphio 쪽에서 먼저 만들어라 — python -m graphio geo-xw")
    xw = json.loads(path.read_text(encoding="utf-8"))
    emd = ((xw.get("map") or {}).get("emd") or {})
    if not emd:
        raise RuntimeError(f"crosswalk 에 map.emd 가 비어 있다: {path}")
    out: dict[tuple[int, int], list[int]] = {}
    # 키는 geojson 코드, **값이 pemdb1 코드**다. 뒤집어 쓰면 전량 무매칭이 된다.
    for pem in emd.values():
        parts = str(pem).split("-")
        if len(parts) != 3:
            continue
        try:
            city, gu, dong = int(parts[0]), int(parts[1]), int(parts[2])
        except ValueError:
            continue
        out.setdefault((city, gu), []).append(dong)
    for v in out.values():
        v.sort()        # 결정론 — 같은 입력이 같은 배치를 내야 한다
    return out


# --------------------------------------------------------------------------
# 좌표 생성
# --------------------------------------------------------------------------


def scatter(lat: float, lon: float, n: int, spread_km: float,
            rng: random.Random) -> list[tuple[float, float]]:
    """기준점 주변 `n` 개 좌표. **최원단이 `spread_km` 를 넘지 않는다.**

    `geo_pattern` 의 반경은 "중심에서 가장 먼 점까지"이므로, 상한을 지켜야
    패턴이 의도대로 나온다(머리말의 임계 절 참조). 균등 면적 분포를 쓰되
    반지름에 `sqrt` 를 걸어 가운데로 뭉치지 않게 한다.

    경도 환산에 `cos(lat)` 을 쓴다 — 고정 계수(88.0 등)를 쓰면 위도대가
    달라질 때 분산이 한 축으로 찌그러진다.
    """
    out: list[tuple[float, float]] = []
    km_per_deg_lat = 111.32
    km_per_deg_lon = max(1e-6, 111.32 * math.cos(math.radians(lat)))
    for _ in range(n):
        r = spread_km * math.sqrt(rng.random())
        th = rng.uniform(0.0, 2 * math.pi)
        out.append((lat + (r * math.sin(th)) / km_per_deg_lat,
                    lon + (r * math.cos(th)) / km_per_deg_lon))
    return out


def pattern_of(points: list[tuple[float, float]]) -> tuple[str, float]:
    """`geo_pattern` 이 이 좌표들에 줄 패턴 — **생성 검증용 축약판이다.**

    정본은 graphio `detector.geo_pattern` 이고 CORRIDOR·WIDE 판별까지 한다.
    여기서는 생성한 좌표가 의도한 `POINT`/`CLUSTER` 범위에 들어가는지만
    보므로 반경 두 단계로 충분하다 — **판정에 쓰지 않는다.**
    """
    if not points:
        return "UNKNOWN", 0.0
    if len(points) == 1:
        return "POINT", 0.0
    lat = sum(p[0] for p in points) / len(points)
    lon = sum(p[1] for p in points) / len(points)
    radius = max(haversine_km(lat, lon, a, o) for a, o in points)
    if radius < GEO_POINT_KM:
        return "POINT", radius
    if radius <= GEO_CLUSTER_KM:
        return "CLUSTER", radius
    return "SCATTER", radius


# --------------------------------------------------------------------------
# 조회
# --------------------------------------------------------------------------


def _rows(conn, sql: str, params: list | None = None) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(sql, params or [])
        return list(cur.fetchall())


def survey(conn) -> dict[str, dict]:
    """테이블별 행수와 **좌표 결손 수**. 0 과 NULL 을 가른다.

    좌표 없는 행을 "0개"로 세면 미매핑이 보이지 않는다 (graphio 불변규칙
    12·17). 활성 셀 쪽은 1분 집계에 실제로 나타나는 셀만 센다 — 마스터
    44만 행 중 트래픽이 붙는 것은 일부이고, 지도·탐지에 나타나는 것은
    그쪽뿐이다.
    """
    out: dict[str, dict] = {}
    for key, (table, _) in TABLES.items():
        total = _rows(conn, f"SELECT COUNT(*) FROM `{table}`")[0][0]
        info = {"table": table, "rows": total}
        if key in ("city", "gu", "dong", "cell"):
            info["no_coord"] = _rows(
                conn,
                f"SELECT COUNT(*) FROM `{table}` "
                "WHERE LATITUDE IS NULL OR LONGITUDE IS NULL "
                "OR LATITUDE = '' OR LONGITUDE = ''")[0][0]
        out[key] = info

    active = _rows(conn, "SELECT COUNT(*) FROM (SELECT DISTINCT ENB_ID, CELLNUM "
                         "FROM PM_COR_LTE_CALL_1M) t")[0][0]
    missing = _rows(conn, """
        SELECT COUNT(*) FROM (
          SELECT DISTINCT a.ENB_ID, a.CELLNUM
            FROM PM_COR_LTE_CALL_1M a
            LEFT JOIN CM_CELL_REP_RU_LOC l
                   ON l.ENB_ID = a.ENB_ID AND l.SECTOR = a.CELLNUM
           WHERE l.ENB_ID IS NULL) t""")[0][0]
    out["_active"] = {"cells": active, "unmapped": missing,
                      "enbs": _rows(conn, "SELECT COUNT(DISTINCT ENB_ID) "
                                          "FROM PM_COR_LTE_CALL_1M")[0][0]}
    return out


def unmapped_active_cells(conn) -> list[tuple[str, int]]:
    """1분 집계에 나타나는데 `CM_CELL_REP_RU_LOC` 에 **행이 없는** 셀.

    **실측(2026-10-26): 804건 전부가 "행 자체가 없음"이고, "행은 있고 좌표가
    NULL" 은 0건이다.** 그래서 `fill-coords` 는 UPDATE 가 아니라 INSERT 다 —
    이 구분을 잘못 잡으면 0행 UPDATE 가 "성공"으로 돌아와 아무것도 안 고친
    채 끝난다.
    """
    return [(str(e), int(c)) for e, c in _rows(conn, """
        SELECT DISTINCT a.ENB_ID, a.CELLNUM
          FROM PM_COR_LTE_CALL_1M a
          LEFT JOIN CM_CELL_REP_RU_LOC l
                 ON l.ENB_ID = a.ENB_ID AND l.SECTOR = a.CELLNUM
         WHERE l.ENB_ID IS NULL
         ORDER BY a.ENB_ID, a.CELLNUM""")]


def enb_anchor(conn, enb: str) -> tuple[float, float] | None:
    """그 eNB 의 **이미 매핑된 섹터들의 중심**. 없으면 `None`.

    `CM_GU` 대표좌표보다 이쪽을 먼저 쓴다. eNB 가 시군구 중심에서 수 km
    떨어져 있으면, 같은 eNB 의 새 셀을 시군구 중심에 놓는 순간 그 eNB 가
    `POINT` 가 아니라 `SCATTER` 로 보인다 — **없는 광역 장애가 만들어진다.**
    실측으로 미매핑 804개 중 768개(95.5%)가 자기 eNB 의 다른 섹터 좌표를
    이미 갖고 있다.
    """
    pts: list[tuple[float, float]] = []
    for la, lo in _rows(conn, "SELECT LATITUDE, LONGITUDE FROM CM_CELL_REP_RU_LOC "
                              "WHERE ENB_ID = %s", [enb]):
        lat, lon = dms_to_decimal(la), dms_to_decimal(lo)
        if lat is not None and lon is not None:
            pts.append((lat, lon))
    if not pts:
        return None
    return (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))


def gu_points(conn) -> list[tuple[int, int, str, float, float]]:
    """좌표가 있는 시군구. `(CITY_ID, GU_ID, GU_NAME, lat, lon)`.

    좌표 없는 시군구는 **빼고 돌려준다** — 기준점이 없으면 셀을 놓을 수
    없고, 0,0 으로 채우면 기니만 바다에 장애가 생긴다(불변규칙 17).
    호출부가 몇 곳이 빠졌는지 말해야 한다.
    """
    return [(int(c), int(g), str(n), float(la), float(lo))
            for c, g, n, la, lo in _rows(conn, """
        SELECT CITY_ID, GU_ID, GU_NAME, LATITUDE, LONGITUDE
          FROM CM_GU
         WHERE LATITUDE IS NOT NULL AND LONGITUDE IS NOT NULL
         ORDER BY GU_ID""")]


# --------------------------------------------------------------------------
# 덤프 — 복귀의 유일한 보장
# --------------------------------------------------------------------------


def dump_path(locale: str, key: str) -> Path:
    """사본 파일 경로 — **정본이 이 함수 하나다.**

    `dump`·`restore`·`dump_exists`·UI 응답이 각자 이름을 조립하면 확장자를
    바꿀 때 한 곳이 남고, 그 한 곳은 **파일을 못 찾았다**가 아니라 "사본이
    없다"로 보인다. 그러면 게이트가 멈추고 사람은 사본을 또 뜬다.
    """
    return LOCALE_DIR / locale / f"{TABLES[key][0]}{DUMP_SUFFIX}"


def dump(conn, locale: str = "kr") -> dict[str, int]:
    """현 DB 의 마스터를 `data/pemdb1/locale/<locale>/` 로 떠낸다.

    **추적 밖이다**(`.gitignore` 가 `data/` 를 디렉터리째 막는다). 그것이
    의도다 — `probe-datagen` 은 PUBLIC 저장소이고 이 내용은 실제 기지국
    자산 대장이다. 복귀 사본은 **로컬에만** 있어야 한다.

    `json.dumps(..., default=str)` 를 유지한다 — 실측으로 네 테이블에
    `Decimal`·`datetime`·`bytes` 가 없어 **한 번도 발동하지 않지만**,
    컬럼이 추가됐을 때 조용히 `TypeError` 로 죽는 것보다 문자열로라도
    남기는 쪽이 낫다. 좌표는 DMS 문자열(`36:37.15/60`)이다.
    """
    (LOCALE_DIR / locale).mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    for key in DUMP_TABLES:
        table, _ = TABLES[key]
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(f"SELECT * FROM `{table}`")
            rows = cur.fetchall()
        with gzip.open(dump_path(locale, key), "wt", encoding="utf-8") as fh:
            json.dump(rows, fh, ensure_ascii=False, default=str)
        counts[table] = len(rows)
    return counts


def dump_exists(locale: str = "kr") -> bool:
    """사본 **파일이 있는지**만 본다 — 게이트로 쓰지 않는다.

    **파일의 존재는 복귀를 뜻하지 않는다.** 한동안 이것이 파괴적 경로의
    선행 조건이었는데, 그때 **그 JSON 을 DB 로 되넣는 코드가 없었다** —
    게이트는 "되돌릴 수 있다"가 아니라 "파일 4개가 존재한다"를 통과시키고
    있었고 그것은 안심만 준다. 게이트는 `restore(dry_run=True)` 다.
    화면 표시용으로만 남긴다.
    """
    return all(dump_path(locale, k).exists() for k in DUMP_TABLES)


def ddl_required(conn, table: str) -> set[str]:
    """`NOT NULL` 이고 기본값도 auto_increment 도 없는 컬럼.

    사본에 이 컬럼이 없으면 적재가 `1364 Field doesn't have a default value`
    로 깨진다 — `restore(dry_run=True)` 가 **쓰기 전에** 그것을 말해야 한다.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name = %s "
            "  AND is_nullable = 'NO' AND column_default IS NULL "
            "  AND extra NOT LIKE '%%auto_increment%%'", [table])
        return {r[0] for r in cur.fetchall()}


def restore(conn, locale: str = "kr", dry_run: bool = True) -> dict:
    """사본을 DB 로 되넣는다 — **복귀의 실행부**.

    `dry_run=True` 면 **아무것도 쓰지 않고** "읽어서 넣을 수 있는가"만
    답한다. 그것이 파괴적 경로의 게이트다(`cli._topo_gate` · `ui._require_dump`).
    확인하는 것 넷 —

    * 네 사본이 **다 있는가** (반쪽 복귀는 "복구했다"고 믿는 상태라 더 나쁘다)
    * gzip·JSON 으로 **읽히는가** (중간에 끊긴 파일은 존재하지만 쓸 수 없다)
    * DDL 과의 **컬럼 교집합** (사본에만 있는 컬럼은 `insert_rows` 가 빼고 알린다)
    * **필수 컬럼 결손** (`NOT NULL` + 기본값 없음이 사본에 없으면 적재가 깨진다)

    실제 복구는 `wipe()` 후 `insert_rows()` 다. 새 로직이 거의 없는 이유는
    `insert_rows` 가 이미 DDL 교집합으로 걸러 주기 때문이고, **그 함수를
    공유하는 것이 의도다** — 복구 전용 적재 경로를 따로 쓰면 `dump` 와
    `restore` 가 서로 다른 컬럼 규칙을 갖게 된다.

    **순서는 `DUMP_TABLES` 순(city → gu → dong → cell)이다.** `pemdb6` 에
    외래키가 없어(실측 0건) 순서가 적재를 막지는 않지만, 중간에 멈췄을 때
    상위 계층부터 들어가 있는 편이 읽기 쉽다.

    ## 복구 범위는 **DB 네 테이블뿐이다**

    `data/geo/` 를 보지 않는다 — `code_crosswalk.json` 은 이 경로 밖이다.
    그래서 `seed-geo --locale jp` 뒤에 `restore kr` 을 해도 DB 는 돌아오지만
    그 나라의 crosswalk 는 **그 자리에 그대로 남는다.** 지금은
    `crosswalk_path` 가 나라별로 갈라 서로를 덮지 않지만, "되돌렸다"가
    실제보다 넓게 읽히면 안 되므로 **반환값이 그 사실을 말한다**
    (`scope` · 불변규칙 17 의 결).
    """
    tables: dict[str, dict] = {}
    ok = True
    for key in DUMP_TABLES:
        table, _ = TABLES[key]
        path = dump_path(locale, key)
        info: dict = {"table": table, "path": str(path)}
        if not path.exists():
            info["error"] = "사본이 없다"
            ok = False
            tables[key] = info
            continue
        try:
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                rows = json.load(fh)
        except (OSError, EOFError, ValueError) as e:      # gzip 손상 · JSON 깨짐
            info["error"] = f"읽을 수 없다: {e}"
            ok = False
            tables[key] = info
            continue
        info["rows"] = len(rows)
        have = ddl_columns(conn, table)
        cols = set(rows[0]) if rows else set()
        info["dropped"] = sorted(cols - have)     # 사본에만 있는 컬럼
        missing = (ddl_required(conn, table) - cols) if rows else set()
        info["missing_required"] = sorted(missing)
        if missing:
            info["error"] = f"필수 컬럼이 사본에 없다: {', '.join(sorted(missing))}"
            ok = False
        tables[key] = info

    out = {
        "ok": ok, "dry_run": dry_run, "locale": locale, "tables": tables,
        # **되돌렸다고 읽히면 안 된다.** 호출부(CLI·UI)가 이 두 줄을 그대로
        # 사람에게 내린다.
        "scope": [TABLES[k][0] for k in DUMP_TABLES],
        "not_restored": [
            f"{XW_FILE} (행정코드 crosswalk) — `data/geo/` 는 이 경로 밖이다. "
            "나라를 바꿨다면 그쪽 crosswalk 는 그대로 남아 있다",
            "CL_CELL_MAP (셀 대장 · 167MB) — `DUMP_TABLES` 밖이라 사본이 없다",
        ],
    }
    if dry_run or not ok:
        # **못 넣을 상태면 지우지 않는다.** `wipe` 를 먼저 하는 구조라
        # 하나라도 틀렸을 때 진행하면 "지웠는데 못 넣는" 상태가 된다.
        return out
    for key in DUMP_TABLES:
        table, _ = TABLES[key]
        with gzip.open(dump_path(locale, key), "rt", encoding="utf-8") as fh:
            rows = json.load(fh)
        tables[key]["deleted"] = wipe(conn, table)
        tables[key]["inserted"] = insert_rows(conn, table, rows)
    return out


# --------------------------------------------------------------------------
# 생성
# --------------------------------------------------------------------------


def seed_enb(conn, xw_path: Path, per_gu: int = 1, cells_per_enb: int = 3,
             spread_km: float = 0.35, seed: int = 20260903,
             dry_run: bool = False) -> dict:
    """시군구마다 eNB 를 놓고 그 하위 셀을 만든다.

    `ENB_ID = ENB_BASE + GU_ID` (`per_gu` 가 2 이상이면 `+ i * 100_000` 은
    대역을 벗어나므로 `GU_ID` 뒤에 순번을 더하지 않고 **대역 안에서 오프셋**
    을 둔다 — 아래 구현 참조).

    **`DONG_ID` 는 그 시군구의 crosswalk drawable 동에서 고른다.** 대표 동
    하나로 고정하지 않는다 — 그러면 그 시군구의 모든 신규 셀이 한 동에 몰려
    읍면동 드릴다운이 거짓을 말한다(graphio 불변규칙 18).

    **drawable 동이 없는 시군구는 `DONG_ID` 를 `NULL` 로 둔다.** 0 으로 채우면
    `_filter_drawable` 이 조용히 버리고(`"<city>-<gu>-0"` 는 집합에 없다),
    `NULL` 이면 같은 함수의 `CITY_ID is None` 가지로 통과해 **미매핑으로
    드러난다** — `H-07` 과 UNMAPPED 패널의 검증 대상으로 남는다.

    `CM_CELL_REP_RU_LOC.DONG_ID` 는 `NOT NULL` 이므로 `CITY_ID` 까지 함께
    비운다. 셋 중 하나만 비우면 DDL 이 거부한다.
    """
    drawable = drawable_dongs(xw_path)
    gus = gu_points(conn)
    rng = random.Random(seed)

    loc_rows: list[dict] = []
    map_rows: list[dict] = []
    no_dong: list[int] = []
    patterns: dict[str, int] = {}

    for city, gu, name, lat, lon in gus:
        dongs = drawable.get((city, gu), [])
        if not dongs:
            no_dong.append(gu)
        for i in range(per_gu):
            # 대역(800000~899999) 안에 머물러야 한다. `GU_ID` 최대 49720 이라
            # `+ 50000 * i` 로 i<2 까지 안전하고, 그 이상은 명시적으로 막는다.
            if i >= 2:
                raise ValueError(
                    "per-gu 는 2 까지다 — ENB_ID 대역(800000~899999)이 "
                    f"GU_ID 최대 49720 에 대해 오프셋 2 칸만 허용한다")
            enb = str(ENB_BASE + gu + 50_000 * i)
            pts = scatter(lat, lon, cells_per_enb, spread_km, rng)
            pat, _ = pattern_of(pts)
            patterns[pat] = patterns.get(pat, 0) + 1
            for sector, (clat, clon) in enumerate(pts):
                dong = dongs[(sector + i) % len(dongs)] if dongs else None
                loc_rows.append({
                    "ENB_ID": enb, "SECTOR": sector,
                    # drawable 동이 없으면 셋을 함께 비운다 (docstring 참조)
                    "CITY_ID": city if dong is not None else None,
                    "GU_ID": gu if dong is not None else None,
                    "DONG_ID": dong,
                    "LATITUDE": decimal_to_dms(clat),
                    "LONGITUDE": decimal_to_dms(clon),
                    # 이름은 짧게 — `RU_NAME` 229,148종의 `_ci` 충돌을 늘리지
                    # 않으려고 사람이 읽을 수 있는 최소형만 쓴다
                    "RU_NAME": f"{name}-{sector}",
                    "RU_CUID": None, "FLAG": None, "DECODE": enb,
                })
                map_rows.append({
                    "ENB_ID": enb, "CELLNUM": sector, "SECTOR": sector,
                    "CELL_ID": sector, "C_UID": None, "MME_GRP_ID": None,
                    "FA": 1, "PCI": (int(enb) + sector) % 504, "ADM_STATE": 1,
                })

    # `DONG_ID` 가 `NOT NULL` 이라 `None` 행은 적재가 거부된다. **그것이
    # 맞다** — 비울 수 없으면 그 시군구는 대상이 아니고, 조용히 0 으로
    # 바꿔 넣으면 `pemfill` 이 버린다. 거부된 행 수를 말하고 넘긴다.
    ok = [r for r in loc_rows if r["DONG_ID"] is not None]
    keys = {(r["ENB_ID"], r["SECTOR"]) for r in ok}
    if not dry_run:
        insert_rows(conn, "CM_CELL_REP_RU_LOC", ok)
        insert_rows(conn, "CL_CELL_MAP",
                    [r for r in map_rows if (r["ENB_ID"], r["CELLNUM"]) in keys])

    # `keys` 를 돌려주는 것은 `pick_report(extra_cells=…)` 때문이다 —
    # `--dry-run` 에서도 "이것들을 넣으면 `enb_limit` 이 얼마여야 하는가"를
    # 답해야 하고, 그 답은 **적재 전에** 필요하다(적재 규모 판단이 사용자
    # 몫이므로 설정을 여기서 고치지 않는다).
    return {"gus": len(gus), "enbs": len(gus) * per_gu,
            "cells": len(loc_rows), "skipped_no_dong": len(loc_rows) - len(ok),
            "gus_without_drawable_dong": no_dong, "patterns": patterns,
            "keys": sorted(keys)}



# --------------------------------------------------------------------------
# 시뮬레이션 선택 대조 — "마스터에는 있고 시뮬레이션에만 없는" 상태를 드러낸다
# --------------------------------------------------------------------------


NEW_BAND = (ENB_BASE, ENB_BASE + 99_999)

#: `topo` 가 만든 셀의 트래픽 가중 배수. `pemdb1._weight` 가 곱한다.
#:
#: **왜 1.0 이 아닌가.** `_pick_enbs` 가 이 대역을 한도 밖에서 전부 포함하므로
#: (`pemdb1._pick_enbs` 의 include 절), 신규 셀이 기존과 같은 가중을 받으면
#: 분당 호 수가 그쪽으로 나뉘어 **기존 eNB 의 표본이 깎인다.** 커버리지와
#: 판정(최소 표본 20 · 불변규칙 5)은 다른 목적이고, 전자를 얻으려고 후자를
#: 뒤로 미루지 않는다.
#:
#: **왜 0.1 이 아닌가 — 0.5 가 실측으로 정해졌다** (2026-10-26).
#:
#: * **채점 트리거는 이 수에 무차별하다.** `ground_truth` 는 SC-21 ·
#:   `expect_kpi: U-12` 이고 U-12 의 분모는 호가 아니라 **패킷**이다
#:   (`min_denom=800` · graphio `detector.py:832`·`kpi.py:178`). 가중을 0.1
#:   까지 내려도 신규 셀이 1,764패킷/분이라 **이미 1분 창**이고, 올려도
#:   당겨질 곳이 없다.
#: * **비용은 정답 셀 1개가 15분 → 30분 창**이다. 미달로 떨어지지 않고
#:   1분 창 32개·5분 창 2개는 그대로다. 그 셀은 이미 1분 창 밖이라 채점
#:   민감도가 낮은 자리다.
#: * **얻는 것이 이 작업의 목적 자체다.** 217개 신규 시군구의 C-Plane 이
#:   30분 → 5분 창이 된다. **`--per-gu 1` 이라 합산할 형제 eNB 가 없어서
#:   eNB 축의 창이 곧 그 시군구 지도 색의 창이다** — "시군구 집계는 카운트
#:   합산이니 괜찮다"(불변규칙 2)가 여기서는 위안이 되지 않는다. 0.1 로
#:   두면 `seed-enb` 가 반쪽이 된다.
#: * 0.3·1.0 은 지배당한다 — 0.3 은 비용이 같고(1셀) 이득만 적고(신규
#:   C-Plane 15분), 1.0 은 이득이 같고(5분) 비용만 크다(악화 3셀).
#:
#: 출처: 에스터 weight 4종 측정표(창고 35분 실측 · `volume_pct=30` ·
#: `PM_COR_UPLANE_CELL_1M` 정답 셀 40/40) + 메이커 교차표(실 후보 풀 ·
#: 신규 217×3=651셀 · 기존 몫 1.0→78.2% / 0.5→87.8% / 0.1→97.3%).
#:
#: **`config` 가 아니라 상수인 이유:** 이 값은 운영 정책이 아니라 **선택
#: 규칙과 묶인 내부 수**다. `enb_limit`·`volume_pct` 처럼 사람이 조절하는
#: 축으로 노출하면, 셋을 각자 돌리다 "기존 판정이 어느 창에서 차는가"가
#: 조합마다 달라져 회귀를 비교할 수 없다. 바꿀 일이 생기면 **측정 근거와
#: 함께** 이 줄을 고친다.
TOPO_WEIGHT = 0.5


def in_new_band(enb) -> bool:
    """`topo seed-enb` 가 놓은 eNB 인가. 대역으로만 판정한다 — 별도 표식
    컬럼을 만들면 상용 DDL 이 바뀐다(`pemdb6` 가 그대로 운영이다)."""
    try:
        v = int(enb)
    except (TypeError, ValueError):
        return False
    return NEW_BAND[0] <= v <= NEW_BAND[1]


def pick_report(conn, xw_path: Path | None, enb_limit: int,
                extra_cells=()) -> dict:
    """`pemfill` 이 **실제로** 고를 eNB 집합과, 새 대역이 들어가려면 필요한 한도.

    ## 왜 이 함수가 필요한가

    마스터에 eNB 를 넣는 것과 **그 eNB 가 트래픽을 받는 것은 다른 일**이다.
    `pemdb1._pick_enbs` 는 좌표 있는 eNB 를 **셀 수 내림차순**으로 `enb_limit`
    칸까지만 고른다. `seed-enb` 가 만드는 eNB 는 셀이 3개뿐이라 29,806개 중
    꼬리에 붙고, `enb_limit: 60` 이면 **한 개도 선택되지 않는다.**

    그런데 그 상태에 **에러도 경고도 없다** — 마스터 조회(`list`)는 행을
    보여주고 시뮬레이션에는 없다. `topo` 로 장비를 놓은 사람은 트래픽을
    기다리다 "생성기가 고장났다"고 읽는다. 그래서 `list` 가 이 대조를 열로
    보여준다.

    ## 선택 규칙을 복사하지 않는다

    `pemdb1._pick_enbs` · `_filter_drawable` 을 **그대로 부른다**. 정렬 규칙을
    여기 옮겨 적으면 한쪽만 고쳐질 때 `list` 가 **틀린 안심**을 준다 — 통과라고
    적어 두고 실제로는 빠지는 상태가 가장 나쁘다. `load_cells(enb_limit=0)`
    으로 후보 전량을 받아(실측 369,581셀 / 2.8초) `_pick_enbs` 에 먹인다.
    `_pick_enbs` 는 행의 `r[5]`(=`CITY_ID`)만 보므로 최소 튜플로 재구성한다.

    **"필요한 한도"를 더는 계산하지 않는다** (2026-10-26). 실측 23,586 은
    한도가 아니라 **해제**였고, `_pick_enbs` 가 대역을 한도 밖에서 포함하게
    바뀐 뒤로는 물을 필요가 없는 수다. `band_missing` 이 비지 않으면 그것은
    "한도가 작다"가 아니라 **include 가 깨졌다**는 뜻이다.

    `extra_cells` 는 **아직 DB 에 없는** 셀 `(ENB_ID, SECTOR)` 들이다 —
    `seed-enb --dry-run` 이 "넣으면 한도가 얼마여야 하는가"를 묻는 자리다.
    좌표를 함께 넣으므로 전부 `MAPPED` 로 센다.
    """
    from . import pemdb1 as P        # 지연 임포트 — `list` 밖에서는 안 쓴다

    # `pemfill` 과 **같은 인자 모양**이다 — 그쪽도 설정에 `geo_crosswalk` 가
    # 없으면 `drawable=None` 으로 전량을 후보로 둔다(`cmd_pemfill:425`).
    # 여기서 없는 경로를 지어내거나 반대로 거부하면 대조가 거짓이 된다.
    drawable = P.load_drawable_dongs(xw_path) if xw_path else None
    full = P.load_cells(conn, enb_limit=0, drawable=drawable)
    # `_pick_enbs` 가 읽는 것은 인덱스 5(`CITY_ID`) 하나뿐이다. 나머지를
    # 채우면 이 함수가 `load_cells` 의 행 모양에 묶인다.
    uniq: dict[tuple[str, int], tuple] = {
        (c.enb_id, c.cell_id): (None,) * 5 + (
            0 if c.geo_status == "MAPPED" else None,)
        for c in full
    }
    for enb, sector in extra_cells:
        uniq[(str(enb), int(sector))] = (None,) * 5 + (0,)

    all_enbs = {e for e, _ in uniq}

    def pick(limit: int) -> set[str]:
        return P._pick_enbs(uniq, limit) or set(all_enbs)

    picked = pick(enb_limit)
    band = {e for e in all_enbs if in_new_band(e)}
    missing = band - picked

    return {
        "enb_limit": enb_limit,
        "candidates": len(all_enbs),
        "candidate_cells": len(uniq),
        "picked_enbs": len(picked),
        "picked_cells": sum(1 for e, _ in uniq if e in picked),
        "band_master": len(band),
        "band_picked": len(band - missing),
        "band_missing": sorted(missing, key=lambda e: int(e)),
    }


def coord_gaps(uniq: dict, keep: set[str] | None) -> list[tuple[str, int]]:
    """좌표를 만들어 줘야 하는 셀 — **`load_cells` 의 `uniq` 를 그대로 받는다.**

    조건 셋을 **전부** 만족하는 것만 돌려준다.

    1. 좌표가 없다 (`LATITUDE`·`LONGITUDE` 중 하나라도 `NULL`. 행 자체가
       없으면 `load_cells` 의 LEFT JOIN 이 `NULL` 로 준다 — 실측 804건이
       모두 이쪽이고 "행은 있고 좌표가 NULL" 은 0건이다)
    2. **그 eNB 의 다른 섹터는 좌표를 갖고 있다** (`geo[enb]`)
    3. 생성 대상 eNB 다 (`_pick_enbs` 가 고른 것)

    ## 2번이 이 함수의 핵이다 — **좌표 없는 eNB 는 손대지 않는다**

    `_pick_enbs` docstring —

    > *"좌표가 있는 eNB 를 셀 수 많은 순으로 채우고, **마지막 5% 는 좌표
    > 없는 eNB** 로 남긴다 — 미매핑 셀이 0이면 `H-07`(미매핑 감시)과
    > UNMAPPED 패널이 한 번도 검증되지 않는다(불변규칙 12)."*

    `_filter_drawable` 도 같은 말을 한다 — *"좌표 없는 셀은 **그대로
    둔다**"*. 즉 미매핑 셀의 존재는 결손이 아니라 **설계**다. 실측 804건은
    두 종류가 섞여 있다 —

        804 = 108  좌표 없는 eNB 3개(36셀씩)   ← 의도된 미매핑. **건드리지 않는다**
            + 696  좌표 있는 eNB 56개의 섹터 결손 ← 이쪽만 만든다

    전부 채우면 `H-07` 과 UNMAPPED 패널의 검증 경로가 **영구히 죽는다.**
    """
    geo: dict[str, bool] = {}
    for (enb, _cn), r in uniq.items():
        geo[enb] = geo.get(enb, False) or (r[8] is not None and r[9] is not None)
    return [(enb, cn) for (enb, cn), r in sorted(uniq.items())
            if (r[8] is None or r[9] is None)
            and geo.get(enb)                       # 2번 — 의도된 미매핑 제외
            and (keep is None or enb in keep)]


def ensure_sector_coords(conn, gaps: list[tuple[str, int]],
                         spread_km: float = 0.35, seed: int = 20260903,
                         dry_run: bool = False) -> dict:
    """`coord_gaps` 가 준 셀에 좌표를 만든다 — **생성기 경로의 백필 대체다.**

    ## 왜 메모리 부여가 아니라 DB INSERT 인가

    graphio 가 `CM_CELL_REP_RU_LOC` 를 **직접 읽는다.** 생성기 메모리에만
    좌표를 주면 xDR 은 나오지만 집계·지도는 여전히 미매핑이다 —

        graphio detect/detector.py:229   "좌표를 못 읽은 셀은 담지 않는다.
                                          CM_CELL_REP_RU_LOC.LATITUDE 는 …"
        graphio metrics/kpi.py:309       CM_CELL_REP_RU_LOC + CM_CITY/GU/DONG 4단 조인

    그래서 INSERT 자체는 수동 백필과 같다. **다른 것은 사람 손이 빠진다는
    것이다** — `load_cells(ensure_coords=True)` 가 매번 부르므로 현장에서도
    자동으로 돌고, "누가 커맨드를 쳤는가"가 사라진다.

    ## 지키는 것

    * **INSERT 만 한다.** 기존 행을 지우거나 고치지 않는다.
    * **활성 셀 키 집합을 바꾸지 않는다.** 대상은 이미 `CL_CELL_MAP` ·
      1분 집계에 있는 키뿐이고(`coord_gaps` 가 `uniq` 에서 고른다), 새 키를
      만들지 않는다. `ground_truth.json` 이 참조하는 집합이 그쪽이다.
    * **멱등이다.** 들어갈 섹터를 넣기 전에 **그 eNB 의 현재 섹터 목록**을
      조회해 이미 있는 것을 뺀다. 두 번째 호출은 `coord_gaps` 단계에서 이미
      비지만(좌표가 채워졌다), 한 프로세스가 `uniq` 를 들고 두 번 부르는
      경우를 여기서도 막는다 — 중복 INSERT 는 `CM_CELL_REP_RU_LOC` 에 PK 가
      없어서 **에러 없이** 통과한다.

    기준점은 `enb_anchor`(그 eNB 의 이미 매핑된 섹터 중심)이고 **`CM_GU`
    대표좌표로 떨어지지 않는다.** eNB 가 시군구 중심에서 수 km 떨어져 있을
    때 새 셀을 중심에 놓으면 그 eNB 가 `POINT` 가 아니라 `SCATTER` 로
    보인다 — **없는 광역 장애가 만들어진다.** 기준점이 없으면(= 좌표 없는
    eNB) 그 건은 **건너뛴다**(`coord_gaps` 가 이미 걸러 주지만 이중으로 막는다).
    """
    by_enb: dict[str, list[int]] = {}
    for enb, sector in gaps:
        by_enb.setdefault(str(enb), []).append(int(sector))

    rng = random.Random(seed)
    rows: list[dict] = []
    stats = {"targets": len(gaps), "enbs": 0, "skipped_no_anchor": 0,
             "skipped_no_dong": 0, "skipped_exists": 0, "patterns": {}}

    for enb in sorted(by_enb):
        have = {int(s) for (s,) in _rows(
            conn, "SELECT SECTOR FROM CM_CELL_REP_RU_LOC WHERE ENB_ID = %s", [enb])}
        sectors = sorted(s for s in set(by_enb[enb]) if s not in have)
        stats["skipped_exists"] += len(set(by_enb[enb])) - len(sectors)
        if not sectors:
            continue
        anchor = enb_anchor(conn, enb)
        if anchor is None:
            # 좌표 없는 eNB — **의도된 미매핑이다**(`coord_gaps` 머리말)
            stats["skipped_no_anchor"] += len(sectors)
            continue
        base = _rows(conn, "SELECT CITY_ID, GU_ID, DONG_ID FROM CM_CELL_REP_RU_LOC "
                           "WHERE ENB_ID = %s AND LATITUDE IS NOT NULL LIMIT 1", [enb])
        city, gu, dong = base[0] if base else (None, None, None)
        if dong is None:
            # 동을 못 정하면 넣지 않는다. 0 으로 채우면 `_filter_drawable` 이
            # 조용히 버리고 "그 지역에 트래픽이 없다"로만 보인다(불변규칙 17).
            stats["skipped_no_dong"] += len(sectors)
            continue
        stats["enbs"] += 1
        pts = scatter(anchor[0], anchor[1], len(sectors), spread_km, rng)
        pat, _ = pattern_of(pts)
        stats["patterns"][pat] = stats["patterns"].get(pat, 0) + 1
        for sector, (clat, clon) in zip(sectors, pts):
            rows.append({
                "ENB_ID": enb, "SECTOR": sector,
                "CITY_ID": city, "GU_ID": gu, "DONG_ID": dong,
                "LATITUDE": decimal_to_dms(clat),
                "LONGITUDE": decimal_to_dms(clon),
                "RU_NAME": f"RU-{enb}-{sector}",
                "RU_CUID": None, "FLAG": None, "DECODE": enb,
            })

    if not dry_run and rows:
        insert_rows(conn, "CM_CELL_REP_RU_LOC", rows)
    stats["inserted"] = len(rows)
    # 호출부(`load_cells`)가 메모리의 `uniq` 를 같은 값으로 갱신해야 한다 —
    # 안 하면 이 틱의 셀 목록은 여전히 미매핑이고, 다음 틱부터만 반영된다.
    stats["rows"] = {(r["ENB_ID"], r["SECTOR"]):
                     (r["CITY_ID"], r["GU_ID"], r["DONG_ID"],
                      r["LATITUDE"], r["LONGITUDE"]) for r in rows}
    return stats


def fill_coords(conn, xw_path: Path | None = None, enb_limit: int = 0,
                dry_run: bool = True) -> dict:
    """좌표 결손을 **세어 본다 — 적재 경로가 아니다.**

    **적재는 `load_cells(ensure_coords=True)` 가 한다**(2026-10-26 이동).
    수동 커맨드로 두면 사람이 기억해서 쳐야 하고, 현장에 반출하면 아무도
    치지 않는다. 그리고 **적재 경로가 둘이면 어느 쪽이 넣었는지 모른다.**
    그래서 `dry_run` 기본이 `True` 이고 `False` 를 받으면 거부한다.

    남겨 둔 이유는 **점검**이다 — "지금 몇 개가 비어 있고 그중 몇 개가
    의도된 미매핑인가"를 `topo list` 옆에서 보는 자리가 필요하다.

    ## 세는 기준이 생성기와 **같아야 한다**

    처음에는 `unmapped_active_cells` + `enb_anchor` 로 싸게 셌다. 실 DB 에서
    그 수가 **768/36** 이고 생성기 경로(`coord_gaps`)는 **696/108** 이었다 —
    두 eNB 가 위치 마스터에 좌표 행을 갖고 있는데 `_filter_drawable` 이
    그 행을 후보에서 떨어뜨려, `load_cells` 에서는 "좌표 없는 eNB"이고
    `enb_anchor` 에게는 "좌표 있는 eNB"였다. **점검이 생성기와 다른 수를
    말하면 점검이 아니다.**

    그래서 `load_cells` 를 그대로 불러(읽기뿐이다) 그 결과로 `coord_gaps` 를
    돈다. 판정 규칙이 **한 함수에만** 있게 하려고 `Cell` 목록을 `coord_gaps`
    가 받는 모양으로 되돌린다 — `pick_report` 가 `_pick_enbs` 에 하는 것과
    같은 방식이고, 규칙을 여기 옮겨 적으면 한쪽만 고쳐질 때 **점검이 틀린
    안심**을 준다.
    """
    if not dry_run:
        raise RuntimeError(
            "fill_coords 는 점검 전용이다 — 적재는 생성기가 한다.\n"
            "  python -m datagen run · pemfill 이 load_cells(ensure_coords=True) 로 "
            "매 틱 채운다 (`topo.ensure_sector_coords`)")

    from . import pemdb1 as P        # 지연 임포트 — 조회 경로에서만 쓴다

    drawable = P.load_drawable_dongs(xw_path) if xw_path else None
    cells = P.load_cells(conn, enb_limit=enb_limit, drawable=drawable)
    # `coord_gaps` 가 읽는 것은 인덱스 8·9(좌표)와 키뿐이다. 나머지를 채우면
    # 이 함수가 `load_cells` 의 행 모양에 묶인다.
    #
    # **`Cell.latitude` 를 그대로 쓰면 안 된다** — 좌표가 없을 때 `_dms` 가
    # `None` 이 아니라 **`0.0`** 을 돌려준다(그 함수 docstring: *"파싱 실패는
    # 0.0 — 좌표 없는 셀은 UNMAPPED 로 남는다"*). 그래서 결손 셀이 "적도
    # 앞바다에 좌표가 있는 셀"로 보이고 결손 수가 **0** 으로 세어졌다
    # (불변규칙 17 과 같은 결의 혼동). 진실은 `geo_status` 에 있다.
    uniq = {(c.enb_id, c.cell_id): (None,) * 5 + (
        None, None, None,
        *((c.latitude, c.longitude) if c.geo_status == "MAPPED" else (None, None)),
    ) for c in cells}
    gaps = coord_gaps(uniq, None)       # 선택은 `load_cells` 가 이미 했다
    missing = [k for k, r in uniq.items() if r[8] is None or r[9] is None]
    return {"cells": len(uniq), "targets": len(missing),
            "fillable": len(gaps), "fillable_enbs": len({e for e, _ in gaps}),
            "intended_unmapped": len(missing) - len(gaps),
            "intended_enbs": len({e for e, _ in missing} - {e for e, _ in gaps})}


# --------------------------------------------------------------------------
# 행정구역 생성 — `CM_CITY` · `CM_GU` · `CM_DONG` 을 **처음부터** 만든다
# --------------------------------------------------------------------------
#
# 지금까지 이 셋은 **상용 `pemdb1` 반출 JSON** 이었다(`docker/mysql/load_master.py`
# 가 적재한다). 받아온 덤프라 일본용을 받을 자리가 없고, `topo` 에 있는 것은
# 단건 CRUD 와 `dump`/`restore` 뿐이어서 **"없는 상태에서 만들기"가 0** 이었다.
# `seed_enb` 가 `CM_GU` 의 좌표와 `CM_DONG` 의 동 목록을 **전제로** 돈다.
#
# ## 입력은 경계 GeoJSON 이다 — 사람이 좌표를 찍지 않는다
#
# 이름은 `properties.name`, 좌표는 **폴리곤 centroid** 에서 나온다. 구글맵에서
# 사람이 찍어 넣는 경로를 만들면 그 수가 어디서 왔는지 다음 사람이 알 수 없고,
# 273개를 손으로 옮기는 동안 오타가 섞인다.
#
# ## 코드는 **우리가 매기지 않는다 — 받는다** (한국)
#
# `CM_CELL_REP_RU_LOC` 442,689행과 1분 집계가 이미 `CITY_ID`·`GU_ID`·`DONG_ID`
# 를 참조한다. 우리가 코드를 새로 매기면 그 전부가 끊어진다. 그래서 코드는
# **코드 맵**에서 받는다 — 번들 geojson 코드(구 통계청 · 부산 `21`) →
# pemdb1 코드(행정표준 · 부산 `26`). 맵이 없으면 **멈춘다**(추측으로 매기면
# 조용히 다른 세대가 된다).
#
# 일본은 같은 자리에 **일본 geojson 코드 → 한국 코드** 맵을 둔다. 그것이
# 사용자 방침("테이블 스위칭만으로 전환")의 구현이다 — 트래픽 테이블의 코드가
# 그대로 남아야 스위칭이 성립한다.
#
# ## crosswalk 를 **함께 낸다** — 이름 매칭이 아니라 생성의 산물이다
#
# `CM_DONG` 을 생성하면 상용 동 이름에 맞춰진 `code_crosswalk.json` 과 매칭이
# 끊기고, `drawable_dongs` 가 비어 `seed_enb` 의 `DONG_ID` 가 **에러 없이
# 전부 NULL** 이 된다. 그래서 `seed_geo` 가 **자기가 쓴 대응을 그대로** 그
# 파일로 쓴다. 우리가 양쪽을 알고 있으므로 추측이 없다 — graphio 의
# `geo_xw`(도 약칭·`수원시장안구` 분리·행정동/법정동)가 하던 이름 매칭이
# 필요 없어진다.

#: locale → 레벨 → 경계 파일 이름. **graphio `geo.py:19` 의 `SOURCES` 사본**
#: 이다 — 그쪽이 내려받고 이쪽이 읽는다. 파일 이름이 어긋나면 "경계 데이터
#: 없음" 으로 멈추므로 조용히 틀리지는 않는다.
GEO_SOURCES: dict[str, dict[str, str]] = {
    "kr": {
        # **한국은 `geo_dir` 바로 아래다.** graphio `geo.py`·`/api/geojson` 이
        # 그 자리를 읽으므로 하위 디렉터리로 옮기면 지도가 503 이 된다.
        "_dir": ".",
        "sido": "skorea-provinces-2018-geo.json",
        "sgg": "skorea-municipalities-2018-geo.json",
        "emd": "skorea-submunicipalities-2018-geo.json",
    },
    "jp": {
        "_dir": "jp",
        "sido": "prefectures.json",            # 47 도도부현
        "sgg": "N03-21_210101.json",           # 1,897 시구정촌 (정령시 区 분해)
        # **읍면동(町丁・字)은 받지 않는다.** e-Stat 에 있지만 수십만 건이고
        # 사용자 방침이 시군구급이다 — `seed_geo` 가 생성으로 채운다.
    },
}

#: locale 별 경계 파일 출처. **번들 디렉터리가 `.gitignore` 안이라 크레딧이
#: 파일과 함께 가야 한다** — `fetch_boundaries` 가 `SOURCE.txt` 로 쓴다.
#: 한국 번들은 graphio `geo.py:3` 머리말이 같은 일을 한다.
GEO_CREDIT: dict[str, dict[str, str]] = {
    "jp": {
        "base": "https://raw.githubusercontent.com/smartnews-smri/"
                "japan-topography/main/data/municipality/geojson/s0001",
        "credit": (
            "일본 행정구역 경계\n"
            "  재배포: smartnews-smri/japan-topography (s0001)\n"
            "  원출처: 국토교통성 국토수치정보 행정구역 데이터 "
            "(2021-09-28 취득) · CC BY 4.0\n"
            "  가공: 위 저장소가 국토수치정보를 가공한 것이다.\n"
            "\n"
            "  `s0001` 을 쓴 이유 — 1.87MB 로 한국 번들(57MB)의 1/30 이고,\n"
            "  화면이 geoMercator().fitSize() 로 자동 맞추므로 PoC 축척에서\n"
            "  더 상세한 `s0010`(26MB)과 차이가 보이지 않는다.\n"
            "  `*_designated_city.json` 은 쓰지 않는다 — 정령시를 통으로 묶어\n"
            "  입도가 거칠다(区 분해본이 한국 시군구와 같은 입도다)."
        ),
    },
}

#: locale 별 `properties` 키. **한쪽만 보면 빈 이름이 나온다** —
#: 일본 파일은 도쿄 23구가 `N03_003=None`(구 이름만), 일반 시가
#: `N03_004=None`(시 이름만)이라 **둘을 이어야** 이름이 선다.
#:
#: 도도부현 파일(`prefectures.json`)에는 **코드가 없다**(`N03_001` 하나뿐) —
#: 시구정촌의 `N03_007[:2]` 에서 역산한다(`read_boundaries` 참조).
def _props_kr(props: dict, level: str) -> tuple[str | None, str | None]:
    code = props.get("code") or props.get("CODE") or props.get("adm_cd")
    name = (props.get("name") or props.get("NAME")
            or props.get("name_local") or props.get("adm_nm"))
    return (str(code) if code is not None else None,
            str(name).strip() if name else None)


def _props_jp(props: dict, level: str) -> tuple[str | None, str | None]:
    if level == "sido":
        # 코드는 호출부가 시구정촌에서 역산해 넣는다
        name = props.get("N03_001")
        return None, (str(name).strip() if name else None)
    code = props.get("N03_007")
    name = f"{props.get('N03_003') or ''}{props.get('N03_004') or ''}".strip()
    return (str(code) if code else None), (name or None)


GEO_PROPS = {"kr": _props_kr, "jp": _props_jp}

#: graphio 가 굽는 crosswalk 파일 이름. **`geo_xw.XW_FILE` 사본이다.**
#:
#: **이름이 같아서 서로를 덮는 것이 실제 위험이었다.** 경계 파일은 locale 로
#: 갈라 뒀는데 crosswalk 출력이 루트 한 곳이라, `seed-geo --locale jp` 가
#: `data/geo/code_crosswalk.json` 을 일본 대응으로 덮었다. 그러면 graphio 가
#:
#:     한국 폴리곤(skorea-*) + 일본 crosswalk → remap 전량 unresolved
#:     → 지도 전체 회색
#:
#: 이 되고 **에러가 나지 않는다.** 게다가 `restore kr` 은 DB 네 테이블만
#: 되돌리고 `data/geo/` 를 보지 않아 **복구되지도 않는다.** 그래서 경로를
#: `xw_path()` 로 가른다 — 경계 파일과 **같은 자리**에서 나온다.
XW_FILE = "code_crosswalk.json"

#: 생성한 읍면동의 코드 규칙 — `GU_ID * 1000 + 순번`. 상용 실측과 같은 모양
#: 이다(`12000` 구의 동이 `12000035`). 자리수를 바꾸면 `int` 컬럼은 받지만
#: 화면·조인이 상용과 다른 폭을 보게 된다.
DONG_CODE_STEP = 1000


def _ring_centroid(ring: list) -> tuple[float, float, float]:
    """폴리곤 고리의 면적 가중 중심과 면적(도²). shoelace 공식이다.

    **bbox 중심을 쓰지 않는다** — 해안선이 한쪽으로 뻗은 시군구에서 중심이
    바다로 나가고, 그 좌표를 기준으로 셀을 뿌리면 `seed_enb` 가 만든 eNB 가
    물 위에 선다. 면적이 0 이면(한 점에 모인 고리) 첫 점을 준다.
    """
    a = cx = cy = 0.0
    n = len(ring)
    for i in range(n):
        x0, y0 = float(ring[i][0]), float(ring[i][1])
        x1, y1 = float(ring[(i + 1) % n][0]), float(ring[(i + 1) % n][1])
        f = x0 * y1 - x1 * y0
        a += f
        cx += (x0 + x1) * f
        cy += (y0 + y1) * f
    if abs(a) < 1e-12:
        return float(ring[0][0]), float(ring[0][1]), 0.0
    return cx / (3.0 * a), cy / (3.0 * a), abs(a) / 2.0


def largest_piece(geom: dict) -> tuple[float, float, float]:
    """`(lon, lat, area)` — 가장 넓은 조각의 중심과 그 면적(도²).

    면적을 함께 돌려주는 이유: **같은 코드가 두 폴리곤일 때 큰 쪽을 고르는
    판정**에 쓴다(`read_boundaries`). 중심만 받으면 뒤의 것이 앞을 덮는다.
    """
    polys = ([geom.get("coordinates") or []] if geom.get("type") == "Polygon"
             else (geom.get("coordinates") or []))
    best = None
    for poly in polys:
        if not poly or len(poly[0]) < 3:
            continue
        x, y, area = _ring_centroid(poly[0])
        if best is None or area > best[2]:
            best = (x, y, area)
    if best is None:
        raise ValueError("폴리곤에서 중심을 구할 수 없다")
    return best


def geom_centroid(geom: dict) -> tuple[float, float]:
    """`Polygon`/`MultiPolygon` 의 중심 `(lon, lat)`.

    **가장 넓은 조각의 중심**을 쓴다. 조각 전체를 면적 가중 평균하면 본토와
    먼 섬이 있는 시군구(옹진군 등)에서 중심이 둘 사이 바다로 간다.
    """
    lon, lat, _area = largest_piece(geom)
    return lon, lat


def geo_level_path(geo_dir: Path, locale: str, level: str) -> Path:
    """경계 파일 경로 — **정본이 이 함수 하나다.**

    한국은 `geo_dir` 바로 아래(graphio 가 그 자리를 읽는다), 일본은
    `geo_dir/jp/` 다. 호출부가 각자 조립하면 한쪽만 고쳐질 때 "경계 데이터
    없음" 으로 멈춘다.
    """
    src = GEO_SOURCES.get(locale)
    if not src:
        raise RuntimeError(
            f"locale '{locale}' 의 경계 파일 이름이 `GEO_SOURCES` 에 없다 — "
            "그 나라 번들을 추가해야 한다")
    if level not in src:
        raise RuntimeError(
            f"locale '{locale}' 에 {level} 레벨 경계가 없다 "
            f"(있는 것: {[k for k in src if not k.startswith('_')]})")
    sub = src.get("_dir", ".")
    return (geo_dir if sub == "." else geo_dir / sub) / src[level]


def crosswalk_path(geo_dir: Path, locale: str = "kr") -> Path:
    """그 locale 의 crosswalk 경로 — **경계 파일과 같은 규칙이다.**

    `kr` 은 `geo_dir` 바로 아래다(graphio `geo_xw.load` 가 그 자리를 읽으므로
    바꿀 수 없다). 다른 나라는 `geo_dir/<_dir>/` 다 — 그래야 한 나라를
    만들어도 다른 나라의 대응이 남는다.

    **`_dir` 기본값이 `geo_level_path` 와 같아야 한다**(`.`). 다르게 두면
    경계 파일과 crosswalk 가 **다른 디렉터리**에 떨어져 `drawable_dongs` 가
    빈 집합을 받고, 그러면 `seed_enb` 의 `DONG_ID` 가 조용히 전부 NULL 이
    된다. 이름을 `xw_path` 로 짓지 않은 이유는 `seed_enb`·`pick_report`·
    `fill_coords` 의 **인자 이름이 이미 `xw_path`** 라 가려지기 때문이다.
    """
    sub = GEO_SOURCES.get(locale, {}).get("_dir", ".")
    return (geo_dir if sub == "." else geo_dir / sub) / XW_FILE


def read_boundaries(geo_dir: Path, locale: str, level: str) -> list[dict]:
    """경계 GeoJSON → `[{code, name, lon, lat, area}]`. 파일이 없으면 멈춘다.

    ## 세 가지를 조용히 통과시키지 않는다 (일본 파일 실측 2026-10-26)

    1. **코드 없는 feature 3건**(경계 미정 지역) — **버린다.** 넣으면 코드
       없는 시군구가 생기고, 그 지역 셀은 영원히 조인되지 않는다.
    2. **같은 코드 두 폴리곤** — 큰 쪽만 쓴다(섬이 따로 떨어진 경우).
       받은 판본에서는 **0건**이지만(고유 1,894 + 무코드 3 = 1,897) 다음
       판본에서 생길 수 있고, 생기면 뒤의 것이 앞을 덮어 중심이 섬으로 간다.
    3. **이름이 비는 것** — 멈춘다. 이름 없는 시군구는 화면에서 코드로만
       보이고, 그것은 "데이터가 왔는데 이름이 없다"로 읽혀 원인을 엉뚱한
       데서 찾게 만든다.

    도도부현 파일에는 코드가 없어 **시구정촌에서 역산한다**(`N03_007[:2]`).
    """
    path = geo_level_path(geo_dir, locale, level)
    if not path.exists():
        raise RuntimeError(
            f"경계 데이터가 없다: {path}\n"
            + ("  받아라 — python -m datagen topo geo-fetch --locale "
               f"{locale}" if locale in GEO_CREDIT
               else "  graphio 쪽에서 먼저 받아라 — python -m graphio geo"))
    doc = json.loads(path.read_text(encoding="utf-8"))
    props_of = GEO_PROPS.get(locale, _props_kr)

    by_name: dict[str, str] = {}
    if locale == "jp" and level == "sido":
        # 도도부현 코드 역산 — 시구정촌의 `N03_001`(도도부현명)과
        # `N03_007[:2]` 를 짝지어 이름 → 코드 표를 만든다
        mpath = geo_level_path(geo_dir, locale, "sgg")
        if not mpath.exists():
            raise RuntimeError(
                f"도도부현 코드를 역산할 시구정촌 파일이 없다: {mpath}")
        for f in json.loads(mpath.read_text(encoding="utf-8")).get("features") or []:
            pr = f.get("properties") or {}
            nm, cd = pr.get("N03_001"), pr.get("N03_007")
            if nm and cd:
                by_name.setdefault(str(nm).strip(), str(cd)[:2])

    out: dict[str, dict] = {}
    dropped = 0
    for feat in doc.get("features") or []:
        props = feat.get("properties") or {}
        code, name = props_of(props, level)
        if code is None and by_name:
            code = by_name.get(name or "")
        if code is None:
            dropped += 1          # 경계 미정 — 버린다
            continue
        if not name:
            raise RuntimeError(
                f"{path.name} 의 feature 에 이름이 없다: {sorted(props)[:8]}"
                " — 번들 포맷이 다르다")
        lon, lat, area = largest_piece(feat.get("geometry") or {})
        prev = out.get(code)
        if prev is None or area > prev["area"]:
            out[code] = {"code": code, "name": name,
                         "lon": lon, "lat": lat, "area": area}
    if not out:
        raise RuntimeError(f"{path.name} 에서 읽은 것이 0건이다 — 포맷이 다르다")
    return [out[k] for k in sorted(out)]


def code_map_path(locale: str) -> Path:
    """locale 의 코드 맵. `dump` 사본과 **같은 디렉터리**에 둔다 — 그 둘이
    한 벌이다(사본을 되넣으면 그 코드 체계로 돌아간다)."""
    return LOCALE_DIR / locale / "code_map.json"


def load_code_map(locale: str, geo_dir: Path) -> dict:
    """geojson 코드 → DB 코드. `{"sido": {...}, "sgg": {...}}`.

    우선순위 — ① `locale/<locale>/code_map.json`(우리가 쓴 것)
    ② 한국만: `code_crosswalk.json` 의 `map`(graphio 가 이름 매칭으로 만든 것).

    **없으면 멈춘다.** 코드를 추측으로 매기면 `CM_CELL_REP_RU_LOC` 44만행과
    1분 집계가 참조하는 코드와 어긋나고, 그것은 에러가 아니라 "그 지역에
    트래픽이 없다"로 보인다.
    """
    p = code_map_path(locale)
    if p.exists():
        doc = json.loads(p.read_text(encoding="utf-8"))
        return {"sido": dict(doc.get("sido") or {}), "sgg": dict(doc.get("sgg") or {})}
    xw = crosswalk_path(geo_dir, locale)
    if xw.exists():
        m = (json.loads(xw.read_text(encoding="utf-8")).get("map") or {})
        # crosswalk 의 `sgg` 값은 `"CITY_ID-GU_ID"` 다 — 뒷자리만 쓴다.
        return {"sido": dict(m.get("sido") or {}),
                "sgg": {k: str(v).split("-")[-1] for k, v in (m.get("sgg") or {}).items()}}
    raise RuntimeError(
        f"코드 맵이 없다: {p}\n"
        f"  한국은 {xw} 가 대신 쓰인다 — python -m graphio geo-xw\n"
        "  일본은 '일본 geojson 코드 → 한국 코드' 맵을 그 자리에 둔다")


#: 생성한 crosswalk 의 `emd` 쪽이 어디서 왔는지.
#:
#: * `polygon` — 읍면동 경계 폴리곤 하나당 동 하나. 지도가 동까지 그려진다
#: * `generated` — 폴리곤 없이 시군구 중심에서 뿌렸다. **그 경우에도 맵을
#:   비우지 않고 항등으로 채운다** — 비우면 `drawable_dongs` 가 비고
#:   `seed_enb` 의 `DONG_ID` 가 에러 없이 전부 `NULL` 이 된다. "안 만들어도
#:   되는 상태"와 "없는 상태"는 다른 것이다(불변규칙 17 의 결).
EMD_FROM_POLYGON = "polygon"
EMD_GENERATED = "generated"


def seed_geo(conn, geo_dir: Path, locale: str = "kr",
             dongs_per_gu: int = 8, spread_km: float = 3.0,
             seed: int = 20260903, replace: bool = False,
             dry_run: bool = True) -> dict:
    """`CM_CITY`·`CM_GU`·`CM_DONG` 을 경계 GeoJSON 에서 만든다.

    `replace=False` 면 **행이 있으면 거부한다** — 44만 셀이 참조하는 코드를
    덮어쓰는 일이라 사람이 의도를 말해야 한다. `replace=True` 는 세 테이블을
    비우고 전량 다시 넣는다(멱등보다 **전량 교체**가 맞다 — 부분 갱신이면
    옛 코드가 남아 어느 세대인지 알 수 없다).

    컬럼 타입이 테이블마다 다르다(실측) —

        CM_CITY   LONGITUDE·LATITUDE  varchar(20)   '126.9917942707449'
        CM_GU     LONGITUDE·LATITUDE  float         126.787
        CM_DONG   LONGITUDE·LATITUDE  float         (+ LATITUDE_IMSI)

    DMS 가 아니다 — `CM_CELL_REP_RU_LOC` 만 DMS 문자열이다. 섞으면
    `gu_points()` 의 `float(la)` 가 깨지거나 좌표가 0 이 된다.
    """
    cmap = load_code_map(locale, geo_dir)
    sido = read_boundaries(geo_dir, locale, "sido")
    sgg = read_boundaries(geo_dir, locale, "sgg")

    city_rows: list[dict] = []
    city_name: dict[int, str] = {}
    skipped = {"sido": [], "sgg": []}
    for a in sido:
        cid = cmap["sido"].get(a["code"])
        if cid is None:
            # **코드 맵에 없는 폴리곤은 넣지 않는다.** 코드를 지어내면 그
            # 지역의 셀이 영원히 조인되지 않는다 — 그 수를 보고한다.
            skipped["sido"].append(a["code"])
            continue
        city_name[int(cid)] = a["name"]
        city_rows.append({
            "CITY_ID": int(cid), "CITY_NAME": a["name"],
            "MAP_X": None, "MAP_Y": None, "MAP_CODE": None,
            # varchar 다 — 문자열로 넣는다
            "LONGITUDE": f"{a['lon']:.6f}", "LATITUDE": f"{a['lat']:.6f}",
        })

    gu_rows: list[dict] = []
    gu_at: dict[int, tuple[int, float, float, str]] = {}
    for a in sgg:
        gid = cmap["sgg"].get(a["code"])
        if gid is None:
            skipped["sgg"].append(a["code"])
            continue
        gid = int(gid)
        # 시도는 시군구 코드 앞 2자리다 — 상용 실측(`41195` → `41`).
        cid = int(str(gid)[:2])
        gu_at[gid] = (cid, a["lat"], a["lon"], a["code"])
        gu_rows.append({
            "CITY_ID": cid, "GU_ID": gid,
            "CITY_NAME": city_name.get(cid), "GU_NAME": a["name"],
            "P_GU_ID": None, "MAP_X": None, "MAP_Y": None,
            "USE_YN": "Y", "MAP_CODE": None,
            "LONGITUDE": a["lon"], "LATITUDE": a["lat"],
        })

    # ── 읍면동 ─────────────────────────────────────────────────────────
    rng = random.Random(seed)
    dong_rows: list[dict] = []
    emd_map: dict[str, str] = {}
    emd_src = EMD_GENERATED
    # **`in` 으로 본다.** `.get("emd", "")` 로 경로를 만들면 `geo_dir / ""`
    # 가 `geo_dir` 자신이 되고 `.exists()` 가 참이라, 일본처럼 emd 가 없는
    # locale 이 폴리곤 분기로 들어가 "emd 레벨 경계가 없다"로 죽는다.
    has_emd = "emd" in GEO_SOURCES.get(locale, {})
    if has_emd and geo_level_path(geo_dir, locale, "emd").exists():
        # 폴리곤이 있으면 **그것 하나당 동 하나**다. 코드는 우리가 매기므로
        # 1:1 이고, graphio 쪽 "행정동 3,504 vs 법정동 8,079" 불일치가
        # 사라진다(그 때문에 지도 커버리지가 51.7% 였다).
        emd_src = EMD_FROM_POLYGON
        seq: dict[int, int] = {}
        for a in read_boundaries(geo_dir, locale, "emd"):
            gid = cmap["sgg"].get(a["code"][:5])
            if gid is None:
                continue
            gid = int(gid)
            if gid not in gu_at:
                continue
            seq[gid] = seq.get(gid, 0) + 1
            did = gid * DONG_CODE_STEP + seq[gid]
            cid = gu_at[gid][0]
            dong_rows.append({
                "CITY_ID": cid, "GU_ID": gid, "DONG_ID": did,
                "CITY_NAME": city_name.get(cid),
                "GU_NAME": next(r["GU_NAME"] for r in gu_rows if r["GU_ID"] == gid),
                "DONG_NAME": a["name"], "P_DONG_ID": did,
                "MAP_X": None, "MAP_Y": None, "USE_YN": "Y", "MAP_CODE": None,
                "LONGITUDE": a["lon"], "LATITUDE": a["lat"],
                "LATITUDE_IMSI": None,
            })
            emd_map[a["code"]] = f"{cid}-{gid}-{did}"
    else:
        for gid, (cid, lat, lon, _code) in sorted(gu_at.items()):
            pts = scatter(lat, lon, dongs_per_gu, spread_km, rng)
            gname = next(r["GU_NAME"] for r in gu_rows if r["GU_ID"] == gid)
            for i, (dlat, dlon) in enumerate(pts, start=1):
                did = gid * DONG_CODE_STEP + i
                dong_rows.append({
                    "CITY_ID": cid, "GU_ID": gid, "DONG_ID": did,
                    "CITY_NAME": city_name.get(cid), "GU_NAME": gname,
                    "DONG_NAME": f"{gname} {i}동", "P_DONG_ID": did,
                    "MAP_X": None, "MAP_Y": None, "USE_YN": "Y",
                    "MAP_CODE": None, "LONGITUDE": dlon, "LATITUDE": dlat,
                    "LATITUDE_IMSI": None,
                })
                # **항등으로 채운다** — 비우면 `drawable_dongs` 가 비고
                # `seed_enb` 의 `DONG_ID` 가 에러 없이 전부 NULL 이 된다.
                emd_map[str(did)] = f"{cid}-{gid}-{did}"

    xw_doc = {
        "source": f"topo seed-geo · locale={locale} · emd={emd_src}",
        "map": {
            "sido": {a["code"]: str(cmap["sido"][a["code"]])
                     for a in sido if a["code"] in cmap["sido"]},
            "sgg": {code: f"{gu_at[int(gid)][0]}-{int(gid)}"
                    for code, gid in cmap["sgg"].items() if int(gid) in gu_at},
            "emd": emd_map,
        },
        "matched": {"sido": len(city_rows), "sgg": len(gu_rows), "emd": len(emd_map)},
        "total": {"sido": len(sido), "sgg": len(sgg), "emd": len(dong_rows)},
        "unmatched": {"sido": skipped["sido"], "sgg": skipped["sgg"], "emd": []},
        "notes": [
            "`topo seed-geo` 가 **자기가 쓴 대응을 그대로** 적은 것이다 — "
            "이름 매칭이 아니라 생성의 산물이라 추측이 없다.",
            "`emd=generated` 면 폴리곤 없이 뿌린 동이고 맵은 **항등**이다. "
            "비우면 `drawable_dongs` 가 비어 셀의 DONG_ID 가 조용히 NULL 이 된다.",
        ],
    }

    stats = {"locale": locale, "city": len(city_rows), "gu": len(gu_rows),
             "dong": len(dong_rows), "emd_source": emd_src,
             "skipped_sido": len(skipped["sido"]), "skipped_sgg": len(skipped["sgg"]),
             "crosswalk": str(crosswalk_path(geo_dir, locale)),
             "no_coord": sum(1 for r in gu_rows if r["LATITUDE"] is None)}
    if dry_run:
        return stats

    have = {k: _rows(conn, f"SELECT COUNT(*) FROM `{TABLES[k][0]}`")[0][0]
            for k in ("city", "gu", "dong")}
    if any(have.values()) and not replace:
        raise RuntimeError(
            f"행이 이미 있다 — {have}. 덮어쓰려면 replace 를 켠다.\n"
            "  44만 셀이 참조하는 코드를 바꾸는 일이다 — 사본을 먼저 뜬다")
    for k in ("dong", "gu", "city"):      # 자식부터 비운다
        wipe(conn, TABLES[k][0])
    insert_rows(conn, "CM_CITY", city_rows)
    insert_rows(conn, "CM_GU", gu_rows)
    insert_rows(conn, "CM_DONG", dong_rows)
    out_xw = crosswalk_path(geo_dir, locale)
    out_xw.parent.mkdir(parents=True, exist_ok=True)
    out_xw.write_text(json.dumps(xw_doc, ensure_ascii=False, indent=1),
                      encoding="utf-8")
    return stats


# --------------------------------------------------------------------------
# 일본 locale — **코드는 한국 것을 그대로 쓴다**
# --------------------------------------------------------------------------
#
# 사용자 방침은 *"동일한 ID 체계로 가고 테이블 스위칭으로 전환"* 이다. 그래서
# JIS 코드를 **DB 에 넣지 않는다** — 선정에만 쓰고, `CM_CITY`·`CM_GU` 에는
# 한국 코드가 들어간다. 트래픽 테이블(`CM_CELL_REP_RU_LOC` 44만행 · 1분 집계)
# 이 그 코드를 참조하므로 **그대로 남아야 스위칭이 성립한다.**
#
#     JP 폴리곤 01101(札幌市中央区)  →  KR 코드 11110(종로구) 자리에 들어간다
#     트래픽은 11110 을 그대로 가리키고, 이름·좌표만 일본 것이 된다

#: KR 시도 코드 → JP 도도부현 이름. **17줄 상수이고 사람이 정했다.**
#:
#: **인구 순위 자동 대응을 넣지 않는다.** 그러면 사용자가 지정한 둘이
#: 모두 깨진다 — KR 3위 부산 → JP 3위 大阪(지정은 福岡), KR 13위 대전 →
#: JP 13위 京都(지정은 大阪). 사용자 예시는 인구 순위가 아니라 **지리적
#: 성격**으로 짝지은 것이다.
#:
#: 고치려면 이 표만 고친다. 다음 사람이 "인구 순위로 바꾸자"고 할 때
#: **그것이 결정이었음**을 알 수 있게 지정 둘에 표시를 남긴다.
#: **키는 `CM_CITY.CITY_ID` 실측값이다 — 행정표준코드가 아니다.**
#: 세종은 `36` 이 아니라 **`12`**, 제주는 `50` 이 아니라 **`49`** 다(실측
#: 2026-10-26). 상용 반출본이 그렇게 왔고 트래픽이 그 코드를 참조하므로,
#: 표준코드로 "고치면" 그 두 시도가 조용히 빠진다.
SIDO_MAP_JP: dict[str, str] = {
    "11": "東京都",      # 서울 25 — 수도
    "12": "滋賀県",      # 세종  1 — 시군구가 하나뿐이다
    "26": "福岡県",      # 부산 16 ← **사용자 지정** (2026-10-26)
    "27": "愛知県",      # 대구  9
    "28": "神奈川県",    # 인천 10 — 수도권 항만
    "29": "広島県",      # 광주  5
    "30": "大阪府",      # 대전  5 ← **사용자 지정** (2026-10-26)
    "31": "兵庫県",      # 울산  5 — 공업항만
    "41": "北海道",      # 경기 52 — 그 수를 수용할 수 있는 유일한 급
    "42": "長野県",      # 강원 18 — 산악
    "43": "栃木県",      # 충북 16
    "44": "静岡県",      # 충남 18
    "45": "新潟県",      # 전북 16
    "46": "鹿児島県",    # 전남 22 — 도서
    "47": "岩手県",      # 경북 24
    "48": "岡山県",      # 경남 27
    "49": "沖縄県",      # 제주  4 — 섬↔섬
}


def _jp_rank(name: str, props_had_ward: bool) -> int:
    """시구정촌의 **도시 위계** — 작을수록 도심.

    **이 파일에 인구가 없다.** 인구 상위로 고를 수 없고 별도 수급도
    불필요하다 — 접미사가 위계 신호다.

        0  정령시의 区 (`N03_004` 가 있다) = 도심. 한국 273도 구 위주다
        1  市
        2  町 · 村 · 그 외
    """
    if props_had_ward:
        return 0
    return 1 if name.endswith("市") else 2


def build_jp_code_map(geo_dir: Path, kr_gu: list[tuple[int, int]]) -> dict:
    """JP 경계 코드 → KR 코드 맵. `locale/jp/code_map.json` 의 내용이다.

    `kr_gu` 는 **지금 DB 에 있는** `(CITY_ID, GU_ID)` 목록이다 — 번들에서
    유도하지 않는다. 트래픽이 참조하는 것이 DB 의 코드이고, 번들(250개)과
    DB(273개)가 다르기 때문이다. 다르면 스위칭 후 그 차이만큼 조용히
    미매핑이 된다.

    시도 대응은 `SIDO_MAP_JP` 가 정하고, 그 안에서 `_jp_rank` 순으로 필요
    수만큼 채운다. **공급이 모자라면 멈춘다** — 부족분을 다른 도도부현에서
    끌어오면 한 시도 코드에 떨어진 두 지역이 들어가고, 지리 롤업이 원시
    재집계라 `geo_pattern` 반경이 거짓으로 커진다(`WIDE` 가 상시로 난다).
    """
    sgg_path = geo_level_path(geo_dir, "jp", "sgg")
    doc = json.loads(sgg_path.read_text(encoding="utf-8"))
    by_pref: dict[str, list[tuple[int, str, str]]] = {}
    for feat in doc.get("features") or []:
        pr = feat.get("properties") or {}
        code, pref = pr.get("N03_007"), pr.get("N03_001")
        if not code or not pref:
            continue          # 경계 미정 — `read_boundaries` 와 같은 판정
        name = f"{pr.get('N03_003') or ''}{pr.get('N03_004') or ''}".strip()
        if not name:
            continue
        by_pref.setdefault(str(pref).strip(), []).append(
            (_jp_rank(name, bool(pr.get("N03_004"))), str(code), name))

    need: dict[int, list[int]] = {}
    for city, gu in sorted(set(kr_gu)):
        need.setdefault(int(city), []).append(int(gu))

    sido: dict[str, str] = {}
    sgg: dict[str, str] = {}
    picked: dict[int, list[dict]] = {}
    for city, gus in sorted(need.items()):
        pref = SIDO_MAP_JP.get(f"{city}")
        if pref is None:
            raise RuntimeError(
                f"KR 시도 {city} 의 일본 대응이 `SIDO_MAP_JP` 에 없다 — "
                "17줄 표를 고친다")
        pool = sorted(by_pref.get(pref) or [])
        if len(pool) < len(gus):
            raise RuntimeError(
                f"{pref} 의 시구정촌 {len(pool)}개로 KR 시도 {city} 의 "
                f"{len(gus)}개를 채울 수 없다 — `SIDO_MAP_JP` 에서 더 큰 "
                "도도부현으로 바꾼다 (부족분을 다른 현에서 끌어오면 한 시도에 "
                "떨어진 두 지역이 섞여 geo_pattern 반경이 거짓으로 커진다)")
        sido[pool[0][1][:2]] = str(city)
        rows = []
        for gu, (_rank, code, name) in zip(gus, pool):
            sgg[code] = str(gu)
            rows.append({"jis": code, "name": name, "gu_id": gu})
        picked[city] = rows

    return {
        "source": "topo code-map --locale jp",
        "note": [
            "JP geojson 코드 → KR 코드. **JIS 는 선정에만 쓰고 DB 에 넣지 "
            "않는다** — 트래픽이 KR 코드를 참조하므로 그대로 남아야 "
            "테이블 스위칭이 성립한다.",
            "시도 대응은 `SIDO_MAP_JP`(사람이 정한 17줄)이고 사용자 지정 "
            "둘(부산→福岡県 · 대전→大阪府)이 들어 있다. 인구 순위 자동 "
            "대응은 그 둘을 깬다.",
            "쓰지 않는 도도부현 30개는 **묶지 않는다** — 회색 배경으로 "
            "그릴 자리다(불변규칙 12 의 결).",
        ],
        "sido": sido,
        "sgg": sgg,
        "picked": picked,
        "unused_prefectures": sorted(set(by_pref) - set(SIDO_MAP_JP.values())),
    }


def save_code_map(doc: dict, locale: str) -> Path:
    path = code_map_path(locale)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=1),
                    encoding="utf-8")
    return path


def fetch_boundaries(geo_dir: Path, locale: str, force: bool = False) -> dict:
    """경계 파일을 받아 `geo_dir/<locale>/` 에 둔다. 크레딧을 함께 쓴다.

    **번들 디렉터리가 `.gitignore` 안이라 크레딧이 파일과 함께 가야 한다** —
    저장소에 적어 두면 반출본에서 사라진다. 한국 번들은 graphio
    `geo.py:3` 머리말이 같은 일을 하고 그쪽은 코드와 함께 간다.
    """
    import urllib.request

    cr = GEO_CREDIT.get(locale)
    if not cr:
        raise RuntimeError(
            f"locale '{locale}' 의 받을 자리가 `GEO_CREDIT` 에 없다 — "
            "한국 번들은 graphio 가 받는다 (python -m graphio geo)")
    out: dict[str, int] = {}
    for level in [k for k in GEO_SOURCES[locale] if not k.startswith("_")]:
        dest = geo_level_path(geo_dir, locale, level)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if force or not dest.exists() or dest.stat().st_size == 0:
            with urllib.request.urlopen(f"{cr['base']}/{dest.name}") as r:
                dest.write_bytes(r.read())
        out[dest.name] = dest.stat().st_size
    (dest.parent / "SOURCE.txt").write_text(
        cr["credit"] + f"\n\n  받은 자리: {cr['base']}\n", encoding="utf-8")
    return out


# --------------------------------------------------------------------------
# 셀 좌표 재배치 — **키를 유지하고 좌표만 다시 쓴다**
# --------------------------------------------------------------------------
#
# 이것이 "테이블 스위칭"의 마지막 조각이다. 코드 맵이 KR 코드를 그대로
# 쓰므로 `seed-geo --locale jp` 뒤에도 셀의 `(ENB_ID, SECTOR)`·`GU_ID` 는
# **그대로 유효하다** — 바뀌어야 하는 것은 **좌표뿐**이다. 그 44만행에는
# 한국 좌표가 들어 있어서, 전환하면 **시군구는 오사카인데 셀은 대전
# 앞산에 찍힌다.** 지도가 그 상태로 그려지고 에러는 나지 않는다.


def relocate_cells(conn, locale: str = "kr", spread_km: float = 0.35,
                   seed: int = 20260903, only_active: bool = True,
                   dry_run: bool = True) -> dict:
    """셀 좌표를 그 `GU_ID` 의 **현 `CM_GU` 중심** 주변으로 다시 쓴다.

    ## 키 집합을 바꾸지 않는다

    `UPDATE` 만 한다 — 행을 넣거나 지우지 않는다. `CL_CELL_MAP`·1분 집계·
    원시 Parquet·`ground_truth.json` 이 `(ENB_ID, SECTOR)` 를 참조하고,
    그것이 `ensure_sector_coords` 가 이미 지키는 불변이다. 그래서 이
    함수는 `fill_coords` 계열과 **역할이 갈린다** —

        ensure_sector_coords   행이 없는 셀에 **INSERT**  (결손 채우기)
        relocate_cells         있는 행의 좌표만 **UPDATE** (나라 전환)

    ## eNB 단위로 묶는다

    한 eNB 의 섹터들이 흩어지면 그 eNB 가 `POINT` 가 아니라 `SCATTER` 로
    보이고 **없는 광역 장애가 만들어진다.** eNB 마다 기준점을 하나 뽑아
    그 주변 `spread_km` 안에 섹터를 둔다 — `fill_coords` 가 하던 모양 그대로다.

    기본 `0.35km` 는 graphio 임계에서 나온 값이다 —

        detector.py:1512   if radius < 0.9:   return "POINT"
        detector.py:1533   if radius <= 3.0:  return "CLUSTER"

    올리면 같은 eNB 가 `CLUSTER`·`SCATTER` 로 보여 `geo_hit` 채점이 흔들린다.

    `only_active=True` 면 **1분 집계에 나타나는 셀만** 고친다(실측 2,161).
    44만행 전부를 고치는 것은 전환에 필요하지 않고, 되돌릴 범위만 넓힌다.
    """
    gus = {g: (c, la, lo) for c, g, _n, la, lo in gu_points(conn)}
    if not gus:
        raise RuntimeError(
            f"CM_GU 에 좌표가 없다 — 먼저 지리 계층을 만든다 "
            f"(topo seed-geo --locale {locale})")

    sql = """SELECT l.ENB_ID, l.SECTOR, l.GU_ID
               FROM CM_CELL_REP_RU_LOC l"""
    if only_active:
        # **활성 셀로 좁힌다.** 1분 집계에 나타나는 키만 지도·탐지에
        # 올라간다 — 나머지를 고쳐도 보이는 것이 달라지지 않는다.
        sql += """
               JOIN (SELECT DISTINCT ENB_ID, CELLNUM
                       FROM PM_COR_LTE_CALL_1M) a
                 ON a.ENB_ID = l.ENB_ID AND a.CELLNUM = l.SECTOR"""
    rows = _rows(conn, sql + " ORDER BY l.ENB_ID, l.SECTOR")

    by_enb: dict[str, list[tuple[int, int | None]]] = {}
    for enb, sector, gu in rows:
        by_enb.setdefault(str(enb), []).append((int(sector), gu))

    rng = random.Random(seed)
    stats = {"locale": locale, "cells": len(rows), "enbs": len(by_enb),
             "updated": 0, "skipped_no_gu": 0, "patterns": {}}
    updates: list[tuple[str, str, str, int]] = []

    for enb in sorted(by_enb):
        sectors = sorted(by_enb[enb])
        # 기준 시군구는 **그 eNB 의 섹터 다수결**이다. 섹터가 두 시군구에
        # 걸쳐 있을 때(경계 eNB) 섹터마다 다른 중심을 쓰면 그 eNB 가
        # 흩어진다 — 하나로 묶는 것이 `POINT` 를 지키는 조건이다.
        votes: dict[int, int] = {}
        for _s, gu in sectors:
            if gu is not None and int(gu) in gus:
                votes[int(gu)] = votes.get(int(gu), 0) + 1
        if not votes:
            # `GU_ID` 를 모르거나 그 시군구에 좌표가 없다 — **건드리지
            # 않는다.** 아무 데나 놓으면 미매핑이 "매핑된 거짓 좌표"로
            # 바뀌어 `H-07` 이 그 셀을 놓친다(불변규칙 12).
            stats["skipped_no_gu"] += len(sectors)
            continue
        gu = max(sorted(votes), key=lambda g: votes[g])
        _city, lat, lon = gus[gu]
        pts = scatter(lat, lon, len(sectors), spread_km, rng)
        pat, _r = pattern_of(pts)
        stats["patterns"][pat] = stats["patterns"].get(pat, 0) + 1
        for (sector, _gu), (clat, clon) in zip(sectors, pts):
            updates.append((decimal_to_dms(clat), decimal_to_dms(clon),
                            enb, sector))

    stats["updated"] = len(updates)
    if dry_run:
        return stats
    with conn.cursor() as cur:
        for i in range(0, len(updates), 5000):
            cur.executemany(
                "UPDATE CM_CELL_REP_RU_LOC SET LATITUDE = %s, LONGITUDE = %s "
                "WHERE ENB_ID = %s AND SECTOR = %s", updates[i:i + 5000])
    conn.commit()
    return stats
