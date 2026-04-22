"""
单小区节电分析工具模块。

数据来源：
- jd_cell_expansion_day: 高负荷状态、白名单信息、节电时间段
- jd_cell_detail_hour_nr: 小时级明细（流量、休眠时长），用于15天聚合分析
- jd_cell_constriction_day: 收缩分析数据
- jd_cell_around: 周边小区高负荷信息
"""

import json
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.logging import get_logger
from app.tools.registry import tool_registry

DB_SCHEMA_AGENT = get_settings().db_schema_agent

logger = get_logger("energy_saving_tool")

# 阈值配置
# 低业务阈值：上下行流量和（单位：MB），小于此值为低业务
LOW_TRAFFIC_THRESHOLD_MB: float = 500.0

# 周边小区距离阈值（单位：米）
AROUND_DISTANCE_THRESHOLD: float = 200.0

@tool_registry.tool(
    description="""分析单个5G小区的节电详情，包含休眠扩展、休眠收缩和负荷状态。

参数说明:
- cgi: 小区全球标识 (格式 460-00-xxx-xxx)，必填
- analysis_target: 分析目标类型:
  * "all"          — 输出完整报告（扩展+收缩+备注）
  * "expansion"    — 仅输出节能扩展分析
  * "constriction" — 仅输出节能收缩分析
  * "load"         — 仅输出高负荷状态
- stat_time: 统计日期 (YYYY-MM-DD)，可选，默认查询最新日期

核心原则: 返回结构化数据 + 基础表格，由 LLM 组装最终报告。
""",
    parameters={
        "type": "object",
        "properties": {
            "cgi": {
                "type": "string",
                "description": "小区全球标识，格式 460-00-基站号-小区号",
            },
            "analysis_target": {
                "type": "string",
                "enum": ["all", "expansion", "constriction", "load"],
                "description": "分析目标: all=全量报告, expansion=休眠扩展, constriction=休眠收缩, load=负荷状态",
            },
            "stat_time": {
                "type": "string",
                "description": "统计日期 (YYYY-MM-DD)，可选，默认查询最新日期",
            },
        },
        "required": ["cgi", "analysis_target"],
    },
)
async def analyze_single_cell_energy(
    cgi: str,
    analysis_target: str,
    db: AsyncSession,
    stat_time: str | None = None,
) -> dict[str, Any]:
    """分析单个5G小区的节电详情。

    返回结构化数据 + 基础表格，由 LLM 组装最终报告。
    """
    logger.info("单小区节电分析: cgi=%s, target=%s, stat_time=%s", cgi, analysis_target, stat_time)

    need_expansion = analysis_target in ("all", "expansion")
    need_constriction = analysis_target in ("all", "constriction")
    need_load = analysis_target in ("all", "load")

    # ── Step 1: 确定查询日期 ──
    if stat_time:
        query_date = _parse_stat_time(stat_time)
    else:
        # 查询最新日期
        latest_sql = text(f"""
            SELECT MAX(stat_time) as max_date
            FROM {DB_SCHEMA_AGENT}.jd_cell_detail_hour_nr
        """)
        latest_result = await db.execute(latest_sql)
        latest_row = latest_result.mappings().first()
        query_date = latest_row["max_date"] if latest_row else None

    if not query_date:
        return {"success": False, "error": "暂无节电分析数据。"}

    display_date = query_date.strftime("%Y-%m-%d")

    # 构建返回结果（基础信息从扩展/收缩结果中获取）
    result: dict[str, Any] = {
        "success": True,
        "cgi": cgi,
        "stat_time": display_date,
    }

    # ── Step 2: 扩展分析（查询 jd_cell_expansion_day）──
    if need_expansion:
        expansion_result = await _query_expansion_data(db, cgi, query_date)
        result["expansion_data"] = expansion_result["data"]
        result["expansion_table"] = expansion_result["table"]
        result["is_high_load"] = expansion_result["is_high_load"]
        result["param_check"] = expansion_result["param_check"]
        # 节电信息 & 白名单信息（统一从扩展表获取）
        result["jd_type"] = expansion_result.get("jd_type")
        result["jd_reason"] = expansion_result.get("reason")
        result["jd_starttime"] = expansion_result.get("starttime")
        result["jd_endtime"] = expansion_result.get("endtime")
        result["is_whitelist"] = expansion_result.get("is_whitelist", False)
        result["whitelist_reason"] = expansion_result.get("reason") or "无"
        # 基础信息
        result["cell_name"] = expansion_result.get("cell_name") or cgi
        result["dist_name"] = expansion_result.get("dist_name") or "-"
        result["county_name"] = expansion_result.get("county_name") or "-"
        result["prod_name"] = expansion_result.get("prod_name") or "-"

    # ── Step 3: 收缩分析（查询 jd_cell_constriction_day + jd_cell_around）──
    if need_constriction:
        constriction_result = await _query_constriction_data(db, cgi, query_date)
        result["constriction_data"] = constriction_result["data"]
        result["constriction_table"] = constriction_result["table"]
        # 基础信息（如果扩展分析没查，从收缩结果获取）
        if "cell_name" not in result:
            result["cell_name"] = constriction_result.get("cell_name") or cgi
            result["dist_name"] = constriction_result.get("dist_name") or "-"
            result["county_name"] = constriction_result.get("county_name") or "-"
            result["prod_name"] = constriction_result.get("prod_name") or "-"
            # 仅收缩时，白名单信息从收缩结果获取
            result["is_whitelist"] = constriction_result.get("is_whitelist", False)
            result["whitelist_reason"] = constriction_result.get("whitelist_reason", "无")

    # ── Step 4: 仅负荷状态 ──
    if need_load and not need_expansion:
        load_info = await _check_high_load_with_base_info(db, cgi, query_date)
        result["is_high_load"] = load_info.get("is_high_load", False)
        # 基础信息
        if "cell_name" not in result:
            result["cell_name"] = load_info.get("cell_name") or cgi
            result["dist_name"] = load_info.get("dist_name") or "-"
            result["county_name"] = load_info.get("county_name") or "-"
            result["prod_name"] = load_info.get("prod_name") or "-"

    # 确保基础信息有默认值
    result.setdefault("cell_name", cgi)
    result.setdefault("dist_name", "-")
    result.setdefault("county_name", "-")
    result.setdefault("prod_name", "-")
    result.setdefault("is_whitelist", False)
    result.setdefault("whitelist_reason", "无")

    return result

