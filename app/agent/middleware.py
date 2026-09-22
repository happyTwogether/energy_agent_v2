"""AgentScope 能效 Prompt 与工具报告直出中间件。"""

from datetime import datetime, timedelta
from typing import Any, AsyncGenerator
from zoneinfo import ZoneInfo

from agentscope.event import (
    AgentEvent,
    ModelCallStartEvent,
    ToolResultEndEvent,
)
from agentscope.message import TextBlock, ToolResultBlock, ToolResultState
from agentscope.middleware import MiddlewareBase
from agentscope.model import ChatResponse, ChatUsage
from agentscope.tool import ToolChoice

from app.core.config import get_settings
from app.agent.toolkit import GENERAL_GUIDANCE_TOOL_NAME
from app.prompts.energy_saving import AGENT_EXECUTION_PROMPT, get_synthesis_prompt


_GENERAL_GUIDANCE_REQUESTS = frozenset({
    "hi",
    "hello",
    "你好",
    "您好",
    "嗨",
    "在吗",
    "谢谢",
    "好的",
    "帮助",
    "你能做什么",
    "你可以做什么",
    "有什么功能",
    "怎么用",
    "如何使用",
})
_IGNORED_GENERAL_CHARACTERS = str.maketrans("", "", " \t\r\n，。！？,.!?")


class EnergyPromptMiddleware(MiddlewareBase):
    """根据当前 AgentState 在执行与工具专属总结 Prompt 间切换。"""

    def __init__(self, user_context: str | None) -> None:
        self._user_context = user_context

    async def on_system_prompt(self, agent: Any, current_prompt: str) -> str:
        tool_name = _latest_successful_tool_name(agent.state.context)
        phase = "synthesis" if tool_name else "execution"
        return build_prompt(
            phase=phase,
            tool_name=tool_name,
            user_context=self._user_context,
        )


class GroundedToolChoiceMiddleware(MiddlewareBase):
    """本轮尚无工具证据时禁止模型直接作答。"""

    async def on_reasoning(
        self,
        agent: Any,
        input_kwargs: dict[str, Any],
        next_handler: Any,
    ) -> AsyncGenerator[AgentEvent, None]:
        next_kwargs = dict(input_kwargs)
        current_choice = next_kwargs.get("tool_choice")
        if not _has_tool_result(agent.state.context) and (
            current_choice is None or current_choice.mode == "auto"
        ):
            next_kwargs["tool_choice"] = await _required_tool_choice(agent)

        async for event in next_handler(**next_kwargs):
            yield event


class DirectAnswerMiddleware(MiddlewareBase):
    """单个成功工具已给出 report_content 时跳过第二次模型调用。"""

    def __init__(self) -> None:
        self._pending_direct_answer: str | None = None

    async def on_reply(
        self,
        agent: Any,
        input_kwargs: dict[str, Any],
        next_handler: Any,
    ) -> AsyncGenerator[AgentEvent, None]:
        completed_results: list[ToolResultEndEvent] = []
        downstream = next_handler(**input_kwargs)

        async for event in downstream:
            if isinstance(event, ToolResultEndEvent):
                completed_results.append(event)
                yield event
                continue

            if isinstance(event, ModelCallStartEvent) and completed_results:
                self._pending_direct_answer = _single_direct_answer(
                    completed_results,
                )
                completed_results.clear()

            yield event

    async def on_model_call(
        self,
        agent: Any,
        input_kwargs: dict[str, Any],
        next_handler: Any,
    ) -> Any:
        """在模型边界返回本地结果，保持 AgentScope 生命周期完整。"""
        direct_answer = self._pending_direct_answer
        self._pending_direct_answer = None
        if direct_answer is None:
            return await next_handler(**input_kwargs)
        return ChatResponse(
            content=[TextBlock(text=direct_answer)],
            is_last=True,
            usage=ChatUsage(
                input_tokens=0,
                output_tokens=0,
                time=0.0,
            ),
        )


def build_prompt(
    phase: str,
    tool_name: str | None,
    user_context: str | None,
) -> str:
    """构建包含生产角色、北京时间和受控用户上下文的 Prompt。"""
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    values = {
        "current_date": now.strftime("%Y-%m-%d"),
        "yesterday": (now - timedelta(days=1)).strftime("%Y-%m-%d"),
    }
    template = (
        get_synthesis_prompt(tool_name)
        if phase == "synthesis"
        else AGENT_EXECUTION_PROMPT
    )
    prompt = template.format(**values)

    settings = get_settings()
    if settings.agent_system_prompt:
        prompt = f"{settings.agent_system_prompt}\n\n{prompt}"
    if user_context:
        prompt = f"{prompt}\n\n# User Context\n{user_context}"
    return prompt


def _latest_successful_tool_name(context: list[Any]) -> str | None:
    """从上下文中读取最近一次成功工具结果。"""
    for message in reversed(context):
        for block in reversed(message.content):
            if (
                isinstance(block, ToolResultBlock)
                and block.state == ToolResultState.SUCCESS
            ):
                return block.name
    return None


def _has_tool_result(context: list[Any]) -> bool:
    """判断当前请求上下文是否已有工具返回。"""
    return any(
        isinstance(block, ToolResultBlock)
        for message in context
        for block in message.content
    )


async def _required_tool_choice(agent: Any) -> ToolChoice:
    """为本轮首次推理构建强制工具白名单。"""
    schemas = await agent.toolkit.get_tool_schemas()
    tool_names = [item["function"]["name"] for item in schemas]
    if (
        GENERAL_GUIDANCE_TOOL_NAME in tool_names
        and _is_general_guidance_request(agent.state.context)
    ):
        return ToolChoice(mode=GENERAL_GUIDANCE_TOOL_NAME)

    business_tools = [
        name for name in tool_names if name != GENERAL_GUIDANCE_TOOL_NAME
    ]
    return ToolChoice(mode="required", tools=business_tools or None)


def _is_general_guidance_request(context: list[Any]) -> bool:
    """仅允许明确、简短的非业务请求进入固定兜底。"""
    for message in reversed(context):
        if message.role == "user":
            normalized = message.get_text_content().lower().translate(
                _IGNORED_GENERAL_CHARACTERS,
            )
            return normalized in _GENERAL_GUIDANCE_REQUESTS
    return False


def _single_direct_answer(
    results: list[ToolResultEndEvent],
) -> str | None:
    """仅允许一个成功工具结果触发直出。"""
    if len(results) != 1 or results[0].state != ToolResultState.SUCCESS:
        return None
    answer = results[0].metadata.get("direct_answer")
    return answer if isinstance(answer, str) and answer else None
