"""Request-scoped AgentScope 运行时测试。"""

from unittest.mock import patch
import unittest

from pydantic import BaseModel

from agentscope.credential import CredentialBase
from agentscope.event import (
    ModelCallEndEvent,
    ReplyEndEvent,
    ReplyStartEvent,
    TextBlockDeltaEvent,
)
from agentscope.message import TextBlock
from agentscope.model import ChatModelBase, ChatResponse, ChatUsage
from agentscope.tool import Toolkit

from app.agent.messages import parse_request_messages
from app.core.config import Settings

try:
    from app.agent.runtime import build_energy_agent, stream_agent_events
except ModuleNotFoundError:
    build_energy_agent = None
    stream_agent_events = None


class FakeChatModel(ChatModelBase):
    """返回固定文本的最小 AgentScope 对话模型。"""

    class Parameters(BaseModel):
        pass

    def __init__(self) -> None:
        super().__init__(
            credential=CredentialBase(),
            model="fake-model",
            parameters=self.Parameters(),
            stream=False,
            max_retries=0,
        )

    async def _call_api(
        self,
        model_name,
        messages,
        tools=None,
        tool_choice=None,
        **kwargs,
    ) -> ChatResponse:
        return ChatResponse(
            content=[TextBlock(text="测试回答")],
            is_last=True,
            usage=ChatUsage(input_tokens=7, output_tokens=2, time=0.01),
        )


class AgentRuntimeTest(unittest.IsolatedAsyncioTestCase):
    """验证请求隔离与真实 AgentScope 事件生命周期。"""

    def setUp(self) -> None:
        self.assertIsNotNone(build_energy_agent, "AgentScope 运行时尚未实现")
        self.assertIsNotNone(stream_agent_events, "AgentScope 事件流尚未实现")

    async def test_agents_with_same_conversation_are_request_scoped(self) -> None:
        parsed = parse_request_messages(
            [
                {"role": "user", "content": "历史问题"},
                {"role": "assistant", "content": "历史回答"},
                {"role": "user", "content": "当前问题"},
            ],
        )
        settings = Settings(_env_file=None, agent_max_steps=3)

        first = build_energy_agent(  # type: ignore[misc]
            "conversation-1",
            parsed,
            settings=settings,
            model=FakeChatModel(),
            toolkit=Toolkit(),
        )
        second = build_energy_agent(  # type: ignore[misc]
            "conversation-1",
            parsed,
            settings=settings,
            model=FakeChatModel(),
            toolkit=Toolkit(),
        )

        self.assertIsNot(first.state, second.state)
        self.assertIsNot(first.state.context, second.state.context)
        self.assertEqual("conversation-1", first.state.session_id)
        self.assertEqual(2, len(first.state.context))
        self.assertNotIn(
            "当前问题",
            [message.get_text_content() for message in first.state.context],
        )

    async def test_streams_public_agent_events_with_fake_model(self) -> None:
        messages = [{"role": "user", "content": "你好"}]
        fake_model = FakeChatModel()

        with (
            patch("app.agent.runtime.build_chat_model", return_value=fake_model),
            patch("app.agent.runtime.build_toolkit", return_value=Toolkit()),
        ):
            events = [
                event
                async for event in stream_agent_events(  # type: ignore[misc]
                    messages,
                    "conversation-2",
                )
            ]

        lifecycle = [type(event) for event in events]
        self.assertIn(ReplyStartEvent, lifecycle)
        self.assertIn(TextBlockDeltaEvent, lifecycle)
        self.assertIn(ModelCallEndEvent, lifecycle)
        self.assertIn(ReplyEndEvent, lifecycle)
        self.assertLess(lifecycle.index(ReplyStartEvent), lifecycle.index(TextBlockDeltaEvent))
        self.assertLess(lifecycle.index(TextBlockDeltaEvent), lifecycle.index(ModelCallEndEvent))
        self.assertLess(lifecycle.index(ModelCallEndEvent), lifecycle.index(ReplyEndEvent))
        self.assertEqual(
            ["测试回答"],
            [
                event.delta
                for event in events
                if isinstance(event, TextBlockDeltaEvent)
            ],
        )


if __name__ == "__main__":
    unittest.main()
