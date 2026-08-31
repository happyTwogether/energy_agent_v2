"""完全离线业务 Schema 目录行为测试。"""

import asyncio
from pathlib import Path

import pytest
import yaml

from app.core.config import Settings
from app.self_service.catalog import BusinessCatalogStore
from app.self_service.models import CatalogCandidate
from tests.fixtures.business_catalog_rows import BUSINESS_CATALOG_ROWS


class FakeMappings:
    def all(self) -> list[dict]:
        return BUSINESS_CATALOG_ROWS


class FakeResult:
    def mappings(self) -> FakeMappings:
        return FakeMappings()


class FakeSession:
    def __init__(self) -> None:
        self.execute_count = 0

    async def execute(self, statement, params):
        self.execute_count += 1
        return FakeResult()


@pytest.fixture
def policy_path() -> Path:
    return Path("config/business_data_catalog.yaml")


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None)


@pytest.mark.asyncio
async def test_catalog_loads_policy_tables_once(
    policy_path: Path,
    settings: Settings,
) -> None:
    session = FakeSession()
    store = BusinessCatalogStore(policy_path=policy_path, settings=settings)

    first = await store.get_or_load(session)
    second = await store.get_or_load(session)

    assert first is second
    assert session.execute_count == 1
    assert "agent_conversations" not in first.tables
    assert first.tables["nr_report_day_detail"].columns["cgi"].label == "小区号"
    assert (
        first.tables["nr_report_day_detail"].columns["cgi"].description
        == "小区号"
    )
    assert "internal_note" not in first.tables["nr_report_day_detail"].columns


@pytest.mark.asyncio
async def test_catalog_loads_and_searches_rru_inventory_tables(
    policy_path: Path,
    settings: Settings,
) -> None:
    store = BusinessCatalogStore(policy_path=policy_path, settings=settings)

    snapshot = await store.get_or_load(FakeSession())
    candidates = store.search("查询5G AAU序列号", limit=5)

    assert "lte_nrm_inventoryunitrru" in snapshot.tables
    assert "sa_nrm_inventoryunitrru" in snapshot.tables
    assert candidates[0].table.name == "sa_nrm_inventoryunitrru"
    assert candidates[0].table.columns["serialnumber"].label == "资产序列号"


def test_dictionary_tables_have_all_explicit_fields(policy_path: Path) -> None:
    policy = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
    expected_counts = {
        "lte_report_day_collect": 54,
        "lte_report_day_detail": 34,
        "nr_report_day_collect": 63,
        "nr_report_day_detail": 38,
        "lte_nrm_inventoryunitrru": 17,
        "sa_nrm_inventoryunitrru": 21,
    }

    assert {
        table_name: len(policy["tables"][table_name]["fields"])
        for table_name in expected_counts
    } == expected_counts
    assert (
        policy["tables"]["nr_report_day_detail"]["fields"]
        ["nr_supersleep_switch"]["description"]
        == "NR系统内极致体眠节能功能开关"
    )
    assert (
        policy["tables"]["sa_nrm_inventoryunitrru"]["fields"]
        ["serialnumber"]["label"]
        == "资产序列号"
    )


@pytest.mark.asyncio
async def test_concurrent_catalog_load_still_queries_metadata_once(
    policy_path: Path,
    settings: Settings,
) -> None:
    class SlowSession(FakeSession):
        async def execute(self, statement, params):
            self.execute_count += 1
            await asyncio.sleep(0)
            return FakeResult()

    session = SlowSession()
    store = BusinessCatalogStore(policy_path=policy_path, settings=settings)

    first, second = await asyncio.gather(
        store.get_or_load(session),
        store.get_or_load(session),
    )

    assert first is second
    assert session.execute_count == 1


@pytest.mark.asyncio
async def test_search_matches_alias_and_english_field_fragment(
    policy_path: Path,
    settings: Settings,
) -> None:
    store = BusinessCatalogStore(policy_path=policy_path, settings=settings)
    await store.get_or_load(FakeSession())

    chinese = store.search("查询扩展表连续部署时段", limit=5)
    english = store.search("deep_sleep hour", limit=5)

    assert chinese[0].table.name == "jd_cell_expansion_day"
    assert english[0].table.name == "nr_report_day_detail"
    assert "deepsleep_hour" in english[0].matched_columns


