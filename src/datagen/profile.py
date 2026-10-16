"""제공된 실 샘플 XDR에서 분포를 학습해 시뮬레이터의 프로파일을 만든다.

순수 파라미터 합성이 아니라 **실데이터에서 뽑은 분포**를 쓰는 이유는, 시뮬레이터로
검증한 KPI 산식·룰이 실환경에서도 같은 형태의 입력을 만나도록 하기 위해서다.
Cause 코드 구성비, Call Type 비중, Flag 결측 양상, RTT·재전송 분포처럼 산식의
분모·분자를 결정하는 값들은 추정으로 지어내면 검증 가치가 사라진다.

학습 결과는 두 종류다.

* **범주형** — 값별 확률 (상위 N개 + 나머지 ``__other__``)
* **수치형** — 십분위 스케치. 역변환 표본추출로 원 분포의 모양을 되살린다.

여기에 ``call_type`` 조건부 Flag 결합분포를 따로 학습한다. KPI Flag는 Call Type에
종속된 상태값이므로(§3.2.5) 주변분포만으로 생성하면 "Detach인데 소통 성공" 같은
불가능한 조합이 만들어진다.
"""

from __future__ import annotations

import gzip
import json
import math
import random
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator

from .fields import SERIES_FIELDS

SEP = "\x1e"

#: 계열별로 학습할 범주형 필드
CATEGORICAL: dict[str, tuple[str, ...]] = {
    "lte_call": (
        "OngoingFlag", "call_type", "old_call_type", "APN", "ims_APN",
        "imsi_mcc_mnc_info", "Vendor", "Model", "PayCode",
        "First_eNB_PLMN", "PDN_Type", "ims_PDN_Type", "CNDomain",
        "InitialUEMessage_RRC_Establishment_Cause", "ue_dcnr", "equip_nw",
        "ue_usage_type", "mme_restrict_dcnr", "nas_key_validity",
        "s6a_error_Cause", "s13_error_Cause", "s1ap_error_Cause",
        "emm_error_Cause", "esm_error_Cause", "s11_error_Cause",
        "s10_error_Cause", "s3_error_Cause",
        "s1ap_error_Message", "emm_error_Message", "esm_error_Message",
        "s11_error_Message", "s6a_error_Message",
        "UEContextRelease_Cause", "UEContextReleaseRequest_Cause",
        "AuthenticationInformation_Cause", "UpdateLocation_Cause",
        "MEIdentityCheck_Cause", "old_call_s1ap_release_cause",
        "first_error_interface_protocol", "first_error_cause", "first_error_message",
        "last_error_interface_protocol", "last_error_cause",
        "PathSwitch_count", "PathSwitchFailure_count", "ModifyBearer_success_count",
        "Gender", "S6a_Authentication_Equip_Type", "S6a_Location_Equip_Type",
        "S13_Equip_Type",
    ),
    "transport": (
        "OngoingFlag", "App_Group_Code", "App_ID", "Protocol", "IP_Version",
        "APN", "SessionQCI", "Tethering", "App_Port", "SynCount", "SynAckCount",
        "RstCount", "FinCount", "SynDirection", "DualDnsRstFlag",
        "UplinkWindowCloseCount", "DnlinkWindowCloseCount",
        "SynMssSize", "SynAckMssSize", "imsi_mcc_mnc_info", "Vendor", "Model",
        "ICMP_Delta_Uplink_IcmpPacketCount", "ICMP_Delta_Dnlink_IcmpPacketCount",
    ),
    "dns": (
        "OngoingFlag", "App_Group_Code", "Protocol", "QueryType", "ReplyCode",
        "AnswerCount", "QueryRetryCount", "dns_retry_sequence", "TimeToLive",
        "APN", "imsi_mcc_mnc_info", "Vendor", "Model",
    ),
    "http": (
        "OngoingFlag", "App_Group_Code", "Method", "StatusCode", "Host_Site",
        "HTTP_Total_Request_ContentType", "APN", "SessionQCI",
        "imsi_mcc_mnc_info", "Vendor", "Model",
    ),
}

