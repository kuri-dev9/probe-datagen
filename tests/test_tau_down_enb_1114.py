"""정지 eNB 에는 TAU 도 남지 않는다 — 2026-11-14 (27번 F-328 · 동구 수정 `pemdb1` TAU 루프 `dead(dst)` 고정).

TAU(5)는 이동 중인 가입자가 TA 경계를 넘은 결과라 호 루프의 억제·재배치·다운 거름을 지나지 않았다. 정지 eNB 두 곳에
TAU 가 20분에 18건 남아 D4 무호 run 이 끊겼다(확정 실패). 이 시험은 꺼진 셀로 들어가는 전환을 `tau_events` 에
직접 넣어 재현한다 — 합성 하니스는 이동이 없어 평소에는 TAU 가 생기지 않는다. DB 를 쓰지 않는다.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from datagen import pemdb1 as P
from datagen import scenarios as S
from datagen.cells import Cell

ROOT = Path(__file__).resolve().parents[1]
T0 = datetime(2026, 11, 14, 11, 0)
TAU = "5"


def _cell(enb: str, n: int) -> Cell:
    k = int(enb[-2:])
    return Cell(
        cell_key=f"{enb}-{n}", enb_id=enb, cell_id=n, enb_seq=0, c_uid="",
        tac=100 + (k % 2), plmn="44010", vlan_id="", latitude=35.60 + k * 0.004 + n * 0.0005,
        longitude=139.60 + k * 0.004 + n * 0.0005, adm_sido_cd="11", adm_sido_nm="",
        adm_sgg_cd="11200", adm_sgg_nm="", adm_emd_cd="11200011", adm_emd_nm="",
        geo_status="MAPPED", rat="EUTRAN", mme_id="0001", sgw_id="0037", pgw_id="0037",
        ims_pgw_id="0037", traffic_weight=1.0, display_name="")


def _master() -> list[Cell]:
    return [_cell(f"{500000 + e}", n) for e in range(40) for n in range(3)]


@pytest.fixture
def pg():
    mp = pytest.MonkeyPatch()
    for f in ("pemdb1/_distributions.json", "profile.json"):
        if not (ROOT / "data" / f).exists():
            pytest.fail(f"data/{f} 가 없다")
    mp.setattr(P, "load_cells", lambda *a, **k: _master())
    mp.setattr(P, "load_apps", lambda conn: None)
    g = P.PemdbGen(None, ROOT / "data", rates={"lte_call": 1500, "transport": 1500}, seed=7, subscriber_pool=2500)
    yield g
    mp.undo()


def _inject_tau(pg, dst_enbs: list[str], n: int = 5):
    """분마다 `n` 건의 TAU 전환을 더 넣는다 — 도착 셀이 `dst_enbs` 의 셀인 것과 정지와 무관한 살아 있는 eNB 인 것."""
    by_enb: dict[str, list[Cell]] = {}
    for c in _master():
        by_enb.setdefault(c.enb_id, []).append(c)
    live = [e for e in by_enb if e not in dst_enbs][:6]
    orig = pg.gen._advance_walkers

    def wrapped(now):
        moved = orig(now)
        for i in range(n):
            e = dst_enbs[i % len(dst_enbs)]
            pg.gen.tau_events.append((i, by_enb[live[0]][0], by_enb[e][i % 3]))
            pg.gen.tau_events.append((i, by_enb[live[1]][0], by_enb[live[2 + i % 3]][i % 3]))
        return moved

    pg.gen._advance_walkers = wrapped
    return live


def _run(pg, minutes: int, rows: list[dict]):
    pg.anchor = T0
    scs, bad = pg.set_fault_rows(rows)
    assert scs and not bad, f"장애가 해석되지 않았다: {bad}"
    out = []
    for m in range(minutes):
        ts = T0 + timedelta(minutes=m)
        out.append(pg.records(m, ts))
    return out, scs[0]


def _first_last_enb_rows(pg, rows, enbs, call_type=TAU):
    idx = pg.gen.index["lte_call"]
    ci, fi, li = idx["call_type"], idx["First_eNB_ID"], idx["Last_eNB_ID"]
    return [r for r in rows if r[ci] == call_type and (r[fi] in enbs or r[li] in enbs)]


def _tau_rows(pg, rows):
    idx = pg.gen.index["lte_call"]
    return [r for r in rows if r[idx["call_type"]] == TAU]


def test_sc17_enb_outage_has_no_tau_into_the_dead_enb_and_it_comes_back(pg):
    enb = "500025"
    _inject_tau(pg, [enb])
    row = {"fault_id": 9, "kind": "enb_s1_outage", "target": f"ENB:{enb}",
           "start_ts": T0 + timedelta(minutes=5), "duration_min": 8, "intensity": 1.0}
    runs, sc = _run(pg, 30, [row])
    dead = [m for m in range(sc.start_minute, sc.start_minute + sc.duration) if sc.intensity(m) >= S.OUTAGE_GATE]
    assert dead
    assert sum(len(_first_last_enb_rows(pg, runs[m], {enb})) for m in range(0, 5)) > 0, "장애 전에는 그 eNB 로 가는 TAU 가 있다(주입 확인)"
    for m in dead:
        assert not _first_last_enb_rows(pg, runs[m], {enb}), f"{m}분: 단절 eNB 의 TAU"
    after = sum(len(_first_last_enb_rows(pg, runs[m], {enb})) for m in range(max(dead) + 1, 30))
    assert after > 0, "정지가 끝난 뒤에는 그 eNB 로 들어가는 TAU 가 다시 나와야 한다"


def test_sc17_other_taus_survive_during_the_outage(pg):
    enb = "500025"
    live = _inject_tau(pg, [enb])
    row = {"fault_id": 9, "kind": "enb_s1_outage", "target": f"ENB:{enb}",
           "start_ts": T0 + timedelta(minutes=5), "duration_min": 8, "intensity": 1.0}
    runs, sc = _run(pg, 20, [row])
    dead = [m for m in range(sc.start_minute, sc.start_minute + sc.duration) if sc.intensity(m) >= S.OUTAGE_GATE]
    kept = sum(len(_tau_rows(pg, runs[m])) for m in dead if m < len(runs))
    assert kept > 0, "살아 있는 셀로 가는 TAU 까지 거르면 안 된다"


def _quake_row(target: str):
    return {"fault_id": 27, "kind": "natural_disaster", "target": target,
            "start_ts": T0 + timedelta(minutes=5), "duration_min": 40, "intensity": 1.0}


def test_27_no_record_of_any_kind_at_a_dead_enb_while_down_but_live_taus_remain(pg):
    # 합성 마스터 중심(35.60+0.078, 139.60+0.078) 근처에 진도 4.0 으로 정지 eNB 를 여럿 만든다
    pg.anchor = T0
    scs, bad = pg.set_fault_rows([_quake_row("QUAKE:35.678,139.678,4.0,ROUND")])
    assert scs and not bad
    sc = scs[0]
    dead = sorted(S.disaster_dead_enbs(sc, sc.start_minute + S.DISASTER_DOWN_AT + 1))
    assert dead, "진도 4.0 에서 꺼진 eNB 가 있어야 시험이 성립한다"
    dead_set = set(dead)
    by_enb: dict[str, list[Cell]] = {}
    for c in _master():
        by_enb.setdefault(c.enb_id, []).append(c)
    live = [e for e in by_enb if e not in dead_set][:4]
    orig = pg.gen._advance_walkers

    def wrapped(now):
        moved = orig(now)
        for i in range(6):
            pg.gen.tau_events.append((i, by_enb[live[0]][0], by_enb[dead[i % len(dead)]][i % 3]))    # 꺼진 셀로
            pg.gen.tau_events.append((i, by_enb[live[1]][0], by_enb[live[2 + i % 2]][i % 3]))         # 살아 있는 셀로
        return moved

    pg.gen._advance_walkers = wrapped
    lo = sc.start_minute + S.DISASTER_DOWN_AT + 1          # 정지 첫 분의 열린 세션 처리가 끝난 분부터
    hi = sc.start_minute + S.DISASTER_DOWN_END
    out = []
    for m in range(0, hi + 8):
        out.append(pg.records(m, T0 + timedelta(minutes=m)))
    for m in range(lo, hi):
        bad_rows = _first_last_enb_rows(pg, out[m], dead_set)
        assert not bad_rows, f"{m}분: 정지 eNB 의 TAU {len(bad_rows)}건"
    live_taus = sum(len(_tau_rows(pg, out[m])) for m in range(lo, hi))
    assert live_taus > 0, "살아 있는 셀로 가는 TAU 는 남아야 한다"
    assert sum(len(_first_last_enb_rows(pg, out[m], dead_set)) for m in range(hi, hi + 8)) > 0, "정상화 뒤에는 TAU 가 돌아온다"
