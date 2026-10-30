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
from . import scenarios as S
from .scenarios import (
    KIND_SPECS,
    Scenario,
    Target,
    _cells_in_one_sgg,
    _node_eq,
    area_cells,
    busy_cluster,
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
    # ── 발생 장비 고정(2026-10-28 · `control.SCENARIOS`) ──────────────────
    # 전에는 전국(`Target()`)이었다. 지도에서 장비를 찍지 않았을 때도
    # 시나리오 목록이 말하는 장비에 건다 — 가장 바쁜 것을 고른다.
    "auth_failure": "mme_id",       # MME–HSS 구간
    "dns_degradation": "sgw_id",    # 사용자 평면 경로
    "external_server": "sgw_id",
    "ue_model": "sgw_id",           # SGW 아래 여러 셀에 흩어진다(R-10)
    "ims_pdn": "sgw_id",            # PGW 번호 = SGW 번호(구 SPGW)
    "cdn_degradation": "sgw_id",    # 6번과 같은 자리
    "pay_dns_delay": "sgw_id",      # 5번과 같은 자리
}

#: 한 시군구 전 셀을 범위로 잡는 kind — 지리적으로 뭉치는 열화다
#: 셀 쌍을 대상으로 받는 kind — eNB 를 찍으면 이웃과 짝을 짓는다.
_PAIR_KINDS = ("mobility", "son_param_change", "ho_interruption")

#: 군중 혼잡 kind — **상위 트래픽 대역**의 가까운 셀 8개(`busy_cluster`).
#: 시군구 전체로 잡으면 한산한 셀이 섞여 ×1.8 을 해도 분모를 못 채운다.
_CROWD_KINDS = ("ran_congestion",)

#: 한 지역 셀 25개를 범위로 잡는 kind — 지진(27번). 그중 40% 가 정지하고
#: 나머지가 그 가입자를 받는다. eNB 하나로는 "받을 이웃"이 없다.
_AREA_KINDS = ("earthquake",)

