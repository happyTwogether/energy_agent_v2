"""
应用配置管理模块。

基于 pydantic-settings 从环境变量和 .env 文件加载配置，
使用单例模式确保全局复用同一个 Settings 实例。
"""

from functools import lru_cache

from pydantic import computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """应用全局配置，自动从环境变量和 .env 文件读取。"""

    db_host: str = "localhost"
    db_port: int = 5432
    db_user: str = "user"
    db_password: str = "password"
    db_name: str = "agent_db"
    db_schema: str = "public"  # PostgreSQL schema，默认 public
    db_schema_rule: str = "energysavingrules"  # 规则表所在 schema，默认 energysavingrules

    @computed_field
    @property
    def database_url(self) -> str:
        """自动拼接 SQLAlchemy 异步数据库连接 URL。"""
        return (
            f"postgresql+asyncpg://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )

    llm_api_key: str = "your_api_key_here"
    llm_base_url: str = ""
    default_model: str = "gpt-4o-mini"
    log_level: str = "INFO"
    agent_max_steps: int = 5
    agent_system_prompt: str = (
        "你是一个智能助手，可以通过调用工具来帮助用户完成任务。"
        "请根据用户的问题，判断是否需要使用工具，并给出清晰的回答。"
    )
    cors_origins: list[str] = ["*"]

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
