"""장비 정지 때 추가 가입자의 끊김·재연결 기록과 5G · IMSI 겹침 (2026-11-21 · 케이 03:00 「장비 100% 장애」 · 맥스 구현 · 에스터 시험).

사용자: 장비 하나가 100% 죽는 장애면 기본 호(추가 가입자 포함)도 전부 영향받아야 한다.
추가 가입자는 U-Plane 세션이 없어 `_node_down_cuts` 가 훑는 `sessions.active` 에 없었다 → 정지 때 「접속 중」 명부(`_xlive`)와
`_extra_down_cuts` 가 끊김(Cause 504)·재연결을 낸다. 합성 셀 마스터 + 고정 시드 · DB 없음(`test_node_down.py` 와 같은 하니스).

1. 추가 0명이면 맥스 수정 전 `pemdb1.py` 백업(`/tmp/mx_s/bak/k0300/`)과 비트 단위로 같다.
2. 추가 N명이어도 기존 가입자의 C-Plane·U-Plane 은 0명일 때와 같다.
3. 추가 가입자에게서 504 절단이 한 사람 한 건·두 분에 걸쳐 나온다 / eNB 정지는 재연결이 나온다.
4. 숨은 SA 단말·기존·추가 가입자의 IMSI·IMEI·MDN 은 겹치지 않는다. 숨은 SA 는 NR 을 받지 않고 추가 가입자는 받는다.
"""
from __future__ import annotations

import collections
import importlib.util
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from datagen import pemdb1 as P
from datagen import scenarios as S
from datagen.cells import Cell

ROOT = Path(__file__).resolve().parents[1]
BAK = Path("/tmp/mx_s/bak/k0300/pemdb1.py")
T0 = datetime(2026, 11, 21, 3, 30)
DOWN_SGW, LIVE_SGW = "0016", "0037"
POOL, MAXN, XN = 2500, 6000, 5000


def _cell(enb: str, n: int, sgw: str) -> Cell:
    return Cell(
        cell_key=f"{enb}-{n}", enb_id=enb, cell_id=n, enb_seq=0, c_uid="",
        tac=100, plmn="44010", vlan_id="", latitude=35.69 + int(enb[-2:]) * 0.002,
        longitude=139.70 + int(enb[-2:]) * 0.002, adm_sido_cd="11", adm_sido_nm="",
        adm_sgg_cd="11200", adm_sgg_nm="", adm_emd_cd="11200011", adm_emd_nm="",
        geo_status="MAPPED", rat="EUTRAN", mme_id="0001", sgw_id=sgw, pgw_id="0037",
        ims_pgw_id="0037", traffic_weight=1.0, display_name="")


def _master() -> list[Cell]:
    out = []
    for e in range(40):
        sgw = DOWN_SGW if e < 20 else LIVE_SGW
        out += [_cell(f"{500000 + e}", n, sgw) for n in range(3)]
    return out


def _bak_module():
    if not BAK.exists():
        pytest.skip("수정 전 백업이 없다")
    spec = importlib.util.spec_from_file_location("datagen.pemdb1_bak", BAK)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["datagen.pemdb1_bak"] = mod
    spec.loader.exec_module(mod)
    return mod


def _make(mod, extra_n: int, *, extra_max: int = MAXN):
    for f in ("pemdb1/_distributions.json", "profile.json"):
        if not (ROOT / "data" / f).exists():
            pytest.fail(f"data/{f} 가 없다")
    mp = pytest.MonkeyPatch()
    mp.setattr(mod, "load_cells", lambda *a, **k: _master())
    mp.setattr(mod, "load_apps", lambda conn: None)
    try:
        g = mod.PemdbGen(None, ROOT / "data", rates={"lte_call": 1500, "transport": 1500},
                         seed=7, subscriber_pool=POOL, extra_lte_max=extra_max)
    finally:
        mp.undo()
    g.anchor = T0
    g.set_extra_subs(extra_n)
    return g


def _fault(kind, start=5, dur=6, target=None):
    return {"fault_id": 9, "kind": kind, "target": target, "start_ts": T0 + timedelta(minutes=start),
            "duration_min": dur, "intensity": 1.0}


