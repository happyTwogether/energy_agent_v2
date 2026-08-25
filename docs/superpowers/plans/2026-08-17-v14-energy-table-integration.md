# V1.4 Energy Table Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将数据库已预计算的 V1.4 节电扩展与收缩字段完整接入 AgentScope 生产链路，并修正单小区、休眠前负荷、批量统计和原始 Excel 导出。

**Architecture:** 新增无数据库依赖的纯业务规则模块，工具层继续负责查询和兼容返回结构。扩展、收缩结论以 `jd_agent` 预计算表为结果源；距离来自 `jd_cell_around`，站型通过 4G/5G 工参表批量补齐。批量和单小区复用同一组规则，避免 N+1 查询并为后续 MCP 迁移保留稳定边界。

**Tech Stack:** Python 3.11、AgentScope、SQLAlchemy async、PostgreSQL、pandas/openpyxl、pytest/unittest。

## Global Constraints

- 基线必须是最新 `origin/main@dcad425` 或其后续快进提交。
- `prb_increase = 10.0` 必须保留，不在应用层按 `> 10%` 过滤。
- 宏站主小区使用 200 米且只允许宏站邻区；室分使用 100 米且允许宏站、室分邻区；微站使用 200 米且不限制邻区站型。
- 扩展和收缩预计算表是业务结果源，不在 Python 中重算低流量概率、零休眠或 PRB 抬升算法。
- 主小区/邻区工参查询必须批量执行，禁止逐行 N+1。
- 保持现有 9 个工具名、输入 Schema 和主要返回键向后兼容。
- 所有生产行为使用 RED → GREEN → REFACTOR；每个测试必须先观察到因目标行为缺失而失败。
- 本计划只改旧 AgentScope `app/` 生产链路，不切换 Durable A2A/MCP。

---

### Task 1: 校准最新 main 的工具 Schema 测试基线

**Files:**
- Modify: `tests/fixtures/tool_schemas.json`
- Test: `tests/test_agent_toolkit.py`

**Interfaces:**
- Consumes: `build_toolkit().get_tool_schemas()` 当前实际 Schema。
- Produces: 与最新参数核查工具描述和 `export_excel` 参数一致的 fixture。

- [x] **Step 1: 复现既有基线失败并保存证据**

Run:

```bash
env PYTHONPATH=. .venv/bin/pytest tests/test_agent_toolkit.py::AgentToolkitTest::test_toolkit_schemas_match_the_legacy_fixture -v
```

Expected: FAIL，差异只位于 `query_energy_param_check` 的 description 和新增 `export_excel` 参数。

- [x] **Step 2: 只同步参数核查 Schema fixture**

将 fixture 中 `query_energy_param_check` 更新为：

```json
{
  "name": "query_energy_param_check",
  "description": "查询节能参数核查结果，分析不合规配置，并可将不合规清单导出为 Excel 报表。\n适用：参数合规性核查、生成参数核查报表 / Excel 下载。\n注意：用户说'参数合规 + 生成报表/导出 Excel'时，用本工具并传 export_excel=true，不要调用 query_report（那是能耗汇总报告）。",
  "parameters": {
    "type": "object",
    "properties": {
      "check_date": {"type": "string", "description": "核查日期 (YYYY-MM-DD)，不传则自动查最新"},
      "cgi": {"type": "string", "description": "CGI，与 cell_name 同时传入时优先"},
      "cell_name": {"type": "string", "description": "小区中文名或其连续片段，未提供 CGI 时使用包含匹配"},
      "dist_name": {"type": "string", "description": "地市名称"},
      "prod_name": {"type": "string", "description": "设备厂家"},
      "export_excel": {"type": "boolean", "description": "是否导出 Excel", "default": false}
    },
    "required": []
  }
}
```

- [x] **Step 3: 验证基线恢复**

Run:

```bash
env PYTHONPATH=. .venv/bin/pytest tests/test_agent_toolkit.py -q
```

Expected: PASS。

- [x] **Step 4: 提交独立基线修复**

