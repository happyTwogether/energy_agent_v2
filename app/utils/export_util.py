"""
全局 Excel 导出服务模块。

提供统一的字典列表导出为 Excel 功能，基于服务端配置生成可下载的静态文件链接。
遵循 Facade 模式，封装 pandas 导出逻辑和文件路径管理。
"""

import os
import uuid
from datetime import datetime

import pandas as pd

from app.core.config import get_settings, MAX_RETURN_ITEMS
from app.core.logging import get_logger

logger = get_logger("export_util")

# 全局导出目录配置
EXPORT_DIR = os.path.join(os.getcwd(), get_settings().export_dir)

# 默认列名中文映射表（项目级统一配置）
DEFAULT_COLUMN_MAPPING = {
    # 通用字段
    "cgi": "CGI",
    "cell_name": "小区名称",
    "enodeb_name": "基站名称",
    "dist_name": "地市",
    "prod_name": "厂家",
    "data_date": "日期",
    "station_type": "站点类型",
    "freq_band": "频段",
    "network_type": "网络类型",
    # 能效指标
    "upoctul_dl": "上下行流量(GB)",
    "avg_energy_efficiency": "平均能效(GB/度)",
    "sa_avg_energy_efficiency": "SA平均能效(GB/度)",
    "lte_curmonthpower_rate": "4G节电率(%)",
    "nr_curmonthpower_rate": "5G节电率(%)",
    "single_station_power": "单站能耗(度)",
    "lte_station_power": "4G基站总能耗(度)",
    "nr_sa_station_power": "5G基站总能耗(度)",
    "low_energy_total": "低能效小区数",
    # 异常诊断相关
    "current_value": "当前值",
    "baseline_max": "基线最大值",
    "drop_rate": "下降幅度",
    "increase_rate": "上升幅度",
    "metric_name": "指标名称",
    "severity": "严重程度",
    # 参数核查相关
    "chn_field_name": "参数中文名称",
    "saving_para_name": "参数英文",
    "objtype_name": "节能技术",
    "powertype_name": "节能类型",
    "saving_para_value": "现网配置值",
    "recommend_value": "指导意见值",
    "saving_switch_state": "核查结果",
    "subjective_reason": "主观原因",
    "objective_reason": "客观原因",
    "check_time": "核查时间",
    "rru_aau": "RRU/AAU型号",
    "chn_num": "通道数",
    #节电扩展相关
    "county_name": "区县",
    "work_band": "工作频段",
    "cover_type": "覆盖类型",
    "cover_scen": "覆盖场景",
    "hour_detail": "小时级业务分布",
    "avg_low_flow_pct": "夜间低业务占比",
}


