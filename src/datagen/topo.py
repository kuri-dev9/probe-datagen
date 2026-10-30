"""마스터 토폴로지 관리 — 행정구역 · 장비 · 셀 (`python -m datagen topo`).

`CM_CITY` · `CM_GU` · `CM_DONG` · `CM_CELL_REP_RU_LOC` · `CL_CELL_MAP` 을
사람이 의도적으로 고치거나 생성기가 채우는 자리다. UI(`ui.py`)가 같은 함수를
부른다 — 화면과 커맨드가 각자 SQL 을 쓰면 한쪽만 고쳐질 때 조용히 갈라진다.

## 로케일 축은 **테이블 이름**이다 (2026-10-26)

나라를 바꾸는 축이 DB 이름이 아니라 테이블 접미사다 — `CM_GU` / `CM_GU_JP`
가 **같은 `pemdb6` 안에** 공존한다. 정본은 `table_name(key, locale)` 하나이고
그 함수를 거치지 않는 테이블 이름이 이 파일에 남아 있으면 안 된다
(`TABLES` 정의 한 곳이 예외다).

**로케일 축이 있는 것은 다섯이다**(`LOCALE_TABLE_KEYS`) — 지역 셋 ·
셀 벌 · `CL_ENB_ORG`(eNB 이름). `CL_CELL_MAP` · `PM_*` · `AI_*` · 권역
테이블(`CM_BRANCH`·`CM_LAREA`·`CM_REGION`)은 **한 벌을 공유한다** — 사용자
방침이 *"나머진 다 그대로 쓴다. 원본 데이터도 그냥 쓴다"* 이기 때문이고,
공유 테이블을 일본 이름으로 덮으면 **KR 화면이 함께 일본어가 된다.** 그
자리에는 가드가 있다(`org_names(allow_shared=…)`).

**축을 늘리는 판정 기준은 "되돌릴 수 있는가" 가 아니라 "화면에 그 나라
이름으로 뜨는가" 다.** `CL_ENB_ORG` 는 `DUMP_TABLES` 밖이어서 한 벌을
덮으면 KR 이 돌아오지 않는데, 지도 라벨이 `RU_NAME` → `BTS_NAME` 순으로
떨어져 셀 27.6% 가 한국 이름으로 보였다 — 그래서 **덮지 않고 벌을 늘렸다**
(2026-10-26 사용자 결정).

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
#:
#: `CL_ENB_ORG` 에도 **PK 가 없고 원본에 `ENB_ID` 중복이 1건 있다**(실측
#: 45,955행 / distinct 45,954 · 2026-10-26). 키를 하나로 적는 것은 `rm`·
#: `update` 의 `WHERE` 를 위한 것이고, **유일성을 뜻하지 않는다** — 그래서
#: 복사 경로가 중복을 자기 책임으로 막는다(`copy_locale_rows`).
TABLES: dict[str, tuple[str, tuple[str, ...]]] = {
    "city": ("CM_CITY", ("CITY_ID",)),
    "gu": ("CM_GU", ("CITY_ID", "GU_ID")),
    "dong": ("CM_DONG", ("CITY_ID", "GU_ID", "DONG_ID")),
    "cell": ("CM_CELL_REP_RU_LOC", ("ENB_ID", "SECTOR")),
    "enb_org": ("CL_ENB_ORG", ("ENB_ID",)),
    "cellmap": ("CL_CELL_MAP", ("ENB_ID", "CELLNUM")),
}

#: `dump` 가 떠내는 집합. `cellmap` 은 167MB 라 제외한다 — 전환 대상이
#: 아니고(좌표가 없다) 떠내면 복귀 사본이 쓸 수 없는 크기가 된다.
#:
#: **`enb_org` 도 없다.** `CL_ENB_ORG`(45,955행)는 조직 마스터이고 이 저장소가
#: 만드는 것이 아니다 — 전량 교체할 이유가 없다. 다만 그래서 **KR 쪽
#: `BTS_NAME` 을 덮으면 되돌릴 사본이 없다**: `relocate_cells` 가 비(非)kr
#: 에서 그쪽을 건드리지 않는 근거가 이 한 줄이다.
DUMP_TABLES = ("city", "gu", "dong", "cell")

#: `rm --all` 이 받는 테이블. **`cellmap` 이 없다** — `topo` 는 위치 마스터를
#: 다루고 `CL_CELL_MAP` 574,404행은 **셀 대장**이다. 전량 삭제할 이유가 없고,
#: `DUMP_TABLES` 에도 없어서 지우면 되돌릴 원본이 아예 없다. 게이트가 그
#: 제외를 모르던 것이 사고의 모양이었다(`dump_exists` 는 `CM_*` 네 파일만
#: 봤다). 단건(`add`·`rm --key`)은 `TABLES` 로 계속 받는다.
WIPEABLE = DUMP_TABLES


def wipeable(key: str, locale: str = "kr") -> bool:
    """`rm --all` 을 받아도 되는가. **되돌릴 수단이 있는지로 판정한다.**

    둘 중 하나면 된다 —

    * `WIPEABLE` — KR 원본이고 `dump`/`restore` 사본이 있다
    * **비(非)kr 로케일 벌** — 다섯 단계로 다시 만들 수 있다
      (`ensure-tables` → `copy-cells` → `copy-enb-org` → `seed-geo` →
      `relocate-cells`). JP 벌을 비울 수 없으면 **다시 만들 길이 없다** —
      복사 경로가 "행이 있으면 넣지 않는다"로 서 있기 때문이다.

    `cellmap` 은 **어느 쪽도 아니다.** 로케일 축이 없어 비울 때 지워지는
    것은 언제나 KR 셀 대장이고, 사본이 없다.
    """
    if key == "cellmap":
        return False
    return key in WIPEABLE or (locale != "kr" and key in LOCALE_TABLE_KEYS)

#: 사본 확장자. **지금 정하는 이유:** 복원 함수를 넣는 시점이 포맷을 정할
#: 유일한 자리다 — 나중에 바꾸면 이미 뜬 사본이 **복원 불가**가 된다
#: (`restore` 가 다른 이름을 찾는다). 비압축 159MB 가 `fill-coords` 전마다
#: 뜨라고 하기엔 무겁고, 느리면 사람이 게이트를 우회할 길을 찾는다.
DUMP_SUFFIX = ".json.gz"

#: **로케일 축이 있는 테이블.** 이 다섯만 나라별 벌을 갖는다 —
#: `CM_CITY_JP` · `CM_GU_JP` · `CM_DONG_JP` · `CM_CELL_REP_RU_LOC_JP` ·
#: `CL_ENB_ORG_JP`.
#:
#: **`enb_org` 는 2026-10-26 에 늘렸다** (사용자 결정). 없으면 JP 지도에서
#: 셀 이름은 일본어인데 eNB 이름은 한국어로 뜬다 — 화면 라벨이
#: `RU_NAME` → `BTS_NAME` → 원시 키 순으로 떨어지므로 실측 비율로 27.6% 가
#: 한국 이름이 된다. 공유 한 벌을 덮어 해결할 수는 없다: `DUMP_TABLES` 에
#: 없어서 **KR 이 되돌아오지 않는다.**
#:
#: `cellmap`(`CL_CELL_MAP` 574,404행)이 **빠진 것이 의도다.** 사용자 방침
#: (2026-10-26): *"지역구에 대응하는 테이블만 JP로 만들라는거야. 나머진 다
#: 그대로 쓸꺼야. 원본 데이터도 그냥 쓸꺼고."* 셀 대장은 `(ENB_ID, CELLNUM)`
#: 키 자체이고 나라와 무관하다 — 벌을 뜨면 **키 집합이 둘**이 되고, 1분
#: 집계·원시 Parquet·`ground_truth.json` 이 참조하는 그 키가 어느 벌의
#: 것인지 알 수 없게 된다.
LOCALE_TABLE_KEYS = ("city", "gu", "dong", "cell", "enb_org")

#: 좌표 컬럼(`LATITUDE`·`LONGITUDE`)이 **있는** 테이블. `survey` 의 결손
#: 집계가 이것으로 좁힌다 — `enb_org` 에는 그 컬럼이 없어서 `LOCALE_TABLE_KEYS`
#: 를 그대로 쓰면 `1054 Unknown column` 으로 조회가 통째로 깨진다.
COORD_TABLE_KEYS = ("city", "gu", "dong", "cell")


def table_name(key: str, locale: str = "kr") -> str:
    """그 로케일의 실제 테이블 이름 — **로케일 축의 정본이 이 함수 하나다.**

    ## 왜 별도 스키마가 아니라 접미사인가 (2026-10-26 방식 변경)

    전에는 `pemdb6_jp` 스키마를 통째로 복제하는 설계였다(`clone_schema` ·
    `locale_database`). 사용자가 범위를 줄였다 — **지역 테이블만 JP 벌을
    만들고 나머지는 원본을 그대로 쓴다.** 그래서 로케일 축이 **DB 이름이
    아니라 테이블 이름**으로 내려온다.

        kr   CM_GU        CM_CELL_REP_RU_LOC
        jp   CM_GU_JP     CM_CELL_REP_RU_LOC_JP        ← 같은 `pemdb6` 안

    `CL_CELL_MAP` · `PM_*` · `AI_*` · 조직 테이블은 **한 벌뿐이다.**

    ## `cellmap` 을 비(非)kr 로 부르면 **멈춘다**

    조용히 `CL_CELL_MAP_JP` 를 만들면 "원본 공유" 전제가 깨진다. 그 테이블은
    셀 키의 정본이고 트래픽·채점이 그 키를 참조하므로, 벌이 둘이면 어느
    쪽 키로 생성했는지 알 수 없다 — 에러가 아니라 **값이 어긋나는** 종류다.
    """
    if key not in TABLES:
        raise RuntimeError(
            f"모르는 테이블 키: {key} (있는 것: {', '.join(TABLES)})")
    base = TABLES[key][0]
    if locale == "kr":
        return base
    if key not in LOCALE_TABLE_KEYS:
        raise RuntimeError(
            f"`{key}`({base}) 에는 로케일 축이 없다 — locale '{locale}' 로 "
            "부를 수 없다.\n"
            "  지역 테이블만 나라별 벌을 갖는다 "
            f"({' · '.join(TABLES[k][0] for k in LOCALE_TABLE_KEYS)}).\n"
            f"  {base} 는 셀 키의 정본이고 트래픽·채점이 그 키를 참조한다 — "
            "벌이 둘이면 어느 쪽으로 생성했는지 알 수 없다")
    return f"{base}_{locale.upper()}"


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


def table_exists(conn, table: str) -> bool:
    """그 테이블이 **현 스키마에** 있는가.

    JP 벌은 `ensure_locale_tables` 가 만들기 전에는 **없다.** 없는 것을
    `SELECT COUNT(*)` 로 세면 `1146 Table doesn't exist` 가 나고, 그 예외는
    화면에서 "조회 실패" 로 보여 **무엇을 먼저 해야 하는지**를 말하지 못한다.
    """
    return bool(_rows(conn,
                      "SELECT 1 FROM information_schema.tables "
                      "WHERE table_schema = DATABASE() AND table_name = %s",
                      [table]))


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


def survey(conn, locale: str = "kr") -> dict[str, dict]:
    """테이블별 행수와 **좌표 결손 수**. 0 과 NULL 을 가른다.

    `locale` 은 **어느 벌을 세는가**다(`table_name`). `cellmap` 은 로케일
    축이 없어 어느 벌에서도 같은 테이블을 센다 — 그 사실을 `shared` 로
    적는다. 안 적으면 "JP 에도 셀 대장이 55만행 있다"로 읽힌다.

    좌표 없는 행을 "0개"로 세면 미매핑이 보이지 않는다 (graphio 불변규칙
    12·17). 활성 셀 쪽은 1분 집계에 실제로 나타나는 셀만 센다 — 마스터
    44만 행 중 트래픽이 붙는 것은 일부이고, 지도·탐지에 나타나는 것은
    그쪽뿐이다.
    """
    out: dict[str, dict] = {}
    for key in TABLES:
        shared = key not in LOCALE_TABLE_KEYS
        table = TABLES[key][0] if shared else table_name(key, locale)
        if not table_exists(conn, table):
            # **"없다"를 0 행으로 적지 않는다**(불변규칙 17). JP 벌을 만들기
            # 전에는 테이블 자체가 없고, 그것과 "만들었는데 비었다"는 다른
            # 상태다 — 화면이 둘을 섞으면 `ensure-tables` 를 또 누른다.
            out[key] = {"table": table, "rows": None, "no_coord": None,
                        "exists": False, "shared": shared}
            continue
        total = _rows(conn, f"SELECT COUNT(*) FROM `{table}`")[0][0]
        info = {"table": table, "rows": total, "exists": True, "shared": shared}
        if key in COORD_TABLE_KEYS:
            info["no_coord"] = _rows(
                conn,
                f"SELECT COUNT(*) FROM `{table}` "
                "WHERE LATITUDE IS NULL OR LONGITUDE IS NULL "
                "OR LATITUDE = '' OR LONGITUDE = ''")[0][0]
        out[key] = info

    # 1분 집계(`PM_*`)는 **한 벌뿐이다** — 로케일 축이 없다. 조인 대상만
    # 그 로케일의 셀 벌로 바꾼다.
    cell = table_name("cell", locale)
    active = _rows(conn, "SELECT COUNT(*) FROM (SELECT DISTINCT ENB_ID, CELLNUM "
                         "FROM PM_COR_LTE_CALL_1M) t")[0][0]
    missing = _rows(conn, f"""
        SELECT COUNT(*) FROM (
          SELECT DISTINCT a.ENB_ID, a.CELLNUM
            FROM PM_COR_LTE_CALL_1M a
            LEFT JOIN `{cell}` l
                   ON l.ENB_ID = a.ENB_ID AND l.SECTOR = a.CELLNUM
           WHERE l.ENB_ID IS NULL) t""")[0][0] if table_exists(conn, cell) else None
    out["_active"] = {"cells": active, "unmapped": missing,
                      "locale": locale, "cell_table": cell,
                      "enbs": _rows(conn, "SELECT COUNT(DISTINCT ENB_ID) "
                                          "FROM PM_COR_LTE_CALL_1M")[0][0]}
    return out


def unmapped_active_cells(conn, locale: str = "kr") -> list[tuple[str, int]]:
    """1분 집계에 나타나는데 `CM_CELL_REP_RU_LOC` 에 **행이 없는** 셀.

    **실측(2026-10-26): 804건 전부가 "행 자체가 없음"이고, "행은 있고 좌표가
    NULL" 은 0건이다.** 그래서 `fill-coords` 는 UPDATE 가 아니라 INSERT 다 —
    이 구분을 잘못 잡으면 0행 UPDATE 가 "성공"으로 돌아와 아무것도 안 고친
    채 끝난다.
    """
    return [(str(e), int(c)) for e, c in _rows(conn, f"""
        SELECT DISTINCT a.ENB_ID, a.CELLNUM
          FROM PM_COR_LTE_CALL_1M a
          LEFT JOIN `{table_name("cell", locale)}` l
                 ON l.ENB_ID = a.ENB_ID AND l.SECTOR = a.CELLNUM
         WHERE l.ENB_ID IS NULL
         ORDER BY a.ENB_ID, a.CELLNUM""")]


def enb_anchor(conn, enb: str, locale: str = "kr") -> tuple[float, float] | None:
    """그 eNB 의 **이미 매핑된 섹터들의 중심**. 없으면 `None`.

    `CM_GU` 대표좌표보다 이쪽을 먼저 쓴다. eNB 가 시군구 중심에서 수 km
    떨어져 있으면, 같은 eNB 의 새 셀을 시군구 중심에 놓는 순간 그 eNB 가
    `POINT` 가 아니라 `SCATTER` 로 보인다 — **없는 광역 장애가 만들어진다.**
    실측으로 미매핑 804개 중 768개(95.5%)가 자기 eNB 의 다른 섹터 좌표를
    이미 갖고 있다.
    """
    pts: list[tuple[float, float]] = []
    for la, lo in _rows(conn, f"SELECT LATITUDE, LONGITUDE "
                              f"FROM `{table_name('cell', locale)}` "
                              "WHERE ENB_ID = %s", [enb]):
        lat, lon = dms_to_decimal(la), dms_to_decimal(lo)
        if lat is not None and lon is not None:
            pts.append((lat, lon))
    if not pts:
        return None
    return (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))


def gu_keys(conn, locale: str = "kr") -> set[tuple[int, int]]:
    """그 벌의 `(CITY_ID, GU_ID)` **전수** — 좌표 유무와 무관하다.

    ## `gu_points` 와 쓰임이 다르다 — 섞으면 조용히 틀린다

    `gu_points` 는 **좌표 있는 구만** 준다. 그것이 그 함수의 올바른 동작
    이다(기준점 없이 셀을 놓을 수 없고 `0,0` 으로 채우면 기니만 바다에
    장애가 생긴다). 그런데 **"그 구가 존재하는가"** 를 그 함수로 물으면
    좌표 결손이 곧 "구가 없다"로 읽힌다 —

        실측 2026-10-26: KR `CM_GU` 273구 중 좌표 없음 2구
        (`41283` 고양 일산구 · `12000` 세종). `seed_enb_jp` 가 KR 대응
        판정에 `gu_points` 를 써서 그 둘을 **"KR 에 없는 신규 구역"** 으로
        분류했고, 이미 셀 278개가 좌표를 받은 세종 자리에 JP 전용 eNB 를
        또 놓으려 했다. 수가 676 이 아니라 678 로 나온 것이 유일한 신호였다.

    그래서 **존재 판정은 이 함수, 좌표는 `gu_points`** 로 나눈다.

    ## 키는 `(CITY_ID, GU_ID)` 쌍이다

    `GU_ID` 단독으로 비교하지 않는다. 지금 데이터에서는 우연히 유일하지만
    그 가정이 깨지면 다른 시도의 구가 같은 구로 접힌다 — CLAUDE.md 의
    "지리 롤업은 원시 재집계" 전제가 그 쌍을 키로 본다.
    """
    return {(int(c), int(g)) for c, g in _rows(
        conn, f"SELECT CITY_ID, GU_ID FROM `{table_name('gu', locale)}` "
              f"WHERE CITY_ID IS NOT NULL AND GU_ID IS NOT NULL")}


def gu_points(conn, locale: str = "kr") -> list[tuple[int, int, str, float, float]]:
    """좌표가 있는 시군구. `(CITY_ID, GU_ID, GU_NAME, lat, lon)`.

    좌표 없는 시군구는 **빼고 돌려준다** — 기준점이 없으면 셀을 놓을 수
    없고, 0,0 으로 채우면 기니만 바다에 장애가 생긴다(불변규칙 17).
    호출부가 몇 곳이 빠졌는지 말해야 한다.
    """
    return [(int(c), int(g), str(n), float(la), float(lo))
            for c, g, n, la, lo in _rows(conn, f"""
        SELECT CITY_ID, GU_ID, GU_NAME, LATITUDE, LONGITUDE
          FROM `{table_name("gu", locale)}`
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
    # **파일 이름은 접미사 없는 원본 이름이다.** 디렉터리가 이미 로케일
    # 축이므로(`locale/jp/`) 파일까지 `_JP` 를 붙이면 `locale/jp/CM_GU_JP…`
    # 가 되어 축이 두 번 적힌다. 더 중요한 이유는 **이미 뜬 KR 사본이
    # 복귀의 유일한 보장**이라는 것이다 — 이름 규칙을 바꾸면 `restore` 가
    # 그 파일을 못 찾고, 그것은 "파일을 못 찾았다"가 아니라 **"사본이
    # 없다"** 로 보여 게이트가 멈춘다.
    return LOCALE_DIR / locale / f"{TABLES[key][0]}{DUMP_SUFFIX}"


