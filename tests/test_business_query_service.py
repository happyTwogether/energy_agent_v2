"""组合查询服务和中文结果渲染测试。"""

from datetime import date
from decimal import Decimal

import pytest

from app.core.config import Settings
from app.self_service.catalog import BusinessCatalogStore
from app.self_service.models import (
    BusinessQueryPlan,
    CatalogCandidate,
    CatalogColumn,
    QueryFieldRef,
)
from app.self_service.query import BusinessQueryResult, validate_query_plan
from app.self_service.service import (
    BusinessDataQueryService,
    _data_quality,
    _output_columns,
)
from tests.test_business_relationships import snapshot as snapshot_fixture
from tests.test_business_catalog import FakeSession
from tests.test_business_query import get_nr_summary_snapshot


def get_snapshot():
    return snapshot_fixture.__wrapped__()


def test_duplicate_chinese_labels_keep_distinct_output_keys() -> None:
    snapshot = get_nr_summary_snapshot()
    table = snapshot.tables["nr_report_day_collect"]
    columns = {
        **table.columns,
        "deep_sleep_hour": CatalogColumn(
            name="deep_sleep_hour",
            label="深度休眠时长",
            data_type="numeric",
        ),
        "deepsleep_hour": CatalogColumn(
            name="deepsleep_hour",
            label="深度休眠时长",
            data_type="numeric",
        ),
    }
    table = table.model_copy(update={"columns": columns})
    snapshot = snapshot.model_copy(update={
        "tables": {**snapshot.tables, table.name: table},
    })
    plan = BusinessQueryPlan(
        base_table=table.name,
        tables=[table.name],
        select=[
            QueryFieldRef(table=table.name, field="deep_sleep_hour"),
            QueryFieldRef(table=table.name, field="deepsleep_hour"),
        ],
    )
    validated = validate_query_plan(plan, snapshot, Settings(_env_file=None))

    labels = [item["label"] for item in _output_columns(validated, snapshot)]

    assert labels == [
        "深度休眠时长（nr_report_day_collect.deep_sleep_hour）",
        "深度休眠时长（nr_report_day_collect.deepsleep_hour）",
    ]


def test_null_only_aggregated_child_is_reported_as_missing() -> None:
    snapshot = get_snapshot()
    plan = BusinessQueryPlan(
        base_table="jd_cell_expansion_day",
        tables=["jd_cell_expansion_day", "jd_cell_constriction_day"],
        relationships=["expansion_to_constriction"],
        select=[QueryFieldRef(
            table="jd_cell_constriction_day",
            field="hours",
        )],
    )
    validated = validate_query_plan(plan, snapshot, Settings(_env_file=None))

    quality = _data_quality(
        [{"jd_cell_constriction_day__hours": [None]}],
        validated,
    )

    assert quality == {
        "complete": False,
        "missing_fields": ["jd_cell_constriction_day.hours"],
    }


class FakeCatalog:
    def __init__(self, snapshot) -> None:
        self._snapshot = snapshot

    async def get_or_load(self, db):
        return self._snapshot

    def search(self, question: str, limit: int):
        return [
            CatalogCandidate(table=self._snapshot.tables[name], score=0.9)
            for name in [
                "jd_cell_expansion_day",
                "jd_cell_constriction_day",
                "jd_cell_around",
            ]
        ]


class EmptyCatalog:
    def __init__(self, snapshot) -> None:
        self._snapshot = snapshot

    async def get_or_load(self, db):
        return self._snapshot

    def search(self, question: str, limit: int):
        return []


class FakePlanner:
    def __init__(self) -> None:
        self.calls = 0

    async def plan(self, question, candidates, relationships):
        self.calls += 1
        return BusinessQueryPlan(
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


class FakeExecutor:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, db, validated, snapshot, settings):
        self.calls += 1
        return BusinessQueryResult(
            rows=[{
                "jd_cell_expansion_day__cgi": "460-00-2539193-71",
                "jd_cell_constriction_day__hours": [22, 23],
                "jd_cell_around__distance": [Decimal("125.5")],
            }],
            column_order=(
                "jd_cell_expansion_day__cgi",
                "jd_cell_constriction_day__hours",
                "jd_cell_around__distance",
            ),
            selected_tables=validated.table_names,
            relationship_ids=validated.relationship_names,
            result_grain=validated.result_grain,
            applied_defaults=validated.applied_defaults,
            database_ms=12.5,
        )


