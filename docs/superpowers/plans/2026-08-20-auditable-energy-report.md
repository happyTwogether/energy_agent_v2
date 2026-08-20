# Auditable Energy Report Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore hour-level expansion evidence, split contraction evidence into readable process tables, and generate a business-oriented single-cell report with both continuous deployment windows and complete whitelist information.

**Architecture:** Keep database access and Markdown table construction in `app/tools/energy_saving_tool.py`, move deterministic normalization and evidence calculations into `app/services/energy_analysis.py`, and let `app/prompts/energy_saving.py` assemble only the approved report structure. Preserve existing raw result and export keys for backward compatibility while adding focused process-table keys.

**Tech Stack:** Python 3.11, asyncio, SQLAlchemy 2.x, AgentScope 2.x, pytest, pytest-asyncio

## Global Constraints

- Work only in the current AgentScope `app/` production path; do not change the Durable A2A/MCP runtime.
- Do not modify database schemas or reimplement the V1.4 expansion/constriction source algorithm.
- Expansion window is `[22:00, next-day 08:00)` using hours `22, 23, 0..7`; regular window is `[00:00, 06:00)` using hours `0..5`.
- Treat `low_flow_pct >= 90%` as low business, but use `hour_filter` membership as the final expansion decision.
- Show the two continuous deployment results without explaining their interval-construction algorithm.
- Preserve constriction rows with `prb_increase = 10.0`.
- Never show “数据库预计算”, SQL implementation details, low-flow numerator/denominator, or a global “expansion minus constriction” time calculation in user-facing report copy.
- Full, expansion, and constriction analyses retain whitelist status; the full report shows reason, start date, end date, and risk warning in an independent section.
- A parameter-check failure is unknown/unavailable, never compliant.
- Preserve current raw data, truncation metadata, Excel export, batch analysis, and single-dimension query contracts.

---

### Task 1: Add deterministic report-evidence helpers

**Files:**
- Modify: `app/services/energy_analysis.py:1-172`
- Modify: `tests/test_energy_analysis.py:1-159`

**Interfaces:**
- Consumes: expansion records containing `hour_detail` and `hour_filter`; sleep rows containing `hours` and `avg_sleep_sum`; enriched constriction rows.
- Produces: `normalize_boolean_flag(value: Any) -> bool`, `build_expansion_hour_evidence(record: dict[str, Any], sleep_rows: list[dict[str, Any]]) -> list[dict[str, Any]]`, and PRB maximum fields on records returned by `enrich_constriction_records`.

- [x] **Step 1: Write failing expansion-evidence and whitelist normalization tests**

Add imports and tests to `tests/test_energy_analysis.py`:

```python
from app.services.energy_analysis import (
    build_expansion_hour_evidence,
    normalize_boolean_flag,
)


def test_boolean_flags_do_not_treat_chinese_no_as_true():
    assert normalize_boolean_flag("是") is True
    assert normalize_boolean_flag("否") is False
    assert normalize_boolean_flag("true") is True
    assert normalize_boolean_flag(None) is False


def test_expansion_hour_evidence_keeps_source_result_as_decision():
    record = {
        "hour_detail": [
            {"hour": 22, "low_flow_pct": 90.0},
            {"hour": 23, "low_flow_pct": 95.0},
        ],
        "hour_filter": "22",
    }
    sleep_rows = [
        {"hours": 22, "avg_sleep_sum": 0},
        {"hours": 23, "avg_sleep_sum": 0},
    ]

    evidence = build_expansion_hour_evidence(record, sleep_rows)

    assert [item["hour"] for item in evidence] == [22, 23, 0, 1, 2, 3, 4, 5, 6, 7]
    assert evidence[0] == {
        "hour": 22,
        "low_flow_pct": 90.0,
        "is_low_business": True,
        "avg_sleep_seconds": 0.0,
        "is_zero_sleep": True,
        "suggestion": "可扩展",
    }
    assert evidence[1]["suggestion"] == "以综合分析结果为准"
    assert evidence[2]["suggestion"] == "数据缺失"
```

