"""datagen_fault 행 → Scenario 변환.

UI 가 DB 에 남기는 fault 는 "무엇을(kind)·어디에(target)·언제부터(start_ts)·
얼마나(duration·intensity)"만 담는다. 생성기가 받는 Scenario 에는 그 외에
정답 메타(truth_code·expect_rules·expect_geo)와 파형(shape·series·spreading)이
더 필요한데, 이들은 kind 가 정하는 고정 속성이다. 그래서
``scenarios.build_scenarios`` 의 spec 을 ``KIND_SPECS`` 로 옮겨 적었다 —
수동 주입에서도 ground truth 가 성립해야 나중에 채점이 가능하다.

대상 해석(_resolve_target)은 **결정적이어야 한다.** 같은 fault 가 틱마다 다시
와도 같은 셀 집합이 나와야 하며(데몬 재시작 포함), 그렇지 않으면 한 장애가
1분짜리 조각으로 흩어져 탐지가 성립하지 않는다. 호출자(engine.set_faults)가
fault_id 로 시드한 rng 를 넘겨 이를 보장한다.
"""

from __future__ import annotations

import random
from datetime import datetime

from .cells import Cell
from .geo_lite import haversine_km
from .scenarios import Scenario, Target, _cells_in_one_sgg, _node_eq

#: kind 고유 속성 — scenarios.build_scenarios 의 spec 에서 그대로 복사했다.
#: peak 는 fault.intensity 가, 대상/시간창은 fault 행이 덮어쓴다.
KIND_SPECS: dict[str, dict] = {
    "gw_resource": dict(
        title="SGW 세션 자원 소진",
        truth_code="GW_RESOURCE_EXHAUSTION", truth_category="CORE_UP",
        expect_rules=("R-05", "R-02", "R-15", "R-21"), expect_geo="SCATTER",
        shape="RAMP",
    ),
    "ran_radio": dict(
        title="특정 지역 무선 구간 열화",
        truth_code="RAN_RADIO_QUALITY", truth_category="RAN",
        expect_rules=("R-06", "R-22", "R-16", "R-20"), expect_geo="CLUSTER",
        shape="STEP",
    ),
    "gw_node_down": dict(
        title="SGW 노드 무응답",
        # 규격의 R-04("F-08이 S11에 집중 + SGW/PGW 편중")가 지목하는 코드는
        # GW_NODE_DOWN 이다. NF_UNREACHABLE 은 구간 미특정 무응답에 쓴다.
        truth_code="GW_NODE_DOWN", truth_category="CORE_UP",
        expect_rules=("R-00b", "R-04", "R-05b"), expect_geo="SCATTER",
        shape="STEP",
    ),
    "auth_failure": dict(
        title="S6a 인증 경로 이상",
        truth_code="AUTH_FAILURE", truth_category="CORE_CP",
        expect_rules=("R-01", "R-21"), expect_geo="SCATTER",
        shape="STEP",
    ),
    "dns_degradation": dict(
        title="DNS 응답 열화",
        truth_code="DNS_DEGRADATION", truth_category="EXTERNAL",
        expect_rules=("R-14",), expect_geo="WIDE",
        shape="RAMP", series=("dns",),
    ),
    "external_server": dict(
        title="외부 서비스 5xx 급증",
        truth_code="EXTERNAL_SERVER", truth_category="EXTERNAL",
        expect_rules=("R-14b", "R-09b"), expect_geo="WIDE",
        shape="STEP", series=("http",),
    ),
    "transport_loss": dict(
        title="국지 전송 구간 손실",
        truth_code="TRANSPORT_LOSS", truth_category="TRANSPORT",
        expect_rules=("R-09", "R-16"), expect_geo="CLUSTER",
        shape="STEP", series=("transport",),
    ),
    "ue_model": dict(
        title="특정 단말 기종 접속 실패",
        truth_code="UE_MODEL_ISSUE", truth_category="UE",
        expect_rules=("R-10",), expect_geo="SCATTER",
        shape="STEP",
    ),
    "mobility": dict(
        title="핸드오버 실패 (셀 쌍 집중)",
        truth_code="RAN_MOBILITY_ISSUE", truth_category="RAN",
        expect_rules=("R-17", "R-20"), expect_geo="CORRIDOR",
        shape="STEP",
    ),
    "mme_overload": dict(
        title="MME 과부하 (확산 중)",
        truth_code="MME_OVERLOAD", truth_category="CORE_CP",
        expect_rules=("R-23", "R-18"), expect_geo="SCATTER",
        shape="RAMP", spreading=True,
    ),
    "change_suspected": dict(
        title="계단형 급락 (변경 의심)",
        truth_code="CHANGE_SUSPECTED", truth_category="CHANGE",
        expect_rules=("R-11",), expect_geo="CLUSTER",
        shape="STEP", ramp_in=1,
    ),
    "ims_pdn": dict(
        title="VoLTE(ims PDN) 전용 접속 실패",
        truth_code="IMS_PDN_ISSUE", truth_category="IMS",
        expect_rules=("R-30",), expect_geo="SCATTER",
        shape="STEP",
    ),
    "nsa_nr_leg": dict(
        title="5G NSA NR 레그 연결 실패",
        truth_code="NSA_NR_LEG_FAILURE", truth_category="NSA",
        expect_rules=("R-33",), expect_geo="CLUSTER",
        shape="RAMP",
    ),
}

