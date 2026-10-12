"""probe-datagen CLI — 1단계: 성공 케이스 백필 적재.

  python -m datagen backfill --minutes 60 --start "2026-08-27 09:00"

생성(성공만) → 내부 DuckDB 집계 → MySQL 적재. 파일은 남기지 않는다.
실시간 틱·장애 주입·제어 UI 는 다음 단계.
"""

from __future__ import annotations

import argparse
import time
from datetime import datetime, timedelta
from pathlib import Path

import yaml

from .engine import DataGen
from .mysql_writer import MysqlWriter

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


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="datagen")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("backfill", help="성공 케이스 N분 생성·적재")
    b.add_argument("--config")
    b.add_argument("--start")
    b.add_argument("--minutes", type=int)
    b.set_defaults(fn=cmd_backfill)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
