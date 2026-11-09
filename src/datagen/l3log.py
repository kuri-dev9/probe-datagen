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
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parents[2]
L3_DIR = HERE / "data" / "l3"
REPORT = HERE / "config" / "scenario_report.json"
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
        for k in ("a_head", "note", "evidence_ref"):
            if t.get(k) is not None and not isinstance(t[k], str):
                err.append(f"{w}.{k} 는 문자열이거나 null 이다")
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
                   "verdict": t["verdict"], **({"note": t["note"]} if t.get("note") else {})}
                  for t in rec["turns"]],
    }


def add(rec: dict, root: Path = L3_DIR, report: Path = REPORT,
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
    updated = False
    if report.exists():
        raw = report.read_text(encoding="utf-8")
        doc = json.loads(raw)
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
    return {"file": str(path), "pruned": pruned, "latest_updated": updated}


def load(no: int | None = None, root: Path = L3_DIR, today: date | None = None,
         keep: int = KEEP_DAYS) -> tuple[list[dict], int]:
    """최근 ``keep`` 일 기록을 최신순으로 — ``(기록, 깨진 줄 수)``. 깨진 줄은 건너뛰고 센다
    (조용히 빼면 "기록 없음"으로 읽힌다). 지우지는 않는다 — 읽는 쪽(gen-ui)은 `ro` 다."""
    if not root.exists():
        return [], 0
    cut = (today or date.today()) - timedelta(days=keep - 1)
    out, broken = [], 0
    for p in sorted(root.iterdir()):
        m = _FILE.match(p.name)
        if not m or date.fromisoformat(m.group(1)) < cut:
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                broken += 1
                continue
            if validate(rec):
                broken += 1
                continue
            if no is None or rec["no"] == no:
                out.append(rec)
    out.sort(key=lambda r: str(r["at"]), reverse=True)
    return out, broken
