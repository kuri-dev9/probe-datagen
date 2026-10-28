"""지역축 — **`locale` 한 칸이 나머지를 먹인다.**

## 왜 이 모듈인가

지역에 따라 갈리는 설정이 **넷으로 흩어져 있었다**(실측 2026-10-28) —

    pemdb1.locale          마스터 테이블·셀 좌표     jp
    plmn.home/…            PLMN · IMSI 접두          45005  (한국)
    graphio geo.locale     행정경계 GeoJSON          kr
    pemdb1.locale_database 스키마 복제 (폐기 대기)   pemdb6_jp

그래서 저장소 쌍이 **서로 모순된 상태로 돌고 있었다** — 셀은 도쿄인데 가입자
IMSI 는 `450…` 이고 지도는 한국 번들이다. **에러가 나지 않는다.**
`identity.py` 머리말이 예고한 *"IMSI 앞 7자로 사업자·로밍을 가른다"* 분석을
넣는 순간 **전 가입자가 외국 로머**가 되고, 도쿄 셀은 한국 시군구에 못 붙어
전부 `UNMAPPED` 로 간다.

사람이 네 곳을 맞춰야 하는 구조에서는 **반드시 한 곳이 빠진다.** 비밀을
`.env` 한 곳으로 모은 것과 같은 종류의 문제이고, 그때 적은 문장이 그대로
걸린다 — *"한 곳을 빠뜨리면 에러 없이 gen 과 corr 이 다른 창고를 본다."*

배경과 전수 조사는
`graphio-probe-rca/docs/jp/지역축-KR-JP.md` 다.

## 규칙 셋

**① 기본은 `kr` 이다.** 현행 한국 코드가 기준선이고(사용자 확정 2026-10-28),
설정을 안 고친 환경의 동작이 바뀌면 안 된다. 기존 `g-sc` 25종 채점이 그 위에
서 있다.

**② 명시값이 유도값을 이긴다.** `plmn.home` 을 적으면 그것이 쓰인다 —
SoftBank(`44020`)·KDDI(`44050`)로 돌릴 길을 막지 않는다. 안 적으면
`locale` 기본값이다.

**③ 어긋나면 말한다. 멈추지는 않는다.** ②의 결과로 `locale: jp` 인데
`plmn` 이 한국일 수 있다 — 그것이 지금 상태다. **그 조합이 유효한 경우가
있으므로**(로밍 테스트 · 마스터만 먼저 전환) 막지 않고, 대신
`conflicts()` 가 **무엇이 무엇과 어긋났는지** 돌려준다. 호출부가 로그와
화면에 싣는다.

## 운영 중에 바뀌지 않는다

사용자 확정(2026-10-28): *"운영 도중에 변경하는 일은 없어. 한번 바꾸면
고정이고, 재시작 이후 반영한다."*

그래서 `CM_DATAGEN_CONFIG` 의 다른 키들과 **성격이 다르다** — 저쪽은 데몬이
틱마다 다시 읽어 다음 분부터 반영되지만(`volume_pct`·`blocked`…), 지역축은
**기동 시 한 번** 읽는다. 바꾸는 중간에 섞이면 같은 분의 Parquet 에 한국
IMSI 와 일본 IMSI 가 함께 들어가고, 그 분은 **어느 쪽으로도 집계되지
않는다.** UI 가 "재시작해야 반영된다"를 적는 이유다.
"""

from __future__ import annotations

from typing import Any

#: 지역축 기본값. **`kr` 이다** — 규칙 ①.
DEFAULT = "kr"

