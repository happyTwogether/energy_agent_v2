"""完全离线业务数据自服务的不可变目录与查询计划模型。"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class CatalogColumn(FrozenModel):
    name: str
    label: str
    data_type: str
    description: str = ""
    unit: str = ""
    aliases: tuple[str, ...] = ()


class CatalogTable(FrozenModel):
    schema_name: str
    name: str
    label: str
    description: str
    aliases: tuple[str, ...] = ()
    default_date_column: str | None = None
    default_grain: str
    grain_keys: dict[str, tuple[str, ...]]
    columns: dict[str, CatalogColumn]


class DateWindowRule(FrozenModel):
    left_column: str
    right_column: str
    days_before: int = Field(ge=0)
    days_after: int = Field(ge=0)


class SameDayRule(FrozenModel):
    left_column: str
    right_column: str


RelationshipCardinality = Literal[
    "many_to_one",
    "one_to_many",
    "one_to_many_window",
    "one_to_many_same_day",
    "many_to_one_latest_snapshot",
    "one_to_many_latest_snapshot",
]


class CatalogRelationship(FrozenModel):
    name: str
    left_table: str
    right_table: str
    cardinality: RelationshipCardinality
    keys: tuple[tuple[str, str], ...]
    allowed_grains: tuple[str, ...]
    detail_grain: str | None = None
    date_window: DateWindowRule | None = None
    same_day: SameDayRule | None = None
    snapshot_column: str | None = None


class CatalogSnapshot(FrozenModel):
    version: str
    tables: dict[str, CatalogTable]
    relationships: dict[str, CatalogRelationship]


class CatalogCandidate(FrozenModel):
    table: CatalogTable
    score: float
    matched_columns: tuple[str, ...] = ()
    matched_metrics: tuple[str, ...] = ()


class CatalogMetric(FrozenModel):
    id: str
    label: str
    description: str
    aliases: tuple[str, ...] = ()
    source_table: str
    source_fields: tuple[str, ...]
    calculator: str
    unit: str = ""
    data_type: str = "numeric"
    grain: str


FilterOperator = Literal[
    "eq", "ne", "gt", "gte", "lt", "lte", "contains", "starts_with",
    "in", "between", "is_null", "is_not_null",
]
AggregateFunction = Literal["count", "sum", "avg", "min", "max"]
ResultGrain = Literal[
    "cell_day", "summary_day", "cell_snapshot", "constriction_record",
    "hour", "neighbor", "parameter_item",
    "rru_item",
]


class QueryFieldRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    table: str
    field: str


class QueryFilter(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field: QueryFieldRef
    operator: FilterOperator
    value: Any = None


class QueryAggregation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    function: AggregateFunction
    field: QueryFieldRef | None = None
    alias: str


class QueryOrder(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field: QueryFieldRef | None = None
    aggregation_alias: str | None = None
    direction: Literal["asc", "desc"] = "asc"

    @model_validator(mode="after")
    def require_one_order_source(self) -> "QueryOrder":
        if (self.field is None) == (self.aggregation_alias is None):
            raise ValueError("排序字段和聚合别名必须且只能提供一个")
        return self


class BusinessQueryPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_table: str
    tables: list[str] = Field(min_length=1, max_length=3)
    relationships: list[str] = Field(default_factory=list, max_length=2)
    select: list[QueryFieldRef] = Field(default_factory=list)
    metrics: list[str] = Field(default_factory=list)
    filters: list[QueryFilter] = Field(default_factory=list)
    group_by: list[QueryFieldRef] = Field(default_factory=list)
    aggregations: list[QueryAggregation] = Field(default_factory=list)
    order_by: list[QueryOrder] = Field(default_factory=list)
    result_grain: ResultGrain | None = None
    limit: int = Field(default=50, ge=1)
    clarification: str | None = None

    @model_validator(mode="after")
    def validate_structure(self) -> "BusinessQueryPlan":
        if len(set(self.tables)) != len(self.tables):
            raise ValueError("查询表不能重复")
        if self.base_table not in self.tables:
            raise ValueError("主表必须属于查询表")
        if len(self.relationships) != len(self.tables) - 1:
            raise ValueError("关系数量必须等于表数量减一")
        references = list(self.select) + list(self.group_by)
        references.extend(item.field for item in self.filters)
        references.extend(item.field for item in self.aggregations if item.field)
        references.extend(item.field for item in self.order_by if item.field)
        if any(reference.table not in self.tables for reference in references):
            raise ValueError("字段引用必须属于查询表")
        if not self.clarification and not (
            self.select or self.metrics or self.aggregations
        ):
            raise ValueError("可执行查询必须选择字段、指标或聚合")
        return self
