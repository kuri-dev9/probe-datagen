"""추가 lte_call 가입자(11-20 · 맥스) — lte_call 만 늘고 U-Plane 은 추가 가입자 수와 무관하게 같다.

DB 를 쓰지 않는다 — 합성 셀 마스터 + 고정 시드, 분 단위 오프라인 생성만 한다(결과는 메모리).
"""
from __future__ import annotations

import collections
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from datagen import pemdb1 as P
from datagen.cells import Cell

ROOT = Path(__file__).resolve().parents[1]
T0 = datetime(2026, 11, 20, 12, 0)
POOL = 2000
MAXN = 3000


def _cell(enb: str, n: int, lat: float, lon: float) -> Cell:
    return Cell(
        cell_key=f"{enb}-{n}", enb_id=enb, cell_id=n, enb_seq=0, c_uid="",
        tac=100, plmn="44010", vlan_id="", latitude=lat, longitude=lon,
        adm_sido_cd="11", adm_sido_nm="", adm_sgg_cd="11200", adm_sgg_nm="",
        adm_emd_cd="11200011", adm_emd_nm="", geo_status="MAPPED",
        rat="EUTRAN", mme_id="0001", sgw_id="0037", pgw_id="0037",
        ims_pgw_id="0037", traffic_weight=1.0, display_name="")


