"""受控业务查询计划校验、SQLAlchemy 构造与只读执行。"""

from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal
import re
from time import perf_counter
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import and_, column, distinct, func, select, table, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.sql import Select

from app.core.config import Settings
from app.self_service.metrics import METRIC_REGISTRY
from app.self_service.models import (
    BusinessQueryPlan,
    CatalogColumn,
    CatalogRelationship,
    CatalogSnapshot,
    QueryAggregation,
    QueryFieldRef,
    QueryFilter,
)
from app.self_service.relationships import (
    ResolvedRelationshipEdge,
    ResolvedRelationshipPath,
    orient_relationships,
)


class BusinessQueryValidationError(ValueError):
    """模型计划未通过本地授权和类型校验。"""


class BusinessQueryTimeoutError(RuntimeError):
    """数据库在自助查询超时限制内未完成。"""


class BusinessQueryExecutionError(RuntimeError):
    """自助查询执行失败且公开信息已脱敏。"""


@dataclass(frozen=True, slots=True)
class ValidatedBusinessQuery:
    plan: BusinessQueryPlan
    path: ResolvedRelationshipPath
    result_grain: str
    filters: tuple[QueryFilter, ...]
    limit: int
    applied_defaults: tuple[str, ...]
    query_fields: tuple[QueryFieldRef, ...]

    @property
    def table_names(self) -> tuple[str, ...]:
        return tuple(self.plan.tables)

    @property
    def relationship_names(self) -> tuple[str, ...]:
        return tuple(self.plan.relationships)

    @property
    def column_names(self) -> tuple[str, ...]:
        return tuple(f"{ref.table}.{ref.field}" for ref in self.query_fields)


@dataclass(frozen=True, slots=True)
class BusinessQueryResult:
    rows: list[dict[str, Any]]
    column_order: tuple[str, ...]
    selected_tables: tuple[str, ...]
    relationship_ids: tuple[str, ...]
    result_grain: str
    applied_defaults: tuple[str, ...]
    database_ms: float


def validate_query_plan(
    plan: BusinessQueryPlan,
    snapshot: CatalogSnapshot,
    settings: Settings,
    max_rows: int | None = None,
) -> ValidatedBusinessQuery:
    """在 SQL 构造前验证授权、关系、字段、类型和资源边界。"""
    missing_tables = [name for name in plan.tables if name not in snapshot.tables]
    if missing_tables:
        raise BusinessQueryValidationError(
            f"查询包含未授权业务表：{'、'.join(missing_tables)}",
        )
    base_table = snapshot.tables[plan.base_table]
    if len(plan.tables) > 1 and base_table.default_date_column is None:
        raise BusinessQueryValidationError(
            "多表查询主表必须包含日期字段，以限制查询范围",
        )
    relationships = _resolve_relationships(plan, snapshot)
    result_grain = plan.result_grain or snapshot.tables[plan.base_table].default_grain
    if result_grain not in snapshot.tables[plan.base_table].grain_keys:
        allowed_by_path = any(
            result_grain in relationship.allowed_grains
            for relationship in relationships
        )
        if not allowed_by_path:
            raise BusinessQueryValidationError("结果粒度未被主表或关系授权")
    try:
        path = orient_relationships(plan.base_table, relationships, result_grain)
    except ValueError as exc:
        raise BusinessQueryValidationError(str(exc)) from exc
    _validate_detail_axis(
        path,
        result_grain,
        result_grain in snapshot.tables[plan.base_table].grain_keys,
    )
    _validate_relationship_grains(
        path,
        result_grain,
        snapshot.tables[plan.base_table].default_grain,
    )
    _validate_references(plan, snapshot)
    query_fields = _validated_query_fields(plan, snapshot, result_grain)
    _validate_multi_table_aggregation(plan, path)
    filters, defaults = _validated_filters(plan, snapshot, settings)
    limit_cap = max_rows or settings.self_service_max_limit
    return ValidatedBusinessQuery(
        plan=plan,
        path=path,
        result_grain=result_grain,
        filters=tuple(filters),
        limit=min(plan.limit, limit_cap),
        applied_defaults=tuple(defaults),
        query_fields=query_fields,
    )


