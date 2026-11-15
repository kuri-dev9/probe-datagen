"""셀 마스터 구 맞춤 도구(`tools/master_gu_align.py`) — 2026-11-15 사용자 결정 안 가 (동구 작성 · 에스터 시험).

운영 DB 에 UPDATE 를 쓰는 도구라 **가짜 DB 로만** 돌린다 — 실제 DB 에는 dry-run 도 하지 않는다.
짝짓기는 (시도, 구 이름)이다(구 이름만으로는 `中央区` 가 도쿄·치바에 다 있다). 쓰기는 UPDATE 뿐이고 옛 값일 때만 바꾸며,
계획과 다르면 ROLLBACK 하고, 원장을 먼저 남겨 되돌릴 수 있게 한다.
"""
from __future__ import annotations

import ast
import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

TOOL = Path(__file__).resolve().parents[1] / "tools" / "master_gu_align.py"
spec = importlib.util.spec_from_file_location("master_gu_align", TOOL)
M = importlib.util.module_from_spec(spec)
spec.loader.exec_module(M)


# ── 가짜 DB ────────────────────────────────────────────────────────────────────────────────
# 도쿄(CITY 1): 港区 GU 10 · 中央区 GU 11 · 江東区 GU 12 / 치바현(CITY 2): 中央区 GU 20 · 習志野市 GU 21
CITY = [(1, "東京都"), (2, "千葉県")]
GU = [(1, 10, "港区"), (1, 11, "中央区"), (1, 12, "江東区"), (2, 20, "中央区"), (2, 21, "習志野市")]
DONG = [(1, 10, 100), (1, 11, 110), (1, 12, 120), (2, 20, 200)]
UNIT = [("13103", "港区", "東京都"), ("13102", "中央区", "東京都"), ("13108", "江東区", "東京都"),
        ("12101", "中央区", "千葉県"), ("99999", "幽霊区", "東京都"), ("13999", "どこか", None)]
# (enb, sector) → (AREA_CD, METHOD) — B안 ADM2
BMAP = {("1", "1"): ("13103", "POLYGON"),      # 마스터 中央区(1,11) → 港区 : 바뀜
        ("2", "1"): ("13108", "NEAREST"),      # 마스터 中央区(1,11) → 江東区 : 바뀜(NEAREST)
        ("3", "1"): ("12101", "POLYGON"),      # 마스터 도쿄 中央区(1,11) → 치바 中央区(2,20) : 시도까지 맞춰야 함
        ("4", "1"): ("13103", "POLYGON"),      # 마스터 港区 그대로 : 이미 같음
        ("5", "1"): ("99999", "POLYGON"),      # CM_GU_JP 에 없는 구 : 건너뜀
        ("6", "1"): ("13999", "POLYGON")}      # 부모 시도를 모름 : 건너뜀
MASTER_ROWS = [("1", 1, 1, 11, 110), ("2", 1, 1, 11, 110), ("3", 1, 1, 11, 110), ("4", 1, 1, 10, 100),
               ("5", 1, 1, 11, 110), ("6", 1, 1, 11, 110), ("7", 1, 1, 11, 110)]     # 7: B안 행 없음


class FakeCur:
    def __init__(self, conn):
        self.c = conn
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.c.log.append((sql, params))
        s = " ".join(sql.split())
        if s.startswith("UPDATE"):
            if self.c.ledger_must_exist is not None:
                assert self.c.ledger_must_exist.exists(), "원장을 쓰기 전에 남겨야 한다"
            self.c.updates.append((s, params))
            return self.c.update_result(params)
        assert s.upper().startswith("SELECT"), s
        if "FROM CM_CITY_JP" in s:
            self._rows = CITY
        elif "FROM CM_GU_JP" in s:
            self._rows = self.c.gu
        elif "FROM CM_DONG_JP" in s:
            self._rows = DONG
        elif "FROM CM_GEO_UNIT" in s:
            self._rows = UNIT
        elif "FROM CM_CELL_GEO_MAP" in s:
            self._rows = [(e, n, cd, m) for (e, n), (cd, m) in BMAP.items()]
        elif f"FROM {M.MASTER}" in s:
            self._rows = [r[:1] + (str(r[1]),) + r[2:] for r in MASTER_ROWS]
        else:
            raise AssertionError(s)
        return len(self._rows)

    def fetchall(self):
        return list(self._rows)


