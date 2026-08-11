import asyncio
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.logging import get_logger
from app.services.cell_resolver import resolve_cell_identifier
from app.services.database import get_session_factory
from app.utils.cell_lookup import resolution_error_response
from app.utils.sql_helpers import ensure_datetime, error_response, fetch_rows, parse_list_field

DB_SCHEMA_AGENT = get_settings().db_schema_agent

logger = get_logger("energy_saving_tool")

# ── 辅助函数 ──


def _fill_base_info(result: dict[str, Any], source: dict[str, Any], cgi: str) -> None:
    """将基础信息填充到 result 中（仅在尚未填充时）。"""
    defaults = {"cell_name": cgi, "dist_name": "-", "county_name": "-", "prod_name": "-"}
    for key, default in defaults.items():
        if key not in result:
            result[key] = source.get(key) or default


def _build_md_table(headers: list[str], rows: list[str], empty_msg: str = "暂无数据") -> str:
    """构建 Markdown 表格字符串。"""
    if not rows:
        return f"| — | {empty_msg} |"
    header_line = "| " + " | ".join(headers) + " |"
    sep_line = "|" + "|".join(["---"] * len(headers)) + "|"
    return header_line + "\n" + sep_line + "\n" + "\n".join(rows)


# 周边小区距离阈值（单位：米）
AROUND_DISTANCE_THRESHOLD: float = 200.0
# 低业务占比阈值（%）
LOW_FLOW_PCT_THRESHOLD: float = 90.0
# 零休眠判断阈值（秒）
ZERO_SLEEP_THRESHOLD: int = 60
# 休眠均值统计窗口（天）
SLEEP_AVG_DAYS: int = 14
# pre_sleep_load 统计天数
PRE_SLEEP_LOAD_DAYS: int = 7
# PRB 利用率高负荷阈值（%）
PRB_HIGH_LOAD_THRESHOLD: float = 50.0
# 不合规项最大返回条数
MAX_UNQUALIFIED_ITEMS: int = 10


TOOL_DESCRIPTION = "通过 CGI 或小区中文名分析单个5G小区的节电详情，包含休眠扩展、休眠收缩、负荷状态和休眠生效前负荷偏高。"
TOOL_INPUT_SCHEMA = {
        "type": "object",
        "properties": {
            "cgi": {
                "type": "string",
                "description": "小区全球标识，格式 460-00-基站号-小区号；与 cell_name 同时传入时优先",
            },
            "cell_name": {
                "type": "string",
                "description": "小区中文名或其连续片段，未提供 CGI 时使用包含匹配",
            },
            "analysis_target": {
                "type": "string",
                "enum": ["all", "expansion", "constriction", "pre_sleep_load"],
                "description": "分析目标",
            },
            "stat_time": {
                "type": "string",
                "description": "统计日期 (YYYY-MM-DD)",
            },
        },
        "required": ["analysis_target"],
}