def _validated_query_fields(
    plan: BusinessQueryPlan,
    snapshot: CatalogSnapshot,
    result_grain: str,
) -> tuple[QueryFieldRef, ...]:
    if plan.metrics and (plan.group_by or plan.aggregations):
        raise BusinessQueryValidationError(
            "计算指标第一阶段不能与分组或聚合同时使用",
        )
    query_fields = list(plan.select)
    seen = {(item.table, item.field) for item in query_fields}
    for metric_id in plan.metrics:
        metric = METRIC_REGISTRY.get(metric_id)
        if metric is None:
            raise BusinessQueryValidationError(f"查询包含未授权指标：{metric_id}")
        if metric.source_table not in plan.tables:
            raise BusinessQueryValidationError(
                f"指标 {metric.label} 需要查询表 {metric.source_table}",
            )
        if result_grain != metric.grain:
            raise BusinessQueryValidationError(
                f"指标 {metric.label} 仅支持 {metric.grain} 粒度",
            )
        table_info = snapshot.tables[metric.source_table]
        missing = set(metric.source_fields) - set(table_info.columns)
        if missing:
            raise BusinessQueryValidationError(
                f"指标 {metric.label} 缺少来源字段：{'、'.join(sorted(missing))}",
            )
        for field_name in metric.source_fields:
            key = (metric.source_table, field_name)
            if key not in seen:
                query_fields.append(QueryFieldRef(
                    table=metric.source_table,
                    field=field_name,
                ))
                seen.add(key)
    for order in plan.order_by:
        if order.field is None:
            continue
        key = (order.field.table, order.field.field)
        if key not in seen:
            query_fields.append(order.field)
            seen.add(key)
    return tuple(query_fields)


def _resolve_relationships(
    plan: BusinessQueryPlan,
    snapshot: CatalogSnapshot,
) -> list[CatalogRelationship]:
    resolved: list[CatalogRelationship] = []
    for name in plan.relationships:
        relationship = snapshot.relationships.get(name)
        if relationship is None:
            raise BusinessQueryValidationError(f"查询包含未授权关系：{name}")
        if {relationship.left_table, relationship.right_table} - set(plan.tables):
            raise BusinessQueryValidationError(f"关系 {name} 的端点不属于查询表")
        resolved.append(relationship)
    return resolved


def _validate_detail_axis(
    path: ResolvedRelationshipPath,
    result_grain: str,
    base_supports_grain: bool,
) -> None:
    detail_grains = {
        "constriction_record", "hour", "neighbor", "parameter_item", "rru_item",
    }
    if result_grain not in detail_grains or base_supports_grain:
        return
    axes = [
        edge
        for edge in path.edges
        if edge.relationship.detail_grain == result_grain
    ]
    if len(axes) != 1:
        raise BusinessQueryValidationError(
            "一次查询只能展开一个明细维度，请选择逐小时、逐邻区、"
            "逐收缩记录或逐参数项。",
        )


def _validate_relationship_grains(
    path: ResolvedRelationshipPath,
    result_grain: str,
    base_grain: str,
) -> None:
    detail_grains = {
        "constriction_record", "hour", "neighbor", "parameter_item", "rru_item",
    }
    for edge in path.edges:
        relationship = edge.relationship
        effective_grain = (
            base_grain
            if result_grain in detail_grains
            and relationship.detail_grain != result_grain
            else result_grain
        )
        if effective_grain not in relationship.allowed_grains:
            raise BusinessQueryValidationError(
                f"关系 {relationship.name} 不允许结果粒度 {result_grain}",
            )


