"""确定性单表字段查询规划测试。"""

from app.self_service.direct_planner import (
    build_dimension_value_lookup,
    build_direct_lookup_plan,
)
from app.self_service.models import (
    CatalogCandidate,
    CatalogColumn,
    CatalogSnapshot,
    CatalogTable,
)


def _candidate(*, include_cgi: bool = True) -> CatalogCandidate:
    column_names = ["data_date", "deepsleep_hour", "deepsleep_switch"]
    if include_cgi:
        column_names.insert(0, "cgi")
    return CatalogCandidate(
        table=CatalogTable(
            schema_name="public",
            name="nr_report_day_detail",
            label="5G小区日指标明细",
            description="5G小区日指标明细",
            aliases=("5G小区指标",),
            default_date_column="data_date",
            default_grain="cell_day",
            grain_keys={
                "cell_day": (
                    ("cgi", "data_date")
                    if include_cgi
                    else ("data_date",)
                ),
            },
            columns={
                name: CatalogColumn(
                    name=name,
                    label=name,
                    data_type="character varying",
                )
                for name in column_names
            },
        ),
        score=0.95,
        matched_columns=("deepsleep_hour", "deepsleep_switch"),
    )


def test_direct_lookup_rejects_table_without_cgi_filter_field() -> None:
    plan = build_direct_lookup_plan(
        "查询5G小区460-00-2539193-71在2026年8月24日的"
        "深度休眠时长和深度休眠开关",
        [_candidate(include_cgi=False)],
    )

    assert plan is None


def _dimension_snapshot() -> CatalogSnapshot:
    tables = {
        name: CatalogTable(
            schema_name="public",
            name=name,
            label=name,
            description=name,
            default_date_column="data_date",
            default_grain="summary_day",
            grain_keys={"summary_day": ("data_date", "dist_name", "prod_name")},
            columns={
                column: CatalogColumn(
                    name=column,
                    label=column,
                    data_type="character varying",
                )
                for column in (
                    "data_date",
                    "province",
                    "dist_name",
                    "prod_name",
                    "freq_band",
                    "site_type",
                    "area",
                )
            },
        )
        for name in ("lte_report_day_collect", "nr_report_day_collect")
    }
    return CatalogSnapshot(version="1", tables=tables, relationships={})


def test_dimension_lookup_builds_4g_and_5g_grouped_plans() -> None:
    lookup = build_dimension_value_lookup(
        "那邵阳有哪些厂家可以看",
        _dimension_snapshot(),
    )

    assert lookup is not None
    assert lookup.dimension == "prod_name"
    assert [plan.base_table for plan in lookup.plans] == [
        "lte_report_day_collect",
        "nr_report_day_collect",
    ]
    assert all(plan.select == plan.group_by for plan in lookup.plans)
    assert all(plan.filters[0].value == "邵阳市" for plan in lookup.plans)


def test_dimension_lookup_supports_other_dimensions_and_network_scope() -> None:
    lookup = build_dimension_value_lookup(
        "邵阳5G有哪些频段",
        _dimension_snapshot(),
    )

    assert lookup is not None
    assert lookup.dimension == "freq_band"
    assert [plan.base_table for plan in lookup.plans] == [
        "nr_report_day_collect",
    ]
    assert lookup.plans[0].filters[0].value == "邵阳市"


def test_dimension_lookup_accepts_common_vendor_phrasings() -> None:
    for question in (
        "邵阳市有什么厂商",
        "邵阳能看哪些设备厂家",
        "邵阳的厂家有哪些",
    ):
        lookup = build_dimension_value_lookup(question, _dimension_snapshot())
        assert lookup is not None
        assert lookup.plans[0].filters[0].value == "邵阳市"

    assert build_dimension_value_lookup(
        "查询能耗报告",
        _dimension_snapshot(),
    ) is None


def test_dimension_lookup_keeps_province_and_city_filters_separate() -> None:
    lookup = build_dimension_value_lookup(
        "湖南省邵阳市有哪些厂家",
        _dimension_snapshot(),
    )

    assert lookup is not None
    assert [
        (item.field.field, item.value)
        for item in lookup.plans[0].filters
    ] == [
        ("province", "湖南省"),
        ("dist_name", "邵阳市"),
    ]