async def analyze_single_cell_energy(
    analysis_target: str,
    db: AsyncSession,
    cgi: str | None = None,
    cell_name: str | None = None,
    stat_time: str | None = None,
) -> dict[str, Any]:
    """分析单个5G小区的节电详情。

    返回结构化数据 + 基础表格，由 LLM 组装最终报告。
    """
    cgi = (cgi or "").strip()
    cell_name = (cell_name or "").strip()
    if not cgi and not cell_name:
        return error_response("请提供 CGI 或小区中文名。")

    logger.info(
        "单小区节电分析: cgi=%s, cell_name=%s, target=%s, stat_time=%s",
        cgi,
        cell_name,
        analysis_target,
        stat_time,
    )

    need_expansion = analysis_target in ("all", "expansion")
    need_constriction = analysis_target in ("all", "constriction")
    need_load = analysis_target in ("all", "load")
    need_pre_sleep_load = analysis_target == "pre_sleep_load"

    # ── Step 1: 确定查询日期（始终先查 DB 最新日期，用户日期超出时自动兜底）──
    if need_pre_sleep_load:
        latest_sql = text(f"""
            SELECT MAX(stat_time) as max_date
            FROM {DB_SCHEMA_AGENT}.jd_cell_pre_hour_busy
        """)
    else:
        latest_sql = text(f"""
            SELECT LEAST(
                (SELECT MAX(stat_time) FROM {DB_SCHEMA_AGENT}.jd_cell_expansion_day),
                (SELECT MAX(stat_time) FROM {DB_SCHEMA_AGENT}.jd_cell_constriction_day)
            ) as max_date
        """)

    latest_result = await db.execute(latest_sql)
    latest_row = latest_result.mappings().first()
    db_latest_date = ensure_datetime(latest_row["max_date"]) if latest_row else None

    if not db_latest_date:
        return error_response("暂无节电分析数据。")

    date_note = ""
    if stat_time:
        user_date = _parse_stat_time(stat_time)
        if user_date and user_date.date() > (db_latest_date.date() if isinstance(db_latest_date, datetime) else db_latest_date):
            date_note = f"您查询的日期 {stat_time} 暂无数据，已自动查询最新数据日期 {db_latest_date.strftime('%Y-%m-%d')}"
            query_date = db_latest_date
        else:
            query_date = user_date
    else:
        query_date = db_latest_date

    display_date = query_date.strftime("%Y-%m-%d")

    name_resolution = None
    if not cgi:
        try:
            name_resolution = await resolve_cell_identifier(
                db=db,
                cell_name=cell_name,
                network="5G",
            )
        except Exception as exc:
            logger.error("单小区名称解析异常: %s", exc, exc_info=True)
            return error_response("小区名称解析失败，请稍后重试。")
        if name_resolution.status != "resolved":
            return resolution_error_response(name_resolution)
        cgi = name_resolution.cgi or ""

    # 构建返回结果（基础信息从扩展/收缩结果中获取）
    result: dict[str, Any] = {
        "success": True,
        "cgi": cgi,
        "analysis_target": analysis_target,
        "stat_time": display_date,
    }
    if name_resolution:
        result["cell_name_match"] = {
            "query": name_resolution.query,
            "match_type": name_resolution.match_type,
        }
    if date_note:
        result["date_note"] = date_note

    # ── Step 2 & 3: 并行执行扩展分析、收缩分析、参数核查（各自独立 session）──
    tasks = []
    task_names = []

    if need_expansion:
        tasks.append(_query_expansion_data(cgi, query_date))
        task_names.append("expansion")

    if need_constriction:
        tasks.append(_query_constriction_data(cgi, query_date))
        task_names.append("constriction")

    # 参数核查也加入并行（扩展分析需要）
    if need_expansion:
        async def _query_param_check_with_session():
            async with get_session_factory()() as sess:
                return await _query_param_check(sess, cgi, display_date)
        tasks.append(_query_param_check_with_session())
        task_names.append("param_check")

    results = await asyncio.gather(*tasks) if tasks else []
    results_map = dict(zip(task_names, results))

    # 处理扩展分析结果
    if "expansion" in results_map:
        expansion_result = results_map["expansion"]
        # 【优化】不再返回原始 expansion_data，只返回表格
        result["expansion_table"] = expansion_result["table"]
        result["high_load_type"] = expansion_result.get("high_load_type", "否")
        # 参数核查结果（仅保留关键字段）
        param_check_raw = results_map.get("param_check", {"is_compliant": True})
        result["param_check"] = {
            "is_compliant": param_check_raw.get("is_compliant", True),
            "unqualified_count": param_check_raw.get("unqualified_count", 0),
        }
        # 如果不合规，保留不合规项（用于表格输出）
        if not param_check_raw.get("is_compliant", True):
            result["param_check"]["unqualified_items"] = param_check_raw.get("unqualified_items", [])[:MAX_UNQUALIFIED_ITEMS]
        # 节电信息 & 白名单信息（统一从扩展表获取）
        result["jd_type"] = expansion_result.get("jd_type")
        result["jd_reason"] = expansion_result.get("reason")
        result["jd_starttime"] = expansion_result.get("starttime")
        result["jd_endtime"] = expansion_result.get("endtime")
        result["is_whitelist"] = expansion_result.get("is_whitelist", False)
        result["whitelist_reason"] = expansion_result.get("reason") or "无"
        # 基础信息
        _fill_base_info(result, expansion_result, cgi)

    # 处理收缩分析结果
    if "constriction" in results_map:
        constriction_result = results_map["constriction"]
        # 【优化】不再返回原始 constriction_data，只返回表格
        result["constriction_table"] = constriction_result["table"]
        # 基础信息（如果扩展分析没查，从收缩结果获取）
        if "cell_name" not in result:
            _fill_base_info(result, constriction_result, cgi)
            # 仅收缩时，白名单信息从收缩结果获取
            result["is_whitelist"] = constriction_result.get("is_whitelist", False)
            result["whitelist_reason"] = constriction_result.get("whitelist_reason", "无")

    # ── Step 4: 仅负荷状态 ──
    if need_load and not need_expansion:
        load_info = await _check_high_load_with_base_info(db, cgi, query_date)
        result["high_load_type"] = load_info.get("high_load_type", "否")
        # 基础信息
        if "cell_name" not in result:
            _fill_base_info(result, load_info, cgi)

    # ── Step 5: 休眠生效前负荷偏高分析 ──
    if need_pre_sleep_load:
        pre_sleep_result = await _query_pre_sleep_load_data(cgi, query_date)
        result["pre_sleep_load_table"] = pre_sleep_result["table"]
        result["high_prb_days_7d"] = pre_sleep_result.get("high_prb_days_7d", 0)
        # 基础信息
        if "cell_name" not in result:
            _fill_base_info(result, pre_sleep_result, cgi)

    # 确保基础信息有默认值
    result.setdefault("cell_name", cgi)
    result.setdefault("dist_name", "-")
    result.setdefault("county_name", "-")
    result.setdefault("prod_name", "-")
    result.setdefault("is_whitelist", False)
    result.setdefault("whitelist_reason", "无")
    result.setdefault("high_load_type", "否")

    return result