def _validate_references(
    plan: BusinessQueryPlan,
    snapshot: CatalogSnapshot,
) -> None:
    references = list(plan.select) + list(plan.group_by)
    references.extend(item.field for item in plan.filters)
    references.extend(item.field for item in plan.aggregations if item.field)
    references.extend(item.field for item in plan.order_by if item.field)
    for reference in references:
        table_info = snapshot.tables[reference.table]
        if reference.field not in table_info.columns:
            raise BusinessQueryValidationError(
                f"字段未授权或不存在：{table_info.label}.{reference.field}",
            )
    for item in plan.filters:
        column_info = snapshot.tables[item.field.table].columns[item.field.field]
        if not _operator_allowed(column_info, item.operator):
            raise BusinessQueryValidationError(
                f"字段 {column_info.label} 不支持操作符 {item.operator}",
            )
    for aggregation in plan.aggregations:
        _validate_aggregation(aggregation, snapshot)
        if not re.fullmatch(r"[\w\u4e00-\u9fff]{1,64}", aggregation.alias):
            raise BusinessQueryValidationError(
                "聚合别名只能包含中英文、数字和下划线",
            )
    aliases = {item.alias for item in plan.aggregations}
    if len(aliases) != len(plan.aggregations):
        raise BusinessQueryValidationError("聚合别名不能重复")
    has_unknown_alias = any(
        item.aggregation_alias not in aliases
        for item in plan.order_by
        if item.aggregation_alias
    )
    if has_unknown_alias:
        raise BusinessQueryValidationError("排序使用了未知聚合别名")


def _validate_multi_table_aggregation(
    plan: BusinessQueryPlan,
    path: ResolvedRelationshipPath,
) -> None:
    if not plan.aggregations:
        return
    grouped = {(item.table, item.field) for item in plan.group_by}
    selected = {(item.table, item.field) for item in plan.select}
    if not selected.issubset(grouped):
        raise BusinessQueryValidationError(
            "聚合查询的展示字段必须同时出现在分组字段中",
        )
    ordered_fields = {
        (item.field.table, item.field.field)
        for item in plan.order_by
        if item.field is not None
    }
    if not ordered_fields.issubset(grouped):
        raise BusinessQueryValidationError(
            "聚合查询的物理排序字段必须同时出现在分组字段中",
        )
    preaggregated_tables: set[str] = set()
    for edge in path.edges:
        if edge.preaggregate or edge.parent_table in preaggregated_tables:
            preaggregated_tables.add(edge.child_table)
    for aggregation in plan.aggregations:
        if aggregation.field and aggregation.field.table in preaggregated_tables:
            raise BusinessQueryValidationError(
                "一对多子表聚合前必须先展开该明细，或改为主表粒度查询。",
            )
    if any(item.table in preaggregated_tables for item in plan.group_by):
        raise BusinessQueryValidationError("一对多子表字段不能直接作为汇总分组")


def _operator_allowed(column_info: CatalogColumn, operator: str) -> bool:
    data_type = column_info.data_type.lower()
    common = {"eq", "ne", "is_null", "is_not_null"}
    comparisons = {"gt", "gte", "lt", "lte", "in", "between"}
    if _is_text(data_type):
        return operator in common | comparisons | {"contains", "starts_with"}
    if _is_numeric(data_type) or _is_date(data_type):
        return operator in common | comparisons
    return operator in common


def _validate_aggregation(
    aggregation: QueryAggregation,
    snapshot: CatalogSnapshot,
) -> None:
    if aggregation.function == "count" and aggregation.field is None:
        return
    if aggregation.field is None:
        raise BusinessQueryValidationError("该聚合函数需要字段")
    column_info = snapshot.tables[aggregation.field.table].columns[aggregation.field.field]
    if aggregation.function in {"sum", "avg"} and not _is_numeric(column_info.data_type):
        raise BusinessQueryValidationError(
            f"文本字段 {column_info.label} 不能求和或平均",
        )
    unsupported = (
        aggregation.function not in {"min", "max", "count"}
        and not _is_numeric(column_info.data_type)
    )
    if unsupported:
        raise BusinessQueryValidationError(f"字段 {column_info.label} 不支持该聚合")


