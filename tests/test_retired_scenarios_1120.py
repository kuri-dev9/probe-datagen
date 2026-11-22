"""폐기(RETIRED) 시나리오 · 추가 가입자 API (11-20 · 맥스 구현 · 에스터 시험).

번호는 남기고 주입 메뉴에서만 뺀다 — 행을 지우면 옛 장애의 번호·이름이 사라진다. DB 없이 가짜 접속과 화면 원문 검사.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException

from datagen import control, faults
from datagen.ui import status_api

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "src/datagen/ui/static/main.html").read_text(encoding="utf-8")
# 11-21 — 24(매일 부하 스위치) · 28(5번 옵션) · 33(6번 옵션)이 더해졌다
RET = {3, 4, 11, 18, 19, 20, 24, 25, 26, 28, 33, 35}


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


class _Conn:
    def __init__(self):
        self.log = {}

    def cursor(self):
        return _Cur(self.log)

    def commit(self):
        pass

    def close(self):
        pass


def test_retired_set_is_exactly_the_decided_numbers():
    assert control.RETIRED == frozenset(RET)


def test_28_and_33_are_retired_after_their_scenes_moved_into_options_of_5_and_6():
    """11-21 — 장면은 5번 「간헐·없는 주소」·6번 「4xx 거절」 옵션으로 옮겼다(옵션이 그 번호·kind 로 보낸다)"""
    assert {28, 33} <= control.RETIRED
    assert 24 in control.RETIRED, "24번은 시나리오가 아니라 매일 부하 스위치다"


def test_retired_rows_stay_in_the_scenario_table_and_get_status_retired():
    c = _Conn()
    control.seed_scenarios(c)
    rows = {r[0]: r for r in c.log["rows"]}
    assert RET <= set(rows), "폐기 번호의 행이 목록에서 빠졌다 — 빠지면 다음 기동 때 DB 행이 지워진다"
    for no in RET:
        assert rows[no][4] == "RETIRED", no
    for no, r in rows.items():
        if no not in RET:
            assert r[4] != "RETIRED", no
    # DELETE 는 목록에 없는 번호만 — 폐기 번호가 그 목록 안에 있다
    sql, params = c.log["exec"][-1]
    assert "DELETE" in sql and RET <= set(params)


def test_every_number_up_to_35_is_still_listed():
    nos = {s[0] for s in control.SCENARIOS}
    assert nos >= set(range(1, 36))


def test_kind_of_retired_rows_is_still_valid_so_startup_does_not_fail():
    for no, _n, kind, *_ in control.SCENARIOS:
        if no in RET and kind is not None:
            assert kind in control.KINDS, no


def test_scenario_26_still_takes_the_model_wide_branch():
    assert 26 in faults._MODEL_WIDE_SCENARIOS


# ── 화면 ────────────────────────────────────────────────────────

def test_menu_hides_retired_but_keeps_them_in_SCN():
    assert 'SCN.filter(s => s.status !== "RETIRED")' in HTML
    assert "SCN = sc;" in HTML                      # 과거 장애 이름 붙이기는 전체 목록


def test_option_checkbox_table_maps_the_screen_scenarios_to_the_retired_numbers():
    """8→26 기종 전국 · 5→28 간헐·없는 주소 · 6→33 4xx 거절 — 체크하면 그 번호와 그 번호 행의 kind 로 보낸다"""
    assert 'id="model-wide"' in HTML
    assert "const SCN_OPTION = {" in HTML
    assert 'ue_model:        {no: 26, label: "기종 전국"}' in HTML
    assert 'dns_degradation: {no: 28, label: "간헐·없는 주소"}' in HTML
    assert 'external_server: {no: 33, label: "4xx 거절"}' in HTML
    assert "function wideOn() { return !!curOpt() && $(\"model-wide\").checked; }" in HTML
    assert "scenario_no: wideOn() ? curOpt().no" in HTML
    assert "note: `#${wideOn() ? curOpt().no" in HTML


def test_option_numbers_are_retired_and_their_kinds_are_valid():
    rows = {s[0]: s for s in control.SCENARIOS}
    for no in (26, 28, 33):
        assert no in control.RETIRED
        assert rows[no][2] in control.KINDS, no
    assert rows[28][2] == "dns_flap" and rows[33][2] == "service_4xx"


# ── 추가 가입자 API ───────────────────────────────────────────────

@pytest.fixture
def api(monkeypatch):
    saved = {}
    monkeypatch.setattr(status_api, "_connect", lambda: _Conn())
    monkeypatch.setattr(status_api, "set_config", lambda conn, k, v: saved.__setitem__(k, v))
    monkeypatch.setattr(status_api, "_pem_cfg", lambda: {"extra_lte_max": 100_000, "extra_lte_subs": 10_000})
    return saved


def _post(n):
    return status_api.api_set_settings(status_api.SettingsReq(extra_lte_subs=n))


@pytest.mark.parametrize("n", [9_999, 0, -1, 100_001])
def test_extra_subs_out_of_range_is_400(api, n):
    with pytest.raises(HTTPException) as e:
        _post(n)
    assert e.value.status_code == 400
    assert "extra_lte_subs" not in api


@pytest.mark.parametrize("n", [10_000, 55_000, 100_000])
def test_extra_subs_in_range_is_saved(api, n):
    _post(n)
    assert api["extra_lte_subs"] == str(n)


def test_extra_range_comes_from_config(monkeypatch):
    monkeypatch.setattr(status_api, "_pem_cfg", lambda: {"extra_lte_max": 50_000})
    assert status_api._extra_range() == (10_000, 50_000, 10_000)


def test_extra_bar_is_in_the_settings_page():
    assert "saveExtra" in HTML and "extra_lte_subs" in HTML
