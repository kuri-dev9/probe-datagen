"""상용 스키마(pemdb1) 어댑터 — 생성한 xDR 을 상용 테이블에 그대로 적재한다.

**상용이 정본이다.** 자체 창고 스키마(`store.py` · `aggregate.py`)를 거치지 않고
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
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from .cells import Cell
from .faults import resolve_active as resolve_active_faults
from .fields import SERIES_FIELDS
from .generator import Generator
from .identity import synth_imei, synth_imsi
from .profile import load as load_profiles
from .scenarios import MUTATORS, Scenario
from .scenarios import build_scenarios as build_sim_scenarios

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

#: C-Plane 원시 xDR. **`GP_RAW_LTE_CALL_KPI` 다**(2026-10-14) — `pemdb6` 가
#: 정본이 되면서 원시 네 종을 `GP_RAW_*` 한 묶음으로 묶었다. 구조는 상용
#: 테이블을 그대로 옮겼고(154컬럼·타입 동일) 파티션만 **시간 단위**다.
XDR_TABLE = "GP_RAW_LTE_CALL_KPI"
PM_ENB = "PM_ENB_KPI_1M"
PM_ENB_CAUSE = "PM_ENB_CAUSE_1M"
PM_EPC = "PM_EPC_KPI_1M_ORG"

#: U-Plane 원시 XDR — **상용에 없다. PoC 가 만든 테이블이다.**
#: 접두를 `GP_` 로 둔 이유: 상용 이름(`PM_APP_*`·`PM_DNS_*`)을 쓰면 운영 전환
#: 때 "있는 줄 알고" 그 이름을 찾는다. 정본 DDL 은
#: `../graphio-probe-rca/docker/mysql/gp_tables.sql`, 요구 명세는 같은 저장소의
#: `docs/UPLANE-TABLE-SPEC.md` 다.
#: 이름이 `GP_RAW_*` 에서 바뀌었다(2026-10-14). 규격 문서의 `_SD`(Summary
#: Detail) 접미를 달고 있었는데, C-Plane 까지 네 종이 한 묶음이 되면서 "원시"
#: 라는 역할을 이름이 말하는 쪽을 골랐다 — 집계는 `GP_KPI_*_1M` 이다.
GP_XDR = {
    "transport": "GP_RAW_TRANSPORT",
    "dns": "GP_RAW_DNS",
    "http": "GP_RAW_HTTP",
}

#: 계열별 `(GP_ 컬럼, 규격 필드명)` — **이름으로 맞춘다.**
#:
#: `XDR_LTE_CALL_KPI` 는 154필드가 1:1 이라 위치로 맞췄지만(`xdr_mapping`),
#: 여기는 규격 415필드 중 55개만 뽑으므로 위치 대응이 아예 없다. 이름으로
#: 맞추고 **컬럼이 하나라도 빠지면 멈춘다**(`uplane_mapping`) — 조용히 빠지면
#: 그 카운터가 영원히 NULL 이고, NULL 은 "모름"으로 읽혀 KPI 가 산출 불가로
#: 내려간다. 에러가 나는 쪽이 낫다.
#:
#: `Traffic_Total_*` 만 이름을 줄였다. 그대로 펴면
#: `TRAFFIC_TOTAL_UPLINK_RETRANS_PACKET_COUNT`(42자)로 식별자 30자 상한을
#: 넘는다(상용이 Oracle 포팅이라 그 제약을 받는다).
#:
#: `EVENT_TIME` 은 여기 없다 — 규격 필드(`SummaryCreateTime`)는 epoch 실수지만
#: DB 컬럼은 파티션 키용 `datetime` 이라 writer 가 분 시각을 직접 넣는다.
#: `write_xdr` 이 C-Plane 에 하는 것과 같다.
UPLANE_FIELDS: dict[str, tuple[tuple[str, str], ...]] = {
    "transport": (
        ("ENB_ID", "eNB_ID"),
        ("CELL_ID", "Cell_ID"),
        ("IMSI", "IMSI"),
        ("SYN_COUNT", "SynCount"),
        ("ACK_TIME", "AckTime"),
        ("RST_COUNT", "RstCount"),
        ("UPLINK_WINDOW_CLOSE_COUNT", "UplinkWindowCloseCount"),
        ("DNLINK_WINDOW_CLOSE_COUNT", "DnlinkWindowCloseCount"),
        ("SYN_MSS_SIZE", "SynMssSize"),
        ("SYN_ACK_MSS_SIZE", "SynAckMssSize"),
        ("UPLINK_RTT_TIME", "UplinkRTTTime"),
        ("UPLINK_RTT_COUNT", "UplinkRTTCount"),
        ("DNLINK_RTT_TIME", "DnlinkRTTTime"),
        ("DNLINK_RTT_COUNT", "DnlinkRTTCount"),
        ("RETRANS_PKT_UP", "Traffic_Total_Uplink_RetransPacketCount"),
        ("PAYLOAD_PKT_UP", "Traffic_Total_Uplink_PayloadPacketCount"),
        ("RETRANS_PKT_DN", "Traffic_Total_Dnlink_RetransPacketCount"),
        ("PAYLOAD_PKT_DN", "Traffic_Total_Dnlink_PayloadPacketCount"),
        ("PAYLOAD_SIZE_UP", "Traffic_Total_Uplink_PayloadSize"),
        ("PAYLOAD_SIZE_DN", "Traffic_Total_Dnlink_PayloadSize"),
        ("THROUGHPUT_TIME_UP", "Traffic_Total_Uplink_ThroughputTime"),
        ("THROUGHPUT_TIME_DN", "Traffic_Total_Dnlink_ThroughputTime"),
        ("TETHERING", "Tethering"),
    ),
    "dns": (
        ("ENB_ID", "eNB_ID"),
        ("CELL_ID", "Cell_ID"),
        ("IMSI", "IMSI"),
        ("QUERY_NAME", "QueryName"),
        ("REPLY_CODE", "ReplyCode"),
        ("ANSWER_COUNT", "AnswerCount"),
        ("QUERY_RETRY_COUNT", "QueryRetryCount"),
        ("DNS_RETRY_SEQUENCE", "dns_retry_sequence"),
        ("DNS_RETRY_INTERVAL", "dns_retry_interval"),
        ("RESPONSE_TIME", "ResponseTime"),
        ("REQUEST_FIRST_TIME", "RequestFirstTime"),
        ("PAYLOAD_SIZE_UP", "Traffic_Total_Uplink_PayloadSize"),
        ("PAYLOAD_SIZE_DN", "Traffic_Total_Dnlink_PayloadSize"),
    ),
    "http": (
        ("ENB_ID", "eNB_ID"),
        ("CELL_ID", "Cell_ID"),
        ("IMSI", "IMSI"),
        ("HOST", "Host"),
        ("HOST_SITE", "Host_Site"),
        ("STATUS_CODE", "StatusCode"),
        ("REQUEST_TIME", "RequestTime"),
        ("RESPONSE_TIME", "ResponseTime"),
        ("UPLINK_RTT_TIME", "UplinkRTTTime"),
        ("UPLINK_RTT_COUNT", "UplinkRTTCount"),
        ("DNLINK_RTT_TIME", "DnlinkRTTTime"),
        ("DNLINK_RTT_COUNT", "DnlinkRTTCount"),
        ("RETRANS_PKT_DN", "Traffic_Total_Dnlink_RetransPacketCount"),
        ("PAYLOAD_PKT_DN", "Traffic_Total_Dnlink_PayloadPacketCount"),
        ("PAYLOAD_SIZE_DN", "Traffic_Total_Dnlink_PayloadSize"),
        ("THROUGHPUT_TIME_DN", "Traffic_Total_Dnlink_ThroughputTime"),
    ),
}

#: `information_schema.DATA_TYPE` 중 숫자로 보는 것. 빈 문자열을 NULL 로
#: 번역할 대상을 가리는 데 쓴다(`UplaneMapping.numeric`).
_NUMERIC_TYPES: frozenset[str] = frozenset({
    "tinyint", "smallint", "mediumint", "int", "integer", "bigint",
    "decimal", "numeric", "float", "double", "real", "bit",
})

#: 분 귀속 컬럼 — **원시 네 종이 모두 이 이름을 쓴다**(2026-10-14).
#: 예전에는 U-Plane 만 `SUMMARY_CREATE_TIME`(규격 `SummaryCreateTime`)이고
#: C-Plane 은 `EVENT_TIME` 이었다. 한 묶음으로 묶으면서 통일했다 — 상용
#: `PM_*` 와 집계 `GP_KPI_*`(`SLOT_TIME`)에 대응이 분명한 쪽이다.
#: graphio 쪽 정본은 `store/tables.py:UPLANE_TIME` 이고 **같은 값이어야 한다.**
UPLANE_SLOT = "EVENT_TIME"

#: U-Plane 계열 순서. 로그·집계 보고에서 자리를 고정한다.
UPLANE_SERIES: tuple[str, ...] = ("transport", "dns", "http")


# --------------------------------------------------------------------------
# 실측 분포
# --------------------------------------------------------------------------


class Distributions:
    """`_distributions.json` 의 실측 구성비 샘플러.

    생성기 자체 프로파일(`data/profile.json`)은 PoC 용 합성 분포라 상용과
    구성비가 다르다. CALL_TYPE 비율이 틀리면 트래픽 구성비가 바뀌어 품질이
    그대로여도 전체 합산 소통률이 움직인다(불변규칙 3).
    """

    def __init__(self, path: Path, rng: random.Random, success_only: bool = True):
        d = json.loads(path.read_text(encoding="utf-8"))
        self.rng = rng
        self.success_only = success_only

        self.call_types = [e["v"] for e in d["call_type"]]
        self.call_type_w = [e["n"] for e in d["call_type"]]
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

        self.apns = [e["v"] for e in d["apn"]]
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
                    "pg_att": 0, "pg_suc": 0}
        rng = self.rng
        att = 1 if rng.random() < r["att"] else 0
        if self.success_only:
            suc, drop = att, 0
        else:
            suc = 1 if att and rng.random() < (r["suc"] / r["att"] if r["att"] else 0) else 0
            drop = 1 if rng.random() < r["drop"] else 0
        detach = 1 if rng.random() < r["detach"] else 0

        p = self.proc_ratio.get(call_type) or {}
        out = {"att": att, "suc": suc, "drop": drop, "detach": detach}
        for key, a_name, s_name in (
            ("data", "data_attempt", "data_success"),
            ("ims", "ims_attempt", "ims_success"),
            ("pg", "paging_attempt", "paging_success"),
        ):
            a = 1 if rng.random() < p.get(a_name, 0.0) else 0
            if not a:
                out[f"{key}_att"], out[f"{key}_suc"] = 0, 0
                continue
            if self.success_only:
                sv = 1
            else:
                num, den = p.get(s_name, 0.0), p.get(a_name, 0.0)
                sv = 1 if rng.random() < (num / den if den else 0.0) else 0
            out[f"{key}_att"], out[f"{key}_suc"] = a, sv
        return out


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
    return 0.25 * (16.0 ** ((h % 10_000) / 10_000))


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


def _pick_enbs(uniq: dict, enb_limit: int | None) -> set[str] | None:
    """생성 대상 eNB 를 결정론적으로 고른다.

    좌표가 있는 eNB 를 셀 수 많은 순으로 채우고, 마지막 5% 는 **좌표 없는
    eNB** 로 남긴다 — 미매핑 셀이 0이면 `H-07`(미매핑 감시)과 UNMAPPED 패널이
    한 번도 검증되지 않는다(불변규칙 12).
    """
    if not enb_limit or enb_limit <= 0:
        return None
    cnt: dict[str, int] = {}
    geo: dict[str, bool] = {}
    for (enb, _cn), r in uniq.items():
        cnt[enb] = cnt.get(enb, 0) + 1
        geo[enb] = geo.get(enb, False) or (r[5] is not None)
    mapped = sorted((e for e in cnt if geo[e]), key=lambda e: (-cnt[e], e))
    unmapped = sorted((e for e in cnt if not geo[e]), key=lambda e: (-cnt[e], e))
    n_un = min(len(unmapped), max(1, enb_limit // 20))
    out = set(unmapped[:n_un])
    out.update(mapped[: max(0, enb_limit - len(out))])
    return out


def load_cells(conn, enb_limit: int | None = None,
               drawable: set[str] | None = None) -> list[Cell]:
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
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT c.ENB_ID, c.CELLNUM, c.C_UID, c.MME_GRP_ID, c.EMS_ID, "
            "       r.CITY_ID, r.GU_ID, r.DONG_ID, r.LATITUDE, r.LONGITUDE "
            f"FROM CL_CELL_MAP AS c "
            "LEFT JOIN CM_CELL_REP_RU_LOC AS r "
            "       ON r.ENB_ID = c.ENB_ID AND r.SECTOR = c.CELLNUM "
            "WHERE c.ENB_ID IS NOT NULL AND c.CELLNUM IS NOT NULL"
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

    # 폴리곤 있는 동으로 좁힌 **뒤에** eNB 를 고른다. 순서를 바꾸면 고른 eNB 의
    # 셀이 대부분 탈락해 생성 규모가 무너진다.
    uniq = _filter_drawable(uniq, drawable)
    keep = _pick_enbs(uniq, enb_limit)
    cells: list[Cell] = []
    for (enb, cellnum), (_e, _c, c_uid, grp, ems,
                         city, gu, dong, lat, lon) in sorted(uniq.items()):
        if keep is not None and enb not in keep:
            continue
        # 같은 eNB 의 셀은 같은 Core 노드를 타야 한다 — 노드 축 집계가 성립하려면
        # 셀마다 노드가 흔들려서는 안 된다. eNB 해시로 고정한다.
        h = int(hashlib.sha256(enb.encode()).hexdigest()[:12], 16)
        pool = by_grp.get(str(grp)) or all_mme
        mme = pool[h % len(pool)]
        pgw = pgws[h % len(pgws)]
        sgw = pgws[(h // 7) % len(pgws)]        # 상용은 SPGW 합본 — 같은 풀을 쓴다
        ims = pgws[(h // 13) % len(pgws)]
        mapped = city is not None
        cells.append(Cell(
            cell_key=f"{enb}-{cellnum}",
            enb_id=enb,
            cell_id=cellnum,
            enb_seq=h % 1_000_000,
            c_uid=str(c_uid or ""),
            tac=0,                      # CL_CELL_MAP 에 TAC 가 없다 — 모름
            plmn="45005",
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
    return cells


# --------------------------------------------------------------------------
# 컬럼 매핑 — 위치로 맞춘다
# --------------------------------------------------------------------------


@dataclass
class XdrMapping:
    columns: list[str]              # EVENT_TIME 을 포함한 상용 컬럼 순서
    widths: list[int | None]        # varchar 최대 길이 (datetime 은 None)


def xdr_mapping(conn, database: str) -> XdrMapping:
    """상용 XDR 컬럼 순서·길이를 읽고 생성기 필드와 1:1 인지 검증한다.

    이름이 아니라 위치로 맞추므로, 길이가 어긋나면 **전부 한 칸씩 밀려** 조용히
    틀린 값이 들어간다. 그래서 여기서 멈춘다.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COLUMN_NAME, CHARACTER_MAXIMUM_LENGTH FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s ORDER BY ORDINAL_POSITION",
            [database, XDR_TABLE],
        )
        rows = cur.fetchall()
    cols = [r[0] for r in rows]
    widths = [int(r[1]) if r[1] is not None else None for r in rows]
    fields = SERIES_FIELDS["lte_call"]
    if not cols or cols[0].upper() != "EVENT_TIME":
        raise RuntimeError(f"{XDR_TABLE} 첫 컬럼이 EVENT_TIME 이 아니다: {cols[:1]}")
    if len(cols) - 1 != len(fields):
        raise RuntimeError(
            f"컬럼 수 불일치 — 상용 {len(cols) - 1} vs 생성기 {len(fields)}. "
            "위치 매핑이 성립하지 않는다"
        )
    return XdrMapping(cols, widths)


