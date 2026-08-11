"""AgentScope Prompt 与报告直出中间件测试。"""

from types import SimpleNamespace
import unittest

from agentscope.event import (
    ModelCallStartEvent,
    ToolResultEndEvent,
)
from agentscope.message import (
    AssistantMsg,
    ToolResultBlock,
    ToolResultState,
    UserMsg,
)
from agentscope.state import AgentState

try:
    from app.agent.middleware import (
        DirectAnswerMiddleware,
        EnergyPromptMiddleware,
        build_prompt,
    )
except ModuleNotFoundError:
    DirectAnswerMiddleware = None
    EnergyPromptMiddleware = None
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

        self.assertIn("4G/5G 网络能耗报表分析专家", prompt)

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


if __name__ == "__main__":
    unittest.main()
