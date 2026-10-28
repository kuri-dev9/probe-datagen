"""핸드오버 토폴로지 — **전환은 셀만 바꾸는 일이 아니다.**

## 왜 생겼나

전에는 전환 상대를 `cells_by_enb[enb_id]` 에서 골랐다 — **같은 사이트의 다른
섹터**다. 근거는 "다른 eNB 로 보내면 한 호가 두 노드를 탄 것처럼 되어 노드 축
집계가 호 안에서 갈린다"였고(`session._neighbour` · `pemdb1._baseline_pathswitch`
의 옛 주석), 그 걱정 자체는 맞다. 그런데 그렇게 두면 **구조적으로 바뀔 수
없는 것들**이 생긴다. 셀 마스터 실측(999셀 · 333 eNB · 섹터 3개씩) —

    같은 eNB 안에서 MME·SGW·PGW 가 갈리는 사이트     0 / 333
    같은 eNB 안에서 TAC 가 갈리는 사이트             0 / 333
    TAC 총 개수                                   333 = eNB 수

즉 **eNB 가 바뀌지 않으면 장비 ID 도 TAC 도 영원히 그대로**다. 그러면

* `old_call_Last_TAC` · `interval_First_TAC` 가 늘 같은 값이라 TAU 유발
  (`call_type 5`)의 근거가 없다.
* SGW·MME 재배치가 없으니 `s10_error_*`(S10/N14) 구간이 데이터에 안 나타난다.
* 좌표가 섹터 오프셋만큼만 움직여 **가입자가 지리적으로 이동하지 않는다** —
  `j-sc-15`(노선 전환 정체)·메시 롤업이 서지 않는다.

노드 축 걱정은 전환 **종류**로 푼다. 실제 LTE 도 그렇게 갈린다.

## 전환 종류

| 종류 | 셀 | eNB | TAC | MME | SGW | PathSwitch |
| --- | --- | --- | --- | --- | --- | --- |
| `INTRA_ENB` | 바뀜 | 유지 | 유지 | 유지 | 유지 | — |
| `X2` | 바뀜 | 바뀜 | 바뀜 | 유지 | 유지 | **있다** |
| `S1` | 바뀜 | 바뀜 | 바뀜 | 유지 | **바뀜** | — |
| `S1_MME` | 바뀜 | 바뀜 | 바뀜 | **바뀜** | 바뀜 | — |

**`PathSwitch_count > 0` ⟺ X2 다.** X2 핸드오버에서 타겟 eNB 가 MME 로
`PathSwitchRequest` 를 보내는 절차이고, S1 핸드오버는 MME 가 중개하므로 그
메시지가 없다. 섹터 간 전환도 eNB 내부에서 끝나 없다.

**그래서 노드 축이 호 안에서 갈리는 전환은 `S1`·`S1_MME` 뿐이다**(15%).
그 둘은 레코드에 **전환 후** 장비를 싣는다 — 실 XDR 도 한 레코드에 장비
필드가 한 벌뿐이라 마지막 값이 남는다.

## 해제 Cause 와 묶는다

`UEContextRelease_Cause` 가 핸드오버 Cause 인데 `First == Last` 인 호가 있었다
(해제 사유는 실측 분포에서 **독립으로** 뽑히고 셀은 안 움직였다). 사람이 읽는
모순이고, 불변규칙 6 의 테스트 자료로도 쓸 수 없다. 그래서 둘을 묶는다 —
**해제 Cause 가 핸드오버면 셀이 반드시 바뀐다.**

Cause 는 `codes.is_normal_release()` 가 정본이고 여기 집합은 그중
**실제로 셀이 바뀌는 것**만 고른 부분집합이다(`session.HANDOVER_CAUSES` 와
같은 자리에서 왔다). `120`(User inactivity, 실측 94.4%)·`300`(Normal Release)은
이동이 아니라 그냥 끝난 것이므로 넣지 않는다.
"""

from __future__ import annotations

import hashlib
import math
from collections import defaultdict

from .cells import Cell

# --------------------------------------------------------------------------
# 전환 종류
# --------------------------------------------------------------------------