#: 계열별로 학습할 수치형 필드 (십분위 스케치)
NUMERIC: dict[str, tuple[str, ...]] = {
    "lte_call": (
        "call_duration_time", "initial_access_duration", "initial_core_duration",
        "initial_paging_duration", "initial_access_msg_count",
        "initial_core_msg_count", "initial_paging_msg_count",
        "initial_paging_attempt_count",
    ),
    "transport": (
        "UplinkRTTCount", "UplinkRTTTime", "DnlinkRTTCount", "DnlinkRTTTime",
        "Traffic_Total_Uplink_PacketCount", "Traffic_Total_Uplink_PayloadPacketCount",
        "Traffic_Total_Uplink_PayloadSize", "Traffic_Total_Uplink_ThroughputTime",
        "Traffic_Total_Uplink_RetransPacketCount",
        "Traffic_Total_Dnlink_PacketCount", "Traffic_Total_Dnlink_PayloadPacketCount",
        "Traffic_Total_Dnlink_PayloadSize", "Traffic_Total_Dnlink_ThroughputTime",
        "Traffic_Total_Dnlink_RetransPacketCount",
    ),
    "dns": (
        "Traffic_Total_Uplink_PayloadSize", "Traffic_Total_Dnlink_PayloadSize",
        "dns_retry_interval",
    ),
    "http": (
        "HTTP_Total_Response_PayloadSize", "HTTP_Total_Request_ContentLength",
        "HTTP_Total_Response_ContentLength",
        "Traffic_Total_Dnlink_PayloadSize", "Traffic_Total_Dnlink_ThroughputTime",
        "DnlinkRTTCount", "DnlinkRTTTime", "UplinkRTTCount", "UplinkRTTTime",
    ),
}

#: 도메인 표본 — 화면의 `Host_Site`·`QueryName` 차원을 실제 값으로 채운다.
DOMAIN_FIELDS: dict[str, str] = {
    "transport": "ClientHelloDomain",
    "dns": "QueryName",
    "http": "Host",
}

TOP_N = 40
MAX_DOMAINS = 400


def iter_records(path: Path, fields: list[str], limit: int | None = None) -> Iterator[dict[str, str]]:
    """``\\x1e`` 구분 .dat 레코드를 dict로 흘려보낸다."""
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8", errors="replace") as fd:  # type: ignore[operator]
        for i, line in enumerate(fd):
            if limit is not None and i >= limit:
                break
            parts = line.rstrip("\n").split(SEP)
            if len(parts) < len(fields):
                parts += [""] * (len(fields) - len(parts))
            yield dict(zip(fields, parts))


def _as_float(text: str) -> float | None:
    if not text or text in ("-1", "-1.0"):
        return None
    try:
        v = float(text)
    except ValueError:
        return None
    return v if math.isfinite(v) else None


@dataclass
class SeriesProfile:
    records: int = 0
    categorical: dict[str, dict[str, float]] = field(default_factory=dict)
    numeric: dict[str, list[float]] = field(default_factory=dict)
    domains: list[str] = field(default_factory=list)
    extras: dict[str, object] = field(default_factory=dict)


def _distribution(counter: Counter, top_n: int = TOP_N) -> dict[str, float]:
    total = sum(counter.values())
    if not total:
        return {}
    top = counter.most_common(top_n)
    dist = {str(k): v / total for k, v in top}
    rest = total - sum(v for _, v in top)
    if rest > 0:
        dist["__other__"] = rest / total
    return dist


def _deciles(values: list[float]) -> list[float]:
    if not values:
        return []
    values.sort()
    n = len(values)
    return [values[min(n - 1, int(round(q / 10 * (n - 1))))] for q in range(11)]


