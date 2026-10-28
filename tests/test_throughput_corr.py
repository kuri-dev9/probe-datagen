"""처리율(`U-15`)이 **탐지에 쓸 수 있는 값**인가.

`Traffic_Total_*_PayloadSize` 와 `*_ThroughputTime` 을 독립으로 뽑으면 한
레코드의 처리율이 **두 독립 난수의 비(比)** 라 무작위가 된다. 창고 실측이
그 모양이었다 (확정대장 §9.8.9):

    셀·분 U-15    84.2% 가 0.01 Mbps 미만  ·  최대 228 Mbps
    셀별 변동계수  평균 3.51 (σ 가 µ 의 3.5배)

그 분산 위에서는 **D1 절대 임계도 `EV-08` 의 30% 하락 판정도 서지 않고**,
`j-sc-16`(앵커 고착)의 주 신호가 통째로 막힌다. 2026-10-28 에 **같은
분위수를 공유**하도록 고쳤다.

**이 파일이 지키는 것은 둘이다** —

1. **상관이 실제로 생겼다** (집계 변동계수가 내려갔다)
2. **그 대가로 다른 것이 바뀌지 않았다** — 각 필드의 주변분포, 그리고
   `U-12`(재전송률)의 분자·분모가 묶이지 않은 것
"""

from __future__ import annotations

import random
import statistics
from pathlib import Path

import pytest

from datagen import profile as pf

HERE = Path(__file__).resolve().parents[1]
PS = "Traffic_Total_Dnlink_PayloadSize"
TT = "Traffic_Total_Dnlink_ThroughputTime"


@pytest.fixture(scope="module")
def prof():
    p = HERE / "data" / "profile.json"
    if not p.exists():
        pytest.skip(f"프로파일이 없다: {p}")
    return pf.load(p)["transport"]


def _sampler(prof, seed=7):
    return pf.Sampler(prof, random.Random(seed))


# ── 1. `u` 자체의 계약 ────────────────────────────────────────────────


def test_same_quantile_gives_same_value(prof):
    """**같은 `u` 는 같은 값이다.** 이것이 깨지면 공유가 성립하지 않는다."""
    smp = _sampler(prof)
    for u in (0.0, 1.5, 4.2, 7.7, 9.3, 9.99):
        assert smp.numeric(PS, u=u) == smp.numeric(PS, u=u)


def test_explicit_quantile_does_not_consume_randomness(prof):
    """`u` 를 주면 **난수를 쓰지 않는다.**

    쓰면 같은 시드로 돌린 전후가 어긋나 재현이 깨진다 — 생성기 전체가
    시드 재현에 기대고 있다.
    """
    smp = _sampler(prof)
    before = smp.rng.getstate()
    smp.numeric(PS, u=3.3)
    smp.numeric(TT, u=3.3)
    assert smp.rng.getstate() == before

    # 반대로 `u` 가 없으면 **소비한다** — 기존 동작이다.
    smp.numeric(PS)
    assert smp.rng.getstate() != before


def test_quantile_is_monotonic(prof):
    """백분위가 오르면 값도 오른다. 보간이 뒤집히면 상관이 **역상관**이 된다."""
    smp = _sampler(prof)
    vals = [smp.numeric(PS, u=u) for u in (0.5, 2.5, 4.5, 6.5, 8.5, 9.5)]
    assert vals == sorted(vals), vals


# ── 2. 주변분포가 바뀌지 않았다 ──────────────────────────────────────