def _validated_filters(
    plan: BusinessQueryPlan,
    snapshot: CatalogSnapshot,
    settings: Settings,
) -> tuple[list[QueryFilter], list[str]]:
    filters: list[QueryFilter] = []
    for item in plan.filters:
        converted = replace_filter_value(item, snapshot)
        column_info = snapshot.tables[item.field.table].columns[item.field.field]
        if (
            _is_timestamp(column_info.data_type)
            and item.operator == "between"
            and _is_date_only_value(item.value[1])
        ):
            filters.extend([
                converted.model_copy(update={
                    "operator": "gte",
                    "value": converted.value[0],
                }),
                converted.model_copy(update={
                    "operator": "lt",
                    "value": converted.value[1] + timedelta(days=1),
                }),
            ])
        else:
            filters.append(converted)
    defaults: list[str] = []
    base_table = snapshot.tables[plan.base_table]
    date_column = base_table.default_date_column
    has_base_date_filter = any(
        item.field.table == plan.base_table and item.field.field == date_column
        for item in filters
    )
    if date_column and not has_base_date_filter:
        end = datetime.now(ZoneInfo("Asia/Shanghai")).date()
        start = end - timedelta(days=settings.self_service_default_days - 1)
        field = QueryFieldRef(table=plan.base_table, field=date_column)
        column_type = base_table.columns[date_column].data_type
        if _is_timestamp(column_type):
            filters.extend([
                QueryFilter(
                    field=field,
                    operator="gte",
                    value=datetime.combine(start, time.min),
                ),
                QueryFilter(
                    field=field,
                    operator="lt",
                    value=datetime.combine(end + timedelta(days=1), time.min),
                ),
            ])
        else:
            filters.append(QueryFilter(
                field=field,
                operator="between",
                value=[start, end],
            ))
        defaults.append(f"最近{settings.self_service_default_days}天")
    _validate_date_ranges(filters, snapshot, settings.self_service_max_days)
    return filters, defaults


def _is_date_only_value(value: Any) -> bool:
    if isinstance(value, datetime):
        return False
    if isinstance(value, date):
        return True
    return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(value).strip()))


def replace_filter_value(
    item: QueryFilter,
    snapshot: CatalogSnapshot,
) -> QueryFilter:
    column_info = snapshot.tables[item.field.table].columns[item.field.field]
    try:
        value = _convert_filter_value(column_info.data_type, item.operator, item.value)
    except (TypeError, ValueError) as exc:
        raise BusinessQueryValidationError(
            f"字段 {column_info.label} 的筛选值类型不正确",
        ) from exc
    return item.model_copy(update={"value": value})


def _convert_filter_value(data_type: str, operator: str, value: Any) -> Any:
    if operator in {"is_null", "is_not_null"}:
        return None
    converter = _value_converter(data_type)
    if operator in {"in", "between"}:
        if not isinstance(value, (list, tuple)):
            raise TypeError("范围筛选值必须为列表")
        if operator == "between" and len(value) != 2:
            raise ValueError("between 必须包含两个值")
        return [converter(item) for item in value]
    return converter(value)


def _value_converter(data_type: str):
    normalized = data_type.lower()
    if _is_numeric(normalized):
        return Decimal
    if _is_timestamp(normalized):
        return _to_datetime
    if _is_date(normalized):
        return _to_date
    if normalized == "boolean":
        return _to_boolean
    return str


def _to_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _to_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, time.min)
    return datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))


def _to_boolean(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"true", "1", "yes", "是"}:
        return True
    if normalized in {"false", "0", "no", "否"}:
        return False
    raise ValueError("无效布尔值")


