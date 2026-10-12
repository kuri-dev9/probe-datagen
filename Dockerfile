# 외부 프론트엔드(`# syntax=docker/dockerfile:1.7`)를 쓰지 않는다.
# 그 지시자는 빌드할 때마다 Docker Hub 에서 프론트엔드 이미지를 받아 실행하는데,
# 타겟 서버는 Hub 접근이 막혀 있고 실제로 `failed to solve: frontend grpc server
# closed unexpectedly` 로 빌드가 죽었다. 여기서는 1.7 전용 문법을 쓰지 않으므로
# 빌더 기본 문법으로 충분하다 — BuildKit 기능(ARG 로 고르는 스테이지 이름)은
# 그대로 쓸 수 있다.
#
# probe-datagen 컨테이너 이미지
# ════════════════════════════════════════════════════════════════════════
#
# graphio-probe-rca 와 **같은 베이스·같은 레이아웃**이다. 두 이미지를 한
# compose 로 묶어 돌리므로 베이스가 같아야 서버가 받아 둘 이미지도 하나로 끝난다
# (타겟 서버는 Docker Hub 를 못 받는다 — 베이스는 tar 로 1회 반입한다).
#
#   PYTHON_IMAGE  python:3.13-slim-bullseye (glibc 2.31)
#     CentOS 7 의 구형 Docker 는 기본 seccomp 가 clone3 을 막는다. glibc 2.34+
#     베이스는 그 때문에 즉사한다 — bullseye 는 2.31 이라 해당 없다.
#     (자세한 배경은 graphio-probe-rca/Dockerfile 머리말)
#
# node 단계가 없다. 생성기는 화면이 없고 제어 UI 는 ui.py 안에 HTML 을
# 문자열로 들고 있다 — 빌드 도구가 필요 없다.
#
# ── 아키텍처 ───────────────────────────────────────────────────────────
# 개발 머신이 arm64, 타겟이 x86_64 다. 서버에서 직접 빌드하면 맞춰지지만,
# 맥에서 구워 반출할 때는 --platform linux/amd64 를 반드시 준다.
#
# ── 레이아웃 ───────────────────────────────────────────────────────────
# 저장소 구조를 /app 에 그대로 두고 PYTHONPATH=/app/src 로 돈다. 패키지를
# 설치하지 않으므로 setuptools 도, PyPI 빌드 격리도 필요 없다.

ARG PYTHON_IMAGE=python:3.13-slim-bullseye

# ══ 1단계: 의존성 ══════════════════════════════════════════════════════
FROM ${PYTHON_IMAGE} AS deps
RUN python3 -m venv /opt/venv
COPY requirements.txt /tmp/requirements.txt
RUN /opt/venv/bin/pip install --no-cache-dir --upgrade pip \
 && /opt/venv/bin/pip install --no-cache-dir -r /tmp/requirements.txt \
 && /opt/venv/bin/pip check

# ══ 2단계: 런타임 ══════════════════════════════════════════════════════
FROM ${PYTHON_IMAGE} AS runtime

# 생성 시각이 창고의 slot_ts 가 된다. UTC 로 돌면 graphio 가 읽는 모든
# 시간창이 9시간 밀린다 (graphio 불변규칙 9 — 시간창은 관측 사실이다).
ENV TZ=Asia/Seoul \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src \
    PATH=/opt/venv/bin:$PATH
RUN ln -snf /usr/share/zoneinfo/${TZ} /etc/localtime && echo "${TZ}" > /etc/timezone

COPY --from=deps /opt/venv /opt/venv

WORKDIR /app
COPY pyproject.toml requirements.txt ./
COPY src/    ./src/
COPY config/ ./config/
# 셀 마스터·코어 노드·실측 분포 프로파일 (약 300KB). 이것이 생성의 바탕이라
# 없으면 기동 자체가 안 된다 — 이미지에 넣는다.
COPY data/   ./data/

RUN groupadd -r -g 1000 datagen \
 && useradd -r -u 1000 -g datagen -d /app -s /sbin/nologin datagen \
 && chown -R datagen:datagen /app
USER datagen

EXPOSE 8009

ENTRYPOINT ["python", "-u", "-m", "datagen"]
# 기본은 상주 데몬. 제어 UI 는 compose 가 명령을 덮어쓴다.
CMD ["run"]
