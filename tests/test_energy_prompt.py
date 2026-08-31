"""节电报告提示词的 V1.4 契约测试。"""

from app.prompts import energy_saving


def test_single_cell_prompt_uses_business_oriented_auditable_evidence():
    """防止报告漏用过程表、白名单有效期或暴露实现细节。"""
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
    assert "V1.4 预计算" not in prompt
    assert "扩展时段减去收缩时段" not in prompt
    assert "同长度选择最晚" not in prompt
    assert "命中天数/有效天数" not in prompt
    assert "参数核查暂不可用" in prompt
    assert "expansion_result_status" in prompt
    assert "暂不能判断是否需要扩展" in prompt
    assert "whitelist_status" in prompt
    assert "whitelisted" in prompt
    assert "not_whitelisted" in prompt
    assert "unknown" in prompt
    assert "未命中节电白名单" not in prompt
    assert "经过智能体分析，该小区没有可以扩展的时段" in prompt
    assert "当前已生效的休眠时间点" in prompt
    assert "需剔除的原节电时段" in prompt
    assert "直接原样返回 `report_content`" in prompt


def test_batch_prompt_mentions_corrected_v14_statistics():
    prompt = energy_saving.SYNTHESIS_BATCH_CELLS

    assert "param_noncompliant" in prompt
    assert "high_sleep_count" in prompt
    assert "仅扩展为“扩展明细”" in prompt
    assert "仅收缩为“收缩明细”" in prompt
    assert "全量为“小区汇总、扩展明细、收缩明细”" in prompt


def test_execution_prompt_routes_generic_fields_without_stealing_specialists():
    prompt = energy_saving.AGENT_EXECUTION_PROMPT

    assert "query_business_data" in prompt
    assert "专业工具优先" in prompt
    assert "查询 nr_report_day_detail 的 deepsleep_hour 字段" in prompt
    assert "rsrp_avg" not in prompt
    assert "查询扩展时段、收缩时段和周边小区距离" in prompt
    assert "银盆岭小学节电空间" in prompt
    assert "不要改写成 SQL" in prompt


def test_business_data_synthesis_uses_structured_rows_for_agent_diagnosis():
    prompt = energy_saving.SYNTHESIS_BUSINESS_DATA

    assert "rows" in prompt
    assert "metric_definitions" in prompt
    assert "比较、诊断" in prompt
    assert "原样返回 `report_content`" not in prompt
