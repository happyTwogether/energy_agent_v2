# Single Data Query Tool Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将单小区和汇总指标查询能力完整迁入 `query_business_data`，并从 AgentScope Toolkit 删除 `query_cell_metric` 和 `query_metric`。

**Architecture:** `query_business_data` 是唯一通用数据查询 Interface，目录快照提供物理字段，确定性指标注册提供来源字段、Python 公式和 SQL 聚合公式。专业诊断、报告、核查、CGI 解析和图表 Module 保留独立 Interface。

**Tech Stack:** Python 3.11、AgentScope 2.0.5、Pydantic 2.11、SQLAlchemy 2.0 async、PyYAML 6.0.3、pytest。

## Global Constraints

- Toolkit 最终只注册 8 个工具，其中唯一数据查询工具是 `query_business_data`。
- 删除 `query_cell_metric` 和 `query_metric`，不保留隐式 fallback。
- 保留物理字段查询、固定公式、分组、排序、Top N、Excel 和图表能力。
- 大模型只能选择指标 ID，不得生成公式或 SQL。
- 比例和平均时长类指标分组后使用聚合分子和聚合分母计算。
- 不新增 Python 依赖；继续使用 `PyYAML==6.0.3`。
- 不修改或提交无关 `.superpowers/` 目录。

---

### Task 1: 使确定性指标支持分组和指标排序

**Files:**
- Modify: `app/self_service/models.py`
- Modify: `app/self_service/metrics.py`
- Modify: `app/self_service/query.py`
- Modify: `app/self_service/planner.py`
- Modify: `app/self_service/service.py`
- Modify: `tests/test_business_metrics.py`
- Modify: `tests/test_business_query.py`
- Modify: `tests/test_business_query_planner.py`
- Modify: `tests/test_business_query_service.py`

**Interfaces:**
- Consumes: `CatalogMetric.source_fields`、`BusinessQueryPlan.metrics`、`BusinessQueryPlan.group_by`、`BusinessQueryPlan.order_by`。
- Produces: `CatalogMetric.source_aggregations: tuple[Literal["sum", "avg", "min", "max"], ...]`；`QueryOrder.metric_id: str | None`；`build_metric_sql_expression(metric, source_columns, aggregate)`。

- [x] **Step 1: 写分组计算指标 RED 测试**

在 `tests/test_business_query.py` 新增计划：按 `dist_name` 分组，选择 `nr_readable_ratio`，按该指标倒序并 `limit=5`。断言编译 SQL 包含 `sum(logic_read_station_total)`、`sum(logic_station_total)`、比例表达式、`ORDER BY nr_readable_ratio DESC` 和 `LIMIT`。

```python
plan = BusinessQueryPlan(
    base_table="nr_report_day_collect",
    tables=["nr_report_day_collect"],
    select=[QueryFieldRef(table="nr_report_day_collect", field="dist_name")],
    group_by=[QueryFieldRef(table="nr_report_day_collect", field="dist_name")],
    metrics=["nr_readable_ratio"],
    order_by=[QueryOrder(metric_id="nr_readable_ratio", direction="desc")],
    limit=5,
)
```

在 `tests/test_business_metrics.py` 新增月节电量换算和注册聚合口径测试；在 `tests/test_business_query_service.py` 新增“聚合来源字段仍计算为正确中文指标，且不泄漏来源字段”测试。

- [x] **Step 2: 运行 RED**

Run: `PYTHONPATH=. .venv/bin/pytest tests/test_business_metrics.py tests/test_business_query.py tests/test_business_query_planner.py tests/test_business_query_service.py -q`

Expected: FAIL，因为 `QueryOrder` 没有 `metric_id`，验证层禁止计算指标与分组同时使用，且注册中没有月节电量换算。

- [x] **Step 3: 实现指标聚合口径**

扩展 `CatalogMetric` 记录每个来源字段的聚合方式，字段数与聚合方式数不一致时拒绝加载。在 `app/self_service/metrics.py` 中为每个 `calculator` 提供参数化 SQLAlchemy 表达式构造器，同一注册同时驱动 Python 返回值和排序 SQL。增加 `lte_month_energy_saving_wan` 和 `nr_month_energy_saving_wan`。

- [x] **Step 4: 实现分组指标 SQL 与排序**

允许 `metrics + group_by`，但展示物理字段必须属于分组字段。自动补齐的指标来源字段按注册口径聚合并保留原来的内部别名，以便 Python 计算和数据质量检查。`QueryOrder.metric_id` 只能引用 `plan.metrics` 中的已授权指标，SQL 使用注册公式别名排序后再应用 `limit`。

- [x] **Step 5: 验证 GREEN**

Run: `PYTHONPATH=. .venv/bin/pytest tests/test_business_metrics.py tests/test_business_query.py tests/test_business_query_planner.py tests/test_business_query_service.py -q`

Expected: PASS。

---

### Task 2: 使图表消费统一查询返回

**Files:**
- Modify: `app/tools/chart_tool.py`
- Modify: `tests/test_chart_tool.py`
- Modify: `tests/fixtures/tool_schemas.json`

**Interfaces:**
- Consumes: `query_business_data` 返回的 `rows: list[dict[str, Any]]` 和 `columns: list[{id,label,type,unit}]`。
- Produces: `generate_chart` 对中文行键、日期维度、分组维度和计算指标的稳定 ECharts option。

- [x] **Step 1: 写中文结构化数据 RED 测试**

新增图表测试，输入包含 `columns=[{"id":"nr_report_day_collect.data_date","label":"数据日期",...},{"id":"nr_traffic_tb","label":"5G上下行业务量",...}]` 和中文 `rows`。断言 X 轴使用日期，series 名使用中文指标名，数值完整保留。

