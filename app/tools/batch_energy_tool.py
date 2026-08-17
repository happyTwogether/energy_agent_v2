import asyncio
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.logging import get_logger
from app.services.energy_analysis import is_pre_sleep_hour
from app.utils.export_util import export_sheets_to_excel
from app.utils.sql_helpers import (
    ensure_datetime,
    error_response,
    fetch_rows,
    parse_list_field,
)

logger = get_logger("batch_energy_tool")

_settings = get_settings()
DB_SCHEMA_AGENT = _settings.db_schema_agent
assert DB_SCHEMA_AGENT.replace("_", "").isalnum(), (
    f"Invalid DB schema name: {DB_SCHEMA_AGENT}"
)

# ── Excel 列名常量 ──
_COL_BASE = ["地市名称", "区县名称", "厂家", "基站名称", "小区名称", "CGI"]
_COL_EXPANSION = [
    "节能扩展：容量与风险说明",
    "节能扩展：低业务时段与0休眠匹配（可扩展时间点）",
    "节能扩展：参数核查结论",
]
_COL_CONSTRICTION = [
    "节能收缩：周边高负荷受影响时间节点",
    "节能收缩：休眠前1周内高负荷次数",
]
_COL_WHITELIST = "特殊情况备注：白名单匹配情况"

# ── analysis_target 枚举值 ──
TARGET_ALL = "all"
TARGET_EXPANSION = "expansion"
TARGET_CONSTRICTION = "constriction"

# ── 筛选参数通配值（应跳过 WHERE 过滤）──
_FILTER_ALL = frozenset({"all", "全部", "所有"})

# ── 高负荷 枚举值 ──
_MAP_HIGHLOAD: dict[str, str] = {
    "上高": "上行高负荷",
    "下高": "下行高负荷",
    "双高": "双向高负荷",
}
PRB_HIGH_LOAD_THRESHOLD = 50.0


TOOL_DESCRIPTION = "批量诊断5G小区节电情况。支持全省、地市、区县、厂家级别查询，可全量分析或仅分析扩展或收缩维度。"
TOOL_INPUT_SCHEMA = {
        "type": "object",
        "properties": {
            "dist_name": {"type": "string", "description": "地市名称"},
            "county_name": {"type": "string", "description": "区县名称"},
            "prod_name": {"type": "string", "description": "厂家名称"},
            "stat_time": {"type": "string", "description": "统计日期 (YYYY-MM-DD)"},
            "analysis_target": {
                "type": "string",
                "enum": [TARGET_ALL, TARGET_EXPANSION, TARGET_CONSTRICTION],
                "description": "分析目标：all=全量(扩展+收缩)，expansion=仅扩展，constriction=仅收缩",
            },
        },
        "required": [],
}


async def analyze_batch_cells_energy(
    db: AsyncSession,
    dist_name: str | None = None,
    county_name: str | None = None,
    prod_name: str | None = None,
    stat_time: str | None = None,
    analysis_target: str = TARGET_ALL,
) -> dict[str, Any]:
    """批量诊断5G小区节电情况。"""
    logger.info(
        "批量节电诊断: target=%s dist=%s county=%s prod=%s time=%s",
        analysis_target, dist_name, county_name, prod_name, stat_time,
    )
    try:
        return await _do_analyze(
            dist_name=dist_name,
            county_name=county_name,
            prod_name=prod_name,
            stat_time=stat_time,
            analysis_target=analysis_target,
        )
    except Exception as exc:
        logger.exception("批量节电诊断失败")
        return error_response("批量分析失败，请稍后重试")


# ═══════════════════════════════════════════════════════════════════
# 核心逻辑
# ═══════════════════════════════════════════════════════════════════


