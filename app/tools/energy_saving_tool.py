import asyncio
import time
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.logging import get_logger
from app.services.cell_resolver import resolve_cell_identifier
from app.services.database import get_session_factory
from app.services.energy_analysis import (
    build_expansion_hour_evidence,
    build_expansion_record,
    collect_site_type_cgis,
    derive_expansion_result_status,
    derive_process_evidence_status,
    derive_whitelist_status,
    enrich_constriction_records,
    filter_judgeable_expansion_evidence,
    is_pre_sleep_hour,
    normalize_boolean_flag,
)
from app.services.energy_report import build_single_cell_energy_report
from app.services.energy_evidence import query_neighbor_relations, query_site_types
from app.utils.cell_lookup import resolution_error_response
from app.utils.export_util import truncate_and_export
from app.utils.sql_helpers import ensure_datetime, error_response, fetch_rows

DB_SCHEMA_AGENT = get_settings().db_schema_agent

logger = get_logger("energy_saving_tool")

# ── 辅助函数 ──


def _fill_base_info(result: dict[str, Any], source: dict[str, Any], cgi: str) -> None:
    """将基础信息填充到 result 中（仅在尚未填充时）。"""
    defaults = {"cell_name": cgi, "dist_name": "-", "county_name": "-", "prod_name": "-"}
    for key, default in defaults.items():
        if key not in result:
            result[key] = source.get(key) or default


def _build_md_table(headers: list[str], rows: list[str], empty_msg: str = "暂无数据") -> str:
    """构建 Markdown 表格字符串。"""
    if not rows:
        return f"| — | {empty_msg} |"
    header_line = "| " + " | ".join(headers) + " |"
    sep_line = "|" + "|".join(["---"] * len(headers)) + "|"
    return header_line + "\n" + sep_line + "\n" + "\n".join(rows)


def _needs_pre_sleep_load(analysis_target: str) -> bool:
    return analysis_target in {"all", "constriction", "pre_sleep_load"}


def _elapsed_ms(started_at: float) -> float:
    return round((time.perf_counter() - started_at) * 1000, 2)


def _record_stage_timing(
    performance: dict[str, float],
    stage: str,
    started_at: float,
) -> None:
    elapsed_ms = _elapsed_ms(started_at)
    performance[f"{stage}_ms"] = elapsed_ms
    logger.info("energy_stage_timing stage=%s elapsed_ms=%.2f", stage, elapsed_ms)


async def _run_timed_stage(
    stage: str,
    operation: Any,
    performance: dict[str, float],
) -> Any:
    started_at = time.perf_counter()
    try:
        return await operation
    finally:
        _record_stage_timing(performance, stage, started_at)


# pre_sleep_load 统计天数
PRE_SLEEP_LOAD_DAYS: int = 7
# PRB 利用率高负荷阈值（%）
PRB_HIGH_LOAD_THRESHOLD: float = 50.0
# 不合规项最大返回条数
MAX_UNQUALIFIED_ITEMS: int = 10
# 扩展过程证据回看 15 天（含统计日）
EXPANSION_LOOKBACK_DAYS: int = 14
EXPANSION_HOURS = [22, 23, 0, 1, 2, 3, 4, 5, 6, 7]


TOOL_DESCRIPTION = "通过 CGI 或小区中文名分析单个5G小区的节电详情，包含休眠扩展、休眠收缩、负荷状态和休眠生效前负荷偏高。"
TOOL_INPUT_SCHEMA = {
        "type": "object",
        "properties": {
            "cgi": {
                "type": "string",
                "description": "小区全球标识，格式 460-00-基站号-小区号；与 cell_name 同时传入时优先",
            },
            "cell_name": {
                "type": "string",
                "description": "小区中文名或其连续片段，未提供 CGI 时使用包含匹配",
            },
            "analysis_target": {
                "type": "string",
                "enum": ["all", "expansion", "constriction", "pre_sleep_load"],
                "description": "分析目标",
            },
            "stat_time": {
                "type": "string",
                "description": "统计日期 (YYYY-MM-DD)",
            },
        },
        "required": ["analysis_target"],
}


