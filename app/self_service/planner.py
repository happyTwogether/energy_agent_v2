"""通过内部 OpenAI-compatible 模型生成受控查询计划。"""

import json
from typing import Any, Sequence

from agentscope.message import SystemMsg, UserMsg
from agentscope.model import ChatModelBase

from app.agent.model import build_chat_model
from app.core.logging import get_logger
from app.self_service.metrics import METRIC_REGISTRY
from app.self_service.models import (
    BusinessQueryPlan,
    CatalogCandidate,
    CatalogColumn,
    CatalogRelationship,
)

logger = get_logger("business_query_planner")


class BusinessQueryPlanningError(RuntimeError):
    """查询意图无法被安全转换为结构化计划。"""


class BusinessQueryPlanner:
    def __init__(self, model: ChatModelBase | None = None) -> None:
        self._model = model or build_chat_model(stream=False)

    async def plan(
        self,
        question: str,
        candidates: Sequence[CatalogCandidate],
        relationships: Sequence[CatalogRelationship],
    ) -> BusinessQueryPlan:
        try:
            response = await self._model.generate_structured_output(
                messages=build_planner_messages(
                    question,
                    candidates[:5],
                    relationships,
                ),
                structured_model=BusinessQueryPlan,
            )
            return BusinessQueryPlan.model_validate(response.content)
        except Exception as exc:
            logger.warning("业务查询结构化规划失败: %s", type(exc).__name__)
            raise BusinessQueryPlanningError(
                "暂时无法理解该数据查询，请换一种说法或明确表名和字段。",
            ) from exc


def build_planner_messages(
    question: str,
    candidates: Sequence[CatalogCandidate],
    relationships: Sequence[CatalogRelationship],
) -> list[Any]:
    catalog = {
        "tables": [
            {
                "id": candidate.table.name,
                "name": candidate.table.label,
                "purpose": candidate.table.description,
                "default_date_column": candidate.table.default_date_column,
                "default_grain": candidate.table.default_grain,
                "columns": [
                    {
                        "id": column.name,
                        "name": column.label,
                        "description": column.description,
                        "type": column.data_type,
                        "unit": column.unit,
                    }
                    for column in _planner_columns(candidate, relationships)
                ],
                "metrics": [
                    {
                        "id": metric.id,
                        "name": metric.label,
                        "description": metric.description,
                        "source_fields": list(metric.source_fields),
                        "unit": metric.unit,
                        "grain": metric.grain,
                    }
                    for metric_id in candidate.matched_metrics
                    if (metric := METRIC_REGISTRY.get(metric_id)) is not None
                ],
            }
            for candidate in candidates
        ],
        "relationships": [relationship.model_dump() for relationship in relationships],
    }
    rules = (
        "你是完全离线业务数据查询规划器。只能选择候选表、精确字段和关系 ID；"
        "最多选择3张表，只能选择关系 ID，不得自行编写关联键。"
        "不得输出 SQL、Schema 名、JOIN 表达式、子查询或窗口函数。"
        "计算指标只能放入 metrics，不能把指标 ID 当作物理字段，"
        "也不能把计算指标用于过滤、分组或排序。"
        "用户没有明确要求逐条或逐小时等明细时，result_grain 留空。"
        "一次只能选择一种明细粒度。CGI 使用 eq；名称允许 contains。"
        "没有日期时不要虚构日期。存在歧义时填写 clarification，不执行猜测。"
    )
    return [
        SystemMsg(name="system", content=rules),
        UserMsg(
            name="user",
            content=(
                f"授权目录：{json.dumps(catalog, ensure_ascii=False)}\n"
                f"用户问题：{question}"
            ),
        ),
    ]


def _planner_columns(
    candidate: CatalogCandidate,
    relationships: Sequence[CatalogRelationship],
) -> list:
    """只发送命中字段和查询安全所需键，限制宽表 Prompt 大小。"""
    table_info = candidate.table
    required = set(candidate.matched_columns)
    required.update(
        key
        for keys in table_info.grain_keys.values()
        for key in keys
    )
    if table_info.default_date_column:
        required.add(table_info.default_date_column)
    for relationship in relationships:
        if relationship.left_table == table_info.name:
            required.update(left for left, _ in relationship.keys)
            if relationship.date_window:
                required.add(relationship.date_window.left_column)
            if relationship.same_day:
                required.add(relationship.same_day.left_column)
        if relationship.right_table == table_info.name:
            required.update(right for _, right in relationship.keys)
            if relationship.date_window:
                required.add(relationship.date_window.right_column)
            if relationship.same_day:
                required.add(relationship.same_day.right_column)
            if relationship.snapshot_column:
                required.add(relationship.snapshot_column)
    if not candidate.matched_columns:
        required.update(list(table_info.columns)[:20])
    for metric_id in candidate.matched_metrics:
        metric = METRIC_REGISTRY.get(metric_id)
        if metric is not None:
            required.update(metric.source_fields)
    return [
        column_info
        for name, column_info in table_info.columns.items()
        if name in required
    ][:30]
