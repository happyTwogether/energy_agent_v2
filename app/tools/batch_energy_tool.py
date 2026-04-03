"""
批量小区节电诊断与导出工具模块。

从 jd_cell_expansion_day 和 jd_cell_constriction_day 两个表查询数据，
合并结果生成 Excel 报告。
"""

import json
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.logging import get_logger
from app.tools.registry import tool_registry
from app.utils.export_util import export_to_excel

logger = get_logger("batch_energy_tool")

DB_SCHEMA_AGENT = get_settings().db_schema_agent


@tool_registry.tool(
    description="""按区县批量诊断5G小区节电情况，生成完整报告。

参数说明:
- dist_name: 地市名称（如"长沙市"），可选。
- county_name: 区县名称（如"芙蓉区"），必填。
- prod_name: 厂家名称（如"华为"、"中兴"），可选。
- stat_time: 统计日期 (YYYY-MM-DD)，可选，默认查询最新日期。

注意: 区县为必填项，若不提供将提示错误。

返回统计摘要 + Excel 下载链接。
""",
    parameters={
        "type": "object",
        "properties": {
            "dist_name": {
                "type": "string",
                "description": "地市名称，如'长沙市'",
            },
            "county_name": {
                "type": "string",
                "description": "区县名称，如'芙蓉区'（必填）",
            },
            "prod_name": {
                "type": "string",
                "description": "厂家名称，如'华为'、'中兴'",
            },
            "stat_time": {
                "type": "string",
                "description": "统计日期 (YYYY-MM-DD)，可选，默认最新日期",
            },
        },
        "required": [],
    },
)
async def analyze_batch_cells_energy(
    db: AsyncSession,
    dist_name: str | None = None,
    county_name: str | None = None,
    prod_name: str | None = None,
    stat_time: str | None = None,
) -> dict[str, Any]:
    """按区县批量诊断5G小区节电情况。"""
    logger.info("批量节电诊断: dist_name=%s, county_name=%s, prod_name=%s, stat_time=%s",
                dist_name, county_name, prod_name, stat_time)

    # ── Step 1: 参数校验（区县必填）──
    if not county_name:
        return {
            "success": False,
            "error": "请输入地市、区县、厂家进行批量核查。区县为必填项。",
        }

    # ── Step 2: 确定查询日期 ──
    if stat_time:
        if " " in stat_time:
            query_date = datetime.strptime(stat_time, "%Y-%m-%d %H:%M:%S")
        else:
            query_date = datetime.strptime(stat_time, "%Y-%m-%d")
    else:
        # 从 jd_cell_expansion_day 获取最新日期
        latest_sql = text(f"""
            SELECT MAX(stat_time) as max_date
            FROM {DB_SCHEMA_AGENT}.jd_cell_expansion_day
        """)
        latest_result = await db.execute(latest_sql)
        latest_row = latest_result.mappings().first()
        query_date = latest_row["max_date"] if latest_row else None

    if not query_date:
        return {"success": False, "error": "暂无节电分析数据。"}

    display_date = query_date.strftime("%Y-%m-%d")

    # ── Step 3: 构建查询条件 ──
    where_clauses = ["stat_time = :stat_time"]
    bind_params: dict[str, Any] = {"stat_time": query_date}

    if county_name:
        where_clauses.append("county_name = :county_name")
        bind_params["county_name"] = county_name
    if dist_name:
        where_clauses.append("dist_name = :dist_name")
        bind_params["dist_name"] = dist_name
    if prod_name:
        where_clauses.append("prod_name = :prod_name")
        bind_params["prod_name"] = prod_name

    where_sql = " AND ".join(where_clauses)

    # ── Step 4: 查询 jd_cell_expansion_day ──
    expansion_sql = text(f"""
        SELECT cgi, cell_name, gnb_name, dist_name, county_name, prod_name,
               is_highload, hour_filter, saving_switch_state,
               is_whitelist, reason, jd_type, starttime, endtime
        FROM {DB_SCHEMA_AGENT}.jd_cell_expansion_day
        WHERE {where_sql}
    """)
    expansion_result = await db.execute(expansion_sql, bind_params)
    expansion_rows = expansion_result.mappings().all()

    # 构建扩展数据映射
    all_cgis: set[str] = set()
    expansion_map: dict[str, dict] = {}

    for row in expansion_rows:
        cgi = row["cgi"]
        all_cgis.add(cgi)
        expansion_map[cgi] = dict(row)

    # ── Step 5: 查询 jd_cell_constriction_day ──
    constriction_sql = text(f"""
        SELECT cgi, constriction_hour
        FROM {DB_SCHEMA_AGENT}.jd_cell_constriction_day
        WHERE {where_sql}
    """)
    constriction_result = await db.execute(constriction_sql, bind_params)
    constriction_rows = constriction_result.mappings().all()

    # 构建收缩数据映射
    constriction_map: dict[str, list[int]] = {}
    for row in constriction_rows:
        cgi = row["cgi"]
        all_cgis.add(cgi)
        if cgi not in constriction_map:
            constriction_map[cgi] = []
        if row["constriction_hour"] is not None:
            constriction_map[cgi].append(row["constriction_hour"])

    # ── Step 6: 合并数据生成表格 ──
    if not all_cgis:
        query_desc = f"区县={county_name}"
        if dist_name:
            query_desc = f"地市={dist_name}, " + query_desc
        if prod_name:
            query_desc += f", 厂家={prod_name}"
        return {"success": False, "error": f"未查询到符合条件的数据（{query_desc}）。"}

    table_data = []
    stats = {
        "total": len(all_cgis),
        "high_load": 0,
        "can_expand": 0,
        "need_constriction": 0,
        "whitelist": 0,
    }

    for cgi in all_cgis:
        exp_info = expansion_map.get(cgi, {})
        const_hours = constriction_map.get(cgi, [])

        # 基础信息
        cell_name = exp_info.get("cell_name") or "-"
        gnb_name = exp_info.get("gnb_name") or "-"
        dist = exp_info.get("dist_name") or dist_name or "-"
        county = exp_info.get("county_name") or county_name or "-"
        prod = exp_info.get("prod_name") or prod_name or "-"

        # 节能扩展：容量与风险说明
        is_highload_raw = exp_info.get("is_highload")
        capacity_risk = "高负荷" if is_highload_raw == "是" else "非高负荷"
        if is_highload_raw == "是":
            stats["high_load"] += 1

        # 节能扩展：低业务时段与0休眠匹配表
        hour_filter_raw = exp_info.get("hour_filter")
        expandable_hours = _parse_hour_filter(hour_filter_raw)
        expandable_str = "、".join(map(str, expandable_hours)) if expandable_hours else "无"
        if expandable_hours:
            stats["can_expand"] += 1

        # 节能扩展：参数核查结论
        saving_switch_state = exp_info.get("saving_switch_state") or "无"

        # 节能收缩：周边200米关联小区高负荷受影响时间节点
        constriction_str = "、".join(map(str, const_hours)) if const_hours else "无"
        if const_hours:
            stats["need_constriction"] += 1

        # 特殊情况备注：节电白名单匹配情况
        is_whitelist_raw = exp_info.get("is_whitelist")
        if is_whitelist_raw == "是":
            stats["whitelist"] += 1
            reason = exp_info.get("reason") or "无"
            jd_type = exp_info.get("jd_type") or "无"
            starttime = exp_info.get("starttime")
            endtime = exp_info.get("endtime")
            starttime_str = starttime.strftime("%Y-%m-%d") if starttime else "无"
            endtime_str = endtime.strftime("%Y-%m-%d") if endtime else "无"
            whitelist_str = f"是白名单小区、白名单原因：{reason}，节电类型：{jd_type}，开始时间：{starttime_str}，结束时间：{endtime_str}"
        else:
            whitelist_str = "无"

        table_data.append({
            "地市名称": dist,
            "区县名称": county,
            "厂家": prod,
            "基站名称": gnb_name,
            "小区名称": cell_name,
            "CGI": cgi,
            "节能扩展：容量与风险说明": capacity_risk,
            "节能扩展：低业务时段与0休眠匹配表": expandable_str,
            "节能扩展：参数核查结论": saving_switch_state,
            "节能收缩：周边200米关联小区高负荷受影响时间节点": constriction_str,
            "特殊情况备注：节电白名单匹配情况": whitelist_str,
        })

    # ── Step 7: 生成 Excel ──
    download_url = export_to_excel(table_data, prefix="batch_analysis")

    # 构建查询描述
    query_parts = [f"区县={county_name}"]
    if dist_name:
        query_parts.insert(0, f"地市={dist_name}")
    if prod_name:
        query_parts.append(f"厂家={prod_name}")
    query_desc = " | ".join(query_parts)

    return {
        "success": True,
        "stat_time": display_date,
        "query_desc": query_desc,
        "stats": stats,
        "total_count": len(table_data),
        "download_url": download_url,
    }


def _parse_hour_filter(hour_filter_raw: Any) -> list[int]:
    """解析 hour_filter 字段。"""
    if not hour_filter_raw:
        return []
    if isinstance(hour_filter_raw, list):
        return hour_filter_raw
    if isinstance(hour_filter_raw, str):
        try:
            result = json.loads(hour_filter_raw)
            if isinstance(result, list):
                return result
        except json.JSONDecodeError:
            # 逗号分隔的字符串
            return [int(h.strip()) for h in hour_filter_raw.split(",") if h.strip().isdigit()]
    return []