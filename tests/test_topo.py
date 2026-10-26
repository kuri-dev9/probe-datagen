"""`topo` 불변 규칙 — 깨면 조용히 틀린 마스터가 만들어진다.

`src/datagen/topo.py` 가 지켜야 하는 것 넷을 고정한다. 공통점은 **위반해도
예외가 나지 않는다**는 것이다 — 행은 들어가고 커맨드는 0 을 돌려주는데
시뮬레이션·지도·채점이 어긋난다.

**DB 를 쓰지 않는다.** `tests/test_session_model.py` 머리말의 집안 규칙이고
이유도 같다 — MySQL 이 떠 있어야 도는 테스트는 환경에 따라 건너뛰어져
아무것도 지키지 못한다. 대신 `FakeConn` 이 SQL 을 받아 기록하고 미리 정한
행을 돌려준다. 그래서 **무엇을 질의했는지까지** 단정할 수 있다 — "DELETE 를
한 번도 쏘지 않았다"는 실 DB 로는 확인이 어렵다.

`T2` 는 graphio 를 임포트한다. **실패하면 `skip` 이 아니라 `fail`** 이다 —
지리 패턴 임계(`0.9`/`3.0`)가 두 저장소에 사본으로 있고, 한쪽만 고쳐질 때
알려 줄 자리가 이 테스트뿐이다. skip 으로 두면 CI 에서 조용히 사라지고
"통과했다"로 읽힌다.
"""

from __future__ import annotations

import gzip
import json
import random
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from datagen import topo

HERE = Path(__file__).resolve().parents[1]

#: graphio 소스 위치. **형제 디렉터리 고정이다** — `probe-datagen` 과
#: `graphio-probe-rca` 는 같은 부모 아래 나란히 clone 된다(현장 포함:
#: `docs/DEPLOY.md`). 없으면 T2 가 실패해야 한다(위 머리말).
GRAPHIO_SRC = HERE.parent / "graphio-probe-rca" / "src"


# --------------------------------------------------------------------------
# 가짜 커넥션 — SQL 을 기록하고 미리 정한 행을 돌려준다
# --------------------------------------------------------------------------


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.rows: list[tuple] = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.conn.sql.append(" ".join(sql.split()))
        self.rows = self.conn.answer(sql, list(params or []))
        return len(self.rows)

    def executemany(self, sql, data):
        one = " ".join(sql.split())
        self.conn.sql.append(one)
        self.conn.inserted.append((one, list(data)))
        return len(data)

    def fetchall(self):
        return self.rows


class FakeConn:
    """`topo` 가 실제로 던지는 질의 다섯 가지에만 답한다.

    모르는 질의에는 **빈 목록이 아니라 예외**를 준다 — 빈 목록을 주면 코드가
    "데이터가 없다"로 읽고 조용히 0 행을 만든다. 테스트가 그걸 통과로
    세는 것이 이 파일이 막으려는 바로 그 사고다.
    """

    def __init__(self, *, ddl=(), unmapped=(), gus=(), coords=None, base=None,
                 sectors=None):
        self.ddl = set(ddl)
        self.unmapped = list(unmapped)
        self.gus = list(gus)
        self.coords = dict(coords or {})     # ENB_ID → [(LAT, LON) DMS 문자열]
        self.base = dict(base or {})         # ENB_ID → (CITY_ID, GU_ID, DONG_ID)
        #: ENB_ID → 이미 행이 있는 SECTOR 들. **멱등의 근거다** —
        #: `CM_CELL_REP_RU_LOC` 에 PK 가 없어 중복 INSERT 가 에러 없이 통과한다.
        self.sectors = {k: list(v) for k, v in (sectors or {}).items()}
        self.sql: list[str] = []
        self.inserted: list[tuple[str, list]] = []
        self.commits = 0

    def cursor(self, cls=None):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1

    def close(self):
        pass

    def answer(self, sql, params):
        if "information_schema.columns" in sql:
            return [(c,) for c in sorted(self.ddl)]
        if "PM_COR_LTE_CALL_1M" in sql:
            return list(self.unmapped)
        if "FROM CM_GU" in sql:
            return list(self.gus)
        if "SELECT SECTOR FROM CM_CELL_REP_RU_LOC" in sql:
            return [(x,) for x in self.sectors.get(str(params[0]), [])]
        if "SELECT LATITUDE, LONGITUDE FROM CM_CELL_REP_RU_LOC" in sql:
            return list(self.coords.get(str(params[0]), []))
        if "SELECT CITY_ID, GU_ID, DONG_ID" in sql:
            b = self.base.get(str(params[0]))
            return [b] if b else []
        raise AssertionError(f"FakeConn 이 모르는 질의다: {sql}")


LOC_DDL = ("ENB_ID", "SECTOR", "CITY_ID", "GU_ID", "DONG_ID",
           "LATITUDE", "LONGITUDE", "RU_NAME", "RU_CUID", "FLAG", "DECODE")
MAP_DDL = ("ENB_ID", "CELLNUM", "SECTOR", "CELL_ID", "C_UID", "MME_GRP_ID",
           "FA", "PCI", "ADM_STATE")


def _inserted_keys(conn, table: str) -> set[tuple]:
    """그 테이블에 들어간 `(첫 두 컬럼)` 집합. 컬럼 순서는 `insert_rows` 가
    `rows[0]` 의 키 순서로 정하므로 `ENB_ID`·`SECTOR`(또는 `CELLNUM`)가 앞이다."""
    out = set()
    for sql, data in conn.inserted:
        if f"INSERT INTO `{table}`" in sql:
            out.update((str(r[0]), int(r[1])) for r in data)
    return out


def _inserted_rows(conn, table: str) -> list[dict]:
    rows = []
    for sql, data in conn.inserted:
        if f"INSERT INTO `{table}`" not in sql:
            continue
        cols = [c.strip(" `") for c in
                sql.split("(", 1)[1].split(")", 1)[0].split(",")]
        rows += [dict(zip(cols, r)) for r in data]
    return rows


# --------------------------------------------------------------------------
# T1 — 좌표 생성은 **생성기 경로**이고, 의도된 미매핑을 건드리지 않는다
# --------------------------------------------------------------------------
#
# 전에는 `topo fill-coords` 라는 수동 커맨드였다(2026-10-26 이동). 사람이
# 기억해서 쳐야 했고 현장에 반출하면 아무도 치지 않는다 — 지금은
# `load_cells(ensure_coords=True)` 가 매 틱 부른다.


#: `load_cells` 의 `uniq` 행 모양. `_pick_enbs` 는 인덱스 5, `coord_gaps` 는
#: 8·9(좌표)를 본다. 전체는 10칸이다.
def _urow(city, gu, dong, lat, lon):
    return (None, None, None, None, None, city, gu, dong, lat, lon)


