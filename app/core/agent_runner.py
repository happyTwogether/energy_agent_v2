"""
Agent 运行时核心模块。

实现 ReAct Loop 骨架，协调 LLM 推理与工具调用，
以 AsyncGenerator 方式逐步产出 StreamEvent，驱动 SSE 流式输出。
"""

import asyncio
import json
from typing import Any, AsyncGenerator

from app.core.config import get_settings
from app.core.logging import get_logger
from app.models.schemas import StreamEvent
from app.services.llm_client import LLMError, get_llm_client
from app.tools.registry import tool_registry

logger = get_logger("agent_runner")


async def _execute_tool(tool_name: str, tool_args: dict[str, Any]) -> dict[str, Any]:
    """执行单个工具调用。

    Args:
        tool_name: 工具名称。
        tool_args: 工具参数字典。

    Returns:
        工具执行结果字典。
    """
    try:
        func = tool_registry.get_tool(tool_name)
        result = await func(**tool_args)
        return {"tool": tool_name, "result": result}
    except Exception as exc:
        logger.error("工具执行失败: tool=%s, error=%s", tool_name, exc, exc_info=True)
        return {"tool": tool_name, "error": str(exc)}


async def run_agent_stream(messages: list[dict[str, Any]]) -> AsyncGenerator[StreamEvent, None]:
    """运行 ReAct Loop Agent，以流式方式产出事件。

    ReAct Loop 流程：
    1. 验证并确保 messages 包含 system_prompt
    2. 循环调用 LLM，判断是否需要工具调用
    3. 若有 tool_calls，并发执行所有工具，将结果追加到 messages
    4. 若 LLM 返回最终回答，输出 final_answer 事件并终止
    5. 达到最大步数时强制终止

    Args:
        messages: 对话消息列表，OpenAI 格式。必须包含 system 消息或自动添加。

    Yields:
        StreamEvent: 每个执行步骤对应的流式事件。
    """
    settings = get_settings()
    max_steps: int = settings.agent_max_steps

    # 确保有 system prompt
    if not messages or messages[0].get("role") != "system":
        messages = [
            {"role": "system", "content": settings.agent_system_prompt},
            *messages,
        ]
    
    steps: int = 0
    tools: list[dict[str, Any]] = tool_registry.get_tools_for_llm()

    try:
        while steps < max_steps:
            steps += 1
            logger.info("Agent 第 %d 步开始", steps)

            yield StreamEvent(
                event_type="agent_step",
                data={"step": steps, "max_steps": max_steps},
            )

            try:
                response: dict[str, Any] = await get_llm_client().chat(
                    messages=messages,
                    tools=tools if tools else None,
                )
            except LLMError as exc:
                logger.error("LLM 调用失败（第 %d 步）: %s", steps, exc)
                yield StreamEvent(event_type="error", data=str(exc))
                return

            choice: dict[str, Any] = response["choices"][0]
            finish_reason: str | None = choice.get("finish_reason")
            assistant_message: dict[str, Any] = choice["message"]
            tool_calls: list[dict[str, Any]] | None = assistant_message.get("tool_calls")

            messages.append(assistant_message)

            if finish_reason == "stop" and not tool_calls:
                content: str = assistant_message.get("content", "")
                logger.info("Agent 最终回答（第 %d 步）: %s", steps, content[:200])
                yield StreamEvent(event_type="final_answer", data=content)
                return

            if tool_calls:
                parsed_calls: list[tuple[str, dict[str, Any]]] = []

                for tc in tool_calls:
                    func_info: dict[str, Any] = tc["function"]
                    tool_name: str = func_info["name"]
                    try:
                        tool_args: dict[str, Any] = json.loads(func_info.get("arguments", "{}"))
                    except json.JSONDecodeError:
                        tool_args = {}

                    call_info: dict[str, Any] = {"tool": tool_name, "args": tool_args}
                    parsed_calls.append((tool_name, tool_args))

                    yield StreamEvent(event_type="tool_call", data=call_info)

                results: list[dict[str, Any]] = []
                async with asyncio.TaskGroup() as tg:
                    tasks = [
                        tg.create_task(_execute_tool(name, args))
                        for name, args in parsed_calls
                    ]
                results = [t.result() for t in tasks]

                for idx, result in enumerate(results):
                    yield StreamEvent(event_type="tool_result", data=result)

                    tc_id: str = tool_calls[idx].get("id", f"call_{idx}")
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "content": json.dumps(result, ensure_ascii=False),
                    })

                logger.info("第 %d 步工具调用完成，共 %d 个工具", steps, len(results))

                if steps == max_steps:
                    logger.warning("达到最大步数 %d，强制终止", max_steps)
                    try:
                        final_response: dict[str, Any] = await get_llm_client().chat(
                            messages=messages,
                            tools=None,
                        )
                        final_content: str = final_response["choices"][0]["message"].get("content", "")
                        yield StreamEvent(event_type="final_answer", data=final_content)
                    except LLMError as exc:
                        logger.error("最终回答 LLM 调用失败: %s", exc)
                        yield StreamEvent(event_type="final_answer", data="已达到最大推理步数，请重新提问。")
                    return

                continue

            content = assistant_message.get("content", "")
            if content:
                logger.info("Agent 返回文本回答（第 %d 步）", steps)
                yield StreamEvent(event_type="final_answer", data=content)
                return

        logger.warning("Agent 循环结束，未产生最终回答")
        yield StreamEvent(event_type="final_answer", data="已达到最大推理步数，请重新提问。")

    except Exception as exc:
        logger.error("Agent 运行异常: %s", exc, exc_info=True)
        yield StreamEvent(event_type="error", data=f"Agent 运行异常: {exc}")
