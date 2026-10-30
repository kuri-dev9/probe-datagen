"""앱 사전 (`CM_APP_CODE`) — 세션이 **어느 앱의 트래픽인가**를 정한다.

## 운영에서는 매핑, 여기서는 그 결과를 바로 쓴다

운영 프로브는 패킷의 `Protocol·IP·Port`(또는 도메인)를 앱 사전과 맞춰
`App_ID`·`App_Group_Code` 를 붙인다. 생성기는 그 판정을 흉내 내지 않는다 —
**세션을 열 때 사전의 앱 하나를 뽑아, 이미 매핑된 것처럼** `App_ID`·
`App_Group_Code`·`App_IPv4`·`App_Port`·`Protocol`·도메인을 한 세트로 적는다
(2026-10-28 사람 결정). 장비를 셀에 랜덤으로 붙이는 것과 같은 모양이다.

**사전이 비어 있으면 옛 동작 그대로다**(`pick` 이 `None`). DB 없이 도는
테스트와 사전을 아직 안 채운 환경이 그 경로다.

## 사전 원본

`docs/jp/CM_APP_CODE_JP_DOCOMO_FDS_1000_SERVICES_V4.csv`(graphio) — 일본
서비스 1,000개. 그중 **IP·포트가 있는 것은 246개뿐**이고 나머지는 도메인으로
매핑되는 앱이다. 넣는 길은 `import_csv` 하나다.
"""

from __future__ import annotations

import csv
import ipaddress
import random
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

T_APP = "CM_APP_CODE"

TCP, UDP = "6", "17"


@dataclass(frozen=True)
class App:
    """사전 한 행 = 앱 하나의 **엔드포인트 하나**."""

    idx: str
    name: str
    group: str
    protocol: str           #: `6` | `17`
    ip: str                 #: CIDR 그대로(`192.168.49.26/32`). 빈 값 = 모름
    port: str               #: 빈 값이면 443 으로 나간다(`port_out`)
    domains: tuple[str, ...]
    weight: float

    def host(self, rng: random.Random) -> str:
        """도메인 목록에서 하나. 목록이 비면 앱 이름으로 만든다."""
        return rng.choice(self.domains) if self.domains else f"{self.name.lower()}.jp"

    def ip_out(self, rng: random.Random) -> str:
        """XDR `App_IPv4` 에 적을 주소 — CIDR 안의 한 주소.

        **사전에 IP 가 없으면 비운다**(불변규칙 17). 그 앱은 도메인으로 매핑되는
        앱이고, 어느 서버 IP 였는지는 사전이 말하지 않는다 — 지어 넣으면 IP 로
        앱을 역추적할 때 근거 없는 값이 섞인다.
        """
        if not self.ip:
            return ""
        try:
            net = ipaddress.ip_network(self.ip, strict=False)
        except ValueError:
            return self.ip
        if net.num_addresses <= 2:
            return str(net.network_address)
        return str(net.network_address + rng.randrange(1, net.num_addresses - 1))

    @property
    def port_out(self) -> str:
        return self.port or "443"


class AppBook:
    """메모리에 들고 있는 사전 — 가중 추첨과 도메인 역조회."""

    def __init__(self, apps: list[App]):
        self.apps = [a for a in apps if a.weight > 0]
        self._cum: list[float] = []
        acc = 0.0
        for a in self.apps:
            acc += a.weight
            self._cum.append(acc)
        #: 도메인 → 앱. 긴 것부터 맞춘다(`pay.rakuten.co.jp` 가 `rakuten.co.jp`
        #: 보다 먼저)
        self._suffix = sorted(((d.lower(), a) for a in self.apps for d in a.domains),
                              key=lambda kv: -len(kv[0]))

    def __bool__(self) -> bool:
        return bool(self.apps)

    def boosted(self, factor) -> "AppBook":
        """비중을 앱마다 `factor(app)` 배 한 사본 — 장애 중 대상 앱을 더 자주.

        원본을 고치지 않는다. 장애가 끝나면 원본으로 돌아가야 하고, 대상
        셀 밖은 원본을 계속 쓴다(`generator.app_book_for`).
        """
        return AppBook([App(a.idx, a.name, a.group, a.protocol, a.ip, a.port,
                            a.domains, a.weight * factor(a)) for a in self.apps])

    def pick(self, rng: random.Random) -> App | None:
        if not self.apps:
            return None
        import bisect

        return self.apps[bisect.bisect_left(self._cum, rng.random() * self._cum[-1])]

    def of_host(self, host: str) -> App | None:
        """호스트 → 앱. **DNS 레코드를 앱에 잇는 유일한 길이다** — DNS 의
        `App_IPv4` 는 앱 서버가 아니라 DNS 서버라서 IP 로는 못 잇는다."""
        h = (host or "").lower().lstrip(".")
        for d, a in self._suffix:
            if h == d or h.endswith("." + d):
                return a
        return None

    def first_of(self, group: str) -> App | None:
        """그 그룹에서 **비중이 가장 큰** 앱 — 시나리오 자동 대상."""
        cand = [a for a in self.apps if a.group == group]
        return max(cand, key=lambda a: a.weight) if cand else None

    def domains_of(self, group: str) -> tuple[str, ...]:
        return tuple(sorted({d.lower() for a in self.apps if a.group == group
                             for d in a.domains}))