```bash
git add tests/fixtures/tool_schemas.json
git commit -m "test(agent): sync parameter tool schema fixture"
```

### Task 2: 建立可复用的 V1.4 纯业务规则

**Files:**
- Create: `app/services/energy_analysis.py`
- Create: `tests/test_energy_analysis.py`

**Interfaces:**
- Consumes: 数据库行字典、关系距离、主/邻区站型和网络标识。
- Produces: `normalize_network_type`、`neighbor_policy`、`evaluate_neighbor_relation`、`build_expansion_record`、`enrich_constriction_records`。

- [x] **Step 1: 阅读测试规则并写失败测试**

Read: `/Users/schunm/.codex/skills/test-driven-development/writing-good-tests.md`

写入以下最小行为测试：

```python
from app.services.energy_analysis import (
    build_expansion_record,
    enrich_constriction_records,
    evaluate_neighbor_relation,
    neighbor_policy,
    normalize_network_type,
)


def test_expansion_record_preserves_database_boundary_and_continuous_fields():
    row = {
        "cgi": "460-00-1-1",
        "hour_detail": [{"hour": 22, "low_flow_pct": 90.0}],
        "hour_filter": "22",
        "hour_int": 1,
        "hour_filter_early": None,
        "hour_int_early": 0,
        "deploy_hours": "22,23,0",
        "deploy_hours_continuous": "22:00-00:59",
        "deploy_hours_early": None,
        "deploy_hours_continuous_early": None,
    }

    result = build_expansion_record(row)

    assert result["hour_detail"][0]["low_flow_pct"] == 90.0
    assert result["deploy_hours_continuous"] == "22:00-00:59"


def test_neighbor_policies_match_confirmed_site_rules():
    assert neighbor_policy("宏站") == (200.0, frozenset({"宏站"}))
    assert neighbor_policy("室分") == (100.0, frozenset({"宏站", "室分"}))
    assert neighbor_policy("微站") == (200.0, None)


def test_prb_increase_equal_ten_is_preserved():
    rows = [{"cgi": "main", "around_cgi": "neighbor", "prb_increase": 10.0}]
    result = enrich_constriction_records(
        rows,
        relation_by_pair={("main", "neighbor"): {"distance": 80.0, "network": "4G"}},
        site_type_by_cell={("5G", "main"): "室分", ("4G", "neighbor"): "宏站"},
    )
    assert result[0]["prb_increase"] == 10.0
```

- [x] **Step 2: 运行并观察模块缺失失败**

Run:

```bash
env PYTHONPATH=. .venv/bin/pytest tests/test_energy_analysis.py -v
```

Expected: FAIL with `ModuleNotFoundError: app.services.energy_analysis`。

- [x] **Step 3: 实现最小纯业务模块**

模块公开接口：

```python
from typing import Any, Literal

NetworkType = Literal["4G", "5G"]
RelationStatus = Literal["allowed", "out_of_range", "site_type_mismatch", "missing"]


def normalize_network_type(value: str | None) -> NetworkType | None: ...


def neighbor_policy(site_type: str | None) -> tuple[float, frozenset[str] | None]: ...


def evaluate_neighbor_relation(
    main_site_type: str | None,
    neighbor_site_type: str | None,
    distance: float | None,
) -> RelationStatus: ...


def build_expansion_record(row: dict[str, Any]) -> dict[str, Any]: ...


def enrich_constriction_records(
    rows: list[dict[str, Any]],
    relation_by_pair: dict[tuple[str, str], dict[str, Any]],
    site_type_by_cell: dict[tuple[NetworkType, str], str],
) -> list[dict[str, Any]]: ...
```

`enrich_constriction_records` 只丢弃明确的 `out_of_range` 和 `site_type_mismatch`；关系或站型缺失时保留记录并写入 `relation_status="missing"`。不得检查或过滤 `prb_increase`。

- [x] **Step 4: 验证规则测试通过**

Run:

```bash
env PYTHONPATH=. .venv/bin/pytest tests/test_energy_analysis.py -v
```

Expected: PASS。

