"""日报与自助查询共用的确定性指标计算公式。"""


def to_wan_du(value: float | None) -> float:
    """将 kWh 换算为万度。"""
    try:
        return round(float(value or 0) / 10_000, 2)
    except (TypeError, ValueError):
        return 0.0


def to_tb(value: float | None) -> float:
    """将 GB 换算为 TB。"""
    try:
        return round(float(value or 0) / 1_024, 2)
    except (TypeError, ValueError):
        return 0.0


def safe_div(
    numerator: float | None,
    denominator: float | None,
    decimals: int = 2,
) -> float:
    """安全除法：分母为 0 或无法转换时返回 0.0。"""
    try:
        denominator_value = float(denominator or 0)
        if denominator_value == 0:
            return 0.0
        return round(float(numerator or 0) / denominator_value, decimals)
    except (TypeError, ValueError):
        return 0.0


def average_effect_hour(
    total_effect_hour: float | None,
    cell_total: float | None,
) -> float:
    """按日报口径计算单小区平均节电生效时长。"""
    return safe_div(total_effect_hour, cell_total, 1)
