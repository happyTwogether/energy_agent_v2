"""
API 路由模块。

定义 REST API 端点，接收用户请求，
通过 SSE 流式推送 Agent 执行过程与结果。
"""

import json
import re
import time
import uuid
from typing import Any, AsyncGenerator

import httpx

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from sse_starlette.sse import EventSourceResponse

from app.core.agent_runner import run_agent_stream
from app.core.config import get_settings
from app.core.json_utils import dumps_decimal
from app.core.logging import get_logger
from app.models.schemas import (
    CellSearchItem,
    ChatRequest,
    ConversationDetailResponse,
    ConversationItem,
    ConversationListResponse,
    DifyChatRequest,
    StreamEvent,
)
from app.services.database import (
    Conversation,
    get_session_factory,
    get_conversation_title,
    list_conversations,
    load_conversation,
    save_conversation,
)
from sqlalchemy import delete as sa_delete

logger = get_logger("routes")

router = APIRouter()


def _sanitize_history_answer(answer: str) -> str:
    """清理历史消息中的工具调用痕迹。"""
    if not answer:
        return ""

    cleaned = answer

    # 1. 清理 <tool> 标签
    if "<tool>" in cleaned and "</tool>" in cleaned:
        cleaned = re.sub(r"<tool>\s*\{[\s\S]*?\}\s*</tool>", "", cleaned)

    # 2. 清理 JSON 代码块
    json_block_start = re.search(r"```json\s*\{", cleaned, flags=re.IGNORECASE)
    if json_block_start:
        cleaned = cleaned[:json_block_start.start()]

    # 3. 清理工具调用 JSON 开头
    tool_json_start = re.search(r"\{\s*\"name\"\s*:\s*\"[^\"]+\"", cleaned)
    if tool_json_start:
        cleaned = cleaned[:tool_json_start.start()]

    # 4. 清理工具调用相关的文字表述
    cleaned = re.sub(r"我(调用|使用)\s*\w+\s*工具[，,。]?\s*", "", cleaned)
    cleaned = re.sub(r"通过\s*\w+\s*工具\s*(查询|获取|调取)[到]?[，,。]?\s*", "查询到", cleaned)

    return cleaned.strip()


@router.get("/health")
async def health_check() -> dict[str, str]:
    """健康检查端点，供容器编排和负载均衡器探活。"""
    return {"status": "ok"}


@router.get("/conversations", response_model=ConversationListResponse)
async def get_conversations(user_id: str) -> ConversationListResponse:
    """获取指定用户的会话列表，按更新时间倒序。"""
    session_factory = get_session_factory()
    async with session_factory() as db:
        items = await list_conversations(db, user_id)
    return ConversationListResponse(
        conversations=[ConversationItem(**item) for item in items]
    )


@router.get("/conversations/{conversation_id}", response_model=ConversationDetailResponse)
async def get_conversation(conversation_id: str) -> ConversationDetailResponse:
    """获取单个会话的消息历史。"""
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
            sa_delete(Conversation).where(Conversation.conversation_id == conversation_id)
        )
        await db.commit()
    logger.info("会话已删除: %s", conversation_id)
    return {"status": "ok"}


