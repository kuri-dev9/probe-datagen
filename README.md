# probe-datagen

graphio PoC 데이터 생성기. **성공 케이스를 기본**으로 생성해 MySQL 창고를 채운다.
요청 시 특정 장비에 장애를 주입한다(2단계).

구 `graphio-data-generator`(단독 실행 불가·본체 의존)에서 **내용만 추출**해
재구성했다. 2026-10-12.

## 구조

```
생성  Generator(성공 전용) → xDR 레코드 (메모리, 파일 안 남김)
집계  내부 DuckDB — 1분 집계 SQL은 graphio 창고 규칙 이식 (aggregate.py)
적재  MySQL 만 — kpi_*_min 9개 + cell_master·core_node (mysql_writer.py)
```

DuckDB 는 **내부 계산 엔진**이다(밖으로 안 보인다). graphio 는 MySQL 만 읽는다.

| 모듈 | 역할 |
| --- | --- |
| `fields.py` | 569 XDR 필드 정의 (이식) |
| `generator.py` | 레코드 생성 — `success_only` 로 성공 강제 |
| `scenarios.py` | 장애 시나리오 13종 (2단계에서 활성) |
| `profile.py` · `cells.py` | 실측 분포 샘플러 · 셀 로더 (이식) |
| `aggregate.py` | 1분 집계 — graphio 창고 규칙 이식, 소스만 Parquet→내부 테이블 |
| `store.py` | 내부 DuckDB — raw 테이블 + 창고 DDL + DataFrame bulk 적재 |
| `mysql_writer.py` | 집계 결과 → MySQL 멱등 적재 |
| `engine.py` · `cli.py` | 오케스트레이션 · CLI |

## 실행

```bash
python3 -m venv .venv && .venv/bin/pip install -e .
PYTHONPATH=src .venv/bin/python -m datagen backfill --minutes 60
```

설정은 `config/datagen.yaml` (MySQL 접속·분당 호수·시작 시각).

## 다음 단계

- 2단계: 실시간 틱 루프 + 장애 주입(datagen_fault 테이블) + 제어 UI
- 성능: 생성 ~1초/분 · 집계 배치 · 적재 DataFrame bulk → 분당 ~1.3초
