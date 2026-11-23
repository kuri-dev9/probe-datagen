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
#: 지진 정보 — 27번 시나리오가 시작될 때 생성기가 한 행을 쓴다(2026-11-13 사용자 승인 DDL)
T_QUAKE = "CM_QUAKE_EVENT"
#: 앱 사전 — 정의와 적재는 `apps` 가 갖고 DDL 만 여기 모은다
from .apps import T_APP  # noqa: E402
#: 27번 국면 시각은 생성기 상수가 정본이다 — 설명이 따로 숫자를 들고 있으면 국면을 바꿀 때 DB 설명만 옛 시각으로
#: 남는다(2026-11-16 정지를 eNB 별 복구로 줄이며 「3~23분」이 그렇게 남았다). 일본어 칸(`i18n_ja`)도 이 문장을 쓴다
from .scenarios import (DISASTER_DOWN_AT, DISASTER_DOWN_END, DISASTER_SURGE_END,  # noqa: E402
                        DISASTER_UP_FROM, SHIFT_RECOVER, SHIFT_RISE, SHIFT_TOTAL)
QUAKE_DESC = (
    "도쿄 둘레 실제 진원역(바다·땅속 깊은 곳)에서 규모 4.5~6.2 지진이 납니다 — 진도 3 이상 넓은 지역에서 흔들림이 셀수록 "
    f"통화가 많이 몰리고(진도 3 약 4배 ~ 진도 5강 이상 40배 · 0~{DISASTER_SURGE_END}분) 진도 5약 이상인 무른 땅의 기지국만 일부 꺼집니다({DISASTER_DOWN_AT}분 정지 · "
    f"eNB 마다 {DISASTER_UP_FROM}~{DISASTER_DOWN_END}분 사이 따로 복구 · {DISASTER_DOWN_END}분 정상화 · "
    "0곳일 수 있습니다) · CM_QUAKE_EVENT")
QUAKE_DESC_JA = (
    "東京周辺の実際の震源域(海域・地下深く)でM4.5~6.2の地震が起きます — 震度3以上の広い地域で揺れが強いほど通話が多く集中し"
    f"(震度3 約4倍 ~ 震度5強以上 40倍 · 0~{DISASTER_SURGE_END}分)、震度5弱以上の軟弱地盤の基地局だけが一部停止します({DISASTER_DOWN_AT}分に停止 · "
    f"eNBごとに{DISASTER_UP_FROM}~{DISASTER_DOWN_END}分の間で個別に復旧 · {DISASTER_DOWN_END}分に正常化 · "
    "0局の場合もあります) · CM_QUAKE_EVENT")

#: 24번 설명 — 국면 길이를 **생성기 상수에서** 만든다(2026-11-17 · 27번 `QUAKE_DESC` 와 같은 이유). 전에는
#: 문장에 「50분 고정 · 30분간」을 적어 두고 일본어 사전 키는 옛 「30분 고정 · 10분간」이라, 길이를 바꿀 때마다
#: 화면 설명이 실제와 갈리고 일본어 번역도 조용히 빠졌다
SHIFT_DESC = (
    f"{SHIFT_TOTAL}분 고정 — A eNB 에 {SHIFT_RISE}분간 호가 몰려 혼잡 → 소등 eNB(B) 개통, "
    f"A 40 · B 60 으로 나뉘며 A 회복({SHIFT_RECOVER}분) → {SHIFT_TOTAL - SHIFT_RISE - SHIFT_RECOVER}분 분석 뒤 "
    "B 소등. 채점 제외")
