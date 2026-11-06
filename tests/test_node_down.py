"""노드 다운 시나리오 — 장비가 죽으면 그 장비로 **아무것도** 들어오지 않는다 (2026-11-04 사용자 결정).

전에는 `gw_node_down` 이 새 접속의 90% 에만 실패를 입히고 이미 열린 세션은 정상 기록을 계속 냈다
("조금 아픈 SGW"). 이 파일이 지키는 것은 —

1. **다운 집합**(`scenarios.node_down_set`): 창 안이면 SGW·PGW 다운, eNB 는 단절 구간(≥0.5)부터. '0016' 과 16 이
   같은 ID. 창이 끝나면 빠진다(복귀). **강도는 다운 여부만 가른다.**
2. **새 호**(`node_down_gate`): 세션 설정 호는 실패로만 · Paging·HO·Reroute 는 버린다 · TAU·Detach 는 남긴다 ·
   다른 SGW 는 그대로.
3. **열린 세션**(`SessionEngine.cut`): 끊긴 분이 마지막이다. FIN 0 · 남은 HTTP 0 · 다음 분 레코드 없음.
4. **통합**(`PemdbGen`): 장애 중 다운 SGW 아래 성공 호 0 · 절단 레코드(Cause 504) 가입자당 한 건 · 끝난 뒤 복귀.

**DB 를 쓰지 않는다** — `test_capacity_shift.py` 와 같은 하니스(셀 마스터를 합성으로 바꿔 끼운다).
"""

from __future__ import annotations

import collections
import random
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from datagen import pemdb1 as P
from datagen import scenarios as S
from datagen import session as SE
from datagen.cells import Cell

ROOT = Path(__file__).resolve().parents[1]
T0 = datetime(2026, 11, 4, 16, 0)
DOWN_SGW, LIVE_SGW = "0016", "0037"


# --------------------------------------------------------------------------
# 합성 셀 · 시나리오
# --------------------------------------------------------------------------


def _cell(enb: str, n: int, sgw: str, pgw: str = "0037") -> Cell:
    return Cell(
        cell_key=f"{enb}-{n}", enb_id=enb, cell_id=n, enb_seq=0, c_uid="",
        tac=100, plmn="44010", vlan_id="", latitude=35.69 + int(enb[-2:]) * 0.002,
        longitude=139.70 + int(enb[-2:]) * 0.002, adm_sido_cd="11", adm_sido_nm="",
        adm_sgg_cd="11200", adm_sgg_nm="", adm_emd_cd="11200011", adm_emd_nm="",
        geo_status="MAPPED", rat="EUTRAN", mme_id="0001", sgw_id=sgw, pgw_id=pgw,
        ims_pgw_id=pgw, traffic_weight=1.0, display_name="")


def _master() -> list[Cell]:
    """eNB 20개는 다운될 SGW(0016) 아래, 20개는 살아 있는 SGW(0037) 아래."""
    out = []
    for e in range(40):
        sgw = DOWN_SGW if e < 20 else LIVE_SGW
        out += [_cell(f"{500000 + e}", n, sgw) for n in range(3)]
    return out


def _sc(kind: str, *, start: int = 5, duration: int = 8, sgw=None, pgw=None, enb=None,
        shape: str = "STEP", peak: float = 1.0, **kw) -> S.Scenario:
    return S.Scenario(
        id=f"T-{kind}", kind=kind, title=kind, truth_code="X", truth_category="X", expect_rules=(),
        expect_geo="WIDE", start_minute=start, duration=duration, shape=shape, peak=peak,
        onset="instant", offset="instant",
        target=S.Target(sgw_id=sgw, pgw_id=pgw, enb_id=enb), **kw)


# --------------------------------------------------------------------------
# 1. 다운 집합
# --------------------------------------------------------------------------


def test_sgw_is_down_exactly_inside_its_window_and_comes_back():
    sc = _sc("gw_node_down", start=5, duration=8, sgw="0016")
    for m in range(0, 20):
        down = S.node_down_set([sc], m)
        inside = 5 <= m < 13
        assert ("16" in down["SGW"]) is inside, m
        assert not down["PGW"] and not down["ENB"]
    assert S.node_down_set([sc], 5)["SGW"]["16"] == 5        # 값은 다운 시작 분


def test_ids_are_folded_so_0016_and_16_are_the_same_node():
    assert S.node_key("0016") == S.node_key(16) == S.node_key("16") == "16"
    assert S.node_key("abc") == "abc"
    a = S.node_down_set([_sc("gw_node_down", sgw="0016")], 6)
    b = S.node_down_set([_sc("gw_node_down", sgw=16)], 6)
    assert a == b and list(a["SGW"]) == ["16"]
    cell = SimpleNamespace(sgw_id="0016", pgw_id="0037", enb_id="1")
    assert S.down_of(a, cell=cell) == ("SGW", "16", 5)
    assert S.down_of(a, core=("0001", "16", "0037")) == ("SGW", "16", 5)      # 세션 코어는 숫자 표기가 달라도 같다


def test_pgw_restart_marks_only_the_pgw():
    down = S.node_down_set([_sc("gw_pgw_restart", pgw="0037")], 6)
    assert list(down["PGW"]) == ["37"] and not down["SGW"] and not down["ENB"]


def test_enb_is_down_from_the_outage_gate_not_from_the_precursor():
    """`enb_s1_outage` — 처음 2분 0.30 은 전조(S1AP 200)다. 단절 구간(≥ OUTAGE_GATE)부터 다운."""
    sc = _sc("enb_s1_outage", start=5, duration=10, enb="500001", shape="STEPS",
             steps=((2, 0.30), (10, 1.0)))
    assert S.OUTAGE_GATE == 0.5
    assert not S.node_down_set([sc], 5)["ENB"] and not S.node_down_set([sc], 6)["ENB"]
    assert S.node_down_set([sc], 7)["ENB"] == {"500001": 7}          # 다운 시작 분은 단절 첫 분
    assert S.node_down_set([sc], 14)["ENB"] == {"500001": 7}
    assert not S.node_down_set([sc], 15)["ENB"]                      # 창이 끝나면 복귀


def test_partial_failures_never_enter_the_down_set():
    """`partial_recovery`·`gw_resource`·`mme_overload` 는 의도된 부분 장애다."""
    for kind in ("partial_recovery", "gw_resource", "mme_overload"):
        sc = _sc(kind, sgw="0016")
        assert not any(S.node_down_set([sc], 6).values()), kind
    assert set(S.NODE_DOWN_KINDS) == {"gw_node_down", "gw_pgw_restart", "enb_s1_outage"}


def test_intensity_only_decides_down_or_not_not_a_failure_rate():
    full, low = _sc("gw_node_down", sgw="0016", peak=1.0), _sc("gw_node_down", sgw="0016", peak=0.2)
    assert S.node_down_set([full], 6) == S.node_down_set([low], 6)
    zero = _sc("gw_node_down", sgw="0016", peak=0.0)
    assert not any(S.node_down_set([zero], 6).values())               # 강도 0 이면 다운이 아니다


def test_overlapping_faults_keep_the_earliest_start():
    a, b = _sc("gw_node_down", start=5, duration=10, sgw="0016"), _sc("gw_node_down", start=8, duration=4, sgw="16")
    assert S.node_down_set([a, b], 9)["SGW"] == {"16": 5}


# --------------------------------------------------------------------------
# 2. 새 호 — node_down_gate
# --------------------------------------------------------------------------


@pytest.fixture
def pg():
    """합성 마스터로 뜬 `PemdbGen` — 테스트마다 새로 만든다(세션 상태가 새지 않게 · DB 없음)."""
    mp = pytest.MonkeyPatch()
    for f in ("pemdb1/_distributions.json", "profile.json"):
        if not (ROOT / "data" / f).exists():
            pytest.fail(f"data/{f} 가 없다 — 실측 분포 없이는 생성기를 띄울 수 없다")
    mp.setattr(P, "load_cells", lambda *a, **k: _master())
    mp.setattr(P, "load_apps", lambda conn: None)
    g = P.PemdbGen(None, ROOT / "data", rates={"lte_call": 1500, "transport": 1500},
                   seed=7, subscriber_pool=2500)
    yield g
    mp.undo()


def _rec(pg, ts: datetime, *, sgw_cells: bool, call_type: str, ok: bool = True):
    """성공 호 하나 — 다운 SGW 아래 셀이거나 살아 있는 SGW 아래 셀."""
    slot = ts.timestamp()
    for _ in range(400):
        rec, cell = pg.gen.base_lte_call(ts, slot)
        if (cell.sgw_id == DOWN_SGW) is sgw_cells:
            rec.set("call_type", call_type)
            rec.set("attempt_flag", "1")
            rec.set("success_flag", "1" if ok else "0")
            return rec, cell
    pytest.fail("원하는 SGW 의 셀을 고르지 못했다")


