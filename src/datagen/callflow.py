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

명세는 그것을 **데이터로 꺼낸 것**이다. 실측으로 확인한 공백 둘 —

    first_error_interface_protocol = 8 (S13)   생성 데이터에 0건
    UpdateLocation_Cause 장애 주입              없음 (평시 배경만)

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
from dataclasses import dataclass, field
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
    #: 직전 호 기록 — `{fail_share, same_iface}`. **실패 호에만 쓴다.**
    old_call: dict[str, float]

    def downstream_for(self, cause: int, fault: Fault | None) -> dict | None:
        """그 Cause 의 하류. 장애가 따로 적었으면 그쪽이 이긴다."""
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
    frel = {k: {int(c): float(w) for c, w in v.items()}
            for k, v in (raw.get("fail_release") or {}).items()}
    oc = {k: float(v) for k, v in (raw.get("old_call") or {}).items()}
    return Spec(steps=steps, flows=flows, faults=faults, release=rel,
                downstream_map=dmap, fail_release=frel, old_call=oc)


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
         fault: Fault | None = None) -> Walk:
    """단계를 전개한다. ``fault`` 가 있으면 그 단계에서 **끊는다**.

    ``phase_totals`` 는 phase 별 총 소요시간(µs)이고 **호출부가 준다** —
    실측 백분위에서 뽑은 값을 배분만 하려는 것이다. 주변분포가 바뀌면
    `pemdb1` 대조 통계가 어긋난다.
    """
    w = Walk(call_type=call_type)
    steps = active_steps(spec, call_type, flags, rng)
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
    totals = {}
    for p in PHASES:
        if used[p] <= 0:
            totals[p] = 0.0          # 일어나지 않은 phase 는 **0 이다**
        elif full[p] > 0:
            totals[p] = phase_totals.get(p, 0.0) * (used[p] / full[p])
        else:
            totals[p] = phase_totals.get(p, 0.0)
    # 단계 합이 호 길이를 넘지 않는다. 한 phase 만 깎으면 그쪽 분포가 치우치므로
    # 비례 축소한다.
    if call_dur_us > 0:
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

    # ① 끊긴 단계가 First Error 다 — **단 S6a·S13 의 TIMEOUT 은 아니다.**
    #    §3.2.6 이 그 둘만 TIMEOUT 을 Call Error 에서 뺀다. 그러면 First
    #    자리가 비고 **하류 NAS 거절이 그것을 받는다**(`set_downstream`).
    rec.set("success_flag", 0)
    if w.timeout and s.iface in TIMEOUT_EXCLUDED_IFACE:
        rec.set("first_error_interface_protocol", 0)
        rec.set("first_error_cause", 0)
        rec.set("first_error_message", 0)
        rec.set("first_error_time", "")
    else:
        rec.set("first_error_interface_protocol", s.iface)
        rec.set("first_error_cause", w.cause)
        rec.set("first_error_message", s.error_message)
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
    if s.cause_field:
        rec.set(s.cause_field, w.cause)
    if not w.timeout:
        if s.error_prefix:
            rec.set(f"{s.error_prefix}_error_Cause", w.cause)
            rec.set(f"{s.error_prefix}_error_Message", s.error_message)
            rec.set(f"{s.error_prefix}_error_Time", f"{t:.6f}")
    elif s.error_prefix:
        # 시각은 남긴다 — **언제 기다리다 포기했는지**는 사실이다.
        rec.set(f"{s.error_prefix}_error_Time", f"{t:.6f}")
        rec.set(f"{s.error_prefix}_error_Message", s.error_message)