async def _query_expansion_data(
    db: AsyncSession,
    cgi: str,
    stat_time: datetime,
) -> dict[str, Any]:
    """查询扩展分析数据。

    数据来源：
    - jd_cell_expansion_day: 高负荷状态、白名单信息、节电时间段
    - jd_cell_detail_hour_nr: 15天聚合数据生成表格
    """
    from datetime import timedelta

    # ── 1. 从 jd_cell_expansion_day 获取高负荷状态和白名单信息 ──
    expansion_sql = text(f"""
        SELECT is_highload, jd_type, reason, starttime, endtime, is_whitelist,
               cell_name, dist_name, county_name, prod_name, gnb_name
        FROM {DB_SCHEMA_AGENT}.jd_cell_expansion_day
        WHERE cgi = :cgi AND stat_time = :stat_time
    """)
    expansion_result = await db.execute(expansion_sql, {"cgi": cgi, "stat_time": stat_time})
    expansion_row = expansion_result.mappings().first()

    is_high_load = False
    jd_type = None
    reason = None
    starttime = None
    endtime = None
    is_whitelist = False
    cell_name = None
    dist_name = None
    county_name = None
    prod_name = None
    gnb_name = None

    if expansion_row:
        is_highload_raw = expansion_row.get("is_highload")
        is_high_load = is_highload_raw == "是" if is_highload_raw else False
        jd_type = expansion_row.get("jd_type")
        reason = expansion_row.get("reason")
        starttime = expansion_row.get("starttime")
        endtime = expansion_row.get("endtime")
        is_whitelist = bool(expansion_row.get("is_whitelist"))
        cell_name = expansion_row.get("cell_name")
        dist_name = expansion_row.get("dist_name")
        county_name = expansion_row.get("county_name")
        prod_name = expansion_row.get("prod_name")
        gnb_name = expansion_row.get("gnb_name")

    # ── 2. 从 jd_cell_detail_hour_nr 获取15天聚合数据（仅夜间时段）──
    # 夜间时段：22:00-08:00
    night_hours = [0, 1, 2, 3, 4, 5, 6, 7, 8, 22, 23]

    start_date = stat_time - timedelta(days=14)
    detail_sql = text(f"""
        SELECT hours,
               AVG(pdcp_upoctdl) as avg_traffic_dl,
               AVG(pdcp_upoctul) as avg_traffic_ul,
               AVG(ee_shallowsleeptimerru + ee_deepsleeptimerru + ee_supersleeptimerru) as avg_sleep_sum,
               COUNT(*) as sample_count
        FROM {DB_SCHEMA_AGENT}.jd_cell_detail_hour_nr
        WHERE cgi = :cgi
          AND stat_time >= :start_date
          AND stat_time <= :end_date
          AND hours = ANY(:night_hours)
        GROUP BY hours
        ORDER BY hours
    """)
    detail_result = await db.execute(detail_sql, {
        "cgi": cgi, "start_date": start_date, "end_date": stat_time, "night_hours": night_hours
    })
    detail_rows = detail_result.mappings().all()

    expansion_data: list[dict[str, Any]] = []

    for row in detail_rows:
        hour = row["hours"]
        sample_count = row["sample_count"] or 0

        # 计算平均流量和（单位：GB → 转换为 MB）
        avg_traffic_dl_mb = (row["avg_traffic_dl"] or 0) * 1024
        avg_traffic_ul_mb = (row["avg_traffic_ul"] or 0) * 1024
        avg_traffic_total_mb = avg_traffic_dl_mb + avg_traffic_ul_mb

        # 判断是否低业务（基于15天平均值）
        is_low_business = avg_traffic_total_mb < LOW_TRAFFIC_THRESHOLD_MB

        # 计算平均休眠时长（秒）
        avg_sleep_sum = row["avg_sleep_sum"] or 0

        # 判断是否接近0休眠（平均值 < 60秒视为接近0）
        is_zero_sleep = avg_sleep_sum < 60

        # 扩展建议
        if is_low_business and is_zero_sleep:
            suggestion = "可扩展"
        elif not is_low_business:
            suggestion = "不建议（非低业务）"
        elif not is_zero_sleep:
            suggestion = "不建议（已有休眠）"
        else:
            suggestion = "不建议"

        expansion_data.append({
            "hour": hour,
            "sample_count": sample_count,
            "is_low_business": is_low_business,
            "avg_traffic_total_mb": round(avg_traffic_total_mb, 2),
            "avg_sleep_seconds": round(avg_sleep_sum, 0),
            "avg_sleep_display": _format_sleep_duration(int(avg_sleep_sum)),
            "is_zero_sleep": is_zero_sleep,
            "suggestion": suggestion,
        })

    # 生成表格
    table_rows = []
    for item in expansion_data:
        table_rows.append(
            f"| {item['hour']:02d}:00 | {'是' if item['is_low_business'] else '否'} | "
            f"{item['avg_sleep_display']} | {'是' if item['is_zero_sleep'] else '否'} | "
            f"{item['suggestion']} |"
        )

    table_md = (
        "| 时间点(时) | 是否低业务 | 休眠时长累加值(15天均值) | 是否接近0休眠 | 扩展建议 |\n"
        "|---|---|---|---|---|\n"
        + "\n".join(table_rows)
        if table_rows
        else "| — | 暂无数据 | — | — | — |"
    )

    # ── 参数核查 ──
    param_check_result = await _query_param_check(db, cgi, stat_time.strftime("%Y-%m-%d"))

    return {
        "data": expansion_data,
        "table": table_md,
        "is_high_load": is_high_load,
        "param_check": param_check_result,
        "jd_type": jd_type,
        "reason": reason,
        "starttime": starttime.strftime("%Y-%m-%d") if starttime else None,
        "endtime": endtime.strftime("%Y-%m-%d") if endtime else None,
        "is_whitelist": is_whitelist,
        "aggregation_days": 15,
        "start_date": start_date.strftime("%Y-%m-%d"),
        "end_date": stat_time.strftime("%Y-%m-%d"),
        # 基础信息
        "cell_name": cell_name,
        "dist_name": dist_name,
        "county_name": county_name,
        "prod_name": prod_name,
        "gnb_name": gnb_name,
    }


