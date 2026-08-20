# 单小区节电报告状态与时延 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 区分扩展结果缺失与确认无候选，生成只展示可复核证据的单小区直出报告，并记录分阶段时延。

**Architecture:** `app/tools/energy_saving_tool.py` 保留数据库访问、并发编排和计时；`app/services/energy_analysis.py` 提供无副作用的状态与过程筛选；新建 `app/services/energy_report.py` 只负责 Markdown 组合。已有 `DirectAnswerMiddleware` 消费 `report_content`，避免第二次模型调用。

**Tech Stack:** Python 3.11, asyncio, SQLAlchemy 2.x, AgentScope 2.x, pytest, pytest-asyncio

## Global Constraints

- 不修改数据库结构、V1.4 源算法、批量分析、导出或 Durable A2A/MCP 运行时。
- `unavailable` 不能显示为 0 小时、非高负荷或不需要扩展；候选字段才是最终扩展结论。
- 过程表仅保留同时拥有 `low_flow_pct` 和 `avg_sleep_seconds` 的时点。
- 白名单使用 `whitelisted`、`not_whitelisted`、`unknown` 三态；仅明确来源能给出前两种状态。
- 保留布尔 `is_whitelist` 兼容字段，新报告以 `whitelist_status` 为准。
- 用户报告中不展示 SQL、预计算、时延或未经数据支持的因果推断。

---

### Task 1: 建立状态与可展示过程证据

**Files:**
- Modify: `app/services/energy_analysis.py:1-165`
- Modify: `tests/test_energy_analysis.py:1-100`

**Interfaces:**
- Consumes: 扩展记录、完整小时证据、可选 `is_whitelist` 来源字典。
- Produces: `derive_expansion_result_status(record) -> str`、`filter_judgeable_expansion_evidence(rows) -> list[dict[str, Any]]`、`derive_process_evidence_status(rows) -> str`、`derive_whitelist_status(*sources) -> str`。

- [x] **Step 1: 写失败测试**

```python
def test_expansion_result_status_distinguishes_absence_from_no_candidate():
    assert derive_expansion_result_status(None) == "unavailable"
    assert derive_expansion_result_status({"hour_filter": None, "hour_filter_early": None}) == "no_candidate"
    assert derive_expansion_result_status({"hour_filter": "22"}) == "candidate_available"


def test_process_table_filters_hours_without_both_judgement_inputs():
    rows = [
        {"hour": 22, "low_flow_pct": 95.0, "avg_sleep_seconds": 0.0},
        {"hour": 23, "low_flow_pct": None, "avg_sleep_seconds": 0.0},
        {"hour": 0, "low_flow_pct": 95.0, "avg_sleep_seconds": None},
    ]
    assert filter_judgeable_expansion_evidence(rows) == [rows[0]]
    assert derive_process_evidence_status(rows) == "partial"


def test_whitelist_status_does_not_turn_missing_source_into_no():
    assert derive_whitelist_status({}) == "unknown"
    assert derive_whitelist_status({"is_whitelist": "否"}) == "not_whitelisted"
    assert derive_whitelist_status({"is_whitelist": "否"}, {"is_whitelist": "是"}) == "whitelisted"
```

- [x] **Step 2: 运行并确认失败**

Run: `PYTHONPATH=. uv run --with-requirements requirements.txt --with pytest --with pytest-asyncio pytest tests/test_energy_analysis.py -k 'result_status or process_table_filters or whitelist_status' -q`

Expected: 缺少新 helper 的导入/收集失败。

- [x] **Step 3: 最小实现**

在 `energy_analysis.py` 新增短小纯函数：缺记录为 `unavailable`，任一候选字段有值为 `candidate_available`；筛选函数要求低业务占比和休眠均值同时非空；白名单只读取存在且非空的 `is_whitelist`，任一“是”优先。

- [x] **Step 4: 运行通过并提交**

