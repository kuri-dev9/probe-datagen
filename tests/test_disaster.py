"""자연재해 시나리오(27번 · `natural_disaster`) — 호 폭주 + 기지국 장애 (2026-11-04 사용자 결정).

국면(장애 시작 분 기준 · 초기값): 0~13분 폭주(2분에 걸쳐 정점 6배) · 3~23분 지역 eNB 40% 정지 · 23분 정상화.
상위 장비(SGW·PGW·MME)는 묶지 않는다. **eNB 가 죽으면 코어 세션은 살아 있다** — 데이터만 멈추고 이웃 셀에서
같은 세션이 이어진다(SGW 다운과 다르다 · 세션을 끊지 않는다).

**DB 를 쓰지 않는다** — `test_node_down.py` 와 같은 하니스(합성 셀 마스터를 `PemdbGen` 에 끼운다).
옛 `earthquake`(배치 `J-SC-27`)는 `test_scenarios_quake.py` 가 그대로 지킨다.
"""

from __future__ import annotations

import collections
import math
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from datagen import faults
from datagen import pemdb1 as P
from datagen import scenarios as S
from datagen import session as SE
from datagen.cells import Cell

ROOT = Path(__file__).resolve().parents[1]
T0 = datetime(2026, 11, 4, 20, 0)
START = 5                                       # 장애 시작 분(정각 — 다운 시각은 START + 3 분의 시작)


# --------------------------------------------------------------------------
# 합성 셀 · 시나리오
# --------------------------------------------------------------------------


def _cell(enb: str, n: int, lat: float, lon: float) -> Cell:
    return Cell(
        cell_key=f"{enb}-{n}", enb_id=enb, cell_id=n, enb_seq=0, c_uid="",
        tac=100, plmn="44010", vlan_id="", latitude=lat, longitude=lon,
        adm_sido_cd="11", adm_sido_nm="", adm_sgg_cd="11200", adm_sgg_nm="",
        adm_emd_cd="11200011", adm_emd_nm="", geo_status="MAPPED",
        rat="EUTRAN", mme_id="0001", sgw_id="0037", pgw_id="0037",
        ims_pgw_id="0037", traffic_weight=1.0, display_name="")


