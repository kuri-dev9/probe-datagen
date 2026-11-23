"""8번 시나리오 이름 「연결 끊김 증가」 (2026-11-23 · 사용자 「통화면 통화 끊김, 통화가 아니면 연결 끊김 증가」 · 맥스 구현 · 에스터 시험).

8번은 통화가 아니라 연결이 끊긴다(무선 사유 121 · C-04 는 데이터 연결 기준). 이름 정본은 `control.SCENARIO_ORDER`
(내부 번호 31)이고 현황 JSON 의 이름 칸·일본어 번역이 같아야 한다. 이름만 바꾼다 — 내부 이름(절단 급증)·KPI 이름(통화 절단률)은 그대로다.
"""
from __future__ import annotations

import json
from pathlib import Path

from datagen import control

CFG = Path(__file__).resolve().parents[1] / "config"
KO, JA = "연결 끊김 증가", "接続切断増加"


def _row():
    return next(r for r in control.SCENARIO_ORDER if r[0] == 31)


def test_control_order_names_scenario_8_as_a_connection_drop():
    no, ko, ja, sub_ko, sub_ja = _row()
    assert (ko, ja) == (KO, JA)
    assert "연결" in sub_ko and "接続" in sub_ja
    assert "통화" not in ko and "通話" not in ja, "통화가 아니라 연결이 끊긴다"


def test_old_name_is_gone_from_the_name_table_and_other_names_are_unchanged():
    names = {r[0]: r[1] for r in control.SCENARIO_ORDER}
    assert "통화 끊김 증가" not in names.values()
    assert names[9] == "이동 중 끊김" and names[2] == "무선 품질 저하" and names[22] == "결제 앱 지연"
    assert names[5] == "앱 첫 연결 실패" and len(set(names.values())) == len(names), "이름이 겹치지 않는다"
    assert [r[0] for r in control.SCENARIO_ORDER].count(31) == 1


def test_report_json_name_and_japanese_translation_agree_with_control():
    for f in ("scenario_report.json", "scenario_report_v50.json"):
        d = json.loads((CFG / f).read_text(encoding="utf-8"))
        assert [s["name"] for s in d["scenarios"] if s["no"] == 31] == [KO], f
    for f in ("scenario_report_ja.json", "scenario_report_v50_ja.json"):
        d = json.loads((CFG / f).read_text(encoding="utf-8"))
        assert d["scenarios"]["31"]["name"]["ja"] == JA, f


def test_internal_and_kpi_names_are_not_renamed():
    src = (Path(control.__file__)).read_text(encoding="utf-8")
    assert "절단 급증" in src                                           # 부제의 내부 이름
    from graphio.metrics import kpi as K
    assert "절단" in K.kpi_def("C-04").name                             # KPI 이름은 그대로