class FakeConn:
    def __init__(self, gu=GU, update_result=None):
        self.log, self.updates, self.gu = [], [], gu
        self.commits = self.rollbacks = 0
        self.closed = False
        self.ledger_must_exist = None
        self.update_result = update_result or (lambda p: 1)

    def cursor(self):
        return FakeCur(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def _scope(monkeypatch):
    monkeypatch.setattr(M, "_scope_gu", lambda: {10, 11})      # 생성 범위 구 — 들어오고 나가는 셀만 센다


def _plan(conn=None):
    conn = conn or FakeConn()
    with conn.cursor() as cur:
        return M.plan(cur)


# ── 짝짓기 ─────────────────────────────────────────────────────────────────────────────────

def test_pairing_uses_prefecture_and_ward_name():
    p = _plan()
    got = {(c["enb"]): (tuple(c["old"]), tuple(c["new"])) for c in p["change"]}
    assert got["1"] == ((1, 11), (1, 10))                          # 中央区 → 港区
    assert got["2"] == ((1, 11), (1, 12))                          # → 江東区
    assert got["3"] == ((1, 11), (2, 20))                          # 치바현 中央区 — 도쿄 11 이 아니라 치바 20


def test_same_ward_name_in_two_prefectures_is_told_apart():
    """구 이름만으로 짝지으면 `中央区` 가 둘이라 건너뛰거나 틀린다 — 시도 이름을 함께 본다."""
    p = _plan()
    assert [c for c in p["change"] if c["enb"] == "3"][0]["new"] == [2, 20]
    assert not any("짝 2개" in w for w in p["skip"])


def test_cells_without_a_pair_are_skipped_and_reported_not_changed():
    p = _plan()
    assert {c["enb"] for c in p["change"]} == {"1", "2", "3"}
    why5 = next(w for w in p["skip"] if "幽霊区" in w)
    assert "CM_GU_JP 에 없음" in why5 and p["skip_cells"][why5] == ["5-1"]
    assert any(w.startswith("? ") for w in p["skip"])               # 부모 시도를 모르면 짝이 없다


def test_two_candidate_pairs_are_skipped():
    gu = GU + [(1, 13, "港区")]                                    # 도쿄에 港区 가 둘
    p = _plan(FakeConn(gu=gu))
    assert "1" not in {c["enb"] for c in p["change"]}              # 港区 로 가야 할 셀 — 짝이 둘이라 건너뜀
    assert any("짝 2개" in w for w in p["skip"])


def test_already_same_and_no_b_row_are_counted_not_changed():
    p = _plan()
    assert p["same"] == 1 and p["no_b"] == 1 and p["total"] == 7


def test_method_split_and_scope_and_dong_counts():
    p = _plan()
    assert sorted(c["method"] for c in p["change"]) == ["NEAREST", "POLYGON", "POLYGON"]
    # 생성 범위(10·11): 1(11→10 안→안) 2(11→12 나감) 3(11→20 나감) — 들어오는 셀은 없다
    assert p["leave"] == 2 and p["enter"] == 0 and p["scope"] == [10, 11]
    # 옛 DONG 110 은 새 구 (1,10)·(1,12)·(2,20) 아래에 없다
    assert p["dong_lost"] == 3


def test_dry_run_plan_issues_selects_only():
    c = FakeConn()
    _plan(c)
    assert c.log and all(s.lstrip().upper().startswith("SELECT") for s, _ in c.log)
    assert c.updates == [] and c.commits == 0


# ── apply ──────────────────────────────────────────────────────────────────────────────────

def test_apply_writes_the_ledger_first_then_updates_with_old_value_guard(tmp_path):
    c = FakeConn()
    p = _plan(c)
    ledger = tmp_path / "led.json"
    c.ledger_must_exist = ledger                                    # UPDATE 가 나가는 순간 원장이 있어야 한다
    M.apply(c, p, ledger)
    doc = json.loads(ledger.read_text(encoding="utf-8"))
    assert doc["table"] == M.MASTER and len(doc["rows"]) == 3 and doc["rows"][0]["old"] == [1, 11]
    assert len(c.updates) == 3 and c.commits == 1 and c.rollbacks == 0
    sql, params = c.updates[0]
    assert re.search(r"WHERE ENB_ID = %s AND SECTOR = %s AND CITY_ID = %s AND GU_ID = %s", sql)
    assert sql.startswith(f"UPDATE {M.MASTER} SET CITY_ID = %s, GU_ID = %s")
    assert params == (1, 10, "1", 1, 1, 11)                        # 새 시·구, 셀, 옛 시·구


def test_apply_never_touches_dong_id_or_other_columns():
    c = FakeConn()
    M.apply(c, _plan(c), Path("/tmp/_gu_led_test.json"))
    for sql, _ in c.updates:
        assert "DONG_ID" not in sql and "SET CITY_ID = %s, GU_ID = %s" in sql
        assert sql.count("SET") == 1


def test_apply_rolls_back_when_row_count_differs_from_the_plan(tmp_path):
    c = FakeConn(update_result=lambda p: 0 if p[2] == "2" else 1)   # 한 행이 그 사이 바뀌어 0행
    with pytest.raises(SystemExit) as e:
        M.apply(c, _plan(c), tmp_path / "led.json")
    assert "ROLLBACK" in str(e.value)
    assert c.rollbacks == 1 and c.commits == 0
    assert (tmp_path / "led.json").exists()                         # 원장은 남아 있다(쓰기 전에 남겼다)


def test_apply_with_nothing_to_change_commits_without_updates(tmp_path):
    c = FakeConn()
    M.apply(c, {"change": []}, tmp_path / "led.json")
    assert c.updates == [] and c.commits == 1


# ── restore ────────────────────────────────────────────────────────────────────────────────

def test_restore_swaps_old_and_new_and_guards_on_the_current_value(tmp_path):
    ledger = tmp_path / "led.json"
    c = FakeConn()
    M.apply(c, _plan(c), ledger)
    r = FakeConn(update_result=lambda p: 1)
    M.restore(r, ledger)
    assert len(r.updates) == 3 and r.commits == 1
    sql, params = r.updates[0]
    assert "WHERE ENB_ID = %s AND SECTOR = %s AND CITY_ID = %s AND GU_ID = %s" in sql
    assert params == (1, 11, "1", 1, 1, 10)                        # 옛 시·구를 쓰고, 지금(새) 값일 때만


def test_restore_skips_rows_whose_value_changed_since(tmp_path, capsys):
    ledger = tmp_path / "led.json"
    c = FakeConn()
    M.apply(c, _plan(c), ledger)
    r = FakeConn(update_result=lambda p: 0 if p[2] == "3" else 1)
    M.restore(r, ledger)
    out = capsys.readouterr().out
    assert "2/3" in out and r.commits == 1                         # 바뀐 한 행은 건드리지 않고 나머지는 되돌린다


# ── 쓰는 것은 UPDATE 뿐이다 ────────────────────────────────────────────────────────────────

def test_no_insert_delete_or_ddl_anywhere_in_the_tool():
    tree = ast.parse(TOOL.read_text(encoding="utf-8"))
    doc_ids = {id(n.body[0].value) for n in ast.walk(tree)
               if isinstance(n, (ast.Module, ast.FunctionDef)) and n.body and isinstance(n.body[0], ast.Expr)
               and isinstance(n.body[0].value, ast.Constant) and isinstance(n.body[0].value.value, str)}
    bad = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)
           and id(n) not in doc_ids
           and re.match(r"\s*(INSERT|DELETE|ALTER|DROP|CREATE|TRUNCATE|REPLACE)\b", n.value, re.I)]
    assert bad == []


