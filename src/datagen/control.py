"""제어 테이블 — UI 와 생성 데몬이 MySQL 을 통해 만나는 자리.

UI 는 여기에 쓰고, 데몬은 틱마다 읽어 적용한다. DB 를 제어 채널로 쓰는 이유:
데몬이 재시작해도 설정·진행 중 장애가 복원되고(내구성), UI 와 데몬이 서로를
몰라도 되며(분리), "언제 무엇을 주입했나"가 공짜로 남는다(이력 = ground truth).

**`CM_DATAGEN_*` 다** (2026-10-14). 구 이름은 `datagen_config`·`datagen_fault`·
`datagen_history` 로 **소문자였고, 그것이 이 DB 에서 유일한 명명 이탈이었다** —
나머지 75테이블은 전부 대문자 `CD_`/`CL_`/`CM_`/`PM_`/`GP_` 접두를 쓴다.
여기서 만드는 스키마가 그대로 상용이 되므로(별도 상용 DB 는 없다) 로컬에서
규칙을 깨면 그 상태가 운영에 남는다.

`CM_` 는 공통·구성 정보의 접두다(`CM_EPC_INFO`·`CM_DONG`·`CM_EMS` …). 이 셋은
관측 데이터가 아니라 **생성기 구성·제어 상태**이므로 그 묶음에 속한다.
graphio 는 읽지 않는다 — 소유자가 만든다. `ensure_tables()` 가
`CREATE IF NOT EXISTS` 로 보장한다.

**파이썬 쪽 dict 키는 소문자를 유지한다.** `list_faults`·`poll_transitions` 가
커서 `description` 을 그대로 키로 쓰고 소비자 네 파일(`ui`·`cli`·`faults`·
`pemdb1`)이 `f["kind"]`·`f["start_ts"]` 로 읽는다. 그래서 **SELECT 에서 `AS` 로
구 이름을 되돌린다** — DB 는 관례를, 코드는 기존 계약을 지킨다.
"""

from __future__ import annotations

import json
from datetime import datetime

#: 테이블 이름. **문자열을 쿼리에 박지 않는다** — 이번 개명에서 15곳을
#: 찾아다녀야 했고, 한 곳을 빠뜨리면 `CREATE IF NOT EXISTS` 가 조용히 새
#: 테이블을 만들어 **두 벌이 공존**한다(에러 없음).
T_CONFIG = "CM_DATAGEN_CONFIG"
T_FAULT = "CM_DATAGEN_FAULT"
T_HISTORY = "CM_DATAGEN_HISTORY"

#: 주입 가능한 장애 종류 — scenarios.MUTATORS 의 키와 1:1
KINDS = (
    "gw_resource", "gw_node_down", "ran_radio", "mme_overload", "auth_failure",
    "dns_degradation", "external_server", "transport_loss", "ue_model",
    "mobility", "change_suspected", "ims_pdn", "nsa_nr_leg",
)

