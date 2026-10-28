#!/usr/bin/env python3
"""실측 XDR(정답지)에서 **패턴 뱅크**를 뽑는다.

## 왜 패턴인가

명세로 레코드를 조립하면 정답지가 이미 담고 있는 것을 **다시 적어야**
한다 — 성공 Cause(`2001`) · 하류 EMM 대응 · 해제 사유 분포 · 단계별
발생률. 하나씩 실측해 명세에 옮기는 동안 네 군데가 틀렸고, 그때마다
정답지를 다시 읽어 고쳤다.

**패턴은 그 왕복을 없앤다.** 실측 레코드를 틀로 쓰고 *달라지는 부분만*
치환하면 —

* 성공 Cause·하류 맵·release 규칙이 **공짜로** 맞는다
* `SPID`(140번)처럼 **정체를 모르는 필드도** 올바른 값이 들어간다
* 조립 로직이 사라져 자원도 덜 쓴다

사용자 지시 2026-10-28: *"정답지 자체를 패턴화 해놓고 달라지는 부분만
계속적으로 바꿔가면 변경해서 쓰면 더 쉽고 자원도 적게 쓰면서 할 수 있을꺼야."*

## 원천은 `data/sample/` 이다 — `data/xdr/` 가 아니다

둘의 성격이 다르다 (2026-10-28 실측) —

| | `sample/aa·bb.txt` | `data/xdr/lte-call/` |
| --- | ---: | ---: |
| 호 | 204 | 60만 |
| 오류 비율 | **96%** | 2% |
| 오류 `(iface, cause)` 고유 | **57종** | 10종 |
| `AuthenticationInformation_Cause` | **8종** | **0종 — 한 번도 안 쓴다** |
| `ground_truth.json` | 없음 | **있다**(SC-01~13 주입) |

`data/xdr/` 는 **1세대 시뮬레이터 산출물**이다. 절차 Cause 를 비우고
해제 사유도 `120`(User inactivity)에 몰려 있다 — 섞으면 20만호가 204호를
압도해 정답지의 다양성이 희석된다. 사용자 확정: *"xdr 샘플이 실제
정답지"*.

표본이 작은 것은 **치환으로 푼다** — 시각·식별자·셀만 갈아 끼우면
한 패턴에서 얼마든지 찍어낼 수 있고, **정답지 밖의 값은 섞이지 않는다.**

## 실측 암호문을 저장소에 두지 않는다

원천(`data/xdr/lte-call/`)은 **실제 가입자 암호문과 기지국 자산 대장**을
담는다 — `.gitignore` 가 `data/` 를 통째로 막는 이유다. 패턴에는 그
자리를 **빈 칸으로** 남기고 생성 시 합성값으로 채운다.

## 시각은 상대값이다

`call_start_time` 을 `0` 으로 두고 나머지 `*_Time` 을 그 차이로 적는다.
그래야 패턴을 **아무 분에나** 놓을 수 있고, 단계 간 간격은 실측 그대로
유지된다.

    python3 tools/extract_patterns.py [--minutes 30] [--per-key 40]
"""

from __future__ import annotations

import argparse
import collections
import json
import random
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "tools"))
from read_sample import DELIM, field_names  # noqa: E402

#: **정답지.** 실측 XDR 표본이다 (사용자 확정 2026-10-28).
SAMPLE_DIR = HERE.parent / "graphio-data-generator" / "data" / "sample"

#: 1세대 시뮬레이터 산출물. **정답지가 아니다** — `--with-xdr` 로만 섞는다.
XDR_DIR = HERE.parent / "graphio-data-generator" / "data" / "xdr" / "lte-call"
OUT = HERE / "data" / "callflow" / "patterns.jsonl"