def _validate_date_ranges(
    filters: list[QueryFilter],
    snapshot: CatalogSnapshot,
    max_days: int,
) -> None:
    grouped: dict[tuple[str, str], list[QueryFilter]] = {}
    for item in filters:
        column_info = snapshot.tables[item.field.table].columns[item.field.field]
        if not _is_date(column_info.data_type):
            continue
        key = (item.field.table, item.field.field)
        grouped.setdefault(key, []).append(item)
    for date_filters in grouped.values():
        lower: list[tuple[Any, str]] = []
        upper: list[tuple[Any, str]] = []
        for item in date_filters:
            if item.operator == "between":
                lower.append((item.value[0], "gte"))
                upper.append((item.value[1], "lte"))
            elif item.operator == "eq":
                lower.append((item.value, "gte"))
                upper.append((item.value, "lte"))
            elif item.operator == "in":
                lower.append((min(item.value), "gte"))
                upper.append((max(item.value), "lte"))
            elif item.operator in {"gt", "gte"}:
                lower.append((item.value, item.operator))
            elif item.operator in {"lt", "lte"}:
                upper.append((item.value, item.operator))
        if not lower or not upper:
            raise BusinessQueryValidationError(
                "日期范围必须同时提供开始和结束",
            )
        start, _ = max(lower, key=lambda item: item[0])
        end, end_operator = min(upper, key=lambda item: item[0])
        start_date = start.date() if isinstance(start, datetime) else start
        end_date = end.date() if isinstance(end, datetime) else end
        span_days = (end_date - start_date).days
        allowed_span = max_days if end_operator == "lt" else max_days - 1
        if end < start or span_days > allowed_span:
            raise BusinessQueryValidationError(f"日期范围最多允许 {max_days} 天")


def build_business_statement(
    validated: ValidatedBusinessQuery,
    snapshot: CatalogSnapshot,
) -> Select:
    """只使用目录标识符和绑定值构造最终业务 SELECT。"""
    sources = _source_tables(validated.plan, snapshot)
    base = sources[validated.plan.base_table]
    base_filters = _filter_expressions(validated.filters, sources)
    base_where = [
        expression
        for query_filter, expression in base_filters
        if query_filter.field.table == validated.plan.base_table
    ]
    if not validated.path.edges:
        return _single_table_statement(validated, snapshot, sources, base_where)

    base_filtered = select(*base.c).where(*base_where).cte("base_filtered")
    runtime_sources = dict(sources)
    runtime_sources[validated.plan.base_table] = base_filtered
    return _controlled_multi_table_statement(validated, snapshot, runtime_sources)


def _single_table_statement(
    validated: ValidatedBusinessQuery,
    snapshot: CatalogSnapshot,
    sources: dict[str, Any],
    where_expressions: list[Any],
) -> Select:
    source = sources[validated.plan.base_table]
    table_info = snapshot.tables[validated.plan.base_table]
    grain_keys = table_info.grain_keys[validated.result_grain]
    storage_is_finer = any(
        len(keys) > len(grain_keys)
        for keys in table_info.grain_keys.values()
    )
    selected = []
    for item in validated.query_fields:
        expression = source.c[item.field]
        if storage_is_finer and not validated.plan.aggregations and item.field not in grain_keys:
            expression = _array_agg_distinct(expression)
        selected.append(expression.label(_column_key(item)))
    selected.extend(_aggregation_expression(item, sources) for item in validated.plan.aggregations)
    statement = select(*selected).select_from(source).where(*where_expressions)
    if storage_is_finer and not validated.plan.aggregations:
        statement = statement.group_by(*[source.c[key] for key in grain_keys])
    elif validated.plan.group_by:
        statement = statement.group_by(*[
            sources[item.table].c[item.field]
            for item in validated.plan.group_by
        ])
    return _apply_order_and_limit(statement, validated, sources)


