"""单小区中文名解析辅助模块。

具体数据表和日期范围由上层解析服务提供，本模块只负责统一的
“精确优先、唯一模糊命中自动解析、多结果返回候选”规则。
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal


MIN_FUZZY_NAME_LENGTH = 2
MAX_CELL_CANDIDATES = 10

CellCandidateFetcher = Callable[[str, bool, int], Awaitable[list[dict[str, Any]]]]
ResolutionStatus = Literal["resolved", "ambiguous", "not_found", "invalid"]


@dataclass(slots=True)
class CellNameResolution:
    """小区名解析结果。"""

    status: ResolutionStatus
    query: str
    match_type: Literal["exact", "fuzzy"] | None = None
    cgi: str | None = None
    cell_name: str | None = None
    network: str | None = None
    candidate: dict[str, Any] | None = None
    candidates: list[dict[str, Any]] = field(default_factory=list)
    candidates_truncated: bool = False
    message: str = ""


def build_contains_pattern(value: str) -> str:
    """构建将用户输入视为普通文本的 ILIKE 包含模式。"""
    escaped = value.replace("!", "!!").replace("%", "!%").replace("_", "!_")
    return f"%{escaped}%"


def infer_network_from_cgi(cgi: str | None) -> str:
    """沿用现有 CGI 规则推断网络制式。"""
    return "5G" if cgi and cgi.startswith("460-00") else "4G"


def _effective_length(value: str) -> int:
    return sum(1 for char in value if not char.isspace())


def _candidate_from_row(row: dict[str, Any]) -> dict[str, Any] | None:
    cgi = str(row.get("cgi") or "").strip()
    if not cgi:
        return None
    return {
        "cell_name": row.get("cell_name") or "-",
        "cgi": cgi,
        "network": row.get("network") or infer_network_from_cgi(cgi),
        "dist_name": row.get("dist_name") or "-",
        "county_name": row.get("county_name") or "-",
        "prod_name": row.get("prod_name") or "-",
    }


def _merge_candidate(existing: dict[str, Any], incoming: dict[str, Any]) -> None:
    for key, value in incoming.items():
        if existing.get(key) in (None, "", "-") and value not in (None, "", "-"):
            existing[key] = value


def normalize_candidates(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按网络制式 + CGI 去重并补齐候选信息。"""
    unique_candidates: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        candidate = _candidate_from_row(row)
        if not candidate:
            continue
        key = (candidate["network"], candidate["cgi"])
        if key in unique_candidates:
            _merge_candidate(unique_candidates[key], candidate)
        else:
            unique_candidates[key] = candidate
    return sorted(
        unique_candidates.values(),
        key=lambda item: (str(item["cell_name"]), item["network"], item["cgi"]),
    )


def _resolved(
    query: str,
    match_type: Literal["exact", "fuzzy"],
    candidate: dict[str, Any],
) -> CellNameResolution:
    return CellNameResolution(
        status="resolved",
        query=query,
        match_type=match_type,
        cgi=candidate["cgi"],
        cell_name=candidate["cell_name"],
        network=candidate["network"],
        candidate=candidate,
    )


def _ambiguous(
    query: str,
    match_type: Literal["exact", "fuzzy"],
    candidates: list[dict[str, Any]],
) -> CellNameResolution:
    truncated = len(candidates) > MAX_CELL_CANDIDATES
    return CellNameResolution(
        status="ambiguous",
        query=query,
        match_type=match_type,
        candidates=candidates[:MAX_CELL_CANDIDATES],
        candidates_truncated=truncated,
        message=f"小区名称“{query}”匹配到多个小区，请从候选列表中选择 CGI。",
    )


def _evaluate_matches(
    query: str,
    match_type: Literal["exact", "fuzzy"],
    rows: list[dict[str, Any]],
) -> CellNameResolution | None:
    candidates = normalize_candidates(rows)
    if len(candidates) == 1:
        return _resolved(query, match_type, candidates[0])
    if len(candidates) > 1:
        return _ambiguous(query, match_type, candidates)
    return None


async def resolve_cell_name(
    cell_name: str | None,
    fetch_candidates: CellCandidateFetcher,
) -> CellNameResolution:
    """使用工具提供的查询函数将中文名解析为唯一 CGI。"""
    query = (cell_name or "").strip()
    if not query:
        return CellNameResolution(status="invalid", query=query, message="请提供 CGI 或小区中文名。")

    fetch_limit = MAX_CELL_CANDIDATES + 1
    exact = _evaluate_matches(query, "exact", await fetch_candidates(query, True, fetch_limit))
    if exact:
        return exact

    if _effective_length(query) < MIN_FUZZY_NAME_LENGTH:
        return CellNameResolution(
            status="invalid",
            query=query,
            message=f"小区名模糊查询至少需要 {MIN_FUZZY_NAME_LENGTH} 个有效字符。",
        )

    pattern = build_contains_pattern(query)
    fuzzy = _evaluate_matches(query, "fuzzy", await fetch_candidates(pattern, False, fetch_limit))
    if fuzzy:
        return fuzzy
    return CellNameResolution(
        status="not_found",
        query=query,
        match_type="fuzzy",
        message=f"未找到包含“{query}”的小区。",
    )


def resolution_error_response(resolution: CellNameResolution) -> dict[str, Any]:
    """将未解析到唯一 CGI 的结果转为统一工具响应。"""
    response: dict[str, Any] = {
        "success": False,
        "error": resolution.message,
        "cell_name_query": resolution.query,
    }
    if resolution.status == "ambiguous":
        response.update({
            "requires_cell_selection": True,
            "match_type": resolution.match_type,
            "candidates": resolution.candidates,
            "candidates_truncated": resolution.candidates_truncated,
        })
    return response
