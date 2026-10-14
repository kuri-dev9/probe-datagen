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
        self.samplers = {
            s: Sampler(p, random.Random(seed + i)) for i, (s, p) in enumerate(profiles.items())
        }
        self.templates = {s: _template(s) for s in SERIES_FIELDS}
        self.index = {s: _index(s) for s in SERIES_FIELDS}

        self.cell_weights = [c.traffic_weight for c in cells]
        self._cum = self._cumulative(self.cell_weights)
        self.cell_by_key = {c.cell_key: c for c in cells}

        pool = subscriber_pool or max(2000, len(cells) * 60)
        self.subs = self._make_subscribers(pool)
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
            mcc_mnc = smp.categorical("imsi_mcc_mnc_info", "4500503") if smp else "4500503"
            if not (len(mcc_mnc) == 7 and mcc_mnc.isdigit()):
                # 프로파일에 `""`(1.2%)와 `"__other__"`(0.6%)가 있다. 그대로 두면
                # IMSI 를 만들 수 없고, 빈 값을 쓰면 `imsi_mcc_mnc_info` 와
                # IMSI 앞 7자가 어긋난다 — 규격이 선언한 관계가 깨진다.
                # 대표값으로 채우고 **두 컬럼을 함께** 그 값으로 쓴다.
                mcc_mnc = "4500503"
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

    def _bind_scenario_targets(self) -> None:
        """프로파일에 의존하는 시나리오 대상(도메인·기종)을 실제 값으로 채운다."""
        for sc in self.scenarios:
            if sc.kind == "external_server" and not sc.target.domain:
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
        pool = self._subs_by_cell.get(cell_idx)
        if pool and self.rng.random() < 0.72:
            return self.subs[self.rng.choice(pool)]
        return self.subs[self.rng.randrange(len(self.subs))]

    def _common_user(self, rec: Rec, sub: Subscriber) -> None:
        for name, value in zip(USER_FIELDS, user_values(sub)):
            rec.set(name, value)

    def _lte_call(self, ts: datetime, slot_start: float) -> tuple[Rec, Cell]:
        smp = self.samplers["lte_call"]
        rng = self.rng
        cell = self._pick_cell()
        sub = self.subs[rng.randrange(len(self.subs))]

        values = self.templates["lte_call"].copy()
        rec = Rec(values, self.index["lte_call"])
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
                   domain: str | None = None) -> Rec:
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
        rec = Rec(values, self.index[series])
        rec.set("SummaryCreateTime", f"{t:.6f}")
        rec.set("OngoingFlag", ongoing)
        rec.set("SessionID", sid)
        rec.set("SessionCreatedTime", f"{t:.6f}")
        for name, value in zip(USER_FIELDS, user):
            rec.set(name, value)
        rec.set("SGW_ID", cell.sgw_id)
        rec.set("PGW_ID", cell.pgw_id)
        rec.set("MME_ID", cell.mme_id)
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
                v = int(smp.numeric(key) * tf)
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
            rec.set("AnswerCount", answers)
            rec.set("ResponseTime", f"{t:.6f}")
            rec.set("TimeToLive", smp.categorical("TimeToLive", "60"))
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
                    MUTATORS[sc.kind](rec, sc, sc.intensity(minute), self.rng, cell)
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