#: 좌표 있는 eNB 2개(섹터 결손 있음) + **좌표가 아예 없는 eNB 1개**.
#: 후자가 `_pick_enbs` 가 불변규칙 12 검증용으로 남기는 자리다.
def _gap_uniq():
    u = {}
    for sector in range(2):                      # 811110 — 좌표 있다
        u[("811110", sector)] = _urow(41, 41110, 1101053,
                                      "37:30.0/000", "127:00.0/000")
    for sector in (2, 3):                        # 그 eNB 의 결손 2셀
        u[("811110", sector)] = _urow(None, None, None, None, None)
    u[("811130", 0)] = _urow(41, 41130, 1101054, "37:25.0/000", "127:05.0/000")
    u[("811130", 1)] = _urow(None, None, None, None, None)
    for sector in range(3):                      # 822222 — **좌표가 하나도 없다**
        u[("822222", sector)] = _urow(None, None, None, None, None)
    return u


def test_coord_gaps_leaves_the_intentionally_unmapped_enb_alone():
    """**의도된 미매핑을 결손으로 세지 않는다.**

    `_pick_enbs` docstring — *"마지막 5% 는 좌표 없는 eNB 로 남긴다 —
    미매핑 셀이 0이면 `H-07`(미매핑 감시)과 UNMAPPED 패널이 한 번도
    검증되지 않는다(불변규칙 12)."* 실측 804건은 두 종류가 섞여 있다 —
    좌표 없는 eNB 3개(108셀)와 좌표 있는 eNB 56개의 섹터 결손(696셀)이고,
    **전부 채우면 그 감시 경로가 영구히 죽는다.**
    """
    gaps = topo.coord_gaps(_gap_uniq(), None)
    assert gaps == [("811110", 2), ("811110", 3), ("811130", 1)]
    assert not [g for g in gaps if g[0] == "822222"]


def test_coord_gaps_respects_the_pick_enbs_selection():
    """생성 대상이 아닌 eNB 의 좌표는 만들지 않는다 — 쓰지 않을 행이다."""
    assert topo.coord_gaps(_gap_uniq(), {"811110"}) == [("811110", 2), ("811110", 3)]
    assert topo.coord_gaps(_gap_uniq(), set()) == []


def _gap_conn():
    return FakeConn(
        ddl=LOC_DDL,
        coords={"811110": [("37:30.0/000", "127:00.0/000")],
                "811130": [("37:25.0/000", "127:05.0/000")]},
        base={"811110": (41, 41110, 1101053), "811130": (41, 41130, 1101054)},
        sectors={"811110": [0, 1], "811130": [0]},
    )


def test_ensure_sector_coords_only_inserts_and_keeps_the_key_set():
    """**INSERT 만 하고 활성 셀 키 집합을 바꾸지 않는다.**

    ① 들어간 키가 `coord_gaps` 가 준 것과 **정확히 같다** (새 키를 만들지
       않는다 — `ground_truth.json` 이 참조하는 집합이 그쪽이다)
    ② `CL_CELL_MAP` 에는 한 행도 넣지 않는다 (활성 키의 정본이다)
    ③ `DELETE`·`UPDATE` 를 한 번도 쏘지 않는다
    """
    uniq = _gap_uniq()
    gaps = topo.coord_gaps(uniq, None)
    conn = _gap_conn()
    r = topo.ensure_sector_coords(conn, gaps)

    assert _inserted_keys(conn, "CM_CELL_REP_RU_LOC") == set(gaps)
    assert not _inserted_keys(conn, "CL_CELL_MAP")
    joined = " ".join(conn.sql).upper()
    assert "DELETE" not in joined and "UPDATE" not in joined
    assert r["inserted"] == len(gaps) == 3
    # 돌려준 `rows` 로 호출부가 메모리의 `uniq` 를 갱신한다 — 안 하면 이 틱의
    # 집계가 미매핑으로 남는다
    assert set(r["rows"]) == set(gaps)
    for city, gu, dong, lat, lon in r["rows"].values():
        assert city is not None and dong is not None
        assert lat and lon


def test_ensure_sector_coords_is_idempotent():
    """두 번 불러도 **중복 INSERT 가 없다.**

    `CM_CELL_REP_RU_LOC` 에 PK 가 없어 중복 행은 **에러 없이** 들어간다 —
    그러면 같은 셀이 두 좌표를 갖고 `load_cells` 의 `uniq.setdefault` 가
    먼저 읽은 쪽을 쓴다. 매 틱 도는 경로라 이 성질이 없으면 하루에 1,440벌이
    쌓인다. 그래서 넣기 전에 **그 eNB 의 현재 섹터 목록**을 조회한다.
    """
    gaps = topo.coord_gaps(_gap_uniq(), None)
    conn = _gap_conn()
    topo.ensure_sector_coords(conn, gaps)
    first = _inserted_keys(conn, "CM_CELL_REP_RU_LOC")

    # 두 번째 호출 — DB 에는 이제 그 섹터들이 있다
    conn.sectors = {"811110": [0, 1, 2, 3], "811130": [0, 1]}
    conn.inserted.clear()
    r = topo.ensure_sector_coords(conn, gaps)
    assert r["inserted"] == 0
    assert r["skipped_exists"] == len(first) == 3
    assert not _inserted_keys(conn, "CM_CELL_REP_RU_LOC")


def test_ensure_sector_coords_uses_the_enbs_own_sectors_as_the_anchor():
    """기준점을 시군구 중심으로 떨어뜨리면 **없는 광역 장애**가 만들어진다.

    eNB 가 시군구 중심에서 수 km 떨어져 있을 때 같은 eNB 의 새 셀을 중심에
    놓는 순간 그 eNB 가 `POINT` 가 아니라 `SCATTER` 로 보인다.
    """
    from datagen.geo_lite import dms_to_decimal, haversine_km

    conn = _gap_conn()
    topo.ensure_sector_coords(conn, [("811110", 2)], spread_km=0.35)
    row = _inserted_rows(conn, "CM_CELL_REP_RU_LOC")[0]
    lat, lon = dms_to_decimal(row["LATITUDE"]), dms_to_decimal(row["LONGITUDE"])
    assert haversine_km(37.5, 127.0, lat, lon) <= 0.35 + 1e-6


def test_ensure_sector_coords_skips_an_enb_without_an_anchor():
    """기준점이 없으면 **건너뛴다** — `coord_gaps` 가 이미 걸러 주지만
    이중으로 막는다. 0,0 으로 채우면 기니만 바다에 장애가 생긴다(불변규칙 17)."""
    conn = FakeConn(ddl=LOC_DDL, sectors={"822222": []})
    r = topo.ensure_sector_coords(conn, [("822222", 0), ("822222", 1)])
    assert r["inserted"] == 0 and r["skipped_no_anchor"] == 2
    assert not _inserted_keys(conn, "CM_CELL_REP_RU_LOC")


