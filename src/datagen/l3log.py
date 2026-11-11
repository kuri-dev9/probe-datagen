"""L3 문답 검사 기록 — 날짜별 JSONL, 7일 보관 (2026-11-09 사용자 결정).

시나리오 현황(`config/scenario_report.json`)의 `latest.l3` 는 **최신 회차 하나의 요약**이라 다시
검사하면 덮이고, 회차가 `history` 로 내려가면 사라진다. 답 원문은 테스터의 `/tmp` 에만 있었다.
여기는 검사 한 건을 **한 줄로 이어 붙이기만** 한다 — 덮어쓰지 않으므로 회차별로 L3 가 나아지는지
나빠지는지 남는다.

* 위치 `data/l3/YYYY-MM-DD.jsonl` — 원시 Parquet 와 같은 날짜별 파일이다. 7일이 지난 파일은 통째로
  지운다(`prune`). `data/` 는 git 밖이다 — 오래 남길 요약은 `scenario_report.json`·`runs.md` 가 맡는다.
* SQLite·Parquet 가 아닌 이유 — 하루 수십 건이라 열 압축 이득이 없고, 답 원문은 사람이 바로 열어
  보는 쪽이 낫다. 이어 붙이기만 하므로 파일이 깨질 일이 적다(깨진 줄은 읽을 때 건너뛰고 센다).
* 쓰는 쪽은 호스트의 `python -m datagen l3 add` 하나다. gen-ui 는 읽기만 한다(컨테이너 `ro` 마운트).
* `fault` 가 그 시나리오의 `latest.fault` 와 같으면 `latest.l3` 요약도 함께 갱신한다 — 테스터가 두
  군데에 따로 쓰면 어긋난다.
* **한국어 해석**(2026-11-11 사용자 "한글로 안 해도 되니 해석이라도") — turn 의 선택 칸 `q_ko`·`answer_ko`.
  질의는 일본어(PoC)라 원문은 그대로 두고 해석을 곁에 단다. 사후 보충(`annotate`)은 원문 줄을 고쳐 쓰지
  않고 **해석만 담은 줄을 이어 붙인다**(`{"annotate": {...}, "turns": [{n, q_ko, answer_ko}]}`) — 읽을 때
  (`load`) 원래 기록에 합친다. 이어 붙이기 원칙이 그대로다. 해석은 기록하는 사람이 쓴다(모델을 부르지 않는다)
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parents[2]
L3_DIR = HERE / "data" / "l3"
REPORT = HERE / "config" / "scenario_report.json"
#: 생성량(%)별 현황 파일 — 기록의 `volume` 이 고른다(2026-11-11). 없거나 15 면 `REPORT`. gen-ui
#: `scenarios_api.SCENARIO_VOLUMES` 와 같은 파일이다
REPORTS = {15: REPORT, 50: HERE / "config" / "scenario_report_v50.json"}
#: 보관 일수 — 오늘 포함 7일치 파일을 둔다(사용자 11-09)
KEEP_DAYS = 7
#: `latest.l3` 요약에 남기는 답 앞부분 길이 — 화면 표가 이 길이로 자른다
HEAD_CHARS = 120

STATUS = ("pass", "partial", "fail")
KINDS = ("first", "context", "data", "offtopic")
ROUTES = ("rca", "general")
VERDICTS = ("ok", "issue")
CHECKS_OK = ("ok", "issue")
CHECK_KEYS = ("numbers", "cause", "refs", "guide", "ja")
#: turn 의 한국어 해석 칸 — 선택이다(없어도 통과)
KO_KEYS = ("q_ko", "answer_ko")
_FILE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.jsonl$")
_FAULT = re.compile(r"^F-\d+$")


def validate(rec: dict) -> list[str]:
    """형식 오류 목록 — 비면 통과. 모르는 값은 "미확인"으로 남기지 않고 거부한다(쓰는 쪽이 고친다)."""
    err: list[str] = []
    if not isinstance(rec, dict):
        return ["기록이 객체가 아니다"]
    no = rec.get("no")
    if not isinstance(no, int) or isinstance(no, bool) or not 1 <= no <= 99:
        err.append(f"no 가 시나리오 번호가 아니다: {no!r}")
    if not isinstance(rec.get("fault"), str) or not _FAULT.match(rec["fault"]):
        err.append(f"fault 는 `F-123` 꼴이어야 한다: {rec.get('fault')!r}")
    try:
        datetime.fromisoformat(str(rec.get("at")))
    except ValueError:
        err.append(f"at 이 시각이 아니다: {rec.get('at')!r}")
    if rec.get("status") not in STATUS:
        err.append(f"status 는 {STATUS} 중 하나: {rec.get('status')!r}")
    vol = rec.get("volume")
    if vol is not None and (isinstance(vol, bool) or vol not in REPORTS):
        err.append(f"volume 은 {sorted(REPORTS)} 중 하나이거나 없다: {vol!r}")
    for k in ("incident_id", "event_id", "by"):
        if rec.get(k) is not None and not isinstance(rec[k], str):
            err.append(f"{k} 는 문자열이거나 null 이다")
    c = rec.get("checks")
    if not isinstance(c, dict):
        err.append("checks 가 없다")
    else:
        for k in CHECK_KEYS:
            if c.get(k) is not None and c[k] not in CHECKS_OK:
                err.append(f"checks.{k} 는 ok/issue/null: {c[k]!r}")
        f = c.get("filtered")
        if f is not None and (not isinstance(f, int) or isinstance(f, bool) or f < 0):
            err.append(f"checks.filtered 는 0 이상 정수이거나 null: {f!r}")
    turns = rec.get("turns")
    if not isinstance(turns, list) or not turns:
        err.append("turns 가 비었다 — 검사는 질의 1회 이상이다")
        return err
    for i, t in enumerate(turns):
        w = f"turns[{i}]"
        if not isinstance(t, dict):
            err.append(f"{w} 가 객체가 아니다")
            continue
        if not isinstance(t.get("n"), int) or isinstance(t.get("n"), bool):
            err.append(f"{w}.n 이 정수가 아니다")
        if t.get("kind") not in KINDS:
            err.append(f"{w}.kind 는 {KINDS} 중 하나: {t.get('kind')!r}")
        if t.get("route") not in ROUTES:
            err.append(f"{w}.route 는 {ROUTES} 중 하나: {t.get('route')!r}")
        for k in ("q", "answer"):
            if not isinstance(t.get(k), str) or not t[k].strip():
                err.append(f"{w}.{k} 가 비었다")
        if t.get("verdict") not in VERDICTS:
            err.append(f"{w}.verdict 는 ok/issue: {t.get('verdict')!r}")
        for k in ("a_head", "note", "evidence_ref", *KO_KEYS):
            if t.get(k) is not None and not isinstance(t[k], str):
                err.append(f"{w}.{k} 는 문자열이거나 null 이다")
    return err


def validate_note(rec: dict) -> list[str]:
    """해석 보충 줄의 형식 오류 — `{"annotate": {"fault", "at"?}, "at", "turns": [{n, q_ko?, answer_ko?}]}`.
    `annotate.at` 은 보충할 원래 기록의 `at` 이다. 없으면 그 회차의 가장 늦은 기록에 붙인다."""
    err: list[str] = []
    a = rec.get("annotate") if isinstance(rec, dict) else None
    if not isinstance(a, dict):
        return ["annotate 가 없다"]
    if not isinstance(a.get("fault"), str) or not _FAULT.match(a["fault"]):
        err.append(f"annotate.fault 는 `F-123` 꼴이어야 한다: {a.get('fault')!r}")
    vol = rec.get("volume")
    if vol is not None and (isinstance(vol, bool) or vol not in REPORTS):
        err.append(f"volume 은 {sorted(REPORTS)} 중 하나이거나 없다: {vol!r}")
    for w, v in (("annotate.at", a.get("at")), ("at", rec.get("at"))):
        if v is None and w == "annotate.at":
            continue
        try:
            datetime.fromisoformat(str(v))
        except ValueError:
            err.append(f"{w} 이 시각이 아니다: {v!r}")
    turns = rec.get("turns")
    if not isinstance(turns, list) or not turns:
        return err + ["turns 가 비었다"]
    for i, t in enumerate(turns):
        w = f"turns[{i}]"
        if not isinstance(t, dict) or not isinstance(t.get("n"), int) or isinstance(t.get("n"), bool):
            err.append(f"{w}.n 이 정수가 아니다")
            continue
        if not any(isinstance(t.get(k), str) and t[k].strip() for k in KO_KEYS):
            err.append(f"{w} 에 해석(q_ko·answer_ko)이 없다")
        for k in KO_KEYS:
            if t.get(k) is not None and not isinstance(t[k], str):
                err.append(f"{w}.{k} 는 문자열이거나 null 이다")
        extra = set(t) - {"n", *KO_KEYS}
        if extra:
            # 원문·판정은 보충으로 바꾸지 않는다 — 고칠 것이 있으면 새 검사 기록이다
            err.append(f"{w} 에 해석 밖의 칸이 있다: {sorted(extra)}")
    return err


def _day_of(rec: dict) -> str:
    return datetime.fromisoformat(str(rec["at"])).date().isoformat()


def prune(root: Path = L3_DIR, today: date | None = None, keep: int = KEEP_DAYS) -> list[str]:
    """보관 기간이 지난 날짜 파일을 지운다 — 지운 파일 이름 목록. 이름이 날짜가 아닌 파일은 건드리지 않는다."""
    if not root.exists():
        return []
    cut = (today or date.today()) - timedelta(days=keep - 1)
    gone = []
    for p in sorted(root.iterdir()):
        m = _FILE.match(p.name)
        if m and date.fromisoformat(m.group(1)) < cut:
            p.unlink()
            gone.append(p.name)
    return gone


def summary(rec: dict) -> dict:
    """`latest.l3` 요약 — 화면 표가 쓰는 칸만. 답 원문은 싣지 않는다(앞부분만)."""
    return {
        "status": rec["status"], "at": str(rec["at"])[:16].replace("T", " "),
        "checks": dict(rec.get("checks") or {}),
        "turns": [{"n": t["n"], "kind": t["kind"], "q": t["q"],
                   "a_head": (t.get("a_head") or t["answer"][:HEAD_CHARS]).strip(),
                   "verdict": t["verdict"], **({"note": t["note"]} if t.get("note") else {}),
                   # 한국어 해석 — 있을 때만. 답 해석은 원문 앞부분과 같은 길이로 자른다
                   **({"q_ko": t["q_ko"]} if t.get("q_ko") else {}),
                   **({"a_head_ko": t["answer_ko"][:HEAD_CHARS].strip()} if t.get("answer_ko") else {})}
                  for t in rec["turns"]],
    }


def report_of(rec: dict, report: Path | None = None) -> Path:
    """그 기록이 갱신할 현황 파일 — 명시한 ``report`` 가 이기고, 아니면 `volume`(없으면 15%)."""
    return report if report is not None else REPORTS.get(rec.get("volume") or 15, REPORT)


def add(rec: dict, root: Path = L3_DIR, report: Path | None = None,
        today: date | None = None) -> dict:
    """기록 한 건을 그날 파일에 이어 붙이고, 오래된 파일을 지우고, 최신 회차면 `latest.l3` 를 갱신한다.

    반환 ``{"file", "pruned", "latest_updated"}``. 형식이 틀리면 `ValueError` — 아무것도 쓰지 않는다.
    """
    err = validate(rec)
    if err:
        raise ValueError("; ".join(err))
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{_day_of(rec)}.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n")
    pruned = prune(root, today)
    target = report_of(rec, report)
    updated = _update_latest(rec, target)
    return {"file": str(path), "pruned": pruned, "latest_updated": updated, "report": str(target)}


def _update_latest(rec: dict, report: Path) -> bool:
    """최신 회차면 `latest.l3` 를 그 기록의 요약으로 — 갱신했으면 참."""
    if not report.exists():
        return False
    raw = report.read_text(encoding="utf-8")
    doc = json.loads(raw)
    updated = False
    for s in doc.get("scenarios") or ():
        if isinstance(s, dict) and s.get("no") == rec["no"]:
            lat = s.get("latest")
            if isinstance(lat, dict) and lat.get("fault") == rec["fault"]:
                lat["l3"] = summary(rec)
                updated = True
            break
    if updated:
        report.write_text(json.dumps(doc, ensure_ascii=False, indent=2)
                          + ("\n" if raw.endswith("\n") else ""), encoding="utf-8")
    return updated


def _merge_notes(recs: list[dict], notes: list[dict]) -> int:
    """해석 보충 줄을 원래 기록에 합친다(늦게 쓴 보충이 이긴다) — 붙일 기록을 못 찾은 보충 수를 돌려준다.
    원문 칸은 건드리지 않고 `q_ko`·`answer_ko` 만 채운다."""
    lost = 0
    for nt in sorted(notes, key=lambda r: str(r["at"])):
        a = nt["annotate"]
        cands = [r for r in recs if r["fault"] == a["fault"]
                 and (a.get("at") is None or str(r["at"]) == str(a["at"]))]
        if not cands:
            lost += 1
            continue
        target = max(cands, key=lambda r: str(r["at"]))
        by_n = {t.get("n"): t for t in target["turns"]}
        for t in nt["turns"]:
            if t["n"] in by_n:
                by_n[t["n"]].update({k: t[k] for k in KO_KEYS if isinstance(t.get(k), str) and t[k].strip()})
    return lost


def _read_day(p: Path) -> tuple[list[dict], list[dict], int]:
    """날짜 파일 하나 → (검사 기록, 해석 보충, 깨진 줄 수)."""
    recs, notes, broken = [], [], 0
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            broken += 1
            continue
        if isinstance(rec, dict) and "annotate" in rec:
            if validate_note(rec):
                broken += 1
            else:
                notes.append(rec)
            continue
        if validate(rec):
            broken += 1
            continue
        recs.append(rec)
    return recs, notes, broken


def annotate(note: dict, root: Path = L3_DIR, report: Path | None = None,
             today: date | None = None) -> dict:
    """이미 있는 검사 기록에 한국어 해석을 단다 — **해석 줄을 이어 붙인다**(원문 줄을 고치지 않는다).

    ``note`` 는 `{"annotate": {"fault", "at"?}, "turns": [{n, q_ko?, answer_ko?}]}` 이다(`at` 이 없으면 지금
    시각을 넣는다). 붙일 기록은 보관 중인 파일에서 찾고, 그 기록과 **같은 날짜 파일**에 쓴다 — 보관 기간이
    함께 끝난다. 못 찾거나 그 기록에 없는 turn 번호면 `ValueError` — 아무것도 쓰지 않는다.
    현황 파일은 보충의 `volume` → 원래 기록의 `volume` → 15% 순으로 고른다(둘이 다르면 거부).
    반환 ``{"file", "latest_updated", "report"}``.
    """
    note = {**note, "at": note.get("at") or datetime.now().isoformat(timespec="seconds")}
    err = validate_note(note)
    if err:
        raise ValueError("; ".join(err))
    a = note["annotate"]
    found: tuple[Path, dict] | None = None
    if root.exists():
        for p in sorted(root.iterdir()):
            if not _FILE.match(p.name):
                continue
            recs, notes, _b = _read_day(p)
            for r in recs:
                if r["fault"] == a["fault"] and (a.get("at") is None or str(r["at"]) == str(a["at"])):
                    if found is None or str(r["at"]) >= str(found[1]["at"]):
                        found = (p, r)
    if found is None:
        raise ValueError(f"붙일 기록이 없다 — {a['fault']}" + (f" · at {a['at']}" if a.get("at") else ""))
    path, rec = found
    if note.get("volume") is not None and rec.get("volume") is not None and note["volume"] != rec["volume"]:
        raise ValueError(f"{a['fault']} 기록은 {rec['volume']}% 인데 보충은 {note['volume']}% 다")
    have = {t["n"] for t in rec["turns"]}
    miss = sorted({t["n"] for t in note["turns"]} - have)
    if miss:
        raise ValueError(f"{a['fault']} 기록에 없는 turn 번호: {miss}")
    # 보충이 정확히 이 기록을 가리키게 `at` 을 고정해 적는다 — 같은 회차를 다시 검사해도 섞이지 않는다
    line = {**note, "annotate": {"fault": a["fault"], "at": str(rec["at"])}}
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(line, ensure_ascii=False, separators=(",", ":")) + "\n")
    recs, notes, _b = _read_day(path)
    _merge_notes(recs, notes)
    merged = next(r for r in recs if r["fault"] == a["fault"] and str(r["at"]) == str(rec["at"]))
    target = report_of({"volume": note.get("volume") or rec.get("volume")}, report)
    return {"file": str(path), "latest_updated": _update_latest(merged, target), "report": str(target)}


def load(no: int | None = None, root: Path = L3_DIR, today: date | None = None,
         keep: int = KEEP_DAYS) -> tuple[list[dict], int]:
    """최근 ``keep`` 일 기록을 최신순으로 — ``(기록, 깨진 줄 수)``. 깨진 줄은 건너뛰고 센다
    (조용히 빼면 "기록 없음"으로 읽힌다). 지우지는 않는다 — 읽는 쪽(gen-ui)은 `ro` 다."""
    if not root.exists():
        return [], 0
    cut = (today or date.today()) - timedelta(days=keep - 1)
    recs, notes, broken = [], [], 0
    for p in sorted(root.iterdir()):
        m = _FILE.match(p.name)
        if not m or date.fromisoformat(m.group(1)) < cut:
            continue
        r, n, b = _read_day(p)
        recs += r
        notes += n
        broken += b
    # 해석 보충 줄을 원래 기록에 합친다 — 붙일 곳을 못 찾은 보충도 깨진 줄로 센다(조용히 빼지 않는다)
    broken += _merge_notes(recs, notes)
    out = [r for r in recs if no is None or r["no"] == no]
    out.sort(key=lambda r: str(r["at"]), reverse=True)
    return out, broken