def _down(sgw=DOWN_SGW):
    return S.node_down_set([_sc("gw_node_down", sgw=sgw)], 6)


@pytest.mark.parametrize("ct", sorted(S.CT_SESSION_SETUP))
def test_session_setup_call_under_a_down_sgw_is_never_a_success(pg, ct):
    """다운 SGW 아래 세션 설정 호 — 실패이거나 버려진다. 성공으로 새지 않는다."""
    rng = random.Random(1)
    rec, cell = _rec(pg, T0, sgw_cells=True, call_type=ct)
    kept = S.node_down_gate(rec, cell, _down(), rng)
    assert (not kept) or str(rec.get("success_flag")) not in ("1", "True"), ct


def test_gate_turns_a_normal_setup_into_an_s11_failure_when_the_call_type_has_a_spec(pg):
    rec, cell = _rec(pg, T0, sgw_cells=True, call_type="1")
    assert S.node_down_gate(rec, cell, _down(), random.Random(2)) is True
    assert str(rec.get("success_flag")) == "0" and rec.get("attempt_flag") == "1"


@pytest.mark.parametrize("ct", sorted(S.DOWN_DROP_CALL_TYPES))
def test_paging_ho_and_reroute_are_dropped(pg, ct):
    rec, cell = _rec(pg, T0, sgw_cells=True, call_type=ct)
    assert S.node_down_gate(rec, cell, _down(), random.Random(3)) is False
    assert S.DOWN_DROP_CALL_TYPES == {"6", "10", "11"}


@pytest.mark.parametrize("ct", ["5", "9"])
def test_tau_and_detach_are_kept_untouched(pg, ct):
    rec, cell = _rec(pg, T0, sgw_cells=True, call_type=ct)
    before = list(rec.values)
    assert S.node_down_gate(rec, cell, _down(), random.Random(4)) is True
    assert rec.values == before


@pytest.mark.parametrize("ct", ["1", "3", "5", "6", "10"])
def test_cells_under_another_sgw_are_untouched(pg, ct):
    rec, cell = _rec(pg, T0, sgw_cells=False, call_type=ct)
    before = list(rec.values)
    assert S.node_down_gate(rec, cell, _down(), random.Random(5)) is True
    assert rec.values == before


def test_pgw_down_fails_setup_with_s11_72_and_leaves_enb_down_to_the_suppressor(pg):
    down = S.node_down_set([_sc("gw_pgw_restart", pgw="0037")], 6)
    rec, cell = _rec(pg, T0, sgw_cells=False, call_type="1")          # PGW 0037 은 전 셀이 지난다
    assert S.node_down_gate(rec, cell, down, random.Random(6)) is True
    assert str(rec.get("success_flag")) == "0"
    enb = S.node_down_set([_sc("enb_s1_outage", enb="500001", shape="STEPS", steps=((10, 1.0),))], 6)
    rec2, cell2 = _rec(pg, T0, sgw_cells=True, call_type="1")
    before = list(rec2.values)
    assert S.node_down_gate(rec2, cell2, enb, random.Random(6)) is True    # eNB 는 억제기 몫 — 여기서 건드리지 않는다
    assert rec2.values == before


# --------------------------------------------------------------------------
# 3. 열린 세션 — SessionEngine.cut
# --------------------------------------------------------------------------


def _engine_with_sessions(pg, minutes: int = 5):
    eng = pg.sessions
    pg.anchor = T0
    for m in range(minutes):
        pg.records(m, T0 + timedelta(minutes=m))
        pg.uplane_records(m, T0 + timedelta(minutes=m))
    return eng


def test_cut_marks_the_session_strips_fin_and_http_and_is_idempotent(pg):
    eng = _engine_with_sessions(pg)
    s = next(x for x in eng.active if x.cut_t < 0)
    assert s.cut_t == -1.0
    eng.cut(s, 123.0)
    assert s.cut_t == 123.0 and s.http_left == 0
    pos = {f: i for i, f in enumerate(SE.TRANSPORT_CONST)}
    assert s.const[pos["FinCount"]] == "0"                             # 정상 종료가 아니다
    eng.cut(s, 999.0)
    assert s.cut_t == 123.0                                            # 한 번만 끊긴다


def test_cut_sessions_end_this_minute_and_leave_no_records_afterwards(pg):
    eng = _engine_with_sessions(pg)
    cut_ids = set()
    for s in list(eng.active):
        eng.cut(s, T0.timestamp() + 5 * 60 + 30.0)
        cut_ids.add(id(s))
    assert cut_ids, "끊을 활성 세션이 없다"
    pg.records(5, T0 + timedelta(minutes=5))
    up = pg.uplane_records(5, T0 + timedelta(minutes=5))
    flags = collections.Counter(r[pg.gen.index["transport"]["OngoingFlag"]] for r in up["transport"])
    assert flags.get(SE.F_INTERIM, 0) == 0, "끊긴 세션이 Interim 으로 이어졌다 — 이번 분이 마지막이어야 한다"
    assert not (cut_ids & {id(s) for s in eng.active}), "끊긴 세션이 다음 분으로 이월됐다"


# --------------------------------------------------------------------------
# 4. 통합 — 장애 중 · 끝난 뒤
# --------------------------------------------------------------------------


def _log_cleanup(pg) -> list[tuple[int, str]]:
    """`pemdb1._down_cut_rec`(MME 가 정리한 C-Plane 절단 기록)이 실제로 레코드를 낸 (분, 가입자) 목록.

    다운 시각을 가로지른 **연결 중 호**도 `drop_flag=1`·Cause 504 절단으로 남기 때문에(맥스 11-04 · `node_down_gate`),
    레코드 필드만으로는 정리 기록과 구분되지 않는다 — 호출을 감싸서 센다.
    """
    log: list[tuple[int, str]] = []
    orig = pg._down_cut_rec

    def wrapped(out, ts, slot_start, imsi, cell0, core, *extra):
        before = len(out)
        orig(out, ts, slot_start, imsi, cell0, core, *extra)       # 다운 시각 등 뒤에 붙는 인자도 그대로 넘긴다
        if len(out) > before:
            log.append((round((ts - T0).total_seconds() / 60), imsi))

    pg._down_cut_rec = wrapped
    return log


def _fault(kind="gw_node_down", start=5, dur=6, target=None):
    """`target=None` 이면 자동 선택(가장 바쁜 SGW) — `SGW:` 접두 대상은 해석되지 않는다."""
    return {"fault_id": 9, "kind": kind, "target": target, "start_ts": T0 + timedelta(minutes=start),
            "duration_min": dur, "intensity": 1.0}


def _run(pg, minutes: int, rows: list[dict]):
    """분마다 (C-Plane 행 목록, U-Plane 계열별)와 해석된 시나리오."""
    pg.anchor = T0
    pg.cleanup_log = _log_cleanup(pg)
    scs, bad = pg.set_fault_rows(rows)
    assert scs and not bad, f"장애가 해석되지 않았다: {bad}"
    out = []
    for m in range(minutes):
        ts = T0 + timedelta(minutes=m)
        out.append((pg.records(m, ts), pg.uplane_records(m, ts)))
    return out, scs[0]


def _cal(pg, rows, field):
    i = pg.gen.index["lte_call"][field]
    return [r[i] for r in rows]


def test_no_successful_call_or_new_session_under_the_down_sgw_and_it_recovers(pg):
    runs, sc = _run(pg, 14, [_fault(start=5, dur=6)])                  # 다운: 5 ≤ m < 11
    key = S.node_key(sc.target.sgw_id)
    lo, hi = sc.start_minute, sc.start_minute + sc.duration
    assert lo == 5 and hi == 11
    at = sc.start_ts
    ok_by_minute = []
    for m, (calls, up) in enumerate(runs):
        sgw = _cal(pg, calls, "SGW_ID")
        succ = _cal(pg, calls, "success_flag")
        drop = _cal(pg, calls, "drop_flag")
        ct = _cal(pg, calls, "call_type")
        # 절단 레코드(drop_flag=1)는 "연결됐다가 끊긴" 호라 success_flag 가 1 이다 — 새 접속 성공이 아니다
        first, last = _cal(pg, calls, "First_eNB_ID"), _cal(pg, calls, "Last_eNB_ID")
        # 핸드오버로 다른 eNB 에서 끝나는 호(First≠Last)는 `test_handover_into_...` 가 따로 본다
        st = [float(x or 0) for x in _cal(pg, calls, "call_start_time")]
        # 다운 시각 **이후에 시작한** 호만 본다 — 그 전에 시작한 호는 새 규칙상 정상이다(끝 시각 문제는 xfail 테스트가 본다)
        ok = sum(1 for s, f, d, c, a, b, t0 in zip(sgw, succ, drop, ct, first, last, st)
                 if S.node_key(s) == key and f in ("1", "True") and d not in ("1", "True")
                 and c in S.CT_SESSION_SETUP and a == b and t0 >= at)
        ok_any = sum(1 for s, f, d, c in zip(sgw, succ, drop, ct)
                     if S.node_key(s) == key and f in ("1", "True") and d not in ("1", "True") and c in S.CT_SESSION_SETUP)
        ok_by_minute.append(ok_any)                                      # 장애 전·복귀 확인용(시작 시각 필터 없음)
        if lo <= m < hi:
            assert ok == 0, f"{m}분: 다운 SGW 아래 성공 호 {ok}건"
            dropped = sum(1 for s, c, t0 in zip(sgw, ct, st)
                          if S.node_key(s) == key and c in S.DOWN_DROP_CALL_TYPES and t0 >= at)
            assert dropped == 0, f"{m}분: 다운 SGW 아래 Paging·HO·Reroute 가 남았다"
    assert sum(ok_by_minute[:lo]) > 0, "장애 전에는 그 SGW 에도 성공 호가 있어야 한다"
    assert sum(ok_by_minute[hi:]) > 0, "장애가 끝난 뒤 복귀하지 않았다"
    # 시도 자체는 남는다 — 실패 시도까지 없애면 장애가 아니라 트래픽 소멸이다
    attempts = sum(1 for m in range(lo, hi) for s, c in zip(_cal(pg, runs[m][0], "SGW_ID"),
                                                            _cal(pg, runs[m][0], "call_type"))
                   if S.node_key(s) == key and c in S.CT_SESSION_SETUP)
    assert attempts > 0, "접속 시도가 전부 사라졌다 — 실패로 남아야 한다"


