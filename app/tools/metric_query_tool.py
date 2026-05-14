"""指标查询工具 — 按指标名查值、支持自由 SQL 探索、小区级和汇总级。"""

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings, MAX_RETURN_ITEMS
from app.core.logging import get_logger
from app.core.metrics_registry import ALL_METRICS, get_metric_names
from app.prompts.sql_generation import SQL_GENERATION_PROMPT
from app.services.database import get_session_factory
from app.services.llm_client import get_llm_client, LLMError
from app.tools.registry import tool_registry
from app.utils.export_util import export_to_excel

logger = get_logger("metric_query_tool")

DB_SCHEMA = get_settings().db_schema
LTE_REPORT_TABLE = f"{DB_SCHEMA}.lte_report_day_collect"
NR_REPORT_TABLE = f"{DB_SCHEMA}.nr_report_day_collect"
LTE_CELL_TABLE = f"{DB_SCHEMA}.lte_report_day_detail"
NR_CELL_TABLE = f"{DB_SCHEMA}.nr_report_day_detail"

# ── 指标组 (metric_names 中可直接用) ──
METRIC_GROUPS: dict[str, list[str]] = {
    "summary_4g": ["station_total", "station_online", "cell_total", "bbu_energy", "rru_energy",
                    "station_energy", "traffic", "avg_energy_efficiency", "energy_saving_rate"],
    "summary_5g": ["station_total", "station_online", "cell_total", "bbu_energy", "rru_energy",
                    "station_energy", "traffic", "avg_energy_efficiency", "energy_saving_rate"],
    "energy_saving_4g": ["symbol_shutdown_on_rate", "symbol_shutdown_hour", "channel_shutdown_on_rate",
                          "channel_shutdown_hour", "carrier_shutdown_hour", "deepsleep_on_rate", "deepsleep_hour"],
    "energy_saving_5g": ["symbol_shutdown_on_rate", "symbol_shutdown_hour", "channel_shutdown_on_rate",
                          "channel_shutdown_hour", "carrier_shutdown_hour", "deepsleep_on_rate",
                          "deepsleep_hour", "supersleep_hour"],
    "cell_basic": ["cell_traffic", "cell_carrier_shutdown_hour", "cell_channel_shutdown_hour",
                    "cell_symbol_shutdown_hour", "cell_deepsleep_hour", "cell_supersleep_hour"],
}


def _resolve_metric_names(raw_names: list[str]) -> list[str]:
    """展开指标组为具体指标名。"""
    result = []
    for name in raw_names:
        if name in METRIC_GROUPS:
            result.extend(METRIC_GROUPS[name])
        else:
            result.append(name)
    return result


def _detect_network(cgi: str) -> str:
    """根据 CGI 格式判断网络类型。460-00 开头为 5G，其余为 4G。"""
    if cgi and cgi.startswith("460-00"):
        return "5G"
    return "4G"


async def _get_latest_metric_date() -> datetime | None:
    """获取指标表的最新数据日期（取 LTE/NR 两表最大日期的较大值）。"""
    sql = text(f"""
        SELECT MAX(data_date) FROM (
            SELECT MAX(data_date) AS data_date FROM {LTE_REPORT_TABLE}
            UNION ALL
            SELECT MAX(data_date) FROM {NR_REPORT_TABLE}
        ) t
    """)
    factory = get_session_factory()
    async with factory() as sess:
        result = await sess.execute(sql)
        row = result.scalar()
        return row if row else None


def _build_query_sql(
    metric_names: list[str],
    table: str,
    network: str = "5G",
) -> str | None:
    """根据指标名列表构建 SELECT SQL。"""
    all_columns: list[str] = []
    for name in metric_names:
        metric = ALL_METRICS.get(name)
        if not metric:
            continue
        cols = metric["columns_nr"] if network == "5G" else metric["columns_lte"]
        if cols is None:
            continue
        for col in cols:
            if col not in all_columns:
                all_columns.append(col)

    if not all_columns:
        return None

    cols_str = ", ".join(all_columns)
    return f"SELECT data_date, dist_name, prod_name, freq_band, {cols_str} FROM {table}"


def _apply_calc(row: dict, metric_name: str) -> float | None:
    """对查询行应用指标计算函数。"""
    metric = ALL_METRICS.get(metric_name)
    if not metric:
        return None
    try:
        return metric["calc"](row)
    except Exception:
        return None


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
    """构建 SQL WHERE 条件和参数字典。"""
    conditions: list[str] = []
    params: dict[str, Any] = {}

    for col, val in [("dist_name", dist_name), ("prod_name", prod_name),
                      ("freq_band", freq_band), ("site_type", site_type),
                      ("area", area), ("cgi", cgi)]:
        if val:
            conditions.append(f"{col} = :{col}")
            params[col] = val

    if date_start:
        conditions.append("data_date >= :date_start")
        params["date_start"] = date_start
    if date_end:
        conditions.append("data_date <= :date_end")
        params["date_end"] = date_end

    condition_sql = " AND ".join(conditions) if conditions else ""
    if condition_sql:
        condition_sql = "WHERE " + condition_sql
    return condition_sql, params


