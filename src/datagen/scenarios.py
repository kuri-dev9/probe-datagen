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
import zlib
from dataclasses import dataclass, field
from typing import Callable

from . import measured
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
    #: **장애 대상 셀의 판정 하한** — 셀당 분당 호 수(강도 1 기준).
    #: 대상 셀에 사람이 적으면 모자란 만큼 그 셀에 호를 더 만든다
    #: (`PemdbGen._floor_drafts`). graphio 는 표본이 모자라면 5·15분 창으로
    #: 넓혀 판정하므로 셀당 8건이면 5분 창에서 40건 — 최소 표본(20)을 넘는다
    floor_per_cell: float = 8.0
    #: kind 별 하한 — 없는 kind 는 `floor_per_cell` 이다. 9번은 전환이 붙는 호가 절반 정도라
    #: 셀 5분 창의 PathSwitch 표본이 8호로는 최소 표본(20)에 못 미친다(2026-11-06 · 값은 yaml)
    floor_by_kind: dict[str, float] = field(default_factory=dict)
    #: 하한을 **5G 능력 단말로만** 채우는 kind(2026-11-06 · 16번). 평소 호가 얼마이든 그만큼 더 낸다.
    #: 판정이 SA 인원을 세는 시나리오라 일반 호로 채우면 SA 는 안 늘고 LTE 만 는다(F-153 · 대상 eNB 평소
    #: 15분 1명 → LTE 189명·SA 14명). 평소 호에서 옮겨 오므로 전국 호 수는 그대로이고(`_sa_floor_calls`),
    #: 옮기는 양은 `floor_by_kind` 가 정한다
    floor_sa_kinds: tuple[str, ...] = ()
    #: 하한을 **평소 호에서 옮겨 채우는** kind — 전국 호 수가 그대로다(`_moved_floor_calls`). 꼬리도 없다
    floor_moved_kinds: tuple[str, ...] = ()
    #: 옮기는 호의 상한 — 그 분 전국 호의 몫. 야간에 다른 셀을 비우지 않게 한다. **초기값**
    floor_moved_share: float = 0.2
    #: 자동 대상 노드 후보 — 가장 큰 노드 대비 셀 수 몫 하한. 노드 축 판정은 그 노드 안 비율이라 작은
    #: 노드도 표본이 찬다(가장 작은 SGW 도 분당 약 130호). **초기값**
    auto_node_min_share: float = 0.5
    #: 전국 축으로 판정하는 kind(`faults._WIDE_AXIS_KINDS`)의 같은 하한 — 신호가 노드 몫만큼 묽어진다
    #: (F-108 · 고른 SGW 몫에 따라 6.07%·4.84% 로 갈렸다). 지금까지 검증한 최대 노드에서 멀지 않게 둔다. **초기값**
    auto_wide_min_share: float = 0.75
    #: eNB 정지 자동 대상의 셀 수 하한과 평소 호율 하한(λ · 분당). D4 는 무호 `1 + ln(1/α)/λ` 분을
    #: 봐야 확정하는데 λ 1.5 면 약 14분이다 — 권장 길이 20분 안에 서야 한다. **초기값**
    outage_min_cells: int = 3
    outage_min_rate: float = 1.5
    #: 16번 자동 대상 후보 — 1km 메시의 평소 15분 VIDEO 인원 하한과 그 안 NR 가능 셀 수 하한. 평소 사람이
    #: 없는 칸은 걸어도 LTE 표본이 없고, 셀이 적으면 하한 호로 SA 를 채우지 못한다. **초기값**
    anchor_min_people: int = 40
    anchor_min_cells: int = 8
    #: 14번 자동 대상의 메시 수 — 후보 하나와 가장 가까운 칸들이고 `anchor_min_*` 를 후보 조건으로 같이
    #: 쓴다. 역 일대가 칸 하나로 끝나지 않고, 인접 칸이 함께 서야 CLUSTER 로 묶인다. **초기값**
    congestion_meshes: int = 4
    #: 17번 — 넘어온 단말의 호 빈도 배수(평소 1인당 빈도 기준). 폴백 직후에는 재등록과 밀린 전송으로
    #: 호가 평소보다 많다고 본다. 표본은 **넘어온 단말의 호로만** 늘린다 — 일반 호로 채우면 N-01(5G 단말
    #: 비중)이 묻힌다(F-115 · `_floor_drafts`). 전체 상한은 `SA_FALLBACK_CAP` 그대로다. **초기값**(실측 근거 없음)
    sa_fallback_call_boost: float = 2.0
    #: 하한을 거는 대상의 **최대 셀 수.** SGW·MME 전체 같은 장비 대상은 셀을
    #: 모으면 이미 표본이 충분하고, 셀당 하한을 걸면 호가 수천 건 는다
    floor_max_cells: int = 60
    #: 27번 폭주 호 중 발신 호(`DISASTER_EMERGENCY_CALL_TYPES`)의 Emergency 접속 비율. **초기값** —
    #: 현장 표본에 Emergency(0)가 없어 근거 값이 없다
    disaster_emergency_share: float = 0.02
    #: **하한 꼬리의 안전 상한(분)** (2026-11-02 · 카드 15 → 11-03 재설계).
    #: 꼬리는 크기로 끝난다(`floor_tail_eps`) — 이 값은 그보다 길게 남지 않게
    #: 하는 상한일 뿐이다. 11-02 판은 "셀당 1호를 40분 붙들다 한 번에 놓기"였고
    #: 놓는 순간 eNB 호율이 계단으로 꺾여 graphio D4 가 15분 호율(λ15)을 기억한
    #: 채 무호 구간을 소등으로 읽었다(11-03 12:00 이후 C-01 201건 · 16r4 꼬리).
    floor_tail_min: int = 120
    #: 꼬리의 반감기(분). D4 최근 창(15분)에 견줄 만해야 λ15 가 계단 없이
    #: 따라 내려온다 — 2분이면 너무 빨리 셀당 1호 근처에 닿는다. **초기값**
    floor_tail_half_min: float = 10.0
    #: 꼬리를 놓는 크기 — 셀당 분당 **평시 위 초과분**이 이 아래로 내려오면
    #: 놓는다. 놓는 순간의 계단이 이 값 이하다. **초기값**
    floor_tail_eps: float = 0.05
    #: **혼잡 중 down RTT 1회 평균** `(최소, 최대)` — `DnlinkRTTTime ÷
    #: DnlinkRTTCount` 단위(graphio 가 /1000 해 ms 로 본다). 평시는 1,000
    #: 이하를 정상으로 본다(사람 결정 2026-10-29) — 10만이면 100ms 로,
    #: graphio 체감 "대기 불량"(`xp.judge.rtt_ms: 150`) 언저리부터 넘는다.
    #: **곱하지 않는다** — 합계 값이라 평시에 곱하면 3ms 가 7ms 가 될 뿐이었다
    congestion_rtt: tuple[float, float] = (100_000.0, 400_000.0)
    #: 혼잡 활성 가입자 배율의 상한 — 확정대장 §3.0 의 "17–20시 1.8배"(24번 A). 14번은 호를 늘리지 않는다
    congestion_load: float = 1.8
    #: 14번에서 품질이 나빠지는 기존 가입자 비율(강도 1.0 일 때 · 2026-11-05 사용자 결정). **초기값**
    congestion_user_share: float = 0.6
    #: 23번에서 몰려서 무너진 가입자 비율(강도 1.0 일 때). 14번과 같은 몫이다. **초기값**
    crowd_user_share: float = 0.6
    #: 23번 자동 대상 eNB 의 셀 수 범위 — 하한은 셀마다 차므로 셀이 많으면 옮기는 호가 커진다. 초기값
    crowd_enb_cells: tuple[int, int] = (2, 3)
    #: 23번 혼잡 몫의 처리율 `(최소, 최대)` bps — 행사장은 100kbps 안팎이 정상이다(2026-11-06 사용자
    #: 결정). 셀 용량 ÷ 동시 가입자로 계산한 값이 아니다 — 셀 마스터에 용량이 없다. **초기값**
    crowd_up_bps: tuple[float, float] = (50_000.0, 150_000.0)
    crowd_dn_bps: tuple[float, float] = (100_000.0, 300_000.0)

    @classmethod
    def from_cfg(cls, cfg: dict | None) -> "FaultCfg":
        """`{"timeout_wait": {"s6a": 3.0, "s11": 5.0}}` 를 읽는다.

        `Shape.from_cfg` 와 같은 모양이다 — **모르는 키는 멈춘다.** 오타가
        "기본값으로 돌았다"로 끝나면 TIMEOUT 대기시간이 조용히 틀린 채
        `initial_core_duration` 의 하한(T-2)이 어긋난다.
        """
        f = cls()
        for k, v in (cfg or {}).items():
            if k == "congestion_rtt":
                lo, hi = (float(x) for x in v)
                if not 0 < lo <= hi:
                    raise RuntimeError(f"pemdb1.fault.congestion_rtt 는 0 < 최소 ≤ 최대: {v}")
                f.congestion_rtt = (lo, hi)
                continue
            if k == "crowd_enb_cells":
                lo, hi = (int(x) for x in v)
                if not 0 < lo <= hi:
                    raise RuntimeError(f"pemdb1.fault.crowd_enb_cells 는 0 < 최소 ≤ 최대: {v}")
                f.crowd_enb_cells = (lo, hi)
                continue
            if k in ("crowd_up_bps", "crowd_dn_bps"):
                lo, hi = (float(x) for x in v)
                if not 0 < lo <= hi:
                    raise RuntimeError(f"pemdb1.fault.{k} 는 0 < 최소 ≤ 최대: {v}")
                setattr(f, k, (lo, hi))
                continue
            if k in ("floor_per_cell", "floor_max_cells", "floor_tail_min",
                     "floor_tail_half_min", "floor_tail_eps", "disaster_emergency_share",
                     "congestion_load", "congestion_user_share", "crowd_user_share",
                     "anchor_min_people", "anchor_min_cells", "congestion_meshes",
                     "floor_moved_share", "auto_node_min_share", "auto_wide_min_share",
                     "outage_min_cells", "outage_min_rate"):
                setattr(f, k, type(getattr(f, k))(v))
                continue
            if k == "sa_fallback_call_boost":
                if float(v) < 1.0:
                    raise RuntimeError(f"pemdb1.fault.sa_fallback_call_boost 는 1 이상: {v}")
                f.sa_fallback_call_boost = float(v)
                continue
            if k == "floor_by_kind":
                for ik in (v or {}):
                    if ik not in KIND_SPECS:
                        raise RuntimeError(f"pemdb1.fault.floor_by_kind 의 모르는 kind: {ik}")
                f.floor_by_kind = {str(ik): float(iv) for ik, iv in (v or {}).items()}
                continue
            if k == "floor_sa_kinds":
                for ik in (v or ()):
                    if ik not in KIND_SPECS:
                        raise RuntimeError(f"pemdb1.fault.floor_sa_kinds 의 모르는 kind: {ik}")
                f.floor_sa_kinds = tuple(str(ik) for ik in (v or ()))
                continue
            if k == "floor_moved_kinds":
                for ik in v or ():
                    if ik not in KIND_SPECS:
                        raise RuntimeError(f"pemdb1.fault.floor_moved_kinds 의 모르는 kind: {ik}")
                f.floor_moved_kinds = tuple(str(ik) for ik in (v or ()))
                continue
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

    __slots__ = ("totals", "const", "outcome", "span", "stall", "ho_stall",
                 "reset", "syn_fail", "retries", "net_mode", "imsi")

    def __init__(self, totals: dict[str, float], const: dict[str, str],
                 outcome: str, span: int):
        self.totals = totals
        self.const = const
        #: `session.OUT_*`. **겹침 방지에 쓴다**(V3 §7.2) — `OUT_STALL` 은
        #: 이미 재전송을 8~20배로 올려 두었으므로 그 위에 장애 배율을 또
        #: 곱하면 두 번 곱해진다.
        self.outcome = outcome
        self.span = span
        #: **C-Plane 정상인 정체 요청**(확정대장 §4.1 G-3). 켜면 세션 한가운데
        #: 한 분이 down payload 0 으로 비고(`session._emit`) 그 세션에
        #: `DnlinkWindowCloseCount` 가 붙는다 — `Q-04`(bad_mask 16) 의 모양.
        #: `outcome` 을 `stall` 로 바꾸지 않는 이유: 그쪽은 `drop_flag` 가 선
        #: 호의 결말이라 `AckTime=0`(→ `U-01`)까지 따라온다.
        self.stall = False
        #: **핸드오버 직후 3–8초 정체** 요청(`j-sc-15`). 세션이 전환할 때
        #: 비로소 구간이 정해진다 — 전환이 없는 세션에는 아무 일도 없다.
        self.ho_stall = False
        #: **연결이 끊긴다**(RST) — 세션 도중 서버·망이 끊었다. TCP 만
        self.reset = False
        #: **연결이 서지 않는다** — SYN 재전송 끝에 ACK 없음(`U-01` 실패).
        #: 데이터가 흐르지 않고 1분 안에 끝난다. TCP 만
        self.syn_fail = False
        #: 앱이 **다시 시도하는 세션 수** — 사용자가 재시도·새로고침한다.
        #: 같은 호·같은 앱으로 새 세션이 열린다(`SessionEngine._open`)
        self.retries = 0
        #: 이 세션의 **망 모드** `LTE`·`SA`(`session.net_mode_of`). 읽기
        #: 전용이다 — 장애가 "SA 세션만" 고르는 데 쓴다(`j-sc-16`)
        self.net_mode = "LTE"
        #: 이 세션의 가입자. 읽기 전용이다 — 장애가 "같은 사람"을 고르는 데 쓴다(`j-sc-14`)
        self.imsi = ""

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
    #: 앱 하나 (`CM_APP_CODE.APP_IDX`) — `j-sc-21`. 세션의 `App_ID` 로 고른다
    app_id: str | None = None
    #: 앱 그룹 (`GROUP_CODE`) — `j-sc-22` 의 `PAY`. 정답·화면 표기용
    app_group: str | None = None
    #: 그 그룹 앱들의 도메인(`paypay.ne.jp`). **DNS 를 앱에 잇는 자리다**
    #: — DNS 레코드의 `App_*` 는 DNS 서버라 질의명으로만 앱을 안다
    app_hosts: tuple[str, ...] = ()
    #: 부하를 넘겨받는 **소등 eNB** — `j-sc-24`(`capacity_shift`). `enb_id`
    #: 가 부하를 내주는 쪽(A)이고 이것이 받는 쪽(B)이다. 평소에는 생성 대상이
    #: 아니라서 셀 목록(`cells`)에 없다 — `PemdbGen.standby` 가 들고 있다
    shift_to: str | None = None
    #: 자연재해(27번 · `natural_disaster`)의 기지국 정지 배치 — ``local``(지역 안 한 곳에 모인
    #: eNB) · ``wide``(지역 전체에 흩어진 eNB). 대상 문자열 앞머리 `LOCAL:`·`WIDE:` 로 받는다
    #: (`faults.build` · 장애 테이블에 인자 칸이 없어 DDL 없이 받는 길이다)
    outage_mode: str | None = None
    #: 15번(`ho_interruption`)이 고른 **노선 구간** — `route_segment` 가 만든 사전(노선 id·역·SGW 가
    #: 바뀌는 칸 수·모양). 노선을 못 쓴 대상은 `None` 이다. 정답지에 그대로 실린다
    route_seg: dict | None = None
    #: 25번(`son_param_change`)이 파라미터를 넣은 **eNB 1~2개**(2026-11-13 사용자 결정) — 그 eNB 의 전 셀이
    #: 출발 셀이다. `enb_id` 를 쓰지 않는다 — 그 칸은 eNB 정지(17번)가 읽어 다른 뜻이 섞인다
    son_enbs: tuple[str, ...] = ()
    #: 25번 출발 셀별 세기 배율(`son_focus` · `SON_GAIN_RANGE` × 평소 호 비율). 없는 셀은 1.0 이다
    son_gain: dict[str, float] = field(default_factory=dict)

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
        if self.son_enbs:
            bits.append("eNB-" + "·".join(self.son_enbs))
        if self.shift_to:
            bits.append(f"→ eNB-{self.shift_to}")
        if self.vendor_model:
            bits.append("/".join(self.vendor_model))
        if self.app_id:
            bits.append(f"APP-{self.app_id}")
        elif self.app_group:
            bits.append(f"APP {self.app_group}")
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
    #: 회복 시간(분). `None` 이면 `ramp_in` 과 같다 — 서서히 오른 것은 서서히
    #: 내린다(2026-11-03 사용자 결정 · 궤적 공통화). 전에는 `remain <= 2` 로
    #: **전 시나리오 회복이 2분 고정**이었다
    ramp_out: int | None = None
    #: 시작·끝 모양 — `"ramp"`(서서히) | `"instant"`(즉시). 노드 다운·서비스
    #: 불가·지진처럼 갑자기 나고 갑자기 걷히는 것에 `instant` 를 준다.
    #: STEPS·HOLD·FLAP 은 파형이 따로 있어 이 플래그를 보지 않는다
    onset: str = "ramp"
    offset: str = "ramp"
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
    #: 기대 룰이 **아직 배정되지 않았다**(확정대장 §2.3 · 일본 시나리오).
    #: `expect_rules` 가 비어 있는 것이 누락이 아니라는 표시다
    rule_pending: bool = False
    #: 주입 뒤 탐지까지 걸리는 최소 시간(분) — `recommended_duration` 이 본다. 0 이면 모양만 본다.
    #: 15분 창 판정은 창이 장애 안에 다 들어오고 다음 밀기까지 기다린다(창 15 + 밀기 5)
    detect_min: int = 0
    #: **정답 원인이 아닌 코드를 내지만 설 것으로 예상되는 룰** — `{룰: 그 룰의 원인 코드}`(2026-11-05
    #: 케이 잠정 결정). `expect_rules` 는 정답 원인 코드를 내는 룰만 둔다. 경쟁 후보·심각도 보조 룰은
    #: 여기다. graphio 채점은 둘 다 읽지 않는다 — 카드·사람 확인용이다
    expect_alternatives: dict[str, str] = field(default_factory=dict)
    #: **채점 문항인가.** `False` 면 장애가 아니라 재현이다(`j-sc-24` 증설
    #: 부하 이동 — 정답 원인이 `—`). `write_truth` 가 빼고, `truth_code` 가
    #: 비어 있는 것이 누락이 아니라는 표시다
    scored: bool = True

    # ── 시간 파형 ────────────────────────────────────────────────────────
    #: `STEPS` 용 `((경과분, 강도), ...)` — SC-17 · SC-18
    steps: tuple[tuple[int, float], ...] = ()
    #: `FLAP` 주기(분)와 저점 강도 — SC-20
    flap_period: int = 3
    flap_low: float = 0.0
    #: 계열별 시작 지연(분) — SC-21 의 "전송망 → 2분 뒤 DNS"
    series_offset: dict[str, int] = field(default_factory=dict)
    #: **출발 셀 하나당 도착 셀 수** — 25번(`son_param_change`)만 쓴다(2026-11-13 뜻을 바꿨다 · 전에는 셀 하나의
    #: 쌍 수였다). 0 이면 쌍 목록 전부에 흩는다(9·15번과 같은 옛 동작). 대상은 `son_focus`, 주입은 `_son_param_change`
    focus_pairs: int = 0
    #: **이 시간대에만 나오는 장애**의 시작 시간대 `((시작시, 끝시), ...)` — 끝시는 넣지 않는다(2026-11-09
    #: 사용자 결정). 15번(`ho_interruption`)은 전철 이동 세션이 몰리는 출퇴근에만 선다. 실측 시간대
    #: (`measured.hours`)보다 앞선다 — 실측은 "선 적이 있는 시각"이고 이것은 "여기서만 선다"는 모양이다.
    #: 주입을 막지 않는다 — 화면이 가상 시각 버튼(각 구간 가운데 정시)을 보인다. 비면 실측을 따른다
    rush_hours: tuple[tuple[int, int], ...] = ()
    #: **대상을 고를 때 맞출 모양** — 비면 `expect_geo` 다. 정답 모양과 고르는 모양이 다른 kind 만 적는다
    #: (2026-11-09 · 25번). 25번은 노선 구간(CORRIDOR)에서 이웃 관계를 고른 뒤 셀 1~3개로 좁혀
    #: (`son_focus`) 실제 장애는 점이다. 둘을 한 키로 두면 정답을 POINT 로 고치는 순간 `_route_target`
    #: 이 "구간 모양 ≠ POINT" 로 늘 후퇴해 대상 선택이 방위각 띠로 바뀐다
    select_geo: str | None = None

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
        if self.shape == "HOLD":
            # 창 전체를 같은 값으로 연다. **시간 모양은 kind 가 직접 만든다**
            # (`j-sc-24` 의 `shift_*`) — 국면이 셋(부하 상승 → 분산 → 분석)이라
            # 강도 하나로 접을 수 없고, 접으면 `covers()` 가 회복 국면에서 0 이
            # 되어 B 가 창이 끝나기 전에 꺼진다.
            return self.peak
        if self.shape == "FLAP":
            # 주기적 열화·회복. **회복 구간 감쇠를 걸지 않는다** — 톱니 모양이
            # 이 파형의 관측 신호이고(SC-20 ⑰) 끝에서 깎으면 마지막 주기가
            # 다른 모양이 된다.
            phase = (elapsed // max(1, self.flap_period)) % 2
            return max(0.0, (1.0 if phase == 0 else self.flap_low) * self.peak)
        # **상승 시간은 `ramp_in` 이 정한다 — `duration` 이 아니다**
        # (2026-10-27 교정). RAMP 만 `duration * 0.6` 을 쓰고 있었다. 그러면
        # 30분짜리를 주입했을 때 **18분 동안 거의 무증상**이다 — 실제로
        # 그 상태를 보고 "장애를 주입했는데 화면에 안 나온다"는 보고가 올라왔다
        # (10분 지나 누적 실패 0.7%). 지속시간과 악화속도는 **다른 축**이다.
        # 장비 하나가 죽는 사건은 오래 간다고 해서 천천히 나빠지지 않는다.
        #
        # 비대칭이기도 했다 — 회복은 아래 `remain <= 2` 로 **길이와 무관하게
        # 2분**인데 상승만 비례했다. `ramp_in` 필드는 이미 있었고 STEP 계열만
        # 쓰고 있었다(`ramp_in=1`·`2`). RAMP 는 기본값 3 을 쓴다.
        #
        # **짧은 장애는 램프를 더 줄인다.** `duration` 이 `ramp_in` 보다 짧으면
        # 피크에 닿기 전에 끝나, 강도를 1.0 으로 줘도 최대치가 나오지 않는다.
        #
        # **회복도 같은 결이다** (2026-11-03). `ramp_out` 분 동안
        # `remain/(ramp_out+1)` 로 내려온다 — `ramp_out=2` 면 옛 `remain/3` 과
        # 같은 값이다. 둘의 합이 `duration` 을 넘으면 같은 비율로 줄인다.
        r_in = 0 if self.onset == "instant" else max(1, self.ramp_in)
        r_out = 0 if self.offset == "instant" else max(
            1, self.ramp_in if self.ramp_out is None else self.ramp_out)
        room = max(1, self.duration - 1)
        if r_in + r_out > room:
            k = room / (r_in + r_out)
            r_in = max(1, int(r_in * k)) if r_in else 0
            r_out = max(1, int(r_out * k)) if r_out else 0
        base = 1.0 if r_in == 0 else min(1.0, (elapsed + 1) / r_in)
        if r_out and remain <= r_out:  # 회복 구간
            base *= remain / (r_out + 1.0)
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
            "expect_alternatives": dict(self.expect_alternatives),
            # **고른 셀로 잰 모양**(2026-11-11 사용자 결정 B) — 고정값은 kind 의 대표 모양이라 실제 대상과 어긋났다
            # (도쿄 셀은 반경 7~8km 라 WIDE(60km 초과)가 나올 수 없다 · 5·11·21·22번). `faults.build` 가 대상
            # 셀로 `geo_shape` 를 재 `_geo_shape` 에 둔다. 셀이 없거나 좌표가 없으면(APN·외부 경로) 고정값이다
            "expect_geo": self.__dict__.get("_geo_shape") or self.expect_geo,
            "expect_geo_static": self.expect_geo,
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
            "app_id": t.app_id,
            "app_group": t.app_group,
            "mme_group": sorted(t.mme_group),
            "enb_id": t.enb_id,
            **_disaster_truth(self),
            **_sa_fallback_truth(self),
            **_route_truth(self),
            **_son_truth(self),
        }