def test_handover_into_a_down_sgw_is_not_recorded_as_a_success(pg):
    """살아 있는 SGW 에서 시작해 다운 SGW 아래 eNB 에서 끝나는 호(First≠Last)가 성공으로 남지 않는다."""
    runs, sc = _run(pg, 14, [_fault(start=5, dur=6)])
    key = S.node_key(sc.target.sgw_id)
    bad = 0
    for m in range(sc.start_minute, sc.start_minute + sc.duration):
        calls = runs[m][0]
        bad += sum(1 for s, f, d, c, t0 in zip(_cal(pg, calls, "SGW_ID"), _cal(pg, calls, "success_flag"),
                                               _cal(pg, calls, "drop_flag"), _cal(pg, calls, "call_type"),
                                               _cal(pg, calls, "call_start_time"))
                   if S.node_key(s) == key and f in ("1", "True") and d not in ("1", "True") and c in S.CT_SESSION_SETUP
                   and float(t0 or 0) >= sc.start_ts)
    assert bad == 0, f"다운 SGW 를 SGW_ID 로 가진 성공 호 {bad}건"


def test_open_sessions_are_cut_within_two_minutes_with_cause_504_once_per_subscriber(pg):
    runs, sc = _run(pg, 9, [_fault(start=5, dur=6)])
    log = pg.cleanup_log
    assert pg.DOWN_CUT_CAUSE == 504 and pg.DOWN_CUT_MINUTES == 2
    assert log, "다운 시작 직후 C-Plane 정리 기록이 없다"
    assert {m for m, _ in log} == {6, 7}, "세션을 끊은 분(5)의 다음 두 분에 나눠 남아야 한다"
    imsis = [i for _, i in log]
    assert len(imsis) == len(set(imsis)), "가입자당 한 건이어야 한다"
    first = sum(1 for m, _ in log if m == 6)
    second = sum(1 for m, _ in log if m == 7)
    assert 0.25 < first / (first + second) < 0.75, (first, second)      # 약 절반씩


def test_no_uplane_continuation_for_cut_sessions_under_the_down_sgw(pg):
    """다운 첫 분 다음 분부터 다운 SGW 를 지나는 U-Plane 레코드는 없다(새 세션도 못 열린다)."""
    runs, sc = _run(pg, 11, [_fault(start=5, dur=6)])
    key = S.node_key(sc.target.sgw_id)
    i = pg.gen.index["transport"]
    for m in range(6, 11):                                              # 다운 첫 분(5) 다음 분부터 — 데이터는 첫 분에 멈춘다
        rows = runs[m][1].get("transport", [])
        under = [r for r in rows if S.node_key(r[i["SGW_ID"]]) == key]
        assert not under, f"{m}분: 다운 SGW 를 지나는 U-Plane 레코드 {len(under)}건"
    before = [r for r in runs[4][1].get("transport", []) if S.node_key(r[i["SGW_ID"]]) == key]
    assert before, "장애 전에는 그 SGW 의 U-Plane 이 있어야 한다"


def test_enb_outage_stops_every_new_call_in_the_dead_enb(pg):
    enb = "500025"
    runs, sc = _run(pg, 40, [_fault(kind="enb_s1_outage", start=5, dur=8, target=f"ENB:{enb}")])
    ci = pg.gen.index["lte_call"]["First_eNB_ID"]
    assert sum(1 for r in runs[4][0] if r[ci] == enb) > 0, "장애 전에는 그 eNB 에도 호가 있어야 한다"
    # 강도가 단절 문턱(0.5) 이상인 분만 다운이다 — 회복 구간(0.4)에는 eNB 가 돌아온다(스텝 파형)
    dead = [m for m in range(sc.start_minute, sc.start_minute + sc.duration) if sc.intensity(m) >= S.OUTAGE_GATE]
    assert dead, "단절 구간이 없다"
    for m in dead:
        n = sum(1 for r in runs[m][0] if r[ci] == enb)
        assert n == 0, f"{m}분: 다운 eNB 의 호 {n}건"
    assert sum(1 for m in range(max(dead) + 1, 40) for r in runs[m][0] if r[ci] == enb) > 0, "장애가 끝난 뒤 복귀하지 않았다"


# --------------------------------------------------------------------------
# 5. 핸드오버 — 다운 장비로 가는 전환은 처음부터 생기지 않는다 (맥스 수정 · pemdb1 `_baseline_handover`)
# --------------------------------------------------------------------------


def _enb_sgw(pg) -> dict[str, str]:
    return {c.enb_id: S.node_key(c.sgw_id) for c in pg.cells}


def test_no_handover_record_ends_under_a_down_sgw_or_down_enb(pg):
    """장애 중에는 First≠Last 호의 끝 eNB 가 다운 SGW 아래에 있지 않다 — 거르기가 아니라 생성 단계에서 막는다."""
    runs, sc = _run(pg, 14, [_fault(start=5, dur=6)])
    key = S.node_key(sc.target.sgw_id)
    sgw_of = _enb_sgw(pg)
    first_i, last_i = pg.gen.index["lte_call"]["First_eNB_ID"], pg.gen.index["lte_call"]["Last_eNB_ID"]
    sgw_i = pg.gen.index["lte_call"]["SGW_ID"]
    x2_ok = 0
    for m in range(sc.start_minute, sc.start_minute + sc.duration):
        moved = [r for r in runs[m][0]
                 if r[first_i] != r[last_i] and sgw_of.get(r[last_i]) == key and sgw_of.get(r[first_i]) != key]
        # S1 전환이면 코어가 상대 셀 것으로 바뀐다(SGW_ID = 다운 SGW) — 그런 호가 처음부터 없어야 한다.
        # X2 는 코어가 그대로라 SGW_ID 가 살아 있는 값이다 — 다운 SGW 지역의 셀로 가도 막지 않는다
        s1_into_down = [r for r in moved if S.node_key(r[sgw_i]) == key]
        assert not s1_into_down, f"{m}분: 살아 있는 SGW 에서 다운 SGW 로 넘어간 호 {len(s1_into_down)}건"
        x2_ok += len(moved) - len(s1_into_down)
    assert x2_ok > 0, "X2 는 그대로 생겨야 한다(코어가 살아 있는 값으로 유지)"


def test_calls_starting_next_to_the_dead_sgw_still_end_normally_on_the_live_side(pg):
    """출발 셀이 살아 있는 SGW 아래면 그 호는 정상으로 끝난다 — 전환만 없어졌다(실패로 섞이지 않는다)."""
    runs, sc = _run(pg, 14, [_fault(start=5, dur=6)])
    key = S.node_key(sc.target.sgw_id)
    sgw_of = _enb_sgw(pg)
    live = [e for e, k in sgw_of.items() if k != key]
    i = pg.gen.index["lte_call"]
    ok = fail = 0
    for m in range(sc.start_minute, sc.start_minute + sc.duration):
        for r in runs[m][0]:
            if r[i["First_eNB_ID"]] in live and r[i["call_type"]] in S.CT_SESSION_SETUP:
                if r[i["success_flag"]] in ("1", "True"):
                    ok += 1
                else:
                    fail += 1
    assert ok > 0 and fail <= ok * 0.2, (ok, fail)                  # 살아 있는 쪽 호가 실패로 무너지지 않았다


