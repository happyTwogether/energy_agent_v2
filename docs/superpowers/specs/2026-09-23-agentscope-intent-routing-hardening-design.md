# AgentScope 意图路由与报表参数加固设计

## 背景

生产对话暴露了两类问题：

1. “全省中兴能耗报表”和“邵阳华为能耗报表”首次调用 `query_report` 时漏传地市或厂家，工具默认查询“全网-全网”，模型随后又重复查询才纠正。
2. 用户只问“邵阳有哪些厂家可以看”时，Agent 误生成完整报表；纠正性追问又误走小区解析，最终耗尽 ReAct 迭代次数。

现有主链路已使用 AgentScope 的 `Agent`、`Toolkit`、`MiddlewareBase`、`ToolChoice` 和 `ReActConfig`。本次不新增另一套智能体框架，也不新增“报表维度枚举工具”，而是收紧 AgentScope 现有组件的职责边界。

## 目标

- 明确报表请求首次就选择 `query_report`，且不能空参数查询错误范围。
- “有哪些厂家”继续使用现有 `query_business_data`，只返回厂家枚举结果。
- Middleware 只负责运行时约束和工具选择，不负责拼 SQL、查库或解析报表业务参数。
- 通用查询规划层正式支持“枚举唯一维度值”，不在 service 中写“邵阳+厂家”专用业务分支。
- 保留 AgentScope ReAct 对多步查询和图表链路的编排能力。

## 非目标

- 不新增独立意图分类模型或额外 LLM 调用。
- 不新增报表维度枚举工具。
- 不把地市、厂家、日期和 SQL 解析逻辑放入 Middleware。
- 不改变能耗报表指标口径、前推 7 天基线和 Markdown 格式。
- 不更改扩展、收缩、异常诊断和参数核查的业务阈值。

## 总体架构

```text
用户问题
  -> AgentScope Agent / ReAct
  -> GroundedToolChoiceMiddleware（证据门禁 + 高置信工具限定）
  -> 现有 Toolkit
       -> query_report（完整固定报表）
       -> query_business_data（字段、指标和维度值）
  -> DirectAnswerMiddleware 或工具专属总结 Prompt
  -> 用户结果
```

### AgentScope Middleware

Middleware 是 Agent 生命周期的拦截器，不是另一个业务 Agent。

- `GroundedToolChoiceMiddleware` 在本轮尚无工具证据时设置 AgentScope `ToolChoice`。
- 明确的完整能耗报表限定为 `query_report`。
- 明确的维度值枚举限定为 `query_business_data`。
- 其他业务请求仍使用现有工具集，由模型依据互斥工具描述选择。
- Middleware 不修改工具 arguments，不读数据库，不生成最终业务结论。

高置信规则只解决工具职责冲突，不在 Middleware 中枚举各种地市、厂家或字段。

### `query_report` Schema 和参数完整性

当前 `query_report` 的所有入参都可选，原生 function calling 可以合法地发出 `{}`，导致错误的“全网-全网”报表。

本次通过 JSON Schema 收紧必填参数，不在 Middleware 里补参数：

- `province`、`dist_name`、`prod_name`为必填字段。
- 用户未限定的维度由模型显式传入“全网”，工具不再因为空参数静默放大范围。
- `freq_band`、`site_type`、`area` 继续可选，未指定时使用现有“全网”默认值。
- 提示词和 Schema 字段说明明确“全省”对应 `dist_name=全网`，地市名规范化后传入 `dist_name`。
- Python 工具函数保留默认值，以兼容内部直接调用；AgentScope 对外 Schema 负责防止模型空调用。

### `query_business_data` 维度值查询

继续使用唯一通用数据查询工具。“邵阳有哪些厂家可以看”的结构化计划应表达为：

- 选择厂家字段 `prod_name`。
- 按 `prod_name` 分组，得到唯一维度值。
- 用 `dist_name=邵阳市` 过滤。
- 不查询能耗指标，不生成固定报表。

这个能力在通用规划层实现：