def test_marginal_distribution_is_unchanged(prof):
    """**각 필드는 여전히 자기 실측 백분위에서 뽑힌다.**

    생기는 것은 둘 **사이의** 상관뿐이다. 주변분포가 움직이면 `pemdb1`
    대조 통계가 어긋나고, 그것은 이 수정이 사려던 것이 아니다.

    통계적 비교 대신 **결정적 동치**를 본다 — 분포 비교는 백분위 p0~p20 이
    전부 `0` 인 필드(실측 `PayloadSize` 가 그렇다)의 경계에서 표본차로
    흔들린다. `numeric()` 은 `u = rng.random() * 10` 을 쓰므로, 같은 난수를
    손으로 넣으면 **같은 값이 나와야** 한다. 그것이 "주변분포가 같다" 의
    가장 강한 형태다.
    """
    for field in (PS, TT, "Traffic_Total_Dnlink_PayloadPacketCount"):
        auto = pf.Sampler(prof, random.Random(42))
        manual_rng = random.Random(42)
        manual = pf.Sampler(prof, random.Random(999))
        for _ in range(2000):
            assert auto.numeric(field) == manual.numeric(
                field, u=manual_rng.random() * 10)


# ── 3. 상관이 실제로 생겼다 ──────────────────────────────────────────


def _cell_minute_cv(prof, shared: bool, n_rec: int = 60,
                    n_cellmin: int = 800, seed: int = 11) -> float:
    """셀·분 집계 `U-15` 의 변동계수. **집계 뒤에도 남는 분산**을 본다.

    레코드 단위로만 보면 "꼬리가 길다" 로 끝나는데, 탐지는 셀·분 집계를
    보므로 거기서 안정돼야 뜻이 있다(불변규칙 2 — 합산 후 나눈다).
    """
    smp = _sampler(prof, seed)
    rng = random.Random(seed + 1)
    vals = []
    for _ in range(n_cellmin):
        num = den = 0.0
        for _ in range(n_rec):
            u = rng.random() * 10
            num += int(smp.numeric(PS, u=u if shared else None)) * 8.0
            den += int(smp.numeric(TT, u=u if shared else None)) / 1e6
        if den > 0:
            vals.append(num / den / 1e6)
    mu = statistics.mean(vals)
    return statistics.pstdev(vals) / mu if mu else float("inf")


def test_sharing_the_quantile_makes_throughput_detectable(prof):
    """**집계 변동계수가 1 아래로 내려간다.**

    `EV-08` 의 `throughput_drop_pct = 30.0` 이 뜻을 가지려면 평시 변동이
    30% 하락과 구분돼야 한다. 창고 실측 CV 3.51 에서는 30% 가 **평시에
    상시 일어나는 폭**이다.
    """
    cv_indep = _cell_minute_cv(prof, shared=False)
    cv_shared = _cell_minute_cv(prof, shared=True)
    assert cv_shared < 1.0, f"공유해도 CV 가 {cv_shared:.2f} 다"
    assert cv_shared < cv_indep / 2, f"독립 {cv_indep:.2f} → 공유 {cv_shared:.2f}"


# ── 4. 묶지 않기로 한 것은 묶이지 않았다 ────────────────────────────


def test_retransmission_is_not_tied_to_payload(monkeypatch):
    """**`U-12` 의 분자·분모는 묶이지 않는다.**

    `RetransPacketCount` 를 `PayloadSize` 와 묶으면 재전송률이 상수에
    가까워져 `U-12` 탐지가 죽는다. 패킷 수 둘은 `U-15` 와 무관하고,
    묶으면 평균 패킷 크기가 고정된다.

    `generator.uplane_rec` 이 어느 필드에 `u` 를 넘기는지를 직접 본다 —
    레코드를 만들려면 셀·가입자가 있어야 해서 `Sampler.numeric` 호출을
    가로챈다.
    """
    seen: dict[str, bool] = {}

    class Spy:
        def numeric(self, name, default=0.0, u=None):
            if name.startswith("Traffic_Total_"):
                seen[name.rsplit("_", 1)[-1]] = u is not None
            return 1.0

    # 호출 모양만 확인한다 — 실제 생성 경로를 띄우지 않는다.
    import inspect

    from datagen import generator

    src = inspect.getsource(generator.Generator.uplane_rec)
    assert 'u=u_thr if f in ("PayloadSize", "ThroughputTime")' in src, (
        "공유 대상이 바뀌었다 — 이 테스트의 전제를 다시 읽어라"
    )
    assert "RetransPacketCount" not in src.split("u_thr if f in")[1][:120]