def test_x2_and_ordinary_handovers_among_live_cells_are_untouched(pg):
    """다운 SGW 와 무관한 전환(살아 있는 SGW 안)은 장애 중에도 그대로 생긴다."""
    runs, sc = _run(pg, 14, [_fault(start=5, dur=6)])
    key = S.node_key(sc.target.sgw_id)
    sgw_of = _enb_sgw(pg)
    i = pg.gen.index["lte_call"]
    during = sum(1 for m in range(sc.start_minute, sc.start_minute + sc.duration) for r in runs[m][0]
                 if r[i["First_eNB_ID"]] != r[i["Last_eNB_ID"]]
                 and sgw_of.get(r[i["First_eNB_ID"]]) != key and sgw_of.get(r[i["Last_eNB_ID"]]) != key)
    before = sum(1 for m in range(0, sc.start_minute) for r in runs[m][0]
                 if r[i["First_eNB_ID"]] != r[i["Last_eNB_ID"]]
                 and sgw_of.get(r[i["First_eNB_ID"]]) != key and sgw_of.get(r[i["Last_eNB_ID"]]) != key)
    assert before > 0 and during > 0, (before, during)


def test_handover_into_a_down_enb_is_not_generated(pg):
    enb = "500025"                                                    # 살아 있는 SGW 아래 eNB
    runs, sc = _run(pg, 30, [_fault(kind="enb_s1_outage", start=5, dur=8, target=f"ENB:{enb}")])
    i = pg.gen.index["lte_call"]
    dead = [m for m in range(sc.start_minute, sc.start_minute + sc.duration) if sc.intensity(m) >= S.OUTAGE_GATE]
    assert dead
    for m in dead:
        into = [r for r in runs[m][0] if r[i["Last_eNB_ID"]] == enb and r[i["First_eNB_ID"]] != enb]
        assert not into, f"{m}분: 다운 eNB 로 끝나는 전환 {len(into)}건"


# --------------------------------------------------------------------------
# 6. 절단 레코드의 Cause · 끊긴 세션의 누적 · 대상 표기
# --------------------------------------------------------------------------


def test_cut_records_carry_cause_504_and_count_as_a_drop_not_a_normal_release(pg):
    runs, sc = _run(pg, 9, [_fault(start=5, dur=6)])
    key = S.node_key(sc.target.sgw_id)
    i = pg.gen.index["lte_call"]
    cuts = [r for m in (5, 6) for r in runs[m][0]
            if r[i["drop_flag"]] in ("1", "True") and S.node_key(r[i["SGW_ID"]]) == key]
    assert cuts
    for r in cuts:
        assert r[i["UEContextRelease_Cause"]] == "504" and r[i["s1ap_error_Cause"]] == "504", r[i["UEContextRelease_Cause"]]
        # 정리 기록(call_type 3)과 다운 시각을 가로지른 연결 중 호의 절단(세션 설정 계열)이 함께 있다
        assert r[i["success_flag"]] in ("1", "True") and r[i["call_type"]] in S.CT_SESSION_SETUP


def test_cumulative_traffic_never_goes_backwards_for_sessions_cut_by_the_outage(pg):
    runs, sc = _run(pg, 10, [_fault(start=5, dur=6)])
    key = S.node_key(sc.target.sgw_id)
    t = pg.gen.index["transport"]
    cols = ("Traffic_Total_Uplink_PayloadSize", "Traffic_Total_Dnlink_PayloadSize")
    seen: dict[tuple, list[tuple[int, int, int]]] = collections.defaultdict(list)
    for m, (_, up) in enumerate(runs):
        for r in up.get("transport", []):
            if S.node_key(r[t["SGW_ID"]]) != key:
                continue
            # TEID 는 합성 값이 0 이라 키가 못 된다 — 세션 시작 시각(상향 FirstTime)이 세션을 가른다
            seen[(r[t["IMSI"]], r[t["Traffic_Total_Uplink_FirstTime"]])].append(
                (m, int(float(r[t[cols[0]]] or 0)), int(float(r[t[cols[1]]] or 0))))
    # 같은 가입자·같은 시작 시각이 한 분에 둘 이상이면 세션을 가를 수 없다 — 그 키는 빼고 본다
    multi = {k: v for k, v in seen.items()
             if len(v) >= 2 and len({m for m, _, _ in v}) == len(v)}
    assert multi, "여러 분에 걸친 세션이 없다"
    cut_ones = {k: v for k, v in multi.items() if v[-1][0] in (5, 6)}      # 장애 첫 두 분에 마지막 레코드가 난 세션
    assert cut_ones, "끊긴 세션을 찾지 못했다"
    for k, v in multi.items():
        for (_, u0, d0), (_, u1, d1) in zip(v, v[1:]):
            assert u1 >= u0 and d1 >= d0, f"누적이 거꾸로 갔다: {k} {v}"


@pytest.mark.parametrize("a,b", [("SGW-16", "SGW-0016"), ("sgw-16", "SGW-16")])
def test_sgw_16_and_sgw_0016_resolve_to_the_same_down_set(pg, a, b):
    """gen-ui 가 보내는 대상 표기(`SGW-<번호>`)는 0채움 여부와 무관하게 같은 장비다."""
    sets = []
    for tgt in (a, b):
        pg.anchor = T0
        scs, bad = pg.set_fault_rows([_fault(start=5, dur=6, target=tgt)])
        assert scs and not bad, (tgt, bad)
        sets.append(S.node_down_set(scs, 6))
    assert sets[0] == sets[1] and list(sets[0]["SGW"]) == ["16"]


# --------------------------------------------------------------------------
# 7. SGW·PGW 다운 — 데이터는 다운 시각에 멈추고, C-Plane 절단 기록만 나눠 남긴다 (사용자 지적 11-04)
# --------------------------------------------------------------------------


def test_every_session_through_the_down_sgw_is_cut_at_the_first_minute_start(pg, monkeypatch):
    """다운 첫 분에 그 SGW 를 `core` 로 가진 세션 **전부**가 분 시작 +0.5초에 끊긴다(둘째 분으로 미루지 않는다)."""
    pg.anchor = T0
    scs, bad = pg.set_fault_rows([_fault(start=5, dur=6)])
    assert scs and not bad
    key = S.node_key(scs[0].target.sgw_id)
    for m in range(5):
        pg.records(m, T0 + timedelta(minutes=m))
        pg.uplane_records(m, T0 + timedelta(minutes=m))
    victims = [s for s in pg.sessions.active if S.node_key(s.core[1]) == key]
    assert victims, "다운 SGW 를 지나는 열린 세션이 없다"
    start = (T0 + timedelta(minutes=5)).timestamp()
    pg.records(5, T0 + timedelta(minutes=5))
    pg.uplane_records(5, T0 + timedelta(minutes=5))
    for s in victims:
        assert s.cut_t == pytest.approx(start + 0.5), (s.sid, s.cut_t - start)
    # 다운 뒤 새로 열려 끊기지 않은 세션이 다운 SGW 를 지나지 않는다
    assert not [s for s in pg.sessions.active if S.node_key(s.core[1]) == key and s.cut_t < 0]


def test_uplane_is_zero_from_the_first_down_minute_and_the_end_record_barely_grows(pg):
    runs, sc = _run(pg, 12, [_fault(start=5, dur=6)])
    key = S.node_key(sc.target.sgw_id)
    t = pg.gen.index["transport"]
    flag, dl = t["OngoingFlag"], t["Traffic_Delta_Dnlink_PayloadSize"]

    def under(m, series="transport"):
        i = pg.gen.index[series]
        return [r for r in runs[m][1].get(series, []) if S.node_key(r[i["SGW_ID"]]) == key]

    normal = sum(float(r[dl] or 0) for r in under(4))
    first = under(5)
    assert normal > 0
    assert all(r[flag] in SE.COUNTED for r in first), "다운 첫 분 레코드는 모두 End 여야 한다"
    assert sum(float(r[dl] or 0) for r in first) < normal * 0.05, "다운 시각이 분 시작이라 증가분이 거의 없어야 한다"
    for series in ("transport", "dns", "http"):
        for m in range(6, 11):
            assert not under(m, series), f"{m}분: 다운 SGW 를 지나는 {series} 레코드가 남았다"


def test_cplane_cut_records_are_split_across_two_minutes_one_per_subscriber(pg):
    runs, sc = _run(pg, 9, [_fault(start=5, dur=6)])
    key = S.node_key(sc.target.sgw_id)
    log = pg.cleanup_log
    by_min = collections.Counter(m for m, _ in log)
    assert by_min[5] == 0, "세션을 끊은 분에는 C-Plane 정리 기록이 없다(MME 가 알아차리는 시차)"
    assert by_min[6] and by_min[7], "끊은 다음 두 분에 나눠 남아야 한다"
    assert by_min[8] == 0 and by_min[9] == 0, "둘째 분 뒤에는 정리 기록이 새로 생기지 않는다"
    imsis = [i for _, i in log]
    assert len(imsis) == len(set(imsis)), "같은 가입자의 정리 기록이 두 분에 중복됐다"
    # 데이터는 첫 분에 이미 멈췄다 — 둘째 분에 기록이 나는 가입자의 끊긴 세션은 둘째 분에 이어지지 않는다
    t = pg.gen.index["transport"]
    late = {i for m, i in log if m == 7}
    victims = {(r[t["IMSI"]], r[t["Traffic_Total_Uplink_FirstTime"]]) for r in runs[4][1].get("transport", [])
               if S.node_key(r[t["SGW_ID"]]) == key and r[t["IMSI"]] in late}
    assert victims and not [r for r in runs[7][1].get("transport", [])
                            if (r[t["IMSI"]], r[t["Traffic_Total_Uplink_FirstTime"]]) in victims]


