"""시나리오 정리(11-21 · 맥스 구현 · 에스터 시험) — 화면 순서·36번 PENDING·옵션 표·24번 매일 부하 스위치.

* `control.SCENARIO_ORDER` — 24개, 폐기 번호와 겹치지 않고, 폐기 아닌 번호는 모두 순서를 가진다. 36번은 11-21 오후 생성기가 들어와 READY.
* 옵션 표(`SCN_OPTION`) — 8→26 · 5→28 · 6→33. 화면 원문 검사.
* `cli._daily_load` · `/api/settings` — 11-21 오후 묶음 방식으로 바뀌어 `test_daily_load_groups_1121.py` 로 옮겼다.
* `faults._shift_target` 의 `DAILY` 대상에는 B 가 없고 `shift_split` 은 늘 0 이다.
* `/api/settings` 의 `daily_load_at` — HH:MM 이 아니면 400.
DB 없이 — 가짜 접속.
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

import pytest

from datagen import cli, control, faults
from datagen import scenarios as S
from datagen.cells import Cell

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "src/datagen/ui/static/main.html").read_text(encoding="utf-8")


# ── 화면 순서 표 ────────────────────────────────────────────────────────────

def test_the_order_table_has_24_distinct_numbers():
    nos = [r[0] for r in control.SCENARIO_ORDER]
    assert len(nos) == 24 and len(set(nos)) == 24


def test_the_order_table_never_lists_a_retired_number():
    assert not ({r[0] for r in control.SCENARIO_ORDER} & control.RETIRED)


def test_every_non_retired_number_has_a_screen_order():
    listed = {s[0] for s in control.SCENARIOS}
    live = listed - control.RETIRED
    assert live == set(control.SCENARIO_RANK), (live ^ set(control.SCENARIO_RANK))


def test_ranks_are_1_to_24_in_table_order():
    assert sorted(control.SCENARIO_RANK.values()) == list(range(1, 25))
    assert [control.SCENARIO_RANK[r[0]] for r in control.SCENARIO_ORDER] == list(range(1, 25))


def test_every_order_row_has_ko_and_ja_name_and_description():
    for no, name, ja, desc, desc_ja in control.SCENARIO_ORDER:
        assert name and ja and desc and desc_ja, no
        assert not re.search(r"[가-힣]", ja + desc_ja), f"{no}: 일본어 칸에 한글이 섞였다"


def test_the_internal_numbers_are_unchanged_and_all_35_are_still_listed():
    nos = {s[0] for s in control.SCENARIOS}
    assert nos >= set(range(1, 36)) and 36 in nos


# ── 36번 신호 폭주 — 자리만 있고 주입 불가 ─────────────────────────────────────

class _Cur:
    def __init__(self, log):
        self.log = log

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def executemany(self, sql, rows):
        self.log["rows"] = rows

    def execute(self, sql, params=None):
        self.log.setdefault("exec", []).append((sql, params))


class _SeedConn:
    def __init__(self):
        self.log = {}

    def cursor(self):
        return _Cur(self.log)

    def commit(self):
        pass


def _seeded():
    c = _SeedConn()
    control.seed_scenarios(c)
    return {r[0]: r for r in c.log["rows"]}


def test_36_is_ready_with_the_signaling_storm_kind():
    """11-21 오후 — 36번 생성기(`signaling_storm`)가 들어와 PENDING 이 풀렸다. 번호는 폐기가 아니다."""
    row = _seeded()[36]
    assert row[2] == "signaling_storm" and row[4] == "READY" and 36 not in control.RETIRED


def test_36_is_third_on_the_screen():
    assert control.SCENARIO_RANK[36] == 11


def test_seed_writes_the_new_name_and_description_for_ranked_numbers():
    rows = _seeded()
    for no, name, _ja, desc, _dja in control.SCENARIO_ORDER:
        assert rows[no][1] == name and rows[no][9] == desc, no


def test_seed_marks_exactly_the_retired_numbers_as_retired_and_keeps_their_rows():
    rows = _seeded()
    assert {no for no, r in rows.items() if r[4] == "RETIRED"} == set(control.RETIRED)


def test_the_menu_disables_a_scenario_without_a_live_kind_so_36_cannot_be_injected():
    """메뉴 option 은 kind 가 gen 의 `ok` 집합에 있을 때만 활성이다 — kind 가 없는 36번은 「준비 중」으로 비활성"""
    assert 'const live = s.kind && ok.has(s.kind);' in HTML
    assert '(live ? "" : " disabled")' in HTML or '${live ? "" : " disabled"}' in HTML
    assert 'T(" — 준비 중")' in HTML
    assert 'SCN.filter(s => s.status !== "RETIRED").sort(byRank)' in HTML


def test_the_menu_shows_the_screen_order_and_keeps_the_internal_number_as_the_value():
    assert '<option value="${s.no}"' in HTML and "${s.rank ?? s.no}." in HTML


# ── 옵션 표 ────────────────────────────────────────────────────────────────

OPTIONS = {"ue_model": (26, "기종 전국", 8), "dns_degradation": (28, "간헐·없는 주소", 5), "external_server": (33, "4xx 거절", 6)}


def test_options_table_in_the_screen_matches_the_decided_numbers():
    for kind, (no, label, _screen) in OPTIONS.items():
        assert re.search(rf'{kind}:\s*\{{no: {no}, label: "{label}"\}}', HTML), kind


def test_option_numbers_point_at_scenarios_with_the_kind_the_screen_will_send():
    rows = {s[0]: s for s in control.SCENARIOS}
    assert rows[28][2] == "dns_flap" and rows[33][2] == "service_4xx"
    assert rows[26][2] == "ue_model"
    for no in (26, 28, 33):
        assert rows[no][2] in control.KINDS


def test_the_screen_scenario_a_checkbox_belongs_to_is_the_one_the_user_sees():
    """체크박스가 붙는 화면 시나리오(8·5·6)의 내부 번호가 순서 표에 있다 — 폐기 번호(26·28·33)에는 메뉴가 없다"""
    for kind, (_no, _label, screen_rank) in OPTIONS.items():
        scn = [s for s in control.SCENARIOS if s[2] == kind and s[0] not in control.RETIRED]
        assert scn, kind
        assert all(s[0] in control.SCENARIO_RANK for s in scn)


def test_the_option_sends_the_option_number_and_the_option_kind():
    assert "scenario_no: wideOn() ? curOpt().no" in HTML
    assert "function optKind()" in HTML and "SCN.find(x => x.no === o.no)" in HTML


# ── 매일 부하 `_daily_load` — 11-21 오후 「망 조작 · 묶음(시간들 × 역들) · 1시간 단위」로 바뀌어 옮겼다 ──────
# 옛 스위치(`daily_load`·`daily_load_at`)·1차 판(`at|len|대상`) 시험은 지웠다 → `test_daily_load_groups_1121.py`

# ── `DAILY` 대상에는 B 가 없다 ──────────────────────────────────────────────

def _cell(enb, n):
    return Cell(cell_key=f"{enb}-{n}", enb_id=enb, cell_id=n, enb_seq=0, c_uid="", tac=100, plmn="44010", vlan_id="",
                latitude=35.69, longitude=139.70, adm_sido_cd="13", adm_sido_nm="", adm_sgg_cd="13104", adm_sgg_nm="",
                adm_emd_cd="1310400", adm_emd_nm="", geo_status="MAPPED", rat="EUTRAN", mme_id="0001", sgw_id="0037",
                pgw_id="0037", ims_pgw_id="0037", traffic_weight=1.0, display_name="")


@pytest.fixture(autouse=True)
def _shift_site(monkeypatch):
    """24번 자리 설정 — 합성 한 자리(A 하나 · B 둘). 전역이라 시험 뒤 되돌린다"""
    site = S.ShiftSite(name="新宿", from_enb="700001", standby_enbs=("700002", "700003"))
    monkeypatch.setattr(S, "SHIFT", S.ShiftCfg(sites=(site,)))


def _site_cells():
    site = S.SHIFT.sites[0]
    cells = [_cell(site.from_enb, n) for n in range(3)] + [_cell(e, 0) for e in site.standby_enbs]
    return site, cells


def test_daily_target_is_the_a_cells_and_has_no_b():
    site, cells = _site_cells()
    t = faults._shift_target("DAILY", datetime(2026, 11, 21, 8, 0), cells)
    assert t is not None and t.enb_id == site.from_enb
    assert t.cell_keys == {c.cell_key for c in cells if c.enb_id == site.from_enb}
    assert not getattr(t, "shift_to", None)


def test_the_normal_24_target_still_picks_a_b():
    _site, cells = _site_cells()
    t = faults._shift_target("", datetime(2026, 11, 21, 8, 0), cells)
    assert t is not None and t.shift_to


def test_the_daily_target_is_the_same_cells_every_day():
    _site, cells = _site_cells()
    a = faults._shift_target("DAILY", datetime(2026, 11, 21, 8, 0), cells)
    b = faults._shift_target("DAILY", datetime(2026, 11, 22, 8, 0), cells)
    assert a.cell_keys == b.cell_keys and a.enb_id == b.enb_id


def test_daily_with_an_unknown_site_is_refused_not_spread_over_the_country():
    _site, cells = _site_cells()
    assert faults._shift_target("DAILY:SITE:없는자리", datetime(2026, 11, 21, 8, 0), cells) is None