- [x] **Step 2: Run the new tests and verify failure**

Run:

```bash
PYTHONPATH=. uv run --with-requirements requirements.txt --with pytest --with pytest-asyncio \
  pytest tests/test_energy_analysis.py::test_boolean_flags_do_not_treat_chinese_no_as_true \
         tests/test_energy_analysis.py::test_expansion_hour_evidence_keeps_source_result_as_decision -v
```

Expected: collection fails because the two helper functions do not exist.

- [x] **Step 3: Implement small parsing and expansion-evidence helpers**

Add to `app/services/energy_analysis.py`:

```python
EXPANSION_HOURS = (22, 23, 0, 1, 2, 3, 4, 5, 6, 7)
LOW_FLOW_PCT_THRESHOLD = 90.0
_TRUE_FLAGS = frozenset({"1", "true", "yes", "y", "是"})


def normalize_boolean_flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in _TRUE_FLAGS


def _hour_set(value: Any) -> set[int]:
    if isinstance(value, list):
        parts = value
    elif value is None:
        parts = []
    else:
        parts = str(value).strip("[]").split(",")
    result: set[int] = set()
    for part in parts:
        try:
            result.add(int(str(part).strip()))
        except (TypeError, ValueError):
            continue
    return result


def _expansion_suggestion(
    hour: int,
    expandable_hours: set[int],
    low_business: bool | None,
    zero_sleep: bool | None,
) -> str:
    if hour in expandable_hours:
        return "可扩展"
    if low_business is None or zero_sleep is None:
        return "数据缺失"
    if not low_business:
        return "不建议（低业务占比不足）"
    if not zero_sleep:
        return "不建议（已有休眠）"
    return "以综合分析结果为准"


def _build_expansion_hour_row(
    hour: int,
    detail_by_hour: dict[int, Any],
    sleep_by_hour: dict[int, Any],
    expandable_hours: set[int],
) -> dict[str, Any]:
    percent = detail_by_hour.get(hour)
    sleep_seconds = sleep_by_hour.get(hour)
    low_business = None if percent is None else float(percent) >= LOW_FLOW_PCT_THRESHOLD
    zero_sleep = None if sleep_seconds is None else float(sleep_seconds) == 0
    return {
        "hour": hour,
        "low_flow_pct": None if percent is None else float(percent),
        "is_low_business": low_business,
        "avg_sleep_seconds": None if sleep_seconds is None else float(sleep_seconds),
        "is_zero_sleep": zero_sleep,
        "suggestion": _expansion_suggestion(
            hour, expandable_hours, low_business, zero_sleep,
        ),
    }


def build_expansion_hour_evidence(
    record: dict[str, Any],
    sleep_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    detail_by_hour = {
        item.get("hour"): item.get("low_flow_pct")
        for item in record.get("hour_detail", [])
        if item.get("hour") is not None
    }
    sleep_by_hour = {row.get("hours"): row.get("avg_sleep_sum") for row in sleep_rows}
    expandable_hours = _hour_set(record.get("hour_filter"))
    return [
        _build_expansion_hour_row(
            hour, detail_by_hour, sleep_by_hour, expandable_hours,
        )
        for hour in EXPANSION_HOURS
    ]
```

Keep each helper focused and close to 30 lines; use the explicit row-construction helper above.

- [x] **Step 4: Write and run failing PRB maximum enrichment test**

Extend `test_prb_increase_equal_ten_is_preserved_and_enriched` input with PRB values and assert:

```python
assert result[0]["main_prb_before"] == 12.0
assert result[0]["around_prb_before"] == 50.0
assert result[0]["around_prb_during"] == 60.0
```

Run:

```bash
PYTHONPATH=. uv run --with-requirements requirements.txt --with pytest --with pytest-asyncio \
  pytest tests/test_energy_analysis.py::test_prb_increase_equal_ten_is_preserved_and_enriched -v
```

Expected: FAIL because maximum fields are absent.

