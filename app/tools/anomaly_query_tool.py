"""
异常指标诊断工具模块。

专门用于对比目标日期与过去 7 天的历史基线，找出数值劣化超过 10% 的核心指标。
核心原则: 纯 Python 逻辑诊断，零 LLM 参与。
"""

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings, MAX_RETURN_ITEMS
from app.core.logging import get_logger
from app.tools.registry import tool_registry
from app.utils.export_util import truncate_and_export

logger = get_logger("anomaly_query_tool")

# 获取数据库 schema
DB_SCHEMA = get_settings().db_schema


# 4G 核心能效指标配置: (字段名, 显示名称, 是否越大越好)
LTE_CORE_METRICS = [
    ("upoctul_dl", "上下行流量(GB)", True),
    ("avg_energy_efficiency", "平均能效(GB/度)", True),
    ("lte_curmonthpower_rate", "节电率(%)", True),
    ("lte_station_power", "基站总能耗(度)", False),  # 越小越好，新增
    ("single_station_power", "单站能耗(度)", False),  # 越小越好
    ("low_energy_total", "低能效小区数", False),  # 越小越好
]


# 5G 核心能效指标配置: (字段名, 显示名称, 是否越大越好)
NR_CORE_METRICS = [
    ("upoctul_dl", "上下行流量(GB)", True),
    ("sa_avg_energy_efficiency", "平均能效(GB/度)", True),
    ("nr_curmonthpower_rate", "节电率(%)", True),
    ("nr_sa_station_power", "基站总能耗(度)", False),  # 越小越好，新增
    ("single_station_power", "单站能耗(度)", False),  # 越小越好
    ("low_energy_total", "低能效小区数", False),  # 越小越好
]


def _calculate_baseline_start(target_date: str) -> str:
    """基于目标日期计算过去7天的起始日期。

    Args:
        target_date: 目标日期 (YYYY-MM-DD)。

    Returns:
        7天前的日期字符串 (YYYY-MM-DD)。
    """
    target = datetime.strptime(target_date, "%Y-%m-%d")
    baseline_start = target - timedelta(days=7)
    return baseline_start.strftime("%Y-%m-%d")