def test_fill_coords_is_check_only_and_counts_like_the_generator():
    """`topo fill-coords` 는 **적재 경로가 아니고, 생성기와 같은 수를 센다.**

    적재 경로가 둘이면 어느 쪽이 넣었는지 모른다 — 그래서 이쪽은 세는 일만
    하고 `dry_run=False` 를 받으면 거부하며 **대안을 말한다.**

    그리고 세는 기준이 `coord_gaps` 와 **같아야** 한다. 처음에는
    `unmapped_active_cells` + `enb_anchor` 로 싸게 셌는데 실 DB 에서
    **768/36 대 696/108** 로 갈렸다 — 두 eNB 가 좌표 행을 갖고 있는데
    `_filter_drawable` 이 그 행을 후보에서 떨어뜨려, 생성기에게는 "좌표
    없는 eNB"이고 `enb_anchor` 에게는 "좌표 있는 eNB"였다. **점검이 생성기와
    다른 수를 말하면 점검이 아니다.**
    """
    conn = _LoadConn(_join_rows(), sectors={"811110": [0, 1]},
                     coords={"811110": [("37:30.0/000", "127:00.0/000")]},
                     base={"811110": (41, 41110, 1101053)})
    r = topo.fill_coords(conn)
    assert r["cells"] == 7
    assert r["targets"] == 5                    # 좌표 없는 셀 전체
    assert (r["fillable"], r["fillable_enbs"]) == (2, 1)        # 811110 의 결손
    assert (r["intended_unmapped"], r["intended_enbs"]) == (3, 1)   # 822222
    joined = " ".join(conn.sql).upper()
    assert "INSERT" not in joined and "DELETE" not in joined
    with pytest.raises(RuntimeError, match="점검 전용"):
        topo.fill_coords(conn, dry_run=False)


def test_fill_coords_and_coord_gaps_cannot_drift():
    """두 수가 **같은 함수에서** 나오는지. 규칙이 두 벌이면 조용히 갈라진다.

    `fill_coords` 는 `coord_gaps` 를 부르고 자기 판정을 갖지 않는다 —
    소스에서 직접 센다(`pick_report` 가 `_pick_enbs` 를 부르는 것과 같은 모양).
    """
    import inspect

    src = inspect.getsource(topo.fill_coords)
    assert "coord_gaps(" in src
    # 판정을 여기 옮겨 적으면 안 된다 — `geo[enb]` 를 다시 계산하는 흔적
    assert "enb_anchor(" not in src
    assert "unmapped_active_cells(" not in src


def test_only_the_generating_commands_turn_ensure_coords_on():
    """**읽기 경로는 쓰지 않는다.**

    `load_cells` 는 조회 함수이고 `ensure_coords` 기본이 `False` 다.
    `cmd_capacity` docstring 이 *"셀 마스터는 DB 에서 읽는다. 읽기뿐이다"* 를
    보증하므로 그쪽이 켜면 그 보증이 깨진다. UI(`ui._cells`)와
    `topo.pick_report` 도 조회다.

    **함수 단위로 본다** — 파일 전체에서 이름을 세면 플래그를 *설명하는*
    주석에 걸린다(`ui.py` 가 실제로 그렇다). 걸리면 테스트가 "켜져 있다"고
    말하고, 그 거짓 실패를 끄다가 진짜 호출을 놓친다.
    """
    import inspect

    from datagen import cli, pemdb1, ui

    assert (inspect.signature(pemdb1.load_cells)
            .parameters["ensure_coords"].default is False)

    src = inspect.getsource(cli)
    for name in ("cmd_run", "cmd_pemfill"):
        block = src[src.index(f"def {name}("):]
        assert "ensure_coords=True" in block[:block.index("\ndef ")], name
    assert "ensure_coords" not in inspect.getsource(cli.cmd_capacity)
    assert "ensure_coords" not in inspect.getsource(ui._cells)
    assert "ensure_coords" not in inspect.getsource(topo.pick_report)


class _LoadConn(FakeConn):
    """`load_cells` 가 던지는 질의 셋에 답한다 — 배선을 끝까지 돌리려고 둔다.

    `coord_gaps`·`ensure_sector_coords` 를 따로 부르는 테스트만으로는
    **`load_cells` 가 그 둘을 부르는지**를 확인할 수 없다. 거기가 이번
    이동의 핵이다(수동 커맨드 → 생성기 경로).
    """

    def __init__(self, join_rows, **kw):
        super().__init__(ddl=LOC_DDL, **kw)
        self.join_rows = list(join_rows)

    def answer(self, sql, params):
        if "FROM CL_CELL_MAP" in sql:
            return list(self.join_rows)
        if "FROM CL_MME" in sql:
            return [("0001", "1")]
        if "FROM CL_PGW" in sql:
            return [("0037",)]
        return super().answer(sql, params)


def _join_rows():
    """`load_cells` 의 조인 결과 — `_gap_uniq` 와 같은 상황을 DB 모양으로."""
    out = []
    for sector in range(2):
        out.append(("811110", sector, None, "1", None, 41, 41110, 1101053,
                    "37:30.0/000", "127:00.0/000"))
    for sector in (2, 3):
        out.append(("811110", sector, None, "1", None, None, None, None, None, None))
    for sector in range(3):      # 좌표 없는 eNB — **의도된 미매핑**
        out.append(("822222", sector, None, "1", None, None, None, None, None, None))
    return out


def test_load_cells_writes_nothing_when_ensure_coords_is_off():
    """끈 경로에서 **좌표 생성 함수가 불리지 않는다.**"""
    from datagen import pemdb1 as P

    conn = _LoadConn(_join_rows(), sectors={"811110": [0, 1]},
                     coords={"811110": [("37:30.0/000", "127:00.0/000")]},
                     base={"811110": (41, 41110, 1101053)})
    cells = P.load_cells(conn, enb_limit=0)
    assert not conn.inserted
    joined = " ".join(conn.sql).upper()
    assert "INSERT" not in joined and "DELETE" not in joined
    assert sum(1 for c in cells if c.geo_status == "UNMAPPED") == 5   # 2 + 3