async def _do_analyze(
    dist_name: str | None,
    county_name: str | None,
    prod_name: str | None,
    stat_time: str | None,
    analysis_target: str,
) -> dict[str, Any]:
    """核心分析流程。"""

    # ── 1. 解析查询日期（始终先查 DB 最新日期，用户日期超出时自动兜底）──
    need_constriction = analysis_target in (TARGET_ALL, TARGET_CONSTRICTION)
    db_latest_date = await _resolve_latest_date(need_constriction)
    if not db_latest_date:
        return error_response("暂无节电分析数据。")

    date_note = ""
    if stat_time:
        fmt = "%Y-%m-%d %H:%M:%S" if " " in stat_time else "%Y-%m-%d"
        user_date = datetime.strptime(stat_time, fmt)
        if user_date.date() > (db_latest_date.date() if isinstance(db_latest_date, datetime) else db_latest_date):
            date_note = f"您查询的日期 {stat_time} 暂无数据，已自动查询最新数据日期 {db_latest_date.strftime('%Y-%m-%d')}"
            query_date = db_latest_date
        else:
            query_date = user_date
    else:
        query_date = db_latest_date
    display_date = query_date.strftime("%Y-%m-%d")

    # ── 2. 构建 WHERE 条件 ──
    where_clauses = ["stat_time = :stat_time"]
    bind_params: dict[str, Any] = {"stat_time": query_date}
    if dist_name and dist_name not in _FILTER_ALL:
        where_clauses.append("dist_name = :dist_name")
        bind_params["dist_name"] = dist_name
    if county_name and county_name not in _FILTER_ALL:
        where_clauses.append("county_name = :county_name")
        bind_params["county_name"] = county_name
    if prod_name and prod_name not in _FILTER_ALL:
        where_clauses.append("prod_name = :prod_name")
        bind_params["prod_name"] = prod_name
    where_sql = " AND ".join(where_clauses)

    # ── 3. 并行查询三张表 ──
    expansion_sql = text(f"""
        SELECT cgi, cell_name, gnb_name, dist_name, county_name, prod_name,
               is_highload, saving_switch_state, is_whitelist, reason,
               jd_type, starttime, endtime, hour_detail,
               hour_filter, hour_int, hour_filter_early, hour_int_early,
               deploy_hours, deploy_hours_continuous,
               deploy_hours_early, deploy_hours_continuous_early
        FROM {DB_SCHEMA_AGENT}.jd_cell_expansion_day
        WHERE {where_sql}
    """)

    query_tasks = [fetch_rows(expansion_sql, bind_params)]

    if need_constriction:
        constriction_sql = text(f"""
            SELECT cgi, stat_time, hours, reason, around_cgi,
                   around_cgi_cell_name, around_cgi_network_type, site_type,
                   self_prb_rate_ul_before, self_prb_rate_dl_before,
                   self_sleep_duration, prb_rate_ul, prb_rate_dl,
                   prb_rate_ul_before, prb_rate_dl_before, prb_increase,
                   before_sleep_hour, before_sleep_date,
                   is_whitelist, cell_name, dist_name, county_name, prod_name
            FROM {DB_SCHEMA_AGENT}.jd_cell_constriction_day
            WHERE {where_sql}
        """)
        query_tasks.append(fetch_rows(constriction_sql, bind_params))

    gathered = await asyncio.gather(*query_tasks)
    expansion_rows = gathered[0]

    # ── 4. 构建数据映射 ──
    all_cgis: set[str] = set()
    expansion_map: dict[str, dict] = {}
    for row in expansion_rows:
        cgi = row["cgi"]
        all_cgis.add(cgi)
        expansion_map[cgi] = dict(row)

    constriction_map: dict[str, list[int]] = {}
    sleep_count_map: dict[str, int] = {}
    constriction_rows: list[dict[str, Any]] = []
    pre_sleep_rows: list[dict[str, Any]] = []
    if need_constriction:
        constriction_rows = gathered[1]
        for row in constriction_rows:
            cgi = row["cgi"]
            all_cgis.add(cgi)
            if cgi not in constriction_map:
                constriction_map[cgi] = []
            if row["hours"] is not None:
                constriction_map[cgi].append(row["hours"])
        pre_sleep_rows = await _query_pre_sleep_rows(all_cgis, query_date)
        sleep_count_map = _count_pre_sleep_days_by_cgi(pre_sleep_rows)

    # ── 5. 合并生成表格行 ──
    query_desc = _build_query_desc(dist_name, county_name, prod_name)
    if not all_cgis:
        return error_response(f"未查询到符合条件的数据（{query_desc}）。")

    table_data, stats = _build_table_data(
        all_cgis=all_cgis,
        expansion_map=expansion_map,
        constriction_map=constriction_map,
        sleep_count_map=sleep_count_map,
        dist_name=dist_name,
        county_name=county_name,
        prod_name=prod_name,
        analysis_target=analysis_target,
    )

    # ── 6. 统计问题清单，但完整结果不因问题过滤而丢失 ──
    stats.update(_compute_batch_stats(table_data))
    stats["param_noncompliant"] = _count_noncompliant_cells(expansion_rows)
    filtered_data = _filter_empty_cells(table_data, analysis_target)
    stats["problem_total"] = len(filtered_data)
    download_url = export_sheets_to_excel(
        {
            "小区汇总": table_data,
            "扩展明细": expansion_rows,
            "收缩明细": constriction_rows,
            "休眠前高负荷": pre_sleep_rows,
        },
        prefix="batch_analysis",
    )

    # ── 7. 生成 report_content ──
    report_content = _generate_batch_report_markdown(
        stats=stats,
        dist_name=dist_name,
        county_name=county_name,
        prod_name=prod_name,
        analysis_target=analysis_target,
        download_url=download_url,
    )

    return {
        "success": True,
        "report_content": report_content,
        "stats": stats,
        "download_url": download_url,
        **({"date_note": date_note} if date_note else {}),
    }