HO_INTRA = "INTRA_ENB"   #: 섹터 간 — eNB 내부. 장비·TAC 유지
HO_X2 = "X2"             #: eNB 간. MME·SGW 유지. **PathSwitchRequest 가 있다**
HO_S1 = "S1"             #: eNB 간 + SGW 재배치. MME 유지
HO_S1_MME = "S1_MME"     #: 위 + MME 재배치 (S10)

#: 종류별 비중. 섹터 간이 가장 흔하고 MME 재배치가 가장 드물다 —
#: MME 풀은 넓게 잡혀 있어(실측 MME 24대 / eNB 333) 인접 eNB 가 같은 MME 를
#: 쓰는 경우가 대부분이다.
KIND_W: dict[str, float] = {HO_INTRA: 0.55, HO_X2: 0.30,
                            HO_S1: 0.12, HO_S1_MME: 0.03}

#: `PathSwitch_count` 를 남기는 종류. **X2 만이다** (머리말).
PATHSWITCH_KINDS = frozenset({HO_X2})

#: eNB 가 바뀌는 종류 — TAC 도 함께 바뀐다(실측: TAC 는 eNB 당 1개).
INTER_ENB_KINDS = frozenset({HO_X2, HO_S1, HO_S1_MME})

#: 종류별 `UEContextRelease_Cause` 후보 (D.3.1). `codes.is_normal_release()`
#: 가 정본이고 이 표는 "그중 이 전환에 맞는 것"을 고른다.
REL_BY_KIND: dict[str, tuple[str, ...]] = {
    # `104`(Handover Cancelled)는 **취소돼도 셀이 바뀌는 경우**가 있어
    # `session.HANDOVER_CAUSES` 에 들어 있다. 여기서 빠뜨렸더니 두 집합이
    # 갈렸고(`test_release_cause_maps_to_every_kind`), 그러면 C-Plane 과
    # U-Plane 이 **서로 다른 호를 핸드오버로 본다** — 대조가 성립하지 않는다.
    HO_INTRA: ("102", "104", "105"),       # Successful / Cancelled / Partial
    HO_X2: ("135", "102"),                 # X2 Handover triggered
    HO_S1: ("133", "117", "118"),          # S1 Intra system / time-critical
    HO_S1_MME: ("134", "122"),             # S1 Inter system / Load Balancing TAU
}

#: 해제 Cause → **그 Cause 와 맞는 종류 전부.** 위 표의 역방향이다.
#:
#: **값이 집합이어야 한다.** `{c: k for k, cs in ... for c in cs}` 로 만들면
#: 두 종류에 걸친 Cause 에서 **뒤의 것이 앞을 조용히 덮는다.** `102`
#: (Successful Handover)가 `INTRA_ENB` 와 `X2` 양쪽에 있어서 실제로 그랬고,
#: 그 결과 C-Plane 에서 `INTRA_ENB` 가 **한 건도 안 나왔다**(실측 20,000호 중
#: 섹터 전환 0). 에러는 없고 종류 분포만 틀어진다.
#:
#: 걸치는 것이 자연스럽다 — `102`·`105` 는 "전환에 성공했다"는 말이라 섹터
#: 간인지 eNB 간인지 구분하지 않는다. 그런 Cause 는 후보를 좁히지 말고
#: **가중 추첨에 맡긴다**(`pick_kind(only=...)`).
KINDS_BY_REL: dict[str, tuple[str, ...]] = {}
for _k, _cs in REL_BY_KIND.items():
    for _c in _cs:
        KINDS_BY_REL[_c] = KINDS_BY_REL.get(_c, ()) + (_k,)

#: 셀이 바뀌는 해제 Cause 전체. `session.HANDOVER_CAUSES` 와 **같은 집합이어야
#: 한다** — 어긋나면 C-Plane 과 U-Plane 이 서로 다른 호를 핸드오버로 본다.
HANDOVER_CAUSES = frozenset(KINDS_BY_REL)

#: 격자 한 칸의 크기(도). 위도 1도 ≈ 111km 이므로 0.02도 ≈ 2.2km 다. 인접
#: 사이트를 찾는 데 쓰는 버킷 크기이고 정확도가 아니라 **후보를 줄이는** 용도다.
_GRID = 0.02