def set_downstream(rec, spec: Spec, w: Walk, fault: Fault | None,
                   t: float, rng: random.Random) -> None:
    """하류 NAS 거절을 적는다 (불변규칙 7).

    **Cause 마다 다르다.** 정답지가 그 대응을 직접 보여 준다 —
    `15004 ROAMING_NOT_ALLOWED` 는 `EMM 11 PLMN not allowed` 로,
    `15421 RAT_NOT_ALLOWED` 는 `EMM 15 No suitable cells in TA` 로 간다.
    고정 하류 하나로 뭉뚱그리면 그 의미가 사라진다.

    하류가 없으면 Last = First 다 — 하류 증상이 없다는 뜻이고 그것도
    사실이다(규칙 C-5).
    """
    d = spec.downstream_for(w.cause, fault)
    if not d:
        for k in ("interface_protocol", "cause", "message", "time"):
            rec.set(f"last_error_{k}", rec.get(f"first_error_{k}"))
        return
    td = t + rng.uniform(0.01, 0.10)
    rec.set(f"{d['prefix']}_error_Cause", d["cause"])
    rec.set(f"{d['prefix']}_error_Time", f"{td:.6f}")
    rec.set(f"{d['prefix']}_error_Message", d.get("message", 0))

    # **First 가 비어 있으면 하류가 그 자리도 받는다.** S6a·S13 TIMEOUT 이
    # Call Error 에서 빠진 경우다(§3.2.6) — 정답지의 `auth=900` 3호가
    # 전부 `first_error = 5/17` 이다.
    if not str(rec.get("first_error_interface_protocol") or "0").strip("0"):
        rec.set("first_error_interface_protocol", d["iface"])
        rec.set("first_error_cause", d["cause"])
        rec.set("first_error_message", d.get("message", 0))
        rec.set("first_error_time", f"{td:.6f}")

    # **해제 사유가 곧 마지막 오류인 경우가 있다.** 인증이 깨진 호는 S1AP
    # `301 Authentication Failure` 로 닫히고, 실측 18호 중 그렇게 닫힌
    # 14호의 `last_error` 가 전부 `2/301` 이다. `302`(Detach)로 닫힌 4호는
    # EMM 거절이 last 를 받는다 — 그래서 해제 Cause 를 먼저 뽑는다.
    rel = spec.fail_release.get(w.broken_at.name if w.broken_at else "")
    if rel:
        ks = list(rel)
        c = rng.choices(ks, weights=[rel[k] for k in ks], k=1)[0]
        w.release_cause = c                     # `_close_call` 이 읽는다
        if c not in _S1AP_NORMAL:
            rec.set("last_error_interface_protocol", 2)
            rec.set("last_error_cause", c)
            rec.set("last_error_message", 23)   # UEContextRelease
            rec.set("last_error_time", f"{td:.6f}")
            return
    rec.set("last_error_interface_protocol", d["iface"])
    rec.set("last_error_cause", d["cause"])
    rec.set("last_error_message", d.get("message", 0))
    rec.set("last_error_time", f"{td:.6f}")


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
        # **단계열에 해제가 없으면 그 필드를 비운다.** `base_lte_call` 이
        # 자체 분포로 `UEContextRelease_*` 를 채워 두는데, 그대로 두면
        # Paging 레코드(접속 자체가 없다)에 해제 기록이 남는다 — 실측
        # 12호 전부 그 필드가 비어 있다.
        for col in ("UEContextRelease_Time", "UEContextRelease_Cause",
                    "UEContextReleaseRequest_Time",
                    "UEContextReleaseRequest_Cause"):
            rec.set(col, "")
        return
    req_cause, rel_cause = spec.release.draw(w.call_type, rng)
    # **절차가 깨진 호는 해제 사유가 그것을 말한다.** `set_downstream` 이
    # 이미 뽑아 두었다 — 인증 실패면 S1AP `301 Authentication Failure` 가
    # 78%, 나머지는 `302 Detach` 다(실측 18호: 14 : 4).
    if w.release_cause:
        rel_cause = w.release_cause
        req_cause = 0          # MME 주도 해제다 — eNB 가 요청한 것이 아니다
    rec.set("UEContextRelease_Time", f"{t1:.6f}")
    rec.set("UEContextRelease_Cause", rel_cause)
    if req_cause and "release_request" in names:
        # eNB 가 먼저 요청한다 — 그 뒤에 MME 가 해제한다. 간격은 짧다.
        rec.set("UEContextReleaseRequest_Time",
                f"{max(t0, t1 - rng.uniform(0.005, 0.05)):.6f}")
        rec.set("UEContextReleaseRequest_Cause", req_cause)
    else:
        # **요청이 없었으면 비워 둔다.** `0` 을 적으면 "요청이 있었고
        # Cause 가 0" 처럼 읽힌다(불변규칙 17).
        rec.set("UEContextReleaseRequest_Time", "")
        rec.set("UEContextReleaseRequest_Cause", "")


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
        if att and failed and fi == 0:
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
    profiles = load_profiles(data_dir / "profile.json")
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
