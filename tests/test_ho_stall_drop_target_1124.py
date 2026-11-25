"""3번 전환 순간 정체 · 8번 호 많은 eNB 군집 · 주입 API 곡선 경고 (2026-11-24 · 사용자 결정 3건).

* 3번 `ho_interruption` — 정체 표시(`DnlinkWindowCloseCount`)를 **정답 셀로 전환해 들어간 순간부터** 단다.
  전에는 세션을 열 때 달아 전환하지 않는 세션에도 정체가 붙고 구간 끝 칸이 묽어졌다(F-598·F-600 놓친 셀의 대부분이 끝 칸).
* 8번 `ran_drop_surge` — 자동 대상을 호가 많은 eNB 군집부터 고른다(F-601 은 분당 3.5건 · 0/10).
* 주입 API — 곡선 끔·구간 밖이면 `warnings` 를 돌려주되 막지 않는다(11-24 아침 3번 F-595 무효).

DB 없이 돈다 — 합성 셀·노선·가짜 설정.
"""
from __future__ import annotations

import math
import sys
from datetime import datetime
from pathlib import Path

import pytest
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_session_model import _mobile_run, gen, RATES, ANCHOR, _calls  # noqa: E402,F401
from test_route_segment import _cell as _rcell, _line_route  # noqa: E402

from datagen import faults  # noqa: E402
from datagen import handover as HO  # noqa: E402
from datagen import pemdb1  # noqa: E402
from datagen import scenarios as S  # noqa: E402
from datagen import session as SES  # noqa: E402
from datagen.cells import Cell  # noqa: E402
from datagen.ui import faults_api  # noqa: E402

SRC = Path(__file__).resolve().parents[1] / "src" / "datagen"


# ── 3번 · 정체를 전환 순간에 건다 ─────────────────────────────────────────────

def test_only_ho_interruption_skips_the_start_cell_filter():
    assert S.PLAN_ANY_START == frozenset({"ho_interruption"})
    src = (SRC / "pemdb1.py").read_text(encoding="utf-8")
    assert "sc.kind not in PLAN_ANY_START" in src, "시작 셀 검사를 건너뛰는 자리가 사라졌다"


def test_ho_interruption_plan_carries_the_target_cells():
    """정체 요청에 정답 셀 집합을 실어 보낸다 — 세션이 전환 도착 셀을 이 집합으로 판정한다."""
    import random
    from types import SimpleNamespace
    sc = SimpleNamespace(active_cells_cache={"1-0", "2-0"})
    plan = SimpleNamespace(outcome="ok", ho_stall=False, ho_stall_cells=None)
    assert S._ho_interruption(plan, sc, 10.0, random.Random(1), None) is True
    assert plan.ho_stall is True and plan.ho_stall_cells == {"1-0", "2-0"}


def test_ho_interruption_leaves_stall_outcomes_alone():
    import random
    from types import SimpleNamespace
    plan = SimpleNamespace(outcome="stall", ho_stall=False, ho_stall_cells=None)
    assert S._ho_interruption(plan, SimpleNamespace(active_cells_cache={"1-0"}), 10.0, random.Random(1), None) is False
    assert plan.ho_stall is False and plan.ho_stall_cells is None


def _ho_marks(runs, idx):
    """세션별 (셀 순서, 정체 표시 있는 행의 순서) — 전환 순서대로 본다."""
    i_sid, i_t = idx["SessionID"], idx["SummaryCreateTime"]
    i_cell = (idx["eNB_ID"], idx["Cell_ID"])
    i_wc = idx["DnlinkWindowCloseCount"]
    seq: dict[str, list] = {}
    for m in runs:
        for r in sorted(m["up"]["transport"], key=lambda x: float(x[i_t])):
            seq.setdefault(r[i_sid], []).append(r)
    return seq, i_cell, i_wc


def _stall_all(cell, plan):
    plan.ho_stall = True
    plan.ho_stall_cells = None          # 어느 셀로 넘어가도 건다
    return True


def _stall_nowhere(cell, plan):
    plan.ho_stall = True
    plan.ho_stall_cells = frozenset()   # 정답 셀이 하나도 없다 — 어디로 넘어가도 정체가 없다
    return True


def _split(runs, idx):
    """(단일 셀 세션들, 전환한 세션들) — 세션별 행 목록."""
    seq, i_cell, i_wc = _ho_marks(runs, idx)
    single, multi = [], []
    for rows in seq.values():
        (single if len({tuple(r[i] for i in i_cell) for r in rows}) == 1 else multi).append(rows)
    return single, multi, i_cell, i_wc


