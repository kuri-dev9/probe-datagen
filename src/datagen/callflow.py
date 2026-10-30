"""LTE 절차(call flow) 명세에서 **XDR 레코드 데이터셋**을 만든다.

## 무엇이고 무엇이 아닌가

**런타임 생성 경로가 이 모듈을 부르지 않는다.** 분 단위 생성은 지금처럼
`pemdb1._coerce` 가 실측 분포로 돌리고, 여기서 만드는 것은 **미리 깔아 두는
데이터셋**이다. 둘을 섞으면 어느 쪽이 만든 레코드인지 알 수 없게 된다.

쓰임은 둘이다 —

* **데이터셋 생성** — call_type 별 정상 호, 장애 종류별 실패 호를 각각
  파일로 만든다. 154컬럼이 다 채워진 레코드라 그대로 질의·집계할 수 있다.
* **검증** — 만든 데이터셋이 명세를 지키는지 되읽어 본다. 단계 시각 순서,
  단계 합과 호 길이, First/Last Error 의 짝, 플래그와 수치의 일치.

명세는 `config/callflow.yaml` 이고 **그쪽이 정본이다.** 단계 순서·장애
지점·Cause 를 고치려면 그 파일을 고친다.

## 왜 명세를 따로 두나

생성 경로에는 call flow 규칙이 이미 **코드로** 들어 있다 —
`scenarios._fail_connect`(First/Last Error 의 짝) · `_nas_reject`(하류 NAS
거절) · `_timeout_core_duration`(대기시간이 지연에 반영된다). 맞게 돌고
있지만 **읽어 낼 수가 없다.** 어느 장애가 어느 구간에서 끊기는지 알려면
25개 mutator 를 다 읽어야 하고, 빠진 것(S13 단말식별·S6a 가입정보 거절)이
있어도 드러나지 않는다.

**2026-10-28 부터 장애 주입도 이 명세를 탄다**(`inject` · `drop`). 위 세
도우미는 그때 지웠다 — 오류 칸만 적고 정답지의 시간·해제·직전 호 서명을
적지 않았다.

명세는 그것을 **데이터로 꺼낸 것**이다. 실측으로 확인한 공백 —

    UpdateLocation_Cause 장애 주입              없음 (평시 배경만)

`first_error_interface_protocol = 8`(S13)도 생성 데이터에 0건이라 공백으로
봤는데 **공백이 아니었다.** 정답지 실패 141호에도 0건이고, S13 오류가 실제로
난 1호는 `first_error` 를 EMM 에 준다 — 프로브가 그 구간을 Call Error
후보로 보지 않는다(`CALL_ERROR_EXCLUDED_IFACE`). EIR 장애는 **구간
Cause 로만** 재현한다.

## 단계 전개 규칙

1. **일어나지 않은 단계는 수치가 `0` 이다.** `flags()` 가 `pg_att=0` 이면
   페이징 시간·건수가 없다.
2. **시각은 단계 순서를 따른다.** 누적이라 역전이 불가능하다.
3. **단계 합이 호 길이를 넘지 않는다.**
4. **메시지 수는 단계에서 나온다** — 시간이 흘렀는데 메시지가 0 일 수 없다.
5. **끊긴 단계가 First Error 다.** 그 뒤 단계는 일어나지 않는다.
6. **하류 NAS 거절이 Last Error 다**(불변규칙 7). 없으면 Last = First.
7. **TIMEOUT 이면 구간 Cause 를 비운다**(규칙 D-2) — 응답이 없었으므로
   Result-Code 를 적을 자리가 없다. `first_error_cause` 에는 `900` 이 간다.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field, replace
from pathlib import Path

import yaml

#: 마이크로초. XDR `*_duration` 의 단위다(규격 `unit : microsecond`).
US = 1_000_000.0

#: phase 이름 — `initial_{phase}_duration` · `_msg_count` 로 접힌다.
#:
#: **`release` 가 여기 없다.** 해제는 접속 절차가 아니고 세 카운터는 "접속에
#: 걸린 시간" 이다. 실측에서 접속 절차는 호의 앞 **26%**(중앙값)에서 끝나고
#: 나머지는 데이터 구간이다 — 호 종료를 더하면 접속 지연이 호 길이만큼
#: 부풀어 오른다.
PHASES = ("paging", "access", "core")

#: 호를 닫는 단계의 phase. duration 카운터가 없다.
RELEASE_PHASE = "release"

#: `flags()` 키 접두 → XDR 플래그 필드 접두. `_baseline_procedures` 와 같은 표다.
GATE_FLAG = {"auth": "auth", "loc": "location", "mec": "mecheck", "pg": "paging"}

#: TIMEOUT Cause. 통합 규격이 `first_error_cause` 에 이것을 포함한다(§3.2.7).
TIMEOUT_CAUSE = 900

#: **TIMEOUT 을 Call Error 로 세지 않는 구간** — S6a(1) · S13(8).
#:
#: 기능정의서 §3.2.6 의 구간별 표가 그 둘만 "TIMEOUT **제외**" 로 적고
#: 나머지(S1AP·EMM·ESM·S11·S10)는 "포함" 이다. `codes.is_interface_error`
#: 가 같은 판정을 한다 — `is_diameter_error(900)` 은 거짓이다.
#:
#: **정답지가 그 결과를 보여 준다.** `AuthenticationInformation_Cause = 900`
#: 인 3호의 `first_error` 가 전부 `5/17`(EMM Network failure)다 — S6a
#: TIMEOUT 이 First 자리를 받지 못하고 **하류 NAS 거절이 그 자리에 온다.**
TIMEOUT_EXCLUDED_IFACE = {1, 8}

#: **TIMEOUT 이 아니어도 First Error 가 되지 못하는 구간.**
#:
#: S13 은 거절을 받아도 `first_error_interface_protocol` 에 올라오지 않는다.
#: 정답지 실패 141호에 `iface = 8` 이 **한 건도 없고**, S13 오류가 실제로
#: 난 1호는 이렇게 적혀 있다 —
#:
#:     s13_error_Cause       = 5012      ← 구간에는 적는다
#:     MEIdentityCheck_Cause = 5012
#:     first_error           = (5, 21)   ← **EMM 이 받는다**
#:     last_error            = (2, 301)
#:
#: 그래서 "S13 공백을 메운다" 는 전제가 틀렸다 — 공백이 아니라 프로브가
#: Call Error 후보로 보지 않는 구간이다. 여기 `8` 을 빼면 정답지에 없는
#: `iface = 8` 이 생성되고, `H-17`(미정의 조합)이 그것을 잡지 못한다.
CALL_ERROR_EXCLUDED_IFACE = {8}

#: **`first_error_interface_protocol` 에 쓰면 안 되는 값.** 통합 규격이 S3 를
#: Call Error 에 반영하지 않는다(기능정의서 §3.2.6) — 3G 연동 실패는
#: `s3_error_*` 로만 남고 First Error 로 올라오지 않는다.
FORBIDDEN_IFACE = {7}


@dataclass(frozen=True)
class Step:
    """절차 한 단계. `config/callflow.yaml` 의 `steps:` 한 항목이다."""

    name: str
    phase: str
    iface: int
    msgs: int
    weight: float
    gate: str = ""            #: flags() 키 접두. "" 면 무조건 일어난다
    time_field: str = ""      #: XDR 시각 필드. "" 면 적을 자리가 없다
    cause_field: str = ""     #: 절차 Cause 필드
    error_prefix: str = ""    #: `{prefix}_error_*` 구간 필드
    error_message: int = 0
    #: First Error 에 적는 메시지. `0` 이면 `error_message` 와 같다.
    #: S11 은 둘이 다르다(First `32` 요청 · 구간 `33` 응답).
    first_message: int = 0
    #: 이 단계에서 끊긴 호의 **실측 서명** — 시간·메시지·식별자·직전 호.
    #: 키: `core_us [lo, hi]` · `core_msgs` · `response_gap [lo, hi]`(초) ·
    #: `clear [필드]` · `old_call_same`(비율) · `end_at_response`.
    on_fail: dict = field(default_factory=dict)
    #: **성공해도 적는 Cause.** 실측에서 `2001 DIAMETER_SUCCESS` 가
    #: `MEIdentityCheck` 98.5% · `UpdateLocation` 86% · `Authentication`
    #: 73.5% 다. 비우면 "절차는 있었는데 결과를 모른다" 가 되어 실패와
    #: 구분되지 않는다(불변규칙 17).
    success_cause: int = 0
    #: 평시 실패율 — **장애 주입과 다르다.** 정답지에도 평시 실패가 섞여
    #: 있고, 그것이 `P-01`·`P-02` 의 분자를 만든다.
    fail_rate: float = 0.0
    fail_causes: dict = field(default_factory=dict)

    def draw_fail(self, rng: random.Random) -> int:
        ks = list(self.fail_causes)
        if not ks:
            return 0
        return rng.choices(ks, weights=[self.fail_causes[k] for k in ks], k=1)[0]


@dataclass(frozen=True)
class Fault:
    """장애 하나 — 어느 단계에서 끊기고 하류에 무엇이 남나."""

    name: str
    title: str
    break_at: str
    causes: dict[int, float]
    timeout: bool = False
    downstream: dict | None = None
    #: **하류 거절이 아예 없다.** `downstream` 이 `None` 인 것과 다르다 —
    #: 그쪽은 `downstream_map` 에 맡긴다는 뜻이고, 이쪽은 **망이 거절을
    #: 내지 않았다** 는 사실이다(`last_error` 가 비워진다).
    no_downstream: bool = False
    #: **First Error 구간 덮어쓰기.** 끊기는 *단계*와 거절을 적는 *구간*이
    #: 다를 때 쓴다 — 세션 단계에서 MME 가 PDN 연결을 ESM 으로 거절하면
    #: 단계는 `session`(S11)인데 First 는 `6/esm` 이다. 정답지의 `iface=6`
    #: 20호가 `s11_error_Cause` 없이 ESM 만 들고 있다. `0` 이면 구간이
    #: 없다(`902 NO_CAUSE_PAGING` — 적을 메시지가 없다).
    iface: int | None = None
    prefix: str | None = None
    #: 그 장애가 성립하는 call_type. 비우면 단계열이 정한다(`call_types_for`).
    #: 정답지가 `(5,9)·(5,10)·(5,40)` 을 **TAU 에서만** 보여 주듯 같은
    #: 단계(`rrc`)라도 절차마다 거절 사유가 다르다.
    call_types: tuple[int, ...] = ()
    #: Cause 마다 다른 하류. **적혀 있으면 전역 `downstream_map` 으로
    #: 떨어지지 않는다** — 없는 Cause 는 하류가 없는 것이다(Last = First).
    downstream_by_cause: dict = field(default_factory=dict)
    #: 하류가 붙는 비율. 정답지 `(2,404)` 4호 중 2호만 `EMM 98` 을 남긴다.
    downstream_rate: float = 1.0
    #: 해제 분포 — **`fail_release` 보다 이긴다.** 같은 단계라도 장애마다
    #: 닫히는 자리가 다르다(`(2,103)` 은 `103`, 세션 거절은 `302`).
    release: dict | None = None
    #: **eNB 가 요청한 해제다** — `UEContextReleaseRequest_Cause` 에 같은
    #: 값이 실린다. 정답지 `(2,103)` 4호가 `rreq = rel = 103` 이다.
    release_request: bool = False
    #: 해제 사유 = 뽑힌 Cause. 무선 실패(`125`·`126`)는 eNB 가 그 사유로
    #: 해제를 요청한다(정답지 `rreq = rel = 126`).
    release_from_cause: bool = False
    #: Cause 마다 다른 해제 — `release` 보다 이긴다. 정답지에서 `302` 로
    #: 닫힌 인증 거절은 **`5012` 한 건뿐**이다. 전체에 7% 를 뿌리면 정답지에
    #: 없는 `15001 → 302` 같은 호가 생긴다.
    release_by_cause: dict = field(default_factory=dict)
    #: call_type 마다 다른 해제 — `{ct: {causes: {...}, request: bool}}`.
    #: Attach 실패는 MME 가 `302` 로 닫고, 서비스 호 실패는 **eNB 가 `120`
    #: 으로 해제를 요청한다**(실측 `rreq = rel`).
    release_by_call_type: dict = field(default_factory=dict)
    #: `release_request` 를 이 call_type 에만 건다. 비우면 전부.
    release_request_call_types: tuple[int, ...] = ()
    #: 메시지 덮어쓰기 — 구간을 덮어쓴 장애는 단계의 메시지가 맞지 않는다
    #: (ESM 거절에 `33 CREATE_SESSION_RESPONSE` 가 실린다). First·구간에 함께.
    message: int | None = None
    message_by_cause: dict = field(default_factory=dict)
    #: 끊긴 단계의 `on_fail` 서명을 **이 장애에서만** 덮는다(키 단위 병합).
    #: 같은 세션 단계라도 S11 거절(GW 응답 대기·재전송)과 ESM 거절(MME 가
    #: 바로 거절)은 시간 모양이 다르다.
    on_fail: dict = field(default_factory=dict)
    #: **First 에만 적는 Cause.** 구간·하류·해제는 뽑힌 Cause(응답)를 따른다.
    #: 정답지 S11 `900` 10호가 그렇다 — First 는 `(3, 900, msg 32)` 인데
    #: `s11_error_Cause` 에는 응답 Cause(`72`·`92`·`93`·`68`, msg 33)가 있고
    #: 하류도 그것을 따른다. 첫 요청이 시간 초과로 재전송되고 결국 거절
    #: 응답을 받은 꼴이다.
    first_cause: int | None = None
    #: **런타임 주입에서만 넓히는 절차** — 비우면 `call_types` 와 같다.
    #: 정답지 대조 데이터셋(`build`)은 `call_types` 를 따르고, UI 주입
    #: (`inject`)은 이것을 따른다. 정답지의 S6a 장애는 전부 Attach 인데
    #: 생성기 평시 호는 TAU·서비스 호에도 인증을 붙여, Attach 에만 실패를
    #: 넣으면 인증 성공률이 1~2%p 밖에 안 움직인다(사용자 결정 2026-10-28:
    #: *"케이스가 적어도 시뮬레이션이니까 시나리오대로 나와야지"*).
    inject_call_types: tuple[int, ...] = ()
    source: str = ""

    def draw_cause(self, rng: random.Random) -> int:
        ks = list(self.causes)
        return rng.choices(ks, weights=[self.causes[k] for k in ks], k=1)[0]


@dataclass
class Release:
    """호를 닫는 규칙 — `config/callflow.yaml` 의 `release:` 절.

    **`UEContextReleaseRequest_Cause` 가 있으면 그대로
    `UEContextRelease_Cause` 에 실린다.** 실측 204호가 그 둘을 정확히
    맞춰 준다(명세 주석). 없으면 MME 가 주도한 해제이고 Cause 가 다르다.
    """

    request_share: dict[int, float]
    request_causes: dict[int, float]
    mme_causes: dict[int, float]

    @staticmethod
    def _draw(d: dict[int, float], rng: random.Random) -> int:
        ks = list(d)
        return rng.choices(ks, weights=[d[k] for k in ks], k=1)[0]

    def draw(self, call_type: int, rng: random.Random) -> tuple[int, int]:
        """`(release_request_cause, release_cause)`. 요청이 없으면 앞이 `0`."""
        if rng.random() < self.request_share.get(call_type, 0.0):
            c = self._draw(self.request_causes, rng)
            return c, c
        return 0, self._draw(self.mme_causes, rng)


@dataclass(frozen=True)
class Flow:
    """한 `call_type` 의 단계열과 **단계별 발생률**."""

    name: str
    steps: tuple[str, ...]
    #: 단계 이름 → 발생 확률. **XDR 샘플에서 센 것**이고 `flags()` 를 이긴다
    #: (사용자 확정 2026-10-28: *"xdr 샘플이 실제 정답지"*).
    share: dict[str, float] = field(default_factory=dict)


@dataclass
class Spec:
    steps: dict[str, Step]
    flows: dict[int, Flow]
    faults: dict[str, Fault]
    release: Release
    #: 절차 Cause → 하류 NAS 거절. **의미가 이어진다**(명세 주석의 대응표).
    downstream_map: dict[int, dict]
    #: 단계 이름 → 그 단계가 실패했을 때의 `UEContextRelease_Cause` 분포.
    fail_release: dict[str, dict[int, float]]
    #: 〃 **타임아웃일 때.** 거절과 무응답이 같은 자리로 닫히지 않는다 —
    #: 답이 없으면 MME 가 보낼 Authentication Failure 도 없다.
    fail_release_timeout: dict[str, dict[int, float]]
    #: 직전 호 기록 — `{fail_share, same_iface}`. **실패 호에만 쓴다.**
    old_call: dict[str, float]
    #: 연결 후 절단의 모양 — `drop()` 이 읽는다.
    drop: dict = field(default_factory=dict)

    def downstream_for(self, cause: int, fault: Fault | None) -> dict | None:
        """그 Cause 의 하류. 장애가 따로 적었으면 그쪽이 이긴다."""
        if fault is not None and fault.downstream_by_cause:
            return fault.downstream_by_cause.get(cause)
        if fault is not None and fault.downstream:
            return fault.downstream
        return self.downstream_map.get(cause)

    def flow_steps(self, call_type: int) -> tuple[Step, ...]:
        return tuple(self.steps[n] for n in self.flows[call_type].steps)

    def call_types_for(self, fault: Fault) -> tuple[int, ...]:
        """그 장애가 **성립하는** call_type — 끊길 단계가 단계열에 있어야 한다.

        없는 단계에서 끊을 수는 없다. Detach 호에 "세션 생성 실패" 를
        적으면 그 호가 무엇인지 아무도 모른다.

        **발생률이 `0` 인 단계도 뺀다** — `Service MO` 의 `auth` 가 그렇다.
        단계열에는 있지만 실측 49호에서 한 번도 일어나지 않았고, 거기서
        끊으면 정답지에 없는 호가 된다.
        """
        out = []
        for ct, fl in self.flows.items():
            if fault.break_at not in fl.steps:
                continue
            st = self.steps[fault.break_at]
            if st.gate and fl.share.get(fault.break_at, 1.0) <= 0.0:
                continue
            if fault.call_types and ct not in fault.call_types:
                continue
            out.append(ct)
        return tuple(out)


DEFAULT_SPEC = Path(__file__).resolve().parents[2] / "config" / "callflow.yaml"


def load_spec(path: Path | str | None = None) -> Spec:
    p = Path(path) if path else DEFAULT_SPEC
    raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    steps = {n: Step(name=n, **v) for n, v in (raw.get("steps") or {}).items()}
    flows = {}
    for ct, v in (raw.get("flows") or {}).items():
        names = tuple(v["steps"])
        bad = [n for n in names if n not in steps]
        if bad:
            raise ValueError(f"flows[{ct}] 가 모르는 단계를 쓴다: {bad}")
        share = {k: float(x) for k, x in (v.get("share") or {}).items()}
        bad = [k for k in share if k not in names]
        if bad:
            raise ValueError(f"flows[{ct}].share 가 단계열에 없는 것을 적는다: {bad}")
        flows[int(ct)] = Flow(name=v.get("name", str(ct)), steps=names,
                              share=share)
    faults = {}
    for n, v in (raw.get("faults") or {}).items():
        if v["break_at"] not in steps:
            raise ValueError(f"faults[{n}].break_at 이 모르는 단계다: {v['break_at']}")
        faults[n] = Fault(name=n, title=v.get("title", n),
                          break_at=v["break_at"],
                          causes={int(k): float(w) for k, w in v["causes"].items()},
                          timeout=bool(v.get("timeout")),
                          downstream=v.get("downstream"),
                          no_downstream=bool(v.get("no_downstream")),
                          iface=v.get("iface"),
                          prefix=v.get("prefix"),
                          call_types=tuple(int(c) for c in v.get("call_types") or ()),
                          downstream_by_cause={int(k): d for k, d in
                                               (v.get("downstream_by_cause") or {}).items()},
                          downstream_rate=float(v.get("downstream_rate", 1.0)),
                          release={int(k): float(w) for k, w in v["release"].items()}
                                  if v.get("release") else None,
                          release_request=bool(v.get("release_request")),
                          release_from_cause=bool(v.get("release_from_cause")),
                          message=v.get("message"),
                          on_fail=dict(v.get("on_fail") or {}),
                          first_cause=(int(v["first_cause"])
                                       if v.get("first_cause") is not None else None),
                          inject_call_types=tuple(
                              int(c) for c in v.get("inject_call_types") or ()),
                          message_by_cause={int(k): int(m) for k, m in
                                            (v.get("message_by_cause") or {}).items()},
                          release_request_call_types=tuple(
                              int(x) for x in v.get("release_request_call_types") or ()),
                          release_by_call_type={
                              int(k): {"causes": {int(c): float(x) for c, x in d["causes"].items()},
                                       "request": bool(d.get("request"))}
                              for k, d in (v.get("release_by_call_type") or {}).items()},
                          release_by_cause={int(k): {int(c): float(x) for c, x in d.items()}
                                            for k, d in (v.get("release_by_cause") or {}).items()},
                          source=v.get("source", ""))
    # 규격이 금지한 인터페이스가 명세에 섞이면 **생성 전에** 멈춘다.
    for s in steps.values():
        if s.iface in FORBIDDEN_IFACE:
            raise ValueError(
                f"steps[{s.name}].iface = {s.iface} — 통합 규격이 그 구간을 "
                f"Call Error 에 반영하지 않는다(기능정의서 §3.2.6)")
    r = raw.get("release") or {}
    rel = Release(
        request_share={int(k): float(v) for k, v in (r.get("request_share") or {}).items()},
        request_causes={int(k): float(v) for k, v in (r.get("request_causes") or {}).items()},
        mme_causes={int(k): float(v) for k, v in (r.get("mme_causes") or {}).items()},
    )
    dmap = {int(k): v for k, v in (raw.get("downstream_map") or {}).items()}
    def _rel(key):
        return {k: {int(c): float(w) for c, w in v.items()}
                for k, v in (raw.get(key) or {}).items()}
    oc = {k: float(v) for k, v in (raw.get("old_call") or {}).items()}
    return Spec(steps=steps, flows=flows, faults=faults, release=rel,
                downstream_map=dmap, fail_release=_rel("fail_release"),
                fail_release_timeout=_rel("fail_release_timeout"), old_call=oc,
                drop=dict(raw.get("drop") or {}))


# ── 단계 전개 ──────────────────────────────────────────────────────────────


@dataclass
class Walk:
    """한 호가 실제로 밟은 단계들과 그 결과."""

    call_type: int
    steps: list[Step] = field(default_factory=list)
    #: 단계 이름 → 시작 시각(epoch)
    times: dict[str, float] = field(default_factory=dict)
    durations: dict[str, float] = field(default_factory=dict)
    msgs: dict[str, int] = field(default_factory=dict)
    broken_at: Step | None = None
    cause: int = 0
    timeout: bool = False
    #: 절차가 깨진 호의 `UEContextRelease_Cause`. **`Rec` 에 실어 나르지
    #: 않는다** — `Rec.set` 은 모르는 필드명을 **조용히 무시하므로**
    #: 임시 키를 쓰면 값이 사라지고 에러도 없다(이 모듈이 `verify_fields`
    #: 로 막는 바로 그 함정이다).
    release_cause: int = 0
    #: eNB 가 요청한 해제인가 — `_close_call` 이 `Request_Cause` 를 채운다.
    release_request: bool = False
    #: 끊은 장애. `apply` 가 구간 덮어쓰기(`Fault.iface`)를 읽는다.
    fault: "Fault | None" = None
    #: 끊긴 구간의 **응답 시각**(`on_fail.response_gap`). 하류·해제·호 끝이
    #: 여기에 맞춰진다. `0` 이면 응답 간격을 두지 않는 단계다.
    resp_time: float = 0.0


def _cut(steps: list[Step], at: int) -> list[Step]:
    """``at`` 번째에서 끊되 **호를 닫는 단계는 남긴다.**

    실패한 호도 해제된다 — 정답지에서 인증이 깨진 18호 전부
    `UEContextRelease_Time` 이 있다. 잘라 버리면 **열리기만 하고 닫히지
    않는 호**가 되고, `release` 가 곧 `call_end` 이므로 호가 언제
    끝났는지도 사라진다.

    `release_request` 는 뺀다 — eNB 가 요청한 해제가 아니라 **MME 가
    절차 실패로 닫는 것**이다(정답지: 인증 실패 호의 78% 가 S1AP
    `301 Authentication Failure`).
    """
    return steps[:at + 1] + [s for s in steps[at + 1:]
                             if s.phase == RELEASE_PHASE
                             and s.name != "release_request"]


def active_steps(spec: Spec, call_type: int, flags: dict[str, int],
                 rng: random.Random | None = None) -> list[Step]:
    """이 호가 실제로 밟는 단계열.

    ## `share` 가 `flags()` 를 이긴다

    `PemdbDist.flags()` 는 상용 **1분 집계**에서 온 `proc_ratio` 를 쓰고,
    `Flow.share` 는 **XDR 레코드 자체**(정답지)에서 센 것이다. 둘이 다르다 —
    `flags()` 로는 Service MO 에 인증이 2% 붙는데 샘플 49호에는 0건이다.
    데이터셋은 정답지를 따른다(사용자 확정 2026-10-28).

    ## 단계끼리 **독립으로** 추첨한다

    Attach MO 103호의 `auth`×`mecheck` 교차가 거의 곱에 맞는다 —
    실측 39.8 / 24.3 / 26.2 / 9.7 에 독립 기대값 42.2 / 21.8 / 23.8 / 12.2.
    **인증 없이 IMEI 체크만 하는 호가 26%** 라 `mecheck` 를 `auth` 뒤에
    묶으면 그만큼이 통째로 사라진다.

    ``rng`` 가 없으면 추첨하지 않고 `flags()` 를 쓴다 — 그 경로는
    `share` 를 적지 않은 call_type(Detach·Handover·Reroute)에서 돈다.
    """
    fl = spec.flows[call_type]
    out = []
    for st in spec.flow_steps(call_type):
        if not st.gate:
            out.append(st)
            continue
        if rng is not None and st.name in fl.share:
            if rng.random() < fl.share[st.name]:
                out.append(st)
            continue
        if flags.get(f"{st.gate}_att", 0):
            out.append(st)
    return out


def walk(spec: Spec, call_type: int, flags: dict[str, int], rng: random.Random,
         *, t0: float, call_dur_us: float,
         phase_totals: dict[str, float],
         fault: Fault | None = None, use_flags: bool = False) -> Walk:
    """단계를 전개한다. ``fault`` 가 있으면 그 단계에서 **끊는다**.

    ``use_flags`` 면 단계 발생을 정답지 비율(`share`)로 다시 뽑지 않고
    **그 레코드의 시도 플래그**를 따른다 — 런타임 주입(`inject`)의 경로다.

    ``phase_totals`` 는 phase 별 총 소요시간(µs)이고 **호출부가 준다** —
    실측 백분위에서 뽑은 값을 배분만 하려는 것이다. 주변분포가 바뀌면
    `pemdb1` 대조 통계가 어긋난다.
    """
    w = Walk(call_type=call_type)
    steps = active_steps(spec, call_type, flags, None if use_flags else rng)
    #: 끊기기 **전** 전체 단계열. phase 총량을 비례 배분할 때 분모가 된다.
    all_steps = list(steps)

    if fault is not None:
        names = [s.name for s in steps]
        if fault.break_at not in names:
            # 그 단계를 밟지 않는 호다. 끊을 자리가 없으므로 **정상 호로
            # 둔다** — 없는 단계에서 실패했다고 적으면 그 호가 무엇인지
            # 알 수 없게 된다.
            fault = None
        else:
            cut = names.index(fault.break_at)
            # **`steps[-1]` 이 아니다.** `_cut` 이 해제 단계를 남기므로
            # 마지막 원소는 `release` 다 — 끊긴 자리를 이름으로 잡는다.
            w.broken_at = steps[cut]
            steps = _cut(steps, cut)
            w.cause = fault.draw_cause(rng)
            w.timeout = fault.timeout
            w.fault = fault

    # **평시 실패도 뒤 단계를 끊는다.** 정답지에서 인증이 실패한 18호는
    # `mecheck` 0 · `ulr` 0 이다 — MME 는 인증이 깨지면 ULR 을 보내지
    # 않는다. 장애 주입이든 평시 실패든 call flow 는 거기서 멈춘다.
    #
    # 장애가 이미 끊었으면 그 앞에서만 본다 — 두 번 끊을 수 없다.
    if w.broken_at is None:
        for i, st in enumerate(steps):
            if st.fail_rate <= 0 or rng.random() >= st.fail_rate:
                continue
            w.broken_at = st
            w.cause = st.draw_fail(rng)
            w.timeout = w.cause == TIMEOUT_CAUSE
            steps = _cut(steps, i)
            break

    w.steps = steps
    used = {p: sum(s.weight for s in steps if s.phase == p) for p in PHASES}

    # **끊긴 호는 그 phase 를 끝까지 밟지 않았다.** 살아남은 단계의 weight
    # 비율만큼만 쓴다 — 그러지 않으면 인증에서 거절당한 호의
    # `initial_access_duration` 이 끝까지 간 호와 같아진다. `phase_totals`
    # 는 정상 호의 실측 분포이므로 **전체를 밟았을 때의 값**이다.
    full = {p: sum(s.weight for s in all_steps if s.phase == p) for p in PHASES}
    # `release` phase 는 `PHASES` 에 없으므로 `totals` 에도 없다 — 아래
    # 루프가 `.get(…, 0.0)` 으로 받아 소요시간 0 이 된다. 시각은 호를 닫을
    # 때 `call_end` 로 덮인다(`_close_call`).
    # **실패 호의 실측값을 받은 phase 는 줄이지 않는다** (2026-10-28).
    # `on_fail.core_us`·`access_us` 는 끊긴 호에서 잰 길이라 이미 "끝까지
    # 밟지 않은" 값이다. 아래 두 축소를 또 받으면 서비스 호 S11 실패의
    # core 가 155µs 까지 내려갔다(실측 최소 30ms). 호 길이는 `_close_call`
    # 이 단계 뒤로 늘린다.
    fixed: set[str] = set()
    if fault is not None:
        of = on_fail_of(spec.steps[fault.break_at], fault, call_type)
        fixed = {p for p in PHASES if of.get(f"{p}_us")}
    totals = {}
    for p in PHASES:
        if used[p] <= 0:
            totals[p] = 0.0          # 일어나지 않은 phase 는 **0 이다**
        elif p in fixed or full[p] <= 0:
            totals[p] = phase_totals.get(p, 0.0)
        else:
            totals[p] = phase_totals.get(p, 0.0) * (used[p] / full[p])
    # 단계 합이 호 길이를 넘지 않는다. 한 phase 만 깎으면 그쪽 분포가 치우치므로
    # 비례 축소한다. **실측값을 받은 호는 줄이지 않는다** — 호 길이를 그
    # 실측에 맞춰 `_close_call` 이 늘린다. 남은 몫으로 다른 phase 를 깎으면
    # 페이징이 0µs 가 되어 플래그와 시간이 어긋났다(`verify` 페이징 불일치).
    if call_dur_us > 0 and not fixed:
        tot = sum(totals.values())
        if tot > call_dur_us:
            k = call_dur_us / tot
            totals = {p: v * k for p, v in totals.items()}

    # **단계열 순서로 돈다.** `phase` 는 어느 카운터
    # (`initial_{phase}_duration`)에 더할지를 정할 뿐 **순서가 아니다.**
    #
    # 구 구현은 phase 로 묶어 돌렸는데, `Attach MO` 의 단계열이
    # `rrc → auth → mecheck → ulr → session → ctxsetup` 인데 `rrc` 와
    # `ctxsetup` 이 둘 다 `access` 라 **붙어 버렸다** — 코어 절차보다
    # Initial Context Setup 이 먼저 일어난 꼴이다. `ctxsetup` 에는 시각
    # 필드가 없어서(통합 규격이 지웠다) 시각 역전 검사에 걸리지도 않았고,
    # **"단계 합 > 호 길이" 로 간접적으로만** 드러났다.
    t = t0
    for s in steps:
        w.times[s.name] = t
        d = (totals[s.phase] * (s.weight / used[s.phase])
             if used.get(s.phase, 0) > 0 else 0.0)
        w.durations[s.name] = d
        t += d / US
        if s.phase in PHASES:
            w.msgs[s.phase] = w.msgs.get(s.phase, 0) + s.msgs
    for p in PHASES:
        w.durations[f"phase:{p}"] = totals[p]
    return w


# ── 레코드로 접기 ──────────────────────────────────────────────────────────


def apply(rec, spec: Spec, w: Walk, flags: dict[str, int],
          rng: random.Random) -> None:
    """`Walk` 를 XDR 레코드 필드로 적는다.

    ``rec`` 은 `scenarios.Rec` 이다 — **모르는 필드명은 조용히 무시되므로**
    (`Rec.set`) 여기 쓰는 이름은 `verify_fields()` 가 따로 센다.
    """
    for p in PHASES:
        d = w.durations.get(f"phase:{p}", 0.0)
        rec.set(f"initial_{p}_duration", int(d))
        m = w.msgs.get(p, 0)
        # 메시지 수는 단계에서 나온다. 재전송으로 조금 늘 수 있으므로 위로만 흔든다.
        rec.set(f"initial_{p}_msg_count", (m + rng.randint(0, 2)) if m else 0)

    # ── 단계가 남기는 것 ─────────────────────────────────────────────
    #
    # **플래그를 단계 추첨 결과에 맞춘다.** `share` 가 `flags()` 를 이기므로
    # (`active_steps`) 그대로 두면 "플래그는 섰는데 단계가 없는" 레코드가
    # 생긴다 — 이 모듈이 고치려는 바로 그 갈림이다.
    ran = {st.name for st in w.steps}
    for st in spec.flow_steps(w.call_type):
        if not st.gate:
            continue
        on = st.name in ran
        rec.set(f"{GATE_FLAG[st.gate]}_attempt_flag", 1 if on else 0)
        if not on:
            rec.set(f"{GATE_FLAG[st.gate]}_success_flag", 0)

    for s in w.steps:
        if s.time_field:
            rec.set(s.time_field, f"{w.times[s.name]:.6f}")
        # **성공해도 Cause 를 적는다.** 실측에서 `2001 DIAMETER_SUCCESS` 가
        # `MEIdentityCheck` 98.5% · `UpdateLocation` 86% · `Authentication`
        # 73.5% 다. 비우면 "절차는 있었는데 결과를 모른다" 가 되어 실패와
        # 구분되지 않는다(불변규칙 17).
        # 끊긴 단계의 Cause 는 아래 ①~③ 이 적는다. 그 앞 단계들은
        # **성공했으므로** `2001` 을 적는다.
        if not s.cause_field or s is w.broken_at:
            continue
        rec.set(s.cause_field, s.success_cause or "")
        if s.gate:
            rec.set(f"{GATE_FLAG[s.gate]}_success_flag", 1)

    # 페이징 시도 횟수 — 페이징을 했을 때만. 응답이 없으면 망이 다시 부른다.
    if flags.get("pg_att"):
        rec.set("initial_paging_attempt_count",
                1 if flags.get("pg_suc") else rng.randint(1, 3))
    else:
        rec.set("initial_paging_attempt_count", 0)

    if w.broken_at is None:
        return

    s = w.broken_at
    t = w.times[s.name] + w.durations[s.name] / US

    # ① 끊긴 단계가 First Error 다 — **두 경우만 아니다.**
    #    §3.2.6 이 S6a·S13 의 TIMEOUT 을 Call Error 에서 빼고, S13 은
    #    **거절도** 올라오지 않는다(`CALL_ERROR_EXCLUDED_IFACE`). 그러면
    #    First 자리가 비고 **하류 NAS 거절이 그것을 받는다**
    #    (`set_downstream`).
    rec.set("success_flag", 0)
    over = w.fault is not None and w.fault.iface is not None
    fmsg = None
    if w.fault is not None:
        fmsg = w.fault.message_by_cause.get(w.cause) or w.fault.message
    sec_msg = fmsg or s.error_message
    first_msg = fmsg or s.first_message or s.error_message
    iface = w.fault.iface if over else s.iface
    prefix = (w.fault.prefix or "") if over else s.error_prefix
    if over and iface == 0:
        # **구간이 없는 실패.** `902 NO_CAUSE_PAGING` — 적을 메시지가 없어
        # 구간도 시각도 `0` 이다(정답지 `(0,902)` 4호 전부 `time = 0.0`).
        rec.set("first_error_interface_protocol", 0)
        rec.set("first_error_cause", w.cause)
        rec.set("first_error_message", 0)
        rec.set("first_error_time", "0.0")
    elif (s.iface in CALL_ERROR_EXCLUDED_IFACE
            or (w.timeout and s.iface in TIMEOUT_EXCLUDED_IFACE)):
        rec.set("first_error_interface_protocol", 0)
        rec.set("first_error_cause", 0)
        rec.set("first_error_message", 0)
        rec.set("first_error_time", "")
    else:
        rec.set("first_error_interface_protocol", iface)
        fc = w.fault.first_cause if w.fault is not None else None
        rec.set("first_error_cause", w.cause if fc is None else fc)
        rec.set("first_error_message", first_msg)
        rec.set("first_error_time", f"{t:.6f}")
    # 접속 실패와 연결 후 절단을 한 레코드에 두지 않는다(규칙 C-2).
    rec.set("drop_flag", 0)

    # ② 끊긴 절차는 **실패**이고, 그 **뒤 절차는 일어나지 않았다.**
    #
    # 구 구현은 끊긴 단계의 성공 플래그만 내렸는데, 그러면 인증에서
    # 거절당한 호에 `LOCATION_ATTEMPT_FLAG = 1` 이 남는다 — **인증이
    # 실패했는데 위치갱신을 시도한** 레코드다. 실제 call flow 에서 MME 는
    # 인증이 깨지면 ULR 을 보내지 않는다.
    #
    # 시도 플래그를 내리면 `P-02`(위치갱신 성공률)의 **분모에서도 빠진다** —
    # 그것이 맞다. 시도하지 않은 절차를 분모에 넣으면 성공률이 장애 규모에
    # 따라 흔들린다.
    if s.gate:
        rec.set(f"{GATE_FLAG[s.gate]}_success_flag", 0)
    after = False
    for st in spec.flow_steps(w.call_type):
        if st is s or st.name == s.name:
            after = True
            continue
        if after and st.gate:
            rec.set(f"{GATE_FLAG[st.gate]}_attempt_flag", 0)
            rec.set(f"{GATE_FLAG[st.gate]}_success_flag", 0)

    # ③ **절차 Cause 는 TIMEOUT 이어도 적는다.** 정답지의
    #    `AuthenticationInformation_Cause = 900` 3호가 그렇다 — 규칙 D-2 가
    #    비우라고 한 것은 **구간 Cause**(`s6a_error_Cause`)이고, 그쪽은
    #    Diameter Result-Code 를 담는 자리라 응답이 없으면 적을 값이 없다.
    #    절차 Cause 는 "그 절차가 어떻게 끝났나" 라서 `900` 이 답이다.
    # 구간을 덮어쓴 장애는 **단계의 절차 Cause 를 적지 않는다** — 그
    # 절차가 응답한 게 아니라 다른 구간이 거절한 것이다. 정답지 `(5,6)`
    # 은 `ulr` 단계에서 끊기지만 `UpdateLocation_Cause` 가 비어 있다.
    if s.cause_field and not over:
        rec.set(s.cause_field, w.cause)
    if not w.timeout:
        if prefix:
            rec.set(f"{prefix}_error_Cause", w.cause)
            rec.set(f"{prefix}_error_Message", sec_msg)
            rec.set(f"{prefix}_error_Time", f"{t:.6f}")
    # ── 끊긴 단계의 실측 서명 (`Step.on_fail`) ─────────────────────────
    of = on_fail_of(s, w.fault, w.call_type)
    if of:
        if of.get("core_msgs"):
            rec.set("initial_core_msg_count", int(of["core_msgs"]))
        gap = of.get("response_gap")
        if gap:
            # **First 는 요청, 구간은 응답이다** — 둘 사이가 실측 0.2~0.55초.
            w.resp_time = t + rng.uniform(float(gap[0]), float(gap[1]))
            if prefix and not w.timeout:
                rec.set(f"{prefix}_error_Time", f"{w.resp_time:.6f}")
        if of.get("pgw_from_sgw"):
            # **실측은 `PGW_ID = 0` 이다** — 세션이 안 만들어져 PGW 가 배정되지
            # 않았다(실패 호 전부). 그대로 두면 graphio 노드 축이 `'0'` 을
            # 장비 하나로 세어 "PGW 0번이 실패를 독차지" 하는 Incident 가 선다.
            # **SGW 와 같은 번호로 채운다** — 구 장비 대부분이 SGW·PGW 를 한
            # 노드(SPGW)로 써서 번호가 같았다(사용자 확정 2026-10-28).
            rec.set("PGW_ID", rec.get("SGW_ID"))
    # TIMEOUT 이면 **구간 칸을 아예 적지 않는다.** 전에는 "언제 기다리다
    # 포기했는지는 사실" 이라며 시각·메시지를 남겼는데, 정답지의 S6a
    # TIMEOUT 3호(`AuthenticationInformation_Cause = 900`)와 페이징 TIMEOUT
    # 1호가 **구간 메시지·시각이 전부 비어 있다.** 프로브는 응답이 없는
    # 구간에 기록을 남기지 않는다 — 기다린 사실은 절차 Cause `900` 이 말한다.


def set_downstream(rec, spec: Spec, w: Walk, fault: Fault | None,
                   t: float, rng: random.Random) -> None:
    """하류 NAS 거절과 해제를 적는다 (불변규칙 7).

    **Cause 마다 다르다.** 정답지가 그 대응을 직접 보여 준다 —
    `15004 ROAMING_NOT_ALLOWED` 는 `EMM 11 PLMN not allowed` 로,
    `15421 RAT_NOT_ALLOWED` 는 `EMM 15 No suitable cells in TA` 로 간다.
    고정 하류 하나로 뭉뚱그리면 그 의미가 사라진다.

    ## 해제를 **먼저** 뽑는다

    구 구현은 하류가 없으면 `Last = First` 를 적고 **바로 나갔다.** 그러면
    해제 사유가 정해지지 않아 `_close_call` 이 평시 분포에서 뽑는다 —
    실패한 호가 `release:` 절의 정상 해제로 닫히는 레코드가 된다.

    `last_error` 는 해제가 정한다. 정답지의 `last = (2, 301)` 26건이
    **전부** `s1ap_error_Cause = 301` 이고 `UEContextRelease_Cause = 301`
    이다 — **해제 사유가 곧 마지막 오류**다. `302`(Detach)로 닫힌 호는
    정상 해제라 오류가 아니고, 그때 EMM 거절이 last 를 받는다.
    """
    d = None if (fault is not None and fault.no_downstream) \
        else spec.downstream_for(w.cause, fault)
    if d and fault is not None and rng.random() >= fault.downstream_rate:
        d = None
    if d and not d.get("message"):
        # **하류 거절의 메시지는 구간이 정한다.** 정답지에서 하류 EMM 은
        # `68 ATTACH_REJECT`(TAU 면 `75`), 하류 ESM 은 `209
        # PDN_CONNECTIVITY_REJECT` 다 — 예외가 없다. 비워 두면 Cause 만 있고
        # 메시지가 `0` 인 레코드가 된다.
        emm = (75 if w.call_type == 5           # TAU_REJECT
               else 78 if w.call_type in (3, 4, 7, 8)   # SERVICE_REJECT
               else 68)                          # ATTACH_REJECT
        d = dict(d, message={"emm": emm, "esm": 209}.get(d["prefix"], 0))
    # 하류 거절은 **응답과 같은 순간**이다(실측 8/8 — MME 가 응답을 받자마자
    # NAS 거절을 낸다). 서명이 없는 단계는 예전처럼 조금 뒤에 둔다.
    td = w.resp_time if w.resp_time else t + rng.uniform(0.01, 0.10)

    if d:
        rec.set(f"{d['prefix']}_error_Cause", d["cause"])
        rec.set(f"{d['prefix']}_error_Time", f"{td:.6f}")
        rec.set(f"{d['prefix']}_error_Message", d.get("message", 0))

        # **First 가 비어 있으면 하류가 그 자리도 받는다.** S6a TIMEOUT 과
        # S13 거절이 Call Error 에서 빠진 경우다(§3.2.6) — 정답지의
        # `auth=900` 3호가 전부 `first_error = 5/17` 이다.
        if not str(rec.get("first_error_interface_protocol") or "0").strip("0"):
            rec.set("first_error_interface_protocol", d["iface"])
            rec.set("first_error_cause", d["cause"])
            rec.set("first_error_message", d.get("message", 0))
            rec.set("first_error_time", f"{td:.6f}")

    # ── 해제 — **하류가 없어도 뽑는다** ──────────────────────────────
    name = w.broken_at.name if w.broken_at else ""
    # 타임아웃 표가 먼저다. 없으면 거절 표로 떨어진다(역호환).
    rel = ({w.cause: 1.0} if fault is not None and fault.release_from_cause
           else None) \
        or (fault.release_by_cause.get(w.cause) if fault is not None else None) \
        or ((fault.release_by_call_type.get(w.call_type) or {}).get("causes")
            if fault is not None else None) \
        or (fault.release if fault is not None and fault.release else None) \
        or (spec.fail_release_timeout.get(name) if w.timeout else None) \
        or spec.fail_release.get(name)
    w.release_request = bool(fault is not None and (
        (fault.release_request and (not fault.release_request_call_types
                                    or w.call_type in fault.release_request_call_types))
        or (fault.release_by_call_type.get(w.call_type) or {}).get("request")))
    if rel:
        ks = list(rel)
        c = rng.choices(ks, weights=[rel[k] for k in ks], k=1)[0]
        w.release_cause = c                     # `_close_call` 이 읽는다
        if c not in _S1AP_NORMAL:
            # **구간 Cause 도 적는다.** 정답지의 `last = (2,301)` 26건이
            # 예외 없이 `s1ap_error_Cause = 301` 을 들고 있다. 비우면
            # last 가 S1AP 를 가리키는데 그 구간에 근거가 없는 레코드가
            # 되고, 불변규칙 7(First Error 가 원인)의 대조가 서지 않는다.
            rec.set("s1ap_error_Cause", c)
            rec.set("s1ap_error_Message", 23)   # UEContextRelease
            rec.set("s1ap_error_Time", f"{td:.6f}")
            rec.set("last_error_interface_protocol", 2)
            rec.set("last_error_cause", c)
            rec.set("last_error_message", 23)
            rec.set("last_error_time", f"{td:.6f}")
            return

    if d:
        rec.set("last_error_interface_protocol", d["iface"])
        rec.set("last_error_cause", d["cause"])
        rec.set("last_error_message", d.get("message", 0))
        rec.set("last_error_time", f"{td:.6f}")
        return

    if fault is not None and fault.no_downstream:
        # **비운다 — First 를 복사하지 않는다.** 정답지의 페이징 무응답
        # 1호가 `first = (2,900)` 인데 `last = (0,0)` 이다. 오류가 아니라
        # 응답이 없었던 것이라 망이 거절을 내지 않는다.
        for k in ("interface_protocol", "cause", "message"):
            rec.set(f"last_error_{k}", 0)
        rec.set("last_error_time", "0.0")   # 숫자 필드의 "없음" 은 `0` 이다
        return

    # 하류가 없으면 Last = First 다 — 하류 증상이 없다는 뜻이고 그것도
    # 사실이다(규칙 C-5).
    for k in ("interface_protocol", "cause", "message", "time"):
        rec.set(f"last_error_{k}", rec.get(f"first_error_{k}"))


#: 정상 종료로 보는 S1AP Cause (불변규칙 6 · `codes.S1AP_NORMAL_CAUSES`).
#: **여기 있는 값은 `last_error` 가 되지 않는다** — 정상 종료는 오류가
#: 아니다. 표를 베끼지 않고 이 모듈이 쓰는 값만 둔다.
_S1AP_NORMAL = frozenset({102, 104, 105, 117, 118, 120, 122, 123, 124,
                          133, 134, 135, 300, 302})


def set_old_call(rec, spec: Spec, w: Walk, rng: random.Random) -> None:
    """직전 호 기록 — **실패 호에만.**

    *"과거 어떤 성공 또는 실패를 한 단말이 이번에 실패했을 때 이걸 뭘로
    보겠냐"*(사용자 확정 2026-10-28)가 이 필드의 쓸모다. 성공 호에 오류
    기록을 남기면 "직전 호가 실패인데 이번엔 성공" 이 되고, 그것은 재시도
    **성공**이라 `OLD_FAIL_CNT`(실패 반복)의 뜻과 어긋난다 — `cell_agg` 도
    `SUCCESS_FLAG != 1` 을 조건에 달고 있다.

    **같은 구간 반복이 기본값이다**(정답지 93%). `OLD_SAME_IFACE` 가 세는
    축이고, 재시도가 같은 자리에서 또 막히는 모양이다.
    """
    of = on_fail_of(w.broken_at, w.fault, w.call_type)
    if of.get("old_call_clear"):
        # 재시도 흔적이 없는 절차다 — 서비스 호의 S11 실패는 직전 호 오류가
        # 0/12 다(실측). 평시 `fail_share` 로 채우면 없는 재시도가 생긴다.
        rec.set("old_call_error_interface_protocol", "")
        rec.set("old_call_error_cause", "")
        return
    if w.broken_at is not None and rng.random() < float(of.get("old_call_same", 0.0)):
        # **재시도하다 또 막힌 호다** — 직전 호가 같은 구간·같은 Cause 로
        # 실패했다. 실측 S11 세션 실패 8/8 이 그렇다(Cause 가 `900` 이면
        # 직전도 `900`). 자원이 모자란 GW 앞에서 단말이 같은 요청을 반복한다.
        rec.set("old_call_error_interface_protocol",
                int(rec.num("first_error_interface_protocol")))
        rec.set("old_call_error_cause", int(rec.num("first_error_cause")))
        return
    if w.broken_at is None:
        # 성공 호 — 위치만 남기고 오류 기록은 **비운다.**
        rec.set("old_call_error_interface_protocol", "")
        rec.set("old_call_error_cause", "")
        return
    if rng.random() >= spec.old_call.get("fail_share", 0.0):
        rec.set("old_call_error_interface_protocol", "")
        rec.set("old_call_error_cause", "")
        return
    now = rec.num("first_error_interface_protocol")
    if now and rng.random() < spec.old_call.get("same_iface", 0.0):
        iface = int(now)
        # 같은 구간에서 또 막혔다. **Cause 까지 같게 두지는 않는다** —
        # 정답지에서 (구간, Cause) 쌍이 고르게 흩어져 있고, 같은 구간의
        # 다른 사유도 "반복" 이다.
        cause = int(rec.num("first_error_cause")) or 0
    else:
        st = w.broken_at
        iface, cause = st.iface, w.cause
    rec.set("old_call_error_interface_protocol", iface)
    rec.set("old_call_error_cause", cause)


def _flow_end(w: Walk, t0: float) -> float:
    """마지막 단계가 끝난 시각."""
    if not w.steps:
        return t0
    last = w.steps[-1]
    return w.times[last.name] + w.durations[last.name] / US


def _close_call(rec, t0: float, end: float, dur_us: float,
                rng: random.Random, *, spec: Spec | None = None,
                w: Walk | None = None) -> None:
    """`call_end_time` · `call_duration_time` 을 **단계 뒤로** 맞춘다.

    ## 왜 필요한가

    `call_duration_time` 은 실측 백분위에서 뽑히는데 그 분포에 `0` 이 많다.
    그대로 두면 **호가 끝난 뒤에 오류가 난** 레코드가 생긴다 — 실제로 첫
    데이터셋에서 `call_end − call_start = 0s` 인데 `last_error` 가
    `+0.12s` 였다. 시각 정합(T-1·T-2)이 거기서 깨진다.

    호 길이를 **단계 합의 하한**으로 쓰지 않고 **아래에서 밀어 올린다** —
    실측 분포가 더 길면 그쪽을 쓴다. 절차가 끝난 뒤에도 호는 이어지고
    (데이터를 주고받는다) 그것이 정상이다.
    """
    if w is not None and w.resp_time:
        # **응답보다 먼저 끝나는 호는 없다** — 하한이다.
        end = max(end, w.resp_time)
        of = on_fail_of(w.broken_at, w.fault, w.call_type)
        if of.get("end_at_response"):
            # **응답이 곧 호의 끝이다** — 실측 Attach 8/8 에서 S11 응답·하류·
            # 해제·`call_end` 가 같은 시각이다.
            dur_us = 0.0
        elif of.get("tail_s"):
            # 응답 뒤로 호가 **이어진다** — 서비스 호는 이미 붙어 있던 단말이라
            # 거절 뒤에도 eNB 가 해제를 요청할 때까지 남는다(실측 3~20초).
            lo, hi = (float(v) for v in of["tail_s"])
            end = w.resp_time + math.exp(rng.uniform(math.log(lo), math.log(hi)))
            dur_us = 0.0
    span = max(end - t0, 0.0)
    want = max(dur_us / US, span)
    if want <= 0.0:
        # 단계가 하나도 없고 실측도 0 인 호. 그래도 길이는 있어야 한다 —
        # `call_start == call_end` 는 "관측하지 못했다" 와 구분되지 않는다.
        want = rng.uniform(0.01, 0.05)
    # **올림한다.** 내림하면 단계 합과 1µs 차이로 어긋난다 — 호 길이는
    # 단계를 담는 그릇이므로 모자라는 쪽으로 틀리면 안 된다.
    us = int(math.ceil(want * US))
    t1 = t0 + us / US
    rec.set("call_end_time", f"{t1:.6f}")
    rec.set("call_duration_time", us)
    rec.set("SummaryCreateTime", f"{t1 + 0.05:.6f}")

    # ── 해제를 **호의 끝에** 적는다 ───────────────────────────────────
    #
    # 실측 204호에서 `UEContextRelease_Time == call_end_time` 이 **191/191,
    # 차이 0** 이다. *"release 까지가 하나의 호"* 가 그 뜻이다.
    #
    # `walk` 가 매긴 시각은 단계 누적값이라 호 끝보다 **앞선다**(접속 절차는
    # 앞 26% 에서 끝난다) — 여기서 덮는다.
    if spec is None or w is None:
        return
    names = {st.name for st in w.steps}
    if "release" not in names:
        # **단계열에 해제가 없으면 그 필드를 `0` 으로 되돌린다.**
        # `base_lte_call` 이 자체 분포로 `UEContextRelease_*` 를 채워 두는데,
        # 그대로 두면 Paging 레코드(접속 자체가 없다)에 해제 기록이 남는다.
        #
        # **빈 문자열이 아니라 `0` 이다.** 정답지 `ct=6` 12호가 전부
        # `Cause = 0` · `Time = 0.0` 이고, 204호 전체에서 빈 문자열이 나오는
        # 필드는 `APN`·`IMEI`·`PLMN` 같은 **텍스트뿐**이다 — 숫자 필드의
        # "없음" 은 `0` 으로 적는다. `''` 로 두면 Parquet 에서
        # `TRY_CAST('' AS BIGINT)` 가 NULL 이 되어 집계가 갈린다.
        for col in ("UEContextRelease_Cause", "UEContextReleaseRequest_Cause"):
            rec.set(col, 0)
        for col in ("UEContextRelease_Time", "UEContextReleaseRequest_Time"):
            rec.set(col, "0.0")
        return
    req_cause, rel_cause = spec.release.draw(w.call_type, rng)
    # **절차가 깨진 호는 해제 사유가 그것을 말한다.** `set_downstream` 이
    # 이미 뽑아 두었다 — 인증 실패면 S1AP `301 Authentication Failure` 가
    # 78%, 나머지는 `302 Detach` 다(실측 18호: 14 : 4).
    if w.release_cause:
        rel_cause = w.release_cause
        # MME 주도 해제면 요청이 없다. **eNB 가 요청했으면 같은 값이
        # 실린다**(정답지 `(2,103)`·`(3,68)` — `rreq = rel`).
        req_cause = rel_cause if w.release_request else 0
        if w.release_request:
            names = names | {"release_request"}
    rec.set("UEContextRelease_Time", f"{t1:.6f}")
    rec.set("UEContextRelease_Cause", rel_cause)
    if req_cause and "release_request" in names:
        # eNB 가 먼저 요청한다 — 그 뒤에 MME 가 해제한다. 간격은 짧다.
        rec.set("UEContextReleaseRequest_Time",
                f"{max(t0, t1 - rng.uniform(0.005, 0.05)):.6f}")
        rec.set("UEContextReleaseRequest_Cause", req_cause)
    else:
        # **요청이 없었으면 `0` 이다.** 전에는 빈 문자열로 뒀는데 정답지가
        # 그렇게 적지 않는다 — 숫자 필드의 "없음" 은 `0` 이고, 그것이
        # "요청이 없었다" 를 말하는 이 규격의 방식이다.
        rec.set("UEContextReleaseRequest_Time", "0.0")
        rec.set("UEContextReleaseRequest_Cause", 0)


# ── 검증 ───────────────────────────────────────────────────────────────────


def verify_fields(spec: Spec) -> list[str]:
    """명세가 쓰는 필드 이름이 **실재하는가.**

    `Rec.set` 은 모르는 이름을 **조용히 무시한다.** 오타가 있으면 "적었는데
    아무 일도 안 일어난" 상태가 되고 에러가 없다.
    """
    from .fields import LTE_CALL_FIELDS
    known = set(LTE_CALL_FIELDS)
    bad = []
    for s in spec.steps.values():
        for col in (s.time_field, s.cause_field):
            if col and col not in known:
                bad.append(f"steps[{s.name}] 의 {col}")
        if s.error_prefix:
            for suf in ("Cause", "Message", "Time"):
                col = f"{s.error_prefix}_error_{suf}"
                if col not in known:
                    bad.append(f"steps[{s.name}] 의 {col}")
    for p in PHASES:
        for col in (f"initial_{p}_duration", f"initial_{p}_msg_count"):
            if col not in known:
                bad.append(col)
    return bad


def verify(recs: list, spec: Spec) -> list[str]:
    """데이터셋이 명세를 지키는가. 어긋난 것을 **사람이 읽을 문장**으로 돌려준다.

    빈 리스트면 통과다. 세는 것은 생성 경로에서 실제로 깨져 있던 것들이고
    (확정대장 쪽이 아니라 원시 Parquet 실측), 숫자는 2026-10-28 기준이다 —

        절차 시각 순서 역전        49.8%
        단계 합 > 호 길이          22.6%
        페이징 플래그 ↔ 시간 불일치  20.1%
        시간 > 0 인데 메시지 0      28.1%
    """
    bad: list[str] = []
    order = [s for s in ("InitialUEMessage_Time", "AuthenticationInformation_Time",
                         "MEIdentityCheck_Time", "UpdateLocation_Time")]
    rev = over = pag = nomsg = ferr = s3 = outside = gate = 0
    for r in recs:
        ts = [r.num(c) for c in order if r.num(c) > 0]
        if ts != sorted(ts):
            rev += 1
        dur = r.num("call_duration_time")
        tot = sum(r.num(f"initial_{p}_duration") for p in PHASES)
        if dur > 0 and tot > dur:
            over += 1
        # 단계가 있으면 시간이 있고, 없으면 없다. **양방향으로 본다** —
        # 한쪽만 보면 "페이징을 안 했는데 시간이 있는" 쪽을 놓친다.
        pg_flag = str(r.get("paging_attempt_flag")) in ("1", "True")
        if pg_flag != (r.num("initial_paging_duration") > 0):
            pag += 1
        for p in PHASES:
            if r.num(f"initial_{p}_duration") > 0 \
                    and r.num(f"initial_{p}_msg_count") == 0:
                nomsg += 1
                break
        t0, t1 = r.num("call_start_time"), r.num("call_end_time")
        if t1 > 0:
            for c in order + ["first_error_time", "last_error_time"]:
                v = r.num(c)
                if v > 0 and not (t0 <= v <= t1 + 1e-6):
                    outside += 1
                    break
        fi = r.num("first_error_interface_protocol")
        if fi in FORBIDDEN_IFACE:
            s3 += 1
        # 실패 호에는 First Error 가 있어야 한다. 그 역도 참이다 —
        # 오류가 있는데 성공으로 적힌 호는 `C-02` 를 위로 밀어 올린다.
        failed = str(r.get("success_flag")) in ("0", "False")
        att = str(r.get("attempt_flag")) in ("1", "True")
        # **구간과 Cause 가 둘 다 비어야 "없음" 이다.** `902 NO_CAUSE_PAGING`
        # 은 적을 메시지가 없어 구간이 `0` 이지만 Cause 는 있다 — 정답지
        # `(0,902)` 4호가 전부 `attempt=1 · success=0` 이다.
        if att and failed and fi == 0 and not r.num("first_error_cause"):
            ferr += 1

        # 끊긴 뒤 절차를 **시도했다고 적지 않는다.** 인증이 깨졌는데
        # `LOCATION_ATTEMPT_FLAG = 1` 이면 `P-02` 의 분모가 장애 규모를 탄다.
        if fi == 1 and str(r.get("auth_success_flag")) in ("0", "False") \
                and str(r.get("auth_attempt_flag")) in ("1", "True") \
                and str(r.get("location_attempt_flag")) in ("1", "True"):
            gate += 1

    n = len(recs) or 1
    for label, cnt, was in (
            ("절차 시각 순서 역전", rev, "49.8%"),
            ("단계 합 > 호 길이", over, "22.6%"),
            ("페이징 플래그 ↔ 시간 불일치", pag, "20.1%"),
            ("시간 > 0 인데 메시지 0", nomsg, "28.1%"),
            ("규격이 금지한 S3(7)가 First Error 에", s3, "0"),
            ("시도·실패인데 First Error 없음", ferr, "—"),
            ("시각이 호 구간 밖", outside, "—"),
            ("인증 실패인데 위치갱신 시도", gate, "—")):
        if cnt:
            bad.append(f"{label}: {cnt:,}/{n:,} ({cnt * 100.0 / n:.2f}%) "
                       f"· 생성 경로 실측 {was}")
    return bad


# ── 데이터셋 ───────────────────────────────────────────────────────────────


#: 생성물이 나가는 곳. `data/` 는 통째로 `.gitignore` 라 **산출물 자리**이고,
#: 명세(`config/callflow.yaml`)만 추적본이다.
DEFAULT_OUT = Path(__file__).resolve().parents[2] / "data" / "callflow"

#: 한 호의 phase 총량을 뽑을 프로파일 필드. **실측 백분위를 그대로 쓴다** —
#: 여기서 숫자를 지어내면 데이터셋이 실제 XDR 과 다른 모양이 된다.
PHASE_FIELD = {
    "paging": "initial_paging_duration",
    "access": "initial_access_duration",
    "core": "initial_core_duration",
}


def nonzero_quantile(sampler, field: str, rng: random.Random) -> float:
    """그 필드의 **값이 있는 구간**에서 뽑는다.

    실측 백분위가 대부분 `0` 이다 (`data/profile.json`) —

        initial_paging_duration   p0~p80 = 0 · p90 = 214,745µs
        initial_access_duration   p0~p60 = 0 · p70 =  32,831µs
        initial_core_duration     p0~p50 = 0 · p60 =     369µs

    **상용 XDR 자체가 이 필드를 대부분 비운다.** 그대로 뽑으면 **단계는
    일어났는데 소요시간이 0** 인 레코드가 쏟아지고, 0 은 "안 일어났다" 와
    구분되지 않는다(불변규칙 17).

    **런타임과 데이터셋의 의도된 차이다.** 런타임 생성기는 실측 분포를
    그대로 따라야 상용과 같은 모양이 나온다. 데이터셋은 *"call flow 대로
    단계마다 데이터가 있는"* 완전형이고 그래야 단계열을 검증할 수 있다.

    **숫자를 지어내지 않는다** — 같은 실측 백분위의 **꼬리**를 쓴다.
    """
    dec = sampler.p.numeric.get(field)
    if not dec:
        return 0.0
    lo = next((i for i, v in enumerate(dec) if v > 0), None)
    if lo is None:
        return 0.0
    u = min(9.999, lo + rng.random() * (10.0 - lo))
    return sampler.numeric(field, u=u)


def build(gen, dist, spec: Spec, *, call_type: int | None = None,
          fault: str | None = None, n: int = 1000,
          ts=None, rng: random.Random | None = None) -> list:
    """레코드 ``n`` 건. ``fault`` 가 없으면 정상 호다.

    ``gen`` 은 `Generator`, ``dist`` 는 `PemdbDist` 다 — **둘 다 DB 없이
    뜬다.** 셀·가입자·식별자는 `gen.base_lte_call` 이 채우고, `call_type`
    과 플래그의 **초기값**은 `dist` 가 준다. 단계 발생 여부는 `Flow.share`
    (정답지)가 정하고 `apply()` 가 플래그를 거기 맞춘다.
    """
    from datetime import datetime

    rng = rng or random.Random(20261028)
    ts = ts or datetime(2026, 10, 28, 9, 0)
    slot = ts.timestamp()
    f_spec = spec.faults[fault] if fault else None
    pool = ([call_type] if call_type
            else list(spec.call_types_for(f_spec)) if f_spec
            else list(spec.flows))

    out = []
    tries = 0
    while len(out) < n:
        tries += 1
        if tries > n * 60:
            raise RuntimeError(
                f"{fault or '정상'} 데이터셋을 {n}건 채우지 못했다 "
                f"({len(out)}건에서 멈춤) — `{f_spec.break_at if f_spec else ''}` "
                f"단계를 밟는 호가 그만큼 드물다. `flows` 를 다시 보라")
        rec, _cell = gen.base_lte_call(ts, slot)
        ct = rng.choice(pool)
        rec.set("call_type", ct)
        rec.set("OngoingFlag", "2")
        flags = dist.flags(str(ct))
        # 단계열에 없는 절차의 플래그는 내린다. `share` 를 적은 단계는
        # 그 비율이 정본이고 `apply()` 가 추첨 결과로 덮는다.
        names = {s.name for s in spec.flow_steps(ct)}
        for gate, step in (("pg", "paging"), ("auth", "auth"),
                           ("loc", "ulr"), ("mec", "mecheck")):
            if step not in names:
                flags[f"{gate}_att"] = flags[f"{gate}_suc"] = 0
        for k, col in (("att", "attempt_flag"), ("suc", "success_flag"),
                       ("drop", "drop_flag"), ("detach", "detach_flag"),
                       ("data_att", "data_attempt_flag"),
                       ("data_suc", "data_success_flag"),
                       ("ims_att", "ims_attempt_flag"),
                       ("ims_suc", "ims_success_flag"),
                       ("pg_att", "paging_attempt_flag"),
                       ("pg_suc", "paging_success_flag"),
                       ("auth_att", "auth_attempt_flag"),
                       ("auth_suc", "auth_success_flag"),
                       ("loc_att", "location_attempt_flag"),
                       ("loc_suc", "location_success_flag"),
                       ("mec_att", "mecheck_attempt_flag"),
                       ("mec_suc", "mecheck_success_flag")):
            rec.set(col, flags.get(k, 0))

        # **`attempt_flag` 로 call flow 를 판단하지 않는다** (2026-10-28).
        #
        # 한때 "시도하지 않은 호는 담지 않는다" 로 걸렀다. 정답지 204호가
        # 전부 `attempt_flag=1` 이었기 때문인데, **그 샘플에 call_type
        # 9·10·11 이 없어서** 그렇게 보인 것이다. 실측 `flag_ratio` 는 —
        #
        #     Detach MO(9) · S1 Handover(10) · Reroute(11)   att=1  **0%**
        #
        # `codes.CALL_TYPE_GROUP` 주석이 같은 말을 한다: *"Reroute 는
        # 실측에서 attempt·success 플래그가 0이라 분모에 기여하지 않는다"*.
        # **소통 시도(`C-02` 분모)와 절차 수행은 다른 축**이고, Detach 호도
        # RRC 와 Release 를 밟는다. 거르면 그 세 절차가 통째로 사라진다.

        t0 = rec.num("call_start_time")
        dur = rec.num("call_duration_time")
        smp = gen.samplers["lte_call"]
        totals = {p: nonzero_quantile(smp, PHASE_FIELD[p], rng) for p in PHASES}
        totals = fail_totals(spec, f_spec, totals, rng, ct)
        w = walk(spec, ct, flags, rng, t0=t0, call_dur_us=dur,
                 phase_totals=totals, fault=f_spec)
        # **장애 데이터셋에는 그 장애를 실제로 겪은 호만 담는다.** 플래그를
        # 세워서 만들지 않는다 — 그러면 분모를 장애가 만드는 꼴이 되고,
        # `scenarios._auth_failure` 가 피한 것이 정확히 그것이다. 다시 뽑는다.
        if f_spec is not None and (w.broken_at is None
                                   or w.broken_at.name != f_spec.break_at):
            continue
        apply(rec, spec, w, flags, rng)
        written = sum(rec.num(f"initial_{p}_duration") for p in PHASES)
        end = max(_flow_end(w, t0), t0 + written / US)
        if w.broken_at is not None:
            t = w.times[w.broken_at.name] + w.durations[w.broken_at.name] / US
            set_downstream(rec, spec, w, f_spec, t, rng)
            end = max(end, rec.num("last_error_time"))
        set_old_call(rec, spec, w, rng)
        _close_call(rec, t0, end, dur, rng, spec=spec, w=w)
        out.append(rec)
    return out


#: 레코드 플래그 → `flags()` 키. `build()` 가 쓰는 표의 역방향이다.
_FLAG_COLS = (("att", "attempt_flag"), ("suc", "success_flag"),
              ("drop", "drop_flag"), ("detach", "detach_flag"),
              ("pg_att", "paging_attempt_flag"), ("pg_suc", "paging_success_flag"),
              ("auth_att", "auth_attempt_flag"), ("auth_suc", "auth_success_flag"),
              ("loc_att", "location_attempt_flag"),
              ("loc_suc", "location_success_flag"),
              ("mec_att", "mecheck_attempt_flag"),
              ("mec_suc", "mecheck_success_flag"))

_SPEC_CACHE: list[Spec] = []


def _spec() -> Spec:
    if not _SPEC_CACHE:
        _SPEC_CACHE.append(load_spec())
    return _SPEC_CACHE[0]


def on_fail_of(step: Step | None, fault: Fault | None,
               call_type: int | None = None) -> dict:
    """끊긴 자리의 실측 서명 — 단계 기본값 위에 장애가, 그 위에 call_type 이 덮는다.

    **같은 단계라도 절차마다 꼴이 다르다.** S11 세션 실패가 Attach 에서는
    재전송 끝에 1.4~2.0초 걸리고 호가 응답에서 닫히는데, 서비스 호에서는
    2~120ms 에 거절되고 호가 10초가량 이어진다(실측). `by_call_type` 이 그
    차이를 적는 자리다.
    """
    if step is None:
        return {}
    out: dict = {}
    for src in (step.on_fail, fault.on_fail if fault is not None else {}):
        out.update({k: v for k, v in src.items() if k != "by_call_type"})
        if call_type is not None:
            out.update((src.get("by_call_type") or {}).get(call_type) or {})
    return out


def fail_totals(spec: Spec, fault: Fault | None, totals: dict[str, float],
                rng: random.Random, call_type: int | None = None) -> dict[str, float]:
    """끊길 단계에 `on_fail.core_us` 가 있으면 **core phase 총량을 그 값으로.**

    `walk` 가 phase 총량을 단계에 나눠 시각을 매기므로, 여기서 바꿔야 단계
    시각·First 시각·호 길이가 함께 맞는다. 레코드에 길이만 덮으면 시각과
    길이가 서로 다른 말을 한다.
    """
    if fault is None:
        return totals
    of = on_fail_of(spec.steps[fault.break_at], fault, call_type)
    out = dict(totals)
    for phase in ("access", "core"):
        lo_hi = of.get(f"{phase}_us")
        if lo_hi:
            lo, hi = (float(v) for v in lo_hi)
            # 로그 균등 — 실측이 몇 배 폭으로 퍼진다(core 0.7~3.0초 · access 2~6초).
            out[phase] = math.exp(rng.uniform(math.log(lo), math.log(hi)))
    return out


def inject(rec, fault_name: str, rng: random.Random, *, tries: int = 6,
           cause: int | None = None, downstream: bool = True) -> bool:
    """**런타임 레코드 한 건을 그 장애 호로 다시 쓴다** — UI 주입 경로.

    `build()` 의 한 호 처리와 같은 순서다(`walk → apply → set_downstream →
    set_old_call → _close_call`). 다른 것은 둘 —

    * **레코드가 이미 있다.** 셀·가입자·식별자·시각은 생성기가 만든 그대로
      두고, call flow 가 정하는 칸만 덮는다. 그래서 주입된 호가 그 분의
      다른 호와 같은 셀·같은 장비에 실린다.
    * **끊을 단계를 못 밟으면 손대지 않는다**(`False`). `build()` 는 호를
      다시 뽑지만 여기서는 호가 하나뿐이다 — 플래그를 세워 억지로 끊으면
      분모를 장애가 만드는 꼴이 된다(`build` 의 같은 주석).

    phase 총량은 그 레코드의 값을 쓴다. 비어 있으면(실측도 대부분 0 이다 —
    `nonzero_quantile` 머리말) 작은 기본값으로 단계 시각만 세운다.

    ``cause`` 를 주면 **Cause 는 장애가 정한다** — 시나리오가 고른 값(SC-11
    의 ESM `30` 등)은 그대로 두고 그 Cause 가 실리는 호의 **모양**만 명세에서
    가져온다. 명세의 `causes` 표는 정답지 비율이고, 시나리오는 그중 한
    원인을 겨냥한다.

    ``downstream=False`` 면 하류 거절을 만들지 않는다 — Last = First 다.
    SC-18 이 그렇다(2026-10-22 사용자 조건: *"모든 Timeout 에 ESM 30 을
    강제하는 전역 규칙은 만들지 않음"*).
    """
    spec = _spec()
    fault = spec.faults[fault_name]
    if cause is not None:
        fault = replace(fault, causes={int(cause): 1.0})
    if not downstream:
        fault = replace(fault, downstream_rate=0.0)
    try:
        ct = int(rec.get("call_type"))
    except (TypeError, ValueError):
        return False
    if ct not in (fault.inject_call_types or spec.call_types_for(fault)):
        return False
    flags = {k: int(rec.num(col)) for k, col in _FLAG_COLS}
    t0 = rec.num("call_start_time")
    dur = rec.num("call_duration_time")
    if not t0:
        return False
    totals = {p: (rec.num(PHASE_FIELD[p]) or 50_000.0) for p in PHASES}
    totals = fail_totals(spec, fault, totals, rng, ct)
    for _ in range(tries):
        w = walk(spec, ct, flags, rng, t0=t0, call_dur_us=dur,
                 phase_totals=totals, fault=fault,
                 # **레코드가 이미 시도 여부를 말한다** — 다시 뽑으면 장애가
                 # 시도 플래그(성공률의 분모)를 바꾼다(`auth_failure` 주석)
                 use_flags=True)
        if w.broken_at is not None and w.broken_at.name == fault.break_at:
            break
    else:
        return False
    apply(rec, spec, w, flags, rng)
    written = sum(rec.num(f"initial_{p}_duration") for p in PHASES)
    end = max(_flow_end(w, t0), t0 + written / US)
    t = w.times[w.broken_at.name] + w.durations[w.broken_at.name] / US
    set_downstream(rec, spec, w, fault, t, rng)
    end = max(end, rec.num("last_error_time"))
    set_old_call(rec, spec, w, rng)
    _close_call(rec, t0, end, dur, rng, spec=spec, w=w)
    return True


def drop(rec, cause: int, rng: random.Random) -> bool:
    """**연결된 호를 비정상 해제로 끊는다** — `spec.drop` 의 실측 모양.

    call flow 는 성공했으므로 단계를 다시 밟지 않는다. 바꾸는 것은 호의 끝 —
    eNB 가 `cause` 로 해제를 요청하고(First, msg 18) MME 가 해제한다(Last,
    msg 23). 호 길이를 줄이고 그 끝에 둘을 놓는다.

    연결되지 않은 호(`success_flag != 1`)는 손대지 않는다(`False`) — 절단은
    접속된 호에만 있다(규칙 C-2·C-4).
    """
    d = _spec().drop
    if str(rec.get("success_flag")) not in ("1", "True"):
        return False
    cts = d.get("call_types")
    if cts and str(rec.get("call_type")) not in {str(c) for c in cts}:
        return False
    t0 = rec.num("call_start_time")
    dur = rec.num("call_duration_time") / US
    if not t0 or dur <= 0:
        return False
    # 절차가 끝난 뒤여야 한다 — 단계 합보다 먼저 끊을 수 없다.
    floor = sum(rec.num(f"initial_{p}_duration") for p in PHASES) / US
    lo, hi = (float(v) for v in d.get("duration_scale", (0.15, 0.6)))
    t_req = t0 + max(floor + 0.01, dur * rng.uniform(lo, hi))
    gap = ((d.get("request_gap_ms_by_cause") or {}).get(cause)
           or d.get("request_gap_ms", (1, 12)))
    g_lo, g_hi = (float(v) / 1000 for v in gap)
    t_rel = t_req + rng.uniform(g_lo, g_hi)
    m_req, m_rel = int(d.get("first_message", 18)), int(d.get("last_message", 23))
    rec.set("drop_flag", 1)
    rec.set("first_error_interface_protocol", 2)
    rec.set("first_error_cause", cause)
    rec.set("first_error_message", m_req)
    rec.set("first_error_time", f"{t_req:.6f}")
    for k, v in (("interface_protocol", 2), ("cause", cause), ("message", m_rel),
                 ("time", f"{t_rel:.6f}")):
        rec.set(f"last_error_{k}", v)
    rec.set("s1ap_error_Cause", cause)
    rec.set("s1ap_error_Message", m_rel)
    rec.set("s1ap_error_Time", f"{t_rel:.6f}")
    rec.set("UEContextReleaseRequest_Cause", cause)
    rec.set("UEContextReleaseRequest_Time", f"{t_req:.6f}")
    rec.set("UEContextRelease_Cause", cause)
    rec.set("UEContextRelease_Time", f"{t_rel:.6f}")
    # 직전 호 오류는 없다 — 실측 0/81. 절단은 재시도의 결과가 아니다.
    rec.set("old_call_error_interface_protocol", "")
    rec.set("old_call_error_cause", "")
    us = int(math.ceil((t_rel - t0) * US))
    rec.set("call_end_time", f"{t0 + us / US:.6f}")
    rec.set("call_duration_time", us)
    rec.set("SummaryCreateTime", f"{t0 + us / US + 0.05:.6f}")
    return True


def _fixtures(cfg: dict, data_dir: Path, seed: int):
    """데이터셋 생성에 필요한 것 — **DB 를 쓰지 않는다.**

    `Generator`(셀·가입자·식별자)와 `Distributions`(상용 실측 결합분포)만
    있으면 154컬럼이 다 찬다. `PemdbGen` 을 띄우면 `CL_CELL_MAP` 적재가
    필요해지고, 데이터셋을 만들려고 창고를 세우는 것은 앞뒤가 바뀐 것이다.
    """
    from .cells import load_cells
    from .generator import Generator
    from .pemdb1 import Baseline, Distributions
    from .profile import load as load_profiles

    cells = load_cells(data_dir / "cells.json")
    profiles = load_profiles(data_dir / "profile.json",
                             locale=(cfg.get("pemdb1") or {}).get("locale", "kr"))
    keep = ("lte_call", "transport", "dns", "http")
    gen = Generator(cells=cells,
                    profiles={s: profiles[s] for s in keep if s in profiles},
                    scenarios=[], rates={"lte_call": 2000}, seed=seed,
                    diurnal=False, subscriber_pool=800,
                    success_only=True, end_only=True)
    bl = Baseline.from_cfg((cfg.get("pemdb1") or {}).get("baseline"))
    dist = Distributions(data_dir / "pemdb1" / "_distributions.json",
                         random.Random(seed + 101), success_only=True,
                         call_type_floor=None, baseline=bl)
    return gen, dist


def write(recs: list, path: Path) -> int:
    """레코드를 Parquet 로. **컬럼은 전부 문자열이다.**

    원시 Parquet 과 같은 모양이어야 같은 질의로 읽을 수 있다 —
    `cell_agg` 머리말이 적듯 *"Parquet 컬럼이 여전히 전부 문자열"* 이고
    숫자로 접으면 `'0'` 센티널·`'021'` 0채움·플래그 비교가 어긋난다.
    """
    import polars as pl

    from .fields import LTE_CALL_FIELDS
    from .pemdb1 import CPLANE_FIELDS

    # 상용 컬럼명으로 내보낸다 — `data/cplane_raw` 와 같은 이름이어야
    # 둘을 한 질의로 대조할 수 있다.
    cols = [(dst, src) for dst, src, *_ in CPLANE_FIELDS]
    idx = {n: i for i, n in enumerate(LTE_CALL_FIELDS)}
    data = {dst: [] for dst, _ in cols}
    for r in recs:
        for dst, src in cols:
            i = idx.get(src)
            data[dst].append("" if i is None else str(r.values[i] or ""))
    path.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(data).write_parquet(path, compression="zstd")
    return len(recs)


def build_all(cfg: dict, data_dir: Path, out: Path | None = None, *,
              n_normal: int = 2000, n_fault: int = 500,
              seed: int = 20261028, spec_path: Path | None = None) -> dict:
    """정상 호(call_type 별) + 장애별 데이터셋을 전부 만든다.

    돌려주는 것은 `{이름: (건수, 검증결과)}` 다 — 검증이 비어 있으면 통과다.
    """
    spec = load_spec(spec_path)
    bad = verify_fields(spec)
    if bad:
        raise ValueError("명세가 없는 필드를 쓴다: " + ", ".join(bad))
    gen, dist = _fixtures(cfg, data_dir, seed)
    out = out or DEFAULT_OUT
    report: dict[str, tuple[int, list[str]]] = {}

    for ct, fl in sorted(spec.flows.items()):
        rng = random.Random(seed + ct)
        recs = build(gen, dist, spec, call_type=ct, n=n_normal, rng=rng)
        slug = fl.name.lower().replace(" ", "_")
        write(recs, out / "normal" / f"{ct:02d}_{slug}.parquet")
        report[f"normal/{ct:02d}_{slug}"] = (len(recs), verify(recs, spec))

    for i, fname in enumerate(sorted(spec.faults)):
        rng = random.Random(seed + 1000 + i)
        recs = build(gen, dist, spec, fault=fname, n=n_fault, rng=rng)
        write(recs, out / "fault" / f"{fname}.parquet")
        report[f"fault/{fname}"] = (len(recs), verify(recs, spec))
    return report