#: 지역별 유도값. **여기가 정본이고 `datagen.yaml` 은 덮어쓰기다.**
#:
#: `roaming` 은 **자국이 아닌 MCC** 여야 한다(`Plmn.from_cfg` 가 거부한다) —
#: 같으면 로밍 가지가 홈 가입자까지 통과시켜 **로밍이 조용히 0이 된다.**
#: 그래서 KR 은 영국(`234`), JP 는 한국(`450`)을 쓴다. 일본 망에 한국
#: 로머가 있는 것이 맞고, 프로파일의 로밍 비율(2.6%)이 보존된다.
LOCALES: dict[str, dict[str, Any]] = {
    "kr": {
        "label": "한국 (KR)",
        # 현행 동작 그대로다 — 한 비트도 달라지면 안 된다(규칙 ①)
        "plmn": {"home": "45005", "imsi_msin_head": "03",
                 "roaming": "2340500"},
        "pemdb1_locale": "kr",
        # graphio 쪽 `geo.locale`. **유도하지 않는다**(저장소가 다르다) —
        # 대조용으로만 들고 있다. §L-2
        "geo_locale": "kr",
    },
    "jp": {
        "label": "日本 (JP) — docomo",
        # docomo `44010` · MNC 2자리라 IMSI 15자리 구성이 한국과 같다.
        # SoftBank `44020` · KDDI `44050` 으로 돌리려면 `plmn.home` 을
        # 명시한다(규칙 ②).
        "plmn": {"home": "44010", "imsi_msin_head": "01",
                 "roaming": "4500503"},
        "pemdb1_locale": "jp",
        "geo_locale": "jp",
    },
}

#: `conflicts()` 가 검사하는 자리. (설명, 설정 경로, 유도 키)
_CHECKS: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("PLMN",        ("plmn", "home"),           "plmn.home"),
    ("IMSI 접두",   ("plmn", "imsi_msin_head"), "plmn.imsi_msin_head"),
    ("로밍 MCC",    ("plmn", "roaming"),        "plmn.roaming"),
    ("마스터 테이블", ("pemdb1", "locale"),     "pemdb1_locale"),
)


def names() -> list[dict[str, str]]:
    """UI 콤보박스가 쓸 목록. **화면이 자기 목록을 들지 않는다.**

    들면 지역이 늘 때 한쪽만 고쳐지고, 그때 "선택지가 없다"가 아니라
    **선택해도 안 먹는다**가 된다(`BLOCKABLE_SERIES` 와 같은 처리).
    """
    return [{"value": k, "label": str(v["label"])} for k, v in LOCALES.items()]


def resolve(cfg: dict | None) -> str:
    """설정 → 유효 지역. 모르는 값이면 **멈춘다.**

    오타(`jpn`·`JP ` )를 조용히 `kr` 로 떨어뜨리면 **한국 데이터가 나오는데
    사람은 일본을 켠 줄 안다.** 생성이 다 끝난 뒤 IMSI 를 보고서야 알게 된다.
    """
    raw = str((cfg or {}).get("locale", DEFAULT) or DEFAULT).strip().lower()
    if raw not in LOCALES:
        raise ValueError(
            f"모르는 locale: {raw!r} (있는 것: {', '.join(LOCALES)})\n"
            "  지역축은 `datagen.yaml` 최상위 `locale:` 한 칸이다.")
    return raw


def _get(cfg: dict, path: tuple[str, ...]) -> Any:
    cur: Any = cfg
    for p in path:
        if not isinstance(cur, dict) or p not in cur:
            return None
        cur = cur[p]
    return cur


def _derived(locale: str, key: str) -> Any:
    """`_CHECKS` 의 유도 키 → 그 지역의 값."""
    spec = LOCALES[locale]
    if key.startswith("plmn."):
        return spec["plmn"][key.split(".", 1)[1]]
    return spec[key]


def conflicts(cfg: dict | None) -> list[dict[str, str]]:
    """**명시값이 지역 기본과 어긋난 자리.** 규칙 ③ — 멈추지 않는다.

    빈 리스트면 설정이 한 지역으로 일관된다는 뜻이다.
    """
    cfg = cfg or {}
    loc = resolve(cfg)
    out: list[dict[str, str]] = []
    for what, path, dkey in _CHECKS:
        got = _get(cfg, path)
        if got is None:
            continue                      # 안 적었다 — 유도값이 쓰인다
        want = _derived(loc, dkey)
        if str(got) != str(want):
            out.append({
                "what": what,
                "path": ".".join(path),
                "configured": str(got),
                "expected": str(want),
                "locale": loc,
            })
    return out


