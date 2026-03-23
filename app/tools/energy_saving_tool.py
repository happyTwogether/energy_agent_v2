"""
节电分析工具模块（占位实现）。

当前功能暂未开放，数据表建设中。
"""

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
    description="""分析 4G/5G 网络节电潜力（该功能暂未开放，数据表建设中）。

参数说明:
- dist_name: 区县名称
- prod_name: 设备厂商
- date_start: 开始日期 (YYYY-MM-DD)
- date_end: 结束日期 (YYYY-MM-DD)
- network_type: 网络类型 (4G/5G/ALL)

注意: 当前功能暂未开放，调用将返回建设中提示。
""",
    parameters={
        "type": "object",
        "properties": {
            "dist_name": {
                "type": "string",
                "description": "区县名称",
            },
            "prod_name": {
                "type": "string",
                "description": "设备厂商",
            },
            "date_start": {
                "type": "string",
                "description": "开始日期 (YYYY-MM-DD)",
            },
            "date_end": {
                "type": "string",
                "description": "结束日期 (YYYY-MM-DD)",
            },
            "network_type": {
                "type": "string",
                "description": "网络类型 (4G/5G/ALL)",
            },
        },
        "required": ["dist_name", "prod_name", "date_start", "date_end", "network_type"],
    },
)
async def analyze_energy_saving(
    dist_name: str,
    prod_name: str,
    date_start: str,
    date_end: str,
    network_type: str,
    db: AsyncSession,
) -> dict[str, Any]:
    """分析 4G/5G 网络节电潜力（占位实现）。

    Args:
        dist_name: 区县名称。
        prod_name: 设备厂商。
        date_start: 开始日期。
        date_end: 结束日期。
        network_type: 网络类型 (4G/5G/ALL)。
        db: 数据库会话。

    Returns:
        建设中提示字典。
    """
    logger.info(
        "节电分析请求(占位): dist_name=%s, prod_name=%s, network_type=%s",
        dist_name,
        prod_name,
        network_type,
    )

    return {
        "success": False,
        "error": "该功能暂未开放，数据表建设中",
    }


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

    # ── Step 1: 查基础信息与白名单 ──
    base_sql = text(f"""
        SELECT cell_name, vendor, is_whitelist, reason
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
    is_whitelist: bool = bool(base_row["is_whitelist"])
    whitelist_reason: str = base_row["reason"] or "无"

    # 提前计算各 section 是否需要，避免不必要的 DB 查询
    need_expansion = analysis_target in ("all", "expansion")
    need_constriction = analysis_target in ("all", "constriction")

    # ── Step 2: 查休眠扩展小时数据（22:00 ~ 08:00，仅 expansion/all 需要）──
    expansion_table_rows: list[str] = []
    param_report: str = ""
    if need_expansion:
        hour_sql = text(f"""
            SELECT
                EXTRACT(HOUR FROM stat_time)::int AS hour_t,
                flow,
                ee_shallowsleeptimerru,
                ee_deepsleeptimerru,
                ee_supersleeptimerru
            FROM {DB_SCHEMA_RULE}.jd_cell_detail_hour_nr
            WHERE cgi = :cgi
              AND EXTRACT(HOUR FROM stat_time) IN (22,23,0,1,2,3,4,5,6,7,8)
            ORDER BY stat_time DESC
            LIMIT 11
        """)
        hour_result = await db.execute(hour_sql, {"cgi": cgi})
        hour_rows = hour_result.mappings().all()

        # 按 22,23,0..8 顺序构建时间点映射（取最新一行）
        NIGHT_HOURS = [22, 23, 0, 1, 2, 3, 4, 5, 6, 7, 8]
        hour_map: dict[int, Any] = {}
        for row in hour_rows:
            h = row["hour_t"]
            if h not in hour_map:
                hour_map[h] = row

        for h in NIGHT_HOURS:
            row = hour_map.get(h)
            if row:
                flow_val = float(row["flow"] or 0)
                shallow = float(row["ee_shallowsleeptimerru"] or 0)
                deep = float(row["ee_deepsleeptimerru"] or 0)
                sup = float(row["ee_supersleeptimerru"] or 0)
                sleep_sum = shallow + deep + sup

                is_low_biz = "是" if flow_val < 500 else "否"
                is_zero_sleep = "是" if sleep_sum == 0 else "否"
                suggest = "可扩展" if is_low_biz == "是" and is_zero_sleep == "是" else "不建议"
            else:
                sleep_sum = 0.0
                is_low_biz = "—"
                is_zero_sleep = "—"
                suggest = "—"

            expansion_table_rows.append(
                f"| {h:02d}:00 | {is_low_biz} | {sleep_sum:.0f} | {is_zero_sleep} | {suggest} |"
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
    high_load_str = str(is_high_load)

    sections: list[str] = [header]

    # 全量报告时添加概览
    if analysis_target == "all":
        sections.append(
            f"**概览结论**：该小区当前高负荷状态为 `{high_load_str}`。"
            "请参考以下扩展与收缩详情。\n"
        )

    # 一、节能扩展
    if analysis_target in ("all", "expansion"):
        expansion_section = (
            "#### 一、节能扩展\n"
            f"**3.1 容量与风险说明**：当前高负荷状态为 `{high_load_str}`。"
        )
        if is_high_load:
            expansion_section += "（当前高负荷，建议先进行压降处理后再考虑扩展。）"
        expansion_section += "\n\n"
        expansion_section += (
            "**3.2 低业务时段与0休眠匹配表**：\n"
            "| 时间点(时) | 是否低业务(<500M) | 休眠时长累加值(秒) | 是否0休眠 | 扩展建议 |\n"
            "|---|---|---|---|---|\n"
        )
        expansion_section += "\n".join(expansion_table_rows) + "\n\n"
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
        sections.append(f"**高负荷状态**：`{high_load_str}`\n")

    report_md = "\n".join(sections)

    return {
        "success": True,
        "cgi": cgi,
        "report_content": report_md,
    }