- [x] **Step 5: Add PRB maximum fields during constriction enrichment**

Add a null-safe helper and include its results in `_enrich_constriction_record`:

```python
def _max_available(*values: Any) -> float | None:
    present = [float(value) for value in values if value is not None]
    return max(present) if present else None
```

Return these additional keys:

```python
"main_prb_before": _max_available(
    row.get("self_prb_rate_ul_before"), row.get("self_prb_rate_dl_before"),
),
"around_prb_before": _max_available(
    row.get("prb_rate_ul_before"), row.get("prb_rate_dl_before"),
),
"around_prb_during": _max_available(
    row.get("prb_rate_ul"), row.get("prb_rate_dl"),
),
```

- [x] **Step 6: Run service tests**

Run:

```bash
PYTHONPATH=. uv run --with-requirements requirements.txt --with pytest --with pytest-asyncio \
  pytest tests/test_energy_analysis.py -q
```

Expected: all service tests pass.

- [x] **Step 7: Commit the business evidence helpers**

```bash
git add app/services/energy_analysis.py tests/test_energy_analysis.py
git diff --cached --check
git commit -m "feat(energy): model auditable report evidence"
```

---

### Task 2: Restore expansion process evidence and dual-window tables

**Files:**
- Modify: `app/tools/energy_saving_tool.py:12-346`
- Modify: `tests/test_energy_saving_tool.py:31-110`

**Interfaces:**
- Consumes: `build_expansion_record`, `build_expansion_hour_evidence`, `normalize_boolean_flag`, and independent-session `fetch_rows`.
- Produces from `_query_expansion_data`: `data`, `process_data`, `process_table`, `candidate_table`, `deployment_table`, legacy-compatible `table`, whitelist metadata, and base cell metadata.
- Produces from `analyze_single_cell_energy`: `expansion_process_data`, `expansion_process_table`, `expansion_candidate_table`, and `expansion_deployment_table` in addition to existing expansion keys.

- [x] **Step 1: Replace the old expansion query test with a failing two-source test**

Update `tests/test_energy_saving_tool.py` so the fake returns one expansion row for `jd_cell_expansion_day` and sleep evidence for `jd_cell_detail_hour_nr`:

```python
@pytest.mark.asyncio
async def test_query_expansion_returns_process_and_dual_window_tables(monkeypatch):
    queries: list[str] = []

    async def fake_fetch_rows(sql, params):
        query = str(sql)
        queries.append(query)
        if "jd_cell_detail_hour_nr" in query:
            assert params["night_hours"] == [22, 23, 0, 1, 2, 3, 4, 5, 6, 7]
            assert params["start_date"] == datetime(2026, 7, 27)
            return [{"hours": 22, "avg_sleep_sum": 0}]
        return [{
            "cgi": params["cgi"],
            "cell_name": "测试小区",
            "hour_detail": [{"hour": 22, "low_flow_pct": 90.0}],
            "hour_filter": "22",
            "hour_int": 1,
            "hour_filter_early": "0",
            "hour_int_early": 1,
            "deploy_hours": "22,23,0",
            "deploy_hours_continuous": "22:00-00:59",
            "deploy_hours_early": "0,1,2,3,4,5",
            "deploy_hours_continuous_early": "00:00-05:59",
            "is_whitelist": "否",
        }]

    monkeypatch.setattr(energy_saving_tool, "fetch_rows", fake_fetch_rows)
    result = await energy_saving_tool._query_expansion_data(
        "460-00-1-1", datetime(2026, 8, 10),
    )

    assert len(queries) == 2
    assert result["process_data"][0]["suggestion"] == "可扩展"
    assert "低业务占比" in result["process_table"]
    assert "22:00-00:59" in result["deployment_table"]
    assert "0,1,2,3,4,5" in result["deployment_table"]
    assert result["is_whitelist"] is False
```

- [x] **Step 2: Run the new expansion test and verify failure**

Run:

```bash
PYTHONPATH=. uv run --with-requirements requirements.txt --with pytest --with pytest-asyncio \
  pytest tests/test_energy_saving_tool.py::test_query_expansion_returns_process_and_dual_window_tables -v
```

Expected: FAIL because only the expansion result table is queried and the new tables are absent.

- [x] **Step 3: Query the 15-day sleep process data in parallel**

In `app/tools/energy_saving_tool.py`, import the new helpers and define:

```python
EXPANSION_LOOKBACK_DAYS = 14
EXPANSION_HOURS = [22, 23, 0, 1, 2, 3, 4, 5, 6, 7]
```

Add a sleep query beside the existing expansion query:

```python
sleep_sql = text(f"""
    SELECT hours,
           AVG(
               CASE
                   WHEN ee_shallowsleeptimerru IS NOT NULL
                    AND ee_deepsleeptimerru IS NOT NULL
                    AND ee_supersleeptimerru IS NOT NULL
                   THEN ee_shallowsleeptimerru
                      + ee_deepsleeptimerru
                      + ee_supersleeptimerru
               END
           ) AS avg_sleep_sum
    FROM {DB_SCHEMA_AGENT}.jd_cell_detail_hour_nr
    WHERE cgi = :cgi
      AND stat_time >= :start_date
      AND stat_time <= :end_date
      AND hours = ANY(:night_hours)
    GROUP BY hours
    ORDER BY hours
""")
```

Use `asyncio.gather` with two `fetch_rows` calls and `start_date = stat_time - timedelta(days=14)`.

- [x] **Step 4: Build three focused expansion tables**

Use `build_expansion_hour_evidence(base_row, sleep_rows)` and create:

```python
process_table = _build_expansion_process_table(process_data)
candidate_table = _build_md_table(
    ["分析时窗", "可扩展小时", "可扩展时长"],
    [
        f"| 22:00至次日08:00 | {base_row.get('hour_filter') or '无可扩展时段'} | {base_row.get('hour_int') or 0}小时 |",
        f"| 00:00至06:00 | {base_row.get('hour_filter_early') or '无可扩展时段'} | {base_row.get('hour_int_early') or 0}小时 |",
    ],
)
deployment_table = _build_md_table(
    ["分析口径", "分析后的部署小时集合", "连续部署时段"],
    [
        f"| 含扩展时段 | {base_row.get('deploy_hours') or '—'} | {base_row.get('deploy_hours_continuous') or '—'} |",
        f"| 仅常规夜间时段 | {base_row.get('deploy_hours_early') or '—'} | {base_row.get('deploy_hours_continuous_early') or '—'} |",
    ],
)
```

`_build_expansion_process_table` formats `None` as “数据缺失”, booleans as “是/否”, percentage with `%`, sleep seconds with `_format_sleep_duration`, and includes all 10 target hours. Return `table` as `candidate_table + "\n\n" + deployment_table` for backward compatibility.

- [x] **Step 5: Propagate expansion process tables and unknown parameter status**

In `analyze_single_cell_energy`, add:

```python
result["expansion_process_data"] = expansion_result["process_data"]
result["expansion_process_table"] = expansion_result["process_table"]
result["expansion_candidate_table"] = expansion_result["candidate_table"]
result["expansion_deployment_table"] = expansion_result["deployment_table"]
```

Replace the parameter default with:

```python
param_check_raw = results_map.get(
    "param_check",
    {"success": False, "is_compliant": None, "error": "参数核查暂不可用"},
)
result["param_check"] = {
    "success": param_check_raw.get("success", False),
    "is_compliant": param_check_raw.get("is_compliant"),
    "unqualified_count": param_check_raw.get("unqualified_count", 0),
}
if param_check_raw.get("error"):
    result["param_check"]["error"] = param_check_raw["error"]
```

Keep existing unqualified-item truncation only when `is_compliant is False`.

- [x] **Step 6: Add response propagation and missing-data tests**

Extend `test_single_cell_response_exposes_expansion_raw_data` to include and assert the three table keys and `expansion_process_data`. Add a test where sleep rows omit an hour and assert the process table says “数据缺失” instead of `0秒`.

