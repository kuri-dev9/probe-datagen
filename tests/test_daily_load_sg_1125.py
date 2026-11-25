"""매일 부하는 **화면의 역 한 칸 = 장애 1건**이다 — 2026-11-25 사용자 「2시간짜리 2역인데 4개 나왔다」 · 맥스 구현 · 에스터 시험.

화면(`/api/stations`)은 표시 이름이 같은 역 ID 를 한 칸으로 묶는데(渋谷 ID 3개) 생성기가 ID 마다 장애를 걸어 4건이 됐다.
고친 뒤 고정하는 것 —
  * 묶는 기준은 한 곳(`faults.station_label`) — 화면과 생성기가 같은 함수를 쓰고, 메모의 역 이름도 화면 이름이다
  * 대상 형식: 같은 이름 ID 가 여럿이면 `DAILY:SG:<첫 ID>`, 하나면 `DAILY:ST:<id>`(옛 형식 그대로 해석) · `CM_DATAGEN_FAULT.TARGET` 은 64자
  * `SG` 는 같은 이름 ID 들의 셀을 합쳐 해석한다 · 셀 0개 판단도 합친 것으로
  * 중복 검사는 대기·진행 중인 매일 부하와 **역 ID 가 하나라도 겹치면** 건너뜀(옛 ID 하나짜리가 돌고 있을 때 묶은 걸 또 걸지 않는다)
DB 는 가짜다(`_Conn`). 역 표는 시험이 직접 채운다.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_daily_load_groups_1121 import _Conn

from datagen import cli, control, faults
from datagen.ui import status_api

SHIBUYA = ["35.65734-139.70223", "35.65855-139.69898", "35.65857-139.70146"]     # 渋谷 ID 셋
SHINJUKU = "35.68968-139.70035"
LINE = "on|18,19|" + ",".join(f"ST:{i}" for i in SHIBUYA + [SHINJUKU])             # 사용자의 묶음 1 — 2시간 · 화면에서는 두 칸
CELLS = [SimpleNamespace(cell_key=k, enb_id=k.split("-")[0]) for k in ("1-0", "2-0", "3-0", "4-0", "5-0", "6-0")]


@pytest.fixture(autouse=True)
def _tbl(monkeypatch):
    monkeypatch.setattr(faults, "STATION_NAMES", {SHIBUYA[0]: "渋谷", SHIBUYA[1]: "渋谷", SHIBUYA[2]: "渋谷", SHINJUKU: "新宿"}, raising=False)
    monkeypatch.setattr(faults, "STATION_CELLS", {SHIBUYA[0]: {"1-0", "2-0"}, SHIBUYA[1]: {"3-0"}, SHIBUYA[2]: {"4-0"},
                                                   SHINJUKU: {"5-0", "6-0"}}, raising=False)
    monkeypatch.setattr(faults, "load_stations", lambda conn: None, raising=False)
    monkeypatch.setattr(faults, "ADM3_CELLS", {"x": {"1-0"}}, raising=False)
    monkeypatch.setattr(faults, "load_adm3", lambda conn: None, raising=False)
    monkeypatch.setattr(cli, "_DAILY_NO_CELLS", set())


def _at(h=18, m=0, s=3):
    return datetime(2026, 11, 25, h, m, s)


# ── 묶기 ──────────────────────────────────────────────────────────────────────

def test_groups_follow_the_display_name_and_keep_the_first_seen_order():
    toks = [f"ST:{SHIBUYA[0]}", f"ST:{SHINJUKU}", f"ST:{SHIBUYA[1]}", f"ST:{SHIBUYA[2]}", "ENB:190885"]
    g = faults.daily_groups(toks)
    assert g == [[f"ST:{SHIBUYA[0]}", f"ST:{SHIBUYA[1]}", f"ST:{SHIBUYA[2]}"], [f"ST:{SHINJUKU}"], ["ENB:190885"]]
    assert faults.daily_groups([f"ST:{SHIBUYA[0]}", f"ST:{SHIBUYA[0]}"]) == [[f"ST:{SHIBUYA[0]}"]], "같은 토큰이 두 번 와도 한 칸"


def test_groups_without_a_station_table_fall_back_to_one_per_id(monkeypatch):
    monkeypatch.setattr(faults, "STATION_NAMES", {})
    assert len(faults.daily_groups([f"ST:{i}" for i in SHIBUYA])) == 3, "이름을 모르면 짐작하지 않고 ID 마다 하나"


def test_station_label_is_the_single_source_and_prefers_the_ja_name():
    names = {"A": {"ja": "渋谷"}}
    assert faults.station_label("A", "渋谷駅新南口", names) == "渋谷"        # 대응표(ja)가 DB 이름을 이긴다
    assert faults.station_label("B", "品川", names) == "品川"               # 표에 없으면 DB 이름
    assert faults.station_label("C", None, names) == "C"                    # 둘 다 없으면 ID
    src = Path(status_api.__file__).read_text(encoding="utf-8")
    assert "station_label" in src, "화면(`api_stations`)도 같은 함수를 쓴다 — 두 벌이 되지 않게"


# ── 대상 형식 ─────────────────────────────────────────────────────────────────

def test_token_is_sg_for_several_ids_and_the_old_st_for_one_and_fits_64_chars():
    g = faults.daily_groups([f"ST:{i}" for i in SHIBUYA + [SHINJUKU]])
    toks = [faults.daily_token(x) for x in g]
    assert toks == [f"SG:{SHIBUYA[0]}", f"ST:{SHINJUKU}"]
    assert all(len(f"{cli.DAILY_PREFIX}:{t}") <= 64 for t in toks)
    assert len(f"{cli.DAILY_PREFIX}:ST:" + ":".join(SHIBUYA)) > 64, "ID 를 다 늘어놓으면 칸 폭(64)을 넘는다 — 그래서 대표 ID 하나"
    assert faults.daily_token(["ENB:1"]) == "ENB:1"


def test_station_group_returns_every_id_with_the_same_name():
    assert faults.station_group(SHIBUYA[1]) == sorted(SHIBUYA) and faults.station_group(SHINJUKU) == [SHINJUKU]
    assert faults.station_group("STN-" + SHIBUYA[0]) == sorted(SHIBUYA), "STN- 접두어를 떼고 본다"
    assert faults.station_group("9.9-9.9") == ["9.9-9.9"], "모르는 역은 그 ID 하나"


def test_parts_unpack_sg_st_old_and_other_targets():
    assert faults.daily_parts(f"DAILY:SG:{SHIBUYA[0]}") == {f"ST:{i}" for i in SHIBUYA}
    assert faults.daily_parts(f"DAILY:ST:{SHINJUKU}") == {f"ST:{SHINJUKU}"}
    assert faults.daily_parts("DAILY:ENB:190885") == {"ENB:190885"} and faults.daily_parts("") == set()


# ── 셀 해석 ───────────────────────────────────────────────────────────────────

def test_sg_cells_are_the_union_of_every_id_and_old_st_still_resolves_one_id():
    assert faults._daily_cells(f"SG:{SHIBUYA[0]}", CELLS) == {"1-0", "2-0", "3-0", "4-0"}
    assert faults._daily_cells(f"ST:{SHIBUYA[0]}", CELLS) == {"1-0", "2-0"}              # 옛 단일 ID 대상 — 하위 호환
    only = [c for c in CELLS if c.cell_key in ("3-0",)]
    assert faults._daily_cells(f"SG:{SHIBUYA[0]}", only) == {"3-0"}, "이 gen 의 셀 마스터에 있는 것만"
    assert faults._daily_cells("SG:9.9-9.9", CELLS) == set()


# ── 등록 ──────────────────────────────────────────────────────────────────────

def test_the_users_group_fires_two_faults_not_four_with_the_screen_names():
    c = _Conn([LINE])
    assert cli._daily_load(c, _at(18), 0, CELLS) is not None
    assert sorted(f["target"] for f in c.faults) == sorted([f"{cli.DAILY_PREFIX}:SG:{SHIBUYA[0]}", f"{cli.DAILY_PREFIX}:ST:{SHINJUKU}"])
    assert len(c.faults) == 2 and all(f["dur"] == 120 and f["mode"] == "daily" and f["kind"] == "capacity_shift" for f in c.faults)
    notes = " ".join(f["note"] for f in c.faults)
    assert "渋谷 (" in notes and "新宿 (" in notes and "新南口" not in notes, "메모의 역 이름은 화면과 같다"
    assert cli._daily_load(c, _at(19), 0, CELLS) is None and len(c.faults) == 2, "이어진 2시간의 다음 정시에는 또 걸지 않는다"
    assert cli._daily_load(c, _at(18, 0, 25), 0, CELLS) is None and len(c.faults) == 2, "같은 분의 다음 틱도"


def test_an_old_style_single_id_fault_still_running_blocks_the_grouped_one():
    c = _Conn([LINE], existing=[f"{cli.DAILY_PREFIX}:ST:{SHIBUYA[1]}"])                # 옛 꼴로 渋谷 ID 하나가 진행 중
    cli._daily_load(c, _at(18), 0, CELLS)
    assert [f["target"] for f in c.faults] == [f"{cli.DAILY_PREFIX}:ST:{SHINJUKU}"], "渋谷는 건너뛰고 新宿만"


def test_a_grouped_fault_already_running_blocks_it_on_the_next_tick():
    c = _Conn([LINE], existing=[f"{cli.DAILY_PREFIX}:SG:{SHIBUYA[0]}"])
    cli._daily_load(c, _at(18), 0, CELLS)
    assert [f["target"] for f in c.faults] == [f"{cli.DAILY_PREFIX}:ST:{SHINJUKU}"]


def test_no_cell_check_uses_the_whole_group_so_one_out_of_range_id_does_not_drop_it(monkeypatch):
    monkeypatch.setattr(faults, "STATION_CELLS", {SHIBUYA[0]: {"900-0"}, SHIBUYA[1]: {"3-0"}, SHIBUYA[2]: {"901-0"}, SHINJUKU: {"5-0"}})
    c = _Conn([LINE])
    cli._daily_load(c, _at(18), 0, CELLS)
    assert f"{cli.DAILY_PREFIX}:SG:{SHIBUYA[0]}" in [f["target"] for f in c.faults], "ID 하나만 이 gen 의 셀이 있어도 건다"
    monkeypatch.setattr(faults, "STATION_CELLS", {SHIBUYA[0]: {"900-0"}, SHIBUYA[1]: {"902-0"}, SHIBUYA[2]: {"901-0"}, SHINJUKU: {"5-0"}})
    c2 = _Conn([LINE])
    cli._daily_load(c2, _at(18), 0, CELLS)
    assert [f["target"] for f in c2.faults] == [f"{cli.DAILY_PREFIX}:ST:{SHINJUKU}"], "전부 범위 밖이면 묶음째 건너뜀"


def test_the_duplicate_query_is_one_read_of_all_daily_targets_not_per_token():
    """중복 검사는 `TARGET LIKE 'DAILY%'` 한 번 — 토큰마다 묻는 옛 질의(`TARGET=%s`)로 돌아가면 가짜 DB 가 거부한다"""
    import inspect
    src = inspect.getsource(cli._daily_load)
    assert "TARGET LIKE 'DAILY%'" in src and "TARGET=%s" not in src.replace(" ", "")
    assert src.count("daily_groups(") == 1 and "for tok in d[\"targets\"]" not in src


def test_non_station_tokens_still_fire_one_each():
    c = _Conn(["on|18|ENB:1,CELL:2-0,ENB:2"])
    cli._daily_load(c, _at(18), 0, [SimpleNamespace(cell_key="2-0", enb_id="2"), SimpleNamespace(cell_key="1-0", enb_id="1")])
    assert sorted(f["target"] for f in c.faults) == sorted(f"{cli.DAILY_PREFIX}:{t}" for t in ("ENB:1", "CELL:2-0", "ENB:2"))


def test_the_resolver_turns_sg_into_one_target_with_all_its_cells():
    t = faults._shift_target(f"DAILY:SG:{SHIBUYA[0]}", datetime(2026, 11, 25, 18), CELLS)
    assert t is not None and t.cell_keys == {"1-0", "2-0", "3-0", "4-0"} and t.enb_id is None        # 여러 eNB 에 걸치면 eNB 하나로 못 박지 않는다
    one = faults._shift_target(f"DAILY:SG:{SHINJUKU}", datetime(2026, 11, 25, 18), CELLS)               # ID 하나인 역도 SG 로 풀린다
    assert one is not None and one.cell_keys == {"5-0", "6-0"}
    old = faults._shift_target(f"DAILY:ST:{SHIBUYA[0]}", datetime(2026, 11, 25, 18), CELLS)             # 진행 중이던 옛 단일 ID 장애는 그대로
    assert old is not None and old.cell_keys == {"1-0", "2-0"}
    assert faults._shift_target("DAILY:SG:9.9-9.9", datetime(2026, 11, 25, 18), CELLS) is None


def test_the_cancel_note_names_the_reason_for_an_sg_group(monkeypatch):
    assert faults.daily_cancel_note(f"DAILY:SG:{SHIBUYA[0]}") == "생성 범위 밖 역(셀 0개) — 이 역의 셀은 생성기가 데이터를 만들지 않습니다"
    monkeypatch.setattr(faults, "STATION_CELLS", {})
    assert "역 매핑에 없는" in faults.daily_cancel_note(f"DAILY:SG:{SHIBUYA[0]}")
    assert faults.daily_cancel_note("ENB:1") is None
