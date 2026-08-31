"""白名单关系树的方向解析和粒度控制。"""

from dataclasses import dataclass, replace

from app.self_service.models import CatalogRelationship


@dataclass(frozen=True, slots=True)
class ResolvedRelationshipEdge:
    relationship: CatalogRelationship
    parent_table: str
    child_table: str
    cardinality: str
    preaggregate: bool


@dataclass(frozen=True, slots=True)
class ResolvedRelationshipPath:
    edges: tuple[ResolvedRelationshipEdge, ...]


def orient_relationships(
    base_table: str,
    relationships: list[CatalogRelationship],
    result_grain: str,
) -> ResolvedRelationshipPath:
    """把无向白名单边整理成从主表向外的稳定树。"""
    visited = {base_table}
    remaining = list(relationships)
    resolved: list[ResolvedRelationshipEdge] = []
    while remaining:
        progress = False
        for relationship in list(remaining):
            left_seen = relationship.left_table in visited
            right_seen = relationship.right_table in visited
            if left_seen == right_seen:
                continue
            parent = relationship.left_table if left_seen else relationship.right_table
            child = relationship.right_table if left_seen else relationship.left_table
            cardinality = _effective_cardinality(relationship, parent)
            preaggregate = (
                cardinality.startswith("one_to_many")
                and result_grain != relationship.detail_grain
            )
            resolved.append(ResolvedRelationshipEdge(
                relationship=relationship,
                parent_table=parent,
                child_table=child,
                cardinality=cardinality,
                preaggregate=preaggregate,
            ))
            visited.add(child)
            remaining.remove(relationship)
            progress = True
        if not progress:
            raise ValueError("关系边没有形成从主表可达的无环树")
    return ResolvedRelationshipPath(
        edges=_expand_path_to_downstream_detail(
            resolved,
            base_table,
            result_grain,
        ),
    )


def _expand_path_to_downstream_detail(
    edges: list[ResolvedRelationshipEdge],
    base_table: str,
    result_grain: str,
) -> tuple[ResolvedRelationshipEdge, ...]:
    detail_edges = [
        edge
        for edge in edges
        if edge.relationship.detail_grain == result_grain
    ]
    if len(detail_edges) != 1:
        return tuple(edges)
    by_child = {edge.child_table: index for index, edge in enumerate(edges)}
    expanded_indexes: set[int] = set()
    cursor = detail_edges[0].parent_table
    while cursor != base_table:
        index = by_child.get(cursor)
        if index is None:
            break
        expanded_indexes.add(index)
        cursor = edges[index].parent_table
    return tuple(
        replace(edge, preaggregate=False)
        if index in expanded_indexes
        else edge
        for index, edge in enumerate(edges)
    )


def _effective_cardinality(
    relationship: CatalogRelationship,
    parent_table: str,
) -> str:
    cardinality = relationship.cardinality
    if parent_table == relationship.left_table:
        return cardinality
    if cardinality.startswith("one_to_many"):
        return "many_to_one"
    if cardinality.startswith("many_to_one"):
        return "one_to_many"
    return cardinality
