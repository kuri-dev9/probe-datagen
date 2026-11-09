"""제어 테이블 — UI 와 생성 데몬이 MySQL 을 통해 만나는 자리.

UI 는 여기에 쓰고, 데몬은 틱마다 읽어 적용한다. DB 를 제어 채널로 쓰는 이유:
데몬이 재시작해도 설정·진행 중 장애가 복원되고(내구성), UI 와 데몬이 서로를
몰라도 되며(분리), "언제 무엇을 주입했나"가 공짜로 남는다(이력 = ground truth).

**`CM_DATAGEN_*` 다** (2026-10-14). 구 이름은 `datagen_config`·`datagen_fault`·
`datagen_history` 로 **소문자였고, 그것이 이 DB 에서 유일한 명명 이탈이었다** —
나머지 75테이블은 전부 대문자 `CD_`/`CL_`/`CM_`/`PM_`/`GP_` 접두를 쓴다.
여기서 만드는 스키마가 그대로 상용이 되므로(별도 상용 DB 는 없다) 로컬에서
규칙을 깨면 그 상태가 운영에 남는다.

`CM_` 는 공통·구성 정보의 접두다(`CM_EPC_INFO`·`CM_DONG`·`CM_EMS` …). 이 셋은
관측 데이터가 아니라 **생성기 구성·제어 상태**이므로 그 묶음에 속한다.
graphio 는 읽지 않는다 — 소유자가 만든다. `ensure_tables()` 가
`CREATE IF NOT EXISTS` 로 보장한다.

**파이썬 쪽 dict 키는 소문자를 유지한다.** `list_faults`·`poll_transitions` 가
커서 `description` 을 그대로 키로 쓰고 소비자 네 파일(`ui`·`cli`·`faults`·
`pemdb1`)이 `f["kind"]`·`f["start_ts"]` 로 읽는다. 그래서 **SELECT 에서 `AS` 로
구 이름을 되돌린다** — DB 는 관례를, 코드는 기존 계약을 지킨다.
"""

from __future__ import annotations

import json
from datetime import datetime

#: 테이블 이름. **문자열을 쿼리에 박지 않는다** — 이번 개명에서 15곳을
#: 찾아다녀야 했고, 한 곳을 빠뜨리면 `CREATE IF NOT EXISTS` 가 조용히 새
#: 테이블을 만들어 **두 벌이 공존**한다(에러 없음).
T_CONFIG = "CM_DATAGEN_CONFIG"
T_FAULT = "CM_DATAGEN_FAULT"
T_HISTORY = "CM_DATAGEN_HISTORY"
#: 장애 시나리오 목록 — **화면·지도가 읽는 고정 목록이다** (2026-10-28).
#: `CM_DATAGEN_*` 가 아닌 이유: 생성기 제어가 아니라 **장애 카탈로그**라
#: graphio 지도·다른 화면도 같은 표를 본다(사용자 지정 이름).
T_SCENARIO = "CM_ERROR_SCENARIO"
#: 앱 사전 — 정의와 적재는 `apps` 가 갖고 DDL 만 여기 모은다
from .apps import T_APP  # noqa: E402

#: 주입 가능한 장애 종류 — **`scenarios.KIND_SPECS` 의 키와 1:1 이어야 한다.**
#:
#: `MUTATORS` 가 아니라 `KIND_SPECS` 와 맞춘다(2026-10-22). 레코드 변형이 없는
#: kind 가 생겼기 때문이다 — `pgw_uplane_degradation` 은 `PLAN_MUTATORS` 로만
#: 돌고 `enb_s1_outage` 는 `SUPPRESSORS` 가 주력이다. `MUTATORS` 기준으로
#: 두면 그 둘이 UI 목록에서 빠지고, 빠진 이유가 어디에도 보이지 않는다.
#: `tests/test_kind_registry.py` 가 세 자리(여기·`KIND_SPECS`·`ui.KIND_LABELS`)를
#: 대조한다.
KINDS = (
    # 기존 13종
    "gw_resource", "gw_node_down", "ran_radio", "mme_overload", "auth_failure",
    "dns_degradation", "external_server", "transport_loss", "ue_model",
    "mobility", "change_suspected", "ims_pdn", "nsa_nr_leg",
    # 하위 변형 3
    "gw_pgw_restart", "external_path_latency", "nsa_policy_restrict",
    "anchor_saturation", "son_param_change", "ran_congestion",
    "ho_interruption", "earthquake", "cdn_degradation", "pay_dns_delay",
    # 27번 — 지진(`earthquake`)을 대체한다(2026-11-04). 옛 `earthquake` 는 지난 기록·정답지를
    # 다시 읽을 수 있게 남긴다(새로 주입하는 27번은 이쪽이다)
    "natural_disaster",
    "event_crowd",
    # 17번 — 숨은 SA 상주 단말이 LTE 로 넘어온다(2026-11-05 · `scenarios.sa_fallback`)
    "sa_fallback",
    # 신규 8종
    "auc_no_response", "dra_route_failure", "s6a_undetermined",
    "pgw_uplane_degradation", "enb_s1_outage", "partial_recovery",
    "apn_pdn_failure", "dns_flap", "compound_causal_failure",
    # 장애가 아닌 재현 — 증설 후 부하 이동(`j-sc-24`)
    "capacity_shift",
)

