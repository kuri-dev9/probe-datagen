"""gen-ui 시나리오 화면 — 일반인용 요약 3칸 (2026-11-14 사용자 결정 · 맥스 구현 고정).

카드를 펼치면 맨 위에 ① 무슨 일이 생기나 ② 시스템이 무엇을 찾았나 ③ 결과 가 오고, 기존 기술 상세는
"자세히 보기" 하나로 접혀 그 **아래**에 있다(지우지 않는다). ② 는 `latest.plain` 이 있으면 그것, 없으면
기록된 값만으로 만든 자동 문장이며 그때는 "자동으로 만든 문장" 표시가 붙는다. 실제 페이지 스크립트를 node 로 돈다.
DB 를 쓰지 않는다.
"""
from __future__ import annotations

import json
import re
import subprocess

import pytest

from datagen import ui

NODE = subprocess.run(["which", "node"], capture_output=True, text=True).stdout.strip()

STUB = """
const _el=new Proxy(function(){},{get:(t,k)=>k==='style'||k==='classList'||k==='dataset'?_el:(k===Symbol.toPrimitive?()=>'':_el),apply:()=>_el,set:()=>true});
globalThis.document=new Proxy({},{get:()=>_el});globalThis.window=globalThis;
globalThis.localStorage={getItem:()=>null,setItem(){}};globalThis.location={search:'',hash:'',pathname:'/scenarios'};
globalThis.fetch=()=>new Promise(()=>{});globalThis.setInterval=()=>0;globalThis.addEventListener=()=>{};
globalThis.MutationObserver=class{observe(){}};
"""


def _node(test: str) -> str:
    if not NODE:
        pytest.skip("node 가 없다")
    js = max(re.findall(r"<script[^>]*>(.*?)</script>", ui._HTML_SCEN, re.S), key=len)
    r = subprocess.run([NODE, "-e", STUB + js + "\n" + test], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr[-800:]
    return r.stdout


def _body(latest: dict) -> str:
    """`detail(s)` 가 그린 HTML 을 한 줄로 받는다."""
    s = {"no": 25, "name": "x", "status": "partial", "latest": latest, "setup": {}, "expect": {}}
    out = _node("""
HOW={'25':{brief:'쉬운 설명 문장'}};
GL={CHANGE_SUSPECTED:{ko:{s:'구성·설정 변화 의심'}}};
const s=%s;
console.log(JSON.stringify(detail(s)));
""" % json.dumps(s, ensure_ascii=False))
    return json.loads(out.strip().splitlines()[-1])


def test_summary_three_boxes_come_before_the_folded_technical_detail():
    h = _body({"plain": "처음 문장", "detect_min": 6})
    a, b, c = h.index("무슨 일이 생기나"), h.index("시스템이 무엇을 찾았나"), h.index(">결과<")
    tech = h.index("자세히 보기 (기술 상세)")
    assert a < b < c < tech
    assert "쉬운 설명 문장" in h[a:b]
    assert "<details" in h[:tech] and h.count("<details") >= 1, "기술 상세는 접힘 안에 있다(지우지 않는다)"
    assert "시나리오 설명" in h[tech:] and "만드는 법" in h[tech:], "기존 구역이 그대로 아래에 남는다"


def test_latest_plain_wins_and_has_no_auto_marker():
    h = _body({"plain": "사람이 쓴 문장", "detect_min": 6, "cause": "CHANGE_SUSPECTED"})
    assert "사람이 쓴 문장" in h
    assert "자동으로 만든 문장" not in h
    assert "6분 뒤" not in h, "plain 이 있으면 자동 문장을 만들지 않는다"


def test_empty_plain_falls_back_to_an_auto_sentence_with_marker_using_only_recorded_values():
    h = _body({"detect_min": 6, "cause": "CHANGE_SUSPECTED"})
    assert "주입하고 6분 뒤에 이상을 처음 찾았습니다." in h
    assert "「구성·설정 변화 의심」" in h                      # 용어집 이름 — 코드가 아니다
    assert "기록된 값으로 자동으로 만든 문장입니다" in h
    top = h[:h.index("자세히 보기 (기술 상세)")]               # 요약 세 칸만 — 상세 구역에는 "신뢰도" 칸이 있다
    assert "신뢰도" not in top and "0.6" not in top, "기록에 없는 말을 지어내지 않는다"


def test_auto_sentence_variants():
    h0 = _body({"detect_min": 0, "cause": "UNDETERMINED"})
    assert "주입하자마자 이상을 찾았습니다." in h0 and "원인은 정하지 못했습니다(판단 보류)." in h0
    h1 = _body({})                                            # 기록 없음 — 지어내지 않는다
    assert "아직 기록이 없습니다" in h1 and "자동으로 만든 문장" not in h1


def test_result_box_uses_plain_status_and_stage_names():
    h = _body({"plain": "x"})
    assert "일부만 맞혔습니다" in h
    for name in ("이상 찾기", "원인 판단", "여러 장애 묶기", "AI 설명"):
        assert name in h, name


def test_japanese_phrases_exist_for_every_new_screen_string():
    for ko in ("무슨 일이 생기나", "시스템이 무엇을 찾았나", "자세히 보기 (기술 상세)",
               "기록된 값으로 자동으로 만든 문장입니다", "아직 기록이 없습니다", "일부만 맞혔습니다",
               "이상 찾기", "원인 판단", "여러 장애 묶기", "AI 설명",
               "주입하자마자 이상을 찾았습니다.", "주입하고 {n}분 뒤에 이상을 처음 찾았습니다.",
               "원인은 정하지 못했습니다(판단 보류).", "시스템이 고른 원인은 「{c}」입니다."):
        assert ko in ui._JA_SCEN or ko in ui._JA_COMMON, ko


def test_plain_sentences_for_25_and_27_exist_only_in_the_50_percent_report_with_ja():
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1] / "config"
    v50 = json.loads((root / "scenario_report_v50.json").read_text(encoding="utf-8"))
    sc = {str(s.get("no")): s for s in (v50.get("scenarios") or v50)} if isinstance(v50.get("scenarios", v50), list) else v50["scenarios"]
    p25 = (sc["25"].get("latest") or {}).get("plain")
    p27 = (sc["27"].get("latest") or {}).get("plain")
    assert p25 and p27
    # 11-14 04:05~05:08 새 회차(F-327·F-328)로 갱신 — 27번도 지진 구현 이후 회차라 "이전 회차" 표시가 없어졌다
    assert "지진 구현 이전" not in p25 and "지진 구현 이전" not in p27
    others = [n for n, s in sc.items() if n not in ("24", "25", "27") and (s.get("latest") or {}).get("plain")]
    assert others == [], "나머지 번호는 비워 둔다(자동 문장) — 24번은 11-14 07:24 회차(F-330)부터 쉬운 문장이 있다"
