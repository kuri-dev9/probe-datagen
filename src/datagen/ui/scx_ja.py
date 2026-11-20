"""시나리오 설명 그림(`scenarios.html` 의 `<template id=scx-NN>`)의 일본어판 — 2026-11-19.

설명 원본은 사용자 확정본(graphio `docs/jp/ui/scenarios-14-27.html` · `scenarios-28-35.html` · 같은 형식의 `scenarios-01-13.html`)을 자른 **한국어 한 벌**
이다. 일본어판을 손으로 한 벌 더 두면 그림·배치를 고칠 때 두 벌이 갈라진다 — 그래서 한국어 템플릿에서
**글자만** 사전(`static/scx_ja.json`, 원문 → 日本語)으로 바꿔 `scx-NN-ja` 를 페이지를 만들 때 굽는다.

번역 단위는 인라인 태그(`b`·`br`·`span`…)만 품은 요소의 안쪽 HTML 과 `aria-label`·`title` 이다 — 조각 글자
단위로 옮기면 「<b>주소 안내 장치(DNS)</b>가」처럼 어순이 깨진다. 사전에 없는 단위는 **한국어로 남는다**(지어
내지 않는다) — `scenario_ja check` 의 `scx` 가 그 수를 센다
"""

from __future__ import annotations

import json
import re
from pathlib import Path

DICT = Path(__file__).parent / "static" / "scx_ja.json"
HANGUL = re.compile(r"[가-힣]")
_INLINE = {"b", "br", "i", "em", "small", "sub", "sup", "span", "strong", "tspan"}
_LEAF = re.compile(
    r'<(p|h1|h2|h3|strong|text|div|span|li|tspan|td|th|b|figcaption|label)(\s[^>]*)?>'
    r'((?:(?!<(?:p|h1|h2|h3|div|li|svg|section|article|text|table|tr|td|th|ul|ol)[\s>/]).)*?)</\1>', re.S)
_ATTR = re.compile(r'((?:aria-label|title)=")([^"]*)(")')
_TPL = re.compile(r'<template id=scx-(\d+)>(.*?)</template>', re.S)


def _is_unit(inner: str) -> bool:
    tags = {t.lower() for t in re.findall(r'</?([a-zA-Z0-9]+)', inner)}
    return not (tags - _INLINE) and bool(HANGUL.search(inner))


def units(body: str) -> list[str]:
    """템플릿 하나의 번역 단위(앞뒤 공백을 뗀 원문) — 사전 키가 이것이다."""
    out = [m.group(3).strip() for m in _LEAF.finditer(body) if _is_unit(m.group(3))]
    out += [m.group(2).strip() for m in _ATTR.finditer(body) if HANGUL.search(m.group(2))]
    return list(dict.fromkeys(out))


def load() -> dict:
    return json.loads(DICT.read_text(encoding="utf-8")) if DICT.exists() else {}


def _ja_body(body: str, ja: dict) -> str:
    def leaf(m):
        inner = m.group(3)
        k = inner.strip()
        if not _is_unit(inner) or k not in ja:
            return m.group(0)
        return m.group(0).replace(inner, inner.replace(k, ja[k], 1), 1) if inner else m.group(0)

    def attr(m):
        k = m.group(2).strip()
        return m.group(1) + ja[k] + m.group(3) if k in ja else m.group(0)

    return _ATTR.sub(attr, _LEAF.sub(leaf, body))


def with_ja(html: str, ja: dict | None = None) -> str:
    """각 `scx-NN` 바로 뒤에 `scx-NN-ja` 를 붙인다. 화면이 `LANG` 으로 고른다(`scenarios.html` `scn-explain`)."""
    ja = load() if ja is None else ja
    return _TPL.sub(lambda m: m.group(0) + f"<template id=scx-{m.group(1)}-ja>{_ja_body(m.group(2), ja)}</template>",
                    html)


def missing(html: str, ja: dict | None = None) -> list[str]:
    """사전에 없는 번역 단위 — 일본어 화면에 한국어로 남는 글."""
    ja = load() if ja is None else ja
    return [u for m in _TPL.finditer(html) for u in units(m.group(2)) if u not in ja]