#: 행사장 kind — 가까운 셀 8개(`area_cells`). 공연장은 한산한 셀일 수도
#: 있다 — 3배면 분모 걱정이 적다(확정대장 §11.1).
_VENUE_KINDS = ("event_crowd",)

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
    if kind in _CROWD_KINDS:
        keys = {c.cell_key for c in busy_cluster(cells, rng, want=8)}
        return Target(cell_keys=keys) if keys else None
    if kind in _AREA_KINDS:
        keys = {c.cell_key for c in area_cells(cells, rng, want=25)}
        return Target(cell_keys=keys) if keys else None
    if kind in _VENUE_KINDS:
        keys = {c.cell_key for c in area_cells(cells, rng, want=8)}
        return Target(cell_keys=keys) if keys else None
    if kind in _PAIR_KINDS:
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

    if text.upper().startswith("DOMAIN:"):
        # 도메인 축 시나리오(SC-05~06b)는 운영자가 도메인을 직접 고를 수 있어야
        # 한다(2026-10-30). 자동 선택만 있으면 소형 도메인이 걸렸을 때 다시 돌릴
        # 방법이 없다 — SC-06b 가 그랬다. 범위는 전국이고 걸러지는 것은 그
        # 도메인의 호다(`APN:` 과 같은 모양). 판정은 `scenarios._domain_hit` 의
        # 부분 문자열 규칙이다.
        dom = text.split(":", 1)[1].strip().lower()
        return Target(domain=dom) if dom else None

    if text.upper().startswith("ENB:"):
        num = text.split(":", 1)[1].strip()
        mine = [c for c in cells if c.enb_id == num]
        if not mine:
            return None
        anchor0 = next((c for c in mine if c.geo_status == "MAPPED"), None)
        if kind in _PAIR_KINDS and anchor0 is not None:
            # 셀 쌍 장애(9·25번) — 그 eNB 의 셀을 앵커로 **이웃 셀과 짝을
            # 짓는다.** eNB 셀만 넣으면 짝이 없어 "옮겨 간 쪽에서 끊긴다"
            # (`_handover_failure` 의 Last 셀)가 서지 않는다.
            anchor, band = corridor_cells(cells, rng, want=18, anchor=anchor0)
            return Target(
                cell_keys={c.cell_key for c in mine} | {c.cell_key for c in band[:14]},
                enb_id=num,
                cell_pairs=[(anchor.cell_key, c.cell_key) for c in band[:8]])
        if kind in _VENUE_KINDS and anchor0 is not None:
            near = area_cells(cells, rng, want=8, anchor=anchor0)
            return Target(cell_keys={c.cell_key for c in mine}
                          | {c.cell_key for c in near}, enb_id=num)
        if kind in _AREA_KINDS and anchor0 is not None:
            near = area_cells(cells, rng, want=25, anchor=anchor0)
            return Target(cell_keys={c.cell_key for c in mine}
                          | {c.cell_key for c in near}, enb_id=num)
        if kind in _CROWD_KINDS and anchor0 is not None:
            # 군중 혼잡(14번) — 그 eNB 를 중심으로 **주변 상위 대역 셀**까지.
            # eNB 하나(3~6셀)로는 "역 일대"가 되지 않는다
            near = busy_cluster(cells, rng, want=8, anchor=anchor0)
            return Target(cell_keys={c.cell_key for c in mine}
                          | {c.cell_key for c in near}, enb_id=num)
        return Target(cell_keys={c.cell_key for c in mine}, enb_id=num)

    # ── 여러 eNB 를 한 장애로 (지도의 범위 주입 · 2026-10-28) ──────────────
    # eNB 하나는 호가 적어 최소 표본(분모 20)을 못 넘기고 묻히기 쉽다.
    # 그렇다고 eNB 마다 주입을 나누면 이력·정답이 N 건으로 쪼개져 "한
    # 장애"로 채점되지 않는다. `TARGET` 이 64자라 eNB 번호를 나열할 수도
    # 없어 **범위를 한 줄로** 받는다.
    #   TAC:<번호>                      그 TAC 의 셀 전부
    #   BOX:<남위>,<서경>,<북위>,<동경>  좌표 사각형 안의 셀 (지도 드래그)
    up = text.upper()
    if up.startswith("TAC:") or up.startswith("BOX:"):
        arg = text.split(":", 1)[1].strip()
        if up.startswith("TAC:"):
            if not arg.isdigit():
                return None
            mine = [c for c in cells if int(c.tac) == int(arg)]
        else:
            try:
                s_, w_, n_, e_ = (float(v) for v in arg.split(","))
            except ValueError:
                return None
            # 좌표가 없는 셀은 사각형 안팎을 말할 수 없다 — 넣지 않는다
            mine = [c for c in cells if c.geo_status == "MAPPED"
                    and s_ <= c.latitude <= n_ and w_ <= c.longitude <= e_]
        if not mine:
            return None
        keys = {c.cell_key for c in mine}
        anchor0 = next((c for c in mine if c.geo_status == "MAPPED"), None)
        if kind in _PAIR_KINDS and anchor0 is not None:
            # 셀 쌍 장애는 범위 안 앵커에서 이웃과 짝을 짓는다 — ENB: 와 같은 이유
            anchor, band = corridor_cells(cells, rng, want=18, anchor=anchor0)
            return Target(cell_keys=keys | {c.cell_key for c in band[:14]},
                          cell_pairs=[(anchor.cell_key, c.cell_key) for c in band[:8]])
        return Target(cell_keys=keys)

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


def _shift_target(raw: str | None, start_ts: datetime,
                  cells: list[Cell]) -> Target | None:
    """`j-sc-24` 대상 — **설정이 정한다.** A 는 `from_enb`, B 는 순환.

    화면에서 다른 eNB 를 골라 왔으면 받지 않는다(취소). 그 eNB 에 걸 수
    있는 것처럼 보이고 실제로는 A 에 걸리면, 어디에 주입했는지를 화면과
    데이터가 다르게 말한다.
    """
    a = S.SHIFT.from_enb
    text = (raw or "").strip()
    if text and text.upper() != f"ENB:{a}":
        return None
    mine = {c.cell_key for c in cells if c.enb_id == a}
    b = S.shift_pick(int(start_ts.timestamp() // 60), S.SHIFT.standby_enbs)
    if not mine or b is None:
        return None
    return Target(cell_keys=mine, enb_id=a, shift_to=b)


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
    if fault["kind"] == "capacity_shift":
        target = _shift_target(fault.get("target"), fault["start_ts"], cells)
    else:
        target = _resolve_target(fault.get("target"), fault["kind"], cells, rng)
    if target is None:
        return None
    s = dict(spec)
    s["kind"] = fault["kind"]
    s["target"] = target
    s["peak"] = float(fault["intensity"])
    dur = int(fault["duration_min"])
    if fault["kind"] == "capacity_shift":
        # **길이는 시나리오가 정한다** — 상승·분산·분석 세 국면이 30분이다.
        # 화면 기본값(10분)으로 자르면 B 가 켜지는 순간 창이 끝난다.
        dur = S.SHIFT_TOTAL
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
