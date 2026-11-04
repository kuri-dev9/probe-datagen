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
import sys
import time
import traceback
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path

import yaml

from . import control, raw_parquet, region, topo

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
    # **지역축은 env 뒤다** (2026-10-28). `GP_MYSQL_DB` 가 `pemdb1` 절을
    # 만들 수 있으므로 그 뒤에 `setdefault` 해야 `locale` 칸이 확실히 선다.
    # 유도는 **빈 칸만** 채운다 — 적힌 값이 이긴다(`region` 규칙 ②).
    cfg = region.derive(_apply_env(base))
    _warn_region_once(cfg)
    return cfg


#: 지역축 경고를 **프로세스당 한 번만** 찍는다. `load_cfg` 는 커맨드마다
#: 불리고 UI 는 요청마다 설정을 보므로, 매번 찍으면 로그가 그 경고로 덮인다 —
#: 그러면 진짜 봐야 할 줄이 묻히고 사람이 경고 전체를 무시하게 된다.
_REGION_WARNED = False


def _warn_region_once(cfg: dict) -> None:
    """지역축이 어긋나 있으면 **stderr 로** 알린다. 멈추지 않는다.

    `region` 규칙 ③ — 그 조합이 유효한 경우가 있다(마스터만 먼저 옮기는
    중, 로밍 테스트). 막으면 그 작업이 불가능해지고, **조용히 두면 아무도
    모른다.** 중간을 택한다.

    **stdout 이 아니다.** `topo survey` 처럼 출력을 파이프로 받는 커맨드가
    있어 섞이면 파싱이 깨진다.
    """
    global _REGION_WARNED
    if _REGION_WARNED:
        return
    _REGION_WARNED = True
    text = region.conflict_text(cfg)
    if text:
        print(text, file=sys.stderr)


def apply_region_override(cfg: dict, conn) -> str:
    """UI 가 저장한 지역을 **기동 시 한 번** 반영한다. 틱마다가 아니다.

    `volume_pct`·`blocked` 와 같은 자리(`CM_DATAGEN_CONFIG`)에 저장하지만
    **읽는 시점이 다르다** — 저쪽은 데몬이 틱마다 다시 읽어 다음 분부터
    반영되고, 지역축은 여기서 한 번이다.

    **중간에 바뀌면 안 되기 때문이다**(사용자 확정 2026-10-28). 같은 분의
    Parquet 에 한국 IMSI 와 일본 IMSI 가 섞이면 그 분은 **어느 쪽으로도
    집계되지 않는다** — 가입자 축 분모가 두 지역으로 갈라지고 에러는 나지
    않는다.

    우선순위는 `datagen.yaml` 의 `locale:` < DB(UI) 다. UI 에서 바꾼 값이
    재기동 후에도 남아야 하므로 DB 가 이긴다 — `volume_pct` 와 같은 근거다.
    **단 사람이 yaml 에 적은 `plmn.*` 는 여전히 이긴다**(`region` 규칙 ②).
    그래서 지역만 바뀌고 PLMN 이 안 따라올 수 있고, 그 어긋남을 아래
    `conflict_text` 가 찍는다.
    """
    cur = region.resolve(cfg)
    saved = (control.get_config(conn, "locale") or "").strip().lower()
    if saved and saved != cur:
        if saved not in region.LOCALES:
            # **멈추지 않는다** — 데몬이 뜨지 않는 것보다 설정 파일 값으로
            # 도는 쪽이 낫다. 다만 왜 무시했는지 말한다.
            print(f"  ⚠ DB 에 저장된 locale 이 이상하다: {saved!r} — 무시하고 "
                  f"설정 파일의 {cur} 로 돈다", file=sys.stderr, flush=True)
        else:
            region.reapply(cfg, saved)
            print(f"  지역축 — UI 저장값 {saved} 적용 "
                  f"(설정 파일은 {cur})", flush=True)
            cur = saved
    text = region.conflict_text(cfg)
    if text:
        print(text, file=sys.stderr, flush=True)
    return cur


#: 환경변수 → 설정 키. **graphio 쪽 `config._ENV_KEYS` 와 같은 변수 이름을
#: 쓴다** (2026-10-20). 같은 DB 를 보는 두 프로세스가 서로 다른 변수를 읽으면
#: 한쪽만 고쳐질 때 **에러 없이** gen 과 corr 이 다른 창고를 본다.
#:
#: 접속 정보가 전에는 `datagen.yaml`(추적본)과 `prod.yaml` 두 벌에 평문으로
#: 있었고, graphio 쪽까지 합치면 환경마다 네 자리였다. 이제 `.env` 하나에
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
    갖고 돌고 있으므로 그쪽이 `.env` 를 고치기 전에 받아도 멈추지 않는다.
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


def _capacity_shift(pem: dict) -> dict | None:
    """`pemdb1.capacity_shift` — `file` 이 있으면 그 JSON 을 읽어 합친다.

    **eNB ID 를 추적본 yaml 에 적지 않는다.** 이 저장소는 PUBLIC 이고 값은
    실제 일본 망 eNB 번호다 — 야마노테선 노선 파일(`data/lines/`)과 같은
    이유로 `data/`(gitignore)에 둔다. 환경마다 한 번 만든다.

    파일을 적었는데 없으면 **멈추지 않고 크게 알린다.** 24번 하나가 해석
    실패로 취소될 뿐 생성 전체를 세울 일이 아니다 — 노선 파일과 다른 점은
    그쪽은 없으면 이동 가입자 전부가 엉뚱하게 깔린다는 것이다.
    """
    sec = dict(pem.get("capacity_shift") or {})
    f = sec.pop("file", None)
    if not f:
        return sec or None
    path = Path(f) if Path(f).is_absolute() else HERE / f
    if not path.exists():
        print(f"  ! capacity_shift 파일이 없다 — {path} · 24번(증설 부하 이동)은 "
              "주입해도 취소된다", flush=True)
        return sec or None
    sec.update(json.loads(path.read_text(encoding="utf-8")))
    return sec


def _truth_path(pem: dict, data_dir: Path) -> Path:
    """`ground_truth.json` 경로. 상대경로는 **저장소 루트 기준**으로 푼다 —
    cwd 에 따라 정답이 엉뚱한 자리에 떨어지면 graphio 는 "ground_truth.json
    없음" 으로 조용히 채점을 건너뛴다."""
    q = Path(pem.get("truth_out") or (data_dir / "truth" / "ground_truth.json"))
    return q if q.is_absolute() else (HERE / q).resolve()


#: `_aside` 가 마지막으로 말한 실패 사유. 같은 사유를 매 틱 도배하지 않기 위한
#: 것이고, 사유가 바뀌거나 복구되면 다시 말한다.
_ASIDE_FAILED: dict[str, str] = {}


def _aside(label: str, fn, *args, **kw):
    """**주 경로를 멈추지 않는 부수 작업.**

    이 데몬의 산출물은 **원시 Parquet 생성·적재 하나**다. 정답 기록 ·
    보존 정리 · 파티션 진단은 전부 "알면 좋은 것"이고, 실패해도 데이터는
    멀쩡해야 한다.

    그런데 `_live_truth` 가 생성보다 **먼저** 불리고 있었다. 정답 파일
    경로가 컨테이너에서 `/graphio-probe-rca` 로 풀려 `PermissionError` 가
    나자 **그 틱의 xDR 이 통째로 만들어지지 않았고** 데몬이 재시작을
    반복했다 — 장애를 처음 주입한 순간이었다(2026-10-27 실측). 데이터에는
    아무 문제가 없었는데 생성이 멈췄다.

    **조용히 넘기지 않는다.** 처음 한 번은 트레이스백까지 찍고, 같은 사유가
    이어지면 더 말하지 않는다 — 매 틱 도배하면 진짜 로그가 묻힌다. 사유가
    바뀌거나 복구되면 다시 말한다. 상태를 남기는 이유는 "정답이 밀려 있다"를
    알아야 채점 결과를 믿을지 판단할 수 있어서다.
    """
    try:
        out = fn(*args, **kw)
    except Exception as exc:                      # noqa: BLE001 — 주 경로 보호가 목적
        sig = f"{type(exc).__name__}: {exc}"
        if _ASIDE_FAILED.get(label) != sig:
            _ASIDE_FAILED[label] = sig
            print(f"  ⚠ {label} 실패 — {sig}", flush=True)
            print("     **생성·적재는 계속한다.** 이 작업만 건너뛴다.",
                  flush=True)
            traceback.print_exc()
        return None
    if _ASIDE_FAILED.pop(label, None) is not None:
        print(f"  ✓ {label} 복구", flush=True)
    return out


