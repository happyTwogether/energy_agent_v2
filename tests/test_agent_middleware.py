"""AgentScope Prompt 与报告直出中间件测试。"""

from types import SimpleNamespace
import json
import unittest
from unittest.mock import AsyncMock

from agentscope.event import (
    ModelCallStartEvent,
    ToolCallStartEvent,
    TextBlockDeltaEvent,
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
        DirectAnswerMiddleware,
        EnergyPromptMiddleware,
        GroundedToolChoiceMiddleware,
        build_prompt,
    )
except ModuleNotFoundError:
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

    async def test_previous_turn_tool_does_not_select_current_synthesis_prompt(self):
        agent = SimpleNamespace(state=AgentState(context=[
            UserMsg(name="user", content="长沙能耗报表"),
            AssistantMsg(name="assistant", content=[ToolResultBlock(
                id="old", name="query_report", output="{}", state=ToolResultState.SUCCESS,
            )]),
            UserMsg(name="user", content="湘潭市的节电空间"),
        ]))
        prompt = await EnergyPromptMiddleware(None).on_system_prompt(agent, "base")
        self.assertIn("原生 function calling", prompt)


class AnalysisArgumentsTest(unittest.IsolatedAsyncioTestCase):
    """验证用户显式档位和日期优先于模型生成参数。"""

    async def _normalize(self, current_user: str, arguments: dict, history=None) -> dict:
        from app.services.query_scope import ToolRequestContext, normalize_analysis_arguments
        return normalize_analysis_arguments(
            "analyze_batch_cells_energy", arguments,
            ToolRequestContext(current_user, tuple(history or [])),
        )

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
    """真实事件顺序：完整批次成功即结束，无须等待下轮模型。"""

    async def _run(self, results):
        async def downstream(**kwargs):
            for index, _, _ in results:
                yield ToolCallStartEvent(reply_id="r", tool_call_id=index, tool_call_name="query_report")
            for index, tool, answer in results:
                yield ToolResultEndEvent(
                    reply_id="r", tool_call_id=index, state=ToolResultState.SUCCESS,
                    metadata={"tool_name": tool, "direct_answer": answer},
                )
            yield ModelCallStartEvent(reply_id="r", model_name="fake")
        return [event async for event in DirectAnswerMiddleware().on_reply(
            SimpleNamespace(name="energy_agent", state=AgentState(session_id="s")), {}, downstream,
        )]

    async def test_short_circuits_single_direct_answer(self):
        events = await self._run([("a", "query_report", "# report")])
        self.assertFalse(any(isinstance(event, ModelCallStartEvent) for event in events))
        self.assertEqual("# report", "".join(event.delta for event in events if isinstance(event, TextBlockDeltaEvent)))

    async def test_multiple_tool_results_continue_to_model(self):
        events = await self._run([("a", "query_report", "first"), ("b", "query_anomaly", "second")])
        self.assertTrue(any(isinstance(event, ModelCallStartEvent) for event in events))
        self.assertFalse(any(isinstance(event, TextBlockDeltaEvent) for event in events))

    async def test_duplicate_same_tool_results_use_last_direct_answer(self):
        events = await self._run([("a", "query_report", "中等结果"), ("b", "query_report", "激进结果")])
        self.assertFalse(any(isinstance(event, ModelCallStartEvent) for event in events))
        self.assertEqual("激进结果", "".join(event.delta for event in events if isinstance(event, TextBlockDeltaEvent)))