- [x] **Step 7: Run tool-focused tests**

Run:

```bash
PYTHONPATH=. uv run --with-requirements requirements.txt --with pytest --with pytest-asyncio \
  pytest tests/test_energy_saving_tool.py -q
```

Expected: all single-cell tool tests pass.

- [x] **Step 8: Commit expansion report evidence**

```bash
git add app/tools/energy_saving_tool.py tests/test_energy_saving_tool.py
git diff --cached --check
git commit -m "feat(energy): restore expansion process evidence"
```

---

### Task 3: Split constriction evidence and complete whitelist metadata

**Files:**
- Modify: `app/tools/energy_saving_tool.py:205-467,571-704`
- Modify: `tests/test_energy_saving_tool.py:192-319`

**Interfaces:**
- Consumes: enriched constriction records with normalized network, distance, site types, relation status, and PRB maxima.
- Produces from `_query_constriction_data`: `main_table`, `relation_table`, `load_table`, `result_table`, legacy `table`, and whitelist status/reason/start/end.
- Produces from `_query_pre_sleep_load_data`: raw `prb_rate_ul`, `prb_rate_dl`, maximum `prb_rate`, and an updated table containing all three values.

- [x] **Step 1: Write failing multi-table constriction test**

Extend `test_query_constriction_enriches_relation_without_dropping_ten` with:

```python
assert "休眠前最高PRB" in result["main_table"]
assert "80.0米" in result["relation_table"]
assert "50.0%" in result["load_table"]
assert "60.0%" in result["load_table"]
assert "10.0个百分点" in result["load_table"]
assert "剔除" in result["result_table"]
```

Include `starttime`, `endtime`, and `is_whitelist="否"` in the fake constriction row and assert normalized status plus formatted dates.

- [x] **Step 2: Run the constriction test and verify failure**

Run:

```bash
PYTHONPATH=. uv run --with-requirements requirements.txt --with pytest --with pytest-asyncio \
  pytest tests/test_energy_saving_tool.py::test_query_constriction_enriches_relation_without_dropping_ten -v
```

Expected: FAIL because focused tables and whitelist dates are absent.

- [x] **Step 3: Select whitelist dates and build four focused tables**

Add `starttime` and `endtime` to the constriction SELECT. Replace the single wide table builder with four small builders:

```python
main_table = _build_constriction_main_table(constriction_data)
relation_table = _build_constriction_relation_table(constriction_data)
load_table = _build_constriction_load_table(constriction_data)
result_table = _build_constriction_result_table(constriction_data)
```

Deduplicate the main-cell condition table by `(hours, before_sleep_date, before_sleep_hour)` so multiple affected neighbors do not repeat the same main-cell evidence. Use these exact headers:

```python
["休眠时段", "休眠前时间", "休眠前上行PRB", "休眠前下行PRB", "休眠前最高PRB", "休眠时长(秒)", "判断"]
["关联小区", "制式", "距离", "主小区站型", "关联小区站型", "关系判断"]
["主小区休眠时段", "关联小区", "休眠前PRB(上/下行)", "休眠期间PRB(上/下行)", "休眠前最高PRB", "休眠期间最高PRB", "抬升量", "影响判断"]
["原节电时段", "触发原因", "关联小区", "关键证据", "收缩建议"]
```

Map `allowed` to “满足分析范围” and `missing` to “关系信息待核实”. Since rows already represent source constriction results, use “受到影响” and “剔除” without applying another PRB threshold.

- [x] **Step 4: Preserve legacy and new constriction response keys**

Return `table=result_table` for compatibility and add all four new table keys. In `analyze_single_cell_energy`, propagate:

```python
result["constriction_main_table"] = constriction_result["main_table"]
result["constriction_relation_table"] = constriction_result["relation_table"]
result["constriction_load_table"] = constriction_result["load_table"]
result["constriction_result_table"] = constriction_result["result_table"]
```