def test_load_cells_fills_the_sector_gaps_when_ensure_coords_is_on():
    """켠 경로에서 **섹터 결손만** 채우고 의도된 미매핑은 남긴다.

    그리고 **이 호출의 반환값에 바로 반영된다** — 갱신하지 않으면 지금 만든
    좌표가 다음 틱부터만 쓰이고 그 한 분의 집계는 미매핑으로 남는다.
    """
    from datagen import pemdb1 as P

    conn = _LoadConn(_join_rows(), sectors={"811110": [0, 1]},
                     coords={"811110": [("37:30.0/000", "127:00.0/000")]},
                     base={"811110": (41, 41110, 1101053)})
    cells = P.load_cells(conn, enb_limit=0, ensure_coords=True)

    assert _inserted_keys(conn, "CM_CELL_REP_RU_LOC") == {("811110", 2), ("811110", 3)}
    assert not _inserted_keys(conn, "CL_CELL_MAP")
    joined = " ".join(conn.sql).upper()
    assert "DELETE" not in joined and "UPDATE" not in joined

    by_key = {(c.enb_id, c.cell_id): c for c in cells}
    # 채운 둘은 이 반환값에서 이미 MAPPED 다
    for key in (("811110", 2), ("811110", 3)):
        assert by_key[key].geo_status == "MAPPED", key
        assert by_key[key].latitude and by_key[key].longitude
        assert by_key[key].adm_emd_cd == "1101053"
    # 좌표 없는 eNB 3셀은 **그대로 UNMAPPED** 다 (불변규칙 12)
    for sector in range(3):
        assert by_key[("822222", sector)].geo_status == "UNMAPPED"
    # **활성 셀 키 집합이 그대로다** — 새 키를 만들지 않는다
    assert set(by_key) == {(e, c) for e, c, *_ in _join_rows()}


# --------------------------------------------------------------------------
# T2 — 생성 좌표가 graphio 의 지리 패턴 임계를 지킨다
# --------------------------------------------------------------------------


def _geo_pattern():
    """graphio 의 `geo_pattern` 정본. **임포트 실패는 `fail` 이다.**

    `pytest.importorskip` 를 쓰지 않는다 — 그러면 임계가 갈라진 상태에서
    테스트가 조용히 건너뛰어진다. graphio 가 없으면 그 사실 자체가 실패다.
    """
    if str(GRAPHIO_SRC) not in sys.path:
        sys.path.insert(0, str(GRAPHIO_SRC))
    try:
        from graphio.detect.detector import geo_pattern
    except ImportError as e:        # noqa: BLE001 — 이유를 보여야 한다
        raise AssertionError(
            f"graphio 를 임포트할 수 없다: {GRAPHIO_SRC} ({e})\n"
            "  형제 디렉터리로 clone 돼 있어야 한다 — 임계 사본을 대조할 "
            "방법이 이 테스트뿐이다 (skip 으로 바꾸지 않는다)") from e
    return geo_pattern


def _pair(radius_km: float) -> list[tuple[float, float]]:
    """중심에서 정확히 `radius_km` 떨어진 두 점. `geo_pattern` 의 반경은
    "중심에서 가장 먼 점까지"이므로 이 모양이 임계를 정면으로 찌른다."""
    d = radius_km / 111.32
    return [(37.5 - d, 127.0), (37.5 + d, 127.0)]


def test_graphio_still_uses_the_thresholds_topo_copied():
    """`topo.GEO_POINT_KM`·`GEO_CLUSTER_KM` 가 graphio 의 실제 경계와 같은가.

    **값을 읽어 비교하지 않고 행동으로 확인한다** — graphio 쪽은 함수 안의
    리터럴(`detector.py:1512`·`1533`)이라 임포트할 이름이 없다. 경계 양옆에
    점을 놓아 패턴이 바뀌는 자리를 찾는 것이 사본 검증의 유일한 방법이다.
    """
    geo_pattern = _geo_pattern()
    eps = 0.02
    assert geo_pattern(_pair(topo.GEO_POINT_KM - eps))[0] == "POINT"
    assert geo_pattern(_pair(topo.GEO_POINT_KM + eps))[0] != "POINT"
    assert geo_pattern(_pair(topo.GEO_CLUSTER_KM - eps))[0] == "CLUSTER"
    assert geo_pattern(_pair(topo.GEO_CLUSTER_KM + eps))[0] != "CLUSTER"


def test_generated_cells_read_as_point_in_graphio():
    """`--spread-km` 기본값으로 만든 셀이 graphio 에서 `POINT` 로 읽히는가.

    `topo.pattern_of` 는 축약판이라 "생성 쪽이 스스로 채점"하는 것이고,
    정본과 어긋나면 의도한 시나리오가 안 나온다(`RAN_NODE_DOWN` 의
    `expect_geo`). 둘을 같은 좌표로 대조한다.
    """
    geo_pattern = _geo_pattern()
    rng = random.Random(20260903)
    for enb in range(30):
        pts = topo.scatter(35.0 + enb * 0.1, 127.0, 3, 0.35, rng)
        assert topo.pattern_of(pts)[0] == "POINT"
        assert geo_pattern(pts)[0] == "POINT", (enb, pts)


# --------------------------------------------------------------------------
# T3 — `seed-enb` 는 그릴 수 있는 동에만 놓거나 명시적으로 비운다
# --------------------------------------------------------------------------


def _xw(tmp_path: Path, mapping: dict[str, str]) -> Path:
    p = tmp_path / "xw.json"
    p.write_text(json.dumps({"map": {"emd": mapping}}), encoding="utf-8")
    return p


def test_seeded_cells_land_in_a_drawable_dong_or_are_left_unmapped(tmp_path):
    """`DONG_ID` 를 **0 으로 채우지 않는다**(graphio 불변규칙 17).

    `_filter_drawable` 은 `"<city>-<gu>-<dong>"` 이 crosswalk 에 없는 셀을
    버리는데, `CITY_ID IS NULL` 인 셀은 **통과시킨다**(미매핑으로 남긴다).
    그래서 그릴 수 있는 동이 없는 시군구는 `0` 이 아니라 `NULL` 이 정답이다 —
    `0` 이면 조용히 버려져 "그 지역에 트래픽이 없다"로만 보이고, `NULL`
    이면 `H-07`·UNMAPPED 패널에 드러난다(불변규칙 12).

    `DONG_ID` 는 `NOT NULL` 이라 그 행은 적재에서 빠진다. **빠진 수를
    돌려주는 것까지가 계약이다** — 조용히 사라지면 54곳이 안 보인다.
    """
    xw = _xw(tmp_path, {"1": "41-41110-1101053", "2": "41-41110-1101054"})
    conn = FakeConn(
        ddl=LOC_DDL + MAP_DDL,
        gus=[(41, 41110, "가", 37.50, 127.00),      # drawable 동 2개
             (41, 41111, "나", 37.60, 127.10)],     # drawable 동 0개
    )
    r = topo.seed_enb(conn, xw, per_gu=1, cells_per_enb=3, dry_run=False)

    assert r["gus_without_drawable_dong"] == [41111]
    assert r["skipped_no_dong"] == 3        # 나 시군구의 셀 3개
    rows = _inserted_rows(conn, "CM_CELL_REP_RU_LOC")
    assert len(rows) == 3
    for row in rows:
        assert (int(row["CITY_ID"]), int(row["GU_ID"])) == (41, 41110)
        assert int(row["DONG_ID"]) in (1101053, 1101054)
    # 적재된 셀 키와 `CL_CELL_MAP` 키가 **같아야** 한다 — 어긋나면 `load_cells`
    # 의 LEFT JOIN 에서 좌표 없는 셀이 되어 미매핑으로 세어진다
    assert (_inserted_keys(conn, "CM_CELL_REP_RU_LOC")
            == _inserted_keys(conn, "CL_CELL_MAP"))


