"""FastAPI 路由切换到 AgentScope 运行时的协议回归测试。"""

import json
from unittest.mock import AsyncMock, patch
import unittest

from agentscope.event import (
    ModelCallEndEvent,
    ReplyEndEvent,
    TextBlockDeltaEvent,
    ToolCallDeltaEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
    ToolResultEndEvent,
    ToolResultStartEvent,
    ToolResultTextDeltaEvent,
)
from agentscope.message import ToolResultState
from agentscope.types import ErrorInfo, ErrorType, ReplyFinishedReason

from app.models.schemas import DifyChatRequest

try:
    from app.api import routes
except ModuleNotFoundError:
    routes = None


async def fake_event_stream(error: bool = False):
    """生成同时包含工具 thought、文本与 usage 的 AgentScope 事件序列。"""
    yield ToolCallStartEvent(
        reply_id="reply-1",
        tool_call_id="call-1",
        tool_call_name="query_report",
    )
    yield ToolCallDeltaEvent(
        reply_id="reply-1",
        tool_call_id="call-1",
        delta='{"province":"湖南省"}',
    )
    yield ToolCallEndEvent(reply_id="reply-1", tool_call_id="call-1")
    yield ToolResultStartEvent(
        reply_id="reply-1",
        tool_call_id="call-1",
        tool_call_name="query_report",
    )
    yield ToolResultTextDeltaEvent(
        reply_id="reply-1",
        tool_call_id="call-1",
        delta='{"success":true}',
    )
    yield ToolResultEndEvent(
        reply_id="reply-1",
        tool_call_id="call-1",
        state=ToolResultState.SUCCESS,
    )
    yield TextBlockDeltaEvent(
        reply_id="reply-1",
        block_id="text-1",
        delta="最终回答",
    )
    yield ModelCallEndEvent(
        reply_id="reply-1",
        input_tokens=7,
        output_tokens=2,
    )
    if error:
        yield ReplyEndEvent(
            session_id="conversation-1",
            reply_id="reply-1",
            finished_reason=ReplyFinishedReason.ERROR,
            error=ErrorInfo(type=ErrorType.UPSTREAM, message="模型失败"),
        )
    else:
        yield ReplyEndEvent(
            session_id="conversation-1",
            reply_id="reply-1",
            finished_reason=ReplyFinishedReason.COMPLETED,
        )


async def empty_completed_event_stream():
    yield ReplyEndEvent(
        session_id="conversation-empty",
        reply_id="reply-empty",
        finished_reason=ReplyFinishedReason.COMPLETED,
    )


class FakeSessionContext:
    """路由测试使用的最小异步数据库上下文。"""

    def __init__(self) -> None:
        self.db = object()

    async def __aenter__(self):
        return self.db

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class FakeSessionFactory:
    def __call__(self):
        return FakeSessionContext()


def decode_events(envelopes):
    return [json.loads(envelope["data"]) for envelope in envelopes]


