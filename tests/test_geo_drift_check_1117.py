"""gen 기동 구 검사 (`pemdb1.geo_drift`·`check_geo_drift`) — 2026-11-17 맥스 · 에스터 시험.

생성 범위 `scope.gu` 는 셀 마스터의 `GU_ID` 로 셀을 고른다. 마스터가 한국 체계 구를 들고 있어 「12구」 설정을 지켜도
12구 밖 9개 구의 셀 약 560개가 생성됐고, 사후에 맞추자 eNB 245개가 조용해져 가짜 무호가 났다. 그래서 **기준이 틀린 채로
생성을 시작하지 않는다**(기본 `stop`). 짝짓기 규칙은 graphio `geo_coverage.master_drift` 와 같다(이름 쌍 · 코드 비교 없음).
검사를 못 하면(표 없음 · ADM2 0행 · 질의 실패) 0 이 아니라 「모름」이라 경고만 하고 진행한다. 가짜 연결 — DB 없이.
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

from datagen import cli
from datagen import pemdb1 as P

ROOT = Path(__file__).resolve().parents[1]


class _Cur:
    def __init__(self, conn):
        self.c = conn

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql):
        self.c.sqls.append(sql)
        if self.c.boom:
            raise RuntimeError("Table 'CM_CELL_GEO_MAP' doesn't exist")
        self.last = sql

    def fetchone(self):
        return (self.c.adm2_rows,)

    def fetchall(self):
        return self.c.rows


class _Conn:
    def __init__(self, rows, adm2_rows=1, boom=False):
        self.rows, self.adm2_rows, self.boom, self.sqls = rows, adm2_rows, boom, []

    def cursor(self):
        return _Cur(self)


SAME = ("東京都", "港区", "東京都", "港区", "13103", 100)
DIFF = ("東京都", "中央区", "東京都", "江東区", "13108", 414)
CROSS = ("千葉県", "中央区", "東京都", "中央区", "13102", 7)       # 이름만 같고 시도가 다르다 — 어긋남
NOMAP = ("東京都", "新宿区", None, None, None, 5)


# ── geo_drift — 세기만 한다 ──────────────────────────────────────────────────

def test_geo_drift_counts_by_name_pair_and_keeps_unknown_apart():
    r = P.geo_drift(_Conn([SAME, DIFF, CROSS, NOMAP, ("東京都", "品川区", "東京都", "港区", "13103", 223)]), "jp", sample=2)
    assert r["cells"] == 100 + 414 + 7 + 5 + 223 and r["differs"] == 414 + 7 + 223 and r["no_map"] == 5
    assert [x["cells"] for x in r["top"]] == [414, 223]
    assert r["top"][0] == {"master": "東京都 中央区", "geo_map": "東京都 江東区", "cells": 414}


def test_the_same_gu_name_in_another_prefecture_is_a_drift():
    r = P.geo_drift(_Conn([CROSS]), "jp")
    assert r["differs"] == 7 and r["top"][0]["master"] == "千葉県 中央区"


def test_it_never_compares_codes():
    """코드 체계가 다르다(마스터 東京 11 · e-Stat 東京 13). 코드가 달라도 이름이 같으면 일치다."""
    assert P.geo_drift(_Conn([("東京都", "港区", "東京都", "港区", "99999", 9)]), "jp")["differs"] == 0


def test_the_query_only_reads_and_joins_on_the_cell_key_and_adm_levels():
    c = _Conn([SAME])
    P.geo_drift(c, "jp")
    sql = " ".join(c.sqls).upper()
    assert "UPDATE" not in sql and "DELETE" not in sql and "INSERT" not in sql
    q = c.sqls[-1]
    assert "m.ENB_ID = l.ENB_ID AND m.CELLNUM = l.SECTOR" in q and "AREA_LEVEL = 'ADM2'" in q and "AREA_LEVEL = 'ADM1'" in q


@pytest.mark.parametrize("conn", [_Conn([], adm2_rows=0), _Conn([], boom=True)])
def test_a_missing_or_empty_geo_map_is_unknown_not_zero(conn, capsys):
    assert P.geo_drift(conn, "jp") is None


def test_a_failed_query_says_so(capsys):
    P.geo_drift(_Conn([], boom=True), "jp")
    assert "구 검사 못 함" in capsys.readouterr().out


# ── check_geo_drift — 멈춤 · 경고 ───────────────────────────────────────────

def test_a_match_passes_with_a_one_line_confirmation(capsys):
    r = P.check_geo_drift(_Conn([SAME]), "jp", "stop")
    assert r["differs"] == 0 and "✓ 구 검사 — 셀 100 모두" in capsys.readouterr().out


def test_stop_refuses_to_start_when_cells_drift_and_says_how_to_fix_it():
    with pytest.raises(RuntimeError) as e:
        P.check_geo_drift(_Conn([SAME, DIFF]), "jp", "stop")
    msg = str(e.value)
    assert "414" in msg and "東京都 中央区 → 東京都 江東区" in msg
    assert "master_gu_align.py --dry-run" in msg and "--apply --backup" in msg and "geo_drift_check: warn" in msg


def test_warn_reports_and_continues(capsys):
    r = P.check_geo_drift(_Conn([SAME, DIFF]), "jp", "warn")
    out = capsys.readouterr().out
    assert r["differs"] == 414 and out.lstrip().startswith("!") and "master_gu_align" in out and "멈춘다" not in out


@pytest.mark.parametrize("mode", ["stop", "warn"])
def test_an_unverifiable_check_only_warns_in_both_modes(mode, capsys):
    assert P.check_geo_drift(_Conn([], adm2_rows=0), "jp", mode) is None
    assert "모름 · 진행" in capsys.readouterr().out


def test_stop_does_not_halt_on_cells_with_no_map_alone(capsys):
    """지도 귀속이 없는 셀은 어긋남이 아니라 모름이다 — 어긋난 셀이 없으면 멈추지 않고 알린다."""
    r = P.check_geo_drift(_Conn([SAME, NOMAP]), "jp", "stop")
    out = capsys.readouterr().out
    assert r["no_map"] == 5 and r["differs"] == 0 and "귀속 ADM2 없음 5" in out


@pytest.mark.parametrize("bad", ["", "STOP", "off", "none", "yes"])
def test_an_unknown_mode_is_rejected_before_any_query(bad):
    c = _Conn([SAME])
    with pytest.raises(RuntimeError, match="geo_drift_check"):
        P.check_geo_drift(c, "jp", bad)
    assert c.sqls == []


def test_the_modes_are_stop_and_warn():
    assert P.GEO_DRIFT_MODES == ("stop", "warn")


# ── 연결 ─────────────────────────────────────────────────────────────────────

def test_the_check_runs_right_after_the_cells_are_loaded_and_before_anything_is_generated():
    src = inspect.getsource(P.PemdbGen.__init__)
    load = src.index("load_cells(")
    check = src.index("check_geo_drift(conn, locale, geo_drift_check)")
    assert load < check and check - load < 400            # 셀을 읽은 직후 — 그 사이에 다른 일이 끼지 않는다
    assert "self.cells" not in src[load:check].replace("self.cells =", "")


def test_the_default_mode_of_the_constructor_is_stop():
    assert inspect.signature(P.PemdbGen.__init__).parameters["geo_drift_check"].default == "stop"


def test_all_three_call_sites_pass_the_setting_through():
    src = inspect.getsource(cli)
    assert src.count('geo_drift_check=str(pem.get("geo_drift_check", "stop"))') == 3
    assert src.count("P.PemdbGen(") == 3


def test_the_shipped_config_stops_by_default():
    import yaml
    cfg = yaml.safe_load((ROOT / "config" / "datagen.yaml").read_text(encoding="utf-8"))
    assert cfg["pemdb1"]["geo_drift_check"] == "stop"


def test_the_pairing_rule_is_documented_next_to_graphio_s_so_the_two_stay_equal():
    doc = P.geo_drift.__doc__
    for part in ("master_drift", "(ENB_ID, SECTOR)", "(ENB_ID, CELLNUM)", "이름 쌍", "코드로 견주지 않는다", "no_map"):
        assert part in doc, part
