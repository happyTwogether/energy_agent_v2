"""共享 SQL 查询辅助函数。

消除 energy_saving_tool 和 batch_energy_tool 间的重复代码。
"""

import json as _json
from datetime import date, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.database import get_session_factory


def ensure_datetime(val: Any) -> datetime | None:
    """将 DB 返回的日期值归一化为 datetime，兼容 str/date/datetime。"""
    if val is None:
        return None
    if isinstance(val, datetime):
        return val
    if isinstance(val, date):
        return datetime.combine(val, datetime.min.time())
    if isinstance(val, str):
        for fmt in ["%Y-%m-%d", "%Y%m%d", "%Y-%m-%d %H:%M:%S"]:
            try:
                return datetime.strptime(val, fmt)
            except ValueError:
                continue
    return None


async def fetch_rows(sql, params):
    """使用独立 session 执行查询并返回 dict 列表。"""
    async with get_session_factory()() as sess:
        result = await sess.execute(sql, params)
        return [dict(r) for r in result.mappings().all()]


async def get_latest_date(db: AsyncSession, tables: list[str], date_col: str = "data_date") -> datetime | None:
    """查询多个表中指定日期列的最新值（UNION ALL + MAX）。

    Args:
        db: 数据库会话
        tables: 表名列表（含 schema 前缀）
        date_col: 日期列名
    """
    subqueries = " UNION ALL ".join(
        f"SELECT MAX({date_col}) AS {date_col} FROM {t}" for t in tables
    )
    sql = text(f"SELECT MAX({date_col}) FROM ({subqueries}) t")
    result = await db.execute(sql)
    row = result.scalar()
    if row is None:
        return None
    return ensure_datetime(row)


async def get_latest_date_standalone(tables: list[str], date_col: str = "data_date") -> datetime | None:
    """使用独立 session 查询多个表中指定日期列的最新值。

    适用于没有传入 db 参数的场景。
    """
    factory = get_session_factory()
    async with factory() as sess:
        return await get_latest_date(sess, tables, date_col)


def parse_list_field(raw: Any) -> list[Any]:
    """解析可能是 JSON 数组字符串、Python 列表或逗号分隔字符串的字段。

    统一处理三种格式：
    - Python list → 直接返回
    - JSON 数组字符串 → json.loads 解析
    - 逗号分隔字符串 → split 拆分
    """
    if not raw:
        return []
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str) and raw.strip():
        raw = raw.strip()
        if raw.startswith("["):
            try:
                parsed = _json.loads(raw)
                return parsed if isinstance(parsed, list) else []
            except _json.JSONDecodeError:
                return []
        return [item.strip() for item in raw.strip("[]").split(",") if item.strip()]
    return []


def success_response(data: dict[str, Any] | None = None, download_url: str | None = None) -> dict[str, Any]:
    """构建统一的成功响应字典。"""
    result: dict[str, Any] = {"success": True}
    if data:
        result.update(data)
    if download_url:
        result["download_url"] = download_url
    return result


def error_response(message: str, **extra: Any) -> dict[str, Any]:
    """构建统一的错误响应字典，支持额外字段。"""
    return {"success": False, "error": message, **extra}