async def analyze_single_cell_energy(
    analysis_target: str,
    db: AsyncSession,
    cgi: str | None = None,
    cell_name: str | None = None,
    stat_time: str | None = None,
) -> dict[str, Any]:
    """分析单个5G小区的节电详情。

    返回结构化数据 + 基础表格，由 LLM 组装最终报告。
    """
    total_started_at = time.perf_counter()
    performance: dict[str, float] = {}
    cgi = (cgi or "").strip()
    cell_name = (cell_name or "").strip()
    if not cgi and not cell_name:
        return error_response("请提供 CGI 或小区中文名。")

    logger.info(
        "单小区节电分析: cgi=%s, cell_name=%s, target=%s, stat_time=%s",
        cgi,
        cell_name,
        analysis_target,
        stat_time,
    )

    need_expansion = analysis_target in ("all", "expansion")
    need_constriction = analysis_target in ("all", "constriction")
    need_load = analysis_target in ("all", "load")
    need_pre_sleep_load = _needs_pre_sleep_load(analysis_target)

    # ── Step 1: 确定查询日期（始终先查 DB 最新日期，用户日期超出时自动兜底）──
    if analysis_target == "pre_sleep_load":
        latest_sql = text(f"""
            SELECT MAX(stat_time) as max_date
            FROM {DB_SCHEMA_AGENT}.jd_cell_pre_hour_busy
        """)
    else:
        latest_sql = text(f"""
            SELECT LEAST(
                (SELECT MAX(stat_time) FROM {DB_SCHEMA_AGENT}.jd_cell_expansion_day),
                (SELECT MAX(stat_time) FROM {DB_SCHEMA_AGENT}.jd_cell_constriction_day)
            ) as max_date
        """)

    latest_started_at = time.perf_counter()
    latest_result = await db.execute(latest_sql)
    latest_row = latest_result.mappings().first()
    db_latest_date = ensure_datetime(latest_row["max_date"]) if latest_row else None
    _record_stage_timing(performance, "latest_date", latest_started_at)

    if not db_latest_date:
        return error_response("暂无节电分析数据。")

    date_note = ""
    if stat_time:
        user_date = _parse_stat_time(stat_time)
        if user_date and user_date.date() > (db_latest_date.date() if isinstance(db_latest_date, datetime) else db_latest_date):
            date_note = f"您查询的日期 {stat_time} 暂无数据，已自动查询最新数据日期 {db_latest_date.strftime('%Y-%m-%d')}"
            query_date = db_latest_date
        else:
            query_date = user_date
    else:
        query_date = db_latest_date

    display_date = query_date.strftime("%Y-%m-%d")

    name_resolution = None
    if not cgi:
        name_resolution_started_at = time.perf_counter()
        try:
            name_resolution = await resolve_cell_identifier(
                db=db,
                cell_name=cell_name,
                network="5G",
            )
        except Exception as exc:
            logger.error("单小区名称解析异常: %s", exc, exc_info=True)
            return error_response("小区名称解析失败，请稍后重试。")
        finally:
            _record_stage_timing(
                performance,
                "name_resolution",
                name_resolution_started_at,
            )
        if name_resolution.status != "resolved":
            return resolution_error_response(name_resolution)
        cgi = name_resolution.cgi or ""

    # 构建返回结果（基础信息从扩展/收缩结果中获取）
    result: dict[str, Any] = {
        "success": True,
        "cgi": cgi,
        "analysis_target": analysis_target,
        "stat_time": display_date,
    }
    if name_resolution:
        result["cell_name_match"] = {
            "query": name_resolution.query,
            "match_type": name_resolution.match_type,
        }
    if date_note:
        result["date_note"] = date_note

    # ── Step 2 & 3: 并行执行扩展分析、收缩分析、参数核查（各自独立 session）──
    tasks = []
    task_names = []

    if need_expansion:
        tasks.append(_run_timed_stage(
            "expansion",
            _query_expansion_data(cgi, query_date),
            performance,
        ))
        task_names.append("expansion")

    if need_constriction:
        tasks.append(_run_timed_stage(
            "constriction",
            _query_constriction_data(cgi, query_date),
            performance,
        ))
        task_names.append("constriction")

    if need_pre_sleep_load:
        tasks.append(_run_timed_stage(
            "pre_sleep_load",
            _query_pre_sleep_load_data(cgi, query_date),
            performance,
        ))
        task_names.append("pre_sleep_load")

    # 参数核查也加入并行（扩展分析需要）
    if need_expansion:
        async def _query_param_check_with_session():
            async with get_session_factory()() as sess:
                return await _query_param_check(sess, cgi, display_date)
        tasks.append(_run_timed_stage(
            "param_check",
            _query_param_check_with_session(),
            performance,
        ))
        task_names.append("param_check")

    results = await asyncio.gather(*tasks) if tasks else []
    results_map = dict(zip(task_names, results))

    # 处理扩展分析结果
    if "expansion" in results_map:
        expansion_result = results_map["expansion"]
        result["expansion_table"] = expansion_result["table"]
        result["expansion_process_data"] = expansion_result.get("process_data", [])
        result["expansion_process_table"] = expansion_result.get("process_table", "")
        result["expansion_candidate_table"] = expansion_result.get("candidate_table", "")
        result["expansion_deployment_table"] = expansion_result.get("deployment_table", "")
        result["expansion_result_status"] = expansion_result.get(
            "expansion_result_status",
            derive_expansion_result_status(
                expansion_result.get("data", [None])[0]
                if expansion_result.get("data")
                else None,
            ),
        )
        result["expansion_process_evidence_status"] = expansion_result.get(
            "process_evidence_status",
            derive_process_evidence_status(
                expansion_result.get("process_data", []),
            ),
        )
        expansion_data, expansion_meta = truncate_and_export(
            expansion_result["data"],
            prefix="single_cell_expansion",
        )
        result["expansion_data"] = expansion_data
        for key, value in expansion_meta.items():
            result[f"expansion_{key}"] = value
        result["high_load_type"] = expansion_result.get("high_load_type")
        # 参数核查结果（仅保留关键字段）
        param_check_raw = results_map.get(
            "param_check",
            {
                "success": False,
                "is_compliant": None,
                "error": "参数核查暂不可用",
            },
        )
        result["param_check"] = {
            "success": param_check_raw.get("success", False),
            "is_compliant": param_check_raw.get("is_compliant"),
            "unqualified_count": param_check_raw.get("unqualified_count", 0),
        }
        if param_check_raw.get("error"):
            result["param_check"]["error"] = param_check_raw["error"]
        # 如果不合规，保留不合规项（用于表格输出）
        if param_check_raw.get("is_compliant") is False:
            result["param_check"]["unqualified_items"] = param_check_raw.get("unqualified_items", [])[:MAX_UNQUALIFIED_ITEMS]
        # 节电信息 & 白名单信息（统一从扩展表获取）
        result["jd_type"] = expansion_result.get("jd_type")
        result["jd_reason"] = expansion_result.get("reason")
        # 基础信息
        _fill_base_info(result, expansion_result, cgi)

    # 处理收缩分析结果
    if "constriction" in results_map:
        constriction_result = results_map["constriction"]
        result["constriction_table"] = constriction_result["table"]
        result["constriction_main_table"] = constriction_result.get("main_table", "")
        result["constriction_relation_table"] = constriction_result.get("relation_table", "")
        result["constriction_load_table"] = constriction_result.get("load_table", "")
        result["constriction_result_table"] = constriction_result.get("result_table", "")
        constriction_data, constriction_meta = truncate_and_export(
            constriction_result["data"],
            prefix="single_cell_constriction",
        )
        result["constriction_data"] = constriction_data
        for key, value in constriction_meta.items():
            result[f"constriction_{key}"] = value
        # 基础信息（如果扩展分析没查，从收缩结果获取）
        if "cell_name" not in result:
            _fill_base_info(result, constriction_result, cgi)

    # ── Step 4: 仅负荷状态 ──
    if need_load and not need_expansion:
        load_info = await _check_high_load_with_base_info(db, cgi, query_date)
        result["high_load_type"] = load_info.get("high_load_type", "否")
        # 基础信息
        if "cell_name" not in result:
            _fill_base_info(result, load_info, cgi)

    # ── Step 5: 休眠生效前负荷偏高分析 ──
    if "pre_sleep_load" in results_map:
        pre_sleep_result = results_map["pre_sleep_load"]
        result["pre_sleep_load_table"] = pre_sleep_result["table"]
        result["high_prb_days_7d"] = pre_sleep_result.get("high_prb_days_7d", 0)
        pre_sleep_data, pre_sleep_meta = truncate_and_export(
            pre_sleep_result["data"],
            prefix="single_cell_pre_sleep_load",
        )
        result["pre_sleep_load_data"] = pre_sleep_data
        for key, value in pre_sleep_meta.items():
            result[f"pre_sleep_load_{key}"] = value
        # 基础信息
        if "cell_name" not in result:
            _fill_base_info(result, pre_sleep_result, cgi)

    # 确保基础信息有默认值
    result.setdefault("cell_name", cgi)
    result.setdefault("dist_name", "-")
    result.setdefault("county_name", "-")
    result.setdefault("prod_name", "-")
    whitelist_sources = [
        results_map.get("expansion"),
        results_map.get("constriction"),
    ]
    result["whitelist_status"] = derive_whitelist_status(*whitelist_sources)
    result["is_whitelist"] = result["whitelist_status"] == "whitelisted"
    whitelist_source = next(
        (
            source
            for source in whitelist_sources
            if source and normalize_boolean_flag(source.get("is_whitelist"))
        ),
        None,
    )
    result["whitelist_reason"] = (
        whitelist_source.get("whitelist_reason") if whitelist_source else None
    )
    result["jd_starttime"] = (
        whitelist_source.get("starttime") if whitelist_source else None
    )
    result["jd_endtime"] = (
        whitelist_source.get("endtime") if whitelist_source else None
    )
    result.setdefault("high_load_type", None if need_expansion else "否")

    report_started_at = time.perf_counter()
    result["report_content"] = build_single_cell_energy_report(result)
    _record_stage_timing(performance, "report_render", report_started_at)
    performance["total_ms"] = _elapsed_ms(total_started_at)
    logger.info(
        "energy_stage_timing stage=total elapsed_ms=%.2f",
        performance["total_ms"],
    )
    result["performance"] = performance

    return result

