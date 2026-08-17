import asyncio
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.logging import get_logger
from app.services.cell_resolver import (
    LTE_PARAMETER_TABLE,
    NR_PARAMETER_TABLE,
    resolve_cell_identifier,
)
from app.services.database import get_session_factory
from app.services.energy_analysis import (
    NetworkType,
    build_expansion_record,
    enrich_constriction_records,
    normalize_network_type,
)
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


# pre_sleep_load 统计天数
PRE_SLEEP_LOAD_DAYS: int = 7
# PRB 利用率高负荷阈值（%）
PRB_HIGH_LOAD_THRESHOLD: float = 50.0
# 不合规项最大返回条数
MAX_UNQUALIFIED_ITEMS: int = 10


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

    latest_result = await db.execute(latest_sql)
    latest_row = latest_result.mappings().first()
    db_latest_date = ensure_datetime(latest_row["max_date"]) if latest_row else None

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
        try:
            name_resolution = await resolve_cell_identifier(
                db=db,
                cell_name=cell_name,
                network="5G",
            )
        except Exception as exc:
            logger.error("单小区名称解析异常: %s", exc, exc_info=True)
            return error_response("小区名称解析失败，请稍后重试。")
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
        tasks.append(_query_expansion_data(cgi, query_date))
        task_names.append("expansion")

    if need_constriction:
        tasks.append(_query_constriction_data(cgi, query_date))
        task_names.append("constriction")

    if need_pre_sleep_load:
        tasks.append(_query_pre_sleep_load_data(cgi, query_date))
        task_names.append("pre_sleep_load")

    # 参数核查也加入并行（扩展分析需要）
    if need_expansion:
        async def _query_param_check_with_session():
            async with get_session_factory()() as sess:
                return await _query_param_check(sess, cgi, display_date)
        tasks.append(_query_param_check_with_session())
        task_names.append("param_check")

    results = await asyncio.gather(*tasks) if tasks else []
    results_map = dict(zip(task_names, results))

    # 处理扩展分析结果
    if "expansion" in results_map:
        expansion_result = results_map["expansion"]
        result["expansion_table"] = expansion_result["table"]
        expansion_data, expansion_meta = truncate_and_export(
            expansion_result["data"],
            prefix="single_cell_expansion",
        )
        result["expansion_data"] = expansion_data
        for key, value in expansion_meta.items():
            result[f"expansion_{key}"] = value
        result["high_load_type"] = expansion_result.get("high_load_type", "否")
        # 参数核查结果（仅保留关键字段）
        param_check_raw = results_map.get("param_check", {"is_compliant": True})
        result["param_check"] = {
            "is_compliant": param_check_raw.get("is_compliant", True),
            "unqualified_count": param_check_raw.get("unqualified_count", 0),
        }
        # 如果不合规，保留不合规项（用于表格输出）
        if not param_check_raw.get("is_compliant", True):
            result["param_check"]["unqualified_items"] = param_check_raw.get("unqualified_items", [])[:MAX_UNQUALIFIED_ITEMS]
        # 节电信息 & 白名单信息（统一从扩展表获取）
        result["jd_type"] = expansion_result.get("jd_type")
        result["jd_reason"] = expansion_result.get("reason")
        result["jd_starttime"] = expansion_result.get("starttime")
        result["jd_endtime"] = expansion_result.get("endtime")
        result["is_whitelist"] = expansion_result.get("is_whitelist", False)
        result["whitelist_reason"] = expansion_result.get("reason") or "无"
        # 基础信息
        _fill_base_info(result, expansion_result, cgi)

    # 处理收缩分析结果
    if "constriction" in results_map:
        constriction_result = results_map["constriction"]
        result["constriction_table"] = constriction_result["table"]
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
            # 仅收缩时，白名单信息从收缩结果获取
            result["is_whitelist"] = constriction_result.get("is_whitelist", False)
            result["whitelist_reason"] = constriction_result.get("whitelist_reason", "无")

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
    result.setdefault("is_whitelist", False)
    result.setdefault("whitelist_reason", "无")
    result.setdefault("high_load_type", "否")

    return result

