"""生产三轮追问、工具协议外泄与模型总等待预算回归。"""

import asyncio
import json
import unittest
from unittest.mock import patch

from agentscope.message import TextBlock
from agentscope.model import ChatResponse

from app.agent.dify_events import DifyEventAdapter
from app.agent.messages import parse_request_messages
from app.agent.runtime import build_energy_agent
from app.agent.tool_specs import EnergyToolSpec
from app.core.config import Settings
from app.tools.batch_energy_tool import TOOL_INPUT_SCHEMA
from test_agent_integration import FakeSessionContext, ToolCallingModel


class ExecutionPolicyTest(unittest.IsolatedAsyncioTestCase):
    async def test_bad_native_arguments_retry_clean_request_before_any_tool_execution(self):
        import httpx
        from openai import BadRequestError
        from agentscope.message import ToolCallBlock
        from agentscope.model import ChatUsage

        class GatewayModel(ToolCallingModel):
            async def _call_api(self, model_name, messages, **kwargs):
                self.calls += 1
                # 模拟网关：历史 assistant.function.arguments 非JSON时立即400。
                for message in messages:
                    for block in getattr(message, "content", []):
                        if isinstance(block, ToolCallBlock):
                            try:
                                json.loads(block.input)
                            except json.JSONDecodeError:
                                raise BadRequestError(
                                    "Expecting value: line 3 column 1 (char 2)",
                                    response=httpx.Response(400, request=httpx.Request("POST", "http://gateway/chat/completions")),
                                    body={"error": {"message": "Expecting value: line 3 column 1 (char 2)"}},
                                )
                arguments = "\n\n" if self.calls == 1 and self.malformed else '{"dist_name":"湘潭市"}'
                return ChatResponse(
                    content=[ToolCallBlock(id=f"call-{self.calls}", name="analyze_batch_cells_energy", input=arguments)],
                    is_last=True, usage=ChatUsage(input_tokens=3, output_tokens=2, time=0.01),
                )

        for malformed in (False, True, True):
            with self.subTest(malformed=malformed):
                model = GatewayModel()
                model.malformed = malformed
                executed, result, _ = await self._run("分析湘潭市的节电空间", model)
                calls = 2 if malformed else 1
                self.assertTrue(result.completed)
                self.assertEqual(calls, model.calls)
                self.assertEqual(1, len(executed))
                self.assertEqual("湘潭市", executed[0]["dist_name"])
                self.assertEqual({"prompt_tokens": 3*calls, "completion_tokens": 2*calls, "total_tokens": 5*calls}, result.usage)
                self.assertEqual(3, result.max_input_tokens)

    async def test_streamed_arguments_are_checked_after_complete_not_per_fragment(self):
        from agentscope.message import ToolCallBlock

        class StreamingModel(ToolCallingModel):
            async def _call_api(self, *args, **kwargs):
                self.calls += 1
                attempt = self.calls

                async def stream():
                    arguments = "\n\n" if attempt == 1 else '{"dist_name":"湘潭市"}'
                    for fragment in (arguments[:2], arguments[2:]):
                        yield ChatResponse(content=[ToolCallBlock(id="streamed", name="analyze_batch_cells_energy", input=fragment)], is_last=False)
                    yield ChatResponse(content=[ToolCallBlock(id="streamed", name="analyze_batch_cells_energy", input=arguments)], is_last=True)
                return stream()

        model = StreamingModel()
        executed, result, _ = await self._run("分析湘潭市的节电空间", model)
        self.assertTrue(result.completed)
        self.assertEqual(2, model.calls)
        self.assertEqual(1, len(executed))
        self.assertEqual("湘潭市", executed[0]["dist_name"])

    async def test_clean_retry_shares_existing_total_wait_budget(self):
        from agentscope.message import ToolCallBlock

        class SlowBrokenModel(ToolCallingModel):
            async def _call_api(self, *args, **kwargs):
                self.calls += 1
                await asyncio.sleep(0.03)
                return ChatResponse(content=[ToolCallBlock(id="bad", name="analyze_batch_cells_energy", input="\n\n")], is_last=True)

        model = SlowBrokenModel()
        executed, result, payloads = await self._run("分析湘潭市的节电空间", model, budget=0.05)
        self.assertFalse(result.completed)
        self.assertEqual(2, model.calls)
        self.assertEqual([], executed)
        self.assertEqual("model_timeout", payloads[-1]["code"])

    async def test_repeated_malformed_native_arguments_stop_without_query(self):
        from agentscope.message import ToolCallBlock

        class BrokenModel(ToolCallingModel):
            async def _call_api(self, *args, **kwargs):
                self.calls += 1
                return ChatResponse(content=[ToolCallBlock(id=f"bad-{self.calls}", name="analyze_batch_cells_energy", input=self.broken)], is_last=True)

        for arguments in ("\n\n", "null", "[]", '"not an object"'):
            with self.subTest(arguments=arguments):
                model = BrokenModel()
                model.broken = arguments
                executed, result, payloads = await self._run("分析湘潭市的节电空间", model)
                self.assertEqual([], executed)
                self.assertEqual(2, model.calls)
                self.assertFalse(result.completed)
                self.assertEqual("model_protocol_error", payloads[-1]["code"])

    async def test_fixed_guidance_does_not_call_remote_model(self):
        from agentscope.message import ToolCallBlock
        from app.agent.toolkit import GENERAL_GUIDANCE_TEXT

        class GuidanceModel(ToolCallingModel):
            async def _call_api(self, *args, **kwargs):
                self.calls += 1
                return ChatResponse(content=[ToolCallBlock(id="greeting", name="answer_general_guidance", input="{}")], is_last=True)

        for question in ("你好！", "您好", "你能做什么", "谢谢"):
            with self.subTest(question=question):
                model = GuidanceModel()
                executed, result, _ = await self._run(question, model)
                self.assertTrue(result.completed)
                self.assertEqual(GENERAL_GUIDANCE_TEXT, result.answer)
                self.assertEqual([], executed)
                self.assertEqual(0, model.calls)

    async def test_guidance_does_not_trigger_remote_summary_for_long_history(self):
        from agentscope.message import AssistantMsg, ToolCallBlock
        from app.agent.toolkit import GENERAL_GUIDANCE_TEXT

        class GuidanceModel(ToolCallingModel):
            async def _call_api(self, *args, **kwargs):
                self.calls += 1
                return ChatResponse(content=[ToolCallBlock(id="greeting", name="answer_general_guidance", input="{}")], is_last=True)

            async def generate_structured_output(self, *args, **kwargs):
                raise AssertionError("问候语不能为了压缩旧历史请求摘要模型")

        model = GuidanceModel()
        model.context_size = 1000
        parsed = parse_request_messages([{"role": "user", "content": "你好"}])
        agent = build_energy_agent("long-greeting", parsed, model=model, settings=Settings(_env_file=None))
        agent.state.context = [AssistantMsg(name="energy_agent", content="历史报告" * 3000)]
        adapter = DifyEventAdapter(conversation_id="long-greeting", message_id="m", task_id="t", created_at=1)
        async for _ in adapter.stream(agent.reply_stream(parsed.current_user)):
            pass
        self.assertTrue(adapter.result.completed)
        self.assertEqual(GENERAL_GUIDANCE_TEXT, adapter.result.answer)
        self.assertEqual(0, model.calls)
        self.assertEqual("", agent.state.summary)

    async def test_greeting_prefix_with_business_goal_still_calls_business_model(self):
        model = ToolCallingModel("analyze_batch_cells_energy", {"dist_name": "湘潭市"})
        executed, result, _ = await self._run("你好，请分析湘潭市的节电空间", model)
        self.assertTrue(result.completed)
        self.assertEqual(1, model.calls)
        self.assertEqual("湘潭市", executed[0]["dist_name"])

    async def test_local_guidance_still_emits_persistable_agent_state(self):
        from agentscope.state import AgentState
        from app.agent.runtime import stream_agent_events
        from app.agent.toolkit import GENERAL_GUIDANCE_TEXT

        model = ToolCallingModel()
        adapter = DifyEventAdapter(conversation_id="greeting-state", message_id="m", task_id="t", created_at=1)
        with (
            patch("app.agent.runtime.build_chat_model", return_value=model),
            patch("app.agent.runtime.get_settings", return_value=Settings(_env_file=None)),
        ):
            async for _ in adapter.stream(stream_agent_events([{"role": "user", "content": "你好"}], "greeting-state")):
                pass
        self.assertTrue(adapter.result.completed)
        self.assertEqual(0, model.calls)
        restored = AgentState.model_validate(adapter.result.agent_state)
        texts = "\n".join(message.get_text_content() for message in restored.context)
        self.assertIn("你好", texts)
        self.assertIn(GENERAL_GUIDANCE_TEXT, texts)

    async def test_three_turns_restore_actual_executed_conditions_from_persisted_messages(self):
        from app.api.routes import _save_answer_to_db
        from app.agent.runtime import stream_agent_events
        from unittest.mock import AsyncMock

        saved = []
        executed = []

        async def handler(db, **arguments):
            executed.append(arguments)
            return {"success": True, "report_content": "本轮查询报告"}

        spec = EnergyToolSpec(name="analyze_batch_cells_energy", description="诊断", handler=handler, input_schema=TOOL_INPUT_SCHEMA)
        models = []
        checkpoint = None
        with (
            patch("app.agent.toolkit.TOOL_SPECS", (spec,)),
            patch("app.agent.toolkit._new_session", return_value=FakeSessionContext()),
        ):
            for question in ("湘潭市的节电空间", "株洲市的呢？", "中等扩展"):
                model = ToolCallingModel("analyze_batch_cells_energy", {"dist_name": "全网", "county_name": "全网", "prod_name": "全网"})
                models.append(model)
                adapter = DifyEventAdapter(conversation_id="three-turns", message_id="m", task_id="t", created_at=1)
                with patch("app.agent.runtime.build_chat_model", return_value=model), patch("app.agent.runtime.get_settings", return_value=Settings(_env_file=None)):
                    async for _ in adapter.stream(stream_agent_events([
                        {"role": "system", "content": "", "agent_state": checkpoint, "session_existed": bool(checkpoint)},
                        {"role": "user", "content": question},
                    ], "three-turns")):
                        pass
                self.assertTrue(adapter.result.completed)
                self.assertIsNotNone(adapter.result.agent_state)
                capture = AsyncMock()
                with patch("app.api.routes.save_conversation", capture):
                    await _save_answer_to_db(object(), "three-turns", "test-user", question, adapter.result.answer, adapter.result.agent_state, adapter.result.state_version, adapter.result.session_existed)
                saved.extend(capture.await_args.kwargs["new_messages"])
                checkpoint = {"version": adapter.result.state_version + 1, "state": capture.await_args.kwargs["agent_state"]}
                self.assertEqual("completed", checkpoint["state"]["tasks_context"]["tasks"][-1]["state"])
        self.assertEqual([1, 0, 0], [model.calls for model in models])
        self.assertEqual(["湘潭市", "株洲市", "株洲市"], [item["dist_name"] for item in executed])
        self.assertEqual("moderate", executed[-1]["expansion_level"])

    async def test_cumulative_budget_covers_more_than_one_model_call(self):
        from agentscope.message import ThinkingBlock

        class ThinkingModel(ToolCallingModel):
            async def _call_api(self, *args, **kwargs):
                self.calls += 1
                await asyncio.sleep(0.03)
                return ChatResponse(content=[ThinkingBlock(thinking="继续思考")], is_last=True)

        model = ThinkingModel()
        _, result, payloads = await self._run("湘潭市的节电空间", model, budget=0.05)
        self.assertEqual(2, model.calls)
        self.assertFalse(result.completed)
        self.assertEqual("model_timeout", payloads[-1]["code"])

    async def test_stream_body_wait_is_included_in_budget(self):
        class SlowStreamModel(ToolCallingModel):
            async def _call_api(self, *args, **kwargs):
                async def stream():
                    yield ChatResponse(content=[TextBlock(text="不能提前展示")], is_last=False)
                    await asyncio.sleep(0.3)
                    yield ChatResponse(content=[TextBlock(text="结束")], is_last=True)
                return stream()

        _, result, payloads = await self._run("湘潭市的节电空间", SlowStreamModel(), budget=0.02)
        self.assertEqual("", result.answer)
        self.assertEqual("model_timeout", payloads[-1]["code"])

    async def _run(self, question, model, history=(), budget=60):
        executed = []

        async def handler(db, **arguments):
            executed.append(arguments)
            return {"success": True, "report_content": "株洲市中等扩展报告"}

        spec = EnergyToolSpec(
            name="analyze_batch_cells_energy", description="批量节电分析", handler=handler,
            input_schema=TOOL_INPUT_SCHEMA,
        )
        parsed = parse_request_messages([
            *({"role": "user", "content": value} for value in history),
            {"role": "user", "content": question},
        ])
        adapter = DifyEventAdapter(conversation_id="policy", message_id="m", task_id="t", created_at=1)
        with (
            patch("app.agent.toolkit.TOOL_SPECS", (spec,)),
            patch("app.agent.toolkit._new_session", return_value=FakeSessionContext()),
        ):
            agent = build_energy_agent(
                "policy", parsed, model=model,
                settings=Settings(_env_file=None, agent_model_budget_seconds=budget),
            )
            payloads = [json.loads(item["data"]) async for item in adapter.stream(agent.reply_stream(parsed.current_user))]
        return executed, adapter.result, payloads

    async def test_city_then_strategy_followup_executes_without_model_roundtrip(self):
        model = ToolCallingModel("analyze_batch_cells_energy", {"dist_name": "全网"})
        executed, result, _ = await self._run(
            "中等扩展", model, ("湘潭市的节电空间", "株洲市的呢？"),
        )
        self.assertTrue(result.completed)
        self.assertEqual("株洲市中等扩展报告", result.answer)
        self.assertEqual("株洲市", executed[0]["dist_name"])
        self.assertEqual("moderate", executed[0]["expansion_level"])
        self.assertEqual(0, model.calls)

    async def test_markup_in_model_text_is_not_displayed_or_executed(self):
        markup = "<tool_call><function=analyze_batch_cells_energy><parameter=dist_name>株洲市</tool_call>"

        class MarkupModel(ToolCallingModel):
            async def _call_api(self, *args, **kwargs):
                self.calls += 1
                return ChatResponse(content=[TextBlock(text=markup)], is_last=True)

        executed, result, payloads = await self._run("湘潭市的节电空间", MarkupModel())
        self.assertEqual([], executed)
        self.assertFalse(result.completed)
        self.assertNotIn("<tool_call>", json.dumps(payloads, ensure_ascii=False))
        self.assertEqual("model_protocol_error", payloads[-1]["code"])

    async def test_streamed_markup_is_withheld_before_any_fragment_reaches_page(self):
        class MarkupModel(ToolCallingModel):
            async def _call_api(self, *args, **kwargs):
                async def stream():
                    for text in ("<to", "ol_call>", "<function=analyze_batch_cells_energy>", "</tool_call>"):
                        yield ChatResponse(content=[TextBlock(text=text)], is_last=False)
                return stream()

        executed, result, payloads = await self._run("湘潭市的节电空间", MarkupModel())
        self.assertEqual([], executed)
        self.assertEqual("", result.answer)
        self.assertFalse(result.completed)
        self.assertEqual([""], [item["answer"] for item in payloads if item["event"] == "message"])

    async def test_entire_model_wait_is_bounded_even_when_sdk_swallows_cancellation(self):
        class SlowModel(ToolCallingModel):
            async def _call_api(self, *args, **kwargs):
                await asyncio.sleep(0.3)
                return ChatResponse(content=[TextBlock(text="不应返回")], is_last=True)

        _, result, payloads = await self._run("湘潭市的节电空间", SlowModel(), budget=0.02)
        self.assertFalse(result.completed)
        self.assertEqual("model_timeout", payloads[-1]["code"])
        self.assertEqual("", result.answer)
