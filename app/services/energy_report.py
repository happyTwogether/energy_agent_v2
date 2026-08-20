"""确定性渲染单小区节电 Markdown 报告。"""

from __future__ import annotations

from typing import Any


def _title_and_overview(result: dict[str, Any]) -> list[str]:
    cell_name = result.get("cell_name") or result.get("cgi") or "未知小区"
    lines = [f"# {cell_name} 节电分析报告", "", "## 概览"]
    for label, key in (("CGI", "cgi"), ("统计日期", "stat_time"), ("分析目标", "analysis_target")):
        if result.get(key):
            lines.append(f"- {label}：{result[key]}")
    return lines


def _append_table(lines: list[str], title: str, table: Any) -> None:
    if isinstance(table, str) and table.strip():
        lines.extend(["", f"### {title}", table.strip()])


def _expansion_status(status: Any) -> str:
    if status == "unavailable":
        return "暂不能判断是否需要扩展。"
    if status == "no_candidate":
        return "已完成扩展评估，当前未识别需新增的扩展时段，保持现有配置。"
    if status == "candidate_available":
        return "已识别需新增的扩展时段。"
    return "扩展结果状态暂不可知。"


def _append_expansion(lines: list[str], result: dict[str, Any]) -> None:
    lines.extend(["", "## 休眠扩展", _expansion_status(result.get("expansion_result_status"))])
    if result.get("expansion_process_evidence_status") in {"complete", "partial"}:
        _append_table(lines, "扩展过程证据", result.get("expansion_process_table"))
    if result.get("expansion_result_status") in {"no_candidate", "candidate_available"}:
        _append_table(lines, "扩展候选时段", result.get("expansion_candidate_table"))
        _append_table(lines, "扩展部署时段", result.get("expansion_deployment_table"))


def _append_constriction(lines: list[str], result: dict[str, Any]) -> None:
    lines.extend(["", "## 休眠收缩"])
    _append_table(lines, "收缩分析", result.get("constriction_table"))


def _append_param_check(lines: list[str], result: dict[str, Any]) -> None:
    param_check = result.get("param_check")
    if not isinstance(param_check, dict):
        return
    lines.extend(["", "## 参数核查"])
    if param_check.get("success") is True:
        if param_check.get("is_compliant") is True:
            lines.append("参数核查结果：合规。")
        elif param_check.get("is_compliant") is False:
            lines.append("参数核查结果：不合规。")
        else:
            lines.append("参数核查结果暂不可用。")
    else:
        lines.append("参数核查结果暂不可用。")


def _append_whitelist(lines: list[str], result: dict[str, Any]) -> None:
    status = result.get("whitelist_status")
    lines.extend(["", "## 白名单"])
    messages = {
        "whitelisted": "该小区是节电白名单小区。",
        "not_whitelisted": "该小区不是节电白名单小区。",
        "unknown": "暂不能判断该小区是否为节电白名单小区。",
    }
    lines.append(messages.get(status, "节电白名单状态暂不可知。"))


def build_single_cell_energy_report(result: dict[str, Any]) -> str:
    """将已获得的单小区分析结果确定性地渲染为 Markdown。"""
    lines = _title_and_overview(result)
    target = result.get("analysis_target")
    if target in {"all", "expansion"}:
        _append_expansion(lines, result)
        _append_param_check(lines, result)
    if target in {"all", "constriction"}:
        _append_constriction(lines, result)
    if target == "pre_sleep_load":
        _append_table(lines, "休眠生效前负荷", result.get("pre_sleep_load_table"))
    _append_whitelist(lines, result)
    return "\n".join(lines)
