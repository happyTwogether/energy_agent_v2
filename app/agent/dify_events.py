"""AgentScope 公共事件到 Dify Agent 协议的有状态适配器。"""

from collections.abc import AsyncGenerator, AsyncIterable
from dataclasses import dataclass, field
import json
import time
from typing import Any
import httpx
from openai import APITimeoutError

from agentscope.event import (
    AgentEvent,
    ModelCallEndEvent,
    ModelCallStartEvent,
    ReplyEndEvent,
    TextBlockDeltaEvent,
    ThinkingBlockDeltaEvent,
    ToolCallDeltaEvent,
    ToolCallStartEvent,
    ToolResultEndEvent,
    ToolResultStartEvent,
    ToolResultTextDeltaEvent,
)
from agentscope.message import ToolResultState
from agentscope.types import ReplyFinishedReason

from app.core.config import get_settings
from app.agent.context import AgentStateEvent, current_task
from agentscope.state import AgentState
from app.core.json_utils import dumps_decimal
from app.core.logging import get_logger
from app.agent.errors import (
    PUBLIC_INTERNAL_ERROR,
    PUBLIC_INTERRUPTED_ERROR,
    PUBLIC_MAX_ITERS_ERROR,
    PUBLIC_MODEL_ERROR,
    PUBLIC_MODEL_TIMEOUT_ERROR,
    PUBLIC_MODEL_PROTOCOL_ERROR,
    ModelDeadlineExceeded,
    ModelProtocolError,
)


logger = get_logger("dify_events")


@dataclass(slots=True)
class DifyRunResult:
    """一次 AgentScope reply 的最终聚合结果。"""

    answer: str = ""
    usage: dict[str, int] = field(
        default_factory=lambda: {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        },
    )
    error: str | None = None
    completed: bool = False
    task_context: dict[str, Any] | None = None
    agent_state: dict[str, Any] | None = None
    state_version: int = 0
    session_existed: bool = False

    # ── 性能指标 ──
    first_model_token_ms: float | None = None  # 首模型响应(含 tool_call)延迟
    first_text_token_ms: float | None = None   # 首用户可见文本 token 延迟
    total_ms: float = 0.0                      # 端到端耗时
    max_input_tokens: int = 0                  # 单次调用峰值输入 token
    context_size: int = 0                      # 上下文窗口大小快照
    # ── 端到端首 token 延迟（起点=HTTP 请求到达路由入口）──
    e2e_first_model_token_ms: float | None = None
    e2e_first_text_token_ms: float | None = None