Run: `PYTHONPATH=. uv run --with-requirements requirements.txt --with pytest --with pytest-asyncio pytest tests/test_energy_analysis.py -q`

Commit:

```bash
git add app/services/energy_analysis.py tests/test_energy_analysis.py
git diff --cached --check
git commit -m "fix(energy): distinguish analysis and whitelist states"
```

### Task 2: 生成确定性单小区报告

**Files:**
- Create: `app/services/energy_report.py`
- Create: `tests/test_energy_report.py`

**Interfaces:**
- Consumes: 顶层单小区结果（目标、状态、已有表格、参数核查、白名单和基础信息）。
- Produces: `build_single_cell_energy_report(result: dict[str, Any]) -> str`。

- [x] **Step 1: 写失败测试**

```python
def test_report_marks_missing_expansion_result_as_undetermined():
    report = build_single_cell_energy_report({
        "analysis_target": "expansion", "cell_name": "测试小区", "cgi": "460-00-1-1",
        "stat_time": "2026-08-10", "expansion_result_status": "unavailable",
        "expansion_process_evidence_status": "missing", "whitelist_status": "unknown",
    })
    assert "暂不能判断是否需要扩展" in report
    assert "无可扩展时段" not in report
    assert "0小时" not in report


def test_report_uses_explicit_non_whitelist_and_only_ready_process_rows():
    report = build_single_cell_energy_report({
        "analysis_target": "expansion", "cell_name": "测试小区", "cgi": "460-00-1-1",
        "stat_time": "2026-08-10", "expansion_result_status": "no_candidate",
        "expansion_process_evidence_status": "partial", "expansion_process_table": "| 22:00-22:59 | 95.0% |",
        "expansion_candidate_table": "候选表", "expansion_deployment_table": "部署表",
        "param_check": {"success": True, "is_compliant": True},
        "whitelist_status": "not_whitelisted",
    })
    assert "当前未识别需新增的扩展时段" in report
    assert "该小区不是节电白名单小区" in report
    assert "23:00-23:59" not in report
```

- [x] **Step 2: 运行并确认失败**

Run: `PYTHONPATH=. uv run --with-requirements requirements.txt --with pytest pytest tests/test_energy_report.py -q`

Expected: `ModuleNotFoundError: app.services.energy_report`。

- [x] **Step 3: 最小实现**

新建纯渲染模块，以短 helper 组合标题/概览、扩展、收缩、参数核查和白名单。仅在过程状态为 `complete` 或 `partial` 时输出过程表；未获得结果时输出一句状态说明，不构造空表或虚假结论。

- [x] **Step 4: 运行通过并提交**

Run: `PYTHONPATH=. uv run --with-requirements requirements.txt --with pytest pytest tests/test_energy_report.py -q`

Commit:

```bash
git add app/services/energy_report.py tests/test_energy_report.py
git diff --cached --check
git commit -m "feat(energy): render deterministic single-cell report"
```

### Task 3: 接入状态、直出正文和阶段耗时

**Files:**
- Modify: `app/tools/energy_saving_tool.py:1-455`
- Modify: `app/services/energy_report.py:1-90`
- Modify: `app/prompts/energy_saving.py:120-175`
- Modify: `tests/test_energy_saving_tool.py:25-170`
- Modify: `tests/test_energy_report.py:1-100`
- Modify: `tests/test_energy_prompt.py:1-45`

**Interfaces:**
- Consumes: Task 1 的纯函数和 Task 2 的报告构造函数。
- Produces: 扩展结果的状态字段和筛选后过程表；顶层的 `whitelist_status`、`performance`、`report_content`。

- [x] **Step 1: 写失败集成测试**

```python
@pytest.mark.asyncio
async def test_query_expansion_without_result_does_not_create_zero_hour_conclusion(monkeypatch):
    async def fake_fetch_rows(sql, params):
        return []
    monkeypatch.setattr(energy_saving_tool, "fetch_rows", fake_fetch_rows)
    result = await energy_saving_tool._query_expansion_data("460-00-1-1", datetime(2026, 8, 10))
    assert result["expansion_result_status"] == "unavailable"
    assert result["candidate_table"] == ""
    assert result["high_load_type"] is None
```

