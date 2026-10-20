"""상용 DDL → 로컬 PoC DDL 변환.

상용 스키마를 정본으로 삼되 세 가지만 바꾼다.
  1) 파티션 범위 — 상용은 2024-05 고정이다. 생성기가 '지금'을 채우므로
     오늘 기준 ±N일 일단위로 다시 깐다. 안 바꾸면 모든 INSERT 가
     MAXVALUE 한 칸에 몰려 프루닝이 죽는다.
  2) MAXVALUE 파티션 추가 — 상용엔 없다. 없으면 범위를 벗어난 분이
     조용히 거부되는 게 아니라 INSERT 가 에러로 죽는다.
  3) `GP_COLUMNS`·`GP_PRIMARY_KEY` — **쓰기 주체가 graphio 로 바뀐 테이블**이
     요구하는 컬럼과 키. 아래 절 참조.

컬럼·타입·collation(utf8mb3_general_ci)은 **건드리지 않는다.** 상용이 정본이고,
여기서 utf8mb4 로 바꾸면 조인할 때 Illegal mix of collations 가 난다.

## 왜 (3)이 생겼나 — 손편집이 재생성에 지워진다 (2026-10-20)

`PM_LINK_EPC_KPI_1M` 은 상용이 채우던 자리였는데 **2026-10-14 에 쓰기 주체가
graphio 로 바뀌었다**(`cell_agg.aggregate_link`). 그 뒤 필요해진 것 둘을
**산출물 `pemdb1_local.sql` 에 손으로** 적어 두고 있었다. 그 파일은
`.gitignore` 된 기계 생성물이라 —

* **이 스크립트를 다시 돌리면 둘 다 사라진다.** `FE_S11_CNT` 가 없으면 corr
  이 `Unknown column 'FE_S11_CNT'` 로 죽는다(이미 한 번 겪었다). 머리말이
  "손으로 고치지 말 것"이라고 적어 두고 정작 손으로 고쳐야만 돌아가는
  상태였다.
* **다른 사람 환경에는 애초에 없다.** 추적되는 것은 이 스크립트뿐이다.

그래서 변형을 **여기로 올렸다.** 상용 원본(`SRC`)은 그대로 둔다 — 상용에
없는 것을 상용 덤프에 적으면 다음 덤프에서 또 어긋난다.

## ⚠ 재생성이 **아직** 지우는 것 — 돌리기 전에 읽을 것

`GP_COLUMNS`·`GP_PRIMARY_KEY` 로 올린 것은 `PM_LINK_EPC_KPI_1M` 뿐이다.
`pemdb1_local.sql` 에는 **아직 이 스크립트가 모르는 손편집 둘**이 남아 있고,
지금 돌리면 둘 다 없어진다(2026-10-20 실제로 밟았다. 백업에서 되돌렸다).

* **`XDR_LTE_CALL_KPI.SPID` 를 일부러 뺀 것.** 상용에는 있다. 요금제·가입
  등급의 대리 지표라 개인 속성에 가까워 로컬 PoC 에 두지 않기로 한 **사람
  판정**(2026-10-13)이다. 재생성은 그것을 되살린다.
* **`CL_SGW` 테이블 전체.** 상용 추출본에 이 테이블이 없어서 사람이 운영
  원본을 보고 컬럼을 준 것이다. `SRC` 에 없으므로 재생성은 **통째로
  떨어뜨린다** — SGW 86대가 권역·제조사 없이 ID 로만 나오던 그 상태로
  돌아간다.

돌려야 하면 **먼저 백업하고 diff 로 대조한다.** 두 항목도 같은 방식으로
(`GP_DROP_COLUMNS` · 손으로 쓴 CREATE 블록) 올리는 것이 숙제다.

**현장 DB 에는 이 파일이 닿지 않는다.** `pemdb1_local.sql` 은 로컬 컨테이너
초기화용이고, 현장 `pemdb6` 는 이미 테이블이 있으므로 `CREATE TABLE IF NOT
EXISTS` 가 아무 일도 하지 않는다. 기존 테이블에 키를 넣는 것은 한 번짜리
`ALTER` 이고, 명령은 graphio 쪽 `docs/DEPLOY.md` 의 "LINK 집계 키 보강"에 있다.
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

# ─────────────────────────────────────────────────────────────────────────────
# graphio 가 요구하는 변형 — 쓰기 주체가 갈린 테이블만
#
# 근거는 머리말 "왜 (3)이 생겼나". 여기 적은 것은 **상용 원본에 없는 것**이다.
# ─────────────────────────────────────────────────────────────────────────────

#: 상용에 없는 추가 컬럼. 컬럼 목록 **끝에** 붙는다 — 쓰는 쪽이
#: `INSERT INTO t (컬럼들) VALUES` 로 이름을 명시하므로 순서는 무관하다.
GP_COLUMNS: dict[str, tuple[str, ...]] = {
    # LINK 알람이 쌍 구간의 First Error(S11) 비율을 내린다.
    "PM_LINK_EPC_KPI_1M": (
        "`FE_S11_CNT` int NOT NULL DEFAULT 0",
    ),
}

#: 상용에 없는 PRIMARY KEY.
#:
#: **파티션 키가 반드시 들어가야 한다** — MySQL 은 유니크 키가 파티션 컬럼을
#: 전부 포함할 것을 요구한다(`EVENT_TIME`). 그리고 **선두에** 두어야 시각
#: 범위 조건이 키 접두를 쓴다. 뒤에 두면 조건이 인덱스를 못 타고 전체 스캔이
#: 되는데, 그건 키가 없는 것과 같다.
#:
#: 왜 보조 인덱스가 아니라 PK 인가 — `cell_agg._write` 가 "구간 DELETE →
#: INSERT" 로 멱등을 **관례로** 지키고 있었다. 키가 있으면 **구조가** 막는다
#: (graphio `AI_INCIDENT_SCOPE` 와 같은 판단). 현장 실측(2026-10-20) 475만 행
#: 에 이 조합 중복 0건이라 그대로 걸 수 있다.
#:
#: 키 바이트 합 293 — utf8mb3 라 문자당 3바이트다(5 + 31+49+13+91+13+91).
#: InnoDB 상한 3072 안이다.
GP_PRIMARY_KEY: dict[str, tuple[str, ...]] = {
    "PM_LINK_EPC_KPI_1M": (
        "EVENT_TIME", "LINK_TYPE", "CALL_TYPE",
        "NODE1_TYPE", "NODE1_ID", "NODE2_TYPE", "NODE2_ID",
    ),
}


def patch(name: str, ddl: str) -> str:
    """`GP_COLUMNS`·`GP_PRIMARY_KEY` 를 컬럼 목록 끝에 더한다."""
    cols = GP_COLUMNS.get(name, ())
    pk = GP_PRIMARY_KEY.get(name, ())
    if not cols and not pk:
        return ddl
    # **PK 컬럼은 NOT NULL 로 바꿔 둔다.** 그냥 두면 MySQL 이 PK 를 만들면서
    # 조용히 NOT NULL 로 고치고 NULL 값을 타입 기본값으로 채운다 — datetime
    # 이면 '0000-00-00' 이다. 선언으로 드러내면 적재가 **에러로** 막힌다.
    for c in pk:
        ddl = re.sub(rf"^(\s*`{c}`\s+.*?)\s+DEFAULT NULL(,?)$",
                     r"\1 NOT NULL\2", ddl, flags=re.M)
    add = list(cols)
    if pk:
        add.append("PRIMARY KEY (" + ", ".join(f"`{c}`" for c in pk) + ")")
    body = "".join(f",\n  {line}" for line in add)
    # 컬럼 목록을 닫는 괄호는 `\n) ENGINE=...` 꼴이다. 파티션 절은 아직
    # 손대지 않은 상태이므로 `repartition` 보다 **먼저** 부른다.
    out, n = re.subn(r"\n\)(\s*ENGINE)", body + "\n)" + r"\1", ddl, count=1)
    if n != 1:
        sys.exit(f"{name}: 컬럼 목록을 닫는 괄호를 못 찾았다 — DDL 모양이 바뀌었다")
    return out


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
    ddl = repartition(patch(t, blocks[t]))
    out.append(f"\n-- ── {t} " + "─" * (60 - len(t)) + "\n" + ddl.replace("CREATE TABLE", "CREATE TABLE IF NOT EXISTS", 1))

out.append("\nSET FOREIGN_KEY_CHECKS = 1;\n")
open(OUT, "w", encoding="utf-8").write("\n".join(out))
print(f"생성: {OUT}")
print(f"테이블 {len(TABLES)-len(missing)}개" + (f" · 누락 {missing}" if missing else ""))
