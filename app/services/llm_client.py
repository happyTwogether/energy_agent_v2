"""
LLM 调用适配层。

封装 LiteLLM，提供同步调用和流式调用两种模式，
屏蔽底层 LLM Provider 差异。Agent Runner 只能通过本模块调用 LLM。
"""

import json
import re
import time
from dataclasses import dataclass
from typing import Any, AsyncGenerator

import litellm

# 禁用 LiteLLM 的回调上报和网络请求（版本检查、使用统计等）
litellm.success_callback = []
litellm.failure_callback = []
litellm.set_verbose = False

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger("llm_client")


@dataclass(slots=True)
class ToolCallContentInspection:
    """文本工具调用检查结果。"""

    tool_calls: list[dict[str, Any]] | None = None
    suspected_draft: bool = False
    suspected_tool_name: str | None = None


_SUSPECTED_TOOL_PATTERNS = [
    re.compile(r'```json[\s\S]*?"name"\s*:\s*"(?P<tool>[^"]+)"[\s\S]*?"arguments"\s*:', re.IGNORECASE),
    re.compile(r'"name"\s*:\s*"(?P<tool>[^"]+)"[\s\S]*?"arguments"\s*:', re.IGNORECASE),
    re.compile(r'调用\s*`?(?P<tool>[a-zA-Z_][\w]*)`?\s*工具'),
    re.compile(r'使用\s*`?(?P<tool>[a-zA-Z_][\w]*)`?\s*工具'),
    # Qwen 口语化工具调用兜底：已调用 tool_name，参数：{...}
    re.compile(r'已调用\s+(?P<tool>[a-zA-Z_]\w*)\s*[，,]\s*参数', re.IGNORECASE),
]


def _looks_like_incomplete_json(content: str) -> bool:
    """判断文本是否像未闭合的 JSON 草稿。"""
    text = content.strip()
    if not text:
        return False
    return (
        '"name"' in text
        and '"arguments"' in text
        and text.count("{") > text.count("}")
    )


def inspect_tool_call_content(content: str | None) -> ToolCallContentInspection:
    """检查文本中是否包含合法或疑似的工具调用内容。"""
    if not content:
        return ToolCallContentInspection()

    extracted = _extract_tool_calls_from_content(content)
    if extracted:
        return ToolCallContentInspection(tool_calls=extracted)

    suspected_tool_name: str | None = None
    for pattern in _SUSPECTED_TOOL_PATTERNS:
        match = pattern.search(content)
        if match:
            suspected_tool_name = match.groupdict().get("tool")
            break

    suspected_draft = bool(suspected_tool_name) or _looks_like_incomplete_json(content)
    return ToolCallContentInspection(
        suspected_draft=suspected_draft,
        suspected_tool_name=suspected_tool_name,
    )


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

    # Qwen 兼容：识别 "已调用 tool_name，参数：{...}" 格式
    # 部分模型（如 Qwen3.6）倾向输出口语化工具调用而非 <tool> 标签或原生 FC
    pattern_natural = r'(?:已)?调用\s+([a-zA-Z_]\w*)\s*(?:工具)?\s*[，,]\s*参数\s*[：:]\s*(\{[\s\S]*?\})\s*$'
    for match in re.findall(pattern_natural, content, re.MULTILINE):
        tool_name, args_str = match
        try:
            tool_args = json.loads(args_str)
            if isinstance(tool_args, dict) and tool_args:
                tool_calls.append({
                    "id": f"call_{len(tool_calls)}",
                    "type": "function",
                    "function": {
                        "name": tool_name,
                        "arguments": json.dumps(tool_args, ensure_ascii=False),
                    },
                })
        except json.JSONDecodeError:
            continue

    return tool_calls if tool_calls else None


def _normalize_response(response: dict[str, Any]) -> dict[str, Any]:
    """将响应中的 <tool> 标签转换为标准 tool_calls 格式。"""
    choices = response.get("choices") or []
    if not choices:
        return response

    choice = choices[0]
    message = choice.get("message") or choice.get("delta") or {}
    content = message.get("content")

    # 如果已经有标准 tool_calls，直接返回
    if message.get("tool_calls"):
        return response

    if content:
        inspection = inspect_tool_call_content(content)
        if inspection.tool_calls:
            logger.debug("从响应中提取到 %d 个工具调用", len(inspection.tool_calls))
            message["tool_calls"] = inspection.tool_calls
            message["content"] = None
            if "message" in choice:
                choice["message"] = message
            else:
                choice["delta"] = message
            response["choices"] = choices
        elif inspection.suspected_draft and get_settings().llm_tool_call_debug_log:
            logger.warning(
                "检测到疑似工具调用草稿但未形成合法 tool_calls: tool=%s",
                inspection.suspected_tool_name or "unknown",
            )

    return response


class LLMError(Exception):
    """LLM 调用失败时抛出的自定义异常。"""
    pass


