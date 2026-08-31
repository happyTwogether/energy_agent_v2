"""报表工具的 AsyncSession 串行安全回归测试。"""

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, patch
import unittest

from pydantic import SecretStr

from app.agent.errors import AgentConfigurationError
from app.core.config import Settings
from app.services import database
from app.tools import report_query_tool


class ReportQuerySessionSafetyTest(unittest.IsolatedAsyncioTestCase):
    async def test_lte_and_nr_queries_do_not_share_session_concurrently(self) -> None:
        active_calls = 0
        max_active_calls = 0

        async def fetch(**kwargs):
            nonlocal active_calls, max_active_calls
            active_calls += 1
            max_active_calls = max(max_active_calls, active_calls)
            await asyncio.sleep(0)
            active_calls -= 1
            return None, []

        with (
            patch.object(
                report_query_tool,
                "get_latest_date",
                AsyncMock(return_value=datetime(2026, 8, 9)),
            ),
            patch.object(report_query_tool, "_fetch_data_with_baseline", side_effect=fetch),
        ):
            result = await report_query_tool.query_report(db=object())

        self.assertFalse(result["success"])
        self.assertEqual(1, max_active_calls)

    async def test_self_service_session_factory_uses_isolated_secret_url(self) -> None:
        fake_engine = object()
        fake_factory = object()
        settings = Settings(
            _env_file=None,
            self_service_enabled=True,
            self_service_database_url=SecretStr(
                "postgresql+asyncpg://reader:secret@db/agent_db",
            ),
        )
        database._self_service_engine = None
        database._self_service_session_factory = None

        with (
            patch.object(database, "get_settings", return_value=settings),
            patch.object(
                database,
                "create_async_engine",
                return_value=fake_engine,
            ) as create_engine,
            patch.object(
                database,
                "async_sessionmaker",
                return_value=fake_factory,
            ),
        ):
            first = database.get_self_service_session_factory()
            second = database.get_self_service_session_factory()

        self.assertIs(fake_factory, first)
        self.assertIs(first, second)
        create_engine.assert_called_once_with(
            "postgresql+asyncpg://reader:secret@db/agent_db",
            echo=False,
            pool_size=10,
            max_overflow=20,
            connect_args={
                "server_settings": {"default_transaction_read_only": "on"},
            },
        )

    async def test_self_service_session_factory_rejects_missing_url(self) -> None:
        settings = Settings(
            _env_file=None,
            self_service_enabled=True,
            self_service_database_url=SecretStr(""),
        )
        database._self_service_engine = None
        database._self_service_session_factory = None

        with patch.object(database, "get_settings", return_value=settings):
            with self.assertRaisesRegex(
                AgentConfigurationError,
                "SELF_SERVICE_DATABASE_URL",
            ):
                database.get_self_service_session_factory()


if __name__ == "__main__":
    unittest.main()
