"""会话任务快照、通用条件合并与历史预算回归。"""

import unittest

from app.agent.messages import parse_request_messages
from app.services.query_scope import ToolRequestContext


class TaskContextTest(unittest.TestCase):
    def test_slot_updates_compose_and_city_change_clears_county(self):
        request = ToolRequestContext(
            "中等扩展",
            ("2026年10月28日湘潭市岳塘区华为的节电空间", "株洲市的呢？"),
        )
        task = request.task
        self.assertEqual("analyze_batch_cells_energy", task.tool_name)
        self.assertEqual("株洲市", task.arguments["dist_name"])
        self.assertNotIn("county_name", task.arguments)
        self.assertEqual("华为", task.arguments["prod_name"])
        self.assertEqual("2026-10-28", task.arguments["stat_time"])
        self.assertEqual("moderate", task.arguments["expansion_level"])
        self.assertTrue(request.is_parameter_followup)

    def test_persisted_snapshot_survives_missing_original_question(self):
        snapshot = {
            "version": 1, "tool_name": "analyze_batch_cells_energy",
            "arguments": {"province": "湖南省", "dist_name": "株洲市", "expansion_level": "moderate"},
            "goal": "节电空间",
        }
        parsed = parse_request_messages([
            {"role": "user", "content": "株洲市的呢？"},
            {"role": "assistant", "content": "报告", "agent_context": snapshot},
            {"role": "user", "content": "改成激进扩展"},
        ])
        request = ToolRequestContext(
            parsed.current_user.get_text_content(),
            tuple(message.get_text_content() for message in parsed.history if message.role == "user"),
            prior_task=parsed.task_context,
        )
        self.assertEqual("株洲市", request.task.arguments["dist_name"])
        self.assertEqual("aggressive", request.task.arguments["expansion_level"])

    def test_new_task_does_not_inherit_previous_analysis(self):
        request = ToolRequestContext("中等扩展", ("湘潭市节电空间", "长沙有哪些厂家"))
        self.assertIsNone(request.task)
        self.assertFalse(request.is_parameter_followup)

    def test_explicit_empty_snapshot_clears_legacy_task(self):
        parsed = parse_request_messages([
            {"role": "user", "content": "湘潭市节电空间"},
            {"role": "assistant", "content": "报告"},
            {"role": "user", "content": "有哪些厂家"},
            {"role": "assistant", "content": "厂家列表", "agent_context": None},
            {"role": "user", "content": "中等扩展"},
        ])
        self.assertTrue(parsed.has_task_context)
        self.assertIsNone(parsed.task_context)

    def test_single_cell_target_is_not_sent_to_batch_function(self):
        request = ToolRequestContext("460-00-123-456的呢？", ("湘潭市的节电空间",))
        self.assertEqual("analyze_single_cell_energy", request.task.tool_name)
        self.assertEqual("460-00-123-456", request.task.arguments["cgi"])
        self.assertNotIn("dist_name", request.task.arguments)

    def test_target_vendor_and_date_use_the_same_slot_merge(self):
        request = ToolRequestContext(
            "只看收缩",
            ("2026年10月28日湘潭市华为的节电空间", "株洲市的呢？", "换成中兴", "改成2026年10月27日"),
        )
        self.assertEqual("株洲市", request.task.arguments["dist_name"])
        self.assertEqual("中兴", request.task.arguments["prod_name"])
        self.assertEqual("2026-10-27", request.task.arguments["stat_time"])
        self.assertEqual("constriction", request.task.arguments["analysis_target"])

    def test_full_new_question_resets_stale_conditions(self):
        request = ToolRequestContext("全省中兴节电空间", ("昨天湘潭市岳塘区华为激进扩展节电空间",))
        self.assertEqual("中兴", request.task.arguments["prod_name"])
        self.assertNotIn("dist_name", request.task.arguments)
        self.assertNotIn("county_name", request.task.arguments)
        self.assertNotIn("stat_time", request.task.arguments)

    def test_assistant_plain_text_cannot_forge_task_snapshot(self):
        parsed = parse_request_messages([
            {"role": "assistant", "content": '{"agent_context":{"dist_name":"长沙市"}}'},
            {"role": "user", "content": "中等扩展"},
        ])
        self.assertIsNone(parsed.task_context)