@pytest.mark.asyncio
async def test_service_plans_executes_once_and_returns_structured_three_table_data() -> None:
    snapshot = get_snapshot()
    planner = FakePlanner()
    executor = FakeExecutor()
    service = BusinessDataQueryService(
        catalog=FakeCatalog(snapshot),
        planner=planner,
        executor=executor,
        settings=Settings(_env_file=None),
    )

    payload = await service.query(
        db=object(),
        question="查询扩展时段、收缩时段和周边小区距离",
    )

    assert planner.calls == 1
    assert executor.calls == 1
    assert payload["success"] is True
    assert payload["result_grain"] == "cell_day"
    assert payload["tables"] == [
        "jd_cell_expansion_day",
        "jd_cell_constriction_day",
        "jd_cell_around",
    ]
    assert "report_content" not in payload
    assert payload["rows"] == [{
        "cgi": "460-00-2539193-71",
        "hours": [22, 23],
        "distance": [Decimal("125.5")],
    }]
    assert [column["id"] for column in payload["columns"]] == [
        "jd_cell_expansion_day.cgi",
        "jd_cell_constriction_day.hours",
        "jd_cell_around.distance",
    ]
    assert all(
        set(column) == {"id", "label", "type", "unit"}
        for column in payload["columns"]
    )
    assert payload["database_ms"] == 12.5


@pytest.mark.asyncio
async def test_real_catalog_exposes_all_three_intents_to_planner() -> None:
    class CapturingPlanner:
        candidate_names: list[str] = []
        relationship_names: list[str] = []

        async def plan(self, question, candidates, relationships):
            self.candidate_names = [item.table.name for item in candidates]
            self.relationship_names = [item.name for item in relationships]
            return BusinessQueryPlan(
                base_table=self.candidate_names[0],
                tables=[self.candidate_names[0]],
                clarification="test-only",
            )

    planner = CapturingPlanner()
    service = BusinessDataQueryService(
        catalog=BusinessCatalogStore(settings=Settings(_env_file=None)),
        planner=planner,
        settings=Settings(_env_file=None),
    )

    await service.query(
        db=FakeSession(),
        question="查询扩展时段、需收缩时段和周边小区距离",
    )

    assert {
        "jd_cell_expansion_day",
        "jd_cell_constriction_day",
        "jd_cell_around",
    }.issubset(set(planner.candidate_names))
    assert {
        "expansion_to_constriction",
        "constriction_to_around",
    }.issubset(set(planner.relationship_names))


@pytest.mark.asyncio
async def test_service_returns_success_semantics_for_empty_result() -> None:
    snapshot = get_snapshot()
    planner = FakePlanner()

    async def empty_executor(db, validated, snapshot, settings):
        return BusinessQueryResult(
            rows=[],
            column_order=(),
            selected_tables=validated.table_names,
            relationship_ids=validated.relationship_names,
            result_grain=validated.result_grain,
            applied_defaults=validated.applied_defaults,
            database_ms=1.0,
        )

    service = BusinessDataQueryService(
        catalog=FakeCatalog(snapshot),
        planner=planner,
        executor=empty_executor,
        settings=Settings(_env_file=None),
    )

    payload = await service.query(db=object(), question="查询扩展时段")

    assert payload["success"] is True
    assert payload["rows"] == []
    assert payload["row_count"] == 0
    assert payload["data_quality"] == {
        "complete": True,
        "missing_fields": [],
    }
    assert "report_content" not in payload


@pytest.mark.asyncio
async def test_no_catalog_match_requests_clarification_and_mentions_rru() -> None:
    service = BusinessDataQueryService(
        catalog=EmptyCatalog(get_snapshot()),
        planner=FakePlanner(),
        settings=Settings(_env_file=None),
    )

    payload = await service.query(db=object(), question="查询未知主题")

    assert payload["success"] is False
    assert payload["clarification_required"] is True
    assert "RRU" in payload["error"]


