"""
能耗指标查询工具模块。

提供 4G/5G 能耗指标的预定义模板查询和基于 LLM 的自由 SQL 查询双引擎。
"""

from typing import Any

FILTER_FIELD_ALIASES: dict[str, dict[str, str]] = {
    "collect": {
        "dist_name": "dist_name",
        "prod_name": "prod_name",
        "freq_band": "freq_band",
        "site_type": "site_type",
        "area": "area",
        "cgi": "cgi",
    },
    "detail": {
        "dist_name": "dist_name",
        "prod_name": "prod_name",
        "freq_band": "freq_band",
        "site_type": "site_type",
        "area": "area",
        "cgi": "cgi",
    },
}

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings, MAX_RETURN_ITEMS
from app.core.logging import get_logger
from app.prompts.sql_generation import SQL_GENERATION_PROMPT
from app.services.llm_client import get_llm_client, LLMError
from app.tools.registry import tool_registry
from app.utils.export_util import export_to_excel

logger = get_logger("metric_query_tool")

# 获取数据库 schema，用于构造带 schema 前缀的表名
DB_SCHEMA = get_settings().db_schema

# 表名配置 - 使用环境变量注入的 schema
LTE_TABLE = f"{DB_SCHEMA}.lte_report_day_collect"
NR_TABLE = f"{DB_SCHEMA}.nr_report_day_collect"

SQL_TEMPLATES: dict[str, dict] = {
    "lte_summary": {
        "description": "4G汇总指标查询（规模、能耗、能效、节电量）",
        "sql": """
            SELECT data_date, dist_name, prod_name, freq_band,
                   bbu_total, logic_station_total, logic_read_station_total,
                   all_cell_total, eightm_channel_total,
                   lte_station_power, lte_station_split_power,
                   nb_station_split_power, gms_station_split_power,
                   bbu_power, rru_power,
                   avg_energy_efficiency, low_energy_total, low_energy_ratio,
                   upoctul_dl, lte_curmonthpower, lte_curmonthpower_rate,
                   single_station_power, commode_station_total
            FROM {schema}.lte_report_day_collect
            WHERE 1=1 {conditions}
            ORDER BY data_date DESC LIMIT 1000
        """
    },
    "nr_summary": {
        "description": "5G汇总指标查询（规模、能耗、能效、节电量）",
        "sql": """
            SELECT data_date, dist_name, prod_name, freq_band,
                   sa_bbu_total, logic_station_total, logic_read_station_total,
                   all_cell_total, thirtytwo_channel_total, sixtyfour_channel_total,
                   nr_sa_station_power, nr_sa_station_split_power,
                   sa_bbu_power, rru_power,
                   sa_avg_energy_efficiency, low_energy_total, low_energy_ratio,
                   upoctul_dl, nr_curmonthpower, nr_curmonthpower_rate,
                   single_station_power, commode_station_total
            FROM {schema}.nr_report_day_collect
            WHERE 1=1 {conditions}
            ORDER BY data_date DESC LIMIT 1000
        """
    },
    "lte_energy_saving": {
        "description": "4G节电功能生效情况查询（符号关断/通道关断/载波关断/深度休眠）",
        "sql": """
            SELECT data_date, dist_name, prod_name, all_cell_total,
                   symdown_effect_total, symdown_effect_ratio, symdown_effect_hour,
                   open_symdown_total, open_symdown_rate,
                   chandown_effect_total, chandown_effect_ratio, chandown_effect_hour,
                   open_chandown_total, open_chandown_rate,
                   carrdown_effect_total, carrdown_effect_ratio, carrdown_effect_hour,
                   open_carrdown_total, open_carrdown_rate,
                   deepsleep_effect_total, deepsleep_effect_ratio, deepsleep_effect_hour,
                   open_deepsleep_total, open_deepsleep_rate
            FROM {schema}.lte_report_day_collect
            WHERE 1=1 {conditions}
            ORDER BY data_date DESC LIMIT 1000
        """
    },
    "nr_energy_saving": {
        "description": "5G节电功能生效情况查询（亚帧静默/通道静默/浅层休眠/深度休眠/极致休眠）",
        "sql": """
            SELECT data_date, dist_name, prod_name, all_cell_total,
                   symdown_effect_total, symdown_effect_ratio, symdown_effect_hour,
                   open_symdown_total, open_symdown_rate,
                   chandown_effect_total, chandown_effect_ratio, chandown_effect_hour,
                   open_chandown_total, open_chandown_rate,
                   carrdown_effect_total, carrdown_effect_ratio, carrdown_effect_hour,
                   open_carrdown_total, open_carrdown_rate,
                   deepsleep_effect_total, deepsleep_effect_ratio, deepsleep_effect_hour,
                   open_deepsleep_total, open_deepsleep_rate,
                   aaurru_supersleep_effect_total, aaurru_supersleep_effect_ratio,
                   aaurru_supersleep_effect_hour,
                   open_supersleep_total, open_supersleep_rate
            FROM {schema}.nr_report_day_collect
            WHERE 1=1 {conditions}
            ORDER BY data_date DESC LIMIT 1000
        """
    },
    "lte_cell_detail": {
        "description": "4G小区级节电明细查询",
        "sql": """
            SELECT data_date, dist_name, prod_name, cgi, chn_num, enbid,
                   carrier_shutdown_hour, channel_shutdown_hour,
                   symbol_shutdown_hour, deepsleep_hour,
                   upoctul_dl, is_low_energy, is_common_mode_station,
                   is_carrier_shutdown_switch, is_channel_shutdown_switch,
                   is_symbol_shutdown_switch, is_deepsleep_switch
            FROM {schema}.lte_report_day_detail
            WHERE 1=1 {conditions}
            ORDER BY data_date DESC LIMIT 1000
        """
    },
    "nr_cell_detail": {
        "description": "5G小区级节电明细查询",
        "sql": """
            SELECT data_date, dist_name, prod_name, cgi, chn_num, gnbid,
                   carrier_shutdown_hour, channel_shutdown_hour,
                   symbol_shutdown_hour, deepsleep_hour, supersleep_hour,
                   upoctul_dl, is_low_energy, is_common_mode_station,
                   is_carrier_shutdown_switch, is_channel_shutdown_switch,
                   is_symbol_shutdown_switch, is_deepsleep_switch,
                   is_nr_supersleep_switch
            FROM {schema}.nr_report_day_detail
            WHERE 1=1 {conditions}
            ORDER BY data_date DESC LIMIT 1000
        """
    }
}