async def _query_expansion_data(
    cgi: str,
    stat_time: datetime,
) -> dict[str, Any]:
    """查询扩展分析数据。

    数据来源：
    - jd_cell_expansion_day: 低业务时段预计算结果
    - jd_cell_detail_hour_nr: 休眠时长数据
    """
    # 并行查询：基础信息 + 休眠时长（各自独立 session）
    expansion_sql = text(f"""
        SELECT is_highload, jd_type, reason, starttime, endtime, is_whitelist,
               cell_name, dist_name, county_name, prod_name, hour_detail
        FROM {DB_SCHEMA_AGENT}.jd_cell_expansion_day
        WHERE cgi = :cgi AND stat_time = :stat_time
    """)

    night_hours = [0, 1, 2, 3, 4, 5, 6, 7, 8, 22, 23]
    start_date = stat_time - timedelta(days=SLEEP_AVG_DAYS)
    sleep_sql = text(f"""
        SELECT hours,
               AVG(ee_shallowsleeptimerru + ee_deepsleeptimerru + ee_supersleeptimerru) as avg_sleep_sum
        FROM {DB_SCHEMA_AGENT}.jd_cell_detail_hour_nr
        WHERE cgi = :cgi
          AND stat_time >= :start_date
          AND stat_time <= :end_date
          AND hours = ANY(:night_hours)
        GROUP BY hours
        ORDER BY hours
    """)

    base_rows, sleep_rows = await asyncio.gather(
        fetch_rows(expansion_sql, {"cgi": cgi, "stat_time": stat_time}),
        fetch_rows(sleep_sql, {
            "cgi": cgi, "start_date": start_date, "end_date": stat_time, "night_hours": night_hours
        }),
    )
    base_row = base_rows[0] if base_rows else None

    high_load_type = "否"
    jd_type = None
    reason = None
    starttime = None
    endtime = None
    is_whitelist = False
    cell_name = None
    dist_name = None
    county_name = None
    prod_name = None
    hour_detail = []

    if base_row:
        is_highload_raw = base_row.get("is_highload") or "否"
        high_load_type = is_highload_raw
        jd_type = base_row.get("jd_type")
        reason = base_row.get("reason")
        starttime = base_row.get("starttime")
        endtime = base_row.get("endtime")
        is_whitelist = bool(base_row.get("is_whitelist"))
        cell_name = base_row.get("cell_name")
        dist_name = base_row.get("dist_name")
        county_name = base_row.get("county_name")
        prod_name = base_row.get("prod_name")
        hour_detail = _parse_hour_detail(base_row.get("hour_detail"))

    # 构建休眠时长映射
    sleep_map: dict[int, float] = {row["hours"]: row["avg_sleep_sum"] or 0 for row in sleep_rows}

    # 从 hour_detail 生成表格数据，合并休眠时长
    expansion_data: list[dict[str, Any]] = []

    for item in hour_detail:
        hour = item.get("hour")
        low_flow_pct = item.get("low_flow_pct", 0)

        # 低业务判断
        is_low_business = low_flow_pct > LOW_FLOW_PCT_THRESHOLD

        # 休眠时长（秒）
        avg_sleep_sum = sleep_map.get(hour, 0)
        is_zero_sleep = avg_sleep_sum < ZERO_SLEEP_THRESHOLD

        # 扩展建议
        if is_low_business and is_zero_sleep:
            suggestion = "可扩展"
        elif not is_low_business:
            suggestion = "不建议（非低业务）"
        else:
            suggestion = "不建议（已有休眠）"

        expansion_data.append({
            "hour": hour,
            "low_flow_pct": low_flow_pct,
            "is_low_business": is_low_business,
            "avg_sleep_seconds": round(avg_sleep_sum, 0),
            "avg_sleep_display": _format_sleep_duration(int(avg_sleep_sum)),
            "is_zero_sleep": is_zero_sleep,
            "suggestion": suggestion,
        })

    # 生成表格
    table_rows = []
    for item in expansion_data:
        table_rows.append(
            f"| {item['hour']:02d}:00 | {'是' if item['is_low_business'] else '否'} | "
            f"{item['low_flow_pct']:.1f}% | {item['avg_sleep_display']} | "
            f"{'是' if item['is_zero_sleep'] else '否'} | {item['suggestion']} |"
        )

    table_md = _build_md_table(
        headers=["时间点(时)", "是否低业务", "低业务占比", f"休眠时长({SLEEP_AVG_DAYS}天均值)", "是否0休眠", "扩展建议"],
        rows=table_rows,
    )

    return {
        "data": expansion_data,
        "table": table_md,
        "high_load_type": high_load_type,
        "jd_type": jd_type,
        "reason": reason,
        "starttime": starttime.strftime("%Y-%m-%d") if starttime else None,
        "endtime": endtime.strftime("%Y-%m-%d") if endtime else None,
        "is_whitelist": is_whitelist,
        # 基础信息
        "cell_name": cell_name,
        "dist_name": dist_name,
        "county_name": county_name,
        "prod_name": prod_name,
    }


