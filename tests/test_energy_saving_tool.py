"""单小区节电工具的 V1.4 行为测试。"""

from datetime import datetime

import pytest

from app.tools import energy_saving_tool


class _LatestDateResult:
    def mappings(self):
        return self

    def first(self):
        return {"max_date": datetime(2026, 8, 10)}


class _LatestDateSession:
    async def execute(self, sql, params=None):
        return _LatestDateResult()


class _SessionContext:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, exc_type, exc, traceback):
        return False


@pytest.mark.asyncio
async def test_query_expansion_uses_precomputed_v14_fields(monkeypatch):
    """扩展结果只读预计算表，并保留数据库给出的 90% 边界。"""
    queries: list[str] = []

    async def fake_fetch_rows(sql, params):
        query = str(sql)
        queries.append(query)
        assert "jd_cell_detail_hour_nr" not in query
        return [
            {
                "cgi": params["cgi"],
                "cell_name": "测试小区",
                "hour_detail": [{"hour": 22, "low_flow_pct": 90.0}],
                "hour_filter": "22",
                "hour_int": 1,
                "hour_filter_early": None,
                "hour_int_early": 0,
                "deploy_hours": "22,23,0",
                "deploy_hours_continuous": "22:00-00:59",
                "deploy_hours_early": None,
                "deploy_hours_continuous_early": None,
            }
        ]

    monkeypatch.setattr(energy_saving_tool, "fetch_rows", fake_fetch_rows)

    result = await energy_saving_tool._query_expansion_data(
        "460-00-1-1",
        datetime(2026, 8, 10),
    )

    assert len(queries) == 1
    assert result["data"][0]["hour_detail"][0]["low_flow_pct"] == 90.0
    assert result["data"][0]["hour_int"] == 1
    assert result["data"][0]["deploy_hours_continuous"] == "22:00-00:59"
    assert "22:00-00:59" in result["table"]


@pytest.mark.asyncio
async def test_single_cell_response_exposes_expansion_raw_data(monkeypatch):
    """顶层响应保留原始扩展字段和明确的截断元数据。"""
    expansion_row = {
        "cgi": "460-00-1-1",
        "hour_int": 1,
        "deploy_hours_continuous": "22:00-00:59",
    }

    async def fake_query_expansion(cgi, stat_time):
        return {
            "data": [expansion_row],
            "table": "扩展表",
            "cell_name": "测试小区",
        }

    async def fake_param_check(db, cgi, stat_time):
        return {"is_compliant": True, "unqualified_count": 0}

    monkeypatch.setattr(
        energy_saving_tool,
        "_query_expansion_data",
        fake_query_expansion,
    )
    monkeypatch.setattr(energy_saving_tool, "_query_param_check", fake_param_check)
    monkeypatch.setattr(
        energy_saving_tool,
        "get_session_factory",
        lambda: _SessionContext,
    )

    result = await energy_saving_tool.analyze_single_cell_energy(
        analysis_target="expansion",
        db=_LatestDateSession(),
        cgi="460-00-1-1",
    )

    assert result["expansion_data"] == [expansion_row]
    assert result["expansion_total_count"] == 1
    assert result["expansion_returned_count"] == 1
    assert result["expansion_is_truncated"] is False
