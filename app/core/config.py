"""
应用配置管理模块。

基于 pydantic-settings 从环境变量和 .env 文件加载配置，
使用单例模式确保全局复用同一个 Settings 实例。
"""

from functools import lru_cache
from urllib.parse import quote_plus

from pydantic import SecretStr, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


# 业务常量
TITLE_MAX_LENGTH: int = 20          # 对话标题最大长度
MAX_RETURN_ITEMS: int = 50          # 单次查询最大返回条数
DB_POOL_SIZE: int = 10              # 数据库连接池大小
DB_MAX_OVERFLOW: int = 20           # 数据库连接池溢出上限
MAX_HISTORY_MESSAGES: int = 20      # 对话历史最大保存条数
DATA_REFRESH_HOUR: int = 15         # 数据刷新截止时间（小时）

class Settings(BaseSettings):
    """应用全局配置，自动从环境变量和 .env 文件读取。"""

    db_host: str = "localhost"
    db_port: int = 5432
    db_user: str = "user"
    db_password: str = "password"
    db_name: str = "agent_db"
    db_schema: str = "public"  # PostgreSQL schema，默认 public
    db_schema_rule: str = "energysavingrules"  # 规则表所在 schema，默认 energysavingrules
    db_schema_agent: str = "jd_agent"  # 代理表所在 schema，默认 jd_agent

    @computed_field
    @property
    def database_url(self) -> str:
        """自动拼接 SQLAlchemy 异步数据库连接 URL。"""
        # 对密码进行 URL 编码，处理特殊字符如 @、#、: 等
        encoded_password = quote_plus(self.db_password)
        return (
            f"postgresql+asyncpg://{self.db_user}:{encoded_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )

    llm_api_key: SecretStr = SecretStr("")
    llm_base_url: str = ""
    default_model: str = "qwen3.6_27b"
    log_level: str = "INFO"
    llm_max_tokens: int = 4096  # LLM 最大输出 token 数
    llm_temperature: float = 0.3  # 生成温度，越低输出越确定
    llm_timeout_seconds: float = 60.0
    llm_context_size: int = 32768
    llm_max_retries: int = 2
    llm_parallel_tool_calls: bool = True
    agent_system_prompt: str = ""
    cors_origins: list[str] = ["*"]
    base_url: str = "http://10.159.55.28:9500/"  # 服务基础URL
    export_dir: str = "static/exports"  # Excel 导出文件目录

    # 完全离线业务数据自服务。默认关闭并使用独立只读连接。
    self_service_enabled: bool = False
    self_service_database_url: SecretStr = SecretStr("")
    self_service_query_timeout_ms: int = 10_000
    self_service_default_limit: int = 50
    self_service_max_limit: int = 500
    self_service_export_max_rows: int = 10_000
    self_service_default_days: int = 7
    self_service_max_days: int = 90
    self_service_catalog_candidates: int = 5

    # 消息存储接口配置
    message_store_url: str = "http://10.159.55.28:6039/sendMsg"
    message_store_enabled: bool = True
    message_store_timeout: int = 5
    agent_max_steps: int = 5  # AgentScope 最大推理-行动轮数
    agent_bot_id: int = 2064169133542531073  # 能效智能体 Dify Bot ID
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """获取全局唯一的 Settings 实例（单例模式）。"""
    return Settings()