def test_seeded_cells_spread_over_the_drawable_dongs(tmp_path):
    """한 동에 몰아넣지 않는다 — 읍면동 드릴다운이 거짓을 말한다(불변규칙 18)."""
    xw = _xw(tmp_path, {str(i): f"41-41110-110105{i}" for i in range(3)})
    conn = FakeConn(ddl=LOC_DDL + MAP_DDL,
                    gus=[(41, 41110, "가", 37.5, 127.0)])
    topo.seed_enb(conn, xw, per_gu=1, cells_per_enb=3, dry_run=False)
    dongs = {int(r["DONG_ID"]) for r in _inserted_rows(conn, "CM_CELL_REP_RU_LOC")}
    assert len(dongs) == 3


def test_drawable_dongs_refuses_to_guess(tmp_path):
    """crosswalk 가 없거나 비면 **멈춘다.** 추측으로 집합을 만들면 지도가
    어긋나고 어디서 어긋났는지 알 방법이 없다(`load_drawable_dongs` 와 같은 선택)."""
    with pytest.raises(RuntimeError):
        topo.drawable_dongs(tmp_path / "없는파일.json")
    with pytest.raises(RuntimeError):
        topo.drawable_dongs(_xw(tmp_path, {}))


# --------------------------------------------------------------------------
# T4 — 파괴적 경로는 복귀 사본을 선행 조건으로 요구한다
# --------------------------------------------------------------------------


def test_dump_exists_needs_all_four_files(tmp_path, monkeypatch):
    """`dump_exists` 는 **네 파일 전부**를 봐야 한다 — 하나라도 없으면 복귀가
    반쪽이고, 반쪽 복귀는 "복구했다"고 믿는 상태라 더 나쁘다.

    **이 함수는 게이트가 아니다**(2026-10-26). 파일의 존재는 복귀를 뜻하지
    않아서 게이트는 `restore(dry_run=True)` 로 옮겼다 — 아래
    `test_gate_stops_when_the_copy_is_absent_or_half` 가 그쪽을 지킨다.
    여기서는 **화면 표시가 반쪽 사본을 "있다"로 말하지 않는 것**만 본다.

    파일 이름은 `dump_path()` 에서 받는다. 테스트가 이름을 직접 조립하면
    확장자를 바꿀 때 **테스트만 통과하는** 상태가 된다.
    """
    monkeypatch.setattr(topo, "LOCALE_DIR", tmp_path)
    assert topo.dump_exists("kr") is False
    (tmp_path / "kr").mkdir()
    keys = list(topo.DUMP_TABLES)
    for i, key in enumerate(keys):
        topo.dump_path("kr", key).write_bytes(b"")
        assert topo.dump_exists("kr") is (i == len(keys) - 1)


def test_dump_covers_every_table_topo_can_destroy():
    """`rm --all` 로 지울 수 있는 테이블이 `DUMP_TABLES` 에 다 있는가.

    `cellmap`(167MB)은 **일부러 빠져 있다** — 좌표가 없어 전환 대상이 아니고
    떠내면 사본이 쓸 수 없는 크기가 된다. 그래서 `rm --all cellmap` 은
    "사본이 있다"는 보장 없이 돌 수 있다. 그 예외를 테스트가 알고 있어야
    나중에 조용히 다른 테이블이 같은 구멍으로 들어가지 않는다.
    """
    assert set(topo.DUMP_TABLES) == set(topo.TABLES) - {"cellmap"}


# --------------------------------------------------------------------------
# 네 번째 단정 — `topo` 대역이 **한도 밖에서** 선택되고, 기존 선택은 안 바뀐다
# --------------------------------------------------------------------------


@dataclass
class _C:
    """`pick_report` 가 읽는 `Cell` 필드 **셋만** 갖는 대역품.

    `Cell` 은 필드가 25개이고 전부 채우면 테스트가 생성기 스키마에 묶인다.
    `pick_report` 가 보는 것은 이 셋뿐이다 — 늘어나면 여기서 `TypeError`
    가 나므로 조용히 어긋나지 않는다.
    """
    enb_id: str
    cell_id: int
    geo_status: str


def _truth_cell_keys() -> list[tuple[str, int]]:
    """`ground_truth.json` 의 정답 셀 키. 없으면 같은 모양의 합성 40개.

    **건너뛰지 않는다.** 그 파일은 `pemfill` 이 만드는 산출물이고 `data/` 는
    추적 밖이라 환경마다 있고 없다. `skip` 으로 두면 "정답이 유지되는가"를
    아무도 검증하지 않는 상태가 되므로, 없으면 합성 키로라도 **같은 성질**을
    단정한다 — 검증하려는 것은 특정 셀 값이 아니라 "한도 밖 추가가 기존
    선택을 건드리지 않는다"는 성질이다.
    """
    truth = GRAPHIO_SRC.parent / "data" / "truth" / "ground_truth.json"
    if truth.exists():
        keys: list[tuple[str, int]] = []
        for sc in json.loads(truth.read_text(encoding="utf-8"))["scenarios"]:
            for ck in (sc.get("cells") or []):
                enb, _, cell = str(ck).rpartition("-")
                if enb and cell.isdigit():
                    keys.append((enb, int(cell)))
        if keys:
            return sorted(set(keys))
    return [(f"1000{i // 8:02d}", i % 8) for i in range(40)]


def _uniq(cells) -> dict:
    """`_pick_enbs` 가 받는 모양. 그 함수는 인덱스 5(`CITY_ID`)만 본다."""
    return {(c.enb_id, c.cell_id): (None,) * 5 + (
        0 if c.geo_status == "MAPPED" else None,) for c in cells}


