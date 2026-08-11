"""真实 AgentScope Agent→Toolkit→middleware→Dify 集成回归。"""

import json
import unittest

from agentscope.credential import CredentialBase
from agentscope.message import TextBlock, ToolCallBlock
from agentscope.model import ChatModelBase, ChatResponse, ChatUsage
from agentscope.tool import Toolkit
from pydantic import BaseModel

from app.agent.dify_events import DifyEventAdapter
from app.agent.messages import parse_request_messages
from app.agent.runtime import build_energy_agent
from app.agent.tool_specs import EnergyToolSpec
from app.agent.toolkit import EnergyFunctionTool
from app.core.config import Settings


class ToolCallingModel(ChatModelBase):
    """首轮返回真实 ToolCallBlock，若未短路则第二轮返回文本。"""

    class Parameters(BaseModel):
        pass

    def __init__(self) -> None:
        super().__init__(
            credential=CredentialBase(),
            model="fake-tool-model",
            parameters=self.Parameters(),
            stream=False,
            max_retries=0,
        )
        self.calls = 0

    async def _call_api(
        self,
        model_name,
        messages,
        tools=None,
        tool_choice=None,
        **kwargs,
    ) -> ChatResponse:
        self.calls += 1
        if self.calls == 1:
            content = [
                ToolCallBlock(
                    id="call-integration",
                    name="query_report",
                    input='{"province":"湖南省"}',
                ),
            ]
        else:
            content = [TextBlock(text="不应该触发的二次模型回答")]
        return ChatResponse(
            content=content,
            is_last=True,
            usage=ChatUsage(input_tokens=5, output_tokens=2, time=0.01),
        )


class FakeSessionContext:
    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, exc_type, exc, traceback) -> bool:
        return False


class AgentScopeIntegrationTest(unittest.IsolatedAsyncioTestCase):
    async def test_real_tool_lifecycle_direct_answer_and_dify_mapping(self) -> None:
        async def query_report(db, province=None):
            return {
                "success": True,
                "report_content": f"# {province}能效报告",
            }

        tool = EnergyFunctionTool(
            spec=EnergyToolSpec(
                name="query_report",
                description="查询报表",
                input_schema={
                    "type": "object",
                    "properties": {"province": {"type": "string"}},
                },
                handler=query_report,
            ),
            session_factory=FakeSessionContext,
        )
        model = ToolCallingModel()
        parsed = parse_request_messages(
            [{"role": "user", "content": "查询湖南能效报表"}],
        )
        agent = build_energy_agent(
            "conversation-integration",
            parsed,
            settings=Settings(_env_file=None, agent_max_steps=3),
            model=model,
            toolkit=Toolkit(tools=[tool]),
        )
        adapter = DifyEventAdapter(
            conversation_id="conversation-integration",
            message_id="message-integration",
            task_id="task-integration",
            created_at=1,
        )

        payloads = [
            json.loads(envelope["data"])
            async for envelope in adapter.stream(
                agent.reply_stream(parsed.current_user),
            )
        ]

        self.assertEqual(1, model.calls)
        self.assertEqual(
            ["message", "agent_thought", "message", "message_end"],
            [payload["event"] for payload in payloads],
        )
        self.assertEqual("query_report", payloads[1]["tool"])
        self.assertEqual("# 湖南省能效报告", payloads[2]["answer"])
        self.assertEqual("# 湖南省能效报告", adapter.result.answer)
        self.assertTrue(adapter.result.completed)


if __name__ == "__main__":
    unittest.main()
