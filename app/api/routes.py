"""FastAPI 路由与 Dify 兼容协议编排。"""

import asyncio
import json
import time
from typing import Any, AsyncGenerator
import uuid

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse
from sqlalchemy import delete as sa_delete, text

from app.agent.dify_events import DifyEventAdapter
from app.agent.errors import (
    AgentPublicError,
    PUBLIC_INTERNAL_ERROR,
    PUBLIC_MODEL_ERROR,
)
from app.agent.runtime import stream_agent_events
from app.core.config import TITLE_MAX_LENGTH
from app.core.json_utils import dumps_decimal
from app.core.logging import get_logger
from app.models.schemas import (
    ConversationDetailResponse,
    ConversationItem,
    ConversationListResponse,
    DifyChatRequest,
)
from app.services.database import (
    Conversation,
    get_conversation_title,
    get_session_factory,
    list_conversations,
    load_conversation,
    save_conversation,
)
from app.services.message_store import store_message

logger = get_logger("routes")
router = APIRouter()


async def _save_answer_to_db(
    db: Any,
    conversation_id: str,
    user_id: str,
    user_query: str,
    answer: str,
) -> None:
    """保存且仅保存一组本轮 user/assistant 消息。"""
    if not answer:
        return
    await save_conversation(
        db,
        conversation_id,
        user_id=user_id,
        new_messages=[
            {"role": "user", "content": user_query},
            {"role": "assistant", "content": answer},
        ],
        title=user_query[:TITLE_MAX_LENGTH],
        replace=False,
    )


async def _persist_answer_and_notify(
    db: Any,
    conversation_id: str,
    user_id: str,
    user_query: str,
    answer: str,
    token_usage: dict[str, int],
    source: int = 0,
) -> None:
    """持久化已完成回答，并异步通知外部消息存储。"""
    await _save_answer_to_db(
        db,
        conversation_id,
        user_id,
        user_query,
        answer,
    )
    if not answer:
        return
    asyncio.create_task(
        store_message(
            conversation_id=conversation_id,
            user=user_id,
            user_input=user_query,
            system_output=answer,
            source=source,
            prompt_tokens=token_usage.get("prompt_tokens"),
            completion_tokens=token_usage.get("completion_tokens"),
            total_tokens=token_usage.get("total_tokens"),
        ),
    )


async def _persist_metrics(
    conversation_id: str,
    user_id: str,
    source: int,
    result: Any,
) -> None:
    """落库一次请求的性能指标，尽力而为，失败仅记录日志。"""
    try:
        usage = result.usage
        prompt_tokens = usage.get("prompt_tokens", 0) or 0
        completion_tokens = usage.get("completion_tokens", 0) or 0
        total_tokens = usage.get("total_tokens", 0) or 0
        context_size = result.context_size or 0
        max_input_tokens = result.max_input_tokens or 0
        context_ratio = (
            round(max_input_tokens / context_size * 100, 2)
            if context_size > 0
            else 0.0
        )
        gen_ms = result.total_ms - (result.first_text_token_ms or 0)
        throughput = (
            round(completion_tokens / (gen_ms / 1000), 2)
            if gen_ms > 0 and completion_tokens > 0
            else 0.0
        )
        success = result.completed and not result.error

        session_factory = get_session_factory()
        async with session_factory() as db:
            await db.execute(
                text(
                    """
                    INSERT INTO jd_agent.agent_metrics
                        (conversation_id, user_id, source,
                         prompt_tokens, completion_tokens, total_tokens,
                         first_model_token_ms, first_text_token_ms, total_ms,
                         e2e_first_model_token_ms, e2e_first_text_token_ms,
                         throughput, max_input_tokens, context_size, context_ratio,
                         success, error_type)
                    VALUES
                        (:conversation_id, :user_id, :source,
                         :prompt_tokens, :completion_tokens, :total_tokens,
                         :first_model_token_ms, :first_text_token_ms, :total_ms,
                         :e2e_first_model_token_ms, :e2e_first_text_token_ms,
                         :throughput, :max_input_tokens, :context_size, :context_ratio,
                         :success, :error_type)
                    """
                ),
                {
                    "conversation_id": conversation_id,
                    "user_id": user_id,
                    "source": source,
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": total_tokens,
                    "first_model_token_ms": result.first_model_token_ms,
                    "first_text_token_ms": result.first_text_token_ms,
                    "total_ms": result.total_ms,
                    "e2e_first_model_token_ms": result.e2e_first_model_token_ms,
                    "e2e_first_text_token_ms": result.e2e_first_text_token_ms,
                    "throughput": throughput,
                    "max_input_tokens": max_input_tokens,
                    "context_size": context_size,
                    "context_ratio": context_ratio,
                    "success": success,
                    "error_type": result.error,
                },
            )
            await db.commit()
    except Exception:
        logger.exception("指标落库失败: cid=%s", conversation_id)


