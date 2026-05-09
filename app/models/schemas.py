"""
数据模型定义模块。

定义请求、响应、SSE 事件等 Pydantic v2 模型，
确保数据校验与序列化的一致性。
"""

from typing import Literal, Any

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    """用户聊天请求模型。"""

    query: str = Field(..., min_length=1, description="用户消息")
    conversation_id: str | None = Field(default=None, description="会话ID，为空则创建新会话")
    user_id: str = Field(default="anonymous", description="用户唯一标识")


class ConversationItem(BaseModel):
    """会话列表中的单个会话项。"""

    conversation_id: str
    title: str
    updated_at: str
    message_count: int


class ConversationListResponse(BaseModel):
    """会话列表响应。"""

    conversations: list[ConversationItem]


class ConversationDetailResponse(BaseModel):
    """单个会话详情响应。"""

    conversation_id: str
    title: str
    messages: list[dict[str, Any]]


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


class DifyChatRequest(BaseModel):
    """Dify 对话应用请求模型。

    对标 Dify Chat API: POST /v1/chat-messages

    请求示例：
    {
        "inputs": {},
        "query": "查询长沙市5G数据",
        "response_mode": "streaming",
        "conversation_id": null,
        "user": "user-123"
    }
    """

    inputs: dict[str, Any] = Field(default_factory=dict, description="工作流变量，key-value 对象")
    query: str = Field(..., min_length=1, description="用户消息")
    response_mode: Literal["blocking", "streaming"] = Field(default="blocking", description="响应模式")
    conversation_id: str | None = Field(default=None, description="会话ID，空则创建新会话")
    user: str = Field(..., description="用户唯一标识")


