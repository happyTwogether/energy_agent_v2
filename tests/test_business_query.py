"""受控 SQLAlchemy 多表查询构造测试。"""

import pytest
from sqlalchemy.dialects import postgresql

from app.core.config import Settings
from app.self_service.models import (
    BusinessQueryPlan,
    CatalogColumn,
    CatalogRelationship,
    CatalogTable,
    QueryAggregation,
    QueryFieldRef,
    QueryFilter,
    QueryOrder,
)
from app.self_service.query import (
    BusinessQueryValidationError,
    build_business_statement,
    validate_query_plan,
)
from tests.test_business_relationships import snapshot as snapshot_fixture


def get_snapshot():
    return snapshot_fixture.__wrapped__()


def get_nr_summary_snapshot():
    snapshot = get_snapshot()
    table = CatalogTable(
        schema_name="public",
        name="nr_report_day_collect",
        label="5G网络日汇总",
        description="5G网络日汇总",
        default_date_column="data_date",
        default_grain="summary_day",
        grain_keys={"summary_day": ("data_date",)},
        columns={
            name: CatalogColumn(name=name, label=name, data_type=data_type)
            for name, data_type in {
                "data_date": "date",
                "dist_name": "text",
                "logic_station_total": "numeric",
                "logic_read_station_total": "numeric",
                "thirtytwo_channel_total": "numeric",
                "sixtyfour_channel_total": "numeric",
                "all_cell_total": "numeric",
            }.items()
        },
    )
    return snapshot.model_copy(update={
        "tables": {**snapshot.tables, table.name: table},
    })


def test_metric_plan_selects_registered_source_fields() -> None:
    snapshot = get_nr_summary_snapshot()
    plan = BusinessQueryPlan(
        base_table="nr_report_day_collect",
        tables=["nr_report_day_collect"],
        select=[QueryFieldRef(table="nr_report_day_collect", field="data_date")],
        metrics=["nr_high_power_ratio"],
    )

    validated = validate_query_plan(plan, snapshot, Settings(_env_file=None))
    sql = str(build_business_statement(validated, snapshot).compile(
        dialect=postgresql.dialect(),
    ))

    assert "thirtytwo_channel_total" in sql
    assert "sixtyfour_channel_total" in sql
    assert "all_cell_total" in sql


def test_metric_plan_rejects_unknown_metric() -> None:
    snapshot = get_nr_summary_snapshot()
    plan = BusinessQueryPlan(
        base_table="nr_report_day_collect",
        tables=["nr_report_day_collect"],
        metrics=["model_invented_ratio"],
    )

    with pytest.raises(BusinessQueryValidationError, match="未授权指标"):
        validate_query_plan(plan, snapshot, Settings(_env_file=None))


def test_grouped_metric_aggregates_sources_and_orders_top_n_by_formula() -> None:
    snapshot = get_nr_summary_snapshot()
    dimension = QueryFieldRef(
        table="nr_report_day_collect",
        field="dist_name",
    )
    plan = BusinessQueryPlan(
        base_table="nr_report_day_collect",
        tables=["nr_report_day_collect"],
        select=[dimension],
        group_by=[dimension],
        metrics=["nr_readable_ratio"],
        order_by=[QueryOrder(
            metric_id="nr_readable_ratio",
            direction="desc",
        )],
        limit=5,
    )

    validated = validate_query_plan(plan, snapshot, Settings(_env_file=None))
    sql = str(build_business_statement(validated, snapshot).compile(
        dialect=postgresql.dialect(),
    ))

    assert "sum(public.nr_report_day_collect.logic_read_station_total)" in sql
    assert "sum(public.nr_report_day_collect.logic_station_total)" in sql
    assert "AS nr_readable_ratio" in sql
    assert "ORDER BY nr_readable_ratio DESC" in sql
    assert "LIMIT" in sql


