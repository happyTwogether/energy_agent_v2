"""
指标注册表：统一管理所有可查询的指标定义。

每个指标定义包含：
- columns_lte / columns_nr: 查询需要的原始列名
- calc: 计算函数 (row, lte_cell_total) -> 最终值
- label: 中文显示名
- unit: 单位
- table: 数据表类型 "report" | "cell"
"""

from typing import Any, Callable

from app.core.config import get_settings

DB_SCHEMA = get_settings().db_schema


def to_wan_du(value: float | None) -> float:
    try:
        return round(float(value or 0) / 10000, 2)
    except (TypeError, ValueError):
        return 0.0


def to_tb(value: float | None) -> float:
    try:
        return round(float(value or 0) / 1024, 2)
    except (TypeError, ValueError):
        return 0.0


def _identity(value: float | None) -> float:
    try:
        return round(float(value or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def safe_div(a: float | None, b: float | None, decimals: int = 2) -> float:
    """安全除法：返回 a/b 的结果，b 为 0 或转换失败时返回 0.0。"""
    try:
        bval = float(b or 0)
        if bval == 0:
            return 0.0
        return round(float(a or 0) / bval, decimals)
    except (TypeError, ValueError):
        return 0.0


def safe_div_row(row: dict, col_a: str, col_b: str, decimals: int = 2) -> float:
    try:
        bval = float(row.get(col_b) or 0)
        if bval == 0:
            return 0.0
        return round(float(row.get(col_a) or 0) / bval, decimals)
    except (TypeError, ValueError):
        return 0.0


MetricDef = dict[str, Any]

# ============================================================
# 汇总级指标 — 来源: lte_report_day_collect / nr_report_day_collect
# ============================================================
REPORT_METRICS: dict[str, MetricDef] = {
    # ── 能耗 ──
    "bbu_energy": {
        "columns_lte": ["bbu_power"],
        "columns_nr": ["sa_bbu_power"],
        "calc": to_wan_du,
        "label": "BBU能耗",
        "unit": "万度",
        "table": "report",
    },
    "rru_energy": {
        "columns_lte": ["rru_power"],
        "columns_nr": ["rru_power"],
        "calc": to_wan_du,
        "label": "RRU/AAU能耗",
        "unit": "万度",
        "table": "report",
    },
    "station_energy": {
        "columns_lte": ["lte_station_power"],
        "columns_nr": ["nr_sa_station_power"],
        "calc": to_wan_du,
        "label": "基站总能耗",
        "unit": "万度",
        "table": "report",
    },
    "month_energy_saving": {
        "columns_lte": ["lte_curmonthpower"],
        "columns_nr": ["nr_curmonthpower"],
        "calc": to_wan_du,
        "label": "月节电量",
        "unit": "万度",
        "table": "report",
    },
    "single_station_power": {
        "columns_lte": ["single_station_power"],
        "columns_nr": ["single_station_power"],
        "calc": _identity,
        "label": "单站能耗",
        "unit": "kwh",
        "table": "report",
    },

    # ── 流量 ──
    "traffic": {
        "columns_lte": ["upoctul_dl"],
        "columns_nr": ["upoctul_dl"],
        "calc": to_tb,
        "label": "上下行业务量",
        "unit": "TB",
        "table": "report",
    },

    # ── 能效 ──
    "avg_energy_efficiency": {
        "columns_lte": ["avg_energy_efficiency"],
        "columns_nr": ["sa_avg_energy_efficiency"],
        "calc": _identity,
        "label": "平均能效",
        "unit": "",
        "table": "report",
    },
    "low_energy_cell_count": {
        "columns_lte": ["low_energy_total"],
        "columns_nr": ["low_energy_total"],
        "calc": _identity,
        "label": "低能效小区数",
        "unit": "个",
        "table": "report",
    },

    # ── 网络规模 ──
    "cell_total": {
        "columns_lte": ["all_cell_total"],
        "columns_nr": ["all_cell_total"],
        "calc": _identity,
        "label": "小区总数",
        "unit": "个",
        "table": "report",
    },
    "station_total": {
        "columns_lte": ["logic_station_total"],
        "columns_nr": ["logic_station_total"],
        "calc": _identity,
        "label": "逻辑站总数",
        "unit": "个",
        "table": "report",
    },
    "station_online": {
        "columns_lte": ["logic_read_station_total"],
        "columns_nr": ["logic_read_station_total"],
        "calc": _identity,
        "label": "在线基站数",
        "unit": "个",
        "table": "report",
    },
    "station_online_ratio": {
        "columns_lte": ["logic_station_total", "logic_read_station_total"],
        "columns_nr": ["logic_station_total", "logic_read_station_total"],
        "calc": lambda row: safe_div_row(row, "logic_read_station_total", "logic_station_total") * 100 if row.get("logic_station_total") else 0.0,
        "label": "基站在线率",
        "unit": "%",
        "table": "report",
    },

    # ── 节电 ──
    "symbol_shutdown_on_rate": {
        "columns_lte": ["open_symdown_rate"],
        "columns_nr": ["open_symdown_rate"],
        "calc": _identity,
        "label": "符号关断开启率",
        "unit": "%",
        "table": "report",
    },
    "symbol_shutdown_hour": {
        "columns_lte": ["symdown_effect_hour"],
        "columns_nr": ["symdown_effect_hour"],
        "calc": _identity,
        "label": "符号关断总时长",
        "unit": "小时",
        "table": "report",
    },
    "channel_shutdown_on_rate": {
        "columns_lte": ["open_chandown_rate"],
        "columns_nr": ["open_chandown_rate"],
        "calc": _identity,
        "label": "通道关断开启率",
        "unit": "%",
        "table": "report",
    },
    "channel_shutdown_hour": {
        "columns_lte": ["chandown_effect_hour"],
        "columns_nr": ["chandown_effect_hour"],
        "calc": _identity,
        "label": "通道关断总时长",
        "unit": "小时",
        "table": "report",
    },
    "carrier_shutdown_hour": {
        "columns_lte": ["carrdown_effect_hour"],
        "columns_nr": ["carrdown_effect_hour"],
        "calc": _identity,
        "label": "载波关断总时长",
        "unit": "小时",
        "table": "report",
    },
    "deepsleep_on_rate": {
        "columns_lte": ["open_deepsleep_rate"],
        "columns_nr": ["open_deepsleep_rate"],
        "calc": _identity,
        "label": "深度休眠开启率",
        "unit": "%",
        "table": "report",
    },
    "deepsleep_hour": {
        "columns_lte": ["deepsleep_effect_hour"],
        "columns_nr": ["deepsleep_effect_hour"],
        "calc": _identity,
        "label": "深度休眠总时长",
        "unit": "小时",
        "table": "report",
    },
    "supersleep_hour": {
        "columns_lte": None,
        "columns_nr": ["aaurru_supersleep_effect_hour"],
        "calc": _identity,
        "label": "极致休眠总时长",
        "unit": "小时",
        "table": "report",
    },
    "energy_saving_rate": {
        "columns_lte": ["lte_curmonthpower_rate"],
        "columns_nr": ["nr_curmonthpower_rate"],
        "calc": _identity,
        "label": "节电率",
        "unit": "%",
        "table": "report",
    },
}

# ============================================================
# 小区级指标 — 来源: lte_report_day_detail / nr_report_day_detail
# ============================================================
CELL_METRICS: dict[str, MetricDef] = {
    "cell_traffic": {
        "columns_lte": ["upoctul_dl"],
        "columns_nr": ["upoctul_dl"],
        "calc": _identity,
        "label": "小区流量",
        "unit": "GB",
        "table": "cell",
    },
    "cell_carrier_shutdown_hour": {
        "columns_lte": ["carrier_shutdown_hour"],
        "columns_nr": ["carrier_shutdown_hour"],
        "calc": _identity,
        "label": "载波关断时长",
        "unit": "小时",
        "table": "cell",
    },
    "cell_channel_shutdown_hour": {
        "columns_lte": ["channel_shutdown_hour"],
        "columns_nr": ["channel_shutdown_hour"],
        "calc": _identity,
        "label": "通道关断时长",
        "unit": "小时",
        "table": "cell",
    },
    "cell_symbol_shutdown_hour": {
        "columns_lte": ["symbol_shutdown_hour"],
        "columns_nr": ["symbol_shutdown_hour"],
        "calc": _identity,
        "label": "符号关断时长",
        "unit": "小时",
        "table": "cell",
    },
    "cell_deepsleep_hour": {
        "columns_lte": ["deepsleep_hour"],
        "columns_nr": ["deepsleep_hour"],
        "calc": _identity,
        "label": "深度休眠时长",
        "unit": "小时",
        "table": "cell",
    },
    "cell_supersleep_hour": {
        "columns_lte": None,
        "columns_nr": ["supersleep_hour"],
        "calc": _identity,
        "label": "极致休眠时长",
        "unit": "小时",
        "table": "cell",
    },
}

# ============================================================
# 合并所有指标
# ============================================================
ALL_METRICS: dict[str, MetricDef] = {**REPORT_METRICS, **CELL_METRICS}
