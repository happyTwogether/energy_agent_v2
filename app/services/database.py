"""
数据库服务模块。

管理 async SQLAlchemy 引擎与会话，定义 ORM 模型，
提供 FastAPI 依赖注入的 get_db() 函数。
"""

import uuid
from datetime import datetime, timezone
from typing import AsyncGenerator

from sqlalchemy import DateTime, JSON, String, Text, Uuid, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
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
    __table_args__ = {"schema": "jd_agent"}

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


class Conversation(Base):
    """会话持久化模型，存储 Dify/OpenAI 格式的对话历史。"""

    __tablename__ = "agent_conversations"
    __table_args__ = {"schema": "jd_agent"}

    conversation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(128), index=True, default="anonymous")
    title: Mapped[str] = mapped_column(String(200), default="新对话")
    messages: Mapped[list] = mapped_column(JSON, default=list)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )


async def load_conversation(db: AsyncSession, conversation_id: str) -> list[dict]:
    """加载会话历史，返回消息列表。不存在则返回空列表。"""
    result = await db.execute(
        select(Conversation).where(Conversation.conversation_id == conversation_id)
    )
    row = result.scalar_one_or_none()
    return list(row.messages) if row else []


async def save_conversation(
    db: AsyncSession,
    conversation_id: str,
    user_id: str,
    new_messages: list[dict],
    title: str | None = None,
    replace: bool = False,
) -> None:
    """保存会话历史（upsert）。replace=True 时直接覆盖，否则追加。自动截断到最近 20 条。"""
    result = await db.execute(
        select(Conversation).where(Conversation.conversation_id == conversation_id)
    )
    row = result.scalar_one_or_none()
    existing_messages = [] if replace else (list(row.messages) if row else [])
    merged = existing_messages + new_messages
    if len(merged) > 20:
        merged = merged[-20:]

    now = datetime.now(timezone.utc)
    final_title = (title or "新对话") if not row else (row.title or "新对话")

    stmt = pg_insert(Conversation).values(
        conversation_id=conversation_id,
        user_id=user_id,
        title=final_title,
        messages=merged,
        updated_at=now,
        created_at=now if not row else row.created_at,
    ).on_conflict_do_update(
        index_elements=["conversation_id"],
        set_={"messages": merged, "title": final_title, "updated_at": now},
    )
    await db.execute(stmt)
    await db.commit()


async def get_conversation_title(db: AsyncSession, conversation_id: str) -> str:
    """获取会话标题。不存在则返回默认值。"""
    result = await db.execute(
        select(Conversation.title).where(Conversation.conversation_id == conversation_id)
    )
    title = result.scalar_one_or_none()
    return title or "新对话"


async def list_conversations(db: AsyncSession, user_id: str) -> list[dict]:
    """获取用户的会话列表，按更新时间倒序。"""
    result = await db.execute(
        select(Conversation)
        .where(Conversation.user_id == user_id)
        .order_by(Conversation.updated_at.desc())
    )
    rows = result.scalars().all()
    return [
        {
            "conversation_id": row.conversation_id,
            "title": row.title,
            "updated_at": row.updated_at.isoformat() if row.updated_at else "",
            "message_count": len(row.messages) if row.messages else 0,
        }
        for row in rows
    ]


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
        logger.warning("数据库表自动创建失败（可能是权限不足），请手动执行 DDL: %s", exc)


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
