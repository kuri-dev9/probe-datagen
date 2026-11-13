"""제어 UI 공통 — 설정·DB 접속·셀 목록·종류 라벨. 모든 router 가 여기서 가져간다."""

from __future__ import annotations

from pathlib import Path

import pymysql

from .. import topo
from ..pemdb1 import CPLANE_ROOT_DEFAULT


HERE = Path(__file__).resolve().parents[3]

#: kind → 운영자용 한국어 라벨. KINDS(control.py)와 1:1 — 빠진 키가 있으면
#: 셀렉트에서 영문 코드가 그대로 보여 금방 눈에 띈다.
KIND_LABELS = {
    # ── 기존 13종 ────────────────────────────────────────────────────────
    "gw_resource": "SGW 자원 소진",
    "gw_node_down": "SGW 노드 다운 (무응답)",
    "ran_radio": "무선 구간 열화",
    "mme_overload": "MME 과부하",
    "auth_failure": "인증(S6a) 거절",
    "dns_degradation": "DNS 열화",
    "external_server": "외부 서버 5xx",
    "transport_loss": "전송망 손실",
    "ue_model": "특정 단말 기종",
    "mobility": "핸드오버 이상",
    "change_suspected": "작업/변경 의심 (MME×APN)",
    "ims_pdn": "IMS PDN 이상",
    "nsa_nr_leg": "NSA NR leg 실패",
    # ── 하위 변형 ────────────────────────────────────────────────────────
    "gw_pgw_restart": "PGW 재기동 통보",
    "external_path_latency": "외부 경로 지연 (백본)",
    "nsa_policy_restrict": "DCNR 정책 제한",
    "anchor_saturation": "5G 체감 품질 저하 (앵커 경합)",
    "son_param_change": "기지국 설정 변경 후 계단형 저하 (SON)",
    "ran_congestion": "무선 용량 혼잡 (기존 가입자 품질 저하)",
    "ho_interruption": "전환 직후 정체 (코리도 4G↔5G)",
    "earthquake": "국지 다수 셀 정지 + 주변 부하 유입 (지진 · 옛 27번)",
    "natural_disaster": "지진 — 통화 폭주 + 기지국 정지 + 문자 실패",
    "sa_fallback": "SA→LTE 폴백 급증 (5G 측 장애 추정)",
    "cdn_degradation": "동영상 CDN 서버측 열화",
    "pay_dns_delay": "결제 앱 응답 지연 (DNS 기인)",
    "event_crowd": "대형 이벤트 군중 혼잡 + 업링크",
    "capacity_shift": "기지국 증설 후 부하 이동 (장애 아님)",
    # ── 신규 8종 ─────────────────────────────────────────────────────────
    "auc_no_response": "S6a 무응답 (TIMEOUT)",
    "dra_route_failure": "S6a 라우팅 계층 이상",
    "s6a_undetermined": "S6a 무응답 (장비 미특정)",
    "pgw_uplane_degradation": "PGW 사용자평면 저하",
    "enb_s1_outage": "eNB 전송 단절 (무호)",
    "partial_recovery": "부분 복구 (단계적 회복)",
    "apn_pdn_failure": "APN 접속 실패",
    "dns_flap": "DNS 간헐 실패",
    "compound_causal_failure": "복합 장애 (전송→DNS)",
}

#: **주입 메뉴는 `CM_ERROR_SCENARIO` 를 읽는다** (2026-10-28). 전에는 여기
#: `FAULT_MENU` 상수였는데, 지도·다른 화면도 같은 목록과 발생 장비를 봐야
#: 해서 DB 로 옮겼다. 정본은 `control.SCENARIOS` 다.

# ── MySQL 접속 ──────────────────────────────────────────────────────────────
#
# **상용 스키마(`pemdb1`)를 본다.** 전환 전에는 생성기 자체 창고(`graphio` DB 의
# `kpi_cell_min`·`file_ledger`·`cell_master`)를 읽었는데, graphio 가 `pemdb1` 으로
# 옮겨간 뒤 그 DB 는 **아무도 쓰지도 읽지도 않는다.** 화면은 멀쩡히 떠서 멈춘
# 숫자를 보여 줬고 에러는 없었다 — 그 조용함이 문제였다.
#
# 제어 테이블(`datagen_*`)도 같은 DB 에 둔다. UI 가 `graphio` 에 쓰고 데몬이
# `pemdb1` 에서 읽으면 **주입한 장애가 영원히 적용되지 않는다** — 양쪽 다
# 정상으로 보이고 그 사이에 아무 신호가 없다.

_CFG: dict | None = None  # main() 이 --config 로 채운다. 미설정이면 기본 yaml.


def _cfg() -> dict:
    """설정. `main()` 이 ``--config`` 로 채우고, 없으면 기본 yaml 이다.

    **`cli.load_cfg` 를 쓴다** (2026-10-14). 전에는 `datagen.yaml` 을 직접
    읽었고 ``--config`` 는 그 파일을 **통째로 대체**했다 — 얇은 환경별 파일
    (`config/local.yaml` 처럼 `mysql.host` 만 적은 것)을 주면 `pemdb1:`
    절 전체를 잃고, 그러면 UI 가 **생성기와 다른 셀 집합·요율을 보여 준다.**
    주입 대상이 생성 대상과 어긋나도 에러가 나지 않는 종류다.
    """
    global _CFG
    if _CFG is None:
        from ..cli import load_cfg
        _CFG = load_cfg(None)
    return _CFG


