"""gen 가입자 위치 이어 받기 — 종료 덤프 · `.used` · 5분 통계 · 지문 · 미리 걷기 제외 (2026-11-23 동구 구현 · 에스터 시험).

사용자 「틱마다 저장하지 말고 종료할 때 캐시 스타일로 덤프」「덤프가 없으면 통계로 마지막 위치」「통계는 5분 이내」.
근거: 11-14 gen 재시작 뒤 외곽 셀 47분 무호(F-329) — 재시작이 걷는 사람을 처음 배치로 되돌렸다.
합성 셀(PUBLIC 저장소라 eNB 번호는 모두 합성) · DB 없음.
"""
from __future__ import annotations

import gzip
import inspect
import json
import random
from pathlib import Path
from types import SimpleNamespace

import pytest

from datagen import cli
from datagen.cells import Cell
from datagen.generator import Generator
from datagen.profile import load as load_profiles

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
RATES = {"lte_call": 300, "transport": 300, "dns": 100, "http": 100}

pytestmark = pytest.mark.skipif(not (DATA / "profile.json").exists(), reason="data/profile.json 없음")


def _cell(enb, n, lat, lon):
    return Cell(cell_key=f"{enb}-{n}", enb_id=enb, cell_id=n, enb_seq=0, c_uid="", tac=100, plmn="44010",
                vlan_id="", latitude=lat, longitude=lon, adm_sido_cd="11", adm_sido_nm="",
                adm_sgg_cd="11200", adm_sgg_nm="", adm_emd_cd="11200011", adm_emd_nm="", geo_status="MAPPED",
                rat="EUTRAN", mme_id="0001", sgw_id="0037", pgw_id="0037", ims_pgw_id="0037",
                traffic_weight=1.0, display_name="")


def _cells(n=240, skip=None):
    out = [_cell(f"{600000 + i // 3}", i % 3, 35.0 + i * 1e-4, 139.0) for i in range(n)]
    return [c for c in out if c.cell_key != skip]


def _gen(cells=None, seed=20261123, pool=800):
    profiles = load_profiles(DATA / "profile.json")
    keep = {s: profiles[s] for s in ("lte_call", "transport", "dns", "http")}
    g = Generator(cells=cells or _cells(), profiles=keep, scenarios=[], rates=RATES, seed=seed, diurnal=True,
                  subscriber_pool=pool, success_only=True, end_only=True)
    g.seat_walkers(0.5, random.Random(7))
    return g


def _walk_cells(g):
    return {s.imsi: s.walk_cell.cell_key for s in g.subs if s.walk_cell is not None}


def _moved_copy(src, shift=1):
    """`src` 의 걷는 사람 위치를 한 칸씩 옮긴 같은 배치의 생성기(= 며칠 돌아 흩어진 상태)"""
    g = _gen()
    keys = [c.cell_key for c in g.cells]
    for s in g.subs:
        if s.walk_cell is not None:
            s.walk_cell = g.cell_by_key[keys[(keys.index(s.walk_cell.cell_key) + shift) % len(keys)]]
    return g


# ── 1. 덤프 → 복원 ───────────────────────────────────────────────────────────

def test_dump_then_restore_gives_the_same_walker_cells_and_fresh_generator_differs():
    running = _moved_copy(_gen())                       # 며칠 돌아 흩어진 상태
    state = running.mobility_state()
    fresh = _gen()                                      # 재시작 직후의 기동 배치
    assert _walk_cells(fresh) != _walk_cells(running)
    ok, why = fresh.restore_mobility(json.loads(json.dumps(state)))       # 파일을 거친 모양(JSON)
    assert ok, why
    assert _walk_cells(fresh) == _walk_cells(running)
    assert [(s.walk_t, s.walk_dwell) for s in fresh.subs] == [(s.walk_t, s.walk_dwell) for s in running.subs]
    assert fresh._walk_n == running._walk_n


def test_the_state_only_carries_moving_subscribers():
    g = _gen()
    st = g.mobility_state()
    movers = {s.imsi for s in g.subs if s.route >= 0 or s.walk_cell is not None}
    assert set(st["subs"]) == movers and 0 < len(movers) < len(g.subs)


def test_restore_replaces_the_pre_walk_so_it_is_skipped():
    g = _gen()
    ok, _ = g.restore_mobility(_gen().mobility_state())
    assert ok and g.walk_warmup_stats["minutes"] == 0 and g.walk_warmup_stats["restored"] > 0


# ── 2. 지문 · 모양이 다르면 아무것도 바꾸지 않는다 ──────────────────────────

@pytest.mark.parametrize("make", [
    lambda: _gen(_cells(skip="600005-1")),               # 셀 목록이 다르다
    lambda: _gen(pool=700),                              # 가입자 수가 다르다
    lambda: _gen(seed=1),                                # 씨앗이 다르다
])
def test_a_different_fingerprint_changes_nothing(make):
    state = _moved_copy(_gen()).mobility_state()
    other = make()
    before = _walk_cells(other)
    ok, why = other.restore_mobility(state)
    assert ok is False and "다르다" in why
    assert _walk_cells(other) == before and other.walk_warmup_stats is None


