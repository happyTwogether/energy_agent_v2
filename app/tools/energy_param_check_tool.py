"""
节能参数核查工具模块。

查询小区节能参数核查结果，生成标准化 Markdown 报告。

表设计规则:
- 统一表名: {schema}.eng_check_result
- 按 check_time 进行分区和日期过滤

日期规则:
- 用户未传 check_date 时: 自动查询数据库中最新日期
- 数据库查询失败时回退: 当前时间 < 15:00 用昨天，>= 15:00 用今天

核心原则: 纯 Python 查询与报告生成，零 LLM 依赖。
"""

from datetime import datetime, timedelta, time
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings, MAX_RETURN_ITEMS
from app.core.logging import get_logger
from app.tools.registry import tool_registry
from app.utils.export_util import export_to_excel

logger = get_logger("param_check_tool")

# 获取数据库 schema（规则表所在 schema）
DB_SCHEMA_RULE = get_settings().db_schema_rule

# 表示数据不可用（非合规也非不合规）的 saving_switch_state 值
_EXCLUDED_STATES = frozenset({"北向数据缺失", "空值"})
_COMPLIANT_STATE = "合格"
_STATES_NOT_UNQUALIFIED = frozenset({_COMPLIANT_STATE}) | _EXCLUDED_STATES


async def _get_check_date(db: AsyncSession, user_date: str | None) -> tuple[str, str]:
    """确定核查日期，返回 (date_str, date_note)。

    始终先查 DB 最新日期；用户指定日期超出数据范围时自动兜底。
    """
    # 先查 DB 最新日期
    db_latest: datetime | None = None
    try:
        sql = text(f"SELECT MAX(check_time) AS latest FROM {DB_SCHEMA_RULE}.eng_check_result")
        result = await db.execute(sql)
        row = result.mappings().first()
        if row and row.get("latest"):
            db_latest = row["latest"]
    except Exception:
        pass

    if user_date:
        user_str = user_date.replace("-", "")
        if db_latest and hasattr(db_latest, "strftime"):
            db_str = db_latest.strftime("%Y%m%d")
            if user_str > db_str:
                return db_str, f"您查询的日期 {user_date} 暂无数据，已自动查询最新数据日期 {db_latest.strftime('%Y-%m-%d')}"
        return user_str, ""

    if db_latest and hasattr(db_latest, "strftime"):
        return db_latest.strftime("%Y%m%d"), ""

    # 回退到时间推断
    now = datetime.now()
    if now.hour < 15:
        return (now - timedelta(days=1)).strftime("%Y%m%d"), ""
    return now.strftime("%Y%m%d"), ""


def _build_check_time_range(check_date: str) -> tuple[datetime, datetime]:
    """根据核查日期构造 check_time 的闭开区间。"""
    date_value = datetime.strptime(check_date, "%Y%m%d").date()
    start_time = datetime.combine(date_value, time.min)
    end_time = start_time + timedelta(days=1)
    return start_time, end_time


async def _fetch_param_check(
    db: AsyncSession,
    check_date: str,
    cgi: str | None = None,
    cell_name: str | None = None,
    dist_name: str | None = None,
    prod_name: str | None = None,
) -> list[dict[str, Any]]:
    """查询节能参数核查数据。

    查询优先级:
    1. cgi（最精确）
    2. cell_name
    3. dist_name + prod_name
    """
    start_time, end_time = _build_check_time_range(check_date)
    conditions = ["check_time >= :start_time", "check_time < :end_time"]
    params: dict[str, Any] = {
        "start_time": start_time,
        "end_time": end_time,
    }

    # 数据驱动筛选器构建：cgi 优先，其余独立可选
    filter_specs: list[tuple[str, str, Any, bool]] = [
        ("cgi = :cgi",               "cgi",       cgi,        bool(cgi)),
        ("cell_name = :cell_name",   "cell_name", cell_name,  bool(cell_name) and not cgi),
        ("dist_name = :dist_name",   "dist_name", dist_name,  bool(dist_name) and not cgi),
        ("prod_name = :prod_name",   "prod_name", prod_name,  bool(prod_name) and not cgi),
    ]
    for template, key, value, active in filter_specs:
        if active and value:
            conditions.append(template)
            params[key] = value

    where_clause = " AND ".join(conditions)

    sql = text(f"""
        SELECT
            check_time,
            prod_name,
            enodeb_name,
            cell_name,
            cgi,
            station_type,
            rru_aau,
            chn_num,
            freq_band,
            saving_para_name,
            chn_field_name,
            objtype_name,
            powertype_name,
            saving_para_value,
            recommend_value,
            saving_switch_state,
            subjective_reason,
            objective_reason
        FROM {DB_SCHEMA_RULE}.eng_check_result
        WHERE {where_clause}
    """)
    try:
        result = await db.execute(sql, params)
        rows = result.mappings().all()
        return [dict(row) for row in rows]
    except ProgrammingError as exc:
        # 表不存在或其他 SQL 错误，需要回滚事务才能继续使用
        await db.rollback()
        if "UndefinedTableError" in str(exc) or "不存在" in str(exc):
            logger.warning("核查表不存在: %s.eng_check_result", DB_SCHEMA_RULE)
            return []
        logger.error("参数核查 SQL 错误: %s", exc)
        return []
    except Exception as exc:
        # 其他异常也需要回滚
        await db.rollback()
        logger.error("参数核查查询异常: %s", exc)
        return []


