"""
数据库服务模块。

管理 async SQLAlchemy 引擎与会话，定义 ORM 模型，
提供 FastAPI 依赖注入的 get_db() 函数。
"""

import uuid
from datetime import datetime, timezone
from typing import AsyncGenerator

from sqlalchemy import DateTime, Text, Uuid
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger("database")

_engine = None
_async_session_factory = None


def get_engine() -> AsyncEngine:
    """获取异步数据库引擎单例（懒加载，首次调用时才创建）。"""
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_async_engine(
            settings.database_url,
            echo=False,
            pool_size=10,
            max_overflow=20,
        )
        logger.info("数据库引擎已创建: %s", settings.db_host)
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """获取异步会话工厂单例（懒加载）。"""
    global _async_session_factory
    if _async_session_factory is None:
        _async_session_factory = async_sessionmaker(
            bind=get_engine(),
            class_=AsyncSession,
            expire_on_commit=False,
        )
    return _async_session_factory


class Base(DeclarativeBase):
    """SQLAlchemy ORM 基类。"""
    pass


class AgentSession(Base):
    """Agent 会话 ORM 模型，用于持久化用户请求与响应。"""

    __tablename__ = "agent_sessions"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        primary_key=True,
        default=uuid.uuid4,
    )
    request: Mapped[str] = mapped_column(Text, nullable=False)
    response: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )


async def init_db() -> None:
    """初始化数据库：自动创建所有 ORM 模型对应的表。

    在应用启动时（lifespan）调用，确保首次运行时表结构自动就绪。
    """
    try:
        engine = get_engine()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("数据库表初始化完成")
    except Exception as exc:
        logger.error("数据库表初始化失败: %s", exc, exc_info=True)
        raise


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI 依赖注入：获取异步数据库会话。

    使用 async with 管理会话生命周期，确保会话在请求结束后正确关闭。

    Yields:
        AsyncSession: 异步数据库会话实例。
    """
    async with get_session_factory()() as session:
        try:
            yield session
        except Exception as exc:
            logger.error("数据库会话异常: %s", exc, exc_info=True)
            await session.rollback()
            raise
