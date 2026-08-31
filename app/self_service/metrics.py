"""业务数据自服务使用的确定性计算指标。"""

from collections.abc import Callable
from typing import Any

from app.core.metrics_registry import (
    average_effect_hour,
    safe_div,
    to_tb,
    to_wan_du,
)
from app.self_service.models import CatalogMetric

MetricCalculator = Callable[[dict[str, Any]], float]


def _ratio(row: dict[str, Any], numerator: str, denominator: str) -> float:
    return safe_div(row.get(numerator), row.get(denominator), 4) * 100


def _sum_ratio(
    row: dict[str, Any],
    numerators: tuple[str, ...],
    denominator: str,
) -> float:
    numerator = sum(float(row.get(name) or 0) for name in numerators)
    return safe_div(numerator, row.get(denominator), 4) * 100


def _average_hour(row: dict[str, Any], field: str) -> float:
    return average_effect_hour(row.get(field), row.get("all_cell_total"))


CALCULATORS: dict[str, MetricCalculator] = {
    "to_wan_du": lambda row: to_wan_du(row.get("value")),
    "to_tb": lambda row: to_tb(row.get("value")),
    "lte_readable_ratio": lambda row: _ratio(
        row, "logic_read_station_total", "logic_station_total",
    ),
    "nr_readable_ratio": lambda row: _ratio(
        row, "logic_read_station_total", "logic_station_total",
    ),
    "lte_high_power_ratio": lambda row: _ratio(
        row, "eightm_channel_total", "all_cell_total",
    ),
    "nr_high_power_ratio": lambda row: _sum_ratio(
        row,
        ("thirtytwo_channel_total", "sixtyfour_channel_total"),
        "all_cell_total",
    ),
    "low_efficiency_ratio": lambda row: _ratio(
        row, "low_energy_total", "all_cell_total",
    ),
    "commode_ratio": lambda row: _ratio(
        row, "commode_station_total", "logic_station_total",
    ),
    "symbol_average_hour": lambda row: _average_hour(
        row, "symdown_effect_hour",
    ),
    "channel_average_hour": lambda row: _average_hour(
        row, "chandown_effect_hour",
    ),
    "carrier_average_hour": lambda row: _average_hour(
        row, "carrdown_effect_hour",
    ),
    "deep_sleep_average_hour": lambda row: _average_hour(
        row, "deepsleep_effect_hour",
    ),
    "super_sleep_average_hour": lambda row: _average_hour(
        row, "aaurru_supersleep_effect_hour",
    ),
}


def _metric(
    metric_id: str,
    label: str,
    description: str,
    source_table: str,
    source_fields: tuple[str, ...],
    calculator: str,
    unit: str,
    *aliases: str,
) -> CatalogMetric:
    return CatalogMetric(
        id=metric_id,
        label=label,
        description=description,
        aliases=aliases,
        source_table=source_table,
        source_fields=source_fields,
        calculator=calculator,
        unit=unit,
        grain="summary_day",
    )


