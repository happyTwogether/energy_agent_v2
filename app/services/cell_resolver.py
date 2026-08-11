"""小区中文名与 CGI 解析服务。

小区身份信息统一来自 4G/5G 工参表，不依赖具体的
节电分析结果表，供各单小区工具复用。
"""

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.logging import get_logger
from app.utils.cell_lookup import CellNameResolution, resolve_cell_name


DB_SCHEMA = get_settings().db_schema
LTE_PARAMETER_TABLE = f"{DB_SCHEMA}.lte_fix_prm"
NR_PARAMETER_TABLE = f"{DB_SCHEMA}.nr_fix_prm"
SUPPORTED_NETWORKS = frozenset({"4G", "5G"})
logger = get_logger("cell_resolver")


class CellResolverValidationError(ValueError):
    """小区解析请求参数不合法。"""


def _build_filter_conditions(
    province: str | None,
    dist_name: str | None,
    county_name: str | None,
    prod_name: str | None,
) -> tuple[list[str], dict[str, Any]]:
    conditions: list[str] = []
    params: dict[str, Any] = {}
    for column, value in (
        ("province", province),
        ("dist_name", dist_name),
        ("county_name", county_name),
        ("prod_name", prod_name),
    ):
        if value:
            conditions.append(f"{column} = :{column}")
            params[column] = value
    return conditions, params


def _build_source_sql(
    table: str,
    network: str,
    match_clause: str,
    filter_conditions: list[str],
) -> str:
    where_parts = [
        match_clause,
        f"data_date = (SELECT MAX(data_date) FROM {table})",
        *filter_conditions,
    ]
    return f"""
        SELECT cgi, cell_name, province, dist_name, county_name, prod_name,
               site_type, area, '{network}' AS network, data_date
        FROM {table}
        WHERE {' AND '.join(where_parts)}
    """


async def _fetch_cell_candidates(
    db: AsyncSession,
    cell_name_match: str,
    exact: bool,
    limit: int,
    network: str | None,
    province: str | None,
    dist_name: str | None,
    county_name: str | None,
    prod_name: str | None,
) -> list[dict[str, Any]]:
    match_clause = (
        "cell_name = :cell_name_match"
        if exact
        else "cell_name ILIKE :cell_name_match ESCAPE '!'"
    )
    filter_conditions, params = _build_filter_conditions(
        province, dist_name, county_name, prod_name
    )
    sources: list[str] = []
    if network in (None, "4G"):
        sources.append(
            _build_source_sql(
                LTE_PARAMETER_TABLE,
                "4G",
                match_clause,
                filter_conditions,
            )
        )
    if network in (None, "5G"):
        sources.append(
            _build_source_sql(
                NR_PARAMETER_TABLE,
                "5G",
                match_clause,
                filter_conditions,
            )
        )

    sql = text(f"""
        SELECT DISTINCT ON (cgi, network)
               cgi, cell_name, province, dist_name, county_name, prod_name,
               site_type, area, network, data_date
        FROM ({' UNION ALL '.join(sources)}) AS matched_cells
        ORDER BY cgi, network
        LIMIT :limit
    """)
    params.update({"cell_name_match": cell_name_match, "limit": limit})
    result = await db.execute(sql, params)
    return [dict(row) for row in result.mappings().all()]


async def resolve_cell_identifier(
    db: AsyncSession,
    cell_name: str | None,
    network: str | None = None,
    province: str | None = None,
    dist_name: str | None = None,
    county_name: str | None = None,
    prod_name: str | None = None,
) -> CellNameResolution:
    """从 LTE/NR 工参表将小区名精确或模糊解析为 CGI。"""
    if network is not None and network not in SUPPORTED_NETWORKS:
        raise CellResolverValidationError("网络制式仅支持 4G 或 5G。")

    logger.info("小区名解析: cell_name=%s, network=%s", cell_name, network or "4G/5G")

    async def _fetch(name_match: str, exact: bool, limit: int) -> list[dict[str, Any]]:
        return await _fetch_cell_candidates(
            db,
            name_match,
            exact,
            limit,
            network,
            province,
            dist_name,
            county_name,
            prod_name,
        )

    return await resolve_cell_name(cell_name, _fetch)
