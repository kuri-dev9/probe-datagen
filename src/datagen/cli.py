"""probe-datagen CLI.

  python -m datagen pemfill --follow          # 실시간 적재 — 이것을 데몬으로 쓴다
  python -m datagen pemfill --follow --once   # 한 틱만 (점검용)
  python -m datagen run                       # pemfill --follow 와 같은 경로
  python -m datagen ui                        # 장애 주입 제어 화면 (:8009)

`pemfill`·`run` 이 **같은 쓰기 경로**(`datagen.pemdb1` → `pemdb6`)를 쓴다.
파일은 남기지 않는다.

**`backfill` 은 막아 두었다** (2026-10-14). 구 창고(`graphio` DB) + 내부 DuckDB
경로이고 graphio 가 그 DB 를 읽지 않는다. 더 중요한 이유는 **백필 자체가 지금
위험**하다는 것이다 — `pemdb6` 가 그대로 운영이 되므로(별도 상용 DB 는 없다)
과거 구간을 평시 데이터로 덮으면 주입해 둔 장애 구간이 밀려나고 되돌릴 원본이
없다.

**구현은 지웠다**(2026-10-15 사람 지시: "백필은 안 할 거다"). `engine.py` ·
`mysql_writer.py` · `aggregate.py` · `store.py` 가 그 사슬이었고 넷 다
부르는 곳이 없었다. 남긴 것은 **서브커맨드와 이 안내뿐**이다 — 치는 사람이
`invalid choice` 가 아니라 왜 막혔는지를 읽어야 한다.

과거 구간이 필요해지면 `pemfill`(상용 스키마 경로)로 만든다. 구 창고로
되돌리는 일은 없다.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import time
from datetime import datetime, timedelta
from pathlib import Path

import yaml

from . import control, raw_parquet

HERE = Path(__file__).resolve().parents[2]


def _deep_merge(base: dict, extra: dict) -> dict:
    """``extra`` 를 ``base`` 위에 덮는다. dict 는 재귀, 그 외는 교체.

    graphio 쪽 `config.py:_deep_merge` 와 **같은 규칙이다.** 두 저장소가
    설정을 같은 방식으로 겹쳐야 환경별 파일을 같은 모양으로 쓸 수 있다.
    """
    out = dict(base)
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_cfg(path: str | None) -> dict:
    """`datagen.yaml` 을 읽고, ``path`` 가 있으면 **그 위에 덮는다.**

    ## 왜 대체가 아니라 병합인가 (2026-10-14)

    전에는 ``--config`` 가 기본 파일을 **통째로 갈아치웠다.** 그래서 환경별
    설정을 두려면 `datagen.yaml` 전체를 복사한 사본이 필요했고, 설정이 두 벌이
    되면 한쪽만 고쳐질 때 조용히 어긋난다 — 생성 모수·요율·보관 기간이 전부
    그 파일에 있다.

    실제로 그 공백이 사고를 냈다. 컨테이너로 띄운 `datagen` 이
    ``mysql.host: 127.0.0.1`` 로 DB 에 붙으려다 죽었다
    (``Errno 111 Connection refused`` — 컨테이너 안에서 그 주소는 자기
    자신이다). 호스트용 값 하나만 덮어쓸 자리가 없어서 호스트 파일이 그대로
    쓰인 것이다.

    병합이면 `config/local.yaml` 처럼 **다른 키만 적은 얇은 파일**을 둘 수
    있다. graphio 가 `default.yaml` → `local.yaml`/`prod.yaml` 로 하는
    것과 같은 구조다.

    **역호환이다.** 완전한 파일을 주던 기존 호출은 모든 키를 덮으므로 결과가
    같다. 달라지는 경우는 "준 파일에 없는 키"뿐이고, 그때 기본값이 살아나는
    것이 의도다.
    """
    base = yaml.safe_load(
        (HERE / "config" / "datagen.yaml").read_text(encoding="utf-8")) or {}
    if path:
        extra = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        base = _deep_merge(base, extra)
    return _apply_env(base)


#: 환경변수 → 설정 키. **graphio 쪽 `config._ENV_KEYS` 와 같은 변수 이름을
#: 쓴다** (2026-10-20). 같은 DB 를 보는 두 프로세스가 서로 다른 변수를 읽으면
#: 한쪽만 고쳐질 때 **에러 없이** gen 과 corr 이 다른 창고를 본다.
#:
#: 접속 정보가 전에는 `datagen.yaml`(추적본)과 `prod.yaml` 두 벌에 평문으로
#: 있었고, graphio 쪽까지 합치면 환경마다 네 자리였다. 이제 `.gp.env` 하나에
#: 적으면 compose 가 네 컨테이너에 같은 값을 넣는다.
#:
#: **`GP_MYSQL_DB` 는 `pemdb1.database` 로 간다.** 실제 쓰기 경로가 그쪽이고
#: (`cli.cmd_run` → `datagen.pemdb1`), `mysql.database` 는 막아 둔 `backfill`
#: 전용이다. 둘 다 덮어 두 값이 갈라지지 않게 한다.
_ENV_KEYS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("GP_MYSQL_HOST",     ("mysql", "host")),
    ("GP_MYSQL_PORT",     ("mysql", "port")),
    ("GP_MYSQL_USER",     ("mysql", "user")),
    ("GP_MYSQL_PASSWORD", ("mysql", "password")),
    ("GP_MYSQL_DB",       ("mysql", "database")),
    ("GP_MYSQL_DB",       ("pemdb1", "database")),
)


def _apply_env(cfg: dict) -> dict:
    """`_ENV_KEYS` 를 설정 위에 덮는다.

    **빈 문자열은 설정하지 않은 것으로 본다.** compose 가 `${GP_MYSQL_HOST}`
    를 넘길 때 호스트에 그 변수가 없으면 빈 값이 들어오는데, 그것으로 yaml 의
    멀쩡한 값을 덮으면 접속이 거부되고 원인이 설정 파일처럼 보인다.

    **환경변수가 없으면 아무것도 하지 않는다** — 현장이 `prod.yaml` 에 실값을
    갖고 돌고 있으므로 그쪽이 `.gp.env` 를 고치기 전에 받아도 멈추지 않는다.
    """
    for var, (sec, key) in _ENV_KEYS:
        raw = os.environ.get(var)
        if raw is None or raw == "":
            continue
        cfg.setdefault(sec, {})[key] = int(raw) if key == "port" else raw
    return cfg


#: `backfill` 을 막는 이유. 조용히 아무것도 안 하는 것보다 **왜 막혔는지
#: 말하며 실패하는 쪽**이 낫다 — 다음 사람이 이 문장을 보고 판단한다.
_BACKFILL_BLOCKED = """backfill 은 막아 두었다 (2026-10-14).

  ① 구 창고 경로다 — `graphio` DB + 내부 DuckDB 에 쓰고, graphio 는 그 DB 를
     읽지 않는다. 지금 창고는 `pemdb6` 다.
  ② **과거 구간 덮어쓰기가 위험하다.** `pemdb6` 가 그대로 운영이 되므로
     평시 데이터로 과거를 메우면 주입해 둔 장애 구간이 밀려나고 되돌릴
     원본이 없다.

