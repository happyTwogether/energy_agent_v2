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

    使用大括号计数定位配对 }，正确处理 arguments 内嵌套 JSON 对象。
    """
    if not content:
        return None

    has_tool_tag = "<tool>" in content
    logger.info("_extract_tool: content_len=%d, has_<tool>=%s", len(content), has_tool_tag)
    if not has_tool_tag:
        # 没有 <tool> 标签，打印内容尾部助诊
        logger.info("_extract_tool: 无 <tool> 标签, content_tail=%.300s", content[-300:])

    tool_calls = []
    pos = 0

    while True:
        # 查找 <tool> 标签
        idx = content.find("<tool>", pos)
        if idx == -1:
            break
        logger.info("_extract_tool: 找到 <tool> at pos=%d", idx)
        json_start = idx + len("<tool>")

        # 跳过空白
        while json_start < len(content) and content[json_start] in (' ', '\t', '\n', '\r'):
            json_start += 1
        if json_start >= len(content) or content[json_start] != '{':
            logger.info("_extract_tool: <tool> 后不是 '{', pos=%d, char=%.10s",
                         json_start, content[json_start:json_start+10] if json_start < len(content) else "EOF")
            pos = json_start
            continue

        # 大括号计数，匹配嵌套 JSON
        depth = 0
        json_end = json_start
        while json_end < len(content):
            ch = content[json_end]
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    break
            json_end += 1
        if depth != 0:  # 未闭合，跳过
            logger.info("_extract_tool: JSON 未闭合 at pos=%d, depth=%d, tail=%.100s",
                         json_start, depth, content[json_end:json_end+100])
            pos = json_start
            continue

        json_str = content[json_start:json_end + 1]

        # 检查 </tool> 闭合标签（缺失时仅告警，JSON 完整就接受）
        after = content[json_end + 1:json_end + 30].strip()
        has_close = after.startswith("</tool>")
        if not has_close:
            logger.info("_extract_tool: </tool> 缺失或未到达 at pos=%d, after=%.30s", json_end + 1, after)

        try:
            data = json.loads(json_str)
            if "name" in data and "arguments" in data:
                tool_calls.append({
                    "id": f"call_{len(tool_calls)}",
                    "type": "function",
                    "function": {
                        "name": data["name"],
                        "arguments": json.dumps(data["arguments"], ensure_ascii=False)
                    }
                })
                logger.info("_extract_tool: 成功提取 tool=%s", data["name"])
            else:
                logger.info("_extract_tool: JSON 缺 name/arguments, keys=%s", list(data.keys()))
        except json.JSONDecodeError as e:
            logger.info("_extract_tool: JSON 解析失败 at pos=%d: %s, json=%.200s",
                         json_start, e, json_str[:200])

        pos = json_end + 1

    # 回退：如果没有 <tool> 标签，尝试匹配裸 {"name": "...", "arguments": {...}} JSON
    if not tool_calls:
        bare_pos = 0
        while True:
            # 查找 {"name" 模式（可能跟在其他文字后面）
            idx = content.find('{"name"', bare_pos)
            if idx == -1:
                break
            # 跳过前面的空白行等，回溯找到真正的大括号起始
            json_start = idx
            # 大括号计数
            depth = 0
            json_end = json_start
            while json_end < len(content):
                ch = content[json_end]
                if ch == '{':
                    depth += 1
                elif ch == '}':
                    depth -= 1
                    if depth == 0:
                        break
                json_end += 1
            if depth != 0:
                bare_pos = json_start + 1
                continue

            json_str = content[json_start:json_end + 1]
            try:
                data = json.loads(json_str)
                if "name" in data and "arguments" in data:
                    tool_calls.append({
                        "id": f"call_{len(tool_calls)}",
                        "type": "function",
                        "function": {
                            "name": data["name"],
                            "arguments": json.dumps(data["arguments"], ensure_ascii=False)
                        }
                    })
                    logger.info("_extract_tool: 裸 JSON 提取成功 tool=%s", data["name"])
            except json.JSONDecodeError:
                pass

            bare_pos = json_end + 1

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
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        # DeepSeek Context Caching: 通过 extra_headers 启用自动前缀缓存
        # 对相同 system+tools 前缀的后续请求可显著降低 TTFT
        if "deepseek" in self.model.lower():
            kwargs.setdefault("extra_headers", {})
            kwargs["extra_headers"]["X-DeepSeek-Cache"] = "enabled"
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
            settings = get_settings()

            async for chunk in response:
                chunk_count += 1
                chunk_dict = chunk.model_dump()
                choices = chunk.choices or []

                if choices:
                    choice = choices[0]
                    delta = choice.delta
                    content = delta.content if delta else None
                    if content:
                        collected_content += content

                    # 检查是否是结束 chunk，尝试提取工具调用
                    finish_reason = choice.finish_reason
                    if finish_reason:
                        # qwen 流式 API 不返回 usage，估算注入到 chunk 供下游使用
                        if not chunk_dict.get("usage"):
                            chunk_dict["usage"] = {
                                "prompt_tokens": estimated_tokens,
                                "completion_tokens": len(collected_content) // 2,
                                "total_tokens": estimated_tokens + len(collected_content) // 2,
                            }
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

                yield chunk_dict

            elapsed = time.time() - t0
            logger.info("LLM stream_chat 完成: chunks=%d, elapsed=%.1fs, estimated_tokens~%d",
                        chunk_count, elapsed, estimated_tokens)
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
