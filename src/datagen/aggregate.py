"""③ 1분 집계 — 모든 분석의 기준 단위 (§4.8).

여기가 KPI 산식의 **단일 구현**이다. 화면·리포트·RCA가 전부 이 결과를 읽으므로,
"이 분의 절단율"이 무엇을 세는지에 대한 답이 시스템 전체에서 하나다.

분 귀속 규칙(§4.8.1)을 그대로 구현한다.

* 시도·소통 → ``call_start_time`` 의 분 (접속을 시도한 시점이 사건 발생 시점)
* 완료·절단 → ``call_end_time`` 의 분 (끊긴 시점이 사건 발생 시점)
* First/Last Error → ``first_error_time`` 의 분 (오류가 실제 발생한 시점)

셀 귀속도 성격별로 다르다(§3.2.10). 시도·성공은 ``First_eNB_ID``+``First_Cell_ID``,
절단은 ``Last_eNB_ID``+``Last_Cell_ID`` 다. 09:12에 시작해 09:47에 끊긴 호는
시도가 09:12분, 절단이 09:47분에 계상되며 셀도 다를 수 있다 — 정상 동작이다.

집계는 항상 **원시 Parquet에서 다시 계산**한다. 지각 도착으로 같은 분을 재집계해도
결과가 같아야 하고(§2.3.4 멱등성), 부분 갱신은 그 보장을 깨뜨린다.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

# probe-datagen 이식 노트 (2026-10-12):
# 원본은 원시를 Parquet 글롭(read_parquet)에서 읽었다. 여기서는 파일을 남기지
# 않으므로 같은 커넥션 안의 **원시 테이블**(store.py 가 만드는 raw_<계열>)을
# 읽는다. 바뀐 것은 소스 참조뿐이고 산식·귀속 규칙·컬럼 명세는 원본 그대로다 —
# 여기를 손보면 graphio 창고 규칙과 어긋나 조용히 틀린다.
Database = Any  # 원본 시그니처 보존용 — 실제로는 duckdb 커넥션이 들어온다


def raw_glob(warehouse, series: str) -> str:
    """원본과 같은 이름을 유지하되 테이블명을 돌려준다 (호출부 무수정)."""
    return f"raw_{series}"

#: 정상 종료로 분류할 S1AP Release Cause (부록 D.3.1). 핸드오버 유발 해제를
#: 절단으로 계상하면 이동이 잦은 지역의 절단률이 실제와 무관하게 치솟는다.
NORMAL_RELEASE = (102, 104, 105, 117, 118, 120, 122, 123, 124, 133, 134, 135, 300, 302)
UNSPEC_RELEASE = (201, 303, 406, 504)

_NORMAL_SQL = ",".join(str(v) for v in NORMAL_RELEASE)
_UNSPEC_SQL = ",".join(str(v) for v in UNSPEC_RELEASE)


def _slot(col: str) -> str:
    """epoch 초 → 분 슬롯 (KST 정각 분)."""
    return f"date_trunc('minute', to_timestamp({col}))"


def _range_filter(start: datetime | None, end: datetime | None, col: str = "slot_ts") -> str:
    parts = []
    if start:
        parts.append(f"{col} >= TIMESTAMP '{start:%Y-%m-%d %H:%M:%S}'")
    if end:
        parts.append(f"{col} <= TIMESTAMP '{end:%Y-%m-%d %H:%M:%S}'")
    return " AND ".join(parts) if parts else "TRUE"


def slot_bounds(con: Database) -> tuple[datetime, datetime] | None:
    row = con.execute(
        "SELECT min(slot_ts), max(slot_ts) FROM file_ledger WHERE status <> 'MISSING'"
    ).fetchone()
    if not row or row[0] is None:
        return None
    return row[0], row[1]


# --------------------------------------------------------------------------
# 접속품질 (Control-Plane)
# --------------------------------------------------------------------------


#: ``kpi_cell_min`` 의 컬럼 명세.
#:
#: (컬럼, 시도·소통 갈래 식, 완료·절단 갈래 식, First Error 갈래 식) 순서다.
#: 세 갈래를 UNION ALL 하므로 컬럼 순서와 개수가 반드시 일치해야 한다. 손으로
#: 맞추면 반드시 어긋나므로 이 한 곳에서 SQL을 생성한다.
CELL_COLUMNS: tuple[tuple[str, str, str, str], ...] = (
    ("end_records", "1", "0", "0"),
    ("flag_missing",
     "CASE WHEN attempt_flag = 0 AND success_flag = 0 AND drop_flag = 0 "
     "AND data_attempt_flag = 0 AND ims_attempt_flag = 0 "
     "AND paging_attempt_flag = 0 AND detach_flag = 0 THEN 1 ELSE 0 END", "0", "0"),
    ("attempt", "attempt_flag", "0", "0"),
    ("success", "success_flag", "0", "0"),
    ("drop_cnt", "0", "drop_flag", "0"),
    ("completed", "0",
     "CASE WHEN success_flag = 1 AND drop_flag = 0 THEN 1 ELSE 0 END", "0"),
    ("data_att", "data_attempt_flag", "0", "0"),
    ("data_suc", "data_success_flag", "0", "0"),
    ("ims_att", "ims_attempt_flag", "0", "0"),
    ("ims_suc", "ims_success_flag", "0", "0"),
    ("paging_att", "paging_attempt_flag", "0", "0"),
    ("paging_suc", "paging_success_flag", "0", "0"),
    ("auth_att", "auth_attempt_flag", "0", "0"),
    ("auth_suc", "auth_success_flag", "0", "0"),
    ("loc_att", "location_attempt_flag", "0", "0"),
    ("loc_suc", "location_success_flag", "0", "0"),
    ("mec_att", "mecheck_attempt_flag", "0", "0"),
    ("mec_suc", "mecheck_success_flag", "0", "0"),
    ("detach_cnt", "detach_flag", "0", "0"),
    ("npr_cnt", "npr_flag", "0", "0"),
    ("fe_any", "0", "0", "1"),
    ("fe_timeout", "0", "0", "CASE WHEN first_error_cause = 900 THEN 1 ELSE 0 END"),
    ("fe_recovered", "0", "0", "CASE WHEN success_flag = 1 THEN 1 ELSE 0 END"),
    ("fe_propagated", "0", "0",
     "CASE WHEN last_error_interface_protocol <> first_error_interface_protocol "
     "THEN 1 ELSE 0 END"),
    *(
        (f"fe_i{i}", "0", "0",
         f"CASE WHEN first_error_interface_protocol = {i} THEN 1 ELSE 0 END")
        for i in range(1, 9)
    ),
    *(
        (f"to_i{i}", "0", "0",
         f"CASE WHEN first_error_cause = 900 AND first_error_interface_protocol = {i} "
         "THEN 1 ELSE 0 END")
        for i in (1, 2, 3, 4, 5, 6, 8)
    ),
    ("drop_normal", "0",
     f"CASE WHEN drop_flag = 1 AND UEContextRelease_Cause IN ({_NORMAL_SQL}) "
     "THEN 1 ELSE 0 END", "0"),
    ("drop_radio", "0",
     "CASE WHEN drop_flag = 1 AND UEContextRelease_Cause BETWEEN 100 AND 199 "
     f"AND UEContextRelease_Cause NOT IN ({_NORMAL_SQL}) THEN 1 ELSE 0 END", "0"),
    ("drop_transport", "0",
     "CASE WHEN drop_flag = 1 AND UEContextRelease_Cause BETWEEN 200 AND 299 "
     f"AND UEContextRelease_Cause NOT IN ({_UNSPEC_SQL}) THEN 1 ELSE 0 END", "0"),
    ("drop_node", "0",
     "CASE WHEN drop_flag = 1 AND UEContextRelease_Cause BETWEEN 500 AND 599 "
     f"AND UEContextRelease_Cause NOT IN ({_UNSPEC_SQL}) THEN 1 ELSE 0 END", "0"),
    ("drop_nas", "0",
     "CASE WHEN drop_flag = 1 AND UEContextRelease_Cause BETWEEN 300 AND 399 "
     f"AND UEContextRelease_Cause NOT IN ({_NORMAL_SQL}) "
     f"AND UEContextRelease_Cause NOT IN ({_UNSPEC_SQL}) THEN 1 ELSE 0 END", "0"),
    ("drop_proto", "0",
     "CASE WHEN drop_flag = 1 AND UEContextRelease_Cause BETWEEN 400 AND 499 "
     f"AND UEContextRelease_Cause NOT IN ({_UNSPEC_SQL}) THEN 1 ELSE 0 END", "0"),
    ("drop_unspec", "0",
     f"CASE WHEN drop_flag = 1 AND UEContextRelease_Cause IN ({_UNSPEC_SQL}) "
     "THEN 1 ELSE 0 END", "0"),
    ("drop_unknown", "0",
     "CASE WHEN drop_flag = 1 AND COALESCE(UEContextRelease_Cause, 0) = 0 "
     "THEN 1 ELSE 0 END", "0"),
    ("acc_dur_sum",
     "CASE WHEN initial_access_duration > 0 THEN initial_access_duration ELSE 0 END",
     "0", "0"),
    ("acc_dur_cnt", "CASE WHEN initial_access_duration > 0 THEN 1 ELSE 0 END", "0", "0"),
    ("core_dur_sum",
     "CASE WHEN initial_core_duration > 0 THEN initial_core_duration ELSE 0 END",
     "0", "0"),
    ("core_dur_cnt", "CASE WHEN initial_core_duration > 0 THEN 1 ELSE 0 END", "0", "0"),
    ("pag_dur_sum",
     "CASE WHEN initial_paging_duration > 0 THEN initial_paging_duration ELSE 0 END",
     "0", "0"),
    ("pag_dur_cnt", "CASE WHEN initial_paging_duration > 0 THEN 1 ELSE 0 END", "0", "0"),
    ("pag_att_sum", "COALESCE(initial_paging_attempt_count, 0)", "0", "0"),
    ("ps_cnt", "COALESCE(PathSwitch_count, 0)", "0", "0"),
    ("psf_cnt", "COALESCE(PathSwitchFailure_count, 0)", "0", "0"),
    ("dcnr_ue", "CASE WHEN ue_dcnr = 1 THEN 1 ELSE 0 END", "0", "0"),
    ("dcnr_nr_ok",
     "CASE WHEN ue_dcnr = 1 AND initial_nr_conn_time > 0 THEN 1 ELSE 0 END", "0", "0"),
    ("dcnr_restrict",
     "CASE WHEN ue_dcnr = 1 AND mme_restrict_dcnr = 1 THEN 1 ELSE 0 END", "0", "0"),
    ("old_fail_cnt",
     "CASE WHEN old_call_error_cause > 0 AND success_flag = 0 THEN 1 ELSE 0 END",
     "0", "0"),
    ("old_same_iface",
     "CASE WHEN old_call_error_cause > 0 AND success_flag = 0 "
     "AND old_call_error_interface_protocol = first_error_interface_protocol "
     "THEN 1 ELSE 0 END", "0", "0"),
)

#: 고유 가입자 수는 카운트 합산으로 얻을 수 없으므로 원시에서 distinct 한다 (§4.8.3)
CELL_DISTINCT: tuple[tuple[str, str, str, str], ...] = (
    ("imsi", "IMSI", "NULL", "NULL"),
    ("fail_imsi",
     "CASE WHEN attempt_flag = 1 AND success_flag = 0 THEN IMSI END", "NULL", "NULL"),
)


def _cell_key(enb: str, cell: str) -> str:
    """셀 키 = eNB_ID + '-' + Cell_ID (§3.2.4).

    식별자가 비어 있는 레코드는 버리지 않고 ``UNKNOWN_CELL``로 모은다. 지도에서
    사라진 셀의 품질 이슈는 영원히 보이지 않게 되므로(§1.2.3), 미매핑·미상 셀은
    숨기지 않고 별도 그룹으로 집계해 H-07/H-16으로 감시한다.
    """
    return (
        f"CASE WHEN COALESCE({enb}, '') = '' OR {cell} IS NULL "
        f"THEN 'UNKNOWN_CELL' ELSE {enb} || '-' || {cell} END"
    )


def _cell_branch(idx: int, slot_expr: str, cell_expr: str, extra_where: str = "") -> str:
    cols = ",\n                   ".join(
        f"{spec[1 + idx]} AS {spec[0]}" for spec in CELL_COLUMNS
    )
    dis = ",\n                   ".join(
        f"{spec[1 + idx]} AS {spec[0]}" for spec in CELL_DISTINCT
    )
    where = f"WHERE {extra_where}" if extra_where else ""
    return f"""SELECT {slot_expr} AS slot_ts,
                   {cell_expr} AS cell_key,
                   COALESCE(call_type, 0) AS call_type,
                   {cols},
                   {dis}
            FROM src {where}"""


def aggregate_cell(
    con: Database,
    warehouse: Any = None,
    start: datetime | None = None,
    end: datetime | None = None,
) -> int:
    """``kpi_cell_min`` — Call Type × KPI Flag 교차 1분 집계.

    Call Type 그룹핑은 선택이 아니라 강제다(§4.1.1). ``attempt_flag``는 10개의
    서로 다른 절차를 한 이름에 담고 있어, 트래픽 구성비가 바뀌면 각 Call Type의
    품질이 그대로여도 전체 합산 소통률이 움직인다.
    """
    g = raw_glob(warehouse, "lte_call")
    where = _range_filter(start, end)
    con.execute(f"DELETE FROM kpi_cell_min WHERE {where}")

    branches = " UNION ALL ".join(
        (
            # 시도·소통: call_start_time 의 분 / First 셀 귀속
            _cell_branch(0, _slot("call_start_time"), _cell_key("First_eNB_ID", "First_Cell_ID")),
            # 완료·절단: call_end_time 의 분 / Last 셀 귀속
            _cell_branch(1, _slot("call_end_time"), _cell_key("Last_eNB_ID", "Last_Cell_ID")),
            # First Error: first_error_time 의 분 / First 셀 귀속
            _cell_branch(
                2,
                _slot("CASE WHEN first_error_time > 0 THEN first_error_time "
                      "ELSE call_start_time END"),
                _cell_key("First_eNB_ID", "First_Cell_ID"),
                "COALESCE(first_error_cause, 0) > 0",
            ),
        )
    )
    agg_cols = ", ".join(f"SUM({spec[0]})" for spec in CELL_COLUMNS)
    dis_cols = ", ".join(f"COUNT(DISTINCT {spec[0]})" for spec in CELL_DISTINCT)
    con.execute(
        f"""
        INSERT INTO kpi_cell_min
        WITH src AS (SELECT * FROM {g} WHERE OngoingFlag = 2),
        unioned AS ({branches})
        SELECT slot_ts, cell_key, call_type, {agg_cols}, {dis_cols}
        FROM unioned
        WHERE {_range_filter(start, end, 'slot_ts')}
        GROUP BY 1, 2, 3
        """
    )
    return con.execute(f"SELECT COUNT(*) FROM kpi_cell_min WHERE {where}").fetchone()[0]


def aggregate_node(
    con: Database, warehouse: Any = None,
    start: datetime | None = None, end: datetime | None = None,
) -> int:
    """``kpi_node_min`` — Core 노드(MME/SGW/PGW) 단위. EV-12 교차 판별의 재료."""
    g = raw_glob(warehouse, "lte_call")
    where = _range_filter(start, end)
    con.execute(f"DELETE FROM kpi_node_min WHERE {where}")
    con.execute(
        f"""
        INSERT INTO kpi_node_min
        WITH src AS (SELECT * FROM {g} WHERE OngoingFlag = 2),
        unpivoted AS (
            SELECT 'MME' AS node_type, MME_ID AS node_id, * FROM src WHERE MME_ID IS NOT NULL
            UNION ALL SELECT 'SGW', SGW_ID, * FROM src WHERE SGW_ID IS NOT NULL
            UNION ALL SELECT 'PGW', PGW_ID, * FROM src WHERE PGW_ID IS NOT NULL
        )
        SELECT {_slot('call_start_time')} AS slot_ts, node_type, node_id,
               COALESCE(call_type, 0),
               SUM(attempt_flag), SUM(success_flag), SUM(drop_flag),
               SUM(CASE WHEN first_error_cause > 0 THEN 1 ELSE 0 END),
               SUM(CASE WHEN first_error_cause = 900 THEN 1 ELSE 0 END),
               SUM(CASE WHEN first_error_interface_protocol = 1 THEN 1 ELSE 0 END),
               SUM(CASE WHEN first_error_interface_protocol = 2 THEN 1 ELSE 0 END),
               SUM(CASE WHEN first_error_interface_protocol = 3 THEN 1 ELSE 0 END),
               SUM(CASE WHEN first_error_interface_protocol = 5 THEN 1 ELSE 0 END),
               SUM(CASE WHEN first_error_interface_protocol = 6 THEN 1 ELSE 0 END),
               SUM(CASE WHEN initial_access_duration > 0 THEN initial_access_duration ELSE 0 END),
               SUM(CASE WHEN initial_access_duration > 0 THEN 1 ELSE 0 END),
               SUM(CASE WHEN initial_core_duration > 0 THEN initial_core_duration ELSE 0 END),
               SUM(CASE WHEN initial_core_duration > 0 THEN 1 ELSE 0 END),
               COUNT(DISTINCT IMSI),
               -- 고유 가입자는 카운트 합산으로 못 얻는다. 원시에서 distinct 한다(§4.8.3).
               -- 이게 없으면 Core 노드 축 Incident 의 영향 규모가 산출 자체가 안 된다.
               COUNT(DISTINCT CASE WHEN attempt_flag = 1 AND success_flag = 0
                                   THEN IMSI END)
        FROM unpivoted
        WHERE {_range_filter(start, end, _slot('call_start_time'))}
        GROUP BY 1, 2, 3, 4
        """
    )
    return con.execute(f"SELECT COUNT(*) FROM kpi_node_min WHERE {where}").fetchone()[0]


def aggregate_dimensions(
    con: Database, warehouse: Any = None,
    start: datetime | None = None, end: datetime | None = None,
) -> None:
    """``kpi_ue_min`` / ``kpi_apn_min`` / ``cause_min`` — 차원 집중도와 Cause 분포."""
    g = raw_glob(warehouse, "lte_call")
    where = _range_filter(start, end)
    slot = _slot("call_start_time")

    con.execute(f"DELETE FROM kpi_ue_min WHERE {where}")
    con.execute(
        f"""
        INSERT INTO kpi_ue_min
        SELECT {slot}, COALESCE(Vendor, ''), COALESCE(Model, ''),
               SUM(attempt_flag), SUM(success_flag), SUM(drop_flag),
               SUM(CASE WHEN first_error_cause > 0 THEN 1 ELSE 0 END),
               SUM(CASE WHEN ue_dcnr = 1 THEN 1 ELSE 0 END),
               SUM(CASE WHEN ue_dcnr = 1 AND initial_nr_conn_time > 0 THEN 1 ELSE 0 END),
               COUNT(DISTINCT IMSI),
               COUNT(DISTINCT CASE WHEN attempt_flag = 1 AND success_flag = 0
                                   THEN IMSI END)
        FROM {g} WHERE OngoingFlag = 2
          AND {_range_filter(start, end, slot)}
        GROUP BY 1, 2, 3
        """
    )

    con.execute(f"DELETE FROM kpi_apn_min WHERE {where}")
    con.execute(
        f"""
        INSERT INTO kpi_apn_min
        SELECT {slot}, COALESCE(APN, ''), COALESCE(call_type, 0),
               SUM(attempt_flag), SUM(success_flag), SUM(drop_flag),
               SUM(CASE WHEN first_error_cause > 0 THEN 1 ELSE 0 END),
               SUM(CASE WHEN esm_error_Cause = 27 THEN 1 ELSE 0 END),
               SUM(CASE WHEN s11_error_Cause = 93 THEN 1 ELSE 0 END)
        FROM {g} WHERE OngoingFlag = 2
          AND {_range_filter(start, end, slot)}
        GROUP BY 1, 2, 3
        """
    )

    # EV-01 / EV-03 — First Error 와 구간별 오류의 Cause·Message 분포
    con.execute(f"DELETE FROM cause_min WHERE {where}")
    cell = _cell_key("First_eNB_ID", "First_Cell_ID")
    stage_selects = "\n            UNION ALL ".join(
        f"""SELECT {slot} AS slot_ts, '{pre}' AS scope, {iface} AS interface,
                   {pre}_error_Cause AS cause, COALESCE({pre}_error_Message, 0) AS message,
                   {cell} AS cell_key
            FROM {g}
            WHERE OngoingFlag = 2 AND COALESCE({pre}_error_Cause, 0) > 0"""
        for pre, iface in (
            ("s6a", 1), ("s1ap", 2), ("s11", 3), ("s10", 4),
            ("emm", 5), ("esm", 6), ("s3", 7), ("s13", 8),
        )
    )
    con.execute(
        f"""
        INSERT INTO cause_min
        WITH stages AS (
            SELECT {slot} AS slot_ts, 'first' AS scope,
                   first_error_interface_protocol AS interface,
                   first_error_cause AS cause,
                   COALESCE(first_error_message, 0) AS message,
                   {cell} AS cell_key
            FROM {g}
            WHERE OngoingFlag = 2 AND COALESCE(first_error_cause, 0) > 0
            UNION ALL
            SELECT {slot}, 'release', 2, UEContextRelease_Cause, 23,
                   {_cell_key("Last_eNB_ID", "Last_Cell_ID")}
            FROM {g}
            WHERE OngoingFlag = 2 AND COALESCE(UEContextRelease_Cause, 0) > 0
            UNION ALL {stage_selects}
        )
        SELECT slot_ts, scope, interface, cause, message, cell_key, COUNT(*)
        FROM stages
        WHERE {_range_filter(start, end, 'slot_ts')}
        GROUP BY 1, 2, 3, 4, 5, 6
        """
    )


# --------------------------------------------------------------------------
# 서비스품질 (User-Plane)
# --------------------------------------------------------------------------

_U_ZERO = ", ".join(["0"] * 0)


def aggregate_user_plane(
    con: Database, warehouse: Any = None,
    start: datetime | None = None, end: datetime | None = None,
) -> None:
    """``kpi_u_cell_min`` / ``kpi_domain_min`` — 방향 축(down/up)을 분리해 집계한다.

    재전송률의 분모는 반드시 ``PayloadPacketCount`` 다(§4.2.4). 전체
    ``PacketCount``를 쓰면 순수 ACK가 섞여 재전송률이 실제보다 낮게 나온다.
    """
    where = _range_filter(start, end)
    con.execute(f"DELETE FROM kpi_u_cell_min WHERE {where}")
    con.execute(f"DELETE FROM kpi_domain_min WHERE {where}")

    tp = raw_glob(warehouse, "transport")
    slot = _slot("SummaryCreateTime")
    con.execute(
        f"""
        INSERT INTO kpi_u_cell_min
        SELECT {slot}, eNB_ID || '-' || Cell_ID, 'transport',
               COUNT(*),
               SUM(CASE WHEN SynCount > 0 THEN 1 ELSE 0 END),
               SUM(CASE WHEN AckTime > 0 THEN 1 ELSE 0 END),
               SUM(CASE WHEN SynCount > 1 THEN 1 ELSE 0 END),
               SUM(CASE WHEN RstCount > 0 THEN 1 ELSE 0 END),
               SUM(CASE WHEN UplinkWindowCloseCount > 0 THEN 1 ELSE 0 END),
               SUM(CASE WHEN DnlinkWindowCloseCount > 0 THEN 1 ELSE 0 END),
               SUM(CASE WHEN SynMssSize > 0 AND SynAckMssSize > 0
                             AND SynMssSize <> SynAckMssSize THEN 1 ELSE 0 END),
               SUM(COALESCE(UplinkRTTTime, 0)), SUM(COALESCE(UplinkRTTCount, 0)),
               SUM(COALESCE(DnlinkRTTTime, 0)), SUM(COALESCE(DnlinkRTTCount, 0)),
               SUM(COALESCE(Traffic_Total_Uplink_RetransPacketCount, 0)),
               SUM(COALESCE(Traffic_Total_Uplink_PayloadPacketCount, 0)),
               SUM(COALESCE(Traffic_Total_Dnlink_RetransPacketCount, 0)),
               SUM(COALESCE(Traffic_Total_Dnlink_PayloadPacketCount, 0)),
               SUM(COALESCE(Traffic_Total_Uplink_PayloadSize, 0)),
               SUM(COALESCE(Traffic_Total_Dnlink_PayloadSize, 0)),
               SUM(COALESCE(Traffic_Total_Uplink_ThroughputTime, 0)),
               SUM(COALESCE(Traffic_Total_Dnlink_ThroughputTime, 0)),
               SUM(COALESCE(ICMP_Delta_Uplink_IcmpPacketCount, 0)),
               SUM(COALESCE(ICMP_Delta_Dnlink_IcmpPacketCount, 0)),
               0, 0, 0, 0, 0, 0, 0, 0, 0,
               0, 0, 0, 0, 0, 0, 0,
               SUM(CASE WHEN COALESCE(ClientHelloDomain, '') <> ''
                          OR COALESCE(LastDnsQueryName, '') <> '' THEN 1 ELSE 0 END),
               0,
               SUM(CASE WHEN Tethering = 1 THEN 1 ELSE 0 END),
               COUNT(DISTINCT IMSI)
        FROM {tp}
        WHERE {_range_filter(start, end, slot)}
        GROUP BY 1, 2
        """
    )

    dn = raw_glob(warehouse, "dns")
    con.execute(
        f"""
        INSERT INTO kpi_u_cell_min
        SELECT {slot}, eNB_ID || '-' || Cell_ID, 'dns',
               COUNT(*), 0, 0, 0, 0, 0, 0, 0,
               0, 0, 0, 0, 0, 0, 0, 0,
               SUM(COALESCE(Traffic_Total_Uplink_PayloadSize, 0)),
               SUM(COALESCE(Traffic_Total_Dnlink_PayloadSize, 0)),
               0, 0,
               SUM(COALESCE(ICMP_Delta_Uplink_IcmpPacketCount, 0)),
               SUM(COALESCE(ICMP_Delta_Dnlink_IcmpPacketCount, 0)),
               COUNT(*),
               SUM(CASE WHEN ReplyCode = 0 AND AnswerCount > 0 THEN 1 ELSE 0 END),
               SUM(CASE WHEN ReplyCode <> 0 THEN 1 ELSE 0 END),
               SUM(CASE WHEN COALESCE(ResponseTime, 0) = 0 THEN 1 ELSE 0 END),
               SUM(CASE WHEN QueryRetryCount > 0 THEN 1 ELSE 0 END),
               SUM(COALESCE(dns_retry_sequence, 0)),
               SUM(COALESCE(dns_retry_interval, 0)),
               SUM(CASE WHEN ResponseTime > 0 AND RequestFirstTime > 0
                        THEN (ResponseTime - RequestFirstTime) * 1e6 ELSE 0 END),
               SUM(CASE WHEN ResponseTime > 0 AND RequestFirstTime > 0 THEN 1 ELSE 0 END),
               0, 0, 0, 0, 0, 0, 0,
               SUM(CASE WHEN COALESCE(QueryName, '') <> '' THEN 1 ELSE 0 END),
               0, 0,
               COUNT(DISTINCT IMSI)
        FROM {dn}
        WHERE {_range_filter(start, end, slot)}
        GROUP BY 1, 2
        """
    )

    ht = raw_glob(warehouse, "http")
    con.execute(
        f"""
        INSERT INTO kpi_u_cell_min
        SELECT {slot}, eNB_ID || '-' || Cell_ID, 'http',
               COUNT(*), 0, 0, 0, 0, 0, 0, 0,
               SUM(COALESCE(UplinkRTTTime, 0)), SUM(COALESCE(UplinkRTTCount, 0)),
               SUM(COALESCE(DnlinkRTTTime, 0)), SUM(COALESCE(DnlinkRTTCount, 0)),
               0, 0,
               SUM(COALESCE(Traffic_Total_Dnlink_RetransPacketCount, 0)),
               SUM(COALESCE(Traffic_Total_Dnlink_PayloadPacketCount, 0)),
               0, SUM(COALESCE(Traffic_Total_Dnlink_PayloadSize, 0)),
               0, SUM(COALESCE(Traffic_Total_Dnlink_ThroughputTime, 0)),
               0, 0,
               0, 0, 0, 0, 0, 0, 0, 0, 0,
               COUNT(*),
               SUM(CASE WHEN StatusCode BETWEEN 200 AND 399 THEN 1 ELSE 0 END),
               SUM(CASE WHEN StatusCode BETWEEN 400 AND 499 THEN 1 ELSE 0 END),
               SUM(CASE WHEN StatusCode BETWEEN 500 AND 599 THEN 1 ELSE 0 END),
               SUM(CASE WHEN COALESCE(StatusCode, 0) = 0
                          OR COALESCE(ResponseTime, 0) = 0 THEN 1 ELSE 0 END),
               SUM(CASE WHEN ResponseTime > 0 AND RequestTime > 0
                        THEN (ResponseTime - RequestTime) * 1e6 ELSE 0 END),
               SUM(CASE WHEN ResponseTime > 0 AND RequestTime > 0 THEN 1 ELSE 0 END),
               SUM(CASE WHEN COALESCE(Host, '') <> '' THEN 1 ELSE 0 END),
               0, 0,
               COUNT(DISTINCT IMSI)
        FROM {ht}
        WHERE {_range_filter(start, end, slot)}
        GROUP BY 1, 2
        """
    )

    # 도메인 차원 — U-36/U-38, EV-08b (외부 서비스 기인 판별)
    con.execute(
        f"""
        INSERT INTO kpi_domain_min
        SELECT {slot}, 'http', COALESCE(NULLIF(Host_Site, ''), 'UNKNOWN'),
               COUNT(*), COUNT(*),
               SUM(CASE WHEN StatusCode BETWEEN 200 AND 399 THEN 1 ELSE 0 END),
               SUM(CASE WHEN StatusCode BETWEEN 400 AND 499 THEN 1 ELSE 0 END),
               SUM(CASE WHEN StatusCode BETWEEN 500 AND 599 THEN 1 ELSE 0 END),
               SUM(CASE WHEN ResponseTime > 0 AND RequestTime > 0
                        THEN (ResponseTime - RequestTime) * 1e6 ELSE 0 END),
               SUM(CASE WHEN ResponseTime > 0 AND RequestTime > 0 THEN 1 ELSE 0 END),
               SUM(COALESCE(UplinkRTTTime, 0)), SUM(COALESCE(UplinkRTTCount, 0)),
               SUM(COALESCE(DnlinkRTTTime, 0)), SUM(COALESCE(DnlinkRTTCount, 0)),
               SUM(COALESCE(Traffic_Total_Dnlink_RetransPacketCount, 0)),
               SUM(COALESCE(Traffic_Total_Dnlink_PayloadPacketCount, 0)),
               0, 0
        FROM {ht}
        WHERE {_range_filter(start, end, slot)}
        GROUP BY 1, 2, 3
        """
    )
    con.execute(
        f"""
        INSERT INTO kpi_domain_min
        SELECT {slot}, 'dns',
               COALESCE(NULLIF(regexp_extract(QueryName, '([^.]+\\.[^.]+)$', 1), ''), 'UNKNOWN'),
               COUNT(*), 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
               COUNT(*),
               SUM(CASE WHEN ReplyCode <> 0 OR COALESCE(ResponseTime, 0) = 0 THEN 1 ELSE 0 END)
        FROM {dn}
        WHERE {_range_filter(start, end, slot)}
        GROUP BY 1, 2, 3
        """
    )


# --------------------------------------------------------------------------
# 시스템 건전성 (§4.4)
# --------------------------------------------------------------------------


def aggregate_health(
    con: Database, warehouse: Any = None,
    start: datetime | None = None, end: datetime | None = None,
) -> None:
    """``health_min`` — H-* 자기 감시 지표.

    RCA의 신뢰도는 입력 데이터 품질에 종속되므로(§4.4), 이 지표가 임계를 넘은
    구간에서 나온 RCA 결과에는 신뢰도 페널티가 붙는다.
    """
    where = _range_filter(start, end)
    lte_glob = raw_glob(warehouse, "lte_call")
    slot = _slot("call_start_time")
    slot_range = _range_filter(start, end, slot)
    con.execute(f"DELETE FROM health_min WHERE {where}")
    con.execute(
        f"""
        INSERT INTO health_min
        WITH cellagg AS (
            SELECT k.slot_ts,
                   SUM(k.end_records) AS end_records,
                   SUM(k.flag_missing) AS flag_missing,
                   SUM(k.drop_cnt) AS drops,
                   SUM(k.drop_unspec + k.drop_unknown) AS drop_unknown,
                   SUM(CASE WHEN m.cell_key IS NULL THEN k.end_records ELSE 0 END) AS unknown_cell,
                   SUM(CASE WHEN m.geo_status <> 'MAPPED' THEN k.end_records ELSE 0 END) AS unmapped,
                   SUM(k.attempt) AS attempt, SUM(k.success) AS success,
                   SUM(k.fe_any) AS fe_any
            FROM kpi_cell_min k
            LEFT JOIN cell_master m USING (cell_key)
            WHERE {_range_filter(start, end, 'k.slot_ts')}
            GROUP BY 1
        ),
        u AS (
            SELECT slot_ts, SUM(sessions) AS sessions, SUM(domain_known) AS domain_known
            FROM kpi_u_cell_min
            WHERE {_range_filter(start, end, 'slot_ts')} AND series IN ('transport', 'http')
            GROUP BY 1
        ),
        raw_health AS (
            -- 집계값으로는 알 수 없는 두 지표는 원시 레코드에서 직접 센다.
            -- "실패 호인데 First Error 가 없다"는 집계 후에는 복원되지 않는다.
            SELECT slot_ts, metric, value FROM (
                SELECT {slot} AS slot_ts,
                       100.0 * SUM(CASE WHEN attempt_flag = 1 AND success_flag = 0
                                             AND COALESCE(first_error_cause, 0) = 0
                                        THEN 1 ELSE 0 END)
                           / NULLIF(SUM(CASE WHEN attempt_flag = 1 AND success_flag = 0
                                             THEN 1 ELSE 0 END), 0) AS h15,
                       100.0 * SUM(CASE WHEN COALESCE(First_eNB_ID, '') = ''
                                             OR COALESCE(First_Cell_ID, 0) = 0
                                        THEN 1 ELSE 0 END) / NULLIF(COUNT(*), 0) AS h16
                FROM {lte_glob}
                WHERE OngoingFlag = 2 AND {slot_range}
                GROUP BY 1
            ) UNPIVOT (value FOR metric IN (h15, h16))
        ),
        ledger AS (
            SELECT slot_ts,
                   SUM(CASE WHEN status = 'MISSING' THEN 1 ELSE 0 END) AS missing,
                   SUM(CASE WHEN status = 'LATE' THEN 1 ELSE 0 END) AS late,
                   SUM(CASE WHEN status = 'PARTIAL' THEN 1 ELSE 0 END) AS partial,
                   COUNT(*) AS files
            FROM file_ledger
            WHERE {_range_filter(start, end, 'slot_ts')}
            GROUP BY 1
        )
        SELECT slot_ts, metric, value, detail FROM (
            SELECT slot_ts, 'H-07' AS metric,
                   100.0 * (unmapped + unknown_cell) / NULLIF(end_records, 0) AS value,
                   '미매핑 셀 비율(%)' AS detail FROM cellagg
            UNION ALL SELECT slot_ts, 'H-11',
                   100.0 * flag_missing / NULLIF(end_records, 0), 'KPI Flag 결측률(%)' FROM cellagg
            UNION ALL SELECT slot_ts, 'H-12',
                   100.0 * drop_unknown / NULLIF(drops, 0), '원인미상 절단율(%)' FROM cellagg
            UNION ALL SELECT slot_ts, 'H-15', value, 'First Error 미기록률(%)' FROM raw_health
                     WHERE metric = 'h15'
            UNION ALL SELECT slot_ts, 'H-16', value, 'Cell_ID 결측률(%)' FROM raw_health
                     WHERE metric = 'h16'
            UNION ALL SELECT slot_ts, 'H-10',
                   100.0 * (sessions - domain_known) / NULLIF(sessions, 0),
                   '도메인 미해석률(%)' FROM u
            UNION ALL SELECT slot_ts, 'H-19',
                   100.0 * missing / NULLIF(files, 0), '결측 분 비율(%)' FROM ledger
            UNION ALL SELECT slot_ts, 'H-21',
                   100.0 * late / NULLIF(files, 0), '지각 파일 비율(%)' FROM ledger
        ) WHERE value IS NOT NULL
        """
    )


def aggregate_all(
    con: Database,
    warehouse: Any = None,
    start: datetime | None = None,
    end: datetime | None = None,
    log=print,
) -> dict[str, int]:
    """전 계층 1분 집계. ``start``/``end``가 없으면 전 구간을 다시 계산한다."""
    import time

    t0 = time.time()
    rows = aggregate_cell(con, warehouse, start, end)
    nodes = aggregate_node(con, warehouse, start, end)
    aggregate_dimensions(con, warehouse, start, end)
    aggregate_user_plane(con, warehouse, start, end)
    aggregate_health(con, warehouse, start, end)
    elapsed = time.time() - t0
    log(f"  집계 — cell {rows:,}행 · node {nodes:,}행 · {elapsed:.1f}s")
    return {"cell_rows": rows, "node_rows": nodes, "elapsed_ms": int(elapsed * 1000)}
