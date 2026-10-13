#!/usr/bin/env python3
"""반출 JSON → 로컬 pemdb1 적재 (마스터·코드 테이블 32개).

**`load_master.py` 와 다른 물건이다.** 저쪽은 graphio 가 읽는 21테이블만
채운다. 이쪽은 `gen_master_ddl.py` 가 만든 나머지를 채운다 — 코드 정본
(`CD_CAUSE`·`CD_MESSAGE`·`CD_IF_PROTO_CEI`)과 조직 계층(`CM_REGION`·
`CM_BRANCH`·`CM_TEAM`·`CM_EMS`)이 여기 있다.

**행 수를 자동 대조한다.** 적재가 조용히 잘리는 쪽이 가장 위험하다 —
`CD_CAUSE` 가 절반만 들어가면 Cause 해석이 "미등록 코드"로 바뀌고, 그것은
에러가 아니라 빈 설명으로 나타난다 (불변규칙 17).
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import pymysql

DATA = Path(__file__).resolve().parents[2] / "data" / "pemdb1"
DDL = Path(__file__).resolve().parent / "pemdb1_masters.sql"

CONN = dict(
    host=os.environ.get("GP_MYSQL_HOST", "127.0.0.1"),
    port=int(os.environ.get("GP_MYSQL_PORT", "3306")),
    user=os.environ.get("GP_MYSQL_USER", "graphio"),
    password=os.environ.get("GP_MYSQL_PASSWORD", "graphio"),
    database=os.environ.get("GP_MYSQL_DB", "pemdb6"),
    charset="utf8mb4",
)

BATCH = 2000


def main() -> int:
    tables = re.findall(r"CREATE TABLE IF NOT EXISTS `(\w+)`", DDL.read_text())
    con = pymysql.connect(**CONN)
    bad: list[str] = []
    try:
        with con.cursor() as cur:
            # DDL 을 문장 단위로 쪼개 올린다. pymysql 은 멀티 문장을 막는다.
            for stmt in DDL.read_text().split(";"):
                if stmt.strip():
                    cur.execute(stmt)
            con.commit()
            print(f"DDL {len(tables)}개 적용\n")

            for t in tables:
                rows = json.loads((DATA / f"{t}.json").read_text())
                cols = re.findall(
                    rf"CREATE TABLE IF NOT EXISTS `{t}` \((.*?)\n\) ENGINE",
                    DDL.read_text(), re.S)
                cols = re.findall(r"`(\w+)`", cols[0])

                cur.execute(f"TRUNCATE TABLE `{t}`")
                ph = ",".join(["%s"] * len(cols))
                sql = (f"INSERT INTO `{t}` "
                       f"({','.join(f'`{c}`' for c in cols)}) VALUES ({ph})")
                for i in range(0, len(rows), BATCH):
                    cur.executemany(sql, [
                        tuple(r.get(c) for c in cols) for r in rows[i:i + BATCH]
                    ])
                con.commit()

                cur.execute(f"SELECT COUNT(*) FROM `{t}`")
                got = cur.fetchone()[0]
                ok = got == len(rows)
                if not ok:
                    bad.append(f"{t}: 파일 {len(rows)} → DB {got}")
                print(f"  {'OK ' if ok else '불일치'} {t:<26} "
                      f"{got:>7,}행 / 파일 {len(rows):>7,}행 · {len(cols)}컬럼")
    finally:
        con.close()

    if bad:
        print("\n행 수가 맞지 않는다 — 적재가 잘렸다:")
        for b in bad:
            print(f"  {b}")
        return 1
    print(f"\n{len(tables)}개 테이블 전부 행 수 일치")
    return 0


if __name__ == "__main__":
    sys.exit(main())
