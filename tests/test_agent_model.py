"""AgentScope 模型工厂测试。"""

import unittest

from app.core.config import Settings

try:
    from app.agent.errors import AgentConfigurationError
    from app.agent.model import build_chat_model
except ModuleNotFoundError:
    AgentConfigurationError = RuntimeError
    build_chat_model = None


class AgentModelFactoryTest(unittest.TestCase):
    """验证供应商中立的 OpenAI-compatible 模型配置边界。"""

    def test_builds_openai_compatible_model_from_runtime_settings(self) -> None:
        self.assertIsNotNone(build_chat_model, "AgentScope 模型工厂尚未实现")
        settings = Settings(
            _env_file=None,
            llm_api_key="test-token-not-secret",
            llm_base_url="https://llm-gateway.internal.example/v1/",
            default_model="qwen3.6_27b",
            llm_max_tokens=2048,
            llm_temperature=0.2,
            llm_timeout_seconds=45.0,
            llm_context_size=32768,
            llm_max_retries=1,
            llm_parallel_tool_calls=False,
        )

        model = build_chat_model(settings)  # type: ignore[misc]

        self.assertEqual("qwen3.6_27b", model.model)
        self.assertEqual(
            "https://llm-gateway.internal.example/v1",
            model.credential.base_url,
        )
        self.assertEqual(2048, model.parameters.max_tokens)
        self.assertEqual(0.2, model.parameters.temperature)
        self.assertFalse(model.parameters.parallel_tool_calls)
        self.assertEqual(32768, model.context_size)
        self.assertEqual(1, model.max_retries)
        self.assertEqual(45.0, model.client_kwargs["timeout"])
        self.assertNotIn("test-token-not-secret", repr(model.credential))
        self.assertNotIn("test-token-not-secret", repr(settings))

    def test_rejects_missing_runtime_api_key(self) -> None:
        self.assertIsNotNone(build_chat_model, "AgentScope 模型工厂尚未实现")
        settings = Settings(_env_file=None, llm_api_key="")

        with self.assertRaisesRegex(AgentConfigurationError, "LLM_API_KEY"):
            build_chat_model(settings)  # type: ignore[misc]

    def test_rejects_missing_runtime_base_url(self) -> None:
        self.assertIsNotNone(build_chat_model, "AgentScope 模型工厂尚未实现")
        settings = Settings(
            _env_file=None,
            llm_api_key="test-token-not-secret",
            llm_base_url=" ",
        )

        with self.assertRaisesRegex(AgentConfigurationError, "LLM_BASE_URL"):
            build_chat_model(settings)  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