#: 노드 지정 target 접두사 → Cell 의 노드 필드
_NODE_FIELD = {"SGW": "sgw_id", "MME": "mme_id", "PGW": "pgw_id"}


def _busiest(cells: list[Cell], attr: str) -> int:
    """셀 수가 가장 많은 노드 id — 자동 대상의 기본값.

    표본이 가장 두터운 노드라야 최소 표본 규칙(분모 20)을 넘겨 탐지가 성립한다.
    """
    cnt: dict[int, int] = {}
    for c in cells:
        k = getattr(c, attr)
        cnt[k] = cnt.get(k, 0) + 1
    return max(cnt, key=lambda k: cnt[k])


def _sgg_all(cells: list[Cell], code: str) -> set[str]:
    """시군구 셀 **전부** — 일부만 고르면 같은 시군구의 정상 셀이 지리 롤업에서
    열화를 희석해 SGG 단위 탐지가 성립하지 않는다 (build_scenarios 와 같은 이유)."""
    return {c.cell_key for c in cells if c.adm_sgg_cd == code}


def _auto_target(kind: str, cells: list[Cell], rng: random.Random) -> Target | None:
    """target 미지정 시 kind 별 기본 범위."""
    if kind in ("gw_resource", "gw_node_down"):
        busy = _busiest(cells, "sgw_id")
        return Target(cell_keys={c.cell_key for c in cells if c.sgw_id == busy}, sgw_id=busy)
    if kind == "mme_overload":
        busy = _busiest(cells, "mme_id")
        return Target(cell_keys={c.cell_key for c in cells if c.mme_id == busy}, mme_id=busy)
    if kind in ("ran_radio", "transport_loss", "change_suspected", "nsa_nr_leg"):
        # _cells_in_one_sgg 는 시군구 "선정"에만 쓴다 — 표본(want개)이 아니라
        # 그 시군구 셀 전부를 가져간다 (롤업 희석 방지).
        _, code = _cells_in_one_sgg(cells, rng, 60)
        keys = _sgg_all(cells, code)
        return Target(cell_keys=keys, sgg_codes={code}) if keys else None
    if kind == "mobility":
        # build_scenarios 와 동일: 임의 기준 셀 + GPS 근접 셀을 인접 쌍으로 (EV-13)
        mapped = [c for c in cells if c.geo_status == "MAPPED"]
        if not mapped:
            return None
        anchor = rng.choice(mapped)
        near = sorted(
            (c for c in mapped if c.cell_key != anchor.cell_key),
            key=lambda c: haversine_km(anchor.latitude, anchor.longitude, c.latitude, c.longitude),
        )[:18]
        pairs = [(anchor.cell_key, c.cell_key) for c in near[:8]]
        return Target(
            cell_keys={anchor.cell_key} | {c.cell_key for c in near[:14]},
            cell_pairs=pairs,
        )
    # auth_failure · ims_pdn · dns_degradation · external_server · ue_model — 전국.
    # 도메인·기종은 generator._bind_scenario_targets 가 프로파일에서 채운다.
    return Target()


