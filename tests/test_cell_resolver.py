"""工参表小区名解析服务测试。"""

import importlib.util
import sys
import types
import unittest
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).parents[1]


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载模块: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _install_dependency_stubs() -> None:
    sqlalchemy = types.ModuleType("sqlalchemy")
    sqlalchemy.text = lambda sql: sql
    sqlalchemy_ext = types.ModuleType("sqlalchemy.ext")
    sqlalchemy_asyncio = types.ModuleType("sqlalchemy.ext.asyncio")
    sqlalchemy_asyncio.AsyncSession = object

    app = types.ModuleType("app")
    app.__path__ = []
    app_core = types.ModuleType("app.core")
    app_core.__path__ = []
    app_utils = types.ModuleType("app.utils")
    app_utils.__path__ = []

    config = types.ModuleType("app.core.config")
    config.get_settings = lambda: types.SimpleNamespace(db_schema="energy_monthreport")
    logging = types.ModuleType("app.core.logging")
    logging.get_logger = lambda name: types.SimpleNamespace(info=lambda *args: None)

    sys.modules.update({
        "sqlalchemy": sqlalchemy,
        "sqlalchemy.ext": sqlalchemy_ext,
        "sqlalchemy.ext.asyncio": sqlalchemy_asyncio,
        "app": app,
        "app.core": app_core,
        "app.core.config": config,
        "app.core.logging": logging,
        "app.utils": app_utils,
    })


def _load_subjects_with_isolated_stubs():
    stub_names = {
        "sqlalchemy",
        "sqlalchemy.ext",
        "sqlalchemy.ext.asyncio",
        "app",
        "app.core",
        "app.core.config",
        "app.core.logging",
        "app.utils",
        "app.utils.cell_lookup",
        "cell_resolver_under_test",
    }
    previous = {name: sys.modules.get(name) for name in stub_names}
    try:
        _install_dependency_stubs()
        lookup_module = _load_module(
            "app.utils.cell_lookup",
            PROJECT_ROOT / "app" / "utils" / "cell_lookup.py",
        )
        resolver_module = _load_module(
            "cell_resolver_under_test",
            PROJECT_ROOT / "app" / "services" / "cell_resolver.py",
        )
        return lookup_module, resolver_module
    finally:
        for name, module in previous.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


cell_lookup, cell_resolver = _load_subjects_with_isolated_stubs()


class _FakeMappings:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def all(self) -> list[dict[str, Any]]:
        return self._rows


class _FakeResult:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def mappings(self) -> _FakeMappings:
        return _FakeMappings(self._rows)


class _FakeDb:
    def __init__(self, responses: list[list[dict[str, Any]]]) -> None:
        self._responses = responses
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def execute(self, sql: str, params: dict[str, Any]) -> _FakeResult:
        self.calls.append((sql, params))
        return _FakeResult(self._responses.pop(0))


def _row(network: str = "5G") -> dict[str, str]:
    return {
        "cgi": "460-00-1-1",
        "cell_name": "长沙岳麓银盆岭小学D59002491607PT-H5H-2623",
        "network": network,
        "dist_name": "长沙市",
        "county_name": "岳麓区",
        "prod_name": "华为",
    }


class CellResolverServiceTest(unittest.IsolatedAsyncioTestCase):
    async def test_5g_lookup_only_queries_nr_parameter_table(self) -> None:
        db = _FakeDb([[_row()]])
        cell_name = _row()["cell_name"]

        resolution = await cell_resolver.resolve_cell_identifier(
            db=db,
            cell_name=cell_name,
            network="5G",
        )

        sql, params = db.calls[0]
        self.assertEqual("resolved", resolution.status)
        self.assertIn("energy_monthreport.nr_fix_prm", sql)
        self.assertNotIn("energy_monthreport.lte_fix_prm", sql)
        self.assertEqual(cell_name, params["cell_name_match"])

    async def test_exact_miss_retries_with_escaped_contains_match(self) -> None:
        db = _FakeDb([[], [_row()]])

        resolution = await cell_resolver.resolve_cell_identifier(
            db=db,
            cell_name="银盆%_",
            network="5G",
        )

        self.assertEqual("fuzzy", resolution.match_type)
        self.assertIn("cell_name = :cell_name_match", db.calls[0][0])
        self.assertIn("cell_name ILIKE :cell_name_match", db.calls[1][0])
        self.assertEqual("%银盆!%!_%", db.calls[1][1]["cell_name_match"])

    async def test_unspecified_network_queries_both_parameter_tables(self) -> None:
        db = _FakeDb([[_row("4G"), _row("5G")]])

        resolution = await cell_resolver.resolve_cell_identifier(
            db=db,
            cell_name="银盆岭小学",
        )

        sql, _ = db.calls[0]
        self.assertEqual("ambiguous", resolution.status)
        self.assertIn("energy_monthreport.lte_fix_prm", sql)
        self.assertIn("energy_monthreport.nr_fix_prm", sql)


if __name__ == "__main__":
    unittest.main()
