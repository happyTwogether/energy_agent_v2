"""
Agent 运行时核心模块。

实现单步 Function Calling Router，协调 LLM 推理与工具调用，
以 AsyncGenerator 方式逐步产出 StreamEvent，驱动 SSE 流式输出。

架构说明：
- 业务模式为"意图识别 → 匹配唯一工具 → 执行 → 返回结果"，不需要多步 ReAct Loop
- 工具为自包含重型工具（内部已完成 SQL + 计算 + 表格生成）
- 保留流式降级、tool draft 检测、直接答案提取等容错机制
"""

import asyncio
import inspect
import json
import re
from datetime import datetime, timedelta
from typing import Any, AsyncGenerator, AsyncIterator

from app.core.config import get_settings
from app.core.json_utils import dumps_decimal
from app.core.logging import get_logger
from app.models.schemas import StreamEvent
from app.prompts import ENERGY_SAVING_INTENT_PROMPT, ENERGY_SAVING_SUMMARY_PROMPT
from app.services.database import get_session_factory
from app.services.llm_client import (
    LLMError,
    get_llm_client,
    get_summary_llm_client,
    inspect_tool_call_content,
)
from app.tools.registry import tool_registry

logger = get_logger("agent_runner")

_IDLE_TIMEOUT_SEC = 20  # 连续 N 秒无新 chunk 才认为流挂起
_MAX_TOOL_RESULT_CHARS = 5000  # 单个工具结果最大字符数


def _sanitize_tool_draft_text(content: str) -> str:
    """清理回答中的工具调用草稿。"""
    cleaned = re.sub(r'<tool>\s*\{[\s\S]*?\}\s*</tool>', '', content)
    cleaned = re.sub(r'```json\s*\{[\s\S]*?$', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'\{\s*"name"\s*:\s*"[^"]+"[\s\S]*?$', '', cleaned)
    return cleaned.strip()


def _build_system_prompt(
    context_text: str | None = None,
) -> str:
    """统一构建 system prompt。"""
    settings = get_settings()
    system_prompt = settings.agent_system_prompt + ENERGY_SAVING_INTENT_PROMPT

    if context_text:
        system_prompt += f"\n\n# User Context\n{context_text}"

    return system_prompt


