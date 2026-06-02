"""
应用配置管理模块。

基于 pydantic-settings 从环境变量和 .env 文件加载配置，
使用单例模式确保全局复用同一个 Settings 实例。
"""

from functools import lru_cache
from urllib.parse import quote_plus

from pydantic import computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


# 业务常量
MAX_RETURN_ITEMS: int = 50  # 单次查询最大返回条数

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
    log_dir: str = "logs"  # 日志文件目录，默认项目根目录下的 logs/
    log_file_max_mb: int = 50  # 单日志文件最大 MB
    log_file_backup_count: int = 5  # 保留的日志备份文件数
    llm_max_tokens: int = 4096  # LLM 最大输出 token 数
    llm_temperature: float = 0.3  # 生成温度，越低输出越确定
    llm_enable_thinking: bool = True  # Qwen3 深度思考开关，默认开启
    summary_model: str = ""  # 总结模型名，不填则复用 default_model
    summary_api_key: str = ""  # 总结模型 API Key，不填则复用 llm_api_key
    summary_base_url: str = ""  # 总结模型 Base URL，不填则复用 llm_base_url
    llm_tool_call_fallback_enabled: bool = True
    llm_tool_call_strict_mode: bool = True
    llm_tool_call_retry_on_suspected_draft: bool = True
    llm_tool_call_debug_log: bool = False
    agent_system_prompt: str = ""
    cors_origins: list[str] = ["*"]
    base_url: str = "http://10.159.55.28:9500/"  # 服务基础URL
    export_dir: str = "static/exports"  # Excel 导出文件目录
    cmdi_mcp_ai_url: str = "http://10.159.55.28:18201/cmdi-mcp-ai"  # CGI 小区查询服务地址

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
