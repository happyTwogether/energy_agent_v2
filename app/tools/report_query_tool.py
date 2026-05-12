from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.logging import get_logger
from app.tools.registry import tool_registry

logger = get_logger("report_query_tool")

# 获取数据库 schema，用于构造带 schema 前缀的表名
DB_SCHEMA = get_settings().db_schema

def safe_div(a: float | None, b: float | None, decimals: int = 2) -> float:
    """安全除法，若 b 为 0 或 None，返回 0.0。强制转为 float 运算。"""
    try:
        if b is None or float(b) == 0.0:
            return 0.0
        if a is None:
            return 0.0
        return round(float(a) / float(b), decimals)
    except (TypeError, ValueError):
        return 0.0


def to_wan_du(value: float | None) -> float:
    """千瓦时转万度 (value / 10000)，保留 2 位小数。强制转为 float。"""
    try:
        if value is None:
            return 0.0
        return round(float(value) / 10000, 2)
    except (TypeError, ValueError):
        return 0.0


def to_tb(value: float | None) -> float:
    """GB 转 TB (value / 1024)，保留 2 位小数。强制转为 float。"""
    try:
        if value is None:
            return 0.0
        return round(float(value) / 1024, 2)
    except (TypeError, ValueError):
        return 0.0


def to_pct(numerator: float | None, denominator: float | None) -> str:
    """安全计算百分比，返回格式如 "12.34%"，分母为 0 返回 "0.00%"。强制转为 float。"""
    try:
        if denominator is None or float(denominator) == 0.0:
            return "0.00%"
        if numerator is None:
            return "0.00%"
        pct = (float(numerator) / float(denominator)) * 100
        return f"{pct:.2f}%"
    except (TypeError, ValueError):
        return "0.00%"

