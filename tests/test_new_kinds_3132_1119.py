"""신규 31번 절단 급증(`ran_drop_surge`) · 32번 전송 구간 절단(`transport_cut`) 생성기 (2026-11-19 · 맥스 구현).

DB 없이 돈다 — 셀 마스터는 `data/cells.json`. 헬퍼는 `test_fault_scenarios` 와 같은 것을 쓴다.
31: 연결된 호만 121 로 끊고 접속 실패·지연은 건드리지 않는다(C-02 그대로, C-04 만 상승) · 정상 종료 14개 원인을 쓰지 않는다.
32: 정지 국면(강도 ≥ `OUTAGE_GATE`)에 대상 지역 새 호 0 · eNB 묶음 모두 다운 · 세션은 지역 밖으로 · 복구 국면 재접속 배율 1.8(초기값).
"""
from __future__ import annotations

import random

import pytest

from datagen import control, faults
from datagen import scenarios as SC
from datagen.pemdb1 import NORMAL_RELEASE
from test_fault_scenarios import _ready_lte, _sc, gen  # noqa: F401  (gen 은 픽스처)

GATE = SC.OUTAGE_GATE


# ── 31 절단 급증 ──────────────────────────────────────────────────────────

def _drop_sc(cell):
    sc = _sc("ran_drop_surge", target=SC.Target(cell_keys={cell.cell_key}))
    sc.active_cells_cache = {cell.cell_key}
    return sc


def test_31_spec_expects_c04_radio_quality_and_r20():
    s = SC.KIND_SPECS["ran_drop_surge"]
    assert (s["expect_kpi"], s["truth_code"], s["expect_geo"]) == ("C-04", "RAN_RADIO_QUALITY", "CLUSTER")
    assert tuple(s["expect_rules"]) == ("R-20",)


def test_31_drops_only_connected_calls_with_cause_121(gen):
    rng = random.Random(33)
    hit = n = 0
    for _ in range(1500):
        rec, cell = _ready_lte(gen, rng, attempt=1)
        sc = _drop_sc(cell)
        n += 1
        if SC.MUTATORS["ran_drop_surge"](rec, sc, 1.0, rng, cell):
            hit += 1
            assert rec.get("drop_flag") == 1 or str(rec.get("drop_flag")) in ("1", "True")
            assert int(rec.num("first_error_cause")) == 121
            assert int(rec.num("UEContextReleaseRequest_Cause")) == 121
    # 강도 1.0 에서 연결된 호의 최대 18%(= 0.60 × 0.30)다. `callflow.drop` 이 절단을 허락하는 호 유형·길이만
    # 끊으므로(2번 무선 열화와 같다) 실측은 그보다 낮다 — 상한을 넘지 않고 눈에 띌 만큼은 나야 한다
    assert 0.08 < hit / n < 0.20, hit / n


def test_31_cause_121_is_not_a_normal_release():
    assert "121" not in {str(c) for c in NORMAL_RELEASE} and 121 not in set(NORMAL_RELEASE)


def test_31_leaves_unconnected_calls_alone(gen):
    rng = random.Random(34)
    for _ in range(300):
        rec, cell = _ready_lte(gen, rng, attempt=0)
        before = rec.get("drop_flag")
        assert SC.MUTATORS["ran_drop_surge"](rec, _drop_sc(cell), 1.0, rng, cell) is False
        assert rec.get("drop_flag") == before


def test_31_does_not_touch_access_failure_or_delay(gen):
    """2번 무선 열화와 다른 점 — 접속 실패·지연이 없다(소통률 그대로)."""
    rng = random.Random(35)
    for _ in range(600):
        rec, cell = _ready_lte(gen, rng, attempt=1)
        d0, s0, a0 = rec.get("initial_access_duration"), rec.get("success_flag"), rec.get("attempt_flag")
        SC.MUTATORS["ran_drop_surge"](rec, _drop_sc(cell), 1.0, rng, cell)
        assert rec.get("initial_access_duration") == d0, "접속 지연을 늘렸다"
        assert (rec.get("success_flag"), rec.get("attempt_flag")) == (s0, a0), "접속 성공·시도 플래그를 바꿨다"
        assert int(rec.num("first_error_cause")) in (0, 121)


def test_31_ignores_cells_outside_the_target(gen):
    rng = random.Random(36)
    rec, cell = _ready_lte(gen, rng, attempt=1)
    sc = _sc("ran_drop_surge", target=SC.Target(cell_keys={cell.cell_key}))
    sc.active_cells_cache = set()
    assert not any(SC.MUTATORS["ran_drop_surge"](_ready_lte(gen, rng, attempt=1)[0], sc, 1.0, rng, cell)
                   for _ in range(200))


def test_31_zero_intensity_changes_nothing(gen):
    rng = random.Random(37)
    assert not any(SC.MUTATORS["ran_drop_surge"](r, _drop_sc(c), 0.0, rng, c)
                   for r, c in (_ready_lte(gen, rng, attempt=1) for _ in range(200)))


# ── 32 전송 구간 절단 ─────────────────────────────────────────────────────