def _normalize_messages_with_system_prompt(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """统一规范消息列表，确保仅由运行时注入 system prompt。"""
    normalized_messages = [dict(message) for message in messages]
    system_message = normalized_messages[0] if normalized_messages and normalized_messages[0].get("role") == "system" else None
    other_messages = normalized_messages[1:] if system_message else normalized_messages
    context_text = None
    if system_message:
        system_content = str(system_message.get("content") or "")
        if system_content.strip():
            context_text = system_content.strip()
    # 诊断日志：压缩前的原始消息
    logger.info("原始消息数=%d (不含 system)", len(other_messages))
    for i, m in enumerate(other_messages):
        c = m.get("content") or ""
        tc = m.get("tool_calls")
        tc_info = f" tool_calls={len(tc)}" if tc else ""
        logger.info("  raw[%d] role=%s len=%d%s preview=%.200s", i, m.get("role"), len(c), tc_info, c)

    other_messages = _compact_history_messages(other_messages)
    return [{"role": "system", "content": _build_system_prompt(context_text)}, *other_messages]


def _compact_history_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """压缩历史消息：user 保留，assistant 精简为工具调用记录，tool 移除。

    目的：防止 LLM 在多轮对话中看到历史工具返回的完整数据后"复用"数据而不调工具。
    """
    compacted: list[dict[str, Any]] = []
    for msg in messages:
        role = msg.get("role")
        content = msg.get("content")
        if role == "user" and content:
            compacted.append(msg)
        elif role == "assistant":
            tool_calls = msg.get("tool_calls")
            if tool_calls:
                summaries = []
                for tc in tool_calls:
                    fn = tc.get("function") or {}
                    summaries.append(
                        f"已调用 {fn.get('name', 'unknown')}，"
                        f"参数：{fn.get('arguments', '{}')}"
                    )
                compacted.append({
                    "role": "assistant",
                    "content": "\n".join(summaries),
                })
            elif content and len(content) < 200:
                # 只保留短消息（错误提示等），长文本（编造报告）移除
                compacted.append(msg)
        # role == "tool" / content 为空 → 跳过

    # 诊断日志：打印压缩后的消息结构
    logger.info("历史消息: before=%d after=%d", len(messages), len(compacted))
    for i, m in enumerate(compacted):
        c = m.get("content") or ""
        tc = m.get("tool_calls")
        tc_info = f" tool_calls={len(tc)}" if tc else ""
        logger.info("  [%d] role=%s len=%d%s preview=%.200s", i, m.get("role"), len(c), tc_info, c)
    return compacted


def _extract_direct_answer_from_tool_result(result: dict[str, Any]) -> str | None:
    """从工具结果中提取可直接返回给用户的最终答案。"""
    payload = result.get("result")
    if not isinstance(payload, dict):
        return None

    if payload.get("success") is False:
        return None

    report_content = payload.get("report_content")
    if not isinstance(report_content, str) or not report_content.strip():
        return None

    direct_answer = report_content.strip()
    download_url = payload.get("download_url")
    if isinstance(download_url, str) and download_url.strip():
        direct_answer += f"\n\n📥 [点击下载文件]({download_url.strip()})"

    return direct_answer


def _is_direct_answer_eligible(results: list[dict[str, Any]]) -> bool:
    """判断工具结果是否适合跳过二次 LLM 总结而直接返回。"""
    if len(results) != 1:
        return False
    return _extract_direct_answer_from_tool_result(results[0]) is not None


def _is_valid_tool_call(tool_call: dict[str, Any], tools: list[dict[str, Any]]) -> tuple[bool, dict[str, Any] | None, str | None]:
    """校验工具调用是否具备合法参数。"""
    function_info = tool_call.get("function") or {}
    tool_name = function_info.get("name")
    if not tool_name:
        return False, None, "工具名缺失"

    try:
        tool_args = json.loads(function_info.get("arguments", "{}"))
    except json.JSONDecodeError:
        return False, None, f"工具 {tool_name} 的参数不是合法 JSON"

    if not isinstance(tool_args, dict) or not tool_args:
        return False, None, f"工具 {tool_name} 的参数为空"

    tool_schema = next((item for item in tools if item.get("function", {}).get("name") == tool_name), None)
    if tool_schema:
        required_fields = tool_schema.get("function", {}).get("parameters", {}).get("required", [])
        missing_fields = [field for field in required_fields if field not in tool_args or tool_args[field] in (None, "")]
        if missing_fields:
            return False, None, f"工具 {tool_name} 缺少必填参数: {', '.join(missing_fields)}"

    return True, tool_args, None


def _truncate_tool_result(result: Any, max_chars: int = _MAX_TOOL_RESULT_CHARS) -> str:
    """截断工具结果，避免单次工具输出撑爆上下文。"""
    content = dumps_decimal(result, ensure_ascii=False)
    if len(content) <= max_chars:
        return content
    truncated = content[:max_chars]
    omitted = len(content) - max_chars
    return f"{truncated}\n...(已截断 {omitted} 个字符)"


async def _stream_with_idle_timeout(
    agen: AsyncIterator[dict[str, Any]],
) -> AsyncGenerator[dict[str, Any], None]:
    """对流式生成器施加逐 chunk 空闲超时。

    仅当两个相邻 chunk 间隔超过 _IDLE_TIMEOUT_SEC 才抛出 asyncio.TimeoutError，
    活跃流无论总时长多长都不会被中断。
    """
    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize=1)
    done_event = asyncio.Event()
    exc_holder: list[BaseException] = []

    async def _producer() -> None:
        try:
            async for chunk in agen:
                await queue.put(chunk)
        except asyncio.CancelledError:
            raise
        except BaseException as e:
            exc_holder.append(e)
        finally:
            done_event.set()
            await queue.put(None)

    producer_task = asyncio.create_task(_producer())
    try:
        while True:
            try:
                chunk = await asyncio.wait_for(queue.get(), timeout=_IDLE_TIMEOUT_SEC)
            except asyncio.TimeoutError:
                raise

            if chunk is None:
                break
            yield chunk

        if exc_holder:
            raise exc_holder[0]
    finally:
        producer_task.cancel()
        try:
            await producer_task
        except asyncio.CancelledError:
            pass
        await agen.aclose()


