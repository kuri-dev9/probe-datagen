"""프로파일 나라별 벌 (2026-10-30 · C-4) — 한 파일에 kr·jp, `locale` 로 고른다."""
from __future__ import annotations

import json

import pytest

from datagen import profile as pf

_SERIES = {"records": 1, "categorical": {}, "numeric": {}, "extras": {}}


def _write(tmp_path, payload) -> object:
    p = tmp_path / "profile.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


def test_locale_picks_its_own_set(tmp_path):
    p = _write(tmp_path, {
        "kr": {"http": {**_SERIES, "domains": ["naver.com"]}},
        "jp": {"http": {**_SERIES, "domains": ["yahoo.co.jp"]}},
    })
    assert pf.load(p, "kr")["http"].domains == ["naver.com"]
    assert pf.load(p, "jp")["http"].domains == ["yahoo.co.jp"]
    assert pf.load(p)["http"].domains == ["naver.com"]     # 기본은 kr


def test_old_flat_file_is_korean_and_refuses_jp(tmp_path):
    """옛 파일을 일본판으로 조용히 쓰면 일본 생성에 한국 도메인이 실린다."""
    p = _write(tmp_path, {"http": {**_SERIES, "domains": ["naver.com"]}})
    assert pf.load(p, "kr")["http"].domains == ["naver.com"]
    with pytest.raises(ValueError):
        pf.load(p, "jp")


def test_missing_locale_stops(tmp_path):
    p = _write(tmp_path, {"kr": {"http": {**_SERIES, "domains": ["a.kr"]}}})
    with pytest.raises(ValueError):
        pf.load(p, "jp")


def test_with_domains_keeps_distributions_and_empty_series(tmp_path):
    kr = {"http": pf.SeriesProfile(records=3, categorical={"APN": {"x": 1.0}},
                                   numeric={}, domains=["naver.com"], extras={}),
          "lte_call": pf.SeriesProfile(records=3, categorical={}, numeric={},
                                       domains=[], extras={})}
    jp = pf.with_domains(kr, ["yahoo.co.jp", "line.me"])
    assert jp["http"].domains == ["yahoo.co.jp", "line.me"]
    assert jp["http"].categorical == kr["http"].categorical
    assert jp["lte_call"].domains == []                    # 원래 비었으면 그대로
    back = pf.save_locales({"kr": kr, "jp": jp}, tmp_path / "out.json")
    assert pf.load(back, "jp")["http"].domains == ["yahoo.co.jp", "line.me"]
