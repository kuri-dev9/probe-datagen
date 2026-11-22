"""gen-ui 시나리오 화면 — 일반인용 요약 2칸(11-16 · 처음 3칸에서 합침) (2026-11-14 사용자 결정 · 맥스 구현 고정).

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


def _body(latest: dict, how="{brief:'쉬운 설명 문장'}", extra: dict | None = None) -> str:
    """`detail(s)` 가 그린 HTML 을 한 줄로 받는다."""
    s = {"no": 25, "name": "x", "status": "partial", "latest": latest, "setup": {}, "expect": {}} | (extra or {})
    out = _node("""
HOW={'25':%s};
GL={CHANGE_SUSPECTED:{ko:{s:'구성·설정 변화 의심'}},'C-24':{ko:{s:'통화 몰림'}}};
const s=%s;
console.log(JSON.stringify(detail(s)));
""" % (how, json.dumps(s, ensure_ascii=False)))
    return json.loads(out.strip().splitlines()[-1])


FLOW = "card_what:'카드 설명',card_flow:['어디에 — 서울','순서 — 0~3분 오름','평소 대비 — 2배','끝나면 — 돌아옵니다']"


def _top(h: str) -> str:
    return h[:h.index("자세히 보기 (기술 상세)")]


def test_summary_two_boxes_come_before_the_folded_technical_detail():
    """11-22 — 카드가 「어떤 장애인가요」 · 「이렇게 만들고 이렇게 움직입니다」 두 장이다(「시스템이 찾은 것」은 빠졌다)."""
    h = _body({"plain": "회차 문장", "detect_min": 6}, how="{" + FLOW + "}")
    a, c, tech = h.index("어떤 장애인가요"), h.index("이렇게 만들고 이렇게 움직입니다"), h.index("자세히 보기 (기술 상세)")
    assert a < c < tech
    # 생성량·실행 상태 머리(<h3><span>…)는 요약 박스가 아니다. 제목 글자만 있는 <h3> 이 두 장이다.
    assert h[:tech].count("<h3>") - h[:tech].count("<h3><span") == 2
    assert "카드 설명" in h[a:c]
    for part in ("어디에", "서울", "순서", "0~3분 오름", "평소 대비", "2배", "끝나면", "돌아옵니다"):
        assert part in h[c:tech], part
    assert "<details" in h[:tech] and h.count("<details") >= 1, "기술 상세는 접힘 안에 있다(지우지 않는다)"
    assert "시나리오 설명" in h[tech:] and "만드는 법" in h[tech:], "기존 구역이 그대로 아래에 남는다"


def test_old_titles_and_the_system_found_text_are_not_in_the_summary():
    h = _body({"plain": "회차 문장", "detect_min": 6, "cause": "CHANGE_SUSPECTED"}, how="{" + FLOW + "}")
    top = _top(h)
    for old in ("무엇이 나타나고 시스템이 무엇을 찾았나", "시스템이 찾은 것", "회차 문장", "주로 움직이는 지표",
                "자동으로 만든 문장", "6분 뒤", "신뢰도", "어떻게 만드나</h3>", "무슨 일이 생기나", ">결과<"):
        assert old not in top, old


def test_flow_lines_split_at_the_first_dash_into_a_bold_head():
    top = _top(_body({}, how="{card_flow:['어디에 — 서울 — 新宿','끝에 줄표 없음']}"))
    assert "<li><b>어디에</b> — 서울 — 新宿</li>" in top
    assert "<li>끝에 줄표 없음</li>" in top                    # 줄표가 없으면 머리를 만들지 않는다


def test_left_box_falls_back_card_what_then_brief_then_what():
    assert "카드 설명" in _top(_body({}, how="{card_what:'카드 설명',brief:'짧은 설명'}"))
    top = _top(_body({}, how="{brief:'짧은 설명'}", extra={"what": "보고서 설명"}))
    assert "짧은 설명" in top and "보고서 설명" not in top
    assert "보고서 설명" in _top(_body({}, how="{}", extra={"what": "보고서 설명"}))


def test_right_box_falls_back_to_the_recorded_phases_and_never_invents():
    top = _top(_body({}, how="{brief:'설명'}", extra={"setup": {"phases": [{"at": "0~3분", "what": "강도 0.5"}]}}))
    assert "<li><b>0~3분</b> — 강도 0.5</li>" in top
    nothing = _top(_body({}, how="{brief:'설명'}"))
    assert nothing.count("아직 정리되지 않았습니다") == 1       # 오른쪽만 비었다(왼쪽은 설명이 있다)


def test_target_duration_params_and_kpi_live_in_the_folded_detail_not_the_summary():
    """11-22 — 대상·길이·설정값·KPI·흔적은 위 요약에서 빠지고 접힌 상세에만 있다."""
    h = _body({"plain": "찾은 문장"}, how="{brief:'설명',traces:[{where:'XDR',what:'호가가 늘어난다'}]}",
              extra={"expect": {"kpi": "C-24"},
                     "setup": {"target": "진원역을 정한다", "duration_min": 30,
                               "params": [{"name": "규모", "value": "4.5~6.2", "initial": False}]}})
    top, rest = _top(h), h[h.index("자세히 보기 (기술 상세)"):]
    for gone in ("진원역을 정한다", "30분", "규모", "C-24", "호가가 늘어난다"):
        assert gone not in top, gone
    for there in ("진원역을 정한다", "30분", "규모", "4.5~6.2", "C-24", "XDR", "호가가 늘어난다"):
        assert there in rest, there


def test_boxes_are_all_unknown_when_nothing_is_recorded_and_not_a_result_card():
    h = _top(_body({}, how="{}"))
    assert h.count("아직 정리되지 않았습니다") >= 2 and "맞혔습니다" not in h and "psg" not in h


def test_no_result_card_in_the_expanded_summary():
    h = _body({"plain": "x"})
    top = h[:h.index("자세히 보기 (기술 상세)")]
    assert "맞혔습니다" not in top and "일부만" not in top and "class=psg" not in top and "r-warn" not in top


def test_japanese_phrases_exist_for_every_new_screen_string():
    for ko in ("어떤 장애이고 어떻게 만드나", "무엇이 나타나고 시스템이 무엇을 찾았나", "아직 정리되지 않았습니다", "주입 {n}분",
               "주로 움직이는 지표: {k}", "시스템이 찾은 것", "설정 {n}개 더 — 자세히 보기", "자세히 보기 (기술 상세)",
               "기록된 값으로 자동으로 만든 문장입니다", "아직 기록이 없습니다", "일부만 맞혔습니다",
               "이상 찾기", "원인 판단", "여러 장애 묶기", "AI 설명",
               "주입하자마자 이상을 찾았습니다.", "주입하고 {n}분 뒤에 이상을 처음 찾았습니다.",
               "원인은 정하지 못했습니다(판단 보류).", "시스템이 고른 원인은 「{c}」입니다."):
        assert ko in ui._JA_SCEN or ko in ui._JA_COMMON, ko
    for old in ("어떤 장애인가", "어떻게 만드나", "무엇이 나타나나"):                   # 쓰이지 않는 옛 제목은 번역 표에서 뺐다
        assert old not in ui._JA_SCEN and old not in ui._JA_COMMON, old


def test_plain_sentences_for_25_and_27_exist_in_the_50_percent_report():
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1] / "config"
    v50 = json.loads((root / "scenario_report_v50.json").read_text(encoding="utf-8"))
    sc = {str(s.get("no")): s for s in (v50.get("scenarios") or v50)} if isinstance(v50.get("scenarios", v50), list) else v50["scenarios"]
    p25 = (sc["25"].get("latest") or {}).get("plain")
    p27 = (sc["27"].get("latest") or {}).get("plain")
    assert p25 and p27
    # 11-14 04:05~05:08 새 회차(F-327·F-328)로 갱신 — 27번도 지진 구현 이후 회차라 "이전 회차" 표시가 없어졌다
    assert "지진 구현 이전" not in p25 and "지진 구현 이전" not in p27
    # 2026-11-15: 화면을 일반 사용자 기준으로 가기로 해 쉬운 문장은 **모든 번호**에 둘 수 있다(허용 목록 폐지).
    # 대신 쉬운 문장이 있는 번호는 일본어 사본에도 같은 번호의 `latest.plain` 이 있어야 하고 `scan()` 이 0이어야 한다


def _v50_paths():
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1] / "config"
    return root / "scenario_report_v50.json", root / "scenario_report_v50_ja.json"


def test_every_number_with_a_plain_sentence_has_the_same_plain_in_the_japanese_copy():
    ko_p, ja_p = _v50_paths()
    ko = json.loads(ko_p.read_text(encoding="utf-8"))["scenarios"]
    ja = json.loads(ja_p.read_text(encoding="utf-8"))["scenarios"]
    ko = {str(s["no"]): s for s in ko} if isinstance(ko, list) else {str(k): v for k, v in ko.items()}
    withplain = [n for n, s in ko.items() if (s.get("latest") or {}).get("plain")]
    assert {"24", "25", "27"} <= set(withplain)
    missing = [n for n in withplain if not ((ja.get(n) or {}).get("latest.plain") or {}).get("ja")]
    assert missing == [], f"일본어 사본에 latest.plain 이 없는 번호: {missing}"


def test_scenario_ja_scan_is_all_zero_for_the_50_percent_report(monkeypatch):
    from datagen import scenario_ja as S
    ko_p, ja_p = _v50_paths()
    monkeypatch.setattr(S, "REPORT", ko_p)
    monkeypatch.setattr(S, "REPORT_JA", ja_p)
    got = S.scan()
    assert got and all(v == 0 for v in got.values()), got
    assert S.todo() == {}
