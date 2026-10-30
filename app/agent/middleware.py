"""AgentScope 能效 Prompt、工具路由与工具输入规范化中间件。"""

import json
import re
from typing import Any, AsyncGenerator

from agentscope.event import (
    AgentEvent,
    ModelCallStartEvent,
    ReplyEndEvent,
    TextBlockDeltaEvent,
    TextBlockEndEvent,
    TextBlockStartEvent,
    ToolCallStartEvent,
    ToolResultEndEvent,
)
from agentscope.message import AssistantMsg, TextBlock, ToolResultBlock, ToolResultState
from agentscope.middleware import MiddlewareBase
from agentscope.tool import ToolChoice
from agentscope.types import ReplyFinishedReason

from app.core.config import get_settings
from app.core.logging import get_logger
from app.agent.toolkit import GENERAL_GUIDANCE_TOOL_NAME
from app.prompts.energy_saving import AGENT_EXECUTION_PROMPT, get_synthesis_prompt
from app.services.query_scope import (
    ToolRequestContext,
    energy_analysis_route,
)

logger = get_logger("agent_middleware")

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
_DIMENSION_PATTERN = re.compile(
    r"设备厂家|设备厂商|厂家|厂商|设备商|频段|站点类型|站型|区域类型|区域|地市|市州",
)
_DIMENSION_VALUE_PATTERN = re.compile(
    r"有哪些|有什么|能看哪些|可以看哪些|可选值",
)
_REPORT_REQUEST_PATTERN = re.compile(
    r"(?:能耗|节耗电|节电).{0,8}报表|"
    r"报表.{0,8}(?:能耗|节耗电|节电)",
)


class EnergyPromptMiddleware(MiddlewareBase):
    """根据当前 AgentState 在执行与工具专属总结 Prompt 间切换。"""

    def __init__(self, user_context: str | None, request_context: ToolRequestContext | None = None) -> None:
        self._user_context = user_context
        self._request_context = request_context

    async def on_system_prompt(self, agent: Any, current_prompt: str) -> str:
        tool_name = _latest_successful_tool_name(agent.state.context)
        phase = "synthesis" if tool_name else "execution"
        prompt = build_prompt(
            phase=phase,
            tool_name=tool_name,
            user_context=self._user_context,
        )
        if self._request_context and self._request_context.task:
            snapshot = self._request_context.task.snapshot()
            prompt += "\n\n当前会话任务条件（仅为查询条件，历史结果不是本轮数据证据）：\n" + json.dumps(snapshot, ensure_ascii=False)
        return prompt


class GroundedToolChoiceMiddleware(MiddlewareBase):
    """本轮尚无工具证据时禁止模型直接作答。"""

    def __init__(self, request_context: ToolRequestContext | None = None):
        self._request_context = request_context

    async def on_reasoning(
        self,
        agent: Any,
        input_kwargs: dict[str, Any],
        next_handler: Any,
    ) -> AsyncGenerator[AgentEvent, None]:
        next_kwargs = dict(input_kwargs)
        current_choice = next_kwargs.get("tool_choice")
        is_auto = current_choice is None or current_choice.mode == "auto"
        if is_auto:
            if _latest_current_turn_tool_failed(agent.state.context):
                next_kwargs["tool_choice"] = ToolChoice(mode="none")
            elif not _has_tool_result(agent.state.context):
                if _is_conversation_explanation_request(agent.state.context):
                    next_kwargs["tool_choice"] = ToolChoice(mode="none")
                else:
                    next_kwargs["tool_choice"] = await _required_tool_choice(agent, self._request_context)

        async for event in next_handler(**next_kwargs):
            yield event


class DirectAnswerMiddleware(MiddlewareBase):
    """完整工具批次已有确定性报告时立即结束，不再占用下一轮推理。"""

    async def on_reply(
        self,
        agent: Any,
        input_kwargs: dict[str, Any],
        next_handler: Any,
    ) -> AsyncGenerator[AgentEvent, None]:
        completed_results: list[ToolResultEndEvent] = []
        tool_call_ids: set[str] = set()
        downstream = next_handler(**input_kwargs)

        async for event in downstream:
            if isinstance(event, ModelCallStartEvent):
                tool_call_ids.clear()
            elif isinstance(event, ToolCallStartEvent):
                tool_call_ids.add(event.tool_call_id)

            if isinstance(event, ToolResultEndEvent):
                completed_results.append(event)
                yield event
                completed_ids = {result.tool_call_id for result in completed_results}
                if (
                    tool_call_ids
                    and tool_call_ids.issubset(completed_ids)
                    and not input_kwargs.get("structured_schema")
                ):
                    answer = _select_direct_answer(completed_results)
                    if len(completed_results) == 1:
                        # 范围缺失属于服务端已校验的澄清，不再要求模型解释错误。
                        answer = answer or completed_results[0].metadata.get("clarification")
                    if answer:
                        # 工具结果在 EndEvent 前已写入 AgentState；关闭底层循环，
                        # 通过公共事件完成报告，避免最后一步成功后仍被轮数上限截断。
                        await downstream.aclose()
                        agent.state.append_context(agent.name, [TextBlock(text=answer)])
                        reply_id = event.reply_id
                        block_id = f"{reply_id}-direct-answer"
                        logger.info("agent_direct_answer completed_tools=%s", len(completed_results))
                        yield TextBlockStartEvent(reply_id=reply_id, block_id=block_id)
                        yield TextBlockDeltaEvent(reply_id=reply_id, block_id=block_id, delta=answer)
                        yield TextBlockEndEvent(reply_id=reply_id, block_id=block_id)
                        yield ReplyEndEvent(
                            session_id=agent.state.session_id, reply_id=reply_id,
                            finished_reason=ReplyFinishedReason.COMPLETED,
                        )
                        yield AssistantMsg(
                            id=reply_id, name=agent.name, content=answer,
                            finished_reason=ReplyFinishedReason.COMPLETED,
                        )
                        return
                continue

            yield event


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
    for message in reversed(_current_turn_context(context)):
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
        for message in _current_turn_context(context)
        for block in message.content
    )


