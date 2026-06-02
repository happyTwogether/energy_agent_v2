"""
统一日志模块。

提供格式化的日志输出，支持从 Settings 读取日志级别。
输出格式：[时间] [级别] [模块名] 消息内容
同时输出到控制台和日志文件（自动轮转）。
"""

import logging
import os
import sys
from logging.handlers import RotatingFileHandler

# Windows 控制台编码修复
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")

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
        formatter = logging.Formatter(
            fmt="[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

        # 控制台输出
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(formatter)
        root_logger.addHandler(console_handler)

        # 文件输出（自动轮转）
        log_dir = settings.log_dir
        os.makedirs(log_dir, exist_ok=True)
        file_handler = RotatingFileHandler(
            filename=os.path.join(log_dir, "energy_agent.log"),
            maxBytes=settings.log_file_max_mb * 1024 * 1024,
            backupCount=settings.log_file_backup_count,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)

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
