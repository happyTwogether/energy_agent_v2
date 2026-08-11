"""报表工具的 AsyncSession 串行安全回归测试。"""

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, patch
import unittest

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


if __name__ == "__main__":
    unittest.main()