For constriction-only responses, also propagate `jd_starttime` and `jd_endtime`. Normalize both expansion and constriction whitelist flags with `normalize_boolean_flag`.

- [x] **Step 5: Add upstream/downstream PRB to pre-sleep output**

In `_query_pre_sleep_load_data`, retain:

```python
"prb_rate_ul": round(float(prb_rate_ul), 2),
"prb_rate_dl": round(float(prb_rate_dl), 2),
"prb_rate": round(float(prb_rate), 2),
```

Update the Markdown headers to include “上行PRB利用率(%)”, “下行PRB利用率(%)”, and “无线利用率(%)”. Update row rendering accordingly.

- [x] **Step 6: Add whitelist and pre-sleep regression tests**

Add a response test that sets expansion `is_whitelist="是"`, reason and dates, then asserts:

```python
assert result["is_whitelist"] is True
assert result["whitelist_reason"] == "重要活动保障"
assert result["jd_starttime"] == "2026-08-01"
assert result["jd_endtime"] == "2026-08-31"
```

Extend the pre-sleep query test with one detail row and assert the raw and table output contain both UL and DL values.

- [x] **Step 7: Run focused service and tool tests**

Run:

```bash
PYTHONPATH=. uv run --with-requirements requirements.txt --with pytest --with pytest-asyncio \
  pytest tests/test_energy_analysis.py tests/test_energy_saving_tool.py -q
```

Expected: all tests pass.

- [x] **Step 8: Commit constriction and whitelist evidence**

```bash
git add app/tools/energy_saving_tool.py tests/test_energy_saving_tool.py
git diff --cached --check
git commit -m "feat(energy): structure constriction report evidence"
```

---

### Task 4: Enforce the approved report contract in the synthesis prompt

**Files:**
- Modify: `app/prompts/energy_saving.py:123-176`
- Modify: `tests/test_energy_prompt.py:1-20`

**Interfaces:**
- Consumes: new expansion and constriction table keys plus existing `param_check`, `pre_sleep_load_table`, whitelist, and truncation metadata.
- Produces: a single-cell synthesis contract with overview, expansion, constriction, embedded pre-sleep evidence, and whitelist sections.

- [x] **Step 1: Write failing prompt-contract tests**

Replace the current single-cell prompt test with:

```python
def test_single_cell_prompt_uses_business_oriented_auditable_evidence():
    prompt = energy_saving.SYNTHESIS_SINGLE_CELL

    for key in (
        "expansion_process_table",
        "expansion_candidate_table",
        "expansion_deployment_table",
        "constriction_main_table",
        "constriction_relation_table",
        "constriction_load_table",
        "constriction_result_table",
        "pre_sleep_load_table",
        "jd_starttime",
        "jd_endtime",
    ):
        assert key in prompt
    assert "数据库预计算" not in prompt
    assert "扩展时段减去收缩时段" not in prompt
    assert "同长度选择最晚" not in prompt
    assert "命中天数/有效天数" not in prompt
    assert "参数核查暂不可用" in prompt
    assert "未命中节电白名单" in prompt
```

- [x] **Step 2: Run the prompt test and verify failure**

Run:

```bash
PYTHONPATH=. uv run --with-requirements requirements.txt --with pytest --with pytest-asyncio \
  pytest tests/test_energy_prompt.py::test_single_cell_prompt_uses_business_oriented_auditable_evidence -v
```

Expected: FAIL on missing table keys and forbidden implementation language.

- [x] **Step 3: Rewrite `SYNTHESIS_SINGLE_CELL` around approved sections**

Use this exact module order:

```text
all            -> 概览结论 / 一、节能扩展 / 二、节能收缩 / 三、特殊情况备注
expansion      -> 概览结论 / 一、节能扩展 / 二、特殊情况备注
constriction   -> 概览结论 / 一、节能收缩 / 二、特殊情况备注
pre_sleep_load -> 概览结论 / 一、休眠前负荷
load           -> 概览结论（仅高负荷）
```

