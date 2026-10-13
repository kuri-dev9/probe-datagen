#!/usr/bin/env python3
"""`CL_SGW` 생성·적재 — 상용 추출본에 없던 테이블.

## 왜 필요한가

`CL_MME`(56행)·`CL_PGW`(169행)는 받았는데 **SGW 등록부만 빠졌다.** 그래서 노드
화면에서 SGW 86대가 권역·제조사 없이 ID 로만 나왔고, `CM_EPC_INFO` 에도 없는
SGW 23대는 `unregistered_nodes` 로 떨어졌다.

컬럼 구성은 사람이 운영 원본을 보고 준 것이고(2026-10-13), **타입은 `CL_PGW` 의
같은 이름 컬럼에서 가져왔다.** DDL 은 `pemdb1_local.sql` 에 있고 거기에 추론한
자리를 표시해 두었다.

## ID 집합 — 합집합을 쓴다

    CM_EPC_INFO(EQUIP_TYPE='SGW')   86
    XDR_LTE_CALL_KPI.SGW_ID distinct 47
    교집합                           24
    합집합                          109   ← 이것을 쓴다

둘 중 하나만 쓰면 안 된다. `CM_EPC_INFO` 만 보면 **XDR 에 실제로 트래픽이 흐르는
SGW 23대가 등록부에 없고**, XDR 만 보면 트래픽이 없는 시점의 장비 62대가 빠진다.
등록부는 "존재하는 장비" 목록이므로 둘을 합쳐야 맞다.

## 속성값은 `CL_PGW` 의 실측 분포를 따른다

사람 지시가 "pgw 확인해보고 유사하게"였다. 실측 분포(169행):

    BRANCH_ID     1500 전부
    LAREA_ID      1000×93 · 1401×48 · NULL×27 · 1100×1
    MTSO_ID       7×87 · 11×72 · 2×8 · 5×1 · 3×1
    MTSO_TEAM_ID  1503×82 · 1501×63 · 1504×17 · 1502×7
    VENDOR_ID     25×75 · 27×67 · NULL×27
    OPR_STATUS    ACT×126 · DEL×43
    EMS_ID        NULL×27 · 0006×15 · 0008×13 · 0054×11 · …

**NULL 비율까지 따른다.** 전부 채우면 "운영에서도 이만큼 채워져 있다"는 거짓
인상을 준다 — 실제로는 PGW 의 16%가 `LAREA_ID` 가 비어 있다(불변규칙 17).

**`MTSO_ID` 와 `LAREA_ID`·`MTSO_TEAM_ID` 는 함께 뽑는다.** 따로 뽑으면 실제로
짝을 이룬 적 없는 조합(예: 서울 교환국 + 충남 권역)이 나온다. PGW 에서 관측된
조합만 쓴다.

**결정론적이다.** `SGW_ID` 로 해시해 뽑으므로 다시 돌려도 같은 값이다 — 원장이
흔들리면 로컬과 반출본이 갈라진다.

    python3 seed_cl_sgw.py --dry-run
    python3 seed_cl_sgw.py
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

import pymysql

EXPORT = Path(__file__).resolve().parents[2] / "data" / "pemdb1" / "CL_SGW.json"

CONN = dict(
    host=os.environ.get("GP_MYSQL_HOST", "127.0.0.1"),
    port=int(os.environ.get("GP_MYSQL_PORT", "3306")),
    user=os.environ.get("GP_MYSQL_USER", "graphio"),
    password=os.environ.get("GP_MYSQL_PASSWORD", "graphio"),
    database=os.environ.get("GP_MYSQL_DB", "pemdb1"),
    charset="utf8mb4",
)

COLS = ("SGW_ID", "OPR_STATUS", "BRANCH_ID", "LAREA_ID", "MTSO_ID", "MTSO_TEAM_ID",
        "VENDOR_ID", "IP", "MYIPID", "NAME", "CAPA", "GRP", "S5S8", "STATUS",
        "INHEMSC", "LEFT_TIME", "INSTALL_DATE", "UPDATE_DATE", "EMS_ID")


def pick(key: str, salt: str, weighted: list[tuple[object, int]]):
    """가중 분포에서 결정론적으로 하나 고른다."""
    total = sum(w for _, w in weighted)
    h = int(hashlib.sha256(f"{key}:{salt}".encode()).hexdigest(), 16) % total
    for v, w in weighted:
        if h < w:
            return v
        h -= w
    return weighted[-1][0]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    con = pymysql.connect(**CONN, cursorclass=pymysql.cursors.DictCursor)
    try:
        cur = con.cursor()

        # ── ID 집합 — 합집합 ───────────────────────────────────────────────
        cur.execute("""SELECT EQUIP_ID AS id, EQUIP_NAME AS nm, NETWORK_TYPE AS nt
                         FROM CM_EPC_INFO WHERE EQUIP_TYPE='SGW'""")
        epc = {r["id"]: r for r in cur.fetchall()}
        cur.execute("SELECT DISTINCT SGW_ID AS id FROM XDR_LTE_CALL_KPI WHERE SGW_ID IS NOT NULL")
        xdr = {r["id"] for r in cur.fetchall()}
        ids = sorted(set(epc) | xdr)
        print(f"ID — CM_EPC_INFO {len(epc)} · XDR {len(xdr)} · 교집합 "
              f"{len(set(epc) & xdr)} · 합집합 {len(ids)}")

        # ── CL_PGW 실측 분포 — 조합은 묶어서 뽑는다 ───────────────────────
        cur.execute("""SELECT MTSO_ID, MTSO_TEAM_ID, LAREA_ID, COUNT(*) n
                         FROM CL_PGW GROUP BY 1,2,3""")
        sites = [((r["MTSO_ID"], r["MTSO_TEAM_ID"], r["LAREA_ID"]), r["n"])
                 for r in cur.fetchall()]
        cur.execute("SELECT VENDOR_ID v, COUNT(*) n FROM CL_PGW GROUP BY 1")
        vendors = [(r["v"], r["n"]) for r in cur.fetchall()]
        cur.execute("SELECT OPR_STATUS s, COUNT(*) n FROM CL_PGW GROUP BY 1")
        statuses = [(r["s"], r["n"]) for r in cur.fetchall()]
        cur.execute("SELECT EMS_ID e, COUNT(*) n FROM CL_PGW GROUP BY 1")
        emss = [(r["e"], r["n"]) for r in cur.fetchall()]
        cur.execute("SELECT BRANCH_ID b, COUNT(*) n FROM CL_PGW GROUP BY 1")
        branches = [(r["b"], r["n"]) for r in cur.fetchall()]
        print(f"분포 — 국소 조합 {len(sites)} · 제조사 {len(vendors)} · "
              f"상태 {len(statuses)} · EMS {len(emss)}")

        rows = []
        for sid in ids:
            mtso, team, larea = pick(sid, "site", sites)
            info = epc.get(sid) or {}
            # `CM_EPC_INFO` 에 있으면 그 이름을 쓴다 — 지어내지 않는다.
            # 없으면 PGW 관례(`PGW_01`)를 따라 `SGW_<ID>` 로 만든다.
            name = info.get("nm") or f"SGW_{sid}"
            rows.append({
                "SGW_ID": sid,
                "OPR_STATUS": pick(sid, "opr", statuses),
                "BRANCH_ID": pick(sid, "branch", branches),
                "LAREA_ID": larea,
                "MTSO_ID": mtso,
                "MTSO_TEAM_ID": team,
                "VENDOR_ID": pick(sid, "vendor", vendors),
                # `CL_PGW` 는 `IP`·`MYIPID`·`CAPA` 가 거의 전부 NULL 이다.
                # 없는 값을 지어내지 않는다(불변규칙 17).
                "IP": None,
                "MYIPID": None,
                "NAME": name,
                "CAPA": None,
                "GRP": None,
                "S5S8": pick(sid, "s5s8", [("ALL", 3), (None, 7)]),
                "STATUS": None,
                "INHEMSC": pick(sid, "inh", [("ON", 2), ("OFF", 1), (None, 7)]),
                "LEFT_TIME": None,
                "INSTALL_DATE": None,
                "UPDATE_DATE": None,
                "EMS_ID": pick(sid, "ems", emss),
            })

        from_epc = sum(1 for r in rows if r["SGW_ID"] in epc)
        print(f"\n행 {len(rows)} — 이름 출처: CM_EPC_INFO {from_epc} · "
              f"생성 {len(rows) - from_epc}")
        for r in rows[:4]:
            print("  ", {k: r[k] for k in ("SGW_ID", "NAME", "OPR_STATUS",
                                           "LAREA_ID", "MTSO_ID", "VENDOR_ID", "EMS_ID")})
        if args.dry_run:
            print("\n--dry-run — 아무것도 바꾸지 않았다.")
            return 0

        # ── 적재 — 멱등 ───────────────────────────────────────────────────
        with con.cursor() as c2:
            c2.execute("SELECT COUNT(*) AS n FROM information_schema.tables "
                       "WHERE TABLE_SCHEMA=%s AND TABLE_NAME='CL_SGW'", (CONN["database"],))
            if not c2.fetchone()["n"]:
                print("CL_SGW 테이블이 없다 — pemdb1_local.sql 을 먼저 적용해라",
                      file=sys.stderr)
                return 1
            c2.execute("DELETE FROM CL_SGW")
            ph = ", ".join(["%s"] * len(COLS))
            c2.executemany(
                f"INSERT INTO CL_SGW ({', '.join(f'`{c}`' for c in COLS)}) VALUES ({ph})",
                [tuple(r[c] for c in COLS) for r in rows])
            n = c2.rowcount
        con.commit()
        print(f"CL_SGW 적재 {n}행")

        EXPORT.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"반출본 → {EXPORT.name}")

        # ── 검증 ──────────────────────────────────────────────────────────
        cur.execute("""SELECT COUNT(*) AS n,
                              COUNT(DISTINCT SGW_ID) AS d,
                              SUM(LAREA_ID IS NULL) AS no_larea,
                              SUM(VENDOR_ID IS NULL) AS no_vendor,
                              SUM(NAME IS NULL) AS no_name
                         FROM CL_SGW""")
        v = cur.fetchone()
        cur.execute("""SELECT COUNT(DISTINCT x.SGW_ID) AS c
                         FROM XDR_LTE_CALL_KPI x JOIN CL_SGW s ON s.SGW_ID = x.SGW_ID""")
        joined = cur.fetchone()["c"]
        print(f"\n검증 — 행 {v['n']} · 유일 {v['d']} · XDR 조인 {joined}/{len(xdr)}")
        print(f"  NULL — LAREA {v['no_larea']} ({100*v['no_larea']/v['n']:.0f}%) · "
              f"VENDOR {v['no_vendor']} ({100*v['no_vendor']/v['n']:.0f}%) · NAME {v['no_name']}")
        ok = v["n"] == v["d"] == len(ids) and joined == len(xdr) and v["no_name"] == 0
        print("전부 통과" if ok else "★ 확인 필요")
        return 0 if ok else 1
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
