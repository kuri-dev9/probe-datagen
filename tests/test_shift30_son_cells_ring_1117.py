"""2026-11-17 저녁 세 가지 — ① 24번 30분(`SHIFT_RAMP` 5 · `SHIFT_RISE` 20 · `SHIFT_TOTAL` 30) ② 25번 정답 셀을 도착 셀로
③ 가입자 집 셀·걷는 사람·숨은 SA 상주 셀을 **셀 키 링**(`CellRing`)으로 정하기.

③ 은 gen 재시작 때 셀 목록이 바뀌면 가입자 배치가 통째로 다시 뽑혀 주입하지 않은 N-01(5G 단말 유입)이 약 2시간 섰던 결함의
수정이다(12:25·15:10). **eNB 번호는 전부 합성**이다(PUBLIC 저장소). DB 없음.
"""
from __future__ import annotations

import random
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from datagen import control, faults
from datagen import generator as G
from datagen import pemdb1 as P
from datagen import scenarios as S
from datagen.cells import Cell
from datagen.generator import CellRing, Generator
from datagen.profile import load as load_profiles
from datagen.ui import i18n_ja, map_api

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
RATES = {"lte_call": 300, "transport": 300, "dns": 100, "http": 100}


# ── 1 · 24번 30분 ───────────────────────────────────────────────────────

def _sc(minute: int, peak: float = 1.0):
    return SimpleNamespace(start_minute=0, _minute=minute, peak=peak)


def test_constants_are_five_twenty_thirty_and_the_rest_unchanged():
    assert (S.SHIFT_RAMP, S.SHIFT_RISE, S.SHIFT_TOTAL) == (5, 20, 30)
    assert (S.SHIFT_RECOVER, S.SHIFT_SPLIT) == (5, 0.6)
    assert S.SHIFT_RISE - S.SHIFT_RAMP >= 15                       # Q-11 3칸 연속 — 정점 뒤 15분은 그대로
    assert S.SHIFT_RISE + S.SHIFT_RECOVER < S.SHIFT_TOTAL          # 회복 뒤에도 B 가 켜진 구간이 남는다


def test_congestion_reaches_one_at_five_minutes_and_falls_to_zero_over_the_next_five():
    c = [S.shift_congestion(_sc(m)) for m in range(30)]
    assert c[0] == pytest.approx(0.2) and c[4] == pytest.approx(1.0)
    assert all(v == pytest.approx(1.0) for v in c[4:20])
    assert c[20] == pytest.approx(0.8) and c[23] == pytest.approx(0.2)
    assert all(v == 0.0 for v in c[24:])
    assert S.shift_congestion(_sc(10, peak=0.5)) == pytest.approx(0.5)


def test_split_is_zero_before_twenty_and_sixty_percent_from_twenty():
    assert [S.shift_split(_sc(m)) for m in (0, 10, 19)] == [0.0, 0.0, 0.0]
    assert S.shift_split(_sc(20)) == 0.6 and S.shift_split(_sc(29)) == 0.6


def test_visible_level_follows_the_five_minute_ramp():
    sc = SimpleNamespace(kind="capacity_shift", start_minute=0, peak=1.0, intensity=lambda m: 0.0)
    assert [round(S.visible_level(sc, m), 2) for m in range(6)] == [0.2, 0.4, 0.6, 0.8, 1.0, 1.0]


def test_recommended_duration_and_add_fault_length_are_thirty():
    rec = S.recommended_duration("capacity_shift")
    assert rec["recommended"] == 30 and rec["min"] == 30
    seen = {}

    class Cur:
        lastrowid = 1
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, params): seen["p"] = params

    class Conn:
        def cursor(self): return Cur()
        def commit(self): pass

    control.add_fault(Conn(), "capacity_shift", None, datetime(2026, 11, 17), duration_min=10)
    assert seen["p"][3] == 30


def test_build_holds_thirty_minutes():
    S.set_shift_cfg({"from_enb": "510001", "standby_enbs": ["910001"]})
    try:
        cells = [_cell("510001", n, 35.6, 139.7) for n in range(3)]
        row = {"fault_id": 1, "kind": "capacity_shift", "target": None,
               "start_ts": datetime(2026, 11, 17, 20, 0), "duration_min": 10, "intensity": 1.0}
        sc = faults.build(row, cells, row["start_ts"], random.Random(0))
        assert sc.duration == S.SHIFT_TOTAL == 30
    finally:
        S.set_shift_cfg(None)


