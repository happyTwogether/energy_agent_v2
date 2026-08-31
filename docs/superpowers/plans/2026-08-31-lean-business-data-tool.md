# 精简业务数据自服务工具 Implementation Plan

> **2026-08-31 范围变更：** 本计划当时实现的 RRU/AAU 资产表已按用户最新要求从活跃目录移除；通用粒度和关系能力保留。详见[暂停 RRU/AAU 数据自服务设计](../specs/2026-08-31-disable-rru-catalog-design.md)。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将已有 `query_business_data` 原型收敛为项目内单一 AgentScope 工具，显式开放字段与确定性指标，支持最多三张表的审核关系查询，并把结构化数据交给主智能体分析。

**Architecture:** 保留现有目录、规划器、SQLAlchemy 查询执行器和独立只读连接。目录以 YAML 的显式 `fields` 和代码指标注册表为唯一授权源，数据库 Schema 只负责校验字段是否真实存在；执行器查询物理字段和指标依赖字段，程序计算指标后返回稳定中文结构化结果，不再生成可被 `DirectAnswerMiddleware` 直出的完整报告。

**Tech Stack:** Python 3.11、AgentScope 2.0.5、Pydantic 2.11、SQLAlchemy 2.0 async、PyYAML、pytest。

**执行状态（2026-08-31）：** 查询链路、确定性指标、结构化返回和数据库安全边界已完成。四张日报表 189 个字段保持开放；RRU/AAU 资产表及目录关系已按后续范围变更暂停开放。

## Global Constraints

- 不实现 MCP、独立服务、独立端口、远程协议或新增鉴权层。
- 不引入 Embedding、向量数据库、Python 沙箱或第二个智能体。
- 工具只提供数据、口径和质量信息，不输出诊断结论或建议。
- 单次最多三张表，只能使用 YAML 中审核关系；一次最多展开一个一对多明细轴。
- 数据库未在 YAML `fields` 中登记的字段不得自动开放。
- 计算指标只能来自代码注册函数，第一阶段不能参与过滤、分组或排序。
- 默认最近 7 天、显式日期最多 90 天、聊天最多 500 行、Excel 最多 10,000 行。
- 当前工作区已有用户和前序任务改动；只修改本计划列出的文件，不回滚或覆盖无关改动。

---

## File Structure

- `config/business_data_catalog.yaml`：12 张授权表、显式字段、中文名、说明、别名、单位及审核关系。
- `app/self_service/models.py`：增加 `CatalogMetric`、计划 `metrics` 和结构化列定义。
- `app/self_service/metrics.py`：统一定义确定性指标、依赖字段与逐行计算。
- `app/self_service/catalog.py`：只加载 YAML 显式字段；验证数据库类型；搜索表、字段和指标。
- `app/self_service/planner.py`：把候选指标交给内部模型，禁止指标用于条件、分组和排序。
- `app/self_service/query.py`：自动补齐指标依赖字段并保留物理查询结果。
- `app/self_service/service.py`：计算指标、生成结构化列和质量信息、可选导出，不生成完整报告。
- `app/self_service/render.py`：仅保留中文行/列转换与 Excel 数据转换。
- `app/tools/business_data_query_tool.py`：工具边界返回统一结构化错误，不设置 `report_content`。
- `app/prompts/energy_saving.py`：主智能体根据结构化结果比较和诊断，不要求原样直出。
- `tests/fixtures/business_catalog_rows.py`：加入显式字段、日报和 RRU 测试元数据。
- `tests/test_business_catalog.py`：覆盖显式字段白名单、字段说明、RRU 表和模糊搜索。
- `tests/test_business_query.py`：覆盖指标依赖查询与禁止指标参与 SQL 条件。
- `tests/test_business_query_planner.py`：覆盖指标候选进入规划上下文。
- `tests/test_business_query_service.py`：覆盖结构化返回、指标计算、空结果和数据质量。
- `tests/test_business_data_query_tool.py`：覆盖工具不产生直出报告。
- `tests/test_energy_prompt.py`：覆盖主智能体消费结构化数据。

---

### Task 1: 显式字段目录与 RRU 表

**Interfaces:**
- Consumes: YAML `tables.<name>.fields`，数据库 `information_schema.columns` 快照。
- Produces: `BusinessCatalogStore.get_or_load(db) -> CatalogSnapshot`，其中 `CatalogTable.columns` 只包含 YAML 和数据库交集。

- [ ] **Step 1: 写失败测试**

在 `tests/test_business_catalog.py` 增加：数据库返回一个未登记字段 `internal_note` 时，快照不包含它；`nr_report_day_detail.cgi` 的 `description` 来自 YAML；两张 RRU 表可被加载和搜索。

- [ ] **Step 2: 验证 RED**

Run: `PYTHONPATH=. .venv/bin/pytest tests/test_business_catalog.py -q`

Expected: FAIL，因为当前目录自动开放数据库全部字段且没有 RRU 表。

- [ ] **Step 3: 最小实现**

修改 `BusinessCatalogStore._load()`：遍历 `item["fields"]` 而不是数据库行；数据库字段不存在时记录错误并跳过；字段中文名和说明只取显式配置，数据库只提供 `data_type`。在 YAML 中加入两张 RRU 表和所有当前已确认字段；无法确认含义的字段不登记。

- [ ] **Step 4: 验证 GREEN**

Run: `PYTHONPATH=. .venv/bin/pytest tests/test_business_catalog.py tests/test_business_relationships.py -q`

Expected: PASS。

---

### Task 2: 确定性计算指标