def load(conn) -> AppBook:
    """`CM_APP_CODE` 를 읽는다. 테이블이 없거나 비면 빈 사전."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT APP_IDX, APP_NAME, GROUP_CODE, PROTOCOL, IP, PORT, "
                f"DOMAIN, WEIGHT FROM {T_APP} WHERE USE_YN = 'Y' "
                "ORDER BY APP_IDX, ENDPOINT_SEQ")
            rows = cur.fetchall()
    except Exception:       # 테이블이 아직 없다 — 옛 동작으로 돈다
        return AppBook([])
    return AppBook([
        App(str(r[0]), r[1], r[2], str(r[3] or TCP), r[4] or "",
            "" if r[5] is None else str(r[5]),
            tuple(d.strip() for d in (r[6] or "").split("|") if d.strip()),
            float(r[7] or 0))
        for r in rows])


# --------------------------------------------------------------------------
# CSV 적재
# --------------------------------------------------------------------------

#: **UDP 로 나가는 앱** — 비어 있는 `PROTOCOL` 을 채우는 규칙.
#:
#: ① 실시간 그룹(게임·통화) ② QUIC(HTTP/3 = UDP 443) 을 기본으로 쓰는 대형
#: 서비스 ③ 동영상 플랫폼. 셋 다 아니면 TCP 다. CSV 가 `6` 을 적어 둔 행은
#: 그대로 둔다 — 사람이 적은 값이 이긴다.
UDP_GROUPS = frozenset({"GAME", "COMMUNICATION"})
UDP_DOMAINS = (
    # QUIC 기본 (Google·Meta 계열)
    "google.com", "googlevideo.com", "youtube.com", "ytimg.com", "gstatic.com",
    "googleapis.com", "facebook.com", "fbcdn.net", "instagram.com",
    "whatsapp.net", "cdninstagram.com",
    # 동영상 스트리밍
    "nicovideo.jp", "nflxvideo.net", "netflix.com", "twitch.tv", "ttvnw.net",
    "abema.tv", "tver.jp", "unext.jp", "disneyplus.com", "bilibili.com",
    "dazn.com", "hulu.jp", "tiktokv.com", "tiktok.com",
)


def infer_protocol(group: str, domains: list[str]) -> str:
    if group in UDP_GROUPS:
        return UDP
    for d in domains:
        d = d.lower()
        if any(d == u or d.endswith("." + u) for u in UDP_DOMAINS):
            return UDP
    return TCP


#: 비중 = **CSV 순위의 Zipf** (`1/rank^0.85`). 원본이 인기 순(Google·
#: YouTube·Apple…)으로 적혀 있고, 실제 트래픽은 상위 소수 앱에 몰린다 —
#: 균등으로 두면 1,000개 중 600개인 `ECT` 가 세션의 60% 를 차지한다.
#: 지수는 `profile.SeriesProfile.domain()` 의 도메인 가중과 같다.
ZIPF_S = 0.85

#: 그룹별 **평시 비중 배율** — Zipf 위에 곱한다. 결제 앱은 CSV 뒤쪽에 있어
#: 순위만으로는 세션의 1.3% 라 `j-sc-22` 의 표본이 얇았다(사람 요청
#: 2026-10-28 "PAY 비중을 조금 높여"). ×4 로 약 5% 가 된다.
GROUP_WEIGHT = {"PAY": 4.0}


def import_csv(conn, path: Path) -> dict:
    """CSV → `CM_APP_CODE`. **같은 `APP_IDX` 는 덮는다**(REPLACE).

    다시 넣어도 결과가 같다(멱등). CSV 에 없는 앱은 건드리지 않는다 — 사람이
    DB 에서 더한 행을 지우지 않는다.
    """
    rows = list(csv.DictReader(path.open(encoding="utf-8-sig")))
    now = datetime.now()
    out, n_udp, n_inferred = [], 0, 0
    for rank, r in enumerate(rows, start=1):
        domains = [d.strip() for d in (r.get("DOMAIN") or "").split("|") if d.strip()]
        proto = (r.get("PROTOCOL") or "").strip()
        if not proto:
            proto = infer_protocol(r["GROUP_CODE"].strip(), domains)
            n_inferred += 1
        n_udp += proto == UDP
        port = (r.get("PORT") or "").strip()
        out.append([
            r["APP_IDX"].strip(), 1, r["APP_NAME"].strip(), r["GROUP_CODE"].strip(),
            None, int(proto), (r.get("IP") or "").strip() or None,
            int(port) if port else None, "|".join(domains) or None,
            (r.get("DESCRIPTION") or "").strip() or None,
            round(GROUP_WEIGHT.get(r["GROUP_CODE"].strip(), 1.0)
                  / rank ** ZIPF_S, 6), "Y", now])
    with conn.cursor() as cur:
        cur.executemany(
            f"REPLACE INTO {T_APP} (APP_IDX, ENDPOINT_SEQ, APP_NAME, GROUP_CODE, "
            "APP_TYPE, PROTOCOL, IP, PORT, DOMAIN, DESCRIPTION, WEIGHT, USE_YN, "
            "UPDATED_TIME) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", out)
    conn.commit()
    return {"rows": len(out), "udp": n_udp, "protocol_inferred": n_inferred}