def test_unscored_truth_phases_use_the_new_constants():
    S.set_shift_cfg({"from_enb": "510001", "standby_enbs": ["910001"]})
    try:
        t0 = datetime(2026, 11, 17, 20, 0)
        cells = [_cell("510001", n, 35.6, 139.7) for n in range(3)]
        sc = faults.build({"fault_id": 2, "kind": "capacity_shift", "target": None, "start_ts": t0,
                           "duration_min": 30, "intensity": 1.0}, cells, t0, random.Random(0))
        doc = P.write_unscored_truth(Path("/tmp/_t_1117_u.json"), t0, [sc], {})
    finally:
        S.set_shift_cfg(None)
    ph = doc["runs"][sc.id]["phases"]
    assert ph == {"start": "2026-11-17 20:00:00", "peak": "2026-11-17 20:05:00", "b_open": "2026-11-17 20:20:00",
                  "a_recovered": "2026-11-17 20:25:00", "end": "2026-11-17 20:30:00"}
    Path("/tmp/_t_1117_u.json").unlink(missing_ok=True)


def test_shift_desc_is_built_from_the_constants_and_is_the_table_row():
    assert control.SHIFT_DESC.startswith("30분 고정") and "20분간" in control.SHIFT_DESC and "5분 분석" in control.SHIFT_DESC
    assert control.SHIFT_DESC_JA.startswith("30分固定") and "20分間" in control.SHIFT_DESC_JA
    row = next(r for r in control.SCENARIOS if r[0] == 24)
    assert row[-1] == control.SHIFT_DESC


def test_ja_dictionary_translates_the_24_description():
    ja = i18n_ja._JA_COMMON
    assert ja[control.SHIFT_DESC] == control.SHIFT_DESC_JA
    assert " · 24번 진행 중, {m}분 시점에 켜진다" in i18n_ja._JA_MAP


def test_map_html_uses_the_b_open_minute_from_the_api():
    html = (ROOT / "src/datagen/ui/static/map.html").read_text(encoding="utf-8")
    assert "b_open_min" in html or "{m}" in html


def test_standby_api_reports_b_open_min_twenty(monkeypatch):
    import datagen.cli as cli
    monkeypatch.setattr(cli, "_capacity_shift", lambda pem: {"from_enb": "510001", "standby_enbs": ["910001"]})
    monkeypatch.setattr(map_api, "_pem_cfg", lambda: {})

    class Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, *a): pass
        def fetchall(self): return []

    class Conn:
        def cursor(self): return Cur()
        def close(self): pass

    monkeypatch.setattr(map_api, "_connect", lambda: Conn())
    assert map_api.api_map_standby()["b_open_min"] == 20


def test_shift_pick_keeps_same_b_two_hundred_minutes_apart_with_the_new_total():
    pool = ("9a", "9b", "9c", "9d")
    assert S.SHIFT_TOTAL == 30
    for s in range(0, 600, 7):
        b = S.shift_pick(s, pool)
        for d in (30, 60, 90):
            assert S.shift_pick(s + d, pool) != b
        assert S.shift_pick(s + 120, pool) == b                       # 4개면 30 × 4 = 120분


# ── 2 · 25번 정답 셀 ────────────────────────────────────────────────────

def _cell(enb, n, lat, lon, w=1.0):
    return Cell(cell_key=f"{enb}-{n}", enb_id=enb, cell_id=n, enb_seq=0, c_uid="", tac=100, plmn="44010",
                vlan_id="", latitude=lat, longitude=lon, adm_sido_cd="11", adm_sido_nm="",
                adm_sgg_cd="11200", adm_sgg_nm="", adm_emd_cd="11200011", adm_emd_nm="", geo_status="MAPPED",
                rat="EUTRAN", mme_id="0001", sgw_id="0037", pgw_id="0037", ims_pgw_id="0037",
                traffic_weight=w, display_name="")


def _son(pairs, gain, enbs=("510001",)):
    tgt = SimpleNamespace(son_enbs=list(enbs), cell_pairs=pairs, son_gain=gain, cell_keys=set(gain))
    return SimpleNamespace(kind="son_param_change", target=tgt)