#: 상태 전이: pending → active → done. cancelled 는 pending/active 어디서든.
DDL = f"""
-- ── CM_DATAGEN_CONFIG ───────────────────────────────────────────────────────
-- UI 가 바꾸는 생성 설정. 데몬이 틱마다 읽으므로 **다음 분부터 반영된다.**
-- 컬럼명이 `KEY`/`VALUE` 가 아닌 이유: **`KEY` 는 MySQL 예약어다**
-- (`GP_SETTING_KV` 와 같은 판단).
CREATE TABLE IF NOT EXISTS `{T_CONFIG}` (
  `CONFIG_KEY`    varchar(64)  NOT NULL         COMMENT 'enabled, rate.<계열> …',
  `CONFIG_VALUE`  varchar(255) NOT NULL,
  `UPDATED_TIME`  datetime     NOT NULL,
  PRIMARY KEY (`CONFIG_KEY`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb3 COLLATE=utf8mb3_general_ci
  COMMENT='생성기 런타임 설정 KV — UI 가 쓰고 데몬이 틱마다 읽는다';

-- ── CM_DATAGEN_FAULT ────────────────────────────────────────────────────────
-- 장애 주입 지시 + 그 이력. 상태 전이는 **DB 에서** 일어난다(`poll_transitions`)
-- — 데몬이 죽었다 떠도 진행 중이던 장애가 그대로 이어진다.
--
-- `FAULT_ID` 가 `AUTO_INCREMENT` 인 것은 이 DB 에서 예외다. 다른 테이블이
-- 피하는 이유는 멱등 upsert(`REPLACE INTO` 가 행을 지우고 새로 넣는다)와
-- 충돌하기 때문인데, 이 테이블은 `UPDATE` 로 상태만 바꾸므로 해당하지 않는다.
-- UI·CLI 가 이 번호로 행을 지목한다(취소·사유 기록).
--
-- `MODE` 를 쓰지 않고 `INJECT_MODE` 인 것은 `sql_mode` 류와 섞여 읽히는 것을
-- 피하려는 것이다.
CREATE TABLE IF NOT EXISTS `{T_FAULT}` (
  `FAULT_ID`      int          NOT NULL AUTO_INCREMENT COMMENT '주입 지시 순번 — UI·CLI 의 핸들',
  `FAULT_KIND`    varchar(32)  NOT NULL         COMMENT 'control.KINDS — scenarios.MUTATORS 키와 1:1',
  `TARGET`        varchar(64)      DEFAULT NULL COMMENT 'SGW-3|MME-1|PGW-2|SGG:11680|NULL(자동)',
  `START_TIME`    datetime     NOT NULL,
  `DURATION_MIN`  int          NOT NULL DEFAULT 30,
  `INTENSITY`     double       NOT NULL DEFAULT 1.0,
  `INJECT_MODE`   varchar(8)   NOT NULL DEFAULT 'manual'  COMMENT 'manual | auto',
  `STATUS`        varchar(12)  NOT NULL DEFAULT 'pending' COMMENT 'pending → active → done · cancelled',
  `NOTE`          varchar(255)     DEFAULT NULL COMMENT '취소 사유 등 — UI 가 보여준다',
  `CREATED_TIME`  datetime     NOT NULL,
  `UPDATED_TIME`  datetime     NOT NULL,
  PRIMARY KEY (`FAULT_ID`),
  KEY `IX_CM_DATAGEN_FAULT_STATUS` (`STATUS`, `START_TIME`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb3 COLLATE=utf8mb3_general_ci
  COMMENT='장애 주입 지시·이력 — 이력이 곧 ground truth';

-- ── CM_DATAGEN_HISTORY ──────────────────────────────────────────────────────
-- 틱 이력. **분 단위로 멱등하다** — `record_tick` 이 DELETE→INSERT 로 맞춘다.
-- UNIQUE 키를 걸지 않는 이유는 그 함수 주석에 있다.
CREATE TABLE IF NOT EXISTS `{T_HISTORY}` (
  `HISTORY_ID`    int      NOT NULL AUTO_INCREMENT,
  `SLOT_TIME`     datetime NOT NULL             COMMENT '분 슬롯 — GP_KPI_*_1M.SLOT_TIME 과 같은 축',
  `RECORD_CNT`    int      NOT NULL             COMMENT '그 분에 생성한 C-Plane 호 수',
  `FAULT_IDS`     varchar(255) DEFAULT NULL     COMMENT '그 분에 적용된 FAULT_ID 목록 (쉼표)',
  `SERIES_STAT`   json         DEFAULT NULL     COMMENT '계열별 {{n, bytes}} — 제어 화면 툴팁용',
  `CREATED_TIME`  datetime NOT NULL,
  PRIMARY KEY (`HISTORY_ID`),
  KEY `IX_CM_DATAGEN_HISTORY_SLOT` (`SLOT_TIME`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb3 COLLATE=utf8mb3_general_ci
  COMMENT='분 단위 생성 이력 — 제어 화면의 인입 현황';
"""


