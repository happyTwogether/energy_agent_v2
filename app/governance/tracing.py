"""
可观测性模块 — 借鉴 Hermes 的全链路 TraceID 思想。

为每次 Agent 请求生成唯一 TraceID，
贯穿 LLM 调用、工具执行、最终输出全过程。
"""

import uuid
from dataclasses import dataclass, field
from typing import Any

from app.core.logging import get_logger

logger = get_logger("tracing")


@dataclass
class Span:
    """单次操作的追踪 Span。"""

    name: str
    trace_id: str
    span_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    parent_span_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_log_context(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "span_name": self.name,
            **self.metadata,
        }


class Tracer:
    """轻量级 Trace 管理器。"""

    def __init__(self) -> None:
        self._active_trace_id: str | None = None
        self._span_stack: list[Span] = []

    def start_trace(self, user_id: str, query_preview: str) -> str:
        """开始一次新的 Trace，返回 trace_id。"""
        self._active_trace_id = str(uuid.uuid4())[:12]
        self._span_stack.clear()
        logger.info(
            "trace_started trace_id=%s user=%s query=%.100s",
            self._active_trace_id,
            user_id,
            query_preview,
        )
        return self._active_trace_id

    def start_span(self, name: str, **metadata: Any) -> Span:
        """开始一个新的 Span。"""
        parent = self._span_stack[-1] if self._span_stack else None
        span = Span(
            name=name,
            trace_id=self._active_trace_id or "no-trace",
            parent_span_id=parent.span_id if parent else None,
            metadata=metadata,
        )
        self._span_stack.append(span)
        return span

    def end_span(self, span: Span, **result: Any) -> None:
        """结束当前 Span。"""
        if span in self._span_stack:
            self._span_stack.remove(span)
        ctx = span.to_log_context()
        ctx.update(result)
        logger.info("span_end %s", ctx)

    def end_trace(self, **summary: Any) -> None:
        """结束当前 Trace。"""
        logger.info(
            "trace_ended trace_id=%s %s",
            self._active_trace_id,
            summary,
        )
        self._active_trace_id = None
        self._span_stack.clear()

    @property
    def active_trace_id(self) -> str:
        return self._active_trace_id or "no-trace"


# 全局 Tracer 单例
tracer = Tracer()