def test_son_truth_cells_are_the_destination_cells_only():
    pairs = [("510001-0", "520001-0"), ("510001-1", "520001-0"), ("510001-2", "520001-1")]
    gain = {"510001-0": 0.3, "510001-1": 0.5, "510001-2": 0.6}
    t = S._son_truth(_son(pairs, gain))
    assert t["cells"] == ["520001-0", "520001-1"] and t["cell_cnt"] == 2
    assert t["src_cells"] == ["510001-0", "510001-1", "510001-2"]


def test_son_truth_keeps_son_cells_pairs_and_enbs():
    pairs = [("510001-0", "520001-0")]
    t = S._son_truth(_son(pairs, {"510001-0": 0.3}))
    assert t["son_enbs"] == ["510001"] and t["son_pairs"] == [["510001-0", "520001-0"]]
    assert t["son_cells"] == [{"cell": "510001-0", "role": "src", "gain": 0.3},
                              {"cell": "520001-0", "role": "dst", "gain": None}]


def test_son_truth_leaves_cells_alone_without_destination_cells():
    t = S._son_truth(_son([], {"510001-0": 0.3}))
    assert "cells" not in t and "cell_cnt" not in t and "src_cells" not in t
    assert t["son_cells"][0]["role"] == "src"


def test_son_truth_excludes_a_destination_that_is_also_a_source():
    pairs = [("510001-0", "510001-1")]
    t = S._son_truth(_son(pairs, {"510001-0": 0.3, "510001-1": 0.4}))
    assert "cells" not in t                                                 # 도착 셀이 하나도 안 남으면 바꾸지 않는다
    assert "510001-1" not in {c["cell"] for c in t["son_cells"] if c["role"] == "dst"}


def test_son_truth_is_empty_for_other_kinds():
    assert S._son_truth(SimpleNamespace(kind="enb_s1_outage", target=SimpleNamespace(son_enbs=[]))) == {}


def test_generation_target_cells_are_not_changed_by_the_truth_change():
    src = (ROOT / "src/datagen/scenarios.py").read_text(encoding="utf-8")
    assert "Target.cell_keys" in src and "생성 대상" in src


# ── 3 · CellRing ────────────────────────────────────────────────────────

def _cells(n=400):
    return [_cell(f"{600000 + i // 3}", i % 3, 35.0 + i * 1e-4, 139.0) for i in range(n)]


def _picks(cells, weights, salt="s", keys=3000):
    ring = CellRing(cells, weights, salt)
    return [cells[ring.pick(k)].cell_key for k in range(keys)]


def test_ring_is_deterministic():
    c = _cells()
    w = [1.0] * len(c)
    assert _picks(c, w) == _picks(c, w)
    assert _picks(c, w, "a") != _picks(c, w, "b")                           # 소금이 다르면 배치가 다르다


def test_ring_removing_a_cell_moves_only_its_keys():
    c = _cells()
    w = [1.0] * len(c)
    before = _picks(c, w)
    gone = c[57].cell_key
    c2 = [x for x in c if x.cell_key != gone]
    after = _picks(c2, [1.0] * len(c2))
    moved = [i for i, (a, b) in enumerate(zip(before, after)) if a != b]
    assert moved and all(before[i] == gone for i in moved)                  # 바뀐 키는 모두 빠진 셀의 몫이었다
    assert gone not in after


def test_ring_removing_a_whole_enb_moves_only_its_keys():
    c = _cells()
    w = [1.0] * len(c)
    before = _picks(c, w)
    c2 = [x for x in c if x.enb_id != "600010"]
    after = _picks(c2, [1.0] * len(c2))
    bad = [i for i, (a, b) in enumerate(zip(before, after)) if a != b and not a.startswith("600010-")]
    assert len(bad) <= 3                                                    # 평균 눈금 맞춤 뒤에도 반올림 경계 몇 개는 허용


def test_ring_adding_a_cell_takes_keys_only_for_itself():
    c = _cells()
    before = _picks(c[:-1], [1.0] * (len(c) - 1))
    after = _picks(c, [1.0] * len(c))
    new = c[-1].cell_key
    moved = [i for i, (a, b) in enumerate(zip(before, after)) if a != b]
    assert all(after[i] == new for i in moved) or len(moved) - sum(after[i] == new for i in moved) <= 3