def _dist2(a: Cell, b: Cell) -> float:
    """제곱 거리(도). 경도는 위도에 따라 줄어들므로 `cos(lat)` 로 보정한다.

    루트를 씌우지 않는다 — 정렬에만 쓰므로 단조성만 있으면 된다.
    """
    k = math.cos(math.radians(a.latitude)) or 1.0
    dy = a.latitude - b.latitude
    dx = (a.longitude - b.longitude) * k
    return dy * dy + dx * dx


class Topology:
    """셀 → 전환 가능한 이웃. **생성기 수명 동안 한 번 만든다.**

    사이트 수가 늘면 전수 비교가 `O(n²)` 이라 격자 버킷으로 후보를 줄인다.
    실측 999셀/333사이트에서는 전수도 괜찮지만, 운영 셀 마스터는 44만 행이라
    (`CM_CELL_REP_RU_LOC`) 그대로 두면 기동이 멈춘다.
    """

    __slots__ = ("intra", "inter", "_by_key")

    def __init__(self, cells: list[Cell], k: int = 4) -> None:
        #: 같은 eNB 의 다른 섹터
        self.intra: dict[str, tuple[Cell, ...]] = {}
        #: 인접 eNB 의 셀 — **k-최근접이다.** 고정 반경으로 하면 비는 곳이
        #: 생긴다(실측: 1.3km 반경이면 200 사이트 중 95개가 이웃 0개).
        #: 이웃이 없는 셀은 전환을 못 하고, 그러면 그 셀에서는 Q-08 이 영원히
        #: 0 이다 — 에러가 아니라 **없는 정상**으로 보인다.
        self.inter: dict[str, tuple[Cell, ...]] = {}
        self._by_key = {c.cell_key: c for c in cells}

        by_enb: dict[str, list[Cell]] = defaultdict(list)
        for c in cells:
            by_enb[c.enb_id].append(c)
        for c in cells:
            self.intra[c.cell_key] = tuple(
                x for x in by_enb[c.enb_id] if x is not c)

        # 사이트 대표 셀(섹터 0 쪽) 하나로 격자를 만든다
        sites = {e: v[0] for e, v in by_enb.items()}
        grid: dict[tuple[int, int], list[Cell]] = defaultdict(list)
        for s in sites.values():
            grid[(int(s.latitude / _GRID), int(s.longitude / _GRID))].append(s)

        near: dict[str, tuple[str, ...]] = {}
        for enb, s in sites.items():
            gy, gx = int(s.latitude / _GRID), int(s.longitude / _GRID)
            cand: list[Cell] = []
            r = 1
            # 후보가 k개 모일 때까지 고리를 넓힌다. 외딴 사이트도 이웃을
            # 얻는다 — **반경을 고정하면 그 셀이 전환에서 빠진다.**
            while len(cand) <= k and r <= 64:
                cand = [o for dy in range(-r, r + 1) for dx in range(-r, r + 1)
                        for o in grid.get((gy + dy, gx + dx), ())
                        if o.enb_id != enb]
                r *= 2
            cand.sort(key=lambda o: _dist2(s, o))
            near[enb] = tuple(o.enb_id for o in cand[:k])

        for c in cells:
            pool: list[Cell] = []
            for e in near.get(c.enb_id, ()):
                pool.extend(by_enb[e])
            self.inter[c.cell_key] = tuple(pool)

    # -- 선택 ------------------------------------------------------------

    def kinds(self, cell: Cell) -> tuple[str, ...]:
        """이 셀에서 **실제로 가능한** 전환 종류.

        이웃이 없는 종류를 돌려주면 호출부가 "전환했다"고 적고도 셀이 그대로인
        레코드를 낸다 — 집계는 그것을 **일어나지 않은 전환**으로 센다.
        """
        out = []
        if self.intra.get(cell.cell_key):
            out.append(HO_INTRA)
        if self.inter.get(cell.cell_key):
            out.extend((HO_X2, HO_S1, HO_S1_MME))
        return tuple(out)

    def pick_kind(self, cell: Cell, rng, only=None) -> str | None:
        """가중 추첨. `only` 로 후보를 좁힌다(예: PathSwitch 가 있는 전환만)."""
        ks = [k for k in self.kinds(cell) if only is None or k in only]
        if not ks:
            return None
        w = [KIND_W[k] for k in ks]
        x = rng.random() * sum(w)
        for k, v in zip(ks, w):
            x -= v
            if x <= 0:
                return k
        return ks[-1]

    def target(self, cell: Cell, kind: str, rng) -> Cell | None:
        """전환 상대. 없으면 `None` — 호출부가 전환을 **취소해야 한다.**"""
        pool = (self.intra if kind == HO_INTRA else self.inter).get(
            cell.cell_key, ())
        if not pool:
            return None
        t = pool[rng.randrange(len(pool))]
        return None if t is cell else t

    def pick(self, cell: Cell, rng, only=None) -> tuple[Cell, str] | None:
        """종류와 상대를 한 번에. 둘 중 하나라도 없으면 `None`."""
        kind = self.pick_kind(cell, rng, only)
        if kind is None:
            return None
        t = self.target(cell, kind, rng)
        return None if t is None else (t, kind)


