"""내부 DuckDB — 생성 레코드의 임시 저장과 1분 집계의 계산 엔진.

바깥(그래피오·운영자)에서는 보이지 않는다. 레코드는 raw_<계열> 테이블에 분
단위로 들어와 집계(aggregate.py)의 소스가 되고, 집계 결과는 아래 WAREHOUSE_DDL
테이블에 쌓였다가 mysql_writer 가 MySQL 로 밀어낸다. 재집계 창을 지난 원시는
버린다 — 파일(.dat·Parquet)은 어디에도 남기지 않는다 (2026-10-12 결정:
실환경 UI 는 MySQL 만 본다).

KEEP · 타입 규칙은 구 ingest/loader.py 에서, WAREHOUSE_DDL 은 graphio
store/schema.py 에서 그대로 가져왔다 — **창고 규칙의 정본은 여전히 그쪽이다.**
스키마를 바꾸려면 graphio 쪽과 같이 바꿔야 한다.
"""

from __future__ import annotations

from datetime import datetime

import duckdb

_TIME_HINT = ("Time", "time")

KEEP: dict[str, tuple[str, ...]] = {
    "lte_call": (
        "SummaryCreateTime", "OngoingFlag", "IMSI", "MDN", "IMEI",
        "ServiceCode", "PayCode", "Gender", "Age", "Vendor", "Model",
        "PGW_ID", "ims_PGW_ID", "SGW_ID", "MME_ID",
        "S6a_Authentication_Equip_ID", "S6a_Location_Equip_ID", "S13_Equip_ID",
        "First_eNB_ID", "First_Cell_ID", "Last_eNB_ID", "Last_Cell ID",
        "First_eNB_PLMN", "Last_eNB_PLMN",
        "interval_First_eNB_C_UID", "interval_First_TAC",
        "call_type", "old_call_type", "call_start_time", "call_end_time",
        "call_duration_time", "APN", "ims_APN", "PDN_Type", "ims_PDN_Type",
        "s6a_error_Cause", "s6a_error_Message", "s13_error_Cause",
        "s1ap_error_Cause", "s1ap_error_Message",
        "emm_error_Cause", "emm_error_Message",
        "esm_error_Cause", "esm_error_Message",
        "s11_error_Cause", "s11_error_Message",
        "s10_error_Cause", "s10_error_Message",
        "s3_error_Cause", "s3_error_Message", "s13_error_Message",
        "AuthenticationInformation_Cause", "UpdateLocation_Cause", "MEIdentityCheck_Cause",
        "UEContextRelease_Cause", "UEContextReleaseRequest_Cause",
        "PathSwitch_count", "PathSwitchFailure_count",
        "attempt_flag", "success_flag", "drop_flag",
        "data_attempt_flag", "data_success_flag", "ims_attempt_flag", "ims_success_flag",
        "paging_attempt_flag", "paging_success_flag", "detach_flag", "npr_flag",
        "auth_attempt_flag", "auth_success_flag",
        "location_attempt_flag", "location_success_flag",
        "mecheck_attempt_flag", "mecheck_success_flag",
        "first_error_interface_protocol", "first_error_message",
        "first_error_time", "first_error_cause",
        "last_error_interface_protocol", "last_error_message", "last_error_cause",
        "old_call_error_interface_protocol", "old_call_error_cause",
        "old_call_s1ap_release_cause", "old_call_end_time",
        "old_call_Last_eNB_ID", "old_call_Last_Cell_ID",
        "initial_access_duration", "initial_access_msg_count",
        "initial_core_duration", "initial_core_msg_count",
        "initial_paging_duration", "initial_paging_msg_count",
        "initial_paging_attempt_count",
        "imsi_mcc_mnc_info", "ue_dcnr", "initial_nr_conn_time",
        "mme_restrict_dcnr", "equip_nw",
    ),
    "transport": (
        "SummaryCreateTime", "OngoingFlag", "IMSI", "Vendor", "Model",
        "SGW_ID", "PGW_ID", "MME_ID", "eNB_ID", "Cell_ID", "APN", "SessionQCI",
        "App_ID", "App_Group_Code", "Protocol", "Tethering",
        "SynCount", "SynAckCount", "RstCount", "FinCount", "AckTime", "SynFirstTime",
        "SynAckFirstTime", "SynMssSize", "SynAckMssSize",
        "UplinkWindowCloseCount", "DnlinkWindowCloseCount",
        "UplinkRTTCount", "UplinkRTTTime", "DnlinkRTTCount", "DnlinkRTTTime",
        "Traffic_Total_Uplink_PacketCount", "Traffic_Total_Uplink_PayloadPacketCount",
        "Traffic_Total_Uplink_PayloadSize", "Traffic_Total_Uplink_RetransPacketCount",
        "Traffic_Total_Uplink_ThroughputTime",
        "Traffic_Total_Dnlink_PacketCount", "Traffic_Total_Dnlink_PayloadPacketCount",
        "Traffic_Total_Dnlink_PayloadSize", "Traffic_Total_Dnlink_RetransPacketCount",
        "Traffic_Total_Dnlink_ThroughputTime",
        "ICMP_Delta_Uplink_IcmpPacketCount", "ICMP_Delta_Dnlink_IcmpPacketCount",
        "ClientHelloDomain", "LastDnsQueryName", "imsi_mcc_mnc_info",
    ),
    "dns": (
        "SummaryCreateTime", "OngoingFlag", "IMSI", "Vendor", "Model",
        "SGW_ID", "PGW_ID", "eNB_ID", "Cell_ID", "APN", "App_Group_Code",
        "RequestFirstTime", "QueryType", "QueryName", "QueryRetryCount",
        "ResponseTime", "ReplyCode", "AnswerCount", "ResolvedIP", "TimeToLive",
        "dns_retry_sequence", "dns_retry_interval",
        "Traffic_Total_Uplink_PayloadSize", "Traffic_Total_Dnlink_PayloadSize",
        "ICMP_Delta_Uplink_IcmpPacketCount", "ICMP_Delta_Dnlink_IcmpPacketCount",
        "imsi_mcc_mnc_info",
    ),
    "http": (
        "SummaryCreateTime", "OngoingFlag", "IMSI", "Vendor", "Model",
        "SGW_ID", "PGW_ID", "eNB_ID", "Cell_ID", "APN", "SessionQCI",
        "App_ID", "App_Group_Code",
        "RequestTime", "Method", "Host", "Host_Prefix", "Host_Site",
        "ResponseTime", "StatusCode",
        "HTTP_Total_Request_ContentLength", "HTTP_Total_Response_ContentLength",
        "HTTP_Total_Response_PayloadSize",
        "UplinkRTTCount", "UplinkRTTTime", "DnlinkRTTCount", "DnlinkRTTTime",
        "Traffic_Total_Dnlink_PayloadPacketCount",
        "Traffic_Total_Dnlink_RetransPacketCount",
        "Traffic_Total_Dnlink_PayloadSize", "Traffic_Total_Dnlink_ThroughputTime",
        "imsi_mcc_mnc_info",
    ),
}