def _ho_prep_rows(pg, runs, minutes):
    i = pg.gen.index["lte_call"]
    # 성공 호(success_flag=1)만 — 다운 SGW 셀의 S11 타임아웃 실패 호도 같은 106/0 을 들고 있다(별도 관찰)
    return [r for m in minutes for r in runs[m][0]
            if r[i["s1ap_error_Cause"]] == "106" and r[i["s1ap_error_Message"]] == "0"
            and r[i["success_flag"]] in ("1", "True")]


#: 전환 준비 실패는 다운 8분 동안 시드당 0~3건뿐인 희귀 사건이다(5G 수정으로 난수 흐름이 바뀐 뒤 시드 7 이 0건이 되었다).
#: 한 시드의 "1건 이상"에 기대면 시드 운이 단언을 정한다 — 세 시드를 합쳐 보고, 불변식은 모든 행에 건다
_PREP_SEEDS = (7, 8, 9)


def test_s1_handover_toward_the_down_sgw_is_recorded_as_a_preparation_failure(pg):
    total = 0
    for seed in _PREP_SEEDS:
        g = pg if seed == 7 else P.PemdbGen(None, ROOT / "data", rates={"lte_call": 1500, "transport": 1500},
                                            seed=seed, subscriber_pool=2500)
        runs, sc = _run(g, 16, [_fault(start=5, dur=8)])
        key = S.node_key(sc.target.sgw_id)
        i = g.gen.index["lte_call"]
        lo, hi = sc.start_minute, sc.start_minute + sc.duration
        assert not _ho_prep_rows(g, runs, range(0, lo)), f"시드 {seed}: 장애 전에는 전환 준비 실패 기록이 없어야 한다"
        rows = _ho_prep_rows(g, runs, range(lo, hi))
        total += len(rows)
        sgw_of = _enb_sgw(g)
        for r in rows:
            assert r[i["Last_eNB_ID"]] == r[i["First_eNB_ID"]], "단말은 출발 셀에 남아야 한다"
            assert S.node_key(r[i["SGW_ID"]]) != key, "SGW_ID 는 살아 있는 값이어야 한다"
            assert r[i["success_flag"]] in ("1", "True") and r[i["drop_flag"]] in ("0", "False", "")
            assert r[i["UEContextRelease_Cause"]] != "106" and r[i["UEContextRelease_Cause"]] not in S_HO_CAUSES_AS_STR(), \
                "해제 Cause 가 전환 계열로 남았다 — 120 이어야 한다"
            for f in ("first_error_interface_protocol", "first_error_message", "first_error_time", "first_error_cause"):
                assert r[i[f]] in ("", "0", "0.0", "0.000000", None), (f, r[i[f]])
            assert sgw_of.get(r[i["First_eNB_ID"]]) != key
    assert total, f"시드 {_PREP_SEEDS} 합쳐도 다운 SGW 로 가려던 S1 전환이 실패로 기록되지 않았다"


def S_HO_CAUSES_AS_STR() -> set[str]:
    from datagen import handover as HO
    return {str(c) for c in HO.HANDOVER_CAUSES}


def test_a_cut_subscriber_reappears_as_a_new_session_not_the_old_one(pg):
    """끊긴 세션은 이어지지 않고, 그 가입자는 새 세션으로 다시 나타난다 — 살아 있는 SGW 에서도, 장애가 끝난 뒤
    SGW-16 아래에서도. (한 가입자가 세션을 여럿 가질 수 있어 가입자가 아니라 **세션**(가입자+시작 시각)으로 본다.)"""
    runs, sc = _run(pg, 18, [_fault(start=5, dur=6)])
    key = S.node_key(sc.target.sgw_id)
    t = pg.gen.index["transport"]
    sess = lambda r: (r[t["IMSI"]], r[t["Traffic_Total_Uplink_FirstTime"]])
    victims = {sess(r) for r in runs[4][1].get("transport", []) if S.node_key(r[t["SGW_ID"]]) == key}
    assert victims, "다운 직전 SGW-16 의 세션이 없다"
    victim_imsis = {k[0] for k in victims}
    start = (T0 + timedelta(minutes=5)).timestamp()
    end = (T0 + timedelta(minutes=sc.start_minute + sc.duration)).timestamp()
    live_new = after_end = 0
    for m in range(6, 18):
        for r in runs[m][1].get("transport", []):
            assert sess(r) not in victims, f"{m}분: 끊긴 세션이 이어졌다"
            if r[t["IMSI"]] not in victim_imsis:
                continue
            first = float(r[t["Traffic_Total_Uplink_FirstTime"]])
            if S.node_key(r[t["SGW_ID"]]) != key and first >= start:
                live_new += 1
            elif S.node_key(r[t["SGW_ID"]]) == key:
                assert first >= end, f"{m}분: 장애 중에 시작한 세션이 다운 SGW 로 열렸다"
                after_end += 1
    assert live_new > 0, "끊긴 가입자가 살아 있는 SGW 에서 새 세션을 열지 않았다"
    assert after_end > 0, "장애가 끝난 뒤 SGW-16 아래에서 신규 접속으로 복귀하지 않았다"


def test_enb_outage_emits_no_504_cut_records(pg):
    """eNB 다운은 C-Plane 절단 레코드(504)를 내지 않는다. **세션을 끊지 않는다**(11-04 원칙 변경 — 데이터만 멈추고
    이웃 셀에서 같은 세션으로 이어진다)는 `test_disaster.py` 의 SC-17 테스트가 본다."""
    enb = "500025"
    runs, sc = _run(pg, 14, [_fault(kind="enb_s1_outage", start=5, dur=8, target=f"ENB:{enb}")])
    i = pg.gen.index["lte_call"]
    gate = next(m for m in range(sc.start_minute, sc.start_minute + sc.duration) if sc.intensity(m) >= S.OUTAGE_GATE)
    cuts = [r for m in range(gate, gate + 3) for r in runs[m][0]
            if r[i["UEContextRelease_Cause"]] == "504" and r[i["First_eNB_ID"]] == enb]
    assert not cuts, "eNB 다운은 C-Plane 절단 레코드를 내지 않는다"


# --------------------------------------------------------------------------
# 8. 106/msg 0(전환 준비 실패)이 붙는 범위 — 실패 호에는 없다 (맥스 수정 · `_baseline_handover`)
# --------------------------------------------------------------------------


def _prep_marker(r, i) -> bool:
    return r[i["s1ap_error_Cause"]] == "106" and r[i["s1ap_error_Message"]] == "0"


def test_s11_failure_calls_under_the_down_sgw_never_carry_the_handover_prep_marker(pg):
    runs, sc = _run(pg, 16, [_fault(start=5, dur=8)])
    key = S.node_key(sc.target.sgw_id)
    i = pg.gen.index["lte_call"]
    failed = [r for m in range(sc.start_minute, sc.start_minute + sc.duration) for r in runs[m][0]
              if S.node_key(r[i["SGW_ID"]]) == key and r[i["success_flag"]] not in ("1", "True")]
    assert failed, "다운 SGW 아래 실패 호가 없다"
    assert not [r for r in failed if _prep_marker(r, i)], "S11 실패 호에 106/msg 0 이 남았다"
    # 다른 사유의 s1ap 오류(301/msg 23 등)는 있어도 된다 — 106/0 조합만 본다


def test_calls_starting_under_the_down_sgw_have_no_handover(pg):
    """출발 셀이 다운 SGW 아래인 호는 옮겨 갈 베어러가 없다 — 전환이 없고 SGW_ID 는 출발 셀 값(다운 SGW)이다."""
    runs, sc = _run(pg, 16, [_fault(start=5, dur=8)])
    key = S.node_key(sc.target.sgw_id)
    sgw_of = _enb_sgw(pg)
    i = pg.gen.index["lte_call"]
    rows = [r for m in range(sc.start_minute, sc.start_minute + sc.duration) for r in runs[m][0]
            if sgw_of.get(r[i["First_eNB_ID"]]) == key and r[i["call_type"]] in S.CT_SESSION_SETUP
            and r[i["drop_flag"]] not in ("1", "True")]          # 절단 레코드(세션 코어 · 셀 이동 가능)는 제외
    assert rows
    for r in rows:
        assert r[i["Last_eNB_ID"]] == r[i["First_eNB_ID"]], "다운 SGW 셀에서 시작한 호가 다른 셀로 끝났다"
        assert S.node_key(r[i["SGW_ID"]]) == key
        assert not _prep_marker(r, i)