def test_grouped_metric_can_use_dimension_from_authorized_join() -> None:
    snapshot = get_nr_summary_snapshot()
    region_table = CatalogTable(
        schema_name="public",
        name="region_dimension",
        label="地市分区维表",
        description="地市与分区的授权映射",
        default_grain="summary_day",
        grain_keys={"summary_day": ("dist_name",)},
        columns={
            "dist_name": CatalogColumn(
                name="dist_name",
                label="地市",
                data_type="text",
            ),
            "region": CatalogColumn(
                name="region",
                label="分区",
                data_type="text",
            ),
        },
    )
    relationship = CatalogRelationship(
        name="summary_to_region",
        left_table="nr_report_day_collect",
        right_table="region_dimension",
        cardinality="many_to_one",
        keys=(("dist_name", "dist_name"),),
        allowed_grains=("summary_day",),
    )
    snapshot = snapshot.model_copy(update={
        "tables": {**snapshot.tables, region_table.name: region_table},
        "relationships": {
            **snapshot.relationships,
            relationship.name: relationship,
        },
    })
    dimension = QueryFieldRef(table="region_dimension", field="region")
    plan = BusinessQueryPlan(
        base_table="nr_report_day_collect",
        tables=["nr_report_day_collect", "region_dimension"],
        relationships=["summary_to_region"],
        select=[dimension],
        group_by=[dimension],
        metrics=["nr_readable_ratio"],
        order_by=[QueryOrder(
            metric_id="nr_readable_ratio",
            direction="desc",
        )],
    )

    validated = validate_query_plan(plan, snapshot, Settings(_env_file=None))
    sql = str(build_business_statement(validated, snapshot).compile(
        dialect=postgresql.dialect(),
    ))

    assert "sum(base_filtered.logic_read_station_total)" in sql
    assert "sum(base_filtered.logic_station_total)" in sql
    assert "GROUP BY public.region_dimension.region" in sql
    assert "ORDER BY nr_readable_ratio DESC" in sql


def test_aggregation_aliases_must_be_unique() -> None:
    snapshot = get_snapshot()
    plan = BusinessQueryPlan(
        base_table="jd_cell_expansion_day",
        tables=["jd_cell_expansion_day"],
        aggregations=[
            QueryAggregation(function="count", alias="小区数"),
            QueryAggregation(function="count", alias="小区数"),
        ],
    )

    with pytest.raises(BusinessQueryValidationError, match="聚合别名不能重复"):
        validate_query_plan(plan, snapshot, Settings(_env_file=None))


def test_split_date_bounds_cannot_bypass_maximum_range() -> None:
    snapshot = get_nr_summary_snapshot()
    date_field = QueryFieldRef(
        table="nr_report_day_collect",
        field="data_date",
    )
    plan = BusinessQueryPlan(
        base_table="nr_report_day_collect",
        tables=["nr_report_day_collect"],
        select=[date_field],
        filters=[
            QueryFilter(field=date_field, operator="gte", value="2000-01-01"),
            QueryFilter(field=date_field, operator="lte", value="2026-08-31"),
        ],
    )

    with pytest.raises(BusinessQueryValidationError, match="最多允许 90 天"):
        validate_query_plan(plan, snapshot, Settings(_env_file=None))


def test_one_sided_date_filter_is_rejected() -> None:
    snapshot = get_nr_summary_snapshot()
    date_field = QueryFieldRef(
        table="nr_report_day_collect",
        field="data_date",
    )
    plan = BusinessQueryPlan(
        base_table="nr_report_day_collect",
        tables=["nr_report_day_collect"],
        select=[date_field],
        filters=[QueryFilter(
            field=date_field,
            operator="gte",
            value="2026-08-01",
        )],
    )

    with pytest.raises(BusinessQueryValidationError, match="开始和结束"):
        validate_query_plan(plan, snapshot, Settings(_env_file=None))


def test_multi_table_query_rejects_base_table_without_date_boundary() -> None:
    snapshot = get_snapshot()
    plan = BusinessQueryPlan(
        base_table="jd_cell_around",
        tables=["jd_cell_around", "jd_cell_constriction_day"],
        relationships=["constriction_to_around"],
        select=[QueryFieldRef(table="jd_cell_around", field="distance")],
    )

    with pytest.raises(BusinessQueryValidationError, match="主表必须包含日期字段"):
        validate_query_plan(plan, snapshot, Settings(_env_file=None))