def sql_name(name: str) -> str:
    """공백 필드명(`Last_Cell ID`) 정규화 — 구 loader.sql_name 과 동일."""
    return name.replace(" ", "_")


_VARCHAR = {
    "IMSI", "MDN", "IMEI", "APN", "ims_APN", "Vendor", "Model", "Gender",
    "Age", "ServiceCode", "PayCode", "QueryName", "Host", "Host_Site",
    "Host_Prefix", "Method", "ResolvedIP", "ClientHelloDomain",
    "LastDnsQueryName", "App_Group_Code", "App_ID", "imsi_mcc_mnc_info",
    "First_eNB_ID", "Last_eNB_ID", "eNB_ID", "old_call_Last_eNB_ID",
    "First_eNB_PLMN", "Last_eNB_PLMN", "equip_nw",
}


def col_type(name: str) -> str:
    """구 loader._col_expr 의 TRY_CAST 규칙과 동일한 타입 배정."""
    n = sql_name(name)
    if any(h in n for h in _TIME_HINT) and "Count" not in n:
        return "DOUBLE"
    if n in _VARCHAR:
        return "VARCHAR"
    return "BIGINT"


def cast_value(name: str, text: str):
    """레코드 문자열 값 → 타입 값. TRY_CAST 와 같은 관용 — 못 바꾸면 NULL."""
    if text == "" or text is None:
        return None
    kind = col_type(name)
    try:
        if kind == "DOUBLE":
            return float(text)
        if kind == "BIGINT":
            return int(float(text))
        return text
    except (TypeError, ValueError):
        return None