def _live_truth(P, gen, scs: list) -> None:
    """실시간 주입(UI·`CM_DATAGEN_FAULT`)의 정답을 남긴다.

    **전에는 `pemfill --scenarios` 만 정답을 썼다.** UI 로 주입한 장애는
    `ground_truth.json` 에 없으니 채점 모집단에 들어가지 않고, RCA 가
    맞혔는지 틀렸는지 **알 수 없는 상태로 남는다**(V3 §4.5 · 1차 ⑫).

    활성 집합이 바뀔 때만 쓴다 — 틱마다 쓰면 분당 한 번 디스크를 건드리고
    파일 시각이 계속 바뀌어 "언제 주입됐나"를 읽을 수 없다.

    **활성 장애가 없으면 덮지 않는다.** 장애가 끝나면 `poll_transitions` 가
    목록에서 빼는데, 그때 빈 파일을 쓰면 **방금 끝난 장애의 정답이 사라진다** —
    채점은 장애가 끝난 뒤에 돌린다.
    """
    # **채점 문항만 센다**(2026-10-29). 24번(증설 재현, `scored=False`)만
    # 걸려 있을 때 그대로 쓰면 `write_truth` 가 그것을 빼고 **빈 목록을
    # 써서 직전 장애의 정답을 지운다** — 실제로 한 번 지워졌다.
    scs = [sc for sc in scs if getattr(sc, "scored", True)]
    path = getattr(gen, "truth_path", None)
    if path is None or gen.anchor is None or not scs:
        return
    key = tuple(sorted((sc.id, sc.kind, sc.start_minute, sc.duration)
                       for sc in scs))
    if key == getattr(gen, "_truth_key", None):
        return
    minutes = max(sc.start_minute + sc.duration for sc in scs) + 1
    P.write_truth(path, gen.anchor, minutes, scs,
                  getattr(gen, "cell_selection", None))
    # **쓰기에 성공한 뒤에 키를 갱신한다.** 먼저 갱신하면 실패했을 때 같은
    # 활성 집합이 이어지는 한 다시 시도하지 않아, 정답이 영영 비어 있는데도
    # 조용하다. 뒤에 두면 다음 틱이 자동으로 재시도한다.
    gen._truth_key = key
    print(f"  정답 기록 — {len(scs)}건 → {path}", flush=True)


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
            # 홈 PLMN·IMSI 접두는 **설정 하나**에서 온다 (`plmn.Plmn` 머리말)
            plmn=P.Plmn.from_cfg(cfg.get("plmn")),
            success_only=bool(pem.get("success_only", True)),
            enb_limit=int(pem.get("enb_limit", 0)), drawable=drawable,
            call_type_floor=pem.get("call_type_floor"),
            uplane_session=pem.get("uplane_session"), mobility=pem.get("mobility"),
            net_mode=pem.get("net_mode"),
            locale=pem.get("locale", "kr"), tac_only=bool(pem.get("tac_only", False)),
            sectors_per_enb=pem.get("sectors_per_enb"), scope=pem.get("scope"),
            baseline=pem.get("baseline"), fault=pem.get("fault"),
            capacity_shift=_capacity_shift(pem))
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
        # ── 지역축 — **기동 시 한 번만** (2026-10-28) ──────────────────
        # 아래 `PemdbGen(plmn=…, locale=…)` 이 cfg 를 읽으므로 **그 전에**
        # 반영해야 한다. 뒤로 가면 데몬은 옛 지역으로 돌면서 화면만 새 값을
        # 보여 주고, 그 어긋남은 IMSI 를 들여다봐야 드러난다.
        apply_region_override(cfg, conn)
        pem = cfg.get("pemdb1") or {}
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
            # 홈 PLMN·IMSI 접두는 **설정 하나**에서 온다 (`plmn.Plmn` 머리말)
            plmn=P.Plmn.from_cfg(cfg.get("plmn")),
            success_only=bool(pem.get("success_only", True)),
            enb_limit=int(pem.get("enb_limit", 0)),
            # 셀 좌표 결손을 **생성기가 채운다** — 수동 백필
            # (`topo fill-coords`)을 대체한다. 의도된 미매핑 eNB 는
            # 건드리지 않는다(`topo.coord_gaps`).
            ensure_coords=True,
            drawable=drawable,
            # 희소 Call Type 바닥. `None` 이면 `Distributions.CALL_TYPE_FLOOR`
            # 기본값(0.3%)을 쓴다. 0 을 주면 실측 분포 그대로 돈다.
            call_type_floor=pem.get("call_type_floor"),
            # U-Plane 세션 분포 모수. 없으면 `session.Shape` 기본값이다.
            uplane_session=pem.get("uplane_session"), mobility=pem.get("mobility"),
            net_mode=pem.get("net_mode"),
            locale=pem.get("locale", "kr"), tac_only=bool(pem.get("tac_only", False)),
            sectors_per_enb=pem.get("sectors_per_enb"), scope=pem.get("scope"),
            # 평시 배경(절차 플래그·절단·PathSwitch)과 TIMEOUT 대기시간.
            # 없으면 `pemdb1.Baseline`·`scenarios.FaultCfg` 기본값이다.
            baseline=pem.get("baseline"), fault=pem.get("fault"),
            capacity_shift=_capacity_shift(pem),
        )
        # 실시간 주입도 정답을 남긴다 — `_live_truth` 가 활성 집합이 바뀔 때 쓴다
        gen.truth_path = _truth_path(pem, data_dir)
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
        tail_last: str | None = None   # 하한 꼬리 — 첫 틱에 반드시 쓴다
        # 기본값은 설정(`pemdb1.clock`), UI 저장값(DB)이 이긴다 — `volume_pct` 와 같다
        clk = pem.get("clock") or {}
        from .generator import set_curve
        set_curve(clk.get("curve"))          # 곡선 정본은 설정이다(`generator.set_curve`)
        di_default = "false" if clk.get("diurnal") is False else "true"
        vo_default = str(60.0 * float(clk.get("virtual_offset_min", 0) or 0))
        di_now = str(control.get_config(conn, "diurnal", di_default)).lower() != "false"
        gen.set_diurnal(di_now)
        if not di_now:
            print("  시간대 곡선 꺼짐 — 피크 수준 평탄 · 그룹·이동 없이 전체 eNB 에 골고루", flush=True)
        vo_now = gen.set_virtual_offset(float(control.get_config(conn, "virtual_offset_sec", vo_default) or 0))
        if vo_now:
            print(f"  가상 시각 {vo_now / 60:+.0f}분 — 곡선·그룹 비율만 옮긴다(레코드 시각은 실제 시각)",
                  flush=True)

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
                conn, "diurnal", di_default)).lower() != "false"
            if di_want != di_now:
                gen.set_diurnal(di_want)
                di_now = di_want
                print(f"[{ts:%H:%M}] 시간대 곡선 "
                      + ("켜짐 — 04시 최저 · 19시 최고"
                         if di_want else "꺼짐 — 피크 수준 평탄 · 전체 eNB 에 골고루"),
                      flush=True)
            vo_want = gen.set_virtual_offset(float(
                control.get_config(conn, "virtual_offset_sec", vo_default) or 0))
            if vo_want != vo_now:
                vo_now = vo_want
                print(f"[{ts:%H:%M}] 가상 시각 "
                      + (f"{vo_now / 60:+.0f}분" if vo_now else "해제 — 지금 시각"), flush=True)

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
                    swept = _aside("보존 정리", _retention_sweep,
                                   conn, ret_h) or {}
                    if any(swept.values()):
                        print(f"  보존 정리({ret_h:g}시간) — 파티션 {swept} 드롭",
                              flush=True)
                # **`pmax` 에 행이 쌓이면 파티션 관리가 멈춘 것이다.** 그 상태는
                # 에러를 내지 않고 질의만 느려지므로, 여기서 말하지 않으면
                # 아무도 모른다. 보존을 꺼도 이 진단은 계속한다 — 둘은 다른 일이다.
                if stuck := _aside("pmax 진단", P.pmax_rows,
                                   conn, database):
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
            _aside("정답 기록", _live_truth, P, gen, scs)

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
            # U-Plane 은 **같은 분 좌표**로 만든다. 다른 좌표를 쓰면 같은 분의
            # C-Plane 과 시나리오 강도가 어긋나 한 장애가 두 축에서 다른 시각에
            # 나타난다.
            # **행을 변수에 담는다.** 바이트 통계를 내려면 같은 행을 한 번 더
            # 봐야 하는데, `uplane_records` 를 두 번 부르면 rng 상태가 달라져
            # **적재한 것과 다른 데이터를 세게 된다.**
            up_rows = gen.uplane_records(minute_idx, ts)
            # **C-Plane 을 마지막에 쓴다** (2026-10-29). corr 는 C-Plane 분
            # 파일이 보이면 그 분을 굽고 다시 굽지 않는다(graphio
            # `pipeline.run_tick` ③). U-Plane 이 뒤에 오면 그 틈에 구운 분의
            # U-Plane 이 영구히 빈다. 생성 순서(rng)는 그대로 두고 쓰기만 바꾼다.
            un = writer.write_uplane_all(ts, up_rows, umaps)
            n = writer.write_xdr(ts, rows)
            # 그 1분만 집계한다. 구간 멱등이라 다시 돌려도 같은 값이다.
            P.aggregate_all(conn, ts, ts + timedelta(minutes=1), cp_root)
            # 분 단위 멱등이다 — 그냥 INSERT 하면 같은 분을 두 번 돌릴 때
            # 이력이 쌓여 제어 화면이 생성량을 2배·3배로 보고한다.
            control.record_tick(
                conn, ts, n, ",".join(str(f["fault_id"]) for f in active),
                series_stat=P.series_stat(rows, up_rows))
            # 하한 꼬리 상태를 화면에 알린다(`PemdbGen.floor_tail_until`). 바뀔
            # 때만 쓴다 — 기동 직후 첫 틱은 반드시 쓴다(재시작으로 꼬리가
            # 사라졌다는 사실까지 덮어야 한다)
            tail_now = control.floor_tail_value(gen.floor_tail_until())
            if tail_now != tail_last:
                control.set_config(conn, control.FLOOR_TAIL_KEY, tail_now)
                tail_last = tail_now
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
        # ── 지역축 — `cmd_run` 과 같은 자리에서 같은 규칙으로 (2026-10-28) ──
        # **여기도 반영해야 한다.** `pemfill` 이 과거 구간을 메우는데 데몬과
        # 다른 지역으로 돌면 같은 창고에 한국·일본 IMSI 가 섞이고, 그것은
        # 분 단위 섞임(`apply_region_override` 머리말)보다 더 찾기 어렵다 —
        # 구간이 통째로 다른 나라가 된다.
        control.ensure_tables(conn)
        apply_region_override(cfg, conn)
        pem = cfg.get("pemdb1") or {}
        mapping = P.xdr_mapping()
        print(f"▶ 컬럼 매핑 확인 — C-Plane Parquet {len(mapping.columns)}컬럼 "
              f"({P.CPLANE_SLOT} + 생성기 필드 {len(mapping.columns) - 1})")
        up_root, up_keep = _uplane_store(cfg, pem)
        cp_root, cp_keep = _cplane_store(cfg, pem)

        # **백필은 요청 구간 전체를 남긴다** (2026-10-22).
        #
        # `raw_parquet.purge` 는 "디스크의 가장 늦은 분 − keep" 을 자른다.
        # 실시간 틱에서는 그것이 곧 지금이라 10분 보관이 뜻대로 동작하지만,
        # `pemfill` 은 과거 구간을 **앞에서 뒤로** 채우므로 마지막 분을 쓰는
        # 순간 앞 `minutes − keep` 분이 통째로 사라진다. U-Plane 기본 보관이
        # 10분이라 40분 백필에서 **30분치가 집계되기 전에 지워졌다** —
        # `PM_COR_UPLANE_CELL_1M` 에 뒤 10분만 남고, U-12·U-29·U-23·U-25
        # 탐지가 구조적으로 돌 수 없었다(에러는 나지 않는다).
        #
        # C-Plane 은 보관 1440분이라 같은 일이 40분 백필에서는 드러나지
        # 않았을 뿐 조건은 같다 — 하루를 넘겨 채우면 똑같이 잘린다.
        #
        # **보관 자체를 끄지는 않는다.** 요청 구간 밖(이전 실행이 남긴 분)은
        # 그대로 정리되어 디스크가 무한히 자라지 않는다.
        up_keep, cp_keep = max(up_keep, minutes), max(cp_keep, minutes)

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
            # 홈 PLMN·IMSI 접두는 **설정 하나**에서 온다 (`plmn.Plmn` 머리말)
            plmn=P.Plmn.from_cfg(cfg.get("plmn")),
            # 실측 실패·절단 비율을 그대로 재현하는 모드. 기본은 성공 전용
            # (평시 전부 녹색)이고, Cause 집계 경로를 확인할 때 쓴다.
            success_only=(False if args.with_failures
                          else bool(pem.get("success_only", True))),
            enb_limit=(args.enb_limit if args.enb_limit is not None
                       else int(pem.get("enb_limit", 0))),
            # cmd_run 과 같다 — 생성 주체가 좌표를 만든다
            ensure_coords=True,
            drawable=drawable,
            call_type_floor=pem.get("call_type_floor"),
            uplane_session=pem.get("uplane_session"), mobility=pem.get("mobility"),
            net_mode=pem.get("net_mode"),
            locale=pem.get("locale", "kr"), tac_only=bool(pem.get("tac_only", False)),
            sectors_per_enb=pem.get("sectors_per_enb"), scope=pem.get("scope"),
            baseline=pem.get("baseline"), fault=pem.get("fault"),
            capacity_shift=_capacity_shift(pem),
        )
        gen.truth_path = _truth_path(pem, data_dir)
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
            truth_path = (Path(args.truth_out) if args.truth_out
                          else _truth_path(pem, data_dir))
            if not truth_path.is_absolute():
                truth_path = (HERE / truth_path).resolve()
            P.write_truth(truth_path, start, minutes, scs,
                          getattr(gen, "cell_selection", None))
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
            _aside("정답 기록", _live_truth, P, gen, scs)

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
                    # C-Plane 이 분 완료 표식이다 — 마지막에 쓴다(`cmd_run` 주석).
                    un = w.write_uplane_all(pending, up_rows, umaps)
                    n = w.write_xdr(pending, rows)
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