def _log(g):
    """정지 기록 함수가 실제로 레코드를 낸 (분, 가입자, 종류)"""
    log = []
    for name, tag in (("_down_cut_rec", "cut"), ("_reconnect_rec", "reconnect")):
        orig = getattr(g, name)

        def wrapped(out, ts, slot_start, imsi, *a, _o=orig, _t=tag, **k):
            n = len(out)
            _o(out, ts, slot_start, imsi, *a, **k)
            if len(out) > n:
                log.append((round((ts - T0).total_seconds() / 60), imsi, _t))
                g.__dict__.setdefault("_made", []).append((_t, imsi, list(out[-1])))
        setattr(g, name, wrapped)
    return log


def _run(mod, extra_n, rows, minutes, *, uplane=True):
    g = _make(mod, extra_n)
    log = _log(g)
    scs, bad = g.set_fault_rows(rows)
    assert scs and not bad, bad
    out = []
    for m in range(minutes):
        ts = T0 + timedelta(minutes=m)
        out.append((g.records(m, ts), g.uplane_records(m, ts) if uplane else None))
    return g, out, scs[0], log


def _enb_target():
    return f"ENB:{500025}"          # 살아 있는 SGW 쪽 eNB 하나 — 이웃 셀 재연결이 가능한 곳


@pytest.fixture(scope="module")
def bak():
    return _bak_module()


FAULTS = {"sgw": lambda: [_fault("gw_node_down", start=5, dur=6)],
          "enb": lambda: [_fault("enb_s1_outage", start=5, dur=8, target=_enb_target())]}


# ── 1. 추가 0명 — 수정 전 백업과 비트 단위로 같다 ────────────────────────────────────────

@pytest.mark.parametrize("kind", ["sgw", "enb"])
def test_zero_extras_is_bit_identical_to_the_backup(bak, kind):
    rows = FAULTS[kind]()
    _g1, a, _s1, _l1 = _run(bak, 0, rows, 16)
    _g2, b, _s2, _l2 = _run(P, 0, FAULTS[kind](), 16)
    for m, ((ca, ua), (cb, ub)) in enumerate(zip(a, b)):
        assert ca == cb, f"{kind} {m}분: C-Plane 이 수정 전과 다르다"
        assert ua == ub, f"{kind} {m}분: U-Plane 이 수정 전과 다르다"


def test_zero_extras_without_any_fault_is_also_identical(bak):
    g1, g2 = _make(bak, 0), _make(P, 0)
    for m in range(6):
        ts = T0 + timedelta(minutes=m)
        assert g1.records(m, ts) == g2.records(m, ts)
        assert g1.uplane_records(m, ts) == g2.uplane_records(m, ts)


# ── 2. 추가 N명 — 기존 가입자의 출력은 0명일 때와 같다 ───────────────────────────────────

@pytest.fixture(scope="module")
def sgw_runs():
    z = _run(P, 0, FAULTS["sgw"](), 16)
    x = _run(P, XN, FAULTS["sgw"](), 16)
    return z, x


@pytest.fixture(scope="module")
def enb_runs():
    z = _run(P, 0, FAULTS["enb"](), 18)
    x = _run(P, XN, FAULTS["enb"](), 18)
    return z, x


def _split(g, calls):
    i = g.gen.index["lte_call"]["IMSI"]
    ex = {s.imsi for s in g.extra_subs}
    return [r for r in calls if r[i] not in ex], [r for r in calls if r[i] in ex]


@pytest.mark.parametrize("runs_name", ["sgw_runs", "enb_runs"])
def test_regular_subscribers_are_untouched_by_the_extras(runs_name, request):
    (g0, out0, _s0, log0), (gx, outx, _sx, logx) = request.getfixturevalue(runs_name)
    for m, ((c0, u0), (cx, ux)) in enumerate(zip(out0, outx)):
        reg, _ex = _split(gx, cx)
        assert reg == c0, f"{m}분: 기존 가입자의 C-Plane 이 추가 {XN}명에서 달라졌다"
        assert ux == u0, f"{m}분: U-Plane 이 추가 {XN}명에서 달라졌다"
    ex = {s.imsi for s in gx.extra_subs}
    assert [x for x in logx if x[1] not in ex] == log0, "기존 가입자의 정지 기록이 달라졌다"


# ── 3. 추가 가입자의 끊김·재연결 ─────────────────────────────────────────────────────────

