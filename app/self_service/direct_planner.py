"""无需模型即可确定的业务查询规划。"""

from dataclasses import dataclass
from datetime import date
import re
from typing import Sequence

from app.self_service.catalog import normalize_search_text, split_search_phrases
from app.self_service.models import (
    BusinessQueryPlan,
    CatalogCandidate,
    CatalogSnapshot,
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
_DIMENSION_ALIASES = {
    "prod_name": ("设备厂家", "设备厂商", "厂家", "厂商", "设备商"),
    "freq_band": ("频段",),
    "site_type": ("站点类型", "站型"),
    "area": ("区域类型", "区域"),
    "dist_name": ("地市", "市州"),
}
_VALUE_LIST_MARKERS = ("有哪些", "有什么", "能看哪些", "可以看哪些", "可选值")
_LEADING_QUERY_WORDS = (
    "那",
    "请问",
    "查询",
    "查一下",
    "看看",
    "帮我看看",
    "我想知道",
)
_REPORT_SUMMARY_TABLES = (
    "lte_report_day_collect",
    "nr_report_day_collect",
)


@dataclass(frozen=True, slots=True)
class DimensionValueLookup:
    """已确定的报表维度值查询。"""

    dimension: str
    plans: tuple[BusinessQueryPlan, ...]


def build_dimension_value_lookup(
    question: str,
    snapshot: CatalogSnapshot,
) -> DimensionValueLookup | None:
    """为报表维度可选值构建受控的 4G/5G 分组查询。"""
    normalized = _normalize_dimension_question(question)
    dimension = _extract_dimension(normalized)
    if dimension is None or not any(
        marker in normalized for marker in _VALUE_LIST_MARKERS
    ):
        return None

    filters = _extract_location_filters(normalized, dimension)
    requested_network = _question_network(question)
    plans: list[BusinessQueryPlan] = []
    for table_name in _REPORT_SUMMARY_TABLES:
        table = snapshot.tables.get(table_name)
        table_network = "nr" if table_name.startswith("nr_") else "lte"
        if requested_network and table_network != requested_network:
            continue
        required_columns = {dimension, *(field for field, _ in filters)}
        if table is None or not required_columns.issubset(table.columns):
            continue
        dimension_ref = QueryFieldRef(table=table_name, field=dimension)
        plans.append(BusinessQueryPlan(
            base_table=table_name,
            tables=[table_name],
            select=[dimension_ref],
            filters=[
                QueryFilter(
                    field=QueryFieldRef(table=table_name, field=field),
                    operator="eq",
                    value=value,
                )
                for field, value in filters
            ],
            group_by=[dimension_ref],
        ))
    if not plans:
        return None
    return DimensionValueLookup(dimension=dimension, plans=tuple(plans))


def _normalize_dimension_question(question: str) -> str:
    return re.sub(r"[\s，。！？,!?]", "", question).lower()


def _extract_dimension(question: str) -> str | None:
    matches = (
        (question.find(alias), -len(alias), dimension)
        for dimension, aliases in _DIMENSION_ALIASES.items()
        for alias in aliases
        if alias in question
    )
    return min(matches, default=(0, 0, None))[2]


def _extract_location_filters(
    question: str,
    dimension: str,
) -> tuple[tuple[str, str], ...]:
    province_match = re.search(r"([\u4e00-\u9fff]{2,6}省)", question)
    city_search_start = province_match.end() if province_match else 0
    city_match = re.search(
        r"([\u4e00-\u9fff]{2,6}市)",
        question[city_search_start:],
    )
    filters: list[tuple[str, str]] = []
    if province_match and province_match.group(1) != "全省":
        filters.append(("province", _strip_query_prefix(province_match.group(1))))
    if city_match:
        filters.append(("dist_name", _strip_query_prefix(city_match.group(1))))
        return tuple(filters)

    aliases = _DIMENSION_ALIASES[dimension]
    dimension_index = min(
        question.find(alias) for alias in aliases if alias in question
    )
    marker_indexes = [
        question.find(marker)
        for marker in _VALUE_LIST_MARKERS
        if marker in question
    ]
    boundary = min([dimension_index, *marker_indexes])
    location = _strip_query_prefix(question[:boundary]).removesuffix("的")
    location = re.sub(r"(?:4g|5g|lte|nr|全网|全省)", "", location)
    if re.fullmatch(r"[\u4e00-\u9fff]{2,6}", location):
        filters.append(("dist_name", f"{location}市"))
    return tuple(filters)


def _strip_query_prefix(value: str) -> str:
    stripped = value
    changed = True
    while changed:
        changed = False
        for prefix in _LEADING_QUERY_WORDS:
            if stripped.startswith(prefix):
                stripped = stripped.removeprefix(prefix)
                changed = True
    return stripped


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
