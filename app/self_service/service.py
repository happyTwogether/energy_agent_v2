"""业务数据自助查询的目录、规划、执行与渲染编排。"""

from collections import Counter
from collections.abc import Awaitable, Callable
from functools import lru_cache
from time import perf_counter
from typing import Any

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.self_service import get_business_catalog_store
from app.self_service.catalog import BusinessCatalogStore
from app.self_service.metrics import calculate_metric, get_metric
from app.self_service.planner import (
    BusinessQueryPlanner,
    BusinessQueryPlanningError,
)
from app.self_service.query import (
    BusinessQueryExecutionError,
    BusinessQueryResult,
    BusinessQueryTimeoutError,
    BusinessQueryValidationError,
    execute_business_query,
    validate_query_plan,
)
from app.utils.export_util import export_to_excel

logger = get_logger("business_query_service")
QueryExecutor = Callable[..., Awaitable[BusinessQueryResult]]


class BusinessDataQueryService:
    def __init__(
        self,
        catalog: BusinessCatalogStore | Any | None = None,
        planner: BusinessQueryPlanner | Any | None = None,
        executor: QueryExecutor = execute_business_query,
        settings: Settings | None = None,
    ) -> None:
        self._catalog = catalog or get_business_catalog_store()
        self._planner = planner or BusinessQueryPlanner()
        self._executor = executor
        self._settings = settings or get_settings()

    async def query(
        self,
        db: Any,
        question: str,
        export_excel: bool = False,
    ) -> dict[str, Any]:
        started = perf_counter()
        try:
            snapshot = await self._catalog.get_or_load(db)
            candidates = self._catalog.search(
                question,
                limit=self._settings.self_service_catalog_candidates,
            )
            complete_candidates = getattr(
                self._catalog,
                "complete_candidates",
                None,
            )
            if complete_candidates is not None:
                candidates = complete_candidates(candidates, max_tables=3)
            catalog_ms = _elapsed_ms(started)
            if not candidates:
                return _public_response(
                    "当前授权数据目录中未找到与该问题匹配的业务表。"
                    "当前可查询主题：小区日指标、网络日汇总、工参、节电扩展、"
                    "节电收缩、小时过程、周边小区、参数核查。",
                    clarification_required=True,
                )
            candidate_names = {candidate.table.name for candidate in candidates}
            relationships = [
                relationship
                for relationship in snapshot.relationships.values()
                if relationship.left_table in candidate_names
                and relationship.right_table in candidate_names
            ]
            planner_started = perf_counter()
            plan = await self._planner.plan(question, candidates, relationships)
            planner_ms = _elapsed_ms(planner_started)
            if "limit" not in plan.model_fields_set:
                plan = plan.model_copy(update={
                    "limit": self._settings.self_service_default_limit,
                })
            if plan.clarification:
                return _public_response(
                    plan.clarification,
                    clarification_required=True,
                )
            if not set(plan.tables).issubset(candidate_names):
                raise BusinessQueryValidationError("规划结果包含候选目录之外的表")
            if export_excel:
                plan = plan.model_copy(update={
                    "limit": self._settings.self_service_export_max_rows,
                })
            validation_started = perf_counter()
            validated = validate_query_plan(
                plan,
                snapshot,
                self._settings,
                max_rows=(
                    self._settings.self_service_export_max_rows
                    if export_excel
                    else self._settings.self_service_max_limit
                ),
            )
            validation_ms = _elapsed_ms(validation_started)
            result = await self._executor(
                db,
                validated,
                snapshot,
                self._settings,
            )
            render_started = perf_counter()
            columns = _output_columns(validated, snapshot)
            rows = _output_rows(result.rows, columns, validated)
            quality = _data_quality(result.rows, validated)
            download_url = None
            if export_excel and rows:
                download_url = export_to_excel(
                    rows[:self._settings.self_service_export_max_rows],
                    prefix="business_data_query",
                    column_mapping={},
                )
            render_ms = _elapsed_ms(render_started)
            logger.info(
                "自助查询完成: tables=%s relationships=%s grain=%s columns=%s "
                "rows=%d catalog_ms=%.1f planner_ms=%.1f validation_ms=%.1f "
                "database_ms=%.1f render_ms=%.1f",
                validated.table_names,
                validated.relationship_names,
                validated.result_grain,
                validated.column_names,
                len(result.rows),
                catalog_ms,
                planner_ms,
                validation_ms,
                result.database_ms,
                render_ms,
            )
            return {
                "success": True,
                "rows": rows,
                "columns": [_public_column(item) for item in columns],
                "download_url": download_url,
                "tables": list(result.selected_tables),
                "relationships": list(result.relationship_ids),
                "metrics": list(validated.plan.metrics),
                "metric_definitions": [
                    get_metric(metric_id).model_dump(mode="json")
                    for metric_id in validated.plan.metrics
                ],
                "result_grain": result.result_grain,
                "actual_date_range": _actual_date_range(validated, snapshot),
                "applied_defaults": list(result.applied_defaults),
                "data_quality": quality,
                "row_count": len(rows),
                "database_ms": result.database_ms,
            }
        except BusinessQueryPlanningError as exc:
            return _public_response(str(exc), success=False)
        except BusinessQueryValidationError as exc:
            return _public_response(str(exc), success=False)
        except BusinessQueryTimeoutError:
            return _public_response(
                "查询范围较大导致超时，请缩小日期范围或增加小区、"
                "地市、厂家等筛选条件。",
                success=False,
            )
        except BusinessQueryExecutionError:
            logger.exception("自助查询数据库执行失败")
            return _public_response(
                "业务数据查询暂时失败，请稍后重试。",
                success=False,
            )


