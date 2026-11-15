"""셀 마스터의 구(`CM_CELL_REP_RU_LOC_JP.CITY_ID·GU_ID`)를 읍면동 B안 ADM2 에 맞춘다 (2026-11-15 · 사용자 결정 안 가).

**왜.** 일본 전환 때 마스터의 `CITY_ID·GU_ID` 는 한국 코드 체계를 그대로 옮기고 좌표만 실제 위치로 바꿨다
(`topo.relocate_cells` 머리말 "키 유지, 좌표만"). 11-15 graphio 읍면동 B안은 `CM_CELL_GEO_MAP` 의 ADM1~3 만
e-Stat 경계 판정으로 바꿨다. 그래서 한 셀의 구가 두 개다 — 사건 제목·`sgg_codes`·시군구 축 탐지·생성 범위
(`scope.gu`)는 마스터를, 지도·사건 지역·체감 ADM2 는 B안을 본다. 25번 F-353 은 같은 셀이 「中央区」와 「港区」로
나와 L3 가 둘을 섞었다. 실제 위치는 B안 쪽이다(경계 안 판정 `POLYGON`).

**짝짓기.** B안 ADM2 이름 + 그 부모 ADM1(도도부현) 이름 → `CM_CITY_JP.CITY_NAME` → `CM_GU_JP(CITY_ID, GU_NAME)`.
구 이름만으로 짝짓지 않는다 — `中央区` 는 도쿄·치바·사가미하라 …에 다 있다. 짝이 없거나 둘 이상이면 그 셀은
**바꾸지 않고** 목록으로 보고한다(새 `CM_GU_JP` 행을 만들지 않는다).

**쓰는 것은 UPDATE 뿐이다.** 행 INSERT·DELETE·DDL 없음. `DONG_ID` 는 건드리지 않는다 — 합성 동이고, 읍면동은
이미 B안(`CM_CELL_GEO_MAP` ADM3)이 정본이다. 다만 새 구 아래에 그 `DONG_ID` 가 없는 셀 수를 함께 센다(마스터
경로의 동 이름 `adm_emd_*` 가 비게 되는 셀이다).

    cd probe-datagen
    ( set -a; . ../graphio-probe-rca/.env; set +a; python3 tools/master_gu_align.py --dry-run )
    ( set -a; . ../graphio-probe-rca/.env; set +a; python3 tools/master_gu_align.py --apply --backup )
    ( set -a; . ../graphio-probe-rca/.env; set +a; python3 tools/master_gu_align.py --restore data/pemdb1/repairs/<원장>.json )

원장·CSV 는 `data/pemdb1/repairs/`(git 밖 — eNB 번호는 운영 자산이고 이 저장소는 공개다).
**반영**: gen 은 셀 마스터를 기동할 때 한 번 읽는다(`pemdb1.load_cells`) — gen 재시작이 필요하다. 생성 범위
`scope.gu` 가 `GU_ID` 로 셀을 고르므로 **생성되는 셀 집합이 바뀐다**(dry-run 이 들어오고 나가는 셀을 센다).
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
OUT = HERE / "data" / "pemdb1" / "repairs"
MASTER = "CM_CELL_REP_RU_LOC_JP"
SCOPE_CFG = HERE / "config" / "datagen.yaml"


def _connect():
    import pymysql
    return pymysql.connect(host=os.environ.get("GP_MYSQL_HOST") or "127.0.0.1",
                           port=int(os.environ.get("GP_MYSQL_PORT") or 3306),
                           user=os.environ["GP_MYSQL_USER"], password=os.environ["GP_MYSQL_PASSWORD"],
                           database=os.environ.get("GP_MYSQL_DB") or "pemdb6", charset="utf8mb4",
                           autocommit=False)


def _scope_gu() -> set[int]:
    """생성 범위 구 목록(`pemdb1.scope.gu`) — 들어오고 나가는 셀을 세는 데만 쓴다."""
    try:
        import yaml
        cfg = yaml.safe_load(SCOPE_CFG.read_text(encoding="utf-8"))
        node = cfg
        for k in ("pemdb1",):
            node = node.get(k) or {}
        stack = [node]
        while stack:
            n = stack.pop()
            if isinstance(n, dict):
                if isinstance(n.get("scope"), dict) and "gu" in n["scope"]:
                    return {int(x) for x in n["scope"]["gu"] or ()}
                stack.extend(n.values())
    except Exception:  # noqa: BLE001 — 보고용이다
        pass
    return set()


def plan(cur) -> dict:
    cur.execute(f"SELECT CITY_ID, CITY_NAME FROM CM_CITY_JP")
    city_by_name: dict[str, list[int]] = {}
    for cid, nm in cur.fetchall():
        city_by_name.setdefault(str(nm), []).append(int(cid))
    cur.execute("SELECT CITY_ID, GU_ID, GU_NAME FROM CM_GU_JP")
    gu_by: dict[tuple[int, str], list[int]] = {}
    gu_name: dict[tuple[int, int], str] = {}
    for cid, gid, nm in cur.fetchall():
        gu_by.setdefault((int(cid), str(nm)), []).append(int(gid))
        gu_name[(int(cid), int(gid))] = str(nm)
    cur.execute("SELECT CITY_ID, GU_ID, DONG_ID FROM CM_DONG_JP")
    dongs = {(int(a), int(b), int(c)) for a, b, c in cur.fetchall()}
    cur.execute("SELECT u2.AREA_CD, u2.AREA_NAME, u1.AREA_NAME FROM CM_GEO_UNIT u2 "
                "LEFT JOIN CM_GEO_UNIT u1 ON u1.AREA_LEVEL = 'ADM1' AND u1.AREA_CD = u2.PARENT_CD "
                "WHERE u2.AREA_LEVEL = 'ADM2'")
    adm2 = {str(cd): (str(nm), None if pn is None else str(pn)) for cd, nm, pn in cur.fetchall()}
    cur.execute("SELECT ENB_ID, CELLNUM, AREA_CD, METHOD FROM CM_CELL_GEO_MAP WHERE AREA_LEVEL = 'ADM2'")
    b: dict[tuple[str, str], tuple[str, str]] = {}
    dup = 0
    for e, n, cd, m in cur.fetchall():
        k = (str(e), str(n))
        dup += k in b
        b[k] = (str(cd), str(m))
    cur.execute(f"SELECT ENB_ID, SECTOR, CITY_ID, GU_ID, DONG_ID FROM {MASTER}")
    rows = cur.fetchall()

    scope = _scope_gu()
    change, skip = [], Counter()
    skip_cells: dict[str, list[str]] = {}
    same = no_b = 0
    pair = Counter()
    dong_lost = dong_lost_before = 0
    enter = leave = 0
    for e, sec, cid, gid, did in rows:
        k = (str(e), str(sec))
        cid, gid = int(cid), int(gid)
        if (cid, gid, int(did)) not in dongs:
            dong_lost_before += 1
        got = b.get(k)
        if got is None:
            no_b += 1
            continue
        nm, pref = adm2.get(got[0], (None, None))
        cands = city_by_name.get(pref or "", [])
        tgt = [(c, g) for c in cands for g in gu_by.get((c, nm or ""), [])]
        if len(tgt) != 1:
            why = f"{pref or '?'} {nm or got[0]} — " + ("CM_GU_JP 에 없음" if not tgt else f"짝 {len(tgt)}개")
            skip[why] += 1
            skip_cells.setdefault(why, []).append(f"{k[0]}-{k[1]}")
            continue
        nc, ng = tgt[0]
        if (nc, ng) == (cid, gid):
            same += 1
            continue
        change.append({"enb": k[0], "sector": k[1], "old": [cid, gid], "new": [nc, ng], "method": got[1]})
        pair[(gu_name.get((cid, gid), f"{cid}-{gid}"), nm)] += 1
        if (nc, ng, int(did)) not in dongs:
            dong_lost += 1
        if scope:
            enter += gid not in scope and ng in scope
            leave += gid in scope and ng not in scope
    return {"total": len(rows), "same": same, "no_b": no_b, "dup_b": dup, "change": change,
            "pairs": pair, "skip": skip, "skip_cells": skip_cells, "dong_lost": dong_lost,
            "dong_lost_before": dong_lost_before, "scope": sorted(scope), "enter": enter, "leave": leave}


def report(p: dict, csv_path: Path | None) -> None:
    ch = p["change"]
    by_m = Counter(c["method"] for c in ch)
    print(f"마스터 {p['total']:,}셀 — 바꿀 {len(ch):,} (POLYGON {by_m.get('POLYGON', 0):,} · "
          f"NEAREST {by_m.get('NEAREST', 0):,}) · 이미 같음 {p['same']:,} · B안 행 없음 {p['no_b']:,} · "
          f"짝 없어 건너뜀 {sum(p['skip'].values()):,}")
    if p["dup_b"]:
        print(f"  ! CM_CELL_GEO_MAP ADM2 가 두 행인 셀 {p['dup_b']} — 마지막 행을 썼다")
    print("  옛 구 → 새 구 상위 15:")
    for (a, z), n in p["pairs"].most_common(15):
        print(f"    {a} → {z}  {n}")
    for why, n in p["skip"].most_common():
        print(f"  건너뜀: {why}  {n}셀 (예 {', '.join(p['skip_cells'][why][:3])})")
    print(f"  동(DONG_ID) 이 새 구 아래에 없는 셀 {p['dong_lost']:,} — 마스터 경로 동 이름이 빈다"
          f" (지금도 없는 셀 {p['dong_lost_before']:,})")
    if p["scope"]:
        print(f"  생성 범위 scope.gu {len(p['scope'])}구 — 들어오는 셀 {p['enter']:,} · 나가는 셀 {p['leave']:,}"
              f" (노선 셀은 구와 상관없이 남는다)")
    if csv_path:
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        with csv_path.open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["enb_id", "sector", "old_city", "old_gu", "new_city", "new_gu", "method"])
            for c in ch:
                w.writerow([c["enb"], c["sector"], *c["old"], *c["new"], c["method"]])
        print(f"  CSV {csv_path}")


def apply(conn, p: dict, ledger: Path) -> None:
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(json.dumps({"table": MASTER, "made": datetime.now().isoformat(timespec="seconds"),
                                  "why": "셀 마스터 구를 읍면동 B안 ADM2 에 맞춤(2026-11-15 사용자 결정 안 가)",
                                  "rows": p["change"]}, ensure_ascii=False), encoding="utf-8")
    print(f"원장 {ledger} ({len(p['change']):,}행) — 쓰기 전에 남겼다")
    n = 0
    with conn.cursor() as cur:
        for c in p["change"]:
            # 옛 값일 때만 바꾼다 — 그 사이 다른 주체가 고쳤으면 건드리지 않는다
            n += cur.execute(f"UPDATE {MASTER} SET CITY_ID = %s, GU_ID = %s "
                             f"WHERE ENB_ID = %s AND SECTOR = %s AND CITY_ID = %s AND GU_ID = %s",
                             (*c["new"], c["enb"], int(c["sector"]), *c["old"]))
    if n != len(p["change"]):
        conn.rollback()
        raise SystemExit(f"! 바뀐 행 {n} ≠ 계획 {len(p['change'])} — 되돌렸다(ROLLBACK)")
    conn.commit()
    print(f"UPDATE {n:,}행 커밋")


def restore(conn, ledger: Path) -> None:
    rows = json.loads(ledger.read_text(encoding="utf-8"))["rows"]
    n = 0
    with conn.cursor() as cur:
        for c in rows:
            n += cur.execute(f"UPDATE {MASTER} SET CITY_ID = %s, GU_ID = %s "
                             f"WHERE ENB_ID = %s AND SECTOR = %s AND CITY_ID = %s AND GU_ID = %s",
                             (*c["old"], c["enb"], int(c["sector"]), *c["new"]))
    conn.commit()
    print(f"되돌림 {n:,}/{len(rows):,}행 (나머지는 그 사이 값이 바뀌어 건드리지 않았다)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true", help="읽기만 — 숫자와 CSV")
    g.add_argument("--apply", action="store_true")
    g.add_argument("--restore", metavar="LEDGER")
    ap.add_argument("--backup", action="store_true", help="--apply 때 필수 — 원장을 먼저 남긴다")
    a = ap.parse_args()
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    conn = _connect()
    try:
        if a.restore:
            restore(conn, Path(a.restore))
            return 0
        with conn.cursor() as cur:
            p = plan(cur)
        conn.rollback()  # 읽기 스냅숏을 닫는다
        report(p, OUT / f"master_gu_align-{stamp}.csv")
        if a.apply:
            if not a.backup:
                print("! --apply 에는 --backup 이 필요하다")
                return 2
            apply(conn, p, OUT / f"master_gu_align-{stamp}.json")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