# --------------------------------------------------------------------------
# 이동 궤적 — **전환 상대를 추첨하지 않는다**
# --------------------------------------------------------------------------
#
# `Topology.pick` 은 이웃을 매번 새로 뽑는다. 한 전환만 보면 맞지만 **이어 놓고
# 보면 가입자가 순간이동한다** — A 에서 B 로 갔다가 다음 전환에 A 의 반대쪽
# C 로 간다. 그러면
#
# * 셀 쌍 표(장표 9)에 의미 있는 쌍이 안 쌓인다 — 모든 쌍이 한두 건이다.
# * 노선 코리도 롤업이 서지 않는다. 코리도는 **순서**가 있는 구간이다.
# * `j-sc-15`(야마노테선 4G↔5G 전환 정체)가 "특정 구간"을 가리킬 수 없다.
#
# 그래서 **경로를 데이터로 둔다.** 셀 키를 순서대로 적으면 가입자가 그 순서로
# 움직이고, 전환은 추첨이 아니라 **다음 칸**이다.
#
# 두 가지로 만들 수 있다 —
#
# | 출처 | 언제 |
# | --- | --- |
# | `data/routes.json` | 실제 노선·역 순서를 아는 경우. **손으로 적는다** |
# | `build_routes()` | 노선 정의가 없을 때. 최근접 체인으로 그럴듯한 경로를 깐다 |
#
# 스펙 §7.2 의 `corridor.geojson` 이 들어오면 세 번째 출처가 된다(노선 버퍼와
# 교차하는 셀을 순서대로). 그때 이 자료구조를 바꾸지 않아도 되게 **경로는
# 셀 목록 + 구간 체류시간**이라는 가장 얇은 모양으로 둔다.


class Route:
    """순서가 있는 셀 경로. **가입자가 이 순서로 움직인다.**

    `dwell_sec` 은 각 칸의 체류시간이다. 길이가 `cells` 와 같아야 한다 —
    짧으면 뒤쪽 칸이 기본값으로 떨어지고, 그러면 **노선 끝이 항상 빠르게
    지나가는** 편향이 생긴다.

    `loop` 는 순환선이다(야마노테선). `False` 면 끝에서 되돌아 온다 —
    사라지게 두면 이동 가입자가 시간이 갈수록 줄어든다.
    """

    __slots__ = ("id", "name", "cells", "dwell_sec", "loop",
                 "station_of", "stations", "station_pos")

    def __init__(self, id: str, name: str, cells: tuple[Cell, ...],
                 dwell_sec: tuple[int, ...], loop: bool = False,
                 station_of: tuple[str, ...] = (),
                 stations: dict | None = None,
                 station_pos: dict | None = None) -> None:
        if len(cells) < 2:
            raise ValueError(f"route {id}: 셀이 2개 미만이다")
        if len(dwell_sec) != len(cells):
            raise ValueError(
                f"route {id}: dwell_sec {len(dwell_sec)} != cells {len(cells)}")
        self.id = id
        self.name = name
        self.cells = cells
        self.dwell_sec = dwell_sec
        self.loop = loop
        #: 칸별 대표 역 코드 — **표시용**이다. 칸 하나가 여러 역을 지날 수 있다
        self.station_of = station_of
        #: 역 코드 → 표시명
        self.stations = stations or {}
        #: 역 코드 → 칸 index. **여정을 역 이름으로 적는 수단이다.**
        #:
        #: `station_of` 의 역방향으로 만들면 안 된다 — 칸이 셀 중복 제거로
        #: 줄면 역 하나가 통째로 빠진다(실측: 야마노테선 시나가와가
        #: 다카나와·오사키 칸에 흡수돼 사라졌다). 그러면 "시나가와역에서
        #: 탄다"를 적을 수 없고, 원인이 노선 생성 쪽이라 **설정을 봐도
        #: 안 보인다.** 그래서 노선 파일이 색인을 따로 들고 온다.
        self.station_pos = station_pos or {}

    def pos_of(self, station: str) -> int | None:
        """역 코드 → 칸. 모르는 역은 `None` — 호출부가 멈춰야 한다."""
        return self.station_pos.get(station)

    def __len__(self) -> int:
        return len(self.cells)

    def step(self, pos: int, forward: bool) -> tuple[int, bool]:
        """다음 칸과 진행 방향. **끝에서 사라지지 않는다.**

        순환선이면 돌고, 아니면 방향을 뒤집는다 — 상행/하행이 그렇게 생긴다.
        """
        n = len(self.cells)
        nxt = pos + (1 if forward else -1)
        if 0 <= nxt < n:
            return nxt, forward
        if self.loop:
            return nxt % n, forward
        forward = not forward
        return pos + (1 if forward else -1), forward

    def kind(self, a: int, b: int) -> str:
        """두 칸 사이 전환의 종류 — `kind_of` 에 맡긴다."""
        return kind_of(self.cells[a], self.cells[b])


