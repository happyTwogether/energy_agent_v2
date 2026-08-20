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
async def test_query_expansion_returns_process_and_dual_window_tables(monkeypatch):
    """扩展报告同时返回逐小时依据、候选结果和双口径部署结果。"""
    queries: list[str] = []

    async def fake_fetch_rows(sql, params):
        query = str(sql)
        queries.append(query)
        if "jd_cell_detail_hour_nr" in query:
            assert params["night_hours"] == [22, 23, 0, 1, 2, 3, 4, 5, 6, 7]
            assert params["start_date"] == datetime(2026, 7, 27)
            assert params["end_date"] == datetime(2026, 8, 10)
            return [{"hours": 22, "avg_sleep_sum": 0}]
        return [
            {
                "cgi": params["cgi"],
                "cell_name": "测试小区",
                "hour_detail": [{"hour": 22, "low_flow_pct": 90.0}],
                "hour_filter": "22",
                "hour_int": 1,
                "hour_filter_early": "0",
                "hour_int_early": 1,
                "deploy_hours": "22,23,0",
                "deploy_hours_continuous": "22:00-00:59",
                "deploy_hours_early": "0,1,2,3,4,5",
                "deploy_hours_continuous_early": "00:00-05:59",
                "is_whitelist": "否",
            }
        ]

    monkeypatch.setattr(energy_saving_tool, "fetch_rows", fake_fetch_rows)

    result = await energy_saving_tool._query_expansion_data(
        "460-00-1-1",
        datetime(2026, 8, 10),
    )

    assert len(queries) == 2
    assert result["data"][0]["hour_detail"][0]["low_flow_pct"] == 90.0
    assert result["process_data"][0]["suggestion"] == "可扩展"
    assert "低业务占比" in result["process_table"]
    assert "0秒" in result["process_table"]
    assert "22:00-22:59" in result["process_table"]
    assert "23:00-23:59" not in result["process_table"]
    assert "00:00-00:59" not in result["process_table"]
    assert "22:00-00:59" in result["deployment_table"]
    assert "0,1,2,3,4,5" in result["deployment_table"]
    assert result["expansion_result_status"] == "candidate_available"
    assert result["process_evidence_status"] == "partial"
    assert result["is_whitelist"] is False


@pytest.mark.asyncio
async def test_query_expansion_without_result_does_not_create_zero_hour_conclusion(
    monkeypatch,
):
    """缺少扩展结果时不得构造已完成评估的零时长结论。"""
    async def fake_fetch_rows(sql, params):
        return []

    monkeypatch.setattr(energy_saving_tool, "fetch_rows", fake_fetch_rows)

    result = await energy_saving_tool._query_expansion_data(
        "460-00-1-1",
        datetime(2026, 8, 10),
    )

    assert result["expansion_result_status"] == "unavailable"
    assert result["candidate_table"] == ""
    assert result["deployment_table"] == ""
    assert result["high_load_type"] is None


@pytest.mark.asyncio
async def test_query_expansion_maps_reason_only_for_explicit_whitelist(monkeypatch):
    """扩展原因仅在明确命中白名单时可作为白名单元数据输出。"""
    async def fake_fetch_rows(sql, params):
        if "jd_cell_detail_hour_nr" in str(sql):
            return []
        return [{
            "cgi": params["cgi"],
            "reason": "重要活动保障",
            "is_whitelist": "是",
            "hour_filter": None,
            "hour_filter_early": None,
        }]

    monkeypatch.setattr(energy_saving_tool, "fetch_rows", fake_fetch_rows)

    result = await energy_saving_tool._query_expansion_data(
        "460-00-1-1",
        datetime(2026, 8, 10),
    )

    assert result["is_whitelist"] is True
    assert result["whitelist_reason"] == "重要活动保障"


@pytest.mark.asyncio
async def test_single_cell_handler_returns_direct_report_and_stage_performance(
    monkeypatch,
):
    """真实 handler 应直出正文，并暴露实际执行阶段的耗时。"""
    async def fake_query_expansion(cgi, stat_time):
        return {
            "data": [],
            "table": "",
            "process_data": [],
            "process_table": "",
            "candidate_table": "",
            "deployment_table": "",
            "expansion_result_status": "unavailable",
            "process_evidence_status": "missing",
            "high_load_type": None,
            "is_whitelist": None,
            "cell_name": "测试小区",
        }

    async def fake_param_check(db, cgi, stat_time):
        return {
            "success": False,
            "is_compliant": None,
            "error": "参数核查暂不可用",
        }

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

    assert "暂不能判断是否需要扩展" in result["report_content"]
    assert result["whitelist_status"] == "unknown"
    for key in (
        "latest_date_ms",
        "expansion_ms",
        "param_check_ms",
        "report_render_ms",
        "total_ms",
    ):
        assert result["performance"][key] >= 0
    assert "constriction_ms" not in result["performance"]
    assert "latest_date_ms" not in result["report_content"]


