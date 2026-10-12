"""probe-datagen CLI.

  python -m datagen backfill --minutes 60 --start "2026-08-27 09:00"
  python -m datagen run [--once]

backfill: 지정 구간 생성(성공만) → 내부 DuckDB 집계 → MySQL 적재 (1단계).
run:      상주 데몬 — 재기동 백필 + 분 단위 실시간 틱 + fault 주입(2단계).
파일은 남기지 않는다.
"""

from __future__ import annotations

import argparse
import random
import time
from datetime import datetime, timedelta
from pathlib import Path

import yaml

from . import control, store
from .engine import DataGen
from .mysql_writer import MysqlWriter, SLOT_TABLES

HERE = Path(__file__).resolve().parents[2]


def load_cfg(path: str | None) -> dict:
    p = Path(path) if path else HERE / "config" / "datagen.yaml"
    return yaml.safe_load(p.read_text(encoding="utf-8"))


def cmd_backfill(args) -> int:
    cfg = load_cfg(args.config)
    data_dir = HERE / cfg["paths"]["data"]
    my = cfg["mysql"]
    rates = cfg["rates"]

    start = datetime.fromisoformat(args.start or cfg["generate"]["start"])
    minutes = args.minutes or int(cfg["generate"]["minutes"])
    batch = int(cfg["generate"].get("aggregate_batch", 30))

    dg = DataGen(data_dir, rates, seed=int(cfg["generate"].get("seed", 20260903)))
    w = MysqlWriter(host=my["host"], port=my["port"], user=my["user"],
                    password=my["password"], database=my["database"])
    print(f"▶ 마스터 적재: {w.write_masters(dg.con)}")

    t0 = time.time()
    for b0 in range(0, minutes, batch):
        b1 = min(minutes, b0 + batch)
        for m in range(b0, b1):
            dg.generate_minute(m, start + timedelta(minutes=m))
        lo, hi = start + timedelta(minutes=b0), start + timedelta(minutes=b1)
        dg.aggregate_range(lo, hi)
        w.write_slot(dg.con, lo, hi)
        print(f"  [{lo:%H:%M}~{hi:%H:%M}] 적재 · {time.time() - t0:.0f}s", flush=True)

    print(f"완료 — {minutes}분 · last_slot {w.last_slot()} · {time.time() - t0:.0f}s")
    w.close()
    return 0


# --------------------------------------------------------------------------
# run — 상주 데몬 (백필 + 실시간 틱 + fault 주입)
# --------------------------------------------------------------------------


def _floor_minute(dt: datetime) -> datetime:
    return dt.replace(second=0, microsecond=0)


