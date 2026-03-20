"""
节电分析工具模块（占位实现）。

当前功能暂未开放，数据表建设中。
"""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.tools.registry import tool_registry

logger = get_logger("energy_saving_tool")


@tool_registry.tool(
    description="""分析 4G/5G 网络节电潜力（该功能暂未开放，数据表建设中）。

参数说明:
- dist_name: 区县名称
- prod_name: 设备厂商
- date_start: 开始日期 (YYYY-MM-DD)
- date_end: 结束日期 (YYYY-MM-DD)
- network_type: 网络类型 (4G/5G/ALL)

注意: 当前功能暂未开放，调用将返回建设中提示。
""",
    parameters={
        "type": "object",
        "properties": {
            "dist_name": {
                "type": "string",
                "description": "区县名称",
            },
            "prod_name": {
                "type": "string",
                "description": "设备厂商",
            },
            "date_start": {
                "type": "string",
                "description": "开始日期 (YYYY-MM-DD)",
            },
            "date_end": {
                "type": "string",
                "description": "结束日期 (YYYY-MM-DD)",
            },
            "network_type": {
                "type": "string",
                "description": "网络类型 (4G/5G/ALL)",
            },
        },
        "required": ["dist_name", "prod_name", "date_start", "date_end", "network_type"],
    },
)
async def analyze_energy_saving(
    dist_name: str,
    prod_name: str,
    date_start: str,
    date_end: str,
    network_type: str,
    db: AsyncSession,
) -> dict[str, Any]:
    """分析 4G/5G 网络节电潜力（占位实现）。

    Args:
        dist_name: 区县名称。
        prod_name: 设备厂商。
        date_start: 开始日期。
        date_end: 结束日期。
        network_type: 网络类型 (4G/5G/ALL)。
        db: 数据库会话。

    Returns:
        建设中提示字典。
    """
    logger.info(
        "节电分析请求(占位): dist_name=%s, prod_name=%s, network_type=%s",
        dist_name,
        prod_name,
        network_type,
    )

    return {
        "success": False,
        "error": "该功能暂未开放，数据表建设中",
    }