def learn_series(path: Path, series: str, limit: int | None = None) -> SeriesProfile:
    fields = SERIES_FIELDS[series]
    prof = SeriesProfile()
    cats: dict[str, Counter] = {f: Counter() for f in CATEGORICAL.get(series, ())}
    nums: dict[str, list[float]] = {f: [] for f in NUMERIC.get(series, ())}
    domains: Counter = Counter()
    domain_field = DOMAIN_FIELDS.get(series)

    flag_names = (
        "attempt_flag", "success_flag", "drop_flag", "data_attempt_flag",
        "data_success_flag", "ims_attempt_flag", "ims_success_flag",
        "paging_attempt_flag", "paging_success_flag", "detach_flag", "npr_flag",
        "auth_attempt_flag", "auth_success_flag", "location_attempt_flag",
        "location_success_flag", "mecheck_attempt_flag", "mecheck_success_flag",
    )
    flag_joint: dict[str, Counter] = defaultdict(Counter)
    end_by_call_type: Counter = Counter()

    for rec in iter_records(path, fields, limit):
        prof.records += 1
        for f, counter in cats.items():
            counter[rec.get(f, "")] += 1
        for f, bucket in nums.items():
            v = _as_float(rec.get(f, ""))
            if v is not None:
                bucket.append(v)
        if domain_field:
            d = (rec.get(domain_field) or "").strip().lstrip(".")
            if d and "." in d:
                domains[d.lower()] += 1
        if series == "lte_call":
            # 호 카운트는 End 레코드만 계상하므로(§3.1.3) Flag 결합분포도 End에서만
            # 학습한다. Start/Interim을 섞으면 전건 all-zero가 분포를 지배해
            # 시뮬레이션 KPI의 분모가 사라진다.
            if rec.get("OngoingFlag") == "2":
                ct = rec.get("call_type", "0")
                end_by_call_type[ct] += 1
                key = "".join(rec.get(f, "0") or "0" for f in flag_names)
                flag_joint[ct][key] += 1

    prof.categorical = {f: _distribution(c) for f, c in cats.items()}
    prof.numeric = {f: _deciles(v) for f, v in nums.items()}
    prof.domains = [d for d, _ in domains.most_common(MAX_DOMAINS)]

    if series == "lte_call":
        prof.extras["flag_names"] = list(flag_names)
        prof.extras["flag_joint"] = {
            ct: _distribution(c, top_n=24) for ct, c in flag_joint.items()
        }
        prof.extras["end_by_call_type"] = _distribution(end_by_call_type)
    return prof


def discover_samples(sample_dir: Path) -> dict[str, Path]:
    """``data/sample/summary/<series>/<date>/<FILE>.dat`` 구조에서 계열별 파일을 찾는다."""
    alias = {"lte-call": "lte_call", "transport": "transport", "dns": "dns", "http": "http"}
    found: dict[str, Path] = {}
    if not sample_dir.exists():
        return found
    for sub in sorted(sample_dir.iterdir()):
        if not sub.is_dir():
            continue
        series = alias.get(sub.name)
        if series is None:
            continue
        candidates = sorted(sub.rglob("*.dat"))
        if candidates:
            found[series] = max(candidates, key=lambda p: p.stat().st_size)
    return found


def learn(sample_dir: Path, limit: int | None = 60000) -> dict[str, SeriesProfile]:
    return {
        series: learn_series(path, series, limit)
        for series, path in discover_samples(sample_dir).items()
    }