def kind_of(src: Cell, dst: Cell) -> str:
    """셀 둘 사이 전환의 종류. **쌍마다 고정이고 추첨이 아니다.**

    경로를 따라 움직이면 상대가 이미 정해져 있다. 그때 종류까지 추첨하면
    "같은 구간인데 어떤 호는 X2, 어떤 호는 S1" 이 되어 셀 쌍 표(장표 9)가
    "이 쌍은 이런 전환"이라고 말할 수 없다. 그래서 쌍을 해시해 **결정론적**
    으로 고른다 — 난수를 쓰지 않으므로 같은 쌍은 늘 같은 종류다.

    **셀 마스터의 `mme_id`·`sgw_id` 차이로 추론하지 않는다.** 그게 자연스러워
    보이지만 실측이 막는다 — 마스터는 MME 를 지리와 무관하게 배정하고
    (24 MME · 45 SGW / 333 eNB), 그래서 인접 eNB 가 거의 늘 다른 MME 다.
    그대로 읽었더니 경로 구간 73개 중 **55개가 MME 재배치**로 나왔다(X2 는 4개).
    실제 망은 MME 풀이 지리적이라 이웃은 보통 같은 MME 를 쓴다 — 재배치는
    풀 경계에서만 일어난다.

    대신 비중(`KIND_W`)을 재현한다. 코어가 실제로 **무엇으로** 바뀌는지는
    호출부가 대상 셀의 마스터 값에서 가져간다(`S1`·`S1_MME` 일 때만).
    """
    if src.enb_id == dst.enb_id:
        return HO_INTRA
    # **TA 가 있으면 마스터를 읽는다**(2026-10-27).
    #
    # 장비 배정이 TA 에서 유도되면(`pemdb1.load_cells` · 도쿄 세트) 인접 eNB 가
    # 대개 같은 MME 를 쓴다 — 실측 86%. 그때 마스터가 말하는 것이 사실이고,
    # 해시로 덮으면 **지리와 맞는 사실을 지우는 셈**이다. 실측 분포도 비중표와
    # 가깝다: X2 78.5% · S1 15.3% · S1_MME 6.2%(기대 66.7 · 26.7 · 6.7).
    #
    # **TA 가 없으면 읽을 수 없다.** KR 벌은 `TAC` 가 비어 있어 장비가 eNB
    # 해시로 배정되고(옛 경로), 그러면 인접 eNB 가 거의 늘 다른 MME 다 —
    # 경로 구간 73개 중 55개가 MME 재배치로 나왔던 그 상태다. 그때는
    # 아래 쌍 해시로 비중만 재현한다.
    if src.tac and dst.tac:
        if src.mme_id != dst.mme_id:
            return HO_S1_MME
        if src.sgw_id != dst.sgw_id:
            return HO_S1
        return HO_X2
    # 방향에 무관하게 같은 종류여야 한다 — 상행과 하행이 같은 구간이다
    key = "|".join(sorted((src.cell_key, dst.cell_key)))
    x = (int(hashlib.blake2b(key.encode(), digest_size=8).hexdigest(), 16)
         % 1_000_000) / 1_000_000
    inter = (HO_X2, HO_S1, HO_S1_MME)
    tot = sum(KIND_W[k] for k in inter)
    for k in inter:
        x -= KIND_W[k] / tot
        if x <= 0:
            return k
    return HO_X2