SHIFT_DESC_JA = (
    f"{SHIFT_TOTAL}分固定 — A eNBに{SHIFT_RISE}分間呼が集中して混雑 → 消灯eNB(B)開通、"
    f"A 40 · B 60に分かれてA回復({SHIFT_RECOVER}分) → {SHIFT_TOTAL - SHIFT_RISE - SHIFT_RECOVER}分の分析後に"
    "B消灯。採点対象外")

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
    "anchor_saturation", "ran_congestion",
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
    # 31·32번 (2026-11-19 · 처음 33·34 로 넣었다가 당겼다)
    "ran_drop_surge", "transport_cut",
    # 28 ②·33·34·35번 (2026-11-19 · HTTP·DNS 신규)
    "dns_nxdomain", "service_4xx", "service_tls_fail", "ipv6_path_fail", "update_surge",
    # 36번 신호 폭주 (2026-11-21)
    "signaling_storm",
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
#:     **DNS 열화(5번)·CDN(21번)은 2026-11-23 부터 PGW 다**(사용자 결정 1 · DNS·CDN 은 외부망 관문 뒤). SPGW 1:1 이라
#:     걸리는 셀은 같다. 결제 DNS(22번)·단말 기종(8번)의 자동 선택은 같은 날부터 전국이다(`faults._AUTO_NODE`)
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
    (5, "DNS 열화", "dns_degradation", None, ("PGW",),
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
    (14, "역 주변 저녁 러시 혼잡", "ran_congestion", None, ("ENB",),
     "역 주변 셀에 사람이 1.8배 몰리고 가입자 60% 처리율·RTT 악화·정체 — CP 정상"),
    (15, "야마노테선 4G↔5G 전환 때 잠깐 멈춤", "ho_interruption", None, ("ENB",),
     "코리도 셀 쌍에서 전환 직후 3–8초 down 정체 + NR 레그 해제 — CP 정상"),
    (16, "5G 잡히는데 느림 (4G 앵커 혼잡)", "anchor_saturation", None, ("ENB",),
     "LTE 앵커 경합으로 5G 체감 저하"),
    (17, "5G 장애로 4G에 몰림", "sa_fallback", None, ("ENB",),
     "평소 SA 에 있어 안 보이던 5G 단말이 한 지역 LTE 에 TAU·Attach 로 몰려 나타난다 — "
     "5G 장비는 주장하지 않는다(시스템 간 전환 필드가 XDR 에 없다)"),
    (18, "5G 데이터 관문(UPF) 자원 바닥", "gw_resource", 1, ("SGW",),
     "1번과 같은 데이터 — UPF 라고 주장하지 않는다"),
    (19, "5G 접속 관리(AMF) 과부하 확산", "mme_overload", 10, ("MME",),
     "10번과 같은 데이터 — AMF 라고 주장하지 않는다"),
    (20, "음성 통화만 안 걸림 (VoNR)", "ims_pdn", 12,
     ("SGW", "PGW"), "12번과 같은 데이터 — 음성 분기를 주장하지 않는다"),
    (21, "동영상 서버(CDN) 느림", "cdn_degradation", None, ("PGW",),
     "동영상 CDN 호스트 사이트 하나에 붙지 못한다 — TCP 연결 실패(SYN 무응답)·업 방향 지연, 다운 방향은 정상"),
    (22, "결제 앱 느림 (DNS 지연)", "pay_dns_delay", None, ("SGW",),
     "결제 등급 도메인만 DNS 응답시간 상승(일부 실패) — 도메인 패턴으로 등급 판정"),
    (23, "행사장 특정 앱 먹통", "event_crowd", None, ("ENB",),
     "행사장 셀 활성 가입자 3배 + 관객이 같은 게임 앱 하나를 켜 그 게임만 내려받기가 끊긴다(정체 · 2026-11-21)"),
    # 24 는 **장애가 아니다**(2026-10-29) — 정답 원인이 `—`(효과 검증)이고
    # 판정 자리가 스펙 §8.3 투자 효과 검증이다. 그래서 채점에서 빠진다
    # (`write_truth`). 대상은 설정(`pemdb1.capacity_shift`)이 고정하므로
    # 발생 장비가 없다 — 지도에서 고른 eNB 에 걸리는 것처럼 보이면 안 된다.
    (24, "같은 지역 같은 시간 혼잡 발생", "capacity_shift", None, (), SHIFT_DESC),
    (25, "설정 변경 뒤 통화 끊김 계단 증가 (SON)", None, None, ("ENB",),
     "기지국(eNB) 1~2곳의 핸드오버 설정이 바뀐 시각부터 그 기지국의 모든 셀에서 옆 셀로 넘어가는 통화가 "
     "끊기기 시작한다 — 셀마다 정도가 다르고 같은 분에 함께 나빠진다"),
    (26, "SW 업데이트한 기종만 접속 실패", "ue_model", 8,
     ("SGW",), "8번과 같은 데이터 — SW 버전 필드가 XDR 에 없다"),
    (27, "지진 — 통화 몰림·기지국 정지", "natural_disaster", None, ("ENB",), QUAKE_DESC),
    # ── 28~35 신규 (2026-11-19 사용자 결정 · 14~27 은 그대로 두고 덧붙인다) ──────────────────
    # 28~30 은 11-13 보류안(stash · V3 SC-20·19·21)의 생성기를 그대로 쓴다. 3·4번과 같은 데이터인 SGW 무응답·
    # 인증 거절은 넣지 않았다(사용자 11-19 「기존과 같으면 가져갈 필요 없다」 — 1~13 을 빼면 함께 뺀다).
    # 자동 대상은 28·29 전국(`faults._auto_target` 끝 — APN 은 `generator._bind_scenario_targets`), 30·31 한
    # 시군구 전 셀(`_AUTO_SGG`), 32 한 지역 셀 25개가 걸친 eNB 전부(`_AREA_KINDS`)다. 28 의 DNS 계층 구분은
    # 다음 단계다. 33~35 는 HTTP·DNS 신규다(대상 앱은 `generator._SERVICE_KINDS`·`_UPDATE_KINDS` · 34 는 PGW)
    # 28 은 두 갈래다 — ① 전국 간헐(`dns_flap` · 이 행) ② 결제 외 한 서비스 「없는 주소」(`dns_nxdomain`).
    # 한 행에 kind 가 하나라 ② 는 주입 API 에 kind 를 직접 주고 시나리오 번호 28 을 함께 넘긴다(33 ② 도 같다)
    (28, "DNS 접속 실패", "dns_flap", None, ("SGW",),
     "DNS 가 3분 나빠지고 3분 회복하기를 되풀이한다 — 같은 장애가 한두 건으로 묶이는지 본다"),
    (29, "특정 APN 접속 실패", "apn_pdn_failure", None, ("SGW",),
     "상위 APN 하나만 전국에서 PDN 접속 실패(S11 78→ESM 27 · S11 93→ESM 33) — 같은 MME·SGW 의 다른 APN 은 정상"),
    (30, "복합 장애 (전송 → DNS → HTTP)", "compound_causal_failure", None, ("ENB",),
     "한 시군구 전송 손실(0분) → DNS 실패(2분) → HTTP 감소(4분) — 1차 원인 전송을 고르는지 본다"),
    (31, "절단 급증", "ran_drop_surge", None, ("ENB",),
     "한 시군구 셀에서 연결된 호가 무선 사유(121)로 끊긴다 — 접속(C-02)·지연은 평소대로, 절단률(C-04)만 오른다"),
    (32, "전송 구간 절단", "transport_cut", None, ("ENB",),
     "한 지역 기지국 묶음이 같은 분에 모두 멈췄다가(15분) 같은 분에 함께 돌아오고, 복구 직후 재접속이 몰린다 — 호 폭주 없음"),
    (33, "특정 서비스 접속 거절", "service_4xx", None, ("SGW",),
     "결제 외 한 서비스가 HTTP 401·403(일부 429)으로 곧바로 거절한다 — 망·DNS·5xx·응답시간은 정상 "
     "(② 보안 연결 실패는 `service_tls_fail`)"),
    (34, "IPv6 경로만 실패", "ipv6_path_fail", None, ("PGW",),
     "PGW 하나의 IPv6 세션만 서지 않고 단말이 몇 초 뒤 IPv4 로 다시 붙는다 — 끊김이 아니라 접속 지연"),
    (35, "앱 업데이트 배포 폭주", "update_surge", None, ("SGW",),
     "업데이트 배포 서비스 하나의 세션이 3배·내려받기 양이 10배로 전국에서 몰린다 — 지역 편중 없음"),
    # 36 신호 폭주(2026-11-21 사용자 추가 결정) — 생성기 `scenarios._signaling_storm`(같은 날). 화면 순서 11번(11-22 3번→11번)
    (36, "신호 폭주 (재접속 폭주)", "signaling_storm", None, ("MME",),
     "장애 복구 직후 등 단말이 한꺼번에 다시 접속해 접속·인증·위치 갱신·음성이 줄줄이 실패한다"),
)

