"""HTTP 运行时可选代理依赖的特性回归测试。"""

import os
from unittest.mock import patch
import unittest

import httpx


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


if __name__ == "__main__":
    unittest.main()