def _pad(text: str, width: int) -> str:
    """표 정렬 — **한글은 두 칸을 차지한다.** `f"{s:22}"` 는 코드포인트를 세므로
    한글 라벨이 섞인 열이 어긋나고, 그러면 숫자 열을 잘못 읽는다."""
    w = sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)
    return text + " " * max(1, width - w)


#: **쓰는 명령.** 이 목록에 있으면 `--write` 없이는 아무것도 적재하지
#: 않는다. `list`·`restore`(점검)·`dump`·`geo-fetch` 는 여기 없다 —
#: 전자는 읽기이고 `dump` 는 사본을 뜨는 것이 목적이다.
TOPO_WRITING = ("clone", "ensure-tables", "copy-cells", "copy-enb-org",
                "code-map", "seed-geo", "seed-enb", "seed-enb-jp",
                "rm-added", "relocate-cells", "org-names",
                "fill-coords", "add", "rm", "update")


def _topo_gate(conn, locale: str, schema: str | None = None) -> bool:
    """파괴적 경로의 선행 조건 — **`restore` 가 실제로 넣을 수 있는가.**

    ## **KR 에만 건다** (2026-10-26 접미사 벌로 바뀐 뒤)

    JP 벌은 **재생성 가능하다** — `ensure-tables` → `copy-cells` →
    `seed-geo` → `relocate-cells` 네 단계가 전부 코드와 경계 GeoJSON 에서
    나온다. 되돌릴 원본이 없는 쪽은 KR 이고(`pemdb6` 가 그대로 운영이다),
    JP 에 KR 사본을 요구하면 **JP 를 만들기 전에는 아무 버튼도 눌리지
    않는다**(JP 사본이 있을 수 없으므로). 게이트가 그렇게 서면 사람은
    게이트를 우회할 길을 찾는다.

    **파일의 존재는 복귀를 뜻하지 않는다.** 전에는 `dump_exists()` 가
    게이트였는데, 그때 그 JSON 을 DB 로 되넣는 코드가 없었다 — 게이트가
    통과시키는 조건이 "되돌릴 수 있다"가 아니라 "파일 4개가 존재한다"였고
    그것은 안심만 준다. 그래서 **읽어서 넣을 수 있는지**를 묻는다
    (`topo.restore(dry_run=True)` 는 아무것도 쓰지 않는다).

    실패 사유를 테이블별로 찍는다 — "사본이 없다" 하나로는 넷 중 어느
    파일이 반쪽인지 알 수 없고, 그러면 사람은 전부 다시 뜬다.
    """
    if locale != "kr":
        print(f"  (복귀 사본 게이트는 KR 에만 건다 — locale {locale} 벌은 "
              f"재생성 경로가 있다: ensure-tables → copy-cells → seed-geo "
              f"→ relocate-cells)")
        return True
    rep = topo.restore(conn, locale, dry_run=True)
    if rep["ok"]:
        return True
    # **어느 스키마를 지우는가**를 함께 말한다. 전에는 KR 전제로 적혀
    # 있어서, JP 스키마 작업인데 KR 사본을 뜨라는 경고가 나왔다 — 그러면
    # 사람이 "내가 KR 을 건드리나" 하고 멈춘다.
    print(f"✗ 복귀 경로가 서 있지 않다 — 대상 스키마 {schema or '?'} · "
          f"사본 {topo.LOCALE_DIR / locale}", file=sys.stderr)
    for info in rep["tables"].values():
        if info.get("error"):
            print(f"    {info['table']:22} {info['error']}", file=sys.stderr)
    print(f"  먼저 떠라 — python -m datagen topo dump {locale}", file=sys.stderr)
    return False


def _limit_advice(rep: dict) -> str:
    """대역 eNB 가 선택에서 빠졌을 때 쓰는 한 줄 — **이제는 버그 신호다.**

    `pemdb1._pick_enbs` 가 `topo` 대역을 **한도 밖에서 항상 포함**하므로
    (2026-10-26), 후보에 들어온 대역 eNB 가 선택에서 빠지는 일은 없다.
    그래도 이 가지를 남기는 이유는, 빠졌다면 그것이 **include 절이 깨졌거나
    `_filter_drawable` 이 그 셀을 전부 버렸다**는 뜻이고 둘 다 조용히
    일어나기 때문이다.

    **한도를 올리라는 안내도, `--cells` 를 늘리라는 안내도 하지 않는다.**
    전자는 실측 23,586 이라 한도가 아니라 해제이고, 후자는 선택 규칙을
    통과하려고 **자산 대장을 부풀리는 것**이다(시군구마다 37셀 eNB 는 거짓
    마스터이고, 기존 eNB 를 밀어내 `ground_truth` 정답도 깬다).
    """
    return (f"`_pick_enbs` 의 topo 대역 include 가 동작하지 않았다 "
            f"(한도 {rep['enb_limit']}). 대역 판정(`topo.in_new_band`)과 "
            f"`_filter_drawable` 이 그 셀을 버렸는지를 본다 — "
            f"한도를 올려 해결하지 않는다")


def _topo_geo_dir(cfg: dict) -> Path:
    """경계 GeoJSON 디렉터리.

    **`pemdb1.geo_crosswalk` 가 가리키는 자리와 같은 디렉터리**다 — 그 파일이
    그 안에 있고(`code_crosswalk.json`), `seed-geo` 가 그것을 다시 쓴다.
    둘을 다른 데서 찾으면 생성한 대응과 읽는 대응이 갈라진다.
    """
    raw = (cfg.get("pemdb1") or {}).get("geo_crosswalk")
    if raw:
        p = Path(str(raw))
        return (p if p.is_absolute() else (HERE / p).resolve()).parent
    raise RuntimeError(
        "pemdb1.geo_crosswalk 가 설정에 없다 — 경계 디렉터리를 알 수 없다.\n"
        "  --geo-dir 로 직접 주거나 설정에 그 키를 넣는다")


def _topo_xw_path_opt(cfg: dict, override: str | None) -> Path | None:
    """crosswalk 경로 — **`pemfill` 과 같은 키를 읽는다**(`pemdb1.geo_crosswalk`).

    여기서 다른 키를 보거나 기본값을 지어내면 `topo` 가 놓은 동과 `pemfill`
    이 그릴 수 있다고 믿는 동이 **다른 집합**이 되고, 에러 없이 셀이 버려진다.

    설정에 없으면 `None` 이다 — `pemfill` 도 그 경우 `drawable=None` 으로
    전량을 후보로 둔다. 조회(`list`)는 그 상태를 그대로 대조해야 한다.
    """
    raw = override or (cfg.get("pemdb1") or {}).get("geo_crosswalk")
    if not raw:
        return None
    path = Path(str(raw))
    return path if path.is_absolute() else (HERE / path).resolve()


def _topo_xw_path(cfg: dict, override: str | None) -> Path:
    """위와 같지만 **없으면 멈춘다.** 생성(`seed-enb`·`fill-coords`)은 그릴 수
    있는 동만 써야 하므로, 집합을 모르는 채 놓으면 `pemfill` 이 조용히 버린다."""
    path = _topo_xw_path_opt(cfg, override)
    if path is None:
        raise RuntimeError(
            "pemdb1.geo_crosswalk 가 설정에 없다 — topo 는 그릴 수 있는 동만 쓴다")
    return path


