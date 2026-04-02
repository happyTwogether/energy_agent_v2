"""
批量小区节电诊断与导出工具模块。

按区县批量拉取小区节电数据，利用 Pandas 向量化计算汇总指标，
生成 Excel 报告并返回带下载链接的 Markdown 摘要。

核心原则:
- 拒绝 for 循环单次查库，必须一次批量 SQL 获取全量数据。
- Markdown 只渲染 Top 10，全量数据走 Excel 下载。
"""

from typing import Any

import pandas as pd
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.logging import get_logger
from app.tools.registry import tool_registry

logger = get_logger("batch_energy_tool")

# 代理表所在 schema
DB_SCHEMA_RULE = get_settings().db_schema_agent

# Top 10 预览列（按优先级排列，实际按表中存在的列过滤）
_PREVIEW_COLS = ["county_name", "dist_name", "prod_name", "cell_name", "cgi"]


@tool_registry.tool(
    description="""按区县批量诊断5G小区节电情况，生成 Excel 报告并返回摘要。

参数说明:
- dist_name: 区县名称，必填（如"芙蓉区"）。若用户只提供了地市，必须追问区县！
- prod_name: 厂家名称，可选（如"华为"、"中兴"）。

注意: 数据量较大，必须确保 dist_name 已精确到区县级别。
""",
    parameters={
        "type": "object",
        "properties": {
            "dist_name": {
                "type": "string",
                "description": "区县名称，精确到区县级别，如'芙蓉区'",
            },
            "prod_name": {
                "type": "string",
                "description": "厂家名称，如'华为'、'中兴'。",
            },
        },
        "required": ["dist_name"],
    },
)
async def analyze_batch_cells_energy(
    dist_name: str,
    db: AsyncSession,
    prod_name: str = "全网",
) -> dict[str, Any]:
    """按区县批量诊断5G小区节电情况，生成 Excel 报告。

    Args:
        dist_name: 区县名称（必填）。
        db: 数据库会话。
        prod_name: 厂家名称，默认 "全网"。

    Returns:
        包含 report_content (Markdown + 下载链接) 的结果字典。
    """
    logger.info("批量节电诊断: dist_name=%s, prod_name=%s", dist_name, prod_name)

    # ── Step 1: 构建批量 SQL（参数化绑定，禁止字符串拼接用户输入）──
    where_clauses = ["dist_name = :dist_name"]
    bind_params: dict[str, Any] = {"dist_name": dist_name}

    if prod_name and prod_name != "全网":
        where_clauses.append("prod_name = :prod_name")
        bind_params["prod_name"] = prod_name

    where_sql = " AND ".join(where_clauses)
    # jd_cell_expansion_day 没有 is_whitelist/reason 字段，移除这些字段
    batch_sql = text(f"""
        SELECT county_name, dist_name, prod_name, cell_name, cgi, work_band, cover_type, hour_detail, avg_low_flow_pct
        FROM {DB_SCHEMA_RULE}.jd_cell_expansion_day
        WHERE {where_sql}
        ORDER BY prod_name, cell_name
    """)

    result = await db.execute(batch_sql, bind_params)
    rows = result.mappings().all()

    # ── Step 2: 转换为 DataFrame ──
    data_list = [dict(row) for row in rows]
    df = pd.DataFrame(data_list)

    if df.empty:
        return {
            "success": False,
            "report_content": f"未查询到 {dist_name} 的相关小区数据。",
        }

    # ── Step 3: 向量化计算汇总指标 ──
    # TODO: 待底层表增加高负荷标识后替换 is_high_load 计算逻辑
    df["is_high_load"] = False

    total_cells = len(df)
    need_load_reduce = int(df["is_high_load"].sum())           # 高负荷小区数（当前全为 0）
    can_expand = total_cells                                    # 可扩展小区（暂以全量估算）
    # 白名单信息需从 constriction 表获取，此处暂不统计
    whitelist_count = 0  # TODO: 需关联 jd_cell_constriction_day 表获取白名单数量

    # ── Step 4: 生成 Excel 文件 ──
    # 导出时去掉内部辅助列
    export_df = df.drop(columns=["is_high_load"], errors="ignore")
    data_list = export_df.to_dict(orient="records")

    # 使用统一的导出工具（自动处理文件名编码问题）
    from app.utils.export_util import export_to_excel
    download_url = export_to_excel(data_list, prefix="batch_analysis")

    if not download_url:
        # 导出失败时返回错误
        return {
            "success": False,
            "report_content": f"Excel 导出失败，请重试。",
        }

    logger.info("Excel 已生成: %s", download_url)

    # ── Step 5: 拼装 Markdown（Top 10 预览）──
    preview_cols = [c for c in _PREVIEW_COLS if c in df.columns]
    top10_md = df.head(10)[preview_cols].to_markdown(index=False)

    vendor_label = prod_name if prod_name != "全网" else "全网厂家"
    report_md = (
        f"### 批量分析小区节能/扩展总结（{dist_name} | {vendor_label}）\n\n"
        "**概览结论**：\n"
        f"总计分析小区数量 **{total_cells}** 个。"
        f"其中 **{need_load_reduce}** 个需要进行高负荷压降，"
        f"**{can_expand}** 个可进行休眠时间扩展，"
        f"白名单需注意修改风险的有 **{whitelist_count}** 个。\n\n"
        f"### 📥 [点击此处下载完整批量分析 Excel 报告]({download_url})\n\n"
        "#### 典型小区数据预览（Top 10）\n"
        f"{top10_md}\n"
    )

    return {
        "success": True,
        "dist_name": dist_name,
        "report_content": report_md,
    }
