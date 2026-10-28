"""지역축 — **KR 동작이 한 비트도 달라지면 안 된다.**

사용자 확정(2026-10-28): *"현재 코드는 한국 기본 코드야. 이 내용을 기준으로
삼아. 변경되는 내용은 일본에 추가되는 내용이 함께 포함되어 있어. 2개가 동시에
만족해야 하는 부분이야."*

그래서 이 파일의 단언은 **거의 전부 쌍**이다 — 같은 것을 KR·JP 두 값으로
돌린다(`test_plmn.py` 가 세운 방식). 한쪽만 통과하는 변경은 전제를 깬 것이다.

**DB 와 설정 파일 쓰기를 하지 않는다**(집안 규칙). `region` 은 순수 함수라
dict 만으로 전부 확인되고, `datagen.yaml` 은 **읽기만** 한다.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from datagen import region
from datagen.plmn import Plmn

HERE = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------
# 규칙 ① — 기본은 KR 이고 현행 동작 그대로다
# --------------------------------------------------------------------------


def test_default_is_kr_and_matches_today():
    """**설정을 안 고친 환경이 바뀌면 안 된다.**

    기존 `g-sc` 25종 채점이 이 값 위에 서 있다 — `test_plmn.py` 가 고정한
    `("45005", "4500503", "2340500")` 과 같아야 한다.
    """
    assert region.DEFAULT == "kr"
    cfg = region.derive({})
    p = Plmn.from_cfg(cfg["plmn"])
    assert (p.home, p.imsi_prefix, p.roaming) == ("45005", "4500503", "2340500")
    assert cfg["pemdb1"]["locale"] == "kr"


def test_both_locales_make_a_valid_plmn():
    """**둘 다 성립한다** — 한쪽만 되면 "동시에 만족" 이 아니다.

    `Plmn.from_cfg` 는 자리수·로밍 MCC 를 로드 시점에 검증한다. 지역 기본값이
    그 검증을 통과하지 못하면 **설정을 고른 순간이 아니라 가입자 풀을 만드는
    한참 뒤에** 터진다.
    """
    for loc in region.LOCALES:
        p = Plmn.from_cfg(region.derive({"locale": loc})["plmn"])
        assert len(p.imsi_prefix) == 7 and p.imsi_prefix.isdigit()
        assert p.imsi_prefix[:5] == p.home
        # 로밍은 **다른 나라 MCC** 여야 한다 — 같으면 로밍이 조용히 0이 된다
        assert p.roaming[:3] != p.home[:3], loc


# --------------------------------------------------------------------------
# 규칙 ② — 명시값이 유도값을 이긴다
# --------------------------------------------------------------------------


def test_explicit_value_wins():
    """SoftBank·KDDI 로 돌릴 길을 막지 않는다."""
    cfg = region.derive({"locale": "jp", "plmn": {"home": "44020"}})
    assert cfg["plmn"]["home"] == "44020"          # 적은 쪽
    assert cfg["plmn"]["imsi_msin_head"] == "01"   # 안 적은 쪽은 유도


def test_reapply_keeps_what_a_person_wrote():
    """**UI 로 지역을 바꿔도 사람이 적은 칸은 그대로다.**

    이것이 지금 저장소의 상태이기도 하다 — `locale: jp` 인데 `plmn` 이
    한국이다. 혼란스러워 보이지만 **마스터만 먼저 옮기는 중**일 수 있어
    막지 않는다. 막는 대신 `conflicts()` 가 말한다.
    """
    cfg = region.derive({"locale": "kr",
                         "plmn": {"home": "45005", "imsi_msin_head": "03",
                                  "roaming": "2340500"}})
    region.reapply(cfg, "jp")
    assert cfg["locale"] == "jp"
    assert cfg["pemdb1"]["locale"] == "jp"         # 유도값은 따라간다
    assert cfg["plmn"]["home"] == "45005"          # 명시값은 남는다
    assert [c["path"] for c in region.conflicts(cfg)] == [
        "plmn.home", "plmn.imsi_msin_head", "plmn.roaming"]


def test_reapply_moves_everything_when_nothing_was_written():
    """아무것도 안 적혀 있으면 **전부 따라간다** — 이것이 의도한 기본 경로."""
    cfg = region.derive({"locale": "kr"})
    region.reapply(cfg, "jp")
    assert cfg["plmn"] == region.LOCALES["jp"]["plmn"]
    assert cfg["pemdb1"]["locale"] == "jp"
    assert region.conflicts(cfg) == []
    # 왕복해도 제자리로 — 한 번 바꾸면 못 돌아오는 변환이면 안 된다
    region.reapply(cfg, "kr")
    assert cfg["plmn"] == region.LOCALES["kr"]["plmn"]
    assert region.conflicts(cfg) == []


# --------------------------------------------------------------------------
# 규칙 ③ — 어긋나면 말한다. 멈추지는 않는다
# --------------------------------------------------------------------------


def test_conflicts_are_reported_not_raised():
    """**예외를 던지지 않는다.** 그 조합이 유효한 경우가 있다."""
    cfg = region.derive({"locale": "jp", "plmn": {"home": "45005"}})
    rows = region.conflicts(cfg)
    assert rows and rows[0]["path"] == "plmn.home"
    assert rows[0]["configured"] == "45005" and rows[0]["expected"] == "44010"
    text = region.conflict_text(cfg)
    assert "45005" in text and "44010" in text


def test_unknown_locale_stops_at_load_time():
    """오타를 조용히 `kr` 로 떨어뜨리면 **한국 데이터가 나오는데 사람은
    일본을 켠 줄 안다.** 생성이 다 끝난 뒤 IMSI 를 보고서야 알게 된다."""
    for bad in ("jpn", "ko", "日本", "kr2", "jp-tokyo"):
        with pytest.raises(ValueError):
            region.resolve({"locale": bad})
    with pytest.raises(ValueError):
        region.reapply(region.derive({}), "jpn")


def test_whitespace_and_case_are_tolerated():
    """`"JP "` 는 받는다 — **오타가 아니라 입력 잡음이다.**

    사람이 yaml 에 적고 UI 가 보내고 env 가 넘기는 값이라 공백·대문자가
    섞인다. 그것까지 거부하면 "맞게 적었는데 안 된다" 가 되고, 거부해서
    막을 사고가 없다(뜻이 하나로만 읽힌다).
    """
    for raw in ("JP", "jp ", " Jp", "JP\t"):
        assert region.resolve({"locale": raw}) == "jp"


def test_empty_means_not_set():
    """**빈 값은 "설정하지 않음" 이다** — 이 저장소 쌍의 공통 규칙이다.

    compose 가 `${GP_...:-}` 로 넘기면 호스트에 변수가 없을 때 빈 문자열이
    들어온다. 그것으로 멀쩡한 값을 덮거나 거부하면 **원인이 설정 파일처럼
    보인다**(루트 `CLAUDE.md` 의 `.env` 절과 같은 판단).
    """
    assert region.resolve({}) == region.DEFAULT
    assert region.resolve({"locale": ""}) == region.DEFAULT
    assert region.resolve({"locale": None}) == region.DEFAULT


# --------------------------------------------------------------------------
# 묶임 — 화면·설정이 자기 목록을 들지 않는다
# --------------------------------------------------------------------------


def test_ui_list_comes_from_the_registry():
    """UI 콤보박스 목록의 정본은 `LOCALES` 다.

    화면이 자기 목록을 들면 지역이 늘 때 한쪽만 고쳐지고, 그때 "선택지가
    없다"가 아니라 **선택해도 안 먹는다**가 된다(`BLOCKABLE_SERIES` 와 같은
    처리). 그래서 `ui.py` 에 지역 이름이 박혀 있지 않은 것을 함께 센다.
    """
    assert [n["value"] for n in region.names()] == list(region.LOCALES)
    src = (HERE / "src" / "datagen" / "ui.py").read_text(encoding="utf-8")
    for lit in ('"44010"', '"45005"', "'44010'", "'45005'"):
        assert lit not in src, f"UI 에 PLMN 이 박혀 있다: {lit}"


def test_config_file_has_the_switch():
    """`datagen.yaml` 최상위에 `locale:` 이 있다 — 축의 자리다.

    **`pemdb1.locale` 이 아니다.** 거기 두면 "마스터 테이블 선택"으로 읽혀
    PLMN 이 따라와야 한다는 것이 드러나지 않는다.
    """
    doc = yaml.safe_load(
        (HERE / "config" / "datagen.yaml").read_text(encoding="utf-8"))
    assert doc.get("locale") in region.LOCALES


def test_load_cfg_derives_without_touching_call_sites():
    """`load_cfg` 가 유도하므로 **호출부를 고치지 않아도 된다.**

    `Plmn.from_cfg(cfg.get("plmn"))` 과 `pem.get("locale")` 이 전부 `cfg` 를
    거친다 — 그 묶임이 이 설계의 전부다. 깨지면 지역을 바꿔도 한쪽만 따라온다.
    """
    from datagen.cli import load_cfg
    cfg = load_cfg(None)
    assert cfg.get("plmn", {}).get("home")        # 비어 있지 않다
    assert cfg.get("pemdb1", {}).get("locale") in region.LOCALES
    assert region.DERIVED_KEY in cfg              # 무엇을 채웠는지 남는다
