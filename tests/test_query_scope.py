"""范围约束通过真实 Agent→Toolkit 链路执行，模型漏参与错参不能扩大查询。"""

import unittest
from unittest.mock import patch

from agentscope.event import ToolResultEndEvent
from agentscope.message import ToolResultState

from app.agent.messages import parse_request_messages
from app.agent.runtime import build_energy_agent
from app.agent.tool_specs import EnergyToolSpec
from app.core.config import Settings
from test_agent_integration import FakeSessionContext, ToolCallingModel


class QueryScopeTest(unittest.IsolatedAsyncioTestCase):
    async def test_strategy_switch_inherits_latest_elliptical_city_not_first_city(self):
        calls, _, _ = await self._run(
            "中等扩展", {"dist_name": "全网"},
            history=("湘潭市的节电空间", "株洲市的呢？"),
        )
        self.assertEqual(1, len(calls))
        self.assertEqual("株洲市", calls[0]["dist_name"])
        self.assertEqual("moderate", calls[0]["expansion_level"])

    async def _run(self, question, arguments, history=(), tool_name="analyze_batch_cells_energy"):
        calls, sessions = [], []

        async def handler(db, **kwargs):
            calls.append(kwargs)
            return {"success": True, "report_content": "范围已核对"}

        def session_factory():
            sessions.append(True)
            return FakeSessionContext()

        spec = EnergyToolSpec(
            name=tool_name, description="业务查询", handler=handler,
            input_schema={"type": "object", "properties": {
                key: {"type": "string"}
                for key in ("province", "dist_name", "county_name", "prod_name")
            }},
        )
        parsed = parse_request_messages([
            *({"role": "user", "content": text} for text in history),
            {"role": "user", "content": question},
        ])
        model = ToolCallingModel(tool_name, arguments)
        with (
            patch("app.agent.toolkit.TOOL_SPECS", (spec,)),
            patch("app.agent.toolkit._new_session", side_effect=session_factory),
        ):
            agent = build_energy_agent(
                "scope-test", parsed, model=model,
                settings=Settings(_env_file=None, agent_max_steps=3),
            )
            events = [event async for event in agent.reply_stream(parsed.current_user)]
        return calls, sessions, events

    async def test_user_city_overrides_missing_wildcard_or_wrong_model_city(self):
        for question, city in (
            ("湘潭市的节电空间", "湘潭市"),
            ("帮我看一下湘潭的节电空间", "湘潭市"),
            ("分析长沙市的节电潜力", "长沙市"),
            ("衡阳节能空间", "衡阳市"),
            ("刚才你查了长沙市，请重新查询湘潭市的节电空间", "湘潭市"),
        ):
            for tool_name in ("analyze_batch_cells_energy", "query_report", "query_anomaly", "query_energy_param_check"):
                with self.subTest(question=question, tool=tool_name):
                    calls, _, _ = await self._run(
                        question, {"dist_name": "全网", "county_name": "芙蓉区"},
                        tool_name=tool_name,
                    )
                    self.assertEqual(1, len(calls))
                    self.assertEqual(city, calls[0]["dist_name"])
                    self.assertIn(calls[0].get("county_name"), (None, "全网"))

    async def test_county_is_preserved_without_becoming_same_named_city(self):
        for question, city, county in (
            ("湘潭县的节电空间", None, "湘潭县"),
            ("湘潭市岳塘区的节电空间", "湘潭市", "岳塘区"),
            ("帮我分析岳塘区的节电空间", None, "岳塘区"),
            ("长沙市浏阳市的节电空间", "长沙市", "浏阳市"),
            ("查询2026年10月28日湘潭市岳塘区的节电空间", "湘潭市", "岳塘区"),
        ):
            with self.subTest(question=question):
                calls, _, _ = await self._run(question, {"dist_name": "全网"})
                self.assertEqual(1, len(calls))
                self.assertEqual(city, calls[0].get("dist_name"))
                self.assertEqual(county, calls[0]["county_name"])

    async def test_switch_followup_inherits_region_but_new_city_clears_old_county(self):
        calls, _, _ = await self._run(
            "换成激进扩展", {"dist_name": "全网"},
            history=("湘潭市岳塘区华为的节电空间",),
        )
        self.assertEqual("湘潭市", calls[0]["dist_name"])
        self.assertEqual("岳塘区", calls[0]["county_name"])
        self.assertEqual("华为", calls[0]["prod_name"])

        calls, _, _ = await self._run(
            "再看激进扩展", {"dist_name": "全网"},
            history=("湘潭市岳塘区华为的节电空间", "换成中等扩展"),
        )
        self.assertEqual("湘潭市", calls[0]["dist_name"])
        self.assertEqual("岳塘区", calls[0]["county_name"])

        calls, _, _ = await self._run(
            "再看长沙市的节电空间", {"dist_name": "湘潭市", "county_name": "岳塘区"},
            history=("湘潭市岳塘区的节电空间",),
        )
        self.assertEqual("长沙市", calls[0]["dist_name"])
        self.assertIsNone(calls[0].get("county_name"))

    async def test_explicit_province_clears_model_city_and_county(self):
        calls, _, _ = await self._run(
            "全省中兴节电空间", {"dist_name": "湘潭市", "county_name": "岳塘区"},
            history=("湘潭市岳塘区的节电空间",),
        )
        self.assertIsNone(calls[0].get("dist_name"))
        self.assertIsNone(calls[0].get("county_name"))
        self.assertEqual("中兴", calls[0]["prod_name"])

    async def test_ambiguous_or_missing_scope_stops_before_opening_database(self):
        for question, history in (
            ("湘潭市和长沙市的节电空间", ()),
            ("看看节电空间", ()),
            ("换成激进扩展", ("查询湘潭市节电空间", "长沙有哪些厂家")),
            ("帮我看湘潭大学附近的节电空间", ()),
            ("2026年10月28日湘潭市和长沙市节电空间", ()),
            ("岳塘区和雨湖区的节电空间", ()),
            ("湘潭和长沙的节电空间", ()),
        ):
            with self.subTest(question=question):
                calls, sessions, events = await self._run(question, {}, history)
                self.assertEqual([], calls)
                self.assertEqual([], sessions)
                self.assertTrue(any(
                    isinstance(event, ToolResultEndEvent) and event.state == ToolResultState.ERROR
                    for event in events
                ))