class LLMClient:
    """LLM 客户端，封装 LiteLLM 的调用细节。"""

    def __init__(self, model: str = "", api_key: str = "", base_url: str = "") -> None:
        """初始化 LLM 客户端，可指定 override 参数，不填则从全局配置加载。"""
        settings = get_settings()
        self.model: str = model or settings.default_model
        self.api_key: str = api_key or settings.llm_api_key
        self.base_url: str = base_url or settings.llm_base_url

    def _build_kwargs(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        stream: bool = False,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        """构建 LiteLLM acompletion 调用参数。"""
        settings = get_settings()
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "api_key": self.api_key,
            "max_tokens": max_tokens if max_tokens is not None else settings.llm_max_tokens,
            "temperature": settings.llm_temperature,
            "timeout": 180,
        }
        if self.base_url:
            kwargs["api_base"] = self.base_url
        if stream:
            kwargs["stream"] = True
            kwargs["stream_options"] = {"include_usage": True}
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        # DeepSeek Context Caching: 通过 extra_headers 启用自动前缀缓存
        # 对相同 system+tools 前缀的后续请求可显著降低 TTFT
        if "deepseek" in self.model.lower():
            kwargs.setdefault("extra_headers", {})
            kwargs["extra_headers"]["X-DeepSeek-Cache"] = "enabled"
        if not settings.llm_enable_thinking:
            kwargs["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}
        return kwargs

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        """调用 LLM 获取完整响应。"""
        try:
            logger.info("调用 LLM chat: model=%s", self.model)
            response = await litellm.acompletion(**self._build_kwargs(messages, tools, max_tokens=max_tokens))
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
        *,
        max_tokens: int | None = None,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """流式调用 LLM，逐块产出响应。"""
        try:
            # 计算输入规模用于诊断
            input_text = json.dumps(messages, ensure_ascii=False)
            tools_text = json.dumps(tools, ensure_ascii=False) if tools else ""
            total_chars = len(input_text) + len(tools_text)
            # 粗略估算: 中英文混合约 2 字符/token (保守估计), 中文为主约 1.5 字符/token
            estimated_tokens = total_chars // 2
            t0 = time.time()
            logger.info("LLM stream_chat 开始: model=%s, input_chars=%d, estimated_tokens~%d",
                        self.model, total_chars, estimated_tokens)
            response = await litellm.acompletion(**self._build_kwargs(messages, tools, stream=True, max_tokens=max_tokens))
            chunk_count = 0
            collected_content = ""
            api_usage: dict[str, Any] | None = None
            settings = get_settings()

            async for chunk in response:
                chunk_count += 1
                # 提取真实 API usage（流式最后一个 chunk 包含 usage）
                chunk_usage = getattr(chunk, "usage", None)
                if chunk_usage is not None:
                    api_usage = chunk_usage.model_dump() if hasattr(chunk_usage, "model_dump") else chunk_usage
                choices = chunk.choices or []

                if choices:
                    choice = choices[0]
                    delta = choice.delta
                    content = delta.content if delta else None
                    if content:
                        collected_content += content

                    # 检查是否是结束 chunk，尝试提取工具调用
                    finish_reason = choice.finish_reason
                    if finish_reason and collected_content and tools and settings.llm_tool_call_fallback_enabled:
                        inspection = inspect_tool_call_content(collected_content)
                        if inspection.tool_calls:
                            logger.info("流式响应中提取到 %d 个工具调用", len(inspection.tool_calls))
                            for i, tc in enumerate(inspection.tool_calls):
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
                            yield {
                                "choices": [{
                                    "index": 0,
                                    "delta": {},
                                    "finish_reason": "tool_calls"
                                }]
                            }
                            continue
                        if inspection.suspected_draft:
                            if settings.llm_tool_call_debug_log:
                                logger.warning(
                                    "流式响应检测到疑似工具调用草稿: tool=%s",
                                    inspection.suspected_tool_name or "unknown",
                                )
                            yield {
                                "choices": [{
                                    "index": 0,
                                    "delta": {
                                        "role": "assistant",
                                        "content": None,
                                        "metadata": {
                                            "suspected_tool_draft": True,
                                            "suspected_tool_name": inspection.suspected_tool_name,
                                        },
                                    },
                                    "finish_reason": finish_reason,
                                }]
                            }
                            continue

                yield chunk.model_dump()

            elapsed = time.time() - t0
            output_chars = len(collected_content)
            output_estimated = output_chars // 2
            if api_usage:
                logger.info(
                    "LLM stream_chat 完成: chunks=%d, elapsed=%.1fs, "
                    "api_usage={prompt_tokens=%s, completion_tokens=%s, total_tokens=%s}, "
                    "output_chars=%d, output_estimated~%d",
                    chunk_count, elapsed,
                    api_usage.get("prompt_tokens", "?"),
                    api_usage.get("completion_tokens", "?"),
                    api_usage.get("total_tokens", "?"),
                    output_chars, output_estimated,
                )
            else:
                logger.info(
                    "LLM stream_chat 完成: chunks=%d, elapsed=%.1fs, "
                    "input_estimated~%d, output_chars=%d, output_estimated~%d",
                    chunk_count, elapsed, estimated_tokens, output_chars, output_estimated,
                )
        except Exception as exc:
            logger.error("LLM stream_chat 调用失败: %s", exc, exc_info=True)
            raise LLMError(f"LLM 流式调用失败: {exc}") from exc


_llm_client: LLMClient | None = None


def get_llm_client() -> LLMClient:
    """获取 LLM 客户端单例（意图识别模型）。"""
    global _llm_client
    if _llm_client is None:
        _llm_client = LLMClient()
    return _llm_client


_summary_llm_client: LLMClient | None = None


def get_summary_llm_client() -> LLMClient:
    """获取总结模型 LLM 客户端。

    使用 summary_model / summary_api_key / summary_base_url 配置，
    未配置时回退到主模型配置。
    """
    global _summary_llm_client
    if _summary_llm_client is None:
        settings = get_settings()
        _summary_llm_client = LLMClient(
            model=settings.summary_model,
            api_key=settings.summary_api_key,
            base_url=settings.summary_base_url,
        )
    return _summary_llm_client
