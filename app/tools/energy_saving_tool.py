"""
节电分析工具模块。
"""

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.logging import get_logger
from app.tools.registry import tool_registry

# 代理表所在 schema（延迟绑定，避免循环导入）
DB_SCHEMA_RULE = get_settings().db_schema_agent

logger = get_logger("energy_saving_tool")


@tool_registry.tool(
    description="""分析单个5G小区的节电详情，包含休眠扩展、休眠收缩、参数核查和负荷状态。

参数说明:
- cgi: 小区全球标识 (格式 460-00-xxx-xxx)，必填
- analysis_target: 分析目标类型:
  * "all"          — 输出完整报告（扩展+收缩+备注）
  * "expansion"    — 仅输出节能扩展分析
  * "constriction" — 仅输出节能收缩分析
  * "load"         — 仅输出高负荷状态

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
        },
        "required": ["cgi", "analysis_target"],
    },
)
async def analyze_single_cell_energy(
    cgi: str,
    analysis_target: str,
    db: AsyncSession,
) -> dict[str, Any]:
    """分析单个5G小区的节电详情。

    Args:
        cgi: 小区全球标识。
        analysis_target: 分析目标 (all/expansion/constriction/load)。
        db: 数据库会话。

    Returns:
        包含 report_content (Markdown) 的结果字典。
    """
    logger.info("单小区节电分析: cgi=%s, target=%s", cgi, analysis_target)

    # ── 高负荷占位符（待底层表增加高负荷标识后替换）──
    is_high_load = False  # TODO: 待底层表增加高负荷标识后替换

    # ── Step 1: 查基础信息（从 jd_cell_expansion_day）──
    base_sql = text(f"""
        SELECT cell_name, prod_name, county_name, work_band, cover_type, cover_scen, hour_detail, avg_low_flow_pct
        FROM {DB_SCHEMA_RULE}.jd_cell_expansion_day
        WHERE cgi = :cgi
        LIMIT 1
    """)
    base_result = await db.execute(base_sql, {"cgi": cgi})
    base_row = base_result.mappings().first()

    if base_row is None:
        return {
            "success": False,
            "report_content": "请输入正确的5G小区CGI或5G小区名。",
        }

    cell_name: str = base_row["cell_name"] or cgi
    prod_name: str = base_row["prod_name"] or "-"
    county_name: str = base_row["county_name"] or "-"
    work_band: str = base_row["work_band"] or "-"
    cover_type: str = base_row["cover_type"] or "-"

    # 提前计算各 section 是否需要，避免不必要的 DB 查询
    need_expansion = analysis_target in ("all", "expansion")
    need_constriction = analysis_target in ("all", "constriction")

    # 白名单信息从收缩表获取（仅在需要时查询）
    is_whitelist: bool = False
    whitelist_reason: str = "无"
    if need_constriction or analysis_target == "all":
        whitelist_sql = text(f"""
            SELECT is_whitelist, reason
            FROM {DB_SCHEMA_RULE}.jd_cell_constriction_day
            WHERE cgi = :cgi
            LIMIT 1
        """)
        whitelist_result = await db.execute(whitelist_sql, {"cgi": cgi})
        whitelist_row = whitelist_result.mappings().first()
        if whitelist_row:
            is_whitelist = bool(whitelist_row["is_whitelist"])
            whitelist_reason = whitelist_row["reason"] or "无"

    # ── Step 2: 解析 hour_detail 数据（仅 expansion/all 需要）──
    expansion_table_rows: list[str] = []
    param_report: str = ""
    if need_expansion:
        # 解析 hour_detail JSON 字段
        # 格式: [{"hour": 0, "low_flow_pct": 100.00}, ...]
        # low_flow_pct 表示该小时低业务且零休眠的百分比
        # 注意：数据库可能返回已解析的 list 对象，或 JSON 字符串
        hour_detail_raw = base_row.get("hour_detail")
        logger.info("hour_detail 原始值: type=%s, value=%s", type(hour_detail_raw), hour_detail_raw)
        hour_map: dict[int, float] = {}  # hour -> low_flow_pct

        hour_detail_list: list[dict] | None = None
        if hour_detail_raw is not None:
            if isinstance(hour_detail_raw, list):
                # 数据库已自动解析为 list
                hour_detail_list = hour_detail_raw
            elif isinstance(hour_detail_raw, str):
                # 需要手动解析 JSON 字符串
                try:
                    hour_detail_list = json.loads(hour_detail_raw)
                except json.JSONDecodeError as e:
                    logger.warning("解析 hour_detail JSON 失败: %s, 原始值: %s", e, hour_detail_raw)

        if hour_detail_list:
            logger.info("hour_detail 解析成功: %s", hour_detail_list)
            for item in hour_detail_list:
                h = item.get("hour")
                pct = item.get("low_flow_pct", 0)
                if h is not None:
                    hour_map[h] = float(pct) if pct else 0.0

        # 仅展示有数据的时间点（按时间顺序排列）
        if hour_map:
            for h in sorted(hour_map.keys()):
                low_flow_pct = hour_map[h]
                is_expandable = low_flow_pct >= 100
                suggest = "可扩展" if is_expandable else "不建议"
                expansion_table_rows.append(
                    f"| {h:02d}:00 | {low_flow_pct:.1f}% | {suggest} |"
                )

        # ── Step 4: 内联调用参数核查工具（延迟导入避免模块级循环依赖）──
        try:
            from app.tools.energy_param_check_tool import query_energy_param_check  # noqa: PLC0415
            param_result = await query_energy_param_check(cgi=cgi, db=db)
            param_report = param_result.get("report_content", "（参数核查数据获取失败）")
        except Exception as exc:
            logger.warning("参数核查内联调用异常: %s", exc)
            param_report = "（参数核查暂时不可用）"

    # ── Step 3: 查休眠收缩数据（仅 constriction/all 需要）──
    constriction_table_rows: list[str] = []
    if need_constriction:
        constriction_sql = text(f"""
            SELECT constriction_hour, reason, constriction_cgi, constriction_cgi_name
            FROM {DB_SCHEMA_RULE}.jd_cell_constriction_day
            WHERE cgi = :cgi
        """)
        constriction_result = await db.execute(constriction_sql, {"cgi": cgi})
        constriction_rows = constriction_result.mappings().all()

        for row in constriction_rows:
            hour_str = str(row["constriction_hour"]) if row["constriction_hour"] is not None else "—"
            reason_str = str(row["reason"] or "—")
            related_cgi = str(row["constriction_cgi"] or "—")
            related_name = str(row["constriction_cgi_name"] or "—")
            constriction_table_rows.append(
                f"| {hour_str} | {reason_str} | {related_name}({related_cgi}) | 建议收缩 |"
            )

        if not constriction_table_rows:
            constriction_table_rows.append("| — | 暂无收缩数据 | — | — |")

    # ── Step 5: 按 analysis_target 动态拼装 Markdown ──
    header = f"### 小区节能/扩展总结（{cell_name} | {cgi}）\n"

    sections: list[str] = [header]

    # 全量报告时添加概览
    if analysis_target == "all":
        sections.append(
            f"**概览结论**：该小区当前高负荷状态为 `{'高负荷预警小区' if is_high_load else '正常小区'}`。"
            "请参考以下扩展与收缩详情。\n"
        )

    # 一、节能扩展
    if analysis_target in ("all", "expansion"):
        expansion_section = (
            "#### 一、节能扩展\n"
            f"**3.1 容量与风险说明**：当前高负荷状态为 `{'高负荷预警小区' if is_high_load else '正常小区'}`。"
        )
        if is_high_load:
            expansion_section += "（当前高负荷，建议先进行压降处理后再考虑扩展。）"
        expansion_section += "\n\n"
        if expansion_table_rows:
            expansion_section += (
                "**3.2 夜间时段低业务零休眠占比表**：\n"
                "| 时间点(时) | 低业务零休眠占比 | 扩展建议 |\n"
                "|---|---|---|\n"
            )
            expansion_section += "\n".join(expansion_table_rows) + "\n\n"
        else:
            expansion_section += "**3.2 夜间时段低业务零休眠占比表**：暂无数据。\n\n"
        expansion_section += "**3.3 参数核查结论**：\n"
        expansion_section += param_report + "\n"
        sections.append(expansion_section)

    # 二、节能收缩
    if analysis_target in ("all", "constriction"):
        constriction_section = (
            "#### 二、节能收缩\n"
            "**4.1 周边200米关联小区高负荷影响判断**：周边影响评估见下表。\n\n"
            "**4.2 需收缩的节能时间点表**：\n"
            "| 原节能生效时间点 | 触发收缩原因 | 关联小区名称 | 收缩建议 |\n"
            "|---|---|---|---|\n"
        )
        constriction_section += "\n".join(constriction_table_rows) + "\n"
        sections.append(constriction_section)

    # 三、特殊情况备注（仅全量报告）
    if analysis_target == "all":
        whitelist_str = "是" if is_whitelist else "否"
        sections.append(
            "#### 三、特殊情况备注\n"
            f"**5.1 节电白名单匹配情况**：是否白名单：{whitelist_str}。原因：{whitelist_reason}。\n"
        )

    # 仅负荷状态
    if analysis_target == "load":
        sections.append(f"**高负荷状态**：`{'高负荷预警小区' if is_high_load else '正常小区'}`\n")

    report_md = "\n".join(sections)

    return {
        "success": True,
        "cgi": cgi,
        "report_content": report_md,
    }