def _format_hour_interval(hour: int) -> str:
    return f"{hour:02d}:00-{hour:02d}:59"


def _format_optional_percentage(value: Any) -> str:
    return "数据缺失" if value is None else f"{float(value):.1f}%"


def _format_optional_boolean(value: bool | None) -> str:
    if value is None:
        return "数据缺失"
    return "是" if value else "否"


def _build_expansion_process_table(rows: list[dict[str, Any]]) -> str:
    table_rows = []
    for row in rows:
        sleep_seconds = row.get("avg_sleep_seconds")
        sleep_display = (
            "数据缺失"
            if sleep_seconds is None
            else _format_sleep_duration(int(sleep_seconds))
        )
        table_rows.append(
            f"| {_format_hour_interval(row['hour'])} | "
            f"{_format_optional_percentage(row.get('low_flow_pct'))} | "
            f"{_format_optional_boolean(row.get('is_low_business'))} | "
            f"{sleep_display} | "
            f"{_format_optional_boolean(row.get('is_zero_sleep'))} | "
            f"{row.get('suggestion') or '数据缺失'} |"
        )
    return _build_md_table(
        ["时段", "低业务占比", "低业务判断", "平均休眠时长", "零休眠判断", "扩展建议"],
        table_rows,
    )


def _build_expansion_candidate_table(row: dict[str, Any]) -> str:
    return _build_md_table(
        ["分析时窗", "可扩展小时", "可扩展时长"],
        [
            f"| 22:00至次日08:00 | {row.get('hour_filter') or '无可扩展时段'} | "
            f"{row.get('hour_int') or 0}小时 |",
            f"| 00:00至06:00 | {row.get('hour_filter_early') or '无可扩展时段'} | "
            f"{row.get('hour_int_early') or 0}小时 |",
        ],
    )


