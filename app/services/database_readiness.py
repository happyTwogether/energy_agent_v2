"""数据库依赖的有界检查、自动恢复与脱敏状态；不降级会话持久化。"""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
import os

from app.agent.errors import AgentConfigurationError
from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger("database_readiness")


def _sqlstate(error: Exception) -> str | None:
    visited = set()
    current = error
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        code = getattr(current, "sqlstate", None)
        if code:
            return code
        current = getattr(current, "orig", None) or current.__cause__
    return None


def _public_failure(error: Exception) -> tuple[str, str]:
    sqlstate = _sqlstate(error)
    if sqlstate in {"42P01", "42703"}:
        return "database_schema_unavailable", "会话表缺失或结构不匹配，请管理员核对建表脚本和目标数据库；服务会自动复查"
    if sqlstate == "42501":
        return "database_permission_denied", "会话表读取权限不足，请管理员核对授权；服务会自动复查"
    if sqlstate and sqlstate.startswith("28"):
        return "database_authentication_failed", "数据库身份验证失败，请管理员核对连接配置"
    if isinstance(error, TimeoutError):
        return "database_connection_timeout", "数据库连接超时，正在自动重连；恢复前暂不能查询或保存会话"
    if isinstance(error, OSError):
        return "database_unreachable", "数据库连接暂不可用，正在自动重连；恢复前暂不能查询或保存会话"
    if isinstance(error, AgentConfigurationError):
        return "database_schema_unavailable", "数据库会话存储检查未通过，请管理员核对表结构和读取权限；服务会自动复查"
    return "database_unavailable", "数据库检查未通过，正在自动重试；恢复前暂不能查询或保存会话"


class DatabaseReadiness:
    """HTTP 存活与数据库就绪分开；只有检查通过才放行业务请求。"""

    def __init__(
        self, checker: Callable[[], Awaitable[None]], *,
        on_ready: Callable[[], Awaitable[None]] | None = None,
        check_timeout: float = 5, retry_interval: float = 5, healthy_interval: float = 30,
    ):
        self.checker = checker
        self.on_ready = on_ready
        self.check_timeout = check_timeout
        self.retry_interval = retry_interval
        self.healthy_interval = healthy_interval
        self.ready = False
        self.ready_event = asyncio.Event()
        self.code = "database_initializing"
        self.message = "正在检查数据库连接和会话存储"
        self.attempts = 0
        self.checked_at = None
        self._prepared = False

    def snapshot(self) -> dict:
        return {
            "ready": self.ready, "code": self.code, "message": self.message,
            "attempts": self.attempts, "checked_at": self.checked_at,
        }

    async def check(self) -> bool:
        self.attempts += 1
        try:
            async with asyncio.timeout(self.check_timeout):
                await self.checker()
        except Exception as error:
            self.ready = False
            self.ready_event.clear()
            self.code, self.message = _public_failure(error)
            self._log_failure(error)
        else:
            was_ready = self.ready
            self.ready = True
            self.code, self.message = "ready", "数据库连接及会话表检查通过"
            self.ready_event.set()
            if not was_ready:
                logger.info("database_ready attempts=%s", self.attempts)
            await self._prepare_once()
        finally:
            self.checked_at = datetime.now(timezone.utc).isoformat()
        return self.ready

    async def _prepare_once(self) -> None:
        if self._prepared or self.on_ready is None:
            return
        try:
            async with asyncio.timeout(self.check_timeout):
                await self.on_ready()
        except Exception as error:
            logger.warning("数据库已就绪，自助目录预热未完成 error_type=%s", type(error).__name__)
        self._prepared = True

    def _log_failure(self, error: Exception) -> None:
        settings = get_settings()
        logger.warning(
            "database_not_ready code=%s attempt=%s target=%s:%s host_source=%s port_source=%s error_type=%s retry_seconds=%s",
            self.code, self.attempts, settings.db_host, settings.db_port,
            "process_env" if "DB_HOST" in os.environ else ".env/default",
            "process_env" if "DB_PORT" in os.environ else ".env/default",
            type(error).__name__, self.retry_interval,
        )

    async def run(self) -> None:
        while True:
            await asyncio.sleep(self.healthy_interval if self.ready else self.retry_interval)
            await self.check()