- [x] **Step 5: 提交纯业务规则**

```bash
git add app/services/energy_analysis.py tests/test_energy_analysis.py
git commit -m "feat(energy): add v1.4 analysis rules"
```

### Task 3: 接入扩展预计算字段和完整原始结果

**Files:**
- Modify: `app/tools/energy_saving_tool.py`
- Create: `tests/test_energy_saving_tool.py`

**Interfaces:**
- Consumes: Task 2 的 `build_expansion_record`。
- Produces: `_query_expansion_data` 返回 `data`、`table` 和全部 V1.4 扩展字段；顶层工具返回 `expansion_data` 及截断元数据。

- [x] **Step 1: 写扩展查询失败测试**

测试替换 `energy_saving_tool.fetch_rows`，只允许一次 `jd_cell_expansion_day` 查询，并返回包含全部新增字段的行：

```python
async def test_query_expansion_uses_precomputed_v14_fields(monkeypatch):
    async def fake_fetch_rows(sql, params):
        assert "jd_cell_detail_hour_nr" not in str(sql)
        return [{
            "cgi": params["cgi"],
            "cell_name": "测试小区",
            "hour_detail": [{"hour": 22, "low_flow_pct": 90.0}],
            "hour_filter": "22",
            "hour_int": 1,
            "hour_filter_early": None,
            "hour_int_early": 0,
            "deploy_hours": "22,23,0",
            "deploy_hours_continuous": "22:00-00:59",
            "deploy_hours_early": None,
            "deploy_hours_continuous_early": None,
        }]
    monkeypatch.setattr(energy_saving_tool, "fetch_rows", fake_fetch_rows)

    result = await energy_saving_tool._query_expansion_data("460-00-1-1", datetime(2026, 8, 10))

    assert result["data"][0]["hour_int"] == 1
    assert result["data"][0]["deploy_hours_continuous"] == "22:00-00:59"
```

- [x] **Step 2: 验证测试因旧查询和旧字段失败**

Run:

```bash
env PYTHONPATH=. .venv/bin/pytest tests/test_energy_saving_tool.py::test_query_expansion_uses_precomputed_v14_fields -v
```

Expected: FAIL，因为旧实现继续查询 `jd_cell_detail_hour_nr` 且不返回连续部署字段。

- [x] **Step 3: 最小修改扩展查询和返回结构**

`jd_cell_expansion_day` SELECT 必须包含：

```sql
hour_detail, hour_filter, hour_int,
hour_filter_early, hour_int_early,
deploy_hours, deploy_hours_continuous,
deploy_hours_early, deploy_hours_continuous_early
```

删除 `_query_expansion_data` 中对 `jd_cell_detail_hour_nr` 的休眠均值查询和旧阈值判断。摘要表直接展示两组可扩展时段、数量和连续部署时段。顶层 `analyze_single_cell_energy` 恢复 `expansion_data`，并用 `truncate_and_export(..., prefix="single_cell_expansion")` 返回 `expansion_total_count`、`expansion_returned_count`、`expansion_is_truncated` 和 `expansion_download_url`。

- [x] **Step 4: 验证扩展测试与工具回归**

Run:

```bash
env PYTHONPATH=. .venv/bin/pytest tests/test_energy_saving_tool.py tests/test_agent_toolkit.py -q
```

Expected: PASS。

- [x] **Step 5: 提交扩展接入**

```bash
git add app/tools/energy_saving_tool.py tests/test_energy_saving_tool.py
git commit -m "feat(energy): consume v1.4 expansion results"
```

### Task 4: 批量补齐收缩距离和双方站型证据

**Files:**
- Modify: `app/tools/energy_saving_tool.py`
- Modify: `tests/test_energy_saving_tool.py`

**Interfaces:**
- Consumes: Task 2 的 `normalize_network_type` 和 `enrich_constriction_records`。
- Produces: `_query_constriction_data` 返回完整证据、关系状态和双方站型，不执行 N+1 查询。

- [x] **Step 1: 写收缩证据和批量查询失败测试**