def _pem_cfg() -> dict:
    return _cfg().get("pemdb1") or {}


def _cplane_root() -> Path:
    """C-Plane 원시 Parquet 루트. **설정이 정본이다.**

    `cli._cplane_store` 와 같은 규칙으로 읽는다 — 두 곳에서 다르게 풀면
    화면이 데몬이 쓰는 디렉터리와 **다른 자리**를 보고 "적재가 멈췄다"로
    보인다. 에러는 나지 않는다.
    """
    sec = (_pem_cfg().get("cplane_parquet") or {})
    raw = Path(str(sec.get("root") or CPLANE_ROOT_DEFAULT))
    return raw if raw.is_absolute() else (HERE / raw).resolve()


def _connect():
    """요청마다 새 커넥션 — pymysql 커넥션은 스레드 전용이라 공유하면
    커서 상태가 섞여 엉뚱한 자리에서 깨진다(FastAPI 는 동기 엔드포인트를
    스레드풀에서 돌린다). 요청당 연결 비용은 로컬/폐쇄망에서 무시할 수준."""
    m = _cfg()["mysql"]
    return pymysql.connect(
        host=m["host"], port=int(m["port"]), user=m["user"],
        password=m["password"],
        database=_pem_cfg().get("database", "pemdb6"),
        # 상용 collation 이 utf8mb3_general_ci 다. utf8mb4 로 열면 리터럴
        # 비교에서 Illegal mix of collations 가 날 수 있다 (pemfill 과 동일).
        charset="utf8mb3",
    )


#: 셀 목록 캐시. `pemdb1.load_cells` 는 결정론적이고 `enb_limit`·`drawable` 이
#: 같으면 **생성기와 글자까지 같은 집합**을 준다 — 그래서 별도 테이블 없이
#: 여기서 다시 계산한다. 셀→노드(MME/SGW/PGW) 배정은 DB 컬럼이 아니라
#: `load_cells` 의 eNB 해시로 정해지므로, 테이블을 조회해서는 알 수 없다.
_CELLS: list | None = None


def _cells(conn) -> list:
    """생성기와 **같은 셀 집합**. 캐시한다 — 요청마다 마스터를 다시 읽을 이유가 없다."""
    global _CELLS
    if _CELLS is None:
        from .. import pemdb1 as P

        pem = _pem_cfg()
        drawable = None
        xw = pem.get("geo_crosswalk")
        if xw:
            q = Path(xw)
            if not q.is_absolute():
                q = (HERE / q).resolve()
            if q.exists():
                drawable = P.load_drawable_dongs(q)
        # 생성기(`cli.cmd_run`)와 **같은 인자**여야 같은 집합이 나온다.
        #
        # **인자가 늘면 여기도 늘려야 한다.** 2026-10-27 에 `locale`·
        # `tac_only`·`sectors_per_enb`·`scope` 가 생겼는데 여기만 안 넘겨서
        # UI 가 **다른 셀 집합**을 봤다 — 도쿄로 돌린 뒤엔 KR 벌을 읽어
        # 0행이었다. 에러가 아니라 화면이 비는 모양이다.
        _CELLS = P.load_cells(
            conn, enb_limit=int(pem.get("enb_limit", 0)), drawable=drawable,
            locale=str(pem.get("locale", "kr")),
            tac_only=bool(pem.get("tac_only", False)),
            sectors_per_enb=pem.get("sectors_per_enb"),
            scope=P.Scope.from_cfg(pem.get("scope")))
    return _CELLS



def _enb_names(conn) -> dict[str, str]:
    """eNB 번호 → 이름(`BTS_NAME`) — 생성 로케일의 조직 벌에서.

    번호만으로는 어디의 eNB 인지 모른다. 이름이 없는 eNB 는 빠진다 —
    화면이 번호로 보인다(지어내지 않는다 · 불변규칙 17)
    """
    loc = str(_pem_cfg().get("locale", "kr"))
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT ENB_ID, BTS_NAME FROM {topo.table_name('enb_org', loc)} "
                        "WHERE BTS_NAME IS NOT NULL AND BTS_NAME <> ''")
            return {str(a): str(b) for a, b in cur.fetchall()}
    except pymysql.MySQLError:
        return {}       # 이름은 곁다리다 — 없어도 주입은 된다


#: `ensure_tables` 는 **프로세스당 한 번**이다(사용자 11-09 "시나리오 로딩이 오래 걸린다"). 전에는 화면을 열
#: 때마다 DDL 문장 전부 · information_schema 조회 · 시나리오 27행 REPLACE · DELETE · COMMIT 을 돌려
#: `/api/scenarios` 가 0.8초였다 — 읽기 요청이 운영 DB 에 매번 쓰고 있었다. 표·시나리오 목록은 코드가
#: 바뀔 때만 바뀌고, 코드가 바뀌면 컨테이너가 다시 뜬다