def dump(conn, locale: str = "kr") -> dict[str, int]:
    """현 DB 의 마스터를 `data/pemdb1/locale/<locale>/` 로 떠낸다.

    **추적 밖이다**(`.gitignore` 가 `data/` 를 디렉터리째 막는다). 그것이
    의도다 — `probe-datagen` 은 PUBLIC 저장소이고 이 내용은 실제 기지국
    자산 대장이다. 복귀 사본은 **로컬에만** 있어야 한다.

    `json.dumps(..., default=str)` 를 유지한다 — 실측으로 네 테이블에
    `Decimal`·`datetime`·`bytes` 가 없어 **한 번도 발동하지 않지만**,
    컬럼이 추가됐을 때 조용히 `TypeError` 로 죽는 것보다 문자열로라도
    남기는 쪽이 낫다. 좌표는 DMS 문자열(`37:00.00/000`)이다 — 예시는
    **명백한 더미**다(실측값을 적으면 다음 사람이 그것을 측정값으로 믿는다).
    """
    (LOCALE_DIR / locale).mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    for key in DUMP_TABLES:
        table = table_name(key, locale)
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
        table = table_name(key, locale)
        path = dump_path(locale, key)
        if not table_exists(conn, table):
            # 벌이 아직 없다. **"사본이 없다"와 다른 말을 한다** — 전자는
            # 사본을 뜨라는 뜻이고 이쪽은 `ensure-tables` 를 먼저 돌리라는
            # 뜻이다. 같은 문구로 뭉치면 사람이 엉뚱한 버튼을 누른다.
            info = {"table": table, "path": str(path),
                    "error": f"테이블이 없다 — 먼저 벌을 만든다 "
                             f"(topo ensure-tables --locale {locale})"}
            ok = False
            tables[key] = info
            continue
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
        "scope": [table_name(k, locale) for k in DUMP_TABLES],
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
        table = table_name(key, locale)
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

    ## **KR 전용이다** — 로케일 인자를 받지 않는다

    이 함수는 `CL_CELL_MAP` 에도 넣는다(셀 대장). 그 테이블은 로케일 축이
    없으므로(`LOCALE_TABLE_KEYS`) JP 벌에만 장비를 놓을 방법이 없다 —
    넣으면 KR 쪽 키 집합이 함께 늘어난다. JP 셀은 **KR 벌의 복사**로
    만들고(`copy_cells`) 좌표만 다시 쓴다(`relocate_cells`), 그래서 여기서
    놓은 eNB 는 그 복사에 **자동으로 따라간다.**
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
        insert_rows(conn, table_name("cell", "kr"), ok)
        insert_rows(conn, table_name("cellmap", "kr"),
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
                         dry_run: bool = False, locale: str = "kr") -> dict:
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

    cell_tbl = table_name("cell", locale)
    for enb in sorted(by_enb):
        have = {int(s) for (s,) in _rows(
            conn, f"SELECT SECTOR FROM `{cell_tbl}` WHERE ENB_ID = %s", [enb])}
        sectors = sorted(s for s in set(by_enb[enb]) if s not in have)
        stats["skipped_exists"] += len(set(by_enb[enb])) - len(sectors)
        if not sectors:
            continue
        anchor = enb_anchor(conn, enb, locale)
        if anchor is None:
            # 좌표 없는 eNB — **의도된 미매핑이다**(`coord_gaps` 머리말)
            stats["skipped_no_anchor"] += len(sectors)
            continue
        base = _rows(conn, f"SELECT CITY_ID, GU_ID, DONG_ID FROM `{cell_tbl}` "
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
        insert_rows(conn, cell_tbl, rows)
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

#: 읍면동 폴리곤이 없는 locale 에서 **생성하는 동 이름의 꼴**.
#:
#: `"{gu} {i}동"` 을 전 locale 에 쓰면 일본 시구정촌에 **한국어 접미사**가
#: 붙는다(`大阪市中央区 1동`). 사용자 요구가 *"실제 지명처럼"* 이라
#: 그 한 글자가 화면에서 바로 보인다.
#:
#: **없는 locale 은 접미사를 붙이지 않는다** — 그 나라의 행정 단위 이름을
#: 모르는 채 지어내면 "실제처럼"의 반대가 된다.
DONG_NAME_FMT: dict[str, str] = {
    "kr": "{gu} {i}동",
    "jp": "{gu}{i}丁目",        # 町丁 단위의 실제 표기
}


def dong_name(locale: str, gu: str, i: int) -> str:
    return DONG_NAME_FMT.get(locale, "{gu} {i}").format(gu=gu, i=i)


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


def kr_dong_ids(conn) -> dict[int, list[int]]:
    """KR `CM_DONG` 의 `{GU_ID: [DONG_ID, …]}` — **생성 동의 설계도다.**

    ## 왜 균일값으로 만들면 안 되는가 (실측 2026-10-26)

    셀의 `DONG_ID` 는 KR 체계(`GU_ID*1000 + 순번`)로 박혀 있고 순번이
    **구마다 다르다**(1~136 · 구당 최대 134 · 평균 34.5). `--dongs 8` 로
    균일하게 만들면 순번 9 이상을 가진 **셀 54.3% 가 동 미매핑**이 되고,
    불변규칙 12 가 그것을 숨기지 않으므로 JP 화면이 거대한 미매핑 패널이
    된다 — 그러면서 **에러는 나지 않는다.** 반대로 136 으로 올리면 셀이
    하나도 없는 추가 구역에까지 동 136개가 생긴다(949×136 = 129,064행).

    그래서 **KR 의 `DONG_ID` 값을 그대로 재사용한다.** 순번을 다시 계산하지
    않는 이유는 KR 쪽에 빈 번호가 있어도(1·2·5 …) 그 모양까지 따라가야
    조인이 전수로 맞기 때문이다.
    """
    out: dict[int, list[int]] = {}
    for g, d in _rows(conn, f"SELECT GU_ID, DONG_ID FROM "
                            f"`{table_name('dong', 'kr')}` "
                            f"WHERE GU_ID IS NOT NULL AND DONG_ID IS NOT NULL "
                            f"ORDER BY GU_ID, DONG_ID"):
        out.setdefault(int(g), []).append(int(d))
    return out


def dong_join_check(conn, locale: str,
                    planned: set[tuple[int, int, int]] | None = None) -> dict | None:
    """셀 벌의 `DONG_ID` 가 동 벌에 **있는가** — 적재 판정 숫자다.

    `planned` 를 주면 **쓰기 전에** 그것으로 답한다(dry-run). 주지 않으면
    실제 두 테이블을 `LEFT JOIN` 한다. 둘을 같은 키로 보고하는 이유는,
    "넣으면 몇 개가 빌 것인가"와 "넣고 나니 몇 개가 비었나"가 같아야
    하기 때문이다 — 다르면 생성과 적재 사이에서 무언가 어긋난 것이다.

    셀 벌이 없으면 **`None`** 이다 — 0 이 아니다(불변규칙 17). 벌을 만들기
    전에는 셀 수를 모르는 것이고, 0 으로 적으면 "전수 조인된다"로 읽힌다.
    """
    cell, dong = table_name("cell", locale), table_name("dong", locale)
    if not table_exists(conn, cell):
        return None
    total = _rows(conn, f"SELECT COUNT(*) FROM `{cell}`")[0][0]
    if planned is None:
        if not table_exists(conn, dong):
            return {"method": "join", "cells": total, "unmatched": total,
                    "unmatched_keys": None,
                    "note": f"{dong} 가 없다 — 전량 미매핑이다"}
        n = _rows(conn, f"""
            SELECT COUNT(*) FROM `{cell}` l
              LEFT JOIN `{dong}` d
                     ON d.CITY_ID = l.CITY_ID AND d.GU_ID = l.GU_ID
                    AND d.DONG_ID = l.DONG_ID
             WHERE d.DONG_ID IS NULL""")[0][0]
        k = _rows(conn, f"""
            SELECT COUNT(*) FROM (
              SELECT l.CITY_ID, l.GU_ID, l.DONG_ID
                FROM `{cell}` l
                LEFT JOIN `{dong}` d
                       ON d.CITY_ID = l.CITY_ID AND d.GU_ID = l.GU_ID
                      AND d.DONG_ID = l.DONG_ID
               WHERE d.DONG_ID IS NULL
               GROUP BY 1, 2, 3) AS t""")[0][0]
        return {"method": "join", "cells": total, "unmatched": int(n),
                "unmatched_keys": int(k)}
    # dry-run — 셀 쪽 조합은 수천 개뿐이라 묶어서 받아 Python 에서 맞춘다
    miss = missk = 0
    for cid, gid, did, n in _rows(
            conn, f"SELECT CITY_ID, GU_ID, DONG_ID, COUNT(*) FROM `{cell}` "
                  f"GROUP BY 1, 2, 3"):
        if (int(cid), int(gid), int(did)) not in planned:
            miss += int(n)
            missk += 1
    return {"method": "planned", "cells": total, "unmatched": miss,
            "unmatched_keys": missk}


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

    ## `dongs_per_gu` 는 **추가 구역용 기본값**이다 (2026-10-26)

    읍면동 폴리곤이 없는 locale 에서 동을 생성할 때, **KR 에 같은 `GU_ID`
    가 있으면 그 구의 `DONG_ID` 를 그대로 재사용한다**(`kr_dong_ids`).
    셀의 `DONG_ID` 가 KR 체계로 박혀 있어서, 구마다 다른 순번 범위를
    균일값으로 덮으면 조인이 깨진다 — 실측으로 `8` 이면 셀 54.3% 가 동
    미매핑이다. 이 인자가 걸리는 곳은 **KR 에 없는 구역**뿐이다(그쪽에는
    셀이 없다).

    결과의 `dong_join` 이 그 조인 상태를 숫자로 말한다 — 쓰기 전에는
    예측(`method: planned`), 쓴 뒤에는 실제 `LEFT JOIN`(`method: join`).
    """
    # **대상은 그 로케일의 벌이다** — `kr` 이면 원본, 그 외는 접미사
    # 테이블(`CM_GU_JP`). 벌이 없으면 멈춘다: 없는 테이블에 `INSERT` 하면
    # `1146` 이 나고 그 메시지는 "깨졌다"로 읽힌다.
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
    # 동을 KR 에서 가져온 구역 수 / 기본값으로 만든 구역 수. 섞어서 한 수로
    # 보고하면 "KR 대응이 빠졌다"를 알 수 없다
    stats_kr_gu = stats_new_gu = 0
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
        # **구마다 동 수가 다르다 — KR 에서 가져온다**(`kr_dong_ids` 머리말).
        #
        # 균일값은 양쪽으로 틀린다: `--dongs 8` 이면 순번 9 이상을 가진 셀
        # 54.3% 가 동 미매핑이 되고(에러 없음), 136 으로 올리면 셀이 없는
        # 추가 구역에까지 동 136개가 생긴다. KR 대응 구역은 **KR 의
        # `DONG_ID` 값을 그대로** 쓰고, KR 에 없는 추가 구역만
        # `dongs_per_gu` 를 쓴다.
        #
        # **`locale == "kr"` 에서는 KR 을 보지 않는다.** 자기 자신을 읽어
        # 자기 자신을 다시 만드는 모양이 되고(순환), 한국은 읍면동 폴리곤이
        # 있어 이 분기로 오지 않는다 — 올 때는 번들이 없는 비정상이므로
        # 종전 동작(균일 생성)을 유지해 변화를 만들지 않는다.
        from_kr = kr_dong_ids(conn) if locale != "kr" else {}
        for gid, (cid, lat, lon, _code) in sorted(gu_at.items()):
            ids = from_kr.get(gid)
            if ids:
                stats_kr_gu += 1
            else:
                # KR 에 없는 구역 — 셀도 없다. 기본값으로 둔다
                ids = [gid * DONG_CODE_STEP + i
                       for i in range(1, dongs_per_gu + 1)]
                stats_new_gu += 1
            pts = scatter(lat, lon, len(ids), spread_km, rng)
            gname = next(r["GU_NAME"] for r in gu_rows if r["GU_ID"] == gid)
            for did, (dlat, dlon) in zip(ids, pts):
                # 이름의 순번은 **코드에서 되돌린다** — `DONG_ID` 가 KR
                # 것이면 이름도 그 번호여야 사람이 둘을 맞출 수 있다
                seq_no = did - gid * DONG_CODE_STEP
                dong_rows.append({
                    "CITY_ID": cid, "GU_ID": gid, "DONG_ID": did,
                    "CITY_NAME": city_name.get(cid), "GU_NAME": gname,
                    "DONG_NAME": dong_name(locale, gname, seq_no),
                    "P_DONG_ID": did,
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

    # **셀의 `DONG_ID` 가 동 벌에 있는가** — 적재 판정 숫자다. 쓰기 전에
    # 답한다(`planned`): 넣고 나서 세면 되돌릴 자리가 없다.
    #
    # **진단이 생성을 죽이지 못하게 한다.** 이 질의는 셀 벌을 읽을 뿐이고
    # 행정구역 생성과 무관하다 — 그쪽이 실패했다고 13,768행 생성을 버리면
    # 손해가 더 크다. 대신 **조용히 넘기지 않고** 사유를 결과에 싣는다:
    # 호출부가 그것을 사람에게 그대로 내린다(`cmd_topo` · `/topo`).
    planned = {(int(r["CITY_ID"]), int(r["GU_ID"]), int(r["DONG_ID"]))
               for r in dong_rows}
    try:
        join = dong_join_check(conn, locale, planned)
    except Exception as e:                        # noqa: BLE001 — 진단이다
        join = {"method": "error", "cells": None, "unmatched": None,
                "unmatched_keys": None, "error": str(e)[:200]}

    stats = {"locale": locale, "city": len(city_rows), "gu": len(gu_rows),
             "dong": len(dong_rows), "emd_source": emd_src,
             "dong_from_kr_gus": stats_kr_gu, "dong_new_gus": stats_new_gu,
             "dong_join": join,
             "tables": [table_name(k, locale) for k in ("city", "gu", "dong")],
             "skipped_sido": len(skipped["sido"]), "skipped_sgg": len(skipped["sgg"]),
             "crosswalk": str(crosswalk_path(geo_dir, locale)),
             "no_coord": sum(1 for r in gu_rows if r["LATITUDE"] is None)}
    if dry_run:
        return stats

    missing = [table_name(k, locale) for k in ("city", "gu", "dong")
               if not table_exists(conn, table_name(k, locale))]
    if missing:
        raise RuntimeError(
            f"대상 테이블이 없다: {', '.join(missing)}\n"
            f"  먼저 벌을 만든다 — topo ensure-tables --locale {locale} --write")
    have = {k: _rows(conn, f"SELECT COUNT(*) FROM `{table_name(k, locale)}`")[0][0]
            for k in ("city", "gu", "dong")}
    if any(have.values()) and not replace:
        raise RuntimeError(
            f"행이 이미 있다 — {have}. 덮어쓰려면 replace 를 켠다.\n"
            "  44만 셀이 참조하는 코드를 바꾸는 일이다 — 사본을 먼저 뜬다")
    for k in ("dong", "gu", "city"):      # 자식부터 비운다
        wipe(conn, table_name(k, locale))
    insert_rows(conn, table_name("city", locale), city_rows)
    insert_rows(conn, table_name("gu", locale), gu_rows)
    insert_rows(conn, table_name("dong", locale), dong_rows)
    out_xw = crosswalk_path(geo_dir, locale)
    out_xw.parent.mkdir(parents=True, exist_ok=True)
    out_xw.write_text(json.dumps(xw_doc, ensure_ascii=False, indent=1),
                      encoding="utf-8")
    # **적재 뒤에 실제 조인으로 다시 센다.** dry-run 의 예측과 달라지면
    # 생성과 적재 사이에서 무언가 어긋난 것이고, 그 어긋남은 "동 드릴다운이
    # 빈다"로만 보인다.
    try:
        stats["dong_join"] = dong_join_check(conn, locale)
    except Exception as e:                        # noqa: BLE001 — 진단이다
        stats["dong_join"] = {"method": "error", "cells": None,
                              "unmatched": None, "unmatched_keys": None,
                              "error": str(e)[:200]}
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


#: 추가 시군구에 발급하는 `GU_ID` 간격. KR 실측이 `11110`·`11140` 처럼
#: 10 단위로 띄워져 있어 같은 결을 쓴다 — 사람이 보고 "같은 체계의 뒷번호"
#: 로 읽을 수 있어야 한다.
GU_ID_STEP = 10


def next_gu_ids(city: int, existing: set[int], want: int,
                step: int = GU_ID_STEP) -> list[int]:
    """그 시도에서 **아직 쓰지 않은** `GU_ID` 를 `want` 개 발급한다.

    ## 번호를 코드에 박지 않는다 — **실측에서 이어 붙인다**

    그 시도의 현재 최대값에서 `step` 씩 올린다. 규칙을 상수로 적어 두면
    (`CITY_ID * 1000 + 900` 같은 것) 다음 판본에서 KR 쪽이 그 자리를 쓰기
    시작할 때 **조용히 충돌한다** — 충돌하면 같은 `GU_ID` 가 두 지역을
    가리키고, 지리 롤업이 원시 재집계라 `geo_pattern` 반경이 거짓으로 커진다.

    ## 접두 2자리가 바뀌면 **멈춘다**

    `seed_geo` 가 `CITY_ID` 를 **`GU_ID` 앞 2자리에서 유도한다**(상용 실측:
    `41195` → `41`). `41990` 다음이 `42000` 이면 그 구역이 **다른 시도**로
    들어가고 에러가 나지 않는다. 그래서 자리수·접두가 바뀌는 순간 발급을
    끊고, 호출부가 "몇 개를 못 넣었다"를 말한다.
    """
    pre = str(city)
    mine = [g for g in existing if str(g).startswith(pre)]
    cur = max(mine) if mine else int(pre + "0" * (5 - len(pre)))
    out: list[int] = []
    while len(out) < want:
        cur += step
        if str(cur)[:len(pre)] != pre or len(str(cur)) != 5:
            break           # 접두·자리수가 바뀌었다 — 그 시도에 자리가 없다
        if cur in existing:
            continue
        out.append(cur)
    return out


def build_jp_code_map(geo_dir: Path, kr_gu: list[tuple[int, int]],
                      include_extra: bool = True) -> dict:
    """JP 경계 코드 → KR 코드 맵. `locale/jp/code_map.json` 의 내용이다.

    `kr_gu` 는 **지금 DB 에 있는** `(CITY_ID, GU_ID)` 목록이다 — 번들에서
    유도하지 않는다. 트래픽이 참조하는 것이 DB 의 코드이고, 번들(250개)과
    DB(273개)가 다르기 때문이다. 다르면 스위칭 후 그 차이만큼 조용히
    미매핑이 된다.

    시도 대응은 `SIDO_MAP_JP` 가 정하고, 그 안에서 `_jp_rank` 순으로 필요
    수만큼 채운다. **공급이 모자라면 멈춘다** — 부족분을 다른 도도부현에서
    끌어오면 한 시도 코드에 떨어진 두 지역이 들어가고, 지리 롤업이 원시
    재집계라 `geo_pattern` 반경이 거짓으로 커진다(`WIDE` 가 상시로 난다).

    ## `include_extra` — **1:1 상한을 푼다** (2026-10-26 사용자 지시)

    *"진짜 1:1 매핑 아니어도 돼. 없는 장비 추가해도 좋아. 행정구역이 일본이
    더 클테니까 말야."* 그래서 KR 273 에 맞춰 자르지 않고, 대응 도도부현에
    **남아 있는 시구정촌을 실제 지명으로 더 담는다.**

    지켜야 하는 것 둘 —

    * **KR 273 과 겹치는 `GU_ID` 의 대응은 그대로 둔다.** 셀 44만행의
      `GU_ID` 가 그 273 안에만 있으므로, 거기를 흔들면 이미 만든 대응이
      깨진다. 추가분은 `next_gu_ids` 가 주는 **KR 에 없는 새 번호**를 받고
      `CM_GU_JP` 에만 존재한다.
    * **이름은 경계 파일의 실제 지명이다.** 생성한 가짜 이름을 쓰지 않는다 —
      "실제 지명처럼" 이 사용자 요구다.

    새 `GU_ID` 구역에는 **장비가 없다**(`CL_CELL_MAP` 은 KR 셀 대장이다).
    `seed_enb_jp` 가 JP 전용 대역으로 놓는다.
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
    no_room: dict[int, dict] = {}
    dense: dict[int, int] = {}      # 1 단위 발급으로 메운 시도
    # 발급 충돌 판정의 기준 집합 — **DB 의 전 시도 `GU_ID`** 다. 시도별로
    # 보면 `11180` 이 서울에서 비어도 다른 시도가 쓰고 있을 수 있다.
    all_gu = {int(g) for _c, g in kr_gu}
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
            # `new` 는 **KR 에 그 `GU_ID` 가 있는가**다. 트래픽이 붙는 쪽과
            # `CM_GU_JP` 에만 있는 쪽을 화면·보고가 갈라 보여야 한다.
            rows.append({"jis": code, "name": name, "gu_id": gu, "new": False})

        # ── 1:1 상한 밖 — **남은 시구정촌을 실제 지명으로 더 담는다**
        rest = pool[len(gus):]
        if include_extra and rest:
            ids = next_gu_ids(city, all_gu, len(rest))
            if len(ids) < len(rest):
                # **10 단위로 모자라면 그 시도는 1 단위로 다시 뽑는다.**
                # 北海道 는 시구정촌 179개인데 `41840~41990` 의 10 단위
                # 자리가 16개뿐이다 — 거기서 끊으면 실제 지명 126개를 버리고
                # 생성 이름을 쓰게 되는데, 사용자 요구가 *"실제 지명처럼"*
                # 이다. 1 단위면 `41831~41999` 169칸이라 다 들어간다.
                #
                # **섞지 않고 통째로 다시 뽑는다** — 한 시도 안에서 10 단위와
                # 1 단위가 섞이면 번호만 보고 체계를 읽을 수 없다. 간격이
                # 좁다는 사실은 `dense_gu_ids` 가 말한다.
                ids = next_gu_ids(city, all_gu, len(rest), step=1)
                dense[city] = len(ids)
            for (_rank, code, name), gid in zip(rest, ids):
                sgg[code] = str(gid)
                all_gu.add(gid)         # 다음 시도 발급과 겹치지 않게
                rows.append({"jis": code, "name": name, "gu_id": gid,
                             "new": True})
            if len(ids) < len(rest):
                # **자리가 없어 못 담은 수를 말한다.** 접두 2자리가 바뀌는
                # 순간 발급이 끊긴다(`next_gu_ids`) — 조용히 자르면 "일본
                # 행정구역이 이만큼뿐"으로 읽힌다.
                no_room[city] = {"pref": pref, "left": len(rest) - len(ids),
                                 "names": [n for _r, _c, n in
                                           rest[len(ids):]][:5]}
        picked[city] = rows

    extra_n = sum(1 for rows in picked.values() for r in rows if r["new"])
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
            "`new: true` 인 구역은 **KR 에 없는 새 `GU_ID`** 다 — "
            "`CM_GU_JP` 에만 있고 트래픽이 참조하지 않는다. 장비는 "
            "`seed_enb_jp` 가 JP 전용 대역으로 놓는다.",
        ],
        "sido": sido,
        "sgg": sgg,
        "picked": picked,
        # 상한을 푼 결과를 **수로 말한다** — 1:1 이었을 때와 비교할 자리다
        "counts": {"kr_gu": len(all_gu) - extra_n, "extra": extra_n,
                   "total": len(sgg)},
        "no_room": no_room,
        # **간격을 좁혀 메운 시도.** 번호가 10 단위가 아니라는 사실이
        # 보여야 한다 — 다음 사람이 "체계가 깨졌다"고 읽지 않게.
        "dense_gu_ids": dense,
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
    """셀 좌표와 **이름**을 그 `GU_ID` 의 현 `CM_GU` 기준으로 다시 쓴다.

    ## 키 집합을 바꾸지 않는다

    `UPDATE` 만 한다 — 행을 넣거나 지우지 않는다. `CL_CELL_MAP`·1분 집계·
    원시 Parquet·`ground_truth.json` 이 `(ENB_ID, SECTOR)` 를 참조하고,
    그것이 `ensure_sector_coords` 가 이미 지키는 불변이다. 그래서 이
    함수는 `fill_coords` 계열과 **역할이 갈린다** —

        ensure_sector_coords   행이 없는 셀에 **INSERT**  (결손 채우기)
        relocate_cells         있는 행의 좌표·이름만 **UPDATE** (나라 전환)

    ## 이름도 함께 바꾼다 — 안 바꾸면 **지도는 오사카인데 이름은 한국**이다

    `RU_NAME`(438,156행 `"[L9]○○빌딩-MT"`)과
    `CL_ENB_ORG.BTS_NAME`(45,483행 `"<장비번호10자리>_○○시_○○동H_L18"`)이
    — **이름 예시는 모양만 적는다.** 실측값은 그 자체가 기지국 자산
    대장이고 이 저장소는 PUBLIC 이다(`.gitignore` 가 같은 이유로 170MB
    마스터를 막는다). 전하려는 것은 "이런 꼴의 한국어 이름이 그만큼
    있다"이므로 모양만으로 충분하다 —
    화면에 그대로 나온다(`graphio store/tables.py:243` 이 후자를 eNB 이름
    마스터로 쓴다). 좌표만 바꾸면 **에러 없이** 그 상태가 된다. 사용자가
    *"DB 째로 이식"* 이라 했으므로 이 DB 가 현장에 간다.

    **번역이 아니라 생성이다.** 사용자 방침이 *"이름 길게 갈 필요 없다.
    짧게 간결하게"* 이고 `CM_GU` 가 이미 그 나라 이름을 들고 있다 —

        BTS_NAME  <시군구명>_<eNB 연번>          大阪市中央区_3
        RU_NAME   <시군구명>_<eNB 연번>-<섹터>   大阪市中央区_3-12

    둘이 같은 접두를 공유하므로 **화면에서 셀과 eNB 가 묶여 보인다.**
    연번은 그 시군구 안에서 `ENB_ID` 정렬 순이라 **결정론적**이다 — 난수면
    돌릴 때마다 이름이 바뀌어 회귀를 비교할 수 없다.

    **`CL_ENB_ORG_MAP`(2,904행)은 건드리지 않는다.** `CL_ENB_ORG`(45,483) 의
    6.4% 뿐이라 전수 미달이고(`tables.py:249` 가 그것을 경고한다) 관례상
    이름·권역은 `CL_ENB_ORG` 를 본다. `LATITUDE_NORM` 은 전 행 NULL 이라
    좌표 소비처도 아니다 — **그 사실을 출력에 적기만 한다.**

    ## eNB 단위로 묶는다

    한 eNB 의 섹터들이 흩어지면 그 eNB 가 `POINT` 가 아니라 `SCATTER` 로
    보이고 **없는 광역 장애가 만들어진다.** eNB 마다 기준점을 하나 뽑아
    그 주변 `spread_km` 안에 섹터를 둔다 — `fill_coords` 가 하던 모양 그대로다.

    기본 `0.35km` 는 graphio 임계에서 나온 값이다 —

        detector.py:1512   if radius < 0.9:   return "POINT"
        detector.py:1533   if radius <= 3.0:  return "CLUSTER"

    올리면 같은 eNB 가 `CLUSTER`·`SCATTER` 로 보여 `geo_hit` 채점이 흔들린다.

    `only_active=True` 면 **1분 집계에 나타나는 셀만** 좌표를 고친다(실측
    2,161). 좁힌 만큼 **이름이 한국으로 남는 셀 수를 보고한다.**

    ## eNB 이름은 **그 로케일의 `enb_org` 벌**에만 쓴다 (2026-10-26)

    `CL_ENB_ORG` 는 처음에 로케일 축이 없는 한 벌이었다. 그 한 벌의
    `BTS_NAME` 을 일본 이름으로 덮으면 **KR 화면의 eNB 이름까지 일본어가
    되고**, `DUMP_TABLES` 밖이라 **되돌아오지 않는다.** 그래서 한동안
    비(非)kr 에서는 아예 건드리지 않고 "한국 이름이 남는다"고 보고만 했다.

    그 상태가 화면에서 어떻게 보이는지가 결정을 바꿨다 — 라벨이
    `RU_NAME` → `BTS_NAME` → 원시 키 순으로 떨어져 **셀 27.6% 가 한국
    이름**으로 뜬다. 사용자가 다섯째 벌(`CL_ENB_ORG_JP`)을 승인했고
    (2026-10-26), 이제 대상이 `table_name("enb_org", locale)` 이다 —
    **KR 벌은 `locale == "kr"` 일 때만 대상이 된다.**

    벌이 아직 없으면 **쓰지 않고 그 사실을 보고한다**
    (`org_untouched_reason`) — 조용히 빼면 화면에 두 나라가 섞인 줄 모른다.
    """
    cell_tbl = table_name("cell", locale)
    if not table_exists(conn, cell_tbl):
        raise RuntimeError(
            f"대상 테이블이 없다: {cell_tbl}\n"
            f"  먼저 벌을 만들고 키를 복사한다 — topo ensure-tables "
            f"--locale {locale} --write · topo copy-cells --locale {locale} --write")
    gus = {g: (c, la, lo, name) for c, g, name, la, lo in gu_points(conn, locale)}
    if not gus:
        raise RuntimeError(
            f"CM_GU 에 좌표가 없다 — 먼저 지리 계층을 만든다 "
            f"(topo seed-geo --locale {locale})")

    # eNB → 기준 시군구. **전수에서 만든다** — `CL_ENB_ORG` 이름이 전수라
    # 활성 셀만 보면 나머지 eNB 의 시군구를 모른다.
    enb_gu: dict[str, dict[int, int]] = {}
    for enb, gu in _rows(conn, f"SELECT ENB_ID, GU_ID FROM `{cell_tbl}`"):
        if gu is None or int(gu) not in gus:
            continue
        enb_gu.setdefault(str(enb), {})[int(gu)] = \
            enb_gu.setdefault(str(enb), {}).get(int(gu), 0) + 1
    # 섹터가 두 시군구에 걸친 경계 eNB 는 **다수결로 하나**를 고른다.
    # 섹터마다 다른 중심을 쓰면 그 eNB 가 흩어진다.
    enb_home: dict[str, int] = {
        enb: max(sorted(v), key=lambda g: v[g]) for enb, v in enb_gu.items()}

    # 시군구 안의 eNB 연번 — `ENB_ID` 정렬 순이라 결정론적이다
    by_gu: dict[int, list[str]] = {}
    for enb in sorted(enb_home):
        by_gu.setdefault(enb_home[enb], []).append(enb)
    enb_no = {enb: i + 1 for gu in by_gu for i, enb in enumerate(by_gu[gu])}

    sql = f"""SELECT l.ENB_ID, l.SECTOR, l.GU_ID
               FROM `{cell_tbl}` l"""
    if only_active:
        # **활성 셀로 좁힌다.** 1분 집계에 나타나는 키만 지도·탐지에
        # 올라간다 — 나머지를 고쳐도 보이는 것이 달라지지 않는다.
        sql += """
               JOIN (SELECT DISTINCT ENB_ID, CELLNUM
                       FROM PM_COR_LTE_CALL_1M) a
                 ON a.ENB_ID = l.ENB_ID AND a.CELLNUM = l.SECTOR"""
    rows = _rows(conn, sql + " ORDER BY l.ENB_ID, l.SECTOR")
    total_cells = _rows(conn, f"SELECT COUNT(*) FROM `{cell_tbl}`")[0][0]

    by_enb: dict[str, list[int]] = {}
    for enb, sector, _gu in rows:
        by_enb.setdefault(str(enb), []).append(int(sector))

    rng = random.Random(seed)
    stats = {"locale": locale, "table": cell_tbl,
             "cells": len(rows), "enbs": len(by_enb),
             "updated": 0, "skipped_no_gu": 0, "patterns": {},
             "renamed_cells": 0, "renamed_enbs": 0,
             "org_untouched_reason": None,
             "cells_out_of_scope": total_cells - len(rows)}
    updates: list[tuple[str, str, str, str, int]] = []

    for enb in sorted(by_enb):
        sectors = sorted(by_enb[enb])
        gu = enb_home.get(enb)
        if gu is None:
            # `GU_ID` 를 모르거나 그 시군구에 좌표가 없다 — **건드리지
            # 않는다.** 아무 데나 놓으면 미매핑이 "매핑된 거짓 좌표"로
            # 바뀌어 `H-07` 이 그 셀을 놓친다(불변규칙 12).
            stats["skipped_no_gu"] += len(sectors)
            continue
        _city, lat, lon, gname = gus[gu]
        pts = scatter(lat, lon, len(sectors), spread_km, rng)
        pat, _r = pattern_of(pts)
        stats["patterns"][pat] = stats["patterns"].get(pat, 0) + 1
        for sector, (clat, clon) in zip(sectors, pts):
            updates.append((decimal_to_dms(clat), decimal_to_dms(clon),
                            f"{gname}_{enb_no[enb]}-{sector}", enb, sector))

    # **그 로케일의 벌에만 쓴다** (위 docstring). `kr` 이면 원본이고 그때는
    # 전수가 맞다 — 이름 마스터를 반만 고치면 두 규칙이 섞인다.
    org_updates: list[tuple[str, str]] = []
    org_tbl = table_name("enb_org", locale)
    stats["org_table"] = org_tbl
    have_org = table_exists(conn, org_tbl)
    if not have_org:
        stats["org_untouched_reason"] = (
            f"{org_tbl} 가 없다 — eNB 이름(BTS_NAME)은 그대로 남는다. "
            f"지도 라벨이 RU_NAME → BTS_NAME 순으로 떨어지므로 셀 일부가 "
            f"한국 이름으로 보인다. 벌을 만들고 복사하면 바뀐다 "
            f"(topo ensure-tables · copy-enb-org --locale {locale} --write)")
    if have_org:
        for enb, in _rows(conn, f"SELECT ENB_ID FROM `{org_tbl}` "
                                f"ORDER BY ENB_ID"):
            gu = enb_home.get(str(enb))
            if gu is None:
                continue
            org_updates.append((f"{gus[gu][3]}_{enb_no[str(enb)]}", str(enb)))

    stats["updated"] = len(updates)
    stats["renamed_cells"] = len(updates)
    stats["renamed_enbs"] = len(org_updates)
    if dry_run:
        return stats
    with conn.cursor() as cur:
        for i in range(0, len(updates), 5000):
            cur.executemany(
                f"UPDATE `{cell_tbl}` "
                "SET LATITUDE = %s, LONGITUDE = %s, RU_NAME = %s "
                "WHERE ENB_ID = %s AND SECTOR = %s", updates[i:i + 5000])
        for i in range(0, len(org_updates), 5000):
            cur.executemany(f"UPDATE `{org_tbl}` SET BTS_NAME = %s "
                            "WHERE ENB_ID = %s", org_updates[i:i + 5000])
    conn.commit()
    return stats