def _pool(truth_keys, *, band: dict[str, int], band_unmapped=()):
    """정답 셀 + 채움 셀 + (선택) `topo` 대역 셀로 후보 풀을 만든다.

    정답 셀의 eNB 에 **셀을 많이** 달아 셀 수 내림차순 상위에 오게 한다 —
    그래야 한도 60 에서 선택되고, "한도 밖 추가가 그 선택을 안 건드린다"를
    물을 수 있다. 미매핑 eNB 도 둔다(`_pick_enbs` 가 5% 를 그쪽에 쓴다).
    """
    cells = [_C(e, c, "MAPPED") for e, c in truth_keys]
    truth_enbs = {e for e, _ in truth_keys}
    for e in truth_enbs:                      # 상위로 밀어 올리는 채움 셀
        cells += [_C(e, 900 + i, "MAPPED") for i in range(30)]
    for i in range(400):                      # 꼬리 — 셀 2~5개짜리 일반 eNB
        cells += [_C(f"2{i:05d}", j, "MAPPED") for j in range(2 + i % 4)]
    for i in range(20):                       # 좌표 없는 eNB
        cells += [_C(f"3{i:05d}", j, "UNMAPPED") for j in range(10)]
    for enb, n in band.items():
        cells += [_C(enb, j, "MAPPED") for j in range(n)]
    # 좌표 없는 대역 eNB — **`topo add cell` 로 좌표 없이 행을 넣을 수 있다**
    # (`seed-enb` 는 항상 좌표를 쓴다). 그러면 그 eNB 가 `unmapped` 목록에
    # 들어가 **`n_un` 칸(한도의 5%)을 먹고** 기존 미매핑 eNB 를 밀어낸다 —
    # `H-07` 과 UNMAPPED 패널이 보던 셀이 바뀌는데 에러는 나지 않는다.
    # 아래 채움 eNB 보다 셀을 많이 줘야 그 자리를 실제로 다투게 된다
    # (`--cells` 가 CLI 플래그라 3 보다 큰 값이 들어올 수 있다).
    for enb in band_unmapped:
        cells += [_C(enb, j, "UNMAPPED") for j in range(12)]
    return _uniq(cells)


BAND = {str(topo.ENB_BASE + 41110): 3, str(topo.ENB_BASE + 41130): 3,
        str(topo.ENB_BASE + 11110): 3}


def test_topo_band_is_selected_even_at_enb_limit_one():
    """**`enb_limit` 과 무관하게 포함된다.**

    실측으로 `seed-enb` 의 3셀 eNB 는 셀 수 내림차순 **22,557등**이고 한도로
    끌어올리려면 **23,586** 이 필요했다 — 후보 mapped eNB 가 22,722개이므로
    그것은 한도가 아니라 **해제**다. 그래서 선택은 한도 축이 아니라 include
    집합으로 한다. 한도 1 에서도 들어가야 그 성질이 증명된다.
    """
    from datagen.pemdb1 import _pick_enbs

    uniq = _pool(_truth_cell_keys(), band=BAND)
    for limit in (1, 2, 60, 333):
        picked = _pick_enbs(uniq, limit)
        assert set(BAND) <= picked, (limit, sorted(set(BAND) - picked))


def test_topo_band_does_not_change_the_existing_selection():
    """**한도 밖 추가다** — 대역 eNB 가 한도 칸을 먹으면 안 된다.

    먹으면 기존 eNB 가 밀려나고 `ground_truth.json` 의 정답 셀 키가 어긋난다.
    그 어긋남은 에러가 아니라 **채점 점수 하락으로만** 보여서, 원인을 룰이나
    가중치에서 찾게 된다. 대역을 넣기 전/후의 **비대역 선택이 같은 집합**인지
    단정한다 — `_pick_enbs` 가 대역을 정렬 목록에서 빼고 나중에 합치는 이유다.
    """
    from datagen.pemdb1 import _pick_enbs

    truth = _truth_cell_keys()
    without = _pool(truth, band={})
    band_un = str(topo.ENB_BASE + 48110)
    with_band = _pool(truth, band=BAND, band_unmapped=(band_un,))
    allband = set(BAND) | {band_un}
    for limit in (1, 5, 60, 333):
        a = _pick_enbs(without, limit)
        b = _pick_enbs(with_band, limit)
        assert a == b - allband, (limit, sorted(a ^ (b - allband)))
        assert allband <= b, (limit, sorted(allband - b))


def test_truth_cells_survive_the_band_include():
    """정답 셀이 **전부** 남는가. 40/40 이 이 변경의 합격선이다.

    `ground_truth.json` 이 있으면 그 키를, 없으면 같은 모양의 합성 키를 쓴다
    (`_truth_cell_keys` 참조 — 건너뛰지 않는다).
    """
    from datagen.pemdb1 import _pick_enbs

    truth = _truth_cell_keys()
    truth_enbs = {e for e, _ in truth}
    with_band = _pool(truth, band=BAND)
    picked = _pick_enbs(with_band, 60)
    assert truth_enbs <= picked, sorted(truth_enbs - picked)
    kept = [k for k in truth if k[0] in picked]
    assert len(kept) == len(truth)
    # 대역 eNB 는 정답에 들어 있지 않아야 한다 — 들어 있으면 "한도 밖
    # 추가"라는 전제 자체가 깨지고 위 두 테스트의 의미가 없어진다
    assert not (truth_enbs & set(BAND))


def test_topo_cells_get_a_lower_traffic_weight(monkeypatch):
    """커버리지는 받고 **기존 eNB 의 1분 창 표본은 지킨다**(불변규칙 5).

    대역을 한도 밖에서 전부 넣으므로, 같은 가중을 주면 분당 호 수가 그쪽으로
    나뉘어 기존 eNB 의 표본이 깎인다. 한도 333 실측에서 330.6 → 61.5 건/분이
    됐던 것과 같은 결의 희석이다.

    **배수만 다르고 분포는 같아야 한다.** 대역에 별도 해시를 쓰면 편중
    분포(0.25~4.0 로그 균등)가 깨져 "최소 표본이 실제로 걸리는지"를 확인할
    수 없게 된다(`_weight` 머리말). 그래서 대역 판정만 끈 값과 비교한다 —
    산식을 테스트에 복사하지 않는 방법이다.
    """
    from datagen import pemdb1 as P

    assert 0.0 < topo.TOPO_WEIGHT < 1.0
    for enb in BAND:
        got = P._weight(enb, 0)
        monkeypatch.setattr(P, "_in_topo_band", lambda e: False)
        plain = P._weight(enb, 0)
        monkeypatch.undo()
        assert got == pytest.approx(plain * topo.TOPO_WEIGHT)
        assert 0.25 <= plain <= 4.0
    # 기존 셀은 **손대지 않는다** — 배수가 비대역에 새면 전체 규모가 줄고
    # 그것은 "트래픽이 준 것"처럼 보여 원인을 생성기에서 찾게 된다
    for enb in ("100001", "135469", "299999"):
        assert 0.25 <= P._weight(enb, 0) <= 4.0


