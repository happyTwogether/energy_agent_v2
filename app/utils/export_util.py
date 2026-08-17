"""
全局 Excel 导出服务模块。

提供统一的字典列表导出为 Excel 功能，基于服务端配置生成可下载的静态文件链接。
遵循 Facade 模式，封装 pandas 导出逻辑和文件路径管理。
"""

import json
import os
import uuid
from datetime import datetime
from typing import Any

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
    # 节电扩展相关
    "county_name": "区县",
    "work_band": "工作频段",
    "cover_type": "覆盖类型",
    "cover_scen": "覆盖场景",
    "hour_detail": "小时级业务分布",
    "avg_low_flow_pct": "夜间低业务占比",
    "hour_filter": "未休眠扩展时段小时列表(含扩展时段)",
    "hour_int": "未休眠可扩展时间数量(含扩展时段)",
    "hour_filter_early": "未休眠可扩展时段小时列表(仅夜间常规时段)",
    "hour_int_early": "未休眠可扩展时间数量(仅夜间常规时段)",
    "deploy_hours": "含已休眠部署时段_不连续(含扩展时段)",
    "deploy_hours_continuous": "含已休眠连续部署时段(含扩展时段)",
    "deploy_hours_early": "含已休眠部署时段_不连续(仅夜间常规时段)",
    "deploy_hours_continuous_early": "含已休眠连续部署时段(仅夜间常规时段)",
    # 节电收缩相关
    "around_cgi": "邻区CGI",
    "around_cgi_cell_name": "邻区名称",
    "around_cgi_network_type": "邻区网络制式",
    "around_network_type": "邻区网络制式(归一化)",
    "site_type": "主小区原始站型",
    "self_prb_rate_ul_before": "主小区休眠前上行PRB利用率",
    "self_prb_rate_dl_before": "主小区休眠前下行PRB利用率",
    "self_sleep_duration": "主小区深度+超级休眠时长(秒)",
    "prb_rate_ul": "邻区休眠期间上行PRB利用率",
    "prb_rate_dl": "邻区休眠期间下行PRB利用率",
    "prb_rate_ul_before": "邻区休眠前上行PRB利用率",
    "prb_rate_dl_before": "邻区休眠前下行PRB利用率",
    "prb_increase": "邻区PRB抬升量",
    "before_sleep_hour": "休眠前小时",
    "before_sleep_date": "休眠前日期",
    "distance": "邻区距离(米)",
    "main_site_type": "主小区站型",
    "around_site_type": "邻区站型",
    "relation_status": "邻区关系校验状态",
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
        dataframe = pd.DataFrame(data_list)
        dataframe = _rename_export_columns(dataframe, column_mapping)
        filename, file_path = _new_export_file(prefix)
        dataframe.to_excel(file_path, index=False, engine="openpyxl")
        logger.info("Excel 导出成功: %s, 数据量: %s 条", file_path, len(dataframe))
        return _download_url(filename)
    except Exception as exc:
        logger.error("Excel 导出失败: %s", exc, exc_info=True)
        return None


def export_sheets_to_excel(
    sheets: dict[str, list[dict]],
    prefix: str,
    column_mapping: dict[str, str] | None = None,
) -> str | None:
    """将多类明细写入一个 Excel，跳过没有数据的工作表。"""
    non_empty_sheets = {
        sheet_name: rows
        for sheet_name, rows in sheets.items()
        if rows
    }
    if not non_empty_sheets:
        logger.warning("导出数据为空，跳过多工作表 Excel 生成")
        return None

    try:
        filename, file_path = _new_export_file(prefix)

        with pd.ExcelWriter(file_path, engine="openpyxl") as writer:
            for sheet_name, rows in non_empty_sheets.items():
                dataframe = pd.DataFrame(rows)
                dataframe = _rename_export_columns(dataframe, column_mapping)
                dataframe.to_excel(
                    writer,
                    sheet_name=sheet_name[:31],
                    index=False,
                )

        logger.info(
            "多工作表 Excel 导出成功: %s, 工作表数: %s",
            file_path,
            len(non_empty_sheets),
        )
        return _download_url(filename)
    except Exception as exc:
        logger.error("多工作表 Excel 导出失败: %s", exc, exc_info=True)
        return None


def _rename_export_columns(
    dataframe: pd.DataFrame,
    column_mapping: dict[str, str] | None,
) -> pd.DataFrame:
    dataframe = dataframe.copy()
    for column in dataframe.columns:
        dataframe[column] = dataframe[column].map(_serialize_export_value)
    if column_mapping == {}:
        return dataframe
    effective_mapping = DEFAULT_COLUMN_MAPPING
    if column_mapping:
        effective_mapping = {**DEFAULT_COLUMN_MAPPING, **column_mapping}
    existing_mapping = {
        key: value
        for key, value in effective_mapping.items()
        if key in dataframe.columns
    }
    return dataframe.rename(columns=existing_mapping)


def _serialize_export_value(value: Any) -> Any:
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, default=str)
    if isinstance(value, set):
        return json.dumps(sorted(value), ensure_ascii=False, default=str)
    return value


def _new_export_file(prefix: str) -> tuple[str, str]:
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    unique_id = uuid.uuid4().hex[:6]
    safe_prefix = prefix.replace("_", "-")
    filename = f"{safe_prefix}-{timestamp}-{unique_id}.xlsx"
    os.makedirs(EXPORT_DIR, exist_ok=True)
    return filename, os.path.join(EXPORT_DIR, filename)


def _download_url(filename: str) -> str:
    relative_url = f"/downloads/{filename}"
    base_url = get_settings().base_url
    return f"{base_url.rstrip('/')}{relative_url}" if base_url else relative_url


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
