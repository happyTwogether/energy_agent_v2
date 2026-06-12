"""
API 路由模块。

定义 REST API 端点，接收用户请求，
通过 SSE 流式推送 Agent 执行过程与结果。

端点说明：
- /api/v1/chat/stream   → PC Web 端（source=1），Dify Agent 格式 SSE
- /api/v1/chat-messages  → App 端（source=0），Dify 兼容，支持 blocking + streaming
"""

import asyncio
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
from app.services.message_store import store_message

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


def _build_usage_metadata(token_usage: dict[str, Any] | None) -> dict[str, Any]:
    """从 LLM usage 构建 Dify metadata.usage 格式。

    Args:
        token_usage: LiteLLM 返回的 usage 对象，如 {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}

    Returns:
        Dify metadata.usage 格式的字典。
    """
    if token_usage:
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
    return {
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
    }


def _extract_usage_tokens(token_usage: dict[str, Any] | None) -> tuple[int | None, int | None, int | None]:
    """从 usage 字典中提取 prompt/completion/total tokens。

    Args:
        token_usage: LLM usage 字典或 None

    Returns:
        (prompt_tokens, completion_tokens, total_tokens) 三元组。
    """
    if not token_usage:
        return None, None, None
    return (
        token_usage.get("prompt_tokens"),
        token_usage.get("completion_tokens"),
        token_usage.get("total_tokens"),
    )


async def _dify_sse_generator(
    messages: list[dict[str, Any]],
    *,
    conversation_id: str,
    user: str | None,
    source: int,
) -> AsyncGenerator[dict[str, Any], None]:
    """共享的 Dify Agent 格式 SSE 生成器。

    产出事件序列：agent_message → agent_thought* → message* → message_end
    PC 端和 App 端的流式接口共用此生成器。

    Args:
        messages: 对话消息列表
        conversation_id: 会话 ID
        user: Dify 格式的 user 标识（PC 端可为 None）
        source: 调用渠道（0=App端, 1=PC端）
    """
    message_id = str(uuid.uuid4())
    task_id = str(uuid.uuid4())
    created_at = int(time.time())

    collected_answer = ""
    token_usage: dict[str, Any] | None = None
    thought_position = 0
    # 暂存 tool_call，等待 tool_result 时合成为 agent_thought
    pending_tool_calls: dict[str, dict[str, Any]] = {}

    # 1. agent_message — 标记 Agent 消息开始
    yield {
        "event": "agent_message",
        "data": dumps_decimal(
            {
                "event": "agent_message",
                "task_id": task_id,
                "message_id": message_id,
                "conversation_id": conversation_id,
                "answer": "",
                "created_at": created_at,
            },
            ensure_ascii=False,
        ),
    }

    try:
        async for event in run_agent_stream(messages=messages):
            if event.event_type == "token":
                token_text = str(event.data)
                collected_answer += token_text
                if "<tool>" not in token_text:
                    yield {
                        "event": "message",
                        "data": dumps_decimal(
                            {
                                "event": "message",
                                "task_id": task_id,
                                "message_id": message_id,
                                "conversation_id": conversation_id,
                                "answer": token_text,
                                "created_at": created_at,
                            },
                            ensure_ascii=False,
                        ),
                    }

            elif event.event_type == "tool_call":
                thought_position += 1
                tool_data = event.data if isinstance(event.data, dict) else {}
                call_id = tool_data.get("call_id", "")
                if call_id:
                    pending_tool_calls[call_id] = tool_data

            elif event.event_type == "tool_result":
                result_data = event.data if isinstance(event.data, dict) else {}
                call_id = result_data.get("call_id", "")
                tool_info = pending_tool_calls.pop(call_id, {}) if call_id else {}
                tool_name = tool_info.get("tool") or result_data.get("tool", "unknown")
                tool_args = tool_info.get("args", {})
                result_content = result_data.get("result") or result_data.get("error", "")

                # 将 tool_result 转为字符串展示
                if isinstance(result_content, dict):
                    observation_str = dumps_decimal(result_content, ensure_ascii=False)
                else:
                    observation_str = str(result_content)

                yield {
                    "event": "agent_thought",
                    "data": dumps_decimal(
                        {
                            "event": "agent_thought",
                            "task_id": task_id,
                            "message_id": message_id,
                            "conversation_id": conversation_id,
                            "position": thought_position,
                            "thought": f"调用 {tool_name} 工具...",
                            "tool": tool_name,
                            "tool_input": dumps_decimal(tool_args, ensure_ascii=False) if tool_args else "",
                            "observation": observation_str,
                            "created_at": created_at,
                        },
                        ensure_ascii=False,
                    ),
                }

            elif event.event_type == "token_usage":
                token_usage = event.data if isinstance(event.data, dict) else None

            elif event.event_type == "final_answer":
                final_text = str(event.data)
                collected_answer = final_text

            elif event.event_type == "error":
                yield {
                    "event": "error",
                    "data": dumps_decimal(
                        {
                            "event": "error",
                            "task_id": task_id,
                            "message_id": message_id,
                            "conversation_id": conversation_id,
                            "message": str(event.data),
                            "code": "internal_error",
                            "status": 500,
                        },
                        ensure_ascii=False,
                    ),
                }

        # 获取用户输入（最后一条 user 消息）
        user_input = ""
        for msg in reversed(messages):
            if msg.get("role") == "user":
                user_input = str(msg.get("content", ""))
                break

        clean_answer = _sanitize_history_answer(collected_answer)

        # 消息存储（异步，不阻塞 SSE 流）
        prompt_tokens, completion_tokens, total_tokens = _extract_usage_tokens(token_usage)
        asyncio.create_task(
            store_message(
                conversation_id=conversation_id,
                user=user,
                user_input=user_input,
                system_output=clean_answer,
                source=source,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
            )
        )

        # message_end
        yield {
            "event": "message_end",
            "data": dumps_decimal(
                {
                    "event": "message_end",
                    "task_id": task_id,
                    "message_id": message_id,
                    "conversation_id": conversation_id,
                    "metadata": _build_usage_metadata(token_usage),
                },
                ensure_ascii=False,
            ),
        }

    except Exception as exc:
        logger.error("Dify SSE 生成异常: %s", exc, exc_info=True)
        yield {
            "event": "error",
            "data": dumps_decimal(
                {
                    "event": "error",
                    "task_id": task_id,
                    "message_id": message_id,
                    "conversation_id": conversation_id,
                    "message": f"服务器内部错误: {exc}",
                    "code": "internal_error",
                    "status": 500,
                },
                ensure_ascii=False,
            ),
        }


