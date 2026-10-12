"""상용 DDL → 로컬 PoC DDL 변환.

상용 스키마를 정본으로 삼되 두 가지만 바꾼다.
  1) 파티션 범위 — 상용은 2024-05 고정이다. 생성기가 '지금'을 채우므로
     오늘 기준 ±N일 일단위로 다시 깐다. 안 바꾸면 모든 INSERT 가
     MAXVALUE 한 칸에 몰려 프루닝이 죽는다.
  2) MAXVALUE 파티션 추가 — 상용엔 없다. 없으면 범위를 벗어난 분이
     조용히 거부되는 게 아니라 INSERT 가 에러로 죽는다.

컬럼·타입·collation(utf8mb3_general_ci)은 **건드리지 않는다.** 상용이 정본이고,
여기서 utf8mb4 로 바꾸면 조인할 때 Illegal mix of collations 가 난다.
"""
import re, sys
from datetime import date, timedelta

SRC = "/Users/kuri/proj/vscode/graphio-probe-rca/data/pemdb1_schema.sql"
OUT = "/Users/kuri/proj/vscode/probe-datagen/docker/mysql/pemdb1_local.sql"

TABLES = [
    # 원시 XDR — 생성기의 주 산출물
    "XDR_LTE_CALL_KPI",
    # 1분 집계 — 생성기가 XDR 에서 집계해 채운다
    "PM_ENB_KPI_1M", "PM_EPC_KPI_1M_ORG", "PM_ENB_CAUSE_1M", "PM_EPC_CAUSE_1M",
    "PM_ENB_ROOT_CAUSE_1M", "PM_LINK_ENB_KPI_1M", "PM_LINK_EPC_KPI_1M",
    "PM_DNS_TOT_1M", "PM_APP_TCP_TOT_1M", "PM_APP_HTTP_TOT_1M",
    "PM_APP_THR_TOT_1M", "PM_ENB_TOTAL_KPI_1M",
    # 마스터 — 상용에서 받아 적재한다
    "CL_CELL_MAP", "CM_CELL_REP_RU_LOC", "CM_CITY", "CM_GU", "CM_DONG",
    "CM_EPC_INFO", "CL_MME", "CL_PGW",
]

RETAIN_DAYS = 10      # 과거
AHEAD_DAYS = 3        # 미래 여유

def repartition(ddl: str) -> str:
    """파티션 절을 오늘 기준 일단위로 다시 깐다."""
    if "PARTITION BY" not in ddl:
        return ddl
    m = re.search(r"COLUMNS\((\w+)\)", ddl)
    key = m.group(1) if m else "EVENT_TIME"
    today = date.today()
    parts = []
    for i in range(-RETAIN_DAYS, AHEAD_DAYS + 1):
        d = today + timedelta(days=i + 1)   # VALUES LESS THAN 은 상한이다
        parts.append(
            f"  PARTITION p{(d - timedelta(days=1)):%Y%m%d} "
            f"VALUES LESS THAN ('{d:%Y-%m-%d} 00:00:00') ENGINE = InnoDB"
        )
    # MAXVALUE — 범위 밖 INSERT 가 에러로 죽지 않게 받아둔다
    parts.append("  PARTITION pmax VALUES LESS THAN (MAXVALUE) ENGINE = InnoDB")
    body = ",\n".join(parts)
    new = f"PARTITION BY RANGE COLUMNS({key})\n(\n{body}\n)"
    # 기존 파티션 절(주석 포함) 제거 후 교체
    ddl = re.sub(r"/\*!50500 PARTITION BY.*?\*/", "", ddl, flags=re.S)
    ddl = re.sub(r"PARTITION BY RANGE\s+COLUMNS\(\w+\)\s*\(.*?\)\s*(?=;|\Z)", "", ddl, flags=re.S)
    ddl = ddl.rstrip().rstrip(";").rstrip()
    return f"{ddl}\n{new};"

src = open(SRC, encoding="utf-8", errors="replace").read()
blocks = dict((n, b) for b, n in re.findall(r"(CREATE TABLE `([A-Z0-9_]+)` \(.*?\n\)[^;]*;)", src, re.S))

out = ["""-- pemdb1 로컬 재현 스키마 (PoC)
--
-- **상용 pemdb1 이 정본이다.** 컬럼·타입·collation 을 그대로 가져왔다.
-- utf8mb3_general_ci 도 그대로다 — 여기서 utf8mb4 로 바꾸면 상용과 구조가
-- 갈리고, 나중에 두 환경을 대조할 수 없게 된다.
--
-- 바꾼 것은 파티션 범위뿐이다. 상용은 2024-05 고정인데 생성기는 '지금'을
-- 채우므로 오늘 기준 일단위로 다시 깔았고, 범위 밖 INSERT 가 에러로 죽지
-- 않도록 MAXVALUE 를 더했다 (상용엔 없다).
--
-- 기계 생성: docker/mysql/gen_ddl.py — 손으로 고치지 말 것.

SET NAMES utf8mb4;
SET FOREIGN_KEY_CHECKS = 0;
"""]
missing = []
for t in TABLES:
    if t not in blocks:
        missing.append(t); continue
    ddl = repartition(blocks[t])
    out.append(f"\n-- ── {t} " + "─" * (60 - len(t)) + "\n" + ddl.replace("CREATE TABLE", "CREATE TABLE IF NOT EXISTS", 1))

out.append("\nSET FOREIGN_KEY_CHECKS = 1;\n")
open(OUT, "w", encoding="utf-8").write("\n".join(out))
print(f"생성: {OUT}")
print(f"테이블 {len(TABLES)-len(missing)}개" + (f" · 누락 {missing}" if missing else ""))
