"""从本轮用户输入约束专业工具范围；不依赖模型补参，也不访问数据库。"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
import re
from typing import Any
from zoneinfo import ZoneInfo


SCOPED_TOOL_NAMES = frozenset({
    "analyze_batch_cells_energy", "query_report", "query_anomaly",
    "query_energy_param_check",
})
_CITY_NAMES = (
    "长沙市", "株洲市", "湘潭市", "衡阳市", "邵阳市", "岳阳市", "常德市",
    "张家界市", "益阳市", "郴州市", "永州市", "怀化市", "娄底市",
)
_CITY_PATTERNS = tuple(
    (city, re.compile(re.escape(city[:-1]) + r"(?:市|(?=$|[\s，。！？、,!?的和与及呢吗]|节电|节能|能耗|华为|中兴|[45][gG]))"))
    for city in _CITY_NAMES
) + (("湘西土家族苗族自治州", re.compile(r"湘西(?:土家族苗族自治州|自治州|州)?(?=$|[\s，。！？、,!?的]|节电|节能|能耗|华为|中兴|[45][gG])")),)
_PROVINCE_PATTERN = re.compile(
    r"(?:湖南|广东|湖北|江西|安徽|福建|浙江|江苏|山东|山西|河北|河南|陕西|甘肃|"
    r"青海|四川|贵州|云南|辽宁|吉林|黑龙江|海南|台湾)省",
)
_QUERY_PREFIX = re.compile(
    r"^(?:请问|请|帮我|给我|麻烦|我想|想|了解|一下|那|再|重新|"
    r"查询|查看|查一下|查|分析|评估|诊断|看看|看一下|看|的|在|按|对)+",
)
_COUNTY_PATTERN = re.compile(
    r"^([\u4e00-\u9fff]{2,8}?(?:自治县|县|区|市))"
    r"(?=$|[\s，。！？、,!?的]|节电|节能|能耗|华为|中兴|[45][gG])",
)
_LOCATION_TIME_PREFIX = re.compile(
    r"^(?:20\d{2}(?:年\d{1,2}月\d{1,2}日?|[-/]\d{1,2}[-/]\d{1,2})|"
    r"今天|今日|昨天|昨日|前天|[45][gG]|LTE|NR|\s)+",
)
_VENDOR_PATTERNS = (
    ("华为", re.compile(r"华为|\bhuawei\b", re.IGNORECASE)),
    ("中兴", re.compile(r"中兴|\bzte\b", re.IGNORECASE)),
    ("爱立信", re.compile(r"爱立信|\bericsson\b", re.IGNORECASE)),
    ("诺基亚", re.compile(r"诺基亚|\bnokia\b", re.IGNORECASE)),
)
_WIDE_SCOPE_PATTERN = re.compile(r"全省|全网|全湖南|湖南省|省内|湖南(?=的|节电|节能|$)")
_REQUERY_PATTERN = re.compile(r"重新查询|重新分析|重查|改查|再查|再看")
_ENERGY_GOAL_PATTERN = re.compile(r"节电空间|节能空间|节电潜力|节能潜力")
_CGI_PATTERN = re.compile(r"(?<!\d)\d{3}-\d{2}-\d+-\d+(?!\d)")


class QueryScopeError(ValueError):
    """用户范围无法被当前工具安全表达，须停止本次查询。"""


@dataclass(frozen=True, slots=True)
class TaskContext:
    """当前会话任务的条件快照，不保存业务结果，不跨会话共享。"""

    tool_name: str
    arguments: dict[str, Any]
    goal: str

    def snapshot(self) -> dict[str, Any]:
        return {"version": 1, "tool_name": self.tool_name, "arguments": dict(self.arguments), "goal": self.goal}

    @classmethod
    def restore(cls, value: Any) -> "TaskContext | None":
        if not isinstance(value, dict) or value.get("version") != 1:
            return None
        name, args, goal = value.get("tool_name"), value.get("arguments"), value.get("goal")
        if not isinstance(name, str) or name not in SCOPED_TOOL_NAMES | {"analyze_single_cell_energy"}:
            return None
        if not isinstance(args, dict) or not isinstance(goal, str) or len(goal) > 2000:
            return None
        if any(not isinstance(key, str) or not isinstance(item, (str, int, float, bool, type(None))) for key, item in args.items()):
            return None
        if len(str(args)) > 8000:
            return None
        return cls(name, dict(args), goal)


@dataclass(frozen=True, slots=True)
class ToolRequestContext:
    """请求入口一次性还原任务；路由、校验和执行共用同一份条件。"""

    current_user_text: str
    history_user_texts: tuple[str, ...] = ()
    prior_task: TaskContext | None = None
    task: TaskContext | None = field(init=False, default=None)
    is_parameter_followup: bool = field(init=False, default=False)
    scope_error: str | None = field(init=False, default=None)

    def __post_init__(self) -> None:
        task = self.prior_task
        if task is None:
            # 兼容没有结构化快照的旧会话；损坏的旧请求不能阻断新的完整请求。
            for text in self.history_user_texts:
                try:
                    task, _ = _reduce_task(task, text)
                except QueryScopeError:
                    task = None
        try:
            task, followup = _reduce_task(task, self.current_user_text)
            object.__setattr__(self, "task", task)
            object.__setattr__(self, "is_parameter_followup", followup)
        except QueryScopeError as exc:
            object.__setattr__(self, "scope_error", str(exc))

def detect_expansion_level(text: str) -> str | None:
    normalized = text.lower().replace(" ", "")
    for value, pattern in (("aggressive", r"激进|500m"), ("moderate", r"中等|400m"), ("conservative", r"保守|300m")):
        if re.search(pattern, normalized):
            return value
    return None


def resolve_requested_date(text: str) -> str | None:
    match = re.search(r"(?<!\d)(20\d{2})[年\-/.](\d{1,2})[月\-/.](\d{1,2})日?", text)
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    if match:
        parts = tuple(map(int, match.groups()))
    else:
        match = re.search(r"(?<!\d)(\d{1,2})月(\d{1,2})日", text)
        parts = (now.year, *map(int, match.groups())) if match else None
    if parts:
        try:
            return datetime(*parts).strftime("%Y-%m-%d")
        except ValueError:
            return None
    for words, days in ((("前天",), 2), (("昨天", "昨日"), 1), (("今天", "今日"), 0)):
        if any(word in text for word in words):
            return (now - timedelta(days=days)).strftime("%Y-%m-%d")
    return None


def _reduce_task(previous: TaskContext | None, text: str) -> tuple[TaskContext | None, bool]:
    """同一个条件补丁合并器处理地区、厂家、日期、档位，完整新任务重置。"""
    actions = list(_REQUERY_PATTERN.finditer(text))
    if actions:
        text = text[actions[-1].end():].strip()
    province, city, county, vendor = _extract_scope(text)
    patch = {key: value for key, value in (
        ("province", province), ("dist_name", city), ("county_name", county), ("prod_name", vendor),
        ("stat_time", resolve_requested_date(text)), ("expansion_level", detect_expansion_level(text)),
    ) if value is not None}
    cgi = _CGI_PATTERN.search(text)
    if cgi:
        patch["cgi"] = cgi.group()
    target = re.search(r"(?:仅|只)(?:看|分析)?(扩展|收缩)", text)
    if target:
        patch["analysis_target"] = "expansion" if target.group(1) == "扩展" else "constriction"
    wide = bool(_WIDE_SCOPE_PATTERN.search(text)) and not city and not county
    if wide:
        patch["province"] = province or "湖南省"
    route = energy_analysis_route(text)
    if route is None and re.search(r"(?:能耗|节耗电|节电).{0,8}报表|报表.{0,8}(?:能耗|节耗电|节电)", text):
        route = "query_report"
    if route:
        if route.startswith("analyze_"):
            patch.setdefault("expansion_level", "conservative")
        if route == "analyze_batch_cells_energy":
            patch.setdefault("analysis_target", "all")
        return TaskContext(route, patch, text[:2000]), False

    residual = text
    for value in (province, city, county, vendor, cgi.group() if cgi else None):
        if value:
            residual = residual.replace(value, "")
    for _, pattern in _CITY_PATTERNS + _VENDOR_PATTERNS:
        residual = pattern.sub("", residual)
    residual = re.sub(r"20\d{2}[年\-/.]\d{1,2}[月\-/.]\d{1,2}日?|\d{1,2}月\d{1,2}日|今天|今日|昨天|昨日|前天", "", residual)
    residual = _WIDE_SCOPE_PATTERN.sub("", residual)
    residual = re.sub(r"保守|中等|激进|[345]00[mM]|扩展|收缩|档位|档", "", residual)
    residual = re.sub(r"请|帮我|换成|改成|切换到?|再看|看一下|看看|只看|只要|仅|只|分析|按|的|呢|吧|那|一下|[\s，。！？、,!?:：]", "", residual)
    if previous and patch and not residual:
        arguments = dict(previous.arguments)
        if wide or (city and city != arguments.get("dist_name")) or (province and province != arguments.get("province")):
            for key in ("dist_name", "county_name", "cgi", "cell_name"):
                arguments.pop(key, None)
        arguments.update(patch)
        if cgi and previous.tool_name.startswith("analyze_"):
            # 更换分析对象时切换同一任务的粒度，不能把 CGI 传给批量函数。
            arguments = {key: value for key, value in arguments.items() if key in {"cgi", "stat_time", "expansion_level", "analysis_target"}}
            return TaskContext("analyze_single_cell_energy", arguments, previous.goal), True
        return TaskContext(previous.tool_name, arguments, previous.goal), True
    if previous and re.search(r"这个结果|这个结论|上面的结果|上述结果|刚才|上一轮|你的回答", text):
        return previous, False
    return None, False


def energy_analysis_route(text: str) -> str | None:
    """仅对明确的节电诊断目标限定工具；原始时段、字段查询仍由模型规划。"""
    if not _ENERGY_GOAL_PATTERN.search(text):
        return None
    if _CGI_PATTERN.search(text):
        return "analyze_single_cell_energy"
    if "小区" in text and not re.search(r"批量|全部|所有|各小区", text):
        return None
    if any(pattern.search(text) for _, pattern in _CITY_PATTERNS) or re.search(
        r"全省|全网|湖南省|县|区", text,
    ):
        return "analyze_batch_cells_energy"
    return None


def normalize_tool_scope(
    tool_name: str,
    arguments: dict[str, Any],
    request: ToolRequestContext,
) -> dict[str, Any]:
    """在打开数据库会话之前，以用户原话覆盖范围并拒绝歧义。"""
    if tool_name not in SCOPED_TOOL_NAMES:
        return arguments
    if request.scope_error:
        raise QueryScopeError(request.scope_error)
    if tool_name == "query_energy_param_check" and (
        arguments.get("cgi") or arguments.get("cell_name")
    ):
        return arguments

    source = request.current_user_text
    if request.task:
        values = request.task.arguments
        province, city, county, vendor = (
            None if values.get(key) in {None, "全网", "all", "全部", "所有"} else values[key]
            for key in ("province", "dist_name", "county_name", "prod_name")
        )
    else:
        province, city, county, vendor = _extract_scope(source)
    model_vendor = arguments.get("prod_name")
    if not vendor and isinstance(model_vendor, str) and model_vendor not in {"全网", "all", "全部", "所有"}:
        vendor = model_vendor if model_vendor and model_vendor in source else None
    is_wide = bool(_WIDE_SCOPE_PATTERN.search(source)) or bool(province and not city and not county)
    if tool_name == "analyze_batch_cells_energy" and not any((province, city, county, vendor, is_wide)):
        raise QueryScopeError("请说明要分析的地市、区县或厂家；查询全省请明确说“全省节电空间”。")
    if county and tool_name in {"query_anomaly", "query_energy_param_check"}:
        raise QueryScopeError("当前工具不支持区县范围，不能将区县查询扩大为地市或全省。")

    normalized = dict(arguments)
    for key in ("province", "county_name"):
        if tool_name in {"query_anomaly", "query_energy_param_check"}:
            normalized.pop(key, None)
    # 固定报表查汇总行，其他专业工具查明细，通配值语义不同。
    wildcard = "全网" if tool_name in {"query_report", "query_anomaly"} else None
    normalized["dist_name"] = city or wildcard
    normalized["prod_name"] = vendor or wildcard
    if tool_name in {"analyze_batch_cells_energy", "query_report"}:
        normalized["province"] = province or "湖南省"
        normalized["county_name"] = county or wildcard
    return {key: value for key, value in normalized.items() if value is not None}


def normalize_analysis_arguments(tool_name: str, arguments: dict[str, Any], request: ToolRequestContext) -> dict[str, Any]:
    """唯一的分析参数合并入口；范围校验随后在同一工具入口执行。"""
    if tool_name not in {"analyze_batch_cells_energy", "analyze_single_cell_energy"}:
        return dict(arguments)
    normalized = dict(arguments)
    task = request.task
    if task and task.tool_name == tool_name:
        normalized.update(task.arguments)
    level = detect_expansion_level(request.current_user_text)
    if level:
        normalized["expansion_level"] = level
    requested_date = resolve_requested_date(request.current_user_text)
    if not requested_date and request.is_parameter_followup and task:
        requested_date = task.arguments.get("stat_time")
    if requested_date:
        normalized["stat_time"] = requested_date
    else:
        normalized.pop("stat_time", None)
    return normalized


def _extract_scope(text: str) -> tuple[str | None, str | None, str | None, str | None]:
    cleaned = _QUERY_PREFIX.sub("", text.strip())
    provinces = set(_PROVINCE_PATTERN.findall(cleaned)) - {"全省"}
    cities = {city for city, pattern in _CITY_PATTERNS if pattern.search(text)}
    vendors = {vendor for vendor, pattern in _VENDOR_PATTERNS if pattern.search(text)}
    remainder = cleaned
    for _, pattern in _CITY_PATTERNS:
        remainder = pattern.sub("|", remainder)
    remainder = _PROVINCE_PATTERN.sub("", remainder)
    counties = set()
    for part in re.split(r"[|，,、和与及]", remainder):
        part = _strip_location_prefix(part)
        match = _COUNTY_PATTERN.match(part)
        if match:
            county = match.group(1)
            if county.endswith("市") and not cities:
                raise QueryScopeError("暂不能确定该地点属于地市还是县级市，请补充所属地市。")
            counties.add(county)
    if any(len(values) > 1 for values in (provinces, cities, counties, vendors)):
        raise QueryScopeError("本次包含多个地区或厂家，当前工具单次只支持一个范围，请分别查询。")
    return tuple(next(iter(values), None) for values in (provinces, cities, counties, vendors))


def _strip_location_prefix(text: str) -> str:
    """移除查询动词、日期和制式前缀，地名本身不做猜测。"""
    previous = None
    while text != previous:
        previous = text
        text = _LOCATION_TIME_PREFIX.sub("", _QUERY_PREFIX.sub("", text.strip()))
    return text