def _to_float(value: Any) -> float | None:
    """将可能包含百分号或逗号的字符串安全转换为浮点数。

    Args:
        value: 原始值，可能是 int、float、str、Decimal 或 None。

    Returns:
        转换后的浮点数，若转换失败则返回 None。
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        # 去除首尾空格，移除百分号，移除千位分隔符，然后转换
        cleaned = value.strip().replace('%', '').replace(',', '')
        try:
            return float(cleaned)
        except ValueError:
            logger.warning(f"无法将字符串转换为浮点数: {value}")
            return None
    # 处理 Decimal 类型（SQLAlchemy 从数据库返回的数值类型）
    if isinstance(value, Decimal):
        return float(value)
    # 其他类型（如 bool）视作无效
    return None


def _detect_anomalies(
    target_data: dict[str, Any] | None,
    baseline_data: list[dict[str, Any]],
    metrics_config: list[tuple[str, str, bool]],
) -> list[dict[str, Any]]:
    """检测异常指标。

    核心算法:
    1. 遍历基线数据，求出各指标的 MAX
    2. 判断: 如果目标日指标 < 基线 MAX * 0.9，则记录为异常

    Args:
        target_data: 目标日数据。
        baseline_data: 基线数据列表(最多7条)。
        metrics_config: 指标配置列表 (字段名, 显示名称, 是否越大越好)。

    Returns:
        异常指标列表。
    """
    anomalies = []

    if not target_data or not baseline_data:
        return anomalies

    # 计算基线各指标的 MAX
    baseline_max: dict[str, float] = {}
    for field_name, _, _ in metrics_config:
        # 提取该字段在基线中的有效数值，并安全转换为浮点数
        values = [
            _to_float(row.get(field_name)) for row in baseline_data
            if _to_float(row.get(field_name)) is not None
        ]
        if values:
            baseline_max[field_name] = max(values)
        else:
            baseline_max[field_name] = 0.0

    # 检测目标日各指标是否劣化
    for field_name, display_name, higher_is_better in metrics_config:
        target_value = _to_float(target_data.get(field_name))
        baseline_value = baseline_max.get(field_name, 0.0)

        if target_value is None or baseline_value == 0:
            continue

        if higher_is_better:
            # 指标越大越好，目标值 < 基线MAX * 0.9 为异常
            threshold = baseline_value * 0.9
            if target_value < threshold:
                drop_rate = (baseline_value - target_value) / baseline_value * 100
                anomalies.append({
                    "metric_name": display_name,
                    "current_value": round(target_value, 2),
                    "baseline_max": round(baseline_value, 2),
                    "drop_rate": f"{drop_rate:.1f}%",
                    "severity": "high" if drop_rate > 20 else "medium",
                })
        else:
            # 指标越小越好，目标值 > 基线MAX * 1.1 为异常
            threshold = baseline_value * 1.1
            if target_value > threshold:
                increase_rate = (target_value - baseline_value) / baseline_value * 100
                anomalies.append({
                    "metric_name": display_name,
                    "current_value": round(target_value, 2),
                    "baseline_max": round(baseline_value, 2),
                    "increase_rate": f"{increase_rate:.1f}%",
                    "severity": "high" if increase_rate > 20 else "medium",
                })

    # 按严重程度排序
    anomalies.sort(key=lambda x: x.get("severity") == "medium")
    return anomalies


async def _fetch_network_data(
    db: AsyncSession,
    dist_name: str,
    prod_name: str,
    baseline_start: str,
    target_date: str,
    table: str,
    column_names: list[str],
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """通用 4G/5G 数据拉取函数，表名和列名参数化。

    Args:
        db: 数据库会话。
        dist_name: 地市名称。
        prod_name: 厂家名称。
        baseline_start: 基线起始日期。
        target_date: 目标日期。
        table: 数据库表名。
        column_names: 查询的列名列表（不含 data_date）。

    Returns:
        (target_data, baseline_data): 目标日期数据和基线数据列表。
    """
    all_columns = ["data_date"] + column_names
    columns_sql = ",\n            ".join(all_columns)

    sql = text(f"""
        SELECT
            {columns_sql}
        FROM {DB_SCHEMA}.{table}
        WHERE dist_name = :dist_name
          AND prod_name = :prod_name
          AND data_date BETWEEN :baseline_start AND :target_date
        ORDER BY data_date DESC
    """)

    result = await db.execute(
        sql,
        {
            "dist_name": dist_name,
            "prod_name": prod_name,
            "baseline_start": baseline_start,
            "target_date": target_date,
        },
    )
    rows = result.mappings().all()

    if not rows:
        return None, []

    target_data = None
    baseline_data = []

    for row in rows:
        row_dict = dict(row)
        if str(row_dict.get("data_date")) == target_date:
            target_data = row_dict
        else:
            baseline_data.append(row_dict)

    return target_data, baseline_data


@tool_registry.tool(
    description="""诊断核心指标劣化情况。对比目标日期与过去7天基线，找出劣化超过10%的指标。""",
    parameters={
        "type": "object",
        "properties": {
            "dist_name": {"type": "string", "description": "地市名称"},
            "prod_name": {"type": "string", "description": "设备厂家"},
            "target_date": {
                "type": "string",
                "description": "目标诊断日期 (YYYY-MM-DD)",
            },
            "export_excel": {
                "type": "boolean",
                "description": "是否导出 Excel",
                "default": False,
            },
        },
        "required": [],
    },
)
async def query_anomaly(
    db: AsyncSession,
    dist_name: str | None = None,
    prod_name: str | None = None,
    target_date: str | None = None,
    export_excel: bool = False,
) -> dict[str, Any]:
    """诊断特定日期是否有核心指标劣化超过10%。

    Args:
        dist_name: 地市名称，默认"全网"。
        prod_name: 设备厂家，默认"全网"。
        target_date: 目标诊断日期 (YYYY-MM-DD)，默认昨天。
        db: 数据库会话。

    Returns:
        包含异常指标列表的字典。
    """
    dist_name = dist_name or "全网"
    prod_name = prod_name or "全网"
    target_date = target_date or (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    logger.info(
        "异常诊断: dist_name=%s, prod_name=%s, target_date=%s",
        dist_name,
        prod_name,
        target_date,
    )

    try:
        # 1. 计算基线起始日期
        baseline_start = _calculate_baseline_start(target_date)
        logger.debug("基线日期范围: %s ~ %s", baseline_start, target_date)

        # 2. 拉取 4G 和 5G 数据
        lte_columns = [name for name, _, _ in LTE_CORE_METRICS]
        nr_columns = [name for name, _, _ in NR_CORE_METRICS]

        lte_target, lte_baseline = await _fetch_network_data(
            db, dist_name, prod_name, baseline_start, target_date,
            table="lte_report_day_collect", column_names=lte_columns,
        )
        nr_target, nr_baseline = await _fetch_network_data(
            db, dist_name, prod_name, baseline_start, target_date,
            table="nr_report_day_collect", column_names=nr_columns,
        )

        # 3. 检测异常
        lte_anomalies = _detect_anomalies(lte_target, lte_baseline, LTE_CORE_METRICS)
        nr_anomalies = _detect_anomalies(nr_target, nr_baseline, NR_CORE_METRICS)
        logger.info("4G指标: %s", lte_target)
        logger.info("4G基线: %s", lte_baseline)
        logger.info("4G异常指标: %s", lte_anomalies)
        logger.info("5G指标: %s", nr_target)
        logger.info("5G基线: %s", nr_baseline)
        logger.info("5G异常指标: %s", nr_anomalies)

        # 合并4G/5G异常数据
        combined_anomalies = []
        for item in lte_anomalies:
            item["network_type"] = "4G"
            combined_anomalies.append(item)
        for item in nr_anomalies:
            item["network_type"] = "5G"
            combined_anomalies.append(item)

        returned_combined, meta = truncate_and_export(
            combined_anomalies, prefix="anomaly_diagnosis", export_excel=export_excel
        )
        if meta["is_truncated"]:
            logger.info("数据量超过%d条，已截断返回前%d条", MAX_RETURN_ITEMS, MAX_RETURN_ITEMS)

        returned_lte = lte_anomalies[:MAX_RETURN_ITEMS] if len(lte_anomalies) > MAX_RETURN_ITEMS else lte_anomalies
        returned_nr = nr_anomalies[:MAX_RETURN_ITEMS] if len(nr_anomalies) > MAX_RETURN_ITEMS else nr_anomalies

        # 4. 组装返回结果
        download_url = meta.pop("download_url", None)
        result = {
            "success": True,
            **({"download_url": download_url} if download_url else {}),
            "target_date": target_date,
            "baseline_range": f"{baseline_start} ~ {target_date}",
            "lte_anomalies": returned_lte,
            "nr_anomalies": returned_nr,
            "has_anomaly": len(lte_anomalies) > 0 or len(nr_anomalies) > 0,
            "total_anomaly_count": meta.pop("total_count"),
            "returned_count": meta.pop("returned_count"),
            "is_truncated": meta.pop("is_truncated"),
            **meta,
        }

        if lte_anomalies:
            result["lte_anomaly_count"] = len(lte_anomalies)
        if nr_anomalies:
            result["nr_anomaly_count"] = len(nr_anomalies)

        logger.info(
            "异常诊断完成: 4G异常%d个, 5G异常%d个",
            len(lte_anomalies),
            len(nr_anomalies),
        )

        return result

    except Exception as exc:
        logger.error("异常诊断失败: %s", exc, exc_info=True)
        return {
            "success": False,
            "error": f"异常诊断失败: {exc}",
        }