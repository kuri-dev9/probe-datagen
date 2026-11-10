"""장애 주입 제어 UI — 포트 8009, FastAPI.

관제 대시보드(graphio, 8008)와 물리적으로 분리된 "뒤 화면"이다. 운영자는 여기서
장애를 주입하고, 생성 데몬이 datagen_* 제어 테이블을 1분마다 읽어 반영한다.
UI 는 **쓰기만** 한다 — 상태 전이(pending→active→done)는 데몬의 몫이다
(control.poll_transitions). UI 가 전이까지 하면 데몬과 두 주체가 같은 행을
다투게 된다.

**한 파일(`ui.py`, 9,199줄)이던 것을 2026-11-10 에 나눴다.** 그때 HTML 을 문자열로
내장한 이유는 "폐쇄망 서버에 .py 하나만 올리면 끝나야 한다"였는데, 현장도 저장소를
clone 해 이미지를 직접 굽게 되어(2026-10-16) 그 전제가 없어졌다. 정적 파일은
`static/` 에 있고 이미지에는 `COPY src/` 로 함께 들어간다. 외부 CDN 참조가 없는
것은 그대로다.

| 모듈 | 맡는 것 |
| --- | --- |
| `common` | 설정 · DB 접속 · 셀 목록 · 종류 라벨 |
| `status_api` | 상태 · 인입 · 계열 · 생성 지역 · 설정 · 주입 대상 |
| `faults_api` | 장애 주입·취소·목록 · 장애 → 사건 대조 · `EVT-…` 조회 |
| `topo_api` | 마스터 토폴로지(`/topo`) |
| `map_api` | 지도(`/map`) |
| `scenarios_api` | 시나리오 현황(`/scenarios`) 자료 |
| `pages` · `static/` · `i18n_ja` | 화면 네 개 · 번역 사전 |
"""

from __future__ import annotations

import argparse
import sys
import types

from fastapi import FastAPI

from . import common, faults_api, map_api, pages, scenarios_api, status_api, topo_api

app = FastAPI(title="probe-datagen 제어 UI")
for _m in (status_api, faults_api, topo_api, pages, map_api, scenarios_api):
    app.include_router(_m.router)

#: 나눈 모듈 — 아래 옛 이름 호환이 훑는다
_PARTS = (common, status_api, faults_api, topo_api, map_api, pages, scenarios_api)

# ── 옛 단일 모듈 이름 호환 ─────────────────────────────────────────────────
#
# 시험과 `cli` 가 `ui.api_x`·`ui._HTML_SCEN`·`ui._EVT_CACHE` 처럼 옛 모듈 이름을 읽는다.
# 나눈 모듈의 이름을 여기에 다시 싣는다(같은 객체다).
#
# **바꿔 끼우기(`monkeypatch.setattr(ui, "_connect", …)`)도 통하게 한다.** 이름만 실어 두면
# 패키지 속성만 바뀌고, 실제로 부르는 쪽(그 이름을 가져간 각 모듈)은 옛 값을 계속 쓴다 —
# 시험이 진짜 DB·진짜 설정 파일을 읽으며 **에러 없이** 엉뚱한 것을 잰다. 생성량 탭에서
# 표가 경로를 복사해 둬 같은 일이 났다(2026-11-10). 그래서 패키지에 쓰면 그 이름을 가진
# 모든 부분 모듈에 같이 쓴다. 시험이 정의한 모듈을 직접 바꾸게 고쳐지면 걷어 낸다.
for _m in _PARTS:
    globals().update({k: v for k, v in vars(_m).items()
                      if not k.startswith("__") and k not in ("router", "annotations")})


class _Compat(types.ModuleType):
    def __setattr__(self, name, value):
        super().__setattr__(name, value)
        for m in _PARTS:
            if name in vars(m):
                setattr(m, name, value)


sys.modules[__name__].__class__ = _Compat


# ── 기동 ────────────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="datagen.ui", description="장애 주입 제어 UI")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8009)
    ap.add_argument("--config", help="datagen.yaml 경로 (기본: config/datagen.yaml)")
    args = ap.parse_args(argv)

    if args.config:
        # **설정 전체를 들고 있는다.** `mysql:` 절만 떼어 두면 `pemdb1:`(대상 DB ·
        # enb_limit · 분당 호 수)를 못 보고, 그러면 UI 가 생성기와 다른 셀 집합을
        # 보여 준다 — 주입 대상이 생성 대상과 어긋나도 에러가 나지 않는다.
        #
        # `load_cfg` 가 기본 yaml 위에 겹치므로(2026-10-14) **얇은 환경별 파일을
        # 줘도 그 위험이 없다.** 전에는 여기서 통째로 대체해서, 컨테이너용
        # `local.yaml`(mysql.host 와 parquet root 만 적은 것)을 주면 생성
        # 모수를 전부 잃었다.
        from ..cli import load_cfg
        common._CFG = load_cfg(args.config)

    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0

