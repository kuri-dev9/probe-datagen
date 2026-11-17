"""24번 여러 자리(A·B 쌍) — 2026-11-17 (사용자 「먼 역에 동시 주입」).

`ShiftSite`·`ShiftCfg.sites`·`site_for`·`home_of`·`all_standby` · `faults._shift_target` · `PemdbGen.standby`·
`_relocator`·`_shifter` · `/api/map/standby` · `write_unscored_truth`.

**eNB 번호는 전부 합성이다** — 실제 값은 `data/capacity_shift.json`(gitignore)에 있고 이 저장소는 PUBLIC 이다.
DB 를 쓰지 않는다(`test_capacity_shift.py` 와 같은 더블).
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from datagen import faults
from datagen import pemdb1 as P
from datagen import scenarios as S
from datagen.cells import Cell
from datagen.ui import map_api

ROOT = Path(__file__).resolve().parents[1]
T0 = datetime(2026, 11, 17, 16, 0)

# 합성 자리 셋 — 서로 다른 좌표(먼 곳)
A1, B1 = "510001", ("910001", "910002")
A2, B2 = "510002", ("910003",)
A3, B3 = "510003", ("910004",)
SITES = [
    {"name": "北駅", "from_enb": A1, "standby_enbs": list(B1)},
    {"name": "南駅", "from_enb": A2, "standby_enbs": list(B2)},
    {"name": "東駅", "from_enb": A3, "standby_enbs": list(B3)},
]
CFG = {"sites": SITES}
ALL_B = B1 + B2 + B3


@pytest.fixture(autouse=True)
def _reset():
    yield
    S.set_shift_cfg(None)


def _cell(enb, n, lat, lon, w=1.0):
    return Cell(cell_key=f"{enb}-{n}", enb_id=enb, cell_id=n, enb_seq=0, c_uid="", tac=100, plmn="44010",
                vlan_id="", latitude=lat, longitude=lon, adm_sido_cd="11", adm_sido_nm="",
                adm_sgg_cd="11200", adm_sgg_nm="", adm_emd_cd="11200011", adm_emd_nm="", geo_status="MAPPED",
                rat="EUTRAN", mme_id="0001", sgw_id="0037", pgw_id="0037", ims_pgw_id="0037",
                traffic_weight=w, display_name="")


def _master():
    pos = {A1: (35.80, 139.70), A2: (35.60, 139.76), A3: (35.70, 139.90)}
    out = []
    for e in range(30):
        out += [_cell(f"{520000 + e}", n, 35.69 + e * 0.002, 139.70 + e * 0.002) for n in range(3)]
    for a, (la, lo) in pos.items():
        out += [_cell(a, n, la, lo, 20.0) for n in range(3)]
    for sites in (B1, B2, B3):
        for b in sites:
            la, lo = pos[{B1: A1, B2: A2, B3: A3}[sites]]
            out += [_cell(b, n, la + 0.0003, lo + 0.0003) for n in range(3)]
    return out


def _row(target=None, fid=3, start=T0):
    return {"fault_id": fid, "kind": "capacity_shift", "target": target, "start_ts": start,
            "duration_min": 10, "intensity": 1.0}


def _non_b(cells):
    return [c for c in cells if c.enb_id not in ALL_B]


# ── 1 · 설정 형식 ───────────────────────────────────────────────────────

def test_multi_site_format_is_read_in_order():
    cfg = S.ShiftCfg.from_cfg(CFG)
    assert [s.name for s in cfg.sites] == ["北駅", "南駅", "東駅"]
    assert cfg.all_standby == ALL_B
    assert cfg.sites[1].from_enb == A2 and cfg.sites[1].standby_enbs == B2


def test_legacy_single_format_is_one_unnamed_site():
    cfg = S.ShiftCfg.from_cfg({"from_enb": A1, "standby_enbs": list(B1)})
    assert len(cfg.sites) == 1 and cfg.sites[0].name == ""
    assert cfg.from_enb == A1 and cfg.standby_enbs == B1 and cfg.all_standby == B1


def test_legacy_properties_are_the_first_site():
    cfg = S.ShiftCfg.from_cfg(CFG)
    assert cfg.from_enb == A1 and cfg.standby_enbs == B1


def test_empty_config_has_no_site_and_no_standby():
    for raw in (None, {}):
        cfg = S.ShiftCfg.from_cfg(raw)
        assert cfg.sites == () and cfg.from_enb == "" and cfg.standby_enbs == () and cfg.all_standby == ()
        assert cfg.site_for("") is None


def test_underscore_keys_are_notes():
    cfg = S.ShiftCfg.from_cfg({"_note": "메모", "sites": [{"_x": 1, **SITES[0]}]})
    assert cfg.from_enb == A1


def _bad(cfg):
    with pytest.raises(RuntimeError):
        S.ShiftCfg.from_cfg(cfg)


def test_rejects_b_shared_by_two_sites():
    _bad({"sites": [SITES[0], {"name": "南駅", "from_enb": A2, "standby_enbs": [B1[0]]}]})


def test_rejects_a_that_is_another_sites_b():
    _bad({"sites": [SITES[0], {"name": "南駅", "from_enb": A2, "standby_enbs": [A1]}]})


def test_rejects_a_that_is_its_own_b():
    _bad({"sites": [{"name": "x", "from_enb": A1, "standby_enbs": [A1]}]})


def test_rejects_duplicate_site_names():
    _bad({"sites": [SITES[0], {"name": "北駅", "from_enb": A2, "standby_enbs": list(B2)}]})


def test_rejects_duplicate_a():
    _bad({"sites": [SITES[0], {"name": "南駅", "from_enb": A1, "standby_enbs": list(B2)}]})


def test_rejects_mixing_sites_with_single_format():
    _bad({"sites": SITES, "from_enb": A1})
    _bad({"sites": SITES, "standby_enbs": list(B1)})


def test_rejects_unknown_keys_at_top_and_in_a_site():
    _bad({"from_enbs": A1})
    _bad({"sites": [{**SITES[0], "extra": 1}]})


def test_rejects_site_without_a():
    _bad({"sites": [{"name": "x", "standby_enbs": ["1"]}]})


# ── 2 · site_for · home_of ──────────────────────────────────────────────

def test_site_for_blank_means_first_site():
    cfg = S.ShiftCfg.from_cfg(CFG)
    for raw in (None, "", "  "):
        assert cfg.site_for(raw).from_enb == A1


def test_site_for_by_name_and_by_a():
    cfg = S.ShiftCfg.from_cfg(CFG)
    assert cfg.site_for("SITE:南駅").from_enb == A2
    assert cfg.site_for("site:東駅").from_enb == A3
    assert cfg.site_for(f"ENB:{A2}").name == "南駅"
    assert cfg.site_for(f"enb:{A3}").name == "東駅"


def test_site_for_rejects_b_unknown_and_malformed():
    cfg = S.ShiftCfg.from_cfg(CFG)
    for raw in (f"ENB:{B1[0]}", "SITE:없는역", "ENB:999999", B1[0], A1, "AREA:x", "LIKE:3"):
        assert cfg.site_for(raw) is None, raw


def test_home_of_returns_that_sites_a():
    cfg = S.ShiftCfg.from_cfg(CFG)
    assert [cfg.home_of(b) for b in ALL_B] == [A1, A1, A2, A3]
    assert cfg.home_of("999999") is None and cfg.home_of(A1) is None


# ── 3 · _shift_target ───────────────────────────────────────────────────

def test_shift_target_uses_that_sites_a_cells_and_b_only():
    S.set_shift_cfg(CFG)
    cells = _non_b(_master())
    for name, a, pool in (("北駅", A1, B1), ("南駅", A2, B2), ("東駅", A3, B3)):
        t = faults._shift_target(f"SITE:{name}", T0, cells)
        assert t.enb_id == a and t.shift_to in pool
        assert t.cell_keys == {f"{a}-{n}" for n in range(3)}


def test_shift_target_blank_is_the_first_site_and_enb_a_selects():
    S.set_shift_cfg(CFG)
    cells = _non_b(_master())
    assert faults._shift_target(None, T0, cells).enb_id == A1
    assert faults._shift_target(f"ENB:{A3}", T0, cells).enb_id == A3


def test_shift_target_cancelled_for_b_unknown_or_missing_cells():
    S.set_shift_cfg(CFG)
    cells = _non_b(_master())
    assert faults._shift_target(f"ENB:{B1[0]}", T0, cells) is None
    assert faults._shift_target("SITE:없는역", T0, cells) is None
    assert faults._shift_target(f"SITE:北駅", T0, [c for c in cells if c.enb_id != A1]) is None   # A 셀이 범위 밖
    S.set_shift_cfg(None)
    assert faults._shift_target(None, T0, cells) is None


def test_shift_target_rotates_b_only_inside_the_site():
    S.set_shift_cfg(CFG)
    cells = _non_b(_master())
    seen = {faults._shift_target("SITE:北駅", T0 + timedelta(minutes=S.SHIFT_TOTAL * k), cells).shift_to
            for k in range(6)}
    assert seen == set(B1)                                              # 둘 다 쓰고 다른 자리 B 는 안 쓴다
    assert {faults._shift_target("SITE:南駅", T0 + timedelta(minutes=S.SHIFT_TOTAL * k), cells).shift_to
            for k in range(6)} == set(B2)


def test_build_through_faults_build_for_each_site():
    S.set_shift_cfg(CFG)
    cells = _non_b(_master())
    for name, a, pool in (("北駅", A1, B1), ("南駅", A2, B2), ("東駅", A3, B3)):
        sc = faults.build(_row(f"SITE:{name}"), cells, T0, random.Random(0))
        assert sc.target.enb_id == a and sc.target.shift_to in pool and sc.duration == S.SHIFT_TOTAL
    assert faults.build(_row(f"ENB:{B1[0]}"), cells, T0, random.Random(0)) is None


# ── 4 · 동시 주입 — 서로 덮지 않는다 ────────────────────────────────────

def test_two_concurrent_faults_keep_their_own_target():
    S.set_shift_cfg(CFG)
    cells = _non_b(_master())
    s1 = faults.build(_row("SITE:北駅", fid=1), cells, T0, random.Random(1))
    s2 = faults.build(_row("SITE:南駅", fid=2), cells, T0, random.Random(2))
    assert (s1.target.enb_id, s2.target.enb_id) == (A1, A2)
    assert s1.target.shift_to in B1 and s2.target.shift_to in B2
    assert s1.target.cell_keys.isdisjoint(s2.target.cell_keys)
    assert S.SHIFT is S.SHIFT and S.SHIFT.from_enb == A1                 # 전역은 설정뿐 — 주입이 바꾸지 않는다


@pytest.fixture
def gen(monkeypatch):
    for f in ("pemdb1/_distributions.json", "profile.json"):
        if not (ROOT / "data" / f).exists():
            pytest.skip(f"data/{f} 없음")
    monkeypatch.setattr(P, "load_cells", lambda *a, **k: _master())
    monkeypatch.setattr(P, "load_apps", lambda conn: None)
    return P.PemdbGen(None, ROOT / "data", rates={"lte_call": 2000, "transport": 1500},
                      seed=1, subscriber_pool=2000, capacity_shift=CFG)


def test_generator_standby_holds_every_sites_b_and_cells_exclude_them(gen):
    assert sorted(gen.standby) == sorted(ALL_B)
    assert all(len(v) == 3 for v in gen.standby.values())
    assert {c.enb_id for c in gen.cells}.isdisjoint(ALL_B)
    assert {A1, A2, A3} <= {c.enb_id for c in gen.cells}                 # A 는 평소 생성 대상


def test_standby_enbs_are_not_handover_neighbours_in_any_site(gen):
    topo = gen.ho_topo
    for c in gen.cells:
        for nb in topo.inter.get(c.cell_key, ()) + topo.intra.get(c.cell_key, ()):
            assert nb.enb_id not in ALL_B


def _sc(gen, site, fid):
    t = faults._shift_target(f"SITE:{site}", T0, _non_b(_master()))
    sc = faults.build(_row(f"SITE:{site}", fid=fid), _non_b(_master()), T0, random.Random(fid))
    sc._minute = sc.start_minute + S.SHIFT_RISE                              # B 개통 뒤 — 분산 60%
    sc.active_cells_cache = set(t.cell_keys)
    return sc


def test_shifter_sends_each_fault_only_to_its_own_b(gen):
    s1, s2 = _sc(gen, "北駅", 1), _sc(gen, "南駅", 2)
    shift = gen._shifter(s1._minute, [s1, s2])
    assert shift is not None
    got = {A1: set(), A2: set()}
    for _ in range(400):
        for a, sc in ((A1, s1), (A2, s2)):
            cell = next(c for c in gen.cells if c.enb_id == a)
            r = shift(cell)
            if r is not None:
                got[a].add(r.enb_id)
    assert got[A1] == {s1.target.shift_to} and got[A2] == {s2.target.shift_to}
    assert got[A1] <= set(B1) and got[A2] <= set(B2)


def test_shifter_leaves_a_site_without_a_fault_alone(gen):
    s1 = _sc(gen, "北駅", 1)
    shift = gen._shifter(s1._minute, [s1])
    cell3 = next(c for c in gen.cells if c.enb_id == A3)
    assert all(shift(cell3) is None for _ in range(100))


def test_relocator_sends_dark_b_sessions_to_their_own_a(gen):
    reloc = gen._relocator(0, [])
    assert reloc is not None
    homes = {A1: set(B1), A2: set(B2), A3: set(B3)}
    for a, bs in homes.items():
        for b in bs:
            for cell in gen.standby[b]:
                got = reloc(cell)
                assert got is not None and got.enb_id == a, (b, got and got.enb_id)


def test_relocator_spares_the_lit_b_but_not_the_other_sites(gen):
    s1 = _sc(gen, "北駅", 1)
    lit = s1.target.shift_to
    reloc = gen._relocator(s1._minute, [s1])
    assert all(reloc(c) is None for c in gen.standby[lit])               # 켜진 B 는 떠나지 않는다
    other = next(b for b in B1 if b != lit)
    assert all(reloc(c).enb_id == A1 for c in gen.standby[other])
    assert all(reloc(c).enb_id == A2 for c in gen.standby[B2[0]])


def test_no_call_reaches_any_sites_b_without_the_scenario(gen):
    gen.anchor = T0
    i_c = gen.gen.index["lte_call"]["First_eNB_ID"]
    for m in range(4):
        recs = gen.records(m, T0 + timedelta(minutes=m))
        gen.uplane_records(m, T0 + timedelta(minutes=m))
        assert not any(r[i_c] in ALL_B for r in recs)


# ── 5 · /api/map/standby ────────────────────────────────────────────────

class _Cur:
    def __init__(self, rows): self._rows = rows
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, sql, params=None): self.sql = sql
    def fetchall(self): return self._rows


class _Conn:
    def __init__(self, rows): self._rows = rows
    def cursor(self): return _Cur(self._rows)
    def close(self): pass


def _api(monkeypatch, rows, now):
    import datagen.cli as cli
    monkeypatch.setattr(cli, "_capacity_shift", lambda pem: CFG)
    monkeypatch.setattr(map_api, "_pem_cfg", lambda: {})
    monkeypatch.setattr(map_api, "_connect", lambda: _Conn(rows))
    monkeypatch.setattr(map_api, "datetime", SimpleNamespace(now=lambda: now))
    return map_api.api_map_standby()


def test_standby_api_without_fault_lists_every_b_off_with_from_of(monkeypatch):
    r = _api(monkeypatch, [], T0)
    assert r["enbs"] == {b: "off" for b in ALL_B}
    assert r["from_of"] == {B1[0]: A1, B1[1]: A1, B2[0]: A2, B3[0]: A3}
    assert r["from"] == A1 and [s["name"] for s in r["sites"]] == ["北駅", "南駅", "東駅"]
    assert r["sites"][1] == {"name": "南駅", "from_enb": A2, "standby_enbs": list(B2)}


def test_standby_api_turns_on_only_that_sites_b(monkeypatch):
    start = T0 - timedelta(minutes=S.SHIFT_RISE + 1)
    r = _api(monkeypatch, [(start, f"SITE:南駅")], T0)
    assert r["enbs"][B2[0]] == "on"
    assert all(r["enbs"][b] == "off" for b in B1 + B3)


def test_standby_api_two_sites_at_once_and_next_phase(monkeypatch):
    on_start = T0 - timedelta(minutes=S.SHIFT_RISE + 1)
    next_start = T0 - timedelta(minutes=5)
    r = _api(monkeypatch, [(on_start, "SITE:東駅"), (next_start, "SITE:南駅")], T0)
    assert r["enbs"][B3[0]] == "on" and r["enbs"][B2[0]] == "next"
    assert r["enbs"][B1[0]] == r["enbs"][B1[1]] == "off"


def test_standby_api_agrees_with_the_generators_b_pick(monkeypatch):
    start = T0 - timedelta(minutes=S.SHIFT_RISE + 1)
    r = _api(monkeypatch, [(start, "SITE:北駅")], T0)
    want = S.shift_pick(int(start.timestamp() // 60), B1)
    assert r["enbs"][want] == "on"
    t = faults._shift_target("SITE:北駅", start, _non_b(_master()))   # 같은 시작 시각이면 생성기도 같은 B
    S.set_shift_cfg(CFG)
    t = faults._shift_target("SITE:北駅", start, _non_b(_master()))
    assert t.shift_to == want


def test_standby_api_ignores_unknown_site_and_finished_fault(monkeypatch):
    start = T0 - timedelta(minutes=S.SHIFT_TOTAL + 5)
    r = _api(monkeypatch, [(start, "SITE:北駅"), (T0 - timedelta(minutes=40), "SITE:없는역")], T0)
    assert set(r["enbs"].values()) == {"off"}


def test_standby_api_ver_changes_with_state(monkeypatch):
    a = _api(monkeypatch, [], T0)["ver"]
    b = _api(monkeypatch, [(T0 - timedelta(minutes=S.SHIFT_RISE + 1), "SITE:北駅")], T0)["ver"]
    assert a != b


def test_map_html_reads_from_of():
    html = (ROOT / "src/datagen/ui/static/map.html").read_text(encoding="utf-8")
    assert "(STBY.from_of||{})[d.dataset.enb]||STBY.from" in html


# ── 6 · 정답지 ──────────────────────────────────────────────────────────

class _C:
    def __init__(self, k): self.cell_key = k


def test_unscored_truth_carries_each_faults_own_a_and_b(tmp_path):
    S.set_shift_cfg(CFG)
    cells = _non_b(_master())
    s1 = faults.build(_row("SITE:北駅", fid=1), cells, T0, random.Random(1))
    s2 = faults.build(_row("SITE:東駅", fid=2), cells, T0, random.Random(2))
    standby = {b: [_C(f"{b}-{n}") for n in range(3)] for b in ALL_B}
    doc = P.write_unscored_truth(tmp_path / "u.json", T0, [s1, s2], standby)
    r1, r2 = doc["runs"][s1.id], doc["runs"][s2.id]
    assert (r1["a_enb"], r2["a_enb"]) == (A1, A3)
    assert r1["b_enb"] in B1 and r2["b_enb"] == B3[0]
    assert r1["a_cells"] == sorted(f"{A1}-{n}" for n in range(3))
    assert r2["b_cells"] == [f"{B3[0]}-{n}" for n in range(3)]
    assert set(r1["a_cells"]).isdisjoint(r2["a_cells"])
