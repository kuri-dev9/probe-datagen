"""시나리오 현황(`/scenarios`) API — 생성량별 결과 자료와 L3 문답 기록."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter

from .common import HERE


router = APIRouter()



#: 시나리오 현황 자료 — 판정이 끝날 때마다 사람이 고친다. 로컬은 `config` 마운트라 바로 보이고,
#: 운영 compose 는 설정 파일 하나만 걸므로 이미지를 구운 시점의 파일을 본다
SCENARIO_REPORT = HERE / "config" / "scenario_report.json"
#: 시나리오를 만드는 방법 — 코드가 바뀔 때만 고친다. 판정마다 고치는 위 파일과 주기가 달라 나눴다
SCENARIO_HOW = HERE / "config" / "scenario_how.json"
#: 결과 자료의 일본어 — 칸마다 원문 해시를 함께 둔다(`scenario_ja`). 용어 뜻 — 코드 옆에 붙는 쉬운 말
SCENARIO_REPORT_JA = HERE / "config" / "scenario_report_ja.json"
SCENARIO_GLOSSARY = HERE / "config" / "scenario_glossary.json"
#: 생성량별 결과 — 15% 가 정본(위 두 파일)이고 50% 는 따로 쌓는다(사용자 11-10). 시나리오 설정·판정 기준은
#: 같고 판정만 다르다. 권장 길이·L3 기록·만드는 방법·용어는 생성량과 무관해 함께 쓴다.
#: 15% 는 `None` — 부르는 시점의 위 두 이름을 읽는다. 경로를 여기 복사하면 그 이름을 바꿔도(시험의
#: monkeypatch) 표가 옛 경로를 들고 있다
SCENARIO_VOLUMES = {
    15: None,
    50: (HERE / "config" / "scenario_report_v50.json", HERE / "config" / "scenario_report_v50_ja.json"),
}


def _read_report(file: Path, key: str, kind: type) -> dict:
    """자료 파일을 읽는다 — 없거나 깨진 것은 서버 오류가 아니라 자료의 상태라 사유로 돌려준다."""
    path = f"config/{file.name}"
    try:
        doc = json.loads(file.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"ok": False, "reason": "missing", "path": path}
    except (OSError, ValueError) as e:      # JSON·UTF-8 오류는 ValueError 다
        return {"ok": False, "reason": "broken", "path": path, "detail": str(e)}
    if not isinstance(doc, dict) or not isinstance(doc.get(key), kind):
        return {"ok": False, "reason": "broken", "path": path, "detail": f"{key} 항목이 없다"}
    return {"ok": True, "doc": doc}


def _timing(doc: dict) -> dict:
    """kind → 권장 길이·시작 시간대와 시각순 실측 — 실측 파일은 수정 시각으로 다시 읽는다."""
    from .. import measured
    from ..scenarios import L2_KINDS, recommended_duration, recommended_hours
    out = {}
    for k in {s.get("kind") for s in doc["scenarios"] if isinstance(s, dict) and s.get("kind")}:
        d = recommended_duration(k)
        if d:
            out[k] = {"recommended": d["recommended"], "hours": recommended_hours(k),
                      "runs": measured.runs(k), "l2": k in L2_KINDS}
    return out


@router.get("/api/scenario-fault-volume")
def api_scenario_fault_volume(fault: str):
    """그 장애가 **어느 생성량 탭에 판정 기록으로 있나** — 주입 화면의 "시나리오 화면에서 보기"가 그 탭으로 간다.

    2026-11-11 사용자 지적 — 50% 회차 F-278 을 열어 링크를 누르면 15% 탭이 열려 옛 회차(F-245)가 최신으로 보였다.
    장애 행·틱 이력에는 주입 당시 생성량이 남지 않는다(DDL 없이 간다). 그래서 **판정 기록(`latest`·`history`
    의 `fault`)에서 찾는다** — 추정이 아니라 기록이다. 없으면(아직 판정 전) ``volume`` 이 None 이고 화면이 지금
    생성량으로 고르며 그 사실을 링크 설명에 적는다. 여러 탭에 있으면 모두 돌려주고 첫 탭을 쓴다
    """
    fid = str(fault).strip().upper()
    if not fid.startswith("F-"):
        fid = f"F-{fid}"
    found: list[int] = []
    for vol in sorted(SCENARIO_VOLUMES):
        file = (SCENARIO_VOLUMES[vol] or (SCENARIO_REPORT, None))[0]
        rep = _read_report(file, "scenarios", list)
        if not rep["ok"]:
            continue
        for sc in rep["doc"]["scenarios"]:
            if not isinstance(sc, dict):
                continue
            runs = [sc.get("latest")] + list(sc.get("history") or ())
            if any(isinstance(r, dict) and str(r.get("fault") or "").upper() == fid for r in runs):
                found.append(vol)
                break
    return {"fault": fid, "volume": found[0] if found else None, "volumes": found}


@router.get("/api/glossary")
def api_glossary():
    """용어집만 — 지도 화면의 장애 이력이 KPI 코드를 쉬운 이름으로 보인다(사용자 11-14 · 일반인이 봐도 알게).

    시나리오 자료(수백 KB)를 통째로 받지 않으려고 따로 둔다. 없거나 깨지면 ``terms`` 가 비고 화면은 코드를 그대로 둔다
    """
    gloss = _read_report(SCENARIO_GLOSSARY, "terms", dict)
    return {"terms": gloss["doc"]["terms"] if gloss["ok"] else {}}


@router.get("/api/scenario-report")
def api_scenario_report(volume: int = 15):
    """요청마다 파일을 읽는다 — 캐시하면 재시작 전까지 옛 판정이 보인다.

    파일이 없거나 깨지면 200 으로 사유를 내리고 화면이 그대로 적는다. 빈 목록으로 내리면
    "시나리오가 없다"로 읽힌다. 만드는 방법은 없어도 결과는 보여 준다 — 그 칸만 빠진다.
    ``volume`` 은 생성량(%) 탭이다 — 없으면 15%. 모르는 값을 15% 로 내리면 다른 생성량의 판정으로 읽힌다.
    """
    from ..scenario_ja import merge as merge_ja
    if volume not in SCENARIO_VOLUMES:
        return {"ok": False, "reason": "volume", "volume": volume, "volumes": sorted(SCENARIO_VOLUMES)}
    report_file, ja_file = SCENARIO_VOLUMES[volume] or (SCENARIO_REPORT, SCENARIO_REPORT_JA)
    rep = _read_report(report_file, "scenarios", list)
    if not rep["ok"]:
        return rep
    out = {"ok": True, "report": rep["doc"], "how": {}, "timing": _timing(rep["doc"])}
    how = _read_report(SCENARIO_HOW, "scenarios", dict)
    if how["ok"]:
        out["how"] = how["doc"]["scenarios"]
    elif how["reason"] == "broken":
        out["how_error"] = how
    # 번역·용어집도 없으면 칸만 빠지고, 깨진 것은 같은 자리로 말한다
    ja = _read_report(ja_file, "scenarios", dict)
    if ja["ok"] and ja_file != SCENARIO_REPORT_JA:
        # 설정·판정 기준은 15% 와 글자까지 같다 — 그 번역을 원문 해시로 빌려 쓴다(`merge` 의 해시 대조).
        # 다른 묶음 키로 얹어 칸으로는 맞지 않게 한다 — 칸이 같아도 원문이 다르면 낡은 번역이다
        base = _read_report(SCENARIO_REPORT_JA, "scenarios", dict)
        if base["ok"]:
            ja = {"ok": True, "doc": {"scenarios": {**{f"15:{g}": v for g, v in base["doc"]["scenarios"].items()},
                                                    **ja["doc"]["scenarios"]}}}
    if ja["ok"]:
        out["report_ja"], left = merge_ja(rep["doc"], ja["doc"])
        out["ja_left"] = len(left["missing"]) + len(left["stale"])
    elif ja["reason"] == "broken":
        out.setdefault("how_error", ja)
    gloss = _read_report(SCENARIO_GLOSSARY, "terms", dict)
    if gloss["ok"]:
        out["glossary"] = gloss["doc"]["terms"]
    elif gloss["reason"] == "broken":
        out.setdefault("how_error", gloss)
    # L3 문답 검사 기록(최근 7일 · `l3log`) — 화면이 이전 검사와 회차별 L3 칸을 그린다. 한 번에 싣는다
    from .. import l3log
    out["l3"], out["l3_broken"] = l3log.load()
    return out