测试提供一条 `prb_increase=10.0` 收缩记录、一个 `jd_cell_around` 关系和两张工参表结果，并记录每张表查询次数：

```python
async def test_query_constriction_enriches_relation_without_dropping_ten(monkeypatch):
    calls = []

    async def fake_fetch_rows(sql, params):
        query = str(sql)
        calls.append(query)
        if "jd_cell_constriction_day" in query:
            return [{
                "cgi": "main", "around_cgi": "neighbor", "hours": 0,
                "site_type": None, "around_cgi_network_type": "lte",
                "self_prb_rate_ul_before": 12.0, "self_prb_rate_dl_before": 5.0,
                "self_sleep_duration": 3600, "prb_rate_ul": 20.0,
                "prb_rate_dl": 60.0, "prb_rate_ul_before": 10.0,
                "prb_rate_dl_before": 50.0, "prb_increase": 10.0,
                "before_sleep_hour": 23, "before_sleep_date": date(2026, 8, 9),
            }]
        if "jd_cell_around" in query:
            return [{"cgi": "main", "around_cgi": "neighbor", "distance": 80.0, "around_cgi_network_type": "lte"}]
        if "nr_fix_prm" in query:
            return [{"cgi": "main", "site_type": "室分"}]
        if "lte_fix_prm" in query:
            return [{"cgi": "neighbor", "site_type": "宏站"}]
        raise AssertionError(query)

    monkeypatch.setattr(energy_saving_tool, "fetch_rows", fake_fetch_rows)
    result = await energy_saving_tool._query_constriction_data("main", datetime(2026, 8, 10))

    assert result["data"][0]["prb_increase"] == 10.0
    assert result["data"][0]["distance"] == 80.0
    assert result["data"][0]["main_site_type"] == "室分"
    assert result["data"][0]["around_site_type"] == "宏站"
    assert sum("lte_fix_prm" in query for query in calls) == 1
```

- [x] **Step 2: 运行并观察证据字段缺失失败**

Run:

```bash
env PYTHONPATH=. .venv/bin/pytest tests/test_energy_saving_tool.py::test_query_constriction_enriches_relation_without_dropping_ten -v
```

Expected: FAIL，因为旧实现只查询 `hours/reason/around_cgi`，不查询工参站型。

- [x] **Step 3: 实现集合查询和证据组装**

增加私有异步帮助函数：

```python
async def _query_neighbor_relations(
    cgi: str,
    around_cgis: set[str],
) -> dict[tuple[str, str], dict[str, Any]]: ...


async def _query_site_types(
    nr_cgis: set[str],
    lte_cgis: set[str],
) -> dict[tuple[str, str], str]: ...
```

工参查询使用安全常量表名和 `cgi = ANY(:cgis)`，每个网络最多一次查询，并限定 `data_date = (SELECT MAX(data_date) FROM table)`。收缩 SELECT 加入全部 V1.4 证据字段。调用 `enrich_constriction_records` 后生成包含休眠前后 PRB、抬升量、距离、主/邻区站型和关系状态的 Markdown 表。

- [x] **Step 4: 验证收缩规则、证据和无 N+1**

Run:

```bash
env PYTHONPATH=. .venv/bin/pytest tests/test_energy_analysis.py tests/test_energy_saving_tool.py -q
```

Expected: PASS。

- [x] **Step 5: 提交收缩接入**

```bash
git add app/tools/energy_saving_tool.py tests/test_energy_saving_tool.py
git commit -m "feat(energy): enrich v1.4 constriction evidence"
```

### Task 5: 补齐完整分析中的休眠前负荷和七日统计

**Files:**
- Modify: `app/tools/energy_saving_tool.py`
- Modify: `tests/test_energy_saving_tool.py`

**Interfaces:**
- Consumes: 现有 `_check_is_pre_sleep_hour` 和 `_query_pre_sleep_load_data`。
- Produces: `all`、`constriction`、`pre_sleep_load` 一致的休眠前负荷结果；七日高负荷仅统计真正的休眠前一小时。

- [x] **Step 1: 写路由和七日计数失败测试**