再增加真实 handler 用例，断言 `report_content` 包含“暂不能判断是否需要扩展”，并包含 `latest_date_ms`、`expansion_ms`、`param_check_ms`、`report_render_ms`、`total_ms`；更新已有过程表用例，断言只渲染完整的 22 点，缺少明细或休眠值的点不出现。

增加完整报告回归用例，断言 `all` 模式保留扩展过程/候选/部署表、四张收缩证据表、休眠前负荷表、白名单原因与有效期，以及参数不合规项；没有对应结果时用一句说明代替空表。

- [x] **Step 2: 运行并确认失败**

Run: `PYTHONPATH=. uv run --with-requirements requirements.txt --with pytest --with pytest-asyncio pytest tests/test_energy_saving_tool.py -k 'without_result or direct_report or process_and_dual_window' -q`

Expected: 当前实现生成零时长表，且没有直出正文、性能 map。

- [x] **Step 3: 最小实现**

在 `energy_saving_tool.py` 中围绕最新日期、名称解析、每个并发任务与报告组装使用 `time.perf_counter`；将毫秒填入 `result["performance"]`，并对每段产生 `logger.info` 的键值日志。扩展记录为空时不构造候选/部署表，且高负荷为 `None`。由扩展/收缩来源合并白名单三态，只读取 `whitelist_reason`，不滥用业务 `reason`。所有默认字段就绪后生成 `result["report_content"]`。

扩充确定性报告，使其保持已确认的可复核结构：概览结论；扩展容量风险、过程表、候选表、连续部署表和参数核查；收缩主小区条件、关系范围、邻区负荷、收缩结果及休眠前负荷；白名单原因和有效期。空证据只输出简短说明，不输出空表。

- [x] **Step 4: 对齐 prompt 兜底契约**

更新 `SYNTHESIS_SINGLE_CELL`：缺扩展结果时只能说暂不能判断；过程表忽略缺失时点；白名单使用三种明确表述。同步更新提示词测试，去掉旧的“未命中节电白名单”断言。

- [x] **Step 5: 运行通过并提交**

Run: `PYTHONPATH=. uv run --with-requirements requirements.txt --with pytest --with pytest-asyncio pytest tests/test_energy_saving_tool.py tests/test_energy_prompt.py tests/test_energy_report.py -q`

Commit:

```bash
git add app/tools/energy_saving_tool.py app/services/energy_report.py app/prompts/energy_saving.py tests/test_energy_saving_tool.py tests/test_energy_report.py tests/test_energy_prompt.py
git diff --cached --check
git commit -m "fix(energy): avoid false no-expansion conclusions"
```

### Task 4: 验证 AgentScope 直出链路与全量回归

**Files:**
- Modify only if a real regression test needs correction: `tests/test_agent_integration.py`

**Interfaces:**
- Consumes: 成功工具返回的 `report_content`。
- Produces: 仅一次模型调用的 AgentScope 响应及全量回归证据。

- [x] **Step 1: 验证直出中间件**

Run: `PYTHONPATH=. uv run --with-requirements requirements.txt --with pytest --with pytest-asyncio pytest tests/test_agent_integration.py tests/test_agent_middleware.py tests/test_agent_toolkit.py -q`

Expected: pass；集成测试中的 `model.calls == 1`。

- [x] **Step 2: 全量测试**

Run: `PYTHONPATH=. UV_CACHE_DIR=/private/tmp/energy-agent-report-uv-cache uv run --isolated --with-requirements requirements.txt --with pytest --with pytest-asyncio pytest -q`

Expected: 全部通过；单独记录既有 warning。

- [x] **Step 3: 最终差异检查**

Run:

```bash
git diff main...HEAD --check
git status --short --branch
git log --oneline main..HEAD
```