#: **`AS` 로 구 키 이름을 되돌린다.** 소비자(`ui`·`cli`·`faults`·`pemdb1`)가
#: 커서 `description` 을 그대로 dict 키로 받아 `f["kind"]`·`f["start_ts"]` 로
#: 읽는다. DB 컬럼명만 관례에 맞추고 **코드 계약은 건드리지 않는다** — 양쪽을
#: 함께 바꾸면 변경 범위가 네 파일로 번지고, 한 곳을 빠뜨리면 `KeyError` 가
#: 런타임에야 나온다.
FAULT_SELECT = (
    "SELECT FAULT_ID AS fault_id, FAULT_KIND AS kind, TARGET AS target, "
    "START_TIME AS start_ts, DURATION_MIN AS duration_min, "
    "INTENSITY AS intensity, INJECT_MODE AS mode, STATUS AS status, "
    "NOTE AS note, CREATED_TIME AS created_at, UPDATED_TIME AS updated_at"
)

#: 틱 루프가 쓰는 짧은 형태 — 해석에 필요한 여섯 컬럼만.
FAULT_SELECT_ACTIVE = (
    "SELECT FAULT_ID AS fault_id, FAULT_KIND AS kind, TARGET AS target, "
    "START_TIME AS start_ts, DURATION_MIN AS duration_min, INTENSITY AS intensity"
)


#: 나중에 더한 컬럼. **`CREATE TABLE IF NOT EXISTS` 는 기존 테이블을 손대지
#: 않는다** — 새로 만든 환경에는 있고 기존 환경에는 없는 상태가 되어, 그쪽에서
#: `INSERT` 가 `1054 Unknown column` 으로 깨진다. 테이블마다 (컬럼, 정의)를
#: 적어 두고 `ensure_tables` 가 없으면 더한다.
#:
#: MySQL 8 에는 `ADD COLUMN IF NOT EXISTS` 가 없다(MariaDB 전용). 그래서
#: `information_schema` 로 먼저 본다.
_ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    (T_HISTORY, "SERIES_STAT",
     "json DEFAULT NULL COMMENT '계열별 {n, bytes} — 제어 화면 툴팁용'"),
)


def ensure_tables(conn) -> None:
    with conn.cursor() as cur:
        for stmt in DDL.split(";"):
            if stmt.strip():
                cur.execute(stmt)
        for table, column, ddl in _ADDED_COLUMNS:
            cur.execute(
                "SELECT COUNT(*) FROM information_schema.columns "
                "WHERE table_schema = DATABASE() AND table_name = %s "
                "AND column_name = %s", [table, column])
            if not cur.fetchone()[0]:
                cur.execute(f"ALTER TABLE `{table}` ADD COLUMN `{column}` {ddl}")
    conn.commit()


def add_fault(conn, kind: str, target: str | None, start_ts: datetime,
              duration_min: int = 30, intensity: float = 1.0,
              mode: str = "manual", note: str | None = None) -> int:
    if kind not in KINDS:
        raise ValueError(f"알 수 없는 kind: {kind!r} — {', '.join(KINDS)}")
    now = datetime.now()
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO {T_FAULT} (FAULT_KIND,TARGET,START_TIME,DURATION_MIN,"
            "INTENSITY,INJECT_MODE,STATUS,NOTE,CREATED_TIME,UPDATED_TIME) "
            "VALUES (%s,%s,%s,%s,%s,%s,'pending',%s,%s,%s)",
            [kind, target, start_ts, duration_min, intensity, mode, note, now, now],
        )
        fid = cur.lastrowid
    conn.commit()
    return fid


def cancel_fault(conn, fault_id: int) -> bool:
    with conn.cursor() as cur:
        n = cur.execute(
            f"UPDATE {T_FAULT} SET STATUS='cancelled', UPDATED_TIME=%s "
            "WHERE FAULT_ID=%s AND STATUS IN ('pending','active')",
            [datetime.now(), fault_id],
        )
    conn.commit()
    return n > 0


