"""框架工作状态的公开序列化边界；不自制历史裁剪或语义压缩。"""

from agentscope.event import EventBase
from agentscope.state import AgentState, Task
from typing import Any

from app.services.query_scope import TaskContext


class AgentStateEvent(EventBase):
    """正常完成后仅供持久化层消费，不输出到页面。"""

    agent_state: dict[str, Any]
    expected_version: int = 0
    session_existed: bool = False


def snapshot_agent_state(state: AgentState) -> dict[str, Any]:
    """保留可续接字段，不把上轮步数、权限或文件缓存带到新请求。"""
    return state.model_dump(mode="json", include={"session_id", "summary", "context", "tasks_context"})


def restore_agent_state(payload: dict[str, Any], conversation_id: str) -> AgentState:
    state = AgentState.model_validate(payload)
    if state.session_id != conversation_id:
        raise ValueError("AgentState 会话标识不一致")
    return state


def current_task(state: AgentState) -> TaskContext | None:
    for task in reversed(state.tasks_context.tasks):
        if task.metadata.get("kind") == "energy_analysis":
            return TaskContext.restore(task.metadata.get("conditions"))
    return None


def update_task(state: AgentState, task: TaskContext | None, *, status: str, outcome: str) -> None:
    """使用框架 Task 记录当前业务任务，不把条件存进问答正文。"""
    if task is None:
        state.tasks_context.tasks = []
        return
    previous = next((item for item in state.tasks_context.tasks if item.subject == task.goal), None)
    active = Task(
        subject=task.goal, description=task.tool_name, state=status,
        metadata={"kind": "energy_analysis", "conditions": task.snapshot(), "last_outcome": outcome},
        **({"id": previous.id, "created_at": previous.created_at} if previous else {}),
    )
    state.tasks_context.tasks = [active]