async def _query_constriction_data(
    cgi: str,
    stat_time: datetime,
) -> dict[str, Any]:
    """查询收缩分析数据。

    从 jd_cell_constriction_day 获取收缩信息，
    关联 jd_cell_around 获取周边高负荷证据。
    """
    # 并行查询：收缩数据 + 周边小区距离
    constriction_sql = text(f"""
        SELECT hours, reason, around_cgi, around_cgi_cell_name,
               is_whitelist, cell_name, dist_name, county_name, prod_name
        FROM {DB_SCHEMA_AGENT}.jd_cell_constriction_day
        WHERE cgi = :cgi AND stat_time = :stat_time
    """)
    around_sql = text(f"""
        SELECT around_cgi, distance
        FROM {DB_SCHEMA_AGENT}.jd_cell_around
        WHERE cgi = :cgi AND distance <= :distance_threshold
    """)

    constriction_rows, around_rows = await asyncio.gather(
        fetch_rows(constriction_sql, {"cgi": cgi, "stat_time": stat_time}),
        fetch_rows(around_sql, {"cgi": cgi, "distance_threshold": AROUND_DISTANCE_THRESHOLD}),
    )

    # 构建周边小区距离映射
    around_distance_map: dict[str, float] = {
        row["around_cgi"]: row["distance"]
        for row in around_rows
    }

    constriction_data: list[dict[str, Any]] = []
    is_whitelist = False
    whitelist_reason = "无"
    cell_name = None
    dist_name = None
    county_name = None
    prod_name = None

    if constriction_rows:
        is_whitelist = bool(constriction_rows[0].get("is_whitelist"))
        # 基础信息从第一条记录获取
        cell_name = constriction_rows[0].get("cell_name")
        dist_name = constriction_rows[0].get("dist_name")
        county_name = constriction_rows[0].get("county_name")
        prod_name = constriction_rows[0].get("prod_name")
        # 白名单原因：取第一条有 reason 值的记录
        for row in constriction_rows:
            if row.get("reason"):
                whitelist_reason = row["reason"]
                break

    for row in constriction_rows:
        hour = row["hours"]
        # 触发收缩原因：固定文案（hours 是收缩节点，说明该时间点周边小区存在负荷偏高状态）
        trigger_reason = "该时间点存在周边小区负荷偏高"
        related_cgi = row["around_cgi"] or "—"
        related_name = row["around_cgi_cell_name"] or "—"

        # 获取距离信息用于证据字段
        distance = around_distance_map.get(related_cgi, 0)
        evidence = f"该时段负荷偏高=是，距离{distance:.0f}米"

        # 收缩建议：既然 hours 是收缩节点，周边肯定高负荷，建议收缩节电
        suggestion = "收缩节电"

        constriction_data.append({
            "constriction_hour": hour,
            "reason": trigger_reason,
            "related_cgi": related_cgi,
            "related_cell_name": related_name,
            "evidence": evidence,
            "suggestion": suggestion,
        })

    if not constriction_data:
        table_md = ""
    else:
        table_rows = []
        for item in constriction_data:
            # 关联小区显示格式：小区名称(CGI)
            related_display = f"{item['related_cell_name']}({item['related_cgi']})" if item['related_cgi'] != "—" else "—"
            table_rows.append(
                f"| {item['constriction_hour'] or '—'} | {item['reason']} | "
                f"{related_display} | "
                f"{item['evidence']} | {item['suggestion']} |"
            )
        table_md = _build_md_table(
            headers=["原节能生效时间点", "触发收缩原因", "关联小区名称", "证据字段", "收缩建议"],
            rows=table_rows,
        )

    return {
        "data": constriction_data,
        "table": table_md,
        "is_whitelist": is_whitelist,
        "whitelist_reason": whitelist_reason,
        # 基础信息
        "cell_name": cell_name,
        "dist_name": dist_name,
        "county_name": county_name,
        "prod_name": prod_name,
    }


