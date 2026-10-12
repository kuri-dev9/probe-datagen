"""내부 DuckDB 창고 → MySQL 적재.

생성·집계가 끝난 분의 결과(kpi_*_min 등)를 MySQL 로 밀어낸다. graphio 는 이
MySQL 만 읽는다 — 외부로 나가는 유일한 출구다.

적재는 **구간 멱등**이다: 같은 분을 다시 보내도 DELETE→INSERT 라 중복이 없다
(graphio 불변규칙·재처리 멱등성과 같은 규약). 스키마는 MySQL 쪽(로컬은
docker/mysql/init.sql, 운영은 DB 담당)이 만든다 — 여기서는 만들지 않는다.
"""

from __future__ import annotations

from datetime import datetime

import pymysql

#: 분 단위 적재 대상 — slot_ts 로 구간 삭제가 가능한 테이블
SLOT_TABLES = (
    "kpi_cell_min", "kpi_node_min", "kpi_ue_min", "kpi_apn_min",
    "cause_min", "kpi_u_cell_min", "kpi_domain_min", "health_min", "file_ledger",
)
#: 한 번만 적재하는 마스터 (전량 교체)
MASTER_TABLES = ("cell_master", "core_node")


class MysqlWriter:
    def __init__(self, host, port, user, password, database,
                 charset="utf8mb4"):
        self.conn = pymysql.connect(
            host=host, port=int(port), user=user, password=password,
            database=database, charset=charset, autocommit=False,
        )

    def _cols(self, duck, table: str) -> list[str]:
        return [r[0] for r in duck.execute(
            f"SELECT column_name FROM information_schema.columns "
            f"WHERE table_name = '{table}' ORDER BY ordinal_position"
        ).fetchall()]

    def _copy(self, duck, table: str, duck_where: str, my_where: str,
              params: list) -> int:
        cols = self._cols(duck, table)
        duck_cols = ",".join(f'"{c}"' for c in cols)      # DuckDB 인용
        my_cols = ",".join(f"`{c}`" for c in cols)        # MySQL 인용
        # DuckDB SELECT 는 ? 플레이스홀더, MySQL DELETE 는 %s — 엔진별로 받는다
        rows = duck.execute(
            f"SELECT {duck_cols} FROM {table}"
            + (f" WHERE {duck_where}" if duck_where else ""), params if duck_where else []
        ).fetchall()
        with self.conn.cursor() as cur:
            if my_where:
                cur.execute(f"DELETE FROM `{table}` WHERE {my_where}", params)
            else:
                cur.execute(f"DELETE FROM `{table}`")
            if rows:
                marks = ",".join(["%s"] * len(cols))
                cur.executemany(
                    f"INSERT INTO `{table}` ({my_cols}) VALUES ({marks})", rows
                )
        return len(rows)

    def write_masters(self, duck) -> dict:
        out = {}
        for t in MASTER_TABLES:
            out[t] = self._copy(duck, t, "", "", [])
        self.conn.commit()
        return out

    def write_slot(self, duck, lo: datetime, hi: datetime) -> dict:
        """[lo, hi) 구간의 집계 결과를 멱등 적재한다."""
        out = {}
        for t in SLOT_TABLES:
            out[t] = self._copy(
                duck, t,
                "slot_ts >= ? AND slot_ts < ?",
                "slot_ts >= %s AND slot_ts < %s",
                [lo, hi],
            )
        self.conn.commit()
        return out

    def last_slot(self, table: str = "kpi_cell_min") -> datetime | None:
        with self.conn.cursor() as cur:
            cur.execute(f"SELECT MAX(slot_ts) FROM `{table}`")
            return cur.fetchone()[0]

    def close(self):
        self.conn.close()