WAREHOUSE_DDL = """
CREATE TABLE IF NOT EXISTS cell_master (
    cell_key      VARCHAR PRIMARY KEY,
    enb_id        VARCHAR,
    cell_id       INTEGER,
    c_uid         BIGINT,
    tac           INTEGER,
    plmn          VARCHAR,
    latitude      DOUBLE,
    longitude     DOUBLE,
    adm_sido_cd   VARCHAR, adm_sido_nm VARCHAR,
    adm_sgg_cd    VARCHAR, adm_sgg_nm  VARCHAR,
    adm_emd_cd    VARCHAR, adm_emd_nm  VARCHAR,
    geo_status    VARCHAR,
    rat           VARCHAR,
    mme_id        INTEGER, sgw_id INTEGER, pgw_id INTEGER, ims_pgw_id INTEGER,
    traffic_weight DOUBLE,
    display_name  VARCHAR
);

CREATE TABLE IF NOT EXISTS core_node (
    node_type    VARCHAR,
    node_id      INTEGER,
    display_name VARCHAR,
    region_label VARCHAR,
    vendor       VARCHAR,
    note         VARCHAR,
    cell_cnt     INTEGER,
    PRIMARY KEY (node_type, node_id)
);

-- ⓪ 파일 감지 (§2.3.1) — 결측·지각·부분 파일을 이 대장으로 판정한다
CREATE TABLE IF NOT EXISTS file_ledger (
    series       VARCHAR,
    slot_ts      TIMESTAMP,
    path         VARCHAR,
    status       VARCHAR,     -- OK | MISSING | EMPTY | PARTIAL | CORRUPT | LATE | LATE_DISCARDED
    records      BIGINT,
    ingested_at  TIMESTAMP,
    arrival_lag_min INTEGER,
    PRIMARY KEY (series, slot_ts)
);

-- ③ 1분 집계 — 접속품질 (Call Type × KPI Flag, §4.1)
CREATE TABLE IF NOT EXISTS kpi_cell_min (
    slot_ts TIMESTAMP, cell_key VARCHAR, call_type INTEGER,
    end_records BIGINT, flag_missing BIGINT,
    attempt BIGINT, success BIGINT, drop_cnt BIGINT, completed BIGINT,
    data_att BIGINT, data_suc BIGINT, ims_att BIGINT, ims_suc BIGINT,
    paging_att BIGINT, paging_suc BIGINT, auth_att BIGINT, auth_suc BIGINT,
    loc_att BIGINT, loc_suc BIGINT, mec_att BIGINT, mec_suc BIGINT,
    detach_cnt BIGINT, npr_cnt BIGINT,
    fe_any BIGINT, fe_timeout BIGINT, fe_recovered BIGINT, fe_propagated BIGINT,
    fe_i1 BIGINT, fe_i2 BIGINT, fe_i3 BIGINT, fe_i4 BIGINT,
    fe_i5 BIGINT, fe_i6 BIGINT, fe_i7 BIGINT, fe_i8 BIGINT,
    to_i1 BIGINT, to_i2 BIGINT, to_i3 BIGINT, to_i4 BIGINT,
    to_i5 BIGINT, to_i6 BIGINT, to_i8 BIGINT,
    drop_normal BIGINT, drop_radio BIGINT, drop_transport BIGINT,
    drop_node BIGINT, drop_nas BIGINT, drop_proto BIGINT,
    drop_unspec BIGINT, drop_unknown BIGINT,
    acc_dur_sum DOUBLE, acc_dur_cnt BIGINT,
    core_dur_sum DOUBLE, core_dur_cnt BIGINT,
    pag_dur_sum DOUBLE, pag_dur_cnt BIGINT, pag_att_sum BIGINT,
    ps_cnt BIGINT, psf_cnt BIGINT,
    dcnr_ue BIGINT, dcnr_nr_ok BIGINT, dcnr_restrict BIGINT,
    old_fail_cnt BIGINT, old_same_iface BIGINT,
    subs BIGINT, fail_subs BIGINT,
    PRIMARY KEY (slot_ts, cell_key, call_type)
);

CREATE TABLE IF NOT EXISTS kpi_node_min (
    slot_ts TIMESTAMP, node_type VARCHAR, node_id INTEGER, call_type INTEGER,
    attempt BIGINT, success BIGINT, drop_cnt BIGINT,
    fe_any BIGINT, fe_timeout BIGINT,
    fe_i1 BIGINT, fe_i2 BIGINT, fe_i3 BIGINT, fe_i5 BIGINT, fe_i6 BIGINT,
    acc_dur_sum DOUBLE, acc_dur_cnt BIGINT,
    core_dur_sum DOUBLE, core_dur_cnt BIGINT,
    subs BIGINT, fail_subs BIGINT,
    PRIMARY KEY (slot_ts, node_type, node_id, call_type)
);

CREATE TABLE IF NOT EXISTS kpi_ue_min (
    slot_ts TIMESTAMP, vendor VARCHAR, model VARCHAR,
    attempt BIGINT, success BIGINT, drop_cnt BIGINT, fe_any BIGINT,
    dcnr_ue BIGINT, dcnr_nr_ok BIGINT,
    subs BIGINT, fail_subs BIGINT,
    PRIMARY KEY (slot_ts, vendor, model)
);

CREATE TABLE IF NOT EXISTS kpi_apn_min (
    slot_ts TIMESTAMP, apn VARCHAR, call_type INTEGER,
    attempt BIGINT, success BIGINT, drop_cnt BIGINT, fe_any BIGINT,
    esm27 BIGINT, s11_93 BIGINT,
    PRIMARY KEY (slot_ts, apn, call_type)
);

-- EV-01 / EV-03 — Cause·Message 분포
CREATE TABLE IF NOT EXISTS cause_min (
    slot_ts TIMESTAMP, scope VARCHAR, interface INTEGER,
    cause INTEGER, message INTEGER, cell_key VARCHAR, cnt BIGINT,
    PRIMARY KEY (slot_ts, scope, interface, cause, message, cell_key)
);

-- ③ 1분 집계 — 서비스품질 (User Traffic, §4.2)
CREATE TABLE IF NOT EXISTS kpi_u_cell_min (
    slot_ts TIMESTAMP, cell_key VARCHAR, series VARCHAR,
    sessions BIGINT,
    tcp_att BIGINT, tcp_ok BIGINT, syn_retry BIGINT, rst_cnt BIGINT,
    zerowin_up BIGINT, zerowin_dn BIGINT, mss_mismatch BIGINT,
    rtt_up_time DOUBLE, rtt_up_cnt BIGINT, rtt_dn_time DOUBLE, rtt_dn_cnt BIGINT,
    retrans_up BIGINT, paypkt_up BIGINT, retrans_dn BIGINT, paypkt_dn BIGINT,
    bytes_up BIGINT, bytes_dn BIGINT, thr_time_up DOUBLE, thr_time_dn DOUBLE,
    icmp_up BIGINT, icmp_dn BIGINT,
    dns_q BIGINT, dns_ok BIGINT, dns_fail BIGINT, dns_noresp BIGINT,
    dns_retry BIGINT, dns_retry_seq_sum BIGINT, dns_retry_int_sum DOUBLE,
    dns_rt_sum DOUBLE, dns_rt_cnt BIGINT,
    http_tx BIGINT, http_ok BIGINT, http_4xx BIGINT, http_5xx BIGINT,
    http_noresp BIGINT, http_rt_sum DOUBLE, http_rt_cnt BIGINT,
    domain_known BIGINT, keepalive BIGINT, tethering BIGINT,
    subs BIGINT,
    PRIMARY KEY (slot_ts, cell_key, series)
);

CREATE TABLE IF NOT EXISTS kpi_domain_min (
    slot_ts TIMESTAMP, series VARCHAR, host_site VARCHAR,
    sessions BIGINT, http_tx BIGINT, http_ok BIGINT, http_4xx BIGINT, http_5xx BIGINT,
    http_rt_sum DOUBLE, http_rt_cnt BIGINT,
    rtt_up_time DOUBLE, rtt_up_cnt BIGINT, rtt_dn_time DOUBLE, rtt_dn_cnt BIGINT,
    retrans_dn BIGINT, paypkt_dn BIGINT,
    dns_q BIGINT, dns_fail BIGINT,
    PRIMARY KEY (slot_ts, series, host_site)
);

-- §4.4 시스템 건전성 (자기 감시)
CREATE TABLE IF NOT EXISTS health_min (
    slot_ts TIMESTAMP, metric VARCHAR, value DOUBLE, detail VARCHAR,
    PRIMARY KEY (slot_ts, metric)
);
"""


