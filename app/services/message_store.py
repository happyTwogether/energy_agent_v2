"""
消息存储客户端模块。

封装自研平台 /sendMsg 接口的异步 HTTP 调用，
在每次对话完成后将消息记录持久化到平台。
存储失败不影响主流程，仅记录日志。
"""

import hashlib
from typing import Any

import httpx

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger("message_store")

# 全局共享的 httpx 客户端（复用连接池）
_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    """获取全局共享的异步 HTTP 客户端。"""
    global _client
    if _client is None:
        settings = get_settings()
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.message_store_timeout),
            headers={"Content-Type": "application/json"},
        )
    return _client


def _user_to_long(user: str | None) -> int:
    """将 Dify 的 user 字符串映射为 Long 型 userId。

    优先提取字符串中的数字部分；若无数字，则使用字符串 hash
    取绝对值并限制在 64 位有符号 Long 范围内。

    Args:
        user: DifyChatRequest.user 字段，如 "user-123"

    Returns:
        正整数 userId（Long 范围）。
    """
    if not user:
        return 0

    import re
    numbers = re.findall(r"\d+", user)
    if numbers:
        return int(numbers[0])

    # hash 映射到正 long 范围
    h = int(hashlib.md5(user.encode()).hexdigest(), 16)
    return h % (2 ** 63 - 1)


async def store_message(
    conversation_id: str,
    user: str | None,
    user_input: str,
    system_output: str,
    *,
    source: int,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    total_tokens: int | None = None,
) -> bool:
    """向自研平台发送消息存储请求。

    本函数为"尽力而为"模式：调用失败仅记录日志，不抛出异常。

    Args:
        conversation_id: 会话唯一标识
        user: DifyChatRequest.user 字段（字符串，将映射为 userId）
        user_input: 用户输入文本
        system_output: 系统最终输出文本
        source: 调用渠道（0=App端, 1=PC端）
        prompt_tokens: 输入 token 数
        completion_tokens: 输出 token 数
        total_tokens: 总 token 数

    Returns:
        True 表示存储成功，False 表示失败。
    """
    settings = get_settings()

    if not settings.message_store_enabled:
        logger.debug("消息存储已禁用，跳过")
        return True

    if not conversation_id or not user_input:
        logger.warning("消息存储参数不完整: conversation_id=%s, user_input_len=%d",
                       conversation_id, len(user_input))
        return False

    user_id = _user_to_long(user)
    payload: dict[str, Any] = {
        "conversationId": conversation_id,
        "userId": user_id,
        "userInput": user_input,
        "systemOutput": system_output,
        "modelId": settings.model_id,
        "source": source,
    }

    # 可选 token 字段
    if prompt_tokens is not None:
        payload["promptTokens"] = prompt_tokens
    if completion_tokens is not None:
        payload["completionTokens"] = completion_tokens
    if total_tokens is not None:
        payload["totalTokens"] = total_tokens

    try:
        client = _get_client()
        response = await client.post(settings.message_store_url, json=payload)
        data = response.json()

        if response.status_code == 200 and data.get("code") == 200:
            logger.info(
                "消息存储成功: conversation_id=%s, user=%s, source=%d, tokens=%s",
                conversation_id, user, source, total_tokens,
            )
            return True
        else:
            logger.warning(
                "消息存储失败: status=%d, code=%s, message=%s",
                response.status_code,
                data.get("code"),
                data.get("message"),
            )
            return False

    except httpx.TimeoutException:
        logger.error("消息存储超时: conversation_id=%s", conversation_id)
        return False
    except Exception as exc:
        logger.error("消息存储异常: conversation_id=%s, error=%s", conversation_id, exc)
        return False
