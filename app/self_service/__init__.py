"""完全离线业务数据自服务的进程级目录生命周期。"""

from functools import lru_cache

from app.self_service.catalog import BusinessCatalogStore
from app.services.database import get_self_service_session_factory


@lru_cache(maxsize=1)
def get_business_catalog_store() -> BusinessCatalogStore:
    return BusinessCatalogStore()


async def warm_business_catalog() -> None:
    """应用启动时用独立只读会话加载一次授权目录。"""
    async with get_self_service_session_factory()() as db:
        await get_business_catalog_store().get_or_load(db)


__all__ = [
    "BusinessCatalogStore",
    "get_business_catalog_store",
    "warm_business_catalog",
]
