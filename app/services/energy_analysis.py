"""V1.4 节电扩展与收缩的纯业务规则。"""

from __future__ import annotations

import json
from typing import Any, Literal

NetworkType = Literal["4G", "5G"]
RelationStatus = Literal[
    "allowed",
    "out_of_range",
    "site_type_mismatch",
    "missing",
]

_NETWORK_ALIASES: dict[str, NetworkType] = {
    "4g": "4G",
    "lte": "4G",
    "5g": "5G",
    "nr": "5G",
}
_SITE_POLICIES: dict[str, tuple[float, frozenset[str] | None]] = {
    "宏站": (200.0, frozenset({"宏站"})),
    "室分": (100.0, frozenset({"宏站", "室分"})),
    "微站": (200.0, None),
}
EXPANSION_HOURS = (22, 23, 0, 1, 2, 3, 4, 5, 6, 7)
LOW_FLOW_PCT_THRESHOLD = 90.0
_TRUE_FLAGS = frozenset({"1", "true", "yes", "y", "是"})


def normalize_boolean_flag(value: Any) -> bool:
    """将数据库常见布尔值归一化，避免非空文本“否”被判为真。"""
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in _TRUE_FLAGS


def _hour_set(value: Any) -> set[int]:
    if isinstance(value, list):
        parts = value
    elif value is None:
        parts = []
    else:
        parts = str(value).strip("[]").split(",")
    result: set[int] = set()
    for part in parts:
        try:
            result.add(int(str(part).strip()))
        except (TypeError, ValueError):
            continue
    return result


def _expansion_suggestion(
    hour: int,
    expandable_hours: set[int],
    low_business: bool | None,
    zero_sleep: bool | None,
) -> str:
    if hour in expandable_hours:
        return "可扩展"
    if low_business is None or zero_sleep is None:
        return "数据缺失"
    if not low_business:
        return "不建议（低业务占比不足）"
    if not zero_sleep:
        return "不建议（已有休眠）"
    return "以综合分析结果为准"


def _build_expansion_hour_row(
    hour: int,
    detail_by_hour: dict[int, Any],
    sleep_by_hour: dict[int, Any],
    expandable_hours: set[int],
) -> dict[str, Any]:
    percent = detail_by_hour.get(hour)
    sleep_seconds = sleep_by_hour.get(hour)
    low_business = None if percent is None else float(percent) >= LOW_FLOW_PCT_THRESHOLD
    zero_sleep = None if sleep_seconds is None else float(sleep_seconds) == 0
    return {
        "hour": hour,
        "low_flow_pct": None if percent is None else float(percent),
        "is_low_business": low_business,
        "avg_sleep_seconds": None if sleep_seconds is None else float(sleep_seconds),
        "is_zero_sleep": zero_sleep,
        "suggestion": _expansion_suggestion(
            hour, expandable_hours, low_business, zero_sleep,
        ),
    }


