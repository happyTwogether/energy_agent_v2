"""
LLM 调用适配层。

封装 LiteLLM，提供同步调用和流式调用两种模式，
屏蔽底层 LLM Provider 差异。Agent Runner 只能通过本模块调用 LLM。
"""

import json
import re
from typing import Any, AsyncGenerator

import litellm

# 禁用 LiteLLM 的回调上报和网络请求（版本检查、使用统计等）
litellm.success_callback = []
litellm.failure_callback = []
litellm.set_verbose = False

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger("llm_client")


def _extract_tool_calls_from_content(content: str | None) -> list[dict[str, Any]] | None:
    """从 <tool> 标签中提取工具调用。

    格式: <tool>{"name": "tool_name", "arguments": {...}}</tool>

    Args:
        content: LLM 返回的文本内容。

    Returns:
        符合 OpenAI tool_calls 格式的工具调用列表，如果没有则返回 None。
    """
    if not content:
        return None

    tool_calls = []
    pattern = r'<tool>\s*(\{[\s\S]*?\})\s*</tool>'

    for match in re.findall(pattern, content):
        try:
            data = json.loads(match)
            if "name" in data and "arguments" in data:
                tool_calls.append({
                    "id": f"call_{len(tool_calls)}",
                    "type": "function",
                    "function": {
                        "name": data["name"],
                        "arguments": json.dumps(data["arguments"], ensure_ascii=False)
                    }
                })
        except json.JSONDecodeError:
            continue

    return tool_calls if tool_calls else None


def _normalize_response(response: dict[str, Any]) -> dict[str, Any]:
    """将响应中的 <tool> 标签转换为标准 tool_calls 格式。

    Args:
        response: LLM 原始响应。

    Returns:
        标准化的响应字典。
    """
    choices = response.get("choices") or []
    if not choices:
        return response

    choice = choices[0]
    message = choice.get("message") or choice.get("delta") or {}
    content = message.get("content")

    # 如果已经有标准 tool_calls，直接返回
    if message.get("tool_calls"):
        return response

    # 从 content 中提取 <tool> 标签
    if content:
        extracted = _extract_tool_calls_from_content(content)
        if extracted:
            logger.debug("从响应中提取到 %d 个工具调用", len(extracted))
            message["tool_calls"] = extracted
            message["content"] = None
            if "message" in choice:
                choice["message"] = message
            else:
                choice["delta"] = message
            response["choices"] = choices

    return response


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
        settings = get_settings()
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "api_key": self.api_key,
            "max_tokens": settings.llm_max_tokens,
            "timeout": 180,
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
        """调用 LLM 获取完整响应。"""
        try:
            logger.info("调用 LLM chat: model=%s", self.model)
            response = await litellm.acompletion(**self._build_kwargs(messages, tools))
            data = response.model_dump()
            normalized = _normalize_response(data)
            logger.info("LLM chat 调用成功")
            return normalized
        except Exception as exc:
            logger.error("LLM chat 调用失败: %s", exc, exc_info=True)
            raise LLMError(f"LLM 调用失败: {exc}") from exc

    async def stream_chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """流式调用 LLM，逐块产出响应。"""
        try:
            logger.info("调用 LLM stream_chat: model=%s", self.model)
            response = await litellm.acompletion(**self._build_kwargs(messages, tools, stream=True))
            chunk_count = 0
            collected_content = ""

            async for chunk in response:
                chunk_count += 1
                data = chunk.model_dump()
                choices = data.get("choices") or []

                if choices:
                    delta = choices[0].get("delta", {})
                    content = delta.get("content") or ""
                    if content:
                        collected_content += content

                    # 检查是否是结束 chunk，尝试提取工具调用
                    finish_reason = choices[0].get("finish_reason")
                    if finish_reason and collected_content and tools:
                        extracted = _extract_tool_calls_from_content(collected_content)
                        if extracted:
                            logger.info("流式响应中提取到 %d 个工具调用", len(extracted))
                            # 构造流式格式的 tool_calls（分多个 chunk 发送，模拟流式行为）
                            for i, tc in enumerate(extracted):
                                yield {
                                    "choices": [{
                                        "index": 0,
                                        "delta": {
                                            "role": "assistant",
                                            "tool_calls": [{"index": i, **tc}]
                                        },
                                        "finish_reason": None
                                    }]
                                }
                            # 最后发送结束 chunk
                            yield {
                                "choices": [{
                                    "index": 0,
                                    "delta": {},
                                    "finish_reason": "tool_calls"
                                }]
                            }
                            continue

                yield data

            logger.info("LLM stream_chat 完成, 共 %d chunks", chunk_count)
        except Exception as exc:
            logger.error("LLM stream_chat 调用失败: %s", exc, exc_info=True)
            raise LLMError(f"LLM 流式调用失败: {exc}") from exc


_llm_client: LLMClient | None = None


def get_llm_client() -> LLMClient:
    """获取 LLM 客户端单例。"""
    global _llm_client
    if _llm_client is None:
        _llm_client = LLMClient()
    return _llm_client