# --------------------------------------------------------------------------
# locale 벌 — **지역 테이블만** 나라별로 만든다 (2026-10-26 방식 확정)
# --------------------------------------------------------------------------
#
# 사용자 방침: *"db 자체를 jp셋으로 만들라는게 아냐. 지역구에 대응하는
# 테이블만 JP로 만들라는거야. CM_GU, CM_DONG 같은 테이블만 말야. 나머진 다
# 그대로 쓸꺼야. 원본 데이터도 그냥 쓸꺼고."*
#
#     CM_CITY_JP              17행    이름 · 중심좌표
#     CM_GU_JP               273행    이름 · 중심좌표
#     CM_DONG_JP           7,847행    이름 · 중심좌표
#     CM_CELL_REP_RU_LOC_JP  442,689행  LATITUDE·LONGITUDE·RU_NAME 만
#
#     CL_ENB_ORG_JP       45,955행   BTS_NAME 만
#
# `ENB_ID`·`SECTOR`·`CITY_ID`·`GU_ID`·`DONG_ID`·각종 `_FLAG`·조직 ID 는
# **KR 값을 그대로 복사한다** — ID 체계를 공유하기로 한 결정이 여기서
# 지켜진다. `CL_CELL_MAP`(55만) · `PM_*` · `AI_*` · 권역 테이블
# (`CM_BRANCH`·`CM_LAREA`·`CM_REGION`)은 **한 벌 공유**다.
#
# ## 셀 벌을 왜 셋이 아니라 **넷**으로 만드는가
#
# 지역 셋(`CM_CITY`·`CM_GU`·`CM_DONG`)만 JP 로 만들면 조인은 성립하고 에러도
# 나지 않는다. 그런데 **좌표는 셀 쪽에 있다** — 지도·탐지가 읽는 자리가
# `CM_CELL_REP_RU_LOC.LATITUDE` 다(graphio `kpi.py:373` · `evidence.py:2099` ·
# `api/app.py` 3곳 · `pipeline.py:571`). 셋만 바꾸면 **일본 지명에 한국
# 좌표**가 붙어 일본 폴리곤이 비어 보이고 `geo_pattern` 반경이 한국
# 좌표에서 계산된다. 조용히 틀리는 종류다.
#
# ## DDL 은 손으로 적지 않는다 — `CREATE TABLE … LIKE`
#
# collation(`utf8mb3_general_ci`)·인덱스·컬럼 타입·기본값이 원본과 1:1 이어야
# 한다. 손으로 적으면 **불변규칙 14** 의 collation 이 어긋나고, 그러면 이름
# 컬럼이 조용히 합쳐지거나 조인이 `Illegal mix of collations` 로 깨진다.
#
# `LIKE` 가 **복제하지 않는 것은 외래키뿐**이다(실측 `pemdb6` 에 0건) —
# `ensure_locale_tables` 가 그 수를 세어 보고한다.


