"""单小区节电 Markdown 报告测试。"""

from app.services.energy_report import build_single_cell_energy_report


def test_report_marks_missing_expansion_result_as_undetermined():
    """防止把缺失的扩展结果伪造为无可扩展时段。"""
    report = build_single_cell_energy_report({
        "analysis_target": "expansion", "cell_name": "测试小区", "cgi": "460-00-1-1",
        "stat_time": "2026-08-10", "expansion_result_status": "unavailable",
        "expansion_process_evidence_status": "missing", "whitelist_status": "unknown",
    })

    assert "暂不能判断是否需要扩展" in report
    assert "无可扩展时段" not in report
    assert "0小时" not in report


def test_report_uses_explicit_non_whitelist_and_only_ready_process_rows():
    """防止未知白名单状态被判否，或输出未就绪的过程行。"""
    report = build_single_cell_energy_report({
        "analysis_target": "expansion", "cell_name": "测试小区", "cgi": "460-00-1-1",
        "stat_time": "2026-08-10", "expansion_result_status": "no_candidate",
        "expansion_process_evidence_status": "partial", "expansion_process_table": "| 22:00-22:59 | 95.0% |",
        "expansion_candidate_table": "候选表", "expansion_deployment_table": "部署表",
        "param_check": {"success": True, "is_compliant": True},
        "whitelist_status": "not_whitelisted",
    })

    assert "已完成扩展评估，当前未识别需新增的扩展时段，保持现有配置" in report
    assert "该小区不是节电白名单小区" in report
    assert "23:00-23:59" not in report


def test_report_hides_expansion_tables_when_result_is_unavailable():
    """防止旧候选或部署表使不可用结果伪装成已完成评估。"""
    report = build_single_cell_energy_report({
        "analysis_target": "expansion", "expansion_result_status": "unavailable",
        "expansion_candidate_table": "无可扩展时段：0小时",
        "expansion_deployment_table": "无可扩展时段：0小时",
        "whitelist_status": "unknown",
    })

    assert "暂不能判断是否需要扩展" in report
    assert "无可扩展时段" not in report
    assert "0小时" not in report


def test_report_marks_indeterminate_param_check_as_unavailable():
    """防止未返回合规结论的成功请求被错误标记为不合规。"""
    report = build_single_cell_energy_report({
        "analysis_target": "expansion", "expansion_result_status": "unavailable",
        "param_check": {"success": True, "is_compliant": None},
        "whitelist_status": "unknown",
    })

    assert "参数核查结果暂不可用" in report
    assert "参数核查结果：不合规" not in report