async def _execute_tool(
    tool_name: str,
    tool_args: dict[str, Any],
    db: Any | None = None,
) -> dict[str, Any]:
    """执行单个工具调用，自动注入 db 上下文。"""
    try:
        func = tool_registry.get_tool(tool_name)

        sig = inspect.signature(func)
        params = list(sig.parameters.keys())

        if "db" in params and "db" not in tool_args:
            if db is not None:
                tool_args = {**tool_args, "db": db}
            else:
                return {
                    "tool": tool_name,
                    "error": "工具需要数据库上下文，但未提供 db 参数",
                }

        if inspect.iscoroutinefunction(func):
            result = await func(**tool_args)
        else:
            result = func(**tool_args)

        return {"tool": tool_name, "result": result}
    except Exception as exc:
        logger.error("工具执行失败: tool=%s, error=%s", tool_name, exc, exc_info=True)
        return {"tool": tool_name, "error": str(exc)}


async def _collect_stream_chunks(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    yield_token,
) -> tuple[str, dict[int, dict], str | None, bool, str | None, bool]:
    """单次 LLM 流式调用，收集 content 和 tool_calls。

    包含流式降级：超时或连接中断时自动切换非流式调用。

    Returns:
        (collected_content, collected_tool_calls, finish_reason,
         suspected_tool_draft, suspected_tool_name, fallback_used)
    """
    collected_content = ""
    collected_tool_calls: dict[int, dict] = {}
    finish_reason: str | None = None
    suspected_tool_draft = False
    suspected_tool_name: str | None = None
    fallback_needed = False

    try:
        async for chunk in _stream_with_idle_timeout(
            get_llm_client().stream_chat(
                messages=messages,
                tools=tools or None,
                max_tokens=2000,
            )
        ):
            choices = chunk.get("choices") or []
            if not choices:
                continue
            choice = choices[0]
            finish_reason = choice.get("finish_reason") or finish_reason
            delta = choice.get("delta", {})

            if delta.get("content"):
                token_text = delta["content"]
                collected_content += token_text
                yield_token(token_text)

            metadata = delta.get("metadata") or {}
            if metadata.get("suspected_tool_draft"):
                suspected_tool_draft = True
                suspected_tool_name = metadata.get("suspected_tool_name") or suspected_tool_name

            for tc_delta in (delta.get("tool_calls") or []):
                idx = tc_delta["index"]
                if idx not in collected_tool_calls:
                    collected_tool_calls[idx] = {
                        "id": "",
                        "type": "function",
                        "function": {"name": "", "arguments": ""},
                    }
                if tc_delta.get("id"):
                    collected_tool_calls[idx]["id"] = tc_delta["id"]
                fn = tc_delta.get("function") or {}
                if fn.get("name"):
                    collected_tool_calls[idx]["function"]["name"] = fn["name"]
                if fn.get("arguments"):
                    collected_tool_calls[idx]["function"]["arguments"] += fn["arguments"]
    except asyncio.TimeoutError:
        logger.warning("流式调用挂起（已等待%d秒），降级为非流式调用", _IDLE_TIMEOUT_SEC)
        fallback_needed = True
    except LLMError as exc:
        logger.warning("流式调用中断: %s，降级为非流式调用", exc)
        fallback_needed = True

    if fallback_needed:
        try:
            fallback_resp = await get_llm_client().chat(
                messages=messages, tools=tools or None,
                max_tokens=2000,
            )
            fb_choice = (fallback_resp.get("choices") or [{}])[0]
            finish_reason = fb_choice.get("finish_reason")
            fb_msg = fb_choice.get("message") or {}
            fb_content = fb_msg.get("content") or ""
            fb_tool_calls = fb_msg.get("tool_calls") or []

            if fb_content and len(fb_content) > len(collected_content):
                remaining = fb_content[len(collected_content):]
                yield_token(remaining)
                collected_content = fb_content

            if fb_tool_calls:
                collected_tool_calls = {i: tc for i, tc in enumerate(fb_tool_calls)}
            else:
                inspection = inspect_tool_call_content(fb_content)
                suspected_tool_draft = inspection.suspected_draft
                suspected_tool_name = inspection.suspected_tool_name or suspected_tool_name
            logger.info("降级非流式调用成功: finish=%s", finish_reason)
        except LLMError as retry_exc:
            raise retry_exc

    return (collected_content, collected_tool_calls, finish_reason,
            suspected_tool_draft, suspected_tool_name, fallback_needed)


