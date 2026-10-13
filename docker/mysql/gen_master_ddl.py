#!/usr/bin/env python3
"""반출된 마스터·코드 테이블 JSON → 로컬 pemdb1 DDL.

**`gen_ddl.py` 와 다른 물건이다.** 저쪽은 상용 `SHOW CREATE TABLE` 결과를
받아 파티션만 재조정한다 — 타입이 정본이다. 이쪽은 **DDL 을 못 받은 테이블**을
JSON 데이터에서 역추론한다. 상용 접근이 끊긴 뒤(2026-10-13 00:04 KST) 남은
것이 행 데이터뿐이었다.

그래서 **타입은 추론이고 정본이 아니다.** 길이·정수폭·NULL 허용이 상용과
다를 수 있다. 분석 결과를 좌우하는 자리(조인 키·코드 값)는 문자열로 남겨
두었다 — 숫자로 좁히면 `'0008'` 같은 선행 0 코드가 `8` 이 되어 조인이 깨진다.

전량이 아닌 테이블은 DDL 머리에 적는다. 그 테이블로 "없다"를 판정하면 안 된다
(불변규칙 17: 모름을 0으로 적지 않는다).
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

DATA = Path(__file__).resolve().parents[2] / "data" / "pemdb1"
OUT = Path(__file__).resolve().parent / "pemdb1_masters.sql"

# 상용 관례를 그대로 따른다. utf8mb4 로 올리면 상용과 구조가 갈려 두 환경을
# 대조할 수 없게 된다 (`pemdb1_local.sql` 머리말과 같은 이유).
COLL = "CHARACTER SET utf8mb3 COLLATE utf8mb3_general_ci"

#: 전량이 아닌 것 — 반출 당시 잘렸다. 사유를 DDL 에 남긴다.
PARTIAL = {
    "CL_ENB_ORG_MAP": "CL_ENB_ORG 43,463행 중 선두 3,000행만. 조직명 참조용이고 커버리지 판정에 쓰면 안 된다",
}

LOCAL_SQL = Path(__file__).resolve().parent / "pemdb1_local.sql"


def commercial_types() -> dict[str, str]:
    """`pemdb1_local.sql` 에서 컬럼 타입을 긁는다 — **이게 정본이다.**

    저 파일은 상용 `SHOW CREATE TABLE` 에서 왔다. 같은 이름의 컬럼이 거기
    있으면 추론하지 않고 그대로 쓴다. 조인 상대의 타입과 어긋나면 MySQL 이
    말없이 형변환하고 **인덱스를 못 쓴다** — 에러가 아니라 느려지는 쪽으로
    틀리므로 발견이 어렵다.

    실측으로 `REGION_ID`·`BRANCH_ID`·`OPTEAM_ID`·`PART_ID`·`COMPANY_ID`·
    `VENDOR_ID`·`CELLNUM`·`MTSO_ID` 는 상용에서 **int** 다. JSON 에도 정수로
    와 있으니 그대로 int 로 둔다. 반대로 `EMS_ID` 는 `varchar(4)` 이고 값이
    `'0008'` 이다 — 이것만 int 로 좁히면 선행 0 이 날아가 `CM_EMS` 와 안 붙는다.

    같은 이름이 테이블마다 폭이 다르면(`ENB_ID` 가 9·20·100·200) **가장 넓은
    것**을 쓴다. 좁은 쪽을 고르면 적재가 잘려 들어간다.
    """
    text = LOCAL_SQL.read_text()
    best: dict[str, tuple[int, str]] = {}
    for m in re.finditer(r"^  `(\w+)` ([^,\n]+?),?$", text, re.M):
        col, typ = m.group(1), m.group(2).strip()
        typ = typ.replace(" NOT NULL", " DEFAULT NULL")
        if "CHARACTER SET" not in typ and typ.startswith("varchar"):
            typ = typ.replace(" DEFAULT NULL", f" {COLL} DEFAULT NULL")
        w = int(m.group(1) and (re.search(r"varchar\((\d+)\)", typ) or [0, 0])[1] or 0)
        if col not in best or w > best[col][0]:
            best[col] = (w, typ)
    return {c: t for c, (_, t) in best.items()}

_DT = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

INT32 = 2_147_483_647


def infer(name: str, values: list, known: dict[str, str]) -> str:
    """컬럼 하나의 타입. **상용에 같은 이름이 있으면 그것을 쓴다.**"""
    if name in known:
        return known[name]

    seen = [v for v in values if v is not None and v != ""]
    if not seen:
        # 전 행 NULL — 상용 타입을 알 수 없다. 가장 넓은 쪽으로 둔다.
        return f"varchar(255) {COLL} DEFAULT NULL"

    if all(isinstance(v, str) and _DT.match(v) for v in seen):
        return "datetime DEFAULT NULL"
    if all(isinstance(v, str) and _DATE.match(v) for v in seen):
        return "date DEFAULT NULL"

    if all(isinstance(v, bool) for v in seen):
        return "tinyint(1) DEFAULT NULL"
    if all(isinstance(v, int) and not isinstance(v, bool) for v in seen):
        return ("int DEFAULT NULL" if max(abs(v) for v in seen) <= INT32
                else "bigint DEFAULT NULL")
    if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in seen):
        return "double DEFAULT NULL"

    width = max(len(str(v)) for v in seen)
    # 여유를 둔다 — 반출 표본에 없던 긴 값이 운영에 있을 수 있다.
    size = next(s for s in (16, 32, 64, 128, 255, 512, 1024, 4000)
                if s >= width * 2) if width * 2 <= 4000 else 0
    return (f"varchar({size}) {COLL} DEFAULT NULL" if size
            else f"text {COLL}")


def columns_of(rows: list[dict], manifest_cols: list[str] | None) -> list[str]:
    """컬럼 순서. manifest 순서를 우선한다 — 상용 `ORDINAL_POSITION` 이다.

    데이터에만 있는 컬럼을 뒤에 붙이는 이유: manifest 가 2차 추출 때
    갱신되지 않은 테이블이 있고(`CM_EPC_INFO`·`CL_PGW` 컬럼 유실 경고),
    **데이터에 있는 것을 버리면 조용히 컬럼이 사라진다.**
    """
    seen: list[str] = []
    for r in rows:
        for k in r:
            if k not in seen:
                seen.append(k)
    if not manifest_cols:
        return seen
    ordered = [c for c in manifest_cols if c in seen]
    return ordered + [c for c in seen if c not in ordered]


def main() -> int:
    manifest = json.loads((DATA / "_manifest.json").read_text())["tables"]
    known = commercial_types()
    print(f"상용 정본 타입 {len(known)}개 컬럼 확보")
    skip = {p.stem for p in DATA.glob("*.json")} & set()  # 자리표시

    # 이미 `pemdb1_local.sql` 이 DDL 을 들고 있는 21개는 건드리지 않는다.
    owned = set(re.findall(r"CREATE TABLE IF NOT EXISTS `(\w+)`", LOCAL_SQL.read_text()))

    parts = [
        "-- pemdb1 마스터·코드 테이블 (로컬 PoC)",
        "--",
        "-- **타입은 상용 DDL 이 아니라 반출 데이터에서 역추론한 것이다.**",
        "-- 상용 접근이 끊긴 뒤 행 데이터만 남아 있었다. 길이·정수폭이 상용과",
        "-- 다를 수 있다. 조인 키·코드 값은 문자열로 남겼다 — 정수로 좁히면",
        "-- '0008' 같은 선행 0 코드가 8 이 되어 조용히 조인이 깨진다.",
        "--",
        "-- 기계 생성: docker/mysql/gen_master_ddl.py — 손으로 고치지 말 것.",
        "",
        "SET NAMES utf8mb4;",
        "SET FOREIGN_KEY_CHECKS = 0;",
        "",
    ]

    made = 0
    for path in sorted(DATA.glob("*.json")):
        table = path.stem
        if table.startswith("_") or table in owned or table in skip:
            continue
        rows = json.loads(path.read_text())
        if not isinstance(rows, list) or not rows:
            print(f"  건너뜀 {table}: 0행 (상용에서도 비어 있어 컬럼을 알 수 없다)")
            continue

        cols = columns_of(rows, (manifest.get(table) or {}).get("columns"))
        defs = [f"  `{c}` {infer(c, [r.get(c) for r in rows], known)}" for c in cols]

        parts.append(f"-- ── {table} ─ {len(rows):,}행 · {len(cols)}컬럼 "
                     + "─" * max(0, 30 - len(table)))
        if table in PARTIAL:
            parts.append(f"-- ⚠ 전량 아님: {PARTIAL[table]}")
        parts.append(f"CREATE TABLE IF NOT EXISTS `{table}` (")
        parts.append(",\n".join(defs))
        parts.append(f") ENGINE=InnoDB DEFAULT CHARSET=utf8mb3 "
                     f"COLLATE=utf8mb3_general_ci;")
        parts.append("")
        made += 1

    OUT.write_text("\n".join(parts))
    print(f"\n{OUT.name}: {made}개 테이블 DDL 생성")
    return 0


if __name__ == "__main__":
    sys.exit(main())
