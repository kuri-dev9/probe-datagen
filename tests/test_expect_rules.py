"""정답지 `expect_rules` · `expect_alternatives` (2026-11-05 · 케이 잠정 결정 "(가)").

graphio 채점(`score`)은 `expect_rules` 를 읽지 않는다 — 카드·사람 확인용이다. 그래서 뜻을 정리했다:
  * `expect_rules`        정답 원인 코드를 내는 룰만
  * `expect_alternatives` 설 것으로 예상되는 경쟁 후보·보조 룰 — `{룰: 그 룰의 원인 코드}`
둘은 겹치지 않는다. 정답지(`as_truth`)와 `write_truth` 파일에 둘 다 실린다.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from datagen import faults
from datagen import pemdb1 as P
from datagen import scenarios as S
from datagen.cells import Cell

T0 = datetime(2026, 11, 5, 3, 0)

RULES = {
    "gw_resource": ("R-05",),
    "gw_node_down": ("R-04",),
    "gw_pgw_restart": ("R-05b",),
    "mme_overload": ("R-23",),
    "change_suspected": ("R-11",),
    "son_param_change": ("R-11",),
    "auc_no_response": ("R-00b", "R-15"),
    "s6a_undetermined": ("R-00b",),
    "ran_radio": ("R-06", "R-22"),
    "external_server": ("R-14b",),
}

ALTERNATIVES = {
    "gw_resource": {"R-21": "SESSION_REJECT", "R-18": "UNDETERMINED", "R-00": "UNDETERMINED"},
    "gw_node_down": {"R-00b": "NF_UNREACHABLE", "R-15": "NF_UNREACHABLE"},
    "gw_pgw_restart": {"R-15": "NF_UNREACHABLE"},
    "mme_overload": {"R-18": "UNDETERMINED", "R-15": "NF_UNREACHABLE"},
    "change_suspected": {"R-21": "SESSION_REJECT"},
    "son_param_change": {"R-17": "RAN_MOBILITY_ISSUE"},
    "auc_no_response": {},
    # 27번: 세 신호 판정(R-46)이 정답이고, 무호만 본 노드 장애(R-36)는 대안으로 남는다(11-05)
    "natural_disaster": {"R-36": "RAN_NODE_DOWN"},
}


def _cell(enb, n, lat, lon, sgw):
    return Cell(cell_key=f"{enb}-{n}", enb_id=enb, cell_id=n, enb_seq=0, c_uid="", tac=100, plmn="44010",
                vlan_id="", latitude=lat, longitude=lon, adm_sido_cd="11", adm_sido_nm="", adm_sgg_cd="11200",
                adm_sgg_nm="", adm_emd_cd="11200011", adm_emd_nm="", geo_status="MAPPED", rat="EUTRAN",
                mme_id="0001", sgw_id=sgw, pgw_id=sgw, ims_pgw_id=sgw, traffic_weight=1.0, display_name="")


def _cells():
    out = []
    for e in range(60):
        out += [_cell(f"{600000 + e}", n, 35.60 + (e // 10) * 0.012 + n * 0.0015,
                      139.60 + (e % 10) * 0.012 + n * 0.0015, "0038" if e < 30 else "0037") for n in range(3)]
    return out


def _sc(kind, target=None):
    row = {"fault_id": 1, "kind": kind, "target": target, "start_ts": T0 + timedelta(minutes=2),
           "duration_min": 10, "intensity": 1.0}
    return faults.build(row, _cells(), T0, faults.target_rng(7, 1), seed=7)


# --------------------------------------------------------------------------
# 1. expect_rules — 맥스 표와 같다
# --------------------------------------------------------------------------


@pytest.mark.parametrize("kind,rules", sorted(RULES.items()))
def test_expect_rules_match_the_agreed_table_in_spec_and_truth(kind, rules):
    assert tuple(S.KIND_SPECS[kind]["expect_rules"]) == rules
    sc = _sc(kind)
    assert sc is not None
    assert sc.as_truth()["expect_rules"] == list(rules)


def test_rules_dropped_by_the_cleanup_are_gone():
    sp = S.KIND_SPECS
    assert "R-04" not in sp["gw_pgw_restart"]["expect_rules"], "PGW 재기동은 S11 응답 거절이라 R-04(무응답)가 아니다"
    assert not {"R-02", "R-15", "R-21"} & set(sp["gw_resource"]["expect_rules"])
    assert not {"R-16", "R-20"} & set(sp["ran_radio"]["expect_rules"]), "R-16 은 전송 손실 · R-20 은 절단만 나는 장면용"
    assert "R-09b" not in sp["external_server"]["expect_rules"]
    assert "R-01" not in sp["auc_no_response"]["expect_rules"] and "R-01" not in sp["s6a_undetermined"]["expect_rules"]
    assert "R-15" not in sp["gw_node_down"]["expect_rules"] and "R-00b" not in sp["gw_node_down"]["expect_rules"]
    assert not {"R-18", "R-15"} & set(sp["mme_overload"]["expect_rules"])
    assert "R-03" not in sp["change_suspected"]["expect_rules"] and "R-17" not in sp["son_param_change"]["expect_rules"]


def test_external_path_latency_keeps_r09b_for_the_kind_that_owns_it():
    assert S.KIND_SPECS["external_path_latency"]["expect_rules"] == ("R-09b",)


def test_untouched_kinds_keep_their_rules():
    sp = S.KIND_SPECS
    assert sp["auth_failure"]["expect_rules"] == ("R-01",) and sp["dns_degradation"]["expect_rules"] == ("R-14",)
    assert sp["compound_causal_failure"]["expect_rules"] == ("R-09", "R-16", "R-14"), "복합 장애는 그대로"
    assert sp["transport_loss"]["expect_rules"] == ("R-09", "R-16")


# --------------------------------------------------------------------------
# 2. expect_alternatives
# --------------------------------------------------------------------------


@pytest.mark.parametrize("kind,alt", sorted(ALTERNATIVES.items()))
def test_alternatives_of_the_seven_kinds_match_the_table(kind, alt):
    sc = _sc(kind)
    assert sc.as_truth()["expect_alternatives"] == alt
    assert dict(sc.expect_alternatives) == alt


def test_scenario_default_is_an_empty_dict_not_shared_between_instances():
    sc1, sc2 = _sc("auth_failure"), _sc("auth_failure")
    assert sc1.as_truth()["expect_alternatives"] == {} == sc2.as_truth()["expect_alternatives"]
    sc1.expect_alternatives["R-99"] = "X"
    assert "R-99" not in sc2.expect_alternatives, "인스턴스끼리 사전을 공유한다"


def test_as_truth_returns_a_copy_of_the_alternatives():
    sc = _sc("gw_node_down")
    t = sc.as_truth()
    t["expect_alternatives"]["R-77"] = "Y"
    assert "R-77" not in sc.as_truth()["expect_alternatives"]


def test_every_other_kind_has_an_empty_alternatives_dict_in_its_truth():
    seen = 0
    for kind in S.KIND_SPECS:
        if kind in ALTERNATIVES:
            continue
        sc = _sc(kind)
        if sc is None:
            continue
        seen += 1
        assert sc.as_truth()["expect_alternatives"] == {}, kind
    assert seen >= 20


def test_alternative_values_are_cause_codes_and_never_the_kinds_own_truth_code():
    """대안 룰은 **다른** 원인 코드를 낸다 — 정답 코드를 내는 룰은 `expect_rules` 로 간다."""
    for kind, alt in ALTERNATIVES.items():
        own = S.KIND_SPECS[kind]["truth_code"]
        for rule, code in alt.items():
            assert code != own, f"{kind}: {rule} 은 정답 코드({own})를 내는데 alternatives 에 있다"
            assert code.isupper() and rule.startswith("R-")


# --------------------------------------------------------------------------
# 3. 겹침 없음
# --------------------------------------------------------------------------


def test_rules_and_alternatives_never_overlap_for_any_kind():
    bad = {k: sorted(set(s.get("expect_rules", ())) & set(s.get("expect_alternatives") or {}))
           for k, s in S.KIND_SPECS.items()}
    assert not {k: v for k, v in bad.items() if v}, bad


def test_truth_of_every_kind_has_no_overlap_either():
    for kind in S.KIND_SPECS:
        sc = _sc(kind)
        if sc is None:
            continue
        t = sc.as_truth()
        assert not set(t["expect_rules"]) & set(t["expect_alternatives"]), kind


def test_rules_are_tuples_of_rule_ids_without_duplicates():
    for kind, s in S.KIND_SPECS.items():
        rules = list(s.get("expect_rules", ()))
        assert len(rules) == len(set(rules)), kind
        assert all(r.startswith("R-") for r in rules), kind
        assert all(r.startswith("R-") for r in (s.get("expect_alternatives") or {})), kind


# --------------------------------------------------------------------------
# 4. 정답지 파일
# --------------------------------------------------------------------------


def test_write_truth_file_carries_both_keys(tmp_path):
    scs = [_sc(k) for k in ("gw_resource", "gw_pgw_restart", "auth_failure")]
    path = tmp_path / "truth.json"
    P.write_truth(path, T0, 40, scs)
    got = {s["kind"]: s for s in json.loads(path.read_text())["scenarios"]}
    assert got["gw_resource"]["expect_rules"] == ["R-05"]
    assert got["gw_resource"]["expect_alternatives"] == ALTERNATIVES["gw_resource"]
    assert got["gw_pgw_restart"]["expect_rules"] == ["R-05b"]
    assert got["gw_pgw_restart"]["expect_alternatives"] == {"R-15": "NF_UNREACHABLE"}
    assert got["auth_failure"]["expect_alternatives"] == {}


def test_graphio_does_not_read_these_keys_so_old_files_stay_valid():
    """채점이 읽지 않는다는 사실을 문서로 고정한다 — graphio `src/` 에 사용처가 없다(맥스 · 케이 확인 11-05)."""
    from pathlib import Path
    root = Path(__file__).resolve().parents[2] / "graphio-probe-rca" / "src"
    if not root.exists():
        pytest.skip("graphio 저장소가 옆에 없다")
    hits = [str(p) for p in root.rglob("*.py") if "expect_rules" in p.read_text(encoding="utf-8", errors="ignore")
            or "expect_alternatives" in p.read_text(encoding="utf-8", errors="ignore")]
    assert not hits, f"graphio 가 정답지 룰 키를 읽는다 — 이 시험의 전제가 깨졌다: {hits}"


def test_change_suspected_alternative_is_r21_not_r03():
    """F-125 실측(2026-11-05) — 경쟁 후보는 R-21 SESSION_REJECT 다.

    R-03(`BEARER_SETUP_ERROR`)은 첫 오류 구간이 NAS-ESM 40% 이상이어야 서는데 이 장애의 첫
    오류는 S11(99.73%)이고 ESM 30 은 그 거절의 하류 증상이다(graphio 불변규칙 7). 서지 않는
    룰을 대안으로 적으면 정답지가 존재하지 않는 경쟁을 기대한다.
    """
    from datagen.scenarios import KIND_SPECS

    alt = KIND_SPECS["change_suspected"]["expect_alternatives"]
    assert alt == {"R-21": "SESSION_REJECT"}
    assert "R-03" not in alt
    assert KIND_SPECS["change_suspected"]["expect_rules"] == ("R-11",)
    assert KIND_SPECS["change_suspected"]["grade"] == "TOP3"
