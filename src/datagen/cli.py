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


def _generation_enabled(conn) -> bool:
    return str(control.get_config(conn, "enabled", "true")).lower() != "false"


def _apply_live_config(conn, dg, base_rates: dict) -> None:
    """datagen_config 의 rate.* 로 분당 호 수를 덮어쓴다 (없으면 yaml 기본값)."""
    rates = dict(base_rates)
    for k in base_rates:
        v = control.get_config(conn, f"rate.{k}")
        if v is not None:
            try:
                rates[k] = max(0, int(v))
            except ValueError:
                pass            # 잘못된 값은 무시하고 기본값을 쓴다
    if rates != dg.gen.rates:
        dg.gen.rates = rates
        print(f"  설정 반영 — 분당 호 수 {rates}", flush=True)


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
            # 제어 화면이 바꾼 설정을 틱마다 읽는다 — 재기동 없이 다음 분부터
            # 반영된다. Generator 는 self.rates 를 매 분 참조하므로 이 자리에서
            # 덮어쓰면 충분하다(인스턴스를 다시 만들 필요가 없다).
            _apply_live_config(w.conn, dg, cfg["rates"])
            if not _generation_enabled(w.conn):
                # 생성 정지 — 틱은 돌되 데이터를 만들지 않는다. 멈춘 동안의
                # 구간은 비어 있고, 그것이 사실이다(0으로 채우지 않는다).
                last_done = ts
                print(f"[{ts:%H:%M}] 생성 정지 (설정)", flush=True)
                continue
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


# --------------------------------------------------------------------------
# pemfill — 상용 스키마(pemdb1) 적재
# --------------------------------------------------------------------------


def cmd_pemfill(args) -> int:
    """상용 스키마로 생성·적재한다 — `XDR_LTE_CALL_KPI` + 1분 집계 PM_*.

    기존 `backfill`·`run` 과 **완전히 별개 경로**다. 자체 창고 스키마(DuckDB
    kpi_*_min)를 거치지 않고 상용 테이블에 직접 쓴다. 되돌릴 수 없는 변경을
    기존 경로에 섞지 않기 위해 분리했다.
    """
    import pymysql

    from . import pemdb1 as P

    cfg = load_cfg(args.config)
    data_dir = HERE / cfg["paths"]["data"]
    my = cfg["mysql"]
    pem = cfg.get("pemdb1") or {}
    database = args.database or pem.get("database", "pemdb1")
    rate = args.rate or int((pem.get("rates") or {}).get("lte_call", 2000))
    minutes = args.minutes
    seed = int(cfg["generate"].get("seed", 20260903))

    # 파티션이 오늘 기준으로 깔려 있다(docker/mysql/gen_ddl.py). 기본 구간을
    # "지금 직전 N분"으로 잡아야 MAXVALUE 한 칸에 몰리지 않는다.
    now = _floor_minute(datetime.now())
    start = (datetime.fromisoformat(args.start) if args.start
             else now - timedelta(minutes=minutes))

    conn = pymysql.connect(
        host=my["host"], port=int(my["port"]), user=my["user"],
        password=my["password"], database=database,
        # 상용 collation 은 utf8mb3_general_ci 다. 커넥션을 utf8mb4 로 열면
        # 리터럴 비교에서 Illegal mix of collations 가 날 수 있다.
        charset="utf8mb3", autocommit=False,
    )
    try:
        mapping = P.xdr_mapping(conn, database)
        print(f"▶ 컬럼 매핑 확인 — {P.XDR_TABLE} {len(mapping.columns)}컬럼 "
              f"(EVENT_TIME + 생성기 필드 {len(mapping.columns) - 1})")

        gen = P.PemdbGen(
            conn, data_dir, rates={"lte_call": rate}, seed=seed,
            subscriber_pool=int(pem.get("subscriber_pool", 20_000)),
            # 실측 실패·절단 비율을 그대로 재현하는 모드. 기본은 성공 전용
            # (평시 전부 녹색)이고, Cause 집계 경로를 확인할 때 쓴다.
            success_only=(False if args.with_failures
                          else bool(pem.get("success_only", True))),
            enb_limit=(args.enb_limit if args.enb_limit is not None
                       else int(pem.get("enb_limit", 0))),
        )
        mapped = sum(1 for c in gen.cells if c.geo_status == "MAPPED")
        print(f"▶ 셀 마스터 — {len(gen.cells):,}셀 / "
              f"eNB {len({c.enb_id for c in gen.cells}):,}개 "
              f"(좌표 보유 {mapped:,} · UNMAPPED {len(gen.cells) - mapped:,})")
        gen.anchor = start

        # ── 장애 주입 ────────────────────────────────────────────────────
        # 기본은 꺼져 있다 — 기존 `pemfill` 호출(평시 전용)의 동작을 바꾸지
        # 않는다. 주입은 **레코드 단위 변형**이고, 집계 결과를 직접 건드리지
        # 않는다. 그래야 적재→집계→탐지→RCA 전 경로가 실제와 같이 돈다.
        scs: list = []
        if args.scenarios:
            scs, skipped = P.build_scenarios(
                gen, minutes, args.scenarios, lead_minutes=args.lead_minutes)
            for line in skipped:
                print(f"  건너뜀 — {line}")
            for sc in scs:
                t = start + timedelta(minutes=sc.start_minute)
                print(f"  {sc.id} {sc.truth_code:24} "
                      f"[{t:%H:%M}+{sc.duration}m] {sc.shape:4} "
                      f"{sc.target.label()}")
            print(f"▶ 시나리오 {len(scs)}종 배치")
            # 상대 경로는 저장소 루트 기준으로 푼다 — cwd 에 따라 정답이
            # 엉뚱한 자리에 떨어지면 graphio 는 "ground_truth.json 없음" 으로
            # 조용히 채점을 건너뛴다.
            truth_path = Path(args.truth_out or pem.get("truth_out")
                              or (data_dir / "truth" / "ground_truth.json"))
            if not truth_path.is_absolute():
                truth_path = (HERE / truth_path).resolve()
            P.write_truth(truth_path, start, minutes, scs)
            print(f"▶ 정답 기록 — {truth_path}")

        w = P.PemdbWriter(conn, mapping)
        t0 = time.time()
        total = 0
        for m in range(minutes):
            ts = start + timedelta(minutes=m)
            rows = gen.records(m, ts)
            total += w.write_xdr(ts, rows)
            print(f"  [{ts:%m-%d %H:%M}] XDR {len(rows):,}건 · "
                  f"{time.time() - t0:.0f}s", flush=True)

        print(f"▶ XDR 적재 완료 — {total:,}건 / {minutes}분 · "
              f"{time.time() - t0:.0f}s"
              + (f" · 길이 초과 절단 {w.clamped}건" if w.clamped else ""))

        if not args.no_agg:
            hi = start + timedelta(minutes=minutes)
            res = P.aggregate_all(conn, start, hi)
            for t, n in res.items():
                print(f"  {t:22} {n:,}행")
    finally:
        conn.close()
    return 0


