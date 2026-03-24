"""
节能参数核查工具模块。

查询小区节能参数核查结果，生成标准化 Markdown 报告。

表名规则:
- 统一表名: {schema}.eng_check_result_YYYYMMDD

日期规则:
- 用户未传 check_date 时:
  - 当前时间 < 15:00: 使用昨天日期
  - 当前时间 >= 15:00: 使用今天日期

核心原则: 纯 Python 查询与报告生成，零 LLM 依赖。
"""

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.logging import get_logger
from app.tools.registry import tool_registry

logger = get_logger("energy_param_check_tool")

# 获取数据库 schema（规则表所在 schema）
DB_SCHEMA_RULE = get_settings().db_schema_rule


def _get_check_date(user_date: str | None) -> str:
    """确定核查日期。"""
    if user_date:
        return user_date.replace("-", "")

    now = datetime.now()
    if now.hour < 15:
        check_date = (now - timedelta(days=1)).strftime("%Y%m%d")
    else:
        check_date = now.strftime("%Y%m%d")

    return check_date


def _get_table_name(date_suffix: str) -> str:
    """获取核查表名。"""
    return f"eng_check_result_{date_suffix}"


async def _fetch_param_check(
    db: AsyncSession,
    table_name: str,
    cgi: str | None = None,
    cell_name: str | None = None,
    dist_name: str | None = None,
    prod_name: str | None = None,
) -> list[dict[str, Any]]:
    """查询节能参数核查数据。

    查询优先级:
    1. cgi（最精确）
    2. cell_name
    3. dist_name + prod_name
    """
    conditions = ["1=1"]
    params: dict[str, Any] = {}

    if cgi:
        conditions.append("cgi = :cgi")
        params["cgi"] = cgi
    elif cell_name:
        conditions.append("cell_name = :cell_name")
        params["cell_name"] = cell_name
        if dist_name:
            conditions.append("dist_name = :dist_name")
            params["dist_name"] = dist_name
        if prod_name:
            conditions.append("prod_name = :prod_name")
            params["prod_name"] = prod_name
    else:
        if dist_name:
            conditions.append("dist_name = :dist_name")
            params["dist_name"] = dist_name
        if prod_name:
            conditions.append("prod_name = :prod_name")
            params["prod_name"] = prod_name

    where_clause = " AND ".join(conditions)

    sql = text(f"""
        SELECT
            check_time,
            prod_name,
            enodeb_name,
            cell_name,
            cgi,
            station_type,
            rru_aau,
            chn_num,
            freq_band,
            saving_para_name,
            field_name,
            objtype_name,
            powertype_name,
            saving_para_value,
            recommend_value,
            saving_switch_state,
            subjective_reason,
            objective_reason
        FROM {DB_SCHEMA_RULE}.{table_name}
        WHERE {where_clause}
    """)
    logger.info("SQL: %s", sql)
    try:
        result = await db.execute(sql, params)
        rows = result.mappings().all()
        return [dict(row) for row in rows]
    except ProgrammingError as exc:
        if "UndefinedTableError" in str(exc) or "不存在" in str(exc):
            logger.warning("核查表不存在: %s", table_name)
            return []
        raise