def _marked(rows_list, i_wc):
    return sum(1 for rows in rows_list if any(r[i_wc] not in ("0", "") for r in rows))


def _counts(gen, mutate):
    _r, runs, _ = _mobile_run(gen, 20, share=0.8, call_share=0.9, plan_mutate=mutate)
    single, multi, i_cell, i_wc = _split(runs, gen.index["transport"])
    return single, multi, i_cell, i_wc


def test_sessions_that_never_hand_over_get_no_stall_mark_from_the_request(gen):
    """전환이 없으면 정체도 없다(정의 「전환하지 않는 세션에는 아무 일도 없다」).

    창 닫힘은 기준선에도 있다(원래 있던 정체·수신 창 닫힘) — 그래서 **요청을 건 실행과 안 건 실행을 견준다.**
    옛 동작(세션을 열 때 표시)이면 단일 셀 세션의 표시가 기준선의 몇 배로 뛴다.
    """
    b_single, b_multi, _, i_wc = _counts(gen, None)
    s_single, s_multi, _, _ = _counts(gen, _stall_all)
    assert len(b_single) > 50 and len(b_multi) > 30, (len(b_single), len(b_multi))
    base_rate = _marked(b_single, i_wc) / len(b_single)
    req_rate = _marked(s_single, i_wc) / len(s_single)
    assert req_rate <= base_rate * 1.3 + 0.01, (base_rate, req_rate)        # 단일 셀 세션은 변함이 없다
    # 전환한 세션은 확실히 늘어난다 — 효과가 실제로 걸렸다는 뜻
    assert _marked(s_multi, i_wc) / len(s_multi) > 4 * max(_marked(b_multi, i_wc) / len(b_multi), 0.01)


def test_stall_mark_starts_after_the_handover_not_in_the_cell_left(gen):
    """떠난 셀의 줄은 이미 0 으로 나갔다 — 표시는 전환 **뒤** 레코드부터다."""
    b_single, b_multi, i_cell, i_wc = _counts(gen, None)
    _s, s_multi, _c, _w = _counts(gen, _stall_all)

    def first_cell_marked(multi):
        n = hit = 0
        for rows in multi:
            first = tuple(r[i] for i in i_cell for r in rows[:1])
            first = tuple(rows[0][i] for i in i_cell)
            head = []
            for r in rows:
                if tuple(r[i] for i in i_cell) != first:
                    break
                head.append(r)
            n += 1
            hit += any(r[i_wc] not in ("0", "") for r in head)
        return hit / max(n, 1)

    def later_marked(multi):
        n = hit = 0
        for rows in multi:
            first = tuple(rows[0][i] for i in i_cell)
            tail = [r for r in rows if tuple(r[i] for i in i_cell) != first]
            if not tail:
                continue
            n += 1
            hit += any(r[i_wc] not in ("0", "") for r in tail)
        return hit / max(n, 1)

    # 첫 셀(떠나기 전) 줄의 표시 비율은 기준선과 같고, 전환 뒤 줄의 표시 비율은 눈에 띄게 높다
    assert first_cell_marked(s_multi) <= first_cell_marked(b_multi) * 1.3 + 0.01, (
        first_cell_marked(b_multi), first_cell_marked(s_multi))
    assert later_marked(s_multi) > 4 * max(later_marked(b_multi), 0.01), (later_marked(b_multi), later_marked(s_multi))


def test_handover_outside_the_target_cells_does_not_stall(gen):
    """정답 셀이 하나도 없으면 어디로 넘어가도 표시가 늘지 않는다 — 요청은 남겨 두었다가 구간 셀로 넘어갈 때 쓴다."""
    b_single, b_multi, _, i_wc = _counts(gen, None)
    n_single, n_multi, _, _ = _counts(gen, _stall_nowhere)
    a_single, a_multi, _, _ = _counts(gen, _stall_all)
    base = _marked(b_multi, i_wc) / len(b_multi)
    none = _marked(n_multi, i_wc) / len(n_multi)
    every = _marked(a_multi, i_wc) / len(a_multi)
    assert none <= base * 1.3 + 0.01, (base, none)
    assert every > 4 * max(base, 0.01), (base, every)


def _stall_here(cell, plan):
    plan.ho_stall = True
    plan.ho_stall_cells = frozenset()   # 넘어올 정답 셀은 없다 — 오직 「연 셀이 정답 셀」만 켠다
    plan.ho_stall_here = True
    return True


