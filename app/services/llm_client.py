"""
LLM 调用适配层。

封装 LiteLLM，提供同步调用和流式调用两种模式，
屏蔽底层 LLM Provider 差异。Agent Runner 只能通过本模块调用 LLM。
"""

from typing import Any, AsyncGenerator

import litellm

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger("llm_client")


class LLMError(Exception):
    """LLM 调用失败时抛出的自定义异常。"""
    pass


class LLMClient:
    """LLM 客户端，封装 LiteLLM 的调用细节。"""

    def __init__(self) -> None:
        """初始化 LLM 客户端，从配置加载 API Key、Base URL 和默认模型。"""
        settings = get_settings()
        self.model: str = settings.default_model
        self.api_key: str = settings.llm_api_key
        self.base_url: str = settings.llm_base_url

    def _build_kwargs(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        stream: bool = False,
    ) -> dict[str, Any]:
        """构建 LiteLLM acompletion 调用参数。

        Args:
            messages: 对话消息列表。
            tools: 可选的工具描述列表。
            stream: 是否启用流式模式。

        Returns:
            完整的 kwargs 字典。
        """
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "api_key": self.api_key,
        }
        if self.base_url:
            kwargs["api_base"] = self.base_url
        if stream:
            kwargs["stream"] = True
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        return kwargs

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """调用 LLM 获取完整响应。

        Args:
            messages: 对话消息列表。
            tools: 可选的工具描述列表。
        Returns:
            LLM 完整响应字典。

        Raises:
            LLMError: LLM 调用失败时抛出。
        """
        try:
            logger.info("调用 LLM chat: model=%s, messages_count=%d", self.model, len(messages))
            response = await litellm.acompletion(**self._build_kwargs(messages, tools))
            logger.info("LLM chat 调用成功")
            return response.model_dump()
        except Exception as exc:
            logger.error("LLM chat 调用失败: %s", exc, exc_info=True)
            raise LLMError(f"LLM 调用失败: {exc}") from exc

    async def stream_chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """流式调用 LLM，逐块产出响应。

        Args:
            messages: 对话消息列表。
            tools: 可选的工具描述列表。

        Yields:
            LLM 流式响应的每个 chunk 字典。

        Raises:
            LLMError: LLM 调用失败时抛出。
        """
        try:
            logger.info("调用 LLM stream_chat: model=%s", self.model)
            response = await litellm.acompletion(**self._build_kwargs(messages, tools, stream=True))
            async for chunk in response:
                yield chunk.model_dump()
            logger.info("LLM stream_chat 完成")
        except Exception as exc:
            logger.error("LLM stream_chat 调用失败: %s", exc, exc_info=True)
            raise LLMError(f"LLM 流式调用失败: {exc}") from exc


_llm_client: LLMClient | None = None


def get_llm_client() -> LLMClient:
    """获取 LLM 客户端单例（懒加载，首次调用时才初始化）。"""
    global _llm_client
    if _llm_client is None:
        _llm_client = LLMClient()
    return _llm_client