# ═══════════════════════════════════════════════════════════════════
# 查询辅助
# ═══════════════════════════════════════════════════════════════════


async def _resolve_latest_date(need_constriction: bool) -> datetime | None:
    """自动获取最新数据日期（使用独立 session）。

    涉及收缩表时取 expansion 和 constriction 两张表最大日期的较小值，
    确保两表都有数据；否则只查 expansion 表。
    """
    exp_sql = text(f"SELECT MAX(stat_time) as max_date FROM {DB_SCHEMA_AGENT}.jd_cell_expansion_day")
    rows = await fetch_rows(exp_sql, {})
    exp_max = ensure_datetime(rows[0]["max_date"]) if rows else None

    if not need_constriction or not exp_max:
        return exp_max

    const_sql = text(f"SELECT MAX(stat_time) as max_date FROM {DB_SCHEMA_AGENT}.jd_cell_constriction_day")
    rows = await fetch_rows(const_sql, {})
    const_max = ensure_datetime(rows[0]["max_date"]) if rows else None

    return min(exp_max, const_max) if const_max else exp_max


async def _query_pre_sleep_rows(
    cgis: set[str],
    query_date: datetime,
) -> list[dict[str, Any]]:
    """集合查询目标小区近七日高 PRB 行，并保留真实休眠前小时。"""
    if not cgis:
        return []
    end_date = query_date.date()
    start_date = end_date - timedelta(days=6)
    sql = text(f"""
        SELECT cgi, stat_time, prb_hour, sleep_hour,
               prb_rate_ul, prb_rate_dl
        FROM {DB_SCHEMA_AGENT}.jd_cell_pre_hour_busy
        WHERE cgi = ANY(:cgis)
          AND stat_time >= :start_date
          AND stat_time <= :end_date
          AND (prb_rate_ul > :prb_threshold
               OR prb_rate_dl > :prb_threshold)
    """)
    rows = await fetch_rows(sql, {
        "cgis": list(cgis),
        "start_date": start_date,
        "end_date": end_date,
        "prb_threshold": PRB_HIGH_LOAD_THRESHOLD,
    })
    return [
        row
        for row in rows
        if is_pre_sleep_hour(row.get("prb_hour"), row.get("sleep_hour"))
    ]


# ═══════════════════════════════════════════════════════════════════
# 数据构建
# ═══════════════════════════════════════════════════════════════════


def _build_query_desc(
    dist_name: str | None, county_name: str | None, prod_name: str | None
) -> str:
    """构建查询维度描述字符串。"""
    parts = []
    if dist_name:
        parts.append(f"地市={dist_name}")
    if county_name:
        parts.append(f"区县={county_name}")
    if prod_name:
        parts.append(f"厂家={prod_name}")
    return " | ".join(parts) if parts else "全省"


def _compute_batch_stats(table_data: list[dict[str, Any]]) -> dict[str, int]:
    """按完整汇总表计算批量总数，不受问题筛选影响。"""
    cgis = {
        str(row[_COL_BASE[5]])
        for row in table_data
        if row.get(_COL_BASE[5])
    }
    return {"total": len(cgis)}


