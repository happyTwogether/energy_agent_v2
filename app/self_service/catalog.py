"""授权业务 Schema 的启动快照和纯内存候选搜索。"""

from collections import defaultdict
import asyncio
from difflib import SequenceMatcher
from itertools import combinations
from pathlib import Path
import re
import unicodedata
from typing import Any, Sequence

from sqlalchemy import text
import yaml

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.self_service.metrics import METRIC_REGISTRY
from app.self_service.models import (
    CatalogCandidate,
    CatalogColumn,
    CatalogRelationship,
    CatalogSnapshot,
    CatalogTable,
)

logger = get_logger("business_catalog")

CATALOG_METADATA_SQL = text("""
WITH requested(schema_name, table_name) AS (
    SELECT * FROM unnest(
        CAST(:schema_names AS text[]),
        CAST(:table_names AS text[])
    )
)
SELECT
    columns.table_schema AS schema_name,
    columns.table_name,
    columns.column_name,
    columns.data_type,
    pg_catalog.col_description(
        (quote_ident(columns.table_schema) || '.' ||
         quote_ident(columns.table_name))::regclass::oid,
        columns.ordinal_position
    ) AS description
FROM information_schema.columns AS columns
JOIN requested
  ON requested.schema_name = columns.table_schema
 AND requested.table_name = columns.table_name
ORDER BY columns.table_schema, columns.table_name, columns.ordinal_position
""")


def normalize_search_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).lower()
    return re.sub(r"[\s_\-/]+", "", normalized)


def candidate_score(query: str, values: Sequence[str]) -> float:
    normalized_query = normalize_search_text(query)
    scores: list[float] = []
    for value in values:
        normalized_value = normalize_search_text(value)
        if not normalized_value:
            continue
        if normalized_query == normalized_value:
            scores.append(1.0)
        elif normalized_value in normalized_query or normalized_query in normalized_value:
            scores.append(0.85)
        else:
            ratio = SequenceMatcher(None, normalized_query, normalized_value).ratio()
            scores.append(ratio * 0.6)
    return max(scores, default=0.0)


