"""小区级指标查询工具 — 查 detail 表（lte_report_day_detail / nr_report_day_detail）。

与 query_metric 的区别：
- 查 detail 表而非 collect 表
- cgi / cell_name 二选一
- 不支持聚合（小区级数据本身就是明细）
"""

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.metrics_registry import CELL_METRICS
from app.services.cell_resolver import resolve_cell_identifier
from app.utils.cell_lookup import (
    infer_network_from_cgi,
    resolution_error_response,
)
from app.utils.export_util import truncate_and_export
from app.utils.sql_helpers import error_response, get_latest_date_standalone

logger = get_logger("cell_metric_query_tool")

DB_SCHEMA = get_settings().db_schema
LTE_CELL_TABLE = f"{DB_SCHEMA}.lte_report_day_detail"
NR_CELL_TABLE = f"{DB_SCHEMA}.nr_report_day_detail"

_CELL_GROUP_NAMES = sorted(CELL_METRICS.keys())


def _detect_network(cgi: str) -> str:
    return infer_network_from_cgi(cgi)


TOOL_DESCRIPTION = "通过 CGI 或小区中文名查询单个小区的节能指标明细数据。"
TOOL_INPUT_SCHEMA = {
        "type": "object",
        "properties": {
            "cgi": {
                "type": "string",
                "description": "小区 CGI，格式 460-00-基站号-小区号；与 cell_name 同时传入时优先",
            },
            "cell_name": {
                "type": "string",
                "description": "小区中文名或其连续片段，未提供 CGI 时使用包含匹配",
            },
            "network": {
                "type": "string",
                "enum": ["4G", "5G"],
                "description": "网络制式，用中文名查询时可用于过滤候选",
            },
            "metric_names": {
                "type": "array",
                "items": {"type": "string", "enum": _CELL_GROUP_NAMES},
                "description": "小区级指标名列表。常用: cell_traffic,cell_carrier_shutdown_hour,cell_channel_shutdown_hour,cell_symbol_shutdown_hour,cell_deepsleep_hour,cell_supersleep_hour",
            },
            "date_start": {"type": "string", "description": "开始日期 (YYYY-MM-DD)"},
            "date_end": {"type": "string", "description": "结束日期 (YYYY-MM-DD)"},
            "export_excel": {"type": "boolean", "description": "是否导出 Excel", "default": False},
        },
        "required": ["metric_names"],
}


async def query_cell_metric(
    db: AsyncSession,
    metric_names: list[str],
    cgi: str | None = None,
    cell_name: str | None = None,
    network: str | None = None,
    date_start: str | None = None,
    date_end: str | None = None,
    export_excel: bool = False,
) -> dict[str, Any]:
    """查询小区级指标明细。"""
    cgi = (cgi or "").strip()
    cell_name = (cell_name or "").strip()
    if not cgi and not cell_name:
        return error_response("请提供 CGI 或小区中文名。")
    if network not in (None, "4G", "5G"):
        return error_response("网络制式仅支持 4G 或 5G。")

    # 校验指标
    unknown = [n for n in metric_names if n not in CELL_METRICS]
    if unknown:
        avail = sorted(CELL_METRICS.keys())
        return error_response(f"未知小区指标: {unknown}。可用: {avail}")

    # 默认日期
    db_latest = await get_latest_date_standalone([LTE_CELL_TABLE, NR_CELL_TABLE])
    date_note = ""
    if not date_start and not date_end:
        if db_latest:
            date_end = db_latest.strftime("%Y-%m-%d")
            date_start = (db_latest - timedelta(days=7)).strftime("%Y-%m-%d")
        else:
            date_end = datetime.now().strftime("%Y-%m-%d")
            date_start = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")

    name_resolution = None
    if not cgi:
        try:
            name_resolution = await resolve_cell_identifier(
                db=db,
                cell_name=cell_name,
                network=network,
            )
        except Exception as exc:
            logger.error("单小区指标名称解析异常: %s", exc, exc_info=True)
            return error_response("小区名称解析失败，请稍后重试。")
        if name_resolution.status != "resolved":
            return resolution_error_response(name_resolution)
        cgi = name_resolution.cgi or ""
        resolved_network = name_resolution.network or _detect_network(cgi)
    else:
        resolved_network = _detect_network(cgi)

    # 选表：CGI 与中文名同时传入时，保持 CGI 优先。
    table = NR_CELL_TABLE if resolved_network == "5G" else LTE_CELL_TABLE

    # 构建列
    cols = ["data_date", "dist_name", "prod_name", "freq_band", "cgi", "cell_name", "gnb_name"]
    for name in metric_names:
        metric = CELL_METRICS.get(name)
        if not metric:
            continue
        col_list = metric["columns_nr"] if resolved_network == "5G" else metric["columns_lte"]
        if col_list:
            for c in col_list:
                if c not in cols:
                    cols.append(c)

    # 构建 SQL
    params: dict[str, Any] = {"cgi": cgi}
    conds = ["cgi = :cgi"]
    if date_start:
        conds.append("data_date >= :date_start")
        params["date_start"] = date_start
    if date_end:
        conds.append("data_date <= :date_end")
        params["date_end"] = date_end

    sql = text(
        f"SELECT {', '.join(cols)} FROM {table} WHERE {' AND '.join(conds)} ORDER BY data_date"
    )

    try:
        result = await db.execute(sql, params)
        rows = result.mappings().all()
        if not rows:
            return error_response(f"未找到 CGI={cgi} 的小区数据")

        data = [dict(row) for row in rows]
        values: dict[str, Any] = {}
        detail_rows: list[dict] = []
        for row in data:
            row_values = {}
            for name in metric_names:
                metric = CELL_METRICS.get(name)
                if not metric:
                    continue
                try:
                    val = metric["calc"](row)
                except Exception:
                    val = None
                if val is not None:
                    row_values[name] = {"label": metric["label"], "value": val, "unit": metric["unit"]}
                    if name not in values:
                        values[name] = {"label": metric["label"], "value": val, "unit": metric["unit"]}
            if row_values:
                detail_rows.append({
                    "date": row.get("data_date"),
                    "cgi": row.get("cgi"),
                    "cell_name": row.get("cell_name"),
                    **row_values,
                })

        detail_rows, meta = truncate_and_export(data, prefix="cell_metric", export_excel=export_excel)
        download_url = meta.pop("download_url", None)
        return {
            "success": True,
            **({"download_url": download_url} if download_url else {}),
            "network": resolved_network,
            "metrics": values,
            "rows": detail_rows,
            "is_truncated": meta.pop("is_truncated"),
            **meta,
            **({"date_note": date_note} if date_note else {}),
            **({
                "cell_name_match": {
                    "query": name_resolution.query,
                    "match_type": name_resolution.match_type,
                }
            } if name_resolution else {}),
        }
    except Exception as exc:
        logger.error("小区指标查询异常: %s", exc, exc_info=True)
        return error_response("小区指标查询失败，请稍后重试")
