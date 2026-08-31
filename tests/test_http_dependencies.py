"""HTTP 运行时可选代理依赖的特性回归测试。"""

import os
from unittest.mock import patch
import unittest

import httpx

import main
from app.core.config import Settings


class HttpDependencyTest(unittest.IsolatedAsyncioTestCase):
    async def test_httpx_client_supports_lowercase_socks_proxy(self) -> None:
        """保证生产依赖能在 SOCKS 代理环境中创建模型客户端。"""
        with patch.dict(
            os.environ,
            {"all_proxy": "socks5://127.0.0.1:1"},
            clear=True,
        ):
            client = httpx.AsyncClient()
            await client.aclose()

    async def test_lifespan_warms_catalog_and_disposes_reader_engine(self) -> None:
        settings = Settings(_env_file=None, self_service_enabled=True)
        with (
            patch.object(main, "get_settings", return_value=settings),
            patch.object(main, "init_db", return_value=None) as init_db,
            patch.object(
                main,
                "warm_business_catalog",
                return_value=None,
            ) as warm_catalog,
            patch.object(
                main,
                "dispose_self_service_engine",
                return_value=None,
            ) as dispose_engine,
        ):
            async with main.lifespan(None):
                pass

        init_db.assert_awaited_once()
        warm_catalog.assert_awaited_once()
        dispose_engine.assert_awaited_once()

    async def test_catalog_warm_failure_does_not_block_lifespan(self) -> None:
        settings = Settings(_env_file=None, self_service_enabled=True)
        with (
            patch.object(main, "get_settings", return_value=settings),
            patch.object(main, "init_db", return_value=None),
            patch.object(
                main,
                "warm_business_catalog",
                side_effect=RuntimeError("metadata unavailable"),
            ),
            patch.object(
                main,
                "dispose_self_service_engine",
                return_value=None,
            ),
        ):
            async with main.lifespan(None):
                reached_yield = True

        self.assertTrue(reached_yield)


if __name__ == "__main__":
    unittest.main()