Require the expansion section to render, in order: capacity/risk, `expansion_process_table`, `expansion_candidate_table`, `expansion_deployment_table`, parameter result, and comprehensive expansion recommendation. State that both continuous deployment values are displayed directly with no construction explanation.

Require the constriction section to render, in order: conclusion, `constriction_main_table`, `constriction_relation_table`, `constriction_load_table`, `constriction_result_table`, `pre_sleep_load_table`, and comprehensive constriction recommendation. Keep `prb_increase=10.0` and never perform a second filter.

Require the whitelist section to use `is_whitelist`, `whitelist_reason`, `jd_starttime`, and `jd_endtime`; render “未命中节电白名单” when false, and “未提供” for missing reason/date when true.

For `param_check.is_compliant is None` or `param_check.success is false`, require “参数核查暂不可用” rather than a compliance claim.

- [x] **Step 4: Run all prompt and single-cell tests**

Run:

```bash
PYTHONPATH=. uv run --with-requirements requirements.txt --with pytest --with pytest-asyncio \
  pytest tests/test_energy_prompt.py tests/test_energy_analysis.py tests/test_energy_saving_tool.py -q
```

Expected: all tests pass.

- [x] **Step 5: Commit the report prompt contract**

```bash
git add app/prompts/energy_saving.py tests/test_energy_prompt.py
git diff --cached --check
git commit -m "feat(prompt): render auditable energy report"
```

---

### Task 5: Full verification, documentation status, and integration

**Files:**
- Modify: `docs/superpowers/specs/2026-08-20-auditable-energy-report-design.md:5`
- Modify: `docs/superpowers/plans/2026-08-20-auditable-energy-report.md` (checkbox status only during execution)

**Interfaces:**
- Consumes: all implementation tasks and the clean `origin/main` baseline.
- Produces: verified feature commits ready for merge into `main`.

- [x] **Step 1: Run the complete test suite**

Run:

```bash
PYTHONPATH=. uv run --with-requirements requirements.txt --with pytest --with pytest-asyncio pytest -q
```

Expected: at least the baseline `69 passed, 4 skipped` with all new tests passing and no failures.

- [x] **Step 2: Run syntax and diff verification**

```bash
PYTHONPATH=. uv run --with-requirements requirements.txt python -m compileall -q app tests
git diff origin/main...HEAD --check
git status --short
```

Expected: compile succeeds, diff check is empty, and only intentional plan checkbox/status edits remain.

- [ ] **Step 3: Mark the design implemented and commit documentation**

Change the design status from `待用户审核` to `已实施`. Mark completed plan checkboxes, then run:

```bash
git add docs/superpowers/specs/2026-08-20-auditable-energy-report-design.md \
        docs/superpowers/plans/2026-08-20-auditable-energy-report.md
git diff --cached --check
git commit -m "docs(energy): record report implementation"
```

- [x] **Step 4: Review the complete branch diff**

```bash
git log --oneline origin/main..HEAD
git diff --stat origin/main...HEAD
git diff origin/main...HEAD -- app/services/energy_analysis.py \
  app/tools/energy_saving_tool.py app/prompts/energy_saving.py tests
```

Confirm there are no unrelated changes, secrets, debug output, destructive database operations, or changes to the Durable A2A/MCP path.

- [ ] **Step 5: Refresh and merge into main**

```bash
git fetch origin main
git rebase origin/main
git -C /private/tmp/energy-agent-main-merge status --short
git -C /private/tmp/energy-agent-main-merge merge --ff-only codex/energy-report-audit-format
```

If the main worktree is not clean, stop and inspect; do not discard changes. After merge, rerun the complete suite in the main worktree.

- [ ] **Step 6: Push verified main**

```bash
git -C /private/tmp/energy-agent-main-merge push origin main
git -C /private/tmp/energy-agent-main-merge status --short --branch
git -C /private/tmp/energy-agent-main-merge log -3 --oneline --decorate
```

Expected: `main` and `origin/main` point to the same final documentation commit and the main worktree is clean.
