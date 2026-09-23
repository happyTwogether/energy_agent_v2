"""AgentScope Prompt 与报告直出中间件测试。"""

from types import SimpleNamespace
import json
import unittest
from unittest.mock import AsyncMock

from agentscope.event import (
    ModelCallStartEvent,
    ToolResultEndEvent,
)
from agentscope.message import (
    AssistantMsg,
    ToolCallBlock,
    ToolResultBlock,
    ToolResultState,
    UserMsg,
)
from agentscope.state import AgentState
from agentscope.tool import ToolChoice, Toolkit

try:
    from app.agent.middleware import (
        BusinessToolInputMiddleware,
        DirectAnswerMiddleware,
        EnergyPromptMiddleware,
        GroundedToolChoiceMiddleware,
        build_prompt,
    )
except ModuleNotFoundError:
    BusinessToolInputMiddleware = None
    DirectAnswerMiddleware = None
    EnergyPromptMiddleware = None
    GroundedToolChoiceMiddleware = None
    build_prompt = None


class AgentPromptMiddlewareTest(unittest.IsolatedAsyncioTestCase):
    """验证执行阶段和工具专属总结阶段的 Prompt 路由。"""

    def setUp(self) -> None:
        self.assertIsNotNone(EnergyPromptMiddleware, "Prompt 中间件尚未实现")
        self.assertIsNotNone(build_prompt, "Prompt 构建器尚未实现")

    async def test_switches_to_tool_specific_synthesis_after_success(
        self,
    ) -> None:
        agent = SimpleNamespace(
            toolkit=Toolkit(),
            state=AgentState(
                context=[
                    UserMsg(name="user", content="查询报表"),
                    AssistantMsg(
                        name="energy_agent",
                        content=[
                            ToolResultBlock(
                                id="call-1",
                                name="query_report",
                                output="{}",
                                state=ToolResultState.SUCCESS,
                            ),
                        ],
                    ),
                ],
            ),
        )

        prompt = await EnergyPromptMiddleware(None).on_system_prompt(  # type: ignore[misc]
            agent,
            "base",
        )

        self.assertIn("Python 按固定口径生成的能耗报告", prompt)
        self.assertIn("直接原样返回", prompt)

    async def test_execution_prompt_uses_native_function_calling(self) -> None:
        agent = SimpleNamespace(state=AgentState(context=[]))

        prompt = await EnergyPromptMiddleware("省份=湖南省").on_system_prompt(  # type: ignore[misc]
            agent,
            "base",
        )

        self.assertIn("原生 function calling", prompt)
        self.assertIn("省份=湖南省", prompt)
        self.assertNotIn("<tool>", prompt)
        self.assertNotIn("</tool>", prompt)


class BusinessToolInputMiddlewareTest(unittest.IsolatedAsyncioTestCase):
    """验证用户显式档位和日期优先于模型生成参数。"""

    async def _normalize(self, current_user: str, arguments: dict, history=None) -> dict:
        captured: dict = {}

        async def next_handler(**kwargs):
            captured.update(kwargs)
            if False:
                yield None

        tool_call = ToolCallBlock(
            id="call-normalize",
            name="analyze_batch_cells_energy",
            input=json.dumps(arguments, ensure_ascii=False),
        )
        middleware = BusinessToolInputMiddleware(
            current_user_text=current_user,
            history_user_texts=history or [],
        )
        async for _ in middleware.on_acting(
            SimpleNamespace(),
            {"tool_call": tool_call},
            next_handler,
        ):
            pass
        return json.loads(captured["tool_call"].input)

    async def test_explicit_moderate_overrides_model_conservative(self) -> None:
        normalized = await self._normalize(
            "中等扩展",
            {
                "dist_name": "长沙市",
                "expansion_level": "conservative",
                "stat_time": "2026-09-22",
            },
        )
        self.assertEqual("moderate", normalized["expansion_level"])
        self.assertNotIn("stat_time", normalized)

    async def test_explicit_aggressive_overrides_model_moderate(self) -> None:
        normalized = await self._normalize(
            "激进扩展",
            {
                "dist_name": "长沙市",
                "expansion_level": "moderate",
            },
        )
        self.assertEqual("aggressive", normalized["expansion_level"])

    async def test_explicit_user_date_is_preserved(self) -> None:
        normalized = await self._normalize(
            "查询长沙市2026-09-21中等扩展",
            {
                "dist_name": "长沙市",
                "expansion_level": "conservative",
                "stat_time": "2026-09-22",
            },
        )
        self.assertEqual("moderate", normalized["expansion_level"])
        self.assertEqual("2026-09-21", normalized["stat_time"])

    async def test_switch_followup_inherits_only_user_specified_date(self) -> None:
        normalized = await self._normalize(
            "激进扩展",
            {
                "dist_name": "长沙市",
                "expansion_level": "conservative",
                "stat_time": "2026-09-22",
            },
            history=["查询长沙市2026-09-20节电空间"],
        )
        self.assertEqual("2026-09-20", normalized["stat_time"])