@pytest.mark.asyncio
async def test_search_recalls_each_intent_in_three_table_question(
    policy_path: Path,
    settings: Settings,
) -> None:
    store = BusinessCatalogStore(policy_path=policy_path, settings=settings)
    await store.get_or_load(FakeSession())

    candidates = store.search(
        "查询扩展时段、需收缩时段和周边小区距离",
        limit=5,
    )

    assert {
        "jd_cell_expansion_day",
        "jd_cell_constriction_day",
        "jd_cell_around",
    }.issubset({candidate.table.name for candidate in candidates})


@pytest.mark.asyncio
async def test_catalog_completes_missing_relationship_bridge(
    policy_path: Path,
    settings: Settings,
) -> None:
    store = BusinessCatalogStore(policy_path=policy_path, settings=settings)
    snapshot = await store.get_or_load(FakeSession())
    candidates = [
        CatalogCandidate(
            table=snapshot.tables[table_name],
            score=0.9,
        )
        for table_name in ["jd_cell_expansion_day", "jd_cell_around"]
    ]

    completed = store.complete_candidates(candidates, max_tables=3)

    assert [candidate.table.name for candidate in completed] == [
        "jd_cell_expansion_day",
        "jd_cell_around",
        "jd_cell_constriction_day",
    ]
    assert store.find_paths([candidate.table.name for candidate in completed])


@pytest.mark.asyncio
async def test_search_returns_registered_calculated_metric(
    policy_path: Path,
    settings: Settings,
) -> None:
    store = BusinessCatalogStore(policy_path=policy_path, settings=settings)
    await store.get_or_load(FakeSession())

    candidates = store.search("查询5G高耗电小区占比", limit=5)

    assert candidates[0].table.name == "nr_report_day_collect"
    assert "nr_high_power_ratio" in candidates[0].matched_metrics


@pytest.mark.asyncio
async def test_catalog_field_override_precedes_database_comment(
    policy_path: Path,
    settings: Settings,
    tmp_path: Path,
) -> None:
    policy = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
    policy["tables"]["nr_report_day_detail"]["fields"]["deepsleep_hour"] = {
        "label": "深睡时长（小时）",
        "description": "5G小区深度休眠生效时长",
        "aliases": ["deep sleep duration"],
        "unit": "小时",
    }
    override_policy = tmp_path / "catalog.yaml"
    override_policy.write_text(
        yaml.safe_dump(policy, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    store = BusinessCatalogStore(policy_path=override_policy, settings=settings)

    snapshot = await store.get_or_load(FakeSession())
    column = snapshot.tables["nr_report_day_detail"].columns["deepsleep_hour"]

    assert column.label == "深睡时长（小时）"
    assert column.description == "5G小区深度休眠生效时长"
    assert column.aliases == ("deep sleep duration",)
    assert column.unit == "小时"


@pytest.mark.asyncio
async def test_catalog_returns_only_whitelisted_relationship_path(
    policy_path: Path,
    settings: Settings,
) -> None:
    store = BusinessCatalogStore(policy_path=policy_path, settings=settings)
    await store.get_or_load(FakeSession())

    paths = store.find_paths(
        [
            "jd_cell_expansion_day",
            "jd_cell_constriction_day",
            "jd_cell_around",
        ],
    )

    assert [edge.name for edge in paths[0]] == [
        "expansion_to_constriction",
        "constriction_to_around",
    ]


@pytest.mark.asyncio
async def test_catalog_finds_two_branches_from_same_base(
    policy_path: Path,
    settings: Settings,
) -> None:
    rows = [*BUSINESS_CATALOG_ROWS]
    rows.extend([
        {
            "schema_name": "jd_agent",
            "table_name": "jd_cell_detail_hour_nr",
            "column_name": name,
            "data_type": data_type,
            "description": name,
        }
        for name, data_type in [
            ("cgi", "character varying"),
            ("stat_time", "date"),
            ("hours", "integer"),
        ]
    ])

    class StarSession(FakeSession):
        async def execute(self, statement, params):
            self.execute_count += 1

            class StarMappings:
                def all(self):
                    return rows

            class StarResult:
                def mappings(self):
                    return StarMappings()

            return StarResult()

    store = BusinessCatalogStore(policy_path=policy_path, settings=settings)
    await store.get_or_load(StarSession())

    paths = store.find_paths([
        "jd_cell_expansion_day",
        "jd_cell_constriction_day",
        "jd_cell_detail_hour_nr",
    ])

    assert {
        edge.name for edge in paths[0]
    } == {"expansion_to_constriction", "expansion_to_hour_detail"}