def test_handover_prep_marker_appears_only_on_connected_calls_toward_a_down_sgw(pg):
    runs, sc = _run(pg, 20, [_fault(start=5, dur=12)])
    key = S.node_key(sc.target.sgw_id)
    i = pg.gen.index["lte_call"]
    lo, hi = sc.start_minute, sc.start_minute + sc.duration
    marked = [(m, r) for m in range(len(runs)) for r in runs[m][0] if _prep_marker(r, i)]
    assert any(lo <= m < hi for m, _ in marked), "장애 중 전환 준비 실패가 한 건도 없다"
    for m, r in marked:
        assert lo <= m < hi, f"{m}분: 장애 밖에서 전환 준비 실패 표식이 붙었다"
        assert r[i["success_flag"]] in ("1", "True"), f"{m}분: 실패 호에 표식이 붙었다"
        assert S.node_key(r[i["SGW_ID"]]) != key, f"{m}분: SGW_ID 가 다운 SGW 다"


# --------------------------------------------------------------------------
# 9. 초 단위 다운 시각 (맥스 수정 11-04 · F-110) — 장애 시작을 분 중간(:18초)으로 둔다
# --------------------------------------------------------------------------

SEC = 18


def _fault_s(start=5, sec=SEC, dur=6, kind="gw_node_down", target=None):
    f = _fault(kind=kind, start=start, dur=dur, target=target)
    f["start_ts"] = f["start_ts"] + timedelta(seconds=sec)
    return f


def _start_minute_epoch(m: int) -> float:
    return (T0 + timedelta(minutes=m)).timestamp()


def test_node_down_set_carries_the_second_level_start_only_when_the_scenario_has_it():
    sc = _sc("gw_node_down", start=5, duration=6, sgw="0016")
    assert "AT" not in S.node_down_set([sc], 6), "시작 시각이 없으면 AT 키가 없어야 한다"
    sc.start_ts = _start_minute_epoch(5) + SEC
    down = S.node_down_set([sc], 6)
    assert down["AT"] == {("SGW", "16"): sc.start_ts}
    assert S.down_at(down, "SGW", "16") == sc.start_ts and S.down_at(down, "SGW", "99") is None
    assert S.down_at(S.node_down_set([_sc("gw_node_down", sgw="0016")], 6), "SGW", "16") is None


def test_the_scenario_built_from_a_fault_row_gets_the_exact_start_second(pg):
    pg.anchor = T0
    scs, bad = pg.set_fault_rows([_fault_s()])
    assert scs and not bad
    assert scs[0].start_ts == pytest.approx(_start_minute_epoch(5) + SEC)
    assert scs[0].start_minute == 5                                       # 분 좌표는 그대로(다른 장애와 같다)


def test_start_minute_calls_are_normal_before_the_down_second_and_fail_after(pg):
    runs, sc = _run(pg, 8, [_fault_s()])
    key = S.node_key(sc.target.sgw_id)
    at = sc.start_ts
    i = pg.gen.index["lte_call"]
    before_ok = after_ok = after_fail = 0
    for r in runs[5][0]:
        if S.node_key(r[i["SGW_ID"]]) != key or r[i["call_type"]] not in S.CT_SESSION_SETUP or r[i["drop_flag"]] in ("1", "True"):
            continue
        t0 = float(r[i["call_start_time"]] or 0)
        ok = r[i["success_flag"]] in ("1", "True")
        if t0 < at:
            before_ok += ok
        else:
            after_ok += ok
            after_fail += not ok
    assert before_ok > 0, "다운 시각 전에 시작한 호는 정상이어야 한다"
    assert after_ok == 0, f"다운 시각 이후 시작한 호가 성공 {after_ok}건"
    assert after_fail > 0


def test_sessions_open_at_the_start_minute_are_cut_exactly_at_the_down_second(pg):
    pg.anchor = T0
    scs, bad = pg.set_fault_rows([_fault_s()])
    assert scs and not bad
    key = S.node_key(scs[0].target.sgw_id)
    at = scs[0].start_ts
    for m in range(5):
        pg.records(m, T0 + timedelta(minutes=m))
        pg.uplane_records(m, T0 + timedelta(minutes=m))
    victims = [s for s in pg.sessions.active if S.node_key(s.core[1]) == key]
    assert victims
    log = []
    real = pg.sessions.cut
    pg.sessions.cut = lambda s, t: (log.append((s, t)), real(s, t))[1]
    pg.records(5, T0 + timedelta(minutes=5))
    pg.uplane_records(5, T0 + timedelta(minutes=5))
    for s in victims:
        assert s.cut_t == pytest.approx(at), (s.sid, s.cut_t - _start_minute_epoch(5))
        assert s.cut_t > _start_minute_epoch(5) + 1, "분 시작이 아니라 다운 시각이어야 한다(18초 일찍 끊기던 결함)"
    # 이 분에 새로 열린 세션도 다운 시각 이전에 끊기지 않는다
    assert all(t >= at - 1e-6 for s, t in log if S.node_key(s.core[1]) == key)


def test_cplane_cuts_start_the_minute_after_the_cut_and_split_over_two_minutes(pg):
    runs, sc = _run(pg, 10, [_fault_s()])
    by_min = collections.Counter(m for m, _ in pg.cleanup_log)
    assert by_min[5] == 0, "끊은 분에는 정리 기록이 없다"
    assert by_min[6] and by_min[7]
    assert by_min[8] == 0 and by_min[9] == 0, "셋째 분 이후 정리 기록이 남았다"      # 맥스 수정 뒤 0 건
    imsis = [i for _, i in pg.cleanup_log]
    assert len(imsis) == len(set(imsis)), "가입자당 한 건이어야 한다"


def test_cplane_cut_records_never_end_before_the_down_second(pg):
    runs, sc = _run(pg, 10, [_fault_s()])
    key = S.node_key(sc.target.sgw_id)
    i = pg.gen.index["lte_call"]
    early = [r for m in (6, 7) for r in runs[m][0]
             if r[i["drop_flag"]] in ("1", "True") and r[i["UEContextRelease_Cause"]] == "504"
             and S.node_key(r[i["SGW_ID"]]) == key and float(r[i["call_end_time"]] or 0) < sc.start_ts]
    assert not early, f"다운 시각보다 앞서 끝나는 절단 기록 {len(early)}건"


def test_f110_reproduction_fault_arrives_after_its_start_minute_was_already_generated(pg):
    """F-110 — 장애를 넣기 전에 시작 분(5)이 이미 만들어졌다. 첫 처리 분(6)에 끊고, 절단 기록은 이어지는 두 분(7·8)에 나뉜다."""
    pg.anchor = T0
    for m in range(6):                                                   # 0~5분: 장애 없이 생성
        pg.records(m, T0 + timedelta(minutes=m))
        pg.uplane_records(m, T0 + timedelta(minutes=m))
    scs, bad = pg.set_fault_rows([_fault_s(start=5, sec=SEC, dur=8)])    # 시작 시각(5분 :18)은 이미 지났다
    assert scs and not bad
    key = S.node_key(scs[0].target.sgw_id)
    log = _log_cleanup(pg)
    for m in range(6, 12):
        ts = T0 + timedelta(minutes=m)
        pg.records(m, ts)
        pg.uplane_records(m, ts)
    by_min = collections.Counter(m for m, _ in log)
    assert by_min[6] == 0, "첫 처리 분에 세션을 끊었고, 정리 기록은 그 분에 없어야 한다"
    assert by_min[7] and by_min[8], "이어지는 두 분에 나뉘어야 한다"
    assert by_min[9] == 0 and by_min[10] == 0
    imsis = [i for _, i in log]
    assert len(imsis) == len(set(imsis))


def test_no_call_that_spans_the_down_second_ends_as_a_success(pg):
    runs, sc = _run(pg, 12, [_fault_s(dur=6)])
    key = S.node_key(sc.target.sgw_id)
    i = pg.gen.index["lte_call"]
    late = 0
    for m in range(sc.start_minute, sc.start_minute + sc.duration):
        late += sum(1 for r in runs[m][0]
                    if S.node_key(r[i["SGW_ID"]]) == key and r[i["call_type"]] in S.CT_SESSION_SETUP
                    and r[i["success_flag"]] in ("1", "True") and r[i["drop_flag"]] not in ("1", "True")
                    and float(r[i["call_end_time"]] or 0) >= sc.start_ts)
    assert late == 0, f"다운 시각 뒤에 끝난 성공 호 {late}건"


