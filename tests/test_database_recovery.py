"""依赖故障不能使 HTTP 退出，也不能把不可用的查询返回成功。"""

import asyncio
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

import httpx

import main
from app.core.config import Settings


class DatabaseRecoveryTest(IsolatedAsyncioTestCase):
    async def test_tcp_timeout_keeps_page_available_and_blocks_database_requests(self):
        settings = Settings(_env_file=None, self_service_enabled=False)
        with (
            patch.object(main, "get_settings", return_value=settings),
            patch.object(main, "init_db", side_effect=TimeoutError("simulated TCP timeout")),
            patch("app.api.routes.get_session_factory") as sessions,
        ):
            app = main.create_app()
            async with main.lifespan(app):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                    self.assertEqual(200, (await client.get("/")).status_code)
                    health = await client.get("/api/v1/health")
                    self.assertEqual(503, health.status_code)
                    self.assertEqual("database_connection_timeout", health.json()["database"]["code"])
                    chat = await client.post("/api/v1/chat/stream", json={"query": "湘潭市的节电空间", "user": "test-owner"})
                    self.assertEqual(503, chat.status_code)
                    self.assertEqual("database_connection_timeout", chat.json()["code"])
                    sessions.assert_not_called()
                    self.assertEqual(200, (await client.get("/api/v1/health/live")).status_code)

    async def test_http_readiness_changes_to_200_after_database_recovery(self):
        with (
            patch.object(main, "get_settings", return_value=Settings(_env_file=None)),
            patch.object(main, "init_db", side_effect=[TimeoutError(), None]),
            patch.object(main, "check_session_storage", new_callable=AsyncMock) as periodic_check,
        ):
            app = main.create_app()
            async with main.lifespan(app):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                    self.assertEqual(503, (await client.get("/api/v1/health")).status_code)
                    self.assertTrue(await app.state.database_readiness.check())
                    health = await client.get("/api/v1/health")
                    self.assertEqual(200, health.status_code)
                    self.assertTrue(health.json()["database"]["ready"])
                    # 后续健康复查不应反复执行 init_db（开发模式可能包含 DDL）。
                    self.assertTrue(await app.state.database_readiness.check())
                    periodic_check.assert_awaited_once()

    async def test_background_retry_recovers_without_another_container_restart(self):
        check = AsyncMock(side_effect=[TimeoutError("first connection"), None])
        status = main.DatabaseReadiness(check, retry_interval=0.001, healthy_interval=60)
        self.assertFalse(await status.check())
        task = asyncio.create_task(status.run())
        try:
            await asyncio.wait_for(status.ready_event.wait(), timeout=1)
            self.assertTrue(status.ready)
            self.assertEqual(2, check.await_count)
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

    async def test_stuck_connection_is_bounded_and_cancelled(self):
        cancelled = asyncio.Event()

        async def stuck():
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        status = main.DatabaseReadiness(stuck, check_timeout=0.01)
        self.assertFalse(await status.check())
        self.assertTrue(cancelled.is_set())
        self.assertEqual("database_connection_timeout", status.code)

    async def test_unknown_database_error_does_not_expose_credentials(self):
        status = main.DatabaseReadiness(AsyncMock(side_effect=RuntimeError("password=secret-from-driver")))
        with self.assertLogs("agent.database_readiness", level="WARNING") as logs:
            self.assertFalse(await status.check())
        self.assertNotIn("secret-from-driver", str(status.snapshot()))
        self.assertNotIn("secret-from-driver", "\n".join(logs.output))