def _build_expansion_deployment_table(row: dict[str, Any]) -> str:
    return _build_md_table(
        ["分析口径", "分析后的部署小时集合", "连续部署时段"],
        [
            f"| 含扩展时段 | {row.get('deploy_hours') or '—'} | "
            f"{row.get('deploy_hours_continuous') or '—'} |",
            f"| 仅常规夜间时段 | {row.get('deploy_hours_early') or '—'} | "
            f"{row.get('deploy_hours_continuous_early') or '—'} |",
        ],
    )


async def _query_expansion_data(
    cgi: str,
    stat_time: datetime,
) -> dict[str, Any]:
    """读取 V1.4 扩展结果，并补齐逐小时过程证据。"""
    expansion_sql = text(f"""
        SELECT cgi, stat_time, is_highload, jd_type, reason, starttime, endtime,
               is_whitelist,
               cell_name, dist_name, county_name, prod_name,
               hour_detail, hour_filter, hour_int,
               hour_filter_early, hour_int_early,
               deploy_hours, deploy_hours_continuous,
               deploy_hours_early, deploy_hours_continuous_early
        FROM {DB_SCHEMA_AGENT}.jd_cell_expansion_day
        WHERE cgi = :cgi AND stat_time = :stat_time
    """)
    sleep_sql = text(f"""
        SELECT hours,
               AVG(
                   CASE
                       WHEN ee_shallowsleeptimerru IS NOT NULL
                        AND ee_deepsleeptimerru IS NOT NULL
                        AND ee_supersleeptimerru IS NOT NULL
                       THEN ee_shallowsleeptimerru
                          + ee_deepsleeptimerru
                          + ee_supersleeptimerru
                   END
               ) AS avg_sleep_sum
        FROM {DB_SCHEMA_AGENT}.jd_cell_detail_hour_nr
        WHERE cgi = :cgi
          AND stat_time >= :start_date
          AND stat_time <= :end_date
          AND hours = ANY(:night_hours)
        GROUP BY hours
        ORDER BY hours
    """)
    rows, sleep_rows = await asyncio.gather(
        fetch_rows(expansion_sql, {"cgi": cgi, "stat_time": stat_time}),
        fetch_rows(
            sleep_sql,
            {
                "cgi": cgi,
                "start_date": stat_time - timedelta(days=EXPANSION_LOOKBACK_DAYS),
                "end_date": stat_time,
                "night_hours": EXPANSION_HOURS,
            },
        ),
    )
    expansion_data = [build_expansion_record(row) for row in rows]
    base_row = expansion_data[0] if expansion_data else None
    raw_process_data = build_expansion_hour_evidence(base_row or {}, sleep_rows)
    process_data = filter_judgeable_expansion_evidence(raw_process_data)
    process_table = (
        _build_expansion_process_table(process_data) if process_data else ""
    )
    candidate_table = _build_expansion_candidate_table(base_row) if base_row else ""
    deployment_table = (
        _build_expansion_deployment_table(base_row) if base_row else ""
    )

    starttime = ensure_datetime(base_row.get("starttime")) if base_row else None
    endtime = ensure_datetime(base_row.get("endtime")) if base_row else None
    whitelist_flag = base_row.get("is_whitelist") if base_row else None
    is_whitelisted = (
        normalize_boolean_flag(whitelist_flag)
        if whitelist_flag not in (None, "")
        else None
    )

    return {
        "data": expansion_data,
        "process_data": process_data,
        "expansion_result_status": derive_expansion_result_status(base_row),
        "process_evidence_status": derive_process_evidence_status(raw_process_data),
        "process_table": process_table,
        "candidate_table": candidate_table,
        "deployment_table": deployment_table,
        "table": "\n\n".join(
            table for table in (candidate_table, deployment_table) if table
        ),
        "high_load_type": (base_row.get("is_highload") or "否") if base_row else None,
        "jd_type": base_row.get("jd_type") if base_row else None,
        "reason": base_row.get("reason") if base_row else None,
        "whitelist_reason": (
            base_row.get("reason") if base_row and is_whitelisted is True else None
        ),
        "starttime": starttime.strftime("%Y-%m-%d") if starttime else None,
        "endtime": endtime.strftime("%Y-%m-%d") if endtime else None,
        "is_whitelist": is_whitelisted,
        # 基础信息
        "cell_name": base_row.get("cell_name") if base_row else None,
        "dist_name": base_row.get("dist_name") if base_row else None,
        "county_name": base_row.get("county_name") if base_row else None,
        "prod_name": base_row.get("prod_name") if base_row else None,
    }


