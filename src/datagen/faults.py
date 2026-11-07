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
    enb_cluster,
    _node_eq,
    area_cells,
    busy_cluster,
    corridor_cells,
    place,
    whole_enbs,
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
    # "external_server" — **자동 선택은 전국이다**(2026-11-05 · 6번 · 케이 수정안 A). 외부 서버 장애는
    # 그 도메인을 쓰는 전국 가입자에게 동시에 보인다(정답지도 `WIDE`). SGW 하나로 좁히면 도메인
    # 축(U-25 · 셀 조건 없음)에서 5xx 가 그 SGW 의 트래픽 몫만큼 묽어져 F-108 이 3.4%(임계 5%)로
    # 미탐지였고, ext1(6.07%)·ext2(4.84%)가 고른 SGW 의 몫에 따라 갈렸다 — 결과가 운이었다.
    # `SGW-n`·`ENB:`·`BOX:` 를 지정하면 지금처럼 그 범위로 좁힌다(`_resolve_target`)
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
_AREA_KINDS = ("earthquake", "natural_disaster",
               # 17번 SA 폴백 — 한 지역(셀 25개)의 5G 측 장애. 정지는 없고 범위만 같은 모양이다
               "sa_fallback")

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


def _route_target(routes, rng: random.Random) -> Target | None:
    """15번 자동 대상 — 노선 연속 구간(`scenarios.route_segment`). 쌍은 이웃 칸끼리다.

    노선이 없거나(한국 locale · `routes_file` 없음) 모양을 넘는 구간이 없으면 `None` 이고, 호출부가
    `corridor_cells` 로 돌아간다. **조용히 돌아가지 않는다** — 야마노테선 시나리오가 노선 밖에
    걸린 것을 F-133 뒤에야 알았다
    """
    got = S.route_segment(routes, rng) if routes else None
    if got is None:
        print("  ! 15번 노선 구간을 못 골랐다 — 노선 "
              + ("모양 미달" if routes else "없음") + " · `corridor_cells` 로 돌아간다", flush=True)
        return None
    seg, info = got
    return Target(cell_keys={c.cell_key for c in seg},
                  cell_pairs=[(a.cell_key, b.cell_key) for a, b in zip(seg, seg[1:])],
                  route_seg=info)


def _auto_target(kind: str, cells: list[Cell], rng: random.Random,
                 routes=None) -> Target | None:
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
    if kind == "anchor_saturation":
        # **셀 50개 · 한 시군구**(`j-sc-16`) — 시나리오 정본(`build_scenarios`
        # 의 `anchor_cells`)과 같은 함수·같은 크기다. 전에는 여기 분기가 없어
        # 전국 `Target()` 으로 떨어졌고, mutator 가 `active_cells_cache` 를
        # 보므로 **대상 셀 0개 = 아무 일도 없는 주입**이 됐다(16번 카드 1절).
        # `_AUTO_SGG` 처럼 시군구 전부를 가져가지 않는다 — 판정 축이 CELL 이고
        # (U-15 셀 스펙) 정본이 50개다
        # 2026-11-03: 시군구 첫 추첨 셀을 앵커로 **인접 eNB 10~15셀 군집**
        # (`enb_cluster`) — 정본과 같은 함수다
        # 후보가 없으면(NR 가능 eNB 0개 · `build`) 추첨이 빈 목록에서 죽는다 — 호출부가 취소한다
        if not any(c.geo_status == "MAPPED" for c in cells):
            return None
        picked, code = _cells_in_one_sgg(cells, rng, 50)
        keys = {c.cell_key for c in enb_cluster(cells, picked[0])} if picked else set()
        return Target(cell_keys=keys, sgg_codes={code}) if keys else None
    if kind == "enb_s1_outage":
        busy = _busiest(cells, "enb_id")
        return Target(cell_keys={c.cell_key for c in cells if c.enb_id == busy},
                      enb_id=busy)
    if kind in _CROWD_KINDS:
        keys = {c.cell_key for c in busy_cluster(cells, rng, want=8)}
        return Target(cell_keys=keys) if keys else None
    if kind in _AREA_KINDS:
        # 지진은 사이트 단위 정지 — 걸친 eNB 의 셀 전부다(`whole_enbs`)
        keys = {c.cell_key for c in whole_enbs(cells, area_cells(cells, rng, want=25))}
        return Target(cell_keys=keys) if keys else None
    if kind in _VENUE_KINDS:
        keys = {c.cell_key for c in area_cells(cells, rng, want=8)}
        return Target(cell_keys=keys) if keys else None
    if kind == "ho_interruption":
        target = _route_target(routes, rng)
        if target is not None:
            return target
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
                    rng: random.Random, routes=None) -> Target | None:
    """target 문자열 → Target. 해석 불능이나 일치 셀 0개면 None (호출부가 cancelled 처리)."""
    text = (raw or "").strip()
    if not text:
        return _auto_target(kind, cells, rng, routes)

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
            near = whole_enbs(cells, area_cells(cells, rng, want=25, anchor=anchor0))
            return Target(cell_keys={c.cell_key for c in mine}
                          | {c.cell_key for c in near}, enb_id=num)
        if kind == "anchor_saturation" and anchor0 is not None:
            # 지도에서 찍은 eNB 를 앵커로 같은 군집 함수(2026-11-03)
            near = enb_cluster(cells, anchor0)
            return Target(cell_keys={c.cell_key for c in near},
                          sgg_codes={anchor0.adm_sgg_cd}, enb_id=num)
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


