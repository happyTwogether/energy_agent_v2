"""
天气查询工具模块（Mock 实现）。

返回模拟天气数据，演示工具注册与调用的标准模式。
"""

from typing import Any

from app.core.logging import get_logger
from app.tools.registry import tool_registry

logger = get_logger("weather_tool")


@tool_registry.tool(
    description="查询指定城市的天气信息，包括温度、天气状况和湿度。",
    parameters={
        "type": "object",
        "properties": {
            "city": {
                "type": "string",
                "description": "要查询天气的城市名称",
            },
        },
        "required": ["city"],
    },
)
async def get_weather(city: str) -> dict[str, Any]:
    """查询指定城市的天气信息（Mock 数据）。

    Args:
        city: 城市名称。

    Returns:
        包含城市、温度、天气状况、湿度的字典。
    """
    try:
        logger.info("查询天气: city=%s", city)
        result: dict[str, Any] = {
            "city": city,
            "temperature": 25,
            "condition": "晴",
            "humidity": 60,
        }
        logger.info("天气查询结果: %s", result)
        return result
    except Exception as exc:
        logger.error("天气查询异常: %s", exc, exc_info=True)
        return {"error": str(exc)}
