"""白名单关系路径和结果粒度安全测试。"""

from pydantic import ValidationError
import pytest

from app.core.config import Settings
from app.self_service.models import (
    BusinessQueryPlan,
    CatalogColumn,
    CatalogRelationship,
    CatalogSnapshot,
    CatalogTable,
    QueryFieldRef,
    QueryFilter,
)
from app.self_service.query import (
    BusinessQueryValidationError,
    validate_query_plan,
)


def make_table(
    name: str,
    columns: dict[str, str],
    *,
    default_grain: str = "cell_day",
    grain_keys: dict[str, tuple[str, ...]] | None = None,
) -> CatalogTable:
    return CatalogTable(
        schema_name="jd_agent",
        name=name,
        label=name,
        description=name,
        default_date_column="stat_time" if "stat_time" in columns else None,
        default_grain=default_grain,
        grain_keys=grain_keys or {"cell_day": ("cgi", "stat_time")},
        columns={
            field: CatalogColumn(
                name=field,
                label=field,
                data_type=data_type,
            )
            for field, data_type in columns.items()
        },
    )


@pytest.fixture
def snapshot() -> CatalogSnapshot:
    expansion = make_table(
        "jd_cell_expansion_day",
        {"cgi": "character varying", "stat_time": "date", "deploy_hours": "text"},
    )
    constriction = make_table(
        "jd_cell_constriction_day",
        {
            "cgi": "character varying",
            "stat_time": "date",
            "hours": "integer",
            "around_cgi": "character varying",
        },
        grain_keys={
            "cell_day": ("cgi", "stat_time"),
            "constriction_record": ("cgi", "stat_time", "hours", "around_cgi"),
        },
    )
    around = make_table(
        "jd_cell_around",
        {"cgi": "character varying", "around_cgi": "character varying", "distance": "numeric"},
        default_grain="neighbor",
        grain_keys={"neighbor": ("cgi", "around_cgi")},
    )
    hourly = make_table(
        "jd_cell_detail_hour_nr",
        {"cgi": "character varying", "stat_time": "date", "hours": "integer", "sleep": "numeric"},
        grain_keys={
            "cell_day": ("cgi", "stat_time"),
            "hour": ("cgi", "stat_time", "hours"),
        },
    )
    pre_sleep = make_table(
        "jd_cell_pre_hour_busy",
        {"cgi": "character varying", "stat_time": "date", "prb_hour": "integer", "load": "numeric"},
        grain_keys={
            "cell_day": ("cgi", "stat_time"),
            "hour": ("cgi", "stat_time", "prb_hour"),
        },
    )
    relationships = {
        "expansion_to_constriction": CatalogRelationship(
            name="expansion_to_constriction",
            left_table=expansion.name,
            right_table=constriction.name,
            cardinality="one_to_many",
            keys=(("cgi", "cgi"), ("stat_time", "stat_time")),
            allowed_grains=("cell_day", "constriction_record"),
            detail_grain="constriction_record",
        ),
        "constriction_to_around": CatalogRelationship(
            name="constriction_to_around",
            left_table=constriction.name,
            right_table=around.name,
            cardinality="many_to_one",
            keys=(("cgi", "cgi"), ("around_cgi", "around_cgi")),
            allowed_grains=("cell_day", "constriction_record", "neighbor"),
            detail_grain="neighbor",
        ),
        "expansion_to_hour": CatalogRelationship(
            name="expansion_to_hour",
            left_table=expansion.name,
            right_table=hourly.name,
            cardinality="one_to_many",
            keys=(("cgi", "cgi"), ("stat_time", "stat_time")),
            allowed_grains=("cell_day", "hour"),
            detail_grain="hour",
        ),
        "expansion_to_pre_sleep": CatalogRelationship(
            name="expansion_to_pre_sleep",
            left_table=expansion.name,
            right_table=pre_sleep.name,
            cardinality="one_to_many",
            keys=(("cgi", "cgi"), ("stat_time", "stat_time")),
            allowed_grains=("cell_day", "hour"),
            detail_grain="hour",
        ),
    }
    return CatalogSnapshot(
        version="test",
        tables={table.name: table for table in [expansion, constriction, around, hourly, pre_sleep]},
        relationships=relationships,
    )