#: **화면 순서와 쉬운 이름** (2026-11-21 사용자 확정 · `graphio-probe-rca/docs/jp/시나리오-정리-초안-1121.md` §8).
#:
#: `(지금 번호, 이름, 名前, 한 줄 설명, 一行説明)` — 줄 순서가 곧 화면 순서(1~)다. **지금 번호는 내부 키로
#: 그대로다** — 정답지·회차 기록·현황 json·`CM_ERROR_SCENARIO` 가 이 번호로 잇는다. 번호를 다시 매기면
#: `seed_scenarios` 가 표에 없는 행을 지운다. 이름은 **명사형**(2026-11-21 오후 사용자 확정 — 「~요」로 끝내지 않는다 · 일본
#: 운영 화면의 장애명은 体言止め)이고, 「이용자가 겪는 일」은 한 줄 설명 앞머리로 옮겼다. 장비·약어는 설명에 둔다.
#: `seed_scenarios` 가 이 이름·설명을 `SCENARIO_NAME`·`DESCRIPTION` 에 쓴다(위 `SCENARIOS` 의 이름·설명은
#: 내부 이름·상세로 남는다). 일본어는 gen-ui 번역 사전이 이 표에서 받는다(`ui.i18n_ja`). 폐기 번호는 없다
SCENARIO_ORDER: tuple[tuple[int, str, str, str, str], ...] = (
    (14, "퇴근길 역 앞 속도 저하", "帰宅ラッシュ時の駅前速度低下",
     "퇴근 시간에 역 앞에서 인터넷이 느려집니다 — 저녁 러시에 역 주변 셀이 붐빕니다",
     "帰宅時間に駅前でネットが遅くなります — 夕方のラッシュで駅周辺のセルが混雑します"),
    (23, "행사장 특정 앱 먹통", "イベント会場の特定アプリ不通",
     "행사장에 모인 관객이 같은 게임을 켜서 그 게임만 내려받기가 끊겨 먹통이 됩니다 — 다른 앱은 정상",
     "イベント会場に集まった観客が同じゲームを起動し、そのゲームだけダウンロードが途切れて使えなくなります — 他のアプリは正常"),
    (15, "야마노테선 5G 불안정", "山手線5G不安定",
     "야마노테선 전철에서 5G가 잡혔다 끊겼다 합니다 — 4G↔5G 전환이 잦습니다",
     "山手線の電車で5Gがついたり消えたりします — 4G↔5Gの切り替えが多いです"),
    (27, "지진 발생", "地震発生",
     "지진으로 기지국 정지·통화 폭주·우회가 함께 옵니다",
     "地震で基地局の停止・通話の殺到・迂回が同時に起きます"),
    (5, "앱 첫 연결 실패", "アプリ初回接続失敗",
     "앱을 처음 열 때 안 열립니다 — 주소 찾기(DNS) 실패·지연",
     "アプリを最初に開くときに開きません — アドレス検索(DNS)の失敗・遅延"),
    (22, "결제 앱 지연", "決済アプリ遅延",
     "결제 앱만 느립니다 — 결제 서비스 주소 찾기가 느립니다",
     "決済アプリだけ遅いです — 決済サービスのアドレス検索が遅いです"),
    (12, "데이터 정상·음성 통화 불가", "データ正常・音声通話不可",
     "데이터는 되는데 전화만 안 됩니다 — VoLTE 음성만 실패",
     "データは使えるが電話だけつながりません — VoLTE の音声だけ失敗"),
    # 8번은 통화가 아니라 연결이 끊긴다(무선 사유 121 · C-04 는 데이터 연결 기준) — 2026-11-23 사용자 「통화가 아니면 연결 끊김 증가」
    (31, "연결 끊김 증가", "接続切断増加",
     "연결이 자꾸 끊깁니다 — 절단 급증",
     "接続がよく切れます — 切断の急増"),
    (2, "무선 품질 저하", "無線品質の低下",
     "전파가 약해 품질이 나빠집니다 — 무선 품질 열화",
     "電波が弱く品質が悪くなります — 無線品質の劣化"),
    (9, "이동 중 끊김", "移動中の切断",
     "이동 중에 끊깁니다 — 옆 셀로 넘어갈 때 실패(핸드오버)",
     "移動中に切れます — 隣のセルへ移るときに失敗(ハンドオーバー)"),
    (36, "단말 동시 재접속", "端末同時再接続",
     "장애 복구 직후 등 휴대폰이 한꺼번에 다시 붙으면서 접속·인증·전화가 줄줄이 실패합니다",
     "障害復旧の直後などに端末が一斉に再接続し、接続・認証・通話が次々に失敗します"),
    (16, "5G 표시 중 속도 저하", "5G表示中の速度低下",
     "5G라고 뜨는데 느립니다 — 5G가 기대는 4G 기지국이 붐빕니다",
     "5G表示なのに遅いです — 5Gが頼る4G基地局が混雑しています"),
    (17, "5G 불가로 4G 집중", "5G不可による4G集中",
     "5G가 안 돼서 4G로 몰립니다 — 5G 쪽 장애로 5G 단말이 한 지역 4G로 넘어옵니다",
     "5Gが使えず4Gに集中します — 5G側の障害で5G端末が一つの地域の4Gへ移ってきます"),
    (8, "특정 기종 접속 불가", "特定機種の接続不可",
     "특정 기종만 접속이 안 됩니다 — 신기종·업데이트 기종 문제",
     "特定機種だけ接続できません — 新機種・アップデートした機種の問題"),
    (6, "특정 사이트 오류", "特定サイトのエラー",
     "특정 사이트만 오류가 납니다 — 외부 서비스 5xx·4xx, 망 문제가 아님을 가립니다",
     "特定サイトだけエラーになります — 外部サービスの 5xx・4xx、網の問題ではないことを見分けます"),
    (21, "동영상 서버 접속 불가", "動画サーバ接続不可",
     "동영상 서버에 연결 자체가 안 됩니다 — CDN 접속(SYN) 실패",
     "動画サーバにそもそも接続できません — CDN への接続(SYN)の失敗"),
    (29, "특정 서비스망 접속 불가", "特定サービス網の接続不可",
     "특정 서비스 망만 접속이 안 됩니다 — 한 APN 설정·장애",
     "特定サービス網だけ接続できません — 一つの APN の設定・障害"),
    (32, "케이블 단선 지역 불통", "ケーブル断による地域不通",
     "케이블이 끊겨 한 지역이 통째로 안 됩니다 — 전송 구간 절단",
     "ケーブルが切れて地域ごと使えません — 伝送区間の切断"),
    (7, "전송망 광역 속도 저하", "伝送網の広域速度低下",
     "넓은 지역이 함께 느려집니다 — 전송망 손실",
     "広い地域がまとめて遅くなります — 伝送網の損失"),
    (1, "데이터 중계 장비 포화", "データ中継装置の飽和",
     "데이터 중계 장비(SGW) 자원이 바닥나 새 연결이 실패합니다",
     "データ中継装置(SGW)のリソースが枯渇し、新しい接続が失敗します"),
    (10, "접속 관리 장비 과부하", "接続管理装置の過負荷",
     "접속 관리 장비(MME)가 과부하라 접속이 늦거나 실패합니다",
     "接続管理装置(MME)が過負荷で接続が遅れたり失敗したりします"),
    (30, "연쇄 복합 장애", "連鎖的な複合障害",
     "여러 장애가 이어서 납니다 — 전송 → DNS → 웹 이용 감소",
     "複数の障害が連鎖します — 伝送 → DNS → Web 利用の減少"),
    (13, "5G 접속 불량", "5G接続不良",
     "5G가 잘 안 붙습니다 — 5G NSA 연결 실패",
     "5Gにつながりにくいです — 5G NSA の接続失敗"),
    (34, "IPv6 전용 접속 불가", "IPv6のみ接続不可",
     "IPv6로만 접속이 안 됩니다 — IPv6 경로 실패",
     "IPv6だけ接続できません — IPv6 経路の失敗"),
)
#: 번호 → 화면 순서(1~)
SCENARIO_RANK: dict[int, int] = {no: i + 1 for i, (no, *_rest) in enumerate(SCENARIO_ORDER)}
SCENARIO_SHOWN: dict[int, tuple[str, str, str, str]] = {no: tuple(rest) for no, *rest in SCENARIO_ORDER}


