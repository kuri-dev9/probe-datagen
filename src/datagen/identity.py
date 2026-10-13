"""가입자·단말 식별자 합성 — `IMSI` · `MDN` · `IMEI`.

## 왜 합성하나

실제 값은 프로브 단계에서 암호화되어 온다(`ofxUSXVv+SqZqYTvB+zCjw==`, base64 24자).
PoC 로컬에는 그 암호문을 두지 않기로 했다(사람 판정). 그런데 **지우거나 일련번호로
바꾸면 체계가 사라진다** — `IMSI` 앞 7자로 사업자·로밍을 가르고, `IMEI` 의 TAC 로
기종을 가르는 분석이 성립하지 않는다. 그래서 **구조가 유효한 합성 평문**을 쓴다.

규격서(`docs/xdr_spec/XDR_Specification_통합.xlsx`, `LTE-Call-KPI` 시트):

      3  IMSI               string / VARCHAR2 / 16
      4  MDN                string / VARCHAR2 / 16
      5  IMEI               string / VARCHAR2 / 16
    134  imsi_mcc_mnc_info  string / VARCHAR2 / 7   "first 7 characters of imsi"

규격 폭 16 은 **평문 폭**이다. 암호문이 24자인 것은 암호화하며 늘어난 것이고,
합성 평문을 표준 15자리로 만들면 16 안에 들어간다.

## 깨뜨리면 조용히 틀리는 것

### 1. `IMSI` 는 그 행의 `imsi_mcc_mnc_info` 로 시작해야 한다

규격이 "first 7 characters of imsi" 라고 못 박았다. 그 컬럼은 **이미 평문**이고
(`'4500503'`·`'2340500'`), 암호문 `IMSI` 는 당연히 그렇게 시작하지 않아
**규격이 선언한 관계가 지금 깨져 있다.** 합성할 때 맞춘다 — 접두를 새로
발명하지 않고 그 컬럼 값을 그대로 가져온다. 사용자가 든 예시
`45005`·`45006` 가 이 `450`+`05` 와 같은 이야기다.

### 2. 매핑은 **1:1(단사)** 이어야 한다

같은 암호문은 항상 같은 합성값으로 가야 하고, 서로 다른 암호문이 한 값으로
합쳐지면 안 된다. 합쳐지면 `UNIQ_IMSI_CNT`·영향 가입자 수가 **줄어들고**
집계·채점이 조용히 흔들린다.

해시를 쓰지 않는다 — 해시는 충돌할 수 있고, 충돌하면 두 가입자가 한 명이 된다.
대신 **정렬된 목록의 인덱스**에 아핀 사상을 걸어 단사를 보장한다
(`_scatter`). 인덱스가 다르면 결과도 반드시 다르다.

### 3. `IMEI` 체크디지트는 Luhn 이다

TAC(8) + SNR(6) + 체크디지트(1) = 15자리. 체크디지트에 아무 숫자나 넣으면
형식만 흉내낸 것이 되고, 단말 검증을 흉내내는 어떤 로직도 통과하지 못한다.

### 4. TAC 는 `Vendor`·`Model` 과 묶는다

같은 기종이면 같은 TAC 다. 실제 IMEI 가 그렇고, 그러지 않으면 단말 기종 축
(SC-08 `UE_MODEL_ISSUE` · 룰 R-10)이 흔들린다 — TAC 로 기종을 가를 수 없게 된다.
"""

from __future__ import annotations

import hashlib

#: 10진 w자리에서의 아핀 사상 계수. 10 과 서로소여야 **단사**다
#: (끝자리가 1·3·7·9 면 된다). 일련번호로 두면 `450050300000001` 처럼
#: 한눈에 가짜인 값이 나오므로 흩뿌리되, 단사는 유지한다.
_AFFINE = {
    8: (36_842_137, 48_271),      # IMSI MSIN
    7: (7_123_457, 9_091),        # MDN 가입자번호 뒤 7자리
    6: (489_133, 1_237),          # IMEI SNR
}
#: MDN 가입자 번호 8자리 — IMSI MSIN 과 다른 계수를 써서 두 값이 서로를
#: 유추하게 하지 않는다 (같은 계수면 MDN = IMSI 뒤 8자리가 된다).
_AFFINE_MDN = (7_123_457, 9_091)


def _scatter(index: int, width: int, coef: tuple[int, int] | None = None) -> str:
    """`index` → w자리 10진 문자열. **단사다.**

    `(a·i + b) mod 10^w` 는 `gcd(a, 10) = 1` 일 때 `Z_10^w` 의 전단사다.
    따라서 서로 다른 `index`(< 10^w)는 서로 다른 값이 된다 — 충돌 검사가
    필요 없다는 것이 해시 대신 이것을 쓰는 이유다.
    """
    a, b = coef or _AFFINE[width]
    mod = 10 ** width
    if index >= mod:
        raise ValueError(f"index {index} 가 {width}자리를 넘는다 — 단사가 깨진다")
    return f"{(a * index + b) % mod:0{width}d}"