#: **장애 시나리오 고정 목록** — `CM_ERROR_SCENARIO` 의 정본 (2026-10-28).
#:
#: `(번호, 이름, 장애 종류, 같은 데이터 원본 번호, 발생 장비, 설명)`.
#: 장애 종류가 `None` 이면 준비 중이다. **발생 장비**는 그 시나리오를 걸 수
#: 있는 장비 종류이고 DB 에서는 장비마다 `*_YN` 마스크로 풀린다 — 지도에서
#: 장비를 찍으면 그 장비가 `Y` 인 시나리오만 보인다. 걸면 주입 API 의
#: `TARGET` 이 `MME-n`·`SGW-n`·`PGW-n`·`ENB:n` 이 되고, 생성기는 그 장비에
#: 물린 셀의 호만 변형한다(`pemdb1` 의 `active_cells_cache` 조건).
#:
#: 장비 배정 근거(사용자 결정 2026-10-28):
#:   * MME·SGW 장애는 그 장비에서 난다.
#:   * S6a 인증 거절은 MME–HSS 구간 문제라 그 아래 SGW·eNB 도 함께 받는다.
#:   * DNS 열화·외부 서버 5xx 는 SGW(사용자 평면 경로) 단위.
#:     **외부 서버 5xx(6번)의 자동 선택은 2026-11-05 부터 전국이다**(케이 수정안 A · 아침 확인
#:     대기). 외부 서버 장애는 그 도메인 전국 가입자에게 보이고, SGW 로 좁히면 도메인 축에서
#:     3.4% 로 묽어져(F-108 실측 · 임계 5%) 결과가 고른 SGW 의 몫에 달렸다. 장비 표시(`SGW`)와
#:     `SGW-n` 지정 경로는 남는다 — 지정하면 그 SGW 로 좁힌다(`faults._AUTO_NODE` 주석)
#:   * 단말 기종은 가입자 축인데 가입자 단위로 걸 수 없다 — SGW 아래 여러
#:     셀에 흩어져 나게 한다(`R-10` 의 "지리 흩어짐" 조건).
#:   * IMS 는 IMS PGW 문제지만 PGW 번호를 SGW 와 같게 쓰므로(구 SPGW) SGW 로 건다.
#:   * 무선·전송·NR·핸드오버 계열은 eNB.
SCENARIOS: tuple[tuple[int, str, str | None, int | None, tuple[str, ...], str], ...] = (
    (1, "SGW 자원 소진", "gw_resource", None, ("SGW",),
     "S11 Create Session 이 73(No resources)으로 거절된다 — 하류 ESM 26"),
    (2, "무선 구간 열화", "ran_radio", None, ("ENB",),
     "접속 지연 증가 + Context Setup 실패(125/126) + 무선 절단(121)"),
    (3, "SGW 노드 무응답", "gw_node_down", None, ("SGW",),
     "S11 재전송 타임아웃(First 900) — 결국 거절 응답, 하류 ESM 30"),
    (4, "인증(S6a) 거절", "auth_failure", None, ("MME", "SGW", "ENB"),
     "MME–HSS 구간 인증 거절 — 정답지 Result-Code 구성비, 해제 301"),
    (5, "DNS 열화", "dns_degradation", None, ("SGW",),
     "DNS 응답 지연·실패 증가 — 도메인 자동 선택"),
    (6, "외부 서버 5xx", "external_server", None, ("SGW",),
     "특정 도메인 HTTP 5xx 증가 — 망 지표는 정상"),
    (7, "전송망 손실", "transport_loss", None, ("ENB",),
     "down 재전송·RTT 증가 — C-Plane 은 정상"),
    (8, "특정 단말 기종", "ue_model", None, ("SGW",),
     "한 기종만 접속 실패 — SGW 아래 여러 셀에 흩어져 난다"),
    (9, "핸드오버 이상", "mobility", None, ("ENB",),
     "셀 쌍 Path Switch 실패 + 핸드오버 절단(106)"),
    (10, "MME 과부하", "mme_overload", None, ("MME",),
     "지연 증가 + Context Setup 실패·절단(500) — 확산형"),
    (11, "작업/변경 의심 (MME×APN)", "change_suspected", None, ("MME",),
     "그 MME 의 한 APN 만 S11 68 → ESM 30 — 계단형"),
    (12, "IMS PDN 이상", "ims_pdn", None, ("SGW", "PGW"),
     "IMS 접속만 실패, 데이터는 정상"),
    (13, "NSA NR leg 실패", "nsa_nr_leg", None, ("ENB",),
     "5G NSA 단말의 NR 레그 미성립"),
    (14, "도심 역 저녁 러시 Sub6 혼잡", "ran_congestion", None, ("ENB",),
     "역 주변 셀의 기존 가입자 60% 처리율·RTT 악화·정체(호는 늘지 않음) — CP 정상"),
    (15, "야마노테선 구간 4G↔5G 전환 정체", "ho_interruption", None, ("ENB",),
     "코리도 셀 쌍에서 전환 직후 3–8초 down 정체 + NR 레그 해제 — CP 정상"),
    (16, "앵커 고착 — 안테나는 서는데 느리다", "anchor_saturation", None, ("ENB",),
     "LTE 앵커 경합으로 5G 체감 저하"),
    (17, "SA→LTE 폴백 급증 (5G 측 장애 추정)", "sa_fallback", None, ("ENB",),
     "평소 SA 에 있어 안 보이던 5G 단말이 한 지역 LTE 에 TAU·Attach 로 몰려 나타난다 — "
     "5G 장비는 주장하지 않는다(시스템 간 전환 필드가 XDR 에 없다)"),
    (18, "SA UPF 자원 소진 → EPC 재현", "gw_resource", 1, ("SGW",),
     "1번과 같은 데이터 — UPF 라고 주장하지 않는다"),
    (19, "AMF 과부하 확산 → EPC 재현", "mme_overload", 10, ("MME",),
     "10번과 같은 데이터 — AMF 라고 주장하지 않는다"),
    (20, "VoNR / EPS Fallback 음성 접속 → IMS 접속 장애 재현", "ims_pdn", 12,
     ("SGW", "PGW"), "12번과 같은 데이터 — 음성 분기를 주장하지 않는다"),
    (21, "동영상 CDN 서버측 열화", "cdn_degradation", None, ("SGW",),
     "동영상 CDN 호스트 사이트 하나가 느리게 답한다 — HTTP 응답 지연·처리율 하락, 망은 정상"),
    (22, "결제 앱 응답 지연 (DNS 기인)", "pay_dns_delay", None, ("SGW",),
     "결제 등급 도메인만 DNS 응답시간 상승(일부 실패) — 도메인 패턴으로 등급 판정"),
    (23, "대형 이벤트 일시 군중 혼잡 + 업링크", "event_crowd", None, ("ENB",),
     "행사장 셀 8개 활성 가입자 3배·2시간 + 업로드 급증으로 업링크 처리율·RTT 악화"),
    # 24 는 **장애가 아니다**(2026-10-29) — 정답 원인이 `—`(효과 검증)이고
    # 판정 자리가 스펙 §8.3 투자 효과 검증이다. 그래서 채점에서 빠진다
    # (`write_truth`). 대상은 설정(`pemdb1.capacity_shift`)이 고정하므로
    # 발생 장비가 없다 — 지도에서 고른 eNB 에 걸리는 것처럼 보이면 안 된다.
    (24, "기지국 증설 후 부하 이동", "capacity_shift", None, (),
     "40분 고정 — A eNB 에 20분간 호가 몰려 혼잡 → 소등 eNB(B) 개통, "
     "A 40 · B 60 으로 나뉘며 A 회복 → 10분 분석 뒤 B 소등. 채점 제외"),
    (25, "SON 파라미터 변경 후 계단형 저하", "son_param_change", None, ("ENB",),
     "그 eNB 의 셀 쌍 핸드오버 실패가 계단으로 선다"),
    (26, "특정 단말 SW 업데이트 후 등록 실패 → 기종 장애 재현", "ue_model", 8,
     ("SGW",), "8번과 같은 데이터 — SW 버전 필드가 XDR 에 없다"),
    (27, "자연재해 — 호 폭주 + 기지국 장애", "natural_disaster", None, ("ENB",),
     "경보 지역 호 폭주(0~13분) · 지역 eNB 40% 정지(3~23분) — 세션은 이웃 셀에서 이어진다"),
)

