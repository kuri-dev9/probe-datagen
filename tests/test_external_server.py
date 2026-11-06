"""외부 서버 5xx(6번 · `external_server`) — 자동 선택은 **전국**이다 (2026-11-05 · 수정안 A).

F-108 에서 SGW 하나(687셀)로 좁힌 5xx 가 도메인 축(U-25 · 셀 조건 없음)에서 그 SGW 의 트래픽 몫만큼 묽어져
3.4%(임계 5%)로 미탐지였다. 외부 서버 장애는 그 도메인 전국 가입자에게 동시에 보이므로(정답지 `WIDE`) 대상을
비우면 셀 범위 없이 전국에 건다. `SGW-n`·`ENB:`·`BOX:`·`DOMAIN:` 지정은 그 범위로 좁힌다. 임계·생산량은 그대로.

DB 를 쓰지 않는다 — 합성 셀 마스터(SGW 셋). 5xx 비율의 허용 범위는 에스터가 정했다(합성 규모 · 단일 시드).
"""

from __future__ import annotations

import random
import re
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from datagen import faults
from datagen import pemdb1 as P
from datagen import scenarios as S
from datagen.cells import Cell

ROOT = Path(__file__).resolve().parents[1]
T0 = datetime(2026, 11, 5, 10, 0)
START, DUR = 2, 5


def _cell(enb: str, n: int, lat: float, lon: float, sgw: str) -> Cell:
    return Cell(
        cell_key=f"{enb}-{n}", enb_id=enb, cell_id=n, enb_seq=0, c_uid="",
        tac=100, plmn="44010", vlan_id="", latitude=lat, longitude=lon,
        adm_sido_cd="11", adm_sido_nm="", adm_sgg_cd="11200", adm_sgg_nm="",
        adm_emd_cd="11200011", adm_emd_nm="", geo_status="MAPPED",
        rat="EUTRAN", mme_id="0001", sgw_id=sgw, pgw_id=sgw,
        ims_pgw_id=sgw, traffic_weight=1.0, display_name="")


def _sgw_of(e: int) -> str:
    """eNB 60개 — SGW 0038 이 가장 크다(30개) · 0037(20개) · 0039(10개)."""
    return "0038" if e < 30 else "0037" if e < 50 else "0039"