실시간 적재는 이쪽이다:
  python -m datagen pemfill --follow          # 데몬
  python -m datagen pemfill --follow --once   # 한 틱만 (점검용)

되살리려면 `cmd_backfill` 본문을 `pemfill` 경로(`datagen.pemdb1`)로 옮기고
이 가드를 지운다. 본문은 그대로 남겨 두었다."""


def cmd_backfill(args) -> int:  # noqa: ARG001
    raise SystemExit(_BACKFILL_BLOCKED)


# ── 구 창고 백필 구현을 들어냈다 (2026-10-15 사람 지시: "백필은 안 할 거다") ──
#
# `_cmd_backfill_retired`(123줄) · `_backfill` · `_purge_old` 가 여기 있었다.
# 셋 다 **부르는 곳이 없었다** — `cmd_backfill` 이 위에서 막고 있었고, 그
# 사슬이 쓰던 `store.py`(구 DuckDB 자체 창고)는 이미 지웠다. 함께 지운 것:
#
#   engine.py (DataGen · 141줄) · mysql_writer.py (88줄) · aggregate.py (661줄)
#
# **서브커맨드 `backfill` 과 `_BACKFILL_BLOCKED` 안내는 남긴다.** 치는 사람이
# `invalid choice` 가 아니라 **왜 막혔는지**를 읽어야 한다 — 그 자리가 이
# 결정의 유일한 기록이다.
#
# 과거 구간이 필요하면 `pemfill` 이다(상용 스키마 경로). 그쪽은 살아 있다.

# ── 백필 사슬과 함께 지웠다가 되살렸다 (2026-10-15) ────────────────────
#
# 아래 셋은 그 구역 안에 있었지만 **상주 데몬(`cmd_run`)이 쓴다.**
# 구역째 들어내면서 함께 날아갔고 `--help` 는 통과했다 — 임포트만으로는
# 안 잡히고 **실제로 틱을 돌려야** `NameError: _floor_minute` 가 난다.
# 지울 때는 지운 이름이 어디서 쓰이는지 세는 것이 순서다.

_CANCEL_NOTE = "대상 해석 실패 — 일치 셀 0개 또는 형식 오류"


def _floor_minute(dt: datetime) -> datetime:
    return dt.replace(second=0, microsecond=0)


def _cancel_faults(conn, fault_ids: list[int], note: str) -> None:
    """대상 해석에 실패한 fault 를 cancelled 로 — 사유를 note 에 남겨 UI 가 보여준다."""
    if not fault_ids:
        return
    now = datetime.now()
    with conn.cursor() as cur:
        for fid in fault_ids:
            cur.execute(
                f"UPDATE {control.T_FAULT} SET STATUS='cancelled', NOTE=%s, "
                "UPDATED_TIME=%s WHERE FAULT_ID=%s",
                [note, now, fid],
            )
    conn.commit()


def _retention_default(gen_cfg: dict) -> int:
    """보존 기간(시간) 기본값 — `retention_hours`, 없으면 `retention_days × 24`.

    **구 키를 계속 읽는다.** 설정 파일들이 `retention_days` 를 들고 있고, 키를
    갈면서 그것을 무시하면 1 이 아닌 값을 적어 둔 환경에서만 어긋난다 —
    로컬은 기본값(24시간)과 우연히 같아 **조용히 통과한다.**
    """
    if (h := gen_cfg.get("retention_hours")) is not None:
        return max(1, int(h))
    return max(1, int(gen_cfg.get("retention_days", 1)) * 24)


def _purge_pemdb1(conn, retention_hours: float) -> dict[str, int]:
    """보존 기간을 지난 파티션을 떨어뜨린다 — `pemdb1.purge_partitions` 위임.

    **구현을 여기 두지 않는다.** 예전에는 이 함수가 테이블 이름(`XDR_LTE_CALL_KPI`)
    과 파티션 이름 규칙(`len(name) == 9`)을 직접 들고 있었다. 원시 테이블이
    `GP_RAW_*` 로 바뀌고 파티션이 시간 단위(`pYYYYMMDDHH`, 11자)가 되자 **두
    조건이 모두 어긋나 에러 없이 아무것도 지우지 않았다** — 이름을 두 곳에
    적어 둔 대가다(2026-10-14).

    지금은 대상·입도·보관 기간을 `pemdb1` 쪽 한 곳에서 판정한다.

    **보존이 한 값이다**(2026-10-15). 전에는 시간 파티션을 코드 상수
    `RETAIN_HOURS=24` 로, 일 파티션을 `retention_days` 로 잘랐다 — 운영자가
    화면에서 보존을 줄여도 **원시는 24시간 그대로**였고, 두 입도가 다른 기간을
    쓰는 이유를 설명할 자리가 없었다. 지금은 `retention_hours` 하나로 두 입도를
    같은 시각에서 자른다(`timedelta` 가 소수 일을 받으므로 `hours/24`).

    **일 파티션은 그래도 하루 단위로만 떨어진다.** 상한이 다음날 00:00 이라
    6시간을 줘도 그 칸이 비워지는 것은 다음날 06:00 이다 — 파티션 입도의
    한계이고, 화면이 그 사실을 적는다.
    """
    from . import pemdb1 as P
    with conn.cursor() as cur:
        cur.execute("SELECT DATABASE()")
        database = cur.fetchone()[0]
    out = {t: n for t, n in P.purge_partitions(
        conn, database, retain_hours=retention_hours,
        retain_days=retention_hours / 24.0).items()}
    conn.commit()
    return out


def _generation_enabled(conn) -> bool:
    return str(control.get_config(conn, "enabled", "true")).lower() != "false"


def _apply_live_config(conn, dg, base_rates: dict) -> None:
    """CM_DATAGEN_CONFIG 의 rate.* 로 분당 호 수를 덮어쓴다 (없으면 yaml 기본값)."""
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


def _retention_sweep(conn, hours: float) -> dict[str, int]:
    """보존 기간을 지난 파티션을 떨어뜨린다 — `_purge_pemdb1` 과 같은 일이다.

    **두 함수가 따로 있었고 구현도 각자 갖고 있었다.** 하나는 상주 데몬(`run`)
    이, 하나는 백필(`pemfill`)이 불렀다. 테이블 이름과 파티션 이름 규칙을 양쪽에
    적어 둔 탓에 `GP_RAW_*` 전환 때 **둘 다 조용히 멈췄다**(2026-10-14).
    지금은 한 자리(`pemdb1.purge_partitions`)만 고치면 둘이 함께 따라온다.
    """
    return _purge_pemdb1(conn, hours)


def _uplane_store(cfg: dict, pem: dict) -> tuple[Path, int]:
    """U-Plane Parquet 루트와 보존 분 수 — `(root, retain_minutes)`.

    **경로를 코드에 박지 않는다.** 지금은 `gen`·`corr` 가 같은 머신의 로컬
    프로세스라 디렉터리 하나를 공유하면 되지만, 컨테이너로 가면 그 자리가
    volume mount 지점이 된다. 설정으로 노출해 두면 그때 고칠 자리가 한 곳이다.

    읽는 쪽은 graphio 의 `paths.uplane_raw` 다. **두 값이 같은 디렉터리를
    가리켜야 하고, 어긋나면 집계가 조용히 0행이 된다** — 에러가 나지 않는
    종류라 기본값을 서로 맞춰 두었다(`pemdb1.UPLANE_ROOT_DEFAULT`).
    """
    from . import pemdb1 as P

    sec = (pem.get("uplane_parquet") or {})
    raw = Path(str(sec.get("root") or P.UPLANE_ROOT_DEFAULT))
    if not raw.is_absolute():
        raw = (HERE / raw).resolve()
    keep = int(sec.get("retain_minutes", P.UPLANE_RETAIN_MINUTES))
    return raw, keep


def _cplane_store(cfg: dict, pem: dict) -> tuple[Path, int]:
    """C-Plane Parquet 루트와 보존 분 수 — `(root, retain_minutes)`.

    `_uplane_store` 와 같은 모양이고 **섹션과 기본 보관만 다르다**
    (`pemdb1.cplane_parquet` · 1440분). 두 평면을 한 함수로 묶지 않은 이유는
    설정 섹션이 따로여야 보관 기간을 따로 줄 수 있기 때문이다 — C-Plane 을
    길게 두는 근거는 `pemdb1.CPLANE_RETAIN_MINUTES` 주석에 있다.

    읽는 쪽은 graphio 의 `paths.cplane_raw` 다. **두 값이 같은 디렉터리를
    가리켜야 하고, 어긋나면 집계가 조용히 0행이 된다.**
    """
    from . import pemdb1 as P

    sec = (pem.get("cplane_parquet") or {})
    raw = Path(str(sec.get("root") or P.CPLANE_ROOT_DEFAULT))
    if not raw.is_absolute():
        raw = (HERE / raw).resolve()
    keep = int(sec.get("retain_minutes", P.CPLANE_RETAIN_MINUTES))
    return raw, keep


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


# --------------------------------------------------------------------------
# capacity — 분당 생성 용량 측정 (100% 기준을 정하는 근거)
# --------------------------------------------------------------------------


def cmd_capacity(args) -> int:
    """배수를 올려가며 한 분을 생성해 시간·크기를 재고 한계를 추정한다.

    ## 왜 필요한가

    `volume_pct` 는 **설정의 기준값 위에 걸리는 비율**이고, 그 기준을 얼마로
    둘지는 서버마다 다르다(디스크·CPU·메모리). 숫자를 감으로 정하면 너무
    작아 표본이 마르거나(불변규칙 5) 너무 커서 틱이 분 예산을 넘긴다 — 둘 다
    **에러 없이** 나타난다. 그래서 재서 정한다.

    ## DB·운영 데이터를 건드리지 않는다

    생성과 **Parquet 쓰기**만 측정하고, 출력은 임시 디렉터리로 보낸 뒤
    지운다. C-Plane·U-Plane 원시가 모두 Parquet 라(2026-10-14) 이것으로
    쓰기 비용까지 재진다. MySQL 에 쓰는 것은 1분 집계(`aggregate_all`)뿐이고
    **그것은 측정하지 않는다** — graphio 의 `corr` 가 하는 일이라 이쪽에서
    재면 두 번 세는 셈이고, 그쪽 비용은 `./gp.sh logs corr` 의 틱 시간에
    이미 찍힌다.

    셀 마스터는 DB 에서 **읽는다**(`load_cells`). 읽기뿐이다.

    ## 한계를 어떻게 읽나

    틱 예산은 60초다(생성이 1분 주기). 거기에 집계·탐지·RCA 가 함께 들어가고
    여유가 없으면 한 틱이 밀리면서 계속 밀린다. 그래서 **생성이 20초(1/3)를
    넘지 않는 지점**을 권장 상한으로 제시한다 — 남은 40초를 집계가 쓴다.

    디스크는 보관 기간과 함께 봐야 한다: C-Plane 은 24시간, U-Plane 은 10분
    보관이므로 같은 분당 바이트라도 누적이 144배 차이난다.
    """
    import shutil
    import tempfile

    import pymysql

    from . import pemdb1 as P

    cfg = load_cfg(args.config)
    data_dir = HERE / cfg["paths"]["data"]
    my, pem = cfg["mysql"], (cfg.get("pemdb1") or {})
    base_rates = _pem_rates(cfg, pem, int((pem.get("rates") or {}).get(
        "lte_call", cfg["rates"].get("lte_call", 2000))))
    steps = [float(x) for x in str(args.steps).split(",") if x.strip()]
    target = float(args.target)

    conn = pymysql.connect(
        host=my["host"], port=int(my["port"]), user=my["user"],
        password=my["password"], database=pem.get("database", "pemdb6"),
        charset="utf8mb3", autocommit=False)
    tmp = Path(tempfile.mkdtemp(prefix="datagen-capacity-"))
    try:
        mapping = P.xdr_mapping(conn, pem.get("database", "pemdb6"))
        drawable = None
        xw = pem.get("geo_crosswalk")
        if xw:
            q = Path(xw)
            drawable = P.load_drawable_dongs(q if q.is_absolute()
                                             else (HERE / q).resolve())
        gen = P.PemdbGen(
            conn, data_dir, rates=base_rates,
            seed=int(cfg["generate"].get("seed", 20260903)),
            subscriber_pool=int(pem.get("subscriber_pool", 20_000)),
            success_only=bool(pem.get("success_only", True)),
            enb_limit=int(pem.get("enb_limit", 0)), drawable=drawable,
            call_type_floor=pem.get("call_type_floor"),
            uplane_session=pem.get("uplane_session"))
        # **보관 정리를 끈다**(retain 0). 측정 중에 지우면 쓴 바이트를 셀 수
        # 없고, 임시 디렉터리라 어차피 통째로 버린다.
        writer = P.PemdbWriter(conn, mapping,
                               uplane_root=tmp / "uplane", uplane_retain_minutes=0,
                               cplane_root=tmp / "cplane", cplane_retain_minutes=0)
        umaps = P.uplane_specs(gen.series)
        enb = len({c.enb_id for c in gen.cells})
        print(f"▶ 생성 용량 측정 — eNB {enb:,} · 셀 {len(gen.cells):,}"
              f" · 기준 100% = {base_rates['lte_call']:,}호/분")
        print(f"  출력은 임시 디렉터리({tmp.name})로 보내고 지운다 — DB 는 읽기만\n")
        print(f"  {'배수':>6} {'호/분':>9} {'생성':>7} {'쓰기':>7} {'합계':>7}"
              f" {'C-Plane':>9} {'U-Plane':>9} {'레코드':>12}")
        print("  " + "─" * 76)

        anchor = _floor_minute(datetime.now())
        gen.anchor = anchor
        rows_out: list[tuple[float, float]] = []
        slot = 0
        for mult in steps:
            gen.set_volume_pct(mult * 100.0)
            # **워밍업이 필요하다.** U-Plane 은 세션 모델에서 나오고 활성
            # 세션이 분 사이에 누적되므로(평균 수명 2분), 첫 분만 재면 세션
            # 풀이 비어 **U-Plane 이 절반 아래로 과소 측정된다** — 실측에서
            # 50% 27,433건 / 100% 63,688건 처럼 배수가 어긋나 보였다.
            #
            # 비율을 바꾼 직후에도 같은 과도기가 있다. 그래서 배수마다 다시
            # 워밍업한다.
            for _ in range(max(0, int(args.warmup))):
                ts = anchor + timedelta(minutes=slot); slot += 1
                gen.records(gen.minute_of(ts), ts)
                gen.uplane_records(gen.minute_of(ts), ts)
            ts = anchor + timedelta(minutes=slot); slot += 1
            t0 = time.time()
            recs = gen.records(gen.minute_of(ts), ts)
            up = gen.uplane_records(gen.minute_of(ts), ts)
            t_gen = time.time() - t0
            t0 = time.time()
            n_c = writer.write_xdr(ts, recs)
            n_u = sum(writer.write_uplane_all(ts, up, umaps).values())
            t_io = time.time() - t0
            cp = sum(f.stat().st_size for f in (tmp / "cplane").rglob("*.parquet"))
            upb = sum(f.stat().st_size for f in (tmp / "uplane").rglob("*.parquet"))
            total = t_gen + t_io
            rows_out.append((mult, total))
            print(f"  {mult * 100:5.0f}% {gen.gen.rates['lte_call']:>9,}"
                  f" {t_gen:>6.1f}s {t_io:>6.1f}s {total:>6.1f}s"
                  f" {cp / 2**20:>8.1f}M {upb / 2**20:>8.1f}M"
                  f" {n_c + n_u:>12,}")
            # 다음 배수의 바이트를 독립으로 재려면 비워야 한다.
            shutil.rmtree(tmp / "cplane", ignore_errors=True)
            shutil.rmtree(tmp / "uplane", ignore_errors=True)
        print()
        # 선형 외삽. 생성·쓰기 모두 건수에 비례하므로 두 점이면 충분하지만,
        # **마지막 두 점을 쓴다** — 작은 배수에서는 고정비(마스터 조회 등)가
        # 섞여 기울기가 과대평가된다.
        if len(rows_out) >= 2:
            (m0, t_0), (m1, t_1) = rows_out[-2], rows_out[-1]
            slope = (t_1 - t_0) / max(1e-9, m1 - m0)
            if slope > 0:
                limit = m0 + (target - t_0) / slope
                print(f"  생성이 {target:g}초를 넘지 않는 지점: 약 {limit * 100:.0f}%"
                      f" ({int(base_rates['lte_call'] * limit):,}호/분)")
                print(f"  → 이 값을 `pemdb1.rates` 에 100% 기준으로 적는다"
                      f" (환경별 파일에서 덮으면 된다)")
        print(f"  ※ 집계·탐지·RCA 는 이 측정에 없다 — `./gp.sh logs corr` 의 틱"
              f" 시간과 합쳐 60초 예산을 본다")
        print(f"  ※ 디스크는 보관 기간과 함께 본다: C-Plane 1,440분 · U-Plane 10분")
        return 0
    finally:
        # 측정은 읽기만 했지만, 트랜잭션을 열어 둔 채 끝내면 다음 접속이
        # 옛 스냅샷을 본다(불변규칙 20 과 같은 사정).
        conn.rollback()
        conn.close()
        shutil.rmtree(tmp, ignore_errors=True)


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
    retention_hours = _retention_default(gen_cfg)

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
            # 희소 Call Type 바닥. `None` 이면 `Distributions.CALL_TYPE_FLOOR`
            # 기본값(0.3%)을 쓴다. 0 을 주면 실측 분포 그대로 돈다.
            call_type_floor=pem.get("call_type_floor"),
            # U-Plane 세션 분포 모수. 없으면 `session.Shape` 기본값이다.
            uplane_session=pem.get("uplane_session"),
        )
        # ── 생성량 비율 (2026-10-14) ──────────────────────────────────
        # **기준은 `pemdb1.rates` 의 절대값이고 비율이 그 위에 걸린다.**
        # DB(`CM_DATAGEN_CONFIG.volume_pct`)가 있으면 그것이 이기고, 없으면
        # 설정의 `rates.volume_pct` 다 — UI 에서 바꾼 값이 재기동 후에도
        # 남아야 하므로 DB 가 우선이다.
        #
        # 왜 절대값을 직접 고치지 않나: 100% 기준이 설정에 남아 있어야
        # "지금 얼마나 줄여 둔 상태인가"를 알 수 있다. 절대값만 바꾸면
        # 20,000 이 원래 값인지 누가 내린 값인지 구분되지 않는다.
        pct_default = float((pem.get("rates") or {}).get("volume_pct", 100))
        pct_now = float(control.get_config(conn, "volume_pct", str(pct_default)))
        gen.set_volume_pct(pct_now)

        # ── 장비 재배정 (2026-10-15) ──────────────────────────────────
        # 호 수는 그대로 두고 **셀이 어느 노드를 거쳤다고 기록하는지**만 바꾼다
        # — 비운 노드가 0 이 되고 받은 쪽으로 몰린다. 장애가 아니라 쏠림이다.
        ra_now = control.get_config(conn, "reassign", "") or ""
        if ra_now.strip():
            moved = gen.reassign_nodes(json.loads(ra_now))
            if moved:
                print("  장비 재배정 — "
                      + " · ".join(f"{k} {v:,}셀" for k, v in moved.items()),
                      flush=True)

        # ── 계열 차단 · 시간대 곡선 (2026-10-15) ──────────────────────
        # 둘 다 **DB 가 정본이고 매 분 다시 읽는다**(아래 틱 루프). 기동 시
        # 한 번 적용하는 것은 첫 틱이 설정 없이 돌지 않게 하려는 것이다 —
        # 전에 요율이 그 실수를 했다(기동 때만 읽어서 UI 가 안 먹었다).
        bl_now = control.get_config(conn, "blocked", "") or ""
        blocked = gen.set_blocked([x for x in bl_now.split(",") if x.strip()])
        if blocked:
            print("  계열 차단 — " + " · ".join(sorted(blocked))
                  + ("  ※ lte_call 차단은 U-Plane 까지 멈춘다"
                     if "lte_call" in blocked else ""), flush=True)
        di_now = str(control.get_config(conn, "diurnal", "true")).lower() != "false"
        gen.set_diurnal(di_now)
        if not di_now:
            print("  시간대 곡선 꺼짐 — 모든 시각이 피크(19시) 수준", flush=True)

        # U-Plane 은 MySQL 이 아니라 로컬 Parquet 로 간다(2026-10-14).
        # 계열 유무의 정본은 **생성 설정**이다 — 전에는 테이블 존재 여부였다.
        up_root, up_keep = _uplane_store(cfg, pem)
        cp_root, cp_keep = _cplane_store(cfg, pem)
        umaps = P.uplane_specs(gen.series)
        writer = P.PemdbWriter(conn, mapping, uplane_root=up_root,
                               uplane_retain_minutes=up_keep,
                               cplane_root=cp_root,
                               cplane_retain_minutes=cp_keep)

        # **anchor 는 기동 시각이다.** fault 의 시간창이 여기서부터 센다.
        # 과거로 잡으면 이미 지난 장애가 되살아난다.
        anchor = _floor_minute(datetime.now())
        gen.anchor = anchor
        up_on = [s for s in P.UPLANE_SERIES if s in umaps]
        print(f"▶ 실시간 틱 — {database} · eNB {len({c.enb_id for c in gen.cells}):,}"
              f" · 셀 {len(gen.cells):,} · 분당 {gen.gen.rates['lte_call']:,}호"
              f" ({pct_now:g}% of {rate:,}) · 보존 {retention_hours}시간")
        # **원시가 어디에 쌓이는지 먼저 말한다.** MySQL 에서 Parquet 로
        # 옮긴 뒤(2026-10-14) `gp.sh mysql "SELECT COUNT(*) ..."` 로는
        # 아무것도 안 보인다 — 그 상태를 "생성이 안 된다"로 읽지 않게 한다.
        print(f"  C-Plane — {cp_root} (Parquet · {cp_keep}분 보관)")
        # **`gen.gen.rates` 를 본다. `rates`(기준값)가 아니다.** 비율이
        # 적용된 뒤의 실제 생성량을 보여야 한다 — 기준값을 찍으면 화면·로그가
        # 데몬이 만드는 양과 다른 수를 말하고, 그러면 "왜 집계가 적은가"를
        # 진단할 때 로그가 거짓 단서가 된다.
        print("  U-Plane — "
              + (" · ".join(f"{s} {gen.gen.rates.get(s, 0):,}" for s in up_on)
                 if up_on else "꺼짐 (rates 0)")
              + (f" → {up_root} (Parquet · {up_keep}분 보관)" if up_on else ""))
        print(f"  anchor {anchor:%Y-%m-%d %H:%M} · 백필 없음"
              + (" (--once)" if args.once else " (Ctrl-C 종료)"))

        last_done: datetime | None = None
        # **기동 시각이 아니라 과거다.** `now()` 로 두면 첫 정리가 10분 뒤라,
        # 띄워 두고 바로 확인할 때 "보존이 안 돈다"로 보인다.
        last_retention = datetime.now() - timedelta(minutes=10)
        #: 마지막으로 로그에 찍은 (켜짐, 시간). 바뀔 때만 말한다 — 10분마다
        #: 같은 줄을 찍으면 하루 144줄이 되어 진짜 사건을 가린다.
        ret_state: tuple[bool, float] | None = None
        while True:
            now = datetime.now()
            pending = _floor_minute(now) - timedelta(minutes=1)   # 방금 닫힌 분
            if last_done is not None and pending <= last_done:
                nxt = _floor_minute(now) + timedelta(minutes=1, seconds=3)
                time.sleep(max(0.5, (nxt - now).total_seconds()))
                continue
            # 기동 직후 첫 틱은 **그 분부터** 시작한다 — 과거로 거슬러 가지 않는다
            ts = max(pending, anchor)

            # **설정을 읽기 전에 트랜잭션을 끝낸다.** 이 커넥션은
            # `autocommit=False` 이고 MySQL InnoDB 는 REPEATABLE READ 라,
            # 한 번 SELECT 한 뒤 커밋하지 않으면 **그 시점 스냅샷에 고정된다** —
            # UI 가 다른 커넥션에서 `CM_DATAGEN_CONFIG` 를 고쳐도 영원히 못 본다.
            #
            # 실제로 그렇게 멈췄다(2026-10-15): `enabled=false` 로 정지시킨 뒤
            # UI 에서 다시 켰는데 데몬이 계속 "생성 정지"를 찍었다. 정지
            # 분기는 적재를 건너뛰므로 **커밋할 일이 없어** 스냅샷이 그 자리에
            # 묶였고, DB 에는 `true` 가 들어 있는데 데몬만 모르는 상태가
            # 되었다. 에러가 나지 않아 "UI 가 안 먹는다"로만 보인다.
            #
            # graphio 가 같은 사고를 겪고 불변규칙 20 으로 고정해 둔 것과 같은
            # 문제다 — 엔진이 MySQL 이면 **읽기만 해도 트랜잭션이 열린다.**
            conn.commit()

            # **매 분 비율을 다시 읽는다.** UI 가 `CM_DATAGEN_CONFIG` 에 쓰면
            # 다음 분부터 반영된다(재기동 불필요). `enabled` 와 같은 경로다 —
            # 그쪽은 이미 그렇게 돌고 있었는데 요율은 기동 시 한 번만 읽어서,
            # UI 의 "다음 분부터 반영된다"는 설명이 **사실이 아니었다**.
            # 재배정도 매 분 다시 읽는다 — UI 에서 바꾸면 다음 분부터.
            ra_want = control.get_config(conn, "reassign", "") or ""
            if ra_want != ra_now:
                moved = gen.reassign_nodes(json.loads(ra_want) if ra_want.strip() else None)
                ra_now = ra_want
                print(f"[{ts:%H:%M}] 장비 재배정 — "
                      + (" · ".join(f"{k} {v:,}셀" for k, v in moved.items())
                         if moved else "해제(원래 배치로)"), flush=True)

            pct_want = float(control.get_config(conn, "volume_pct", str(pct_default)))
            if pct_want != pct_now:
                pct_now = gen.set_volume_pct(pct_want)
                print(f"[{ts:%H:%M}] 생성량 {pct_now:g}% "
                      f"(분당 {gen.gen.rates['lte_call']:,}호)", flush=True)

            # 계열 차단·시간대 곡선도 매 분 다시 읽는다 — 같은 경로다.
            bl_want = control.get_config(conn, "blocked", "") or ""
            if bl_want != bl_now:
                blocked = gen.set_blocked(
                    [x for x in bl_want.split(",") if x.strip()])
                bl_now = bl_want
                print(f"[{ts:%H:%M}] 계열 차단 — "
                      + (" · ".join(sorted(blocked)) if blocked else "해제(전부 생성)")
                      + ("  ※ lte_call 차단은 U-Plane 까지 멈춘다"
                         if "lte_call" in blocked else ""), flush=True)

            di_want = str(control.get_config(
                conn, "diurnal", "true")).lower() != "false"
            if di_want != di_now:
                gen.set_diurnal(di_want)
                di_now = di_want
                print(f"[{ts:%H:%M}] 시간대 곡선 "
                      + ("켜짐 — 04시 최저 · 19시 최고"
                         if di_want else "꺼짐 — 모든 시각이 피크 수준"),
                      flush=True)

            # ── 보존 정리 (2026-10-15) ────────────────────────────────
            # **생성 정지보다 앞이다.** 전에는 틱 맨 끝, `enabled=false` 의
            # `continue` 뒤에 있었다 — 그래서 **생성을 끄면 정리도 멈췄다.**
            # 디스크가 찬 상태에서 생성만 끄고 기다리는 것이 가장 자연스러운
            # 대응인데, 그때 아무것도 지워지지 않았다.
            #
            # 주기를 1시간에서 10분으로 당겼다. 보존을 6시간처럼 짧게 주면
            # 1시간 주기로는 최대 한 시간을 더 들고 있게 된다. 조회는
            # `information_schema` 한 번이고 떨어뜨릴 칸이 없으면 DDL 을
            # 실행하지 않는다.
            if datetime.now() - last_retention >= timedelta(minutes=10):
                # **보존은 화면에서 끌 수 있다.** 끄면 창고가 무한히 자라므로
                # 상태가 바뀔 때 로그에 남긴다 — 조용히 안 지우면 디스크가 찰
                # 때까지 아무도 모른다.
                ret_on = str(control.get_config(
                    conn, "retention_on", "true")).lower() != "false"
                ret_h = float(control.get_config(
                    conn, "retention_hours", str(retention_hours)))
                if (ret_on, ret_h) != ret_state:
                    print(f"[{ts:%H:%M}] 보존 "
                          + (f"{ret_h:g}시간" if ret_on else "정리 중지 (설정)"),
                          flush=True)
                    ret_state = (ret_on, ret_h)
                if ret_on:
                    swept = _retention_sweep(conn, ret_h)
                    if any(swept.values()):
                        print(f"  보존 정리({ret_h:g}시간) — 파티션 {swept} 드롭",
                              flush=True)
                # **`pmax` 에 행이 쌓이면 파티션 관리가 멈춘 것이다.** 그 상태는
                # 에러를 내지 않고 질의만 느려지므로, 여기서 말하지 않으면
                # 아무도 모른다. 보존을 꺼도 이 진단은 계속한다 — 둘은 다른 일이다.
                if stuck := P.pmax_rows(conn, database):
                    print(f"  ★ pmax 적적 — {stuck} · 파티션 범위를 확인하라",
                          flush=True)
                last_retention = datetime.now()

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
            # **행을 변수에 담는다.** 바이트 통계를 내려면 같은 행을 한 번 더
            # 봐야 하는데, `uplane_records` 를 두 번 부르면 rng 상태가 달라져
            # **적재한 것과 다른 데이터를 세게 된다.**
            up_rows = gen.uplane_records(minute_idx, ts)
            un = writer.write_uplane_all(ts, up_rows, umaps)
            # 그 1분만 집계한다. 구간 멱등이라 다시 돌려도 같은 값이다.
            P.aggregate_all(conn, ts, ts + timedelta(minutes=1), cp_root)
            # 분 단위 멱등이다 — 그냥 INSERT 하면 같은 분을 두 번 돌릴 때
            # 이력이 쌓여 제어 화면이 생성량을 2배·3배로 보고한다.
            control.record_tick(
                conn, ts, n, ",".join(str(f["fault_id"]) for f in active),
                series_stat=P.series_stat(rows, up_rows))
            last_done = ts
            up = " · ".join(f"{s[:4]} {v:,}" for s, v in un.items())
            print(f"[{ts:%H:%M}] XDR {n:,}건"
                  + (f" · U {up}" if up else "")
                  + f" · fault {','.join(sc.id for sc in scs) or '-'}"
                  f" · {time.time() - t0:.1f}s", flush=True)

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
        mapping = P.xdr_mapping()
        print(f"▶ 컬럼 매핑 확인 — C-Plane Parquet {len(mapping.columns)}컬럼 "
              f"({P.CPLANE_SLOT} + 생성기 필드 {len(mapping.columns) - 1})")
        up_root, up_keep = _uplane_store(cfg, pem)
        cp_root, cp_keep = _cplane_store(cfg, pem)

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
            call_type_floor=pem.get("call_type_floor"),
            uplane_session=pem.get("uplane_session"),
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

        umaps = P.uplane_specs(gen.series)
        if umaps:
            print(f"  U-Plane — {' · '.join(umaps)} → {up_root} "
                  f"(Parquet · {up_keep}분 보관)")
        else:
            print("  U-Plane — 꺼짐 (pemdb1.rates 0). 그 계열은 건너뛴다")
        print(f"  C-Plane — {cp_root} (Parquet · {cp_keep}분 보관)")
        w = P.PemdbWriter(conn, mapping, uplane_root=up_root,
                          uplane_retain_minutes=up_keep,
                          cplane_root=cp_root,
                          cplane_retain_minutes=cp_keep)
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
            # **디스크의 분 파일을 센다.** 전에는
            # `SELECT DISTINCT EVENT_TIME FROM GP_RAW_LTE_CALL_KPI` 였다.
            # 파일 이름이 분이고, 쓰기가 `os.replace` 라 이름이 있으면 그
            # 분은 완성된 것이다(`raw_parquet.minutes` 머리말).
            #
            # **보관 기간 밖은 "안 만든 것"으로 나온다.** C-Plane 1440분보다
            # 긴 백필을 `--resume` 으로 재개하면 앞쪽을 다시 만든다 — 그게
            # 맞다(파일이 없으면 집계도 다시 구울 수 없다).
            done = set(raw_parquet.minutes(
                cp_root, P.CPLANE_SERIES,
                start, start + timedelta(minutes=minutes)))
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
            res = P.aggregate_all(conn, start, hi, cp_root)
            for t, n in res.items():
                print(f"  {t:22} {n:,}행")

        if args.follow:
            _pem_follow(conn, P, gen, w, start + timedelta(minutes=minutes),
                        umaps=umaps, cplane_root=cp_root,
                        retention_hours=_retention_default(cfg["generate"]),
                        no_agg=args.no_agg, once=args.once)
    finally:
        conn.close()
    return 0


def _pem_follow(conn, P, gen, w, last_done, *, umaps: dict,
                cplane_root: Path | None,
                retention_hours: float, no_agg: bool, once: bool) -> None:
    """상용 스키마 실시간 틱 — 분이 닫히면 그 분을 생성·적재한다.

    **`pemfill` 의 준비 코드를 그대로 쓴다.** 셀 선택·가입자 풀·컬럼 매핑이
    백필과 같아야 같은 DB 안에서 데이터가 갈라지지 않는다. 구 `cmd_run` 은
    자체 창고(`graphio.kpi_cell_min` · `slot_ts`)에 쓰는 별개 경로이고, 전환 후
    **아무도 읽지 않는 DB 에 쓰고 있었다** — 에러가 안 나서 더 나빴다.

    ``last_done`` 은 **다음에 생성할 분**이다. 이미 적재한 분을 다시 만들면
    `PemdbWriter` 가 구간 멱등(DELETE→INSERT)이라 행 수는 안 늘지만, 레코드가
    통째로 바뀌어 그 분의 집계·탐지 결과가 흔들린다. 그래서 전진만 한다.

    장애는 `CM_DATAGEN_FAULT` 에서 틱마다 읽는다(`control.poll_transitions`).
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
            # **생성은 재시도 루프 밖에서 한 번만 한다.** U-Plane 세션 엔진이
            # 활성 세션을 분 사이에 들고 있어(`session.SessionEngine`) 같은
            # 분을 두 번 생성하면 세션이 두 칸 전진하고 **같은 세션의 레코드가
            # 두 줄 나온다.** 적재는 구간 멱등이지만 생성은 아니다.
            up_rows = gen.uplane_records(minute_idx, pending)

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
                    un = w.write_uplane_all(pending, up_rows, umaps)
                    if not no_agg:
                        P.aggregate_all(conn, pending,
                                        pending + timedelta(minutes=1),
                                        cplane_root)
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
                    res = _purge_pemdb1(conn, retention_hours)
                except Exception as exc:          # noqa: BLE001
                    conn.rollback()
                    print(f"  ! 보존 정리 실패 — {exc}", flush=True)
                    next_purge = datetime.now() + timedelta(hours=1)
                    if once:
                        return
                    continue
                total = sum(res.values())
                if total:
                    print(f"  보존 정리({retention_hours:g}시간) — "
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
    b = sub.add_parser("backfill", help="※ 막혀 있다 — 치면 이유를 말한다 (구 창고 경로 · 과거 덮어쓰기 위험)")
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

    # 상용 스키마 경로 — `run` 과 같은 쓰기 경로다 (`cmd_run` docstring 참조)
    p = sub.add_parser("pemfill", help="생성·적재 + 1분 집계 (pemdb6) — --follow 가 실시간 데몬")
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

    # **이 문구가 한동안 틀려 있었다.** 구 구현이 `graphio` DB 에 쓰던 시절의
    # 설명("구 창고 전용 · graphio 는 안 읽는다")이 남아 있었고, 그 뒤 쓰기
    # 경로를 `pemfill` 과 같게 고쳤는데 help 만 그대로였다 — 읽은 사람이
    # `gp.sh start gen` 이 엉뚱한 DB 에 쓴다고 오판했다(실제로 그랬다).
    r = sub.add_parser("run", help="상주 데몬 — pemfill --follow 와 같은 쓰기 경로 (백필 없음)")
    r.add_argument("--config")
    r.add_argument("--once", action="store_true", help="틱 1회만 수행하고 종료 (테스트용)")
    r.set_defaults(fn=cmd_run)

    c = sub.add_parser("capacity",
                       help="분당 생성 용량 측정 — 100% 기준을 정하는 근거")
    c.add_argument("--config")
    c.add_argument("--steps", default="0.5,1,2,4",
                   help="측정할 배수 (기본 0.5,1,2,4 — 1 이 설정의 100%%)")
    c.add_argument("--warmup", type=int, default=3,
                   help="배수마다 버리는 워밍업 분 수 (기본 3 — 세션 풀을 채운다)")
    c.add_argument("--target", type=float, default=20.0,
                   help="생성에 허용할 초 (기본 20 = 60초 틱 예산의 1/3)")
    c.set_defaults(fn=cmd_capacity)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