@dataclass
class UplaneMapping:
    """계열 하나의 적재 명세 — `(GP_ 컬럼, 생성기 필드 인덱스, varchar 폭)`."""

    table: str
    columns: list[str]              # UPLANE_SLOT 을 포함한 INSERT 컬럼 순서
    picks: list[int]                # 생성기 값 리스트에서 뽑을 위치 (SLOT 제외)
    widths: list[int | None]        # picks 와 같은 길이
    #: 숫자 타입 컬럼인가 — `picks` 와 같은 길이.
    #:
    #: **빈 문자열을 NULL 로 번역해야 하는 자리를 가린다.** 측정값이
    #: `varchar(20)` 에서 숫자로 바뀐 뒤(2026-10-14) 빈 문자열을 그대로 넘기면
    #: `STRICT_TRANS_TABLES` 가 `ERROR 1366 Incorrect integer value` 로 거부해
    #: 적재가 통째로 멈춘다. 실측 분포에 그런 값이 있다 — `data/profile.json`
    #: 의 `dns.categorical` 에서 `ReplyCode`·`AnswerCount`·`QueryRetryCount`
    #: 가 각각 0.385% 확률로 빈 문자열을 낸다(원본 .dat 에 그렇게 들어 있었다).
    numeric: list[bool]


def uplane_mapping(conn, database: str) -> dict[str, UplaneMapping]:
    """`GP_RAW_*` 세 테이블의 적재 명세를 만든다.

    **이름으로 맞추고, 어긋나면 멈춘다.** DDL 쪽 컬럼이 `UPLANE_FIELDS` 에
    없거나 반대로 없는 컬럼을 적재하려 하면 둘 다 에러다. 조용히 빠지면 그
    카운터가 영원히 NULL 이고, NULL 은 "모름"으로 읽혀 해당 KPI 가 산출 불가로
    내려간다 — 데이터는 들어오는데 지표만 안 서는 형태라 추적이 어렵다.

    테이블이 아직 없으면(DDL 미적용) 그 계열을 **생략한다**. 멈추지 않는
    이유는 C-Plane 적재가 U-Plane 때문에 막혀서는 안 되기 때문이다 —
    호출부가 돌려받은 dict 의 키로 "무엇이 살아 있는가"를 판정한다.
    """
    out: dict[str, UplaneMapping] = {}
    for series in UPLANE_SERIES:
        table = GP_XDR[series]
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COLUMN_NAME, CHARACTER_MAXIMUM_LENGTH, DATA_TYPE "
                "FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s ORDER BY ORDINAL_POSITION",
                [database, table],
            )
            rows = cur.fetchall()
        if not rows:
            continue
        db_cols = {r[0].upper(): (int(r[1]) if r[1] is not None else None) for r in rows}
        # **타입을 DB 에서 읽는다.** 코드에 목록을 적으면 DDL 을 바꿀 때
        # 한쪽만 고쳐지고, 그러면 빈 문자열이 다시 적재를 멈춘다.
        db_numeric = {r[0].upper(): str(r[2]).lower() in _NUMERIC_TYPES for r in rows}
        spec = UPLANE_FIELDS[series]
        want = {c for c, _ in spec} | {UPLANE_SLOT}
        if missing := want - set(db_cols):
            raise RuntimeError(f"{table} 에 없는 컬럼: {sorted(missing)}")
        if extra := set(db_cols) - want:
            raise RuntimeError(
                f"{table} 에 적재 명세가 없는 컬럼: {sorted(extra)} — "
                "UPLANE_FIELDS 를 채우지 않으면 그 컬럼은 영원히 NULL 이다"
            )
        idx = {name: i for i, name in enumerate(SERIES_FIELDS[series])}
        if absent := [f for _, f in spec if f not in idx]:
            raise RuntimeError(f"{series} 생성기에 없는 규격 필드: {absent}")
        out[series] = UplaneMapping(
            table=table,
            columns=[UPLANE_SLOT] + [c for c, _ in spec],
            picks=[idx[f] for _, f in spec],
            widths=[db_cols[c] for c, _ in spec],
            numeric=[db_numeric[c] for c, _ in spec],
        )
    return out