def _master() -> list[Cell]:
    """eNB 60개 × 3셀, 격자 배치(조금씩 벌린다) — 국소 25셀 · 넓은 지역 80셀이 모두 잡힌다."""
    out = []
    for e in range(60):
        lat = 35.60 + (e // 10) * 0.012
        lon = 139.60 + (e % 10) * 0.012
        out += [_cell(f"{600000 + e}", n, lat + n * 0.0015, lon + n * 0.0015) for n in range(3)]
    return out


def _row(kind="natural_disaster", start=START, dur=25, target=None, sec=0):
    return {"fault_id": 27, "kind": kind, "target": target, "start_ts": T0 + timedelta(minutes=start, seconds=sec),
            "duration_min": dur, "intensity": 1.0}


@pytest.fixture
def make(monkeypatch):
    def build(rate: int = 1500):
        for f in ("pemdb1/_distributions.json", "profile.json"):
            if not (ROOT / "data" / f).exists():
                pytest.fail(f"data/{f} 가 없다")
        monkeypatch.setattr(P, "load_cells", lambda *a, **k: _master())
        monkeypatch.setattr(P, "load_apps", lambda conn: None)
        g = P.PemdbGen(None, ROOT / "data", rates={"lte_call": rate, "transport": rate}, seed=11, subscriber_pool=2500)
        g.anchor = T0
        return g
    return build


def _run(g, minutes: int, rows: list[dict]):
    scs, bad = g.set_fault_rows(rows)
    assert scs and not bad, f"장애가 해석되지 않았다: {bad}"
    out = []
    for m in range(minutes):
        ts = T0 + timedelta(minutes=m)
        out.append((g.records(m, ts), g.uplane_records(m, ts)))
    return out, scs[0]


def _col(g, series, name):
    return g.gen.index[series][name]


def _area_calls(g, rows, sc):
    i = _col(g, "lte_call", "First_eNB_ID")
    enbs = {k.rsplit("-", 1)[0] for k in sc.target.cell_keys}
    return [r for r in rows if r[i] in enbs]


# --------------------------------------------------------------------------
# 1. 국면 — 폭주 배율 · 정지 집합 · 다운 시각 (단위)
# --------------------------------------------------------------------------


def _unit_sc(n_enbs=10, **kw):
    cells = _master()[: n_enbs * 3]
    spec = dict(S.KIND_SPECS["natural_disaster"])
    spec.update(kind="natural_disaster", target=S.Target(cell_keys={c.cell_key for c in cells}, **kw))
    sc = S.Scenario(id="T-27", start_minute=START, duration=25, **spec)
    sc.__dict__["_pool"] = cells
    return sc, cells


def test_surge_ramps_over_two_minutes_holds_the_peak_and_ends_at_thirteen():
    sc, _ = _unit_sc()
    mult = [S.disaster_surge_mult(sc, START + e) for e in range(-2, 16)]
    by = {e: S.disaster_surge_mult(sc, START + e) for e in range(-2, 16)}
    assert by[-2] == by[-1] == 1.0, "시작 전은 평소다"
    assert 1.0 < by[0] < by[1], "0~2분에 걸쳐 오른다"
    assert by[1] == pytest.approx(S.DISASTER_SURGE_PEAK) == by[2] == by[12], "2~13분은 정점이다"
    assert by[13] == by[14] == by[15] == 1.0, "13분부터 배율 1 이다"
    # 정점 40배 — 동일본 대지진 실측(발신 약 50배 · 착신 약 20배) 기준의 **초기값**(11-05 · 결정 대기)
    assert S.DISASTER_SURGE_PEAK == 40.0 and (S.DISASTER_SURGE_RAMP, S.DISASTER_SURGE_END) == (2, 13)
    assert by[0] == pytest.approx(20.5), "0분째는 정점과 평소의 중간(약 20.5배)이다"
    assert max(mult) <= S.DISASTER_SURGE_PEAK + 1e-9


def test_surge_scales_with_intensity_and_other_kinds_are_untouched():
    sc, _ = _unit_sc()
    half = S.Scenario(id="T", start_minute=START, duration=25, **{**dict(S.KIND_SPECS["natural_disaster"]),
                      "kind": "natural_disaster", "target": sc.target, "peak": 0.5})
    assert S.disaster_surge_mult(half, START + 5) == pytest.approx(1.0 + (S.DISASTER_SURGE_PEAK - 1.0) * 0.5)
    eq = S.Scenario(id="E", start_minute=START, duration=25, **{**dict(S.KIND_SPECS["earthquake"]),
                    "kind": "earthquake", "target": sc.target})
    assert S.disaster_surge_mult(eq, START + 5) == 1.0 and not S.disaster_dead_enbs(eq, START + 5)


def test_down_phase_is_three_to_twenty_three_minutes_with_a_fixed_set_of_forty_percent():
    sc, cells = _unit_sc(n_enbs=10)
    n = len({c.enb_id for c in cells})
    sets = {e: S.disaster_dead_enbs(sc, START + e) for e in range(-1, 26)}
    for e in (-1, 0, 1, 2):
        assert not sets[e], f"{e}분: 다운 전인데 정지 eNB 가 있다"
    for e in (23, 24, 25):
        assert not sets[e], f"{e}분: 정상화 뒤인데 정지 eNB 가 남았다"
    first = sets[3]
    assert len(first) == math.ceil(0.4 * n)
    for e in range(3, 23):
        assert sets[e] == first, f"{e}분: 정지 집합이 흔들렸다"
    assert (S.DISASTER_DOWN_AT, S.DISASTER_DOWN_END) == (3, 23) and S.DISASTER_DOWN_RATIO == pytest.approx(0.4)


def test_down_time_is_the_start_of_the_down_minute():
    sc, _ = _unit_sc()
    assert S.disaster_down_at(sc) is None, "시작 시각을 모르면 None"
    for sec in (0, 18, 59):
        sc.start_ts = (T0 + timedelta(minutes=START, seconds=sec)).timestamp()
        assert S.disaster_down_at(sc) == (T0 + timedelta(minutes=START + 3)).timestamp(), sec


def test_local_takes_the_enbs_nearest_the_center_and_wide_scatters_them():
    from datagen.geo_lite import haversine_km
    local, cells = _unit_sc(n_enbs=40, outage_mode="local")
    wide, _ = _unit_sc(n_enbs=40, outage_mode="wide")
    pos = {}
    for c in cells:
        pos.setdefault(c.enb_id, (c.latitude, c.longitude))
    # 중심은 제품과 같이 **전 셀 평균**이다(eNB 첫 셀 평균과 0.2km 쯤 다르다)
    la = sum(c.latitude for c in cells) / len(cells)
    lo = sum(c.longitude for c in cells) / len(cells)
    dist = {e: haversine_km(la, lo, *p) for e, p in pos.items()}
    dl = S.disaster_dead_enbs(local, START + 5)
    dw = S.disaster_dead_enbs(wide, START + 5)
    assert len(dl) == len(dw) == math.ceil(0.4 * 40)
    assert max(dist[e] for e in dl) <= min(dist[e] for e in set(pos) - dl) + 1e-9, "local 은 중심에 가까운 eNB 부터다"
    assert dw != dl
    assert max(dist[e] for e in dw) > max(dist[e] for e in dl), "wide 는 중심 밖까지 흩어진다"


# --------------------------------------------------------------------------
# 2. 대상 문자열 — LOCAL · WIDE · 빈 값 · BOX · 길이 하한
# --------------------------------------------------------------------------


def _build(target, dur=25):
    import random
    return faults.build({"fault_id": 27, "kind": "natural_disaster", "target": target,
                         "start_ts": T0 + timedelta(minutes=START, seconds=18), "duration_min": dur,
                         "intensity": 1.0}, _master(), T0, random.Random(5), seed=11)


def test_empty_target_is_a_local_area_of_about_twenty_five_cells():
    sc = _build(None)
    assert sc is not None and (sc.target.outage_mode or "local") == "local"
    assert 25 <= len(sc.target.cell_keys) <= 25 + 3 * 3, len(sc.target.cell_keys)   # eNB 단위로 넓힌다(셀 3개씩)
    assert len({k.rsplit("-", 1)[0] for k in sc.target.cell_keys}) * 3 == len(sc.target.cell_keys)


def test_local_and_wide_words_pick_the_mode_and_the_area_size():
    loc, wide = _build("LOCAL"), _build("WIDE")
    assert loc.target.outage_mode == "local" and wide.target.outage_mode == "wide"
    assert len(wide.target.cell_keys) > 2 * len(loc.target.cell_keys)
    assert 80 <= len(wide.target.cell_keys) <= 80 + 3 * 3


def test_wide_with_a_box_keeps_the_mode_and_uses_the_box():
    box = "WIDE:BOX:35.60,139.60,35.66,139.66"
    sc = _build(box)
    assert sc is not None and sc.target.outage_mode == "wide"
    assert sc.target.cell_keys
    cells = {c.cell_key: c for c in _master()}
    assert all(35.60 - 0.02 <= cells[k].latitude <= 35.66 + 0.02 and 139.60 - 0.02 <= cells[k].longitude <= 139.66 + 0.02
               for k in sc.target.cell_keys)


@pytest.mark.parametrize("dur", [10, 1, 23])
def test_short_durations_are_stretched_to_the_full_twenty_three_minutes(dur):
    sc = _build("LOCAL", dur=dur)
    assert sc.duration >= S.DISASTER_DOWN_END, sc.duration


def test_start_ts_is_carried_to_the_scenario_with_second_precision():
    sc = _build("LOCAL")
    assert sc.start_ts == pytest.approx((T0 + timedelta(minutes=START, seconds=18)).timestamp())
    assert sc.start_minute == START


# --------------------------------------------------------------------------
# 3. 호 폭주 — 양 · 구성 · 규모 무관
# --------------------------------------------------------------------------


def _disaster_run(g, minutes=30, target="LOCAL"):
    return _run(g, minutes, [_row(target=target)])


def _baseline_area_mean(g, runs, sc, lo=0, hi=START):
    return sum(len(_area_calls(g, runs[m][0], sc)) for m in range(lo, hi)) / (hi - lo)


def test_dense_harness_surge_is_big_and_the_national_cap_holds(make):
    """셀당 평소 호가 하한보다 많은 **조밀한** 하니스 — 정점 40배는 전국 상한(더하는 호 ≤ 전국 평소의 2배)에
    먼저 걸린다. 그래서 여기서는 크기의 하한과 상한(전국 3배)만 본다. 40배 자체는 아래 희소 하니스가 본다."""
    g = make()
    runs, sc = _disaster_run(g)
    base = _baseline_area_mean(g, runs, sc)
    assert base > 30, "지역 평소 호가 너무 적어 배율을 못 잰다"
    for m in (START + 1, START + 2):                       # 정점이지만 다운 전(0~3분)
        ratio = len(_area_calls(g, runs[m][0], sc)) / base
        assert ratio >= 10, f"{m}분: 지역 호가 평소의 {ratio:.1f}배 — 폭주가 작다"
    total_base = sum(len(runs[m][0]) for m in range(START)) / START
    for m in range(START, START + 13):
        assert len(runs[m][0]) <= 3.0 * total_base * 1.1, f"{m}분: 전국 호가 평소의 3배를 넘었다"
    assert len(runs[START + 14][0]) < 1.5 * total_base, "13분 뒤에는 폭주가 가라앉아야 한다"


def test_surge_ratio_is_the_same_at_a_larger_generation_volume(make):
    ratios = []
    for rate in (1500, 4500):
        g = make(rate)
        runs, sc = _disaster_run(g, minutes=START + 4)
        base = _baseline_area_mean(g, runs, sc)
        ratios.append(len(_area_calls(g, runs[START + 2][0], sc)) / base)
    assert abs(ratios[0] - ratios[1]) / ratios[0] < 0.25, ratios


def test_surge_adds_more_voice_than_data_and_more_mo_than_mt_without_touching_the_normal_mix(make):
    g = make()
    runs, sc = _disaster_run(g, minutes=START + 4)
    i = _col(g, "lte_call", "call_type")
    base = collections.Counter(r[i] for m in range(START) for r in _area_calls(g, runs[m][0], sc))
    base = {k: v / START for k, v in base.items()}
    during = collections.Counter(r[i] for m in (START + 1, START + 2) for r in _area_calls(g, runs[m][0], sc))
    extra = {k: during.get(k, 0) / 2 - base.get(k, 0) for k in ("3", "4", "7", "8")}
    assert extra["7"] + extra["8"] > extra["3"] + extra["4"], extra
    assert 1.8 <= (extra["3"] + extra["7"]) / max(1.0, extra["4"] + extra["8"]) <= 3.6, extra
    # 지역 밖 호의 구성은 그대로다(폭주는 경보 지역에만)
    enbs = {k.rsplit("-", 1)[0] for k in sc.target.cell_keys}
    fi = _col(g, "lte_call", "First_eNB_ID")
    out = [r for r in runs[START + 2][0] if r[fi] not in enbs]
    assert sum(1 for r in out if r[i] in ("7", "8")) < 0.02 * len(out)


def test_before_the_start_nothing_is_added(make):
    g = make()
    runs, sc = _disaster_run(g, minutes=START + 1)
    base = _baseline_area_mean(g, runs, sc, 0, START - 1)
    assert len(_area_calls(g, runs[START - 1][0], sc)) < 1.5 * base


# --------------------------------------------------------------------------
# 4. 정지 — 호 0건 · 이웃에 나타남 · 복귀
# --------------------------------------------------------------------------


def test_dead_enbs_have_no_calls_while_down_and_come_back_at_twenty_three(make):
    g = make()
    runs, sc = _disaster_run(g, minutes=START + 26)
    fi = _col(g, "lte_call", "First_eNB_ID")
    li = _col(g, "lte_call", "Last_eNB_ID")
    dead = S.disaster_dead_enbs(sc, START + 5)
    assert dead
    before = sum(1 for m in range(START) for r in runs[m][0] if r[fi] in dead)
    assert before > 0, "정지할 eNB 에 평소 호가 있어야 한다"
    for m in range(START + 3, START + 23):
        rows = runs[m][0]
        assert not [r for r in rows if r[fi] in dead or r[li] in dead], f"{m}분: 정지 eNB 의 호가 남았다"
    assert sum(1 for m in range(START + 24, START + 26) for r in runs[m][0] if r[fi] in dead) > 0, "23분 뒤 복귀하지 않았다"
    assert not [r for r in runs[START + 2][0] if False]                # 다운 전 분은 정지가 없다(위 `before` 가 이미 본다)


def test_calls_of_dead_cells_move_to_live_neighbours_instead_of_vanishing(make):
    """다운만 남은 국면(13~23분)에도 지역 호 총량이 평소 근처다 — 정지 셀의 호가 이웃 셀로 옮겨 간다."""
    g = make()
    runs, sc = _disaster_run(g, minutes=START + 22)
    base = _baseline_area_mean(g, runs, sc)
    during = [len(_area_calls(g, runs[m][0], sc)) for m in range(START + 15, START + 21)]
    mean = sum(during) / len(during)
    assert 0.8 * base <= mean <= 1.8 * base, (base, mean)


def test_no_uplane_session_runs_on_a_dead_enb_after_the_reconnect_window(make):
    g = make()
    runs, sc = _disaster_run(g, minutes=START + 12)
    dead = S.disaster_dead_enbs(sc, START + 5)
    t = g.gen.index["transport"]
    for m in range(START + 4, START + 12):                 # 재연결 지연(≤30초)이 지난 뒤
        rows = [r for r in runs[m][1].get("transport", []) if r[t["eNB_ID"]] in dead]
        assert not rows, f"{m}분: 정지 eNB 의 U-Plane 레코드 {len(rows)}건"


# --------------------------------------------------------------------------
# 5. 세션 — 끊기지 않고 이웃 셀에서 같은 세션으로 이어진다
# --------------------------------------------------------------------------


def _hold_log(g):
    log = []
    orig = g._enb_down_hold

    def wrapped(s, *a, **k):
        log.append(s.sid)
        return orig(s, *a, **k)

    g._enb_down_hold = wrapped
    return log


def _reconnect_log(g):
    log = []
    orig = g._reconnect_rec

    def wrapped(out, ts, slot_start, imsi, *a, **k):
        before = len(out)
        orig(out, ts, slot_start, imsi, *a, **k)
        if len(out) > before:
            log.append((round((ts - T0).total_seconds() / 60), imsi, out[-1]))

    g._reconnect_rec = wrapped
    return log


def test_sessions_on_a_dead_enb_are_held_not_cut_and_continue_on_a_live_cell(make):
    g = make()
    hold = _hold_log(g)
    g.anchor = T0
    scs, bad = g.set_fault_rows([_row()])
    assert scs and not bad
    sc = scs[0]
    dead = S.disaster_dead_enbs(sc, START + 4)
    for m in range(START + 3):                              # 다운 직전 분까지
        g.records(m, T0 + timedelta(minutes=m))
        g.uplane_records(m, T0 + timedelta(minutes=m))
    victims = [s for s in g.sessions.active if str(s.cell.enb_id) in dead]
    assert len(victims) > 5, "정지 eNB 에 열린 세션이 거의 없다"
    ids = {s.sid for s in victims}
    m = START + 3                                           # 다운 분
    g.records(m, T0 + timedelta(minutes=m))
    g.uplane_records(m, T0 + timedelta(minutes=m))
    alive = [s for s in victims if s in g.sessions.active]
    assert alive, "다운 분에 세션이 전부 사라졌다 — 끊으면 안 된다"
    for s in alive:
        assert s.cut_t < 0, "eNB 다운이 세션을 끊었다(코어 세션은 살아 있어야 한다)"
        assert str(s.cell.enb_id) not in dead, "정지 eNB 에 그대로 남았다 — 이웃 셀로 이어져야 한다"
    assert set(hold) & ids, "정지 처리(`_enb_down_hold`)가 열린 세션에 걸리지 않았다"


def test_the_same_session_continues_in_the_next_minutes_under_a_live_enb(make):
    g = make()
    runs, sc = _disaster_run(g, minutes=START + 8)
    dead = S.disaster_dead_enbs(sc, START + 5)
    t = g.gen.index["transport"]
    key = lambda r: (r[t["IMSI"]], r[t["Traffic_Total_Uplink_FirstTime"]])
    victims = {key(r) for r in runs[START + 2][1].get("transport", []) if r[t["eNB_ID"]] in dead}
    assert victims
    later = [r for m in range(START + 4, START + 8) for r in runs[m][1].get("transport", []) if key(r) in victims]
    assert later, "정지 eNB 의 세션이 이어지지 않았다 — 같은 세션이 이웃 셀에서 계속돼야 한다"
    assert all(r[t["eNB_ID"]] not in dead for r in later)


def test_reconnect_service_request_is_one_successful_mo_call_per_subscriber_without_a_new_session(make):
    g = make()
    log = _reconnect_log(g)
    runs, sc = _disaster_run(g, minutes=START + 8)
    assert log, "재연결 Service Request 가 하나도 없다"
    i = g.gen.index["lte_call"]
    assert {m for m, _, _ in log} <= {START + 4}, "다운 분 다음 분에 남는다"
    dead = S.disaster_dead_enbs(sc, START + 5)
    for _m, _imsi, vals in log:
        assert vals[i["call_type"]] == "3" and vals[i["success_flag"]] in ("1", "True") and vals[i["drop_flag"]] in ("0", "False", "")
        assert vals[i["First_eNB_ID"]] not in dead, "재연결은 살아 있는 셀에서 난다"


def test_reconnect_service_request_is_one_per_subscriber(make):
    g = make()
    log = _reconnect_log(g)
    _disaster_run(g, minutes=START + 8)
    seen = collections.Counter((m, imsi) for m, imsi, _ in log)
    assert seen and max(seen.values()) == 1, f"같은 분 같은 가입자의 재연결 최대 {max(seen.values())}건"


def test_reconnect_ends_after_the_reconnect_time_and_inside_its_own_minute(make):
    g = make()
    log = _reconnect_log(g)
    _disaster_run(g, minutes=START + 6)
    i = g.gen.index["lte_call"]
    slot = (T0 + timedelta(minutes=START + 4)).timestamp()
    down_at = (T0 + timedelta(minutes=START + 3)).timestamp()
    assert log
    for _m, _imsi, vals in log:
        end = float(vals[i["call_end_time"]])
        assert down_at <= end, "재연결이 다운 시각보다 앞선다"
        assert slot - 1e-6 <= end < slot + 60 + 1e-6, f"재연결 끝 시각이 그 분 밖이다 ({end - slot:.1f}초)"


def test_no_handover_into_a_dead_enb_while_down(make):
    g = make()
    runs, sc = _disaster_run(g, minutes=START + 14)
    dead = S.disaster_dead_enbs(sc, START + 5)
    li = _col(g, "lte_call", "Last_eNB_ID")
    for m in range(START + 3, START + 14):
        assert not [r for r in runs[m][0] if r[li] in dead], f"{m}분: 정지 eNB 로 끝나는 호"


# --------------------------------------------------------------------------
# 6. SC-17 enb_s1_outage — 같은 원칙(세션을 끊지 않는다)
# --------------------------------------------------------------------------


def _outage_row(enb="600025", start=START, dur=10):
    return _row(kind="enb_s1_outage", start=start, dur=dur, target=f"ENB:{enb}")


def test_enb_outage_keeps_the_session_and_continues_it_on_another_enb_with_no_new_calls(make):
    g = make()
    scs, bad = g.set_fault_rows([_outage_row()])
    assert scs and not bad
    sc = scs[0]
    enb = "600025"
    gate = next(m for m in range(sc.start_minute, sc.start_minute + sc.duration) if sc.intensity(m) >= S.OUTAGE_GATE)
    for m in range(gate):
        g.records(m, T0 + timedelta(minutes=m))
        g.uplane_records(m, T0 + timedelta(minutes=m))
    victims = [s for s in g.sessions.active if str(s.cell.enb_id) == enb]
    assert len(victims) > 3, "대상 eNB 에 열린 세션이 거의 없다"
    ci = _col(g, "lte_call", "First_eNB_ID")
    for m in range(gate, gate + 3):
        rows = g.records(m, T0 + timedelta(minutes=m))
        g.uplane_records(m, T0 + timedelta(minutes=m))
        assert not [r for r in rows if r[ci] == enb], f"{m}분: 다운 eNB 의 새 호가 남았다(무호여야 한다)"
    alive = [s for s in victims if s in g.sessions.active]
    assert alive, "단절 뒤 세션이 전부 사라졌다 — 끊으면 안 된다"
    for s in alive:
        assert s.cut_t < 0 and str(s.cell.enb_id) != enb, "세션이 끊겼거나 다운 eNB 에 남았다"


def test_enb_outage_emits_no_cplane_release_records_for_the_held_sessions(make):
    g = make()
    runs, sc = _run(g, START + 10, [_outage_row()])
    i = g.gen.index["lte_call"]
    gate = next(m for m in range(sc.start_minute, sc.start_minute + sc.duration) if sc.intensity(m) >= S.OUTAGE_GATE)
    cuts = [r for m in range(gate, gate + 3) for r in runs[m][0]
            if r[i["drop_flag"]] in ("1", "True") and r[i["First_eNB_ID"]] == "600025"]
    assert not cuts, "eNB 다운은 C-Plane 절단 레코드를 내지 않는다(MME 의 로컬 해제 · 프로브에 남지 않는다)"


# --------------------------------------------------------------------------
# 7. 옛 earthquake — 그대로 읽힌다
# --------------------------------------------------------------------------


def test_the_old_earthquake_kind_is_still_a_separate_spec_with_its_own_behavior():
    assert "earthquake" in S.KIND_SPECS and "natural_disaster" in S.KIND_SPECS
    assert S.KIND_SPECS["earthquake"]["title"] != S.KIND_SPECS["natural_disaster"]["title"]
    assert "자연재해" in S.KIND_SPECS["natural_disaster"]["title"]
    sc, _ = _unit_sc()
    eq = S.Scenario(id="E", start_minute=START, duration=25, **{**dict(S.KIND_SPECS["earthquake"]),
                    "kind": "earthquake", "target": sc.target})
    assert S.disaster_surge_mult(eq, START + 2) == 1.0                    # 옛 지진은 폭주가 없다


# --------------------------------------------------------------------------
# 8. 재연결은 가입자 단위 (맥스 11-05 · `_enb_down_hold`) — 단말 하나는 Service Request 한 번으로 모든 베어러를 되살린다
# --------------------------------------------------------------------------


def _hold_calls(g):
    """`_enb_down_hold` 가 끝난 직후 (가입자, 세션, 넘어가는 시각, 데이터 멈춤 길이(초)) 를 모은다."""
    rows = []
    orig = g._enb_down_hold

    def wrapped(s, minute, slot_start, down, hit, relocate, later):
        before = s.sid in g.__dict__.get("_enb_held", set())
        orig(s, minute, slot_start, down, hit, relocate, later)
        if not before and s.sid in g.__dict__.get("_enb_held", set()):
            fz = s.freeze
            rows.append((s.user[0], s.sid, s.ho_at_t, (fz[1] - fz[0]) * max(1, s.span) * 60.0 if fz else None))

    g._enb_down_hold = wrapped
    return rows


def test_all_sessions_of_one_subscriber_move_at_the_same_second_with_the_same_freeze_length(make):
    g = make(4500)
    rows = _hold_calls(g)
    _disaster_run(g, minutes=START + 6)
    by = collections.defaultdict(list)
    for imsi, sid, ho, gap in rows:
        by[imsi].append((sid, ho, gap))
    multi = {k: v for k, v in by.items() if len(v) >= 2}
    assert multi, "세션이 둘 이상인 가입자가 없다 — 시나리오 규모를 키워야 한다"
    for imsi, v in multi.items():
        hos = {round(ho, 3) for _sid, ho, _gap in v}
        assert len(hos) == 1, f"{imsi}: 세션마다 넘어가는 시각이 다르다 {sorted(hos)}"
        gaps = {round(gap, 2) for _sid, _ho, gap in v if gap is not None}
        assert len(gaps) <= 1, f"{imsi}: 데이터 멈춤 길이가 다르다 {sorted(gaps)}"


def test_a_second_down_of_the_same_subscriber_draws_a_new_reconnect_time(make):
    """같은 다운이면 묶고, 다른 장애로 다시 다운되면(다운 시각이 다르면) 재연결 시각을 새로 뽑는다."""
    g = make()
    g.anchor = T0
    for m in range(4):
        g.records(m, T0 + timedelta(minutes=m))
        g.uplane_records(m, T0 + timedelta(minutes=m))
    s = next(x for x in g.sessions.active if x.span >= 3)
    enb = str(s.cell.enb_id)
    slot = (T0 + timedelta(minutes=4)).timestamp()

    def down(at):
        return {"SGW": {}, "PGW": {}, "ENB": {enb: 4}, "AT": {("ENB", enb): at}}

    g._enb_down_hold(s, 4, slot, down(slot + 10), ("ENB", enb, 4), None, [])
    first = list(g._enb_reconn[s.user[0]])
    g._enb_held.discard(s.sid)
    g._enb_down_hold(s, 4, slot, down(slot + 10), ("ENB", enb, 4), None, [])           # 같은 다운 — 그대로
    assert g._enb_reconn[s.user[0]][:2] == first[:2]
    g._enb_held.discard(s.sid)
    g._enb_down_hold(s, 4, slot, down(slot + 40), ("ENB", enb, 4), None, [])           # 다른 다운 — 새로 뽑는다
    again = g._enb_reconn[s.user[0]]
    assert again[0] == slot + 40 and again[1] != first[1]
    assert S.DISASTER_RECONNECT_S[0] <= again[1] - again[0] <= S.DISASTER_RECONNECT_S[1]
    assert first[1] - first[0] >= S.DISASTER_RECONNECT_S[0]


# --------------------------------------------------------------------------
# 9. 정답지 — 자연재해 맥락 키 (`scenarios._disaster_truth` · `as_truth`)
# --------------------------------------------------------------------------


def _truth_sc(make, kind="natural_disaster", target=None):
    g = make()
    scs, bad = g.set_fault_rows([_row(kind=kind, target=target, sec=18)])
    assert scs and not bad
    return scs[0]


def test_disaster_truth_carries_the_context_phases_and_the_dead_enb_list(make):
    sc = _truth_sc(make)
    t = sc.as_truth()
    # 11-05 사용자 결정: 세 신호(호 폭주 + eNB 무호 + Emergency 증가)를 합친 판정이 정답이다 — 지진 단정 없음
    assert t["truth_code"] == "NATURAL_DISASTER_SUSPECTED" and t["expect_kpi"] == "C-01"
    assert t["truth_category"] == "EXTERNAL", "재해는 망 밖 원인이다"
    assert t["expect_rules"] == ["R-46"] and not t.get("rule_pending")
    assert t["expect_alternatives"] == {"R-36": "RAN_NODE_DOWN"}, "무호만 본 노드 장애는 대안으로 남는다"
    assert t["outage_mode"] == "local"
    assert t["context"] == [{"code": "CALL_SURGE", "from_min": 0, "to_min": 13, "cells": sorted(sc.target.cell_keys)}]
    assert t["phases"] == {"surge": [0, 13], "down": [3, 23]}
    assert t["down_enbs"] == sorted(S.disaster_dead_enbs(sc, sc.start_minute + 5)), "정답지의 정지 eNB 가 실제 정지 집합과 다르다"
    assert t["down_at_ts"] == S.disaster_down_at(sc) == pytest.approx((T0 + timedelta(minutes=START + 3)).timestamp())


def test_disaster_truth_outage_mode_follows_the_target_word(make):
    assert _truth_sc(make, target="WIDE").as_truth()["outage_mode"] == "wide"
    assert _truth_sc(make, target="LOCAL").as_truth()["outage_mode"] == "local"


@pytest.mark.parametrize("kind", ["gw_node_down", "enb_s1_outage", "earthquake"])
def test_other_kinds_have_no_disaster_keys_in_their_truth(make, kind):
    sc = _truth_sc(make, kind=kind)
    t = sc.as_truth()
    for k in ("outage_mode", "context", "phases", "down_enbs", "down_at_ts"):
        assert k not in t, f"{kind}: 정답지에 자연재해 키 {k} 가 있다"
    assert t["scenario_id"]


def test_disaster_truth_is_written_by_the_live_truth_path(make, tmp_path):
    """실시간 주입의 정답지 파일(`cli._live_truth` → `pemdb1.write_truth`)에도 같은 키가 실린다 — 리오가 주입 뒤 바로 본다.
    다른 종류의 문항 옆에서도 자연재해 키는 그 문항에만 붙는다."""
    import json
    sc = _truth_sc(make)
    path = tmp_path / "truth.json"
    P.write_truth(path, T0, 40, [sc])
    row = json.loads(path.read_text(encoding="utf-8"))["scenarios"][0]
    assert row["context"][0]["code"] == "CALL_SURGE" and row["context"][0]["to_min"] == 13
    assert row["down_enbs"] == sc.as_truth()["down_enbs"] and row["phases"]["down"] == [3, 23]
    assert row["truth_code"] == "NATURAL_DISASTER_SUSPECTED", "채점이 읽는 주원인 키는 세 신호 판정이다"
    assert row["truth_category"] == "EXTERNAL" and row["expect_rules"] == ["R-46"]


# --------------------------------------------------------------------------
# 10. 실운영 밀도 — 셀당 평소 호가 1건 미만 (2026-11-05 · F-111 결함)
# --------------------------------------------------------------------------
#
# F-111 에서 폭주가 "평소 × 6" 이 아니라 **하한(셀당 8호 × 25셀 = 200) × 6 = 1,200 으로 고정**됐다 — 지역
# 평소가 셀당 0.6건이라 하한 채움이 기준을 통째로 덮었다. 위 조밀한 하니스(셀당 8건 이상)는 그것을 못 본다.
# 여기서는 셀 1,800개(eNB 600) · 분당 1,500건으로 **셀당 약 0.8건**을 만든다. 정점 40배는 초기값이다.

_SPARSE = {}


def _sparse_master() -> list[Cell]:
    out = []
    for e in range(600):
        lat = 35.20 + (e // 30) * 0.012
        lon = 139.20 + (e % 30) * 0.012
        out += [_cell(f"{600000 + e}", n, lat + n * 0.0015, lon + n * 0.0015) for n in range(3)]
    return out


def _sparse(monkeypatch, target="LOCAL", kind="natural_disaster", minutes=START + 27):
    """희소 하니스 실행 — 같은 인자는 한 번만 돈다(느리다)."""
    key = (target, kind, minutes)
    if key not in _SPARSE:
        for f in ("pemdb1/_distributions.json", "profile.json"):
            if not (ROOT / "data" / f).exists():
                pytest.fail(f"data/{f} 가 없다")
        monkeypatch.setattr(P, "load_cells", lambda *a, **k: _sparse_master())
        monkeypatch.setattr(P, "load_apps", lambda conn: None)
        g = P.PemdbGen(None, ROOT / "data", rates={"lte_call": 1500, "transport": 1500}, seed=11,
                       subscriber_pool=2500)
        g.anchor = T0
        runs, sc = _run(g, minutes, [_row(kind=kind, target=target)])
        _SPARSE[key] = (g, runs, sc)
    return _SPARSE[key]


def _series(g, runs, sc):
    return [len(_area_calls(g, runs[m][0], sc)) for m in range(len(runs))]


def _mean(xs):
    xs = list(xs)
    return sum(xs) / len(xs)


def test_sparse_harness_really_is_below_the_floor(monkeypatch):
    g, runs, sc = _sparse(monkeypatch)
    per_cell = _mean(_series(g, runs, sc)[:START]) / len(sc.target.cell_keys)
    assert per_cell < 1.5, f"셀당 평소 호 {per_cell:.2f} — 실운영 밀도(1건 미만)가 아니다"


def test_peak_is_about_forty_times_normal_not_floor_times_peak(monkeypatch):
    g, runs, sc = _sparse(monkeypatch)
    s = _series(g, runs, sc)
    base = _mean(s[:START])
    peak = _mean(s[START + 1:START + 13])
    assert 30 <= peak / base <= 60, f"정점 지역 호가 평소의 {peak / base:.1f}배 (기대 약 40배)"
    # 옛 결함(하한 × 6 = 고정 1,200)은 이 범위에도 들 수 있다 — 그것을 가르는 것은 "다운만" 국면 테스트다


def test_ramp_minute_zero_is_between_normal_and_peak(monkeypatch):
    g, runs, sc = _sparse(monkeypatch)
    s = _series(g, runs, sc)
    base = _mean(s[:START])
    peak = _mean(s[START + 1:START + 13])
    assert base * 8 <= s[START] <= peak, f"0분째 {s[START]} (평소 {base:.0f} · 정점 {peak:.0f})"
    assert s[START] / base < 30, "0분째가 이미 정점 크기다 — 차오름이 없다"


def test_down_only_phase_is_back_to_normal_without_the_floor(monkeypatch):
    g, runs, sc = _sparse(monkeypatch)
    s = _series(g, runs, sc)
    base = _mean(s[:START])
    down_only = _mean(s[START + 13:START + 23])
    assert down_only <= 2.0 * base, f"13~22분 지역 호 {down_only:.0f} 가 평소 {base:.0f} 의 2배를 넘는다 — 하한이 남았다"
    assert down_only < 8 * len(sc.target.cell_keys) / 2, "하한(셀당 8호) 수준이 남아 있다"
    after = _mean(s[START + 24:])
    assert after <= 2.0 * base, f"정상화 뒤 {after:.0f} (평소 {base:.0f})"


def test_dead_enbs_stay_at_zero_in_the_down_only_phase(monkeypatch):
    g, runs, sc = _sparse(monkeypatch)
    dead = S.disaster_dead_enbs(sc, START + 15)
    assert dead
    i = _col(g, "lte_call", "First_eNB_ID")
    for m in range(START + 4, START + 23):
        assert not [r for r in runs[m][0] if r[i] in dead], f"{m}분: 정지 eNB 의 호가 있다"


def test_surge_composition_on_the_area_is_voice_heavy_and_mo_heavy(monkeypatch):
    """정점 분에는 지역 호의 대부분이 더한 호라 지역 전체로 세어도 음성 약 60% · 발신:착신 약 2.5:1 근처다."""
    g, runs, sc = _sparse(monkeypatch)
    i = _col(g, "lte_call", "call_type")
    rows = [r for m in range(START + 2, START + 13) for r in _area_calls(g, runs[m][0], sc)]
    c = collections.Counter(r[i] for r in rows)
    n = len(rows)
    assert n > 3000
    voice = (c["7"] + c["8"]) / n
    mo_mt = (c["3"] + c["7"]) / max(1, c["4"] + c["8"])
    assert 0.5 <= voice <= 0.7, f"음성 몫 {voice:.2f} (기대 약 0.6)"
    assert 2.0 <= mo_mt <= 3.1, f"발신:착신 {mo_mt:.2f} (기대 약 2.5)"


def test_national_total_stays_under_three_times_for_local_and_wide(monkeypatch):
    for target in ("LOCAL", "WIDE"):
        g, runs, sc = _sparse(monkeypatch, target=target)
        total = [len(runs[m][0]) for m in range(len(runs))]
        base = _mean(total[:START])
        assert max(total[START:START + 13]) <= 3.0 * base * 1.1, f"{target}: 전국 호가 평소의 3배를 넘었다"
        assert _mean(total[START + 14:START + 23]) <= 1.3 * base, f"{target}: 폭주가 끝난 뒤에도 전국이 높다"


def test_wide_area_has_the_same_shape_as_local(monkeypatch):
    """넓은 지역(80셀)은 `floor_max_cells`(60)를 넘어 하한이 안 걸린다 — 하한을 뺀 뒤에는 국소와 모양이 같아야 한다."""
    g, runs, sc = _sparse(monkeypatch, target="WIDE")
    assert len(sc.target.cell_keys) > 60
    s = _series(g, runs, sc)
    base = _mean(s[:START])
    assert 25 <= _mean(s[START + 1:START + 13]) / base <= 65
    assert _mean(s[START + 13:START + 23]) <= 2.0 * base


def test_natural_disaster_leaves_no_floor_tail(monkeypatch):
    g, _runs, _sc = _sparse(monkeypatch)
    assert not g._floor_last, f"자연재해가 하한 꼬리를 남겼다: {list(g._floor_last)[:3]}"


# 14번(ran_congestion)은 11-05 F-129 에서 하한을 끄도록 바뀌었다 — 아래 별도 테스트가 그 사실을 고정한다
@pytest.mark.parametrize("kind", ["earthquake"])
def test_other_kinds_keep_the_floor_on_the_same_harness(monkeypatch, kind):
    """같은 희소 하니스에서 옛 지진·혼잡은 하한이 그대로 걸린다(과거 기록 재현 · 다른 시나리오 불변)."""
    g, runs, sc = _sparse(monkeypatch, target=None, kind=kind, minutes=START + 8)
    assert g._floor_last, f"{kind}: 하한이 걸리지 않았다"
    s = _series(g, runs, sc)
    base = _mean(s[:START])
    assert _mean(s[START + 1:START + 6]) >= 3 * max(base, 1), f"{kind}: 하한 채움이 보이지 않는다 (평소 {base:.0f})"

def test_congestion_14_no_longer_gets_the_floor(monkeypatch):
    """14번은 하한을 채우지 않는다(11-05 F-129 · 맥스) — 부하는 `congestion_load` 배율이 곧 주입이다.
    같은 희소 하니스에서 하한이 대상 셀을 평소 3배 이상으로 채우면 안 된다."""
    g, runs, sc = _sparse(monkeypatch, target=None, kind="ran_congestion", minutes=START + 8)
    assert not g._floor_last, "14번에 하한 꼬리가 생겼다"
    s = _series(g, runs, sc)
    base = _mean(s[:START])
    peak = _mean(s[START + 1:START + 6])
    assert peak < 3 * max(base, 1), f"하한 채움이 보인다 (평소 {base:.0f} → {peak:.0f})"


def test_504_share_outside_dead_enbs_is_unchanged_by_the_surge(make):
    """정지 eNB 밖 지역 셀의 504 ÷ 호 비율이 장애 전과 같다 — 폭주가 평소 절단 비율을 바꾸지 않는다
    (F-111 에서 보인 분당 0~2건은 평소 절단이 호와 함께 늘어난 것이다). 조밀 하니스(표본이 많다)."""
    g = make(rate=4500)
    runs, sc = _disaster_run(g, minutes=START + 14)
    dead = S.disaster_dead_enbs(sc, START + 5)
    fe, rc = _col(g, "lte_call", "First_eNB_ID"), _col(g, "lte_call", "UEContextRelease_Cause")

    def share(minutes):
        rows = [r for m in minutes for r in _area_calls(g, runs[m][0], sc) if r[fe] not in dead]
        return sum(1 for r in rows if r[rc] == "504") / max(1, len(rows)), len(rows)

    before, nb = share(range(START))
    during, nd = share(range(START + 4, START + 13))
    assert nb >= 300 and nd >= 3000
    assert abs(during - before) <= 0.01 + 0.5 * before, f"504 비율 평소 {before:.4f} → 폭주 중 {during:.4f}"