METRIC_REGISTRY = {
    metric.id: metric
    for metric in (
        _metric("lte_bbu_energy_wan", "4G BBU能耗", "4G BBU能耗由kWh换算为万度", "lte_report_day_collect", ("bbu_power",), "to_wan_du", "万度"),
        _metric("nr_bbu_energy_wan", "5G BBU能耗", "5G SA BBU能耗由kWh换算为万度", "nr_report_day_collect", ("sa_bbu_power",), "to_wan_du", "万度"),
        _metric("lte_rru_energy_wan", "4G RRU能耗", "4G RRU能耗由kWh换算为万度", "lte_report_day_collect", ("rru_power",), "to_wan_du", "万度"),
        _metric("nr_rru_energy_wan", "5G RRU/AAU能耗", "5G RRU或AAU能耗由kWh换算为万度", "nr_report_day_collect", ("rru_power",), "to_wan_du", "万度"),
        _metric("lte_station_energy_wan", "4G基站总能耗", "4G基站总能耗由kWh换算为万度", "lte_report_day_collect", ("lte_station_power",), "to_wan_du", "万度"),
        _metric("nr_station_energy_wan", "5G基站总能耗", "5G基站总能耗由kWh换算为万度", "nr_report_day_collect", ("nr_sa_station_power",), "to_wan_du", "万度"),
        _metric("lte_traffic_tb", "4G上下行业务量", "4G上下行业务量由GB换算为TB", "lte_report_day_collect", ("upoctul_dl",), "to_tb", "TB"),
        _metric("nr_traffic_tb", "5G上下行业务量", "5G上下行业务量由GB换算为TB", "nr_report_day_collect", ("upoctul_dl",), "to_tb", "TB"),
        _metric("lte_readable_ratio", "4G基站可读率", "可读4G逻辑站数占全部4G逻辑站数的比例", "lte_report_day_collect", ("logic_read_station_total", "logic_station_total"), "lte_readable_ratio", "%", "4G在线率"),
        _metric("nr_readable_ratio", "5G基站可读率", "可读5G逻辑站数占全部5G逻辑站数的比例", "nr_report_day_collect", ("logic_read_station_total", "logic_station_total"), "nr_readable_ratio", "%", "5G在线率"),
        _metric("lte_high_power_ratio", "4G高耗电小区占比", "高通道4G小区占全部4G小区的比例", "lte_report_day_collect", ("eightm_channel_total", "all_cell_total"), "lte_high_power_ratio", "%", "4G高通道占比"),
        _metric("nr_high_power_ratio", "5G高耗电小区占比", "32通道和64通道小区占全部5G小区的比例", "nr_report_day_collect", ("thirtytwo_channel_total", "sixtyfour_channel_total", "all_cell_total"), "nr_high_power_ratio", "%", "5G高通道占比"),
        _metric("lte_low_efficiency_ratio", "4G低能效小区占比", "4G低能效小区占全部4G小区的比例", "lte_report_day_collect", ("low_energy_total", "all_cell_total"), "low_efficiency_ratio", "%"),
        _metric("nr_low_efficiency_ratio", "5G低能效小区占比", "5G低能效小区占全部5G小区的比例", "nr_report_day_collect", ("low_energy_total", "all_cell_total"), "low_efficiency_ratio", "%"),
        _metric("nr_commode_ratio", "5G共模站占比", "共模逻辑站占全部5G逻辑站的比例", "nr_report_day_collect", ("commode_station_total", "logic_station_total"), "commode_ratio", "%", "共模占比"),
        _metric("lte_symbol_average_hour", "4G符号关断平均时长", "符号关断总时长除以4G小区总数", "lte_report_day_collect", ("symdown_effect_hour", "all_cell_total"), "symbol_average_hour", "小时"),
        _metric("lte_channel_average_hour", "4G通道关断平均时长", "通道关断总时长除以4G小区总数", "lte_report_day_collect", ("chandown_effect_hour", "all_cell_total"), "channel_average_hour", "小时"),
        _metric("lte_carrier_average_hour", "4G载波关断平均时长", "载波关断总时长除以4G小区总数", "lte_report_day_collect", ("carrdown_effect_hour", "all_cell_total"), "carrier_average_hour", "小时"),
        _metric("lte_deep_sleep_average_hour", "4G深度休眠平均时长", "深度休眠总时长除以4G小区总数", "lte_report_day_collect", ("deepsleep_effect_hour", "all_cell_total"), "deep_sleep_average_hour", "小时"),
        _metric("nr_symbol_average_hour", "5G亚帧静默平均时长", "亚帧静默总时长除以5G小区总数", "nr_report_day_collect", ("symdown_effect_hour", "all_cell_total"), "symbol_average_hour", "小时"),
        _metric("nr_channel_average_hour", "5G通道静默平均时长", "通道静默总时长除以5G小区总数", "nr_report_day_collect", ("chandown_effect_hour", "all_cell_total"), "channel_average_hour", "小时"),
        _metric("nr_shallow_sleep_average_hour", "5G浅层休眠平均时长", "浅层休眠总时长除以5G小区总数", "nr_report_day_collect", ("carrdown_effect_hour", "all_cell_total"), "carrier_average_hour", "小时"),
        _metric("nr_deep_sleep_average_hour", "5G深度休眠平均时长", "深度休眠总时长除以5G小区总数", "nr_report_day_collect", ("deepsleep_effect_hour", "all_cell_total"), "deep_sleep_average_hour", "小时"),
        _metric("nr_super_sleep_average_hour", "5G极致休眠平均时长", "极致休眠总时长除以5G小区总数", "nr_report_day_collect", ("aaurru_supersleep_effect_hour", "all_cell_total"), "super_sleep_average_hour", "小时"),
    )
}


def get_metric(metric_id: str) -> CatalogMetric:
    return METRIC_REGISTRY[metric_id]


def calculate_metric(metric: CatalogMetric, row: dict[str, Any]) -> float:
    calculator_row = dict(row)
    if len(metric.source_fields) == 1:
        calculator_row["value"] = row.get(metric.source_fields[0])
    return round(CALCULATORS[metric.calculator](calculator_row), 4)