```python
def test_pre_sleep_load_is_required_for_all_and_constriction():
    assert energy_saving_tool._needs_pre_sleep_load("all") is True
    assert energy_saving_tool._needs_pre_sleep_load("constriction") is True
    assert energy_saving_tool._needs_pre_sleep_load("pre_sleep_load") is True
    assert energy_saving_tool._needs_pre_sleep_load("expansion") is False


def test_high_prb_days_only_counts_actual_pre_sleep_hours():
    rows = [
        {"stat_time": date(2026, 8, 9), "prb_hour": 23, "sleep_hour": "0", "prb_rate_ul": 51.0, "prb_rate_dl": 1.0},
        {"stat_time": date(2026, 8, 9), "prb_hour": 22, "sleep_hour": "0", "prb_rate_ul": 90.0, "prb_rate_dl": 1.0},
    ]
    assert energy_saving_tool._count_high_pre_sleep_days(rows) == 1
```

- [x] **Step 2: 运行并观察帮助函数缺失失败**

Run:

```bash
env PYTHONPATH=. .venv/bin/pytest tests/test_energy_saving_tool.py -k "pre_sleep" -v
```

Expected: FAIL because `_needs_pre_sleep_load` / `_count_high_pre_sleep_days` 不存在。

- [x] **Step 3: 实现路由和单小区七日明细计数**

`_query_pre_sleep_load_data` 的七日查询拉取单小区必要字段：

```sql
SELECT stat_time, prb_hour, sleep_hour, prb_rate_ul, prb_rate_dl
FROM jd_agent.jd_cell_pre_hour_busy
WHERE cgi = :cgi
  AND stat_time BETWEEN :start_date AND :end_date
```

生产库中这些数值字段可能包含空字符串，因此 SQL 不执行数值比较或小时转换。随后在 Python 中安全解析 PRB 利用率，使用 `_check_is_pre_sleep_hour` 过滤真实休眠前小时，并按日期去重计数。批量查询使用同一口径，且仅查询实际存在收缩记录的 CGI。`analyze_single_cell_energy` 对 `all` 和 `constriction` 与其他独立查询并行获取休眠前负荷，避免串行增加响应时间。

- [x] **Step 4: 验证完整分析和休眠前负荷测试**

Run:

```bash
env PYTHONPATH=. .venv/bin/pytest tests/test_energy_saving_tool.py -q
```

Expected: PASS。

- [x] **Step 5: 提交休眠前负荷闭环**

```bash
git add app/tools/energy_saving_tool.py tests/test_energy_saving_tool.py
git commit -m "fix(energy): include pre-sleep load in full analysis"
```

### Task 6: 修正批量统计并导出完整 V1.4 明细

**Files:**
- Modify: `app/tools/batch_energy_tool.py`
- Modify: `app/utils/export_util.py`
- Create: `tests/test_batch_energy_tool.py`
- Create: `tests/test_export_util.py`

**Interfaces:**
- Consumes: 扩展/收缩预计算行和 Task 5 的休眠前高 PRB 候选口径。
- Produces: 正确的批量统计、完整汇总表和多工作表 Excel 下载地址。

- [x] **Step 1: 写批量总数和 Excel 失败测试**

```python
def test_batch_total_is_counted_before_issue_filtering():
    table_data = [
        {"CGI": "a", "节能扩展：容量与风险说明": "非高负荷"},
        {"CGI": "b", "节能扩展：容量与风险说明": "高负荷"},
    ]
    stats = batch_energy_tool._compute_batch_stats(table_data)
    assert stats["total"] == 2


def test_parameter_noncompliant_count_excludes_missing_states():
    rows = [
        {"cgi": "a", "saving_switch_state": "不合格"},
        {"cgi": "b", "saving_switch_state": "北向数据缺失"},
        {"cgi": "c", "saving_switch_state": "合格"},
    ]
    assert batch_energy_tool._count_noncompliant_cells(rows) == 1


def test_export_sheets_creates_named_worksheets(tmp_path, monkeypatch):
    monkeypatch.setattr(export_util, "EXPORT_DIR", str(tmp_path))
    url = export_util.export_sheets_to_excel(
        {"小区汇总": [{"cgi": "a"}], "收缩明细": [{"cgi": "a", "prb_increase": 10.0}]},
        prefix="batch_analysis",
    )
    workbook = openpyxl.load_workbook(next(tmp_path.glob("*.xlsx")), read_only=True)
    assert workbook.sheetnames == ["小区汇总", "收缩明细"]
    assert url is not None
```