def test_three_table_default_grain_preaggregates_one_to_many() -> None:
    snapshot = get_snapshot()
    plan = BusinessQueryPlan(
        base_table="jd_cell_expansion_day",
        tables=[
            "jd_cell_expansion_day",
            "jd_cell_constriction_day",
            "jd_cell_around",
        ],
        relationships=["expansion_to_constriction", "constriction_to_around"],
        select=[
            QueryFieldRef(table="jd_cell_expansion_day", field="cgi"),
            QueryFieldRef(table="jd_cell_constriction_day", field="hours"),
            QueryFieldRef(table="jd_cell_around", field="distance"),
        ],
        filters=[
            QueryFilter(
                field=QueryFieldRef(table="jd_cell_expansion_day", field="cgi"),
                operator="eq",
                value="460-00-2539193-71",
            ),
        ],
    )
    validated = validate_query_plan(plan, snapshot, Settings(_env_file=None))

    statement = build_business_statement(validated, snapshot)
    compiled = statement.compile(dialect=postgresql.dialect())
    sql = str(compiled)

    assert "array_agg(DISTINCT" in sql
    assert "jd_cell_constriction_day" in sql
    assert "jd_cell_around" in sql
    assert "460-00-2539193-71" not in sql
    assert "460-00-2539193-71" in compiled.params.values()


def test_single_table_query_has_no_join() -> None:
    snapshot = get_snapshot()
    plan = BusinessQueryPlan(
        base_table="jd_cell_expansion_day",
        tables=["jd_cell_expansion_day"],
        select=[
            QueryFieldRef(table="jd_cell_expansion_day", field="cgi"),
            QueryFieldRef(table="jd_cell_expansion_day", field="deploy_hours"),
        ],
    )
    validated = validate_query_plan(plan, snapshot, Settings(_env_file=None))

    statement = build_business_statement(validated, snapshot)
    sql = str(statement.compile(dialect=postgresql.dialect()))

    assert " JOIN " not in sql
    assert "LIMIT" in sql


def test_order_by_unselected_field_adds_internal_select_expression() -> None:
    snapshot = get_snapshot()
    plan = BusinessQueryPlan(
        base_table="jd_cell_expansion_day",
        tables=["jd_cell_expansion_day"],
        select=[QueryFieldRef(table="jd_cell_expansion_day", field="cgi")],
        order_by=[QueryOrder(
            field=QueryFieldRef(
                table="jd_cell_expansion_day",
                field="deploy_hours",
            ),
            direction="desc",
        )],
    )

    validated = validate_query_plan(plan, snapshot, Settings(_env_file=None))
    sql = str(build_business_statement(validated, snapshot).compile(
        dialect=postgresql.dialect(),
    ))

    assert "deploy_hours AS jd_cell_expansion_day__deploy_hours" in sql
    assert "ORDER BY jd_cell_expansion_day__deploy_hours DESC" in sql


def test_single_constriction_table_defaults_to_one_cell_day_row() -> None:
    snapshot = get_snapshot()
    plan = BusinessQueryPlan(
        base_table="jd_cell_constriction_day",
        tables=["jd_cell_constriction_day"],
        select=[
            QueryFieldRef(table="jd_cell_constriction_day", field="cgi"),
            QueryFieldRef(table="jd_cell_constriction_day", field="stat_time"),
            QueryFieldRef(table="jd_cell_constriction_day", field="hours"),
        ],
    )
    validated = validate_query_plan(plan, snapshot, Settings(_env_file=None))

    sql = str(build_business_statement(validated, snapshot).compile(
        dialect=postgresql.dialect(),
    ))

    assert "array_agg(DISTINCT jd_agent.jd_cell_constriction_day.hours)" in sql
    assert "GROUP BY jd_agent.jd_cell_constriction_day.cgi" in sql
    assert "jd_agent.jd_cell_constriction_day.stat_time" in sql


def test_constriction_with_neighbor_defaults_to_one_cell_day_row() -> None:
    snapshot = get_snapshot()
    plan = BusinessQueryPlan(
        base_table="jd_cell_constriction_day",
        tables=["jd_cell_constriction_day", "jd_cell_around"],
        relationships=["constriction_to_around"],
        select=[
            QueryFieldRef(table="jd_cell_constriction_day", field="cgi"),
            QueryFieldRef(table="jd_cell_constriction_day", field="hours"),
            QueryFieldRef(table="jd_cell_around", field="distance"),
        ],
    )
    validated = validate_query_plan(plan, snapshot, Settings(_env_file=None))

    sql = str(build_business_statement(validated, snapshot).compile(
        dialect=postgresql.dialect(),
    ))

    assert "array_agg(DISTINCT base_filtered.hours)" in sql
    assert "array_agg(DISTINCT jd_agent.jd_cell_around.distance)" in sql
    assert "FILTER (WHERE jd_agent.jd_cell_around.distance IS NOT NULL)" in sql
    assert "GROUP BY base_filtered.cgi, base_filtered.stat_time" in sql


