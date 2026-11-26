"""셀별 상주 인구(주거)를 굽는다 — 추가 가입자를 주택가 쪽으로 더 두기 위한 표 (2026-11-26 사용자 결정 11).

**왜.** 사용자 「추가 가입자 위치를 각 셀마다 분배해. 그리고 주택가 쪽으로 더 많이 배치」. 추가 가입자의
머무는 셀은 eNB 균등(`_make_extra_subs` 의 `flat_weights`)이라 셀마다 나뉘기는 하지만 주택가 가중이 없었다.

**근거 — 국세조사 町丁目 인구(기본 · `--source estat`).** 지도용으로 이미 받아 둔 e-Stat 2020 국세조사 소지역
경계(`graphio-probe-rca/data/_geo_src/estat-r2ka{11,12,13,14}/*.dbf`)에 町丁目마다 상주 인구(`JINKO`)와 대표점
(`X_CODE`·`Y_CODE`)이 있다. 상주 인구가 곧 「사는 곳」이다. **읽기만 한다.** 셀 좌표점이 든 町丁目 경계(`.shp`)를
찾아 그 町丁目 인구를 안에 든 좌표점들이 똑같이 나눈다(`_estat`).

**OSM 건물 태그(`--source osm`)는 비교용으로만 남긴다** (2026-11-26 실측). 8m 건물 원본은 높이·층수 태그가 **달린**
건물만 받아 온 것이라(`build_jp_geo_layers.py` 의 `q_of`) 태그 입력이 활발한 구에 몰린다 — 주거 건물 수 1~3위가
台東·豊島·荒川 이고 가장 주택가인 世田谷 는 상위에 없다. 셀 4,361/6,929 개가 주거 0 이었다. 주택가가 아니라
**지도 입력 밀도**를 센다.

**범위.** 셀 마스터의 구(`CM_GU*` 의 시도·구 이름)와 같은 시도·시구정촌 이름의 町丁目(건물)만 센다. 범위 밖 인구를
가장 가까운 셀에 붙이면 경계 셀이 사이타마·가나가와 인구를 다 받는다.

**쓰는 쪽.** `pemdb1.PemdbGen._make_extra_subs` 가 기동할 때 한 번 읽어 셀 가중을
`eNB 균등 × (1 + 그 셀 주거 수 / 셀 평균 주거 수)` 로 둔다. 파일이 없으면 지금처럼 eNB 균등이다.

    cd probe-datagen
    ( set -a; . ../graphio-probe-rca/.env; set +a; .venv/bin/python tools/bake_home_resid.py --config config/local.yaml )

출력은 `data/home_resid_<locale>.json`(git 밖 — 셀 키는 eNB 번호다). gen 이미지가 `COPY data/` 로 담으므로
**반영은 gen 재빌드**다.
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "src"))

SRC = HERE.parent / "graphio-probe-rca" / "data" / "_geo_src"
#: OSM 주거 분류(wiki `Key:building` 의 Accommodation 묶음). `allotment_house`(텃밭 창고)·`yes`(용도 모름)는 뺀다
RESID_TAGS = frozenset(("house", "apartments", "residential", "detached", "semidetached_house",
                        "terrace", "dormitory", "bungalow"))


def _dbf(path: Path) -> list[dict[str, str]]:
    """dBASE III 표를 읽는다 — e-Stat 경계 속성(cp932). 의존성을 늘리지 않으려고 직접 읽는다"""
    import struct
    b = path.read_bytes()
    n = struct.unpack("<I", b[4:8])[0]
    hl, rl = struct.unpack("<HH", b[8:12])
    fs, o = [], 1
    for i in range((hl - 33) // 32):
        d = b[32 + i * 32:64 + i * 32]
        fs.append((d[:11].split(b"\0")[0].decode(), o, d[16]))
        o += d[16]
    return [{k: b[hl + r * rl + s:hl + r * rl + s + ln].decode("cp932", "replace").strip() for k, s, ln in fs}
            for r in range(n)]


def _shp(path: Path) -> list[list[np.ndarray]]:
    """Polygon(형 5) 레코드마다 고리 목록. 순서는 같은 이름 `.dbf` 의 행 순서와 같다"""
    import struct
    b = path.read_bytes()
    out, o = [], 100
    while o < len(b):
        ln = struct.unpack(">i", b[o + 4:o + 8])[0] * 2
        c = b[o + 8:o + 8 + ln]
        o += 8 + ln
        if struct.unpack("<i", c[:4])[0] != 5:
            out.append([])
            continue
        npart, npt = struct.unpack("<ii", c[36:44])
        parts = list(struct.unpack(f"<{npart}i", c[44:44 + 4 * npart])) + [npt]
        xy = np.frombuffer(c, dtype="<f8", count=2 * npt, offset=44 + 4 * npart).reshape(-1, 2)
        out.append([xy[parts[i]:parts[i + 1]] for i in range(npart)])
    return out


def _inside(rings: list[np.ndarray], x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """짝홀 규칙 — 구멍 고리도 같은 규칙으로 빠진다"""
    hit = np.zeros(len(x), dtype=bool)
    for r in rings:
        x0, y0, x1, y1 = r[:-1, 0], r[:-1, 1], r[1:, 0], r[1:, 1]
        cr = (y0[None, :] > y[:, None]) != (y1[None, :] > y[:, None])
        with np.errstate(divide="ignore", invalid="ignore"):
            xi = x0[None, :] + (y[:, None] - y0[None, :]) * (x1 - x0)[None, :] / (y1 - y0)[None, :]
        hit ^= (np.count_nonzero(cr & (x[:, None] < xi), axis=1) % 2).astype(bool)
    return hit


def _estat(src: Path, wards: set[tuple[str, str]], P: np.ndarray, kx: float) -> tuple[np.ndarray, Counter]:
    """셀 좌표점(``P`` = [[위도, 경도]…])마다 상주 인구 몫과, 구별 인구.

    町丁目 경계 안에 든 셀 좌표점들이 그 町丁目 인구를 똑같이 나눈다. 셀 좌표점이 하나도 없는 町丁目(작은 町丁目)은
    대표점에서 가장 가까운 셀 좌표점에 준다 — 버리면 그 인구가 사라진다"""
    per = np.zeros(len(P))
    by: Counter = Counter()
    for f in sorted(glob.glob(str(src / "estat-r2ka*" / "*.dbf"))):
        rows, shapes = _dbf(Path(f)), _shp(Path(f).with_suffix(".shp"))
        for r, rings in zip(rows, shapes):
            v = float(r["JINKO"] or 0)
            if (r["PREF_NAME"], r["CITY_NAME"]) not in wards or v <= 0 or not rings:
                continue
            by[r["CITY_NAME"]] += v
            allp = np.concatenate(rings)
            m = ((P[:, 1] >= allp[:, 0].min()) & (P[:, 1] <= allp[:, 0].max())
                 & (P[:, 0] >= allp[:, 1].min()) & (P[:, 0] <= allp[:, 1].max()))
            idx = np.flatnonzero(m)
            if len(idx):
                idx = idx[_inside(rings, P[idx, 1], P[idx, 0])]
            if len(idx):
                per[idx] += v / len(idx)
            else:
                d = (P[:, 0] - float(r["Y_CODE"])) ** 2 + ((P[:, 1] - float(r["X_CODE"])) * kx) ** 2
                per[int(d.argmin())] += v
    return per, by


def _buildings(src: Path, box) -> tuple[list[tuple[float, float, float]], Counter]:
    """(비교용) 주거 건물 중심 목록과 태그 분포. 타일이 겹치므로 (type, id) 로 한 번만 센다.
    구 경계를 갖고 있지 않아 범위는 셀 좌표의 외접 사각형(``box``)으로만 자른다"""
    seen: set = set()
    tags: Counter = Counter()
    pts: list[tuple[float, float]] = []
    for f in sorted(glob.glob(str(src / "osm_bldg8-*.json"))):
        for e in json.loads(Path(f).read_text(encoding="utf-8"))["elements"]:
            k = (e["type"], e["id"])
            if k in seen:
                continue
            seen.add(k)
            t = (e.get("tags") or {}).get("building")
            tags[t] += 1
            b = e.get("bounds")
            if t in RESID_TAGS and b:
                y, x = (b["minlat"] + b["maxlat"]) / 2, (b["minlon"] + b["maxlon"]) / 2
                if box[0] <= y <= box[2] and box[1] <= x <= box[3]:
                    pts.append((y, x, 1.0))
    return pts, tags


def _cells(cfg_path: str | None):
    import pymysql

    from datagen import cli
    from datagen import pemdb1 as P

    cfg = cli.load_cfg(cfg_path)
    my = cfg["mysql"]
    conn = pymysql.connect(host=my["host"], port=int(my["port"]), user=my["user"],
                           password=my["password"], database=(cfg.get("pemdb1") or {}).get("database", "pemdb6"),
                           charset="utf8mb3", autocommit=False)
    try:
        cli.apply_region_override(cfg, conn)
        pem = cfg.get("pemdb1") or {}
        drawable = None
        if pem.get("geo_crosswalk"):
            q = Path(pem["geo_crosswalk"])
            drawable = P.load_drawable_dongs(q if q.is_absolute() else (HERE / q).resolve())
        locale = pem.get("locale", "kr")
        cells = P.load_cells(conn, enb_limit=int(pem.get("enb_limit", 0)), drawable=drawable,
                             ensure_coords=False, plmn=P.Plmn.from_cfg(cfg.get("plmn")),
                             locale=locale, tac_only=bool(pem.get("tac_only", False)),
                             sectors_per_enb=pem.get("sectors_per_enb"), scope=P.Scope.from_cfg(pem.get("scope")))
        gu = P._load_gu_names(conn, locale)
        extra = int(pem.get("extra_lte_max", 100_000))
    finally:
        conn.rollback()
        conn.close()
    return cells, gu, locale, extra


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", default=None, help="datagen.yaml 위에 덮을 설정(예: config/local.yaml)")
    ap.add_argument("--extra", type=int, default=80_000, help="구별 비교에 쓸 추가 가입자 수(보고용)")
    ap.add_argument("--source", choices=("estat", "osm"), default="estat", help="estat=국세조사 인구 · osm=건물 태그(비교용)")
    ap.add_argument("--dry-run", action="store_true", help="파일을 쓰지 않는다")
    a = ap.parse_args(argv)

    cells, gu, locale, _ = _cells(a.config)
    wards = {gu[c.adm_sgg_cd] for c in cells if c.adm_sgg_cd in gu}
    # 좌표 있는 셀만 — 좌표 없는 셀은 주거 0 (지금 몫 그대로)
    with_xy = [c for c in cells if c.latitude is not None and c.longitude is not None]
    by_xy: dict[tuple[float, float], list] = defaultdict(list)
    for c in with_xy:
        by_xy[(round(c.latitude, 6), round(c.longitude, 6))].append(c)
    keys = list(by_xy)
    P = np.array(keys)
    # 등장방형 근사 — 도쿄 권역 몇십 km 에서 가장 가까운 점을 고르는 데는 충분하다
    kx = math.cos(math.radians(float(P[:, 0].mean())))
    if a.source == "estat":
        per_pt, tags = _estat(SRC, wards, P, kx)
        miss = sorted(w[1] for w in wards if w[1] not in tags)
        print(f"인구 {sum(tags.values()):,.0f} · 구 {len(wards)} 중 짝 없는 구 {miss or '없음'}")
        pts = per_pt
    else:
        pts, tags = _buildings(SRC, (P[:, 0].min(), P[:, 1].min(), P[:, 0].max(), P[:, 1].max()))
        print(f"건물 {sum(tags.values()):,}동 · 범위 안 주거 {len(pts):,}동 · 태그 상위 {tags.most_common(6)}")
        B = np.array([(y, x) for y, x, _ in pts])
        near = np.empty(len(B), dtype=np.int64)
        for s in range(0, len(B), 4096):
            d = (B[s:s + 4096, None, 0] - P[None, :, 0]) ** 2 + ((B[s:s + 4096, None, 1] - P[None, :, 1]) * kx) ** 2
            near[s:s + 4096] = d.argmin(axis=1)
        per_pt = np.bincount(near, minlength=len(keys)).astype(float)
    counts: dict[str, float] = {}
    for i, k in enumerate(keys):
        cs = by_xy[k]
        for c in cs:
            counts[c.cell_key] = float(per_pt[i]) / len(cs)

    # ── 보고 — 가중과 구별 기대 인원(예전 eNB 균등 대비) ──
    per_enb = Counter(c.enb_id for c in cells)
    flat = [1.0 / per_enb[c.enb_id] for c in cells]
    r = [counts.get(c.cell_key, 0.0) for c in cells]
    mean_r = sum(r) / len(r)
    new = [f * (1.0 + x / mean_r) for f, x in zip(flat, r)] if mean_r > 0 else flat
    zero = sum(1 for x in r if x == 0)

    def gname(c) -> str:
        v = gu.get(c.adm_sgg_cd) if c.adm_sgg_cd else None
        return v[1] if v else (c.adm_sgg_nm or c.adm_sgg_cd or "?")

    print(f"셀 {len(cells):,} · 좌표 있는 셀 {len(with_xy):,} · 고유 좌표 {len(keys):,} · 주거 0 셀 {zero:,} · 셀 평균 주거 {mean_r:,.1f}")
    top = sorted(range(len(cells)), key=lambda i: -new[i] / flat[i])[:5]
    print("가중 배율(새/예전) 상위 5:")
    for i in top:
        c = cells[i]
        print(f"  {c.cell_key[:10]}…  {gname(c)}  주거 {r[i]:,.0f}  배율 ×{new[i] / flat[i]:.2f}")
    old_g: Counter = Counter()
    new_g: Counter = Counter()
    for c, f, w in zip(cells, flat, new):
        old_g[gname(c)] += f
        new_g[gname(c)] += w
    so, sn = sum(old_g.values()), sum(new_g.values())
    print(f"추가 가입자 {a.extra:,}명 기준 구별 기대 인원 상위 5 (새 / 예전):")
    for g, w in new_g.most_common(5):
        print(f"  {g}  {a.extra * w / sn:,.0f} / {a.extra * old_g[g] / so:,.0f}")
    print("늘어난 구 상위 5 (새 − 예전):")
    for g in sorted(new_g, key=lambda g: -(new_g[g] / sn - old_g[g] / so))[:5]:
        print(f"  {g}  {a.extra * (new_g[g] / sn - old_g[g] / so):+,.0f}")

    if a.dry_run:
        return 0
    out = HERE / "data" / f"home_resid_{locale}.json"
    src = ("e-Stat 2020 국세조사 소지역 JINKO" if a.source == "estat"
           else "osm_bldg8-*.json building=" + ",".join(sorted(RESID_TAGS)))
    body = {"made": datetime.now().isoformat(timespec="seconds"), "source": src,
            "counts": counts}
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    tmp.replace(out)
    print(f"→ {out.relative_to(HERE)} ({len(counts):,}셀)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
