"""
Agent 循环引擎 — ReAct 模式的核心实现。

从旧的"意图路由"模式升级为真正的 ReAct 循环：
Think → Act → Observe → (if needed) Think → Act → Observe → Respond

核心变化：
- 旧: LLM 只能选一个工具，选完就结束
- 新: LLM 可以多轮推理，自己决定调几个工具、何时结束

借鉴 Hermes HEX 引擎的状态管理 + OpenClaw 的 Agent 委托思想。
"""

import asyncio
import inspect
import json
import re
from typing import Any, AsyncGenerator

from app.core.config import get_settings
from app.core.json_utils import dumps_decimal
from app.core.logging import get_logger
from app.engine.harness import HarnessEngine, DEFAULT_MAX_STEPS, DEFAULT_TOKEN_BUDGET
from app.governance.tracing import tracer
from app.governance.guardrails import validate_tool_args, is_safe_sql
from app.memory.engine import MemoryEngine
from app.models.schemas import StreamEvent
from app.prompts.builder import prompt_builder
from app.services.database import get_session_factory
from app.services.llm_client import (
    LLMError,
    get_llm_client,
    inspect_tool_call_content,
)
from app.tools.registry import tool_registry

logger = get_logger("agent_loop")

# —— 常量 ——
_IDLE_TIMEOUT_SEC = 20
_MAX_TOOL_RESULT_CHARS = 3000


# ═══════════════════════════════════════════════════════════════
# 工具辅助函数
# ═══════════════════════════════════════════════════════════════

def _sanitize_tool_draft_text(content: str) -> str:
    """清理回答中的工具调用草稿标记。"""
    cleaned = re.sub(r'<tool>\s*\{[\s\S]*?\}\s*</tool>', '', content)
    cleaned = re.sub(r'```json\s*\{[\s\S]*?$', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'\{\s*"name"\s*:\s*"[^"]+"[\s\S]*?$', '', cleaned)
    return cleaned.strip()


def _extract_direct_answer(result: dict[str, Any]) -> str | None:
    """从工具结果中提取可直接返回的最终答案（优化路径）。"""
    payload = result.get("result")
    if not isinstance(payload, dict):
        return None
    if payload.get("success") is False:
        return None
    report = payload.get("report_content")
    if not isinstance(report, str) or not report.strip():
        return None
    answer = report.strip()
    download_url = payload.get("download_url")
    if isinstance(download_url, str) and download_url.strip():
        answer += f"\n\n[点击下载文件]({download_url.strip()})"
    return answer


def _is_single_direct_answer(results: list[dict[str, Any]]) -> bool:
    """单个工具返回了 report_content 时可直接输出。"""
    if len(results) != 1:
        return False
    return _extract_direct_answer(results[0]) is not None


async def _execute_tool(
    tool_name: str,
    tool_args: dict[str, Any],
    db: Any | None = None,
) -> dict[str, Any]:
    """执行单个工具调用。"""
    try:
        func = tool_registry.get_tool(tool_name)
        sig = inspect.signature(func)
        params = list(sig.parameters.keys())

        if "db" in params and "db" not in tool_args:
            if db is not None:
                tool_args = {**tool_args, "db": db}
            else:
                return {"tool": tool_name, "error": "工具需要数据库上下文，但未提供 db 参数"}

        if inspect.iscoroutinefunction(func):
            result = await func(**tool_args)
        else:
            result = await asyncio.to_thread(func, **tool_args)

        return {"tool": tool_name, "result": result}
    except Exception as exc:
        logger.error("工具执行失败: tool=%s, error=%s", tool_name, exc, exc_info=True)
        return {"tool": tool_name, "error": str(exc)}


def _truncate_result(result: Any, max_chars: int = _MAX_TOOL_RESULT_CHARS) -> str:
    """截断工具结果。"""
    content = dumps_decimal(result, ensure_ascii=False)
    if len(content) <= max_chars:
        return content
    return f"{content[:max_chars]}\n...(已截断 {len(content) - max_chars} 个字符)"


# ═══════════════════════════════════════════════════════════════
# LLM 流式调用
# ═══════════════════════════════════════════════════════════════