def _generate_report(query_label: str, data: list[dict[str, Any]], is_single_cgi: bool) -> str:
    """生成节能参数核查 Markdown 报告。

    展示逻辑：
    - 合规：仅提示合规数量，不展示详情表格
    - 不合规：用表格展示不合规参数详情
    """
    unqualified_items = [row for row in data if row.get("saving_switch_state") != "合格"]
    qualified_count = len(data) - len(unqualified_items)

    if is_single_cgi:
        # 模式 A：单小区 CGI 查询
        cgi = data[0].get("cgi", "-") if data else "-"
        base_info = data[0]
        enodeb = base_info.get("enodeb_name", "-")
        cell = base_info.get("cell_name", "-")
        vendor = base_info.get("prod_name", "-")
        station_type = base_info.get("station_type", "-")
        freq = base_info.get("freq_band", "-")

        report_md = f"### 小区({cgi})节能参数配置总体查询情况\n\n"
        report_md += f"**基础信息**：基站 `{enodeb}` | 小区 `{cell}` | 厂家 `{vendor}` | 类型 `{station_type}` | 频段 `{freq}`\n\n"

        if not unqualified_items:
            # 全部合规
            report_md += f"**核查结论：✅ 合规**\n\n"
            report_md += f"- 经核查，该小区共 {qualified_count} 项节能参数均符合规范要求，状态合格。\n"
        else:
            # 存在不合规项
            report_md += f"**核查结论：❌ 不合规**\n\n"
            report_md += f"- 共核查 {len(data)} 项参数，其中合格 {qualified_count} 项，**不合规 {len(unqualified_items)} 项**\n"

            # 不合规参数详情表格
            report_md += "\n#### 不合规参数明细\n"
            report_md += "| 参数中文名称 | 参数英文 | 节能技术 | 节能类型 | 现网配置值 | 指导意见值 | 核查结果 | 主观原因 | 客观原因 |\n"
            report_md += "|---|---|---|---|---|---|---|---|---|\n"

            for item in unqualified_items:
                c_name = item.get('saving_para_name', '-')
                e_name = item.get('field_name', '-')
                obj_type = item.get('objtype_name', '-')
                pwr_type = item.get('powertype_name', '-')
                cur_val = item.get('saving_para_value', '-')
                rec_val = item.get('recommend_value', '-')
                state = item.get('saving_switch_state', '-')
                sub_rsn = str(item.get('subjective_reason') or '-').replace('\n', ' ')
                obj_rsn = str(item.get('objective_reason') or '-').replace('\n', ' ')

                report_md += f"| {c_name} | {e_name} | {obj_type} | {pwr_type} | {cur_val} | {rec_val} | {state} | {sub_rsn} | {obj_rsn} |\n"

    else:
        # 模式 B：批量查询汇总
        report_md = f"### 节能参数核查汇总报告（{query_label}）\n\n"

        if not unqualified_items:
            # 全部合规
            report_md += f"**核查结论：✅ 合规**\n\n"
            report_md += f"- 经核查，共 {qualified_count} 项节能参数均符合规范要求，状态合格。\n"
        else:
            # 存在不合规项
            report_md += f"**核查结论：❌ 不合规**\n\n"
            report_md += f"- 共核查 {len(data)} 项参数，其中合格 {qualified_count} 项，**不合规 {len(unqualified_items)} 项**\n"

            # 不合规参数详情表格（含CGI列）
            report_md += "\n#### 不合规参数明细\n"
            report_md += "| CGI | 参数中文名称 | 参数英文 | 节能技术 | 节能类型 | 现网配置值 | 指导意见值 | 核查结果 | 主观原因 | 客观原因 |\n"
            report_md += "|---|---|---|---|---|---|---|---|---|---|\n"

            for item in unqualified_items:
                item_cgi = item.get('cgi', '-')
                c_name = item.get('saving_para_name', '-')
                e_name = item.get('field_name', '-')
                obj_type = item.get('objtype_name', '-')
                pwr_type = item.get('powertype_name', '-')
                cur_val = item.get('saving_para_value', '-')
                rec_val = item.get('recommend_value', '-')
                state = item.get('saving_switch_state', '-')
                sub_rsn = str(item.get('subjective_reason') or '-').replace('\n', ' ')
                obj_rsn = str(item.get('objective_reason') or '-').replace('\n', ' ')

                report_md += f"| {item_cgi} | {c_name} | {e_name} | {obj_type} | {pwr_type} | {cur_val} | {rec_val} | {state} | {sub_rsn} | {obj_rsn} |\n"

    return report_md


