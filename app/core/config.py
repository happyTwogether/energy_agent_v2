"""
应用配置管理模块。

基于 pydantic-settings 从环境变量和 .env 文件加载配置，
使用单例模式确保全局复用同一个 Settings 实例。
"""

from functools import lru_cache
from urllib.parse import quote_plus

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

    llm_api_key: str = ""
    llm_base_url: str = ""
    default_model: str = ""
    log_level: str = "INFO"
    agent_max_steps: int = 5
    llm_max_tokens: int = 8192  # LLM 最大输出 token 数，防止长报告被截断
    agent_system_prompt: str = (
        "你是一个智能助手，可以通过调用工具来帮助用户完成任务。\n\n"
        "## 工具调用规则\n"
        "当需要使用工具时，请严格按以下格式输出（JSON 格式）：\n"
        '<tool>{"name": "工具名", "arguments": {"参数1": "值1", "参数2": "值2"}}</tool>\n\n'
        "示例：查询长沙市的指标\n"
        '<tool>{"name": "query_metric", "arguments": {"template_key": "lte_summary", "dist_name": "长沙市"}}</tool>\n\n'
        "请根据用户的问题，判断是否需要使用工具，并给出清晰的回答。"
    )
    cors_origins: list[str] = ["*"]
    base_url: str = ""  # 服务基础URL，用于生成完整下载链接，如 http://localhost:8000 或 http://10.154.14.93:8080

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
