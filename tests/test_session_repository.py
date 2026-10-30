"""真实 SQL 仓储契约：SQLite 执行兼容的 PostgreSQL upsert，不模拟查询结果。"""

import unittest

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.agent.errors import AgentPublicError
from app.services import database


class AsyncSessionBridge:
    def __init__(self, session):
        self.session = session

    async def execute(self, statement, *args):
        return self.session.execute(statement, *args)

    async def commit(self):
        self.session.commit()

    async def rollback(self):
        self.session.rollback()


class SessionRepositoryTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        self.connection = self.engine.connect()
        self.connection.execute(text("ATTACH DATABASE ':memory:' AS jd_agent"))
        database.Base.metadata.create_all(self.connection)
        self.connection.commit()
        self.session = Session(self.connection)
        self.db = AsyncSessionBridge(self.session)

    def tearDown(self):
        self.session.close()
        self.connection.close()
        self.engine.dispose()

    async def test_original_transcript_is_not_deleted_after_ten_turns(self):
        for turn in range(13):
            await database.save_conversation(self.db, "a", "owner", [
                {"role": "user", "content": f"问题{turn}"},
                {"role": "assistant", "content": f"回答{turn}"},
            ])
        messages = await database.load_conversation(self.db, "a", "owner")
        self.assertEqual(26, len(messages))
        self.assertEqual("问题0", messages[0]["content"])

    async def test_other_user_cannot_read_append_or_delete_conversation(self):
        await database.save_conversation(self.db, "a", "owner", [{"role": "user", "content": "私有内容"}])
        with self.assertRaises(AgentPublicError):
            await database.load_conversation(self.db, "a", "other")
        with self.assertRaises(AgentPublicError):
            await database.save_conversation(self.db, "a", "other", [{"role": "user", "content": "覆盖"}])
        with self.assertRaises(AgentPublicError):
            await database.delete_owned_conversation(self.db, "a", "other")
        self.assertEqual("私有内容", (await database.load_conversation(self.db, "a", "owner"))[0]["content"])

    async def test_state_and_original_pair_commit_together_with_version_check(self):
        state = {"summary": "第一份摘要", "context": [], "tasks_context": {"tasks": []}}
        await database.save_conversation(self.db, "a", "owner", [{"role": "user", "content": "一"}], agent_state=state)
        saved = await database.load_agent_state(self.db, "a", "owner")
        self.assertEqual(1, saved["version"])
        self.assertEqual("第一份摘要", saved["state"]["summary"])
        self.assertIsNone(await database.load_agent_state(self.db, "a", "other"))
        state["summary"] = "第二份摘要"
        await database.save_conversation(self.db, "a", "owner", [{"role": "user", "content": "二"}], agent_state=state, expected_state_version=1)
        with self.assertRaises(AgentPublicError):
            await database.save_conversation(self.db, "a", "owner", [{"role": "user", "content": "过期分支"}], agent_state=state, expected_state_version=1)
        self.assertEqual(["一", "二"], [item["content"] for item in await database.load_conversation(self.db, "a", "owner")])
        self.assertEqual(2, (await database.load_agent_state(self.db, "a", "owner"))["version"])

    async def test_delete_removes_only_owned_transcript_and_state(self):
        for cid, owner in (("a", "owner"), ("b", "other")):
            await database.save_conversation(self.db, cid, owner, [], agent_state={"summary": "摘要"})
        await database.delete_owned_conversation(self.db, "a", "owner")
        self.assertIsNone(await database.load_agent_state(self.db, "a", "owner"))
        self.assertIsNotNone(await database.load_agent_state(self.db, "b", "other"))
        with self.assertRaises(AgentPublicError):
            await database.load_conversation(self.db, "a", "owner")

    async def test_inflight_old_request_cannot_recreate_deleted_session(self):
        await database.save_conversation(self.db, "a", "owner", [], agent_state={"summary": "旧"})
        await database.delete_owned_conversation(self.db, "a", "owner")
        with self.assertRaises(AgentPublicError):
            await database.save_conversation(self.db, "a", "owner", [], agent_state={"summary": "旧"}, expected_state_version=1, allow_create=False)
        self.assertIsNone(await database.load_agent_state(self.db, "a", "owner"))

    async def test_required_state_table_is_checked_without_reading_user_data(self):
        await database.verify_session_storage(self.db)
        await self.db.execute(text("DROP TABLE jd_agent.agent_session_states"))
        with self.assertRaises(database.AgentConfigurationError):
            await database.verify_session_storage(self.db)
