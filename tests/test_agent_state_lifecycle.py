"""真实框架自动摘要及跨请求恢复，不用自制字符裁剪代替框架。"""

import asyncio
import unittest
from unittest.mock import patch

from agentscope.message import AssistantMsg, UserMsg
from agentscope.model import StructuredResponse
from agentscope.state import AgentState

from app.agent.messages import parse_request_messages
from app.agent.runtime import build_energy_agent
from app.agent.dify_events import DifyEventAdapter
from app.core.config import Settings
from test_agent_integration import ToolCallingModel


class SummaryModel(ToolCallingModel):
    def __init__(self):
        super().__init__()
        self.summary_calls = 0

    async def count_tokens(self, messages, tools=None, **kwargs):
        return sum(len(message.get_text_content() or "") for message in messages) // 4

    async def generate_structured_output(self, messages, structured_model, **kwargs):
        self.summary_calls += 1
        return StructuredResponse(content={key: "保留的会话摘要" for key in structured_model["properties"]})


class AgentStateLifecycleTest(unittest.IsolatedAsyncioTestCase):
    async def test_native_compression_then_restart_restores_summary_and_reserved_context(self):
        from app.agent.context import snapshot_agent_state
        model = SummaryModel()
        model.context_size = 10000
        history = [UserMsg(name="user", content="湘潭市节电空间")]
        for _ in range(10):
            history.extend([AssistantMsg(name="energy_agent", content="旧报告数值" * 1000), UserMsg(name="user", content="解释上一个结果")])
        parsed = parse_request_messages([{"role": "user", "content": "这个结果为什么这样"}])
        agent = build_energy_agent("state-a", parsed, model=model, settings=Settings(_env_file=None))
        agent.state.context = history
        original_count = len(agent.state.context)
        await agent.compress_context()
        self.assertEqual(1, model.summary_calls)
        self.assertIn("保留的会话摘要", agent.state.summary)
        self.assertLess(len(agent.state.context), original_count)
        saved = snapshot_agent_state(agent.state)
        parsed = parse_request_messages([
            {"role": "system", "content": "", "agent_state": {"version": 3, "state": saved}},
            {"role": "user", "content": "再解释一下这个结果"},
        ])
        restored = build_energy_agent("state-a", parsed, model=SummaryModel(), settings=Settings(_env_file=None))
        self.assertEqual(agent.state.summary, restored.state.summary)
        self.assertEqual([msg.id for msg in agent.state.context], [msg.id for msg in restored.state.context])
        self.assertEqual(3, parsed.state_version)

    async def test_summary_wait_also_obeys_model_budget(self):
        class SlowSummaryModel(SummaryModel):
            async def generate_structured_output(self, *args, **kwargs):
                await asyncio.sleep(0.2)
                return await super().generate_structured_output(*args, **kwargs)

        parsed = parse_request_messages([{"role": "user", "content": "湘潭市的节电空间"}])
        model = SlowSummaryModel()
        model.context_size = 10000
        agent = build_energy_agent("timeout-a", parsed, model=model, settings=Settings(_env_file=None, agent_model_budget_seconds=0.02))
        agent.state.context = [UserMsg(name="user", content="旧记录" * 15000)]
        adapter = DifyEventAdapter(conversation_id="timeout-a", message_id="m", task_id="t", created_at=1)
        async for _ in adapter.stream(agent.reply_stream(parsed.current_user)):
            pass
        self.assertFalse(adapter.result.completed)
        self.assertIsNotNone(adapter.result.error)
        self.assertIn("超时", adapter.result.error)
        self.assertEqual("", agent.state.summary)

    def test_state_cannot_be_restored_under_a_different_conversation(self):
        parsed = parse_request_messages([
            {"role": "system", "content": "", "agent_state": {"version": 1, "state": AgentState(session_id="a").model_dump(mode="json")}},
            {"role": "user", "content": "问题"},
        ])
        with self.assertRaises(ValueError):
            build_energy_agent("b", parsed, model=SummaryModel(), settings=Settings(_env_file=None))