async def _query_expansion_data(
    cgi: str,
    stat_time: datetime,
) -> dict[str, Any]:
    """直接读取数据库预计算的 V1.4 扩展结果。"""
    expansion_sql = text(f"""
        SELECT cgi, stat_time, is_highload, jd_type, reason, starttime, endtime,
               is_whitelist, cell_name, dist_name, county_name, prod_name,
               hour_detail, hour_filter, hour_int,
               hour_filter_early, hour_int_early,
               deploy_hours, deploy_hours_continuous,
               deploy_hours_early, deploy_hours_continuous_early
        FROM {DB_SCHEMA_AGENT}.jd_cell_expansion_day
        WHERE cgi = :cgi AND stat_time = :stat_time
    """)
    rows = await fetch_rows(expansion_sql, {"cgi": cgi, "stat_time": stat_time})
    expansion_data = [build_expansion_record(row) for row in rows]
    base_row = expansion_data[0] if expansion_data else {}

    table_rows = [
        "| "
        f"{row.get('hour_filter') or '—'} | {row.get('hour_int') or 0} | "
        f"{row.get('deploy_hours_continuous') or '—'} | "
        f"{row.get('hour_filter_early') or '—'} | "
        f"{row.get('hour_int_early') or 0} | "
        f"{row.get('deploy_hours_continuous_early') or '—'} |"
        for row in expansion_data
    ]
    table_md = _build_md_table(
        headers=[
            "可扩展时段(含扩展)",
            "数量",
            "连续部署时段(含扩展)",
            "可扩展时段(常规)",
            "常规数量",
            "连续部署时段(常规)",
        ],
        rows=table_rows,
    )

    starttime = ensure_datetime(base_row.get("starttime"))
    endtime = ensure_datetime(base_row.get("endtime"))

    return {
        "data": expansion_data,
        "table": table_md,
        "high_load_type": base_row.get("is_highload") or "否",
        "jd_type": base_row.get("jd_type"),
        "reason": base_row.get("reason"),
        "starttime": starttime.strftime("%Y-%m-%d") if starttime else None,
        "endtime": endtime.strftime("%Y-%m-%d") if endtime else None,
        "is_whitelist": bool(base_row.get("is_whitelist")),
        # 基础信息
        "cell_name": base_row.get("cell_name"),
        "dist_name": base_row.get("dist_name"),
        "county_name": base_row.get("county_name"),
        "prod_name": base_row.get("prod_name"),
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
               is_whitelist, cell_name, dist_name, county_name, prod_name
        FROM {DB_SCHEMA_AGENT}.jd_cell_constriction_day
        WHERE cgi = :cgi AND stat_time = :stat_time
    """)
    constriction_rows = await fetch_rows(
        constriction_sql,
        {"cgi": cgi, "stat_time": stat_time},
    )
    around_cgis = {
        str(row["around_cgi"])
        for row in constriction_rows
        if row.get("around_cgi")
    }
    relation_by_pair = await _query_neighbor_relations(cgi, around_cgis)
    nr_cgis, lte_cgis = _group_site_type_queries(
        cgi,
        constriction_rows,
        relation_by_pair,
    )
    site_type_by_cell = await _query_site_types(nr_cgis, lte_cgis)
    constriction_data = enrich_constriction_records(
        constriction_rows,
        relation_by_pair,
        site_type_by_cell,
    )

    table_rows = [_build_constriction_table_row(row) for row in constriction_data]
    table_md = ""
    if table_rows:
        table_md = _build_md_table(
            headers=[
                "休眠前时间",
                "关联小区",
                "主小区休眠前PRB",
                "主小区休眠时长(秒)",
                "邻区休眠前/期间PRB",
                "抬升量",
                "距离及站型",
                "关系状态",
            ],
            rows=table_rows,
        )

    first_row = constriction_rows[0] if constriction_rows else {}
    whitelist_reason = next(
        (row["reason"] for row in constriction_rows if row.get("reason")),
        "无",
    )

    return {
        "data": constriction_data,
        "table": table_md,
        "is_whitelist": bool(first_row.get("is_whitelist")),
        "whitelist_reason": whitelist_reason,
        # 基础信息
        "cell_name": first_row.get("cell_name"),
        "dist_name": first_row.get("dist_name"),
        "county_name": first_row.get("county_name"),
        "prod_name": first_row.get("prod_name"),
    }


async def _query_neighbor_relations(
    cgi: str,
    around_cgis: set[str],
) -> dict[tuple[str, str], dict[str, Any]]:
    if not around_cgis:
        return {}
    sql = text(f"""
        SELECT cgi, around_cgi, around_cgi_network_type, distance
        FROM {DB_SCHEMA_AGENT}.jd_cell_around
        WHERE cgi = :cgi AND around_cgi = ANY(:around_cgis)
    """)
    rows = await fetch_rows(sql, {"cgi": cgi, "around_cgis": list(around_cgis)})
    return {(str(row["cgi"]), str(row["around_cgi"])): row for row in rows}


def _group_site_type_queries(
    main_cgi: str,
    rows: list[dict[str, Any]],
    relation_by_pair: dict[tuple[str, str], dict[str, Any]],
) -> tuple[set[str], set[str]]:
    nr_cgis = (
        {main_cgi}
        if any(not row.get("site_type") for row in rows)
        else set()
    )
    lte_cgis: set[str] = set()
    for row in rows:
        around_cgi = str(row.get("around_cgi") or "")
        relation = relation_by_pair.get((main_cgi, around_cgi), {})
        network = normalize_network_type(
            row.get("around_cgi_network_type")
            or relation.get("around_cgi_network_type"),
        )
        if network == "5G" and around_cgi:
            nr_cgis.add(around_cgi)
        elif network == "4G" and around_cgi:
            lte_cgis.add(around_cgi)
    return nr_cgis, lte_cgis


async def _query_site_types(
    nr_cgis: set[str],
    lte_cgis: set[str],
) -> dict[tuple[NetworkType, str], str]:
    tasks = []
    networks: list[NetworkType] = []
    for network, table, cgis in (
        ("5G", NR_PARAMETER_TABLE, nr_cgis),
        ("4G", LTE_PARAMETER_TABLE, lte_cgis),
    ):
        if not cgis:
            continue
        sql = text(f"""
            SELECT cgi, site_type
            FROM {table}
            WHERE cgi = ANY(:cgis)
              AND data_date = (SELECT MAX(data_date) FROM {table})
        """)
        tasks.append(fetch_rows(sql, {"cgis": list(cgis)}))
        networks.append(network)

    grouped_rows = await asyncio.gather(*tasks) if tasks else []
    result: dict[tuple[NetworkType, str], str] = {}
    for network, rows in zip(networks, grouped_rows):
        for row in rows:
            if row.get("cgi") and row.get("site_type"):
                result[(network, str(row["cgi"]))] = str(row["site_type"])
    return result


def _format_prb(ul: Any, dl: Any) -> str:
    ul_display = "—" if ul is None else f"{ul}%"
    dl_display = "—" if dl is None else f"{dl}%"
    return f"上行{ul_display}/下行{dl_display}"


def _build_constriction_table_row(row: dict[str, Any]) -> str:
    before_date = row.get("before_sleep_date") or "—"
    before_hour = row.get("before_sleep_hour")
    before_time = (
        f"{before_date} {before_hour}:00"
        if before_hour is not None
        else str(before_date)
    )
    related_cgi = row.get("around_cgi") or "—"
    related_name = row.get("around_cgi_cell_name") or "—"
    distance = row.get("distance")
    relation = (
        "关系信息缺失"
        if row.get("relation_status") == "missing"
        else row.get("relation_status")
    )
    distance_display = "—" if distance is None else f"{distance}米"
    main_prb = _format_prb(
        row.get("self_prb_rate_ul_before"),
        row.get("self_prb_rate_dl_before"),
    )
    around_prb_before = _format_prb(
        row.get("prb_rate_ul_before"),
        row.get("prb_rate_dl_before"),
    )
    around_prb = _format_prb(row.get("prb_rate_ul"), row.get("prb_rate_dl"))
    sleep_duration = row.get("self_sleep_duration")
    sleep_display = "—" if sleep_duration is None else sleep_duration
    prb_increase = row.get("prb_increase")
    increase_display = "—" if prb_increase is None else prb_increase
    site_types = (
        f"{row.get('main_site_type') or '—'}→"
        f"{row.get('around_site_type') or '—'}"
    )
    return (
        f"| {before_time} | {related_name}({related_cgi}) | "
        f"{main_prb} | {sleep_display} | "
        f"{around_prb_before} / {around_prb} | {increase_display} | "
        f"{distance_display}，{site_types} | {relation} |"
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
            "is_compliant": result.get("is_compliant", True),
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
    """判断是否为休眠前一小时。

    Args:
        prb_hour: 上一小时时间点（整数，如 22 表示 22:00-23:00）
        sleep_hour: 休眠时间点（字符串，可能包含多个时间点，如 "22,23,0"）

    Returns:
        如果 prb_hour 是休眠时间点的前一小时，返回 True。

    判断逻辑：
        如果 (prb_hour + 1) 在 sleep_hour 列表中，则为休眠前一小时。
        例如：prb_hour=21，sleep_hour="22,23,0"，则 21+1=22 在列表中，返回 True。
    """
    if prb_hour is None or not sleep_hour:
        return False

    try:
        # 解析 sleep_hour（可能是逗号分隔的多个时间点）
        sleep_hours = [int(h.strip()) for h in sleep_hour.split(",") if h.strip()]
        # 休眠前一小时 = 休眠时间点 - 1（处理跨天情况）
        pre_sleep_hours = [(h - 1) % 24 for h in sleep_hours]
        return prb_hour in pre_sleep_hours
    except (ValueError, AttributeError):
        return False


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
            f"{item['sleep_seconds']} | {item['prb_rate']:.2f}% | "
            f"{item['is_pre_sleep_hour']} | {item['high_prb_days_7d']} |"
        )

    if not table_rows:
        table_rows.append("| — | 休眠生效前无高负荷 | — | — | — | — | — | — | — | — |")

    table_md = _build_md_table(
        headers=["时间", "地市名称", "厂家", "基站名称", "小区名称", "CGI",
                 "休眠类生效时长(秒)", f"无线利用率(%)", "是否休眠前一小时", f"7天内PRB>{PRB_HIGH_LOAD_THRESHOLD:.0f}%天数"],
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
