# AgentScope Full Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用 AgentScope 2.0.5 全量替换自研 Agent/LiteLLM/ToolRegistry 内核，在不改变 Dify 外部协议和能效业务行为的前提下运行 `qwen3.6_27b`。

**Architecture:** FastAPI 和现有 PostgreSQL 会话库继续是服务边界；每个请求从数据库历史构造独立 `AgentState`，由 AgentScope `Agent.reply_stream()` 调用 `OpenAIChatModel` 和 `Toolkit`。项目适配层将 9 个业务函数包装为 `ToolBase`，将 `AgentEvent` 统一转成 streaming/blocking 共用的 Dify 语义。

**Tech Stack:** Python 3.11, AgentScope 2.0.5, FastAPI 0.115, Pydantic 2.11, SQLAlchemy 2.0 async, unittest, PostgreSQL, OpenAI-compatible Chat Completions.

**Execution Status (2026-08-11):** Tasks 1-8 implementation and development verification are complete. Historical per-task commit commands were consolidated into one atomic cutover commit because the migration deletes the legacy runtime while adding its AgentScope replacement, and the working tree also contains the approved pre-migration cell-lookup baseline. Online smoke is provider-neutral; a public provider validates the protocol, while the release gate remains a production-homologous intranet `qwen3.6_27b` run.

**Verification snapshot:** The provider-neutral configuration gate discovered 50 tests: 46 offline tests passed and 4 opt-in live tests safely skipped; compile/import, 98-package dependency compatibility, secret scan, provider-coupling scan, and `git diff --check` passed. The same 4 live tests passed against DeepSeek `deepseek-v4-pro` with its default thinking explicitly disabled through smoke-only `extra_body`; this is protocol evidence, not the production model gate. Earlier Docker build, container startup, and container-internal `/api/v1/health` passed; a final image rebuild after the last logging/empty-answer hardening remains pending because Docker Desktop stopped responding.

## Global Constraints

- 精确锁定 `agentscope==2.0.5`，不使用 2.0.6 dev API。
- 生产默认模型为 `qwen3.6_27b`，base URL 和 API Key 分别从 `LLM_BASE_URL`、`LLM_API_KEY` 运行时注入；源码不提供公网供应商默认端点。
- 禁止在代码、Git diff、命令行、日志和测试产物中写入真实密钥。
- 保留 `/api/v1/chat/stream`、`/api/v1/chat-messages`、会话管理和下载接口。
- 保留 Dify `message` / `agent_thought` / `message_end` / `error` 字段语义。
- 保留 9 个工具的名称、description、JSON Schema、业务结果与 `report_content` 直出。
- 每个工具调用创建独立 `AsyncSession`，不在 Agent/Toolkit 对象上持有 session。
- 现有工作区的小区名称解析改动是迁移基线，不回滚、不覆盖。
- 项目 Python 已经统一使用 `snake_case` 模块和标识符；新代码继续该本地惯例。
- 所有生产行为按 RED → GREEN → REFACTOR 实现，每个新行为必须先观察到相应失败测试。

---

### Task 1: 固化基线并建立 AgentScope 模型工厂

**Files:**
- Create: `app/agent/__init__.py`
- Create: `app/agent/errors.py`
- Create: `app/agent/model.py`
- Create: `tests/test_agent_model.py`
- Modify: `app/core/config.py`
- Modify: `.env.example`
- Modify: `requirements.txt`

**Interfaces:**
- Produces: `AgentConfigurationError`
- Produces: `build_chat_model(settings: Settings | None = None, *, stream: bool = True) -> OpenAIChatModel`
- Consumes later: `Settings.llm_timeout_seconds`, `Settings.llm_context_size`, `Settings.llm_max_retries`, `Settings.llm_parallel_tool_calls`

- [ ] **Step 1: Verify the pre-migration baseline**

Run:

```bash
/Users/schunm/.local/bin/python3.11 -m unittest discover -s tests -v
```

Expected: 11 existing cell lookup/resolver tests pass before migration code changes.

