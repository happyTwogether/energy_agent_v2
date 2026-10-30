"""真实 HTTP→仓储→AgentScope→状态持久化回归，模型与业务库边界本地替换。"""

import unittest
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import httpx
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.agent.tool_specs import EnergyToolSpec
from app.core.config import Settings
from app.services.database import Base, load_agent_state
from app.tools.batch_energy_tool import TOOL_INPUT_SCHEMA
from test_agent_integration import FakeSessionContext, ToolCallingModel
from test_session_repository import AsyncSessionBridge


class OwnedSessionApiTest(unittest.IsolatedAsyncioTestCase):
    async def test_phone_ownership_restart_followups_and_public_history(self):
        from main import create_app

        engine = create_engine("sqlite://")
        connection = engine.connect()
        connection.execute(text("ATTACH DATABASE ':memory:' AS jd_agent"))
        Base.metadata.create_all(connection)
        connection.commit()

        @asynccontextmanager
        async def session_factory():
            with Session(connection) as session:
                yield AsyncSessionBridge(session)

        models, executed = [], []

        def model_factory(*args):
            model = ToolCallingModel("analyze_batch_cells_energy", {"dist_name": "全网"})
            models.append(model)
            return model

        async def handler(db, **arguments):
            executed.append(arguments)
            return {"success": True, "report_content": f"{arguments['dist_name']}:{arguments['expansion_level']}"}

        spec = EnergyToolSpec(name="analyze_batch_cells_energy", description="本地分析", handler=handler, input_schema=TOOL_INPUT_SCHEMA)
        try:
            with (
                patch("app.api.routes.get_session_factory", return_value=session_factory),
                patch("app.api.routes._persist_metrics", AsyncMock()),
                patch("app.api.routes.store_message", AsyncMock()),
                patch("app.agent.toolkit.TOOL_SPECS", (spec,)),
                patch("app.agent.toolkit._new_session", return_value=FakeSessionContext()),
                patch("app.agent.runtime.build_chat_model", side_effect=model_factory),
                patch("app.agent.runtime.get_settings", return_value=Settings(_env_file=None)),
            ):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app()), base_url="http://test") as client:
                    first = await client.post("/api/v1/chat-messages", json={"user": "test-phone-a", "query": "湘潭市的节电空间"})
                    self.assertEqual(200, first.status_code, first.text)
                    cid = first.json()["conversation_id"]
                    rejected = await client.post("/api/v1/chat-messages", json={"user": "test-phone-b", "query": "中等扩展", "conversation_id": cid})
                    self.assertEqual(404, rejected.status_code)
                    self.assertEqual(1, len(models))
                    for method in (client.get, client.delete):
                        response = await method(f"/api/v1/conversations/{cid}", params={"user_id": "test-phone-b"})
                        self.assertEqual(404, response.status_code)
                        self.assertNotIn("湘潭", response.text)
                    for question in ("株洲市的呢？", "中等扩展"):
                        response = await client.post("/api/v1/chat-messages", json={"user": "test-phone-a", "query": question, "conversation_id": cid})
                        self.assertEqual(200, response.status_code, response.text)
                    self.assertEqual("株洲市:moderate", response.json()["answer"])
                    self.assertEqual([1, 0, 0], [model.calls for model in models])
                    details = await client.get(f"/api/v1/conversations/{cid}", params={"user_id": "test-phone-a"})
                    self.assertEqual(6, len(details.json()["messages"]))
                    self.assertNotIn("tasks_context", details.text)
                    self.assertNotIn("agent_context", details.text)
                    async with session_factory() as db:
                        checkpoint = await load_agent_state(db, cid, "test-phone-a")
                    self.assertEqual(3, checkpoint["version"])
                    task = checkpoint["state"]["tasks_context"]["tasks"][-1]
                    self.assertEqual("completed", task["state"])
                    self.assertEqual("株洲市", task["metadata"]["conditions"]["arguments"]["dist_name"])
                    deleted = await client.delete(f"/api/v1/conversations/{cid}", params={"user_id": "test-phone-a"})
                    self.assertEqual(200, deleted.status_code)
        finally:
            connection.close()
            engine.dispose()
