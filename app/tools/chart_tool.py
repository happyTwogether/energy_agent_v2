"""图表生成工具 — 接收数据，构建 ECharts option JSON，零渲染依赖。"""

import json
from typing import Any

from app.tools.registry import tool_registry
from app.core.logging import get_logger

logger = get_logger("chart_tool")

_COLORS = ["#5470c6", "#91cc75", "#fac858", "#ee6666", "#73c0de", "#3ba272", "#fc8452", "#9a60b4"]


def _extract_val(row: dict, col: str) -> float:
    v = row.get(col, 0)
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, dict):
        return float(v.get("value", 0))
    return 0.0


def _extract_metric_cols(rows: list[dict]) -> list[str]:
    skip = {"date", "data_date", "dist_name", "prod_name", "freq_band",
            "site_type", "area", "cgi", "cell_name", "network"}
    result = []
    for k in rows[0]:
        if k in skip:
            continue
        v = rows[0][k]
        if isinstance(v, (int, float)):
            result.append(k)
        elif isinstance(v, dict) and isinstance(v.get("value"), (int, float)):
            result.append(k)
    return result


def _pivot_by_category(rows: list[dict], cat_col: str, date_col: str) -> dict[str, dict[str, float]]:
    """按 category 列 pivot: {category: {date: {metric: value}}}"""
    pivoted: dict[str, dict[str, float]] = {}
    for r in rows:
        cat = str(r.get(cat_col, "未知"))
        date = str(r.get(date_col, r.get("date", "")))
        if cat not in pivoted:
            pivoted[cat] = {}
        for k, v in r.items():
            val = _extract_val(r, k)
            pivoted[cat][f"{date}|{k}"] = val
    return pivoted


def _detect_category_dim(rows: list[dict]) -> str | None:
    """检测是否存在多类别维度（如多个地市）。返回维度列名或 None。"""
    if not rows:
        return None
    for col in ["dist_name", "prod_name"]:
        if col in rows[0]:
            vals = {str(r.get(col, "")) for r in rows}
            if len(vals) > 1:
                return col
    return None


def _extract_labels(rows: list[dict]) -> list[str]:
    for col in ["data_date", "date", "dist_name", "prod_name", "freq_band"]:
        if col in rows[0]:
            return [str(r.get(col, "")) for r in rows]
    return [str(i) for i in range(len(rows))]


def _make_line(rows: list[dict], title: str, smooth: bool = True) -> dict:
    cat_dim = _detect_category_dim(rows)
    metrics = _extract_metric_cols(rows)

    if cat_dim and "data_date" in rows[0]:
        # 多维度数据：按 category pivot，每个类别一条折线，X 轴用日期
        categories = sorted({str(r.get(cat_dim, "")) for r in rows})
        dates = sorted({str(r.get("data_date", "")) for r in rows})
        series = []
        for ci, cat in enumerate(categories):
            for metric in metrics:
                cat_rows = [r for r in rows if str(r.get(cat_dim, "")) == cat]
                date_vals: dict[str, float] = {}
                for r in cat_rows:
                    d = str(r.get("data_date", ""))
                    date_vals[d] = _extract_val(r, metric)
                data = [date_vals.get(d, 0.0) for d in dates]
                series.append({
                    "name": f"{cat}-{metric}", "type": "line", "data": data,
                    "smooth": smooth,
                    "lineStyle": {"color": _COLORS[ci % len(_COLORS)], "width": 2},
                    "symbol": "circle", "symbolSize": 4,
                })
        legend_names = [s["name"] for s in series]
    else:
        # 单维度数据：每个指标一条折线
        labels = _extract_labels(rows)
        series = []
        for i, col in enumerate(metrics):
            s = {
                "name": col, "type": "line", "data": [_extract_val(r, col) for r in rows],
                "smooth": smooth,
                "lineStyle": {"color": _COLORS[i % len(_COLORS)], "width": 2},
                "symbol": "circle", "symbolSize": 4,
            }
            series.append(s)
        legend_names = metrics
        dates = labels

    return {
        "title": {"text": title, "left": "center", "textStyle": {"fontSize": 14, "fontWeight": "bold"}},
        "tooltip": {"trigger": "axis"},
        "legend": {"data": legend_names, "top": 28, "textStyle": {"fontSize": 8}},
        "grid": {"left": "5%", "right": "5%", "bottom": "10%", "top": "20%", "containLabel": True},
        "xAxis": {"type": "category", "data": dates, "axisLabel": {"rotate": 30, "fontSize": 10}},
        "yAxis": {"type": "value"},
        "series": series,
        "backgroundColor": "transparent",
    }


def _make_bar(rows: list[dict], title: str, stacked: bool = False) -> dict:
    labels = _extract_labels(rows)
    metrics = _extract_metric_cols(rows)
    series = []
    for i, col in enumerate(metrics):
        s = {
            "name": col, "type": "bar", "data": [_extract_val(r, col) for r in rows],
            "itemStyle": {"color": _COLORS[i % len(_COLORS)], "borderRadius": [4, 4, 0, 0]},
            "barWidth": "50%" if len(metrics) == 1 else "60%",
        }
        if stacked:
            s["stack"] = "total"
        series.append(s)
    return {
        "title": {"text": title, "left": "center", "textStyle": {"fontSize": 14, "fontWeight": "bold"}},
        "tooltip": {"trigger": "axis"},
        "legend": {"data": metrics, "top": 28, "textStyle": {"fontSize": 10}},
        "grid": {"left": "5%", "right": "5%", "bottom": "10%", "top": "20%", "containLabel": True},
        "xAxis": {"type": "category", "data": labels, "axisLabel": {"rotate": 30, "fontSize": 10}},
        "yAxis": {"type": "value"},
        "series": series,
        "backgroundColor": "transparent",
    }