#: **생성 시 반드시 갈아 끼우는 자리.** 실측값을 패턴에 남기지 않는다 —
#: 가입자 암호문·실제 기지국 ID·사람이 쓴 이름이 여기 들어 있다
#: (불변규칙 13·14 · `.gitignore` 의 자산 대장 항목).
WIPE = (
    # 가입자·단말 식별자 (암호문)
    "IMSI", "MDN", "IMEI", "SERVICECODE",
    # 민감 — `privacy.drop_at_ingest`
    "ExchangeNumber", "Gender", "Age",
    # 셀·장비 — 실제 자산 대장
    "First_eNB_ID", "First_Cell_ID", "First_TAC", "First_eNB_VLAN_ID",
    "First_eNB_C_UID", "First_eNB_PLMN",
    "Last_eNB_ID", "Last_Cell_ID", "Last_TAC", "Last_eNB_VLAN_ID",
    "Last_eNB_C_UID", "Last_eNB_PLMN",
    "interval_First_eNB_ID", "interval_First_Cell_ID", "interval_First_TAC",
    "interval_First_eNB_VLAN_ID", "interval_First_eNB_C_UID",
    "interval_First_eNB_PLMN",
    "old_call_Last_eNB_ID", "old_call_Last_Cell_ID", "old_call_Last_TAC",
    "old_call_Last_eNB_PLMN",
    "MME_ID", "SGW_ID", "PGW_ID", "ims_PGW_ID",
    "imsi_mcc_mnc_info",
)

#: 시각 필드 — `call_start_time` 기준 **상대 초**로 바꿔 적는다.
TIME_SUFFIX = ("_Time", "_time")
TIME_EXTRA = ("SummaryCreateTime",)

#: **이름이 `_time` 인데 시각이 아닌 것.** `call_duration_time` 은 호의
#: **길이(µs)** 다 — 상대화하면 `@-1787215658` 같은 값이 나온다(실제로
#: 첫 추출이 그랬다). 접미사로만 가르면 걸리는 유일한 자리다.
#: `initial_nr_conn_time` 은 epoch 소수라 시각이 맞다
#: (`cell_agg` 머리말의 실측값 `1791960060.266319`).
NOT_TIME = frozenset({"call_duration_time"})

#: 패턴을 가르는 키. 오류가 1.86% 로 드물어 `cause` 까지 넣어도 조합이
#: 적다(실측 10만 호에서 41종).
KEY_FIELDS = ("call_type", "attempt_flag", "success_flag",
              "first_error_interface_protocol", "first_error_cause")

#: U-Plane 에서 **반드시 갈아 끼우는 자리.** C-Plane 의 `WIPE` 와 같은
#: 이유다 — 가입자 암호문·실제 기지국 ID·사람이 쓴 이름·접속한 서버.
#:
#: **`SessionID` 가 `IMSI` 를 통째로 품는다** (실측 2026-10-28) —
#:
#:     2887582732.950798296.6.<IMSI>.1787682997.434389
#:     └ IP(uint)  └ port    └proto  └ 가입자  └ 세션 시작 epoch.µs
#:
#: 그래서 `IMSI` 를 바꾸면 `SessionID` 도 **다시 만들어야 한다.**
#: `patterns.新session_id` 가 그 일을 한다.
UPLANE_WIPE = (
    # 가입자·단말
    "IMSI", "MDN", "IMEI", "ServiceCode", "imsi_mcc_mnc_info",
    "SessionID",
    # 민감 — `privacy.drop_at_ingest` (일본은 적재 단계에서 뺀다)
    "ExchangeNumber", "Gender", "Age",
    "URI", "Referer", "Referer_Prefix", "Referer_Site", "UserAgent",
    # 셀·장비 — 실제 자산 대장
    "eNB_ID", "Cell_ID", "MME_ID", "SGW_ID", "PGW_ID",
    # APN · 주소 — **C-Plane 호에서 물려받는다**(한 베어러의 APN 이
    # 계열마다 다를 수 없다 · `generator.uplane_rec` 주석)
    "APN",
    "User_IPv4", "User_IPv6", "App_IPv4", "App_IPv6",
)

#: U-Plane 에서 패턴을 가르는 키. C-Plane 과 달리 `call_type` 이 없고
#: **그 줄이 세션의 어디인가**(`OngoingFlag`)와 **결말**이 축이다.
#:
#: `transport` 에는 `ReplyCode` 가 없다 — 결말은 `RstCount`(RST 로 끊겼나)
#: 가 말한다. 실측 2만 행에서 `0` 72% · `1` 19% · `2` 6% 다.
UPLANE_KEY = {
    "transport": ("OngoingFlag", "_RST"),
    "dns": ("OngoingFlag", "ReplyCode"),
    "http": ("OngoingFlag", "StatusCode"),
}

