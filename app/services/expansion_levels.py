"""节电扩展策略及其数据库结果表映射。"""

from dataclasses import dataclass


DEFAULT_EXPANSION_LEVEL = "conservative"


@dataclass(frozen=True, slots=True)
class ExpansionLevelConfig:
    """一个受控的节电扩展策略。"""

    key: str
    label: str
    traffic_threshold_mbps: int
    table_name: str

    @property
    def criteria_text(self) -> str:
        return (
            f"{self.label}（连续不少于15天，低于"
            f"{self.traffic_threshold_mbps}M的概率不低于90%）"
        )


EXPANSION_LEVELS: dict[str, ExpansionLevelConfig] = {
    "conservative": ExpansionLevelConfig(
        key="conservative",
        label="保守扩展",
        traffic_threshold_mbps=300,
        table_name="jd_cell_expansion_day",
    ),
    "moderate": ExpansionLevelConfig(
        key="moderate",
        label="中等扩展",
        traffic_threshold_mbps=400,
        table_name="jd_cell_expansion_day_400",
    ),
    "aggressive": ExpansionLevelConfig(
        key="aggressive",
        label="激进扩展",
        traffic_threshold_mbps=500,
        table_name="jd_cell_expansion_day_500",
    ),
}


def resolve_expansion_level(
    expansion_level: str | None,
) -> ExpansionLevelConfig:
    """解析工具枚举值，拒绝未登记的数据库表选择。"""
    normalized = (expansion_level or DEFAULT_EXPANSION_LEVEL).strip().lower()
    config = EXPANSION_LEVELS.get(normalized)
    if config is None:
        supported = "、".join(EXPANSION_LEVELS)
        raise ValueError(f"不支持的扩展档位，可选值为：{supported}。")
    return config


def expansion_level_metadata(config: ExpansionLevelConfig) -> dict[str, object]:
    """返回可安全暴露给报告和调用方的策略元数据。"""
    return {
        "expansion_level": config.key,
        "expansion_level_label": config.label,
        "expansion_threshold_mbps": config.traffic_threshold_mbps,
        "expansion_criteria": config.criteria_text,
    }


def expansion_level_tip(expansion_level: str | None) -> str:
    """生成当前档位以外的两个可查询选项。"""
    selected = resolve_expansion_level(expansion_level)
    alternatives = [
        f"{config.label}（{config.traffic_threshold_mbps}M）"
        for config in EXPANSION_LEVELS.values()
        if config.key != selected.key
    ]
    alternative_text = "或".join(alternatives)
    return f"> 还可选择{alternative_text}查看对应结果。"