# ========== PC 端接口 /api/v1/chat/stream ==========


@router.get("/health")
async def health_check() -> dict[str, str]:
    """健康检查端点，供容器编排和负载均衡器探活。"""
    return {"status": "ok"}


@router.post("/chat/stream")
async def chat_stream(request: ChatRequest) -> EventSourceResponse:
    """PC Web 端 SSE 流式聊天接口。

    接收用户消息，调用 Agent Runner 执行 ReAct Loop，
    以 Dify Agent 格式 SSE 事件推送给客户端。

    消息存储：source=1 (PC端)。

    Args:
        request: 用户聊天请求，包含 messages 数组。

    Returns:
        EventSourceResponse: SSE 流式响应。
    """
    try:
        logger.info("PC端 聊天请求: messages_count=%d", len(request.messages))
        conversation_id = str(uuid.uuid4())
        return EventSourceResponse(
            content=_dify_sse_generator(
                messages=request.messages,
                conversation_id=conversation_id,
                user=None,
                source=1,
            ),
            media_type="text/event-stream",
        )
    except Exception as exc:
        logger.error("PC端 聊天接口异常: %s", exc, exc_info=True)
        raise


# ========== App 端 Dify 接口 /api/v1/chat-messages ==========


def _build_messages_from_dify(
    request: DifyChatRequest,
) -> tuple[str, list[dict[str, Any]]]:
    """构建 Agent 所需的消息列表。

    Args:
        request: Dify 风格的请求

    Returns:
        (conversation_id, messages): 会话ID和完整消息列表
    """
    conversation_id = request.conversation_id
    if not conversation_id:
        conversation_id = str(uuid.uuid4())

    history = _conversations.get(conversation_id, [])

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
    messages: list[dict[str, Any]],
    user: str,
    user_query: str,
) -> AsyncGenerator[dict[str, Any], None]:
    """App 端 Dify 流式 SSE 响应生成器。

    委托给共享的 _dify_sse_generator，并在完成后更新内存会话历史。

    Args:
        conversation_id: 会话 ID
        messages: 完整消息列表
        user: DifyChatRequest.user
        user_query: 用户问题原文
    """
    collected_answer = ""

    async for sse_event in _dify_sse_generator(
        messages=messages,
        conversation_id=conversation_id,
        user=user,
        source=0,
    ):
        # 收集回答文本用于会话历史
        data_str = sse_event.get("data", "")
        try:
            data_obj = json.loads(data_str)
        except (json.JSONDecodeError, TypeError):
            data_obj = {}

        if data_obj.get("event") == "message":
            collected_answer += data_obj.get("answer", "")
        elif data_obj.get("event") == "error":
            pass

        yield sse_event

    # 保存对话历史
    clean_answer = _sanitize_history_answer(collected_answer)
    if conversation_id not in _conversations:
        _conversations[conversation_id] = []
    _conversations[conversation_id].extend([
        {"role": "user", "content": user_query},
        {"role": "assistant", "content": clean_answer},
    ])
    if len(_conversations[conversation_id]) > 20:
        _conversations[conversation_id] = _conversations[conversation_id][-20:]