class BusinessCatalogStore:
    """加载一次授权目录并为后续请求提供纯内存读取。"""

    def __init__(
        self,
        policy_path: Path | str = "config/business_data_catalog.yaml",
        settings: Settings | None = None,
    ) -> None:
        self._policy_path = Path(policy_path)
        self._settings = settings or get_settings()
        self._snapshot: CatalogSnapshot | None = None
        self._load_lock = asyncio.Lock()

    async def get_or_load(self, db: Any) -> CatalogSnapshot:
        if self._snapshot is not None:
            return self._snapshot
        async with self._load_lock:
            if self._snapshot is None:
                self._snapshot = await self._load(db)
        return self._snapshot

    def snapshot(self) -> CatalogSnapshot:
        if self._snapshot is None:
            raise RuntimeError("业务数据目录尚未预热")
        return self._snapshot

    def search(self, question: str, limit: int) -> list[CatalogCandidate]:
        snapshot = self.snapshot()
        candidates: list[tuple[int, CatalogCandidate]] = []
        for index, table_info in enumerate(snapshot.tables.values()):
            table_values = [
                table_info.name,
                table_info.label,
                table_info.description,
                *table_info.aliases,
            ]
            table_score = candidate_score(question, table_values)
            column_scores = [
                (
                    candidate_score(
                        question,
                        [column.name, column.label, column.description, *column.aliases],
                    ),
                    column.name,
                )
                for column in table_info.columns.values()
            ]
            metric_scores = [
                (
                    candidate_score(
                        question,
                        [metric.id, metric.label, metric.description, *metric.aliases],
                    ),
                    metric.id,
                )
                for metric in METRIC_REGISTRY.values()
                if metric.source_table == table_info.name
            ]
            matched = tuple(
                name
                for score, name in sorted(column_scores, reverse=True)[:5]
                if score >= 0.24
            )
            best_column = max((score for score, _ in column_scores), default=0.0)
            matched_metrics = tuple(
                name
                for score, name in sorted(metric_scores, reverse=True)[:5]
                if score >= 0.24
            )
            best_metric = max((score for score, _ in metric_scores), default=0.0)
            score = max(table_score, best_column * 0.95, best_metric)
            if score >= 0.24:
                candidates.append((index, CatalogCandidate(
                    table=table_info,
                    score=score,
                    matched_columns=matched,
                    matched_metrics=matched_metrics,
                )))
        candidates.sort(key=lambda item: (-item[1].score, item[0]))
        return [candidate for _, candidate in candidates[:limit]]

    def find_paths(
        self,
        table_names: Sequence[str],
    ) -> list[tuple[CatalogRelationship, ...]]:
        selected = set(table_names)
        if not selected or len(selected) > 3:
            return []
        if len(selected) == 1:
            return [()]
        available = [
            relationship
            for relationship in self.snapshot().relationships.values()
            if relationship.left_table in selected
            and relationship.right_table in selected
        ]
        paths: list[tuple[CatalogRelationship, ...]] = []
        for edges in combinations(available, len(selected) - 1):
            reached = {table_names[0]}
            changed = True
            while changed:
                changed = False
                for edge in edges:
                    endpoints = {edge.left_table, edge.right_table}
                    if endpoints & reached and not endpoints <= reached:
                        reached.update(endpoints)
                        changed = True
            if reached == selected:
                paths.append(tuple(edges))
        return paths

    def complete_candidates(
        self,
        candidates: Sequence[CatalogCandidate],
        max_tables: int = 3,
    ) -> list[CatalogCandidate]:
        """为两个已命中但不直连的业务表补全唯一的单表桥接路径。"""
        completed = list(candidates)
        if len(completed) != 2 or max_tables < 3:
            return completed
        selected_names = [candidate.table.name for candidate in completed]
        if self.find_paths(selected_names):
            return completed
        selected = set(selected_names)
        possible_bridges: list[str] = []
        for table_name in self.snapshot().tables:
            if table_name in selected:
                continue
            if self.find_paths([*selected_names, table_name]):
                possible_bridges.append(table_name)
        if len(possible_bridges) != 1:
            return completed
        bridge = self.snapshot().tables[possible_bridges[0]]
        completed.append(CatalogCandidate(table=bridge, score=0.0))
        return completed

    async def _load(self, db: Any) -> CatalogSnapshot:
        policy = yaml.safe_load(self._policy_path.read_text(encoding="utf-8"))
        table_policy: dict[str, dict[str, Any]] = policy["tables"]
        requested = [
            (getattr(self._settings, item["schema_source"]), table_name)
            for table_name, item in table_policy.items()
        ]
        result = await db.execute(
            CATALOG_METADATA_SQL,
            {
                "schema_names": [schema for schema, _ in requested],
                "table_names": [table_name for _, table_name in requested],
            },
        )
        rows = result.mappings().all()
        rows_by_table: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            rows_by_table[(row["schema_name"], row["table_name"])].append(dict(row))

        tables: dict[str, CatalogTable] = {}
        for table_name, item in table_policy.items():
            schema_name = getattr(self._settings, item["schema_source"])
            table_rows = rows_by_table.get((schema_name, table_name), [])
            if not table_rows:
                logger.warning("授权业务表不存在: %s.%s", schema_name, table_name)
                continue
            configured_fields = item.get("fields", {})
            database_fields = {
                row["column_name"]: row
                for row in table_rows
            }
            missing_fields = set(configured_fields) - set(database_fields)
            if missing_fields:
                logger.error(
                    "授权字段在数据库中不存在，已跳过: %s.%s fields=%s",
                    schema_name,
                    table_name,
                    sorted(missing_fields),
                )
            columns = {
                field_name: self._build_column(
                    database_fields[field_name],
                    field_policy,
                )
                for field_name, field_policy in configured_fields.items()
                if field_name in database_fields
            }
            grain_keys = {
                name: tuple(keys)
                for name, keys in item["grain_keys"].items()
            }
            if not self._table_policy_is_valid(item, columns, grain_keys):
                logger.error("授权业务表粒度配置无效: %s.%s", schema_name, table_name)
                continue
            tables[table_name] = CatalogTable(
                schema_name=schema_name,
                name=table_name,
                label=item["label"],
                description=item["description"],
                aliases=tuple(item.get("aliases", [])),
                default_date_column=item.get("default_date_column"),
                default_grain=item["default_grain"],
                grain_keys=grain_keys,
                columns=columns,
            )
        relationships = self._load_relationships(policy, tables)
        return CatalogSnapshot(
            version=str(policy["version"]),
            tables=tables,
            relationships=relationships,
        )

    def _build_column(
        self,
        row: dict[str, Any],
        override: dict[str, Any],
    ) -> CatalogColumn:
        name = row["column_name"]
        return CatalogColumn(
            name=name,
            label=override["label"],
            data_type=row["data_type"],
            description=override["description"],
            unit=override.get("unit", ""),
            aliases=tuple(override.get("aliases", [])),
        )

    @staticmethod
    def _table_policy_is_valid(
        item: dict[str, Any],
        columns: dict[str, CatalogColumn],
        grain_keys: dict[str, tuple[str, ...]],
    ) -> bool:
        if item["default_grain"] not in grain_keys:
            return False
        required = {key for keys in grain_keys.values() for key in keys}
        default_date = item.get("default_date_column")
        if default_date:
            required.add(default_date)
        return required.issubset(columns)

    def _load_relationships(
        self,
        policy: dict[str, Any],
        tables: dict[str, CatalogTable],
    ) -> dict[str, CatalogRelationship]:
        relationships: dict[str, CatalogRelationship] = {}
        for name, item in policy["relationships"].items():
            left = tables.get(item["left"])
            right = tables.get(item["right"])
            if left is None or right is None:
                continue
            keys = tuple(tuple(pair) for pair in item.get("keys", []))
            if any(a not in left.columns or b not in right.columns for a, b in keys):
                logger.error("白名单关系关联键无效: %s", name)
                continue
            relationship = CatalogRelationship(
                name=name,
                left_table=left.name,
                right_table=right.name,
                cardinality=item["cardinality"],
                keys=keys,
                allowed_grains=tuple(item["allowed_grains"]),
                detail_grain=item.get("detail_grain"),
                date_window=item.get("date_window"),
                same_day=item.get("same_day"),
                snapshot_column=item.get("snapshot_column"),
            )
            if _relationship_columns_exist(relationship, left, right):
                relationships[name] = relationship
            else:
                logger.error("白名单关系日期或快照字段无效: %s", name)
        return relationships


def _relationship_columns_exist(
    relationship: CatalogRelationship,
    left: CatalogTable,
    right: CatalogTable,
) -> bool:
    if relationship.date_window:
        if relationship.date_window.left_column not in left.columns:
            return False
        if relationship.date_window.right_column not in right.columns:
            return False
    if relationship.same_day:
        if relationship.same_day.left_column not in left.columns:
            return False
        if relationship.same_day.right_column not in right.columns:
            return False
    if relationship.snapshot_column:
        return relationship.snapshot_column in right.columns
    return True