def _master() -> list[Cell]:
    out = []
    for e in range(200):
        lat, lon = 35.20 + (e // 20) * 0.012, 139.20 + (e % 20) * 0.012
        out += [_cell(f"{600000 + e}", n, lat + n * 0.0015, lon + n * 0.0015) for n in range(3)]
    return out


def _gen(monkeypatch, extra_max: int, extra_n: int):
    for f in ("pemdb1/_distributions.json", "profile.json"):
        if not (ROOT / "data" / f).exists():
            pytest.fail(f"data/{f} 가 없다")
    monkeypatch.setattr(P, "load_cells", lambda *a, **k: _master())
    monkeypatch.setattr(P, "load_apps", lambda conn: None)
    g = P.PemdbGen(None, ROOT / "data", rates={"lte_call": 2000, "transport": 1500},
                   seed=7, subscriber_pool=POOL, extra_lte_max=extra_max)
    g.anchor = T0
    g.set_extra_subs(extra_n)
    return g


def _minute(g, m: int):
    ts = T0 + timedelta(minutes=m)
    calls = g.records(m, ts)
    up = g.uplane_records(m, ts)
    return calls, up


@pytest.fixture
def runs(monkeypatch):
    """추가 0명 · 1천 명 · 3천 명을 같은 시드·같은 분으로 — 첫 분의 (lte_call, U-Plane)"""
    out = {}
    for n in (0, 1000, 3000):
        g = _gen(monkeypatch, MAXN, n)
        out[n] = (g, *_minute(g, 0))
    return out


def _identity(g, series, row):
    """측정값(시각·크기·시간 칸)을 뺀 나머지 — 누가 · 어느 셀에서 · 어떤 세션인가"""
    idx = g.gen.index[series]
    return [v for k, j in idx.items() if j < len(row)
            for v in [row[j]] if not (k.endswith("Time") or k.startswith("Traffic_"))]


def test_uplane_rows_and_who_are_the_same_whatever_the_extra_count(runs):
    """U-Plane 은 행 수와 **누구의 세션인가**와 측정값이 모두 같다(맥스가 추가 호 동안 공용 난수를 얼린 뒤 · 11-20 k2110)"""
    g0, _c0, base = runs[0]
    assert base and sum(len(v) for v in base.values()) > 0
    for n in (1000, 3000):
        g, _c, up = runs[n]
        assert set(up) == set(base)
        for k in base:
            assert len(up[k]) == len(base[k]), f"{k}: 추가 {n}명에서 U-Plane 행 수가 달라졌다"
            assert [_identity(g, k, r) for r in up[k]] == [_identity(g0, k, r) for r in base[k]]
            assert up[k] == base[k], f"{k}: 첫 분 측정값이 추가 {n}명에서 달라졌다"


def test_lte_call_grows_with_the_extra_count(runs):
    """추가 가입자는 60~120초에 한 번 TAU 1건(11-23 확정) — 분당 늘어난 수 ≈ 인원 × 평균(60/주기) = 인원 × ln2 ≈ 0.69"""
    c0, c1, c3 = (len(runs[n][1]) for n in (0, 1000, 3000))
    assert c0 < c1 < c3
    for n, c in ((1000, c1), (3000, c3)):
        want = n * 0.693
        assert abs((c - c0) - want) <= 0.1 * want, (n, c - c0, want)


def test_extra_records_are_one_tau_per_person_from_the_home_cell(runs):
    """늘어난 기록은 전부 TAU(call_type 5) · 한 사람 한 분에 많아야 한 건 · 통화·데이터 절차가 아니다"""
    g, calls, _up = runs[3000]
    ix = g.gen.index["lte_call"]
    extra = {s.imsi for s in g.extra_subs}
    got = [r for r in calls if r[ix["IMSI"]] in extra]
    assert got, "추가 가입자 기록이 없다"
    assert {r[ix["call_type"]] for r in got} == {P.TAU_CALL_TYPE}
    who = collections.Counter(r[ix["IMSI"]] for r in got)
    assert max(who.values()) == 1


def test_extra_beat_is_a_fixed_60_to_120s_period_per_person(monkeypatch):
    """박자는 IMSI 로 정해져 재시작·인원 변경에도 같고, 한 사람의 간격은 60~120초다"""
    g = _gen(monkeypatch, MAXN, 3000)
    t0 = int(T0.timestamp())
    seen = collections.defaultdict(list)
    for m in range(6):
        for j in g._extra_beat(t0 + 60 * m, 3000):
            seen[j].append(m)
    assert len(seen) > 2000
    per, ph = g._beat
    assert per.min() >= 60 and per.max() <= 120
    assert (ph < per).all()
    g2 = _gen(monkeypatch, MAXN, 1000)
    assert g2._extra_beat(t0, 1000) == [j for j in g._extra_beat(t0, 3000) if j < 1000]


def test_existing_lte_calls_are_a_prefix_of_the_run_with_extras(runs):
    """추가 호는 맨 끝이라 기존 호 레코드는 그대로 앞에 있다"""
    a = runs[0][1]
    b = runs[3000][1]
    assert b[:len(a)] == a


def test_extra_subscribers_do_not_collide_with_the_regular_pool(runs):
    g = runs[3000][0]
    regular = {s.imsi for s in g.gen.subs}
    extra = {s.imsi for s in g.extra_subs}
    assert len(extra) == len(g.extra_subs) == MAXN
    assert not (regular & extra)
    assert not (set(g.gen.sub_by_imsi) & extra)


def test_extra_subscribers_never_open_a_uplane_session(runs):
    g, calls, up = runs[3000]
    extra = {s.imsi for s in g.extra_subs}
    i = g.gen.index["lte_call"]["IMSI"]
    got = [r for r in calls if r[i] in extra]
    assert got, "추가 가입자 호가 하나도 없다"
    for name, rows in up.items():
        idx = g.gen.index.get(name, {}).get("IMSI")
        if idx is None:
            continue
        assert not [r for r in rows if r[idx] in extra], f"{name}: 추가 가입자 세션이 있다"


def test_set_extra_subs_clamps_to_the_prebuilt_count(monkeypatch):
    g = _gen(monkeypatch, MAXN, 0)
    assert g.set_extra_subs(-5) == 0
    assert g.set_extra_subs(10 ** 9) == MAXN
    assert g.set_extra_subs(1234) == 1234


def test_without_extra_max_nothing_changes(monkeypatch):
    """`extra_lte_max` 가 0 인 기존 경로(시험·배치) — 같은 시드의 출력이 같다"""
    a = _gen(monkeypatch, 0, 0)
    b = _gen(monkeypatch, MAXN, 0)
    ca, ua = _minute(a, 0)
    cb, ub = _minute(b, 0)
    assert ca == cb and ua == ub


def test_the_same_extra_people_stay_when_the_value_changes(monkeypatch):
    g = _gen(monkeypatch, MAXN, 3000)
    first = [s.imsi for s in g.extra_subs[:1000]]
    g.set_extra_subs(1000)
    assert [s.imsi for s in g.extra_subs[:1000]] == first


def test_uplane_rows_are_identical_over_several_minutes(monkeypatch):
    """맥스가 추가 호를 만드는 동안 공용 난수를 얼려(11-20 k2110) 둘째 분부터의 어긋남(+2~3%)이 사라졌다"""
    rows = {}
    for n in (0, 3000):
        g = _gen(monkeypatch, MAXN, n)
        rows[n] = [_minute(g, m)[1] for m in range(4)]
    assert rows[0] == rows[3000]


def test_extra_zero_output_is_unchanged_over_several_minutes(monkeypatch):
    a = _gen(monkeypatch, 0, 0)
    b = _gen(monkeypatch, MAXN, 0)
    for m in range(3):
        assert _minute(a, m) == _minute(b, m)


def test_lte_call_of_the_regular_pool_is_a_prefix_every_minute(monkeypatch):
    a = _gen(monkeypatch, MAXN, 0)
    b = _gen(monkeypatch, MAXN, 3000)
    for m in range(3):
        ca, cb = _minute(a, m)[0], _minute(b, m)[0]
        assert cb[:len(ca)] == ca and len(cb) > len(ca)


def test_extra_calls_never_land_on_a_dark_standby_enb(monkeypatch):
    """24번 소등 eNB(B) — 켜지는 분이 아니면 추가 가입자 호도 B 로 가지 않는다(11-20 · 정지 셀 확인)"""
    import test_capacity_shift as CS
    from datagen import scenarios as S
    monkeypatch.setattr(P, "load_cells", lambda *a, **k: CS._master())
    monkeypatch.setattr(P, "load_apps", lambda conn: None)
    out = {}
    for n in (0, 3000):
        g = P.PemdbGen(None, ROOT / "data", rates={"lte_call": 2000, "transport": 1500}, seed=1,
                       subscriber_pool=2000, extra_lte_max=MAXN,
                       capacity_shift={"from_enb": CS.A, "standby_enbs": list(CS.POOL)})
        g.set_extra_subs(n)
        try:
            out[n] = CS._run(g, datetime(2026, 11, 20, 15, 0), 3, 3 + S.SHIFT_TOTAL + 4)
        finally:
            S.set_shift_cfg(None)
    b0 = [x[1] for x in out[0]]
    b3 = [x[1] for x in out[3000]]
    dark = [i for i, v in enumerate(b0) if v == 0]
    assert dark, "B 가 한 번도 꺼져 있지 않은 구간이 없다 — 시험이 의미 없다"
    assert all(b3[i] == 0 for i in dark), "추가 가입자 호가 꺼진 B 로 갔다"