def cmd_topo(args) -> int:
    """마스터 토폴로지 — 1회성 관리 커맨드. 상주 경로(`pemfill`)와 분리돼 있다.

    **`pemfill` 과 함께 돌리지 않는다.** 그쪽이 마스터를 읽어 셀 목록을
    만드는 중에 이쪽이 행을 넣으면, 같은 분의 생성과 집계가 서로 다른 셀
    집합을 보게 된다 — 에러가 아니라 값이 어긋난다.
    """
    cfg = load_cfg(args.config)
    # **locale 은 스키마를 가르지 않는다** (2026-10-26 방식 확정). 로케일
    # 축이 테이블 이름으로 내려왔으므로(`topo.table_name`), 같은 `pemdb6`
    # 안에서 `CM_GU` 와 `CM_GU_JP` 가 공존한다. 둘을 함께 쓰면 축이 두 번
    # 적혀(`pemdb6_jp.CM_GU_JP`) 어느 쪽이 유효한지 알 수 없다.
    #
    # **`clone` 만 예외다** — 그 커맨드 자체가 "다른 스키마를 만든다" 이고
    # 폐기 대기 경로다(`topo.locale_database` 머리말).
    # ── **쓰기는 `--write` 가 있을 때만 한다** (2026-10-26) ─────────────
    #
    # 전에는 `--dry-run` 이 `store_true` 라 **기본이 적재**였다. 함수
    # 기본값(`dry_run=True`)만 보고 "기본이 dry-run" 이라 믿었는데 CLI 는
    # 반대였고, 그래서 **`topo seed-geo --replace` 한 줄이 그대로 적재**였다.
    # 오타 하나(`--locale` 누락)가 KR 세 테이블을 덮는 자리다.
    #
    # `--dry-run` 은 **계속 받는다** — 기존 호출이 조용히 적재로 바뀌면 안
    # 된다. 지금은 그것이 기본이므로 "이미 기본"이라고 말해 준다.
    write = bool(getattr(args, "write", False))
    dry = not write
    src = str((cfg.get("pemdb1") or {}).get("database", "pemdb6"))
    db = (topo.locale_database(cfg, args.locale, args.database)
          if args.action == "clone" else (args.database or src))
    tag = f"locale {args.locale} · 스키마 {db}"
    # **대상 테이블 이름을 함께 찍는다.** 로케일 축이 테이블 이름이라
    # "어느 스키마인가" 만으로는 KR 을 건드리는지 알 수 없다 — 접미사가
    # 붙었는지가 그 판단이다.
    try:
        tag += ("  대상 테이블 "
                + " · ".join(topo.table_name(k, args.locale)
                             for k in topo.LOCALE_TABLE_KEYS))
    except RuntimeError:
        pass
    print(f"▶ 대상: {tag}")
    if args.action in TOPO_WRITING and not write:
        print("  (읽기만 한다 — 적재는 `--write` 를 붙여야 돈다)")
    elif args.action in TOPO_WRITING and args.dry_run:
        print("  (`--dry-run` 은 이제 기본이다 — `--write` 가 없으면 쓰지 않는다)")
    # **`clone` 은 원본에 붙는다.** 대상 스키마는 아직 없고(그걸 만드는
    # 명령이다) 없는 DB 로 접속하면 `1044 Access denied to database` 가
    # 난다 — 권한 문제처럼 보이지만 실은 "아직 없다"다.
    conn = topo.connect(cfg, src if args.action == "clone" else db)
    if args.action in TOPO_WRITING and write:
        # **적재 직전에 대상과 현재 행 수를 찍는다.** 오타 하나(`--locale`
        # 누락)가 KR 을 덮는 자리다 — 사람이 스키마 이름과 행 수를 보고
        # "내가 맞는 DB 를 보고 있나"를 판단할 수 있어야 한다.
        try:
            # **그 로케일 벌의 현재 행 수다.** 없으면 "없다" 로 적는다 —
            # 0 으로 적으면 "만들었는데 비었다" 로 읽힌다(불변규칙 17).
            def _cnt(key: str) -> str:
                t = topo.table_name(key, args.locale)
                if not topo.table_exists(conn, t):
                    return f"{t} 없음"
                n = topo._rows(conn, f"SELECT COUNT(*) FROM `{t}`")[0][0]
                return f"{t} {n:,}"
            print(f"  ✎ **쓴다** — {db} · 현재 "
                  + " · ".join(_cnt(k) for k in topo.LOCALE_TABLE_KEYS))
        except Exception as e:      # noqa: BLE001 — 못 세도 진행은 막지 않는다
            print(f"  ✎ **쓴다** — {db} (행 수를 세지 못했다: {e})")
    try:
        act = args.action

        if act == "clone":
            # **KR 을 건드리지 않는다** — 읽어서 다른 스키마로 복제한다.
            # 되돌리려면 그 스키마를 지우면 되므로 `dump kr` 이 선행
            # 조건이 아니다(그쪽은 KR 을 덮는 경로의 게이트다).
            r = topo.clone_schema(conn, src, db, dry_run=dry)
            print(f"▶ clone{' (읽기만)' if dry else ''} — "
                  f"{r['source']} → {r['target']} "
                  f"({r['charset']}/{r['collation']})")
            print(f"  테이블 {r['tables']} — 데이터까지 {r['with_data']} · "
                  f"구조만 {r['structure_only']}")
            print(f"  복사 {r['copy_rows']:,}행 {r['copy_mb']:,.1f} MB · "
                  f"구조만 {r['skip_mb']:,.1f} MB (일본 트래픽이 새로 만든다)")
            if dry:
                for p_ in r["plan"]:
                    if p_["data"] and p_["mb"] >= 1.0:
                        print(f"    {p_['table']:30} {p_['rows']:>9,}행 "
                              f"{p_['mb']:>7,.1f} MB")
                print(f"  대상 스키마 존재 {'예' if r['target_exists'] else '아니오'}"
                      f" · 만들 권한 {'있다' if r['can_create_schema'] else '없다'}"
                      f" · 대상 GRANT {'있다' if r['has_target_grant'] else '없다'}")
                if not (r["target_exists"] and r["has_target_grant"]):
                    # **이 계정은 스키마를 만들 수 없다**(전역 권한이
                    # `PROCESS` 뿐). 미리 말하지 않으면 실행 때
                    # `1044 Access denied` 로 멈추고 그 메시지는 "계정이
                    # 틀렸다"처럼 읽혀 엉뚱한 데를 찾게 만든다.
                    print("  ⚠ 먼저 사람이 root 로 한 번 만들어 주어야 한다 —")
                    for q in r["prepare_sql"]:
                        print(f"      {q}")
                print("  적재하지 않았다 — DDL 이라 승인 뒤다 "
                      "(--dry-run 을 떼면 CREATE TABLE … LIKE 부터 돈다)")
            else:
                print(f"  테이블 생성 {r['created']} · 데이터 복사 {r['copied']} · "
                      f"이미 있어 건너뜀 {r['already']}")
            return 0

        if act == "ensure-tables":
            # **지역 테이블 넷만 만든다.** `CREATE TABLE … LIKE` 라
            # collation·인덱스·타입이 원본과 1:1 이다 — 손으로 적으면
            # 불변규칙 14 의 collation 이 어긋나고 조용히 틀린다.
            r = topo.ensure_locale_tables(conn, args.locale, write=write)
            print(f"▶ ensure-tables{' (읽기만)' if dry else ''} — "
                  f"locale {r['locale']}")
            for t in r["tables"]:
                mark = ("만들었다" if t["created"]
                        else "이미 있다" if t["existed"] else "없다 — 만들 대상")
                rows = "—" if t["rows"] is None else f"{t['rows']:,}행"
                print(f"  {t['table']:26} {mark:12} {rows:>10}  "
                      f"(원본 {t['source']} {t['source_rows']:,}행 · "
                      f"{t['collation']})")
            print(f"  collation 은 원본에서 그대로 온다 — `CREATE TABLE … LIKE` "
                  f"가 복제하지 않는 것은 **외래키뿐**이고 실측 "
                  f"{r['foreign_keys']}건이다")
            if dry:
                print("  만들지 않았다 — DDL 이라 승인 뒤다 (--write)")
            return 0

        if act in ("copy-cells", "copy-enb-org"):
            # 키·참조만 DB 안에서 복사하고 **좌표·이름은 비운다.**
            # 44만행을 Python 으로 왕복시키지 않는다.
            r = topo.copy_locale_rows(
                conn, "cell" if act == "copy-cells" else "enb_org",
                args.locale, write=write)
            print(f"▶ {act}{' (읽기만)' if dry else ''} — "
                  f"{r['source']} → {r['table']}")
            print(f"  원본 {r['source_rows']:,}행 · 대상 현재 "
                  f"{r['rows_before']:,}행 · 복사 컬럼 {r['columns']}개")
            print(f"  비워 두는 컬럼: {', '.join(r['null_columns'])} — "
                  f"0 이나 '' 로 채우지 않는다(불변규칙 4·17). 좌표는 "
                  f"relocate-cells 가 만든다")
            if r["source_duplicate_keys"]:
                # **걸러내지 않는다** — 복사는 거울이다. 원본에 있는 중복을
                # 조용히 접으면 두 환경을 대조할 수 없다(실측: CL_ENB_ORG
                # 45,955행 / distinct ENB_ID 45,954).
                print(f"  ⚠ 원본에 키 중복 {r['source_duplicate_keys']}건 "
                      f"({', '.join(r['key_columns'])}) — 그대로 복사한다")
            if r["source_only_columns"]:
                print(f"  ⚠ 원본에만 있는 컬럼 제외: "
                      f"{', '.join(r['source_only_columns'])} — 벌이 원본과 "
                      f"다른 DDL 이다(LIKE 로 만들지 않았다는 뜻)")
            if r["skipped"]:
                print(f"  {r['skipped']}")
            else:
                print(f"  적재 {r['inserted']:,}행 → 현재 {r['rows']:,}행")
            return 0

        if act == "list":
            info = topo.survey(conn, args.locale)
            a = info.pop("_active")
            print(f"▶ 마스터 현황 (locale {args.locale})")
            for key, v in info.items():
                # **"테이블이 없다"를 0 행으로 적지 않는다**(불변규칙 17).
                if not v.get("exists", True):
                    print(f"  {key:8} {v['table']:26} "
                          f"{'테이블 없음':>12}  → topo ensure-tables "
                          f"--locale {args.locale} --write")
                    continue
                miss = v.get("no_coord")
                tail = "" if miss is None else f"  좌표 없음 {miss:,}"
                shared = "  (한 벌 공유)" if v.get("shared") else ""
                print(f"  {key:8} {v['table']:26} {v['rows']:>9,}{tail}{shared}")
            # **활성 셀을 따로 센다.** 마스터 44만 중 트래픽이 붙는 것만
            # 지도·탐지에 나타나고, 채울 가치가 있는 자리도 그쪽이다.
            print(f"\n▶ 활성 셀 (1분 집계에 나타나는 것) — eNB {a['enbs']:,}")
            print(f"  셀 {a['cells']:,}  ·  {a['cell_table']} 에 행 없음 "
                  + ("산출 불가 (그 벌이 아직 없다)" if a["unmapped"] is None
                     else f"{a['unmapped']:,}"))
            if a["unmapped"]:
                # **생성기가 만든다** — `topo fill-coords` 는 세는 커맨드다
                # (2026-10-26 이동). 그리고 804 를 전부 채우지 않는다:
                # 좌표 없는 eNB 는 불변규칙 12 검증용이다.
                print("  → 좌표는 생성기가 만든다 (매 틱 ·"
                      " load_cells(ensure_coords=True))")
                print("     그중 몇 개가 대상이고 몇 개가 의도된 미매핑인지 —"
                      " `topo fill-coords`")

            # **원장을 함께 찍는다.** JP 전용 장비를 KR 선택에서 빼는
            # 근거이자 되돌림 수단이고, 추적 밖이라 지워져도 조용하다.
            led = topo.load_added_enbs(args.locale)
            if led.get("enbs"):
                print(f"\n▶ JP 전용 장비 원장 — {len(led['enbs']):,}대 · "
                      f"대역 시작 {led.get('band_start')}")
                print(f"  {topo.added_enbs_path(args.locale)}")
                print("  이 파일이 KR 생성에서 그 장비를 빼는 근거다 — "
                      "지우면 KR 이 후보로 보고 미매핑이 늘어난다")

            # **마스터에 있는 것과 시뮬레이션이 쓰는 것은 다르다.**
            # `_pick_enbs` 가 셀 수 내림차순으로 `enb_limit` 칸만 고르므로,
            # `topo` 가 놓은 3셀 eNB 는 꼬리에 붙어 **에러 없이 빠진다.**
            # 그 상태가 안 보이는 것이 이 커맨드를 고친 이유다.
            pem = cfg.get("pemdb1") or {}
            limit = int(pem.get("enb_limit", 0))
            rep = topo.pick_report(
                conn, _topo_xw_path_opt(cfg, args.geo_crosswalk), limit)
            print(f"\n▶ 시뮬레이션 선택 (pemdb1.enb_limit = {limit or '0 → 전량'}"
                  f" · load_cells 실측)")
            others = rep["candidates"] - rep["band_master"]
            o_pick = rep["picked_enbs"] - rep["band_picked"]
            print(f"  {_pad('대역', 26)}{'마스터':>7} {'선택':>8}  통과")
            print(f"  {_pad('기존 (topo 대역 밖)', 26)}{others:>8,} "
                  f"{o_pick:>8,}  {'✓' if o_pick else '✗'}")
            # 마스터에 0 개면 통과/탈락을 말할 대상이 없다 — `✗` 로 적으면
            # "넣었는데 빠졌다"로 읽힌다(불변규칙 17 과 같은 결의 혼동).
            mark = ("—" if not rep["band_master"]
                    else "✓" if not rep["band_missing"] else "✗")
            print(f"  {_pad(f'topo ({topo.NEW_BAND[0]}~{topo.NEW_BAND[1]})', 26)}"
                  f"{rep['band_master']:>8,} {rep['band_picked']:>8,}  {mark}")
            print(f"  후보 셀 {rep['candidate_cells']:,} → 선택 셀 "
                  f"{rep['picked_cells']:,}")
            if rep["band_missing"]:
                m = rep["band_missing"]
                print(f"  ✗ topo eNB {len(m)}개가 **트래픽을 받지 못한다** — "
                      f"마스터에만 있고 시뮬레이션에는 없다")
                print(f"    {', '.join(m[:10])}{' …' if len(m) > 10 else ''}")
                print(f"    → {_limit_advice(rep)}")
            return 0

        if act == "restore":
            # **기본이 점검이다.** `--write` 를 붙여야 실제로 넣는다 — 이
            # 경로는 `wipe` 를 먼저 하므로, 못 넣을 상태에서 진행하면
            # "지웠는데 못 넣는" 상태가 된다(`topo.restore` 가 그래서 ok 가
            # 아니면 쓰지 않는다).
            rep = topo.restore(conn, args.locale, dry_run=dry)
            print(f"▶ restore{'' if args.write else ' (점검만)'} — "
                  f"{topo.LOCALE_DIR / args.locale}")
            for info in rep["tables"].values():
                tail = info.get("error") or (
                    f"{info.get('rows', 0):,}행"
                    + (f" → 적재 {info['inserted']:,}" if "inserted" in info else "")
                    + (f" · 사본에만 있는 컬럼 제외 {', '.join(info['dropped'])}"
                       if info.get("dropped") else ""))
                print(f"  {info['table']:22} {tail}")
            # **복구 범위를 말한다.** 안 적으면 "되돌렸다"가 실제보다 넓게
            # 읽힌다 — `data/geo/` 는 이 경로 밖이다(불변규칙 17 의 결).
            print(f"  복구 범위: {' · '.join(rep['scope'])}")
            for line in rep["not_restored"]:
                print(f"  ✗ 복구 대상 아님 — {line}")
            if not rep["ok"]:
                print("✗ 복귀 경로가 서 있지 않다 — 위 사유를 먼저 고친다",
                      file=sys.stderr)
            return 0 if rep["ok"] else 1

        if act == "dump":
            counts = topo.dump(conn, args.locale)
            print(f"▶ {topo.LOCALE_DIR / args.locale} 로 떠냈다 — **추적 밖이다**")
            for key in topo.DUMP_TABLES:
                path = topo.dump_path(args.locale, key)
                mb = path.stat().st_size / 1e6
                print(f"  {path.name:30} {counts[topo.TABLES[key][0]]:>9,}행"
                      f"  {mb:7.1f} MB")
            print("  되돌릴 때 — python -m datagen topo restore "
                  f"{args.locale} [--write]")
            return 0

        if act == "add":
            table = topo.table_name(args.target, args.locale)
            row = dict(kv.split("=", 1) for kv in args.set)
            if not write:
                print(f"▶ add (읽기만) — {db}.{table} 에 1행을 넣을 것이다\n"
                      f"  {row}\n  적재는 --write 다")
                return 0
            if not _topo_gate(conn, args.locale, db):
                return 1
            n = topo.insert_rows(conn, table, [row])
            print(f"▶ {db}.{table} +{n}행  {row}")
            return 0

        if act == "update":
            # **키 컬럼은 고치지 않는다** — 1분 집계·원시 Parquet·
            # ground_truth 가 그 키를 참조한다(`topo.update_row` 머리말).
            table = topo.table_name(args.target, args.locale)
            cond = dict(kv.split("=", 1) for kv in args.key)
            row = dict(kv.split("=", 1) for kv in args.set)
            if not write:
                print(f"▶ update (읽기만) — {db}.{table}\n"
                      f"  WHERE {cond}\n  SET {row}\n  적용은 --write 다")
                return 0
            if not _topo_gate(conn, args.locale, db):
                return 1
            r = topo.update_row(conn, args.target, args.locale, cond, row)
            print(f"▶ {db}.{r['table']} {r['updated']}행 수정  "
                  f"{', '.join(r['columns'])}")
            if r["dropped"]:
                print(f"  ⚠ DDL 에 없어 제외: {', '.join(r['dropped'])}")
            return 0

        if act == "rm":
            table = topo.table_name(args.target, args.locale)
            keys = topo.TABLES[args.target][1]
            if args.all:
                # **`cellmap` 은 받지 않는다.** `topo` 는 위치 마스터를 다루고
                # `CL_CELL_MAP` 574,404행은 **셀 대장**이다. 전량 삭제할 이유가
                # 없고 `DUMP_TABLES` 에도 없어서 지우면 되돌릴 원본이 아예
                # 없다. 전에는 게이트(`dump_exists`)가 `CM_*` 네 파일만 보고
                # 이 삭제를 통과시켰다.
                if not topo.wipeable(args.target, args.locale):
                    print(f"✗ {table} 는 전체 삭제 대상이 아니다.\n"
                          "  `topo` 는 위치 마스터를 다룬다 — 셀 대장을 통째로"
                          " 지울 이유가 없고 복귀 사본도 없다.\n"
                          f"  단건은 된다 — topo rm {args.target} --key …",
                          file=sys.stderr)
                    return 1
                # 복귀 사본이 **실제로 넣을 수 있는 상태**여야 한다.
                if not _topo_gate(conn, args.locale, db):
                    return 1
                want = f"{table} 전체 삭제"
                print(f"  {table} 를 통째로 지운다. 되돌리려면 "
                      f"`topo restore {args.locale}` 를 돈다.")
                if input(f"  확인하려면 '{want}' 를 그대로 입력: ").strip() != want:
                    print("취소했다.")
                    return 1
                print(f"▶ {table} -{topo.wipe(conn, table):,}행")
                return 0
            if not args.key:
                print(f"✗ --key 가 필요하다 (키 컬럼: {', '.join(keys)})", file=sys.stderr)
                return 1
            cond = dict(kv.split("=", 1) for kv in args.key)
            where = " AND ".join(f"`{k}` = %s" for k in cond)
            if not write:
                hit = topo._rows(conn, f"SELECT COUNT(*) FROM `{table}` "
                                       f"WHERE {where}", list(cond.values()))[0][0]
                print(f"▶ rm (읽기만) — {db}.{table} 에서 {hit}행이 지워진다\n"
                      f"  {cond}\n  삭제는 --write 다")
                return 0
            if not _topo_gate(conn, args.locale, db):
                return 1
            with conn.cursor() as cur:
                n = cur.execute(f"DELETE FROM `{table}` WHERE {where}",
                                list(cond.values()))
            conn.commit()
            print(f"▶ {db}.{table} -{n}행  {cond}")
            return 0

        xw = _topo_xw_path(cfg, args.geo_crosswalk)

        if act == "geo-fetch":
            gd = Path(args.geo_dir) if args.geo_dir else _topo_geo_dir(cfg)
            got = topo.fetch_boundaries(gd, args.locale, force=args.replace)
            print(f"▶ geo-fetch — locale {args.locale} · "
                  f"{topo.geo_level_path(gd, args.locale, 'sgg').parent}")
            for name, size in got.items():
                print(f"  {name:28} {size / 1e6:6.2f} MB")
            print("  SOURCE.txt 에 출처를 함께 적었다 — 번들 디렉터리가"
                  " 추적 밖이라 크레딧이 파일과 같이 가야 한다")
            return 0

        if act == "code-map":
            # **DB 의 `(CITY_ID, GU_ID)` 가 입력이다.** 번들에서 유도하지
            # 않는다 — 트래픽이 참조하는 것은 DB 의 코드이고, 번들(250)과
            # DB(273)가 다르면 스위칭 후 그 차이만큼 조용히 미매핑이 된다.
            gd = Path(args.geo_dir) if args.geo_dir else _topo_geo_dir(cfg)
            # **KR 벌을 읽는다.** JP 이름이 KR 코드 자리에 들어가는 것이
            # 이 맵의 정의다 — 대상이 `CM_GU_JP` 라도 입력은 KR 코드다.
            kr_gu = [(int(c), int(g)) for c, g in topo._rows(
                conn, f"SELECT CITY_ID, GU_ID "
                      f"FROM `{topo.table_name('gu', 'kr')}` ORDER BY GU_ID")]
            doc = topo.build_jp_code_map(gd, kr_gu)
            c = doc["counts"]
            print(f"▶ code-map — locale {args.locale} · KR 시군구 "
                  f"{len(kr_gu)}개 기준")
            print(f"  KR 대응 {c['kr_gu']} + **추가 구역 {c['extra']}** = "
                  f"{c['total']}  (1:1 상한을 풀었다 — 사용자 지시 2026-10-26)")
            for city, rows in sorted(doc["picked"].items()):
                new = sum(1 for r in rows if r["new"])
                print(f"  {city:>3} ({topo.SIDO_MAP_JP[str(city)]:6}) "
                      f"{len(rows) - new:>3} + {new:>3}개  예: "
                      f"{rows[0]['name']}({rows[0]['jis']}) → "
                      f"GU_ID {rows[0]['gu_id']}")
            if doc["dense_gu_ids"]:
                # 10 단위 자리가 모자라 1 단위로 발급한 시도 — 번호만 보고
                # "체계가 깨졌다"고 읽지 않게 말한다
                print(f"  GU_ID 간격을 1 로 좁힌 시도 "
                      f"{len(doc['dense_gu_ids'])}곳: "
                      f"{', '.join(str(k) for k in doc['dense_gu_ids'])}")
            if doc["no_room"]:
                print(f"  ⚠ 시도 접두 자리가 없어 못 담은 구역 — "
                      + " · ".join(f"{k}({v['pref']}) {v['left']}개"
                                   for k, v in doc["no_room"].items()))
            print(f"  쓰지 않는 도도부현 {len(doc['unused_prefectures'])}곳 — "
                  "**묶지 않는다**(회색 배경으로 그릴 자리다)")
            if dry:
                print("  저장하지 않았다 — 저장은 --write 다")
            else:
                print(f"  → {topo.save_code_map(doc, args.locale)}")
            return 0

        if act == "seed-geo":
            # **`--replace` 는 세 테이블을 비운다.** 복귀 사본을 요구하지
            # 않고 있었고, `--locale` 을 빠뜨리면 KR 세 테이블이 사본 없이
            # 날아간다 — `relocate-cells` 와 같은 게이트를 붙인다.
            if write and args.replace and not _topo_gate(conn, args.locale, db):
                return 1
            # **경계 GeoJSON 이 입력이다.** graphio 가 받아 둔 디렉터리를
            # 그대로 읽는다(`graphio/config` 의 `paths.geo`) — 두 저장소가
            # 같은 파일을 보게 하려고 경로를 인자로 받는다.
            gd = Path(args.geo_dir) if args.geo_dir else _topo_geo_dir(cfg)
            r = topo.seed_geo(conn, gd, locale=args.locale,
                              dongs_per_gu=args.dongs,
                              spread_km=args.spread_km,
                              seed=int(cfg["generate"].get("seed", 20260903)),
                              replace=args.replace,
                              dry_run=dry)
            print(f"▶ seed-geo{' (읽기만)' if dry else ''} — "
                  f"locale {r['locale']} · {gd}")
            print("  " + " · ".join(
                f"{t} {r[k]:,}" for k, t in zip(("city", "gu", "dong"),
                                                r["tables"]))
                  + f"  (읍면동 출처: {r['emd_source']})")
            if r["skipped_sido"] or r["skipped_sgg"]:
                # **코드 맵에 없는 폴리곤은 넣지 않는다.** 코드를 지어내면
                # 그 지역 셀이 영원히 조인되지 않는다 — 수를 말한다.
                print(f"  ⚠ 코드 맵에 없어 제외 — 시도 {r['skipped_sido']} · "
                      f"시군구 {r['skipped_sgg']}")
            if r["emd_source"] == topo.EMD_GENERATED:
                print("  읍면동은 폴리곤 없이 생성했다 — crosswalk 의 emd 는"
                      " **항등**이다 (비우면 셀의 DONG_ID 가 조용히 NULL 이 된다)")
                # **구마다 동 수가 다르다.** 균일값으로 덮으면 셀의 DONG_ID
                # 와 어긋나고, 그 어긋남은 "동 드릴다운이 빈다"로만 보인다
                print(f"    동 수는 KR 구별 범위를 그대로 따랐다 — "
                      f"KR 대응 {r['dong_from_kr_gus']}구 · "
                      f"추가 {r['dong_new_gus']}구는 --dongs {args.dongs}")
            j = r["dong_join"]
            if j is None:
                # 셀 벌이 없으면 셀 수를 모른다 — 0 으로 적지 않는다
                print("  셀↔동 조인: 산출 불가 (셀 벌이 아직 없다)")
            elif j.get("error"):
                # **진단만 실패했다** — 생성 결과는 위에 있다. 조용히
                # 넘기지 않고 사유를 적는다
                print(f"  ⚠ 셀↔동 조인 진단 실패 — {j['error']}")
                print("    생성 결과(위 행 수)는 유효하다. 적재 전에 "
                      "그 수를 확인할 수 없다는 뜻이다")
            else:
                # **기준은 KR 자체 미매핑이다 — 숫자를 박지 않는다.** 셀의
                # `DONG_ID` 가 KR 체계이므로 JP 가 KR 보다 좋아질 수는 없고,
                # 나빠지면 동 드릴다운이 그만큼 빈다. 상수 71 로 적어 두면
                # KR 데이터가 바뀔 때 기준만 낡는다.
                base = topo.dong_join_check(conn, "kr")
                lim = base["unmatched"] if base else None
                mark = ("?" if lim is None
                        else "✓" if j["unmatched"] <= lim else "✗")
                print(f"  {mark} 셀↔동 조인 ({j['method']}) — 셀 "
                      f"{j['cells']:,} 중 동 벌에 없는 것 "
                      f"{j['unmatched']:,}행 / 키 {j['unmatched_keys']}")
                print(f"    KR 자체 미매핑 {lim:,}행 이하여야 한다 — 셀의 "
                      f"DONG_ID 가 KR 체계라 JP 가 그보다 좋아질 수는 없다. "
                      f"0 으로 만들려고 없는 동을 지어내지 않는다")
                if mark == "✗":
                    print(f"    → 이 구성으로 적재하면 KR 보다 "
                          f"{j['unmatched'] - lim:,}행이 더 빈다")
            if dry:
                print("  적재하지 않았다. 넣으려면 --dry-run 을 떼고, 행이"
                      " 이미 있으면 --replace 를 함께 준다")
            else:
                print(f"  crosswalk 를 다시 썼다 — {r['crosswalk']}")
            return 0

        if act == "org-names":
            # **지리가 아니라 조직이다** — 별 커맨드로 뒀다. `CM_REGION` 의
            # `영동2` 가 15개 시도에 걸치고 그 테이블에 `CITY_ID` 가 없다.
            if write and not _topo_gate(conn, args.locale, db):
                return 1
            r = topo.org_names(conn, locale=args.locale, dry_run=dry,
                               allow_shared=bool(args.allow_shared))
            print(f"▶ org-names{' (읽기만)' if dry else ''} — "
                  f"locale {r['locale']}")
            if r.get("skipped"):
                print(f"  {r['skipped']}")
                return 0
            print(f"  CM_BRANCH {r['branch']} (상수 표 · 사람이 정한다) · "
                  f"CM_LAREA {r['larea']} (SIDO_MAP_JP 에서 유도) · "
                  f"CM_REGION {r['region']} (<부모 이름><연번>)")
            for k, label in (("branch", "지사"), ("larea", "권역"),
                             ("region", "지역")):
                if r["sample"][k]:
                    ex = " · ".join(f"{i} → {n}" for n, i in r["sample"][k])
                    print(f"    {label:4} {ex}")
            if r["region_no_parent"]:
                # 부모가 `CM_BRANCH` 에 없다(실측 `BRANCH_ID=1200` 아래 2줄).
                # 이름을 지어내면 "있는 지사"처럼 보인다
                print(f"  ⚠ 부모 지사를 못 찾아 제외 {r['region_no_parent']}줄 — "
                      f"이름을 지어내지 않는다")
            if r["larea_no_sido"]:
                print(f"  ⚠ 대응 시도가 없어 제외: {', '.join(r['larea_no_sido'])}"
                      f" — LAREA_EXTRA_JP 에 넣는다")
            print(f"  ⚠ CM_BRANCH · CM_LAREA · CM_REGION 은 **한 벌 공유**다 — "
                  f"이 이름은 KR 화면에도 그대로 나온다")
            print(f"  건드리지 않는 자리: {' · '.join(r['untouched'])}")
            print("    앞 셋은 graphio 가 **이름을 조인하지 않는다**(EMS 는"
                  " ID 숫자만 쓴다) — 반출본과 대조할 수 있게 원본을 둔다")
            print("    CB_VENDOR 는 읽히지만 **제조사 축은 지리 전환과 다른"
                  " 축**이다 (일본에서도 삼성 장비를 쓴다)")
            return 0

        if act == "relocate-cells":
            # **키를 유지하고 좌표만 다시 쓴다** — 전환의 마지막 조각이다.
            # 코드 맵이 KR 코드를 그대로 쓰므로 `(ENB_ID, SECTOR)`·`GU_ID`
            # 는 유효하고, 그 44만행에 들어 있는 **한국 좌표**만 바꾼다.
            # 안 바꾸면 시군구는 오사카인데 셀은 대전 앞산에 찍힌다.
            if write and not _topo_gate(conn, args.locale, db):
                return 1
            r = topo.relocate_cells(conn, locale=args.locale,
                                    spread_km=args.spread_km,
                                    seed=int(cfg["generate"].get("seed", 20260903)),
                                    only_active=not args.all,
                                    dry_run=dry)
            print(f"▶ relocate-cells{' (읽기만)' if dry else ''} — "
                  f"locale {r['locale']} · {r['table']} · "
                  f"{'전체' if args.all else '활성'} 셀 {r['cells']:,} / "
                  f"eNB {r['enbs']:,}")
            print(f"  좌표 갱신 {r['updated']:,} · 분포 패턴 {r['patterns']}"
                  f"  (POINT 가 아니면 spread-km 를 의심한다)")
            # **이름도 바꿨다는 것을 말한다.** 좌표만 바뀔 것으로 읽히면
            # `RU_NAME`·`BTS_NAME` 이 한국으로 남은 줄 모르고, 그 상태는
            # "지도는 오사카인데 기지국은 ○○빌딩"이다(에러 아님).
            print(f"  이름 갱신 — 셀 RU_NAME {r['renamed_cells']:,} · "
                  f"eNB BTS_NAME {r['renamed_enbs']:,}  "
                  f"(<시군구명>_<연번>[-섹터] · CM_GU 이름을 쓴다)")
            if r["cells_out_of_scope"]:
                # 활성 셀만 고쳤으면 나머지 `RU_NAME` 은 그대로다 —
                # DB 째 이식이면 `--all` 이 맞다
                print(f"  ⚠ 범위 밖 {r['cells_out_of_scope']:,}셀의 좌표·이름은"
                      f" 그대로다 — DB 째 이식이면 --all 로 전수를 돌린다")
            if r["org_untouched_reason"]:
                # **eNB 이름이 한국어로 남는다.** 조용히 빼면 화면에 두
                # 나라가 섞인 줄 모른다 — 그 테이블은 한 벌 공유다.
                print(f"  ⚠ {r['org_untouched_reason']}")
            print("  CL_ENB_ORG_MAP(2,904행)은 건드리지 않았다 — CL_ENB_ORG"
                  " 전수의 6.4% 뿐이고 이름·권역은 그쪽을 본다")
            if r["skipped_no_gu"]:
                # 시군구를 모르는 셀은 **건드리지 않는다** — 아무 데나 놓으면
                # 미매핑이 "매핑된 거짓 좌표"가 되어 H-07 이 놓친다
                print(f"  시군구를 모르거나 그 시군구에 좌표가 없어 제외 "
                      f"{r['skipped_no_gu']:,}셀 — 미매핑으로 남는다")
            return 0

        if act == "seed-enb":
            r = topo.seed_enb(conn, xw, per_gu=args.per_gu,
                              cells_per_enb=args.cells, spread_km=args.spread_km,
                              seed=int(cfg["generate"].get("seed", 20260903)),
                              dry_run=dry)
            print(f"▶ seed-enb{' (읽기만)' if dry else ''} — "
                  f"시군구 {r['gus']} · eNB {r['enbs']} · 셀 {r['cells']}")
            print(f"  분포 패턴: {r['patterns']}  (POINT 가 아니면 spread-km 를 의심한다)")
            if r["gus_without_drawable_dong"]:
                # **조용히 넘기지 않는다.** 이 시군구들은 `DONG_ID` 를 비워야
                # 하고, 그 컬럼이 NOT NULL 이라 적재에서 빠진다. 0 으로 채우면
                # `_filter_drawable` 이 버려서 "트래픽 없음"으로만 보인다.
                g = r["gus_without_drawable_dong"]
                print(f"  ⚠ 그릴 수 있는 동이 없는 시군구 {len(g)}곳 — "
                      f"셀 {r['skipped_no_dong']}개를 넣지 않았다")
                print(f"    {', '.join(str(x) for x in g[:20])}"
                      f"{' …' if len(g) > 20 else ''}")
            # **적재 규모 판단은 사용자 몫이므로 설정을 고치지 않는다.**
            # 대신 얼마여야 하는지 말한다 — 이 수를 모르면 장비를 넣고도
            # 트래픽이 0 인 채로 남는다(위 `list` 의 선택 열과 같은 사실).
            if r["keys"]:
                lim = int((cfg.get("pemdb1") or {}).get("enb_limit", 0))
                rep = topo.pick_report(conn, xw, lim, extra_cells=r["keys"])
                if rep["band_missing"]:
                    print(f"  ✗ 이 eNB {len(rep['band_missing'])}개가 "
                          f"선택되지 않았다 — **include 가 깨졌다**")
                    print(f"    → {_limit_advice(rep)}")
                else:
                    # `enb_limit` 과 무관하다는 것이 요점이다 — 그 수를 보고
                    # "60 이라 괜찮았다"로 읽으면, 한도를 줄였을 때 커버리지가
                    # 따라 사라진다고 오해한다.
                    print(f"  ✓ eNB {rep['band_picked']}개가 선택된다 — "
                          f"`enb_limit`({lim}) 과 **무관하다** "
                          f"(`_pick_enbs` 가 topo 대역을 한도 밖에서 포함한다)")
                    print(f"  트래픽 가중은 기존의 {topo.TOPO_WEIGHT}배다 — "
                          f"커버리지는 받고 기존 eNB 의 1분 창 표본은 지킨다")
            return 0

        if act == "seed-enb-jp":
            # **추가 구역에 장비를 놓는다** — KR 선택에서 빠지는 장비다.
            # 대역 시작 번호는 **인자**다: 코드에 박지 않고, 받은 번호로
            # 만든 ID 가 세 테이블에서 실제로 비어 있는지 확인한 뒤 쓴다.
            if args.band_start is None:
                print("✗ --band-start 가 필요하다 — 빈 ENB_ID 대역을 "
                      "실측해서 준다.\n"
                      "  코드에 기본값을 두지 않는다: 어느 번호가 비어 "
                      "있는지는 그 DB 의 사실이고, 가정을 코드에 남기면 "
                      "다른 DB 에서 조용히 겹친다", file=sys.stderr)
                return 1
            r = topo.seed_enb_jp(conn, args.locale, args.band_start,
                                 per_gu=args.per_gu, cells_per_enb=args.cells,
                                 spread_km=args.spread_km,
                                 seed=int(cfg["generate"].get("seed", 20260903)),
                                 write=write)
            print(f"▶ seed-enb-jp{' (읽기만)' if dry else ''} — "
                  f"locale {r['locale']} · 추가 구역 {r['gus']} · "
                  f"eNB {r['enbs']} · 셀 {r['cells']}")
            print(f"  KR 에 없는 구역 {r['gus_jp_only']} = 대상 {r['gus']} + "
                  f"좌표 없어 제외 {r['gus_no_coord']}")
            if r["gus_no_coord"]:
                # **조용히 빠지지 않게 한다.** 좌표 없는 구역은 기준점이
                # 없어 장비를 놓을 수 없는데, 그 사실을 안 적으면 그 구역이
                # 왜 빈지 알 수 없다
                print(f"    예: {r['gus_no_coord_sample']} — seed-geo 가 그 "
                      f"구역의 중심좌표를 만들었는지 본다")
            print(f"  대역 시작 {r['band_start']} · 분포 패턴 {r['patterns']}"
                  f"  (POINT 가 아니면 spread-km 를 의심한다)")
            print(f"  쓰는 곳 — {r['tables']['cellmap']} (**KR 과 공유 · "
                  f"사본 없음**) · {r['tables']['cell']} · {r['tables']['org']}")
            if r["skipped_no_dong"]:
                print(f"  ⚠ 동이 없어 건너뛴 구역 "
                      f"{len(r['skipped_no_dong'])}곳 — DONG_ID 가 NOT NULL "
                      f"이고 0 으로 채우면 조용히 버려진다")
            if r["no_org_default"]:
                print(f"  ⚠ 시도 대표 조직을 못 찾은 구역 "
                      f"{len(set(r['no_org_default']))}곳 — 그 eNB 는 조직 "
                      f"축에서 빠진다(지어내지 않는다)")
            if r.get("already"):
                # **에러가 아니라 완료 상태다.** 전에는 겹침을 한 에러로
                # 뭉쳐서, 정상 완료 뒤 세어 보기가 붉은 실패로 보였다
                print(f"  ✓ {r['note']}")
            elif r["written"]:
                print(f"  ✎ 원장 {r['ledger']} — 누적 {r['ledger_total']}대")
                print(f"     이 파일이 **KR 선택에서 빼는 근거이자 되돌림 "
                      f"수단**이다 (topo rm-added --locale {args.locale})")
            else:
                print("  적재하지 않았다 — 넣으려면 --write")
            return 0

        if act == "rm-added":
            # `CL_CELL_MAP` 의 되돌림. 사본이 없으므로 **원장에 적힌 것만**
            # 지운다 — 대역으로 추측하면 그 번호의 다른 장비가 함께 사라진다.
            r = topo.remove_added_enbs(conn, args.locale, write=write)
            print(f"▶ rm-added{' (읽기만)' if dry else ''} — "
                  f"locale {r['locale']} · 원장 {r['enbs']}대")
            for t, n in r["tables"].items():
                print(f"  {t:26} "
                      + ("벌이 없다" if n is None else f"{n:,}행"))
            print("  삭제했다 · 원장을 비웠다" if r["written"]
                  else "  지우지 않았다 — 삭제는 --write 다")
            return 0

        if act == "fill-coords":
            # **점검 전용이다** (2026-10-26). 적재는 생성기가 한다 —
            # `cmd_run`·`cmd_pemfill` 이 `load_cells(ensure_coords=True)` 로
            # 매 틱 채운다. 수동 커맨드로 두면 사람이 기억해서 쳐야 하고
            # 현장에서는 아무도 치지 않으며, **적재 경로가 둘이면 어느 쪽이
            # 넣었는지 모른다.**
            #
            # 생성기와 **같은 인자**를 준다(crosswalk · enb_limit). 안 주면
            # 후보 집합이 달라져 점검이 다른 수를 말한다 — 실제로 한 번
            # 768/36 대 696/108 로 갈렸다.
            pem = cfg.get("pemdb1") or {}
            r = topo.fill_coords(conn, xw, int(pem.get("enb_limit", 0)))
            print(f"▶ fill-coords (점검) — 선택 셀 {r['cells']:,} 중 좌표 없음 "
                  f"{r['targets']:,}")
            print(f"  생성 대상 {r['fillable']}셀 / eNB {r['fillable_enbs']} — "
                  f"좌표 있는 eNB 의 섹터 결손")
            print(f"  건드리지 않는 의도된 미매핑 {r['intended_unmapped']}셀 / "
                  f"eNB {r['intended_enbs']} — 좌표 없는 eNB")
            print("    → `_pick_enbs` 가 불변규칙 12(H-07 · UNMAPPED 패널)"
                  " 검증용으로 일부러 남긴다")
            print("  적재는 생성기가 한다 — python -m datagen run (매 틱)")
            return 0

        print(f"✗ 모르는 action: {act}", file=sys.stderr)
        return 1
    except RuntimeError as e:
        # **트레이스백을 내지 않는다.** 이 명령들의 `RuntimeError` 는
        # "사람이 조건을 안 맞췄다"(원본=대상 · 사본 없음 · 순서 거꾸로)이고,
        # 트레이스백은 "깨졌다"로 읽혀 엉뚱한 데를 찾게 만든다.
        print(f"✗ {e}", file=sys.stderr)
        return 1
    finally:
        conn.close()


