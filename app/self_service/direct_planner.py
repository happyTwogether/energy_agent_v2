"""无需模型即可确定的单表小区日字段查询规划。"""

from datetime import date
import re
from typing import Sequence

from app.self_service.catalog import normalize_search_text, split_search_phrases
from app.self_service.models import (
    BusinessQueryPlan,
    CatalogCandidate,
    QueryFieldRef,
    QueryFilter,
)

_CGI_PATTERN = re.compile(r"(?<!\d)(\d{3}-\d{2}-\d+-\d+)(?!\d)")
_CHINESE_DATE_PATTERN = re.compile(
    r"(?<!\d)(\d{4})年(\d{1,2})月(\d{1,2})日?",
)
_SEPARATED_DATE_PATTERN = re.compile(
    r"(?<!\d)(\d{4})[-/](\d{1,2})[-/](\d{1,2})(?!\d)",
)
_COMPLEX_QUERY_PATTERN = re.compile(
    r"平均|总计|合计|数量|多少|占比|比率|趋势|排名|最高|最低|"
    r"对比|比较|同比|环比|汇总|分组|前\s*\d+|top\s*\d+",
    flags=re.IGNORECASE,
)


def build_direct_lookup_plan(
    question: str,
    candidates: Sequence[CatalogCandidate],
) -> BusinessQueryPlan | None:
    """只为信息完整、无歧义的 CGI+日期原始字段查询建立计划。"""
    if _COMPLEX_QUERY_PATTERN.search(question):
        return None
    cgi = _extract_cgi(question)
    query_date = _extract_date(question)
    if cgi is None or query_date is None:
        return None
    ranked = _eligible_candidates(question, candidates)
    if not ranked or _top_candidate_is_ambiguous(ranked):
        return None
    _, _, candidate, fields = ranked[0]
    return _create_plan(candidate, fields, cgi, query_date)


def _eligible_candidates(
    question: str,
    candidates: Sequence[CatalogCandidate],
) -> list[tuple[int, float, CatalogCandidate, tuple[str, ...]]]:
    expected_fields = len(split_search_phrases(question))
    requested_network = _question_network(question)
    ranked = []
    for candidate in candidates:
        table = candidate.table
        if (
            "cgi" not in table.columns
            or table.default_date_column not in table.columns
        ):
            continue
        fields = _requested_fields(candidate)
        table_network = _table_network(candidate)
        if candidate.matched_metrics or len(fields) < expected_fields:
            continue
        if requested_network and table_network != requested_network:
            continue
        ranked.append((len(fields), candidate.score, candidate, fields))
    return sorted(ranked, key=lambda item: (-item[0], -item[1]))


def _requested_fields(candidate: CatalogCandidate) -> tuple[str, ...]:
    table = candidate.table
    context_fields = {
        key
        for keys in table.grain_keys.values()
        for key in keys
    }
    if table.default_date_column:
        context_fields.add(table.default_date_column)
    return tuple(
        name
        for name in candidate.matched_columns
        if name not in context_fields
    )


def _top_candidate_is_ambiguous(
    ranked: Sequence[tuple[int, float, CatalogCandidate, tuple[str, ...]]],
) -> bool:
    if len(ranked) < 2:
        return False
    first, second = ranked[:2]
    return first[0] == second[0] and abs(first[1] - second[1]) < 0.05


def _create_plan(
    candidate: CatalogCandidate,
    fields: Sequence[str],
    cgi: str,
    query_date: date,
) -> BusinessQueryPlan:
    table = candidate.table
    filters = [
        QueryFilter(
            field=QueryFieldRef(table=table.name, field="cgi"),
            operator="eq",
            value=cgi,
        ),
        QueryFilter(
            field=QueryFieldRef(
                table=table.name,
                field=table.default_date_column or "",
            ),
            operator="eq",
            value=query_date,
        ),
    ]
    return BusinessQueryPlan(
        base_table=table.name,
        tables=[table.name],
        select=[QueryFieldRef(table=table.name, field=name) for name in fields],
        filters=filters,
    )


def _extract_cgi(question: str) -> str | None:
    match = _CGI_PATTERN.search(question)
    return match.group(1) if match else None


def _extract_date(question: str) -> date | None:
    for pattern in (_CHINESE_DATE_PATTERN, _SEPARATED_DATE_PATTERN):
        match = pattern.search(question)
        if match:
            try:
                return date(*(int(value) for value in match.groups()))
            except ValueError:
                return None
    return None


def _question_network(question: str) -> str | None:
    normalized = normalize_search_text(question)
    if "5g" in normalized or "nr" in normalized:
        return "nr"
    if "4g" in normalized or "lte" in normalized:
        return "lte"
    return None


def _table_network(candidate: CatalogCandidate) -> str | None:
    table = candidate.table
    normalized = normalize_search_text(
        " ".join((table.name, table.label, *table.aliases)),
    )
    if table.name.startswith("nr_") or "5g" in normalized:
        return "nr"
    if table.name.startswith("lte_") or "4g" in normalized:
        return "lte"
    return None
