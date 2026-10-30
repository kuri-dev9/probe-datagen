"""상용 스키마(pemdb1) 어댑터 — 생성한 xDR 을 상용 테이블에 그대로 적재한다.

**상용이 정본이다.** 자체 창고 스키마(구 `store.py` · `aggregate.py`, 2026-10-15 삭제)를 거치지 않고
`XDR_LTE_CALL_KPI` 에 직접 쓰고, 1분 집계는 MySQL 안에서 SQL 로 돈다. 중간에
DuckDB 를 두면 컬럼 이름·타입을 두 번 번역해야 하고, 그 번역이 틀렸을 때
알아낼 방법이 없다.

구조는 셋이다.

1. **셀 마스터** — `CL_CELL_MAP` + `CM_CELL_REP_RU_LOC` 에서 읽는다. `data/cells.json`
   (자체 생성 350셀)을 쓰지 않는다. 상용 XDR 의 `FIRST_ENB_ID`·`FIRST_CELL_ID` 가
   마스터와 조인되지 않으면 eNB 집계가 통째로 비므로, 식별자는 **마스터에서
   가져온 실제 값**이어야 한다 (실측: `FIRST_CELL_ID` = `CL_CELL_MAP.CELLNUM`,
   `FIRST_ENB_ID` = `CL_CELL_MAP.ENB_ID`, 둘 다 평문 숫자).

2. **컬럼 매핑** — `fields.SERIES_FIELDS["lte_call"]`(154) 과 상용
   `XDR_LTE_CALL_KPI`(155 = EVENT_TIME + 154) 는 **순서가 1:1로 같다.** 이름만
   철자가 다른 자리가 9곳 있다(`MDN`/`mdn`, `AuthenticationInformation_*`/
   `AUTHINFORMATION_*`, `InitialUEMessage_RRC_Establishment_Cause`/
   `INITMSG_RRC_ESTABLISH_CAUSE`, `old_call_error_interface_protocol`/
   `OLD_CALL_ERROR_INF_PROTOCOL`, `RESERVED_151~154`/`RESERVED3~6`). 이름으로
   맞추면 그 9곳이 조용히 빠지므로 **위치로 맞추고 길이만 검증**한다.

3. **값 분포** — `data/pemdb1/_distributions.json` (상용 334만행 실측). CALL_TYPE ·
   APN · Vendor/Model · Release Cause 를 여기서 뽑는다. 자체 프로파일
   (`data/profile.json`)의 구성비는 상용과 다르다.

집계 규칙은 graphio 불변규칙을 따른다 — 롤업은 카운트 합산만, 정상 종료
(`UECONTEXTRELEASE_CAUSE` 14종)는 절단이 아니고, 결측은 0으로 채우지 않는다.
"""

from __future__ import annotations

import hashlib
import json
import random
import collections
import zlib
from collections.abc import Iterable
from dataclasses import dataclass, field, fields
from datetime import datetime, timedelta
from pathlib import Path

# 대역 정의와 가중 배수의 **정본은 `topo.py` 다** — 여기 숫자를 다시
# 적으면 사본이 둘이 되고, 한쪽만 고쳐질 때 `seed-enb` 가 놓은 eNB 가
# 선택에서 조용히 빠진다. `topo` 는 `pemdb1` 을 지연 임포트하므로
# 순환이 생기지 않는다(`topo.pick_report`).
from . import topo
# 별칭으로도 받는다 — `_weight` 는 셀마다 불리고(실측 37만 회),
# 테스트가 `P._in_topo_band` 를 monkeypatch 해 대역 판정만 끈다.
from .topo import TOPO_WEIGHT, in_new_band as _in_topo_band, table_name

import polars as pl

from . import raw_parquet
from .cells import Cell
from . import handover as HO
from .faults import resolve_active as resolve_active_faults
from .fields import SERIES_FIELDS
from .generator import Generator, Groups, USER_FIELDS
from .identity import synth_imei, synth_imsi
from .plmn import Plmn
from .region import apn_map
from .profile import load as load_profiles
from .scenarios import (
    APP_BOOSTERS,
    BOOSTERS,
    RELOCATORS,
    PACKET_DENOM_KPIS,
    MUTATORS,
    PLAN_MUTATORS,
    SUPPRESSORS,
    Scenario,
    set_fault_cfg,
    set_shift_cfg,
    shift_split,
    visible_level,
)
from . import scenarios as _scenarios


def _shift_cfg():
    """지금 걸린 `capacity_shift` 설정 — 모듈 전역을 매번 다시 읽는다
    (`set_shift_cfg` 가 갈아 끼우므로 import 시점 값을 붙잡으면 안 된다)."""
    return _scenarios.SHIFT
from .scenarios import build_scenarios as build_sim_scenarios
from .session import Call, SessionEngine, Shape
from .apps import load as load_apps

# --------------------------------------------------------------------------
# 상수
# --------------------------------------------------------------------------

#: 핸드오버 유발 해제 — 정상 종료다 (부록 D.3.1 / graphio 불변규칙 6).
#: 절단으로 계상하면 이동이 잦은 지역의 절단률이 상시 붉게 나온다.
NORMAL_RELEASE = (
    "102", "104", "105", "117", "118", "120", "122", "123", "124",
    "133", "134", "135", "300", "302",
)

#: first_error_interface_protocol → CAUSE_TYPE. CD_IF_PROTO_CEI 의
#: IF_PROTO_CODE → PROTOCOL_NAME 이고, CAUSE_DEF.CAUSE_TYPE 이 이 이름을 쓴다.
IF_PROTO_NAME = {
    "1": "DIAM", "2": "S1AP", "3": "GTPv2C", "4": "GTPv2C",
    "5": "EMM", "6": "ESM", "7": "GTPv1C", "8": "DIAM",
}

#: 상용 추출 조건 (상용 `XDR_LTE_CALL_KPI` 테이블 코멘트: ONGOING_FLAG='2').
#: 2 = 호 종료 레코드 — 플래그가 실려 있는 유일한 레코드다.
ONGOING_END = "2"

#: TAU(Tracking Area Update) 의 `CALL_TYPE` — `CD_CALL_TYPE` 코드 5.
#: `_CT_GROUP` 에서 `"tau"` 그룹이고, SA 에서는 Mobility/Periodic
#: Registration Update 다(개발스펙 §3.4).
TAU_CALL_TYPE = "5"

#: TA 몇 개를 한 장비가 맡나 — **장비를 TAC 에서 유도할 때의 묶음 크기.**
#:
#: 실제 망은 MME pool 이 TA 목록을 맡는다. `1` 로 두면 TA 경계마다 MME 가
#: 바뀌어 전환이 거의 다 `S1_MME` 가 되고, 크게 두면 MME 가 한두 대로 쏠려
#: 노드 축 시나리오(SC-10 MME 과부하)가 무대를 못 찾는다.
#:
#: 묶음 크기는 `handover.build_ta_lists` 의 상위 묶음(4×4)과 **맞춰야 한다** —
#: 그 함수가 `id // 16` 이 상위 묶음이 되도록 번호를 매긴다.
#:
#: ## 묶음이 **중첩돼야** 한다 — 서로 배수여야 한다
#:
#: `S1`(SGW 만 재배치)이 흔하고 `S1_MME`(MME 까지)가 드문 것이 실제다.
#: 그러려면 **SGW 경계가 MME 영역 안에 들어가야** 한다 — 즉 SGW 묶음이
#: MME 묶음의 약수여야 한다.
#:
#: 처음에 `MME=4 · SGW=3` 으로 두었더니 경계가 서로 엇갈려 뒤집혔다 —
#: 실측 `S1` 6.6% · `S1_MME` 13.8%(기대는 26.7% · 6.7%). TA 경계를 넘을
#: 때마다 MME 가 바뀐 셈이다.
#:
#: `1 | 4 | 12` 로 두면 한 MME 영역(4 목록) 안에 SGW 영역이 4개다 →
#: 목록 경계에서 SGW 가 늘 바뀌고 MME 는 1/4 → `S1` : `S1_MME` ≈ 3:1
#: (비중표는 4:1).
#:
#: ## **TAC 이 아니라 TAI List 가 기준이다**(2026-10-27)
#:
#: 전에는 `tac // N` 이었다. 실제 TAC 번호가 **지리를 느슨하게만** 반영해서
#: (실측: 번호가 4 이내인 TAC 쌍의 거리 중앙 5.8km · 임의 쌍 12.6km) 그
#: 산술이 지리와 안 맞았다. TAI List 는 좌표로 묶고 Z-order 로 번호를
#: 매기므로 번호가 가까우면 지리적으로도 가깝다.
_TA_PER_SGW = 1
#: **`build_ta_lists` 의 상위 묶음 크기(4×4 = 16)와 같아야 한다.**
#: 그 함수가 `id // 16` 이 상위 묶음이 되도록 번호를 매긴다 — 다른 값을 주면
#: 묶음이 번호와 어긋나 MME 가 목록마다 바뀐다.
_TA_PER_MME = 16
_TA_PER_PGW = 48

#: "없음" 센티널. 상용 Cause 컬럼은 NULL 이 아니라 **문자열 `'0'`** 으로
#: "없음"을 적는다(`FIRST_ERROR_CAUSE` 의 99.6%). 집계가 이것을 거르지 않으면
#: `C_02000000` 같은 **존재하지 않는 Cause 코드가 만들어진다.**
#: graphio 쪽 정본은 `store/tables.py:NONE_SENTINEL` 이고 같은 값이어야 한다.
NONE_CODE = "0"

#: **원시 xDR 은 MySQL 에 없다 — 전부 로컬 Parquet 다**(2026-10-14).
#:
#: 1차로 U-Plane 3종(`GP_RAW_TRANSPORT`·`GP_RAW_DNS`·`GP_RAW_HTTP`)을 뺐다.
#: 세 테이블에 분당 6.3만 행을 INSERT 하는 것이 전체 쓰기 I/O 의 68% 였고
#: (실측) 집계의 **입력 전용**이라 집계 후 아무도 읽지 않았다.
#:
#: 2차로 **C-Plane(`GP_RAW_LTE_CALL_KPI`)도 뺐다.** 근거가 조금 다르다 —
#: 그쪽은 SCR-06(호)·SCR-07(가입자)·RCA 증거팩이 원시 레코드를 직접 읽고
#: 있었다. 그런데 **읽는 쪽이 먼저 1차 통계(`PM_COR_*_1M`)로 옮겨 갔고**,
#: 남은 소비자가 1분 집계뿐이 되었다. 그 상태에서 154컬럼 varchar 를 분당
#: 2만 행씩 DB 에 넣을 이유가 없다.
#:
#: 설계와 레이아웃은 `raw_parquet` 머리말에 있다. 보관 기간만 평면마다
#: 다르다 — U-Plane 10분 · C-Plane 1440분(그쪽 표 참조).
#:
#: **`XDR_TABLE` 상수는 지웠다.** 이름을 남겨 두면 "테이블이 아직 있나"로
#: 읽히고, 실제로 개명 때 한 줄이 남아서 제어 화면이 조용히 빈 값을 띄운
#: 전력이 있다(`ui.api_status` 주석).
PM_ENB = "PM_ENB_KPI_1M"
PM_ENB_CAUSE = "PM_ENB_CAUSE_1M"
PM_EPC = "PM_EPC_KPI_1M_ORG"

#: 계열별 `(컬럼, 규격 필드명, 타입, 문자 폭)` — **이름으로 맞춘다.**
#:
#: **타입과 폭이 여기 적혀 있는 이유**(2026-10-14): 전에는 `uplane_mapping` 이
#: `information_schema` 에서 읽었다. 테이블을 지우면 그 원천이 없어지므로
#: 정본이 코드로 옮겨 온다. 값은 구 DDL 과 **같다** — 집계 결과가 전후 동일해야
#: 하고, 타입이 바뀌면 캐스팅 결과가 조용히 달라진다.
#:
#: 폭은 `str` 컬럼에만 있다(구 `varchar(n)`). 넘치면 잘라서 넘기고 횟수를
#: 센다 — Parquet 자체에는 길이 제한이 없지만, **자르지 않으면 DB 시절과
#: 다른 값이 집계에 들어간다.**
#:
#: `XDR_LTE_CALL_KPI` 는 154필드가 1:1 이라 위치로 맞췄지만(`xdr_mapping`),
#: 여기는 규격 415필드 중 55개만 뽑으므로 위치 대응이 아예 없다. 이름으로
#: 맞추고 **생성기에 없는 필드를 쓰라고 하면 멈춘다**(`uplane_specs`) —
#: 조용히 빠지면 그 카운터가 영원히 NULL 이고, NULL 은 "모름"으로 읽혀 KPI 가
#: 산출 불가로 내려간다. 에러가 나는 쪽이 낫다.
#:
#: `Traffic_Total_*` 만 이름을 줄였다. 그대로 펴면
#: `TRAFFIC_TOTAL_UPLINK_RETRANS_PACKET_COUNT`(42자)로 식별자 30자 상한을
#: 넘는다(상용이 Oracle 포팅이라 그 제약을 받는다). **컬럼 이름은 그대로
#: 둔다** — 집계(graphio `metrics/uplane_agg.py`)가 이 이름으로 읽는다.
#:
#: `EVENT_TIME` 은 여기 없다 — 규격 필드(`SummaryCreateTime`)는 epoch 실수지만
#: 분 귀속 컬럼은 `datetime` 이라 writer 가 분 시각을 직접 넣는다.
#: `write_xdr` 이 C-Plane 에 하는 것과 같다.
#: **`ONGOING_FLAG`·`SESSION_ID` 는 세션 모델이 추가했다**(2026-10-14). 구 DDL
#: 에는 없었다 — 그때는 U-Plane 레코드 하나가 세션 하나였고 전부 `20`
#: (Start/End) 이라 담을 뜻이 없었다. 세션이 분을 넘기면서
#:
#:   · `ONGOING_FLAG` 가 **집계 대상을 가른다.** `{2, 20}` 만 세고(graphio
#:     `codes.COUNTED_ONGOING_FLAGS`) Interim 을 세면 같은 바이트를 여러 번
#:     센다. 이 컬럼이 없으면 그 구분을 할 수 없다.
#:   · `SESSION_ID` 가 **한 세션의 레코드를 잇는다.** 핸드오버로 셀이 바뀐
#:     세션이 끊기지 않았음을 확인할 유일한 키다(설계 §7.6). 그리고 그 세션을
#:     열어 준 **호와 잇는다** — SCR-07 가입자 추적의 연결 고리다.
#:   · `MME_ID`·`SGW_ID`·`PGW_ID` 가 **장비 축 집계의 원천이다**(2026-10-14).
#:     셀 축(`GP_KPI_U_CELL_1M`)·도메인 축(`GP_KPI_DOMAIN_1M`)은 있었는데 장비
#:     축이 비어 있어서 "이 SGW 를 경유하는 트래픽의 품질"을 화면이 물을 수
#:     없었다. 생성기는 전부터 이 세 값을 레코드에 쓰고 있었고(`uplane_rec` 이
#:     셀에서 받는다) **Parquet 컬럼 목록에만 없었다.** 셀→노드를 나중에
#:     조인해 얻는 방법도 있지만, 한 레코드가 자기 경로를 이미 들고 있으므로
#:     조인은 틀릴 여지만 늘린다(0 채움 표기가 어긋나면 조용히 빈다).
UPLANE_FIELDS: dict[str, tuple[tuple[str, str, str, int | None], ...]] = {
    "transport": (
        ("ENB_ID", "eNB_ID", "str", 30),
        ("CELL_ID", "Cell_ID", "str", 10),
        ("IMSI", "IMSI", "str", 30),
        ("ONGOING_FLAG", "OngoingFlag", "i8", None),
        ("SESSION_ID", "SessionID", "str", 32),
        ("MME_ID", "MME_ID", "str", 30),
        ("SGW_ID", "SGW_ID", "str", 30),
        ("PGW_ID", "PGW_ID", "str", 30),
        ("SYN_COUNT", "SynCount", "i64", None),
        ("ACK_TIME", "AckTime", "f64", None),
        ("RST_COUNT", "RstCount", "i64", None),
        ("UPLINK_WINDOW_CLOSE_COUNT", "UplinkWindowCloseCount", "i64", None),
        ("DNLINK_WINDOW_CLOSE_COUNT", "DnlinkWindowCloseCount", "i64", None),
        ("SYN_MSS_SIZE", "SynMssSize", "i32", None),
        ("SYN_ACK_MSS_SIZE", "SynAckMssSize", "i32", None),
        ("UPLINK_RTT_TIME", "UplinkRTTTime", "i64", None),
        ("UPLINK_RTT_COUNT", "UplinkRTTCount", "i64", None),
        ("DNLINK_RTT_TIME", "DnlinkRTTTime", "i64", None),
        ("DNLINK_RTT_COUNT", "DnlinkRTTCount", "i64", None),
        ("RETRANS_PKT_UP", "Traffic_Total_Uplink_RetransPacketCount", "i64", None),
        ("PAYLOAD_PKT_UP", "Traffic_Total_Uplink_PayloadPacketCount", "i64", None),
        ("RETRANS_PKT_DN", "Traffic_Total_Dnlink_RetransPacketCount", "i64", None),
        ("PAYLOAD_PKT_DN", "Traffic_Total_Dnlink_PayloadPacketCount", "i64", None),
        ("PAYLOAD_SIZE_UP", "Traffic_Total_Uplink_PayloadSize", "i64", None),
        ("PAYLOAD_SIZE_DN", "Traffic_Total_Dnlink_PayloadSize", "i64", None),
        ("THROUGHPUT_TIME_UP", "Traffic_Total_Uplink_ThroughputTime", "f64", None),
        ("THROUGHPUT_TIME_DN", "Traffic_Total_Dnlink_ThroughputTime", "f64", None),
        ("TETHERING", "Tethering", "i8", None),
        # 앱 축 (2026-10-28 · `CM_APP_CODE`). **이름은 규격 그대로** —
        # 운영 프로브가 Protocol·IP·Port 로 매핑해 붙이는 값이다
        ("APP_ID", "App_ID", "str", 20),
        ("APP_GROUP_CODE", "App_Group_Code", "str", 50),
        ("APP_IP", "App_IPv4", "str", 45),
        ("APP_PORT", "App_Port", "i32", None),
    ),
    "dns": (
        ("ENB_ID", "eNB_ID", "str", 30),
        ("CELL_ID", "Cell_ID", "str", 10),
        ("IMSI", "IMSI", "str", 30),
        ("ONGOING_FLAG", "OngoingFlag", "i8", None),
        ("SESSION_ID", "SessionID", "str", 32),
        ("MME_ID", "MME_ID", "str", 30),
        ("SGW_ID", "SGW_ID", "str", 30),
        ("PGW_ID", "PGW_ID", "str", 30),
        ("QUERY_NAME", "QueryName", "str", 255),
        ("REPLY_CODE", "ReplyCode", "i32", None),
        ("ANSWER_COUNT", "AnswerCount", "i32", None),
        ("QUERY_RETRY_COUNT", "QueryRetryCount", "i32", None),
        ("DNS_RETRY_SEQUENCE", "dns_retry_sequence", "i32", None),
        ("DNS_RETRY_INTERVAL", "dns_retry_interval", "f64", None),
        ("RESPONSE_TIME", "ResponseTime", "f64", None),
        ("REQUEST_FIRST_TIME", "RequestFirstTime", "f64", None),
        ("PAYLOAD_SIZE_UP", "Traffic_Total_Uplink_PayloadSize", "i64", None),
        ("PAYLOAD_SIZE_DN", "Traffic_Total_Dnlink_PayloadSize", "i64", None),
        ("TETHERING", "Tethering", "i8", None),
        # 앱 축 — transport·http 와 같은 이름(2026-10-28). DNS 는 앱 사전을
        # 타지 않아 `App_ID` 가 비고 `App_IPv4` 가 `0` 이다 — 생성기 값 그대로다
        ("APP_ID", "App_ID", "str", 20),
        ("APP_GROUP_CODE", "App_Group_Code", "str", 50),
        ("APP_IP", "App_IPv4", "str", 45),
        ("APP_PORT", "App_Port", "i32", None),
    ),
    "http": (
        ("ENB_ID", "eNB_ID", "str", 30),
        ("CELL_ID", "Cell_ID", "str", 10),
        ("IMSI", "IMSI", "str", 30),
        ("ONGOING_FLAG", "OngoingFlag", "i8", None),
        ("SESSION_ID", "SessionID", "str", 32),
        ("MME_ID", "MME_ID", "str", 30),
        ("SGW_ID", "SGW_ID", "str", 30),
        ("PGW_ID", "PGW_ID", "str", 30),
        ("HOST", "Host", "str", 255),
        ("HOST_SITE", "Host_Site", "str", 255),
        ("STATUS_CODE", "StatusCode", "i32", None),
        ("REQUEST_TIME", "RequestTime", "f64", None),
        ("RESPONSE_TIME", "ResponseTime", "f64", None),
        ("UPLINK_RTT_TIME", "UplinkRTTTime", "i64", None),
        ("UPLINK_RTT_COUNT", "UplinkRTTCount", "i64", None),
        ("DNLINK_RTT_TIME", "DnlinkRTTTime", "i64", None),
        ("DNLINK_RTT_COUNT", "DnlinkRTTCount", "i64", None),
        ("RETRANS_PKT_DN", "Traffic_Total_Dnlink_RetransPacketCount", "i64", None),
        ("PAYLOAD_PKT_DN", "Traffic_Total_Dnlink_PayloadPacketCount", "i64", None),
        ("PAYLOAD_SIZE_DN", "Traffic_Total_Dnlink_PayloadSize", "i64", None),
        ("THROUGHPUT_TIME_DN", "Traffic_Total_Dnlink_ThroughputTime", "f64", None),
        ("TETHERING", "Tethering", "i8", None),
        # 앱 축 (2026-10-28 · `CM_APP_CODE`). **이름은 규격 그대로** —
        # 운영 프로브가 Protocol·IP·Port 로 매핑해 붙이는 값이다
        ("APP_ID", "App_ID", "str", 20),
        ("APP_GROUP_CODE", "App_Group_Code", "str", 50),
        ("APP_IP", "App_IPv4", "str", 45),
        ("APP_PORT", "App_Port", "i32", None),
    ),
}

# ── 체감품질(XP) 필드 — 세 계열에 **같은 이름**으로 덧붙인다 (2026-10-28) ──
#
# **기존 파일에 컬럼을 더한다(통합).** 체감 전용 Parquet 를 따로 쓰는 안과
# 실측 비교했다 — 분당 3.7MB/0.31초(통합) vs 4.2MB/0.35초(분리). 키 6컬럼이
# 두 파일에 중복되지 않는 만큼 통합이 가볍다(사용자 결정). 기존 집계는 이름으로
# 골라 읽으므로(`graphio store/raw_parquet.scan`) 컬럼이 늘어도 값이 같다.
#
# **Delta 가 체감 판정의 기준이다.** Interim 레코드의 `Traffic_Total_*` 는 세션
# 누적이라 분에 귀속할 수 없다 — 그 분에 흐른 양은 Delta 다(개발스펙 X-3).
# 장애 훅은 세션 총량에 걸리고(`PLAN_MUTATORS`) Total 과 Delta 가 같은 총량에서
# 쪼개지므로(`session._emit`) **Delta 에도 장애가 그대로 실린다.**
#
# **Bearer 블록·`User_*`·`IP_Version` 은 생성기가 모델링하지 않는다** — 값이
# 전부 템플릿 기본값(`0`·빈칸)이다. 규격 필드라 싣되, 그 `0` 은 관측이 아니다.
#
# 이름은 30자 상한(상용 Oracle 포팅)을 지킨다 — Delta 는 `DLT_` 접두로 Total
# (접두 없음)과 가른다. `D_` 는 `DN`(down)과 헷갈린다.
_XP_SESSION: tuple[tuple[str, str, str, int | None], ...] = (
    ("SESSION_CREATED_TIME", "SessionCreatedTime", "f64", None),
    ("SESSION_QCI", "SessionQCI", "i32", None),
    ("BEARER_CREATED_TIME", "BearerCreatedTime", "f64", None),
    ("BEARER_ID", "BearerID", "str", 50),
    ("PARENT_EBI", "ParentEBI", "i32", None),
    ("EBI", "EBI", "i32", None),
    ("DEDICATED_QCI", "DedicatedQCI", "i32", None),
    ("DEDICATED_BEARER", "DedicatedBearer", "i8", None),
    ("PROTOCOL", "Protocol", "i32", None),
    ("IP_VERSION", "IP_Version", "i8", None),
    ("APP_IPV6", "App_IPv6", "str", 38),
    ("USER_IP", "User_IPv4", "str", 45),
    ("USER_IPV6", "User_IPv6", "str", 38),
    ("USER_PORT", "User_Port", "i32", None),
)
_DLT_PARTS: tuple[tuple[str, str, str], ...] = (
    ("FIRST_TIME", "FirstTime", "f64"),
    ("LAST_TIME", "LastTime", "f64"),
    ("PKT", "PacketCount", "i64"),
    ("DATA_SIZE", "DataSize", "i64"),
    ("PAYLOAD_PKT", "PayloadPacketCount", "i64"),
    ("PAYLOAD_SIZE", "PayloadSize", "i64"),
    ("RETRANS_PKT", "RetransPacketCount", "i64"),
    ("RETRANS_DATA_SIZE", "RetransDataSize", "i64"),
    ("RETRANS_PAYLOAD_SIZE", "RetransPayloadSize", "i64"),
    ("THROUGHPUT_TIME", "ThroughputTime", "f64"),
)
_XP_DELTA: tuple[tuple[str, str, str, int | None], ...] = tuple(
    (f"DLT_{col}_{d}", f"Traffic_Delta_{full}_{field}", kind, None)
    for d, full in (("UP", "Uplink"), ("DN", "Dnlink"))
    for col, field, kind in _DLT_PARTS
)
for _s in UPLANE_FIELDS:
    UPLANE_FIELDS[_s] = UPLANE_FIELDS[_s] + _XP_SESSION + _XP_DELTA
del _s

#: 분 귀속 컬럼 — **C-Plane 과 U-Plane 이 같은 이름을 쓴다**(2026-10-14).
#: 예전에는 U-Plane 만 `SUMMARY_CREATE_TIME`(규격 `SummaryCreateTime`)이고
#: C-Plane 은 `EVENT_TIME` 이었다. 한 묶음으로 묶으면서 통일했다 — 상용
#: `PM_*` 와 집계 `PM_COR_*`(`EVENT_TIME`)에 대응이 분명한 쪽이다.
#: U-Plane 이 Parquet 로 나간 뒤에도 **컬럼 이름은 그대로다** — graphio 쪽
#: 정본은 `store/tables.py:UPLANE_TIME` 이고 **같은 값이어야 한다.**
UPLANE_SLOT = "EVENT_TIME"

#: U-Plane 계열 순서. 로그·집계 보고에서 자리를 고정한다.
#: **graphio 쪽 정본은 `store/tables.py:UPLANE_SERIES` 이고 같은 값이어야
#: 한다** — 그 문자열이 `GP_KPI_U_CELL_1M.SERIES` 에 그대로 들어간다.
UPLANE_SERIES: tuple[str, ...] = ("transport", "dns", "http")

#: U-Plane Parquet 를 몇 분치 남길지. **사람이 10분으로 정했다**(2026-10-14).
#: 집계 여부를 보지 않는다 — 근거는 `raw_parquet.purge` 머리말.
#: 설정으로 덮어쓴다(`pemdb1.uplane_parquet.retain_minutes`).
UPLANE_RETAIN_MINUTES = 10

#: U-Plane Parquet 루트의 기본값. **설정이 정본이다**
#: (`pemdb1.uplane_parquet.root`) — 이 값은 설정을 빠뜨렸을 때의 폴백이고,
#: 읽는 쪽 기본값(graphio `paths.uplane_raw`)과 **같은 디렉터리를 가리켜야
#: 한다.** 어긋나면 집계가 조용히 0행이 된다.
#:
#: **데이터를 양쪽 코드 저장소 밖으로 옮겼다**(2026-10-14 저녁). 전에는
#: `../graphio-probe-rca/data/uplane_raw` 로 **읽는 쪽 git 작업 디렉터리**를
#: 가리켰다 — 쓰는 쪽이 남의 저장소에 분당 수 MB 를 쌓는 모양이었다.
#: `graphio-data-generator/` 는 git 저장소가 아니어서 그 자리에 맞고,
#: 운영의 `/DATA1/summary` 와 같은 성격이 된다.
#:
#: 상대경로는 `cli._uplane_store` 가 **`HERE`(저장소 루트) 기준**으로 푼다 —
#: cwd 와 무관하므로 어디서 띄워도 같은 디렉터리다.
UPLANE_ROOT_DEFAULT = "../graphio-data-generator/data/uplane_raw"


# --------------------------------------------------------------------------
# C-Plane 원시 Parquet — 2차 전환 (2026-10-14)
# --------------------------------------------------------------------------

#: C-Plane 계열 이름. **디렉터리 이름이 되고 `graphio` 쪽과 같아야 한다**
#: (`store/tables.py:CPLANE_SERIES`). 생성기 쪽 `SERIES_FIELDS` 의 키와도
#: 같은 값이라 한 글자만 어긋나도 집계가 조용히 0행이 된다.
CPLANE_SERIES = "lte_call"

#: 분 귀속 컬럼. U-Plane 과 **같은 이름**이고 graphio 쪽 정본은
#: `store/tables.py:XDR_TIME` 이다.
CPLANE_SLOT = "EVENT_TIME"

#: C-Plane Parquet 를 몇 분치 남길지. **1440분 = 24시간이다.**
#:
#: U-Plane(10분)보다 길게 두는 이유가 둘이다.
#:
#: 1. **구 MySQL 세대와 같은 창을 유지한다.** 파티션 보관이 `RETAIN_HOURS=24`
#:    였다. 저장소를 바꾼 것이 기능 범위를 조용히 줄이면 안 된다.
#: 2. **끊어 둔 기능을 되살릴 여지다.** SCR-06(호 조회)·SCR-07(가입자 추적)·
#:    EV-01·02·03·04c 는 "원시를 직접 조회"가 필요해서 끊었다(사람 지시).
#:    되살리기로 결정되면 **이 보관 기간 안의 구간만** 조회할 수 있다 —
#:    10분으로 두면 되살려도 쓸 수 없다.
#:
#: 설정으로 덮어쓴다(`pemdb1.cplane_parquet.retain_minutes`).
#: 디스크 비용은 실측 1분치 zstd 약 1.4MB → 24시간 약 2GB 다.
CPLANE_RETAIN_MINUTES = 1440

#: C-Plane Parquet 루트의 기본값. **설정이 정본이다**
#: (`pemdb1.cplane_parquet.root`) — 읽는 쪽 기본값(graphio
#: `paths.cplane_raw`)과 **같은 디렉터리를 가리켜야 한다.** 어긋나면 집계가
#: 조용히 0행이 된다. 저장소 밖으로 옮긴 근거는 위 `UPLANE_ROOT_DEFAULT`.
CPLANE_ROOT_DEFAULT = "../graphio-data-generator/data/cplane_raw"