#: **증거 없음** — 데이터에 그 장애를 판정할 근거 필드가 없어 만들지 않는 번호.
#: "준비 중"(아직 안 만들었다)과 다르다. 이쪽은 만들면 근거 없는 주장이 된다 —
#: 18~20번이 "UPF·AMF 라고 주장하지 않는다"로 남은 것과 같은 결이다.
#: 화면은 `STATUS='NO_EVIDENCE'` 를 "증거 없음"으로 그린다(2026-10-29).
#: 17번은 2026-11-05 에 빠졌다 — 시스템 간 핸드오버 대신 "숨은 5G 단말이 LTE 에 나타나는" 모양으로
#: 기존 필드(`ue_dcnr`·call_type 1/5)만 써서 만든다(사용자 승인 대안)
NO_EVIDENCE = frozenset()

#: `CM_ERROR_SCENARIO` 의 장비 마스크 컬럼 — 순서가 곧 화면 표기 순서다.
SCENARIO_EQUIP = ("MME", "SGW", "PGW", "ENB")

#: 상태 전이: pending → active → done. cancelled 는 pending/active 어디서든.
DDL = f"""
-- ── CM_DATAGEN_CONFIG ───────────────────────────────────────────────────────
-- UI 가 바꾸는 생성 설정. 데몬이 틱마다 읽으므로 **다음 분부터 반영된다.**
-- 컬럼명이 `KEY`/`VALUE` 가 아닌 이유: **`KEY` 는 MySQL 예약어다**
-- (`GP_SETTING_KV` 와 같은 판단).
CREATE TABLE IF NOT EXISTS `{T_CONFIG}` (
  `CONFIG_KEY`    varchar(64)  NOT NULL         COMMENT 'enabled, rate.<계열> …',
  `CONFIG_VALUE`  varchar(255) NOT NULL,
  `UPDATED_TIME`  datetime     NOT NULL,
  PRIMARY KEY (`CONFIG_KEY`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb3 COLLATE=utf8mb3_general_ci
  COMMENT='생성기 런타임 설정 KV — UI 가 쓰고 데몬이 틱마다 읽는다';

-- ── CM_DATAGEN_FAULT ────────────────────────────────────────────────────────
-- 장애 주입 지시 + 그 이력. 상태 전이는 **DB 에서** 일어난다(`poll_transitions`)
-- — 데몬이 죽었다 떠도 진행 중이던 장애가 그대로 이어진다.
--
-- `FAULT_ID` 가 `AUTO_INCREMENT` 인 것은 이 DB 에서 예외다. 다른 테이블이
-- 피하는 이유는 멱등 upsert(`REPLACE INTO` 가 행을 지우고 새로 넣는다)와
-- 충돌하기 때문인데, 이 테이블은 `UPDATE` 로 상태만 바꾸므로 해당하지 않는다.
-- UI·CLI 가 이 번호로 행을 지목한다(취소·사유 기록).
--
-- `MODE` 를 쓰지 않고 `INJECT_MODE` 인 것은 `sql_mode` 류와 섞여 읽히는 것을
-- 피하려는 것이다.
CREATE TABLE IF NOT EXISTS `{T_FAULT}` (
  `FAULT_ID`      int          NOT NULL AUTO_INCREMENT COMMENT '주입 지시 순번 — UI·CLI 의 핸들',
  `FAULT_KIND`    varchar(32)  NOT NULL         COMMENT 'control.KINDS — scenarios.MUTATORS 키와 1:1',
  `TARGET`        varchar(64)      DEFAULT NULL COMMENT 'SGW-3|MME-1|PGW-2|SGG:11680|NULL(자동)',
  `START_TIME`    datetime     NOT NULL,
  `DURATION_MIN`  int          NOT NULL DEFAULT 30,
  `INTENSITY`     double       NOT NULL DEFAULT 1.0,
  `INJECT_MODE`   varchar(8)   NOT NULL DEFAULT 'manual'  COMMENT 'manual | auto',
  `STATUS`        varchar(12)  NOT NULL DEFAULT 'pending' COMMENT 'pending → active → done · cancelled',
  `NOTE`          varchar(255)     DEFAULT NULL COMMENT '취소 사유 등 — UI 가 보여준다',
  `CREATED_TIME`  datetime     NOT NULL,
  `UPDATED_TIME`  datetime     NOT NULL,
  PRIMARY KEY (`FAULT_ID`),
  KEY `IX_CM_DATAGEN_FAULT_STATUS` (`STATUS`, `START_TIME`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb3 COLLATE=utf8mb3_general_ci
  COMMENT='장애 주입 지시·이력 — 이력이 곧 ground truth';

-- ── CM_DATAGEN_HISTORY ──────────────────────────────────────────────────────
-- 틱 이력. **분 단위로 멱등하다** — `record_tick` 이 DELETE→INSERT 로 맞춘다.
-- UNIQUE 키를 걸지 않는 이유는 그 함수 주석에 있다.
CREATE TABLE IF NOT EXISTS `{T_HISTORY}` (
  `HISTORY_ID`    int      NOT NULL AUTO_INCREMENT,
  `EVENT_TIME`    datetime NOT NULL             COMMENT '분 슬롯 — PM_COR_*_1M.EVENT_TIME 과 같은 축',
  `RECORD_CNT`    int      NOT NULL             COMMENT '그 분에 생성한 C-Plane 호 수',
  `FAULT_IDS`     varchar(255) DEFAULT NULL     COMMENT '그 분에 적용된 FAULT_ID 목록 (쉼표)',
  `SERIES_STAT`   json         DEFAULT NULL     COMMENT '계열별 {{n, bytes}} — 제어 화면 툴팁용',
  `CREATED_TIME`  datetime NOT NULL,
  PRIMARY KEY (`HISTORY_ID`),
  KEY `IX_CM_DATAGEN_HISTORY_SLOT` (`EVENT_TIME`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb3 COLLATE=utf8mb3_general_ci
  COMMENT='분 단위 생성 이력 — 제어 화면의 인입 현황';

-- ── CM_ERROR_SCENARIO ───────────────────────────────────────────────────────
-- 장애 시나리오 고정 목록. **정본은 `control.SCENARIOS` 이고 `ensure_tables`
-- 가 매번 덮어쓴다** — 손으로 고친 값은 다음 기동에 사라진다. 화면·지도는
-- 읽기만 한다. 발생 장비를 한 칸에 몰지 않고 장비마다 마스크(`*_YN`)로 둔다
-- — 다른 화면이 "이 장비에 걸 수 있는 시나리오" 를 `WHERE SGW_YN='Y'` 로 고른다.
CREATE TABLE IF NOT EXISTS `{T_SCENARIO}` (
  `SCENARIO_NO`    int          NOT NULL             COMMENT '화면 번호 1~27',
  `SCENARIO_NAME`  varchar(100) NOT NULL,
  `FAULT_KIND`     varchar(32)      DEFAULT NULL     COMMENT '주입 API 의 kind (CM_DATAGEN_FAULT.FAULT_KIND). NULL = 준비 중',
  `SAME_AS_NO`     int              DEFAULT NULL     COMMENT '같은 데이터를 쓰는 원본 시나리오 번호 (DOWNGRADE)',
  `STATUS`         varchar(12)  NOT NULL             COMMENT 'READY | PENDING | NO_EVIDENCE',
  `MME_YN`         varchar(1)   NOT NULL DEFAULT 'N' COMMENT 'MME 에 걸 수 있다 → TARGET MME-n',
  `SGW_YN`         varchar(1)   NOT NULL DEFAULT 'N' COMMENT 'SGW 에 걸 수 있다 → TARGET SGW-n',
  `PGW_YN`         varchar(1)   NOT NULL DEFAULT 'N' COMMENT 'PGW 에 걸 수 있다 → TARGET PGW-n',
  `ENB_YN`         varchar(1)   NOT NULL DEFAULT 'N' COMMENT 'eNB 에 걸 수 있다 → TARGET ENB:n',
  `DESCRIPTION`    varchar(500)     DEFAULT NULL,
  `UPDATED_TIME`   datetime     NOT NULL,
  PRIMARY KEY (`SCENARIO_NO`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb3 COLLATE=utf8mb3_general_ci
  COMMENT='장애 시나리오 고정 목록 · 발생 장비 마스크 — 정본 control.SCENARIOS';
CREATE TABLE IF NOT EXISTS `{T_APP}` (
  `APP_IDX`         varchar(20)  NOT NULL             COMMENT '앱 코드 → XDR App_ID',
  `ENDPOINT_SEQ`    int          NOT NULL DEFAULT 1   COMMENT '앱 안의 엔드포인트 번호 — 앱 하나에 IP·포트가 여럿일 수 있다',
  `APP_NAME`        varchar(100) NOT NULL,
  `GROUP_CODE`      varchar(50)  NOT NULL             COMMENT '앱 그룹 → XDR App_Group_Code (STREAMING·PAY·SNS…)',
  `APP_TYPE`        varchar(20)      DEFAULT NULL     COMMENT '엔드포인트 성격 (API·CDN·WEB…)',
  `PROTOCOL`        int              DEFAULT NULL     COMMENT 'IP 프로토콜 번호 (6 TCP · 17 UDP)',
  `IP`              varchar(45)      DEFAULT NULL     COMMENT 'CIDR (192.168.49.26/32) → XDR App_IPv4. NULL = 도메인으로 매핑되는 앱',
  `PORT`            int              DEFAULT NULL     COMMENT '→ XDR App_Port. NULL 이면 생성기가 443 으로 낸다',
  `DOMAIN`          varchar(500)     DEFAULT NULL     COMMENT '도메인 목록 (| 구분) — DNS 질의명·HTTP Host 를 앱에 잇는다',
  `DESCRIPTION`     varchar(500)     DEFAULT NULL,
  `WEIGHT`          double       NOT NULL DEFAULT 1   COMMENT '생성 비중 (gen 전용) — 세션이 이 앱을 고를 상대 확률',
  `USE_YN`          varchar(1)   NOT NULL DEFAULT 'Y',
  `UPDATED_TIME`    datetime     NOT NULL,
  PRIMARY KEY (`APP_IDX`, `ENDPOINT_SEQ`),
  KEY `IX_APP_ENDPOINT` (`PROTOCOL`, `IP`, `PORT`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb3 COLLATE=utf8mb3_general_ci
  COMMENT='앱 사전 — Protocol·IP·Port 또는 도메인 → 앱. 사람이 관리한다(apps.import_csv)';
"""


