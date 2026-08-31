"""AgentScope 通用业务数据工具边界测试。"""

from unittest.mock import AsyncMock, patch

import pytest

from app.tools.business_data_query_tool import query_business_data


@pytest.mark.asyncio
async def test_business_data_tool_rejects_blank_question() -> None:
    payload = await query_business_data(db=object(), question="   ")

    assert payload["success"] is False
    assert payload["error"] == "请说明需要查询的业务数据。"
    assert "report_content" not in payload


@pytest.mark.asyncio
async def test_business_data_tool_forwards_complete_question() -> None:
    service = AsyncMock()
    service.query.return_value = {"success": True, "rows": []}

    with patch(
        "app.tools.business_data_query_tool.get_business_data_query_service",
        return_value=service,
    ):
        payload = await query_business_data(
            db="reader-session",
            question="查询扩展、收缩和周边距离",
            export_excel=True,
        )

    assert payload["success"] is True
    service.query.assert_awaited_once_with(
        db="reader-session",
        question="查询扩展、收缩和周边距离",
        export_excel=True,
    )