def test_plan_marks_sessions_opened_in_a_target_cell():
    """정답 셀에서 연 세션은 `ho_stall_here` 가 켜진다 — 그 셀에서 NR 레그를 붙이는 순간이 전환이다."""
    import random
    from types import SimpleNamespace
    sc = SimpleNamespace(active_cells_cache={"1-0", "2-0"})
    for cell, want in ((SimpleNamespace(cell_key="1-0"), True), (SimpleNamespace(cell_key="9-9"), False), (None, False)):
        plan = SimpleNamespace(outcome="ok", ho_stall=False, ho_stall_cells=None, ho_stall_here=False)
        assert S._ho_interruption(plan, sc, 10.0, random.Random(1), cell) is True
        assert plan.ho_stall_here is want, (cell, plan.ho_stall_here)


def test_session_opened_in_the_target_cell_stalls_without_any_handover(gen):
    """정답 셀에서 연 세션은 전환이 없어도 창 닫힘이 붙는다 — 끝나는 셀이 곧 정답 셀이라 U-07 이 거기 잡힌다."""
    b_single, b_multi, _, i_wc = _counts(gen, None)
    h_single, h_multi, _, _ = _counts(gen, _stall_here)
    assert len(b_single) > 50 and len(h_single) > 50
    base = _marked(b_single, i_wc) / len(b_single)
    here = _marked(h_single, i_wc) / len(h_single)
    assert here > 4 * max(base, 0.01), (base, here)             # 단일 셀 세션도 확실히 늘어난다
    assert _marked(h_multi, i_wc) / len(h_multi) > 4 * max(_marked(b_multi, i_wc) / len(b_multi), 0.01)


def test_here_flag_off_leaves_single_cell_sessions_at_baseline(gen):
    """`ho_stall_here` 가 꺼져 있으면(정답 셀 밖에서 연 세션) 전환 없는 세션은 변함이 없다."""
    b_single, _bm, _, i_wc = _counts(gen, None)
    n_single, _nm, _, _ = _counts(gen, _stall_nowhere)
    base = _marked(b_single, i_wc) / len(b_single)
    assert _marked(n_single, i_wc) / len(n_single) <= base * 1.3 + 0.01


# ── 구간 셀을 떠나는 전환 · 들어오는 전환 (F-604) ─────────────────────────────────

def _ho_classes(gen, T, mutate):
    """전환을 (떠난 셀이 구간, 도착 셀이 구간) 네 갈래로 나눠 센다 → {갈래: [전환 수, 창 닫힘 단 수, SGW 변경 수, down 늦음 수, SGW 변경 중 창 닫힘 단 수]}.

    `down 늦음` = SGW 가 바뀐 새 레코드의 down 첫 패킷이 up 보다 늦다 — 정체(공백)가 그 전환에 걸렸다는 관측이다.
    창 닫힘은 도착 줄(떠난 뒤 첫 줄)의 `DnlinkWindowCloseCount` 로 본다.
    """
    _r, runs, _ = _mobile_run(gen, 20, share=0.8, call_share=0.9, plan_mutate=mutate)
    idx = gen.index["transport"]
    seq, i_cell, i_wc = _ho_marks(runs, idx)
    i_sgw, i_up, i_dn = idx["SGW_ID"], idx["Traffic_Total_Uplink_FirstTime"], idx["Traffic_Total_Dnlink_FirstTime"]
    out: dict[tuple, list] = {}
    for rows in seq.values():
        for a, b in zip(rows, rows[1:]):
            ca, cb = tuple(a[i] for i in i_cell), tuple(b[i] for i in i_cell)
            if ca == cb:
                continue
            o = out.setdefault(("%s-%s" % ca in T, "%s-%s" % cb in T), [0, 0, 0, 0, 0])
            o[0] += 1
            o[1] += b[i_wc] not in ("0", "")
            if a[i_sgw] != b[i_sgw]:
                o[2] += 1
                o[3] += float(b[i_dn]) > float(b[i_up])
                o[4] += b[i_wc] not in ("0", "")           # SGW 가 바뀐 전환의 새 레코드 첫 줄
    return out


@pytest.fixture(scope="module")
def ho_classes(gen):
    """구간 셀 집합 T = 지나는 셀의 절반. 요청을 걸지 않은 실행과 건 실행을 견준다."""
    _r, runs, _ = _mobile_run(gen, 20, share=0.8, call_share=0.9, plan_mutate=None)
    idx = gen.index["transport"]
    cells = sorted({"%s-%s" % (r[idx["eNB_ID"]], r[idx["Cell_ID"]]) for m in runs for r in m["up"]["transport"]})
    T = frozenset(cells[::2])

    def mut(cell, plan):
        plan.ho_stall = True
        plan.ho_stall_cells = T
        return True

    return _ho_classes(gen, T, None), _ho_classes(gen, T, mut)