class DirectAnswerMiddlewareTest(unittest.IsolatedAsyncioTestCase):
    """验证 report_content 仅在单工具成功时短路第二次模型调用。"""

    def setUp(self) -> None:
        self.assertIsNotNone(DirectAnswerMiddleware, "报告直出中间件尚未实现")

    async def test_short_circuits_single_direct_answer(self) -> None:
        async def next_handler(**kwargs):
            yield ToolResultEndEvent(
                reply_id="reply-1",
                tool_call_id="call-1",
                state=ToolResultState.SUCCESS,
                metadata={"direct_answer": "# report"},
            )
            yield ModelCallStartEvent(
                reply_id="reply-1",
                model_name="qwen3.6_27b",
            )

        agent = SimpleNamespace(state=AgentState(session_id="session-1"))
        middleware = DirectAnswerMiddleware()
        events = [
            event
            async for event in middleware.on_reply(  # type: ignore[misc]
                agent,
                {},
                next_handler,
            )
        ]

        downstream_called = False

        async def model_next_handler(**kwargs):
            nonlocal downstream_called
            downstream_called = True

        response = await middleware.on_model_call(
            agent,
            {},
            model_next_handler,
        )

        self.assertTrue(any(isinstance(event, ModelCallStartEvent) for event in events))
        self.assertFalse(downstream_called)
        self.assertEqual("# report", response.content[0].text)
        self.assertTrue(response.is_last)

    async def test_multiple_tool_results_continue_to_model(self) -> None:
        async def next_handler(**kwargs):
            yield ToolResultEndEvent(
                reply_id="reply-2",
                tool_call_id="call-1",
                state=ToolResultState.SUCCESS,
                metadata={"direct_answer": "first"},
            )
            yield ToolResultEndEvent(
                reply_id="reply-2",
                tool_call_id="call-2",
                state=ToolResultState.SUCCESS,
                metadata={"direct_answer": "second"},
            )
            yield ModelCallStartEvent(
                reply_id="reply-2",
                model_name="qwen3.6_27b",
            )

        agent = SimpleNamespace(state=AgentState(session_id="session-2"))
        middleware = DirectAnswerMiddleware()
        events = [
            event
            async for event in middleware.on_reply(  # type: ignore[misc]
                agent,
                {},
                next_handler,
            )
        ]

        self.assertTrue(any(isinstance(event, ModelCallStartEvent) for event in events))

        sentinel = object()

        async def model_next_handler(**kwargs):
            return sentinel

        response = await middleware.on_model_call(
            agent,
            {},
            model_next_handler,
        )
        self.assertIs(sentinel, response)


    async def test_duplicate_same_tool_results_use_last_direct_answer(self) -> None:
        async def next_handler(**kwargs):
            for call_id, answer in (("call-a", "中等结果"), ("call-b", "激进结果")):
                yield ToolResultEndEvent(
                    reply_id="reply-dup",
                    tool_call_id=call_id,
                    state=ToolResultState.SUCCESS,
                    metadata={
                        "tool_name": "analyze_batch_cells_energy",
                        "direct_answer": answer,
                    },
                )
            yield ModelCallStartEvent(reply_id="reply-dup", model_name="fake")

        middleware = DirectAnswerMiddleware()
        events = [
            event
            async for event in middleware.on_reply(
                SimpleNamespace(state=AgentState(session_id="session-dup")),
                {},
                next_handler,
            )
        ]
        self.assertTrue(any(isinstance(event, ModelCallStartEvent) for event in events))

        async def model_next_handler(**kwargs):
            raise AssertionError("同名重复报告不应再次调用模型")

        response = await middleware.on_model_call(
            SimpleNamespace(),
            {},
            model_next_handler,
        )
        self.assertEqual("激进结果", response.content[0].text)


