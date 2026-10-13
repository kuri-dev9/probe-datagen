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


#: 보존 정리 대상 — (테이블, 시각 컬럼). **마스터는 넣지 않는다**
#: (`CL_*`·`CM_*` 는 시각 컬럼이 없거나 보존 대상이 아니다).
#: `GP_*` 는 graphio 산출물이라 **datagen 이 지우지 않는다** — 남의 산출물을
#: 지우면 그쪽 재실행 전제가 깨진다. graphio 가 자기 `reset()` 으로 비운다.
def _purge_pemdb1(conn, retention_days: int) -> dict[str, int]:
    """보존 기간을 지난 파티션을 떨어뜨린다 — `pemdb1.purge_partitions` 위임.

    **구현을 여기 두지 않는다.** 예전에는 이 함수가 테이블 이름(`XDR_LTE_CALL_KPI`)
    과 파티션 이름 규칙(`len(name) == 9`)을 직접 들고 있었다. 원시 테이블이
    `GP_RAW_*` 로 바뀌고 파티션이 시간 단위(`pYYYYMMDDHH`, 11자)가 되자 **두
    조건이 모두 어긋나 에러 없이 아무것도 지우지 않았다** — 이름을 두 곳에
    적어 둔 대가다(2026-10-14).

    지금은 대상·입도·보관 기간을 `pemdb1` 쪽 한 곳에서 판정한다. 시간 파티션은
    24시간, 일 파티션은 `retention_days` 다.
    """
    from . import pemdb1 as P
    with conn.cursor() as cur:
        cur.execute("SELECT DATABASE()")
        database = cur.fetchone()[0]
    out = {t: n for t, n in P.purge_partitions(
        conn, database, retain_hours=P.RETAIN_HOURS,
        retain_days=retention_days).items()}
    conn.commit()
    return out


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


def _retention_sweep(conn, days: int) -> dict[str, int]:
    """보존 기간을 지난 파티션을 떨어뜨린다 — `_purge_pemdb1` 과 같은 일이다.

    **두 함수가 따로 있었고 구현도 각자 갖고 있었다.** 하나는 상주 데몬(`run`)
    이, 하나는 백필(`pemfill`)이 불렀다. 테이블 이름과 파티션 이름 규칙을 양쪽에
    적어 둔 탓에 `GP_RAW_*` 전환 때 **둘 다 조용히 멈췄다**(2026-10-14).
    지금은 한 자리(`pemdb1.purge_partitions`)만 고치면 둘이 함께 따라온다.
    """
    return _purge_pemdb1(conn, days)


def _pem_rates(cfg: dict, pem: dict, lte_rate: int) -> dict:
    """상용 경로의 계열별 분당 생성량.

    `lte_call` 은 호출부가 정한 값(`--rate` 우선)을 그대로 쓰고, U-Plane 세
    계열은 `pemdb1.rates` → 전역 `rates` 순으로 찾는다. **없으면 0 이고, 0 은
    "그 계열을 만들지 않는다"는 뜻이다** — 설정에 없는 계열을 기본값으로 켜면
    기존 호출의 동작이 조용히 바뀐다.

    전역 `rates:` 의 값(transport 1500 등)은 구 창고 세대의 350셀 기준이다.
    상용 경로는 셀이 2천 개 규모라 그대로 쓰면 셀당 1건 미만이 되어 어떤
    창에서도 판정이 서지 않는다(불변규칙 5). 그래서 `pemdb1.rates` 쪽에
    따로 적어 둔다.
    """
    pem_rates = pem.get("rates") or {}
    base = cfg.get("rates") or {}
    out = {"lte_call": int(lte_rate)}
    for s in ("transport", "dns", "http"):
        out[s] = int(pem_rates.get(s, base.get(s, 0)) or 0)
    return out


