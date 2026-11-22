"""테스트가 실제 graphio web(`127.0.0.1:8008`)으로 **과거 데이터 복사 요청**을 보내지 못하게 막는다 (2026-11-21).

23번(`event_crowd`)은 장애가 시작되면 gen 이 `POST {graphio_api}/api/history/copy` 를 부른다(`cli._history_request`).
데몬 루프를 `datagen.yaml` 설정으로 돌리면서 23번을 거는 시험이 있으면 web 이 떠 있는 개발 머신에서 **진짜 과거 표에
복사가 일어난다.** 그래서 8008 포트로 가는 `urlopen` 은 시험 안에서 항상 거부하고, 요청 자체를 시험하는 쪽은
`urlopen` 을 자기 가짜로 바꿔 쓴다(그 경우 이 막이 덮어쓰인다).
"""

from __future__ import annotations

import urllib.request

import pytest

_GUARDED_PORTS = (":8008/",)


@pytest.fixture(autouse=True)
def _no_real_graphio_calls(monkeypatch):
    real = urllib.request.urlopen

    def guarded(req, *a, **k):
        url = getattr(req, "full_url", req)
        if isinstance(url, str) and any(p in url + "/" for p in _GUARDED_PORTS):
            raise RuntimeError(f"시험에서 실제 graphio(web) 로 요청을 보내려 했다: {url}")
        return real(req, *a, **k)

    monkeypatch.setattr(urllib.request, "urlopen", guarded)