def _is_safe_sql(sql: str) -> bool:
    upper = sql.upper().strip()
    for kw in ["INSERT", "UPDATE", "DELETE", "DROP", "TRUNCATE", "ALTER", "CREATE", "EXEC", "EXECUTE", "UNION"]:
        if kw in upper:
            return False
    return upper.startswith("SELECT")


# ── 自由探索 LLM 辅助 ──
def _build_llm_prompt(
    metric_desc: str,
    dist_name: str | None = None,
    prod_name: str | None = None,
    date_start: str | None = None,
    date_end: str | None = None,
) -> str:
    condition_parts = []
    if dist_name:
        condition_parts.append(f"地市: {dist_name}")
    if prod_name:
        condition_parts.append(f"厂家: {prod_name}")
    if date_start:
        condition_parts.append(f"开始日期: {date_start}")
    if date_end:
        condition_parts.append(f"结束日期: {date_end}")
    return SQL_GENERATION_PROMPT.format(
        schema=DB_SCHEMA,
        metric_desc=metric_desc,
        condition_str="，".join(condition_parts) if condition_parts else "无",
    )


def _extract_sql_from_llm_response(response_text: str) -> str:
    sql = response_text.strip()
    for prefix in ["```sql", "```"]:
        if sql.startswith(prefix):
            sql = sql[len(prefix):]
    if sql.endswith("```"):
        sql = sql[:-3]
    return sql.strip()


async def _execute_single_query(db: AsyncSession, sql: str, params: dict | None = None) -> dict[str, Any]:
    try:
        result = await db.execute(text(sql), params or {})
        rows = result.mappings().all()
        return {"success": True, "row_count": len(rows), "data": [dict(row) for row in rows]}
    except Exception as exc:
        logger.error("SQL 执行异常: %s", exc, exc_info=True)
        return {"success": False, "error": f"SQL 执行失败: {exc}"}


