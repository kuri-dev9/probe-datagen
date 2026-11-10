"""L3 문답 검사 기록 — 날짜별 JSONL, 7일 보관 (2026-11-09 · 맥스 구현 고정).

`l3log.validate/add/prune/load`, CLI(`datagen l3 add|prune`), gen-ui API(`/api/l3`, `/api/scenario-report` 의 `l3`·`l3_broken`),
화면(이전 회차 표의 L3 칸 · 답 원문 · 이전 검사 · 깨진 줄 경고 · HTML 이스케이프). 임시 디렉터리만 쓴다 — 운영 `data/l3` 는 건드리지 않는다.
"""
from __future__ import annotations

import copy
import json
import re
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

from datagen import l3log, ui

HERE = Path(__file__).resolve().parent.parent
TODAY = date(2026, 11, 9)


def _turn(n=1, kind="first", **kw):
    t = {"n": n, "kind": kind, "route": "rca", "q": f"질문{n}", "answer": f"답 원문 {n} " + "가" * 200,
         "verdict": "ok", "note": "메모"}
    t.update(kw)
    return t


def _rec(**kw):
    r = {"no": 3, "fault": "F-224", "incident_id": "IX1", "event_id": "EVT-1", "at": "2026-11-09T17:25:00",
         "status": "partial", "checks": {"numbers": "ok", "cause": "issue", "refs": "ok", "guide": "ok", "ja": "ok", "filtered": 0},
         "by": "에스터", "turns": [_turn(1), _turn(2, "context", route="general", verdict="issue")]}
    r.update(kw)
    return r


def _report(tmp_path, fault="F-224", no=3):
    p = tmp_path / "scenario_report.json"
    doc = {"scenarios": [{"no": no, "latest": {"fault": fault, "summary": "s"}}, {"no": 4, "latest": {"fault": "F-1"}}]}
    p.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return p


# ── 1. validate ──────────────────────────────────────────────────────────────

def test_a_good_record_passes():
    assert l3log.validate(_rec()) == []


@pytest.mark.parametrize("patch", [
    {"no": 0}, {"no": 100}, {"no": "3"}, {"no": True},
    {"fault": "224"}, {"fault": "F-"}, {"fault": None},
    {"at": "어제"}, {"at": None},
    {"status": "ok"}, {"status": None},
    {"checks": None}, {"checks": {"numbers": "bad"}}, {"checks": {"filtered": -1}}, {"checks": {"filtered": "0"}},
    {"turns": []}, {"turns": None}, {"turns": ["x"]},
    {"incident_id": 3}, {"by": 3},
])
def test_bad_top_level_fields_are_rejected(patch):
    assert l3log.validate(_rec(**patch))


@pytest.mark.parametrize("patch", [
    {"kind": "x"}, {"route": "other"}, {"verdict": "pass"}, {"q": ""}, {"q": "  "}, {"answer": ""}, {"answer": None},
    {"n": "1"}, {"n": True}, {"note": 3}, {"a_head": 3}, {"evidence_ref": ["EV-00"]},
])
def test_bad_turn_fields_are_rejected(patch):
    t = _turn(1)
    t.update(patch)
    assert l3log.validate(_rec(turns=[t]))


def test_null_checks_and_optional_fields_are_allowed():
    r = _rec(checks={"numbers": None, "filtered": None}, incident_id=None, event_id=None, by=None)
    for t in r["turns"]:
        t.pop("note")
    assert l3log.validate(r) == []


# ── 2. add ───────────────────────────────────────────────────────────────────

