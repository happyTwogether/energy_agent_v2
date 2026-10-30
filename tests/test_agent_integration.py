"""真实 AgentScope Agent→Toolkit→middleware→Dify 集成回归。"""

import json
import unittest
from datetime import datetime
from unittest.mock import patch

from agentscope.credential import CredentialBase
from agentscope.message import TextBlock, ThinkingBlock, ToolCallBlock, ToolResultBlock
from agentscope.model import ChatModelBase, ChatResponse, ChatUsage
from agentscope.tool import Toolkit
from pydantic import BaseModel

from app.agent.dify_events import DifyEventAdapter
from app.agent.messages import parse_request_messages
from app.agent.runtime import build_energy_agent
from app.agent.tool_specs import EnergyToolSpec
from app.agent.toolkit import EnergyFunctionTool
from app.tools import batch_energy_tool
from app.core.config import Settings


class ToolCallingModel(ChatModelBase):
    """首轮返回真实 ToolCallBlock，若未短路则第二轮返回文本。"""

    class Parameters(BaseModel):
        pass

    def __init__(self, tool_name="query_report", arguments=None) -> None:
        super().__init__(
            credential=CredentialBase(),
            model="fake-tool-model",
            parameters=self.Parameters(),
            stream=False,
            max_retries=0,
        )
        self.calls = 0
        self.tool_choices = []
        self.tool_name = tool_name
        self.arguments = arguments if arguments is not None else {"province": "湖南省"}

    async def _call_api(
        self,
        model_name,
        messages,
        tools=None,
        tool_choice=None,
        **kwargs,
    ) -> ChatResponse:
        self.calls += 1
        self.tool_choices.append(tool_choice)
        if self.calls == 1:
            content = [
                ToolCallBlock(
                    id="call-integration",
                    name=self.tool_name,
                    input=json.dumps(self.arguments, ensure_ascii=False),
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
    async def test_report_survives_tool_success_at_iteration_limit(self):
        class LateToolModel(ToolCallingModel):
            async def _call_api(self, *args, **kwargs):
                if self.calls < 3:
                    self.calls += 1
                    return ChatResponse(
                        content=[ThinkingBlock(thinking="正在选择工具")], is_last=True,
                        usage=ChatUsage(input_tokens=3, output_tokens=1, time=0.01),
                    )
                # 第四次模型调用才给出工具，第五步工具成功；不应再要求第六步。
                self.calls += 1
                return ChatResponse(
                    content=[ToolCallBlock(id="late-report", name="query_report", input='{"province":"湖南省"}')],
                    is_last=True, usage=ChatUsage(input_tokens=3, output_tokens=1, time=0.01),
                )

        async def query_report(db, province=None):
            return {"success": True, "report_content": "# 湘潭市报告已生成"}

        toolkit = Toolkit(tools=[EnergyFunctionTool(
            spec=EnergyToolSpec(
                name="query_report", description="报表", handler=query_report,
                input_schema={"type": "object", "properties": {"province": {"type": "string"}}},
            ), session_factory=FakeSessionContext,
        )])
        parsed = parse_request_messages([{"role": "user", "content": "湘潭市的能耗报表"}])
        model = LateToolModel()
        agent = build_energy_agent(
            "last-step", parsed, model=model, toolkit=toolkit,
            settings=Settings(_env_file=None, agent_max_steps=5),
        )
        adapter = DifyEventAdapter(conversation_id="last-step", message_id="m", task_id="t", created_at=1)
        payloads = [json.loads(item["data"]) async for item in adapter.stream(agent.reply_stream(parsed.current_user))]

        self.assertEqual("# 湘潭市报告已生成", adapter.result.answer)
        self.assertTrue(adapter.result.completed)
        self.assertNotIn("error", [item["event"] for item in payloads])
        self.assertEqual(4, model.calls)
        self.assertTrue(any(
            isinstance(block, ToolResultBlock)
            for message in agent.state.context for block in message.content
        ))
        self.assertIn("# 湘潭市报告已生成", agent.state.context[-1].get_text_content())

    async def test_direct_report_completes_with_single_model_step_budget(self):
        async def query_report(db, province=None):
            return {"success": True, "report_content": "报告正文"}

        toolkit = Toolkit(tools=[EnergyFunctionTool(
            spec=EnergyToolSpec(
                name="query_report", description="报表", handler=query_report,
                input_schema={"type": "object", "properties": {"province": {"type": "string"}}},
            ), session_factory=FakeSessionContext,
        )])
        parsed = parse_request_messages([{"role": "user", "content": "查询能耗报表"}])
        agent = build_energy_agent(
            "one-step", parsed, model=ToolCallingModel(), toolkit=toolkit,
            settings=Settings(_env_file=None, agent_max_steps=1),
        )
        final_message = await agent.reply(parsed.current_user)
        self.assertEqual("报告正文", final_message.get_text_content())

    async def test_direct_answer_waits_for_all_tools_and_does_not_hide_failure(self):
        class BatchModel(ToolCallingModel):
            async def _call_api(self, *args, **kwargs):
                self.calls += 1
                blocks = (
                    [ToolCallBlock(id=name, name=name, input="{}") for name in ("query_report", "query_anomaly")]
                    if self.calls == 1 else [TextBlock(text="异常诊断失败，请检查数据")]
                )
                return ChatResponse(content=blocks, is_last=True)

        executed = []

        async def report(db):
            executed.append("report")
            return {"success": True, "report_content": "不能提前返回的第一份报告"}

        async def anomaly(db):
            executed.append("anomaly")
            return {"success": False, "error": "诊断数据缺失"}

        toolkit = Toolkit(tools=[EnergyFunctionTool(
            spec=EnergyToolSpec(
                name=name, description=name, handler=handler,
                input_schema={"type": "object", "properties": {}},
            ), session_factory=FakeSessionContext,
        ) for name, handler in (("query_report", report), ("query_anomaly", anomaly))])
        parsed = parse_request_messages([{"role": "user", "content": "查询报表并诊断异常"}])
        model = BatchModel()
        agent = build_energy_agent("mixed", parsed, model=model, toolkit=toolkit)
        result = await agent.reply(parsed.current_user)
        self.assertCountEqual(["report", "anomaly"], executed)
        self.assertEqual(2, model.calls)
        self.assertEqual("异常诊断失败，请检查数据", result.get_text_content())

    async def test_city_report_and_visible_tool_input_use_executed_scope(self):
        async def fetch_rows(sql, params):
            query = str(sql)
            if "MAX(stat_time)" in query:
                return [{"max_date": datetime(2026, 10, 28)}]
            if "jd_cell_expansion_day" in query:
                return [{
                    "cgi": "460-00-1-1", "dist_name": "湘潭市", "county_name": "雨湖区",
                    "prod_name": "华为", "cell_name": "测试小区", "stat_time": datetime(2026, 10, 28),
                }]
            return []

        parsed = parse_request_messages([{"role": "user", "content": "湘潭市的节电空间"}])
        model = ToolCallingModel("analyze_batch_cells_energy", {"dist_name": "全网"})
        adapter = DifyEventAdapter(conversation_id="city", message_id="m", task_id="t", created_at=1)
        with (
            patch("app.agent.toolkit._new_session", return_value=FakeSessionContext()),
            patch.object(batch_energy_tool, "fetch_rows", side_effect=fetch_rows),
            patch.object(batch_energy_tool, "export_sheets_to_excel", return_value="/downloads/test.xlsx"),
        ):
            agent = build_energy_agent("city", parsed, model=model)
            payloads = [json.loads(item["data"]) async for item in adapter.stream(agent.reply_stream(parsed.current_user))]

        self.assertTrue(adapter.result.completed)
        self.assertIn("地市=湘潭市", adapter.result.answer)
        self.assertEqual(1, model.calls)
        thought = next(item for item in payloads if item["event"] == "agent_thought")
        self.assertEqual("湘潭市", json.loads(thought["tool_input"])["dist_name"])

    async def test_city_energy_request_cannot_execute_province_query(self) -> None:
        for wrong_arguments in ({}, {"dist_name": "全网"}, {"dist_name": "长沙市"}):
            with self.subTest(arguments=wrong_arguments):
                queries = []

                async def fetch_rows(sql, params):
                    if "MAX(stat_time)" in str(sql):
                        return [{"max_date": datetime(2026, 10, 28)}]
                    queries.append((str(sql), dict(params)))
                    return []

                parsed = parse_request_messages([
                    {"role": "user", "content": "湘潭市的节电空间"},
                ])
                model = ToolCallingModel("analyze_batch_cells_energy", wrong_arguments)
                with (
                    patch("app.agent.toolkit._new_session", return_value=FakeSessionContext()),
                    patch.object(batch_energy_tool, "fetch_rows", side_effect=fetch_rows),
                ):
                    agent = build_energy_agent(
                        "city-scope", parsed, model=model,
                        settings=Settings(_env_file=None, agent_max_steps=3),
                    )
                    events = [event async for event in agent.reply_stream(parsed.current_user)]

                self.assertTrue(events)
                self.assertEqual("analyze_batch_cells_energy", model.tool_choices[0].mode)
                self.assertEqual(2, len(queries))
                for sql, params in queries:
                    self.assertIn("dist_name = :dist_name", sql)
                    self.assertEqual("湘潭市", params["dist_name"])

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
        self.assertEqual("required", model.tool_choices[0].mode)
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
