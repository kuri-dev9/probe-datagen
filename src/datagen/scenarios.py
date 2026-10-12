"""장애 시나리오 13종 — 대표 8 + 확장 5.

각 시나리오는 **정답(ground truth)** 을 들고 있다. 어떤 Cause Taxonomy 코드가
정답인지, 어떤 지리 분포(EV-11)를 만들어야 하는지, 어떤 룰(R-*)이 발동해야 하는지를
선언해두면 RCA 결과를 자동 채점할 수 있다. §0.4의 "Top-1 70% / Top-3 90%"는 이런
정답 없이는 측정 자체가 불가능하다.

시나리오는 레코드 단위 변형(mutation)으로 구현된다. 집계 결과를 직접 조작하지
않고 XDR 레코드를 바꾸므로, 적재→집계→탐지→RCA 전 경로가 실제와 동일하게
동작하는지 검증된다.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Callable

from .cells import Cell

# --------------------------------------------------------------------------
# 변형 대상 레코드 핸들
# --------------------------------------------------------------------------


class Rec:
    """생성 중인 XDR 레코드 (필드명 → 인덱스 매핑 위의 얇은 래퍼)."""

    __slots__ = ("values", "index")

    def __init__(self, values: list[str], index: dict[str, int]):
        self.values = values
        self.index = index

    def get(self, name: str, default: str = "") -> str:
        i = self.index.get(name)
        if i is None:
            i = self.index.get(name.replace(" ", "_"))
        return self.values[i] if i is not None else default

    def set(self, name: str, value) -> None:
        # 통합 규격의 일부 필드명에는 공백이 있다(`Last_Cell ID`). 필드 인덱스는
        # 정규화명(`Last_Cell_ID`)이라, 원본 헤더명으로 들어온 set 이 조용히
        # 무시되지 않도록 공백을 흡수한다. 원본 .dat→read_csv 경로가 하던 일이다.
        i = self.index.get(name)
        if i is None:
            i = self.index.get(name.replace(" ", "_"))
        if i is not None:
            self.values[i] = str(value)

    def num(self, name: str, default: float = 0.0) -> float:
        try:
            return float(self.get(name, "") or default)
        except ValueError:
            return default

    def scale(self, name: str, factor: float) -> None:
        self.set(name, int(self.num(name) * factor))


@dataclass
class Target:
    """시나리오가 영향을 주는 범위."""

    cell_keys: set[str] = field(default_factory=set)
    sgg_codes: set[str] = field(default_factory=set)
    sido_codes: set[str] = field(default_factory=set)
    sgw_id: int | None = None
    pgw_id: int | None = None
    mme_id: int | None = None
    s6a_equip_id: int | None = None
    vendor_model: tuple[str, str] | None = None
    domain: str | None = None
    apn: str | None = None
    cell_pairs: list[tuple[str, str]] = field(default_factory=list)

    def label(self) -> str:
        bits = []
        if self.sgw_id:
            bits.append(f"SGW-{self.sgw_id}")
        if self.pgw_id:
            bits.append(f"PGW-{self.pgw_id}")
        if self.mme_id:
            bits.append(f"MME-{self.mme_id}")
        if self.s6a_equip_id:
            bits.append(f"S6a-{self.s6a_equip_id}")
        if self.vendor_model:
            bits.append("/".join(self.vendor_model))
        if self.domain:
            bits.append(self.domain)
        if self.cell_keys:
            bits.append(f"셀 {len(self.cell_keys)}개")
        return " · ".join(bits) or "전국"


@dataclass
class Scenario:
    """주입 시나리오 1건 = Incident 1건의 정답."""

    id: str
    kind: str
    title: str
    #: 정답 Cause Taxonomy 코드 (§6.5)
    truth_code: str
    truth_category: str
    #: 발동해야 하는 룰 (§6.2)
    expect_rules: tuple[str, ...]
    #: 기대 지리 분포 패턴 (EV-11)
    expect_geo: str
    start_minute: int
    duration: int
    target: Target
    #: 변화 양상 — STEP(설정·작업 변경 의심) / RAMP(부하 누적·자원 소진)
    shape: str = "STEP"
    peak: float = 1.0
    #: 시간에 따라 영향 셀이 늘어나는 확산성 장애인가 (R-18)
    spreading: bool = False
    ramp_in: int = 3
    series: tuple[str, ...] = ("lte_call",)

    def intensity(self, minute: int) -> float:
        """분 슬롯에서의 장애 강도 0~peak."""
        if not (self.start_minute <= minute < self.start_minute + self.duration):
            return 0.0
        elapsed = minute - self.start_minute
        remain = self.start_minute + self.duration - minute
        if self.shape == "RAMP":
            base = min(1.0, (elapsed + 1) / max(1, self.duration * 0.6))
        else:
            base = min(1.0, (elapsed + 1) / max(1, self.ramp_in))
        if remain <= 2:  # 회복 구간
            base *= remain / 3.0
        return max(0.0, base * self.peak)

    def covers(self, minute: int) -> bool:
        return self.intensity(minute) > 0.0

    def active_cells(self, minute: int) -> set[str]:
        """확산성 장애는 시간에 따라 영향 셀이 늘어난다."""
        if not self.spreading or not self.cell_order:
            return self.target.cell_keys
        elapsed = minute - self.start_minute
        frac = min(1.0, 0.25 + elapsed / max(1, self.duration * 0.8))
        n = max(1, int(len(self.cell_order) * frac))
        return set(self.cell_order[:n])

    cell_order: list[str] = field(default_factory=list)

    def scope_kind(self) -> str:
        """채점 시 어떤 축으로 매칭할지 — 지리 범위가 없는 시나리오가 있다."""
        if self.kind in ("dns_degradation", "external_server"):
            return "SERVICE"
        if self.kind in ("auth_failure", "ims_pdn"):
            return "GLOBAL"
        if self.kind == "ue_model":
            return "UE"
        return "CELL"

    def expect_kpi(self) -> str | None:
        """이 시나리오가 발동시켜야 할 대표 트리거 KPI (채점 시 Incident 매칭용)."""
        return {
            "dns_degradation": "U-29",
            "external_server": "U-23",
            "auth_failure": "P-01",
            "ims_pdn": "C-21",
            "nsa_nr_leg": "N-02",
            "mobility": "PSF",
            "transport_loss": "U-12",
        }.get(self.kind)

    def as_truth(self) -> dict:
        return {
            "scenario_id": self.id,
            "scope_kind": self.scope_kind(),
            "expect_kpi": self.expect_kpi(),
            "expect_node": (
                f"SGW-{self.target.sgw_id}" if self.target.sgw_id
                else f"MME-{self.target.mme_id}" if self.target.mme_id
                else None
            ),
            "cells": sorted(self.target.cell_keys),
            "title": self.title,
            "truth_code": self.truth_code,
            "truth_category": self.truth_category,
            "expect_rules": list(self.expect_rules),
            "expect_geo": self.expect_geo,
            "start_minute": self.start_minute,
            "duration": self.duration,
            "shape": self.shape,
            "spreading": self.spreading,
            "scope": self.target.label(),
            "cell_cnt": len(self.target.cell_keys),
            "series": list(self.series),
        }


# --------------------------------------------------------------------------
# 레코드 변형 — kind별 구현
# --------------------------------------------------------------------------

Mutator = Callable[[Rec, Scenario, float, random.Random, Cell], bool]
MUTATORS: dict[str, Mutator] = {}


def mutator(kind: str) -> Callable[[Mutator], Mutator]:
    def deco(fn: Mutator) -> Mutator:
        MUTATORS[kind] = fn
        return fn

    return deco


def _fail_connect(rec: Rec, interface: int, cause: int, message: int) -> None:
    """소통 실패 호로 만든다 — 접속 단계에서 무너지는 형태 (R-21)."""
    rec.set("success_flag", 0)
    rec.set("drop_flag", 0)
    rec.set("data_success_flag", 0)
    if rec.get("ims_attempt_flag") == "1":
        rec.set("ims_success_flag", 0)
    rec.set("first_error_interface_protocol", interface)
    rec.set("first_error_cause", cause)
    rec.set("first_error_message", message)
    rec.set("first_error_time", rec.get("call_start_time"))
    rec.set("last_error_interface_protocol", interface)
    rec.set("last_error_cause", cause)
    rec.set("last_error_message", message)
    rec.set("last_error_time", rec.get("call_end_time") or rec.get("call_start_time"))


@mutator("gw_resource")
def _gw_resource(rec, sc, intensity, rng, cell) -> bool:
    """SGW/PGW 세션 자원 소진 — S11 Cause 73 (No resources available)."""
    if sc.target.sgw_id is not None and int(rec.get("SGW_ID", "0") or 0) != sc.target.sgw_id:
        return False
    if rng.random() > intensity * 0.82:
        return False
    _fail_connect(rec, interface=3, cause=73, message=33)
    rec.set("s11_error_Cause", 73)
    rec.set("s11_error_Message", 33)
    rec.set("s11_error_Time", rec.get("call_start_time"))
    rec.set("esm_error_Cause", 26)
    rec.set("esm_error_Message", 195)
    rec.scale("initial_core_duration", 1.0 + 1.6 * intensity)
    return True


@mutator("gw_node_down")
def _gw_node_down(rec, sc, intensity, rng, cell) -> bool:
    """노드 무응답 — First Error에 TIMEOUT(900)이 포함되므로 F-08로 바로 잡힌다."""
    if sc.target.sgw_id is not None and int(rec.get("SGW_ID", "0") or 0) != sc.target.sgw_id:
        return False
    if rng.random() > intensity * 0.9:
        return False
    _fail_connect(rec, interface=3, cause=900, message=32)
    rec.set("s11_error_Cause", 900)
    rec.set("s11_error_Message", 32)
    if rng.random() < 0.18 * intensity:
        # PGW 재기동 통보 — GW_NODE_DOWN의 확정적 증거 (D.9.4)
        rec.set("s11_error_Message", 179)
        rec.set("s11_error_Cause", 72)
        rec.set("first_error_cause", 72)
        rec.set("first_error_message", 179)
    rec.scale("initial_core_duration", 1.0 + 3.0 * intensity)
    return True


@mutator("ran_radio")
def _ran_radio(rec, sc, intensity, rng, cell) -> bool:
    """무선 구간 열화 — S1AP 1xx 대역. 접속 실패와 절단이 함께 는다."""
    if cell.cell_key not in sc.active_cells_cache:
        return False
    if rng.random() > intensity * 0.6:
        return False
    if rng.random() < 0.45:
        _fail_connect(rec, interface=2, cause=rng.choice([125, 126]), message=9)
        rec.set("s1ap_error_Cause", rec.get("first_error_cause"))
        rec.set("s1ap_error_Message", 9)
    else:
        # 접속은 됐으나 유지가 안 되는 형태 (R-20)
        rec.set("success_flag", 1)
        rec.set("drop_flag", 1)
        rec.set("UEContextRelease_Cause", 121)  # Radio Connection With UE Lost
        rec.set("UEContextRelease_Time", rec.get("call_end_time"))
        rec.set("UEContextReleaseRequest_Cause", 121)
        rec.set("last_error_interface_protocol", 2)
        rec.set("last_error_cause", 121)
    rec.scale("initial_access_duration", 1.0 + 1.4 * intensity)
    return True


@mutator("mme_overload")
def _mme_overload(rec, sc, intensity, rng, cell) -> bool:
    """MME 과부하 — OverloadStart(34)와 Release Cause 500이 직접 증거."""
    if sc.target.mme_id is not None and int(rec.get("MME_ID", "0") or 0) != sc.target.mme_id:
        return False
    if cell.cell_key not in sc.active_cells_cache:
        return False
    rec.scale("initial_access_duration", 1.0 + 2.2 * intensity)
    rec.scale("initial_core_duration", 1.0 + 1.3 * intensity)
    if rng.random() > intensity * 0.5:
        return False
    if rng.random() < 0.5:
        _fail_connect(rec, interface=2, cause=500, message=34)
        rec.set("s1ap_error_Cause", 500)
        rec.set("s1ap_error_Message", 34)
    else:
        rec.set("success_flag", 1)
        rec.set("drop_flag", 1)
        rec.set("UEContextRelease_Cause", 500)
        rec.set("UEContextRelease_Time", rec.get("call_end_time"))
    return True


@mutator("auth_failure")
def _auth_failure(rec, sc, intensity, rng, cell) -> bool:
    """S6a 인증 경로 이상 — HSS/DRA 과부하(3004)와 무응답(TIMEOUT)이 섞인다.

    ``auth_attempt_flag`` 가 선 호에만 걸면 표본이 너무 얇아 탐지가 성립하지 않는다.
    실제로는 인증이 관여하지 않는 호도 S6a 경로 장애의 영향을 받으므로, 대상 호에
    인증 절차를 표시하고 실패시킨다.
    """
    if rng.random() > intensity * 0.4:
        return False
    rec.set("auth_attempt_flag", 1)
    rec.set("auth_success_flag", 0)
    cause = rng.choices([3004, 3002, 900, 5012], weights=[0.45, 0.2, 0.25, 0.1], k=1)[0]
    rec.set("AuthenticationInformation_Cause", cause)
    rec.set("AuthenticationInformation_Time", rec.get("call_start_time"))
    # S6a 구간 Cause는 TIMEOUT을 제외한다 (D.2) — 900은 절차 필드로만 남는다
    if cause != 900:
        rec.set("s6a_error_Cause", cause)
        rec.set("s6a_error_Message", 318)
    _fail_connect(rec, interface=1, cause=cause, message=318)
    rec.set("emm_error_Cause", 17)
    return True


@mutator("dns_degradation")
def _dns_degradation(rec, sc, intensity, rng, cell) -> bool:
    """DNS 열화 — 망 지표는 정상인데 체감 장애가 나는 가장 흔한 숨은 원인."""
    if rng.random() > intensity * 0.7:
        return False
    if rng.random() < 0.55:
        rec.set("ReplyCode", 2)  # SERVFAIL
        rec.set("AnswerCount", 0)
        rec.set("ResolvedIP", "")
    else:
        rec.set("ResponseTime", "0.0")  # 무응답
        rec.set("AnswerCount", 0)
    rec.set("QueryRetryCount", rng.randint(1, 3))
    rec.set("dns_retry_sequence", rng.randint(1, 3))
    rec.set("dns_retry_interval", round(rng.uniform(1.0, 5.0), 3))
    return True


@mutator("external_server")
def _external_server(rec, sc, intensity, rng, cell) -> bool:
    """외부 서버·CDN 이슈 — up 축만 열화하고 down 축은 정상이다 (R-09b/R-14b)."""
    host = rec.get("Host") or rec.get("ClientHelloDomain")
    if sc.target.domain and sc.target.domain not in host:
        return False
    if rng.random() > intensity * 0.85:
        return False
    rec.set("StatusCode", rng.choices([500, 502, 503, 504], weights=[3, 2, 3, 2], k=1)[0])
    rec.scale("UplinkRTTTime", 1.0 + 3.5 * intensity)
    rec.set("HTTP_Total_Response_PayloadSize", 0)
    return True


@mutator("transport_loss")
def _transport_loss(rec, sc, intensity, rng, cell) -> bool:
    """전송 구간 손실 — down 축 재전송·aRTT가 오르고 up 축은 정상이다 (R-09)."""
    if cell.cell_key not in sc.active_cells_cache:
        return False
    if rng.random() > intensity * 0.8:
        return False
    down_pkt = max(1.0, rec.num("Traffic_Total_Dnlink_PayloadPacketCount"))
    rec.set(
        "Traffic_Total_Dnlink_RetransPacketCount",
        int(down_pkt * rng.uniform(0.06, 0.22) * intensity) + 1,
    )
    rec.scale("DnlinkRTTTime", 1.0 + 2.8 * intensity)
    rec.scale("Traffic_Total_Dnlink_ThroughputTime", 1.0 + 1.9 * intensity)
    return True


@mutator("ue_model")
def _ue_model(rec, sc, intensity, rng, cell) -> bool:
    """특정 단말 기종 이슈 — 지리적으로 흩어지고 기종에만 집중된다 (R-10)."""
    vm = sc.target.vendor_model
    if vm and (rec.get("Vendor"), rec.get("Model")) != vm:
        return False
    if rng.random() > intensity * 0.7:
        return False
    _fail_connect(rec, interface=6, cause=30, message=195)
    rec.set("esm_error_Cause", 30)
    rec.set("esm_error_Message", 195)
    rec.set("data_success_flag", 0)
    return True


@mutator("mobility")
def _mobility(rec, sc, intensity, rng, cell) -> bool:
    """이동성(핸드오버) 이슈 — 셀 쌍에 PathSwitchFailure가 집중된다 (R-17)."""
    if cell.cell_key not in sc.active_cells_cache:
        return False
    if rng.random() > intensity * 0.5:
        return False
    rec.set("PathSwitch_count", rng.randint(1, 3))
    rec.set("PathSwitchFailure_count", rng.randint(1, 2))
    if sc.target.cell_pairs and rng.random() < 0.8:
        src, dst = rng.choice(sc.target.cell_pairs)
        enb, _, cid = dst.rpartition("-")
        rec.set("Last_eNB_ID", enb)
        rec.set("Last_Cell ID", cid)
    if rng.random() < 0.45:
        rec.set("success_flag", 1)
        rec.set("drop_flag", 1)
        rec.set("UEContextRelease_Cause", 106)  # Handover Failure In Target EPC/eNB
        rec.set("last_error_interface_protocol", 2)
        rec.set("last_error_cause", 106)
    return True


@mutator("change_suspected")
def _change_suspected(rec, sc, intensity, rng, cell) -> bool:
    """계단형 변화 — 원인 Cause가 한 곳에 몰리지 않아 변경 의심으로만 남는다 (R-11)."""
    if cell.cell_key not in sc.active_cells_cache:
        return False
    if rng.random() > intensity * 0.45:
        return False
    # 어느 한 Cause 도 지배하지 않는 혼합. 구체 원인이 특정되지 않은 채 계단형으로
    # 무너지는 것이 "변경 의심"의 실제 관측 양상이다.
    interface, cause, msg = rng.choices(
        [(5, 17, 68), (2, 504, 9), (6, 31, 195), (3, 89, 33), (2, 403, 9)],
        weights=[1, 1, 1, 1, 1],
        k=1,
    )[0]
    _fail_connect(rec, interface, cause, msg)
    prefix = {1: "s6a", 2: "s1ap", 3: "s11", 4: "s10", 5: "emm", 6: "esm"}.get(interface)
    if prefix:
        rec.set(f"{prefix}_error_Cause", cause)
        rec.set(f"{prefix}_error_Message", msg)
    return True


@mutator("ims_pdn")
def _ims_pdn(rec, sc, intensity, rng, cell) -> bool:
    """VoLTE 전용 이슈 — C-21만 악화하고 C-20(Data)은 정상이다 (R-30)."""
    if rec.get("ims_attempt_flag") != "1":
        return False
    if sc.target.pgw_id is not None and int(rec.get("ims_PGW_ID", "0") or 0) != sc.target.pgw_id:
        return False
    if rng.random() > intensity * 0.8:
        return False
    rec.set("ims_success_flag", 0)
    rec.set("esm_error_Cause", 33)  # Requested service option not subscribed
    rec.set("esm_error_Message", 209)
    rec.set("first_error_interface_protocol", 6)
    rec.set("first_error_cause", 33)
    rec.set("first_error_message", 209)
    rec.set("first_error_time", rec.get("call_start_time"))
    rec.set("last_error_interface_protocol", 6)
    rec.set("last_error_cause", 33)
    return True


@mutator("nsa_nr_leg")
def _nsa_nr_leg(rec, sc, intensity, rng, cell) -> bool:
    """NR 레그 연결 실패 — 5G 단말인데 LTE로만 동작해 "5G인데 느리다"가 된다."""
    if rec.get("ue_dcnr") != "1":
        return False
    if cell.cell_key not in sc.active_cells_cache:
        return False
    # NR 레그 실패는 커버리지 문제이므로 해당 셀의 DCNR 단말 대부분에 나타난다
    if rng.random() > intensity * 0.95:
        return False
    rec.set("initial_nr_conn_time", "0.0")
    if rng.random() < 0.15:
        rec.set("mme_restrict_dcnr", 1)
    return True


# --------------------------------------------------------------------------
# 시나리오 구성
# --------------------------------------------------------------------------


def _cells_in_one_sgg(cells: list[Cell], rng: random.Random, want: int = 30) -> tuple[list[Cell], str]:
    """한 시군구 안에서 셀을 고른다 — 지리적으로 뭉치는(Cluster) 열화용."""
    by_sgg: dict[str, list[Cell]] = {}
    for c in cells:
        if c.geo_status == "MAPPED":
            by_sgg.setdefault(c.adm_sgg_cd, []).append(c)
    candidates = [k for k, v in by_sgg.items() if len(v) >= max(6, want // 3)]
    if not candidates:
        candidates = list(by_sgg)
    code = rng.choice(candidates)
    group = by_sgg[code]
    return rng.sample(group, min(want, len(group))), code


def _cells_scattered(cells: list[Cell], rng: random.Random, want: int = 40) -> list[Cell]:
    mapped = [c for c in cells if c.geo_status == "MAPPED"]
    return rng.sample(mapped, min(want, len(mapped)))


def build_scenarios(
    cells: list[Cell],
    minutes: int,
    rng: random.Random,
    selected: str | list[str] = "all",
    lead_minutes: int | None = None,
) -> list[Scenario]:
    """13종 시나리오를 시뮬레이션 구간에 배치한다.

    구간이 짧으면 앞쪽부터 넣되 서로 겹치지 않게 간격을 둔다. 겹치면 Incident
    상관·압축(§5.5)이 두 원인을 하나로 묶어 채점이 흐려진다.
    """
    if not cells:
        return []
    by_sgw: dict[int, list[Cell]] = {}
    for c in cells:
        by_sgw.setdefault(c.sgw_id, []).append(c)
    busy_sgw = max(by_sgw, key=lambda k: len(by_sgw[k]))
    by_mme: dict[int, list[Cell]] = {}
    for c in cells:
        by_mme.setdefault(c.mme_id, []).append(c)
    busy_mme = max(by_mme, key=lambda k: len(by_mme[k]))

    # 한 시군구 안의 셀을 "전부" 가져간다. 일부만 고르면 같은 시군구의 정상 셀이
    # 지리 롤업에서 열화를 희석해 SGG 단위 탐지가 성립하지 않는다.
    cluster_cells, cluster_sgg = _cells_in_one_sgg(cells, rng, 60)
    loss_cells, loss_sgg = _cells_in_one_sgg(cells, rng, 40)
    change_cells, change_sgg = _cells_in_one_sgg(cells, rng, 45)
    nsa_cells, _ = _cells_in_one_sgg(cells, rng, 60)
    overload_cells = [c for c in by_mme[busy_mme]][:80]

    # 이동성 이슈는 인접한 셀 쌍 — GPS 근접 셀을 후보 인접셀로 본다 (EV-13)
    from .geo_lite import haversine_km

    anchor = rng.choice([c for c in cells if c.geo_status == "MAPPED"])
    near = sorted(
        (c for c in cells if c.geo_status == "MAPPED" and c.cell_key != anchor.cell_key),
        key=lambda c: haversine_km(anchor.latitude, anchor.longitude, c.latitude, c.longitude),
    )[:18]
    pairs = [(anchor.cell_key, c.cell_key) for c in near[:8]]

    vendor_models = sorted({(c.enb_id[:2], "A688") for c in cells[:1]})
    del vendor_models  # 실제 기종은 프로파일에서 뽑아 generator가 채운다

    specs: list[tuple[str, dict]] = [
        ("SC-01", dict(
            kind="gw_resource", title="SGW 세션 자원 소진",
            truth_code="GW_RESOURCE_EXHAUSTION", truth_category="CORE_UP",
            expect_rules=("R-05", "R-02", "R-15", "R-21"), expect_geo="SCATTER",
            shape="RAMP", peak=1.0,
            target=Target(cell_keys={c.cell_key for c in by_sgw[busy_sgw]}, sgw_id=busy_sgw),
        )),
        ("SC-02", dict(
            kind="ran_radio", title="특정 지역 무선 구간 열화",
            truth_code="RAN_RADIO_QUALITY", truth_category="RAN",
            expect_rules=("R-06", "R-22", "R-16", "R-20"), expect_geo="CLUSTER",
            shape="STEP", peak=1.0,
            target=Target(cell_keys={c.cell_key for c in cluster_cells}, sgg_codes={cluster_sgg}),
        )),
        ("SC-03", dict(
            kind="gw_node_down", title="SGW 노드 무응답",
            # 규격의 R-04("F-08이 S11에 집중 + SGW/PGW 편중")가 지목하는 코드는
            # GW_NODE_DOWN 이다. NF_UNREACHABLE 은 구간이 특정되지 않은 무응답에 쓴다.
            truth_code="GW_NODE_DOWN", truth_category="CORE_UP",
            expect_rules=("R-00b", "R-04", "R-05b"), expect_geo="SCATTER",
            shape="STEP", peak=1.0,
            target=Target(cell_keys={c.cell_key for c in by_sgw[busy_sgw]}, sgw_id=busy_sgw),
        )),
        ("SC-04", dict(
            kind="auth_failure", title="S6a 인증 경로 이상",
            truth_code="AUTH_FAILURE", truth_category="CORE_CP",
            expect_rules=("R-01", "R-21"), expect_geo="SCATTER",
            shape="STEP", peak=1.0,
            target=Target(),
        )),
        ("SC-05", dict(
            kind="dns_degradation", title="DNS 응답 열화",
            truth_code="DNS_DEGRADATION", truth_category="EXTERNAL",
            expect_rules=("R-14",), expect_geo="WIDE",
            shape="RAMP", peak=1.0, series=("dns",),
            target=Target(),
        )),
        ("SC-06", dict(
            kind="external_server", title="외부 서비스 5xx 급증",
            truth_code="EXTERNAL_SERVER", truth_category="EXTERNAL",
            expect_rules=("R-14b", "R-09b"), expect_geo="WIDE",
            shape="STEP", peak=1.0, series=("http",),
            target=Target(domain=""),   # generator가 프로파일 도메인으로 채운다
        )),
        ("SC-07", dict(
            kind="transport_loss", title="국지 전송 구간 손실",
            truth_code="TRANSPORT_LOSS", truth_category="TRANSPORT",
            expect_rules=("R-09", "R-16"), expect_geo="CLUSTER",
            shape="STEP", peak=1.0, series=("transport",),
            target=Target(cell_keys={c.cell_key for c in loss_cells}, sgg_codes={loss_sgg}),
        )),
        ("SC-08", dict(
            kind="ue_model", title="특정 단말 기종 접속 실패",
            truth_code="UE_MODEL_ISSUE", truth_category="UE",
            expect_rules=("R-10",), expect_geo="SCATTER",
            shape="STEP", peak=1.0,
            target=Target(vendor_model=None),  # generator가 프로파일 기종으로 채운다
        )),
        # ---- 확장 5종 ----
        ("SC-09", dict(
            kind="mobility", title="핸드오버 실패 (셀 쌍 집중)",
            truth_code="RAN_MOBILITY_ISSUE", truth_category="RAN",
            expect_rules=("R-17", "R-20"), expect_geo="CORRIDOR",
            shape="STEP", peak=1.0,
            target=Target(
                cell_keys={anchor.cell_key} | {c.cell_key for c in near[:14]},
                cell_pairs=pairs,
            ),
        )),
        ("SC-10", dict(
            kind="mme_overload", title="MME 과부하 (확산 중)",
            truth_code="MME_OVERLOAD", truth_category="CORE_CP",
            expect_rules=("R-23", "R-18"), expect_geo="SCATTER",
            shape="RAMP", peak=1.0, spreading=True,
            target=Target(cell_keys={c.cell_key for c in overload_cells}, mme_id=busy_mme),
        )),
        ("SC-11", dict(
            kind="change_suspected", title="계단형 급락 (변경 의심)",
            truth_code="CHANGE_SUSPECTED", truth_category="CHANGE",
            expect_rules=("R-11",), expect_geo="CLUSTER",
            shape="STEP", peak=1.0, ramp_in=1,
            target=Target(cell_keys={c.cell_key for c in change_cells}, sgg_codes={change_sgg}),
        )),
        ("SC-12", dict(
            kind="ims_pdn", title="VoLTE(ims PDN) 전용 접속 실패",
            truth_code="IMS_PDN_ISSUE", truth_category="IMS",
            expect_rules=("R-30",), expect_geo="SCATTER",
            shape="STEP", peak=1.0,
            target=Target(),
        )),
        ("SC-13", dict(
            kind="nsa_nr_leg", title="5G NSA NR 레그 연결 실패",
            truth_code="NSA_NR_LEG_FAILURE", truth_category="NSA",
            expect_rules=("R-33",), expect_geo="CLUSTER",
            shape="RAMP", peak=1.0,
            target=Target(cell_keys={c.cell_key for c in nsa_cells}),
        )),
    ]

    if selected in ("none", []):
        return []
    wanted = None
    if isinstance(selected, list):
        wanted = set(selected)
    elif selected != "all":
        wanted = {s.strip() for s in selected.split(",")}
    if wanted:
        specs = [s for s in specs if s[0] in wanted]
    if not specs:
        return []

    # 겹치지 않게 배치 — 앞뒤 여유를 두어 "직전 정상 구간" 비교가 가능하게 한다.
    # 지속시간을 먼저 확보한다: 5~6분짜리 이상은 최소 표본 규칙과 연속 N분 조건에
    # 걸려 탐지 자체가 되지 않으므로, 간격을 줄이더라도 길이를 지킨다.
    # 첫 장애까지의 여유. 라이브 시연은 이걸 짧게 잡아 빨리 보여주되, 0으로는
    # 두지 않는다 — 평시 구간이 없으면 베이스라인도 대조군도 없다.
    lead = lead_minutes if lead_minutes is not None else max(10, minutes // 15)
    lead = max(3, min(lead, max(3, minutes // 3)))
    gap = 4
    span = max(8, (minutes - lead * 2) // max(1, len(specs)))
    dur = max(8, min(28, span - gap))

    out: list[Scenario] = []
    for i, (sid, spec) in enumerate(specs):
        start = lead + i * span
        if start + dur >= minutes:
            break
        sc = Scenario(id=sid, start_minute=start, duration=dur, **spec)
        sc.cell_order = sorted(sc.target.cell_keys)
        rng.shuffle(sc.cell_order)
        out.append(sc)
    return out


# Scenario는 분 슬롯마다 활성 셀 집합을 캐시해 두고 mutator가 그걸 본다.
Scenario.active_cells_cache = set()  # type: ignore[attr-defined]