async def _query_constriction_data(
    db: AsyncSession,
    cgi: str,
    stat_time: datetime,
) -> dict[str, Any]:
    """查询收缩分析数据。

    从 jd_cell_constriction_day 获取收缩信息，
    关联 jd_cell_around 获取周边高负荷证据。
    """
    # 查询收缩数据（包含基础信息）
    # 字段说明：hours=节能收缩时间节点, around_cgi=影响源小区, around_cgi_cell_name=影响源小区名
    constriction_sql = text(f"""
        SELECT hours, reason, around_cgi, around_cgi_cell_name,
               is_whitelist, cell_name, dist_name, county_name, prod_name, gnb_name
        FROM {DB_SCHEMA_AGENT}.jd_cell_constriction_day
        WHERE cgi = :cgi AND stat_time = :stat_time
    """)
    constriction_result = await db.execute(constriction_sql, {"cgi": cgi, "stat_time": stat_time})
    constriction_rows = constriction_result.mappings().all()

    # 查询周边小区高负荷信息
    around_sql = text(f"""
        SELECT around_cgi, around_cgi_network_type, distance, is_high_load
        FROM {DB_SCHEMA_AGENT}.jd_cell_around
        WHERE cgi = :cgi AND distance <= :distance_threshold
    """)
    around_result = await db.execute(around_sql, {"cgi": cgi, "distance_threshold": AROUND_DISTANCE_THRESHOLD})
    around_rows = around_result.mappings().all()

    # 构建周边高负荷映射
    around_high_load_map = {
        row["around_cgi"]: {
            "network_type": row.get("around_cgi_network_type", "-"),
            "distance": row["distance"],
            "is_high_load": bool(row.get("is_high_load", 0)),
        }
        for row in around_rows
    }

    constriction_data: list[dict[str, Any]] = []
    is_whitelist = False
    whitelist_reason = "无"
    cell_name = None
    dist_name = None
    county_name = None
    prod_name = None
    gnb_name = None

    if constriction_rows:
        is_whitelist = bool(constriction_rows[0].get("is_whitelist"))
        # 基础信息从第一条记录获取
        cell_name = constriction_rows[0].get("cell_name")
        dist_name = constriction_rows[0].get("dist_name")
        county_name = constriction_rows[0].get("county_name")
        prod_name = constriction_rows[0].get("prod_name")
        gnb_name = constriction_rows[0].get("gnb_name")
        # 白名单原因可能在多条记录中，取第一条有值的
        for row in constriction_rows:
            if row.get("reason"):
                whitelist_reason = row["reason"]
                break

    for row in constriction_rows:
        hour = row["hours"]
        reason = row["reason"] or "—"
        related_cgi = row["around_cgi"] or "—"
        related_name = row["around_cgi_cell_name"] or "—"

        # 获取证据字段
        around_info = around_high_load_map.get(related_cgi, {})
        evidence = "—"
        if around_info:
            if around_info.get("is_high_load"):
                evidence = f"高负荷=是，距离{around_info.get('distance', 0):.0f}米"
            else:
                evidence = f"高负荷=否，距离{around_info.get('distance', 0):.0f}米"

        # 收缩建议
        if around_info.get("is_high_load"):
            suggestion = "剔除"
        else:
            suggestion = "观察"

        constriction_data.append({
            "constriction_hour": hour,
            "reason": reason,
            "related_cgi": related_cgi,
            "related_cell_name": related_name,
            "evidence": evidence,
            "suggestion": suggestion,
        })

    # 生成表格
    table_rows = []
    for item in constriction_data:
        table_rows.append(
            f"| {item['constriction_hour'] or '—'} | {item['reason']} | "
            f"{item['related_cell_name']}({item['related_cgi']}) | "
            f"{item['evidence']} | {item['suggestion']} |"
        )

    if not table_rows:
        table_rows.append("| — | 暂无收缩数据 | — | — | — |")

    table_md = (
        "| 原节能生效时间点 | 触发收缩原因 | 关联小区名称 | 证据字段 | 收缩建议 |\n"
        "|---|---|---|---|---|\n"
        + "\n".join(table_rows)
    )

    return {
        "data": constriction_data,
        "table": table_md,
        "is_whitelist": is_whitelist,
        "whitelist_reason": whitelist_reason,
        # 基础信息
        "cell_name": cell_name,
        "dist_name": dist_name,
        "county_name": county_name,
        "prod_name": prod_name,
        "gnb_name": gnb_name,
    }


