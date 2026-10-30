"""统一任务条件驱动追问；模型边界负责总预算和协议校验。"""

import asyncio
import inspect
import json
import re
import time
import uuid
from contextlib import asynccontextmanager

import jsonschema
from agentscope.message import SystemMsg, TextBlock, ToolCallBlock, ToolResultState
from agentscope.event import ModelCallEndEvent, ToolResultEndEvent
from agentscope.middleware import MiddlewareBase
from agentscope.model import ChatResponse, ChatUsage

from app.agent.errors import ModelDeadlineExceeded, ModelProtocolError, ToolArgumentsProtocolError
from app.agent.context import current_task, update_task
from app.agent.middleware import is_general_guidance_text
from app.agent.toolkit import GENERAL_GUIDANCE_TOOL_NAME
from app.core.logging import get_logger
from app.services.query_scope import TaskContext, ToolRequestContext

logger = get_logger("execution_policy")
_PROTOCOL_TEXT = re.compile(r"<\s*(?:tool_call|function(?:\s|=|>)|parameter(?:\s|=|>))", re.IGNORECASE)


class ExecutionPolicyMiddleware(MiddlewareBase):
    """所有工具共用同一执行入口；明确条件补丁无需重新调用模型理解。"""

    def __init__(self, request_context: ToolRequestContext, model_budget_seconds: float):
        self._request = request_context
        self._remaining = model_budget_seconds
        self._planned = False
        self._rejected_usage = []

    async def on_reply(self, agent, input_kwargs, next_handler):
        task = self._request.task
        if task != current_task(agent.state) or self._request.is_parameter_followup:
            update_task(agent.state, task, status="in_progress", outcome="running")
        async for event in next_handler(**input_kwargs):
            if isinstance(event, ModelCallEndEvent) and self._rejected_usage:
                peak = max([event.input_tokens] + [usage.input_tokens for usage in self._rejected_usage])
                event = event.model_copy(update={
                    "input_tokens": event.input_tokens + sum(usage.input_tokens for usage in self._rejected_usage),
                    "output_tokens": event.output_tokens + sum(usage.output_tokens for usage in self._rejected_usage),
                    "metadata": {**event.metadata, "peak_attempt_input_tokens": peak},
                })
                self._rejected_usage.clear()
            if isinstance(event, ToolResultEndEvent):
                if event.state == ToolResultState.SUCCESS and "task_context" in event.metadata:
                    update_task(agent.state, TaskContext.restore(event.metadata["task_context"]), status="completed", outcome="success")
                elif event.state == ToolResultState.ERROR:
                    update_task(agent.state, task, status="pending", outcome="requires_clarification" if event.metadata.get("clarification") else "failed")
            yield event

    @asynccontextmanager
    async def _model_budget(self):
        if self._remaining <= 0:
            raise ModelDeadlineExceeded()
        started = time.perf_counter()
        deadline = asyncio.timeout(self._remaining)
        try:
            async with deadline:
                yield
            if deadline.expired():
                raise ModelDeadlineExceeded()
        except TimeoutError as exc:
            raise ModelDeadlineExceeded() from exc
        finally:
            self._remaining -= time.perf_counter() - started

    async def on_compress_context(self, agent, input_kwargs, next_handler):
        if await self._can_answer_fixed_guidance(agent):
            # 此轮没有远程推理，不为一句问候额外请求摘要模型；下轮业务照常压缩。
            logger.info("context_compression_deferred reason=local_guidance")
            return
        # 框架压缩直接调用模型，不经过 on_model_call，同样纳入统一等待预算。
        started, previous_summary = time.perf_counter(), agent.state.summary
        try:
            async with self._model_budget():
                await next_handler(**input_kwargs)
        finally:
            logger.info("context_compression_check elapsed_ms=%.2f summary_changed=%s", (time.perf_counter() - started) * 1000, agent.state.summary != previous_summary)

    async def on_model_call(self, agent, input_kwargs, next_handler):
        choice = input_kwargs.get("tool_choice")
        if (
            not self._planned and getattr(choice, "mode", None) == GENERAL_GUIDANCE_TOOL_NAME
            and await self._can_answer_fixed_guidance(agent)
        ):
            self._planned = True
            logger.info("fixed_guidance_local tool=%s remote_model_calls=0", GENERAL_GUIDANCE_TOOL_NAME)
            return ChatResponse(
                content=[ToolCallBlock(id=f"guidance-{uuid.uuid4().hex}", name=GENERAL_GUIDANCE_TOOL_NAME, input="{}")],
                is_last=True, usage=ChatUsage(input_tokens=0, output_tokens=0, time=0),
            )
        task = self._request.task
        if task and self._request.is_parameter_followup and not self._planned:
            self._planned = True
            schemas = await agent.toolkit.get_tool_schemas()
            schema = next((item["function"]["parameters"] for item in schemas if item["function"]["name"] == task.tool_name), None)
            if schema is not None:
                arguments = dict(task.arguments)
                try:
                    if not set(arguments).issubset(schema.get("properties", {})):
                        raise jsonschema.ValidationError("unsupported task fields")
                    jsonschema.validate(arguments, schema)
                except jsonschema.ValidationError:
                    # 未知或不完整条件交给同一个模型规划入口，禁止绕过工具 Schema。
                    pass
                else:
                    logger.info("task_context_followup tool=%s fields=%s", task.tool_name, sorted(arguments))
                    return ChatResponse(
                        content=[ToolCallBlock(id=f"context-{uuid.uuid4().hex}", name=task.tool_name, input=json.dumps(arguments, ensure_ascii=False))],
                        is_last=True, usage=ChatUsage(input_tokens=0, output_tokens=0, time=0),
                    )

        kwargs = input_kwargs
        for attempt in range(2):
            chunks, streamed = await self._read_response(next_handler, kwargs)
            _validate_protocol(chunks)
            try:
                _validate_tool_arguments(chunks)
            except ToolArgumentsProtocolError as error:
                complete = _complete_response(chunks)
                if complete and complete.usage:
                    self._rejected_usage.append(complete.usage)
                allowed = {item["function"]["name"] for item in input_kwargs.get("tools", [])}
                logger.warning(
                    "tool_arguments_invalid tool=%s argument_chars=%s reason=%s retry=%s",
                    error.tool_name if error.tool_name in allowed else "unknown",
                    error.argument_chars, error.reason, attempt == 0,
                )
                if attempt:
                    raise
                # 错误响应尚未交给框架，不进入历史；按原问题干净重试一次，不猜参数。
                kwargs = {**input_kwargs, "messages": [*input_kwargs["messages"], SystemMsg(
                    name="system", content="上次原生工具调用的 arguments 格式无效。请根据原问题重新生成标准工具调用；arguments 必须是合法 JSON 对象，不能为空白、数组、null、代码块或工具标签。不要把上次无效参数写入回复。",
                )]}
                continue
            if not streamed:
                return chunks[0]

            async def checked_stream():
                for chunk in chunks:
                    yield chunk

            return checked_stream()

    async def _read_response(self, next_handler, kwargs):
        async with self._model_budget():
            response = await next_handler(**kwargs)
        if not inspect.isasyncgen(response):
            return [response], False
        chunks = []
        try:
            async with self._model_budget():
                async for chunk in response:
                    chunks.append(chunk)
        finally:
            await response.aclose()
        return chunks, True

    async def _can_answer_fixed_guidance(self, agent) -> bool:
        if not is_general_guidance_text(self._request.current_user_text):
            return False
        schemas = await agent.toolkit.get_tool_schemas()
        for item in schemas:
            function = item["function"]
            if function["name"] == GENERAL_GUIDANCE_TOOL_NAME:
                try:
                    jsonschema.validate({}, function["parameters"])
                except jsonschema.ValidationError:
                    return False
                return True
        return False


def _validate_protocol(chunks):
    text = "".join(block.text for chunk in chunks if isinstance(chunk, ChatResponse) for block in chunk.content if isinstance(block, TextBlock))
    if _PROTOCOL_TEXT.search(text):
        raise ModelProtocolError()


def _complete_response(chunks):
    return next((chunk for chunk in reversed(chunks) if isinstance(chunk, ChatResponse) and chunk.is_last), None)


def _validate_tool_arguments(chunks):
    response = _complete_response(chunks)
    if response is None:
        return  # 未正常结束的流由框架现有中断检查处理，不执行部分工具参数。
    for block in response.content:
        if not isinstance(block, ToolCallBlock):
            continue
        try:
            arguments = json.loads(block.input)
        except (json.JSONDecodeError, TypeError):
            raise ToolArgumentsProtocolError(block.name, len(block.input), "invalid_json") from None
        if not isinstance(arguments, dict):
            raise ToolArgumentsProtocolError(block.name, len(block.input), "expected_object")
