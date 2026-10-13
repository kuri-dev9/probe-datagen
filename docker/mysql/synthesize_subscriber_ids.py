#!/usr/bin/env python3
"""기존 `pemdb1` 의 암호문 가입자 식별자 → 합성 평문 치환. **로컬 전용.**

## 왜

`XDR_LTE_CALL_KPI` 에 들어 있는 `IMSI`·`mdn`·`IMEI` 는 프로브 단계에서 암호화된
base64 24자다(`ofxUSXVv+SqZqYTvB+zCjw==`). PoC 로컬에 실제 개인정보를 두지 않기로
했다(사람 판정). **생성 경로는 이미 합성값을 쓴다**(`datagen.identity`) — 이
스크립트는 **그 전에 적재된 행**을 같은 체계로 맞춘다.

운영에 적용하지 마라. 운영은 암호문이 정본이다(`CLAUDE.md` 불변규칙 14).

## 매핑은 1:1 이어야 한다 — 그러지 않으면 조용히 틀린다

같은 암호문은 항상 같은 합성값으로, 서로 다른 암호문은 서로 다른 값으로 간다.
합쳐지면 `UNIQ_IMSI_CNT`·영향 가입자 수가 **줄어들고** 집계·채점이 흔들린다.

단사성은 `datagen.identity._scatter` 의 아핀 사상이 구조적으로 보장한다 —
해시가 아니므로 충돌 검사가 필요 없다. 이 스크립트는 그 위에 **정렬 순서로
인덱스를 고정**해 재현성을 더한다. 같은 입력이면 몇 번 돌려도 같은 결과다.

## 지키는 제약

* `IMSI` 는 그 행의 `IMSI_MCC_MNC_INFO` 로 **시작한다**(규격 "first 7 characters
  of imsi"). 접두를 발명하지 않고 그 컬럼 값을 가져온다 — 사업자·로밍 구분이
  보존된다.
* `IMSI` ↔ `mdn` 은 **같은 가입자 인덱스**를 쓴다. 한 가입자가 두 번호를 갖거나
  두 가입자가 한 번호를 쓰지 않는다.
* `IMEI` 의 TAC 는 `VENDOR`·`MODEL` 에서 나온다 — 같은 기종이면 같은 TAC 다.
  그러지 않으면 단말 기종 축(SC-08 · 룰 R-10)이 TAC 로 기종을 가를 수 없다.
* 체크디지트는 **Luhn** 이다.

## 행마다 UPDATE 하지 않는다

184만 행이다. 임시 테이블에 벌크 적재하고 `UPDATE ... JOIN` 한 문장으로 바꾼다.
행마다 보내면 한 트랜잭션에 묶여 수십 분이 되고, 끊기면 통째로 롤백된다 —
`CM_CELL_REP_RU_LOC.RU_NAME` 38.6만 행에서 그렇게 겪었다(10분 초과 → 롤백,
벌크로 바꿔 14.5초).

임시 테이블 collation 을 대상과 **똑같이** 준다. 다르면 JOIN 이
`Illegal mix of collations` 로 깨진다(불변규칙 14).

    python3 synthesize_subscriber_ids.py --dry-run
    python3 synthesize_subscriber_ids.py
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import pymysql

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from datagen.identity import (  # noqa: E402
    luhn_ok, synth_imei, synth_imsi, synth_mdn, synth_service_code)

CONN = dict(
    host=os.environ.get("GP_MYSQL_HOST", "127.0.0.1"),
    port=int(os.environ.get("GP_MYSQL_PORT", "3306")),
    user=os.environ.get("GP_MYSQL_USER", "graphio"),
    password=os.environ.get("GP_MYSQL_PASSWORD", "graphio"),
    database=os.environ.get("GP_MYSQL_DB", "pemdb6"),
    charset="utf8mb4",
)
BATCH = 2000

#: 대상 컬럼의 collation. 임시 테이블이 이것과 달라지면 JOIN 이 깨진다.
COLL = "CHARACTER SET utf8mb3 COLLATE utf8mb3_general_ci"

#: 이미 합성된 값인가 — 암호문은 base64 라 `+`·`/`·`=` 를 포함하고, 합성값은
#: 순수 숫자다. 멱등성 판정과 `--dry-run` 보고에 쓴다.
ENC = "(IMSI REGEXP '[^0-9]')"

#: **이미 합성된 값은 건드리지 않는다.** 매핑을 현재 값에서 만들므로, 가드가
#: 없으면 다시 돌릴 때 합성값을 **또** 매핑해 값이 매번 바뀐다(멱등하지 않다).
#: `CM_CELL_REP_RU_LOC.RU_NAME` 에서 같은 함정을 밟아 826행을 망친 전례가 있다.
#: 암호문은 base64 라 `+`·`/`·`=` 를 포함하고 합성값은 순수 숫자다.
ENC_NUM = "REGEXP '[^0-9]'"              # IMSI · mdn · IMEI
ENC_SVC = "NOT LIKE 'SVC%'"              # SERVICECODE



def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=8, help="표본 출력 수")
    args = ap.parse_args()

    con = pymysql.connect(**CONN, cursorclass=pymysql.cursors.DictCursor)
    try:
        q = con.cursor()

        # ── 불변값 기록 — 1:1 이면 치환 후에도 같아야 한다 ──────────────────
        q.execute("""SELECT COUNT(*) AS rows_tot,
                            COUNT(DISTINCT IMSI) AS d_imsi,
                            COUNT(DISTINCT mdn)  AS d_mdn,
                            COUNT(DISTINCT IMEI) AS d_imei,
                            COUNT(DISTINCT SERVICECODE) AS d_svc
                       FROM XDR_LTE_CALL_KPI""")
        before = q.fetchone()
        q.execute("SELECT SUM(UNIQ_IMSI_CNT) AS s FROM PM_ENB_KPI_1M")
        before["pm_uniq"] = q.fetchone()["s"]
        print(f"치환 전  행 {before['rows_tot']:,} · IMSI {before['d_imsi']:,}종 · "
              f"mdn {before['d_mdn']:,}종 · IMEI {before['d_imei']:,}종 · "
              f"PM UNIQ_IMSI 합 {before['pm_uniq']:,}")

        # ── 전제 검사 — 깨지면 멈춘다 ──────────────────────────────────────
        # 1:1 매핑을 만들려면 (암호문 IMSI) → (MCC_MNC, mdn) 가 함수여야 한다.
        # 아니면 한 가입자가 두 사업자 접두나 두 번호를 갖는 것이고, 그 상태로
        # 치환하면 어느 쪽을 버려도 사실이 바뀐다.
        q.execute("""SELECT
             (SELECT COUNT(*) FROM (SELECT IMSI FROM XDR_LTE_CALL_KPI GROUP BY IMSI
               HAVING COUNT(DISTINCT IMSI_MCC_MNC_INFO) > 1) a) AS imsi_multi_plmn,
             (SELECT COUNT(*) FROM (SELECT IMSI FROM XDR_LTE_CALL_KPI GROUP BY IMSI
               HAVING COUNT(DISTINCT mdn) > 1) b) AS imsi_multi_mdn,
             (SELECT COUNT(*) FROM (SELECT mdn FROM XDR_LTE_CALL_KPI GROUP BY mdn
               HAVING COUNT(DISTINCT IMSI) > 1) c) AS mdn_multi_imsi,
             (SELECT COUNT(*) FROM (SELECT IMEI FROM XDR_LTE_CALL_KPI GROUP BY IMEI
               HAVING COUNT(DISTINCT CONCAT(IFNULL(VENDOR,''),'/',IFNULL(MODEL,''))) > 1) d)
               AS imei_multi_model""")
        pre = q.fetchone()
        if any(pre.values()):
            print("전제 위반 — 치환하지 않았다:", pre, file=sys.stderr)
            print("  한 가입자가 두 사업자 접두·두 번호를 갖거나 한 단말이 두 기종이다.",
                  file=sys.stderr)
            return 1
        print(f"전제 검사 통과 {pre}")

        # ── 가입자 — IMSI · mdn 을 같은 인덱스로 ───────────────────────────
        # **정렬해서 인덱스를 고정한다.** DB 가 돌려주는 순서에 맡기면 다시
        # 돌릴 때 다른 값이 나오고, 로컬과 반출본이 갈라진다.
        q.execute("""SELECT IMSI, ANY_VALUE(mdn) AS mdn,
                            ANY_VALUE(IMSI_MCC_MNC_INFO) AS plmn
                       FROM XDR_LTE_CALL_KPI
                      WHERE IMSI IS NOT NULL AND IMSI REGEXP '[^0-9]'
                      GROUP BY IMSI ORDER BY IMSI""")
        subs = q.fetchall()
        sub_map = []        # (old_imsi, new_imsi, old_mdn, new_mdn)
        skipped_plmn = 0
        for i, r in enumerate(subs):
            plmn = (r["plmn"] or "").strip()
            if len(plmn) != 7 or not plmn.isdigit():
                # 접두를 모르면 **지어내지 않는다** — 그 행은 암호문으로 남기고
                # 보고한다. 임의 접두를 넣으면 사업자·로밍 구분이 거짓이 된다.
                skipped_plmn += 1
                continue
            sub_map.append((r["IMSI"], synth_imsi(plmn, i), r["mdn"], synth_mdn(i)))

        # ── 단말 — IMEI 는 자기 정체로 매핑한다 ────────────────────────────
        # 가입자와 묶지 않는다. 한 가입자가 단말을 바꿀 수 있고, 실제로 IMEI 와
        # IMSI 의 distinct 수가 같다고 1:1 이라는 뜻은 아니다.
        q.execute("""SELECT IMEI, ANY_VALUE(VENDOR) AS vendor, ANY_VALUE(MODEL) AS model
                       FROM XDR_LTE_CALL_KPI
                      WHERE IMEI IS NOT NULL AND IMEI REGEXP '[^0-9]'
                      GROUP BY IMEI ORDER BY IMEI""")
        devs = [(r["IMEI"], synth_imei(r["vendor"] or "", r["model"] or "", i))
                for i, r in enumerate(q.fetchall())]

        # ── 서비스 코드 ───────────────────────────────────────────────────
        # graphio 는 이 컬럼을 읽지 않고 민감 목록에도 없다. 그래도 치환한다 —
        # 암호화된 운영 데이터를 로컬에 남기지 않는 것이 이 작업의 목적이다.
        q.execute("""SELECT SERVICECODE FROM XDR_LTE_CALL_KPI
                      WHERE SERVICECODE IS NOT NULL
                        AND SERVICECODE NOT LIKE 'SVC%'
                      GROUP BY SERVICECODE ORDER BY SERVICECODE""")
        svcs = [(r["SERVICECODE"], synth_service_code(i))
                for i, r in enumerate(q.fetchall())]

        print(f"가입자 매핑 {len(sub_map):,} (접두 불명으로 건너뜀 {skipped_plmn:,}) · "
              f"단말 매핑 {len(devs):,} · 서비스코드 매핑 {len(svcs):,}")

        # ── 단사성 자체 검증 — 적용 전에 확인한다 ──────────────────────────
        for name, vals in (("IMSI", [m[1] for m in sub_map]),
                           ("mdn", [m[3] for m in sub_map]),
                           ("IMEI", [d[1] for d in devs]),
                           ("SERVICECODE", [s[1] for s in svcs])):
            if len(set(vals)) != len(vals):
                print(f"{name} 합성값이 충돌했다 — 아핀 사상 전제가 깨졌다",
                      file=sys.stderr)
                return 1
        bad_luhn = [d[1] for d in devs if not luhn_ok(d[1])]
        if bad_luhn:
            print(f"IMEI 체크디지트 불통 {len(bad_luhn)}건: {bad_luhn[:3]}", file=sys.stderr)
            return 1
        print("단사성·Luhn 자체 검증 통과")

        for old, new, omdn, nmdn in sub_map[:args.limit]:
            print(f"  {old:26} → {new}   mdn {omdn[:14]:14}… → {nmdn}")
        for old, new in devs[:3]:
            print(f"  {old:26} → {new}  (IMEI)")
        for old, new in svcs[:2]:
            print(f"  {old:26} → {new}  (SERVICECODE)")

        if not sub_map and not devs and not svcs:
            print("\n치환할 암호문이 없다 — 이미 전부 합성값이다(멱등).")
            return 0
        if args.dry_run:
            print("\n--dry-run — 아무것도 바꾸지 않았다.")
            return 0

        # ── 적용 — 임시 테이블 + UPDATE JOIN ──────────────────────────────
        with con.cursor() as cur:
            cur.execute("DROP TEMPORARY TABLE IF EXISTS _sub_map")
            cur.execute(f"""CREATE TEMPORARY TABLE _sub_map (
                              OLD_IMSI varchar(30) {COLL} NOT NULL,
                              NEW_IMSI varchar(30) {COLL} NOT NULL,
                              OLD_MDN  varchar(30) {COLL},
                              NEW_MDN  varchar(30) {COLL},
                              PRIMARY KEY (OLD_IMSI)) ENGINE=InnoDB""")
            ins = "INSERT INTO _sub_map VALUES (%s,%s,%s,%s)"
            for i in range(0, len(sub_map), BATCH):
                cur.executemany(ins, sub_map[i:i + BATCH])

            cur.execute("DROP TEMPORARY TABLE IF EXISTS _dev_map")
            cur.execute(f"""CREATE TEMPORARY TABLE _dev_map (
                              OLD_IMEI varchar(30) {COLL} NOT NULL,
                              NEW_IMEI varchar(30) {COLL} NOT NULL,
                              PRIMARY KEY (OLD_IMEI)) ENGINE=InnoDB""")
            for i in range(0, len(devs), BATCH):
                cur.executemany("INSERT INTO _dev_map VALUES (%s,%s)", devs[i:i + BATCH])

            # `IMSI` 와 `mdn` 을 한 문장에서 바꾼다 — 따로 돌리면 중간 상태에서
            # `(IMSI, mdn)` 쌍이 깨진 채로 읽힐 수 있다.
            cur.execute("""UPDATE XDR_LTE_CALL_KPI x
                             JOIN _sub_map m ON m.OLD_IMSI = x.IMSI
                              SET x.IMSI = m.NEW_IMSI,
                                  x.mdn  = COALESCE(m.NEW_MDN, x.mdn)""")
            n_sub = cur.rowcount
            cur.execute("""UPDATE XDR_LTE_CALL_KPI x
                             JOIN _dev_map d ON d.OLD_IMEI = x.IMEI
                              SET x.IMEI = d.NEW_IMEI""")
            n_dev = cur.rowcount

            # 같은 암호문이 다른 테이블에도 있다 — 거기도 맞춰야 조인·대조가 산다.
            cur.execute("DROP TEMPORARY TABLE IF EXISTS _svc_map")
            cur.execute(f"""CREATE TEMPORARY TABLE _svc_map (
                              OLD_SVC varchar(30) {COLL} NOT NULL,
                              NEW_SVC varchar(30) {COLL} NOT NULL,
                              PRIMARY KEY (OLD_SVC)) ENGINE=InnoDB""")
            for i in range(0, len(svcs), BATCH):
                cur.executemany("INSERT INTO _svc_map VALUES (%s,%s)", svcs[i:i + BATCH])
            cur.execute("""UPDATE XDR_LTE_CALL_KPI x
                             JOIN _svc_map v ON v.OLD_SVC = x.SERVICECODE
                              SET x.SERVICECODE = v.NEW_SVC""")
            n_svc = cur.rowcount

            cur.execute("SELECT COUNT(*) AS c FROM PM_ENB_ROOT_CAUSE_1M")
            if cur.fetchone()["c"]:
                cur.execute("""UPDATE PM_ENB_ROOT_CAUSE_1M p
                                 JOIN _sub_map m ON m.OLD_IMSI = p.IMSI
                                  SET p.IMSI = m.NEW_IMSI""")
                print(f"  PM_ENB_ROOT_CAUSE_1M {cur.rowcount:,}행")
        con.commit()
        print(f"XDR UPDATE — 가입자 {n_sub:,}행 · 단말 {n_dev:,}행 · "
              f"서비스코드 {n_svc:,}행")

        # ── 검증 ──────────────────────────────────────────────────────────
        q.execute(f"""SELECT COUNT(*) AS rows_tot,
                             COUNT(DISTINCT IMSI) AS d_imsi,
                             COUNT(DISTINCT mdn)  AS d_mdn,
                             COUNT(DISTINCT IMEI) AS d_imei,
                             COUNT(DISTINCT SERVICECODE) AS d_svc,
                             SUM({ENC}) AS still_enc,
                             SUM(IMSI NOT LIKE CONCAT(IMSI_MCC_MNC_INFO, '%%')) AS prefix_bad,
                             SUM(CHAR_LENGTH(IMSI) <> 15) AS imsi_len_bad,
                             SUM(CHAR_LENGTH(IMEI) <> 15) AS imei_len_bad,
                             SUM(CHAR_LENGTH(mdn)  <> 11) AS mdn_len_bad
                        FROM XDR_LTE_CALL_KPI""")
        after = q.fetchone()
        q.execute("SELECT SUM(UNIQ_IMSI_CNT) AS s FROM PM_ENB_KPI_1M")
        after["pm_uniq"] = q.fetchone()["s"]

        print("\n검증")
        ok = True
        for label, key in (("행 수", "rows_tot"), ("IMSI 종수", "d_imsi"),
                           ("mdn 종수", "d_mdn"), ("IMEI 종수", "d_imei"),
                           ("서비스코드 종수", "d_svc"),
                           ("PM UNIQ_IMSI 합", "pm_uniq")):
            same = before[key] == after[key]
            ok &= same
            print(f"  {label:16} {before[key]:>12,} → {after[key]:>12,}  "
                  f"{'보존' if same else '★ 달라졌다'}")
        for label, key in (("암호문 잔존", "still_enc"), ("접두 불일치", "prefix_bad"),
                           ("IMSI 길이≠15", "imsi_len_bad"),
                           ("IMEI 길이≠15", "imei_len_bad"),
                           ("mdn 길이≠11", "mdn_len_bad")):
            v = int(after[key] or 0)
            ok &= (v == skipped_plmn if key in ("still_enc", "prefix_bad") else v == 0)
            print(f"  {label:16} {v:>12,}")
        print(f"\n{'전부 통과' if ok else '★ 확인 필요'}"
              + (f" (접두 불명 {skipped_plmn:,}행은 의도적으로 남겼다)" if skipped_plmn else ""))
        return 0 if ok else 1
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