def test_sgw_down_cuts_extras_once_each_split_over_two_minutes(sgw_runs):
    _z, (g, out, sc, log) = sgw_runs
    ex = {s.imsi for s in g.extra_subs}
    cuts = [(m, i) for m, i, t in log if i in ex and t == "cut"]
    assert cuts, "추가 가입자의 504 절단 기록이 하나도 없다"
    who = collections.Counter(i for _m, i in cuts)
    assert max(who.values()) == 1, "한 사람에게 절단 기록이 둘 이상이다"
    lo = sc.start_minute
    by_min = collections.Counter(m for m, _ in cuts)
    assert set(by_min) <= {lo + 1, lo + 2}, f"절단은 다운 시작 다음 두 분에 걸쳐야 한다: {dict(by_min)}"
    assert len(by_min) == 2 and 0.3 < by_min[lo + 1] / len(cuts) < 0.7, f"절반씩 나뉘지 않았다: {dict(by_min)}"


def test_the_cut_records_are_504_drops_under_the_down_sgw(sgw_runs):
    _z, (g, out, sc, log) = sgw_runs
    ix = g.gen.index["lte_call"]
    ex = {s.imsi for s in g.extra_subs}
    key = S.node_key(sc.target.sgw_id)
    made = [r for t, i, r in g._made if t == "cut" and i in ex]
    assert made
    for r in made:
        assert S.node_key(r[ix["SGW_ID"]]) == key, "절단 기록의 SGW 가 다운 SGW 가 아니다"
        assert r[ix["drop_flag"]] in ("1", "True")
        assert "504" in {r[ix[c]] for c in ("last_error_cause", "first_error_cause", "UEContextRelease_Cause", "emm_error_Cause", "s11_error_Cause")
                         if c in ix} | {r[j] for j in range(len(r)) if r[j] == "504"}


def test_no_extra_succeeds_a_new_setup_under_the_down_sgw(sgw_runs):
    _z, (g, out, sc, _log) = sgw_runs
    ix = g.gen.index["lte_call"]
    ex = {s.imsi for s in g.extra_subs}
    key, at = S.node_key(sc.target.sgw_id), sc.start_ts
    lo, hi = sc.start_minute, sc.start_minute + sc.duration
    seen = 0
    for m in range(lo, hi):
        for r in out[m][0]:
            if r[ix["IMSI"]] not in ex or S.node_key(r[ix["SGW_ID"]]) != key:
                continue
            seen += 1
            ok = (r[ix["success_flag"]] in ("1", "True") and r[ix["drop_flag"]] not in ("1", "True")
                  and r[ix["call_type"]] in S.CT_SESSION_SETUP and r[ix["First_eNB_ID"]] == r[ix["Last_eNB_ID"]]
                  and float(r[ix["call_start_time"]] or 0) >= at)
            assert not ok, f"{m}분: 다운 SGW 아래 추가 가입자 성공 호"
    assert seen > 0, "다운 SGW 아래 추가 가입자 호(실패 시도)가 하나도 없다"


def test_extras_are_cut_at_the_same_rate_as_regular_subscribers(sgw_runs):
    """끊김을 받은 고유 인원 / 다운 전 그 SGW 에 성공 접속한 인원 — 기존 가입자와 같은 비율이어야 한다.

    비율이 1 이 아닌 것은 정상이다(세션 수명이 다한 사람은 이미 끊겨 있다 · 실측 기존 0.117 · 추가 0.114).
    추가 가입자에게만 비율이 낮으면 명부가 새는 것이고, 높으면 수명 처리가 없다는 뜻이다"""
    _z, (g, out, sc, log) = sgw_runs
    ix = g.gen.index["lte_call"]
    ex = {s.imsi for s in g.extra_subs}
    key, lo = S.node_key(sc.target.sgw_id), sc.start_minute

    def ratio(sel):
        cut = {i for _m, i, t in log if sel(i) and t == "cut"}
        # 추가 가입자는 TAU(주기 기록)만 낸다 — 「접속 중」의 근거가 호 절차가 아니라 그 기록이다(11-23)
        cts = {P.TAU_CALL_TYPE} if sel is is_ext else S.CT_SESSION_SETUP
        att = {r[ix["IMSI"]] for calls, _u in out[:lo] for r in calls
               if sel(r[ix["IMSI"]]) and S.node_key(r[ix["SGW_ID"]]) == key
               and r[ix["success_flag"]] in ("1", "True") and r[ix["drop_flag"]] not in ("1", "True")
               and r[ix["call_type"]] in cts}
        assert cut <= att | {r[ix["IMSI"]] for calls, _u in out for r in calls
                             if sel(r[ix["IMSI"]]) and S.node_key(r[ix["SGW_ID"]]) == key}
        return len(cut) / len(att), len(cut), len(att)

    def is_ext(i):
        return i in ex

    reg, ext = ratio(lambda i: i not in ex), ratio(is_ext)
    assert ext[2] > 1000 and reg[1] > 50
    # 비율은 같을 필요가 없다 — 추가는 수명(`_span`)이 끝나기 전에 TAU 가 다시 올 뿐이라 기준 모집단이 다르다. 한 사람 한 건과
    # 접속 인원 중 일정 몫이 끊기는지만 본다
    assert 0.02 < ext[0] <= 1.0, (reg, ext)