async def _query_constriction_data(
    cgi: str,
    stat_time: datetime,
) -> dict[str, Any]:
    """查询收缩结果，并批量补齐距离与双方站型。"""
    constriction_sql = text(f"""
        SELECT cgi, stat_time, hours, reason, around_cgi,
               around_cgi_cell_name, around_cgi_network_type, site_type,
               self_prb_rate_ul_before, self_prb_rate_dl_before,
               self_sleep_duration, prb_rate_ul, prb_rate_dl,
               prb_rate_ul_before, prb_rate_dl_before, prb_increase,
               before_sleep_hour, before_sleep_date,
               is_whitelist, starttime, endtime,
               cell_name, dist_name, county_name, prod_name
        FROM {DB_SCHEMA_AGENT}.jd_cell_constriction_day
        WHERE cgi = :cgi AND stat_time = :stat_time
    """)
    constriction_rows = await fetch_rows(
        constriction_sql,
        {"cgi": cgi, "stat_time": stat_time},
    )
    relation_pairs = {
        (str(row.get("cgi") or ""), str(row.get("around_cgi") or ""))
        for row in constriction_rows
        if row.get("cgi") and row.get("around_cgi")
    }
    relation_by_pair = await query_neighbor_relations(relation_pairs, fetch_rows)
    nr_cgis, lte_cgis = collect_site_type_cgis(
        constriction_rows,
        relation_by_pair,
    )
    site_type_by_cell = await query_site_types(nr_cgis, lte_cgis, fetch_rows)
    constriction_data = enrich_constriction_records(
        constriction_rows,
        relation_by_pair,
        site_type_by_cell,
    )

    main_table = _build_constriction_main_table(constriction_data)
    relation_table = _build_constriction_relation_table(constriction_data)
    load_table = _build_constriction_load_table(constriction_data)
    result_table = _build_constriction_result_table(constriction_data)

    first_row = constriction_rows[0] if constriction_rows else None
    whitelist_flag = first_row.get("is_whitelist") if first_row else None
    return {
        "data": constriction_data,
        "main_table": main_table,
        "relation_table": relation_table,
        "load_table": load_table,
        "result_table": result_table,
        "table": result_table,
        "is_whitelist": (
            normalize_boolean_flag(whitelist_flag)
            if whitelist_flag not in (None, "")
            else None
        ),
        "whitelist_reason": (
            first_row.get("whitelist_reason") if first_row else None
        ),
        "starttime": _format_date(first_row.get("starttime")) if first_row else None,
        "endtime": _format_date(first_row.get("endtime")) if first_row else None,
        # 基础信息
        "cell_name": first_row.get("cell_name") if first_row else None,
        "dist_name": first_row.get("dist_name") if first_row else None,
        "county_name": first_row.get("county_name") if first_row else None,
        "prod_name": first_row.get("prod_name") if first_row else None,
    }


