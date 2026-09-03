# 能效智能体提示词职责重构 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 重构执行提示词和工具描述的职责边界，使数据读取稳定调用 `query_business_data`，节电决策稳定调用专业分析工具，并提供可在内网模型运行的路由回归评测。

**Architecture:** 保持现有 AgentScope 单次原生 function calling 架构，不增加路由模型。执行提示词只负责全局约束和一级任务分类，工具描述负责各自的适用与排除边界，JSON Schema 负责参数，总结提示词只解释工具结果，Python 继续生成确定性业务报告。

**Tech Stack:** Python 3.11、AgentScope 2.0.5、OpenAI-compatible 内网模型、pytest/unittest。

## Global Constraints

- 不新增 Python 依赖、外部服务或数据库变更。
- 不改变确定性指标公式、节电业务阈值和既有 `report_content` 报告格式。
- 不新增基于关键词的 Python 硬编码路由器。
- 不处理工作区中用户已有的 `.superpowers/` 未跟踪目录。
- 所有生产行为修改遵循 RED → GREEN → REFACTOR。

---

### Task 1: 建立互斥的执行路由边界

**Files:**
- Modify: `tests/test_energy_prompt.py`
- Modify: `tests/test_agent_toolkit.py`
- Modify: `app/prompts/energy_saving.py`
- Modify: `app/tools/business_data_query_tool.py`
- Modify: `app/tools/energy_saving_tool.py`
- Modify: `app/tools/batch_energy_tool.py`
- Modify: `app/tools/anomaly_query_tool.py`
- Modify: `app/tools/energy_param_check_tool.py`
- Modify: `app/tools/report_query_tool.py`
- Modify: `app/tools/cell_lookup_tool.py`
- Modify: `app/tools/chart_tool.py`
- Modify: `tests/fixtures/tool_schemas.json`

**Interfaces:**
- Consumes: `EnergyToolSpec.description` 和现有八个工具的 JSON Schema。
- Produces: 精简的 `AGENT_EXECUTION_PROMPT`，以及采用“适用；不适用”结构的八个 `TOOL_DESCRIPTION`。

- [ ] **Step 1: 写入失败的边界测试**

在 `tests/test_energy_prompt.py` 增加断言，要求执行提示词明确：字段/指标读取使用 `query_business_data`，即使问题只涉及一个小区；仅在用户要求节电判断或建议时使用 `analyze_single_cell_energy`；同时禁止出现旧句“分析节电空间或查指标时，直接调用对应业务工具”和“按中文名查询单小区指标”。

在 `tests/test_agent_toolkit.py` 增加断言，要求 `query_business_data` 描述包含“仅查询字段值/指标值”这一适用边界，`analyze_single_cell_energy` 描述包含“不用于仅查询字段值、指标值、表记录、明细或趋势”这一排除边界；其他专业工具也必须包含“不适用”说明。

- [ ] **Step 2: 运行聚焦测试并确认 RED**

Run: `PYTHONPATH=. .venv/bin/pytest tests/test_energy_prompt.py tests/test_agent_toolkit.py -q`

Expected: 新增边界断言失败，失败原因是旧执行提示词和工具描述尚未声明互斥边界。

- [ ] **Step 3: 最小重构执行提示词**

将 `AGENT_EXECUTION_PROMPT` 重写为五部分：角色与日期、全局安全规则、按最终目标路由、跨工具参数规则、图表串联规则。删除字段枚举、重复图表规则、旧单小区指标参数说明和“查指标调用对应业务工具”的冲突句。

必须保留以下行为：原生 function calling、每次重新查询、日期不推断、不编造数据与下载链接、`query_business_data.question` 原样传递、专业分析的 CGI/小区名与 `analysis_target` 推导、地域和厂家参数的必要归一化。

- [ ] **Step 4: 最小重构八个工具描述并更新 Schema 快照**

每个 `TOOL_DESCRIPTION` 使用短句说明产出、适用范围和“不适用”范围。重点让 `query_business_data` 与 `analyze_single_cell_energy` 形成互斥边界；JSON Schema 的字段、类型和 required 不变，只更新快照中的 description。

- [ ] **Step 5: 运行聚焦测试并确认 GREEN**

Run: `PYTHONPATH=. .venv/bin/pytest tests/test_energy_prompt.py tests/test_agent_toolkit.py -q`

Expected: 全部通过。

### Task 2: 清理总结提示词的重复职责

**Files:**
- Modify: `tests/test_energy_prompt.py`
- Modify: `app/prompts/energy_saving.py`
- Test: `tests/test_agent_middleware.py`

**Interfaces:**
- Consumes: 工具返回的 `rows`、`columns`、`metric_definitions`、`data_quality` 和 `report_content`。
- Produces: 只负责结果展示的工具专属 synthesis prompts；`get_synthesis_prompt(tool_name)` 接口保持不变。