def _count_noncompliant_cells(rows: list[dict[str, Any]]) -> int:
    """按 CGI 去重统计真实参数不合规，排除合格和数据缺失状态。"""
    ignored_states = {"", "合格", "北向数据缺失"}
    cgis = {
        str(row["cgi"])
        for row in rows
        if row.get("cgi")
        and str(row.get("saving_switch_state") or "").strip()
        not in ignored_states
    }
    return len(cgis)


def _is_database_true(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "是"}


def _count_pre_sleep_days_by_cgi(
    rows: list[dict[str, Any]],
) -> dict[str, int]:
    dates_by_cgi: dict[str, set[date]] = {}
    for row in rows:
        cgi = str(row.get("cgi") or "")
        stat_time = ensure_datetime(row.get("stat_time"))
        if not cgi or not stat_time:
            continue
        dates_by_cgi.setdefault(cgi, set()).add(stat_time.date())
    return {cgi: len(dates) for cgi, dates in dates_by_cgi.items()}


def _build_table_data(
    *,
    all_cgis: set[str],
    expansion_map: dict[str, dict],
    constriction_map: dict[str, list[int]],
    sleep_count_map: dict[str, int],
    dist_name: str | None,
    county_name: str | None,
    prod_name: str | None,
    analysis_target: str,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """合并三表数据，生成表格行和统计。"""
    table_data: list[dict[str, Any]] = []
    stats: dict[str, int] = {
        "total": 0,
        "high_load": 0,
        "can_expand": 0,
        "need_constriction": 0,
        "neighbor_pressure": 0,
        "whitelist": 0,
        "high_sleep_count": 0,
        "param_noncompliant": 0,
    }

    show_expansion = analysis_target in (TARGET_ALL, TARGET_EXPANSION)
    show_constriction = analysis_target in (TARGET_ALL, TARGET_CONSTRICTION)

    for cgi in all_cgis:
        exp_info = expansion_map.get(cgi, {})
        const_hours = constriction_map.get(cgi, [])

        # ── 基础信息 ──
        row: dict[str, Any] = {
            _COL_BASE[0]: exp_info.get("dist_name") or dist_name or "-",
            _COL_BASE[1]: exp_info.get("county_name") or county_name or "-",
            _COL_BASE[2]: exp_info.get("prod_name") or prod_name or "-",
            _COL_BASE[3]: exp_info.get("gnb_name") or "-",
            _COL_BASE[4]: exp_info.get("cell_name") or "-",
            _COL_BASE[5]: cgi,
        }

        # ── 扩展字段 ──
        if show_expansion:
            is_highload_raw = exp_info.get("is_highload")
            if is_highload_raw == "否":
                capacity_risk = "非高负荷"
            else:
                stats["high_load"] += 1
                capacity_risk = _MAP_HIGHLOAD.get(is_highload_raw, "高负荷")

            expandable_hours = _parse_hour_filter(exp_info.get("hour_filter"))
            expandable_str = "、".join(map(str, expandable_hours)) if expandable_hours else "无"
            if expandable_hours:
                stats["can_expand"] += 1

            saving_switch_state = exp_info.get("saving_switch_state") or "无"

            row[_COL_EXPANSION[0]] = capacity_risk
            row[_COL_EXPANSION[1]] = expandable_str
            row[_COL_EXPANSION[2]] = saving_switch_state

        # ── 收缩字段 ──
        constriction_str = "、".join(map(str, const_hours)) if const_hours else "无"

        sleep_count = sleep_count_map.get(cgi, 0)

        if const_hours or sleep_count > 0:
            stats["need_constriction"] += 1
        if const_hours:
            stats["neighbor_pressure"] += 1
        if sleep_count > 0:
            stats["high_sleep_count"] += 1

        if show_constriction:
            row[_COL_CONSTRICTION[0]] = constriction_str
            row[_COL_CONSTRICTION[1]] = sleep_count

        # ── 白名单（所有模式保留）──
        is_whitelist_raw = exp_info.get("is_whitelist")
        if _is_database_true(is_whitelist_raw):
            stats["whitelist"] += 1
            reason = exp_info.get("reason") or "无"
            jd_type = exp_info.get("jd_type") or "无"
            st = ensure_datetime(exp_info.get("starttime"))
            et = ensure_datetime(exp_info.get("endtime"))
            st_str = st.strftime("%Y-%m-%d") if st else "无"
            et_str = et.strftime("%Y-%m-%d") if et else "无"
            row[_COL_WHITELIST] = (
                f"是白名单小区、白名单原因：{reason}，节电类型：{jd_type}，"
                f"开始时间：{st_str}，结束时间：{et_str}"
            )
        else:
            row[_COL_WHITELIST] = "无"

        table_data.append(row)

    return table_data, stats

# ═══════════════════════════════════════════════════════════════════
# 过滤
# ═══════════════════════════════════════════════════════════════════


def _filter_empty_cells(
    table_data: list[dict[str, Any]], analysis_target: str
) -> list[dict[str, Any]]:
    """过滤无任何问题的小区（非高负荷 + 无扩展 + 无收缩 + sleep=0 + 非白名单）。"""
    filtered: list[dict[str, Any]] = []
    show_expansion = analysis_target in (TARGET_ALL, TARGET_EXPANSION)
    show_constriction = analysis_target in (TARGET_ALL, TARGET_CONSTRICTION)

    for row in table_data:
        if row.get(_COL_WHITELIST, "无") != "无":
            filtered.append(row)
            continue

        has_expansion = show_expansion and (
            row.get(_COL_EXPANSION[0], "非高负荷") != "非高负荷"
            or row.get(_COL_EXPANSION[1], "无") != "无"
        )
        has_constriction = show_constriction and (
            row.get(_COL_CONSTRICTION[0], "无") != "无"
            or row.get(_COL_CONSTRICTION[1], 0) > 0
        )
        if has_expansion or has_constriction:
            filtered.append(row)

    return filtered


# ═══════════════════════════════════════════════════════════════════
# 报告生成
# ═══════════════════════════════════════════════════════════════════


def _generate_batch_report_markdown(
    *,
    stats: dict[str, int],
    dist_name: str | None,
    county_name: str | None,
    prod_name: str | None,
    analysis_target: str,
    download_url: str | None,
) -> str:
    """生成批量分析 Markdown 报告。"""
    title = _build_query_desc(dist_name, county_name, prod_name)

    if analysis_target == TARGET_EXPANSION:
        mode_label = "扩展总结"
    elif analysis_target == TARGET_CONSTRICTION:
        mode_label = "收缩总结"
    else:
        mode_label = "扩展与收缩总结"

    lines = [f"### 批量分析小区节能{mode_label}（{title}）", ""]
    lines.append("**概览结论**：")
    lines.append(f"- 总计涉及小区数量 **{stats.get('total', 0)}** 个。")

    if analysis_target in (TARGET_ALL, TARGET_EXPANSION):
        lines.append(f"- 其中 **{stats.get('high_load', 0)}** 个需要进行高负荷压降。")
        lines.append(f"- **{stats.get('can_expand', 0)}** 个可进行休眠时间扩展。")
        lines.append(
            f"- **{stats.get('param_noncompliant', 0)}** 个存在节能参数不合规。"
        )

    if analysis_target in (TARGET_ALL, TARGET_CONSTRICTION):
        lines.append(f"- **{stats.get('need_constriction', 0)}** 个需要进行节能收缩。")
        np_count = stats.get("neighbor_pressure", 0)
        if np_count > 0:
            lines.append(f"- **{np_count}** 个休眠对周边邻近小区造成高负荷压力，需收缩节能时间段。")
        hs = stats.get("high_sleep_count", 0)
        if hs > 0:
            lines.append(f"- **{hs}** 个休眠前1周内存在高负荷记录。")

    lines.append(f"- **{stats.get('whitelist', 0)}** 个存在特殊情况白名单，需注意修改风险。")

    if download_url:
        lines.append("")
        lines.append(f"📥 [点击下载完整批量分析 Excel 报告]({download_url})")

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════════════════════════


def _parse_hour_filter(raw: Any) -> list[int]:
    """解析 hour_filter 字段（兼容 JSON 数组和逗号分隔字符串）。"""
    parsed = parse_list_field(raw)
    result: list[int] = []
    for x in parsed:
        if isinstance(x, (int, float)):
            result.append(int(x))
        elif isinstance(x, str) and x.lstrip("-").isdigit():
            result.append(int(x))
    return result