**Interfaces:**
- Produces: `CatalogMetric`；`METRIC_REGISTRY: dict[str, CatalogMetric]`；`metric_source_fields(metric_id, table_name) -> tuple[str, ...]`；`calculate_metrics(rows, metric_ids, table_name) -> list[dict[str, Any]]`。
- `BusinessQueryPlan.metrics: list[str]` 只允许输出指标 ID。

- [ ] **Step 1: 写失败测试**

增加测试：`nr_readable_ratio` 使用 `logic_read_station_total / logic_station_total * 100`；分母为零返回 `0.0`；`nr_high_power_ratio` 使用 `(thirtytwo_channel_total + sixtyfour_channel_total) / all_cell_total * 100`；计划选择指标时 SQL 自动选择依赖字段。

- [ ] **Step 2: 验证 RED**

Run: `PYTHONPATH=. .venv/bin/pytest tests/test_business_query.py tests/test_business_query_service.py -q`

Expected: FAIL，因为模型和执行链尚无 `metrics`。

- [ ] **Step 3: 最小实现**

新增 `app/self_service/metrics.py`，将日报已使用的单位换算、可读率、高通道占比、低能效占比、共模站占比和平均生效时长注册为固定函数。扩展模型、目录搜索、规划 Prompt 和查询选择，使指标依赖字段自动补齐；本地校验拒绝未知指标以及指标出现在过滤、分组或排序位置。

- [ ] **Step 4: 验证 GREEN**

Run: `PYTHONPATH=. .venv/bin/pytest tests/test_business_query.py tests/test_business_query_planner.py tests/test_business_query_service.py -q`

Expected: PASS。

---

### Task 3: RRU 审核关系与粒度保护

> 历史实施记录：这两条目录关系已由 2026-08-31 范围变更撤下，不属于当前活跃目录。

**Interfaces:**
- Produces: `lte_detail_to_rru`、`nr_detail_to_rru` 两条 `one_to_many_latest_snapshot` 关系；新增 `rru_item` 结果粒度。

- [ ] **Step 1: 写失败测试**

增加测试：日明细与对应 RRU 资产默认保持 `cell_day` 粒度并预聚合；明确 `rru_item` 时可展开；同一计划不能同时展开 `rru_item` 与 `hour/neighbor`；无白名单关系的两表计划被拒绝。

- [ ] **Step 2: 验证 RED**

Run: `PYTHONPATH=. .venv/bin/pytest tests/test_business_relationships.py tests/test_business_query.py -q`

Expected: FAIL，因为当前无 RRU 关系和粒度。

- [ ] **Step 3: 最小实现**

扩展关系模型和 YAML：按 `cgi` 关联右表最新 `data_date` 快照；默认一对多预聚合，只有 `rru_item` 展开。复用现有单明细轴校验，不允许笛卡尔积。

- [ ] **Step 4: 验证 GREEN**

Run: `PYTHONPATH=. .venv/bin/pytest tests/test_business_relationships.py tests/test_business_query.py -q`

Expected: PASS。

---

### Task 4: 结构化返回并交由主智能体诊断

**Interfaces:**
- Produces: 成功响应包含 `success`、`rows`、`columns`、`tables`、`relationships`、`metrics`、`metric_definitions`、`result_grain`、`actual_date_range`、`applied_defaults`、`data_quality`、`row_count`、`database_ms`、`download_url`。
- 错误响应包含 `success=false`、`error`、`clarification_required`，不包含 `report_content`。

- [ ] **Step 1: 写失败测试**

修改服务和工具测试，断言结构化行使用稳定中文列名，列定义保留 ID/类型/单位；零行仍 `success=true`；结果没有 `report_content`；工具元数据不产生 `direct_answer`；Prompt 要求主智能体比较诊断而不是原样返回。

- [ ] **Step 2: 验证 RED**

Run: `PYTHONPATH=. .venv/bin/pytest tests/test_business_query_service.py tests/test_business_data_query_tool.py tests/test_agent_toolkit.py tests/test_energy_prompt.py -q`

Expected: FAIL，因为当前服务只返回 Markdown 报告摘要。

- [ ] **Step 3: 最小实现**

服务把数据库行和计算指标转换为结构化中文行，生成列定义、指标定义、实际日期范围和缺失字段信息。Excel 使用同一中文行；删除自服务报告渲染和 `report_content`。仅修改 `query_business_data` 对应 synthesis，不影响节电报告等专业工具的直出行为。

- [ ] **Step 4: 验证 GREEN**

Run: `PYTHONPATH=. .venv/bin/pytest tests/test_business_query_service.py tests/test_business_data_query_tool.py tests/test_agent_toolkit.py tests/test_energy_prompt.py -q`

Expected: PASS。

---

### Task 5: 回归、质量检查与提交

- [ ] **Step 1: 运行聚焦套件**

Run: `PYTHONPATH=. .venv/bin/pytest tests/test_business_catalog.py tests/test_business_relationships.py tests/test_business_query.py tests/test_business_query_planner.py tests/test_business_query_service.py tests/test_business_data_query_tool.py tests/test_agent_toolkit.py tests/test_energy_prompt.py -q`

- [ ] **Step 2: 运行离线全量回归**

Run: `PYTHONPATH=. .venv/bin/pytest tests -q --ignore=tests/live`

- [ ] **Step 3: 静态与差异检查**

Run: `git diff --check`

检查 `git diff`，确认没有 MCP、任意 SQL、任意公式或无关改动。

- [ ] **Step 4: 提交**

只暂存本计划列出的代码、测试、配置和计划文件，提交信息：`feat(data): add structured business data self-service`。
