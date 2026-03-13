"""
工具注册中心模块。

管理所有可用工具的元数据与可调用函数，
供 Agent Runner 查询和调用。
"""

from typing import Any, Callable

from pydantic import BaseModel, Field

from app.core.logging import get_logger

logger = get_logger("tool_registry")


class ToolMetadata(BaseModel):
    """工具元数据模型，描述工具的名称、功能和参数规范。"""

    name: str = Field(..., description="工具名称")
    description: str = Field(..., description="工具功能描述")
    parameters: dict[str, Any] = Field(
        default_factory=dict,
        description="工具参数的 JSON Schema 定义",
    )


class ToolRegistry:
    """工具注册中心，管理工具的注册、查询和列举。"""

    def __init__(self) -> None:
        """初始化空的工具注册表。"""
        self._tools: dict[str, Callable] = {}
        self._metadata: dict[str, ToolMetadata] = {}

    def register_tool(
        self,
        name: str,
        func: Callable,
        metadata: ToolMetadata,
    ) -> None:
        """注册一个工具到注册中心。

        Args:
            name: 工具唯一名称。
            func: 工具的可调用函数。
            metadata: 工具的元数据描述。
        """
        self._tools[name] = func
        self._metadata[name] = metadata
        logger.info("工具已注册: %s", name)

    def tool(
        self,
        *,
        description: str,
        parameters: dict[str, Any] | None = None,
    ) -> Callable:
        """装饰器：注册一个工具函数，自动以函数名作为工具名称。

        用法::

            @tool_registry.tool(
                description="查询天气",
                parameters={"type": "object", "properties": {...}, "required": [...]},
            )
            async def get_weather(city: str) -> dict:
                ...

        Args:
            description: 工具功能描述。
            parameters: 工具参数的 JSON Schema 定义。
        """
        def decorator(func: Callable) -> Callable:
            name = func.__name__
            meta = ToolMetadata(
                name=name,
                description=description,
                parameters=parameters or {},
            )
            self.register_tool(name=name, func=func, metadata=meta)
            return func
        return decorator

    def get_tool(self, name: str) -> Callable:
        """根据名称获取工具函数。

        Args:
            name: 工具名称。

        Returns:
            对应的工具可调用函数。

        Raises:
            KeyError: 工具不存在时抛出。
        """
        if name not in self._tools:
            logger.error("工具未找到: %s", name)
            raise KeyError(f"工具未找到: {name}")
        return self._tools[name]

    def list_tools(self) -> list[ToolMetadata]:
        """列举所有已注册工具的元数据。

        Returns:
            所有工具元数据的列表。
        """
        return list(self._metadata.values())

    def get_tools_for_llm(self) -> list[dict[str, Any]]:
        """将已注册工具转换为 LLM function calling 所需的格式。

        Returns:
            符合 OpenAI function calling 规范的工具描述列表。
        """
        tools: list[dict[str, Any]] = []
        for meta in self._metadata.values():
            tools.append({
                "type": "function",
                "function": {
                    "name": meta.name,
                    "description": meta.description,
                    "parameters": meta.parameters,
                },
            })
        return tools


tool_registry = ToolRegistry()