def test_child_filter_is_applied_inside_preaggregation_branch() -> None:
    snapshot = get_snapshot()
    plan = BusinessQueryPlan(
        base_table="jd_cell_expansion_day",
        tables=[
            "jd_cell_expansion_day",
            "jd_cell_constriction_day",
            "jd_cell_around",
        ],
        relationships=["expansion_to_constriction", "constriction_to_around"],
        select=[
            QueryFieldRef(table="jd_cell_expansion_day", field="cgi"),
            QueryFieldRef(table="jd_cell_around", field="distance"),
        ],
        filters=[
            QueryFilter(
                field=QueryFieldRef(table="jd_cell_around", field="distance"),
                operator="lte",
                value=200,
            ),
        ],
    )
    validated = validate_query_plan(plan, snapshot, Settings(_env_file=None))

    compiled = build_business_statement(validated, snapshot).compile(
        dialect=postgresql.dialect(),
    )

    assert "distance <=" in str(compiled)
    assert 200 in compiled.params.values()
    assert "LEFT OUTER JOIN branch_1" not in str(compiled)


def test_many_to_one_latest_snapshot_stays_scalar() -> None:
    snapshot = get_snapshot()
    nr_prm = CatalogTable(
        schema_name="public",
        name="nr_fix_prm",
        label="5G小区工参",
        description="5G小区工参",
        default_date_column="data_date",
        default_grain="cell_snapshot",
        grain_keys={"cell_snapshot": ("cgi", "data_date")},
        columns={
            "cgi": CatalogColumn(name="cgi", label="CGI", data_type="text"),
            "data_date": CatalogColumn(name="data_date", label="日期", data_type="date"),
            "site_type": CatalogColumn(name="site_type", label="站型", data_type="text"),
        },
    )
    relationship = CatalogRelationship(
        name="expansion_to_nr_prm",
        left_table="jd_cell_expansion_day",
        right_table="nr_fix_prm",
        cardinality="many_to_one_latest_snapshot",
        keys=(("cgi", "cgi"),),
        snapshot_column="data_date",
        allowed_grains=("cell_day",),
    )
    snapshot = snapshot.model_copy(update={
        "tables": {**snapshot.tables, nr_prm.name: nr_prm},
        "relationships": {**snapshot.relationships, relationship.name: relationship},
    })
    plan = BusinessQueryPlan(
        base_table="jd_cell_expansion_day",
        tables=["jd_cell_expansion_day", "nr_fix_prm"],
        relationships=["expansion_to_nr_prm"],
        select=[
            QueryFieldRef(table="jd_cell_expansion_day", field="cgi"),
            QueryFieldRef(table="nr_fix_prm", field="site_type"),
        ],
    )
    validated = validate_query_plan(plan, snapshot, Settings(_env_file=None))

    sql = str(build_business_statement(validated, snapshot).compile(
        dialect=postgresql.dialect(),
    ))

    assert "max(" in sql.lower()
    assert "GROUP BY public.nr_fix_prm.cgi" in sql
    assert "array_agg" not in sql.lower()


def test_one_detail_axis_keeps_other_one_to_many_branch_aggregated() -> None:
    snapshot = get_snapshot()
    plan = BusinessQueryPlan(
        base_table="jd_cell_expansion_day",
        tables=[
            "jd_cell_expansion_day",
            "jd_cell_detail_hour_nr",
            "jd_cell_constriction_day",
        ],
        relationships=["expansion_to_hour", "expansion_to_constriction"],
        select=[
            QueryFieldRef(table="jd_cell_expansion_day", field="cgi"),
            QueryFieldRef(table="jd_cell_detail_hour_nr", field="hours"),
            QueryFieldRef(table="jd_cell_constriction_day", field="hours"),
        ],
        result_grain="hour",
    )
    validated = validate_query_plan(plan, snapshot, Settings(_env_file=None))

    sql = str(build_business_statement(validated, snapshot).compile(
        dialect=postgresql.dialect(),
    ))

    assert "jd_cell_detail_hour_nr.hours AS jd_cell_detail_hour_nr__hours" in sql
    assert "array_agg(DISTINCT jd_agent.jd_cell_constriction_day.hours)" in sql
    assert sql.count("branch_1") >= 2


