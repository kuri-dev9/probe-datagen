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

## 상용 스키마(pemdb1) 경로 — `pemfill`

**상용이 정본이다.** 자체 창고 스키마(`kpi_*_min`) 대신 상용 테이블에 직접 쓰는
별도 경로를 뒀다. 기존 `backfill`·`run` 은 그대로 둔다 — 두 경로는 테이블도
DB도 겹치지 않는다.

```bash
PYTHONPATH=src .venv/bin/python -m datagen pemfill --minutes 15
PYTHONPATH=src .venv/bin/python -m datagen pemfill --minutes 15 --start "2026-10-13 09:00"
PYTHONPATH=src .venv/bin/python -m datagen pemfill --minutes 2 --with-failures   # 실측 실패율 재현
PYTHONPATH=src .venv/bin/python -m datagen pemfill --minutes 2 --no-agg          # XDR 만
```

```
셀 마스터  CL_CELL_MAP + CM_CELL_REP_RU_LOC  (data/cells.json 대신)
생성       Generator → base_lte_call → 실측 분포 덮기 → 시나리오 변형
적재       XDR_LTE_CALL_KPI (155컬럼, 분 단위 DELETE→INSERT 멱등)
집계       MySQL 안에서 SQL — PM_ENB_KPI_1M · PM_ENB_CAUSE_1M · PM_EPC_KPI_1M_ORG
```

설정은 `config/datagen.yaml` 의 `pemdb1:` 절이다(DB명·eNB 수·분당 호 수).

### 실측으로 고정한 것

| 항목 | 값 |
| --- | --- |
| 컬럼 매핑 | `SERIES_FIELDS["lte_call"]`(154) ↔ XDR(155) 가 **순서 1:1**. 철자 다른 자리 9곳이 있어 **위치로** 맞춘다 |
| 시각 | epoch 초 문자열(`"1791849641.523882"`), 없으면 `"0.0"`. `EVENT_TIME` 만 datetime(파티션 키) |
| "없음" | NULL 이 아니라 문자열 `"0"` |
| 셀 키 | `FIRST_ENB_ID` = `CL_CELL_MAP.ENB_ID`, `FIRST_CELL_ID` = `CELLNUM` (둘 다 평문) |
| 레코드 범위 | `ONGOINGFLAG='2'`(호 종료)만. 상용 추출 조건과 같다 |
| 구성비 | `data/pemdb1/_distributions.json` — CALL_TYPE·APN·Vendor/Model·Release·First Error |
| Cause 표기 | `C_<IF_PROTO 2자리><Cause 6자리>` (`C_02000504`). TIMEOUT 은 `iface='0'` → `C_00000901` |

### 아직 모르는 것 — NULL 로 둔다

`PM_ENB_KPI_1M` 의 다음 컬럼은 상용 산출 정의를 확인하지 못해 **NULL** 이다.
모름을 0으로 적으면 운영자가 무해하다는 뜻으로 읽는다(graphio 불변규칙 17).

- `MNO` · `EX_*` (8개) — `EX` 접두의 의미를 모른다. 로밍 제외? 특정 Cause 제외?
- `COMPANY_ID`·`REGION_ID`·`BRANCH_ID`·`OPTEAM_ID`·`PART_ID` — eNB→조직 매핑 테이블이 없다
- `BTS_NAME` — `CM_CELL_REP_RU_LOC.RU_NAME` 의 한글이 추출 단계에서 깨졌다(`(??)???LRRU`)
- `PM_ENB_CAUSE_1M.DESCRIPTION` — `CD_CAUSE` 가 로컬 pemdb1 에 없다
- `PM_EPC_KPI_1M_ORG.MCC`·`MNC`·`MNO` — 가입자 차원이라 노드·분 단위에 단일 값이 없다

`CALL_TYPE` 은 XDR 값(`"3"`·`"4"`…)을 그대로 넣었다. `PM_*.CALL_TYPE` 이
varchar(16) 이라 상용은 이름(`TAU`·`PAGING`)을 담을 수도 있는데, 실측 데이터를
못 봐서 **조인이 성립하는 쪽**을 골랐다.

## 다음 단계

- 2단계: 실시간 틱 루프 + 장애 주입(datagen_fault 테이블) + 제어 UI
- 성능: 생성 ~1초/분 · 집계 배치 · 적재 DataFrame bulk → 분당 ~1.3초