def _kind_shown() -> dict[str, str]:
    """kind → 화면 이름(2026-11-21 · 24개 정리). 한 kind 를 여러 번호가 쓰면 **화면 순서가 앞선** 번호의 이름이다
    (`ue_model` 은 8번 · 26번은 그 옵션). 폐기 번호만 쓰는 kind(`dns_flap`·`service_4xx` 등)는 여기 없다 —
    과거 장애 기록 표시용으로 옛 이름(`ui.KIND_LABELS`·`KIND_SPECS.title`)을 그대로 쓴다."""
    kind_of = {no: kind for no, _n, kind, *_ in SCENARIOS}
    out: dict[str, str] = {}
    for no, name, *_ in SCENARIO_ORDER:
        k = kind_of.get(no)
        if k and k not in out:
            out[k] = name
    return out


#: kind → 화면 이름 — 주입 화면의 종류 이름(`ui.KIND_LABELS`)과 정답지 제목(`scenarios.place`)이 여기서 받는다
KIND_SHOWN: dict[str, str] = _kind_shown()

#: **증거 없음** — 데이터에 그 장애를 판정할 근거 필드가 없어 만들지 않는 번호.
#: "준비 중"(아직 안 만들었다)과 다르다. 이쪽은 만들면 근거 없는 주장이 된다 —
#: 18~20번이 "UPF·AMF 라고 주장하지 않는다"로 남은 것과 같은 결이다.
#: 화면은 `STATUS='NO_EVIDENCE'` 를 "증거 없음"으로 그린다(2026-10-29).
#: 17번은 2026-11-05 에 빠졌다 — 시스템 간 핸드오버 대신 "숨은 5G 단말이 LTE 에 나타나는" 모양으로
#: 기존 필드(`ue_dcnr`·call_type 1/5)만 써서 만든다(사용자 승인 대안)
NO_EVIDENCE = frozenset()

#: **폐기** — 번호와 행은 남기고 주입 목록에서만 뺀다(2026-11-20 사용자 시나리오 정리). 행을 지우면
#: 옛 장애(F-xxx)의 번호·이름을 못 찾는다(`faults.scenario_no` 가 같은 kind 행으로 번호를 잇고, 화면이
#: 과거 장애에 이 표로 이름을 붙인다). 그래서 `SCENARIOS` 에서 빼지 않는다 — 빼면 `seed_scenarios` 가
#: 다음 기동 때 행을 지운다. 3(SGW 정지)·4(HSS 인증)는 삭제, 18→1·19→10·20→12·26→8·11→29·35→23 은
#: 합침, 25(SON)는 삭제다 — **25 는 kind 까지 지웠다**(2026-11-21 · 행의 `FAULT_KIND` 는 None · 옛 장애 행의
#: `son_param_change` 는 DB 에 그대로 남고 이름은 번호로 붙는다). **26 은 내부 주입 번호로 계속 쓴다** — 8번 화면의 「기종 전국」 체크가
#: `scenario_no=26` 으로 보내 `faults._MODEL_WIDE_SCENARIOS` 분기를 탄다. 28·33 은 장면을 5·6번 옵션으로 옮기고
#: 폐기했다(2026-11-21 — 5번 「간헐·없는 주소」가 28 의 kind, 6번 「4xx 거절」이 33 의 kind 를 같은 번호로 보낸다 ·
#: 26 과 같은 방식). **24 는 시나리오가 아니라 gen-ui 스위치다**(2026-11-21 사용자 「매일 같은 시간에 발생시킬지
#: ON/OFF 만」 · `daily_load`) — 그래서 주입 목록에서 뺀다
RETIRED = frozenset({3, 4, 11, 18, 19, 20, 24, 25, 26, 28, 33, 35})