def export_to_excel(
    data_list: list[dict],
    prefix: str = "export",
    column_mapping: dict[str, str] | None = None,
) -> str | None:
    """
    将字典列表导出为 Excel 文件，并返回下载链接。

    Args:
        data_list: 从数据库查询出的字典列表数据。
        prefix: 导出文件名的前缀，如 "energy_report"、"batch_result"。
        column_mapping: 列名映射字典，支持三种模式：
            - None: 使用 DEFAULT_COLUMN_MAPPING 自动转换（推荐）
            - {}: 禁用映射，保持原字段名
            - {"cgi": "小区标识"}: 与默认映射合并，传入的优先级更高

    Returns:
        str: 前端可访问的下载 URL；若未配置 base_url，则返回相对路径（如 "/downloads/energy-report-20250324120000-a1b2c3.xlsx"）。
        None: 当数据为空或导出失败时返回 None。

    Examples:
        >>> data = [{"cell_id": "A1", "energy": 100}]
        >>> url = export_to_excel(data, prefix="report")  # 使用默认映射
        >>> url = export_to_excel(data, prefix="report", column_mapping={})  # 禁用映射
    """
    if not data_list:
        logger.warning("导出数据为空，跳过 Excel 生成")
        return None

    try:
        # 转换为 DataFrame
        df = pd.DataFrame(data_list)

        # 处理列名映射
        if column_mapping is None:
            # 使用默认映射（只映射存在的列）
            effective_mapping = {k: v for k, v in DEFAULT_COLUMN_MAPPING.items() if k in df.columns}
            df = df.rename(columns=effective_mapping)
        elif column_mapping:
            # 合并模式：传入的映射与默认映射合并，传入的优先级更高
            merged_mapping = {**DEFAULT_COLUMN_MAPPING, **column_mapping}
            effective_mapping = {k: v for k, v in merged_mapping.items() if k in df.columns}
            df = df.rename(columns=effective_mapping)
        # column_mapping == {} 时，不做任何映射，保持原字段名

        # 组装防并发的唯一文件名: 前缀-年月日时分秒-uuid前6位.xlsx
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        unique_id = uuid.uuid4().hex[:6]
        safe_prefix = prefix.replace("_", "-")
        filename = f"{safe_prefix}-{timestamp}-{unique_id}.xlsx"

        # 确保目录存在 (二次保险)
        os.makedirs(EXPORT_DIR, exist_ok=True)

        file_path = os.path.join(EXPORT_DIR, filename)

        # 导出 Excel (使用 openpyxl 引擎)
        df.to_excel(file_path, index=False, engine="openpyxl")

        logger.info("Excel 导出成功: %s, 数据量: %d 条", file_path, len(df))

        # 生成完整下载链接（使用服务配置的 base_url；未配置时返回相对路径）
        relative_url = f"/downloads/{filename}"
        base_url = get_settings().base_url
        if base_url:
            return f"{base_url.rstrip('/')}{relative_url}"
        return relative_url

    except Exception as e:
        logger.error("Excel 导出失败: %s", e, exc_info=True)
        return None


def cleanup_old_exports(max_age_hours: int = 24) -> int:
    """
    清理过期的导出文件。

    Args:
        max_age_hours: 文件最大保留时间（小时），默认 24 小时。

    Returns:
        int: 成功删除的文件数量。
    """
    import time

    if not os.path.exists(EXPORT_DIR):
        return 0

    deleted_count = 0
    current_time = time.time()
    max_age_seconds = max_age_hours * 3600

    try:
        for filename in os.listdir(EXPORT_DIR):
            if not filename.endswith(".xlsx"):
                continue

            file_path = os.path.join(EXPORT_DIR, filename)
            file_mtime = os.path.getmtime(file_path)

            if current_time - file_mtime > max_age_seconds:
                os.remove(file_path)
                deleted_count += 1
                logger.debug("删除过期导出文件: %s", filename)

        if deleted_count > 0:
            logger.info("清理过期导出文件完成，共删除 %d 个文件", deleted_count)

    except Exception as e:
        logger.error("清理导出文件失败: %s", e, exc_info=True)

    return deleted_count


def truncate_and_export(
    data: list[dict],
    prefix: str,
    export_excel: bool = False,
    max_items: int = MAX_RETURN_ITEMS,
) -> tuple[list[dict], dict]:
    """截断数据列表并可选导出 Excel，返回 (截断后数据, 元信息字典)。

    Args:
        data: 原始数据列表。
        prefix: 导出文件名前缀。
        export_excel: 用户是否显式要求导出。
        max_items: 截断阈值，默认使用全局配置 MAX_RETURN_ITEMS。

    Returns:
        (截断后数据, 元信息字典):
        - is_truncated: 是否截断
        - total_count: 原始数据条数
        - returned_count: 返回数据条数
        - download_url: Excel 下载链接（仅导出时）
        - auto_exported: 是否自动导出（仅导出时）
    """
    total = len(data)
    is_truncated = total > max_items
    result_data = data[:max_items] if is_truncated else data

    meta: dict = {
        "is_truncated": is_truncated,
        "total_count": total,
        "returned_count": len(result_data),
    }

    should_export = export_excel or is_truncated
    if should_export and data:
        url = export_to_excel(data, prefix=prefix)
        if url:
            meta["download_url"] = url
            meta["auto_exported"] = not export_excel

    return result_data, meta