def save(profiles: dict[str, SeriesProfile], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        s: {
            "records": p.records,
            "categorical": p.categorical,
            "numeric": p.numeric,
            "domains": p.domains,
            "extras": p.extras,
        }
        for s, p in profiles.items()
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def load(path: Path) -> dict[str, SeriesProfile]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {
        s: SeriesProfile(
            records=v["records"],
            categorical=v["categorical"],
            numeric=v["numeric"],
            domains=v["domains"],
            extras=v.get("extras", {}),
        )
        for s, v in raw.items()
    }


class Sampler:
    """프로파일에서 값을 뽑는다. 결정적 시드로 재현 가능하다."""

    def __init__(self, profile: SeriesProfile, rng: random.Random):
        self.p = profile
        self.rng = rng
        self._cat_cache: dict[str, tuple[list[str], list[float]]] = {}
        self._domain_w: list[float] | None = None

    def categorical(self, field_name: str, default: str = "0") -> str:
        dist = self.p.categorical.get(field_name)
        if not dist:
            return default
        if field_name not in self._cat_cache:
            keys = [k for k in dist if k != "__other__"]
            weights = [dist[k] for k in keys]
            if not keys:
                return default
            self._cat_cache[field_name] = (keys, weights)
        keys, weights = self._cat_cache[field_name]
        return self.rng.choices(keys, weights=weights, k=1)[0]

    #: 마지막 구간(p90~p100)의 꼬리 가중. **1 이면 균등(구 동작)이고 클수록
    #: p90 쪽에 몰린다.** 4 는 실측으로 정했다 — 아래 근거.
    TAIL_K = 4.0

    def numeric(self, field_name: str, default: float = 0.0) -> float:
        """백분위 11개(p0~p100)에서 하나를 뽑는다.

        ## 마지막 구간은 선형이 아니다 (2026-10-15)

        구 구현은 **전 구간을 선형 보간**했다. 그런데 백분위의 끝 두 점이
        `p90 = 28,416` · `p100 = 1,420,573,216`(실측 transport 하향
        PayloadSize)처럼 다섯 자릿수 차이가 나는 필드가 있다. 선형이면 상위
        10% 가 그 사이에 **균등하게** 깔려 그 구간 평균이 710MB 가 되고,
        전체 평균이 **71MB** 로 올라간다 — 세션 하나가 1분에 수백 MB 를 나른
        꼴이다(실측 Parquet 평균 63MB 로 확인했다).

        `p100` 은 표본 전체의 **최댓값 한 건**이다. 그것을 구간의 끝점으로
        쓰면서 균등 보간하면 **이상치 하나가 상위 10% 전부를 끌어올린다.**

        그래서 마지막 구간만 ① **기하(로그) 보간** — 값이 자릿수로 움직이는
        꼬리에 맞다 — 과 ② **꼬리 가중**(`frac ** TAIL_K`)을 쓴다. 둘을 합치면
        p90 근처가 대부분이고 GB 급은 드물게 나온다:

            지수   평균        p90       p99
              1    13.2M     31,485    482M      (로그만)
              4     3.6M     28,416     34M      ← 채택
              6     2.5M     28,416      9M

        **다른 구간은 그대로 선형이다.** p0~p90 은 값이 고르게 늘어 선형이
        맞고, 바꾸면 중앙값이 흔들린다(실측 3,385 로 전후 동일).

        0 이 끼면 기하 보간이 성립하지 않으므로 그 구간은 선형으로 둔다.
        """
        dec = self.p.numeric.get(field_name)
        if not dec or len(dec) < 2:
            return default
        u = self.rng.random() * 10
        i = min(9, int(u))
        frac = u - i
        a, b = dec[i], dec[i + 1]
        if i == 9 and a > 0 and b > a:
            return a * (b / a) ** (frac ** self.TAIL_K)
        return a + (b - a) * frac

    def domain(self) -> str:
        """Zipf 가중 도메인 선택.

        균등하게 뽑으면 수백 개 도메인에 트래픽이 고르게 퍼져 도메인 차원 분석
        (U-36·EV-08b)이 통계적으로 성립하지 않는다. 실제 트래픽은 상위 소수
        도메인에 집중되며, 프로파일의 도메인 목록은 이미 빈도 순으로 정렬되어
        있으므로 순위의 역수로 가중한다.
        """
        if not self.p.domains:
            return "example.com"
        if self._domain_w is None:
            self._domain_w = [1.0 / (i + 1) ** 0.85 for i in range(len(self.p.domains))]
        return self.rng.choices(self.p.domains, weights=self._domain_w, k=1)[0]

    def choose(self, values: Iterable[str], weights: Iterable[float]) -> str:
        vs, ws = list(values), list(weights)
        return self.rng.choices(vs, weights=ws, k=1)[0]