def cmd_ui(args) -> int:
    """제어 UI 기동 — 실제 구현은 datagen.ui 에 있다 (import 는 지연시킨다)."""
    from .ui import main as ui_main

    argv = ["--host", args.host, "--port", str(args.port)]
    if args.config:
        argv += ["--config", args.config]
    return ui_main(argv)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="datagen")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("backfill", help="성공 케이스 N분 생성·적재")
    b.add_argument("--config")
    b.add_argument("--start")
    b.add_argument("--minutes", type=int)
    b.set_defaults(fn=cmd_backfill)
    # 제어 UI 도 같은 진입점에 둔다. 컨테이너 ENTRYPOINT 가 `python -m datagen`
    # 하나라, 서비스마다 명령만 바꿔 같은 이미지를 쓸 수 있어야 한다
    # (compose 의 datagen / datagen-ui 가 그렇게 갈린다).
    u = sub.add_parser("ui", help="장애 주입 제어 화면 (기본 8009)")
    u.add_argument("--config")
    u.add_argument("--host", default="127.0.0.1")
    u.add_argument("--port", type=int, default=8009)
    u.set_defaults(fn=cmd_ui)

    # 상용 스키마 경로 — 기존 backfill/run 과 섞지 않는다
    p = sub.add_parser("pemfill", help="상용 스키마(pemdb1) 생성·적재 + 1분 집계")
    p.add_argument("--config")
    p.add_argument("--database", help="대상 DB (기본 설정 pemdb1.database)")
    p.add_argument("--start", help="시작 분 (기본: 지금 - minutes)")
    p.add_argument("--minutes", type=int, default=1)
    p.add_argument("--rate", type=int, help="분당 호 수 (기본 설정 pemdb1.rates.lte_call)")
    p.add_argument("--enb-limit", type=int,
                   help="생성 대상 eNB 수 (기본 설정 pemdb1.enb_limit · 0 이면 전량)")
    p.add_argument("--with-failures", action="store_true",
                   help="실측 실패·절단 비율을 재현 (기본은 성공 전용)")
    p.add_argument("--no-agg", action="store_true", help="XDR 적재만 하고 집계는 건너뛴다")
    # 장애 주입. 미지정이면 평시 전용 — 기존 호출의 동작이 그대로다.
    # `all` 은 pemdb1 에서 돌아가는 10종(`pemdb1.SUPPORTED`)이다. SC-05·06·07 은
    # DNS·HTTP·Transport XDR 이 pemdb1 에 없어 미연동이며, 지정해도 사유와 함께
    # 건너뛴다 — 없는 원천을 지어내지 않는다.
    p.add_argument("--scenarios", metavar="all|SC-01,SC-02,…",
                   help="장애 시나리오 주입 + ground_truth.json 기록")
    p.add_argument("--lead-minutes", type=int,
                   help="첫 장애까지의 평시 구간 (기본: 구간 길이에서 자동)")
    p.add_argument("--truth-out", help="정답 파일 경로 (기본 설정 pemdb1.truth_out)")
    p.set_defaults(fn=cmd_pemfill)

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
