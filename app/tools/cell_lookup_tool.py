"""小区中文名查询 CGI 工具。"""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.services.cell_resolver import (
    CellResolverValidationError,
    resolve_cell_identifier,
)
from app.utils.cell_lookup import resolution_error_response
from app.utils.sql_helpers import error_response


logger = get_logger("cell_lookup_tool")


TOOL_DESCRIPTION = (
    "通过小区中文名或连续名称片段解析4G/5G小区CGI。"
    "适用：用户只要求查找小区名称对应的CGI或小区标识。"
    "不适用：查询小区指标、明细或执行节电诊断。"
)
TOOL_INPUT_SCHEMA = {
        "type": "object",
        "properties": {
            "cell_name": {
                "type": "string",
                "description": "小区中文名或其连续片段",
            },
            "network": {
                "type": "string",
                "enum": ["4G", "5G"],
                "description": "网络制式，不传则同时查询 4G/5G",
            },
            "province": {"type": "string", "description": "省份"},
            "dist_name": {"type": "string", "description": "地市名称"},
            "county_name": {"type": "string", "description": "区县名称"},
            "prod_name": {"type": "string", "description": "设备厂家"},
        },
        "required": ["cell_name"],
}


async def resolve_cell_cgi(
    cell_name: str,
    db: AsyncSession,
    network: str | None = None,
    province: str | None = None,
    dist_name: str | None = None,
    county_name: str | None = None,
    prod_name: str | None = None,
) -> dict[str, Any]:
    """从 LTE/NR 工参表查找唯一 CGI。"""
    try:
        resolution = await resolve_cell_identifier(
            db=db,
            cell_name=cell_name,
            network=network,
            province=province,
            dist_name=dist_name,
            county_name=county_name,
            prod_name=prod_name,
        )
    except CellResolverValidationError as exc:
        return error_response(str(exc))
    except Exception as exc:
        logger.error("小区 CGI 解析异常: %s", exc, exc_info=True)
        return error_response("小区 CGI 查询失败，请稍后重试。")

    if resolution.status != "resolved":
        return resolution_error_response(resolution)
    return {
        "success": True,
        "cell_name_query": resolution.query,
        "match_type": resolution.match_type,
        **(resolution.candidate or {}),
    }
