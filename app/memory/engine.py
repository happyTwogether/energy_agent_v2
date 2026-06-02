"""
记忆引擎 — 统一入口。

整合会话记忆和持久记忆，
为 Agent Engine 提供统一的记忆读写接口。
"""

from typing import Any

from app.core.logging import get_logger
from app.memory.session import SessionMemory
from app.memory.persistent import persistent_memory as pm

logger = get_logger("memory_engine")

# 记忆摘要最大长度（注入到 system prompt 中）
_MAX_MEMORY_CONTEXT_CHARS = 800


class MemoryEngine:
    """统一记忆引擎。

    借鉴 Hermes 的分层记忆思想：
    - SessionMemory: 当前对话上下文（层1）
    - PersistentMemory: 跨会话知识（层2）
    """

    def __init__(self) -> None:
        self.session = SessionMemory()

    # ── 会话记忆 ──

    def load_history(self, messages: list[dict[str, Any]]) -> None:
        """加载历史消息到会话记忆。"""
        self.session.load(messages)

    def get_context_messages(self) -> list[dict[str, Any]]:
        """获取应传递给 LLM 的上下文消息。"""
        return self.session.get_context_messages()

    def record_turn(
        self,
        user_query: str,
        assistant_response: str,
        tool_results: list[dict[str, Any]] | None = None,
    ) -> None:
        """记录一轮完整的交互。"""
        self.session.append({"role": "user", "content": user_query})
        for tr in (tool_results or []):
            self.session.append({
                "role": "tool",
                "tool_call_id": tr.get("call_id", "unknown"),
                "content": str(tr.get("result", ""))[:_MAX_MEMORY_CONTEXT_CHARS],
            })
        self.session.append({"role": "assistant", "content": assistant_response})

    # ── 持久记忆 ──

    async def get_persistent_context(self, user_id: str) -> str:
        """获取应注入 system prompt 的持久记忆摘要。

        Returns:
            格式化后的记忆文本，可直接追加到 system prompt。
        """
        try:
            all_facts = await pm.get_all(user_id)
            if not all_facts:
                return ""

            parts: list[str] = []
            for key, value in all_facts.items():
                if isinstance(value, dict):
                    parts.append(f"- {key}: {json_compact(value)}")
                else:
                    parts.append(f"- {key}: {value}")

            text = "\n".join(parts)
            if len(text) > _MAX_MEMORY_CONTEXT_CHARS:
                text = text[:_MAX_MEMORY_CONTEXT_CHARS] + "\n...(记忆已截断)"
            return text
        except Exception as exc:
            logger.warning("获取持久记忆失败: %s", exc)
            return ""

    async def learn_from_turn(
        self,
        user_id: str,
        query: str,
        answer: str,
    ) -> None:
        """从当前对话轮次中学习（异步，不阻塞响应）。"""
        try:
            await pm.extract_and_save(user_id, query, answer)
        except Exception as exc:
            logger.warning("自动学习失败: %s", exc)

    # ── 重置 ──

    def reset(self) -> None:
        """重置会话记忆（每次新请求不需要，仅用于清理）。"""
        self.session.clear()


def json_compact(obj: Any) -> str:
    """紧凑 JSON 序列化。"""
    import json
    return json.dumps(obj, ensure_ascii=False, default=str)
