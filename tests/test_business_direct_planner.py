"""确定性单表字段查询规划测试。"""

from app.self_service.direct_planner import build_direct_lookup_plan
from app.self_service.models import CatalogCandidate, CatalogColumn, CatalogTable


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