- [ ] **Step 2: Pin the dependency and create an isolated Python 3.11 environment**

Change `requirements.txt` from `litellm>=1.81.0` to `agentscope==2.0.5`, then run:

```bash
/Users/schunm/.local/bin/uv venv --python /Users/schunm/.local/bin/python3.11 .venv
/Users/schunm/.local/bin/uv pip install --python .venv/bin/python -r requirements.txt
```

Expected: `.venv/bin/python -c "import agentscope; print(agentscope.__version__)"` prints `2.0.5`.

- [ ] **Step 3: Write the failing model factory tests**

`tests/test_agent_model.py` must exercise the real AgentScope model object:

```python
import unittest

from app.agent.errors import AgentConfigurationError
from app.agent.model import build_chat_model
from app.core.config import Settings


class AgentModelFactoryTest(unittest.TestCase):
    def test_builds_openai_compatible_model_from_runtime_settings(self) -> None:
        settings = Settings(
            _env_file=None,
            llm_api_key="test-token-not-secret",
            llm_base_url="https://example.invalid/v3",
            default_model="qwen3.6_27b",
            llm_max_tokens=2048,
            llm_temperature=0.2,
            llm_timeout_seconds=45.0,
            llm_context_size=32768,
            llm_max_retries=1,
        )

        model = build_chat_model(settings)

        self.assertEqual("qwen3.6_27b", model.model)
        self.assertEqual("https://example.invalid/v3", model.credential.base_url)
        self.assertEqual(2048, model.parameters.max_tokens)
        self.assertEqual(0.2, model.parameters.temperature)
        self.assertEqual(32768, model.context_size)
        self.assertEqual(1, model.max_retries)
        self.assertNotIn("test-token-not-secret", repr(model.credential))

    def test_rejects_missing_runtime_api_key(self) -> None:
        settings = Settings(_env_file=None, llm_api_key="")

        with self.assertRaisesRegex(AgentConfigurationError, "LLM_API_KEY"):
            build_chat_model(settings)
```

- [ ] **Step 4: Run the tests and verify RED**

Run:

```bash
.venv/bin/python -m unittest tests.test_agent_model -v
```

Expected: FAIL because `app.agent.model` and `AgentConfigurationError` do not exist.

- [ ] **Step 5: Implement the minimal model factory**

`app/agent/model.py` constructs:

```python
credential = OpenAICredential(
    api_key=SecretStr(settings.llm_api_key),
    base_url=settings.llm_base_url.rstrip("/"),
)
parameters = OpenAIChatModel.Parameters(
    max_tokens=settings.llm_max_tokens,
    temperature=settings.llm_temperature,
    parallel_tool_calls=settings.llm_parallel_tool_calls,
)
return OpenAIChatModel(
    credential=credential,
    model=settings.default_model,
    parameters=parameters,
    stream=stream,
    max_retries=settings.llm_max_retries,
    context_size=settings.llm_context_size,
    client_kwargs={"timeout": settings.llm_timeout_seconds},
)
```

Set `DEFAULT_MODEL=qwen3.6_27b`, leave the application default `LLM_BASE_URL` empty, validate both runtime credential fields, and keep `.env.example` on reserved internal example values.

- [ ] **Step 6: Run GREEN and regression tests**

Run:

```bash
.venv/bin/python -m unittest tests.test_agent_model -v
.venv/bin/python -m unittest discover -s tests -v
```

Expected: model factory tests and the 11 baseline tests pass.

- [ ] **Step 7: Commit the model boundary**

```bash
git add requirements.txt .env.example app/core/config.py app/agent/__init__.py app/agent/errors.py app/agent/model.py tests/test_agent_model.py
git commit -m "feat(model): 接入 AgentScope 对话模型"
```

### Task 2: 将持久化对话转为 AgentScope 消息

**Files:**
- Create: `app/agent/messages.py`
- Create: `tests/test_agent_messages.py`

