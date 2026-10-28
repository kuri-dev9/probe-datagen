"""1분 XDR 파일 생성기.

파일 규약은 §2.3.1을 따른다 — 계열별 1분 파일, ``.tmp`` 로 쓰고 rename, 그리고
``.done`` 마커. 부록 A-24가 "부분 파일 읽기를 막는 필수 합의 사항"으로 남겨둔
항목을 시뮬레이터 쪽에서 확정해 두는 셈이다. 집계기가 쓰는 중인 파일을 읽으면
그 분의 KPI가 과소 집계되고, 이는 **없는 장애를 만들어낸다.**

레코드는 프로파일(실 샘플 분포) × 셀 마스터 × 시나리오 변형의 합성이다.
구성비(call_type·APN·Cause·기종·도메인)는 실데이터에서, 품질 수준(소통률·절단률)은
정상 구간의 관측값에서 가져오고, 장애는 시나리오가 레코드를 변형해 만든다.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from .fields import SERIES_FIELDS
from .cells import Cell, encrypt_id
from .plmn import Plmn
from .profile import Sampler, SeriesProfile
from .identity import synth_imei, synth_imsi, synth_mdn
from .scenarios import Rec, MUTATORS, Scenario

SEP = "\x1e"

SERIES_FILE_PREFIX = {
    "lte_call": "LTE-CALL-KPI",
    "transport": "TRANSPORT",
    "http": "HTTP",
    "dns": "DNS",
}
SERIES_DIR = {"lte_call": "lte-call", "transport": "transport", "http": "http", "dns": "dns"}

#: 시간대별 트래픽 곡선 (0~23시 상대 배율).
#:
#: **04시가 최저, 19시가 최고다** (2026-10-14 사용자 지정). 07~24 가 높고
#: 01~06 이 낮으며, 사이를 계단 없이 오르내린다 — 시간이 바뀌는 자리에서 값이
#: 점프하면 1분 집계 그래프에 수직선이 생기고, 그것이 장애처럼 보인다.
#:
#: **진폭이 2배(0.50~1.00)인 이유.** 이 곡선을 두 곳에 쓴다 —
#: 레코드 **개수**(`_volume`)와 레코드당 **트래픽 크기**(`_traffic_scale`).
#: 총 트래픽은 둘의 곱이므로 `0.50² = 0.25` ~ `1.00² = 1.00`, 즉 **4배**가
#: 된다. 사용자가 지정한 "많고 적은 차이 약 4배"가 총 트래픽 기준이다.
#:
#: 야간 저트래픽 셀에서 최소 표본 규칙(§4.8.4)이 실제로 필요한지 확인할 수
#: 있어야 한다는 원래 목적도 그대로다 — 04시 셀당 표본이 1분 창에서 모자라
#: `ESCALATION_WINDOWS` 확대를 밟는다.
DIURNAL = [
    # 0     1     2     3     4     5     6     7     8     9    10    11
    0.62, 0.56, 0.52, 0.51, 0.50, 0.54, 0.62, 0.73, 0.83, 0.88, 0.91, 0.92,
    #12    13    14    15    16    17    18    19    20    21    22    23
    0.91, 0.90, 0.90, 0.92, 0.95, 0.97, 0.99, 1.00, 0.98, 0.92, 0.82, 0.71,
]

#: 계열별 **진폭 축소**. `1.0` 이면 곡선 그대로, `0.0` 이면 평탄하다.
#: 적용식은 `1 - w·(1 - DIURNAL[h])` — 기준점이 1.0 이므로 피크는 그대로
#: 두고 저점만 끌어올린다.
#:
#: **`lte_call` 은 0.25 다.** C-Plane xDR 은 호 하나하나의 시그널링 기록이고
#: 사용량(바이트)과 직접 묶이지 않는다 — 데이터를 많이 쓰는 시간대라고 호
#: 수가 그만큼 늘지는 않는다(사용자 판단: "시그널 데이터는 호 자체만 판단").
#: 04시↔19시 **1.14배**가 된다. 0 으로 두지 않는 것은 완전히 평탄한 호 수가
#: 더 비현실적이기 때문이고, 크게 두지 않는 것은 **셀 축 최소 표본**이 야간에
#: 무너지면 탐지가 통째로 서지 않기 때문이다(불변규칙 5).
#:
#: **U-Plane 세 항목의 뜻이 바뀌었다** (2026-10-14, 세션 모델). 더는 계열별
#: 레코드 **개수**에 걸리지 않는다 — U-Plane 레코드 수는 이제 성공 호에서
#: 파생되므로(`session.SessionEngine`) 개수 쪽 곡선은 `lte_call` 과
#: `uplane_sessions` 가 함께 만든다. 이 셋이 남아서 하는 일은 **세션당 바이트**
#: 뿐이다(`_traffic_scale`). 지우지 않은 것은 "U-Plane 트래픽 크기 곡선"이라는
#: 역할이 그대로 있고, 계열별로 다르게 두고 싶을 자리이기 때문이다.
#:
#: `uplane_sessions` 가 **호당 세션 수**에 걸리는 새 항목이다. 0.857 이면
#: 04시 배율이 `1 - 0.857×0.5 = 0.571` = 1/1.75 이고, 총 트래픽이
#:
#:     호 1/1.14  ×  호당 세션 1/1.75  ×  세션 바이트 1/2  =  총 1/4
#:
#: 가 되어 사용자가 지정한 4배를 유지한다(설계 §4). 곡선을 세 자리에 나눠
#: 거는 이유는 하나에 몰면 그 축만 비현실적으로 출렁이기 때문이다 — 실제로는
#: 피크에 "쓰는 사람이 늘고, 더 자주 쓰고, 한 번에 더 많이 쓴다".
DIURNAL_WEIGHT: dict[str, float] = {
    "lte_call": 0.25,
    "uplane_sessions": 0.857,
    "transport": 1.0,
    "dns": 1.0,
    "http": 1.0,
}


def diurnal_factor(hour: int, series: str = "", weight: float | None = None) -> float:
    """시간대 배율. ``series`` 의 진폭 축소를 반영한다.

    `_volume`(개수)과 `_traffic_scale`(크기)이 **같은 함수**를 쓴다 — 두 자리가
    따로 계산하면 한쪽만 고쳐질 때 총 트래픽 배수가 조용히 달라진다.
    """
    w = DIURNAL_WEIGHT.get(series, 1.0) if weight is None else weight
    return 1.0 - w * (1.0 - DIURNAL[hour % 24])

_STRINGY = (
    "IMSI", "MDN", "IMEI", "ServiceCode", "PayCode", "Gender", "Age", "Vendor",
    "Model", "APN", "ims_APN", "VLAN", "IPv6", "SessionID", "QueryName", "Host",
    "Referer", "UserAgent", "ContentType", "ResolvedIP", "ClientHelloDomain",
    "URI", "Method", "eNB_ID", "PLMN", "ExchangeNumber", "Domain", "Cell_ID",
)


def _default_for(name: str) -> str:
    if name.startswith("RESERVED"):
        return ""
    if "Time" in name or name.endswith("_time") or name.endswith("time"):
        return "0.0"
    if any(tok in name for tok in _STRINGY):
        return ""
    return "0"


def _template(series: str) -> list[str]:
    return [_default_for(f) for f in SERIES_FIELDS[series]]


def _index(series: str) -> dict[str, int]:
    return {f: i for i, f in enumerate(SERIES_FIELDS[series])}


#: 가입자 식별 필드 — C-Plane·U-Plane 이 **같은 목록·같은 순서**로 쓴다.
#:
#: U-Plane 세션 모델이 호에서 이 값들을 상속하므로(`session.Call.user`) 한쪽만
#: 늘어나면 U-Plane 레코드의 그 필드가 조용히 빈다. 그래서 목록을 한 자리에
#: 두고 `_common_user` 와 `session.USER_FIELDS` 가 함께 읽는다.
USER_FIELDS = (
    "IMSI", "MDN", "IMEI", "ServiceCode", "PayCode", "Gender", "Age",
    "Vendor", "Model", "imsi_mcc_mnc_info", "ExchangeNumber",
)


@dataclass
class Subscriber:
    imsi: str
    mdn: str
    imei: str
    service_code: str
    pay_code: str
    gender: str
    age: str
    vendor: str
    model: str
    mcc_mnc: str
    exchange: str
    dcnr: str
    home_cell: int
    #: 이 가입자가 타는 경로 인덱스. `-1` 은 **고정 가입자**다.
    #:
    #: 이동을 가입자 속성으로 두는 이유: 호마다 추첨하면 같은 가입자가
    #: 어떤 호에서는 움직이고 어떤 호에서는 안 움직인다. 그러면 궤적이
    #: 가입자 단위로 이어지지 않아 **일별 체감(S-01)·민원 조회(SCR-07')가
    #: "이 사람이 어디를 다녔나"를 말할 수 없다.**
    route: int = -1
    #: 경로 위의 현재 칸.
    route_pos: int = 0
    #: 진행 방향. 순환선이 아니면 끝에서 뒤집힌다(상행/하행).
    route_fwd: bool = True
    #: 이 칸에 들어온 시각(epoch). 체류시간이 지나면 다음 칸으로 간다.
    route_t: float = 0.0
    #: **하차 칸.** `-1` 은 끝없이 순환(노선 전체를 도는 사람).
    #:
    #: 여정을 두는 이유: 실제 승객은 노선을 영원히 돌지 않는다. 타고,
    #: 내리고, 머물고, 다시 탄다. 영원히 돌게 두면 **역 주변 체류가 없어**
    #: 역세권 혼잡(`j-sc-14` 도심 역 저녁 러시)이 생기지 않는다 — 모든
    #: 가입자가 늘 이동 중이다.
    route_to: int = -1
    #: **승차 칸.** 여정이 끝나면 여기서 다시 시작한다.
    route_from: int = -1
    #: 여정 회차. 하차 후 다시 탈 때 늘어난다.
    #:
    #: **세션이 여정 경계를 넘지 않게 하는 수단이다.** 재승차는 하차 칸에서
    #: 승차 칸으로 돌아가는 것이고 그 둘은 인접하지 않다 — 그때 열려 있던
    #: 세션이 경로를 계속 따르면 **인접하지 않은 셀로 순간이동한 핸드오버**가
    #: 나온다. 세션은 자기가 시작한 회차를 기억하고, 회차가 바뀌면 경로
    #: 추종을 멈춘다(`session._route_cell`).
    trip: int = 0
    #: 하차 후 **다시 탈 시각**(epoch). `0` 은 승차 중이다.
    route_wait: float = 0.0


#: 거주지 격자 한 칸(도). 0.05도 ≈ 5.5km — `home_radius_m`(10km)보다 작아야
#: 고리 하나로 반경을 덮는 데 두 칸이면 충분하다.
_GRID_DEG = 0.05


def _dist_m(a: Cell, b: Cell) -> float:
    """두 셀 사이 거리(m). 경도는 위도에 따라 줄어들므로 보정한다.

    `handover._dist2` 와 **다른 함수인 이유**: 저쪽은 정렬용이라 제곱 거리
    (도 단위)를 쓰고 루트를 안 씌운다. 여기는 **미터로 자르는** 자리라
    실제 거리가 필요하다.
    """
    k = math.cos(math.radians(a.latitude)) or 1.0
    return math.hypot((a.latitude - b.latitude) * 111_320.0,
                      (a.longitude - b.longitude) * 111_320.0 * k)


def user_values(sub: Subscriber) -> tuple[str, ...]:
    """`USER_FIELDS` 순서의 값. 순서를 바꾸면 U-Plane 상속이 어긋난다."""
    return (sub.imsi, sub.mdn, sub.imei, sub.service_code, sub.pay_code,
            sub.gender, sub.age, sub.vendor, sub.model, sub.mcc_mnc,
            sub.exchange)


class Generator:
    """셀 마스터 + 프로파일 + 시나리오로 1분치 XDR 레코드를 만든다."""

    def __init__(
        self,
        cells: list[Cell],
        profiles: dict[str, SeriesProfile],
        scenarios: list[Scenario],
        rates: dict[str, int],
        seed: int = 20260903,
        diurnal: bool = True,
        subscriber_pool: int = 0,
        success_only: bool = True,
        end_only: bool = False,
        #: 홈 PLMN · IMSI 접두. `None` 이면 한국 기본값(현 동작)이다.
        plmn: "Plmn | None" = None,
    ):
        self.cells = cells
        self.profiles = profiles
        self.scenarios = scenarios
        self.rates = rates
        self.diurnal = diurnal
        # 성공 전용 베이스라인 (2026-10-12 결정). 실측 분포를 그대로 쓰면 평시
        # 소수 실패·절단·First Error 가 섞인다 — PoC 는 "평소 전부 녹색"이 기본이고
        # 실패는 시나리오 주입으로만 만든다. 끄면(False) 원본의 실측 재현 동작.
        self.success_only = success_only
        # 호 종료 레코드만 만든다. `OngoingFlag` 를 프로파일에서 뽑으면 '2'(종료)가
        # 40% 뿐이고, 종료 레코드에만 채우는 필드(initial_*_duration ·
        # UEContextRelease_* · initial_nr_conn_time)가 나머지 60% 에서 빈다.
        # 상용 적재는 추출 조건이 `ONGOINGFLAG='2'` 라서 전 레코드를 '2' 로
        # 올려 쓰는데, 그러면 **"호 종료인데 지표가 비어 있는" 레코드**가 된다.
        # 실측 결과 N-02(NR 레그 연결률)가 96.5% 대신 38% 로 나와 셀 184건이
        # 허위 Critical 이 됐다 — 없는 장애를 만든 것이다(불변규칙 4·17).
        self.end_only = end_only
        self.rng = random.Random(seed)
        #: 홈 PLMN · IMSI 접두 (`plmn.Plmn`). `None` 이면 한국 기본값이고
        #: 그것이 현 동작이다 — 구 경로(`generate_minute`)가 설정을 모른다.
        self.plmn = plmn or Plmn()
        self.samplers = {
            s: Sampler(p, random.Random(seed + i)) for i, (s, p) in enumerate(profiles.items())
        }
        self.templates = {s: _template(s) for s in SERIES_FIELDS}
        self.index = {s: _index(s) for s in SERIES_FIELDS}

        self.cell_weights = [c.traffic_weight for c in cells]
        self._cum = self._cumulative(self.cell_weights)
        self.cell_by_key = {c.cell_key: c for c in cells}
        #: 셀 키 → `cells` 인덱스. `_sub_for` 가 인덱스를 받으므로 필요하다 —
        #: `_pick_cell` 은 `Cell` 을 돌려주고 `_subs_by_cell` 은 인덱스로
        #: 색인돼 있다.
        self.cell_index = {c.cell_key: i for i, c in enumerate(cells)}
        #: 전환 토폴로지. `PemdbGen` 이 걸어 준다.
        #:
        #: **거주지 판정에는 쓰지 않는다**(2026-10-27). 전환 상대는 k-최근접
        #: 이어야 하고(이웃이 없는 셀이 없어야 한다) 그래서 희소 지역에서
        #: 수십 km 를 잡는다 — 거주지로는 "주변"이 아니다. 거주지는
        #: `_cells_around` 가 **거리로** 찾는다.
        self.ho_topo = None
        self._near_cache: dict[int, list[int]] = {}
        self._far_cache: dict[int, list[int]] = {}
        self._grid: dict[tuple[int, int], list[int]] | None = None
        #: 거주지로 볼 **주변 반경**(m). 전환 토폴로지의 k-최근접과 다른
        #: 값이다 — 전환은 "이웃이 없는 셀이 없어야" 하므로 거리를 안 자르고,
        #: 거주는 "주변이어야" 하므로 자른다.
        #:
        #: 10km 는 도심 생활권이다. 실측에서 자르지 않으면 90km 가 나왔고,
        #: 2km 로 조이면 희소 지역 셀이 자기 주민만 보게 되어 꼬리가 사라진다.
        self.home_radius_m = 10_000.0
        #: 가입자를 **거주지 근처에서 뽑나.** 기본은 끄기다.
        #:
        #: **샘플 XDR 이 이 축을 규정하지 않는다** — 분포에 가입자↔셀 관계가
        #: 없다(호출부 주석의 실측 목록). 규정하지 않는 축에 모델을 넣는 것은
        #: 사람의 결정이어야 하고, 켜면 셀·분 `SUBS` 가 -22% 움직여 기존
        #: 기준선과 전후 비교가 끊긴다.
        #:
        #: 켜야 하는 때: 일별 체감(S-01)·민원 조회(SCR-07' "이 가입자가 어디를
        #: 다녔나")를 검증할 때. 끄면 한 가입자가 60분에 서로 다른 셀 25개
        #: (전체 348개 중)에 나타난다 — 그 화면이 성립하지 않는다.
        self.home_locality = False
        #: eNB 별 셀 목록 — **평시 PathSwitch 의 Last 셀**이 같은 eNB 의 다른
        #: 셀이어야 한다(`session._by_enb` 와 같은 이유). 다른 eNB 로 보내면
        #: 한 호가 두 노드를 탄 것처럼 되어 노드 축 집계가 호 안에서 갈린다.
        self.cells_by_enb: dict[str, list[Cell]] = {}
        for c in cells:
            self.cells_by_enb.setdefault(c.enb_id, []).append(c)
        #: `pemdb1.Baseline` — 평시 배경(절차 플래그·절단·PathSwitch)의 모수.
        #: `PemdbGen.__init__` 이 걸어 준다. 구 경로(`generate_minute`)는
        #: `None` 이고 그때는 평시 배경이 **없다** — 그 경로는 상용 분포를
        #: 덮지 않으므로 분모 자체를 프로파일이 들고 있다.
        self.baseline = None
        #: 상용 APN 풀(실측 빈도순). `PemdbGen` 이 걸어 준다 — 시나리오의 APN
        #: 대상이 레코드에 실제로 존재하는 값이어야 한다.
        self.apn_pool: list[str] = []

        #: 이동 가입자가 **호를 만들 비율**. 태운 비율(`seat_routes(share)`)과
        #: 다른 축이다 — 움직이는 사람이 전체의 12% 라도 그들이 더 자주
        #: 통신하면 호 비중은 더 높다. 기본 0 이라 **경로를 깔아도
        #: `mobility_share` 를 올리지 않으면 호는 평소와 같다.**
        self.mobility_share = 0.0
        #: 이동 경로. `seat_routes` 가 채운다 — 비어 있으면 아무도 움직이지
        #: 않고, 그때 동작은 이 기능이 없던 때와 같다.
        self.routes: list = []
        #: 여정 명세 — `[{route, from, to, share}]`. 역 코드로 적는다.
        #: `seat_routes` **전에** 걸어 둔다. 비면 모두 끝없이 순환한다
        self.journeys: list = []
        #: 하차 후 머무는 시간 범위(초). 그 동안 셀이 고정이고 전환이 없다
        self.off_sec: tuple[int, int] = (300, 1800)
        #: **TAC 가 바뀐 전환** — `[(가입자 index, 이전 셀, 새 셀)]`.
        #:
        #: TA 경계를 넘으면 단말이 Tracking Area Update 를 한다. 그건 핸드오버
        #: 레코드와 **별개의 C-Plane 절차**이고(`call_type 5`), 그 레코드가
        #: 없으면 "TAC 가 바뀌었는데 아무 절차도 없다"가 된다 — TAU 성공률·
        #: 소요(C-01~05 의 TAU 갈래)가 통째로 비고, 0 은 "문제 없음"으로
        #: 읽힌다(불변규칙 17).
        #:
        #: `records()` 가 매 분 비운다. 비우지 않으면 같은 전환으로 TAU 가
        #: 계속 나온다.
        self.tau_events: list[tuple[int, object, object]] = []
        #: 이동 가입자 인덱스. `seat_routes` 가 채운다
        self._mobile: list[int] = []
        pool = subscriber_pool or max(2000, len(cells) * 60)
        self.subs = self._make_subscribers(pool)
        #: IMSI → 가입자 인덱스. **U-Plane 세션이 이것으로 되찾는다** —
        #: `Call` 은 가입자 객체가 아니라 `USER_FIELDS` 값만 들고 가므로
        #: (메모리 이유, `Call` 머리말) 이동 상태를 보려면 역색인이 있어야 한다.
        self.sub_by_imsi = {s.imsi: i for i, s in enumerate(self.subs)}
        self._subs_by_cell: dict[int, list[int]] = {}
        for i, s in enumerate(self.subs):
            self._subs_by_cell.setdefault(s.home_cell, []).append(i)

        self._bind_scenario_targets()

    # -- setup -----------------------------------------------------------

    @staticmethod
    def _cumulative(weights: list[float]) -> list[float]:
        out, acc = [], 0.0
        for w in weights:
            acc += w
            out.append(acc)
        return out

    # -- 이동 -------------------------------------------------------------

    def seat_routes(self, routes: list, share: float, rng) -> None:
        """경로를 깔고 가입자 일부를 태운다.

        **호출하지 않으면 아무도 움직이지 않는다** — `route = -1` 이 기본이고
        그때 동작은 이 기능이 없던 때와 같다. 역호환을 그렇게 둔다.

        출발 칸을 흩뿌린다. 전부 0번 칸에서 시작하면 **모두 같은 타이밍에
        같은 구간을 전환해** 셀 쌍 표가 파도처럼 움직인다 — 그건 노선이
        아니라 단체 행진이다.
        """
        # **먼저 전원 내린다.** 빈 목록으로 부르는 것이 "이동을 끄는" 길이고,
        # 내리지 않으면 이미 태운 가입자가 사라진 경로를 가리킨다 — 테스트가
        # 그 상태를 물려받아 뒤 테스트가 알 수 없는 이유로 깨진다.
        for sub in self.subs:
            sub.route = -1
            sub.route_pos = 0
            sub.route_fwd = True
            sub.route_t = 0.0
        self.routes = routes
        self._mobile = []
        if not routes or share <= 0.0:
            return
        for sub in self.subs:
            if rng.random() >= share:
                continue
            ri = rng.randrange(len(routes))
            sub.route = ri
            sub.route_pos = rng.randrange(len(routes[ri]))
            sub.route_fwd = rng.random() < 0.5
            # 체류시간 안의 임의 지점에서 시작한다 — 전진 타이밍을 흩는다
            sub.route_t = -rng.random() * routes[ri].dwell_sec[sub.route_pos]
        self._mobile = [i for i, s in enumerate(self.subs) if s.route >= 0]
        if self.journeys:
            self._assign_journeys(rng)

    @staticmethod
    def _journey_pos(rt, v, where: str) -> int:
        """`from`/`to` 값을 **칸 번호**로 바꾼다.

        **모델은 칸 번호다.** 정수를 그대로 받고, 문자열이면 노선이 들고 온
        역 색인(`station_pos`)으로 푼다 — 설정을 사람이 읽기 좋게 적을 수
        있게 하는 **입력 편의**일 뿐이고, 이 아래로는 역이 무엇인지 아무도
        모른다.

        **범위를 벗어나거나 모르는 이름이면 멈춘다.** 조용히 자르면 여정이
        엉뚱한 구간이 되고, "정의한 구간에 가입자가 없다"는 결과가 나오는데
        원인이 설정 오타다.
        """
        if isinstance(v, bool):
            raise RuntimeError(f"journey {where} 가 불린이다: {v!r}")
        if isinstance(v, int):
            if not (0 <= v < len(rt)):
                raise RuntimeError(
                    f"journey {where}={v} 가 노선 {rt.id} 의 칸 범위"
                    f" 0~{len(rt)-1} 를 벗어난다")
            return v
        pos = rt.pos_of(str(v))
        if pos is None:
            raise RuntimeError(
                f"journey {where}={v!r} 를 노선 {rt.id} 에서 못 찾는다"
                f" (아는 이름: {sorted(rt.station_pos)})")
        return pos

    def _assign_journeys(self, rng) -> None:
        """가입자에게 **승차 칸 → 하차 칸**을 준다.

        ## 한쪽 방향만이다

        한 가입자는 `5→9` 또는 `21→19` 처럼 **한 방향으로만** 간다. 하차하면
        머물고, 다시 탈 때도 **같은 방향으로 같은 구간**을 간다.

        전에는 왕복으로 두었다(내린 칸에서 타고 온 칸으로). 그러면 한 가입자가
        두 방향을 다 겪어 **그 사람의 전환 구간이 양방향으로 섞인다** — 장애를
        한 방향 구간에 주입했을 때 영향 가입자가 반대 방향 통행자까지 포함해
        버린다.

        `share` 합이 1 보다 작으면 나머지는 끝없이 순환한다 — 노선을 도는
        사람도 있어야 전 구간에 트래픽이 깔린다.
        """
        by_id = {r.id: (i, r) for i, r in enumerate(self.routes)}
        spec = []
        for j in self.journeys:
            got = by_id.get(j["route"])
            if got is None:
                raise RuntimeError(
                    f"journey 의 모르는 노선 {j['route']!r} "
                    f"(아는 것: {sorted(by_id)})")
            ri, rt = got
            a = self._journey_pos(rt, j["from"], "from")
            b = self._journey_pos(rt, j["to"], "to")
            if a == b:
                raise RuntimeError(
                    f"journey {j['route']} 의 from·to 가 같은 칸({a})이다 —"
                    f" 이동이 0 이고 하차 판정이 즉시 선다")
            spec.append((float(j.get("share", 0.0)), ri, a, b))
        for i in self._mobile:
            sub = self.subs[i]
            x = rng.random()
            for share, ri, a, b in spec:
                x -= share
                if x <= 0:
                    sub.route, sub.route_pos = ri, a
                    sub.route_from, sub.route_to = a, b
                    # 방향은 칸 번호로 정한다. 외선·내선이 **별 노선**이라
                    # 칸 번호가 늘어나는 쪽이 그 노선의 진행 방향이고,
                    # 순환선에서 반대로 돌면 같은 두 역 사이인데 지나는
                    # 구간이 전혀 다르다
                    sub.route_fwd = b > a
                    sub.route_t = -rng.random() * self.routes[ri].dwell_sec[a]
                    sub.route_wait = 0.0
                    break

    def advance_routes(self, now: float) -> int:
        """체류시간이 지난 이동 가입자를 다음 칸으로. **매 분 한 번 부른다.**

        반환은 움직인 가입자 수다 — 0 이면 이동이 멈춘 것이고, 그건 보통
        `seat_routes` 를 안 불렀거나 체류시간이 분보다 긴 경우다.

        한 번에 **한 칸만** 간다. 밀린 만큼 여러 칸 보내면 중간 칸을 건너뛰어
        그 구간의 전환이 데이터에 없다 — 체류시간이 분보다 짧으면 그만큼
        천천히 진행하는 것이 낫다(한 세션이 여러 칸을 겪는 것이 목적이다).
        """
        moved = 0
        for i, sub in enumerate(self.subs):
            if sub.route < 0:
                continue
            rt = self.routes[sub.route]
            if sub.route_wait > 0.0:
                # **하차해서 머무는 중이다.** 셀은 그대로이고 전환이 없다 —
                # 그게 역 주변 체류이고, 역세권 혼잡이 거기서 생긴다
                if now < sub.route_wait:
                    continue
                sub.route_wait = 0.0
                sub.route_t = now
                if sub.route_to >= 0:
                    # **같은 구간을 같은 방향으로 다시 간다.** 왕복이 아니다 —
                    # 한 가입자의 통행은 한 방향이다(`_assign_journeys`).
                    #
                    # 승차 칸으로 되돌리는 것은 셀의 불연속이다. 그래서 회차를
                    # 올려 **그 전에 열린 세션이 따라오지 못하게** 한다 —
                    # 따라오면 인접하지 않은 셀로 순간이동한 핸드오버가 된다.
                    sub.route_pos = sub.route_from
                    sub.trip += 1
                continue
            if now - sub.route_t < rt.dwell_sec[sub.route_pos]:
                continue
            was = rt.cells[sub.route_pos]
            sub.route_pos, sub.route_fwd = rt.step(sub.route_pos, sub.route_fwd)
            sub.route_t = now
            moved += 1
            # **TAI List 밖으로 나갔을 때만 TAU 다.** TA 경계가 아니다 —
            # MME 가 UE 에게 TA **목록**을 주고 목록 밖에 들어갈 때만 TAU 가
            # 난다(`handover.build_ta_lists` 머리말).
            #
            # `tac` 으로 판정했더니 도심에서 폭증했다 — 실측으로 신주쿠 2km
            # 안에 TAC 가 49개이고 이웃 셀 쌍의 77% 가 TA 경계를 넘는다.
            #
            # **목록이 `0`(미정)이면 판정하지 않는다.** 위치를 몰라 묶지 못한
            # 셀이고, 경계를 지어내지 않는다(불변규칙 12·17).
            now_c = rt.cells[sub.route_pos]
            if (now_c is not was and was.ta_list and now_c.ta_list
                    and now_c.ta_list != was.ta_list):
                self.tau_events.append((i, was, now_c))
            if sub.route_to >= 0 and sub.route_pos == sub.route_to:
                a, b = self.off_sec
                sub.route_wait = now + self.rng.randint(a, b)
        return moved

    def cell_of(self, sub: Subscriber) -> Cell | None:
        """이동 가입자의 **현재 위치 셀**. 고정 가입자는 `None`."""
        if sub.route < 0:
            return None
        return self.routes[sub.route].cells[sub.route_pos]

    def pick_mobile(self):
        """이동 가입자 하나와 그 위치 셀. 태운 가입자가 없으면 `None`.

        호 생성이 **셀을 먼저 고르고 가입자를 나중에** 고르는 구조라
        (`_pick_cell` → `_sub_for`), 이동 가입자는 그 순서를 뒤집어야 한다 —
        그 사람이 지금 **어디 있는지**가 먼저다.
        """
        if not self._mobile:
            return None
        sub = self.subs[self._mobile[self.rng.randrange(len(self._mobile))]]
        return sub, self.routes[sub.route].cells[sub.route_pos]

    def tau_rec(self, ts, slot_start: float, sub: Subscriber,
                src: "Cell", dst: "Cell"):
        """TAC 경계를 넘은 단말의 **Tracking Area Update** 레코드.

        `_lte_call` 을 복사하지 않고 그것을 불러 쓴 뒤 갈아 끼운다 — 복사하면
        154필드 중 한쪽만 고쳐지는 자리가 생긴다. 바꾸는 것은 **가입자·셀·
        절차**뿐이다.

        `call_type` 은 호출부가 `_coerce(force_call_type=...)` 로 지켜야 한다 —
        `_coerce` 가 실측 분포로 덮기 때문이다. 여기서 세워도 거기서 지워진다.

        **`old_call_Last_TAC` 에 떠난 TAC 를 적는다.** TAU 가 왜 일어났는지는
        그 값과 `interval_First_TAC` 의 차이로만 읽힌다 — 같은 값이면
        "TA 가 안 바뀌었는데 TAU 를 했다"가 된다.
        """
        rec, _cell = self._lte_call(ts, slot_start)
        self._common_user(rec, sub)
        # 새 TA 에서 수행한다 — 셀은 전환 **후**다
        rec.set("First_eNB_ID", dst.enb_id)
        rec.set("First_Cell_ID", dst.cell_id)
        rec.set("First_eNB_VLAN_ID", dst.vlan_id)
        rec.set("First_eNB_PLMN", dst.plmn)
        rec.set("Last_eNB_ID", dst.enb_id)
        rec.set("Last_Cell_ID", dst.cell_id)
        rec.set("Last_eNB_VLAN_ID", dst.vlan_id)
        rec.set("Last_eNB_PLMN", dst.plmn)
        rec.set("interval_First_eNB_ID", dst.enb_id)
        rec.set("interval_First_Cell_ID", dst.cell_id)
        rec.set("interval_First_eNB_C_UID", dst.c_uid)
        rec.set("interval_First_TAC", dst.tac)
        rec.set("interval_First_eNB_VLAN_ID", dst.vlan_id)
        rec.set("interval_First_eNB_PLMN", dst.plmn)
        rec.set("MME_ID", dst.mme_id)
        rec.set("SGW_ID", dst.sgw_id)
        rec.set("PGW_ID", dst.pgw_id)
        rec.set("ims_PGW_ID", dst.ims_pgw_id)
        # 떠난 TA — TAU 의 사유다
        rec.set("old_call_Last_eNB_ID", src.enb_id)
        rec.set("old_call_Last_Cell_ID", src.cell_id)
        rec.set("old_call_Last_TAC", src.tac)
        rec.set("old_call_Last_eNB_PLMN", src.plmn)
        return rec, dst

    def _pick_cell(self) -> Cell:
        import bisect

        x = self.rng.random() * self._cum[-1]
        return self.cells[bisect.bisect_left(self._cum, x)]

    def _make_subscribers(self, n: int) -> list[Subscriber]:
        smp = self.samplers.get("lte_call")
        rng = random.Random(self.rng.randrange(1 << 30))
        subs: list[Subscriber] = []
        for i in range(n):
            vendor = smp.categorical("Vendor", "100SS") if smp else "100SS"
            model = smp.categorical("Model", "A1DE") if smp else "A1DE"
            # `mcc_mnc` 를 먼저 정한다 — 규격이 `imsi_mcc_mnc_info` 를 "first 7
            # characters of imsi" 로 못 박았으므로 IMSI 가 그것에서 파생돼야
            # 한다. 거꾸로 하면 두 컬럼이 어긋나고 에러는 나지 않는다.
            # **설정이 프로파일을 이긴다.** `data/profile.json` 의
            # `imsi_mcc_mnc_info` 분포는 상용 실측이라 전부 한국 값이다 —
            # 그대로 쓰면 `plmn.home` 을 일본으로 바꿔도 가입자 IMSI 가
            # `450` 으로 나오고 **에러가 없다.** `normalize` 가 분포 모양
            # (끝 2자 = 사업자 하위 버킷)은 유지하고 앞 5자만 바꾼다.
            # 7자가 아닌 값(`""` 1.2% · `"__other__"` 0.6%)도 그쪽이
            # 대표값으로 채운다 — 빈 값이면 IMSI 를 만들 수 없다.
            mcc_mnc = self.plmn.normalize(
                smp.categorical("imsi_mcc_mnc_info", "") if smp else "")
            subs.append(
                Subscriber(
                    # 합성 평문이다 — 암호문을 만들지 않는다(`identity` 머리말).
                    # 인덱스 `i` 로 만들어 같은 seed 면 같은 식별자가 나온다.
                    imsi=synth_imsi(mcc_mnc, i),
                    mdn=synth_mdn(i),
                    imei=synth_imei(vendor, model, i),
                    # **ServiceCode 는 손대지 않는다.** 40종 암호문인데 대응하는
                    # 코드 테이블이 없어 체계를 알 수 없다 — 모르는 것을
                    # 지어내지 않는다.
                    service_code=encrypt_id(f"svc:{i % 40}", "sub"),
                    pay_code=smp.categorical("PayCode", "NA00006538") if smp else "NA00006538",
                    gender=smp.categorical("Gender", "1") if smp else "1",
                    age=f"{rng.randint(12, 88):03d}",
                    vendor=vendor,
                    model=model,
                    mcc_mnc=mcc_mnc,
                    exchange=f"{rng.randint(1000, 9999)}",
                    dcnr=smp.categorical("ue_dcnr", "-1") if smp else "-1",
                    home_cell=rng.randrange(len(self.cells)),
                )
            )
        return subs

    #: 도메인을 프로파일에서 받아야 하는 kind — 둘 다 한 Host 에 집중시킨다
    _DOMAIN_KINDS = ("external_server", "external_path_latency")
    #: APN 을 프로파일에서 받아야 하는 kind.
    #:
    #: **상위 트래픽 APN 을 고른다**(V3 §SC-19 권장안). 그래야 그 APN 의 실패가
    #: `C-02 CELL/SGG` 를 임계 아래로 끌어내려 Incident 가 생기고, 그 위에서
    #: `R-07` 이 APN 을 지목한다 — APN 축 `DetectSpec` 을 새로 만들지 않아도
    #: 된다. 꼬리 APN 을 고르면 희석되어 **Incident 가 아예 생기지 않는다.**
    _APN_KINDS = ("apn_pdn_failure", "change_suspected")

    def _bind_scenario_targets(self) -> None:
        """프로파일에 의존하는 시나리오 대상(도메인·기종·APN)을 실제 값으로 채운다."""
        for sc in self.scenarios:
            if sc.kind in self._DOMAIN_KINDS and not sc.target.domain:
                doms = self.profiles.get("http", SeriesProfile()).domains
                # 상위 트래픽 도메인이어야 도메인 차원 탐지의 분모가 확보된다
                sc.target.domain = doms[self.rng.randrange(min(4, len(doms)))] if doms else "cdn.example.com"
            if sc.kind == "ue_model" and sc.target.vendor_model is None:
                cnt: dict[tuple[str, str], int] = {}
                for s in self.subs:
                    if s.vendor and s.model:
                        cnt[(s.vendor, s.model)] = cnt.get((s.vendor, s.model), 0) + 1
                if cnt:
                    ranked = sorted(cnt.items(), key=lambda kv: -kv[1])
                    sc.target.vendor_model = ranked[min(2, len(ranked) - 1)][0]
            if sc.kind in self._APN_KINDS and sc.target.apn is None:
                sc.target.apn = self._top_apn()

    def _top_apn(self) -> str | None:
        """트래픽 상위 3위 안의 APN 하나.

        **프로파일이 아니라 `apn_pool` 을 본다**(있으면). 상용 경로는
        `pemdb1._coerce` 가 `Distributions.apn()` 으로 APN 을 덮으므로
        프로파일 분포와 레코드의 분포가 다르다 — 프로파일에서 고르면 그 APN 이
        레코드에 **한 건도 없을 수 있고**, 그러면 주입이 성립하지 않는다.
        """
        pool = getattr(self, "apn_pool", None)
        if pool:
            return pool[self.rng.randrange(min(3, len(pool)))]
        apns = self.profiles.get("lte_call", SeriesProfile()).extras.get("apn_top")
        if apns:
            return apns[self.rng.randrange(min(3, len(apns)))]
        return None

    # -- volume ----------------------------------------------------------

    def _volume(self, series: str, ts: datetime) -> int:
        base = self.rates.get(series, 0)
        if not base:
            return 0
        factor = diurnal_factor(ts.hour, series) if self.diurnal else 1.0
        factor *= 1.0 + self.rng.uniform(-0.06, 0.06)
        return max(0, int(base * factor))

    def _traffic_scale(self, series: str, ts: datetime) -> float:
        """**세션당** 트래픽 크기 배율.

        세션 모델(2026-10-14) 이후 이 곡선은 곱해지는 자리가 셋 중 하나다 —
        호 수(`lte_call`) × 호당 세션 수(`uplane_sessions`) × 여기. 셋의 곱이
        총 트래픽 4배를 만든다(`DIURNAL_WEIGHT` 주석). 전에는 개수(`_volume`)와
        여기 둘뿐이라 제곱이었다.

        **C-Plane 에는 적용하지 않는다.** U-Plane 전용이고, `lte_call` 의
        측정값은 지속시간·메시지 수라 바이트가 아니다.
        """
        if not self.diurnal:
            return 1.0
        # 분 단위 흔들림은 개수 쪽에만 준다. 크기까지 흔들면 같은 분의 처리율이
        # 이유 없이 출렁여 변화점 탐지(D2)가 그것을 집는다.
        return diurnal_factor(ts.hour, series)

    # -- record builders -------------------------------------------------

    def _sub_for(self, cell_idx: int) -> Subscriber:
        """그 셀에 사는 가입자 하나. 없으면 전체에서 뽑는다.

        **꼬리는 `주변 셀`이다 — 전국이 아니다**(2026-10-27 사람 결정).
        전부 home 으로 묶으면 셀마다 가입자 집합이 완전히 갈려 "옆 셀에서도
        통신한다"가 사라진다. 그래서 28% 는 다른 사람을 뽑는데, 그 사람도
        **이 셀 주변에 사는 사람**이어야 한다.

        전에는 그 자리가 전체 균일 추첨이었다. 그러면 한 가입자가 60분에
        서로 다른 셀 9개에 나타나고(home 적중률은 99% 인데도) 그중 대부분이
        **전국에 흩어진 셀**이다 — 노선을 타지 않는 사람의 이동은 그런
        모양이 아니다. 장비가 옮겨 가는 것이 아니면 **그 셀 또는 그 주변**에
        있어야 한다.

        주변은 `handover.Topology` 가 안다 — 전환 상대를 고르는 데 쓰는 그
        관계를 그대로 쓴다. 두 벌로 두면 "전환은 되는데 그 셀에 사는 사람이
        없다"가 생긴다.

        **`_lte_call` 이 이것을 부른다**(2026-10-27). 그 전에는 전체 균일
        추첨이었고 이 함수가 죽은 코드였다 — 근거는 호출부 주석.
        """
        rng = self.rng
        pool = self._subs_by_cell.get(cell_idx)
        if pool and rng.random() < 0.72:
            return self.subs[rng.choice(pool)]
        near = self._near_subs(cell_idx)
        if near:
            return self.subs[near[rng.randrange(len(near))]]
        # **주변에 없으면 그 셀 사람으로 되돌린다.** 꼬리를 뽑으려다 전국에서
        # 뽑으면 지역성이 거기서 깨진다 — 실측에서 그 경로가 home 에서
        # 90km 떨어진 셀까지 만들었다.
        if pool:
            return self.subs[rng.choice(pool)]
        # **그 셀에 아무도 살지 않는다.** 셀 수가 가입자 수에 비해 많으면
        # 일어난다(실측 348셀 중 1개). 전에는 여기서 전체 추첨이었고, 그
        # 한 셀 때문에 **제주 가입자가 서울 셀에 나타났다**(471km · 관측의
        # 2.94%). 가장 가까운 주민으로 바꾼다 — 반경을 넘더라도 그것이
        # 가능한 선택 중 가장 지역적이다.
        nearest = self._nearest_subs(cell_idx)
        return self.subs[nearest[rng.randrange(len(nearest))]]

    def _near_subs(self, cell_idx: int) -> list[int]:
        """그 셀 **주변**에 사는 가입자 인덱스. 한 번 구해 캐시한다.

        매 호마다 토폴로지를 되짚으면 분당 수천 번이다. 셀 수만큼만 캐시하면
        되므로 메모리는 문제가 아니다.
        """
        got = self._near_cache.get(cell_idx)
        if got is not None:
            return got
        here = self.cells[cell_idx]
        out: list[int] = []
        for i in self._cells_around(cell_idx):
            if _dist_m(here, self.cells[i]) <= self.home_radius_m:
                out.extend(self._subs_by_cell.get(i, ()))
        self._near_cache[cell_idx] = out
        return out

    def _nearest_subs(self, cell_idx: int) -> list[int]:
        """**가장 가까운** 주민이 있는 셀의 가입자들. 반경을 넘어도 돌려준다.

        그 셀에 아무도 살지 않을 때의 마지막 수단이다. 반경 안에 없으면
        고리를 넓혀 가며 찾으므로 **빈 목록을 돌려주지 않는다** — 돌려주면
        호출부가 전체 추첨으로 떨어지고 거기서 지역성이 깨진다.
        """
        got = self._far_cache.get(cell_idx)
        if got is not None:
            return got
        here = self.cells[cell_idx]
        best: list[int] = []
        r = self.home_radius_m
        while not best and r < 4e7:            # 지구 둘레면 더 넓힐 데가 없다
            cand = [(_dist_m(here, self.cells[i]), i)
                    for i in self._cells_around(cell_idx, r)
                    if self._subs_by_cell.get(i)]
            if cand:
                best = list(self._subs_by_cell[min(cand)[1]])
            r *= 4
        self._far_cache[cell_idx] = best or list(range(len(self.subs)))
        return self._far_cache[cell_idx]

    def _cells_around(self, cell_idx: int, radius: float | None = None):
        """반경 안의 셀 인덱스 — 격자 버킷으로 후보를 줄인다.

        **전수 비교를 피한다.** 운영 셀 마스터는 수십만 행이고(`enb_limit`
        으로 줄이지만) 셀마다 전수를 돌면 기동이 멈춘다. 격자는 한 번만
        만든다.
        """
        if self._grid is None:
            self._grid = {}
            for i, c in enumerate(self.cells):
                self._grid.setdefault(
                    (int(c.latitude / _GRID_DEG),
                     int(c.longitude / _GRID_DEG)), []).append(i)
        here = self.cells[cell_idx]
        rad = self.home_radius_m if radius is None else radius
        span = max(1, int(rad / (_GRID_DEG * 111_320.0)) + 1)
        gy = int(here.latitude / _GRID_DEG)
        gx = int(here.longitude / _GRID_DEG)
        for dy in range(-span, span + 1):
            for dx in range(-span, span + 1):
                for i in self._grid.get((gy + dy, gx + dx), ()):
                    if i != cell_idx:
                        yield i

    def _common_user(self, rec: Rec, sub: Subscriber) -> None:
        for name, value in zip(USER_FIELDS, user_values(sub)):
            rec.set(name, value)

    def _lte_call(self, ts: datetime, slot_start: float) -> tuple[Rec, Cell]:
        smp = self.samplers["lte_call"]
        rng = self.rng
        # **이동 가입자는 순서가 뒤집힌다.** 평소에는 셀을 가중 추첨하고
        # 가입자를 따로 뽑는데, 움직이는 사람은 "지금 어디 있는지"가 먼저다.
        # 그러지 않으면 궤적이 호에 반영되지 않아 **경로를 깔아도 전환이
        # 추첨으로 돌아간다.**
        got = self.pick_mobile() if rng.random() < self.mobility_share else None
        if got is not None:
            sub, cell = got
        else:
            cell = self._pick_cell()
            # **기본은 전체 균일 추첨이다 — 샘플 XDR 이 그렇게 말한다.**
            #
            # 정확히는 **말하지 않는다.** 샘플 분포(`_distributions.json`)에
            # 있는 축은 call_type · flags · release_cause · first_error ·
            # durations · apn · vendor_model 이고, `profile.json` 의
            # `lte_call` 범주형 51개 중 셀·가입자를 잇는 것은 없다
            # (`First_eNB_PLMN`·`imsi_mcc_mnc_info` 는 PLMN 접두어다).
            # **가입자↔셀 관계는 샘플이 규정하지 않는 축**이고, 그러면
            # 기본값은 모델을 넣지 않는 쪽이어야 한다.
            #
            # `home_locality` 를 켜면 `home_cell` 근처에서 뽑는다. 그건
            # 사람이 더하는 것이다 — 지하철 노선(`mobility.journeys`)과 같은
            # 층이고, 켜면 셀·분 `SUBS` 가 **-22%** 움직여(8.7 → 6.8)
            # 기존 기준선과 전후 비교가 끊긴다.
            if self.home_locality:
                sub = self._sub_for(self.cell_index[cell.cell_key])
            else:
                sub = self.subs[rng.randrange(len(self.subs))]
            # **고른 사람이 이동 중이면 셀을 그 사람 위치로 옮긴다.**
            # `mobility_share` 는 "이동 가입자를 얼마나 자주 고르나"일 뿐이고,
            # 한 번 고른 뒤에 임의 셀에 두면 **그 사람이 경로를 벗어난다** —
            # 실측으로 이동 가입자 925명 중 863명이 그렇게 경로 밖 셀에
            # 나타났다. 궤적이 깨지면 경로를 깐 의미가 없다.
            at = self.cell_of(sub)
            if at is not None:
                cell = at

        values = self.templates["lte_call"].copy()
        rec = Rec(values, self.index["lte_call"], "lte_call")
        t = slot_start + rng.random() * 60
        rec.set("SummaryCreateTime", f"{t + 0.05:.6f}")

        ongoing = "2" if self.end_only else smp.categorical("OngoingFlag", "2")
        rec.set("OngoingFlag", ongoing)
        self._common_user(rec, sub)

        rec.set("PGW_ID", cell.pgw_id)
        rec.set("ims_PGW_ID", cell.ims_pgw_id)
        rec.set("SGW_ID", cell.sgw_id)
        rec.set("MME_ID", cell.mme_id)
        rec.set("First_eNB_ID", cell.enb_id)
        rec.set("First_Cell_ID", cell.cell_id)
        rec.set("First_eNB_VLAN_ID", cell.vlan_id)
        rec.set("Last_eNB_ID", cell.enb_id)
        rec.set("Last_Cell_ID", cell.cell_id)
        rec.set("Last_eNB_VLAN_ID", cell.vlan_id)
        rec.set("First_eNB_PLMN", cell.plmn)
        rec.set("Last_eNB_PLMN", cell.plmn)
        rec.set("interval_First_eNB_PLMN", cell.plmn)
        rec.set("interval_First_eNB_ID", cell.enb_id)
        rec.set("interval_First_Cell_ID", cell.cell_id)
        rec.set("interval_First_eNB_C_UID", cell.c_uid)
        rec.set("interval_First_TAC", cell.tac)
        rec.set("interval_First_eNB_VLAN_ID", cell.vlan_id)

        call_type = int(smp.categorical("call_type", "3") or 3)
        rec.set("call_type", call_type)
        rec.set("APN", smp.categorical("APN", ""))
        rec.set("PDN_Type", smp.categorical("PDN_Type", "2"))
        rec.set("ims_PDN_Type", smp.categorical("ims_PDN_Type", "0"))
        rec.set("CNDomain", smp.categorical("CNDomain", "-1"))
        rec.set(
            "InitialUEMessage_RRC_Establishment_Cause",
            smp.categorical("InitialUEMessage_RRC_Establishment_Cause", "4"),
        )
        rec.set("ue_dcnr", sub.dcnr)
        rec.set("equip_nw", smp.categorical("equip_nw", ""))
        rec.set("nas_key_validity", smp.categorical("nas_key_validity", "1"))

        dur = min(smp.numeric("call_duration_time", 0.0), 6e8)
        start = t - dur / 1e6
        rec.set("call_start_time", f"{start:.6f}")
        rec.set("call_end_time", f"{t:.6f}")
        rec.set("call_duration_time", int(dur))
        rec.set("InitialUEMessage_Time", f"{start:.6f}")
        rec.set("interval_call_start_time", f"{start:.6f}")

        if ongoing == "2":
            self._apply_flags(rec, smp, call_type)
            rec.set("initial_access_duration", int(smp.numeric("initial_access_duration")))
            rec.set("initial_core_duration", int(smp.numeric("initial_core_duration")))
            rec.set("initial_paging_duration", int(smp.numeric("initial_paging_duration")))
            rec.set("initial_access_msg_count", int(smp.numeric("initial_access_msg_count")))
            rec.set("initial_core_msg_count", int(smp.numeric("initial_core_msg_count")))
            rec.set("initial_paging_msg_count", int(smp.numeric("initial_paging_msg_count")))
            rec.set(
                "initial_paging_attempt_count",
                int(smp.numeric("initial_paging_attempt_count")),
            )
            rel = smp.categorical("UEContextRelease_Cause", "0")
            rec.set("UEContextRelease_Cause", rel)
            if rel != "0":
                rec.set("UEContextRelease_Time", f"{t:.6f}")
            if rec.get("drop_flag") == "1" and rel in ("0", "120", "300"):
                # 절단 호인데 정상 Cause가 붙으면 D-02~D-05 분류가 무의미해진다
                rec.set("UEContextRelease_Cause", self.rng.choice([121, 125, 200, 500]))
                rec.set("UEContextRelease_Time", f"{t:.6f}")
            self._maybe_baseline_error(rec, smp, start)
            if sub.dcnr == "1" and self.rng.random() < 0.965:
                rec.set("initial_nr_conn_time", f"{start + 0.35:.6f}")
        return rec, cell

    def _apply_flags(self, rec: Rec, smp: Sampler, call_type: int) -> None:
        """Call Type 조건부 Flag 결합분포에서 뽑는다 (§3.2.5).

        주변분포로 각 플래그를 따로 뽑으면 "Detach인데 소통 성공" 같은 조합이
        생겨 Call Type × Flag 교차 KPI가 무너진다.
        """
        if self.success_only:
            # 성공 전용: 시도=1·성공=1·절단=0. 부가 절차 플래그는 Call Type 에
            # 해당하는 것만 성공으로 — 시나리오가 이 깨끗한 바탕 위에 실패를 꽂는다.
            rec.set("attempt_flag", 1)
            rec.set("success_flag", 1)
            rec.set("drop_flag", 0)
            return
        extras = smp.p.extras
        names: list[str] = extras.get("flag_names", [])  # type: ignore[assignment]
        joint: dict = extras.get("flag_joint", {})  # type: ignore[assignment]
        dist = joint.get(str(call_type)) or joint.get("3") or {}
        keys = [k for k in dist if k != "__other__" and len(k) == len(names)]
        if not (names and keys):
            rec.set("attempt_flag", 1)
            rec.set("success_flag", 1)
            return
        weights = [dist[k] for k in keys]
        pattern = self.rng.choices(keys, weights=weights, k=1)[0]
        for name, ch in zip(names, pattern):
            rec.set(name, ch)
        if rec.get("attempt_flag") == "0" and rec.get("success_flag") == "0":
            # H-11(Flag 결측률) 표본 — End 레코드인데 전 플래그가 0인 경우
            if self.rng.random() < 0.85:
                rec.set("attempt_flag", 1)
                rec.set("success_flag", 1)

    def _maybe_baseline_error(self, rec: Rec, smp: Sampler, start: float) -> None:
        if self.success_only:
            return  # 성공 전용 베이스라인에는 평시 오류를 남기지 않는다
        """평시에도 존재하는 소수 오류. 정상 구간의 Cause 구성비를 유지한다.

        소통 실패 호에는 원칙적으로 First Error 가 남아야 한다. 실패인데
        ``first_error_cause = 0`` 인 비율이 H-15(First Error 미기록률)이며,
        그 값이 0이면 지표가 한 번도 검증되지 않으므로 소수는 남겨 둔다.
        """
        failed = rec.get("attempt_flag") == "1" and rec.get("success_flag") == "0"
        iface = smp.categorical("first_error_interface_protocol", "0")
        if iface == "0":
            if not (failed and self.rng.random() < 0.93):
                return
            iface = self.rng.choices(
                ["2", "3", "5", "6", "1"], weights=[4, 3, 2, 2, 1], k=1
            )[0]
        cause = smp.categorical("first_error_cause", "0")
        if cause == "0":
            if not failed:
                return
            cause = {
                "1": "3004", "2": "504", "3": "87", "5": "17", "6": "31",
            }[iface]
        rec.set("first_error_interface_protocol", iface)
        rec.set("first_error_cause", cause)
        rec.set("first_error_message", smp.categorical("s1ap_error_Message", "0"))
        rec.set("first_error_time", f"{start:.6f}")
        rec.set("last_error_interface_protocol", iface)
        rec.set("last_error_cause", cause)
        prefix = {"1": "s6a", "2": "s1ap", "3": "s11", "4": "s10", "5": "emm", "6": "esm"}.get(iface)
        if prefix:
            rec.set(f"{prefix}_error_Cause", cause)
        if self.rng.random() < 0.55:
            rec.set("success_flag", 0)

    def uplane_rec(self, series: str, ts: datetime, tf: float,
                   cell: Cell, t: float, user: tuple[str, ...],
                   apn: str | None, ongoing: str, sid: str,
                   domain: str | None = None,
                   core: tuple[int, int, int] | None = None) -> Rec:
        """U-Plane 레코드 하나 — **식별자·셀·시각을 호출부가 준다.**

        구 `_user_traffic` 의 본문이고, 달라진 것은 셀·가입자·시각을 스스로
        고르지 않는다는 점뿐이다. 세션 모델에서 그 셋은 **성공 호에서
        상속**되어야 하고(`session.Call`), 여기서 다시 뽑으면 인과가 끊어진다.

        * ``tf`` — 트래픽 **크기** 배율(시간대 곡선). 한 번 구해 이 레코드의
          모든 양(量) 필드에 같은 값을 쓴다 — 필드마다 다시 구하면 패킷 수와
          바이트가 서로 다른 배율을 받아 **평균 패킷 크기가 시간대마다
          달라진다.**
        * ``apn`` — ``None`` 이면 프로파일에서 뽑는다. 세션 모델은 호의 APN 을
          그대로 넘긴다(한 베어러의 APN 이 계열마다 다를 수 없다).
        * ``domain`` — ``None`` 이면 계열 프로파일에서 뽑는다. 세션 모델은
          HTTP 에만 넘긴다. **계열마다 도메인 풀이 다르다**(실측 교집합
          dns∩http 18/400) — 한 풀로 통일하면 다른 계열의 도메인 축 구성비가
          바뀐다. DNS→HTTP 인과는 이름이 아니라 **세션 단위 게이트**로
          성립한다(`SessionEngine._open`).
        """
        smp = self.samplers[series]
        rng = self.rng
        values = self.templates[series].copy()
        rec = Rec(values, self.index[series], series)
        rec.set("SummaryCreateTime", f"{t:.6f}")
        rec.set("OngoingFlag", ongoing)
        rec.set("SessionID", sid)
        rec.set("SessionCreatedTime", f"{t:.6f}")
        for name, value in zip(USER_FIELDS, user):
            rec.set(name, value)
        # 코어 장비는 **세션이 들고 다닌다.** 셀 마스터 값을 그대로 쓰면
        # 핸드오버로 셀이 바뀔 때마다 MME·SGW 가 함께 바뀐다 — X2 핸드오버는
        # 코어 재배치가 없으므로 그건 틀린 모양이다(`handover` 머리말의 표).
        # 재배치가 있는 종류(`S1`·`S1_MME`)에서만 호출부가 이 값을 갈아 준다.
        mme, sgw, pgw = ((cell.mme_id, cell.sgw_id, cell.pgw_id)
                         if core is None else core)
        rec.set("SGW_ID", sgw)
        rec.set("PGW_ID", pgw)
        rec.set("MME_ID", mme)
        rec.set("eNB_ID", cell.enb_id)
        rec.set("Cell_ID", cell.cell_id)
        rec.set("APN", smp.categorical("APN", "") if apn is None else apn)
        rec.set("SessionQCI", smp.categorical("SessionQCI", "6"))
        rec.set("App_Group_Code", smp.categorical("App_Group_Code", "Other"))
        rec.set("App_ID", smp.categorical("App_ID", ""))
        rec.set("Protocol", smp.categorical("Protocol", "6"))
        rec.set("Tethering", smp.categorical("Tethering", "0"))
        rec.set("SynMssSize", smp.categorical("SynMssSize", "1372"))
        rec.set("SynAckMssSize", smp.categorical("SynAckMssSize", "1380"))

        for f in (
            "UplinkRTTCount", "UplinkRTTTime", "DnlinkRTTCount", "DnlinkRTTTime",
        ):
            rec.set(f, int(smp.numeric(f)))
        for direction in ("Uplink", "Dnlink"):
            # **`PayloadSize` 와 `ThroughputTime` 은 같은 분위수에서 뽑는다**
            # (2026-10-28). 처리율 `U-15` 가 그 둘의 비(比)이므로 독립으로
            # 뽑으면 레코드마다 처리율이 **무작위**가 된다. 실측에서 셀·분
            # `U-15` 의 84.2% 가 `0.01 Mbps` 미만이고 최대가 228 Mbps,
            # 셀별 변동계수가 **평균 3.51** 이었다 — 그 분산 위에서는 D1
            # 절대 임계도 `EV-08` 의 30% 하락 판정도 서지 않고, 그래서
            # `j-sc-16`(앵커 고착)의 주 신호가 통째로 막혔다(확정대장 §9.8.9).
            #
            # **주변분포는 그대로다** — 각 필드는 여전히 자기 실측 백분위에서
            # 뽑힌다. 생기는 것은 둘 사이의 **상관**뿐이고, 그것이 "큰 전송은
            # 시간도 길다" 는 물리에 맞는다.
            #
            # **나머지 셋은 묶지 않는다.** `RetransPacketCount` 를 묶으면
            # 재전송률이 상수에 가까워져 `U-12` 탐지가 죽는다. 패킷 수 두 개는
            # `U-15` 와 무관하고, 묶으면 평균 패킷 크기가 고정된다.
            u_thr = rng.random() * 10
            for f in (
                "PacketCount", "PayloadPacketCount", "PayloadSize",
                "ThroughputTime", "RetransPacketCount",
            ):
                key = f"Traffic_Total_{direction}_{f}"
                # **`ThroughputTime` 도 같이 곱한다.** 처리율은
                # `PayloadSize / ThroughputTime` 이라(U-15) 크기만 키우면
                # **피크 시간대의 처리율이 배율만큼 올라간다** — 혼잡한 19시에
                # 망이 가장 빠른 것으로 보이고, 실제와 반대다. 둘을 함께 늘리면
                # 비율은 그대로고 **총량만** 시간대를 따른다 — 그것이 "사용량이
                # 늘어난다"의 뜻이다. 혼잡에 따른 품질 저하는 장애 주입이 한다.
                v = int(smp.numeric(
                    key, u=u_thr if f in ("PayloadSize", "ThroughputTime")
                    else None) * tf)
                rec.set(key, v)
                # **`Delta` 도 같은 값으로 둔다.** 이 레코드는 그 자체로 한
                # 세션(`ONGOINGFLAG=20`)이므로 "세션 누적"과 "이 분의 증분"이
                # 같다. 분을 넘기는 세션은 `session.SessionEngine._emit` 이
                # 둘을 갈라 다시 쓴다. 비워 두면 생성량 통계
                # (`pemdb1.series_stat`)가 dns·http 바이트를 0 으로 센다.
                rec.set(f"Traffic_Delta_{direction}_{f}", v)
            rec.set(f"Traffic_Total_{direction}_FirstTime", f"{t - 3.0:.6f}")
            rec.set(f"Traffic_Total_{direction}_LastTime", f"{t:.6f}")
            rec.set(f"Traffic_Delta_{direction}_FirstTime", f"{t - 3.0:.6f}")
            rec.set(f"Traffic_Delta_{direction}_LastTime", f"{t:.6f}")
        rec.set("ICMP_Delta_Uplink_IcmpPacketCount", smp.categorical("ICMP_Delta_Uplink_IcmpPacketCount", "0"))
        rec.set("ICMP_Delta_Dnlink_IcmpPacketCount", smp.categorical("ICMP_Delta_Dnlink_IcmpPacketCount", "0"))

        if self.success_only:
            # 성공 전용은 U-Plane 에도 걸린다. 실측 분포의 재전송 꼬리값이 저표본
            # 셀에 몰리면 U-12(재전송률)가 임계를 넘어 **없는 장애가 뜬다** —
            # 실제로 백필 30분에서 단일 셀 Critical 2건이 나왔다 (2026-10-12).
            # 정상 수준(페이로드의 0.5%)으로 눌러 둔다. transport_loss 시나리오의
            # 뮤테이터는 이 뒤에 돌므로 장애 주입은 그대로 성립한다.
            for d in ("Uplink", "Dnlink"):
                payload = int(rec.num(f"Traffic_Total_{d}_PayloadPacketCount"))
                cap = max(0, int(payload * 0.005))
                if int(rec.num(f"Traffic_Total_{d}_RetransPacketCount")) > cap:
                    rec.set(f"Traffic_Total_{d}_RetransPacketCount", cap)
                    rec.set(f"Traffic_Delta_{d}_RetransPacketCount", cap)

        if series == "transport":
            rec.set("SynCount", smp.categorical("SynCount", "1"))
            rec.set("SynAckCount", smp.categorical("SynAckCount", "1"))
            rec.set("RstCount", smp.categorical("RstCount", "0"))
            rec.set("FinCount", smp.categorical("FinCount", "1"))
            rec.set("UplinkWindowCloseCount", smp.categorical("UplinkWindowCloseCount", "0"))
            rec.set("DnlinkWindowCloseCount", smp.categorical("DnlinkWindowCloseCount", "0"))
            rec.set("SynFirstTime", f"{t - 4.0:.6f}")
            if rec.get("SynCount") != "0" and rng.random() < 0.985:
                rec.set("SynAckFirstTime", f"{t - 3.94:.6f}")
                rec.set("AckTime", f"{t - 3.90:.6f}")
            rec.set("ClientHelloDomain", "." + smp.domain())
            rec.set("LastDnsQueryName", "." + smp.domain())
            rec.set("LastDnsRequestTime", f"{t - 20:.6f}")
        elif series == "dns":
            rec.set("Protocol", "17")
            rec.set("QueryType", smp.categorical("QueryType", "1"))
            rec.set("QueryName", "." + smp.domain())
            rec.set("RequestFirstTime", f"{t - 0.02:.6f}")
            rec.set("RequestLastTime", f"{t - 0.02:.6f}")
            reply = "0" if self.success_only else smp.categorical("ReplyCode", "0")
            rec.set("ReplyCode", reply)
            # ReplyCode 와 AnswerCount 를 따로 뽑으면 "성공인데 답이 없는" 질의가
            # 생겨 U-29(DNS 성공률)의 베이스라인이 무너진다. 정상 응답은 답을 갖는다.
            answers = smp.categorical("AnswerCount", "1")
            if reply == "0" and answers == "0":
                answers = "1"
            elif reply != "0":
                answers = "0"
            # ── 빈 값까지 본다 (2026-10-22) ────────────────────────────────
            #
            # 실 샘플에는 `AnswerCount` 가 **빈 문자열**인 행이 0.385% 있고,
            # 그것은 "모름"이라 실패로 읽지 않는다(`session._dns_failed` 머리말).
            # **원본에서는 그 행의 `ReplyCode` 도 함께 비어 있었다** — 짝이다.
            #
            # `success_only` 가 `ReplyCode` 만 `"0"` 으로 누르면서 그 짝이
            # 깨져 **표본에 없던 조합**("rcode 0 · 답 모름")이 만들어졌다.
            # `DNS_OK` 는 `REPLY_CODE=0 AND ANSWER_COUNT>0` 이므로 그 행이
            # 성공으로 세어지지 않아 **평시 `U-29` 가 99.6% 에서 멈춘다.**
            # 위 두 줄이 막으려던 바로 그 모순이고, 리터럴 `"0"` 만 보고
            # 빈 값을 놓쳤다.
            #
            # **성공 전용 모드에서만 채운다.** 실측 재현 모드는 빈 값을 그대로
            # 둔다 — 그쪽에서는 `ReplyCode` 도 비어 있어 짝이 유지되고,
            # "모름"이 데이터에 남아야 H-계열 자기감시가 검증된다.
            if self.success_only and not answers.strip().isdigit():
                answers = "1"
            rec.set("AnswerCount", answers)
            rec.set("ResponseTime", f"{t:.6f}")
            rec.set("TimeToLive", smp.categorical("TimeToLive", "60"))
            # ── 재시도 카운터도 `success_only` 를 따른다 (2026-10-22) ──────
            #
            # **실 샘플의 `QueryRetryCount` 는 99.3% 가 `'1'` 이다.** 그대로
            # 뽑으면 평시 `U-33`(DNS 재시도율)이 **99.6%** 가 되고 —
            #
            #   ① 그 지표가 **장애를 가리킬 수 없다.** 평시와 장애가 같은
            #      값이면 화면·증거팩에서 신호가 아니다(불변규칙 17 의 취지).
            #   ② "rcode 0 · 답 있음 · 그런데 1회 재시도"는 **레코드 내부
            #      모순**이다. 바로 위에서 `ReplyCode` 를 성공으로 누르고
            #      `AnswerCount` 를 맞춰 두는 것과 같은 자리인데 재시도만
            #      빠져 있었다.
            #   ③ SC-05·SC-20 의 통과 조건 `DNS_RETRY/DNS_Q ≥ 0.3` 이 평시에
            #      이미 충족되어 **주입 검증이 성립하지 않는다.**
            #
            # 실패를 만드는 것이 아니라 **평시를 평시로 두는 것**이다. 실측
            # 재현 모드(`success_only=False`)는 분포를 그대로 쓴다.
            if self.success_only:
                rec.set("QueryRetryCount", 0)
                rec.set("dns_retry_sequence", 0)
                rec.set("dns_retry_interval", "0.0")
            else:
                rec.set("QueryRetryCount", smp.categorical("QueryRetryCount", "0"))
                rec.set("dns_retry_sequence", smp.categorical("dns_retry_sequence", "0"))
                rec.set("dns_retry_interval", round(smp.numeric("dns_retry_interval"), 3))
            rec.set("ResolvedIP", f"{rng.randrange(1, 224)}.{rng.randrange(256)}."
                                  f"{rng.randrange(256)}.{rng.randrange(1, 255)}")
        else:  # http
            host = smp.domain() if domain is None else domain
            rec.set("Host", host)
            rec.set("Host_Site", host.split(".")[-2] if host.count(".") >= 1 else host)
            rec.set("Host_Prefix", host.split(".")[0])
            rec.set("Method", smp.categorical("Method", "GET"))
            status = smp.categorical("StatusCode", "200")
            if self.success_only and status.startswith("5"):
                status = "200"   # 5xx 는 EXTERNAL_SERVER 시나리오가 만들 몫이다
            rec.set("StatusCode", status)
            rec.set("RequestTime", f"{t - 0.18:.6f}")
            rec.set("ResponseTime", f"{t:.6f}")
            # 본문 크기도 시간대를 따른다 — 같은 `tf` 를 쓴다(위 주석 참조).
            rec.set("HTTP_Total_Response_PayloadSize", int(smp.numeric("HTTP_Total_Response_PayloadSize") * tf))
            rec.set("HTTP_Total_Request_ContentLength", int(smp.numeric("HTTP_Total_Request_ContentLength") * tf))
            rec.set("HTTP_Total_Response_ContentLength", int(smp.numeric("HTTP_Total_Response_ContentLength") * tf))
        return rec

    def _user_traffic(self, series: str, ts: datetime,
                      slot_start: float) -> tuple[Rec, Cell]:
        """**구 경로** — 호와 무관하게 셀·가입자·시각을 스스로 뽑는 U-Plane
        레코드 하나.

        세션 모델(2026-10-14) 이후 상용 경로(`pemdb1.PemdbGen`)는 이 함수를
        쓰지 않는다. 남겨 둔 것은 `generate_minute`(.dat 파일 생성, 구 창고
        경로)이 아직 부르기 때문이고, 그 경로는 `cli.cmd_backfill` 이 막혀
        있어 실제로는 돌지 않는다. **여기로 만든 U-Plane 에는 C-Plane 과
        인과가 없다** — 새 코드가 이 함수를 부르면 그 인과가 다시 끊어진다.
        """
        cell = self._pick_cell()
        sub = self.subs[self.rng.randrange(len(self.subs))]
        rec = self.uplane_rec(
            series, ts, self._traffic_scale(series, ts), cell,
            slot_start + self.rng.random() * 60, user_values(sub), None,
            self.samplers[series].categorical("OngoingFlag", "20"), "")
        return rec, cell

    # -- per-minute generation -------------------------------------------

    def base_lte_call(self, ts: datetime, slot_start: float) -> tuple[Rec, Cell]:
        """시나리오 변형 **전**의 LTE-CALL 레코드 하나.

        상용 스키마 어댑터(`pemdb1.PemdbGen`)가 생성과 변형 사이에 끼어들어
        실측 분포로 값을 덮어쓴다. 변형 뒤에 덮으면 주입한 장애가 지워지므로
        그 중간 단계를 공개한다. `generate_minute` 경로는 그대로다.
        """
        return self._lte_call(ts, slot_start)

    def generate_minute(self, minute: int, ts: datetime) -> dict[str, list[str]]:
        """**구 경로** — 계열별 1분 .dat 파일용 레코드.

        U-Plane 을 `_user_traffic` 으로 만들므로 **C-Plane 과 인과가 없다**
        (세션 모델 이전 동작). 상용 경로는 `pemdb1.PemdbGen` 이고 그쪽이
        `session.SessionEngine` 을 쓴다. 이 함수를 부르는 것은
        `engine.DataGen` 하나이고 그 경로는 `cli.cmd_backfill` 이 막아 두었다.
        """
        slot_start = ts.timestamp()
        active = [sc for sc in self.scenarios if sc.covers(minute)]
        for sc in active:
            sc.active_cells_cache = sc.active_cells(minute)  # type: ignore[attr-defined]

        out: dict[str, list[str]] = {}
        for series in ("lte_call", "transport", "dns", "http"):
            if series not in self.profiles:
                continue
            n = self._volume(series, ts)
            lines: list[str] = []
            builder = self._lte_call if series == "lte_call" else None
            for _ in range(n):
                if builder is not None:
                    rec, cell = builder(ts, slot_start)
                else:
                    rec, cell = self._user_traffic(series, ts, slot_start)
                for sc in active:
                    if series not in sc.series:
                        continue
                    if sc.target.cell_keys and cell.cell_key not in sc.active_cells_cache:
                        continue
                    if sc.kind not in MUTATORS:
                        continue        # PLAN/SUPPRESS 전용 kind — 구 경로엔 없다
                    sc._minute = minute
                    MUTATORS[sc.kind](rec, sc, sc.intensity_for(series, minute),
                                      self.rng, cell)
                lines.append(SEP.join(rec.values))
            out[series] = lines
        return out


def write_minute(
    xdr_root: Path,
    ts: datetime,
    payload: dict[str, list[str]],
    done_marker: bool = True,
) -> list[Path]:
    """1분 파일 기록 — ``.tmp`` 로 쓰고 rename, 이어서 ``.done`` 마커 (§2.3.1)."""
    written: list[Path] = []
    day = ts.strftime("%Y%m%d")
    stamp = ts.strftime("%Y%m%d_%H%M")
    for series, lines in payload.items():
        d = xdr_root / SERIES_DIR[series] / day
        d.mkdir(parents=True, exist_ok=True)
        final = d / f"{SERIES_FILE_PREFIX[series]}_R1_{stamp}.dat"
        tmp = final.with_suffix(".dat.tmp")
        tmp.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        tmp.replace(final)
        if done_marker:
            final.with_suffix(".dat.done").write_text(
                f"{len(lines)}\n", encoding="utf-8"
            )
        written.append(final)
    return written


def minute_slots(start: datetime, minutes: int):
    for i in range(minutes):
        yield i, start + timedelta(minutes=i)
