"""시나리오 현황 자료의 일본어 번역 (`config/scenario_report_ja.json`).

`scenario_report.json` 은 판정이 끝날 때마다 고치는 한국어 자료라 일본어를 그 안에 넣지 않는다 —
고치는 사람이 두 말을 함께 맞춰야 하고, 한쪽만 고쳐져도 알 길이 없다. 번역은 칸(`latest.summary`
같은 경로)마다 **원문 해시**와 함께 따로 두고 요청 때 겹친다. 원문이 바뀌면 해시가 어긋나므로
옛 번역을 내지 않고 한국어를 그대로 보이며 "낡음"으로 센다 — 다른 판정의 번역이 붙어 나오는
것보다 한국어가 남는 쪽이 낫다.

판정을 갱신한 뒤 — `python -m datagen.scenario_ja todo > t.json`, 번역해서
`python -m datagen.scenario_ja apply t.json`, `python -m datagen.scenario_ja check` 로 0 건을 본다.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Iterator

HERE = Path(__file__).resolve().parents[2]
REPORT = HERE / "config" / "scenario_report.json"
REPORT_JA = HERE / "config" / "scenario_report_ja.json"
HOW = HERE / "config" / "scenario_how.json"
GLOSSARY = HERE / "config" / "scenario_glossary.json"

HANGUL = re.compile("[가-힣]")
#: 번역하지 않는 칸 — `evidence` 는 근거 파일 이름이라 번역할 뜻이 없다. `q_ko`·`a_head_ko` 는 일본어 L3
#: 문답의 **한국어 해석**이다(`l3log` · 2026-11-11) — 원문이 일본어라 일본어 화면은 원문을 보면 된다.
#: 빼지 않으면 해석 칸마다 "번역 없음"으로 세어 `ja_left` 가 0 이 되지 않는다
SKIP_KEYS = frozenset({"evidence", "q_ko", "a_head_ko"})


def src_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]


def _walk(node, path: tuple) -> Iterator[tuple[tuple, str]]:
    if isinstance(node, dict):
        for k, v in node.items():
            if k not in SKIP_KEYS:
                yield from _walk(v, path + (k,))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _walk(v, path + (i,))
    elif isinstance(node, str) and HANGUL.search(node):
        yield path, node


def strings(report: dict) -> Iterator[tuple[str, str, tuple, str]]:
    """번역 대상 — `(묶음 키, 칸 경로, 실제 경로, 원문)`. 묶음 키는 시나리오 번호이거나 `notes` 다."""
    for i, s in enumerate(report.get("scenarios") or []):
        if isinstance(s, dict):
            for path, text in _walk(s, ()):
                yield str(s.get("no")), ".".join(map(str, path)), ("scenarios", i) + path, text
    for path, text in _walk(report.get("notes"), ()):
        yield "notes", ".".join(map(str, path)), ("notes",) + path, text


def _put(doc, path: tuple, value: str) -> None:
    for k in path[:-1]:
        doc = doc[k]
    doc[path[-1]] = value


def merge(report: dict, ja: dict) -> tuple[dict, dict]:
    """일본어를 겹친 사본과 `{"missing": [...], "stale": [...]}`.

    칸에서 찾고, 없으면 같은 원문의 번역을 쓴다 — 목록 앞에 한 줄이 끼면 뒤 칸의 번호가 전부 밀리는데
    그것은 원문이 바뀐 것이 아니다.
    """
    table = ja.get("scenarios") or {}
    by_hash = {e["h"]: e["ja"] for group in table.values() for e in group.values()}
    out = copy.deepcopy(report)
    missing, stale = [], []
    for group, cell, path, text in strings(report):
        h = src_hash(text)
        entry = (table.get(group) or {}).get(cell)
        if entry and entry["h"] == h:
            _put(out, path, entry["ja"])
        elif h in by_hash:
            _put(out, path, by_hash[h])
        else:
            (stale if entry else missing).append(f"{group}:{cell}")
    return out, {"missing": missing, "stale": stale}


def _load(file: Path) -> dict:
    return json.loads(file.read_text(encoding="utf-8"))


def _load_ja() -> dict:
    return _load(REPORT_JA) if REPORT_JA.exists() else {"scenarios": {}}


def todo() -> dict:
    """번역이 없거나 낡은 칸 — `{"<묶음>:<칸>": 원문}`."""
    report, ja = _load(REPORT), _load_ja()
    _, info = merge(report, ja)
    want = set(info["missing"]) | set(info["stale"])
    return {f"{g}:{c}": t for g, c, _, t in strings(report) if f"{g}:{c}" in want}


def apply(done: dict) -> int:
    """번역을 넣는다 — `{"<묶음>:<칸>": 일본어}`. 지금 원문의 해시를 함께 적고, 없어진 칸은 지운다.

    **받지 않은 칸도 같은 원문의 번역이 있으면 지금 칸으로 옮긴다.** `latest` 가 새 회차로 바뀌면 옛
    `latest` 가 `history` 맨 앞으로 들어가 뒤 칸이 전부 밀리는데, 칸만 보고 덮으면 밀린 칸의 번역이
    사라졌다(11-06 16번 갱신 · 세 번에 나눠 맞췄다). 그래서 쓰기 전에 해시로 먼저 모아 둔다.
    """
    report, ja = _load(REPORT), _load_ja()
    table = ja.setdefault("scenarios", {})
    by_hash = {e["h"]: e["ja"] for group in table.values() for e in group.values()}
    live: dict[str, set] = {}
    n = 0
    for group, cell, _, text in strings(report):
        live.setdefault(group, set()).add(cell)
        h = src_hash(text)
        if f"{group}:{cell}" in done:
            table.setdefault(group, {})[cell] = {"h": h, "ja": done[f"{group}:{cell}"]}
            n += 1
        elif h in by_hash:
            table.setdefault(group, {})[cell] = {"h": h, "ja": by_hash[h]}
    for group in list(table):
        for cell in list(table[group]):
            if cell not in live.get(group, ()):
                del table[group][cell]
    REPORT_JA.write_text(json.dumps(ja, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return n


_WHY_JA = (
    ("계단 마지막 구간까지", "階段の最終区間まで"),
    (r"국면 3개\(몰림·분산·분석\)", "3局面(集中・分散・分析)"),
    ("즉시 시작·종료 · 정점 유지", "即時開始・終了・ピーク維持"),
    (r"열화·회복 (\d+)주기\((\d+)분 주기\)", r"劣化・回復 \1周期(\2分周期)"),
    (r"주입 (\d+)분 뒤 탐지 \+ 여유 (\d+)", r"注入 \1分後に検知 + 余裕 \2"),
    (r"늦게 시작하는 계열", "遅れて始まる系列"),
    (r"\((\d+)회부터 반영\)", r"(\1回から反映)"),
    (r"실측 (\d+)회 정답 없음", r"実測 \1回 正解なし"),
    (r"실측 (\d+)회 L2 없음", r"実測 \1回 L2なし"),
    (r"L2까지 (\d+)분", r"L2まで \1分"),
    (r"실측 (\d+)회", r"実測 \1回"),
    (r"정답까지 (\d+)분", r"正解まで \1分"),
    ("상승", "上昇"), ("정점", "ピーク"), ("하강", "下降"),
)


def why_ja(text: str) -> str:
    """`recommended_duration` 의 산정 근거. 숫자가 끼어 있어 고정 사전(`T`)으로는 못 찾는다."""
    for ko, ja in _WHY_JA:
        text = re.sub(ko, ja, text)
    return text


def scan() -> dict:
    """일본어 화면에 나갈 자료에 남은 한글 건수."""
    merged, info = merge(_load(REPORT), _load_ja())
    left = sum(1 for _, _, _, _ in strings(merged))
    how = sum(1 for s in _load(HOW)["scenarios"].values() for _ in _walk(s.get("ja") or {}, ()))
    terms = _load(GLOSSARY)["terms"] if GLOSSARY.exists() else {}
    gloss = sum(1 for t in terms.values() for _ in _walk(t.get("ja") or {}, ()))
    from .scenarios import KIND_SPECS, recommended_duration
    why = sum(1 for k in KIND_SPECS if HANGUL.search(why_ja((recommended_duration(k) or {}).get("why") or "")))
    return {"report": left, "missing": len(info["missing"]), "stale": len(info["stale"]),
            "how": how, "glossary": gloss, "why": why}


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else "check"
    if cmd == "todo":
        print(json.dumps(todo(), ensure_ascii=False, indent=1))
    elif cmd == "apply":
        print(apply(_load(Path(argv[1]))), "칸")
    elif cmd == "check":
        got = scan()
        print(got)
        return 1 if any(got.values()) else 0
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