def ensure_locale_tables(conn, locale: str, write: bool = False) -> dict:
    """그 로케일의 벌을 `CREATE TABLE IF NOT EXISTS … LIKE` 로 만든다.

    대상은 `LOCALE_TABLE_KEYS` **전부**다 — 목록을 여기 다시 적지 않는다.
    처음에는 넷이었고 `enb_org` 가 늘었는데(2026-10-26), 그때 이 함수가
    자기 목록을 들고 있었으면 **벌 하나가 조용히 안 만들어진다.** 그
    테이블만 없으면 `relocate_cells` 가 "이름은 그대로 남는다"고만 말하고
    에러가 나지 않는다.

    `write=False` 면 **무엇을 만들지만 말하고 아무것도 하지 않는다** —
    기존 `--write` 게이트와 같은 모양이다.

    **`locale == "kr"` 은 거부한다.** KR 은 원본이고 `LIKE` 를 돌릴 대상이
    아니다. 받아 주면 `wipe` 계열과 엮였을 때 운영 테이블이 위험해지고,
    "아무 일도 안 하는 성공"은 사람이 JP 를 만든 줄로 읽는다.

    행 수를 **없을 때 `None` 으로** 돌려준다 — "테이블이 없다"와 "만들었는데
    0행"은 다른 상태다(불변규칙 17).
    """
    if locale == "kr":
        raise RuntimeError(
            "locale 'kr' 은 원본이다 — 벌을 만들 대상이 아니다.\n"
            "  이 함수는 접미사 벌(`CM_GU_JP` 등)을 만든다. KR 이름을 "
            "대상으로 받으면 `LIKE` 가 돌 자리가 없고, 실수로 `wipe` 경로와 "
            "엮이면 운영 테이블이 위험하다")

    out: dict = {"locale": locale, "write": write, "tables": [],
                 "created": 0, "exists": 0}
    for key in LOCALE_TABLE_KEYS:
        src = TABLES[key][0]
        dst = table_name(key, locale)
        if not table_exists(conn, src):
            raise RuntimeError(f"원본 테이블이 없다: {src}")
        # 원본의 collation 과 외래키 수를 함께 보고한다. 전자는 `LIKE` 가
        # 복제하는 것이고 후자는 **유일하게 복제하지 않는 것**이다.
        coll = _rows(conn, "SELECT TABLE_COLLATION FROM information_schema.TABLES "
                           "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s",
                     [src])
        fks = _rows(conn, "SELECT COUNT(*) FROM information_schema.TABLE_CONSTRAINTS "
                          "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s "
                          "AND CONSTRAINT_TYPE = 'FOREIGN KEY'", [src])[0][0]
        had = table_exists(conn, dst)
        if write and not had:
            with conn.cursor() as cur:
                cur.execute(f"CREATE TABLE IF NOT EXISTS `{dst}` LIKE `{src}`")
            conn.commit()
        now = table_exists(conn, dst)
        info = {
            "key": key, "source": src, "table": dst,
            "existed": had, "created": bool(write and not had),
            "source_rows": _rows(conn, f"SELECT COUNT(*) FROM `{src}`")[0][0],
            # **없으면 `None`** — "만들었는데 0행"과 다른 상태다
            "rows": (_rows(conn, f"SELECT COUNT(*) FROM `{dst}`")[0][0]
                     if now else None),
            "collation": coll[0][0] if coll else None,
            "foreign_keys": int(fks),
        }
        out["tables"].append(info)
        out["created" if info["created"] else "exists"] += 1
    # 외래키가 있으면 `LIKE` 가 그것만 빼놓는다 — 수를 말한다
    out["foreign_keys"] = sum(t["foreign_keys"] for t in out["tables"])
    return out