def _cut_sc(gen, n_cells=6):
    """한 지역 셀 묶음 — 같은 eNB 두 곳 이상을 걸치게 잡는다."""
    by = {}
    for c in gen.cells:
        by.setdefault(c.enb_id, []).append(c)
    enbs = [e for e, cs in by.items() if len(cs) >= 2][:3]
    cells = [c for e in enbs for c in by[e][:2]]
    sc = _sc("transport_cut", duration=17, target=SC.Target(cell_keys={c.cell_key for c in cells}))
    sc._pool_all = gen.cells
    return sc, cells, set(map(str, enbs))


def test_32_curve_is_15_minutes_down_then_two_recovery_minutes():
    sc = _sc("transport_cut", duration=17)
    assert all(sc.intensity(m) == pytest.approx(1.0) for m in range(0, 15))
    assert sc.intensity(15) == pytest.approx(0.40) and sc.intensity(16) == pytest.approx(0.40)
    assert sc.intensity(17) == 0.0
    assert 0.40 < GATE <= 1.0, "복구 계단은 게이트 아래여야 한다"


def test_32_spec_expects_node_down_r36_c01():
    s = SC.KIND_SPECS["transport_cut"]
    assert (s["truth_code"], s["expect_kpi"], s["expect_geo"]) == ("RAN_NODE_DOWN", "C-01", "CLUSTER")
    assert tuple(s["expect_rules"]) == ("R-36",)


def test_32_enb_set_is_read_from_cell_keys(gen):
    sc, cells, enbs = _cut_sc(gen)
    assert {str(e) for e in SC.transport_cut_enbs(sc)} == {str(c.cell_key).rsplit("-", 1)[0] for c in cells}


def test_32_suppressor_blocks_target_cells_only_in_the_down_phase(gen):
    sc, cells, _ = _cut_sc(gen)
    rng = random.Random(1)
    fn = SC.SUPPRESSORS["transport_cut"]
    assert all(fn(sc, c, 1.0, rng) for c in cells)                                 # 정지 국면
    assert not any(fn(sc, c, 0.40, rng) for c in cells)                            # 복구 국면
    other = next(c for c in gen.cells if c.cell_key not in sc.target.cell_keys)
    assert not fn(sc, other, 1.0, rng)                                             # 대상 밖


def test_32_all_enbs_of_the_cluster_are_down_together_and_back_together(gen):
    sc, cells, enbs = _cut_sc(gen)
    sc.start_minute = 100
    for m in range(100, 115):
        down = SC.node_down_set([sc], m)["ENB"]
        assert set(map(str, down)) >= {str(e) for e in SC.transport_cut_enbs(sc)}, f"{m}분에 일부 eNB 가 살아 있다"
        assert set(down.values()) == {100}, "다운 시작 분이 eNB 마다 다르다"
    for m in (115, 116, 117):
        assert SC.node_down_set([sc], m)["ENB"] == {}, f"{m}분에 아직 다운이다(같이 복구해야 한다)"


def test_32_sessions_relocate_outside_the_target_area(gen):
    sc, cells, _ = _cut_sc(gen)
    fn = SC.RELOCATORS["transport_cut"] if hasattr(SC, "RELOCATORS") else None
    assert fn is not None
    for c in cells:
        dst = fn(sc, c, 1.0, None)
        assert dst is not None and dst.cell_key not in sc.target.cell_keys
        assert fn(sc, c, 1.0, None) is dst                                         # 셀마다 한 번만 찾는다
        assert fn(sc, c, 0.40, None) is None                                       # 복구 국면에는 옮기지 않는다
    other = next(c for c in gen.cells if c.cell_key not in sc.target.cell_keys)
    assert fn(sc, other, 1.0, None) is None


def test_32_reconnect_surge_is_18x_only_in_recovery(gen):
    sc, _, _ = _cut_sc(gen)
    fn = SC.BOOSTERS["transport_cut"]
    assert fn(sc, 1.0) == 1.0
    assert fn(sc, 0.40) == pytest.approx(1.8)
    assert fn(sc, 0.0) == pytest.approx(1.0)                                       # 장애 밖(강도 0)은 배율 없음


def test_32_has_no_call_flood_during_the_down_phase(gen):
    """정지 국면에 호를 늘리는 장치를 걸지 않는다(27번 지진과 다르다)."""
    sc, _, _ = _cut_sc(gen)
    assert SC.BOOSTERS["transport_cut"](sc, 1.0) == 1.0


# ── 등록 ───────────────────────────────────────────────────────────────────

def test_kinds_are_registered_in_gen_and_control():
    assert "ran_drop_surge" in control.KINDS and "transport_cut" in control.KINDS
    assert "ran_drop_surge" in faults._AUTO_SGG
    assert "transport_cut" in faults._AREA_KINDS


def test_scenarios_31_and_32_are_ready_and_33_is_still_pending():
    rows = {r[0]: r for r in control.SCENARIOS}
    assert rows[31][2] == "ran_drop_surge" and rows[32][2] == "transport_cut"
    assert rows[33][2] is None
    assert 34 not in rows and 35 not in rows       # 옛 31·32(3·4번과 같은 데이터)는 지웠다(사용자 11-19)