def _frac(o, k):
    return o[k] / max(o[0], 1)


def test_handover_leaving_a_target_cell_also_stalls(ho_classes):
    """구간 셀을 **떠나는** 전환에도 정체(down 공백)가 걸린다 — graphio EV-04g 는 전환을 떠난 셀에 귀속한다."""
    base, mut = ho_classes
    leave = mut[(True, False)]
    assert leave[2] > 100, leave
    assert leave[3] / leave[2] > 0.2, leave                         # 걸렸다(들어올 때만이면 0)
    assert base[(True, False)][3] == 0, base[(True, False)]        # 요청이 없으면 없다


def test_handover_leaving_a_target_cell_gets_no_window_close(ho_classes):
    """떠나는 전환은 창 닫힘을 달지 않는다 — 밖 셀의 U-07 이 오르지 않게. 들어오는 전환에서 낸 것보다 훨씬 적다."""
    base, mut = ho_classes
    leave, enter = mut[(True, False)], mut[(False, True)]
    assert _frac(enter, 1) > 4 * max(_frac(base[(False, True)], 1), 0.01)
    assert _frac(leave, 1) < _frac(enter, 1) / 3, (_frac(leave, 1), _frac(enter, 1))
    # SGW 가 바뀌는 전환은 새 레코드 상수의 창 닫힘이 따로 넘어올 수 있다(F-604 · 코리도에서 연 세션) — 기준선 수준이어야 한다
    b = base[(True, False)]
    leave_sgw, base_sgw = leave[4] / max(leave[2], 1), b[4] / max(b[2], 1)
    assert leave_sgw <= base_sgw * 2 + 0.01, (leave_sgw, base_sgw)


def test_handover_entering_a_target_cell_keeps_stall_and_window_close(ho_classes):
    base, mut = ho_classes
    for k in ((False, True), (True, True)):
        o = mut[k]
        assert o[3] / o[2] > 0.2, (k, o)
        assert _frac(o, 1) > 4 * max(_frac(base[k], 1), 0.01), (k, _frac(o, 1), _frac(base[k], 1))


def test_handover_between_cells_outside_the_target_never_stalls(ho_classes):
    """구간 밖끼리의 전환에는 정체도 창 닫힘도 없다 — 요청은 남겨 두었다가 구간 셀이 낀 전환에 쓴다."""
    base, mut = ho_classes
    o = mut[(False, False)]
    assert o[0] > 100 and o[3] == 0, o
    assert _frac(o, 1) <= _frac(base[(False, False)], 1) * 1.3 + 0.01


# ── 장애 전에 연 세션도 구간을 처음 만나는 전환에서 멈춘다 (F-606 · `ho_fault`) ─────────────

def _ho_fault_run(gen, ho_fault, mutate=None, minutes=20):
    """`_mobile_run` 과 같지만 엔진에 `ho_fault`(이 분 활성 3번 장애의 (구간 셀, 몫))를 넣는다. 세션을 열 때 요청이 없는 실행이면
    모든 세션이 「장애 전에 연」 세션이다."""
    import random as _r
    from datetime import timedelta
    rng = _r.Random(5)
    topo = HO.Topology(gen.cells)
    routes = HO.build_routes(topo, gen.cells, 10, rng, length=8)
    gen.seat_routes(routes, 0.8, rng)
    gen.mobility_share = 0.9
    try:
        eng = SES.SessionEngine(gen, RATES, SES.Shape(handover=0.0), seed=20260903)
        eng.ho_fault = ho_fault
        out = []
        for m in range(minutes):
            ts = ANCHOR + timedelta(minutes=m)
            gen.advance_routes(ts.timestamp())
            calls, _rows = _calls(gen, eng, ts)
            out.append({"ts": ts, "up": eng.minute(ts, calls, None, mutate)})
        return out
    finally:
        gen.seat_routes([], 0.0, rng)
        gen.mobility_share = 0.0