#: C-Plane `(Parquet 컬럼, 규격 필드명, varchar 폭)` 153개 — **위치 대응이다.**
#:
#: ## 왜 코드에 있나
#:
#: 전에는 `xdr_mapping()` 이 `information_schema` 에서 컬럼 순서와 폭을
#: 읽었다. 테이블을 지우면 그 원천이 없어지므로 정본이 코드로 옮겨 온다 —
#: `UPLANE_FIELDS` 가 같은 이유로 그렇게 되어 있다. 값은 **삭제 직전의
#: `GP_RAW_LTE_CALL_KPI` 에서 그대로 떠 왔다**(154컬럼 중 `EVENT_TIME` 을 뺀
#: 153개). 집계 결과가 전후 동일해야 하고, 폭이 바뀌면 절단 결과가 조용히
#: 달라진다.
#:
#: ## 왜 이름이 아니라 위치로 맞추나
#:
#: U-Plane 은 규격 415필드 중 55개만 뽑으므로 위치 대응이 없어 이름으로
#: 맞췄다. C-Plane 은 생성기 필드 153개가 테이블 컬럼과 **1:1 순서 대응**이고
#: 이름이 서로 다르다(`AuthenticationInformation_Time` →
#: `AUTHINFORMATION_TIME` 처럼 8개가 줄여져 있고, `RESERVED_151` →
#: `RESERVED3` 처럼 번호도 다르다). 구 `xdr_mapping` 이 위치로 맞춘 이유가
#: 그것이고, 그 대응을 그대로 굳혔다. 두 번째 자리에 규격 필드명을 함께
#: 적어 두는 것은 **대응이 어긋났을 때 사람이 짚을 수 있게** 하려는 것이다 —
#: `cplane_spec()` 이 기동 때 생성기 필드와 대조한다.
#:
#: ## 타입이 없다 — 전부 `str` 이다
#:
#: `GP_RAW_LTE_CALL_KPI` 는 153컬럼이 전부 varchar 였고(§3 "모든 컬럼이
#: varchar 다") 집계가 그 전제로 쓰여 있다. Parquet 에서 숫자로 바꾸면
#: `'0'` 센티널·빈 문자열·`'021'` 같은 0채움이 전부 다르게 접히고,
#: **플래그 비교(`= '1'`)부터 어긋난다.** 저장소만 바꾸는 작업이므로
#: 표현은 그대로 둔다 — 숫자 판정은 읽는 쪽(graphio `metrics/cell_agg.py`)이
#: 구 SQL 과 같은 식으로 한다.
#:
#: 폭은 **0 이 "제한 없음"이 아니라 `EVENT_TIME` 뿐이고, 그것은 이 표에
#: 없다.** 153개 전부 양수다.
CPLANE_FIELDS: tuple[tuple[str, str, int], ...] = (
    ("SUMMARYCREATETIME", "SummaryCreateTime", 20),
    ("ONGOINGFLAG", "OngoingFlag", 1),
    ("IMSI", "IMSI", 30),
    ("mdn", "MDN", 30),
    ("IMEI", "IMEI", 30),
    ("SERVICECODE", "ServiceCode", 30),
    ("PAYCODE", "PayCode", 10),
    ("GENDER", "Gender", 2),
    ("AGE", "Age", 4),
    ("VENDOR", "Vendor", 6),
    ("MODEL", "Model", 4),
    ("PGW_ID", "PGW_ID", 10),
    ("IMS_PGW_ID", "ims_PGW_ID", 10),
    ("SGW_ID", "SGW_ID", 10),
    ("MME_ID", "MME_ID", 10),
    ("S6A_AUTHENTICATION_EQUIP_TYPE", "S6a_Authentication_Equip_Type", 1),
    ("S6A_AUTHENTICATION_EQUIP_ID", "S6a_Authentication_Equip_ID", 10),
    ("S6A_LOCATION_EQUIP_TYPE", "S6a_Location_Equip_Type", 1),
    ("S6A_LOCATION_EQUIP_ID", "S6a_Location_Equip_ID", 10),
    ("S13_EQUIP_TYPE", "S13_Equip_Type", 1),
    ("S13_EQUIP_ID", "S13_Equip_ID", 10),
    ("FIRST_ENB_ID", "First_eNB_ID", 30),
    ("FIRST_CELL_ID", "First_Cell_ID", 3),
    ("FIRST_ENB_VLAN_ID", "First_eNB_VLAN_ID", 30),
    ("LAST_ENB_ID", "Last_eNB_ID", 30),
    ("LAST_CELL_ID", "Last_Cell_ID", 3),
    ("LAST_ENB_VLAN_ID", "Last_eNB_VLAN_ID", 30),
    ("PDN_TYPE", "PDN_Type", 1),
    ("PDN_IPV4", "PDN_IPv4", 10),
    ("PDN_IPV6", "PDN_IPv6", 32),
    ("IMS_PDN_TYPE", "ims_PDN_Type", 1),
    ("IMS_PDN_IPV4", "ims_PDN_IPv4", 10),
    ("IMS_PDN_IPV6", "ims_PDN_IPv6", 32),
    ("OLD_CALL_TYPE", "old_call_type", 2),
    ("OLD_CALL_END_TIME", "old_call_end_time", 20),
    ("OLD_CALL_LAST_ENB_ID", "old_call_Last_eNB_ID", 30),
    ("OLD_CALL_LAST_CELL_ID", "old_call_Last_Cell_ID", 3),
    ("OLD_CALL_LAST_TAC", "old_call_Last_TAC", 10),
    ("CALL_TYPE", "call_type", 2),
    ("CALL_START_TIME", "call_start_time", 20),
    ("CALL_END_TIME", "call_end_time", 20),
    ("CALL_DURATION_TIME", "call_duration_time", 20),
    ("APN", "APN", 100),
    ("IMS_APN", "ims_APN", 100),
    ("S6A_ERROR_MESSAGE", "s6a_error_Message", 3),
    ("S6A_ERROR_TIME", "s6a_error_Time", 20),
    ("S6A_ERROR_CAUSE", "s6a_error_Cause", 10),
    ("AUTHINFORMATION_TIME", "AuthenticationInformation_Time", 20),
    ("AUTHINFORMATION_CAUSE", "AuthenticationInformation_Cause", 10),
    ("UPDATELOCATION_TIME", "UpdateLocation_Time", 20),
    ("UPDATELOCATION_CAUSE", "UpdateLocation_Cause", 10),
    ("S13_ERROR_MESSAGE", "s13_error_Message", 3),
    ("S13_ERROR_TIME", "s13_error_Time", 20),
    ("S13_ERROR_CAUSE", "s13_error_Cause", 10),
    ("MEIDENTITYCHECK_TIME", "MEIdentityCheck_Time", 20),
    ("MEIDENTITYCHECK_CAUSE", "MEIdentityCheck_Cause", 10),
    ("S1AP_ERROR_MESSAGE", "s1ap_error_Message", 10),
    ("S1AP_ERROR_TIME", "s1ap_error_Time", 20),
    ("S1AP_ERROR_CAUSE", "s1ap_error_Cause", 10),
    ("CNDOMAIN", "CNDomain", 2),
    ("INITMSG_RRC_ESTABLISH_CAUSE", "InitialUEMessage_RRC_Establishment_Cause", 2),
    ("PATHSWITCH_COUNT", "PathSwitch_count", 10),
    ("PATHSWITCHFAILURE_COUNT", "PathSwitchFailure_count", 10),
    ("UECONTEXTRELEASEREQUEST_TIME", "UEContextReleaseRequest_Time", 20),
    ("UECONTEXTRELEASEREQUEST_CAUSE", "UEContextReleaseRequest_Cause", 10),
    ("UECONTEXTRELEASE_TIME", "UEContextRelease_Time", 20),
    ("UECONTEXTRELEASE_CAUSE", "UEContextRelease_Cause", 10),
    ("EMM_ERROR_MESSAGE", "emm_error_Message", 10),
    ("EMM_ERROR_TIME", "emm_error_Time", 20),
    ("EMM_ERROR_CAUSE", "emm_error_Cause", 10),
    ("DETACHREQUEST_TIME", "DetachRequest_Time", 20),
    ("DETACHREQUEST_CAUSE", "DetachRequest_Cause", 10),
    ("DETACHREQUEST_TYPE", "DetachRequest_Type", 2),
    ("DETACHREQUEST_SWITCHOFF", "DetachRequest_Switchoff", 2),
    ("DETACHREQUEST_DIRECTION", "DetachRequest_Direction", 2),
    ("ESM_ERROR_MESSAGE", "esm_error_Message", 10),
    ("ESM_ERROR_TIME", "esm_error_Time", 20),
    ("ESM_ERROR_CAUSE", "esm_error_Cause", 10),
    ("S11_ERROR_MESSAGE", "s11_error_Message", 10),
    ("S11_ERROR_TIME", "s11_error_Time", 20),
    ("S11_ERROR_CAUSE", "s11_error_Cause", 10),
    ("S10_ERROR_MESSAGE", "s10_error_Message", 10),
    ("S10_ERROR_TIME", "s10_error_Time", 20),
    ("S10_ERROR_CAUSE", "s10_error_Cause", 10),
    ("S3_ERROR_MESSAGE", "s3_error_Message", 10),
    ("S3_ERROR_TIME", "s3_error_Time", 20),
    ("S3_ERROR_CAUSE", "s3_error_Cause", 10),
    ("SMS_MO_CP_ERROR_CAUSE", "sms_mo_cp_error_cause", 10),
    ("SMS_MO_RP_ERROR_CAUSE", "sms_mo_rp_error_cause", 10),
    ("SMS_MO_TP_ERROR_CAUSE", "sms_mo_tp_error_cause", 10),
    ("SMS_MT_CP_ERROR_CAUSE", "sms_mt_cp_error_cause", 10),
    ("SMS_MT_RP_ERROR_CAUSE", "sms_mt_rp_error_cause", 10),
    ("SMS_MT_TP_ERROR_CAUSE", "sms_mt_tp_error_cause", 10),
    ("ATTEMPT_FLAG", "attempt_flag", 1),
    ("SUCCESS_FLAG", "success_flag", 1),
    ("DATA_ATTEMPT_FLAG", "data_attempt_flag", 1),
    ("DATA_SUCCESS_FLAG", "data_success_flag", 1),
    ("IMS_ATTEMPT_FLAG", "ims_attempt_flag", 1),
    ("IMS_SUCCESS_FLAG", "ims_success_flag", 1),
    ("DROP_FLAG", "drop_flag", 1),
    ("PAGING_ATTEMPT_FLAG", "paging_attempt_flag", 1),
    ("PAGING_SUCCESS_FLAG", "paging_success_flag", 1),
    ("DETACH_FLAG", "detach_flag", 1),
    ("NPR_FLAG", "npr_flag", 1),
    ("AUTH_ATTEMPT_FLAG", "auth_attempt_flag", 1),
    ("AUTH_SUCCESS_FLAG", "auth_success_flag", 1),
    ("LOCATION_ATTEMPT_FLAG", "location_attempt_flag", 1),
    ("LOCATION_SUCCESS_FLAG", "location_success_flag", 1),
    ("MECHECK_ATTEMPT_FLAG", "mecheck_attempt_flag", 1),
    ("MECHECK_SUCCESS_FLAG", "mecheck_success_flag", 1),
    ("FIRST_ERROR_INTERFACE_PROTOCOL", "first_error_interface_protocol", 2),
    ("FIRST_ERROR_MESSAGE", "first_error_message", 3),
    ("FIRST_ERROR_TIME", "first_error_time", 20),
    ("FIRST_ERROR_CAUSE", "first_error_cause", 10),
    ("LAST_ERROR_INTERFACE_PROTOCOL", "last_error_interface_protocol", 2),
    ("LAST_ERROR_MESSAGE", "last_error_message", 3),
    ("LAST_ERROR_TIME", "last_error_time", 20),
    ("LAST_ERROR_CAUSE", "last_error_cause", 10),
    ("INTERVAL_FIRST_ENB_ID", "interval_First_eNB_ID", 30),
    ("INTERVAL_FIRST_ENB_IP", "interval_First_eNB_IP", 10),
    ("INTERVAL_FIRST_CELL_ID", "interval_First_Cell_ID", 3),
    ("INTERVAL_FIRST_TAC", "interval_First_TAC", 10),
    ("INTERVAL_FIRST_ENB_C_UID", "interval_First_eNB_C_UID", 10),
    ("INTERVAL_FIRST_ENB_VLAN_ID", "interval_First_eNB_VLAN_ID", 30),
    ("INTERVAL_CALL_START_TIME", "interval_call_start_time", 20),
    ("OLD_CALL_S1AP_RELEASE_CAUSE", "old_call_s1ap_release_cause", 10),
    ("INITIAL_ACCESS_DURATION", "initial_access_duration", 20),
    ("INITIAL_ACCESS_MSG_COUNT", "initial_access_msg_count", 10),
    ("INITIAL_CORE_DURATION", "initial_core_duration", 20),
    ("INITIAL_CORE_MSG_COUNT", "initial_core_msg_count", 10),
    ("INITIAL_PAGING_DURATION", "initial_paging_duration", 20),
    ("INITIAL_PAGING_MSG_COUNT", "initial_paging_msg_count", 10),
    ("INITIAL_PAGING_ATTEMPT_COUNT", "initial_paging_attempt_count", 10),
    ("IMSI_MCC_MNC_INFO", "imsi_mcc_mnc_info", 7),
    ("UE_DCNR", "ue_dcnr", 2),
    ("INITIAL_NR_CONN_TIME", "initial_nr_conn_time", 20),
    ("UE_USAGE_TYPE", "ue_usage_type", 3),
    ("MME_RESTRICT_DCNR", "mme_restrict_dcnr", 2),
    ("MODIFYBEARER_SUCCESS_COUNT", "ModifyBearer_success_count", 10),
    ("INITIALUEMESSAGE_TIME", "InitialUEMessage_Time", 20),
    ("EXCHANGENUMBER", "ExchangeNumber", 4),
    ("NAS_KEY_VALIDITY", "nas_key_validity", 3),
    ("EQUIP_NW", "equip_nw", 3),
    ("FIRST_ENB_PLMN", "First_eNB_PLMN", 6),
    ("LAST_ENB_PLMN", "Last_eNB_PLMN", 6),
    ("OLD_CALL_LAST_ENB_PLMN", "old_call_Last_eNB_PLMN", 6),
    ("INTERVAL_FIRST_ENB_PLMN", "interval_First_eNB_PLMN", 6),
    ("OLD_CALL_ERROR_INF_PROTOCOL", "old_call_error_interface_protocol", 2),
    ("OLD_CALL_ERROR_CAUSE", "old_call_error_cause", 10),
    ("RESERVED3", "RESERVED_151", 10),
    ("RESERVED4", "RESERVED_152", 10),
    ("RESERVED5", "RESERVED_153", 10),
    ("RESERVED6", "RESERVED_154", 10),
)

#: C-Plane Parquet 에서 **값을 비우는** 컬럼(`write_xdr`). 컬럼 자체는 남는다.
CPLANE_REDACTED: tuple[str, ...] = ("GENDER", "AGE", "EXCHANGENUMBER")


# --------------------------------------------------------------------------
# 평시 배경 — 장애가 없을 때도 분모가 있어야 한다
# --------------------------------------------------------------------------

#: Call Type(`CD_CALL_TYPE` 규격 체계 `1`~`11`) → 절차 그룹.
#:
#: 평시 배경이 그룹별로 다른 비율을 쓴다 — Attach 는 인증을 100% 하고
#: Service 는 2% 만 한다. **모르는 코드는 `"service"` 다**(불변규칙 17) —
#: 0 으로 떨어뜨리면 "그 절차는 인증을 안 한다"와 "코드를 모른다"가 섞인다.
_CT_GROUP = {
    "1": "attach", "2": "attach", "8": "attach",    # Attach MO/MT · Ext Svc MT
    "5": "tau",
    "3": "service", "4": "service", "7": "service",
    "6": "paging",
    "9": "detach", "10": "handover", "11": "handover",
}

#: 절단 사유 그룹 → S1AP Cause 후보. **전부 `S1AP_NORMAL_CAUSES` 밖이어야
#: `DROP_CNT` 가 세어진다**(불변규칙 6 · `_p_drop`).
#:
#: ⚠️ `201`·`303`·`406`·`504` 는 graphio `S1AP_UNSPECIFIED` 라
#: `DROP_TRANSPORT`/`NAS`/`PROTO`/`NODE` 가 아니라 **`DROP_UNSPEC` 으로 간다**
#: (`cell_agg._drop_group` 이 `exclude=UNSPEC_CODES`). V3 §3.4-③ 의 표가
#: `transport` 에 `201` 을, `unspec` 에도 `201` 을 둔 것은 그 사정을 반영한
#: 것이고, 결과적으로 `DROP_TRANSPORT` 실측 비중은 적어 둔 12% 의 절반이 된다.
_DROP_CAUSES = {
    # **정상 종료도 절단으로 센다** (2026-10-22 운영 실측 교정).
    #
    # 구 구현은 여섯 그룹 전부 `S1AP_NORMAL_CAUSES` **밖**이었다. 근거는
    # "정상 종료면 `DROP_CNT` 가 안 세어진다" 였는데 **그것이 틀렸다** —
    # `cell_agg.py:491` 의 `DROP_CNT` 는 `DROP_FLAG` 만 본다(Cause 무관).
    # 정상 종료는 `DROP_NORMAL`(:536)이 **따로** 세고, 상용과 대조할 때만
    # `DROP_CNT - DROP_NORMAL` 을 쓴다(그 파일 §"다른 담당자에게").
    #
    # 그 결과 `DROP_NORMAL` 이 **항상 0** 이라 한 번도 검증되지 않았다.
    # 운영 실측(`drop_flag=1` 157건)은 정반대다:
    #
    #     normal (300·302)   72건  45.9%   ← 가장 많다
    #     unspec (504)       61건  38.9%
    #     radio  (1xx)       24건  15.3%
    #     transport/node/nas/proto  0건
    #
    # **`C-04` 는 바뀌지 않는다** — `drop_rate` 가 그대로고 `DROP_CNT` 는
    # 플래그만 보므로 절단 **분류**만 달라진다.
    "normal": (300, 302),                   # S1AP_NORMAL_CAUSES → DROP_NORMAL
    "radio": (121, 125, 126, 110, 112),     # S1AP 1xx → DROP_RADIO
    "transport": (200, 201),                # 2xx → DROP_TRANSPORT
    "node": (500, 501, 502, 503),           # 5xx → DROP_NODE
    "nas": (301, 303, 304),                 # 3xx → DROP_NAS
    "proto": (400, 401, 402, 403, 404, 405, 406),   # 4xx → DROP_PROTO
    "unspec": (201, 303, 406, 504),         # S1AP_UNSPECIFIED → DROP_UNSPEC
}


@dataclass
class Baseline:
    """평시 배경의 모수 — `config/datagen.yaml` 의 `pemdb1.baseline`.

    ## 왜 필요한가

    `success_only=True` 가 "평소 전부 녹색"을 만드는데, 그것이 지나쳐 **분모까지
    0 으로 만들었다**. 실측(`cplane_raw` 194만 건) —

        P-01 인증 성공률     상시 0%   ← 분모가 장애 호뿐이었다
        P-02 가입정보 성공률  분모 0    ← `location_*_flag` 미기입
        P-03 단말식별 성공률  분모 0    ← `mecheck_*_flag` 미기입
        P-04 페이징 성공률    상시 100% ← 실패 0건
        P-07 NPR 발생률      전부 0    ← `npr_flag` 미기입
        C-04 절단률          0.0036%   ← `success_only` 가 `drop=0` 강제
        PSF  PathSwitch 실패  분모 0    ← `PathSwitch_count` 미기입

    분모가 없으면 `R-01`(P-01)·`R-01b`(P-02)·`R-25`(P-04)·`R-17`(PSF) 이
    **구조적으로 발동할 수 없고**, SC-04·14·15·09 는 주입해도 아무 일이
    일어나지 않는다. 평시 배경은 "실패를 만드는 것"이 아니라 **분모를 만드는
    것**이고, 그래서 `success_only` 와 무관하게 적용한다.

    ## 🔴 전부 PoC 기본값이다

    운영 실측이 아니다(V3 §12.1 의 1~8번). 운영 값이 들어오면 yaml 한 곳만
    고친다 — 주석·문서에 "실측"이라고 적지 않는다.
    """

    #: 절차 시도 비율 — `_CT_GROUP` 의 그룹별. 이 값이 `P-01` 의 **분모**를
    #: 정한다. 너무 낮으면 `MIN_PROCEDURE_DENOM=40` 을 못 채워 `R-01` 이
    #: 발동하지 않고, 너무 높으면 인증이 모든 호에 붙어 비현실적이 된다.
    #: 분당 6,000호 기준 Attach 0.29% + TAU 3.2%×0.35 + Service 95%×0.02
    #: ≈ 2.9% → 분당 174건이라 1분 창에서 분모 40을 넘는다.
    auth_attempt: dict = field(
        default_factory=lambda: {"attach": 1.00, "tau": 0.35, "service": 0.02})
    auth_success: float = 0.999
    location_attempt: dict = field(
        default_factory=lambda: {"attach": 1.00, "tau": 0.90, "service": 0.00})
    location_success: float = 0.9995
    mecheck_attempt: dict = field(
        default_factory=lambda: {"attach": 0.60, "tau": 0.00, "service": 0.00})
    mecheck_success: float = 0.9998
    #: `paging_attempt` 는 실측 `proc_ratio` 를 그대로 쓴다 — 여기는 성공률만
    paging_success: float = 0.98
    npr_rate: float = 0.0008
    #: 연결 **후** 절단. `success=1` 인 호에만 선다(규칙 C-2)
    drop_rate: float = 0.003
    #: 절단 사유 구성비. **합이 1.0 이어야 한다** — 아니면 멈춘다
    #: 절단 사유 구성비. **운영 실측 기반**(2026-10-22) — 근거는
    #: `_DROP_CAUSES` 머리말. 관측된 세 그룹이 0.93 이고 나머지 넷은
    #: 실측 0건이지만 **표본이 157건이라 "없다"로 단정하지 않고** 최소치만
    #: 남긴다. KPI 를 켜려고 비중을 올리지는 않는다.
    drop_cause_mix: dict = field(default_factory=lambda: {
        "normal": 0.45, "unspec": 0.33, "radio": 0.15,
        "transport": 0.03, "node": 0.02, "nas": 0.01, "proto": 0.01,
    })
    #: PathSwitch 시도 비율 — 그룹별. `PSF` 의 분모(`ps_cnt + psf_cnt`)다
    pathswitch_attempt: dict = field(
        default_factory=lambda: {"service": 0.08, "handover": 1.00})
    pathswitch_fail: float = 0.02
    #: 직전 호 기록(`old_call_*`)을 남기는 비율. **`OLD_FAIL_CNT` 의 분자가
    #: 여기서 생긴다** — 비워 두면 `PM_COR_LTE_CALL_1M.OLD_FAIL_CNT` 와
    #: `OLD_SAME_IFACE` 두 컬럼이 **구조적으로 늘 0** 이고, 0 은 "직전 호
    #: 실패가 없다"로 읽힌다(불변규칙 17). 실패한 호에만 적용된다.
    old_call_fail: float = 0.18
    #: 그중 **직전 호와 같은 구간**에서 실패한 비율 — `OLD_SAME_IFACE`.
    #: "같은 원인의 반복" 신호이고, 재시도가 같은 자리에서 또 막히는 모양이다.
    old_call_same_iface: float = 0.55

    @classmethod
    def from_cfg(cls, cfg: dict | None) -> "Baseline":
        """`Shape.from_cfg` 와 같은 모양 — **모르는 키는 멈춘다.**

        오타가 "기본값으로 돌았다"로 끝나면 평시 분모가 조용히 어긋나고,
        그것은 탐지가 안 되는 형태로만 나타난다.

        dict 필드는 **덮지 않고 합친다** — `auth_attempt: {service: 0.05}` 만
        적어도 attach·tau 가 살아 있어야 한다.
        """
        b = cls()
        for k, v in (cfg or {}).items():
            if not hasattr(b, k):
                raise RuntimeError(f"pemdb1.baseline 의 모르는 키: {k}")
            cur = getattr(b, k)
            if isinstance(cur, dict):
                unknown = set(v or {}) - set(cur)
                if unknown:
                    raise RuntimeError(
                        f"pemdb1.baseline.{k} 의 모르는 키: {sorted(unknown)}")
                cur.update({ik: float(iv) for ik, iv in (v or {}).items()})
            else:
                setattr(b, k, float(v))
        total = sum(b.drop_cause_mix.values())
        if abs(total - 1.0) > 1e-6:
            raise RuntimeError(
                f"pemdb1.baseline.drop_cause_mix 의 합이 {total:g} 다 — 1.0 이어야 "
                "한다. 합이 1 이 아니면 그룹 비율이 적어 둔 값과 달라지고, "
                "`DROP_RADIO/DROP_CNT` 통과 조건이 왜 안 맞는지 알 수 없다")
        return b


# --------------------------------------------------------------------------
# 실측 분포
# --------------------------------------------------------------------------


class Distributions:
    """`_distributions.json` 의 실측 구성비 샘플러.

    생성기 자체 프로파일(`data/profile.json`)은 PoC 용 합성 분포라 상용과
    구성비가 다르다. CALL_TYPE 비율이 틀리면 트래픽 구성비가 바뀌어 품질이
    그대로여도 전체 합산 소통률이 움직인다(불변규칙 3).
    """

    #: 희소 Call Type 의 **최소 비율**. 실측 그대로 뽑으면 분당 20,000호에서
    #: 어떤 절차는 1건도 안 나온다 —
    #:
    #:     2 Attach MT   0.0024% → 0.5건/분
    #:     9 Detach MO   0.0140% → 2.8건/분
    #:     7 Ext Svc MO  0.0214% → 4.3건/분
    #:
    #: 최소 표본은 분모 20건이고(불변규칙 5) 그 위에 이항검정이 한 겹 더 있다.
    #: 분당 몇 건짜리 절차는 30분 창에서도 판정이 서지 않아 **화면·탐지·RCA 가
    #: 그 절차를 한 번도 다루지 않는다** — 에러 없이 기능이 비어 있는 상태다.
    #:
    #: 0.3% 면 분당 60건이라 1분 창에서 분모 20을 넘는다. **실측 분포를 일부러
    #: 왜곡하는 것**이므로 근거를 남긴다: 사용자 지시(2026-10-14) — "에러 case
    #: 말고도 조금씩 데이터가 만들어져야 한다". 구성비 자체는 그대로다 —
    #: Service(3·4)가 여전히 95% 이상이고 Attach 는 적다(불변규칙 3 의 취지).
    CALL_TYPE_FLOOR = 0.003

    #: 바닥을 **적용하지 않는** Call Type. `codes.CALL_TYPE` 에 정의가 없는 값은
    #: 늘리면 안 된다 — 화면이 `UNKNOWN` 으로 표기하고 H-17(미정의 코드
    #: 출현률)이 오른다. 실측의 `0` 이 그렇다(6건, 규격서에 없음).
    CALL_TYPE_FLOOR_EXCLUDE = frozenset({"0"})

    def __init__(self, path: Path, rng: random.Random, success_only: bool = True,
                 call_type_floor: float | None = None,
                 baseline: "Baseline | None" = None,
                 #: APN 값 변환표(`region.apn_map`). 비면 실측(한국) 값 그대로다.
                 apn_map: dict[str, str] | None = None):
        d = json.loads(path.read_text(encoding="utf-8"))
        self.rng = rng
        self.success_only = success_only
        #: 평시 배경 모수. ``None`` 이면 절차 플래그·절단을 만들지 않는다 —
        #: 구 동작이고, 그때 `P-01`~`P-04`·`C-04`·`PSF` 의 분모가 비는 것도
        #: 그대로다(`Baseline` 머리말의 실측 표).
        self.baseline = baseline

        self.call_types = [e["v"] for e in d["call_type"]]
        self.call_type_w = self._floored_call_type_w(
            self.call_types, [e["n"] for e in d["call_type"]],
            self.CALL_TYPE_FLOOR if call_type_floor is None else call_type_floor,
        )
        #: CALL_TYPE 별 플래그 비율 — "Detach인데 소통 성공" 같은 조합을 막으려면
        #: 플래그를 주변분포로 따로 뽑지 않고 Call Type 조건부로 뽑아야 한다.
        self.flag_ratio = {
            e["v"]: {
                "att": e["att"] / e["n"],
                "suc": e["suc"] / e["n"],
                "drop": e["drop"] / e["n"],
                "detach": e["detach"] / e["n"],
            }
            for e in d["call_type"] if e["n"]
        }

        #: First Error (iface, cause) 결합분포. 주변분포로 따로 뽑으면 실제로
        #: 존재하지 않는 조합(S11 구간에 EMM Cause)이 생겨 룰이 엉뚱하게 발동한다.
        #: iface='0' 은 TIMEOUT(900/901/902) 자리다 — 실측에서 901 이 가장 많다.
        self.errors = [(e["iface"], e["cause"]) for e in d["first_error"]]
        self.error_w = [e["n"] for e in d["first_error"]]

        #: 비정상 해제 Cause — 절단 호에 붙인다. 정상 종료를 절단 호에 붙이면
        #: D-02~D-05 분류가 무의미해진다.
        ab = [e for e in d["release_cause"]
              if e["v"] not in NORMAL_RELEASE and e["v"] != "0"]
        self.abnormal = [e["v"] for e in ab] or ["500"]
        self.abnormal_w = [e["n"] for e in ab] or [1]

        # **비중은 실측 그대로, 값만 그 나라 것으로** (2026-10-30). 실측 분포는
        # 한국 SKT APN 이라 JP 로 돌려도 `*.sktelecom.com…mcc450` 이 나왔다 —
        # 시나리오 정답지(SC-11·19)와 APN 축이 한국 값을 말했다. 표에 없는 값은
        # 그대로 둔다(빈 값 · 표가 모르는 새 값).
        amap = apn_map or {}
        self.apns = [amap.get(e["v"], e["v"]) for e in d["apn"]]
        self.apn_w = [e["n"] for e in d["apn"]]

        self.vendor_models = [(e["vendor"], e["model"]) for e in d["vendor_model"]]
        self.vendor_model_w = [e["n"] for e in d["vendor_model"]]

        #: 부가 절차 플래그(Data/IMS/Paging)의 CALL_TYPE 조건부 비율.
        #: `d["call_type"]` 에는 주 상태 플래그(att/suc/drop/detach)만 있고
        #: 부가 플래그는 `d["flags"]["by_call_type"]` 에 따로 실려 있다 —
        #: 그걸 읽지 않아서 `IMS_ATTEMPT_FLAG`·`DATA_ATTEMPT_FLAG` 가
        #: **320,044행 전부 '0'** 이 됐다(실측). 분모가 0 이면 `C-20`·`C-21`
        #: 이 한 번도 판정되지 않고, SC-12(IMS PDN) 는 변형 조건
        #: (`ims_attempt_flag == '1'`)에 걸릴 레코드가 없어 주입 자체가
        #: 성립하지 않는다. 실측 비율(CALL_TYPE 3·4 에서 ims ~87%)을 쓴다.
        self.proc_ratio: dict[str, dict[str, float]] = {}
        for e in ((d.get("flags") or {}).get("by_call_type") or []):
            n = e.get("n") or 0
            if not n:
                continue
            self.proc_ratio[str(e["CALL_TYPE"])] = {
                k: e.get(k, 0) / n for k in
                ("data_attempt", "data_success", "ims_attempt",
                 "ims_success", "paging_attempt", "paging_success")
            }

        rel = d["release_cause"]
        if success_only:
            # 성공 전용 베이스라인에는 비정상 해제를 남기지 않는다. 실측 비율을
            # 그대로 쓰면 0.1% 가량의 비정상 Cause(504·126·101…)가 섞여
            # PM_ENB_CAUSE_1M 에 평시 잡음으로 남는다 — PoC 는 "평소 전부 녹색"이다.
            rel = [e for e in rel if e["v"] in NORMAL_RELEASE or e["v"] == "0"]
        self.releases = [e["v"] for e in rel]
        self.release_w = [e["n"] for e in rel]

    @classmethod
    def _floored_call_type_w(cls, types: list[str], weights: list[int],
                             floor: float) -> list[float]:
        """희소 Call Type 가중치를 ``floor`` 까지 끌어올린다.

        올린 몫은 **바닥 위에 있는 타입에서 비례 차감**한다. 전체 합을 그대로
        두어야 `rng.choices` 의 상대 비율이 의도한 값이 된다 — 그냥 더하면
        피크 타입(3·4)의 몫이 함께 줄어드는 양을 계산할 수 없다.

        ``floor`` 가 0 이면 실측 그대로 돌려준다(끄는 경로).
        """
        total = float(sum(weights))
        if floor <= 0 or total <= 0:
            return [float(w) for w in weights]

        target = total * floor
        out = [float(w) for w in weights]
        # ① 바닥 미만을 끌어올리고 필요한 양을 센다
        need = 0.0
        donors: list[int] = []
        for i, (v, w) in enumerate(zip(types, out)):
            if v in cls.CALL_TYPE_FLOOR_EXCLUDE:
                continue
            if w < target:
                need += target - w
                out[i] = target
            else:
                donors.append(i)
        if need <= 0 or not donors:
            return out
        # ② 바닥 위에 있는 타입에서 비례 차감. 차감해도 바닥을 깨지 않는다 —
        #    실측에서 3·4 가 95% 이므로 몇 % 를 덜어도 여유가 크다.
        pool = sum(out[i] for i in donors)
        if pool <= need:
            return out                      # 덜 수 없으면 비율만 올린 채 둔다
        for i in donors:
            out[i] -= need * (out[i] / pool)
        return out

    def _pick(self, pool: list, weights: list):
        return self.rng.choices(pool, weights=weights, k=1)[0]

    def call_type(self) -> str:
        return self._pick(self.call_types, self.call_type_w)

    def apn(self) -> str:
        return self._pick(self.apns, self.apn_w)

    def vendor_model(self) -> tuple[str, str]:
        return self._pick(self.vendor_models, self.vendor_model_w)

    def release_cause(self) -> str:
        return self._pick(self.releases, self.release_w)

    def abnormal_release(self) -> str:
        return self._pick(self.abnormal, self.abnormal_w)

    def first_error(self) -> tuple[str, str]:
        return self._pick(self.errors, self.error_w)

    def flags(self, call_type: str) -> dict[str, int]:
        """CALL_TYPE 조건부 플래그. 성공 전용이면 성공=시도·절단=0.

        부가 절차 플래그(Data/IMS/Paging)를 함께 낸다. 주변분포로 따로 뽑지
        않는 이유는 주 상태 플래그와 같다 — `PAGING` 호에 IMS 시도가 서는
        식의 없는 조합이 생기면 `C-20`·`C-21` 교차 KPI 가 무너진다
        (불변규칙 3).
        """
        r = self.flag_ratio.get(call_type)
        if not r:
            return {"att": 0, "suc": 0, "drop": 0, "detach": 0,
                    "data_att": 0, "data_suc": 0, "ims_att": 0, "ims_suc": 0,
                    "pg_att": 0, "pg_suc": 0,
                    "auth_att": 0, "auth_suc": 0, "loc_att": 0, "loc_suc": 0,
                    "mec_att": 0, "mec_suc": 0, "npr": 0}
        rng = self.rng
        att = 1 if rng.random() < r["att"] else 0
        if self.success_only:
            suc, drop = att, 0
        else:
            suc = 1 if att and rng.random() < (r["suc"] / r["att"] if r["att"] else 0) else 0
            # **시도하지 않은 호는 절단되지 않는다** (2026-10-22 교정).
            # 구 구현은 `att` 와 독립으로 뽑아 `att=0 · drop=1` 을 만들었다 —
            # 운영 10만 행에서 `att=0` 인 조합 5종은 **전부 drop=0** 이다.
            # 드물지만(4만 건 중 1건) 분모 없는 분자라 `C-04` 를 흔든다.
            drop = 1 if att and rng.random() < r["drop"] else 0
        detach = 1 if rng.random() < r["detach"] else 0

        p = self.proc_ratio.get(call_type) or {}
        out = {"att": att, "suc": suc, "drop": drop, "detach": detach}
        for key, a_name, s_name in (
            ("data", "data_attempt", "data_success"),
            ("ims", "ims_attempt", "ims_success"),
            ("pg", "paging_attempt", "paging_success"),
        ):
            a = 1 if rng.random() < p.get(a_name, 0.0) else 0
            # **접속 시도가 없으면 Data·IMS 도 없다** (2026-10-22 운영 실측).
            #
            # 구 구현은 `att` 와 **독립 추첨**해서 "시도하지 않은 호에 데이터
            # 세션이 있는" 조합을 2% 만들었다(4만 건 중 812·779건). 운영
            # 10만 행에서 `att=0` 인 조합은 5종뿐이고 **전부 data/ims 가 0** 이다:
            #
            #     0000000000 · 0000000100(detach) · 0000001000(authA)
            #     0000010000(pgA) · 0000010100(pgA+detach)
            #
            # **`pg` 는 제외한다** — 페이징은 망이 UE 를 부르는 것이라 접속
            # 시도와 독립이고, 운영에도 `att=0 · pg_att=1` 이 16,266건 있다.
            if key in ("data", "ims") and not att:
                a = 0
            if not a:
                out[f"{key}_att"], out[f"{key}_suc"] = 0, 0
                continue
            if self.success_only:
                sv = 1
            else:
                num, den = p.get(s_name, 0.0), p.get(a_name, 0.0)
                sv = 1 if rng.random() < (num / den if den else 0.0) else 0
            out[f"{key}_att"], out[f"{key}_suc"] = a, sv

        # ── 평시 배경 (2026-10-22) ────────────────────────────────────────
        #
        # **분모를 만드는 것이지 실패를 만드는 것이 아니다.** 그래서
        # `success_only` 와 무관하게 적용한다 — 성공률은 99.9% 대이고 바뀌는
        # 것은 "그 절차를 몇 건 수행했나"다. 이것이 없으면 `P-01`·`P-02`·
        # `P-03` 의 분모가 0 이라 **그 지표가 한 번도 판정되지 않는다.**
        bl = self.baseline
        if bl is None:
            for k in ("auth_att", "auth_suc", "loc_att", "loc_suc",
                      "mec_att", "mec_suc", "npr"):
                out[k] = 0
            return out
        grp = _CT_GROUP.get(call_type, "service")
        for key, att_map, suc_p in (
            ("auth", bl.auth_attempt, bl.auth_success),
            ("loc", bl.location_attempt, bl.location_success),
            ("mec", bl.mecheck_attempt, bl.mecheck_success),
        ):
            a = 1 if rng.random() < att_map.get(grp, 0.0) else 0
            out[f"{key}_att"] = a
            # 포함 관계 — `성공 ≤ 시도`(규칙 C-4). 시도하지 않은 절차에
            # 성공을 세우면 분자가 분모를 넘는다
            out[f"{key}_suc"] = 1 if a and rng.random() < suc_p else 0
        # 페이징은 **시도 비율을 실측에서 받고**(위 `proc_ratio`) 성공률만 덮는다.
        # 실패 0건이면 `P-04` 가 상시 100% 라 `R-25` 가 발동할 수 없다.
        if out["pg_att"]:
            out["pg_suc"] = 1 if rng.random() < bl.paging_success else 0
        # NPR 은 **페이징을 했을 때만** 성립한다 — 시도가 없으면 "응답이 없다"를
        # 말할 수 없다(2026-10-22 교정). 구 구현은 `pg_att` 와 무관하게 뽑아
        # 4만 건 중 17건의 모순을 만들었다. 운영에서 `npr=1` 인 조합은
        # `1000010010`(pg_att=1) 하나뿐이다.
        out["npr"] = 1 if out["pg_att"] and rng.random() < bl.npr_rate else 0
        # 연결 **후** 절단. `success_only` 모드에서만 평시 배경이 만든다 —
        # 실측 재현 모드(`success_only=False`)는 이미 실측 비율로 절단을
        # 뽑았고(위), 그 위에 또 걸면 두 분포가 겹친다.
        if self.success_only and out["att"] and out["suc"]:
            out["drop"] = 1 if rng.random() < bl.drop_rate else 0
        return out

    def drop_cause(self) -> int:
        """평시 절단의 해제 Cause — `drop_cause_mix` 로 그룹을 뽑고 그 안에서 고른다.

        **`normal` 그룹이 있다**(2026-10-22). 구 docstring 은 *"전부
        `S1AP_NORMAL_CAUSES` 밖이다. 안에 있으면 `DROP_CNT` 가 세어지지 않아
        `C-04` 가 0 으로 남는다"* 였는데 **그 전제가 틀렸다** —
        `cell_agg.DROP_CNT` 는 `DROP_FLAG` 만 보고 Cause 를 보지 않는다.
        근거와 운영 실측은 `_DROP_CAUSES` 머리말.
        """
        bl = self.baseline
        groups = list(bl.drop_cause_mix)
        g = self.rng.choices(groups, weights=[bl.drop_cause_mix[k] for k in groups],
                             k=1)[0]
        return self.rng.choice(_DROP_CAUSES[g])


