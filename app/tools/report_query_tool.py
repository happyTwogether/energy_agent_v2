from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.metrics_registry import average_effect_hour, safe_div, to_tb, to_wan_du
from app.utils.sql_helpers import error_response, get_latest_date, success_response

logger = get_logger("report_query_tool")

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
DB_SCHEMA = get_settings().db_schema          # PostgreSQL 的 schema 名
BASELINE_LOOKBACK_DAYS: int = 7               # 基线回溯 7 天
ANOMALY_THRESHOLD: float = 0.1              # 偏移 10% 以上算异常（双向）

def to_pct(numerator: float | None, denominator: float | None) -> str:
    """安全计算百分比，返回格式如 "12.34%"，分母为 0 返回 "0.00%"."""
    ratio = safe_div(float(numerator or 0) * 100, denominator)
    return f"{ratio:.2f}%"

# ---------------------------------------------------------------------------
# 数据获取（通用：参数化表名消除 LTE/NR 重复）
# ---------------------------------------------------------------------------

async def _fetch_data_with_baseline(
    db: AsyncSession,
    table: str,
    province: str,
    dist_name: str,
    prod_name: str,
    freq_band: str,
    site_type: str,
    area: str,
    query_start: str,
    date_end: str,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """拉取指定表数据并分离目标日（第一条）和基线数据（其余）。"""
    sql = text(f"""
        SELECT * FROM {DB_SCHEMA}.{table}
        WHERE province = :province
          AND dist_name = :dist_name
          AND prod_name = :prod_name
          AND freq_band = :freq_band
          AND site_type = :site_type
          AND area = :area
          AND data_date BETWEEN :query_start AND :date_end
        ORDER BY data_date DESC
    """)

    result = await db.execute(
        sql,
        {
            "province": province,
            "dist_name": dist_name,
            "prod_name": prod_name,
            "freq_band": freq_band,
            "site_type": site_type,
            "area": area,
            "query_start": query_start,
            "date_end": date_end,
        },
    )
    rows = result.mappings().all()

    if not rows:
        return None, []

    target_data = dict(rows[0])
    baseline_data = [dict(row) for row in rows[1:]]

    return target_data, baseline_data


# ---------------------------------------------------------------------------
# 异常诊断指标定义（模块级常量）
# ---------------------------------------------------------------------------

# 各指标: (字段名, 显示名称, 值转换函数, 单位)
LTE_METRICS: list[tuple[str, str, Any, str]] = [
    ("logic_station_total", "LTE逻辑站数量", lambda x: x, "个"),
    ("all_cell_total", "LTE总小区数", lambda x: x, "个"),
    ("lte_station_power", "LTE基站总能耗", to_wan_du, "万度"),
    ("single_station_power", "LTE单站能耗", lambda x: x, "度"),
    ("carrdown_effect_hour", "载波关断生效总小时数", lambda x: x, "小时"),
    ("open_carrdown_total", "开启载波关断的小区数", lambda x: x, "个"),
    ("chandown_effect_hour", "通道关断生效总小时数", lambda x: x, "小时"),
    ("open_chandown_total", "开启通道关断的小区数", lambda x: x, "个"),
    ("symdown_effect_hour", "符号关断生效总小时数", lambda x: x, "小时"),
    ("open_symdown_total", "开启符号关断的小区数", lambda x: x, "个"),
    ("open_deepsleep_total", "开启深度休眠的小区数", lambda x: x, "个"),
    ("deepsleep_effect_hour", "深度休眠生效总小时数", lambda x: x, "小时"),
    ("lte_curmonthpower", "LTE节电量", to_wan_du, "万度"),
    ("lte_curmonthpower_rate", "LTE节电比例", lambda x: float(str(x).replace("%", "")) if x is not None else 0.0, "%"),
]

NR_METRICS: list[tuple[str, str, Any, str]] = [
    ("logic_station_total", "5G逻辑站数量", lambda x: x, "个"),
    ("all_cell_total", "5G总小区数", lambda x: x, "个"),
    ("nr_sa_station_power", "5G基站总能耗", to_wan_du, "万度"),
    ("carrdown_effect_hour", "浅层休眠生效总小时数", lambda x: x, "小时"),
    ("open_carrdown_total", "开启浅层休眠的小区数", lambda x: x, "个"),
    ("chandown_effect_hour", "通道静默生效总小时数", lambda x: x, "小时"),
    ("open_chandown_total", "开启通道静默的小区数", lambda x: x, "个"),
    ("symdown_effect_hour", "亚帧静默生效总小时数", lambda x: x, "小时"),
    ("open_symdown_total", "开启亚帧静默的小区数", lambda x: x, "个"),
    ("deepsleep_effect_hour", "深度休眠生效总小时数", lambda x: x, "小时"),
    ("open_deepsleep_total", "开启深度休眠的小区数", lambda x: x, "个"),
    ("open_supersleep_total", "开启极致休眠的小区数", lambda x: x, "个"),
    ("aaurru_supersleep_effect_hour", "极致休眠生效总小时数", lambda x: x, "小时"),
    ("nr_curmonthpower", "5G节电量", to_wan_du, "万度"),
    ("nr_curmonthpower_rate", "5G节电比例", lambda x: float(str(x).replace("%", "")) if x is not None else 0.0, "%"),
]


def _diagnose_anomalies(
    target_data: dict[str, Any],
    baseline_data: list[dict[str, Any]],
    metrics: list[tuple[str, str, Any, str]],
) -> list[str]:
    """通用异常诊断：当前值与 7 天基线峰值比较，偏移 ≥ 10% 标记异常（双向）。"""
    anomalies: list[str] = []

    for field_name, display_name, convert_fn, unit in metrics:
        target_raw = target_data.get(field_name)
        try:
            target_value = float(convert_fn(target_raw)) if target_raw is not None else 0.0
        except (TypeError, ValueError):
            target_value = 0.0

        baseline_values: list[float] = []
        for row in baseline_data:
            raw_val = row.get(field_name)
            if raw_val is not None:
                try:
                    baseline_values.append(float(convert_fn(raw_val)))
                except (TypeError, ValueError):
                    continue

        if not baseline_values:
            continue

        baseline_peak = max(baseline_values)
        if baseline_peak == 0:
            continue

        offset = abs(target_value - baseline_peak) / baseline_peak
        if offset >= ANOMALY_THRESHOLD:
            direction = (target_value - baseline_peak) / baseline_peak * 100
            anomalies.append(
                f"{display_name}: 当前 {target_value:g}{unit}，7天峰值 {baseline_peak:g}{unit}，偏移 {direction:+.1f}%"
            )

    return anomalies


# ---------------------------------------------------------------------------
# 数据处理
# ---------------------------------------------------------------------------

def _process_lte_data(
    target_data: dict[str, Any] | None,
    baseline_data: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, list[str]]:
    """处理 4G 数据：单位换算和异常诊断。"""
    if not target_data:
        return None, []

    # 基础字段
    lte_cell_total = target_data.get("all_cell_total") or 0
    lte_logic_station_total = target_data.get("logic_station_total") or 0
    lte_station_onl = target_data.get("logic_read_station_total") or 0
    lte_highchannel_cell_total = target_data.get("eightm_channel_total") or 0

    # 能耗换算 (千瓦时 -> 万度)
    bbu_power_wan = to_wan_du(target_data.get("bbu_power"))
    rru_power_wan = to_wan_du(target_data.get("rru_power"))
    lte_station_power_wan = to_wan_du(target_data.get("lte_station_power"))
    lte_curmonthpower_wan = to_wan_du(target_data.get("lte_curmonthpower"))

    # 流量换算 (GB -> TB)
    upoctul_dl_tb = to_tb(target_data.get("upoctul_dl"))

    processed = {
        # 基础信息
        "date": target_data.get("data_date"),
        "lte_logic_station_total": lte_logic_station_total,
        "lte_bbu_channel_total": target_data.get("bbu_total") or 0,
        "lte_cell_total": lte_cell_total,
        "lte_highchannel_cell_total": lte_highchannel_cell_total,
        "lte_station_onl": lte_station_onl,

        # 网络规模计算
        "lte_readable_ratio": to_pct(lte_station_onl, lte_logic_station_total),
        "lte_high_power_ratio": to_pct(lte_highchannel_cell_total, lte_cell_total),

        # 能耗 - 万度转换
        "bbu_power_wan": bbu_power_wan,
        "rru_power_wan": rru_power_wan,
        "lte_station_power_wan": lte_station_power_wan,

        # 流量 - TB转换
        "upoctul_dl_tb": upoctul_dl_tb,

        # 节电
        "lte_curmonthpower_wan": lte_curmonthpower_wan,
        "lte_curmonthpower_rate": target_data.get("lte_curmonthpower_rate") or "0.00%",
        "lte_perstation_pow": round(target_data.get("single_station_power") or 0, 2),

        # 能效
        "avg_energy_efficiency": round(target_data.get("avg_energy_efficiency") or 0, 2),
        "low_energyefficiency_cell_total": target_data.get("low_energy_total") or 0,
        "lte_low_efficiency_ratio": to_pct(target_data.get("low_energy_total"), lte_cell_total),

        # 节电软关断 - 4G
        # 总生效小时（原始字段）
        # 平均生效小时 = 总生效小时 / 总小区数
        "symdown_on": target_data.get("open_symdown_rate") or "0.00%",
        "symdown_effect": target_data.get("symdown_effect_ratio") or "0.00%",
        "symdown_hour": round(target_data.get("symdown_effect_hour") or 0, 1),
        "symdown_avg_hour": average_effect_hour(
            target_data.get("symdown_effect_hour"), lte_cell_total,
        ),

        "chandown_on": target_data.get("open_chandown_rate") or "0.00%",
        "chandown_effect": target_data.get("chandown_effect_ratio") or "0.00%",
        "chandown_hour": round(target_data.get("chandown_effect_hour") or 0, 1),
        "chandown_avg_hour": average_effect_hour(
            target_data.get("chandown_effect_hour"), lte_cell_total,
        ),

        "carrdown_on": target_data.get("open_carrdown_rate") or "0.00%",
        "carrdown_effect": target_data.get("carrdown_effect_ratio") or "0.00%",
        "carrdown_hour": round(target_data.get("carrdown_effect_hour") or 0, 1),
        "carrdown_avg_hour": average_effect_hour(
            target_data.get("carrdown_effect_hour"), lte_cell_total,
        ),

        "deepsleep_on": target_data.get("open_deepsleep_rate") or "0.00%",
        "deepsleep_effect": target_data.get("deepsleep_effect_ratio") or "0.00%",
        "deepsleep_hour": round(target_data.get("deepsleep_effect_hour") or 0, 1),
        "deepsleep_avg_hour": average_effect_hour(
            target_data.get("deepsleep_effect_hour"), lte_cell_total,
        ),
    }

    # 异常诊断
    anomalies = _diagnose_anomalies(target_data, baseline_data, LTE_METRICS)
    return processed, anomalies


def _process_nr_data(
    target_data: dict[str, Any] | None,
    baseline_data: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, list[str]]:
    """处理 5G 数据：单位换算和异常诊断。"""
    if not target_data:
        return None, []

    nr_cell_total = target_data.get("all_cell_total") or 0
    nr_logic_station_total = target_data.get("logic_station_total") or 0
    nr_station_onl = target_data.get("logic_read_station_total") or 0
    nr_highchannel_cell_total = (target_data.get("thirtytwo_channel_total") or 0) + (target_data.get("sixtyfour_channel_total") or 0)
    commode_station_total = target_data.get("commode_station_total") or 0

    # 能耗换算
    nr_bbu_power_wan = to_wan_du(target_data.get("sa_bbu_power"))
    nr_rru_power_wan = to_wan_du(target_data.get("rru_power"))
    nr_sa_station_power_wan = to_wan_du(target_data.get("nr_sa_station_power"))
    nr_curmonthpower_wan = to_wan_du(target_data.get("nr_curmonthpower"))

    # 流量换算
    upoctul_dl_tb = to_tb(target_data.get("upoctul_dl"))

    processed = {
        # 基础信息
        "date": target_data.get("data_date"),
        "nr_logic_station_total": nr_logic_station_total,
        "nr_bbu_channel_total": target_data.get("sa_bbu_total") or 0,
        "nr_cell_total": nr_cell_total,
        "nr_32t_cell_total": target_data.get("thirtytwo_channel_total") or 0,
        "nr_64t_cell_total": target_data.get("sixtyfour_channel_total") or 0,
        "nr_highchannel_cell_total": nr_highchannel_cell_total,
        "nr_station_onl": nr_station_onl,

        # 网络规模计算
        "nr_readable_ratio": to_pct(nr_station_onl, nr_logic_station_total),
        "nr_high_power_ratio": to_pct(nr_highchannel_cell_total, nr_cell_total),
        "commode_station_total": commode_station_total,
        "commode_ratio": to_pct(commode_station_total, nr_logic_station_total),

        # 能耗 - 万度转换
        "nr_bbu_power_wan": nr_bbu_power_wan,
        "nr_rru_power_wan": nr_rru_power_wan,
        "nr_sa_station_power_wan": nr_sa_station_power_wan,

        # 流量 - TB转换
        "upoctul_dl_tb": upoctul_dl_tb,

        # 节电
        "nr_curmonthpower_wan": nr_curmonthpower_wan,
        "nr_curmonthpower_rate": target_data.get("nr_curmonthpower_rate") or "0.00%",
        "nr_perstation_pow": round(target_data.get("single_station_power") or 0, 2),

        # 能效
        "sa_avg_energy_efficiency": round(target_data.get("sa_avg_energy_efficiency") or 0, 2),
        "low_energyefficiency_nr_cell_total": target_data.get("low_energy_total") or 0,
        "nr_low_efficiency_ratio": to_pct(target_data.get("low_energy_total"), nr_cell_total),

        # 节电软关断 - 5G
        # 总生效小时（原始字段）
        # 平均生效小时 = 总生效小时 / 总小区数
        "subframe_silence_on": target_data.get("open_symdown_rate") or "0.00%",
        "subframe_silence_effect": target_data.get("symdown_effect_ratio") or "0.00%",
        "subframe_silence_hour": round(target_data.get("symdown_effect_hour") or 0, 1),
        "subframe_silence_avg_hour": average_effect_hour(
            target_data.get("symdown_effect_hour"), nr_cell_total,
        ),

        "channel_silence_on": target_data.get("open_chandown_rate") or "0.00%",
        "channel_silence_effect": target_data.get("chandown_effect_ratio") or "0.00%",
        "channel_silence_hour": round(target_data.get("chandown_effect_hour") or 0, 1),
        "channel_silence_avg_hour": average_effect_hour(
            target_data.get("chandown_effect_hour"), nr_cell_total,
        ),

        "shallow_sleep_on": target_data.get("open_carrdown_rate") or "0.00%",
        "shallow_sleep_effect": target_data.get("carrdown_effect_ratio") or "0.00%",
        "shallow_sleep_hour": round(target_data.get("carrdown_effect_hour") or 0, 1),
        "shallow_sleep_avg_hour": average_effect_hour(
            target_data.get("carrdown_effect_hour"), nr_cell_total,
        ),

        "deep_sleep_on": target_data.get("open_deepsleep_rate") or "0.00%",
        "deep_sleep_effect": target_data.get("deepsleep_effect_ratio") or "0.00%",
        "deep_sleep_hour": round(target_data.get("deepsleep_effect_hour") or 0, 1),
        "deep_sleep_avg_hour": average_effect_hour(
            target_data.get("deepsleep_effect_hour"), nr_cell_total,
        ),

        "extreme_sleep_on": target_data.get("open_supersleep_rate") or "0.00%",
        "extreme_sleep_effect": target_data.get("aaurru_supersleep_effect_ratio") or "0.00%",
        "extreme_sleep_hour": round(target_data.get("aaurru_supersleep_effect_hour") or 0, 1),
        "extreme_sleep_avg_hour": average_effect_hour(
            target_data.get("aaurru_supersleep_effect_hour"), nr_cell_total,
        ),
    }

    # 异常诊断
    anomalies = _diagnose_anomalies(target_data, baseline_data, NR_METRICS)
    return processed, anomalies

# ---------------------------------------------------------------------------
# 报告生成（按章节拆分，提升可维护性）
# ---------------------------------------------------------------------------

def _report_date(nr_data: dict[str, Any] | None, lte_data: dict[str, Any] | None) -> str:
    """提取报告日期，优先 5G 数据。"""
    if nr_data:
        return nr_data.get("date", "")
    if lte_data:
        return lte_data.get("date", "")
    return ""


def _build_network_scale_section(
    nr_data: dict[str, Any] | None,
    lte_data: dict[str, Any] | None,
    report_date: str,
) -> list[str]:
    """一、网络规模介绍"""
    lines = ["## 一、网络规模介绍", ""]

    if nr_data:
        nr_high_power_total = (nr_data.get("nr_32t_cell_total") or 0) + (nr_data.get("nr_64t_cell_total") or 0)
        lines.append("### 5G方面")
        lines.append("")
        lines.append(
            f"5G方面：逻辑站数{nr_data.get('nr_logic_station_total')}个、"
            f"BBU数{nr_data.get('nr_bbu_channel_total')}个、"
            f"小区数{nr_data.get('nr_cell_total')}个、"
            f"32T+64T高耗电设备小区{nr_high_power_total}个（占比{nr_data.get('nr_high_power_ratio')}），"
            f"4/5G共模站数{nr_data.get('commode_station_total')}个（占比{nr_data.get('commode_ratio')}）；"
        )
        lines.append("")
        lines.append(
            "| 日期 | 5G总BBU数 | 5G逻辑站数量 | 可通过网管读取的5G逻辑站数 | 可读电量逻辑站占比 | "
            "5G总小区数 | 32通道5G小区数 | 64通道5G小区数 | 高耗电小区设备占比 | 4/5G共模站数量 | 共模站点占比 |"
        )
        lines.append(
            "|------|-----------|--------------|----------------------------|--------------------|"
            "------------|----------------|----------------|--------------------|----------------|--------------|"
        )
        lines.append(
            f"| {report_date} | {nr_data.get('nr_bbu_channel_total')} | {nr_data.get('nr_logic_station_total')} | "
            f"{nr_data.get('nr_station_onl')} | {nr_data.get('nr_readable_ratio')} | {nr_data.get('nr_cell_total')} | "
            f"{nr_data.get('nr_32t_cell_total')} | {nr_data.get('nr_64t_cell_total')} | {nr_data.get('nr_high_power_ratio')} | "
            f"{nr_data.get('commode_station_total')} | {nr_data.get('commode_ratio')} |"
        )
        lines.append("")

    if lte_data:
        lines.append("### 4G方面")
        lines.append("")
        lines.append(
            f"4G方面：逻辑站数{lte_data.get('lte_logic_station_total')}个、"
            f"BBU数{lte_data.get('lte_bbu_channel_total')}个、"
            f"小区数{lte_data.get('lte_cell_total')}个、"
            f"8T以上高耗电设备小区{lte_data.get('lte_highchannel_cell_total')}个（占比{lte_data.get('lte_high_power_ratio')}）。"
        )
        lines.append("")
        lines.append(
            "| 日期 | LTE总BBU数 | LTE逻辑站数量 | 可通过网管读取的LTE逻辑站数量 | 可读电量逻辑站点比 | "
            "LTE总小区数 | 8通道及以上小区数 | 高耗电小区设备占比 |"
        )
        lines.append(
            "|------|------------|---------------|-----------------------------|--------------------|"
            "-------------|-------------------|--------------------|"
        )
        lines.append(
            f"| {report_date} | {lte_data.get('lte_bbu_channel_total')} | {lte_data.get('lte_logic_station_total')} | "
            f"{lte_data.get('lte_station_onl')} | {lte_data.get('lte_readable_ratio')} | {lte_data.get('lte_cell_total')} | "
            f"{lte_data.get('lte_highchannel_cell_total')} | {lte_data.get('lte_high_power_ratio')} |"
        )
        lines.append("")

    return lines


def _build_energy_efficiency_section(
    nr_data: dict[str, Any] | None,
    lte_data: dict[str, Any] | None,
    report_date: str,
) -> list[str]:
    """二、网络能效介绍"""
    lines = ["## 二、网络能效介绍", ""]

    if nr_data:
        lines.append("### 5G方面")
        lines.append("")
        lines.append(
            f"5G方面：日流量{nr_data.get('upoctul_dl_tb')}TB、"
            f"基站总能耗{nr_data.get('nr_sa_station_power_wan')}万度、"
            f"节电量{nr_data.get('nr_curmonthpower_wan')}万度，"
            f"节电比例{nr_data.get('nr_curmonthpower_rate')}、"
            f"单站能耗{nr_data.get('nr_perstation_pow')}度、"
            f"5G小区平均能效{nr_data.get('sa_avg_energy_efficiency')}GB/度；"
        )
        lines.append("")
        lines.append(
            "| 日期 | 5G小区BBU总电量（万度） | 5G基站总能耗（万度） | 5G基站按制式分拆总能耗（万度） | "
            "单站能耗（度） | 5G小区平均能效（GB/千瓦时） | "
            "5G小区RRU总电量（万度） | NR小区上下行业务量（TB） | 节电量（万度） | 节电比例 |"
        )
        lines.append(
            "|------|------------------------|---------------------|-----------------------------|"
            "---------------|---------------------------|"
            "------------------------|-------------------------|----------------|----------|"
        )
        lines.append(
            f"| {report_date} | {nr_data.get('nr_bbu_power_wan')} | {nr_data.get('nr_sa_station_power_wan')} | "
            f"{nr_data.get('nr_sa_station_power_wan')} | {nr_data.get('nr_perstation_pow')} | "
            f"{nr_data.get('sa_avg_energy_efficiency')} | {nr_data.get('nr_rru_power_wan')} | "
            f"{nr_data.get('upoctul_dl_tb')} | {nr_data.get('nr_curmonthpower_wan')} | {nr_data.get('nr_curmonthpower_rate')} |"
        )
        lines.append("")

    if lte_data:
        lines.append("### 4G方面")
        lines.append("")
        lines.append(
            f"4G方面：日流量{lte_data.get('upoctul_dl_tb')}TB、"
            f"基站总能耗{lte_data.get('lte_station_power_wan')}万度、"
            f"节电量{lte_data.get('lte_curmonthpower_wan')}万度，"
            f"节电比例{lte_data.get('lte_curmonthpower_rate')}、"
            f"单站能耗{lte_data.get('lte_perstation_pow')}度、"
            f"4G小区平均能效{lte_data.get('avg_energy_efficiency')}GB/度；"
        )
        lines.append("")
        lines.append(
            "| 日期 | LTE小区BBU总电量（万度） | LTE基站总能耗（万度） | LTE基站分拆总能耗（万度） | "
            "NB基站分拆总能耗（万度） | GSM基站分拆总能耗（万度） | LTE小区平均能效（GB/千瓦时） | "
            "LTE小区RRU总电量（万度） | LTE小区上下行业务量（TB） | "
            "节电量（万度） | 节电比例 |"
        )
        lines.append(
            "|------|-------------------------|----------------------|--------------------------|"
            "--------------------------|--------------------------|----------------------------|"
            "--------------------------|-------------------------|"
            "----------------|----------|"
        )
        lines.append(
            f"| {report_date} | {lte_data.get('bbu_power_wan')} | {lte_data.get('lte_station_power_wan')} | "
            f"{lte_data.get('lte_station_power_wan')} | 0 | 0 | "
            f"{lte_data.get('avg_energy_efficiency')} | {lte_data.get('rru_power_wan')} | "
            f"{lte_data.get('upoctul_dl_tb')} | {lte_data.get('lte_curmonthpower_wan')} | {lte_data.get('lte_curmonthpower_rate')} |"
        )
        lines.append("")

    return lines


def _build_power_saving_section(
    nr_data: dict[str, Any] | None,
    lte_data: dict[str, Any] | None,
    report_date: str,
) -> list[str]:
    """三、节电软关断情况介绍"""
    lines = ["## 三、节电软关断情况介绍", ""]

    if nr_data:
        lines.append("### 5G方面")
        lines.append("")
        lines.append(
            f"5G方面：亚帧静默平均生效{nr_data.get('subframe_silence_avg_hour')}小时/小区，"
            f"通道静默平均生效{nr_data.get('channel_silence_avg_hour')}小时/小区，"
            f"浅层休眠平均生效{nr_data.get('shallow_sleep_avg_hour')}小时/小区，"
            f"深度休眠平均生效{nr_data.get('deep_sleep_avg_hour')}小时/小区，"
            f"极致休眠平均生效{nr_data.get('extreme_sleep_avg_hour')}小时/小区。"
        )
        lines.append("")
        lines.append(
            "| 日期 | 亚帧静默开启比例 | 亚帧静默生效比例 | 亚帧静默总生效小时 | 亚帧静默平均生效小时 | "
            "通道静默开启比例 | 通道静默生效比例 | 通道静默总生效小时 | 通道静默平均生效小时 | "
            "浅层休眠开启比例 | 浅层休眠生效比例 | 浅层休眠总生效小时 | 浅层休眠平均生效小时 | "
            "深度休眠开启比例 | 深度休眠生效比例 | 深度休眠总生效小时 | 深度休眠平均生效小时 | "
            "极致休眠开启比例 | 极致休眠生效比例 | 极致休眠总生效小时 | 极致休眠平均生效小时 |"
        )
        lines.append(
            "|------|------------------|------------------|--------------------|----------------------|"
            "------------------|------------------|--------------------|----------------------|"
            "------------------|------------------|--------------------|----------------------|"
            "------------------|------------------|--------------------|----------------------|"
            "------------------|------------------|--------------------|----------------------|"
        )
        lines.append(
            f"| {report_date} | {nr_data.get('subframe_silence_on')} | {nr_data.get('subframe_silence_effect')} | "
            f"{nr_data.get('subframe_silence_hour')} | {nr_data.get('subframe_silence_avg_hour')} | "
            f"{nr_data.get('channel_silence_on')} | {nr_data.get('channel_silence_effect')} | "
            f"{nr_data.get('channel_silence_hour')} | {nr_data.get('channel_silence_avg_hour')} | "
            f"{nr_data.get('shallow_sleep_on')} | {nr_data.get('shallow_sleep_effect')} | "
            f"{nr_data.get('shallow_sleep_hour')} | {nr_data.get('shallow_sleep_avg_hour')} | "
            f"{nr_data.get('deep_sleep_on')} | {nr_data.get('deep_sleep_effect')} | "
            f"{nr_data.get('deep_sleep_hour')} | {nr_data.get('deep_sleep_avg_hour')} | "
            f"{nr_data.get('extreme_sleep_on')} | {nr_data.get('extreme_sleep_effect')} | "
            f"{nr_data.get('extreme_sleep_hour')} | {nr_data.get('extreme_sleep_avg_hour')} |"
        )
        lines.append("")

    if lte_data:
        lines.append("### 4G方面")
        lines.append("")
        lines.append(
            f"4G方面：符号关断平均生效{lte_data.get('symdown_avg_hour')}小时/小区，"
            f"通道关断平均生效{lte_data.get('chandown_avg_hour')}小时/小区，"
            f"载波关断平均生效{lte_data.get('carrdown_avg_hour')}小时/小区，"
            f"深度休眠平均生效{lte_data.get('deepsleep_avg_hour')}小时/小区。"
        )
        lines.append("")
        lines.append(
            "| 日期 | 符号关断开启比例 | 符号关断生效比例 | 符号关断总生效小时 | 符号关断平均生效小时 | "
            "通道关断开启比例 | 通道关断生效比例 | 通道关断总生效小时 | 通道关断平均生效小时 | "
            "载波关断开启比例 | 载波关断生效比例 | 载波关断总生效小时 | 载波关断平均生效小时 | "
            "深度休眠开启比例 | 深度休眠生效比例 | 深度休眠总生效小时 | 深度休眠平均生效小时 |"
        )
        lines.append(
            "|------|------------------|------------------|--------------------|----------------------|"
            "------------------|------------------|--------------------|----------------------|"
            "------------------|------------------|--------------------|----------------------|"
            "------------------|------------------|--------------------|----------------------|"
        )
        lines.append(
            f"| {report_date} | {lte_data.get('symdown_on')} | {lte_data.get('symdown_effect')} | "
            f"{lte_data.get('symdown_hour')} | {lte_data.get('symdown_avg_hour')} | "
            f"{lte_data.get('chandown_on')} | {lte_data.get('chandown_effect')} | "
            f"{lte_data.get('chandown_hour')} | {lte_data.get('chandown_avg_hour')} | "
            f"{lte_data.get('carrdown_on')} | {lte_data.get('carrdown_effect')} | "
            f"{lte_data.get('carrdown_hour')} | {lte_data.get('carrdown_avg_hour')} | "
            f"{lte_data.get('deepsleep_on')} | {lte_data.get('deepsleep_effect')} | "
            f"{lte_data.get('deepsleep_hour')} | {lte_data.get('deepsleep_avg_hour')} |"
        )
        lines.append("")

    return lines


def _build_anomaly_section(
    lte_anomalies: list[str],
    nr_anomalies: list[str],
) -> list[str]:
    """四、异常波动指标定位"""
    lines = ["## 四、异常波动指标定位", ""]

    if not lte_anomalies and not nr_anomalies:
        lines.append("暂无异常波动指标")
    else:
        if lte_anomalies:
            lines.append("- 4G方面：")
            for anomaly in lte_anomalies:
                lines.append(f"  - {anomaly}")
        if nr_anomalies:
            lines.append("- 5G方面：")
            for anomaly in nr_anomalies:
                lines.append(f"  - {anomaly}")
    lines.append("")
    return lines


def _generate_report_markdown(
    dist_name: str,
    prod_name: str,
    freq_band: str,
    site_type: str,
    area: str,
    lte_data: dict[str, Any] | None,
    nr_data: dict[str, Any] | None,
    lte_anomalies: list[str],
    nr_anomalies: list[str],
) -> str:
    """使用纯 Python f-string 生成 Markdown 报告。"""
    report_date = _report_date(nr_data, lte_data)

    lines = [f"# {dist_name}-{prod_name}-频段({freq_band})-站型({site_type})-区域({area})维度4/5G网络节耗电总体情况", ""]
    lines.extend(_build_network_scale_section(nr_data, lte_data, report_date))
    lines.extend(_build_energy_efficiency_section(nr_data, lte_data, report_date))
    lines.extend(_build_power_saving_section(nr_data, lte_data, report_date))
    lines.extend(_build_anomaly_section(lte_anomalies, nr_anomalies))

    return "\n".join(lines)

TOOL_DESCRIPTION = (
    "按固定口径生成4G/5G网络能耗Markdown报告，并使用前7天基线对比。"
    "适用：用户明确要求完整能耗汇总报告。"
    "不适用：仅查询字段、指标、明细或参数合规报告。"
)
TOOL_INPUT_SCHEMA = {
        "type": "object",
        "properties": {
            "province": {"type": "string", "description": "省份名称，未传默认湖南省"},
            "dist_name": {"type": "string", "description": "地市名称，未传默认全网"},
            "prod_name": {"type": "string", "description": "设备厂家"},
            "freq_band": {
                "type": "string",
                "description": "频段",
            },
            "site_type": {
                "type": "string",
                "description": "站型",
            },
            "area": {
                "type": "string",
                "description": "区域类型（一般城区/主城区/乡镇/农村/县城/全网）",
            },
            "date_start": {
                "type": "string",
                "description": "开始日期 (YYYY-MM-DD)",
            },
            "date_end": {
                "type": "string",
                "description": "结束日期 (YYYY-MM-DD)",
            },
        },
        "required": [],
}


async def query_report(
    db: AsyncSession,
    province: str | None = None,
    dist_name: str | None = None,
    prod_name: str | None = None,
    freq_band: str | None = None,
    site_type: str | None = None,
    area: str | None = None,
    date_start: str | None = None,
    date_end: str | None = None,
) -> dict[str, Any]:
    """查询并生成 4G/5G 网络节耗电分析报告。

    Args:
        db: 数据库会话。
        province: 省份名称，未传时默认全网。
        dist_name: 地市名称，未传时默认全网。
        prod_name: 设备厂家，未传时默认全网。
        freq_band: 频段，未传时默认全网。
        site_type: 站型，未传时默认全网。
        area: 区域类型（一般城区/主城区/乡镇/农村/县城），未传时默认全网。
        date_start: 报告日期，会暗中前推7天，未传时默认昨天。
        date_end: 结束日期，未传时默认与 date_start 相同。

    Returns:
        包含报告和数据的字典。
    """
    province = province or "湖南省"
    dist_name = dist_name or "全网"
    prod_name = prod_name or "全网"
    freq_band = freq_band or "全网"
    site_type = site_type or "全网"
    area = area or "全网"
    db_latest_dt = await get_latest_date(db, [f"{DB_SCHEMA}.lte_report_day_collect", f"{DB_SCHEMA}.nr_report_day_collect"])
    if not db_latest_dt:
        return error_response("数据库中暂无报表数据")
    db_latest = db_latest_dt.strftime("%Y-%m-%d")

    date_note = ""
    if date_start:
        if date_start > db_latest:
            date_note = f"您查询的日期 {date_start} 暂无数据，已自动查询最新数据日期 {db_latest}"
            date_start = db_latest
    else:
        date_start = db_latest
    date_end = date_end or date_start
    if date_end > db_latest:
        date_end = db_latest

    logger.info(
        "报表查询: province=%s, dist_name=%s, prod_name=%s, freq_band=%s, site_type=%s, area=%s, date_range=%s~%s",
        province,
        dist_name,
        prod_name,
        freq_band,
        site_type,
        area,
        date_start,
        date_end,
    )

    try:
        # 1. 计算查询起始日期（前推 N 天获取基线）
        date_start_dt = datetime.strptime(date_start, "%Y-%m-%d")
        query_start_dt = date_start_dt - timedelta(days=BASELINE_LOOKBACK_DAYS)
        query_start = query_start_dt.strftime("%Y-%m-%d")

        logger.info("前推%d天获取基线: %s ~ %s", BASELINE_LOOKBACK_DAYS, query_start, date_end)

        # 2. 串行拉取 4G/5G 原始数据。AsyncSession 不允许并发共享。
        fetch_kwargs = dict(
            db=db, province=province, dist_name=dist_name, prod_name=prod_name,
            freq_band=freq_band, site_type=site_type, area=area,
            query_start=query_start, date_end=date_end,
        )
        lte_target_raw, lte_baseline_raw = await _fetch_data_with_baseline(
            table="lte_report_day_collect",
            **fetch_kwargs,
        )
        nr_target_raw, nr_baseline_raw = await _fetch_data_with_baseline(
            table="nr_report_day_collect",
            **fetch_kwargs,
        )

        if not lte_target_raw and not nr_target_raw:
            return {
                "success": False,
                "error": "未找到指定时间段内的数据",
            }

        # 3. Python 预处理（单位换算 + 异常诊断）
        lte_data, lte_anomalies = _process_lte_data(lte_target_raw, lte_baseline_raw)
        nr_data, nr_anomalies = _process_nr_data(nr_target_raw, nr_baseline_raw)

        # 4. 生成报告
        report_markdown = _generate_report_markdown(
            dist_name=dist_name,
            prod_name=prod_name,
            freq_band=freq_band,
            site_type=site_type,
            area=area,
            lte_data=lte_data,
            nr_data=nr_data,
            lte_anomalies=lte_anomalies,
            nr_anomalies=nr_anomalies,
        )

        return {
            "success": True,
            "report_content": report_markdown,
            **({"date_note": date_note} if date_note else {}),
        }

    except Exception as exc:
        logger.error("报表查询异常: %s", exc, exc_info=True)
        return {
            "success": False,
            "error": "报表生成失败，请稍后重试",
        }
