"""Dify 持久化消息到 AgentScope 消息的适配测试。"""

import unittest

try:
    from app.agent.messages import parse_request_messages
except ModuleNotFoundError:
    parse_request_messages = None


class AgentMessagesTest(unittest.TestCase):
    """验证历史消息、当前输入和受控上下文不会互相重复。"""

    def test_separates_history_current_user_and_system_context(self) -> None:
        self.assertIsNotNone(parse_request_messages, "消息适配器尚未实现")

        parsed = parse_request_messages(  # type: ignore[misc]
            [
                {
                    "role": "system",
                    "content": '用户上下文信息：{"province": "湖南"}',
                },
                {"role": "user", "content": "上一个问题"},
                {"role": "assistant", "content": "上一个回答"},
                {"role": "user", "content": "查询长沙5G能耗"},
            ],
        )

        self.assertEqual(1, len(parsed.history))
        self.assertEqual("查询长沙5G能耗", parsed.current_user.get_text_content())
        self.assertEqual(
            '用户上下文信息：{"province": "湖南"}',
            parsed.user_context,
        )
        self.assertEqual(
            ["user"],
            [message.role for message in parsed.history],
        )

    def test_excludes_historical_assistant_answers_from_agent_context(self) -> None:
        parsed = parse_request_messages(  # type: ignore[misc]
            [
                {"role": "user", "content": "查询测试小区的保守扩展"},
                {"role": "assistant", "content": "旧回答：可扩展3小时，流量123M"},
                {"role": "user", "content": "再看中等扩展"},
            ],
        )

        history_text = [message.get_text_content() for message in parsed.history]

        self.assertEqual(["查询测试小区的保守扩展"], history_text)
        self.assertNotIn("123M", "".join(history_text))

    def test_rejects_request_without_final_user_message(self) -> None:
        self.assertIsNotNone(parse_request_messages, "消息适配器尚未实现")

        with self.assertRaisesRegex(ValueError, "最后一条.*user"):
            parse_request_messages(  # type: ignore[misc]
                [{"role": "assistant", "content": "answer"}],
            )


if __name__ == "__main__":
    unittest.main()
