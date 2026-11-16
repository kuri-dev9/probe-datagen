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
    """11-16 — 세 칸이 「어떤 장애인가 · 어떻게 만드나 · 무엇이 나타나나」로 바뀌었다(「결과」 칸은 없다)."""
    h = _body({"plain": "처음 문장", "detect_min": 6})
    a, b, c = h.index("어떤 장애인가"), h.index("어떻게 만드나"), h.index("무엇이 나타나나")
    tech = h.index("자세히 보기 (기술 상세)")
    assert a < b < c < tech
    assert "쉬운 설명 문장" in h[a:b]
    assert "처음 문장" in h[c:tech]                              # 시스템이 찾은 것(latest.plain)은 ③ 안에
    assert ">결과<" not in h[:tech] and "무슨 일이 생기나" not in h[:tech] and "시스템이 무엇을 찾았나" not in h[:tech]
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
    top = h1[:h1.index("자세히 보기 (기술 상세)")]
    assert "아직 정리되지 않았습니다" in top and "자동으로 만든 문장" not in top
    assert top.count("아직 정리되지 않았습니다") == 2           # ② 와 ③ (① 은 설명이 있다)


def _top(s_extra: dict, how="{brief:'설명'}") -> str:
    s = {"no": 25, "name": "x", "status": "partial", "latest": {}, "setup": {}, "expect": {}} | s_extra
    out = _node("""
HOW={'25':%s};
GL={C-24:{ko:{s:'통화 몰림'}}};
const s=%s;
console.log(JSON.stringify(detail(s)));
""".replace("C-24:", "'C-24':") % (how, json.dumps(s, ensure_ascii=False)))
    h = json.loads(out.strip().splitlines()[-1])
    return h[:h.index("자세히 보기 (기술 상세)")]


def test_how_box_uses_target_duration_and_at_most_two_params_and_counts_the_rest():
    """② 어떻게 만드나 — 대상 · 주입 길이 · 초기값이 아닌 설정 최대 2개, 나머지는 「설정 n개 더 — 자세히 보기」."""
    h = _top({"setup": {"target": "진원역을 정한다", "duration_min": 30,
                        "params": [{"name": "규모", "value": "4.5~6.2", "initial": False},
                                   {"name": "권장 주입 길이", "value": "20분", "initial": True},
                                   {"name": "폭주 정점", "value": "40배", "initial": True},
                                   {"name": "진앙 흩뜨림", "value": "±0.1도", "initial": False},
                                   {"name": "지반", "value": "1.0", "initial": False}]}})
    b = h[h.index("어떻게 만드나"):h.index("무엇이 나타나나")]
    assert "진원역을 정한다" in b and "주입 30분" in b
    assert "규모 4.5~6.2" in b and "진앙 흩뜨림 ±0.1도" in b           # 초기값이 아닌 설정 앞 2개
    assert "지반 1.0" not in b and "폭주 정점" not in b                 # 3번째 이후와 초기값은 줄에 안 나온다
    assert "권장 주입 길이" not in b                                     # 안내 줄은 이름으로 완전히 뺀다
    assert "설정 2개 더 — 자세히 보기" in b                               # 뺀 것(폭주 정점 · 지반)은 숨기지 않고 개수로 알린다


def test_effect_box_has_kpi_label_first_trace_and_found_text_and_nothing_invented():
    h = _top({"expect": {"kpi": "C-24"}, "latest": {"plain": "찾은 문장"}},
             how="{brief:'설명',traces:[{where:'XDR',what:'호가 늘어난다'},{where:'둘째',what:'안 나온다'}]}")
    c = h[h.index("무엇이 나타나나"):]
    assert "주로 움직이는 지표: 통화 몰림 (C-24)" in c
    assert "XDR — 호가 늘어난다" in c and "둘째" not in c         # 흔적은 첫 줄만(나머지는 자세히 보기)
    assert "시스템이 찾은 것" in c and "찾은 문장" in c


def test_boxes_are_all_unknown_when_nothing_is_recorded_and_not_a_result_card():
    h = _top({}, how="{}")
    assert h.count("아직 정리되지 않았습니다") >= 2 and "맞혔습니다" not in h and "psg" not in h


def test_no_result_card_in_the_expanded_summary():
    h = _body({"plain": "x"})
    top = h[:h.index("자세히 보기 (기술 상세)")]
    assert "맞혔습니다" not in top and "일부만" not in top and "class=psg" not in top and "r-warn" not in top


def test_japanese_phrases_exist_for_every_new_screen_string():
    for ko in ("어떤 장애인가", "어떻게 만드나", "무엇이 나타나나", "아직 정리되지 않았습니다", "주입 {n}분",
               "주로 움직이는 지표: {k}", "시스템이 찾은 것", "설정 {n}개 더 — 자세히 보기", "자세히 보기 (기술 상세)",
               "기록된 값으로 자동으로 만든 문장입니다", "아직 기록이 없습니다", "일부만 맞혔습니다",
               "이상 찾기", "원인 판단", "여러 장애 묶기", "AI 설명",
               "주입하자마자 이상을 찾았습니다.", "주입하고 {n}분 뒤에 이상을 처음 찾았습니다.",
               "원인은 정하지 못했습니다(판단 보류).", "시스템이 고른 원인은 「{c}」입니다."):
        assert ko in ui._JA_SCEN or ko in ui._JA_COMMON, ko


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
