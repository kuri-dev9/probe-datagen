"""생성 엔진 — 1분 슬롯을 만들어 내부 DuckDB 에 적재하고 집계한다.

흐름: Generator 가 분당 xDR 레코드(문자열 리스트)를 만든다 → store 가 타입
캐스팅해 raw_<계열> 에 넣는다 → aggregate 가 같은 커넥션에서 1분 집계를 돌려
창고 테이블(kpi_*_min 등)을 채운다. 파일은 남기지 않는다.

성공 전용(1단계)이다. 장애 주입(scenarios)은 2단계에서 Generator 에 활성
시나리오를 넘기는 것으로 붙는다 — 구조는 이미 그걸 받게 돼 있다.
"""

from __future__ import annotations

import json
import random
from datetime import datetime, timedelta
from pathlib import Path

from . import aggregate, faults, store
from .cells import Cell, load_cells
from .generator import Generator, SEP, SERIES_DIR
from .profile import load as load_profiles
from .scenarios import Scenario


def _rows_for_series(lines: list[str], series: str) -> list[tuple]:
    """SEP 구분 문자열 → store 스키마(KEEP 컬럼) 순서의 타입 캐스팅 튜플.

    Generator 는 569 전체 필드를 SEP 로 이어 내보낸다. store 의 raw_<계열> 은
    KEEP 컬럼만 가지므로, 전체 인덱스에서 KEEP 위치만 뽑아 캐스팅한다.
    """
    from .fields import SERIES_FIELDS
    # SERIES_FIELDS 는 정규화명(Last_Cell_ID), KEEP 는 .dat 원본 헤더명(Last_Cell ID)이
    # 섞여 있다. 원본은 read_csv 가 sql_name 으로 흡수했다 — 여기서 같은 정규화로 맞춘다.
    full_idx = {store.sql_name(f): i for i, f in enumerate(SERIES_FIELDS[series])}
    picks = [(store.sql_name(name), full_idx[store.sql_name(name)]) for name in store.KEEP[series]]
    out = []
    for line in lines:
        vals = line.split(SEP)
        out.append(tuple(store.cast_value(name, vals[i]) for name, i in picks))
    return out