def build_routes(topo: "Topology", cells: list[Cell], n: int, rng,
                 length: int = 8, dwell: tuple[int, int] = (45, 150),
                 loop_share: float = 0.25) -> list[Route]:
    """최근접 체인으로 경로 ``n`` 개를 깐다 — **노선 정의가 없을 때.**

    출발 셀을 고르고 인접 eNB 로 `length` 칸을 이어 붙인다. **이미 지난
    eNB 로 돌아가지 않는다** — 돌아가면 같은 쌍을 왕복해 셀 쌍 표가 두 칸에
    몰린다.

    `dwell` 은 칸당 체류시간 범위(초)다. 45~150초면 8칸을 6~20분에 지난다 —
    세션 수명(평균 3분)보다 길어 **한 세션이 여러 칸을 겪는다.** 그게
    "이동 중 전환"의 모양이다.
    """
    out: list[Route] = []
    pool = [c for c in cells if topo.inter.get(c.cell_key)]
    if not pool:
        return out
    for i in range(n):
        cur = pool[rng.randrange(len(pool))]
        seen = {cur.enb_id}
        chain = [cur]
        while len(chain) < length:
            nxt = [c for c in topo.inter.get(cur.cell_key, ())
                   if c.enb_id not in seen]
            if not nxt:
                break
            cur = min(nxt, key=lambda c: _dist2(chain[-1], c))
            seen.add(cur.enb_id)
            chain.append(cur)
        if len(chain) < 2:
            continue
        out.append(Route(
            id=f"RT-{i:03d}", name=f"auto-{chain[0].enb_id[:6]}",
            cells=tuple(chain),
            dwell_sec=tuple(rng.randint(*dwell) for _ in chain),
            loop=rng.random() < loop_share,
        ))
    return out


def load_routes(path, cells: list[Cell]) -> list[Route]:
    """`routes.json` 에서 읽는다 — **실제 노선을 그대로 쓰는 길.**

    ```json
    [{"id": "YMT", "name": "야마노테선 외선", "loop": true,
      "cells": ["ENB001-0", "ENB002-1", "..."],
      "dwell_sec": [90, 75, 110],
      "station_of": ["JY25", "JY25", "JY24"],
      "stations": {"JY25": "시나가와", "JY24": "오사키"},
      "station_pos": {"JY25": 0, "JY24": 2}}]
    ```

    **모르는 셀 키는 멈춘다.** 조용히 건너뛰면 노선이 짧아지고, 그러면
    "이 구간에서 전환이 안 일어난다"는 결과가 나오는데 원인이 설정 오타다.
    `dwell_sec` 을 생략하면 전부 90초로 둔다.
    """
    import json
    from pathlib import Path

    p = Path(path)
    by_key = {c.cell_key: c for c in cells}
    out: list[Route] = []
    for e in json.loads(p.read_text(encoding="utf-8")):
        keys = e["cells"]
        bad = [k for k in keys if k not in by_key]
        if bad:
            raise ValueError(
                f"routes {e['id']}: 셀 마스터에 없는 키 {bad[:5]}"
                f"{' …' if len(bad) > 5 else ''}")
        dwell = e.get("dwell_sec") or [90] * len(keys)
        out.append(Route(
            id=e["id"], name=e.get("name", e["id"]),
            cells=tuple(by_key[k] for k in keys),
            dwell_sec=tuple(int(x) for x in dwell),
            loop=bool(e.get("loop", False)),
            station_of=tuple(e.get("station_of") or ()),
            stations=e.get("stations") or {},
            station_pos={k: int(v) for k, v in
                         (e.get("station_pos") or {}).items()}))
    return out


