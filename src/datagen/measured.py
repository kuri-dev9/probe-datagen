"""권장 지속시간의 실측 기록 — `config/measured_durations.json` (2026-11-05 사용자 결정).

모양으로 계산한 권장값(`scenarios.recommended_duration`)은 탐지가 몇 분을 요구하는지 모른다.
주입마다 탐지 구간을 재서 시나리오(kind)별로 쌓고, 그 값을 권장으로 쓴다.

**값은 사람이 넣는다**(`python -m datagen measure`). 여기서 DB 를 읽어 재지 않는다 — 정답 여부는
카드를 본 사람이 판정한다. 파일이 바뀌면 수정 시각을 보고 다시 읽으므로 gen-ui 를 재시작하지 않는다.

시각은 모두 분이고 재지 못한 값은 `null` 이다(불변규칙 17 의 결).
"""

from __future__ import annotations

import json
import math
import os
from datetime import datetime
from pathlib import Path

PATH = Path(__file__).resolve().parents[2] / "config" / "measured_durations.json"

#: 정답만 권장에 쓴다. 오답·미탐은 기록으로 남기되 "그만큼 넣으면 된다"는 근거가 아니다
RESULTS = ("correct", "wrong", "missed")

_cache: dict = {"mtime": None, "data": {}}


def load(path: Path | None = None) -> dict[str, list[dict]]:
    """kind → 기록 목록. 파일이 없거나 깨졌으면 비어 있다 — 권장이 모양 기준으로 돌아갈 뿐이다."""
    p = path or PATH
    try:
        mtime = p.stat().st_mtime_ns
    except OSError:
        return {}
    if _cache["mtime"] == (p, mtime):
        return _cache["data"]
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"  ! 실측 기록을 못 읽었다 — 모양 기준으로 돌아간다: {p.name} · {e}", flush=True)
        data = {}
    data = _well_formed(data, p.name)
    _cache.update(mtime=(p, mtime), data=data)
    return data


def _well_formed(data, name: str) -> dict[str, list[dict]]:
    """모양이 틀린 부분을 없는 것으로 친다 — 손으로 고친 파일 하나로 주입 화면이 막히면 안 된다."""
    if not isinstance(data, dict):
        print(f"  ! 실측 기록이 객체가 아니다 — 모양 기준으로 돌아간다: {name}", flush=True)
        return {}
    good = {k: v for k, v in data.items()
            if isinstance(v, list) and all(isinstance(r, dict) for r in v)}
    if len(good) < len(data):
        print(f"  ! 실측 기록의 모양이 틀려 뺀 종류: {sorted(set(data) - set(good))}", flush=True)
    return good


def _minutes(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def summary(kind: str, path: Path | None = None) -> dict:
    """kind 의 실측 요약. `to_correct`·`settle` 은 정답 기록의 가장 긴 값이고 없으면 None 이다."""
    runs = load(path).get(kind) or []
    ok = [r for r in runs if r.get("result") == "correct" and _minutes(r.get("to_correct_min"))]
    ends = [r["end_to_final_min"] for r in ok if _minutes(r.get("end_to_final_min"))]
    return {"runs": len(runs), "n": len(ok),
            "to_correct": math.ceil(max(r["to_correct_min"] for r in ok)) if ok else None,
            "settle": math.ceil(max(ends)) if ends else None}


def add(kind: str, record: dict, path: Path | None = None) -> None:
    """기록 한 건을 넣는다. 같은 장애는 덮어쓴다 — 고쳐 넣어도 횟수가 늘지 않는다."""
    p = path or PATH
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        data = {}
    rows = [r for r in data.get(kind, []) if r.get("fault") != record["fault"]]
    data[kind] = rows + [{**record, "recorded_at": datetime.now().isoformat(timespec="seconds")}]
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                   encoding="utf-8")
    os.replace(tmp, p)          # 읽는 쪽이 반쯤 쓴 파일을 보지 않게
