# Provider-Neutral LLM Smoke Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 保留生产默认模型 `qwen3.6_27b`，移除九天公网端点耦合，并让同一组 AgentScope 在线烟测可用于 DeepSeek 或生产内网 OpenAI-compatible 服务。

**Architecture:** `Settings` 只提供默认模型名，不提供任何供应商 Base URL；`build_chat_model()` 在创建 AgentScope credential 前验证运行时密钥和端点。在线测试只读取标准 `LLM_*` 配置，并通过 `RUN_LLM_SMOKE=1` 显式启用，因此公网 DeepSeek 和生产同构内网端点复用同一测试代码。

**Tech Stack:** Python 3.11, Pydantic Settings, AgentScope 2.0.5, unittest, OpenAI-compatible Chat Completions.

**Execution Status (2026-08-11):** Tasks 1-3 completed with verified RED → GREEN cycles. The offline gate discovered 50 tests: 46 passed and 4 live tests skipped. The same 4 tests passed against DeepSeek `deepseek-v4-pro` with thinking disabled through smoke-only `extra_body`; this does not replace the production-homologous intranet gate.

## Global Constraints

- 生产默认模型保持 `qwen3.6_27b`。
- 生产 `LLM_BASE_URL` 由部署环境注入内网独立推理服务地址，源码不得包含公网供应商默认端点。
- 公网 DeepSeek 只验证协议兼容，不作为生产 `qwen3.6_27b` 模型行为验收结论。
- API Key 只从父进程环境读取，不写入源码、Git diff、命令行参数、日志或测试产物。
- 所有生产行为按 RED → GREEN → REFACTOR 实现。

---

### Task 1: 强制运行时注入模型端点

**Files:**
- Modify: `tests/test_agent_model.py:15-52`
- Modify: `app/core/config.py:46-48`
- Modify: `app/agent/model.py:11-25`

**Interfaces:**
- Consumes: `Settings.llm_api_key: SecretStr`, `Settings.llm_base_url: str`, `Settings.default_model: str`
- Produces: `build_chat_model(settings: Settings | None = None, *, stream: bool = True) -> OpenAIChatModel`
- Error contract: 空 `LLM_API_KEY` 抛出包含 `LLM_API_KEY` 的 `AgentConfigurationError`；空白 `LLM_BASE_URL` 抛出包含 `LLM_BASE_URL` 的 `AgentConfigurationError`

- [x] **Step 1: 写入缺失 Base URL 的失败测试，并把现有模型测试改为供应商中立命名**

```python
class AgentModelFactoryTest(unittest.TestCase):
    """验证供应商中立的 OpenAI-compatible 模型配置边界。"""

    def test_builds_openai_compatible_model_from_runtime_settings(self) -> None:
        settings = Settings(
            _env_file=None,
            llm_api_key="test-token-not-secret",
            llm_base_url="https://llm-gateway.internal.example/v1/",
            default_model="qwen3.6_27b",
        )

        model = build_chat_model(settings)

        self.assertEqual("qwen3.6_27b", model.model)
        self.assertEqual(
            "https://llm-gateway.internal.example/v1",
            model.credential.base_url,
        )

    def test_rejects_missing_runtime_base_url(self) -> None:
        settings = Settings(
            _env_file=None,
            llm_api_key="test-token-not-secret",
            llm_base_url=" ",
        )

        with self.assertRaisesRegex(AgentConfigurationError, "LLM_BASE_URL"):
            build_chat_model(settings)
```

- [x] **Step 2: 运行目标测试并确认 RED**

Run:

```bash
.venv/bin/python -m unittest tests.test_agent_model.AgentModelFactoryTest.test_rejects_missing_runtime_base_url -v
```

Expected: FAIL，因为当前 model factory 未返回 `LLM_BASE_URL` 配置错误。

- [x] **Step 3: 写入最小生产实现**

`app/core/config.py`：

```python
llm_api_key: SecretStr = SecretStr("")
llm_base_url: str = ""
default_model: str = "qwen3.6_27b"
```

`app/agent/model.py`：

```python
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
```

- [x] **Step 4: 运行模型工厂测试并确认 GREEN**

Run:

```bash
.venv/bin/python -m unittest tests.test_agent_model -v
```

Expected: 3 tests PASS；模型名仍为 `qwen3.6_27b`，任意显式 OpenAI-compatible Base URL 均可注入。

### Task 2: 将在线烟测改为供应商中立

**Files:**
- Move: `tests/live/test_jiutian_smoke.py` → `tests/live/test_openai_compatible_smoke.py`
- Modify: `.env.example:9-11`
- Modify: `docs/superpowers/plans/2026-08-10-agentscope-full-migration.md:11-18,572-612`

