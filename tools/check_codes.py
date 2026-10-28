#!/usr/bin/env python3
"""`config/callflow.yaml` 의 Cause·Message 가 **코드 표에 있는가.**

## 왜 도구로 두나

명세에 코드를 손으로 적는다. 숫자 하나가 표 밖이면 —

* 에러가 나지 않는다. 레코드는 그대로 만들어진다.
* 화면이 그 호를 `UNKNOWN` 으로 적고 `H-17`(미정의 코드 출현률)이 오른다.
* **의미가 더 정확한 코드를 쓰려다 그 호가 무엇인지 잃는다.**

실제로 2026-10-28 첫 판에 넷이 틀렸다 —

    ctxsetup_failure   S1AP 21·22·26    S1AP Cause 는 백의 자리가 그룹이다
    dra_route_failure  Diameter 3003    대역은 맞고 표에 없다
    meidentity_reject  Diameter 5422    TS 29.272 에는 있고 이 표에 없다

## 코드 표를 베끼지 않는다

정본은 **graphio 의 `schema/codes.py`** 이고 그쪽이 `XDR_Cause_v0.5.xlsx`
를 옮긴 것이다. 여기서 표를 복사하면 두 벌이 되고 한쪽만 고쳐진다
(불변규칙 1 의 취지). 그래서 **형제 디렉터리에 graphio 가 있을 때만**
돌고, 없으면 건너뛴다고 말한다.

    python3 tools/check_codes.py
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
GRAPHIO = HERE.parent / "graphio-probe-rca" / "src"


def main() -> int:
    if not GRAPHIO.exists():
        print(f"건너뛴다 — graphio 가 없다: {GRAPHIO}")
        print("  코드 표의 정본이 그쪽이고 여기서 베끼지 않는다.")
        return 0
    sys.path.insert(0, str(HERE / "src"))
    sys.path.insert(0, str(GRAPHIO))
    from datagen.callflow import load_spec
    from graphio.schema import codes as C

    # 구간 → (이름, Cause 표, Message 표)
    TAB = {
        1: ("S6a", C.DIAMETER_CAUSE, C.DIAMETER_MESSAGE),
        2: ("S1AP", C.S1AP_CAUSE, C.S1AP_MESSAGE),
        3: ("S11", C.GTPV2_CAUSE, C.GTPV2_MESSAGE),
        4: ("S10", C.GTPV2_CAUSE, C.GTPV2_MESSAGE),
        5: ("EMM", C.EMM_CAUSE, C.EMM_MESSAGE),
        6: ("ESM", C.ESM_CAUSE, C.ESM_MESSAGE),
        8: ("S13", C.DIAMETER_CAUSE, C.DIAMETER_MESSAGE),
    }
    spec = load_spec()
    bad: list[str] = []

    print("── 단계 — 구간과 메시지")
    for name, st in sorted(spec.steps.items()):
        if st.iface not in TAB:
            bad.append(f"steps[{name}].iface = {st.iface} 는 모르는 구간이다")
            continue
        nm, _, msgs = TAB[st.iface]
        if st.error_message and st.error_message not in msgs:
            bad.append(f"steps[{name}].error_message = {st.error_message} "
                       f"가 {nm} 메시지 표에 없다")
            mark, label = "✗", "표에 없다"
        else:
            mark = "✓"
            label = msgs.get(st.error_message, "—")
        print(f"  {mark} {name:10s} {nm:5s} msg {st.error_message:4d}  {label}")

    print("\n── 장애 — Cause")
    for name, f in sorted(spec.faults.items()):
        st = spec.steps[f.break_at]
        nm, causes, _ = TAB[st.iface]
        for c in f.causes:
            # TIMEOUT 은 **구간 Cause 표에 없는 것이 맞다** — 프로브가 만드는
            # 공통 센티널이고 통합 규격이 `first_error_cause` 에 포함한다.
            if c == C.TIMEOUT_CAUSE:
                print(f"  ✓ {name:24s} {nm:5s} {c:6d}  TIMEOUT (공통 D.2)")
                continue
            if c in causes:
                print(f"  ✓ {name:24s} {nm:5s} {c:6d}  {causes[c]}")
            else:
                bad.append(f"faults[{name}].causes 의 {c} 가 {nm} 표에 없다")
                print(f"  ✗ {name:24s} {nm:5s} {c:6d}  표에 없다")
        d = f.downstream
        if d:
            nm2, causes2, _ = TAB[d["iface"]]
            c2 = d["cause"]
            if c2 in causes2:
                print(f"    ↳ 하류 {nm2:5s} {c2:6d}  {causes2[c2]}")
            else:
                bad.append(f"faults[{name}].downstream.cause = {c2} 가 "
                           f"{nm2} 표에 없다")
                print(f"    ✗ 하류 {nm2:5s} {c2:6d}  표에 없다")

    # 규격이 금지한 구간이 섞였는가 — `load_spec` 이 이미 막지만 여기서도 말한다.
    print("\n── 규격 제약")
    s3 = [n for n, st in spec.steps.items() if st.iface == 7]
    print(f"  {'✗' if s3 else '✓'} S3(7)를 First Error 에 쓰지 않는다"
          + (f" — {s3}" if s3 else ""))

    print()
    if bad:
        print(f"✗ {len(bad)}곳이 코드 표 밖이다")
        for b in bad:
            print(f"   {b}")
        return 1
    print("✓ 전부 코드 표에 있다")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