async def _check_high_load_with_base_info(db: AsyncSession, cgi: str, stat_time: datetime) -> dict[str, Any]:
    """检查小区是否高负荷，同时获取基础信息。"""
    sql = text(f"""
        SELECT is_highload, cell_name, dist_name, county_name, prod_name
        FROM {DB_SCHEMA_AGENT}.jd_cell_expansion_day
        WHERE cgi = :cgi AND stat_time = :stat_time
    """)
    result = await db.execute(sql, {"cgi": cgi, "stat_time": stat_time})
    row = result.mappings().first()

    if not row:
        return {"high_load_type": "否"}

    return {
        "high_load_type": row.get("is_highload") or "否",
        "cell_name": row.get("cell_name"),
        "dist_name": row.get("dist_name"),
        "county_name": row.get("county_name"),
        "prod_name": row.get("prod_name"),
    }


async def _query_param_check(db: AsyncSession, cgi: str, stat_time: str | None = None) -> dict[str, Any]:
    """调用参数核查工具获取参数合规情况。"""
    try:
        # 延迟导入避免循环引用
        from app.tools.energy_param_check_tool import query_energy_param_check  # noqa: PLC0415
        result = await query_energy_param_check(cgi=cgi, check_date=stat_time, db=db)
        return {
            "success": result.get("success", False),
            "is_compliant": result.get("is_compliant", True),
            "total_count": result.get("total_count", 0),
            "unqualified_count": result.get("unqualified_count", 0),
            "report_content": result.get("report_content", ""),
            "unqualified_items": result.get("unqualified_items", []),
        }
    except Exception as exc:
        logger.error("参数核查调用异常: %s", exc, exc_info=True)
        return {
            "success": False,
            "is_compliant": None,
            "error": "参数核查暂时不可用，请稍后重试",
        }


