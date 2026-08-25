"""批量节电统计口径测试。"""

from datetime import date, datetime

import pytest

from app.tools import batch_energy_tool


def test_batch_total_is_counted_before_issue_filtering():
    table_data = [
        {"CGI": "a", "节能扩展：容量与风险说明": "非高负荷"},
        {"CGI": "b", "节能扩展：容量与风险说明": "高负荷"},
    ]

    stats = batch_energy_tool._compute_batch_stats(table_data)

    assert stats["total"] == 2


def test_parameter_noncompliant_count_excludes_missing_states():
    rows = [
        {"cgi": "a", "saving_switch_state": "不合格"},
        {"cgi": "a", "saving_switch_state": "参数异常"},
        {"cgi": "b", "saving_switch_state": "北向数据缺失"},
        {"cgi": "c", "saving_switch_state": "合格"},
        {"cgi": "d", "saving_switch_state": None},
    ]

    assert batch_energy_tool._count_noncompliant_cells(rows) == 1


def test_pre_sleep_count_uses_distinct_dates_per_cell():
    rows = [
        {"cgi": "a", "stat_time": date(2026, 8, 9)},
        {"cgi": "a", "stat_time": datetime(2026, 8, 9, 12)},
        {"cgi": "a", "stat_time": date(2026, 8, 10)},
        {"cgi": "b", "stat_time": date(2026, 8, 10)},
    ]

    assert batch_energy_tool._count_pre_sleep_days_by_cgi(rows) == {
        "a": 2,
        "b": 1,
    }


@pytest.mark.asyncio
async def test_batch_pre_sleep_query_filters_dirty_metrics_in_python(monkeypatch):
    async def fake_fetch_rows(sql, params):
        query = str(sql)
        assert "prb_rate_ul >" not in query
        assert "prb_rate_dl >" not in query
        assert "prb_threshold" not in params
        return [
            {
                "cgi": "a",
                "stat_time": date(2026, 8, 9),
                "prb_hour": "23",
                "sleep_hour": "0",
                "prb_rate_ul": "51",
                "prb_rate_dl": "",
            },
            {
                "cgi": "a",
                "stat_time": date(2026, 8, 8),
                "prb_hour": "",
                "sleep_hour": "",
                "prb_rate_ul": "",
                "prb_rate_dl": "",
            },
            {
                "cgi": "a",
                "stat_time": date(2026, 8, 7),
                "prb_hour": "23",
                "sleep_hour": "0",
                "prb_rate_ul": "50",
                "prb_rate_dl": None,
            },
        ]

    monkeypatch.setattr(batch_energy_tool, "fetch_rows", fake_fetch_rows)

    rows = await batch_energy_tool._query_pre_sleep_rows(
        {"a"},
        datetime(2026, 8, 10),
    )

    assert len(rows) == 1
    assert rows[0]["stat_time"] == date(2026, 8, 9)


def test_batch_whitelist_accepts_database_boolean():
    """PostgreSQL boolean 白名单值不能因只识别中文“是”而漏计。"""
    table_data, stats = batch_energy_tool._build_table_data(
        all_cgis={"a"},
        expansion_map={
            "a": {
                "cgi": "a",
                "is_highload": "否",
                "is_whitelist": True,
                "reason": "测试原因",
            }
        },
        constriction_map={},
        sleep_count_map={},
        dist_name=None,
        county_name=None,
        prod_name=None,
        analysis_target="expansion",
    )

    assert stats["whitelist"] == 1
    assert "测试原因" in table_data[0][batch_energy_tool._COL_WHITELIST]