def _controlled_multi_table_statement(
    validated: ValidatedBusinessQuery,
    snapshot: CatalogSnapshot,
    sources: dict[str, Any],
) -> Select:
    base_name = validated.plan.base_table
    base = sources[base_name]
    base_table_info = snapshot.tables[base_name]
    correlation_grain = (
        validated.result_grain
        if validated.result_grain in base_table_info.grain_keys
        else base_table_info.default_grain
    )
    base_keys = base_table_info.grain_keys[correlation_grain]
    base_storage_is_finer = (
        validated.result_grain in base_table_info.grain_keys
        and any(
            len(keys) > len(base_keys)
            for keys in base_table_info.grain_keys.values()
        )
    )
    result_owner = _result_grain_owner(validated, snapshot)
    external_detail = result_owner is not None and result_owner != base_name
    owner_keys = (
        snapshot.tables[result_owner].grain_keys[validated.result_grain]
        if external_detail and result_owner
        else ()
    )
    branches, direct_edges = _partition_edges(validated.path.edges, base_name)
    final_from = base
    direct_tables = {base_name}
    for edge in direct_edges:
        final_from = final_from.outerjoin(
            sources[edge.child_table],
            _join_condition(edge.relationship, sources),
        )
        direct_tables.add(edge.child_table)
    expressions = {
        _column_key(item): sources[item.table].c[item.field].label(_column_key(item))
        for item in validated.query_fields
        if item.table in direct_tables
    }
    filters = _filter_expressions(validated.filters, sources)
    direct_filters = [
        expression
        for item, expression in filters
        if item.field.table in direct_tables and item.field.table != base_name
    ]
    for index, edges in enumerate(branches, start=1):
        descendants = {edge.child_table for edge in edges}
        branch_from = base
        for edge in edges:
            branch_from = branch_from.outerjoin(
                sources[edge.child_table],
                _join_condition(edge.relationship, sources),
            )
        branch_columns = [base.c[key].label(key) for key in base_keys]
        for item in validated.query_fields:
            if item.table in descendants:
                branch_columns.append(
                    _array_agg_distinct(sources[item.table].c[item.field]).label(
                        _column_key(item),
                    ),
                )
        branch_filters = [
            expression
            for item, expression in filters
            if item.field.table in descendants
        ]
        branch = (
            select(*branch_columns)
            .select_from(branch_from)
            .where(*branch_filters)
            .group_by(*[base.c[key] for key in base_keys])
            .cte(f"branch_{index}")
        )
        branch_condition = and_(*[
            base.c[key] == branch.c[key]
            for key in base_keys
        ])
        final_from = (
            final_from.join(branch, branch_condition)
            if branch_filters
            else final_from.outerjoin(branch, branch_condition)
        )
        expressions.update({
            _column_key(item): branch.c[_column_key(item)]
            for item in validated.query_fields
            if item.table in descendants
        })
    selected: list[Any] = []
    extra_group_by: list[Any] = []
    for item in validated.query_fields:
        key = _column_key(item)
        expression = expressions[key]
        if external_detail and not validated.plan.aggregations:
            if item.table == base_name and item.field in base_keys:
                selected.append(expression)
            elif item.table == result_owner and item.field in owner_keys:
                selected.append(expression)
            elif item.table == result_owner:
                selected.append(
                    func.max(sources[item.table].c[item.field]).label(key),
                )
            elif item.table in direct_tables:
                selected.append(
                    _array_agg_distinct(sources[item.table].c[item.field]).label(key),
                )
            else:
                selected.append(expression)
                extra_group_by.append(expression)
        elif base_storage_is_finer and not validated.plan.aggregations:
            if item.table == base_name and item.field in base_keys:
                selected.append(expression)
            elif item.table in direct_tables:
                selected.append(
                    _array_agg_distinct(sources[item.table].c[item.field]).label(key),
                )
            else:
                selected.append(expression)
                extra_group_by.append(expression)
        else:
            selected.append(expression)
    selected.extend(
        _aggregation_expression(item, sources)
        for item in validated.plan.aggregations
    )
    statement = select(*selected).select_from(final_from).where(*direct_filters)
    if external_detail and not validated.plan.aggregations and result_owner:
        statement = statement.group_by(
            *[base.c[key] for key in base_keys],
            *[sources[result_owner].c[key] for key in owner_keys],
            *extra_group_by,
        )
    elif base_storage_is_finer and not validated.plan.aggregations:
        statement = statement.group_by(
            *[base.c[key] for key in base_keys],
            *extra_group_by,
        )
    elif validated.plan.group_by:
        statement = statement.group_by(*[
            sources[item.table].c[item.field]
            for item in validated.plan.group_by
        ])
    return _apply_order_and_limit(statement, validated, sources)