async def _fetch_lte_data_with_baseline(
    db: AsyncSession,
    dist_name: str,
    prod_name: str,
    freq_band: str,
    site_type: str,
    area: str,
    query_start: str,
    date_end: str,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """拉取 4G 数据并分离目标日和基线数据。"""
    sql = text(f"""
        SELECT * FROM {DB_SCHEMA}.lte_report_day_collect
        WHERE dist_name = :dist_name
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

    # 第一条是目标日，其余是基线
    target_data = dict(rows[0])
    baseline_data = [dict(row) for row in rows[1:]]

    return target_data, baseline_data


async def _fetch_nr_data_with_baseline(
    db: AsyncSession,
    dist_name: str,
    prod_name: str,
    freq_band: str,
    site_type: str,
    area: str,
    query_start: str,
    date_end: str,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """拉取 5G 数据并分离目标日和基线数据。"""
    sql = text(f"""
        SELECT * FROM {DB_SCHEMA}.nr_report_day_collect
        WHERE dist_name = :dist_name
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
        "symdown_avg_hour": safe_div(target_data.get("symdown_effect_hour"), lte_cell_total, 1),

        "chandown_on": target_data.get("open_chandown_rate") or "0.00%",
        "chandown_effect": target_data.get("chandown_effect_ratio") or "0.00%",
        "chandown_hour": round(target_data.get("chandown_effect_hour") or 0, 1),
        "chandown_avg_hour": safe_div(target_data.get("chandown_effect_hour"), lte_cell_total, 1),

        "carrdown_on": target_data.get("open_carrdown_rate") or "0.00%",
        "carrdown_effect": target_data.get("carrdown_effect_ratio") or "0.00%",
        "carrdown_hour": round(target_data.get("carrdown_effect_hour") or 0, 1),
        "carrdown_avg_hour": safe_div(target_data.get("carrdown_effect_hour"), lte_cell_total, 1),

        "deepsleep_on": target_data.get("open_deepsleep_rate") or "0.00%",
        "deepsleep_effect": target_data.get("deepsleep_effect_ratio") or "0.00%",
        "deepsleep_hour": round(target_data.get("deepsleep_effect_hour") or 0, 1),
        "deepsleep_avg_hour": safe_div(target_data.get("deepsleep_effect_hour"), lte_cell_total, 1),
    }

    # 异常诊断 (对比基线 MAX)
    anomalies = []

    # 定义需要诊断的指标: (字段名, 显示名称, 是否越大越好, 单位转换函数)
    lte_metrics = [
        ("upoctul_dl", "4G上下行业务量", True, to_tb),
        ("avg_energy_efficiency", "4G平均能效", True, lambda x: x),
        ("lte_curmonthpower_rate", "4G节电率", True, lambda x: float(x.replace("%", "")) if isinstance(x, str) and "%" in x else (x or 0)),
        ("lte_station_power", "4G基站总能耗", False, to_wan_du),
        ("single_station_power", "4G单站能耗", False, lambda x: x),
        ("low_energy_total", "4G低能效小区数", False, lambda x: x),
    ]

    for field_name, display_name, higher_is_better, convert_fn in lte_metrics:
        # 获取目标值并转为 float
        target_raw = target_data.get(field_name)
        try:
            target_value = float(convert_fn(target_raw)) if target_raw is not None else 0.0
        except (TypeError, ValueError):
            target_value = 0.0

        # 计算基线最大值
        baseline_values = []
        for row in baseline_data:
            raw_val = row.get(field_name)
            if raw_val is not None:
                try:
                    baseline_values.append(float(convert_fn(raw_val)))
                except (TypeError, ValueError):
                    continue

        if not baseline_values:
            continue

        baseline_max_val = max(baseline_values)
        if baseline_max_val == 0:
            continue

        if higher_is_better:
            # 越大越好，目标值 < 基线MAX * 0.9 为异常
            if target_value < baseline_max_val * 0.9:
                drop_rate = (baseline_max_val - target_value) / baseline_max_val * 100
                anomalies.append(
                    f"{display_name}: 当前 {target_value:.2f}，7天峰值 {baseline_max_val:.2f}，降幅 {drop_rate:.1f}%"
                )
        else:
            # 越小越好，目标值 > 基线MAX * 1.1 为异常
            if target_value > baseline_max_val * 1.1:
                increase_rate = (target_value - baseline_max_val) / baseline_max_val * 100
                anomalies.append(
                    f"{display_name}: 当前 {target_value:.2f}，7天基线 {baseline_max_val:.2f}，上升 {increase_rate:.1f}%"
                )

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
    nr_rru_power_wan = to_wan_du(target_data.get("nr_rru_power"))
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
        "subframe_silence_avg_hour": safe_div(target_data.get("symdown_effect_hour"), nr_cell_total, 1),

        "channel_silence_on": target_data.get("open_chandown_rate") or "0.00%",
        "channel_silence_effect": target_data.get("chandown_effect_ratio") or "0.00%",
        "channel_silence_hour": round(target_data.get("chandown_effect_hour") or 0, 1),
        "channel_silence_avg_hour": safe_div(target_data.get("chandown_effect_hour"), nr_cell_total, 1),

        "shallow_sleep_on": target_data.get("open_carrdown_rate") or "0.00%",
        "shallow_sleep_effect": target_data.get("carrdown_effect_ratio") or "0.00%",
        "shallow_sleep_hour": round(target_data.get("carrdown_effect_hour") or 0, 1),
        "shallow_sleep_avg_hour": safe_div(target_data.get("carrdown_effect_hour"), nr_cell_total, 1),

        "deep_sleep_on": target_data.get("open_deepsleep_rate") or "0.00%",
        "deep_sleep_effect": target_data.get("deepsleep_effect_ratio") or "0.00%",
        "deep_sleep_hour": round(target_data.get("deepsleep_effect_hour") or 0, 1),
        "deep_sleep_avg_hour": safe_div(target_data.get("deepsleep_effect_hour"), nr_cell_total, 1),

        "extreme_sleep_on": target_data.get("open_supersleep_rate") or "0.00%",
        "extreme_sleep_effect": target_data.get("aaurru_supersleep_effect_ratio") or "0.00%",
        "extreme_sleep_hour": round(target_data.get("aaurru_supersleep_effect_hour") or 0, 1),
        "extreme_sleep_avg_hour": safe_div(target_data.get("aaurru_supersleep_effect_hour"), nr_cell_total, 1),
    }

    # 异常诊断
    anomalies = []

    nr_metrics = [
        ("upoctul_dl", "5G上下行业务量", True, to_tb),
        ("sa_avg_energy_efficiency", "5G平均能效", True, lambda x: x),
        ("nr_curmonthpower_rate", "5G节电率", True, lambda x: float(x.replace("%", "")) if isinstance(x, str) and "%" in x else (x or 0)),
        ("nr_sa_station_power", "5G基站总能耗", False, to_wan_du),
        ("single_station_power", "5G单站能耗", False, lambda x: x),
        ("low_energy_total", "5G低能效小区数", False, lambda x: x),
    ]

    for field_name, display_name, higher_is_better, convert_fn in nr_metrics:
        # 获取目标值并转为 float
        target_raw = target_data.get(field_name)
        try:
            target_value = float(convert_fn(target_raw)) if target_raw is not None else 0.0
        except (TypeError, ValueError):
            target_value = 0.0

        # 计算基线最大值
        baseline_values = []
        for row in baseline_data:
            raw_val = row.get(field_name)
            if raw_val is not None:
                try:
                    baseline_values.append(float(convert_fn(raw_val)))
                except (TypeError, ValueError):
                    continue

        if not baseline_values:
            continue

        baseline_max_val = max(baseline_values)
        if baseline_max_val == 0:
            continue

        if higher_is_better:
            if target_value < baseline_max_val * 0.9:
                drop_rate = (baseline_max_val - target_value) / baseline_max_val * 100
                anomalies.append(
                    f"{display_name}: 当前 {target_value:.2f}，7天峰值 {baseline_max_val:.2f}，降幅 {drop_rate:.1f}%"
                )
        else:
            if target_value > baseline_max_val * 1.1:
                increase_rate = (target_value - baseline_max_val) / baseline_max_val * 100
                anomalies.append(
                    f"{display_name}: 当前 {target_value:.2f}，7天基线 {baseline_max_val:.2f}，上升 {increase_rate:.1f}%"
                )

    return processed, anomalies

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
    """使用纯 Python f-string 生成 Markdown 报告。

    Args:
        dist_name: 地市名称。
        prod_name: 厂家名称。
        lte_data: 处理后的 4G 数据。
        nr_data: 处理后的 5G 数据。
        lte_anomalies: 4G 异常列表。
        nr_anomalies: 5G 异常列表。

    Returns:
        Markdown 格式报告。
    """
    # 获取报告日期
    report_date = ""
    if nr_data:
        report_date = nr_data.get("date", "")
    elif lte_data:
        report_date = lte_data.get("date", "")

    lines = []
    lines.append(f"# {dist_name}-{prod_name}-频段({freq_band})-站型({site_type})-区域({area})维度4/5G网络节耗电总体情况")
    lines.append("")

    # ========== 一、网络规模介绍 ==========
    lines.append("## 一、网络规模介绍")
    lines.append("")

    # 5G 网络规模
    if nr_data:
        lines.append("### 5G方面")
        lines.append("")
        nr_high_power_total = (nr_data.get("nr_32t_cell_total") or 0) + (nr_data.get("nr_64t_cell_total") or 0)
        lines.append(
            f"5G方面：逻辑站数{nr_data.get('nr_logic_station_total')}个、"
            f"BBU数{nr_data.get('nr_bbu_channel_total')}个、"
            f"小区数{nr_data.get('nr_cell_total')}个、"
            f"32T+64T高耗电设备小区{nr_high_power_total}个（占比{nr_data.get('nr_high_power_ratio')}），"
            f"4/5G共模站数{nr_data.get('commode_station_total')}个（占比{nr_data.get('commode_ratio')}）；"
        )
        lines.append("")
        lines.append("| 日期 | 5G总BBU数 | 5G逻辑站数量 | 可通过网管读取的5G逻辑站数 | 可读电量逻辑站占比 | 5G总小区数 | 32通道5G小区数 | 64通道5G小区数 | 高耗电小区设备占比 | 4/5G共模站数量 | 共模站点占比 |")
        lines.append("|------|-----------|--------------|----------------------------|--------------------|------------|----------------|----------------|--------------------|----------------|--------------|")
        lines.append(
            f"| {report_date} | {nr_data.get('nr_bbu_channel_total')} | {nr_data.get('nr_logic_station_total')} | "
            f"{nr_data.get('nr_station_onl')} | {nr_data.get('nr_readable_ratio')} | {nr_data.get('nr_cell_total')} | "
            f"{nr_data.get('nr_32t_cell_total')} | {nr_data.get('nr_64t_cell_total')} | {nr_data.get('nr_high_power_ratio')} | "
            f"{nr_data.get('commode_station_total')} | {nr_data.get('commode_ratio')} |"
        )
        lines.append("")

    # 4G 网络规模
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
        lines.append("| 日期 | LTE总BBU数 | LTE逻辑站数量 | 可通过网管读取的LTE逻辑站数量 | 可读电量逻辑站点比 | LTE总小区数 | 8通道及以上小区数 | 高耗电小区设备占比 |")
        lines.append("|------|------------|---------------|-----------------------------|--------------------|-------------|-------------------|--------------------|")
        lines.append(
            f"| {report_date} | {lte_data.get('lte_bbu_channel_total')} | {lte_data.get('lte_logic_station_total')} | "
            f"{lte_data.get('lte_station_onl')} | {lte_data.get('lte_readable_ratio')} | {lte_data.get('lte_cell_total')} | "
            f"{lte_data.get('lte_highchannel_cell_total')} | {lte_data.get('lte_high_power_ratio')} |"
        )
        lines.append("")

    # ========== 二、网络能效介绍 ==========
    lines.append("## 二、网络能效介绍")
    lines.append("")

    # 5G 能效
    if nr_data:
        lines.append("### 5G方面")
        lines.append("")
        lines.append(
            f"5G方面：日流量{nr_data.get('upoctul_dl_tb')}TB、"
            f"基站总能耗{nr_data.get('nr_sa_station_power_wan')}万度、"
            f"节电量{nr_data.get('nr_curmonthpower_wan')}万度，"
            f"节电比例{nr_data.get('nr_curmonthpower_rate')}、"
            f"单站能耗{nr_data.get('nr_perstation_pow')}度、"
            f"5G小区每度电产生业务收入{nr_data.get('sa_avg_energy_efficiency')}GB、"
            f"低能效5G小区数量{nr_data.get('low_energyefficiency_nr_cell_total')}个"
            f"占比{nr_data.get('nr_low_efficiency_ratio')}；"
        )
        lines.append("")
        lines.append("| 日期 | 5G小区BBU总电量（万度） | 5G基站总能耗（万度） | 5G基站按制式分拆总能耗（万度） | 单站能耗（度） | 5G小区平均能效（GB/千瓦时） | 低能效5G小区数 | 低能效5G小区比例 | 5G小区RRU总电量（万度） | NR小区上下行业务量（TB） | 节电量（万度） | 节电比例 |")
        lines.append("|------|------------------------|---------------------|-----------------------------|---------------|---------------------------|----------------|-------------------|------------------------|-------------------------|----------------|----------|")
        lines.append(
            f"| {report_date} | {nr_data.get('nr_bbu_power_wan')} | {nr_data.get('nr_sa_station_power_wan')} | "
            f"{nr_data.get('nr_sa_station_power_wan')} | {nr_data.get('nr_perstation_pow')} | "
            f"{nr_data.get('sa_avg_energy_efficiency')} | {nr_data.get('low_energyefficiency_nr_cell_total')} | "
            f"{nr_data.get('nr_low_efficiency_ratio')} | {nr_data.get('nr_rru_power_wan')} | "
            f"{nr_data.get('upoctul_dl_tb')} | {nr_data.get('nr_curmonthpower_wan')} | {nr_data.get('nr_curmonthpower_rate')} |"
        )
        lines.append("")

    # 4G 能效
    if lte_data:
        lines.append("### 4G方面")
        lines.append("")
        lines.append(
            f"4G方面：日流量{lte_data.get('upoctul_dl_tb')}TB、"
            f"基站总能耗{lte_data.get('lte_station_power_wan')}万度、"
            f"节电量{lte_data.get('lte_curmonthpower_wan')}万度，"
            f"节电比例{lte_data.get('lte_curmonthpower_rate')}、"
            f"单站能耗{lte_data.get('lte_perstation_pow')}度、"
            f"4G小区每度电产生业务收入{lte_data.get('avg_energy_efficiency')}GB、"
            f"低能效4G小区数量{lte_data.get('low_energyefficiency_cell_total')}个"
            f"占比{lte_data.get('lte_low_efficiency_ratio')}；"
        )
        lines.append("")
        lines.append("| 日期 | LTE小区BBU总电量（万度） | LTE基站总能耗（万度） | LTE基站分拆总能耗（万度） | NB基站分拆总能耗（万度） | GSM基站分拆总能耗（万度） | LTE小区平均能效（GB/千瓦时） | 低能效LTE小区数 | 低能效LTE小区比例 | LTE小区RRU总电量（万度） | LTE小区上下行业务量（TB） | 节电量（万度） | 节电比例 |")
        lines.append("|------|-------------------------|----------------------|--------------------------|--------------------------|--------------------------|----------------------------|-----------------|------------|--------------------------|-------------------------|----------------|----------|")
        lines.append(
            f"| {report_date} | {lte_data.get('bbu_power_wan')} | {lte_data.get('lte_station_power_wan')} | "
            f"{lte_data.get('lte_station_power_wan')} | 0 | 0 | "
            f"{lte_data.get('avg_energy_efficiency')} | {lte_data.get('low_energyefficiency_cell_total')} | "
            f"{lte_data.get('lte_low_efficiency_ratio')} | {lte_data.get('rru_power_wan')} | "
            f"{lte_data.get('upoctul_dl_tb')} | {lte_data.get('lte_curmonthpower_wan')} | {lte_data.get('lte_curmonthpower_rate')} |"
        )
        lines.append("")

    # ========== 三、节电软关断情况介绍 ==========
    lines.append("## 三、节电软关断情况介绍")
    lines.append("")

    # 5G 软关断
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
        lines.append("| 日期 | 亚帧静默开启比例 | 亚帧静默生效比例 | 亚帧静默总生效小时 | 亚帧静默平均生效小时 | 通道静默开启比例 | 通道静默生效比例 | 通道静默总生效小时 | 通道静默平均生效小时 | 浅层休眠开启比例 | 浅层休眠生效比例 | 浅层休眠总生效小时 | 浅层休眠平均生效小时 | 深度休眠开启比例 | 深度休眠生效比例 | 深度休眠总生效小时 | 深度休眠平均生效小时 | 极致休眠开启比例 | 极致休眠生效比例 | 极致休眠总生效小时 | 极致休眠平均生效小时 |")
        lines.append("|------|------------------|------------------|--------------------|----------------------|------------------|------------------|--------------------|----------------------|------------------|------------------|--------------------|----------------------|------------------|------------------|--------------------|----------------------|------------------|------------------|--------------------|----------------------|")
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

    # 4G 软关断
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
        lines.append("| 日期 | 符号关断开启比例 | 符号关断生效比例 | 符号关断总生效小时 | 符号关断平均生效小时 | 通道关断开启比例 | 通道关断生效比例 | 通道关断总生效小时 | 通道关断平均生效小时 | 载波关断开启比例 | 载波关断生效比例 | 载波关断总生效小时 | 载波关断平均生效小时 | 深度休眠开启比例 | 深度休眠生效比例 | 深度休眠总生效小时 | 深度休眠平均生效小时 |")
        lines.append("|------|------------------|------------------|--------------------|----------------------|------------------|------------------|--------------------|----------------------|------------------|------------------|--------------------|----------------------|------------------|------------------|--------------------|----------------------|")
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

    # ========== 四、异常波动指标定位 ==========
    lines.append("## 四、异常波动指标定位")
    lines.append("")

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

    return "\n".join(lines)