def _faults_overlapping(conn, lo: datetime, hi: datetime) -> list[dict]:
    """[lo, hi) 와 생애가 겹치는 fault — 백필 전용 조회.

    poll_transitions 는 "지금 활성"만 준다. 배치 하나가 fault 의 생애 전체를
    덮으면 같은 호출 안에서 pending→active→done 으로 건너뛰어 목록에서 빠지고,
    그 장애는 **조용히** 생성되지 않는다. 그래서 백필은 상태가 아니라 구간
    겹침으로 뽑는다 — Scenario 가 분 단위로 창을 다시 자르므로 넉넉히 줘도
    결과는 같다.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT fault_id,kind,target,start_ts,duration_min,intensity "
            "FROM datagen_fault WHERE status<>'cancelled' "
            "AND start_ts < %s "
            "AND DATE_ADD(start_ts, INTERVAL duration_min MINUTE) > %s "
            "ORDER BY fault_id",
            [hi, lo],
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def _cancel_faults(conn, fault_ids: list[int], note: str) -> None:
    """대상 해석에 실패한 fault 를 cancelled 로 — 사유를 note 에 남겨 UI 가 보여준다."""
    if not fault_ids:
        return
    now = datetime.now()
    with conn.cursor() as cur:
        for fid in fault_ids:
            cur.execute(
                "UPDATE datagen_fault SET status='cancelled', note=%s, updated_at=%s "
                "WHERE fault_id=%s",
                [note, now, fid],
            )
    conn.commit()


_CANCEL_NOTE = "대상 해석 실패 — 일치 셀 0개 또는 형식 오류"


def _backfill(dg: DataGen, w: MysqlWriter, rng: random.Random,
              start: datetime, minutes: int, batch: int) -> None:
    """[start, start+minutes) 를 batch 단위로 생성·집계·적재한다.

    dg.anchor == start 를 전제한다 (분 인덱스 좌표계). 단위 테스트가 직접 부른다.
    """
    t0 = time.time()
    for b0 in range(0, minutes, batch):
        b1 = min(minutes, b0 + batch)
        lo, hi = start + timedelta(minutes=b0), start + timedelta(minutes=b1)
        # 상태 전이는 poll 에 맡기되(과거 시각 기준), 적용 목록은 겹침 질의로 뽑는다
        control.poll_transitions(w.conn, hi)
        active = _faults_overlapping(w.conn, lo, hi)
        scs, bad = dg.set_faults(active, rng)
        _cancel_faults(w.conn, bad, _CANCEL_NOTE)
        for m in range(b0, b1):
            dg.generate_minute(m, start + timedelta(minutes=m))
        dg.aggregate_range(lo, hi)
        w.write_slot(dg.con, lo, hi)
        tags = ",".join(sc.id for sc in scs) or "-"
        print(f"  [{lo:%m-%d %H:%M}~{hi:%H:%M}] 적재 · fault {tags} · {time.time() - t0:.0f}s",
              flush=True)


def _purge_old(conn, retention_days: int) -> None:
    """보존 기간을 지난 분 슬롯을 MySQL 에서 비운다 — 창고가 무한히 자라지 않게."""
    cutoff = datetime.now() - timedelta(days=retention_days)
    with conn.cursor() as cur:
        for t in SLOT_TABLES:
            cur.execute(f"DELETE FROM `{t}` WHERE slot_ts < %s", [cutoff])
    conn.commit()


def cmd_run(args) -> int:
    cfg = load_cfg(args.config)
    data_dir = HERE / cfg["paths"]["data"]
    my = cfg["mysql"]
    gen_cfg = cfg["generate"]
    seed = int(gen_cfg.get("seed", 20260903))
    batch = int(gen_cfg.get("aggregate_batch", 30))
    backfill_minutes = args.backfill_minutes or int(gen_cfg.get("backfill_minutes", 180))
    retention_days = int(gen_cfg.get("retention_days", 7))

    dg = DataGen(data_dir, cfg["rates"], seed=seed)
    w = MysqlWriter(host=my["host"], port=my["port"], user=my["user"],
                    password=my["password"], database=my["database"])
    control.ensure_tables(w.conn)
    print(f"▶ 마스터 적재: {w.write_masters(dg.con)}")

    rng = random.Random(seed)
    now = _floor_minute(datetime.now())
    last = w.last_slot()
    floor_start = now - timedelta(minutes=backfill_minutes)
    # 시작점: 마지막 슬롯 +1분. 다만 갭이 backfill_minutes 보다 크면 최근 구간만
    # 메운다 — 며칠치 갭을 전부 메우면 재기동이 몇 시간이 되고, 어차피 보존
    # 기간이 지난 과거는 곧 지워진다.
    if last is None or last + timedelta(minutes=1) < floor_start:
        start = floor_start
    else:
        start = last + timedelta(minutes=1)
    dg.anchor = start  # 분 인덱스 0 = 백필 시작 분 — fault 시간창 계산의 기준점

    total = max(0, int((now - start).total_seconds() // 60))  # 지금-1분까지
    if total:
        print(f"▶ 백필: {start:%Y-%m-%d %H:%M} ~ {now - timedelta(minutes=1):%H:%M} ({total}분)")
        _backfill(dg, w, rng, start, total, batch)
    # 마지막으로 생성한 분 — 틱이 같은 분을 다시 생성하면 raw 가 중복돼
    # 집계가 부풀므로(멱등이 깨진다) 반드시 그 다음 분부터 잇는다
    last_done = start + timedelta(minutes=total - 1) if total else start - timedelta(minutes=1)

    # 첫 보존 정리는 1시간 뒤 — 기동 직후 대량 DELETE 로 첫 틱을 막지 않는다
    last_retention = datetime.now()
    print("▶ 틱 루프 시작" + (" (--once)" if args.once else " (Ctrl-C 종료)"))
    try:
        while True:
            now = datetime.now()
            pending = _floor_minute(now) - timedelta(minutes=1)  # 직전 분 = 방금 닫힌 분
            if pending <= last_done:
                # 다음 분 경계 +3초 — 분이 닫힌 직후의 늦은 레코드 여유
                nxt = _floor_minute(now) + timedelta(minutes=1, seconds=3)
                time.sleep(max(0.5, (nxt - now).total_seconds()))
                continue
            ts = pending
            active = control.poll_transitions(w.conn, datetime.now())
            scs, bad = dg.set_faults(active, rng)
            _cancel_faults(w.conn, bad, _CANCEL_NOTE)
            counts = dg.generate_minute(dg.minute_of(ts), ts)
            # 지각 귀속 재집계 — 완료 시각이 뒤 분에 떨어진 호를 15분 창에서 재귀속.
            # anchor 이전은 duck 에 raw 가 없어 재집계하면 기존 슬롯만 지워진다 — 자른다.
            lo = max(dg.anchor, ts - timedelta(minutes=15))
            hi = ts + timedelta(minutes=1)
            dg.aggregate_range(lo, hi)
            w.write_slot(dg.con, lo, hi)
            rec_total = sum(counts.values())
            tags = ",".join(str(f["fault_id"]) for f in active)
            with w.conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO datagen_history (slot_ts, records, faults, created_at) "
                    "VALUES (%s,%s,%s,%s)",
                    [ts, rec_total, tags or None, datetime.now()],
                )
            w.conn.commit()
            # 시도·완료 귀속이 둘 다 끝난 분만 버린다 (재집계 창 15분의 2배 여유)
            store.trim_raw(dg.con, ts - timedelta(minutes=30))
            last_done = ts
            fault_label = ",".join(sc.id for sc in scs) or "-"
            print(f"[{ts:%H:%M}] {rec_total}건 · fault {fault_label}", flush=True)
            if datetime.now() - last_retention >= timedelta(hours=1):
                _purge_old(w.conn, retention_days)
                last_retention = datetime.now()
            if args.once:
                break
    except KeyboardInterrupt:
        print("\n■ 중단 요청 — 연결 정리 후 종료")
    finally:
        w.close()
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="datagen")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("backfill", help="성공 케이스 N분 생성·적재")
    b.add_argument("--config")
    b.add_argument("--start")
    b.add_argument("--minutes", type=int)
    b.set_defaults(fn=cmd_backfill)
    r = sub.add_parser("run", help="상주 데몬 — 백필 + 실시간 틱 + fault 주입")
    r.add_argument("--config")
    r.add_argument("--once", action="store_true", help="틱 1회만 수행하고 종료 (테스트용)")
    r.add_argument("--backfill-minutes", type=int,
                   help="재기동 시 메울 과거 분 수 (설정 generate.backfill_minutes 덮어쓰기)")
    r.set_defaults(fn=cmd_run)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
