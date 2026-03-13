"""
统一日志模块。

提供格式化的日志输出，支持从 Settings 读取日志级别。
输出格式：[时间] [级别] [模块名] 消息内容
"""

import logging
import sys

_initialized: bool = False


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
        formatter = logging.Formatter(
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