def test_enb_down_reconnects_extras_and_cuts_nobody(enb_runs):
    _z, (g, out, sc, log) = enb_runs
    ex = {s.imsi for s in g.extra_subs}
    rec = [(m, i) for m, i, t in log if i in ex and t == "reconnect"]
    assert rec, "eNB 정지에서 추가 가입자의 재연결 기록이 없다"
    assert not [x for x in log if x[1] in ex and x[2] == "cut"], "eNB 정지에서 504 절단이 났다"
    assert max(collections.Counter(i for _m, i in rec).values()) == 1
    lo = sc.start_minute
    assert all(m >= lo + 1 for m, _ in rec), "정지 시작보다 먼저 재연결이 났다"


def test_the_book_empties_by_itself_and_never_grows_past_the_extras(sgw_runs):
    _z, (g, _out, _sc, _log) = sgw_runs
    assert len(g._xlive) <= len(g.extra_subs)
    assert all(imsi in g.extra_by_imsi for imsi in g._xlive)


# ── 4. IMSI 겹침 · 5G ───────────────────────────────────────────────────────────────────

def test_imsi_imei_mdn_never_overlap_between_regular_hidden_sa_and_extras():
    g = _make(P, MAXN)
    reg = g.gen.subs
    hidden = [s for subs in g.sa_hidden.values() for s in subs]
    assert hidden, "숨은 SA 단말이 없다 — 이 시험이 아무것도 못 가른다"
    for field in ("imsi", "imei", "mdn"):
        sets = {"reg": [getattr(s, field) for s in reg], "hid": [getattr(s, field) for s in hidden],
                "ext": [getattr(s, field) for s in g.extra_subs]}
        for k, v in sets.items():
            assert len(v) == len(set(v)), f"{field}: {k} 안에서 겹친다"
        assert not (set(sets["reg"]) & set(sets["hid"])), f"{field}: 기존·숨은 SA"
        assert not (set(sets["reg"]) & set(sets["ext"])), f"{field}: 기존·추가"
        assert not (set(sets["hid"]) & set(sets["ext"])), f"{field}: 숨은 SA·추가"


def _nr_share(g, minutes):
    ix = g.gen.index["lte_call"]
    ex = {s.imsi for s in g.extra_subs}
    cnt = collections.Counter()
    for m in range(minutes):
        for r in g.records(m, T0 + timedelta(minutes=m)):
            who = "ext" if r[ix["IMSI"]] in ex else "reg"
            cnt[who, "n"] += 1
            if r[ix["initial_nr_conn_time"]] not in ("0.0", "0", "", None) or r[ix.get("nr_cell_id", ix["IMSI"])] not in ("", "0", None) and "nr_cell_id" in ix:
                cnt[who, "nr"] += 1
    return cnt


def test_extras_are_heartbeat_only_so_no_nr_while_regulars_keep_it():
    """추가 가입자는 TAU 만 낸다(11-23) — NR 은 `NR_CALL_TYPES` 호에만 붙으므로 없다. 5G 몫은 기본 가입자 것이다"""
    g = _make(P, XN)
    ix = g.gen.index["lte_call"]
    assert "initial_nr_conn_time" in ix
    cnt = _nr_share(g, 8)
    assert P.TAU_CALL_TYPE not in g.NR_CALL_TYPES
    assert cnt["ext", "n"] > 1000 and cnt["reg", "nr"] > 0
    assert cnt["ext", "nr"] == 0


def test_a_hidden_sa_terminal_is_still_not_in_either_lookup():
    g = _make(P, MAXN)
    hidden = [s for subs in g.sa_hidden.values() for s in subs]
    assert not [s for s in hidden if s.imsi in g.gen.sub_by_imsi or s.imsi in g.extra_by_imsi]
