"""
API 路由模块。

定义 REST API 端点，接收用户请求，
通过 SSE 流式推送 Agent 执行过程与结果。
"""

import json
from typing import AsyncGenerator

from fastapi import APIRouter
from sse_starlette.sse import EventSourceResponse

from app.core.agent_runner import run_agent_stream
from app.core.logging import get_logger
from app.models.schemas import ChatRequest, StreamEvent

logger = get_logger("routes")

router = APIRouter()


@router.get("/health")
async def health_check() -> dict[str, str]:
    """健康检查端点，供容器编排和负载均衡器探活。"""
    return {"status": "ok"}


async def _event_generator(request: ChatRequest) -> AsyncGenerator[dict, None]:
    """将 Agent 流式事件转换为 SSE 格式的事件生成器。

    Args:
        request: 用户聊天请求。

    Yields:
        符合 SSE 协议的事件字典，包含 event 和 data 字段。
    """
    try:
        logger.info("开始处理请求: messages_count=%d", len(request.messages))
        async for event in run_agent_stream(messages=request.messages):
            event_data: str = json.dumps(
                {"event_type": event.event_type, "data": event.data},
                ensure_ascii=False,
            )

            yield {
                "event": event.event_type,
                "data": event_data,
            }
        logger.info("请求处理完成")
    except Exception as exc:
        logger.error("SSE 事件生成异常: %s", exc, exc_info=True)
        error_event = StreamEvent(event_type="error", data=f"服务器内部错误: {exc}")
        yield {
            "event": error_event.event_type,
            "data": json.dumps(
                {"event_type": error_event.event_type, "data": error_event.data},
                ensure_ascii=False,
            ),
        }


@router.post("/chat/stream")
async def chat_stream(request: ChatRequest) -> EventSourceResponse:
    """SSE 流式聊天接口。

    接收用户消息，调用 Agent Runner 执行 ReAct Loop，
    将每个执行步骤的 StreamEvent 通过 SSE 推送给客户端。

    Args:
        request: 用户聊天请求，包含 messages 数组。

    Returns:
        EventSourceResponse: SSE 流式响应。
    """
    try:
        logger.info("收到聊天请求: messages_count=%d", len(request.messages))
        return EventSourceResponse(
            content=_event_generator(request),
            media_type="text/event-stream",
        )
    except Exception as exc:
        logger.error("聊天接口异常: %s", exc, exc_info=True)
        raise