**Interfaces:**
- Produces: `ParsedRequestMessages(history: list[Msg], current_user: Msg, user_context: str | None)`
- Produces: `parse_request_messages(messages: list[dict[str, Any]]) -> ParsedRequestMessages`
- Consumes later: `build_energy_agent()` and `stream_agent_events()`

- [ ] **Step 1: Write the failing conversion tests**

Use literal input and assert that history is not duplicated:

```python
class AgentMessagesTest(unittest.TestCase):
    def test_separates_history_current_user_and_system_context(self) -> None:
        parsed = parse_request_messages([
            {"role": "system", "content": "用户上下文信息：{\"province\": \"湖南\"}"},
            {"role": "user", "content": "上一个问题"},
            {"role": "assistant", "content": "上一个回答"},
            {"role": "user", "content": "查询长沙5G能耗"},
        ])

        self.assertEqual(2, len(parsed.history))
        self.assertEqual("查询长沙5G能耗", parsed.current_user.get_text_content())
        self.assertEqual("用户上下文信息：{\"province\": \"湖南\"}", parsed.user_context)
        self.assertEqual(["user", "assistant"], [msg.role for msg in parsed.history])

    def test_rejects_request_without_final_user_message(self) -> None:
        with self.assertRaisesRegex(ValueError, "最后一条.*user"):
            parse_request_messages([{"role": "assistant", "content": "answer"}])
```

- [ ] **Step 2: Run the tests and verify RED**

Run `.venv/bin/python -m unittest tests.test_agent_messages -v`.

Expected: FAIL because `parse_request_messages` is absent.

- [ ] **Step 3: Implement strict role conversion**

Convert only plain-text `user` and `assistant` history using `UserMsg` and `AssistantMsg`. Treat the first system message as controlled context, require the final non-system message to be `user`, and exclude that final user message from `history`.

- [ ] **Step 4: Run GREEN and commit**

```bash
.venv/bin/python -m unittest tests.test_agent_messages -v
git add app/agent/messages.py tests/test_agent_messages.py
git commit -m "feat(agent): 转换 AgentScope 会话消息"
```

### Task 3: 将 9 个业务工具迁入 AgentScope Toolkit

**Files:**
- Create: `app/agent/tool_specs.py`
- Create: `app/agent/toolkit.py`
- Create: `tests/fixtures/tool_schemas.json`
- Create: `tests/test_agent_toolkit.py`
- Modify: `app/tools/anomaly_query_tool.py`
- Modify: `app/tools/batch_energy_tool.py`
- Modify: `app/tools/cell_lookup_tool.py`
- Modify: `app/tools/cell_metric_query_tool.py`
- Modify: `app/tools/chart_tool.py`
- Modify: `app/tools/energy_param_check_tool.py`
- Modify: `app/tools/energy_saving_tool.py`
- Modify: `app/tools/metric_query_tool.py`
- Modify: `app/tools/report_query_tool.py`

**Interfaces:**
- Produces: `EnergyToolSpec(name, description, input_schema, handler, is_read_only, is_concurrency_safe)`
- Produces: `EnergyFunctionTool(ToolBase)`
- Produces: `build_toolkit(session_factory=get_session_factory) -> Toolkit`
- Produces metadata keys: `tool_name`, `direct_answer`, `download_url`

- [ ] **Step 1: Capture the legacy schema fixture before removing decorators**

Import all nine current tool modules, call `tool_registry.get_tools_for_llm()`, sort by `function.name`, and store the exact Decimal-safe JSON in `tests/fixtures/tool_schemas.json`. The fixture must contain exactly:

```text
analyze_batch_cells_energy
analyze_single_cell_energy
generate_chart
query_anomaly
query_cell_metric
query_energy_param_check
query_metric
query_report
resolve_cell_cgi
```

- [ ] **Step 2: Write failing adapter and schema parity tests**

`tests/test_agent_toolkit.py` covers real `ToolChunk` behavior:

```python
class AgentToolkitTest(unittest.IsolatedAsyncioTestCase):
    async def test_tool_call_uses_a_fresh_session_and_preserves_direct_answer(self) -> None:
        sessions = []

        class FakeSessionContext:
            async def __aenter__(self):
                session = object()
                sessions.append(session)
                return session

            async def __aexit__(self, exc_type, exc, traceback):
                return False

        async def handler(db, province=None):
            return {
                "success": True,
                "report_content": f"report:{province}",
                "download_url": "https://download.invalid/report.xlsx",
            }

        tool = EnergyFunctionTool(
            spec=EnergyToolSpec(
                name="query_report",
                description="report",
                input_schema={"type": "object", "properties": {}},
                handler=handler,
                is_read_only=False,
            ),
            session_factory=FakeSessionContext,
        )

        first = await tool.call(province="湖南省")
        second = await tool.call(province="广东省")

        self.assertIsNot(sessions[0], sessions[1])
        self.assertEqual("success", first.state)
        self.assertIn("report:湖南省", first.metadata["direct_answer"])
        self.assertIn("report.xlsx", first.metadata["direct_answer"])
        self.assertEqual("success", second.state)

    async def test_toolkit_schemas_match_the_legacy_fixture(self) -> None:
        actual = sorted(
            await build_toolkit().get_tool_schemas(),
            key=lambda item: item["function"]["name"],
        )
        self.assertEqual(load_schema_fixture(), actual)
```

Add separate tests for `success=False -> ToolResultState.ERROR`, `check_permissions() -> ALLOW`, and the absence of `db` from every exposed schema.

- [ ] **Step 3: Run tests and verify RED**

Run `.venv/bin/python -m unittest tests.test_agent_toolkit -v`.

Expected: FAIL because the AgentScope adapter/catalog do not exist.

- [ ] **Step 4: Implement the adapter and explicit catalog**

`EnergyFunctionTool.call()` must create a session inside each invocation and return:

```python
payload = await self._spec.handler(db=db, **kwargs)
direct_answer = extract_direct_answer(payload)
return ToolChunk(
    content=[TextBlock(text=dumps_decimal(payload, ensure_ascii=False))],
    state=(
        ToolResultState.ERROR
        if payload.get("success") is False
        else ToolResultState.SUCCESS
    ),
    metadata={
        "tool_name": self.name,
        "direct_answer": direct_answer or "",
        "download_url": payload.get("download_url") or "",
    },
)
```

Copy each current decorator description and parameter schema verbatim into `TOOL_SPECS`. Remove decorator imports and decorator blocks from the nine business modules only after the schema parity test is green.

- [ ] **Step 5: Run GREEN, all existing tests, and commit**

```bash
.venv/bin/python -m unittest tests.test_agent_toolkit -v
.venv/bin/python -m unittest discover -s tests -v
git add app/agent/tool_specs.py app/agent/toolkit.py app/tools tests/fixtures/tool_schemas.json tests/test_agent_toolkit.py
git commit -m "feat(tools): 迁移业务工具到 AgentScope Toolkit"
```

### Task 4: 实现双阶段 Prompt 与 `report_content` 短路

**Files:**
- Create: `app/agent/middleware.py`
- Create: `tests/test_agent_middleware.py`
- Modify: `app/prompts/energy_saving.py`

**Interfaces:**
- Produces: `EnergyPromptMiddleware(user_context: str | None)`
- Produces: `DirectAnswerMiddleware`
- Produces: `build_prompt(phase: str, tool_name: str | None, user_context: str | None) -> str`

- [ ] **Step 1: Write failing prompt-switch tests**

Construct real `AgentState` messages and verify:

```python
async def test_switches_to_tool_specific_synthesis_after_success(self) -> None:
    agent = SimpleNamespace(state=AgentState(context=[
        UserMsg(name="user", content="查询报表"),
        AssistantMsg(name="energy_agent", content=[
            ToolResultBlock(
                id="call-1",
                name="query_report",
                output="{}",
                state=ToolResultState.SUCCESS,
            ),
        ]),
    ]))

    prompt = await EnergyPromptMiddleware(None).on_system_prompt(agent, "base")

    self.assertIn("4G/5G 网络能耗报表分析专家", prompt)
```