def _resolve_target(raw: str | None, kind: str, cells: list[Cell], rng: random.Random) -> Target | None:
    """target 문자열 → Target. 해석 불능이나 일치 셀 0개면 None (호출부가 cancelled 처리)."""
    text = (raw or "").strip()
    if not text:
        return _auto_target(kind, cells, rng)

    if text.upper().startswith("SGG:"):
        code = text.split(":", 1)[1].strip()
        keys = _sgg_all(cells, code)
        return Target(cell_keys=keys, sgg_codes={code}) if keys else None

    head, _, num = text.partition("-")
    field = _NODE_FIELD.get(head.upper())
    if field and num.strip().isdigit():
        # 표기를 보존한다. 상용 노드 ID 는 0채움('0014')이고 사용자는 'SGW-14'
        # 로도 적는다 — 비교는 `_node_eq` 가 숫자로 맞추되, Target 에는 **셀
        # 마스터에 실린 표기**를 넣어야 `as_truth()` 의 expect_node 가
        # graphio 의 axis_ids 와 글자까지 같아진다.
        keys = {c.cell_key for c in cells if _node_eq(getattr(c, field), num)}
        if not keys:
            return None
        actual = next(getattr(c, field) for c in cells if _node_eq(getattr(c, field), num))
        return Target(cell_keys=keys, **{field: actual})

    return None  # 알 수 없는 형식 — 조용히 전국에 뿌리면 오히려 디버깅이 어려워진다


def build(fault: dict, cells: list[Cell], anchor: datetime, rng: random.Random) -> Scenario | None:
    """datagen_fault 한 행을 Scenario 로 바꾼다.

    시간창은 anchor(분 인덱스 0의 시각) 기준 분 좌표로 옮긴다 — Scenario 가
    intensity() 로 스스로 창을 자르므로, 창 밖 분에 걸려 있어도 무해하다.
    """
    spec = KIND_SPECS.get(fault["kind"])
    if spec is None:
        return None
    target = _resolve_target(fault.get("target"), fault["kind"], cells, rng)
    if target is None:
        return None
    sc = Scenario(
        id=f"F-{fault['fault_id']}",
        kind=fault["kind"],
        start_minute=int((fault["start_ts"] - anchor).total_seconds() // 60),
        duration=int(fault["duration_min"]),
        peak=float(fault["intensity"]),
        target=target,
        **spec,
    )
    # spreading(점진 확산)과 active_cells() 가 cell_order 를 본다 — build_scenarios 와 동일
    sc.cell_order = sorted(target.cell_keys)
    rng.shuffle(sc.cell_order)
    return sc


def resolve_active(cells: list[Cell], anchor: datetime, seed: int,
                   active: list[dict],
                   cache: dict[int, tuple[tuple, Scenario]],
                   ) -> tuple[list[Scenario], list[int]]:
    """활성 fault 행 목록 → (Scenario 목록, 해석 실패한 fault_id 목록).

    **구 창고 경로(`engine.DataGen`)와 상용 경로(`pemdb1.PemdbGen`)가 이 함수를
    공유한다.** 두 경로가 각자 해석하면 같은 fault 가 서로 다른 셀 집합에
    걸리고, 그것은 에러 없이 다른 데이터를 만든다.

    같은 fault 는 틱마다 다시 와도 **같은 대상**이어야 한다 — 대상 셀이 틱마다
    바뀌면 장애가 1분 조각으로 흩어져 탐지·채점이 성립하지 않는다. 그래서
    호출자 rng(호출 순서에 따라 상태가 흔들린다) 대신 fault_id 로 시드한 전용
    rng 로 해석하고, 결과를 `cache` 에 들고 있는다. 데몬이 재시작해도 같은
    대상이 나온다.
    """
    scs: list[Scenario] = []
    bad: list[int] = []
    for f in active:
        fid = int(f["fault_id"])
        # UI 가 fault 를 수정하면(시간·강도·대상) 키가 바뀌어 재해석된다
        key = (f["kind"], str(f.get("target") or ""), f["start_ts"],
               int(f["duration_min"]), float(f["intensity"]), anchor)
        cached = cache.get(fid)
        if cached and cached[0] == key:
            scs.append(cached[1])
            continue
        frng = random.Random(seed * 1_000_003 + fid)
        sc = build(f, cells, anchor, frng)
        if sc is None:
            bad.append(fid)
        else:
            cache[fid] = (key, sc)
            scs.append(sc)
    return scs, bad