def _build_summary_lines(
    lines: list[str],
    total_count: int,
    qualified_count: int,
    unqualified_count: int,
    unqualified_cgi_count: int,
    excluded_count: int,
    is_single_cgi: bool,
    detail_items: list[dict[str, Any]] | None,
) -> None:
    """追加核查结论与统计行到 lines 列表。

    detail_items=None 时仅显示摘要，否则附带明细表格。
    """
    if unqualified_count == 0:
        # 全部合规
        lines.append("**核查结论：✅ 合规**")
        lines.append("")
        prefix = "该小区" if is_single_cgi else ""
        lines.append(f"- 经核查，{prefix}共 {qualified_count} 项节能参数均符合规范要求，状态合格。")
        if excluded_count:
            lines.append(f"- 其中 {excluded_count} 项参数因北向数据缺失/空值无法判断，已排除统计")
        return

    # 存在不合规项
    lines.append("**核查结论：❌ 不合规**")
    lines.append("")
    lines.append(f"- 共核查 {total_count} 项参数，其中合格 {qualified_count} 项，**不合规 {unqualified_count} 项**")
    if not is_single_cgi:
        lines.append(f"- 不合规小区涉及 **{unqualified_cgi_count}** 个")
    if excluded_count:
        lines.append(f"- 其中 {excluded_count} 项参数因北向数据缺失/空值无法判断，已排除统计")

    if detail_items is None:
        # 仅摘要模式
        lines.append("- 数据量较大，详细明细请查看 Excel 下载文件")
        return

    # 明细表格
    lines.append("")
    lines.append("#### 不合规参数明细")

    if is_single_cgi:
        lines.append("| 参数中文名称 | 参数英文 | 节能技术 | 节能类型 | 现网配置值 | 指导意见值 | 核查结果 | 主观原因 | 客观原因 |")
        lines.append("|---|---|---|---|---|---|---|---|---|")
    else:
        lines.append("| CGI | 参数中文名称 | 参数英文 | 节能技术 | 节能类型 | 现网配置值 | 指导意见值 | 核查结果 | 主观原因 | 客观原因 |")
        lines.append("|---|---|---|---|---|---|---|---|---|---|")

    for item in detail_items:
        c_name = item.get("saving_para_name", "-")
        e_name = item.get("chn_field_name", "-")
        obj_type = item.get("objtype_name", "-")
        pwr_type = item.get("powertype_name", "-")
        cur_val = item.get("saving_para_value", "-")
        rec_val = item.get("recommend_value", "-")
        state = item.get("saving_switch_state", "-")
        sub_rsn = str(item.get("subjective_reason") or "-").replace("\n", " ")
        obj_rsn = str(item.get("objective_reason") or "-").replace("\n", " ")

        if is_single_cgi:
            lines.append(f"| {c_name} | {e_name} | {obj_type} | {pwr_type} | {cur_val} | {rec_val} | {state} | {sub_rsn} | {obj_rsn} |")
        else:
            item_cgi = item.get("cgi", "-")
            lines.append(f"| {item_cgi} | {c_name} | {e_name} | {obj_type} | {pwr_type} | {cur_val} | {rec_val} | {state} | {sub_rsn} | {obj_rsn} |")


def _generate_report(
    query_label: str,
    data: list[dict[str, Any]],
    is_single_cgi: bool,
    show_detail: bool,
    unqualified_items: list[dict[str, Any]],
    qualified_count: int,
    excluded_count: int,
    unqualified_cgi_count: int,
) -> str:
    """生成节能参数核查 Markdown 报告。

    展示逻辑：
    - 合规：仅提示合规数量，不展示详情表格
    - 不合规且数据量小(show_detail=True)：用表格展示不合规参数详情
    - 不合规且数据量大(show_detail=False)：仅显示统计摘要，提示下载Excel查看详情
    """
    lines: list[str] = []
    total_count = len(data)
    unqualified_count = len(unqualified_items)

    # ── 标题 / 基础信息 ──
    if is_single_cgi:
        base_info = data[0] if data else {}
        cgi = base_info.get("cgi", "-")
        enodeb = base_info.get("enodeb_name", "-")
        cell = base_info.get("cell_name", "-")
        vendor = base_info.get("prod_name", "-")
        station_type = base_info.get("station_type", "-")
        freq = base_info.get("freq_band", "-")
        lines.append(f"### 小区({cgi})节能参数配置总体查询情况")
        lines.append("")
        lines.append(f"**基础信息**：基站 `{enodeb}` | 小区 `{cell}` | 厂家 `{vendor}` | 类型 `{station_type}` | 频段 `{freq}`")
        lines.append("")
    else:
        lines.append(f"### 4/5G节能参数核查汇总报告（{query_label}）")
        lines.append("")

    # ── 核查结论与统计 ──
    _build_summary_lines(
        lines, total_count, qualified_count, unqualified_count,
        unqualified_cgi_count, excluded_count,
        is_single_cgi,
        unqualified_items if (unqualified_items and show_detail) else None,
    )

    return "\n".join(lines)