- [x] **Step 2: 运行并观察当前过滤后计数和导出接口缺失**

Run:

```bash
env PYTHONPATH=. .venv/bin/pytest tests/test_batch_energy_tool.py tests/test_export_util.py -v
```

Expected: FAIL because `_compute_batch_stats`、`_count_noncompliant_cells`、`export_sheets_to_excel` 不存在。

- [x] **Step 3: 实现批量统计和多表导出**

`export_sheets_to_excel` 签名：

```python
def export_sheets_to_excel(
    sheets: dict[str, list[dict]],
    prefix: str,
    column_mapping: dict[str, str] | None = None,
) -> str | None: ...
```

使用一个 `pd.ExcelWriter(..., engine="openpyxl")` 写入非空工作表。批量查询：

- 扩展 SELECT 增加全部连续部署字段。
- 收缩 SELECT 增加全部 PRB 证据字段。
- 休眠前候选查询增加 `stat_time <= :end_date` 和 PRB 阈值，Python 过滤真正休眠前一小时。
- `stats["total"]` 在问题过滤前按 CGI 去重计算。
- `stats["param_noncompliant"]` 只统计非“合格/北向数据缺失/空值”的 CGI。
- Excel 至少包含“小区汇总”“扩展明细”“收缩明细”“休眠前高负荷”工作表；无数据的明细表不创建。

- [x] **Step 4: 验证批量和导出测试**

Run:

```bash
env PYTHONPATH=. .venv/bin/pytest tests/test_batch_energy_tool.py tests/test_export_util.py -q
```

Expected: PASS。

- [x] **Step 5: 提交批量修正**

```bash
git add app/tools/batch_energy_tool.py app/utils/export_util.py tests/test_batch_energy_tool.py tests/test_export_util.py
git commit -m "fix(energy): correct batch analysis statistics"
```

### Task 7: 同步提示词、中文列名和工具契约

**Files:**
- Modify: `app/prompts/energy_saving.py`
- Modify: `app/utils/export_util.py`
- Modify: `tests/fixtures/tool_schemas.json` only if an intentional tool description/schema changes
- Modify: `tests/test_agent_toolkit.py`
- Create: `tests/test_energy_prompt.py`

**Interfaces:**
- Consumes: Tasks 3-6 的返回字段。
- Produces: V1.4 报告说明、稳定中文 Excel 列名和通过的工具 Schema contract。

- [x] **Step 1: 写提示词失败测试**

```python
def test_single_cell_prompt_uses_precomputed_v14_evidence():
    prompt = energy_saving.SYNTHESIS_SINGLE_CELL
    assert "deploy_hours_continuous" in prompt
    assert "prb_increase" in prompt
    assert "休眠前负荷" in prompt
    assert "低业务占比 > 90%" not in prompt
    assert "休眠时长低于60秒" not in prompt
```

- [x] **Step 2: 运行并观察旧口径失败**

Run:

```bash
env PYTHONPATH=. .venv/bin/pytest tests/test_energy_prompt.py -v
```

Expected: FAIL，因为提示词仍写旧阈值且未引用新增字段。

- [x] **Step 3: 更新提示词和列名映射**

`DEFAULT_COLUMN_MAPPING` 增加：

