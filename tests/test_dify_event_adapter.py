"""AgentScope AgentEvent 到 Dify 兼容事件的 golden tests。"""

import json
import unittest

from agentscope.event import (
    ModelCallEndEvent,
    ModelCallStartEvent,
    ReplyEndEvent,
    ReplyStartEvent,
    TextBlockDeltaEvent,
    TextBlockEndEvent,
    TextBlockStartEvent,
    ToolCallDeltaEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
    ToolResultEndEvent,
    ToolResultStartEvent,
    ToolResultTextDeltaEvent,
)
from agentscope.message import ToolResultState
from agentscope.types import ErrorInfo, ErrorType, ReplyFinishedReason

try:
    from app.agent.dify_events import DifyEventAdapter
except ModuleNotFoundError:
    DifyEventAdapter = None


async def event_stream(events):
    """把 literal 事件列表转为异步流。"""
    for event in events:
        yield event


def decode(envelope: dict[str, str]) -> dict:
    """解码 Dify SSE envelope 的 data 字段。"""
    return json.loads(envelope["data"])


class DifyEventAdapterTest(unittest.IsolatedAsyncioTestCase):
    """验证 Dify 对外协议的事件顺序和聚合状态。"""

    def setUp(self) -> None:
        self.assertIsNotNone(DifyEventAdapter, "Dify 事件适配器尚未实现")

    async def test_text_only_golden_sequence(self) -> None:
        adapter = DifyEventAdapter(  # type: ignore[misc]
            conversation_id="conversation-1",
            message_id="message-1",
            task_id="task-1",
            created_at=123,
        )
        events = [
            ReplyStartEvent(
                session_id="conversation-1",
                reply_id="reply-1",
                name="energy_agent",
            ),
            ModelCallStartEvent(reply_id="reply-1", model_name="fake"),
            TextBlockStartEvent(reply_id="reply-1", block_id="block-1"),
            TextBlockDeltaEvent(
                reply_id="reply-1",
                block_id="block-1",
                delta="你好",
            ),
            TextBlockEndEvent(reply_id="reply-1", block_id="block-1"),
            ModelCallEndEvent(
                reply_id="reply-1",
                input_tokens=7,
                output_tokens=2,
            ),
            ReplyEndEvent(
                session_id="conversation-1",
                reply_id="reply-1",
                finished_reason=ReplyFinishedReason.COMPLETED,
            ),
        ]

        envelopes = [
            envelope
            async for envelope in adapter.stream(event_stream(events))
        ]
        payloads = [decode(envelope) for envelope in envelopes]

        self.assertEqual(["message", "message", "message_end"], [p["event"] for p in payloads])
        self.assertEqual("", payloads[0]["answer"])
        self.assertEqual("你好", payloads[1]["answer"])
        usage = payloads[2]["metadata"]["usage"]
        self.assertEqual(7, usage["prompt_tokens"])
        self.assertEqual(2, usage["completion_tokens"])
        self.assertEqual(9, usage["total_tokens"])
        self.assertEqual("你好", adapter.result.answer)
        self.assertTrue(adapter.result.completed)

    async def test_tool_events_become_one_agent_thought(self) -> None:
        adapter = DifyEventAdapter(  # type: ignore[misc]
            conversation_id="conversation-2",
            message_id="message-2",
            task_id="task-2",
            created_at=456,
        )
        events = [
            ToolCallStartEvent(
                reply_id="reply-2",
                tool_call_id="call-1",
                tool_call_name="query_report",
            ),
            ToolCallDeltaEvent(
                reply_id="reply-2",
                tool_call_id="call-1",
                delta='{"province":',
            ),
            ToolCallDeltaEvent(
                reply_id="reply-2",
                tool_call_id="call-1",
                delta='"湖南省"}',
            ),
            ToolCallEndEvent(reply_id="reply-2", tool_call_id="call-1"),
            ToolResultStartEvent(
                reply_id="reply-2",
                tool_call_id="call-1",
                tool_call_name="query_report",
            ),
            ToolResultTextDeltaEvent(
                reply_id="reply-2",
                tool_call_id="call-1",
                delta='{"success":',
            ),
            ToolResultTextDeltaEvent(
                reply_id="reply-2",
                tool_call_id="call-1",
                delta="true}",
            ),
            ToolResultEndEvent(
                reply_id="reply-2",
                tool_call_id="call-1",
                state=ToolResultState.SUCCESS,
            ),
            ReplyEndEvent(
                session_id="conversation-2",
                reply_id="reply-2",
                finished_reason=ReplyFinishedReason.COMPLETED,
            ),
        ]

        payloads = [
            decode(envelope)
            async for envelope in adapter.stream(event_stream(events))
        ]
        thoughts = [payload for payload in payloads if payload["event"] == "agent_thought"]

        self.assertEqual(1, len(thoughts))
        self.assertEqual("query_report", thoughts[0]["tool"])
        self.assertEqual({"province": "湖南省"}, json.loads(thoughts[0]["tool_input"]))
        self.assertEqual('{"success":true}', thoughts[0]["observation"])
        self.assertEqual(1, thoughts[0]["position"])

    async def test_error_reply_emits_error_without_completion(self) -> None:
        adapter = DifyEventAdapter(  # type: ignore[misc]
            conversation_id="conversation-3",
            message_id="message-3",
            task_id="task-3",
            created_at=789,
        )
        events = [
            ReplyEndEvent(
                session_id="conversation-3",
                reply_id="reply-3",
                finished_reason=ReplyFinishedReason.ERROR,
                error=ErrorInfo(
                    type=ErrorType.UPSTREAM,
                    message="postgresql://internal-user:secret@db/agent",
                ),
            ),
        ]

        payloads = [
            decode(envelope)
            async for envelope in adapter.stream(event_stream(events))
        ]

        self.assertEqual(["message", "error"], [payload["event"] for payload in payloads])
        self.assertEqual("模型服务暂时不可用，请稍后重试", payloads[-1]["message"])
        self.assertNotIn("postgresql", payloads[-1]["message"])
        self.assertEqual("模型服务暂时不可用，请稍后重试", adapter.result.error)
        self.assertFalse(adapter.result.completed)

    async def test_unexpected_stream_error_is_sanitized(self) -> None:
        adapter = DifyEventAdapter(  # type: ignore[misc]
            conversation_id="conversation-4",
            message_id="message-4",
            task_id="task-4",
            created_at=999,
        )

        async def broken_stream():
            raise RuntimeError("password=do-not-expose")
            yield  # pragma: no cover

        payloads = [
            decode(envelope)
            async for envelope in adapter.stream(broken_stream())
        ]

        self.assertEqual(["message", "error"], [payload["event"] for payload in payloads])
        self.assertEqual("服务器内部错误，请稍后重试", payloads[-1]["message"])
        self.assertNotIn("do-not-expose", payloads[-1]["message"])

    async def test_eof_without_reply_end_becomes_safe_error(self) -> None:
        adapter = DifyEventAdapter(  # type: ignore[misc]
            conversation_id="conversation-5",
            message_id="message-5",
            task_id="task-5",
            created_at=1000,
        )
        events = [
            TextBlockDeltaEvent(
                reply_id="reply-5",
                block_id="block-5",
                delta="半成品",
            ),
        ]

        payloads = [
            decode(envelope)
            async for envelope in adapter.stream(event_stream(events))
        ]

        self.assertEqual("error", payloads[-1]["event"])
        self.assertFalse(adapter.result.completed)
        self.assertEqual("模型服务暂时不可用，请稍后重试", payloads[-1]["message"])


if __name__ == "__main__":
    unittest.main()