@tool_registry.tool(
    description="""查询小区节能参数核查结果，分析不合规配置。
""",
    parameters={
        "type": "object",
        "properties": {
            "check_date": {
                "type": "string",
                "description": "核查日期 (YYYY-MM-DD)，不传则自动查最新",
            },
            "cgi": {
                "type": "string",
                "description": "CGI，优先级最高",
            },
            "cell_name": {
                "type": "string",
                "description": "小区名称",
            },
            "dist_name": {"type": "string", "description": "地市名称"},
            "prod_name": {"type": "string", "description": "设备厂家"},
            "export_excel": {
                "type": "boolean",
                "description": "是否导出 Excel",
                "default": False,
            },
        },
        "required": [],
    },
)
async def query_energy_param_check(
    db: AsyncSession,
    check_date: str | None = None,
    cgi: str | None = None,
    cell_name: str | None = None,
    dist_name: str | None = None,
    prod_name: str | None = None,
    export_excel: bool = False,
) -> dict[str, Any]:
    """查询小区节能参数核查结果。"""
    date_str, date_note = await _get_check_date(db, check_date)

    # 构建查询标签（用于日志和批量报告标题）
    query_parts = []
    if cgi:
        query_parts.append(f"cgi={cgi}")
    elif cell_name:
        query_parts.append(f"cell_name={cell_name}")
    else:
        if dist_name:
            query_parts.append(f"dist={dist_name}")
        if prod_name:
            query_parts.append(f"prod={prod_name}")
    query_label = ", ".join(query_parts) if query_parts else "全量查询"

    logger.info("节能参数核查: date=%s, query=%s, table=%s.eng_check_result", date_str, query_label, DB_SCHEMA_RULE)

    try:
        data = await _fetch_param_check(db, date_str, cgi, cell_name, dist_name, prod_name)

        if not data:
            return {
                "success": True,
                "report_content": f"未查询到 {query_label} 在 {date_str} 的参数核查数据。",
                **({"date_note": date_note} if date_note else {}),
            }

        unqualified_items = [row for row in data if row.get("saving_switch_state") not in _STATES_NOT_UNQUALIFIED]
        excluded_count = sum(1 for row in data if row.get("saving_switch_state") in _EXCLUDED_STATES)
        qualified_count = len(data) - len(unqualified_items) - excluded_count
        unique_unqualified_cgis = len({item["cgi"] for item in unqualified_items if item.get("cgi")})
        is_single_cgi = bool(cgi)

        # 数据截断逻辑：超过50条时只返回前50条，报告不显示明细表格
        is_truncated = len(unqualified_items) > MAX_RETURN_ITEMS
        if is_truncated:
            logger.info("数据量超过%d条，报告将只显示摘要", MAX_RETURN_ITEMS)

        # 生成报告，数据量大时不显示明细表格
        report_md = _generate_report(
            query_label, data, is_single_cgi,
            show_detail=not is_truncated,
            unqualified_items=unqualified_items,
            qualified_count=qualified_count,
            excluded_count=excluded_count,
            unqualified_cgi_count=unique_unqualified_cgis,
        )
        logger.info(
            "节能参数核查完成: date=%s, query=%s, total=%d, unqualified=%d, excluded=%d",
            date_str, query_label, len(data), len(unqualified_items), excluded_count
        )

        returned_items = unqualified_items[:MAX_RETURN_ITEMS] if is_truncated else unqualified_items

        result = {
            "success": True,
            "check_date": date_str,
            "check_date_display": datetime.strptime(date_str, "%Y%m%d").strftime("%Y-%m-%d"),
            "is_compliant": len(unqualified_items) == 0,
            "total_count": len(data),
            "unqualified_count": len(unqualified_items),
            "qualified_count": qualified_count,
            "excluded_count": excluded_count,
            "report_content": report_md,
            "unqualified_items": returned_items,
            "returned_count": len(returned_items),
            "is_truncated": is_truncated,
            "unqualified_cgi_count": unique_unqualified_cgis,
            **({"date_note": date_note} if date_note else {}),
        }

        # Excel 导出逻辑
        # 1. 显式要求导出 或 2. 不合规结果超过50条自动导出 或 3. 数据被截断时强制导出
        should_export = export_excel or (len(unqualified_items) > 50) or is_truncated
        if should_export and unqualified_items:
            download_url = export_to_excel(unqualified_items, prefix="energy_param_check")
            if download_url:
                result["download_url"] = download_url
                result["auto_exported"] = not export_excel  # 标记是否为自动导出

        return result

    except Exception as exc:
        logger.error("节能参数核查异常: %s", exc, exc_info=True)
        return {
            "success": False,
            "error": f"核查失败: {exc}",
        }