1. 规划 Prompt 明确“有哪些/可选值/唯一值”使用同一字段的 `select + group_by`。
2. 规划器结构化输出校验失败时，记录可定位的校验原因，并进行最多一次带校验反馈的修复重试。
3. 重试仍失败时返回明确错误，不改走小区解析或完整报表。
4. 维度枚举是通用查询内部的一种受控计划类型，不是新的 Agent 工具。它可对具有同一授权维度的多个候选表分别执行参数化查询，再对同名维度值做集合合并。
5. service 按计划类型执行通用查询，不保留“邵阳+厂家”专用 SQL、专用工具或专用返回格式。

对未指定 4G/5G 的厂家枚举，查询 `lte_report_day_collect` 和 `nr_report_day_collect` 中符合筛选条件的 `prod_name`，合并去重后返回；用户明确指定 4G 或 5G 时只查询对应来源。

## 实现收口

当前工作区中已有一版为快速修复案例而添加的实验性代码。正式实现首先收口这些变更：

- 删除消息适配层中的厂家专用正则和历史问题替换。
- 保留 Middleware 对高置信工具的 AgentScope `ToolChoice` 限定，但将判断收敛为少量工具职责边界。
- 将厂家枚举从专用 service 分支改造为通用维度值计划和通用合并执行。
- 保留已增加的失败日志详情，用于定位结构化规划校验错误。

### 多轮纠正和失败收口

- 保留“历史用户问题可用、历史助手回答隔离”的现有原则。
- 省略对象的纠正性追问由执行 Prompt 使用历史用户问题理解，消息适配层不写厂家专用重写规则。
- `query_business_data` 返回规划失败、需要澄清或零行时，总结 Prompt 必须回答当前状态，不得换用 `query_report` 或 `resolve_cell_cgi` 猜测。
- 完整报表的 `report_content` 仍由 `DirectAnswerMiddleware` 原样直出，避免二次模型改写。

## Prompt 调整

执行 Prompt 保持短且分层：

1. 先按用户最终产出区分“取数”与“专业报告/诊断”。
2. “有哪些厂家”是取数，不是报告。
3. 只有明确要求完整能耗报表时才用 `query_report`。
4. 工具失败后优先向用户说明失败或请求澄清，不为了完成循环而换用无关工具。

不把地市名单、厂家名单、SQL 模板或大量句式样例写入主 Prompt。

## 测试与验收

### 离线测试

- Middleware 测试：报表请求限定到 `query_report`，厂家枚举限定到 `query_business_data`，工具返回后解除首轮证据门禁。
- Schema 测试：`query_report` 的必填维度不允许空 function call。
- 规划器测试：维度枚举生成 `select + group_by`，一次校验修复后仍只能使用授权表和字段。
- 服务测试：厂家枚举走通用计划和通用输出，不走专用 service 分支。
- 消息测试：历史助手答案仍不进入 Agent context，消息层不做厂家专用重写。
- 全量回归：`PYTHONPATH=. .venv/bin/pytest -q`。

### 生产同款模型路由验收

评测样本至少覆盖：

1. `全省中兴能耗报表` -> `query_report(province=湖南省, dist_name=全网, prod_name=中兴)`。
2. `邵阳华为能耗报表` -> `query_report(province=湖南省, dist_name=邵阳市, prod_name=华为)`。
3. `邵阳有哪些厂家可以看` -> `query_business_data(question=原问题)`。
4. `我没问你，我只问有哪些` -> 复用最近的用户问题，重新调用 `query_business_data`。

路由评测只调用模型检查工具名和 arguments，可不访问业务库。数据结果验收需要目标环境数据库或最小脱敏样本。

## 生产数据要求

实现和离线测试不需要生产数据。端到端验证时，若无法连接目标数据库，只需导出最近 7 天、邵阳市、以下字段的脱敏结果：

- 表：`lte_report_day_collect`、`nr_report_day_collect`。
- 字段：`data_date`、`province`、`dist_name`、`prod_name`、`freq_band`、`site_type`、`area`。
- 可只保留去重后的维度组合，不需要能耗数值、小区名、CGI、基站号或用户数据。

只有在离线与生产同款模型路由测试通过后，才请求这份数据，避免用生产数据弥补代码设计缺陷。

## 回退

本次不修改数据库 Schema 或业务指标。若路由或参数行为退化，可单独回退 Middleware、Prompt、工具 Schema 和规划器修改，报表查询代码和数据库口径不受影响。