#: **번호 없는 별도 시나리오**(2026-11-22 사용자) — 주입 목록에는 없지만(`RETIRED` 에 남는다 · 주입은
#: 「매일 같은 시각 부하」 스위치) 시나리오 화면에서는 폐기 묶음이 아니라 목록 끝에 번호 없이 선다.
#: 24 는 데이터가 몰렸을 때 기지국 증설의 효과를 확인하려고 둔다. 이름 「같은 지역 같은 시간 혼잡 발생」
UNNUMBERED = frozenset({24})
UNNUMBERED_NAME = {24: ("같은 지역 같은 시간 혼잡 발생", "同じ地域・同じ時間帯の混雑")}

#: `CM_ERROR_SCENARIO` 의 장비 마스크 컬럼 — 순서가 곧 화면 표기 순서다.
SCENARIO_EQUIP = ("MME", "SGW", "PGW", "ENB")

#: 상태 전이: pending → active → done. cancelled 는 pending/active 어디서든.
#: 주입 당시 조건 칸(2026-11-11 사용자 승인 DDL) — 새 환경 `DDL` 과 기존 환경 `_ADDED_COLUMNS` 가 **이 한
#: 벌**을 쓴다. 두 곳에 따로 적으면 한쪽만 고쳐져 새 환경과 기존 환경의 칸이 갈린다. 모두 NULL 허용이고
#: **NULL 은 "기록 전 장애"**다 — 0 으로 채우면 생성량 0%·오프셋 0 이라는 없는 사실이 된다(graphio 불변규칙 17).
#: 이전 행은 채우지 않는다. 주석에 `;` 를 쓰지 않는다(`ensure_tables` 가 DDL 을 `;` 로 자른다)
FAULT_COND_COLUMNS: tuple[tuple[str, str], ...] = (
    ("SCENARIO_NO",
     "int DEFAULT NULL COMMENT '시나리오 번호 (CM_ERROR_SCENARIO.SCENARIO_NO) — 생성 API 값, 없으면 메모 #n. NULL = 기록 전 장애'"),
    ("VOLUME_PCT",
     "tinyint unsigned DEFAULT NULL COMMENT '활성 전이 때의 생성량(%) — CM_DATAGEN_CONFIG.volume_pct. NULL = 기록 전 장애'"),
    ("VIRTUAL_OFFSET_SEC",
     "int DEFAULT NULL COMMENT '활성 전이 때의 가상 시각 오프셋(초) — 곡선·그룹 비율만 옮긴다. NULL = 기록 전 장애'"),
    ("RESOLVED_TARGET",
     "varchar(255) DEFAULT NULL COMMENT '생성기가 실제로 고른 대상 (정답지 scope 와 같은 글) — 예 SGW-9, MME-1 · APN · 셀 N개. NULL = 기록 전 장애'"),
    ("INJECT_CONTEXT",
     "json DEFAULT NULL COMMENT '활성 전이 때의 생성 조건 {diurnal, blocked, reassign, curve, gen_rev}. NULL = 기록 전 장애'"),
)
_FAULT_COND_DDL = "".join(f"  `{c}` {d},\n" for c, d in FAULT_COND_COLUMNS)


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
{_FAULT_COND_DDL}  `CREATED_TIME`  datetime     NOT NULL,
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

