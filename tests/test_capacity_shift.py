"""24번 — 기지국 증설 후 부하 이동(`capacity_shift`). 깨면 시연이 무호 장애가 된다.

소등 eNB(B)는 마스터에 있지만 평소 호를 만들지 않고, 24번이 켰을 때만
A 의 호를 받는다. 이 파일이 지키는 것은 —

1. **B 가 평소 어디로도 새지 않는다** — 추첨·핸드오버 이웃·다른 장애 대상.
   새면 B 에 평소 호가 쌓이고, 끄는 순간 graphio 무호(D4)로 탐지된다.
2. **같은 B 를 60분 안에 다시 켜지 않는다** — D4 는 꺼지는 순간 직전 60분
   호 수 중앙값을 본다(`scenarios.shift_pick` 머리말).
3. **국면** — 0–10분 A 상승 · 10분 B 개통 후 A 40 / B 60 · 30분 B 소등.
4. **길이가 DB 에 30분으로 들어간다** — `poll_transitions` 가 행의
   `DURATION_MIN` 으로 장애를 끝낸다.

**DB 를 쓰지 않는다**(`test_session_model.py` 머리말). `PemdbGen` 은
`load_cells`·`load_apps` 만 바꿔 띄운다 — 분포·프로파일은 `data/` 의 실측
파일을 그대로 읽는다(`test_fault_scenarios.py` 와 같은 전제).

**eNB 번호는 합성이다** — 실제 값은 `data/capacity_shift.json`(gitignore)에
있고, 이 저장소는 PUBLIC 이다.
"""

from __future__ import annotations

import collections
import random
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from datagen import control, faults
from datagen import pemdb1 as P
from datagen import scenarios as S
from datagen.cells import Cell

ROOT = Path(__file__).resolve().parents[1]
A = "500000"
POOL = ("900001", "900002", "900003", "900004")


def _cell(enb: str, n: int, lat: float, lon: float, w: float = 1.0) -> Cell:
    return Cell(
        cell_key=f"{enb}-{n}", enb_id=enb, cell_id=n, enb_seq=0, c_uid="",
        tac=100, plmn="44010", vlan_id="", latitude=lat, longitude=lon,
        adm_sido_cd="11", adm_sido_nm="", adm_sgg_cd="11200", adm_sgg_nm="",
        adm_emd_cd="11200011", adm_emd_nm="", geo_status="MAPPED",
        rat="EUTRAN", mme_id="0001", sgw_id="0037", pgw_id="0037",
        ims_pgw_id="0037", traffic_weight=w, display_name="")


def _master() -> list[Cell]:
    """일반 eNB 40개(A 포함) + 소등 후보 4개. **B 를 A 바로 옆에 둔다** —
    가장 가까운 이웃이라 핸드오버 이웃에서 빠졌는지가 실제로 시험된다."""
    out = [_cell(f"{500000 + e}", n, 35.69 + e * 0.002, 139.70 + e * 0.002,
                 20.0 if e == 0 else 1.0)
           for e in range(40) for n in range(3)]
    out += [_cell(b, n, 35.6901, 139.7001) for b in POOL for n in range(3)]
    return out


@pytest.fixture
def gen(monkeypatch):
    for f in ("pemdb1/_distributions.json", "profile.json"):
        if not (ROOT / "data" / f).exists():
            pytest.fail(f"data/{f} 가 없다 — 실측 분포 없이는 생성기를 띄울 수 없다")
    monkeypatch.setattr(P, "load_cells", lambda *a, **k: _master())
    monkeypatch.setattr(P, "load_apps", lambda conn: None)
    g = P.PemdbGen(None, ROOT / "data", rates={"lte_call": 2000, "transport": 1500},
                   seed=1, subscriber_pool=2000,
                   capacity_shift={"from_enb": A, "standby_enbs": list(POOL)})
    yield g
    S.set_shift_cfg(None)


