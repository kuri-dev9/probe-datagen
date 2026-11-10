"""마스터 토폴로지(`/topo`) API — 덤프·복원·시드·좌표·행 CRUD."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import topo
from .common import HERE, _cfg, _connect, _pem_cfg


router = APIRouter()


# ── 마스터 토폴로지 (`/topo`) ───────────────────────────────────────────────
#
# **CLI(`python -m datagen topo`)와 같은 함수를 부른다.** 화면이 자기 SQL 을
# 쓰면 한쪽만 고쳐질 때 두 경로가 다른 마스터를 만들고, 그것은 에러가 아니라
# 값이 어긋나는 종류다(`topo.py` 머리말).
#
# **파괴적 경로는 화면이 CLI 보다 느슨하지 않다.** `rm --all` 은 CLI 와 똑같이
# `locale/<나라>` 사본을 선행 조건으로 요구하고, 여기서는 **실적재 전부**
# (`seed-enb`·`fill-coords`)에도 같은 조건을 건다 — 화면은 눌리기 쉽고
# `pemdb6` 가 그대로 운영이라 되돌릴 원본이 없다. 더 엄한 쪽으로만 다르다.
#
# ## 사본 게이트는 **KR 에만** 걸린다 (2026-10-26 접미사 벌 도입)
#
# JP 벌(`CM_GU_JP` …)은 네 단계로 **재생성된다** — 벌 만들기 → 셀 키 복사 →
# 행정구역 생성 → 좌표 재배치. 되돌릴 원본이 없는 쪽은 KR 이고, JP 에 KR
# 사본을 요구하면 **JP 를 만들기 전에는 아무 버튼도 눌리지 않는다.** 그렇게
# 선 게이트는 사람이 우회할 길을 찾게 만든다(`cli._topo_gate` 와 같은 판단).
#
# ## 관리는 **이 화면이 정본이다** (사용자 지시 2026-10-26)
#
# *"gen 스크립트로 명령 수행할 생각 없어. ui에다 새로운 화면 만들어서
# 거기서 추가 수정 삭제 관리 가능하도록 만들어줘."* 그래서 CRUD 그리드와
# JP 벌 생성 4단계가 전부 여기 있다. CLI(`python -m datagen topo`)는 같은
# 함수를 부르는 **두 번째 입구**로 남는다 — 화면이 자기 SQL 을 쓰지 않는
# 한 둘이 갈라지지 않는다.


def _topo_xw():
    """crosswalk 경로 — **`pemfill` 과 같은 키**(`pemdb1.geo_crosswalk`).

    다른 키를 보거나 기본값을 지어내면 화면이 놓은 동과 생성기가 그릴 수
    있다고 믿는 동이 다른 집합이 되고, 에러 없이 셀이 버려진다. 설정에
    없으면 `None` 이고 그 경우 `pemfill` 도 전량을 후보로 둔다.
    """
    raw = _pem_cfg().get("geo_crosswalk")
    if not raw:
        return None
    q = Path(str(raw))
    return q if q.is_absolute() else (HERE / q).resolve()


def _require_dump(conn, locale: str) -> None:
    """쓰기 전 공통 관문 — **`restore` 가 실제로 넣을 수 있는가.**

    **파일의 존재는 복귀를 뜻하지 않는다.** 전에는 `topo.dump_exists()` 가
    관문이었는데, 그때 그 JSON 을 DB 로 되넣는 코드가 없었다 — 통과 조건이
    "되돌릴 수 있다"가 아니라 "파일 4개가 존재한다"였고 그것은 안심만 준다.
    지금은 **읽어서 넣을 수 있는지**를 묻는다(`dry_run=True` 는 쓰지 않는다).

    409 로 돌려 화면이 "무엇을 먼저 해야 하는지"를 말하게 한다 — 그냥 막으면
    버튼이 안 먹는 것처럼 보인다. 실패한 테이블을 함께 실어, 넷 중 어느
    사본이 반쪽인지 사람이 알 수 있게 한다.
    """
    if locale != "kr":
        # JP 벌은 재생성 경로가 있다 (위 머리말) — KR 사본을 요구하지 않는다
        return
    rep = topo.restore(conn, locale, dry_run=True)
    if rep["ok"]:
        return
    bad = {i["table"]: i["error"] for i in rep["tables"].values() if i.get("error")}
    raise HTTPException(409, f"복귀 경로가 서 있지 않다: "
                             f"{topo.LOCALE_DIR / locale} — 먼저 `사본 뜨기`를"
                             f" 누른다 · {bad}")


@router.get("/api/topo")
def api_topo(locale: str = "kr"):
    """마스터 현황 + **시뮬레이션 선택 통과 여부**.

    `locale` 은 **어느 벌을 보는가**다(`topo.table_name`). 선택 통과
    (`pick`)는 **항상 KR 기준**이다 — 생성기가 KR 벌을 읽기 때문이고
    (`pemdb1.load_cells` 의 `locale="kr"` 기본), 창고를 다시 굽지 않는
    것이 사용자 방침이다. 그 사실을 `pick_locale` 로 적는다.

    둘을 한 응답에 담는 이유: "마스터에 행이 있다"와 "그 eNB 가 트래픽을
    받는다"는 **다른 사실**이고, 화면에서 떨어뜨려 두면 사람이 전자만 보고
    "넣었다"고 판단한다. 실측으로 3셀 eNB 는 셀 수 내림차순 22,557등이라
    `enb_limit` 축으로는 들어가지 못했다 — 지금은 `_pick_enbs` 가 대역을
    한도 밖에서 포함하므로 통과해야 하고, 통과하지 않으면 그것이 버그 신호다.
    """
    conn = _connect()
    try:
        info = topo.survey(conn, locale)
        active = info.pop("_active")
        xw = _topo_xw()
        limit = int(_pem_cfg().get("enb_limit", 0))
        # **선택 통과는 KR 기준이므로 KR crosswalk 가 맞다** — 생성기가
        # 그것을 본다(`pemfill` 과 같은 키). 아래 `no_dong` 과 다른 파일을
        # 쓰는 것이 의도다.
        pick = topo.pick_report(conn, xw, limit)
        # ── **그 로케일의 crosswalk 를 본다** (2026-10-26 결함 A)
        #
        # 전에는 설정의 단일 crosswalk(= KR 것)로 JP 구역을 판정해서, jp
        # 뷰가 *"949 구역 중 731곳에 그릴 수 있는 동이 없다"* 는 **거짓
        # 숫자**를 띄웠다. 실제로는 `CM_DONG_JP` 가 949 구역 전부에 동을
        # 갖고 있다. `_topo_xw()` 자체는 `pemfill` 과 같은 키를 봐야 하는
        # 올바른 함수이고 섞인 것은 이 호출 쪽이었다 —
        # `gu_points`/`gu_keys` 때와 같은 모양의 실수다.
        #
        # **파일이 없으면 `None`(산출 불가)이다.** 0 으로 적으면 "전부
        # 그릴 수 있다", 전수로 적으면 "전부 못 그린다"로 읽힌다 — 둘 다
        # 재지 못한 것과 다른 말이다(불변규칙 17).
        lc_xw = topo.crosswalk_path(_geo_dir(), locale) if xw else None
        drawable = (topo.drawable_dongs(lc_xw)
                    if lc_xw and lc_xw.exists() else None)
        # 벌이 아직 없으면 조회할 것이 없다 — **0 으로 적지 않는다**
        gus = (topo.gu_points(conn, locale)
               if topo.table_exists(conn, topo.table_name("gu", locale)) else [])
        no_dong = (None if drawable is None else
                   [g for _c, g, _n, _a, _o in gus if not drawable.get((_c, g))])
        gate = topo.restore(conn, "kr", dry_run=True)
        added = topo.load_added_enbs(locale)
        # ── **벌마다 행 수를 함께 내린다** (결함 B)
        #
        # 전에는 존재 여부만 실어서, kr 뷰에서 jp 다섯 줄이 `— / 있다` 로
        # 나와 **"없다"처럼 읽혔다.** 벌이 없으면 `None` 이고 0 이 아니다.
        locales = {}
        for lc in topo.GEO_SOURCES:
            arr = []
            for k in topo.LOCALE_TABLE_KEYS:
                t = topo.table_name(k, lc)
                ex = topo.table_exists(conn, t)
                arr.append({
                    "key": k, "table": t, "exists": ex,
                    "rows": (topo._rows(conn, f"SELECT COUNT(*) FROM `{t}`")[0][0]
                             if ex else None),
                })
            locales[lc] = arr
    finally:
        conn.close()
    return {
        "tables": info, "active": active, "pick": pick,
        "locale": locale, "locales": locales,
        # **원장 상태를 화면에 올린다.** 이 파일이 KR 선택에서 JP 전용
        # 장비를 빼는 근거이자 되돌림 수단인데 추적 밖(`data/`)이다 —
        # 안 보이면 지워진 것을 아무도 모른다(`topo.seed_enb_jp` 머리말).
        "added": {"enbs": len(added.get("enbs") or []),
                  "path": str(topo.added_enbs_path(locale)),
                  "band_start": added.get("band_start")},
        "locale_keys": list(topo.LOCALE_TABLE_KEYS),
        # ── **좌표 컬럼이 있는 테이블** (결함 C)
        #
        # `CL_ENB_ORG`·`CL_CELL_MAP` 에는 그 컬럼이 아예 없다. 화면이
        # `no_coord == null` 을 "산출 불가"로 찍고 있었는데, 그것은
        # **"모름"** 이고 여기 필요한 말은 **"해당 없음"** 이다. 둘을 한
        # 문자열로 뭉개면 불변규칙 17 이 지키려던 구분 자체가 무의미해진다.
        "coord_keys": list(topo.COORD_TABLE_KEYS),
        # **선택 통과는 KR 기준이다** — 생성기가 KR 벌을 읽는다
        "pick_locale": "kr",
        "enb_limit": limit, "topo_weight": topo.TOPO_WEIGHT,
        "band": list(topo.NEW_BAND),
        "crosswalk": str(xw) if xw else None,
        # 좌표가 있어 seed 대상이 되는 시군구와, 그중 **그릴 수 있는 동이
        # 없어 `DONG_ID` 를 비울 수밖에 없는** 곳. 0 으로 채우면
        # `_filter_drawable` 이 조용히 버린다(불변규칙 12·17).
        "seed_gus": len(gus),
        # `None` = 그 로케일 crosswalk 가 없어 재지 못했다 (0 과 다르다)
        "gus_without_drawable_dong": (None if no_dong is None else len(no_dong)),
        "drawable_crosswalk": str(lc_xw) if lc_xw else None,
        "drawable_crosswalk_exists": bool(lc_xw and lc_xw.exists()),
        # **`exists` 는 표시용이고 `restorable` 이 관문이다.** 둘을 함께
        # 내리는 이유: 파일은 있는데 못 읽는 상태(중간에 끊긴 gzip)를
        # 화면이 "사본 없음"으로만 말하면 사람이 또 뜬다.
        "dump": {"locale": "kr", "exists": topo.dump_exists("kr"),
                 "restorable": gate["ok"], "dir": str(topo.LOCALE_DIR / "kr"),
                 "suffix": topo.DUMP_SUFFIX,
                 "files": [{"table": i["table"], "rows": i.get("rows"),
                            "error": i.get("error")}
                           for i in gate["tables"].values()],
                 # **복구 범위를 함께 내린다** — "되돌릴 수 있다"가 실제보다
                 # 넓게 읽히면 안 된다(불변규칙 17 의 결)
                 "scope": gate["scope"],
                 "not_restored": gate["not_restored"]},
    }


class TopoReq(BaseModel):
    #: **기본이 `True` 다.** 화면에서 실행 버튼을 잘못 눌러도 세어 보기만
    #: 한다 — 실적재는 "실제로 넣기"를 명시적으로 켜야 한다.
    dry_run: bool = True
    locale: str = "kr"
    per_gu: int = 1
    cells: int = 3
    spread_km: float = 0.35
    #: `add` 용 — 넣을 컬럼. DDL 에 없는 키는 `insert_rows` 가 빼고 알린다.
    row: dict | None = None
    #: `rm` 용 — 지울 행의 키(AND). `all=True` 면 무시된다.
    key: dict | None = None
    all: bool = False
    #: `rm --all` 확인 문구. CLI 의 타이핑 확인과 같은 자리다.
    confirm: str | None = None
    target: str = "cell"
    #: `seed-geo` — 읍면동 폴리곤이 없을 때 시군구당 생성할 동 수
    dongs: int = 8
    #: `seed-geo` 는 세 테이블을 **비우고** 다시 넣는다. `geo-fetch` 는
    #: 이미 받은 파일을 다시 받는다. 둘 다 사람이 의도를 말해야 한다.
    replace: bool = False
    #: `relocate-cells` — 활성 셀이 아니라 전수. `update` 용 값
    row_only: bool = False
    #: `seed-enb-jp` — JP 전용 `ENB_ID` 대역의 시작. **기본값이 없다**:
    #: 어느 번호가 비어 있는지는 그 DB 의 사실이고, 코드에 숫자를 두면
    #: "비어 있다"는 가정이 남아 다른 DB 에서 조용히 겹친다.
    band_start: int | None = None


@router.post("/api/topo/dump")
def api_topo_dump(req: TopoReq):
    """현 마스터를 `data/pemdb1/locale/<나라>/` 로 떠낸다 — **복귀의 유일한 보장**."""
    conn = _connect()
    try:
        counts = topo.dump(conn, req.locale)
    finally:
        conn.close()
    return {"ok": True, "dir": str(topo.LOCALE_DIR / req.locale), "counts": counts}


@router.post("/api/topo/restore")
def api_topo_restore(req: TopoReq):
    """사본을 DB 로 되넣는다. **`dry_run` 기본 `True` 라 점검만 한다.**"""
    conn = _connect()
    try:
        return topo.restore(conn, req.locale, dry_run=req.dry_run)
    finally:
        conn.close()


@router.post("/api/topo/seed-enb")
def api_topo_seed(req: TopoReq):
    xw = _topo_xw()
    if xw is None:
        raise HTTPException(400, "pemdb1.geo_crosswalk 가 설정에 없다 —"
                                 " 그릴 수 있는 동을 모르는 채로 놓지 않는다")
    conn = _connect()
    try:
        if not req.dry_run:
            _require_dump(conn, req.locale)
        r = topo.seed_enb(conn, xw, per_gu=req.per_gu, cells_per_enb=req.cells,
                          spread_km=req.spread_km,
                          seed=int(_cfg()["generate"].get("seed", 20260903)),
                          dry_run=req.dry_run)
        # 놓은 eNB 가 **실제로 선택되는지** 같은 응답에 담는다. 떨어뜨려
        # 두면 "행은 생겼는데 트래픽이 0" 인 상태가 화면에서 안 보인다.
        pick = topo.pick_report(conn, xw, int(_pem_cfg().get("enb_limit", 0)),
                                extra_cells=r["keys"]) if r["keys"] else None
    finally:
        conn.close()
    r.pop("keys", None)
    return {"ok": True, "dry_run": req.dry_run, "result": r, "pick": pick}


@router.post("/api/topo/fill-coords")
def api_topo_fill(req: TopoReq):
    """좌표 결손 **점검** — 적재 경로가 아니다 (2026-10-26).

    적재는 생성기가 한다 — `cmd_run`·`cmd_pemfill` 이
    `load_cells(ensure_coords=True)` 로 매 틱 채운다
    (`topo.ensure_sector_coords`). 화면에 버튼으로 두면 사람이 눌러야 하고,
    **적재 경로가 둘이면 어느 쪽이 넣었는지 모른다.**

    세어 주는 것이 둘이다 — 좌표 있는 eNB 의 **섹터 결손**(생성기가 채운다)
    과 **좌표 없는 eNB**(`_pick_enbs` 가 불변규칙 12 검증용으로 일부러
    남긴다 — 채우지 않는다). 섞어 보면 "결손 804" 로 읽힌다.
    """
    conn = _connect()
    try:
        # 생성기와 **같은 인자**다 — crosswalk·`enb_limit` 를 빼면 후보
        # 집합이 달라져 점검이 다른 수를 말한다(실측 768/36 대 696/108).
        r = topo.fill_coords(conn, _topo_xw(),
                             int(_pem_cfg().get("enb_limit", 0)))
    finally:
        conn.close()
    return {"ok": True, "check_only": True, "result": r}


# ── locale 벌 — **4단계**. 순서가 전제다 ────────────────────────────────────
#
#   1  ensure-tables   CREATE TABLE … LIKE 로 벌 넷을 만든다 (DDL)
#   2  copy-cells      셀 키·지역 참조를 DB 안에서 복사 (좌표는 NULL)
#   3  code-map        JP 경계 코드 → KR 코드 맵 (DB 의 CM_GU 가 입력)
#   4  seed-geo        행정구역 셋을 경계 GeoJSON 에서 만든다
#   5  relocate-cells  좌표·RU_NAME 을 그 나라로 쓴다
#
# **순서를 바꾸면 조용히 빈다.** `seed-geo` 가 `code-map` 없이 돌면 코드
# 대응이 없어 전량 제외되고, `relocate-cells` 가 `seed-geo` 전에 돌면
# `CM_GU_JP` 에 좌표가 없어 멈춘다(그쪽은 에러가 나므로 안전한 편이다).


def _geo_dir():
    """경계 GeoJSON 디렉터리 — **CLI 와 같은 함수를 부른다.**

    `cli._topo_geo_dir` 가 `pemdb1.geo_crosswalk` 의 디렉터리를 쓴다. 여기서
    따로 조립하면 화면이 받은 파일과 커맨드가 읽는 파일이 갈라진다.
    """
    from ..cli import _topo_geo_dir

    return _topo_geo_dir(_cfg())


@router.post("/api/topo/ensure-tables")
def api_topo_ensure(req: TopoReq):
    """지역 테이블 넷을 `CREATE TABLE … LIKE` 로 만든다 — **1단계**.

    `dry_run` 기본이 `True` 라 무엇을 만들지만 말한다. collation·인덱스·
    타입이 원본에서 그대로 오는 것이 `LIKE` 를 쓰는 이유다(불변규칙 14).
    """
    conn = _connect()
    try:
        return {"ok": True, "dry_run": req.dry_run,
                "result": topo.ensure_locale_tables(
                    conn, req.locale, write=not req.dry_run)}
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    finally:
        conn.close()


@router.post("/api/topo/copy-cells")
def api_topo_copy_cells(req: TopoReq):
    """셀 키·지역 참조를 DB 안에서 복사한다 — **2단계**. 좌표·이름은 NULL."""
    conn = _connect()
    try:
        return {"ok": True, "dry_run": req.dry_run,
                "result": topo.copy_cells(conn, req.locale,
                                          write=not req.dry_run)}
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    finally:
        conn.close()


@router.post("/api/topo/copy-enb-org")
def api_topo_copy_enb_org(req: TopoReq):
    """eNB 조직 벌 복사 — **2b 단계**. `BTS_NAME` 만 NULL 로 남긴다.

    안 만들면 JP 지도에서 셀 이름은 일본어인데 eNB 이름은 한국어로 뜬다 —
    라벨이 `RU_NAME` → `BTS_NAME` 순으로 떨어지기 때문이다.
    """
    conn = _connect()
    try:
        return {"ok": True, "dry_run": req.dry_run,
                "result": topo.copy_enb_org(conn, req.locale,
                                            write=not req.dry_run)}
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    finally:
        conn.close()


@router.post("/api/topo/seed-enb-jp")
def api_topo_seed_enb_jp(req: TopoReq):
    """추가 구역에 장비를 놓는다 — **6단계**. KR 선택에서 빠지는 장비다.

    `CL_CELL_MAP` 에 행이 들어간다 — **KR 과 공유이고 `DUMP_TABLES` 밖**
    이라 사본이 없다. 그래서 원장(`topo.ADDED_FILE`)을 먼저 쓰고, 그 파일이
    KR 선택에서 빼는 근거이자 되돌림 수단이 된다(`rm-added`).
    """
    if req.band_start is None:
        raise HTTPException(400,
                            "ENB_ID 대역 시작 번호가 필요하다 — 빈 대역을 "
                            "실측해서 넣는다. 기본값을 두지 않는 이유: 어느 "
                            "번호가 비어 있는지는 그 DB 의 사실이고, 가정을 "
                            "코드에 남기면 다른 DB 에서 조용히 겹친다")
    conn = _connect()
    try:
        return {"ok": True, "dry_run": req.dry_run,
                "result": topo.seed_enb_jp(
                    conn, req.locale, int(req.band_start),
                    per_gu=req.per_gu, cells_per_enb=req.cells,
                    spread_km=req.spread_km,
                    seed=int(_cfg()["generate"].get("seed", 20260903)),
                    write=not req.dry_run)}
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    finally:
        conn.close()


@router.post("/api/topo/rm-added")
def api_topo_rm_added(req: TopoReq):
    """원장에 적힌 JP 전용 eNB 를 세 테이블에서 지운다 — `CL_CELL_MAP` 되돌림."""
    conn = _connect()
    try:
        return {"ok": True, "dry_run": req.dry_run,
                "result": topo.remove_added_enbs(conn, req.locale,
                                                 write=not req.dry_run)}
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    finally:
        conn.close()


@router.post("/api/topo/geo-fetch")
def api_topo_geo_fetch(req: TopoReq):
    """경계 GeoJSON 을 받는다. **없으면 무엇이 없는지 말하며 멈춘다.**

    빈 테이블을 만들어 두면 화면이 조용히 빈다 — 그래서 `seed-geo` 전에
    이것이 서야 한다. 크레딧(`SOURCE.txt`)을 파일과 함께 쓴다(번들
    디렉터리가 추적 밖이라 저장소에 적어 두면 반출본에서 사라진다).
    """
    try:
        if req.dry_run:
            # **세어 보기는 내려받지 않는다.** 무엇이 없는지만 말한다 —
            # 폐쇄망에서 버튼 하나가 수십 MB 를 끌어오면 안 된다.
            gd = _geo_dir()
            have = []
            for lv in [k for k in topo.GEO_SOURCES[req.locale]
                       if not k.startswith("_")]:
                dest = topo.geo_level_path(gd, req.locale, lv)
                have.append({"level": lv, "file": dest.name,
                             "exists": dest.exists(),
                             "bytes": dest.stat().st_size if dest.exists() else None})
            return {"ok": True, "dry_run": True, "files": have,
                    "dir": str(topo.geo_level_path(gd, req.locale, "sgg").parent),
                    "note": "받지 않았다 — 실행을 누르면 없는 것만 받는다"}
        got = topo.fetch_boundaries(_geo_dir(), req.locale, force=req.replace)
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    except OSError as e:      # 폐쇄망·DNS — "받을 수 없다"를 그대로 말한다
        raise HTTPException(502, f"경계 파일을 받을 수 없다: {e}")
    return {"ok": True, "files": got,
            "dir": str(topo.geo_level_path(_geo_dir(), req.locale, "sgg").parent)}


@router.post("/api/topo/code-map")
def api_topo_code_map(req: TopoReq):
    """JP 경계 코드 → **KR 코드** 맵 — 3단계.

    입력은 **DB 의 `(CITY_ID, GU_ID)`** 다. 번들에서 유도하지 않는다 —
    트래픽이 참조하는 것은 DB 의 코드이고, 번들과 DB 가 다르면 전환 후
    그 차이만큼 조용히 미매핑이 된다.
    """
    if req.locale == "kr":
        raise HTTPException(400, "코드 맵은 KR 코드를 **받는** 쪽이 만든다 —"
                                 " locale kr 은 대상이 아니다")
    conn = _connect()
    try:
        kr_gu = [(int(c), int(g)) for c, g in topo._rows(
            conn, f"SELECT CITY_ID, GU_ID "
                  f"FROM `{topo.table_name('gu', 'kr')}` ORDER BY GU_ID")]
        doc = topo.build_jp_code_map(_geo_dir(), kr_gu)
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    finally:
        conn.close()
    out = {"ok": True, "dry_run": req.dry_run, "kr_gu": len(kr_gu),
           "sido": len(doc["sido"]), "sgg": len(doc["sgg"]),
           "unused_prefectures": doc["unused_prefectures"],
           "sample": {str(c): rows[:2] for c, rows in
                      sorted(doc["picked"].items())[:3]}}
    if not req.dry_run:
        out["path"] = str(topo.save_code_map(doc, req.locale))
    return out


@router.post("/api/topo/seed-geo")
def api_topo_seed_geo(req: TopoReq):
    """행정구역 셋을 경계 GeoJSON 에서 만든다 — **4단계**.

    `replace` 는 세 테이블을 **비우고** 다시 넣는다. KR 대상이면 사본
    게이트가 걸린다(`_require_dump`) — 44만 셀이 참조하는 코드를 덮는 일이다.
    """
    conn = _connect()
    try:
        if not req.dry_run and req.replace:
            _require_dump(conn, req.locale)
        r = topo.seed_geo(conn, _geo_dir(), locale=req.locale,
                          dongs_per_gu=req.dongs, spread_km=3.0,
                          seed=int(_cfg()["generate"].get("seed", 20260903)),
                          replace=req.replace, dry_run=req.dry_run)
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    finally:
        conn.close()
    return {"ok": True, "dry_run": req.dry_run, "result": r}


@router.post("/api/topo/relocate-cells")
def api_topo_relocate(req: TopoReq):
    """셀 좌표와 `RU_NAME` 을 그 나라로 다시 쓴다 — **5단계**.

    **키를 바꾸지 않는다** — `UPDATE` 만 한다. `all=False`(기본)이면 1분
    집계에 나타나는 활성 셀만이고, `all=True` 면 44만행 전부다. 비(非)KR 에서
    `CL_ENB_ORG.BTS_NAME` 은 **건드리지 않는다**(한 벌 공유 — 응답의
    `org_untouched_reason` 이 그 사실을 말한다).
    """
    conn = _connect()
    try:
        if not req.dry_run:
            _require_dump(conn, req.locale)
        r = topo.relocate_cells(conn, locale=req.locale,
                                spread_km=req.spread_km,
                                seed=int(_cfg()["generate"].get("seed", 20260903)),
                                only_active=not req.all, dry_run=req.dry_run)
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    finally:
        conn.close()
    return {"ok": True, "dry_run": req.dry_run, "result": r}


# ── 행 CRUD 그리드 ─────────────────────────────────────────────────────────


@router.get("/api/topo/rows")
def api_topo_rows(table: str = "gu", locale: str = "kr", q: str = "",
                  limit: int = 50, offset: int = 0):
    """행 조회 — 검색·페이지. **전체 수를 함께 내린다.**

    `CM_DONG` 7,847 · 셀 44만을 통째로 내리면 화면이 멈춘다. `total` 을
    주어 화면이 페이지 수를 추측하지 않게 한다 — 추측하면 마지막 페이지가
    비어 "데이터가 없다"로 보인다.
    """
    if table not in topo.TABLES:
        raise HTTPException(400, f"대상은 {', '.join(topo.TABLES)} 다")
    conn = _connect()
    try:
        return topo.browse(conn, table, locale, q, limit, offset)
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    finally:
        conn.close()


@router.post("/api/topo/update")
def api_topo_update(req: TopoReq):
    """한 행의 **키가 아닌 컬럼**을 고친다.

    키 컬럼은 받지 않는다 — 1분 집계·원시 Parquet·`ground_truth.json`·
    `CL_CELL_MAP` 이 그 키를 참조하고, 화면에서 바꾸면 그 행만 옮겨지고
    나머지는 **에러 없이** 끊어진다(`topo.update_row` 머리말).
    """
    if req.target not in topo.TABLES:
        raise HTTPException(400, f"대상은 {', '.join(topo.TABLES)} 다")
    if not req.key:
        raise HTTPException(400, "고칠 행의 키가 필요하다")
    if not req.row:
        raise HTTPException(400, "고칠 컬럼이 비어 있다")
    conn = _connect()
    try:
        _require_dump(conn, req.locale)
        return {"ok": True, **topo.update_row(conn, req.target, req.locale,
                                              req.key, req.row)}
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    finally:
        conn.close()


@router.post("/api/topo/add")
def api_topo_add(req: TopoReq):
    if req.target not in topo.TABLES:
        raise HTTPException(400, f"대상은 {', '.join(topo.TABLES)} 다")
    if not req.row:
        raise HTTPException(400, "넣을 컬럼이 비어 있다")
    # **대상 테이블은 로케일이 정한다** — `gu` + `jp` = `CM_GU_JP`
    try:
        table = topo.table_name(req.target, req.locale)
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    conn = _connect()
    try:
        _require_dump(conn, req.locale)
        n = topo.insert_rows(conn, table, [req.row])
    finally:
        conn.close()
    return {"ok": True, "table": table, "rows": n}


@router.post("/api/topo/rm")
def api_topo_rm(req: TopoReq):
    if req.target not in topo.TABLES:
        raise HTTPException(400, f"대상은 {', '.join(topo.TABLES)} 다")
    try:
        table = topo.table_name(req.target, req.locale)
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    keys = topo.TABLES[req.target][1]
    if req.all and not topo.wipeable(req.target, req.locale):
        # **`cellmap` 은 받지 않는다** — `topo` 는 위치 마스터를 다루고
        # `CL_CELL_MAP` 574,404행은 **셀 대장**이다. 전량 삭제할 이유가 없고
        # `DUMP_TABLES` 에도 없어서 지우면 되돌릴 원본이 아예 없다. 전에는
        # 관문이 `CM_*` 네 파일만 보고 이 삭제를 통과시켰다.
        raise HTTPException(400, f"{table} 는 전체 삭제 대상이 아니다 — 셀 "
                                 "대장을 통째로 지울 이유가 없고 복귀 사본도 "
                                 "없다. 단건 삭제는 된다")
    conn = _connect()
    try:
        _require_dump(conn, req.locale)
        if req.all:
            # CLI 와 **같은 문구**를 요구한다. 화면의 확인 입력을 생략하면
            # 버튼 하나로 수십만 행이 사라진다.
            want = f"{table} 전체 삭제"
            if (req.confirm or "").strip() != want:
                raise HTTPException(400, f"확인 문구가 다르다 — '{want}' 를 "
                                         "그대로 입력한다")
            return {"ok": True, "table": table, "deleted": topo.wipe(conn, table)}
        if not req.key:
            raise HTTPException(400, f"지울 행의 키가 필요하다: {', '.join(keys)}")
        where = " AND ".join(f"`{k}` = %s" for k in req.key)
        with conn.cursor() as cur:
            n = cur.execute(f"DELETE FROM `{table}` WHERE {where}",
                            list(req.key.values()))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "table": table, "deleted": n}
