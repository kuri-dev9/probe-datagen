"""제어 테이블 — UI 와 생성 데몬이 MySQL 을 통해 만나는 자리.

UI 는 여기에 쓰고, 데몬은 틱마다 읽어 적용한다. DB 를 제어 채널로 쓰는 이유:
데몬이 재시작해도 설정·진행 중 장애가 복원되고(내구성), UI 와 데몬이 서로를
몰라도 되며(분리), "언제 무엇을 주입했나"가 공짜로 남는다(이력 = ground truth).

`datagen_*` 접두사는 graphio 가 읽지 않는 생성기 소유 테이블이라는 표시다.
소유자가 만든다 — ensure_tables() 가 CREATE IF NOT EXISTS 로 보장한다.
"""

from __future__ import annotations

from datetime import datetime

#: 주입 가능한 장애 종류 — scenarios.MUTATORS 의 키와 1:1
KINDS = (
    "gw_resource", "gw_node_down", "ran_radio", "mme_overload", "auth_failure",
    "dns_degradation", "external_server", "transport_loss", "ue_model",
    "mobility", "change_suspected", "ims_pdn", "nsa_nr_leg",
)

#: 상태 전이: pending → active → done. cancelled 는 pending/active 어디서든.
DDL = """
CREATE TABLE IF NOT EXISTS datagen_config (
  k          VARCHAR(64) PRIMARY KEY,
  v          VARCHAR(255) NOT NULL,
  updated_at DATETIME NOT NULL
);
CREATE TABLE IF NOT EXISTS datagen_fault (
  fault_id     INT AUTO_INCREMENT PRIMARY KEY,
  kind         VARCHAR(32)  NOT NULL,
  target       VARCHAR(64)  NULL,     -- 'SGW-3'|'MME-1'|'PGW-2'|'SGG:11680'|NULL(자동)
  start_ts     DATETIME     NOT NULL,
  duration_min INT          NOT NULL DEFAULT 30,
  intensity    DOUBLE       NOT NULL DEFAULT 1.0,
  mode         VARCHAR(8)   NOT NULL DEFAULT 'manual',   -- manual|auto
  status       VARCHAR(12)  NOT NULL DEFAULT 'pending',  -- pending|active|done|cancelled
  note         VARCHAR(255) NULL,
  created_at   DATETIME     NOT NULL,
  updated_at   DATETIME     NOT NULL
);
CREATE TABLE IF NOT EXISTS datagen_history (
  id         INT AUTO_INCREMENT PRIMARY KEY,
  slot_ts    DATETIME NOT NULL,
  records    INT NOT NULL,
  faults     VARCHAR(255) NULL,       -- 그 분에 적용된 fault_id 목록 (쉼표)
  created_at DATETIME NOT NULL,
  KEY idx_slot (slot_ts)
);
"""


def ensure_tables(conn) -> None:
    with conn.cursor() as cur:
        for stmt in DDL.split(";"):
            if stmt.strip():
                cur.execute(stmt)
    conn.commit()


def add_fault(conn, kind: str, target: str | None, start_ts: datetime,
              duration_min: int = 30, intensity: float = 1.0,
              mode: str = "manual", note: str | None = None) -> int:
    if kind not in KINDS:
        raise ValueError(f"알 수 없는 kind: {kind!r} — {', '.join(KINDS)}")
    now = datetime.now()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO datagen_fault (kind,target,start_ts,duration_min,"
            "intensity,mode,status,note,created_at,updated_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,'pending',%s,%s,%s)",
            [kind, target, start_ts, duration_min, intensity, mode, note, now, now],
        )
        fid = cur.lastrowid
    conn.commit()
    return fid


def cancel_fault(conn, fault_id: int) -> bool:
    with conn.cursor() as cur:
        n = cur.execute(
            "UPDATE datagen_fault SET status='cancelled', updated_at=%s "
            "WHERE fault_id=%s AND status IN ('pending','active')",
            [datetime.now(), fault_id],
        )
    conn.commit()
    return n > 0


def list_faults(conn, limit: int = 50) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT fault_id,kind,target,start_ts,duration_min,intensity,"
            "mode,status,note,created_at,updated_at "
            "FROM datagen_fault ORDER BY fault_id DESC LIMIT %s", [limit],
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def poll_transitions(conn, now: datetime) -> list[dict]:
    """상태 전이를 수행하고 **현재 활성** fault 목록을 돌려준다.

    데몬이 틱마다 부른다. 전이는 DB 에서 일어나므로 데몬이 죽었다 떠도
    진행 중이던 장애가 그대로 이어진다.
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE datagen_fault SET status='active', updated_at=%s "
            "WHERE status='pending' AND start_ts <= %s", [now, now])
        cur.execute(
            "UPDATE datagen_fault SET status='done', updated_at=%s "
            "WHERE status='active' "
            "AND DATE_ADD(start_ts, INTERVAL duration_min MINUTE) <= %s",
            [now, now])
        cur.execute(
            "SELECT fault_id,kind,target,start_ts,duration_min,intensity "
            "FROM datagen_fault WHERE status='active' ORDER BY fault_id")
        cols = [d[0] for d in cur.description]
        active = [dict(zip(cols, r)) for r in cur.fetchall()]
    conn.commit()
    return active


def get_config(conn, k: str, default: str | None = None) -> str | None:
    with conn.cursor() as cur:
        cur.execute("SELECT v FROM datagen_config WHERE k=%s", [k])
        row = cur.fetchone()
    return row[0] if row else default


def set_config(conn, k: str, v: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO datagen_config (k,v,updated_at) VALUES (%s,%s,%s) "
            "ON DUPLICATE KEY UPDATE v=VALUES(v), updated_at=VALUES(updated_at)",
            [k, v, datetime.now()])
    conn.commit()
