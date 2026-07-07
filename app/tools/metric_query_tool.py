"""指标查询工具 — 结构化参数驱动的汇总级指标查询。

特点：
- 预定义指标 + 指标组，SQL 由代码拼接，100% 不会列名错误
- 支持聚合（GROUP BY）、排序（ORDER BY）、Top N（LIMIT）
- 显式 4G/5G 网络选择
"""

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.metrics_registry import ALL_METRICS, REPORT_METRICS
from app.tools.registry import tool_registry
from app.utils.export_util import truncate_and_export
from app.utils.sql_helpers import error_response, get_latest_date_standalone, success_response

logger = get_logger("metric_query_tool")

DB_SCHEMA = get_settings().db_schema
LTE_REPORT_TABLE = f"{DB_SCHEMA}.lte_report_day_collect"
NR_REPORT_TABLE = f"{DB_SCHEMA}.nr_report_day_collect"

_DIMENSION_COLS = ["dist_name", "prod_name", "freq_band", "site_type", "area"]
_GROUP_BY_DIMS = _DIMENSION_COLS + ["data_date"]

# -- 指标组 --
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
}

_GROUP_NAMES = list(METRIC_GROUPS.keys())
_ALL_METRIC_NAMES = sorted(REPORT_METRICS.keys())


def _resolve_metric_names(raw_names: list[str]) -> list[str]:
    result = []
    for name in raw_names:
        if name in METRIC_GROUPS:
            result.extend(METRIC_GROUPS[name])
        else:
            result.append(name)
    return result


def _resolve_columns(metric_names: list[str], network: str) -> list[tuple[str, str]]:
    """解析指标名为数据库列名。返回 [(metric_name, db_column), ...]"""
    cols = []
    for name in metric_names:
        metric = ALL_METRICS.get(name)
        if not metric:
            continue
        candidates = metric["columns_nr"] if network == "5G" else metric["columns_lte"]
        if not candidates:
            continue
        for col in candidates:
            if col not in [c[1] for c in cols]:
                cols.append((name, col))
    return cols


def _agg_func(metric_name: str) -> str:
    """根据指标名推断聚合函数：rate 类用 AVG，其他用 SUM。"""
    if "rate" in metric_name or "ratio" in metric_name or "efficiency" in metric_name:
        return "AVG"
    return "SUM"


def _build_agg_sql(
    metric_names: list[str],
    table: str,
    network: str,
    group_by: list[str] | None,
    order_by: list[dict] | None,
    limit_val: int | None,
    conditions: str,
) -> str:
    """构建含聚合/排序/限制的 SELECT SQL。"""
    resolved_metrics = _resolve_metric_names(metric_names)
    cols = _resolve_columns(resolved_metrics, network)

    select_parts: list[str] = []
    if group_by:
        for dim in group_by:
            if dim in _GROUP_BY_DIMS:
                select_parts.append(dim)
        for metric_name, db_col in cols:
            agg = _agg_func(metric_name)
            if agg == "AVG":
                # rate/ratio 列存 VARCHAR ("27.89%")→去%→CAST
                col_expr = f"CAST(NULLIF(REPLACE({db_col}::TEXT,'%',''),'') AS DOUBLE PRECISION)"
            else:
                col_expr = db_col
            select_parts.append(f"{agg}({col_expr}) AS agg_{metric_name}")
    else:
        select_parts.append("data_date")
        select_parts.extend(_DIMENSION_COLS)
        for _, db_col in cols:
            select_parts.append(db_col)

    sql = f"SELECT {', '.join(select_parts)} FROM {table} {conditions}"

    if group_by:
        sql += " GROUP BY " + ", ".join(g for g in group_by if g in _GROUP_BY_DIMS)

    if order_by:
        order_clauses = []
        for ob in order_by:
            field = ob.get("field", "") if isinstance(ob, dict) else str(ob)
            direction = ob.get("direction", "DESC") if isinstance(ob, dict) else "DESC"
            direction = direction.upper()
            if direction not in ("ASC", "DESC"):
                direction = "DESC"
            if field in _GROUP_BY_DIMS or field.startswith("agg_"):
                order_clauses.append(f"{field} {direction}")
            elif field in ALL_METRICS:
                order_clauses.append(f"agg_{field} {direction}")
        if order_clauses:
            sql += " ORDER BY " + ", ".join(order_clauses)

    if limit_val:
        sql += f" LIMIT {int(limit_val)}"

    return sql