@pytest.mark.asyncio
async def test_single_cell_load_handler_reports_state_and_stage_performance(
    monkeypatch,
):
    """仅负荷 handler 应直出结论并记录独立查询阶段耗时。"""
    async def fake_check_high_load(db, cgi, stat_time):
        return {"high_load_type": "上高", "cell_name": "测试小区"}

    monkeypatch.setattr(
        energy_saving_tool,
        "_check_high_load_with_base_info",
        fake_check_high_load,
    )

    result = await energy_saving_tool.analyze_single_cell_energy(
        analysis_target="load",
        db=_LatestDateSession(),
        cgi="460-00-1-1",
    )

    assert "上高" in result["report_content"]
    assert "上行高负荷" in result["report_content"]
    assert result["performance"]["load_ms"] >= 0
    assert "expansion_ms" not in result["performance"]
    assert "load_ms" not in result["report_content"]


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
            "process_data": [{"hour": 22, "suggestion": "可扩展"}],
            "process_table": "逐小时过程表",
            "candidate_table": "候选时段表",
            "deployment_table": "连续部署表",
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
    assert result["expansion_process_data"][0]["suggestion"] == "可扩展"
    assert result["expansion_process_table"] == "逐小时过程表"
    assert result["expansion_candidate_table"] == "候选时段表"
    assert result["expansion_deployment_table"] == "连续部署表"


@pytest.mark.asyncio
async def test_single_cell_response_preserves_unavailable_param_state(monkeypatch):
    """参数核查异常时不得被默认值伪装成合规。"""
    async def fake_query_expansion(cgi, stat_time):
        return {"data": [], "table": "扩展表", "cell_name": "测试小区"}

    async def fake_param_check(db, cgi, stat_time):
        return {
            "success": False,
            "is_compliant": None,
            "error": "参数核查暂不可用",
        }

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

    assert result["param_check"] == {
        "success": False,
        "is_compliant": None,
        "unqualified_count": 0,
        "error": "参数核查暂不可用",
    }


@pytest.mark.asyncio
async def test_param_check_failure_without_conclusion_is_unknown(monkeypatch):
    """上游核查失败且未给结论时，不得默认成参数合规。"""
    from app.tools import energy_param_check_tool

    async def fake_query_energy_param_check(cgi, check_date, db):
        return {"success": False, "error": "核查服务失败"}

    monkeypatch.setattr(
        energy_param_check_tool,
        "query_energy_param_check",
        fake_query_energy_param_check,
    )

    result = await energy_saving_tool._query_param_check(
        object(),
        "460-00-1-1",
        "2026-08-10",
    )

    assert result["success"] is False
    assert result["is_compliant"] is None


