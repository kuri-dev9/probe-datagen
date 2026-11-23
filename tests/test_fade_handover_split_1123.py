"""핸드오버로 **그 분 안에서** 대상 셀을 떠난 세션은 그 분 남은 몫의 망가짐을 새 셀로 들고 가지 않는다 — 2026-11-23 (F-536 · 22번).

전에는 걷힘 가중을 분을 여는 셀 기준으로 그 분 전체에 한 번 붙였다. 분 안에서 이웃 셀로 넘어가면 뒤 몫이 옛 셀의 가중 1 을 지녀
정답 밖 셀 115개가 셀마다 1~2분씩 손실을 냈다. 이제 `session._fade_w` 가 새 셀 가중을 따로 구해 `(prev, mid, w_옛)` · `(mid, now, w_새)` 로 쪼갠다.
DB 없이 — 합성 셀 마스터(`test_disaster`). `_route_cell` 을 가짜로 바꿔 세션을 대상 밖/안 셀로 옮긴다."""
from __future__ import annotations

from datetime import timedelta

import pytest

from datagen import pemdb1 as P

import test_disaster as TD
import test_session_fade_1119 as TF

PRE = 15                                 # 이 분 이전까지 돌려 세션을 쌓는다


def _setup(kind, mp):
    mp.setattr(P, "load_cells", lambda *a, **k: TD._master())
    mp.setattr(P, "load_apps", lambda conn: TF.BOOK)             # 결제 앱 지연(앱 범위)이 앱 목록을 읽는다
    g = P.PemdbGen(None, TD.ROOT / "data", rates={"lte_call": 1500, "transport": 1500}, seed=11, subscriber_pool=2500)
    g.anchor = TD.T0
    g.gen.app_book = TF.BOOK
    scs, bad = g.set_fault_rows([{"fault_id": 1, "kind": kind, "target": None,
                                  "start_ts": TD.T0 + timedelta(minutes=10), "duration_min": 11, "intensity": 1.0}])
    assert scs and not bad
    return g, scs[0]


def _move(kind):
    """장애 중 세션 반을 대상 밖으로, 반을 대상 안 다른 셀로 옮긴 한 분의 `fade_seg` 증분 — {"out","in"} · 대상 · 다음 분 셀"""
    mp = pytest.MonkeyPatch()
    try:
        g, sc = _setup(kind, mp)
        cells = TD._master()
        if sc.target.cell_keys:                                   # 대상을 절반으로 줄여 대상 밖 셀이 있게 한다
            half = {c.cell_key for c in cells if int(c.enb_id) % 2 == 0}
            sc.target.cell_keys = (set(sc.target.cell_keys) & half) or half
        inside = set(sc.target.cell_keys)
        outside = [c for c in cells if c.cell_key not in inside]
        for m in range(PRE):
            ts = TD.T0 + timedelta(minutes=m)
            g.records(m, ts)
            g.uplane_records(m, ts)
        eng = g.sessions
        faded = [s for s in eng.active if s.fade_sc is sc and 0 < s.i < s.span - 1 and (not inside or s.cell.cell_key in inside)]
        assert len(faded) >= 4, "걷힘 세션이 없다 — 하니스가 세션을 못 잡았다"
        half_n = len(faded) // 2
        out_ids = {id(s) for s in faded[:half_n]}
        in_ids = {id(s) for s in faded[half_n:]}
        orig = eng._route_cell

        def fake(s):
            if id(s) in out_ids and outside:
                return outside[0], True
            if id(s) in in_ids:
                dst = next((c for c in cells if (not inside or c.cell_key in inside) and c is not s.cell), None)
                return dst, True
            return orig(s)

        mp.setattr(eng, "_route_cell", fake)
        n0 = {id(s): len(s.fade_seg) for s in faded}
        ts = TD.T0 + timedelta(minutes=PRE)
        g.records(PRE, ts)
        g.uplane_records(PRE, ts)

        def segs(ids):
            return [s.fade_seg[n0[id(s)]:] for s in faded if id(s) in ids and len(s.fade_seg) > n0[id(s)]]

        return {"out": segs(out_ids), "in": segs(in_ids), "inside": inside, "has_outside": bool(outside)}
    finally:
        mp.undo()