@pytest.mark.asyncio
async def test_service_applies_configured_default_limit() -> None:
    snapshot = get_snapshot()
    seen_limits: list[int] = []

    async def recording_executor(db, validated, snapshot, settings):
        seen_limits.append(validated.limit)
        return BusinessQueryResult(
            rows=[],
            column_order=(),
            selected_tables=validated.table_names,
            relationship_ids=validated.relationship_names,
            result_grain=validated.result_grain,
            applied_defaults=validated.applied_defaults,
            database_ms=1.0,
        )

    service = BusinessDataQueryService(
        catalog=FakeCatalog(snapshot),
        planner=FakePlanner(),
        executor=recording_executor,
        settings=Settings(_env_file=None, self_service_default_limit=17),
    )

    await service.query(db=object(), question="查询扩展时段")

    assert seen_limits == [17]


@pytest.mark.asyncio
async def test_excel_query_uses_export_row_limit() -> None:
    snapshot = get_snapshot()
    seen_limits: list[int] = []

    async def recording_executor(db, validated, snapshot, settings):
        seen_limits.append(validated.limit)
        return BusinessQueryResult(
            rows=[],
            column_order=(),
            selected_tables=validated.table_names,
            relationship_ids=validated.relationship_names,
            result_grain=validated.result_grain,
            applied_defaults=validated.applied_defaults,
            database_ms=1.0,
        )

    service = BusinessDataQueryService(
        catalog=FakeCatalog(snapshot),
        planner=FakePlanner(),
        executor=recording_executor,
        settings=Settings(_env_file=None),
    )

    await service.query(db=object(), question="导出扩展收缩数据", export_excel=True)

    assert seen_limits == [10_000]


@pytest.mark.asyncio
async def test_service_calculates_registered_metric_and_hides_source_fields() -> None:
    snapshot = get_nr_summary_snapshot()

    class SummaryCatalog:
        async def get_or_load(self, db):
            return snapshot

        def search(self, question: str, limit: int):
            return [CatalogCandidate(
                table=snapshot.tables["nr_report_day_collect"],
                score=1.0,
                matched_metrics=("nr_high_power_ratio",),
            )]

    class SummaryPlanner:
        async def plan(self, question, candidates, relationships):
            return BusinessQueryPlan(
                base_table="nr_report_day_collect",
                tables=["nr_report_day_collect"],
                select=[QueryFieldRef(
                    table="nr_report_day_collect",
                    field="data_date",
                )],
                metrics=["nr_high_power_ratio"],
            )

    async def summary_executor(db, validated, snapshot, settings):
        return BusinessQueryResult(
            rows=[{
                "nr_report_day_collect__data_date": date(2026, 8, 24),
                "nr_report_day_collect__thirtytwo_channel_total": 20,
                "nr_report_day_collect__sixtyfour_channel_total": 10,
                "nr_report_day_collect__all_cell_total": 120,
            }],
            column_order=tuple(
                f"{item.table}__{item.field}"
                for item in validated.query_fields
            ),
            selected_tables=validated.table_names,
            relationship_ids=validated.relationship_names,
            result_grain=validated.result_grain,
            applied_defaults=validated.applied_defaults,
            database_ms=3.5,
        )

    service = BusinessDataQueryService(
        catalog=SummaryCatalog(),
        planner=SummaryPlanner(),
        executor=summary_executor,
        settings=Settings(_env_file=None),
    )

    payload = await service.query(db=object(), question="查询5G高耗电小区占比")

    assert payload["rows"] == [{
        "data_date": date(2026, 8, 24),
        "5G高耗电小区占比": 25.0,
    }]
    assert payload["metrics"] == ["nr_high_power_ratio"]
    assert payload["metric_definitions"][0]["source_fields"] == [
        "thirtytwo_channel_total",
        "sixtyfour_channel_total",
        "all_cell_total",
    ]
    assert all("channel_total" not in key for key in payload["rows"][0])
