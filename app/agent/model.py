"""AgentScope OpenAI-compatible 模型工厂。"""

from agentscope.credential import OpenAICredential
from agentscope.model import OpenAIChatModel
from pydantic import SecretStr

from app.agent.errors import AgentConfigurationError
from app.core.config import Settings, get_settings


def build_chat_model(
    settings: Settings | None = None,
    *,
    stream: bool = True,
) -> OpenAIChatModel:
    """从运行时配置创建 OpenAI-compatible 对话模型。"""
    runtime_settings = settings or get_settings()
    api_key = runtime_settings.llm_api_key.get_secret_value()
    if not api_key.strip():
        raise AgentConfigurationError("缺少运行时配置 LLM_API_KEY")

    base_url = runtime_settings.llm_base_url.strip().rstrip("/")
    if not base_url:
        raise AgentConfigurationError("缺少运行时配置 LLM_BASE_URL")

    credential = OpenAICredential(
        api_key=SecretStr(api_key),
        base_url=base_url,
    )
    parameters = OpenAIChatModel.Parameters(
        max_tokens=runtime_settings.llm_max_tokens,
        temperature=runtime_settings.llm_temperature,
        parallel_tool_calls=runtime_settings.llm_parallel_tool_calls,
    )
    return OpenAIChatModel(
        credential=credential,
        model=runtime_settings.default_model,
        parameters=parameters,
        stream=stream,
        max_retries=runtime_settings.llm_max_retries,
        context_size=runtime_settings.llm_context_size,
        client_kwargs={"timeout": runtime_settings.llm_timeout_seconds},
    )
