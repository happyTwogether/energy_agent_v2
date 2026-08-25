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

    assert "经过智能体分析，该小区没有可以扩展的时段" in report
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


def test_all_report_preserves_complete_auditable_evidence():
    """完整直出报告必须保留每类可复核证据和风险信息。"""
    report = build_single_cell_energy_report({
        "analysis_target": "all",
        "cell_name": "测试小区",
        "cgi": "460-00-1-1",
        "stat_time": "2026-08-10",
        "high_load_type": "下高",
        "expansion_result_status": "candidate_available",
        "expansion_process_evidence_status": "partial",
        "expansion_process_table": "扩展过程证据表",
        "expansion_candidate_table": "扩展候选表",
        "expansion_deployment_table": "扩展部署表",
        "expansion_data": [{"cgi": "460-00-1-1"}],
        "constriction_data": [{"around_cgi": "neighbor"}],
        "constriction_main_table": "收缩主小区条件表",
        "constriction_relation_table": "收缩关系范围表",
        "constriction_load_table": "收缩邻区负荷表",
        "constriction_result_table": "收缩结果表",
        "pre_sleep_load_data": [{"cgi": "460-00-1-1"}],
        "pre_sleep_load_table": "休眠前负荷表",
        "high_prb_days_7d": 3,
        "param_check": {
            "success": True,
            "is_compliant": False,
            "unqualified_items": [{
                "param_name": "符号关断开关",
                "current_value": "关闭",
                "suggested_value": "开启",
                "impact": "无法进入节能状态",
            }],
        },
        "whitelist_status": "whitelisted",
        "whitelist_reason": "重要活动保障",
        "jd_starttime": "2026-08-01",
        "jd_endtime": "2026-08-31",
    })

    for evidence in (
        "扩展过程证据表",
        "扩展候选表",
        "扩展部署表",
        "收缩主小区条件表",
        "收缩关系范围表",
        "收缩邻区负荷表",
        "收缩结果表",
        "休眠前负荷表",
        "符号关断开关",
        "关闭",
        "开启",
        "无法进入节能状态",
        "重要活动保障",
        "2026-08-01",
        "2026-08-31",
    ):
        assert evidence in report


def test_all_report_uses_business_conclusions_without_empty_evidence_sections():
    """成功空结果是业务结论，不应渲染缺证据标题。"""
    report = build_single_cell_energy_report({
        "analysis_target": "all",
        "expansion_result_status": "no_candidate",
        "expansion_process_evidence_status": "missing",
        "expansion_data": [],
        "constriction_data": [],
        "pre_sleep_load_data": [],
        "param_check": {"success": False, "is_compliant": None},
        "whitelist_status": "not_whitelisted",
    })

    assert "经过智能体分析，该小区没有可以扩展的时段" in report
    assert "该小区没有需要剔除的原节电时段" in report
    assert "经智能体分析，近7天未发现休眠生效前持续高负荷情况" in report
    assert "该小区不是节电白名单小区" in report
    for title in (
        "扩展候选时段",
        "扩展部署时段",
        "主小区休眠条件核查",
        "周边小区关联范围核查",
        "周边小区负荷变化",
        "需收缩节电时段",
        "休眠前本小区负荷",
    ):
        assert f"### {title}" not in report
    assert "未获得" not in report
    assert "| — |" not in report


def test_report_orders_horizontal_expansion_detail_and_param_check():
    report = build_single_cell_energy_report({
        "analysis_target": "expansion",
        "expansion_result_status": "candidate_available",
        "high_load_type": "下高",
        "expansion_process_table": "过程表",
        "expansion_detail_table": "| 可扩展休眠小时详情 | 是否白名单 |\n|---|---|\n| 22点 | 否 |",
        "expansion_deployment_table": "连续部署表",
        "param_check": {"success": True, "is_compliant": True},
        "whitelist_status": "not_whitelisted",
    })

    assert "实施扩展前需先完成相应方向的负荷压降" in report
    assert "字段中文名称" not in report
    assert "| 可扩展休眠小时详情 | 是否白名单 |" in report
    assert report.index("容量与风险说明") < report.index("低业务与休眠情况分析")
    assert report.index("低业务与休眠情况分析") < report.index("扩展结果明细")
    assert report.index("扩展结果明细") < report.index("连续部署结果")
    assert report.index("连续部署结果") < report.index("参数核查")


