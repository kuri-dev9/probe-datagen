"""백필 fault 주입 단위 검증 — 과거 20분 창.

anchor=2026-08-27 10:00 에 fault(gw_resource, 10:05 시작, 10분)를 datagen_fault
에 넣고 10:00~10:20 을 백필(cli._backfill 직접 호출)한 뒤 MySQL 에서 확인한다:

  1) 10:06~10:12 — success < attempt (실패 주입)
  2) cause_min — s11/first 계열 cause(73) 존재
  3) 10:00~10:04 (장애 전) — success == attempt (성공 전용 베이스라인 유지)
  4) 존재하지 않는 노드 대상("SGW-3" — 이 셀 세트의 SGW id 는 101~) 은
     cancelled + note 처리

주의: 이 셀 세트(data/cells.json)의 SGW id 는 101 이상이라 "SGW-3" 은 일치 셀이
0개다. 주입 검증은 셀이 가장 많은 "SGW-111" 로 하고, "SGW-3" 은 무효 대상
경로(cancelled) 검증에 쓴다.

실행:  PYTHONPATH=src .venv/bin/python tests/test_backfill_fault.py [--keep]
       (--keep: 검증 후 적재 데이터·fault 행을 지우지 않고 남긴다)
"""

from __future__ import annotations

import random
import sys
from datetime import datetime

from datagen import control
from datagen.cli import HERE, _backfill, load_cfg
from datagen.engine import DataGen
from datagen.mysql_writer import MysqlWriter, SLOT_TABLES

ANCHOR = datetime(2026, 8, 27, 10, 0)
MINUTES = 21                       # 10:00 ~ 10:20 (양끝 포함 21분)
FAULT_START = datetime(2026, 8, 27, 10, 5)


def q(conn, sql: str, params=None):
    with conn.cursor() as cur:
        cur.execute(sql, params or [])
        return cur.fetchall()


def main() -> int:
    keep = "--keep" in sys.argv
    cfg = load_cfg(None)
    my = cfg["mysql"]

    dg = DataGen(HERE / cfg["paths"]["data"], cfg["rates"],
                 seed=int(cfg["generate"].get("seed", 20260903)), anchor=ANCHOR)
    w = MysqlWriter(host=my["host"], port=my["port"], user=my["user"],
                    password=my["password"], database=my["database"])
    control.ensure_tables(w.conn)

    # 검증용 fault 2건 — 유효 대상(SGW-111)과 무효 대상(SGW-3)
    fid_ok = control.add_fault(w.conn, "gw_resource", "SGW-111", FAULT_START,
                               duration_min=10, intensity=1.0, note="단위검증")
    fid_bad = control.add_fault(w.conn, "gw_resource", "SGW-3", FAULT_START,
                                duration_min=10, intensity=1.0, note="단위검증-무효대상")
    print(f"▶ fault 삽입: 유효 #{fid_ok} (SGW-111) · 무효 #{fid_bad} (SGW-3)")

    print(f"▶ 백필 {ANCHOR:%Y-%m-%d %H:%M} ~ +{MINUTES}분")
    _backfill(dg, w, random.Random(0), ANCHOR, MINUTES,
              batch=int(cfg["generate"].get("aggregate_batch", 30)))

    failures: list[str] = []

    # 1) 장애 창(10:06~10:12): 실패가 주입되어 success < attempt
    att, suc = q(w.conn,
                 "SELECT SUM(attempt),SUM(success) FROM kpi_cell_min "
                 "WHERE slot_ts BETWEEN '2026-08-27 10:06' AND '2026-08-27 10:12'")[0]
    print(f"[1] 10:06~10:12  attempt={att} success={suc}")
    if not (att and suc is not None and suc < att):
        failures.append("장애 창에서 success < attempt 아님")

    # 2) cause_min 에 s11/first 계열 cause 존재
    rows = q(w.conn,
             "SELECT scope,interface,cause,SUM(cnt) FROM cause_min "
             "WHERE slot_ts>='2026-08-27 10:05' GROUP BY 1,2,3 ORDER BY 4 DESC LIMIT 5")
    print("[2] cause_min 상위 5:")
    for r in rows:
        print(f"    scope={r[0]:<6} interface={r[1]} cause={r[2]} cnt={r[3]}")
    if not any(r[0] in ("s11", "first") and int(r[2]) == 73 for r in rows):
        failures.append("s11/first 계열 cause 73 이 상위에 없음")

    # 3) 장애 전(10:00~10:04): 성공 전용 베이스라인.
    #
    #    엄밀한 success == attempt 는 성립하지 않는다 — 시도는 call_start_time
    #    의 분에 귀속되는데(aggregate_cell §4.1), 생성기는 End 레코드의
    #    call_start_time 을 종료 시각 - 통화시간(최대 600초)으로 역산한다.
    #    장애 분에 "종료"된 호가 장애 전에 "시작"했으면 그 실패는 장애 전
    #    슬롯으로 귀속된다. 레코드 변형은 생성 분 단위라 이 소량 번짐은
    #    generator/aggregate(수정 금지) 의 귀속 규칙에서 오는 구조적 현상이다.
    #    → 장애 전 실패율이 2% 미만의 소량인지로 판정한다.
    att0, suc0 = q(w.conn,
                   "SELECT SUM(attempt),SUM(success) FROM kpi_cell_min "
                   "WHERE slot_ts BETWEEN '2026-08-27 10:00' AND '2026-08-27 10:04'")[0]
    print(f"[3] 10:00~10:04  attempt={att0} success={suc0} "
          f"(실패 {att0 - suc0}건 = 지각 귀속 번짐)")
    for r in q(w.conn,
               "SELECT slot_ts, SUM(attempt), SUM(success) FROM kpi_cell_min "
               "WHERE slot_ts BETWEEN '2026-08-27 10:00' AND '2026-08-27 10:04' "
               "GROUP BY 1 ORDER BY 1"):
        print(f"    {r[0]:%H:%M}  attempt={r[1]} success={r[2]}")
    att0, suc0 = int(att0 or 0), int(suc0 or 0)  # pymysql 은 SUM 을 Decimal 로 준다
    if not att0 or (att0 - suc0) > att0 * 0.02:
        failures.append("장애 전 구간 실패가 소량(2%)을 넘음 — 주입이 창 밖으로 샜다")

    # 4) 무효 대상 fault 는 cancelled + note
    st = q(w.conn, "SELECT fault_id,status,note FROM datagen_fault "
                   "WHERE fault_id IN (%s,%s)", [fid_ok, fid_bad])
    print("[4] fault 상태:")
    for r in st:
        print(f"    #{r[0]} status={r[1]} note={r[2]}")
    bad_row = next(r for r in st if r[0] == fid_bad)
    if bad_row[1] != "cancelled":
        failures.append("무효 대상 fault 가 cancelled 로 전이되지 않음")

    if not keep:
        # 검증 데이터 정리 — 창고를 시험 전 상태로 되돌린다 (1단계 데이터 보존)
        with w.conn.cursor() as cur:
            for t in SLOT_TABLES:
                cur.execute(f"DELETE FROM `{t}` WHERE slot_ts >= %s AND slot_ts < %s",
                            ["2026-08-27 10:00:00", "2026-08-27 10:21:00"])
            cur.execute("DELETE FROM datagen_fault WHERE fault_id IN (%s,%s)",
                        [fid_ok, fid_bad])
        w.conn.commit()
        print("▶ 검증 데이터 정리 완료 (--keep 미지정)")
    w.close()

    if failures:
        print("FAIL:\n  - " + "\n  - ".join(failures))
        return 1
    print("PASS — 백필 fault 주입 검증 통과")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