async def _check_high_load_with_base_info(db: AsyncSession, cgi: str, stat_time: datetime) -> dict[str, Any]:
    """检查小区是否高负荷，同时获取基础信息（从 jd_cell_expansion_day 表查询）。"""
    sql = text(f"""
        SELECT is_highload, cell_name, dist_name, county_name, prod_name, gnb_name
        FROM {DB_SCHEMA_AGENT}.jd_cell_expansion_day
        WHERE cgi = :cgi AND stat_time = :stat_time
    """)
    result = await db.execute(sql, {"cgi": cgi, "stat_time": stat_time})
    row = result.mappings().first()

    if not row:
        return {"is_high_load": False}

    is_highload_raw = row.get("is_highload")
    return {
        "is_high_load": is_highload_raw == "是" if is_highload_raw else False,
        "cell_name": row.get("cell_name"),
        "dist_name": row.get("dist_name"),
        "county_name": row.get("county_name"),
        "prod_name": row.get("prod_name"),
        "gnb_name": row.get("gnb_name"),
    }


async def _query_param_check(db: AsyncSession, cgi: str, stat_time: str | None = None) -> dict[str, Any]:
    """调用参数核查工具获取参数合规情况。"""
    try:
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
        logger.warning("参数核查调用异常: %s", exc)
        return {
            "success": False,
            "is_compliant": True,  # 异常时默认合规，避免误导
            "report_content": "（参数核查暂时不可用）",
        }


def _parse_stat_time(stat_time: str | datetime | None) -> datetime | None:
    """将用户输入的日期转换为 datetime 对象。"""
    if not stat_time:
        return None
    if isinstance(stat_time, datetime):
        return stat_time
    if " " in stat_time:
        return datetime.strptime(stat_time, "%Y-%m-%d %H:%M:%S")
    return datetime.strptime(stat_time, "%Y-%m-%d")


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