def test_new_band_is_recognised_only_inside_its_range():
    """대역 판정은 `ENB_ID` 하나로 한다 — 표식 컬럼을 만들면 상용 DDL 이
    바뀐다(`pemdb6` 가 그대로 운영이다)."""
    assert topo.in_new_band(topo.ENB_BASE) is True
    assert topo.in_new_band(str(topo.ENB_BASE + 49_720)) is True
    assert topo.in_new_band(topo.ENB_BASE - 1) is False
    assert topo.in_new_band(900_000) is False       # 이미 13,483행이 있다
    assert topo.in_new_band(None) is False
    assert topo.in_new_band("ENB-X") is False


def test_pick_report_reads_the_band_as_covered(monkeypatch):
    """`topo list` 가 보여주는 열이 **실제 선택 규칙**을 읽는가.

    `pick_report` 는 `_pick_enbs` 를 그대로 부른다(정렬 규칙을 복사하지
    않는다). include 가 들어간 뒤에는 한도 1 에서도 `band_missing` 이 비어야
    하고, 비지 않으면 그것은 "한도가 작다"가 아니라 **include 가 깨졌다**는
    신호다 — `cli._limit_advice` 가 그렇게 말한다.
    """
    from datagen import pemdb1 as P

    cells = [_C("100001", i, "MAPPED") for i in range(30)]
    cells += [_C("811110", i, "MAPPED") for i in range(3)]
    monkeypatch.setattr(P, "load_cells", lambda *a, **k: cells)
    monkeypatch.setattr(P, "load_drawable_dongs", lambda *a, **k: {"x"})
    rep = topo.pick_report(object(), None, 1)
    assert rep["band_master"] == 1 and rep["band_picked"] == 1
    assert rep["band_missing"] == []
    assert "required_limit" not in rep      # 더는 계산하지 않는다


# --------------------------------------------------------------------------
# 복귀 경로 — **파일의 존재는 복귀를 뜻하지 않는다**
# --------------------------------------------------------------------------


CITY_DDL = ("CITY_ID", "CITY_NAME", "LATITUDE", "LONGITUDE")
GU_DDL = ("CITY_ID", "GU_ID", "GU_NAME", "LATITUDE", "LONGITUDE", "MAP_CODE")
DONG_DDL = ("CITY_ID", "GU_ID", "DONG_ID", "DONG_NAME", "USE_YN")

#: 왕복이 지켜야 하는 세 성질을 한 행에 담았다 — `None`(모름) · 한글(`_ci`
#: 충돌 실측이 있던 `RU_NAME` 계열) · DMS 좌표 문자열.
ROUNDTRIP = {
    "city": [{"CITY_ID": 41, "CITY_NAME": "경기", "LATITUDE": "37:30.0/000",
              "LONGITUDE": None}],
    "gu": [{"CITY_ID": 41, "GU_ID": 41110, "GU_NAME": "부천시 원미구",
            "LATITUDE": None, "LONGITUDE": None, "MAP_CODE": None}],
    "dong": [{"CITY_ID": 41, "GU_ID": 41110, "DONG_ID": 1101053,
              "DONG_NAME": "누리동", "USE_YN": None}],
    "cell": [{"ENB_ID": "811110", "SECTOR": 0, "CITY_ID": 41, "GU_ID": 41110,
              "DONG_ID": 1101053, "LATITUDE": "36:37.15/60",
              "LONGITUDE": "127:16.44/420",
              "RU_NAME": "[L9]○○빌딩-MT", "RU_CUID": None,
              "FLAG": None, "DECODE": "811110"}],
}
DDLS = {"city": CITY_DDL, "gu": GU_DDL, "dong": DONG_DDL,
        "cell": LOC_DDL}


class _RestoreConn(FakeConn):
    """`restore` 가 던지는 질의 둘(DDL 컬럼 · 필수 컬럼)에 테이블별로 답한다."""

    def __init__(self, ddls, required=()):
        super().__init__()
        self.ddls = {t: set(c) for t, c in ddls.items()}
        self.required = set(required)
        self.deleted: list[str] = []

    def answer(self, sql, params):
        if "is_nullable" in sql:
            table = str(params[0])
            return [(c,) for c in sorted(self.required & self.ddls.get(table, set()))]
        if "information_schema.columns" in sql:
            return [(c,) for c in sorted(self.ddls.get(str(params[0]), set()))]
        if "DELETE FROM" in sql:
            self.deleted.append(sql)
            return []
        raise AssertionError(f"_RestoreConn 이 모르는 질의다: {sql}")


def _write_dump(tmp_path, monkeypatch, rows_by_key, keys=None):
    """`topo.dump` 와 **같은 직렬화**로 사본을 만든다. 경로는 `dump_path` 가 준다."""
    monkeypatch.setattr(topo, "LOCALE_DIR", tmp_path)
    (tmp_path / "kr").mkdir(parents=True, exist_ok=True)
    for key in (keys if keys is not None else topo.DUMP_TABLES):
        with gzip.open(topo.dump_path("kr", key), "wt", encoding="utf-8") as fh:
            json.dump(rows_by_key.get(key, []), fh,
                      ensure_ascii=False, default=str)


def _ddl_map():
    return {topo.TABLES[k][0]: DDLS[k] for k in topo.DUMP_TABLES}


def test_restore_roundtrip_keeps_nulls_hangul_and_every_column(tmp_path, monkeypatch):
    """ⓐ **`None` 이 `0`·`""` 로 바뀌지 않고, 한글이 깨지지 않고, 컬럼이 안 빠진다.**

    셋 다 조용히 틀리는 종류다 — `None` → `0` 은 "모름"을 "없음"으로 바꾸고
    (불변규칙 17), 한글 깨짐은 **성공한 INSERT** 로 돌아오고, 컬럼 누락은
    복구한 마스터가 원본과 다른데 에러가 없다.
    """
    _write_dump(tmp_path, monkeypatch, ROUNDTRIP)
    conn = _RestoreConn(_ddl_map())
    rep = topo.restore(conn, "kr", dry_run=False)
    assert rep["ok"] and not rep["dry_run"]

    for key in topo.DUMP_TABLES:
        table = topo.TABLES[key][0]
        got = _inserted_rows(conn, table)
        assert len(got) == 1, table
        want = ROUNDTRIP[key][0]
        # 컬럼이 하나도 빠지지 않는다 (DDL 교집합이 전부를 덮는다)
        assert set(got[0]) == set(want), (table, set(want) ^ set(got[0]))
        for col, v in want.items():
            if v is None:
                assert got[0][col] is None, (table, col, got[0][col])
            else:
                assert got[0][col] == v, (table, col)
    # 넷 다 비운 뒤 넣는다 — 한 테이블만 지우고 끝나면 반쪽 복구다
    assert len(conn.deleted) == len(topo.DUMP_TABLES)