def test_report_separates_neighbor_impact_from_pre_sleep_analysis():
    report = build_single_cell_energy_report({
        "analysis_target": "constriction",
        "constriction_data": [{"hours": 1}],
        "current_sleep_hours": [0, 1],
        "constriction_main_table": "主小区表",
        "constriction_relation_table": "关系表",
        "constriction_load_table": "负荷表",
        "constriction_result_table": "需剔除表",
        "pre_sleep_load_data": [{"cgi": "a"}],
        "pre_sleep_load_table": "休眠前表",
        "high_prb_days_7d": 4,
        "whitelist_status": "not_whitelisted",
    })

    assert "### 周边200米关联小区高负荷影响判断" in report
    assert "当前已生效的休眠时间点：0、1点" in report
    assert "#### 需剔除的原节电时段" in report
    assert "### 休眠生效前高负荷分析" in report
    assert "调整后的建议节能时间节点" not in report


def test_load_report_uses_explicit_high_load_state():
    """仅负荷报告应明确输出结构化高负荷状态。"""
    report = build_single_cell_energy_report({
        "analysis_target": "load",
        "high_load_type": "双高",
        "whitelist_status": "unknown",
    })

    assert "## 负荷状态" in report
    assert "双高" in report
    assert "上下行高负荷" in report


def test_pre_sleep_empty_detail_keeps_seven_day_high_load_risk():
    """当日无明细时仍须保留独立计算的近七日风险结论。"""
    report = build_single_cell_energy_report({
        "analysis_target": "pre_sleep_load",
        "pre_sleep_load_data": [],
        "high_prb_days_7d": 4,
        "whitelist_status": "unknown",
    })

    assert "未获得休眠前负荷证据" not in report
    assert "近 7 天休眠前高负荷天数：4 天" in report
    assert "持续高负荷风险" in report
    assert "| — |" not in report


def test_report_overview_uses_untruncated_constriction_total_count():
    """收缩概览应使用原始总数，而不是聊天返回的截断行数。"""
    report = build_single_cell_energy_report({
        "analysis_target": "constriction",
        "constriction_data": [{"around_cgi": "neighbor"}],
        "constriction_total_count": 51,
        "whitelist_status": "unknown",
    })

    assert "共获得 51 条可复核收缩记录" in report
    assert "共获得 1 条可复核收缩记录" not in report


def test_param_check_table_escapes_markdown_control_characters():
    """参数值不得借助竖线或换行破坏 Markdown 表格结构。"""
    report = build_single_cell_energy_report({
        "analysis_target": "expansion",
        "expansion_result_status": "unavailable",
        "param_check": {
            "success": True,
            "is_compliant": False,
            "unqualified_items": [{
                "param_name": "参数\\名|甲\n乙",
                "current_value": "开\\关|值",
                "suggested_value": "建议\r\n值",
                "impact": "影响\r内容",
            }],
        },
        "whitelist_status": "unknown",
    })

    assert "参数\\\\名\\|甲<br>乙" in report
    assert "开\\\\关\\|值" in report
    assert "建议<br>值" in report
    assert "影响<br>内容" in report


def test_report_overview_preserves_nonempty_date_note():
    """自动回退日期的原因应原样进入确定性报告概览。"""
    date_note = (
        "您查询的日期 2026-08-20 暂无数据，"
        "已自动查询最新数据日期 2026-08-10"
    )
    report = build_single_cell_energy_report({
        "analysis_target": "load",
        "stat_time": "2026-08-10",
        "date_note": date_note,
        "whitelist_status": "unknown",
    })

    assert date_note in report