#: **`AS` 로 구 키 이름을 되돌린다.** 소비자(`ui`·`cli`·`faults`·`pemdb1`)가
#: 커서 `description` 을 그대로 dict 키로 받아 `f["kind"]`·`f["start_ts"]` 로
#: 읽는다. DB 컬럼명만 관례에 맞추고 **코드 계약은 건드리지 않는다** — 양쪽을
#: 함께 바꾸면 변경 범위가 네 파일로 번지고, 한 곳을 빠뜨리면 `KeyError` 가
#: 런타임에야 나온다.
FAULT_SELECT = (
    "SELECT FAULT_ID AS fault_id, FAULT_KIND AS kind, TARGET AS target, "
    "START_TIME AS start_ts, DURATION_MIN AS duration_min, "
    "INTENSITY AS intensity, INJECT_MODE AS mode, STATUS AS status, "
    "NOTE AS note, CREATED_TIME AS created_at, UPDATED_TIME AS updated_at"
)

#: 틱 루프가 쓰는 짧은 형태 — 해석에 필요한 여섯 컬럼만.
FAULT_SELECT_ACTIVE = (
    "SELECT FAULT_ID AS fault_id, FAULT_KIND AS kind, TARGET AS target, "
    "START_TIME AS start_ts, DURATION_MIN AS duration_min, INTENSITY AS intensity"
)