#: 로케일 벌을 복사할 때 **비워 두는** 컬럼. 그 나라에서 새로 만드는 값이다.
#:
#: **0 이나 빈 문자열로 채우지 않는다**(불변규칙 4·17). 좌표 `0,0` 은 기니만
#: 바다이고 `''` 는 `dms_to_decimal` 이 `None` 으로 읽어 미매핑이 되는데,
#: 그 둘은 "아직 안 만들었다"와 구분되지 않는다. `NULL` 이면 `survey` 의
#: `no_coord` 가 그 수를 그대로 말한다.
#:
#: `enb_org` 는 **`BTS_NAME` 하나뿐**이다 — `REGION_ID`·`BRANCH_ID` 등 조직
#: ID 는 graphio 가 그 숫자로 조인하므로(`kpi.py:532·587`) KR 값을 그대로
#: 가져가야 한다. 이름만 그 나라 것이 된다.
COPY_NULL_COLUMNS: dict[str, tuple[str, ...]] = {
    "cell": ("LATITUDE", "LONGITUDE", "RU_NAME"),
    "enb_org": ("BTS_NAME",),
}

#: 뒤호환 별칭 — `copy_cells` 시절의 이름을 참조하는 자리가 있다.
CELL_COPY_NULL = COPY_NULL_COLUMNS["cell"]


def copy_locale_rows(conn, key: str, locale: str, write: bool = False) -> dict:
    """KR 벌의 행을 그 로케일 벌로 **DB 안에서** 복사한다 — 생성 컬럼은 비운다.

    44만행을 Python 으로 왕복시키지 않는다. `INSERT … SELECT` 한 문장이고,
    `COPY_NULL_COLUMNS[key]` 는 **컬럼 목록에서 빼서 `NULL` 로 남긴다.**
    그 값들은 다음 단계가 만든다 —

        ensure_locale_tables   벌을 만든다 (DDL)
        copy_locale_rows       키·참조를 복사한다  ← 여기
        relocate_cells         좌표·RU_NAME·BTS_NAME 을 그 나라로 쓴다

    ## **이미 행이 있으면 넣지 않는다** — DB 가 막아 주지 않는다

    실측(2026-10-26)으로 다섯 벌 전부 **PK·UNIQUE 가 없다.**
    `CM_CELL_REP_RU_LOC` 의 `ix_local_ru_loc_enb_sector` 는 **비-UNIQUE**
    이고 `CL_ENB_ORG` 에는 인덱스조차 없다. 그래서 두 번 돌리면 중복이
    **에러 없이** 쌓이고, 그러면 한 셀이 좌표를 둘 갖는다. 이 가드가
    그 구멍을 메우는 **유일한 자리**다.

    원본 자체의 중복도 **세어서 말한다** — `CL_ENB_ORG` 는 45,955행 /
    distinct `ENB_ID` 45,954 다. 걸러내지 않는 것이 의도다: 복사는 **거울**
    이고, 원본에 있는 것을 조용히 하나로 접으면 두 환경을 대조할 수 없다.

    비울 컬럼이 `NOT NULL` 이면 **멈춘다** — 0 이나 `''` 로 바꿔 넣으면
    "계산하지 못한 값"이 "계산해서 0" 으로 보인다(불변규칙 17).
    """
    if locale == "kr":
        raise RuntimeError("locale 'kr' 은 원본이다 — 자기 자신을 복사할 수 없다")
    if key not in COPY_NULL_COLUMNS:
        raise RuntimeError(
            f"`{key}` 는 복사 대상이 아니다 "
            f"(있는 것: {', '.join(COPY_NULL_COLUMNS)}).\n"
            "  행정구역 셋(city·gu·dong)은 복사가 아니라 **생성**이다 — "
            "경계 GeoJSON 에서 만든다(`seed_geo`)")
    src = table_name(key, "kr")
    dst = table_name(key, locale)
    nulls = COPY_NULL_COLUMNS[key]
    if not table_exists(conn, dst):
        raise RuntimeError(
            f"대상 테이블이 없다: {dst}\n"
            f"  먼저 벌을 만든다 — topo ensure-tables --locale {locale} --write")

    have_src, have_dst = ddl_columns(conn, src), ddl_columns(conn, dst)
    only_src = sorted(have_src - have_dst)
    cols = [c for c in sorted(have_src & have_dst) if c not in nulls]
    required = ddl_required(conn, dst) & set(nulls)
    if required:
        raise RuntimeError(
            f"{dst} 의 {', '.join(sorted(required))} 가 NOT NULL 이다 — "
            "비워 둘 수 없다.\n"
            "  0 이나 빈 문자열로 채우지 않는다 — '아직 안 만들었다'와 "
            "'만들어서 0' 이 섞인다(불변규칙 17). DDL 을 먼저 본다")

    keycols = TABLES[key][1]
    grp = ",".join(f"`{c}`" for c in keycols)
    n_src = _rows(conn, f"SELECT COUNT(*) FROM `{src}`")[0][0]
    n_uniq = _rows(conn, f"SELECT COUNT(*) FROM "
                         f"(SELECT {grp} FROM `{src}` GROUP BY {grp}) AS t")[0][0]
    n_dst = _rows(conn, f"SELECT COUNT(*) FROM `{dst}`")[0][0]
    stats = {"key": key, "locale": locale, "source": src, "table": dst,
             "source_rows": n_src, "rows_before": n_dst,
             # **원본의 중복을 걸러내지 않고 말한다** (docstring 참조)
             "source_duplicate_keys": n_src - n_uniq,
             "key_columns": list(keycols),
             "columns": len(cols), "null_columns": list(nulls),
             "source_only_columns": only_src, "inserted": 0, "skipped": None}
    if n_dst:
        stats["skipped"] = (
            f"{dst} 에 이미 {n_dst:,}행이 있다 — PK 가 없어 중복 INSERT 가 "
            f"에러 없이 쌓인다. 다시 만들려면 먼저 비운다 "
            f"(topo rm {key} --locale {locale} --all)")
        return stats
    if not write:
        stats["skipped"] = "읽기만 했다 — 적재는 --write 다"
        return stats
    collist = ",".join(f"`{c}`" for c in cols)
    with conn.cursor() as cur:
        n = cur.execute(f"INSERT INTO `{dst}` ({collist}) "
                        f"SELECT {collist} FROM `{src}`")
    conn.commit()
    stats["inserted"] = int(n or 0)
    stats["rows"] = _rows(conn, f"SELECT COUNT(*) FROM `{dst}`")[0][0]
    return stats


def copy_cells(conn, locale: str, write: bool = False) -> dict:
    """셀 벌 복사 — `copy_locale_rows(key="cell")` 의 이름 있는 입구."""
    return copy_locale_rows(conn, "cell", locale, write)


def copy_enb_org(conn, locale: str, write: bool = False) -> dict:
    """eNB 조직 벌 복사 — `BTS_NAME` 만 비운다.

    **왜 이 벌이 필요한가:** 안 만들면 JP 지도에서 셀 이름은 일본어인데
    (`CM_CELL_REP_RU_LOC_JP.RU_NAME`) eNB 이름은 한국어로 뜬다 — 화면
    라벨이 `RU_NAME` → `BTS_NAME` → 원시 키 순으로 떨어지기 때문이다.
    공유 한 벌(`CL_ENB_ORG`)을 덮어 해결할 수는 없다: `DUMP_TABLES` 에
    없어서 **KR 이 되돌아오지 않는다.**
    """
    return copy_locale_rows(conn, "enb_org", locale, write)


# --------------------------------------------------------------------------
# JP 전용 eNB — **`CL_CELL_MAP` 이 공유라 여기만 되돌릴 수 없다**
# --------------------------------------------------------------------------
#
# 사용자 지시(2026-10-26): *"없는 장비 추가해도 좋아. 행정구역이 일본이 더
# 클테니까 말야."* 추가 구역(`new: true` · KR 에 없는 `GU_ID`)에는 장비가
# 없다 — `CM_GU_JP` 에만 있는 구역이고 셀 대장은 KR 것을 복사했으므로.
#
# ## 어려운 자리는 하나다 — 셀 대장이 로케일 축을 갖지 않는다
#
# 생성기가 셀을 고르는 경로가 `CL_CELL_MAP` ⨝ 셀 벌이다(`load_cells`).
# 그러니 "장비를 추가"하는 것은 **공유 테이블에 행을 넣는 일**이고, 그냥
# 넣으면 KR 생성이 그 eNB 를 후보로 본다. 좌표가 KR 벌에 없으니 **미매핑
# 셀**이 되어 `H-07`·UNMAPPED 패널에 상시로 뜬다(불변규칙 12 가 그것을
# 숨기지 못하게 막아 둔 자리다).
#
# ## 그래서 **넣은 목록을 파일로 남기고, 그 목록으로 KR 선택에서 뺀다**
#
# `_pick_enbs` 가 `locale_only_enbs()` 를 빼고 고른다. **대역 숫자가 아니라
# 실제로 넣은 ID 목록**이다 —
#
#   * 숫자를 코드에 박으면 그 대역이 비어 있다는 **가정**이 코드에 남는다.
#     목록은 넣을 때 **실측**으로 확인한 결과다(`seed_enb_jp` 가 세 테이블의
#     기존 `ENB_ID` 와 겹치는지 보고 겹치면 멈춘다).
#   * 같은 파일이 **되돌림 수단**이다. `CL_CELL_MAP` 은 `DUMP_TABLES` 밖이라
#     사본이 없다 — 지울 대상을 아는 유일한 방법이 이 목록이다.
#
# **이 파일이 없어지면 KR 이 그 eNB 를 후보로 본다.** 추적 밖(`data/`)이므로
# 그 사실을 적어 둔다 — 지우려면 먼저 `topo rm-added` 로 DB 에서 빼야 한다.

#: 넣은 JP 전용 eNB 목록. `dump` 사본과 **같은 디렉터리**에 둔다 — 그 둘이
#: 한 벌이다(사본을 되넣으면 그 시점의 셀 집합으로 돌아간다).
ADDED_FILE = "enb_added.json"


def added_enbs_path(locale: str) -> Path:
    return LOCALE_DIR / locale / ADDED_FILE


def load_added_enbs(locale: str) -> dict:
    """그 로케일에 넣은 eNB 원장. 없으면 `{}` — **0 개와 같은 뜻이다.**

    여기서는 "없다"와 "0개"를 같게 두어도 된다. 파일이 없으면 넣은 적이
    없다는 뜻이고, 파일이 있는데 비면 다 되돌렸다는 뜻이라 둘 다 "KR 에서
    뺄 것이 없다"로 귀결된다.
    """
    p = added_enbs_path(locale)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        # **깨진 원장을 조용히 빈 집합으로 읽지 않는다.** 그러면 KR 이 그
        # eNB 를 후보로 보기 시작하고, 원인은 "미매핑이 늘었다"로만 보인다.
        raise RuntimeError(
            f"eNB 원장을 읽을 수 없다: {p}\n"
            "  이 파일이 KR 선택에서 JP 전용 eNB 를 빼는 근거다 — "
            "비면 KR 생성이 그 장비를 후보로 본다(미매핑으로 드러난다)")


def locale_only_enbs() -> set[str]:
    """**KR 생성에서 빼야 하는** eNB 전부 — 모든 비(非)kr 로케일의 합집합.

    `pemdb1._pick_enbs` 가 이것을 부른다. 매 틱 읽는다 — 캐시하면 원장을
    고친 뒤 프로세스를 재시작해야 반영되고, 그 사이의 생성물은 원장과
    어긋난 셀 집합을 담는다.
    """
    out: set[str] = set()
    if not LOCALE_DIR.exists():
        return out
    for d in sorted(LOCALE_DIR.iterdir()):
        if not d.is_dir() or d.name == "kr":
            continue
        out.update(str(e) for e in (load_added_enbs(d.name).get("enbs") or []))
    return out


def _org_defaults(conn, locale: str) -> dict[int, tuple]:
    """시도별 **대표 조직 속성** — 새 eNB 가 조직 축에서 빠지지 않게 한다.

    새 eNB 에는 부모가 없다. `REGION_ID`·`BRANCH_ID` 를 `NULL` 로 두면
    graphio 의 조직 축 조인(`kpi.py:532·587`)에서 그 장비가 통째로 빠지고,
    화면에는 "그 지역에 장비가 없다"로 보인다. 지어내지 않고 **같은 시도에
    가장 많은 조합**을 쓴다 — 유도 근거가 데이터 안에 있다.

    `EMS_ID`·`MME_GRP_ID` 까지 함께 가져온다. 조합을 쪼개 각 컬럼의 최빈값을
    따로 뽑으면 **실재하지 않는 조합**이 만들어진다(지사 A + 팀 B).
    """
    cell, org = table_name("cell", locale), table_name("enb_org", locale)
    rows = _rows(conn, f"""
        SELECT c.CITY_ID, o.COMPANY_ID, o.REGION_ID, o.BRANCH_ID,
               o.OPTEAM_ID, o.PART_ID, o.EMS_ID, o.MME_GRP_ID, COUNT(*) AS n
          FROM (SELECT DISTINCT ENB_ID, CITY_ID FROM `{cell}`) c
          JOIN `{org}` o ON o.ENB_ID = c.ENB_ID
         GROUP BY 1, 2, 3, 4, 5, 6, 7, 8
         ORDER BY c.CITY_ID, n DESC""")
    out: dict[int, tuple] = {}
    for r in rows:
        out.setdefault(int(r[0]), tuple(r[1:8]))     # 시도별 첫 행 = 최빈 조합
    return out