```python
{
    "hour_filter": "未休眠扩展时段小时列表(含扩展时段)",
    "hour_int": "未休眠可扩展时间数量(含扩展时段)",
    "hour_filter_early": "未休眠可扩展时段小时列表(仅夜间常规时段)",
    "hour_int_early": "未休眠可扩展时间数量(仅夜间常规时段)",
    "deploy_hours": "含已休眠部署时段_不连续(含扩展时段)",
    "deploy_hours_continuous": "含已休眠连续部署时段(含扩展时段)",
    "deploy_hours_early": "含已休眠部署时段_不连续(仅夜间常规时段)",
    "deploy_hours_continuous_early": "含已休眠连续部署时段(仅夜间常规时段)",
    "self_prb_rate_ul_before": "主小区休眠前上行PRB利用率",
    "self_prb_rate_dl_before": "主小区休眠前下行PRB利用率",
    "self_sleep_duration": "主小区深度+超级休眠时长(秒)",
    "prb_rate_ul": "邻区休眠期间上行PRB利用率",
    "prb_rate_dl": "邻区休眠期间下行PRB利用率",
    "prb_rate_ul_before": "邻区休眠前上行PRB利用率",
    "prb_rate_dl_before": "邻区休眠前下行PRB利用率",
    "prb_increase": "邻区PRB抬升量",
    "distance": "邻区距离(米)",
    "main_site_type": "主小区站型",
    "around_site_type": "邻区站型",
}
```

提示词说明结果来自数据库预计算字段，并要求完整分析输出连续部署和收缩证据。

- [x] **Step 4: 验证提示词、导出和 Schema contract**

Run:

```bash
env PYTHONPATH=. .venv/bin/pytest tests/test_energy_prompt.py tests/test_export_util.py tests/test_agent_toolkit.py -q
```

Expected: PASS。

- [x] **Step 5: 提交展示契约**

```bash
git add app/prompts/energy_saving.py app/utils/export_util.py tests/test_energy_prompt.py tests/test_export_util.py tests/test_agent_toolkit.py tests/fixtures/tool_schemas.json
git commit -m "feat(energy): expose v1.4 evidence in reports"
```

### Task 8: 全量验证、代码评审和分支收口

**Files:**
- Verify: all modified production and test files
- Update: `docs/superpowers/plans/2026-08-17-v14-energy-table-integration.md` checkbox status

**Interfaces:**
- Consumes: Tasks 1-7 的全部提交。
- Produces: 可审查、可推送、可集成到 `main` 的分支。

- [x] **Step 1: 运行目标测试**

```bash
env PYTHONPATH=. .venv/bin/pytest \
  tests/test_energy_analysis.py \
  tests/test_energy_saving_tool.py \
  tests/test_batch_energy_tool.py \
  tests/test_export_util.py \
  tests/test_energy_prompt.py \
  tests/test_agent_toolkit.py -q
```

Expected: PASS with zero failures。

- [x] **Step 2: 运行全套测试**

```bash
env PYTHONPATH=. .venv/bin/pytest -q
```

Expected: 所有非 live 测试通过；live 测试按现有环境条件 skip，不得新增 warning 或 failure。

- [x] **Step 3: 运行静态和差异检查**

```bash
.venv/bin/python -m compileall -q app tests
git diff origin/main...HEAD --check
git status --short --branch
```

Expected: compileall 和 diff check 退出码 0；工作区只包含预期的计划勾选更新。

- [x] **Step 4: 使用 requesting-code-review 做独立评审**

重点检查 SQL 参数绑定、集合查询次数、缺失关系保留策略、Excel 完整性、提示词字段名和向后兼容返回键。

- [x] **Step 5: 修复评审问题并重新运行 Steps 1-3**

任何生产修改都必须先新增能复现问题的失败测试，再实施修复。

- [x] **Step 6: 提交计划收口**

```bash
git add docs/superpowers/plans/2026-08-17-v14-energy-table-integration.md
git commit -m "docs(energy): complete v1.4 implementation plan"
```

- [x] **Step 7: 使用 finishing-a-development-branch 选择集成方式**

用户已要求最终合并到 `main` 时，先确认最新远程 `main`，将分支变基到最新 `origin/main`，重新运行 Steps 1-3，再执行非交互式合并与推送；不得把 Durable A2A 分支的 16 个架构提交一并带入。
