"""U-Plane 원시 XDR 의 로컬 Parquet 보관소 — **MySQL 을 거치지 않는다.**

## 왜 DB 에서 뺐나 (2026-10-14)

분당 6.3만 행의 원시 XDR 을 MySQL 에 INSERT 하는 것이 이 PoC 의 쓰기 I/O 의
거의 전부였고, 그중 **U-Plane 3종이 68%** 를 차지했다(실측). 그런데 이 세
테이블은 1분 집계(`GP_KPI_U_CELL_1M`·`GP_KPI_DOMAIN_1M`)의 **입력 전용**이다 —
집계가 끝난 뒤 화면·RCA·채점 어디에서도 다시 읽지 않는다. 아무도 읽지 않는
데이터를 트랜잭션·undo log·바이너리 로그·인덱스를 모두 태워 가며 DB 에 넣을
이유가 없다.

**C-Plane(`GP_RAW_LTE_CALL_KPI`)은 그대로 MySQL 에 둔다.** 그쪽은 SCR-06
(세션)·SCR-07(가입자) 화면과 RCA 증거팩이 원시 레코드를 직접 조회한다.

원칙은 하나로 줄었다 — **MySQL 에는 통계만 올라간다.**

## 레이아웃 — 파일 하나가 1분이다

    <root>/<series>/<YYYY-MM-DD>/<HHMM>.parquet

집계가 `[lo, hi)` **분 구간**으로 읽으므로 분 단위로 파일을 찾을 수 있어야
한다. 날짜 디렉터리를 한 겹 두는 이유는 한 디렉터리에 파일이 무한히 쌓이지
않게 하는 것이고, 보존 정리도 디렉터리째로 지울 수 있다.

압축은 **zstd** 다. 실측으로 1분치 3종 합이 1.17MB — 같은 데이터의 MySQL
적재가 수십 MB 의 쓰기를 유발하던 자리다.

**쓰기는 멱등이다.** 같은 분을 다시 돌리면 그 파일을 덮어쓴다 — DB 시절의
`DELETE` + `INSERT` 와 같은 성질이다. 임시 파일에 쓴 뒤 `os.replace` 로
바꿔치므로 **읽는 쪽이 반쯤 쓰인 파일을 보지 않는다**(같은 머신의 다른
프로세스인 `corr` 가 그 분을 집계하러 들어온다).

## 레이아웃은 두 저장소가 함께 지킨다

읽는 쪽은 graphio 의 `src/graphio/store/uplane_parquet.py` 다. **경로 규칙이
같아야 하고**, 어긋나면 집계가 조용히 0행이 된다 — 에러가 나지 않는 종류다
(`UPLANE_SLOT` ↔ graphio `tables.UPLANE_TIME` 과 같은 관계).

경로 자체는 **설정으로 받는다**(`config/datagen.yaml` 의
`pemdb1.uplane_parquet.root`). 코드에 박으면 컨테이너로 옮길 때 volume mount
지점을 바꿀 수 없다.
"""

from __future__ import annotations

import os
import shutil
from datetime import datetime, timedelta
from pathlib import Path

import polars as pl

#: 분 파일 이름 포맷. 날짜 디렉터리와 합쳐 분을 유일하게 가리킨다.
DAY_FMT = "%Y-%m-%d"
MINUTE_FMT = "%H%M"

#: 파일 확장자. 임시 파일은 여기에 `.tmp` 를 더 붙인다 — 읽는 쪽의 glob
#: (`*.parquet`)에 걸리지 않아야 반쯤 쓰인 파일이 집계에 들어가지 않는다.
SUFFIX = ".parquet"
TMP_SUFFIX = ".parquet.tmp"

#: 타입 토큰 → polars 자료형. **MySQL DDL 의 타입을 그대로 옮긴 것이다.**
#:
#: 테이블을 지우면 `information_schema` 가 없어지므로 타입의 정본이 코드로
#: 옮겨 온다 — 전에는 `uplane_mapping` 이 DB 에서 읽었다. Parquet 는
#: 자기 기술적(self-describing)이라 읽는 쪽은 이 표를 알 필요가 없다.
DTYPES: dict[str, pl.DataType] = {
    "str": pl.String(),
    "i8": pl.Int8(),
    "i32": pl.Int32(),
    "i64": pl.Int64(),
    "f64": pl.Float64(),
}


# --------------------------------------------------------------------------
# 경로
# --------------------------------------------------------------------------


def series_dir(root: Path, series: str) -> Path:
    """계열 디렉터리. **존재 여부가 "그 계열이 있는가" 의 답이다.**

    읽는 쪽(`graphio.store.uplane_parquet.available_series`)이 이 디렉터리로
    계열 유무를 판정한다. 그래서 쓰기 주체는 **레코드가 0건이어도 디렉터리는
    만들어 둔다** — 그 계열은 "있고 그 분에 트래픽이 없었다"는 뜻이다. 반대로
    디렉터리가 없으면 "원천이 없다"이고, 집계는 그것을 0 으로 적지 않는다
    (불변규칙 17).
    """
    return root / series