def _run(g, t0: datetime, start: int, minutes: int):
    """분마다 (A 호, B 호, B 세션) 을 센다."""
    g.anchor = t0
    g.set_fault_rows([{"fault_id": 1, "kind": "capacity_shift", "target": None,
                       "start_ts": t0 + timedelta(minutes=start),
                       "duration_min": S.SHIFT_TOTAL, "intensity": 1.0}])
    i_c = g.gen.index["lte_call"]["First_eNB_ID"]
    i_u = g.gen.index["transport"]["eNB_ID"]
    out = []
    for m in range(minutes):
        ts = t0 + timedelta(minutes=m)
        calls = collections.Counter(r[i_c] for r in g.records(m, ts))
        sess = collections.Counter(r[i_u] for r in
                                   g.uplane_records(m, ts).get("transport", []))
        out.append((calls[A], sum(calls[b] for b in POOL),
                    sum(sess[b] for b in POOL)))
    return out


# --------------------------------------------------------------------------
# 1. 소등 eNB 는 어디로도 새지 않는다
# --------------------------------------------------------------------------


def test_standby_enbs_are_not_in_the_generation_set(gen):
    assert {c.enb_id for c in gen.cells}.isdisjoint(POOL)
    assert sorted(gen.standby) == sorted(POOL)
    assert all(len(v) == 3 for v in gen.standby.values())


def test_standby_enbs_are_not_handover_neighbours(gen):
    """B 는 A 의 가장 가까운 이웃이다 — 토폴로지에 남으면 전환으로 호가 샌다."""
    topo = gen.ho_topo
    for c in gen.cells:
        for nb in topo.inter.get(c.cell_key, ()) + topo.intra.get(c.cell_key, ()):
            assert nb.enb_id not in POOL


def test_no_call_reaches_a_standby_enb_without_the_scenario(gen):
    gen.anchor = datetime(2026, 10, 29, 15, 0)
    i_c = gen.gen.index["lte_call"]["First_eNB_ID"]
    for m in range(5):
        recs = gen.records(m, gen.anchor + timedelta(minutes=m))
        gen.uplane_records(m, gen.anchor + timedelta(minutes=m))
        assert not any(r[i_c] in POOL for r in recs)


def test_other_faults_cannot_target_a_standby_enb(gen):
    """다른 장애의 대상 해석도 `self.cells` 를 본다 — 그 목록에 없으면 못 고른다."""
    row = {"fault_id": 7, "kind": "enb_s1_outage", "target": f"ENB:{POOL[0]}",
           "start_ts": datetime(2026, 10, 29, 15, 0), "duration_min": 10,
           "intensity": 1.0}
    assert faults.build(row, gen.cells, row["start_ts"], random.Random(0)) is None


# --------------------------------------------------------------------------
# 2. 같은 B 를 60분 안에 다시 켜지 않는다
# --------------------------------------------------------------------------


def test_same_standby_is_reused_only_after_the_d4_window():
    """하루의 모든 시작 분 쌍에서 — 같은 B 면 시작이 60분 이상 떨어져 있다.

    60 은 graphio `detect.silence.lookback_minutes` 다. 앞 회차 B 가 켜져
    있던 구간(`s1+10 … s1+30`)이 뒤 회차 B 소등 시각(`s2+30`)의 직전 60분
    밖이려면 `s2 - s1 >= 60` 이면 된다.
    """
    day = range(0, 24 * 60, 1)
    last: dict[str, int] = {}
    for s in day:
        b = S.shift_pick(s, POOL)
        if b in last and s - last[b] < S.SHIFT_TOTAL:
            continue                    # 같은 칸 — 겹치는 두 회차는 성립하지 않는다
        if b in last:
            assert s - last[b] >= 60, (b, last[b], s)
        last[b] = s


def test_consecutive_runs_use_different_standby():
    """한 시간에 두 번 — 30분 간격이면 다른 B 다."""
    for s in range(0, 600, 7):
        assert S.shift_pick(s, POOL) != S.shift_pick(s + S.SHIFT_TOTAL, POOL)


# --------------------------------------------------------------------------
# 3. 국면
# --------------------------------------------------------------------------


def test_phases_rise_split_and_switch_off(gen):
    t0 = datetime(2026, 10, 29, 15, 0)
    got = _run(gen, t0, start=2, minutes=36)
    # 시작 전·B 개통 전에는 B 에 호가 없다
    assert all(b == 0 for _, b, _ in got[:12])
    # 0–10분 A 가 오른다
    assert got[11][0] > got[1][0] * 1.2
    # 개통 뒤 A 40 · B 60 (±8%p)
    a = sum(x for x, _, _ in got[13:31])
    b = sum(y for _, y, _ in got[13:31])
    assert 0.52 < b / (a + b) < 0.68
    # 30분 창이 끝나면 B 호는 즉시 0, 세션은 한 분 안에 떠난다
    assert all(y == 0 for _, y, _ in got[32:])
    assert all(s == 0 for _, _, s in got[33:])