Also assert the first phase contains native tool-calling instructions and contains neither `<tool>` nor `</tool>`.

- [ ] **Step 2: Write failing direct-answer lifecycle tests**

Feed `DirectAnswerMiddleware.on_reply()` a generator yielding `ToolResultEndEvent(metadata={"direct_answer": "# report"})` followed by `ModelCallStartEvent`. Assert the output contains one synthetic `TextBlockDeltaEvent(delta="# report")` and one completed `ReplyEndEvent`, and does not yield the next `ModelCallStartEvent`. Add a two-tool-result case proving no short circuit occurs.

- [ ] **Step 3: Run RED**

Run `.venv/bin/python -m unittest tests.test_agent_middleware -v`.

- [ ] **Step 4: Implement middleware with public AgentScope events only**

The direct-answer middleware buffers completed tool metadata until the next model call. For exactly one successful result with `direct_answer`, it emits `TextBlockStartEvent`, `TextBlockDeltaEvent`, `TextBlockEndEvent`, and `ReplyEndEvent(COMPLETED)`, then closes the lower generator. Multiple results and failures pass through unchanged.

- [ ] **Step 5: Rewrite the execution prompt for native function calling**

Keep all domain routing rules and examples, but express examples as tool name + arguments without XML-like tags. Remove textual JSON repair/fallback instructions because Toolkit schemas and AgentScope validation own that boundary.

- [ ] **Step 6: Run GREEN and commit**

```bash
.venv/bin/python -m unittest tests.test_agent_middleware -v
.venv/bin/python -m unittest discover -s tests -v
git add app/agent/middleware.py app/prompts/energy_saving.py tests/test_agent_middleware.py
git commit -m "feat(agent): 保留双阶段提示与报告直出"
```

### Task 5: 构建 request-scoped AgentScope 运行时

**Files:**
- Create: `app/agent/runtime.py`
- Create: `tests/test_agent_runtime.py`

**Interfaces:**
- Produces: `build_energy_agent(conversation_id: str, parsed: ParsedRequestMessages, settings: Settings | None = None, model: ChatModelBase | None = None, toolkit: Toolkit | None = None) -> Agent`
- Produces: `stream_agent_events(messages: list[dict[str, Any]], conversation_id: str) -> AsyncGenerator[AgentEvent, None]`
- Consumes: model factory, toolkit factory, parsed messages, both middleware classes

- [ ] **Step 1: Write failing isolation tests**

Create two agents with the same conversation ID and assert they own different `AgentState` and context list objects while each state has the expected history and `session_id`. Assert the current user is not present until passed to `reply_stream()`.

- [ ] **Step 2: Write a failing fake-model integration test**

Implement a test-only `ChatModelBase` whose `_call_api()` returns one `ChatResponse(content=[TextBlock(text="测试回答")], is_last=True, usage=ChatUsage(...))`. Consume `stream_agent_events()` and assert it yields `ReplyStartEvent`, `TextBlockDeltaEvent`, `ModelCallEndEvent`, and `ReplyEndEvent` in lifecycle order.

- [ ] **Step 3: Run RED**

Run `.venv/bin/python -m unittest tests.test_agent_runtime -v`.

- [ ] **Step 4: Implement the runtime factory**

Construct AgentScope with:

```python
Agent(
    name="energy_agent",
    system_prompt=AGENT_EXECUTION_PROMPT,
    model=model or build_chat_model(settings),
    toolkit=toolkit or build_toolkit(),
    middlewares=[
        DirectAnswerMiddleware(),
        EnergyPromptMiddleware(parsed.user_context),
    ],
    state=AgentState(
        session_id=conversation_id,
        context=parsed.history,
    ),
    react_config=ReActConfig(max_iters=settings.agent_max_steps),
    injection_config=InjectionConfig(
        inject_runtime_state=False,
        timezone="Asia/Shanghai",
    ),
)
```