class GroundedToolChoiceMiddlewareTest(unittest.IsolatedAsyncioTestCase):
    """验证本轮必须先有工具证据才能生成文本。"""

    def setUp(self) -> None:
        self.assertIsNotNone(
            GroundedToolChoiceMiddleware,
            "工具证据门禁中间件尚未实现",
        )

    async def test_requires_tool_before_current_turn_has_a_result(self) -> None:
        captured: dict = {}

        async def next_handler(**kwargs):
            captured.update(kwargs)
            yield ModelCallStartEvent(reply_id="reply-3", model_name="fake")

        agent = SimpleNamespace(
            toolkit=Toolkit(),
            state=AgentState(
                context=[UserMsg(name="user", content="查询湖南能耗")],
            ),
        )

        events = [
            event
            async for event in GroundedToolChoiceMiddleware().on_reasoning(  # type: ignore[misc]
                agent,
                {"tool_choice": None},
                next_handler,
            )
        ]

        self.assertEqual(1, len(events))
        self.assertIsInstance(captured["tool_choice"], ToolChoice)
        self.assertEqual("required", captured["tool_choice"].mode)

    async def test_business_request_cannot_choose_general_guidance(self) -> None:
        captured: dict = {}
        schemas = [
            {"function": {"name": "query_report"}},
            {"function": {"name": "answer_general_guidance"}},
        ]

        async def next_handler(**kwargs):
            captured.update(kwargs)
            yield ModelCallStartEvent(reply_id="reply-5", model_name="fake")

        agent = SimpleNamespace(
            toolkit=SimpleNamespace(get_tool_schemas=AsyncMock(return_value=schemas)),
            state=AgentState(
                context=[UserMsg(name="user", content="查询湖南能耗")],
            ),
        )

        events = [
            event
            async for event in GroundedToolChoiceMiddleware().on_reasoning(  # type: ignore[misc]
                agent,
                {"tool_choice": None},
                next_handler,
            )
        ]

        self.assertEqual(1, len(events))
        self.assertEqual("required", captured["tool_choice"].mode)
        self.assertEqual(["query_report"], captured["tool_choice"].tools)

    async def test_greeting_is_forced_to_fixed_guidance_tool(self) -> None:
        captured: dict = {}
        schemas = [
            {"function": {"name": "query_report"}},
            {"function": {"name": "answer_general_guidance"}},
        ]

        async def next_handler(**kwargs):
            captured.update(kwargs)
            yield ModelCallStartEvent(reply_id="reply-6", model_name="fake")

        agent = SimpleNamespace(
            toolkit=SimpleNamespace(get_tool_schemas=AsyncMock(return_value=schemas)),
            state=AgentState(
                context=[UserMsg(name="user", content="你好！")],
            ),
        )

        events = [
            event
            async for event in GroundedToolChoiceMiddleware().on_reasoning(  # type: ignore[misc]
                agent,
                {"tool_choice": None},
                next_handler,
            )
        ]

        self.assertEqual(1, len(events))
        self.assertEqual(
            "answer_general_guidance",
            captured["tool_choice"].mode,
        )

    async def test_conversation_explanation_does_not_force_tool(self) -> None:
        captured: dict = {}

        async def next_handler(**kwargs):
            captured.update(kwargs)
            yield ModelCallStartEvent(reply_id="reply-explain", model_name="fake")

        agent = SimpleNamespace(
            toolkit=Toolkit(),
            state=AgentState(
                context=[
                    UserMsg(name="user", content="长沙市节电空间"),
                    AssistantMsg(name="assistant", content="扩展策略：保守扩展"),
                    UserMsg(name="user", content="？那你刚为什么回答保守扩展的"),
                ],
            ),
        )

        events = [
            event
            async for event in GroundedToolChoiceMiddleware().on_reasoning(
                agent,
                {"tool_choice": None},
                next_handler,
            )
        ]

        self.assertEqual(1, len(events))
        self.assertIsNone(captured["tool_choice"])

    async def test_releases_gate_after_current_turn_tool_result(self) -> None:
        captured: dict = {}

        async def next_handler(**kwargs):
            captured.update(kwargs)
            yield ModelCallStartEvent(reply_id="reply-4", model_name="fake")

        agent = SimpleNamespace(
            toolkit=Toolkit(),
            state=AgentState(
                context=[
                    UserMsg(name="user", content="查询湖南能耗"),
                    AssistantMsg(
                        name="energy_agent",
                        content=[
                            ToolResultBlock(
                                id="call-2",
                                name="query_report",
                                output="{}",
                                state=ToolResultState.SUCCESS,
                            ),
                        ],
                    ),
                ],
            ),
        )

        events = [
            event
            async for event in GroundedToolChoiceMiddleware().on_reasoning(  # type: ignore[misc]
                agent,
                {"tool_choice": None},
                next_handler,
            )
        ]

        self.assertEqual(1, len(events))
        self.assertIsNone(captured["tool_choice"])


if __name__ == "__main__":
    unittest.main()