def _result_grain_owner(
    validated: ValidatedBusinessQuery,
    snapshot: CatalogSnapshot,
) -> str | None:
    owners = [
        table_name
        for table_name in validated.plan.tables
        if validated.result_grain in snapshot.tables[table_name].grain_keys
    ]
    if not owners:
        return None
    if validated.plan.base_table in owners:
        return validated.plan.base_table
    return owners[0]


def _partition_edges(
    edges: tuple[ResolvedRelationshipEdge, ...],
    base_name: str,
) -> tuple[list[list[ResolvedRelationshipEdge]], list[ResolvedRelationshipEdge]]:
    branches: list[list[ResolvedRelationshipEdge]] = []
    owners: dict[str, list[ResolvedRelationshipEdge]] = {}
    direct_edges: list[ResolvedRelationshipEdge] = []
    direct_tables = {base_name}
    for edge in edges:
        parent_branch = owners.get(edge.parent_table)
        if parent_branch is not None:
            parent_branch.append(edge)
            owners[edge.child_table] = parent_branch
            continue
        if edge.preaggregate:
            branch = [edge]
            branches.append(branch)
            owners[edge.child_table] = branch
            continue
        if edge.parent_table not in direct_tables:
            raise BusinessQueryValidationError("关系路径无法归入唯一预聚合分支")
        direct_edges.append(edge)
        direct_tables.add(edge.child_table)
    return branches, direct_edges


def _source_tables(
    plan: BusinessQueryPlan,
    snapshot: CatalogSnapshot,
) -> dict[str, Any]:
    sources = {
        name: table(
            snapshot.tables[name].name,
            *(column(column_name) for column_name in snapshot.tables[name].columns),
            schema=snapshot.tables[name].schema_name,
        )
        for name in plan.tables
    }
    for relationship_name in plan.relationships:
        relationship = snapshot.relationships[relationship_name]
        if relationship.cardinality not in {
            "many_to_one_latest_snapshot",
            "one_to_many_latest_snapshot",
        }:
            continue
        right_name = relationship.right_table
        right = sources[right_name]
        snapshot_column = relationship.snapshot_column
        if snapshot_column is None:
            raise BusinessQueryValidationError("最新快照关系缺少日期字段")
        key_fields = tuple(right_field for _, right_field in relationship.keys)
        latest = (
            select(
                *[right.c[field_name].label(field_name) for field_name in key_fields],
                func.max(right.c[snapshot_column]).label("snapshot_value"),
            )
            .group_by(*[right.c[field_name] for field_name in key_fields])
            .subquery(f"{right_name}_latest_dates")
        )
        latest_conditions = [
            right.c[field_name] == latest.c[field_name]
            for field_name in key_fields
        ]
        latest_conditions.append(
            right.c[snapshot_column] == latest.c.snapshot_value,
        )
        sources[right_name] = (
            select(*right.c)
            .select_from(right.join(latest, and_(*latest_conditions)))
            .subquery(f"{right_name}_latest")
        )
    return sources


def _join_condition(
    relationship: CatalogRelationship,
    sources: dict[str, Any],
) -> Any:
    left = sources[relationship.left_table]
    right = sources[relationship.right_table]
    conditions = [left.c[a] == right.c[b] for a, b in relationship.keys]
    if relationship.date_window:
        rule = relationship.date_window
        conditions.extend([
            right.c[rule.right_column]
            >= left.c[rule.left_column] - timedelta(days=rule.days_before),
            right.c[rule.right_column]
            <= left.c[rule.left_column] + timedelta(days=rule.days_after),
        ])
    if relationship.same_day:
        rule = relationship.same_day
        conditions.extend([
            right.c[rule.right_column] >= left.c[rule.left_column],
            right.c[rule.right_column]
            < left.c[rule.left_column] + timedelta(days=1),
        ])
    return and_(*conditions)


