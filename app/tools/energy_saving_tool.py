"""
节电分析工具模块。
"""

import json
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.logging import get_logger
from app.tools.registry import tool_registry

# 代理表所在 schema
DB_SCHEMA_RULE = get_settings().db_schema_agent

logger = get_logger("energy_saving_tool")


def _parse_stat_time(stat_time: str | datetime | None) -> datetime | None:
    """将用户输入的日期转换为 datetime 对象。

    支持格式:
    - "2026-03-30" -> datetime(2026, 3, 30, 0, 0, 0)
    - "2026-03-30 00:00:00" -> datetime(2026, 3, 30, 0, 0, 0)
    - datetime 对象 -> 原样返回
    """
    if not stat_time:
        return None
    if isinstance(stat_time, datetime):
        return stat_time
    # 字符串格式
    if " " in stat_time:
        return datetime.strptime(stat_time, "%Y-%m-%d %H:%M:%S")
    return datetime.strptime(stat_time, "%Y-%m-%d")


@tool_registry.tool(
    description="""分析单个5G小区的节电详情，包含休眠扩展、休眠收缩、参数核查和负荷状态。

参数说明:
- cgi: 小区全球标识 (格式 460-00-xxx-xxx)，必填
- analysis_target: 分析目标类型:
  * "all"          — 输出完整报告（扩展+收缩+备注）
  * "expansion"    — 仅输出节能扩展分析
  * "constriction" — 仅输出节能收缩分析
  * "load"         — 仅输出高负荷状态
- stat_time: 统计日期 (YYYY-MM-DD)，可选，默认查询最新日期

核心原则: 纯 Python 查库与 Markdown 渲染，零 LLM 中间推演。
""",
    parameters={
        "type": "object",
        "properties": {
            "cgi": {
                "type": "string",
                "description": "小区全球标识，格式 460-00-基站号-小区号",
            },
            "analysis_target": {
                "type": "string",
                "enum": ["all", "expansion", "constriction", "load"],
                "description": "分析目标: all=全量报告, expansion=休眠扩展, constriction=休眠收缩, load=负荷状态",
            },
            "stat_time": {
                "type": "string",
                "description": "统计日期 (YYYY-MM-DD)，可选，默认查询最新日期",
            },
        },
        "required": ["cgi", "analysis_target"],
    },
)
async def analyze_single_cell_energy(
    cgi: str,
    analysis_target: str,
    db: AsyncSession,
    stat_time: str | None = None,
) -> dict[str, Any]:
    """分析单个5G小区的节电详情。

    Args:
        cgi: 小区全球标识。
        analysis_target: 分析目标 (all/expansion/constriction/load)。
        db: 数据库会话。
        stat_time: 统计日期，可选，默认查询最新日期。

    Returns:
        包含 report_content (Markdown) 的结果字典。
    """
    logger.info("单小区节电分析: cgi=%s, target=%s, stat_time=%s", cgi, analysis_target, stat_time)

    # ── 高负荷占位符（待底层表增加高负荷标识后替换）──
    is_high_load = False  # TODO: 待底层表增加高负荷标识后替换

    # ── Step 1: 确定查询日期 ──
    if stat_time:
        # 用户指定日期，转换为 datetime 对象
        query_date = _parse_stat_time(stat_time)
    else:
        # 查询最新日期（数据库返回 datetime 对象）
        latest_date_sql = text(f"""
            SELECT MAX(stat_time) as max_date
            FROM {DB_SCHEMA_RULE}.jd_cell_expansion_day
        """)
        latest_result = await db.execute(latest_date_sql)
        latest_row = latest_result.mappings().first()
        query_date = latest_row["max_date"] if latest_row else None

    if not query_date:
        return {
            "success": False,
            "report_content": "暂无节电分析数据。",
        }

    # ── Step 2: 根据分析目标选择主表获取基础信息 ──
    need_expansion = analysis_target in ("all", "expansion")
    need_constriction = analysis_target in ("all", "constriction")

    # 按优先级选择主表：扩展分析优先 expansion 表，收缩分析优先 constriction 表
    if need_expansion and not need_constriction:
        # 仅扩展：从 expansion 表获取 hour_detail
        base_sql = text(f"""
            SELECT cell_name, hour_detail
            FROM {DB_SCHEMA_RULE}.jd_cell_expansion_day
            WHERE cgi = :cgi AND stat_time = :stat_time
            LIMIT 1
        """)
    elif need_constriction and not need_expansion:
        # 仅收缩：从 constriction 表获取 cell_name
        base_sql = text(f"""
            SELECT cell_name
            FROM {DB_SCHEMA_RULE}.jd_cell_constriction_day
            WHERE cgi = :cgi AND stat_time = :stat_time
            LIMIT 1
        """)
    else:
        # 全量分析或 load：UNION 两个表
        base_sql = text(f"""
            SELECT cell_name, hour_detail
            FROM {DB_SCHEMA_RULE}.jd_cell_expansion_day
            WHERE cgi = :cgi AND stat_time = :stat_time
            UNION ALL
            SELECT cell_name, NULL as hour_detail
            FROM {DB_SCHEMA_RULE}.jd_cell_constriction_day
            WHERE cgi = :cgi AND stat_time = :stat_time
            LIMIT 1
        """)

    base_result = await db.execute(base_sql, {"cgi": cgi, "stat_time": query_date})
    base_row = base_result.mappings().first()
    logger.info("查询 CGI: %s, stat_time: %s, 结果: %s, sql:%s", cgi, query_date, "有数据" if base_row else "无数据",base_sql)

    if base_row is None:
        return {
            "success": False,
            "report_content": "该小区在指定日期暂不需要任何节电扩展/收缩措施。",
        }

    cell_name: str = base_row["cell_name"] or cgi

    # ── Step 3: 扩展分析数据（仅 expansion/all）──
    expansion_table_rows: list[str] = []
    expansion_data: list[dict[str, Any]] = []
    param_report: str = ""
    if need_expansion:
        # 解析 hour_detail
        hour_detail_raw = base_row.get("hour_detail")
        hour_map: dict[int, float] = {}

        hour_detail_list: list[dict] | None = None
        if hour_detail_raw is not None:
            if isinstance(hour_detail_raw, list):
                hour_detail_list = hour_detail_raw
            elif isinstance(hour_detail_raw, str):
                try:
                    hour_detail_list = json.loads(hour_detail_raw)
                except json.JSONDecodeError as e:
                    logger.warning("解析 hour_detail 失败: %s", e)

        if hour_detail_list:
            for item in hour_detail_list:
                h = item.get("hour")
                pct = item.get("low_flow_pct", 0)
                if h is not None:
                    hour_map[h] = float(pct) if pct else 0.0

        # 构建原始数据和表格行
        for h in sorted(hour_map.keys()):
            low_flow_pct = hour_map[h]
            suggest = "可扩展" if low_flow_pct >= 100 else "不建议"
            expansion_table_rows.append(f"| {h:02d}:00 | {low_flow_pct:.1f}% | {suggest} |")
            expansion_data.append({
                "hour": h,
                "low_flow_pct": low_flow_pct,
                "suggestion": suggest,
            })

        # 参数核查（内联调用）
        try:
            from app.tools.energy_param_check_tool import query_energy_param_check  # noqa: PLC0415
            param_result = await query_energy_param_check(cgi=cgi, db=db)
            param_report = param_result.get("report_content", "（参数核查数据获取失败）")
        except Exception as exc:
            logger.warning("参数核查调用异常: %s", exc)
            param_report = "（参数核查暂时不可用）"

    # ── Step 4: 收缩分析数据（仅 constriction/all）──
    constriction_table_rows: list[str] = []
    constriction_data: list[dict[str, Any]] = []
    is_whitelist: bool = False
    whitelist_reason: str = "无"

    if need_constriction:
        # 一次查询获取收缩数据 + 白名单信息
        constriction_sql = text(f"""
            SELECT constriction_hour, reason, constriction_cgi, constriction_cgi_name,
                   is_whitelist
            FROM {DB_SCHEMA_RULE}.jd_cell_constriction_day
            WHERE cgi = :cgi AND stat_time = :stat_time
        """)
        constriction_result = await db.execute(constriction_sql, {"cgi": cgi, "stat_time": query_date})
        constriction_rows = constriction_result.mappings().all()

        # 从第一条获取白名单状态
        if constriction_rows:
            is_whitelist = bool(constriction_rows[0].get("is_whitelist", False))

        for row in constriction_rows:
            hour_str = str(row["constriction_hour"]) if row["constriction_hour"] is not None else "—"
            reason_str = str(row["reason"] or "—")
            related_cgi = str(row["constriction_cgi"] or "—")
            related_name = str(row["constriction_cgi_name"] or "—")
            constriction_table_rows.append(
                f"| {hour_str} | {reason_str} | {related_name}({related_cgi}) | 建议收缩 |"
            )
            constriction_data.append({
                "constriction_hour": row["constriction_hour"],
                "reason": reason_str,
                "related_cgi": related_cgi,
                "related_cell_name": related_name,
            })

        if not constriction_table_rows:
            constriction_table_rows.append("| — | 暂无收缩数据 | — | — |")

    # ── Step 5: 拼装 Markdown 报告 ──
    is_full_report = analysis_target == "all"

    # 显示用日期（只取日期部分）
    display_date = query_date.strftime("%Y-%m-%d") if query_date else "-"
    header = f"### 小区节能/扩展总结（{cell_name} | {cgi} | {display_date}）\n"
    sections: list[str] = [header]

    # 全量报告概览
    if is_full_report:
        high_load_label = "高负荷预警小区" if is_high_load else "正常小区"
        sections.append(
            f"**概览结论**：该小区当前高负荷状态为 `{high_load_label}`。"
            "请参考以下扩展与收缩详情。\n"
        )

    # 扩展部分
    if need_expansion:
        high_load_label = "高负荷预警小区" if is_high_load else "正常小区"
        section_num = "一" if is_full_report else "1"

        expansion_section = f"#### {section_num}、节能扩展\n"
        expansion_section += f"**{section_num}.1 容量与风险说明**：当前高负荷状态为 `{high_load_label}`。"
        if is_high_load:
            expansion_section += "（当前高负荷，建议先进行压降处理后再考虑扩展。）"
        expansion_section += "\n\n"

        if expansion_table_rows:
            expansion_section += (
                f"**{section_num}.2 夜间时段低业务零休眠占比表**：\n"
                "| 时间点(时) | 低业务零休眠占比 | 扩展建议 |\n"
                "|---|---|---|\n"
            )
            expansion_section += "\n".join(expansion_table_rows) + "\n\n"
        else:
            expansion_section += f"**{section_num}.2 夜间时段低业务零休眠占比表**：暂无数据。\n\n"

        expansion_section += f"**{section_num}.3 参数核查结论**：\n{param_report}\n"
        sections.append(expansion_section)

    # 收缩部分
    if need_constriction:
        section_num = "二" if is_full_report else "1"

        constriction_section = f"#### {section_num}、节能收缩\n"
        constriction_section += (
            f"**{section_num}.1 周边200米关联小区高负荷影响判断**：周边影响评估见下表。\n\n"
            f"**{section_num}.2 需收缩的节能时间点表**：\n"
            "| 原节能生效时间点 | 触发收缩原因 | 关联小区名称 | 收缩建议 |\n"
            "|---|---|---|---|\n"
        )
        constriction_section += "\n".join(constriction_table_rows) + "\n"
        sections.append(constriction_section)

    # 白名单备注（仅全量报告）
    if is_full_report:
        whitelist_str = "是" if is_whitelist else "否"
        sections.append(
            "#### 三、特殊情况备注\n"
            f"**3.1 节电白名单匹配情况**：是否白名单：{whitelist_str}。原因：{whitelist_reason}。\n"
        )

    # 仅负荷状态
    if analysis_target == "load":
        high_load_label = "高负荷预警小区" if is_high_load else "正常小区"
        sections.append(f"**高负荷状态**：`{high_load_label}`\n")

    report_md = "\n".join(sections)

    # 构建返回结果
    result: dict[str, Any] = {
        "success": True,
        "cgi": cgi,
        "cell_name": cell_name,
        "stat_time": display_date,  # 返回日期字符串
        "report_content": report_md,
    }

    # 按需添加扩展数据
    if need_expansion:
        result["expansion_data"] = expansion_data
        result["is_high_load"] = is_high_load

    # 按需添加收缩数据
    if need_constriction:
        result["constriction_data"] = constriction_data
        result["is_whitelist"] = is_whitelist

    return result