# --------------------------------------------------------------------------
# TAI List — **TA 가 바뀐다고 TAU 가 나는 것이 아니다**
# --------------------------------------------------------------------------
#
# LTE 는 TAU 를 TA 경계마다 하지 않는다. MME 가 UE 에게 TA **목록**(TAI List)
# 을 주고, 목록 **밖**의 TA 에 들어갈 때만 TAU 가 난다. 그것이 도심의 잘은
# TA 에서 ping-pong TAU 를 막는 장치다.
#
# ## 왜 이 층이 필요한지는 실데이터가 알려 줬다
#
# OpenCellID 도쿄 도코모 실측(2026-10-27) —
#
#     TAC 658개 · **신주쿠 2km 안에 TAC 49개**
#     이웃 셀 쌍 중 TA 경계를 넘는 것  **77%**
#
# TAC 변화만으로 TAU 를 내면 전환할 때마다 TAU 가 난다. 전에 합성 격자
# (2.2km = 한 TA)로 그럴듯한 빈도(5분마다)를 얻었는데, 그건 **TA List 를
# TA 로 착각해 만든 것**이었다. 실데이터가 그 혼동을 드러냈다.
#
# ## 목록은 지리로 묶는다
#
# 실제 TAC 번호는 지리를 **느슨하게만** 반영한다(실측: 번호가 4 이내인 TAC
# 쌍의 거리 중앙 5.8km · 임의 쌍 12.6km). 그래서 `tac // N` 같은 산술로는
# 묶이지 않는다 — 좌표로 묶는다.
#
# **결정론이다.** 난수를 쓰지 않는다 — 목록이 실행마다 바뀌면 같은 전환이
# 어떤 날에는 TAU 를 내고 어떤 날에는 안 낸다.

#: TAI List 한 칸의 크기(도). 0.08도 ≈ 8.9km.
#:
#: 실측에서 격자별 결과 — `0.03`(3.3km) List 141 · `0.05`(5.6km) 65 ·
#: `0.08`(8.9km) 33 · `0.12`(13.4km) 20. 야마노테선 둘레가 32.7km 이고 한
#: 바퀴가 90분이므로, 8.9km 목록이면 한 바퀴에 4~6번 넘어 **TAU 가 15~20분
#: 마다**다. 통근 이동에서 그 정도가 실제에 가깝다.
TA_LIST_GRID_DEG = 0.08

#: TAI List 번호의 시작값. `0` 은 "목록 미정"으로 이미 쓰고 있어 피한다.
TA_LIST_BASE = 1


