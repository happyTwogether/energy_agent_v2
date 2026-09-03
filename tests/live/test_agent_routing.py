"""生产同款内网模型的首轮工具路由评测。"""

import json
import os
from pathlib import Path
import unittest

import httpx
from agentscope.message import SystemMsg, ToolCallBlock, UserMsg
from agentscope.model import ChatResponse
from agentscope.tool import ToolChoice

from app.agent.middleware import build_prompt
from app.agent.model import build_chat_model
from app.agent.tool_specs import TOOL_SPECS


RUN_LIVE = os.getenv("RUN_AGENT_ROUTING_EVAL") == "1"
CASES_PATH = Path(__file__).parents[1] / "fixtures" / "agent_routing_cases.json"


def _load_cases() -> list[dict]:
    return json.loads(CASES_PATH.read_text(encoding="utf-8"))


def _tool_schemas() -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": spec.name,
                "description": spec.description,
                "parameters": spec.input_schema,
            },
        }
        for spec in TOOL_SPECS
    ]


def _parse_arguments(tool_call: ToolCallBlock) -> dict:
    if isinstance(tool_call.input, dict):
        return tool_call.input
    return json.loads(tool_call.input)


@unittest.skipUnless(
    RUN_LIVE,
    "设置 RUN_AGENT_ROUTING_EVAL=1 后执行内网模型路由评测",
)
class AgentRoutingLiveTest(unittest.IsolatedAsyncioTestCase):
    """只调用模型检查 function calling，不执行工具或访问数据库。"""

    async def asyncSetUp(self) -> None:
        self.assertTrue(
            os.getenv("LLM_API_KEY"),
            "路由评测要求由父进程安全注入 LLM_API_KEY",
        )
        self.model = build_chat_model(stream=False)
        self.http_client = httpx.AsyncClient(
            timeout=self.model.client_kwargs["timeout"],
        )
        self.model.client_kwargs = {"http_client": self.http_client}

        extra_body_json = os.getenv("LLM_ROUTING_EXTRA_BODY_JSON")
        if extra_body_json:
            extra_body = json.loads(extra_body_json)
            self.assertIsInstance(extra_body, dict)
            self.model.extra_body = extra_body

    async def asyncTearDown(self) -> None:
        await self.http_client.aclose()

    async def test_first_tool_matches_user_goal(self) -> None:
        system_prompt = build_prompt(
            phase="execution",
            tool_name=None,
            user_context=None,
        )

        for case in _load_cases():
            with self.subTest(case=case["name"]):
                response = await self.model(
                    messages=[
                        SystemMsg(name="system", content=system_prompt),
                        UserMsg(name="user", content=case["question"]),
                    ],
                    tools=_tool_schemas(),
                    tool_choice=ToolChoice(mode="required"),
                )

                self.assertIsInstance(response, ChatResponse)
                calls = [
                    block
                    for block in response.content
                    if isinstance(block, ToolCallBlock)
                ]
                self.assertTrue(calls, f"{case['name']}: 模型未调用工具")
                actual_call = calls[0]
                self.assertEqual(
                    case["expected_tool"],
                    actual_call.name,
                    f"{case['name']}: {case['question']}",
                )
                actual_arguments = _parse_arguments(actual_call)
                for key, expected in case.get("expected_arguments", {}).items():
                    self.assertEqual(
                        expected,
                        actual_arguments.get(key),
                        f"{case['name']}: 参数 {key}",
                    )


if __name__ == "__main__":
    unittest.main()