#: 나중에 더한 컬럼. **`CREATE TABLE IF NOT EXISTS` 는 기존 테이블을 손대지
#: 않는다** — 새로 만든 환경에는 있고 기존 환경에는 없는 상태가 되어, 그쪽에서
#: `INSERT` 가 `1054 Unknown column` 으로 깨진다. 테이블마다 (컬럼, 정의)를
#: 적어 두고 `ensure_tables` 가 없으면 더한다.
#:
#: MySQL 8 에는 `ADD COLUMN IF NOT EXISTS` 가 없다(MariaDB 전용). 그래서
#: `information_schema` 로 먼저 본다.
_ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    (T_HISTORY, "SERIES_STAT",
     "json DEFAULT NULL COMMENT '계열별 {n, bytes} — 제어 화면 툴팁용'"),
)


def ensure_tables(conn) -> None:
    with conn.cursor() as cur:
        for stmt in DDL.split(";"):
            if stmt.strip():
                cur.execute(stmt)
        for table, column, ddl in _ADDED_COLUMNS:
            cur.execute(
                "SELECT COUNT(*) FROM information_schema.columns "
                "WHERE table_schema = DATABASE() AND table_name = %s "
                "AND column_name = %s", [table, column])
            if not cur.fetchone()[0]:
                cur.execute(f"ALTER TABLE `{table}` ADD COLUMN `{column}` {ddl}")
    seed_scenarios(conn)
    # 앱 사전은 **넣지 않는다** — 원본이 사람이 준 CSV 다(`apps.import_csv`)
    conn.commit()


