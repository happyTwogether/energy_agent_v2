"""
Guardrails 安全模块。

提供 SQL 注入检测和敏感数据过滤。
"""

import re
from typing import Any

from app.core.logging import get_logger

logger = get_logger("guardrails")

# —— SQL 注入检测 ——
_FORBIDDEN_SQL_KEYWORDS = [
    "INSERT", "UPDATE", "DELETE", "DROP", "TRUNCATE",
    "ALTER", "CREATE", "EXEC", "EXECUTE", "UNION",
    "GRANT", "REVOKE", "LOAD", "INTO OUTFILE", "INTO DUMPFILE",
]

# —— 敏感数据字段 ——
_SENSITIVE_FIELD_PATTERNS = [
    re.compile(r"password", re.IGNORECASE),
    re.compile(r"secret", re.IGNORECASE),
    re.compile(r"token", re.IGNORECASE),
    re.compile(r"api[_-]?key", re.IGNORECASE),
]


def is_safe_sql(sql: str) -> bool:
    """检查 SQL 是否仅包含安全的 SELECT 操作。

    Args:
        sql: 待检查的 SQL 语句。

    Returns:
        True 如果 SQL 安全，False 如果包含危险操作。
    """
    upper = sql.upper().strip()
    for kw in _FORBIDDEN_SQL_KEYWORDS:
        # 使用单词边界匹配，避免 SELECT 中包含 "IN" 误判
        if re.search(r"\b" + re.escape(kw) + r"\b", upper):
            logger.warning("SQL 安全检查拦截: 包含关键词 %s", kw)
            return False
    return upper.startswith("SELECT") or upper.startswith("WITH")


def mask_sensitive_fields(data: dict[str, Any]) -> dict[str, Any]:
    """对字典中的敏感字段值进行脱敏。

    Args:
        data: 待脱敏的字典。

    Returns:
        脱敏后的字典（浅拷贝）。
    """
    if not isinstance(data, dict):
        return data

    masked = dict(data)
    for key in list(masked.keys()):
        for pattern in _SENSITIVE_FIELD_PATTERNS:
            if pattern.search(str(key)):
                masked[key] = "***"
                break
    return masked


def validate_tool_args(
    tool_name: str,
    args: dict[str, Any],
    required_fields: list[str],
) -> tuple[bool, str | None]:
    """校验工具调用的参数合法性。

    Args:
        tool_name: 工具名称。
        args: 工具参数。
        required_fields: 必填参数列表。

    Returns:
        (is_valid, error_message)
    """
    if not isinstance(args, dict) or not args:
        return False, f"工具 {tool_name} 的参数为空"

    missing = [f for f in required_fields if f not in args or args[f] in (None, "")]
    if missing:
        return False, f"工具 {tool_name} 缺少必填参数: {', '.join(missing)}"

    return True, None