def test_a_different_walker_share_changes_the_fingerprint():
    a, b = _gen(), _gen()
    b.seat_walkers(0.3, random.Random(7))
    assert a.mobility_fingerprint() != b.mobility_fingerprint()


def test_an_unknown_cell_in_the_dump_aborts_without_half_restoring():
    src = _moved_copy(_gen())
    state = src.mobility_state()
    imsi = sorted(state["subs"])[5]
    row = list(state["subs"][imsi])
    row[len(g := _gen()._MOB_FIELDS)] = "999999-9"       # 셀 목록에 없는 셀
    state["subs"][imsi] = row
    fresh = _gen()
    before = _walk_cells(fresh)
    ok, why = fresh.restore_mobility(state)
    assert ok is False and "셀" in why and _walk_cells(fresh) == before      # 앞 사람까지 되돌려지지 않았다


def test_a_wrong_format_or_garbage_is_refused():
    fresh = _gen()
    before = _walk_cells(fresh)
    st = _gen().mobility_state()
    st["fields"] = ["x"]
    assert fresh.restore_mobility(st)[0] is False
    assert fresh.restore_mobility({})[0] is False and fresh.restore_mobility(None)[0] is False
    st2 = _gen().mobility_state()
    st2["subs"]["nobody"] = [0] * 11
    assert fresh.restore_mobility(st2)[0] is False
    assert _walk_cells(fresh) == before


# ── 3. 통계로 앉히기 ─────────────────────────────────────────────────────────

def test_seat_from_cells_puts_walkers_on_their_last_cell_and_skips_the_rest():
    g = _gen()
    walkers = [s for s in g.subs if s.walk_cell is not None]
    fixed = next(s for s in g.subs if s.walk_cell is None and s.route < 0)
    target = [c for c in g.cells if c.cell_key != walkers[0].walk_cell.cell_key][3].cell_key
    n = g.seat_from_cells({walkers[0].imsi: (target, False), walkers[1].imsi: ("999999-9", False),
                           fixed.imsi: (target, False)})
    assert n == 1
    assert walkers[0].walk_cell.cell_key == target
    assert fixed.walk_cell is None                               # 고정 가입자는 건드리지 않는다
    assert walkers[1].walk_cell.cell_key != "999999-9"           # 모르는 셀이면 기동 배치 그대로


def test_seat_from_cells_marks_seated_walkers_to_skip_the_pre_walk(monkeypatch):
    g = _gen()
    w = [i for i, s in enumerate(g.subs) if s.walk_cell is not None]
    target = g.cells[10].cell_key
    g.seat_from_cells({g.subs[w[0]].imsi: (target, True)})
    assert g._warm_skip == {w[0]}
    seen = {}

    def spy(now):
        seen["during"] = list(g._walkers)
        return 0

    monkeypatch.setattr(g, "_advance_walkers", spy)
    g.ho_topo = object()
    g.walk_warmup_min = 1
    all_walkers = list(g._walkers)
    g._warm_walkers(1_000_000.0)
    assert w[0] not in seen["during"] and set(seen["during"]) == set(all_walkers) - {w[0]}
    assert g._walkers == all_walkers                             # 끝나면 목록이 원래대로
    assert g.walk_warmup_stats["skipped"] == 1


def test_without_seated_walkers_nothing_is_skipped():
    g = _gen()
    assert g.seat_from_cells({}) == 0 and not getattr(g, "_warm_skip", None)


# ── 4. cli — 종료 덤프 · .used · 폴백 ───────────────────────────────────────

def _wrap(tmp_path, g):
    return SimpleNamespace(truth_path=tmp_path / "truth" / "ground_truth.json", gen=g)


def _dump_path(tmp_path):
    return tmp_path / "truth" / cli.MOBILITY_STATE_FILE


def test_save_writes_a_gzip_json_next_to_the_truth_file_via_tmp(tmp_path):
    w = _wrap(tmp_path, _moved_copy(_gen()))
    cli._save_mobility(w)
    p = _dump_path(tmp_path)
    assert p.exists() and not p.with_name(p.name + ".tmp").exists()
    with gzip.open(p, "rt", encoding="utf-8") as f:
        st = json.load(f)
    assert st["fingerprint"] == w.gen.mobility_fingerprint() and st["saved_at"] and st["subs"]


def test_a_failing_save_keeps_the_previous_file(tmp_path, monkeypatch):
    w = _wrap(tmp_path, _gen())
    cli._save_mobility(w)
    p = _dump_path(tmp_path)
    before = p.read_bytes()
    monkeypatch.setattr(w.gen, "mobility_state", lambda: {"x": object()})        # JSON 으로 못 쓴다
    with pytest.raises(TypeError):
        cli._save_mobility(w)
    assert p.read_bytes() == before                                              # 임시 파일에서 죽어 앞 파일이 남는다