# ── 主工具 ──
@tool_registry.tool(
    description="查询能耗指标数值。传 metric_names 按指标名或指标组查值，传 metric_desc 进行自由 SQL 探索。传 cgi 查小区级，不传查汇总级。",
    parameters={
        "type": "object",
        "properties": {
            "metric_names": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": list(METRIC_GROUPS.keys()),
                },
                "description": "指标组或指标名列表。指标组: summary_4g,summary_5g,energy_saving_4g,energy_saving_5g,cell_basic。单指标如 bbu_energy,station_online_ratio,cell_traffic 等，传错返回可用列表",
            },
            "metric_desc": {
                "type": "string",
                "description": "自由探索模式的查询需求描述，与 metric_names 互斥",
            },
            "dist_name": {"type": "string", "description": "地市名称"},
            "prod_name": {"type": "string", "description": "设备厂家"},
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
            "date_start": {"type": "string", "description": "开始日期 (YYYY-MM-DD)"},
            "date_end": {
                "type": "string",
                "description": "结束日期 (YYYY-MM-DD)",
            },
            "cgi": {"type": "string", "description": "小区 CGI"},
            "export_excel": {
                "type": "boolean",
                "description": "是否导出 Excel",
                "default": False,
            },
        },
        "required": [],
    },
)
async def query_metric(
    db: AsyncSession,
    metric_names: list[str] | None = None,
    metric_desc: str | None = None,
    dist_name: str | None = None,
    prod_name: str | None = None,
    freq_band: str | None = None,
    site_type: str | None = None,
    area: str | None = None,
    date_start: str | None = None,
    date_end: str | None = None,
    cgi: str | None = None,
    export_excel: bool = False,
) -> dict[str, Any]:
    """查询能耗指标数值。"""
    # 默认日期：优先取 DB 最新日期，DB 无数据时回退到当前时间
    db_latest = await _get_latest_metric_date()
    date_note = ""

    if not date_start and not date_end:
        if db_latest:
            date_end = db_latest.strftime("%Y-%m-%d")
            date_start = (db_latest - timedelta(days=7)).strftime("%Y-%m-%d")
        else:
            date_end = datetime.now().strftime("%Y-%m-%d")
            date_start = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")

    if date_start and db_latest:
        db_latest_str = db_latest.strftime("%Y-%m-%d")
        if date_start > db_latest_str:
            date_note = f"您查询的日期 {date_start} 暂无最新数据，已自动调整至 {db_latest_str}"
            date_end = db_latest_str
            date_start = (db_latest - timedelta(days=7)).strftime("%Y-%m-%d")

    network = _detect_network(cgi or "")
    is_cell = bool(cgi)

    # ── 自由探索模式 ──
    if metric_desc:
        if not metric_desc.strip():
            return {"success": False, "error": "metric_desc 不能为空"}
        prompt = _build_llm_prompt(metric_desc, dist_name, prod_name, date_start, date_end)
        messages = [
            {"role": "system", "content": "你是一个专业的 SQL 查询生成助手。"},
            {"role": "user", "content": prompt},
        ]
        try:
            llm_response = await get_llm_client().chat(messages=messages)
            sql = _extract_sql_from_llm_response(
                llm_response.get("choices", [{}])[0].get("message", {}).get("content", "")
            )
            if not sql:
                return {"success": False, "error": "LLM 未生成有效 SQL"}
            if not _is_safe_sql(sql):
                return {"success": False, "error": "生成的 SQL 包含不安全操作"}
            condition_sql, params = _build_conditions(dist_name, prod_name, freq_band, site_type, area, date_start, date_end, cgi)
            sql = sql.rstrip(";") + " " + condition_sql
            result = await _execute_single_query(db, sql, params)
        except LLMError as exc:
            return {"success": False, "error": f"LLM 调用失败: {exc}"}
        final = _finalize_result(result, export_excel)
        if date_note:
            final["date_note"] = date_note
        return final

    # ── 指标名模式 ──
    if not metric_names:
        return {"success": False, "error": "请提供 metric_names 或 metric_desc"}

    # 检查无效指标名
    unknown = [n for n in metric_names if n not in ALL_METRICS and n not in METRIC_GROUPS]
    if unknown:
        avail = sorted(ALL_METRICS.keys())
        return {"success": False, "error": f"未知指标: {unknown}。可用: {avail}，指标组: {list(METRIC_GROUPS.keys())}"}

    resolved = _resolve_metric_names(metric_names)
    table = (LTE_CELL_TABLE if network == "4G" else NR_CELL_TABLE) if is_cell else (LTE_REPORT_TABLE if network == "4G" else NR_REPORT_TABLE)
    sql = _build_query_sql(resolved, table, network)
    if not sql:
        return {"success": False, "error": f"指标 {metric_names} 在当前网络类型下无可用列"}

    condition_sql, params = _build_conditions(dist_name, prod_name, freq_band, site_type, area, date_start, date_end, cgi)
    sql = sql + " " + condition_sql

    result = await _execute_single_query(db, sql, params)
    if not result["success"]:
        return result

    # 对每行数据应用计算，构造 label:value 结构
    data = result["data"]
    values: dict[str, Any] = {}
    detail_rows: list[dict] = []
    for row in data:
        row_values = {}
        for name in resolved:
            metric = ALL_METRICS.get(name)
            if not metric:
                continue
            val = _apply_calc(row, name)
            if val is not None:
                key = metric["label"]
                row_values[name] = {"label": metric["label"], "value": val, "unit": metric["unit"]}
                if name not in values:
                    values[name] = {"label": metric["label"], "value": val, "unit": metric["unit"]}
        if row_values:
            detail_rows.append({"date": row.get("data_date"), "dist_name": row.get("dist_name"),
                                 "prod_name": row.get("prod_name"), "cgi": row.get("cgi"), **row_values})

    is_truncated = len(detail_rows) > MAX_RETURN_ITEMS
    if is_truncated:
        detail_rows = detail_rows[:MAX_RETURN_ITEMS]

    response = {
        "success": True,
        "metrics": values,
        "rows": detail_rows,
        "row_count": len(data),
        "returned_count": len(detail_rows),
        "is_truncated": is_truncated,
        **({"date_note": date_note} if date_note else {}),
    }

    should_export = export_excel or (len(data) > 50) or is_truncated
    if should_export and data:
        download_url = export_to_excel(data, prefix="metric_query")
        if download_url:
            response["download_url"] = download_url

    return response


def _finalize_result(result: dict, export_excel: bool) -> dict:
    """处理查询结果：截断 + Excel 导出。"""
    if not result.get("success"):
        return result
    data = result.get("data", [])
    is_truncated = len(data) > MAX_RETURN_ITEMS
    if is_truncated:
        result["data"] = data[:MAX_RETURN_ITEMS]
    response = {"success": True, "total_count": len(data), "returned_count": len(result["data"]),
                "is_truncated": is_truncated, **result}
    should_export = export_excel or (len(data) > 50) or is_truncated
    if should_export and data:
        url = export_to_excel(data, prefix="metric_query")
        if url:
            response["download_url"] = url
    return response