@pytest.mark.asyncio
async def test_batch_analysis_exports_complete_v14_sheets(monkeypatch):
    """正常小区保留在汇总，三类原始证据写入同一工作簿。"""
    captured_sheets = {}

    async def fake_latest_date(need_constriction):
        return datetime(2026, 8, 10)

    async def fake_fetch_rows(sql, params):
        query = str(sql)
        if "jd_cell_expansion_day" in query:
            assert "deploy_hours_continuous" in query
            return [
                {
                    "cgi": "a",
                    "is_highload": "否",
                    "hour_filter": None,
                    "saving_switch_state": "合格",
                },
                {
                    "cgi": "b",
                    "is_highload": "下高",
                    "hour_filter": "22",
                    "saving_switch_state": "不合格",
                },
            ]
        if "jd_cell_constriction_day" in query:
            assert "self_sleep_duration" in query
            assert "prb_increase" in query
            return [
                {
                    "cgi": "b",
                    "around_cgi": "neighbor",
                    "around_cgi_network_type": "lte",
                    "site_type": None,
                    "hours": 0,
                    "prb_increase": 10.0,
                }
            ]
        if "jd_cell_around" in query:
            return [
                {
                    "cgi": "b",
                    "around_cgi": "neighbor",
                    "around_cgi_network_type": "lte",
                    "distance": 80.0,
                }
            ]
        if "nr_fix_prm" in query:
            return [{"cgi": "b", "site_type": "室分"}]
        if "lte_fix_prm" in query:
            return [{"cgi": "neighbor", "site_type": "宏站"}]
        if "jd_cell_pre_hour_busy" in query:
            assert "cgi = ANY(:cgis)" in query
            assert "stat_time <= :end_date" in query
            assert set(params["cgis"]) == {"b"}
            assert "prb_rate_ul >" not in query
            assert "prb_rate_dl >" not in query
            assert "prb_threshold" not in params
            return [
                {
                    "cgi": "b",
                    "stat_time": date(2026, 8, 9),
                    "prb_hour": 23,
                    "sleep_hour": "0",
                    "prb_rate_ul": 51.0,
                    "prb_rate_dl": 1.0,
                },
                {
                    "cgi": "b",
                    "stat_time": date(2026, 8, 8),
                    "prb_hour": 22,
                    "sleep_hour": "0",
                    "prb_rate_ul": 90.0,
                    "prb_rate_dl": 1.0,
                },
            ]
        raise AssertionError(query)

    def fake_export_sheets(sheets, prefix, column_mapping=None):
        captured_sheets.update(sheets)
        return "/downloads/batch.xlsx"

    monkeypatch.setattr(batch_energy_tool, "_resolve_latest_date", fake_latest_date)
    monkeypatch.setattr(batch_energy_tool, "fetch_rows", fake_fetch_rows)
    monkeypatch.setattr(
        batch_energy_tool,
        "export_sheets_to_excel",
        fake_export_sheets,
    )

    result = await batch_energy_tool._do_analyze(
        dist_name=None,
        county_name=None,
        prod_name=None,
        stat_time=None,
        analysis_target="all",
    )

    assert result["stats"]["total"] == 2
    assert result["stats"]["problem_total"] == 1
    assert result["stats"]["param_noncompliant"] == 1
    assert len(captured_sheets["小区汇总"]) == 2
    summary_b = next(row for row in captured_sheets["小区汇总"] if row["CGI"] == "b")
    assert summary_b["扩展时段（含扩展时段）"] == "22"
    assert summary_b["收缩记录数"] == 1
    assert summary_b["需剔除的原节电时段"] == "0"
    assert summary_b["受影响关联小区数"] == 1
    assert summary_b["最大PRB抬升量"] == 10.0
    assert captured_sheets["收缩明细"][0]["prb_increase"] == 10.0
    assert captured_sheets["收缩明细"][0]["distance"] == 80.0
    assert captured_sheets["收缩明细"][0]["around_site_type"] == "宏站"
    assert "main_site_type" not in captured_sheets["收缩明细"][0]
    assert list(captured_sheets) == ["小区汇总", "扩展明细", "收缩明细"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("analysis_target", "expected_sheet", "forbidden_table"),
    [
        ("expansion", "扩展明细", "jd_cell_constriction_day"),
        ("constriction", "收缩明细", "jd_cell_expansion_day"),
    ],
)
async def test_single_dimension_batch_exports_only_its_detail_sheet(
    monkeypatch,
    analysis_target,
    expected_sheet,
    forbidden_table,
):
    captured_sheets = {}
    queries: list[str] = []

    async def fake_latest_date(target):
        assert target == analysis_target
        return datetime(2026, 8, 10)

    async def fake_fetch_rows(sql, params):
        query = str(sql)
        queries.append(query)
        if "jd_cell_expansion_day" in query:
            return [{"cgi": "a", "stat_time": date(2026, 8, 10)}]
        if "jd_cell_constriction_day" in query:
            return [{
                "cgi": "a",
                "stat_time": date(2026, 8, 10),
                "hours": 1,
                "around_cgi": None,
            }]
        if "jd_cell_pre_hour_busy" in query:
            return []
        raise AssertionError(query)

    async def fake_enrich(rows):
        return rows

    def fake_export(sheets, prefix, column_mapping=None):
        captured_sheets.update(sheets)
        return "/downloads/batch.xlsx"

    monkeypatch.setattr(batch_energy_tool, "_resolve_latest_date", fake_latest_date)
    monkeypatch.setattr(batch_energy_tool, "fetch_rows", fake_fetch_rows)
    monkeypatch.setattr(batch_energy_tool, "_enrich_batch_constriction_rows", fake_enrich)
    monkeypatch.setattr(batch_energy_tool, "export_sheets_to_excel", fake_export)

    result = await batch_energy_tool._do_analyze(
        dist_name=None,
        county_name=None,
        prod_name=None,
        stat_time=None,
        analysis_target=analysis_target,
    )

    assert result["success"] is True
    assert list(captured_sheets) == [expected_sheet]
    assert all(forbidden_table not in query for query in queries)


def test_export_field_orders_match_confirmed_csv_headers():
    assert len(batch_energy_tool.EXPANSION_EXPORT_FIELDS) == 23
    assert batch_energy_tool.EXPANSION_EXPORT_FIELDS[:3] == (
        "stat_time", "dist_name", "county_name",
    )
    assert batch_energy_tool.EXPANSION_EXPORT_FIELDS[-2:] == (
        "deploy_hours_early", "deploy_hours_continuous_early",
    )
    assert len(batch_energy_tool.CONSTRICTION_EXPORT_FIELDS) == 27
    assert batch_energy_tool.CONSTRICTION_EXPORT_FIELDS[-2:] == (
        "distance", "around_site_type",
    )
    assert batch_energy_tool.ENERGY_EXPORT_COLUMN_MAPPING["distance"] == "关联小区距离（米）"
    assert batch_energy_tool.ENERGY_EXPORT_COLUMN_MAPPING["around_site_type"] == "关联小区站型"


def test_batch_summary_ignores_dirty_prb_increase_values():
    rows = [{"CGI": "a"}]

    batch_energy_tool._augment_summary_rows(
        rows,
        expansion_map={},
        constriction_rows=[
            {"cgi": "a", "hours": "", "around_cgi": "n1", "prb_increase": ""},
            {"cgi": "a", "hours": "1", "around_cgi": "n2", "prb_increase": "bad"},
            {"cgi": "a", "hours": 2, "around_cgi": "n2", "prb_increase": "10.5"},
        ],
    )

    assert rows[0]["需剔除的原节电时段"] == "1、2"
    assert rows[0]["最大PRB抬升量"] == 10.5
