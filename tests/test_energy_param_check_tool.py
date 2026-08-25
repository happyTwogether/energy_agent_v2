"""节能参数核查日期解析测试。"""

import pytest

from app.tools import energy_param_check_tool


class _FailOnExecuteSession:
    def __init__(self):
        self.calls = 0

    async def execute(self, sql, params=None):
        self.calls += 1
        raise AssertionError("明确日期不应查询 MAX(check_time)")


@pytest.mark.asyncio
async def test_explicit_check_date_skips_latest_date_query():
    session = _FailOnExecuteSession()
    check_date, date_note = await energy_param_check_tool._get_check_date(
        session,
        "2026-08-10",
    )

    assert check_date == "20260810"
    assert date_note == ""
    assert session.calls == 0