# --------------------------------------------------------------------------
# 셀 마스터 — 상용 테이블에서 읽는다
# --------------------------------------------------------------------------


def _dms(value: str | None) -> float:
    """`37:33.43/220` → 37.5620. CM_CELL_REP_RU_LOC 의 좌표 표기다.

    도:분.초/밀리초 꼴이다. 파싱 실패는 0.0 — 좌표 없는 셀은 UNMAPPED 로 남는다.
    """
    if not value:
        return 0.0
    try:
        deg, rest = value.split(":", 1)
        mnt, rest = rest.split(".", 1)
        sec, milli = rest.split("/", 1)
        return int(deg) + int(mnt) / 60 + (int(sec) + int(milli) / 1000) / 3600
    except (ValueError, TypeError):
        return 0.0


def _weight(enb_id: str, cellnum: int) -> float:
    """셀별 상대 트래픽 규모 — eNB 해시로 결정론적으로 정한다.

    도심/교외 구분 정보가 마스터에 없다. 균등하게 두면 모든 셀이 같은 표본을
    가져 최소 표본 규칙(분모 20)이 실제로 걸리는지 확인할 수 없으므로, 재현
    가능한 편중을 준다. 같은 셀은 항상 같은 가중치가 나온다.
    """
    h = int(hashlib.sha256(f"{enb_id}:{cellnum}".encode()).hexdigest()[:8], 16)
    # 0.25 ~ 4.0 의 로그 균등 — 상위 셀이 하위 셀의 16배
    w = 0.25 * (16.0 ** ((h % 10_000) / 10_000))
    # `topo` 가 놓은 커버리지용 셀은 **낮은 가중**을 받는다. 그쪽은
    # `_pick_enbs` 가 한도 밖에서 항상 포함하므로, 같은 가중을 주면 분당 호
    # 수가 나뉘어 **기존 eNB 의 1분 창 표본이 깨진다**(근거는 `topo.TOPO_WEIGHT`).
    return w * TOPO_WEIGHT if _in_topo_band(enb_id) else w


#: 시나리오 대상 셀의 `traffic_weight` 하한.
#:
#: ## 왜 필요한가
#:
#: `ground_truth.json` 의 정답 셀 40개 중 **4개가 어떤 창에서도 분모를 못
#: 채웠다**(실측 2026-10-26). SC-21 의 트리거는 `U-12`(down 재전송률)이고
#: 그 분모는 호가 아니라 **패킷**이다(`paypkt_dn` · `min_denom=800` —
#: graphio `detect/detector.py:832` · `metrics/kpi.py:178`). 그 4개는 분당
#: 5~18패킷이라 30분 창(`ESCALATION_WINDOWS` 의 상한)에서도 미달이다.
#: **관측 불가한 셀에 장애를 넣는 시나리오는 성립하지 않는다** — 채점이
#: RCA 품질이 아니라 "그 셀이 보였는가"를 재게 된다.
#:
#: ## 왜 분모를 보장하지 않고 **고르는가**
#:
#: 반대 방향(미달 셀의 분모를 `min_denom` 까지 끌어올리기)은 쓰지 않는다.
#: `detector._significant` 의 통계량이 **분모의 √에 비례**하므로
#: `4 → 800` 으로 보장하면 `√200 = 14.1배` 민감한 탐지기가 되고, 채점이
#: RCA 품질이 아니라 "보장된 분모"를 재게 된다. 이쪽은 **원래 분모가 큰
#: 셀을 고르는 것**이라 생성 규칙이 그대로다 — 그 셀의 분모는 같은 weight
#: 대의 다른 셀과 같다.
#:
#: ## 왜 창고 조회도, 사전 모의도 아닌가
#:
#: `패킷/세션` 은 **셀의 속성이 아니라 "단일 스트림에서 몇 번째로 뽑았나"**
#: 다 — `generator.py:197` 이 계열당 `Sampler` 하나를 두고
#: `profile.py:337` 의 `self.rng.random()` 에 **셀 키가 들어가지 않는다.**
#: 그래서 둘 다 안 된다 —
#:
#:   * **창고 조회** — 그 시드에만 맞는다. 시드 4개 모의에서 미달 집합이
#:     `0 / 4 / 3 / 0` 개로 바뀌고 같은 셀이 **5,100배** 흔들렸다.
#:   * **사전 모의** — 순환이다. 모의가 실제와 같은 값을 받으려면 전 셀의
#:     전 세션을 같은 순서로 뽑아야 하고(= 전체 재실행), 게다가
#:     `_bind_scenario_targets` 가 같은 `rng` 를 소비하므로 **선택을 바꾸면
#:     그 모의가 무효가 된다.**
#:
#: 반면 **`_weight(enb_id, cellnum)` 는 sha256 이라 생성 전에 계산되고
#: 시드·실행과 무관하다.** 그리고 보장해야 하는 것은 `패킷/세션` 이 아니라
#: **세션 수**이고, 그쪽은 weight 가 결정론적으로 정한다
#: (`corr(weight, 세션/분) = 0.987` · 세션합 ≈ 132 × weight — 실측 선형).
#:
#: ## 왜 1.56 인가
#:
#: 세션 **110 이상에서 30분 창 미달 확률 0.0%**(시도 2,000회)이고, 하한
#: 1.56 에서 **풀 766개**(필요 40의 19배) · **미달 0/766** · 1분 창
#: 97.9% 다. 실측 미달 4개의 weight 는 `0.26 · 0.30 · 0.31 · 0.86` 으로
#: 전부 이 아래다.
#:
#: **`1.20` 은 쓰지 않는다** — 미달 0.1% 라 40셀을 뽑으면 **4% 확률로
#: 미달이 끼고** 테스트가 간헐 실패한다. 간헐 실패는 "가끔 빨간 것"이 되어
#: 결국 아무도 보지 않는다.
WEIGHT_FLOOR = 1.56


def scenario_pool(cells: list[Cell]) -> list[Cell]:
    """셀 축 시나리오가 **대상으로 삼을 수 있는** 셀. 순서를 바꾸지 않는다.

    `_weight` 를 그대로 부른다 — 하한을 여기서 다시 계산하면 가중 산식의
    사본이 생기고, 한쪽만 고쳐질 때 "고른 셀의 분모가 작다"가 조용히
    돌아온다. 순서를 유지하는 이유는 호출부의 교체분이 목록 순서에
    의존하기 때문이다(`scenarios._cells_in_one_sgg`).
    """
    return [c for c in cells if _weight(c.enb_id, c.cell_id) >= WEIGHT_FLOOR]


def load_drawable_dongs(path: Path) -> set[str]:
    """graphio 의 행정코드 crosswalk 에서 **폴리곤이 있는 동 코드** 집합을 읽는다.

    반환 원소는 `CM_CELL_REP_RU_LOC` 의 `CITY_ID-GU_ID-DONG_ID` 를 이은 꼴
    (`"11-11110-11110003"`)이다 — graphio `metrics/kpi.py:cell_geo_exprs()` 의
    `adm_emd_cd` 와 같은 형식이라 그대로 비교된다.

    **왜 필요한가.** 번들 경계 geojson 은 **행정동**(3,504) 이고 pemdb1 은
    **법정동**(8,079) 이다. `종로1·2·3·4가동` 같은 통합 행정동은 법정동 목록에
    대응 행이 **원리상 없다** — 이름이 우연히 같은 2,868개만 crosswalk 가
    맞췄다. 그래서 셀을 아무렇게나 고르면 읍면동 지도의 절반이 회색으로 남고,
    운영자는 그것을 "그 지역 문제 없음"으로 읽는다(불변규칙 12).

    해법은 경계 데이터를 새로 구하는 것이 아니라 **그릴 수 있는 동에서 셀을
    고르는 것**이다 — 마스터에 44만 셀이 있고 그중 22만이 폴리곤 있는 동에
    속한다(실측). PoC 에서 셀 선택은 우리 재량이다.

    **파일이 없으면 멈춘다.** 없는 집합을 추측으로 만들면 지도가 또 어긋나고,
    그때는 어디서 어긋났는지 알 방법이 없다.
    """
    if not path.exists():
        raise RuntimeError(
            f"행정코드 crosswalk 가 없다: {path}\n"
            "  graphio 쪽에서 먼저 만들어라 — python -m graphio geo-xw"
        )
    with path.open(encoding="utf-8") as fh:
        xw = json.load(fh)
    emd = ((xw.get("map") or {}).get("emd") or {})
    if not emd:
        raise RuntimeError(f"crosswalk 에 map.emd 가 비어 있다: {path}")
    # 키는 geojson 코드, **값이 pemdb1 코드**다. 뒤집어 쓰면 전량 무매칭이 된다.
    return set(emd.values())


def _filter_drawable(uniq: dict, drawable: set[str] | None) -> dict:
    """폴리곤 있는 동의 셀만 남긴다. 좌표 없는 셀은 **그대로 둔다**.

    `CITY_ID IS NULL` 인 셀은 애초에 동에 닿지 않으므로 읍면동 커버리지의
    분모가 아니고, 빼면 `H-07`(미매핑 감시)과 UNMAPPED 패널이 한 번도
    검증되지 않는다(불변규칙 12). "시뮬레이션이 그 셀을 안 쓴다"와 "미매핑
    셀이 없다"는 다른 사실이다.
    """
    if not drawable:
        return uniq
    out = {}
    for key, r in uniq.items():
        city, gu, dong = r[5], r[6], r[7]
        if city is None:                       # UNMAPPED — 판정 대상이 아니다
            out[key] = r
        elif f"{city}-{gu}-{dong}" in drawable:
            out[key] = r
    return out


def _scope_sql(scope: "Scope | None") -> str:
    """`scope` 의 구·동을 SQL 조건으로. 둘 다 비면 빈 문자열.

    **합집합이다** — "이 구 또는 이 동". 교집합으로 두면 동이 구 안에 있으니
    늘 동만 남아 `gu` 가 무의미해진다.
    """
    if scope is None or (not scope.gu and not scope.dong):
        return ""
    # **노선이 함께 있으면 질의로 좁히지 않는다.** 합집합이어야 하는데
    # 질의가 먼저 좁히면 노선 셀이 그 구 밖에서 잘려 **교집합**이 된다 —
    # 실측에서 "노선 + 신주쿠구"가 10셀이었다(노선 88 · 구 1,114 인데).
    # 그때는 전량을 끌어와 파이썬에서 합친다.
    if scope.route:
        return ""
    parts = []
    if scope.gu:
        ids = ",".join(str(int(x)) for x in scope.gu)
        parts.append(f"r.GU_ID IN ({ids})")
    if scope.dong:
        ids = ",".join(str(int(x)) for x in scope.dong)
        parts.append(f"r.DONG_ID IN ({ids})")
    return " AND (" + " OR ".join(parts) + ")"


def _scope_route(cells: list[Cell], scope: "Scope") -> list[Cell]:
    """노선이 쓰는 셀(+ 반경)만 남긴다. 노선이 없으면 그대로.

    **노선 파일을 읽는다** — 그 파일이 셀 키의 목록이고, 같은 규칙을 여기서
    다시 만들면 두 벌이 된다(한쪽만 고쳐지면 "정의한 구간에 트래픽이 없다"가
    되는데 원인이 안 보인다).

    **모르는 노선 id 는 멈춘다.** 조용히 전량으로 넘어가면 범위를 좁혔다고
    믿는 채로 전 도쿄에 호가 흩어진다 — 그러면 셀당 표본이 0.12호/분이고
    탐지가 서지 않는다(불변규칙 5).
    """
    import json

    want = str(scope.route)
    path = Path(scope.lines_dir)
    found = None
    for f in sorted(path.glob("*.json")) if path.exists() else ():
        for spec in json.loads(f.read_text(encoding="utf-8")):
            if str(spec.get("id")) == want:
                found = spec
                break
        if found:
            break
    if found is None:
        have = sorted(str(spec.get("id"))
                      for f in (sorted(path.glob("*.json"))
                                if path.exists() else ())
                      for spec in json.loads(f.read_text(encoding="utf-8")))
        raise RuntimeError(
            f"scope.route 의 모르는 노선 {want!r} — {path} 에서 못 찾는다"
            f" (아는 것: {have})")

    keys = set(found["cells"])
    on = [c for c in cells if c.cell_key in keys]
    if not scope.route_radius_m:
        return on
    # 반경 — 노선 셀에서 그만큼 안의 셀을 더한다. 노선 셀만 쓰면 **이동
    # 가입자밖에 없어** 역 주변 체류가 생기지 않는다
    import math

    pts = [(c.latitude, c.longitude) for c in on if c.latitude]
    r = float(scope.route_radius_m)
    out, seen = list(on), {c.cell_key for c in on}
    for c in cells:
        if c.cell_key in seen or not c.latitude:
            continue
        k = math.cos(math.radians(c.latitude)) or 1.0
        if any(math.hypot((c.latitude - p[0]) * 111_320.0,
                          (c.longitude - p[1]) * 111_320.0 * k) <= r
               for p in pts):
            out.append(c)
            seen.add(c.cell_key)
    return out


