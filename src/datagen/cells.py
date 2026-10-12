"""셀 마스터 생성 (CM 연동 §1.2.1 / §3.6의 시뮬레이션 대체물).

실제 CM이 없으므로 전국 시군구에 셀을 배치해 마스터를 만든다. 배치는 인구·도심
가중이며 좌표는 읍면동 폴리곤 내부에서 뽑으므로, 지리 롤업과 공간 군집 탐지가
현실과 비슷한 밀도 위에서 동작한다.

**식별자 표현은 제공된 실 샘플을 그대로 따른다.** 샘플의 ``eNB_ID``는 base64
암호문(``bN6ZUsFBJNqmIRppABK8qQ==``)이고 ``Cell_ID``는 평문 소수(3, 41…)다. 즉
§3.2.4의 ECGI 비트 파싱이 성립하지 않으며(부록 A-41), 셀 키는 ``eNB_ID`` +
``Cell_ID`` 복합키로만 다룰 수 있다. 시뮬레이터도 같은 제약을 재현한다 —
암호화가 형식을 깨뜨렸을 때 Macro/Femto 판별이 왜 불가능한지가 데이터에 그대로
드러나야 검증이 된다.
"""

from __future__ import annotations

import base64
import hashlib
import json
import random
from dataclasses import asdict, dataclass
from pathlib import Path


#: Core 노드 풀 — §1.2.4의 수동 등록 목록에 대응한다.
MME_COUNT = 24
SGW_COUNT = 48
PGW_COUNT = 48
IMS_PGW_COUNT = 16

PLMN_POOL = ["45005", "45011", "45008"]
PLMN_WEIGHTS = [0.72, 0.18, 0.10]


def encrypt_id(value: str, salt: str = "graphio") -> str:
    """샘플과 같은 형태의 결정적 암호문(base64 22자 + ``==``).

    결정적이므로 동일 셀은 항상 동일 암호문이 되고, 가입자 단위 집계·중복 제거
    (§3.2.11)가 성립한다. 형식은 보존되지 않으므로 ECGI 파싱은 불가능하다.
    """
    digest = hashlib.sha256(f"{salt}:{value}".encode()).digest()[:16]
    return base64.b64encode(digest).decode()


@dataclass
class Cell:
    cell_key: str          # 화면·조인의 단위 = eNB_ID + '-' + Cell_ID (§3.2.4)
    enb_id: str            # 암호문
    cell_id: int           # 평문 섹터 번호 (단독으로는 유일하지 않다)
    enb_seq: int           # 시뮬레이터 내부 사이트 일련번호
    c_uid: int             # interval_First_eNB_C_UID — 샘플에서 평문으로 확인됨
    tac: int
    plmn: str
    vlan_id: str
    latitude: float
    longitude: float
    adm_sido_cd: str
    adm_sido_nm: str
    adm_sgg_cd: str
    adm_sgg_nm: str
    adm_emd_cd: str
    adm_emd_nm: str
    geo_status: str        # MAPPED | UNMAPPED | UNKNOWN_CELL
    rat: str               # EUTRAN | NR
    mme_id: int
    sgw_id: int
    pgw_id: int
    ims_pgw_id: int
    traffic_weight: float  # 상대 트래픽 규모 — 도심 셀일수록 크다
    display_name: str


# generate_cells() 는 가져오지 않았다 — 행정경계 인덱스(graphio 본체의 geo.py +
# data/geo 57MB)에 의존한다. 셀 세트가 필요하면 구 저장소에서 만들어 JSON 으로
# 가져온다 (data/cells.json). 여기는 **읽기 전용**이다.


def core_nodes(cells: list[Cell]) -> list[dict]:
    """§1.2.4 Core 노드 수동 등록 목록 — 관측된 식별자에 표시명을 부여한다."""
    seen: dict[tuple[str, int], dict] = {}
    for c in cells:
        region = c.adm_sido_nm or "미상"
        for ntype, nid in (
            ("MME", c.mme_id),
            ("SGW", c.sgw_id),
            ("PGW", c.pgw_id),
            ("ims_PGW", c.ims_pgw_id),
        ):
            key = (ntype, nid)
            entry = seen.setdefault(
                key,
                {
                    "node_type": ntype,
                    "node_id": nid,
                    "display_name": f"{ntype}-{nid:03d}",
                    "region_label": region,
                    "vendor": "",
                    "note": "",
                    "cell_cnt": 0,
                },
            )
            entry["cell_cnt"] += 1
    return sorted(seen.values(), key=lambda e: (e["node_type"], e["node_id"]))


def save_cells(cells: list[Cell], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps([asdict(c) for c in cells], ensure_ascii=False), encoding="utf-8"
    )
    return path


def load_cells(path: Path) -> list[Cell]:
    return [Cell(**row) for row in json.loads(path.read_text(encoding="utf-8"))]