def _filter_expressions(
    filters: tuple[QueryFilter, ...],
    sources: dict[str, Any],
) -> list[tuple[QueryFilter, Any]]:
    return [
        (item, _filter_expression(sources[item.field.table].c[item.field.field], item))
        for item in filters
    ]


def _filter_expression(field: Any, item: QueryFilter) -> Any:
    value = item.value
    operations = {
        "eq": lambda: field == value,
        "ne": lambda: field != value,
        "gt": lambda: field > value,
        "gte": lambda: field >= value,
        "lt": lambda: field < value,
        "lte": lambda: field <= value,
        "in": lambda: field.in_(value),
        "between": lambda: field.between(value[0], value[1]),
        "is_null": lambda: field.is_(None),
        "is_not_null": lambda: field.is_not(None),
        "contains": lambda: field.ilike(f"%{_escape_like(value)}%", escape="!"),
        "starts_with": lambda: field.ilike(f"{_escape_like(value)}%", escape="!"),
    }
    return operations[item.operator]()


def _escape_like(value: Any) -> str:
    return str(value).replace("!", "!!").replace("%", "!%").replace("_", "!_")


def _aggregation_expression(
    item: QueryAggregation,
    sources: dict[str, Any],
) -> Any:
    field = sources[item.field.table].c[item.field.field] if item.field else None
    functions = {
        "count": func.count,
        "sum": func.sum,
        "avg": func.avg,
        "min": func.min,
        "max": func.max,
    }
    if field is None:
        return func.count().label(item.alias)
    return functions[item.function](field).label(item.alias)


def _array_agg_distinct(expression: Any) -> Any:
    return func.array_agg(distinct(expression)).filter(expression.is_not(None))


def _apply_order_and_limit(
    statement: Select,
    validated: ValidatedBusinessQuery,
    sources: dict[str, Any],
) -> Select:
    for item in validated.plan.order_by:
        expression = (
            column(_column_key(item.field))
            if item.field
            else column(item.aggregation_alias)
        )
        statement = statement.order_by(
            expression.desc() if item.direction == "desc" else expression.asc(),
        )
    return statement.limit(validated.limit)


def _column_key(item: QueryFieldRef) -> str:
    return f"{item.table}__{item.field}"


async def execute_business_query(
    db: Any,
    validated: ValidatedBusinessQuery,
    snapshot: CatalogSnapshot,
    settings: Settings,
) -> BusinessQueryResult:
    statement = build_business_statement(validated, snapshot)
    started = perf_counter()
    try:
        await db.execute(
            text("SELECT set_config('statement_timeout', :timeout, true)"),
            {"timeout": f"{settings.self_service_query_timeout_ms}ms"},
        )
        result = await db.execute(statement)
        rows = [dict(row) for row in result.mappings().all()]
    except DBAPIError as exc:
        if "timeout" in str(exc).lower() or "canceling statement" in str(exc).lower():
            raise BusinessQueryTimeoutError("查询范围较大导致超时") from exc
        raise BusinessQueryExecutionError("业务数据查询执行失败") from exc
    return BusinessQueryResult(
        rows=rows,
        column_order=tuple(
            [_column_key(item) for item in validated.query_fields]
            + [item.alias for item in validated.plan.aggregations]
        ),
        selected_tables=validated.table_names,
        relationship_ids=validated.relationship_names,
        result_grain=validated.result_grain,
        applied_defaults=validated.applied_defaults,
        database_ms=(perf_counter() - started) * 1000,
    )


def _is_text(data_type: str) -> bool:
    normalized = data_type.lower()
    return any(token in normalized for token in ("char", "text"))


def _is_numeric(data_type: str) -> bool:
    normalized = data_type.lower()
    return any(token in normalized for token in (
        "int", "numeric", "decimal", "real", "double", "float",
    ))


def _is_date(data_type: str) -> bool:
    normalized = data_type.lower()
    return "date" in normalized or "timestamp" in normalized


def _is_timestamp(data_type: str) -> bool:
    return "timestamp" in data_type.lower()
