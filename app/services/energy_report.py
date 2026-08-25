"""确定性渲染单小区节电 Markdown 报告。"""

from __future__ import annotations

from typing import Any


def _title_and_overview(result: dict[str, Any]) -> list[str]:
    cell_name = result.get("cell_name") or result.get("cgi") or "未知小区"
    lines = [f"# {cell_name} 节电分析报告", "", "## 概览结论"]
    for label, key in (
        ("CGI", "cgi"),
        ("统计日期", "stat_time"),
        ("分析目标", "analysis_target"),
        ("日期说明", "date_note"),
    ):
        if result.get(key):
            lines.append(f"- {label}：{result[key]}")
    if result.get("analysis_target") in {"all", "expansion"}:
        lines.append(f"- 扩展结论：{_expansion_status(result.get('expansion_result_status'))}")
    if result.get("analysis_target") in {"all", "constriction"}:
        count = result.get("constriction_total_count")
        if not isinstance(count, int):
            count = len(result.get("constriction_data") or [])
        if count:
            lines.append(f"- 收缩结论：共获得 {count} 条可复核收缩记录。")
        else:
            lines.append("- 收缩结论：该小区没有需要剔除的原节电时段。")
    return lines


def _append_table(lines: list[str], title: str, table: Any) -> bool:
    if not isinstance(table, str) or not table.strip():
        return False
    lines.extend(["", f"### {title}", table.strip()])
    return True


def _append_table_or_note(
    lines: list[str],
    title: str,
    table: Any,
    note: str,
) -> None:
    if not _append_table(lines, title, table):
        lines.extend(["", f"### {title}", note])


def _append_truncation_notice(
    lines: list[str],
    prefix: str,
    result: dict[str, Any],
) -> None:
    if result.get(f"{prefix}_is_truncated") is not True:
        return
    lines.extend(["", "仅展示部分数据。"])
    download_url = result.get(f"{prefix}_download_url")
    if isinstance(download_url, str) and download_url:
        lines.append(f"📥 [下载完整数据]({download_url})")


def _expansion_status(status: Any) -> str:
    messages = {
        "unavailable": "暂不能判断是否需要扩展。",
        "no_candidate": "经过智能体分析，该小区没有可以扩展的时段。",
        "candidate_available": "已识别需新增的扩展时段。",
    }
    return messages.get(status, "扩展结果状态暂不可知。")


def _capacity_risk(high_load_type: Any) -> str:
    if high_load_type in {"上高", "下高", "双高"}:
        return f"当前为{high_load_type}，实施扩展前需先完成相应方向的负荷压降。"
    if high_load_type == "否":
        return "当前非高负荷，具备继续评估扩展空间的容量条件。"
    return "容量风险状态暂不可用。"


def _load_status(high_load_type: Any) -> str:
    messages = {
        "上高": "当前负荷状态：上高（上行高负荷）。",
        "下高": "当前负荷状态：下高（下行高负荷）。",
        "双高": "当前负荷状态：双高（上下行高负荷）。",
        "否": "当前负荷状态：非高负荷。",
    }
    return messages.get(high_load_type, "当前负荷状态：未知。")


def _append_load(lines: list[str], result: dict[str, Any]) -> None:
    lines.extend(["", "## 负荷状态", _load_status(result.get("high_load_type"))])


def _append_expansion(lines: list[str], result: dict[str, Any]) -> None:
    lines.extend([
        "",
        "## 休眠扩展",
        _expansion_status(result.get("expansion_result_status")),
        "",
        "### 容量与风险说明",
        _capacity_risk(result.get("high_load_type")),
    ])
    _append_table(lines, "低业务与休眠情况分析", result.get("expansion_process_table"))
    _append_table(lines, "扩展结果明细", result.get("expansion_detail_table"))
    if result.get("expansion_result_status") == "candidate_available":
        _append_table(lines, "扩展候选时段", result.get("expansion_candidate_table"))
        _append_table(lines, "连续部署结果", result.get("expansion_deployment_table"))


def _append_constriction(lines: list[str], result: dict[str, Any]) -> None:
    lines.extend(["", "## 休眠收缩"])
    has_result = bool(result.get("constriction_data"))
    if not has_result:
        lines.append("该小区没有需要剔除的原节电时段。")
        return
    lines.extend(["", "### 周边200米关联小区高负荷影响判断"])
    sleep_hours = result.get("current_sleep_hours") or []
    if sleep_hours:
        lines.append("当前已生效的休眠时间点：" + "、".join(map(str, sleep_hours)) + "点。")
    for title, key in (
        ("主小区休眠条件", "constriction_main_table"),
        ("周边小区关联范围", "constriction_relation_table"),
        ("周边小区负荷变化", "constriction_load_table"),
        ("需剔除的原节电时段", "constriction_result_table"),
        ("收缩结果明细", "constriction_detail_table"),
    ):
        table = result.get(key)
        if isinstance(table, str) and table.strip():
            lines.extend(["", f"#### {title}", table.strip()])
    _append_truncation_notice(lines, "constriction", result)


