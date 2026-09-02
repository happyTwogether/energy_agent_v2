"""内部模型结构化业务查询计划测试。"""

from agentscope.model import StructuredResponse
from pydantic import ValidationError
import pytest

from app.self_service.models import (
    BusinessQueryPlan,
    CatalogCandidate,
    CatalogColumn,
    CatalogRelationship,
    CatalogTable,
    QueryFieldRef,
    QueryFilter,
)
from app.self_service.planner import BusinessQueryPlanner, build_planner_messages


def test_query_plan_rejects_unknown_operator() -> None:
    with pytest.raises(ValidationError):
        QueryFilter(
            field=QueryFieldRef(table="nr_report_day_detail", field="cgi"),
            operator="raw_sql",
            value="x",
        )


def test_query_plan_rejects_four_tables() -> None:
    with pytest.raises(ValidationError):
        BusinessQueryPlan(
            base_table="a",
            tables=["a", "b", "c", "d"],
            relationships=["ab", "bc", "cd"],
            select=[QueryFieldRef(table="a", field="cgi")],
        )


def test_executable_plan_requires_projection_or_aggregation() -> None:
    with pytest.raises(ValidationError):
        BusinessQueryPlan(
            base_table="nr_report_day_detail",
            tables=["nr_report_day_detail"],
        )


class FakeStructuredModel:
    def __init__(self) -> None:
        self.calls = 0
        self.messages = []

    async def generate_structured_output(self, messages, structured_model):
        self.calls += 1
        self.messages = messages
        return StructuredResponse(
            content={
                "base_table": "jd_cell_expansion_day",
                "tables": [
                    "jd_cell_expansion_day",
                    "jd_cell_constriction_day",
                    "jd_cell_around",
                ],
                "relationships": [
                    "expansion_to_constriction",
                    "constriction_to_around",
                ],
                "select": [
                    {"table": "jd_cell_expansion_day", "field": "cgi"},
                    {"table": "jd_cell_constriction_day", "field": "hours"},
                    {"table": "jd_cell_around", "field": "distance"},
                ],
                "result_grain": None,
                "limit": 50,
            },
        )


def candidate(name: str, label: str, columns: list[str]) -> CatalogCandidate:
    return CatalogCandidate(
        table=CatalogTable(
            schema_name="jd_agent",
            name=name,
            label=label,
            description=label,
            default_grain="cell_day",
            grain_keys={"cell_day": ("cgi", "stat_time")},
            columns={
                column: CatalogColumn(
                    name=column,
                    label=column,
                    data_type="character varying",
                )
                for column in columns
            },
        ),
        score=0.9,
    )


@pytest.mark.asyncio
async def test_planner_calls_internal_model_once_with_relationship_ids() -> None:
    model = FakeStructuredModel()
    planner = BusinessQueryPlanner(model=model)
    candidates = [
        candidate("jd_cell_expansion_day", "节电扩展日结果", ["cgi", "stat_time"]),
        candidate("jd_cell_constriction_day", "节电收缩日结果", ["cgi", "hours"]),
        candidate("jd_cell_around", "200米周边关联小区", ["cgi", "distance"]),
    ]
    relationships = [
        CatalogRelationship(
            name="expansion_to_constriction",
            left_table="jd_cell_expansion_day",
            right_table="jd_cell_constriction_day",
            cardinality="one_to_many",
            keys=(("cgi", "cgi"),),
            allowed_grains=("cell_day", "constriction_record"),
            detail_grain="constriction_record",
        ),
        CatalogRelationship(
            name="constriction_to_around",
            left_table="jd_cell_constriction_day",
            right_table="jd_cell_around",
            cardinality="many_to_one",
            keys=(("around_cgi", "around_cgi"),),
            allowed_grains=("cell_day", "neighbor"),
            detail_grain="neighbor",
        ),
    ]

    plan = await planner.plan(
        "查询扩展时段、收缩时段和邻区距离",
        candidates,
        relationships,
    )

    prompt = str(model.messages)
    assert model.calls == 1
    assert plan.tables == [
        "jd_cell_expansion_day",
        "jd_cell_constriction_day",
        "jd_cell_around",
    ]
    assert "expansion_to_constriction" in prompt
    assert "只能选择关系 ID" in prompt
    assert "不得输出 SQL" in prompt


def test_planner_prompt_limits_wide_table_columns_to_relevant_subset() -> None:
    wide_table = CatalogTable(
        schema_name="public",
        name="wide_table",
        label="宽表",
        description="测试宽表",
        default_date_column="data_date",
        default_grain="cell_day",
        grain_keys={"cell_day": ("cgi", "data_date")},
        columns={
            name: CatalogColumn(
                name=name,
                label=name,
                data_type="numeric",
                description=("目标字段业务说明" if name == "field_42" else ""),
            )
            for name in ["cgi", "data_date", *[f"field_{index}" for index in range(100)]]
        },
    )
    candidate_item = CatalogCandidate(
        table=wide_table,
        score=0.9,
        matched_columns=("field_42",),
    )

    from app.self_service.planner import build_planner_messages

    prompt = str(build_planner_messages("查询field_42", [candidate_item], []))

    assert "field_42" in prompt
    assert "目标字段业务说明" in prompt
    assert "data_date" in prompt
    assert "cgi" in prompt
    assert "field_99" not in prompt


def test_planner_prompt_exposes_only_matched_registered_metrics() -> None:
    summary = CatalogTable(
        schema_name="public",
        name="nr_report_day_collect",
        label="5G网络日汇总",
        description="5G网络日汇总",
        default_date_column="data_date",
        default_grain="summary_day",
        grain_keys={"summary_day": ("data_date",)},
        columns={
            name: CatalogColumn(name=name, label=name, data_type="numeric")
            for name in [
                "data_date",
                "logic_station_total",
                "logic_read_station_total",
            ]
        },
    )
    candidate_item = CatalogCandidate(
        table=summary,
        score=1.0,
        matched_metrics=("nr_readable_ratio",),
    )

    from app.self_service.planner import build_planner_messages

    prompt = str(build_planner_messages("查询5G基站可读率", [candidate_item], []))

    assert "nr_readable_ratio" in prompt
    assert "5G基站可读率" in prompt
    assert "logic_read_station_total" in prompt
    assert "指标只能放入 metrics" in prompt


def test_planner_prompt_allows_registered_metric_grouping_and_sorting() -> None:
    summary = CatalogTable(
        schema_name="public",
        name="nr_report_day_collect",
        label="5G网络日汇总",
        description="5G网络日汇总",
        default_date_column="data_date",
        default_grain="summary_day",
        grain_keys={"summary_day": ("data_date",)},
        columns={
            name: CatalogColumn(name=name, label=name, data_type="numeric")
            for name in [
                "data_date",
                "dist_name",
                "logic_station_total",
                "logic_read_station_total",
            ]
        },
    )
    candidate_item = CatalogCandidate(
        table=summary,
        score=1.0,
        matched_metrics=("nr_readable_ratio",),
    )

    prompt = str(build_planner_messages(
        "按地市查5G基站可读率前5名",
        [candidate_item],
        [],
    ))

    assert "计算指标可以用于分组结果的排序" in prompt
    assert "不能把计算指标用于过滤" in prompt