@tool_registry.tool(
    description="""查询小区节能参数核查结果。

查询指定日期的节能参数核查数据，分析不合规配置，生成标准化 Markdown 报告。

表名规则:
- 统一表名: {schema}.eng_check_result_YYYYMMDD

日期规则:
- 用户未传 check_date 时:
  - 当前时间 < 15:00: 使用昨天日期
  - 当前时间 >= 15:00: 使用今天日期

查询优先级（至少提供一种查询条件）:
1. 如果提供 cgi，直接用 cgi 查询（最精确，输出单小区报告）
2. 如果提供 cell_name，用 cell_name 查询
3. 否则需要 dist_name + prod_name 进行批量查询（输出汇总报告）

参数说明:
- check_date: 核查日期 (YYYY-MM-DD，未提及自动计算)
- cgi: CGI 过滤（可选，优先级最高，如 "460-00-12345-678"）
- cell_name: 小区名称过滤（可选，如 "长沙芙蓉区芙蓉广场-HHH-1"）
- dist_name: 地市名称（可选，当未提供 cgi/cell_name 时必填）
- prod_name: 设备厂家（可选，当未提供 cgi/cell_name 时必填）

触发条件: "节能参数核查"、"参数检查"、"配置核查"、"参数合规"。

返回:
- 包含 report_content 和结构化数据的字典
""",
    parameters={
        "type": "object",
        "properties": {
            "check_date": {
                "type": "string",
                "description": "核查日期 (YYYY-MM-DD)，未提及时根据当前时间自动计算（<15:00用昨天，>=15:00用今天）",
            },
            "cgi": {
                "type": "string",
                "description": "CGI 过滤（可选，优先级最高，如 460-00-12345-678）",
            },
            "cell_name": {
                "type": "string",
                "description": "小区名称过滤（可选，如 长沙芙蓉区芙蓉广场-HHH-1）",
            },
            "dist_name": {
                "type": "string",
                "description": "地市名称（可选，当未提供 cgi/cell_name 时必填）",
            },
            "prod_name": {
                "type": "string",
                "description": "设备厂家（可选，当未提供 cgi/cell_name 时必填）",
            },
        },
        "required": [],
    },
)
async def query_energy_param_check(
    db: AsyncSession,
    check_date: str | None = None,
    cgi: str | None = None,
    cell_name: str | None = None,
    dist_name: str | None = None,
    prod_name: str | None = None,
) -> dict[str, Any]:
    """查询小区节能参数核查结果。"""
    date_str = _get_check_date(check_date)
    table_name = _get_table_name(date_str)

    # 构建查询标签（用于日志和批量报告标题）
    query_parts = []
    if cgi:
        query_parts.append(f"cgi={cgi}")
    elif cell_name:
        query_parts.append(f"cell_name={cell_name}")
    else:
        if dist_name:
            query_parts.append(f"dist={dist_name}")
        if prod_name:
            query_parts.append(f"prod={prod_name}")
    query_label = ", ".join(query_parts) if query_parts else "全量查询"

    logger.info("节能参数核查: date=%s, %s, table=%s", date_str, query_label, table_name)

    try:
        data = await _fetch_param_check(db, table_name, cgi, cell_name, dist_name, prod_name)

        if not data:
            return {
                "success": True,
                "report_content": f"未查询到 {query_label} 在 {date_str} 的参数核查数据。",
            }

        unqualified_items = [row for row in data if row.get("saving_switch_state") != "合格"]
        is_single_cgi = bool(cgi)
        report_md = _generate_report(query_label, data, is_single_cgi)
        logger.info(
            "节能参数核查完成: date=%s, query=%s, total=%d, unqualified=%d",
            date_str, query_label, len(data), len(unqualified_items)
        )

        return {
            "success": True,
            "check_date": date_str,
            "check_date_display": f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}",
            "is_compliant": len(unqualified_items) == 0,
            "total_count": len(data),
            "unqualified_count": len(unqualified_items),
            "qualified_count": len(data) - len(unqualified_items),
            "report_content": report_md,
            "unqualified_items": unqualified_items,
        }

    except Exception as exc:
        logger.error("节能参数核查异常: %s", exc, exc_info=True)
        return {
            "success": False,
            "error": f"核查失败: {exc}",
        }