# --------------------------------------------------------------------------
# 생성 — 기존 Generator 를 상용 분포로 덮어쓴다
# --------------------------------------------------------------------------


class PemdbGen:
    """상용 스키마용 생성기 — 셀 마스터는 DB, 분포는 실측 JSON."""

    def __init__(self, conn, data_dir: Path, rates: dict, seed: int = 20260903,
                 subscriber_pool: int = 20_000, success_only: bool = True,
                 enb_limit: int | None = None,
                 drawable: set[str] | None = None):
        self.cells = load_cells(conn, enb_limit=enb_limit, drawable=drawable)
        if not self.cells:
            raise RuntimeError("CL_CELL_MAP 이 비어 있다 — 마스터를 먼저 적재하라")
        self.dist = Distributions(
            data_dir / "pemdb1" / "_distributions.json",
            random.Random(seed + 101), success_only=success_only,
        )
        profiles = load_profiles(data_dir / "profile.json")
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
        )
        self._reseat_subscribers(random.Random(seed + 202))
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
            # 인바운드 로머 — APN 분포에 mcc234 가 2.6% 있다 (sg1.super...)
            sub.mcc_mnc = "2340500" if rng.random() < 0.026 else "4500503"
            # **식별자를 다시 만든다.** `mcc_mnc` 와 기종을 여기서 덮으므로,
            # `_make_subscribers` 가 만든 IMSI·IMEI 는 이미 어긋났다. IMSI 는
            # 규격상 `imsi_mcc_mnc_info` 로 시작해야 하고(앞 7자), IMEI 의 TAC 는
            # 기종과 묶여야 한다(R-10). 다시 만들지 않으면 둘 다 조용히 깨진다.
            # MDN 은 두 값에 의존하지 않으므로 그대로 둔다.
            sub.imsi = synth_imsi(sub.mcc_mnc, i)
            sub.imei = synth_imei(sub.vendor, sub.model, i)

    # -- 분 단위 ---------------------------------------------------------

    def minute_of(self, ts: datetime) -> int:
        if self.anchor is None:
            raise RuntimeError("anchor 미설정")
        return int((ts - self.anchor).total_seconds() // 60)

    def records(self, minute: int, ts: datetime) -> list[list[str]]:
        """한 분의 레코드(생성기 필드 순서의 값 리스트)를 만든다.

        순서가 중요하다: 기반 생성 → **상용 분포 덮기** → 시나리오 변형.
        덮기를 변형 뒤에 두면 장애가 지워진다.
        """
        gen = self.gen
        slot_start = ts.timestamp()
        active = [sc for sc in gen.scenarios if sc.covers(minute)]
        for sc in active:
            sc.active_cells_cache = sc.active_cells(minute)  # type: ignore[attr-defined]

        out: list[list[str]] = []
        for _ in range(gen._volume("lte_call", ts)):
            rec, cell = gen.base_lte_call(ts, slot_start)
            self._coerce(rec)
            for sc in active:
                if "lte_call" not in sc.series:
                    continue
                if sc.target.cell_keys and cell.cell_key not in sc.active_cells_cache:
                    continue
                MUTATORS[sc.kind](rec, sc, sc.intensity(minute), gen.rng, cell)
            out.append(rec.values)
        return out

    def uplane_records(self, minute: int, ts: datetime) -> dict[str, list[list[str]]]:
        """한 분의 U-Plane 레코드 — 계열별 값 리스트.

        `records`(lte_call)와 구조가 같지만 **`_coerce` 가 없다.** 덮어쓸 상용
        실측 분포가 없기 때문이다 — 상용에 원시 Transport·DNS·HTTP XDR 자체가
        없어서 `_distributions.json` 에 그 계열의 값 분포가 들어 있지 않다.
        여기서 억지로 C-Plane 분포를 가져다 쓰면 근거 없는 수치가 된다.

        **시나리오 변형은 계열을 가린다.** `sc.series` 에 그 계열이 없으면
        건너뛴다 — `SC-05`(DNS)·`SC-06`(HTTP)·`SC-07`(Transport)이 각각 자기
        계열만 건드리도록 이미 선언돼 있고, 전 계열에 걸면 한 장애가 세 축을
        동시에 흔들어 원인 판정이 섞인다.
        """
        gen = self.gen
        slot_start = ts.timestamp()
        active = [sc for sc in gen.scenarios if sc.covers(minute)]
        for sc in active:
            sc.active_cells_cache = sc.active_cells(minute)  # type: ignore[attr-defined]

        out: dict[str, list[list[str]]] = {}
        for series in UPLANE_SERIES:
            if series not in self.series:
                continue
            rows: list[list[str]] = []
            for _ in range(gen._volume(series, ts)):
                rec, cell = gen.base_user_traffic(series, ts, slot_start)
                for sc in active:
                    if series not in sc.series:
                        continue
                    if sc.target.cell_keys and cell.cell_key not in sc.active_cells_cache:
                        continue
                    MUTATORS[sc.kind](rec, sc, sc.intensity(minute), gen.rng, cell)
                rows.append(rec.values)
            out[series] = rows
        return out

    def _coerce(self, rec) -> None:
        """자체 프로파일에서 나온 값을 상용 실측값으로 바꾼다."""
        d = self.dist
        # 상용 추출은 ONGOING_FLAG='2'(호 종료)만 담는다. 1(진행 중) 레코드를
        # 섞으면 플래그가 비어 분모가 조용히 줄어든다.
        rec.set("OngoingFlag", ONGOING_END)
        ct = d.call_type()
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

        # 해제 Cause — 절단 호에는 비정상만 붙인다
        rel = d.abnormal_release() if f["drop"] else d.release_cause()
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
        """`datagen_fault` 활성 행 목록을 걸어 둔다 — 실시간 틱(`cli.cmd_run`)용.

        해석은 `faults.resolve_active` 가 한다. 구 창고 경로(`engine.DataGen`)와
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
    """상용 테이블 적재 — 구간 멱등(DELETE→INSERT)."""

    def __init__(self, conn, mapping: XdrMapping):
        self.conn = conn
        self.map = mapping
        self.clamped = 0
        #: 숫자 컬럼에 온 빈 문자열을 NULL 로 바꾼 횟수. **조용히 바꾸지
        #: 않는다** — 실측 분포가 내는 "모름"이라 정상이지만, 갑자기 늘면
        #: 생성 쪽이 바뀐 것이다.
        self.nulled = 0

    def _row(self, ts: datetime, values: list[str]) -> tuple:
        """생성기 값 리스트 → 상용 컬럼 순서 튜플 (EVENT_TIME 선두)."""
        out: list = [ts]
        for i, v in enumerate(values):
            w = self.map.widths[i + 1]
            if w is not None and len(v) > w:
                # STRICT_TRANS_TABLES 에서는 길이 초과가 에러다. 잘라서 넘기되
                # 횟수를 세어 보고한다 — 조용히 자르면 값이 틀린 것을 모른다.
                v = v[:w]
                self.clamped += 1
            out.append(v)
        return tuple(out)

    def write_xdr(self, ts: datetime, rows: list[list[str]]) -> int:
        cols = ",".join(f"`{c}`" for c in self.map.columns)
        marks = ",".join(["%s"] * len(self.map.columns))
        data = [self._row(ts, r) for r in rows]
        with self.conn.cursor() as cur:
            cur.execute(
                f"DELETE FROM `{XDR_TABLE}` WHERE EVENT_TIME >= %s AND EVENT_TIME < %s",
                [ts, ts + timedelta(minutes=1)],
            )
            for i in range(0, len(data), 2000):
                cur.executemany(
                    f"INSERT INTO `{XDR_TABLE}` ({cols}) VALUES ({marks})",
                    data[i:i + 2000],
                )
        self.conn.commit()
        return len(data)

    def write_uplane(self, ts: datetime, series: str, rows: list[list[str]],
                     mapping: UplaneMapping) -> int:
        """`GP_RAW_*` 한 계열 적재 — `write_xdr` 과 같은 구간 멱등이다.

        `EVENT_TIME` 에는 **분 시각을 넣는다.** 규격 필드(`SummaryCreateTime`)는 epoch
        실수지만 DB 컬럼은 파티션 키라 `datetime` 이어야 하고, 1분 집계에
        분 안의 초는 쓰이지 않는다. `write_xdr` 이 `EVENT_TIME` 에 하는 것과
        같은 처리다 — 네 테이블의 분 귀속이 같아야 같은 분의 C-Plane·U-Plane
        이 한 Incident 로 상관된다. **이제 컬럼 이름까지 같다.**

        응답시간 계열(`RESPONSE_TIME`·`REQUEST_TIME`·`REQUEST_FIRST_TIME`)은
        **원래의 epoch 값을 그대로 둔다.** 집계가 둘의 차이를 쓰므로 분 시각으로
        덮으면 모든 응답시간이 0 이 된다.
        """
        cols = ",".join(f"`{c}`" for c in mapping.columns)
        marks = ",".join(["%s"] * len(mapping.columns))
        data = []
        for values in rows:
            out: list = [ts]
            for pos, width, is_num in zip(mapping.picks, mapping.widths,
                                          mapping.numeric):
                v = values[pos]
                if is_num and isinstance(v, str) and not v.strip():
                    # **빈 문자열은 "모름"이고, 숫자 컬럼에서 그 표현은 NULL
                    # 이다**(불변규칙 17). 0 으로 채우면 `REPLY_CODE` 에서는
                    # NOERROR(성공)가 되고 `ANSWER_COUNT` 에서는 "응답 0건"이
                    # 되어, 모르는 것이 사실처럼 집계된다.
                    #
                    # 그대로 넘기면 `STRICT_TRANS_TABLES` 가 `ERROR 1366` 으로
                    # **적재를 통째로 멈춘다** — 실측 분포가 0.385% 확률로
                    # 그런 값을 내므로 1분이면 반드시 걸린다.
                    out.append(None)
                    self.nulled += 1
                    continue
                if width is not None and len(v) > width:
                    # STRICT_TRANS_TABLES 에서는 길이 초과가 에러다. 잘라서
                    # 넘기되 횟수를 센다 — 조용히 자르면 값이 틀린 것을 모른다.
                    v = v[:width]
                    self.clamped += 1
                out.append(v)
            data.append(tuple(out))
        with self.conn.cursor() as cur:
            cur.execute(
                f"DELETE FROM `{mapping.table}` "
                f"WHERE `{UPLANE_SLOT}` >= %s AND `{UPLANE_SLOT}` < %s",
                [ts, ts + timedelta(minutes=1)],
            )
            for i in range(0, len(data), 2000):
                cur.executemany(
                    f"INSERT INTO `{mapping.table}` ({cols}) VALUES ({marks})",
                    data[i:i + 2000],
                )
        self.conn.commit()
        return len(data)

    def write_uplane_all(self, ts: datetime, batches: dict[str, list[list[str]]],
                         mappings: dict[str, UplaneMapping]) -> dict[str, int]:
        """계열별 적재를 한 번에. 테이블이 없는 계열은 **건너뛴다.**

        `uplane_mapping` 이 DDL 미적용 계열을 명세에서 빼므로, 여기서 그
        계열의 레코드는 버려진다. 생성은 했는데 적재되지 않는 상태를 조용히
        두면 "왜 비어 있나"를 추적할 수 없으니 호출부가 반환값으로 센다.
        """
        return {
            s: self.write_uplane(ts, s, rows, mappings[s])
            for s, rows in batches.items() if s in mappings
        }

    def last_event_time(self, table: str = XDR_TABLE) -> datetime | None:
        with self.conn.cursor() as cur:
            cur.execute(f"SELECT MAX(EVENT_TIME) FROM `{table}`")
            return cur.fetchone()[0]


# --------------------------------------------------------------------------
# 1분 집계 — MySQL 안에서 돈다
# --------------------------------------------------------------------------

#: eNB 단위 대표 속성. CL_CELL_MAP 은 (ENB_ID, CELLNUM) 단위라 eNB 하나에
#: 여러 행이 있다. C_UID·MME_GRP_ID 는 eNB 당 유일(215/215 실측)하지만
#: EMS_ID 는 셀마다 다를 수 있어(855쌍) 대표값 하나를 고른다.
_ENB_MASTER = (
    "(SELECT ENB_ID, MIN(C_UID) AS C_UID, MIN(MME_GRP_ID) AS MME_GRP_ID, "
    "        MIN(EMS_ID) AS EMS_ID "
    "   FROM CL_CELL_MAP WHERE ENB_ID IS NOT NULL GROUP BY ENB_ID) AS m"
)

#: 절단 — DROP_FLAG 가 섰고 해제 Cause 가 정상 종료가 아닌 것만. 정상 종료를
#: 절단으로 세면 이동이 잦은 지역이 상시 붉어진다 (불변규칙 6).
_DROP = (
    "COUNT(CASE WHEN x.DROP_FLAG='1' AND x.UECONTEXTRELEASE_CAUSE NOT IN "
    "(" + ",".join(f"'{c}'" for c in NORMAL_RELEASE) + ") THEN 1 END)"
)


def _flag(col: str) -> str:
    """플래그 합산. `SUM(CASE..)` 는 0행에서 NULL 이 되므로 COUNT 를 쓴다."""
    return f"COUNT(CASE WHEN x.{col}='1' THEN 1 END)"


def aggregate_enb(conn, lo: datetime, hi: datetime) -> int:
    """`XDR_LTE_CALL_KPI` → `PM_ENB_KPI_1M`.

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
    """
    sql = f"""
    INSERT INTO `{PM_ENB}`
      (EVENT_TIME, CDATE, CHOUR, CTIME, ENB_ID, CALL_TYPE, C_UID,
       MME_GRP_ID, EMS_ID,
       ATTEMPT_CNT, SUCCESS_CNT, DROP_CNT,
       DATA_ATTEMPT_CNT, DATA_SUCESS_CNT, IMS_ATTEMPT_CNT, IMS_SUCESS_CNT,
       UNIQ_IMSI_CNT, DETACH_CNT)
    SELECT x.EVENT_TIME,
           DATE_FORMAT(x.EVENT_TIME, '%%Y%%m%%d'),
           DATE_FORMAT(x.EVENT_TIME, '%%H'),
           DATE_FORMAT(x.EVENT_TIME, '%%H%%i'),
           x.FIRST_ENB_ID, x.CALL_TYPE,
           m.C_UID, m.MME_GRP_ID, m.EMS_ID,
           {_flag('ATTEMPT_FLAG')}, {_flag('SUCCESS_FLAG')}, {_DROP},
           {_flag('DATA_ATTEMPT_FLAG')}, {_flag('DATA_SUCCESS_FLAG')},
           {_flag('IMS_ATTEMPT_FLAG')}, {_flag('IMS_SUCCESS_FLAG')},
           COUNT(DISTINCT x.IMSI), {_flag('DETACH_FLAG')}
      FROM `{XDR_TABLE}` AS x
      LEFT JOIN {_ENB_MASTER} ON m.ENB_ID = x.FIRST_ENB_ID
     WHERE x.EVENT_TIME >= %s AND x.EVENT_TIME < %s
       AND x.ONGOINGFLAG = '{ONGOING_END}'
       AND x.FIRST_ENB_ID IS NOT NULL AND x.FIRST_ENB_ID <> ''
     GROUP BY x.EVENT_TIME, x.FIRST_ENB_ID, x.CALL_TYPE,
              m.C_UID, m.MME_GRP_ID, m.EMS_ID
    """
    with conn.cursor() as cur:
        cur.execute(
            f"DELETE FROM `{PM_ENB}` WHERE EVENT_TIME >= %s AND EVENT_TIME < %s", [lo, hi])
        cur.execute(sql, [lo, hi])
        n = cur.rowcount
    conn.commit()
    return n


def aggregate_enb_cause(conn, lo: datetime, hi: datetime) -> int:
    """`XDR_LTE_CALL_KPI` → `PM_ENB_CAUSE_1M`.

    CAUSE 표기는 CAUSE_DEF·CD_CAUSE 를 따른다 — `C_<IF_PROTO 2자리><Cause 6자리>`
    (실측: `C_02000504` = S1MME_S1AP + Cause 504).

    **First Error 구간이 원인이고 하류 Cause 는 증상이다**(불변규칙 7). 그래서
    First Error 쪽을 주로 담고, S1AP 해제 Cause 는 정상 종료를 뺀 비정상만
    따로 담는다.
    """
    normal = ",".join(f"'{c}'" for c in NORMAL_RELEASE)
    proto_case = " ".join(
        f"WHEN '{k}' THEN '{v}'" for k, v in IF_PROTO_NAME.items())
    sql = f"""
    INSERT INTO `{PM_ENB_CAUSE}`
      (EVENT_TIME, CDATE, CHOUR, CTIME, ENB_ID, CALL_TYPE, C_UID,
       MME_GRP_ID, EMS_ID, CAUSE_TYPE, CAUSE, CAUSE_CNT)
    SELECT s.EVENT_TIME,
           DATE_FORMAT(s.EVENT_TIME, '%%Y%%m%%d'),
           DATE_FORMAT(s.EVENT_TIME, '%%H'),
           DATE_FORMAT(s.EVENT_TIME, '%%H%%i'),
           s.ENB_ID, s.CALL_TYPE, m.C_UID, m.MME_GRP_ID, m.EMS_ID,
           s.CAUSE_TYPE, s.CAUSE, COUNT(*)
      FROM (
            SELECT x.EVENT_TIME, x.FIRST_ENB_ID AS ENB_ID, x.CALL_TYPE,
                   CASE x.FIRST_ERROR_INTERFACE_PROTOCOL {proto_case}
                        ELSE 'NONE' END AS CAUSE_TYPE,
                   CONCAT('C_',
                          LPAD(x.FIRST_ERROR_INTERFACE_PROTOCOL, 2, '0'),
                          LPAD(x.FIRST_ERROR_CAUSE, 6, '0')) AS CAUSE
              FROM `{XDR_TABLE}` AS x
             WHERE x.EVENT_TIME >= %s AND x.EVENT_TIME < %s
               AND x.ONGOINGFLAG = '{ONGOING_END}'
               AND x.FIRST_ERROR_CAUSE NOT IN ('', '0')
            UNION ALL
            SELECT x.EVENT_TIME, x.FIRST_ENB_ID AS ENB_ID, x.CALL_TYPE,
                   'S1AP' AS CAUSE_TYPE,
                   CONCAT('C_02', LPAD(x.UECONTEXTRELEASE_CAUSE, 6, '0')) AS CAUSE
              FROM `{XDR_TABLE}` AS x
             WHERE x.EVENT_TIME >= %s AND x.EVENT_TIME < %s
               AND x.ONGOINGFLAG = '{ONGOING_END}'
               AND x.UECONTEXTRELEASE_CAUSE NOT IN ('', '0', {normal})
           ) AS s
      LEFT JOIN {_ENB_MASTER} ON m.ENB_ID = s.ENB_ID
     WHERE s.ENB_ID IS NOT NULL AND s.ENB_ID <> ''
     GROUP BY s.EVENT_TIME, s.ENB_ID, s.CALL_TYPE, s.CAUSE_TYPE, s.CAUSE,
              m.C_UID, m.MME_GRP_ID, m.EMS_ID
    """
    with conn.cursor() as cur:
        cur.execute(
            f"DELETE FROM `{PM_ENB_CAUSE}` WHERE EVENT_TIME >= %s AND EVENT_TIME < %s",
            [lo, hi])
        cur.execute(sql, [lo, hi, lo, hi])
        n = cur.rowcount
    conn.commit()
    return n


def aggregate_epc(conn, lo: datetime, hi: datetime) -> int:
    """`XDR_LTE_CALL_KPI` → `PM_EPC_KPI_1M_ORG` (MME/SGW/PGW 노드 축).

    MCC/MNC/MNO 는 가입자 차원이라 노드·분 단위 집계에 단일 값이 없다. 임의로
    고르면 없는 사실을 만들므로 NULL 로 둔다(불변규칙 17).
    """
    def leg(node_type: str, col: str) -> str:
        return (
            f"SELECT x.EVENT_TIME, '{node_type}' AS NODE_TYPE, x.{col} AS NODE_ID, "
            f"       x.CALL_TYPE, x.IMSI, x.ATTEMPT_FLAG, x.SUCCESS_FLAG, x.DROP_FLAG, "
            f"       x.DATA_ATTEMPT_FLAG, x.DATA_SUCCESS_FLAG, x.IMS_ATTEMPT_FLAG, "
            f"       x.IMS_SUCCESS_FLAG, x.DETACH_FLAG, x.UECONTEXTRELEASE_CAUSE "
            f"  FROM `{XDR_TABLE}` AS x "
            f" WHERE x.EVENT_TIME >= %s AND x.EVENT_TIME < %s "
            f"   AND x.ONGOINGFLAG = '{ONGOING_END}' "
            f"   AND x.{col} IS NOT NULL AND x.{col} <> ''"
        )

    legs = " UNION ALL ".join(
        leg(t, c) for t, c in (("MME", "MME_ID"), ("SGW", "SGW_ID"), ("PGW", "PGW_ID")))
    sql = f"""
    INSERT INTO `{PM_EPC}`
      (EVENT_TIME, CDATE, CHOUR, CTIME, NODE_TYPE, NODE_ID, CALL_TYPE,
       ATTEMPT_CNT, SUCCESS_CNT, DROP_CNT,
       DATA_ATTEMPT_CNT, DATA_SUCESS_CNT, IMS_ATTEMPT_CNT, IMS_SUCESS_CNT,
       UNIQ_IMSI_CNT, DETACH_CNT)
    SELECT x.EVENT_TIME,
           DATE_FORMAT(x.EVENT_TIME, '%%Y%%m%%d'),
           DATE_FORMAT(x.EVENT_TIME, '%%H'),
           DATE_FORMAT(x.EVENT_TIME, '%%H%%i'),
           x.NODE_TYPE, x.NODE_ID, x.CALL_TYPE,
           {_flag('ATTEMPT_FLAG')}, {_flag('SUCCESS_FLAG')}, {_DROP},
           {_flag('DATA_ATTEMPT_FLAG')}, {_flag('DATA_SUCCESS_FLAG')},
           {_flag('IMS_ATTEMPT_FLAG')}, {_flag('IMS_SUCCESS_FLAG')},
           COUNT(DISTINCT x.IMSI), {_flag('DETACH_FLAG')}
      FROM ({legs}) AS x
     GROUP BY x.EVENT_TIME, x.NODE_TYPE, x.NODE_ID, x.CALL_TYPE
    """
    with conn.cursor() as cur:
        cur.execute(
            f"DELETE FROM `{PM_EPC}` WHERE EVENT_TIME >= %s AND EVENT_TIME < %s", [lo, hi])
        cur.execute(sql, [lo, hi] * 3)
        n = cur.rowcount
    conn.commit()
    return n


def aggregate_all(conn, lo: datetime, hi: datetime) -> dict[str, int]:
    """[lo, hi) 구간의 1분 집계 전부. 구간 멱등이다."""
    return {
        PM_ENB: aggregate_enb(conn, lo, hi),
        PM_ENB_CAUSE: aggregate_enb_cause(conn, lo, hi),
        PM_EPC: aggregate_epc(conn, lo, hi),
    }


# --------------------------------------------------------------------------
# 보존 정리 — 파티션 단위로 버린다
# --------------------------------------------------------------------------

#: 생성기가 **쓰는** 테이블과 그 시각 컬럼 — 보존 정리의 대상이다.
#:
#: **기준은 접두가 아니라 쓰기 주체다.** `GP_RAW_*` 는 이름이 `GP_` 지만
#: **생성기가 쓰는 원시 XDR** 이므로 여기 들어간다. 빼면 두 가지가 같이 깨진다 —
#: 디스크가 무한히 자라고, `ensure_partitions` 가 앞을 늘려 주지 않아 범위를
#: 벗어난 분이 전부 `pmax` 한 칸에 몰려 프루닝이 죽는다(에러 없이 느려진다).
#:
#: **`GP_KPI_*`·`GP_INCIDENT` 류 분석 산출물은 넣지 않는다.** 그것은 graphio 가
#: 쓰고 (`graphio/store/tables.py:RESETTABLE_TABLES`), 남의 산출물을 생성기가
#: 지우면 두 주체가 같은 행을 다툰다. graphio 가 Incident 를 쓰는 중에 생성기가
#: 그 파티션을 떨어뜨리면 분석 결과만 사라지고 에러는 나지 않는다 — 운영자
#: 화면에서는 "장애 없음"으로 보인다(불변규칙 17 이 금지하는 형태).
#: `CL_*`·`CM_*` 마스터도 대상이 아니다 — 다른 주체가 관리하고 시각 컬럼이 없다.
WRITTEN_TABLES: tuple[tuple[str, str], ...] = (
    (XDR_TABLE, "EVENT_TIME"),
    (PM_ENB, "EVENT_TIME"),
    (PM_ENB_CAUSE, "EVENT_TIME"),
    (PM_EPC, "EVENT_TIME"),
) + tuple((GP_XDR[s], UPLANE_SLOT) for s in UPLANE_SERIES)


#: 원시 xDR 의 보관 시간. **시간 파티션이라 이 입도로 자를 수 있다** —
#: 일 파티션으로는 "보존 1일"이 실제로 1~2일분이 남았다(상한이 cutoff 이하인
#: 날만 떨어뜨릴 수 있기 때문이다). graphio 쪽 DDL 생성기
#: (`docker/mysql/gen_gp_xdr_ddl.py`)의 `RETAIN_HOURS` 와 같은 값이어야 한다.
RETAIN_HOURS = 24

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

#: 돌아가는 13종 전부. **채점 상한이 10/13(76.9%)에서 13/13 으로 올라간다** —
#: 구 벤치마크에서 SC-05·SC-06 은 C-Plane 이벤트가 0건이어서 탐지 자체가
#: 불가능했고, 그것이 탐지율 저하가 아니라 **채점 상한**이었다.
SUPPORTED: tuple[str, ...] = (
    "SC-01", "SC-02", "SC-03", "SC-04", "SC-05", "SC-06", "SC-07",
    "SC-08", "SC-09", "SC-10", "SC-11", "SC-12", "SC-13",
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
    wanted = set(SUPPORTED) if selected in ("all", None, "") else {
        t.strip().upper() for t in selected.split(",") if t.strip()}
    skipped = [f"{sid}: {why}" for sid, why in sorted(UNSUPPORTED.items())
               if sid in wanted]
    wanted &= set(SUPPORTED)
    if not wanted:
        return [], skipped

    rng = random.Random((seed if seed is not None else gen.seed) + 777)
    scs = build_sim_scenarios(
        gen.cells, minutes, rng,
        selected=sorted(wanted), lead_minutes=lead_minutes,
    )
    placed = {sc.id for sc in scs}
    skipped += [f"{sid}: 구간이 짧아 배치되지 않았다 (minutes={minutes})"
                for sid in sorted(wanted - placed)]
    gen.set_faults(scs)
    return scs, skipped


def write_truth(path: Path, start: datetime, minutes: int,
                scenarios: list[Scenario]) -> dict:
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
        "scenarios": [sc.as_truth() for sc in scenarios],
        # 미연동을 정답에 넣지 않는다. 넣으면 분모가 13이 되어 탐지하지
        # 못한 것이 아니라 **원천이 없는 것**까지 실패로 세게 된다.
        "unsupported": UNSUPPORTED,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    return doc