def test_no_new_uplane_session_opens_under_the_down_sgw_after_the_down_second(pg):
    runs, sc = _run(pg, 12, [_fault_s(dur=6)])
    key = S.node_key(sc.target.sgw_id)
    t = pg.gen.index["transport"]
    for m in range(sc.start_minute + 1, sc.start_minute + sc.duration):
        new = [r for r in runs[m][1].get("transport", [])
               if S.node_key(r[t["SGW_ID"]]) == key and r[t["OngoingFlag"]] in (SE.F_START, SE.F_SINGLE)]
        assert not new, f"{m}분: 다운 SGW 아래 새 세션 {len(new)}건"


# --------------------------------------------------------------------------
# 10. 다운 시각을 가로지른 호 (맥스 수정 11-04 · `down_phase` · `retime_drop`)
# --------------------------------------------------------------------------


def _down_drops(pg, runs, sc, minutes):
    key = S.node_key(sc.target.sgw_id)
    i = pg.gen.index["lte_call"]
    return [(m, r) for m in minutes for r in runs[m][0]
            if r[i["drop_flag"]] in ("1", "True") and r[i["UEContextRelease_Cause"]] == "504"
            and S.node_key(r[i["SGW_ID"]]) == key]


def test_every_504_cut_record_ends_after_the_down_second(pg):
    """연결 중에 다운을 맞은 호의 절단도, MME 정리 기록도 — 끝 시각은 다운 시각 이후다(맥스 수정 `retime_drop`)."""
    runs, sc = _run(pg, 14, [_fault_s(dur=6)])
    i = pg.gen.index["lte_call"]
    drops = _down_drops(pg, runs, sc, range(sc.start_minute, sc.start_minute + sc.duration + 2))
    assert drops
    for m, r in drops:
        end = float(r[i["call_end_time"]] or 0)
        assert end >= sc.start_ts - 1e-6, f"{m}분: 절단 끝 시각이 다운 시각보다 앞선다 ({end - sc.start_ts:.1f}초)"
        assert r[i["success_flag"]] in ("1", "True") and r[i["UEContextRelease_Cause"]] == "504"


def test_every_504_cut_record_ends_inside_its_own_minute(pg):
    runs, sc = _run(pg, 14, [_fault_s(dur=6)])
    i = pg.gen.index["lte_call"]
    drops = _down_drops(pg, runs, sc, range(sc.start_minute, sc.start_minute + sc.duration + 2))
    bad = [(m, float(r[i["call_end_time"]]) - _start_minute_epoch(m)) for m, r in drops
           if not (-1e-6 <= float(r[i["call_end_time"]]) - _start_minute_epoch(m) < 60 + 1e-6)]
    assert not bad, f"끝 시각이 그 분 밖인 절단 레코드 {len(bad)}건 (예: {bad[:3]})"


def test_a_call_connected_at_the_down_second_is_cut_in_the_start_minute_and_opens_no_session(pg):
    """시작 분에 끝난 호 중 접속을 마치고 쓰던 호는 절단(504)으로 남고, 그 호가 다운 SGW 에서 세션을 열지 않는다."""
    runs, sc = _run(pg, 12, [_fault_s(dur=6)])
    key = S.node_key(sc.target.sgw_id)
    i = pg.gen.index["lte_call"]
    straddlers = [r for r in runs[sc.start_minute][0]
                  if S.node_key(r[i["SGW_ID"]]) == key and r[i["drop_flag"]] in ("1", "True")
                  and r[i["UEContextRelease_Cause"]] == "504" and r[i["call_type"]] in S.CT_SESSION_SETUP]
    assert straddlers, "시작 분에 다운 시각을 가로지른 연결 중 호의 절단이 없다"
    t = pg.gen.index["transport"]
    # 다운 시각 이후 다운 SGW 아래 새 세션은 시작 분에도 없다(그 호들이 세션을 열지 않았다)
    new = [r for r in runs[sc.start_minute][1].get("transport", [])
           if S.node_key(r[t["SGW_ID"]]) == key and r[t["OngoingFlag"]] in (SE.F_START, SE.F_SINGLE)
           and float(r[t["Traffic_Total_Uplink_FirstTime"]]) >= sc.start_ts]
    assert not new, f"다운 시각 이후 다운 SGW 아래 새 세션 {len(new)}건"


def test_calls_in_the_access_procedure_at_the_down_second_fail_with_s11_timeout(pg):
    """접속 절차 중 다운을 맞은 호는 S11 타임아웃(900) 실패다 — 정상 종료도 절단도 아니다."""
    runs, sc = _run(pg, 12, [_fault_s(dur=6)])
    key = S.node_key(sc.target.sgw_id)
    i = pg.gen.index["lte_call"]
    failed = [r for m in range(sc.start_minute, sc.start_minute + sc.duration) for r in runs[m][0]
              if S.node_key(r[i["SGW_ID"]]) == key and r[i["call_type"]] in ("3", "4")
              and r[i["success_flag"]] not in ("1", "True") and r[i["drop_flag"]] not in ("1", "True")
              and float(r[i["call_end_time"]] or 0) >= sc.start_ts]
    assert len(failed) > 50
    s11 = sum(1 for r in failed if r[i["first_error_cause"]] == "900")
    assert s11 / len(failed) >= 0.95, (s11, len(failed))


def test_steady_state_second_level_start_leaves_no_new_session_and_no_plain_success_during_the_outage(pg):
    """정상 상태(이력 있음)에서 :18 에 장애를 시작해도 장애 중 다운 SGW 아래 새 세션과 `drop_flag=0` 성공 호가 0건이다."""
    runs, sc = _run(pg, 12, [_fault_s(dur=6)])
    key = S.node_key(sc.target.sgw_id)
    i, t = pg.gen.index["lte_call"], pg.gen.index["transport"]
    for m in range(sc.start_minute + 1, sc.start_minute + sc.duration):
        plain = [r for r in runs[m][0]
                 if S.node_key(r[i["SGW_ID"]]) == key and r[i["call_type"]] in S.CT_SESSION_SETUP
                 and r[i["success_flag"]] in ("1", "True") and r[i["drop_flag"]] not in ("1", "True")
                 and float(r[i["call_end_time"]] or 0) >= sc.start_ts]
        assert not plain, f"{m}분: 성공 호 {len(plain)}건"
        new = [r for r in runs[m][1].get("transport", [])
               if S.node_key(r[t["SGW_ID"]]) == key and r[t["OngoingFlag"]] in (SE.F_START, SE.F_SINGLE)]
        assert not new, f"{m}분: 새 U-Plane 세션 {len(new)}건"


# --------------------------------------------------------------------------
# 11. 레코드 시각 — 호 끝 시각은 그 레코드의 분 안이다 (맥스 수정 11-04 · `fit_minute` · `retime_drop`)
# --------------------------------------------------------------------------


def _outside(pg, rows, m):
    i = pg.gen.index["lte_call"]["call_end_time"]
    slot = _start_minute_epoch(m)
    return [float(r[i]) - slot for r in rows if not (0 <= float(r[i]) - slot < 60)]


def _is_epoch(v, end):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return False
    return bool(v) and "." in v and f > S._EPOCH_MIN and abs(f - end) < 3600.0


def test_fit_minute_leaves_in_minute_records_untouched(pg):
    ts = T0 + timedelta(minutes=3)
    slot = ts.timestamp()
    for _ in range(50):
        rec, _cell = pg.gen.base_lte_call(ts, slot)
        end = float(rec.get("call_end_time"))
        if slot <= end < slot + 59.9:
            before = list(rec.values)
            assert S.fit_minute(rec, slot) == 0.0
            assert rec.values == before
            return
    pytest.fail("분 안의 레코드를 만들지 못했다")


def test_fit_minute_shifts_every_time_field_by_the_overflow_and_keeps_durations(pg):
    ts = T0 + timedelta(minutes=3)
    slot = ts.timestamp()
    rec, _cell = pg.gen.base_lte_call(ts, slot)
    end = float(rec.get("call_end_time"))
    idx = [i for i, v in enumerate(rec.values) if _is_epoch(v, end)]
    assert idx
    for i in idx:                                                         # 모든 시각 칸을 분 끝 밖으로 민다
        rec.values[i] = f"{float(rec.values[i]) + (slot + 75.0 - end):.6f}"
    start, end2 = float(rec.get("call_start_time")), float(rec.get("call_end_time"))
    shift = S.fit_minute(rec, slot)
    start3, end3 = float(rec.get("call_start_time")), float(rec.get("call_end_time"))
    assert shift == pytest.approx(75.0 - 59.95)
    assert end3 == pytest.approx(slot + 59.95)
    assert end3 - start3 == pytest.approx(end2 - start, abs=1e-5), "길이는 그대로여야 한다"


def test_no_fault_minutes_have_every_call_end_inside_their_own_minute(pg):
    """평시(장애 없음)에는 `fit_minute` 가 할 일이 없다 — 모든 레코드의 끝 시각이 이미 그 분 안이다."""
    pg.anchor = T0
    for m in range(5):
        rows = pg.records(m, T0 + timedelta(minutes=m))
        pg.uplane_records(m, T0 + timedelta(minutes=m))
        assert rows and not _outside(pg, rows, m), f"{m}분: 분 밖 끝 시각 {len(_outside(pg, rows, m))}건"


