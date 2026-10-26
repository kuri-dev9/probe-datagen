"""홈 PLMN 과 IMSI 접두 — **한 값에서 유도한다.**

## 왜 모듈 하나인가

이 값들이 세 파일에 흩어진 리터럴이었다 — `pemdb1.py` 의 `plmn="45005"`,
같은 파일의 `"4500503"`/`"2340500"`, `generator.py` 의 `"4500503"` 둘.
**한쪽만 바꾸면 전 가입자가 외국 가입자가 된다** —

    셀의 PLMN        "45005"
    가입자 IMSI 접두  "4500503"  →  앞 5자 "45005"      ← 일치해야 한다

지금은 로밍 판정 로직이 없어 증상이 안 나오지만, `identity.py` 머리말이
*"IMSI 앞 7자로 사업자·로밍을 가른다"* 고 적어 둔 분석을 넣는 순간 **전부
로밍으로** 나온다. 에러는 나지 않는다.

## 설정에 7자 칸을 만들지 않는다

    plmn:
      home: "45005"            # 셀 PLMN · 5자 (MCC 3 + MNC 2)
      imsi_msin_head: "03"     # IMSI 접두 7자 = home + 이것
      roaming: "2340500"       # 인바운드 로머 · 7자

`imsi_mcc_mnc_info`(규격 폭 7자)를 **`home + imsi_msin_head` 로 유도**한다.
칸을 둘 두면 한쪽만 고쳐지는 같은 함정이 **설정 파일로 옮겨간다.**

일본은 `home: "44010"` · `imsi_msin_head: "01"` → `4401001`(docomo)다.
MCC 3 + MNC 2 가 한국과 같은 모양이라 IMSI 15자리 구성이 한 자리도
어긋나지 않는다(`identity.imsi` 가 7자가 아니면 `ValueError` 다).

## locale 과 자동으로 엮지 않는다

`topo --locale jp` 가 식별자까지 바꾸면 **지리만 바꾸려던 사람이 놀란다.**
사람이 설정에서 바꾸고, `prod.yaml.example` 이 일본 값을 주석으로 둔다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_DIGITS = re.compile(r"^\d+$")


@dataclass(frozen=True)
class Plmn:
    """홈 PLMN · IMSI 접두 · 로밍 PLMN. 기본값은 한국(현 동작)이다."""

    home: str = "45005"
    imsi_msin_head: str = "03"
    roaming: str = "2340500"

    @property
    def imsi_prefix(self) -> str:
        """`imsi_mcc_mnc_info` — **유도값이다.** 설정에 칸이 없다."""
        return self.home + self.imsi_msin_head

    def normalize(self, raw: str | None) -> str:
        """프로파일에서 뽑은 7자 값을 **이 설정의 PLMN 으로 옮긴다.**

        **설정이 프로파일을 이긴다.** `data/profile.json` 의
        `imsi_mcc_mnc_info` 분포는 상용 실측이라 전부 한국 값
        (`4500502`~`4500513` · `4501212` · `234xxxx`)이다. 그대로 쓰면
        설정을 `44010` 으로 바꿔도 가입자 IMSI 가 `450` 으로 나오고
        **에러가 없다.**

        분포의 **모양은 유지한다** — 끝 2자(사업자 하위 버킷)를 그대로 쓰고
        앞 5자만 `home` 으로 바꾼다. 버리면 가입자가 한 접두에 몰려
        UE·사업자 축의 다양성이 사라진다.

        로밍(`roaming` 과 같은 MCC 3자)은 **그대로 둔다** — 일본 망에
        외국 로머가 있는 것이 맞고, 그것까지 홈으로 바꾸면 프로파일의
        로밍 비율이 사라진다.

        7자 숫자가 아니면(프로파일에 `""` 1.2% · `"__other__"` 0.6% 가 있다)
        `imsi_prefix` 로 채운다 — 빈 값을 쓰면 IMSI 를 만들 수 없고,
        두 컬럼이 어긋나면 규격이 선언한 관계가 깨진다.
        """
        v = str(raw or "")
        if not (len(v) == 7 and _DIGITS.match(v)):
            return self.imsi_prefix
        if v[:3] == self.roaming[:3]:
            return v                      # 로밍 — 손대지 않는다
        return self.home + v[5:]

    @classmethod
    def from_cfg(cls, cfg: dict | None) -> "Plmn":
        """설정 → `Plmn`. **로드 시점에 검증한다.**

        `identity.imsi` 가 7자가 아니면 `ValueError` 를 던지는데, 그것은
        가입자 풀을 만드는 **한참 뒤**다. 거기서 터지면 "생성이 깨졌다"로
        읽혀 설정을 의심하지 않는다 — 그래서 여기서 멈춘다.
        """
        d = dict(cfg or {})
        p = cls(home=str(d.get("home", cls.home)),
                imsi_msin_head=str(d.get("imsi_msin_head", cls.imsi_msin_head)),
                roaming=str(d.get("roaming", cls.roaming)))
        for name, val, n in (("plmn.home", p.home, 5),
                             ("plmn.imsi_msin_head", p.imsi_msin_head, 2),
                             ("plmn.roaming", p.roaming, 7)):
            if len(val) != n or not _DIGITS.match(val):
                raise ValueError(
                    f"{name} 은 숫자 {n}자여야 한다: {val!r}\n"
                    "  MCC 3 + MNC 2 가 PLMN 5자이고 IMSI 접두는 그 뒤 2자를"
                    " 더한 7자다 (규격 `imsi_mcc_mnc_info` 폭).\n"
                    "  한국 45005/03 · 일본 docomo 44010/01 ·"
                    " SoftBank 44020 · KDDI 44050")
        if len(p.imsi_prefix) != 7:
            raise ValueError(
                f"plmn.home + plmn.imsi_msin_head 가 7자가 아니다: "
                f"{p.imsi_prefix!r}")
        if p.roaming[:3] == p.home[:3]:
            # **자리수만 보면 이것을 못 잡는다.** 로밍 값을 실수로 자국
            # MCC 로 두면 `normalize` 의 로밍 가지(`v[:3] == roaming[:3]`)가
            # 홈 가입자까지 통과시키고, `_reseat_subscribers` 의 2.6% 도
            # 홈과 구분되지 않는다 — **로밍이 조용히 0이 된다.**
            raise ValueError(
                f"plmn.roaming 의 MCC 가 plmn.home 과 같다: "
                f"{p.roaming[:3]} — 그러면 로밍 가입자가 홈과 구분되지 않아 "
                "로밍 비율이 조용히 0이 된다.\n"
                "  로밍은 **다른 나라 MCC** 다 — 영국 234 · 한국 450 · 일본 440")
        return p