def _format_prb(ul: Any, dl: Any) -> str:
    ul_display = _format_prb_value(ul)
    dl_display = _format_prb_value(dl)
    return f"上行{ul_display}/下行{dl_display}"


def _format_prb_value(value: Any) -> str:
    return "—" if value is None else f"{float(value):.1f}%"


def _format_date(value: Any) -> str | None:
    parsed = ensure_datetime(value)
    return parsed.strftime("%Y-%m-%d") if parsed else None


def _format_constriction_hour(hour: Any) -> str:
    if hour is None:
        return "—"
    return _format_hour_interval(int(hour))


def _format_before_time(row: dict[str, Any]) -> str:
    before_date = row.get("before_sleep_date") or "—"
    before_hour = row.get("before_sleep_hour")
    return (
        f"{before_date} {before_hour}:00"
        if before_hour is not None
        else str(before_date)
    )


def _related_cell(row: dict[str, Any]) -> str:
    related_cgi = row.get("around_cgi") or "—"
    related_name = row.get("around_cgi_cell_name") or "—"
    return f"{related_name}({related_cgi})"


def _build_constriction_main_table(rows: list[dict[str, Any]]) -> str:
    unique_rows = {}
    for row in rows:
        key = (row.get("hours"), row.get("before_sleep_date"), row.get("before_sleep_hour"))
        unique_rows.setdefault(key, row)
    table_rows = [
        f"| {_format_constriction_hour(row.get('hours'))} | {_format_before_time(row)} | "
        f"{_format_prb_value(row.get('self_prb_rate_ul_before'))} | "
        f"{_format_prb_value(row.get('self_prb_rate_dl_before'))} | "
        f"{_format_prb_value(row.get('main_prb_before'))} | "
        f"{row.get('self_sleep_duration') if row.get('self_sleep_duration') is not None else '—'} | "
        "纳入周边影响分析 |"
        for row in unique_rows.values()
    ]
    return _build_md_table(
        ["休眠时段", "休眠前时间", "休眠前上行PRB", "休眠前下行PRB", "休眠前最高PRB", "休眠时长(秒)", "判断"],
        table_rows,
    )


_RELATION_LABELS = {
    "allowed": "满足分析范围",
    "missing": "关系信息待核实",
}


def _build_constriction_relation_table(rows: list[dict[str, Any]]) -> str:
    table_rows = []
    for row in rows:
        distance = row.get("distance")
        distance_display = "—" if distance is None else f"{float(distance):.1f}米"
        relation_label = _RELATION_LABELS.get(
            row.get("relation_status"),
            "不满足分析范围",
        )
        table_rows.append(
            f"| {_related_cell(row)} | {row.get('around_network_type') or '—'} | "
            f"{distance_display} | {row.get('main_site_type') or '—'} | "
            f"{row.get('around_site_type') or '—'} | {relation_label} |"
        )
    return _build_md_table(
        ["关联小区", "制式", "距离", "主小区站型", "关联小区站型", "关系判断"],
        table_rows,
    )


def _format_increase(value: Any) -> str:
    return "—" if value is None else f"{float(value):.1f}个百分点"


def _build_constriction_load_table(rows: list[dict[str, Any]]) -> str:
    table_rows = [
        f"| {_format_constriction_hour(row.get('hours'))} | {_related_cell(row)} | "
        f"{_format_prb(row.get('prb_rate_ul_before'), row.get('prb_rate_dl_before'))} | "
        f"{_format_prb(row.get('prb_rate_ul'), row.get('prb_rate_dl'))} | "
        f"{_format_prb_value(row.get('around_prb_before'))} | "
        f"{_format_prb_value(row.get('around_prb_during'))} | "
        f"{_format_increase(row.get('prb_increase'))} | 受到影响 |"
        for row in rows
    ]
    return _build_md_table(
        ["主小区休眠时段", "关联小区", "休眠前PRB(上/下行)", "休眠期间PRB(上/下行)", "休眠前最高PRB", "休眠期间最高PRB", "抬升量", "影响判断"],
        table_rows,
    )


def _build_constriction_result_table(rows: list[dict[str, Any]]) -> str:
    table_rows = []
    for row in rows:
        evidence = (
            f"PRB最高由{_format_prb_value(row.get('around_prb_before'))}升至"
            f"{_format_prb_value(row.get('around_prb_during'))}，"
            f"抬升{_format_increase(row.get('prb_increase'))}"
        )
        table_rows.append(
            f"| {_format_constriction_hour(row.get('hours'))} | "
            f"{row.get('reason') or '关联小区负荷受影响'} | {_related_cell(row)} | "
            f"{evidence} | 剔除该时段 |"
        )
    return _build_md_table(
        ["原节电时段", "触发原因", "关联小区", "关键证据", "收缩建议"],
        table_rows,
    )