def _make_pie(rows: list[dict], title: str) -> dict:
    labels = _extract_labels(rows)
    metrics = _extract_metric_cols(rows)
    if not metrics:
        return {}
    data = [{"name": str(r.get(_extract_labels([r])[0] if _extract_labels([r]) else "", "")),
             "value": _extract_val(r, metrics[0])} for r in rows]
    return {
        "title": {"text": title, "left": "center", "textStyle": {"fontSize": 14, "fontWeight": "bold"}},
        "tooltip": {"trigger": "item", "formatter": "{b}: {c} ({d}%)"},
        "legend": {"data": [d["name"] for d in data], "top": 28, "textStyle": {"fontSize": 10}},
        "series": [{"type": "pie", "radius": "55%", "center": ["50%", "55%"], "data": data}],
        "backgroundColor": "transparent",
    }


def _make_scatter(rows: list[dict], title: str) -> dict:
    metrics = _extract_metric_cols(rows)
    if len(metrics) < 2:
        return _make_bar(rows, title)
    return {
        "title": {"text": title, "left": "center", "textStyle": {"fontSize": 14, "fontWeight": "bold"}},
        "tooltip": {"trigger": "item"},
        "grid": {"left": "8%", "right": "5%", "bottom": "8%", "top": "15%", "containLabel": True},
        "xAxis": {"type": "value", "name": metrics[0]},
        "yAxis": {"type": "value", "name": metrics[1]},
        "series": [{"type": "scatter", "symbolSize": 8,
                     "data": [[_extract_val(r, metrics[0]), _extract_val(r, metrics[1])] for r in rows]}],
        "backgroundColor": "transparent",
    }


def _make_radar(rows: list[dict], title: str) -> dict:
    metrics = _extract_metric_cols(rows)
    if not metrics:
        return {}
    indicator = [{"name": m, "max": max(_extract_val(r, m) for r in rows) * 1.2} for m in metrics]
    labels = _extract_labels(rows)
    series_data = []
    for i, r in enumerate(rows):
        series_data.append({
            "name": labels[i] if i < len(labels) else str(i),
            "value": [_extract_val(r, m) for m in metrics],
            "lineStyle": {"color": _COLORS[i % len(_COLORS)]},
            "itemStyle": {"color": _COLORS[i % len(_COLORS)]},
        })
    return {
        "title": {"text": title, "left": "center", "textStyle": {"fontSize": 14, "fontWeight": "bold"}},
        "tooltip": {},
        "legend": {"data": [d["name"] for d in series_data], "bottom": 0, "textStyle": {"fontSize": 10}},
        "radar": {"indicator": indicator, "center": ["50%", "55%"], "radius": "55%"},
        "series": [{"type": "radar", "data": series_data}],
        "backgroundColor": "transparent",
    }


_CHART_BUILDERS = {
    "line": lambda r, t: _make_line(r, t),
    "area": lambda r, t: _make_line(r, t, smooth=False),
    "bar": _make_bar,
    "stacked_bar": lambda r, t: _make_bar(r, t, stacked=True),
    "pie": _make_pie,
    "scatter": _make_scatter,
    "radar": _make_radar,
}


@tool_registry.tool(
    description="根据查询数据生成 ECharts 图表 option JSON。传查询结果和图表类型，返回完整 ECharts option，前端渲染。",
    parameters={
        "type": "object",
        "properties": {
            "charts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "图表标题"},
                        "chart_type": {
                            "type": "string",
                            "enum": ["line", "area", "bar", "stacked_bar", "pie", "scatter", "radar"],
                        },
                        "data": {"type": "object", "description": "query_metric 等工具的完整返回结果"},
                    },
                    "required": ["title", "data"],
                },
            },
        },
        "required": ["charts"],
    },
)
async def generate_chart(
    db,
    charts: list[dict],
) -> dict[str, Any]:
    """生成 ECharts option JSON。"""
    results = []
    for i, spec in enumerate(charts):
        title = spec.get("title", f"图表{i+1}")
        chart_type = spec.get("chart_type", "bar")
        data = spec.get("data", {})
        rows = data.get("rows", []) if isinstance(data, dict) else []

        if not rows:
            results.append({"option_json": "", "title": title, "success": False, "error": "数据为空"})
            continue

        builder = _CHART_BUILDERS.get(chart_type)
        if not builder:
            results.append({"option_json": "", "title": title, "success": False, "error": f"不支持的图表类型: {chart_type}"})
            continue

        try:
            option = builder(rows, title)
            results.append({
                "option_json": json.dumps(option, ensure_ascii=False, separators=(",", ":")),
                "title": title, "chart_type": chart_type, "index": i, "success": True,
            })
        except Exception as exc:
            logger.error("图表生成失败 [%d] %s: %s", i, title, exc, exc_info=True)
            results.append({"option_json": "", "title": title, "success": False, "error": str(exc)})

    return {"success": True, "charts": results}