def _public_response(
    message: str,
    success: bool = False,
    clarification_required: bool = False,
) -> dict[str, Any]:
    return {
        "success": success,
        "error": None if success else message,
        "clarification_required": clarification_required,
    }


def _output_columns(validated: Any, snapshot: Any) -> list[dict[str, Any]]:
    columns = [
        {
            "id": f"{item.table}.{item.field}",
            "label": snapshot.tables[item.table].columns[item.field].label,
            "type": column_info.data_type,
            "unit": column_info.unit,
            "source_key": f"{item.table}__{item.field}",
            "kind": "field",
        }
        for item in validated.plan.select
        for column_info in [snapshot.tables[item.table].columns[item.field]]
    ]
    columns.extend({
        "id": metric.id,
        "label": metric.label,
        "type": metric.data_type,
        "unit": metric.unit,
        "source_key": metric.id,
        "kind": "metric",
    } for metric in map(get_metric, validated.plan.metrics))
    columns.extend({
        "id": item.alias,
        "label": item.alias,
        "type": "numeric",
        "unit": "",
        "source_key": item.alias,
        "kind": "aggregation",
    } for item in validated.plan.aggregations)
    label_counts = Counter(item["label"] for item in columns)
    for item in columns:
        if label_counts[item["label"]] > 1:
            item["label"] = f"{item['label']}（{item['id']}）"
    return columns


def _public_column(column_info: dict[str, Any]) -> dict[str, Any]:
    return {
        key: column_info[key]
        for key in ("id", "label", "type", "unit")
    }


def _output_rows(
    source_rows: list[dict[str, Any]],
    columns: list[dict[str, Any]],
    validated: Any,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for source_row in source_rows:
        calculated = {
            metric_id: calculate_metric(
                get_metric(metric_id),
                _metric_source_row(source_row, get_metric(metric_id).source_table),
            )
            for metric_id in validated.plan.metrics
        }
        rows.append({
            column_info["label"]: (
                calculated[column_info["id"]]
                if column_info["kind"] == "metric"
                else source_row.get(column_info["source_key"])
            )
            for column_info in columns
        })
    return rows


def _metric_source_row(source_row: dict[str, Any], table_name: str) -> dict[str, Any]:
    prefix = f"{table_name}__"
    return {
        key.removeprefix(prefix): value
        for key, value in source_row.items()
        if key.startswith(prefix)
    }


def _data_quality(
    source_rows: list[dict[str, Any]],
    validated: Any,
) -> dict[str, Any]:
    required_keys = {
        f"{item.table}__{item.field}"
        for item in validated.plan.select
    }
    for metric_id in validated.plan.metrics:
        metric = get_metric(metric_id)
        required_keys.update(
            f"{metric.source_table}__{field_name}"
            for field_name in metric.source_fields
        )
    missing = sorted({
        key.replace("__", ".", 1)
        for row in source_rows
        for key in required_keys
        if _is_missing_value(row.get(key))
    })
    return {"complete": not missing, "missing_fields": missing}


def _is_missing_value(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, (list, tuple)):
        return not value or any(item is None for item in value)
    return False


def _actual_date_range(validated: Any, snapshot: Any) -> dict[str, Any]:
    table_info = snapshot.tables[validated.plan.base_table]
    date_column = table_info.default_date_column
    if not date_column:
        return {}
    values: dict[str, Any] = {}
    for item in validated.filters:
        if item.field.table != table_info.name or item.field.field != date_column:
            continue
        if item.operator == "between":
            values.update({"start": item.value[0], "end": item.value[1]})
        elif item.operator in {"gt", "gte"}:
            values["start"] = item.value
        elif item.operator in {"lt", "lte"}:
            values["end"] = item.value
        elif item.operator == "eq":
            values.update({"start": item.value, "end": item.value})
    return values


def _elapsed_ms(started: float) -> float:
    return (perf_counter() - started) * 1000


@lru_cache(maxsize=1)
def get_business_data_query_service() -> BusinessDataQueryService:
    return BusinessDataQueryService()