async def _run_blocking(
    conversation_id: str,
    messages: list[dict[str, Any]],
    user: str,
    user_query: str,
) -> dict[str, Any]:
    """App 端 Dify Blocking 响应。

    收集完整回答后返回 JSON，同时存储消息和更新会话历史。
    """
    message_id = str(uuid.uuid4())
    task_id = str(uuid.uuid4())
    created_at = int(time.time())

    collected_answer = ""
    token_usage: dict[str, Any] | None = None

    async for event in run_agent_stream(messages=messages):
        if event.event_type == "token":
            pass  # blocking 模式不逐 token 推送
        elif event.event_type == "token_usage":
            token_usage = event.data if isinstance(event.data, dict) else None
        elif event.event_type == "final_answer":
            collected_answer = str(event.data)
            break
        elif event.event_type == "error":
            raise Exception(str(event.data))

    clean_answer = _sanitize_history_answer(collected_answer)

    # 保存对话历史
    if conversation_id not in _conversations:
        _conversations[conversation_id] = []
    _conversations[conversation_id].extend([
        {"role": "user", "content": user_query},
        {"role": "assistant", "content": clean_answer},
    ])
    if len(_conversations[conversation_id]) > 20:
        _conversations[conversation_id] = _conversations[conversation_id][-20:]

    # 消息存储（异步）
    prompt_tokens, completion_tokens, total_tokens = _extract_usage_tokens(token_usage)
    asyncio.create_task(
        store_message(
            conversation_id=conversation_id,
            user=user,
            user_input=user_query,
            system_output=clean_answer,
            source=0,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
        )
    )

    return {
        "event": "message",
        "task_id": task_id,
        "id": message_id,
        "message_id": message_id,
        "conversation_id": conversation_id,
        "mode": "chat",
        "answer": collected_answer,
        "created_at": created_at,
        "metadata": _build_usage_metadata(token_usage),
    }


@router.post("/chat-messages", response_model=None)
async def dify_chat_messages(http_request: Request, req: DifyChatRequest) -> Response:
    """Dify 对话应用 API（App 端）。

    对标 Dify API: POST /v1/chat-messages
    https://docs.dify.ai/zh/use-dify/publish/developing-with-apis

    支持两种响应模式：
    - blocking: 等待完整响应后返回 JSON
    - streaming: SSE 流式返回（Dify Agent 格式）

    消息存储：source=0 (App端)。

    请求示例：
    {
        "inputs": {},
        "query": "查询昨天5G数据",
        "response_mode": "streaming",
        "conversation_id": null,
        "user": "user-123"
    }
    """
    try:
        logger.info(
            "App端 Dify 请求: query=%s, conversation_id=%s, response_mode=%s, user=%s",
            req.query,
            req.conversation_id,
            req.response_mode,
            req.user,
        )

        conversation_id, messages = _build_messages_from_dify(req)

        if req.response_mode == "streaming":
            return EventSourceResponse(
                content=_dify_stream_generator(
                    conversation_id=conversation_id,
                    messages=messages,
                    user=req.user,
                    user_query=req.query,
                ),
                media_type="text/event-stream",
            )
        else:
            result = await _run_blocking(
                conversation_id=conversation_id,
                messages=messages,
                user=req.user,
                user_query=req.query,
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