def _stall_stats(gen, runs, T):
    """SGW 가 바뀐 전환을 (구간 셀이 낀 전환 · 안 낀 전환) 으로 나눠 센다 → ((전환 수, down 늦음 수) × 2, 세션당 down 늦음 최대)."""
    idx = gen.index["transport"]
    seq, i_cell, _w = _ho_marks(runs, idx)
    i_sgw, i_up, i_dn = idx["SGW_ID"], idx["Traffic_Total_Uplink_FirstTime"], idx["Traffic_Total_Dnlink_FirstTime"]
    touch, other, per_session = [0, 0], [0, 0], 0
    for rows in seq.values():
        n = 0
        for a, b in zip(rows, rows[1:]):
            ca, cb = tuple(a[i] for i in i_cell), tuple(b[i] for i in i_cell)
            if ca == cb or a[i_sgw] == b[i_sgw]:
                continue
            late = float(b[i_dn]) > float(b[i_up])
            o = touch if ("%s-%s" % ca in T or "%s-%s" % cb in T) else other
            o[0] += 1
            o[1] += late
            n += late
        per_session = max(per_session, n)
    return touch, other, per_session


@pytest.fixture(scope="module")
def pre_fault_cells(gen):
    runs = _ho_fault_run(gen, None)
    idx = gen.index["transport"]
    cells = sorted({"%s-%s" % (r[idx["eNB_ID"]], r[idx["Cell_ID"]]) for m in runs for r in m["up"]["transport"]})
    return frozenset(cells[::2])


def test_session_opened_before_the_fault_stalls_where_it_first_meets_the_target(gen, pre_fault_cells):
    """장애 전에 연 세션(열 때 요청이 없던 세션)도 장애 중 구간 셀이 낀 전환에서 같은 몫으로 멈춘다."""
    T = pre_fault_cells
    b_touch, _b_other, _ = _stall_stats(gen, _ho_fault_run(gen, None), T)
    touch, other, per_session = _stall_stats(gen, _ho_fault_run(gen, (T, 0.9)), T)
    assert b_touch[1] == 0 and touch[0] > 100, (b_touch, touch)
    assert touch[1] / touch[0] > 0.1, touch                      # 걸렸다 — 수정 전에는 0
    assert other[1] == 0 and other[0] > 100, other                 # 구간 셀이 안 낀 전환에는 정체가 없다
    assert per_session <= 1, per_session                           # 세션당 한 번


def test_the_share_scales_the_stall(gen, pre_fault_cells):
    T = pre_fault_cells
    hi = _stall_stats(gen, _ho_fault_run(gen, (T, 0.9)), T)[0]
    lo = _stall_stats(gen, _ho_fault_run(gen, (T, 0.3)), T)[0]
    assert lo[1] / lo[0] < hi[1] / hi[0] * 0.6, (lo, hi)


def _opened_without_stall(cell, plan):
    """장애 중에 열린 세션 — 열 때 요청을 굴렸고(`plan_mutate` 가 불렸다) 몫에서 빠졌다(정체 없음)."""
    plan.ho_stall = False
    return True


def test_session_opened_during_the_fault_is_not_rolled_again(gen, pre_fault_cells):
    """열 때 이미 정한 세션(`ho_decided`)은 전환마다 다시 굴리지 않는다 — 열 때 몫에서 빠졌으면 끝까지 안 멈춘다."""
    T = pre_fault_cells
    touch, other, _ = _stall_stats(gen, _ho_fault_run(gen, (T, 0.9), mutate=_opened_without_stall), T)
    assert touch[0] > 100
    assert touch[1] == 0 and other[1] == 0, (touch, other)


def test_without_a_fault_nothing_changes_for_the_open_sessions(gen, pre_fault_cells):
    T = pre_fault_cells
    base = _stall_stats(gen, _ho_fault_run(gen, None), T)
    assert base[0][1] == 0 and base[1][1] == 0, base


def _fake_ho(kind="ho_interruption", series=("transport",), cells=("1-0", "2-0"), inten=0.5):
    return SimpleNamespace(kind=kind, series=series, active_cells_cache=set(cells),
                           intensity_for=lambda s, m, v=inten: v)


def test_ho_fault_cells_returns_cells_and_share(monkeypatch):
    assert S.HO_STALL_SHARE == 0.9
    cells, share = S.ho_fault_cells([_fake_ho(inten=0.5)], 3)
    assert cells == frozenset({"1-0", "2-0"}) and share == pytest.approx(0.5 * S.HO_STALL_SHARE)


def test_ho_fault_cells_ignores_other_faults_and_empty_ones():
    assert S.ho_fault_cells([], 3) is None
    assert S.ho_fault_cells([_fake_ho(kind="ran_drop_surge")], 3) is None, "3번이 아닌 장애는 무시한다"
    assert S.ho_fault_cells([_fake_ho(series=("cplane",))], 3) is None, "transport 계열이 아니면 무시한다"
    assert S.ho_fault_cells([_fake_ho(cells=())], 3) is None, "구간 셀이 없으면 None"
    assert S.ho_fault_cells([_fake_ho(inten=0.0)], 3) is None, "강도가 0 이면 멈출 몫이 없다"