#: **`OngoingFlag = 4` 를 `1` 로 접는다** (사용자 결정 2026-10-28).
#:
#: 실측 프로브는 그 값을 낸다 — `transport` 6.1% · `http` 1.7%. 그러나
#: 우리는 쓰지 않기로 했다 —
#:
#: > *"handover 자체는 나중에 통계 생성시에 알 수 있어. call 쪽에서 ho 가
#: > 된다면 그 시점에 사용한 데이터 전부 ho 에 들어가는 걸로 처리하면 돼.
#: > 그리고 사실 up 는 ho 랑 관련도 없잖아. 그냥 spgw 만 바뀌는 거잖아."*
#:
#: U-Plane 세션은 **S/PGW 를 타는 베어러**다. 핸드오버는 eNB 쪽 사건이고,
#: 전환 귀속은 **집계가** C-Plane 의 HO 시점으로 그 구간 U-Plane 을 묶어
#: 푼다(스펙 §4.4 의 CP–UP 조인). 레코드에 표시할 일이 아니다.
#:
#: **그 줄의 나머지 값은 유효하다** — 트래픽량·RTT·재전송이 다 들어 있다.
#: 버리지 않고 `1 Interim` 으로 접는다.
FOLD_HO_FLAG = {"4": "1"}


def is_time(name: str) -> bool:
    if name in NOT_TIME:
        return False
    return name.endswith(TIME_SUFFIX) or name in TIME_EXTRA


def to_pattern(rec: dict) -> dict | None:
    """레코드 한 장 → 패턴. 시각을 상대화하고 식별자를 비운다."""
    try:
        t0 = float((rec.get("call_start_time") or "0").strip() or 0)
    except ValueError:
        return None
    if t0 <= 0:
        return None
    # **셀이 바뀌었는지는 값을 비우기 전에 기록한다.** `First_*` 와
    # `Last_*` 를 둘 다 지우면 "한 호 안에서 전환이 있었다" 는 사실이
    # 사라진다 — 정답지에 그런 호가 call_type 3(14) · 5(4) · 4(1) 있고,
    # `PathSwitch_count` 와 짝이 된다.
    moved = (rec.get("First_eNB_ID") != rec.get("Last_eNB_ID")
             or rec.get("First_Cell_ID") != rec.get("Last_Cell_ID"))
    out = {"_moved": "1" if moved else "0"}
    for k, v in rec.items():
        v = (v or "").strip()
        if k in WIPE:
            out[k] = ""
            continue
        if is_time(k) and v not in ("", "0", "0.0"):
            try:
                out[k] = f"@{float(v) - t0:.6f}"      # `@` = 상대 초
            except ValueError:
                out[k] = v
            continue
        out[k] = v
    return out


def read_records(paths, names):
    for p in paths:
        for line in Path(p).open(encoding="utf-8", errors="replace"):
            line = line.rstrip("\n")
            if not line.strip():
                continue
            v = line.split(DELIM)
            if len(v) != len(names):
                continue
            yield dict(zip(names, v))


def uplane_pattern(rec: dict, series: str) -> dict | None:
    """U-Plane 레코드 한 장 → 패턴.

    C-Plane 과 같은 규율이다 — 식별자·자산 ID 를 비우고 시각을 상대화한다.
    기준 시각은 `SummaryCreateTime`(그 줄이 만들어진 때)이다.
    """
    try:
        t0 = float((rec.get("SummaryCreateTime") or "0").strip() or 0)
    except ValueError:
        return None
    if t0 <= 0:
        return None
    out: dict[str, str] = {}
    for k, v in rec.items():
        v = (v or "").strip()
        if k in UPLANE_WIPE:
            out[k] = ""
            continue
        if k == "OngoingFlag":
            out[k] = FOLD_HO_FLAG.get(v, v)
            continue
        if is_time(k) and v not in ("", "0", "0.0"):
            try:
                out[k] = f"@{float(v) - t0:.6f}"
            except ValueError:
                out[k] = v
            continue
        out[k] = v
    return out


def uplane_key(rec: dict, series: str) -> tuple:
    ks = UPLANE_KEY[series]
    out = []
    for k in ks:
        if k == "_RST":
            v = (rec.get("RstCount") or "0").strip()
            out.append("1" if v not in ("", "0") else "0")
        else:
            raw = (rec.get(k) or "").strip()
            out.append(FOLD_HO_FLAG.get(raw, raw) if k == "OngoingFlag" else raw)
    return tuple(out)


