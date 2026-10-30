"""Request-scoped AgentScope 能效智能体运行时。"""

from typing import Any, AsyncGenerator

from agentscope.agent import Agent, ContextConfig, InjectionConfig, ReActConfig
from agentscope.event import AgentEvent, EventBase, ReplyEndEvent
from agentscope.types import ReplyFinishedReason
from agentscope.model import ChatModelBase
from agentscope.state import AgentState
from agentscope.tool import Toolkit

from app.agent.messages import ParsedRequestMessages, parse_request_messages
from app.agent.context import AgentStateEvent, restore_agent_state, snapshot_agent_state
from app.agent.execution_policy import ExecutionPolicyMiddleware
from app.agent.middleware import (
    DirectAnswerMiddleware,
    EnergyPromptMiddleware,
    GroundedToolChoiceMiddleware,
)
from app.agent.model import build_chat_model
from app.agent.toolkit import build_toolkit
from app.core.config import Settings, get_settings
from app.prompts.energy_saving import AGENT_EXECUTION_PROMPT
from app.services.query_scope import ToolRequestContext


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
    request_context = ToolRequestContext(
        current_user_text=parsed.current_user.get_text_content(),
        history_user_texts=tuple(
            message.get_text_content() for message in parsed.history if message.role == "user" and not parsed.has_task_context
        ),
        prior_task=parsed.task_context,
    )
    runtime_toolkit = toolkit if toolkit is not None else build_toolkit(request_context=request_context)
    state = restore_agent_state(parsed.agent_state, conversation_id) if parsed.agent_state is not None else AgentState(
        session_id=conversation_id, context=[message.model_copy(deep=True) for message in parsed.history],
    )

    return Agent(
        name="energy_agent",
        system_prompt=AGENT_EXECUTION_PROMPT,
        model=runtime_model,
        toolkit=runtime_toolkit,
        middlewares=[
            DirectAnswerMiddleware(),
            ExecutionPolicyMiddleware(request_context, runtime_settings.agent_model_budget_seconds),
            GroundedToolChoiceMiddleware(request_context),
            EnergyPromptMiddleware(parsed.user_context, request_context),
        ],
        state=state,
        context_config=ContextConfig(
            trigger_ratio=runtime_settings.agent_context_trigger_ratio,
            reserve_ratio=runtime_settings.agent_context_reserve_ratio,
            tool_result_limit=runtime_settings.agent_tool_result_token_limit,
            compression_prompt=(
                "请生成可用于继续当前能效分析会话的摘要。保留用户目标、明确约束、地区、厂家、绝对日期、"
                "扩展档位、已完成工具及报告链接、待澄清事项。相对日期依据原消息时间解释，无法确定须标记未知。"
                "结构化任务条件由服务器独立保存，不要猜测条件；历史业务数值必须注明统计日期和来源，不能作为本轮实时结果。"
                "不要把工具错误或未执行计划写成成功结果；不要输出或复述工具调用标签。"
            ),
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
        if isinstance(event, ReplyEndEvent) and event.finished_reason == ReplyFinishedReason.COMPLETED:
            yield AgentStateEvent(agent_state=snapshot_agent_state(agent.state), expected_version=parsed.state_version, session_existed=parsed.session_existed)
        if isinstance(event, EventBase):
            yield event
