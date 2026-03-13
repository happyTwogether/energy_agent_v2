"""
数据模型定义模块。

定义请求、响应、SSE 事件等 Pydantic v2 模型，
确保数据校验与序列化的一致性。
"""

from typing import Literal, Any

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    """用户聊天请求模型（OpenAI 格式）。"""

    messages: list[dict[str, Any]] = Field(..., min_items=1, description="对话消息列表，OpenAI 格式")


class ChatResponse(BaseModel):
    """聊天响应模型（OpenAI 格式）。"""

    choices: list[dict[str, Any]] = Field(..., description="响应选择列表")
    usage: dict[str, Any] | None = Field(default=None, description="Token 使用统计")


class StreamEvent(BaseModel):
    """SSE 流式事件模型。

    SSE 输出格式约定：

    # token 事件示例：
    # event: token
    # data: {"event_type": "token", "data": "你好"}

    # tool_call 事件示例：
    # event: tool_call
    # data: {"event_type": "tool_call", "data": {"tool": "get_weather", "args": {"city": "北京"}}}

    # tool_result 事件示例：
    # event: tool_result
    # data: {"event_type": "tool_result", "data": {"tool": "get_weather", "result": {...}}}

    # final_answer 事件示例：
    # event: final_answer
    # data: {"event_type": "final_answer", "data": "北京今天晴，25°C"}

    # error 事件示例：
    # event: error
    # data: {"event_type": "error", "data": "工具调用超时"}
    """

    event_type: Literal[
        "token",
        "tool_call",
        "tool_result",
        "agent_step",
        "final_answer",
        "error",
    ] = Field(..., description="事件类型")
    data: dict | str = Field(..., description="事件数据")