def test_ho_fault_cells_unions_cells_and_takes_the_larger_share():
    a, b = _fake_ho(cells=("1-0",), inten=0.4), _fake_ho(cells=("2-0",), inten=0.8)
    cells, share = S.ho_fault_cells([a, _fake_ho(kind="x"), b], 3)
    assert cells == frozenset({"1-0", "2-0"}) and share == pytest.approx(0.8 * S.HO_STALL_SHARE)


def test_open_time_roll_and_pre_fault_roll_use_the_same_share():
    """`_ho_interruption`(열 때)이 쓰는 몫과 `ho_fault_cells`(장애 전에 연 세션)가 같은 상수다."""
    import inspect
    assert "HO_STALL_SHARE" in inspect.getsource(S._ho_interruption)
    assert "HO_STALL_SHARE" in inspect.getsource(S.ho_fault_cells)


# ── 노선 칸 · 넘어올 수 있는 셀 ───────────────────────────────────────────────

def _route_of(keys, loop=False):
    cells = tuple(_rcell(k, 35.68, 139.70 + i * 0.003) for i, k in enumerate(keys))
    return HO.Route("T", "T", cells, tuple([60] * len(cells)), loop=loop,
                    station_of=tuple(f"S{i // 3}" for i in range(len(cells))),
                    stations={f"S{i}": f"역{i}" for i in range(len(cells) // 3 + 1)})


def test_enterable_cells_are_those_with_a_different_neighbour():
    rt = _route_of(["1-0", "2-0", "2-0", "2-0", "3-0"])
    assert S._route_enterable(rt) == {"1-0", "2-0", "3-0"}


def test_a_run_of_the_same_cell_only_is_not_enterable():
    assert S._route_enterable(_route_of(["9-0", "9-0", "9-0"])) == set()


def test_loop_route_joins_its_ends():
    rt = _route_of(["1-0", "2-0", "1-0"], loop=True)
    assert S._route_enterable(rt) == {"1-0", "2-0"}
    assert S._route_enterable(_route_of(["5-0", "5-0", "5-0"], loop=True)) == set()


# ── 8번 · 호 많은 eNB 군집 ────────────────────────────────────────────────────

def _c(enb, n, lat, lon):
    return Cell(cell_key=f"{enb}-{n}", enb_id=str(enb), cell_id=n, enb_seq=0, c_uid="", tac=100, plmn="44010",
                vlan_id="", latitude=lat, longitude=lon, adm_sido_cd="13", adm_sido_nm="", adm_sgg_cd="A",
                adm_sgg_nm="", adm_emd_cd="A001", adm_emd_nm="", geo_status="MAPPED", rat="EUTRAN",
                mme_id="0001", sgw_id="0001", pgw_id="0001", ims_pgw_id="0001", traffic_weight=1.0, display_name="")


def _town(enbs, per=12, gap_deg=0.5):
    """eNB 마다 `gap_deg`(약 50km) 떨어뜨리고 셀을 12개씩 둔다 — `enb_cluster`(10~15셀)가 eNB 하나에서 멈춰 eNB 하나가 곧 군집 하나다."""
    cells = []
    for i, e in enumerate(enbs):
        cells += [_c(e, n, 35.0 + gap_deg * i, 139.0 + 0.0001 * n) for n in range(per)]
    return cells


def _rate(success):
    r = pemdb1.EnbRate({str(k): 1.0 for k in success})        # λ 가 있어야 실제 결과처럼 참(truthy)이다
    r.success.update({str(k): float(v) for k, v in success.items()})
    return r


def test_anchor_is_the_busiest_enb():
    cells = _town([101, 102, 103, 104])
    t = faults._drop_surge_target(cells, _rate({101: 1.0, 102: 40.0, 103: 7.0, 104: 3.0}))
    assert t is not None and {k.split("-")[0] for k in t.cell_keys} == {"102"}


def test_used_clusters_are_skipped_busiest_first():
    cells = _town([101, 102, 103, 104])
    rate = _rate({101: 1.0, 102: 40.0, 103: 7.0, 104: 6.0})
    first = faults._drop_surge_target(cells, rate)
    used = {faults.target_key(first.cell_keys): 5}
    second = faults._drop_surge_target(cells, rate, used)
    assert {k.split("-")[0] for k in second.cell_keys} == {"103"}, "다음으로 바쁜 eNB 로 내려간다"
    used[faults.target_key(second.cell_keys)] = 6
    third = faults._drop_surge_target(cells, rate, used)
    assert {k.split("-")[0] for k in third.cell_keys} == {"104"}


def _enbs_of(t):
    return {k.split("-")[0] for k in t.cell_keys}


def test_only_enbs_at_or_above_the_floor_become_candidates():
    """판정할 수 있는 eNB(C-04 분모율 ≥ 하한)만 후보다 — 한산한 eNB 는 앵커도 이웃도 되지 않는다."""
    floor = S.FAULT.drop_enb_min_rate
    cells = _town([101, 102, 103, 104])
    # 102 는 한산하다(하한 바로 아래). 이웃으로 붙지도 않아야 한다 — 가까이 둔 cells 로 일부러 한 군집 후보로 만든다
    near = [_c(e, n, 35.0 + 0.0001 * i, 139.0 + 0.0001 * n) for i, e in enumerate([201, 202, 203]) for n in range(5)]
    rate = _rate({201: floor + 6, 202: floor - 0.5, 203: floor + 1})
    t = faults._drop_surge_target(near, rate)
    assert t is not None and "202" not in _enbs_of(t), _enbs_of(t)
    assert _enbs_of(t) <= {"201", "203"}
    # 모든 회차에서 군집 안 eNB 는 하한 이상이다
    rate2 = _rate({101: floor - 1, 102: floor + 20, 103: floor, 104: floor - 0.1})
    used = {}
    for fid in range(1, 6):
        t = faults._drop_surge_target(cells, rate2, used)
        assert all(rate2.success[e] >= floor for e in _enbs_of(t)), (fid, _enbs_of(t))
        used[faults.target_key(t.cell_keys)] = fid


def test_floor_is_inclusive():
    """하한과 같은 eNB 도 후보다 — 앞 회차가 쓴 군집을 건너뛰면 하한 딱 그 eNB 로 내려간다."""
    floor = S.FAULT.drop_enb_min_rate
    cells = _town([101, 102, 103])
    rate = _rate({101: floor, 102: floor + 5, 103: floor - 0.01})
    first = faults._drop_surge_target(cells, rate)
    assert _enbs_of(first) == {"102"}
    second = faults._drop_surge_target(cells, rate, {faults.target_key(first.cell_keys): 1})
    assert _enbs_of(second) == {"101"}, "하한과 같은 eNB 는 후보다(103 은 하한 아래라 아니다)"
    third = faults._drop_surge_target(cells, rate, {faults.target_key(first.cell_keys): 1,
                                                    faults.target_key(second.cell_keys): 2})
    assert _enbs_of(third) == {"102"}, "후보를 다 썼으면 가장 오래전 것으로 돌아온다 — 103 으로 내려가지 않는다"


def test_without_enbs_over_the_floor_it_picks_without_a_floor():
    """새벽처럼 하한을 넘는 eNB 가 없으면 하한 없이 분모율이 있는 eNB 중 가장 바쁜 곳을 고른다."""
    floor = S.FAULT.drop_enb_min_rate
    cells = _town([101, 102, 103])
    t = faults._drop_surge_target(cells, _rate({101: floor / 8, 102: floor / 2, 103: 0.0}))
    assert t is not None and _enbs_of(t) == {"102"}


def test_enbs_without_any_rate_are_never_candidates_even_without_a_floor():
    cells = _town([101, 102])
    t = faults._drop_surge_target(cells, _rate({101: 0.0, 102: 0.0}))
    assert t is None or _enbs_of(t) <= set()      # 분모율이 0 인 eNB 는 어느 경로로도 고르지 않는다


def test_when_everything_was_used_the_oldest_cluster_comes_back():
    cells = _town([101, 102, 103])
    rate = _rate({101: 5.0, 102: 40.0, 103: 7.0})
    used = {}
    for fid, enb in ((9, "102"), (3, "103"), (7, "101")):
        used[faults.target_key({f"{enb}-{n}" for n in range(12)})] = fid
    t = faults._drop_surge_target(cells, rate, used)
    assert {k.split("-")[0] for k in t.cell_keys} == {"103"}, "가장 오래전(3번 회차)에 쓴 군집"


def test_enb_without_base_calls_never_becomes_an_anchor():
    cells = _town([101, 102])
    t = faults._drop_surge_target(cells, _rate({101: 12.0, 102: 0.0}), used={faults.target_key(
        {f"101-{n}" for n in range(12)}): 1})
    assert {k.split("-")[0] for k in t.cell_keys} == {"101"}, "분모율 0 인 eNB 는 안 고른다 — 쓴 군집이라도 다시 쓴다"


def test_no_success_rate_falls_back_to_the_old_path():
    cells = _town([101, 102])
    assert faults._drop_surge_target(cells, None) is None
    assert faults._drop_surge_target(cells, pemdb1.EnbRate()) is None
    assert faults._drop_surge_target(cells, _rate({})) is None
    assert faults._drop_surge_target(cells, _rate({101: 0.0, 102: 0.0})) is None


def test_only_ran_drop_surge_uses_the_busy_enb_branch():
    src = (SRC / "faults.py").read_text(encoding="utf-8")
    assert src.count("_drop_surge_target(") == 2          # 정의 1 + 호출 1
    assert 'fault["kind"] == "ran_drop_surge"' in src
    for other in ("_drop_surge_target(pick, _rate_at(enb_rate, fault), used)",):
        assert src.count(other) == 1


def test_enb_call_rate_keeps_its_old_values():
    """분모율은 따로 담는다 — `λ`(dict 값)와 `.mean` 은 eNB 정지·지진이 쓰므로 그대로다."""
    r = pemdb1.EnbRate({"1": 3.0})
    r.mean["1"] = 9.0
    assert r["1"] == 3.0 and r.mean["1"] == 9.0 and r.success == {}


# ── 주입 API · 곡선 경고 ─────────────────────────────────────────────────────

def _cfg(monkeypatch, diurnal="true", offset=0.0):
    vals = {"diurnal": diurnal, "virtual_offset_sec": offset}
    monkeypatch.setattr(faults_api, "get_config", lambda conn, key, default=None: vals.get(key, default))
    monkeypatch.setattr("datagen.ui.status_api._clock_defaults", lambda: ("true", "0"))


def test_curve_off_warns_for_a_rush_scenario(monkeypatch):
    _cfg(monkeypatch, diurnal="false")
    ko, ja = faults_api._rush_warnings(None, "ho_interruption", datetime(2026, 11, 24, 8, 30))
    assert ko and ja and "곡선이 꺼져" in ko[0] and "切れて" in ja[0]


def test_outside_rush_hours_warns(monkeypatch):
    _cfg(monkeypatch)
    ko, ja = faults_api._rush_warnings(None, "ho_interruption", datetime(2026, 11, 24, 12, 0))
    assert ko and "12:00" in ko[0] and ja and "12:00" in ja[0]


def test_inside_rush_hours_is_silent(monkeypatch):
    _cfg(monkeypatch)
    assert faults_api._rush_warnings(None, "ho_interruption", datetime(2026, 11, 24, 8, 30)) == ([], [])
    assert faults_api._rush_warnings(None, "ho_interruption", datetime(2026, 11, 24, 17, 0)) == ([], [])


def test_virtual_offset_decides_the_hour(monkeypatch):
    _cfg(monkeypatch, offset=-19349.0)            # 13:33 − 5시간 22분 ≈ 08:11
    assert faults_api._rush_warnings(None, "ho_interruption", datetime(2026, 11, 24, 13, 33)) == ([], [])
    _cfg(monkeypatch, offset=0.0)
    assert faults_api._rush_warnings(None, "ho_interruption", datetime(2026, 11, 24, 13, 33))[0]


def test_kinds_without_rush_hours_never_warn(monkeypatch):
    _cfg(monkeypatch, diurnal="false")
    assert faults_api._rush_warnings(None, "ran_drop_surge", datetime(2026, 11, 24, 3, 0)) == ([], [])


def test_rush_hours_end_is_exclusive(monkeypatch):
    _cfg(monkeypatch)
    assert faults_api._rush_warnings(None, "ho_interruption", datetime(2026, 11, 24, 9, 0))[0]
    assert faults_api._rush_warnings(None, "ho_interruption", datetime(2026, 11, 24, 6, 59))[0]
    assert faults_api._rush_warnings(None, "ho_interruption", datetime(2026, 11, 24, 7, 0)) == ([], [])


def test_the_api_only_warns_and_never_blocks():
    src = (SRC / "ui" / "faults_api.py").read_text(encoding="utf-8")
    body = src[src.index("def _rush_warnings"):src.index('@router.post("/api/faults/{fault_id}/cancel")')]
    assert "raise" not in body, "경고만 한다 — 주입을 막지 않는다"
    assert '"warnings": warn' in src and '"warnings_ja": warn_ja' in src