-- ── CM_QUAKE_EVENT ──────────────────────────────────────────────────────────
-- 지진 정보 (2026-11-13 사용자 승인). 27번 장애가 시작될 때 생성기가 장애 ID 시드로 정한 지진 한 건을
-- 쓴다(`cli` → `record_quakes`). 실제 지진(P2P·기상청)도 같은 모양으로 받는다 — 지진 1건 + 진앙 하나.
-- 음영 셀 목록은 두지 않는다 — 정답지(down_enbs)에 있고 이 행과 시드로 다시 셀 수 있다. graphio 가
-- 읽어도 되는 것은 운영에서도 받을 수 있는 이 지진 정보까지다(꺼진 셀은 D4 가 스스로 찾는다).
-- 2026-11-14 부터 판정이 쓰는 칸은 발생 시각·진앙·깊이·규모·진도다(graphio EV-19). SHAPE 는 늘 ROUND,
-- RADIUS_KM 는 지반 1.0 에서 진도 3 이상인 진앙 거리, BASE_PCT·MAX_CELLS 는 설정값(정지 확률 · 11-15 A안부터 미사용)·최대 셀이다 —
-- 모두 생성기 참고값이고 `NOT NULL` 이라 남긴다(`scenarios.quake_new`)
CREATE TABLE IF NOT EXISTS `{T_QUAKE}` (
  `QUAKE_ID`      varchar(32)  NOT NULL                COMMENT '지진 키 — 시나리오는 Q-F<FAULT_ID>, 실제 지진은 받은 곳의 지진 ID',
  `FAULT_ID`      int              DEFAULT NULL         COMMENT '시나리오 장애 CM_DATAGEN_FAULT.FAULT_ID — 실제 지진이면 NULL',
  `SOURCE`        varchar(16)  NOT NULL                COMMENT '출처 SCENARIO(생성기 27번) | P2P(P2P 지진정보) | JMA(기상청)',
  `ORIGIN_TIME`   datetime     NOT NULL                COMMENT '발생 시각(진원시) — EV-19 가 장애 시작 앞 quake.before_minutes ~ 뒤 after_minutes 에서 가장 늦은 한 건을 고르는 키. 시나리오는 장애 시작 시각',
  `HYPO_NAME`     varchar(64)      DEFAULT NULL         COMMENT '진원 지명 — 시나리오는 진원역 이름(기상청 진앙 지명 원문). 모르면 NULL · EV-19 는 읽지 않는다',
  `LAT`           decimal(9,6) NOT NULL                COMMENT '진앙 위도(십진 도) — EV-19 가 셀까지 진앙 거리를 잰다',
  `LON`           decimal(9,6) NOT NULL                COMMENT '진앙 경도(십진 도) — EV-19 가 셀까지 진앙 거리를 잰다',
  `DEPTH_KM`      decimal(5,1)     DEFAULT NULL         COMMENT '진원 깊이(km) — 모르면 NULL(0 아님). NULL 이면 EV-19 가 진도를 추정하지 않는다(available=false). 11-14 이전 시나리오 행은 NULL',
  `MAGNITUDE`     decimal(3,1)     DEFAULT NULL         COMMENT '규모(M) — 모르면 NULL(0 아님). NULL 이면 EV-19 가 진도를 추정하지 않는다. 11-14 이전 시나리오 행은 NULL',
  `INTENSITY`     decimal(2,1) NOT NULL                COMMENT '진도(계측 진도 · 소수 한 자리) — 시나리오는 진앙(지표 거리 0 · 지반 1.0)의 계산 진도, 실제 지진은 최대 진도. EV-19 는 max_intensity 로 싣기만 하고 판정에 쓰지 않는다. 11-14 이전 행은 2.0~4.0 무작위',
  `SHAPE`         varchar(8)   NOT NULL                COMMENT '생성기 참고값 — 음영 모양. 11-14 부터 늘 ROUND, 이전 행은 ROUND | LINEAR. EV-19 는 읽지 않는다(운영 지진 정보에 없는 값)',
  `AZIMUTH_DEG`   smallint         DEFAULT NULL         COMMENT '생성기 참고값 — LINEAR 선의 방향(도 · 0~359 · 0 = 북). ROUND 면 NULL. EV-19 는 읽지 않는다',
  `LENGTH_KM`     decimal(5,1)     DEFAULT NULL         COMMENT '생성기 참고값 — LINEAR 선의 길이(km · 진앙이 가운데). ROUND 면 NULL. EV-19 는 읽지 않는다',
  `RADIUS_KM`     decimal(5,1) NOT NULL                COMMENT '생성기 참고값 — 지반 1.0 에서 진도 quake_surge_intensity 이상이 되는 진앙 거리(km). 11-14 이전 행은 음영 반경. EV-19 는 읽지 않는다',
  `BASE_PCT`      decimal(4,1) NOT NULL                COMMENT '생성기 참고값 — 설정 quake_down_pct. 11-15 A안부터 정지 선택에 쓰지 않는다(진도 5약 이상 eNB 를 흔들림 가중 무작위로 MAX_CELLS 까지 끈다). 11-14~11-15 행은 그 eNB 의 정지 확률(%), 11-14 이전 행은 진앙 0km 정지 확률. EV-19 는 읽지 않는다',
  `MAX_CELLS`     int          NOT NULL                COMMENT '생성기 참고값 — 정지 셀 최대 개수(설정 quake_down_max_cells). EV-19 는 읽지 않는다',
  `UPDATED_TIME`  datetime     NOT NULL                COMMENT '행을 쓴 시각 — 생성기는 INSERT IGNORE 라 처음 쓴 값(주입 당시)이 남는다',
  PRIMARY KEY (`QUAKE_ID`),
  KEY `IX_CM_QUAKE_EVENT_FAULT` (`FAULT_ID`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb3 COLLATE=utf8mb3_general_ci
  COMMENT='지진 정보 — 지진 1건 = 1행(진앙 하나). 27번 시작 시 생성기가 쓰고 실제 지진(P2P·기상청)도 같은 모양으로 받는다. graphio EV-19 는 ORIGIN_TIME·LAT·LON·DEPTH_KM·MAGNITUDE·INTENSITY 만 읽는다. 단위 km·%·십진 도, 모르는 값은 NULL(0 아님)';
CREATE TABLE IF NOT EXISTS `{T_APP}` (
  `APP_IDX`         varchar(20)  NOT NULL             COMMENT '앱 코드 → XDR App_ID',
  `ENDPOINT_SEQ`    int          NOT NULL DEFAULT 1   COMMENT '앱 안의 엔드포인트 번호 — 앱 하나에 IP·포트가 여럿일 수 있다',
  `APP_NAME`        varchar(100) NOT NULL             COMMENT '앱 이름 — 화면·답에 쓰는 이름. 한 앱(APP_IDX)에 엔드포인트(ENDPOINT_SEQ)가 여러 행이라 앱 단위로는 APP_IDX 로 묶는다',
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
    "NOTE AS note, CREATED_TIME AS created_at, UPDATED_TIME AS updated_at, "
    "SCENARIO_NO AS scenario_no, VOLUME_PCT AS volume_pct, "
    "VIRTUAL_OFFSET_SEC AS virtual_offset_sec, RESOLVED_TARGET AS resolved_target, "
    "INJECT_CONTEXT AS inject_context"
)

#: 틱 루프가 쓰는 짧은 형태 — 해석에 필요한 여덟 컬럼만. `scenario_no` 는 칸이 있으면 메모보다 먼저 읽힌다.
#: `note` 는 순환 몫의 시나리오 번호다(`faults.scenario_no` 가 `#19` 를 읽는다). 빠지면 kind 를 나눠 쓰는
#: 번호(18·19·20·26)가 원본 번호(1·10·12·8)로 읽혀 남의 사용 기록으로 대상을 고른다(2026-11-11 · 19번 MME-3 연속)
FAULT_SELECT_ACTIVE = (
    "SELECT FAULT_ID AS fault_id, FAULT_KIND AS kind, TARGET AS target, "
    "START_TIME AS start_ts, DURATION_MIN AS duration_min, INTENSITY AS intensity, "
    "NOTE AS note, SCENARIO_NO AS scenario_no"
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
    *((T_FAULT, c, d) for c, d in FAULT_COND_COLUMNS),
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
        # 폐기가 맨 앞이다 — kind 가 있어도 READY 로 보이면 주입 메뉴에 다시 뜬다
        # 화면 이름·한 줄 설명(`SCENARIO_ORDER`)이 있으면 그것을 쓴다 — 없으면(폐기 번호) 내부 이름 그대로
        if no in SCENARIO_SHOWN:
            name, _ja, desc, _desc_ja = SCENARIO_SHOWN[no]
        rows.append([no, name, kind, same, ("RETIRED" if no in RETIRED else "READY" if kind
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
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    # 화면 순서(`SCENARIO_RANK`) — DB 칸이 아니다(DDL 없이 · 2026-11-21). 순서가 없는 번호(폐기)는 None 이다
    for r in rows:
        r["rank"] = SCENARIO_RANK.get(r["no"])
    return rows


def add_fault(conn, kind: str, target: str | None, start_ts: datetime,
              duration_min: int = 30, intensity: float = 1.0,
              mode: str = "manual", note: str | None = None,
              scenario_no: int | None = None) -> int:
    if kind not in KINDS:
        raise ValueError(f"알 수 없는 kind: {kind!r} — {', '.join(KINDS)}")
    if kind == "capacity_shift" and not str(target or "").upper().startswith("DAILY"):
        # 매일 부하(`DAILY` 대상 · 2026-11-21)는 gen 이 준 길이(1시간 × 이어진 시간)를 그대로 쓴다 — B 개통 국면이 없다.
        # **길이를 여기서 고정한다** — `faults.build` 에서만 `SHIFT_TOTAL` 로 늘리면
        # `poll_transitions` 가 행의 `DURATION_MIN`(화면 기본 10분)으로 장애를
        # 끝내, B 가 켜지는 순간 시나리오가 내려간다. 화면·지도·CLI 어느
        # 경로로 들어와도 이 함수를 지난다.
        from .scenarios import SHIFT_TOTAL
        duration_min = SHIFT_TOTAL
    target = _check_like(conn, kind, target)
    if scenario_no is None:
        # 받은 값이 없으면 메모 앞 `#n` — kind 로 짐작하지 않는다(같은 kind 를 여러 번호가 쓴다 · 19·10)
        from .faults import note_scenario_no
        scenario_no = note_scenario_no(note)
    now = datetime.now()
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO {T_FAULT} (FAULT_KIND,TARGET,START_TIME,DURATION_MIN,"
            "INTENSITY,INJECT_MODE,STATUS,NOTE,SCENARIO_NO,CREATED_TIME,UPDATED_TIME) "
            "VALUES (%s,%s,%s,%s,%s,%s,'pending',%s,%s,%s,%s)",
            [kind, target, start_ts, duration_min, intensity, mode, note, scenario_no, now, now],
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


def poll_transitions(conn, now: datetime, cond: dict | None = None,
                     slot: datetime | None = None) -> list[dict]:
    """상태 전이를 수행하고 **현재 활성** fault 목록을 돌려준다.

    데몬이 틱마다 부른다. 전이는 DB 에서 일어나므로 데몬이 죽었다 떠도
    진행 중이던 장애가 그대로 이어진다.

    ``cond`` 가 있으면 **pending → active 전이와 같은 UPDATE 로** 그 순간의 생성 조건을 적는다
    (`volume_pct`·`virtual_offset_sec`·`context`). 따로 적으면 그 사이에 UI 가 값을 바꿔 전이 시점과
    다른 값이 남을 수 있다. 한 번 활성이 된 행은 다시 이 UPDATE 에 걸리지 않으므로 덮어쓰지 않는다.
    없으면(다른 경로) 칸을 건드리지 않는다 — NULL 은 "기록 전"이다.

    ``slot`` 은 **지금 만드는 분**이다(2026-11-11 · 8번 F-264·F-284). 상태 전이는 벽시계(``now``)로 하되 돌려줄
    활성 목록은 그 분이 장애 창 안인지로 고른다. 실시간 틱은 방금 닫힌 분을 그 분 + 3초에 만들므로, 벽시계로만
    고르면 마지막 분을 만들 때 장애가 이미 `done` 이라 **모든 장애가 마지막 1분을 잃었다**(F-284 13:56 기종 실패
    0건 · F-264 00:43 0건 — 정답지는 11분). 취소된 장애는 넣지 않는다 — 사람이 끊은 것이다.
    """
    with conn.cursor() as cur:
        if cond is None:
            cur.execute(
                f"UPDATE {T_FAULT} SET STATUS='active', UPDATED_TIME=%s "
                "WHERE STATUS='pending' AND START_TIME <= %s", [now, now])
        else:
            vol, vo = cond.get("volume_pct"), cond.get("virtual_offset_sec")
            ctx = cond.get("context")
            cur.execute(
                f"UPDATE {T_FAULT} SET STATUS='active', UPDATED_TIME=%s, "
                "VOLUME_PCT=%s, VIRTUAL_OFFSET_SEC=%s, INJECT_CONTEXT=%s "
                "WHERE STATUS='pending' AND START_TIME <= %s",
                [now,
                 None if vol is None else max(0, min(255, int(round(float(vol))))),
                 None if vo is None else int(round(float(vo))),
                 None if ctx is None else json.dumps(ctx, ensure_ascii=False, separators=(",", ":")),
                 now])
        cur.execute(
            f"UPDATE {T_FAULT} SET STATUS='done', UPDATED_TIME=%s "
            "WHERE STATUS='active' "
            "AND DATE_ADD(START_TIME, INTERVAL DURATION_MIN MINUTE) <= %s",
            [now, now])
        if slot is None:
            cur.execute(FAULT_SELECT_ACTIVE + f" FROM {T_FAULT} "
                        "WHERE STATUS='active' ORDER BY FAULT_ID")
        else:
            cur.execute(FAULT_SELECT_ACTIVE + f" FROM {T_FAULT} "
                        "WHERE (STATUS='active' OR (STATUS='done' "
                        "AND DATE_ADD(START_TIME, INTERVAL DURATION_MIN MINUTE) > %s)) "
                        "ORDER BY FAULT_ID", [slot])
        cols = [d[0] for d in cur.description]
        active = [dict(zip(cols, r)) for r in cur.fetchall()]
    conn.commit()
    return active


def record_resolved(conn, targets: dict[int, str]) -> list[int]:
    """생성기가 해석한 실제 대상을 `RESOLVED_TARGET` 에 적는다 — **비어 있는 행만**, 적은 fault_id 를 돌려준다.

    값은 정답지 `scope`(`Target.label()`)와 같은 글이다 — 화면과 정답지가 같은 말을 해야 대조가 된다.
    한 번 적으면 덮지 않는다(`IS NULL`). 해석은 fault_id 시드라 틱마다 같지만, gen 재빌드로 해석 코드가
    바뀌면 다른 값이 나올 수 있다 — 그때도 **주입 당시** 값이 남아야 한다.
    """
    done = []
    with conn.cursor() as cur:
        for fid, label in targets.items():
            if not label:
                continue
            cur.execute(
                f"UPDATE {T_FAULT} SET RESOLVED_TARGET=%s "
                "WHERE FAULT_ID=%s AND RESOLVED_TARGET IS NULL", [label[:255], fid])
            done.append(fid)
    conn.commit()
    return done


def record_quakes(conn, rows: list[dict]) -> list[int]:
    """27번 지진 정보를 `CM_QUAKE_EVENT` 에 적는다 — **이미 있는 지진은 덮지 않는다**(`INSERT IGNORE`).

    지진은 장애 ID 시드로 정해져 틱마다 같은 값이지만, gen 재빌드로 계산이 바뀌어도 **주입 당시** 값이
    남아야 한다(`record_resolved` 와 같은 이유). 적은 fault_id 를 돌려준다
    """
    done = []
    now = datetime.now()
    with conn.cursor() as cur:
        for r in rows:
            cur.execute(
                f"INSERT IGNORE INTO {T_QUAKE} (QUAKE_ID, FAULT_ID, SOURCE, ORIGIN_TIME, HYPO_NAME, LAT, LON, "
                "DEPTH_KM, MAGNITUDE, INTENSITY, SHAPE, AZIMUTH_DEG, LENGTH_KM, RADIUS_KM, BASE_PCT, MAX_CELLS, "
                "UPDATED_TIME) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                [r["quake_id"], r["fault_id"], r["source"], r["origin_time"], (r.get("hypo_name") or "")[:64] or None,
                 r["lat"], r["lon"], r.get("depth_km"), r.get("magnitude"), r["intensity"], r["shape"],
                 r.get("azimuth_deg"), r.get("length_km"), r["radius_km"], r["base_pct"], r["max_cells"], now])
            done.append(int(r["fault_id"]))
    conn.commit()
    return done


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


#: **매일 같은 시각 부하 묶음**(2026-11-21 사용자 「망 조작 · 장비 재배정 아래 · 역 단위로 등록 · 시간 여러 개 · 1시간 단위」).
#: 한 묶음 = 설정 키 하나 `daily_load.<n>` · 값 `on|08,18,19|ST:35.68968-139.70035,ST:…` (켜기|가상 시(時) 목록|대상).
#: 「이 시간들 × 이 역들」이고 부하 하나는 `DAILY_HOUR_MIN`(1시간) — 정시에 시작한다. 이어진 시간(18·19시)은 gen 이
#: 한 부하로 잇는다(`cli._daily_load`). DDL 없이 `CONFIG_VALUE`(varchar 255)에 담는다 — 역 ID 는 앞머리 `STN-` 를 떼어 적는다.
#: 대상 토큰 — 화면은 `ST:<역 ID>`(`CM_CELL_STATION_MAP` 의 그 역 셀 전부)만 쓴다. `ENB:<eNB>`·`CELL:<eNB>-<셀>` 은
#: 1차 판의 해석이 남아 있어 받기만 한다.
DAILY_KEY = "daily_load."
DAILY_TOKEN_RE = r"(ST:[0-9.\-]{3,40}|ENB:\d{1,10}|CELL:\d{1,10}-\d{1,5})"
#: 부하 한 칸의 길이(분) — 사용자 「부하 단위는 1시간」. 몰림·회복 5분은 24번 국면 그대로다(`scenarios._shift_rise`)
DAILY_HOUR_MIN = 60


def parse_daily_line(v: str | None) -> dict | None:
    """`on|08,18|ST:…,ST:…` → `{"on", "hours": [8, 18], "targets"}`. 형식이 틀리면 None(그 묶음은 걸지 않는다)"""
    import re
    parts = str(v or "").strip().split("|")
    if len(parts) != 3:
        return None
    on, hs, tg = (p.strip() for p in parts)
    if on not in ("on", "off"):
        return None
    hl = [h.strip() for h in hs.split(",") if h.strip()]
    if not hl or not all(re.fullmatch(r"[01]\d|2[0-3]", h) for h in hl):
        return None
    toks = [t.strip() for t in tg.split(",") if t.strip()]
    if not toks or not all(re.fullmatch(DAILY_TOKEN_RE, t) for t in toks):
        return None
    return {"on": on == "on", "hours": sorted({int(h) for h in hl}), "targets": list(dict.fromkeys(toks))}


def format_daily_line(d: dict) -> str:
    hours = sorted({int(h) for h in d["hours"]})
    return f"{'on' if d.get('on') else 'off'}|{','.join(f'{h:02d}' for h in hours)}|{','.join(d['targets'])}"


def daily_run(hours, h: int) -> int:
    """`h` 시부터 이어진 시간 수(자정 넘어 이어짐) — 18·19시면 18시에 2. 이어진 시간을 한 부하로 걸어 정시마다
    회복 → 몰림 골이 생기지 않게 한다"""
    hs, n = set(hours), 0
    while n < 24 and (h + n) % 24 in hs:
        n += 1
    return n


def daily_lines(conn) -> list[tuple[int, dict]]:
    """저장된 매일 부하 묶음 — `(묶음 번호, 묶음)`. 빈 값·깨진 값은 건너뛴다(지운 묶음은 빈 값으로 남는다)"""
    out = []
    for k, v in config_prefix(conn, DAILY_KEY).items():
        n = k[len(DAILY_KEY):]
        d = parse_daily_line(v)
        if n.isdigit() and d:
            out.append((int(n), d))
    return sorted(out)


def config_prefix(conn, prefix: str) -> dict[str, str]:
    """`prefix` 로 시작하는 설정 전부 — 매일 부하 묶음(`daily_load.<n>`)처럼 개수가 정해지지 않은 키를 읽는다"""
    with conn.cursor() as cur:
        cur.execute(f"SELECT CONFIG_KEY, CONFIG_VALUE FROM {T_CONFIG} WHERE CONFIG_KEY LIKE %s",
                    [prefix.replace("_", r"\_") + "%"])
        return {k: v for k, v in cur.fetchall()}


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
