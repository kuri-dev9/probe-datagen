"""장애 시나리오 22종 (하위 변형 포함 24) — 정본 명세는
graphio-probe-rca `docs/XDR-FAULT-SCENARIOS-V3.md` 다.

각 시나리오는 **정답(ground truth)** 을 들고 있다. 어떤 Cause Taxonomy 코드가
정답인지, 어떤 지리 분포(EV-11)를 만들어야 하는지, 어떤 룰(R-*)이 발동해야 하는지를
선언해두면 RCA 결과를 자동 채점할 수 있다. §0.4의 "Top-1 70% / Top-3 90%"는 이런
정답 없이는 측정 자체가 불가능하다.

시나리오는 레코드 단위 변형(mutation)으로 구현된다. 집계 결과를 직접 조작하지
않고 XDR 레코드를 바꾸므로, 적재→집계→탐지→RCA 전 경로가 실제와 동일하게
동작하는지 검증된다.

## 변형을 거는 자리가 셋이다

| 체계 | 서명 | 언제 |
| --- | --- | --- |
| `MUTATORS` | `(rec, sc, I, rng, cell) -> bool` | 레코드가 만들어진 **뒤** |
| `PLAN_MUTATORS` | `(plan, sc, I, rng, cell) -> bool` | U-Plane **세션 총량**을 정할 때 |
| `SUPPRESSORS` | `(sc, cell, I, rng) -> bool` | 레코드를 **만들기 전** |

* `PLAN_MUTATORS` 가 따로 있는 이유 — `Traffic_Total_*` 를 분 레코드에서 직접
  고치면 `Total` 의 단조성이 깨진다(V3 규칙 U-1·U-2). 세션 총량에 걸면
  `session._emit` 의 분 쪼개기를 통과하므로 역전이 **원리적으로 불가능**하다.
  `Traffic_Delta_*` 는 Parquet 에 없으므로 건드리지 않는다(V3 §4.4).
* `SUPPRESSORS` 가 따로 있는 이유 — mutator 는 **이미 만들어진** 레코드를 받는다.
  "레코드를 안 만드는 것"(SC-17 무호)은 그 서명으로 표현할 수 없다.

## 방향 규칙 (V3 §2.2 D-1)

**MME 가 UE·노드에게 거절을 통보하는 자리에는 `OUT` 메시지를, UE·노드가 MME 에게
거절을 보내는 자리에는 `IN` 메시지를 쓴다.** 그래서 같은 "베어러 활성 거절"이라도
망이 거절하면 `209 PDN_CONNECTIVITY_REJECT`(OUT)이고 단말이 거절하면
`195 ACTIVATE_DEFAULT_EPS_BEARER_CONTEXT_REJECT`(IN)다.

## TIMEOUT 규칙 (V3 §2.3 D-2 · §9.1)

`900` 은 프로브 전용 Process Specific Cause 이고 `2001`~`5xxx`·`13xxx` 는 표준
Diameter Result-Code 다. **한 호에 둘을 함께 적지 않는다.** 그리고 TIMEOUT 에는
존재하지 않는 응답 메시지를 만들지 않는다 — 요청 메시지만 적는다(S11→`32`,
S6a→`318`). `{prefix}_error_Cause` 는 S6a(1)·S13(8) 에서 **비운다**(규격 D.2 ·
graphio `codes.INTERFACE_TIMEOUT_EXCLUDED`).
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Callable

from .cells import Cell

# --------------------------------------------------------------------------
# 장애 공통 설정 — `config/datagen.yaml` 의 `pemdb1.fault`
# --------------------------------------------------------------------------


@dataclass
class FaultCfg:
    """TIMEOUT 대기시간 등 mutator 가 쓰는 설정값.

    **모듈 전역으로 둔다.** mutator 서명이 `(rec, sc, I, rng, cell)` 이라 설정을
    받을 자리가 없고, `Scenario` 는 `faults.build`(UI 주입)와
    `build_scenarios`(배치 생성) 두 경로에서 만들어지는데 둘 다 설정을 보지
    않는다. 값을 코드에 박지 않으려면(작업 관례) 여기가 유일한 자리다.
    `PemdbGen.__init__` 이 `set_fault_cfg()` 로 한 번 덮는다.

    🔴 **운영 실측이 아니다.** 프로브가 TIMEOUT 을 몇 초에 판정하는지 확인되지
    않았다(V3 §12-9·10). 값을 고치려면 yaml 한 곳이다.
    """

    #: S6a(Diameter) 무응답 판정 대기시간(초) — SC-14
    timeout_wait_s6a: float = 3.0
    #: S11(GTPv2-C) 무응답 판정 대기시간(초) — SC-03 · SC-18
    timeout_wait_s11: float = 5.0

    @classmethod
    def from_cfg(cls, cfg: dict | None) -> "FaultCfg":
        """`{"timeout_wait": {"s6a": 3.0, "s11": 5.0}}` 를 읽는다.

        `Shape.from_cfg` 와 같은 모양이다 — **모르는 키는 멈춘다.** 오타가
        "기본값으로 돌았다"로 끝나면 TIMEOUT 대기시간이 조용히 틀린 채
        `initial_core_duration` 의 하한(T-2)이 어긋난다.
        """
        f = cls()
        for k, v in (cfg or {}).items():
            if k == "timeout_wait":
                for ik, iv in (v or {}).items():
                    attr = f"timeout_wait_{ik}"
                    if not hasattr(f, attr):
                        raise RuntimeError(f"pemdb1.fault.timeout_wait 의 모르는 키: {ik}")
                    setattr(f, attr, float(iv))
                continue
            raise RuntimeError(f"pemdb1.fault 의 모르는 키: {k}")
        return f


FAULT = FaultCfg()


def set_fault_cfg(cfg: dict | None) -> FaultCfg:
    """`pemdb1.fault` 를 걸어 둔다. 호출하지 않으면 dataclass 기본값이 쓰인다."""
    global FAULT
    FAULT = FaultCfg.from_cfg(cfg)
    return FAULT


# --------------------------------------------------------------------------
# 변형 대상 레코드 핸들
# --------------------------------------------------------------------------


class Rec:
    """생성 중인 XDR 레코드 (필드명 → 인덱스 매핑 위의 얇은 래퍼)."""

    __slots__ = ("values", "index", "series")

    def __init__(self, values: list[str], index: dict[str, int], series: str = ""):
        self.values = values
        self.index = index
        #: 이 레코드의 계열. **다계열 시나리오가 갈래를 가른다** — SC-21 은
        #: transport 와 dns 를 서로 다르게 바꾸고, 한 mutator 가 둘을 받는다.
        self.series = series

    def get(self, name: str, default: str = "") -> str:
        i = self.index.get(name)
        return self.values[i] if i is not None else default

    def set(self, name: str, value) -> None:
        # **이름이 틀리면 조용히 무시된다.** 구 구현은 공백 든 원본 헤더명
        # (`Last_Cell ID`)을 정규화명으로 흡수했는데, `fields.SERIES_FIELDS`
        # 에는 공백 든 필드명이 하나도 없다(실측 0건) — 흡수가 가리는 것은
        # 오타뿐이었다. 그래서 걷어내고 호출부를 실제 필드명으로 고쳤다.
        i = self.index.get(name)
        if i is not None:
            self.values[i] = str(value)

    def num(self, name: str, default: float = 0.0) -> float:
        try:
            return float(self.get(name, "") or default)
        except ValueError:
            return default

    def scale(self, name: str, factor: float) -> None:
        self.set(name, int(self.num(name) * factor))


class Plan:
    """U-Plane **세션 총량** 핸들 — `session._plan` 이 넘긴다 (V3 규칙 U-1).

    `Rec` 과 달리 문자열 리스트가 아니라 **숫자 dict** 다. 세션 총량은 아직
    레코드가 아니고(`_emit` 이 분마다 쪼개 쓴다) 누적 역전을 막으려면 그
    단계에서 고쳐야 한다. 키는 `Traffic_Total_*` · `UplinkRTTTime` 처럼
    **실제 필드명**이다 — `fields.SERIES_FIELDS` 와 대조할 수 있어야 한다(A1).

    `const` 는 세션 수명 동안 변하지 않는 문자열 필드(도메인·APN·QCI…)이고
    **읽기 전용**이다. SC-06b 가 `ClientHelloDomain` 으로 대상을 가린다.
    """

    __slots__ = ("totals", "const", "outcome", "span")

    def __init__(self, totals: dict[str, float], const: dict[str, str],
                 outcome: str, span: int):
        self.totals = totals
        self.const = const
        #: `session.OUT_*`. **겹침 방지에 쓴다**(V3 §7.2) — `OUT_STALL` 은
        #: 이미 재전송을 8~20배로 올려 두었으므로 그 위에 장애 배율을 또
        #: 곱하면 두 번 곱해진다.
        self.outcome = outcome
        self.span = span

    def get(self, name: str, default: float = 0.0) -> float:
        return self.totals.get(name, default)

    def set(self, name: str, value: float) -> None:
        # **모르는 이름은 조용히 무시하지 않는다.** `Rec.set` 과 반대다 —
        # 여기 키는 손으로 적은 목록(TRAFFIC_PAIRS·CUM_PLAIN)에서 나오므로
        # 오타면 반드시 버그다. `Traffic_Delta_*` 를 적으면 여기서 걸린다.
        if name not in self.totals:
            raise KeyError(
                f"세션 총량에 없는 필드: {name!r} — "
                f"`session.TRAFFIC_PAIRS`/`CUM_PLAIN` 만 바꿀 수 있다"
            )
        self.totals[name] = max(0.0, float(value))   # U-3 음수 금지

    def scale(self, name: str, factor: float) -> None:
        self.set(name, self.get(name) * max(0.0, factor))

    def text(self, name: str, default: str = "") -> str:
        return self.const.get(name, default)


def clip(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else (hi if x > hi else x)


@dataclass
class Target:
    """시나리오가 영향을 주는 범위."""

    cell_keys: set[str] = field(default_factory=set)
    sgg_codes: set[str] = field(default_factory=set)
    sido_codes: set[str] = field(default_factory=set)
    # 상용은 0채움 varchar('0014'), 구 자체 스키마는 정수다. 양쪽을 담고
    # 비교는 `_node_eq` 가 맞춘다. **표기를 바꾸지 않는다** — `as_truth()` 의
    # `expect_node` 가 graphio `detail.axis_ids`("SGW-0014")와 글자까지 같아야
    # 채점이 붙는다.
    sgw_id: str | int | None = None
    pgw_id: str | int | None = None
    mme_id: str | int | None = None
    #: **쓰지 않는다.** 상용 실측 184만 행이 전부 `'0'` 이고 규격 부록 A-31 이
    #: `S6a_Equip_Type` 의 `6:HSS`·`7:DRA` 를 삭제했다 — 장비 구분이 원천적으로
    #: 불가능하다. 인위적으로 채우지 않는다(V3 §9.2). 필드를 지우지 않는 것은
    #: 과거 정답 파일이 참조하고, 왜 비우는지가 여기 적혀 있어야 하기 때문이다.
    s6a_equip_id: str | int | None = None
    vendor_model: tuple[str, str] | None = None
    domain: str | None = None
    apn: str | None = None
    cell_pairs: list[tuple[str, str]] = field(default_factory=list)
    #: **MME 집합** — SC-14(3대) · SC-15(5대). "여러 MME 에 걸쳐 있지만 전국은
    #: 아니다"라는 관측 사실을 장비 ID 없이 표현하는 유일한 수단이다.
    mme_group: set[str] = field(default_factory=set)
    #: eNB 하나 — SC-17 무호. 셀이 아니라 eNB 단위로 억제한다.
    enb_id: str | None = None

    def label(self) -> str:
        bits = []
        if self.sgw_id:
            bits.append(f"SGW-{self.sgw_id}")
        if self.pgw_id:
            bits.append(f"PGW-{self.pgw_id}")
        if self.mme_id:
            bits.append(f"MME-{self.mme_id}")
        if self.mme_group:
            bits.append(f"MME {len(self.mme_group)}대")
        if self.enb_id:
            bits.append(f"eNB-{self.enb_id}")
        if self.vendor_model:
            bits.append("/".join(self.vendor_model))
        if self.domain:
            bits.append(self.domain)
        if self.apn:
            bits.append(self.apn)
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
    #: 변화 양상 — STEP(설정·작업 변경 의심) / RAMP(부하 누적·자원 소진) /
    #: STEPS(구간별 고정 강도 — 부분 복구·단절) / FLAP(주기적 열화·회복)
    shape: str = "STEP"
    peak: float = 1.0
    #: 시간에 따라 영향 셀이 늘어나는 확산성 장애인가 (R-18)
    spreading: bool = False
    ramp_in: int = 3
    series: tuple[str, ...] = ("lte_call",)
    cell_order: list[str] = field(default_factory=list)

    # ── 채점 메타 ────────────────────────────────────────────────────────
    #: 채점 시 어떤 축으로 Incident 와 매칭할지 — 지리 범위가 없는 시나리오가 있다
    scope_kind: str = "CELL"
    #: 이 시나리오가 발동시켜야 할 대표 트리거 KPI
    expect_kpi: str | None = None
    #: 정답 판정 등급 — `TOP1` | `TOP3` | `LAYER` | `UNDETERMINED` (V3 §4.5)
    grade: str = "TOP1"
    #: `UNDETERMINED`·낮은 confidence 도 정답으로 인정하는가 (SC-15b)
    expect_undetermined: bool = False

    # ── 시간 파형 ────────────────────────────────────────────────────────
    #: `STEPS` 용 `((경과분, 강도), ...)` — SC-17 · SC-18
    steps: tuple[tuple[int, float], ...] = ()
    #: `FLAP` 주기(분)와 저점 강도 — SC-20
    flap_period: int = 3
    flap_low: float = 0.0
    #: 계열별 시작 지연(분) — SC-21 의 "전송망 → 2분 뒤 DNS"
    series_offset: dict[str, int] = field(default_factory=dict)

    def intensity(self, minute: int) -> float:
        """분 슬롯에서의 장애 강도 0~peak."""
        if not (self.start_minute <= minute < self.start_minute + self.duration):
            return 0.0
        elapsed = minute - self.start_minute
        remain = self.start_minute + self.duration - minute
        if self.shape == "STEPS" and self.steps:
            # 구간별 고정 강도. **마지막 구간 뒤는 0 이고 회복 감쇠를 따로
            # 걸지 않는다** — `steps` 가 이미 복구 양상을 적고 있다.
            for until, level in self.steps:
                if elapsed < until:
                    return max(0.0, level * self.peak)
            return 0.0
        if self.shape == "FLAP":
            # 주기적 열화·회복. **회복 구간 감쇠를 걸지 않는다** — 톱니 모양이
            # 이 파형의 관측 신호이고(SC-20 ⑰) 끝에서 깎으면 마지막 주기가
            # 다른 모양이 된다.
            phase = (elapsed // max(1, self.flap_period)) % 2
            return max(0.0, (1.0 if phase == 0 else self.flap_low) * self.peak)
        if self.shape == "RAMP":
            base = min(1.0, (elapsed + 1) / max(1, self.duration * 0.6))
        else:
            base = min(1.0, (elapsed + 1) / max(1, self.ramp_in))
        if remain <= 2:  # 회복 구간
            base *= remain / 3.0
        return max(0.0, base * self.peak)

    def intensity_for(self, series: str, minute: int) -> float:
        """계열별 강도 — `series_offset` 만큼 늦게 시작한다(SC-21).

        ⚠️ `covers()` 는 **오프셋 없는** `intensity(minute)` 를 보므로
        `duration` 에 최대 오프셋을 더해 선언해야 한다. 그러지 않으면 늦게
        시작한 계열이 끝에서 잘린다(V3 §SC-21 선행 주석).
        """
        off = self.series_offset.get(series, 0)
        return self.intensity(minute - off) if off else self.intensity(minute)

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

    def as_truth(self) -> dict:
        t = self.target
        return {
            "scenario_id": self.id,
            "kind": self.kind,
            "scope_kind": self.scope_kind,
            "expect_kpi": self.expect_kpi,
            # **장비 미특정은 `null` 이다**(V3 §9.2). SC-14·15·15b 는 S6a 경로
            # 무응답까지만 적고 장비를 지목하지 않는다 — `mme_group` 을
            # expect_node 로 적으면 "MME 가 원인"이라는 없는 사실이 정답이 된다.
            "expect_node": (
                f"SGW-{t.sgw_id}" if t.sgw_id
                else f"PGW-{t.pgw_id}" if t.pgw_id
                else f"MME-{t.mme_id}" if t.mme_id
                else None
            ),
            "cells": sorted(t.cell_keys),
            "title": self.title,
            "truth_code": self.truth_code,
            "truth_category": self.truth_category,
            "expect_rules": list(self.expect_rules),
            "expect_geo": self.expect_geo,
            "start_minute": self.start_minute,
            "duration": self.duration,
            "shape": self.shape,
            "spreading": self.spreading,
            "scope": t.label(),
            "cell_cnt": len(t.cell_keys),
            "series": list(self.series),
            # ── V3 채점 확장 ───────────────────────────────────────────────
            "grade": self.grade,
            "expect_undetermined": self.expect_undetermined,
            "steps": [list(s) for s in self.steps],
            "series_offset": dict(self.series_offset),
            "apn": t.apn,
            "domain": t.domain,
            "mme_group": sorted(t.mme_group),
            "enb_id": t.enb_id,
        }


# Scenario는 분 슬롯마다 활성 셀 집합을 캐시해 두고 mutator가 그걸 본다.
Scenario.active_cells_cache = set()  # type: ignore[attr-defined]


# --------------------------------------------------------------------------
# kind 고유 속성 — **여기가 정본이다**
# --------------------------------------------------------------------------
#
# 전에는 `build_scenarios` 의 spec 리터럴과 `faults.KIND_SPECS` 두 벌이었고,
# 한쪽만 고치면 **같은 kind 가 배치 생성과 UI 주입에서 다른 정답을 갖는다** —
# 에러 없이 채점만 어긋난다. 이제 한 곳이고 `build_scenarios` 도 이것을 읽는다.
#
# `peak`·`target`·시간창은 호출부가 정한다(fault 행 또는 배치 간격).

KIND_SPECS: dict[str, dict] = {
    # ── 기존 13종 ─────────────────────────────────────────────────────────
    "gw_resource": dict(
        title="SGW 세션 자원 소진",
        truth_code="GW_RESOURCE_EXHAUSTION", truth_category="CORE_UP",
        expect_rules=("R-05", "R-02", "R-15", "R-21"), expect_geo="SCATTER",
        shape="RAMP", scope_kind="CELL", expect_kpi="C-02",
    ),
    "ran_radio": dict(
        title="특정 지역 무선 구간 열화",
        truth_code="RAN_RADIO_QUALITY", truth_category="RAN",
        expect_rules=("R-06", "R-22", "R-16", "R-20"), expect_geo="CLUSTER",
        shape="STEP", scope_kind="CELL", expect_kpi="C-02",
    ),
    "gw_node_down": dict(
        title="SGW 노드 무응답",
        # 규격의 R-04("F-08이 S11에 집중 + SGW/PGW 편중")가 지목하는 코드는
        # GW_NODE_DOWN 이다. NF_UNREACHABLE 은 구간 미특정 무응답에 쓴다.
        truth_code="GW_NODE_DOWN", truth_category="CORE_UP",
        expect_rules=("R-04", "R-00b", "R-15"), expect_geo="SCATTER",
        shape="STEP", ramp_in=2, scope_kind="CELL", expect_kpi="C-02",
    ),
    "gw_pgw_restart": dict(
        title="PGW 재기동 통보",
        truth_code="GW_NODE_DOWN", truth_category="CORE_UP",
        expect_rules=("R-05b", "R-04", "R-15"), expect_geo="SCATTER",
        # 재기동은 순간이다 — 계단이 1분이고 지속이 짧다
        shape="STEP", ramp_in=1, scope_kind="CELL", expect_kpi="C-02",
    ),
    "auth_failure": dict(
        title="S6a 인증 거절 (응답 수신형)",
        truth_code="AUTH_FAILURE", truth_category="CORE_CP",
        expect_rules=("R-01",), expect_geo="SCATTER",
        shape="STEP", scope_kind="GLOBAL", expect_kpi="P-01",
    ),
    "dns_degradation": dict(
        title="DNS 응답 열화",
        truth_code="DNS_DEGRADATION", truth_category="EXTERNAL",
        expect_rules=("R-14",), expect_geo="WIDE",
        shape="RAMP", series=("dns",), scope_kind="SERVICE", expect_kpi="U-29",
    ),
    "external_server": dict(
        title="외부 서비스 5xx 급증",
        truth_code="EXTERNAL_SERVER", truth_category="EXTERNAL",
        expect_rules=("R-14b", "R-09b"), expect_geo="WIDE",
        shape="STEP", series=("http",), scope_kind="SERVICE", expect_kpi="U-25",
    ),
    "external_path_latency": dict(
        title="외부 경로 지연 (백본)",
        truth_code="BACKBONE_DEGRADATION", truth_category="EXTERNAL",
        expect_rules=("R-09b",), expect_geo="WIDE",
        # R-09b 가 `EXTERNAL_SERVER` 를 내므로 Top-1 일치가 구조적으로 어렵다.
        # **의미상 같은 계층**이라 TOP3 로 둔다(V3 §SC-06b ⑯).
        shape="RAMP", series=("transport", "http"),
        scope_kind="SERVICE", expect_kpi="U-23", grade="TOP3",
    ),
    "transport_loss": dict(
        title="국지 전송 구간 손실",
        truth_code="TRANSPORT_LOSS", truth_category="TRANSPORT",
        expect_rules=("R-09", "R-16"), expect_geo="CLUSTER",
        shape="STEP", series=("transport",), scope_kind="CELL", expect_kpi="U-12",
    ),
    "ue_model": dict(
        title="특정 단말 기종 접속 실패",
        truth_code="UE_MODEL_ISSUE", truth_category="UE",
        expect_rules=("R-10",), expect_geo="SCATTER",
        shape="STEP", scope_kind="UE", expect_kpi="C-02",
    ),
    "mobility": dict(
        title="핸드오버 실패 (셀 쌍 집중)",
        truth_code="RAN_MOBILITY_ISSUE", truth_category="RAN",
        expect_rules=("R-17",), expect_geo="CORRIDOR",
        shape="STEP", scope_kind="CELL", expect_kpi="PSF",
    ),
    "mme_overload": dict(
        title="MME 과부하 (확산 중)",
        truth_code="MME_OVERLOAD", truth_category="CORE_CP",
        expect_rules=("R-23", "R-18", "R-15"), expect_geo="SCATTER",
        shape="RAMP", spreading=True, scope_kind="CELL", expect_kpi="C-02",
    ),
    "change_suspected": dict(
        title="계단형 급락 (변경 의심)",
        truth_code="CHANGE_SUSPECTED", truth_category="CHANGE",
        expect_rules=("R-11", "R-03"), expect_geo="WIDE",
        # XDR 만으로 "설정 변경"을 확정할 수 없다 — `R-11` 의
        # `confidence_cap=0.65` 가 그 뜻이고 Top-1 이 R-03 으로 갈릴 수 있다.
        shape="STEP", ramp_in=1, scope_kind="CELL", expect_kpi="C-02",
        grade="TOP3",
    ),
    "ims_pdn": dict(
        title="VoLTE(ims PDN) 전용 접속 실패",
        truth_code="IMS_PDN_ISSUE", truth_category="IMS",
        expect_rules=("R-30",), expect_geo="SCATTER",
        shape="STEP", scope_kind="GLOBAL", expect_kpi="C-21",
    ),
    "nsa_nr_leg": dict(
        title="5G NSA NR 레그 연결 실패",
        truth_code="NSA_NR_LEG_FAILURE", truth_category="NSA",
        expect_rules=("R-33",), expect_geo="CLUSTER",
        shape="RAMP", scope_kind="CELL", expect_kpi="N-02",
    ),
    "nsa_policy_restrict": dict(
        title="DCNR 정책 제한",
        truth_code="NSA_POLICY_RESTRICT", truth_category="NSA",
        expect_rules=("R-34",), expect_geo="WIDE",
        shape="STEP", ramp_in=2, scope_kind="CELL", expect_kpi="N-02",
    ),
    # ── 신규 8종 ──────────────────────────────────────────────────────────
    "auc_no_response": dict(
        title="S6a 무응답 (TIMEOUT)",
        truth_code="NF_UNREACHABLE", truth_category="CORE_CP",
        expect_rules=("R-00b", "R-01", "R-15"), expect_geo="SCATTER",
        shape="RAMP", scope_kind="GLOBAL", expect_kpi="P-01",
    ),
    "dra_route_failure": dict(
        title="S6a 라우팅 계층 이상 (Diameter 3xxx)",
        truth_code="AUTH_FAILURE", truth_category="CORE_CP",
        expect_rules=("R-01",), expect_geo="SCATTER",
        # **계층까지만 맞히면 정답이다.** 장비를 맞히라고 요구하지 않는다.
        shape="STEP", ramp_in=2, scope_kind="GLOBAL", expect_kpi="P-01",
        grade="LAYER",
    ),
    "s6a_undetermined": dict(
        title="S6a 무응답 (장비 미특정)",
        truth_code="NF_UNREACHABLE", truth_category="CORE_CP",
        expect_rules=("R-00b", "R-01"), expect_geo="SCATTER",
        # **구분할 수 없는 것을 구분해 내라고 채점하지 않는다.** 근거 없이
        # 찍는 쪽으로 기울면 운영에서 가장 나쁜 실패가 된다(V3 §SC-15b).
        shape="RAMP", scope_kind="GLOBAL", expect_kpi="P-01",
        grade="UNDETERMINED", expect_undetermined=True,
    ),
    "pgw_uplane_degradation": dict(
        title="PGW 사용자평면 품질 저하",
        truth_code="GW_OVERLOAD", truth_category="CORE_UP",
        expect_rules=("R-09e",), expect_geo="SCATTER",
        shape="RAMP", series=("transport", "http"),
        scope_kind="NODE", expect_kpi="U-12",
    ),
    "enb_s1_outage": dict(
        title="eNB 전송 단절 (무호)",
        truth_code="RAN_NODE_DOWN", truth_category="RAN",
        expect_rules=("R-36",), expect_geo="POINT",
        shape="STEPS", steps=((2, 0.30), (17, 1.00), (19, 0.70), (20, 0.40)),
        scope_kind="CELL", expect_kpi="C-01",
    ),
    "partial_recovery": dict(
        title="부분 장애와 자동 복구",
        truth_code="GW_NODE_DOWN", truth_category="CORE_UP",
        expect_rules=("R-04", "R-00b"), expect_geo="SCATTER",
        shape="STEPS", steps=((5, 0.80), (12, 0.50), (20, 0.20)),
        scope_kind="CELL", expect_kpi="C-02",
    ),
    "apn_pdn_failure": dict(
        title="APN 별 PDN 접속 실패",
        truth_code="SUBSCRIPTION_ERROR", truth_category="CORE_CP",
        expect_rules=("R-07",), expect_geo="WIDE",
        shape="STEP", ramp_in=2, scope_kind="CELL", expect_kpi="C-02",
    ),
    "dns_flap": dict(
        title="DNS 간헐 실패",
        truth_code="DNS_DEGRADATION", truth_category="EXTERNAL",
        expect_rules=("R-14",), expect_geo="WIDE",
        shape="FLAP", flap_period=3, flap_low=0.0, series=("dns",),
        scope_kind="SERVICE", expect_kpi="U-29",
    ),
    "compound_causal_failure": dict(
        title="복합 장애 (전송망 → DNS → HTTP)",
        truth_code="TRANSPORT_LOSS", truth_category="TRANSPORT",
        expect_rules=("R-09", "R-16", "R-14"), expect_geo="CLUSTER",
        shape="RAMP", series=("transport", "dns"),
        series_offset={"dns": 2},
        scope_kind="CELL", expect_kpi="U-12",
    ),
}


# --------------------------------------------------------------------------
# 레코드 변형 — kind별 구현
# --------------------------------------------------------------------------

Mutator = Callable[[Rec, Scenario, float, random.Random, Cell], bool]
MUTATORS: dict[str, Mutator] = {}

PlanMutator = Callable[[Plan, Scenario, float, random.Random, Cell], bool]
PLAN_MUTATORS: dict[str, PlanMutator] = {}

Suppressor = Callable[[Scenario, Cell, float, random.Random], bool]
SUPPRESSORS: dict[str, Suppressor] = {}


def mutator(kind: str) -> Callable[[Mutator], Mutator]:
    def deco(fn: Mutator) -> Mutator:
        MUTATORS[kind] = fn
        return fn

    return deco


def plan_mutator(kind: str) -> Callable[[PlanMutator], PlanMutator]:
    def deco(fn: PlanMutator) -> PlanMutator:
        PLAN_MUTATORS[kind] = fn
        return fn

    return deco


def suppressor(kind: str) -> Callable[[Suppressor], Suppressor]:
    def deco(fn: Suppressor) -> Suppressor:
        SUPPRESSORS[kind] = fn
        return fn

    return deco


# --------------------------------------------------------------------------
# 공통 보조
# --------------------------------------------------------------------------

#: `first_error_interface_protocol` → 구간별 Cause 컬럼 접두어 (CD_IF_PROTO_CEI).
#: graphio `codes.INTERFACE_PREFIX` 와 같아야 한다.
IFACE_PREFIX = {1: "s6a", 2: "s1ap", 3: "s11", 4: "s10",
                5: "emm", 6: "esm", 7: "s3", 8: "s13"}

#: 세션 생성이 있는 절차(`CD_CALL_TYPE` 규격 체계). TAU(5)·Paging(6)·Detach(9)·
#: 핸드오버(10·11)는 세션을 만들지 않으므로 GW 거절의 대상이 아니다.
CT_SESSION_SETUP = frozenset({"1", "2", "3", "4", "7", "8"})
#: Attach 계열 — NAS 거절이 `ATTACH_REJECT(68)` 이다
CT_ATTACH = frozenset({"1", "2"})
#: Service 계열 — NAS 거절이 `SERVICE_REJECT(78)` 이다
CT_SERVICE = frozenset({"3", "4", "7", "8"})


def _node_eq(raw: str, want: str | int | None) -> bool:
    """레코드의 노드 ID 가 시나리오 대상과 같은가.

    표기가 두 벌이다. 상용 `XDR_LTE_CALL_KPI.SGW_ID`·`MME_ID`·`PGW_ID` 는
    **0 채움 varchar** (`'0014'`)이고, 구 자체 스키마는 정수였다. 문자열로
    비교하면 `'14'` 와 `'0014'` 가 어긋나 **변형이 한 건도 걸리지 않는다** —
    에러 없이 장애가 주입되지 않고, 채점만 0% 로 나온다. graphio 쪽
    `detector.resolve_axis_cells` 가 같은 이유로 `cast_int` 를 쓴다.

    숫자로 접어 비교하고, 숫자가 아니면 문자열로 떨어뜨린다.
    """
    if want is None:
        return True
    try:
        return int(raw or 0) == int(want)
    except (TypeError, ValueError):
        return str(raw) == str(want)


def _in_group(raw: str, group: set[str]) -> bool:
    """노드 ID 가 집합 안에 있는가 — `_node_eq` 와 같은 표기 문제를 푼다."""
    if not group:
        return True
    return any(_node_eq(raw, g) for g in group)


def _start(rec: Rec) -> float:
    return rec.num("call_start_time")


def _end(rec: Rec) -> float:
    return rec.num("call_end_time") or rec.num("call_start_time")


def _set_first(rec: Rec, interface: int, cause: int, message: int, t: float) -> None:
    rec.set("first_error_interface_protocol", interface)
    rec.set("first_error_cause", cause)
    rec.set("first_error_message", message)
    rec.set("first_error_time", f"{t:.6f}")


def _set_last(rec: Rec, interface: int, cause: int, message: int, t: float) -> None:
    rec.set("last_error_interface_protocol", interface)
    rec.set("last_error_cause", cause)
    rec.set("last_error_message", message)
    rec.set("last_error_time", f"{t:.6f}")


def _fail_connect(rec: Rec, interface: int, cause: int, message: int,
                  t: float | None = None,
                  last: tuple[int, int, int, float] | None = None) -> None:
    """소통 실패 호로 만든다 — 접속 단계에서 무너지는 형태 (R-21).

    * ``t`` — First Error 시각(epoch). 없으면 `call_start_time`.
      구 구현은 항상 `call_start_time` 을 썼는데, 그러면 **절차가 걸린 시간이
      0 초**가 되어 T-1·T-2(시각 정합)를 검증할 수 없다.
    * ``last`` — `(interface, cause, message, time)`. 없으면 Last = First 다
      (규칙 C-5: 하류 증상이 없으면 둘이 같다). **`call_end_time` 으로
      두지 않는다** — 접속이 실패한 호에서 마지막 오류는 호가 끝날 때가
      아니라 그 거절이 전파된 시점이다.

    `drop_flag` 는 **0 으로 둔다**(규칙 C-2). 접속 실패와 연결 후 절단을 한
    레코드에 함께 두면 `C-02`·`C-04` 가 같은 호를 양쪽에서 센다.
    """
    t0 = _start(rec) if t is None else t
    rec.set("success_flag", 0)
    rec.set("drop_flag", 0)
    rec.set("data_success_flag", 0)
    if rec.get("ims_attempt_flag") == "1":
        rec.set("ims_success_flag", 0)
    _set_first(rec, interface, cause, message, t0)
    if last is None:
        _set_last(rec, interface, cause, message, t0)
    else:
        _set_last(rec, *last)


def _connected(rec: Rec) -> bool:
    """**연결 후 절단**을 심을 수 있는 호인가 (규칙 C-2·C-4).

    `_coerce` 는 실측 분포로 `attempt_flag` 를 뽑고 `success_flag` 를 그것에
    묶는다 — 그래서 **시도 자체가 없는 호**(Detach·TAU·Paging 의 일부, 실측
    약 4%)가 있고 그 호는 `attempt=0 · success=0` 이다.

    절단 갈래가 `success_flag` 를 **1 로 세우면** 그 호가 `attempt=0 ·
    success=1` 이 되어 `C-02` 가 **100% 를 넘는다**(실측 24건 / 10만 호에서
    100.023%). V3 의 표가 `success_flag` 를 "유지"라고 적은 것이 그 뜻인데
    구현이 `set(1)` 이었다 — 평시 배경이 들어오기 전에는 모든 호가
    `attempt=1` 이라 드러나지 않았다.

    그래서 **세우지 않고 확인한다.** 연결되지 않은 호는 절단 갈래를 건너뛴다 —
    "접속하지 못한 호"와 "연결 후 끊긴 호"는 다른 사실이다.
    """
    return rec.get("success_flag") == "1" and rec.get("attempt_flag") == "1"


def _nas_reject(rec: Rec, rng: random.Random, t0: float,
                prob: float) -> tuple[int, int] | None:
    """하류 NAS 거절을 심는다 — `(cause, message)` 또는 ``None``.

    MME 가 UE 에게 통보하는 자리이므로 **`OUT` 메시지**다(규칙 D-1).
    Attach 호에 `SERVICE_REJECT` 를, Service 호에 `ATTACH_REJECT` 를 적으면
    절차와 메시지가 어긋난다 — `call_type` 으로 가른다.
    """
    if rng.random() >= prob:
        return None
    ct = rec.get("call_type")
    if ct in CT_ATTACH:
        msg = 68                      # ATTACH_REJECT (OUT)
    elif ct in CT_SERVICE:
        msg = 78                      # SERVICE_REJECT (OUT)
    else:
        return None                   # TAU·Paging·Detach — 거절 메시지가 다르다
    t = t0 + rng.uniform(0.01, 0.10)
    _set_last(rec, 5, 17, msg, t)     # EMM 17 Network failure
    rec.set("emm_error_Cause", 17)
    rec.set("emm_error_Message", msg)
    rec.set("emm_error_Time", f"{t:.6f}")
    return 17, msg


def _timeout_core_duration(rec: Rec, rng: random.Random, wait: float) -> None:
    """TIMEOUT 호의 `initial_core_duration` — **대기시간을 하한으로 쓴다**.

    배율(`× (1+3.0×I)`)로 두면 평시값이 작은 호는 TIMEOUT 대기보다 짧은 지연이
    남아 규칙 T-2(대기한 시간이 지연에 반영되어야 한다)를 깬다. V2 의 생성식이
    그랬다.
    """
    base = rec.num("initial_core_duration")
    rec.set("initial_core_duration",
            int(max(base, wait * 1e6 * rng.uniform(0.8, 1.1))))


# --------------------------------------------------------------------------
# SC-01 · gw_resource — SGW 세션 자원 부족
# --------------------------------------------------------------------------


@mutator("gw_resource")
def _gw_resource(rec, sc, intensity, rng, cell) -> bool:
    """SGW 세션 자원 소진 — S11 Cause 73 (No resources available).

    거절 **응답을 받았다**: `s11_error_Message = 33 CREATE_SESSION_RESPONSE`
    (IN). SC-03(무응답)과 갈리는 자리다 — 그쪽은 `900`/`32`(OUT)다.
    """
    if not _node_eq(rec.get("SGW_ID"), sc.target.sgw_id):
        return False
    if rec.get("call_type") not in CT_SESSION_SETUP:
        return False            # 세션 생성이 없는 절차는 거절될 자리가 없다
    if rng.random() >= intensity * 0.60:
        return False
    t = _start(rec) + rec.num("initial_access_duration") / 1e6 + rng.uniform(0.01, 0.05)
    _fail_connect(rec, interface=3, cause=73, message=33, t=t)
    rec.set("s11_error_Cause", 73)
    rec.set("s11_error_Message", 33)
    rec.set("s11_error_Time", f"{t:.6f}")
    # 하류 NAS 거절 45%. Service 호는 ESM `209 PDN_CONNECTIVITY_REJECT`(OUT),
    # Attach 호는 EMM `68 ATTACH_REJECT`(OUT). **`195` 는 UE→MME 라 이 자리에
    # 올 수 없다**(V2→V3 수정 1).
    if rng.random() < 0.45:
        ct = rec.get("call_type")
        td = t + rng.uniform(0.01, 0.10)
        if ct in CT_SERVICE:
            _set_last(rec, 6, 26, 209, td)
            rec.set("esm_error_Cause", 26)      # Insufficient resources
            rec.set("esm_error_Message", 209)
            rec.set("esm_error_Time", f"{td:.6f}")
        elif ct in CT_ATTACH:
            _set_last(rec, 5, 17, 68, td)
            rec.set("emm_error_Cause", 17)
            rec.set("emm_error_Message", 68)
            rec.set("emm_error_Time", f"{td:.6f}")
    rec.scale("initial_core_duration", 1.0 + 1.6 * intensity)
    return True


# --------------------------------------------------------------------------
# SC-02 · ran_radio — RAN 무선 품질 저하
# --------------------------------------------------------------------------


@mutator("ran_radio")
def _ran_radio(rec, sc, intensity, rng, cell) -> bool:
    """무선 구간 열화 — S1AP 1xx 대역. 접속 실패·절단·지연이 함께 는다.

    세 갈래다(20/30/50). 구 구현은 실패와 절단을 45/55 로 갈라 **지연만
    나빠지는 호가 없었다** — 실제 무선 열화는 대부분 재전송·재시도로 흡수되고
    일부만 실패한다.

    `UEContextReleaseRequest_Cause` 는 **설정하지 않는다** — 집계·RCA 사용
    0건인데 값을 적으면 "쓰는 필드"로 오해된다(V3 §4.4).
    """
    if cell.cell_key not in sc.active_cells_cache:
        return False
    rec.scale("initial_access_duration", 1.0 + 1.4 * intensity)
    if rng.random() >= intensity * 0.60:
        return False
    roll = rng.random()
    if roll < 0.20:
        # ① 접속 실패 — InitialContextSetup(9, OUT)이 실패한 절차다
        cause = rng.choice([125, 126])
        t = _start(rec) + rng.uniform(0.05, 0.50)
        _fail_connect(rec, interface=2, cause=cause, message=9, t=t)
        rec.set("s1ap_error_Cause", cause)
        rec.set("s1ap_error_Message", 9)
        rec.set("s1ap_error_Time", f"{t:.6f}")
    elif roll < 0.50 and _connected(rec):
        # ② 연결 후 절단 — **First Error 를 건드리지 않는다.** 접속은 성공했다.
        #    `success_flag` 를 **세우지 않는다**(규칙 C-4 — `_connected` 주석)
        t = _end(rec)
        rec.set("drop_flag", 1)
        rec.set("UEContextRelease_Cause", 121)  # Radio Connection With UE Lost
        rec.set("UEContextRelease_Time", f"{t:.6f}")
        _set_last(rec, 2, 121, 23, t)           # UEContextRelease(23, OUT)
    # ③ 지연만 50% — 플래그·오류 무변경
    return True


# --------------------------------------------------------------------------
# SC-03 · gw_node_down — SGW 무응답 / SC-03b · gw_pgw_restart
# --------------------------------------------------------------------------


@mutator("gw_node_down")
def _gw_node_down(rec, sc, intensity, rng, cell) -> bool:
    """SGW 무응답 — S11 Create Session Request 에 답이 없다.

    `s11_error_Message = 32 CREATE_SESSION_REQUEST`(OUT) 다. **응답을 못
    받았으므로 요청 메시지를 적는다** — 존재하지 않는 응답 메시지를 TIMEOUT
    상황에 만들지 않는다(V3 §9.1).

    ## 하류 NAS 거절을 만든다 (2026-10-22 교정 · 사용자 조건부 승인)

    구 구현은 *"무응답에는 하류 증상이 없다"* 로 `last_error = first_error`
    였다. **운영 XDR 실측이 반대다** — `IF=3 · cause=900 · msg=32` 7건
    **전부** 하류가 남았다:

        call_type=1 Attach   last=(6, 30, 209)  esm 30   ×3
        call_type=1 Attach   last=(5, 15,  68)  emm 15   ×3
        call_type=3 Service  last=(6, 33, 209)  esm 33   ×1

    MME 는 SGW 응답을 못 받으면 타이머 만료 후 UE 에게 거절을 보내야 한다 —
    그래야 단말이 재시도한다. 흐름상 자연스럽다.

    **다만 "항상 ESM 30" 은 아니다**(3/7). 표본이 7건이라 비율을 그대로
    베끼면 과적합이고, `EMM 15`(No Suitable Cells)는 SGW 장애와 의미가 맞지
    않아 오히려 판정을 흐린다. 그래서 **의미가 맞는 `ESM 30`
    (Request rejected by Serving GW or PDN GW)만** 만들고, 나머지는 구 동작
    (하류 없음)을 남긴다 — 둘 다 운영에 존재하는 모양이다.

    **전역 규칙이 아니다.** 이 함수 안에서만 적용한다. 다른 TIMEOUT
    시나리오(SC-14·SC-15b·SC-18)는 건드리지 않는다.
    """
    if not _node_eq(rec.get("SGW_ID"), sc.target.sgw_id):
        return False
    if rec.get("call_type") not in CT_SESSION_SETUP:
        return False
    if rng.random() >= intensity * 0.90:
        return False
    wait = FAULT.timeout_wait_s11
    t = _start(rec) + wait
    if rng.random() < 0.70:
        # 하류 ESM 거절이 **실제로 생성되는 경우** 에만 Last 를 바꾼다
        td = t + rng.uniform(0.05, 0.30)
        _fail_connect(rec, interface=3, cause=900, message=32, t=t,
                      last=(6, 30, 209, td))
        rec.set("esm_error_Cause", 30)
        rec.set("esm_error_Message", 209)   # PDN_CONNECTIVITY_REJECT (OUT)
        rec.set("esm_error_Time", f"{td:.6f}")
    else:
        _fail_connect(rec, interface=3, cause=900, message=32, t=t)
    rec.set("s11_error_Cause", 900)
    rec.set("s11_error_Message", 32)
    rec.set("s11_error_Time", f"{t:.6f}")
    _timeout_core_duration(rec, rng, wait)
    return True


@mutator("gw_pgw_restart")
def _gw_pgw_restart(rec, sc, intensity, rng, cell) -> bool:
    """PGW 재기동 통보 — S11 Cause 72 + `179 PGW_RESTART_NOTIFICATION`(IN).

    **SC-03 에서 분리했다.** 구 구현은 18% 확률로 SC-03 에 섞었는데, 그러면
    SC-03 의 정답이 `R-04` 인지 `R-05b` 인지 흐려진다. 분리하면 두 룰이 각자
    검증된다 — `R-05b` 는 이 조합만으로 판정하므로 **이것이 그 룰의 유일한
    검증 경로**다.

    ⚠️ 재기동은 실제로는 **기존 세션도 무효화**한다. 그것을 표현하려면
    `SessionEngine` 에 "특정 PGW 세션 강제 종료" 경로가 필요해 이번 범위에서
    제외했다(V3 §12-18) — 신규 접속 실패만 만든다.
    """
    if not _node_eq(rec.get("PGW_ID"), sc.target.pgw_id):
        return False
    if rec.get("call_type") not in CT_SESSION_SETUP:
        return False
    if rng.random() >= intensity * 0.70:
        return False
    t = _start(rec) + rng.uniform(0.1, 0.6)
    _fail_connect(rec, interface=3, cause=72, message=179, t=t)
    rec.set("s11_error_Cause", 72)          # System failure
    rec.set("s11_error_Message", 179)
    rec.set("s11_error_Time", f"{t:.6f}")
    rec.scale("initial_core_duration", 1.0 + 1.2 * intensity)
    return True


# --------------------------------------------------------------------------
# SC-04 · auth_failure — S6a 인증 거절 (응답 수신형)
# --------------------------------------------------------------------------


@mutator("auth_failure")
def _auth_failure(rec, sc, intensity, rng, cell) -> bool:
    """S6a 인증 거절 — AIR 에 **오류 Result-Code 응답**(AIA)이 왔다.

    **`auth_attempt_flag` 를 세우지 않는다.** 구 구현은 대상 호에 인증 절차를
    표시하고 실패시켰는데, 그러면 분모를 장애가 만들어 `P-01` 이 "인증을 시도한
    호의 성공률"이 아니라 "장애 호의 비율"이 된다. 평시 배경
    (`pemdb1.Baseline`)이 분모를 만들고 여기서는 **그 중 일부를 실패시킨다**.

    Cause 는 **5xxx 우세**다(영구 실패 계층). TIMEOUT(`900`)은 여기 섞지
    않는다 — 그것이 SC-14 이고, 한 호에 프로브 Cause 와 Result-Code 를 함께
    적지 않는다(규칙 D-2).
    """
    if rec.get("auth_attempt_flag") != "1":
        return False
    if rng.random() >= intensity * 0.50:
        return False
    cause = rng.choices([5012, 5030, 3004], weights=[0.5, 0.3, 0.2], k=1)[0]
    t = _start(rec) + rng.uniform(0.02, 0.20)
    rec.set("auth_success_flag", 0)
    rec.set("AuthenticationInformation_Cause", cause)
    rec.set("AuthenticationInformation_Time", f"{t:.6f}")
    # Result-Code 응답을 받았으므로 **구간 Cause 를 기록한다.** TIMEOUT 이면
    # 비워야 하지만(D.2) 여기는 900 이 아니다.
    rec.set("s6a_error_Cause", cause)
    rec.set("s6a_error_Message", 318)       # AIR/AIA (OUT)
    rec.set("s6a_error_Time", f"{t:.6f}")
    _fail_connect(rec, interface=1, cause=cause, message=318, t=t)
    _nas_reject(rec, rng, t, 0.70)
    return True


# --------------------------------------------------------------------------
# SC-14 · auc_no_response / SC-15 · dra_route_failure / SC-15b
# --------------------------------------------------------------------------


def _s6a_fault(rec, sc, intensity, rng, gate: float, timeout: bool) -> bool:
    """SC-14·15·15b 공용 — `timeout` 하나로 무응답/거절을 가른다.

    | | 무응답(SC-14·15b) | 3xxx 거절(SC-15) |
    | --- | --- | --- |
    | `AuthenticationInformation_Cause` | `900` | `3002`/`3004`/`3005` |
    | `s6a_error_*` | **비운다**(D.2) | 기록한다 |
    | `FE_TIMEOUT` | 100% | 0 |
    | `initial_core_duration` | 대기시간 하한(T-2) | `× (1+1.2×I)` |

    둘 다 **장비를 지목하지 않는다.** `S6a_*_Equip_ID` 를 채우지 않고
    (상용 실측 184만 행 전부 `'0'` · 규격 A-31 이 `6:HSS`·`7:DRA` 를 삭제했다)
    대상은 MME 집합으로만 표현한다 — graphio 가 S6a 장비 축을 집계하지
    않으므로 채워도 쓰이지 않고, 쓰이지 않는 값을 지어내지 않는다(V3 §9.2).
    """
    if rec.get("auth_attempt_flag") != "1":
        return False
    if not _in_group(rec.get("MME_ID"), sc.target.mme_group):
        return False
    if rng.random() >= intensity * gate:
        return False
    rec.set("auth_success_flag", 0)
    if timeout:
        wait = FAULT.timeout_wait_s6a
        t = _start(rec) + wait
        rec.set("AuthenticationInformation_Cause", 900)
        rec.set("AuthenticationInformation_Time", f"{t:.6f}")
        # ★ `s6a_error_Cause`·`_Message` 를 **설정하지 않는다**(D.2 ·
        #   `codes.INTERFACE_TIMEOUT_EXCLUDED = {1, 8}`). `EV-03.by_stage['s6a']`
        #   가 비어 있는 것이 C-1 준수의 관측 증거다.
        _fail_connect(rec, interface=1, cause=900, message=318, t=t)
        _timeout_core_duration(rec, rng, wait)
        _nas_reject(rec, rng, t, 0.80)
    else:
        cause = rng.choices([3002, 3004, 3005], weights=[0.5, 0.35, 0.15], k=1)[0]
        t = _start(rec) + rng.uniform(0.02, 0.20)   # 거절은 빠르다
        rec.set("AuthenticationInformation_Cause", cause)
        rec.set("AuthenticationInformation_Time", f"{t:.6f}")
        rec.set("s6a_error_Cause", cause)
        rec.set("s6a_error_Message", 318)
        rec.set("s6a_error_Time", f"{t:.6f}")
        _fail_connect(rec, interface=1, cause=cause, message=318, t=t)
        rec.scale("initial_core_duration", 1.0 + 1.2 * intensity)
        _nas_reject(rec, rng, t, 0.70)
    return True


@mutator("auc_no_response")
def _auc_no_response(rec, sc, intensity, rng, cell) -> bool:
    """S6a 무응답 — 원인 장비를 특정하지 않는다. MME 3대에 걸친다."""
    return _s6a_fault(rec, sc, intensity, rng, gate=0.70, timeout=True)


@mutator("dra_route_failure")
def _dra_route_failure(rec, sc, intensity, rng, cell) -> bool:
    """S6a Diameter **3xxx 대역**(라우팅 계층) 거절. MME 5대에 걸친다.

    🔴 "DRA 장비 장애"로 단정하지 않는다 — `DIAMETER_ROUTING_RANGE` 는 규격
    D.7 이 분류한 **대역의 성격**이고 장비를 지목하는 근거가 아니다.
    """
    return _s6a_fault(rec, sc, intensity, rng, gate=0.60, timeout=False)


@mutator("s6a_undetermined")
def _s6a_undetermined(rec, sc, intensity, rng, cell) -> bool:
    """SC-14 와 **똑같은 무응답.** 다른 것은 대상 MME 집합뿐이다.

    구분 불가능한 쌍의 한쪽이다. RCA 가 특정 장비를 원인으로 **단정하면
    오답**이고, `NF_UNREACHABLE` 또는 `UNDETERMINED` 를 내면 정답이다.
    """
    return _s6a_fault(rec, sc, intensity, rng, gate=0.70, timeout=True)


# --------------------------------------------------------------------------
# SC-05 · dns_degradation / SC-20 · dns_flap
# --------------------------------------------------------------------------


def _dns_fail(rec: Rec, rng: random.Random, intensity: float) -> None:
    """DNS 실패 갈래 — 서버 오류(A)와 무응답(B)을 **다른 칸에 센다**.

    graphio 는 `DNS_FAIL`(rcode≠0)과 `DNS_NORESP`(`ResponseTime ≤ 0`)를 갈라
    집계한다 — 조치가 다르기 때문이다(서버 장애 vs 경로 단절). 한쪽으로만
    만들면 그 구분이 한 번도 검증되지 않는다.

    **무응답은 `ReplyCode = 900` 이다** (2026-10-22 운영 XDR 실측으로 교정).
    구 구현은 *"응답이 없으면 rcode 도 없다"* 는 전제로 `ReplyCode` 를
    건드리지 않았는데, **운영 데이터에 그 모양이 한 건도 없다** —
    DNS 10만 행 분류:

        정상                      99,086
        rcode=900 & RT=0  무응답      401   ← 운영의 무응답 표현
        빈 레코드                     382
        rcode≠0  & RT>0  서버 오류     131
        rcode=0  & RT=0                0   ← 구 구현이 만들던 모양

    `900` 은 C-Plane 과 같은 프로브 전용 Process Specific Cause 다
    (`pemdb6.CD_CAUSE` 에 `C_07000900 Request Time Out` 으로도 있다).
    평시 배경은 `generator._emit` 이 `smp.categorical("ReplyCode")` 로 실측
    분포를 쓰므로 `900` 이 섞여 나온다 — **장애 때만 안 나오면 역전이다.**

    `ResolvedIP` 는 **건드리지 않는다** — Parquet 에 없다(V3 §4.4).
    """
    if rng.random() < 0.55:
        # ① 서버 오류 — 응답은 **왔다**. `ResponseTime` 을 유지한다
        rec.set("ReplyCode", 2)             # SERVFAIL
        rec.set("AnswerCount", 0)
        # 응답을 받았으므로 재시도가 거의 없다 (실측 1:127 · 2:2)
        retry = 1 if rng.random() < 0.98 else 2
    else:
        # ② 무응답 — `ReplyCode = 900`(TIMEOUT) + `ResponseTime = 0`
        rec.set("ReplyCode", 900)
        rec.set("ResponseTime", "0.0")
        rec.set("AnswerCount", 0)
        # 무응답일 때만 재시도가 는다 (실측 1:315 · 2:84 · 3:2).
        # **일률적으로 강제하지 않는다** — 분포를 따르되 1회가 다수다.
        retry = rng.choices((1, 2, 3), weights=(79, 20, 1))[0]
    rec.set("QueryRetryCount", retry)
    # ⚠️ `dns_retry_sequence`·`dns_retry_interval` 은 운영 실측에서 **530건
    # 전부 `0`** 이다(쓰이지 않는 필드로 보인다). 지금은 구 동작을 유지한다 —
    # 필드 의미를 바꾸는 변경이라 사람 판단이 필요하다.
    rec.set("dns_retry_sequence", retry)    # 순서 = 횟수 (마지막 재시도의 레코드다)
    rec.set("dns_retry_interval", round(rng.uniform(1.0, 5.0), 3))


@mutator("dns_degradation")
def _dns_degradation(rec, sc, intensity, rng, cell) -> bool:
    """DNS 열화 — 망 지표는 정상인데 체감 장애가 나는 가장 흔한 숨은 원인."""
    if rng.random() >= intensity * 0.70:
        return False
    _dns_fail(rec, rng, intensity)
    return True


@mutator("dns_flap")
def _dns_flap(rec, sc, intensity, rng, cell) -> bool:
    """DNS 간헐 실패 — 3분 열화 / 3분 회복을 반복한다 (`FLAP`).

    **검증 대상은 Top-1 이 아니다.** "같은 원인의 반복 열화를 Incident 몇 건으로
    압축하는가"다 — 운영 알람 실측에서 `접속 성공율 @ S6AuC` 가 98.6만 건 →
    사건 1개였던 것과 같은 문제다(FAULT-CATALOG §1.4).

    **성공 질의의 응답시간도 함께 올린다.** `U-32` 에 탐지 스펙이 없어
    Incident 를 만들지는 못하지만 집계는 되므로(`DNS_RT_SUM/CNT`) 화면·증거팩에
    사실로 남는다. 현 생성기는 고정 20ms 다.
    """
    if intensity <= 0.0:
        return False                        # 회복 구간
    if rng.random() < intensity * 0.70:
        _dns_fail(rec, rng, intensity)
        return True
    # 실패하지 않은 질의 — 지연만 오른다
    req = rec.num("RequestFirstTime")
    if req > 0:
        rec.set("ResponseTime", f"{req + rng.uniform(0.02, 0.02 + 0.5 * intensity):.6f}")
    return True


# --------------------------------------------------------------------------
# SC-06 · external_server / SC-06b · external_path_latency
# --------------------------------------------------------------------------


def _domain_hit(rec: Rec, sc: Scenario) -> bool:
    if not sc.target.domain:
        return True
    host = rec.get("Host") or rec.get("ClientHelloDomain")
    return sc.target.domain in host


@mutator("external_server")
def _external_server(rec, sc, intensity, rng, cell) -> bool:
    """외부 서버 5xx — 특정 Host 의 애플리케이션 오류 (R-14b).

    **down·up 축을 건드리지 않는다.** 5xx 만으로 `U-25`(도메인별 5xx율)가
    임계를 넘으므로 충분하고, RTT 를 함께 올리면 `R-09b`·`R-09d` 와 섞여
    "외부 서버"와 "백본 지연"이 구분되지 않는다. 경로 지연은 SC-06b 다.

    `HTTP_Total_Response_PayloadSize` 는 **건드리지 않는다** — Parquet 에 없다.
    """
    if not _domain_hit(rec, sc):
        return False
    if rng.random() >= intensity * 0.85:
        return False
    rec.set("StatusCode", rng.choices([500, 502, 503, 504],
                                      weights=[3, 2, 3, 2], k=1)[0])
    return True


@mutator("external_path_latency")
def _external_path_latency(rec, sc, intensity, rng, cell) -> bool:
    """외부 경로·백본 지연 — **up 축만** 오르고 5xx 는 없다 (R-09b).

    `UplinkRTTTime` 의 규격 desc 는 "SGW 방향의 Round Trip Time" 이다 — HTTP
    서버 처리시간이 아니라 **up 축 TCP RTT** 고, `R-09b` 가 그 축을 본다.
    `USER_COUNTER_SERIES["rtt_up_time"] = ("transport","http")` 이므로 http
    레코드의 값도 `U-10`(sRTT)에 들어간다.

    down 축은 **유지한다** — 건드리면 `R-09d`(양 축 동시 열화)로 빠져
    `DATA_QUALITY_SUSPECT` 가 나온다.
    """
    if rec.series != "http":
        return False                        # transport 는 `PLAN_MUTATORS` 가 본다
    if not _domain_hit(rec, sc):
        return False
    if rng.random() >= intensity * 0.85:
        return False
    req = rec.num("RequestTime")
    if req > 0:
        rec.set("ResponseTime", f"{req + rng.uniform(0.2, 0.2 + 2.0 * intensity):.6f}")
    rec.scale("UplinkRTTTime", 1.0 + 3.5 * intensity)
    return True


@plan_mutator("external_path_latency")
def _external_path_latency_plan(plan, sc, intensity, rng, cell) -> bool:
    """세션 누적 up 축 RTT — 그 세션이 대상 도메인을 보고 있을 때만.

    **`_plan` 단계다**(규칙 U-1). `UplinkRTTTime` 은 세션 누적이라
    `_emit` 이 분마다 쪼개 쓰므로, 분 레코드에서 고치면 누적이 역전한다.
    """
    dom = sc.target.domain
    if dom and dom not in plan.text("ClientHelloDomain"):
        return False
    if rng.random() >= intensity * 0.85:
        return False
    plan.scale("UplinkRTTTime", 1.0 + 3.5 * intensity)
    return True


# --------------------------------------------------------------------------
# SC-07 · transport_loss — 전송망 패킷 손실
# --------------------------------------------------------------------------


def _down_loss(plan: Plan, rng: random.Random, intensity: float,
               lo: float, hi: float, k: float) -> None:
    """down 재전송률을 `clip(U(lo,hi) + k×I, 0, 0.30)` 로 올린다.

    **분모(Payload 패킷)를 건드리지 않는다** — 비율이 실제로 움직여야 탐지가
    성립하고, 분자·분모를 함께 키우면 `U-12` 가 꿈쩍하지 않는다.
    상한 30% 는 규칙 U-4(`Retrans ≤ Payload`)의 여유 있는 쪽 경계다.
    """
    pay = plan.get("Traffic_Total_Dnlink_PayloadPacketCount")
    rate = clip(rng.uniform(lo, hi) + k * intensity, 0.0, 0.30)
    plan.set("Traffic_Total_Dnlink_RetransPacketCount", int(pay * rate))


@plan_mutator("transport_loss")
def _transport_loss_plan(plan, sc, intensity, rng, cell) -> bool:
    """특정 지역 전송 구간의 Downlink 손실 — **C-Plane 은 건드리지 않는다**.

    up 축은 전부 유지한다(불변규칙 8 — up 은 Service/Backbone 축이다).
    `RstCount`·`AckTime` 도 그대로다 — 손실은 세션을 끊지 않는다.

    ⚠️ `OUT_STALL` 세션에는 추가 배율을 걸지 않는다(V3 §7.2). `classify()` 가
    이미 재전송을 8~20배로 올려 두었으므로 여기서 또 곱하면 두 번 곱해진다.
    """
    if cell.cell_key not in sc.active_cells_cache:
        return False
    if plan.outcome == "stall":
        return False
    if rng.random() >= intensity * 0.80:
        return False
    _down_loss(plan, rng, intensity, 0.03, 0.08, 0.12)
    plan.scale("DnlinkRTTTime", 1.0 + 2.8 * intensity)
    plan.scale("Traffic_Total_Dnlink_ThroughputTime", 1.0 + 1.9 * intensity)
    return True


# --------------------------------------------------------------------------
# SC-08 · ue_model — 특정 UE 기종 장애
# --------------------------------------------------------------------------


@mutator("ue_model")
def _ue_model(rec, sc, intensity, rng, cell) -> bool:
    """특정 기종이 **망이 보낸 베어러 활성화 요청을 처리하지 못해 거절**한다.

    `esm_error_Message = 195 ACTIVATE_DEFAULT_EPS_BEARER_CONTEXT_REJECT` 는
    **IN**(UE→MME)이고 **이 자리에서는 그것이 맞다** — 망이 `193`(OUT)으로
    요청하고 UE 가 `195`(IN)로 거절한다.

    Cause 는 `96`/`99`(프로토콜 오류 대역)다. `30`("Request rejected by
    Serving GW or PDN GW")·`31`("unspecified")은 **망측** 거절 사유라 UE 가
    보내는 거절에 적으면 방향과 의미가 어긋난다. 부수 효과로 `R-03`
    (`esm 26/30`)과 겹치지 않아 Top-1 이 `UE_MODEL_ISSUE` 로 깨끗하게 나온다.

    `success_flag` 를 함께 내리는 것이 **필수다** — `cell_agg.bake_ue` 에
    `DATA_SUC` 가 없어서 UE 축 탐지는 `C-02`(success/attempt) 하나뿐이다.
    """
    vm = sc.target.vendor_model
    if vm and (rec.get("Vendor"), rec.get("Model")) != vm:
        return False
    if rec.get("data_attempt_flag") != "1":
        return False
    if rng.random() >= intensity * 0.70:
        return False
    cause = rng.choice([96, 99])
    t = _start(rec) + rng.uniform(0.2, 0.5)
    _fail_connect(rec, interface=6, cause=cause, message=195, t=t)
    rec.set("esm_error_Cause", cause)
    rec.set("esm_error_Message", 195)
    rec.set("esm_error_Time", f"{t:.6f}")
    return True


# --------------------------------------------------------------------------
# SC-09 · mobility — 핸드오버 장애
# --------------------------------------------------------------------------


@mutator("mobility")
def _mobility(rec, sc, intensity, rng, cell) -> bool:
    """특정 Source→Target 셀 쌍에서 Path Switch 실패 (R-17).

    **`PathSwitchFailure_count ≤ PathSwitch_count` 를 지킨다.** 구 구현은 둘을
    독립으로 뽑아 실패가 시도를 넘을 수 있었고, 그러면 `PSF` 분모
    (`ps_cnt + psf_cnt`)와 분자가 어긋나 100% 넘는 실패율이 나온다.

    ⚠️ 절단은 **Last 셀에 계상된다**(`cell_agg._cell_specs` 의 `DROP_CNT`).
    `Last_*` 를 Target 셀로 바꾸면 절단이 Target 셀에 쌓이므로 ground truth 의
    `cells` 에 Source·Target 을 **모두** 넣어야 Incident 범위와 교집합이 잡힌다.
    """
    if cell.cell_key not in sc.active_cells_cache:
        return False
    if rng.random() >= intensity * 0.50:
        return False
    ps = rng.randint(1, 3)
    rec.set("PathSwitch_count", ps)
    rec.set("PathSwitchFailure_count", rng.randint(1, ps))
    if sc.target.cell_pairs and rng.random() < 0.80:
        _src, dst = rng.choice(sc.target.cell_pairs)
        enb, _, cid = dst.rpartition("-")
        rec.set("Last_eNB_ID", enb)
        rec.set("Last_Cell_ID", cid)
    if rng.random() < 0.45 and _connected(rec):
        # 연결은 됐다 — `success_flag` 를 **유지**하고(세우지 않는다)
        # First Error 도 건드리지 않는다
        t = _end(rec)
        rec.set("drop_flag", 1)
        rec.set("UEContextRelease_Cause", 106)  # Handover Failure In Target EPC/eNB
        rec.set("UEContextRelease_Time", f"{t:.6f}")
        _set_last(rec, 2, 106, 23, t)
    return True


# --------------------------------------------------------------------------
# SC-10 · mme_overload — MME 과부하
# --------------------------------------------------------------------------


@mutator("mme_overload")
def _mme_overload(rec, sc, intensity, rng, cell) -> bool:
    """MME 처리 용량 초과 — 전반 지연 + 신규 접속 일부 실패 (R-23).

    `s1ap_error_Message` 는 **`9 InitialContextSetup`(OUT)** 이다.
    `34 OverloadStart` 는 **non-UE-associated 절차**로 MME→eNB 과부하
    통보이고 특정 호에 매달리지 않는다 — 구 구현이 그것을 적고 있었다.
    `R-23` 은 "Release Cause 500 편중 **또는** S1AP 34" 로 OR 조건이라
    갈래 ② 의 `500` 으로 발동한다.

    지연은 **게이트를 통과하지 않은 호에도** 걸린다 — "실패하지 않은 호의
    지연이 평시 2배"가 과부하의 관측 신호다(⑱-2).
    """
    if not _node_eq(rec.get("MME_ID"), sc.target.mme_id):
        return False
    if cell.cell_key not in sc.active_cells_cache:
        return False
    rec.scale("initial_access_duration", 1.0 + 2.2 * intensity)
    rec.scale("initial_core_duration", 1.0 + 1.3 * intensity)
    if rng.random() >= intensity * 0.50:
        return False
    if rng.random() < 0.50 or not _connected(rec):
        # ① 접속 실패. **연결되지 않은 호는 반드시 이쪽이다** — 절단 갈래는
        #    `success_flag` 를 세울 수 없다(규칙 C-4 · `_connected` 주석)
        t = _start(rec) + rng.uniform(0.1, 0.6)
        _fail_connect(rec, interface=2, cause=500, message=9, t=t)
        rec.set("s1ap_error_Cause", 500)    # Control Processing Overload
        rec.set("s1ap_error_Message", 9)
        rec.set("s1ap_error_Time", f"{t:.6f}")
    else:
        # ② 연결 후 절단. Cause 500 은 `RAN_LOST_CAUSES` 에 **없으므로**
        #    `classify()` → `OUT_STALL` → down 재전송 급증 + `AckTime=0`.
        #    과부하가 U-Plane 품질로도 번진다 — 의도한 것이다
        t = _end(rec)
        rec.set("drop_flag", 1)
        rec.set("UEContextRelease_Cause", 500)
        rec.set("UEContextRelease_Time", f"{t:.6f}")
        _set_last(rec, 2, 500, 23, t)
    return True


# --------------------------------------------------------------------------
# SC-11 · change_suspected — 설정 변경 후 장애 (MME × APN)
# --------------------------------------------------------------------------


@mutator("change_suspected")
def _change_suspected(rec, sc, intensity, rng, cell) -> bool:
    """특정 MME 의 APN 라우팅 설정 오변경 — **MME × APN 조합**이다.

    구 구현은 5개 (iface, cause, msg) 조합을 균등 추첨했다. "어느 Cause 도
    지배하지 않는 혼합"이 변경 의심의 양상이라는 근거였는데, 그러면
    `first_error_interface_protocol` 이 다섯 구간에 흩어져 **R-00 의 진입점
    판정(70% 집중)이 서지 않고** EV-01·EV-03 이 아무것도 가리키지 못한다.

    V3 는 축을 바꿨다 — **같은 MME 의 다른 APN 은 정상이고 같은 APN 의 다른
    MME 도 정상**인 것이 "그 MME 에 건 설정"의 관측 사실이다. 그 두 비교군이
    SC-19(전 MME 의 그 APN)와 갈리는 지점이다.

    Cause 는 `30`("Request rejected by Serving GW or PDN GW")이다 — `27`/`93`
    은 APN 축 전용 카운터(`ESM27`·`S11_93`)를 건드려 SC-19 와 섞인다.
    """
    if not _node_eq(rec.get("MME_ID"), sc.target.mme_id):
        return False
    if sc.target.apn and rec.get("APN") != sc.target.apn:
        return False
    if rec.get("data_attempt_flag") != "1":
        return False
    if rng.random() >= intensity * 0.85:    # 설정 오류는 거의 전건이다
        return False
    t = _start(rec) + rng.uniform(0.2, 0.5)
    _fail_connect(rec, interface=6, cause=30, message=209, t=t)
    rec.set("esm_error_Cause", 30)
    rec.set("esm_error_Message", 209)       # PDN_CONNECTIVITY_REJECT (OUT)
    rec.set("esm_error_Time", f"{t:.6f}")
    return True


# --------------------------------------------------------------------------
# SC-12 · ims_pdn — IMS PDN 접속 실패
# --------------------------------------------------------------------------


@mutator("ims_pdn")
def _ims_pdn(rec, sc, intensity, rng, cell) -> bool:
    """VoLTE 전용 이슈 — `C-21` 만 악화하고 `C-20`(Data)은 정상이다 (R-30).

    ★ **First / Last Error 를 건드리지 않는다.** `success_flag=1` 인 호에
    First Error 를 심으면 `cell_agg` 가 `FE_RECOVERED`(`err AND
    SUCCESS_FLAG='1'`)로 계상해 **`R-24`(First Error 상승 + 회복률 높음)가
    유발된다.** "IMS 만 실패"는 호 수준 오류가 아니다.

    `esm_error_Cause` 는 남긴다 — `_causes` 가 First Error 와 독립으로 구간
    Cause 컬럼을 읽으므로 `EV-03.by_stage['esm']` 에 사실로 남는다.

    Cause 는 `26`(Insufficient resources)다. `33`("Requested service option
    not subscribed")은 **미가입**이라 자원 부족과 뜻이 다르다.
    """
    if rec.get("ims_attempt_flag") != "1":
        return False
    if not _node_eq(rec.get("ims_PGW_ID"), sc.target.pgw_id):
        return False
    if rng.random() >= intensity * 0.80:
        return False
    t = _start(rec) + rng.uniform(0.3, 0.8)
    rec.set("ims_success_flag", 0)
    rec.set("esm_error_Cause", 26)
    rec.set("esm_error_Message", 209)       # PDN_CONNECTIVITY_REJECT (OUT)
    rec.set("esm_error_Time", f"{t:.6f}")
    return True


# --------------------------------------------------------------------------
# SC-13 · nsa_nr_leg / SC-13b · nsa_policy_restrict
# --------------------------------------------------------------------------


@mutator("nsa_nr_leg")
def _nsa_nr_leg(rec, sc, intensity, rng, cell) -> bool:
    """NR 레그 미성립 — 커버리지·자원 부족. "5G인데 느리다"가 된다 (R-33).

    **`mme_restrict_dcnr` 를 건드리지 않는다.** 구 구현은 15% 확률로 `1` 을
    섞었는데, `R-33`("N-04 **정상**")과 `R-34`("N-04 **급증**")은 배타적
    조건이라 섞으면 두 룰이 동시에 후보가 되어 쌍 비교가 성립하지 않는다.
    정책 제한은 SC-13b 다.
    """
    if rec.get("ue_dcnr") != "1":
        return False
    if cell.cell_key not in sc.active_cells_cache:
        return False
    if rng.random() >= intensity * 0.95:
        return False
    rec.set("initial_nr_conn_time", "0.0")
    return True


@mutator("nsa_policy_restrict")
def _nsa_policy_restrict(rec, sc, intensity, rng, cell) -> bool:
    """MME 의 DCNR 정책 제한 — `N-02` 급락 + `N-04` 급증 (R-34).

    셀이 아니라 **MME 축**이다. 커버리지 문제라면 지리로 뭉치지만 정책은
    그 MME 를 경유하는 전국 단말에 걸린다 — `expect_geo=WIDE` 가 그 뜻이다.
    """
    if rec.get("ue_dcnr") != "1":
        return False
    if not _node_eq(rec.get("MME_ID"), sc.target.mme_id):
        return False
    if rng.random() >= intensity * 0.90:
        return False
    rec.set("initial_nr_conn_time", "0.0")
    rec.set("mme_restrict_dcnr", 1)
    return True


# --------------------------------------------------------------------------
# SC-16 · pgw_uplane_degradation — PGW U-Plane 품질 저하
# --------------------------------------------------------------------------


@plan_mutator("pgw_uplane_degradation")
def _pgw_uplane_degradation(plan, sc, intensity, rng, cell) -> bool:
    """특정 PGW 의 사용자평면 처리 용량 초과 — **C-Plane 은 정상이다**.

    그것이 SC-07 과 다른 축을 만든다. SC-07 은 CELL 축 CLUSTER 고 이쪽은
    NODE 축이다 — PGW 경유 셀이 전국에 흩어져 `U-12 CELL` 은 희석되어 뜨지
    않고, **신규 `U-12 NODE` 스펙**이 잡는다.

    `ThroughputTime` 을 **유지하고** `PayloadSize` 만 줄인다 — `U-15`
    (= bytes/time)가 실제로 떨어져야 한다. 둘을 함께 줄이면 처리율이 그대로다.
    up 축은 전부 유지한다 — 건드리면 `R-09d`(양 축 동시)로 빠진다.
    """
    if not _node_eq(cell.pgw_id, sc.target.pgw_id):
        return False
    if plan.outcome == "stall":
        return False                        # 겹침 방지 (V3 §7.2)
    if rng.random() >= intensity * 0.85:
        return False
    _down_loss(plan, rng, intensity, 0.02, 0.02, 0.10)
    plan.scale("DnlinkRTTTime", 1.0 + 2.0 * intensity)
    plan.scale("Traffic_Total_Dnlink_PayloadSize", max(0.2, 1.0 - 0.4 * intensity))
    return True


# --------------------------------------------------------------------------
# SC-17 · enb_s1_outage — eNB/S1 전송 단절 (무호)
# --------------------------------------------------------------------------


#: 전조·단절을 가르는 강도 경계. **mutator 와 suppressor 가 같은 `I` 를 받으므로
#: 경계를 코드에 박는다** — 두 체계가 같은 분에 함께 돌면 "레코드가 없는데
#: 전조 신호가 심긴" 모순이 생긴다.
#:
#:     I <  0.5   suppressor 는 억제하지 않고 mutator 가 전조를 심는다
#:     I >= 0.5   suppressor 가 억제하고 mutator 는 돌지 않는다
#:
#: `I = 0.0` 으로 두면 `covers()` 가 거짓이 되어 시나리오가 통째로 비활성이다
#: (`intensity(minute) > 0.0`) — 그래서 전조 구간이 `0.30` 이다.
OUTAGE_GATE = 0.5


@suppressor("enb_s1_outage")
def _enb_s1_outage_suppress(sc, cell, intensity, rng) -> bool:
    """True 면 이 셀의 호를 만들지 않는다 — 레코드가 **아예 없다**.

    `success_flag=0` 레코드를 만들지 않는 것이 요점이다. 관측되지 않은 호를
    가짜 실패 XDR 로 생성하면 "무호"와 "전부 실패"가 구분되지 않고, D4
    「트래픽 소멸」이 검증하려는 것이 바로 그 구분이다.
    """
    if sc.target.enb_id is None or cell.enb_id != sc.target.enb_id:
        return False
    if intensity < OUTAGE_GATE:
        return False                        # 전조·안정 구간 — mutator 가 담당한다
    return rng.random() < intensity


@mutator("enb_s1_outage")
def _enb_s1_outage(rec, sc, intensity, rng, cell) -> bool:
    """전조·안정 구간의 품질 저하 — 전송 자원 부족(S1AP 200)이 섞인다.

    단절 구간(`I >= 0.5`)에는 **돌지 않는다.** 그 분의 레코드는 suppressor 가
    막았고, 재추첨으로 다른 셀에 간 호는 대상 eNB 가 아니다.
    """
    if sc.target.enb_id is None or cell.enb_id != sc.target.enb_id:
        return False
    if intensity >= OUTAGE_GATE:
        return False
    rec.scale("initial_access_duration", 2.5)
    if rng.random() >= 0.20:
        return False
    t = _start(rec) + rng.uniform(0.05, 0.50)
    _fail_connect(rec, interface=2, cause=200, message=9, t=t)
    rec.set("s1ap_error_Cause", 200)        # Transport Resource Unavailable
    rec.set("s1ap_error_Message", 9)
    rec.set("s1ap_error_Time", f"{t:.6f}")
    return True


# --------------------------------------------------------------------------
# SC-18 · partial_recovery — 부분 장애와 자동 복구
# --------------------------------------------------------------------------


@mutator("partial_recovery")
def _partial_recovery(rec, sc, intensity, rng, cell) -> bool:
    """SGW 무응답이되 **실패율이 단계적으로 내려간다** (`STEPS` 80/50/20%).

    ★ **지연 지체가 이 시나리오의 핵심이다.** 실패율이 80%→20% 로 떨어진
    뒤에도 성공 호의 지연이 한동안 높게 남는 것이 "복구 중"을 RCA 가 읽을 수
    있는 유일한 신호다. `Scenario` 에 이력을 들고 있을 필요는 없다 —
    `intensity(minute - 5)` 로 계산한다.

    `I` 가 **그대로 실패율이다**(게이트 계수 1.0) — `steps` 가 0.8/0.5/0.2 를
    적고 있고, 거기에 계수를 또 곱하면 적어 둔 값과 실제가 어긋난다.
    """
    if not _node_eq(rec.get("SGW_ID"), sc.target.sgw_id):
        return False
    if rec.get("call_type") not in CT_SESSION_SETUP:
        return False
    wait = FAULT.timeout_wait_s11
    if rng.random() < intensity:
        t = _start(rec) + wait
        _fail_connect(rec, interface=3, cause=900, message=32, t=t)
        rec.set("s11_error_Cause", 900)
        rec.set("s11_error_Message", 32)
        rec.set("s11_error_Time", f"{t:.6f}")
        _timeout_core_duration(rec, rng, wait)
        return True
    # 성공 호 — 지연은 실패율보다 **늦게** 회복한다
    lag = max(intensity, sc.intensity(sc._minute - 5) * 0.6)
    rec.scale("initial_core_duration", 1.0 + 3.0 * lag)
    return True


# --------------------------------------------------------------------------
# SC-19 · apn_pdn_failure — APN 별 PDN 접속 장애
# --------------------------------------------------------------------------


@mutator("apn_pdn_failure")
def _apn_pdn_failure(rec, sc, intensity, rng, cell) -> bool:
    """특정 APN 의 가입정보·프로파일 오류 — **전 MME·전 SGW** 에 걸린다 (R-07).

    `27`(Unknown or missing APN)과 `93`(APN access denied - no subscription)이
    **APN 축에 전용 카운터를 가진 유일한 두 Cause 다**
    (`cell_agg.bake_apn`: `ESM27`·`S11_93`). 다른 Cause 를 쓰면 R-07 이
    발동하지 않는다.

    `success_flag` 를 함께 내리는 것이 필수다 — R-07 의 분모 계산
    (`attempt - success`)이 움직여야 "실패의 30% 이상이 이 APN" 이 성립한다.
    """
    if sc.target.apn and rec.get("APN") != sc.target.apn:
        return False
    if rec.get("data_attempt_flag") != "1":
        return False
    if rng.random() >= intensity * 0.75:
        return False
    if rng.random() < 0.60:
        # ① ESM 27 — `ESM27` 카운터
        t = _start(rec) + rng.uniform(0.2, 0.5)
        _fail_connect(rec, interface=6, cause=27, message=209, t=t)
        rec.set("esm_error_Cause", 27)
        rec.set("esm_error_Message", 209)
        rec.set("esm_error_Time", f"{t:.6f}")
    else:
        # ② S11 93 — `S11_93` 카운터. 거절 **응답을 받았다**(33 IN)
        t = _start(rec) + rec.num("initial_access_duration") / 1e6 + rng.uniform(0.01, 0.05)
        _fail_connect(rec, interface=3, cause=93, message=33, t=t)
        rec.set("s11_error_Cause", 93)
        rec.set("s11_error_Message", 33)
        rec.set("s11_error_Time", f"{t:.6f}")
    return True


# --------------------------------------------------------------------------
# SC-21 · compound_causal_failure — 복합 장애·2차 증상
# --------------------------------------------------------------------------


@mutator("compound_causal_failure")
def _compound_causal_failure(rec, sc, intensity, rng, cell) -> bool:
    """전송망 손실(1차) → 그 지역 DNS 악화(2차) → HTTP 요청량 감소(3차).

    **3차는 코드가 아니라 세션 모델이 만든다.** `_open()` 이 DNS 를 먼저 만들고
    mutate 를 건 뒤 `_dns_failed()` 면 `return` 하므로, 그 세션의 transport·http
    가 생기지 않는다 — DNS→HTTP 인과는 `SessionEngine` 이 이미 갖고 있고 세
    계열에 독립적으로 mutation 을 걸지 않는다.

    1차(transport)는 `PLAN_MUTATORS` 가, 2차(dns)는 여기가 한다. 시간차는
    `series_offset={"dns": 2}` 가 만든다.
    """
    if rec.series != "dns":
        return False                        # transport 는 `PLAN_MUTATORS` 가 본다
    if cell.cell_key not in sc.active_cells_cache:
        return False
    if rng.random() >= intensity * 0.70:
        return False
    # SC-05 와 같은 갈래이되 무응답이 우세하다 — 지역 전송 구간이 먼저
    # 나빠졌으므로 서버 오류보다 경로 쪽 증상이 많은 것이 인과에 맞다.
    # **비율만 다르고 표현은 `_dns_fail` 과 같아야 한다** — 무응답은
    # `ReplyCode = 900`(2026-10-22 운영 실측 교정, 그 함수 머리말 참조).
    if rng.random() < 0.60:
        rec.set("ReplyCode", 900)
        rec.set("ResponseTime", "0.0")
        rec.set("AnswerCount", 0)
        retry = rng.choices((1, 2, 3), weights=(79, 20, 1))[0]
    else:
        rec.set("ReplyCode", 2)
        rec.set("AnswerCount", 0)
        retry = 1 if rng.random() < 0.98 else 2
    rec.set("QueryRetryCount", retry)
    rec.set("dns_retry_sequence", retry)
    rec.set("dns_retry_interval", round(rng.uniform(1.0, 5.0), 3))
    return True


@plan_mutator("compound_causal_failure")
def _compound_causal_failure_plan(plan, sc, intensity, rng, cell) -> bool:
    """1차 — 전송망 손실. SC-07 과 같은 식이다."""
    if cell.cell_key not in sc.active_cells_cache:
        return False
    if plan.outcome == "stall":
        return False
    if rng.random() >= intensity * 0.80:
        return False
    _down_loss(plan, rng, intensity, 0.03, 0.03, 0.12)
    plan.scale("DnlinkRTTTime", 1.0 + 2.8 * intensity)
    plan.scale("Traffic_Total_Dnlink_ThroughputTime", 1.0 + 1.9 * intensity)
    return True


# --------------------------------------------------------------------------
# 시나리오 구성
# --------------------------------------------------------------------------


def _cells_in_one_sgg(cells: list[Cell], rng: random.Random,
                      want: int = 30) -> tuple[list[Cell], str]:
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


def _bearing(a: Cell, b: Cell) -> float:
    """a 에서 b 를 본 방위각(도, 0~360). 평면 근사로 충분하다 — 수십 km 범위다."""
    dy = b.latitude - a.latitude
    dx = (b.longitude - a.longitude) * math.cos(math.radians(a.latitude))
    return math.degrees(math.atan2(dx, dy)) % 360.0


def _bearing_diff(a: Cell, b: Cell, theta: float) -> float:
    """기준 방위 `theta` 와의 각 차이(±180)."""
    d = (_bearing(a, b) - theta + 180.0) % 360.0 - 180.0
    return d


def corridor_cells(cells: list[Cell], rng: random.Random,
                   want: int = 18, half_angle: float = 25.0
                   ) -> tuple[Cell, list[Cell]]:
    """앵커 + **방위각 ±25° 안**의 근접 셀 — `CORRIDOR` 를 만들기 위한 제약.

    `detector.geo_pattern()` 이 `CORRIDOR` 를 주려면 `elong > 3.2` AND
    `radius > 2.5km` AND 셀 ≥ 5 다. "GPS 최근접 N셀"은 앵커 주위 **등방
    분포**가 되어 `CLUSTER` 로 떨어지고(fit 1.0 → 0.8) `EV-11.pattern ==
    "CORRIDOR"` 통과 조건이 서지 않는다. 그래서 방위를 하나 뽑아 그 띠 안에서만
    고른다 — 선형 구조(철도·고속도로 연변 셀)의 실제 모양이기도 하다.

    띠가 너무 좁아 셀이 모자라면 각도를 두 번 넓힌다. **빈손으로 돌려주지
    않는다** — 대상이 없으면 시나리오가 통째로 비고, 그것은 "주입했는데 아무
    일도 없었다"로 나타나 원인을 찾기 어렵다.
    """
    from .geo_lite import haversine_km

    mapped = [c for c in cells if c.geo_status == "MAPPED"]
    if not mapped:
        return cells[0], []
    anchor = rng.choice(mapped)
    near = sorted(
        (c for c in mapped if c.cell_key != anchor.cell_key),
        key=lambda c: haversine_km(anchor.latitude, anchor.longitude,
                                   c.latitude, c.longitude),
    )
    theta = rng.uniform(0.0, 360.0)
    for angle in (half_angle, half_angle * 2, 180.0):
        band = [c for c in near if abs(_bearing_diff(anchor, c, theta)) <= angle]
        if len(band) >= 5:
            return anchor, band[:want]
    return anchor, near[:want]


def build_scenarios(
    cells: list[Cell],
    minutes: int,
    rng: random.Random,
    selected: str | list[str] = "all",
    lead_minutes: int | None = None,
) -> list[Scenario]:
    """22종 시나리오를 시뮬레이션 구간에 배치한다.

    구간이 짧으면 앞쪽부터 넣되 서로 겹치지 않게 간격을 둔다. 겹치면 Incident
    상관·압축(§5.5)이 두 원인을 하나로 묶어 채점이 흐려진다.

    kind 고유 속성은 `KIND_SPECS` 에서 가져온다 — 전에는 이 함수의 리터럴과
    `faults.KIND_SPECS` 두 벌이었고 한쪽만 고치면 **같은 kind 가 배치 생성과
    UI 주입에서 다른 정답을 갖는다**(에러 없이 채점만 어긋난다).
    """
    if not cells:
        return []
    by_sgw: dict[str, list[Cell]] = {}
    by_mme: dict[str, list[Cell]] = {}
    by_pgw: dict[str, list[Cell]] = {}
    by_enb: dict[str, list[Cell]] = {}
    for c in cells:
        by_sgw.setdefault(c.sgw_id, []).append(c)
        by_mme.setdefault(c.mme_id, []).append(c)
        by_pgw.setdefault(c.pgw_id, []).append(c)
        by_enb.setdefault(c.enb_id, []).append(c)
    busy_sgw = max(by_sgw, key=lambda k: len(by_sgw[k]))
    busy_mme = max(by_mme, key=lambda k: len(by_mme[k]))
    busy_pgw = max(by_pgw, key=lambda k: len(by_pgw[k]))
    busy_enb = max(by_enb, key=lambda k: len(by_enb[k]))
    # 두 번째로 큰 SGW — SC-18 이 SC-03 과 같은 노드를 쓰면 두 Incident 가
    # 같은 축에 쌓여 "부분 복구"의 계단이 다른 장애의 꼬리로 보인다
    sgw_rank = sorted(by_sgw, key=lambda k: -len(by_sgw[k]))
    other_sgw = sgw_rank[1] if len(sgw_rank) > 1 else busy_sgw
    mme_rank = sorted(by_mme, key=lambda k: -len(by_mme[k]))
    other_mme = mme_rank[1] if len(mme_rank) > 1 else busy_mme
    # SC-14(3대) · SC-15(5대) · SC-15b(3대) — **셋이 서로 다른 집합**이어야
    # 쌍 비교가 성립한다. SC-15b 는 SC-14 와 **대상만 다른** 같은 무응답이므로
    # (구분 불가 쌍의 조건) 집합이 겹치면 "대상이 달라서 구분됐다"가 되어
    # 검증하려던 것이 사라진다.
    mme_g3 = set(mme_rank[:3])
    mme_g5 = set(mme_rank[3:8]) or set(mme_rank[:5])
    mme_g3b = set(mme_rank[8:11]) or mme_g5 or mme_g3

    # 한 시군구 안의 셀을 "전부" 가져간다. 일부만 고르면 같은 시군구의 정상 셀이
    # 지리 롤업에서 열화를 희석해 SGG 단위 탐지가 성립하지 않는다.
    cluster_cells, cluster_sgg = _cells_in_one_sgg(cells, rng, 60)
    loss_cells, loss_sgg = _cells_in_one_sgg(cells, rng, 40)
    nsa_cells, nsa_sgg = _cells_in_one_sgg(cells, rng, 60)
    compound_cells, compound_sgg = _cells_in_one_sgg(cells, rng, 40)
    overload_cells = by_mme[busy_mme][:80]

    anchor, corridor = corridor_cells(cells, rng, want=18)
    pairs = [(anchor.cell_key, c.cell_key) for c in corridor[:8]]

    def spec(sid: str, kind: str, **over) -> tuple[str, dict]:
        d = dict(KIND_SPECS[kind])
        d["kind"] = kind
        d.update(over)
        return sid, d

    specs: list[tuple[str, dict]] = [
        spec("SC-01", "gw_resource",
             target=Target(cell_keys={c.cell_key for c in by_sgw[busy_sgw]},
                           sgw_id=busy_sgw)),
        spec("SC-02", "ran_radio",
             target=Target(cell_keys={c.cell_key for c in cluster_cells},
                           sgg_codes={cluster_sgg})),
        spec("SC-03", "gw_node_down",
             target=Target(cell_keys={c.cell_key for c in by_sgw[busy_sgw]},
                           sgw_id=busy_sgw)),
        spec("SC-03b", "gw_pgw_restart",
             target=Target(cell_keys={c.cell_key for c in by_pgw[busy_pgw]},
                           pgw_id=busy_pgw)),
        spec("SC-04", "auth_failure", target=Target()),
        spec("SC-05", "dns_degradation", target=Target()),
        # 도메인은 `generator._bind_scenario_targets` 가 프로파일에서 채운다
        spec("SC-06", "external_server", target=Target(domain="")),
        spec("SC-06b", "external_path_latency", target=Target(domain="")),
        spec("SC-07", "transport_loss",
             target=Target(cell_keys={c.cell_key for c in loss_cells},
                           sgg_codes={loss_sgg})),
        # 기종은 `_bind_scenario_targets` 가 가입자 풀에서 채운다
        spec("SC-08", "ue_model", target=Target(vendor_model=None)),
        spec("SC-09", "mobility",
             # ground truth 의 `cells` 에 Source·Target 을 **모두** 넣는다 —
             # 절단이 Last 셀(= Target)에 계상되므로 한쪽만 넣으면 Incident
             # 범위와 교집합이 반만 잡힌다
             target=Target(cell_keys={anchor.cell_key}
                           | {c.cell_key for c in corridor[:14]},
                           cell_pairs=pairs)),
        spec("SC-10", "mme_overload",
             target=Target(cell_keys={c.cell_key for c in overload_cells},
                           mme_id=busy_mme)),
        # APN 은 `_bind_scenario_targets` 가 상위 트래픽에서 채운다
        spec("SC-11", "change_suspected",
             target=Target(cell_keys={c.cell_key for c in by_mme[other_mme]},
                           mme_id=other_mme, apn=None)),
        spec("SC-12", "ims_pdn", target=Target()),
        spec("SC-13", "nsa_nr_leg",
             target=Target(cell_keys={c.cell_key for c in nsa_cells},
                           sgg_codes={nsa_sgg})),
        spec("SC-13b", "nsa_policy_restrict",
             target=Target(cell_keys={c.cell_key for c in by_mme[other_mme]},
                           mme_id=other_mme)),
        spec("SC-14", "auc_no_response",
             target=Target(cell_keys={c.cell_key for c in cells
                                      if c.mme_id in mme_g3},
                           mme_group=mme_g3)),
        spec("SC-15", "dra_route_failure",
             target=Target(cell_keys={c.cell_key for c in cells
                                      if c.mme_id in mme_g5},
                           mme_group=mme_g5)),
        spec("SC-15b", "s6a_undetermined",
             target=Target(cell_keys={c.cell_key for c in cells
                                      if c.mme_id in mme_g3b},
                           mme_group=mme_g3b)),
        spec("SC-16", "pgw_uplane_degradation",
             target=Target(cell_keys={c.cell_key for c in by_pgw[busy_pgw]},
                           pgw_id=busy_pgw)),
        spec("SC-17", "enb_s1_outage",
             target=Target(cell_keys={c.cell_key for c in by_enb[busy_enb]},
                           enb_id=busy_enb)),
        spec("SC-18", "partial_recovery",
             target=Target(cell_keys={c.cell_key for c in by_sgw[other_sgw]},
                           sgw_id=other_sgw)),
        spec("SC-19", "apn_pdn_failure", target=Target(apn=None)),
        spec("SC-20", "dns_flap", target=Target()),
        spec("SC-21", "compound_causal_failure",
             target=Target(cell_keys={c.cell_key for c in compound_cells},
                           sgg_codes={compound_sgg})),
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
    for i, (sid, s) in enumerate(specs):
        start = lead + i * span
        d = _duration_for(s, dur)
        if start + d >= minutes:
            break
        out.append(place(sid, s, start, d, rng))
    return out


def _duration_for(s: dict, dur: int) -> int:
    """파형이 지속시간을 정하는 시나리오가 있다.

    * `STEPS` — `steps` 의 마지막 경과분이 곧 지속시간이다. 짧게 자르면
      "80% → 50% → 20%" 의 뒷 구간이 사라져 계단이 보이지 않는다.
    * `series_offset` — ⚠️ **최대 오프셋을 더해 선언한다.** `covers()` 는
      오프셋 없는 `intensity(minute)` 를 보므로, 더하지 않으면 늦게 시작한
      계열이 끝에서 잘린다(V3 §SC-21).
    """
    steps = s.get("steps") or ()
    base = max(steps[-1][0], 8) if steps else dur
    off = max((s.get("series_offset") or {}).values(), default=0)
    return base + off


def place(sid: str, s: dict, start: int, duration: int,
          rng: random.Random) -> Scenario:
    """`KIND_SPECS` 항목 + 시간창 → `Scenario`. 배치·UI 주입이 함께 쓴다."""
    sc = Scenario(id=sid, start_minute=start, duration=duration, **s)
    sc.cell_order = sorted(sc.target.cell_keys)
    rng.shuffle(sc.cell_order)
    return sc


#: `partial_recovery` 가 "5분 전 강도"를 보려면 현재 분을 알아야 한다.
#: `_mutator` 가 분마다 세워 둔다 — mutator 서명에 분이 없고, 추가하면 22개
#: 전부와 `generator`·`pemdb1` 두 호출부를 함께 고쳐야 한다.
Scenario._minute = 0  # type: ignore[attr-defined]