def _build_conditions(
    dist_name: str | None = None,
    prod_name: str | None = None,
    freq_band: str | None = None,
    site_type: str | None = None,
    area: str | None = None,
    date_start: str | None = None,
    date_end: str | None = None,
    cgi: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """根据过滤参数构建安全条件字符串和参数字典。

    Args:
        dist_name: 地市名称。
        prod_name: 设备厂家。
        freq_band: 频段。
        site_type: 站型。
        area: 区域。
        date_start: 开始日期 (YYYY-MM-DD)。
        date_end: 结束日期 (YYYY-MM-DD)。
        cgi: 小区全局标识。

    Returns:
        (条件字符串, 参数字典) 元组。
    """
    conditions: list[str] = []
    params: dict[str, Any] = {}

    if dist_name:
        conditions.append("dist_name = :dist_name")
        params["dist_name"] = dist_name

    if prod_name:
        conditions.append("prod_name = :prod_name")
        params["prod_name"] = prod_name

    if freq_band:
        conditions.append("freq_band = :freq_band")
        params["freq_band"] = freq_band

    if site_type:
        conditions.append("site_type = :site_type")
        params["site_type"] = site_type

    if area:
        conditions.append("area = :area")
        params["area"] = area

    if date_start:
        conditions.append("data_date >= :date_start")
        params["date_start"] = date_start

    if date_end:
        conditions.append("data_date <= :date_end")
        params["date_end"] = date_end

    if cgi:
        conditions.append("cgi = :cgi")
        params["cgi"] = cgi

    # 使用 AND 连接所有条件
    condition_sql = " AND ".join(conditions) if conditions else ""
    if condition_sql:
        condition_sql = "AND " + condition_sql

    return condition_sql, params


def _extract_sql_from_llm_response(response_text: str) -> str:
    """清理 LLM 返回的 Markdown 代码块。

    Args:
        response_text: LLM 返回的原始文本。

    Returns:
        清理后的 SQL 字符串。
    """
    sql = response_text.strip()

    if sql.startswith("```sql"):
        sql = sql[6:]
    elif sql.startswith("```"):
        sql = sql[3:]

    if sql.endswith("```"):
        sql = sql[:-3]

    return sql.strip()


def _is_safe_sql(sql: str) -> bool:
    """检查 SQL 是否只包含 SELECT 语句。

    Args:
        sql: 要检查的 SQL 字符串。

    Returns:
        如果是安全的 SELECT 语句返回 True，否则返回 False。
    """
    upper_sql = sql.upper().strip()

    dangerous_keywords = [
        "INSERT",
        "UPDATE",
        "DELETE",
        "DROP",
        "TRUNCATE",
        "ALTER",
        "CREATE",
        "EXEC",
        "EXECUTE",
        "UNION",
    ]

    for keyword in dangerous_keywords:
        if keyword in upper_sql:
            return False

    if not upper_sql.startswith("SELECT"):
        return False

    return True


def _build_llm_prompt(
    metric_desc: str,
    dist_name: str | None = None,
    prod_name: str | None = None,
    freq_band: str | None = None,
    site_type: str | None = None,
    area: str | None = None,
    date_start: str | None = None,
    date_end: str | None = None,
) -> str:
    """构建 LLM 提示词。

    Args:
        metric_desc: 用户描述的指标查询需求。
        dist_name: 地市名称。
        prod_name: 设备厂家。
        freq_band: 频段。
        site_type: 站型。
        area: 区域。
        date_start: 开始日期。
        date_end: 结束日期。

    Returns:
        完整的提示词字符串。
    """
    conditions = []
    if dist_name:
        conditions.append(f"地市: {dist_name}")
    if prod_name:
        conditions.append(f"厂家: {prod_name}")
    if freq_band:
        conditions.append(f"频段: {freq_band}")
    if site_type:
        conditions.append(f"站型: {site_type}")
    if area:
        conditions.append(f"区域: {area}")
    if date_start:
        conditions.append(f"开始日期: {date_start}")
    if date_end:
        conditions.append(f"结束日期: {date_end}")

    condition_str = "\n".join(conditions) if conditions else "无额外过滤条件"

    return SQL_GENERATION_PROMPT.format(
        schema=DB_SCHEMA,
        metric_desc=metric_desc,
        condition_str=condition_str,
    )


async def _execute_single_query(
    db: AsyncSession,
    sql: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """执行单条 SQL 查询。

    Args:
        db: 数据库会话。
        sql: SQL 语句。
        params: 查询参数。

    Returns:
        查询结果字典。
    """
    try:
        result = await db.execute(text(sql), params or {})
        rows = result.mappings().all()
        data = [dict(row) for row in rows]

        return {
            "success": True,
            "row_count": len(data),
            "data": data,
        }
    except Exception as exc:
        logger.error("SQL 执行异常: %s", exc, exc_info=True)
        return {
            "success": False,
            "error": f"SQL 执行失败: {exc}",
        }


@tool_registry.tool(
    description="""查询 4G/5G 能耗指标数据，支持预定义模板和自由 SQL 探索。

模板: lte_summary(4G汇总) / nr_summary(5G汇总) / lte_energy_saving(4G节电) / nr_energy_saving(5G节电) / lte_cell_detail(4G小区明细) / nr_cell_detail(5G小区明细) / freeform(自由探索)
含特定CGI、对比、TOP排序时用 freeform，填写 metric_desc。
""",
    parameters={
        "type": "object",
        "properties": {
            "template_key": {
                "type": "string",
                "enum": [
                    "lte_summary",
                    "nr_summary",
                    "lte_energy_saving",
                    "nr_energy_saving",
                    "lte_cell_detail",
                    "nr_cell_detail",
                    "freeform",
                ],
                "description": "查询模板标识",
            },
            "dist_name": {
                "type": "string",
                "description": "地市名称，如长沙市",
            },
            "prod_name": {
                "type": "string",
                "description": "设备厂家，如华为",
            },
            "freq_band": {
                "type": "string",
                "description": "频段",
            },
            "site_type": {
                "type": "string",
                "description": "站型",
            },
            "area": {
                "type": "string",
                "description": "区域",
            },
            "date_start": {
                "type": "string",
                "description": "开始日期 (YYYY-MM-DD)",
            },
            "date_end": {
                "type": "string",
                "description": "结束日期 (YYYY-MM-DD)",
            },
            "cgi": {
                "type": "string",
                "description": "小区全局标识，格式 460-00-基站号-小区号",
            },
            "metric_desc": {
                "type": "string",
                "description": "自由探索模式下的查询需求描述",
            },
            "export_excel": {
                "type": "boolean",
                "description": "是否导出 Excel",
                "default": False,
            },
        },
        "required": ["template_key"],
    },
)
async def query_metric(
    template_key: str,
    db: AsyncSession,
    dist_name: str | None = None,
    prod_name: str | None = None,
    freq_band: str | None = None,
    site_type: str | None = None,
    area: str | None = None,
    date_start: str | None = None,
    date_end: str | None = None,
    cgi: str | None = None,
    metric_desc: str | None = None,
    export_excel: bool = False,
) -> dict[str, Any]:
    """查询 4G/5G 能耗指标。

    支持预定义模板查询和自由 SQL 探索两种模式。

    Args:
        template_key: 模板标识，决定查询模式。
        db: 数据库会话。
        dist_name: 地市名称过滤。
        prod_name: 设备厂家过滤。
        freq_band: 频段过滤。
        site_type: 站型过滤。
        area: 区域过滤。
        date_start: 开始日期。
        date_end: 结束日期。
        cgi: 小区标识过滤。
        metric_desc: 自由探索模式的查询描述。

    Returns:
        包含查询结果的字典。
    """
    # 默认值处理
    dist_name = dist_name or "全网"
    prod_name = prod_name or "全网"
    freq_band = freq_band or "全网"
    site_type = site_type or "全网"
    area = area or "全网"

    logger.info(
        "指标查询: template_key=%s, dist_name=%s, prod_name=%s, freq_band=%s, site_type=%s, area=%s, date_range=%s~%s",
        template_key,
        dist_name,
        prod_name,
        freq_band,
        site_type,
        area,
        date_start,
        date_end,
    )

    try:
        if template_key == "freeform":
            if not metric_desc:
                return {
                    "success": False,
                    "error": "自由探索模式必须提供 metric_desc 参数",
                }

            prompt = _build_llm_prompt(
                metric_desc=metric_desc,
                dist_name=dist_name,
                prod_name=prod_name,
                freq_band=freq_band,
                site_type=site_type,
                area=area,
                date_start=date_start,
                date_end=date_end,
            )

            messages = [
                {"role": "system", "content": "你是一个专业的 SQL 查询生成助手。"},
                {"role": "user", "content": prompt},
            ]

            try:
                llm_response = await get_llm_client().chat(messages=messages)
                response_content = llm_response.get("choices", [{}])[0].get("message", {}).get("content", "")

                if not response_content:
                    return {
                        "success": False,
                        "error": "LLM 返回空响应",
                    }

                sql_content = _extract_sql_from_llm_response(response_content)

                if "### SQL_4G ###" in sql_content and "### SQL_5G ###" in sql_content:
                    parts = sql_content.split("### SQL_5G ###")
                    sql_4g = parts[0].replace("### SQL_4G ###", "").strip()
                    sql_5g = parts[1].strip()

                    if not _is_safe_sql(sql_4g) or not _is_safe_sql(sql_5g):
                        return {
                            "success": False,
                            "error": "LLM 生成的 SQL 包含不安全操作",
                        }

                    lte_result = await _execute_single_query(db, sql_4g)
                    nr_result = await _execute_single_query(db, sql_5g)

                    # 合并数据用于导出
                    combined_data = []
                    for item in lte_result.get("data", []):
                        item["network_type"] = "4G"
                        combined_data.append(item)
                    for item in nr_result.get("data", []):
                        item["network_type"] = "5G"
                        combined_data.append(item)

                    # 数据截断逻辑：超过50条时只返回前50条，完整数据走Excel
                    is_truncated = len(combined_data) > MAX_RETURN_ITEMS
                    if is_truncated:
                        logger.info("数据量超过%d条，已截断返回前%d条", MAX_RETURN_ITEMS, MAX_RETURN_ITEMS)

                    result = {
                        "success": True,
                        "template_key": template_key,
                        "is_cross_table": True,
                        "lte_data": lte_result.get("data", [])[:MAX_RETURN_ITEMS] if len(lte_result.get("data", [])) > MAX_RETURN_ITEMS else lte_result.get("data", []),
                        "nr_data": nr_result.get("data", [])[:MAX_RETURN_ITEMS] if len(nr_result.get("data", [])) > MAX_RETURN_ITEMS else nr_result.get("data", []),
                        "lte_row_count": lte_result.get("row_count", 0),
                        "nr_row_count": nr_result.get("row_count", 0),
                        "total_count": len(combined_data),
                        "returned_count": min(len(combined_data), MAX_RETURN_ITEMS),
                        "is_truncated": is_truncated,
                    }

                    # Excel 导出逻辑
                    # 1. 显式要求导出 或 2. 结果超过50条自动导出 或 3. 数据被截断时强制导出
                    should_export = export_excel or (len(combined_data) > 50) or is_truncated
                    if should_export and combined_data:
                        download_url = export_to_excel(combined_data, prefix="metric_query")
                        if download_url:
                            result["download_url"] = download_url
                            result["auto_exported"] = not export_excel  # 标记是否为自动导出

                    return result

                sql = sql_content

                if not _is_safe_sql(sql):
                    return {
                        "success": False,
                        "error": "LLM 生成的 SQL 包含不安全操作",
                    }

                result = await _execute_single_query(db, sql)

                # 数据截断逻辑：超过50条时只返回前50条，完整数据走Excel
                data = result.get("data", [])
                is_truncated = len(data) > MAX_RETURN_ITEMS
                if is_truncated:
                    result["data"] = data[:MAX_RETURN_ITEMS]
                    logger.info("数据量超过%d条，已截断返回前%d条", MAX_RETURN_ITEMS, MAX_RETURN_ITEMS)

                response = {
                    "success": True,
                    "template_key": template_key,
                    **result,
                    "total_count": len(data),
                    "returned_count": len(result["data"]),
                    "is_truncated": is_truncated,
                }

                # Excel 导出逻辑
                # 1. 显式要求导出 或 2. 结果超过50条自动导出 或 3. 数据被截断时强制导出
                should_export = export_excel or (len(data) > 50) or is_truncated
                if should_export and data:
                    download_url = export_to_excel(data, prefix="metric_query")
                    if download_url:
                        response["download_url"] = download_url
                        response["auto_exported"] = not export_excel  # 标记是否为自动导出

                return response

            except LLMError as exc:
                logger.error("LLM 调用失败: %s", exc)
                return {
                    "success": False,
                    "error": f"LLM 调用失败: {exc}",
                }

        else:
            if template_key not in SQL_TEMPLATES:
                available_keys = ", ".join(SQL_TEMPLATES.keys())
                return {
                    "success": False,
                    "error": f"未知模板: {template_key}。可用模板: {available_keys}, freeform",
                }

            template = SQL_TEMPLATES[template_key]
            condition_sql, params = _build_conditions(
                dist_name=dist_name,
                prod_name=prod_name,
                freq_band=freq_band,
                site_type=site_type,
                area=area,
                date_start=date_start,
                date_end=date_end,
                cgi=cgi,
            )

            # 构建 SQL - 使用模板中的 sql 字段，替换占位符
            sql = template['sql'].format(schema=DB_SCHEMA, conditions=condition_sql)

            result = await _execute_single_query(db, sql, params)

            # 数据截断逻辑：超过50条时只返回前50条，完整数据走Excel
            data = result.get("data", [])
            is_truncated = len(data) > MAX_RETURN_ITEMS
            if is_truncated:
                result["data"] = data[:MAX_RETURN_ITEMS]
                logger.info("数据量超过%d条，已截断返回前%d条", MAX_RETURN_ITEMS, MAX_RETURN_ITEMS)

            response = {
                "template_key": template_key,
                **result,
                "total_count": len(data),
                "returned_count": len(result["data"]),
                "is_truncated": is_truncated,
            }

            # Excel 导出逻辑
            # 1. 显式要求导出 或 2. 结果超过50条自动导出 或 3. 数据被截断时强制导出
            should_export = export_excel or (len(data) > 50) or is_truncated
            if should_export and data:
                download_url = export_to_excel(data, prefix=template_key)
                if download_url:
                    response["download_url"] = download_url
                    response["auto_exported"] = not export_excel  # 标记是否为自动导出

            return response

    except Exception as exc:
        logger.error("指标查询异常: %s", exc, exc_info=True)
        return {
            "success": False,
            "error": f"查询执行失败: {exc}",
        }