def build_ta_lists(cells: list[Cell], grid: float = TA_LIST_GRID_DEG) -> int:
    """TA 를 지리적으로 묶어 `Cell.ta_list` 를 채운다. 만든 목록 수를 돌려준다.

    **같은 TA 의 셀은 같은 목록이다.** TA 하나가 두 목록에 걸치면 그 TA 안에서
    움직이는 것만으로 TAU 가 난다 — 목록은 TA 의 묶음이지 셀의 묶음이 아니다.
    그래서 TA 의 **중심점**으로 격자 칸을 정하고 그 TA 전체를 거기 넣는다.

    **좌표 없는 셀은 `ta_list = 0` 으로 남는다.** 위치를 모르면 목록도 모른다
    (불변규칙 12·17). 그 셀에서는 목록 경계가 없는 것으로 읽혀 TAU 가 나지
    않는다 — 지어내는 것보다 낫다.

    **셀 목록을 제자리에서 고친다.** 복사해 돌려주면 호출부가 어느 쪽을
    쓰는지 갈려, 한쪽은 목록이 있고 한쪽은 `0` 인 상태가 생긴다.
    """
    pts: dict[int, list[Cell]] = defaultdict(list)
    for c in cells:
        if c.tac:
            pts[c.tac].append(c)
    if not pts:
        return 0
    # TA 중심점 → 격자 칸
    key_of: dict[int, tuple[int, int]] = {}
    for tac, group in pts.items():
        la = sum(c.latitude for c in group) / len(group)
        lo = sum(c.longitude for c in group) / len(group)
        key_of[tac] = (int(la / grid), int(lo / grid))
    # ── 번호 매기기 — **상위 묶음이 번호에 박혀 있어야 한다** ──────────
    #
    # 장비를 목록 번호에서 유도한다(`pemdb1._TA_PER_MME` 등). 그러려면
    # `id // 4` 가 **지리적으로 붙은 목록 4개**여야 한다.
    #
    # Z-order 로 정렬해 순위를 매기는 것만으로는 부족했다 — 실측에서
    # `S1` 9.8% · `S1_MME` 14.9% 로 뒤집혔다(기대 26.7 · 6.7). 순위는
    # 근접을 **대체로** 보존할 뿐이라 `id // 4` 가 2×2 블록과 어긋난다.
    #
    # 그래서 **두 단계로 매긴다**: 먼저 두 배 격자(상위 묶음)를 Z-order 로
    # 세우고, 그 안에서 목록에 0~3 을 준다. 그러면 `id // 4` 가 상위 묶음과
    # **정확히** 같다 — 근사가 아니다.
    # **상위 묶음은 4×4 다**(= 최대 16 목록 · ≈36km).
    #
    # 2×2 로 두었더니 `S1` 7.8% · `S1_MME` 16.9% 로 여전히 뒤집혔다. 격자
    # 묶음은 **경계 쌍**이 늘 생기고, 블록이 작을수록 그 비중이 크다 —
    # 2×2 면 목록 경계를 넘는 쌍의 절반쯤이 블록도 함께 넘는다.
    #
    # MME 영역이 목록보다 **훨씬** 커야 `S1`(SGW 만)이 흔하고
    # `S1_MME`(MME 까지)가 드물어진다. 실제 망도 MME 풀이 넓은 지역을 맡는다.
    coarse: dict[tuple[int, int], list[tuple[int, int]]] = defaultdict(list)
    for k in set(key_of.values()):
        coarse[(k[0] // 4, k[1] // 4)].append(k)
    ids: dict[tuple[int, int], int] = {}
    for rank, ck in enumerate(sorted(coarse, key=_morton)):
        for sub, k in enumerate(sorted(coarse[ck], key=_morton)):
            # 한 상위 묶음에 4×4 = 최대 16칸이므로 `sub` 는 0~15 다
            ids[k] = TA_LIST_BASE + rank * 16 + sub
    for tac, group in pts.items():
        v = ids[key_of[tac]]
        for c in group:
            c.ta_list = v
    return len(ids)


def _morton(key: tuple[int, int]) -> int:
    """격자 좌표 → Z-order 값. 2차원 근접을 1차원 번호에 보존한다."""
    y, x = key[0] + (1 << 20), key[1] + (1 << 20)
    z = 0
    for b in range(21):
        z |= ((x >> b) & 1) << (2 * b)
        z |= ((y >> b) & 1) << (2 * b + 1)
    return z


# --------------------------------------------------------------------------
# 이동 궤적 — **전환 상대를 추첨하지 않는다**
# --------------------------------------------------------------------------
#
# `Topology.pick` 은 이웃을 매번 새로 뽑는다. 한 전환만 보면 맞지만 **이어 놓고
# 보면 가입자가 순간이동한다** — A 에서 B 로 갔다가 다음 전환에 A 의 반대쪽
# C 로 간다. 그러면
#
# * 셀 쌍 표(장표 9)에 의미 있는 쌍이 안 쌓인다 — 모든 쌍이 한두 건이다.
# * 노선 코리도 롤업이 서지 않는다. 코리도는 **순서**가 있는 구간이다.
# * `j-sc-15`(야마노테선 4G↔5G 전환 정체)가 "특정 구간"을 가리킬 수 없다.
#
# 그래서 **경로를 데이터로 둔다.** 셀 키를 순서대로 적으면 가입자가 그 순서로
# 움직이고, 전환은 추첨이 아니라 **다음 칸**이다.
#
# 두 가지로 만들 수 있다 —
#
# | 출처 | 언제 |
# | --- | --- |
# | `data/routes.json` | 실제 노선·역 순서를 아는 경우. **손으로 적는다** |
# | `build_routes()` | 노선 정의가 없을 때. 최근접 체인으로 그럴듯한 경로를 깐다 |
#
# 스펙 §7.2 의 `corridor.geojson` 이 들어오면 세 번째 출처가 된다(노선 버퍼와
# 교차하는 셀을 순서대로). 그때 이 자료구조를 바꾸지 않아도 되게 **경로는
# 셀 목록 + 구간 체류시간**이라는 가장 얇은 모양으로 둔다.