class AgentRoutesTest(unittest.IsolatedAsyncioTestCase):
    """验证路由只依赖 AgentScope 事件和统一 Dify 适配器。"""

    def setUp(self) -> None:
        self.assertIsNotNone(routes, "旧路由仍依赖已移除的 LiteLLM")
        self.assertTrue(
            hasattr(routes, "stream_agent_events"),  # type: ignore[arg-type]
            "路由尚未切换到 AgentScope stream_agent_events",
        )

    async def test_streaming_golden_sequence_and_single_persistence(self) -> None:
        persist = AsyncMock()
        with (
            patch.object(routes, "stream_agent_events", return_value=fake_event_stream()),
            patch.object(routes, "get_session_factory", return_value=FakeSessionFactory()),
            patch.object(routes, "_persist_answer_and_notify", persist),
        ):
            envelopes = []
            async for envelope in routes._dify_stream_generator(
                conversation_id="conversation-1",
                messages=[{"role": "user", "content": "问题"}],
                user="user-1",
                user_query="问题",
                source=0,
            ):
                if envelope["event"] == "message_end":
                    self.assertEqual(1, persist.await_count)
                envelopes.append(envelope)

        payloads = decode_events(envelopes)
        self.assertEqual(
            ["message", "agent_thought", "message", "message_end"],
            [payload["event"] for payload in payloads],
        )
        persist.assert_awaited_once()
        self.assertEqual("最终回答", persist.await_args.args[4])
        self.assertEqual(9, persist.await_args.args[5]["total_tokens"])

    async def test_streaming_persistence_failure_emits_error_not_success(self) -> None:
        persist = AsyncMock(side_effect=RuntimeError("password=do-not-expose"))
        with (
            patch.object(routes, "stream_agent_events", return_value=fake_event_stream()),
            patch.object(routes, "get_session_factory", return_value=FakeSessionFactory()),
            patch.object(routes, "_persist_answer_and_notify", persist),
        ):
            envelopes = [
                envelope
                async for envelope in routes._dify_stream_generator(
                    conversation_id="conversation-1",
                    messages=[{"role": "user", "content": "问题"}],
                    user="user-1",
                    user_query="问题",
                    source=0,
                )
            ]

        payloads = decode_events(envelopes)
        self.assertEqual("error", payloads[-1]["event"])
        self.assertNotIn("message_end", [payload["event"] for payload in payloads])
        self.assertNotIn("do-not-expose", payloads[-1]["message"])

    async def test_streaming_empty_completion_emits_error_terminal(self) -> None:
        persist = AsyncMock()
        with (
            patch.object(
                routes,
                "stream_agent_events",
                return_value=empty_completed_event_stream(),
            ),
            patch.object(routes, "get_session_factory", return_value=FakeSessionFactory()),
            patch.object(routes, "_persist_answer_and_notify", persist),
        ):
            payloads = decode_events(
                [
                    envelope
                    async for envelope in routes._dify_stream_generator(
                        conversation_id="conversation-empty",
                        messages=[{"role": "user", "content": "问题"}],
                        user="user-1",
                        user_query="问题",
                    )
                ],
            )

        self.assertEqual(["message", "error"], [payload["event"] for payload in payloads])
        persist.assert_not_awaited()

    async def test_blocking_uses_same_answer_and_usage(self) -> None:
        persist = AsyncMock()
        with (
            patch.object(routes, "stream_agent_events", return_value=fake_event_stream()),
            patch.object(routes, "get_session_factory", return_value=FakeSessionFactory()),
            patch.object(routes, "_persist_answer_and_notify", persist),
        ):
            result = await routes._run_blocking(
                conversation_id="conversation-1",
                messages=[{"role": "user", "content": "问题"}],
                user="user-1",
                user_query="问题",
            )

        self.assertEqual("最终回答", result["answer"])
        usage = result["metadata"]["usage"]
        self.assertEqual((7, 2, 9), (
            usage["prompt_tokens"],
            usage["completion_tokens"],
            usage["total_tokens"],
        ))
        persist.assert_awaited_once()

    async def test_completed_answer_saves_exactly_one_pair(self) -> None:
        save = AsyncMock()
        with patch.object(routes, "save_conversation", save):
            await routes._save_answer_to_db(
                object(),
                "conversation-1",
                "user-1",
                "问题",
                "回答",
            )

        save.assert_awaited_once()
        self.assertEqual(
            [
                {"role": "user", "content": "问题"},
                {"role": "assistant", "content": "回答"},
            ],
            save.await_args.kwargs["new_messages"],
        )

    async def test_error_run_does_not_save_partial_answer(self) -> None:
        persist = AsyncMock()
        with (
            patch.object(routes, "stream_agent_events", return_value=fake_event_stream(error=True)),
            patch.object(routes, "get_session_factory", return_value=FakeSessionFactory()),
            patch.object(routes, "_persist_answer_and_notify", persist),
        ):
            payloads = decode_events(
                [
                    envelope
                    async for envelope in routes._dify_stream_generator(
                        conversation_id="conversation-1",
                        messages=[{"role": "user", "content": "问题"}],
                        user="user-1",
                        user_query="问题",
                        source=0,
                    )
                ],
            )

        self.assertEqual("error", payloads[-1]["event"])
        persist.assert_not_awaited()

    async def test_inputs_are_controlled_context_without_history_duplication(self) -> None:
        request = DifyChatRequest(
            inputs={"province": "湖南省"},
            query="当前问题",
            response_mode="blocking",
            conversation_id="conversation-1",
            user="user-1",
        )
        history = [
            {"role": "user", "content": "历史问题"},
            {"role": "assistant", "content": "历史回答"},
        ]

        with patch.object(routes, "load_conversation", AsyncMock(return_value=history)):
            conversation_id, messages = await routes._build_messages_from_dify(
                request,
                object(),
            )

        self.assertEqual("conversation-1", conversation_id)
        self.assertEqual(
            '用户上下文信息：{"province": "湖南省"}',
            messages[0]["content"],
        )
        self.assertEqual(history, messages[1:3])
        self.assertEqual({"role": "user", "content": "当前问题"}, messages[-1])
        self.assertEqual(1, sum(message["content"] == "历史问题" for message in messages))

    async def test_blocking_releases_history_session_before_agent_run(self) -> None:
        exited = False

        class TrackingSessionContext(FakeSessionContext):
            async def __aexit__(self, exc_type, exc, traceback):
                nonlocal exited
                exited = True
                return False

        class TrackingSessionFactory:
            def __call__(self):
                return TrackingSessionContext()

        async def run_blocking(**kwargs):
            self.assertTrue(exited)
            self.assertNotIn("db", kwargs)
            return {"event": "message", "answer": "ok"}

        request = DifyChatRequest(
            query="当前问题",
            response_mode="blocking",
            user="user-1",
        )
        with (
            patch.object(routes, "get_session_factory", return_value=TrackingSessionFactory()),
            patch.object(routes, "load_conversation", AsyncMock(return_value=[])),
            patch.object(routes, "_run_blocking", AsyncMock(side_effect=run_blocking)),
        ):
            response = await routes.dify_chat_messages(object(), request)

        self.assertEqual(200, response.status_code)

    async def test_blocking_error_response_does_not_expose_exception(self) -> None:
        request = DifyChatRequest(
            query="当前问题",
            response_mode="blocking",
            user="user-1",
        )
        with (
            patch.object(routes, "get_session_factory", return_value=FakeSessionFactory()),
            patch.object(routes, "load_conversation", AsyncMock(return_value=[])),
            patch.object(
                routes,
                "_run_blocking",
                AsyncMock(side_effect=RuntimeError("password=do-not-expose")),
            ),
        ):
            response = await routes.dify_chat_messages(object(), request)

        payload = json.loads(response.body)
        self.assertEqual(500, response.status_code)
        self.assertEqual("服务器内部错误，请稍后重试", payload["message"])
        self.assertNotIn("do-not-expose", response.body.decode())

    async def test_blocking_empty_completion_uses_same_error_semantics(self) -> None:
        persist = AsyncMock()
        request = DifyChatRequest(
            query="当前问题",
            response_mode="blocking",
            user="user-1",
        )
        with (
            patch.object(routes, "get_session_factory", return_value=FakeSessionFactory()),
            patch.object(routes, "load_conversation", AsyncMock(return_value=[])),
            patch.object(
                routes,
                "stream_agent_events",
                return_value=empty_completed_event_stream(),
            ),
            patch.object(routes, "_persist_answer_and_notify", persist),
        ):
            response = await routes.dify_chat_messages(object(), request)

        payload = json.loads(response.body)
        self.assertEqual(500, response.status_code)
        self.assertEqual("模型服务暂时不可用，请稍后重试", payload["message"])
        persist.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