def cmd_ui(args) -> int:
    """제어 UI 기동 — 실제 구현은 datagen.ui 에 있다 (import 는 지연시킨다)."""
    from .ui import main as ui_main

    argv = ["--host", args.host, "--port", str(args.port)]
    if args.config:
        argv += ["--config", args.config]
    return ui_main(argv)


def cmd_callflow(args) -> int:
    """call flow 명세에서 **데이터셋을 미리 만든다.**

    런타임 생성(`run`·`pemfill`)과 **별개 경로다.** 분 단위 생성은 지금처럼
    실측 분포가 돌리고, 이것은 call flow 를 보여 주는 샘플을 깔아 둔다.
    명세는 `config/callflow.yaml` 이고 그쪽이 정본이다.
    """
    from . import callflow

    cfg = load_cfg(args.config)
    data_dir = Path(args.data_dir) if args.data_dir else Path("data")
    out = Path(args.out) if args.out else None
    if args.verify_only:
        spec = callflow.load_spec()
        bad = callflow.verify_fields(spec)
        print(f"명세 — 단계 {len(spec.steps)} · 절차 {len(spec.flows)} · "
              f"장애 {len(spec.faults)}")
        if bad:
            print("✗ 없는 필드를 쓴다:", ", ".join(bad))
            return 1
        print("✓ 필드 전부 실재한다")
        for n, f in sorted(spec.faults.items()):
            cts = spec.call_types_for(f)
            print(f"   {n:24s} {f.break_at:9s} → call_type {list(cts)}")
        return 0

    report = callflow.build_all(cfg, data_dir, out,
                                n_normal=args.normal, n_fault=args.fault)
    fail = 0
    for name, (cnt, bad) in report.items():
        mark = "✓" if not bad else "✗"
        print(f" {mark} {name:34s} {cnt:6,d}건")
        for b in bad:
            fail += 1
            print(f"     {b}")
    print(f"\n→ {out or callflow.DEFAULT_OUT}")
    return 1 if fail else 0