def test_every_statement_in_all_modes_is_select_or_update(tmp_path):
    c = FakeConn()
    M.apply(c, _plan(c), tmp_path / "l.json")
    M.restore(c, tmp_path / "l.json")
    kinds = {s.split()[0].upper() for s, _ in c.log}
    assert kinds <= {"SELECT", "UPDATE"}


# ── main — 인자 · 안전장치 ──────────────────────────────────────────────────────────────────

def _main(monkeypatch, tmp_path, *argv, conn=None):
    conn = conn or FakeConn()
    monkeypatch.setattr(M, "_connect", lambda: conn)
    monkeypatch.setattr(M, "OUT", tmp_path)
    monkeypatch.setattr(sys, "argv", ["master_gu_align.py", *argv])
    return M.main(), conn


def test_dry_run_reads_only_and_closes_the_snapshot(monkeypatch, tmp_path):
    rc, c = _main(monkeypatch, tmp_path, "--dry-run")
    assert rc == 0 and c.updates == [] and c.commits == 0 and c.rollbacks >= 1 and c.closed
    assert list(tmp_path.glob("master_gu_align-*.csv"))            # CSV 는 남긴다
    assert not list(tmp_path.glob("*.json"))                       # dry-run 은 원장을 만들지 않는다


def test_apply_without_backup_is_refused_before_any_write(monkeypatch, tmp_path, capsys):
    rc, c = _main(monkeypatch, tmp_path, "--apply")
    assert rc == 2 and c.updates == [] and c.commits == 0
    assert "--backup" in capsys.readouterr().out
    assert not list(tmp_path.glob("*.json"))


