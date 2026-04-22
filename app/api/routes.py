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

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from sse_starlette.sse import EventSourceResponse

from app.core.agent_runner import run_agent_stream
from app.core.json_utils import dumps_decimal
from app.core.logging import get_logger
from app.models.schemas import (
    ChatRequest,
    DifyChatRequest,
    StreamEvent,
)

logger = get_logger("routes")

router = APIRouter()

# 内存会话存储: conversation_id -> list[dict]
# 结构: [{"role": "user", "content": "..."}, {"role": "assistant", "content": "..."}]
_conversations: dict[str, list[dict[str, Any]]] = {}


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
            event_data: str = dumps_decimal(
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
            "data": dumps_decimal(
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


# ========== Dify 风格接口 ==========


def _build_messages_from_dify(
    request: DifyChatRequest,
) -> tuple[str, list[dict[str, Any]]]:
    """构建 Agent 所需的消息列表。

    Args:
        request: Dify 风格的请求

    Returns:
        (conversation_id, messages): 会话ID和完整消息列表
    """
    # 确定会话 ID
    conversation_id = request.conversation_id
    if not conversation_id:
        conversation_id = str(uuid.uuid4())

    # 获取或初始化会话历史
    history = _conversations.get(conversation_id, [])

    # 构造 system prompt（包含 inputs 上下文）
    system_content = (
        "你是一个4G/5G网络节能分析助手。"
        "对于 query_report、query_metric、query_anomaly 这类查询，若用户未明确提及地市/厂家/日期，"
        "必须静默使用默认值直接调用工具，禁止追问确认。"
        "其中 query_report 未提及日期时默认昨天，未提及厂家时默认全网。"
    )
    if request.inputs:
        system_content += f"\n\n用户上下文信息：{json.dumps(request.inputs, ensure_ascii=False)}"

    # 构建完整消息列表
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
) -> AsyncGenerator[dict, None]:
    """Dify 风格的 SSE 流式响应生成器。

    事件顺序：
    1. message (开始)
    2. message_end (结束)

    Dify Streaming 响应格式：
    - message 事件: {"event": "message", "data": {"task_id": "", "message_id": "", "conversation_id": "", "answer": "", "created_at": 1234567890}}
    - message_end 事件: {"event": "message_end", "data": {"task_id": "", "message_id": "", "conversation_id": "", "metadata": {}}}
    """
    try:
        collected_answer = ""

        async for event in run_agent_stream(messages=messages):
            if event.event_type == "token":
                # token -> Dify 的 message 事件
                token_text = str(event.data)
                collected_answer += token_text
                # 过滤 <tool> 标签，不输出给前端
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
                # final_answer 包含完整回答
                final_text = str(event.data)
                # 如果之前没有收集到 token，或者 final_answer 与收集的不同，发送差异部分
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
                    # 已经有 token，用 final_answer 作为最终答案
                    collected_answer = final_text
            elif event.event_type == "error":
                yield {
                    "event": "error",
                    "data": dumps_decimal(
                        {"message": str(event.data), "code": "internal_error"},
                        ensure_ascii=False,
                    ),
                }

        # 保存对话历史（追加而不是覆盖）
        clean_answer = _sanitize_history_answer(collected_answer)
        if conversation_id not in _conversations:
            _conversations[conversation_id] = []
        _conversations[conversation_id].extend([
            {"role": "user", "content": user_query},
            {"role": "assistant", "content": clean_answer},
        ])
        # 限制历史长度，保留最近 10 轮对话
        if len(_conversations[conversation_id]) > 20:
            _conversations[conversation_id] = _conversations[conversation_id][-20:]

        # 发送 message_end
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
) -> dict[str, Any]:
    """Dify Blocking 响应格式。"""
    collected_answer = ""
    token_count = 0

    async for event in run_agent_stream(messages=messages):
        if event.event_type == "token":
            token_count += 1
        elif event.event_type == "final_answer":
            # blocking 模式只消费最终答案，避免重复拼接大量 token
            collected_answer = str(event.data)
            logger.info(
                "blocking 收到 final_answer: len=%d, token_count=%d",
                len(collected_answer),
                token_count,
            )
            break
        elif event.event_type == "error":
            raise Exception(str(event.data))

    logger.info("blocking 开始清洗答案: len=%d", len(collected_answer))
    clean_answer = _sanitize_history_answer(collected_answer)
    logger.info("blocking 清洗答案完成: raw_len=%d, clean_len=%d", len(collected_answer), len(clean_answer))

    # 保存对话历史（追加而不是覆盖）
    if conversation_id not in _conversations:
        _conversations[conversation_id] = []
    _conversations[conversation_id].extend([
        {"role": "user", "content": user_query},
        {"role": "assistant", "content": clean_answer},
    ])
    # 限制历史长度，保留最近 10 轮对话
    if len(_conversations[conversation_id]) > 20:
        _conversations[conversation_id] = _conversations[conversation_id][-20:]
    logger.info("blocking 保存历史完成: conversation_id=%s, history_size=%d", conversation_id, len(_conversations[conversation_id]))

    logger.info("blocking 准备返回响应: message_id=%s, answer_len=%d", message_id, len(collected_answer))
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
    https://docs.dify.ai/zh/use-dify/publish/developing-with-apis

    支持两种响应模式：
    - blocking: 等待完整响应后返回 JSON
    - streaming: SSE 流式返回

    请求示例：
    {
        "inputs": {},
        "query": "查询昨天5G数据",
        "response_mode": "streaming",
        "conversation_id": null,  // 或传入已有会话ID
        "user": "user-123"
    }
    """
    try:
        logger.info(
            "收到 Dify 聊天请求: query=%s, conversation_id=%s, response_mode=%s",
            req.query,
            req.conversation_id,
            req.response_mode,
        )

        # 构建消息列表
        conversation_id, messages = _build_messages_from_dify(req)
        message_id = str(uuid.uuid4())
        task_id = str(uuid.uuid4())
        created_at = int(time.time())

        if req.response_mode == "streaming":
            # 流式响应
            return EventSourceResponse(
                content=_dify_stream_generator(
                    conversation_id=conversation_id,
                    message_id=message_id,
                    task_id=task_id,
                    created_at=created_at,
                    messages=messages,
                    user_query=req.query,
                ),
                media_type="text/event-stream",
            )
        else:
            # 阻塞响应
            result = await _run_blocking(
                conversation_id=conversation_id,
                message_id=message_id,
                task_id=task_id,
                created_at=created_at,
                messages=messages,
                user_query=req.query,
            )
            logger.info("blocking 已生成结果，准备序列化: message_id=%s", message_id)
            payload = dumps_decimal(result, ensure_ascii=False)
            logger.info(
                "blocking 序列化完成: message_id=%s, bytes=%d",
                message_id,
                len(payload.encode("utf-8")),
            )
            response = Response(content=payload, media_type="application/json")
            logger.info("blocking Response 已构造: message_id=%s", message_id)
            return response

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