def test_every_subscriber_can_be_found_by_imsi(gen):
    """`_reseat_subscribers` 가 IMSI 를 새로 쓴 뒤 역색인을 다시 만든다.

    전에는 2,000명 중 285명만 찾혀 "같은 사람을 B 로"가 14% 만 일어났다 —
    분산이 60% 가 아니라 9% 로 나왔고 에러는 없었다.
    """
    g = gen.gen
    assert all(g.sub_by_imsi.get(s.imsi) == i for i, s in enumerate(g.subs))


def test_congestion_rises_then_recovers():
    sc = S.place("F-1", dict(S.KIND_SPECS["capacity_shift"], kind="capacity_shift",
                             target=S.Target(), peak=1.0), 0, S.SHIFT_TOTAL,
                 random.Random(0))
    lv = []
    for m in range(S.SHIFT_TOTAL):
        sc._minute = m
        lv.append(S.shift_congestion(sc))
    assert lv[0] < lv[S.SHIFT_RISE - 1] == 1.0
    assert lv[S.SHIFT_RISE] < 1.0                       # 개통 직후 내려가기 시작
    assert lv[S.SHIFT_RISE + S.SHIFT_RECOVER - 1] == 0.0
    assert all(x == 0.0 for x in lv[S.SHIFT_RISE + S.SHIFT_RECOVER:])
    # 창 전체에서 covers — 회복 국면에 0 이 되면 B 가 일찍 꺼진다
    assert all(sc.covers(m) for m in range(S.SHIFT_TOTAL))


# --------------------------------------------------------------------------
# 4. 주입 경로 — 대상·길이·채점
# --------------------------------------------------------------------------


def _row(target=None, fid=3):
    return {"fault_id": fid, "kind": "capacity_shift", "target": target,
            "start_ts": datetime(2026, 10, 29, 15, 0), "duration_min": 10,
            "intensity": 1.0}


def test_build_targets_the_configured_a_and_holds_thirty_minutes():
    S.set_shift_cfg({"from_enb": A, "standby_enbs": list(POOL)})
    try:
        cells = [c for c in _master() if c.enb_id not in POOL]
        rng = random.Random(0)
        sc = faults.build(_row(), cells, datetime(2026, 10, 29, 15, 0), rng)
        assert sc.target.enb_id == A and sc.target.shift_to in POOL
        assert sc.target.cell_keys == {f"{A}-{n}" for n in range(3)}
        assert sc.duration == S.SHIFT_TOTAL
        # A 를 지목하면 받고, 다른 eNB 를 지목하면 거절한다
        assert faults.build(_row(f"ENB:{A}"), cells, _row()["start_ts"], rng)
        assert faults.build(_row("ENB:500003"), cells, _row()["start_ts"], rng) is None
    finally:
        S.set_shift_cfg(None)


def test_build_is_cancelled_without_config():
    S.set_shift_cfg(None)
    assert faults.build(_row(), _master(), _row()["start_ts"],
                        random.Random(0)) is None


def test_add_fault_stores_thirty_minutes():
    """`poll_transitions` 는 행의 `DURATION_MIN` 으로 끝낸다 — 10분이 들어가면
    B 가 켜지는 순간 장애가 내려간다."""
    seen = {}

    class Cur:
        lastrowid = 1
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, params): seen["p"] = params

    class Conn:
        def cursor(self): return Cur()
        def commit(self): pass

    control.add_fault(Conn(), "capacity_shift", None, datetime(2026, 10, 29),
                      duration_min=10)
    assert seen["p"][3] == S.SHIFT_TOTAL


def test_truth_file_leaves_it_out(tmp_path):
    """정답 원인이 `—` 다 — 채점 문항이 되면 A 혼잡을 맞혀도 오답으로 센다."""
    S.set_shift_cfg({"from_enb": A, "standby_enbs": list(POOL)})
    try:
        sc = faults.build(_row(), _master(), _row()["start_ts"],
                          random.Random(0))
        doc = P.write_truth(tmp_path / "t.json", _row()["start_ts"], 30, [sc])
        assert doc["scenarios"] == []
    finally:
        S.set_shift_cfg(None)


