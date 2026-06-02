"""
会话记忆模块 — 当前对话的上下文管理。

借鉴 OpenClaw LCM 的无损上下文思想：
所有消息不可变追加，通过压缩策略控制上下文大小。
"""

from typing import Any

from app.core.logging import get_logger

logger = get_logger("session_memory")

# 保留最近 N 轮完整对话（user + assistant 对）
_MAX_FULL_ROUNDS = 4
# 超过保留轮数的历史只保留摘要
_MAX_SUMMARY_CHARS = 500


class SessionMemory:
    """当前对话的上下文管理器。

    不引入新的存储依赖，纯内存操作。
    职责：管理单次会话中的消息历史，决定哪些内容传递给 LLM。
    """

    def __init__(self) -> None:
        self._messages: list[dict[str, Any]] = []
        self._summary: str = ""

    def load(self, messages: list[dict[str, Any]]) -> None:
        """从持久化存储加载历史消息。"""
        self._messages = list(messages)

    def append(self, message: dict[str, Any]) -> None:
        """追加一条消息。"""
        self._messages.append(message)

    def get_context_messages(self) -> list[dict[str, Any]]:
        """获取应传递给 LLM 的消息列表。

        策略（借鉴 OpenClaw LCM 的比例压缩思想）：
        1. 保留最近 _MAX_FULL_ROUNDS 轮完整对话
        2. 更早的历史压缩为一段摘要文本
        3. 工具返回结果超过阈值时截断
        """
        if len(self._messages) <= _MAX_FULL_ROUNDS * 2:
            return list(self._messages)

        # 计算截断点：保留最近 N 轮
        # 一轮 = user + assistant（可能还有 tool 消息）
        rounds = 0
        cutoff = len(self._messages)
        for i in range(len(self._messages) - 1, -1, -1):
            msg = self._messages[i]
            if msg.get("role") == "user":
                rounds += 1
                if rounds >= _MAX_FULL_ROUNDS:
                    cutoff = i
                    break

        older = self._messages[:cutoff]
        recent = self._messages[cutoff:]

        # 将旧消息压缩为摘要
        summary = self._summarize_older_messages(older)

        result: list[dict[str, Any]] = []
        if summary:
            result.append({
                "role": "system",
                "content": f"[历史对话摘要]\n{summary}",
            })
        result.extend(recent)
        return result

    def _summarize_older_messages(self, messages: list[dict[str, Any]]) -> str:
        """将旧消息压缩为简短摘要（非 LLM 方式，纯提取）。"""
        parts: list[str] = []
        for msg in messages:
            role = msg.get("role", "?")
            content = msg.get("content", "")
            if isinstance(content, str) and content.strip():
                short = content[:200].replace("\n", " ")
                parts.append(f"[{role}]: {short}")
                if len(" ".join(parts)) > _MAX_SUMMARY_CHARS:
                    break

        if not parts:
            return ""
        return " | ".join(parts)

    def clear(self) -> None:
        """清空所有消息。"""
        self._messages.clear()
        self._summary = ""

    @property
    def message_count(self) -> int:
        return len(self._messages)
