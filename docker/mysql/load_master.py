"""상용에서 받은 마스터 JSON → 로컬 pemdb1 적재.

생성기가 쓸 바탕이다. **좌표가 있는 셀만 넣는다** — 좌표 없는 셀은 지도·공간
군집·지리 패턴(EV-11)에서 쓸 수 없고, 57만 행을 다 넣으면 로컬에서 조회가
느려지기만 한다.

조인 키 주의 (실측으로 확인한 것):
  CM_CELL_REP_RU_LOC.DECODE  = 평문 ENB_ID   ← ENB_ID 컬럼은 암호문이다
  CM_CELL_REP_RU_LOC.SECTOR  = 실제로는 CELLNUM 값이다 (이름과 내용이 다르다)
  XDR.FIRST_CELL_ID          = CL_CELL_MAP.CELLNUM
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pymysql

HERE = Path(__file__).resolve().parents[2]
SRC = HERE / "data" / "pemdb1"

CFG = dict(host=os.environ.get("DB_HOST", "127.0.0.1"),
           port=int(os.environ.get("DB_PORT", "3306")),
           user=os.environ.get("DB_USER", "graphio"),
           password=os.environ.get("DB_PASSWORD", "graphio"),
           database=os.environ.get("DB_NAME", "pemdb1"),
           charset="utf8mb4", autocommit=False)


def rows_of(name: str) -> list[dict]:
    p = SRC / f"{name}.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []


def insert(conn, table: str, rows: list[dict], cols: list[str] | None = None) -> int:
    if not rows:
        return 0
    cols = cols or list(rows[0])
    marks = ",".join(["%s"] * len(cols))
    collist = ",".join(f"`{c}`" for c in cols)
    data = [tuple(r.get(c) for c in cols) for r in rows]
    with conn.cursor() as cur:
        cur.execute(f"DELETE FROM `{table}`")
        for i in range(0, len(data), 5000):
            cur.executemany(
                f"INSERT INTO `{table}` ({collist}) VALUES ({marks})", data[i:i + 5000])
    conn.commit()
    return len(data)


def main() -> int:
    conn = pymysql.connect(**CFG)
    try:
        # ── 셀 마스터 — 좌표가 있는 eNB 로 좁힌다
        geo = rows_of("CM_CELL_REP_RU_LOC")
        cmap = rows_of("CL_CELL_MAP")
        geo_enb = {str(r["DECODE"]) for r in geo if r.get("DECODE")}
        cells = [r for r in cmap if str(r.get("ENB_ID")) in geo_enb]
        print(f"  셀 마스터 — 전체 {len(cmap):,} → 좌표 보유 {len(cells):,} "
              f"(eNB {len(geo_enb & {str(r['ENB_ID']) for r in cmap})})")

        n = insert(conn, "CL_CELL_MAP", cells)
        print(f"  CL_CELL_MAP          {n:,}")

        # 좌표 테이블은 ENB_ID 가 암호문이다. 조인이 되도록 DECODE(평문)를
        # ENB_ID 자리에 넣어 적재한다 — 로컬에서는 암호문을 쓸 이유가 없고,
        # 이렇게 해야 CL_CELL_MAP.ENB_ID 와 바로 붙는다.
        for r in geo:
            if r.get("DECODE"):
                r["ENB_ID"] = str(r["DECODE"])
        n = insert(conn, "CM_CELL_REP_RU_LOC", geo)
        print(f"  CM_CELL_REP_RU_LOC   {n:,}  (ENB_ID ← DECODE 평문으로 치환)")

        for t in ("CM_CITY", "CM_GU", "CM_DONG", "CM_EPC_INFO", "CL_MME", "CL_PGW"):
            rows = rows_of(t)
            if not rows:
                print(f"  {t:20} 파일 없음 — 건너뜀")
                continue
            # DDL 에 없는 컬럼이 섞여 있으면 거부되므로 교집합만 넣는다
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema=%s AND table_name=%s", [CFG["database"], t])
                have = {r[0] for r in cur.fetchall()}
            cols = [c for c in rows[0] if c in have]
            print(f"  {t:20} {insert(conn, t, rows, cols):,}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
