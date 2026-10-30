"""
数据库服务模块。

管理 async SQLAlchemy 引擎与会话，定义 ORM 模型。
"""

from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer, JSON, String, delete, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.agent.errors import AgentConfigurationError, ConversationConflict, ConversationNotFound
from app.core.config import DB_MAX_OVERFLOW, DB_POOL_SIZE, get_settings
from app.core.logging import get_logger

logger = get_logger("database")

_engine = None
_async_session_factory = None
_self_service_engine: AsyncEngine | None = None
_self_service_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    """获取异步数据库引擎单例（懒加载，首次调用时才创建）。"""
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_async_engine(
            settings.database_url,
            echo=False,
            pool_size=DB_POOL_SIZE,
            max_overflow=DB_MAX_OVERFLOW,
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


def get_self_service_session_factory() -> async_sessionmaker[AsyncSession]:
    """获取业务数据自服务的独立只读会话工厂。"""
    global _self_service_engine, _self_service_session_factory
    if _self_service_session_factory is not None:
        return _self_service_session_factory

    settings = get_settings()
    if not settings.self_service_enabled:
        raise AgentConfigurationError(
            "通用数据查询未启用：请设置 SELF_SERVICE_ENABLED=true",
        )
    _self_service_engine = create_async_engine(
        settings.database_url,
        echo=False,
        pool_size=DB_POOL_SIZE,
        max_overflow=DB_MAX_OVERFLOW,
        connect_args={
            "server_settings": {"default_transaction_read_only": "on"},
        },
    )
    _self_service_session_factory = async_sessionmaker(
        bind=_self_service_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    return _self_service_session_factory


async def dispose_self_service_engine() -> None:
    """关闭自助查询引擎并清除进程级缓存。"""
    global _self_service_engine, _self_service_session_factory
    if _self_service_engine is not None:
        await _self_service_engine.dispose()
    _self_service_engine = None
    _self_service_session_factory = None


class Base(DeclarativeBase):
    """SQLAlchemy ORM 基类。"""
    pass


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


class ConversationAgentState(Base):
    """框架工作状态与展示原文独立，version 阻止并发旧分支覆盖。"""

    __tablename__ = "agent_session_states"
    __table_args__ = {"schema": "jd_agent"}

    conversation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(128), index=True)
    state: Mapped[dict] = mapped_column(JSON)
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


async def load_conversation(db: AsyncSession, conversation_id: str, user_id: str, *, allow_missing: bool = False) -> list[dict]:
    """加载本用户的原文；未知/其他用户的已有会话统一拒绝。"""
    result = await db.execute(
        select(Conversation).where(Conversation.conversation_id == conversation_id, Conversation.user_id == user_id)
    )
    row = result.scalar_one_or_none()
    if row is None and not allow_missing:
        raise ConversationNotFound()
    return list(row.messages) if row else []


async def load_agent_state(db: AsyncSession, conversation_id: str, user_id: str) -> dict | None:
    result = await db.execute(select(ConversationAgentState).where(
        ConversationAgentState.conversation_id == conversation_id, ConversationAgentState.user_id == user_id,
    ).execution_options(populate_existing=True))
    row = result.scalar_one_or_none()
    return {"version": row.version, "state": row.state} if row else None


async def save_conversation(
    db: AsyncSession,
    conversation_id: str,
    user_id: str,
    new_messages: list[dict],
    title: str | None = None,
    replace: bool = False,
    agent_state: dict | None = None,
    expected_state_version: int = 0,
    allow_create: bool = True,
) -> None:
    """原文与框架状态同一事务保存；不按模型窗口删除原文。"""
    result = await db.execute(
        select(Conversation).where(Conversation.conversation_id == conversation_id).with_for_update().execution_options(populate_existing=True)
    )
    row = result.scalar_one_or_none()
    if row is None and (not allow_create or expected_state_version > 0):
        raise ConversationNotFound()
    if row is not None and row.user_id != user_id:
        raise ConversationNotFound()
    existing_messages = [] if replace else (list(row.messages) if row else [])
    merged = existing_messages + new_messages

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
        where=Conversation.user_id == user_id,
    ).returning(Conversation.conversation_id)
    try:
        if (await db.execute(stmt)).scalar_one_or_none() is None:
            raise ConversationNotFound()
        if agent_state is not None:
            state_stmt = pg_insert(ConversationAgentState).values(
                conversation_id=conversation_id, user_id=user_id, state=agent_state,
                version=expected_state_version + 1, updated_at=now,
            ).on_conflict_do_update(
                index_elements=["conversation_id"],
                set_={"state": agent_state, "version": expected_state_version + 1, "updated_at": now},
                where=(ConversationAgentState.user_id == user_id) & (ConversationAgentState.version == expected_state_version),
            ).returning(ConversationAgentState.version)
            if (await db.execute(state_stmt)).scalar_one_or_none() is None:
                raise ConversationConflict()
        await db.commit()
    except Exception:
        await db.rollback()
        raise