Call `reply_stream(parsed.current_user)` exactly once and yield only public `AgentEvent` values.

- [ ] **Step 5: Run GREEN and commit**

```bash
.venv/bin/python -m unittest tests.test_agent_runtime -v
.venv/bin/python -m unittest discover -s tests -v
git add app/agent/runtime.py tests/test_agent_runtime.py
git commit -m "feat(agent): 建立请求级 AgentScope 运行时"
```

### Task 6: 将 AgentEvent 统一适配为 Dify streaming/blocking 语义

**Files:**
- Create: `app/agent/dify_events.py`
- Create: `tests/test_dify_event_adapter.py`

**Interfaces:**
- Produces: `DifyRunResult(answer: str, usage: dict[str, int], error: str | None, completed: bool)`
- Produces: `DifyEventAdapter(conversation_id, message_id, task_id, created_at)`
- Produces: `DifyEventAdapter.stream(events: AsyncIterable[AgentEvent]) -> AsyncGenerator[dict[str, str], None]`

- [ ] **Step 1: Write failing golden-sequence tests**

Use deterministic IDs and literal AgentScope events. Verify a text-only run yields:

```text
message(answer="")
message(answer="你好")
message_end(metadata.usage prompt=7 completion=2 total=9)
```

Verify tool call deltas plus tool result text produce exactly one `agent_thought` with parsed `tool_input`, observation, and position 1. Verify `ReplyEndEvent(ERROR)` produces `error` and does not mark the result completed.

- [ ] **Step 2: Run RED**

Run `.venv/bin/python -m unittest tests.test_dify_event_adapter -v`.

- [ ] **Step 3: Implement a stateful event adapter**

Accumulate tool argument fragments by `tool_call_id`, output only `TextBlockDeltaEvent` as user-visible answer, add every `ModelCallEndEvent` token count, and serialize through `dumps_decimal()`. `stream()` updates `adapter.result`; blocking callers consume the same stream and read that result instead of maintaining another aggregation loop.

- [ ] **Step 4: Run GREEN and commit**

```bash
.venv/bin/python -m unittest tests.test_dify_event_adapter -v
.venv/bin/python -m unittest discover -s tests -v
git add app/agent/dify_events.py tests/test_dify_event_adapter.py
git commit -m "feat(api): 适配 AgentScope 事件为 Dify 协议"
```

### Task 7: 切换 FastAPI 入口并移除旧运行时

**Files:**
- Create: `tests/test_agent_routes.py`
- Modify: `app/api/routes.py`
- Modify: `app/models/schemas.py`
- Modify: `main.py`
- Delete: `app/core/agent_runner.py`
- Delete: `app/services/llm_client.py`
- Delete: `app/tools/registry.py`

**Interfaces:**
- Routes consume: `stream_agent_events()` and `DifyEventAdapter`
- Routes preserve: existing URL paths, Dify envelope fields, conversation persistence, message-store notification
- Removes: `StreamEvent`, tool-tag sanitizer/parser, LiteLLM fallback configuration

- [ ] **Step 1: Write failing route parity tests**

Patch only `stream_agent_events()` with a complete real AgentEvent sequence, keep `DifyEventAdapter` real, and verify:

- streaming emits the golden `message -> agent_thought -> message -> message_end` sequence;
- blocking returns the same final answer and usage as the streamed sequence;
- completed answers save exactly one user/assistant pair;
- error runs do not save partial assistant output;
- `inputs` remain controlled system context and history is not duplicated.

- [ ] **Step 2: Run RED against existing routes**

Run `.venv/bin/python -m unittest tests.test_agent_routes -v`.

Expected: FAIL because routes still consume custom `StreamEvent` and separate blocking aggregation.

- [ ] **Step 3: Replace route orchestration**

For each request create one `DifyEventAdapter`, pass `stream_agent_events(messages, conversation_id)` into `adapter.stream()`, and use `adapter.result` for persistence. Streaming yields envelopes directly; blocking consumes the same envelope stream without emitting it and builds its response from the result.

