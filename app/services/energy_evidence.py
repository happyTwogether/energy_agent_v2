"""节电关系与工参证据的集合查询。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import text

from app.core.config import get_settings
from app.services.cell_resolver import LTE_PARAMETER_TABLE, NR_PARAMETER_TABLE
from app.services.energy_analysis import NetworkType

RowFetcher = Callable[
    [Any, dict[str, Any]],
    Awaitable[list[dict[str, Any]]],
]

DB_SCHEMA_AGENT = get_settings().db_schema_agent


async def query_neighbor_relations(
    pairs: set[tuple[str, str]],
    fetcher: RowFetcher,
) -> dict[tuple[str, str], dict[str, Any]]:
    """一次查询精确的主小区/邻区组合，避免 N+1 和关系过取。"""
    if not pairs:
        return {}
    ordered_pairs = sorted(pairs)
    sql = text(f"""
        WITH requested_pairs AS (
            SELECT *
            FROM unnest(
                CAST(:main_cgis AS text[]),
                CAST(:around_cgis AS text[])
            ) AS pair(cgi, around_cgi)
        )
        SELECT relation.cgi, relation.around_cgi,
               relation.around_cgi_network_type, relation.distance
        FROM {DB_SCHEMA_AGENT}.jd_cell_around AS relation
        INNER JOIN requested_pairs AS pair
          ON pair.cgi = relation.cgi
         AND pair.around_cgi = relation.around_cgi
    """)
    rows = await fetcher(sql, {
        "main_cgis": [pair[0] for pair in ordered_pairs],
        "around_cgis": [pair[1] for pair in ordered_pairs],
    })
    return {
        (str(row["cgi"]), str(row["around_cgi"])): row
        for row in rows
    }


async def query_site_types(
    nr_cgis: set[str],
    lte_cgis: set[str],
    fetcher: RowFetcher,
) -> dict[tuple[NetworkType, str], str]:
    """每种网络最多一次查询最新工参站型。"""
    tasks = []
    networks: list[NetworkType] = []
    for network, table, cgis in (
        ("5G", NR_PARAMETER_TABLE, nr_cgis),
        ("4G", LTE_PARAMETER_TABLE, lte_cgis),
    ):
        if not cgis:
            continue
        sql = text(f"""
            SELECT cgi, site_type
            FROM {table}
            WHERE cgi = ANY(:cgis)
              AND data_date = (SELECT MAX(data_date) FROM {table})
        """)
        tasks.append(fetcher(sql, {"cgis": sorted(cgis)}))
        networks.append(network)

    grouped_rows = await asyncio.gather(*tasks) if tasks else []
    result: dict[tuple[NetworkType, str], str] = {}
    for network, rows in zip(networks, grouped_rows):
        for row in rows:
            if row.get("cgi") and row.get("site_type"):
                result[(network, str(row["cgi"]))] = str(row["site_type"])
    return result