def minute_path(root: Path, series: str, ts: datetime) -> Path:
    """그 분의 파일 경로. ``ts`` 는 **분으로 내림된 시각**이어야 한다."""
    return (series_dir(root, series) / ts.strftime(DAY_FMT)
            / f"{ts.strftime(MINUTE_FMT)}{SUFFIX}")


def _parse_minute(day_dir: Path, name: str) -> datetime | None:
    """`<YYYY-MM-DD>/<HHMM>.parquet` → 분 시각. 규칙에 안 맞으면 ``None``.

    **이름을 문자열로 비교하지 않는다.** 파티션 정리에서 같은 실수를 한 적이
    있다 — 자릿수가 어긋나 비교가 틀렸고, 에러 없이 아무것도 지우지 않았다.
    """
    if not name.endswith(SUFFIX):
        return None
    try:
        return datetime.strptime(f"{day_dir.name} {name[:-len(SUFFIX)]}",
                                 f"{DAY_FMT} {MINUTE_FMT}")
    except ValueError:
        return None


# --------------------------------------------------------------------------
# 쓰기
# --------------------------------------------------------------------------


def write_minute(root: Path, series: str, ts: datetime,
                 frame: pl.DataFrame) -> int:
    """한 분을 Parquet 로 쓴다. 같은 분이 있으면 **덮어쓴다**(구간 멱등).

    임시 이름으로 쓴 뒤 `os.replace` 로 바꿔치는 이유는 읽는 쪽이 같은 머신의
    다른 프로세스라서다 — 그 분을 집계하러 들어온 `corr` 가 반쯤 쓰인 파일을
    열면 집계가 틀린 값을 내거나 예외로 죽는다. `os.replace` 는 같은 파일
    시스템 안에서 원자적이다.
    """
    path = minute_path(root, series, ts)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name[:-len(SUFFIX)] + TMP_SUFFIX)
    # zstd — gzip 보다 빠르고 snappy 보다 작다. 이 자리는 분당 한 번 돌고
    # 파일은 10분만 남으므로 압축률보다 쓰기 지연이 중요하다.
    frame.write_parquet(tmp, compression="zstd")
    os.replace(tmp, path)
    return frame.height


# --------------------------------------------------------------------------
# 보존 — 10분치만 남긴다
# --------------------------------------------------------------------------


def purge(root: Path, series: str, keep_minutes: int) -> int:
    """**가장 최근 분으로부터** ``keep_minutes`` 분만 남기고 지운다.

    ## 왜 벽시계가 아니라 "가장 최근 파일" 기준인가

    `pemfill` 은 과거 구간을 메운다. 벽시계로 자르면 백필한 분이 전부 기준
    밖이라 **쓰는 즉시 지워진다** — 에러 없이 데이터가 사라지고, 집계는 0행을
    굽는다. 디스크에 있는 가장 늦은 분을 기준으로 삼으면 실시간 틱과 백필이
    같은 규칙으로 동작한다.

    ## 왜 집계 여부를 보지 않는가

    사람이 그렇게 정했다(2026-10-14). 집계가 밀리면 그 분은 영원히 집계되지
    않지만, 그 상태는 **집계 테이블에 그 분의 행이 없는 것으로 나타난다** —
    0 으로 채워 "정상"으로 보이게 하는 것과 다르다(불변규칙 4·17). 집계 완료를
    기다리는 설계는 두 프로세스 사이에 상태를 하나 더 만들고, 그 상태가
    어긋나면 디스크가 무한히 자란다.
    """
    base = series_dir(root, series)
    if not base.is_dir():
        return 0
    found: list[tuple[datetime, Path]] = []
    for day in base.iterdir():
        if not day.is_dir():
            continue
        for f in day.iterdir():
            when = _parse_minute(day, f.name)
            if when is not None:
                found.append((when, f))
    if not found:
        return 0
    cutoff = max(w for w, _ in found) - timedelta(minutes=keep_minutes)
    dropped = 0
    for when, f in found:
        if when > cutoff:
            continue
        f.unlink(missing_ok=True)
        dropped += 1
    # 빈 날짜 디렉터리는 치운다. 남겨 두면 날마다 하나씩 쌓이고, 읽는 쪽이
    # 구간 안의 날짜 디렉터리를 전부 뒤지므로 조회가 점점 느려진다.
    # **계열 디렉터리 자체는 지우지 않는다** — 그 존재가 "계열이 있다"는
    # 사실을 나르기 때문이다(`series_dir` 참조).
    for day in base.iterdir():
        if day.is_dir() and not any(day.iterdir()):
            shutil.rmtree(day, ignore_errors=True)
    return dropped


def disk_usage(root: Path, series: str) -> tuple[int, int]:
    """``(파일 수, 바이트)`` — 로그에 보관 규모를 적는 데 쓴다."""
    base = series_dir(root, series)
    if not base.is_dir():
        return (0, 0)
    n = total = 0
    for day in base.iterdir():
        if not day.is_dir():
            continue
        for f in day.iterdir():
            if f.name.endswith(SUFFIX):
                n += 1
                total += f.stat().st_size
    return (n, total)