def _build_conditions(
    dist_name: str | None = None,
    prod_name: str | None = None,
    freq_band: str | None = None,
    site_type: str | None = None,
    area: str | None = None,
    date_start: str | None = None,
    date_end: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """构建 SQL WHERE 条件。"""
    conds = []
    params: dict[str, Any] = {}
    for col, val in [("dist_name", dist_name), ("prod_name", prod_name),
                      ("freq_band", freq_band), ("site_type", site_type),
                      ("area", area)]:
        if val:
            conds.append(f"{col} = :{col}")
            params[col] = val
    if date_start:
        conds.append("data_date >= :date_start")
        params["date_start"] = date_start
    if date_end:
        conds.append("data_date <= :date_end")
        params["date_end"] = date_end
    condition_sql = " AND ".join(conds)
    if condition_sql:
        return "WHERE " + condition_sql, params
    return "", params




async def _execute_query(db: AsyncSession, sql: str, params: dict) -> dict[str, Any]:
    try:
        result = await db.execute(text(sql), params or {})
        rows = result.mappings().all()
        return success_response({"row_count": len(rows), "data": [dict(row) for row in rows]})
    except Exception as exc:
        logger.error("SQL 执行异常: %s", exc, exc_info=True)
        return error_response(f"SQL 执行失败: {exc}")


def _apply_calc(row: dict, metric_name: str) -> float | None:
    metric = ALL_METRICS.get(metric_name)
    if not metric:
        return None
    try:
        return metric["calc"](row)
    except Exception:
        return None


@tool_registry.tool(
    description="查询汇总级能耗指标。支持聚合（group_by）、排序（order_by）、Top N（limit）。传 metric_names 指定指标名或指标组。",
    parameters={
        "type": "object",
        "properties": {
            "metric_names": {
                "type": "array",
                "items": {"type": "string", "enum": _ALL_METRIC_NAMES + _GROUP_NAMES},
                "description": "指标名或指标组。指标组: summary_4g,summary_5g,energy_saving_4g,energy_saving_5g",
            },
            "network": {
                "type": "string",
                "enum": ["4G", "5G"],
                "description": "网络类型。用户未指定时根据上下文推断，4G 选 4G，5G 选 5G，两者都需要时分两次调用",
            },
            "dist_name": {"type": "string", "description": "地市名称"},
            "prod_name": {"type": "string", "description": "设备厂家"},
            "freq_band": {"type": "string", "description": "频段"},
            "site_type": {"type": "string", "description": "站型"},
            "area": {"type": "string", "description": "区域"},
            "date_start": {"type": "string", "description": "开始日期 (YYYY-MM-DD)"},
            "date_end": {"type": "string", "description": "结束日期 (YYYY-MM-DD)"},
            "group_by": {
                "type": "array",
                "items": {"type": "string", "enum": _GROUP_BY_DIMS},
                "description": "聚合维度列表。传一个或多个: dist_name/prod_name/freq_band/site_type/area/data_date。不传则返回明细",
            },
            "order_by": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "field": {"type": "string", "description": "排序字段：指标名或维度名"},
                        "direction": {"type": "string", "enum": ["asc", "desc"]},
                    },
                },
                "description": "排序规则。field 传指标名或 group_by 中的维度名",
            },
            "limit": {
                "type": "integer",
                "description": "返回 TOP N 条。用户说 TOP N 时设置",
            },
            "export_excel": {
                "type": "boolean",
                "description": "是否导出 Excel",
                "default": False,
            },
        },
        "required": ["metric_names", "network"],
    },
)
async def query_metric(
    db: AsyncSession,
    metric_names: list[str],
    network: str,
    dist_name: str | None = None,
    prod_name: str | None = None,
    freq_band: str | None = None,
    site_type: str | None = None,
    area: str | None = None,
    date_start: str | None = None,
    date_end: str | None = None,
    group_by: list[str] | None = None,
    order_by: list[dict] | None = None,
    limit: int | None = None,
    export_excel: bool = False,
) -> dict[str, Any]:
    """查询汇总级能耗指标。"""
    # 默认日期
    db_latest = await get_latest_date_standalone([LTE_REPORT_TABLE, NR_REPORT_TABLE])
    date_note = ""
    if not date_start and not date_end:
        if db_latest:
            date_end = db_latest.strftime("%Y-%m-%d")
            date_start = (db_latest - timedelta(days=7)).strftime("%Y-%m-%d")
        else:
            date_end = datetime.now().strftime("%Y-%m-%d")
            date_start = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    if date_start and db_latest and date_start > db_latest.strftime("%Y-%m-%d"):
        date_note = f"您查询的日期 {date_start} 暂无最新数据，已自动调整至 {db_latest.strftime('%Y-%m-%d')}"
        date_end = db_latest.strftime("%Y-%m-%d")
        date_start = (db_latest - timedelta(days=7)).strftime("%Y-%m-%d")

    # 选表
    table = NR_REPORT_TABLE if network == "5G" else LTE_REPORT_TABLE

    # 校验指标
    unknown = [n for n in metric_names if n not in ALL_METRICS and n not in METRIC_GROUPS]
    if unknown:
        avail = sorted(ALL_METRICS.keys())
        return error_response(f"未知指标: {unknown}。可用: {avail}，指标组: {_GROUP_NAMES}")

    # 构建 SQL
    conditions, params = _build_conditions(
        dist_name=dist_name, prod_name=prod_name, freq_band=freq_band,
        site_type=site_type, area=area, date_start=date_start, date_end=date_end,
    )
    sql = _build_agg_sql(metric_names, table, network, group_by, order_by, limit, conditions)

    # 执行
    result = await _execute_query(db, sql, params)
    if not result["success"]:
        return result

    # 处理结果
    data = result["data"]
    resolved = _resolve_metric_names(metric_names)
    values: dict[str, Any] = {}
    detail_rows: list[dict] = []
    for row in data:
        row_values = {}
        for name in resolved:
            metric = ALL_METRICS.get(name)
            if not metric or metric["table"] != "report":
                continue
            # 聚合模式用 agg_ 前缀的别名列
            agg_col = f"agg_{name}"
            if group_by and agg_col in row:
                val = round(row[agg_col], 2) if row[agg_col] is not None else 0
            else:
                val = _apply_calc(row, name)
            if val is not None:
                row_values[name] = {"label": metric["label"], "value": val, "unit": metric["unit"]}
                if name not in values:
                    values[name] = {"label": metric["label"], "value": val, "unit": metric["unit"]}
        if row_values:
            row_out = {}
            if group_by:
                row_out = {dim: row.get(dim) for dim in group_by if dim in row}
            row_out["date"] = row.get("data_date", "")
            detail_rows.append({**row_out, **row_values})

    # 截断返回行；Excel 用原始 SQL 数据导出（扁平列）
    returned_rows = detail_rows[:50]
    is_truncated = len(detail_rows) > 50
    download_url = ""
    if export_excel:
        _, excel_meta = truncate_and_export(data, prefix="metric_query", export_excel=True)
        download_url = excel_meta.get("download_url", "")
    return {
        "success": True,
        **({"download_url": download_url} if download_url else {}),
        "metrics": values,
        "rows": returned_rows,
        "is_truncated": is_truncated,
        "total_count": len(detail_rows),
        "returned_count": len(returned_rows),
        **({"date_note": date_note} if date_note else {}),
    }
