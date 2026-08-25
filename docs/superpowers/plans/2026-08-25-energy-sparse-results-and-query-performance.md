# 节电稀疏结果、报告结构与查询性能实施计划

> 依据：`docs/superpowers/specs/2026-08-25-energy-sparse-results-and-query-performance-design.md`

**目标：** 修正扩展/收缩空结果的业务语义，按确认结构输出单小区报告和批量 Excel，并通过查询裁剪、SQL 聚合和生产索引降低慢查询耗时。

**实现原则：** 保持工具层并发入口不变，把“查询成功但为空”与异常分开；业务状态和表格选择放在纯函数/报告服务中，SQL 层只负责返回原始字段和聚合结果。所有行为变更先写失败测试，再做最小实现。

## 任务 1：固定稀疏结果与过程时点语义

**文件：**
- 修改：`tests/test_energy_analysis.py`
- 修改：`app/services/energy_analysis.py`

1. 增加空扩展结果为 `no_candidate`、候选时点提取、无候选保留 10 个时点的测试。
2. 运行聚焦测试并确认按预期失败。
3. 实现状态推导和过程行选择纯函数。
4. 重跑聚焦测试至通过。

## 任务 2：重构单小区查询与耗时统计

**文件：**
- 修改：`tests/test_energy_saving_tool.py`
- 修改：`tests/test_energy_param_check_tool.py`
- 修改：`app/tools/energy_saving_tool.py`
- 修改：`app/tools/energy_param_check_tool.py`

1. 测试候选小时裁剪、无候选 10 时点解释、空收缩短路、明确日期跳过 `MAX`、7 天 SQL 聚合和分阶段耗时。
2. 确认失败后，将扩展结果与休眠聚合改为顺序裁剪查询。
3. 将休眠前 7 天高负荷天数下推 SQL，并补当前已生效休眠时点。
4. 参数日期明确时直接查询明细；输出细分 `performance`。
5. 重跑工具层测试至通过。

## 任务 3：按业务结构重写单小区报告

**文件：**
- 修改：`tests/test_energy_report.py`
- 修改：`tests/test_energy_prompt.py`
- 修改：`app/services/energy_report.py`
- 修改：`app/prompts/energy_saving.py`

1. 为全空结果、横向扩展明细、高负荷提示、过程表两种展示规则、收缩两部分、白名单三态写失败测试。
2. 实现共享基础信息与精确中文列名映射。
3. 按固定顺序渲染扩展内容；空候选不渲染候选/部署空表。
4. 分开渲染周边影响和休眠前高负荷，空结果输出业务结论。
5. 更新提示词契约并兼容工作区现有角色文案。
6. 重跑报告与提示词测试至通过。

## 任务 4：实现批量一对多工作表方案

**文件：**
- 修改：`tests/test_batch_energy_tool.py`
- 修改：`tests/test_export_util.py`
- 修改：`app/tools/batch_energy_tool.py`
- 修改：`app/utils/export_util.py`

1. 测试仅扩展 1 张、仅收缩 1 张、同时查询 3 张工作表。
2. 测试精确字段顺序/中文列名、汇总一 CGI 一行、收缩明细一对多保留。
3. 按查询目标只访问需要的结果表，并实现基础信息回退与稳定排序。
4. 导出时冻结首行、开启筛选并保持字段顺序。
5. 重跑批量与导出测试至通过。

## 任务 5：增加生产索引脚本

**文件：**
- 新增：`db/indexes/2026-08-25_energy_query_indexes.sql`
- 新增或修改：`tests/test_energy_query_indexes.py`

1. 测试六组索引键、`CONCURRENTLY IF NOT EXISTS` 以及无事务包装。
2. 新增可逐条执行的 PostgreSQL 索引脚本。
3. 重跑索引测试至通过。

## 任务 6：回归、审查与交付

1. 运行所有节电相关聚焦测试。
2. 运行完整测试套件并记录通过/跳过数量。
3. 检查 `git diff --check`、变更范围和用户已有未提交修改。
4. 只暂存本需求相关 hunks，创建 Conventional Commit。
5. 拉取并确认远程 `main` 无新增冲突后推送 `main`。
