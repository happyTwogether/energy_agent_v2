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
    assert "expansion_process_evidence_status" in prompt
    assert "whitelist_status" in prompt
    assert "whitelisted" in prompt
    assert "not_whitelisted" in prompt
    assert "unknown" in prompt
    assert "未命中节电白名单" not in prompt


def test_batch_prompt_mentions_corrected_v14_statistics():
    prompt = energy_saving.SYNTHESIS_BATCH_CELLS

    assert "param_noncompliant" in prompt
    assert "high_sleep_count" in prompt
    assert "四个工作表" in prompt