def test_ring_never_picks_zero_weight_cells():
    c = _cells(100)
    w = [0.0 if i % 2 else 1.0 for i in range(100)]
    got = set(_picks(c, w))
    assert got and all(int(k.split("-")[0]) * 3 + int(k.split("-")[1]) - 600000 * 3 >= 0 for k in got)
    zero = {c[i].cell_key for i in range(100) if i % 2}
    assert got.isdisjoint(zero)


def test_ring_heavier_cells_get_more_keys():
    c = _cells(60)
    w = [8.0 if i < 5 else 1.0 for i in range(60)]
    n = collections_count(_picks(c, w, keys=6000))
    heavy = sum(n.get(c[i].cell_key, 0) for i in range(5)) / 5
    light = sum(n.get(c[i].cell_key, 0) for i in range(5, 60)) / 55
    assert heavy > light * 3


def collections_count(xs):
    out = {}
    for x in xs:
        out[x] = out.get(x, 0) + 1
    return out


def test_ring_all_zero_weights_fall_back_to_one_vnode_per_cell():
    c = _cells(10)
    ring = CellRing(c, [0.0] * 10, "z")
    assert 0 <= ring.pick(1) < 10


def test_ring_uses_a_stable_hash_not_python_hash():
    assert G._key_int("a", 1) == G._key_int("a", 1)
    assert G._key_int("a", 1) != G._key_int("a", 2)
    assert 0 <= G._key_int("x") < 2 ** 64
    assert G._keyed_rng("k", 3).random() == G._keyed_rng("k", 3).random()


# ── 4 · Generator — 셀 하나가 빠져도 나머지 가입자는 그대로 ──────────────

def _real_cells():
    from datagen.cells import load_cells
    return load_cells(DATA / "cells.json")


def _gen(cells, seed=20260903, pool=3000):
    profiles = load_profiles(DATA / "profile.json")
    keep = {s: profiles[s] for s in ("lte_call", "transport", "dns", "http")}
    return Generator(cells=cells, profiles=keep, scenarios=[], rates=RATES, seed=seed, diurnal=True,
                     subscriber_pool=pool, success_only=True, end_only=True)