- [x] **Step 2: 运行 RED**

Run: `PYTHONPATH=. .venv/bin/pytest tests/test_chart_tool.py -q`

Expected: FAIL，因为旧图表工具只识别英文行键，且 schema 文案绑定 `query_metric`。

- [x] **Step 3: 实现列元数据解析**

将 `columns` 转换为中文键到字段 ID/类型/单位的映射，优先按 ID 识别日期与维度，其余数值列作为指标。保留对现有英文行数据的兼容，但 schema 只说明消费“数据查询工具的完整结果”。

- [x] **Step 4: 验证 GREEN**

Run: `PYTHONPATH=. .venv/bin/pytest tests/test_chart_tool.py -q`

Expected: PASS。

---

### Task 3: 撤下旧数据工具并精简意图契约

**Files:**
- Delete: `app/tools/cell_metric_query_tool.py`
- Delete: `app/tools/metric_query_tool.py`
- Modify: `app/agent/tool_specs.py`
- Modify: `app/agent/toolkit.py`
- Modify: `app/prompts/energy_saving.py`
- Modify: `app/core/metrics_registry.py`
- Modify: `tests/test_agent_toolkit.py`
- Modify: `tests/test_energy_prompt.py`
- Modify: `tests/fixtures/tool_schemas.json`
- Modify: `docs/operations/business-data-self-service.md`

**Interfaces:**
- Consumes: Task 1 已完整迁移的物理字段和确定性指标能力。
- Produces: 只含 8 个工具的 `TOOL_SPECS`；唯一数据查询名称 `query_business_data`；不含旧指标注册常量的共享公式 Module。

- [x] **Step 1: 写工具目录 RED 测试**

修改 Toolkit 测试断言精确 8 个工具，数据查询工具集合精确为 `{"query_business_data"}`，并断言旧工具名不在 schema。将提示词测试改为断言三类稳定意图边界，不断言单句中文示例。

- [x] **Step 2: 运行 RED**

Run: `PYTHONPATH=. .venv/bin/pytest tests/test_agent_toolkit.py tests/test_energy_prompt.py -q`

Expected: FAIL，因为旧两个工具仍在 Toolkit 和提示词中。

- [x] **Step 3: 删除旧工具与重复注册**

从 `TOOL_SPECS`、schema 夹具和总结提示词映射删除旧工具，删除两个工具文件。从 `app/core/metrics_registry.py` 删除 `REPORT_METRICS`、`CELL_METRICS`、`ALL_METRICS` 以及只服务旧注册的辅助函数，保留日报与新指标注册共用的安全换算公式。

- [x] **Step 4: 精简执行提示词**

将数据意图收缩为三条：授权数据获取/筛选/分组/比较/排序/导出统一调用 `query_business_data`；专业诊断和固定报告调用对应工具；图表先调用 `query_business_data` 再调用 `generate_chart`。删除旧指标组、旧工具参数和逐句数据路由示例。

- [x] **Step 5: 同步运维文档和验收问题**

文档明确唯一数据入口、物理字段/计算指标分层、`SELF_SERVICE_ENABLED=true`、只读连接和 PyYAML 依赖。增加单小区深度休眠时长+开关、各地市流量 Top 5 和能耗趋势图验收问题。

- [x] **Step 6: 验证 GREEN**

Run: `PYTHONPATH=. .venv/bin/pytest tests/test_agent_toolkit.py tests/test_energy_prompt.py tests/test_business_metrics.py tests/test_business_query.py tests/test_business_query_planner.py tests/test_business_query_service.py tests/test_chart_tool.py -q`

Expected: PASS。

---

### Task 4: 全量验证、提交与推送

**Files:**
- Modify: `docs/superpowers/plans/2026-09-02-single-data-query-tool.md`

**Interfaces:**
- Consumes: 上述三个 Task 的完整工作树。
- Produces: 通过验证、Conventional Commit 并与 `origin/main` 一致的 `main`。

- [x] **Step 1: 运行静态核对**

Run: `rg -n "query_cell_metric|query_metric|REPORT_METRICS|CELL_METRICS|ALL_METRICS" app tests/fixtures docs/operations`

Expected: 无运行时或 schema 残留；若文档需说明已撤下名称，只能出现于历史设计/计划。

- [x] **Step 2: 运行全量测试**

Run: `PYTHONPATH=. .venv/bin/pytest -q`

Expected: 全部通过，只允许已知的 Starlette `python_multipart` 第三方弃用 warning。

- [ ] **Step 3: 审查并提交**

```bash
git diff --check
git status --short
git diff
git add app/agent/tool_specs.py app/agent/toolkit.py app/core/metrics_registry.py app/prompts/energy_saving.py app/self_service/models.py app/self_service/metrics.py app/self_service/planner.py app/self_service/query.py app/tools/business_data_query_tool.py app/tools/chart_tool.py app/tools/cell_metric_query_tool.py app/tools/metric_query_tool.py tests/fixtures/tool_schemas.json tests/test_agent_toolkit.py tests/test_business_metrics.py tests/test_business_query.py tests/test_business_query_planner.py tests/test_business_query_service.py tests/test_chart_tool.py tests/test_energy_prompt.py docs/operations/business-data-self-service.md docs/superpowers/plans/2026-09-02-single-data-query-tool.md
git diff --cached --check
git diff --cached
git commit -m "refactor(data): unify business data queries"
```

- [ ] **Step 4: 推送并核对远程**

Run: `git push origin main`

Expected: `git rev-parse HEAD` 与 `git rev-parse origin/main` 一致。
