"""
Agent 运行时核心模块。

实现 ReAct Loop 骨架，协调 LLM 推理与工具调用，
以 AsyncGenerator 方式逐步产出 StreamEvent，驱动 SSE 流式输出。
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
from app.prompts import ENERGY_SAVING_PROMPT_APPENDIX
from app.services.database import get_session_factory
from app.services.llm_client import (
    LLMError,
    get_llm_client,
    inspect_tool_call_content,
)
from app.tools.registry import tool_registry

logger = get_logger("agent_runner")

_IDLE_TIMEOUT_SEC = 20  # 连续 N 秒无新 chunk 才认为流挂起

# 上下文长度限制配置
_MAX_TOOL_RESULT_CHARS = 5000  # 单个工具结果最大字符数
_MAX_TOTAL_CONTEXT_CHARS = 50000  # 总上下文最大字符数（约 15k tokens）


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
    current_date = datetime.now().strftime("%Y-%m-%d")
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    system_prompt = settings.agent_system_prompt + ENERGY_SAVING_PROMPT_APPENDIX.format(
        current_date=current_date,
        yesterday=yesterday,
    )

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
    return [{"role": "system", "content": _build_system_prompt(context_text)}, *other_messages]


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


def _estimate_context_length(messages: list[dict]) -> int:
    """估算上下文字符数。"""
    total = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            total += len(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, str):
                    total += len(part)
                elif isinstance(part, dict) and part.get("text"):
                    total += len(part["text"])
    return total


def _prune_old_messages(messages: list[dict], max_chars: int = _MAX_TOTAL_CONTEXT_CHARS) -> list[dict]:
    """清理旧消息，保持上下文在限制内。

    保留：system 消息 + 最近几轮对话
    删除：旧的 tool 结果
    """
    if _estimate_context_length(messages) <= max_chars:
        return messages

    # 分离 system 消息和其他消息
    system_msg = None
    other_msgs = []
    for msg in messages:
        if msg.get("role") == "system":
            system_msg = msg
        else:
            other_msgs.append(msg)

    # 从旧到新遍历，删除 tool 消息直到满足限制
    pruned_msgs = []
    for msg in other_msgs:
        pruned_msgs.append(msg)

    # 反向遍历，将旧的 tool 消息替换为简短摘要
    tool_msg_count = 0
    for i in range(len(pruned_msgs) - 1, -1, -1):
        if _estimate_context_length([system_msg] + pruned_msgs) <= max_chars:
            break
        msg = pruned_msgs[i]
        if msg.get("role") == "tool":
            # 将旧的 tool 消息替换为简短提示
            tool_msg_count += 1
            pruned_msgs[i] = {
                "role": "tool",
                "tool_call_id": msg.get("tool_call_id", ""),
                "content": "[历史工具结果已清理]",
            }

    result = [system_msg] + pruned_msgs if system_msg else pruned_msgs
    return result


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
    """执行单个工具调用，自动注入 db 上下文。

    Args:
        tool_name: 工具名称。
        tool_args: 工具参数字典（LLM 提供）。
        db: 数据库会话，若工具需要 db 参数则自动注入。

    Returns:
        工具执行结果字典。
    """
    try:
        func = tool_registry.get_tool(tool_name)

        # 检查工具函数签名，判断是否需要 db 参数
        sig = inspect.signature(func)
        params = list(sig.parameters.keys())

        # 如果工具需要 db 参数，但 LLM 没提供，则注入
        if "db" in params and "db" not in tool_args:
            if db is not None:
                tool_args = {**tool_args, "db": db}
            else:
                return {
                    "tool": tool_name,
                    "error": "工具需要数据库上下文，但未提供 db 参数",
                }

        # 执行工具调用
        if inspect.iscoroutinefunction(func):
            result = await func(**tool_args)
        else:
            result = func(**tool_args)

        return {"tool": tool_name, "result": result}
    except Exception as exc:
        logger.error("工具执行失败: tool=%s, error=%s", tool_name, exc, exc_info=True)
        return {"tool": tool_name, "error": str(exc)}


async def run_agent_stream(messages: list[dict[str, Any]]) -> AsyncGenerator[StreamEvent, None]:
    """运行 ReAct Loop Agent，以流式方式产出事件。

    ReAct Loop 流程：
    1. 验证并确保 messages 包含 system_prompt
    2. 循环调用 LLM，判断是否需要工具调用
    3. 若有 tool_calls，并发执行所有工具，将结果追加到 messages
    4. 若 LLM 返回最终回答，输出 final_answer 事件并终止
    5. 达到最大步数时强制终止

    Args:
        messages: 对话消息列表，OpenAI 格式。运行时会统一重建 system 消息。

    Yields:
        StreamEvent: 每个执行步骤对应的流式事件。
    """
    settings = get_settings()
    max_steps: int = settings.agent_max_steps

    messages = _normalize_messages_with_system_prompt(messages)
    
    steps: int = 0
    tools: list[dict[str, Any]] = tool_registry.get_tools_for_llm()

    try:
        while steps < max_steps:
            steps += 1
            logger.info("Agent 第 %d 步开始", steps)

            # 清理旧消息，保持上下文在限制内
            messages[:] = _prune_old_messages(messages)
            logger.debug("messages: %s", messages)
            yield StreamEvent(
                event_type="agent_step",
                data={"step": steps, "max_steps": max_steps},
            )

            collected_content = ""
            collected_tool_calls: dict[int, dict] = {}
            finish_reason: str | None = None
            _stream_fallback_needed = False
            suspected_tool_draft = False
            suspected_tool_name: str | None = None

            try:
                async for chunk in _stream_with_idle_timeout(
                    get_llm_client().stream_chat(
                        messages=messages,
                        tools=tools or None,
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
                            yield StreamEvent(event_type="token", data=token_text)

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
                # 30秒无新 chunk → DeepSeek 流式 API 挂起，降级为非流式
                logger.warning("流式调用挂起（第 %d 步，已等待%d秒），降级为非流式调用", steps, _IDLE_TIMEOUT_SEC)
                _stream_fallback_needed = True
            except LLMError as exc:
                # 流式中途连接断开 → 同样降级，比报错更可靠
                logger.warning("流式调用中断（第 %d 步）: %s，降级为非流式调用", steps, exc)
                _stream_fallback_needed = True

            if _stream_fallback_needed:
                try:
                    fallback_resp = await get_llm_client().chat(
                        messages=messages, tools=tools or None
                    )
                    fb_choice = (fallback_resp.get("choices") or [{}])[0]
                    finish_reason = fb_choice.get("finish_reason")
                    fb_msg = fb_choice.get("message") or {}
                    fb_content = fb_msg.get("content") or ""
                    fb_tool_calls = fb_msg.get("tool_calls") or []

                    # 【关键修复】输出降级后获取的内容（扣除已输出的部分）
                    if fb_content and len(fb_content) > len(collected_content):
                        remaining = fb_content[len(collected_content):]
                        yield StreamEvent(event_type="token", data=remaining)
                        collected_content = fb_content

                    if fb_tool_calls:
                        collected_tool_calls = {i: tc for i, tc in enumerate(fb_tool_calls)}
                    else:
                        inspection = inspect_tool_call_content(fb_content)
                        suspected_tool_draft = inspection.suspected_draft
                        suspected_tool_name = inspection.suspected_tool_name or suspected_tool_name
                    logger.info("降级非流式调用成功（第 %d 步）: finish=%s", steps, finish_reason)
                except LLMError as retry_exc:
                    logger.error("降级非流式调用也失败（第 %d 步）: %s", steps, retry_exc)
                    yield StreamEvent(event_type="error", data=str(retry_exc))
                    return

            tool_calls: list[dict[str, Any]] | None = (
                list(collected_tool_calls.values()) if collected_tool_calls else None
            )

            if not tool_calls and collected_content and not suspected_tool_draft and settings.llm_tool_call_fallback_enabled:
                inspection = inspect_tool_call_content(collected_content)
                if inspection.tool_calls:
                    tool_calls = inspection.tool_calls
                    logger.info("从 <tool> 标签中提取到 %d 个工具调用", len(tool_calls))
                elif inspection.suspected_draft:
                    suspected_tool_draft = True
                    suspected_tool_name = inspection.suspected_tool_name or suspected_tool_name
                    logger.warning(
                        "检测到疑似工具调用草稿（第 %d 步）: tool=%s",
                        steps,
                        suspected_tool_name or "unknown",
                    )

            assistant_message: dict[str, Any] = {
                "role": "assistant",
                "content": collected_content or None,
            }
            if tool_calls:
                assistant_message["tool_calls"] = tool_calls

            messages.append(assistant_message)

            if finish_reason == "stop" and not tool_calls:
                if suspected_tool_draft and settings.llm_tool_call_retry_on_suspected_draft:
                    logger.warning(
                        "疑似工具调用草稿触发非流式补救（第 %d 步）: tool=%s",
                        steps,
                        suspected_tool_name or "unknown",
                    )
                    try:
                        retry_resp = await get_llm_client().chat(messages=messages, tools=tools or None)
                        retry_choice = (retry_resp.get("choices") or [{}])[0]
                        retry_finish_reason = retry_choice.get("finish_reason")
                        retry_msg = retry_choice.get("message") or {}
                        retry_tool_calls = retry_msg.get("tool_calls") or []
                        retry_content = retry_msg.get("content") or ""

                        if retry_tool_calls:
                            tool_calls = retry_tool_calls
                            finish_reason = retry_finish_reason or finish_reason
                            assistant_message["tool_calls"] = tool_calls
                            assistant_message["content"] = retry_content or None
                            messages[-1] = assistant_message
                            logger.info("疑似工具草稿补救成功（第 %d 步）: tool_calls=%d", steps, len(tool_calls))
                        else:
                            clean_content = _sanitize_tool_draft_text(retry_content or collected_content)
                            fallback_message = clean_content or "本次工具调用未生成有效参数，请重试。"
                            logger.warning("疑似工具草稿补救失败（第 %d 步），返回安全提示", steps)
                            yield StreamEvent(event_type="final_answer", data=fallback_message)
                            return
                    except LLMError as retry_exc:
                        logger.error("疑似工具草稿补救异常（第 %d 步）: %s", steps, retry_exc)
                        yield StreamEvent(event_type="final_answer", data="本次工具调用未生成有效参数，请重试。")
                        return

                if not tool_calls:
                    clean_content = _sanitize_tool_draft_text(collected_content)
                    logger.info("Agent 最终回答（第 %d 步）: %s", steps, clean_content[:200])
                    yield StreamEvent(event_type="final_answer", data=clean_content)
                    return

            if tool_calls:
                parsed_calls: list[tuple[str, dict[str, Any]]] = []

                for tc in tool_calls:
                    tool_name = (tc.get("function") or {}).get("name") or "unknown"
                    is_valid, tool_args, error_message = _is_valid_tool_call(tc, tools)
                    if not is_valid or tool_args is None:
                        logger.warning("拦截非法工具调用（第 %d 步）: %s", steps, error_message)
                        yield StreamEvent(
                            event_type="final_answer",
                            data=error_message or f"工具 {tool_name} 的参数无效，请重试。",
                        )
                        return

                    call_info: dict[str, Any] = {"tool": tool_name, "args": tool_args}
                    parsed_calls.append((tool_name, tool_args))

                    yield StreamEvent(event_type="tool_call", data=call_info)

                # 【核心补丁】创建数据库会话并注入到需要 db 的工具
                results: list[dict[str, Any]] = []
                session_factory = get_session_factory()
                async with session_factory() as db:
                    async with asyncio.TaskGroup() as tg:
                        tasks = [
                            tg.create_task(_execute_tool(name, args, db=db))
                            for name, args in parsed_calls
                        ]
                    results = [t.result() for t in tasks]

                for idx, result in enumerate(results):
                    yield StreamEvent(event_type="tool_result", data=result)

                    tc_id: str = tool_calls[idx].get("id", f"call_{idx}")
                    # 截断工具结果，防止上下文过长
                    truncated_content = _truncate_tool_result(result)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "content": truncated_content,
                    })

                logger.info("第 %d 步工具调用完成，共 %d 个工具", steps, len(results))

                if _is_direct_answer_eligible(results):
                    direct_answer = _extract_direct_answer_from_tool_result(results[0])
                    if direct_answer:
                        logger.info("第 %d 步命中工具结果直出: tool=%s, answer_len=%d", steps, results[0].get("tool"), len(direct_answer))
                        yield StreamEvent(event_type="final_answer", data=direct_answer)
                        return

                if steps == max_steps:
                    logger.warning("达到最大步数 %d，强制终止", max_steps)
                    final_collected = ""
                    _max_step_fallback = False
                    try:
                        async for chunk in _stream_with_idle_timeout(
                            get_llm_client().stream_chat(
                                messages=messages,
                                tools=None,
                            )
                        ):
                            choices = chunk.get("choices") or []
                            if not choices:
                                continue
                            delta = choices[0].get("delta", {})
                            if delta.get("content"):
                                token_text = delta["content"]
                                final_collected += token_text
                                yield StreamEvent(event_type="token", data=token_text)
                    except (asyncio.TimeoutError, LLMError) as exc:
                        logger.warning("最大步数强制汇总流式失败: %s，降级为非流式", exc)
                        _max_step_fallback = True
                    if _max_step_fallback:
                        try:
                            fb = await get_llm_client().chat(messages=messages, tools=None)
                            fb_content = ((fb.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
                            # 【关键修复】输出降级后获取的内容（扣除已输出的部分）
                            if fb_content and len(fb_content) > len(final_collected):
                                remaining = fb_content[len(final_collected):]
                                yield StreamEvent(event_type="token", data=remaining)
                                final_collected = fb_content
                        except LLMError:
                            pass
                    yield StreamEvent(event_type="final_answer", data=final_collected or "已达到最大推理步数，请重新提问。")
                    return

                continue

            if collected_content:
                logger.info("Agent 返回文本回答（第 %d 步）", steps)
                yield StreamEvent(event_type="final_answer", data=collected_content)
                return

        logger.warning("Agent 循环结束，未产生最终回答")
        yield StreamEvent(event_type="final_answer", data="已达到最大推理步数，请重新提问。")

    except Exception as exc:
        logger.error("Agent 运行异常: %s", exc, exc_info=True)
        yield StreamEvent(event_type="error", data=f"Agent 运行异常: {exc}")