def luhn_check_digit(payload: str) -> str:
    """Luhn 체크디지트 1자리 — `payload` 는 체크디지트를 **뺀** 숫자열이다.

    오른쪽에서 두 번째 자리부터 한 칸씩 띄며 2배하고, 10 이상이면 9를 뺀다
    (자릿수 합과 같다). 합을 10의 배수로 만드는 수가 체크디지트다.
    """
    if not payload.isdigit():
        raise ValueError(f"숫자가 아니다: {payload!r}")
    total = 0
    # payload 뒤에 체크디지트가 한 자리 붙을 것이므로, payload 의 맨 오른쪽이
    # "오른쪽에서 두 번째" 자리다 → 거기서부터 2배가 시작된다.
    for i, ch in enumerate(reversed(payload)):
        d = int(ch)
        if i % 2 == 0:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return str((10 - total % 10) % 10)


def luhn_ok(number: str) -> bool:
    """체크디지트까지 포함한 번호가 Luhn 을 통과하는가 — 검증용."""
    return (len(number) > 1 and number.isdigit()
            and luhn_check_digit(number[:-1]) == number[-1])


def synth_imsi(plmn7: str, index: int) -> str:
    """`imsi_mcc_mnc_info`(앞 7자) + MSIN 8자리 = 15자리.

    접두를 **발명하지 않는다.** 호출부가 그 행의 `imsi_mcc_mnc_info` 를 넘기고,
    그래서 규격의 "first 7 characters of imsi" 가 구성상 반드시 성립한다.
    """
    p = str(plmn7 or "").strip()
    if len(p) != 7 or not p.isdigit():
        raise ValueError(f"imsi_mcc_mnc_info 가 숫자 7자가 아니다: {plmn7!r}")
    return p + _scatter(index, 8)


def synth_mdn(index: int) -> str:
    """한국 이동전화 번호 — `010` + 가입자번호 8자리 = 11자리.

    011·016·017·018·019 는 2004년 번호통합으로 신규 부여가 끝났다. 지금
    데이터에 그것이 섞여 있으면 그 자체가 틀린 사실이므로 `010` 만 쓴다.

    가입자번호 첫 자리를 `2`~`9` 로 고정한다. `010-0xxx`·`010-1xxx` 는
    부여되지 않는 대역이라, 흩뿌린 8자리를 그대로 쓰면 `010-0000-9091` 같은
    **있을 수 없는 번호**가 나온다. 앞 1자리를 인덱스 상위에서, 뒤 7자리를
    아핀 사상에서 가져오므로 단사는 유지된다.
    """
    lead = 2 + (index // 10 ** 7) % 8
    return f"010{lead}" + _scatter(index % 10 ** 7, 7, _AFFINE_MDN)


def tac_for(vendor: str, model: str) -> str:
    """`Vendor`·`Model` → TAC 8자리. 같은 기종이면 항상 같은 값이다.

    앞 2자리는 Reporting Body Identifier 다. `35` = BABT 로, 실제 휴대폰
    IMEI 에서 가장 흔하다. 나머지 6자리는 기종 해시 — 기종이 다르면 TAC 도
    다르다(해시 충돌 가능성은 있지만, TAC 는 분석의 **보조 축**이고 기종 축의
    정본은 `Vendor`·`Model` 컬럼이다. 식별자의 단사성과는 무관하다).
    """
    h = hashlib.sha256(f"{vendor}/{model}".encode()).hexdigest()
    return "35" + f"{int(h[:12], 16) % 1_000_000:06d}"


def synth_imei(vendor: str, model: str, index: int) -> str:
    """TAC(8) + SNR(6) + Luhn 체크디지트(1) = 15자리.

    SNR 을 **전역 인덱스**에서 뽑는다. 같은 기종이면 TAC 가 같으므로, SNR 이
    기종 안에서만 유일하면 TAC 가 겹치는 순간 IMEI 가 충돌한다. 전역 인덱스로
    뽑으면 `_scatter` 의 단사성이 IMEI 전체의 유일성을 그대로 보장한다.
    """
    body = tac_for(vendor, model) + _scatter(index, 6)
    return body + luhn_check_digit(body)


def synth_service_code(index: int) -> str:
    """`ServiceCode` — `SVC` + 7자리 = 10자리.

    **구조는 우리가 정한 것이다.** 규격서는 `string / VARCHAR2 / 10` 과
    "ServiceCode" 라고만 적고 체계를 정의하지 않으며, `pemdb1` 에 대응 코드
    테이블도 없다(`CD_SERVER_CALLTYPE` 은 `CALL_TYPE` 용이다). `IMSI`·`IMEI` 와
    달리 **맞출 표준이 없으므로 흉내낼 대상도 없다** — 그래서 한눈에 합성값임을
    알 수 있는 `SVC` 접두를 붙인다.

    `spid`(141행, `uint`/`NUMBER(3)`, "Subscriber Profile ID for RFP")는 별도
    컬럼이고 평문이다. 둘을 섞지 않는다.
    """
    return "SVC" + _scatter(index, 7)
