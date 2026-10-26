"""홈 PLMN 과 IMSI 접두 — 어긋나면 **전 가입자가 외국 가입자**가 된다.

전에는 세 파일에 흩어진 리터럴이었다 — `pemdb1.py` 의 `plmn="45005"`,
같은 파일의 `"4500503"`/`"2340500"`, `generator.py` 의 `"4500503"` 둘.

    셀의 PLMN        "45005"
    가입자 IMSI 접두  "4500503"  →  앞 5자 "45005"      ← 일치해야 한다

지금은 로밍 판정 로직이 없어 증상이 안 나온다. `identity.py` 머리말이
*"IMSI 앞 7자로 사업자·로밍을 가른다"* 고 적어 둔 분석을 넣는 순간 **전부
로밍으로** 나오고, **그때도 에러는 없다.** 그래서 설정 한 곳에서 유도하고
그 묶임을 여기서 고정한다.

**DB 를 쓰지 않는다** — 셀은 `data/cells.json`(자체 생성 350셀), 프로파일은
`data/profile.json` 이다(집안 규칙 · `tests/test_session_model.py` 머리말).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from datagen.cells import load_cells
from datagen.generator import Generator
from datagen.identity import synth_imsi
from datagen.plmn import Plmn
from datagen.profile import load as load_profiles

HERE = Path(__file__).resolve().parents[1]
DATA = HERE / "data"

#: 일본 docomo. MCC 3 + MNC 2 가 한국과 같은 모양이다.
JP = {"home": "44010", "imsi_msin_head": "01", "roaming": "2340500"}


# --------------------------------------------------------------------------
# 묶임 — **한 값에서 유도한다**
# --------------------------------------------------------------------------


def test_imsi_prefix_always_starts_with_the_home_plmn():
    """`imsi_mcc_mnc_info[:5] == plmn.home` 이 **구조적으로** 성립한다.

    설정에 7자 칸을 따로 두지 않았기 때문이다 — 두 칸이면 한쪽만 고쳐지는
    같은 함정이 설정 파일로 옮겨간다.
    """
    for cfg in ({}, JP, {"home": "44020", "imsi_msin_head": "99"},
                {"home": "44050", "imsi_msin_head": "00"}):
        p = Plmn.from_cfg(cfg)
        assert p.imsi_prefix[:5] == p.home
        assert len(p.imsi_prefix) == 7 and p.imsi_prefix.isdigit()


def test_config_has_no_seven_digit_slot():
    """설정에 `imsi_mcc_mnc_info` 칸이 **없어야** 한다.

    있으면 사람이 그 칸만 고치고 `home` 을 안 고칠 수 있다 — 그게 이
    변경이 없애려던 상태다.
    """
    doc = yaml.safe_load((HERE / "config" / "datagen.yaml").read_text(encoding="utf-8"))
    assert set(doc["plmn"]) == {"home", "imsi_msin_head", "roaming"}
    # 기본값이 지금 동작이어야 한다 — 설정을 안 고친 환경이 바뀌면 안 된다
    p = Plmn.from_cfg(doc["plmn"])
    assert (p.home, p.imsi_prefix, p.roaming) == ("45005", "4500503", "2340500")


def test_bad_width_is_refused_at_load_time():
    """**로드 시점에 멈춘다.**

    `identity.imsi` 가 7자가 아니면 `ValueError` 를 던지는데 그것은 가입자
    풀을 만드는 한참 뒤다 — 거기서 터지면 "생성이 깨졌다"로 읽혀 설정을
    의심하지 않는다.
    """
    for bad in ({"home": "450"}, {"home": "4500"}, {"home": "45005a"},
                {"imsi_msin_head": "3"}, {"imsi_msin_head": "003"},
                {"roaming": "234050"}, {"roaming": "23405000"},
                {"home": ""}, {"roaming": "abcdefg"}):
        with pytest.raises(ValueError):
            Plmn.from_cfg(bad)


def test_roaming_must_be_a_foreign_mcc():
    """로밍 MCC 가 홈과 같으면 **로밍이 조용히 0이 된다.**

    `normalize` 의 로밍 가지(`v[:3] == roaming[:3]`)가 홈 가입자까지
    통과시키고, `_reseat_subscribers` 의 2.6% 도 홈과 구분되지 않는다.
    **자리수만 보면 이것을 못 잡는다** — 7자이고 숫자이기 때문이다.
    """
    with pytest.raises(ValueError, match="MCC"):
        Plmn.from_cfg({"home": "45005", "roaming": "4500599"})
    with pytest.raises(ValueError, match="MCC"):
        Plmn.from_cfg({"home": "44010", "imsi_msin_head": "01",
                       "roaming": "4402099"})       # 같은 일본 MCC
    # 다른 나라 MCC 는 통과한다 — 한국 망의 영국 로머 · 일본 망의 한국 로머
    assert Plmn.from_cfg({"home": "44010", "imsi_msin_head": "01",
                          "roaming": "4500503"}).roaming == "4500503"
    assert Plmn.from_cfg({}).roaming == "2340500"


def test_japanese_values_make_a_15_digit_imsi():
    """일본 값으로 IMSI 15자리가 나온다 — 자리수 구성이 그대로다.

    MCC 3 + MNC 2 + MSIN 머리 2 = 7자이고 뒤에 MSIN 8자리가 붙는다.
    `identity.imsi` 가 7자가 아니면 거부한다.
    """
    p = Plmn.from_cfg(JP)
    for i in (0, 1, 999, 19_999):
        imsi = synth_imsi(p.imsi_prefix, i)
        assert len(imsi) == 15 and imsi.isdigit()
        assert imsi.startswith("4401001")
        assert imsi[:5] == p.home
    # 한국도 그대로
    assert synth_imsi(Plmn().imsi_prefix, 7).startswith("4500503")


# --------------------------------------------------------------------------
# 프로파일이 설정을 덮지 않는다
# --------------------------------------------------------------------------


def test_profile_has_korean_values_so_config_must_win():
    """**이 변경의 성패가 걸린 자리다.**

    `data/profile.json` 의 `imsi_mcc_mnc_info` 분포는 상용 실측이라 전부
    한국 값이다(`4500502`~`4500513` · `4501212` · `234xxxx`). 그대로 쓰면
    설정을 `44010` 으로 바꿔도 가입자 IMSI 가 `450` 으로 나오고 **에러가
    없다.** 먼저 그 전제(프로파일에 한국 값이 있다)를 고정한다.
    """
    profiles = load_profiles(DATA / "profile.json")
    cat = profiles["lte_call"].categorical["imsi_mcc_mnc_info"]
    assert cat, "프로파일에 그 필드가 없다 — 이 테스트의 전제가 바뀌었다"
    assert any(str(k).startswith("450") for k in cat)


def test_normalize_moves_the_prefix_but_keeps_the_bucket():
    """분포 **모양은 유지**하고 앞 5자만 바꾼다.

    끝 2자는 사업자 하위 버킷이다 — 버리면 가입자가 한 접두에 몰려 UE·
    사업자 축의 다양성이 사라진다. 로밍(MCC 3자가 같은 것)은 **손대지
    않는다** — 일본 망에 외국 로머가 있는 것이 맞다.
    """
    p = Plmn.from_cfg(JP)
    assert p.normalize("4500509") == "4401009"      # 버킷 09 유지
    assert p.normalize("4501212") == "4401012"
    assert p.normalize("2340500") == "2340500"      # 로밍 — 그대로
    # 7자가 아닌 값(프로파일에 `""` 1.2% · `"__other__"` 0.6%)은 대표값으로
    for junk in ("", "__other__", None, "450", "45005031"):
        assert p.normalize(junk) == p.imsi_prefix
    # 한국 기본값에서는 아무것도 바뀌지 않는다 — 현 동작 보존
    kr = Plmn()
    assert kr.normalize("4500509") == "4500509"


def _gen(plmn: Plmn | None) -> Generator:
    cells = load_cells(DATA / "cells.json")
    profiles = load_profiles(DATA / "profile.json")
    keep = {s: profiles[s] for s in ("lte_call", "transport", "dns", "http")}
    return Generator(cells=cells, profiles=keep, scenarios=[],
                     rates={"lte_call": 2000, "transport": 2200,
                            "dns": 900, "http": 1500},
                     seed=20260903, diurnal=True, subscriber_pool=500,
                     success_only=True, end_only=True, plmn=plmn)


def test_generated_subscribers_follow_the_config_not_the_profile():
    """**실제 가입자 풀**이 설정을 따른다 — 프로파일이 이기지 않는다.

    `Generator._make_subscribers` 가 프로파일에서 뽑은 값을
    `Plmn.normalize` 로 옮긴다. 그리고 IMSI 는 그 값에서 파생되므로
    **두 컬럼이 어긋날 수 없다**(규격: `imsi_mcc_mnc_info` = IMSI 앞 7자).
    """
    p = Plmn.from_cfg(JP)
    subs = _gen(p).subs
    assert subs
    home = [s for s in subs if not s.mcc_mnc.startswith(p.roaming[:3])]
    assert home, "홈 가입자가 하나도 없다"
    for s in home:
        assert s.mcc_mnc.startswith(p.home), s.mcc_mnc
        assert s.imsi[:7] == s.mcc_mnc          # 규격이 선언한 관계
        assert len(s.imsi) == 15
    # 버킷이 하나로 몰리지 않았다 — 분포 모양을 유지한 결과
    assert len({s.mcc_mnc for s in home}) > 1
    # 한국 기본값에서는 전과 같다
    kr = [s for s in _gen(None).subs if not s.mcc_mnc.startswith("234")]
    assert all(s.mcc_mnc.startswith("45005") for s in kr)


def test_dead_plmn_pool_is_still_dead():
    """`cells.PLMN_POOL`·`PLMN_WEIGHTS` 는 **호출부가 0건**이다.

    지우지 않고 죽었다고 적어 두었다(`geo.py` 의 `SIDO_POPULATION` 과 같은
    처리). 되살아나면 PLMN 의 출처가 둘이 되고, 그때 설정과 어긋나도
    에러가 나지 않는다 — 그래서 **죽어 있는 것을 테스트가 지킨다.**
    """
    import re

    from datagen import cells as cells_mod

    assert cells_mod.PLMN_POOL and cells_mod.PLMN_WEIGHTS
    hits = []
    for path in (HERE / "src" / "datagen").glob("*.py"):
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"\bPLMN_(POOL|WEIGHTS)\b", line) and not line.lstrip().startswith("#"):
                hits.append(f"{path.name}:{i}")
    # 정의 두 줄만 있어야 한다
    assert sorted(hits) == ["cells.py:31", "cells.py:32"] or len(hits) == 2, hits


def test_cell_plmn_comes_from_the_config():
    """셀의 PLMN 도 설정에서 온다 — 리터럴이면 **가입자와 어긋난다.**

    `load_cells` 가 전 셀에 같은 값을 넣는다(`CL_CELL_MAP` 에 그 컬럼이
    없다). DB 없이 `FakeConn` 으로 돌린다(집안 규칙).
    """
    from datagen import pemdb1 as P
    from tests.test_topo import _LoadConn, _join_rows

    for cfg, want in (({}, "45005"), (JP, "44010"), ({"home": "44050"}, "44050")):
        conn = _LoadConn(_join_rows(), sectors={"811110": [0, 1]})
        got = P.load_cells(conn, enb_limit=0, plmn=P.Plmn.from_cfg(cfg))
        assert got and {c.plmn for c in got} == {want}, cfg


def test_no_plmn_literal_is_left_in_the_generation_path():
    """리터럴이 **코드에 남아 있지 않다.**

    `_reseat_subscribers` 는 `PemdbGen` 메서드라 DB 없이 돌리기 어렵다 —
    그 자리를 소스로 센다. 주석·머리말은 이력으로 그 값을 적고 있으므로
    **대입/비교 꼴만** 센다(`geo` 쪽 `* 88.0` 테스트와 같은 방식).
    """
    src = (HERE / "src" / "datagen" / "pemdb1.py").read_text(encoding="utf-8")
    code = [ln for ln in src.splitlines()
            if not ln.lstrip().startswith(("#", "#:"))]
    # **대입 꼴만 세지 않는다** — 변이로 `("2340500" if … else "4500503")`
    # 를 넣었을 때 `= "…"` 패턴을 비껴가 통과했다. 코드 줄에 그 숫자가
    # 있으면 그 자체가 사본이다(실측: 지금 0건).
    for lit in ("4500503", "2340500", "45005"):
        bad = [ln.strip()[:90] for ln in code if lit in ln]
        assert not bad, (lit, bad)
    # `generator.py` 의 폴백 둘도 사라졌다
    gsrc = (HERE / "src" / "datagen" / "generator.py").read_text(encoding="utf-8")
    gcode = [ln for ln in gsrc.splitlines() if not ln.lstrip().startswith("#")]
    assert not [ln for ln in gcode if '"4500503"' in ln], "generator 폴백이 남았다"