def test_validation_resolves_default_grain_and_relationship_tree(snapshot) -> None:
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
    )

    validated = validate_query_plan(plan, snapshot, Settings(_env_file=None))

    assert validated.result_grain == "cell_day"
    assert [edge.relationship.name for edge in validated.path.edges] == [
        "expansion_to_constriction",
        "constriction_to_around",
    ]
    assert validated.path.edges[0].preaggregate is True


def test_validation_rejects_unapproved_table_before_sql(snapshot) -> None:
    plan = BusinessQueryPlan(
        base_table="agent_conversations",
        tables=["agent_conversations"],
        select=[QueryFieldRef(table="agent_conversations", field="messages")],
    )

    with pytest.raises(BusinessQueryValidationError, match="未授权"):
        validate_query_plan(plan, snapshot, Settings(_env_file=None))


def test_validation_rejects_unknown_field_and_text_sum(snapshot) -> None:
    unknown = BusinessQueryPlan(
        base_table="jd_cell_expansion_day",
        tables=["jd_cell_expansion_day"],
        select=[QueryFieldRef(table="jd_cell_expansion_day", field="password")],
    )

    with pytest.raises(BusinessQueryValidationError, match="字段"):
        validate_query_plan(unknown, snapshot, Settings(_env_file=None))


def test_validation_rejects_ambiguous_detail_axis(snapshot) -> None:
    plan = BusinessQueryPlan(
        base_table="jd_cell_expansion_day",
        tables=[
            "jd_cell_expansion_day",
            "jd_cell_detail_hour_nr",
            "jd_cell_pre_hour_busy",
        ],
        relationships=["expansion_to_hour", "expansion_to_pre_sleep"],
        select=[
            QueryFieldRef(table="jd_cell_detail_hour_nr", field="hours"),
            QueryFieldRef(table="jd_cell_pre_hour_busy", field="prb_hour"),
        ],
        result_grain="hour",
    )

    with pytest.raises(BusinessQueryValidationError, match="只能展开一个明细维度"):
        validate_query_plan(plan, snapshot, Settings(_env_file=None))


def test_timestamp_default_range_covers_the_complete_end_day(snapshot) -> None:
    parameter_table = make_table(
        "eng_check_result",
        {
            "cgi": "character varying",
            "check_time": "timestamp without time zone",
            "saving_para_name": "character varying",
        },
        default_grain="parameter_item",
        grain_keys={
            "parameter_item": ("cgi", "check_time", "saving_para_name"),
        },
    ).model_copy(update={"default_date_column": "check_time"})
    snapshot = snapshot.model_copy(update={
        "tables": {**snapshot.tables, parameter_table.name: parameter_table},
    })
    plan = BusinessQueryPlan(
        base_table="eng_check_result",
        tables=["eng_check_result"],
        select=[QueryFieldRef(table="eng_check_result", field="cgi")],
    )

    validated = validate_query_plan(plan, snapshot, Settings(_env_file=None))

    assert [item.operator for item in validated.filters] == ["gte", "lt"]
    assert validated.filters[0].value.hour == 0
    assert validated.filters[1].value.hour == 0
    assert (
        validated.filters[1].value.date()
        - validated.filters[0].value.date()
    ).days == 7


def test_explicit_timestamp_date_range_covers_the_complete_end_day(snapshot) -> None:
    parameter_table = make_table(
        "eng_check_result",
        {
            "cgi": "character varying",
            "check_time": "timestamp without time zone",
            "saving_para_name": "character varying",
        },
        default_grain="parameter_item",
        grain_keys={
            "parameter_item": ("cgi", "check_time", "saving_para_name"),
        },
    ).model_copy(update={"default_date_column": "check_time"})
    snapshot = snapshot.model_copy(update={
        "tables": {**snapshot.tables, parameter_table.name: parameter_table},
    })
    date_field = QueryFieldRef(table="eng_check_result", field="check_time")
    plan = BusinessQueryPlan(
        base_table="eng_check_result",
        tables=["eng_check_result"],
        select=[QueryFieldRef(table="eng_check_result", field="cgi")],
        filters=[QueryFilter(
            field=date_field,
            operator="between",
            value=["2026-08-01", "2026-08-31"],
        )],
    )

    validated = validate_query_plan(plan, snapshot, Settings(_env_file=None))

    assert [item.operator for item in validated.filters] == ["gte", "lt"]
    assert validated.filters[0].value.isoformat() == "2026-08-01T00:00:00"
    assert validated.filters[1].value.isoformat() == "2026-09-01T00:00:00"
