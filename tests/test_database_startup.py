"""生产只做表检查，建表需显式开启；缺表与读权限错误分开诊断。"""

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from sqlalchemy.exc import ProgrammingError

from app.core.config import Settings
from app.services import database


class ConnectionContext:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, *args):
        return False


class DatabaseStartupTest(unittest.IsolatedAsyncioTestCase):
    async def _start(self, settings):
        connection = SimpleNamespace(run_sync=AsyncMock())
        engine = SimpleNamespace(connect=Mock(return_value=ConnectionContext(connection)), begin=Mock(return_value=ConnectionContext(connection)))
        verify = AsyncMock()
        with patch.object(database, "get_engine", return_value=engine), patch.object(database, "get_settings", return_value=settings), patch.object(database, "verify_session_storage", verify):
            await database.init_db()
        verify.assert_awaited_once_with(connection)
        return engine, connection

    async def test_default_does_not_attempt_ddl(self):
        engine, connection = await self._start(Settings(_env_file=None))
        engine.begin.assert_not_called()
        connection.run_sync.assert_not_awaited()

    async def test_development_can_explicitly_enable_auto_creation(self):
        engine, connection = await self._start(Settings(_env_file=None, db_auto_create_tables=True))
        engine.begin.assert_called_once()
        connection.run_sync.assert_awaited_once_with(database.Base.metadata.create_all)

    async def test_missing_state_table_is_reported_as_missing_not_write_failure(self):
        origin = RuntimeError("missing relation")
        origin.sqlstate = "42P01"
        db = SimpleNamespace(execute=AsyncMock(side_effect=[None, ProgrammingError("SELECT", {}, origin)]))
        with self.assertRaisesRegex(database.AgentConfigurationError, "缺少.*agent_session_states"):
            await database.verify_session_storage(db)

    async def test_select_denied_is_reported_as_read_privilege(self):
        origin = RuntimeError("permission denied")
        origin.sqlstate = "42501"
        db = SimpleNamespace(execute=AsyncMock(side_effect=ProgrammingError("SELECT", {}, origin)))
        with self.assertRaisesRegex(database.AgentConfigurationError, "SELECT"):
            await database.verify_session_storage(db)