- [ ] **Step 1: 写入失败的总结职责测试**

增加测试要求 `_SYNTHESIS_IDENTITY` 不再要求模型重做报告结构；`SYNTHESIS_BUSINESS_DATA` 明确只回答用户所问字段并区分零行与失败；确定性报告提示词明确非空 `report_content` 原样返回。保留单小区旧结果兼容块所需的 V1.4 契约断言。

- [ ] **Step 2: 运行聚焦测试并确认 RED**

Run: `PYTHONPATH=. .venv/bin/pytest tests/test_energy_prompt.py -q`

Expected: 新增的职责断言失败。

- [ ] **Step 3: 精简公共和工具专属总结提示词**

公共 identity 只保留不编造、术语、错误与候选选择规则。数据查询总结限定为按问题展示 `rows`、列单位、粒度、口径和质量；固定报告类在 `report_content` 存在时原样返回。保留 `SYNTHESIS_SINGLE_CELL` 的旧结构兼容规则，不改变 Python 报告输出。

- [ ] **Step 4: 运行提示词与中间件测试并确认 GREEN**

Run: `PYTHONPATH=. .venv/bin/pytest tests/test_energy_prompt.py tests/test_agent_middleware.py -q`

Expected: 全部通过，执行/总结阶段切换和直出行为保持不变。

### Task 3: 增加真实内网模型路由评测

**Files:**
- Create: `tests/fixtures/agent_routing_cases.json`
- Create: `tests/live/test_agent_routing.py`
- Modify: `docs/operations/business-data-self-service.md`

**Interfaces:**
- Consumes: `AGENT_EXECUTION_PROMPT`、`TOOL_SPECS`、`build_chat_model(stream=False)` 和环境变量中的现有 LLM 配置。
- Produces: 通过 `RUN_AGENT_ROUTING_EVAL=1` 显式启用的真实 function-calling 路由评测，不访问数据库。

- [ ] **Step 1: 写入路由案例夹具和 opt-in live 测试**

案例至少覆盖：生产失败原句及其改写、单小区原始字段、多表原始字段、单小区节电空间、仅扩展诊断、批量诊断、异常诊断、参数核查、固定报告、小区 CGI 解析和图表取数第一步。每条记录包含 `question`、`expected_tool`，必要时包含 `expected_arguments`；生产失败原句要求 `question` 完整原样传入。

测试直接把 `TOOL_SPECS` 转成 OpenAI function schema，调用现有内网模型一次，只检查首轮工具名与关键参数，不执行工具和数据库查询。没有设置 `RUN_AGENT_ROUTING_EVAL=1` 时安全跳过。

- [ ] **Step 2: 先运行离线收集测试**

Run: `PYTHONPATH=. .venv/bin/pytest tests/live/test_agent_routing.py -q`

Expected: 测试模块可正常收集，未显式启用时显示 skipped。

- [ ] **Step 3: 为生产测试补充操作说明**

在运维文档增加命令：

```bash
RUN_AGENT_ROUTING_EVAL=1 PYTHONPATH=. .venv/bin/pytest tests/live/test_agent_routing.py -v
```

说明该命令会访问配置的内网模型但不会查询业务数据库，并说明失败输出会指出问题、实际工具和预期工具。

- [ ] **Step 4: 运行相关离线测试**

Run: `PYTHONPATH=. .venv/bin/pytest tests/test_energy_prompt.py tests/test_agent_toolkit.py tests/test_agent_middleware.py tests/live/test_agent_routing.py -q`

Expected: 离线测试通过，live 路由评测安全跳过。

### Task 4: 全量验证和交付

**Files:**
- Verify: all modified files

**Interfaces:**
- Consumes: 前三项所有修改。
- Produces: 可发布的提示词路由重构提交。

- [ ] **Step 1: 运行完整离线测试**

Run: `PYTHONPATH=. .venv/bin/pytest -q`

Expected: 所有离线测试通过；需要显式环境变量的 live 测试 skipped。

- [ ] **Step 2: 运行静态验证**

Run: `.venv/bin/python -m compileall -q app tests`

Run: `git diff --check`

Expected: 两条命令均退出 0。

- [ ] **Step 3: 审查变更范围**

确认没有新增依赖、数据库代码或指标公式变化，没有暂存 `.superpowers/`，工具总数和 JSON Schema 参数保持不变。

- [ ] **Step 4: 提交实现**

```bash
git add app/prompts/energy_saving.py app/tools tests docs/operations/business-data-self-service.md tests/fixtures/agent_routing_cases.json tests/live/test_agent_routing.py
git commit -m "fix(agent): separate data queries from energy diagnosis"
```
