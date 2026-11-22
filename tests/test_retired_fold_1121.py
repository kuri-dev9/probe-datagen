"""폐기 묶음 · 종류 이름(`KIND_SHOWN`) — 2026-11-21 24개 정리.

* 현황 API 가 `retired` 12개를 내린다. 현황 화면은 폐기를 건수·판정에서 빼고 「전체」 맨 뒤 묶음으로 접는다.
* `KIND_SHOWN` — 한 kind 를 여러 번호가 쓰면 화면 순서가 **앞선** 번호의 이름. 폐기 번호만 쓰는 kind 는 표에 없다.
* `scenario_how.json` — 폐기 번호 칸에만 `retired`(한·일) 가 있다.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from datagen import control
from datagen.ui import common, scenarios_api

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "src" / "datagen" / "ui" / "static" / "scenarios.html").read_text(encoding="utf-8")
# 11-22 — 24번은 카드가 부활해 폐기에서 빠졌다(화면 순서 25번째 · 사용자 「옛 24 카드 부활」)
EXPECT_RETIRED = {3, 4, 11, 18, 19, 20, 25, 26, 28, 33, 35}


def test_the_report_api_hands_down_the_eleven_retired_numbers():
    out = scenarios_api.api_scenario_report(volume=50)
    assert out["ok"] is True
    assert out["retired"] == sorted(EXPECT_RETIRED) and len(out["retired"]) == 11
    assert not set(out["retired"]) & {int(k) for k in out["ranks"]}, "폐기 번호는 화면 순서(rank)에 없다"
    assert len(out["ranks"]) == 24    # 24번은 폐기에서 빠졌지만 화면 순서표(rank)에는 아직 없다(11-22 부활 카드)


def test_the_screen_drops_retired_from_counts_and_folds_them_at_the_end():
    # 살아 있는 것만 센다 · 폐기는 따로 모은다 · 「전체」일 때만 맨 뒤 묶음 · 폐기 카드 상태 칸은 「폐기」
    assert re.search(r"all=every\.filter\(s=>!RETIRED\.has\(String\(s\.no\)\)\),\s*gone=every\.filter\(s=>RETIRED\.has", HTML)
    assert "fold('retired'" in HTML and "gone.map(card)" in HTML
    assert "dead ? `<span class=\"st s-dim\">${esc(T('폐기'))}" in HTML
    # 장애 창에서 폐기 번호로 들어오면 묶음이 열린 채로 보인다
    assert "if(RETIRED.has(no[1])) OPEN.add('retired')" in HTML


def test_the_japanese_table_has_the_retired_fold_phrases():
    from datagen.ui import i18n_ja
    src = Path(i18n_ja.__file__).read_text(encoding="utf-8")
    assert "폐기 {n}건" in src and "아래는 과거 기록입니다." in src


def test_kind_shown_uses_the_earliest_screen_number_for_a_shared_kind():
    kind_of = {no: kind for no, _n, kind, *_ in control.SCENARIOS}
    seen: dict[str, str] = {}
    for no, name, *_ in control.SCENARIO_ORDER:        # 화면 순서대로
        k = kind_of.get(no)
        if k:
            seen.setdefault(k, name)
    assert control.KIND_SHOWN == seen
    # 공유 kind 의 실례 — ue_model 은 8번(앞선 번호)의 이름이고 폐기 26번의 이름이 아니다
    assert control.KIND_SHOWN["ue_model"] == control.SCENARIO_SHOWN[8][0]
    ranks = control.SCENARIO_RANK
    shared = [k for k in seen if sum(1 for no, kk in kind_of.items() if kk == k and no in ranks) > 1]
    for k in shared:
        first = min((no for no, kk in kind_of.items() if kk == k and no in ranks), key=ranks.get)
        assert control.KIND_SHOWN[k] == control.SCENARIO_SHOWN[first][0], k


def test_kinds_used_only_by_retired_numbers_are_not_in_the_table_and_keep_old_names():
    live = {no for no, *_ in control.SCENARIO_ORDER}
    kind_of = {no: kind for no, _n, kind, *_ in control.SCENARIOS}
    only_retired = {k for no, k in kind_of.items() if no in control.RETIRED and k
                    and not any(kk == k and n in live for n, kk in kind_of.items())}
    assert only_retired, "폐기 전용 kind 가 하나도 없으면 이 시험이 아무것도 안 본다"
    assert not only_retired & set(control.KIND_SHOWN)


def test_kind_labels_take_the_new_names_and_the_daily_load_name():
    for k, v in control.KIND_SHOWN.items():
        if k in common.KIND_LABELS:
            assert common.KIND_LABELS[k] == v, k
    assert common.KIND_LABELS["capacity_shift"] == "매일 같은 시각 부하"
    assert common.KIND_LABELS["event_crowd"] == "행사장 특정 앱 먹통"


def test_the_scenario_how_file_marks_only_retired_numbers():
    how = json.loads((ROOT / "config" / "scenario_how.json").read_text(encoding="utf-8"))["scenarios"]
    marked = {int(n) for n, v in how.items() if "retired" in v}
    assert marked == EXPECT_RETIRED
    for n in marked:
        r = how[str(n)]["retired"]
        assert isinstance(r, dict) and r.get("ko") and r.get("ja") or isinstance(r, str) and r, n