#: **이전 fault 의 대상을 다시 쓴다** — `LIKE:<fault_id>` (2026-11-02 · 카드 15r2).
#: 같은 시나리오를 같은 셀에 다시 넣어 회차끼리 비교하려는 것이다. 셀 목록을
#: 직접 받지 않는 이유: `TARGET` 이 `varchar(64)` 라 셀 15개(약 150자)가
#: 들어가지 않고, 넓히는 것은 운영 스키마 DDL 이다. 대상은 fault_id 로 시드한
#: rng 로 정해지므로(`resolve_active`) 그 rng 를 다시 만들면 같은 대상이 나온다.
#: **원본이 자동 대상(TARGET 이 비어 있음)이었던 것만** 받는다 — 검사는
#: `control.add_fault` 가 한다(여기는 DB 를 보지 않는다).
LIKE_PREFIX = "LIKE:"


def like_fault_id(raw: str | None) -> int | None:
    """`LIKE:66` → 66. 형식이 아니면 `None`."""
    text = (raw or "").strip()
    if not text.upper().startswith(LIKE_PREFIX):
        return None
    num = text[len(LIKE_PREFIX):].strip()
    return int(num) if num.isdigit() else None


def target_rng(seed: int, fault_id: int) -> random.Random:
    """fault 한 건의 전용 rng — 대상 해석이 이것을 **처음으로** 쓴다.

    `resolve_active` 와 `LIKE:` 가 같은 식을 쓰게 한 자리에 둔다. 두 벌이면
    한쪽만 고쳐졌을 때 "같은 대상"이 조용히 다른 대상이 된다.
    """
    return random.Random(seed * 1_000_003 + fault_id)