async def _stream_llm_summary(
    messages: list[dict[str, Any]],
    yield_token,
) -> str:
    """调用 LLM 对工具结果进行总结（不带 tools，使用总结模型 + 总结 Prompt）。"""
    # 替换 system message 为总结专用 Prompt
    summary_messages = [
        {"role": "system", "content": ENERGY_SAVING_SUMMARY_PROMPT},
        *[m for m in messages if m.get("role") != "system"],
    ]

    summary_client = get_summary_llm_client()
    collected = ""
    fallback_needed = False

    try:
        async for chunk in _stream_with_idle_timeout(
            summary_client.stream_chat(messages=summary_messages, tools=None)
        ):
            choices = chunk.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta", {})
            if delta.get("content"):
                token_text = delta["content"]
                collected += token_text
                yield_token(token_text)
    except (asyncio.TimeoutError, LLMError) as exc:
        logger.warning("LLM 总结流式失败: %s，降级为非流式", exc)
        fallback_needed = True

    if fallback_needed:
        try:
            fb = await summary_client.chat(messages=summary_messages, tools=None)
            fb_content = ((fb.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
            if fb_content and len(fb_content) > len(collected):
                remaining = fb_content[len(collected):]
                yield_token(remaining)
                collected = fb_content
        except LLMError as exc:
            logger.error("LLM 总结降级也失败: %s", exc)
            raise

    return collected


def _extract_tool_calls_from_text(
    content: str,
    settings: Any,
    suspected_tool_draft: bool,
) -> tuple[list[dict[str, Any]] | None, bool, str | None]:
    """从 LLM 文本内容中提取 <tool> 标签中的工具调用（DeepSeek 兼容）。"""
    inspection = inspect_tool_call_content(content)
    if inspection.tool_calls:
        logger.info("从 <tool> 标签中提取到 %d 个工具调用", len(inspection.tool_calls))
        return inspection.tool_calls, suspected_tool_draft, None
    elif inspection.suspected_draft:
        logger.warning("检测到疑似工具调用草稿: tool=%s", inspection.suspected_tool_name or "unknown")
        return None, True, inspection.suspected_tool_name
    return None, suspected_tool_draft, None


async def run_agent_stream(
    messages: list[dict[str, Any]],
    output_messages: list[dict[str, Any]] | None = None,
) -> AsyncGenerator[StreamEvent, None]:
    """运行单步 Function Calling Router，以流式方式产出事件。

    流程：
    1. 构建 system prompt + 工具列表
    2. 单次流式 LLM 调用（含降级保护）
    3. 无 tool_call → 直接输出 LLM 回答
    4. 有 tool_call → 并发执行工具
       - 工具返回 report_content → 跳过 LLM 总结，直接输出
       - 无 report_content → 再调用 LLM 总结后输出

    Args:
        messages: 对话消息列表，OpenAI 格式。运行时会统一重建 system 消息。

    Yields:
        StreamEvent: 流式事件（token / tool_call / tool_result / final_answer / error）
    """
    settings = get_settings()

    messages = _normalize_messages_with_system_prompt(messages)
    tools: list[dict[str, Any]] = tool_registry.get_tools_for_llm()

    # 诊断日志：输出意图识别的 system prompt 和工具列表（使用缓存序列化避免重复 JSON 编码）
    tools_json = tool_registry.get_tools_json()
    system_msg = messages[0]["content"] if messages else ""
    logger.info("意图识别 system_prompt (%d chars):\n%s", len(system_msg), system_msg)
    logger.info("意图识别 tools 数量=%d, tools_json (%d chars):\n%s",
                len(tools), len(tools_json), tools_json)

    # 兼容前端：发送固定 agent_step
    yield StreamEvent(event_type="agent_step", data={"step": 1, "max_steps": 1})

    try:
        # ── Phase 1: 单次 LLM 流式调用（实时流式 yield token）──
        collected_content = ""
        collected_tool_calls: dict[int, dict] = {}
        finish_reason: str | None = None
        suspected_tool_draft = False
        suspected_tool_name: str | None = None

        token_queue: asyncio.Queue[str] = asyncio.Queue()
        stream_done = asyncio.Event()
        stream_result: list = []
        _stream_error: BaseException | None = None

        async def _run_phase1_stream():
            nonlocal _stream_error
            try:
                result = await _collect_stream_chunks(
                    messages, tools,
                    lambda t: token_queue.put_nowait(t),
                )
                stream_result.extend(result)
            except BaseException as e:
                _stream_error = e
            finally:
                stream_done.set()

        stream_task = asyncio.create_task(_run_phase1_stream())

        try:
            while not stream_done.is_set() or not token_queue.empty():
                try:
                    t = await asyncio.wait_for(token_queue.get(), timeout=0.1)
                    yield StreamEvent(event_type="token", data=t)
                except asyncio.TimeoutError:
                    continue

            if _stream_error is not None:
                if isinstance(_stream_error, LLMError):
                    logger.error("LLM 调用失败: %s", _stream_error)
                    yield StreamEvent(event_type="error", data=str(_stream_error))
                    return
                raise _stream_error

            collected_content, collected_tool_calls, finish_reason, \
                suspected_tool_draft, suspected_tool_name, _ = stream_result
        finally:
            if not stream_task.done():
                stream_task.cancel()
                try:
                    await stream_task
                except asyncio.CancelledError:
                    pass

        # ── Phase 2: 从文本中提取 <tool> 标签（DeepSeek 兼容） ──
        tool_calls: list[dict[str, Any]] | None = (
            list(collected_tool_calls.values()) if collected_tool_calls else None
        )

        if not tool_calls and collected_content and not suspected_tool_draft \
                and settings.llm_tool_call_fallback_enabled:
            tool_calls, suspected_tool_draft, suspected_tool_name = \
                _extract_tool_calls_from_text(collected_content, settings, suspected_tool_draft)

        # ── Phase 3: 构建 assistant 消息 ──
        assistant_message: dict[str, Any] = {
            "role": "assistant",
            "content": collected_content or None,
        }
        if tool_calls:
            assistant_message["tool_calls"] = tool_calls
        messages.append(assistant_message)

        # ── Phase 4: 无工具调用 → 处理 tool draft 重试或直接输出 ──
        if not tool_calls:
            if suspected_tool_draft and settings.llm_tool_call_retry_on_suspected_draft:
                logger.warning("疑似工具调用草稿触发非流式补救: tool=%s",
                               suspected_tool_name or "unknown")
                try:
                    retry_resp = await get_llm_client().chat(messages=messages, tools=tools or None, max_tokens=2000)
                    retry_choice = (retry_resp.get("choices") or [{}])[0]
                    retry_msg = retry_choice.get("message") or {}
                    retry_tool_calls = retry_msg.get("tool_calls") or []
                    retry_content = retry_msg.get("content") or ""

                    if retry_tool_calls:
                        tool_calls = retry_tool_calls
                        assistant_message["tool_calls"] = tool_calls
                        assistant_message["content"] = retry_content or None
                        messages[-1] = assistant_message
                        logger.info("疑似工具草稿补救成功: tool_calls=%d", len(tool_calls))
                        # 继续进入 Phase 5
                    else:
                        clean_content = _sanitize_tool_draft_text(retry_content or collected_content)
                        fallback_message = clean_content or "本次工具调用未生成有效参数，请重试。"
                        logger.warning("疑似工具草稿补救失败，返回安全提示")
                        yield StreamEvent(event_type="final_answer", data=fallback_message)
                        return
                except LLMError as retry_exc:
                    logger.error("疑似工具草稿补救异常: %s", retry_exc)
                    yield StreamEvent(event_type="final_answer",
                                      data="本次工具调用未生成有效参数，请重试。")
                    return

            if not tool_calls:
                clean_content = _sanitize_tool_draft_text(collected_content)
                logger.info("Agent 直接回答: %s", clean_content[:200])
                yield StreamEvent(event_type="final_answer", data=clean_content)
                return

        # ── Phase 5: 执行工具调用 ──
        if tool_calls:
            # 校验参数
            parsed_calls: list[tuple[str, dict[str, Any]]] = []
            for tc in tool_calls:
                tool_name = (tc.get("function") or {}).get("name") or "unknown"
                is_valid, tool_args, error_message = _is_valid_tool_call(tc, tools)
                if not is_valid or tool_args is None:
                    logger.warning("拦截非法工具调用: %s", error_message)
                    yield StreamEvent(
                        event_type="final_answer",
                        data=error_message or f"工具 {tool_name} 的参数无效，请重试。",
                    )
                    return

                yield StreamEvent(event_type="tool_call", data={
                    "tool": tool_name, "args": tool_args,
                    "call_id": tc.get("id", f"call_{len(parsed_calls)}"),
                })
                parsed_calls.append((tool_name, tool_args))

            # 并行执行所有工具（每个工具独立 session，真正并发）
            results: list[dict[str, Any]] = []
            session_factory = get_session_factory()

            async def _run_with_session(name: str, args: dict) -> dict:
                async with session_factory() as db:
                    return await _execute_tool(name, args, db=db)

            async with asyncio.TaskGroup() as tg:
                tasks = [
                    tg.create_task(_run_with_session(name, args))
                    for name, args in parsed_calls
                ]
            results = [t.result() for t in tasks]

            # 输出工具结果
            for idx, result in enumerate(results):
                tc_id: str = tool_calls[idx].get("id", f"call_{idx}")
                yield StreamEvent(event_type="tool_result",
                                  data={**result, "call_id": tc_id})
                truncated_content = _truncate_tool_result(result)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "content": truncated_content,
                })

            logger.info("工具调用完成，共 %d 个工具", len(results))

            # 直接答案提取：工具返回 report_content 时跳过 LLM 总结
            if _is_direct_answer_eligible(results):
                direct_answer = _extract_direct_answer_from_tool_result(results[0])
                if direct_answer:
                    logger.info("命中工具结果直出: tool=%s, answer_len=%d",
                                results[0].get("tool"), len(direct_answer))
                    yield StreamEvent(event_type="final_answer", data=direct_answer)
                    return

            # LLM 总结工具结果（实时流式 yield token）
            summary_queue: asyncio.Queue[str] = asyncio.Queue()
            summary_done = asyncio.Event()
            summary_result: list = []
            _summary_error: BaseException | None = None

            async def _run_summary_stream():
                nonlocal _summary_error
                try:
                    text = await _stream_llm_summary(
                        messages,
                        lambda t: summary_queue.put_nowait(t),
                    )
                    summary_result.append(text)
                except BaseException as e:
                    _summary_error = e
                finally:
                    summary_done.set()

            summary_task = asyncio.create_task(_run_summary_stream())

            try:
                while not summary_done.is_set() or not summary_queue.empty():
                    try:
                        t = await asyncio.wait_for(summary_queue.get(), timeout=0.1)
                        yield StreamEvent(event_type="token", data=t)
                    except asyncio.TimeoutError:
                        continue

                if _summary_error is not None:
                    if isinstance(_summary_error, LLMError):
                        logger.exception("LLM 总结失败")
                        yield StreamEvent(event_type="error", data="工具执行完成，但总结生成失败，请重试。")
                        return
                    raise _summary_error

                summary_text = summary_result[0] if summary_result else ""
                yield StreamEvent(event_type="final_answer", data=summary_text or "工具执行完成，但未能生成总结。")
                return
            finally:
                if not summary_task.done():
                    summary_task.cancel()
                    try:
                        await summary_task
                    except asyncio.CancelledError:
                        pass

        # ── Phase 6: 兜底 ──
        if collected_content:
            yield StreamEvent(event_type="final_answer", data=collected_content)
            return

        logger.warning("Agent 未产生最终回答")
        yield StreamEvent(event_type="final_answer", data="未收到有效回答，请重试。")

    except Exception as exc:
        logger.error("Agent 运行异常: %s", exc, exc_info=True)
        yield StreamEvent(event_type="error", data=f"Agent 运行异常: {exc}")
    finally:
        if output_messages is not None:
            output_messages.clear()
            output_messages.extend(
                m for m in messages if m.get("role") != "system" and m.get("content")
            )