def seed_scenarios(conn) -> None:
    """`SCENARIOS` 를 `CM_ERROR_SCENARIO` 에 **그대로** 맞춘다.

    목록에서 빠진 번호는 지운다 — 남기면 화면에 유령 시나리오가 뜬다.
    """
    now = datetime.now()
    rows = []
    for no, name, kind, same, equip, desc in SCENARIOS:
        if kind is not None and kind not in KINDS:
            raise ValueError(f"SCENARIOS #{no}: 모르는 kind {kind!r}")
        rows.append([no, name, kind, same, ("READY" if kind
                     else "NO_EVIDENCE" if no in NO_EVIDENCE else "PENDING"),
                     *("Y" if e in equip else "N" for e in SCENARIO_EQUIP),
                     desc, now])
    with conn.cursor() as cur:
        cur.executemany(
            f"REPLACE INTO {T_SCENARIO} (SCENARIO_NO, SCENARIO_NAME, FAULT_KIND, "
            "SAME_AS_NO, STATUS, MME_YN, SGW_YN, PGW_YN, ENB_YN, DESCRIPTION, "
            "UPDATED_TIME) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", rows)
        cur.execute(
            f"DELETE FROM {T_SCENARIO} WHERE SCENARIO_NO NOT IN "
            f"({','.join(['%s'] * len(rows))})", [r[0] for r in rows])