**Interfaces:**
- Live gate: `RUN_LLM_SMOKE=1`
- Runtime inputs: `LLM_API_KEY`, `LLM_BASE_URL`, `DEFAULT_MODEL`, optional `LLM_SMOKE_EXTRA_BODY_JSON`
- Test class: `OpenAICompatibleSmokeTest(unittest.IsolatedAsyncioTestCase)`

- [x] **Step 1: 重命名在线测试及其 opt-in gate**

目标文件头部必须为：

```python
"""OpenAI-compatible 模型协议在线冒烟测试。"""

RUN_LIVE = os.getenv("RUN_LLM_SMOKE") == "1"


@unittest.skipUnless(RUN_LIVE, "设置 RUN_LLM_SMOKE=1 后执行在线模型测试")
class OpenAICompatibleSmokeTest(unittest.IsolatedAsyncioTestCase):
    """验证非流式、流式和 function calling 协议路径。"""
```

保留现有 4 个测试方法及其非流、流式、array/nullable Schema、并行工具调用断言，不添加任何供应商分支。

- [x] **Step 2: 更新环境示例和历史实施计划**

`.env.example` 的模型配置改为：

```dotenv
LLM_API_KEY=your_runtime_secret_here
LLM_BASE_URL=https://llm-gateway.internal.example/v1
DEFAULT_MODEL=qwen3.6_27b
```

历史实施计划注明：生产端点是内网独立部署；原九天 smoke 已被供应商中立测试取代；公网测试不能替代生产同构验证。

- [x] **Step 3: 验证默认离线执行安全跳过**

Run:

```bash
.venv/bin/python -m unittest tests.live.test_openai_compatible_smoke -v
```

Expected: 4 tests SKIPPED，提示设置 `RUN_LLM_SMOKE=1`；测试和运行时代码中无 `RUN_JIUTIAN_SMOKE`、九天域名或 `JiutianSmokeTest`。

- [x] **Step 4: 检查供应商中立边界**

Run:

```bash
rg -n "jiutian|RUN_JIUTIAN_SMOKE|JiutianSmokeTest" app tests .env.example
```

Expected: no matches.

### Task 3: 运行 DeepSeek 协议烟测与完整回归

**Files:**
- Modify after verification: `docs/superpowers/specs/2026-08-10-agentscope-full-migration-design.md:254-262`

**Interfaces:**
- Secret source: parent process `DEEPSEEK_API_KEY`
- Temporary smoke endpoint: `https://api.deepseek.com`
- Temporary smoke model: `deepseek-v4-pro`
- Production defaults remain: `DEFAULT_MODEL=qwen3.6_27b`, deployment-provided internal `LLM_BASE_URL`

- [x] **Step 1: 检查父进程是否安全配置 DeepSeek 密钥**

Run without printing the value:

```bash
test -n "$(launchctl getenv DEEPSEEK_API_KEY)"
```

Expected: exit 0. If absent, leave the 4 live tests skipped and report that only the external live gate remains pending; never reuse the Jiutian key.

- [x] **Step 2: 使用进程变量运行 DeepSeek 在线测试**

Run without printing the secret:

```bash
deepseek_key="$(launchctl getenv DEEPSEEK_API_KEY)"
RUN_LLM_SMOKE=1 \
LLM_API_KEY="$deepseek_key" \
LLM_BASE_URL=https://api.deepseek.com \
DEFAULT_MODEL=deepseek-v4-pro \
LLM_SMOKE_EXTRA_BODY_JSON='{"thinking":{"type":"disabled"}}' \
.venv/bin/python -m unittest tests.live.test_openai_compatible_smoke -v
unset deepseek_key
```

Expected: 4 tests PASS. A provider-specific failure is recorded as compatibility evidence and does not change production defaults.

- [x] **Step 3: 运行完整离线门禁**

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m compileall -q app main.py
.venv/bin/python -c "from main import app; print(app.title, app.version)"
.venv/bin/python -m pip check
git diff --check
rg -n "sk-[A-Za-z0-9_-]{16,}" --glob '!*.min.js' --glob '!.git/**' .
```

Expected: 50 tests discovered，46 offline tests PASS，4 opt-in live tests SKIPPED；compile/import、dependency check、whitespace check 和 secret scan 均通过。

- [x] **Step 4: 更新验证记录并提交本次纠偏**

验证记录只写实际执行结果，不把 DeepSeek 协议通过描述成生产模型验收。随后运行：

```bash
git add .env.example app/core/config.py app/agent/model.py tests/test_agent_model.py tests/live docs/superpowers
git commit -m "fix(model): 移除公网供应商默认端点"
```
