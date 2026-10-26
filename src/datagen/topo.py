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

    out = {"ok": ok, "dry_run": dry_run, "locale": locale, "tables": tables}
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