def seed_enb_jp(conn, locale: str, band_start: int, per_gu: int = 1,
                cells_per_enb: int = 3, spread_km: float = 0.35,
                seed: int = 20260903, write: bool = False) -> dict:
    """추가 구역(KR 에 없는 `GU_ID`)에 장비를 놓는다.

    **대역 시작 번호는 인자다.** 코드에 박지 않는다 — 어느 번호가 비어
    있는지는 그 DB 의 사실이고, 이 함수는 받은 번호로 만든 ID 전부가 세
    테이블에서 **실제로 비어 있는지 확인한 뒤**에 쓴다. 겹치면 멈춘다:
    같은 `ENB_ID` 가 두 장비를 가리키면 1분 집계가 둘의 호를 합치고, 그것은
    에러가 아니라 **표본이 늘어난 것처럼** 보인다.

    쓰는 곳 셋 —

        CL_CELL_MAP              셀 대장. **KR 과 공유다 · 사본이 없다**
        CM_CELL_REP_RU_LOC_<L>   좌표 · RU_NAME
        CL_ENB_ORG_<L>           BTS_NAME · 조직 참조

    첫째가 되돌릴 수 없는 자리라, **쓰기 전에 원장을 먼저 남긴다**
    (`added_enbs_path`). 중간에 끊겨도 무엇을 넣으려 했는지가 파일에 있고,
    `remove_added_enbs` 가 그것으로 지운다.

    좌표는 그 시군구 중심에서 `spread_km` 안에 뿌린다 — eNB 하나가
    `POINT` 으로 보여야 `RAN_NODE_DOWN` 해석이 선다(`scatter` 머리말).
    """
    if locale == "kr":
        raise RuntimeError(
            "locale 'kr' 에는 이 함수를 쓰지 않는다 — KR 장비는 "
            "`seed_enb`(topo 대역)가 놓는다. 이쪽은 **KR 선택에서 빠지는** "
            "장비를 만든다")
    cell, org = table_name("cell", locale), table_name("enb_org", locale)
    cellmap = table_name("cellmap", "kr")
    for t in (cell, org):
        if not table_exists(conn, t):
            raise RuntimeError(
                f"대상 테이블이 없다: {t}\n"
                f"  먼저 벌을 만들고 복사한다 — topo ensure-tables · "
                f"copy-cells · copy-enb-org --locale {locale} --write")

    # ── 대상 구역: `CM_GU_<L>` 에는 있고 KR `CM_GU` 에는 없는 구역
    #
    # **존재 판정은 `gu_keys`(전수) · 좌표는 `gu_points`(좌표 있는 것)** 다.
    # 둘을 섞으면 **KR 의 좌표 결손이 "KR 에 없는 구역"으로 읽힌다** —
    # `gu_keys` 머리말의 실측(세종 `12000` 이 그렇게 분류됐다) 참조.
    # 키는 `(CITY_ID, GU_ID)` 쌍이다: `GU_ID` 단독은 유일성을 보장하지 않는다.
    kr_keys = gu_keys(conn, "kr")
    jp_only = {k for k in gu_keys(conn, locale) if k not in kr_keys}
    # 순서는 `gu_points` 의 `ORDER BY GU_ID` 를 그대로 쓴다 — `ENB_ID` 발급이
    # 이 순서를 따르므로 바뀌면 같은 구역이 다른 번호를 받는다
    new_gu = [(c, g, n, la, lo) for c, g, n, la, lo in gu_points(conn, locale)
              if (c, g) in jp_only]
    # **좌표가 없어 장비를 놓을 수 없는 JP 구역.** 조용히 빠지면 그 구역이
    # 왜 빈지 알 수 없다 — 수를 보고한다(불변규칙 12 의 결).
    no_coord = sorted(jp_only - {(c, g) for c, g, _n, _a, _o in new_gu})
    if not new_gu:
        raise RuntimeError(
            f"{table_name('gu', locale)} 에 KR 에 없는 구역이 없다 — "
            "먼저 코드 맵과 행정구역을 1:1 상한 밖까지 만든다 "
            "(topo code-map · seed-geo --write)"
            + (f"\n  (KR 에 없는 구역은 {len(jp_only)}곳인데 전부 좌표가 "
               f"없다 — seed-geo 가 중심좌표를 만들었는지 본다)"
               if jp_only else ""))

    # 그 구역의 동 — **`CM_DONG_<L>` 에서 직접 읽는다.** crosswalk 는 KR
    # 생성의 그릴 수 있는 동 집합이고, 이 장비는 KR 선택에서 빠지므로
    # 그쪽 필터와 무관하다. 동이 없으면 그 구역은 건너뛴다(`DONG_ID` 가
    # `NOT NULL` 이고 0 으로 채우면 조용히 버려진다 · 불변규칙 17).
    dongs: dict[int, list[int]] = {}
    for g, d in _rows(conn, f"SELECT GU_ID, DONG_ID FROM "
                            f"`{table_name('dong', locale)}` "
                            f"ORDER BY GU_ID, DONG_ID"):
        dongs.setdefault(int(g), []).append(int(d))

    # ── ID 발급 — 받은 번호에서 순차. 자리수·대역 밖은 멈춘다
    want = len(new_gu) * per_gu
    ids = [str(band_start + i) for i in range(want)]

    # **세 테이블에서 실제로 비어 있는지 본다.** 대역이 비어 있다는 가정을
    # 코드에 남기지 않는다 — 넣는 시점의 사실로 확인한다.
    taken: set[str] = set()
    for t, col in ((cellmap, "ENB_ID"), (cell, "ENB_ID"), (org, "ENB_ID")):
        taken |= {str(e) for (e,) in _rows(
            conn, f"SELECT DISTINCT `{col}` FROM `{t}`") if e is not None}
    # ── 겹침은 **두 가지**다 — 구분해서 말한다 (2026-10-26 결함 E)
    #
    #   ⓐ 우리가 이미 놓았다   원장에 그 ID 가 있다. **정상 완료 상태**다 —
    #                          멱등이므로 다시 넣지 않고 그렇게 말한다.
    #   ⓑ 남의 번호와 겹친다   원장 밖의 ID 가 쓰이고 있다. **멈춘다** —
    #                          같은 `ENB_ID` 가 두 장비를 가리키면 1분 집계가
    #                          둘의 호를 합치고 에러가 나지 않는다.
    #
    # 전에는 둘을 한 에러로 뭉쳤다. 7단계가 **정상 완료**된 뒤 세어 보기를
    # 누르면 붉은 실패가 떠서, 운영자가 되돌리려 든다.
    ledger = {str(e) for e in (load_added_enbs(locale).get("enbs") or [])}
    clash = sorted(set(ids) & taken)
    foreign = [e for e in clash if e not in ledger]
    if foreign:
        raise RuntimeError(
            f"발급할 ENB_ID {len(foreign)}개가 **원장 밖에서** 이미 쓰이고 "
            f"있다 (예: {', '.join(foreign[:5])}).\n"
            f"  빈 대역을 실측해서 --band-start 로 준다 — 같은 ENB_ID 가 두 "
            f"장비를 가리키면 1분 집계가 둘의 호를 합치고 에러가 나지 않는다"
            + (f"\n  (그중 {len(clash) - len(foreign)}개는 원장에 있는 "
               f"우리 장비다)" if len(clash) > len(foreign) else ""))
    already = sorted(set(ids) & ledger)

    rng = random.Random(seed)
    cell_rows: list[dict] = []
    map_rows: list[dict] = []
    org_rows: list[dict] = []
    defaults = _org_defaults(conn, locale)
    stats = {"locale": locale, "band_start": band_start,
             "gus": len(new_gu), "gus_jp_only": len(jp_only),
             "gus_no_coord": len(no_coord),
             "gus_no_coord_sample": no_coord[:5],
             "enbs": 0, "cells": 0, "already": 0,
             "skipped_no_dong": [], "no_org_default": [],
             "patterns": {}, "tables": {"cellmap": cellmap, "cell": cell,
                                        "org": org},
             "enbs_list": [], "written": False}
    k = 0
    for city, gu, gname, lat, lon in new_gu:
        dl = dongs.get(gu) or []
        if not dl:
            stats["skipped_no_dong"].append(gu)
            k += per_gu            # ID 를 건너뛴다 — 번호가 구역에 묶이지
            continue               # 않게 두면 재실행 때 다른 구역에 붙는다
        for i in range(per_gu):
            enb = ids[k]
            k += 1
            pts = scatter(lat, lon, cells_per_enb, spread_km, rng)
            pat, _r = pattern_of(pts)
            stats["patterns"][pat] = stats["patterns"].get(pat, 0) + 1
            stats["enbs"] += 1
            stats["enbs_list"].append(enb)
            for sector, (clat, clon) in enumerate(pts):
                cell_rows.append({
                    "ENB_ID": enb, "SECTOR": sector,
                    "CITY_ID": city, "GU_ID": gu,
                    "DONG_ID": dl[(sector + i) % len(dl)],
                    "LATITUDE": decimal_to_dms(clat),
                    "LONGITUDE": decimal_to_dms(clon),
                    # 이름 규칙은 `relocate_cells` 와 같다 — 그쪽이 나중에
                    # 다시 돌아도 같은 모양이 나와야 한다
                    "RU_NAME": f"{gname}_{i + 1}-{sector}",
                    "RU_CUID": None, "FLAG": None, "DECODE": enb,
                })
                map_rows.append({
                    "ENB_ID": enb, "CELLNUM": sector, "SECTOR": sector,
                    "CELL_ID": sector, "C_UID": None, "MME_GRP_ID": None,
                    "FA": 1, "PCI": (int(enb) + sector) % 504, "ADM_STATE": 1,
                })
            dft = defaults.get(int(city))
            if dft is None:
                stats["no_org_default"].append(city)
            org_rows.append({
                "ENB_ID": enb, "C_UID": None,
                "COMPANY_ID": dft[0] if dft else None,
                "REGION_ID": dft[1] if dft else None,
                "BRANCH_ID": dft[2] if dft else None,
                "OPTEAM_ID": dft[3] if dft else None,
                "PART_ID": dft[4] if dft else None,
                "EMS_ID": dft[5] if dft else None,
                "MME_GRP_ID": dft[6] if dft else None,
                "BTS_NAME": f"{gname}_{i + 1}",
            })
    stats["cells"] = len(cell_rows)
    if already:
        # **이미 놓여 있다.** 에러가 아니라 완료 상태다 — 멱등이므로 다시
        # 넣지 않는다. 다시 만들려면 원장으로 먼저 지운다(`rm-added`).
        stats["already"] = len(already)
        stats["note"] = (
            f"이미 놓여 있다 — 원장의 {len(already)}대가 그 번호를 쓰고 "
            f"있다(대역 시작 {band_start}). 멱등이므로 다시 넣지 않는다.\n"
            f"  다시 만들려면 먼저 지운다 — topo rm-added --locale {locale}")
        return stats
    if not write:
        stats["note"] = "읽기만 했다 — 적재는 --write 다"
        return stats

    # **원장을 먼저 쓴다** (docstring). 중간에 끊겨도 지울 대상을 안다.
    path = added_enbs_path(locale)
    path.parent.mkdir(parents=True, exist_ok=True)
    prev = load_added_enbs(locale)
    doc = {
        "source": f"topo seed-enb-jp --locale {locale}",
        "note": [
            "**KR 생성에서 이 eNB 를 뺀다** — `pemdb1._pick_enbs` 가 "
            "`topo.locale_only_enbs()` 로 이 목록을 읽는다. 이 파일이 "
            "없어지면 KR 이 후보로 보고 미매핑 셀이 늘어난다.",
            "**되돌림 수단이다.** `CL_CELL_MAP` 은 공유 테이블이고 "
            "`DUMP_TABLES` 밖이라 사본이 없다 — 지울 대상을 아는 유일한 "
            "방법이 이 목록이다 (`topo rm-added`).",
        ],
        "band_start": band_start,
        "enbs": sorted(set((prev.get("enbs") or [])) | set(stats["enbs_list"]),
                       key=lambda e: int(e)),
        "cells_per_enb": cells_per_enb,
    }
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=1),
                    encoding="utf-8")

    insert_rows(conn, cell, cell_rows)
    insert_rows(conn, cellmap, map_rows)
    insert_rows(conn, org, org_rows)
    stats["written"] = True
    stats["ledger"] = str(path)
    stats["ledger_total"] = len(doc["enbs"])
    return stats


def remove_added_enbs(conn, locale: str, write: bool = False) -> dict:
    """원장에 적힌 eNB 를 세 테이블에서 지운다 — **`CL_CELL_MAP` 의 되돌림.**

    그 테이블은 공유이고 사본이 없다(`DUMP_TABLES` 밖). 그래서 "되돌린다"가
    사본 복원이 아니라 **넣은 것만 지운다**는 뜻이고, 그 대상을 아는 유일한
    근거가 원장이다. 원장이 없으면 **아무것도 하지 않고 멈춘다** — 대역으로
    추측해 지우면 그 번호를 쓰는 다른 장비가 함께 사라진다.
    """
    led = load_added_enbs(locale)
    enbs = [str(e) for e in (led.get("enbs") or [])]
    if not enbs:
        raise RuntimeError(
            f"원장이 비어 있다: {added_enbs_path(locale)}\n"
            "  지울 대상을 모른다 — 대역으로 추측해 지우면 그 번호를 쓰는 "
            "다른 장비가 함께 사라진다")
    targets = [(table_name("cellmap", "kr"), "ENB_ID"),
               (table_name("cell", locale), "ENB_ID"),
               (table_name("enb_org", locale), "ENB_ID")]
    marks = ",".join(["%s"] * len(enbs))
    out = {"locale": locale, "enbs": len(enbs), "tables": {}, "written": write}
    for t, col in targets:
        if not table_exists(conn, t):
            out["tables"][t] = None        # 없는 벌 — 지울 것이 없다
            continue
        n = _rows(conn, f"SELECT COUNT(*) FROM `{t}` "
                        f"WHERE `{col}` IN ({marks})", enbs)[0][0]
        out["tables"][t] = n
        if write and n:
            with conn.cursor() as cur:
                cur.execute(f"DELETE FROM `{t}` WHERE `{col}` IN ({marks})", enbs)
    if write:
        conn.commit()
        # **원장을 비운다.** 남겨 두면 KR 선택에서 계속 빠지고, 그 eNB 는
        # 이제 DB 에 없으므로 "왜 빼는지" 알 수 없는 목록이 된다.
        added_enbs_path(locale).write_text(
            json.dumps({**led, "enbs": [],
                        "removed_note": "topo rm-added 로 지웠다"},
                       ensure_ascii=False, indent=1), encoding="utf-8")
    return out


