"""U-Plane 세션 모델 — **C-Plane 이 열어 준 베어러 위에서만 트래픽이 흐른다.**

설계 근거는 graphio 쪽 `docs/UPLANE-SESSION-MODEL.md` 다. 이 파일은 그 문서의
구현이고, 문서가 정하지 않은 자리(분포 모수·절단 case 의 구체적 필드 조작)를
여기서 고정한다.

## 왜 생겼나

전에는 C-Plane(`_lte_call`)과 U-Plane(`_user_traffic`)이 **완전히 독립**이었다.
같은 IMSI 가 우연히 겹칠 수는 있어도 인과가 없었고, 그래서 성립하지 않는
것들이 있었다 —

* SCR-07 가입자 추적: "이 가입자가 접속해서 무엇을 했나"를 이을 수 없다.
* R-14 DNS_DEGRADATION: DNS 가 죽은 중에도 HTTP 가 멀쩡했다.
* 절단의 서비스품질 영향: C-Plane 이 끊겼는데 U-Plane 이 정상이었다.

## 계층 — TRANSPORT 가 베이스다

    성공 호 1개 (SUCCESS_FLAG='1')
     └ 세션 k개                        k = 기하분포, 평균이 시간대 곡선을 따른다
         ├ ① DNS 질의  (확률 p_dns)     → dns 레코드       선행
         ├ ② TCP 연결                   → transport 레코드  ★ 항상
         └ ③ HTTP 트랜잭션 m개 (p_http) → http 레코드       후행

**실패한 호는 U-Plane 을 만들지 않는다** — 베어러가 서지 않았으므로 흘릴
데이터가 없다. **DNS 가 실패하면 그 세션의 TCP·HTTP 가 없다** — 이름을 못
풀었으니 연결할 대상이 없다. 이 두 줄이 이 모델의 전부다.

## 레코드를 끊는 자리 — `ONGOINGFLAG`

**한 줄이 세션 하나가 아니다.** 세션 안에서 "특정 메시지까지"를 한 줄로
끊어 내고, 그 줄이 세션의 어디인지를 `ONGOINGFLAG` 가 말한다
(규격 / graphio `schema/codes.ONGOING_FLAG`). 끊는 자리가 **넷**이다 —

    세션이 열릴 때 1회 · 1분마다 1회 · 세션이 닫힐 때 1회
    그리고 **셀이 바뀔 때 1회**  ← 핸드오버가 추가한 자리

    5분 세션 (19:03 시작 → 19:07 종료 · 19:05:23 에 전환)
    19:03     0  Start      SJK-041   Traffic_Delta_* = 그 구간의 증분
    19:04     1  Interim    SJK-041          "
    19:05:23  4  H/O        SJK-041   ← **떠난** 셀. 19:05:00~23 의 Delta
    19:05:59  1  Interim    SJK-042   ← 새 셀. 19:05:23~59 의 Delta
    19:06     1  Interim    SJK-042          "
    19:07     2  End        SJK-042   Traffic_Total_* = 세션 누적 ← 집계가 센다

**핸드오버 분만 두 줄이다.** 분 경계를 기다리면 안 된다 — 한 줄에는 셀이
하나뿐이라, 전환 전후를 한 줄에 담으면 **한쪽 셀의 양이 다른 셀로 넘어간다.**
이동이 잦은 구간에서 그 오차는 상쇄되지 않고 한쪽으로 쌓인다.

세션은 끊지 않는다 — 두 줄 다 같은 `SessionID` 이고 `End` 는 한 번뿐이라
**집계가 세는 수는 변하지 않는다**(불변규칙 6).

**`4` 는 규격서에 없는 Graphio 확장이다.** 규격은 Transport·DNS 가
`0·1·2·20`, HTTP 가 `3 : Http Interim` 까지다. 운영 프로브가 주지 않을
가능성이 크므로 집계 쪽에 폴백(연속 Interim 의 셀 변화)을 함께 둔다 —
근거와 요청서는 graphio `docs/jp/XDR규격-확장항목.md` §1.

**집계는 `{2, 20}` 만 센다**(graphio `schema/codes.COUNTED_ONGOING_FLAGS`).
그래서 `Total` 과 `Delta` 의 뜻이 갈린다 — 종료 레코드의 `Total` 이 세션 전체를
대표하고, Interim 의 `Delta` 는 그 분의 증분이다. Interim 을 세면 **같은
바이트를 여러 번 센다.**

## 양을 어디서 가져오나 — 프로파일 값이 곧 세션 총량이다

세션의 누적 측정값은 `profile.json` 의 수치 분포에서 그대로 뽑는다. 그 분포는
실 샘플 xDR 에서 학습한 것이고, **실 xDR 의 `Traffic_Total_*` 는 원래 세션
총량이다**(종료 레코드에 실려 온다). 구 구현이 그것을 "1분 레코드 하나의 값"
으로 쓴 것이 오히려 규격과 어긋났다.

그래서 이 모델에서 **종료 레코드의 값 분포는 구 구현의 레코드 값 분포와
같다.** 집계는 종료 레코드만 세므로 `GP_KPI_U_CELL_1M` 의 카운터가 전후로
통계적으로 같다 — 달라지는 것은 세지 않는 Interim 레코드가 늘어난다는 것뿐이다.
전후 비교가 가능해야 한다는 요구(§7)를 이 선택이 지킨다.

## 레코드 수와 설정의 관계

`config/datagen.yaml` 의 `pemdb1.rates` 를 **파생 계수로 환산해** 쓴다(§1.4).
키를 지우지 않은 것은 같은 숫자를 다른 뜻으로 읽히게 하지 않으려는 것이다.

    rates.transport  →  분당 **새 세션 수**(피크 기준). 정상 상태에서
                        종료 레코드 수 = 새 세션 수이므로 집계 분모가 전과 같다.
    rates.dns        →  p_dns        = rates.dns  / rates.transport   (0.41)
    rates.http       →  세션당 HTTP 트랜잭션 기대값
                                     = rates.http / rates.transport   (0.68)

**원시 transport 레코드 수는 늘어난다** — 분당 활성 세션 수(= 새 세션 × 평균
수명)에 수렴하므로 평균 수명이 2분이면 2배다. 집계가 세는 수는 그대로다.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime

from .cells import Cell
from .handover import HO_INTRA, HO_S1, HO_S1_MME, Topology, kind_of
from .generator import USER_FIELDS, diurnal_factor
from .scenarios import Plan, Rec

# --------------------------------------------------------------------------
# ONGOINGFLAG — 규격 값. graphio `schema/codes.ONGOING_FLAG` 와 같아야 한다
# --------------------------------------------------------------------------

F_START = "0"
F_INTERIM = "1"
F_END = "2"
#: **내부 표시일 뿐 레코드에 나가지 않는다** (2026-10-28 · 사용자 결정).
#:
#: 셀이 바뀌는 순간 레코드를 끊는 것은 그대로다 — 한 줄에 셀이 하나라
#: 전환 전후를 한 줄에 담으면 **떠나는 셀이 과소, 들어오는 셀이 과대**가
#: 된다(§1.2). 바뀐 것은 **그 줄에 `4` 를 적지 않는다**는 것이다.
#:
#: > *"XDR 은 내용을 판단해서 정리하는 게 아냐. 그냥 있던 사실을 적는
#: > 거야. 근데 ongoing=4 를 어떻게 알꺼야."*
#:
#: 프로브는 셀이 바뀐 **사실**은 안다(패킷의 eNB ID 가 바뀐다). 그러나
#: *"이 Interim 은 핸드오버 때문이다"* 는 **판단**이고, 그 판단에 필요한
#: 것(S1AP/X2AP 제어 메시지)이 U-Plane 관측점에 없다. `4` 는 관찰이
#: 아니라 추정이다 — §1.4 가 *"추정으로 채우지 않는다"* 고 적은 원칙이
#: 폴백보다 **그 값 자체에 먼저** 걸린다.
#:
#: 그래서 끊은 줄도 규격 안의 `1 Interim` 으로 나간다. 규격서가 아는
#: 값만 쓰므로 **원본과 가장 유사해진다.** 핸드오버 관측은 C-Plane 이
#: 한다(`PathSwitch_count` · 해제 Cause · `First != Last` 셀).
_SPLIT_AT_HO = "\x00ho"
F_SINGLE = "20"         #: Start/End — 1분 안에 끝난 세션(한 줄)

#: 집계가 세는 값. graphio `codes.COUNTED_ONGOING_FLAGS` = {2, 20} 와 짝이고
#: **같은 집합이어야 한다** — 어긋나면 집계가 조용히 부풀거나 빈다.
COUNTED = (F_END, F_SINGLE)

# --------------------------------------------------------------------------
# C-Plane 해제 Cause → U-Plane 절단 case (설계 §3)
# --------------------------------------------------------------------------

#: **셀이 바뀌는** 해제 Cause. D.3.1 의 정상 종료 14종 중 실제로 핸드오버·셀
#: 재배치를 뜻하는 것만 골랐다. 14종 전체를 핸드오버로 보면 `120`(User
#: inactivity, 실측 94.4%)과 `300`(Normal Release)까지 셀이 바뀌는 셈이 되어
#: 거의 모든 세션이 핸드오버가 된다 — 그 둘은 이동이 아니라 **그냥 끝난 것**
#: 이다. 정본은 graphio `codes.is_normal_release()` 이고 이 집합은 그것의
#: 부분집합이다(절단으로 세지 않는다는 판정은 그대로 저쪽이 한다).
HANDOVER_CAUSES = frozenset({
    "102",      # Successful Handover
    "104",      # Handover Cancelled
    "105",      # Partial Handover
    "117",      # Time critical handover
    "118",      # Resource optimisation handover
    "122",      # Load Balancing TAU Required — 셀 재배치다
    "133",      # S1 Intra system Handover triggered
    "134",      # S1 Inter system Handover triggered
    "135",      # X2 Handover triggered
})

#: 무선 구간이 끊어진 Cause — **즉시 끊김**이다. End 에 `RstCount>0` 이 남고
#: 그 뒤 레코드가 없다. `ran_radio`(SC-01) 뮤테이터가 `121` 을 심는다.
RAN_LOST_CAUSES = frozenset({"106", "110", "112", "121", "125", "126"})

#: 절단 case 이름. 문자열로 두는 이유는 로그·테스트가 그대로 읽기 때문이다.
OUT_NORMAL = "normal"       #: 정상 종료 — End, FIN 정상
OUT_RST = "rst"             #: 즉시 끊김 — End 에 RST, 수명 단축
OUT_STALL = "stall"         #: 한쪽만 지속 — 재전송 급증, ACK 없음, 타임아웃 End
OUT_ABORT = "abort"         #: U 에 의한 종료 — C-Plane 정상인데 HTTP 응답 없음


# --------------------------------------------------------------------------
# 세션이 수명 동안 들고 가는 필드
# --------------------------------------------------------------------------

#: 호에서 **상속**하는 가입자 식별 필드는 `generator.USER_FIELDS` 다 — 한
#: 자리에 두고 양쪽이 읽는다. 목록이 갈라지면 U-Plane 레코드의 그 필드가
#: 조용히 비고, "U-Plane 의 IMSI 가 같은 분의 C-Plane 호에 있다"(§7.2 고아
#: 레코드 0)가 깨진다.

#: 세션 수명 동안 **값이 변하지 않는** transport 필드. 분마다 다시 뽑으면 같은
#: 세션의 레코드가 분마다 다른 앱·QCI·MSS·도메인을 갖는다 — 세션이라는 개념
#: 자체가 무너지고, 도메인 축 집계가 한 세션을 여러 도메인에 흩뿌린다.
#:
#: TCP 연결 설정값(`Syn*`·`AckTime`)도 여기 있다. 규격상 그 필드는 세션 전체의
#: 누적이라 **모든 분 레코드가 같은 값을 나른다** — 종료 레코드만 집계되므로
#: `U-01`(TCP 연결 성공률)의 분자·분모가 구 구현과 같아진다.
TRANSPORT_CONST = (
    "APN", "SessionQCI", "App_Group_Code", "App_ID", "Protocol", "Tethering",
    "SynMssSize", "SynAckMssSize", "SynCount", "SynAckCount", "FinCount",
    "RstCount", "UplinkWindowCloseCount", "DnlinkWindowCloseCount",
    "ICMP_Delta_Uplink_IcmpPacketCount", "ICMP_Delta_Dnlink_IcmpPacketCount",
    "ClientHelloDomain", "LastDnsQueryName", "LastDnsRequestTime",
    "SynFirstTime", "SynAckFirstTime", "AckTime",
)

#: `Traffic_Total_*` / `Traffic_Delta_*` 가 **짝으로** 있는 측정값. 누적은
#: `Total`, 그 분의 증분은 `Delta` 다. 구 DDL 이 `Total` 만 쓴 것은 세션이 1분
#: 안에 끝나 둘이 같았기 때문이고, 이 모델에서 의미가 갈린다.
TRAFFIC_PAIRS = tuple(
    f"{d}_{f}"
    for d in ("Uplink", "Dnlink")
    for f in ("PacketCount", "PayloadPacketCount", "PayloadSize",
              "ThroughputTime", "RetransPacketCount")
)

#: 짝이 없는 누적 측정값 — RTT 는 규격에 `Delta` 구간이 없다.
CUM_PLAIN = ("UplinkRTTCount", "UplinkRTTTime",
             "DnlinkRTTCount", "DnlinkRTTTime")

#: `TRANSPORT_CONST` 안의 자리. 절단 case 가 몇 개를 덮어쓰는데 `.index()` 를
#: 그때마다 부르면 세션마다 선형 탐색이 된다(설계 §5.1 — 필드 인덱스 캐시).
_C = {f: i for i, f in enumerate(TRANSPORT_CONST)}


# --------------------------------------------------------------------------
# 설정
# --------------------------------------------------------------------------


@dataclass
class Shape:
    """세션 분포 모수. `config/datagen.yaml` 의 `pemdb1.uplane_session` 이 덮는다.

    **양을 고정하지 않는다**(§1.3). 고정 비율로 만들면 분포가 부자연스럽고,
    최소 표본 규칙(불변규칙 5)이 실제로 걸리는지 확인할 수 없다.
    """

    #: 세션 길이(분) 평균. 1 에서 잘린 기하분포라 **짧은 게 많고 가끔 길다**.
    #: 이 값이 곧 `transport` 원시 레코드의 증가 배수다(활성 = 새 세션 × 수명).
    #: Call Type 별 세션 확률 가중. **빈 dict 가 기본이고 `CT_SESSION_W` 위에
    #: 덮인다.** `None` 으로 두면 `from_cfg` 의 `type(기본값)(v)` 가
    #: `NoneType(v)` 가 되어 기동이 죽는다 — 실제로 그렇게 깨졌다.
    call_type_weight: dict = field(default_factory=dict)
    span_mean: float = 2.0
    #: 수명 상한(분). §1.3 의 "1~10분".
    span_max: int = 10
    #: 호당 세션 수 상한. 평균은 `rates` 에서 파생되고(§1.4) 이 값이 꼬리를 자른다.
    sessions_max: int = 3
    #: HTTP 트랜잭션 `m` 의 평균과 상한(§1.3 의 "1~20"). 세션당 HTTP 트랜잭션
    #: **기대값**은 `rates.http / rates.transport` 이고, 그것을 이 평균으로
    #: 나눈 값이 "HTTP 를 하는 세션의 비율"(`p_http`)이 된다.
    http_tx_mean: float = 3.5
    http_tx_max: int = 20
    #: "U 에 의한 종료" 비율 — C-Plane 은 정상인데 앱이 끊는다(§3).
    #: `U-26`(HTTP 무응답률)에만 드러난다. `U-23` 의 분모는 무응답을 빼므로
    #: (`metrics/kpi.py` 의 U-23 주석) 성공률은 흔들리지 않는다.
    app_abort: float = 0.01
    #: 수명 3분 이상인 세션이 **핸드오버를 겪을** 비율. C-Plane 해제 Cause 가
    #: `HANDOVER_CAUSES` 면 이 확률과 무관하게 겪는다.
    #:
    #: 왜 Cause 만으로 하지 않나: 세션 **도중의** 핸드오버는 호를 끝내지 않으므로
    #: 해제 Cause 에 남지 않는다. 그 증거는 `First_Cell != Last_Cell` ·
    #: `PathSwitch_count` 인데 생성기가 아직 C-Plane 에 이동성을 넣지 않는다
    #: (`_lte_call` 이 `Last_* = First_*` 로 둔다). 그래서 U-Plane 쪽에서
    #: 확률로 만든다 — 세션이 **끊기지 않고** 셀만 바뀌는 것을 검증할 수 있어야
    #: 한다(§7.6). C-Plane 이동성이 생기면 이 확률을 그쪽에서 받아야 한다.
    handover: float = 0.25

    @classmethod
    def from_cfg(cls, cfg: dict | None) -> "Shape":
        s = cls()
        for k, v in (cfg or {}).items():
            if not hasattr(s, k):
                # 모르는 키를 조용히 삼키면 오타가 "기본값으로 돌았다"로 끝난다
                raise RuntimeError(f"pemdb1.uplane_session 의 모르는 키: {k}")
            setattr(s, k, type(getattr(s, k))(v))
        return s


# --------------------------------------------------------------------------
# 호와 세션
# --------------------------------------------------------------------------


#: Call Type → **세션을 열 상대 확률**(`CD_CALL_TYPE` 코드).
#:
#: 전에는 모든 성공 호가 같은 확률로 세션을 열었다. `_k()` 주석은 "Paging·TAU·
#: Detach 처럼 사용자 트래픽과 무관한 절차가 있다"고 적어 두었는데 **구현이
#: 그것을 보지 않았다** — 알림만 받고 끝나는 호도 데이터 세션을 열었고, 그래서
#: U-Plane 양이 실제보다 많고 고르게 퍼졌다(2026-10-15 사람 지적).
#:
#: 값은 **상대 가중이고 총량을 바꾸지 않는다** — `session_lambda` 가 그 분
#: 호들의 가중 평균으로 정규화하므로 `rates.transport`(분당 새 세션 수)는
#: 그대로다. 바뀌는 것은 **어떤 호가 그 세션을 여는가**의 분포다.
#:
#:   SRMO(01)   발신 데이터 요청 — 데이터를 쓰려고 거는 호다. 기준 1.0
#:   SRMT(02)   착신 — **상당수가 알림만 받고 끝난다.** 낮게 둔다
#:   ESRMO/MT   VoLTE(06·07). 음성이라 데이터가 적다
#:   ATTACH     등록(00·08). 붙는 순간 약간의 트래픽은 있다
#:   TAU/PAGING/DETACH/AUTH  시그널링뿐 — **0 이다**(세션을 열지 않는다)
#:   APP(60)    앱 트래픽
#:
#: `config/datagen.yaml` 의 `pemdb1.uplane_session.call_type_weight` 로 덮는다.
#:
#: ⚠️ **키 체계를 2026-10-22 에 고쳤다.** 전에는 `"00"`~`"60"`(`CD_CALL_TYPE`
#: 2자리 체계)이었는데 레코드의 `call_type` 에 들어가는 값은 규격 체계
#: `"1"`~`"11"` 이다(`_distributions.json` 실측: 3 Service MO 51.3% ·
#: 4 Service MT 43.9% · 5 TAU 3.2% · 6 Paging 1.2% · 1 Attach MO 0.29%).
#: 키가 한 개도 맞지 않아 `_ct_weight` 가 전부 기본 1.0 을 돌려줬고 —
#: 거기에 `pemdb1.records()` 의 인덱스 버그(`idx.get("CALL_TYPE")` → 항상
#: `-1`)까지 겹쳐 **`call_type` 자체가 빈 문자열이었다.** 둘 다 고쳤다.
#:
#: **총량은 바뀌지 않는다** — `session_lambda` 가 그 분 호들의 가중 평균으로
#: 정규화한다. 바뀌는 것은 "어떤 호가 세션을 여는가"다. Service(3·4)가 95%
#: 이상이라 총량 변화는 작지만 **TAU·Paging 호의 세션이 사라져 셀별 U-Plane
#: 표본이 재분포한다.**
CT_SESSION_W: dict[str, float] = {
    "1": 0.10, "2": 0.10,            # Attach MO/MT — 붙는 순간 약간
    "3": 1.00,                       # Service MO — 발신 데이터. 기준 1.0
    "4": 0.30,                       # Service MT — 착신 알림이 많다
    "5": 0.00,                       # TAU        — 시그널링뿐
    "6": 0.00,                       # Paging     — 알림만
    "7": 0.20, "8": 0.20,            # Ext Service (VoLTE)
    "9": 0.00,                       # Detach
    "10": 0.00, "11": 0.00,          # S1 HO · Reroute
}


@dataclass(slots=True)
class Call:
    """성공 호 하나 — U-Plane 이 상속할 사실만 담는다.

    C-Plane 레코드 전체(154필드)를 들고 있지 않다. 세션은 최대 10분을 이월하는데
    그 동안 원본 행을 붙들면 분당 2만 행 × 154 필드가 메모리에 남는다. 필요한
    것은 식별자·셀·시각·결말뿐이다.
    """

    cell: Cell
    user: tuple[str, ...]       #: USER_FIELDS 순서
    apn: str
    t: float                    #: 베어러가 선 시각(epoch) — 호의 `call_end_time`
    outcome: str
    rel: str = "0"              #: UEContextRelease_Cause — 핸드오버 판정에 쓴다
    #: `CALL_TYPE` 코드(`CD_CALL_TYPE`). **세션을 몇 개 열지가 여기서 갈린다** —
    #: Paging·TAU·Detach 는 사용자 트래픽과 무관한 절차다(`CT_SESSION_W`).
    call_type: str = ""


@dataclass(slots=True)
class Session:
    """활성 세션 하나. 분마다 레코드 한 줄을 내고 수명이 끝나면 사라진다."""

    sid: str
    cell: Cell
    user: tuple[str, ...]
    const: tuple[str, ...]      #: TRANSPORT_CONST 순서
    traffic: tuple[float, ...]  #: TRAFFIC_PAIRS 순서 — 세션 **누적**
    plain: tuple[float, ...]    #: CUM_PLAIN 순서 — 세션 **누적**
    apn: str
    t0: float                   #: 세션 시작(epoch)
    t_end: float                #: 세션 종료(epoch)
    span: int                   #: 수명(분)
    i: int                      #: 지금까지 낸 레코드 수(= 이번이 몇 번째 분인가)
    outcome: str
    ho_at: int                  #: 핸드오버가 일어나는 분 오프셋 (-1 없음)
    http_left: int              #: 남은 HTTP 트랜잭션 수
    domain: str                 #: HTTP Host — 세션 하나는 한 도메인을 본다
    #: 핸드오버가 **실제로 일어난 시각**(epoch). 분 경계가 아니다 — 그래서
    #: 그 분의 HTTP 레코드를 전후로 갈라 셀에 귀속할 수 있다. `-1.0` 은
    #: "이 분에 핸드오버가 없다". **매 분 초기화한다** — 남겨 두면 다음 분의
    #: HTTP 가 이미 떠난 셀로 들어간다.
    ho_t: float = -1.0
    #: 이번 분 전환의 종류(`handover.HO_*`). 빈 문자열은 전환 없음. 세션이
    #: `S1` 을 겪었으면 그 뒤 레코드의 `SGW_ID` 가 바뀐다 — 어느 종류였는지
    #: 모르면 그 변화가 버그처럼 보인다.
    ho_kind: str = ""
    #: 세션이 들고 다니는 코어 장비 `(MME, SGW, PGW)`. **셀에서 매번 읽지
    #: 않는다** — 그러면 X2 핸드오버(코어 재배치 없음)에서도 장비가 바뀐다.
    #: `S1`·`S1_MME` 전환에서만 갈린다.
    core: tuple[int, int, int] = (0, 0, 0)
    #: **현재 장비가 이 세션을 보기 시작한 지점**(세션 누적의 비율).
    #:
    #: XDR 은 SGW·MME 단위로 생성된다. 그래서 장비가 재배치되면 **그 장비는
    #: 이 세션을 처음 본다** — 누적(`Traffic_Total_*`)이 거기서 0 부터 다시
    #: 시작한다(사람 확인 2026-10-27).
    #:
    #: `0.0` 이면 세션 시작부터 같은 장비가 보고 있다. `S1`·`S1_MME` 전환에서
    #: 그 분의 비율로 올라간다 — `INTRA_ENB`·`X2` 는 장비가 그대로라 올리지
    #: 않는다.
    base: float = 0.0
    #: 현재 장비가 보기 시작한 **시각**(epoch). `Traffic_Total_*_FirstTime`
    #: 이 이 값이다 — 세션 시작이 아니다.
    base_t: float = 0.0
    #: 가입자 인덱스. `-1` 은 **고정 가입자이거나 되찾지 못한 경우**다.
    #: 이동 가입자면 전환이 추첨이 아니라 **그 사람의 위치**를 따른다.
    sub_i: int = -1
    #: 세션이 시작된 **여정 회차**. 가입자가 재승차하면 회차가 올라가고,
    #: 그때부터 이 세션은 경로를 따르지 않는다 — 재승차는 하차 칸에서 승차
    #: 칸으로 돌아가는 것이고 그 둘은 인접하지 않다. 따라가게 두면 **인접하지
    #: 않은 셀로 순간이동한 핸드오버**가 나온다.
    trip: int = 0


# --------------------------------------------------------------------------
# 분포
# --------------------------------------------------------------------------


def geom_p(mean: float, k_max: int) -> float:
    """``sum_{k=1..k_max} p^k = mean`` 을 푸는 ``p``.

    상한에서 잘린 기하분포의 평균을 **원하는 값에 정확히** 맞추기 위한 것이다.
    그냥 `p = mean/(1+mean)` 을 쓰면 꼬리를 자른 만큼 평균이 내려가고, 그러면
    분당 세션 수가 설정(`rates.transport`)보다 적게 나온다 — 집계 분모가
    조용히 줄어드는 형태라 알아채기 어렵다.

    단조증가 함수라 이분법으로 충분하다. 분당 한 번 도는 자리다.
    """
    if mean <= 0:
        return 0.0
    if mean >= k_max:
        return 1.0
    lo, hi = 0.0, 1.0
    for _ in range(40):
        mid = (lo + hi) / 2
        s = term = 0.0
        term = 1.0
        for _k in range(k_max):
            term *= mid
            s += term
        if s < mean:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


# --------------------------------------------------------------------------
# 엔진
# --------------------------------------------------------------------------


class SessionEngine:
    """활성 세션을 이월하며 분마다 U-Plane 레코드를 낸다.

    ## 데몬이 재시작되면 활성 세션이 사라진다

    그때는 진행 중이던 세션의 `End` 가 영영 나오지 않는다(설계 §2.1). 기동 시
    활성 세션 없음으로 시작하고 그 분부터 새 세션만 만든다 — **누락분은 그
    세션들의 누적뿐이고, 집계는 `End` 만 세므로 "그 분에 종료된 세션이 적었다"
    로 나타난다.** 0 으로 채우지 않는다(불변규칙 4·17). 기동 직후 몇 분은
    종료 레코드가 정상 상태보다 적고, 평균 수명만큼 지나면 찬다.
    """

    def __init__(self, gen, rates: dict, shape: Shape | None = None,
                 seed: int = 20260903):
        self.gen = gen
        self.shape = shape or Shape()
        self.rng = random.Random(seed + 909)
        self.active: list[Session] = []
        self._sid_n = 0

        #: Call Type 가중 — 설정이 있으면 기본표 위에 덮는다(키만 바꿀 수 있게).
        self._ctw = dict(CT_SESSION_W)
        self._ctw.update({str(k): float(v)
                          for k, v in (self.shape.call_type_weight or {}).items()})

        base = max(1, int(rates.get("transport", 0) or 0))
        #: 분당 **새 세션 수**(피크). `rates.transport` 를 그대로 쓴다 — 정상
        #: 상태에서 종료 레코드 수가 이 값이므로 집계 분모가 전과 같다.
        self.sessions_peak = base
        #: DNS 질의를 하는 세션 비율. 나머지는 캐시 히트다(§1.4).
        self.p_dns = min(1.0, (rates.get("dns", 0) or 0) / base)
        #: 세션당 HTTP 트랜잭션 **기대값**. `p_http × E[m]` 이 이 값이어야
        #: 분당 HTTP 레코드 수가 `rates.http` 가 된다.
        self.http_per_session = (rates.get("http", 0) or 0) / base
        self.p_http = min(1.0, self.http_per_session
                          / max(1.0, self.shape.http_tx_mean))
        #: 수명 분포의 모수. 평균을 `span_mean` 에 맞춘다.
        #:
        #: **`-1` 이 핵심이다.** `_span` 은 1 에서 시작해 더 가므로
        #: `E[span] = 1 + sum_{k=1..max-1} p^k` 이고, 그 합이 `span_mean - 1`
        #: 이어야 한다. `-1` 을 빼먹으면 평균이 1 만큼 커지고 — 실측에서
        #: transport 원시가 66,000/분(= 22,000 × 3.0)으로 나왔다. 집계가 세는
        #: 수는 그대로라 **에러도 집계 차이도 없이 파일만 1.5배** 커진다.
        self._span_p = geom_p(self.shape.span_mean - 1.0,
                              self.shape.span_max - 1)
        #: 전환 토폴로지 — 섹터 간 + **인접 eNB**. 전에는 같은 eNB 안에서만
        #: 골랐고, 그러면 장비 ID 와 TAC 가 **구조적으로** 바뀔 수 없었다
        #: (셀 마스터 실측: 같은 eNB 안 장비 갈림 0 · TAC 갈림 0). 노드 축이
        #: 세션 안에서 갈리는 것은 `S1`·`S1_MME` 뿐이고, 실 XDR 도 레코드에
        #: 장비 필드가 한 벌뿐이라 전환 후 값이 남는다 — `handover` 머리말.
        self.ho_topo = Topology(gen.cells)

        #: 필드 인덱스 캐시. 분당 수만 레코드를 도는 자리에서 이름으로 쓰면
        #: 호출마다 dict 조회가 붙는다(설계 §5.1).
        idx = gen.index["transport"]
        self._i_user = tuple(idx[f] for f in USER_FIELDS)
        self._i_const = tuple(idx[f] for f in TRANSPORT_CONST)
        self._i_head = tuple(idx[f] for f in (
            "SummaryCreateTime", "OngoingFlag", "SessionID",
            "SessionCreatedTime", "SGW_ID", "PGW_ID", "MME_ID",
            "eNB_ID", "Cell_ID"))
        self._i_traffic = tuple(
            (idx[f"Traffic_Total_{n}"], idx[f"Traffic_Delta_{n}"])
            for n in TRAFFIC_PAIRS)
        self._i_plain = tuple(idx[f] for f in CUM_PLAIN)
        self._i_times = tuple(
            tuple(idx[f"Traffic_{g}_{d}_{w}"] for w in ("FirstTime", "LastTime"))
            for g in ("Total", "Delta") for d in ("Uplink", "Dnlink"))

    # -- 호 → 세션 ------------------------------------------------------

    def classify(self, rec: Rec) -> str | None:
        """C-Plane 레코드의 결말. ``None`` 이면 U-Plane 을 만들지 않는다.

        판정 순서가 중요하다. 절단(`drop_flag`)이 선 호는 Cause 를 봐야 하고,
        **핸드오버 유발 해제는 절단이 아니므로**(불변규칙 6) 절단 분기에
        들어오지 않는다 — `_coerce` 가 절단 호에는 비정상 Cause 만 붙이고
        뮤테이터도 `121`·`500` 처럼 비정상만 심는다.
        """
        if rec.get("success_flag") != "1":
            return None                 # 베어러가 서지 않았다 — 흘릴 것이 없다
        if rec.get("drop_flag") == "1":
            rel = rec.get("UEContextRelease_Cause")
            return OUT_RST if rel in RAN_LOST_CAUSES else OUT_STALL
        if self.rng.random() < self.shape.app_abort:
            return OUT_ABORT            # C-Plane 정상인데 앱이 끊었다
        return OUT_NORMAL

    def session_lambda(self, ts: datetime, n_calls: int) -> float:
        """이번 분의 **호당 세션 수** 평균.

        `rates.transport × f_call(h) × f_sess(h)` 개의 세션을 `n_calls` 개의
        성공 호에 나눠 담는다. 호 수가 이미 `f_call` 을 타고 있으므로
        `n_calls` 로 나누면 그 성분이 상쇄되고, 결과적으로 분당 새 세션 수가
        **설정값 × 두 곡선의 곱**이 된다.

        시간대 곡선을 호 수·호당 세션 수·세션당 바이트 **셋에** 걸어 총 트래픽
        4배를 유지한다(설계 §4):

            04시  호 1/1.14 × 호당 세션 1/1.75 × 세션 바이트 1/2 = 총 1/4
            19시     1      ×        1        ×       1        = 총 1
        """
        if n_calls <= 0:
            return 0.0
        if not self.gen.diurnal:
            return self.sessions_peak / n_calls
        f_call = diurnal_factor(ts.hour, "lte_call")
        f_sess = diurnal_factor(ts.hour, "uplane_sessions")
        return self.sessions_peak * f_call * f_sess / n_calls

    def _ct_weight(self, call: Call) -> float:
        """그 호가 세션을 열 상대 가중. 모르는 코드는 1.0(기준)이다.

        **0 으로 떨어뜨리지 않는다** — 코드 테이블에 없는 Call Type 이 오면
        그 호의 트래픽이 통째로 사라지고, 그것은 "그 절차는 데이터를 안
        쓴다"와 구분되지 않는다(불변규칙 17).
        """
        return self._ctw.get(str(call.call_type or ""), 1.0)

    def _k(self, p: float) -> int:
        """호 하나가 여는 세션 수 — 상한에서 잘린 기하분포.

        **0 이 나올 수 있다.** 성공 호인데 데이터를 안 쓰는 경우가 있다 —
        Paging·TAU·Detach 처럼 사용자 트래픽과 무관한 절차가 그렇고, 야간에는
        호당 세션 수 평균 자체가 1 아래로 내려간다(위 `session_lambda`).
        """
        rng = self.rng
        k = 0
        kmax = self.shape.sessions_max
        while k < kmax and rng.random() < p:
            k += 1
        return k

    def _span(self) -> int:
        """세션 수명(분) — 1 에서 시작해 잘린 기하분포. 짧은 게 많다."""
        rng = self.rng
        n = 1
        p = self._span_p
        while n < self.shape.span_max and rng.random() < p:
            n += 1
        return n

    # -- 분 단위 ---------------------------------------------------------

    def minute(self, ts: datetime, calls: list[Call],
               mutate=None, plan_mutate=None) -> dict[str, list[list[str]]]:
        """한 분의 U-Plane 레코드를 계열별로 낸다.

        ``mutate(series, rec, cell)`` 는 활성 시나리오의 뮤테이터를 거는
        콜백이다. **DNS 를 먼저 만들고 거기에 뮤테이터를 걸어 본 뒤** TCP·HTTP
        를 만든다 — `SC-05`(DNS 열화)가 심은 실패가 그 세션의 연결을 실제로
        막아야 한다(§1.2). 순서를 뒤집으면 "DNS 장애 중에도 HTTP 가 정상"인
        지금의 문제가 그대로 남는다.

        ``plan_mutate(cell, plan)`` 는 **세션 총량**에 거는 콜백이다
        (`scenarios.PLAN_MUTATORS`). 양(量) 필드를 분 레코드에서 고치면
        `Traffic_Total_*` 의 누적이 역전하므로 — `_emit` 의 분 쪼개기를 통과해야
        단조성이 원리적으로 보장된다 — 총량 단계에서만 바꾼다(V3 규칙 U-1·U-2).
        **이월 세션에는 걸지 않는다**: 세션이 열릴 때 한 번 정해지고, 분마다 다시
        걸면 같은 세션의 누적이 분 경계에서 점프한다.
        """
        slot = ts.timestamp()
        out: dict[str, list[list[str]]] = {"transport": [], "dns": [], "http": []}
        # 세션당 바이트에 걸리는 시간대 배율. 세션을 **열 때 한 번** 쓴다 —
        # 분마다 다시 걸면 한 세션의 누적이 분 경계에서 점프한다.
        tf = self.gen._traffic_scale("transport", ts)

        # 이월분을 먼저 떼어 둔다. `self.active` 에 새 세션이 쌓이므로 같은
        # 리스트를 돌면 **새 세션이 두 번 나온다.**
        carried = self.active
        self.active = []

        # ① 새 세션 — 성공 호에서 파생된다.
        #
        # **Call Type 마다 확률이 다르다**(2026-10-15). 알림만 받고 끝나는
        # 호(PAGING·TAU·DETACH)는 데이터 세션을 열지 않는다. 총량은 그대로
        # 유지하려고 **그 분 호들의 가중 평균으로 정규화**한다 — 안 하면
        # `rates.transport`(분당 새 세션 수)가 가중치에 끌려 조용히 줄어든다.
        w_of = self._ct_weight
        wsum = sum(w_of(c) for c in calls)
        lam = self.session_lambda(ts, len(calls))
        # 가중 평균이 0 이면 그 분은 트래픽을 낼 호가 하나도 없다는 뜻이다 —
        # 0 으로 나누지 않고 세션을 만들지 않는다(불변규칙 4: 비어 있는 것이
        # 사실이다).
        norm = (len(calls) / wsum) if wsum > 0 else 0.0
        for call in calls:
            w = w_of(call) * norm
            if w <= 0:
                continue
            p = geom_p(min(lam * w, float(self.shape.sessions_max)),
                       self.shape.sessions_max)
            for _ in range(self._k(p)):
                self._open(ts, slot, tf, call, out, mutate, plan_mutate)

        # ② 이월 세션 — 이번 분의 레코드를 낸다
        for s in carried:
            self._emit(ts, slot, s, out, mutate)
            if s.i < s.span:
                self.active.append(s)
        return out

    def _open(self, ts: datetime, slot: float, tf: float, call: Call,
              out: dict, mutate, plan_mutate=None) -> None:
        """세션 하나를 열고 그 분의 레코드를 낸다.

        순서는 실제 call flow 다 — 도메인을 풀고(DNS), 연결하고(TCP), 요청한다
        (HTTP). **DNS 가 실패하면 여기서 끝난다** — transport 도 http 도 없다.
        """
        rng = self.rng
        gen = self.gen
        # 세션 시작은 호가 베어러를 세운 시각 이후다. 분 안에 남은 시간이 거의
        # 없으면 분 끝에 붙인다 — 분 경계를 넘겨 다음 분으로 미루지 않는 이유는
        # 그 호의 U-Plane 이 **같은 분**에 보여야 시나리오 강도가 어긋나지
        # 않기 때문이다(`cli.cmd_run` 의 "같은 분 좌표" 주석).
        t0 = min(max(call.t, slot), slot + 58.0)

        self._sid_n += 1
        sid = f"{int(slot):x}{self._sid_n:05x}"

        # ① DNS — 선행. 확률 p_dns 로만 일어난다(나머지는 캐시 히트)
        if rng.random() < self.p_dns:
            rec = gen.uplane_rec("dns", ts, tf, call.cell, t0 - 0.02,
                                 call.user, call.apn, F_SINGLE, sid)
            if mutate is not None:
                mutate("dns", rec, call.cell)
            out["dns"].append(rec.values)
            if self._dns_failed(rec):
                # 이름을 못 풀었다 — 연결할 대상이 없다(§1.2). 세션이 없다.
                return

        # ② TCP — 세션이 있으면 **반드시** 생긴다
        span = self._span()
        if call.outcome == OUT_RST:
            # 즉시 끊김 — 무선이 끊어졌으므로 길게 못 간다
            span = 1 if rng.random() < 0.7 else 2
        sess = self._plan(ts, tf, call, t0, span, sid, plan_mutate)
        self._emit(ts, slot, sess, out, mutate)
        if sess.i < sess.span:
            self.active.append(sess)

    @staticmethod
    def _dns_failed(rec: Rec) -> bool:
        """DNS 질의가 실패했는가 — 응답 코드·응답 유무·답 개수로 본다.

        `0`(NOERROR)인데 답이 0건인 질의도 **실패다**. 성공으로 보면 연결할 IP
        가 없는데 TCP 가 서는 모순이 생긴다(graphio `uplane_agg` 의 `DNS_OK` 와
        같은 판정).

        **빈 값은 "모름"이고, 모름을 실패로 단정하지 않는다**(불변규칙 17).
        실측 분포가 0.385% 확률로 빈 `ReplyCode`·`AnswerCount` 를 낸다
        (원본 .dat 에 그렇게 들어 있었다). 그것을 실패로 읽으면 **근거 없이
        세션을 죽이고** 분당 수십 건의 transport 가 사라진다 — 에러는 나지
        않고 집계 분모만 줄어든다. `"" or 0` 꼴로 쓰면 빈 값이 0 으로 접혀
        바로 그 함정에 빠진다.
        """
        def num(name: str) -> float | None:
            v = rec.get(name, "").strip()
            if not v:
                return None
            try:
                return float(v)
            except ValueError:
                return None

        code = rec.get("ReplyCode", "").strip()
        if code and code != "0":
            return True                 # 명시적 에러 rcode (SERVFAIL·NXDOMAIN…)
        rt = num("ResponseTime")
        if rt is not None and rt <= 0:
            return True                 # 무응답 — 경로가 끊겼다
        ans = num("AnswerCount")
        return ans is not None and ans <= 0

    def _plan(self, ts: datetime, tf: float, call: Call, t0: float,
              span: int, sid: str, plan_mutate=None) -> Session:
        """세션 하나의 수명치를 **한 번에** 정한다.

        짧은 단위로 레코드 하나씩 만들지 않는 이유는 성능이다(설계 §5.1) —
        분당 수만 세션을 돌면서 세션마다 프로파일 샘플링을 여러 번 하면 그것만
        으로 틱이 눈에 띄게 느려진다. 여기서 한 번 뽑고 분마다 쪼갠다.
        """
        rng = self.rng
        gen = self.gen
        # 세션 **총량**을 담은 레코드를 한 번 만든다. 프로파일의 수치 분포가
        # 곧 세션 총량의 분포다(머리말). 이 레코드 자체는 내보내지 않고
        # 상수·누적값만 뽑아 쓴다 — 분 레코드는 `_emit` 이 만든다.
        rec = gen.uplane_rec("transport", ts, tf, call.cell, t0,
                             call.user, call.apn, F_END, sid)
        idx = gen.index["transport"]
        vals = rec.values

        # 길이와 **느슨하게** 연동한다(§1.3) — 긴 세션이 더 많이 흘린다.
        # 평균 1 인 계수라 총량의 기댓값은 그대로다.
        size_f = 0.5 + 0.5 * span / max(1.0, self.shape.span_mean)

        traffic = [float(vals[idx[f"Traffic_Total_{n}"]] or 0) * size_f
                   for n in TRAFFIC_PAIRS]
        plain = tuple(float(vals[idx[n]] or 0) * size_f for n in CUM_PLAIN)

        if call.outcome == OUT_STALL:
            # 한쪽만 지속 — 재전송이 급증하고 ACK 이 안 온다. `U-12`(down
            # 재전송률)가 이것을 집는다(§3.2). 분모(Payload 패킷)는 그대로 두고
            # 분자만 올린다 — 비율이 실제로 움직여야 탐지가 성립한다.
            for i, n in enumerate(TRAFFIC_PAIRS):
                if n.endswith("RetransPacketCount"):
                    traffic[i] = max(1.0, traffic[i]) * rng.uniform(8.0, 20.0)

        const = [vals[idx[f]] for f in TRANSPORT_CONST]

        # ── 장애 훅 (V3 규칙 U-1) ─────────────────────────────────────
        #
        # **여기가 양(量) 필드를 바꾸는 유일한 자리다.** `_emit` 의
        # `now = 1.0 if last else (i+1)/span` 쪼개기를 통과하므로 `Total` 역전이
        # 원리적으로 불가능하다. 분 레코드에서 직접 고치면 같은 세션의 누적이
        # 분 경계에서 거꾸로 갈 수 있고, 그것은 에러 없이 `U-12` 를 음수로
        # 만든다. `Traffic_Delta_*` 는 Parquet 에 없으므로 건드리지 않는다.
        #
        # **`size_f` 를 곱한 뒤에 건다**(규칙 U-5). 순서를 바꾸면 긴 세션만
        # 과도하게 나빠진다 — 길이와 양의 느슨한 연동이 장애 배율에 증폭된다.
        if plan_mutate is not None:
            totals = {f"Traffic_Total_{n}": traffic[i]
                      for i, n in enumerate(TRAFFIC_PAIRS)}
            totals.update({n: plain[i] for i, n in enumerate(CUM_PLAIN)})
            pl = Plan(totals, dict(zip(TRANSPORT_CONST, const)),
                      call.outcome, span)
            plan_mutate(call.cell, pl)
            traffic = [totals[f"Traffic_Total_{n}"] for n in TRAFFIC_PAIRS]
            plain = tuple(totals[n] for n in CUM_PLAIN)

        # 규칙 U-4 — **재전송은 페이로드 패킷을 넘지 않는다.** 배율이 겹치면
        # (`OUT_STALL` 의 8~20배 × 장애 훅) 넘을 수 있다. 장애가 없는 분에도
        # 적용한다 — 조건부로 두면 "장애 중에만 규칙이 지켜진다"가 되고,
        # 그 차이는 음수·100% 넘는 재전송률로 조용히 나타난다.
        for _d in ("Uplink", "Dnlink"):
            _ir = TRAFFIC_PAIRS.index(f"{_d}_RetransPacketCount")
            _ip = TRAFFIC_PAIRS.index(f"{_d}_PayloadPacketCount")
            traffic[_ir] = max(0.0, min(traffic[_ir], traffic[_ip]))

        if call.outcome == OUT_RST:
            const[_C["RstCount"]] = "1"
            const[_C["FinCount"]] = "0"
        elif call.outcome == OUT_STALL:
            # ACK 이 안 왔다 → `TCP_OK` 가 세어지지 않아 `U-01` 이 내려간다
            const[_C["AckTime"]] = "0.0"
            const[_C["DnlinkWindowCloseCount"]] = str(rng.randint(1, 4))

        # 핸드오버는 **수명 3분 이상**에서만 관측된다. 1~2분 세션에 끼우면
        # `H/O Interim` 이 `End` 자리를 차지해 "열리면 닫힌다"가 깨진다(§7.3).
        ho = -1
        if span >= 3 and (call.rel in HANDOVER_CAUSES
                          or rng.random() < self.shape.handover):
            ho = rng.randint(1, span - 2)

        m = 0
        if rng.random() < self.p_http:
            # 긴 세션이 더 많이 요청한다(§1.3). 평균은 `http_tx_mean` 이고
            # 그것이 `p_http` 와 짝이라 분당 HTTP 레코드 수가 설정을 재현한다.
            scale = span / max(1.0, self.shape.span_mean)
            m = max(1, min(self.shape.http_tx_max,
                           int(round(self.shape.http_tx_mean * scale
                                     * rng.uniform(0.4, 1.6)))))

        smp = gen.samplers.get("http")
        return Session(
            sid=sid, cell=call.cell, user=call.user, const=tuple(const),
            traffic=tuple(traffic), plain=plain, apn=call.apn,
            core=(call.cell.mme_id, call.cell.sgw_id, call.cell.pgw_id),
            base_t=t0,
            # IMSI 로 되찾는다 — `Call` 은 가입자 객체를 들고 오지 않는다
            # (메모리 이유, `Call` 머리말). 뮤테이터가 IMSI 를 바꿨으면
            # 못 찾고 `-1` 이 되는데, 그때는 고정 가입자처럼 돈다
            sub_i=(si := gen.sub_by_imsi.get(call.user[0], -1)),
            trip=(gen.subs[si].trip if si >= 0 else 0),
            t0=t0, t_end=t0 + (span - 1) * 60.0 + rng.random() * 55.0,
            span=span, i=0, outcome=call.outcome, ho_at=ho, http_left=m,
            domain=smp.domain() if smp is not None else "",
        )

    # -- 레코드 ----------------------------------------------------------

    def _slice(self, s: Session, flag: str, t: float,
               cell: Cell | None = None) -> list[str]:
        """세션의 한 분 transport 레코드 — **프로파일을 다시 뽑지 않는다.**

        같은 세션의 레코드는 앱·QCI·MSS·도메인이 같아야 하고(`TRANSPORT_CONST`),
        분마다 다시 뽑으면 그 값들이 흔들려 세션이라는 개념이 무너진다. 성능
        이유도 같다 — 분당 수만 레코드를 도는 자리라 범주형 샘플링을 레코드마다
        돌리면 그것만으로 틱이 눈에 띄게 느려진다(설계 §5.1).

        **이름이 아니라 인덱스로 쓴다.** `Rec.set` 은 호출마다 dict 조회를
        하는데, 여기서는 쓸 자리가 고정이므로 `__init__` 때 캐시한 인덱스
        튜플을 그대로 쓴다(같은 이유로 `pemdb1._BYTE_IDX` 가 캐시한다).
        """
        vals = self.gen.templates["transport"].copy()
        for i, v in zip(self._i_user, s.user):
            vals[i] = v
        for i, v in zip(self._i_const, s.const):
            vals[i] = v
        if cell is None:
            cell = s.cell
        (i_time, i_flag, i_sid, i_created, i_sgw, i_pgw, i_mme,
         i_enb, i_cell) = self._i_head
        vals[i_time] = f"{t:.6f}"
        vals[i_flag] = flag
        vals[i_sid] = s.sid
        vals[i_created] = f"{s.t0:.6f}"
        # 코어는 세션 것이다 — `s.core` 머리말
        vals[i_mme], vals[i_sgw], vals[i_pgw] = s.core
        vals[i_enb] = cell.enb_id
        vals[i_cell] = str(cell.cell_id)
        return vals

    def _emit(self, ts: datetime, slot: float, s: Session,
              out: dict, mutate) -> None:
        """세션의 이번 분 레코드를 낸다 — transport 1~2줄 + HTTP 0~m줄.

        **핸드오버 분만 transport 가 두 줄이다.** 셀이 바뀌는 순간 거기까지를
        한 줄로 닫고(이전 셀) 나머지를 새 셀로 잇는다. 세션은 끊지 않는다 —
        같은 `SessionID` 이고 `End` 는 여전히 한 번이다. 근거는 그 분기의 주석.
        """
        gen = self.gen
        rng = self.rng
        i = s.i
        s.i += 1
        last = s.i >= s.span

        # **이동 가입자는 전환을 추첨하지 않는다.** 그 사람이 지금 있는 셀이
        # 세션이 쓰던 셀과 다르면 **움직인 것이고, 그래서 전환이 일어난다.**
        # 추첨으로 두면 같은 사람이 한 분에는 A→B, 다음 분에는 B→A 반대쪽
        # 으로 가 궤적이 이어지지 않는다(`handover` 의 Route 머리말).
        here, mobile = (self._route_cell(s) if 0 < i < s.span
                        else (None, False))
        moved = here if (here is not None and here is not s.cell) else None

        if s.span == 1:
            flag = F_SINGLE
        elif i == 0:
            flag = F_START
        elif last and moved is None:
            flag = F_END
        elif moved is not None or (not mobile and i == s.ho_at):
            # **이동 중이면 `ho_at` 을 보지 않는다.** `ho_at` 은 해제 Cause 가
            # 핸드오버면 `Shape.handover` 와 무관하게 켜지는데, 그때 추첨으로
            # 상대를 고르면 **경로 밖 셀로 간다** — 실측으로 26,452건 중 4건이
            # 그렇게 궤적을 벗어났다. 움직이는 사람의 전환 근거는 경로 하나다.
            flag = _SPLIT_AT_HO
        else:
            flag = F_INTERIM

        t = s.t_end if last else min(slot + 59.0, s.t_end)
        prev = i / s.span
        now = 1.0 if last else (i + 1) / s.span
        # **매 분 초기화한다.** 남겨 두면 다음 분의 HTTP 가 이미 떠난 셀로
        # 들어가고, 그것은 에러가 아니라 틀린 셀 귀속으로만 나타난다.
        s.ho_t = -1.0
        s.ho_kind = ""
        before = s.cell

        if flag != _SPLIT_AT_HO:
            self._put(out, mutate, s, flag, t, prev, now, slot, s.cell)
        else:
            # 핸드오버 — **분 경계가 아니라 사건 시각에 레코드를 끊는다.**
            #
            # XDR 은 "세션 안에서 특정 메시지까지"를 한 줄로 낸다. 그 끊는
            # 자리가 지금까지 셋이었다(시작 · 분 경계 · 종료). 핸드오버가
            # **넷째 끊는 자리**다 — 셀이 바뀌는 순간 거기까지를 한 줄로
            # 닫아야 그 양이 **어느 셀에서 흘렀는지**가 정해진다.
            #
            # 분 경계로 미루면 전환 전후의 트래픽이 한 줄에 섞이고, 그 줄은
            # 셀이 하나뿐이므로 **한쪽 셀의 양이 다른 셀로 넘어간다.** 이동이
            # 잦은 구간에서 그 오차가 체계적으로 쌓인다.
            #
            # 세션은 **끊지 않는다.** 두 줄 다 같은 `SessionID` 이고 뒤 줄이
            # 이어받는다(불변규칙 6). `End` 는 여전히 한 번뿐이다.
            # **이웃이 없으면 핸드오버가 아니다.** 그걸 무시하고 같은 셀로
            # `flag=4` 를 내면 집계가 **일어나지 않은 전환**을 센다 — Q-08 이
            # 부푼다(옛 구현 실측: 4,754건 중 509건 = 10.7%).
            # 경로가 말한 셀이 있으면 그것이 상대다. 종류는 **셀 마스터가
            # 정한다**(`kind_of`) — 같은 쌍의 전환이 호마다 다른 종류로
            # 나오면 셀 쌍 표가 "이 쌍은 이런 전환"이라고 말할 수 없다
            got = ((moved, kind_of(before, moved)) if moved is not None
                   else self._neighbour(before))
            if got is None:
                self._put(out, mutate, s, F_INTERIM, t, prev, now,
                          slot, before)
                return
            after, s.ho_kind = got

            t_ho = max(s.t0 + 0.1,
                       min(slot + rng.uniform(5.0, 54.0), t))
            mid = prev + (now - prev) * ((t_ho - max(s.t0, slot))
                                         / max(1e-6, t - max(s.t0, slot)))
            mid = min(max(mid, prev), now)

            # ① 전환 **이전** 셀. 스펙 §4.2 가 그렇게 정했다 — 끊긴 자리는
            #    떠난 셀이고, 새 셀에 적으면 원인이 한 칸 뒤로 밀린다.
            # **`1 Interim` 으로 나간다** — `4` 는 적지 않는다(위 상수).
            self._put(out, mutate, s, F_INTERIM, t_ho, prev, mid, slot, before)

            # ② 새 셀로 이어진다. 마지막 분이면 `End` 지만 `ho_at` 이
            #    `span-2` 이하라 실제로는 `Interim` 이다(방어적으로 둔다).
            # **코어 재배치는 전환 뒤에 적는다.** 앞 줄(`flag=4`)은 떠난
            # 셀의 호 이므로 옛 장비를 싣는다 — 여기서 갈면 두 줄이 같은
            # 값을 갖고, 재배치가 **일어났는지 알 수 없어진다.**
            #
            # 종류가 정한다. 새 셀의 마스터 값을 무조건 쓰면 X2 에서도
            # MME·SGW 가 바뀐다 — 그렇게 두었을 때 전환 4,773건 중 1,634건이
            # MME 를 갈았다(틀린 쪽. X2 는 코어 재배치가 없다).
            if s.ho_kind == HO_S1:
                s.core = (s.core[0], after.sgw_id, after.pgw_id)
            elif s.ho_kind == HO_S1_MME:
                s.core = (after.mme_id, after.sgw_id, after.pgw_id)
            if s.ho_kind in (HO_S1, HO_S1_MME):
                # **새 장비는 이 세션을 처음 본다.** XDR 이 SGW·MME 단위로
                # 생성되므로 누적이 거기서 0 부터 다시 시작한다(`Session.base`).
                # `INTRA_ENB`·`X2` 는 장비가 그대로라 리셋하지 않는다 —
                # 리셋하면 같은 프로브가 자기가 센 것을 잊는 셈이 된다.
                s.base, s.base_t = mid, t_ho
            s.cell = after
            self._put(out, mutate, s, F_END if last else F_INTERIM,
                      t, mid, now, t_ho, s.cell)
            s.ho_t = t_ho

        # HTTP — 남은 트랜잭션을 남은 분에 나눠 흘린다
        if s.http_left:
            left_min = s.span - i
            n = s.http_left if left_min <= 1 else min(
                s.http_left,
                1 + int(rng.random() * (2.0 * s.http_left / left_min)))
            s.http_left -= n
            for _ in range(n):
                # TCP 가 선 **뒤**에 요청한다 — 순서가 뒤집히면 call flow 가
                # 아니다. 분 안에서는 무작위로 흩뿌린다.
                th = max(s.t0 + 0.25, min(slot + rng.random() * 59.0, t))
                # 핸드오버가 있던 분이면 **그 시각 전의 요청은 떠난 셀**이다.
                # `s.cell` 하나로 다 적으면 전환 전 트래픽이 새 셀로 넘어간다.
                hc = before if (s.ho_t > 0 and th < s.ho_t) else s.cell
                h = gen.uplane_rec("http", ts, 1.0, hc, th, s.user,
                                   s.apn, F_SINGLE, s.sid, domain=s.domain,
                                   core=s.core)
                if s.outcome == OUT_ABORT:
                    # U 에 의한 종료 — 요청은 갔는데 응답이 없다(§3).
                    # `U-26`(HTTP 무응답률)이 이것을 집는다. `U-23` 의 분모는
                    # 무응답을 빼므로 성공률은 흔들리지 않는다(kpi.py U-23).
                    h.set("ResponseTime", "0.0")
                    h.set("StatusCode", "0")
                if mutate is not None:
                    mutate("http", h, hc)
                out["http"].append(h.values)

    def _put(self, out: dict, mutate, s: Session, flag: str, t: float,
             f_prev: float, f_now: float, d_start: float, cell: Cell) -> None:
        """transport 한 줄을 낸다 — 누적 비율 `f_prev`→`f_now` 구간.

        **`_emit` 에서 떼어낸 이유는 핸드오버 분에 두 번 불러야 하기
        때문이다.** 전에는 "한 분 = 한 줄"이라 이 계산이 `_emit` 안에 있었다.

        `f_prev`·`f_now` 는 세션 누적의 비율이다. 분 경계에서는
        `i/span`→`(i+1)/span` 이고, 핸드오버로 쪼갠 분에서는 그 사이의
        `mid` 가 들어온다. **두 줄의 Delta 합이 쪼개기 전의 한 줄과 같다.**

        `Total` 은 **`s.base` 부터** 센다. XDR 은 SGW·MME 단위로 생성되므로
        장비가 재배치되면 그 장비는 이 세션을 처음 보고, 누적이 0 부터 다시
        시작한다(`Session.base`). 그래서 **장비가 바뀐 세션의 종료 레코드
        `Total` 은 세션 전체가 아니다** — 재배치 뒤 구간만이다. 집계가 그
        조각을 이어 붙이는 것은 통계 쪽 일이다(사람 결정 2026-10-27).

        `d_start` 는 **Delta 관측 구간의 시작**이다. 분 경계 줄은 분 시작,
        핸드오버 뒤 줄은 전환 시각이다. `Total` 쪽은 **현재 장비가 보기
        시작한 시각**이다.
        """
        vals = self._slice(s, flag, t, cell)
        # **누적은 `base` 부터다** — 현재 장비가 보기 시작한 지점. Delta 는
        # 구간 증분이라 `base` 와 무관하다(앞뒤 차이로 구한다)
        b = s.base
        for (i_tot, i_del), v in zip(self._i_traffic, s.traffic):
            # **둘을 같은 절단 급수의 차이로 구한다.** `int(v*(f-b))` 와
            # `int(v*f) - int(v*b)` 는 최대 1 씩 어긋나고, 그 오차가 레코드
            # 수만큼 쌓여 `Σ Delta == Total` 이 깨진다(실측 0.96%).
            cum = int(v * f_now)
            vals[i_tot] = str(cum - int(v * b))
            vals[i_del] = str(cum - int(v * f_prev))
        for j, v in zip(self._i_plain, s.plain):
            vals[j] = str(int(v * f_now) - int(v * b))
        # 양(量)의 관측 구간. `Total` 은 **현재 장비가 보기 시작한 시각**부터,
        # `Delta` 는 이 줄부터다.
        t_s, t_now = f"{max(s.t0, s.base_t):.6f}", f"{t:.6f}"
        d_s = f"{max(s.t0, d_start):.6f}"
        for k, (i_first, i_last) in enumerate(self._i_times):
            vals[i_first] = t_s if k < 2 else d_s
            vals[i_last] = t_now
        if mutate is not None:
            mutate("transport",
                   Rec(vals, self.gen.index["transport"], "transport"), cell)
        out["transport"].append(vals)

    def _route_cell(self, s: Session) -> tuple[Cell | None, bool]:
        """`(따를 셀, 이동 가입자인가)`.

        둘을 함께 돌려주는 이유: "따를 셀이 없다"에 **두 가지 뜻**이 있다.

        | 셀 | 이동 | 뜻 | 전환 |
        | --- | --- | --- | --- |
        | 있음 | True | 경로 위에 있다 | 그 셀로 |
        | None | False | 고정 가입자이거나 IMSI 미해결 | 추첨 허용 |
        | None | **True** | **지난 회차의 세션** | **금지** |

        셋째를 둘째와 같이 다루면 그 세션이 추첨 전환을 하고, 상대가 하필
        경로 위의 먼 칸이면 **인접하지 않은 셀로 순간이동한 레코드**가 남는다
        (실측 4만 전이 중 1건이 그렇게 새어 나갔다). 그 세션은 마지막 셀에
        머문 채 끝나야 한다 — 소유자는 이미 다음 통행을 시작했다.
        """
        if s.sub_i < 0:
            return None, False
        sub = self.gen.subs[s.sub_i]
        if sub.route < 0:
            return None, False
        if sub.trip != s.trip:
            return None, True
        return self.gen.cell_of(sub), True

    def _neighbour(self, cell: Cell) -> tuple[Cell, str] | None:
        """전환 상대와 종류. **없으면 `None` — 호출부가 전환을 취소한다.**

        전에는 상대가 없으면 **같은 셀을 돌려줬다.** 그러면 셀이 그대로인
        `flag=4` 레코드가 생기고(실측 10.7%) 집계가 그것을 일어나지 않은
        전환으로 센다 — Q-08 이 부푼다. 그래서 "없음"을 값으로 말한다.

        종류에 따라 `Cell` 이 들고 있는 `mme_id`·`sgw_id`·`pgw_id`·`tac` 가
        함께 바뀌고, `_slice` 가 그것을 레코드에 싣는다. **장비 ID 변경이
        따로 구현되는 것이 아니라 셀을 바꾸면 따라온다.**

        ## 노선을 타지 않는 가입자는 **섹터 전환만** 한다

        `INTRA_ENB` 로 제한한다 — 같은 사이트의 다른 섹터다. 그래서 eNB·TAC·
        MME·SGW 가 **바뀌지 않는다**(셀 마스터 실측: 같은 eNB 안 장비 갈림 0 ·
        TAC 갈림 0 · §1.6).

        왜: 그 사람은 움직이고 있지 않다. 집·사무실에서 통신하는 중이고,
        전환이 난다면 섹터 경계에 걸쳐 있기 때문이다. 거기에 eNB 간 전환을
        주면 **장비가 옮겨 가고 TAU 까지 따라붙는다** — 이동하지 않는 사람에게
        그건 사실이 아니고, 그 TAU 가 TAU 성공률의 분모를 채워 "이동이 많은
        망"으로 보이게 한다(2026-10-27 사람 결정).

        eNB 간 전환과 코어 재배치는 **경로를 타는 가입자**의 것이다
        (`_route_cell` 쪽 갈래). 노선을 타지 않는 사람의 장거리 이동이
        필요해지면 그때 별 기능으로 둔다 — 지금 섞으면 둘을 가를 수 없다.
        """
        return self.ho_topo.pick(cell, self.rng, only=(HO_INTRA,))
