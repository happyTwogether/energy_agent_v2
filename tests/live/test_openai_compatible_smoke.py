"""OpenAI-compatible 模型协议在线冒烟测试。"""

import inspect
import json
import os
import unittest

import httpx
from agentscope.message import SystemMsg, TextBlock, ToolCallBlock, UserMsg
from agentscope.model import ChatResponse, OpenAIChatModel
from agentscope.tool import ToolChoice

from app.agent.model import build_chat_model


RUN_LIVE = os.getenv("RUN_LLM_SMOKE") == "1"


def _build_smoke_model(
    *,
    stream: bool,
) -> tuple[OpenAIChatModel, httpx.AsyncClient]:
    """创建模型，并透传可选的供应商中立 smoke 请求字段。"""
    model = build_chat_model(stream=stream)
    http_client = httpx.AsyncClient(
        timeout=model.client_kwargs["timeout"],
    )
    model.client_kwargs = {"http_client": http_client}
    extra_body_json = os.getenv("LLM_SMOKE_EXTRA_BODY_JSON")
    if extra_body_json:
        extra_body = json.loads(extra_body_json)
        if not isinstance(extra_body, dict):
            raise AssertionError("LLM_SMOKE_EXTRA_BODY_JSON 必须是 JSON object")
        model.extra_body = extra_body
    return model, http_client


@unittest.skipUnless(RUN_LIVE, "设置 RUN_LLM_SMOKE=1 后执行在线模型测试")
class OpenAICompatibleSmokeTest(unittest.IsolatedAsyncioTestCase):
    """验证非流式、流式和 function calling 协议路径。"""

    def setUp(self) -> None:
        self.assertTrue(
            os.getenv("LLM_API_KEY"),
            "在线测试要求由父进程安全注入 LLM_API_KEY",
        )

    async def test_non_stream_text(self) -> None:
        model, http_client = _build_smoke_model(stream=False)
        self.addAsyncCleanup(http_client.aclose)

        response = await model(
            messages=[UserMsg(name="user", content="只回答：连接正常")],
        )

        self.assertIsInstance(response, ChatResponse)
        text = "".join(
            block.text
            for block in response.content
            if isinstance(block, TextBlock)
        )
        self.assertTrue(text.strip())

    async def test_stream_text(self) -> None:
        model, http_client = _build_smoke_model(stream=True)
        self.addAsyncCleanup(http_client.aclose)

        response = await model(
            messages=[UserMsg(name="user", content="用一句话说明连接正常")],
        )

        self.assertTrue(inspect.isasyncgen(response))
        chunks = [chunk async for chunk in response]
        visible_text = "".join(
            block.text
            for chunk in chunks
            for block in chunk.content
            if isinstance(block, TextBlock) and not chunk.is_last
        )
        self.assertTrue(visible_text.strip())
        self.assertTrue(any(chunk.is_last for chunk in chunks))

    async def test_required_tool_call_schema(self) -> None:
        model, http_client = _build_smoke_model(stream=False)
        self.addAsyncCleanup(http_client.aclose)
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "lookup_demo_cell",
                    "description": "查找演示小区",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "cell_name": {"type": "string"},
                            "network": {
                                "type": "string",
                                "enum": ["4G", "5G"],
                            },
                            "metric_names": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "note": {
                                "anyOf": [
                                    {"type": "string"},
                                    {"type": "null"},
                                ],
                            },
                        },
                        "required": [
                            "cell_name",
                            "network",
                            "metric_names",
                            "note",
                        ],
                    },
                },
            },
        ]

        response = await model(
            messages=[
                SystemMsg(name="system", content="按要求调用工具。"),
                UserMsg(
                    name="user",
                    content=(
                        "查询演示小区的5G流量和能耗指标，"
                        "metric_names 传 cell_traffic 和 power_usage，"
                        "note 传 null"
                    ),
                ),
            ],
            tools=tools,
            tool_choice=ToolChoice(mode="lookup_demo_cell"),
        )

        self.assertIsInstance(response, ChatResponse)
        calls = [
            block
            for block in response.content
            if isinstance(block, ToolCallBlock)
        ]
        self.assertEqual(1, len(calls))
        arguments = json.loads(calls[0].input)
        self.assertIsInstance(arguments, dict)
        self.assertIsInstance(arguments.get("cell_name"), str)
        self.assertIn(arguments.get("network"), {"4G", "5G"})
        self.assertIsInstance(arguments.get("metric_names"), list)
        self.assertIn(arguments.get("note"), {None, ""})

    async def test_parallel_tool_calls(self) -> None:
        model, http_client = _build_smoke_model(stream=False)
        self.addAsyncCleanup(http_client.aclose)
        tools = [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": f"查询{name}演示值",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "province": {"type": "string"},
                        },
                        "required": ["province"],
                    },
                },
            }
            for name in ("query_lte_demo", "query_nr_demo")
        ]

        response = await model(
            messages=[
                SystemMsg(
                    name="system",
                    content="必须在同一轮并行调用两个工具，不要输出文本。",
                ),
                UserMsg(name="user", content="同时查询湖南省4G和5G演示值"),
            ],
            tools=tools,
            tool_choice=ToolChoice(mode="required"),
        )

        self.assertIsInstance(response, ChatResponse)
        calls = [
            block
            for block in response.content
            if isinstance(block, ToolCallBlock)
        ]
        self.assertEqual(
            {"query_lte_demo", "query_nr_demo"},
            {call.name for call in calls},
        )


if __name__ == "__main__":
    unittest.main()
