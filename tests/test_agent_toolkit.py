"""AgentScope 业务工具适配层测试。"""

import json
from pathlib import Path
import unittest

from agentscope.message import ToolResultState
from agentscope.permission import PermissionBehavior, PermissionContext

try:
    from app.agent.errors import AgentConfigurationError
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

    async def test_business_data_tool_uses_self_service_session(self) -> None:
        default_sessions: list[object] = []
        reader_sessions: list[object] = []

        class SessionContext:
            def __init__(self, target: list[object]) -> None:
                self._target = target

            async def __aenter__(self) -> object:
                session = object()
                self._target.append(session)
                return session

            async def __aexit__(self, exc_type, exc, traceback) -> bool:
                return False

        toolkit = build_toolkit(
            session_factory=lambda: SessionContext(default_sessions),
            self_service_session_factory=lambda: SessionContext(reader_sessions),
        )
        tool = await toolkit.get_tool("query_business_data")

        self.assertIsNotNone(tool)
        await tool.call(question="查询5G小区流量")

        self.assertEqual([], default_sessions)
        self.assertEqual(1, len(reader_sessions))

    async def test_missing_reader_configuration_is_returned_explicitly(self) -> None:
        def missing_reader_session():
            raise AgentConfigurationError(
                "缺少运行时配置 SELF_SERVICE_DATABASE_URL",
            )

        toolkit = build_toolkit(
            self_service_session_factory=missing_reader_session,
        )
        tool = await toolkit.get_tool("query_business_data")

        chunk = await tool.call(question="查询原始字段")

        self.assertEqual(ToolResultState.ERROR, chunk.state)
        self.assertIn("SELF_SERVICE_DATABASE_URL", chunk.content[0].text)
        self.assertEqual("", chunk.metadata["direct_answer"])


if __name__ == "__main__":
    unittest.main()