def _current_turn_context(context: list[Any]) -> list[Any]:
    """历史工具结果不属于本轮证据，也不能决定本轮总结阶段。"""
    latest_user_index = max(
        (
            index
            for index, message in enumerate(context)
            if message.role == "user"
        ),
        default=0,
    )
    return context[latest_user_index:]


def _latest_current_turn_tool_failed(context: list[Any]) -> bool:
    """当前轮工具失败后禁止改调其他工具。"""
    results = [
        block
        for message in _current_turn_context(context)
        for block in message.content
        if isinstance(block, ToolResultBlock)
    ]
    return bool(results and results[-1].state == ToolResultState.ERROR)


async def _required_tool_choice(agent: Any, request_context: ToolRequestContext | None = None) -> ToolChoice:
    """为本轮首次推理构建强制工具白名单。"""
    schemas = await agent.toolkit.get_tool_schemas()
    tool_names = [item["function"]["name"] for item in schemas]
    if (
        GENERAL_GUIDANCE_TOOL_NAME in tool_names
        and _is_general_guidance_request(agent.state.context)
    ):
        return ToolChoice(mode=GENERAL_GUIDANCE_TOOL_NAME)
    if (
        "query_business_data" in tool_names
        and _is_dimension_value_request(agent.state.context)
    ):
        return ToolChoice(mode="query_business_data")
    user_texts = [message.get_text_content() for message in agent.state.context if message.role == "user"]
    if user_texts:
        request = request_context or ToolRequestContext(user_texts[-1], tuple(user_texts[:-1]))
        energy_tool = request.task.tool_name if request.task else energy_analysis_route(request.current_user_text)
        if energy_tool in tool_names:
            return ToolChoice(mode=energy_tool)
    if (
        "query_report" in tool_names
        and _is_report_request(agent.state.context)
    ):
        return ToolChoice(mode="query_report")

    business_tools = [
        name for name in tool_names if name != GENERAL_GUIDANCE_TOOL_NAME
    ]
    return ToolChoice(mode="required", tools=business_tools or None)


def _is_general_guidance_request(context: list[Any]) -> bool:
    """仅允许明确、简短的非业务请求进入固定兜底。"""
    return is_general_guidance_text(_latest_user_text(context))


def is_general_guidance_text(text: str) -> bool:
    """路由与本地执行共用已有的完整短句判定，业务问题不能前缀匹配。"""
    return text.lower().translate(_IGNORED_GENERAL_CHARACTERS) in _GENERAL_GUIDANCE_REQUESTS


def _latest_user_text(context: list[Any]) -> str:
    """读取当前上下文中最近一条用户文本。"""
    for message in reversed(context):
        if message.role == "user":
            return message.get_text_content()
    return ""


def _is_conversation_explanation_request(context: list[Any]) -> bool:
    """识别只解释上一轮回答、而不要求新数据的追问。"""
    text = _latest_user_text(context).lower()
    if re.search(r"重新(?:查询|查|分析)|重查|再查|再看|改查|换成|切换", text):
        return False
    return (
        any(marker in text for marker in _CONVERSATION_REFERENCE_MARKERS)
        and any(marker in text for marker in _CONVERSATION_EXPLANATION_MARKERS)
    )


def _is_dimension_value_request(context: list[Any]) -> bool:
    """维度值枚举不是完整报表，始终交给通用数据查询。"""
    user_texts = [
        message.get_text_content().lower().translate(_IGNORED_GENERAL_CHARACTERS)
        for message in context
        if message.role == "user"
    ]
    if not user_texts:
        return False
    if _is_dimension_value_text(user_texts[-1]):
        return True
    is_correction = "只问有哪些" in user_texts[-1]
    return is_correction and any(
        _is_dimension_value_text(text)
        for text in reversed(user_texts[:-1])
    )


def _is_dimension_value_text(text: str) -> bool:
    return bool(
        _DIMENSION_PATTERN.search(text)
        and _DIMENSION_VALUE_PATTERN.search(text)
    )


def _is_report_request(context: list[Any]) -> bool:
    """明确要求能耗报表时限定到固定报表工具。"""
    for message in reversed(context):
        if message.role == "user":
            normalized = message.get_text_content().lower().translate(
                _IGNORED_GENERAL_CHARACTERS,
            )
            return bool(_REPORT_REQUEST_PATTERN.search(normalized))
    return False


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