async def _stream_llm(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    yield_token,
) -> tuple[str, dict[int, dict], str | None, bool, str | None, bool]:
    """单次流式 LLM 调用，带降级保护。

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
        async for chunk in _stream_with_timeout(
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
                        "id": "", "type": "function",
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
        logger.warning("流式调用挂起，降级为非流式")
        fallback_needed = True
    except LLMError as exc:
        logger.warning("流式调用中断: %s，降级为非流式", exc)
        fallback_needed = True

    if fallback_needed:
        try:
            fb = await get_llm_client().chat(messages=messages, tools=tools or None, max_tokens=2000)
            fb_choice = (fb.get("choices") or [{}])[0]
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
        except LLMError as retry_exc:
            raise retry_exc

    return (collected_content, collected_tool_calls, finish_reason,
            suspected_tool_draft, suspected_tool_name, fallback_needed)


async def _stream_with_timeout(agen):
    """对流式生成器施加逐 chunk 空闲超时。"""
    queue: asyncio.Queue = asyncio.Queue(maxsize=1)
    done = asyncio.Event()
    exc_holder: list[BaseException] = []

    async def _producer():
        try:
            async for chunk in agen:
                await queue.put(chunk)
        except BaseException as e:
            exc_holder.append(e)
        finally:
            done.set()
            await queue.put(None)

    task = asyncio.create_task(_producer())
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
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await agen.aclose()


# ═══════════════════════════════════════════════════════════════
# TokenStream 辅助类
# ═══════════════════════════════════════════════════════════════

class _TokenStream:
    """异步可迭代对象 — 封装 asyncio.Queue + Event 模式。"""

    def __init__(self, producer_fn):
        self._producer_fn = producer_fn
        self.result = None

    async def _consume(self):
        token_queue: asyncio.Queue[str] = asyncio.Queue()
        stream_done = asyncio.Event()
        stream_result: list = []
        _stream_error: BaseException | None = None

        async def _producer():
            nonlocal _stream_error
            try:
                result = await self._producer_fn(lambda t: token_queue.put_nowait(t))
                stream_result.append(result)
            except BaseException as e:
                _stream_error = e
            finally:
                stream_done.set()

        task = asyncio.create_task(_producer())
        try:
            while not stream_done.is_set() or not token_queue.empty():
                try:
                    t = await asyncio.wait_for(token_queue.get(), timeout=0.1)
                    yield t
                except asyncio.TimeoutError:
                    continue
            if _stream_error is not None:
                raise _stream_error
            self.result = stream_result[0] if stream_result else None
        finally:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    def __aiter__(self):
        return self._consume()


# ═══════════════════════════════════════════════════════════════
# Agent Engine — 核心 ReAct 循环
# ═══════════════════════════════════════════════════════════════

class AgentEngine:
    """ReAct Agent 循环引擎。

    核心流程：
    1. 加载记忆上下文
    2. 构建 system prompt
    3. ReAct 循环: LLM思考 → 调工具 → 观察结果 → 判断是否继续
    4. 输出最终回答
    """

    def __init__(self) -> None:
        self.harness = HarnessEngine()
        self.memory = MemoryEngine()

    async def run(
        self,
        query: str,
        history: list[dict[str, Any]],
        user_id: str,
        *,
        extra_instructions: str = "",
    ) -> AsyncGenerator[StreamEvent, None]:
        """运行 Agent 主循环。

        Args:
            query: 用户当前提问。
            history: 历史消息列表（不含当前提问）。
            user_id: 用户标识。
            extra_instructions: 额外的指令（注入到 system prompt 中）。

        Yields:
            StreamEvent: 流式事件。
        """
        settings = get_settings()
        tools = tool_registry.get_tools_for_llm()
        self.harness.reset()

        # —— Trace ——
        trace_id = tracer.start_trace(user_id, query[:100])

        # —— 记忆 ——
        self.memory.load_history(history)
        memory_ctx = await self.memory.get_persistent_context(user_id)

        # —— 合并额外指令 ——
        merged_extra = settings.agent_system_prompt
        if extra_instructions:
            merged_extra = f"{merged_extra}\n\n{extra_instructions}" if merged_extra else extra_instructions

        # —— 构建消息 ——
        system_prompt = prompt_builder.build(
            tools=tools,
            memory_context=memory_ctx,
            extra_instructions=merged_extra,
        )
        context_messages = self.memory.get_context_messages()

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            *context_messages,
            {"role": "user", "content": query},
        ]

        # 诊断日志
        tools_json = tool_registry.get_tools_json()
        logger.info(
            "AgentLoop 启动 trace_id=%s system_prompt_len=%d tools=%d",
            trace_id, len(system_prompt), len(tools),
        )
        logger.debug("tools_json (%d chars):\n%s", len(tools_json), tools_json)

        yield StreamEvent(event_type="agent_step", data={"step": 0, "max_steps": self.harness.max_steps})

        # ═══════════════════════════════════════════════════
        # ReAct 主循环
        # ═══════════════════════════════════════════════════
        try:
            while self.harness.can_continue():
                self.harness.step()
                step_num = self.harness.state.step_count
                yield StreamEvent(event_type="agent_step", data={
                    "step": step_num,
                    "max_steps": self.harness.max_steps,
                })

                # ── LLM 思考 ──
                span = tracer.start_span("llm_call", step=step_num)
                try:
                    stream_gen = _TokenStream(
                        producer_fn=lambda cb: _stream_llm(messages, tools, cb),
                    )
                    ait = stream_gen.__aiter__()
                    while True:
                        try:
                            token = await ait.__anext__()
                            yield StreamEvent(event_type="token", data=token)
                        except StopAsyncIteration:
                            break

                    (collected_content, collected_tool_calls, finish_reason,
                     suspected_tool_draft, suspected_tool_name,
                     _) = stream_gen.result
                except LLMError as exc:
                    tracer.end_span(span, error=str(exc))
                    logger.error("LLM 调用失败: %s", exc)
                    yield StreamEvent(event_type="error", data=str(exc))
                    tracer.end_trace(error="llm_failure")
                    return

                tracer.end_span(span, content_len=len(collected_content),
                                tool_calls=len(collected_tool_calls))

                # ── 解析 tool_calls ──
                tool_calls: list[dict[str, Any]] = (
                    list(collected_tool_calls.values()) if collected_tool_calls else None
                )

                # DeepSeek <tool> 标签兼容
                if not tool_calls and collected_content and not suspected_tool_draft \
                        and settings.llm_tool_call_fallback_enabled:
                    inspection = inspect_tool_call_content(collected_content)
                    if inspection.tool_calls:
                        tool_calls = inspection.tool_calls
                    elif inspection.suspected_draft:
                        suspected_tool_draft = True
                        suspected_tool_name = inspection.suspected_tool_name

                # ── 无工具调用 → LLM 认为可以回答了 ──
                if not tool_calls:
                    if suspected_tool_draft and settings.llm_tool_call_retry_on_suspected_draft:
                        # 疑似工具草稿，尝试非流式补救一次
                        logger.warning("疑似工具草稿触发补救: tool=%s", suspected_tool_name or "unknown")
                        try:
                            retry_resp = await get_llm_client().chat(
                                messages=messages, tools=tools or None, max_tokens=2000,
                            )
                            retry_msg = (retry_resp.get("choices") or [{}])[0].get("message") or {}
                            retry_tool_calls = retry_msg.get("tool_calls") or []
                            if retry_tool_calls:
                                tool_calls = retry_tool_calls
                                messages.append({
                                    "role": "assistant",
                                    "content": retry_msg.get("content") or None,
                                    "tool_calls": tool_calls,
                                })
                                # 继续进入工具执行
                            else:
                                clean = _sanitize_tool_draft_text(retry_msg.get("content", "") or collected_content)
                                final_answer = clean or "本次工具调用未生成有效参数，请重试。"
                                yield StreamEvent(event_type="final_answer", data=final_answer)
                                await self.memory.learn_from_turn(user_id, query, final_answer)
                                tracer.end_trace(answer_len=len(final_answer))
                                return
                        except LLMError:
                            yield StreamEvent(event_type="final_answer",
                                              data="本次工具调用未生成有效参数，请重试。")
                            tracer.end_trace(error="tool_draft_retry_failed")
                            return
                    else:
                        # 真正的最终回答
                        clean = _sanitize_tool_draft_text(collected_content)
                        messages.append({"role": "assistant", "content": clean})
                        yield StreamEvent(event_type="final_answer", data=clean)
                        await self.memory.learn_from_turn(user_id, query, clean)
                        tracer.end_trace(answer_len=len(clean), steps=step_num)
                        return

                # ── 执行工具 ──
                # 校验参数
                parsed_calls: list[tuple[str, dict[str, Any]]] = []
                for tc in tool_calls:
                    tool_name = (tc.get("function") or {}).get("name") or "unknown"
                    try:
                        args = json.loads((tc.get("function") or {}).get("arguments", "{}"))
                    except json.JSONDecodeError:
                        yield StreamEvent(event_type="final_answer",
                                          data=f"工具 {tool_name} 的参数不是合法 JSON，请重试。")
                        tracer.end_trace(error="invalid_tool_args_json")
                        return

                    if not isinstance(args, dict) or not args:
                        yield StreamEvent(event_type="final_answer",
                                          data=f"工具 {tool_name} 的参数为空，请重试。")
                        tracer.end_trace(error="empty_tool_args")
                        return

                    # 获取工具 schema 校验必填参数
                    tool_schema = next(
                        (t for t in tools if t.get("function", {}).get("name") == tool_name), None
                    )
                    if tool_schema:
                        required = tool_schema.get("function", {}).get("parameters", {}).get("required", [])
                        is_valid, err = validate_tool_args(tool_name, args, required)
                        if not is_valid:
                            yield StreamEvent(event_type="final_answer", data=err or "参数校验失败")
                            tracer.end_trace(error="tool_args_validation_failed")
                            return

                    # SQL 安全检查
                    if "sql" in str(args).lower() or "query" in tool_name.lower():
                        for val in args.values():
                            if isinstance(val, str) and ("SELECT" in val.upper() or "FROM" in val.upper()):
                                if not is_safe_sql(val):
                                    yield StreamEvent(event_type="final_answer",
                                                      data=f"工具 {tool_name} 的 SQL 包含不安全操作，已拦截。")
                                    tracer.end_trace(error="sql_injection_blocked")
                                    return

                    parsed_calls.append((tool_name, args))

                # 循环检测
                for name, _ in parsed_calls:
                    if self.harness.record_tool_call(name):
                        yield StreamEvent(event_type="final_answer",
                                          data=f"检测到对 {name} 的重复调用，为避免无限循环已终止。请优化提问后重试。")
                        tracer.end_trace(error="loop_detected", tool=name)
                        return

                # 通知前端工具调用
                for idx, (name, args) in enumerate(parsed_calls):
                    yield StreamEvent(event_type="tool_call", data={
                        "tool": name, "args": args,
                        "call_id": tool_calls[idx].get("id", f"call_{idx}"),
                    })

                # 并行执行工具
                results: list[dict[str, Any]] = []
                session_factory = get_session_factory()

                async def _run_with_session(name: str, args: dict) -> dict:
                    async with session_factory() as db:
                        return await _execute_tool(name, args, db=db)

                tool_span = tracer.start_span("tool_execution", count=len(parsed_calls))
                try:
                    async with asyncio.TaskGroup() as tg:
                        tasks = [tg.create_task(_run_with_session(name, args))
                                 for name, args in parsed_calls]
                    results = [t.result() for t in tasks]
                except Exception as exc:
                    tracer.end_span(tool_span, error=str(exc))
                    yield StreamEvent(event_type="error", data=f"工具执行异常: {exc}")
                    tracer.end_trace(error="tool_execution_error")
                    return

                tracer.end_span(tool_span, tool_names=[r.get("tool") for r in results])

                # 输出工具结果
                for idx, result in enumerate(results):
                    tc_id = tool_calls[idx].get("id", f"call_{idx}")
                    yield StreamEvent(event_type="tool_result", data={**result, "call_id": tc_id})
                    truncated = _truncate_result(result)
                    self.harness.consume_tool_result(len(truncated))
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "content": truncated,
                    })

                # 记录 assistant 消息（含 tool_calls）
                messages.append({
                    "role": "assistant",
                    "content": collected_content or None,
                    "tool_calls": tool_calls,
                })

                logger.info("第%d轮工具调用完成，共%d个工具", step_num, len(results))

                # —— 直接答案优化：单工具返回 report_content 时跳过 LLM 总结 ——
                if _is_single_direct_answer(results):
                    direct = _extract_direct_answer(results[0])
                    if direct:
                        logger.info("命中直接答案路径: tool=%s", results[0].get("tool"))
                        yield StreamEvent(event_type="final_answer", data=direct)
                        await self.memory.learn_from_turn(user_id, query, direct)
                        tracer.end_trace(answer_len=len(direct), steps=step_num, direct_answer=True)
                        return

                # —— Token 预算检查 ——
                if self.harness.is_token_exhausted():
                    logger.warning("Token 预算耗尽，强制结束循环")
                    yield StreamEvent(event_type="final_answer",
                                      data="本次查询涉及数据较多，已汇总已获取的信息。如需更详细的分析，请缩小查询范围。")
                    tracer.end_trace(steps=step_num, token_exhausted=True)
                    return

                # —— 继续循环：LLM 在下一轮中判断是继续调工具还是给出最终回答 ——

            # —— 达到 max_steps ——
            logger.warning("达到最大步骤数 %d，强制结束", self.harness.max_steps)
            yield StreamEvent(event_type="final_answer",
                              data="分析步骤已达上限，以下是已收集信息的汇总。如需深入分析，请缩小问题范围。")
            tracer.end_trace(steps=self.harness.state.step_count, max_steps_reached=True)

        except Exception as exc:
            logger.error("Agent 循环异常: %s", exc, exc_info=True)
            yield StreamEvent(event_type="error", data=f"Agent 运行异常: {exc}")
            tracer.end_trace(error=str(exc))


# 全局单例
agent_engine = AgentEngine()
