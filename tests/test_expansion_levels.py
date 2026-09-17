"""节电扩展三档策略的固定映射测试。"""

import pytest

from app.services.expansion_levels import (
    DEFAULT_EXPANSION_LEVEL,
    expansion_level_metadata,
    resolve_expansion_level,
)


@pytest.mark.parametrize(
    ("level", "label", "threshold", "table_name"),
    [
        ("conservative", "保守扩展", 300, "jd_cell_expansion_day"),
        ("moderate", "中等扩展", 400, "jd_cell_expansion_day_400"),
        ("aggressive", "激进扩展", 500, "jd_cell_expansion_day_500"),
    ],
)
def test_expansion_level_uses_fixed_table_mapping(
    level,
    label,
    threshold,
    table_name,
):
    config = resolve_expansion_level(level)

    assert config.label == label
    assert config.traffic_threshold_mbps == threshold
    assert config.table_name == table_name


def test_expansion_level_defaults_to_conservative_metadata():
    config = resolve_expansion_level(None)

    assert config.key == DEFAULT_EXPANSION_LEVEL
    assert expansion_level_metadata(config) == {
        "expansion_level": "conservative",
        "expansion_level_label": "保守扩展",
        "expansion_threshold_mbps": 300,
        "expansion_criteria": "保守扩展（连续不少于15天，低于300M的概率不低于90%）",
    }


def test_expansion_level_rejects_unknown_table_selector():
    with pytest.raises(ValueError, match="不支持的扩展档位"):
        resolve_expansion_level("jd_cell_expansion_day_custom")