def extract_uplane(series: str, per_key: int, limit: int,
                   rng: random.Random) -> dict[tuple, list]:
    """계열 하나의 패턴 뱅크. 저수지 추출로 앞쪽 파일에 쏠리지 않게 한다."""
    import read_sample as RS

    names = RS.field_names(series)
    bank: dict[tuple, list] = collections.defaultdict(list)
    seen: collections.Counter = collections.Counter()
    n = 0
    for p in RS.sample_files(series):
        for line in Path(p).open(encoding="utf-8", errors="replace"):
            line = line.rstrip("\n")
            if not line.strip():
                continue
            v = line.split(DELIM)
            if len(v) != len(names):
                continue
            rec = dict(zip(names, v))
            n += 1
            key = uplane_key(rec, series)
            seen[key] += 1
            got = bank[key]
            if len(got) < per_key:
                pat = uplane_pattern(rec, series)
                if pat:
                    got.append(pat)
            elif rng.random() < per_key / seen[key]:
                pat = uplane_pattern(rec, series)
                if pat:
                    got[rng.randrange(per_key)] = pat
            if limit and n >= limit:
                break
        if limit and n >= limit:
            break
    for key in bank:
        bank[key] = (bank[key], seen[key])
    return bank


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--with-xdr", action="store_true",
                    help="1세대 산출물(`data/xdr/`)을 섞는다 — **기본은 안 쓴다**")
    ap.add_argument("--minutes", type=int, default=60,
                    help="`--with-xdr` 일 때 훑을 분 파일 수")
    ap.add_argument("--per-key", type=int, default=40,
                    help="조합당 보관할 패턴 수. 정답지가 204호뿐이라 "
                         "기본값이면 **사실상 전량**이 들어간다")
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--series", default="all",
                    help="lte_call · transport · dns · http · all")
    ap.add_argument("--limit", type=int, default=100000,
                    help="U-Plane 계열당 훑을 줄 수 (파일이 10만 줄이다)")
    a = ap.parse_args()

    want = (["lte_call", "transport", "dns", "http"] if a.series == "all"
            else [a.series])
    if want != ["lte_call"]:
        rng = random.Random(20261028)
        total = 0
        for series in want:
            if series == "lte_call":
                continue
            bank = extract_uplane(series, a.per_key, a.limit, rng)
            out = Path(a.out).parent / f"patterns_{series}.jsonl"
            out.parent.mkdir(parents=True, exist_ok=True)
            n = 0
            with out.open("w", encoding="utf-8") as f:
                for key, (pats, w) in sorted(bank.items()):
                    for p in pats:
                        f.write(json.dumps({"key": list(key), "weight": w,
                                            "rec": p},
                                           ensure_ascii=False) + "\n")
                        n += 1
            total += n
            print(f"  {series:10s} 조합 {len(bank):3d}종 · 패턴 {n:5,d} → {out.name}")
        if a.series != "all":
            return 0
        print()

    names = field_names()
    files = sorted(SAMPLE_DIR.glob("*.txt"))
    if not files:
        print(f"정답지가 없다: {SAMPLE_DIR}")
        return 1
    if a.with_xdr:
        files += sorted(XDR_DIR.glob("*/*.dat"))[:a.minutes]

    rng = random.Random(20261028)
    bank: dict[tuple, list] = collections.defaultdict(list)
    seen = collections.Counter()
    total = 0
    for rec in read_records(files, names):
        total += 1
        key = tuple((rec.get(k) or "").strip() for k in KEY_FIELDS)
        seen[key] += 1
        got = bank[key]
        # 저수지 추출 — 앞쪽 파일에 쏠리지 않게 한다.
        if len(got) < a.per_key:
            p = to_pattern(rec)
            if p:
                got.append(p)
        elif rng.random() < a.per_key / seen[key]:
            p = to_pattern(rec)
            if p:
                got[rng.randrange(a.per_key)] = p

    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with out.open("w", encoding="utf-8") as f:
        for key, pats in sorted(bank.items()):
            for p in pats:
                f.write(json.dumps({"key": list(key), "weight": seen[key],
                                    "rec": p}, ensure_ascii=False) + "\n")
                n += 1
    print(f"원천 {total:,}호 · 조합 {len(bank)}종 · 패턴 {n:,} → {out}")
    print(f"  (조합당 최대 {a.per_key} · `weight` 가 실측 구성비다)")
    err = {k: v for k, v in seen.items() if k[3] not in ("", "0")}
    print(f"  오류 조합 {len(err)}종 · {sum(err.values()):,}호 "
          f"({sum(err.values())*100.0/total:.2f}%)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