def _parse_stat_time(stat_time: str | datetime | None) -> datetime | None:
    """将用户输入的日期转换为 datetime 对象。"""
    if not stat_time:
        return None
    if isinstance(stat_time, datetime):
        return stat_time
    try:
        if " " in stat_time:
            return datetime.strptime(stat_time, "%Y-%m-%d %H:%M:%S")
        return datetime.strptime(stat_time, "%Y-%m-%d")
    except ValueError:
        return None


def _parse_hour_detail(hour_detail_raw: Any) -> list[dict]:
    """解析 hour_detail 字段。格式：[{"hour": 0, "low_flow_pct": 85.5}, ...]"""
    return parse_list_field(hour_detail_raw)


def _format_sleep_duration(seconds: int | None) -> str:
    """将休眠时长（秒）格式化为可读字符串。"""
    if seconds is None:
        return "数据缺失"
    if seconds == 0:
        return "0秒"
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    parts = []
    if hours > 0:
        parts.append(f"{hours}小时")
    if minutes > 0:
        parts.append(f"{minutes}分钟")
    if secs > 0:
        parts.append(f"{secs}秒")
    return "".join(parts)


def _check_is_pre_sleep_hour(prb_hour: int | None, sleep_hour: str | None) -> bool:
    """判断是否为休眠前一小时。

    Args:
        prb_hour: 上一小时时间点（整数，如 22 表示 22:00-23:00）
        sleep_hour: 休眠时间点（字符串，可能包含多个时间点，如 "22,23,0"）

    Returns:
        如果 prb_hour 是休眠时间点的前一小时，返回 True。

    判断逻辑：
        如果 (prb_hour + 1) 在 sleep_hour 列表中，则为休眠前一小时。
        例如：prb_hour=21，sleep_hour="22,23,0"，则 21+1=22 在列表中，返回 True。
    """
    if prb_hour is None or not sleep_hour:
        return False

    try:
        # 解析 sleep_hour（可能是逗号分隔的多个时间点）
        sleep_hours = [int(h.strip()) for h in sleep_hour.split(",") if h.strip()]
        # 休眠前一小时 = 休眠时间点 - 1（处理跨天情况）
        pre_sleep_hours = [(h - 1) % 24 for h in sleep_hours]
        return prb_hour in pre_sleep_hours
    except (ValueError, AttributeError):
        return False