def _build_usage_metadata(token_usage: dict[str, int]) -> dict[str, Any]:
    """构建 Dify blocking 响应的 metadata.usage。"""
    return {
        "usage": {
            "prompt_tokens": token_usage.get("prompt_tokens", 0),
            "prompt_unit_price": "0",
            "prompt_price_unit": "0",
            "prompt_price": "0",
            "completion_tokens": token_usage.get("completion_tokens", 0),
            "completion_unit_price": "0",
            "completion_price_unit": "0",
            "completion_price": "0",
            "total_tokens": token_usage.get("total_tokens", 0),
            "total_price": "0",
            "currency": "USD",
            "latency": 0,
        },
        "retriever_resources": [],
    }


def _new_dify_adapter(
    conversation_id: str,
    route_start_perf: float | None = None,
) -> DifyEventAdapter:
    """为一次响应创建独立的 Dify 事件聚合器。"""
    return DifyEventAdapter(
        conversation_id=conversation_id,
        message_id=str(uuid.uuid4()),
        task_id=str(uuid.uuid4()),
        created_at=int(time.time()),
        route_start_perf=route_start_perf,
    )


@router.get("/health")
async def health_check() -> dict[str, str]:
    """健康检查端点。"""
    return {"status": "ok"}


@router.get("/conversations", response_model=ConversationListResponse)
async def get_conversations(user_id: str) -> ConversationListResponse:
    """获取指定用户的会话列表。"""
    session_factory = get_session_factory()
    async with session_factory() as db:
        items = await list_conversations(db, user_id)
    return ConversationListResponse(
        conversations=[ConversationItem(**item) for item in items],
    )


@router.get(
    "/conversations/{conversation_id}",
    response_model=ConversationDetailResponse,
)
async def get_conversation(conversation_id: str) -> ConversationDetailResponse:
    """获取指定会话的完整消息历史。"""
    session_factory = get_session_factory()
    async with session_factory() as db:
        messages = await load_conversation(db, conversation_id)
        title = await get_conversation_title(db, conversation_id)
    return ConversationDetailResponse(
        conversation_id=conversation_id,
        title=title,
        messages=messages,
    )


@router.delete("/conversations/{conversation_id}")
async def delete_conversation(conversation_id: str) -> dict[str, str]:
    """删除指定会话。"""
    session_factory = get_session_factory()
    async with session_factory() as db:
        await db.execute(
            sa_delete(Conversation).where(
                Conversation.conversation_id == conversation_id,
            ),
        )
        await db.commit()
    logger.info("会话已删除: %s", conversation_id)
    return {"status": "ok"}


async def _build_messages_from_dify(
    request: DifyChatRequest,
    db: Any,
) -> tuple[str, list[dict[str, Any]]]:
    """构建受控 system 上下文、持久化历史和本轮用户输入。"""
    conversation_id = request.conversation_id or str(uuid.uuid4())
    history = await load_conversation(db, conversation_id)
    system_content = ""
    if request.inputs:
        system_content = (
            "用户上下文信息："
            + json.dumps(request.inputs, ensure_ascii=False)
        )

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_content},
        *history,
        {"role": "user", "content": request.query},
    ]
    return conversation_id, messages


async def _dify_stream_generator(
    conversation_id: str,
    messages: list[dict[str, Any]],
    user: str,
    user_query: str,
    *,
    source: int = 0,
    route_start_perf: float | None = None,
) -> AsyncGenerator[dict[str, str], None]:
    """流式输出 Dify 事件，并仅在正常完成后持久化答案。"""
    adapter = _new_dify_adapter(conversation_id, route_start_perf)
    events = stream_agent_events(messages, conversation_id)
    terminal_envelope: dict[str, str] | None = None
    async for envelope in adapter.stream(events):
        if envelope["event"] == "message_end":
            terminal_envelope = envelope
        else:
            yield envelope

    result = adapter.result
    await _persist_metrics(conversation_id, user, source, result)
    if not result.completed or result.error:
        return
    if not result.answer:
        yield adapter.error_envelope(PUBLIC_MODEL_ERROR)
        return

    try:
        session_factory = get_session_factory()
        async with session_factory() as db:
            await _persist_answer_and_notify(
                db,
                conversation_id,
                user,
                user_query,
                result.answer,
                result.usage,
                source=source,
            )
    except Exception:
        logger.exception("流式回答持久化失败: cid=%s", conversation_id)
        yield adapter.error_envelope()
        return

    if terminal_envelope is not None:
        yield terminal_envelope