def list_faults(conn, limit: int = 50) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(FAULT_SELECT + f" FROM {T_FAULT} "
                    "ORDER BY FAULT_ID DESC LIMIT %s", [limit])
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def poll_transitions(conn, now: datetime) -> list[dict]:
    """상태 전이를 수행하고 **현재 활성** fault 목록을 돌려준다.

    데몬이 틱마다 부른다. 전이는 DB 에서 일어나므로 데몬이 죽었다 떠도
    진행 중이던 장애가 그대로 이어진다.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE {T_FAULT} SET STATUS='active', UPDATED_TIME=%s "
            "WHERE STATUS='pending' AND START_TIME <= %s", [now, now])
        cur.execute(
            f"UPDATE {T_FAULT} SET STATUS='done', UPDATED_TIME=%s "
            "WHERE STATUS='active' "
            "AND DATE_ADD(START_TIME, INTERVAL DURATION_MIN MINUTE) <= %s",
            [now, now])
        cur.execute(FAULT_SELECT_ACTIVE + f" FROM {T_FAULT} "
                    "WHERE STATUS='active' ORDER BY FAULT_ID")
        cols = [d[0] for d in cur.description]
        active = [dict(zip(cols, r)) for r in cur.fetchall()]
    conn.commit()
    return active


def record_tick(conn, slot_ts: datetime, records: int,
                faults: str | None = None,
                series_stat: dict | None = None) -> None:
    """틱 이력을 **분 단위로 멱등하게** 남긴다.

    생성 자체는 멱등이다(`PemdbWriter.write_xdr` 이 DELETE→INSERT). 그런데
    이력을 그냥 INSERT 하면 같은 분을 두 번 돌릴 때 행이 쌓이고, 제어 화면의
    인입 현황이 `SUM(records)` 를 쓰므로 **실제 생성량의 2배·3배를 보고한다** —
    실측으로 15:00 한 분이 3행 쌓여 19,059건이 57,177건으로 보였다. 에러는
    없고 숫자만 틀리는 형태다.

    UNIQUE 키를 걸지 않고 DELETE→INSERT 로 맞춘다. 키를 추가하면 이미 만들어진
    테이블에는 `CREATE TABLE IF NOT EXISTS` 가 손대지 못해 한쪽만 적용되고,
    그 상태에서 평범한 INSERT 가 남아 있으면 `1062 Duplicate entry` 로 데몬이
    죽는다.
    """
    with conn.cursor() as cur:
        cur.execute(f"DELETE FROM {T_HISTORY} WHERE SLOT_TIME=%s", [slot_ts])
        cur.execute(
            f"INSERT INTO {T_HISTORY} "
            "(SLOT_TIME, RECORD_CNT, FAULT_IDS, SERIES_STAT, CREATED_TIME) "
            "VALUES (%s,%s,%s,%s,%s)",
            [slot_ts, records, faults or None,
             json.dumps(series_stat, separators=(",", ":")) if series_stat else None,
             datetime.now()])
    conn.commit()


def get_config(conn, k: str, default: str | None = None) -> str | None:
    with conn.cursor() as cur:
        cur.execute(f"SELECT CONFIG_VALUE FROM {T_CONFIG} WHERE CONFIG_KEY=%s", [k])
        row = cur.fetchone()
    return row[0] if row else default


def set_config(conn, k: str, v: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO {T_CONFIG} (CONFIG_KEY,CONFIG_VALUE,UPDATED_TIME) "
            "VALUES (%s,%s,%s) ON DUPLICATE KEY UPDATE "
            "CONFIG_VALUE=VALUES(CONFIG_VALUE), UPDATED_TIME=VALUES(UPDATED_TIME)",
            [k, v, datetime.now()])
    conn.commit()
