"""单小区节电工具的 V1.4 行为测试。"""

from datetime import date, datetime

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


def test_pre_sleep_load_is_required_for_all_and_constriction():
    """完整分析和收缩分析都必须形成休眠前负荷闭环。"""
    assert energy_saving_tool._needs_pre_sleep_load("all") is True
    assert energy_saving_tool._needs_pre_sleep_load("constriction") is True
    assert energy_saving_tool._needs_pre_sleep_load("pre_sleep_load") is True
    assert energy_saving_tool._needs_pre_sleep_load("expansion") is False


def test_high_prb_days_only_counts_actual_pre_sleep_hours():
    """同日重复命中只算一天，非休眠前小时即使高负荷也不计数。"""
    rows = [
        {
            "stat_time": date(2026, 8, 9),
            "prb_hour": 23,
            "sleep_hour": "0",
            "prb_rate_ul": 51.0,
            "prb_rate_dl": 1.0,
        },
        {
            "stat_time": date(2026, 8, 9),
            "prb_hour": 22,
            "sleep_hour": "0",
            "prb_rate_ul": 90.0,
            "prb_rate_dl": 1.0,
        },
        {
            "stat_time": datetime(2026, 8, 9, 12),
            "prb_hour": 23,
            "sleep_hour": "0",
            "prb_rate_ul": 60.0,
            "prb_rate_dl": 1.0,
        },
    ]

    assert energy_saving_tool._count_high_pre_sleep_days(rows) == 1


@pytest.mark.asyncio
async def test_pre_sleep_query_counts_candidate_rows_in_python(monkeypatch):
    """七日 SQL 有闭区间和阈值，实际休眠前关系在 Python 中判定。"""
    queries: list[str] = []

    async def fake_fetch_rows(sql, params):
        query = str(sql)
        queries.append(query)
        if "ORDER BY stat_time, prb_hour" in query:
            return []
        assert "COUNT(" not in query
        assert "stat_time >= :start_date" in query
        assert "stat_time <= :end_date" in query
        assert "prb_rate_ul > :prb_threshold" in query
        return [
            {
                "stat_time": date(2026, 8, 9),
                "prb_hour": 23,
                "sleep_hour": "0",
                "prb_rate_ul": 51.0,
                "prb_rate_dl": 1.0,
            },
            {
                "stat_time": date(2026, 8, 8),
                "prb_hour": 22,
                "sleep_hour": "0",
                "prb_rate_ul": 90.0,
                "prb_rate_dl": 1.0,
            },
        ]

    monkeypatch.setattr(energy_saving_tool, "fetch_rows", fake_fetch_rows)

    result = await energy_saving_tool._query_pre_sleep_load_data(
        "main",
        datetime(2026, 8, 10),
    )

    assert len(queries) == 2
    assert result["high_prb_days_7d"] == 1


@pytest.mark.asyncio
async def test_all_response_includes_pre_sleep_load(monkeypatch):
    """完整分析必须同时返回休眠前负荷原始结果。"""
    async def fake_query_expansion(cgi, stat_time):
        return {"data": [], "table": "扩展表", "cell_name": "测试小区"}

    async def fake_query_constriction(cgi, stat_time):
        return {"data": [], "table": "收缩表"}

    async def fake_query_pre_sleep(cgi, stat_time):
        return {
            "data": [{"cgi": cgi, "is_pre_sleep_hour": "是"}],
            "table": "休眠前负荷表",
            "high_prb_days_7d": 1,
        }

    async def fake_param_check(db, cgi, stat_time):
        return {"is_compliant": True, "unqualified_count": 0}

    monkeypatch.setattr(
        energy_saving_tool,
        "_query_expansion_data",
        fake_query_expansion,
    )
    monkeypatch.setattr(
        energy_saving_tool,
        "_query_constriction_data",
        fake_query_constriction,
    )
    monkeypatch.setattr(
        energy_saving_tool,
        "_query_pre_sleep_load_data",
        fake_query_pre_sleep,
    )
    monkeypatch.setattr(energy_saving_tool, "_query_param_check", fake_param_check)
    monkeypatch.setattr(
        energy_saving_tool,
        "get_session_factory",
        lambda: _SessionContext,
    )

    result = await energy_saving_tool.analyze_single_cell_energy(
        analysis_target="all",
        db=_LatestDateSession(),
        cgi="main",
    )

    assert result["pre_sleep_load_table"] == "休眠前负荷表"
    assert result["pre_sleep_load_data"][0]["is_pre_sleep_hour"] == "是"
    assert result["high_prb_days_7d"] == 1


@pytest.mark.asyncio
async def test_query_constriction_enriches_relation_without_dropping_ten(
    monkeypatch,
):
    """收缩证据按集合补齐，并保留抬升量恰好为 10 的记录。"""
    calls: list[str] = []

    async def fake_fetch_rows(sql, params):
        query = str(sql)
        calls.append(query)
        if "jd_cell_constriction_day" in query:
            assert "self_prb_rate_ul_before" in query
            assert "self_sleep_duration" in query
            assert "prb_increase" in query
            base_row = {
                "cgi": "main",
                "hours": 0,
                "site_type": None,
                "around_cgi_network_type": "lte",
                "self_prb_rate_ul_before": 12.0,
                "self_prb_rate_dl_before": 5.0,
                "self_sleep_duration": 3600,
                "prb_rate_ul": 20.0,
                "prb_rate_dl": 60.0,
                "prb_rate_ul_before": 10.0,
                "prb_rate_dl_before": 50.0,
                "prb_increase": 10.0,
                "before_sleep_hour": 23,
                "before_sleep_date": date(2026, 8, 9),
            }
            return [
                {**base_row, "around_cgi": "neighbor", "prb_increase": 10.0},
                {**base_row, "around_cgi": "neighbor-2", "prb_increase": 11.0},
            ]
        if "jd_cell_around" in query:
            assert "unnest(" in query
            return [
                {
                    "cgi": "main",
                    "around_cgi": "neighbor",
                    "distance": 80.0,
                    "around_cgi_network_type": "lte",
                },
                {
                    "cgi": "main",
                    "around_cgi": "neighbor-2",
                    "distance": 90.0,
                    "around_cgi_network_type": "lte",
                },
            ]
        if "nr_fix_prm" in query:
            assert "ANY(:cgis)" in query
            return [{"cgi": "main", "site_type": "室分"}]
        if "lte_fix_prm" in query:
            assert "ANY(:cgis)" in query
            return [
                {"cgi": "neighbor", "site_type": "宏站"},
                {"cgi": "neighbor-2", "site_type": "宏站"},
            ]
        raise AssertionError(query)

    monkeypatch.setattr(energy_saving_tool, "fetch_rows", fake_fetch_rows)

    result = await energy_saving_tool._query_constriction_data(
        "main",
        datetime(2026, 8, 10),
    )

    assert result["data"][0]["prb_increase"] == 10.0
    assert result["data"][0]["distance"] == 80.0
    assert result["data"][0]["main_site_type"] == "室分"
    assert result["data"][0]["around_site_type"] == "宏站"
    assert result["data"][0]["relation_status"] == "allowed"
    assert sum("jd_cell_around" in query for query in calls) == 1
    assert sum("nr_fix_prm" in query for query in calls) == 1
    assert sum("lte_fix_prm" in query for query in calls) == 1