def _son_truth(sc) -> dict:
    """25번 정답지 — 파라미터를 넣은 eNB 와 셀별 역할·배율(2026-11-13). 화면 셀별 표와 대조하려는 값이고 채점은
    모르는 키를 무시한다. 출발 셀은 구조상 graphio 장애 셀이 아니다(절단은 도착 셀에 계상) — 그래서 따로 싣는다"""
    t = sc.target
    if sc.kind != "son_param_change" or not t.son_enbs:
        return {}
    src = sorted({a for a, _ in t.cell_pairs} | {k for k in t.son_gain})
    dst = sorted({b for _, b in t.cell_pairs} - set(src))
    return {
        "son_enbs": list(t.son_enbs),
        "son_cells": ([{"cell": k, "role": "src", "gain": t.son_gain.get(k)} for k in src]
                      + [{"cell": k, "role": "dst", "gain": None} for k in dst]),
        "son_pairs": [list(p) for p in t.cell_pairs],
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
    # **기대 룰은 R-05·R-21 이다**(2026-11-05 · F-117 · 맥스 대조). R-02(세션 거절)와 R-15(Core 노드
    # 집중 → NF_UNREACHABLE)는 Cause 73 이 특정 GW 에 몰리면 **R-05 에 양보하도록 설계돼 있다**
    # (graphio `rules._r02` 의 GW 편중 분기 · `_r15` 의 `_gw_exhaustion`) — 이 장애에서 서지 않는 것이
    # 맞다. R-02 는 메시지 33(응답)도 요구하는데 이 장애의 First 는 32(요청)다(`callflow` session_resource).
    # R-18(확산)은 원인이 아니라 심각도 보조 룰(UNDETERMINED)이라 기대에 넣지 않는다 — RAMP 로 대상 셀이
    # 늘면 설 수 있다
    "gw_resource": dict(
        title="SGW 세션 자원 소진",
        truth_code="GW_RESOURCE_EXHAUSTION", truth_category="CORE_UP",
        # R-21(소통률만 악화)은 섰지만 SESSION_REJECT 를 낸다 — 정답 근거가 아니라 증상 쪽 후보로 옮긴다
        expect_rules=("R-05",), expect_geo="SCATTER",
        expect_alternatives={"R-21": "SESSION_REJECT", "R-18": "UNDETERMINED",
                             "R-00": "UNDETERMINED"},
        shape="RAMP", scope_kind="CELL", expect_kpi="C-02",
    ),
    # **기대 룰은 R-06·R-22 다**(2026-11-05 · F-118 · 맥스 대조). R-16 은 무선 룰이 아니라 전송 손실 룰
    # (`TRANSPORT_LOSS` · down 재전송 EV-08 필수)이고 이 장애는 U-Plane 을 건드리지 않는다. R-20 은
    # "소통률 정상 + 절단만 급증" 장면용인데 이 장애는 접속 실패와 절단을 함께 낸다(`_ran_radio` 세 갈래)
    "ran_radio": dict(
        title="특정 지역 무선 구간 열화",
        truth_code="RAN_RADIO_QUALITY", truth_category="RAN",
        expect_rules=("R-06", "R-22"), expect_geo="CLUSTER",
        shape="STEP", scope_kind="CELL", expect_kpi="C-02",
    ),
    "gw_node_down": dict(
        title="SGW 노드 무응답",
        # 규격의 R-04("F-08이 S11에 집중 + SGW/PGW 편중")가 지목하는 코드는
        # GW_NODE_DOWN 이다. NF_UNREACHABLE 은 구간 미특정 무응답에 쓴다.
        truth_code="GW_NODE_DOWN", truth_category="CORE_UP",
        # R-00b·R-15 는 NF_UNREACHABLE 을 낸다 — 정답(GW_NODE_DOWN)의 경쟁 후보다(R-00b 는 R-04 에 양보)
        expect_rules=("R-04",), expect_geo="SCATTER",
        expect_alternatives={"R-00b": "NF_UNREACHABLE", "R-15": "NF_UNREACHABLE"},
        shape="STEP", ramp_in=2, scope_kind="CELL", expect_kpi="C-02",
        # 노드 다운은 갑자기 나고 갑자기 걷힌다(2026-11-03 궤적 공통화)
        onset="instant", offset="instant",
    ),
    "gw_pgw_restart": dict(
        title="PGW 재기동 통보",
        truth_code="GW_NODE_DOWN", truth_category="CORE_UP",
        # R-04 는 S11 **무응답**이 조건인데 이 장애는 Cause 72 **응답 거절**이라 서지 않는다. R-15 는
        # NF_UNREACHABLE 을 내는 경쟁 후보다
        expect_rules=("R-05b",), expect_geo="SCATTER",
        expect_alternatives={"R-15": "NF_UNREACHABLE"},
        # 재기동은 순간이다 — 계단이 1분이고 지속이 짧다
        shape="STEP", ramp_in=1, scope_kind="CELL", expect_kpi="C-02",
        onset="instant", offset="instant",
    ),
    "auth_failure": dict(
        title="S6a 인증 거절 (응답 수신형)",
        truth_code="AUTH_FAILURE", truth_category="CORE_CP",
        expect_rules=("R-01",), expect_geo="SCATTER",
        shape="STEP", scope_kind="GLOBAL", expect_kpi="P-01",
        onset="instant", offset="instant",
    ),
    "dns_degradation": dict(
        title="DNS 응답 열화",
        truth_code="DNS_DEGRADATION", truth_category="EXTERNAL",
        expect_rules=("R-14",), expect_geo="WIDE",
        shape="RAMP", series=("dns",), scope_kind="SERVICE", expect_kpi="U-29",
    ),
    # **기대 룰은 R-14b 하나다**(2026-11-05 · F-116 · 동구 조사). 이 장애는 HTTP 응답만 5xx 로 바꾸고
    # TCP 계층(재전송·sRTT)은 건드리지 않는다(`_external_server`) — 서버가 5xx 를 빨리 돌려주면 경로
    # 지연이 생기지 않으므로 EV-08 판정이 "U-Plane 정상"이고 R-09b(up 축 급증 + down 정상 + 5xx 편중)가
    # 서지 않는 것이 맞다. R-09b 는 서버·경로가 느려져 TCP 지연까지 생기는 06b(`external_path_latency`) 몫이다
    "external_server": dict(
        title="외부 서비스 5xx 급증",
        truth_code="EXTERNAL_SERVER", truth_category="EXTERNAL",
        expect_rules=("R-14b",), expect_geo="WIDE",
        shape="STEP", series=("http",), scope_kind="SERVICE", expect_kpi="U-25",
        onset="instant", offset="instant",
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
        # R-18(확산)은 심각도 보조 룰, R-15 는 NF_UNREACHABLE 을 내는 경쟁 후보다
        expect_rules=("R-23",), expect_geo="SCATTER",
        expect_alternatives={"R-18": "UNDETERMINED", "R-15": "NF_UNREACHABLE"},
        shape="RAMP", spreading=True, scope_kind="CELL", expect_kpi="C-02",
    ),
    "change_suspected": dict(
        title="계단형 급락 (변경 의심)",
        truth_code="CHANGE_SUSPECTED", truth_category="CHANGE",
        # 경쟁 후보는 R-21 이다(11-05 F-125 실측 — 소통률만 떨어져 SESSION_REJECT 가 선다).
        # R-03 은 서지 않는다: 첫 오류가 S11 이라 ESM 30 은 하류 증상이다(graphio 불변규칙 7)
        expect_rules=("R-11",), expect_geo="WIDE",
        expect_alternatives={"R-21": "SESSION_REJECT"},
        # XDR 만으로 "설정 변경"을 확정할 수 없다 — `R-11` 의
        # `confidence_cap=0.65` 가 그 뜻이고 Top-1 이 R-21 로 갈릴 수 있다.
        shape="STEP", ramp_in=1, scope_kind="CELL", expect_kpi="C-02",
        onset="instant", offset="instant",
        grade="TOP3",
    ),
    "ims_pdn": dict(
        title="VoLTE(ims PDN) 전용 접속 실패",
        truth_code="IMS_PDN_ISSUE", truth_category="IMS",
        expect_rules=("R-30",), expect_geo="SCATTER",
        shape="STEP", scope_kind="GLOBAL", expect_kpi="C-21",
        onset="instant", offset="instant",
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
        onset="instant", offset="instant",
    ),
    # ── 신규 8종 ──────────────────────────────────────────────────────────
    "auc_no_response": dict(
        title="S6a 무응답 (TIMEOUT)",
        truth_code="NF_UNREACHABLE", truth_category="CORE_CP",
        # R-01 은 S6a 첫 오류가 대부분 TIMEOUT 이면 R-00b 에 양보하도록 돼 있다(graphio `_r01` · SC-14
        # 실측) — 무응답 장애라 서지 않는다. R-00b·R-15 는 정답과 같은 NF_UNREACHABLE 이다
        expect_rules=("R-00b", "R-15"), expect_geo="SCATTER",
        shape="RAMP", scope_kind="GLOBAL", expect_kpi="P-01",
        onset="instant", offset="instant",
    ),
    "dra_route_failure": dict(
        title="S6a 라우팅 계층 이상 (Diameter 3xxx)",
        truth_code="AUTH_FAILURE", truth_category="CORE_CP",
        expect_rules=("R-01",), expect_geo="SCATTER",
        # **계층까지만 맞히면 정답이다.** 장비를 맞히라고 요구하지 않는다.
        shape="STEP", ramp_in=2, scope_kind="GLOBAL", expect_kpi="P-01",
        onset="instant", offset="instant",
        grade="LAYER",
    ),
    "s6a_undetermined": dict(
        title="S6a 무응답 (장비 미특정)",
        truth_code="NF_UNREACHABLE", truth_category="CORE_CP",
        # R-01 은 무응답이면 R-00b 에 양보한다(위 `auc_no_response` 와 같다)
        expect_rules=("R-00b",), expect_geo="SCATTER",
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
    # `j-sc-16` — 일본 요구 「アンテナは立つが遅い」 (2026-10-28).
    #
    # **`expect_kpi` 가 `U-15` 다.** 그 탐지 스펙은 graphio 쪽에 넣었지만
    # **임계가 아직 비어 있어 돌지 않는다** — 재생성 후 평시 분포를 보고
    # 정한다(확정대장 OPEN-41). 그때까지 이 시나리오는 주입은 되고
    # Incident 는 열리지 않는다.
    "anchor_saturation": dict(
        title="5G 체감 품질 저하 (앵커 경합)",
        truth_code="SA_QUALITY_LOW", truth_category="5G",
        # POINT — 메시 한 칸에서 대상을 뽑으면 셀 반경이 0.9km 미만이다(F-156 · 0.61km)
        expect_rules=("R-39",), expect_geo="POINT",
        shape="RAMP", series=("transport",),
        scope_kind="CELL", expect_kpi="U-15",
        # F-137 이 주입 25분 뒤에 섰다 — 15분 창에 SA 인원이 차는 시간까지다. 초기값
        detect_min=25,
    ),
    # 일본 시연 `j-sc-25` — **`change_suspected` 의 정답에 `mobility` 의 주입을
    # 얹는다**(확정대장 §12.4.1). SON 이 바꾸는 것은 H/O 파라미터라 `C-04`
    # 를 숫자로 꺾지 않고 **셀 쌍 전환 실패**를 넣어 관측으로 꺾이게 한다.
    # XDR 만으로는 `R-11` 이 `confidence_cap` 0.65 라 `R-17`(0.75)에 Top-1 을
    # 내준다 — `change_suspected` 와 같은 이유로 `TOP3` 다.
    "son_param_change": dict(
        title="기지국 설정 변경 후 계단형 저하 (SON)",
        truth_code="CHANGE_SUSPECTED", truth_category="CHANGE",
        # R-17 은 셀 쌍 전환 실패로 서지만 RAN_MOBILITY_ISSUE 를 낸다(11-04 25r4 에서 1위였다)
        # **대상은 eNB 1~2개의 전 셀이다**(2026-11-13 사용자 결정 · 전에는 노선 구간의 셀 하나). 파라미터는 eNB 에
        # 들어가는데 셀 하나만 영향받는 것은 실제 모양이 아니다. 정답 모양은 고른 셀로 잰다(`faults.build` 의
        # `geo_shape` · 사용자 결정 B) — eNB 하나는 한 사이트라 대개 POINT, 둘이면 잰 값을 따른다. 여기 POINT 는
        # 셀을 못 잴 때의 고정값이다. 노선 구간에서 고르지 않으므로 `select_geo` 를 두지 않는다
        expect_rules=("R-11",), expect_geo="POINT",
        expect_alternatives={"R-17": "RAN_MOBILITY_ISSUE"},
        shape="STEP", ramp_in=1, scope_kind="CELL", expect_kpi="C-04",
        onset="instant", offset="instant",
        grade="TOP3",
        # **출발 셀마다 도착 셀 1~2곳**(2026-11-13 · 거리순 · 이웃 관계 표가 없다). SON 은 이웃 관계마다
        # 핸드오버 파라미터를 바꾼다 — 잘못 바꾸면 그 셀에서 그 이웃으로 가는 전환만 실패한다(11-09). 그
        # 지문을 출발 셀마다 지킨다. 셀별 세기는 `SON_GAIN_RANGE`·`SON_LOAD_FLOOR`. **초기값이다**
        focus_pairs=2,
    ),
    # 일본 시연 `j-sc-14` — 도심 역 저녁 러시. **원인 코드와 룰은 graphio
    # 에 아직 없다**(확정대장 §4.2 A-4 · 룰 번호 미정). gen 은 XDR 과 정답만
    # 적는다 — 그래서 `expect_rules` 가 비어 있다. 2단계(혼잡 룰) 전까지 graphio 는 판단 보류로 낸다.
    # 호는 늘리지 않고 기존 가입자의 일부가 나빠진다(2026-11-05 사용자 결정 · 스펙의 1.8배와 다르다).
    # 탐지 지표는 Q-11(RTT 초과 가입자 비율)이고 메시·정목 단위로 서서 인접 칸이 묶인다(`CLUSTER`)
    "ran_congestion": dict(
        title="무선 용량 혼잡 (기존 가입자 품질 저하)",
        truth_code="RAN_CAPACITY_CONGESTION", truth_category="RAN",
        expect_rules=("R-47",), expect_geo="CLUSTER",
        shape="RAMP", series=("transport", "http"),
        scope_kind="CELL", expect_kpi="Q-11",
        detect_min=15,          # Q-11 은 5분 창 3칸 연속 — F-136 이 주입 13분 뒤에 섰다. 초기값
    ),
    # 일본 시연 `j-sc-15` — 야마노테선 전환 정체. 원인 코드·`Q-08`·
    # `CELL_PAIR` 축·룰이 전부 graphio 에 아직 없다(확정대장 §7.3 NEW 4).
    "ho_interruption": dict(
        title="전환 직후 정체 (코리도 4G↔5G)",
        truth_code="RAN_HO_INTERRUPTION", truth_category="RAN",
        expect_rules=("R-40",), expect_geo="CORRIDOR",
        shape="RAMP", series=("lte_call", "transport"),
        scope_kind="CELL", expect_kpi="Q-08",
        # 출퇴근에만 나온다 — 노선 셀에 이동 세션이 몰려야 전환이 잦다. 낮·밤에는 표본이 모자라
        # 서지 않는다(2026-11-09 사용자: "지금 시간대나 일반 시간에는 안 나올 아이"). **초기값이다**
        rush_hours=((7, 9), (17, 19)),
    ),
    # 일본 시연 `j-sc-27` — 지진. 정지는 `g-sc-17` 과 같은 원인(R-36)이고
    # 다른 점은 **정지 셀의 가입자가 이웃 셀로 옮겨 붙는다**는 것이다.
    # 파형은 갑자기 선다 — 지진에 전조는 없다. 15분 뒤 복구가 시작된다.
    "earthquake": dict(
        title="국지 다수 셀 정지 + 주변 부하 유입 (지진)",
        truth_code="RAN_NODE_DOWN", truth_category="RAN",
        expect_rules=("R-36",), expect_geo="CLUSTER",
        shape="STEPS", steps=((15, 1.00), (18, 0.70), (20, 0.40)),
        onset="instant", offset="instant",
        series=("lte_call", "transport"),
        scope_kind="CELL", expect_kpi="C-01",
    ),
    # **지진 — 통화 폭주 + 기지국 정지 + 문자 실패**(2026-11-14 개명 · 옛 이름 "자연재해 — 호 폭주 + 기지국 장애" · 2026-11-04 사용자 결정 · CM_ERROR_SCENARIO 27 이 이것을
    # 가리킨다). 지진(`earthquake`)을 대체한다 — 실제 재난에서 크게 보이는 것은 장비 손상이 아니라
    # 경보 뒤 **호 폭주**이고, 정지는 정전·전송로로 뒤따른다(총무성 백서 H23 · 포항 2017).
    # 상위 장비(SGW·PGW·MME)는 묶지 않는다. 국면은 kind 가 직접 만든다(`disaster_phase`) —
    # 강도 하나로 접을 수 없어 `HOLD` 다(capacity_shift 와 같다). 정답 원인은 **세 신호를 합친 판정**
    # (호 폭주 + 몇몇 eNB 무호 + Emergency 접속 증가 · 2026-11-05 사용자 결정)이고, 프로브는 재난 종류를
    # 모르므로 지진이라 단정하지 않는다(graphio R-46). 재해는 망 밖 원인이라 `EXTERNAL` 이다
    "natural_disaster": dict(
        title="지진 — 통화 폭주 + 기지국 정지 + 문자 실패",
        truth_code="NATURAL_DISASTER_SUSPECTED", truth_category="EXTERNAL",
        expect_rules=("R-46",), expect_geo="CLUSTER",
        expect_alternatives={"R-36": "RAN_NODE_DOWN"},
        shape="HOLD", onset="instant", offset="instant",
        series=("lte_call", "transport"),
        scope_kind="CELL", expect_kpi="C-01",
    ),
    # **SA→LTE 폴백 급증**(17번 · 2026-11-05 사용자 결정 "대안"). 평소 SA 에 상주해 EPC 데이터에
    # 보이지 않던 5G 단말이 5G 쪽(gNB·5GC) 장애로 한 지역에서 LTE 로 넘어와 **처음 나타난다**.
    # "SA" 의 뜻(`net_mode_of` · NR 연결 = SA)은 바꾸지 않는다 — 넘어온 단말은 NR 레그가 없어 LTE 다.
    # 규격서에 시스템 간 핸드오버 유형·gNB 대상 ID·5GS 유래 표시가 없어(리오 조사 11-05) 증거는
    # **5G 능력 단말 수(N-01)와 Attach·TAU 의 급증**뿐이다. 원인 코드는 결정 대기(`SA_FALLBACK_SUSPECTED`
    # 제안 · graphio Taxonomy 에 없다) — 룰이 없으니 `rule_pending`
    "sa_fallback": dict(
        title="SA→LTE 폴백 급증 (5G 측 장애 추정)",
        truth_code="SA_FALLBACK_SUSPECTED", truth_category="5G",
        expect_rules=("R-48",), expect_geo="CLUSTER",
        shape="HOLD", onset="instant", offset="instant",
        series=("lte_call", "transport"),
        scope_kind="CELL", expect_kpi="N-01",
        detect_min=20,          # N-01 은 15분 창 — 창 15 + 밀기 5. 초기값
    ),
    # 일본 시연 `j-sc-21` — CDN 서버측 열화. **지역 문제가 아니다**가 메시지
    # 이고(투자 ❌), 그 판정에 필요한 앱 축·`Q-06` 이 graphio 에 없다(§12.6 X-3·4).
    "cdn_degradation": dict(
        title="동영상 CDN 서버측 열화",
        truth_code="EXTERNAL_SERVER", truth_category="EXTERNAL",
        expect_rules=(), rule_pending=True, expect_geo="WIDE",
        shape="RAMP", series=("transport", "http"),
        scope_kind="SERVICE", expect_kpi="U-12",
    ),
    # 일본 시연 `j-sc-22` — 결제 앱 DNS 지연. `Q-07` 등급별 산식이 없다(X-4).
    "pay_dns_delay": dict(
        title="결제 앱 응답 지연 (DNS 기인)",
        truth_code="DNS_DEGRADATION", truth_category="EXTERNAL",
        expect_rules=(), rule_pending=True, expect_geo="WIDE",
        shape="RAMP", series=("dns", "transport", "http"),
        scope_kind="SERVICE", expect_kpi="U-32",
    ),
    # 일본 시연 `j-sc-23` — 대형 이벤트. 원인 둘(`EVENT_CROWD` 주 +
    # `UPLINK_RADIO_LIMIT` 부) 중 **주 원인만** 적는다(OPEN-35 안 A).
    # 파형은 25분 — 모이는 3분 · 공연 20분 · 흩어지는 2분. 길이가 판정이 아니라 호가 조건을 채운다
    # (2026-11-07 사용자 결정 · 전에는 2시간). 대상이 eNB 하나라 지리는 POINT 다.
    # 정답 룰은 graphio `R-41`(2026-11-03 · 카드 23). 그 룰의 CLUSTER(⑤)·up 부
    # 근거(⑥)가 14번 `ran_congestion`(셀 하나 혼잡 · 룰 미배정)과의 경계다.
    "event_crowd": dict(
        title="대형 이벤트 군중 혼잡 + 업링크",
        truth_code="EVENT_CROWD", truth_category="RAN",
        expect_rules=("R-41",), expect_geo="POINT",
        shape="STEPS", steps=((3, 0.50), (23, 1.00), (25, 0.40)),
        series=("transport",),
        scope_kind="CELL", expect_kpi="Q-09",
        detect_min=20,          # Q-09 한 표는 15분 창 — 창 15 + 밀기 5. 초기값
    ),
    # 일본 시연 `j-sc-24` — 기지국 증설 후 부하 이동. **장애가 아니다** —
    # 스펙 §8.3 투자 효과 검증이고 정답 원인이 `—` 다. 그래서 `scored=False`
    # 이고 `write_truth` 가 채점 대상에서 뺀다. 배치 생성(`build_scenarios`)
    # 에도 없다 — 소등 eNB 가 설정돼 있어야 성립한다. 1~10분의 A 혼잡은
    # 실제로 탐지되지만(14번과 같은 식), 그것을 맞히는 것이 이 시나리오의
    # 목적이 아니다. 시간 모양은 `shift_*` 가 만든다(`HOLD`).
    "capacity_shift": dict(
        title="기지국 증설 후 부하 이동",
        truth_code="", truth_category="", scored=False,
        expect_rules=(), rule_pending=True, expect_geo="POINT",
        shape="HOLD", series=("transport", "http"),
        scope_kind="CELL", expect_kpi="Q-11",
    ),
    "enb_s1_outage": dict(
        title="eNB 전송 단절 (무호)",
        truth_code="RAN_NODE_DOWN", truth_category="RAN",
        expect_rules=("R-36",), expect_geo="POINT",
        # 첫 2분 0.30 은 램프가 아니라 **전조**(S1AP 200 절단 · `OUTAGE_GATE`
        # 아래)다. 단절(≥ 0.5)은 2분째에 즉시 선다 — 즉시 시작을 이유로 지우면
        # 이 시나리오의 전조 신호가 사라진다(2026-11-03 맥스 판단)
        shape="STEPS", steps=((2, 0.30), (17, 1.00), (19, 0.70), (20, 0.40)),
        onset="instant", offset="instant",
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
        # 2026-11-04 사용자 결정 — graphio 가 `SUBSCRIPTION_ERROR`(HSS 가입자 조회 실패 · R-01b)와
        # `APN_CONFIG_ERROR`(한 APN 의 정의·프로파일 오류 · R-07)를 나눴다. 이 장애는 후자다
        truth_code="APN_CONFIG_ERROR", truth_category="CORE_CP",
        expect_rules=("R-07",), expect_geo="WIDE",
        shape="STEP", ramp_in=2, scope_kind="CELL", expect_kpi="C-02",
        onset="instant", offset="instant",
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

#: 셀 **가중 배율** — 그 분에 대상 셀로 호가 더 많이 배정된다(확정대장 §3.3).
#: suppressor 의 대칭이다. 전국 호 수(`_volume`)는 그대로이고 어디로 가는지만
#: 바뀐다 — 레코드 복제가 아니라 배정이므로 고유 `IMSI` 가 함께 는다.
Booster = Callable[[Scenario, float], float]
BOOSTERS: dict[str, Booster] = {}

#: 셀 **재배치** — 정지한 셀로 뽑힌 호·세션을 살아 있는 이웃 셀로 옮긴다
#: (`j-sc-27`). `None` 이면 그 셀은 살아 있다. suppressor 와 다른 점은
#: **가입자가 남는다**는 것이다 — suppressor 는 그 호를 버리고 전국에서
#: 다시 뽑으므로 정지 셀의 사람이 사라진다(확정대장 §11.3 요소 ⑤).
#: `pool` 은 그 장애의 대상 셀(`Cell`) 목록이다.
Relocator = Callable[[Scenario, Cell, float, list], "Cell | None"]
RELOCATORS: dict[str, Relocator] = {}


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


def booster(kind: str) -> Callable[[Booster], Booster]:
    def deco(fn: Booster) -> Booster:
        BOOSTERS[kind] = fn
        return fn

    return deco


#: 장애 중 **앱 추첨 비중**을 바꾼다 — `(sc, I, app) → 배율`. 대상 앱을
#: 반복해서 많이 만들어야 그 앱 축의 표본이 선다(사람 요청 2026-10-28).
#: 대상 셀(`sc.target.cell_keys`) 안에서만 걸린다 — 밖은 평시 구성비다.
AppBooster = Callable[[Scenario, float, object], float]
APP_BOOSTERS: dict[str, AppBooster] = {}


def app_booster(kind: str) -> Callable[[AppBooster], AppBooster]:
    def deco(fn: AppBooster) -> AppBooster:
        APP_BOOSTERS[kind] = fn
        return fn

    return deco


def relocator(kind: str) -> Callable[[Relocator], Relocator]:
    def deco(fn: Relocator) -> Relocator:
        RELOCATORS[kind] = fn
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


def _hold_until(rec: Rec, t: float, tail_s: tuple[float, float],
                rng: random.Random) -> None:
    """호가 ``t + tail_s[0]`` 보다 먼저 끝나면 ``t`` 뒤 ``tail_s`` 초까지 늘린다.

    배경 호 길이는 실측 분포라 짧은 호가 많다. 그 위에 "호가 이어지는"
    장애 사건을 적으면 **호가 끝난 뒤에 거절이 온** 레코드가 생긴다.
    해제는 호의 끝에 있으므로(`callflow._close_call` · 실측 191/191)
    해제·해제 요청 시각도 함께 옮긴다 — 사유는 배경 값을 그대로 둔다.
    """
    lo, hi = tail_s
    if _end(rec) >= t + lo:
        return
    t1 = t + math.exp(rng.uniform(math.log(lo), math.log(hi)))
    rec.set("call_end_time", f"{t1:.6f}")
    rec.set("call_duration_time", int(math.ceil((t1 - _start(rec)) * 1e6)))
    rec.set("SummaryCreateTime", f"{t1 + 0.05:.6f}")
    if rec.num("UEContextRelease_Time"):
        rec.set("UEContextRelease_Time", f"{t1:.6f}")
    if rec.num("UEContextReleaseRequest_Time"):
        rec.set("UEContextReleaseRequest_Time",
                f"{t1 - rng.uniform(0.005, 0.05):.6f}")


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


#: 15번 노선 구간에서 호당 NR 변화가 평소의 몇 배가 되나(강도 1.0). 초기값
HO_NR_CHURN = 3.0


def modify_bearer_base(rec: Rec) -> int:
    """NR 과 무관한 `ModifyBearer_success_count` — 호당 1 + PathSwitch 성공 수.

    현장 추출(447호)에서 NR 없는 호는 이 값과 거의 정확히 같다. 규격의 `PathSwitch_count` 는 Request
    횟수이고 `PathSwitchFailure_count` 는 그중 실패라, 성공만 뺀다(`5G-CLASSIFICATION.md`)
    """
    return 1 + max(0, int(rec.num("PathSwitch_count"))
                   - int(rec.num("PathSwitchFailure_count")))


def nr_change(rec: Rec) -> int:
    """이 호의 NR 변화 횟수 — Modify Bearer 에서 기본 몫을 뺀 나머지. 없으면 0."""
    mb = int(rec.num("ModifyBearer_success_count"))
    return max(0, mb - modify_bearer_base(rec)) if mb else 0


def set_nr_change(rec: Rec, change: int) -> None:
    rec.set("ModifyBearer_success_count", modify_bearer_base(rec) + max(0, change))


def nr_clear(rec: Rec) -> None:
    """이 호의 NR 을 없앤다 — 시각과 NR 변화를 **함께** 지운다(한쪽만 지우면 두 필드가 어긋난다)."""
    rec.set("initial_nr_conn_time", "0.0")
    if int(rec.num("ModifyBearer_success_count")):
        set_nr_change(rec, 0)


def nr_settle(rec: Rec) -> None:
    """장애 변형 **뒤** 호의 NR·Modify Bearer 를 맞춘다 — `_coerce` 는 변형 전에 값을 적는다.

    접속 실패로 바뀐 호는 베어러가 서지 않았으니 둘 다 없다. 절단으로 호가 짧아져 NR 시각이 호 끝
    뒤에 놓이면 NR 이 붙기 전에 끊긴 것이다.
    """
    if rec.get("attempt_flag") == "1" and rec.get("success_flag") not in ("1", "True"):
        rec.set("initial_nr_conn_time", "0.0")
        rec.set("ModifyBearer_success_count", 0)
    elif rec.num("initial_nr_conn_time") > (rec.num("call_end_time") or float("inf")):
        nr_clear(rec)


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


# --------------------------------------------------------------------------
# SC-01 · gw_resource — SGW 세션 자원 부족
# --------------------------------------------------------------------------


@mutator("gw_resource")
def _gw_resource(rec, sc, intensity, rng, cell) -> bool:
    """SGW 세션 자원 소진 — S11 Cause 73. **`callflow` 명세대로 적는다** (2026-10-28).

    `j-sc-18`(SA UPF 자원 소진)의 DOWNGRADE 재현이기도 하다 — XDR 에 UPF 를
    지목할 근거가 없어 EPC 의 이 장애로 만든다(확정대장 §10.2).

    ## 전 구현은 정답지와 세 군데 어긋났다

    * Attach 실패의 하류를 **EMM `17`** 로 냈다 — 정답지 `(3,73)` 3호는 전부
      **ESM `26`**(msg `209`)이다.
    * First 메시지를 `33`(응답)으로 냈다 — 정답지는 **`32`(요청)** 이고 응답
      `33` 은 구간(`s11_error_Message`)에 간다.
    * **시간 서명이 없었다.** 실측 S11 세션 실패는 Attach 면 재전송 끝에
      core 1.4~2.0초 · 응답에서 호가 닫히고, 서비스 호면 2~120ms 에 거절되고
      eNB 가 `120` 으로 해제를 요청한다. 전 구현은 core 를 배율로만 늘렸다.

    그 규칙은 전부 `config/callflow.yaml` 의 `session` 단계·`session_resource`
    장애에 있고 정답지 대조를 통과했다. 여기 다시 적으면 두 벌이 된다.
    """
    if not _node_eq(rec.get("SGW_ID"), sc.target.sgw_id):
        return False
    if rng.random() >= intensity * 0.60:
        return False
    # 지연 import — `callflow` 는 명세 yaml 을 읽는다. 이 장애가 없으면 읽을
    # 이유가 없고, `scenarios` 는 생성기 전 경로가 import 한다.
    from . import callflow
    return callflow.inject(rec, "session_resource", rng)


# --------------------------------------------------------------------------
# SC-02 · ran_radio — RAN 무선 품질 저하
# --------------------------------------------------------------------------


@mutator("ran_radio")
def _ran_radio(rec, sc, intensity, rng, cell) -> bool:
    """무선 구간 열화 — S1AP 1xx 대역. 접속 실패·절단·지연이 함께 는다.

    세 갈래다(20/30/50). 구 구현은 실패와 절단을 45/55 로 갈라 **지연만
    나빠지는 호가 없었다** — 실제 무선 열화는 대부분 재전송·재시도로 흡수되고
    일부만 실패한다.

    두 실패 갈래는 **`callflow` 명세가 적는다** (2026-10-28). 전 구현은 오류
    칸만 적었고 해제 요청도 비웠다 — 정답지는 접속 실패가 Context Setup
    실패 서명(`ctxsetup_failure`)이고 절단 81호가 **전부 eNB 요청**
    (`rreq = rel`)이다. 그래서 해제 요청 Cause 를 적는다(V3 §4.4 의
    "설정하지 않는다" 를 실측으로 뒤집었다).
    """
    if cell.cell_key not in sc.active_cells_cache:
        return False
    rec.scale("initial_access_duration", 1.0 + 1.4 * intensity)
    if rng.random() >= intensity * 0.60:
        return False
    from . import callflow
    roll = rng.random()
    if roll < 0.20:
        # ① 접속 실패 — InitialContextSetup(9, OUT)이 실패한 절차다.
        #    끊을 단계가 없는 절차(TAU 등)면 지연만 남는다
        callflow.inject(rec, "ctxsetup_failure", rng)
    elif roll < 0.50 and _connected(rec):
        # ② 연결 후 절단 — 접속은 성공했다. `success_flag` 를 **세우지
        #    않는다**(규칙 C-4 — `_connected` 주석) · `121` Radio Connection
        #    With UE Lost
        callflow.drop(rec, 121, rng)
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
    if _before_down(rec, sc):
        return False                        # 다운 시각 전에 시작한 호 — 그때 SGW 는 살아 있었다
    # **확률을 쓰지 않는다**(2026-11-04 사용자 결정 "SGW 노드 다운이면 그 SGW 로 아무것도
    # 들어오지 않아야 한다"). 전에는 `intensity × 0.90` 만 실패해 나머지 10% 가 성공 호로
    # 세션을 열었다. 실패로 못 바꾼 호는 `node_down_gate` 가 버린다 — 성공 호는 0 이다
    return _s11_timeout(rec, rng)


def _before_down(rec, sc) -> bool:
    """호의 접속 절차가 다운 시각(초) 전에 **끝났나** — 그러면 S11 실패로 바꾸지 않는다.

    시작 시각으로 가르면 틀린다(에스터 회귀 · 2026-11-04). 레코드는 **끝난 분**에 나오고
    시작은 최대 10분 앞이라(`generator._lte_call` · `start = t − dur`) 다운 뒤 분의 호도 다수가
    "다운 전에 시작"이다 — 그 호가 성공으로 남아 죽은 SGW 에서 세션을 열었다. 가로지르는 호 중
    **접속을 마친 것**은 `node_down_gate` 가 절단으로 바꾼다
    """
    at = sc.__dict__.get("start_ts")
    return at is not None and down_phase(rec, at) != "setup"


#: 접속 절차 단계 — `callflow.PHASES` 와 같다(`initial_<단계>_duration` · µs)
_SETUP_PHASES = ("paging", "access", "core")


def down_phase(rec, at: float | None) -> str:
    """다운 시각에 그 호가 어디 있었나 — ``before``(호가 끝났다) · ``connected``(접속을 마치고
    통화·데이터 중) · ``setup``(아직 접속 중이거나 다운 뒤에 시작했다). 시각을 모르면 ``setup``."""
    if at is None:
        return "setup"
    try:
        t0 = float(rec.get("call_start_time") or 0.0)
        t1 = float(rec.get("call_end_time") or 0.0)
        setup = sum(float(rec.get(f"initial_{p}_duration") or 0.0) for p in _SETUP_PHASES) / 1e6
    except (TypeError, ValueError):
        return "setup"
    if t1 and t1 < at:
        return "before"
    return "connected" if t0 and t0 + setup < at else "setup"


#: 레코드의 시각(epoch) 칸 — 분 안으로 옮길 때 함께 민다. 길이(µs)·카운트는 건드리지 않는다
_EPOCH_MIN = 1e9


def down_floor(rec, cell, down: dict) -> float | None:
    """노드 다운이 이 호를 바꿀 때의 **끝 시각 하한** — 다운 시각(epoch). 바꾸지 않으면 ``None``.

    **뮤테이터·`node_down_gate` 가 시각을 바꾸기 전에** 부른다 — 바뀐 끝 시각으로 가르면 다운 전에
    끝난 평시 호를 다운 뒤로 밀 수 있다. 다운 SGW·PGW 를 지나고 다운 시각 전에 끝나지 않은 호만이다
    """
    if not down:
        return None
    hit = down_of(down, cell=cell) or down_of(
        down, core=(None, rec.get("SGW_ID") or None, rec.get("PGW_ID") or None))
    if hit is None or hit[0] == "ENB":
        return None
    at = down_at(down, hit[0], hit[1])
    if at is None or down_phase(rec, at) == "before":
        return None
    return at


def fit_minute(rec, slot: float, lo: float | None = None) -> float:
    """호 끝 시각이 레코드 분(``slot``) 밖이면 **호 전체를 그만큼 민다** — 민 초를 돌려준다(뒤로 밀면 음수).

    XDR 은 호가 끝날 때 쓰이므로 끝 시각이 곧 기록 분이다. 생성기는 분을 먼저 정하고 그 분의
    호를 만드는데, S11 재전송 타임아웃(`callflow` `s11_retry_timeout` · 재전송 몇 번 × 수 초)이
    접속 단계를 늘리면 분 끝 가까이 끝나던 호가 다음 분으로 넘친다(에스터 관찰 · 장애 중 분당
    76~106건 · 장애 전후 0). 집계는 `EVENT_TIME`(= 생성 분)으로 분을 가르므로 숫자는 틀리지
    않지만, 레코드 안의 끝 시각과 기록 분이 어긋난다. 호 시작은 합성값이라 앞으로 미는 쪽이
    "그 분에 끝난 호" 라는 생성 전제를 지킨다 — 다음 분으로 넘기면 그 분 장애 강도와 셀 배정이
    다른 분 것이 된다. 길이·단계 시간은 그대로다
    """
    try:
        end = float(rec.get("call_end_time") or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if not end:
        return 0.0
    # 끝 시각 구간 `[floor, slot + 59.95]`. ``lo`` 는 노드 다운 경로가 넘기는 **다운 시각**이다 —
    # 시작 분에서 다운으로 실패한 호가 다운 시각 전 초에 놓이면 "장애 전에 장애 실패가 기록된"
    # 레코드가 된다(불변규칙 9 의 결 · 2026-11-04). 다른 장애 종류는 하한 없이 분 시작이다
    floor = max(slot, lo) if lo is not None else slot
    top = slot + 59.95
    if floor > top:
        floor = top
    over = end - top
    if end < floor:
        # **앞쪽도 같다**(2026-11-04 에스터 관찰) — 성공 호를 짧은 실패 호로 다시 쓰면
        # (`callflow.inject` → `_close_call` 이 끝을 단계 뒤로 맞춘다) 시작이 몇 분 앞이던 호의
        # 끝이 이 분 시작보다 앞선다(S11 타임아웃 · 최대 86초 · SC-18 도 같다). 구간 안의 자리는
        # 원래 초에서 잇는다 — `floor + (end − floor) mod 폭`. 한 점에 몰지 않는다
        width = max(0.05, top - floor)
        over = end - (floor + ((end - floor) % width))
    elif over <= 0.0:
        return 0.0
    for i, v in enumerate(rec.values):
        if not v or "." not in v:
            continue
        try:
            f = float(v)
        except ValueError:
            continue
        if f > _EPOCH_MIN and abs(f - end) < 3600.0:
            rec.values[i] = f"{f - over:.6f}"
    return over


def retime_drop(rec, lo: float, hi: float, rng) -> None:
    """`callflow.drop` 이 고른 절단 시각을 ``[lo, hi]`` 안으로 옮긴다 — 요청·해제 간격은 그대로.

    `drop` 은 호 길이의 15~60% 지점에서 끊는데, 호 시작이 최대 10분 앞이라 절단 시각이 다운
    시각보다 앞설 수 있다(에스터 관찰 · 절단 레코드의 약 5%). 노드 다운의 절단은 **다운 시각 이후,
    그 레코드의 분 안**이어야 한다. 호 시작(`call_start_time`)은 두다 — 연결은 그 전부터 있었다
    """
    try:
        t0 = float(rec.get("call_start_time") or 0.0)
        t_req = float(rec.get("UEContextReleaseRequest_Time") or 0.0)
        t_rel = float(rec.get("UEContextRelease_Time") or 0.0)
    except (TypeError, ValueError):
        return
    gap = max(0.0, t_rel - t_req)
    # 접속 절차가 끝난 뒤여야 한다 — 그 전에 끊을 수 없다(`callflow.drop` 과 같은 하한)
    try:
        setup = sum(float(rec.get(f"initial_{p}_duration") or 0.0) for p in _SETUP_PHASES) / 1e6
    except (TypeError, ValueError):
        setup = 0.0
    lo = max(lo, t0 + setup + 0.01)
    # **해제(`nr`)까지 ``hi`` 안이다** — 요청 시각만 맞추면 간격(최대 3.7초 · Cause 200)만큼
    # 분 끝을 넘었다(에스터 관찰 · 시작 분 최대 15초). 남은 자리가 간격보다 좁으면 간격을 줄인다
    room = max(0.0, hi - lo)
    gap = min(gap, max(0.001, room * 0.5))
    nq = rng.uniform(lo, max(lo, hi - gap))
    nr = min(nq + gap, max(hi, nq + 0.001))
    rec.set("first_error_time", f"{nq:.6f}")
    rec.set("UEContextReleaseRequest_Time", f"{nq:.6f}")
    for f in ("last_error_time", "s1ap_error_Time", "UEContextRelease_Time"):
        rec.set(f, f"{nr:.6f}")
    us = int(math.ceil(max(0.0, nr - t0) * 1e6))
    rec.set("call_end_time", f"{t0 + us / 1e6:.6f}")
    rec.set("call_duration_time", us)
    rec.set("SummaryCreateTime", f"{t0 + us / 1e6 + 0.05:.6f}")


def _s11_timeout(rec, rng, *, downstream: bool = True) -> bool:
    """S11 재전송 타임아웃 한 건 — SC-03 과 SC-18 이 같이 쓴다.

    **`callflow` 명세가 적는다**(`s11_retry_timeout` · 2026-10-28). 정답지
    S11 `900` 10호의 모양이다 — First 는 `(3, 900, msg 32)` 지만 **구간에는
    결국 받은 거절 응답**(Cause · msg 33)이 남고 하류도 그것을 따른다.
    전 구현은 구간에 `900`/msg 32 를 적었는데 정답지에 그런 호는 없다.

    하류는 ESM 30 만, 70% 에만 — 2026-10-22 사용자 조건부 승인 그대로다
    (명세 주석). 정답지 S11 `900` 은 Attach·서비스(3·4)에만 있다.
    """
    from . import callflow
    ct = str(rec.get("call_type"))
    if ct == "1":
        return callflow.inject(rec, "s11_retry_timeout", rng,
                               downstream=downstream)
    if ct in ("3", "4"):
        return callflow.inject(rec, "s11_retry_timeout_svc", rng,
                               downstream=downstream)
    return False


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
    if _before_down(rec, sc):
        return False
    # 확률을 쓰지 않는다 — SC-03 과 같다(2026-11-04). 기존 세션 무효화도 이제 한다
    # (`node_down_set` · `pemdb1._node_down_cuts`) — 아래 ⚠️ 는 그 전 범위의 기록이다
    # **`callflow` 명세가 적는다** (2026-10-28). 정답지 S11 `72` 10호는 전부
    # Attach 이고 **응답 msg `33`** 이다 — `179 PGW_RESTART_NOTIFICATION` 은
    # 정답지에 **한 건도 없다.** `R-05b` 는 "`72` 또는 `179`" 라 `72` 로
    # 발동한다. 서비스 호는 `session_resource` 의 `73` 과 같은 확장이다.
    from . import callflow
    if str(rec.get("call_type")) == "1":
        return callflow.inject(rec, "s11_reject", rng, cause=72)
    return callflow.inject(rec, "s11_reject_svc", rng, cause=72)


# --------------------------------------------------------------------------
# 노드 다운 — 그 장비로는 아무것도 들어오지 않는다 (2026-11-04 사용자 결정)
# --------------------------------------------------------------------------
#
# "SGW 노드 다운이라는 시나리오가 있으려면 말 그대로 SGW 로 아무것도 안 들어와야 한다.
# 그 아래 모든 기지국은 콜이 없어야 한다." 전에는 새 접속의 일부만 실패시키고, 이미 열린
# 세션은 열 때 박아 둔 `core` 로 정상 기록을 계속 냈다(`session.py` `Session.core`) —
# "조금 아픈 SGW" 였다. F-109(SGW-16) 에서 체감 통계에 정상 세션이 섞여 판정을 흐렸다.
#
# **다운 집합은 활성 장애에서 매 분 계산한다** — 활성 장애의 정본은 `CM_DATAGEN_FAULT`
# (`faults.resolve_active`)이고 새 테이블을 두지 않는다. gen 이 다시 떠도 같은 행에서 같은
# 집합이 나오고, 장애가 끝난 분부터 저절로 빠진다(복귀).

#: kind → 다운되는 장비 종류. `partial_recovery`(SC-18) · `gw_resource` · `mme_overload` 는
#: 의도된 **부분** 장애라 들지 않는다
NODE_DOWN_KINDS = {"gw_node_down": "SGW", "gw_pgw_restart": "PGW", "enb_s1_outage": "ENB"}


def _down_from(sc, kind: str) -> int | None:
    """그 장애에서 장비가 내려가는 첫 분. eNB 는 단절 구간(`OUTAGE_GATE` 이상)부터다 —
    그 앞의 전조(S1AP 200)는 살아 있는 eNB 의 열화다."""
    if kind != "ENB":
        return sc.start_minute
    for m in range(sc.start_minute, sc.start_minute + sc.duration):
        if sc.intensity(m) >= OUTAGE_GATE:
            return m
    return None


def node_down_set(active, minute: int) -> dict[str, dict[str, int]]:
    """이 분에 다운된 장비 — ``{"SGW": {id: 다운 시작 분}, "PGW": {…}, "ENB": {…}}``.

    ID 는 SGW·PGW 는 숫자로 접은 문자열(`_node_eq` 와 같은 표기 문제 — '0016' 과 '16'),
    eNB 는 셀 마스터 값 그대로다. **강도는 다운 여부만 가른다** — SGW·PGW 는 창 안이면
    다운, eNB 는 `OUTAGE_GATE` 이상이면 다운이다. 강도로 실패 비율을 정하지 않는다.
    """
    out: dict = {"SGW": {}, "PGW": {}, "ENB": {}, "AT": {}}
    for sc in active:
        kind = NODE_DOWN_KINDS.get(sc.kind)
        if kind is None:
            continue
        i = sc.intensity(minute)
        if kind == "ENB":
            nid = sc.target.enb_id
            if nid is None or i < OUTAGE_GATE:
                continue
            key = str(nid)
        else:
            nid = sc.target.sgw_id if kind == "SGW" else sc.target.pgw_id
            if nid is None or i <= 0.0:
                continue
            key = node_key(nid)
        start = _down_from(sc, kind)
        if start is None:
            continue
        out[kind][key] = min(out[kind].get(key, start), start)
        # 다운 **시각**(epoch) — 장애 기록의 실제 시작(초 단위)이다. eNB 는 전조 뒤 단절이
        # 서는 분이라 분 시작이다. 그 전의 호는 정상이고 세션은 이 시각에 끊긴다
        at = (sc.__dict__.get("start_ts") if start == sc.start_minute else None)
        if at is not None:
            prev = out["AT"].get((kind, key))
            out["AT"][(kind, key)] = at if prev is None else min(prev, at)
    for sc in active:
        if sc.kind != "natural_disaster":
            continue
        at = disaster_down_at(sc)
        start = sc.start_minute + DISASTER_DOWN_AT
        for e in disaster_dead_enbs(sc, minute):
            out["ENB"][e] = min(out["ENB"].get(e, start), start)
            if at is not None:
                out["AT"][("ENB", e)] = at
    if not out["AT"]:
        del out["AT"]
    return out


def down_at(down: dict, kind: str, key: str) -> float | None:
    """그 장비의 다운 시각(epoch) — 모르면 ``None``(분 시작으로 본다)."""
    return (down.get("AT") or {}).get((kind, key))


def node_key(raw) -> str:
    """SGW·PGW ID 표기를 하나로 — 숫자면 숫자 문자열, 아니면 그대로."""
    try:
        return str(int(raw))
    except (TypeError, ValueError):
        return str(raw)


def down_of(down: dict, cell=None, core=None) -> tuple[str, str, int] | None:
    """셀 또는 세션 코어 ``(mme, sgw, pgw)`` 가 다운 장비를 지나는가 → ``(종류, ID, 시작 분)``."""
    sgw = core[1] if core is not None else getattr(cell, "sgw_id", None)
    pgw = core[2] if core is not None else getattr(cell, "pgw_id", None)
    for kind, nid in (("SGW", sgw), ("PGW", pgw)):
        if nid is not None and down[kind]:
            k = node_key(nid)
            if k in down[kind]:
                return kind, k, down[kind][k]
    if cell is not None and down["ENB"]:
        e = str(getattr(cell, "enb_id", ""))
        if e in down["ENB"]:
            return "ENB", e, down["ENB"][e]
    return None


#: 노드 다운 절단 Cause — S1AP `504 Unspecified Failure`(초기값 · `pemdb1.PemdbGen.DOWN_CUT_CAUSE`
#: 와 같은 값). 다운 시각을 가로질러 끊긴 호와, 열린 세션의 정리 기록이 같은 값을 쓴다
DOWN_CUT_CAUSE = 504


def down_opens_session(rec, cell, down: dict) -> bool:
    """이 호가 세션을 열어도 되나 — 다운 SGW·PGW 를 지나고 다운 시각 전에 끝나지 않았으면 안 된다.

    절단된 호(`connected`)도 `success_flag=1` 이라 `SessionEngine.classify` 가 세션을 연다 —
    세션은 호 **끝**(`Call.t`)에서 열리므로 그 시각의 죽은 SGW 로 데이터가 흐르게 된다.
    """
    hit = down_of(down, cell=cell) or down_of(
        down, core=(None, rec.get("SGW_ID") or None, rec.get("PGW_ID") or None))
    if hit is None or hit[0] == "ENB":
        return True
    return down_phase(rec, down_at(down, hit[0], hit[1])) == "before"


#: 다운 SGW·PGW 아래 셀에서 **만들지 않는** 호 — 살아 있는 SGW 를 전제하는 절차다.
#: Paging(6)은 SGW 의 하향 데이터 통지가 계기이고, S1 HO·Reroute(10·11)는 열린 베어러가
#: 있어야 한다(그 베어러는 다운과 함께 끊었다). TAU(5)·Detach(9)는 MME 만 거치는 신호라
#: 남긴다 — 세션을 열지 않는다(`session.CT_SESSION_W` 0). 근거: TS 23.401 §5.3.4.3(Paging 은
#: SGW Downlink Data Notification 으로 시작) · §5.3.3(TAU 는 SGW 변경이 없으면 SGW 를 부르지 않는다)
DOWN_DROP_CALL_TYPES = frozenset({"6", "10", "11"})


def node_down_gate(rec, cell, down: dict, rng, slot: float | None = None) -> bool:
    """다운 장비를 지나는 새 호를 거른다 — ``False`` 면 그 호를 내지 않는다.

    * SGW·PGW 아래 셀의 세션 설정 호(`CT_SESSION_SETUP`) — **실패로만** 낸다. 뮤테이터가
      못 바꾼 호(그 Call Type 에 명세가 없다 · MT Attach·VoLTE 등)는 성공으로 둘 수 없어 버린다
    * `DOWN_DROP_CALL_TYPES` — 버린다
    * 그 밖(TAU·Detach) — 그대로
    eNB 다운은 `enb_s1_outage` 억제기가 이미 호를 만들지 않는다 — 여기 오지 않는다.
    """
    # **레코드에 찍힌 장비도 본다** — 호가 전환으로 다른 셀에서 끝나면 `SGW_ID`·`PGW_ID`·
    # `Last_eNB_ID` 는 끝난 쪽 값이다(`pemdb1._apply_handover`). 첫 셀만 보면 다운 SGW 에서
    # 끝난 성공 호가 남는다(에스터 xfail). 전환 자체도 `_baseline_handover` 가 막지만 두 겹이다
    hit = down_of(down, cell=cell) or down_of(
        down, core=(None, rec.get("SGW_ID") or None, rec.get("PGW_ID") or None))
    if hit is None:
        for f in ("First_eNB_ID", "Last_eNB_ID"):
            e = str(rec.get(f) or "")
            if e and e in down["ENB"]:
                return False                # 다운 eNB 를 지나는 호는 없다(무호)
        return True
    if hit[0] == "ENB":
        return True                         # 첫 셀이 다운 eNB — 억제기가 이미 막는다
    # **다운 시각을 가로지르는 호** — 끝 시각으로 가른다(`down_phase` · 에스터 회귀).
    #   before     다운 전에 끝났다 — 정상이다
    #   connected  접속을 마치고 쓰던 중 다운 — **절단**이다(504 · 다운 시각 이후 · 그 분 안).
    #              세션은 열지 않는다(`down_opens_session`)
    #   setup      접속 중이었거나 다운 뒤에 시작 — S11 실패다(아래)
    at = down_at(down, hit[0], hit[1])
    phase = down_phase(rec, at)
    if phase == "before":
        return True
    ct = str(rec.get("call_type"))
    if ct in DOWN_DROP_CALL_TYPES:
        return False
    if phase == "connected" and ct in CT_SESSION_SETUP:
        if str(rec.get("success_flag")) not in ("1", "True"):
            return True                     # 평시 실패 호 — 그대로
        from . import callflow
        if slot is None:
            # 레코드의 분 — `drop` 이 끝 시각을 바꾸기 **전에** 읽는다. 바꾼 뒤에 읽으면 앞선
            # 분이 나와 절단이 레코드 분 밖(분 시작 전)으로 갔다(에스터 관찰 · 분당 15~29건)
            slot = math.floor(float(rec.get("call_end_time") or at) / 60.0) * 60.0
        if not callflow.drop(rec, DOWN_CUT_CAUSE, rng):
            return False                    # 절단을 적을 수 없는 호 종류 — 성공으로 둘 수 없다
        # 절단은 `max(다운 시각, 레코드 분 시작)` 이후, 레코드 분 끝 전 — 시작 분이면 다운 시각부터
        retime_drop(rec, max(at, slot) + 0.1, slot + 59.9, rng)
        return True
    if ct in CT_SESSION_SETUP and str(rec.get("success_flag")) in ("1", "True"):
        # 뮤테이터가 그 장애 대상(`sc.target`)으로만 걸리므로 한 번 더 건다 — 같은 장비가
        # 셀 목록 밖에서도 다운이다(대상 셀을 고른 장애라도 장비는 하나다)
        if hit[0] == "SGW":
            _s11_timeout(rec, rng)
        else:
            from . import callflow
            callflow.inject(rec, "s11_reject" if ct == "1" else "s11_reject_svc", rng, cause=72)
        if str(rec.get("success_flag")) in ("1", "True"):
            return False
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
    # **`callflow` 명세가 적는다** (2026-10-28). Cause 는 정답지 인증 거절
    # 15호의 구성비(`15001`·`5003` 최다)이고, 하류 EMM·해제(`301`)·시각도
    # 그 정답지 모양이다. 전 구현은 `{5012, 5030, 3004}` 로 정답지 최다
    # 둘을 만들지 못했다. 정답지는 Attach 뿐이지만 평시 호가 인증을 붙이는
    # 절차 전부에 건다(`inject_call_types` · 사용자 결정 2026-10-28).
    from . import callflow
    return callflow.inject(rec, "auth_reject", rng)


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
    # **`callflow` 명세가 적는다** (2026-10-28) — 무응답은 `auth_timeout`
    # (구간 Cause 를 비운다 · D.2), 3xxx 거절은 `dra_route_failure`.
    # 정답지는 Attach 뿐이지만 평시 호가 인증을 붙이는 절차 전부에 건다.
    from . import callflow
    return callflow.inject(
        rec, "auth_timeout" if timeout else "dra_route_failure", rng)


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
    _congested_rtt(rec, rng, intensity, "Uplink")
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
    _congested_rtt(plan, rng, intensity, "Uplink")
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
    _congested_rtt(plan, rng, intensity)
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

    ## 절차마다 실측 모양이 다르다 (2026-10-28 · J-SC-26)

    | 절차 | 실측 | 여기서 |
    | --- | --- | --- |
    | Attach (ct1) | ESM 거절이 호 실패가 된다 — ESM First 실패 31호 중 29호 | `esm_ue_reject` (msg 195 · `96`/`99`) |
    | 서비스 (ct3/4) | 단말이 ESM `195` 로 거절해도 **호는 성공**한다(4호 전부) | Context Setup 실패 `126` |

    서비스 호를 ESM 실패로 두면 실측에 없는 호가 된다. 그렇다고 Last 만 남기면
    `C-02` 가 움직이지 않는다 — Attach 는 전체 호의 0.2% 라 기종 축에서
    희석된다. 서비스 호에서 **단말이 무선 절차를 끝내지 못한** 모양은
    `126`(Failure in the Radio Interface Procedure)이고 그 서명은 실측에 있다
    (ct3/4 `126` 5호 · `ctxsetup_failure`). `R-10` 은 Cause 가 아니라 기종
    접속률과 지리 흩어짐을 보므로 판정 경로는 같다.
    """
    vm = sc.target.vendor_model
    if vm and (rec.get("Vendor"), rec.get("Model")) != vm:
        return False
    if rec.get("data_attempt_flag") != "1":
        return False
    if rng.random() >= intensity * 0.70:
        return False
    from . import callflow
    if str(rec.get("call_type")) == "1":
        return callflow.inject(rec, "esm_ue_reject", rng)
    return callflow.inject(rec, "ctxsetup_failure", rng, cause=126)


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
    return _handover_failure(rec, sc, rng)


@mutator("son_param_change")
def _son_param_change(rec, sc, intensity, rng, cell) -> bool:
    """SON 이 H/O 파라미터를 바꾼 셀 쌍 — 그 시각부터 전환 실패가 계단으로 선다.

    주입은 SC-09 와 **같은 한 건**이다(`_handover_failure`). 다른 것은 시간
    모양(`STEP` · `ramp_in=1`)과 정답(`CHANGE_SUSPECTED`)뿐이다.

    **`focus_pairs` 가 있으면 그 쌍의 출발 셀에서 시작한 호만 끊는다**(2026-11-09). 실패는 그 호의
    `First_*`(출발 셀)에서 쌍의 도착 셀(`Last_*`)로 간다 — 호 안의 전환 기록 규칙이 그렇다
    (`pemdb1._apply_handover` · First = 전환 전, Last = 전환 후). 강도·확률·106 절단·PSF 비율은 그대로고,
    실패가 퍼지던 자리만 모은다. 9·15번(`mobility`·`ho_interruption`)은 이 분기를 지나지 않는다
    """
    if cell.cell_key not in sc.active_cells_cache:
        return False
    pairs = None
    if sc.focus_pairs:
        pairs = [p for p in sc.target.cell_pairs if p[0] == cell.cell_key]
        if not pairs:
            return False
    # 셀별 세기(2026-11-13) — 출발 셀마다 배율이 다르다. **시작 분은 모든 셀이 같다** — 한 변경이 한 시점에
    # 들어간다. 배율이 없는 셀(옛 대상)은 1.0 이다
    gain = sc.target.son_gain.get(cell.cell_key, 1.0)
    if rng.random() >= intensity * gain * 0.50:
        return False
    return _handover_failure(rec, sc, rng, pairs=pairs)


#: 전환 실패가 실리는 절차 — 실측 해제 `106` 14호의 `call_type` 전수.
_HO_CALL_TYPES = frozenset({"3", "4", "5"})


def _handover_failure(rec, sc, rng, pairs=None) -> bool:
    """셀 쌍의 핸드오버 실패 한 건 — SC-09 와 J-SC-25 가 같이 쓴다.

    **실측 모양이다**(2026-10-28 · R1+FAIL 의 해제 `106` 14호 + PSF 1호).
    전 구현은 PSF 를 세우고 오류를 비웠고, 절단은 Last 만 적었다 — 실측
    14호는 전부 일반 절단(`callflow.drop`)과 같은 꼴이다.

    | 갈래 | 실측 |
    | --- | --- |
    | Path Switch 실패 | First = (S1AP, `100`, msg `3` PathSwitchRequest) → 10ms 뒤 `106` 절단 |
    | 핸드오버 실패 절단 | First = (S1AP, `106`, msg 18) → 1~2ms 뒤 Last (`106`, msg 23) = 호 끝, `rreq = rel` |

    PSF 갈래를 절반으로 둔다 — 실측은 1:13 이지만 `R-17` 이 PSF 집중을
    보므로 그 신호가 서야 한다. **실패율 자체는 장애 강도가 정한다.**
    전환이 실패하면 단말은 끊긴다 — PSF 만 있고 호가 이어지는 꼴은 실측에
    없다. 그래서 **연결된 호에만** 건다(규칙 C-4).
    """
    if not _connected(rec):
        return False
    # 실측 `106` 절단 14호가 **전부 ct3·4·5** 다 — 이미 붙어 있다가 옮겨
    # 가는 호다. Attach 중 전환 실패는 실측에 없다.
    if str(rec.get("call_type")) not in _HO_CALL_TYPES:
        return False
    from . import callflow
    nr = nr_change(rec)                     # 아래서 PathSwitch 가 바뀐다 — NR 몫은 그대로 둔다
    if not callflow.drop(rec, 106, rng):    # Handover Failure In Target EPC/eNB
        return False
    ps = max(int(rec.num("PathSwitch_count")), 1)
    rec.set("PathSwitch_count", ps)
    if rng.random() < 0.50:
        t = rec.num("UEContextReleaseRequest_Time") - rng.uniform(0.005, 0.015)
        rec.set("PathSwitchFailure_count", 1)
        _set_first(rec, 2, 100, 3, t)       # Unspecified · PathSwitchRequest
    set_nr_change(rec, nr)
    pool = pairs if pairs is not None else sc.target.cell_pairs
    if pool and rng.random() < 0.80:
        # 실측 14호 중 11호가 Last 셀이 First 와 다르다 — 옮겨 간 쪽에서 끊긴다.
        # ``pairs`` 를 주면(25번) 그 호의 출발 셀에서 나가는 쌍만이다 — 출발 셀은 그 호의 First 다
        _src, dst = rng.choice(pool)
        enb, _, cid = dst.rpartition("-")
        rec.set("Last_eNB_ID", enb)
        rec.set("Last_Cell_ID", cid)
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
    # 두 갈래 모두 **`callflow` 명세가 적는다** (2026-10-28 · `j-sc-19`).
    #
    # 전 구현은 실측 모양에서 벗어나 있었다 —
    #   ① 접속 실패가 해제 사유에 `500` 을 적지 않았다. `R-23` 은 **해제
    #      사유의 `500`** 을 보므로 이 갈래는 룰에 보이지 않았다.
    #   ② 절단이 First Error·S1AP 구간·eNB 요청을 비워 뒀다. 실측 절단 81호는
    #      전부 First(msg 18 요청)·Last(msg 23 해제)·`rreq = rel` 이다.
    #
    # `500` 자체는 정답지에 없다(평시 스냅숏 — R1 S1AP 5xx 0건). **그릇만
    # 실측이다** — Context Setup 실패(`126`·`103`)와 절단(`504`·`126`…)의 모양.
    from . import callflow
    if rng.random() < 0.50 or not _connected(rec):
        # ① 접속 실패. **연결되지 않은 호는 반드시 이쪽이다** — 절단 갈래는
        #    `success_flag` 를 세울 수 없다(규칙 C-4 · `_connected` 주석)
        return callflow.inject(rec, "ctxsetup_overload", rng)
    # ② 연결 후 절단. Cause 500 은 `RAN_LOST_CAUSES` 에 **없으므로**
    #    `classify()` → `OUT_STALL` → down 재전송 급증 + `AckTime=0`.
    #    과부하가 U-Plane 품질로도 번진다 — 의도한 것이다
    return callflow.drop(rec, 500, rng)


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

    ESM Cause 는 `30`("Request rejected by Serving GW or PDN GW")이다 —
    `27`/`93` 은 APN 축 전용 카운터(`ESM27`·`S11_93`)를 건드려 SC-19 와
    섞인다. 서비스 호의 First 는 S11 `68` 이고 `30` 은 그 하류다(아래).
    """
    if not _node_eq(rec.get("MME_ID"), sc.target.mme_id):
        return False
    if sc.target.apn and rec.get("APN") != sc.target.apn:
        return False
    if rec.get("data_attempt_flag") != "1":
        return False
    if rng.random() >= intensity * 0.85:    # 설정 오류는 거의 전건이다
        return False
    # **절차마다 실측 모양이 다르다** (2026-10-28 · `callflow`).
    #
    # 서비스 호 — 정답지 S11 `68`(Service not supported) 6호가 **전부 한
    # APN** 이고 하류에 ESM `30` 을 남긴다. "그 APN 라우팅이 틀렸다" 의
    # 실측 모양이 이것이다. 전 구현은 ESM `30` 을 First 로 적었는데 서비스
    # 호에서 ESM 이 First 인 실패는 실측에 없다.
    #
    # Attach — MME 가 PDN 연결을 직접 거절한 ESM `30` 4호의 모양.
    from . import callflow
    if str(rec.get("call_type")) == "1":
        return callflow.inject(rec, "esm_reject", rng, cause=30)
    return callflow.inject(rec, "s11_reject_svc", rng, cause=68)


# --------------------------------------------------------------------------
# SC-12 · ims_pdn — IMS PDN 접속 실패
# --------------------------------------------------------------------------


@mutator("ims_pdn")
def _ims_pdn(rec, sc, intensity, rng, cell) -> bool:
    """VoLTE 전용 이슈 — `C-21` 만 악화하고 `C-20`(Data)은 정상이다 (R-30).

    ★ **First Error 를 건드리지 않는다.** `success_flag=1` 인 호에
    First Error 를 심으면 `cell_agg` 가 `FE_RECOVERED`(`err AND
    SUCCESS_FLAG='1'`)로 계상해 **`R-24`(First Error 상승 + 회복률 높음)가
    유발된다.** "IMS 만 실패"는 호 수준 오류가 아니다.

    **Last Error 는 적는다** — 실측 모양이다(2026-10-28). 정답지(R1+FAIL)에
    "호 성공 · 부가 PDN 만 거절" 호가 38건 있고 **전부 First 는 비우고
    Last = (ESM, Cause, 거절 메시지)**, Last 시각 = `esm_error_Time` 이다.
    거절은 호 시작 **35~230ms 뒤**(중앙 62ms)이고 호는 그대로 이어져
    비활동 해제(`120`)로 끝난다 — 해제는 평시 배경 그대로 둔다.
    `cell_agg` 의 `FE_*` 는 First 만 보므로 Last 를 적어도 위 함정은 없다.

    `esm_error_Cause` 는 남긴다 — `_causes` 가 First Error 와 독립으로 구간
    Cause 컬럼을 읽으므로 `EV-03.by_stage['esm']` 에 사실로 남는다.

    Cause 는 `26`(Insufficient resources)다. `33`("Requested service option
    not subscribed")은 **미가입**이라 자원 부족과 뜻이 다르다. **정답지에
    IMS 실패 호는 0건**이라(R1 IMS 시도 35,284호 전부 성공) Cause 자체는
    실측 근거가 없다 — 그릇만 위 38건에서 가져왔다.
    """
    if rec.get("ims_attempt_flag") != "1":
        return False
    if not _node_eq(rec.get("ims_PGW_ID"), sc.target.pgw_id):
        return False
    if rng.random() >= intensity * 0.80:
        return False
    t = _start(rec) + math.exp(rng.uniform(math.log(0.035), math.log(0.25)))
    rec.set("ims_success_flag", 0)
    rec.set("esm_error_Cause", 26)
    rec.set("esm_error_Message", 209)       # PDN_CONNECTIVITY_REJECT (OUT)
    rec.set("esm_error_Time", f"{t:.6f}")
    rec.set("last_error_interface_protocol", 6)
    rec.set("last_error_cause", 26)
    rec.set("last_error_message", 209)
    rec.set("last_error_time", f"{t:.6f}")
    # 거절 뒤에도 호는 이어진다 — 실측 38건의 거절→호끝 8~19초
    _hold_until(rec, t, (8.0, 19.0), rng)
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
    nr_clear(rec)
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
    nr_clear(rec)
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
    _congested_rtt(plan, rng, intensity)
    plan.scale("Traffic_Total_Dnlink_PayloadSize", max(0.2, 1.0 - 0.4 * intensity))
    return True


# --------------------------------------------------------------------------
# J-SC-16 · anchor_saturation — 「アンテナは立つが遅い」 5G 체감 저하
# --------------------------------------------------------------------------


@plan_mutator("anchor_saturation")
def _anchor_saturation(plan, sc, intensity, rng, cell) -> bool:
    """5G 로 **붙었는데 느리다** — 셀 경합 (R-39).

    일본 요구 「アンテナは立つが遅い」(안테나는 서는데 느리다)다. `g-sc-16`
    의 식(`ThroughputTime` 유지 · `PayloadSize` 축소 → `U-15` 하락)을
    **NODE 축에서 CELL 축으로** 옮긴 것이고 새 식이 아니다.

    ## 건드리지 않는 것이 이 mutator 의 정의다

    **`initial_nr_conn_time`** — 그것을 `0` 으로 만들면 `N-02`(NR 레그
    연결률)가 떨어져 `R-33`(`NSA_NR_LEG_FAILURE`)이 깨어나고, 같은 주입에서
    두 원인이 나와 Top-1 이 갈린다(불변규칙 10 · 확정대장 §9.4). 그리고
    그 값은 **다른 시나리오의 것이다** — SA 관점에서 `= 0` 은 *5G 를 못
    쓴다*(`j-sc-17` 폴백)이고, 여기는 *쓰는데 느리다* 다(§9.8.1).

    **재전송·RTT** — 올리면 `R-09`(`TRANSPORT_LOSS`)·`R-09e`
    (`GW_OVERLOAD`)와 경쟁한다. `R-39` 가 "재전송·aRTT 정상" 을 조건으로
    걸고 있으므로 올리는 순간 **아무 룰도 안 붙는다.**

    **up 축** — Service/Backbone 축이다(불변규칙 8).

    `g-sc-16` 이 `_down_loss` 로 손실을 함께 넣는 것과 **일부러 다르다.**
    그쪽은 GW 사용자평면 과부하라 손실이 실제로 생기고, 이쪽은 무선 구간
    스케줄링 경합이라 **패킷이 버려지지 않고 늦게 간다.**

    ⚠️ `OUT_STALL` 은 제외한다(V3 §7.2) — 이미 재전송이 8~20배라 `U-12`
    가 움직이고, 그러면 `R-39` 의 ② 조건에 걸려 자기 룰이 안 붙는다.
    """
    if cell.cell_key not in sc.active_cells_cache:
        return False
    if plan.outcome == "stall":
        return False
    # **SA 세션만 느리다**(사용자 결정 2026-11-02) — 앵커 경합은 5G 로 붙은
    # 단말의 일이다. LTE 세션까지 깎으면 "셀 전체 혼잡"과 구분되지 않고,
    # R-39 의 SA·LTE 비교가 설 자리가 없다. 망 모드는 `session.net_mode_of`
    if plan.net_mode != "SA":
        return False
    # 대상 셀의 SA 세션은 **전부** 걸린다(2026-11-03 · 0.9 → 1.0 · 초기값).
    # 군집 10~15셀로 좁힌 만큼 셀 안에서 진하게 한다 — 전국 volume 은 그대로
    if rng.random() >= intensity * 1.0:
        return False
    # 하한 0.25 는 `g-sc-16`(0.2)보다 얕다. 저쪽은 손실과 RTT 를 함께
    # 올려 신호가 셋이고, 이쪽은 **처리율 하나로만** 판정되므로 폭이
    # 필요하다 — `EV-08` 의 `throughput_drop_pct` 가 30% 다.
    plan.scale("Traffic_Total_Dnlink_PayloadSize",
               max(0.25, 1.0 - 0.6 * intensity))
    return True


#: 앵커 경합 대상 셀 추첨 가중 상한 — **초기값**(2026-11-03). 군집 10~15셀의
#: 표본이 ADM3·MESH1K 판정 단위에서 서도록 대상 셀 호 수를 올린다. 전국 호 수는
#: `_volume` 이 정하므로 그대로다
ANCHOR_LOAD = 3.0


@booster("anchor_saturation")
def _anchor_saturation_load(sc, intensity) -> float:
    """대상 셀 가중을 창 동안 `1 + (ANCHOR_LOAD-1)·I` 배 — `ran_congestion` 과
    같은 결이다. `floor_per_cell` 은 lte_call 만 늘려 U-15(transport 세션)를
    채우지 못한다."""
    return 1.0 + (ANCHOR_LOAD - 1.0) * intensity


# --------------------------------------------------------------------------
# J-SC-14 · ran_congestion — 도심 역 저녁 러시 Sub6 혼잡
# --------------------------------------------------------------------------

def _congested_rtt(plan, rng, intensity: float, way: str = "Dnlink") -> None:
    """큐가 쌓인 down RTT — **1회 평균을 절대값 범위로 끌어올린다.**

    `DnlinkRTTTime` 은 패킷 사이 대기의 **합계**이고 `DnlinkRTTCount` 로
    나누면 1회 평균이다. 전에는 합계에 `1 + 1.5·I` 를 곱했는데 평시 1회
    평균이 수 ms 라 혼잡이어도 수 ms 였다 — graphio 체감 판정(150ms)에
    한 번도 걸리지 않았다(2026-10-29 실측, 14번 대상 8셀 대기 불량 0명).

    강도 `I` 만큼 평시 값에서 목표(`FAULT.congestion_rtt` 범위의 난수)로
    옮긴다. 표본이 없는 세션(`Count=0`)은 건드리지 않는다 — 합계만 만들면
    "표본 0인데 대기 시간이 있다"가 된다.

    **RTT 를 올리는 장애는 전부 이것을 쓴다**(2026-10-29) — 7·21·22번과
    PGW 사용자평면·복합 장애도 합계에 곱하고 있었다. `way` 는 `Dnlink`·
    `Uplink` 다.
    """
    # `Plan`(세션 총량)과 `Rec`(http 분 레코드) 둘 다 받는다 — 읽는 이름만 다르다
    read = plan.num if isinstance(plan, Rec) else plan.get
    cnt = read(f"{way}RTTCount")
    if cnt <= 0:
        return
    cur = read(f"{way}RTTTime") / cnt
    lo, hi = FAULT.congestion_rtt
    target = rng.uniform(lo, hi)
    if target > cur:
        v = (cur + (target - cur) * intensity) * cnt
        plan.set(f"{way}RTTTime", int(v) if isinstance(plan, Rec) else v)


def congested_user(imsi: str, sid: str, intensity: float, share: float | None = None) -> bool:
    """14·23번에서 이 가입자가 나빠졌나 — IMSI 와 장애 ID 로 정해 주입 내내 같은 사람이다.

    강도가 오르면 같은 순위의 사람이 차례로 더해진다. 강도가 내려도 빠지는 쪽은 나중에 든 사람이다
    """
    share = FAULT.congestion_user_share if share is None else share
    return zlib.crc32(f"{sid}:{imsi}".encode()) % 1000 < 1000 * share * min(1.0, intensity)


@plan_mutator("ran_congestion")
def _ran_congestion_users(plan, sc, intensity, rng, cell) -> bool:
    """14번 — **원래 있던 사람이 나빠진다.** 호·가입자는 늘지 않는다(2026-11-05 사용자 결정).

    세션마다 추첨하면 같은 사람이 분마다 좋았다 나빴다 해서 "그 사람이 불편하다"가 서지 않는다.
    고른 사람의 세션은 전부 혼잡 품질이 되고 나머지는 평시 그대로다
    """
    if plan.outcome == "stall" or not congested_user(plan.imsi, sc.id, intensity):
        return False
    _congest(plan, rng, intensity)
    return True


def _ran_congestion(plan, sc, intensity, rng, cell) -> bool:
    """혼잡을 세션 추첨으로 건다 — 24번 A 와 재해 주변 셀이 쓴다. 14번은 사람으로 고른다."""
    if plan.outcome == "stall":
        return False                        # 겹침 방지 (V3 §7.2)
    if rng.random() >= intensity * 0.8:
        return False
    _congest(plan, rng, intensity)
    return True


def _congest(plan, rng, intensity: float) -> None:
    """무선 용량 포화의 품질 — **C-Plane 은 정상이고 느려진다.**

    - 처리율: `ThroughputTime` 유지 · `PayloadSize` 축소 → `Q-05`·`U-15` 하락
    - 대기: `DnlinkRTTTime` 상승 — 큐가 쌓인다
    - 정체: 일부 세션이 한 분 동안 down 이 멈춘다(`plan.stall`) → `Q-04`
    - 재전송은 **조금만** — 스케줄링 경합은 버리지 않고 늦게 보낸다.
      많이 올리면 `TRANSPORT_LOSS` 와 경쟁한다.

    up 축은 건드리지 않는다(불변규칙 8 · `Q-09` 는 `j-sc-23` 의 것이다).
    """
    plan.scale("Traffic_Total_Dnlink_PayloadSize",
               max(0.3, 1.0 - 0.55 * intensity))
    _congested_rtt(plan, rng, intensity)
    _down_loss(plan, rng, intensity, 0.005, 0.01, 0.02)
    if plan.span >= 3 and rng.random() < 0.35 * intensity:
        plan.stall = True


# --------------------------------------------------------------------------
# J-SC-15 · ho_interruption — 야마노테선 구간 4G↔5G 전환 정체
# --------------------------------------------------------------------------


@plan_mutator("ho_interruption")
def _ho_interruption(plan, sc, intensity, rng, cell) -> bool:
    """코리도 셀에서 시작한 세션이 **전환하면 직후 3–8초 down 이 멈춘다.**

    확정대장 §7.0 의 "H/O 직후 3–8초 정체" 다. 여기서는 **요청만** 한다 —
    구간은 실제 전환 시각에 `session._emit` 이 정한다. 전환하지 않는
    세션에는 아무 일도 없고, 그것이 `Q-08`(이동 직후 정체) 이 `Q-04`
    (정체 일반)와 갈리는 자리다.

    처리율·RTT 는 건드리지 않는다 — 올리면 `j-sc-14`(혼잡)·`R-39` 와
    원인이 겹친다. 이쪽은 **전환 순간에만** 나빠지는 것이 정의다.
    """
    if plan.outcome == "stall":
        return False                        # 겹침 방지 (V3 §7.2)
    if rng.random() >= intensity * 0.9:
        return False
    plan.ho_stall = True
    return True


@mutator("ho_interruption")
def _ho_interruption_nr(rec, sc, intensity, rng, cell) -> bool:
    """같은 코리도에서 **NR 레그가 풀린다** — 추가·해제 반복의 C-Plane 쪽.

    `nsa_nr_leg`(13번)와 같은 필드(`initial_nr_conn_time = 0`)지만 **비율이
    절반 아래다.** 레그를 붙였다 떼는 것이라 일부 호는 NR 로 끝나고, 전부
    떨어뜨리면 `N-02` 급락 → `R-33`(NR 레그 실패)이 Top-1 을 가져간다.
    `Q-14`(5G 체류율)가 내려갈 만큼만 떨어뜨린다.

    **NR 이 남은 호는 흔들린다**(2026-11-05) — 붙었다 떨어지기를 반복하면 경로가 그만큼 더 바뀐다.
    호당 NR 변화(`ModifyBearer − PathSwitch 성공 − 1`)를 `HO_NR_CHURN` 배로 늘린다. 레코드 하나로
    "끊겼다"를 적는 필드가 없어 이것이 이 구간의 서명이다(`5G-CLASSIFICATION.md`)
    """
    if rec.get("ue_dcnr") != "1":
        return False
    if rng.random() >= intensity * 0.35:
        nr = nr_change(rec)
        if nr:
            set_nr_change(rec, round(nr * (1.0 + (HO_NR_CHURN - 1.0) * intensity)))
        return bool(nr)
    nr_clear(rec)
    return True


# --------------------------------------------------------------------------
# J-SC-27 · earthquake — 국지 다수 셀 정지 + 주변 부하 유입
# --------------------------------------------------------------------------

#: 지역 셀 중 정지하는 비율 — 확정대장 §11.3 "ADM2 1곳 셀 40% 무응답".
QUAKE_DOWN = 0.40


def _quake_rank(sc) -> dict[str, int]:
    """대상 eNB → 정지 순위. `sc` 에 한 번만 만들어 둔다.

    eNB 마다 **고정된 값** `u ∈ [0,1)` 을 해시로 정하고 그 순으로 줄 세운다.
    해시 문턱(`u < 0.4·I`)을 그대로 쓰지 않는 이유: 25셀 지역의 eNB 는 몇
    개뿐이라 해시 운에 따라 정지 eNB 가 0개가 될 수 있다(조이 M-5 설계).
    """
    rank = sc.__dict__.get("_quake_rank")
    if rank is None:
        enbs = {k.rsplit("-", 1)[0] for k in sc.target.cell_keys}
        order = sorted(enbs, key=lambda e: zlib.crc32(f"{sc.id}:{e}".encode()))
        rank = {e: i for i, e in enumerate(order)}
        sc.__dict__["_quake_rank"] = rank
    return rank


def quake_down(sc, cell, intensity: float) -> bool:
    """이 셀이 지금 정지해 있는가 — **eNB 단위다** (2026-11-03 M-5).

    지진은 사이트 단위 정지다(사용자 결정 — 셀 일부 정지는 다른 장애).
    셀 단위로 멈추면 같은 eNB 의 남은 셀이 호를 계속 내서, eNB 합으로 판정하는
    D4 가 무호를 보지 못한다. 대상 eNB 를 `_quake_rank` 순으로 세우고 앞의
    `ceil(0.4·I·n_enb)` 개를 멈춘다 — 비율 0.40 은 그대로다. 정지 집합이 분마다
    흔들리지 않고, 복구 구간(`I` 하락)에서는 순위가 뒤인 eNB 부터 살아난다 —
    같은 eNB 가 꺼졌다 켜졌다 하지 않는다.
    """
    if cell.cell_key not in sc.target.cell_keys:
        return False
    rank = _quake_rank(sc)
    r = rank.get(str(cell.enb_id))
    if r is None:
        return False
    return r < math.ceil(QUAKE_DOWN * intensity * len(rank) - 1e-9)


@relocator("earthquake")
def _quake_relocate(sc, cell, intensity, pool):
    """정지 셀 → **가장 가까운 살아 있는 셀.** 대상 지역 안에서 찾는다.

    지역 밖으로 보내지 않는 이유: "주변 부하 유입"이 요구이고, 멀리 흩으면
    유입이 희석되어 주변 혼잡이 보이지 않는다. 지역 전체가 정지했으면
    (`I` 가 커 40% 가 아니라 전부인 경우는 없지만) `None` 이 아니라 원래
    셀을 그대로 쓰게 둔다 — 호를 버리지 않는다.
    """
    if not quake_down(sc, cell, intensity):
        return None
    from .geo_lite import haversine_km

    live = [c for c in pool if not quake_down(sc, c, intensity)]
    if not live:
        return None
    return min(live, key=lambda c: haversine_km(cell.latitude, cell.longitude,
                                                c.latitude, c.longitude))


@plan_mutator("earthquake")
def _quake_ring(plan, sc, intensity, rng, cell) -> bool:
    """정지 셀 **주변**(살아 있는 지역 셀)의 혼잡 — `j-sc-14` 식을 얕게.

    유입으로 활성 가입자가 늘어난 셀이 실제로 느려져야 "주변 혼잡"이
    데이터에 선다. 강도를 0.6 배로 둔 것은 주 원인이 정지(`RAN_NODE_DOWN`)
    이기 때문이다 — 혼잡을 같은 세기로 넣으면 원인 순위가 뒤집힌다
    (원인 2개 동시 판정은 확정대장 OPEN-35 로 보류).
    """
    if quake_down(sc, cell, intensity):
        return False
    return _ran_congestion(plan, sc, 0.6 * intensity, rng, cell)


# --------------------------------------------------------------------------
# J-SC-27 · natural_disaster — 지진 — 통화 폭주 + 기지국 정지 + 문자 실패
# --------------------------------------------------------------------------
#
# 국면(장애 시작 분 기준 · **초기값**) —
#
#   0 ~ 3분    호 폭주만 — 경보를 본 사람들이 안부 통화·문자·뉴스. 2분에 걸쳐 차오른다
#   3분        기지국 다운(지진 정보로 고른 음영 eNB · `quake_plan` · 2026-11-13. 전에는 지역 eNB 의 40%)
#   3 ~ 13분   폭주 + 다운
#   13 ~ 23분  다운만 — 폭주가 가라앉아도 무호가 남아야 장비 장애라는 근거가 선다
#   23분       정상화
#
# **다운 eNB 의 세션은 코어에 살아 있다**(2026-11-04 원칙 · SGW 다운과 다르다). 데이터는 다운
# 시각에 멈추고(`freeze`), 이웃 살아 있는 셀에서 같은 세션으로 이어진다(`relocator`) — 그 셀에서
# 재연결 Service Request 를 남긴다(`pemdb1._enb_down_reconnect`).

#: 국면 경계(분) — 초기값 · 시연 압축값이다(실제 정전 정지는 몇 시간 뒤가 정점이다)
DISASTER_SURGE_RAMP = 2
DISASTER_DOWN_AT = 3
DISASTER_SURGE_END = 13
DISASTER_DOWN_END = 23
#: 폭주 정점 — 경보 지역 셀의 평소 호 대비 배율(강도 1.0 일 때). **초기값 · 결정 대기**(11-05 케이
#: 잠정안). 실측 발신 약 50배·착신 약 20배(동일본 대지진 · 도코모)에 맞춰 40 으로 올렸다(첫 값 6).
#: 처리 용량 때문에 전국 합은 평소의 3배로 묶는다(`pemdb1` 의 `SURGE_CAP` · 사용자 "최대치의
#: 1.2~3배, 몇 분 이내")
DISASTER_SURGE_PEAK = 40.0
#: 폭주로 더해지는 호의 구성 — 음성(VoLTE 확장 서비스 7·8)이 데이터(3·4)보다 많고 발신이 착신보다
#: 많다. 비율은 실측 증가 배수(발신 50 : 착신 20 = 5 : 2)를 따르고, 음성 몫 0.6 은 **초기값**이다
#: (총무성 H24 "패킷은 음성만큼 늘지 않았다"). 평소 구성(3 이 68.7% · 7·8 거의 0)은 건드리지 않는다 —
#: 더한 호만 이 구성이다
DISASTER_SURGE_MIX = (("7", 0.6 * 5 / 7), ("8", 0.6 * 2 / 7),
                      ("3", 0.4 * 5 / 7), ("4", 0.4 * 2 / 7))
#: Emergency 접속을 찍는 절차 — 단말이 거는 발신뿐이다. 착신(4·8)의 접속 사유는 MT_ACCESS 다
DISASTER_EMERGENCY_CALL_TYPES = frozenset(("3", "7"))
#: 지역 셀 중 정지하는 eNB 비율 — 지진의 `QUAKE_DOWN` 그대로(확정대장 §11.3). 2026-11-13 부터는 지진 정보
#: (`quake_plan`)가 없을 때만 쓴다 — 지난 기록 재생과 자동 지역 고르기(`faults._busy_disaster_area`)의 미리 셈
DISASTER_DOWN_RATIO = QUAKE_DOWN
#: 자동 선택 지역 크기(셀 수) — 국소 / 넓은 지역. 초기값
DISASTER_AREA_CELLS = {"local": 25, "wide": 80}
#: 다운 뒤 이웃 셀에서 다시 붙기까지(초) — 무선 연결 유실 감지 + 셀 재선택 + Service Request.
#: 초기값(현업 확인 전)
DISASTER_RECONNECT_S = (5.0, 30.0)


# ── 지진 정보로 음영 eNB 를 고른다 (2026-11-13 사용자 결정 · 27번) ──────────────────────────
#
# 27번이 시작될 때 지진 한 건(진앙·진도·모양)을 정하고 `CM_QUAKE_EVENT` 에 적는다(`cli` · `control`).
# 음영(정지) eNB 는 그 지진에서 고른다 — 전에는 지역 eNB 의 40%(`DISASTER_DOWN_RATIO`)였다. 호 폭주·
# 국면(3분 정지 · 23분 정상화)은 그대로다. **아래 값은 모두 결과를 보고 조정할 초기값이다.**

#: 시나리오 진도 범위 — 무작위 · 소수 한 자리(사용자 결정 2.0~4.0)
QUAKE_INTENSITY_RANGE = (2.0, 4.0)
#: 진도 → (음영 반경 km, 진앙 0km 정지 확률 %, 정지 셀 최대 수). 사이는 직선으로 잇는다. 확률·최대 셀은
#: 사용자 값(2 → 10%·10셀, 3 → 30%·25셀, 4 → 50%·40셀), 반경은 케이 제안(3·6·10km)이다. 실측 근거는 없다
QUAKE_TABLE = ((2.0, 3.0, 10.0, 10), (3.0, 6.0, 30.0, 25), (4.0, 10.0, 50.0, 40))
#: 음영 모양 — ROUND(진앙에서의 거리) · LINEAR(진앙을 지나는 선분에서의 거리 · 단층을 따라 꺼지는 모양)
QUAKE_SHAPES = ("ROUND", "LINEAR")
#: LINEAR 선분 길이 = 반경 × 이 값(진앙이 선분 가운데). 초기값
QUAKE_LINEAR_LEN_X = 2.0


def quake_params(intensity: float) -> tuple[float, float, int]:
    """진도 → (반경 km, 진앙 정지 확률 %, 최대 셀) — `QUAKE_TABLE` 사이를 직선으로 잇는다. 범위 밖은 끝값."""
    t = QUAKE_TABLE
    x = min(max(float(intensity), t[0][0]), t[-1][0])
    for (x0, r0, p0, m0), (x1, r1, p1, m1) in zip(t, t[1:]):
        if x <= x1:
            f = (x - x0) / (x1 - x0) if x1 > x0 else 0.0
            return (round(r0 + (r1 - r0) * f, 1), round(p0 + (p1 - p0) * f, 1),
                    int(round(m0 + (m1 - m0) * f)))
    return t[-1][1], t[-1][2], t[-1][3]


def quake_new(lat: float, lon: float, rng: random.Random, intensity: float | None = None,
              shape: str | None = None) -> dict:
    """지진 한 건 — 정하지 않은 값은 ``rng``(장애 ID 시드)로 뽑는다. 키는 `CM_QUAKE_EVENT` 컬럼과 같은 뜻이다."""
    lo, hi = QUAKE_INTENSITY_RANGE
    inten = round(rng.uniform(lo, hi), 1) if intensity is None else round(float(intensity), 1)
    shape = shape if shape in QUAKE_SHAPES else QUAKE_SHAPES[rng.randrange(len(QUAKE_SHAPES))]
    radius, base, most = quake_params(inten)
    q = {"lat": round(float(lat), 6), "lon": round(float(lon), 6), "intensity": inten, "shape": shape,
         "azimuth_deg": None, "length_km": None, "radius_km": radius, "base_pct": base, "max_cells": most}
    if shape == "LINEAR":
        q["azimuth_deg"] = rng.randrange(360)
        q["length_km"] = round(radius * QUAKE_LINEAR_LEN_X, 1)
    return q


def quake_dist_km(q: dict, lat: float, lon: float) -> float:
    """셀까지 거리 — ROUND 는 진앙, LINEAR 는 진앙을 가운데 둔 선분(방위 0 = 북 · 시계 방향)."""
    from .geo_lite import haversine_km

    if q["shape"] != "LINEAR":
        return haversine_km(q["lat"], q["lon"], lat, lon)
    kx = 111.32 * math.cos(math.radians(q["lat"]))
    x, y = (lon - q["lon"]) * kx, (lat - q["lat"]) * 111.0
    a = math.radians(q["azimuth_deg"] or 0)
    dx, dy = math.sin(a), math.cos(a)
    half = (q["length_km"] or 0.0) / 2.0
    t = max(-half, min(half, x * dx + y * dy))
    return math.hypot(x - t * dx, y - t * dy)


def quake_plan(cells: list[Cell], q: dict, rng: random.Random,
               want: int = 25) -> tuple[set[str], list[str], list[str]]:
    """지진 → (폭주 지역 셀, 음영 eNB, 음영 셀).

    반경 안 셀마다 정지 확률 `base × (1 − 거리/반경)` 을 두고 **가까운 셀부터** ``rng`` 로 뽑는다. 한 셀이
    뽑히면 **그 eNB 의 셀을 함께** 끈다(실제 정전·전송 단절은 eNB 째 꺼진다 · 케이 제안). 함께 끄면 최대
    셀 수를 넘는 eNB 는 건너뛰고, 최대에 닿으면 멈춘다. 좌표 없는 셀은 거리를 모르므로 넣지 않는다.

    **폭주 지역은 기존 크기다**(``want`` = `DISASTER_AREA_CELLS` 국소 25 · 넓은 80 · 2026-11-13 정정). **진앙**
    에서 가까운 셀부터 ``want`` 개에 꺼진 eNB 의 셀을 더한다 — 그래서 조금 넘을 수 있다. LINEAR 여도 폭주는
    진앙 둘레다(2026-11-14 · 11-13 결정 "진앙 중심 25/80셀"). 선분 거리로 고르면 선분 위 셀이 모두 거리 0 으로
    같아져 25셀이 선분 끝에 몰렸다(F-329 · 진앙에서 5~7.6km). 선분 거리는 정지 eNB 를 고를 때만 쓴다.
    처음에는 반경 안 셀 전부였는데, 진도 4(10km)면 수백~수천 셀이 되어 전국 상한(`pemdb1.SURGE_CAP`)에
    눌려 "지역만 40배 폭주"가 희석됐다. 사용자 결정은 "호 폭주는 기존과 동일"이다
    """
    r = float(q["radius_km"])
    ranked = sorted(((quake_dist_km(q, c.latitude, c.longitude), c.cell_key, c) for c in cells
                     if c.geo_status == "MAPPED"), key=lambda x: (x[0], x[1]))
    near = [(d, c) for d, _k, c in ranked if d <= r]
    per: dict[str, list[str]] = {}
    for c in cells:
        per.setdefault(str(c.enb_id), []).append(c.cell_key)
    dead: list[str] = []
    n = 0
    for d, c in near:
        if n >= q["max_cells"]:
            break
        e = str(c.enb_id)
        if e in dead:
            continue
        if rng.random() >= q["base_pct"] / 100.0 * (1.0 - d / r if r > 0 else 0.0):
            continue
        if n + len(per[e]) > q["max_cells"]:
            continue
        dead.append(e)
        n += len(per[e])
    dead_cells = sorted(k for e in dead for k in per[e])
    from .geo_lite import haversine_km

    by_epi = sorted((haversine_km(q["lat"], q["lon"], c.latitude, c.longitude), c.cell_key) for c in cells
                    if c.geo_status == "MAPPED")
    area = {k for _d, k in by_epi[:max(0, int(want))]} | set(dead_cells)
    return area, sorted(dead), dead_cells


#: 27번 폭주 호에 얹는 SMS 에러(2026-11-13 사용자 결정) — 비율은 레코드 몫이고 **초기값**이다(현장 실측 없음).
#: SMS 필드는 LTE 호 기록 안의 "마지막 SMS 에러 원인"이다 — 시도 여부 칸이 없어 성공률은 셀 수 없다
DISASTER_SMS_SHARE = {"surge": 0.08, "down": 0.15}
#: 발신(MO) — (필드, 원인) · 규격 `XDR_Cause_v0.5` SM_RP 42 Congestion · SM_CP 22 Congestion · SM_TP 192 SC busy
DISASTER_SMS_MO = (("sms_mo_rp_error_cause", "42"), ("sms_mo_cp_error_cause", "22"),
                   ("sms_mo_tp_error_cause", "192"))
#: 착신(MT) — 음영 국면의 수신 도달 불가 · SM_RP 27 Destination out of order · 28 Unidentified subscriber
DISASTER_SMS_MT = (("sms_mt_rp_error_cause", "27"), ("sms_mt_rp_error_cause", "28"))
#: 착신 절차 — 4(데이터 착신) · 8(음성 착신). 나머지 폭주 절차(3·7)는 발신이다
DISASTER_MT_CALL_TYPES = frozenset(("4", "8"))


def disaster_sms_error(sc, minute: int, ct: str, rng: random.Random) -> tuple[str, str] | None:
    """폭주 호 하나에 얹을 SMS 에러 `(필드, 원인)` — 없으면 None.

    폭주만 있는 국면은 `DISASTER_SMS_SHARE["surge"]`, 음영이 생긴 뒤는 `"down"` 이다. 발신은 혼잡 원인
    셋 중 하나, 착신은 음영이 있을 때만 도달 불가(27·28)다 — 음영 전에는 받을 사람이 다 살아 있다
    """
    dead = disaster_dead_enbs(sc, minute)
    share = DISASTER_SMS_SHARE["down" if dead else "surge"]
    if rng.random() >= share:
        return None
    if ct in DISASTER_MT_CALL_TYPES:
        return DISASTER_SMS_MT[rng.randrange(len(DISASTER_SMS_MT))] if dead else None
    return DISASTER_SMS_MO[rng.randrange(len(DISASTER_SMS_MO))]


def disaster_elapsed(sc, minute: int) -> int:
    return minute - sc.start_minute


def disaster_surge_mult(sc, minute: int) -> float:
    """그 분의 폭주 배율(1 = 평소). 0 분부터 `DISASTER_SURGE_RAMP` 분에 걸쳐 정점까지 오른다."""
    if sc.kind != "natural_disaster":
        return 1.0
    e = disaster_elapsed(sc, minute)
    if not (0 <= e < DISASTER_SURGE_END):
        return 1.0
    ramp = min(1.0, (e + 1) / max(1, DISASTER_SURGE_RAMP))
    return 1.0 + (DISASTER_SURGE_PEAK - 1.0) * ramp * max(0.0, min(1.0, sc.peak))


def _disaster_rank(sc) -> dict[str, int]:
    """지역 eNB → 정지 순위. ``local`` 은 지역 가운데에 가까운 eNB 부터(한 곳에 모인다),
    ``wide`` 는 해시 순(흩어진다 · 지진의 `_quake_rank` 와 같다). `sc` 에 한 번만 만든다."""
    rank = sc.__dict__.get("_dis_rank")
    if rank is not None:
        return rank
    rank = disaster_rank_of(sc.target.cell_keys, sc.__dict__.get("_pool") or [],
                            sc.target.outage_mode or "local", sc.id)
    sc.__dict__["_dis_rank"] = rank
    return rank


def disaster_rank_of(cell_keys, pool, mode: str, sid: str) -> dict[str, int]:
    """정지 순위의 본체 — 시나리오 없이도 부를 수 있게 뺐다(대상 자동 선택이 후보마다 미리 센다 ·
    `faults._busy_disaster_area`). 규칙은 `_disaster_rank` 머리말 그대로다"""
    enbs = sorted({k.rsplit("-", 1)[0] for k in cell_keys})
    if mode == "local" and pool:
        from .geo_lite import haversine_km
        pts = [(c.latitude, c.longitude) for c in pool if c.geo_status == "MAPPED"]
        if pts:
            la = sum(p[0] for p in pts) / len(pts)
            lo = sum(p[1] for p in pts) / len(pts)
            pos: dict[str, tuple[float, float]] = {}
            for c in pool:
                if c.geo_status == "MAPPED":
                    pos.setdefault(str(c.enb_id), (c.latitude, c.longitude))
            enbs.sort(key=lambda e: (haversine_km(la, lo, *pos[e]) if e in pos else 1e9, e))
        else:
            enbs.sort(key=lambda e: zlib.crc32(f"{sid}:{e}".encode()))
    else:
        enbs.sort(key=lambda e: zlib.crc32(f"{sid}:{e}".encode()))
    return {e: i for i, e in enumerate(enbs)}


def disaster_dead_n(n_enbs: int) -> int:
    """지역 eNB 수 → 정지 eNB 수(`ceil(비율 × n)`)."""
    return math.ceil(DISASTER_DOWN_RATIO * n_enbs - 1e-9)


def disaster_dead_enbs(sc, minute: int) -> set[str]:
    """그 분에 정지해 있는 eNB — 다운 국면에만. 정지 집합은 고정이다(분마다 흔들리지 않는다)."""
    if sc.kind != "natural_disaster":
        return set()
    e = disaster_elapsed(sc, minute)
    if not (DISASTER_DOWN_AT <= e < DISASTER_DOWN_END):
        return set()
    q = sc.__dict__.get("_quake_dead")
    if q is not None:
        # 지진 정보로 고른 음영 eNB(2026-11-13) — 없을 때만 옛 비율 규칙이다(지난 기록 재생)
        return set(q)
    rank = _disaster_rank(sc)
    n = disaster_dead_n(len(rank))
    return {k for k, r in rank.items() if r < n}


def _disaster_truth(sc) -> dict:
    """자연재해 정답지 덧붙임 — 주원인(`NATURAL_DISASTER_SUSPECTED`)의 **맥락**을 싣는다.

    graphio 채점은 모르는 키를 무시한다. 폭주 탐지가 생기면 `context` 를 읽는다. 분은 장애 시작
    기준 상대값이다(`start_minute` 에 더하면 절대 분)
    """
    if sc.kind != "natural_disaster":
        return {}
    quake = sc.__dict__.get("_quake")
    if sc.__dict__.get("_quake_dead") is not None:
        dead = sorted(sc._quake_dead)
    else:
        rank = _disaster_rank(sc)
        n = disaster_dead_n(len(rank))
        dead = sorted(k for k, r in rank.items() if r < n)
    at = disaster_down_at(sc)
    rate = sc.__dict__.get("_enb_rate")
    return {
        # 지진 한 건(`CM_QUAKE_EVENT` 와 같은 값 · 2026-11-13) — 옛 비율 규칙이면 None
        "quake": quake,
        "down_cells": sc.__dict__.get("_quake_dead_cells"),
        # 정지 eNB 의 평소 호율(분당 시도 · 장애 시작 전 60분 · `pemdb1.enb_call_rate`). D4 확정에
        # 걸리는 분(N*)을 미리 셈하라고 싣는다. 창고를 못 읽었으면 None 이다(불변규칙 17)
        "down_enbs_rate": ({e: round(rate.get(e, 0.0), 3) for e in dead}
                           if rate is not None else None),
        # 폭주 기준값 — 지역의 장애 시작 앞 60분 분당 평균 호(`faults._surge_base`). 정점 지역 호는
        # 대략 이 값 × 배율이다. 창고를 못 읽었으면 None(그 분 평소 호를 기준으로 썼다)
        "surge_base_per_min": (round(sc._surge_base, 2)
                               if sc.__dict__.get("_surge_base") is not None else None),
        "outage_mode": sc.target.outage_mode or "local",
        "context": [{"code": "CALL_SURGE", "from_min": 0, "to_min": DISASTER_SURGE_END,
                     "cells": sorted(sc.target.cell_keys)}],
        "phases": {"surge": [0, DISASTER_SURGE_END],
                   "down": [DISASTER_DOWN_AT, DISASTER_DOWN_END]},
        "down_enbs": dead,
        # epoch 초 — 레코드 시각(`slot_start`)과 같은 기준. 장애 기록 시각보다 최대 59초 늦다
        "down_at_ts": at,
    }


# --------------------------------------------------------------------------
# SA→LTE 폴백 급증 (17번 · `sa_fallback`)
# --------------------------------------------------------------------------
#
# **숨은 SA 상주 단말**(`pemdb1.PemdbGen.sa_hidden`)은 가입자 풀 밖의 별도 집합이다 — 평소 호를 내지
# 않으므로 생산량(15%)·가입자 구성이 바뀌지 않는다. 장애가 걸리면 대상 지역에 상주하던 그 단말 중
# 강도만큼이 몇 분에 걸쳐 LTE 에 붙고(TAU 또는 Attach), 그 뒤 평소 가입자와 같은 빈도로 호를 낸다.
# 장애가 끝나면 다시 SA 로 돌아가 EPC 에서 사라진다(`pemdb1._sa_fallback_drafts`).

#: 넘어오는 데 걸리는 시간(분) — 단말마다 [0, 이 값) 에서 뽑는다. 5G 셀이 꺼져도 단말은 무선 유실
#: 감지·셀 재선택·등록 재시도 타이머가 서로 달라 한꺼번에 오지 않는다. 돌아갈 때도 같다. 초기값
SA_FALLBACK_RAMP_MIN = 3
#: 들어오는 절차 중 TAU(5) 몫 — 나머지는 Attach(1). N26 이 있으면 5GS→EPS 유휴 이동은 매핑된 GUTI
#: 로 TAU 를 하고(TS 23.502 §4.11.1.3.2), N26 이 없거나 등록이 안 풀리면 Attach 다(§4.11.2.3).
#: 현장 N26 여부를 모른다 — 초기값(현업 확인 전)
SA_FALLBACK_TAU_SHARE = 0.7
#: 숨은 단말이 더하는 레코드의 상한 — 그 분 평소 호 수의 배수(자연재해 `SURGE_CAP` 의 결). 초기값
SA_FALLBACK_CAP = 0.5


def _sa_fallback_truth(sc) -> dict:
    """17번 정답지 덧붙임 — 원인 코드는 결정 대기라 맥락만 싣는다(graphio 채점은 모르는 키를 무시한다)."""
    if sc.kind != "sa_fallback":
        return {}
    n = sc.__dict__.get("_sa_n")
    return {
        "context": [{"code": "SA_FALLBACK", "from_min": 0, "to_min": sc.duration,
                     "cells": sorted(sc.target.cell_keys)}],
        "phases": {"arrive": [0, SA_FALLBACK_RAMP_MIN],
                   "back": [sc.duration, sc.duration + SA_FALLBACK_RAMP_MIN]},
        # 넘어오기로 뽑힌 숨은 단말 수 — 첫 분 전에는 아직 모른다(None · 불변규칙 17)
        "fallback_ue": n,
        # 그 단말들의 IMSI(합성 평문) — 리오가 "넘어온 단말"을 레코드에서 정확히 세라고 싣는다.
        # 뽑힌 단말이 다 레코드를 내는 것은 아니다 — 들어옴 기록은 생산량 비율로 뽑고 호는 평소
        # 1인당 빈도라, 15% 에서는 몇 분 동안 한 건도 안 내는 단말이 있다
        "fallback_imsis": sc.__dict__.get("_sa_imsis"),
    }


def disaster_down_at(sc) -> float | None:
    """다운 시각(epoch) — **다운 국면이 시작되는 분의 시작**이다.

    장애 기록은 초 단위(:18)지만 호·세션의 재배치(`relocator`)가 분 단위라, 분 중간을 다운 시각으로
    두면 그 분의 다운 전 호가 이미 이웃으로 옮겨 가 있다. 모든 레코드가 같은 시각을 말하게 분
    경계에 둔다 — 장애 시작 기준으로 최대 59초 늦다(보고 · 정답지 시각은 이 값이다)
    """
    t = sc.__dict__.get("start_ts")
    if t is None:
        return None
    return math.floor(t / 60.0) * 60.0 + DISASTER_DOWN_AT * 60.0


@relocator("natural_disaster")
def _disaster_relocate(sc, cell, intensity, pool):
    """정지 eNB 의 셀 → 지역 안 가장 가까운 살아 있는 셀(지진과 같다). 세션은 이 길로 이어진다."""
    dead = disaster_dead_enbs(sc, getattr(sc, "_minute", sc.start_minute))
    if not dead or str(cell.enb_id) not in dead or cell.cell_key not in sc.target.cell_keys:
        return None
    from .geo_lite import haversine_km

    live = [c for c in pool if str(c.enb_id) not in dead]
    if not live:
        return None
    return min(live, key=lambda c: haversine_km(cell.latitude, cell.longitude,
                                                c.latitude, c.longitude))


@plan_mutator("natural_disaster")
def _disaster_ring(plan, sc, intensity, rng, cell) -> bool:
    """정지 eNB 주변(살아 있는 지역 셀)의 혼잡 — 지진의 `_quake_ring` 과 같은 세기(0.6 배)."""
    dead = disaster_dead_enbs(sc, getattr(sc, "_minute", sc.start_minute))
    if not dead or str(cell.enb_id) in dead:
        return False
    return _ran_congestion(plan, sc, 0.6 * intensity, rng, cell)


@relocator("enb_s1_outage")
def _outage_relocate(sc, cell, intensity, pool):
    """eNB 단절(SC-17) — 그 eNB 셀의 **열린 세션**을 가장 가까운 다른 eNB 셀로(2026-11-04 원칙).

    새 호는 억제기가 이미 막는다(무호) — 이 함수가 받는 것은 사실상 세션뿐이다. 전에는 세션을
    끊었는데(`End`), eNB 만 죽으면 코어 세션은 살아 있고 이웃에서 같은 세션으로 이어진다(지진과
    같은 원칙). 이웃은 그 eNB 셀 둘레의 다른 eNB 셀이다 — `faults.build` 가 실어 둔 전국 셀에서
    셀마다 한 번만 찾는다
    """
    if sc.target.enb_id is None or str(cell.enb_id) != str(sc.target.enb_id):
        return None
    if intensity < OUTAGE_GATE:
        return None
    memo = sc.__dict__.setdefault("_near", {})
    if cell.cell_key in memo:
        return memo[cell.cell_key]
    from .geo_lite import haversine_km

    allc = sc.__dict__.get("_pool_all") or []
    cand = [c for c in allc if str(c.enb_id) != str(sc.target.enb_id) and c.geo_status == "MAPPED"]
    dst = (min(cand, key=lambda c: haversine_km(cell.latitude, cell.longitude,
                                                c.latitude, c.longitude))
           if cand and cell.geo_status == "MAPPED" else None)
    memo[cell.cell_key] = dst
    return dst


# --------------------------------------------------------------------------
# J-SC-24 · capacity_shift — 기지국 증설 후 부하 이동
# --------------------------------------------------------------------------
#
# **장애 주입이 아니라 증설 재현이다**(2026-10-29 사람 결정). 스펙 §8.3 은
# 변경 전후 4주를 비교하지만 시연은 40분에 압축한다 —
#
#   0–20분   A 에 호가 10분에 걸쳐 몰리고 10분 유지한다(`congestion_load` 까지) · A 혼잡
#   20분     B 개통 — A 로 오던 호의 60% 가 B 로 간다. A 가 서서히 회복
#   30–40분  분석 시간. 40분에 B 가 다시 꺼진다
#
# 혼잡 구간이 10분이면 graphio Q-11(5분 창 3칸 연속)이 서지 못했다(F-152 · 2026-11-06 사용자 결정).
# 임계가 아니라 시나리오 길이를 늘렸다 — 증설이 필요한 혼잡은 10분보다 오래 간다
#
# B 는 **새로 등록하지 않는다.** 마스터에 이미 있는 eNB 를 평소 생성
# 대상에서 빼 두고(`pemdb1.capacity_shift.standby_enbs`) 이 창에서만 호를
# 준다. graphio 지도는 그 시간에 호가 있던 셀만 그리므로 B 는 켜질 때
# "처음 나타난다". 마스터 행을 넣거나 지우지 않으니 B 의 과거 1분 통계가
# 미매핑으로 떨어지지 않는다(불변규칙 12).

#: 국면 길이(분). **바꾸면 아래 B 순환의 안전 간격도 같이 움직인다.**
SHIFT_RISE = 20
SHIFT_TOTAL = 40
#: 혼잡이 다 차는 데 걸리는 시간(분) — 나머지 `SHIFT_RISE − SHIFT_RAMP` 분은 정점을 유지한다. Q-11 은
#: 5분 창 3칸 연속이라, 20분에 걸쳐 오르면 칸 비율이 문턱을 넘는 창이 2칸뿐이다(칸 안 A 몫 약 38% ·
#: F-152 는 A 불량 28% 에 칸 비율 7~11%). 호를 더 몰지 않고 정점에 일찍 닿게 한다. **초기값**
SHIFT_RAMP = 10
#: B 개통 뒤 A 품질이 평시로 돌아오는 데 걸리는 시간(분) — "서서히"
SHIFT_RECOVER = 5
#: B 개통 뒤 A 로 오던 호 중 B 가 가져가는 몫 — "A 40 · B 60"
SHIFT_SPLIT = 0.6


@dataclass
class ShiftCfg:
    """`pemdb1.capacity_shift` — 부하를 내주는 eNB 와 소등 eNB 목록.

    **모듈 전역으로 둔다** — `FaultCfg` 와 같은 이유다. `faults.build` 가
    대상을 해석할 때 읽는데 그 서명에 설정을 받을 자리가 없다.
    `PemdbGen.__init__` 이 `set_shift_cfg()` 로 한 번 덮는다.
    """

    #: 부하를 내주는 eNB(A). 비면 이 kind 는 해석 실패로 취소된다
    from_enb: str = ""
    #: 소등 eNB(B) 후보. **여럿을 돌려 쓴다** — `shift_pick` 머리말
    standby_enbs: tuple[str, ...] = ()

    @classmethod
    def from_cfg(cls, cfg: dict | None) -> "ShiftCfg":
        """모르는 키는 멈춘다 — 오타가 "소등 목록이 비었다"로 끝나면 B 가
        평소에도 호를 받고, 30분 뒤 그것을 끄는 순간 무호로 탐지된다."""
        f = cls()
        for k, v in (cfg or {}).items():
            if k.startswith("_"):
                continue                    # 사람이 남긴 메모 — 파일의 `_note`
            if k == "from_enb":
                f.from_enb = str(v or "").strip()
            elif k == "standby_enbs":
                f.standby_enbs = tuple(str(x).strip() for x in (v or ()))
            else:
                raise RuntimeError(f"pemdb1.capacity_shift 의 모르는 키: {k}")
        if f.from_enb and f.from_enb in f.standby_enbs:
            raise RuntimeError("pemdb1.capacity_shift: from_enb 가 standby_enbs 에 "
                               f"들어 있다 ({f.from_enb}) — A 를 끄면 내줄 부하가 없다")
        return f


SHIFT = ShiftCfg()


def set_shift_cfg(cfg: dict | None) -> ShiftCfg:
    """`pemdb1.capacity_shift` 를 걸어 둔다."""
    global SHIFT
    SHIFT = ShiftCfg.from_cfg(cfg)
    return SHIFT


def shift_pick(start_minute_epoch: int, pool: tuple[str, ...]) -> str | None:
    """이번 주입이 켤 B — **시작 시각의 `SHIFT_TOTAL` 분 칸 번호로 돌린다.**

    한 시간에 두 번 돌 수 있다(사람 요청). 같은 B 를 다시 쓰면 graphio 의
    무호 판정(D4)이 걸린다 — B 가 꺼지는 순간 **직전 60분 호 수 중앙값**이
    `min_baseline_calls`(20) 이상이면 무호다. 한 번만 켜면 그 60분 중 20분만
    호가 있어 중앙값이 0 이지만, 두 번이면 40분이 되어 넘는다.

    칸 번호로 돌리면 같은 B 가 다시 걸리려면 시작이 `len(pool)` 칸(4개면
    120분 이상) 떨어져야 하고, 그때 앞 회차의 켜진 구간은 이미 60분 창 밖이다.
    **상태가 없다** — DB 이력을 세지 않으므로 재기동해도 같은 B 가 나온다.
    fault_id 로 돌리지 않는 이유는 그 번호를 모든 장애가 함께 쓰기 때문이다
    — 사이에 다른 장애가 끼면 같은 B 가 연달아 걸릴 수 있다.
    """
    if not pool:
        return None
    return pool[(start_minute_epoch // SHIFT_TOTAL) % len(pool)]


def _shift_elapsed(sc) -> int:
    return int(getattr(sc, "_minute", sc.start_minute)) - sc.start_minute


def shift_split(sc) -> float:
    """지금 A 로 온 호 중 B 로 넘길 몫. B 개통 전에는 0."""
    return SHIFT_SPLIT if _shift_elapsed(sc) >= SHIFT_RISE else 0.0


def shift_congestion(sc) -> float:
    """A 의 혼잡 강도 — 오르다가 B 개통 뒤 `SHIFT_RECOVER` 분에 걸쳐 내려간다."""
    e = _shift_elapsed(sc)
    if e < SHIFT_RISE:
        lv = min(1.0, (e + 1) / SHIFT_RAMP)
    else:
        lv = max(0.0, 1.0 - (e - SHIFT_RISE + 1) / SHIFT_RECOVER)
    return lv * sc.peak


def visible_level(sc, minute: int) -> float:
    """판정 하한을 얼마나 채우나 0~1 — **장애가 서는 모양을 따른다.**

    강도(`peak`)로 나누는 이유: 사람이 강도 0.3 으로 넣었다고 대상 셀의
    사람이 30% 만 보여야 하는 것은 아니다 — 약한 장애도 판정할 표본은
    있어야 한다. 24번은 "`SHIFT_RAMP` 분에 걸쳐 몰린다"가 파형이라 그 국면을 따른다.
    """
    if sc.kind == "capacity_shift":
        sc._minute = minute
        return min(1.0, (_shift_elapsed(sc) + 1) / SHIFT_RAMP)
    return min(1.0, sc.intensity(minute) / max(sc.peak, 1e-9))


@booster("capacity_shift")
def _capacity_shift_load(sc, intensity) -> float:
    """A 가중 — `SHIFT_RAMP` 분에 걸쳐 `congestion_load` 까지 오르고 **창 끝까지 유지한다.**

    사람이 몰린 것은 B 가 개통해도 그대로다. 달라지는 것은 그 사람들이
    어느 셀에 붙느냐이고, 그것은 `PemdbGen._shifter` 가 정한다 — 그래서
    B 개통 뒤 A 의 실제 부하는 `1.8 × 0.4 = 0.72` 배로 평시보다 낮아진다.
    """
    e = _shift_elapsed(sc)
    frac = min(1.0, (e + 1) / SHIFT_RAMP)
    return 1.0 + (FAULT.congestion_load - 1.0) * frac


@plan_mutator("capacity_shift")
def _capacity_shift(plan, sc, intensity, rng, cell) -> bool:
    """A 의 혼잡 — 14번과 **같은 식**(`_congest`)을 국면 강도로 건다.

    식을 따로 두지 않는다. 같은 현상(용량 포화)을 두 식으로 만들면 14번과
    24번의 A 가 다른 모양으로 나빠지고, graphio 가 같은 원인으로 읽을 근거가
    흔들린다. B 에는 걸지 않는다 — 증설된 쪽은 여유가 있다.
    """
    lv = shift_congestion(sc)
    if lv <= 0.0:
        return False
    return _ran_congestion(plan, sc, lv, rng, cell)


# --------------------------------------------------------------------------
# 앱 등급 — 도메인 패턴 (확정대장 §12.3 · OPEN-45 "gen 이 소유한다")
# --------------------------------------------------------------------------

#: 등급 → 도메인 패턴 — **앱 사전(`CM_APP_CODE`)이 비었을 때의 대체 경로다.**
#: 사전이 있으면 21·22번은 사전의 앱·그룹으로 고른다(`generator.
#: _bind_scenario_targets`). DB 없이 도는 테스트가 이 경로를 쓴다.
#:
#: 처음 판본: **XDR 에 `App_Group_Code` 가 없어서** 질의명·Host 로
#: 등급을 정한다. 한국 실측 표본(`profile.json`)과 일본 사업자를 함께 적는다
#: — 프로파일이 일본으로 바뀌어도(OPEN-42) 이 표를 다시 쓰지 않게.
#: 패턴에 안 걸리는 도메인은 **등급 없음**이고 다른 등급에 섞지 않는다.
APP_CLASS_PATTERNS: dict[str, tuple[str, ...]] = {
    "PAY": ("pay", "toss", "kbcard", "bank", "d-barai", "merpay", "aupay"),
    "VIDEO": ("cdn", "ytimg", "googlevideo", "akamai", "cloudfront", "phinf",
              "pstatic", "kwimgs", "yximgs", "abema", "tver", "nicovideo"),
}


def app_class_hit(domain: str, cls: str) -> bool:
    d = (domain or "").lower()
    return any(p in d for p in APP_CLASS_PATTERNS[cls])


def host_site(domain: str) -> str:
    """`img1.kakaocdn.net` → `kakaocdn` — **호스트 사이트** (확정대장 §12.2).

    `co.kr`·`co.jp` 같은 2단 접미는 한 칸 더 들어간다. IP 표기면 그대로다.
    """
    parts = [p for p in (domain or "").lower().strip(".").split(".") if p]
    if len(parts) < 2 or all(p.isdigit() for p in parts):
        return domain
    if len(parts) >= 3 and parts[-2] in ("co", "or", "ne", "go", "ac", "com"):
        return parts[-3]
    return parts[-2]


# --------------------------------------------------------------------------
# J-SC-21 · cdn_degradation — 동영상 CDN 서버측 열화
# --------------------------------------------------------------------------


def _app_session_broken(plan, rng, intensity: float) -> None:
    """앱 장애 세션 공통 — **느리고, 끊기고, 다시 시도한다**(사람 요청 2026-10-28).

    - RTT: down ×(1+6I) · up ×(1+4I) — 응답이 눈에 띄게 늦다
    - 처리율: payload 축소 + down 재전송
    - 연결: 25%·I 는 도중 RST, 그다음 15%·I 는 연결 자체가 안 선다
    - 재시도: 60%·I 의 세션이 1~(1+3I)개의 세션을 더 연다 — 장애가
      나면 세션 수가 **오히려 는다**. 사용자가 새로고침하고 앱이 재연결한다
    """
    _congested_rtt(plan, rng, intensity)
    _congested_rtt(plan, rng, intensity, "Uplink")
    plan.scale("Traffic_Total_Dnlink_PayloadSize", max(0.2, 1.0 - 0.6 * intensity))
    _down_loss(plan, rng, intensity, 0.02, 0.04, 0.12)
    r = rng.random()
    if r < 0.25 * intensity:
        plan.reset = True
    elif r < 0.40 * intensity:
        plan.syn_fail = True
    if rng.random() < 0.6 * intensity:
        plan.retries = rng.randint(1, 1 + int(3 * intensity))


@mutator("cdn_degradation")
def _cdn_degradation(rec, sc, intensity, rng, cell) -> bool:
    """CDN 서버가 **느리게 답한다** — HTTP 응답시간 상승 + 일부 5xx.

    `external_server`(6번)는 5xx 하나로 신호를 낸다. 이쪽은 **서버 처리
    지연**이 주 신호이고 5xx 는 곁가지다(타임아웃 끝에 504). 대상은 도메인
    하나가 아니라 **호스트 사이트**(`kakaocdn`)다 — 같은 사업자의 여러 엣지
    호스트가 함께 느려진다(§12.2 "IP 대역 대신 `HOST_SITE`").

    transport 쪽(RTT·처리율·끊김·재시도)은 `_cdn_degradation_plan` 이 본다.
    처음에는 RTT 를 두지 않았다(경로 지연 `R-09b` 와 가르려고) — 사람이
    "RTT 도 크게, 끊기고, 세션도 많이" 로 정했다(2026-10-28).
    """
    if rec.series != "http":
        return False
    if sc.target.app_id:
        if rec.get("App_ID") != sc.target.app_id:
            return False
    elif not _domain_hit(rec, sc):
        return False
    if rng.random() >= intensity * 0.85:
        return False
    req = rec.num("RequestTime")
    if req > 0:
        rec.set("ResponseTime",
                f"{req + rng.uniform(0.5, 0.5 + 3.0 * intensity):.6f}")
    if rng.random() < 0.10 * intensity:
        rec.set("StatusCode", 504)
    return True


@plan_mutator("cdn_degradation")
def _cdn_degradation_plan(plan, sc, intensity, rng, cell) -> bool:
    """그 앱 세션이 망가진다 — `_app_session_broken`.

    **셀이 아니라 앱으로 고른다** — 그래서 대상 장비 아래 여러 셀에 흩어진다.
    """
    if sc.target.app_id:
        if plan.text("App_ID") != sc.target.app_id:
            return False
    else:
        dom = sc.target.domain
        if dom and dom not in plan.text("ClientHelloDomain"):
            return False
    if plan.outcome == "stall":
        return False
    if rng.random() >= intensity * 0.85:
        return False
    _app_session_broken(plan, rng, intensity)
    return True


@app_booster("cdn_degradation")
def _cdn_app_load(sc, intensity, app) -> float:
    """대상 스트리밍 앱 세션을 최대 3배로 — 저녁 시청이 몰린 모양."""
    return 1.0 + 2.0 * intensity if app.idx == sc.target.app_id else 1.0


# --------------------------------------------------------------------------
# J-SC-22 · pay_dns_delay — 결제 앱 응답 지연 (DNS 기인)
# --------------------------------------------------------------------------


@mutator("pay_dns_delay")
def _pay_dns_delay(rec, sc, intensity, rng, cell) -> bool:
    """**결제 등급** 도메인의 DNS 가 느리다 — 대부분 지연, 일부만 실패.

    `dns_degradation`(5번)은 전 도메인 실패가 주 신호다. 이쪽은 결제
    도메인만(`APP_CLASS_PATTERNS["PAY"]`) **응답시간**이 오른다 — 결제는
    몇 초만 늦어도 체감이 무너지고(스펙 §5.2 대기 임계 800ms), 실패까지
    가지 않아도 장애다. 실패를 15% 섞는 것은 지연이 길어지면 실제로
    타임아웃이 나기 때문이다.
    """
    if rec.series != "dns":
        return False
    q = (rec.get("QueryName") or "").lower()
    if sc.target.app_hosts:
        q = q.lstrip(".")
        if not any(q == h or q.endswith("." + h) for h in sc.target.app_hosts):
            return False
    elif not app_class_hit(q, "PAY"):
        return False
    if rng.random() >= intensity * 0.85:
        return False
    if rng.random() < 0.15 * intensity:
        _dns_fail(rec, rng, intensity)
        return True
    req = rec.num("RequestFirstTime")
    if req > 0:
        rec.set("ResponseTime",
                f"{req + rng.uniform(0.3, 0.3 + 2.5 * intensity):.6f}")
    return True


@app_booster("pay_dns_delay")
def _pay_app_load(sc, intensity, app) -> float:
    """결제 앱 세션을 최대 6배로 — 점심·퇴근길 결제가 몰리는 시간대.

    평시 결제 비중이 약 5% 라 대상 SGW 하나에서는 분당 몇 건이다. 배율이
    없으면 DNS 지연을 넣어도 `Q-07`(PAY 대기 불량) 분모가 20 에 못 미친다.
    """
    return 1.0 + 5.0 * intensity if app.group == "PAY" else 1.0


@plan_mutator("pay_dns_delay")
def _pay_session(plan, sc, intensity, rng, cell) -> bool:
    """결제 앱 세션도 망가진다 — DNS 만 늦으면 체감이 약하다(사람 결정).

    처음에는 DNS 기인이라 RTT 를 정상으로 두었다(원인 가르기). 사람이
    "RTT 도 크게, 끊기고, 세션도 많이" 로 정했다(2026-10-28) — 결제가
    안 되면 사용자는 몇 번이고 다시 누른다.
    """
    if plan.text("App_Group_Code") != "PAY":
        return False
    if plan.outcome == "stall":
        return False
    if rng.random() >= intensity * 0.85:
        return False
    _app_session_broken(plan, rng, intensity)
    return True


# --------------------------------------------------------------------------
# J-SC-23 · event_crowd — 대형 이벤트 일시 군중 혼잡 + 업링크
# --------------------------------------------------------------------------


@booster("event_crowd")
def _event_crowd_load(sc, intensity) -> float:
    """활성 가입자 **3배**(확정대장 §11.1). 14번 러시(1.8배)와 같은 장치다.

    추첨 비중만 올린다 — 호 대부분은 `floor_moved_kinds` 의 하한이 평소 호에서 옮겨 온다.
    """
    return 1.0 + 2.0 * intensity


@app_booster("event_crowd")
def _event_app_load(sc, intensity, app) -> float:
    """행사장에서는 **SNS 가 몰린다** — 사진·영상 업로드(§11.1 요구)."""
    return 1.0 + 2.0 * intensity if app.group == "SNS" else 1.0


#: SNS 사진 한 장의 업로드 바이트 범위 — 🔴 초기값. 스마트폰 12MP 사진의
#: HEIC·JPEG 일반 크기(공개 사양 기준 추정, 실측 아님). graphio 업링크 속도
#: 표본 하한(`min_speed_bytes` 1MB) 위에 있지만 **그 문턱에 맞춘 값이 아니다.**
#: 짧은 영상(수십 MB)은 넣지 않는다 — 몇 세션이 셀 업링크를 독점해 군중
#: 모양이 깨진다 (2026-11-03 #29 (가) · 조이 M-3).
SNS_PHOTO_BYTES = (1.5e6, 4.0e6)


def _sns_photo(plan, rng) -> None:
    """SNS 세션에 사진 한 장 업로드를 더한다. **처리율은 바꾸지 않는다.**

    패킷은 기존 바이트/패킷 비로, `ThroughputTime` 은 기존 시간/바이트 비로
    늘린다 — 처리율은 변형기가 이미 정한 (1+I)/(1+3I) 배 그대로다. Q-09 가
    서는지는 혼잡 세기에 달려 있고 업로드 크기가 정하지 않는다. 재전송은 뒤의
    `pay` 가 새 패킷 수를 읽으므로 따라온다. 패킷이 0 이면 바이트/패킷 비가
    없으므로 더하지 않는다.
    """
    size = plan.get("Traffic_Total_Uplink_PayloadSize")
    pkts = plan.get("Traffic_Total_Uplink_PayloadPacketCount")
    t = plan.get("Traffic_Total_Uplink_ThroughputTime")
    if size <= 0 or pkts <= 0:
        return
    add = rng.uniform(*SNS_PHOTO_BYTES)
    plan.set("Traffic_Total_Uplink_PayloadSize", size + add)
    plan.set("Traffic_Total_Uplink_PayloadPacketCount", pkts + add / (size / pkts))
    plan.set("Traffic_Total_Uplink_ThroughputTime", t + add * (t / size))


def _crowd_uplink(plan, rng, intensity: float) -> None:
    """올리는 양을 늘리고 up 처리시간·RTT·재전송을 올린다."""
    up = 1.0 + 1.0 * intensity
    plan.scale("Traffic_Total_Uplink_PayloadSize", up)
    plan.scale("Traffic_Total_Uplink_PayloadPacketCount", up)
    plan.scale("Traffic_Total_Uplink_ThroughputTime", 1.0 + 3.0 * intensity)
    if plan.text("App_Group_Code") == "SNS":
        _sns_photo(plan, rng)
    _congested_rtt(plan, rng, intensity, "Uplink")
    pay = plan.get("Traffic_Total_Uplink_PayloadPacketCount")
    rate = clip(rng.uniform(0.02, 0.04) + 0.08 * intensity, 0.0, 0.30)
    plan.set("Traffic_Total_Uplink_RetransPacketCount", int(pay * rate))


def _crowd_choke(plan, rng, intensity: float) -> None:
    """혼잡 몫 — 셀의 무선 자원이 바닥나 메시지도 잘 안 간다(2026-11-06 사용자 결정).

    up 은 **올리려던 양을 그대로 두고 시간을 늘린다** — graphio 업링크 판정이 분당 1MB 이상 올린
    행만 보기 때문이다(`min_speed_bytes`). 그래서 처리시간이 분당 60초를 넘을 수 있다. 실제 망이면
    100kbps 로는 분당 750KB 밖에 못 올린다. down 은 시간을 두고 양을 줄인다(`_congest` 와 같다).
    down RTT 는 올리지 않는다 — `xp_agg._judge` 가 down RTT 불량인 행을 업링크 불량에서 뺀다
    """
    _crowd_uplink(plan, rng, intensity)
    size = plan.get("Traffic_Total_Uplink_PayloadSize")
    if size > 0:
        plan.set("Traffic_Total_Uplink_ThroughputTime",
                 size * 8 / rng.uniform(*FAULT.crowd_up_bps) * 1e6)
    size = plan.get("Traffic_Total_Dnlink_PayloadSize")
    t = plan.get("Traffic_Total_Dnlink_ThroughputTime")
    if size > 0 and t > 0:
        can = rng.uniform(*FAULT.crowd_dn_bps) * t / 1e6 / 8
        plan.scale("Traffic_Total_Dnlink_PayloadSize", min(1.0, can / size))


@plan_mutator("event_crowd")
def _event_crowd(plan, sc, intensity, rng, cell) -> bool:
    """**업링크가 먼저 무너진다** — 공연장에서 다 같이 사진·영상을 올린다.

    몰린 사람의 일부는 100kbps 안팎으로 무너진다(`_crowd_choke`). 세션이 아니라 사람으로 고른다 —
    "그 사람이 안 올라간다"가 서려면 분마다 좋았다 나빴다 하면 안 된다. 나머지는 아래 모양이다.

    부하 3배만으로는 `Q-09`(업링크 무선 불량)가 0 이다(§11.1.1). 그래서
    up 축을 직접 넣는다:

    - up payload ↑ (업로드 증가 — 요구 자체)
    - up `ThroughputTime` 은 **더 크게** ↑ — 둘을 같이 올리면 처리율이
      그대로다(`g-sc-16` 의 함정의 거울상)
    - up RTT ↑ · up 재전송 ↑ — 상행 무선 자원 포화

    down 은 **얕게만** 내린다 — "down 보다 up 열화가 두드러질 것"(조건 5).
    ⚠️ `g-sc-16` 이 금지한 up 축을 의도적으로 쓰는 유일한 시나리오다 —
    `R-09d`(양 축 동시)와 경쟁하는지는 graphio 쪽 확인 대상이다(OPEN-39).
    """
    if plan.outcome == "stall":
        return False
    if congested_user(plan.imsi, sc.id, intensity, FAULT.crowd_user_share):
        _crowd_choke(plan, rng, intensity)
        return True
    if rng.random() >= intensity * 0.85:
        return False
    _crowd_uplink(plan, rng, intensity)
    plan.scale("Traffic_Total_Dnlink_PayloadSize", max(0.6, 1.0 - 0.2 * intensity))
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
    # **단절 구간이면 전부 막는다**(2026-11-04 사용자 결정 "그 기지국은 콜이 없어야 한다").
    # 전에는 `rng < intensity` 라 0.70 단계에서 30% 가 새어 나와 세션까지 열었다. 그 eNB 는
    # 같은 분에 다운 집합(`node_down_set`)에도 들어 기존 세션이 끊긴다 — 둘이 같은 경계를 본다
    return True


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
    # **연결된 호의 절단이다** (2026-10-28 · `callflow.drop`). 정답지의 S1AP
    # `200`(Transport Resource Unavailable)은 전부 서비스 호가 **붙은 뒤**
    # eNB 요청으로 끊긴 꼴이고(First msg 18 · Last msg 23 · `rreq = rel`),
    # 전 구현처럼 Context Setup(msg 9) 실패로 나온 호는 없다.
    from . import callflow
    return callflow.drop(rec, 200, rng)


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
    # **하류를 만들지 않는다** — SC-03 의 ESM 30 완화는 그 시나리오에만
    # 건다(2026-10-22 사용자 조건 · `test_downstream_nas_relaxation_...`)
    if rng.random() < intensity and _s11_timeout(rec, rng, downstream=False):
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
    """특정 APN 의 정의·프로파일 오류(`APN_CONFIG_ERROR`) — **전 MME·전 SGW** 에 걸린다 (R-07).

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
    # **`callflow` 명세가 적는다** (2026-10-28). 두 카운터는 First 가 아니라
    # **구간 Cause** 를 센다(`cell_agg.bake_apn`). 정답지에서 ESM `27` 은 MME
    # 가 직접 낸 거절이 아니라 **S11 `78`(Missing or unknown APN)의 하류**
    # 로만 나온다(Attach 4호) — 전 구현은 ESM `27` 을 First 로 적었다.
    #   ① S11 78 → ESM 27   `ESM27` 카운터
    #   ② S11 93 → ESM 33   `S11_93` 카운터
    from . import callflow
    cause = 78 if rng.random() < 0.60 else 93
    if str(rec.get("call_type")) == "1":
        return callflow.inject(rec, "s11_reject", rng, cause=cause)
    return callflow.inject(rec, "s11_reject_svc", rng, cause=cause)


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
    _congested_rtt(plan, rng, intensity)
    plan.scale("Traffic_Total_Dnlink_ThroughputTime", 1.0 + 1.9 * intensity)
    return True


# --------------------------------------------------------------------------
# 시나리오 구성
# --------------------------------------------------------------------------


#: **분모가 패킷인 지표.** 이 지표를 트리거로 쓰는 셀 축 시나리오만
#: `traffic_weight` 하한(`pemdb1.WEIGHT_FLOOR`)을 받는다.
#:
#: graphio `metrics/kpi.py:178·184` 가 `U-12`·`U-14` 의 `denom` 을
#: `paypkt_dn` 으로 선언하고, `detect/detector.py:832` 가 그 축의
#: `min_denom` 을 **800** 으로 잡는다 — 셀당 U-Plane 표본이 얇아 30분 창
#: 에서도 미달이 나오는 자리다.
#:
#: **호 수 분모는 넣지 않는다.** `C-01`~`C-05`·`N-02`·`PSF` 는 `min_sample`
#: 기본값(20)이고 셀당 호가 분당 수 건이라 창 확대로 찬다 — 세션 31개인
#: 셀도 호는 수백 건이다. 거기에 하한을 걸면 **불필요한 쏠림**만 생긴다
#: (그 하한이 걸려 있던 동안 정답 셀 교체가 4개가 아니라 21개였다).
PACKET_DENOM_KPIS = frozenset({"U-12", "U-14"})


def _needs_weight_floor(*kinds: str) -> bool:
    """이 kind 들 중 하나라도 **패킷 분모** 지표를 트리거로 쓰는가.

    `KIND_SPECS` 에서 끌어온다 — kind→지표 대응을 호출부에 적으면 사본이
    생기고, 시나리오의 `expect_kpi` 를 고칠 때 하한이 따라오지 않는다.
    """
    return any(KIND_SPECS.get(k, {}).get("expect_kpi") in PACKET_DENOM_KPIS
               for k in kinds)


def _cells_in_one_sgg(cells: list[Cell], rng: random.Random,
                      want: int = 30,
                      eligible: set[str] | None = None
                      ) -> tuple[list[Cell], str]:
    """한 시군구 안에서 셀을 고른다 — 지리적으로 뭉치는(Cluster) 열화용.

    `eligible`(관측 가능한 셀 키)이 오면 **추첨은 그대로 두고 미달 셀만
    교체한다.** 후보 풀 자체를 좁히면 시군구별 셀 수가 달라져 `rng.choice`
    가 **다른 시군구**를 고르고, 그러면 정답 셀이 통째로 바뀐다 — 실측으로
    40개 중 유지 0개였다. 바뀌어야 하는 것은 "미달 셀이 빠지는 것"뿐이다.

    교체분은 같은 시군구의 관측 가능한 셀에서 **목록 순서대로** 가져온다
    (난수를 더 쓰지 않는다 — 그러면 뒤따르는 추첨이 전부 밀린다).
    """
    by_sgg: dict[str, list[Cell]] = {}
    for c in cells:
        if c.geo_status == "MAPPED":
            by_sgg.setdefault(c.adm_sgg_cd, []).append(c)
    candidates = [k for k, v in by_sgg.items() if len(v) >= max(6, want // 3)]
    if not candidates:
        candidates = list(by_sgg)
    code = rng.choice(candidates)
    group = by_sgg[code]
    picked = rng.sample(group, min(want, len(group)))
    if eligible is not None:
        keep = [c for c in picked if c.cell_key in eligible]
        if len(keep) < len(picked):
            chosen = {c.cell_key for c in picked}
            spare = [c for c in group
                     if c.cell_key in eligible and c.cell_key not in chosen]
            keep += spare[: len(picked) - len(keep)]
        picked = keep
    return picked, code


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
                   want: int = 18, half_angle: float = 25.0,
                   anchor: Cell | None = None) -> tuple[Cell, list[Cell]]:
    """앵커 + **방위각 ±25° 안**의 근접 셀 — `CORRIDOR` 를 만들기 위한 제약.

    `detector.geo_pattern()` 이 `CORRIDOR` 를 주려면 `elong > 3.2` AND
    `radius > 2.5km` AND 셀 ≥ 5 다. "GPS 최근접 N셀"은 앵커 주위 **등방
    분포**가 되어 `CLUSTER` 로 떨어지고(fit 1.0 → 0.8) `EV-11.pattern ==
    "CORRIDOR"` 통과 조건이 서지 않는다. 그래서 방위를 하나 뽑아 그 띠 안에서만
    고른다 — 선형 구조(철도·고속도로 연변 셀)의 실제 모양이기도 하다.

    띠가 너무 좁아 셀이 모자라면 각도를 두 번 넓힌다. **빈손으로 돌려주지
    않는다** — 대상이 없으면 시나리오가 통째로 비고, 그것은 "주입했는데 아무
    일도 없었다"로 나타나 원인을 찾기 어렵다.

    ``anchor`` 를 주면 그 셀을 앵커로 쓴다 — 지도에서 eNB 를 찍은 경우다.
    """
    from .geo_lite import haversine_km

    mapped = [c for c in cells if c.geo_status == "MAPPED"]
    if not mapped:
        return cells[0], []
    if anchor is None or anchor.geo_status != "MAPPED":
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


#: 15번 노선 구간의 셀 수 — 시작 값은 `corridor_cells` 때의 규모(15셀)이고, 모양이 안 나오면 상한까지
#: 늘린다. 초기값
ROUTE_SEG_CELLS = 15
ROUTE_SEG_MAX_CELLS = 30
#: graphio `detector.geo_pattern` 의 CORRIDOR 조건(`radius > 2.5` · `elong > 3.2`)에 곱하는 여유.
#: 판정은 **이벤트가 선 셀만** 보므로 대상 전체가 문턱에 걸치면 일부 셀이 빠질 때 모양이 깨진다. 초기값
ROUTE_SEG_MARGIN = 1.2
CORRIDOR_RADIUS_KM = 2.5
CORRIDOR_ELONG = 3.2


def corridor_shape(cells: list[Cell]) -> tuple[float, float]:
    """셀 묶음의 (반경 km, 길이비) — graphio `detector.geo_pattern` 과 **같은 식**이다.

    판정을 여기서 다시 하려는 것이 아니라, 구간을 고를 때 그 판정에 걸릴 모양인지 미리 재는 것이다.
    """
    from .geo_lite import haversine_km

    lat = sum(c.latitude for c in cells) / len(cells)
    lon = sum(c.longitude for c in cells) / len(cells)
    radius = max(haversine_km(lat, lon, c.latitude, c.longitude) for c in cells)
    kx = 111.320 * math.cos(math.radians(lat))
    xs = [(c.longitude - lon) * kx for c in cells]
    ys = [(c.latitude - lat) * 111.0 for c in cells]
    sxx = sum(x * x for x in xs) / len(xs)
    syy = sum(y * y for y in ys) / len(ys)
    sxy = sum(x * y for x, y in zip(xs, ys)) / len(xs)
    tr, det = sxx + syy, sxx * syy - sxy * sxy
    disc = max(0.0, tr * tr / 4 - det)
    return radius, math.sqrt((tr / 2 + math.sqrt(disc)) / max(1e-9, tr / 2 - math.sqrt(disc)))


def geo_shape(cells: list[Cell]) -> str:
    """셀 묶음의 지리 모양 — graphio `detect/detector.py:geo_pattern` 과 **같은 판정**이다(2026-11-07 · B안).

    정답지 `expect_geo` 는 graphio 가 대상 셀을 잰 모양과 같아야 한다. 생성기는 graphio 를 import
    하지 않으므로 식을 옮겨 둔다 — 문턱(0.9·2.5·3.2·3.0·60km)과 경도 환산(`111.320 × cos(위도)` ·
    `graphio.geo.km_per_deg_lon`)을 바꾸면 **양쪽을 함께** 바꾼다. 좌표 없는 셀은 뺀다.
    """
    from .geo_lite import haversine_km

    pts = [c for c in cells if c.geo_status == "MAPPED"]
    if not pts:
        return "UNKNOWN"
    if len(pts) == 1:
        return "POINT"
    lat = sum(c.latitude for c in pts) / len(pts)
    lon = sum(c.longitude for c in pts) / len(pts)
    radius = max(haversine_km(lat, lon, c.latitude, c.longitude) for c in pts)
    if radius < 0.9:
        return "POINT"
    _r, elong = corridor_shape(pts)
    if radius > 60:
        return "WIDE" if len(pts) >= 12 else "SCATTER"
    if elong > CORRIDOR_ELONG and radius > CORRIDOR_RADIUS_KM and len(pts) >= 5:
        return "CORRIDOR"
    return "CLUSTER" if radius <= 3.0 else "SCATTER"


def route_segment(routes: list, rng: random.Random, want: int = ROUTE_SEG_CELLS,
                  limit: int = ROUTE_SEG_MAX_CELLS) -> tuple[list[Cell], dict] | None:
    """노선의 **연속 구간** — 15번 대상(2026-11-05 사용자 승인).

    `corridor_cells` 는 노선을 보지 않아 F-133 대상이 노선 셀과 0/15 로 겹쳤고 반경 1.07km 에
    몰렸다(앵커 300개 시뮬레이션에서 CORRIDOR 조건을 넘는 경우 2%). 여기서는 노선 칸을 순서대로
    잘라 쓴다 — 가입자가 실제로 지나는 셀이고, 이웃 칸끼리가 전환 쌍이다.

    시작 칸은 `rng` 로 섞고, `want` 셀부터 한 칸씩 늘려 모양(`corridor_shape`)을 넘는 가장 짧은 구간을
    고른다. 순환선이 아니면 끝을 넘지 않는다. **SGW 가 바뀌는 구간을 일부러 고르지 않는다** — 몇
    곳인지 `sgw_changes` 로 적기만 한다. 모양을 넘는 구간이 없으면 `None` 이다(호출부가 후퇴한다).
    """
    starts = [(ri, pos) for ri, rt in enumerate(routes) for pos in range(len(rt))]
    rng.shuffle(starts)
    need_r = CORRIDOR_RADIUS_KM * ROUTE_SEG_MARGIN
    need_e = CORRIDOR_ELONG * ROUTE_SEG_MARGIN
    for n in range(want, limit + 1):
        for ri, pos in starts:
            rt = routes[ri]
            if not rt.loop and pos + n > len(rt):
                continue
            idx = [(pos + i) % len(rt) for i in range(min(n, len(rt)))]
            seg: list[Cell] = []
            for i in idx:
                c = rt.cells[i]
                if c.geo_status == "MAPPED" and c.cell_key not in {x.cell_key for x in seg}:
                    seg.append(c)
            if len(seg) < 5:
                continue
            radius, elong = corridor_shape(seg)
            if radius <= need_r or elong <= need_e:
                continue
            names = [rt.stations.get(rt.station_of[i], rt.station_of[i])
                     for i in (idx[0], idx[-1])] if rt.station_of else []
            return seg, {
                "route_id": rt.id, "stations": names, "cells": len(seg),
                "sgw_changes": sum(a.sgw_id != b.sgw_id for a, b in zip(seg, seg[1:])),
                "radius_km": round(radius, 2), "elong": round(elong, 2),
            }
    return None


#: 15번 **범위(%)** — 노선 셀 중 몇 %에 넣나(2026-11-09 사용자 결정 · 강도를 범위로 바꾼다). 기본·하한·상한.
#: 셀 수는 `max(ROUTE_SEG_CELLS, round(노선 셀 × 범위))` 라 10% 여도 예전 회차(15셀)와 같은 규모다. 초기값
ROUTE_RANGE_DEFAULT = 50
ROUTE_RANGE_MIN = 10
ROUTE_RANGE_MAX = 100
#: 100% 미만일 때 조각 하나의 셀 수 — 역 1~2구간. 조각 사이에는 최소 한 역을 비운다. 초기값
ROUTE_PIECE_MIN = 3
ROUTE_PIECE_MAX = 6


def route_range(pct) -> int:
    """범위 값을 [하한, 상한] 정수 %로 — 못 읽으면 기본값."""
    try:
        v = round(float(pct))
    except (TypeError, ValueError):
        return ROUTE_RANGE_DEFAULT
    return max(ROUTE_RANGE_MIN, min(ROUTE_RANGE_MAX, v))


def _split(total: int, k: int, lo: int, hi: int | None, rng: random.Random) -> list[int]:
    """``total`` 을 ``k`` 개로 — 각각 ``lo`` 이상(``hi`` 가 있으면 이하), 나머지는 무작위로 얹는다."""
    out = [lo] * k
    left = total - lo * k
    while left > 0:
        room = [i for i in range(k) if hi is None or out[i] < hi]
        if not room:
            break
        out[rng.choice(room)] += 1
        left -= 1
    return out


def route_spread(routes: list, rng: random.Random, pct: int = ROUTE_RANGE_DEFAULT,
                 ) -> tuple[list[Cell], list[tuple[str, str]], dict] | None:
    """15번 대상 — 노선 하나에서 **범위(%)만큼의 셀을 떨어진 조각 여러 개**로 고른다(2026-11-09).

    시나리오 원문(PPT 9쪽 노선 스트립)은 노선 위 **여러 역에서 산발적으로** 나는 전환 정체다. 예전
    `route_segment` 는 연속 15셀 한 덩어리였다. 사용자 결정: 강도를 범위로 바꾸고, 100% 는 한 바퀴 전체,
    그 아래는 셀 3~6개(역 1~2개) 조각을 **사이에 최소 한 역을 비우고** 흩어 놓는다.

    * **역 단위로 자른다.** 칸 수로 간격을 두면 역 경계와 어긋나 "사이에 다른 역이 없는" 간격이 생겼다
      (범위 60% 에서 60회 중 40회). 조각은 역 1~2개이고, 3셀이 안 되면 이웃 역을 하나 붙인다.
    * 셀 수 목표 = ``max(ROUTE_SEG_CELLS, round(노선 셀 × pct))`` — 역 단위라 몇 셀 넘칠 수 있다.
      목표보다 적으면 간격을 줄여 조각을 넓힌다.
    * 조각 수는 간격(역 하나 이상)이 들어갈 만큼만이다. 범위가 크면(야마노테선 약 50% 초과) 간격이
      모자라 조각이 **역 2개보다 커진다** — 조각 크기보다 범위를 지킨다.
    * 노선 칸에는 같은 셀이 두 번 오는 자리가 있다(야마노테선 90칸 · 고유 88셀) — 처음 나온 칸만 쓴다.
    * 쌍은 조각 안의 이웃 칸끼리다. 한 바퀴 전체면 끝 칸과 첫 칸도 잇는다.
    * 모양을 맞춰 고르지 않는다 — 고른 셀 전체의 모양(`geo_shape`)을 그대로 적는다(정답지 `expect_geo`).

    반환은 ``(셀 목록, 쌍 목록, 정보)`` 이고, 노선이 없으면 ``None`` 이다.
    """
    if not routes:
        return None
    pct = route_range(pct)
    rt = routes[rng.randrange(len(routes))]
    first: dict[str, int] = {}
    for i, c in enumerate(rt.cells):
        first.setdefault(c.cell_key, i)
    order = sorted(first.values())
    seq = [rt.cells[i] for i in order]
    n = len(seq)
    st_all = (list(rt.station_of) if len(rt.station_of) == len(rt.cells)
              else [str(i) for i in range(len(rt.cells))])
    st = [st_all[i] for i in order]
    # 역 덩어리 — 이어진 같은 역 칸. 순환선이면 끝과 처음이 같은 역일 때 하나로 잇는다
    blocks: list[list[int]] = []
    for i in range(n):
        if blocks and st[blocks[-1][-1]] == st[i]:
            blocks[-1].append(i)
        else:
            blocks.append([i])
    if rt.loop and len(blocks) > 1 and st[blocks[0][0]] == st[blocks[-1][-1]]:
        blocks[0] = blocks.pop() + blocks[0]
    m = len(blocks)
    loop = rt.loop
    want = min(n, max(ROUTE_SEG_CELLS, round(n * pct / 100)))
    per = n / m
    want_st = min(m, math.ceil(want / per))                 # 목표 셀을 덮는 역 수(올림)
    whole = want >= n or want_st >= m
    name = lambda b: rt.stations.get(st[blocks[b][0]], st[blocks[b][0]])  # noqa: E731
    if whole:
        pieces_b = [list(range(m))]
    else:
        free = m - want_st
        k_max = min(want_st, free if loop else free + 1)
        k_min = -(-want_st // 2)                           # 역 2개 이하가 되는 최소 조각 수
        kk = rng.randint(min(k_min, k_max), k_max) if k_max >= 1 else 1
        sizes = _split(want_st, kk, 1, 2 if kk * 2 >= want_st else None, rng)
        inner = kk if loop else kk - 1
        gaps = _split(free, inner, 1, None, rng) if inner else []
        pos = rng.randrange(m) if loop else rng.randint(0, max(0, free - sum(gaps)))
        pieces_b = []
        for i, sz in enumerate(sizes):
            pieces_b.append([(pos + j) % m for j in range(sz)])
            pos += sz + (gaps[i] if i < len(gaps) else 0)

        def cells_of(pb):
            return sum(len(blocks[b]) for b in pb)

        def grow(i) -> bool:
            # 조각 i 를 뒤 간격 쪽으로 한 역 넓힌다 — 간격이 2역 이상일 때만(한 역은 남긴다)
            if not loop and i == len(pieces_b) - 1:
                return False
            if i >= len(gaps) or gaps[i] < 2:
                return False
            pieces_b[i].append((pieces_b[i][-1] + 1) % m)
            gaps[i] -= 1
            return True
        def grow_back(i) -> bool:
            # 앞 간격 쪽으로 한 역 — 뒤 간격이 한 역뿐일 때 쓴다
            j = i - 1 if i > 0 else (len(gaps) - 1 if loop else -1)
            if j < 0 or j >= len(gaps) or gaps[j] < 2:
                return False
            pieces_b[i].insert(0, (pieces_b[i][0] - 1) % m)
            gaps[j] -= 1
            return True
        # 3셀이 안 되는 조각은 이웃 역을 붙인다 → 목표 셀 수에 못 미치면 조각을 넓힌다
        for i in range(len(pieces_b)):
            while cells_of(pieces_b[i]) < ROUTE_PIECE_MIN and (grow(i) or grow_back(i)):
                pass
        idx = list(range(len(pieces_b)))
        while sum(cells_of(pb) for pb in pieces_b) < want:
            rng.shuffle(idx)
            if not any(grow(i) for i in idx):
                break
    cells: list[Cell] = []
    pairs: list[tuple[str, str]] = []
    info_pieces = []
    for pb in pieces_b:
        mine = [seq[i] for b in pb for i in blocks[b]]
        cells += mine
        pairs += [(a.cell_key, b.cell_key) for a, b in zip(mine, mine[1:])]
        if whole and loop and len(mine) > 1:
            pairs.append((mine[-1].cell_key, mine[0].cell_key))
        info_pieces.append({
            "stations": [name(pb[0]), name(pb[-1])], "station_cnt": len(pb), "cells": len(mine),
            "shape": geo_shape(mine),
            "sgw_changes": sum(a.sgw_id != b.sgw_id for a, b in zip(mine, mine[1:])),
        })
    mapped = [c for c in cells if c.geo_status == "MAPPED"]
    radius, elong = corridor_shape(mapped) if len(mapped) >= 2 else (0.0, 0.0)
    return cells, pairs, {
        "route_id": rt.id, "range_pct": pct, "route_cells": n, "cells": len(cells),
        "whole": whole, "pieces": info_pieces,
        "stations": [p["stations"][0] for p in info_pieces],
        "shape": geo_shape(cells),
        "sgw_changes": sum(p["sgw_changes"] for p in info_pieces),
        "radius_km": round(radius, 2), "elong": round(elong, 2),
    }


def _route_truth(sc) -> dict:
    """15번 정답지 덧붙임 — 고른 노선 구간과 이웃 칸 쌍. 노선을 못 쓴 대상이면 없다."""
    seg = sc.target.route_seg
    if not seg:
        return {}
    out = {"route_seg": dict(seg), "cell_pairs": [list(p) for p in sc.target.cell_pairs]}
    # 범위 배치(`route_spread`)는 모양을 맞춰 고르지 않는다 — 정답 모양은 **고른 셀 전체의 실제 모양**이다
    # (100% 는 고리라 CORRIDOR 가 아니다 · 2026-11-09). 이 키가 `as_truth` 의 `expect_geo` 를 덮는다
    if seg.get("range_pct") is not None and seg.get("shape"):
        out["expect_geo"] = seg["shape"]
        out["range_pct"] = seg["range_pct"]
    return out


#: 25번 출발 셀 세기 배율 범위 — 셀마다 해시로 고정한다(2026-11-13 사용자 승인). **초기값이다** — 결과를 보고
#: 바꾼다. 화면에서 "같은 시각, 같은 eNB, 셀마다 다른 정도"가 보이게 하려는 값이고 실측 근거는 없다
SON_GAIN_RANGE = (0.5, 1.0)
#: 25번 평소 호 비율(셀 `traffic_weight` ÷ 그 eNB 최대 셀)의 하한 — 배율에 곱한다. 하한이 없으면 한산한
#: 셀이 0 에 가까워져 사실상 꺼진다(2026-11-13 케이 결정). **초기값이다**
SON_LOAD_FLOOR = 0.5
#: 25번 자동 대상 후보 — 셀 `traffic_weight` 합 상위 eNB 수. 그 안에서 회차마다 돌려 고른다. **초기값이다**
SON_AUTO_TOP_ENBS = 20


def son_gain(src: list[Cell]) -> dict[str, float]:
    """25번 출발 셀별 세기 배율 — `SON_GAIN_RANGE` 해시 배율 × `SON_LOAD_FLOOR` 하한의 평소 호 비율.

    해시는 셀 키로만 정한다 — 같은 셀은 회차·틱·gen 재시작마다 같은 배율이다. 호 비율의 기준은 **그 eNB
    안의 최대 셀**이다. 다른 eNB 와 비교하면 eNB 가 둘일 때 한쪽 전체가 약해진다
    """
    import hashlib

    lo, hi = SON_GAIN_RANGE
    top: dict[str, float] = {}
    for c in src:
        top[c.enb_id] = max(top.get(c.enb_id, 0.0), float(c.traffic_weight or 0.0))
    out: dict[str, float] = {}
    for c in src:
        h = int(hashlib.sha1(f"son:{c.cell_key}".encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
        t = top.get(c.enb_id) or 0.0
        load = max(SON_LOAD_FLOOR, float(c.traffic_weight or 0.0) / t) if t > 0 else 1.0
        out[c.cell_key] = round((lo + (hi - lo) * h) * load, 3)
    return out


def son_near_enb(cells: list[Cell], enb: str) -> str | None:
    """``enb`` 와 사이트 중심이 가장 가까운 다른 eNB — 25번 "eNB 2개"의 둘째(2026-11-13). 좌표가 없으면 None."""
    from .geo_lite import haversine_km

    pos: dict[str, list[Cell]] = {}
    for c in cells:
        if c.geo_status == "MAPPED":
            pos.setdefault(c.enb_id, []).append(c)
    if enb not in pos:
        return None

    def mid(e: str) -> tuple[float, float]:
        m = pos[e]
        return sum(c.latitude for c in m) / len(m), sum(c.longitude for c in m) / len(m)

    la, lo = mid(enb)
    rest = [(haversine_km(la, lo, *mid(e)), e) for e in pos if e != enb]
    return min(rest)[1] if rest else None


def son_focus(cells: list[Cell], enbs: list[str], n: int) -> Target | None:
    """25번 대상 — **eNB 1~2개의 전 셀을 출발 셀**로, 출발 셀마다 도착 셀 ``n`` 곳(2026-11-13 사용자 결정).

    파라미터는 eNB 에 들어간다 — 그 eNB 의 셀이 함께 영향받는다. 전에는 노선 구간에서 **셀 하나**만 남겼다
    (11-09). 이웃 관계(셀 쌍)마다 실패가 몰린다는 설정 변경의 지문은 출발 셀마다 지킨다 — 한 출발 셀의
    실패는 그 셀의 도착 셀 1~``n`` 곳으로만 간다.

    * 도착 셀은 **거리순**이다 — 셀 마스터에 이웃 관계 표가 없다(리오 확인 · 11-13). 고른 eNB 의 셀은
      도착 셀에서 뺀다(같은 사이트 안 전환은 이 eNB 의 파라미터만으로 설명되지 않는다).
    * 좌표 없는 출발 셀은 쌍을 만들지 못한다 — 대상 셀에는 남지만 실패가 나지 않는다(숨기지 않는다).
    * 셀별 세기는 `son_gain`. **시작 분은 모든 셀이 같다.**

    대상 셀은 출발·도착 셀이다 — 절단은 도착 셀(Last)에 계상되므로 둘 다 정답 셀이다(9번과 같은 이유).
    UI 주입(`faults`)과 배치(`build_scenarios`)가 이 함수 하나를 쓴다. eNB 셀이 없으면 None.
    """
    import heapq

    from .geo_lite import haversine_km

    want = [str(e) for e in enbs if e]
    mine = set(want)
    src = sorted((c for c in cells if c.enb_id in mine), key=lambda c: c.cell_key)
    if not src:
        return None
    other = [c for c in cells if c.geo_status == "MAPPED" and c.enb_id not in mine]
    pairs: list[tuple[str, str]] = []
    for a in src:
        if a.geo_status != "MAPPED" or n <= 0:
            continue
        near = heapq.nsmallest(n, other, key=lambda c: (
            haversine_km(a.latitude, a.longitude, c.latitude, c.longitude), c.cell_key))
        pairs += [(a.cell_key, c.cell_key) for c in near]
    keys = {c.cell_key for c in src} | {b for _, b in pairs}
    return Target(cell_keys=keys, cell_pairs=pairs, son_enbs=tuple(want), son_gain=son_gain(src))


def busy_cluster(cells: list[Cell], rng: random.Random, want: int = 8,
                 anchor: Cell | None = None) -> list[Cell]:
    """**상위 트래픽 대역**에서 서로 가까운 셀 `want` 개 — 도심 역 혼잡의 자리.

    확정대장 §1.4.2 G-1 이 "상위 대역에서 고른다"고 정했다. 한산한 셀을
    고르면 ×1.8 을 해도 판정 가능 가입자(분모 20)를 못 채운다. 대역은
    `traffic_weight` 상위 3% 이고(대장 실측 217셀 ≈ 전체 2.8%), 앵커를 그
    중 상위 20 셀에서 뽑아 **같은 대역 안의 최근접**으로 채운다 — 대역 밖
    셀을 섞으면 그 셀이 표본 미달로 판정에서 빠진다.

    ``anchor`` 를 주면 그 셀 주변에서 고른다(지도에서 eNB 를 찍은 경우).
    """
    from .geo_lite import haversine_km

    mapped = sorted((c for c in cells if c.geo_status == "MAPPED"),
                    key=lambda c: -c.traffic_weight)
    if not mapped:
        return []
    band = mapped[:max(want * 4, len(mapped) * 3 // 100)]
    if anchor is None or anchor.geo_status != "MAPPED":
        anchor = rng.choice(band[:20])
    near = sorted((c for c in band if c.cell_key != anchor.cell_key),
                  key=lambda c: haversine_km(anchor.latitude, anchor.longitude,
                                             c.latitude, c.longitude))
    return [anchor] + near[:want - 1]


def area_cells(cells: list[Cell], rng: random.Random, want: int = 25,
               anchor: Cell | None = None) -> list[Cell]:
    """앵커 주변 **가까운 셀 `want` 개** — 한 지역(지진 피해 구역).

    시군구(ADM2) 축은 확정대장 §11.3 이 "범위 밖, CELL 군집으로 먼저"로
    정했다. 트래픽 대역을 가리지 않는 것이 `busy_cluster` 와 다르다 —
    지진은 한산한 셀도 같이 멈춘다.
    """
    from .geo_lite import haversine_km

    mapped = [c for c in cells if c.geo_status == "MAPPED"]
    if not mapped:
        return []
    if anchor is None or anchor.geo_status != "MAPPED":
        anchor = rng.choice(mapped)
    return sorted(mapped, key=lambda c: haversine_km(
        anchor.latitude, anchor.longitude, c.latitude, c.longitude))[:want]


def enb_cluster(cells: list[Cell], anchor: Cell, want: tuple[int, int] = (10, 15)
                ) -> list[Cell]:
    """앵커에서 가까운 eNB 부터 **셀을 통째로** 더해 `want` 범위에서 멈춘다
    (2026-11-03 · 16번 군집화).

    16번(앵커 경합)은 정답지가 `CLUSTER` 인데 gen 은 시군구 안 50셀을 흩어
    뿌렸다. 대상 셀 비중이 판정 단위(ADM3·메시)마다 3~5% 라 어느 단위에서도
    처리율 저하가 나오지 않았다(16r4 실측). 인접 eNB 몇 개의 10~15셀로 모은다.
    eNB 를 자르지 않는다 — 최소치(`want[0]`)를 넘기면 그 eNB 까지 넣고 멈추되,
    상한을 넘기게 하는 eNB 는 이미 최소치를 채웠으면 넣지 않는다.
    난수를 쓰지 않는다 — 앵커는 호출부가 정한다.
    """
    from .geo_lite import haversine_km

    lo, hi = want
    by_enb: dict[str, list[Cell]] = {}
    for c in cells:
        if c.geo_status == "MAPPED":
            by_enb.setdefault(c.enb_id, []).append(c)

    def dist(enb: str) -> float:
        c0 = by_enb[enb][0]
        return haversine_km(anchor.latitude, anchor.longitude, c0.latitude, c0.longitude)

    order = sorted(by_enb, key=lambda e: (e != anchor.enb_id, dist(e), e))
    out: list[Cell] = []
    for e in order:
        grp = sorted(by_enb[e], key=lambda c: c.cell_key)
        if out and len(out) >= lo and len(out) + len(grp) > hi:
            break
        out += grp
        if len(out) >= lo:
            break
    return out


def whole_enbs(cells: list[Cell], picked: list[Cell]) -> list[Cell]:
    """`picked` 가 걸친 eNB 의 **셀 전부** (2026-11-03 M-5).

    `area_cells` 는 거리순 셀이라 eNB 경계를 자른다. 지진(27)은 사이트 단위
    정지라 대상 밖에 남은 같은 eNB 셀이 호를 내면 D4 가 서지 않는다. 난수를
    더 쓰지 않으므로 다른 시나리오의 대상은 바뀌지 않는다.
    """
    enbs = {c.enb_id for c in picked}
    return [c for c in cells if c.enb_id in enbs]


def build_scenarios(
    cells: list[Cell],
    minutes: int,
    rng: random.Random,
    selected: str | list[str] = "all",
    lead_minutes: int | None = None,
    eligible: set[str] | None = None,
    nr_on=None,
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
    # **`eligible` 은 "관측 가능한 셀 키" 집합이다** (2026-10-26).
    #
    # 그 시나리오의 트리거 지표가 30분 창 안에 분모를 채우는 셀이고
    # (`denom.eligible`), 관측 불가한 셀에 장애를 넣으면 채점이 RCA 품질이
    # 아니라 "그 셀이 보였는가"를 재게 된다 — 실측으로 정답 40개 중 4개가
    # 어떤 창에서도 `U-12` 분모(패킷 800)를 못 채웠다.
    #
    # **풀을 좁히지 않고 추첨 뒤에 교체한다**(`_cells_in_one_sgg` 참조).
    # 그리고 **노드·전역 축에는 걸지 않는다** — 그쪽 분모는 수백 셀의 합이라
    # 미달이 생기지 않고, 좁히면 "그 노드의 셀 전부"라는 전제가 깨진다.
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
    # **하한은 트리거 지표의 분모 종류에 따라 건다**(`_needs_weight_floor`).
    # 전에는 셀 축 전체에 걸려 호 수 분모 시나리오(C-02·N-02)의 대상까지
    # 바뀌었고, 그쪽은 세션이 얇아도 호가 수백 건이라 **불필요한 쏠림**이었다.
    def _floor(*kinds: str):
        return eligible if (eligible and _needs_weight_floor(*kinds)) else None

    cluster_cells, cluster_sgg = _cells_in_one_sgg(
        cells, rng, 60, _floor("gw_resource", "ran_radio", "gw_node_down"))
    loss_cells, loss_sgg = _cells_in_one_sgg(
        cells, rng, 40, _floor("transport_loss"))
    nsa_cells, nsa_sgg = _cells_in_one_sgg(
        cells, rng, 60, _floor("nsa_nr_leg", "nsa_policy_restrict"))
    # **`nsa_cells` 를 재사용하지 않는다.** 확정대장 §9.8.6 요소 ① 이
    # `REUSE` 라고 적었는데, 같은 셀에 `g-sc-13`(NR 레그 미성립)과 함께
    # 걸리면 같은 분의 `N-02` 가 섞여 **`R-39` 의 ④ 조건("N-02 정상")이
    # 거짓이 된다.** 두 시나리오가 서로의 발동을 막는 모양이라 셀을 가른다.
    # 시군구 추첨은 그대로 두고(난수 순서 보존 — 뒤 시나리오 대상이 바뀌지
    # 않게) 그 첫 셀을 앵커로 **인접 eNB 군집**을 만든다(2026-11-03)
    _anchor_pick, anchor_sgg = _cells_in_one_sgg(
        cells, rng, 50, _floor("anchor_saturation"))
    # 군집은 NR 가능 eNB 로만 짓는다(`faults.build` 와 같다) — 추첨 뒤에 좁혀 난수 순서를 지킨다
    anchor_pool = [c for c in cells if nr_on(str(c.enb_id))] if nr_on is not None else cells
    anchor_cells = (enb_cluster(anchor_pool, _anchor_pick[0]) if _anchor_pick else [])
    compound_cells, compound_sgg = _cells_in_one_sgg(
        cells, rng, 40, _floor("partial_recovery", "dns_flap",
                               "compound_causal_failure"))
    overload_cells = by_mme[busy_mme][:80]

    # **CORRIDOR 는 걸러내지 않는다.** 방위각 ±25° 띠 안에서 고르므로
    # 미달 셀을 빼면 띠가 끊어져 `elong > 3.2` 가 서지 않고, 그러면
    # `EV-11.pattern == "CORRIDOR"` 통과 조건이 사라진다(그쪽 docstring).
    # 이 시나리오의 트리거 지표는 `denom.KPI_DENOM` 에 등록돼 있지 않아
    # 미등록으로 보고된다.
    anchor, corridor = corridor_cells(cells, rng, want=18)
    pairs = [(anchor.cell_key, c.cell_key) for c in corridor[:8]]
    # J-SC-25 의 SON 앵커 — **`rng` 를 더 소비하지 않는다.** 소비하면
    # 뒤에 고르는 시나리오의 대상이 전부 바뀌어 기존 채점 기준선이 흔들린다.
    # SC-09 셀은 뺀다 — 같은 셀에 둘이 걸리면 `R-17` 근거가 섞인다. 대상은 앵커 셀의
    # eNB 전 셀이다(2026-11-13 · `son_focus`). 띠(`son_band`)는 J-SC-15 를 비키는 데만 남는다
    sc09 = {anchor.cell_key} | {c.cell_key for c in corridor}
    son_anchor, son_band = corridor_cells(
        [c for c in cells if c.cell_key not in sc09],
        random.Random(f"son:{anchor.cell_key}"), want=18)
    # J-SC-14 의 혼잡 셀 8개 — 같은 이유로 `rng` 를 소비하지 않는다
    crowd = busy_cluster(cells, random.Random(f"crowd:{anchor.cell_key}"), want=8)
    # J-SC-15 의 코리도 — SC-09·J-SC-25 셀과 겹치지 않게 뺀다(같은 셀에 둘이
    # 걸리면 전환 증거가 섞인다)
    used = sc09 | {son_anchor.cell_key} | {c.cell_key for c in son_band}
    ho_anchor, ho_band = corridor_cells(
        [c for c in cells if c.cell_key not in used],
        random.Random(f"hoi:{anchor.cell_key}"), want=12)
    quake = whole_enbs(cells, area_cells(
        cells, random.Random(f"quake:{anchor.cell_key}"), want=25))
    venue = area_cells(cells, random.Random(f"event:{anchor.cell_key}"), want=8)

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
        spec("J-SC-16", "anchor_saturation",
             target=Target(cell_keys={c.cell_key for c in anchor_cells},
                           sgg_codes={anchor_sgg})),
        # 배치도 UI 주입과 같은 대상(`son_focus` · 앵커 셀의 eNB 전 셀) — 한쪽만 바꾸면 같은 kind 의 정답 셀이 갈린다
        spec("J-SC-25", "son_param_change",
             target=son_focus(cells, [son_anchor.enb_id],
                              int(KIND_SPECS["son_param_change"].get("focus_pairs") or 0))),
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
        # 목록 **끝**에 둔다 — 배치가 목록 순서로 시각을 나눠 주므로 중간에
        # 끼우면 뒤 시나리오의 차례가 전부 한 칸씩 밀린다
        spec("J-SC-14", "ran_congestion",
             target=Target(cell_keys={c.cell_key for c in crowd})),
        spec("J-SC-15", "ho_interruption",
             target=Target(cell_keys={ho_anchor.cell_key}
                           | {c.cell_key for c in ho_band},
                           cell_pairs=[(ho_anchor.cell_key, c.cell_key)
                                       for c in ho_band[:6]])),
        spec("J-SC-21", "cdn_degradation", target=Target()),
        spec("J-SC-22", "pay_dns_delay", target=Target()),
        spec("J-SC-23", "event_crowd",
             target=Target(cell_keys={c.cell_key for c in venue})),
        spec("J-SC-27", "earthquake",
             target=Target(cell_keys={c.cell_key for c in quake})),
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


# --------------------------------------------------------------------------
# 권장 지속시간 — gen-ui 주입 칸의 기본값 (2026-11-03 사용자 결정)
# --------------------------------------------------------------------------
#
# 주입 칸이 모든 시나리오에 30분 고정이었다. 시나리오마다 모양이 다르므로
# **모양에서 정한 권장값을 그대로 주입한다** — 여유를 더하지 않는다. 장애가
# 끝난 뒤 결과가 확정되기까지 걸리는 시간(확정 대기)은 주입 길이와 **따로**
# 보인다(사용자 지시: "권장만 주입하고 여유는 따로 표시").
#
# 모양 계산은 탐지가 몇 분을 요구하는지 모른다. 실측 정답 기록(`measured`)이 있으면 그것을 쓰고
# (`basis: "measured"`), 없으면 **모양 기준 초기값**이다(`basis: "shape"`).

#: 즉시형·점진형 공통 — 정점을 이만큼 유지해야 탐지(연속 조건 + 5분 창)와
#: L2 모양(지역별 표본 하한)이 선다. F-86 실측: 주입 3~4분 뒤 첫 이벤트
_PLATEAU_MIN = 5
#: 이보다 짧으면 정점이 탐지 확정 전에 끝난다(경고용 하한)
_MIN_FLOOR = 6
#: 주입 종료 → 최종 판정. graphio 의 해소 대기 5분 + 틱 지연 — 22번 실측 6~7분
SETTLE_MIN = 7
#: 탐지 최소 시간(`detect_min`)에 더하는 여유 — 창을 5분마다 미는 판정이 한 번 더 볼 몫이다. 초기값
DETECT_MARGIN_MIN = 5
#: 실측이 모양 계산보다 **짧을 때** 권장을 줄이는 데 필요한 정답 횟수. 늘리는 것은 1회로 바로 한다 —
#: 한 번 잰 값으로 줄이면 운에 좌우되고, 놓치는 쪽이 더 큰 손해다(2026-11-05 케이)
MEASURED_SHRINK_RUNS = 2


#: L2 사건이 서야 하는 kind — 광역 모양(Core·DNS·CDN·APN·재해)의 장애. 권장 길이와 시작 시간대는 L2
#: 까지로 잰다. 국소 장애는 L1 묶음이 곧 결론이라 여기 없다(`scenario_report.json` 의 L2 `na`)
L2_KINDS = frozenset({
    "gw_resource", "ran_radio", "gw_node_down", "auth_failure", "dns_degradation", "external_server",
    "transport_loss", "mme_overload", "change_suspected", "cdn_degradation", "pay_dns_delay",
    "natural_disaster"})


def recommended_hours(kind: str) -> dict:
    """kind 의 권장 시작 시간대(`measured.hours`)와 화면 문구 — 안내이고 주입을 막지 않는다."""
    from .generator import DIURNAL

    s = KIND_SPECS.get(kind) or {}
    if s.get("rush_hours"):
        # 이 시간대에만 서는 장애 — 실측 시간대 대신 정해 둔 구간을 말한다. `clock` 은 화면의
        # 가상 시각 버튼(구간 가운데 정시) · `rush` 는 지금 시각이 구간 안인지 화면이 가린다
        rush = [[int(a), int(b)] for a, b in s["rush_hours"]]
        span = "·".join(f"{a:02d}~{b:02d}" for a, b in rush)
        span_ja = span.replace("~", "〜")
        return {"n": 0, "rush": rush,
                "clock": [f"{(a + ((b - a) % 24) // 2) % 24:02d}:00" for a, b in rush],
                "text": f"출퇴근 시간대 {span}시 권장", "text_ja": f"通勤時間帯 {span_ja}時を推奨"}

    h = measured.hours(kind, DIURNAL, need_l2=kind in L2_KINDS)
    if not h["n"]:
        return {**h, "text": "시작 시간대 실측 없음", "text_ja": "開始時間帯の実測なし"}
    if h["all"]:
        return {**h, "text": "시작 시간대 제약 없음", "text_ja": "開始時間帯の制約なし"}
    a, b = f"{h['from']:02d}", f"{h['to']:02d}"
    if (h["to"] - h["from"]) % 24 > 16:
        # 하루 대부분이면 **뺄 시각**을 말한다 — "05~03시"는 읽히지 않는다
        return {**h, "text": f"{b}~{a}시 제외", "text_ja": f"{b}〜{a}時を除く"}
    return {**h, "text": f"호가 많은 {a}~{b}시", "text_ja": f"呼が多い{a}〜{b}時"}


def recommended_duration(kind: str) -> dict | None:
    """kind 의 권장·최소 지속(분)과 그 근거. 모르는 kind 면 None."""
    s = KIND_SPECS.get(kind)
    if s is None:
        return None
    shape = s.get("shape", "STEP")
    off = max((s.get("series_offset") or {}).values(), default=0)
    steps = s.get("steps") or ()
    r_out = 0
    if steps:
        # 계단의 마지막 경과분이 곧 길이다 — 자르면 뒤 계단이 사라진다(`_duration_for`)
        rec = max(steps[-1][0], 8)
        mn, why = rec, "계단 마지막 구간까지"
    elif shape == "HOLD" and not s.get("detect_min"):
        # 국면형(24번) — 몰림 → 분산 → 분석 세 국면이 정해진 길이다
        rec = mn = SHIFT_TOTAL
        why = "국면 3개(몰림·분산·분석)"
    elif shape == "FLAP":
        # 톱니 모양 자체가 신호다 — 열화·회복을 세 번 이상 보여야 한다
        p = int(s.get("flap_period") or 3)
        rec, mn, why = p * 2 * 3, p * 2 * 2, f"열화·회복 3주기({p}분 주기)"
    else:
        r_in = 0 if s.get("onset") == "instant" else int(s.get("ramp_in") or 3)
        r_out = 0 if s.get("offset") == "instant" else int(
            s.get("ramp_out") if s.get("ramp_out") is not None else (s.get("ramp_in") or 3))
        rec = max(_MIN_FLOOR + 2, r_in + _PLATEAU_MIN + r_out)
        mn = max(_MIN_FLOOR, r_in + 3)
        why = ("즉시 시작·종료 · 정점 유지" if not r_in and not r_out
               else f"상승 {r_in} + 정점 {_PLATEAU_MIN} + 하강 {r_out}")
    if off:
        rec, mn, why = rec + off, mn + off, why + f" · 늦게 시작하는 계열 +{off}"
    detect = int(s.get("detect_min") or 0)
    if detect:
        # 권장은 **탐지 최소 시간 + 몇 분**이다(사용자 정의) — 모양이 그보다 짧아도 이 값이 앞선다
        mn = max(mn, detect)
        if detect + DETECT_MARGIN_MIN > rec:
            rec = detect + DETECT_MARGIN_MIN
            why = f"주입 {detect}분 뒤 탐지 + 여유 {DETECT_MARGIN_MIN}"
    need_l2 = kind in L2_KINDS
    got = measured.summary(kind, need_l2=need_l2)
    goal = "L2까지" if need_l2 else "정답까지"
    settle = SETTLE_MIN if got["settle"] is None else got["settle"]
    if got["n"]:
        # 최소(`mn`)는 시나리오 모양이 정한 길이다 — 계단·국면을 잘라 내는 권장은 내지 않는다
        real = max(got["to_correct"] + r_out, mn)
        if real >= rec or got["n"] >= MEASURED_SHRINK_RUNS:
            why = f"실측 {got['n']}회 · {goal} {got['to_correct']}분 + 하강 {r_out}"
            return {"recommended": int(real), "min": int(mn), "settle": int(settle),
                    "why": why, "basis": "measured"}
        why = (f"실측 {got['n']}회 · {goal} {got['to_correct']}분"
               f"({MEASURED_SHRINK_RUNS}회부터 반영) · {why}")
        return {"recommended": int(rec), "min": int(mn), "settle": int(settle),
                "why": why, "basis": "shape"}
    if got["runs"]:
        why += f" · 실측 {got['runs']}회 {'L2 없음' if need_l2 else '정답 없음'}"
    return {"recommended": int(rec), "min": int(mn), "settle": int(settle),
            "why": why, "basis": "shape"}