async def _check_high_load_with_base_info(db: AsyncSession, cgi: str, stat_time: datetime) -> dict[str, Any]:
    """检查小区是否高负荷，同时获取基础信息。"""
    sql = text(f"""
        SELECT is_highload, cell_name, dist_name, county_name, prod_name
        FROM {DB_SCHEMA_AGENT}.jd_cell_expansion_day
        WHERE cgi = :cgi AND stat_time = :stat_time
    """)
    result = await db.execute(sql, {"cgi": cgi, "stat_time": stat_time})
    row = result.mappings().first()

    if not row:
        return {"high_load_type": "否"}

    return {
        "high_load_type": row.get("is_highload") or "否",
        "cell_name": row.get("cell_name"),
        "dist_name": row.get("dist_name"),
        "county_name": row.get("county_name"),
        "prod_name": row.get("prod_name"),
    }


async def _query_param_check(db: AsyncSession, cgi: str, stat_time: str | None = None) -> dict[str, Any]:
    """调用参数核查工具获取参数合规情况。"""
    try:
        # 延迟导入避免循环引用
        from app.tools.energy_param_check_tool import query_energy_param_check  # noqa: PLC0415
        result = await query_energy_param_check(cgi=cgi, check_date=stat_time, db=db)
        return {
            "success": result.get("success", False),
            "is_compliant": result.get("is_compliant"),
            "total_count": result.get("total_count", 0),
            "unqualified_count": result.get("unqualified_count", 0),
            "report_content": result.get("report_content", ""),
            "unqualified_items": result.get("unqualified_items", []),
        }
    except Exception as exc:
        logger.error("参数核查调用异常: %s", exc, exc_info=True)
        return {
            "success": False,
            "is_compliant": None,
            "error": "参数核查暂时不可用，请稍后重试",
        }


def _parse_stat_time(stat_time: str | datetime | None) -> datetime | None:
    """将用户输入的日期转换为 datetime 对象。"""
    if not stat_time:
        return None
    if isinstance(stat_time, datetime):
        return stat_time
    try:
        if " " in stat_time:
            return datetime.strptime(stat_time, "%Y-%m-%d %H:%M:%S")
        return datetime.strptime(stat_time, "%Y-%m-%d")
    except ValueError:
        return None


def _format_sleep_duration(seconds: int | None) -> str:
    """将休眠时长（秒）格式化为可读字符串。"""
    if seconds is None:
        return "数据缺失"
    if seconds == 0:
        return "0秒"
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    parts = []
    if hours > 0:
        parts.append(f"{hours}小时")
    if minutes > 0:
        parts.append(f"{minutes}分钟")
    if secs > 0:
        parts.append(f"{secs}秒")
    return "".join(parts)


def _check_is_pre_sleep_hour(prb_hour: int | None, sleep_hour: str | None) -> bool:
    """兼容原工具内私有接口，规则实现位于纯业务模块。"""
    return is_pre_sleep_hour(prb_hour, sleep_hour)


def _count_high_pre_sleep_days(rows: list[dict[str, Any]]) -> int:
    """按日期去重统计真正休眠前一小时的高 PRB 候选行。"""
    high_dates = set()
    for row in rows:
        prb_rate = max(
            row.get("prb_rate_ul") or 0,
            row.get("prb_rate_dl") or 0,
        )
        if prb_rate <= PRB_HIGH_LOAD_THRESHOLD:
            continue
        if not _check_is_pre_sleep_hour(row.get("prb_hour"), row.get("sleep_hour")):
            continue
        stat_time = ensure_datetime(row.get("stat_time"))
        if stat_time:
            high_dates.add(stat_time.date())
    return len(high_dates)


