"""장애 기록 표에 주입 당시 조건 칸을 더한다 — 2026-11-11 (사용자 승인 DDL · 맥스 구현 고정).

`CM_DATAGEN_FAULT` 에 `SCENARIO_NO`·`VOLUME_PCT`·`VIRTUAL_OFFSET_SEC`·`RESOLVED_TARGET`·`INJECT_CONTEXT` 를 더했다.
설정 표(`CM_DATAGEN_CONFIG`)는 현재값 하나뿐이라 15%·50% 회차, 가상 시각(+2시간 30분)을 나중에 알 수 없었다.
**NULL 은 "기록 전 장애"다** — 0 으로 채우면 생성량 0%·오프셋 0 이라는 없는 사실이 된다(graphio 불변규칙 17).

DB 없이 — 가짜 커서로 실제 나갈 SQL 을 본다.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

import pytest

from datagen import cli, control, faults

NOW = datetime(2026, 11, 11, 22, 6, 3)
COLS = ["SCENARIO_NO", "VOLUME_PCT", "VIRTUAL_OFFSET_SEC", "RESOLVED_TARGET", "INJECT_CONTEXT"]


class _Cur:
    """실행된 문장과 인자를 모은다. `SELECT` 는 별칭을 `description` 으로 내고, 칸 존재 확인은 `have` 로 답한다."""

    def __init__(self, have=(), rows=()):
        self.sql: list[tuple[str, list | None]] = []
        self.have = set(have)
        self.rows = list(rows)
        self.description = None
        self._one = None
        self.lastrowid = 77

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.sql.append((sql, params))
        if "information_schema.columns" in sql:
            self._one = (1 if params[1] in self.have else 0,)
        elif sql.startswith("SELECT"):
            head = sql.split(" FROM ", 1)[0]
            cols = [p.split(" AS ")[-1].strip() for p in head[len("SELECT "):].split(", ")]
            self.description = [(c,) for c in cols]

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self.rows


class _Conn:
    def __init__(self, **kw):
        self.cur = _Cur(**kw)
        self.commits = 0

    def cursor(self):
        return self.cur

    def commit(self):
        self.commits += 1


def _stmts(conn, head):
    return [(s, p) for s, p in conn.cur.sql if s.startswith(head)]


# ---------------------------------------------------------------- 1. 칸 정의 한 벌

def test_condition_columns_are_the_five_agreed_ones_and_all_nullable():
    assert [c for c, _ in control.FAULT_COND_COLUMNS] == COLS
    for c, d in control.FAULT_COND_COLUMNS:
        assert "DEFAULT NULL" in d, c
        assert "NULL = 기록 전 장애" in d, f"{c}: NULL 이 무슨 뜻인지 COMMENT 에 있어야 한다"
        assert ";" not in d, f"{c}: `ensure_tables` 가 DDL 을 `;` 로 자른다"


def test_new_environment_ddl_and_existing_environment_alter_use_the_same_definitions():
    """새 환경 `DDL` 과 기존 환경 `_ADDED_COLUMNS` 가 **같은 정의**를 쓴다 — 한쪽만 고쳐 칸이 갈리지 않게."""
    added = {c: d for t, c, d in control._ADDED_COLUMNS if t == control.T_FAULT}
    assert added == dict(control.FAULT_COND_COLUMNS)
    block = control.DDL.split(f"CREATE TABLE IF NOT EXISTS `{control.T_FAULT}`", 1)[1].split(") ENGINE", 1)[0]
    for c, d in control.FAULT_COND_COLUMNS:
        assert f"`{c}` {d}" in block, c
    assert block.count("COMMENT") >= len(COLS)


def test_comment_text_never_breaks_the_statement_split():
    """`DDL.split(";")` 가 자른 조각이 모두 온전한 문장이어야 한다 — COMMENT 안에 `;` 가 없다."""
    for stmt in control.DDL.split(";"):
        assert stmt.count("(") == stmt.count(")"), stmt[:80]
        assert stmt.count("'") % 2 == 0, stmt[:80]


# ---------------------------------------------------------------- 2. ensure_tables

def test_ensure_tables_alters_five_times_when_the_columns_are_missing(monkeypatch):
    monkeypatch.setattr(control, "seed_scenarios", lambda conn: None)
    conn = _Conn(have={"SERIES_STAT"})
    control.ensure_tables(conn)
    alters = [s for s, _ in conn.cur.sql if s.startswith("ALTER TABLE")]
    mine = [a for a in alters if f"`{control.T_FAULT}`" in a]
    assert len(mine) == 5
    assert [re.search(r"ADD COLUMN `(\w+)`", a).group(1) for a in mine] == COLS


def test_ensure_tables_adds_nothing_when_all_columns_exist(monkeypatch):
    monkeypatch.setattr(control, "seed_scenarios", lambda conn: None)
    conn = _Conn(have={"SERIES_STAT", *COLS})
    control.ensure_tables(conn)
    assert not [s for s, _ in conn.cur.sql if s.startswith("ALTER TABLE")]


def test_ensure_tables_adds_only_the_missing_ones(monkeypatch):
    monkeypatch.setattr(control, "seed_scenarios", lambda conn: None)
    conn = _Conn(have={"SERIES_STAT", "SCENARIO_NO", "VOLUME_PCT"})
    control.ensure_tables(conn)
    got = [re.search(r"ADD COLUMN `(\w+)`", s).group(1) for s, _ in conn.cur.sql if s.startswith("ALTER TABLE")]
    assert got == ["VIRTUAL_OFFSET_SEC", "RESOLVED_TARGET", "INJECT_CONTEXT"]


# ---------------------------------------------------------------- 3. add_fault 의 시나리오 번호

def _insert_no(conn):
    s, p = _stmts(conn, "INSERT INTO")[0]
    cols = re.search(r"\(([^)]*)\) VALUES", s).group(1).split(",")
    cols = [c for c in cols if c != "STATUS"]            # STATUS 는 'pending' 리터럴이라 자리표시자가 없다
    assert len(cols) == len(p)
    return dict(zip(cols, p))["SCENARIO_NO"]


def _add(conn, note, **kw):
    return control.add_fault(conn, "sgw_exhaust" if "sgw_exhaust" in control.KINDS else control.KINDS[0],
                             None, NOW, note=note, **kw)


def test_add_fault_takes_the_given_scenario_no():
    conn = _Conn()
    _add(conn, "#3 아무 말", scenario_no=19)
    assert _insert_no(conn) == 19, "받은 값이 메모보다 앞선다"


def test_add_fault_reads_the_note_when_no_number_is_given():
    conn = _Conn()
    _add(conn, "#12 ims-v50n (50% 재주행)")
    assert _insert_no(conn) == 12


def test_add_fault_leaves_null_when_neither_exists_and_never_guesses_from_kind():
    """kind 를 나눠 쓰는 번호가 있다(10·19 · 1·18) — 짐작하면 남의 번호가 적힌다."""
    conn = _Conn()
    _add(conn, "그냥 메모")
    assert _insert_no(conn) is None
    conn = _Conn()
    _add(conn, None)
    assert _insert_no(conn) is None


def test_note_scenario_no_patterns():
    assert faults.note_scenario_no("#19 mme") == 19
    assert faults.note_scenario_no("뭔가 시나리오 7 어쩌고") == 7
    assert faults.note_scenario_no("no10-v8 ...") == 10
    assert faults.note_scenario_no("메모 #5") is None, "앞에 있는 `#n` 만 번호다"
    assert faults.note_scenario_no(None) is None and faults.note_scenario_no("") is None


# ---------------------------------------------------------------- 4. poll_transitions

COND = {"volume_pct": 50, "virtual_offset_sec": 9000,
        "context": {"diurnal": True, "blocked": ["dns"], "reassign": None, "curve": None, "gen_rev": "abc1234"}}


def _poll(cond):
    conn = _Conn()
    control.poll_transitions(conn, NOW, cond=cond)
    return conn


def test_activation_update_carries_the_conditions_in_the_same_statement():
    """전이와 **같은 UPDATE** 에 조건 세 칸이 실린다 — 따로 적으면 그 사이 UI 가 값을 바꿔 전이 시점과 달라진다."""
    conn = _poll(COND)
    ups = _stmts(conn, "UPDATE")
    act = [(s, p) for s, p in ups if "SET STATUS='active'" in s]
    assert len(act) == 1
    s, p = act[0]
    assert "VOLUME_PCT=%s" in s and "VIRTUAL_OFFSET_SEC=%s" in s and "INJECT_CONTEXT=%s" in s
    assert "WHERE STATUS='pending'" in s
    assert p[1] == 50 and p[2] == 9000
    assert json.loads(p[3]) == COND["context"]
    assert not [1 for s2, _ in ups if "VOLUME_PCT" in s2 and s2 is not s], "조건을 쓰는 UPDATE 가 따로 있으면 안 된다"


def test_only_pending_rows_are_written_so_started_faults_keep_their_values():
    s = _stmts(_poll(COND), "UPDATE")[0][0]
    assert "STATUS='pending'" in s and "START_TIME <= %s" in s


def test_without_cond_the_condition_columns_are_not_touched():
    """`cond` 가 없는 경로는 칸을 건드리지 않는다 — NULL 은 "기록 전"이다."""
    conn = _poll(None)
    assert not [s for s, _ in conn.cur.sql if "VOLUME_PCT" in s or "INJECT_CONTEXT" in s]
    assert "UPDATED_TIME=%s WHERE" in _stmts(conn, "UPDATE")[0][0].replace("' ", "' ").replace("  ", " ") \
        or "UPDATED_TIME=%s " in _stmts(conn, "UPDATE")[0][0]


@pytest.mark.parametrize("vol,want", [(50, 50), (49.6, 50), (300, 255), (-5, 0), (None, None)])
def test_volume_is_clamped_to_tinyint_unsigned(vol, want):
    p = _stmts(_poll({**COND, "volume_pct": vol}), "UPDATE")[0][1]
    assert p[1] == want


def test_missing_values_are_written_as_null_not_zero():
    p = _stmts(_poll({"volume_pct": None, "virtual_offset_sec": None, "context": None}), "UPDATE")[0][1]
    assert p[1] is None and p[2] is None and p[3] is None


def test_zero_offset_is_a_real_value_and_stays_zero():
    p = _stmts(_poll({**COND, "virtual_offset_sec": 0}), "UPDATE")[0][1]
    assert p[2] == 0


def test_active_select_includes_scenario_no():
    conn = _poll(COND)
    s = _stmts(conn, "SELECT")[0][0]
    assert "SCENARIO_NO AS scenario_no" in s and "NOTE AS note" in s


# ---------------------------------------------------------------- 5. record_resolved

def test_record_resolved_writes_only_empty_rows():
    conn = _Conn()
    done = control.record_resolved(conn, {287: "MME-6 · 셀 773개", 288: "MME-1 · APN"})
    ups = _stmts(conn, "UPDATE")
    assert len(ups) == 2 and done == [287, 288]
    for s, _ in ups:
        assert "RESOLVED_TARGET IS NULL" in s and "WHERE FAULT_ID=%s" in s
    assert conn.commits == 1


def test_record_resolved_truncates_at_255_and_skips_blank_labels():
    conn = _Conn()
    done = control.record_resolved(conn, {1: "가" * 400, 2: "", 3: None})
    ups = _stmts(conn, "UPDATE")
    assert len(ups) == 1 and len(ups[0][1][0]) == 255 and done == [1]


# ---------------------------------------------------------------- 6. faults.scenario_no 우선순위

def test_scenario_no_prefers_the_column_over_the_note_over_the_kind():
    assert faults.scenario_no({"scenario_no": 19, "note": "#10 x", "kind": "mme_overload"}) == 19
    assert faults.scenario_no({"scenario_no": None, "note": "#10 x", "kind": "mme_overload"}) == 10
    assert faults.scenario_no({"scenario_no": 0, "note": "#10 x", "kind": "mme_overload"}) == 0
    # 칸도 메모도 없으면 kind — 여럿이 쓰면 원본 번호(19 → 10)
    assert faults.scenario_no({"note": None, "kind": "mme_overload"}) == 10


# ---------------------------------------------------------------- 7. gen_rev

def test_gen_rev_env_wins(monkeypatch):
    monkeypatch.setenv("GEN_REV", "6a8d8c1-dirty")
    assert cli.gen_rev() == "6a8d8c1-dirty"


@pytest.mark.parametrize("val", ["unknown", "", "  "])
def test_gen_rev_ignores_unknown_and_blank_and_falls_back_to_git(monkeypatch, val):
    monkeypatch.setenv("GEN_REV", val)
    seen = []

    class R:
        returncode, stdout = 0, "deadbee\n"

    import subprocess
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: seen.append(a) or R())
    assert cli.gen_rev() == "deadbee" and seen


def test_gen_rev_is_none_when_git_is_missing_or_fails(monkeypatch):
    """모르는 판을 지어내지 않는다."""
    import subprocess
    monkeypatch.delenv("GEN_REV", raising=False)

    def boom(*a, **k):
        raise OSError("no git")
    monkeypatch.setattr(subprocess, "run", boom)
    assert cli.gen_rev() is None

    class R:
        returncode, stdout = 128, "fatal"
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: R())
    assert cli.gen_rev() is None


def test_fault_cond_bundles_the_settings():
    c = cli._fault_cond(50, 9000, True, {"dns", "http"}, "", [1, 2], "rev")
    assert c["volume_pct"] == 50 and c["virtual_offset_sec"] == 9000
    ctx = c["context"]
    assert ctx["diurnal"] is True and ctx["blocked"] == ["dns", "http"] and ctx["reassign"] is None
    assert ctx["curve"] == [1, 2] and ctx["gen_rev"] == "rev"
    assert cli._fault_cond(15, 0, False, None, '{"a": 1}', None, None)["context"] == {
        "diurnal": False, "blocked": [], "reassign": {"a": 1}, "curve": None, "gen_rev": None}


def test_dockerfile_takes_the_rev_as_a_build_arg_at_the_end():
    src = Path(__file__).resolve().parents[1].joinpath("Dockerfile").read_text(encoding="utf-8")
    assert "ARG GEN_REV=unknown" in src and "ENV GEN_REV=${GEN_REV}" in src
    assert src.index("ARG GEN_REV") > src.index("USER datagen"), "맨 끝에 — 판이 바뀔 때 앞 층 캐시가 깨지지 않게"


# ---------------------------------------------------------------- 8. /api/faults

def test_api_faults_parses_the_json_column_and_keeps_broken_text(monkeypatch):
    from datagen.ui import faults_api as F

    rows = [{"fault_id": 3, "inject_context": '{"gen_rev":"x","diurnal":true}'},
            {"fault_id": 2, "inject_context": "{깨진"},
            {"fault_id": 1, "inject_context": None},
            {"fault_id": 0, "inject_context": {"already": 1}}]

    class C:
        def close(self):
            pass
    monkeypatch.setattr(F, "_connect", lambda: C())
    monkeypatch.setattr(F, "list_faults", lambda conn, limit=50: [dict(r) for r in rows])
    monkeypatch.setattr(F, "get_config", lambda conn, key: "{}")
    got = {r["fault_id"]: r["inject_context"] for r in F.api_faults(limit=4)}
    assert got[3] == {"gen_rev": "x", "diurnal": True}
    assert got[2] == "{깨진"
    assert got[1] is None
    assert got[0] == {"already": 1}


def test_fault_request_accepts_a_scenario_no():
    from datagen.ui.faults_api import FaultReq
    assert FaultReq(kind="x", scenario_no=12).scenario_no == 12
    assert FaultReq(kind="x").scenario_no is None


def test_api_add_fault_passes_the_scenario_no_through():
    src = Path(__file__).resolve().parents[1].joinpath("src/datagen/ui/faults_api.py").read_text(encoding="utf-8")
    assert "scenario_no=req.scenario_no" in src


# ---------------------------------------------------------------- 9. 화면

HTML = Path(__file__).resolve().parents[1].joinpath("src/datagen/ui/static/main.html").read_text(encoding="utf-8")


def test_fault_list_has_seven_columns_everywhere():
    assert HTML.count('colspan="7"') == 3 and 'colspan="6"' not in HTML
    head = HTML.split('<tr id="fhead">', 1)[1].split("</tr>", 1)[0]
    assert head.count("<th>") == 7 and "<th>생성량</th>" in head
    assert HTML.split('<colgroup><col style="width:46px">', 1)[1].split("</colgroup>", 1)[0].count("<col") == 6


def test_null_shows_as_before_recorded_never_as_zero():
    assert 'f.volume_pct == null ? T("기록 전")' in HTML
    assert 'if (f.virtual_offset_sec == null) return T("기록 전")' in HTML
    assert 'T("실제 대상") + " " + (f.resolved_target || T("기록 전"))' in HTML


def test_target_column_and_sort_use_the_resolved_target_for_auto_faults():
    assert 'f.resolved_target ? T("자동") + " → " + f.resolved_target : T("자동")' in HTML
    assert 'v: f => f.target || f.resolved_target || ""' in HTML
    assert 'f.volume_pct == null ? -1 : f.volume_pct' in HTML, "기록 전은 정렬에서 맨 아래"


def test_injection_sends_the_scenario_no_as_a_field():
    assert 'scenario_no: parseInt($("kind").value, 10) || null' in HTML


def test_recorded_volume_opens_that_tab_before_any_judgement_exists():
    assert "f.volume_pct != null && SCN_VOLS.includes(f.volume_pct)" in HTML
    assert 'scnHref(s.no, f.volume_pct, "F-" + id)' in HTML


@pytest.mark.parametrize("key", ["기록 전", "실제 대상", "생성기", "주입 때 생성량 {v}% 탭으로 연다"])
def test_japanese_texts_exist(key):
    from datagen.ui import i18n_ja
    assert i18n_ja._JA_COMMON.get(key), key


def test_detail_header_has_the_condition_line():
    assert '<p id="fm-c"></p>' in HTML and '$("fm-c").textContent' in HTML