def cmd_run(args) -> int:
    """상주 데몬 — **실시간 틱만.** 백필하지 않는다.

    ## 왜 백필을 없앴나

    구 구현은 기동할 때 과거 N분(기본 180)을 먼저 메웠다. 그런데 백필은
    `pemfill` 이 따로 하고, 데몬이 기동마다 과거를 다시 만들면 **이미 있는
    구간을 덮어쓴다** — 장애를 주입해 둔 구간이 평시 데이터로 밀린다.
    사람이 "백필 하지 마라, 실시간으로만 써라"고 명시했다(2026-10-13).

    ## 왜 경로를 바꿨나

    구 구현은 `graphio` DB 의 자체 창고(`kpi_cell_min`·`cause_min`…)에 썼다.
    graphio 가 상용 스키마 `pemdb1` 으로 전환된 뒤로 **그 DB 를 아무도 읽지
    않는다.** 틱은 멀쩡히 돌고 화면만 멈춰 있어 원인을 찾기 어려웠다 —
    에러가 없는 쪽이 더 나쁘다.

    지금은 `pemfill` 과 **같은 쓰기 경로**(`datagen.pemdb1`)를 쓴다. 두 벌로
    만들면 백필과 실시간이 서로 다른 데이터를 만들고, 그 차이는 조용하다.
    """
    import pymysql

    from . import pemdb1 as P

    cfg = load_cfg(args.config)
    data_dir = HERE / cfg["paths"]["data"]
    my = cfg["mysql"]
    pem = cfg.get("pemdb1") or {}
    gen_cfg = cfg["generate"]
    database = pem.get("database", "pemdb6")
    rate = int((pem.get("rates") or {}).get("lte_call",
                                            cfg["rates"].get("lte_call", 2000)))
    seed = int(gen_cfg.get("seed", 20260903))
    retention_days = int(gen_cfg.get("retention_days", 1))

    conn = pymysql.connect(
        host=my["host"], port=int(my["port"]), user=my["user"],
        password=my["password"], database=database,
        # 상용 collation 이 utf8mb3_general_ci 다. utf8mb4 로 열면 리터럴
        # 비교에서 Illegal mix of collations 가 날 수 있다.
        charset="utf8mb3", autocommit=False,
    )
    try:
        control.ensure_tables(conn)
        mapping = P.xdr_mapping(conn, database)
        # U-Plane 테이블이 아직 없으면 빈 dict 가 오고 그 계열은 적재되지
        # 않는다. C-Plane 틱이 그것 때문에 막혀서는 안 된다.
        umaps = P.uplane_mapping(conn, database)

        drawable = None
        xw_path = pem.get("geo_crosswalk")
        if xw_path:
            q = Path(xw_path)
            if not q.is_absolute():
                q = (HERE / q).resolve()
            drawable = P.load_drawable_dongs(q)

        rates = _pem_rates(cfg, pem, rate)
        gen = P.PemdbGen(
            conn, data_dir, rates=rates, seed=seed,
            subscriber_pool=int(pem.get("subscriber_pool", 20_000)),
            success_only=bool(pem.get("success_only", True)),
            enb_limit=int(pem.get("enb_limit", 0)),
            drawable=drawable,
        )
        writer = P.PemdbWriter(conn, mapping)

        # **anchor 는 기동 시각이다.** fault 의 시간창이 여기서부터 센다.
        # 과거로 잡으면 이미 지난 장애가 되살아난다.
        anchor = _floor_minute(datetime.now())
        gen.anchor = anchor
        up_on = [s for s in P.UPLANE_SERIES if s in gen.series and s in umaps]
        print(f"▶ 실시간 틱 — {database} · eNB {len({c.enb_id for c in gen.cells}):,}"
              f" · 셀 {len(gen.cells):,} · 분당 {rate:,}호 · 보존 {retention_days}일")
        print("  U-Plane — "
              + (" · ".join(f"{s} {rates[s]:,}" for s in up_on) if up_on
                 else "꺼짐 (rates 0 이거나 GP_RAW_* 테이블 없음)"))
        print(f"  anchor {anchor:%Y-%m-%d %H:%M} · 백필 없음"
              + (" (--once)" if args.once else " (Ctrl-C 종료)"))

        last_done: datetime | None = None
        last_retention = datetime.now()
        while True:
            now = datetime.now()
            pending = _floor_minute(now) - timedelta(minutes=1)   # 방금 닫힌 분
            if last_done is not None and pending <= last_done:
                nxt = _floor_minute(now) + timedelta(minutes=1, seconds=3)
                time.sleep(max(0.5, (nxt - now).total_seconds()))
                continue
            # 기동 직후 첫 틱은 **그 분부터** 시작한다 — 과거로 거슬러 가지 않는다
            ts = max(pending, anchor)

            if str(control.get_config(conn, "enabled", "true")).lower() == "false":
                # 생성 정지. 그 구간은 **비어 있고 그것이 사실이다** — 0 으로
                # 채우면 없는 트래픽 급감이 된다(불변규칙 4).
                last_done = ts
                print(f"[{ts:%H:%M}] 생성 정지 (설정)", flush=True)
                if args.once:
                    break
                continue

            active = control.poll_transitions(conn, datetime.now())
            scs, bad = gen.set_fault_rows(active)
            if bad:
                _cancel_faults(conn, bad, _CANCEL_NOTE)

            t0 = time.time()
            # **적재보다 먼저 파티션을 보장한다.** 생성기가 꺼져 있는 동안은
            # 파티션이 늘지 않아도 해가 없다 — 쓸 데이터가 없으니 담을 그릇도
            # 필요 없다. 문제는 **다시 켤 때**다: 오래 쉬었으면 마지막 상한이
            # 과거라, 그 상태로 적재하면 그 분이 `pmax` 한 칸으로 흘러간다.
            # 그러면 범위 조건으로 프루닝이 안 돼 질의마다 전체 스캔이 되고,
            # **에러는 나지 않고 느려질 뿐이라 알아채기 어렵다.**
            #
            # 평소에는 조회 한 번으로 끝난다(필요 없으면 DDL 을 실행하지 않는다).
            added = P.ensure_partitions(conn, database)
            if added:
                print(f"  파티션 추가 — {added}", flush=True)
            minute_idx = gen.minute_of(ts)
            rows = gen.records(minute_idx, ts)
            n = writer.write_xdr(ts, rows)
            # U-Plane 은 **같은 분 좌표**로 만든다. 다른 좌표를 쓰면 같은 분의
            # C-Plane 과 시나리오 강도가 어긋나 한 장애가 두 축에서 다른 시각에
            # 나타난다.
            un = writer.write_uplane_all(
                ts, gen.uplane_records(minute_idx, ts), umaps)
            # 그 1분만 집계한다. 구간 멱등이라 다시 돌려도 같은 값이다.
            P.aggregate_all(conn, ts, ts + timedelta(minutes=1))
            # 분 단위 멱등이다 — 그냥 INSERT 하면 같은 분을 두 번 돌릴 때
            # 이력이 쌓여 제어 화면이 생성량을 2배·3배로 보고한다.
            control.record_tick(
                conn, ts, n, ",".join(str(f["fault_id"]) for f in active))
            last_done = ts
            up = " · ".join(f"{s[:4]} {v:,}" for s, v in un.items())
            print(f"[{ts:%H:%M}] XDR {n:,}건"
                  + (f" · U {up}" if up else "")
                  + f" · fault {','.join(sc.id for sc in scs) or '-'}"
                  f" · {time.time() - t0:.1f}s", flush=True)

            if datetime.now() - last_retention >= timedelta(hours=1):
                swept = _retention_sweep(conn, retention_days)
                if any(swept.values()):
                    print(f"  보존 정리 — 파티션 {swept} 드롭 "
                          f"(원시 {P.RETAIN_HOURS}시간 · 집계 {retention_days}일)",
                          flush=True)
                # **`pmax` 에 행이 쌓이면 파티션 관리가 멈춘 것이다.** 그 상태는
                # 에러를 내지 않고 질의만 느려지므로, 여기서 말하지 않으면
                # 아무도 모른다.
                if stuck := P.pmax_rows(conn, database):
                    print(f"  ★ pmax 적적 — {stuck} · 파티션 범위를 확인하라",
                          flush=True)
                last_retention = datetime.now()
            if args.once:
                break
    except KeyboardInterrupt:
        print("\n■ 중단 요청 — 연결 정리 후 종료")
    finally:
        conn.close()
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
    database = args.database or pem.get("database", "pemdb6")
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
        umaps = P.uplane_mapping(conn, database)
        if umaps:
            print("  U-Plane — " + " · ".join(
                f"{m.table} {len(m.columns)}컬럼" for m in umaps.values()))
        else:
            print(f"  U-Plane — GP_RAW_* 가 {database} 에 없다. 그 계열은 건너뛴다")

        # ── 그릴 수 있는 동으로 셀을 한정한다 ─────────────────────────────
        # 번들 경계 geojson 은 행정동, pemdb1 은 법정동이다. 매핑으로는 못 넘는
        # 차이라(`pemdb1.load_drawable_dongs` 참조) 셀 쪽을 좁힌다. 경로가
        # 설정돼 있는데 파일이 없으면 **멈춘다** — 조용히 전량으로 돌면 읍면동
        # 지도의 절반이 다시 회색이 되고, 그것을 알아챌 방법이 없다.
        drawable = None
        xw_path = args.geo_crosswalk or pem.get("geo_crosswalk")
        if xw_path and not args.no_geo_filter:
            p = Path(xw_path)
            if not p.is_absolute():
                p = (HERE / p).resolve()
            drawable = P.load_drawable_dongs(p)
            print(f"▶ 폴리곤 있는 동 {len(drawable):,}개 — {p}")

        gen = P.PemdbGen(
            conn, data_dir, rates=_pem_rates(cfg, pem, rate), seed=seed,
            subscriber_pool=int(pem.get("subscriber_pool", 20_000)),
            # 실측 실패·절단 비율을 그대로 재현하는 모드. 기본은 성공 전용
            # (평시 전부 녹색)이고, Cause 집계 경로를 확인할 때 쓴다.
            success_only=(False if args.with_failures
                          else bool(pem.get("success_only", True))),
            enb_limit=(args.enb_limit if args.enb_limit is not None
                       else int(pem.get("enb_limit", 0))),
            drawable=drawable,
        )
        mapped = [c for c in gen.cells if c.geo_status == "MAPPED"]
        print(f"▶ 셀 마스터 — {len(gen.cells):,}셀 / "
              f"eNB {len({c.enb_id for c in gen.cells}):,}개 "
              f"(좌표 보유 {len(mapped):,} · UNMAPPED {len(gen.cells) - len(mapped):,})")
        # 지리 축이 실제로 몇 칸을 칠하는지 — 패턴(CLUSTER/CORRIDOR)이 성립할
        # 만큼 한 시군구에 셀이 모여 있는지를 여기서 보고 판단한다.
        if mapped:
            print(f"  시도 {len({c.adm_sido_cd for c in mapped})} · "
                  f"시군구 {len({(c.adm_sido_cd, c.adm_sgg_cd) for c in mapped})} · "
                  f"읍면동 {len({(c.adm_sido_cd, c.adm_sgg_cd, c.adm_emd_cd) for c in mapped})}")
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
        up_total: dict[str, int] = {}
        # ── 재개 ────────────────────────────────────────────────────────────
        # **시나리오 배치가 `(start, minutes)` 에 묶여 있어 구간을 쪼갤 수 없다.**
        # `scenarios.build_scenarios` 가 `lead`·`span`·`dur` 를 그 두 값에서
        # 계산하므로, 뒷부분만 따로 돌리면 13종이 다른 자리에 깔리고 정답 파일도
        # 다시 쓰인다. 그래서 같은 명령을 그대로 다시 호출하되 **이미 만든 분만
        # 건너뛴다.**
        #
        # 분 단위로 안전하다. `write_xdr` 이 DELETE→INSERT 를 한 트랜잭션에
        # 넣고 커밋하므로 어떤 분도 반쯤 쓰인 상태로 남지 않는다 — 행이 있으면
        # 그 분은 완성된 것이다.
        # **적재보다 먼저 파티션을 보장한다.** 백필은 과거 구간을 쓰므로
        # 특히 중요하다 — 범위 밖이면 그 분이 전부 `pmax` 로 몰리고, 그러면
        # 프루닝이 죽는다(에러 없이 느려진다). `ensure_partitions` 는 필요
        # 없으면 조회 한 번으로 끝난다.
        added = P.ensure_partitions(conn, database)
        if added:
            print(f"▶ 파티션 추가 — {added}")

        done: set = set()
        if args.resume:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT DISTINCT EVENT_TIME FROM `{P.XDR_TABLE}` "
                    "WHERE EVENT_TIME >= %s AND EVENT_TIME < %s",
                    [start, start + timedelta(minutes=minutes)],
                )
                done = {r[0] for r in cur.fetchall()}
            print(f"▶ 재개 — 이미 만든 {len(done):,}분을 건너뛴다 "
                  f"(남은 {minutes - len(done):,}분)")
        for m in range(minutes):
            ts = start + timedelta(minutes=m)
            if ts in done:
                continue
            rows = gen.records(m, ts)
            total += w.write_xdr(ts, rows)
            un = w.write_uplane_all(ts, gen.uplane_records(m, ts), umaps)
            for s, v in un.items():
                up_total[s] = up_total.get(s, 0) + v
            up = " · ".join(f"{s[:4]} {v:,}" for s, v in un.items())
            print(f"  [{ts:%m-%d %H:%M}] XDR {len(rows):,}건"
                  + (f" · U {up}" if up else "")
                  + f" · {time.time() - t0:.0f}s", flush=True)

        print(f"▶ XDR 적재 완료 — {total:,}건 / {minutes}분 · "
              f"{time.time() - t0:.0f}s"
              + (f" · 길이 초과 절단 {w.clamped}건" if w.clamped else "")
              + (f" · 빈 값 → NULL {w.nulled}건" if w.nulled else ""))
        if up_total:
            print("  U-Plane — " + " · ".join(
                f"{s} {v:,}건" for s, v in up_total.items()))

        if not args.no_agg:
            hi = start + timedelta(minutes=minutes)
            res = P.aggregate_all(conn, start, hi)
            for t, n in res.items():
                print(f"  {t:22} {n:,}행")

        if args.follow:
            _pem_follow(conn, P, gen, w, start + timedelta(minutes=minutes),
                        umaps=umaps,
                        retention_days=int(cfg["generate"].get("retention_days", 1)),
                        no_agg=args.no_agg, once=args.once)
    finally:
        conn.close()
    return 0