def cmd_patterns(args) -> int:
    """정답지 패턴에서 데이터셋을 찍어낸다.

    `callflow`(명세 조립)와 **다른 경로다.** 이쪽은 실측 레코드를 틀로
    쓰고 시각·식별자·셀만 갈아 끼운다 — 정답지 밖의 값이 섞이지 않는다.
    """
    from . import callflow, patterns

    cfg = load_cfg(args.config)
    data_dir = Path(args.data_dir) if args.data_dir else Path("data")
    gen, _dist = callflow._fixtures(cfg, data_dir, args.seed)
    bank = patterns.Bank(args.bank)
    print(f"패턴 {len(bank):,} · 조합 {len(bank.keys)}종")

    import random as _r

    # **상용 컬럼명으로 내보낸다.** 자체 필드명(`call_type`·`First_eNB_ID`)
    # 으로 쓰면 `cell_agg` 가 한 컬럼도 못 읽는다 — 그쪽은 원시 Parquet 의
    # 상용 이름(`CALL_TYPE`·`FIRST_ENB_ID`)을 본다. 데이터셋이 파이프라인에
    # 들어가려면 **원시와 같은 모양**이어야 한다.
    names = None
    out = Path(args.out) if args.out else patterns.DEFAULT_BANK.parent / "dataset"

    der = patterns.Derive() if not args.no_derive else None
    groups: list[tuple[str, list, int | None]] = []
    if args.by_call_type:
        have = {int(k[0]) for k in bank.keys if k[0]}
        for ct in sorted(have):
            groups.append((f"ct{ct:02d}", bank.match(call_type=ct), None))
        # **정답지에 없는 절차는 파생한다.** 표본 204호에 여섯 종뿐이고
        # 나머지는 있는 패턴에서 달라지는 부분만 바꿔 만든다.
        if der:
            for ct in sorted(set(der.rules) - have):
                groups.append((f"ct{ct:02d}", [], ct))
    else:
        groups.append(("all", bank.keys, None))
    made: list[tuple] = []
    for name, keys, ct in groups:
        if not keys and ct is None:
            continue
        recs = patterns.build(gen, bank, keys=keys or None, n=args.count,
                              rng=_r.Random(args.seed + len(name) + (ct or 0)),
                              derive=der, call_type=ct, minutes=args.minutes)
        # **`--raw-layout` 은 모아서 한 번에 쓴다.** 그룹마다 쓰면 같은
        # 분 파일을 11번 덮어써 **마지막 call_type 만 남는다**(실제로
        # 그랬다 — 한 분이 통째로 Reroute 였다).
        n = (len(recs) if args.raw_layout else
             patterns.write(recs, out / f"{name}.parquet"))
        made.append((name, keys, ct, recs))
        tag = f"파생 ← {der.sources(ct)}" if ct else f"키 {len(keys)}종"
        print(f" ✓ {name:8s} {n:7,d}건  ({tag})")
    # ── U-Plane — **C-Plane 호에서 파생한다** ────────────────────────
    #
    # 따로 만들지 않는다. 성공한 호의 가입자·셀·장비·APN 을 물려받고
    # 시각을 그 호 구간 안에 놓아야 스펙 §4.4 의 CP–UP 조인이 선다 —
    # 그러지 않으면 `net_mode`·`call_type` 을 물려받을 수 없고 U-Plane 이
    # 통째로 `UNKNOWN` 이 된다.
    all_calls = [r for _n, _k, _c, recs in made for r in recs]
    if args.raw_layout:
        n = patterns.write_raw_layout(all_calls, out, "lte_call")
        print(f"\n ✓ lte_call {n:7,d}건  (원시 경로 규칙 · 11종 합본)")
    if not args.no_uplane:
        print()
        for series, per in (("transport", args.transport),
                            ("dns", args.dns), ("http", args.http)):
            if per <= 0:
                continue
            b = patterns.Bank(patterns.bank_path(series))
            recs = patterns.build_uplane(b, all_calls, series, per_call=per,
                                         rng=_r.Random(args.seed + len(series)))
            n = (patterns.write_raw_layout(recs, out, series)
                 if args.raw_layout else
                 patterns.write_uplane(recs, out / f"{series}.parquet", series))
            print(f" ✓ {series:8s} {n:7,d}건  (C-Plane 성공 호에서 파생)")

    print(f"\n→ {out}")
    return 0


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

    # 정답지 패턴 — `callflow` 와 **다른 경로다**(명세 조립이 아니라 치환)
    pt = sub.add_parser("patterns",
                        help="정답지 패턴으로 데이터셋 생성 (창고에 쓰지 않는다)")
    pt.add_argument("--config")
    pt.add_argument("--data-dir")
    pt.add_argument("--bank", help="패턴 파일 (기본 data/callflow/patterns.jsonl)")
    pt.add_argument("--out", help="나갈 곳 (기본 data/callflow/dataset)")
    pt.add_argument("--count", type=int, default=5000, help="묶음당 레코드")
    pt.add_argument("--seed", type=int, default=20261028)
    pt.add_argument("--minutes", type=int, default=10,
                    help="레코드를 몇 분에 걸쳐 뿌릴지")
    pt.add_argument("--raw-layout", action="store_true",
                    help="원시 경로 규칙(<series>/<날짜>/<HHMM>.parquet)으로 "
                         "내보낸다 — **집계가 그대로 읽는다**")
    pt.add_argument("--no-uplane", action="store_true",
                    help="U-Plane 을 만들지 않는다 (C-Plane 만)")
    pt.add_argument("--transport", type=float, default=1.8,
                    help="성공 호당 transport 레코드 (실측 비율)")
    pt.add_argument("--dns", type=float, default=0.6)
    pt.add_argument("--http", type=float, default=0.9)
    pt.add_argument("--no-derive", action="store_true",
                    help="정답지에 없는 call_type 을 만들지 않는다")
    pt.add_argument("--by-call-type", action="store_true",
                    help="call_type 별로 파일을 가른다")
    pt.set_defaults(fn=cmd_patterns)

    # call flow 데이터셋 — **런타임 생성과 별개다.** 창고에 쓰지 않고
    # `data/callflow/` 로 나간다.
    cf = sub.add_parser("callflow",
                        help="call flow 명세로 데이터셋 생성·검증 (창고에 쓰지 않는다)")
    cf.add_argument("--config")
    cf.add_argument("--data-dir", help="cells.json·profile.json 이 있는 곳 (기본 data)")
    cf.add_argument("--out", help="나갈 곳 (기본 data/callflow)")
    cf.add_argument("--normal", type=int, default=2000, help="call_type 당 정상 호")
    cf.add_argument("--fault", type=int, default=500, help="장애당 레코드")
    cf.add_argument("--verify-only", action="store_true",
                    help="명세만 점검하고 만들지 않는다")
    cf.set_defaults(fn=cmd_callflow)

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

    # `%` 는 **두 번 적는다.** argparse 가 help 문자열에 `% params` 를 걸므로
    # 한 번만 적으면 `--help` 가 `ValueError: unsupported format character` 로
    # 죽는다 — 그 명령뿐 아니라 **`--help` 전체**가 안 나온다(2026-10-22 발견).
    c = sub.add_parser("capacity",
                       help="분당 생성 용량 측정 — 100%% 기준을 정하는 근거")
    c.add_argument("--config")
    c.add_argument("--steps", default="0.5,1,2,4",
                   help="측정할 배수 (기본 0.5,1,2,4 — 1 이 설정의 100%%)")
    c.add_argument("--warmup", type=int, default=3,
                   help="배수마다 버리는 워밍업 분 수 (기본 3 — 세션 풀을 채운다)")
    c.add_argument("--target", type=float, default=20.0,
                   help="생성에 허용할 초 (기본 20 = 60초 틱 예산의 1/3)")
    c.set_defaults(fn=cmd_capacity)

    # 마스터 토폴로지 — **1회성이다.** `pemfill`·`run` 은 상주하며 마스터를
    # 읽기만 하고, 쓰기를 그쪽에 넣으면 gen 이 둘 떴을 때 마스터가 흔들린다.
    t = sub.add_parser("topo", help="마스터 토폴로지 관리 — 행정구역·장비·셀 (1회성)")
    t.add_argument("action",
                   choices=["list", "add", "update", "rm", "dump", "restore",
                            "ensure-tables", "copy-cells", "copy-enb-org",
                            "clone", "geo-fetch", "code-map", "seed-geo",
                            "seed-enb", "seed-enb-jp", "rm-added",
                            "relocate-cells", "org-names", "fill-coords"])
    t.add_argument("target", nargs="?", choices=list(topo.TABLES),
                   help="add/update/rm 의 대상 테이블 (실제 이름은 --locale 이 "
                        "정한다 — gu + jp = CM_GU_JP)")
    t.add_argument("--config")
    t.add_argument("--database",
                   help="대상 스키마 — 기본은 pemdb1.database. **locale 은 "
                        "스키마를 가르지 않는다**(로케일 축은 테이블 이름이다). "
                        "clone 만 pemdb1.locale_database.<locale> 을 본다")
    t.add_argument("--locale", default="kr",
                   help="**대상 벌**. 지역 테이블 넷이 이 축을 갖는다 — "
                        "kr 은 원본(CM_GU), 그 외는 접미사(CM_GU_JP). "
                        "dump/restore 의 사본 디렉터리 이름도 이것이다")
    t.add_argument("--geo-dir", help="seed-geo: 경계 GeoJSON 디렉터리 "
                                     "(기본 pemdb1.geo_crosswalk 의 디렉터리)")
    t.add_argument("--dongs", type=int, default=8,
                   help="seed-geo: **KR 에 없는 추가 구역**의 동 수. KR 에 "
                        "같은 GU_ID 가 있으면 그 구의 DONG_ID 를 그대로 "
                        "재사용한다(셀의 DONG_ID 가 KR 체계로 박혀 있다)")
    t.add_argument("--replace", action="store_true",
                   help="seed-geo: 행이 있어도 세 테이블을 비우고 다시 넣는다")
    t.add_argument("--set", action="append", default=[], metavar="COL=VAL",
                   help="add: 넣을 컬럼 · update: 고칠 컬럼 (여러 번)")
    t.add_argument("--key", action="append", default=[], metavar="COL=VAL",
                   help="rm/update: 대상 행의 키 (여러 번 · AND). "
                        "update 는 키 컬럼 전부를 요구한다")
    t.add_argument("--allow-shared", action="store_true",
                   help="org-names: CM_BRANCH·CM_LAREA·CM_REGION 은 **한 벌 "
                        "공유**다 — 그 이름을 바꾸면 KR 화면도 함께 바뀐다. "
                        "그것이 결정일 때만 켠다")
    t.add_argument("--all", action="store_true",
                   help="rm: 테이블 전체 (확인 입력 + 사본 요구) · "
                        "relocate-cells: 활성 셀이 아니라 44만행 전부")
    t.add_argument("--per-gu", type=int, default=1,
                   help="seed-enb: 시군구당 eNB 수 (최대 2 — ENB_ID 대역 제약)")
    t.add_argument("--cells", type=int, default=3,
                   help="seed-enb · seed-enb-jp: eNB 당 셀 수")
    # **기본값이 없다.** 어느 ENB_ID 대역이 비어 있는지는 그 DB 의 사실이고,
    # 코드에 숫자를 두면 "비어 있다"는 가정이 남는다 — 다른 DB 에서 조용히
    # 겹치고, 같은 ENB_ID 가 두 장비를 가리키면 1분 집계가 둘의 호를 합친다.
    t.add_argument("--band-start", type=int,
                   help="seed-enb-jp: JP 전용 ENB_ID 대역의 시작 번호 "
                        "(**필수 · 기본값 없음** — 빈 대역을 실측해서 준다)")
    # 기본 0.35 는 graphio `detector.py:1512` 의 POINT 임계 0.9km 에서 나왔다.
    # 올리면 같은 eNB 가 CLUSTER·SCATTER 로 보이고 `geo_hit` 채점이 흔들린다.
    t.add_argument("--spread-km", type=float, default=0.35,
                   help="eNB 내부 좌표 분산 반경 (기본 0.35 — POINT 임계 0.9 의 안쪽)")
    t.add_argument("--geo-crosswalk", help="행정코드 crosswalk (기본 설정 pemdb1.geo_crosswalk)")
    t.add_argument("--write", action="store_true",
                   help="**실제로 적재한다.** 없으면 읽기만 한다 — "
                        "쓰는 명령(clone·seed-geo·seed-enb·relocate-cells·"
                        "org-names·code-map·add·rm·restore) 전부에 걸린다")
    t.add_argument("--dry-run", action="store_true",
                   help="(이제 기본이다 — 받아 주기만 한다. 적재는 --write)")
    t.set_defaults(fn=cmd_topo)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