def test_shift_config_rejects_typos_and_a_in_the_pool():
    with pytest.raises(RuntimeError):
        S.ShiftCfg.from_cfg({"from_enbs": A})
    with pytest.raises(RuntimeError):
        S.ShiftCfg.from_cfg({"from_enb": A, "standby_enbs": [A]})
    # `_` 로 시작하는 키는 사람이 남긴 메모다
    assert S.ShiftCfg.from_cfg({"_note": "x", "from_enb": A}).from_enb == A


def test_scenario_24_is_listed_with_no_equipment():
    """대상은 설정이 고정한다 — 지도에서 eNB 를 찍어 거는 것처럼 보이면 안 된다."""
    row = next(r for r in control.SCENARIOS if r[0] == 24)
    assert row[2] == "capacity_shift" and row[4] == ()


def test_live_truth_does_not_overwrite_when_only_unscored_is_active(tmp_path):
    """24번만 걸려 있을 때 정답 파일을 빈 목록으로 덮지 않는다 — 직전 장애의
    정답이 사라진다(2026-10-29 실제로 한 번 지워졌다)."""
    from datagen import cli
    S.set_shift_cfg({"from_enb": A, "standby_enbs": list(POOL)})
    try:
        sc = faults.build(_row(), _master(), _row()["start_ts"], random.Random(0))
        path = tmp_path / "t.json"
        path.write_text("이전 정답", encoding="utf-8")

        class G:
            truth_path = path
            anchor = _row()["start_ts"]

        cli._live_truth(P, G(), [sc])
        assert path.read_text(encoding="utf-8") == "이전 정답"
    finally:
        S.set_shift_cfg(None)


# --------------------------------------------------------------------------
# 장애 대상 셀의 판정 하한 (2026-10-29) — 같은 생성기 fixture 를 쓴다
# --------------------------------------------------------------------------


def test_fault_target_cells_reach_the_floor(gen):
    """사람이 적은 셀에 장애를 넣어도 **판정할 만큼 호가 생긴다.**

    전에는 14번 대상 8셀 합계가 분당 1~6건이라 열화를 걸어도 이벤트가
    0건이었다. 하한은 셀당 `floor_per_cell` 이고 장애 모양을 따라 오른다.
    """
    t0 = datetime(2026, 10, 29, 16, 0)
    gen.anchor = t0
    enb = "500007"
    # 합성 마스터는 eNB 가 40개뿐이라 대상이 원래 분당 ~35건을 받는다 —
    # 하한을 그보다 높여야 "모자란 만큼 채운다"가 실제로 시험된다
    gen.fault.floor_per_cell = 40.0
    gen.set_fault_rows([{"fault_id": 5, "kind": "ran_radio", "target": f"ENB:{enb}",
                         "start_ts": t0, "duration_min": 10, "intensity": 0.3}])
    i_c = gen.gen.index["lte_call"]["First_eNB_ID"]
    got = []
    for m in range(12):
        ts = t0 + timedelta(minutes=m)
        got.append(sum(1 for r in gen.records(m, ts) if r[i_c] == enb))
        gen.uplane_records(m, ts)
    floor = gen.fault.floor_per_cell * 3            # 셀 3개
    # 강도 0.3 이어도 하한은 다 채운다 — 약한 장애도 표본은 있어야 한다
    assert min(got[3:8]) >= floor * 0.9, got
    # 끝나면 평소로 돌아간다
    assert got[-1] < floor / 2, got


def test_floor_skips_node_wide_targets(gen):
    """장비 전체 대상(셀이 많다)에는 하한을 걸지 않는다 — 수천 건이 는다."""
    t0 = datetime(2026, 10, 29, 16, 0)
    gen.anchor = t0
    gen.set_fault_rows([{"fault_id": 6, "kind": "gw_resource", "target": "SGW-0037",
                         "start_ts": t0, "duration_min": 10, "intensity": 1.0}])
    sc = gen.gen.scenarios[0]
    assert len(sc.target.cell_keys) > gen.fault.floor_max_cells
    n = len(gen.records(5, t0 + timedelta(minutes=5)))
    assert n < 2400, n          # 하한이 걸렸다면 120셀 × 8 = 960건이 더 붙는다