def list_scenarios(conn) -> list[dict]:
    """화면·지도용 — 번호 순."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT SCENARIO_NO AS no, SCENARIO_NAME AS name, FAULT_KIND AS kind, "
            "SAME_AS_NO AS same_as, STATUS AS status, MME_YN AS mme, "
            "SGW_YN AS sgw, PGW_YN AS pgw, ENB_YN AS enb, DESCRIPTION AS description "
            f"FROM {T_SCENARIO} ORDER BY SCENARIO_NO")
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def add_fault(conn, kind: str, target: str | None, start_ts: datetime,
              duration_min: int = 30, intensity: float = 1.0,
              mode: str = "manual", note: str | None = None) -> int:
    if kind not in KINDS:
        raise ValueError(f"알 수 없는 kind: {kind!r} — {', '.join(KINDS)}")
    if kind == "capacity_shift":
        # **길이를 여기서 고정한다** — `faults.build` 에서만 `SHIFT_TOTAL` 로 늘리면
        # `poll_transitions` 가 행의 `DURATION_MIN`(화면 기본 10분)으로 장애를
        # 끝내, B 가 켜지는 순간 시나리오가 내려간다. 화면·지도·CLI 어느
        # 경로로 들어와도 이 함수를 지난다.
        from .scenarios import SHIFT_TOTAL
        duration_min = SHIFT_TOTAL
    target = _check_like(conn, kind, target)
    now = datetime.now()
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO {T_FAULT} (FAULT_KIND,TARGET,START_TIME,DURATION_MIN,"
            "INTENSITY,INJECT_MODE,STATUS,NOTE,CREATED_TIME,UPDATED_TIME) "
            "VALUES (%s,%s,%s,%s,%s,%s,'pending',%s,%s,%s)",
            [kind, target, start_ts, duration_min, intensity, mode, note, now, now],
        )
        fid = cur.lastrowid
    conn.commit()
    return fid


def _check_like(conn, kind: str, target: str | None) -> str | None:
    """`LIKE:<fault_id>` 를 받기 전에 원본을 확인한다 (2026-11-02 · 카드 15r2).

    해석(`faults.build`)은 DB 를 보지 않으므로 여기서 막는다 — 틀린 원본을
    받아 두면 주입 시각에 대상이 엉뚱하게 해석되고 에러가 나지 않는다.

    * 원본이 없으면 거부
    * kind 가 다르면 거부 — kind 마다 대상 해석 함수가 달라 같은 rng 로도
      다른 셀이 나온다
    * 원본이 **자동 대상**(TARGET 비어 있음)이 아니면 거부 — 그때는 원본의
      TARGET 을 그대로 쓰면 된다

    **저장할 대상 문자열을 돌려준다**(2026-11-09). 15번(범위 kind)은 —

    * 대상이 비면 `ROUTE%:<기본>` 으로 적는다. 비어 있는 15번 행은 **범위 도입 전 회차**만 남게 해서
      `LIKE:` 가 그 원본을 예전 연속 구간으로 재현할 수 있게 한다.
    * 원본이 `ROUTE%:N` 이면 자동 대상으로 보고 `ROUTE%:N:LIKE:<원본>` 으로 적는다 — 해석(`faults.build`)은
      DB 를 보지 않으므로 원본 범위를 대상 문자열에 실어야 같은 셀이 나온다. 다른 범위를 함께 주면 거부한다.
    """
    from .faults import LIKE_PREFIX, ROUTE_RANGE_KINDS, like_fault_id, route_range_of
    from .scenarios import ROUTE_RANGE_DEFAULT

    text = (target or "").strip()
    ranged = kind in ROUTE_RANGE_KINDS
    pct = None
    if ranged:
        pct, rest = route_range_of(text)
        if not rest:
            return f"ROUTE%:{pct if pct is not None else ROUTE_RANGE_DEFAULT}"
        if pct is not None:
            text = rest
    if not text.upper().startswith(LIKE_PREFIX):
        return target if pct is None else f"ROUTE%:{pct}:{text}"
    src = like_fault_id(text)
    if src is None:
        raise ValueError(f"LIKE 형식 오류: {text!r} — LIKE:<fault_id>")
    with conn.cursor() as cur:
        cur.execute(f"SELECT FAULT_KIND, TARGET FROM {T_FAULT} WHERE FAULT_ID=%s", [src])
        row = cur.fetchone()
    if row is None:
        raise ValueError(f"LIKE 원본 fault {src} 가 없다")
    if row[0] != kind:
        raise ValueError(f"LIKE 원본 fault {src} 의 kind 는 {row[0]!r} — {kind!r} 와 다르다")
    orig = (row[1] or "").strip()
    if ranged:
        o_pct, o_rest = route_range_of(orig)
        if o_rest:
            raise ValueError(
                f"LIKE 원본 fault {src} 는 대상 {row[1]!r} 를 지정했다 — 그 값을 그대로 쓴다")
        if pct is not None and o_pct is not None and pct != o_pct:
            raise ValueError(f"LIKE 원본 fault {src} 의 범위는 {o_pct}% — {pct}% 로는 같은 셀이 나오지 않는다")
        if pct is not None and o_pct is None:
            raise ValueError(f"LIKE 원본 fault {src} 는 범위 도입 전 회차다 — 범위를 주지 않는다")
        # 범위 원본은 범위를 싣고, 빈 원본(범위 도입 전)은 그대로 — 해석이 예전 연속 구간으로 간다
        return f"ROUTE%:{o_pct}:LIKE:{src}" if o_pct is not None else f"LIKE:{src}"
    if orig:
        raise ValueError(
            f"LIKE 원본 fault {src} 는 대상 {row[1]!r} 를 지정했다 — 그 값을 그대로 쓴다")
    return target


def cancel_fault(conn, fault_id: int) -> bool:
    with conn.cursor() as cur:
        n = cur.execute(
            f"UPDATE {T_FAULT} SET STATUS='cancelled', UPDATED_TIME=%s "
            "WHERE FAULT_ID=%s AND STATUS IN ('pending','active')",
            [datetime.now(), fault_id],
        )
    conn.commit()
    return n > 0


def list_faults(conn, limit: int = 50) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(FAULT_SELECT + f" FROM {T_FAULT} "
                    "ORDER BY FAULT_ID DESC LIMIT %s", [limit])
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def poll_transitions(conn, now: datetime) -> list[dict]:
    """상태 전이를 수행하고 **현재 활성** fault 목록을 돌려준다.

    데몬이 틱마다 부른다. 전이는 DB 에서 일어나므로 데몬이 죽었다 떠도
    진행 중이던 장애가 그대로 이어진다.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE {T_FAULT} SET STATUS='active', UPDATED_TIME=%s "
            "WHERE STATUS='pending' AND START_TIME <= %s", [now, now])
        cur.execute(
            f"UPDATE {T_FAULT} SET STATUS='done', UPDATED_TIME=%s "
            "WHERE STATUS='active' "
            "AND DATE_ADD(START_TIME, INTERVAL DURATION_MIN MINUTE) <= %s",
            [now, now])
        cur.execute(FAULT_SELECT_ACTIVE + f" FROM {T_FAULT} "
                    "WHERE STATUS='active' ORDER BY FAULT_ID")
        cols = [d[0] for d in cur.description]
        active = [dict(zip(cols, r)) for r in cur.fetchall()]
    conn.commit()
    return active


