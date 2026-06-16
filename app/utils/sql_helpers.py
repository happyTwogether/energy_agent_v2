"""共享 SQL 查询辅助函数。

消除 energy_saving_tool 和 batch_energy_tool 间的重复代码。
"""

from datetime import date, datetime
from typing import Any

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