def _master() -> list[Cell]:
    out = []
    for e in range(60):
        lat = 35.60 + (e // 10) * 0.012
        lon = 139.60 + (e % 10) * 0.012
        out += [_cell(f"{600000 + e}", n, lat + n * 0.0015, lon + n * 0.0015, _sgw_of(e)) for n in range(3)]
    return out


def _fault(target=None, kind="external_server", intensity=1.0, fid=6):
    return {"fault_id": fid, "kind": kind, "target": target, "start_ts": T0 + timedelta(minutes=START),
            "duration_min": DUR, "intensity": intensity}


def _build(target=None, kind="external_server", seed=7, intensity=1.0):
    return faults.build(_fault(target, kind, intensity), _master(), T0, faults.target_rng(seed, 6), seed=seed)


# --------------------------------------------------------------------------
# 1. 대상 해석 — 자동은 전국, 지정은 그 범위
# --------------------------------------------------------------------------


def test_auto_target_is_nationwide_without_cells_or_node():
    sc = _build()
    assert sc is not None
    assert sc.target.cell_keys == set() and not sc.target.sgw_id and not sc.target.enb_id
    assert not (sc.target.pgw_id or sc.target.mme_id)


def test_external_server_is_no_longer_in_the_auto_node_table():
    assert "external_server" not in faults._AUTO_NODE


@pytest.mark.parametrize("kind,field", [("dns_degradation", "sgw_id"), ("cdn_degradation", "sgw_id"),
                                        ("pay_dns_delay", "sgw_id")])
def test_other_kinds_still_pick_the_busiest_sgw(kind, field):
    """5·21·22번은 이번에 바꾸지 않았다 — 자동 선택은 여전히 셀이 가장 많은 SGW 다."""
    assert faults._AUTO_NODE[kind] == field
    sc = _build(kind=kind)
    assert sc is not None and sc.target.sgw_id == "0038", sc.target.sgw_id
    assert sc.target.cell_keys == {c.cell_key for c in _master() if c.sgw_id == "0038"}


def test_sgw_target_narrows_to_that_sgw():
    sc = _build(target="SGW-0039")
    assert sc is not None
    assert sc.target.sgw_id == "0039"
    assert sc.target.cell_keys == {c.cell_key for c in _master() if c.sgw_id == "0039"}


def test_enb_target_narrows_to_that_enb():
    sc = _build(target="ENB:600003")
    assert sc.target.cell_keys == {"600003-0", "600003-1", "600003-2"}


def test_box_target_narrows_to_cells_in_the_box():
    sc = _build(target="BOX:35.595,139.595,35.625,139.625")
    assert sc is not None and sc.target.cell_keys
    cells = {c.cell_key: c for c in _master()}
    for k in sc.target.cell_keys:
        c = cells[k]
        assert 35.595 <= c.latitude <= 35.625 and 139.595 <= c.longitude <= 139.625
    assert len(sc.target.cell_keys) < len(cells)


def test_domain_target_is_nationwide_with_that_domain_only():
    sc = _build(target="DOMAIN:Example.COM")
    assert sc.target.domain == "example.com" and sc.target.cell_keys == set()


# --------------------------------------------------------------------------
# 2. 정답지
# --------------------------------------------------------------------------


def test_truth_of_auto_target_has_no_cells_no_node_and_stays_service_scoped():
    t = _build().as_truth()
    assert t["cells"] == [] and t["cell_cnt"] == 0
    assert t["expect_node"] is None
    assert t["scope_kind"] == "SERVICE" and t["expect_geo"] == "WIDE"
    assert t["truth_code"] == "EXTERNAL_SERVER" and t["expect_kpi"] == "U-25"
    assert "R-14b" in t["expect_rules"]


def test_truth_of_sgw_target_carries_the_sgw_cells_and_expect_node():
    t = _build(target="SGW-0039").as_truth()
    assert t["expect_node"] == "SGW-0039"
    assert t["cells"] == sorted(c.cell_key for c in _master() if c.sgw_id == "0039")
    assert t["cell_cnt"] == len(t["cells"]) == 30
    assert t["scope_kind"] == "SERVICE"


def test_truth_scope_label_names_the_domain_once_it_is_bound(monkeypatch):
    """도메인이 정해지면 `scope` 는 그 도메인이다 — 비었을 때만 "전국" 이다(`Target.label`)."""
    t = S.Target()
    assert t.label() in ("전국", "")
    t.domain = "google.com"
    assert "google.com" in t.label()


# --------------------------------------------------------------------------
# 3. 레코드 — 전국의 대상 도메인 http 5xx · 지정이면 그 셀만
# --------------------------------------------------------------------------


@pytest.fixture
def make(monkeypatch):
    def build(rate: int = 1500):
        for f in ("pemdb1/_distributions.json", "profile.json"):
            if not (ROOT / "data" / f).exists():
                pytest.fail(f"data/{f} 가 없다")
        monkeypatch.setattr(P, "load_cells", lambda *a, **k: _master())
        monkeypatch.setattr(P, "load_apps", lambda conn: None)
        g = P.PemdbGen(None, ROOT / "data", rates={"lte_call": rate, "transport": rate, "http": rate},
                       seed=11, subscriber_pool=2500)
        g.anchor = T0
        return g
    return build


def _run_http(g, target=None, intensity=1.0, minutes=START + DUR + 1):
    scs, bad = g.set_fault_rows([_fault(target, intensity=intensity)])
    assert scs and not bad, bad
    rows = []
    for m in range(minutes):
        ts = T0 + timedelta(minutes=m)
        g.records(m, ts)
        rows.append(g.uplane_records(m, ts).get("http", []))
    return scs[0], rows


def _is5xx(r, ix):
    try:
        return 500 <= int(float(r[ix["StatusCode"]])) < 600
    except ValueError:
        return False


def _rate(rows, ix, host=None, only_sgw=None, exclude_sgw=None):
    sel = [r for r in rows
           if (host is None or host in (r[ix["Host"]] or ""))
           and (only_sgw is None or r[ix["SGW_ID"]] == only_sgw)
           and (exclude_sgw is None or r[ix["SGW_ID"]] != exclude_sgw)]
    return (sum(1 for r in sel if _is5xx(r, ix)) / len(sel) if sel else None), len(sel)


def test_auto_target_makes_the_top_domain_5xx_nationwide_at_intensity_times_085(make):
    g = make()
    sc, rows = _run_http(g)
    ix = g.gen.index["http"]
    dom = sc.target.domain
    assert dom, "자동 선택이 도메인을 정하지 못했다"
    during = [r for m in range(START, START + DUR) for r in rows[m]]
    r, n = _rate(during, ix, host=dom)
    assert n >= 100, f"대상 도메인 표본이 적다({n})"
    assert 0.70 <= r <= 0.95, f"{dom}: 5xx 비율 {r:.2f} (기대 약 0.85)"
    # **어느 SGW 에서도** 보인다 — 전국이다(예전에는 가장 큰 SGW 0038 에서만 났다)
    for sgw in ("0037", "0038", "0039"):
        rs, ns = _rate(during, ix, host=dom, only_sgw=sgw)
        assert ns >= 10 and rs >= 0.5, f"SGW {sgw}: 5xx 비율 {rs} (표본 {ns}) — 전국이 아니다"


def test_other_domains_and_the_time_before_the_fault_are_untouched(make):
    g = make()
    sc, rows = _run_http(g)
    ix = g.gen.index["http"]
    dom = sc.target.domain
    before = [r for m in range(START) for r in rows[m]]
    during = [r for m in range(START, START + DUR) for r in rows[m]]
    other_during = [r for r in during if dom not in (r[ix["Host"]] or "")]
    base_other, _ = _rate([r for r in before if dom not in (r[ix["Host"]] or "")], ix)
    r_other, n_other = _rate(other_during, ix)
    assert n_other > 100
    assert r_other <= (base_other or 0) + 0.02, f"다른 도메인 5xx {r_other:.3f} (평소 {base_other:.3f})"
    r_before, _n = _rate(before, ix, host=dom)
    assert (r_before or 0) <= 0.05, f"장애 전 대상 도메인 5xx {r_before}"


def test_after_the_fault_ends_the_5xx_goes_back(make):
    g = make()
    sc, rows = _run_http(g, minutes=START + DUR + 3)
    ix = g.gen.index["http"]
    after = [r for m in range(START + DUR + 1, START + DUR + 3) for r in rows[m]]
    r, n = _rate(after, ix, host=sc.target.domain)
    assert n >= 30 and r <= 0.05, f"끝난 뒤 5xx {r}"


def test_intensity_scales_the_5xx_share(make):
    g = make()
    sc, rows = _run_http(g, intensity=0.2)
    ix = g.gen.index["http"]
    during = [r for m in range(START, START + DUR) for r in rows[m]]
    r, n = _rate(during, ix, host=sc.target.domain)
    assert 0.10 <= r <= 0.25, f"강도 0.2 → 5xx {r:.2f} (기대 약 0.17)"


def test_sgw_target_only_changes_that_sgws_records(make):
    g = make()
    sc, rows = _run_http(g, target="SGW-0039")
    ix = g.gen.index["http"]
    dom = sc.target.domain
    during = [r for m in range(START, START + DUR) for r in rows[m]]
    inside, ni = _rate(during, ix, host=dom, only_sgw="0039")
    outside, no = _rate(during, ix, host=dom, exclude_sgw="0039")
    assert ni >= 10 and no >= 30
    assert inside >= 0.6, f"지정 SGW 안 5xx {inside:.2f}"
    assert outside <= 0.05, f"지정 SGW 밖 5xx {outside:.2f} — 지정이 전국으로 번졌다"


def test_domain_target_changes_only_that_domain(make):
    g = make()
    probe, rows0 = _run_http(g)                           # 평소 도메인 하나를 알아내려 한 번 돌린다
    dom_auto = probe.target.domain
    ix = g.gen.index["http"]
    other = next((r[ix["Host"]] for r in rows0[0] if dom_auto not in (r[ix["Host"]] or "") and r[ix["Host"]]), None)
    assert other
    g2 = make()
    host = re.sub(r"^[^.]+\.", "", other) if other.count(".") >= 2 else other   # 부분 문자열 규칙
    sc, rows = _run_http(g2, target=f"DOMAIN:{host}")
    ix2 = g2.gen.index["http"]
    during = [r for m in range(START, START + DUR) for r in rows[m]]
    hit = [r for r in during if host in (r[ix2["Host"]] or "")]
    miss = [r for r in during if host not in (r[ix2["Host"]] or "")]
    assert hit and sum(1 for r in hit if _is5xx(r, ix2)) / len(hit) >= 0.6
    assert sum(1 for r in miss if _is5xx(r, ix2)) / len(miss) <= 0.05


# --------------------------------------------------------------------------
# 4. 도메인 자동 선택 · 장비 표시 · gen-ui
# --------------------------------------------------------------------------


def test_auto_domain_is_the_top_traffic_domain(make):
    """도메인 자동 선택은 그대로다 — `_top_traffic_domain`(사전이 있으면) · 없으면 프로파일 1위."""
    g = make()
    scs, _ = g.set_fault_rows([_fault()])
    dom = scs[0].target.domain
    top = g._top_traffic_domain() if getattr(g, "app_book", None) else None
    doms = g.gen.profiles["http"].domains
    assert dom == (top or (doms[0] if doms else "cdn.example.com"))


def test_equipment_display_for_number_6_still_includes_sgw():
    from datagen import control
    row = next(r for r in control.SCENARIOS if r[0] == 6)
    assert row[2] == "external_server" and "SGW" in row[4], row


def test_control_comment_records_the_nationwide_change():
    src = (ROOT / "src" / "datagen" / "control.py").read_text()
    assert "전국" in src and "F-108" in src and "3.4%" in src


def test_gen_ui_labels_auto_as_nationwide_only_for_external_server():
    ui = (ROOT / "src" / "datagen" / "ui.py").read_text()
    m = re.search(r'const AUTO_NATIONWIDE = new Set\(\[(.*?)\]\)', ui)
    assert m, "AUTO_NATIONWIDE 가 없다"
    kinds = set(re.findall(r'"([a-z_0-9]+)"', m.group(1)))
    assert kinds == {"external_server"}, kinds
    assert 'T("자동 선택 (전국)")' in ui and 'T("자동 선택")' in ui
    assert '"자동 선택 (전국)": "自動選択 (全国)"' in ui, "일본어 번역이 없다"