def _item_value(item: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if item.get(key) not in (None, ""):
            return item[key]
    return "未提供"


def _markdown_cell(value: Any) -> str:
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("\n", "<br>")


def _param_items_table(items: list[dict[str, Any]]) -> str:
    rows = ["| 参数项 | 当前值 | 建议值 | 影响 |", "|---|---|---|---|"]
    for item in items:
        rows.append(
            "| {name} | {current} | {suggested} | {impact} |".format(
                name=_markdown_cell(
                    _item_value(item, "param_name", "saving_para_name"),
                ),
                current=_markdown_cell(
                    _item_value(item, "current_value", "saving_para_value"),
                ),
                suggested=_markdown_cell(
                    _item_value(item, "suggested_value", "recommend_value"),
                ),
                impact=_markdown_cell(_item_value(
                    item, "impact", "objective_reason", "subjective_reason",
                )),
            ),
        )
    return "\n".join(rows)


def _append_param_check(lines: list[str], result: dict[str, Any]) -> None:
    param_check = result.get("param_check")
    if not isinstance(param_check, dict):
        return
    lines.extend(["", "## 参数核查"])
    if param_check.get("success") is not True or param_check.get("is_compliant") is None:
        lines.append("参数核查结果暂不可用。")
        return
    if param_check.get("is_compliant") is True:
        lines.append("参数核查结果：合规。")
        return
    lines.append("参数核查结果：不合规。")
    items = param_check.get("unqualified_items") or []
    lines.append(_param_items_table(items) if items else "不合规项明细暂不可用。")


def _append_pre_sleep_load(lines: list[str], result: dict[str, Any]) -> None:
    lines.extend(["", "### 休眠生效前高负荷分析"])
    if result.get("pre_sleep_load_data") == []:
        high_days = result.get("high_prb_days_7d")
        if not high_days:
            lines.append("经智能体分析，近7天未发现休眠生效前持续高负荷情况。")
    else:
        table = result.get("pre_sleep_load_table")
        if isinstance(table, str) and table.strip():
            lines.extend(["", "#### 休眠前本小区负荷", table.strip()])
    high_days = result.get("high_prb_days_7d")
    if high_days:
        lines.append(f"近 7 天休眠前高负荷天数：{high_days} 天。")
    if isinstance(high_days, (int, float)) and high_days >= 3:
        lines.append("存在持续高负荷风险，建议优先收缩相关时段。")
    _append_truncation_notice(lines, "pre_sleep_load", result)


def _append_whitelist(lines: list[str], result: dict[str, Any]) -> None:
    status = result.get("whitelist_status")
    lines.extend(["", "## 白名单"])
    messages = {
        "whitelisted": "该小区是节电白名单小区。",
        "not_whitelisted": "该小区不是节电白名单小区。",
        "unknown": "暂不能判断该小区是否为节电白名单小区。",
    }
    lines.append(messages.get(status, "节电白名单状态暂不可知。"))
    if status == "whitelisted":
        lines.extend([
            f"- 白名单原因：{result.get('whitelist_reason') or '未提供'}",
            f"- 生效开始时间：{result.get('jd_starttime') or '未提供'}",
            f"- 生效结束时间：{result.get('jd_endtime') or '未提供'}",
            "- 修改节能配置前须确认业务影响。",
        ])


def build_single_cell_energy_report(result: dict[str, Any]) -> str:
    """将已获得的单小区分析结果确定性地渲染为 Markdown。"""
    lines = _title_and_overview(result)
    _append_table(lines, "基础信息", result.get("base_info_table"))
    target = result.get("analysis_target")
    if target in {"all", "expansion"}:
        _append_expansion(lines, result)
        _append_param_check(lines, result)
        lines.extend(["", "### 综合扩展建议", _expansion_status(result.get("expansion_result_status"))])
    if target in {"all", "constriction"}:
        _append_constriction(lines, result)
        _append_pre_sleep_load(lines, result)
    if target == "pre_sleep_load":
        _append_pre_sleep_load(lines, result)
    if target == "load":
        _append_load(lines, result)
    _append_whitelist(lines, result)
    return "\n".join(lines)
