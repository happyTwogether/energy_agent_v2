"""报表工具的 AsyncSession 串行安全回归测试。"""

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, patch
import unittest

from app.agent.errors import AgentConfigurationError
from app.core.config import Settings
from app.services import database
from app.tools import report_query_tool


class ReportQuerySessionSafetyTest(unittest.IsolatedAsyncioTestCase):
    async def test_report_filters_county_and_splits_unique_daily_rows(self) -> None:
        rows = [
            {
                "data_date": "2026-09-23",
                "dist_name": "全网",
                "logic_station_total": 26_234,
                "logic_read_station_total": 25_000,
                "all_cell_total": 69_607,
                "sa_bbu_total": 20_000,
                "nr_sa_station_power": 1_867_790.12,
            },
            {
                "data_date": "2026-09-22",
                "dist_name": "全网",
                "logic_station_total": 25_900,
                "all_cell_total": 68_900,
                "nr_sa_station_power": 1_800_000,
            },
        ]
        result = unittest.mock.Mock()
        result.mappings.return_value.all.return_value = rows
        db = AsyncMock()
        db.execute.return_value = result

        target, baseline = await report_query_tool._fetch_data_with_baseline(
            db=db,
            table="nr_report_day_collect",
            province="湖南省",
            dist_name="全网",
            county_name="全网",
            prod_name="中兴",
            freq_band="全网",
            site_type="全网",
            area="全网",
            query_start="2026-09-16",
            date_end="2026-09-23",
        )

        statement = str(db.execute.await_args.args[0])
        self.assertIn("dist_name = :dist_name", statement)
        self.assertIn("county_name = :county_name", statement)
        self.assertEqual("全网", db.execute.await_args.args[1]["county_name"])
        self.assertEqual(26_234, target["logic_station_total"])
        self.assertEqual(69_607, target["all_cell_total"])
        self.assertEqual(1_867_790.12, target["nr_sa_station_power"])
        self.assertEqual(1, len(baseline))
        self.assertEqual(25_900, baseline[0]["logic_station_total"])

    async def test_city_report_keeps_exact_city_row(self) -> None:
        rows = [
            {"data_date": "2026-09-23", "dist_name": "长沙市", "logic_station_total": 200},
            {"data_date": "2026-09-22", "dist_name": "长沙市", "logic_station_total": 190},
        ]
        result = unittest.mock.Mock()
        result.mappings.return_value.all.return_value = rows
        db = AsyncMock()
        db.execute.return_value = result

        target, baseline = await report_query_tool._fetch_data_with_baseline(
            db=db,
            table="lte_report_day_collect",
            province="湖南省",
            dist_name="长沙市",
            county_name="岳麓区",
            prod_name="中兴",
            freq_band="全网",
            site_type="全网",
            area="全网",
            query_start="2026-09-16",
            date_end="2026-09-23",
        )

        statement = str(db.execute.await_args.args[0])
        self.assertIn("dist_name = :dist_name", statement)
        self.assertEqual("岳麓区", db.execute.await_args.args[1]["county_name"])
        self.assertEqual(200, target["logic_station_total"])
        self.assertEqual([190], [row["logic_station_total"] for row in baseline])

    def test_complete_report_dimensions_reject_duplicate_daily_rows(self) -> None:
        rows = [
            {"data_date": "2026-09-23", "logic_station_total": 200},
            {"data_date": "2026-09-23", "logic_station_total": 20},
        ]

        with self.assertRaisesRegex(ValueError, "2026-09-23"):
            report_query_tool._split_target_and_baseline(rows, "2026-09-23")

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

    async def test_self_service_session_factory_reuses_encoded_main_database_url(self) -> None:
        fake_engine = object()
        fake_factory = object()
        settings = Settings(
            _env_file=None,
            self_service_enabled=True,
            db_host="db.internal",
            db_port=5433,
            db_user="reader",
            db_password="secret@word",
            db_name="smartcore",
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
            "postgresql+asyncpg://reader:secret%40word@db.internal:5433/smartcore",
            echo=False,
            pool_size=10,
            max_overflow=20,
            connect_args={
                "server_settings": {"default_transaction_read_only": "on"},
            },
        )

    async def test_self_service_session_factory_rejects_disabled_feature(self) -> None:
        settings = Settings(
            _env_file=None,
            self_service_enabled=False,
        )
        database._self_service_engine = None
        database._self_service_session_factory = None

        with patch.object(database, "get_settings", return_value=settings):
            with self.assertRaisesRegex(
                AgentConfigurationError,
                "SELF_SERVICE_ENABLED",
            ):
                database.get_self_service_session_factory()


if __name__ == "__main__":
    unittest.main()