def test_save_without_a_truth_path_does_nothing():
    cli._save_mobility(SimpleNamespace(truth_path=None, gen=_gen()))


def test_load_restores_from_the_dump_and_renames_it_to_used(tmp_path, capsys):
    running = _moved_copy(_gen())
    cli._save_mobility(_wrap(tmp_path, running))
    fresh = _wrap(tmp_path, _gen())
    cli._load_mobility(fresh, conn=None)
    assert _walk_cells(fresh.gen) == _walk_cells(running)
    p = _dump_path(tmp_path)
    assert not p.exists() and p.with_name(p.name + ".used").exists()
    assert "덤프" in capsys.readouterr().out


def test_a_used_dump_is_not_read_again(tmp_path):
    cli._save_mobility(_wrap(tmp_path, _moved_copy(_gen())))
    cli._load_mobility(_wrap(tmp_path, _gen()), conn=None)
    second = _wrap(tmp_path, _gen())
    before = _walk_cells(second.gen)
    cli._load_mobility(second, conn=None)                       # 비정상 종료 뒤: 오래된 위치를 이어 받지 않는다
    assert _walk_cells(second.gen) == before


def test_a_different_fingerprint_dump_falls_back_to_stats(tmp_path, monkeypatch, capsys):
    cli._save_mobility(_wrap(tmp_path, _gen(_cells(skip="600005-1"))))
    fresh = _wrap(tmp_path, _gen())
    seen = {}
    monkeypatch.setattr(cli, "_last_cells_xp", lambda conn, want, minutes: seen.update(m=minutes) or {})
    cli._load_mobility(fresh, conn=object())
    out = capsys.readouterr().out
    assert "이어 받지 않음" in out and seen["m"] == 5


def test_a_corrupt_dump_warns_and_falls_back(tmp_path, monkeypatch, capsys):
    p = _dump_path(tmp_path)
    p.parent.mkdir(parents=True)
    p.write_bytes(b"not gzip")
    fresh = _wrap(tmp_path, _gen())
    before = _walk_cells(fresh.gen)
    monkeypatch.setattr(cli, "_last_cells_xp", lambda conn, want, minutes: {})
    cli._load_mobility(fresh, conn=object())
    assert "읽지 못함" in capsys.readouterr().out and _walk_cells(fresh.gen) == before


def test_without_a_dump_the_last_five_minutes_of_stats_seat_the_walkers(tmp_path, monkeypatch, capsys):
    fresh = _wrap(tmp_path, _gen())
    walkers = [s for s in fresh.gen.subs if s.walk_cell is not None]
    target = fresh.gen.cells[7].cell_key
    seen = {}

    def fake(conn, want, minutes):
        seen.update(minutes=minutes, want=set(want))
        return {walkers[0].imsi: (target, False), walkers[1].imsi: (target, True)}

    monkeypatch.setattr(cli, "_last_cells_xp", fake)
    cli._load_mobility(fresh, conn=object())
    assert seen["minutes"] == cli.MOBILITY_LOOKBACK_MIN == 5
    assert walkers[0].imsi in seen["want"]
    assert walkers[0].walk_cell.cell_key == target and walkers[1].walk_cell.cell_key == target
    assert "통계 2" in capsys.readouterr().out


def test_a_stats_failure_falls_back_to_the_startup_layout(tmp_path, monkeypatch, capsys):
    fresh = _wrap(tmp_path, _gen())
    before = _walk_cells(fresh.gen)

    def boom(*a):
        raise RuntimeError("표 없음")

    monkeypatch.setattr(cli, "_last_cells_xp", boom)
    cli._load_mobility(fresh, conn=object())
    assert _walk_cells(fresh.gen) == before and "읽지 못함" in capsys.readouterr().out


def test_without_a_connection_nothing_is_read(tmp_path, capsys):
    cli._load_mobility(_wrap(tmp_path, _gen()), conn=None)
    assert "통계 0" in capsys.readouterr().out


def test_the_dump_is_written_only_at_shutdown_not_every_tick():
    src = inspect.getsource(cli)
    assert src.count("_save_mobility, gen") == 1                         # 호출은 종료 경로 한 곳
    at = src.index('_aside("이동 상태 저장", _save_mobility, gen)')
    assert "중단 요청" in src[max(0, at - 250):at]                        # 중단(KeyboardInterrupt) 처리 안에서만 부른다
    assert "signal.signal(signal.SIGTERM" in src                         # docker stop 도 같은 경로로


def test_raw_parquet_fallback_and_app_state_are_gone():
    src = inspect.getsource(cli)
    assert "_last_cells_raw" not in src and "cp_root" not in inspect.getsource(cli._load_mobility)