def test_downstream_neighbor_detail_does_not_duplicate_constriction_hours() -> None:
    snapshot = get_snapshot()
    plan = BusinessQueryPlan(
        base_table="jd_cell_expansion_day",
        tables=[
            "jd_cell_expansion_day",
            "jd_cell_constriction_day",
            "jd_cell_around",
        ],
        relationships=["expansion_to_constriction", "constriction_to_around"],
        select=[
            QueryFieldRef(table="jd_cell_expansion_day", field="cgi"),
            QueryFieldRef(table="jd_cell_constriction_day", field="hours"),
            QueryFieldRef(table="jd_cell_around", field="around_cgi"),
            QueryFieldRef(table="jd_cell_around", field="distance"),
        ],
        result_grain="neighbor",
    )
    validated = validate_query_plan(plan, snapshot, Settings(_env_file=None))

    sql = str(build_business_statement(validated, snapshot).compile(
        dialect=postgresql.dialect(),
    ))

    assert "array_agg(DISTINCT jd_agent.jd_cell_constriction_day.hours)" in sql
    assert "max(jd_agent.jd_cell_around.distance)" in sql
    assert "GROUP BY base_filtered.cgi, base_filtered.stat_time" in sql
    assert "jd_agent.jd_cell_around.around_cgi" in sql


def test_multi_table_aggregation_groups_scalar_relationship_fields() -> None:
    snapshot = get_snapshot()
    nr_prm = CatalogTable(
        schema_name="public",
        name="nr_fix_prm",
        label="5G小区工参",
        description="5G小区工参",
        default_date_column="data_date",
        default_grain="cell_snapshot",
        grain_keys={"cell_snapshot": ("cgi", "data_date")},
        columns={
            "cgi": CatalogColumn(name="cgi", label="CGI", data_type="text"),
            "data_date": CatalogColumn(name="data_date", label="日期", data_type="date"),
            "site_type": CatalogColumn(name="site_type", label="站型", data_type="text"),
        },
    )
    relationship = CatalogRelationship(
        name="expansion_to_nr_prm",
        left_table="jd_cell_expansion_day",
        right_table="nr_fix_prm",
        cardinality="many_to_one_latest_snapshot",
        keys=(("cgi", "cgi"),),
        snapshot_column="data_date",
        allowed_grains=("cell_day",),
    )
    snapshot = snapshot.model_copy(update={
        "tables": {**snapshot.tables, nr_prm.name: nr_prm},
        "relationships": {**snapshot.relationships, relationship.name: relationship},
    })
    plan = BusinessQueryPlan(
        base_table="jd_cell_expansion_day",
        tables=["jd_cell_expansion_day", "nr_fix_prm"],
        relationships=["expansion_to_nr_prm"],
        select=[QueryFieldRef(table="nr_fix_prm", field="site_type")],
        group_by=[QueryFieldRef(table="nr_fix_prm", field="site_type")],
        aggregations=[QueryAggregation(function="count", alias="小区数")],
    )
    validated = validate_query_plan(plan, snapshot, Settings(_env_file=None))

    sql = str(build_business_statement(validated, snapshot).compile(
        dialect=postgresql.dialect(),
    ))

    assert "count(*) as" in sql.lower()
    assert "GROUP BY nr_fix_prm_latest.site_type" in sql


def test_multi_table_aggregation_rejects_preaggregated_child_metric() -> None:
    snapshot = get_snapshot()
    plan = BusinessQueryPlan(
        base_table="jd_cell_expansion_day",
        tables=["jd_cell_expansion_day", "jd_cell_constriction_day"],
        relationships=["expansion_to_constriction"],
        aggregations=[
            QueryAggregation(
                function="avg",
                field=QueryFieldRef(table="jd_cell_constriction_day", field="hours"),
                alias="平均时段",
            ),
        ],
    )

    with pytest.raises(BusinessQueryValidationError, match="先展开该明细"):
        validate_query_plan(plan, snapshot, Settings(_env_file=None))
