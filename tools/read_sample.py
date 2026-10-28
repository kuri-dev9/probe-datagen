#!/usr/bin/env python3
"""실측 XDR 샘플(정답지)을 읽는다 — **필드 하나를 보정한다.**

## 왜 보정이 필요한가

`fields.LTE_CALL_FIELDS` 는 **153개**인데 실측 샘플은 **154개**다
(204호 전부 균일). `ExchangeNumber` 앞에 목록에 없는 필드가 하나 있다 —

```
139 InitialUEMessage_Time   1792650298.482881   목록과 일치
140 ???                     0(159) · 21(30) · 118(7) · 126(6) · 108(2)
141 ExchangeNumber          9166 · 2156 · 0411   ← 규격이 적은 "국번 4자리"
142 nas_key_validity        1
143 equip_nw                ''                   ← 실측 전량 공백(기지 사실)
144 First_eNB_PLMN          45005
```

`141` 이 4자리로 전부 고르고(`Counter({4: 204})`) 규격 설명이
*"국번 4자리 (010-ABCD-EFGH)"* 다 — **`ExchangeNumber` 는 141 이고
140 이 유령**이다. 보정하지 않으면 그 뒤 14개 필드가 전부 한 칸씩
밀려 `old_call_error_interface_protocol` 에 PLMN `45005` 가 들어온다.

**140 의 정체는 아직 모른다.** 값이 다섯 개뿐이고 `21`·`108`·`118`·`126`
은 S1AP Cause 처럼 보이지만 단정할 근거가 없다. 규격서
(`XDR_Specification_통합.xlsx`)를 사람이 열어 확인할 자리다.

## 이것은 생성기의 결함이기도 하다

`LTE_CALL_FIELDS` 와 `CPLANE_FIELDS` 가 **둘 다 153** 이고 집합이 같다.
즉 생성기가 내보내는 Parquet 은 실제 XDR 보다 한 컬럼 적다. 적재 쪽이
위치로 맞추면 거기서도 밀린다 — **고칠지는 사람이 판단한다**(민감 필드
영역이라 `privacy.drop_at_ingest` 로 일부러 뺐을 수도 있다).
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
SAMPLE_DIR = HERE.parent / "graphio-data-generator" / "data" / "sample"

#: 목록에 없는 유령 필드의 **샘플 쪽 인덱스**.
GHOST_AT = 140

#: 구분자 — RS(record separator). `cell_agg` 머리말의 `delim=e'\\x1e'` 와 같다.
DELIM = "\x1e"


def field_names() -> list[str]:
    sys.path.insert(0, str(HERE / "src"))
    from datagen.fields import LTE_CALL_FIELDS
    names = list(LTE_CALL_FIELDS)
    return names[:GHOST_AT] + ["_UNKNOWN_140"] + names[GHOST_AT:]


def read(paths=None) -> list[dict]:
    """정답지를 `{필드명: 값}` 목록으로. 길이가 안 맞으면 **멈춘다.**"""
    names = field_names()
    paths = paths or sorted(SAMPLE_DIR.glob("*.txt"))
    out = []
    for p in paths:
        for i, line in enumerate(Path(p).open(encoding="utf-8", errors="replace"), 1):
            line = line.rstrip("\n")
            if not line.strip():
                continue
            v = line.split(DELIM)
            if len(v) != len(names):
                raise ValueError(
                    f"{p}:{i} 필드 {len(v)}개 — 기대 {len(names)}개. "
                    f"샘플 포맷이 바뀌었다면 `GHOST_AT` 을 다시 본다")
            out.append(dict(zip(names, v)))
    return out


def g(r: dict, name: str) -> str:
    return (r.get(name) or "").strip()


def nz(r: dict, name: str) -> bool:
    """값이 **있다**고 볼 것인가. `0`·`-1` 은 센티널이라 없는 것으로 본다."""
    return g(r, name) not in ("", "0", "0.0", "-1")


if __name__ == "__main__":
    rows = read()
    print(f"정답지 {len(rows):,}호 · 필드 {len(field_names())}")
    for n in ("call_type", "success_flag", "old_call_error_interface_protocol",
              "old_call_error_cause", "ExchangeNumber", "equip_nw"):
        import collections
        c = collections.Counter(g(r, n) for r in rows)
        print(f"   {n:34s}", dict(c.most_common(5)))
