"""能效业务函数到 AgentScope Toolkit 的适配层。"""

from collections.abc import Callable
from typing import Any, AsyncContextManager

from agentscope.message import TextBlock, ToolResultState
from agentscope.permission import (
    PermissionBehavior,
    PermissionContext,
    PermissionDecision,
)
from agentscope.tool import ToolBase, ToolChunk, Toolkit

from app.agent.tool_specs import EnergyToolSpec, TOOL_SPECS
from app.agent.errors import PUBLIC_TOOL_ERROR
from app.core.json_utils import dumps_decimal
from app.core.logging import get_logger
from app.services.database import get_session_factory

SessionFactory = Callable[[], AsyncContextManager[Any]]
logger = get_logger("agent_toolkit")


class EnergyFunctionTool(ToolBase):
    """为每次调用创建独立数据库会话的 AgentScope 工具。"""

    def __init__(
        self,
        spec: EnergyToolSpec,
        session_factory: SessionFactory,
    ) -> None:
        super().__init__()
        self._spec = spec
        self._session_factory = session_factory
        self.name = spec.name
        self.description = spec.description
        self.input_schema = spec.input_schema
        self.is_read_only = spec.is_read_only
        self.is_concurrency_safe = spec.is_concurrency_safe

    async def call(self, **kwargs: Any) -> ToolChunk:
        """调用业务函数并保留结构化结果及直出元数据。"""
        try:
            async with self._session_factory() as db:
                payload = await self._spec.handler(db=db, **kwargs)
        except Exception:
            logger.exception("业务工具执行异常: tool=%s", self.name)
            payload = {
                "success": False,
                "error": PUBLIC_TOOL_ERROR,
            }

        direct_answer = extract_direct_answer(payload)
        return ToolChunk(
            content=[
                TextBlock(text=dumps_decimal(payload, ensure_ascii=False)),
            ],
            state=(
                ToolResultState.ERROR
                if payload.get("success") is False
                else ToolResultState.SUCCESS
            ),
            metadata={
                "tool_name": self.name,
                "direct_answer": direct_answer or "",
                "download_url": payload.get("download_url") or "",
            },
        )

    async def check_permissions(
        self,
        tool_input: dict[str, Any],
        context: PermissionContext,
    ) -> PermissionDecision:
        """服务端业务工具无需交互式授权。"""
        return PermissionDecision(
            behavior=PermissionBehavior.ALLOW,
            message=f"允许调用业务工具 {self.name}",
        )


def extract_direct_answer(payload: dict[str, Any]) -> str | None:
    """提取工具已经生成好的 Markdown，并补充下载链接。"""
    report_content = payload.get("report_content")
    if not isinstance(report_content, str) or not report_content:
        return None

    download_url = payload.get("download_url")
    if isinstance(download_url, str) and download_url and download_url not in report_content:
        return f"{report_content}\n\n📥 [点击下载完整报告]({download_url})"
    return report_content


def build_toolkit(
    session_factory: SessionFactory | None = None,
) -> Toolkit:
    """构建包含全部 9 个能效工具的 AgentScope Toolkit。"""
    runtime_session_factory = session_factory or _new_session
    return Toolkit(
        tools=[
            EnergyFunctionTool(
                spec=spec,
                session_factory=runtime_session_factory,
            )
            for spec in TOOL_SPECS
        ],
    )


def _new_session() -> AsyncContextManager[Any]:
    """延迟获取 SQLAlchemy sessionmaker，避免构建 schema 时初始化引擎。"""
    return get_session_factory()()