async def _run_blocking(
    conversation_id: str,
    messages: list[dict[str, Any]],
    user: str,
    user_query: str,
    *,
    route_start_perf: float | None = None,
) -> dict[str, Any]:
    """消费同一 Dify 事件流，并返回 blocking JSON。"""
    adapter = _new_dify_adapter(conversation_id, route_start_perf)
    events = stream_agent_events(messages, conversation_id)
    async for _ in adapter.stream(events):
        pass

    result = adapter.result
    await _persist_metrics(conversation_id, user, 0, result)
    if result.error:
        raise AgentPublicError(result.error)
    if not result.completed:
        raise AgentPublicError(PUBLIC_MODEL_ERROR)
    if not result.answer:
        raise AgentPublicError(PUBLIC_MODEL_ERROR)

    session_factory = get_session_factory()
    async with session_factory() as db:
        await _persist_answer_and_notify(
            db,
            conversation_id,
            user,
            user_query,
            result.answer,
            result.usage,
            source=0,
        )
    return {
        "event": "message",
        "task_id": adapter.task_id,
        "id": adapter.message_id,
        "message_id": adapter.message_id,
        "conversation_id": conversation_id,
        "mode": "chat",
        "answer": result.answer,
        "created_at": adapter.created_at,
        "metadata": _build_usage_metadata(result.usage),
    }


@router.post("/chat/stream")
async def chat_stream(http_request: Request, req: DifyChatRequest) -> Response:
    """PC Web 端 Dify 格式流式聊天接口。"""
    route_start_perf = time.perf_counter()
    logger.info(
        "PC端聊天请求: query=%s, cid=%s, user=%s",
        req.query,
        req.conversation_id,
        req.user,
    )
    session_factory = get_session_factory()
    async with session_factory() as db:
        conversation_id, messages = await _build_messages_from_dify(req, db)

    return EventSourceResponse(
        content=_dify_stream_generator(
            conversation_id=conversation_id,
            messages=messages,
            user=req.user,
            user_query=req.query,
            source=1,
            route_start_perf=route_start_perf,
        ),
        media_type="text/event-stream",
    )


@router.post("/chat-messages", response_model=None)
async def dify_chat_messages(
    http_request: Request,
    req: DifyChatRequest,
) -> Response:
    """Dify 对话 API，支持 blocking 与 streaming。"""
    try:
        route_start_perf = time.perf_counter()
        logger.info(
            "App端 Dify 请求: query=%s, cid=%s, mode=%s, user=%s",
            req.query,
            req.conversation_id,
            req.response_mode,
            req.user,
        )
        session_factory = get_session_factory()
        async with session_factory() as db:
            conversation_id, messages = await _build_messages_from_dify(req, db)

        if req.response_mode == "blocking":
            result = await _run_blocking(
                conversation_id=conversation_id,
                messages=messages,
                user=req.user,
                user_query=req.query,
                route_start_perf=route_start_perf,
            )
            return Response(
                content=dumps_decimal(result, ensure_ascii=False),
                media_type="application/json",
            )

        return EventSourceResponse(
            content=_dify_stream_generator(
                conversation_id=conversation_id,
                messages=messages,
                user=req.user,
                user_query=req.query,
                source=0,
                route_start_perf=route_start_perf,
            ),
            media_type="text/event-stream",
        )

    except AgentPublicError as exc:
        logger.warning("Dify Agent 安全错误: type=%s", type(exc).__name__)
        return JSONResponse(
            status_code=500,
            content={
                "message": str(exc),
                "code": "agent_error",
                "type": "agent_runtime_error",
            },
        )
    except Exception as exc:
        logger.error("Dify 聊天接口异常: %s", exc, exc_info=True)
        return JSONResponse(
            status_code=500,
            content={
                "message": PUBLIC_INTERNAL_ERROR,
                "code": "internal_error",
                "type": "internal_server_error",
            },
        )