async def _get_latest_report_date(db: AsyncSession) -> str:
    """查询数据库中报表数据的最新日期。"""
    sql = text(f"""
        SELECT MAX(data_date) FROM (
            SELECT MAX(data_date) AS data_date FROM {DB_SCHEMA}.lte_report_day_collect
            UNION ALL
            SELECT MAX(data_date) FROM {DB_SCHEMA}.nr_report_day_collect
        ) t
    """)
    result = await db.execute(sql)
    row = result.scalar()
    if row:
        return row.strftime("%Y-%m-%d") if hasattr(row, 'strftime') else str(row)[:10]
    return ""


@tool_registry.tool(
    description="""生成 4G/5G 网络节耗电分析报告。查询指定时间段汇总数据，前推7天获取基线，对比生成 Markdown 报告。
未指定日期时自动查询数据库最新数据日期。""",
    parameters={
        "type": "object",
        "properties": {
            "dist_name": {
                "type": "string",
                "description": "地市名称，如长沙市",
            },
            "prod_name": {
                "type": "string",
                "description": "设备厂家",
            },
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
                "description": "区域",
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
    },
)
async def query_report(
    db: AsyncSession,
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
        dist_name: 地市名称，未传时默认全网。
        prod_name: 设备厂家，未传时默认全网。
        freq_band: 频段，未传时默认全网。
        site_type: 站型，未传时默认全网。
        area: 区域，未传时默认全网。
        date_start: 报告日期，会暗中前推7天，未传时默认昨天。
        date_end: 结束日期，未传时默认与 date_start 相同。

    Returns:
        包含报告和数据的字典。
    """
    dist_name = dist_name or "全网"
    prod_name = prod_name or "全网"
    freq_band = freq_band or "全网"
    site_type = site_type or "全网"
    area = area or "全网"
    if not date_start:
        date_start = await _get_latest_report_date(db)
        if not date_start:
            return {"success": False, "error": "数据库中暂无报表数据"}
    date_end = date_end or date_start

    logger.info(
        "报表查询: dist_name=%s, prod_name=%s, freq_band=%s, site_type=%s, area=%s, date_range=%s~%s",
        dist_name,
        prod_name,
        freq_band,
        site_type,
        area,
        date_start,
        date_end,
    )

    try:
        # 1. 计算查询起始日期（前推7天）
        date_start_dt = datetime.strptime(date_start, "%Y-%m-%d")
        query_start_dt = date_start_dt - timedelta(days=7)
        query_start = query_start_dt.strftime("%Y-%m-%d")

        logger.info("暗中前推7天获取基线: %s ~ %s", query_start, date_end)

        # 2. 拉取原始数据（目标日 + 基线）
        lte_target_raw, lte_baseline_raw = await _fetch_lte_data_with_baseline(
            db, dist_name, prod_name, freq_band, site_type, area, query_start, date_end
        )
        nr_target_raw, nr_baseline_raw = await _fetch_nr_data_with_baseline(
            db, dist_name, prod_name, freq_band, site_type, area, query_start, date_end
        )

        if not lte_target_raw and not nr_target_raw:
            return {
                "success": False,
                "error": "未找到指定时间段内的数据",
            }

        # 3. Python 预处理（单位换算 + 异常诊断）
        lte_data, lte_anomalies = _process_lte_data(lte_target_raw, lte_baseline_raw)
        nr_data, nr_anomalies = _process_nr_data(nr_target_raw, nr_baseline_raw)

        # 4. 纯 Python 生成报告（零 LLM 依赖）
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
            "dist_name": dist_name,
            "prod_name": prod_name,
            "freq_band": freq_band,
            "site_type": site_type,
            "area": area,
            "date_start": date_start,
            "date_end": date_end,
            "baseline_range": f"{query_start} ~ {date_end}",
            "report_content": report_markdown,
            "lte_anomalies": lte_anomalies,
            "nr_anomalies": nr_anomalies,
            "has_anomaly": len(lte_anomalies) > 0 or len(nr_anomalies) > 0,
        }

    except Exception as exc:
        logger.error("报表查询异常: %s", exc, exc_info=True)
        return {
            "success": False,
            "error": f"报表生成失败: {exc}",
        }
