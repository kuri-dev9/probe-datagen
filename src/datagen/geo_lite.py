"""graphio 본체 geo.py 에서 떼어 온 최소 조각.

본체 전체(행정경계 인덱스)는 셀 **생성**에만 필요하고, 여기는 기존 셀 JSON 을
읽기만 하므로 거리 계산 하나면 된다.
"""

from __future__ import annotations

import math


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


# --------------------------------------------------------------------------
# DMS 문자열 ↔ 십진 — `CM_CELL_REP_RU_LOC.LATITUDE` 의 형식
# --------------------------------------------------------------------------
#
# **이것은 graphio `store/tables.py:dms_to_decimal` 의 의도된 거울이다.**
# 사본을 두는 이유: gen 은 런타임에 graphio 를 임포트하지 않는다(운영에
# 생성기만 돌 수 있고, 두 저장소가 서로를 의존하면 이미지가 묶인다). 대신
# **테스트가 두 구현을 대조한다** — `tests/test_topo.py` 가 graphio 를
# 임포트해 왕복을 검증하고, 임포트 실패는 `skip` 이 아니라 `fail` 이다.
#
# 쓰는 쪽(`decimal_to_dms`)은 graphio 에 없다 — graphio 는 읽기만 한다.


def dms_to_decimal(value: object) -> float | None:
    """`'37:33.43/220'` → `37.562006`. 해석 불가면 ``None``.

    **0.0 을 돌려주지 않는다.** 좌표 0,0 은 기니만 바다이고 그것이 섞이면
    Incident 중심과 반경이 통째로 틀린다 (graphio 불변규칙 17).

    **초는 문자열로 읽는다.** `float` 로 읽어 소수부를 100 배 하면 `'34.2'`
    의 초가 20 이 되는데 실제 값은 2 다 — 0채움이 없는 형식이다.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        head, _, milli = text.partition("/")
        deg_text, _, rest = head.partition(":")
        min_text, _, sec_text = rest.partition(".")
        degrees = int(deg_text)
        minutes = int(min_text)
        seconds: float = int(sec_text) if sec_text else 0
        if milli:
            seconds += int(milli) / 1000
    except (TypeError, ValueError):
        return None
    # 분·초가 60 을 넘으면 DMS 가 아니다. 1 분을 넘겨 읽으면 좌표가 1.8km
    # 밀리는데 에러가 나지 않는다 — 계산 못 한 값으로 돌려 UNMAPPED 로 드러낸다.
    if not (0 <= minutes < 60 and 0 <= seconds < 60):
        return None
    return degrees + minutes / 60 + seconds / 3600


def decimal_to_dms(value: float) -> str:
    """`37.562006` → `'37:33.43/220'`. `dms_to_decimal` 의 역함수.

    **초 소수부를 `/밀리` 로 떼어 적는다.** `'37:33.43220'` 처럼 붙여 쓰면
    `dms_to_decimal` 이 초를 `43220` 으로 읽고 60 초 검사에서 탈락해
    **좌표가 통째로 버려진다** — 에러 없이 미매핑이 된다.

    **반올림으로 60 이 되는 자리를 올린다.** `59.9996` 초를 `60/000` 으로
    적으면 같은 검사에 걸린다. 밀리초 단위로 끊고 올림을 전파한다.
    """
    neg = value < 0
    v = abs(float(value))
    deg = int(v)
    rest_min = (v - deg) * 60
    minutes = int(rest_min)
    total_milli = int(round((rest_min - minutes) * 60 * 1000))
    if total_milli >= 60_000:            # 초가 60 으로 반올림됐다
        total_milli -= 60_000
        minutes += 1
    if minutes >= 60:
        minutes -= 60
        deg += 1
    sec, milli = divmod(total_milli, 1000)
    return f"{'-' if neg else ''}{deg}:{minutes}.{sec}/{milli:03d}"
