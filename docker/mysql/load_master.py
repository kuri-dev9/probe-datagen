"""상용에서 받은 마스터 JSON → 로컬 pemdb1 적재.

생성기가 쓸 바탕이다. **좌표가 있는 셀만 넣는다** — 좌표 없는 셀은 지도·공간
군집·지리 패턴(EV-11)에서 쓸 수 없고, 57만 행을 다 넣으면 로컬에서 조회가
느려지기만 한다.

조인 키 주의 (실측으로 확인한 것):
  셀·노드 키는 **전부 평문**이다 — `CL_CELL_MAP.ENB_ID` · `CL_ENB_ORG.ENB_ID` ·
  `CM_CELL_REP_RU_LOC.ENB_ID` 가 같은 평문 숫자 체계다.
  `CM_CELL_REP_RU_LOC.ENB_ID` 는 원래 base64 암호문이었고, 반출 JSON 쪽을
  `normalize_enb_key.py` 로 평문(`DECODE` 값)으로 통일했다. 암호문은 같은 행의
  `ENB_ID_ENC` 에 남아 있다 — **DDL 에 없는 컬럼이라 DB 에는 안 들어간다**
  (아래 `insert` 가 DDL 교집합만 넣는다). 상용 테이블 구조를 로컬에서 바꾸지
  않으려는 것이다.
  `CM_CELL_REP_RU_LOC.SECTOR`  = 실제로는 CELLNUM 값이다 (이름과 내용이 다르다)
  `XDR.FIRST_CELL_ID`          = CL_CELL_MAP.CELLNUM

가입자 식별자(`IMSI`·`IMEI`·`mdn`·`SPID`·`SERVICECODE`)는 **암호문 그대로 둔다.**
복호화하지 않고 집계·조인한다 (graphio 불변규칙 14).
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import pymysql

HERE = Path(__file__).resolve().parents[2]
SRC = HERE / "data" / "pemdb1"

#: 암호문 판정 — 반출본이 구 세대인지 보려는 것뿐이다.
B64 = re.compile(r"^[A-Za-z0-9+/]{16,}={0,2}$")

CFG = dict(host=os.environ.get("DB_HOST", "127.0.0.1"),
           port=int(os.environ.get("DB_PORT", "3306")),
           user=os.environ.get("DB_USER", "graphio"),
           password=os.environ.get("DB_PASSWORD", "graphio"),
           database=os.environ.get("DB_NAME", "pemdb1"),
           # **charset 을 명시한다.** 어긋나면 한글이 '?' 로 바뀌어 들어가고,
           # 그것은 에러가 아니라 성공한 INSERT 로 돌아온다.
           charset="utf8mb4", autocommit=False)


def rows_of(name: str) -> list[dict]:
    p = SRC / f"{name}.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []


def ddl_columns(conn, table: str) -> set[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema=%s AND table_name=%s",
                    [CFG["database"], table])
        return {r[0] for r in cur.fetchall()}


def insert(conn, table: str, rows: list[dict]) -> int:
    """JSON 행 → 테이블. **DDL 에 있는 컬럼만 넣는다.**

    반출 JSON 에는 DDL 에 없는 컬럼이 섞여 있다(`ENB_ID_ENC` — 평문 통일 뒤에도
    남긴 암호문). 그대로 INSERT 하면 `Unknown column` 으로 적재가 통째로
    깨지고, DDL 에 컬럼을 더하면 로컬이 상용과 다른 구조가 되어 두 환경을
    대조할 수 없다. 그래서 **적재기가 걸러낸다.**
    """
    if not rows:
        return 0
    have = ddl_columns(conn, table)
    cols = [c for c in rows[0] if c in have]
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

        # **구 세대 반출본을 조용히 받지 않는다.** 이 컬럼이 암호문이면
        # `CL_CELL_MAP.ENB_ID`(평문)와 짝이 0건이 되고, 아래 좌표 필터가
        # 모든 셀을 버려 `CL_CELL_MAP` 이 0행으로 적재된다 — 에러 없이.
        if geo and B64.match(str(geo[0].get("ENB_ID") or "")):
            print("CM_CELL_REP_RU_LOC.json 의 ENB_ID 가 암호문이다 — 평문으로 통일해라:\n"
                  "  python3 docker/mysql/normalize_enb_key.py", file=sys.stderr)
            return 1

        geo_enb = {str(r["ENB_ID"]) for r in geo if r.get("ENB_ID")}
        cells = [r for r in cmap if str(r.get("ENB_ID")) in geo_enb]
        print(f"  셀 마스터 — 전체 {len(cmap):,} → 좌표 보유 {len(cells):,} "
              f"(eNB {len(geo_enb & {str(r['ENB_ID']) for r in cmap})})")

        n = insert(conn, "CL_CELL_MAP", cells)
        print(f"  CL_CELL_MAP          {n:,}")

        n = insert(conn, "CM_CELL_REP_RU_LOC", geo)
        print(f"  CM_CELL_REP_RU_LOC   {n:,}  (ENB_ID 평문 · ENB_ID_ENC 는 DDL 에 없어 제외)")

        for t in ("CM_CITY", "CM_GU", "CM_DONG", "CM_EPC_INFO", "CL_MME", "CL_PGW"):
            rows = rows_of(t)
            if not rows:
                print(f"  {t:20} 파일 없음 — 건너뜀")
                continue
            print(f"  {t:20} {insert(conn, t, rows):,}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