@pytest.mark.asyncio
async def test_single_cell_response_normalizes_expansion_whitelist_metadata(
    monkeypatch,
):
    """扩展响应必须输出布尔白名单状态和完整有效期。"""
    async def fake_query_expansion(cgi, stat_time):
        return {
            "data": [],
            "table": "扩展表",
            "cell_name": "测试小区",
            "is_whitelist": "是",
            "reason": "扩展业务原因",
            "whitelist_reason": "重要活动保障",
            "starttime": "2026-08-01",
            "endtime": "2026-08-31",
        }

    async def fake_param_check(db, cgi, stat_time):
        return {"success": True, "is_compliant": True, "unqualified_count": 0}

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

    assert result["is_whitelist"] is True
    assert result["whitelist_status"] == "whitelisted"
    assert result["whitelist_reason"] == "重要活动保障"
    assert result["jd_starttime"] == "2026-08-01"
    assert result["jd_endtime"] == "2026-08-31"


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
            return [
                {
                    "stat_time": date(2026, 8, 10),
                    "dist_name": "地市",
                    "prod_name": "厂家",
                    "gnb_name": "基站",
                    "cell_name": "主小区",
                    "cgi": "main",
                    "ee_shallowsleeptimerru": 600,
                    "ee_deepsleeptimerru": 0,
                    "ee_supersleeptimerru": 0,
                    "prb_hour": 23,
                    "prb_rate_ul": 51.0,
                    "prb_rate_dl": 48.0,
                    "sleep_hour": "0",
                }
            ]
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
    assert result["data"][0]["prb_rate_ul"] == 51.0
    assert result["data"][0]["prb_rate_dl"] == 48.0
    assert result["data"][0]["prb_rate"] == 51.0
    assert "上行PRB利用率(%)" in result["table"]
    assert "下行PRB利用率(%)" in result["table"]


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
async def test_constriction_response_exposes_focused_tables_and_whitelist(
    monkeypatch,
):
    """仅收缩分析也必须透传四张证据表和完整白名单信息。"""
    async def fake_query_constriction(cgi, stat_time):
        return {
            "data": [],
            "table": "结果表",
            "main_table": "主小区条件表",
            "relation_table": "关系范围表",
            "load_table": "邻区负荷表",
            "result_table": "收缩结果表",
            "cell_name": "测试小区",
            "is_whitelist": "是",
            "whitelist_reason": "重要活动保障",
            "starttime": "2026-08-01",
            "endtime": "2026-08-31",
        }

    async def fake_query_pre_sleep(cgi, stat_time):
        return {"data": [], "table": "休眠前负荷表", "high_prb_days_7d": 0}

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

    result = await energy_saving_tool.analyze_single_cell_energy(
        analysis_target="constriction",
        db=_LatestDateSession(),
        cgi="main",
    )

    assert result["constriction_main_table"] == "主小区条件表"
    assert result["constriction_relation_table"] == "关系范围表"
    assert result["constriction_load_table"] == "邻区负荷表"
    assert result["constriction_result_table"] == "收缩结果表"
    assert result["is_whitelist"] is True
    assert result["whitelist_reason"] == "重要活动保障"
    assert result["jd_starttime"] == "2026-08-01"
    assert result["jd_endtime"] == "2026-08-31"


@pytest.mark.asyncio
async def test_all_response_uses_constriction_whitelist_when_expansion_misses(
    monkeypatch,
):
    """扩展未命中时，完整分析仍须保留收缩侧的白名单风险。"""
    async def fake_query_expansion(cgi, stat_time):
        return {
            "data": [],
            "table": "扩展表",
            "cell_name": "测试小区",
            "is_whitelist": False,
        }

    async def fake_query_constriction(cgi, stat_time):
        return {
            "data": [],
            "table": "收缩表",
            "is_whitelist": True,
            "whitelist_reason": None,
            "starttime": "2026-08-01",
            "endtime": "2026-08-31",
        }

    async def fake_query_pre_sleep(cgi, stat_time):
        return {"data": [], "table": "休眠前负荷表", "high_prb_days_7d": 0}

    async def fake_param_check(db, cgi, stat_time):
        return {"success": True, "is_compliant": True, "unqualified_count": 0}

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

    assert result["is_whitelist"] is True
    assert result["whitelist_status"] == "whitelisted"
    assert result["whitelist_reason"] is None
    assert result["jd_starttime"] == "2026-08-01"
    assert result["jd_endtime"] == "2026-08-31"


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
                "reason": "邻区负荷抬升",
                "site_type": None,
                "around_cgi_network_type": "lte",
                "around_cgi_cell_name": "关联小区",
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
                "starttime": date(2026, 8, 1),
                "endtime": date(2026, 8, 31),
                "is_whitelist": "否",
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
    assert "休眠前最高PRB" in result["main_table"]
    assert result["main_table"].count("2026-08-09 23:00") == 1
    assert "80.0米" in result["relation_table"]
    assert "50.0%" in result["load_table"]
    assert "60.0%" in result["load_table"]
    assert "10.0个百分点" in result["load_table"]
    assert "剔除" in result["result_table"]
    assert result["is_whitelist"] is False
    assert result["whitelist_reason"] is None
    assert result["starttime"] == "2026-08-01"
    assert result["endtime"] == "2026-08-31"
    assert sum("jd_cell_around" in query for query in calls) == 1
    assert sum("nr_fix_prm" in query for query in calls) == 1
    assert sum("lte_fix_prm" in query for query in calls) == 1