# --------------------------------------------------------------------------
# 행 조회 · 수정 — **화면의 CRUD 그리드가 이 셋을 부른다**
# --------------------------------------------------------------------------
#
# `ui.py` 가 자기 SQL 을 쓰지 않게 하려고 여기 둔다(모듈 머리말의 규율).
# 화면과 커맨드가 각자 조립하면 한쪽만 고쳐질 때 두 경로가 다른 행을 보고,
# 그것은 에러가 아니라 **값이 어긋나는** 종류다.

#: 한 번에 내려주는 행 수의 상한. `CM_DONG` 7,847 · 셀 44만을 통째로 내리면
#: 화면이 멈춘다 — 검색과 페이지로 좁힌다.
BROWSE_MAX = 500


def browse(conn, key: str, locale: str = "kr", q: str = "",
           limit: int = 50, offset: int = 0) -> dict:
    """행 목록 + 전체 수 + 컬럼 · 키 컬럼. **검색은 이름·키 컬럼만** 본다.

    `q` 를 전 컬럼에 `LIKE` 로 걸면 44만행에서 풀스캔이 여러 번 돈다. 이름
    컬럼(`*_NAME`)과 키 컬럼만 보는 것으로 좁히고, **어느 컬럼을 봤는지
    응답에 적는다** — 안 적으면 "검색했는데 안 나온다"가 버그처럼 보인다.

    `total` 은 **그 조건의 전체 수**다. 페이지 수를 화면이 추측하지 않게 한다.
    """
    table = (TABLES[key][0] if key not in LOCALE_TABLE_KEYS
             else table_name(key, locale))
    keys = list(TABLES[key][1])
    if not table_exists(conn, table):
        return {"table": table, "exists": False, "columns": [], "keys": keys,
                "rows": [], "total": None, "searched": [],
                "locale": locale, "shared": key not in LOCALE_TABLE_KEYS}
    cols = [r[0] for r in _rows(
        conn, "SELECT column_name FROM information_schema.columns "
              "WHERE table_schema = DATABASE() AND table_name = %s "
              "ORDER BY ordinal_position", [table])]
    searched = [c for c in cols if c.endswith("_NAME") or c in keys]
    where, params = "", []
    if q:
        where = " WHERE " + " OR ".join(f"`{c}` LIKE %s" for c in searched)
        params = [f"%{q}%"] * len(searched)
    total = _rows(conn, f"SELECT COUNT(*) FROM `{table}`{where}", params)[0][0]
    lim = max(1, min(int(limit or 50), BROWSE_MAX))
    order = ", ".join(f"`{c}`" for c in keys)
    with conn.cursor(pymysql.cursors.DictCursor) as cur:
        cur.execute(f"SELECT * FROM `{table}`{where} ORDER BY {order} "
                    f"LIMIT {lim} OFFSET {max(0, int(offset or 0))}", params)
        rows = cur.fetchall()
    return {"table": table, "exists": True, "columns": cols, "keys": keys,
            "rows": rows, "total": total, "searched": searched,
            "limit": lim, "offset": max(0, int(offset or 0)),
            "locale": locale, "shared": key not in LOCALE_TABLE_KEYS}


def update_row(conn, key: str, locale: str, keyvals: dict, values: dict) -> dict:
    """한 행의 **키가 아닌 컬럼**을 고친다.

    ## 키 컬럼은 받지 않는다

    `(ENB_ID, SECTOR)`·`GU_ID`·`DONG_ID` 는 1분 집계·원시 Parquet·
    `ground_truth.json`·`CL_CELL_MAP` 이 참조하는 **조인 키**다. 화면에서
    그것을 고치면 그 행만 바뀌고 나머지는 옛 키를 그대로 가리켜 **에러 없이**
    끊어진다. 키를 바꾸는 일은 삭제 + 추가이고, 그때는 사람이 두 조작을
    각각 의도해야 한다.

    `WHERE` 가 **키 컬럼 전부**를 요구한다 — 일부만 주면 같은 `GU_ID` 의
    수천 행이 한 번에 바뀐다.
    """
    table = (TABLES[key][0] if key not in LOCALE_TABLE_KEYS
             else table_name(key, locale))
    keycols = list(TABLES[key][1])
    missing = [c for c in keycols if c not in (keyvals or {})]
    if missing:
        raise RuntimeError(
            f"키 컬럼이 빠졌다: {', '.join(missing)} "
            f"(필요한 것: {', '.join(keycols)}) — 일부만 주면 같은 값의 "
            "여러 행이 한 번에 바뀐다")
    have = ddl_columns(conn, table)
    bad_key = [c for c in (values or {}) if c in keycols]
    if bad_key:
        raise RuntimeError(
            f"키 컬럼은 고칠 수 없다: {', '.join(bad_key)}\n"
            "  1분 집계·원시 Parquet·ground_truth 가 그 키를 참조한다 — "
            "바꾸면 그 행만 옮겨지고 나머지는 에러 없이 끊어진다.\n"
            "  키를 바꾸는 것은 삭제 + 추가다")
    sets = [c for c in (values or {}) if c in have]
    dropped = [c for c in (values or {}) if c not in have]
    if not sets:
        raise RuntimeError(
            f"고칠 컬럼이 없다 (DDL 에 없는 것 제외: {', '.join(dropped) or '-'})")
    sql = (f"UPDATE `{table}` SET " + ", ".join(f"`{c}` = %s" for c in sets)
           + " WHERE " + " AND ".join(f"`{c}` = %s" for c in keycols))
    params = [values[c] for c in sets] + [keyvals[c] for c in keycols]
    with conn.cursor() as cur:
        n = cur.execute(sql, params)
    conn.commit()
    return {"table": table, "updated": int(n), "columns": sets,
            "dropped": dropped}


# --------------------------------------------------------------------------
# locale 별 **스키마** — ※ 폐기 대기. 아래는 DB 째 복제 경로의 잔존부다
# --------------------------------------------------------------------------
#
# **2026-10-26 에 방식이 바뀌었다** — 위 "locale 벌" 절이 지금 경로다.
# 아래 `locale_database` · `clone_schema` 는 **별도 스키마(`pemdb6_jp`)로
# DB 를 통째 복제**하던 설계이고, 사용자가 범위를 "지역 테이블만" 으로
# 줄이면서 쓰이지 않는다. 지우지 않고 두는 이유는 `clone` 커맨드가 아직
# 그것을 부르고, "DB 째 이식" 이 다시 필요해질 수 있기 때문이다.
#
# **둘을 섞지 않는다.** 접미사 벌을 `pemdb6_jp` 안에 또 만들면 로케일 축이
# 두 번 적히고(`pemdb6_jp.CM_GU_JP`), 그러면 어느 축이 유효한지 알 수 없다.
# 그래서 `cmd_topo` 는 **`clone` 말고는 locale 로 스키마를 가르지 않는다.**
#
# 사용자 방침(2026-10-26): *"일본 적재는 별도의 테이블로 처리할꺼야. db에만
# 넣어둘꺼고, 이걸 나중에 db채로 이식할꺼야."*
#
# **테이블 이름을 그대로 두고 스키마를 가른다.**
#
#     pemdb6      CM_CITY · CM_GU · CM_DONG · CM_CELL_REP_RU_LOC …   ← KR
#     pemdb6_jp   CM_CITY · CM_GU · CM_DONG · CM_CELL_REP_RU_LOC …   ← JP (같은 이름)
#
# **이 설계가 선택되지 않은 이유**(2026-10-26 사용자 결정): 사용자는
# 지역 테이블만 JP 로 만들고 **나머지와 원본 데이터는 그대로 쓰기로** 했다.
# 스키마를 가르면 `PM_*` 3.9GB 와 `CL_CELL_MAP` 55만행까지 축이 갈려,
# "원본 데이터를 그대로 쓴다"가 성립하지 않는다.
#
# 그리고 *"접미사로 가면 모든 질의에 분기가 들어간다"* 는 당시 근거는
# **graphio 에 대해 틀렸다** — 그쪽은 지역 테이블 이름을
# `store/tables.py` 의 상수 넷(`CELL_LOC`·`CITY`·`GU`·`DONG`)으로만
# 부르고 하드코딩이 0 이다. 이 저장소도 이제 `table_name()` 한 곳이다.
#
# **KR 을 건드리지 않으므로 복귀 사본(`dump kr`)이 이 경로에서는 선행 조건이
# 아니다.** 되돌릴 것이 없다 — JP 스키마를 지우면 된다.

#: locale → 대상 스키마를 설정에서 받는 키. 없으면 `pemdb1.database` 다
#: (= 지금 동작). 이름을 코드에 박지 않는 이유는 현장마다 스키마 이름이
#: 다를 수 있고, **그 이름이 운영으로 넘어가는 값**이기 때문이다.
#:
#:     pemdb1:
#:       database: pemdb6
#:       locale_database:
#:         jp: pemdb6_jp
LOCALE_DB_KEY = "locale_database"

#: **데이터까지 복사하는** 테이블 접두. 나머지는 **구조만** 만든다.
#:
#: 가르는 기준은 "일본에서 새로 생성되는가"다 —
#:
#:   CM_ CL_   마스터(행정구역·셀·장비·조직). 셀 키를 그대로 가져가야
#:             테이블 스위칭이 성립한다 → **복사한다** (실측 153MB)
#:   CD_ CB_   코드 테이블(Cause·메시지·벤더). 나라와 무관하다 → 복사한다
#:   CAUSE_    `CAUSE_DEF` 364행 — Taxonomy 다. 나라와 무관하다 → 복사한다
#:   PM_       1분 집계 3.9GB. **일본 트래픽이 새로 만든다** → 구조만
#:   AI_       탐지·RCA 산출물. 같은 이유로 → 구조만
#:
#: `CM_DATAGEN_HISTORY`(637행)는 **로그**라 구조만 만든다 — 한국에서 돌린
#: 이력이 일본 DB 에 섞이면 "언제 무엇을 넣었나"가 거짓이 된다.
CLONE_DATA_PREFIXES = ("CM_", "CL_", "CD_", "CB_", "CAUSE_")

#: 접두가 맞아도 **구조만** 만드는 예외.
CLONE_STRUCTURE_ONLY = ("CM_DATAGEN_HISTORY",)


def locale_database(cfg: dict, locale: str, override: str | None = None) -> str:
    """그 locale 의 대상 스키마. **기본은 `pemdb1.database` 그대로다.**

    `--database` override 가 가장 세다. 그다음이 설정의
    `pemdb1.locale_database.<locale>` 이고, 없으면 현 DB 다 — 그래서
    `--locale jp` 를 설정 없이 주면 **KR 스키마를 건드린다.** 그 상태를
    막으려고 호출부가 "어느 스키마인가"를 항상 출력한다.
    """
    if override:
        return override
    pem = cfg.get("pemdb1") or {}
    return str((pem.get(LOCALE_DB_KEY) or {}).get(locale)
               or pem.get("database", "pemdb6"))


def clone_schema(conn, source: str, target: str,
                 dry_run: bool = True) -> dict:
    """`source` 스키마의 테이블을 `target` 으로 복제한다.

    **마스터는 데이터까지, 집계·산출물은 구조만**이다(`CLONE_DATA_PREFIXES`).
    `CREATE TABLE … LIKE` 를 쓰므로 **인덱스·기본값·collation 이 그대로**
    간다 — DDL 을 다시 적으면 상용과 다른 구조가 되고 두 환경을 대조할 수
    없다(`docker/mysql/load_master.py` 와 같은 규율).

    **스키마의 collation 을 source 에서 받아온다.** `utf8mb3_general_ci` 가
    아닌 기본값으로 만들면 조인 키가 `Illegal mix of collations` 로 깨지거나
    (`_cs` 쪽) 이름이 조용히 합쳐진다(불변규칙 14).

    `CREATE TABLE IF NOT EXISTS` · 행이 있으면 건너뛰므로 **다시 돌려도
    안전하다** — 중간에 멈췄을 때 이어서 돌리는 것이 정상 경로다.
    """
    if source == target:
        raise RuntimeError(
            f"원본과 대상이 같다: {target}\n"
            "  설정에 pemdb1.locale_database.<locale> 을 넣거나 --database 로 "
            "다른 스키마를 준다 — 같으면 KR 을 덮는다")
    coll = _rows(conn, "SELECT DEFAULT_CHARACTER_SET_NAME, DEFAULT_COLLATION_NAME "
                       "FROM information_schema.SCHEMATA WHERE SCHEMA_NAME = %s",
                 [source])
    if not coll:
        raise RuntimeError(f"원본 스키마가 없다: {source}")
    charset, collation = coll[0]

    rows = _rows(conn, """
        SELECT TABLE_NAME, TABLE_ROWS,
               ROUND((DATA_LENGTH + INDEX_LENGTH) / 1048576, 1)
          FROM information_schema.TABLES
         WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE'
         ORDER BY TABLE_NAME""", [source])
    plan: list[dict] = []
    for name, nrows, mb in rows:
        with_data = (name.startswith(CLONE_DATA_PREFIXES)
                     and name not in CLONE_STRUCTURE_ONLY)
        plan.append({"table": name, "rows": int(nrows or 0),
                     "mb": float(mb or 0), "data": with_data})

    # **대상 스키마를 만들 권한이 있는가.** `CREATE DATABASE` 는 전역
    # 권한이고, 이 계정은 `GRANT ALL ON pemdb6.*` 만 받는다(실측: 전역은
    # `PROCESS` 뿐) — 그래서 **사람이 root 로 한 번 만들어 주어야 한다.**
    # 그 사실을 dry-run 이 미리 말하지 않으면 실행 때 `1044 Access denied`
    # 로 멈추고, 그 메시지는 "계정이 틀렸다"처럼 읽힌다.
    exists = bool(_rows(conn, "SELECT 1 FROM information_schema.SCHEMATA "
                              "WHERE SCHEMA_NAME = %s", [target]))
    grants = [g[0] for g in _rows(conn, "SHOW GRANTS FOR CURRENT_USER()")]
    can_create = any("ON *.*" in g and ("ALL PRIVILEGES" in g or "CREATE" in g)
                     for g in grants)
    has_target = exists and any(
        f"`{target}`" in g or "ON *.*" in g for g in grants)

    stats = {
        "source": source, "target": target,
        "charset": charset, "collation": collation,
        "target_exists": exists,
        "can_create_schema": can_create,
        "has_target_grant": has_target,
        "prepare_sql": [
            f"CREATE DATABASE `{target}` CHARACTER SET {charset} "
            f"COLLATE {collation};",
            # 계정 이름은 `CURRENT_USER()` 에서 `@` 앞을 자른다. SQL 쪽
            # 문자열 함수를 쓰지 않는다 — `chr(64)` 는 MySQL 함수가 아니다
            # (실측 `1305 FUNCTION … does not exist`).
            f"GRANT ALL PRIVILEGES ON `{target}`.* TO "
            f"'{str(_rows(conn, 'SELECT CURRENT_USER()')[0][0]).split('@')[0]}'@'%';",
        ],
        "tables": len(plan),
        "with_data": sum(1 for p in plan if p["data"]),
        "structure_only": sum(1 for p in plan if not p["data"]),
        "copy_rows": sum(p["rows"] for p in plan if p["data"]),
        "copy_mb": round(sum(p["mb"] for p in plan if p["data"]), 1),
        "skip_mb": round(sum(p["mb"] for p in plan if not p["data"]), 1),
        "created": 0, "copied": 0, "already": 0,
    }
    if dry_run:
        stats["plan"] = plan
        return stats

    with conn.cursor() as cur:
        cur.execute(f"CREATE DATABASE IF NOT EXISTS `{target}` "
                    f"CHARACTER SET {charset} COLLATE {collation}")
        for p in plan:
            t = p["table"]
            cur.execute(f"CREATE TABLE IF NOT EXISTS `{target}`.`{t}` "
                        f"LIKE `{source}`.`{t}`")
            stats["created"] += 1
            if not p["data"]:
                continue
            cur.execute(f"SELECT COUNT(*) FROM `{target}`.`{t}`")
            if cur.fetchone()[0]:
                # **이미 있으면 건너뛴다.** 다시 넣으면 PK 없는 테이블
                # (`CM_CELL_REP_RU_LOC`)에 중복이 에러 없이 쌓인다.
                stats["already"] += 1
                continue
            cur.execute(f"INSERT INTO `{target}`.`{t}` "
                        f"SELECT * FROM `{source}`.`{t}`")
            stats["copied"] += 1
    conn.commit()
    return stats


