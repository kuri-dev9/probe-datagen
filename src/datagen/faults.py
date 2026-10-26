"""CM_DATAGEN_FAULT 행 → Scenario 변환.

UI 가 DB 에 남기는 fault 는 "무엇을(kind)·어디에(target)·언제부터(start_ts)·
얼마나(duration·intensity)"만 담는다. 생성기가 받는 Scenario 에는 그 외에
정답 메타(truth_code·expect_rules·expect_geo)와 파형(shape·series·spreading)이
더 필요한데, 이들은 kind 가 정하는 고정 속성이다.

**그 고정 속성의 정본은 `scenarios.KIND_SPECS` 다.** 전에는 이 파일이 같은
표를 한 벌 더 들고 있었고, 한쪽만 고치면 **같은 kind 가 배치 생성과 UI 주입에서
다른 정답을 갖는다** — 에러 없이 채점만 어긋난다(2026-10-22 단일화).

대상 해석(_resolve_target)은 **결정적이어야 한다.** 같은 fault 가 틱마다 다시
와도 같은 셀 집합이 나와야 하며(데몬 재시작 포함), 그렇지 않으면 한 장애가
1분짜리 조각으로 흩어져 탐지가 성립하지 않는다. 호출자(`PemdbGen.set_fault_rows`)가
fault_id 로 시드한 rng 를 넘겨 이를 보장한다.
"""

from __future__ import annotations

import random
from datetime import datetime

from .cells import Cell
from .scenarios import (
    KIND_SPECS,
    Scenario,
    Target,
    _cells_in_one_sgg,
    _node_eq,
    corridor_cells,
    place,
)

#: 노드 지정 target 접두사 → Cell 의 노드 필드
_NODE_FIELD = {"SGW": "sgw_id", "MME": "mme_id", "PGW": "pgw_id"}

#: kind → 자동 대상의 노드 축. `_busiest` 로 고른 뒤 그 노드를 거치는 셀 전부를
#: 범위로 잡는다 — 표본이 가장 두터운 노드라야 최소 표본 규칙(분모 20)을 넘긴다.
_AUTO_NODE = {
    "gw_resource": "sgw_id",
    "gw_node_down": "sgw_id",
    "partial_recovery": "sgw_id",
    "gw_pgw_restart": "pgw_id",
    "pgw_uplane_degradation": "pgw_id",
    "mme_overload": "mme_id",
    "change_suspected": "mme_id",
    "nsa_policy_restrict": "mme_id",
}

#: 한 시군구 전 셀을 범위로 잡는 kind — 지리적으로 뭉치는 열화다
_AUTO_SGG = ("ran_radio", "transport_loss", "nsa_nr_leg",
             "compound_causal_failure")

#: MME 집합을 범위로 잡는 kind → 상위 몇 대인가 `(건너뛸 수, 가져갈 수)`.
#:
#: **SC-14 와 SC-15 가 서로 다른 집합이어야 한다** — 같으면 "무응답 vs 3xxx
#: 거절" 쌍 비교가 대상 차이로 오염된다. SC-15b 는 SC-14 와 같은 크기이되
#: 다른 집합을 쓴다(구분 불가 쌍의 조건은 "대상만 다르다"다).
_AUTO_MME_GROUP = {
    "auc_no_response": (0, 3),
    "dra_route_failure": (3, 5),
    "s6a_undetermined": (8, 3),
}


def _busiest(cells: list[Cell], attr: str):
    """셀 수가 가장 많은 노드 id — 자동 대상의 기본값.

    표본이 가장 두터운 노드라야 최소 표본 규칙(분모 20)을 넘겨 탐지가 성립한다.
    """
    cnt: dict = {}
    for c in cells:
        k = getattr(c, attr)
        cnt[k] = cnt.get(k, 0) + 1
    return max(cnt, key=lambda k: cnt[k])


def _ranked(cells: list[Cell], attr: str) -> list:
    """노드 id 를 셀 수 내림차순으로. MME 집합 선정에 쓴다."""
    cnt: dict = {}
    for c in cells:
        k = getattr(c, attr)
        cnt[k] = cnt.get(k, 0) + 1
    return sorted(cnt, key=lambda k: -cnt[k])


def _sgg_all(cells: list[Cell], code: str) -> set[str]:
    """시군구 셀 **전부** — 일부만 고르면 같은 시군구의 정상 셀이 지리 롤업에서
    열화를 희석해 SGG 단위 탐지가 성립하지 않는다 (build_scenarios 와 같은 이유)."""
    return {c.cell_key for c in cells if c.adm_sgg_cd == code}