class GroundedToolChoiceMiddlewareTest(unittest.IsolatedAsyncioTestCase):
    """验证本轮必须先有工具证据才能生成文本。"""

    def setUp(self) -> None:
        self.assertIsNotNone(
            GroundedToolChoiceMiddleware,
            "工具证据门禁中间件尚未实现",
        )

    async def test_new_data_request_is_not_mistaken_for_explaining_old_answer(self):
        for text in (
            "刚才你查的是全省，重新查询湘潭市的节电空间",
            "生成湘潭市的节电空间报表",
        ):
            captured = {}

            async def next_handler(**kwargs):
                captured.update(kwargs)
                if False:
                    yield None

            agent = SimpleNamespace(
                toolkit=SimpleNamespace(get_tool_schemas=AsyncMock(return_value=[
                    {"function": {"name": "analyze_batch_cells_energy"}},
                    {"function": {"name": "query_report"}},
                ])),
                state=AgentState(context=[
                    UserMsg(name="user", content="长沙能耗报表"),
                    AssistantMsg(name="assistant", content=[ToolResultBlock(
                        id="old", name="query_report", output="{}", state=ToolResultState.SUCCESS,
                    )]),
                    UserMsg(name="user", content=text),
                ]),
            )
            async for _ in GroundedToolChoiceMiddleware().on_reasoning(
                agent, {"tool_choice": None}, next_handler,
            ):
                pass
            with self.subTest(text=text):
                self.assertIsNotNone(captured["tool_choice"])
                self.assertEqual("analyze_batch_cells_energy", captured["tool_choice"].mode)

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
        self.assertEqual("none", captured["tool_choice"].mode)

    async def test_vendor_value_list_is_forced_to_business_data(self) -> None:
        captured: dict = {}
        schemas = [
            {"function": {"name": "query_business_data"}},
            {"function": {"name": "query_report"}},
            {"function": {"name": "resolve_cell_cgi"}},
        ]

        async def next_handler(**kwargs):
            captured.update(kwargs)
            yield ModelCallStartEvent(reply_id="reply-vendor", model_name="fake")

        agent = SimpleNamespace(
            toolkit=SimpleNamespace(get_tool_schemas=AsyncMock(return_value=schemas)),
            state=AgentState(
                context=[UserMsg(name="user", content="那邵阳有哪些厂家可以看")],
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
        self.assertEqual("query_business_data", captured["tool_choice"].mode)

    async def test_vendor_value_correction_keeps_business_data_route(self) -> None:
        captured: dict = {}
        schemas = [
            {"function": {"name": "query_business_data"}},
            {"function": {"name": "query_report"}},
            {"function": {"name": "resolve_cell_cgi"}},
        ]

        async def next_handler(**kwargs):
            captured.update(kwargs)
            yield ModelCallStartEvent(reply_id="reply-correction", model_name="fake")

        agent = SimpleNamespace(
            toolkit=SimpleNamespace(get_tool_schemas=AsyncMock(return_value=schemas)),
            state=AgentState(context=[
                UserMsg(name="user", content="那邵阳有哪些厂家可以看"),
                UserMsg(name="user", content="？我没问你，我只问有哪些"),
            ]),
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
        self.assertEqual("query_business_data", captured["tool_choice"].mode)

    async def test_explicit_energy_report_is_forced_to_report_tool(self) -> None:
        captured: dict = {}
        schemas = [
            {"function": {"name": "query_business_data"}},
            {"function": {"name": "query_report"}},
            {"function": {"name": "resolve_cell_cgi"}},
        ]

        async def next_handler(**kwargs):
            captured.update(kwargs)
            yield ModelCallStartEvent(reply_id="reply-report", model_name="fake")

        agent = SimpleNamespace(
            toolkit=SimpleNamespace(get_tool_schemas=AsyncMock(return_value=schemas)),
            state=AgentState(
                context=[UserMsg(name="user", content="全省中兴能耗报表")],
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
        self.assertEqual("query_report", captured["tool_choice"].mode)

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

    async def test_failed_tool_forces_answer_instead_of_switching_tools(self) -> None:
        captured: dict = {}

        async def next_handler(**kwargs):
            captured.update(kwargs)
            yield ModelCallStartEvent(reply_id="reply-failed", model_name="fake")

        agent = SimpleNamespace(
            toolkit=Toolkit(),
            state=AgentState(
                context=[
                    UserMsg(name="user", content="邵阳有哪些厂家"),
                    AssistantMsg(
                        name="energy_agent",
                        content=[
                            ToolResultBlock(
                                id="call-failed",
                                name="query_business_data",
                                output='{"success": false}',
                                state=ToolResultState.ERROR,
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
        self.assertEqual("none", captured["tool_choice"].mode)


if __name__ == "__main__":
    unittest.main()
