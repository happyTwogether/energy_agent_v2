"""
统一日志模块。

提供格式化的日志输出，支持从 Settings 读取日志级别。
输出格式：[时间] [级别] [模块名] 消息内容
"""

import logging
import re
import sys

# Windows 控制台编码修复
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")

_initialized: bool = False

_SENSITIVE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"\b([a-z][a-z0-9+.-]*://)([^/\s:@]+):([^@\s/]+)@",
            re.IGNORECASE,
        ),
        r"\1***:***@",
    ),
    (
        re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE),
        "Bearer ***",
    ),
    (
        re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
        "sk-***",
    ),
    (
        re.compile(
            r"\b(api[_-]?key|password|token|authorization)\b"
            r"(\s*[:=]\s*)([^\s,;]+)",
            re.IGNORECASE,
        ),
        r"\1\2***",
    ),
)


def redact_sensitive_data(value: str) -> str:
    """脱敏常见凭证、Authorization 和 URL 用户信息。"""
    redacted = value
    for pattern, replacement in _SENSITIVE_PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    return redacted


class SensitiveDataFormatter(logging.Formatter):
    """在最终日志文本上脱敏，包括异常 traceback。"""

    def format(self, record: logging.LogRecord) -> str:
        formatted = super().format(record)
        if record.exc_text:
            record.exc_text = redact_sensitive_data(record.exc_text)
        return redact_sensitive_data(formatted)


def _init_root_logger() -> None:
    """初始化根 logger 配置，仅执行一次。"""
    global _initialized
    if _initialized:
        return

    from app.core.config import get_settings

    settings = get_settings()

    root_logger = logging.getLogger("agent")
    root_logger.setLevel(getattr(logging, settings.log_level.upper(), logging.INFO))

    if not root_logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        formatter = SensitiveDataFormatter(
            fmt="[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        handler.setFormatter(formatter)
        root_logger.addHandler(handler)

    _initialized = True


def get_logger(name: str) -> logging.Logger:
    """获取指定模块名的 logger 实例。

    Args:
        name: 模块名称，将作为 'agent.<name>' 的子 logger。

    Returns:
        配置好的 logging.Logger 实例。
    """
    _init_root_logger()
    return logging.getLogger(f"agent.{name}")