class DifyEventAdapter:
    """将 AgentScope 生命周期映射为 Dify streaming/blocking 共用语义。"""

    def __init__(
        self,
        conversation_id: str,
        message_id: str,
        task_id: str,
        created_at: int,
        route_start_perf: float | None = None,
    ) -> None:
        self.conversation_id = conversation_id
        self.message_id = message_id
        self.task_id = task_id
        self.created_at = created_at
        self.result = DifyRunResult()
        self._tool_calls: dict[str, dict[str, Any]] = {}
        self._thought_position = 0

        # ── 性能指标采集 ──
        self._route_start_perf = route_start_perf
        self._start_perf = time.perf_counter()
        self._first_model_token_at: float | None = None
        self._first_text_token_at: float | None = None
        self._max_input_tokens = 0
        self._context_size = get_settings().llm_context_size
        self._model_call_count = 0
        self._model_started_at: float | None = None
        self._model_output_counts = {"text_chars": 0, "thinking_chars": 0, "tool_calls": 0}

    async def stream(
        self,
        events: AsyncIterable[AgentEvent],
    ) -> AsyncGenerator[dict[str, str], None]:
        """消费 AgentEvent，产出 Dify SSE envelope 并更新 ``result``。"""
        yield self._message_envelope("")

        try:
            async for event in events:
                if isinstance(event, AgentStateEvent):
                    self.result.agent_state = event.agent_state
                    self.result.state_version = event.expected_version
                    self.result.session_existed = event.session_existed
                    task = current_task(AgentState.model_validate(event.agent_state))
                    self.result.task_context = task.snapshot() if task else None
                elif isinstance(event, ModelCallStartEvent):
                    self._model_call_count += 1
                    self._model_started_at = time.perf_counter()
                    self._model_output_counts = {"text_chars": 0, "thinking_chars": 0, "tool_calls": 0}
                    logger.info("agent_model_call_start call=%s", self._model_call_count)

                elif isinstance(event, ThinkingBlockDeltaEvent):
                    self._model_output_counts["thinking_chars"] += len(event.delta)

                elif isinstance(event, TextBlockDeltaEvent):
                    self._model_output_counts["text_chars"] += len(event.delta)
                    now = time.perf_counter()
                    if self._first_text_token_at is None:
                        self._first_text_token_at = now
                    if self._first_model_token_at is None:
                        self._first_model_token_at = now
                    self.result.answer += event.delta
                    yield self._message_envelope(event.delta)

                elif isinstance(event, ModelCallEndEvent):
                    self._add_usage(event.input_tokens, event.output_tokens)
                    peak_input = event.metadata.get("peak_attempt_input_tokens", event.input_tokens)
                    if peak_input > self._max_input_tokens:
                        self._max_input_tokens = peak_input
                    elapsed_ms = (
                        round((time.perf_counter() - self._model_started_at) * 1000, 2)
                        if self._model_started_at is not None else 0
                    )
                    logger.info(
                        "agent_model_call_end call=%s elapsed_ms=%s finish=%s input_tokens=%s output_tokens=%s counts=%s",
                        self._model_call_count, elapsed_ms, event.finished_reason,
                        event.input_tokens, event.output_tokens, self._model_output_counts,
                    )

                elif isinstance(event, ToolCallStartEvent):
                    self._model_output_counts["tool_calls"] += 1
                    self._thought_position += 1
                    self._tool_calls[event.tool_call_id] = {
                        "name": event.tool_call_name,
                        "arguments": "",
                        "observation": "",
                        "position": self._thought_position,
                    }

                elif isinstance(event, ToolCallDeltaEvent):
                    if self._first_model_token_at is None:
                        self._first_model_token_at = time.perf_counter()
                    call = self._ensure_tool_call(event.tool_call_id)
                    call["arguments"] += event.delta

                elif isinstance(event, ToolResultStartEvent):
                    call = self._ensure_tool_call(event.tool_call_id)
                    call["name"] = event.tool_call_name

                elif isinstance(event, ToolResultTextDeltaEvent):
                    call = self._ensure_tool_call(event.tool_call_id)
                    call["observation"] += event.delta

                elif isinstance(event, ToolResultEndEvent):
                    if event.state == ToolResultState.SUCCESS and isinstance(event.metadata.get("task_context"), dict):
                        self.result.task_context = event.metadata["task_context"]
                    effective_input = event.metadata.get("effective_input")
                    if isinstance(effective_input, dict):
                        call = self._ensure_tool_call(event.tool_call_id)
                        call["arguments"] = dumps_decimal(effective_input, ensure_ascii=False)
                    yield self._thought_envelope(event.tool_call_id)

                elif isinstance(event, ReplyEndEvent):
                    if event.finished_reason == ReplyFinishedReason.COMPLETED:
                        self.result.completed = True
                        yield self._message_end_envelope()
                    else:
                        message = _public_reply_error(event.finished_reason)
                        logger.warning(
                            "Agent 未正常完成: reason=%s error_type=%s",
                            event.finished_reason,
                            event.error.type if event.error is not None else None,
                        )
                        self.result.error = message
                        yield self._error_envelope(message)

            if not self.result.completed and self.result.error is None:
                logger.warning("AgentScope 事件流在 ReplyEndEvent 前结束")
                self.result.error = PUBLIC_MODEL_ERROR
                yield self._error_envelope(PUBLIC_MODEL_ERROR)

        except (httpx.TimeoutException, APITimeoutError, ModelDeadlineExceeded):
            logger.warning("模型服务响应超时: model_call=%s", self._model_call_count)
            self.result.error = PUBLIC_MODEL_TIMEOUT_ERROR
            yield self._error_envelope(PUBLIC_MODEL_TIMEOUT_ERROR, code="model_timeout", status=504)
        except ModelProtocolError:
            logger.warning("模型工具协议无效: model_call=%s", self._model_call_count)
            self.result.error = PUBLIC_MODEL_PROTOCOL_ERROR
            yield self._error_envelope(PUBLIC_MODEL_PROTOCOL_ERROR, code="model_protocol_error", status=502)
        except Exception:
            logger.exception("AgentScope 事件适配异常")
            message = PUBLIC_INTERNAL_ERROR
            self.result.error = message
            yield self._error_envelope(message)
        finally:
            self._finalize_metrics()

    def error_envelope(self, message: str = PUBLIC_INTERNAL_ERROR, *, code: str = "agent_error", status: int = 500) -> dict[str, str]:
        """构建已脱敏的 Dify error envelope。"""
        self.result.completed = False
        self.result.error = message
        return self._error_envelope(message, code=code, status=status)

    def _add_usage(self, input_tokens: int, output_tokens: int) -> None:
        self.result.usage["prompt_tokens"] += input_tokens
        self.result.usage["completion_tokens"] += output_tokens
        self.result.usage["total_tokens"] = (
            self.result.usage["prompt_tokens"]
            + self.result.usage["completion_tokens"]
        )

    def _finalize_metrics(self) -> None:
        """在事件流结束时统一结算性能指标。"""
        end = time.perf_counter()
        self.result.total_ms = round((end - self._start_perf) * 1000, 2)
        if self._first_model_token_at is not None:
            self.result.first_model_token_ms = round(
                (self._first_model_token_at - self._start_perf) * 1000, 2,
            )
        if self._first_text_token_at is not None:
            self.result.first_text_token_ms = round(
                (self._first_text_token_at - self._start_perf) * 1000, 2,
            )
        if self._route_start_perf is not None:
            if self._first_model_token_at is not None:
                self.result.e2e_first_model_token_ms = round(
                    (self._first_model_token_at - self._route_start_perf) * 1000,
                    2,
                )
            if self._first_text_token_at is not None:
                self.result.e2e_first_text_token_ms = round(
                    (self._first_text_token_at - self._route_start_perf) * 1000,
                    2,
                )
        self.result.max_input_tokens = self._max_input_tokens
        self.result.context_size = self._context_size

    def _ensure_tool_call(self, tool_call_id: str) -> dict[str, Any]:
        if tool_call_id not in self._tool_calls:
            self._thought_position += 1
            self._tool_calls[tool_call_id] = {
                "name": "unknown",
                "arguments": "",
                "observation": "",
                "position": self._thought_position,
            }
        return self._tool_calls[tool_call_id]

    def _message_envelope(self, answer: str) -> dict[str, str]:
        return self._envelope(
            "message",
            {
                "event": "message",
                "task_id": self.task_id,
                "message_id": self.message_id,
                "conversation_id": self.conversation_id,
                "answer": answer,
                "created_at": self.created_at,
            },
        )

    def _thought_envelope(self, tool_call_id: str) -> dict[str, str]:
        call = self._ensure_tool_call(tool_call_id)
        tool_input = _normalize_tool_input(call["arguments"])
        self._tool_calls.pop(tool_call_id, None)
        return self._envelope(
            "agent_thought",
            {
                "event": "agent_thought",
                "id": f"agent_thought_{call['position']}",
                "task_id": self.task_id,
                "message_id": self.message_id,
                "conversation_id": self.conversation_id,
                "position": call["position"],
                "thought": f"调用 {call['name']} 工具...",
                "tool": call["name"],
                "tool_input": tool_input,
                "observation": call["observation"],
                "created_at": self.created_at,
            },
        )

    def _message_end_envelope(self) -> dict[str, str]:
        usage = self.result.usage
        return self._envelope(
            "message_end",
            {
                "event": "message_end",
                "task_id": self.task_id,
                "message_id": self.message_id,
                "conversation_id": self.conversation_id,
                "metadata": {
                    "usage": {
                        "prompt_tokens": usage["prompt_tokens"],
                        "prompt_unit_price": "0",
                        "prompt_price_unit": "0",
                        "prompt_price": "0",
                        "completion_tokens": usage["completion_tokens"],
                        "completion_unit_price": "0",
                        "completion_price_unit": "0",
                        "completion_price": "0",
                        "total_tokens": usage["total_tokens"],
                        "total_price": "0",
                        "currency": "USD",
                        "latency": 0,
                    },
                    "retriever_resources": [],
                },
            },
        )

    def _error_envelope(
        self, message: str, *, code: str = "internal_error", status: int = 500,
    ) -> dict[str, str]:
        return self._envelope(
            "error",
            {
                "event": "error",
                "task_id": self.task_id,
                "message_id": self.message_id,
                "conversation_id": self.conversation_id,
                "message": message,
                "code": code,
                "status": status,
            },
        )

    @staticmethod
    def _envelope(event: str, payload: dict[str, Any]) -> dict[str, str]:
        return {
            "event": event,
            "data": dumps_decimal(payload, ensure_ascii=False),
        }


def _normalize_tool_input(raw_arguments: str) -> str:
    """将工具参数规范化为可供 Dify 展示的 JSON 字符串。"""
    if not raw_arguments:
        return ""
    try:
        value = json.loads(raw_arguments)
    except json.JSONDecodeError:
        return raw_arguments
    return dumps_decimal(value, ensure_ascii=False)


def _public_reply_error(reason: ReplyFinishedReason) -> str:
    """将 AgentScope 结束原因映射为稳定的对外错误。"""
    if reason == ReplyFinishedReason.EXCEED_MAX_ITERS:
        return PUBLIC_MAX_ITERS_ERROR
    if reason == ReplyFinishedReason.INTERRUPTED:
        return PUBLIC_INTERRUPTED_ERROR
    return PUBLIC_MODEL_ERROR