def _auto_target(kind: str, cells: list[Cell], rng: random.Random) -> Target | None:
    """target 미지정 시 kind 별 기본 범위."""
    field = _AUTO_NODE.get(kind)
    if field:
        busy = _busiest(cells, field)
        return Target(cell_keys={c.cell_key for c in cells
                                 if getattr(c, field) == busy},
                      **{field: busy})
    if kind in _AUTO_MME_GROUP:
        skip, take = _AUTO_MME_GROUP[kind]
        ranked = _ranked(cells, "mme_id")
        group = set(ranked[skip:skip + take]) or set(ranked[:take])
        return Target(cell_keys={c.cell_key for c in cells if c.mme_id in group},
                      mme_group=group)
    if kind in _AUTO_SGG:
        # _cells_in_one_sgg 는 시군구 "선정"에만 쓴다 — 표본(want개)이 아니라
        # 그 시군구 셀 전부를 가져간다 (롤업 희석 방지).
        _, code = _cells_in_one_sgg(cells, rng, 60)
        keys = _sgg_all(cells, code)
        return Target(cell_keys=keys, sgg_codes={code}) if keys else None
    if kind == "enb_s1_outage":
        busy = _busiest(cells, "enb_id")
        return Target(cell_keys={c.cell_key for c in cells if c.enb_id == busy},
                      enb_id=busy)
    if kind == "mobility":
        # **방위각 ±25° 제약**을 쓴다 — `build_scenarios` 와 같은 함수다.
        # GPS 최근접 N셀은 등방 분포가 되어 `CORRIDOR` 가 아니라 `CLUSTER` 로
        # 떨어진다(`detector.geo_pattern` 의 `elong > 3.2`).
        mapped = [c for c in cells if c.geo_status == "MAPPED"]
        if not mapped:
            return None
        anchor, band = corridor_cells(cells, rng, want=18)
        if not band:
            return None
        return Target(
            cell_keys={anchor.cell_key} | {c.cell_key for c in band[:14]},
            cell_pairs=[(anchor.cell_key, c.cell_key) for c in band[:8]],
        )
    # auth_failure · ims_pdn · dns_degradation · dns_flap · external_server ·
    # external_path_latency · ue_model · apn_pdn_failure — 전국.
    # 도메인·기종·APN 은 `generator._bind_scenario_targets` 가 채운다.
    return Target()


def _resolve_target(raw: str | None, kind: str, cells: list[Cell],
                    rng: random.Random) -> Target | None:
    """target 문자열 → Target. 해석 불능이나 일치 셀 0개면 None (호출부가 cancelled 처리)."""
    text = (raw or "").strip()
    if not text:
        return _auto_target(kind, cells, rng)

    if text.upper().startswith("SGG:"):
        code = text.split(":", 1)[1].strip()
        keys = _sgg_all(cells, code)
        return Target(cell_keys=keys, sgg_codes={code}) if keys else None

    if text.upper().startswith("APN:"):
        # APN 축 시나리오(SC-19·SC-11)는 운영자가 APN 을 직접 고를 수 있어야
        # 한다. 범위는 전국이고 걸러지는 것은 그 APN 의 호다.
        apn = text.split(":", 1)[1].strip()
        return Target(apn=apn) if apn else None

    if text.upper().startswith("ENB:"):
        num = text.split(":", 1)[1].strip()
        keys = {c.cell_key for c in cells if c.enb_id == num}
        return Target(cell_keys=keys, enb_id=num) if keys else None

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


def build(fault: dict, cells: list[Cell], anchor: datetime,
          rng: random.Random) -> Scenario | None:
    """CM_DATAGEN_FAULT 한 행을 Scenario 로 바꾼다.

    시간창은 anchor(분 인덱스 0의 시각) 기준 분 좌표로 옮긴다 — Scenario 가
    intensity() 로 스스로 창을 자르므로, 창 밖 분에 걸려 있어도 무해하다.

    **`STEPS`·`series_offset` 시나리오의 지속시간은 fault 행이 아니라 파형이
    정한다.** `steps` 의 마지막 경과분보다 짧게 자르면 "80% → 50% → 20%" 의
    뒷 구간이 사라져 계단이 보이지 않고, `series_offset` 을 더하지 않으면 늦게
    시작한 계열이 끝에서 잘린다. UI 가 준 길이는 **하한**으로 쓴다.
    """
    spec = KIND_SPECS.get(fault["kind"])
    if spec is None:
        return None
    target = _resolve_target(fault.get("target"), fault["kind"], cells, rng)
    if target is None:
        return None
    s = dict(spec)
    s["kind"] = fault["kind"]
    s["target"] = target
    s["peak"] = float(fault["intensity"])
    dur = int(fault["duration_min"])
    steps = s.get("steps") or ()
    if steps:
        dur = max(dur, steps[-1][0])
    dur += max((s.get("series_offset") or {}).values(), default=0)
    return place(
        f"F-{fault['fault_id']}", s,
        int((fault["start_ts"] - anchor).total_seconds() // 60), dur, rng,
    )


def resolve_active(cells: list[Cell], anchor: datetime, seed: int,
                   active: list[dict],
                   cache: dict[int, tuple[tuple, Scenario]],
                   ) -> tuple[list[Scenario], list[int]]:
    """활성 fault 행 목록 → (Scenario 목록, 해석 실패한 fault_id 목록).

    **구 창고 경로(`engine.DataGen`, 2026-10-15 삭제)와 상용 경로(`pemdb1.PemdbGen`)가 이 함수를
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