- [ ] **Step 4: Remove legacy modules and startup registration**

Remove `StreamEvent` from schemas, remove `_register_tools()` from `main.py`, update the FastAPI description to AgentScope, delete the three legacy runtime files, and remove all obsolete `llm_tool_call_*` settings.

- [ ] **Step 5: Run route, import, and full tests**

```bash
.venv/bin/python -m unittest tests.test_agent_routes -v
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m compileall -q app main.py
.venv/bin/python -c "from main import app; print(app.title)"
rg -n "litellm|app\.core\.agent_runner|app\.services\.llm_client|app\.tools\.registry|tool_registry|StreamEvent" app main.py requirements.txt
```

Expected: tests/imports pass and `rg` returns no runtime matches.

- [ ] **Step 6: Commit the API cutover and deletion**

```bash
git add app main.py requirements.txt tests/test_agent_routes.py
git commit -m "refactor(agent): 全量切换到 AgentScope 内核"
```

### Task 8: 验证 OpenAI-compatible 协议、容器启动和密钥安全

**Files:**
- Create: `tests/live/__init__.py`
- Create: `tests/live/test_openai_compatible_smoke.py`
- Modify: `Dockerfile`
- Modify: `.env.example`
- Modify: `docs/superpowers/specs/2026-08-10-agentscope-full-migration-design.md`

**Interfaces:**
- Live test reads: `LLM_API_KEY`, `LLM_BASE_URL`, `DEFAULT_MODEL`, and optional `LLM_SMOKE_EXTRA_BODY_JSON`
- Live test exercises: AgentScope non-stream text, stream text, required tool call, array/nullable schema arguments, and parallel tool calls

- [ ] **Step 1: Write an opt-in live smoke test**

Use `@unittest.skipUnless(os.getenv("RUN_LLM_SMOKE") == "1", ...)`. Do not print credential or complete HTTP payload. The test must:

1. create a non-stream `OpenAIChatModel` and assert non-empty `ChatResponse` text;
2. create a stream model and assert at least one non-empty text delta plus a final response;
3. pass a harmless `lookup_demo_cell` schema with required string, enum, array, and nullable fields, force that tool through `ToolChoice`, and assert a `ToolCallBlock` with JSON object input;
4. request two harmless tools in the same turn and verify that both tool calls are emitted when `parallel_tool_calls` is enabled.

- [ ] **Step 2: Verify the test safely skips without secrets**

Run:

```bash
.venv/bin/python -m unittest tests.live.test_openai_compatible_smoke -v
```

Expected: SKIPPED when `RUN_LLM_SMOKE` is absent.

- [ ] **Step 3: Run the real smoke test only after credentials exist in the parent environment**

Run without placing a key in command history:

```bash
RUN_LLM_SMOKE=1 .venv/bin/python -m unittest tests.live.test_openai_compatible_smoke -v
```

Expected: non-stream, stream, complex-schema tool-call, and parallel tool-call tests pass against the explicitly configured endpoint and model. A public provider run is protocol evidence only; release validation repeats the same tests against the production-homologous intranet `qwen3.6_27b` endpoint. If the parent environment lacks `LLM_API_KEY`, stop this step and request secure environment injection rather than embedding the key.

- [ ] **Step 4: Run final offline verification**

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m compileall -q app main.py
.venv/bin/python -c "from main import app; print(app.title, app.version)"
git diff --check
rg -n "sk-[A-Za-z0-9_-]{16,}" --glob '!*.min.js' --glob '!.git/**' .
git status --short
```

Expected: all offline tests pass, compile/import exits 0, whitespace check is clean, secret scan returns no matches, and status contains only intended migration files plus the approved baseline changes.

- [ ] **Step 5: Review requirement coverage and commit verification assets**

Re-read the approved design sections 2, 6, 7, 8, 9, 11, and 13; map every Definition of Done item to the command evidence above. Then commit:

```bash
git add Dockerfile .env.example tests/live docs/superpowers
git commit -m "test(agent): 补齐模型协议与交付验证"
```