async def _query_pre_sleep_load_data(
    cgi: str,
    stat_time: datetime,
) -> dict[str, Any]:
    """查询休眠生效前负荷偏高分析数据。

    数据来源：jd_cell_pre_hour_busy

    返回字段：
        - 时间、地市名称、厂家、基站名称、小区名称、CGI
        - 休眠类生效时长（秒）
        - 无线利用率（%）
        - 是否休眠前一小时
        - 7天内休眠前1小时PRB利用率大于50%的天数
    """
    # 计算时间范围（包含当天）
    start_date_7d = stat_time - timedelta(days=PRE_SLEEP_LOAD_DAYS - 1)

    # 并行查询：当日明细 + 7天统计（各自独立 session）
    detail_sql = text(f"""
        SELECT
            stat_time,
            dist_name,
            prod_name,
            gnb_name,
            cell_name,
            cgi,
            ee_shallowsleeptimerru,
            ee_deepsleeptimerru,
            ee_supersleeptimerru,
            prb_hour,
            prb_rate_ul,
            prb_rate_dl,
            sleep_hour
        FROM {DB_SCHEMA_AGENT}.jd_cell_pre_hour_busy
        WHERE cgi = :cgi
          AND stat_time = :stat_time
        ORDER BY stat_time, prb_hour
    """)

    stat_sql = text(f"""
        SELECT stat_time, prb_hour, sleep_hour, prb_rate_ul, prb_rate_dl
        FROM {DB_SCHEMA_AGENT}.jd_cell_pre_hour_busy
        WHERE cgi = :cgi
          AND stat_time >= :start_date
          AND stat_time <= :end_date
          AND (prb_rate_ul > :prb_threshold OR prb_rate_dl > :prb_threshold)
    """)

    detail_rows, stat_rows = await asyncio.gather(
        fetch_rows(detail_sql, {"cgi": cgi, "stat_time": stat_time}),
        fetch_rows(stat_sql, {
            "cgi": cgi,
            "start_date": start_date_7d,
            "end_date": stat_time,
            "prb_threshold": PRB_HIGH_LOAD_THRESHOLD,
        }),
    )
    high_prb_days = _count_high_pre_sleep_days(stat_rows)

    # 构建表格数据
    data: list[dict[str, Any]] = []
    for row in detail_rows:
        # 计算休眠类生效时长（浅层+深度+极致）
        total_sleep_seconds = (
            (row.get("ee_shallowsleeptimerru") or 0) +
            (row.get("ee_deepsleeptimerru") or 0) +
            (row.get("ee_supersleeptimerru") or 0)
        )

        # 计算无线利用率（取上下行较大值）
        prb_rate_ul = row.get("prb_rate_ul") or 0
        prb_rate_dl = row.get("prb_rate_dl") or 0
        prb_rate = max(prb_rate_ul, prb_rate_dl)

        # 判断是否休眠前一小时
        is_pre_sleep_hour = _check_is_pre_sleep_hour(
            row.get("prb_hour"),
            row.get("sleep_hour")
        )

        # 格式化时间：stat_time + prb_hour
        stat_time_val = ensure_datetime(row.get("stat_time"))
        prb_hour_val = row.get("prb_hour")
        if stat_time_val and prb_hour_val is not None:
            time_display = f"{stat_time_val.strftime('%Y-%m-%d')} {prb_hour_val:02d}:00:00"
        else:
            time_display = "-"

        data.append({
            "time": time_display,
            "dist_name": row.get("dist_name") or "-",
            "prod_name": row.get("prod_name") or "-",
            "gnb_name": row.get("gnb_name") or "-",
            "cell_name": row.get("cell_name") or "-",
            "cgi": row.get("cgi") or cgi,
            "sleep_seconds": total_sleep_seconds,
            "sleep_display": _format_sleep_duration(total_sleep_seconds),
            "prb_rate_ul": round(float(prb_rate_ul), 2),
            "prb_rate_dl": round(float(prb_rate_dl), 2),
            "prb_rate": round(prb_rate, 2),
            "is_pre_sleep_hour": "是" if is_pre_sleep_hour else "否",
            "high_prb_days_7d": high_prb_days,
        })

    # 生成表格
    table_rows = []
    for item in data:
        table_rows.append(
            f"| {item['time']} | {item['dist_name']} | {item['prod_name']} | "
            f"{item['gnb_name']} | {item['cell_name']} | {item['cgi']} | "
            f"{item['sleep_seconds']} | {item['prb_rate_ul']:.2f}% | "
            f"{item['prb_rate_dl']:.2f}% | {item['prb_rate']:.2f}% | "
            f"{item['is_pre_sleep_hour']} | {item['high_prb_days_7d']} |"
        )

    if not table_rows:
        table_rows.append(
            "| — | 休眠生效前无高负荷 | — | — | — | — | — | — | — | — | — | — |"
        )

    table_md = _build_md_table(
        headers=["时间", "地市名称", "厂家", "基站名称", "小区名称", "CGI",
                 "休眠类生效时长(秒)", "上行PRB利用率(%)", "下行PRB利用率(%)",
                 "无线利用率(%)", "是否休眠前一小时", f"7天内PRB>{PRB_HIGH_LOAD_THRESHOLD:.0f}%天数"],
        rows=table_rows,
    )

    # 获取基础信息（从第一条记录）
    first_row = detail_rows[0] if detail_rows else {}

    return {
        "data": data,
        "table": table_md,
        "high_prb_days_7d": high_prb_days,
        "cell_name": first_row.get("cell_name") or cgi,
        "dist_name": first_row.get("dist_name") or "-",
        "county_name": first_row.get("county_name") or "-",
        "prod_name": first_row.get("prod_name") or "-",
    }