def _busy_disaster_area(cells: list[Cell], rate: dict[str, float], want: int,
                        mode: str, sid: str, top: int = 40) -> set[str] | None:
    """자연재해 자동 대상 — **정지할 eNB 들의 평소 호율이 가장 높은 지역**(2026-11-05 · F-111).

    15% 생산량에서는 eNB 대부분이 분당 1호 미만이라 graphio D4 가 20분 정지를 확정하지 못했다
    (λ 0.76~0.88 → 확정 24~27분 · 리오 실측). `gw_node_down` 이 표본이 가장 두터운 SGW 를 고르는
    것과 같은 원칙이다 — 임계·생산량은 건드리지 않는다.

    후보는 호율 상위 `top` 개 eNB 를 각각 중심으로 한 지역(`area_cells` + `whole_enbs` · 지금과 같은
    모양)이고, 그 지역에서 **실제로 정지할 eNB**(`disaster_rank_of` · 순위 규칙은 그대로)의
    호율 최솟값이 가장 큰 곳을 고른다 — D4 는 eNB 마다 확정하므로 가장 느린 eNB 가 판정을 늦춘다.
    같으면 합이 큰 쪽, 그다음 eNB 번호. 난수를 쓰지 않는다 — 같은 창고면 같은 지역이다
    """
    first: dict[str, Cell] = {}
    for c in cells:
        if c.geo_status == "MAPPED":
            first.setdefault(c.enb_id, c)
    cand = sorted((e for e in first if rate.get(e, 0.0) > 0.0),
                  key=lambda e: (-rate[e], e))[:top]
    best, best_key = None, None
    for e in cand:
        picked = whole_enbs(cells, area_cells(cells, random.Random(0), want=want,
                                              anchor=first[e]))
        keys = {c.cell_key for c in picked}
        rank = S.disaster_rank_of(keys, picked, mode, sid)
        n = S.disaster_dead_n(len(rank))
        dead = [x for x, r in rank.items() if r < n]
        if not dead:
            continue
        lams = [rate.get(x, 0.0) for x in dead]
        score = (min(lams), sum(lams), -int(e) if e.isdigit() else 0)
        if best_key is None or score > best_key:
            best, best_key = keys, score
    return best


def _meshes(cells: list[Cell], fault: dict, load) -> list[tuple[str, float, list[Cell]]]:
    """`(메시, 평소 15분 인원, 그 메시의 셀)` 목록. 창고를 못 읽으면 빈 목록이다 — 호출부가 추첨으로 돌아간다."""
    if load is None:
        return []
    try:
        got = load(fault["start_ts"])
    except Exception as e:              # 창고 장애로 생성을 멈추지 않는다
        print(f"  ! 메시 인원 조회 실패 — 자동 대상은 군집 추첨({e})", flush=True)
        return []
    by_key = {c.cell_key: c for c in cells if c.geo_status == "MAPPED"}
    out = []
    for mesh in sorted(got or {}):
        people, keys = got[mesh]
        mine = [by_key[k] for k in sorted(keys) if k in by_key]
        if mine:
            out.append((mesh, people, mine))
    return out


def _busy_mesh(people: float, mine: list[Cell]) -> bool:
    return people >= S.FAULT.anchor_min_people and len(mine) >= S.FAULT.anchor_min_cells


def _congestion_target(cells: list[Cell], rng: random.Random, fault: dict, load) -> Target | None:
    """14번 자동 대상 — 평소 사람이 있는 메시 하나와 가장 가까운 메시들의 **셀 전부**(2026-11-06 · F-155).

    Q-11 은 메시·정목 칸의 비율이라 칸 안 사람의 상당수가 나빠져야 선다. `busy_cluster` 의 8셀은
    메시 6칸에 흩어져 주입이 먹힌 셀(불량 51%)이 칸의 RTT 표본 1~11% 뿐이었고, 칸 비율이 0~7.9%
    에 그쳐 미탐이었다(F-129·F-155). 칸을 통째로 잡은 F-136(BOX · 776셀)은 칸 비율 40~57% 로 섰다.
    호는 늘리지 않으므로 범위가 넓어져도 생성량은 그대로다. 주입마다 후보 안에서 돌려 고른다.
    후보가 없거나 창고를 못 읽으면 None 이고 호출부가 kind 의 예전 추첨으로 돌아간다.

    17번도 쓴다 — N-01 도 칸의 비율인데 25셀은 셀이 많은 칸에서 몫이 절반 아래다(메시 셀 수로 본
    확률 61%). 칸을 통째로 잡으면 넘어오는 단말과 평소 단말의 비가 칸 크기와 상관없이 같다.
    """
    from .geo_lite import haversine_km

    meshes = _meshes(cells, fault, load)
    cand = [m for m in meshes if _busy_mesh(m[1], m[2])]
    if not cand:
        return None
    first = cand[rng.randrange(len(cand))]

    def centre(mine: list[Cell]) -> tuple[float, float]:
        return (sum(c.latitude for c in mine) / len(mine), sum(c.longitude for c in mine) / len(mine))

    at = centre(first[2])
    near = sorted(meshes, key=lambda m: (haversine_km(*at, *centre(m[2])), m[0]))
    picked = near[:S.FAULT.congestion_meshes]
    print(f"  · 14번 대상 메시 {[m[0] for m in picked]} (후보 {len(cand)}칸)", flush=True)
    return Target(cell_keys={c.cell_key for m in picked for c in m[2]})