@pytest.fixture(scope="module")
def two_gens():
    if not (DATA / "cells.json").exists() or not (DATA / "profile.json").exists():
        pytest.skip("data/cells.json·profile.json 없음")
    cells = _real_cells()
    drop = cells[len(cells) // 2].enb_id
    smaller = [c for c in cells if c.enb_id != drop]
    return _gen(cells), _gen(smaller), drop, cells, smaller


def test_generators_with_the_same_cells_and_seed_agree(two_gens):
    a, _, _, cells, _ = two_gens
    b = _gen(cells)
    assert [(s.home_cell, s.age, s.exchange) for s in a.subs] == [(s.home_cell, s.age, s.exchange) for s in b.subs]


def test_removing_one_enb_moves_only_the_subscribers_who_lived_there(two_gens):
    a, b, drop, _, _ = two_gens
    moved = other = 0
    for sa, sb in zip(a.subs, b.subs):
        ka, kb = a.cells[sa.home_cell].cell_key, b.cells[sb.home_cell].cell_key
        if ka != kb:
            moved += 1
            other += not ka.startswith(f"{drop}-")
    assert other <= max(3, len(a.subs) // 500)                              # 바뀐 사람은 거의 모두 빠진 eNB 사람
    assert moved < len(a.subs) * 0.05                                       # 옛 코드는 거의 전원이 바뀌었다


def test_other_subscriber_fields_do_not_depend_on_the_cell_list(two_gens):
    a, b, *_ = two_gens
    assert [s.age for s in a.subs] == [s.age for s in b.subs]
    assert [s.exchange for s in a.subs] == [s.exchange for s in b.subs]
    assert [s.imsi for s in a.subs] == [s.imsi for s in b.subs]


def test_walkers_start_cells_move_only_for_the_removed_enb(two_gens):
    a, b, drop, *_ = two_gens
    a.seat_walkers(0.5, random.Random(7))
    b.seat_walkers(0.5, random.Random(7))
    assert a._walkers == b._walkers                                         # 누가 걷는지는 셀 목록과 무관
    other = 0
    for i in a._walkers:
        ka, kb = a.subs[i].walk_cell.cell_key, b.subs[i].walk_cell.cell_key
        if ka != kb and not ka.startswith(f"{drop}-"):
            other += 1
    assert other <= 3
    assert [a.subs[i].walk_dwell for i in a._walkers] == [b.subs[i].walk_dwell for i in b._walkers]


def test_seat_walkers_is_deterministic(two_gens):
    a, _, _, cells, _ = two_gens
    c = _gen(cells)
    a.seat_walkers(0.5, random.Random(7))
    c.seat_walkers(0.5, random.Random(7))
    assert [(i, a.subs[i].walk_cell.cell_key) for i in a._walkers] == [(i, c.subs[i].walk_cell.cell_key) for i in c._walkers]


def test_seat_routes_accepts_the_old_call_and_is_deterministic():
    if not (DATA / "cells.json").exists():
        pytest.skip("data 없음")
    cells = _real_cells()
    g1, g2 = _gen(cells, pool=500), _gen(cells, pool=500)
    for g in (g1, g2):
        g.seat_routes([], 0.5, random.Random(3))                            # 예전 호출 모양(공용 rng 를 넘김)
    assert [s.route for s in g1.subs] == [s.route for s in g2.subs]


# ── 5 · 숨은 SA 상주 셀 ─────────────────────────────────────────────────

@pytest.fixture
def pgen(monkeypatch):
    if not (DATA / "pemdb1/_distributions.json").exists() or not (DATA / "profile.json").exists():
        pytest.skip("data 없음")
    master = [_cell(f"{700000 + i // 3}", i % 3, 35.0 + i * 1e-4, 139.0, 1.0 + (i % 5)) for i in range(300)]
    monkeypatch.setattr(P, "load_cells", lambda *a, **k: list(master))
    monkeypatch.setattr(P, "load_apps", lambda conn: None)
    return P.PemdbGen(None, DATA, rates=RATES, seed=1, subscriber_pool=1500)


def test_sa_hidden_resident_cells_are_deterministic(pgen):
    a = pgen._make_sa_hidden(0.2, 11)
    b = pgen._make_sa_hidden(0.2, 11)
    assert {k: len(v) for k, v in a.items()} == {k: len(v) for k, v in b.items()}
    assert sum(len(v) for v in a.values()) == int(0.2 * len(pgen.gen.subs))


def test_sa_hidden_changes_with_the_seed_and_ignores_the_shared_rng(pgen):
    a = pgen._make_sa_hidden(0.2, 11)
    state = pgen.gen.rng.getstate()
    c = pgen._make_sa_hidden(0.2, 12)
    assert pgen.gen.rng.getstate() == state                                 # 본 생성의 난수 흐름을 흔들지 않는다
    assert {k: len(v) for k, v in a.items()} != {k: len(v) for k, v in c.items()}


def test_sa_hidden_residents_have_sa_capability_and_unique_ids(pgen):
    out = pgen._make_sa_hidden(0.2, 11)
    subs = [s for v in out.values() for s in v]
    assert subs and all(s.dcnr == "1" and s.route == -1 for s in subs)
    assert len({s.imsi for s in subs}) == len(subs)
    known = {c.cell_key for c in pgen.gen.cells}
    assert set(out) <= known


def test_sa_hidden_only_the_removed_enbs_residents_move(monkeypatch, pgen):
    import datagen.generator as GG
    cells = list(pgen.gen.cells)
    w = list(pgen.gen.cell_weights)
    drop = cells[150].enb_id
    keep = [(c, x) for c, x in zip(cells, w) if c.enb_id != drop]
    r1 = CellRing(cells, w, "sa_hidden:11")
    r2 = CellRing([c for c, _ in keep], [x for _, x in keep], "sa_hidden:11")
    other = 0
    for i in range(1500, 1800):
        a, b = cells[r1.pick(i)].cell_key, [c for c, _ in keep][r2.pick(i)].cell_key
        if a != b and not a.startswith(f"{drop}-"):
            other += 1
    assert other <= 3
    assert GG.CellRing is CellRing
