"""Dify/OpenAI 格式消息到 AgentScope 消息的适配层。"""

from dataclasses import dataclass
from typing import Any

from agentscope.message import AssistantMsg, Msg, UserMsg
from agentscope.state import AgentState
from app.services.query_scope import TaskContext


@dataclass(frozen=True, slots=True)
class ParsedRequestMessages:
    """一次请求中可恢复的历史、当前输入和受控用户上下文。"""

    history: list[Msg]
    current_user: Msg
    user_context: str | None
    task_context: TaskContext | None = None
    has_task_context: bool = False
    agent_state: dict[str, Any] | None = None
    state_version: int = 0
    session_existed: bool = False


def parse_request_messages(
    messages: list[dict[str, Any]],
) -> ParsedRequestMessages:
    """严格拆分持久化历史与当前用户输入。"""
    normalized = list(messages)
    user_context: str | None = None
    stored = None
    session_existed = False

    if normalized and normalized[0].get("role") == "system":
        user_context = _require_text_content(normalized[0])
        stored = normalized[0].get("agent_state")
        session_existed = bool(normalized[0].get("session_existed"))
        normalized = normalized[1:]

    if not normalized or normalized[-1].get("role") != "user":
        raise ValueError("最后一条非 system 消息必须是 user")

    history = [
        _convert_history_message(message)
        for message in normalized[:-1]
    ]
    current_user = UserMsg(
        name="user",
        content=_require_text_content(normalized[-1]),
    )
    task_context = None
    has_task_context = False
    # 只读取服务端持久化的独立字段，不从助手正文或用户文本抽取记忆。
    for message in reversed(normalized[:-1]):
        if message.get("role") == "assistant" and "agent_context" in message:
            has_task_context = True
            task_context = TaskContext.restore(message["agent_context"])
            break
    if stored is not None:
        from app.agent.context import current_task
        task_context = current_task(AgentState.model_validate(stored["state"]))
        has_task_context = True
    return ParsedRequestMessages(
        history=history,
        current_user=current_user,
        user_context=user_context,
        task_context=task_context,
        has_task_context=has_task_context,
        agent_state=stored["state"] if stored is not None else None,
        state_version=stored["version"] if stored is not None else 0,
        session_existed=session_existed,
    )


def _convert_history_message(message: dict[str, Any]) -> Msg:
    """转换一条仅包含纯文本的历史消息。"""
    role = message.get("role")
    content = _require_text_content(message)
    if role == "user":
        return UserMsg(name="user", content=content)
    if role == "assistant":
        return AssistantMsg(name="assistant", content=content)
    raise ValueError(f"历史消息 role 仅支持 user/assistant，收到: {role!r}")


def _require_text_content(message: dict[str, Any]) -> str:
    """读取纯文本 content，拒绝未定义的多模态输入。"""
    content = message.get("content")
    if not isinstance(content, str):
        raise ValueError("消息 content 必须是纯文本字符串")
    return content