def test_restore_dry_run_writes_nothing(tmp_path, monkeypatch):
    """게이트는 **읽어서 넣을 수 있는지**만 묻는다 — 쓰면 게이트가 아니다."""
    _write_dump(tmp_path, monkeypatch, ROUNDTRIP)
    conn = _RestoreConn(_ddl_map())
    rep = topo.restore(conn, "kr", dry_run=True)
    assert rep["ok"] is True
    assert not conn.inserted and not conn.deleted
    joined = " ".join(conn.sql).upper()
    assert "DELETE" not in joined and "INSERT" not in joined


def test_gate_stops_when_the_copy_is_absent_or_half(tmp_path, monkeypatch):
    """ⓑ 사본이 **없거나 반쪽이거나 읽히지 않으면** 게이트가 멈춘다.

    **파일의 존재는 복귀를 뜻하지 않는다.** 한동안 `dump_exists()` 가
    게이트였고 그때 되넣는 코드가 없었다 — 통과 조건이 "되돌릴 수 있다"가
    아니라 "파일 4개가 존재한다"였다. 그래서 세 상태를 각각 단정한다.
    """
    conn = _RestoreConn(_ddl_map())
    monkeypatch.setattr(topo, "LOCALE_DIR", tmp_path)
    assert topo.restore(conn, "kr", dry_run=True)["ok"] is False   # 없다

    # 반쪽 — 셋만 있다. `dump_exists` 도 False 지만 **사유를 말해야** 한다
    half = [k for k in topo.DUMP_TABLES if k != "cell"]
    _write_dump(tmp_path, monkeypatch, ROUNDTRIP, keys=half)
    rep = topo.restore(conn, "kr", dry_run=True)
    assert rep["ok"] is False
    assert rep["tables"]["cell"]["error"]
    assert not rep["tables"]["city"].get("error")
    assert topo.dump_exists("kr") is False

    # 파일은 있는데 gzip 이 깨졌다 — **`dump_exists` 는 True 인데 복귀는 불가**
    _write_dump(tmp_path, monkeypatch, ROUNDTRIP)
    topo.dump_path("kr", "cell").write_bytes(b"not a gzip stream")
    assert topo.dump_exists("kr") is True
    rep = topo.restore(conn, "kr", dry_run=True)
    assert rep["ok"] is False and rep["tables"]["cell"]["error"]


def test_gate_stops_when_a_required_column_is_missing(tmp_path, monkeypatch):
    """`NOT NULL` + 기본값 없음이 사본에 없으면 적재가 `1364` 로 깨진다 —
    **지우기 전에** 말해야 한다. `restore` 는 `wipe` 를 먼저 하는 구조다."""
    rows = {k: [dict(v[0])] for k, v in ROUNDTRIP.items()}
    del rows["cell"][0]["DONG_ID"]
    _write_dump(tmp_path, monkeypatch, rows)
    conn = _RestoreConn(_ddl_map(), required=("DONG_ID",))
    rep = topo.restore(conn, "kr", dry_run=True)
    assert rep["ok"] is False
    assert rep["tables"]["cell"]["missing_required"] == ["DONG_ID"]
    # 진행하라고 해도 **쓰지 않는다**
    assert topo.restore(conn, "kr", dry_run=False)["ok"] is False
    assert not conn.deleted and not conn.inserted


def test_cellmap_is_not_wipeable():
    """ⓒ `rm cellmap --all` 은 거부된다.

    `topo` 는 위치 마스터를 다루고 `CL_CELL_MAP` 574,404행은 **셀 대장**이다.
    전량 삭제할 이유가 없고 `DUMP_TABLES` 에도 없어서 지우면 되돌릴 원본이
    아예 없다 — 전에는 게이트가 `CM_*` 네 파일만 보고 이 삭제를 통과시켰다.
    단건(`add`·`rm --key`)은 계속 받는다.
    """
    assert "cellmap" in topo.TABLES          # 단건은 받는다
    assert "cellmap" not in topo.WIPEABLE    # 전체 삭제는 아니다
    assert set(topo.WIPEABLE) == set(topo.DUMP_TABLES)
    # 사본 없는 테이블을 지울 수 있는 길이 남아 있으면 안 된다
    assert set(topo.WIPEABLE) <= set(topo.DUMP_TABLES)


def test_dump_is_gzip_and_restore_reads_it(tmp_path, monkeypatch):
    """ⓓ 사본은 `.json.gz` 이고 `restore` 가 **그 이름으로** 찾는다.

    포맷을 복원 함수와 같은 시점에 정한 이유 — 나중에 바꾸면 이미 뜬 사본이
    복원 불가가 된다(`restore` 가 다른 이름을 본다). 그래서 경로는
    `dump_path()` 한 자리에서만 나온다.
    """
    assert topo.DUMP_SUFFIX == ".json.gz"
    _write_dump(tmp_path, monkeypatch, ROUNDTRIP)
    for key in topo.DUMP_TABLES:
        path = topo.dump_path("kr", key)
        assert path.name.endswith(".json.gz")
        assert path.read_bytes()[:2] == b"\x1f\x8b"      # gzip 매직
    conn = _RestoreConn(_ddl_map())
    assert topo.restore(conn, "kr", dry_run=True)["ok"] is True
    # 비압축 `.json` 만 있으면 **복귀 불가**여야 한다 — 조용히 받으면
    # 포맷이 둘이 되고 어느 쪽이 정본인지 알 수 없다
    for key in topo.DUMP_TABLES:
        p = topo.dump_path("kr", key)
        p.rename(p.with_suffix(""))          # .json.gz → .json
    assert topo.restore(conn, "kr", dry_run=True)["ok"] is False

def test_restore_says_what_it_does_not_restore(tmp_path, monkeypatch):
    """**되돌렸다고 읽히면 안 된다.**

    `restore` 는 DB 네 테이블만 복구하고 `data/geo/` 를 보지 않는다 —
    `code_crosswalk.json` 은 이 경로 밖이다. 그리고 `CL_CELL_MAP` 은
    `DUMP_TABLES` 밖이라 사본이 아예 없다. 그 둘을 **반환값이 말해야**
    CLI·UI 가 사람에게 내릴 수 있다(불변규칙 17 의 결).
    """
    _write_dump(tmp_path, monkeypatch, ROUNDTRIP)
    rep = topo.restore(_RestoreConn(_ddl_map()), "kr", dry_run=True)
    assert rep["ok"] is True
    assert rep["scope"] == [topo.TABLES[k][0] for k in topo.DUMP_TABLES]
    joined = " ".join(rep["not_restored"])
    assert topo.XW_FILE in joined
    assert "CL_CELL_MAP" in joined
    # 범위에 그 둘이 **들어가 있으면** 안 된다 — 그러면 두 말이 모순이다
    assert topo.TABLES["cellmap"][0] not in rep["scope"]