def _anchor_target(cells: list[Cell], rng: random.Random, fault: dict, load) -> Target | None:
    """16번 자동 대상 — 평소 사람이 있는 1km 메시들 중에서 **주입마다 돌려** 고른다(2026-11-06).

    앵커 경합은 5G 단말이 있는 곳의 장애이고, 평소 한산한 칸에 걸면 대상 셀의 사람이 전부 장애가
    만든 사람이 된다(F-153). 가장 붐비는 칸으로 고정하지 않는다 — 바쁜 칸에서만 동작을 확인한 셈이
    되지 않게 조건을 넘는 칸 안에서 추첨한다. 군집은 그 메시 안에서만 짓는다 — 메시를 넘으면 표본이
    두 칸으로 갈려 어느 칸도 판정 표본에 닿지 못한다(F-153 · 53393575·53393584).
    후보가 없거나 창고를 못 읽으면 None 이고, 호출부가 전처럼 군집 추첨을 한다.
    """
    cand = [(mesh, mine) for mesh, people, mine in _meshes(cells, fault, load)
            if _busy_mesh(people, mine)]
    if not cand:
        print("  ! 16번 후보 메시가 없다 — 군집 추첨으로 돌아간다", flush=True)
        return None
    mesh, mine = cand[rng.randrange(len(cand))]
    first = mine[rng.randrange(len(mine))]
    print(f"  · 16번 대상 메시 {mesh} (후보 {len(cand)}칸)", flush=True)
    return Target(cell_keys={c.cell_key for c in enb_cluster(mine, first)},
                  sgg_codes={first.adm_sgg_cd})


