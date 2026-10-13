#!/usr/bin/env python3
"""반출 `CM_CELL_REP_RU_LOC.json` 의 eNB 키를 **평문으로 통일**한다.

## 왜

같은 개념의 식별자가 세 체계로 흩어져 있었다.

```
CL_CELL_MAP.ENB_ID              평문 숫자   '157837'
CL_ENB_ORG.ENB_ID               평문 숫자   '134364937'
CM_CELL_REP_RU_LOC.ENB_ID       base64      'q+10PhUBMb668gEmDj1edA=='   ← 이것만 달랐다
  └ 같은 테이블 DECODE 컬럼     평문        '1562'
```

**로컬 DB 에는 이미 `ENB_ID` 자리에 `DECODE`(평문)를 넣어 적재해 왔다**
(`load_master.py`). 그런데 반출 JSON 은 암호문이라 같은 데이터가 두 체계로
존재했고, 그 때문에 조인이 **에러 없이 0행**이 된 사고가 있었다
(`data/pemdb1/repairs/gen_ru_name.py` 의 반출 JSON 교정 블록 주석).

이 스크립트가 JSON 쪽을 DB 쪽에 맞춘다. 그러면 체계가 하나가 되고, 적재
단계의 치환 로직이 필요 없어진다.

## 암호문을 지우지 않는다

`ENB_ID_ENC` 로 **남긴다.** 두 가지 이유다.

1. 되돌릴 수 없는 변경을 만들지 않는다. 운영 암호문과 대조해야 할 수 있다.
2. **상용 XDR 의 `FIRST_ENB_ID` 가 암호문일 가능성이 남아 있다.** 상용 XDR
   원시행은 한 번도 받지 못했고(`_manifest.json` 의 `missing.XDR 원시행`),
   `FIRST_ENB_ID` 는 `varchar(30)` 으로 **암호문 4컬럼(`IMSI`·`mdn`·`IMEI`·
   `SERVICECODE`)과 같은 폭**이다. 만약 상용이 암호문이면 이 테이블이
   **암호문 → 평문 사전**이 되고, 그 사전이 `ENB_ID_ENC` 다.

`DECODE` 도 그대로 둔다 — 상용에 실재하는 컬럼이고, `gen_ru_name.py --restore`
가 그 키로 되돌린다.

## DDL 은 건드리지 않는다

`pemdb1_local.sql` 의 `CM_CELL_REP_RU_LOC` 는 상용 `SHOW CREATE TABLE` 산출물이고
`ENB_ID_ENC` 가 없다. **거기에 컬럼을 더하면 로컬이 상용과 다른 구조가 되어 두
환경을 대조할 수 없다.** 그래서 DDL 대신 **적재기**를 고쳤다 — `load_master.py`
가 DDL 에 있는 컬럼만 넣으므로 `ENB_ID_ENC` 는 JSON 에만 남고 DB 에는 안 들어간다.

## 안전장치

`(DECODE, SECTOR)` 가 평문 기준으로도 유일해야 한다. 아니면 **서로 다른 셀이
하나로 합쳐진다** (graphio 불변규칙 14 가 경고하는 사고). 실측으로 암호문
45,927개와 평문 45,927개가 1:1 이고 `(DECODE, SECTOR)` 중복이 0건이지만,
검사를 코드에 남겨 둔다 — 다음 반출본이 다를 수 있다.

멱등하다. 이미 `ENB_ID_ENC` 가 있고 `ENB_ID` 가 평문이면 아무것도 쓰지 않는다.

    python3 normalize_enb_key.py --dry-run
    python3 normalize_enb_key.py
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

B64 = re.compile(r"^[A-Za-z0-9+/]{16,}={0,2}$")
EXPORT = Path(__file__).resolve().parents[2] / "data" / "pemdb1" / "CM_CELL_REP_RU_LOC.json"

_DEC = json.JSONDecoder()


def records(text: str):
    """레코드를 하나씩 떼어 낸다.

    165MB 파일을 `json.load` 로 통째로 dict 로 만들면 수 GB 를 쓴다. 텍스트는
    읽되 객체는 스트리밍으로 만들어 변환 즉시 버린다.
    """
    i = text.index("[") + 1
    while True:
        while i < len(text) and text[i] in " \n\r\t,":
            i += 1
        if i >= len(text) or text[i] == "]":
            return
        obj, i = _DEC.raw_decode(text, i)
        yield obj


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="검사만 하고 쓰지 않는다")
    ap.add_argument("--file", default=str(EXPORT))
    args = ap.parse_args()

    path = Path(args.file)
    text = path.read_text(encoding="utf-8")

    # ── 1단계: 검사. 쓰기 전에 충돌을 확인한다 ────────────────────────────
    enc, dec = set(), set()
    pk = Counter()
    pairs: set[tuple[str, str]] = set()
    rows = enc_rows = plain_rows = nodec = 0
    for r in records(text):
        rows += 1
        e, d, s = r.get("ENB_ID"), r.get("DECODE"), r.get("SECTOR")
        if isinstance(e, str) and B64.match(e):
            enc_rows += 1
        else:
            plain_rows += 1
        if d is None or d == "":
            nodec += 1
            continue
        d = str(d)
        enc.add(e)
        dec.add(d)
        pairs.add((e, d))
        pk[(d, str(s))] += 1

    dup = [k for k, c in pk.items() if c > 1]
    print(f"{path.name}  {rows:,}행")
    print(f"  ENB_ID  암호문 {enc_rows:,}행 · 평문 {plain_rows:,}행")
    print(f"  distinct  ENB_ID {len(enc):,}  DECODE {len(dec):,}  쌍 {len(pairs):,}")
    print(f"  DECODE 결측 {nodec:,}행 · (DECODE, SECTOR) 중복 {len(dup):,}건")

    if nodec:
        # 평문이 없는 행을 평문 키로 옮길 방법이 없다. 0 이나 빈 문자열로
        # 채우면 서로 다른 셀이 한 키로 합쳐진다(불변규칙 17).
        print("DECODE 가 빈 행이 있다 — 평문 키를 만들 수 없다. 중단.", file=sys.stderr)
        return 1
    if len(pairs) != len(enc) or len(pairs) != len(dec):
        print("암호문↔평문이 1:1 이 아니다 — 서로 다른 셀이 합쳐진다. 중단.",
              file=sys.stderr)
        return 1
    if dup:
        print(f"(DECODE, SECTOR) 가 유일하지 않다 ({len(dup):,}건) — 중단.",
              file=sys.stderr)
        for k in dup[:5]:
            print(f"    {k} ×{pk[k]}", file=sys.stderr)
        return 1

    if enc_rows == 0:
        print("  이미 평문이다 — 바꿀 것이 없다.")
        return 0
    if args.dry_run:
        print(f"\n--dry-run — {enc_rows:,}행을 평문으로 바꿀 수 있다. 쓰지 않았다.")
        return 0

    # ── 2단계: 변환. 임시 파일에 쓰고 마지막에 교체한다 ─────────────────
    #
    # 통째로 메모리에 들고 덮어쓰면 중간에 끊겼을 때 165MB 원본이 사라진다.
    tmp = path.with_suffix(".json.tmp")
    n = 0
    with tmp.open("w", encoding="utf-8") as f:
        f.write("[")
        first = True
        for r in records(text):
            e = r.get("ENB_ID")
            if isinstance(e, str) and B64.match(e):
                r["ENB_ID_ENC"] = e              # 암호문은 남긴다 — 사전 역할
                r["ENB_ID"] = str(r["DECODE"])   # 평문으로 통일
                n += 1
            if not first:
                f.write(",")
            f.write(json.dumps(r, ensure_ascii=False))
            first = False
        f.write("]")
    os.replace(tmp, path)
    print(f"\n{n:,}행 교정 — ENB_ID ← DECODE(평문) · 암호문은 ENB_ID_ENC 로 보존")
    print(f"  {path}  ({path.stat().st_size / 1048576:.0f}MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