def _pick_enbs(uniq: dict, enb_limit: int | None) -> set[str] | None:
    """생성 대상 eNB 를 결정론적으로 고른다.

    좌표가 있는 eNB 를 셀 수 많은 순으로 채우고, 마지막 5% 는 **좌표 없는
    eNB** 로 남긴다 — 미매핑 셀이 0이면 `H-07`(미매핑 감시)과 UNMAPPED 패널이
    한 번도 검증되지 않는다(불변규칙 12).

    `topo` 대역(`topo.NEW_BAND`)은 **한도와 무관하게 전부 포함한다** — 아래
    include 절의 주석에 근거가 있다.

    **다른 로케일 전용 eNB 는 여기까지 오지 않는다** — `load_cells` 가
    `uniq` 를 만들 때 이미 뺀다(그쪽 주석). 한도가 0 이면 이 함수가
    `None` 을 돌려주므로 여기서 거르면 전량 모드를 놓친다.
    """
    if not enb_limit or enb_limit <= 0:
        return None
    cnt: dict[str, int] = {}
    geo: dict[str, bool] = {}
    for (enb, _cn), r in uniq.items():
        cnt[enb] = cnt.get(enb, 0) + 1
        geo[enb] = geo.get(enb, False) or (r[5] is not None)

    # **`topo` 대역은 한도 밖에서 항상 포함한다** (2026-10-26).
    #
    # `enb_limit` 은 **규모 한도**이고 커버리지는 다른 축이다. 두 목적을 한
    # 숫자에 섞으면 규모를 조절할 때마다 커버리지가 따라 움직인다. 실측:
    # `topo seed-enb` 가 놓는 3셀 eNB 는 셀 수 내림차순에서 **22,557등**이고,
    # 한도로 끌어올리려면 **23,586** 이 필요하다 — 그것은 한도가 아니라
    # **해제**다(후보 mapped eNB 가 22,722개다). 그 사이의 어떤 값도
    # "시군구마다 장비 하나"를 주지 못하고 기존 eNB 의 분당 호 수만 깎는다
    # (330.6 → 61.5 건/분, 한도 333 실측).
    #
    # **정렬 목록에서 아예 빼고 나중에 합친다.** 대역 eNB 를 `mapped` 에
    # 남겨 두면 그것이 한도 칸을 먹어 기존 선택 집합이 바뀐다 —
    # `ground_truth.json` 의 정답 셀 키가 어긋나고, 그 어긋남은 에러가
    # 아니라 채점 점수 하락으로만 보인다.
    band = {e for e in cnt if _in_topo_band(e)}
    mapped = sorted((e for e in cnt if geo[e] and e not in band),
                    key=lambda e: (-cnt[e], e))
    unmapped = sorted((e for e in cnt if not geo[e] and e not in band),
                      key=lambda e: (-cnt[e], e))
    n_un = min(len(unmapped), max(1, enb_limit // 20))
    out = set(unmapped[:n_un])
    out.update(mapped[: max(0, enb_limit - len(out))])
    return out | band


def load_cells(conn, enb_limit: int | None = None,
               drawable: set[str] | None = None,
               ensure_coords: bool = False,
               plmn: Plmn | None = None,
               locale: str = "kr",
               tac_only: bool = False,
               sectors_per_enb: int | None = None,
               scope: "Scope | None" = None) -> list[Cell]:
    """`CL_CELL_MAP` + `CM_CELL_REP_RU_LOC` → `Cell` 목록.

    조인 키는 실측으로 확인된 것을 쓴다. `CM_CELL_REP_RU_LOC.ENB_ID` 는
    **반출 JSON 단계에서 이미 평문**이고 암호문은 `ENB_ID_ENC` 로 따로 남아
    있다(2026-10-13 반출본 통일). `SECTOR` 는 이름과 달리 `CELLNUM` 값이다.

    `XDR_LTE_CALL_KPI.FIRST_ENB_ID` 도 평문 숫자다 — 근거는 생성 데이터가
    아니라 프로브 규격서다(`docs/xdr_spec/XDR_Specification_통합.xlsx`,
    `LTE-Call-KPI` 22행: `First_eNB_ID · uint · NUMBER · 10`,
    "First ECGI에서 eNB ID 파싱"). 그래서 셀 키를 평문으로 맞춘다.

    좌표가 없는 셀도 **버리지 않는다** — 미매핑 셀을 숨기면 그 셀의 이슈가
    영원히 안 보인다(불변규칙 12). `geo_status='UNMAPPED'` 로 남긴다.
    `CL_CELL_MAP`(로컬 574,404행)에는 있지만 `CM_CELL_REP_RU_LOC`(442,689행)에
    없는 셀이 있고, 그런 셀은 좌표가 없어 지리 롤업에서 빠진다. 노드 축
    시나리오(SGW/MME 전체 셀)는 그 셀을 포함하므로 정답 셀의 약 15% 가
    미매핑이다 — **의도된 것이다**(`_pick_enbs` 가 일부러 남긴다. H-07 감시와
    UNMAPPED 패널이 한 번도 검증되지 않는 것을 막는다). 지리 패턴을 채점하는
    CLUSTER·CORRIDOR 시나리오는 `_cells_in_one_sgg` 가 `MAPPED` 만 고르므로
    영향을 받지 않는다(실측 미매핑 0%).

    `enb_limit` 은 생성 대상 eNB 수를 줄인다. 마스터 전량(eNB 32,121)에 분당
    수천 호를 흘리면 eNB 당 표본이 1건 미만이 되어 **어떤 창에서도 판정이
    서지 않는다**(최소 표본 20, 불변규칙 5). 선택은 결정론적이다 — 틱마다
    대상 eNB 가 바뀌면 시계열이 조각난다.

    `drawable` 은 `load_drawable_dongs()` 가 준 **폴리곤 있는 동 코드** 집합이다.
    주면 그 동의 셀(+좌표 없는 셀)만 후보가 되어 읍면동 지도가 전부 칠해진다.
    `None` 이면 종전대로 전량이 후보다.

    **그 집합은 로케일 전용이다.** `code_crosswalk.json` 은
    `southkorea-maps kostat 2018 → pemdb1 행정표준코드` 이고, 일본 동 코드와는
    **우연히 겹치는 것만** 맞는다 — 도쿄 셀에 그대로 걸었더니 1,192셀 중
    **434개만** 남았다(64% 탈락). 남은 것이 "폴리곤이 있는 동"이라는 보장도
    없다. 그래서 `locale != "kr"` 이면 **적용하지 않는다**(2026-10-27).

    일본 지도의 동 커버리지는 graphio 쪽 geo 데이터의 일이고, 그쪽 crosswalk
    가 생기면 여기에 그 집합을 주면 된다.

    ## 생성기는 **KR 벌을 읽는다** (2026-10-26 로케일 축 도입 뒤)

    `locale` 기본이 `"kr"` 이고 그것이 의도다. 사용자 방침은 *"원본 데이터도
    그냥 쓴다"* 이고, 트래픽 키(`(ENB_ID, CELLNUM)`)는 **나라와 무관**하므로
    창고를 다시 굽지 않는다. 나라가 갈리는 것은 graphio 가 그 키에 붙이는
    **지역·좌표 쪽**이다(`store/tables.py` 의 테이블 상수).

    이름을 `topo.table_name` 으로 받는 이유는 **접미사가 한 곳에서만
    결정되게** 하려는 것이다 — 여기 문자열로 적어 두면 로케일 축이 두 벌이
    되고, 한쪽만 고쳐질 때 생성기와 화면이 다른 벌을 본다(에러 없음).
    """
    cellmap_tbl = table_name("cellmap", "kr")   # 로케일 축이 없다 — 한 벌
    cell_tbl = table_name("cell", locale)
    if drawable is not None and locale != "kr":
        # **KR crosswalk 를 JP 셀에 걸지 않는다** — 머리말의 실측
        drawable = None
    with conn.cursor() as cur:
        cur.execute(
            "SELECT c.ENB_ID, c.CELLNUM, c.C_UID, c.MME_GRP_ID, c.EMS_ID, "
            "       r.CITY_ID, r.GU_ID, r.DONG_ID, r.LATITUDE, r.LONGITUDE, "
            "       r.TAC, r.SGW_ID, r.MME_ID, r.PGW_ID "
            f"FROM `{cellmap_tbl}` AS c "
            # **`tac_only` 면 INNER JOIN 이다.** LEFT 로 두고 뒤에서 걸러도
            # 되지만, 질의가 걸러 주는 쪽이 기동이 빠르다
            + (f"JOIN `{cell_tbl}` AS r "
               "       ON r.ENB_ID = c.ENB_ID AND r.SECTOR = c.CELLNUM "
               "      AND r.TAC IS NOT NULL "
               if tac_only else
               f"LEFT JOIN `{cell_tbl}` AS r "
               "       ON r.ENB_ID = c.ENB_ID AND r.SECTOR = c.CELLNUM ")
            + "WHERE c.ENB_ID IS NOT NULL AND c.CELLNUM IS NOT NULL"
            # **구·동은 질의가 거른다.** 파이썬에서 거르면 23,039행을 다
            # 끌어와 버리고, 좁은 범위일수록 낭비가 크다. 노선은 좌표가
            # 필요해 아래에서 거른다
            + _scope_sql(scope)
        )
        rows = cur.fetchall()
        # MME/PGW 풀 — 노드 축(EV-*, 토폴로지 화면)이 실제 장비 ID 를 보게 한다
        cur.execute("SELECT MME_ID, MME_GRP_ID FROM CL_MME WHERE OPR_STATUS='ACT'")
        mmes = cur.fetchall()
        cur.execute("SELECT PGW_ID FROM CL_PGW WHERE OPR_STATUS='ACT'")
        pgws = [r[0] for r in cur.fetchall()]

    by_grp: dict[str, list[str]] = {}
    for mid, grp in mmes:
        by_grp.setdefault(str(grp), []).append(str(mid))
    all_mme = [str(m) for m, _ in mmes] or ["0001"]
    pgws = pgws or ["0037"]

    # (ENB_ID, CELLNUM) 중복 행이 마스터에 있다 — 셀 단위로 접는다
    uniq: dict[tuple[str, int], tuple] = {}
    for r in rows:
        if r[0] is None or r[1] is None:
            continue
        uniq.setdefault((str(r[0]), int(r[1])), r)

    # **섹터 상한** — 시뮬레이션 규모 문제다(2026-10-27).
    #
    # 도쿄 JP 벌은 eNB 당 섹터가 **중앙 26개**다(다중 밴드 도심). 그대로 쓰면
    # 200 eNB 가 5,012셀이고 분당 2,000호에서 셀당 **0.4호**다 — 최소 표본
    # 20(불변규칙 5)을 30분 창에서도 못 채워 **셀 축 판정이 아예 서지 않는다.**
    # 6개면 ≈1,200셀 · 셀당 1.67호/분 · 15분 창에 25호로 찬다.
    #
    # **TA 는 상한과 무관하다** — TAC 는 eNB 속성이고 그 eNB 의 전 섹터가 같은
    # TA 다. 상한은 "그중 몇 개를 시뮬레이션에 쓰나"일 뿐이다.
    #
    # 번호가 작은 쪽을 남긴다 — 어느 섹터를 쓰는지가 실행마다 바뀌면 시계열이
    # 조각난다.
    if sectors_per_enb and sectors_per_enb > 0:
        per: dict[str, list[int]] = {}
        for enb, cellnum in uniq:
            per.setdefault(enb, []).append(cellnum)
        keep_sec = {(e, c) for e, cs in per.items()
                    for c in sorted(cs)[:sectors_per_enb]}
        uniq = {k: v for k, v in uniq.items() if k in keep_sec}

    # **다른 로케일 전용 장비를 이 로케일의 후보에서 뺀다** (2026-10-26).
    #
    # `CL_CELL_MAP` 은 로케일 축이 없는 **공유 테이블**이다. 그래서 JP 추가
    # 구역에 놓은 장비가 거기 들어 있고, 그냥 두면 KR 생성이 그것을 후보로
    # 본다 — KR 셀 벌에 좌표 행이 없으니 **미매핑 셀**이 되어 `H-07` ·
    # UNMAPPED 패널에 상시로 뜬다(불변규칙 12 가 숨기지 못하게 막아 둔 자리).
    #
    # **대역 숫자가 아니라 실제로 넣은 ID 목록으로 뺀다**(`topo.ADDED_FILE`
    # 원장). 숫자를 코드에 박으면 "그 대역이 비어 있다"는 가정이 남는데,
    # 원장은 넣을 때 세 테이블을 실측해 확인한 결과다. 같은 파일이
    # **되돌림 수단**이기도 하다 — `topo.seed_enb_jp` 머리말.
    #
    # **`enb_limit` 보다 앞에서 걸러야 한다.** 한도가 0(전량)이면
    # `_pick_enbs` 가 `None` 을 돌려주고 선택 단계가 없다 — 거기에만 두면
    # 전량 모드에서 조용히 섞인다.
    skip = topo.locale_only_enbs() if locale == "kr" else set()
    if skip:
        before = len(uniq)
        uniq = {k: v for k, v in uniq.items() if k[0] not in skip}
        if before != len(uniq):
            print(f"  · 다른 로케일 전용 eNB 제외 — 셀 {before - len(uniq):,} "
                  f"(원장 {len(skip)}대 · topo.ADDED_FILE)")

    # 폴리곤 있는 동으로 좁힌 **뒤에** eNB 를 고른다. 순서를 바꾸면 고른 eNB 의
    # 셀이 대부분 탈락해 생성 규모가 무너진다.
    uniq = _filter_drawable(uniq, drawable)
    keep = _pick_enbs(uniq, enb_limit)

    # **좌표 결손을 생성기가 채운다** (2026-10-26 · `ensure_coords`).
    #
    # 전에는 `topo fill-coords` 라는 **수동 커맨드**였다. 사람이 기억해서
    # 쳐야 했고 현장에 반출하면 아무도 치지 않는다 — 그러면 그 셀의 집계는
    # 영구히 미매핑이고 지도에 안 나온다. 지금은 셀 목록을 만드는 자리에서
    # 매번 확인한다.
    #
    # **읽기 함수가 쓰기를 하는 모양이므로 플래그로 명시한다.** 기본은
    # `False` 다 — `cmd_capacity` docstring 이 *"셀 마스터는 DB 에서
    # 읽는다. 읽기뿐이다"* 를 보증하고, UI(`ui._cells`)와
    # `topo.pick_report` 도 조회 경로다. 켜는 것은 `cmd_run`·`cmd_pemfill`
    # 둘뿐이다.
    #
    # **메모리 부여가 아니라 DB INSERT 인 이유**와 **의도된 미매핑 108셀을
    # 건드리지 않는 이유**는 `topo.ensure_sector_coords` ·
    # `topo.coord_gaps` 머리말에 있다.
    if ensure_coords:
        gaps = topo.coord_gaps(uniq, keep)
        if gaps:
            made = topo.ensure_sector_coords(conn, gaps, locale=locale)["rows"]
            # **이 틱의 셀 목록에 바로 반영한다.** 갱신하지 않으면 지금
            # 만든 좌표가 다음 틱부터만 쓰이고, 그 한 분의 집계는 미매핑으로
            # 남는다 — 에러가 아니라 지도에 한 칸이 비는 모양이다.
            for key, (city, gu, dong, lat, lon) in made.items():
                r = uniq.get(key)
                if r is not None:
                    # **길이를 지킨다** — 행이 14-tuple(끝이 TAC·SGW_ID·
                    # MME_ID·PGW_ID)이고 아래 언패킹이 그 수를 센다. 줄이면
                    # `ValueError` 로 죽는다
                    uniq[key] = r[:5] + (city, gu, dong, lat, lon) + r[10:14]
    cells: list[Cell] = []
    #: 마스터가 지정한 `(SGW_ID, MME_ID, PGW_ID)` — 셀과 **같은 순서**로 쌓는다.
    #: `Cell` 에 자리를 만들지 않는 이유: 그 값은 "마스터가 뭐라 했나"이고
    #: 최종 배정(`Cell.sgw_id`)과 뜻이 다르다. 섞으면 어느 쪽이 적용됐는지
    #: 알 수 없다.
    equip: list[tuple[str | None, str | None, str | None]] = []
    for (enb, cellnum), (_e, _c, c_uid, grp, ems,
                         city, gu, dong, lat, lon,
                         tac_raw, sgw_raw, mme_raw, pgw_raw) in sorted(uniq.items()):
        # **`NULL` 은 0 이 된다 — "TA 모름"이다.** `int(None)` 이 터지지 않게
        # 걸러 두는 것이 아니라, 아래 장비 유도가 `if tac` 로 갈리기 때문이다
        tac = int(tac_raw) if tac_raw not in (None, "", 0, "0") else 0
        if keep is not None and enb not in keep:
            continue
        # 같은 eNB 의 셀은 같은 Core 노드를 타야 한다 — 노드 축 집계가 성립하려면
        # 셀마다 노드가 흔들려서는 안 된다. eNB 해시로 고정한다.
        h = int(hashlib.sha256(enb.encode()).hexdigest()[:12], 16)
        pool = by_grp.get(str(grp)) or all_mme
        # **장비를 TAC 에서 유도한다**(2026-10-27). 전에는 eNB 해시였고
        # (`h % len(pool)`) 그래서 **인접 eNB 가 무관한 MME·SGW 를 받았다** —
        # 실측으로 경로 구간 73개 중 55개가 MME 재배치로 나왔고, 전환 종류를
        # 마스터에서 읽을 수 없어 `handover.kind_of` 가 쌍 해시를 쓰게 됐다.
        #
        # 실제 망은 **MME pool 이 TA 목록을 맡는다.** 그래서 TA 몇 개를 한
        # MME 가 보도록 묶는다 — `1` 로 두면 TA 경계마다 MME 가 바뀌어 전환이
        # 거의 다 `S1_MME` 가 된다. SGW·PGW 는 더 큰 단위다.
        #
        # **TAI List 가 기준이다**(`_TA_PER_*` 머리말). 목록이 없으면(좌표
        # 없음) 옛 경로인 eNB 해시로 떨어진다 — 위치를 모르는 셀의 장비
        # 배정을 지어낼 수 없다.
        #
        # `ta_list` 는 이 루프보다 **뒤에** 채워진다(`build_ta_lists` 가 셀을
        # 다 만든 뒤 돈다). 그래서 여기서는 쓸 수 없고, 아래에서 다시 배정한다.
        key = h
        mme = pool[key % len(pool)]
        pgw = pgws[h % len(pgws)]
        # 상용은 SPGW 합본 — 같은 풀을 쓴다
        sgw = pgws[(h // 7) % len(pgws)]
        ims = pgws[(h // 13) % len(pgws)]
        mapped = city is not None
        equip.append((str(sgw_raw) if sgw_raw else None,
                      str(mme_raw) if mme_raw else None,
                      str(pgw_raw) if pgw_raw else None))
        cells.append(Cell(
            cell_key=f"{enb}-{cellnum}",
            enb_id=enb,
            cell_id=cellnum,
            enb_seq=h % 1_000_000,
            c_uid=str(c_uid or ""),
            # **마스터가 들고 온 TA 다**(`CM_CELL_REP_RU_LOC*.TAC`).
            # 값은 **OpenCellID 도쿄 실데이터**다 —
            # `graphio-probe-rca/tools/load_opencellid_tokyo.py` 가 적재한다.
            # 합성 TA 를 만들던 코드는 걷어냈다(2026-10-27): 실제 TAC 가
            # 있으면 지어낼 이유가 없다.
            #
            # `0` 은 **"TA 모름"** 이다 — 컬럼이 비었거나 좌표가 없는 셀이다.
            # 그 셀에서는 TA 경계가 없는 것으로 읽혀 TAU 가 나지 않는다 —
            # 위치를 모르면 경계도 모르는 것이 맞다(불변규칙 12·17).
            tac=tac,
            # 전 셀이 같은 홈 PLMN 이다 — `CL_CELL_MAP` 에 그 컬럼이 없다.
            # 리터럴이 아니라 설정에서 온다(`plmn.Plmn` 머리말): 셀 PLMN 과
            # 가입자 IMSI 접두가 어긋나면 **전 가입자가 외국 가입자**가 된다.
            plmn=(plmn or Plmn()).home,
            vlan_id="",
            latitude=_dms(lat),
            longitude=_dms(lon),
            # 행정 코드는 상용 ID 를 그대로 쓴다. 이름은 CM_CITY/GU/DONG 의
            # 한글이 JSON 추출 단계에서 깨져 있어(??) 지어내지 않고 비워 둔다.
            adm_sido_cd=str(city or ""), adm_sido_nm="",
            adm_sgg_cd=str(gu or ""), adm_sgg_nm="",
            adm_emd_cd=str(dong or ""), adm_emd_nm="",
            geo_status="MAPPED" if mapped else "UNMAPPED",
            rat="EUTRAN",
            mme_id=mme, sgw_id=sgw, pgw_id=pgw, ims_pgw_id=ims,
            traffic_weight=_weight(enb, cellnum),
            display_name=f"eNB{enb}-{cellnum}",
        ))
    # ── TA 묶기 — **마스터에 TAC 가 없다** ──────────────────────────────
    #
    # 좌표가 없는 셀(`UNMAPPED`)은 묶을 수 없어 `tac=0` 으로 남는다. 그것도
    # 사실이다 — 위치를 모르는 셀의 TA 를 지어내지 않는다(불변규칙 12·17).
    # 그 셀에서는 TAU 판정이 서지 않고, `advance_routes` 가 `tac` 비교로
    # 결정하므로 **같은 0 끼리는 TA 경계가 없는 것으로 읽힌다** — 좌표를
    # 모르면 경계도 모르는 것이 맞다.
    # ── TAI List — **TA 가 바뀐다고 TAU 가 나는 것이 아니다** ──────────
    #
    # MME 가 UE 에게 TA **목록**을 주고 목록 밖으로 나갈 때만 TAU 가 난다.
    # 그 층이 없으면 도심에서 TAU 가 폭증한다 — 실측으로 신주쿠 2km 안에
    # TAC 가 49개이고 이웃 셀 쌍의 77% 가 TA 경계를 넘는다
    # (`handover.build_ta_lists` 머리말).
    #
    # **TA 는 마스터가 들고 온다**(`CM_CELL_REP_RU_LOC*.TAC`). 목록은 거기서
    # 지리로 유도한다 — 실제 TAC 번호가 지리를 느슨하게만 반영해 산술로는
    # 묶이지 않는다.
    n_list = HO.build_ta_lists([c for c in cells if c.geo_status == "MAPPED"])

    # ── 장비를 **TAI List 에서** 유도한다 ──────────────────────────────
    #
    # 셀을 만들 때는 할 수 없다 — 목록이 그때 아직 없다(위 `build_ta_lists`).
    # 그래서 여기서 다시 배정한다. 목록이 없는 셀(좌표 없음)은 **그대로
    # 둔다** — eNB 해시 배정이 남고, 위치를 모르는 셀의 장비를 지어내지
    # 않는다.
    #
    # 왜 목록 단위인가: 실제 망은 MME pool 이 TA 목록을 맡는다. eNB 해시로
    # 배정하면 **인접 eNB 가 무관한 MME 를 받아** 전환 종류를 마스터에서
    # 읽을 수 없다(`handover.kind_of` · docs/jp §1.6 의 실측).
    if n_list:
        for c in cells:
            if not c.ta_list:
                continue
            c.mme_id = all_mme[(c.ta_list // _TA_PER_MME) % len(all_mme)]
            c.sgw_id = pgws[(c.ta_list // _TA_PER_SGW) % len(pgws)]
            c.pgw_id = pgws[(c.ta_list // _TA_PER_PGW) % len(pgws)]
            c.ims_pgw_id = pgws[((c.ta_list // _TA_PER_PGW) + 1) % len(pgws)]

    # ── 마스터가 장비를 지정했으면 **그것이 이긴다** ────────────────────
    #
    # 위 유도는 마스터가 말하지 않을 때의 대안이다. `SGW_ID`·`MME_ID` 가
    # 채워져 있으면 그건 **지리 계층으로 묶은 결과**이고
    # (`graphio/tools/build_tokyo_core.py`: TAC n개 → SGW · SGW n개 → MME),
    # 산술 유도보다 사실에 가깝다.
    #
    # **양쪽이 같은 규칙을 각자 구현하지 않으려고 컬럼에 적는다** — 묶는
    # 쪽은 graphio, 읽는 쪽은 여기이고 서로 다른 저장소라 코드를 공유할 수
    # 없다. 각자 구현하면 한쪽만 고쳐질 때 에러 없이 대응이 갈린다.
    #
    # **PGW 도 마스터가 정한다**(2026-10-28 `PGW_ID` 컬럼). 전에는 위 산술
    # (`ta_list // _TA_PER_PGW`)뿐이라 범위를 좁히면 목록 번호가 앞쪽에만
    # 생겨 PGW 3702 차례가 오지 않았다. `ims_pgw_id` 는 마스터에 자리가
    # 없어 산술 그대로다.
    for c, (sg, mm, pg) in zip(cells, equip):
        if sg:
            c.sgw_id = sg
        if mm:
            c.mme_id = mm
        if pg:
            c.pgw_id = pg

    # ── 범위 — **맨 마지막이다** ──────────────────────────────────────
    #
    # 좌표가 있어야 반경을 잴 수 있고, 장비·TA 가 붙은 뒤여야 걸러낸 셀이
    # 완성된 모양이다.
    #
    # 노선이 없으면 구·동은 질의가 이미 걸렀다(`_scope_sql`). 노선이 있으면
    # 질의가 손대지 않았으므로 **여기서 합집합을 만든다.**
    if scope is not None and scope.route:
        keep = {c.cell_key for c in _scope_route(cells, scope)}
        if scope.gu:
            want = {str(int(x)) for x in scope.gu}
            keep |= {c.cell_key for c in cells if c.adm_sgg_cd in want}
        if scope.dong:
            want = {str(int(x)) for x in scope.dong}
            keep |= {c.cell_key for c in cells if c.adm_emd_cd in want}
        cells = [c for c in cells if c.cell_key in keep]
    return cells


# --------------------------------------------------------------------------
# 컬럼 매핑 — 위치로 맞춘다
# --------------------------------------------------------------------------


@dataclass
class XdrMapping:
    """C-Plane Parquet 적재 명세. **DB 를 보지 않는다**(2026-10-14).

    구 구현은 `information_schema` 에서 컬럼 순서·폭을 읽었다. 테이블을
    지우면 물어볼 곳이 없어지므로 정본이 `CPLANE_FIELDS` 로 옮겨 왔다 —
    `UplaneSpec` 이 같은 이유로 그렇게 되어 있다.

    이름(`XdrMapping`)과 필드 구성(`columns`·`widths`)은 그대로 둔다.
    `cmd_pemfill` 이 컬럼 수를 로그에 찍고 `PemdbWriter` 가 두 리스트를
    쓰므로, 이름을 바꾸면 전후 대조 때 "무엇이 바뀐 것인지" 가 흐려진다.
    """

    columns: list[str]              # EVENT_TIME 을 포함한 Parquet 컬럼 순서
    widths: list[int | None]        # varchar 최대 길이 (EVENT_TIME 은 None)
    fields: list[str]               # 규격 필드명 (widths 와 같이 EVENT_TIME 자리는 없다)


def xdr_mapping(conn=None, database: str | None = None) -> XdrMapping:
    """C-Plane Parquet 컬럼 순서·폭. **생성기 필드와 1:1 인지 검증한다.**

    인자 두 개는 **서명 호환으로 남긴다.** 호출부(`cmd_pemfill`·`cmd_run`)가
    커넥션을 들고 있고, 거기서 인자를 떼면 "DB 를 안 본다"는 변화가 호출부
    diff 에 묻힌다. 둘 다 쓰이지 않는다.

    위치로 맞추므로 **한 칸만 밀려도 153개 값이 전부 엉뚱한 컬럼에 들어가고
    에러가 나지 않는다.** 그래서 여기서 멈춘다 — 구 구현이 컬럼 수를 검증한
    것과 같은 취지이고, 지금은 **규격 필드명까지** 대조할 수 있다(그 대응이
    `CPLANE_FIELDS` 두 번째 자리에 적혀 있다).
    """
    fields = SERIES_FIELDS[CPLANE_SERIES]
    spec_fields = [f for _, f, _ in CPLANE_FIELDS]
    if len(spec_fields) != len(fields):
        raise RuntimeError(
            f"컬럼 수 불일치 — CPLANE_FIELDS {len(spec_fields)} vs 생성기 "
            f"{len(fields)}. 위치 매핑이 성립하지 않는다"
        )
    # 위치별 규격 필드명 대조. 생성기가 필드를 추가·삭제·재배치하면 여기서
    # 걸린다 — 통과하면 위치 대응이 성립한다는 뜻이다.
    if drift := [(i, a, b) for i, (a, b) in enumerate(zip(spec_fields, fields))
                 if a != b]:
        head = ", ".join(f"[{i}] {a} != {b}" for i, a, b in drift[:5])
        raise RuntimeError(
            f"C-Plane 위치 매핑이 어긋났다 ({len(drift)}자리): {head}. "
            "CPLANE_FIELDS 와 fields.SERIES_FIELDS['lte_call'] 중 "
            "무엇이 맞는지 사람이 판단해야 한다"
        )
    return XdrMapping(
        columns=[CPLANE_SLOT] + [c for c, _, _ in CPLANE_FIELDS],
        widths=[None] + [w for _, _, w in CPLANE_FIELDS],
        fields=list(spec_fields),
    )


@dataclass
class UplaneSpec:
    """계열 하나의 Parquet 적재 명세.

    구 `UplaneMapping` 의 후신이다. **테이블 이름 대신 계열 이름을 들고 있고,
    타입·폭을 `information_schema` 가 아니라 `UPLANE_FIELDS` 에서 받는다** —
    테이블을 지우면 DB 에 물어볼 곳이 없어지기 때문이다(2026-10-14).
    """

    series: str
    columns: list[str]              # UPLANE_SLOT 을 포함한 Parquet 컬럼 순서
    picks: list[int]                # 생성기 값 리스트에서 뽑을 위치 (SLOT 제외)
    kinds: list[str]                # picks 와 같은 길이 — DTYPES 의 키
    widths: list[int | None]        # picks 와 같은 길이 — str 컬럼의 문자 폭


def uplane_specs(series_on: Iterable[str] | None = None) -> dict[str, UplaneSpec]:
    """계열별 Parquet 적재 명세를 만든다.

    **생성기 필드와 이름으로 맞추고, 어긋나면 멈춘다.** 조용히 빠지면 그
    카운터가 영원히 NULL 이고, NULL 은 "모름"으로 읽혀 해당 KPI 가 산출 불가로
    내려간다 — 데이터는 들어오는데 지표만 안 서는 형태라 추적이 어렵다.

    ``series_on`` 은 켜 둔 계열(`PemdbGen.series`)이다. 주지 않으면 셋 다
    만든다. **구 `uplane_mapping` 과 달리 DB 를 보지 않는다** — 전에는 테이블
    존재 여부가 "그 계열이 살아 있는가"의 답이었고, 지금은 생성 설정
    (`pemdb1.rates`)이 그 답이다.
    """
    want = set(series_on) if series_on is not None else set(UPLANE_SERIES)
    out: dict[str, UplaneSpec] = {}
    for series in UPLANE_SERIES:
        if series not in want:
            continue
        spec = UPLANE_FIELDS[series]
        idx = {name: i for i, name in enumerate(SERIES_FIELDS[series])}
        if absent := [f for _, f, _, _ in spec if f not in idx]:
            raise RuntimeError(f"{series} 생성기에 없는 규격 필드: {absent}")
        if bad := [k for _, _, k, _ in spec if k not in raw_parquet.DTYPES]:
            raise RuntimeError(f"{series} UPLANE_FIELDS 의 모르는 타입: {bad}")
        out[series] = UplaneSpec(
            series=series,
            columns=[UPLANE_SLOT] + [c for c, _, _, _ in spec],
            picks=[idx[f] for _, f, _, _ in spec],
            kinds=[k for _, _, k, _ in spec],
            widths=[w for _, _, _, w in spec],
        )
    return out


# --------------------------------------------------------------------------
# 생성 — 기존 Generator 를 상용 분포로 덮어쓴다
# --------------------------------------------------------------------------


#: 계열별 **페이로드 바이트** 필드. 제어 화면 툴팁이 "그 분에 얼마나 흘렸나"를
#: 보여주는 데 쓴다.
#:
#: `PayloadSize` 만 센다 — `DataSize` 는 헤더를 포함하고 `RetransDataSize` 는
#: 재전송이라 **같은 바이트를 두 번 세게 된다.** 사용자가 보는 "사용량"은
#: 실제로 흘러간 페이로드다.
#:
#: **`Delta` 다. `Total` 이 아니다**(2026-10-14, 세션 모델). 세션이 분을 넘기면
#: `Total` 은 누적이라 Start·Interim·End 를 모두 더하면 같은 바이트를 여러 번
#: 센다 — 5분 세션 하나가 3배로 보고된다. 화면이 묻는 것은 "그 분에 얼마나
#: 흘렸나"이고 그게 정확히 `Delta` 다.
_BYTE_FIELDS = ("Traffic_Delta_Uplink_PayloadSize",
                "Traffic_Delta_Dnlink_PayloadSize")

#: 필드 인덱스 캐시. 분당 5만 행을 도는 자리라 매번 `.index()` 를 부르면
#: 그것만으로 틱이 눈에 띄게 느려진다.
_BYTE_IDX: dict[str, tuple[int, ...]] = {}


def series_stat(lte_rows: list, uplane_rows: dict[str, list]) -> dict:
    """계열별 ``{n, bytes}`` — `CM_DATAGEN_HISTORY.SERIES_STAT` 에 들어간다.

    C-Plane(`lte_call`)은 건수만 센다. 시그널링 기록이라 페이로드 바이트가
    없다 — 0 으로 적지 않고 **키 자체를 두지 않는다**(불변규칙 17: 모름을
    0으로 적지 않는다). 화면은 그 계열에 바이트를 표시하지 않는다.
    """
    out: dict[str, dict] = {"lte_call": {"n": len(lte_rows)}}
    for series, rows in (uplane_rows or {}).items():
        if series not in _BYTE_IDX:
            fields = SERIES_FIELDS.get(series) or []
            _BYTE_IDX[series] = tuple(
                fields.index(f) for f in _BYTE_FIELDS if f in fields)
        idx = _BYTE_IDX[series]
        total = 0
        for r in rows:
            for i in idx:
                v = r[i]
                if v:
                    try:
                        total += int(float(v))
                    except (TypeError, ValueError):
                        pass        # 생성값이 비정상이면 그 행만 건너뛴다
        out[series] = {"n": len(rows), "bytes": total}
    return out


@dataclass
class Scope:
    """**생성 범위** — 분당 호를 어느 지역에 흘리나.

    ## 왜 필요한가

    마스터는 넓고(도쿄 셀 17,188) 분당 호는 적다(2,000). 그대로 흘리면
    셀당 **0.12호/분**이고, 최소 표본 20(불변규칙 5)을 30분 창에서도 못
    채워 **셀 축 탐지가 한 번도 서지 않는다** — 에러가 아니라 지도가 전부
    "판정 불가" 회색이 된다.

    호를 11배로 늘리면 전 셀이 판정되지만 원시 Parquet 도 11배다. 지금
    장비에서는 그렇게 하지 않는다(사람 결정 2026-10-27) — 대신 **좁은
    지역에만** 흘린다.

        구 하나(신주쿠)      eNB 311 · 셀 1,666  →  1.2호/분 · 15분 창
        동 하나(신주쿠 11)   eNB 133 · 셀   764  →  2.6호/분 ·  5분 창
        노선 하나            eNB  88 · 셀   540  →  3.7호/분 ·  5분 창

    ## 합집합이다

    `gu` · `dong` · `route` 를 함께 주면 **합친다**. "이 구 + 이 노선"이
    자연스러운 요구이고, 교집합은 빈 결과가 되기 쉽다.

    **다 비면 제한이 없다** — 마스터 전량이다. 그게 기본이고, 좁히는 것이
    사람의 결정이다.
    """

    #: `CM_GU_JP.GU_ID` 목록 (예: `11200` 신주쿠구).
    gu: list = field(default_factory=list)
    #: `CM_DONG_JP.DONG_ID` 목록 (예: `11200011`).
    dong: list = field(default_factory=list)
    #: 노선 id (`data/lines/*.json` 의 `id`). 그 노선이 **쓰는 셀**이 들어온다.
    route: str = ""
    #: 노선에서 이 반경(m) 안의 셀까지 넓힌다. `0` 이면 노선 셀만.
    #:
    #: 노선 셀만 쓰면 **이동 가입자밖에 없다** — 역 주변에 머무는 사람이
    #: 없어 역세권 혼잡(`j-sc-14`)이 생기지 않는다. 반경을 주면 그 주변
    #: 고정 가입자가 함께 들어온다.
    route_radius_m: float = 0.0
    #: 노선 파일이 있는 디렉터리.
    lines_dir: str = "data/lines"

    @classmethod
    def from_cfg(cls, cfg: dict | None) -> "Scope":
        if not cfg:
            return cls()
        known = {f.name for f in fields(cls)}
        bad = sorted(set(cfg) - known)
        if bad:
            raise RuntimeError(
                f"scope 설정에 모르는 키: {bad} (아는 키: {sorted(known)})")
        return cls(**cfg)

    def empty(self) -> bool:
        return not (self.gu or self.dong or self.route)


@dataclass
class Mobility:
    """이동 경로 설정 — **기본값이 "아무도 움직이지 않음"이다.**

    `Shape`·`Baseline` 과 같은 모양이고 **모르는 키는 멈춘다**(`from_cfg`).
    역호환을 끄기로 두는 이유: 경로를 깔면 전환 수와 셀 쌍 분포가 바뀌므로
    기존 기준선과 전후 비교가 안 된다. **켜는 것이 사람의 결정**이어야 한다.
    """

    #: 경로 정의 파일(`routes.json`). 비우면 `build_routes` 로 자동 생성한다.
    #: **실제 노선을 쓰려면 이 파일을 적는다** — 형식은 `handover.load_routes`.
    routes_file: str = ""
    #: 자동 생성할 경로 수와 칸 수. `routes_file` 이 있으면 무시된다
    count: int = 0
    length: int = 8
    #: 칸당 체류시간 범위(초). 세션 평균 수명(3분)보다 짧아야 **한 세션이
    #: 여러 칸을 겪는다** — 그게 "이동 중 전환"이다
    dwell_min: int = 45
    dwell_max: int = 150
    #: 순환선 비율. 순환선은 끝에서 돌고, 아니면 방향을 뒤집는다(상·하행)
    loop_share: float = 0.25
    #: 경로에 **태울 가입자** 비율
    sub_share: float = 0.0
    #: 이동 가입자를 **고를** 비율. `sub_share` 와 다른 축이다 — 움직이는
    #: 사람이 전체의 12% 라도 그들이 더 자주 통신하면 호 비중은 더 높다
    call_share: float = 0.0
    #: 여정 — `[{route, stations: [최소, 최대], share}]`(임의 역에서 n 역)
    #: 또는 `[{route, from, to, share}]`(고정 구간). 고정 구간은 전원이 같은
    #: 칸에서 출발한다 — 시연용 데이터에는 앞의 것을 쓴다.
    #: 비면 모두 끝없이 순환한다(노선 전체를 도는 사람).
    journeys: list = field(default_factory=list)
    #: 하차 후 머무는 시간(초). 그 동안 셀이 고정이고 전환이 없다 —
    #: 그게 역 주변 체류이고 역세권 혼잡이 거기서 생긴다
    off_sec_min: int = 300
    off_sec_max: int = 1800
    #: **그냥 이동**(도보·버스·차) 그룹에 넣을 가입자 비율 — 노선에 타지
    #: 않은 사람 중에서. 이웃 eNB 로 `walk_sec` 마다 옮겨 간다
    walk_share: float = 0.0
    walk_sec_min: int = 180
    walk_sec_max: int = 600
    #: **시간대별 호 그룹 비율** — `[[시작시, 끝시, 고정%, 이동%, 지하철%], …]`
    #: (`generator.Groups`). 지하철은 이동에 포함된다. 비면 종전대로
    #: `call_share` 하나로 노선 탑승자를 고른다. 있으면 `call_share` 는 안 쓴다
    hourly: list = field(default_factory=list)
    #: 지하철 운행 시각 `[시작, 끝)`. 밖에서는 지하철 몫이 0 이고 열차가 선다
    subway_hours: list = field(default_factory=lambda: [6, 24])
    #: 시간대 경계를 넘어가는 시간(분) — 경계 앞뒤 절반씩
    blend_min: int = 60
    #: **노선을 타지 않는 가입자**를 거주지 근처에서 뽑나. 기본 끄기.
    #:
    #: 경로와 **다른 축이다.** 경로는 "이 사람이 이 구간을 다닌다"이고 이쪽은
    #: "이 사람이 여기 산다"다. 샘플 XDR 이 후자를 규정하지 않으므로
    #: (`generator._lte_call` 주석의 실측) 기본은 끄기이고, 켜면 셀·분
    #: `SUBS` 가 -22% 움직여 기존 기준선과 전후 비교가 끊긴다.
    home_locality: bool = False
    #: 거주지로 볼 주변 반경(m). `home_locality` 가 켜졌을 때만 쓴다
    home_radius_m: float = 10_000.0

    @classmethod
    def from_cfg(cls, cfg: dict | None) -> "Mobility":
        if not cfg:
            return cls()
        known = {f.name for f in fields(cls)}
        bad = sorted(set(cfg) - known)
        if bad:
            raise RuntimeError(
                f"mobility 설정에 모르는 키: {bad} (아는 키: {sorted(known)})")
        return cls(**cfg)


class PemdbGen:
    """상용 스키마용 생성기 — 셀 마스터는 DB, 분포는 실측 JSON."""

    def __init__(self, conn, data_dir: Path, rates: dict, seed: int = 20260903,
                 subscriber_pool: int = 20_000, success_only: bool = True,
                 enb_limit: int | None = None,
                 drawable: set[str] | None = None,
                 #: 셀 좌표 결손을 생성 시점에 채운다. **`cmd_run`·`cmd_pemfill`
                 #: 만 켠다** — `cmd_capacity` 는 읽기뿐이다(그 docstring 의 보증).
                 ensure_coords: bool = False,
                 #: 홈 PLMN · IMSI 접두(`plmn.Plmn`). `None` 이면 한국 기본값.
                 plmn: Plmn | None = None,
                 call_type_floor: float | None = None,
                 uplane_session: dict | None = None,
                 baseline: dict | None = None,
                 fault: dict | None = None,
                 mobility: dict | None = None,
                 #: 셀 **좌표·지역 벌**을 어느 나라로 읽나(`topo.table_name`).
                 #: 기본 `kr` 이 의도다 — 바꾸는 것은 사람의 결정이고, 바꾸면
                 #: 셀 세트가 통째로 갈려 기존 기준선과 비교가 끊긴다.
                 locale: str = "kr",
                 #: `TAC` 가 있는 셀만 후보로 쓴다.
                 #:
                 #: **더는 세트를 좁히는 수단이 아니다**(2026-10-27) — 실데이터
                 #: 마스터는 전 행에 TAC 가 있다. 남는 쓸모는 "TA 를 모르는
                 #: 셀을 뺀다"이고, 그건 좌표가 없는 셀을 빼는 것과 같다.
                 tac_only: bool = False,
                 #: eNB 당 쓸 섹터 수 상한. 도쿄 JP 벌은 eNB 당 섹터가 중앙
                 #: 26개라 그대로 쓰면 셀당 호 수가 최소 표본을 못 채운다.
                 sectors_per_enb: int | None = None,
                 #: **생성 범위** — 분당 호를 어느 지역에 흘리나(`Scope`).
                 #: 비면 마스터 전량이다.
                 scope: dict | None = None,
                 #: `j-sc-24` 부하 이동 — A eNB 와 소등 eNB 목록
                 #: (`scenarios.ShiftCfg`). 비면 소등 eNB 가 없다.
                 capacity_shift: dict | None = None):
        self.plmn = plmn or Plmn()
        cells = load_cells(conn, enb_limit=enb_limit, drawable=drawable,
                           ensure_coords=ensure_coords, plmn=self.plmn,
                           locale=locale, tac_only=tac_only,
                           sectors_per_enb=sectors_per_enb,
                           scope=Scope.from_cfg(scope))
        # ── 소등 eNB 를 **셀 목록에서 뺀다** (`j-sc-24` · 2026-10-29) ────────
        #
        # 마스터 전량을 생성하므로(`enb_limit: 0`) 그대로 두면 B 에도 평소
        # 호가 생긴다. 추첨에서만 빼면 부족하다 — 핸드오버 이웃
        # (`HO.Topology`), 다른 장애의 대상 선정(`busy_cluster`·`area_cells`
        # ·`_busiest`), 지진의 이동처가 전부 `self.cells` 에서 고른다. 목록에
        # 아예 없어야 그 길로 새지 않는다. 시나리오가 켰을 때만
        # `_shifter` 가 여기서 꺼내 호를 붙인다.
        shift = set_shift_cfg(capacity_shift)
        off = set(shift.standby_enbs)
        #: 소등 eNB → 그 셀들. **생성 대상이 아니다** — `_shifter` 만 쓴다
        self.standby: dict[str, list[Cell]] = {e: [] for e in shift.standby_enbs}
        self.cells = []
        for c in cells:
            (self.standby[c.enb_id] if c.enb_id in off else self.cells).append(c)
        missing = [e for e, v in self.standby.items() if not v]
        if missing:
            # 멈추지 않는다 — 로케일을 바꾸면(kr) 일본 eNB 가 없는 것이 맞고,
            # 그때는 24번만 해석 실패로 취소된다. 대신 크게 알린다.
            print(f"  ! 소등 eNB 가 셀 마스터(생성 범위)에 없다 — {missing}",
                  flush=True)
        if not self.cells:
            raise RuntimeError("CL_CELL_MAP 이 비어 있다 — 마스터를 먼저 적재하라")
        #: 평시 배경 모수(`pemdb1.baseline`). **장애 mutator 와 무관하다** —
        #: 분모를 만드는 자리이고 실패는 시나리오가 꽂는다.
        self.baseline = Baseline.from_cfg(baseline)
        #: 장애 공통 설정(`pemdb1.fault`) — TIMEOUT 대기시간. mutator 가
        #: 모듈 전역(`scenarios.FAULT`)으로 읽는다(서명에 설정을 받을 자리가 없다).
        self.fault = set_fault_cfg(fault)
        self.dist = Distributions(
            data_dir / "pemdb1" / "_distributions.json",
            random.Random(seed + 101), success_only=success_only,
            call_type_floor=call_type_floor, baseline=self.baseline,
            # APN 은 **셀 마스터와 같은 나라축**을 따른다(`locale` — 셀 좌표·
            # 지역 벌을 고르는 그 값). `plmn` 을 따르지 않는 이유: plmn 은
            # 일부러 KR 로 남아 있고(`datagen.yaml` 의 plmn 블록 주석), 그
            # 결정과 APN 을 묶으면 APN 만 바꾸려 해도 IMSI 가 함께 바뀐다.
            apn_map=apn_map(locale),
        )
        # 도메인 표본도 **같은 나라축**이다 — 위 `apn_map` 과 같은 `locale`
        profiles = load_profiles(data_dir / "profile.json", locale=locale)
        # **`lte_call` 이 반드시 첫 자리여야 한다.** `Generator.__init__` 이
        # `enumerate(profiles.items())` 로 계열별 샘플러 시드를 `seed + i` 로
        # 준다. 순서가 바뀌면 lte_call 의 시드가 달라져 **같은 seed 로 돌려도
        # C-Plane 데이터가 재현되지 않는다** — 에러 없이 과거 결과와 어긋난다.
        series_rates = {"lte_call": int(rates.get("lte_call", 2000))}
        for s in UPLANE_SERIES:
            # 비율 0 이면 그 계열을 아예 만들지 않는다. 빈 계열을 넣어 두면
            # 집계가 0행을 "값이 0" 으로 쓸 자리가 생긴다(불변규칙 17).
            if int(rates.get(s, 0)) > 0:
                series_rates[s] = int(rates[s])
        self.series: tuple[str, ...] = tuple(series_rates)
        self.gen = Generator(
            cells=self.cells,
            profiles={s: profiles[s] for s in series_rates},
            scenarios=[], rates=series_rates,
            seed=seed, diurnal=True, subscriber_pool=subscriber_pool,
            success_only=success_only,
            # 상용 적재는 `ONGOINGFLAG='2'`(호 종료)만 담는다. 생성기가
            # 프로파일에서 뽑으면 '2' 가 40% 뿐이라 **종료 레코드에만 채우는
            # 필드가 60% 에서 빈다** — `_coerce` 가 플래그는 덮지만
            # `initial_*_duration`·`initial_nr_conn_time` 은 못 덮는다.
            end_only=True,
            plmn=self.plmn,
        )
        self._reseat_subscribers(random.Random(seed + 202))
        # 평시 PathSwitch 는 `_coerce` 가 셀 마스터를 보고 심는다. 생성기 쪽에도
        # 걸어 두는 이유는 구 경로(`generate_minute`)가 `_coerce` 를 타지 않아
        # **평시 배경이 없다**는 것을 코드에서 읽히게 하려는 것이다.
        self.gen.baseline = self.baseline
        #: 전환 토폴로지 — `_baseline_handover` 가 쓴다. **U-Plane 과 같은
        #: 모듈이다**(`handover.Topology`). 양쪽이 각자 이웃을 고르면 같은
        #: 셀 쌍에서 C-Plane 은 전환했는데 U-Plane 은 안 한 호가 생기고,
        #: 그러면 "핸드오버 직후 정체"를 대조할 자료가 안 된다.
        #:
        #: **`self.topo` 가 아니다.** `datagen.topo`(밴드·테이블 토폴로지)라는
        #: 별 모듈이 이미 있고 `test_topo` 가 `topo.<이름>(` 호출을 스캔해
        #: 정의 존재를 검사한다 — 같은 이름을 쓰면 그 검사가 **엉뚱한 모듈에서**
        #: 함수를 찾는다 — 실제로 `test_topo` 가 "pick_kind 가 없다" 로
        #: 걸렸다. 그 검사는 **주석까지 스캔**하므로 여기에도 그 이름을 점
        #: 찍어 적지 않는다.
        self.ho_topo = HO.Topology(self.cells)
        # **생성기도 같은 토폴로지를 본다.** `_sub_for` 가 "이 셀 주변에 사는
        # 사람"을 물으므로, 두 벌을 만들면 전환 상대와 거주 분포가 서로 다른
        # 관계를 보게 된다 — "전환은 되는데 그 셀에 사는 사람이 없다".
        self.gen.ho_topo = self.ho_topo
        self._seat_mobility(Mobility.from_cfg(mobility), seed)
        # **시나리오의 APN 대상이 레코드에 실제로 있는 값이어야 한다.**
        # `_coerce` 가 `Distributions.apn()` 으로 APN 을 덮으므로 프로파일에서
        # 고르면 그 APN 이 한 건도 없을 수 있고, 그러면 SC-19·SC-11 이 주입돼도
        # 아무 일이 일어나지 않는다 — 에러 없이 조용히.
        #
        # **`_bind_scenario_targets` 를 여기서 다시 부르지 않는다** — 이 시점에
        # `gen.scenarios` 는 비어 있고, 시나리오가 걸릴 때 `set_faults` 가
        # 부른다. 그때 이 풀이 이미 서 있어야 한다는 것이 순서의 전부다.
        self.gen.apn_pool = [
            a for a, _ in sorted(zip(self.dist.apns, self.dist.apn_w),
                                 key=lambda kv: -kv[1])
        ]
        # 앱 사전 — 세션이 **이미 매핑된 앱**으로 나간다(`apps` 머리말).
        # 시나리오 대상(21·22번)이 이 사전에서 앱을 고르므로 같은 이유로
        # `set_faults` 전에 서 있어야 한다
        self.gen.app_book = load_apps(conn)
        #: U-Plane 세션 엔진 — **C-Plane 과 U-Plane 을 잇는 자리**(2026-10-14).
        #: `rates` 의 U-Plane 세 값이 여기서 파생 계수로 환산된다
        #: (`session` 머리말 §1.4). 활성 세션을 분 사이에 들고 있으므로
        #: **생성기 인스턴스 하나가 계속 살아 있어야 한다** — 틱마다 새로
        #: 만들면 세션이 매번 끊기고 종료 레코드가 나오지 않는다.
        self.sessions = SessionEngine(
            self.gen, series_rates, Shape.from_cfg(uplane_session), seed=seed)
        #: **100% 기준 요율.** `set_volume_pct` 가 여기서 곱해 쓴다 — 현재 값
        #: (`self.gen.rates`)에서 거꾸로 계산하면 반올림 오차가 누적된다
        #: (70% → 50% 를 두 번 거치면 35% 가 아니라 34.x% 가 된다).
        self._base_rates: dict[str, int] = dict(series_rates)
        self._volume_pct: float = 100.0
        #: 계열 차단 (2026-10-15). **생성 자체를 막는다** — 요율 0 으로 눌러도
        #: 되지만 그러면 `set_volume_pct` 가 기준값을 곱해 되살린다.
        #: "원래 안 쓰는 계열"(요율 0)과 "일부러 끊은 계열"은 다른 상태다.
        self._blocked: frozenset[str] = frozenset()
        #: 재배정 전 원본 노드 배치. `reassign_nodes` 가 처음 불릴 때 뜬다 —
        #: 되돌리려면 원본이 있어야 하고, 누적 적용은 되돌릴 수 없다.
        self._nodes0: dict[str, tuple] | None = None
        self._reassign: dict = {}
        #: `records()` 가 담고 `uplane_records()` 가 꺼내는 그 분의 성공 호.
        #: 두 호출 사이에 끼어 있는 이유는 **U-Plane 이 C-Plane 레코드에서
        #: 나와야** 하기 때문이다 — 분이 어긋나면 멈춘다(`uplane_records`).
        self._calls: list[Call] = []
        self._calls_at: datetime | None = None
        self.anchor: datetime | None = None
        self.seed = seed
        #: fault_id → (판별 키, Scenario). 같은 fault 가 틱마다 다시 와도 같은
        #: Scenario 를 재사용해야 generator 의 도메인·기종 바인딩이 다시 구르지
        #: 않는다 (`faults.resolve_active` 가 읽고 쓴다).
        self._fault_cache: dict[int, tuple[tuple, Scenario]] = {}

    def _reseat_subscribers(self, rng: random.Random) -> None:
        """가입자 풀의 Vendor/Model·PLMN 을 상용 실측 분포로 바꾼다.

        레코드 단위로 덮으면 같은 IMSI 가 분마다 다른 기종으로 보여 UE 축
        집계(R-10, 기종 쏠림)가 성립하지 않는다. 가입자에 고정해야 한다.
        """
        dist = self.dist
        for i, sub in enumerate(self.gen.subs):
            sub.vendor, sub.model = dist.vendor_model()
            # 인바운드 로머 — APN 분포에 mcc234 가 2.6% 있다 (sg1.super...).
            # **둘 다 설정에서 온다**(`plmn.Plmn`). 홈 쪽을 리터럴로 두면
            # 셀 PLMN 과 어긋나 전 가입자가 외국 가입자가 된다.
            sub.mcc_mnc = (self.plmn.roaming if rng.random() < 0.026
                           else self.plmn.imsi_prefix)
            # **식별자를 다시 만든다.** `mcc_mnc` 와 기종을 여기서 덮으므로,
            # `_make_subscribers` 가 만든 IMSI·IMEI 는 이미 어긋났다. IMSI 는
            # 규격상 `imsi_mcc_mnc_info` 로 시작해야 하고(앞 7자), IMEI 의 TAC 는
            # 기종과 묶여야 한다(R-10). 다시 만들지 않으면 둘 다 조용히 깨진다.
            # MDN 은 두 값에 의존하지 않으므로 그대로 둔다.
            sub.imsi = synth_imsi(sub.mcc_mnc, i)
            sub.imei = synth_imei(sub.vendor, sub.model, i)
        # **IMSI 역색인을 다시 만든다**(2026-10-29). `Generator.__init__` 이
        # 바꾸기 전 IMSI 로 만들어 두었고, 위에서 IMSI 를 전부 새로 썼다.
        # 그대로 두면 가입자 2,000명 중 285명(14%)만 찾혀 — **에러 없이** —
        # "같은 사람을 다른 셀로"(`j-sc-27` 재접속 · `j-sc-24` 부하 이동)가
        # 대부분 일어나지 않고, U-Plane 세션이 이동 가입자를 되찾지 못한다
        # (`session` 의 `sub_i`).
        self.gen.sub_by_imsi = {s.imsi: i for i, s in enumerate(self.gen.subs)}

    # -- 분 단위 ---------------------------------------------------------

    def minute_of(self, ts: datetime) -> int:
        if self.anchor is None:
            raise RuntimeError("anchor 미설정")
        return int((ts - self.anchor).total_seconds() // 60)

    def _active(self, minute: int) -> list[Scenario]:
        """이 분에 걸리는 시나리오 + 대상 셀 캐시."""
        active = [sc for sc in self.gen.scenarios if sc.covers(minute)]
        for sc in active:
            sc.active_cells_cache = sc.active_cells(minute)  # type: ignore[attr-defined]
            # booster·shifter 는 서명에 분이 없다 — 국면을 보려면 여기서
            # 세워 둔다(`j-sc-24` 의 `shift_*`)
            sc._minute = minute
        return active

    def _mutator(self, minute: int, active: list[Scenario]):
        """계열·셀 자격을 판정해 뮤테이터를 거는 콜백.

        **시나리오 변형은 계열을 가린다.** `sc.series` 에 그 계열이 없으면
        건너뛴다 — `SC-05`(DNS)·`SC-06`(HTTP)·`SC-07`(Transport)이 각각 자기
        계열만 건드리도록 이미 선언돼 있고, 전 계열에 걸면 한 장애가 세 축을
        동시에 흔들어 원인 판정이 섞인다.

        **강도는 `intensity_for(series, …)` 로 받는다**(2026-10-22). 계열별
        시작 오프셋이 있는 시나리오(SC-21: transport 0분 → dns 2분)가 있고,
        오프셋이 없으면 `intensity()` 와 같은 값이다.
        """
        sel = [sc for sc in active if sc.kind in MUTATORS]
        if not sel:
            return None
        rng = self.gen.rng

        def mutate(series: str, rec, cell) -> None:
            for sc in sel:
                if series not in sc.series:
                    continue
                if sc.target.cell_keys and cell.cell_key not in sc.active_cells_cache:
                    continue
                sc._minute = minute     # SC-18 이 "5분 전 강도"를 본다
                MUTATORS[sc.kind](rec, sc, sc.intensity_for(series, minute),
                                  rng, cell)

        return mutate

    def _plan_mutator(self, minute: int, active: list[Scenario]):
        """U-Plane **세션 총량**에 거는 콜백 (`scenarios.PLAN_MUTATORS`).

        양(量) 필드를 분 레코드에서 고치면 `Traffic_Total_*` 의 누적이
        역전한다 — `session._plan` 단계에서만 바꾼다(V3 규칙 U-1·U-2).
        세션은 transport 계열이므로 자격 판정도 그 계열로 한다.
        """
        sel = [sc for sc in active
               if sc.kind in PLAN_MUTATORS and "transport" in sc.series]
        if not sel:
            return None
        rng = self.gen.rng

        def plan_mutate(cell, plan) -> None:
            for sc in sel:
                if sc.target.cell_keys and cell.cell_key not in sc.active_cells_cache:
                    continue
                sc._minute = minute
                PLAN_MUTATORS[sc.kind](
                    plan, sc, sc.intensity_for("transport", minute), rng, cell)

        return plan_mutate

    def _suppressor(self, minute: int, active: list[Scenario]):
        """호 **생성을 막는** 콜백 (`scenarios.SUPPRESSORS`) — SC-17 무호.

        mutator 로는 표현할 수 없다. 그 서명은 이미 만들어진 레코드를 받으므로
        "레코드를 안 만드는 것"을 적을 자리가 없고, `set_volume_pct` 는
        전역이라 셀·노드 단위가 아니다.
        """
        sel = [sc for sc in active if sc.kind in SUPPRESSORS]
        if not sel:
            return None
        rng = self.gen.rng

        def suppress(cell) -> bool:
            return any(SUPPRESSORS[sc.kind](sc, cell, sc.intensity(minute), rng)
                       for sc in sel)

        return suppress

    def _boost(self, minute: int, active: list[Scenario]) -> dict[str, float] | None:
        """셀 키 → 가중 배율 (`scenarios.BOOSTERS`) — `j-sc-14` 활성 가입자 증가.

        suppressor 의 대칭이다. 둘 다 "어느 셀에 호가 생기나"를 바꾸므로
        mutator(레코드를 고친다)로는 쓸 수 없다. 여러 장애가 같은 셀에 걸리면
        곱한다.
        """
        out: dict[str, float] = {}
        for sc in active:
            fn = BOOSTERS.get(sc.kind)
            if fn is None or not sc.target.cell_keys:
                continue
            f = fn(sc, sc.intensity(minute))
            for k in sc.active_cells_cache:
                out[k] = out.get(k, 1.0) * f
        return out or None

    def _relocator(self, minute: int, active: list[Scenario]):
        """정지 셀 → 이웃 셀 콜백 (`scenarios.RELOCATORS`) — `j-sc-27`.

        호(`records`)와 이어지는 세션(`SessionEngine.relocate`)이 **같은
        함수**를 쓴다. 따로 두면 새 호는 옮겨 가는데 열려 있던 세션은 죽은
        셀에서 계속 흘러, 정지 셀에 U-Plane 만 남는다.
        """
        sel = [sc for sc in active if sc.kind in RELOCATORS and sc.target.cell_keys]
        # **꺼진 소등 eNB 에 남은 세션도 떠난다**(`j-sc-24`). B 를 끄면 새 호는
        # 즉시 0 이지만 B 에서 열린 세션은 몇 분 더 흐른다 — 꺼진 기지국에
        # 사용자 트래픽이 남는다. 지금 켜져 있는 B 는 빼고, 나머지 소등 셀은
        # A(`from_enb`)로 보낸다. A 가 생성 범위에 없으면 아무 데로도 보내지 않는다.
        lit = {sc.target.shift_to for sc in active
               if sc.kind == "capacity_shift" and shift_split(sc) > 0}
        dark = {c.cell_key for e, v in self.standby.items() if e not in lit for c in v}
        home = [c for c in self.cells if c.enb_id == _shift_cfg().from_enb] if dark else []
        if not sel and not (dark and home):
            return None
        by_key = self.gen.cell_by_key
        pools = {sc.id: [by_key[k] for k in sc.target.cell_keys if k in by_key]
                 for sc in sel}

        def relocate(cell):
            if home and cell.cell_key in dark:
                return home[zlib.crc32(cell.cell_key.encode()) % len(home)]
            for sc in sel:
                if cell.cell_key not in sc.active_cells_cache:
                    continue
                dst = RELOCATORS[sc.kind](sc, cell, sc.intensity(minute),
                                          pools[sc.id])
                if dst is not None:
                    return dst
            return None

        return relocate

    def _floor_drafts(self, minute: int, active: list[Scenario], landed,
                      suppress, ts, slot_start):
        """장애 대상 셀마다 **판정 하한까지 모자란 호**를 낸다.

        대상이 `floor_max_cells` 를 넘으면 걸지 않는다(장비 전체 대상).
        억제된 셀(무호 장애)에는 채우지 않는다 — 꺼진 셀에 호를 만들면 장애가
        지워진다. 가입자는 고정 그룹에서 뽑는다 — 이동 그룹에서 뽑으면 그
        사람의 궤적 밖 셀에 호가 생긴다.
        """
        gen, cfg = self.gen, self.fault
        by_key = gen.cell_by_key
        pool = gen._fixed or range(len(gen.subs))
        done: set[str] = set()
        for sc in active:
            keys = sc.target.cell_keys
            if not keys or len(keys) > cfg.floor_max_cells:
                continue
            want = cfg.floor_per_cell * visible_level(sc, minute)
            for k in sorted(sc.active_cells_cache):
                cell = by_key.get(k)
                if cell is None or k in done:
                    continue
                done.add(k)                     # 두 장애가 같은 셀이면 한 번만
                if suppress is not None and suppress(cell):
                    continue
                need = int(want + gen.rng.random()) - landed[k]
                for _ in range(max(0, need)):
                    sub = gen.subs[pool[gen.rng.randrange(len(pool))]]
                    yield gen.base_lte_call(ts, slot_start, at=(sub, cell))

    def _shifter(self, minute: int, active: list[Scenario]):
        """A 셀로 뽑힌 호 → 소등 eNB(B) 셀 콜백 — `j-sc-24` 부하 이동.

        `_relocator` 와 비슷하지만 **세션에는 걸지 않는다.** 그쪽은 "셀이
        죽었다"를 뜻해 열려 있던 세션까지 옮기고 분마다 다시 묻는다 — 살아
        있는 A 에 그 콜백을 쓰면 확률이 분마다 겹쳐 A 의 세션이 거의 다 B 로
        간다. 새 호만 나누면 그 호에서 파생되는 새 세션이 B 에서 열리고,
        A 에 열려 있던 세션은 자연히 끝난다.
        """
        sel = [sc for sc in active
               if sc.kind == "capacity_shift" and sc.target.shift_to
               and self.standby.get(sc.target.shift_to) and shift_split(sc) > 0]
        if not sel:
            return None
        rng = self.gen.rng

        def shift(cell):
            for sc in sel:
                if cell.cell_key in sc.active_cells_cache and rng.random() < shift_split(sc):
                    pool = self.standby[sc.target.shift_to]
                    return pool[rng.randrange(len(pool))]
            return None

        return shift

    def reassign_nodes(self, spec: dict | None) -> dict:
        """장비 재배정 — ``{"SGW": {"0015": "0103"}, ...}``. 적용 결과를 돌려준다.

        ## 총량은 그대로고 쏠림만 만든다

        호 수를 건드리지 않는다. 셀이 어느 노드를 거쳤다고 **기록하는지**만
        바꾸므로 분당 생성량은 유지되고, 비운 노드의 구간이 0 이 되면서 받은
        노드로 몰린다 — "장애는 아닌데 한쪽으로 몰린 것처럼" 이 그 모양이다
        (2026-10-15 사람 요청).

        **장비를 지우거나 만들지 않는다.** UI 가 "장비 추가·삭제는 미지원 —
        셀 재배정 설계가 선행되어야 한다"고 적어 둔 것은 **토폴로지 변경**
        이야기이고, 이미 있는 장비 사이에서 셀을 옮기는 것은 그 설계가 필요
        없다. 레코드 생성이 매번 `cell.sgw_id` 를 다시 읽기 때문이다
        (`generator._c_plane`·`session._emit`).

        ## 마스터(`CL_CELL_MAP`)는 건드리지 않는다

        메모리상 셀만 바꾼다. 그래서 **구성정보는 A 인데 관측은 B** 가 되는데,
        graphio 가 경로를 구성이 아니라 **관측된 호**에서 얻으므로
        (`PM_LINK_CELL_NODE_1M`) 화면은 재배정된 대로 그린다. 마스터를 고치면
        되돌릴 수 없고, PoC 에서 원하는 것은 일시적 쏠림이다.

        ``spec`` 이 비면 **원래 배치로 되돌린다** — 기동 시 스냅숏을 떠 둔다.
        """
        if self._nodes0 is None:                    # 최초 1회 원본 보관
            self._nodes0 = {c.cell_key: (c.mme_id, c.sgw_id, c.pgw_id)
                            for c in self.cells}
        # 항상 원본에서 다시 적용한다 — 누적하면 A→B→C 가 되어 되돌릴 수 없고,
        # 같은 spec 을 두 번 주면 결과가 달라진다.
        for c in self.cells:
            c.mme_id, c.sgw_id, c.pgw_id = self._nodes0[c.cell_key]
        moved: dict[str, int] = {}
        for kind, pairs in (spec or {}).items():
            attr = {"MME": "mme_id", "SGW": "sgw_id", "PGW": "pgw_id"}.get(
                str(kind).upper())
            if not attr:
                continue
            for src, dst in (pairs or {}).items():
                n = 0
                for c in self.cells:
                    # **문자열로 비교한다.** 노드 ID 는 `'0015'` 꼴 0채움이고
                    # 숫자로 접으면 `'0015'` 와 `'15'` 가 섞인다.
                    if str(getattr(c, attr)) == str(src):
                        setattr(c, attr, type(getattr(c, attr))(dst))
                        n += 1
                if n:
                    moved[f"{kind}:{src}→{dst}"] = n
        self._reassign = dict(spec or {})
        return moved

    def reassign_spec(self) -> dict:
        """지금 적용된 재배정. UI·로그가 인용한다."""
        return dict(self._reassign)

    def set_volume_pct(self, pct: float) -> float:
        """생성량을 기준의 ``pct`` %로 맞춘다. 적용된 값을 돌려준다.

        ## 왜 요율 dict 수정만으로는 부족한가

        `Generator._volume()` 은 매 분 `self.rates` 를 다시 읽으므로 C-Plane
        호 수는 dict 를 제자리에서 고치면 바로 따라온다. **그런데 U-Plane 은
        줄지 않는다** — `SessionEngine.session_lambda()` 가
        ``sessions_peak / n_calls`` 로 **호당** 세션 수를 내므로, 호가 줄면
        호당 세션이 늘어 분당 세션 총량이 그대로 유지된다. 그래서
        `sessions_peak` 도 함께 줄인다.

        **비율 계수는 건드리지 않는다.** `p_dns`·`http_per_session` 은
        `rates.dns / rates.transport` 꼴이라 분자·분모에 같은 비율을 곱하면
        값이 변하지 않는다 — 다시 계산하면 반올림만 흔들린다.

        **생성기를 다시 만들지 않는다.** `SessionEngine` 이 활성 세션을 분
        사이에 들고 있어서 새로 만들면 세션이 끊기고 종료 레코드가 나오지
        않는다(`self.sessions` 주석). 요율만 갈아 끼우면 다음 분부터 반영된다.

        0% 도 받는다 — "만들지 않는다"는 뜻이고 `_volume()` 이 0 을 돌려준다.
        그 구간은 **비어 있고 그것이 사실이다**(불변규칙 4: 0 으로 채우지
        않는다). 다만 `enabled=false` 와는 다르다 — 그쪽은 틱 자체를 건너뛴다.
        """
        # **상한을 두지 않는다.** 0~100 은 UI·API 의 정책이고(`ui.api_set_settings`
        # 가 거부한다) 이 메서드는 비율을 받아 적용하는 일만 한다 — `capacity`
        # 측정이 200%·400% 를 재야 하는데, 여기서 100 으로 자르면 **같은 값을
        # 다른 배수로 측정하고 그 결과로 기울기를 외삽한다.** 실제로 그렇게
        # 7,362% 라는 엉뚱한 한계가 나왔다(2026-10-15).
        pct = max(0.0, float(pct))
        for s, base in self._base_rates.items():
            if s in self.gen.rates:
                self.gen.rates[s] = max(0, int(round(base * pct / 100.0)))
        tp = self._base_rates.get("transport", 0)
        if tp:
            # `max(1, ...)` 은 `SessionEngine.__init__` 과 같은 규칙이다 —
            # 0 이면 나눗셈이 깨진다. 0% 에서는 호 자체가 없어 무해하다.
            self.sessions.sessions_peak = max(1, int(round(tp * pct / 100.0)))
        self._volume_pct = pct
        return pct

    def volume_pct(self) -> float:
        """현재 적용된 비율(%). 로그·UI 가 인용한다."""
        return self._volume_pct

    def set_blocked(self, series: Iterable[str] | None) -> frozenset[str]:
        """계열을 끊는다. 적용된 집합을 돌려준다.

        **요율을 0 으로 누르지 않는다.** 그러면 `set_volume_pct` 가 기준값
        (`_base_rates`)을 다시 곱해 되살리고, 더 나쁜 것은 "이 환경은 그 계열을
        원래 안 만든다"(요율 0)와 "지금 일부러 끊었다"가 **구분되지 않는다** —
        화면이 경고를 띄울 근거가 사라진다(불변규칙 17 의 취지).

        `lte_call` 을 끊으면 **U-Plane 도 함께 멈춘다.** 세션이 성공 호에서
        파생되므로(설계 §1.1) 호가 없으면 열 세션이 없다. 숨기지 않고 화면이
        그렇게 적는다 — 조용히 넷이 다 비면 "생성기가 죽었다"로 읽힌다.

        끊긴 계열은 **파일을 만들지 않는다.** 빈 Parquet 를 쓰면 집계가 0행을
        "값이 0" 으로 읽을 자리가 생긴다(불변규칙 4).
        """
        want = frozenset(str(x) for x in (series or ()) if str(x))
        bad = want - set(BLOCKABLE_SERIES)
        if bad:
            raise ValueError(f"끊을 수 없는 계열이다: {sorted(bad)}")
        self._blocked = want
        return want

    def blocked(self) -> frozenset[str]:
        """지금 끊어 둔 계열. 화면·로그가 인용한다."""
        return self._blocked

    def set_diurnal(self, on: bool) -> bool:
        """시간대 곡선 on/off. 적용값을 돌려준다.

        **플래그가 하나다.** `Generator.diurnal` 이 레코드 개수(`_volume`) ·
        레코드당 바이트(`_traffic_scale`) · 호당 세션 수
        (`SessionEngine.session_lambda`) 세 자리를 함께 가린다 — 따로 두면
        한쪽만 꺼져 총 트래픽 배수가 설계(4배)와 조용히 달라진다.

        끄면 **모든 시각이 피크(19시)와 같아진다.** 배율 1.0 이 기준이므로
        평탄화는 저점을 끌어올리는 쪽이다 — 야간에도 주간 수준이 나온다.
        """
        self.gen.diurnal = bool(on)
        return self.gen.diurnal

    def diurnal(self) -> bool:
        """시간대 곡선이 켜져 있나."""
        return bool(self.gen.diurnal)

    def _seat_mobility(self, mb: "Mobility", seed: int) -> None:
        """경로를 깔고 가입자를 태운다. **끄기가 기본이다.**

        `routes_file` 이 있으면 그것을 읽고, 없으면 `count` 개를 자동 생성한다.
        둘 다 없으면 아무것도 하지 않는다 — 그때 전환은 `Topology.pick` 의
        추첨이고, 경로가 없던 때와 같다.

        **파일이 있는데 읽히지 않으면 멈춘다.** 조용히 자동 생성으로 넘어가면
        "노선을 정의했는데 그 구간이 데이터에 없다"가 되고 원인이 경로 파일의
        오타다.
        """
        rng = random.Random(seed + 7717)
        # **경로와 독립이다.** 노선을 깔지 않아도 지역성만 켤 수 있다
        self.gen.home_locality = bool(mb.home_locality)
        self.gen.home_radius_m = float(mb.home_radius_m)
        if mb.routes_file:
            routes = HO.load_routes(mb.routes_file, self.cells)
        elif mb.count > 0:
            routes = HO.build_routes(
                self.ho_topo, self.cells, mb.count, rng, length=mb.length,
                dwell=(mb.dwell_min, mb.dwell_max), loop_share=mb.loop_share)
        else:
            # 노선이 없어도 그냥 이동·시간대 비율은 선다 — 둘은 노선과 다른 축이다
            self._seat_groups(mb, rng)
            return
        # **여정을 `seat_routes` 전에 걸어야 한다** — 그 안에서 배정한다
        self.gen.journeys = list(mb.journeys)
        self.gen.off_sec = (mb.off_sec_min, mb.off_sec_max)
        self.gen.seat_routes(routes, mb.sub_share, rng)
        self.gen.mobility_share = mb.call_share
        self._seat_groups(mb, rng)

    def _seat_groups(self, mb: "Mobility", rng: random.Random) -> None:
        """그냥 이동 그룹과 시간대 비율을 건다 — **노선 뒤에** 부른다(노선
        탑승자를 건너뛰고 나머지에서 고른다)."""
        self.gen.seat_walkers(mb.walk_share, rng, (mb.walk_sec_min, mb.walk_sec_max))
        self.gen.groups = (Groups(mb.hourly, mb.subway_hours, mb.blend_min)
                           if mb.hourly else None)

    def records(self, minute: int, ts: datetime) -> list[list[str]]:
        """한 분의 레코드(생성기 필드 순서의 값 리스트)를 만든다.

        순서가 중요하다: 기반 생성 → **상용 분포 덮기** → 시나리오 변형.
        덮기를 변형 뒤에 두면 장애가 지워진다.

        **성공 호를 모아 둔다**(2026-10-14). U-Plane 이 그 호에서 파생되므로
        (`session` 머리말) 어떤 호가 베어러를 세웠는지를 여기서만 알 수 있다.
        변형 **뒤에** 분류하는 이유는 장애가 호의 결말을 바꾸기 때문이다 —
        `ran_radio` 가 심은 `drop_flag=1`·Cause 121 이 U-Plane 의 "즉시 끊김"
        으로 이어져야 한다(설계 §3.2).
        """
        gen = self.gen
        # **이동을 먼저 전진시킨다.** 호를 만든 뒤에 옮기면 그 분의 호가
        # 한 칸 전 위치에서 생기고, 전환이 분 경계에 몰린다
        gen.advance_routes(ts.timestamp())
        slot_start = ts.timestamp()
        active = self._active(minute)
        mutate = self._mutator(minute, active)
        idx = gen.index["lte_call"]
        i_user = tuple(idx[f] for f in USER_FIELDS)      # 필드 인덱스 캐시
        i_apn, i_end = idx["APN"], idx["call_end_time"]
        i_rel = idx["UEContextRelease_Cause"]
        # **세션 수가 Call Type 에 따라 갈린다**(`session.CT_SESSION_W`).
        #
        # ⚠️ **키는 `call_type`(소문자)이다.** 전에 `idx.get("CALL_TYPE", -1)`
        # 로 적혀 있었는데 `CALL_TYPE` 은 **Parquet 컬럼명**이고
        # `fields.SERIES_FIELDS["lte_call"]` 의 필드명은 `call_type` 이다
        # (인덱스 38). 그래서 `i_ct` 가 항상 `-1` → `Call.call_type` 이 항상
        # `""` → `session._ct_weight()` 가 전부 1.0 을 돌려줘 **`CT_SESSION_W`
        # 가 한 번도 적용되지 않았다.** 알림만 받고 끝나는 호(Paging·TAU·
        # Detach)도 데이터 세션을 열었고, 에러는 나지 않았다.
        i_ct = idx.get("call_type", -1)

        out: list[list[str]] = []
        calls: list[Call] = []
        # **끊긴 계열은 만들지 않는다**(2026-10-15). `_calls` 를 비워 두면
        # U-Plane 도 자연히 멈춘다 — 세션이 호에서 파생되므로 따로 막을 필요가
        # 없고, 그 인과를 끊어 두면 "호는 0인데 트래픽은 있다"가 된다.
        n_calls = 0 if "lte_call" in self._blocked else gen._volume("lte_call", ts)
        # **장애가 없는 분에도 부른다** — 창이 끝난 분에 `None` 으로 되돌려야
        # 가중이 평시로 돌아온다
        gen.set_weight_boost(self._boost(minute, active))
        suppress = self._suppressor(minute, active)
        relocate = self._relocator(minute, active)
        shift = self._shifter(minute, active)
        i_imsi = idx["IMSI"]
        # **장애 대상 셀의 판정 하한** (2026-10-29 사람 결정 · `FaultCfg.floor_*`).
        # 사람이 위치를 찍어 장애를 넣었는데 그 셀에 호가 분당 0.3건이면
        # 열화를 걸어도 최소 표본(20)이 서지 않아 **아무 데도 안 보인다**
        # (실측: 14번 대상 8셀 합계 분당 1~6건, 이벤트 0). 그래서 평소 호를
        # 다 낸 뒤 대상 셀마다 모자란 만큼 **그 셀에 호를 더 만든다.** 더 만든
        # 호도 아래 같은 경로(이동·덮기·장애 변형·세션 파생)를 탄다.
        # 세는 것은 **이동 전 셀**이다 — 24번은 A 에 채운 뒤 60% 가 B 로 간다.
        landed: collections.Counter = collections.Counter()

        def drafts():
            for _ in range(n_calls):
                if suppress is None:
                    rec, cell = gen.base_lte_call(ts, slot_start)
                else:
                    # ── 억제된 셀의 호는 **버리지 않고 다른 셀로 재추첨한다** ──
                    #
                    # 실제 망에서 인접 셀이 흡수하는 모습이고, **전국 호 수가
                    # 유지되어 "전국 트래픽 급감"이라는 가짜 신호가 생기지
                    # 않는다** — D4 「트래픽 소멸」의 ① 결측 가드가 바로 그
                    # 구분(수집 결측 vs 특정 eNB 무호)을 본다.
                    #
                    # 상한 8회: 대상 eNB 가 전체의 1/60 이므로 8회 연속 실패
                    # 확률이 `(1/60)^8` 로 무시할 수 있다. 그래도 상한을 두는
                    # 것은 운영자가 eNB 를 하나만 남기고 억제하는 설정을 넣을 수
                    # 있어서다 — 그때 무한 루프가 되면 데몬이 멈춘다.
                    for _retry in range(8):
                        rec, cell = gen.base_lte_call(ts, slot_start)
                        if not suppress(cell):
                            break
                    else:
                        continue
                yield rec, cell
            # 평소 호를 다 낸 뒤 — `landed` 가 이 분의 대상 셀 호 수를 다 셌다
            if n_calls:
                yield from self._floor_drafts(minute, active, landed, suppress,
                                              ts, slot_start)

        for rec, cell in drafts():
            landed[cell.cell_key] += 1
            dst = relocate(cell) if relocate is not None else None
            if dst is None and shift is not None:
                # 증설된 B 로 붙는 호(`j-sc-24`) — 아래와 같이 **같은 사람**이다
                dst = shift(cell)
            if dst is not None:
                # ── 정지 셀의 호는 **같은 사람**이 이웃 셀에서 다시 건다 ──
                #
                # 위 suppressor 처럼 다시 추첨하면 다른 가입자가 나와 "재분배"
                # 일 뿐이다. 확정대장 §11.3 조건 4 가 "정지 직전 그 셀에 있던
                # IMSI 가 주변 셀에 나타날 것"이다. 가입자를 되찾지 못하면
                # (뮤테이터가 IMSI 를 바꾼 경우는 여기 오기 전이라 없다)
                # 원래 호를 그대로 둔다
                si = gen.sub_by_imsi.get(rec.values[i_imsi], -1)
                if si >= 0:
                    rec, cell = gen.base_lte_call(ts, slot_start,
                                                  at=(gen.subs[si], dst))
            self._coerce(rec, cell)
            if mutate is not None:
                mutate("lte_call", rec, cell)
            vals = rec.values
            out.append(vals)
            outcome = self.sessions.classify(rec)
            if outcome is not None:
                # **레코드에 적힌 값을 그대로 가져간다.** 생성기의 가입자
                # 객체를 다시 읽으면 뮤테이터가 바꾼 값(기종 등)과 어긋나고,
                # 그러면 U-Plane 의 IMSI 가 C-Plane 에 없는 값이 될 수 있다
                # (§7.2 고아 레코드 0).
                try:
                    t = float(vals[i_end] or slot_start)
                except ValueError:
                    t = slot_start
                calls.append(Call(
                    cell=cell, user=tuple(vals[i] for i in i_user),
                    apn=vals[i_apn], t=t, outcome=outcome, rel=vals[i_rel],
                    call_type=(vals[i_ct] if i_ct >= 0 else "")))
        # ── TAU — **TAC 가 바뀐 전환마다 한 건** ────────────────────────
        #
        # 핸드오버 레코드와 **별개 절차**다. 이것이 없으면 "TAC 가 바뀌었는데
        # 아무 절차도 없다"가 되고, TAU 성공률·소요가 통째로 빈다 — 0 은
        # "문제 없음"으로 읽힌다(불변규칙 17).
        #
        # **`_volume` 밖이다.** 분당 호 수를 TAU 로 깎으면 이동을 켜는 것만으로
        # 다른 절차의 분모가 줄어 기존 기준선과 전후 비교가 안 된다. TAU 는
        # 실제로 **추가 절차**이므로 더해지는 것이 맞다.
        #
        # **핸드오버가 TAU 를 다시 만들지 않게** `_baseline_handover` 를 타지
        # 않는다 — TAU 레코드는 이미 전환의 결과이고, 거기에 또 셀을 바꾸면
        # 떠난 TA 기록(`old_call_Last_TAC`)과 어긋난다.
        tau = gen.tau_events
        # **TAU 도 생성량 비율을 따른다**(2026-10-29 사람 결정). 호 수는
        # `volume_pct`(현장 15%)로 줄이는데 움직이는 사람 수는 가입자 풀
        # 그대로라, TAU 만 100% 로 나와 낮에 전체 레코드의 20%(분당 ~700건)
        # 가 됐다. 이동·TA 판정은 건드리지 않고 **기록만** 같은 비율로 뽑는다
        keep = min(1.0, max(0.0, self._volume_pct / 100.0))
        if tau and "lte_call" not in self._blocked:
            for sub_i, src, dst in tau:
                if keep < 1.0 and gen.rng.random() >= keep:
                    continue
                rec, cell = gen.tau_rec(ts, slot_start, gen.subs[sub_i],
                                        src, dst)
                self._coerce(rec, None, force_call_type=TAU_CALL_TYPE)
                if mutate is not None:
                    mutate("lte_call", rec, cell)
                out.append(rec.values)
        # **매 분 비운다.** 남겨 두면 같은 전환으로 TAU 가 계속 나온다
        gen.tau_events = []

        self._calls, self._calls_at = calls, ts
        return out

    def uplane_records(self, minute: int, ts: datetime) -> dict[str, list[list[str]]]:
        """한 분의 U-Plane 레코드 — 계열별 값 리스트.

        **`records()` 를 먼저 불러야 한다.** U-Plane 은 그 분의 성공 호에서
        파생되므로(설계 §1.1) 호 목록 없이는 만들 것이 없다. 분이 어긋나면
        **멈춘다** — 조용히 다른 분의 호에 트래픽을 붙이면 같은 분의 C-Plane·
        U-Plane 이 다른 장애를 보게 되고, 에러가 나지 않는다.

        `records`(lte_call)와 달리 **`_coerce` 가 없다.** 덮어쓸 상용 실측
        분포가 없기 때문이다 — 상용에 원시 Transport·DNS·HTTP XDR 자체가
        없어서 `_distributions.json` 에 그 계열의 값 분포가 들어 있지 않다.
        여기서 억지로 C-Plane 분포를 가져다 쓰면 근거 없는 수치가 된다.

        켜 두지 않은 계열(`pemdb1.rates` 0)은 **목록에서 뺀다.** 빈 리스트를
        넣어 두면 적재가 0행을 "값이 0" 으로 쓸 자리가 생긴다(불변규칙 17).
        """
        if self._calls_at != ts:
            raise RuntimeError(
                f"uplane_records({ts:%Y-%m-%d %H:%M}) 앞에 records() 가 없다 "
                f"(호 목록은 {self._calls_at} 것이다). U-Plane 은 그 분의 성공 "
                "호에서 파생된다 — 순서를 바꾸면 인과가 끊어진다")
        active = self._active(minute)
        # 열려 있던 세션도 정지 셀을 떠난다 — `records` 와 같은 콜백이다
        self.sessions.relocate = self._relocator(minute, active)
        # 장애 대상 셀의 세션은 **대상 앱을 더 자주 고른다** — 장애가 없는
        # 분에는 빈 목록으로 되돌린다
        book = self.gen.app_book
        self.gen.app_boosts = [
            (set(sc.active_cells_cache) if sc.target.cell_keys else None,
             book.boosted(lambda a, sc=sc, i=sc.intensity(minute):
                          APP_BOOSTERS[sc.kind](sc, i, a)))
            for sc in active if sc.kind in APP_BOOSTERS] if book else []
        got = self.sessions.minute(ts, self._calls,
                                   self._mutator(minute, active),
                                   self._plan_mutator(minute, active))
        # 끊긴 계열은 **목록에서 뺀다** — 빈 리스트로 두면 적재가 빈 Parquet
        # 파일을 만들고, 집계가 그 0행을 "값이 0" 으로 읽을 자리가 생긴다
        # (불변규칙 4). 파일이 없으면 그 분은 **없는 것**이고 그게 사실이다.
        return {s: rows for s, rows in got.items()
                if s in self.series and s not in self._blocked}

    def _coerce(self, rec, cell: Cell | None = None,
                force_call_type: str | None = None) -> None:
        """자체 프로파일에서 나온 값을 상용 실측값으로 바꾼다.

        ``force_call_type`` 은 절차를 **고정**한다. 평소에는 실측 분포가
        `call_type` 을 정하는데, TAU(`5`)처럼 "그 레코드가 왜 생겼는지"가
        이미 정해진 경우가 있다 — TAC 가 바뀐 전환이 그렇다.

        ``cell`` 은 **평시 PathSwitch 가 Last 셀을 정하려고** 받는다. V3 §3.5 는
        그것을 `generator._lte_call` 에 두라고 적었는데(그 함수가 셀을 안다),
        거기서는 **`call_type` 이 아직 프로파일 값이다** — `_coerce` 가 바로
        뒤에서 `d.call_type()` 으로 덮으므로 절차 그룹(`_CT_GROUP`)이 어긋난다.
        PathSwitch 시도 비율은 Service 8% / Handover 100% 로 그룹마다 다르니
        **최종 `call_type` 을 아는 자리**에 있어야 한다. 그래서 셀을 받는 쪽으로
        돌렸다 (V3 대비 변경점 — `docs` 에 기록).
        """
        d = self.dist
        # 상용 추출은 ONGOING_FLAG='2'(호 종료)만 담는다. 1(진행 중) 레코드를
        # 섞으면 플래그가 비어 분모가 조용히 줄어든다.
        rec.set("OngoingFlag", ONGOING_END)
        # **`force_call_type` 은 실측 분포를 이긴다.** TAU 레코드가 그것으로
        # 온다 — 여기서 분포로 덮으면 TAC 가 바뀐 전환에 TAU 가 아닌 절차가
        # 붙고, 그러면 그 레코드가 무엇인지 아무도 모른다.
        ct = force_call_type or d.call_type()
        rec.set("call_type", ct)
        f = d.flags(ct)
        rec.set("attempt_flag", f["att"])
        rec.set("success_flag", f["suc"])
        rec.set("drop_flag", f["drop"])
        rec.set("detach_flag", f["detach"])
        # 부가 절차 플래그 — 이걸 비워 두면 `C-20`(Data)·`C-21`(IMS) 의 분모가
        # 0 이라 그 두 지표가 **한 번도 판정되지 않는다**. 빈 값은 "이상 없음"
        # 으로 보이고 그것이 불변규칙 17 이 금지하는 형태다.
        rec.set("data_attempt_flag", f["data_att"])
        rec.set("data_success_flag", f["data_suc"])
        rec.set("ims_attempt_flag", f["ims_att"])
        rec.set("ims_success_flag", f["ims_suc"])
        rec.set("paging_attempt_flag", f["pg_att"])
        rec.set("paging_success_flag", f["pg_suc"])
        rec.set("APN", d.apn())

        # ── 평시 절차 플래그 (2026-10-22) ────────────────────────────────
        #
        # **`P-01`·`P-02`·`P-03`·`P-07` 의 분모가 여기서 생긴다.** 비워 두면
        # 그 네 지표가 한 번도 판정되지 않고, 빈 값은 "이상 없음"으로 보인다
        # (불변규칙 17). SC-04·14·15 는 `auth_attempt_flag` 가 선 호만
        # 고르므로(V3 ⑥) 이것이 없으면 **주입 자체가 성립하지 않는다.**
        if self.baseline is not None:
            self._baseline_procedures(rec, f)

        # 해제 Cause — 절단 호의 사유. **정상 종료도 포함한다**(운영 45.9%).
        #
        # 평시 배경이 있으면 `drop_cause_mix` 로 뽑는다 — **정상 종료를 포함한다**
        # (2026-10-22, `_DROP_CAUSES` 머리말). 구 주석은 "둘 다
        # `S1AP_NORMAL_CAUSES` 밖"이라 적었고 `DROP_RADIO/DROP_CNT` 통과 조건
        # [0.45, 0.65] 를 근거로 들었는데, **운영 실측이 0.153 이다** — 그
        # 조건 쪽이 운영과 맞지 않는다.
        if f["drop"]:
            rel = (str(d.drop_cause()) if self.baseline is not None
                   else d.abnormal_release())
        else:
            rel = d.release_cause()
        rec.set("UEContextRelease_Cause", rel)
        rec.set("UEContextRelease_Time",
                rec.get("call_end_time", "0.0") if rel != "0" else "0.0")

        # 플래그를 덮었으므로 오류 기록도 같이 맞춘다. 기반 생성기가 자체
        # 프로파일로 남긴 First Error 를 그대로 두면 "성공인데 오류 기록 있음"
        # 같은 모순이 남아, 룰이 증상만 보고 발동한다(불변규칙 7).
        if f["att"] and not f["suc"]:
            self._set_error(rec, *d.first_error())
        else:
            self._clear_error(rec)

        # 평시 핸드오버 — `PSF` 의 분모 + 셀·장비·TAC 변경. 셀 마스터와
        # **최종 해제 Cause** 를 둘 다 알아야 하므로 마지막이다
        if self.baseline is not None and cell is not None:
            self._baseline_handover(rec, ct, cell, rel)
            # 직전 호 — 핸드오버 **뒤**다. 핸드오버-In 호의 출발 셀을
            # `_baseline_handover` 가 정한 이웃 관계에서 고르기 때문이다
            self._baseline_old_call(rec, ct, cell, f)

    def _baseline_procedures(self, rec, f: dict) -> None:
        """인증·가입정보·단말식별·NPR 플래그와 그 실패 기록.

        **실패 호에 오류 기록을 남긴다.** 세 Cause 필드
        (`AuthenticationInformation_Cause`·`UpdateLocation_Cause`·
        `MEIdentityCheck_Cause`)는 집계·RCA 가 읽지 않는다 — 그래도 적는
        이유는 "성공률이 99.9% 인데 실패 호에 아무 기록이 없다"가 **사람이
        읽는 모순**이기 때문이고, `R-01` 의 설명문이
        `AuthenticationInformation_Cause` 를 인용한다.

        `drop_flag` 는 여기서 손대지 않는다 — `flags()` 가 정하고 호출부가
        쓴다. 한 자리에서만 정해야 `success=1 AND drop=1`(연결 후 절단)과
        `success=0 AND drop=0`(접속 실패)의 배타성이 지켜진다(규칙 C-2).
        """
        rng = self.dist.rng
        t0 = rec.num("call_start_time")
        for key, flag, cause_col, time_col, cause in (
            ("auth", "auth", "AuthenticationInformation_Cause",
             "AuthenticationInformation_Time", 5012),
            ("loc", "location", "UpdateLocation_Cause",
             "UpdateLocation_Time", 5420),
            ("mec", "mecheck", "MEIdentityCheck_Cause",
             "MEIdentityCheck_Time", 5030),
        ):
            att, suc = f[f"{key}_att"], f[f"{key}_suc"]
            rec.set(f"{flag}_attempt_flag", att)
            rec.set(f"{flag}_success_flag", suc)
            if not att:
                continue
            t = t0 + rng.uniform(0.02, 0.20)
            rec.set(time_col, f"{t:.6f}")
            if not suc:
                rec.set(cause_col, cause)
        rec.set("npr_flag", f["npr"])

    def _baseline_handover(self, rec, call_type: str, cell: Cell,
                           rel: str) -> None:
        """평시 핸드오버 — 셀·eNB·TAC·장비 ID 를 **함께** 바꾼다.

        ## 전에 무엇이 모자랐나

        이 함수는 `_baseline_pathswitch` 였고 하는 일이 둘이었다 —
        `PathSwitch_count` 를 심어 `PSF`(PathSwitch 실패율)의 분모를 만들고,
        70% 확률로 `Last_Cell_ID` 를 같은 eNB 의 다른 섹터로 바꾸는 것.
        그래서 **전환이 일어나도 바뀌지 않는 것들**이 있었다 (근거와 실측은
        `handover` 머리말) —

        * `Last_eNB_ID` 를 쓰기는 했지만 풀이 같은 eNB 라 **값이 늘 같았다.**
        * `Last_eNB_VLAN_ID`·`Last_eNB_PLMN` 은 아예 안 고쳤다 — 셀이 바뀌었는데
          그 셀의 VLAN 이 아닌 값이 남는다.
        * `interval_First_*` 가 늘 First 와 같았다. 스펙은 **Interim 구간의
          셀**을 그 필드로 보라고 하는데(§3.1.3·§4.2) 그 값이 전환을 모른다.
        * `SGW_ID`·`MME_ID`·TAC 가 영원히 그대로 → S10 재배치도 TAU 유발도
          데이터에 없다.
        * `UEContextRelease_Cause` 가 핸드오버인데 `First == Last` 인 호가
          있었다 — 해제 사유는 실측 분포에서 **독립으로** 뽑혔다.

        ## 지금

        `PathSwitch_count` 의 **확률과 값은 건드리지 않았다.** SC-09 가
        "평시 2% → 장애 25%" 로 비교하므로 그 분모가 움직이면 시나리오가
        깨진다. 바뀐 것은 **그 사건에 딸린 변경을 제대로 적는 것**이다.

        종류는 둘로 갈라 뽑는다 —

        * PathSwitch 가 뽑힌 호 → `X2` (그 메시지가 있는 전환이 X2 다)
        * 해제 Cause 가 핸드오버인 호 → 그 Cause 가 가리키는 종류
          (`REL_BY_KIND` 의 역방향)

        **이웃이 없으면 전환을 취소한다.** 종류를 적고도 셀이 그대로면 집계가
        일어나지 않은 전환을 센다.
        """
        bl = self.baseline
        rng = self.dist.rng
        grp = _CT_GROUP.get(call_type, "service")

        # ① **전환이 먼저고 `PathSwitch` 는 결과다** (2026-10-28 교정).
        #
        # 구 구현은 인과가 거꾸로였다 — `PathSwitch` 를 뽑고 그 호의 종류를
        # **`X2` 로 고정**했다. `PathSwitch_count > 0 ⟺ X2` 는 맞지만
        # **역이 아니다.** X2 가 아닌 전환(섹터 간·S1·S1_MME)도 일어나는데
        # 그 경로가 없어서 —
        #
        #     실측 141,200호   셀 변경 9,465 · 그중 eNB 간 9,456(99.9%)
        #                      PathSwitch 9,452 — **사실상 전부 X2**
        #                      섹터 간 9건(0.01%)   `KIND_W` 선언은 55%
        #
        # `S1`·`S1_MME` 가 **구조적으로 나올 수 없었다.** 그래서 MME·SGW
        # 재배치가 일어나지 않고 `s10_error_*` 구간이 데이터에 없다 —
        # `handover` 머리말이 고치려던 문제가 그대로 남아 있었다.
        #
        # 지금은 **전환 여부를 먼저 정하고 종류를 `KIND_W` 로 뽑는다.**
        # `pathswitch_attempt` 는 그대로 쓰되 의미가 "전환 시도"다 —
        # 이름은 설정 호환을 위해 두고 뜻만 이 주석이 고정한다.
        p = bl.pathswitch_attempt.get(grp, 0.0)
        kind = None
        if p > 0.0 and rng.random() < p:
            kind = self.ho_topo.pick_kind(cell, rng)

        # ①-a **전환 상대를 먼저 확보한다.** 못 구하면 전환이 **일어나지
        #      않은 것**이고, 그때 `PathSwitch` 만 적으면 "셀은 그대로인데
        #      X2 가 있는" 레코드가 된다.
        #
        # 집계가 그것을 바로 잡아냈다(2026-10-28 · 15:40~15:45 재집계) —
        #
        #     HO_CNT 506 · HO_X2_CNT 741 · HO_INTER_CNT 0
        #     → X2 가 전환보다 **많다**(146%). 파생 `S1 계열` 이 **음수**.
        #
        # 구 구현도 같은 순서였다. `PathSwitch` 가 전환의 **결과**인데
        # 원인보다 먼저 적히던 자리다.
        nb = self.ho_topo.target(cell, kind, rng) if kind else None
        if kind is not None and nb is None:
            kind = None

        if kind == HO.HO_X2:
            # **X2 에서만 `PathSwitchRequest` 가 있다.** 타겟 eNB 가 MME 로
            # 직접 보내는 절차이고, S1 은 MME 가 중개하므로 그 메시지가
            # 없다. 섹터 간은 eNB 안에서 끝난다(`handover` 머리말).
            #
            # 그 결과 `PathSwitch` 비율이 전환의 **30%**(`KIND_W[X2]`)로
            # 줄어든다 — 실측 6.69% → 약 2% 이고, SC-09 가 기준으로 삼는
            # *"평시 2%"* 와 맞는다(그 시나리오 주석).
            ps = rng.randint(1, 3)
            psf = 1 if rng.random() < bl.pathswitch_fail else 0
            rec.set("PathSwitch_count", ps)
            # 규칙 — `실패 ≤ 시도`. 넘으면 `PSF` 분모(`ps + psf`)와 분자가
            # 어긋나 100% 넘는 실패율이 나온다
            rec.set("PathSwitchFailure_count", min(psf, ps))

        # ② 해제 Cause 가 핸드오버면 **셀이 반드시 바뀐다.** PathSwitch 쪽이
        #    이미 종류를 정했으면 그것을 따른다 — Cause 를 다시 덮으면 ①의
        #    분모가 흔들린다
        if kind is None and rel in HO.HANDOVER_CAUSES:
            # 한 Cause 가 여러 종류에 걸칠 수 있다(`102` 는 섹터 간도 eNB 간도
            # 된다). 좁히지 않고 **그 후보들 사이에서 가중 추첨**한다
            kind = self.ho_topo.pick_kind(
                cell, rng, only=HO.KINDS_BY_REL.get(rel))
            nb = self.ho_topo.target(cell, kind, rng) if kind else None

        if kind is None or nb is None:
            return

        self._apply_handover(rec, cell, nb, kind, rel)

    def _baseline_old_call(self, rec, call_type: str, cell: Cell,
                           f: dict) -> None:
        """직전 호 기록(`old_call_*`) — **지금까지 통째로 비어 있었다.**

        ## 왜 채우나

        둘이다.

        ① **핸드오버-In 호는 어디서 왔는지가 있어야 한다.** `call_type 10`
        (S1 Handover In)·`11` 은 이전 호가 이웃 셀에서 핸드오버 Cause 로 끝나고
        그것을 이어받은 것이다. `old_call_Last_*` 가 비면 "넘어왔다"는 말만
        있고 **어디서 넘어왔는지가 없다** — 셀 쌍 분석(`j-sc-15` 장표 9)이
        U-Plane 쪽 자료만으로 서야 한다.

        ② **`OLD_FAIL_CNT`·`OLD_SAME_IFACE` 의 분자가 여기서 생긴다.**
        graphio `cell_agg` 가 `OLD_CALL_ERROR_CAUSE` 유무로 그 둘을 세는데
        (`PM_COR_LTE_CALL_1M`), 생성기가 그 필드를 한 번도 쓰지 않아 **두
        컬럼이 구조적으로 늘 0** 이었다. 0 은 "직전 호 실패가 없다"로 읽힌다 —
        불변규칙 17 이 금지하는 형태이고, "같은 원인의 반복" 신호가 통째로
        죽어 있었다.

        ## 규칙

        * 핸드오버 그룹(`_CT_GROUP == "handover"`) → 출발 셀은 **이웃**이다.
          해제 사유는 핸드오버 Cause 이고 오류 기록은 없다(정상 전환).
        * 그 밖에 **실패한 호** → `old_call_fail` 확률로 직전 호도 실패였다고
          적는다. 그중 `old_call_same_iface` 는 **같은 구간**에서 실패한다.
        * **성공한 호에는 오류 기록을 남기지 않는다.** 남기면 "직전 호가
          실패인데 이 호는 성공"이 되고, 그건 재시도 성공이라 `OLD_FAIL_CNT`
          의 뜻(실패 반복)과 어긋난다 — `cell_agg` 도 `SUCCESS_FLAG != 1` 을
          조건에 달고 있다.
        """
        bl = self.baseline
        rng = self.dist.rng
        t0 = float(rec.get("call_start_time", "0.0") or 0.0)
        grp = _CT_GROUP.get(call_type, "service")

        if grp == "handover":
            got = self.ho_topo.pick(cell, rng)
            if got is None:
                return
            src, kind = got
            rec.set("old_call_type", rng.choice(("3", "3", "3", "1", "5")))
            rec.set("old_call_end_time", f"{t0 - rng.uniform(0.05, 0.6):.6f}")
            rec.set("old_call_Last_eNB_ID", src.enb_id)
            rec.set("old_call_Last_Cell_ID", src.cell_id)
            rec.set("old_call_Last_TAC", src.tac)
            rec.set("old_call_Last_eNB_PLMN", src.plmn)
            rec.set("old_call_s1ap_release_cause",
                    HO.REL_BY_KIND[kind][0])
            # 정상 전환이다 — 오류가 아니다(불변규칙 6·7)
            rec.set("old_call_error_interface_protocol", "0")
            rec.set("old_call_error_cause", "0")
            return

        if not (f["att"] and not f["suc"]):
            return
        if rng.random() >= bl.old_call_fail:
            return
        iface, cause = self.dist.first_error()
        if rng.random() < bl.old_call_same_iface:
            # 같은 자리에서 또 막혔다 — `OLD_SAME_IFACE` 의 분자
            iface = rec.get("first_error_interface_protocol") or iface
        rec.set("old_call_type", call_type)
        rec.set("old_call_end_time", f"{t0 - rng.uniform(0.2, 8.0):.6f}")
        rec.set("old_call_Last_eNB_ID", cell.enb_id)
        rec.set("old_call_Last_Cell_ID", cell.cell_id)
        rec.set("old_call_Last_TAC", cell.tac)
        rec.set("old_call_Last_eNB_PLMN", cell.plmn)
        rec.set("old_call_error_interface_protocol", iface)
        rec.set("old_call_error_cause", cause)

    def _apply_handover(self, rec, src: Cell, dst: Cell, kind: str,
                        rel: str) -> None:
        """전환의 결과를 레코드에 적는다 — **한 자리에서 전부.**

        흩어 두면 한쪽만 고쳐져 "셀은 바뀌었는데 VLAN 은 옛 셀" 같은 레코드가
        남는다. 그건 에러가 아니라 조용히 틀린 값이다.

        `Last_*` 가 전환 **후**다. 실 XDR 은 레코드에 장비 필드가 한 벌뿐이라
        마지막 값이 남는다 — 그래서 `SGW_ID`·`MME_ID` 도 전환 후 값이다.
        **셀 귀속이 First 냐 Last 냐는 집계가 정한다**(graphio
        `cell_agg._cell_specs`) — 생성기는 둘을 다르게 적는 것까지가 일이다.
        """
        rng = self.dist.rng
        rec.set("Last_eNB_ID", dst.enb_id)
        rec.set("Last_Cell_ID", dst.cell_id)
        rec.set("Last_eNB_VLAN_ID", dst.vlan_id)
        rec.set("Last_eNB_PLMN", dst.plmn)

        # `interval_First_*` — **그 Interim 구간이 시작된 셀**이다(§3.1.3).
        # 이 레코드는 종료 레코드(`ONGOING_FLAG='2'`)이므로 마지막 구간이고,
        # 전환이 그 구간 **안**에서 일어났으면 구간은 옛 셀에서 시작했다.
        # 늘 First 로 두면 그 필드가 전환을 모르고, 늘 Last 로 두면
        # Interim 셀 귀속을 검증할 자료가 안 된다.
        t0 = float(rec.get("call_start_time", "0.0") or 0.0)
        t1 = float(rec.get("call_end_time", "0.0") or 0.0)
        iv_start = max(t0, t1 - 60.0)          # 마지막 1분 구간
        t_ho = t0 + (t1 - t0) * rng.random()
        iv = src if t_ho > iv_start else dst
        rec.set("interval_First_eNB_ID", iv.enb_id)
        rec.set("interval_First_Cell_ID", iv.cell_id)
        rec.set("interval_First_eNB_C_UID", iv.c_uid)
        rec.set("interval_First_TAC", iv.tac)
        rec.set("interval_First_eNB_VLAN_ID", iv.vlan_id)
        rec.set("interval_First_eNB_PLMN", iv.plmn)
        rec.set("interval_call_start_time", f"{max(t0, iv_start):.6f}")

        # 장비 재배치 — 종류가 정한다. `INTRA_ENB`·`X2` 는 코어가 그대로다
        if kind in (HO.HO_S1, HO.HO_S1_MME):
            rec.set("SGW_ID", dst.sgw_id)
            rec.set("PGW_ID", dst.pgw_id)
        if kind == HO.HO_S1_MME:
            rec.set("MME_ID", dst.mme_id)
            # MME 재배치는 S10 을 탄다. **오류가 아니라 절차다** — Cause 를
            # 심지 않는다. 심으면 "성공인데 오류 기록 있음"이 되어 룰이
            # 증상만 보고 발동한다(불변규칙 7)

        # 해제 Cause 가 핸드오버가 아니면 손대지 않는다. **거꾸로 덮으면
        # 안 된다** — 절단 호(`drop_flag=1`)의 Cause 를 정상 종료로 바꾸면
        # 절단율이 조용히 내려간다
        if rel not in HO.HANDOVER_CAUSES:
            return
        want = HO.REL_BY_KIND.get(kind)
        if want and rel not in want:
            # Cause 와 종류가 어긋났다 — Cause 를 종류에 맞춘다. 종류 쪽이
            # 셀·장비 변경을 이미 레코드에 적었으므로 그쪽이 사실이다
            rec.set("UEContextRelease_Cause", want[0])

    #: first_error_interface_protocol → 구간별 Cause 컬럼 접두어 (CD_IF_PROTO_CEI).
    _ERR_PREFIX = {"1": "s6a", "2": "s1ap", "3": "s11", "4": "s10",
                   "5": "emm", "6": "esm", "8": "s13"}

    @staticmethod
    def _clear_error(rec) -> None:
        """'없음' 은 NULL 이 아니라 문자열 '0' 이다 (실측: FIRST_ERROR_CAUSE 99.6%)."""
        for side in ("first", "last"):
            rec.set(f"{side}_error_interface_protocol", "0")
            rec.set(f"{side}_error_message", "0")
            rec.set(f"{side}_error_cause", "0")
            rec.set(f"{side}_error_time", "0.0")

    def _set_error(self, rec, iface: str, cause: str) -> None:
        t = rec.get("call_start_time", "0.0")
        for side in ("first", "last"):
            rec.set(f"{side}_error_interface_protocol", iface)
            rec.set(f"{side}_error_cause", cause)
            rec.set(f"{side}_error_time", t)
        prefix = self._ERR_PREFIX.get(iface)
        if prefix:
            # 구간별 Cause 컬럼에도 남긴다 — 룰이 First Error 구간을 교차
            # 확인한다(R-27). 한쪽만 채우면 그 확인이 항상 실패한다.
            rec.set(f"{prefix}_error_Cause", cause)
            rec.set(f"{prefix}_error_Time", t)

    # -- fault ------------------------------------------------------------

    def set_faults(self, scenarios: list[Scenario]) -> None:
        self.gen.scenarios = scenarios
        self.gen._bind_scenario_targets()

    def set_fault_rows(self, active: list[dict]) -> tuple[list[Scenario], list[int]]:
        """`CM_DATAGEN_FAULT` 활성 행 목록을 걸어 둔다 — 실시간 틱(`cli.cmd_run`)용.

        해석은 `faults.resolve_active` 가 한다. 구 창고 경로(`engine.DataGen`, 삭제됨)와
        **같은 함수**를 쓴다 — 두 경로가 각자 해석하면 같은 fault 가 서로 다른
        셀 집합에 걸리고, 그 차이는 에러 없이 다른 데이터로 나타난다.
        """
        if self.anchor is None:
            raise RuntimeError("anchor 미설정 — set_fault_rows 전에 anchor 를 정해야 한다")
        scs, bad = resolve_active_faults(
            self.cells, self.anchor, self.seed, active, self._fault_cache)
        self.set_faults(scs)
        return scs, bad


# --------------------------------------------------------------------------
# 적재
# --------------------------------------------------------------------------


class PemdbWriter:
    """원시 xDR 적재 — 구간 멱등. **전부 로컬 Parquet 다**(2026-10-14).

    처음에는 C-Plane 만 MySQL 이었다(`GP_RAW_LTE_CALL_KPI`). 화면 SCR-06·07 과
    RCA 증거팩이 그 원시 레코드를 직접 조회해서 DB 에 둘 이유가 있었다.
    **그 조회가 1차 통계(`PM_COR_*_1M`)로 옮겨 가면서 이유가 없어졌다** —
    남은 소비자는 1분 집계뿐이고, 그것은 파일에서 더 빠르게 돈다.

    그래서 두 평면이 같은 모양이 되었다. 다른 것은 **보관 기간**뿐이다
    (`raw_parquet` 머리말의 표: U-Plane 10분 · C-Plane 1440분).

    `conn` 은 **여전히 받는다.** 1분 집계 `PM_ENB_KPI_1M`·`PM_ENB_CAUSE_1M`·
    `PM_EPC_KPI_1M_ORG` 가 MySQL 로 가고(`aggregate_all`) 호출부가 같은
    커넥션을 쓴다 — "MySQL 에는 통계만 올라간다".
    """

    def __init__(self, conn, mapping: XdrMapping,
                 uplane_root: Path | None = None,
                 uplane_retain_minutes: int = UPLANE_RETAIN_MINUTES,
                 cplane_root: Path | None = None,
                 cplane_retain_minutes: int = CPLANE_RETAIN_MINUTES):
        self.conn = conn
        self.map = mapping
        #: U-Plane Parquet 루트. **없으면 쓰지 않는다** — `write_uplane` 을
        #: 부르면 멈춘다. 경로를 기본값으로 숨기면 설정을 빠뜨린 실행이
        #: 엉뚱한 디렉터리에 쌓고, 집계는 그것을 못 찾아 0행이 된다.
        self.uplane_root = uplane_root
        self.uplane_retain_minutes = uplane_retain_minutes
        #: C-Plane Parquet 루트. 같은 이유로 **없으면 `write_xdr` 이 멈춘다.**
        self.cplane_root = cplane_root
        self.cplane_retain_minutes = cplane_retain_minutes
        self.clamped = 0
        #: 숫자 컬럼에 온 빈 문자열을 NULL 로 바꾼 횟수. **조용히 바꾸지
        #: 않는다** — 실측 분포가 내는 "모름"이라 정상이지만, 갑자기 늘면
        #: 생성 쪽이 바뀐 것이다.
        self.nulled = 0

    def write_xdr(self, ts: datetime, rows: list[list[str]]) -> int:
        """C-Plane 한 분을 **Parquet 파일로** 쓴다 — 구간 멱등이다.

        구 구현은 `DELETE` + `INSERT` 로 멱등을 얻었다. 지금은 그 분의 파일을
        덮어쓴다 — 임시 이름으로 쓴 뒤 `os.replace` 라 **읽는 쪽이 반쯤 쓰인
        파일을 보지 않는다**(같은 머신의 `corr` 가 그 분을 집계하러 들어온다).

        ## 153컬럼 전부 `pl.String` 이다

        `GP_RAW_LTE_CALL_KPI` 가 전부 varchar 였고 집계가 그 전제로 쓰여 있다
        (플래그는 문자열 `'1'`, "없음"은 센티널 `'0'`). 숫자로 접으면
        `'021'`·`'0'`·`''` 이 전부 다르게 접히고 **플래그 비교부터 어긋난다.**
        저장소만 바꾸는 작업이므로 표현은 그대로 둔다 — 근거는
        `CPLANE_FIELDS` 주석.

        ## 길이 절단은 유지한다

        Parquet 에는 길이 제한이 없다. 그래도 `varchar(n)` 폭으로 자르는 이유는
        **자르지 않으면 DB 시절과 다른 값이 집계에 들어가기 때문이다** —
        전후 동일성이 이 작업의 판정 기준이다(`write_uplane` 과 같은 태도).
        횟수는 세어서 보고한다. 조용히 자르면 값이 틀린 것을 모른다.

        ## 행 루프를 돌지 않는다

        구 `_row()` 는 행마다 Python 루프로 튜플을 만들며 폭을 쟀다 — 분당
        2만 행 × 153컬럼 = 306만 번의 비교다. 여기서는 프레임을 한 번에 세우고
        절단·계수를 polars 식으로 컬럼당 한 번 돌린다(`write_uplane` 이 같은
        이유로 그렇게 바뀌었다).
        """
        if self.cplane_root is None:
            raise RuntimeError(
                "C-Plane Parquet 루트가 없다 — PemdbWriter(cplane_root=...) 로 "
                "주거나 설정 pemdb1.cplane_parquet.root 를 채워라")
        names = self.map.columns[1:]            # EVENT_TIME 제외
        widths = self.map.widths[1:]
        # **열 지향으로 한 번에 세운다.** `orient="row"` 는 polars 쪽에서
        # 전치하므로 Python 루프가 없다. 생성기 값은 전부 `str` 이다.
        frame = pl.DataFrame(
            rows, schema={n: pl.String for n in names}, orient="row")
        if frame.height:
            # 절단 횟수를 **먼저 센다.** 자른 뒤에 세면 전부 폭 이하라 0 이
            # 나온다 — 구 구현이 자르면서 셌던 자리다.
            over = frame.select([
                (pl.col(n).str.len_chars() > w).sum().alias(n)
                for n, w in zip(names, widths) if w is not None
            ])
            self.clamped += int(sum(over.row(0)))
            frame = frame.with_columns([
                pl.col(n).str.slice(0, w)
                for n, w in zip(names, widths) if w is not None
            ])
        # **민감 필드는 컬럼을 남기고 값을 비운다**(2026-10-28 사용자 결정).
        # 일본은 `通信の秘密` 로 보관 자체를 문제로 본다 — API 경계에서 막는
        # 것(불변규칙 13)보다 한 단계 앞이다. 컬럼을 지우지 않는 이유는
        # `xdr_mapping` 이 154필드를 **위치로** 맞추기 때문이다 — 하나를 빼면
        # 뒤 필드가 전부 한 칸씩 밀린다. 읽는 쪽(graphio)은 이 셋을 고르지 않는다.
        frame = frame.with_columns([
            pl.lit("", dtype=pl.String).alias(n)
            for n in CPLANE_REDACTED if n in frame.columns
        ])
        frame = frame.with_columns(
            # 분 귀속 컬럼. **마이크로초 해상도**다 — 읽는 쪽이 `[lo, hi)`
            # 비교를 하므로 해상도가 달라지면 경계에서 어긋난다
            # (`write_uplane` 과 같은 처리).
            pl.lit(ts).cast(pl.Datetime("us")).alias(CPLANE_SLOT)
        ).select(self.map.columns)
        n = raw_parquet.write_minute(self.cplane_root, CPLANE_SERIES, ts, frame)
        # **쓴 뒤에 보존 정리를 돌린다** — 지우는 주체를 쓰는 쪽에 둔 것은
        # 그쪽이 "방금 어느 분을 썼는가"를 알기 때문이다(`write_uplane_all`
        # 과 같다). 기준은 벽시계가 아니라 디스크의 가장 늦은 분이다
        # (`raw_parquet.purge` 머리말 — 백필이 쓰는 즉시 지워지지 않게).
        if self.cplane_retain_minutes > 0:
            raw_parquet.purge(self.cplane_root, CPLANE_SERIES,
                              self.cplane_retain_minutes)
        return n

    def write_uplane(self, ts: datetime, series: str, rows: list[list[str]],
                     spec: UplaneSpec) -> int:
        """한 계열의 한 분을 **Parquet 파일로** 쓴다 — `write_xdr` 과 같은 구간
        멱등이다(같은 분을 다시 돌리면 그 파일을 덮어쓴다).

        `EVENT_TIME` 에는 **분 시각을 넣는다.** 규격 필드(`SummaryCreateTime`)는
        epoch 실수지만 분 귀속 컬럼은 `datetime` 이어야 하고, 1분 집계에 분 안의
        초는 쓰이지 않는다. `write_xdr` 이 C-Plane 에 하는 것과 같은 처리다 —
        같은 분의 C-Plane·U-Plane 이 한 Incident 로 상관되려면 분 귀속이 같아야
        한다. 파일 이름에도 그 분이 들어가지만 **컬럼을 지우지 않는다**: 읽는
        쪽이 파일 이름이 아니라 데이터로 분을 판정할 수 있어야 한다(이름과
        내용이 어긋나도 집계가 조용히 틀리지 않는다).

        응답시간 계열(`RESPONSE_TIME`·`REQUEST_TIME`·`REQUEST_FIRST_TIME`)은
        **원래의 epoch 값을 그대로 둔다.** 집계가 둘의 차이를 쓰므로 분 시각으로
        덮으면 모든 응답시간이 0 이 된다.

        ## 값을 컬럼 단위로 세운다

        구 구현은 행마다 Python 루프를 돌며 튜플을 만들었다. 여기서는 문자열
        컬럼을 먼저 세우고 캐스팅·절단을 polars 식으로 한 번에 돌린다 — 분당
        4.6만 행을 도는 자리라 행 루프가 틱 시간의 대부분이었다.

        **빈 문자열은 "모름"이고 숫자 컬럼에서 그 표현은 NULL 이다**(불변규칙
        17). 0 으로 채우면 `REPLY_CODE` 에서는 NOERROR(성공)가 되고
        `ANSWER_COUNT` 에서는 "응답 0건"이 되어, 모르는 것이 사실처럼 집계된다.
        실측 분포가 0.385% 확률로 그런 값을 낸다 — `data/profile.json` 의
        `dns.categorical` 에서 `ReplyCode`·`AnswerCount`·`QueryRetryCount` 가
        그렇다(원본 .dat 에 그렇게 들어 있었다).

        **캐스팅 실패는 막지 않는다.** DB 시절 `STRICT_TRANS_TABLES` 가
        `ERROR 1366` 으로 적재를 멈춘 자리이고, 조용히 NULL 로 삼키면 그
        카운터만 비어 추적할 수 없게 된다 — 에러가 나는 쪽이 낫다.
        """
        if self.uplane_root is None:
            raise RuntimeError(
                "U-Plane Parquet 루트가 없다 — PemdbWriter(uplane_root=...) 로 "
                "주거나 설정 pemdb1.uplane_parquet.root 를 채워라")
        cols: dict[str, pl.Series] = {}
        exprs: list[pl.Expr] = []
        for name, pos, kind, width in zip(spec.columns[1:], spec.picks,
                                          spec.kinds, spec.widths):
            cols[name] = pl.Series(name, [r[pos] for r in rows], dtype=pl.String)
            e = pl.col(name)
            if kind == "str":
                if width is not None:
                    # 폭을 넘기면 자른다 — DB 의 `varchar(n)` 과 같은 값이
                    # 집계에 들어가야 전후 결과가 같다. 횟수는 아래에서 센다.
                    e = e.str.slice(0, width)
            else:
                # 빈 문자열(공백만 있는 것까지) → NULL. 그 뒤 캐스팅한다.
                e = (pl.when(e.str.strip_chars() == "")
                       .then(None)
                       .otherwise(e)
                       .cast(raw_parquet.DTYPES[kind]))
            exprs.append(e.alias(name))

        frame = pl.DataFrame(cols)
        # 절단·NULL 치환 횟수를 **세어서 보고한다.** 조용히 바꾸면 값이 틀린
        # 것을 모른다. 벡터 연산이라 비용은 컬럼당 한 번이다.
        for name, kind, width in zip(spec.columns[1:], spec.kinds, spec.widths):
            if kind == "str":
                if width is not None and frame.height:
                    self.clamped += int(
                        frame.select(
                            (pl.col(name).str.len_chars() > width).sum()
                        ).item() or 0)
            elif frame.height:
                self.nulled += int(
                    frame.select(
                        (pl.col(name).str.strip_chars() == "").sum()
                    ).item() or 0)

        frame = frame.with_columns(exprs).with_columns(
            # 분 귀속 컬럼. `datetime` 이고 **마이크로초 해상도**다 — 분으로
            # 내린 값만 들어가지만, 읽는 쪽이 `[lo, hi)` 비교를 하므로
            # 해상도가 달라지면 경계에서 어긋난다.
            pl.lit(ts).cast(pl.Datetime("us")).alias(UPLANE_SLOT)
        ).select(spec.columns)
        return raw_parquet.write_minute(self.uplane_root, series, ts, frame)

    def write_uplane_all(self, ts: datetime, batches: dict[str, list[list[str]]],
                         specs: dict[str, UplaneSpec]) -> dict[str, int]:
        """계열별 적재를 한 번에. 명세가 없는 계열은 **건너뛴다.**

        `uplane_specs` 가 꺼 둔 계열을 명세에서 빼므로, 여기서 그 계열의
        레코드는 버려진다. 생성은 했는데 적재되지 않는 상태를 조용히 두면
        "왜 비어 있나"를 추적할 수 없으니 호출부가 반환값으로 센다.

        **쓴 뒤에 보존 정리를 돌린다.** 10분치만 남긴다(사람 지시,
        2026-10-14) — 집계 여부를 보지 않는 이유는 `raw_parquet.purge`
        머리말에 있다. 지우는 주체를 쓰는 쪽에 둔 것은 그쪽이 "방금 어느 분을
        썼는가"를 알기 때문이다.
        """
        out = {
            s: self.write_uplane(ts, s, rows, specs[s])
            for s, rows in batches.items() if s in specs
        }
        if self.uplane_retain_minutes > 0:
            for series in out:
                raw_parquet.purge(self.uplane_root, series,
                                     self.uplane_retain_minutes)
        return out

    def last_event_time(self, series: str = CPLANE_SERIES) -> datetime | None:
        """마지막으로 쓴 분. 없으면 ``None`` — **epoch 로 떨어지지 않는다.**

        구 구현은 `SELECT MAX(EVENT_TIME)` 이었다. 원시가 파일로 나가면서
        디렉터리를 훑는다 — 파일 이름이 분이고, 이름이 있으면 그 분은 완성된
        것이다(근거는 `raw_parquet.minutes` 머리말).

        인자 이름이 `table` 에서 `series` 로 바뀌었다. **기본값으로 받던
        호출부는 그대로 돈다**(실제로 전부 기본값이다).
        """
        return raw_parquet.last_minute(self.cplane_root, series)


# --------------------------------------------------------------------------
# 1분 집계 — **원시는 Parquet, 산출물은 MySQL**
#
# 전에는 `INSERT INTO PM_* SELECT ... FROM GP_RAW_LTE_CALL_KPI` 로 MySQL 이
# 혼자 했다. 원시가 DB 를 떠나면서(2026-10-14) 집계 엔진을 polars 로 옮겼다 —
# graphio 쪽 `metrics/uplane_agg.py` 가 U-Plane 에서 먼저 밟은 길이고 같은
# 모양이다.
#
# **산출물은 그대로 MySQL 이다.** 이 세 테이블은 상용 `PM_*` 를 재현하는
# 것이고, 운영에서는 다른 주체가 넣어 준다 — PoC 가 그 자리를 대신 메운다.
# 원칙은 "MySQL 에는 통계만 올라간다".
#
# ## 조건식이 SQL 문자열에서 `pl.Expr` 로 바뀌었다
#
# Parquet 컬럼이 **여전히 전부 문자열**이므로(`CPLANE_FIELDS` 주석) 판정은
# 구 SQL 과 같다 — 플래그는 `= '1'`, "없음"은 센티널 `'0'`. 달라진 것은
# 표현형뿐이다. 식을 손으로 다시 쓰지 않고 헬퍼를 한 벌 두고 재사용한다.
# --------------------------------------------------------------------------

#: eNB 단위 대표 속성. CL_CELL_MAP 은 (ENB_ID, CELLNUM) 단위라 eNB 하나에
#: 여러 행이 있다. C_UID·MME_GRP_ID 는 eNB 당 유일(215/215 실측)하지만
#: EMS_ID 는 셀마다 다를 수 있어(855쌍) 대표값 하나를 고른다.
#:
#: **여전히 MySQL 질의다.** 마스터는 원시가 아니라 다른 주체가 관리하는
#: 테이블이고 파일로 나가지 않았다. 결과(약 3.2만 행)를 polars 로 받아
#: 조인한다 — LEFT JOIN 의 의미를 그대로 보존한다.
#: 테이블 이름은 `topo.table_name` 에서 받는다 — `CL_CELL_MAP` 은 로케일
#: 축이 없는 한 벌이고, 그 사실을 여기서 문자열로 다시 적으면 축이 두 벌이
#: 된다(한쪽만 고쳐질 때 조용히 갈라진다).
_ENB_MASTER_SQL = (
    "SELECT ENB_ID, MIN(C_UID) AS C_UID, MIN(MME_GRP_ID) AS MME_GRP_ID, "
    "       MIN(EMS_ID) AS EMS_ID "
    f"  FROM `{table_name('cellmap', 'kr')}` "
    "WHERE ENB_ID IS NOT NULL GROUP BY ENB_ID"
)

#: MySQL 에 한 번에 보낼 행 수. 너무 크면 `max_allowed_packet` 에 걸린다.
_AGG_BATCH = 2000


def _s(col: str) -> pl.Expr:
    return pl.col(col)


def _p_flag(col: str) -> pl.Expr:
    """플래그 1건. 상용 플래그는 `varchar(1)` 의 `'1'`/`'0'` 이다.

    구 `_flag()` 의 `COUNT(CASE WHEN x.col='1' THEN 1 END)` 와 같은 판정이다.
    `SUM(CASE..)` 를 쓰지 않은 이유(0행 그룹에서 NULL)는 polars 에서는
    해당되지 않지만, **값이 같아야 하므로 식 자체를 바꾸지 않는다.**
    """
    return pl.when(_s(col) == "1").then(1).otherwise(0)


def _p_drop() -> pl.Expr:
    """절단 — `DROP_FLAG` 가 섰고 해제 Cause 가 정상 종료가 **아닌** 것만.

    정상 종료를 절단으로 세면 이동이 잦은 지역이 상시 붉어진다(불변규칙 6).
    구 `_DROP` 과 같은 식이다.
    """
    return pl.when(
        (_s("DROP_FLAG") == "1")
        & ~_s("UECONTEXTRELEASE_CAUSE").is_in(list(NORMAL_RELEASE))
    ).then(1).otherwise(0)


def _p_end_only(lf: pl.LazyFrame) -> pl.LazyFrame:
    """완결 호만 — `ONGOINGFLAG = '2'`.

    **문자열로 비교한다.** Parquet 컬럼이 문자열이라 숫자로 비교하면 전량이
    탈락하고 집계가 0행이 된다 — 에러가 나지 않는 종류다(graphio
    `uplane_agg._counted` 가 반대 방향으로 같은 함정을 적어 두고 있다).
    """
    return lf.filter(_s("ONGOINGFLAG") == ONGOING_END)


def _p_derived() -> list[pl.Expr]:
    """`PM_*` 관례의 유도 컬럼. `EVENT_TIME` 에서 나온다 — 저장하는 값이지
    산식이 아니다. 구 SQL 의 `DATE_FORMAT` 세 개와 같다."""
    t = pl.col(CPLANE_SLOT)
    return [
        t.dt.strftime("%Y%m%d").alias("CDATE"),
        t.dt.strftime("%H").alias("CHOUR"),
        t.dt.strftime("%H%M").alias("CTIME"),
    ]


#: `_enb_master` 캐시 (2026-10-29). 전에는 1분마다 두 번(`aggregate_enb` ·
#: `aggregate_enb_cause`) `CL_CELL_MAP` 을 GROUP BY 로 다시 읽었다.
#:
#: **프로세스당 한 번 읽는다.** 생성기는 셀 목록을 기동 때 한 번 읽으므로
#: (`load_cells`) 마스터를 고쳐도 재기동 전에는 그 셀의 트래픽이 생기지 않는다
#: — 집계 쪽만 매분 새로 읽을 이유가 없다. 다시 읽는 것은 **처음 보는 eNB 가
#: 데이터에 나타났을 때**뿐이다.
#:
#: `absent` 는 다시 읽고도 마스터에 없던 eNB 다. 마스터에 없는 eNB 는 정상
#: 경로이고(LEFT JOIN 인 이유 · 불변규칙 12) 그것을 "처음 본다"로 세면 매분
#: 다시 읽게 되어 캐시가 무의미해진다.
_ENB_CACHE: dict = {"frame": None, "known": frozenset(), "absent": set()}


def _enb_master(conn, need: Iterable[str] = ()) -> pl.DataFrame:
    """eNB 대표 속성. ``need`` 는 이번 집계에 나온 eNB — 모르는 것이 있으면 다시 읽는다."""
    c = _ENB_CACHE
    unseen = {e for e in need if e is not None} - c["known"] - c["absent"]
    if c["frame"] is None or unseen:
        c["frame"] = _load_enb_master(conn)
        c["known"] = frozenset(c["frame"]["ENB_ID"].to_list())
        c["absent"] |= unseen - c["known"]
    return c["frame"]


def _load_enb_master(conn) -> pl.DataFrame:
    """eNB 대표 속성을 polars 프레임으로. 조인 키는 **평문 문자열**이다."""
    with conn.cursor() as cur:
        cur.execute(_ENB_MASTER_SQL)
        rows = cur.fetchall()
    schema = {"ENB_ID": pl.String, "C_UID": pl.String,
              "MME_GRP_ID": pl.String, "EMS_ID": pl.String}
    if not rows:
        return pl.DataFrame(schema=schema)
    # 값이 숫자로 올 수 있다(`C_UID` 가 int 인 스키마). 전부 문자열로 세운다 —
    # MySQL 에 되넣을 때 varchar 컬럼이라 표기가 같아야 한다.
    return pl.DataFrame(
        [[None if v is None else str(v) for v in r] for r in rows],
        schema=schema, orient="row")


def _scan_cplane(root: Path | None, lo: datetime,
                 hi: datetime) -> pl.LazyFrame | None:
    """`[lo, hi)` 구간의 C-Plane 원시. 파일이 없으면 ``None``.

    **``None`` 과 0행은 다른 사실이다** — 파일이 없으면 그 구간에 원천이 안 온
    것이고, 0행이면 원천은 왔는데 레코드가 없는 것이다(불변규칙 17).

    파일 선택은 프루닝이고 구간 판정은 **데이터가 한다** — 이름과 내용이
    어긋나도 집계가 조용히 틀리지 않게 `EVENT_TIME` 으로 한 번 더 거른다
    (graphio `raw_parquet.scan` 과 같은 태도).
    """
    if root is None:
        return None
    mins = raw_parquet.minutes(root, CPLANE_SERIES, lo, hi)
    if not mins:
        return None
    files = [raw_parquet.minute_path(root, CPLANE_SERIES, m) for m in mins]
    col = pl.col(CPLANE_SLOT)
    return (pl.scan_parquet(files)
              .filter((col >= pl.lit(lo)) & (col < pl.lit(hi))))


def _wipe_insert(conn, table: str, time_col: str, cols: list[str],
                 frame: pl.DataFrame, lo: datetime, hi: datetime) -> int:
    """`[lo, hi)` 를 지우고 다시 넣는다 — 구 `DELETE` + `INSERT` 와 같다.

    **범위 DELETE 다.** U-Plane 쪽(`uplane_agg._wipe`)이 "파일이 있는 분만"
    지우는 것과 다른데, 이유는 보관 기간이다 — 거기는 원시가 10분뿐이라
    범위를 통째로 지우면 되살릴 수 없는 과거 집계가 날아간다. 여기는 호출부가
    항상 **방금 쓴 분**(또는 백필 구간)만 넘기므로 범위와 재생 가능 구간이
    같다.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"DELETE FROM `{table}` WHERE {time_col} >= %s AND {time_col} < %s",
            [lo, hi])
        n = 0
        if frame.height:
            names = ",".join(f"`{c}`" for c in cols)
            marks = ",".join(["%s"] * len(cols))
            sql = f"INSERT INTO `{table}` ({names}) VALUES ({marks})"
            rows = frame.select(cols).rows()
            for i in range(0, len(rows), _AGG_BATCH):
                cur.executemany(sql, rows[i:i + _AGG_BATCH])
            n = len(rows)
    conn.commit()
    return n


def aggregate_enb(conn, lo: datetime, hi: datetime,
                  cplane_root: Path | None = None) -> int:
    """C-Plane Parquet → `PM_ENB_KPI_1M`.

    키는 (EVENT_TIME, FIRST_ENB_ID, CALL_TYPE) 다. CALL_TYPE 을 섞으면 트래픽
    구성비가 바뀔 때 품질이 그대로여도 소통률이 움직인다(불변규칙 3).

    비율은 저장하지 않는다 — 분자·분모만 담고 상위 계층이 `SUM()` 후 나눈다
    (불변규칙 2). 레코드가 없는 (분, eNB) 조합은 **행을 만들지 않는다**
    (불변규칙 4 — 결측을 0으로 채우지 않는다).

    채우지 못한 컬럼은 NULL 로 둔다. 모름을 0으로 적으면 운영자가 무해하다는
    뜻으로 읽는다(불변규칙 17):
      · BTS_NAME        — CM_CELL_REP_RU_LOC.RU_NAME 의 한글이 깨져 있다
      · COMPANY/REGION/BRANCH/OPTEAM/PART_ID — eNB→조직 매핑 테이블이 없다
      · MNO · EX_*      — 상용 산출 정의를 확인하지 못했다 (README 참조)

    ## `UNIQ_IMSI_CNT` 가 MySQL 과 갈릴 수 있고, polars 가 맞다

    구 `COUNT(DISTINCT x.IMSI)` 는 컬럼 collation(`utf8mb3_general_ci`)을
    따라 **대소문자를 무시**했다. polars 는 바이트 비교다 — 운영에서 IMSI 가
    암호문으로 오면 `A` 와 `a` 는 다른 가입자이므로 이쪽이 옳다(불변규칙 14).
    로컬 합성 평문에서는 실측 차이가 0 이다(distinct 20,000 양쪽 동일).
    """
    lf = _scan_cplane(cplane_root, lo, hi)
    cols = ["EVENT_TIME", "CDATE", "CHOUR", "CTIME", "ENB_ID", "CALL_TYPE",
            "C_UID", "MME_GRP_ID", "EMS_ID",
            "ATTEMPT_CNT", "SUCCESS_CNT", "DROP_CNT",
            "DATA_ATTEMPT_CNT", "DATA_SUCESS_CNT",
            "IMS_ATTEMPT_CNT", "IMS_SUCESS_CNT",
            "UNIQ_IMSI_CNT", "DETACH_CNT"]
    if lf is None:
        # 원천이 없다. **지우기만 하고 0 을 넣지 않는다** — 0 행을 쓰면
        # "값이 0" 과 "원천이 없음" 이 구분되지 않는다(불변규칙 17).
        return _wipe_insert(conn, PM_ENB, "EVENT_TIME", cols,
                            pl.DataFrame(), lo, hi)
    enb = _s("FIRST_ENB_ID")
    frame = (
        _p_end_only(lf)
        # 노드·셀 ID 가 없는 레코드는 그 축에 계상할 수 없다. 0 으로 채우면
        # 존재하지 않는 eNB 0 번의 장애가 만들어진다(불변규칙 4).
        .filter(enb.is_not_null() & (enb != ""))
        .group_by([CPLANE_SLOT, "FIRST_ENB_ID", "CALL_TYPE"])
        .agg([
            _p_flag("ATTEMPT_FLAG").sum().alias("ATTEMPT_CNT"),
            _p_flag("SUCCESS_FLAG").sum().alias("SUCCESS_CNT"),
            _p_drop().sum().alias("DROP_CNT"),
            _p_flag("DATA_ATTEMPT_FLAG").sum().alias("DATA_ATTEMPT_CNT"),
            _p_flag("DATA_SUCCESS_FLAG").sum().alias("DATA_SUCESS_CNT"),
            _p_flag("IMS_ATTEMPT_FLAG").sum().alias("IMS_ATTEMPT_CNT"),
            _p_flag("IMS_SUCCESS_FLAG").sum().alias("IMS_SUCESS_CNT"),
            # `COUNT(DISTINCT)` 는 NULL 을 세지 않는다. polars `n_unique()` 는
            # NULL 을 하나의 값으로 세므로 먼저 버린다.
            _s("IMSI").drop_nulls().n_unique().alias("UNIQ_IMSI_CNT"),
            _p_flag("DETACH_FLAG").sum().alias("DETACH_CNT"),
        ])
        .rename({"FIRST_ENB_ID": "ENB_ID"})
        .with_columns(_p_derived())
        .collect()
    )
    # LEFT JOIN 이다 — 마스터에 없는 eNB 도 행이 남아야 한다. `how="left"` 가
    # 그 의미이고, 없으면 그 eNB 의 트래픽이 통째로 사라진다(불변규칙 12).
    frame = frame.join(_enb_master(conn, frame["ENB_ID"].unique().to_list()),
                       on="ENB_ID", how="left")
    return _wipe_insert(conn, PM_ENB, "EVENT_TIME", cols, frame, lo, hi)


def aggregate_enb_cause(conn, lo: datetime, hi: datetime,
                        cplane_root: Path | None = None) -> int:
    """C-Plane Parquet → `PM_ENB_CAUSE_1M`.

    CAUSE 표기는 CAUSE_DEF·CD_CAUSE 를 따른다 — `C_<IF_PROTO 2자리><Cause 6자리>`
    (실측: `C_02000504` = S1MME_S1AP + Cause 504).

    **First Error 구간이 원인이고 하류 Cause 는 증상이다**(불변규칙 7). 그래서
    First Error 쪽을 주로 담고, S1AP 해제 Cause 는 정상 종료를 뺀 비정상만
    따로 담는다. 두 갈래를 구 SQL 이 `UNION ALL` 로 합쳤고 여기서는
    `pl.concat` 이다.

    `LPAD` 의 polars 판이 `str.pad_start` 다. **`'0'` 센티널을 걸러낸 뒤**
    채우므로 `C_02000000` 같은 가짜 코드가 생기지 않는다 — 구 SQL 의
    `NOT IN ('', '0')` 조건과 같은 자리다.
    """
    cols = ["EVENT_TIME", "CDATE", "CHOUR", "CTIME", "ENB_ID", "CALL_TYPE",
            "C_UID", "MME_GRP_ID", "EMS_ID", "CAUSE_TYPE", "CAUSE", "CAUSE_CNT"]
    lf = _scan_cplane(cplane_root, lo, hi)
    if lf is None:
        return _wipe_insert(conn, PM_ENB_CAUSE, "EVENT_TIME", cols,
                            pl.DataFrame(), lo, hi)
    enb = _s("FIRST_ENB_ID")
    base = _p_end_only(lf).filter(enb.is_not_null() & (enb != ""))

    def code(proto: pl.Expr, cause: pl.Expr) -> pl.Expr:
        """`C_<proto 2자리><cause 6자리>`. 구 `CONCAT('C_', LPAD(..), LPAD(..))`."""
        return pl.concat_str([
            pl.lit("C_"),
            proto.str.pad_start(2, "0"),
            cause.str.pad_start(6, "0"),
        ])

    # ① First Error 갈래. `IF_PROTO_NAME` 으로 CAUSE_TYPE 을 붙이고, 표에 없는
    #    구간은 `'NONE'` 이다 — 구 SQL 의 `CASE ... ELSE 'NONE'` 과 같다.
    proto = _s("FIRST_ERROR_INTERFACE_PROTOCOL")
    ctype = proto
    for k, v in IF_PROTO_NAME.items():
        ctype = pl.when(proto == k).then(pl.lit(v)).otherwise(ctype)
    first = (
        base.filter(~_s("FIRST_ERROR_CAUSE").is_in(["", NONE_CODE]))
            .with_columns([
                pl.when(proto.is_in(list(IF_PROTO_NAME)))
                  .then(ctype).otherwise(pl.lit("NONE")).alias("CAUSE_TYPE"),
                code(proto, _s("FIRST_ERROR_CAUSE")).alias("CAUSE"),
            ])
    )
    # ② S1AP 해제 Cause 갈래 — 정상 종료 14종은 뺀다(불변규칙 6).
    rel = (
        base.filter(~_s("UECONTEXTRELEASE_CAUSE")
                    .is_in(["", NONE_CODE, *NORMAL_RELEASE]))
            .with_columns([
                pl.lit("S1AP").alias("CAUSE_TYPE"),
                code(pl.lit("02"), _s("UECONTEXTRELEASE_CAUSE")).alias("CAUSE"),
            ])
    )
    keys = [CPLANE_SLOT, "FIRST_ENB_ID", "CALL_TYPE", "CAUSE_TYPE", "CAUSE"]
    frame = (
        pl.concat([first.select(keys), rel.select(keys)])
          .group_by(keys)
          .agg(pl.len().alias("CAUSE_CNT"))
          .rename({"FIRST_ENB_ID": "ENB_ID"})
          .with_columns(_p_derived())
          .collect()
    )
    frame = frame.join(_enb_master(conn, frame["ENB_ID"].unique().to_list()),
                       on="ENB_ID", how="left")
    return _wipe_insert(conn, PM_ENB_CAUSE, "EVENT_TIME", cols, frame, lo, hi)


def aggregate_epc(conn, lo: datetime, hi: datetime,
                  cplane_root: Path | None = None) -> int:
    """C-Plane Parquet → `PM_EPC_KPI_1M_ORG` (MME/SGW/PGW 노드 축).

    한 레코드가 세 노드에 **모두** 계상된다 — 노드마다 따로 보는 지표이므로
    중복이 아니다. 그래서 이 테이블의 `ATTEMPT_CNT` 총합은 eNB 테이블의 약
    3배가 된다. **이것을 롤업에서 더하면 안 된다.**

    MCC/MNC/MNO 는 가입자 차원이라 노드·분 단위 집계에 단일 값이 없다. 임의로
    고르면 없는 사실을 만들므로 NULL 로 둔다(불변규칙 17).
    """
    cols = ["EVENT_TIME", "CDATE", "CHOUR", "CTIME", "NODE_TYPE", "NODE_ID",
            "CALL_TYPE", "ATTEMPT_CNT", "SUCCESS_CNT", "DROP_CNT",
            "DATA_ATTEMPT_CNT", "DATA_SUCESS_CNT",
            "IMS_ATTEMPT_CNT", "IMS_SUCESS_CNT",
            "UNIQ_IMSI_CNT", "DETACH_CNT"]
    lf = _scan_cplane(cplane_root, lo, hi)
    if lf is None:
        return _wipe_insert(conn, PM_EPC, "EVENT_TIME", cols,
                            pl.DataFrame(), lo, hi)
    base = _p_end_only(lf)
    legs = []
    for kind, col in (("MME", "MME_ID"), ("SGW", "SGW_ID"), ("PGW", "PGW_ID")):
        c = _s(col)
        legs.append(
            base.filter(c.is_not_null() & (c != ""))
                .with_columns([pl.lit(kind).alias("NODE_TYPE"),
                               c.alias("NODE_ID")])
                .group_by([CPLANE_SLOT, "NODE_TYPE", "NODE_ID", "CALL_TYPE"])
                .agg([
                    _p_flag("ATTEMPT_FLAG").sum().alias("ATTEMPT_CNT"),
                    _p_flag("SUCCESS_FLAG").sum().alias("SUCCESS_CNT"),
                    _p_drop().sum().alias("DROP_CNT"),
                    _p_flag("DATA_ATTEMPT_FLAG").sum().alias("DATA_ATTEMPT_CNT"),
                    _p_flag("DATA_SUCCESS_FLAG").sum().alias("DATA_SUCESS_CNT"),
                    _p_flag("IMS_ATTEMPT_FLAG").sum().alias("IMS_ATTEMPT_CNT"),
                    _p_flag("IMS_SUCCESS_FLAG").sum().alias("IMS_SUCESS_CNT"),
                    _s("IMSI").drop_nulls().n_unique().alias("UNIQ_IMSI_CNT"),
                    _p_flag("DETACH_FLAG").sum().alias("DETACH_CNT"),
                ])
        )
    # 세 축을 한 프레임으로 모은다. `NODE_TYPE` 이 키에 있으므로 섞이지
    # 않는다 — 축별로 따로 INSERT 하면 멱등 삭제 범위를 세 번 계산해야 한다.
    frame = pl.concat(legs).with_columns(_p_derived()).collect()
    return _wipe_insert(conn, PM_EPC, "EVENT_TIME", cols, frame, lo, hi)


def aggregate_all(conn, lo: datetime, hi: datetime,
                  cplane_root: Path | None = None) -> dict[str, int]:
    """[lo, hi) 구간의 1분 집계 전부. 구간 멱등이다.

    ``cplane_root`` 가 ``None`` 이면 세 테이블의 그 구간을 **비우기만** 한다 —
    원천이 없는 것을 0 으로 적지 않는다(불변규칙 17).
    """
    return {
        PM_ENB: aggregate_enb(conn, lo, hi, cplane_root),
        PM_ENB_CAUSE: aggregate_enb_cause(conn, lo, hi, cplane_root),
        PM_EPC: aggregate_epc(conn, lo, hi, cplane_root),
    }

# --------------------------------------------------------------------------
# 보존 정리 — 파티션 단위로 버린다
# --------------------------------------------------------------------------

#: 생성기가 **쓰는** 테이블과 그 시각 컬럼 — 보존 정리의 대상이다.
#:
#: **기준은 접두가 아니라 쓰기 주체다.** `GP_RAW_LTE_CALL_KPI` 는 이름이 `GP_`
#: 지만 **생성기가 쓰는 원시 XDR** 이므로 여기 들어간다. 빼면 두 가지가 같이
#: 깨진다 — 디스크가 무한히 자라고, `ensure_partitions` 가 앞을 늘려 주지
#: 않아 범위를 벗어난 분이 전부 `pmax` 한 칸에 몰려 프루닝이 죽는다(에러 없이
#: 느려진다).
#:
#: **원시 xDR 4종이 전부 여기 없다**(2026-10-14). U-Plane 3종이 먼저,
#: C-Plane(`lte_call`)이 뒤이어 MySQL 을 떠나 로컬 Parquet 가 됐다. 보존은
#: 파티션 드롭이 아니라 파일 삭제다(`raw_parquet.purge` — U-Plane 10분 ·
#: C-Plane 1440분). 그래서 **남은 것은 1분 집계 세 테이블뿐이고**, 이름은
#: `WRITTEN_TABLES` 그대로 둔다(쓰기 주체 기준이라는 뜻이 변하지 않았다).
#:
#: **`GP_KPI_*`·`GP_INCIDENT` 류 분석 산출물은 넣지 않는다.** 그것은 graphio 가
#: 쓰고 (`graphio/store/tables.py:RESETTABLE_TABLES`), 남의 산출물을 생성기가
#: 지우면 두 주체가 같은 행을 다툰다. graphio 가 Incident 를 쓰는 중에 생성기가
#: 그 파티션을 떨어뜨리면 분석 결과만 사라지고 에러는 나지 않는다 — 운영자
#: 화면에서는 "장애 없음"으로 보인다(불변규칙 17 이 금지하는 형태).
#: `CL_*`·`CM_*` 마스터도 대상이 아니다 — 다른 주체가 관리하고 시각 컬럼이 없다.
WRITTEN_TABLES: tuple[tuple[str, str], ...] = (
    (PM_ENB, "EVENT_TIME"),
    (PM_ENB_CAUSE, "EVENT_TIME"),
    (PM_EPC, "EVENT_TIME"),
)


#: 시간 파티션의 보관 시간. **지금 이 입도의 테이블이 없다**(2026-10-14).
#:
#: 원시 xDR 4종만 시간 파티션이었고 전부 로컬 Parquet 로 나갔다. 남은
#: `PM_*` 셋은 일 파티션이라 `retain_days` 를 따른다.
#:
#: **상수를 지우지 않는 이유**: `purge_partitions` 가 입도별로 분기하고
#: (`_GRAIN_BY_LEN`) 호출부가 이 값을 로그에 찍는다. 시간 파티션 테이블이
#: 다시 생기면 그때 쓸 값이고, 지우면 분기 자체가 없어져 그런 테이블이
#: 생겼을 때 **아무것도 지우지 않는다** — 에러 없이 디스크가 자란다.
#:
#: C-Plane Parquet 보관(`CPLANE_RETAIN_MINUTES` = 1440분)이 이 값과 같은
#: 창인 것은 **일부러 맞춘 것이다** — 저장소를 바꾼 것이 조회 가능 범위를
#: 조용히 줄이지 않게 하려는 것이다.
RETAIN_HOURS = 24

#: 끊을 수 있는 계열. **`UPLANE_SERIES` + `lte_call` 이다.**
#:
#: 목록을 따로 적지 않고 거기서 유도한다 — 계열이 늘 때 한쪽만 고쳐지면
#: "끊었는데 계속 나온다"가 되고, 에러는 나지 않는다.
#:
#: `lte_call` 을 넣는 이유는 **그것만 끊는 상황이 실제로 있다** — 상용에서
#: C-Plane 프로브와 U-Plane 프로브가 따로 붙으므로 한쪽만 미연동일 수 있다.
#: 다만 여기서는 그 역이 성립하지 않는다: 호가 없으면 세션이 없으므로
#: `lte_call` 차단은 U-Plane 까지 멈춘다(`PemdbGen.set_blocked`).
BLOCKABLE_SERIES: tuple[str, ...] = ("lte_call",) + tuple(UPLANE_SERIES)

#: 파티션 이름 → 입도. **이름 길이로 판정한다.**
#:
#: `pYYYYMMDD`(9자)는 일, `pYYYYMMDDHH`(11자)는 시간이다. 입도를 코드에
#: 적어 두지 않는 이유: 테이블마다 다르고(원시 4종은 시간, `PM_*` 는 일),
#: DDL 을 바꿀 때 한쪽만 고쳐지면 **조용히 안 지워진다.** DB 가 정본이다.
_GRAIN_BY_LEN: dict[int, str] = {9: "day", 11: "hour"}

#: 입도별 파티션 이름 포맷. 이름은 그 구간의 **시작** 시각이고 상한은 +1단위다.
_GRAIN_FMT: dict[str, str] = {"day": "%Y%m%d", "hour": "%Y%m%d%H"}

#: 입도별 한 칸의 길이.
_GRAIN_STEP: dict[str, timedelta] = {
    "day": timedelta(days=1),
    "hour": timedelta(hours=1),
}


def _table_partitions(conn, database: str, table: str
                      ) -> tuple[str | None, list[tuple[str, datetime]]]:
    """`(입도, [(이름, 상한)])`. `pmax`(MAXVALUE)는 제외한다.

    입도는 파티션 **이름 길이**로 읽는다(`_GRAIN_BY_LEN`). 테이블이 섞여
    있기 때문이다 — 원시 4종은 시간 파티션이고 `PM_*` 는 일 파티션이다.
    이름이 규칙을 벗어나면 입도를 `None` 으로 돌려주고, 호출부는 그 테이블을
    건드리지 않는다(상용 원본이 다른 표기를 쓸 수 있다).

    `PARTITION_DESCRIPTION` 은 `'2026-10-14 00:00:00'`(따옴표 포함) 꼴이다 —
    RANGE COLUMNS 의 상한이고, 그 값 **미만**이 그 파티션에 들어간다.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT PARTITION_NAME, PARTITION_DESCRIPTION FROM information_schema.PARTITIONS "
            "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND PARTITION_NAME IS NOT NULL "
            "ORDER BY PARTITION_ORDINAL_POSITION",
            [database, table],
        )
        rows = cur.fetchall()
    out: list[tuple[str, datetime]] = []
    grains: set[str] = set()
    for name, desc in rows:
        text = (desc or "").strip().strip("'")
        if text.upper() == "MAXVALUE":
            continue
        g = _GRAIN_BY_LEN.get(len(name))
        if g is None or not name[1:].isdigit():
            continue        # 상용 원본의 다른 표기 — 손대지 않는다
        try:
            out.append((name, datetime.fromisoformat(text)))
        except ValueError:
            continue
        grains.add(g)
    # 한 테이블에 두 입도가 섞이면 판정할 수 없다. 건드리지 않는 쪽이 안전하다.
    grain = grains.pop() if len(grains) == 1 else None
    return grain, out


def ensure_partitions(conn, database: str, ahead_hours: int = 48,
                      ahead_days: int = 3) -> dict[str, list[str]]:
    """앞으로 쓸 파티션을 `pmax` 에서 떼어 미리 만든다. **입도별로 다르게.**

    **드롭만 하고 추가하지 않으면 파티션 체계가 조용히 죽는다.** 범위를 벗어난
    분은 전부 `pmax` 한 칸에 들어가고, 그러면 시각 범위 조건으로 프루닝이
    안 돼 질의마다 전체 스캔이 된다 — 에러는 나지 않고 느려질 뿐이라 알아채기
    어렵다.

    **생성기가 꺼져 있는 동안은 아무것도 하지 않아도 된다** — 쓰지 않으면
    담을 그릇도 필요 없다. 대신 **다시 켤 때 적재보다 먼저 돌아야 한다.**
    오래 쉬었으면 마지막 상한이 과거일 수 있고, 그 상태로 적재하면 그 분이
    `pmax` 로 흘러간다. 그래서 `want` 는 "오늘+N"이 아니라 **현재 시각 기준**
    이다 — 구 구현은 전자여서, 상한이 이미 더 뒤면 `new` 가 빈 리스트가 되고
    조용히 아무것도 하지 않았다.

    공백 구간은 따로 메우지 않는다. RANGE 파티션은 앞 칸의 상한 이상을 다음
    칸이 받으므로, 쉬는 동안의 빈 구간은 현재 칸이 흡수한다. 그 구간에 데이터가
    없으니 프루닝 효율만 미미하게 떨어진다.

    `pmax` 가 비어 있으면 REORGANIZE 는 메타데이터 변경에 가깝다(실측 수십 ms).
    """
    out: dict[str, list[str]] = {}
    now = datetime.now().replace(minute=0, second=0, microsecond=0)
    for table, _col in WRITTEN_TABLES:
        grain, parts = _table_partitions(conn, database, table)
        if grain is None or not parts:
            continue            # 파티션이 없거나 이름 규칙이 다르다
        step = _GRAIN_STEP[grain]
        fmt = _GRAIN_FMT[grain]
        if grain == "hour":
            base = now
            span = ahead_hours
        else:
            base = now.replace(hour=0)
            span = ahead_days
        have = {u for _n, u in parts}
        last = max(have)
        # **현재 칸부터** 센다. 쉬다 켠 경우 여기가 과거보다 뒤라 공백을 넘는다.
        want = [base + step * (i + 1) for i in range(span + 1)]
        new = [u for u in want if u not in have and u > last]
        if not new:
            continue
        spec = ", ".join(
            f"PARTITION p{(u - step):{fmt}} "
            f"VALUES LESS THAN ('{u:%Y-%m-%d %H:%M:%S}') ENGINE = InnoDB"
            for u in new)
        with conn.cursor() as cur:
            cur.execute(
                f"ALTER TABLE `{table}` REORGANIZE PARTITION pmax INTO "
                f"({spec}, PARTITION pmax VALUES LESS THAN (MAXVALUE) ENGINE = InnoDB)")
        out[table] = [f"p{(u - step):{fmt}}" for u in new]
    return out


def pmax_rows(conn, database: str) -> dict[str, int]:
    """`pmax` 에 쌓인 행 수. **0 이 아니면 파티션 관리가 멈춘 것이다.**

    범위를 벗어난 분이 거기 모이고, 그 상태는 에러를 내지 않는다 — 질의가
    느려질 뿐이다. 그래서 호출부가 이것을 보고 경고한다.
    """
    out: dict[str, int] = {}
    with conn.cursor() as cur:
        for table, _col in WRITTEN_TABLES:
            cur.execute(
                "SELECT TABLE_ROWS FROM information_schema.PARTITIONS "
                "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND PARTITION_NAME='pmax'",
                [database, table],
            )
            row = cur.fetchone()
            if not row or not row[0]:
                continue
            # 추정치라 정확한 수를 세려면 COUNT(*) 가 필요하다. 여기서는
            # "0 인가 아닌가"만 쓰므로 추정으로 충분하다 — 매 틱 전수 집계를
            # 하면 그게 더 비싸다.
            cur.execute(f"SELECT COUNT(*) FROM `{table}` PARTITION (pmax)")
            n = int(cur.fetchone()[0])
            if n:
                out[table] = n
    return out


def purge_partitions(conn, database: str, retain_hours: int = 24,
                     retain_days: int = 1) -> dict[str, int]:
    """보관 기간을 지난 파티션을 떨어뜨린다. **입도별로 다른 기간을 쓴다.**

    원시 xDR 4종은 시간 파티션이라 24시간 입도로 자른다. 일 파티션(`PM_*`)은
    `retain_days` 다.

    **이름을 문자열로 비교하지 않는다.** 구 구현은
    `name[1:] < cutoff.strftime('%Y%m%d')` 였고, 시간 파티션 이름은 10자라
    자릿수가 어긋나 비교가 틀렸다 — `'2026101400' < '20261013'` 이 거짓이
    되어 **에러 없이 아무것도 지우지 않았다.** 지금은 상한을 `datetime` 으로
    파싱해 비교한다.

    `DELETE` 를 쓰지 않는 이유는 비용이다. 분당 2만 호면 하루가 1,000만 행이
    넘고, 그만큼을 `DELETE` 로 지우면 모든 행이 undo log·바이너리 로그를 타며
    인덱스를 갱신한다. `DROP PARTITION` 은 테이블스페이스 파일을 지우는 일이라
    행 수와 거의 무관하다.

    **상한이 cutoff 이하인 칸만** 떨어뜨린다. 상한은 "그 값 미만"이 들어 있다는
    뜻이므로, 상한 ≤ cutoff 면 그 칸의 모든 행이 보관 기간 밖이다.
    """
    now = datetime.now()
    cut = {"hour": now - timedelta(hours=retain_hours),
           "day": now - timedelta(days=retain_days)}
    out: dict[str, int] = {}
    for table, _col in WRITTEN_TABLES:
        grain, parts = _table_partitions(conn, database, table)
        if grain is None or not parts:
            continue
        cutoff = cut[grain]
        drop = [n for n, upper in parts if upper <= cutoff]
        # **전부 떨어뜨리지 않는다.** 파티션이 하나도 없는 테이블은 RANGE
        # 정의가 `pmax` 만 남아, 그 뒤 모든 적재가 거기로 몰린다.
        if len(drop) >= len(parts):
            drop = drop[:-1]
        if not drop:
            continue
        with conn.cursor() as cur:
            for name in drop:
                cur.execute(f"ALTER TABLE `{table}` DROP PARTITION `{name}`")
        out[table] = len(drop)
    return out


# --------------------------------------------------------------------------
# 장애 주입 — 시나리오 배치와 정답(ground truth)
# --------------------------------------------------------------------------

#: pemdb1 에서 돌릴 수 없는 시나리오와 그 이유.
#: **억지로 만들지 않는다.** 원천 XDR 이 없는데 값을 지어내면 PoC 가 거짓말을
#: 한다 — 룰은 발동하고 채점은 올라가지만 운영에서는 아무것도 재현되지 않는다.
#: 미연동으로 남기고 보고하는 것이 불변규칙 17 의 취지다.
#: 지금은 비어 있다. **U-Plane 세 종(SC-05·06·07)이 2026-10-13 에 풀렸다** —
#: `GP_RAW_TRANSPORT`·`GP_RAW_DNS`·`GP_RAW_HTTP` 를 만들고 적재 경로를
#: 붙였다(`UPLANE_FIELDS` · `PemdbWriter.write_uplane`).
#:
#: 풀기 전에 **변조기가 건드리는 필드가 실제로 적재되는지** 대조했다. 규격
#: 415필드 중 55개만 적재하므로, 변조는 됐는데 그 컬럼이 안 실리면 **장애가
#: 주입되지 않고 채점만 0% 로 나온다** — 에러가 없는 종류다.
#:
#:   SC-05 `_dns_degradation`   6/7 적재 (`ResolvedIP` 만 빠짐)
#:   SC-06 `_external_server`   3/4 적재 (`HTTP_Total_Response_PayloadSize` 빠짐)
#:   SC-07 `_transport_loss`    4/4 적재
#:
#: 빠진 둘은 **1분 집계가 읽지 않는다**(`graphio/metrics/cell_agg.py` 의
#: `_u_cell_specs`). 판정 신호는 각각 `ReplyCode=2`/`ResponseTime=0` →
#: `DNS_FAIL`·`DNS_NORESP`, `StatusCode` 5xx → `HTTP_5XX`,
#: down 재전송·`DnlinkRTTTime` → `RETRANS_DN`·`RTT_DN_TIME` 이라 전부 살아 있다.
UNSUPPORTED: dict[str, str] = {}

#: 돌아가는 전부 — **22 시나리오 + 하위 변형 2 = 24**(2026-10-22, V3).
#:
#: 구 벤치마크에서 SC-05·SC-06 은 C-Plane 이벤트가 0건이어서 탐지 자체가
#: 불가능했고, 그것이 탐지율 저하가 아니라 **채점 상한**이었다. 지금은 24종
#: 전부가 Incident 를 만들 경로를 갖는다 — 신규 축 둘(`U-12 NODE`·D4)은
#: graphio 쪽에 함께 들어갔다.
#:
#: **순서가 배치 순서다**(`build_scenarios` 가 이 순서로 구간을 나눈다).
#: 하위 변형(`SC-03b` 등)을 원본 바로 뒤에 두어, 짧은 구간을 돌릴 때
#: 쌍 비교(V3 §10.3)의 양쪽이 함께 들어가거나 함께 빠지게 한다.
SUPPORTED: tuple[str, ...] = (
    "SC-01", "SC-02", "SC-03", "SC-03b", "SC-04", "SC-05",
    "SC-06", "SC-06b", "SC-07", "SC-08", "SC-09", "SC-10",
    "SC-11", "SC-12", "SC-13", "SC-13b", "SC-14", "SC-15",
    "SC-15b", "SC-16", "SC-17", "SC-18", "SC-19", "SC-20", "SC-21",
    # 일본 시연 시나리오. **접두를 `J-` 로 가른다** — `g-sc` 25종은
    # 기존 채점의 기준선이고(확정대장 §14.7) 번호를 이어 붙이면 둘이
    # 섞여 "어느 쪽 회귀인지" 를 가를 수 없다.
    "J-SC-14", "J-SC-15", "J-SC-16", "J-SC-21", "J-SC-22", "J-SC-23",
    "J-SC-25", "J-SC-27",
)


def build_scenarios(gen: "PemdbGen", minutes: int, selected: str = "all",
                    lead_minutes: int | None = None,
                    seed: int | None = None) -> tuple[list[Scenario], list[str]]:
    """시나리오를 구간에 배치한다. 반환은 (배치된 것, 건너뛴 사유).

    `scenarios.build_scenarios` 를 **그대로** 쓴다. 변형은 필드명 기반이고
    `fields.SERIES_FIELDS["lte_call"]` 과 상용 155컬럼은 위치가 1:1 이므로
    (`xdr_mapping` 이 검증한다) 포팅할 것이 없다. 바꿔야 했던 것은 노드 ID
    표기 비교 하나였다(`scenarios._node_eq`).

    대상 선정은 **셀 마스터에 실린 실제 ID** 로 돈다. `load_cells` 가
    `Cell.sgw_id`·`mme_id` 에 `CL_PGW`·`CL_MME` 의 0채움 varchar 를 넣으므로
    `Target(sgw_id='0037')` 이 되고, `as_truth()` 의 `expect_node` 가
    `"SGW-0037"` 로 나와 graphio `GP_INCIDENT.DETAIL.axis_ids` 와 글자까지
    같아진다 (`detector._node_key` = `node_type || '-' || node_id`).
    """
    # **대문자로 올리지 않는다.** 하위 변형 ID 는 `SC-03b` 처럼 소문자 접미가
    # 붙어 있어서 `.upper()` 를 걸면 `SC-03B` 가 되고 `SUPPORTED` 와 어긋난다 —
    # 교집합에서 조용히 빠져 "구간이 짧아 배치되지 않았다"로 보고된다.
    _norm = {sid.upper(): sid for sid in SUPPORTED}
    wanted = set(SUPPORTED) if selected in ("all", None, "") else {
        _norm.get(t.strip().upper(), t.strip())
        for t in selected.split(",") if t.strip()}
    skipped = [f"{sid}: {why}" for sid, why in sorted(UNSUPPORTED.items())
               if sid in wanted]
    wanted &= set(SUPPORTED)
    if not wanted:
        return [], skipped

    base_seed = (seed if seed is not None else gen.seed) + 777
    scs = build_sim_scenarios(
        gen.cells, minutes, random.Random(base_seed),
        selected=sorted(wanted), lead_minutes=lead_minutes,
    )

    # ── 관측 불가한 셀을 뺀다 ───────────────────────────────────────────
    #
    # 셀 축 시나리오의 대상은 **`WEIGHT_FLOOR` 를 넘는 셀**만 쓴다. 근거는
    # 그 상수의 주석에 있다(창고 조회도 사전 모의도 쓰지 않는 이유 포함).
    #
    # **추첨 자체는 건드리지 않는다** — `_cells_in_one_sgg` 가 고른 뒤
    # 미달 셀만 교체한다. 후보 풀을 좁히면 시군구별 셀 수가 달라져
    # `rng.choice` 가 다른 시군구를 고르고, 정답 셀이 통째로 바뀐다
    # (실측으로 40개 중 유지 0개였다).
    pool = {c.cell_key for c in scenario_pool(gen.cells)}
    if pool and len(pool) < len(gen.cells):
        scs = build_sim_scenarios(
            gen.cells, minutes, random.Random(base_seed),
            selected=sorted(wanted), lead_minutes=lead_minutes,
            eligible=pool,
        )
    # **빠진 것을 정답 파일에 적는다.** 하한을 걸면 "분모가 얇은 셀에서
    # 탐지가 서는가"가 측정 대상에서 사라지고, 그러면 탐지율 100% 는
    # *"분모가 충분할 때의 100%"* 를 뜻한다. 재지 못한 것을 수치처럼
    # 읽히게 두지 않는다(불변규칙 17). graphio `score` 가 이 값을 읽어
    # 표 앞 경고로 낸다 — **문구에 숫자를 박지 않으려고 값을 넘긴다.**
    floored = sorted({sc.expect_kpi for sc in scs
                      if sc.scope_kind == "CELL"
                      and sc.expect_kpi in PACKET_DENOM_KPIS})
    gen.cell_selection = {
        "weight_floor": WEIGHT_FLOOR,
        "applies_to_kpi": floored,
        "cells_total": len(gen.cells),
        "cells_eligible": len(pool),
    }
    skipped.append(f"셀 축 대상 후보 — 셀 {len(gen.cells)} 중 {len(pool)}개 "
                   f"(traffic_weight >= {WEIGHT_FLOOR}) · 적용 지표 "
                   f"{', '.join(floored) or '없음'}")

    placed = {sc.id for sc in scs}
    skipped += [f"{sid}: 구간이 짧아 배치되지 않았다 (minutes={minutes})"
                for sid in sorted(wanted - placed)]
    gen.set_faults(scs)
    return scs, skipped


def write_truth(path: Path, start: datetime, minutes: int,
                scenarios: list[Scenario],
                cell_selection: dict | None = None) -> dict:
    """`ground_truth.json` — graphio `pipeline.score_against_truth` 가 읽는다.

    포맷을 바꾸지 않는다. 바뀐 것은 **셀 키의 내용**이다 — 구 파일은 자체
    난수 base64 암호문(`'SRmZzsHOGQXR6XVpRFPM2A==-1'`)이었고 지금은 상용
    평문 `"{ENB_ID}-{CELLNUM}"`(`'157837-21'`)이다. graphio 쪽
    `store/tables.py:cell_key()` 와 같은 모양이어야 `GP_INCIDENT_SCOPE` 와
    교집합이 잡힌다. 모양이 어긋나면 **에러 없이 0%** 가 나온다.

    생성과 같은 호출에서 쓴다. 따로 돌리면 시나리오 배치에 쓰인 rng 상태가
    달라져 셀 집합이 바뀌고, 정답이 데이터와 다른 세대가 된다.
    """
    doc = {
        "start": start.isoformat(),
        "minutes": minutes,
        # **채점 문항이 아닌 재현은 넣지 않는다**(`Scenario.scored` ·
        # `j-sc-24` 증설). 넣으면 "맞혀야 할 원인"이 빈 문자열인 문항이
        # 생기고, 그 창의 A 혼잡을 RCA 가 맞혀도 오답으로 센다.
        "scenarios": [sc.as_truth() for sc in scenarios if sc.scored],
        # 미연동을 정답에 넣지 않는다. 넣으면 분모가 13이 되어 탐지하지
        # 못한 것이 아니라 **원천이 없는 것**까지 실패로 세게 된다.
        "unsupported": UNSUPPORTED,
    }
    # **대상 셀을 어떻게 골랐는지 함께 적는다.** 없으면 채점이 "탐지율
    # 100%" 를 무조건으로 읽는다 — 하한을 걸었으면 그것은 *"분모가 충분할
    # 때의 100%"* 다. graphio `pipeline._truth_diagnostics` 가 이 블록을
    # 읽어 경고 한 줄로 낸다. **값을 넘기므로 그쪽 문구에 숫자가 없다.**
    if cell_selection:
        doc["cell_selection"] = dict(cell_selection)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    return doc
