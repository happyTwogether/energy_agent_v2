"""节电报告提示词的 V1.4 契约测试。"""

from app.prompts import energy_saving


def test_single_cell_prompt_uses_precomputed_v14_evidence():
    prompt = energy_saving.SYNTHESIS_SINGLE_CELL

    assert "deploy_hours_continuous" in prompt
    assert "prb_increase" in prompt
    assert "休眠前负荷" in prompt
    assert "低业务占比 > 90%" not in prompt
    assert "休眠时长 < 60秒" not in prompt


def test_batch_prompt_mentions_corrected_v14_statistics():
    prompt = energy_saving.SYNTHESIS_BATCH_CELLS

    assert "param_noncompliant" in prompt
    assert "high_sleep_count" in prompt
    assert "四个工作表" in prompt