@pytest.fixture(scope="module")
def cell_bound():
    return _move("transport_loss")


def test_a_session_that_leaves_the_target_cells_mid_minute_is_split_with_a_zero_tail(cell_bound):
    got = cell_bound["out"]
    assert got and cell_bound["has_outside"]
    assert all(len(x) == 2 for x in got), got[:3]
    assert all(x[0][2] > 0 and x[1][2] == 0.0 for x in got)              # 앞 몫은 옛 셀 가중 · 뒤 몫은 새 셀(대상 밖) 0
    assert all(x[0][1] == x[1][0] for x in got)                          # 이어진 구간 — 어긋남이 없다


def test_moving_between_target_cells_does_not_split(cell_bound):
    got = cell_bound["in"]
    assert got
    assert all(len(x) == 1 and x[0][2] > 0 for x in got), got[:3]


def test_a_nationwide_fault_has_no_cell_filter_so_nothing_is_split():
    """대상 셀이 없는 장애(전국) — 셀로 거르지 않으므로 어디로 옮겨도 가중이 같고 쪼개지지 않는다."""
    got = _move("pay_dns_delay")   # 전국·앱 범위 — 16번 cdn 은 흐른 양이 없어 걷힘 하니스로 못 쓴다(2026-11-23)
    assert not any(got["out"]) or all(len(x) <= 1 for x in got["out"])
    assert all(len(x) <= 1 for x in got["in"])


def test_sum_of_delta_still_equals_total_for_sessions_split_at_a_handover():
    """쪼갠 구간도 같은 `_fade_g` 급수로 Total·Delta 를 내므로 `Σ Delta == Total`(V3 U-1·U-3)이 유지된다."""
    mp = pytest.MonkeyPatch()
    try:
        g, sc = _setup("transport_loss", mp)
        cells = TD._master()
        half = {c.cell_key for c in cells if int(c.enb_id) % 2 == 0}
        sc.target.cell_keys = (set(sc.target.cell_keys) & half) or half
        outside = [c for c in cells if c.cell_key not in sc.target.cell_keys]
        assert outside
        eng = g.sessions
        orig = eng._route_cell
        moved = set()

        def fake(s):
            if s.fade_sc is sc and 0 < s.i < s.span - 1 and s.cell.cell_key in sc.target.cell_keys and len(moved) < 200:
                moved.add(id(s))
                return outside[0], True
            return orig(s)

        mp.setattr(eng, "_route_cell", fake)
        ix = g.gen.index["transport"]
        seen, dsum = {}, {}
        ends = bad = inv = 0
        for m in range(PRE + 16):                       # 처음부터 센다 — 중간부터 세면 앞 분 Delta 가 빠져 합이 어긋난다
            ts = TD.T0 + timedelta(minutes=m)
            g.records(m, ts)
            for r in g.uplane_records(m, ts).get("transport", []):
                sid, flag = r[ix["SessionID"]], r[ix["OngoingFlag"]]
                tot = int(r[ix["Traffic_Total_Dnlink_RetransPacketCount"]] or 0)
                dl = int(r[ix["Traffic_Delta_Dnlink_RetransPacketCount"]] or 0)
                if seen.get(sid) is not None and tot < seen[sid] and flag not in ("0", "20"):
                    inv += 1
                seen[sid] = tot
                dsum[sid] = dl if flag in ("0", "20") else dsum.get(sid, 0) + dl
                if flag in ("2", "20"):
                    ends += 1
                    bad += dsum[sid] != tot
        assert moved and ends
        assert inv == 0 and bad == 0, (inv, bad, ends)
    finally:
        mp.undo()
