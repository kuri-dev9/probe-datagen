"""장애 주입 — 바로 · 예약 · 랜덤 N개 순차 예약 (2026-11-12).

gen-ui(:8009)의 주입 API 를 부른다. **예약은 gen 이 한다** — `start_ts` 가 미래면 그 장애는
`pending` 으로 남고 gen 이 그 시각에 `active` 로 올린다(`control` 의 상태 전이). 그래서 이
스크립트는 상주하지 않는다. 한 번 돌면 예약을 넣고 끝난다.

    python3 tools/inject.py list                              # 시나리오 · 권장 시간 · 권장 시간대
    python3 tools/inject.py queue                             # 대기·진행 중 장애
    python3 tools/inject.py now 14                            # 14번 바로
    python3 tools/inject.py at 14 "2026-11-12 18:30"          # 14번 예약 (HH:MM 만 주면 오늘·지났으면 내일)
    python3 tools/inject.py random 5                          # 랜덤 5개 — 큐 끝부터 이어서
    python3 tools/inject.py random 5 --start 19:00 --gap 20 --only 1-16,21-25
    python3 tools/inject.py ... --dry-run                     # 계획만 찍는다

**한 번에 하나다.** 창고·gen 이 하나라 장애가 겹치면 에러 없이 값이 섞인다. 그래서 모든
주입은 **이미 걸린 대기·진행 장애가 끝난 뒤**로 줄을 선다. 간격은

    다음 시작 = max(앞 장애 끝 + settle + gap, 앞 장애 tail_until)

이다. `settle` 은 시나리오의 확정 대기(분석이 끝나는 데 드는 분 · `/api/scenarios` 의
`dur.settle`)이고, `gap` 은 쿨다운(기본 15분 — 앞 장애가 다음 기준선을 오염시키지 않게).
`tail_until` 은 앞 장애의 평시 꼬리가 걷히는 시각이다(gen 이 아는 경우만).
`now` 도 큐가 비어 있을 때만 "바로"다. 비어 있지 않으면 거절한다 — `--queue` 를 주면 줄을 선다.

**지속은 권장 시간이 기본이다**(`dur.recommended` — 탐지 최소 + 몇 분). `--dur` 로 바꾼다.
권장보다 짧으면 탐지되지 않을 수 있다고 알린다(막지는 않는다).

**랜덤에서 빼는 것:**
- `same_as` 가 있는 번호(18·19·20·26) — 원 번호와 같은 데이터다.
- 17번 — 판정 재료가 없다(NO_EVIDENCE).
- `--hours` 를 켜면(기본) 그 시각이 권장 시간대(`dur.hours`) 밖인 번호. 새벽에는 판정 표본이
  모자라 미탐이 났다(11-12 23·24번).

지정 주입(`now`·`at`)은 위를 막지 않고 경고만 한다 — 사람이 고른 것이다.

**쓰지 않는 것:** 취소(`/cancel`)·삭제. 잘못 넣었으면 gen-ui 화면에서 사람이 취소한다.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta

URL = os.environ.get("GP_GENUI_URL", "http://localhost:8009").rstrip("/")
#: 랜덤 기본 제외 — 판정 재료가 없다(NO_EVIDENCE · scenario-card 스킬 "제외")
NO_EVIDENCE = {17}
DEFAULT_GAP = 15


def _call(path: str, body: dict | None = None):
    req = urllib.request.Request(
        URL + path, data=None if body is None else json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="GET" if body is None else "POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        sys.exit(f"gen-ui 가 거절했다 ({e.code}): {e.read().decode(errors='replace')}")
    except (urllib.error.URLError, OSError) as e:
        sys.exit(f"gen-ui({URL})에 닿지 않는다: {e} — ./gp.sh status 로 gen-ui 가 떠 있는지 본다")


def _ts(v) -> datetime | None:
    try:
        return datetime.fromisoformat(str(v)[:19]) if v else None
    except ValueError:
        return None


def scenarios() -> dict[int, dict]:
    return {int(s["no"]): s for s in _call("/api/scenarios")}


def queue() -> list[dict]:
    """대기·진행 중 장애 — 최근 목록에서 고른다."""
    return [f for f in _call("/api/faults?limit=50") if f.get("status") in ("pending", "active")]


def _free_from(q: list[dict], sc: dict[int, dict], gap: int) -> datetime:
    """큐가 다 끝나고 다음 장애를 넣어도 되는 첫 시각."""
    t = datetime.now()
    for f in q:
        s = sc.get(int(f.get("scenario_no") or 0)) or {}
        end = _ts(f["start_ts"]) + timedelta(minutes=int(f["duration_min"] or 0))
        t = max(t, end + timedelta(minutes=_settle(s) + gap), _ts(f.get("tail_until")) or t)
    return t


def _settle(s: dict) -> int:
    return int((s.get("dur") or {}).get("settle") or 0)


def _dur(s: dict, override: int | None) -> int:
    return int(override or (s.get("dur") or {}).get("recommended") or 30)


def in_hours(s: dict, t: datetime) -> bool:
    """권장 시간대 안인가. `from`>`to` 는 자정을 넘는 구간(예: 5→3 = 05시~다음날 03시)."""
    h = (s.get("dur") or {}).get("hours") or {}
    a, b = h.get("from"), h.get("to")
    if h.get("all") or a is None or b is None:
        return True
    return a <= t.hour < b if a < b else (t.hour >= a or t.hour < b)


def _hours_text(s: dict) -> str:
    h = (s.get("dur") or {}).get("hours") or {}
    if h.get("all") or h.get("from") is None:
        return "언제나"
    return f"{h['from']:02d}~{h['to']:02d}시"


def _parse_at(v: str) -> datetime:
    v = v.strip()
    try:
        if len(v) <= 5:                                   # HH:MM — 오늘, 지났으면 내일
            hh, mm = map(int, v.split(":"))
            t = datetime.now().replace(hour=hh, minute=mm, second=0, microsecond=0)
            return t if t > datetime.now() else t + timedelta(days=1)
        return datetime.fromisoformat(v)
    except ValueError:
        sys.exit(f"시각 형식: 'HH:MM' 또는 'YYYY-MM-DD HH:MM' — 받은 값 {v!r}")


def _parse_only(v: str | None, sc: dict[int, dict]) -> set[int]:
    if not v:
        return set(sc)
    out: set[int] = set()
    for part in v.split(","):
        a, _, b = part.strip().partition("-")
        out |= set(range(int(a), int(b or a) + 1))
    return out & set(sc)


def _post(no: int, s: dict, start: datetime | None, dur: int, note: str, dry: bool) -> str:
    body = {"kind": s["kind"], "duration_min": dur, "intensity": 1.0, "scenario_no": no,
            "note": f"#{no} {note}"}
    if start is not None:
        body["start_ts"] = start.isoformat(timespec="seconds")
    if dry:
        return "(dry-run)"
    return f"F-{_call('/api/faults', body)['fault_id']}"


def _line(fid: str, no: int, s: dict, start: datetime, dur: int, warn: list[str]) -> str:
    end = start + timedelta(minutes=dur)
    w = f"  ⚠ {' · '.join(warn)}" if warn else ""
    return (f"{fid:>10}  #{no:<2} {s['kind']:<18} {start:%m-%d %H:%M} → {end:%H:%M} "
            f"({dur}분)  {s['name']}{w}")


def _warn(no: int, s: dict, start: datetime, dur: int) -> list[str]:
    out = []
    if s.get("same_as"):
        out.append(f"{s['same_as']}번과 같은 데이터")
    if no in NO_EVIDENCE:
        out.append("판정 재료 없음(NO_EVIDENCE)")
    if not in_hours(s, start):
        out.append(f"권장 시간대 밖({_hours_text(s)}) — 표본이 모자라 미탐일 수 있다")
    rec = int((s.get("dur") or {}).get("recommended") or 0)
    if rec and dur < rec:
        out.append(f"권장 {rec}분보다 짧다 — 탐지되지 않을 수 있다")
    return out


def cmd_list(a, sc):
    print(f"{'no':>3}  {'kind':<18} {'권장':>4} {'확정':>4}  {'시간대':<9} 이름")
    for no, s in sorted(sc.items()):
        tag = (f"  (={s['same_as']}번 데이터)" if s.get("same_as") else
               "  (랜덤 제외 · NO_EVIDENCE)" if no in NO_EVIDENCE else "")
        print(f"{no:>3}  {s['kind']:<18} {_dur(s, None):>3}분 {_settle(s):>3}분  "
              f"{_hours_text(s):<9} {s['name']}{tag}")


def cmd_queue(a, sc):
    q = queue()
    if not q:
        print("대기·진행 중 장애 없음")
    for f in q:
        print(f"F-{f['fault_id']}  {f['status']:<8} #{f.get('scenario_no') or '-'} {f['kind']:<18} "
              f"{f['start_ts']} ({f['duration_min']}분)  {f.get('note') or ''}")
    print(f"다음 장애를 넣을 수 있는 시각: {_free_from(q, sc, a.gap):%m-%d %H:%M}")


def cmd_one(a, sc, at: datetime | None):
    if a.no not in sc:
        sys.exit(f"{a.no}번 시나리오가 없다 — list 로 본다")
    s, q = sc[a.no], queue()
    free = _free_from(q, sc, a.gap)
    if at is None:                                        # now
        if q and not a.queue:
            sys.exit(f"대기·진행 중 장애가 {len(q)}건 있다 — 겹치면 값이 섞인다. "
                     f"--queue 를 주면 {free:%H:%M} 에 줄을 선다")
        start = free if q else None
    else:
        if at < free:
            sys.exit(f"{at:%m-%d %H:%M} 은 앞 장애와 겹친다 — {free:%m-%d %H:%M} 이후로 준다")
        start = at
    dur = _dur(s, a.dur)
    shown = start or datetime.now()
    print(_line(_post(a.no, s, start, dur, a.note, a.dry_run), a.no, s, shown, dur,
                _warn(a.no, s, shown, dur)))


def cmd_random(a, sc):
    pool = sorted(n for n in _parse_only(a.only, sc)
                  if not sc[n].get("same_as") and n not in NO_EVIDENCE)
    if not pool:
        sys.exit("고를 시나리오가 없다 — --only 를 본다")
    rng = random.Random(a.seed)
    t = max(_free_from(queue(), sc, a.gap), _parse_at(a.start) if a.start else datetime.now())
    t = (t + timedelta(minutes=1)).replace(second=0, microsecond=0)
    bag: list[int] = []
    plan = []
    last = None
    while len(plan) < a.count:
        if not bag:                                       # 한 바퀴 다 돌기 전에는 반복하지 않는다
            bag = pool[:]
            rng.shuffle(bag)
            if len(bag) > 1 and bag[-1] == last:
                bag[0], bag[-1] = bag[-1], bag[0]
        ok = [n for n in bag if not a.hours or in_hours(sc[n], t)]
        if not ok:                                        # 이 시각에 맞는 번호가 없다 — 10분 뒤로
            t += timedelta(minutes=10)
            if t > datetime.now() + timedelta(days=2):
                sys.exit("48시간 안에 권장 시간대가 맞는 번호가 없다 — --no-hours 나 --only 를 본다")
            continue
        no = ok[-1]
        bag.remove(no)
        dur = _dur(sc[no], a.dur)
        plan.append((no, t, dur))
        last = no
        t = t + timedelta(minutes=dur + _settle(sc[no]) + a.gap)
    for no, start, dur in plan:
        s = sc[no]
        print(_line(_post(no, s, start, dur, a.note, a.dry_run), no, s, start, dur,
                    _warn(no, s, start, dur)))
    end = plan[-1][1] + timedelta(minutes=plan[-1][2])
    print(f"{len(plan)}건 · {plan[0][1]:%m-%d %H:%M} ~ {end:%m-%d %H:%M}"
          + ("  — dry-run, 넣지 않았다" if a.dry_run else ""))


def main() -> None:
    ap = argparse.ArgumentParser(description="장애 주입 — 바로 · 예약 · 랜덤 순차 예약 (gen-ui API)")
    ap.add_argument("--gap", type=int, default=DEFAULT_GAP, help=f"장애 사이 쿨다운 분 (기본 {DEFAULT_GAP})")
    ap.add_argument("--dry-run", action="store_true", help="계획만 찍고 넣지 않는다")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="시나리오 목록")
    sub.add_parser("queue", help="대기·진행 중 장애")
    for name in ("now", "at"):
        p = sub.add_parser(name, help="지정 번호 " + ("바로" if name == "now" else "예약"))
        p.add_argument("no", type=int)
        if name == "at":
            p.add_argument("when", help="'HH:MM' 또는 'YYYY-MM-DD HH:MM'")
        else:
            p.add_argument("--queue", action="store_true", help="앞 장애가 있으면 끝난 뒤로 줄을 선다")
        p.add_argument("--dur", type=int, help="지속 분 (기본 권장 시간)")
        p.add_argument("--note", default="inject.py")
    r = sub.add_parser("random", help="랜덤 N개 순차 예약")
    r.add_argument("count", type=int)
    r.add_argument("--start", help="첫 시작 'HH:MM' 또는 'YYYY-MM-DD HH:MM' (기본 큐 끝)")
    r.add_argument("--only", help="고를 번호 범위 예: 1-16,21-25")
    r.add_argument("--dur", type=int, help="모든 건의 지속 분 (기본 번호별 권장 시간)")
    r.add_argument("--seed", type=int, help="같은 순서를 다시 뽑을 때")
    r.add_argument("--no-hours", dest="hours", action="store_false", help="권장 시간대를 보지 않는다")
    r.add_argument("--note", default="inject.py random")
    a = ap.parse_args()
    sc = scenarios()
    if a.cmd == "list":
        cmd_list(a, sc)
    elif a.cmd == "queue":
        cmd_queue(a, sc)
    elif a.cmd == "now":
        cmd_one(a, sc, None)
    elif a.cmd == "at":
        cmd_one(a, sc, _parse_at(a.when))
    else:
        if a.count < 1:
            sys.exit("count 는 1 이상")
        cmd_random(a, sc)


if __name__ == "__main__":
    main()