async def get_conversation_title(db: AsyncSession, conversation_id: str, user_id: str) -> str:
    """获取会话标题。不存在则返回默认值。"""
    result = await db.execute(
        select(Conversation.title).where(Conversation.conversation_id == conversation_id, Conversation.user_id == user_id)
    )
    title = result.scalar_one_or_none()
    if title is None:
        raise ConversationNotFound()
    return title or "新对话"


async def delete_owned_conversation(db: AsyncSession, conversation_id: str, user_id: str) -> None:
    """同一事务只删除本用户的问答和框架状态。"""
    try:
        # 与保存保持相同的锁顺序：先会话，再状态，避免删除/保存交叉死锁。
        owned = await db.execute(select(Conversation.conversation_id).where(
            Conversation.conversation_id == conversation_id, Conversation.user_id == user_id,
        ).with_for_update())
        if owned.scalar_one_or_none() is None:
            raise ConversationNotFound()
        await db.execute(delete(ConversationAgentState).where(
            ConversationAgentState.conversation_id == conversation_id, ConversationAgentState.user_id == user_id,
        ))
        result = await db.execute(delete(Conversation).where(Conversation.conversation_id == conversation_id, Conversation.user_id == user_id))
        if not result.rowcount:
            raise ConversationNotFound()
        await db.commit()
    except Exception:
        await db.rollback()
        raise


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
    """生产默认仅检查必要表；开发环境可显式开启自动建表。"""
    if get_settings().db_auto_create_tables:
        async with get_engine().begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        logger.info("数据库自动建表完成")
    else:
        logger.info("数据库自动建表已关闭，仅检查必要会话表")
    await check_session_storage()


async def check_session_storage() -> None:
    """复查连接及核心会话表，不重复执行开发环境的建表。"""
    async with get_engine().connect() as connection:
        await verify_session_storage(connection)


async def verify_session_storage(db) -> None:
    """只读取表结构，不读取会话内容，不写入生产数据。"""
    tables = (
        ("jd_agent.agent_conversations", select(Conversation.conversation_id, Conversation.user_id, Conversation.messages).limit(0)),
        ("jd_agent.agent_session_states", select(ConversationAgentState.conversation_id, ConversationAgentState.user_id, ConversationAgentState.state, ConversationAgentState.version).limit(0)),
    )
    for table_name, statement in tables:
        try:
            await db.execute(statement)
        except Exception as exc:
            origin = getattr(exc, "orig", exc)
            sqlstate = getattr(origin, "sqlstate", None)
            if sqlstate == "42P01":
                if table_name == "jd_agent.agent_session_states":
                    message = f"缺少会话表 {table_name}，请由管理员执行 db/agent_session_state.sql；应用账号不需要建表权限"
                else:
                    message = f"缺少原有问答表 {table_name}，请核对 DB_NAME 与已有数据库结构；新状态表脚本不会重建原有问答表"
            elif sqlstate == "42501":
                message = f"无权读取会话表 {table_name}，请检查 schema USAGE 和表 SELECT 权限；本次尚未检查写入"
            elif sqlstate == "42703":
                message = f"会话表 {table_name} 的字段与当前版本不一致，请核对数据库结构"
            else:
                message = f"会话表 {table_name} 检查失败，请检查连接、结构和读取权限"
            raise AgentConfigurationError(message) from exc


async def inspect_session_permissions(db) -> list[dict]:
    """只读查询当前账号的会话表权限，不创建表，不试写业务数据。"""
    statement = text("""
        SELECT current_user AS db_user,
               current_database() AS db_name,
               to_regclass(:table_name) IS NOT NULL AS table_exists,
               COALESCE(has_schema_privilege(current_user, to_regnamespace('jd_agent'), 'USAGE'), false) AS schema_usage,
               COALESCE(has_table_privilege(current_user, to_regclass(:table_name), 'SELECT'), false) AS can_select,
               COALESCE(has_table_privilege(current_user, to_regclass(:table_name), 'INSERT'), false) AS can_insert,
               COALESCE(has_table_privilege(current_user, to_regclass(:table_name), 'UPDATE'), false) AS can_update,
               COALESCE(has_table_privilege(current_user, to_regclass(:table_name), 'DELETE'), false) AS can_delete
    """)
    checks = []
    for table_name in ("jd_agent.agent_conversations", "jd_agent.agent_session_states"):
        result = await db.execute(statement, {"table_name": table_name})
        checks.append({"table": table_name, **dict(result.mappings().one())})
    return checks