def conflict_text(cfg: dict | None) -> str:
    """사람이 읽을 한 덩어리. 어긋난 것이 없으면 빈 문자열."""
    rows = conflicts(cfg)
    if not rows:
        return ""
    loc = rows[0]["locale"]
    lines = [f"⚠ 지역축이 어긋나 있다 — locale: {loc} 인데 아래가 다르다."]
    for r in rows:
        lines.append(f"    {r['what']:<12} {r['path']} = {r['configured']}"
                     f"   (locale {loc} 이면 {r['expected']})")
    lines.append("  **에러가 아니다** — 명시값이 이긴다(규칙 ②). 마스터만 먼저")
    lines.append("  옮기는 중이라면 의도된 상태다. 아니라면 그 칸을 지워라 —")
    lines.append("  지우면 locale 에서 유도된다.")
    return "\n".join(lines)


#: `derive` 가 **채운** 키를 cfg 에 남기는 자리. UI 가 지역을 바꿀 때
#: 이 목록만 교체한다 — yaml 에 사람이 적은 값은 건드리지 않는다(규칙 ②).
#: 밑줄로 시작해 설정 키와 섞이지 않는다.
DERIVED_KEY = "_region_derived"


def derive(cfg: dict, locale: str | None = None) -> dict:
    """`locale` 에서 유도해 **빈 칸만** 채운다. 적힌 칸은 건드리지 않는다.

    `cli.load_cfg` 가 마지막에 부른다. 그래서 `Plmn.from_cfg(cfg["plmn"])`
    과 `pem.get("locale")` 같은 **호출부를 하나도 고치지 않아도** 지역축이
    따라온다 — 그 자리들이 전부 `cfg` 를 거치기 때문이다.

    ``locale`` 을 주면 설정의 `locale:` 대신 그것을 쓴다(UI 저장분 반영).

    **제자리에서 고친다**(`load_cfg` 가 만든 dict 다). 사본을 돌려주면
    `_apply_env` 와 호출 순서가 얽혔을 때 한쪽만 반영된다.

    채운 키를 `cfg[DERIVED_KEY]` 에 남긴다 — `reapply` 가 **그것만**
    바꾸기 위해서다. 남기지 않으면 지역을 바꿀 때 사람이 적은 값과
    유도된 값을 구분할 방법이 없다.
    """
    loc = locale if locale is not None else resolve(cfg)
    if loc not in LOCALES:
        raise ValueError(f"모르는 locale: {loc!r} (있는 것: {', '.join(LOCALES)})")
    spec = LOCALES[loc]
    filled: list[str] = []
    plmn = cfg.setdefault("plmn", {})
    if isinstance(plmn, dict):
        for k, v in spec["plmn"].items():
            if k not in plmn:
                plmn[k] = v
                filled.append(f"plmn.{k}")
    pem = cfg.setdefault("pemdb1", {})
    if isinstance(pem, dict) and "locale" not in pem:
        pem["locale"] = spec["pemdb1_locale"]
        filled.append("pemdb1.locale")
    cfg[DERIVED_KEY] = filled
    return cfg


def reapply(cfg: dict, locale: str) -> dict:
    """**지역을 바꿔 다시 유도한다** — UI 저장분을 기동 시 반영하는 경로.

    `derive` 가 채웠던 칸만 새 지역 값으로 바꾸고, **사람이 yaml 에 적은
    칸은 그대로 둔다**(규칙 ②). 그래서 `plmn.home` 이 명시돼 있으면 UI 로
    지역을 바꿔도 PLMN 은 그대로다 — **그것이 의도다.** 혼란을 막는 것은
    `conflicts()` 와 화면 경고의 몫이다.

    `cfg["locale"]` 도 함께 바꾼다. 안 바꾸면 그 뒤의 `resolve(cfg)` 가
    옛 값을 돌려줘 **화면과 실제가 갈린다.**
    """
    if locale not in LOCALES:
        raise ValueError(f"모르는 locale: {locale!r} (있는 것: {', '.join(LOCALES)})")
    for key in list(cfg.get(DERIVED_KEY) or ()):
        sec, _, name = key.partition(".")
        node = cfg.get(sec)
        if isinstance(node, dict):
            node.pop(name, None)
    cfg["locale"] = locale
    return derive(cfg, locale)