class DataGen:
    def __init__(self, data_dir: Path, rates: dict, seed: int = 20260903,
                 anchor: datetime | None = None):
        self.cells: list[Cell] = load_cells(data_dir / "cells.json")
        self.profiles = load_profiles(data_dir / "profile.json")
        self.core_nodes = json.loads((data_dir / "core_nodes.json").read_text(encoding="utf-8"))
        self.gen = Generator(
            cells=self.cells, profiles=self.profiles, scenarios=[],
            rates=rates, seed=seed, diurnal=True,
        )
        self.seed = seed
        #: 분 인덱스 0 의 시각 — fault 시간창을 분 좌표로 옮기는 기준점.
        #: None 이면 첫 generate 시각에서 역산해 채운다.
        self.anchor = anchor
        #: fault_id → (판별 키, Scenario). 같은 fault 가 틱마다 다시 와도 같은
        #: Scenario 를 재사용해야 generator 의 도메인·기종 바인딩이 다시 구르지 않는다.
        self._fault_cache: dict[int, tuple[tuple, Scenario]] = {}
        self.con = store.open_db()
        self._seed_masters()

    def _seed_masters(self) -> None:
        """cell_master · core_node — 지리·토폴로지 화면과 노드 축 해석의 바탕."""
        self.con.executemany(
            "INSERT INTO cell_master VALUES (" + ",".join("?" * 22) + ")",
            [(
                c.cell_key, c.enb_id, c.cell_id, c.c_uid, c.tac, c.plmn,
                c.latitude, c.longitude, c.adm_sido_cd, c.adm_sido_nm,
                c.adm_sgg_cd, c.adm_sgg_nm, c.adm_emd_cd, c.adm_emd_nm,
                c.geo_status, c.rat, c.mme_id, c.sgw_id, c.pgw_id, c.ims_pgw_id,
                c.traffic_weight, c.display_name,
            ) for c in self.cells],
        )
        self.con.executemany(
            "INSERT INTO core_node VALUES (?,?,?,?,?,?,?)",
            [(n["node_type"], n["node_id"], n.get("display_name", ""),
              n.get("region_label", ""), n.get("vendor", ""), n.get("note", ""),
              n.get("cell_cnt", 0)) for n in self.core_nodes],
        )

    def generate_minute(self, minute: int, ts: datetime) -> dict:
        """한 분의 xDR 레코드를 만들어 내부 raw_<계열> 에 적재한다. 집계는 안 한다.

        집계는 매 분 호출하면 누적 raw 전체를 재스캔해 느리다(백필에서 치명적).
        여러 분을 생성한 뒤 aggregate_range 로 한 번에 돌린다 — 구간 멱등이라
        결과는 분마다 돌린 것과 같다.
        """
        if self.anchor is None:
            # 호출자가 anchor 를 안 정했으면 첫 generate 가 기준 — minute 인덱스가
            # 0 이 아닐 수도 있으므로 역산해 "인덱스 0 의 시각"으로 맞춘다.
            self.anchor = ts - timedelta(minutes=minute)
        payload = self.gen.generate_minute(minute, ts)
        counts = {}
        for series, lines in payload.items():
            rows = _rows_for_series(lines, series)
            counts[series] = store.insert_minute(self.con, series, rows)
            self.con.execute(
                "INSERT INTO file_ledger VALUES (?,?,?,?,?,?,?)",
                [series, ts, f"mem://{SERIES_DIR[series]}/{ts:%Y%m%d_%H%M}",
                 "OK", counts[series], datetime.now(), 0],
            )
        return counts

    def aggregate_range(self, lo: datetime, hi: datetime) -> None:
        """[lo, hi) 구간을 한 번에 집계한다 (창고 테이블 채움)."""
        aggregate.aggregate_all(self.con, None, start=lo, end=hi, log=lambda *a: None)

    def produce_minute(self, minute: int, ts: datetime) -> dict:
        """실시간 틱용 — 한 분 생성 + 그 분만 집계 (여유 있을 때)."""
        counts = self.generate_minute(minute, ts)
        self.aggregate_range(ts, ts + timedelta(minutes=1))
        return counts

    # -- fault 주입 -------------------------------------------------------

    def minute_of(self, ts: datetime) -> int:
        """anchor 기준 분 인덱스 — generate_minute 의 minute 와 같은 좌표계."""
        if self.anchor is None:
            raise RuntimeError("anchor 미설정 — 백필 시작 전에 anchor 를 정해야 한다")
        return int((ts - self.anchor).total_seconds() // 60)

    def set_faults(self, active: list[dict], rng: random.Random) -> tuple[list[Scenario], list[int]]:
        """활성 fault 목록을 Scenario 로 바꿔 생성기에 건다.

        반환: (적용된 Scenario 목록, 해석 실패한 fault_id 목록).

        해석은 `faults.resolve_active` 가 한다 — 상용 경로(`pemdb1.PemdbGen`)와
        **같은 함수**여야 같은 fault 가 같은 셀 집합에 걸린다. `rng` 인자는
        호환을 위해 남겨 두지만 쓰지 않는다(대상 해석은 fault_id 로 시드한
        전용 rng 로 돈다 — 호출 순서에 흔들리면 장애가 조각난다).
        """
        if self.anchor is None:
            raise RuntimeError("anchor 미설정 — set_faults 전에 anchor 를 정해야 한다")
        scs, bad = faults.resolve_active(
            self.cells, self.anchor, self.seed, active, self._fault_cache)
        self.gen.scenarios = scs
        # 프로파일 의존 대상(도메인·기종) 바인딩 — 캐시된 Scenario 는 이미 값이
        # 차 있어 다시 구르지 않는다 (_bind_scenario_targets 가 빈 것만 채운다)
        self.gen._bind_scenario_targets()
        return scs, bad