def test_add_appends_lines_and_never_overwrites(tmp_path):
    rep = _report(tmp_path, fault="F-9")
    root = tmp_path / "l3"
    l3log.add(_rec(), root, rep, TODAY)
    l3log.add(_rec(), root, rep, TODAY)                       # 같은 기록을 또 넣으면 두 줄
    lines = (root / "2026-11-09.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2 and json.loads(lines[0]) == _rec()


def test_invalid_record_writes_nothing(tmp_path):
    root = tmp_path / "l3"
    with pytest.raises(ValueError):
        l3log.add(_rec(status="bad"), root, _report(tmp_path), TODAY)
    assert not root.exists() or not list(root.iterdir())


def test_file_is_named_by_the_record_day(tmp_path):
    root = tmp_path / "l3"
    l3log.add(_rec(at="2026-11-08T23:59:00"), root, _report(tmp_path, fault="F-9"), TODAY)
    assert [p.name for p in root.iterdir()] == ["2026-11-08.jsonl"]


# ── 3. latest.l3 갱신 ────────────────────────────────────────────────────────

def test_summary_updates_only_when_fault_equals_latest_fault(tmp_path):
    rep = _report(tmp_path, fault="F-224")
    got = l3log.add(_rec(), tmp_path / "l3", rep, TODAY)
    doc = json.loads(rep.read_text(encoding="utf-8"))
    l3 = doc["scenarios"][0]["latest"]["l3"]
    assert got["latest_updated"] is True and l3["status"] == "partial" and l3["at"] == "2026-11-09 17:25"
    assert len(l3["turns"]) == 2


def test_older_fault_leaves_latest_l3_alone(tmp_path):
    rep = _report(tmp_path, fault="F-300")
    before = rep.read_text(encoding="utf-8")
    got = l3log.add(_rec(fault="F-224"), tmp_path / "l3", rep, TODAY)
    assert got["latest_updated"] is False and rep.read_text(encoding="utf-8") == before


def test_summary_has_a_head_not_the_full_answer(tmp_path):
    rec = _rec(turns=[_turn(1)])
    s = l3log.summary(rec)
    assert "answer" not in s["turns"][0]
    assert s["turns"][0]["a_head"] == rec["turns"][0]["answer"][:l3log.HEAD_CHARS].strip()
    s2 = l3log.summary(_rec(turns=[_turn(1, a_head="앞부분")]))
    assert s2["turns"][0]["a_head"] == "앞부분"


def test_other_scenarios_and_file_format_are_unchanged(tmp_path):
    rep = _report(tmp_path, fault="F-224")
    before = json.loads(rep.read_text(encoding="utf-8"))
    l3log.add(_rec(), tmp_path / "l3", rep, TODAY)
    raw = rep.read_text(encoding="utf-8")
    after = json.loads(raw)
    assert after["scenarios"][1] == before["scenarios"][1]
    assert raw.endswith("\n") and raw.startswith('{\n  "scenarios"')       # 들여쓰기 2칸 · 끝 줄바꿈 유지
    after["scenarios"][0]["latest"].pop("l3")
    assert after == before


# ── 4. prune ─────────────────────────────────────────────────────────────────

def _touch_days(root, days):
    root.mkdir(parents=True, exist_ok=True)
    for d in days:
        (root / f"{d}.jsonl").write_text("{}\n", encoding="utf-8")


def test_prune_keeps_seven_days_including_today(tmp_path):
    root = tmp_path / "l3"
    days = [(TODAY - timedelta(days=i)).isoformat() for i in range(0, 10)]
    _touch_days(root, days)
    gone = l3log.prune(root, TODAY)
    left = sorted(p.stem for p in root.iterdir())
    assert left == sorted(days[:7])                                   # 오늘 포함 7일
    assert sorted(g[:10] for g in gone) == sorted(days[7:])


def test_prune_does_not_touch_non_date_files(tmp_path):
    root = tmp_path / "l3"
    _touch_days(root, ["2020-01-01"])
    (root / "notes.txt").write_text("x")
    (root / "2020-01-01.jsonl.bak").write_text("x")
    l3log.prune(root, TODAY)
    assert sorted(p.name for p in root.iterdir()) == ["2020-01-01.jsonl.bak", "notes.txt"]


def test_prune_on_missing_dir_is_empty(tmp_path):
    assert l3log.prune(tmp_path / "none", TODAY) == []


def test_add_prunes_old_files_too(tmp_path):
    root = tmp_path / "l3"
    _touch_days(root, ["2026-10-01"])
    got = l3log.add(_rec(), root, _report(tmp_path, fault="F-9"), TODAY)
    assert got["pruned"] == ["2026-10-01.jsonl"]


# ── 5. load ──────────────────────────────────────────────────────────────────

def test_load_is_newest_first_and_filters_by_no(tmp_path):
    root, rep = tmp_path / "l3", _report(tmp_path, fault="F-9")
    l3log.add(_rec(at="2026-11-09T10:00:00"), root, rep, TODAY)
    l3log.add(_rec(at="2026-11-09T12:00:00"), root, rep, TODAY)
    l3log.add(_rec(no=10, fault="F-225", at="2026-11-09T11:00:00"), root, rep, TODAY)
    recs, broken = l3log.load(None, root, TODAY)
    assert [r["at"][11:13] for r in recs] == ["12", "11", "10"] and broken == 0
    assert [r["no"] for r in l3log.load(3, root, TODAY)[0]] == [3, 3]


def test_load_ignores_files_older_than_seven_days_without_deleting(tmp_path):
    root = tmp_path / "l3"
    _touch_days(root, ["2026-10-01"])
    (root / "2026-10-01.jsonl").write_text(json.dumps(_rec(at="2026-10-01T10:00:00")) + "\n", encoding="utf-8")
    assert l3log.load(None, root, TODAY) == ([], 0)
    assert (root / "2026-10-01.jsonl").exists()


def test_load_skips_and_counts_broken_and_invalid_lines(tmp_path):
    root = tmp_path / "l3"
    root.mkdir()
    good = json.dumps(_rec(), ensure_ascii=False)
    bad_fmt = json.dumps(_rec(status="bad"))
    (root / "2026-11-09.jsonl").write_text(f"{good}\n{{깨진 줄\n{bad_fmt}\n\n{good}\n", encoding="utf-8")
    recs, broken = l3log.load(None, root, TODAY)
    assert len(recs) == 2 and broken == 2


def test_load_on_missing_dir(tmp_path):
    assert l3log.load(None, tmp_path / "none", TODAY) == ([], 0)


# ── 6. CLI ───────────────────────────────────────────────────────────────────

def _cli(*args):
    return subprocess.run([sys.executable, "-m", "datagen", "l3", *args], cwd=HERE, capture_output=True, text=True)


@pytest.fixture
def sandbox(monkeypatch, tmp_path):
    """CLI 는 모듈 기본 경로를 쓴다 — 함수 호출로 돌려 `L3_DIR`·`REPORT` 를 임시 위치로 바꾼다."""
    from datagen import cli
    root = tmp_path / "l3"
    rep = _report(tmp_path, fault="F-224")
    real_add = l3log.add
    monkeypatch.setattr(l3log, "add", lambda rec: real_add(rec, root, rep))
    real_prune = l3log.prune
    # `prune` 는 add 도 부른다(인자 2개) — 인자 없는 CLI 호출만 임시 위치로 돌린다
    monkeypatch.setattr(l3log, "prune", lambda r=root, t=None: real_prune(r, t))
    return cli, root, rep


class _A:
    def __init__(self, op, file=None):
        self.op, self.file = op, file


def test_cli_add_accepts_an_object_and_a_list(sandbox, tmp_path, capsys):
    cli, root, _ = sandbox
    one = tmp_path / "one.json"
    one.write_text(json.dumps(_rec(), ensure_ascii=False), encoding="utf-8")
    many = tmp_path / "many.json"
    many.write_text(json.dumps([_rec(), _rec(at="2026-11-09T18:00:00")], ensure_ascii=False), encoding="utf-8")
    assert cli.cmd_l3(_A("add", str(one))) == 0
    assert cli.cmd_l3(_A("add", str(many))) == 0
    assert len((root / "2026-11-09.jsonl").read_text(encoding="utf-8").splitlines()) == 3
    assert "latest.l3 갱신" in capsys.readouterr().out


def test_cli_missing_or_broken_file_gives_a_message_and_rc1(sandbox, tmp_path, capsys):
    cli, _, _ = sandbox
    assert cli.cmd_l3(_A("add", str(tmp_path / "none.json"))) == 1
    bad = tmp_path / "bad.json"
    bad.write_text("{깨짐", encoding="utf-8")
    assert cli.cmd_l3(_A("add", str(bad))) == 1
    out = capsys.readouterr().out
    assert out.count("기록 파일을 못 읽었다") == 2 and "Traceback" not in out


def test_cli_invalid_record_is_rejected_with_rc1(sandbox, tmp_path, capsys):
    cli, root, _ = sandbox
    p = tmp_path / "r.json"
    p.write_text(json.dumps(_rec(status="bad")), encoding="utf-8")
    assert cli.cmd_l3(_A("add", str(p))) == 1
    assert "거부" in capsys.readouterr().out and not root.exists()


def test_cli_add_without_a_file_is_rc2_and_prune_runs(sandbox, capsys):
    cli, root, _ = sandbox
    assert cli.cmd_l3(_A("add")) == 2
    _touch_days(root, ["2020-01-01"])
    assert cli.cmd_l3(_A("prune")) == 0 and "지움 1개" in capsys.readouterr().out


def test_cli_is_wired_in_the_real_entry_point():
    r = _cli("add")
    assert r.returncode == 2 and "l3 add <json 파일>" in r.stdout


# ── 7. API ───────────────────────────────────────────────────────────────────

@pytest.fixture
def api(monkeypatch, tmp_path):
    root, rep = tmp_path / "l3", _report(tmp_path, fault="F-9")
    l3log.add(_rec(at="2026-11-09T10:00:00"), root, rep, TODAY)
    l3log.add(_rec(no=10, fault="F-225", at="2026-11-09T12:00:00"), root, rep, TODAY)
    (root / "2026-11-09.jsonl").write_text((root / "2026-11-09.jsonl").read_text(encoding="utf-8") + "{깨짐\n", encoding="utf-8")
    real = l3log.load
    monkeypatch.setattr(l3log, "load", lambda no=None: real(no, root, TODAY))
    return ui


def test_api_l3_shape_and_filter(api):
    d = api.api_l3()
    assert set(d) == {"ok", "keep_days", "records", "broken"} and d["keep_days"] == 7 and d["ok"] is True
    assert [r["no"] for r in d["records"]] == [10, 3] and d["broken"] == 1
    assert "answer" in d["records"][0]["turns"][0]                       # 답 원문 전체를 내린다
    assert [r["no"] for r in api.api_l3(3)["records"]] == [3]


def test_scenario_report_carries_l3_and_broken(api):
    d = api.api_scenario_report()
    assert [r["no"] for r in d["l3"]] == [10, 3] and d["l3_broken"] == 1


# ── 8. 화면 (실제 페이지 스크립트를 node 로) ─────────────────────────────────

NODE = subprocess.run(["which", "node"], capture_output=True, text=True).stdout.strip()


def _page_js() -> str:
    html = ui._HTML_SCEN
    return max(re.findall(r"<script[^>]*>(.*?)</script>", html, re.S), key=len)


STUB = """
const _el=new Proxy(function(){},{get:(t,k)=>k==='style'||k==='classList'||k==='dataset'?_el:(k===Symbol.toPrimitive?()=>'':_el),apply:()=>_el,set:()=>true});
globalThis.document=new Proxy({},{get:()=>_el});globalThis.window=globalThis;
globalThis.localStorage={getItem:()=>null,setItem(){}};globalThis.location={search:'',hash:'',pathname:'/scenarios'};
globalThis.fetch=()=>new Promise(()=>{});globalThis.setInterval=()=>0;globalThis.addEventListener=()=>{};
globalThis.MutationObserver=class{observe(){}};
"""


def _node(test: str) -> str:
    if not NODE:
        pytest.skip("node 가 없다")
    src = STUB + _page_js() + "\n" + test
    r = subprocess.run([NODE, "-e", src], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr[-800:]
    return r.stdout


def test_ui_hidden_when_no_l3_records_and_dash_in_history_cell():
    out = _node("""
L3LOG=[];L3BROKEN=0;
const s={no:3,latest:{fault:'F-2',l3:null}};
console.log('LOG['+String(l3Log(s)).replace(/<[^>]+>/g,'')+']');
console.log('CELL['+l3Badge(l3OfFault(s,'F-1'))+']');
""")
    assert "LOG[]" in out and "CELL[–]" in out


def test_ui_top_record_is_raw_and_the_rest_fold_as_previous():
    out = _node("""
const mk=(at,f,st)=>({no:3,fault:f,at,status:st,turns:[{n:1,kind:'first',route:'rca',q:'q',answer:'원문',verdict:'ok',note:''},
  {n:2,kind:'data',route:'general',q:'q2',answer:'원문2',verdict:'issue',note:''}]});
L3LOG=[mk('2026-11-09T17:50:00','F-224','partial'),mk('2026-11-09T17:25:00','F-224','partial'),mk('2026-11-08T10:00:00','F-200','pass')];
L3BROKEN=0;
const s={no:3,latest:{fault:'F-224',l3:{at:'2026-11-09 17:50',status:'partial',checks:{},turns:[]}}};
const h=String(l3Log(s));
console.log('RAW',h.includes('답 원문 보기'),'OLDER',h.includes('이전 검사 2건'));
console.log('CELL['+l3Badge(l3OfFault(s,'F-224')).replace(/<[^>]+>/g,'')+']');
console.log('CELLF200['+l3Badge(l3OfFault(s,'F-200')).replace(/<[^>]+>/g,'')+']');
""")
    assert "RAW true OLDER true" in out
    assert "CELL[△ 1/2]" in out and "CELLF200[✓ 1/2]" in out


def test_ui_warns_about_broken_lines_and_escapes_answers():
    out = _node("""
L3LOG=[{no:3,fault:'F-224',at:'2026-11-09T17:50:00',status:'partial',turns:[{n:1,kind:'first',route:'rca',q:'q',
  answer:'<img src=x onerror=alert(1)>',verdict:'ok',note:'<b>n</b>'}]}];
L3BROKEN=2;
const s={no:3,latest:{fault:'F-224',l3:{at:'2026-11-09 17:50',status:'partial',checks:{},turns:[]}}};
const h=String(l3Log(s));
console.log('WARN',h.includes('읽지 못한 기록 2줄'),'RAW_TAG',h.includes('<img'),'ESC',h.includes('&lt;img'));
""")
    assert "WARN true RAW_TAG false ESC true" in out


def test_gitignore_keeps_the_answers_out_of_git():
    txt = (HERE / ".gitignore").read_text(encoding="utf-8")
    assert "data/l3/" in txt