def record_tick(conn, slot_ts: datetime, records: int,
                faults: str | None = None,
                series_stat: dict | None = None) -> None:
    """틱 이력을 **분 단위로 멱등하게** 남긴다.

    생성 자체는 멱등이다(`PemdbWriter.write_xdr` 이 DELETE→INSERT). 그런데
    이력을 그냥 INSERT 하면 같은 분을 두 번 돌릴 때 행이 쌓이고, 제어 화면의
    인입 현황이 `SUM(records)` 를 쓰므로 **실제 생성량의 2배·3배를 보고한다** —
    실측으로 15:00 한 분이 3행 쌓여 19,059건이 57,177건으로 보였다. 에러는
    없고 숫자만 틀리는 형태다.

    UNIQUE 키를 걸지 않고 DELETE→INSERT 로 맞춘다. 키를 추가하면 이미 만들어진
    테이블에는 `CREATE TABLE IF NOT EXISTS` 가 손대지 못해 한쪽만 적용되고,
    그 상태에서 평범한 INSERT 가 남아 있으면 `1062 Duplicate entry` 로 데몬이
    죽는다.
    """
    with conn.cursor() as cur:
        cur.execute(f"DELETE FROM {T_HISTORY} WHERE EVENT_TIME=%s", [slot_ts])
        cur.execute(
            f"INSERT INTO {T_HISTORY} "
            "(EVENT_TIME, RECORD_CNT, FAULT_IDS, SERIES_STAT, CREATED_TIME) "
            "VALUES (%s,%s,%s,%s,%s)",
            [slot_ts, records, faults or None,
             json.dumps(series_stat, separators=(",", ":")) if series_stat else None,
             datetime.now()])
    conn.commit()


def get_config(conn, k: str, default: str | None = None) -> str | None:
    with conn.cursor() as cur:
        cur.execute(f"SELECT CONFIG_VALUE FROM {T_CONFIG} WHERE CONFIG_KEY=%s", [k])
        row = cur.fetchone()
    return row[0] if row else default


#: 하한 꼬리가 끝나는 시각(fault 별 JSON) — 데몬이 쓰고 `/api/faults` 가 읽는다
FLOOR_TAIL_KEY = "floor_tail"


def floor_tail_value(until: dict[str, str]) -> str:
    """`CONFIG_VALUE`(varchar 255)에 들어가게 접는다 — 넘치면 **최근 fault 부터** 남긴다.

    오래된 fault 를 버리는 것은 그쪽 꼬리가 먼저 끝나기 때문이다.
    """
    items = sorted(until.items(), key=lambda kv: int(kv[0]), reverse=True)
    while True:
        v = json.dumps(dict(sorted(items, key=lambda kv: int(kv[0]))),
                       separators=(",", ":"))
        if len(v) <= 255 or not items:
            return v
        items = items[:-1]


def set_config(conn, k: str, v: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO {T_CONFIG} (CONFIG_KEY,CONFIG_VALUE,UPDATED_TIME) "
            "VALUES (%s,%s,%s) ON DUPLICATE KEY UPDATE "
            "CONFIG_VALUE=VALUES(CONFIG_VALUE), UPDATED_TIME=VALUES(UPDATED_TIME)",
            [k, v, datetime.now()])
    conn.commit()