# --------------------------------------------------------------------------
# 조직 이름 — **지리가 아니다.** 별 커맨드로 둔다
# --------------------------------------------------------------------------
#
# `seed-geo` 는 행정구역을 만들고 `relocate-cells` 는 셀을 다룬다. 조직
# (`CM_BRANCH`·`CM_LAREA`·`CM_REGION`)은 **지리와 독립**이라 그 둘 중
# 어디에 붙여도 거짓이 된다 — 실측으로 `CM_REGION` 의 `영동2` 가 **15개
# 시도**에 걸쳐 있고 `CM_REGION` 에는 `CITY_ID` 가 없다.
#
# **ID·부모 연결은 유지한다.** `CL_ENB_ORG.REGION_ID`·`BRANCH_ID` 와
# `CM_EPC_INFO` 쪽이 그 숫자를 참조하고, graphio `kpi.py:532·587` 이
# 그것으로 조인한다 — 바꾸면 조직 축이 통째로 끊어진다. **이름만** 쓴다.

#: `CM_BRANCH` 6줄 — **사람이 정할 표다.** 지금 값은 아키 제안(2026-10-26)
#: 이고, 광역 묶음이라 유도할 근거가 없다(지리와 독립이다).
#:
#: **키는 `BRANCH_ID` 다.** 이름으로 키를 잡으면 한 번 바꾼 뒤 다시 돌릴 때
#: 못 찾는다 — 이 함수는 멱등이어야 한다.
#:
#: 고치려면 이 표만 고친다. `Core Infra` 는 중립이라 그대로, `기타` 만
#: 영문·일본어로 옮긴다.
BRANCH_MAP_JP: dict[int, str] = {
    1000: "関東Infra",      # 수도권Infra
    1100: "東日本Infra",    # 동부Infra
    1300: "西日本Infra",    # 서부Infra
    1400: "中部Infra",      # 중부Infra
    1500: "Core Infra",     # 그대로 — 영문이라 중립이다
    9999: "その他",         # 기타
}

#: `CM_LAREA` 중 **시도가 아닌 이름**. 나머지 8줄은 `CM_CITY` 이름으로
#: `SIDO_MAP_JP` 를 찾아 유도한다 — **별도 표를 만들지 않는다**(두 표가
#: 어긋나면 코어 장비 권역과 지리가 갈린다).
#:
#: `수도권` 은 서울·경기·인천 **셋의 묶음**이라 대응 시도가 없다. 그래서
#: 이 한 줄만 사람이 정한다.
LAREA_EXTRA_JP: dict[str, str] = {"수도권": "関東"}

#: **건드리지 않는 자리.** 적어 두지 않으면 다음 사람이 "한국어가 남았다"고
#: 고친다.
#:
#:   `CL_MME.NET_NAME`    56행 전부 `SKTelecom` — graphio 소비처 **0건**
#:                        (`CL_SGW`·`CL_PGW` 에는 컬럼 자체가 없다)
#:   `CM_EMS.EMS_NAME`    181행 18종(`DUNSAN`·`SUNGSU` 등) — graphio 는
#:                        `CL_ENB_ORG.EMS_ID`(숫자)만 쓰고 이름을 조인하지 않는다
#:   `CM_COMPANY`         21행(`인천Access`·`망운용센터`) — 소비처 0건
#:   `CB_VENDOR`          25행 중 한국어 12종(`삼성`·`SK텔레시스`·`네이블` …)
#:
#: 앞 셋은 **이름이 어디서도 읽히지 않는다** — 바꾸면 반출본과 대조할 수
#: 없어지는 것만 남는다. `CB_VENDOR` 는 읽히지만(SCR-10 표) **제조사 축은
#: 지리 전환과 다른 축**이다 — 일본에서도 삼성 장비를 쓰고, 바꾸는 것은
#: "일본에 없는 벤더를 지우고 있는 벤더를 만드는" 별개 작업이다.
ORG_UNTOUCHED = ("CL_MME.NET_NAME", "CM_EMS.EMS_NAME", "CM_COMPANY",
                 "CB_VENDOR.VENDOR_NAME")


def org_names(conn, locale: str = "jp", dry_run: bool = True,
              allow_shared: bool = False) -> dict:
    """`CM_BRANCH`·`CM_LAREA`·`CM_REGION` 의 **이름만** 다시 쓴다.

    ## 세 테이블에 **로케일 축이 없다** — 그래서 기본이 거부다 (2026-10-26)

    범위가 줄었다: *"지역구에 대응하는 테이블만 JP로 만들고 나머진 다 그대로
    쓴다."* `CM_BRANCH`·`CM_LAREA`·`CM_REGION` 은 `LOCALE_TABLE_KEYS` 밖의
    **한 벌**이고 `DUMP_TABLES` 에도 없다(복귀 사본이 아예 없다). 그 한 벌을
    일본 이름으로 덮으면 **KR 화면의 조직 축이 함께 일본어가 된다.**

    전에는 별도 스키마(`pemdb6_jp`)를 복제하는 설계여서 그쪽을 덮어도 KR 이
    안전했다. 접미사 방식에서는 그 전제가 깨지므로 `allow_shared=True` 를
    **명시적으로** 받아야 돈다 — 조직 이름까지 일본어로 가는 것이 사람의
    결정일 때만 그렇게 된다.

    셋이 다른 규칙이다 — `kpi.py:574` 가 *"`CM_LAREA`(코어 9종)와
    `CM_REGION`(eNB 34종)을 섞지 않는다"* 고 못 박으므로 **같은 규칙으로
    만들지 않는다.**

        CM_BRANCH   6줄   상수 표(`BRANCH_MAP_JP`) — 사람이 정한다
        CM_LAREA    9줄   `CM_CITY` 이름 → `SIDO_MAP_JP` 로 **유도**
                          (`수도권` 만 `LAREA_EXTRA_JP`)
        CM_REGION  34줄   `<부모 BRANCH 이름><연번>` 으로 **생성**

    `CM_REGION` 을 생성하는 이유: 지리와 독립이라 유도할 근거가 없고
    (`영동2` 가 15개 시도에 걸친다) 어차피 자유 명명이다. 34줄을 사람이
    짓는 것은 낭비다.

    **`locale` 이 `kr` 이면 아무것도 하지 않는다** — 한국 이름이 원본이고
    그것을 "생성"으로 덮으면 반출본과 대조할 수 없다.

    부모를 못 찾은 `CM_REGION` 은 **건드리지 않고 수를 보고한다**. 실측으로
    `BRANCH_ID = 1200`(대구) 이 `CM_BRANCH` 에 **없고** 그 밑에 2줄이 있다 —
    이름을 지어내면 그 둘이 "있는 지사"처럼 보인다.
    """
    if locale == "kr":
        return {"locale": locale, "skipped": "kr 은 원본이다 — 덮지 않는다",
                "branch": 0, "larea": 0, "region": 0,
                "region_no_parent": 0, "larea_no_sido": [],
                "untouched": list(ORG_UNTOUCHED)}
    if not allow_shared:
        raise RuntimeError(
            "CM_BRANCH · CM_LAREA · CM_REGION 에는 로케일 축이 없다 — "
            "한 벌을 공유한다.\n"
            f"  locale '{locale}' 이름으로 덮으면 **KR 화면의 조직 축도 함께** "
            "바뀌고, 그 세 테이블은 복귀 사본(`DUMP_TABLES`)에도 없다.\n"
            "  지역 테이블만 나라별 벌을 만드는 것이 지금 범위다 "
            "(CM_CITY_JP · CM_GU_JP · CM_DONG_JP · CM_CELL_REP_RU_LOC_JP).\n"
            "  조직 세 테이블은 **일본 행을 2000번대로 함께 둔다**(2026-10-30) — "
            "graphio `tools/jp_org.py` 가 넣고 도쿄 장비·eNB 가 그 번호를 본다.\n"
            "  그래도 바꿔야 한다면 그것이 사람의 결정이다 — allow_shared 를 켠다")

    # ── CM_BRANCH — 상수 표
    branch: list[tuple[str, int]] = []
    names: dict[int, str] = {}
    for bid, bname in _rows(conn, "SELECT BRANCH_ID, BRANCH_NAME "
                                  "FROM CM_BRANCH ORDER BY BRANCH_ID"):
        new = BRANCH_MAP_JP.get(int(bid))
        if new is None:
            # 표에 없는 지사는 **건드리지 않는다** — 이름을 지어내면
            # 사람이 정한 표와 코드가 만든 이름이 섞인다
            names[int(bid)] = str(bname)
            continue
        names[int(bid)] = new
        branch.append((new, int(bid)))

    # ── CM_LAREA — `SIDO_MAP_JP` 에서 유도
    #
    # **`seed-geo` 보다 먼저 돌아야 한다.** 유도가 `CM_CITY` 의 **한국
    # 이름**을 읽어 `SIDO_MAP_JP` 를 찾으므로, `seed-geo` 가 그 이름을
    # 일본으로 바꾼 뒤에 돌리면 **전량 무매칭**이 되어 9줄이 조용히
    # 제외된다. 그 상태는 "권역 이름만 한국어로 남은" 모양이고 에러가
    # 아니다 — 그래서 **멈춘다.**
    # **KR 벌을 읽는다.** 유도가 한국 시도 이름 → `SIDO_MAP_JP` 이므로
    # 원본이 입력이다. 접미사 방식에서는 `seed-geo --locale jp` 가 KR
    # `CM_CITY` 를 건드리지 않으므로, 전에 있던 **순서 제약(org-names 가
    # seed-geo 앞)이 사라졌다** — 아래 가드는 KR 벌 자체가 다른 나라로
    # 덮인 경우(`seed-geo --locale kr`)를 위해 남긴다.
    sido = {str(n): int(c) for c, n in
            _rows(conn, f"SELECT CITY_ID, CITY_NAME "
                        f"FROM `{table_name('city', 'kr')}`")}
    known = sum(1 for n in sido if n in {"서울", "부산", "대구", "인천", "광주",
                                         "대전", "울산", "세종", "경기", "강원",
                                         "충북", "충남", "전북", "전남", "경북",
                                         "경남", "제주"})
    if sido and known == 0:
        raise RuntimeError(
            "KR CM_CITY 의 이름이 한국 것이 아니다 — CM_LAREA 9줄을 그 "
            "이름으로 `SIDO_MAP_JP` 에서 유도하므로 전량 무매칭이 된다.\n"
            "  그 상태는 '권역 이름만 한국어로 남은' 모양이고 에러가 아니다.\n"
            f"  지금 CM_CITY: {', '.join(sorted(sido)[:5])} …\n"
            "  접미사 방식에서는 KR 벌이 덮이지 않는다 — 덮였다면 "
            "`seed-geo --locale kr` 이 돌았다는 뜻이다 (restore 로 되돌린다)")
    larea: list[tuple[str, int]] = []
    no_sido: list[str] = []
    for lid, lname in _rows(conn, "SELECT LAREA_ID, LAREA_NAME "
                                  "FROM CM_LAREA ORDER BY LAREA_ID"):
        key = str(lname)
        new = LAREA_EXTRA_JP.get(key)
        if new is None:
            city = sido.get(key)
            new = SIDO_MAP_JP.get(str(city)) if city is not None else None
        if new is None:
            no_sido.append(key)
            continue
        larea.append((new, int(lid)))

    # ── CM_REGION — `<부모 이름><연번>`
    region: list[tuple[str, int]] = []
    no_parent = 0
    seq: dict[int, int] = {}
    for rid, bid in _rows(conn, "SELECT REGION_ID, BRANCH_ID "
                                "FROM CM_REGION ORDER BY REGION_ID"):
        parent = names.get(int(bid))
        if parent is None:
            no_parent += 1          # 부모가 `CM_BRANCH` 에 없다 (실측 2줄)
            continue
        seq[int(bid)] = seq.get(int(bid), 0) + 1
        region.append((f"{parent}{seq[int(bid)]}", int(rid)))

    stats = {"locale": locale, "branch": len(branch), "larea": len(larea),
             "region": len(region), "region_no_parent": no_parent,
             "shared_tables": ["CM_BRANCH", "CM_LAREA", "CM_REGION"],
             "larea_no_sido": no_sido, "untouched": list(ORG_UNTOUCHED),
             "sample": {"branch": branch[:2], "larea": larea[:2],
                        "region": region[:2]}}
    if dry_run:
        return stats
    with conn.cursor() as cur:
        # **이름만 쓴다.** `BRANCH_ID`·`LAREA_ID`·`REGION_ID`·`TEAM_ID`·
        # `REGION_TYPE` 은 손대지 않는다 — graphio 가 그 숫자로 조인한다.
        if branch:
            cur.executemany("UPDATE CM_BRANCH SET BRANCH_NAME = %s "
                            "WHERE BRANCH_ID = %s", branch)
        if larea:
            cur.executemany("UPDATE CM_LAREA SET LAREA_NAME = %s "
                            "WHERE LAREA_ID = %s", larea)
        if region:
            cur.executemany("UPDATE CM_REGION SET REGION_NAME = %s "
                            "WHERE REGION_ID = %s", region)
    conn.commit()
    return stats