def build(fault: dict, cells: list[Cell], anchor: datetime,
          rng: random.Random, seed: int | None = None,
          enb_rate=None, routes=None, nr_on=None, mesh_load=None) -> Scenario | None:
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
    like = like_fault_id(fault.get("target"))
    # 16번은 NR 가능 eNB 안에서만 고른다(2026-11-05 사용자 결정) — 불가 eNB 에는 SA 세션이 없어
    # 걸어도 아무 일이 없다. 자동·`ENB:`·`BOX:`·`TAC:`·`SGG:` 가 한 길로 지나게 후보 셀을 좁힌다
    pick = cells
    if fault["kind"] == "anchor_saturation" and nr_on is not None:
        pick = [c for c in cells if nr_on(str(c.enb_id))]
    mode = None
    raw_target = fault.get("target")
    if fault["kind"] == "natural_disaster":
        # 기지국 정지 배치 — 대상 앞머리 `LOCAL:`·`WIDE:`(또는 그 낱말만). 장애 테이블에 인자 칸이
        # 없어(DDL 없이) 대상 문자열로 받는다. 기본 ``local``
        mode, raw_target = _disaster_mode(raw_target)
    if fault["kind"] == "capacity_shift":
        target = _shift_target(fault.get("target"), fault["start_ts"], cells)
    elif fault["kind"] == "natural_disaster" and not (raw_target or "").strip() and like is None:
        # 자동 선택 — 지역 크기가 배치를 따른다(국소 25셀 · 넓은 80셀 · 초기값)
        # 창고의 eNB 호율이 있으면 **정지 eNB 호율이 가장 높은 지역**(`_busy_disaster_area`), 없으면
        # 전처럼 무작위 지역이다
        want = S.DISASTER_AREA_CELLS.get(mode, 25)
        rate = _rate_at(enb_rate, fault)
        keys = (_busy_disaster_area(cells, rate, want, mode, f"F-{fault['fault_id']}")
                if rate else None)
        if not keys:
            keys = {c.cell_key for c in whole_enbs(cells, area_cells(cells, rng, want=want))}
        target = Target(cell_keys=keys) if keys else None
    elif like is not None:
        # 원본 fault 의 rng 는 **대상 해석에만** 쓴다(조이 결정). 나머지 난수
        # (`place` 의 파형·셀 순서 등)는 이 fault 의 rng 다 — 회차가 독립이다
        if seed is None:
            return None
        target = _resolve_target(None, fault["kind"], pick, target_rng(seed, like), routes)
    else:
        target = None
        if not (raw_target or "").strip():
            if fault["kind"] == "anchor_saturation":
                target = _anchor_target(pick, rng, fault, mesh_load)
            elif fault["kind"] in _CROWD_KINDS or fault["kind"] == "sa_fallback":
                target = _congestion_target(pick, rng, fault, mesh_load)
        if target is None:
            target = _resolve_target(raw_target, fault["kind"], pick, rng, routes)
    if target is None:
        if len(pick) < len(cells):
            print(f"  ! 16번 대상 {raw_target or '자동'} — NR 가능 eNB 셀이 없어 걸지 않는다", flush=True)
        return None
    if mode is not None:
        target.outage_mode = mode
    s = dict(spec)
    s["kind"] = fault["kind"]
    s["target"] = target
    s["peak"] = float(fault["intensity"])
    dur = int(fault["duration_min"])
    if fault["kind"] == "capacity_shift":
        # **길이는 시나리오가 정한다** — 상승·분산·분석 세 국면이 `SHIFT_TOTAL` 분이다.
        # 화면 기본값(10분)으로 자르면 B 가 켜지는 순간 창이 끝난다.
        dur = S.SHIFT_TOTAL
    if fault["kind"] == "natural_disaster":
        # 국면(폭주 → 다운 → 다운만 → 정상화)이 23분이다 — 짧게 주면 다운만 남는 국면이 잘린다
        dur = max(dur, S.DISASTER_DOWN_END)
    steps = s.get("steps") or ()
    if steps:
        dur = max(dur, steps[-1][0])
    dur += max((s.get("series_offset") or {}).values(), default=0)
    sc = place(
        f"F-{fault['fault_id']}", s,
        int((fault["start_ts"] - anchor).total_seconds() // 60), dur, rng,
    )
    # **초 단위 시작 시각** — 시작 분은 버림이라(19:07:18 → 19:07 분) 그 분 전체가 장애로
    # 보인다. 노드 다운은 이 시각 전의 호·세션을 건드리지 않는다(`scenarios.node_down_set`
    # 의 `AT` · 2026-11-04 F-110). epoch 는 레코드 시각(`slot_start = ts.timestamp()`)과 같은 기준이다
    sc.start_ts = fault["start_ts"].timestamp()
    if fault["kind"] in ("natural_disaster", "earthquake"):
        # 정지 순위(`_disaster_rank` local)가 좌표를 본다 — 대상 셀을 실어 둔다
        by_key = {c.cell_key: c for c in cells}
        sc._pool = [by_key[k] for k in sorted(target.cell_keys) if k in by_key]
    if fault["kind"] == "natural_disaster":
        # 정답지의 `down_enbs_rate` — 지정 대상(ENB:·BOX:)이어도 싣는다. 못 읽으면 None
        sc._enb_rate = _rate_at(enb_rate, fault)
        # 폭주 기준값 — 같은 조회의 분당 평균(`EnbRate.mean`)에서 셀 몫으로 나눈다
        # 합이 0 이면 **모름**이다 — 폭주는 그 분 평소 호로 후퇴하고(`pemdb1._surge_drafts` 의 같은
        # 판정), 정답지에도 0.0 이 아니라 null 을 싣는다(불변규칙 17 · 에스터 xfail 11-05)
        base = _surge_base(cells, target.cell_keys, sc._enb_rate)
        total = sum(base.values())
        sc._surge_cell_base = base if total > 0 else {}
        sc._surge_base = total if total > 0 else None
    if fault["kind"] == "enb_s1_outage":
        # 세션을 이웃 eNB 셀로 잇는다(`scenarios._outage_relocate`) — 이웃은 대상 밖이다
        sc._pool_all = cells
    return sc


def _surge_base(cells: list[Cell], keys, rate) -> dict[str, float]:
    """대상 셀 → **장애 시작 앞 60분의 분당 평균 호**(자연재해 폭주의 기준값 · 2026-11-05 F-112).

    창고 값은 eNB 단위(`PM_ENB_KPI_1M`)라 셀 몫은 eNB 평균을 그 eNB 의 셀 수로 나눈다. `BOX:` 처럼
    eNB 를 자른 대상이면 대상 안 셀 몫만 들어간다. 평균이 없으면(조회 실패 · 공급자가 λ 만 준다)
    빈 사전이고, 폭주는 그 분 평소 호로 돌아간다(`pemdb1._surge_drafts`)
    """
    mean = getattr(rate, "mean", None) if rate else None
    if not mean:
        return {}
    per_enb: dict[str, int] = {}
    for c in cells:
        per_enb[c.enb_id] = per_enb.get(c.enb_id, 0) + 1
    by_key = {c.cell_key: c for c in cells}
    out: dict[str, float] = {}
    for k in keys:
        c = by_key.get(k)
        if c is not None and per_enb.get(c.enb_id):
            out[k] = mean.get(str(c.enb_id), 0.0) / per_enb[c.enb_id]
    return out


def _rate_at(enb_rate, fault: dict) -> dict[str, float] | None:
    """eNB 호율 공급자(`pemdb1.enb_call_rate` 를 감싼 것)를 장애 시작 시각으로 부른다 — 한 fault 에
    한 번만(결과를 fault 사전에 들고 있는다). 공급자가 없거나 창고를 못 읽으면 None"""
    if enb_rate is None:
        return None
    if "_enb_rate" not in fault:
        try:
            fault["_enb_rate"] = enb_rate(fault["start_ts"]) or None
        except Exception as e:          # 창고 장애로 생성을 멈추지 않는다 — 무작위 지역으로 간다
            print(f"  ! eNB 호율 조회 실패 — 자연재해 자동 대상은 무작위 지역({e})", flush=True)
            fault["_enb_rate"] = None
    return fault["_enb_rate"]


def _disaster_mode(raw: str | None) -> tuple[str, str | None]:
    """`WIDE:BOX:…` → (``wide``, ``BOX:…``). 앞머리가 없으면 ``local``."""
    text = (raw or "").strip()
    head, sep, rest = text.partition(":")
    if head.upper() in ("LOCAL", "WIDE"):
        return head.lower(), (rest.strip() or None) if sep else None
    return "local", (text or None)


def resolve_active(cells: list[Cell], anchor: datetime, seed: int,
                   active: list[dict],
                   cache: dict[int, tuple[tuple, Scenario]],
                   enb_rate=None, routes=None, nr_on=None, mesh_load=None,
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
        frng = target_rng(seed, fid)
        sc = build(f, cells, anchor, frng, seed=seed, enb_rate=enb_rate, routes=routes,
                   nr_on=nr_on, mesh_load=mesh_load)
        if sc is None:
            bad.append(fid)
        else:
            cache[fid] = (key, sc)
            scs.append(sc)
    return scs, bad
