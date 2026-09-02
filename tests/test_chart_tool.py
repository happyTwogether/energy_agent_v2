"""统一业务数据查询结果的图表生成测试。"""

import json

import pytest

from app.tools.chart_tool import generate_chart


@pytest.mark.asyncio
async def test_line_chart_uses_column_metadata_for_chinese_rows() -> None:
    query_result = {
        "success": True,
        "columns": [
            {
                "id": "nr_report_day_collect.data_date",
                "label": "数据日期",
                "type": "date",
                "unit": "",
            },
            {
                "id": "nr_traffic_tb",
                "label": "5G上下行业务量",
                "type": "numeric",
                "unit": "TB",
            },
        ],
        "rows": [
            {"数据日期": "2026-08-23", "5G上下行业务量": 12.5},
            {"数据日期": "2026-08-24", "5G上下行业务量": 13.75},
        ],
    }

    result = await generate_chart(
        db=object(),
        charts=[{
            "title": "5G流量趋势",
            "chart_type": "line",
            "data": query_result,
        }],
    )

    option = json.loads(result["charts"][0]["option_json"])
    assert option["xAxis"]["data"] == ["2026-08-23", "2026-08-24"]
    assert option["series"] == [{
        "name": "5G上下行业务量",
        "type": "line",
        "data": [12.5, 13.75],
        "smooth": True,
        "lineStyle": {"color": "#5470c6", "width": 2},
        "symbol": "circle",
        "symbolSize": 4,
    }]