def open_db() -> duckdb.DuckDBPyConnection:
    """인메모리 DuckDB — raw_<계열> + 창고 테이블을 갖춘 계산 엔진."""
    con = duckdb.connect(":memory:")
    con.execute("SET TimeZone='Asia/Seoul'")
    for series, fields in KEEP.items():
        cols = ", ".join(f'"{sql_name(f)}" {col_type(f)}' for f in fields)
        con.execute(f"CREATE TABLE raw_{series} ({cols})")
    con.execute(WAREHOUSE_DDL)
    return con


def insert_minute(con, series: str, rows: list[tuple]) -> int:
    """raw_<계열> 에 분당 레코드를 적재한다.

    **DataFrame bulk append 를 쓴다.** DuckDB 의 executemany INSERT 는 행마다
    개별 실행이라 분당 수천 행에 수십 초가 걸린다(실측: 5천 행 36초, 누적 악화).
    DataFrame 을 통째로 넘기면 컬럼 지향으로 한 번에 들어가 1초 미만이다.
    """
    if not rows:
        return 0
    import pandas as pd
    cols = [sql_name(f) for f in KEEP[series]]
    df = pd.DataFrame(rows, columns=cols)
    con.append(f"raw_{series}", df)
    return len(rows)


def trim_raw(con, before: datetime) -> None:
    """재집계 창을 지난 원시를 버린다 — 시도·완료 귀속이 둘 다 끝난 분만."""
    epoch = before.timestamp()
    for series, col in (
        ("lte_call", "call_start_time"), ("transport", "SummaryCreateTime"),
        ("dns", "SummaryCreateTime"), ("http", "SummaryCreateTime"),
    ):
        con.execute(
            f"DELETE FROM raw_{series} "
            f"WHERE COALESCE({col}, 0) < {epoch} "
            f"AND COALESCE(call_end_time, 0) < {epoch}" if series == "lte_call" else
            f"DELETE FROM raw_{series} WHERE COALESCE({col}, 0) < {epoch}"
        )