def test_node_down_overflow_past_the_minute_end_is_gone(pg):
    """맥스 수정 전에는 장애 중 분당 76~106건이 분 끝을 넘었다 — 이제 0건이다(앞쪽 미달은 아래 xfail)."""
    runs, sc = _run(pg, 14, [_fault_s(dur=6)])
    for m in range(sc.start_minute, sc.start_minute + sc.duration + 1):
        over = [x for x in _outside(pg, runs[m][0], m) if x >= 60]
        assert not over, f"{m}분: 분 끝을 넘은 레코드 {len(over)}건"


def test_every_call_ends_inside_its_own_minute_during_the_outage(pg):
    runs, sc = _run(pg, 14, [_fault_s(dur=6)])
    bad = {m: _outside(pg, runs[m][0], m) for m in range(sc.start_minute, sc.start_minute + sc.duration + 1)}
    assert not any(bad.values()), {m: len(v) for m, v in bad.items() if v}


def test_504_cut_records_have_request_and_release_between_the_down_second_and_the_minute_end(pg):
    runs, sc = _run(pg, 14, [_fault_s(dur=6)])
    i = pg.gen.index["lte_call"]
    key = S.node_key(sc.target.sgw_id)
    n = bad = 0
    for m in range(sc.start_minute, sc.start_minute + sc.duration + 2):
        slot = _start_minute_epoch(m)
        lo = max(sc.start_ts, slot)
        for r in runs[m][0]:
            # 다운 절단만 가린다 — 평시 배경 절단도 해제 Cause 504 를 섞어 갖는다(평시 절단은 다운 시각 전에 끝날 수 있다).
            # 다운이 만든 절단은 First Error 도 504 다
            if not (r[i["drop_flag"]] in ("1", "True") and r[i["UEContextRelease_Cause"]] == "504"
                    and r[i["first_error_cause"]] in ("504", "504.0")
                    and S.node_key(r[i["SGW_ID"]]) == key):
                continue
            n += 1
            for f in ("UEContextReleaseRequest_Time", "UEContextRelease_Time"):
                t = float(r[i[f]] or 0)
                if not (lo - 1e-6 <= t < slot + 60):
                    bad += 1
    assert n > 100
    assert bad == 0, f"요청·해제 시각이 구간 밖인 절단 {bad}/{n}"


def _sc18_outside(pg):
    pg.anchor = T0
    scs, bad = pg.set_fault_rows([_fault(kind="partial_recovery", start=4, dur=20)])
    assert scs and not bad
    over = under = 0
    for m in range(0, 22):
        ts = T0 + timedelta(minutes=m)
        rows = pg.records(m, ts)
        pg.uplane_records(m, ts)
        out = _outside(pg, rows, m)
        over += sum(1 for x in out if x >= 60)
        under += sum(1 for x in out if x < 0)
    return over, under


def test_partial_recovery_sc18_no_call_ends_past_its_minute(pg):
    """SC-18 은 같은 S11 타임아웃 명세를 쓴다 — 이전부터 있던 분 끝 넘침이 `fit_minute` 로 함께 고쳐졌다."""
    over, _under = _sc18_outside(pg)
    assert over == 0, f"분 끝을 넘은 레코드 {over}건"


def test_partial_recovery_sc18_no_call_ends_before_its_minute(pg):
    _over, under = _sc18_outside(pg)
    assert under == 0, f"분 시작보다 앞선 레코드 {under}건"


# --------------------------------------------------------------------------
# 12. `fit_minute` 의 두 방향과 다운 시각 하한 (맥스 수정 11-04 · `fit_minute(rec, slot, lo)` · `down_floor`)
# --------------------------------------------------------------------------


def _fresh_rec(pg, minute=3):
    ts = T0 + timedelta(minutes=minute)
    slot = ts.timestamp()
    rec, _cell = pg.gen.base_lte_call(ts, slot)
    end = float(rec.get("call_end_time"))
    idx = [i for i, v in enumerate(rec.values) if _is_epoch(v, end)]
    return rec, slot, end, idx


def _move_all(rec, idx, delta):
    for i in idx:
        rec.values[i] = f"{float(rec.values[i]) + delta:.6f}"


def _offsets(rec, idx, end):
    return [round(float(rec.values[i]) - end, 4) for i in idx]


def test_fit_minute_pushes_an_early_call_back_into_the_minute_keeping_every_step_time(pg):
    rec, slot, end, idx = _fresh_rec(pg)
    _move_all(rec, idx, (slot - 40.0) - end)                              # 끝이 분 시작보다 40초 앞
    end0 = float(rec.get("call_end_time"))
    off0 = _offsets(rec, idx, end0)
    shift = S.fit_minute(rec, slot)
    end1 = float(rec.get("call_end_time"))
    assert shift < 0, "뒤로 밀면 음수를 돌려준다"
    assert slot <= end1 < slot + 60
    assert _offsets(rec, idx, end1) == off0, "길이와 단계 시간(끝 대비 각 시각의 간격)이 그대로여야 한다"


def test_fit_minute_never_moves_an_end_that_is_already_inside_the_allowed_range(pg):
    rec, slot, end, idx = _fresh_rec(pg)
    _move_all(rec, idx, (slot + 30.0) - end)
    before = list(rec.values)
    assert S.fit_minute(rec, slot, slot + 10.0) == 0.0                    # 하한(slot+10) 이상이면 그대로
    assert rec.values == before


def test_fit_minute_with_a_down_floor_puts_the_end_between_the_floor_and_the_minute_end(pg):
    rec, slot, end, idx = _fresh_rec(pg)
    floor = slot + 18.0
    _move_all(rec, idx, (slot + 5.0) - end)                               # 끝이 다운 시각(:18)보다 앞
    off0 = _offsets(rec, idx, float(rec.get("call_end_time")))
    S.fit_minute(rec, slot, floor)
    end1 = float(rec.get("call_end_time"))
    assert floor - 1e-6 <= end1 < slot + 60
    assert _offsets(rec, idx, end1) == off0
    # 하한을 주지 않으면 분 시작이 하한이다 — SC-18 처럼 다운 시각 하한이 적용되지 않는다
    rec2, slot2, end2, idx2 = _fresh_rec(pg)
    _move_all(rec2, idx2, (slot2 + 5.0) - end2)
    assert S.fit_minute(rec2, slot2) == 0.0, "분 안(:05)이면 하한이 없을 때 그대로여야 한다"


def test_start_minute_s11_failures_end_between_the_down_second_and_the_minute_end(pg):
    runs, sc = _run(pg, 8, [_fault_s(dur=6)])
    key = S.node_key(sc.target.sgw_id)
    i = pg.gen.index["lte_call"]
    slot = _start_minute_epoch(sc.start_minute)
    fails = [r for r in runs[sc.start_minute][0]
             if S.node_key(r[i["SGW_ID"]]) == key and r[i["call_type"]] in ("3", "4")
             and r[i["success_flag"]] not in ("1", "True") and r[i["first_error_cause"]] == "900"]
    assert len(fails) > 20, "시작 분에 S11 실패 호가 거의 없다"
    for r in fails:
        end = float(r[i["call_end_time"]])
        assert sc.start_ts - 1e-6 <= end < slot + 60, f"S11 실패 호의 끝이 다운 시각 앞이거나 분 밖이다 ({end - sc.start_ts:.1f}초)"


def test_calls_that_ended_before_the_down_second_in_the_start_minute_are_left_alone(pg):
    """다운 시각 전에 끝난 호는 장애 실패로 바뀌지 않는다 — S11 900 실패도 504 절단도 아니다."""
    runs, sc = _run(pg, 8, [_fault_s(dur=6)])
    key = S.node_key(sc.target.sgw_id)
    i = pg.gen.index["lte_call"]
    early = [r for r in runs[sc.start_minute][0]
             if S.node_key(r[i["SGW_ID"]]) == key and r[i["call_type"]] in S.CT_SESSION_SETUP
             and float(r[i["call_end_time"]]) < sc.start_ts]
    assert len(early) > 5, "다운 시각 전에 끝난 호가 거의 없다"
    assert not [r for r in early if r[i["first_error_cause"]] == "900" or r[i["UEContextRelease_Cause"]] == "504"]
    assert sum(1 for r in early if r[i["success_flag"]] in ("1", "True")) > 0.8 * len(early)


def test_sc18_pushes_only_by_the_minute_floor_no_down_floor(pg):
    """SC-18 은 다운 시각 하한을 받지 않는다 — 끝 시각은 분 시작 이상이면 된다(위 `test_partial_recovery_sc18_*`)."""
    assert S.down_floor.__code__.co_argcount == 3                         # (rec, cell, down) — 장애 종류와 무관하게 노드 다운만 하한을 준다
    over, under = _sc18_outside(pg)
    assert over == 0 and under == 0
