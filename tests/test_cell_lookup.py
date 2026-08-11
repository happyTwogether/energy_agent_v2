"""小区中文名解析规则测试。"""

import importlib.util
import sys
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "app" / "utils" / "cell_lookup.py"
MODULE_SPEC = importlib.util.spec_from_file_location("cell_lookup_under_test", MODULE_PATH)
if MODULE_SPEC is None or MODULE_SPEC.loader is None:
    raise RuntimeError("无法加载小区名解析模块")
cell_lookup = importlib.util.module_from_spec(MODULE_SPEC)
sys.modules[MODULE_SPEC.name] = cell_lookup
MODULE_SPEC.loader.exec_module(cell_lookup)

MAX_CELL_CANDIDATES = cell_lookup.MAX_CELL_CANDIDATES
build_contains_pattern = cell_lookup.build_contains_pattern
resolve_cell_name = cell_lookup.resolve_cell_name
resolution_error_response = cell_lookup.resolution_error_response


SAMPLE_CELL_NAME = "长沙岳麓银盆岭小学D59002491607PT-H5H-2623"


def _candidate(cgi: str, cell_name: str = SAMPLE_CELL_NAME) -> dict[str, str]:
    return {
        "cgi": cgi,
        "cell_name": cell_name,
        "network": "5G",
        "dist_name": "长沙市",
        "county_name": "岳麓区",
        "prod_name": "华为",
    }


class CellLookupTest(unittest.IsolatedAsyncioTestCase):
    async def test_exact_match_has_priority(self) -> None:
        calls: list[tuple[str, bool, int]] = []

        async def fetch(value: str, exact: bool, limit: int) -> list[dict[str, str]]:
            calls.append((value, exact, limit))
            return [_candidate("460-00-1-1")] if exact else []

        resolution = await resolve_cell_name(SAMPLE_CELL_NAME, fetch)

        self.assertEqual("resolved", resolution.status)
        self.assertEqual("exact", resolution.match_type)
        self.assertEqual("460-00-1-1", resolution.cgi)
        self.assertEqual("长沙市", resolution.candidate["dist_name"])
        self.assertEqual([(SAMPLE_CELL_NAME, True, MAX_CELL_CANDIDATES + 1)], calls)

    async def test_unique_contiguous_fragment_resolves(self) -> None:
        calls: list[tuple[str, bool]] = []

        async def fetch(value: str, exact: bool, limit: int) -> list[dict[str, str]]:
            del limit
            calls.append((value, exact))
            return [] if exact else [_candidate("460-00-1-1")]

        resolution = await resolve_cell_name("银盆岭小学", fetch)

        self.assertEqual("resolved", resolution.status)
        self.assertEqual("fuzzy", resolution.match_type)
        self.assertEqual([(
            "银盆岭小学", True
        ), (
            "%银盆岭小学%", False
        )], calls)

    async def test_duplicate_exact_names_require_selection(self) -> None:
        async def fetch(value: str, exact: bool, limit: int) -> list[dict[str, str]]:
            del value, limit
            if not exact:
                self.fail("精确名重复时不应继续模糊查询")
            return [_candidate("460-00-1-1"), _candidate("460-00-2-1")]

        resolution = await resolve_cell_name(SAMPLE_CELL_NAME, fetch)

        self.assertEqual("ambiguous", resolution.status)
        self.assertEqual("exact", resolution.match_type)
        self.assertEqual(2, len(resolution.candidates))

    async def test_same_cgi_in_different_networks_remains_ambiguous(self) -> None:
        lte = _candidate("same-cgi")
        lte["network"] = "4G"
        nr = _candidate("same-cgi")
        nr["network"] = "5G"

        async def fetch(value: str, exact: bool, limit: int) -> list[dict[str, str]]:
            del value, limit
            return [lte, nr] if exact else []

        resolution = await resolve_cell_name(SAMPLE_CELL_NAME, fetch)

        self.assertEqual("ambiguous", resolution.status)
        self.assertEqual({"4G", "5G"}, {
            candidate["network"] for candidate in resolution.candidates
        })

    async def test_short_fuzzy_query_is_rejected_after_exact_miss(self) -> None:
        call_count = 0

        async def fetch(value: str, exact: bool, limit: int) -> list[dict[str, str]]:
            nonlocal call_count
            del value, exact, limit
            call_count += 1
            return []

        resolution = await resolve_cell_name("小", fetch)

        self.assertEqual("invalid", resolution.status)
        self.assertEqual(1, call_count)
        self.assertIn("至少需要 2 个有效字符", resolution.message)

    async def test_ambiguous_candidates_are_capped_and_deduplicated(self) -> None:
        rows = [_candidate(f"460-00-1-{index}") for index in range(12)]
        rows.append(_candidate("460-00-1-0"))

        async def fetch(value: str, exact: bool, limit: int) -> list[dict[str, str]]:
            del value, limit
            return [] if exact else rows

        resolution = await resolve_cell_name("银盆岭小学", fetch)
        response = resolution_error_response(resolution)

        self.assertEqual("ambiguous", resolution.status)
        self.assertTrue(response["requires_cell_selection"])
        self.assertTrue(response["candidates_truncated"])
        self.assertEqual(MAX_CELL_CANDIDATES, len(response["candidates"]))
        self.assertEqual({
            "cell_name", "cgi", "network", "dist_name", "county_name", "prod_name"
        }, set(response["candidates"][0]))

    async def test_no_match_returns_clear_error(self) -> None:
        async def fetch(value: str, exact: bool, limit: int) -> list[dict[str, str]]:
            del value, exact, limit
            return []

        resolution = await resolve_cell_name("银盆岭小学", fetch)
        response = resolution_error_response(resolution)

        self.assertEqual("not_found", resolution.status)
        self.assertFalse(response["success"])
        self.assertNotIn("requires_cell_selection", response)

    def test_like_wildcards_are_treated_as_literal_text(self) -> None:
        self.assertEqual("%银盆!%!_!!H5H%", build_contains_pattern("银盆%_!H5H"))


if __name__ == "__main__":
    unittest.main()