def _pem_follow(conn, P, gen, w, last_done, *, umaps: dict,
                retention_days: int, no_agg: bool, once: bool) -> None:
    """상용 스키마 실시간 틱 — 분이 닫히면 그 분을 생성·적재한다.

    **`pemfill` 의 준비 코드를 그대로 쓴다.** 셀 선택·가입자 풀·컬럼 매핑이
    백필과 같아야 같은 DB 안에서 데이터가 갈라지지 않는다. 구 `cmd_run` 은
    자체 창고(`graphio.kpi_cell_min` · `slot_ts`)에 쓰는 별개 경로이고, 전환 후
    **아무도 읽지 않는 DB 에 쓰고 있었다** — 에러가 안 나서 더 나빴다.

    ``last_done`` 은 **다음에 생성할 분**이다. 이미 적재한 분을 다시 만들면
    `PemdbWriter` 가 구간 멱등(DELETE→INSERT)이라 행 수는 안 늘지만, 레코드가
    통째로 바뀌어 그 분의 집계·탐지 결과가 흔들린다. 그래서 전진만 한다.

    장애는 `datagen_fault` 에서 틱마다 읽는다(`control.poll_transitions`).
    데몬이 죽었다 떠도 진행 중이던 장애가 이어진다 — 전이가 DB 에서 일어난다.
    """
    from . import control

    control.ensure_tables(conn)
    # 첫 보존 정리는 1시간 뒤 — 기동 직후 대량 삭제로 첫 틱을 막지 않는다.
    next_purge = datetime.now() + timedelta(hours=1)
    print(f"▶ 틱 루프 시작 — 다음 분 {last_done:%H:%M}"
          + (" (--once)" if once else " (Ctrl-C 종료)"), flush=True)
    try:
        while True:
            now = datetime.now()
            pending = _floor_minute(now) - timedelta(minutes=1)   # 방금 닫힌 분
            if pending < last_done:
                nxt = _floor_minute(now) + timedelta(minutes=1, seconds=3)
                time.sleep(max(0.5, (nxt - now).total_seconds()))
                continue

            # 장애 전이 + 활성 목록. 해석은 `faults.resolve_active` 가 하고
            # 같은 fault 는 틱마다 같은 대상에 걸린다(fault_id 로 시드).
            active = control.poll_transitions(conn, pending)
            if gen.anchor is None:
                gen.anchor = pending
            scs, bad = gen.set_fault_rows(active)
            for fid in bad:
                print(f"  ! fault {fid} 해석 실패 — 건너뛴다", flush=True)

            t0 = time.time()
            minute_idx = int((pending - gen.anchor).total_seconds() // 60)
            rows = gen.records(minute_idx, pending)

            # **한 분이 실패해도 데몬은 살아 있어야 한다.** 틱이 예외로 죽으면
            # 그 뒤로 데이터가 멈추는데, 화면은 "최신 관측 시각"을 그대로
            # 보여주므로 **멈춘 것이 보이지 않는다.** 실제로 그렇게 죽었다 —
            # 다른 `pemfill` 이 같은 구간을 건드려 `1213 Deadlock` 이 났다.
            #
            # 데드락·락 타임아웃은 재시도하면 대개 통과한다(MySQL 이 한쪽을
            # 희생시키는 정상 동작이다). 그 외 예외는 재시도해도 같을 테니
            # 한 번만 기록하고 다음 분으로 넘어간다.
            n = None
            un: dict[str, int] = {}
            for attempt in range(1, 4):
                try:
                    n = w.write_xdr(pending, rows)
                    un = w.write_uplane_all(
                        pending, gen.uplane_records(minute_idx, pending), umaps)
                    if not no_agg:
                        P.aggregate_all(conn, pending,
                                        pending + timedelta(minutes=1))
                    break
                except Exception as exc:          # noqa: BLE001 — 데몬이 멈추면 안 된다
                    code = getattr(exc, "args", [None])[0]
                    conn.rollback()
                    if code in (1213, 1205) and attempt < 3:   # 데드락 · 락 대기 초과
                        time.sleep(attempt * 2)
                        print(f"  [{pending:%H:%M}] 재시도 {attempt}/2 — {code}",
                              flush=True)
                        continue
                    print(f"  ! [{pending:%m-%d %H:%M}] 실패 — {type(exc).__name__}: "
                          f"{exc}", flush=True)
                    break
            tags = ",".join(sc.id for sc in scs) or "-"
            if n is not None:
                up = " · ".join(f"{s[:4]} {v:,}" for s, v in un.items())
                print(f"  [{pending:%m-%d %H:%M}] XDR {n:,}건"
                      + (f" · U {up}" if up else "")
                      + f" · fault {tags} · {time.time() - t0:.1f}s", flush=True)
            # 실패해도 전진한다 — 같은 분을 무한히 재시도하면 그 뒤가 영영
            # 생성되지 않는다. 빠진 분은 행이 없을 뿐이고, 0 으로 채우지
            # 않으므로 비율이 NULL 이 되어 "없는 장애"를 만들지 않는다(불변규칙 4).
            last_done = pending + timedelta(minutes=1)

            if datetime.now() >= next_purge:
                try:
                    res = _purge_pemdb1(conn, retention_days)
                except Exception as exc:          # noqa: BLE001
                    conn.rollback()
                    print(f"  ! 보존 정리 실패 — {exc}", flush=True)
                    next_purge = datetime.now() + timedelta(hours=1)
                    if once:
                        return
                    continue
                total = sum(res.values())
                if total:
                    print(f"  보존 정리({retention_days}일) — "
                          + " · ".join(f"{k} {v:,}" for k, v in res.items() if v),
                          flush=True)
                next_purge = datetime.now() + timedelta(hours=1)

            if once:
                return
    except KeyboardInterrupt:
        print("\n틱 루프 종료", flush=True)


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
    b = sub.add_parser("backfill", help="[구 창고 전용] 성공 케이스 N분 생성·적재 — graphio 는 안 읽는다")
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
    # 셀 선택을 폴리곤 있는 동으로 한정한다. 기본은 설정의 경로를 쓴다 —
    # `--no-geo-filter` 로 끄면 종전대로 전량이 후보이고 읍면동 지도가 다시
    # 절반 회색이 된다(비교용으로만 쓴다).
    p.add_argument("--geo-crosswalk",
                   help="행정코드 crosswalk 경로 (기본 설정 pemdb1.geo_crosswalk)")
    p.add_argument("--no-geo-filter", action="store_true",
                   help="폴리곤 없는 동의 셀도 후보에 넣는다 (읍면동 지도가 비어 보인다)")
    p.add_argument("--with-failures", action="store_true",
                   help="실측 실패·절단 비율을 재현 (기본은 성공 전용)")
    p.add_argument("--no-agg", action="store_true", help="XDR 적재만 하고 집계는 건너뛴다")
    # 같은 명령을 다시 호출해 중단된 지점부터 이어 만든다. 시나리오 배치가
    # (start, minutes) 에 묶여 있어 구간을 쪼갤 수 없기 때문이다.
    p.add_argument("--resume", action="store_true",
                   help="이미 XDR 이 있는 분은 건너뛴다 (중단 후 이어 만들기)")
    # 장애 주입. 미지정이면 평시 전용 — 기존 호출의 동작이 그대로다.
    # `all` 은 pemdb1 에서 돌아가는 10종(`pemdb1.SUPPORTED`)이다. SC-05·06·07 은
    # DNS·HTTP·Transport XDR 이 pemdb1 에 없어 미연동이며, 지정해도 사유와 함께
    # 건너뛴다 — 없는 원천을 지어내지 않는다.
    p.add_argument("--scenarios", metavar="all|SC-01,SC-02,…",
                   help="장애 시나리오 주입 + ground_truth.json 기록")
    p.add_argument("--lead-minutes", type=int,
                   help="첫 장애까지의 평시 구간 (기본: 구간 길이에서 자동)")
    p.add_argument("--truth-out", help="정답 파일 경로 (기본 설정 pemdb1.truth_out)")
    # 백필이 끝난 뒤 상주하며 분마다 생성한다. **이것이 실시간 경로다** —
    # 구 `run` 은 자체 창고(graphio DB)에 쓰므로 graphio 화면에 안 나온다.
    p.add_argument("--follow", action="store_true",
                   help="백필 후 상주하며 분마다 생성 (실시간 — 이걸 데몬으로 쓴다)")
    p.add_argument("--once", action="store_true",
                   help="--follow 와 함께: 한 틱만 돌고 끝낸다 (점검용)")
    p.set_defaults(fn=cmd_pemfill)

    r = sub.add_parser("run", help="[구 창고 전용] 상주 데몬 — graphio 는 안 읽는다. 실시간은 pemfill --follow")
    r.add_argument("--config")
    r.add_argument("--once", action="store_true", help="틱 1회만 수행하고 종료 (테스트용)")
    r.set_defaults(fn=cmd_run)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
