"""AgentScope 业务工具适配层测试。"""

import json
from pathlib import Path
import unittest

from agentscope.message import ToolResultState
from agentscope.permission import PermissionBehavior, PermissionContext

try:
    from app.agent.tool_specs import EnergyToolSpec
    from app.agent.toolkit import EnergyFunctionTool, build_toolkit
except ModuleNotFoundError:
    EnergyToolSpec = None
    EnergyFunctionTool = None
    build_toolkit = None


def load_schema_fixture() -> list[dict]:
    """读取迁移前固化的 9 个 OpenAI function schema。"""
    fixture = Path(__file__).parent / "fixtures" / "tool_schemas.json"
    return json.loads(fixture.read_text(encoding="utf-8"))


class AgentToolkitTest(unittest.IsolatedAsyncioTestCase):
    """验证工具调用边界与 legacy schema 完全一致。"""

    def setUp(self) -> None:
        self.assertIsNotNone(EnergyToolSpec, "AgentScope 工具目录尚未实现")
        self.assertIsNotNone(EnergyFunctionTool, "AgentScope 工具适配器尚未实现")
        self.assertIsNotNone(build_toolkit, "AgentScope Toolkit 尚未实现")

    async def test_tool_call_uses_a_fresh_session_and_preserves_direct_answer(
        self,
    ) -> None:
        sessions: list[object] = []

        class FakeSessionContext:
            async def __aenter__(self) -> object:
                session = object()
                sessions.append(session)
                return session

            async def __aexit__(self, exc_type, exc, traceback) -> bool:
                return False

        async def handler(db, province=None):
            return {
                "success": True,
                "report_content": f"report:{province}",
                "download_url": "https://download.invalid/report.xlsx",
            }

        tool = EnergyFunctionTool(  # type: ignore[misc]
            spec=EnergyToolSpec(  # type: ignore[misc]
                name="query_report",
                description="report",
                input_schema={"type": "object", "properties": {}},
                handler=handler,
                is_read_only=False,
            ),
            session_factory=FakeSessionContext,
        )

        first = await tool.call(province="湖南省")
        second = await tool.call(province="广东省")

        self.assertIsNot(sessions[0], sessions[1])
        self.assertEqual(ToolResultState.SUCCESS, first.state)
        self.assertIn("report:湖南省", first.metadata["direct_answer"])
        self.assertIn("report.xlsx", first.metadata["direct_answer"])
        self.assertEqual(ToolResultState.SUCCESS, second.state)

    async def test_false_success_payload_becomes_error_chunk(self) -> None:
        class FakeSessionContext:
            async def __aenter__(self) -> object:
                return object()

            async def __aexit__(self, exc_type, exc, traceback) -> bool:
                return False

        async def handler(db):
            return {"success": False, "error": "query failed"}

        tool = EnergyFunctionTool(  # type: ignore[misc]
            spec=EnergyToolSpec(  # type: ignore[misc]
                name="query_report",
                description="report",
                input_schema={"type": "object", "properties": {}},
                handler=handler,
            ),
            session_factory=FakeSessionContext,
        )

        chunk = await tool.call()

        self.assertEqual(ToolResultState.ERROR, chunk.state)
        self.assertIn("query failed", chunk.content[0].text)

    async def test_unexpected_handler_error_is_sanitized(self) -> None:
        class FakeSessionContext:
            async def __aenter__(self) -> object:
                return object()

            async def __aexit__(self, exc_type, exc, traceback) -> bool:
                return False

        async def handler(db):
            raise RuntimeError("postgresql://internal-user:secret@db/agent")

        tool = EnergyFunctionTool(  # type: ignore[misc]
            spec=EnergyToolSpec(  # type: ignore[misc]
                name="query_report",
                description="report",
                input_schema={"type": "object", "properties": {}},
                handler=handler,
            ),
            session_factory=FakeSessionContext,
        )

        chunk = await tool.call()

        self.assertEqual(ToolResultState.ERROR, chunk.state)
        self.assertIn("工具执行失败，请稍后重试", chunk.content[0].text)
        self.assertNotIn("postgresql", chunk.content[0].text)

    async def test_business_tools_are_allowed_without_hitl(self) -> None:
        async def handler(db):
            return {"success": True}

        tool = EnergyFunctionTool(  # type: ignore[misc]
            spec=EnergyToolSpec(  # type: ignore[misc]
                name="query_report",
                description="report",
                input_schema={"type": "object", "properties": {}},
                handler=handler,
            ),
            session_factory=lambda: None,
        )

        decision = await tool.check_permissions({}, PermissionContext())

        self.assertEqual(PermissionBehavior.ALLOW, decision.behavior)

    async def test_toolkit_schemas_match_the_legacy_fixture(self) -> None:
        actual = sorted(
            await build_toolkit().get_tool_schemas(),  # type: ignore[misc]
            key=lambda item: item["function"]["name"],
        )

        self.assertEqual(load_schema_fixture(), actual)
        self.assertTrue(
            all(
                "db" not in item["function"]["parameters"]["properties"]
                for item in actual
            ),
        )


if __name__ == "__main__":
    unittest.main()