def build_expansion_hour_evidence(
    record: dict[str, Any],
    sleep_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """构造扩展时窗的逐小时过程证据，最终结论以源结果为准。"""
    detail_by_hour = {
        item.get("hour"): item.get("low_flow_pct")
        for item in record.get("hour_detail", [])
        if item.get("hour") is not None
    }
    sleep_by_hour = {
        row.get("hours"): row.get("avg_sleep_sum")
        for row in sleep_rows
    }
    expandable_hours = _hour_set(record.get("hour_filter"))
    return [
        _build_expansion_hour_row(
            hour, detail_by_hour, sleep_by_hour, expandable_hours,
        )
        for hour in EXPANSION_HOURS
    ]


def normalize_network_type(value: str | None) -> NetworkType | None:
    """将数据库网络枚举归一化为 4G/5G。"""
    if not value:
        return None
    return _NETWORK_ALIASES.get(value.strip().lower())


def neighbor_policy(
    site_type: str | None,
) -> tuple[float, frozenset[str] | None]:
    """返回主小区站型对应的距离和允许邻区站型。"""
    normalized = (site_type or "").strip()
    return _SITE_POLICIES.get(normalized, (200.0, None))


def evaluate_neighbor_relation(
    main_site_type: str | None,
    neighbor_site_type: str | None,
    distance: float | None,
) -> RelationStatus:
    """评估已补齐关系信息是否满足确认后的站型和距离规则。"""
    if not main_site_type or not neighbor_site_type or distance is None:
        return "missing"
    max_distance, allowed_types = neighbor_policy(main_site_type)
    if distance > max_distance:
        return "out_of_range"
    if allowed_types is not None and neighbor_site_type not in allowed_types:
        return "site_type_mismatch"
    return "allowed"


def build_expansion_record(row: dict[str, Any]) -> dict[str, Any]:
    """复制数据库扩展结果，并将 JSON 小时详情规范为列表。"""
    result = dict(row)
    raw_detail = result.get("hour_detail")
    if isinstance(raw_detail, str):
        try:
            parsed = json.loads(raw_detail)
        except json.JSONDecodeError:
            parsed = []
        result["hour_detail"] = parsed if isinstance(parsed, list) else []
    elif not isinstance(raw_detail, list):
        result["hour_detail"] = []
    return result


def is_pre_sleep_hour(prb_hour: int | None, sleep_hour: str | None) -> bool:
    """判断 PRB 小时是否为任一休眠时间点的前一小时。"""
    if prb_hour is None or not sleep_hour:
        return False
    try:
        sleep_hours = [
            int(hour.strip())
            for hour in sleep_hour.split(",")
            if hour.strip()
        ]
    except (ValueError, AttributeError):
        return False
    return prb_hour in {(hour - 1) % 24 for hour in sleep_hours}


def collect_site_type_cgis(
    rows: list[dict[str, Any]],
    relation_by_pair: dict[tuple[str, str], dict[str, Any]],
) -> tuple[set[str], set[str]]:
    """按网络收集需要补齐站型的主小区和邻区 CGI。"""
    nr_cgis: set[str] = set()
    lte_cgis: set[str] = set()
    for row in rows:
        main_cgi = str(row.get("cgi") or "")
        around_cgi = str(row.get("around_cgi") or "")
        if main_cgi and not row.get("site_type"):
            nr_cgis.add(main_cgi)
        relation = relation_by_pair.get((main_cgi, around_cgi), {})
        network = normalize_network_type(
            row.get("around_cgi_network_type")
            or relation.get("around_cgi_network_type"),
        )
        if network == "5G" and around_cgi:
            nr_cgis.add(around_cgi)
        elif network == "4G" and around_cgi:
            lte_cgis.add(around_cgi)
    return nr_cgis, lte_cgis


def _site_type(
    network: NetworkType | None,
    cgi: str,
    site_type_by_cell: dict[tuple[NetworkType, str], str],
) -> str | None:
    if network is None:
        return None
    return site_type_by_cell.get((network, cgi))


def _max_available(*values: Any) -> float | None:
    present = [float(value) for value in values if value is not None]
    return max(present) if present else None


def _enrich_constriction_record(
    row: dict[str, Any],
    relation: dict[str, Any] | None,
    site_type_by_cell: dict[tuple[NetworkType, str], str],
) -> dict[str, Any]:
    main_cgi = str(row.get("cgi") or "")
    around_cgi = str(row.get("around_cgi") or "")
    network = normalize_network_type(
        row.get("around_cgi_network_type")
        or (relation or {}).get("around_cgi_network_type"),
    )
    main_site_type = row.get("site_type") or _site_type(
        "5G", main_cgi, site_type_by_cell,
    )
    around_site_type = _site_type(network, around_cgi, site_type_by_cell)
    distance = (relation or {}).get("distance")
    status = evaluate_neighbor_relation(
        main_site_type,
        around_site_type,
        distance,
    )
    return {
        **row,
        "around_network_type": network,
        "distance": distance,
        "main_site_type": main_site_type,
        "around_site_type": around_site_type,
        "relation_status": status,
        "main_prb_before": _max_available(
            row.get("self_prb_rate_ul_before"),
            row.get("self_prb_rate_dl_before"),
        ),
        "around_prb_before": _max_available(
            row.get("prb_rate_ul_before"),
            row.get("prb_rate_dl_before"),
        ),
        "around_prb_during": _max_available(
            row.get("prb_rate_ul"),
            row.get("prb_rate_dl"),
        ),
    }


def enrich_constriction_records(
    rows: list[dict[str, Any]],
    relation_by_pair: dict[tuple[str, str], dict[str, Any]],
    site_type_by_cell: dict[tuple[NetworkType, str], str],
) -> list[dict[str, Any]]:
    """补齐收缩证据，仅剔除关系信息明确违反规则的记录。"""
    result: list[dict[str, Any]] = []
    for row in rows:
        pair = (str(row.get("cgi") or ""), str(row.get("around_cgi") or ""))
        enriched = _enrich_constriction_record(
            row,
            relation_by_pair.get(pair),
            site_type_by_cell,
        )
        if enriched["relation_status"] in {"out_of_range", "site_type_mismatch"}:
            continue
        result.append(enriched)
    return result
