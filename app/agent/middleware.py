"""AgentScope 能效 Prompt、工具路由与工具输入规范化中间件。"""

import json
import re
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
_ENERGY_ANALYSIS_TOOL_NAMES = frozenset({
    "analyze_batch_cells_energy",
    "analyze_single_cell_energy",
})
_EXPANSION_LEVEL_MARKERS = (
    ("aggressive", ("激进扩展", "激进", "500m", "低于500m")),
    ("moderate", ("中等扩展", "中等", "400m", "低于400m")),
    ("conservative", ("保守扩展", "保守", "300m", "低于300m")),
)
_CONVERSATION_REFERENCE_MARKERS = (
    "你刚",
    "刚才你",
    "上一轮",
    "上轮",
    "上一次",
    "前面你",
    "你前面",
    "之前你",
    "你的回答",
    "你的回复",
    "刚才回答",
    "刚才回复",
    "这个结果",
    "这个结论",
    "上面的结果",
    "上述结果",
)
_CONVERSATION_EXPLANATION_MARKERS = (
    "为什么",
    "为何",
    "怎么会",
    "解释",
    "是什么意思",
    "回答",
    "回复",
    "查的",
    "查询的",
    "用的",
    "调用",
)
_EXPLICIT_DATE_PATTERN = re.compile(
    r"(?<!\d)(20\d{2})[年\-/.](\d{1,2})[月\-/.](\d{1,2})日?",
)
_MONTH_DAY_PATTERN = re.compile(r"(?<!\d)(\d{1,2})月(\d{1,2})日")


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
        if not _has_tool_result(agent.state.context):
            if _is_conversation_explanation_request(agent.state.context):
                next_kwargs["tool_choice"] = ToolChoice(mode="none")
            elif current_choice is None or current_choice.mode == "auto":
                next_kwargs["tool_choice"] = await _required_tool_choice(agent)

        async for event in next_handler(**next_kwargs):
            yield event


class BusinessToolInputMiddleware(MiddlewareBase):
    """用当前用户原话校正节电工具的枚举和日期参数。"""

    def __init__(
        self,
        current_user_text: str,
        history_user_texts: list[str],
    ) -> None:
        self._current_user_text = current_user_text
        self._history_user_texts = history_user_texts

    async def on_acting(
        self,
        agent: Any,
        input_kwargs: dict[str, Any],
        next_handler: Any,
    ) -> AsyncGenerator[AgentEvent, None]:
        tool_call = input_kwargs.get("tool_call")
        tool_name = getattr(tool_call, "name", "")
        if tool_name not in _ENERGY_ANALYSIS_TOOL_NAMES:
            async for event in next_handler(**input_kwargs):
                yield event
            return

        try:
            arguments = json.loads(getattr(tool_call, "input", "") or "{}")
        except (TypeError, json.JSONDecodeError):
            async for event in next_handler(**input_kwargs):
                yield event
            return
        if not isinstance(arguments, dict):
            async for event in next_handler(**input_kwargs):
                yield event
            return

        explicit_level = _detect_explicit_expansion_level(
            self._current_user_text,
        )
        if explicit_level:
            arguments["expansion_level"] = explicit_level

        requested_date = _resolve_requested_date(self._current_user_text)
        if requested_date:
            arguments["stat_time"] = requested_date
        elif _is_expansion_switch_followup(self._current_user_text):
            inherited_date = _latest_requested_date(
                self._history_user_texts,
            )
            if inherited_date:
                arguments["stat_time"] = inherited_date
            else:
                arguments.pop("stat_time", None)
        else:
            arguments.pop("stat_time", None)

        normalized_call = tool_call.model_copy(
            update={
                "input": json.dumps(
                    arguments,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            },
        )
        next_kwargs = dict(input_kwargs)
        next_kwargs["tool_call"] = normalized_call
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
                self._pending_direct_answer = _select_direct_answer(
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
    template = (
        get_synthesis_prompt(tool_name)
        if phase == "synthesis"
        else AGENT_EXECUTION_PROMPT
    )
    prompt = template.format()

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


def _latest_user_text(context: list[Any]) -> str:
    """读取当前上下文中最近一条用户文本。"""
    for message in reversed(context):
        if message.role == "user":
            return message.get_text_content()
    return ""


def _is_conversation_explanation_request(context: list[Any]) -> bool:
    """识别只解释上一轮回答、而不要求新数据的追问。"""
    text = _latest_user_text(context).lower()
    return (
        any(marker in text for marker in _CONVERSATION_REFERENCE_MARKERS)
        and any(marker in text for marker in _CONVERSATION_EXPLANATION_MARKERS)
    )


def _detect_explicit_expansion_level(text: str) -> str | None:
    """从用户原话识别明确指定的三档节电扩展策略。"""
    normalized = text.lower().replace(" ", "")
    for level, markers in _EXPANSION_LEVEL_MARKERS:
        if any(marker in normalized for marker in markers):
            return level
    return None


def _is_expansion_switch_followup(text: str) -> bool:
    """短句指定扩展档位时视为上一轮节电分析的切档追问。"""
    return (
        _detect_explicit_expansion_level(text) is not None
        and len(text.strip()) <= 32
    )


def _resolve_requested_date(text: str) -> str | None:
    """仅在用户明确提及日期时返回标准 YYYY-MM-DD。"""
    match = _EXPLICIT_DATE_PATTERN.search(text)
    if match:
        year, month, day = map(int, match.groups())
        try:
            return datetime(year, month, day).strftime("%Y-%m-%d")
        except ValueError:
            return None

    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    month_day = _MONTH_DAY_PATTERN.search(text)
    if month_day:
        month, day = map(int, month_day.groups())
        try:
            return datetime(now.year, month, day).strftime("%Y-%m-%d")
        except ValueError:
            return None

    if "前天" in text:
        return (now - timedelta(days=2)).strftime("%Y-%m-%d")
    if "昨天" in text or "昨日" in text:
        return (now - timedelta(days=1)).strftime("%Y-%m-%d")
    if "今天" in text or "今日" in text:
        return now.strftime("%Y-%m-%d")
    return None


def _latest_requested_date(history_user_texts: list[str]) -> str | None:
    """切换扩展档位时继承最近一次由用户明确指定的日期。"""
    for text in reversed(history_user_texts):
        if requested_date := _resolve_requested_date(text):
            return requested_date
    return None


def _select_direct_answer(
    results: list[ToolResultEndEvent],
) -> str | None:
    """单工具直出；同名工具重复返回时取最后一次，避免暴露内部总结。"""
    if not results or any(
        result.state != ToolResultState.SUCCESS for result in results
    ):
        return None

    answers = [
        result.metadata.get("direct_answer")
        for result in results
    ]
    if not all(isinstance(answer, str) and answer for answer in answers):
        return None
    if len(results) == 1:
        return answers[0]

    tool_names = [
        result.metadata.get("tool_name")
        for result in results
    ]
    if tool_names[0] and all(name == tool_names[0] for name in tool_names):
        return answers[-1]
    return None