async def _event_generator(
    query: str, conversation_id: str | None, user_id: str
) -> AsyncGenerator[dict, None]:
    """将 Agent 流式事件转换为 SSE 格式的事件生成器。

    流程：加载/创建会话 → 调用 Agent → 保存结果到 DB。
    """
    session_factory = get_session_factory()
    async with session_factory() as db:
        cid = conversation_id or str(uuid.uuid4())
        history = await load_conversation(db, cid)

        messages = list(history)
        messages.append({"role": "user", "content": query})

        is_new = len(history) == 0

        # 首条事件：告知前端 conversation_id
        yield {
            "event": "conversation",
            "data": dumps_decimal(
                {"event_type": "conversation", "data": {"conversation_id": cid}},
                ensure_ascii=False,
            ),
        }

        saved_messages: list[dict] = []
        t_start = time.time()
        try:
            logger.info("开始处理请求: query=%s, cid=%s", query[:50], cid)
            output_chars = 0
            async for event in run_agent_stream(messages=messages, output_messages=saved_messages):
                if event.event_type == "token":
                    output_chars += len(str(event.data))
                event_data = dumps_decimal(
                    {"event_type": event.event_type, "data": event.data},
                    ensure_ascii=False,
                )
                yield {"event": event.event_type, "data": event_data}
            logger.info("请求处理完成: cid=%s, elapsed=%.1fs", cid, time.time() - t_start)
            logger.info("本次请求消耗token数(估算): %d, cid=%s", max(output_chars // 2, 1), cid)
        except Exception as exc:
            logger.error("SSE 事件生成异常: %s", exc, exc_info=True)
            yield {
                "event": "error",
                "data": dumps_decimal(
                    {"event_type": "error", "data": f"服务器内部错误: {exc}"},
                    ensure_ascii=False,
                ),
            }

        # 保存会话：output_messages 已包含最终回答（由 agent_runner._emit_final 自动写入）
        title = query[:20] if is_new else None
        await save_conversation(db, cid, user_id, saved_messages, title=title, replace=True)


@router.post("/chat/stream")
async def chat_stream(request: ChatRequest) -> EventSourceResponse:
    """SSE 流式聊天接口。

    接收用户消息，加载/创建会话，通过 Agent Runner 处理，
    将每个执行步骤的 StreamEvent 通过 SSE 推送给客户端。
    """
    try:
        logger.info("收到聊天请求: query=%s, cid=%s", request.query[:50], request.conversation_id)
        return EventSourceResponse(
            content=_event_generator(
                query=request.query,
                conversation_id=request.conversation_id,
                user_id=request.user_id,
            ),
            media_type="text/event-stream",
        )
    except Exception as exc:
        logger.error("聊天接口异常: %s", exc, exc_info=True)
        raise



@router.get("/cells/search")
async def search_cells(keyword: str = Query(..., min_length=1, description="小区名关键字")):
    """模糊查询小区，代理调用 CGI 查询服务。"""
    settings = get_settings()
    url = f"{settings.cmdi_mcp_ai_url}/tableFieldCorrespondence/selectLteByCgiOrCellName"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(5.0)) as client:
            resp = await client.post(url, json={"cgi": "", "cellName": keyword, "netWork": ""})
            resp.raise_for_status()
            data = resp.json()
            items = [CellSearchItem(**item) for item in data.get("data", [])]
            return {"code": 200, "msg": data.get("msg", "ok"), "data": [item.model_dump() for item in items]}
    except httpx.TimeoutException:
        logger.warning("小区查询超时: keyword=%s", keyword)
        return {"code": 500, "msg": "查询超时", "data": []}
    except Exception as exc:
        logger.error("小区查询异常: %s", exc)
        return {"code": 500, "msg": str(exc), "data": []}


# ========== Dify 风格接口 ==========


async def _build_messages_from_dify(
    request: DifyChatRequest,
    db,
) -> tuple[str, list[dict[str, Any]]]:
    """构建 Agent 所需的消息列表。

    Args:
        request: Dify 风格的请求
        db: 数据库会话

    Returns:
        (conversation_id, messages): 会话ID和完整消息列表
    """
    conversation_id = request.conversation_id
    if not conversation_id:
        conversation_id = str(uuid.uuid4())

    history = await load_conversation(db, conversation_id)

    system_content = (
        "你是一个4G/5G网络节能分析助手。"
        "对于 query_report、query_metric、query_anomaly 这类查询，若用户未明确提及地市/厂家/日期，"
        "必须静默使用默认值直接调用工具，禁止追问确认。"
        "其中 query_report 未提及日期时默认昨天，未提及厂家时默认全网。"
    )
    if request.inputs:
        system_content += f"\n\n用户上下文信息：{json.dumps(request.inputs, ensure_ascii=False)}"

    messages: list[dict[str, Any]] = [{"role": "system", "content": system_content}]
    messages.extend(history)
    messages.append({"role": "user", "content": request.query})

    return conversation_id, messages


async def _dify_stream_generator(
    conversation_id: str,
    message_id: str,
    task_id: str,
    created_at: int,
    messages: list[dict[str, Any]],
    user_query: str,
    user_id: str,
    db,
) -> AsyncGenerator[dict, None]:
    """Dify 风格的 SSE 流式响应生成器。"""
    t_start = time.time()
    try:
        collected_answer = ""

        saved_messages: list[dict] = []
        output_chars = 0
        async for event in run_agent_stream(messages=messages, output_messages=saved_messages):
            if event.event_type == "token":
                token_text = str(event.data)
                output_chars += len(token_text)
                collected_answer += token_text
                if "<tool>" not in token_text:
                    yield {
                        "event": "message",
                        "data": dumps_decimal(
                            {
                                "task_id": task_id,
                                "message_id": message_id,
                                "conversation_id": conversation_id,
                                "answer": token_text,
                                "created_at": created_at,
                            },
                            ensure_ascii=False,
                        ),
                    }
            elif event.event_type == "final_answer":
                final_text = str(event.data)
                if not collected_answer:
                    collected_answer = final_text
                    yield {
                        "event": "message",
                        "data": dumps_decimal(
                            {
                                "task_id": task_id,
                                "message_id": message_id,
                                "conversation_id": conversation_id,
                                "answer": final_text,
                                "created_at": created_at,
                            },
                            ensure_ascii=False,
                        ),
                    }
                else:
                    collected_answer = final_text
            elif event.event_type == "error":
                yield {
                    "event": "error",
                    "data": dumps_decimal(
                        {"message": str(event.data), "code": "internal_error"},
                        ensure_ascii=False,
                    ),
                }

        # output_messages 已包含最终回答（由 agent_runner._emit_final 自动写入）
        await save_conversation(db, conversation_id, user_id, saved_messages, replace=True)

        yield {
            "event": "message_end",
            "data": dumps_decimal(
                {
                    "task_id": task_id,
                    "message_id": message_id,
                    "conversation_id": conversation_id,
                    "metadata": {},
                },
                ensure_ascii=False,
            ),
        }

        logger.info("请求处理完成: cid=%s, elapsed=%.1fs", conversation_id, time.time() - t_start)
        logger.info("本次请求消耗token数(估算): %d, cid=%s", max(output_chars // 2, 1), conversation_id)

    except Exception as exc:
        logger.error("Dify SSE 事件生成异常: %s", exc, exc_info=True)
        yield {
            "event": "error",
            "data": dumps_decimal(
                {"message": f"服务器内部错误: {exc}", "code": "internal_error"},
                ensure_ascii=False,
            ),
        }


async def _run_blocking(
    conversation_id: str,
    message_id: str,
    task_id: str,
    created_at: int,
    messages: list[dict[str, Any]],
    user_query: str,
    user_id: str,
    db,
) -> dict[str, Any]:
    """Dify Blocking 响应格式。"""
    t_start = time.time()
    collected_answer = ""
    token_count = 0

    saved_messages: list[dict] = []
    async for event in run_agent_stream(messages=messages, output_messages=saved_messages):
        if event.event_type == "token":
            token_count += len(str(event.data))
        elif event.event_type == "final_answer":
            collected_answer = str(event.data)
            break
        elif event.event_type == "error":
            raise Exception(str(event.data))

    # output_messages 已包含最终回答（由 agent_runner._emit_final 自动写入）
    await save_conversation(db, conversation_id, user_id, saved_messages, replace=True)

    logger.info("请求处理完成: cid=%s, elapsed=%.1fs", conversation_id, time.time() - t_start)
    logger.info("本次请求消耗token数(估算): %d, cid=%s", max(token_count // 2, 1), conversation_id)

    return {
        "event": "message",
        "task_id": task_id,
        "id": message_id,
        "message_id": message_id,
        "conversation_id": conversation_id,
        "mode": "chat",
        "answer": collected_answer,
        "created_at": created_at,
        "metadata": {
            "usage": {
                "prompt_tokens": 0,
                "prompt_unit_price": "0",
                "prompt_price_unit": "0",
                "prompt_price": "0",
                "completion_tokens": 0,
                "completion_unit_price": "0",
                "completion_price_unit": "0",
                "completion_price": "0",
                "total_tokens": 0,
                "total_price": "0",
                "currency": "USD",
                "latency": 0,
            },
            "retriever_resources": [],
        },
    }


@router.post("/chat-messages", response_model=None)
async def dify_chat_messages(http_request: Request, req: DifyChatRequest) -> Response:
    """Dify 对话应用 API。

    对标 Dify API: POST /v1/chat-messages
    支持 streaming (SSE) 和 blocking (JSON) 两种响应模式。
    """
    try:
        logger.info(
            "收到 Dify 聊天请求: query=%s, conversation_id=%s, response_mode=%s",
            req.query,
            req.conversation_id,
            req.response_mode,
        )

        session_factory = get_session_factory()
        async with session_factory() as db:
            conversation_id, messages = await _build_messages_from_dify(req, db)
            message_id = str(uuid.uuid4())
            task_id = str(uuid.uuid4())
            created_at = int(time.time())

            if req.response_mode == "streaming":
                return EventSourceResponse(
                    content=_dify_stream_generator(
                        conversation_id=conversation_id,
                        message_id=message_id,
                        task_id=task_id,
                        created_at=created_at,
                        messages=messages,
                        user_query=req.query,
                        user_id=req.user,
                        db=db,
                    ),
                    media_type="text/event-stream",
                )
            else:
                result = await _run_blocking(
                    conversation_id=conversation_id,
                    message_id=message_id,
                    task_id=task_id,
                    created_at=created_at,
                    messages=messages,
                    user_query=req.query,
                    user_id=req.user,
                    db=db,
                )
                payload = dumps_decimal(result, ensure_ascii=False)
                return Response(content=payload, media_type="application/json")

    except Exception as exc:
        logger.error("Dify 聊天接口异常: %s", exc, exc_info=True)
        return JSONResponse(
            status_code=500,
            content={
                "message": str(exc),
                "code": "internal_error",
                "type": "internal_server_error",
            },
        )