"""27번 지진의 eNB 지반 증폭률 표를 만든다 — J-SHIS 표층 지반(V4 · 250m 메시)의 ARV (2026-11-15).

생성기는 `pemdb1.fault.quake_arv_path`(CSV `enb_id,arv`)를 읽어 셀 진도에 곱한다
(`scenarios.quake_intensity`). 표가 없으면 모든 eNB 가 1.0 이라 도쿄 진도가 고르게 나와 기지국 정지가 0곳이다.

    python tools/build_quake_arv.py              # data/quake/enb_arv.csv
    python tools/build_quake_arv.py --limit 20   # 앞 20 메시만 (확인용)

* **DB 는 읽기만 한다.** eNB 대표 위치 = `CM_CELL_REP_RU_LOC_JP` 셀 좌표(도:분:초/밀리초)의 eNB 평균.
  접속은 `GP_MYSQL_*` 환경변수(graphio `.env` 를 서브셸에서 읽어 준다). 호스트 기본 127.0.0.1.
* **J-SHIS 에 부담을 주지 않는다.** 한 번에 하나, 요청 사이 `--gap` 초(기본 1.0) 이상. 같은 250m 메시의
  eNB 는 한 번만 부른다(메시 코드는 JIS X 0410 1/4 지역 메시 — J-SHIS 응답의 `meshcode` 와 같은 식이다).
* **이어 받기.** 메시 결과는 `data/quake/jshis_mesh.csv` 에 한 줄씩 바로 쓴다. 다시 돌리면 이미 받은 메시는
  부르지 않는다. 실패한 메시는 `jshis_failed.csv` 에 남기고 **값을 채우지 않는다**(graphio 불변규칙 17) —
  다음 실행에서 다시 시도한다. 표에 없는 eNB 는 생성기가 1.0 으로 둔다(그 사실은 이 도구의 끝 보고에 나온다).
* **순서.** 최근 60분 1분 통계(`PM_ENB_KPI_1M`)에 나온 eNB — 생성기가 실제로 쓰는 범위 — 를 먼저 받는다.
* 출력은 git 밖이다(`.gitignore` `data/quake/`). eNB 번호는 운영 자산이고 이 저장소는 공개다.
  이미지에는 `COPY data/` 로 들어간다 — 컨테이너 경로 `/app/data/quake/enb_arv.csv`.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "src"))

from datagen.geo_lite import dms_to_decimal  # noqa: E402

URL = ("https://www.j-shis.bosai.go.jp/map/api/sstrct/V4/meshinfo.geojson"
       "?position={lon:.6f}%2C{lat:.6f}&epsg=4326")
OUT = HERE / "data" / "quake"


def mesh250(lat: float, lon: float) -> str:
    """JIS X 0410 1/4 지역 메시(약 250m · 10자리) — J-SHIS `meshcode` 와 같다(아다치 이코·하네다·오테마치로 대조)."""
    p, a = int(lat * 60 // 40), lat * 60 % 40
    u, f = int(lon - 100), (lon - 100) % 1
    q, b = int(a // 5), a % 5
    v, g = int(f * 60 // 7.5), f * 60 % 7.5
    r, c = int(b * 60 // 30), b * 60 % 30
    w, h = int(g * 60 // 45), g * 60 % 45
    s, x = int(c // 15), int(h // 22.5)
    s2, x2 = int(c % 15 // 7.5), int(h % 22.5 // 11.25)
    return f"{p:02d}{u:02d}{q}{v}{r}{w}{s * 2 + x + 1}{s2 * 2 + x2 + 1}"


def _connect():
    import pymysql
    return pymysql.connect(host=os.environ.get("GP_MYSQL_HOST") or "127.0.0.1",
                           port=int(os.environ.get("GP_MYSQL_PORT") or 3306),
                           user=os.environ["GP_MYSQL_USER"], password=os.environ["GP_MYSQL_PASSWORD"],
                           database=os.environ.get("GP_MYSQL_DB") or "pemdb6", charset="utf8mb3")


def enb_positions(conn) -> tuple[dict[str, tuple[float, float]], set[str]]:
    """eNB → (위도, 경도) 평균과, 최근 60분 통계에 나온 eNB(생성 범위)."""
    acc: dict[str, list[tuple[float, float]]] = {}
    with conn.cursor() as cur:
        cur.execute("SELECT ENB_ID, LATITUDE, LONGITUDE FROM CM_CELL_REP_RU_LOC_JP")
        for e, la, lo in cur.fetchall():
            la, lo = dms_to_decimal(la), dms_to_decimal(lo)
            if la is not None and lo is not None:
                acc.setdefault(str(e), []).append((la, lo))
        cur.execute("SELECT DISTINCT ENB_ID FROM PM_ENB_KPI_1M "
                    "WHERE EVENT_TIME >= NOW() - INTERVAL 60 MINUTE")
        live = {str(r[0]) for r in cur.fetchall()}
    pos = {e: (sum(p[0] for p in v) / len(v), sum(p[1] for p in v) / len(v)) for e, v in acc.items()}
    return pos, live


def fetch(lat: float, lon: float, timeout: float) -> dict:
    req = urllib.request.Request(URL.format(lat=lat, lon=lon), headers={"User-Agent": "probe-datagen/quake-arv"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        doc = json.loads(r.read().decode("utf-8"))
    feats = doc.get("features") or []
    if doc.get("status") != "Success" or not feats:
        raise ValueError(f"응답에 값이 없다 (status={doc.get('status')})")
    pr = feats[0]["properties"]
    return {"meshcode": str(pr.get("meshcode") or doc.get("metaData", {}).get("meshcode") or ""),
            "arv": float(pr["ARV"]), "avs": pr.get("AVS"), "jname": pr.get("JNAME")}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--gap", type=float, default=1.0, help="요청 사이 초 (1.0 미만은 거부)")
    ap.add_argument("--limit", type=int, default=0, help="이번에 부를 메시 수 상한 (0 = 전부)")
    ap.add_argument("--timeout", type=float, default=20.0)
    a = ap.parse_args()
    if a.gap < 1.0:
        ap.error("--gap 은 1.0 이상 — J-SHIS 에 부담을 주지 않는다")
    OUT.mkdir(parents=True, exist_ok=True)
    cache_p, fail_p, out_p = OUT / "jshis_mesh.csv", OUT / "jshis_failed.csv", OUT / "enb_arv.csv"

    conn = _connect()
    try:
        pos, live = enb_positions(conn)
    finally:
        conn.close()
    mesh_of = {e: mesh250(la, lo) for e, (la, lo) in pos.items()}
    # 메시마다 대표 위치 하나(그 메시 첫 eNB) — 생성 범위 메시를 앞에
    rep: dict[str, tuple[float, float]] = {}
    for e in sorted(pos, key=lambda e: (e not in live, e)):
        rep.setdefault(mesh_of[e], pos[e])
    order = list(rep)
    got: dict[str, float] = {}
    if cache_p.exists():
        with open(cache_p, encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                got[row["mesh"]] = float(row["arv"])
    todo = [m for m in order if m not in got]
    if a.limit:
        todo = todo[:a.limit]
    print(f"eNB {len(pos)} (생성 범위 {len(live & set(pos))}) · 메시 {len(order)} · 받은 메시 {len(got)} · "
          f"이번에 부를 메시 {len(todo)} · 간격 {a.gap}초", flush=True)

    new_cache = not cache_p.exists()
    failed: list[tuple[str, str]] = []
    with open(cache_p, "a", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        if new_cache:
            w.writerow(["mesh", "arv", "avs", "jname"])
        for i, m in enumerate(todo, 1):
            t0 = time.monotonic()
            la, lo = rep[m]
            try:
                r = fetch(la, lo, a.timeout)
                if r["meshcode"] and r["meshcode"] != m:
                    # 계산한 메시와 응답 메시가 다르면 응답을 믿고 적는다 — 경계 위 좌표의 반올림 차이다
                    print(f"  · 메시 {m} → 응답 {r['meshcode']}", flush=True)
                w.writerow([m, r["arv"], r["avs"], r["jname"]])
                fh.flush()
                got[m] = r["arv"]
            except Exception as e:  # 네트워크·형식 — 채우지 않고 남긴다
                failed.append((m, f"{type(e).__name__}: {e}"))
            if i % 100 == 0 or i == len(todo):
                print(f"  {i}/{len(todo)} · 실패 {len(failed)}", flush=True)
            time.sleep(max(0.0, a.gap - (time.monotonic() - t0)))
    if failed:
        with open(fail_p, "w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["mesh", "error"])
            w.writerows(failed)
    elif fail_p.exists():
        fail_p.unlink()

    rows = sorted((e, got[mesh_of[e]]) for e in pos if mesh_of[e] in got)
    with open(out_p, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["enb_id", "arv"])
        w.writerows(rows)
    have_live = sum(1 for e in live if e in pos and mesh_of[e] in got)
    print(f"끝 — {out_p} eNB {len(rows)}/{len(pos)} (생성 범위 {have_live}/{len(live & set(pos))}) · "
          f"이번 실패 {len(failed)}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