def test_apply_with_backup_writes_ledger_and_commits(monkeypatch, tmp_path):
    rc, c = _main(monkeypatch, tmp_path, "--apply", "--backup")
    assert rc == 0 and len(c.updates) == 3 and c.commits == 1 and c.closed
    assert len(list(tmp_path.glob("master_gu_align-*.json"))) == 1


def test_restore_mode_does_not_plan(monkeypatch, tmp_path):
    led = tmp_path / "led.json"
    c0 = FakeConn()
    M.apply(c0, _plan(c0), led)
    rc, c = _main(monkeypatch, tmp_path, "--restore", str(led))
    assert rc == 0 and c.commits == 1 and len(c.updates) == 3
    assert not any(s.lstrip().upper().startswith("SELECT") for s, _ in c.log)    # 원장대로만 — 새로 계획하지 않는다


def test_one_mode_is_required_and_modes_exclude_each_other(monkeypatch, tmp_path):
    for argv in ([], ["--dry-run", "--apply"]):
        monkeypatch.setattr(M, "_connect", lambda: FakeConn())
        monkeypatch.setattr(sys, "argv", ["x", *argv])
        with pytest.raises(SystemExit) as e:
            M.main()
        assert e.value.code == 2


def test_ledger_and_csv_stay_outside_git():
    """eNB 번호는 운영 자산이고 저장소는 공개다 — 출력 경로가 git 밖(.gitignore)이어야 한다."""
    assert M.OUT.parts[-3:] == ("data", "pemdb1", "repairs")
    ig = (Path(__file__).resolve().parents[1] / ".gitignore").read_text(encoding="utf-8")
    assert "data/pemdb1" in ig or "repairs" in ig


def test_tool_does_not_import_the_product_packages_or_contain_secrets():
    src = TOOL.read_text(encoding="utf-8")
    assert not re.search(r"password\s*=\s*[\"'][^\"']+[\"']", src, re.I)     # 비밀은 환경변수로만
    assert "GP_MYSQL_PASSWORD" in src