async def _query_pre_sleep_load_data(
    cgi: str,
    stat_time: datetime,
) -> dict[str, Any]:
    """查询休眠生效前负荷偏高分析数据。

    数据来源：jd_cell_pre_hour_busy

    返回字段：
        - 时间、地市名称、厂家、基站名称、小区名称、CGI
        - 休眠类生效时长（秒）
        - 无线利用率（%）
        - 是否休眠前一小时
        - 7天内休眠前1小时PRB利用率大于50%的天数
    """
    # 计算时间范围（包含当天）
    start_date_7d = stat_time - timedelta(days=PRE_SLEEP_LOAD_DAYS - 1)

    # 并行查询：当日明细 + 7天统计（各自独立 session）
    detail_sql = text(f"""
        SELECT
            stat_time,
            dist_name,
            prod_name,
            gnb_name,
            cell_name,
            cgi,
            ee_shallowsleeptimerru,
            ee_deepsleeptimerru,
            ee_supersleeptimerru,
            prb_hour,
            prb_rate_ul,
            prb_rate_dl,
            sleep_hour
        FROM {DB_SCHEMA_AGENT}.jd_cell_pre_hour_busy
        WHERE cgi = :cgi
          AND stat_time = :stat_time
        ORDER BY stat_time, prb_hour
    """)

    stat_sql = text(f"""
        SELECT COUNT(DISTINCT DATE(stat_time)) AS high_prb_days
        FROM {DB_SCHEMA_AGENT}.jd_cell_pre_hour_busy
        WHERE cgi = :cgi
          AND stat_time >= :start_date
          AND stat_time <= :end_date
          AND (prb_rate_ul > :prb_threshold OR prb_rate_dl > :prb_threshold)
    """)

    detail_rows, stat_rows = await asyncio.gather(
        fetch_rows(detail_sql, {"cgi": cgi, "stat_time": stat_time}),
        fetch_rows(stat_sql, {
            "cgi": cgi,
            "start_date": start_date_7d,
            "end_date": stat_time,
            "prb_threshold": PRB_HIGH_LOAD_THRESHOLD,
        }),
    )
    high_prb_days = stat_rows[0]["high_prb_days"] if stat_rows else 0

    # 构建表格数据
    data: list[dict[str, Any]] = []
    for row in detail_rows:
        # 计算休眠类生效时长（浅层+深度+极致）
        total_sleep_seconds = (
            (row.get("ee_shallowsleeptimerru") or 0) +
            (row.get("ee_deepsleeptimerru") or 0) +
            (row.get("ee_supersleeptimerru") or 0)
        )

        # 计算无线利用率（取上下行较大值）
        prb_rate_ul = row.get("prb_rate_ul") or 0
        prb_rate_dl = row.get("prb_rate_dl") or 0
        prb_rate = max(prb_rate_ul, prb_rate_dl)

        # 判断是否休眠前一小时
        is_pre_sleep_hour = _check_is_pre_sleep_hour(
            row.get("prb_hour"),
            row.get("sleep_hour")
        )

        # 格式化时间：stat_time + prb_hour
        stat_time_val = ensure_datetime(row.get("stat_time"))
        prb_hour_val = row.get("prb_hour")
        if stat_time_val and prb_hour_val is not None:
            time_display = f"{stat_time_val.strftime('%Y-%m-%d')} {prb_hour_val:02d}:00:00"
        else:
            time_display = "-"

        data.append({
            "time": time_display,
            "dist_name": row.get("dist_name") or "-",
            "prod_name": row.get("prod_name") or "-",
            "gnb_name": row.get("gnb_name") or "-",
            "cell_name": row.get("cell_name") or "-",
            "cgi": row.get("cgi") or cgi,
            "sleep_seconds": total_sleep_seconds,
            "sleep_display": _format_sleep_duration(total_sleep_seconds),
            "prb_rate": round(prb_rate, 2),
            "is_pre_sleep_hour": "是" if is_pre_sleep_hour else "否",
            "high_prb_days_7d": high_prb_days,
        })

    # 生成表格
    table_rows = []
    for item in data:
        table_rows.append(
            f"| {item['time']} | {item['dist_name']} | {item['prod_name']} | "
            f"{item['gnb_name']} | {item['cell_name']} | {item['cgi']} | "
            f"{item['sleep_seconds']} | {item['prb_rate']:.2f}% | "
            f"{item['is_pre_sleep_hour']} | {item['high_prb_days_7d']} |"
        )

    if not table_rows:
        table_rows.append("| — | 休眠生效前无高负荷 | — | — | — | — | — | — | — | — |")

    table_md = _build_md_table(
        headers=["时间", "地市名称", "厂家", "基站名称", "小区名称", "CGI",
                 "休眠类生效时长(秒)", f"无线利用率(%)", "是否休眠前一小时", f"7天内PRB>{PRB_HIGH_LOAD_THRESHOLD:.0f}%天数"],
        rows=table_rows,
    )

    # 获取基础信息（从第一条记录）
    first_row = detail_rows[0] if detail_rows else {}

    return {
        "data": data,
        "table": table_md,
        "high_prb_days_7d": high_prb_days,
        "cell_name": first_row.get("cell_name") or cgi,
        "dist_name": first_row.get("dist_name") or "-",
        "county_name": first_row.get("county_name") or "-",
        "prod_name": first_row.get("prod_name") or "-",
    }
