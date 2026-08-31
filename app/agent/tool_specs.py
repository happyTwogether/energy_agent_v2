"""AgentScope 业务工具的显式目录。"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from app.tools.anomaly_query_tool import (
    TOOL_DESCRIPTION as ANOMALY_DESCRIPTION,
    TOOL_INPUT_SCHEMA as ANOMALY_SCHEMA,
    query_anomaly,
)
from app.tools.batch_energy_tool import (
    TOOL_DESCRIPTION as BATCH_ENERGY_DESCRIPTION,
    TOOL_INPUT_SCHEMA as BATCH_ENERGY_SCHEMA,
    analyze_batch_cells_energy,
)
from app.tools.business_data_query_tool import (
    TOOL_DESCRIPTION as BUSINESS_DATA_DESCRIPTION,
    TOOL_INPUT_SCHEMA as BUSINESS_DATA_SCHEMA,
    query_business_data,
)
from app.tools.cell_lookup_tool import (
    TOOL_DESCRIPTION as CELL_LOOKUP_DESCRIPTION,
    TOOL_INPUT_SCHEMA as CELL_LOOKUP_SCHEMA,
    resolve_cell_cgi,
)
from app.tools.cell_metric_query_tool import (
    TOOL_DESCRIPTION as CELL_METRIC_DESCRIPTION,
    TOOL_INPUT_SCHEMA as CELL_METRIC_SCHEMA,
    query_cell_metric,
)
from app.tools.chart_tool import (
    TOOL_DESCRIPTION as CHART_DESCRIPTION,
    TOOL_INPUT_SCHEMA as CHART_SCHEMA,
    generate_chart,
)
from app.tools.energy_param_check_tool import (
    TOOL_DESCRIPTION as ENERGY_PARAM_DESCRIPTION,
    TOOL_INPUT_SCHEMA as ENERGY_PARAM_SCHEMA,
    query_energy_param_check,
)
from app.tools.energy_saving_tool import (
    TOOL_DESCRIPTION as ENERGY_SAVING_DESCRIPTION,
    TOOL_INPUT_SCHEMA as ENERGY_SAVING_SCHEMA,
    analyze_single_cell_energy,
)
from app.tools.metric_query_tool import (
    TOOL_DESCRIPTION as METRIC_DESCRIPTION,
    TOOL_INPUT_SCHEMA as METRIC_SCHEMA,
    query_metric,
)
from app.tools.report_query_tool import (
    TOOL_DESCRIPTION as REPORT_DESCRIPTION,
    TOOL_INPUT_SCHEMA as REPORT_SCHEMA,
    query_report,
)

ToolHandler = Callable[..., Awaitable[dict[str, Any]]]


@dataclass(frozen=True, slots=True)
class EnergyToolSpec:
    """一个能效业务工具的 AgentScope 注册信息。"""

    name: str
    description: str
    input_schema: dict[str, Any]
    handler: ToolHandler
    is_read_only: bool = True
    is_concurrency_safe: bool = True
    session_scope: Literal["default", "self_service"] = "default"


TOOL_SPECS = (
    EnergyToolSpec(
        name="analyze_batch_cells_energy",
        description=BATCH_ENERGY_DESCRIPTION,
        input_schema=BATCH_ENERGY_SCHEMA,
        handler=analyze_batch_cells_energy,
        is_read_only=False,
    ),
    EnergyToolSpec(
        name="analyze_single_cell_energy",
        description=ENERGY_SAVING_DESCRIPTION,
        input_schema=ENERGY_SAVING_SCHEMA,
        handler=analyze_single_cell_energy,
    ),
    EnergyToolSpec(
        name="query_business_data",
        description=BUSINESS_DATA_DESCRIPTION,
        input_schema=BUSINESS_DATA_SCHEMA,
        handler=query_business_data,
        session_scope="self_service",
    ),
    EnergyToolSpec(
        name="generate_chart",
        description=CHART_DESCRIPTION,
        input_schema=CHART_SCHEMA,
        handler=generate_chart,
    ),
    EnergyToolSpec(
        name="query_anomaly",
        description=ANOMALY_DESCRIPTION,
        input_schema=ANOMALY_SCHEMA,
        handler=query_anomaly,
        is_read_only=False,
    ),
    EnergyToolSpec(
        name="query_cell_metric",
        description=CELL_METRIC_DESCRIPTION,
        input_schema=CELL_METRIC_SCHEMA,
        handler=query_cell_metric,
        is_read_only=False,
    ),
    EnergyToolSpec(
        name="query_energy_param_check",
        description=ENERGY_PARAM_DESCRIPTION,
        input_schema=ENERGY_PARAM_SCHEMA,
        handler=query_energy_param_check,
        is_read_only=False,
    ),
    EnergyToolSpec(
        name="query_metric",
        description=METRIC_DESCRIPTION,
        input_schema=METRIC_SCHEMA,
        handler=query_metric,
        is_read_only=False,
    ),
    EnergyToolSpec(
        name="query_report",
        description=REPORT_DESCRIPTION,
        input_schema=REPORT_SCHEMA,
        handler=query_report,
    ),
    EnergyToolSpec(
        name="resolve_cell_cgi",
        description=CELL_LOOKUP_DESCRIPTION,
        input_schema=CELL_LOOKUP_SCHEMA,
        handler=resolve_cell_cgi,
    ),
)
