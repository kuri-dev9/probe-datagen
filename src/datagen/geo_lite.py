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
