"""Request-scoped AgentScope 能效智能体运行时。"""

from typing import Any, AsyncGenerator

from agentscope.agent import Agent, InjectionConfig, ReActConfig
from agentscope.event import AgentEvent, EventBase
from agentscope.model import ChatModelBase
from agentscope.state import AgentState
from agentscope.tool import Toolkit

from app.agent.messages import ParsedRequestMessages, parse_request_messages
from app.agent.middleware import DirectAnswerMiddleware, EnergyPromptMiddleware
from app.agent.model import build_chat_model
from app.agent.toolkit import build_toolkit
from app.core.config import Settings, get_settings
from app.prompts.energy_saving import AGENT_EXECUTION_PROMPT


def build_energy_agent(
    conversation_id: str,
    parsed: ParsedRequestMessages,
    settings: Settings | None = None,
    model: ChatModelBase | None = None,
    toolkit: Toolkit | None = None,
) -> Agent:
    """为一次 HTTP 请求创建完全独立的 AgentScope Agent。"""
    runtime_settings = settings or get_settings()
    runtime_model = model if model is not None else build_chat_model(runtime_settings)
    runtime_toolkit = toolkit if toolkit is not None else build_toolkit()
    history = [message.model_copy(deep=True) for message in parsed.history]

    return Agent(
        name="energy_agent",
        system_prompt=AGENT_EXECUTION_PROMPT,
        model=runtime_model,
        toolkit=runtime_toolkit,
        middlewares=[
            DirectAnswerMiddleware(),
            EnergyPromptMiddleware(parsed.user_context),
        ],
        state=AgentState(
            session_id=conversation_id,
            context=history,
        ),
        react_config=ReActConfig(max_iters=runtime_settings.agent_max_steps),
        injection_config=InjectionConfig(
            inject_runtime_state=False,
            timezone="Asia/Shanghai",
        ),
    )


async def stream_agent_events(
    messages: list[dict[str, Any]],
    conversation_id: str,
) -> AsyncGenerator[AgentEvent, None]:
    """解析一次请求，并只暴露 AgentScope 公共 AgentEvent。"""
    parsed = parse_request_messages(messages)
    agent = build_energy_agent(conversation_id, parsed)

    async for event in agent.reply_stream(parsed.current_user):
        if isinstance(event, EventBase):
            yield event
