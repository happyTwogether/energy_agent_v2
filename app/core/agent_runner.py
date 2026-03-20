"""
Agent 运行时核心模块。

实现 ReAct Loop 骨架，协调 LLM 推理与工具调用，
以 AsyncGenerator 方式逐步产出 StreamEvent，驱动 SSE 流式输出。
"""

import asyncio
import inspect
import json
from datetime import datetime, timedelta
from typing import Any, AsyncGenerator

from app.core.config import get_settings
from app.core.json_utils import dumps_decimal
from app.core.logging import get_logger
from app.models.schemas import StreamEvent
from app.services.database import get_session_factory
from app.services.llm_client import LLMError, get_llm_client
from app.tools.registry import tool_registry

logger = get_logger("agent_runner")


# [APPEND TO SYSTEM_PROMPT_TEMPLATE]
# 动态追加到 System Prompt 的节能分析助手指引
ENERGY_SAVING_PROMPT_APPENDIX = """
你是一个4G/5G网络节能分析助手，帮助用户查询网络指标、生成报表和分析节电潜力。
今天日期：{current_date}，昨天日期：{yesterday}

## 可用工具

### query_metric（指标查询）
查询4G/5G节能相关指标数据。
调用前提取参数：
- template_key（必填）: lte_summary / nr_summary / lte_energy_saving / nr_energy_saving / lte_cell_detail / nr_cell_detail。
  * 若用户给特定cgi、只查个别字段、对比4/5G、或求TOP排序，传 freeform。
- dist_name: 识别湖南地市(如长沙市，统一补市)，未提及传"全网"。
- prod_name: 华为/中兴/爱立信/诺基亚，未提及传"全网"。
- date_start / date_end: 未提及传 {yesterday}。"今天"传 {current_date}。推算"前天"、"上周X"、"最近N天"。单日查询两者相同。
- cgi: 仅 freeform 传。格式 460-00-基站号-小区号。
- metric_desc: freeform 时必填，一句话描述需求。

### query_report（报表生成）
生成完整节耗电分析报告(Markdown)。
触发条件："生成报表"、"出报告"、"报表查询"等。

【参数提取与默认兜底规则（绝对禁止过度追问）】
- dist_name: 提取地市。若用户未明确提及特定地市，直接默认传 "全网"，禁止追问。
- prod_name: 提取厂家。若用户未明确提及特定厂家，直接默认传 "全网"，禁止追问。
- date_start / date_end: 
  * 若用户只提供了一个具体日期（如"2025年12月14日"、"昨天"），则 date_start 和 date_end 均传该同一日期，**绝对禁止追问结束日期**。
  * 若用户完全未提及时间，则两者均默认传 {yesterday}。

(注：底层工具会自动处理7天历史基线拉取用于异常诊断，Agent 只需严格传递用户目标查询的起始和结束日期即可，不要让用户操心回溯逻辑。)
（使用本工具时需要先把工具返回的内容完整展示给用户。）

### analyze_energy_saving（节电分析）
触发条件："节电分析"、"节电潜力"。当前直接告知用户建设中。

### query_anomaly（异常指标诊断）
诊断特定日期是否有核心指标劣化超过10%。
触发条件：用户询问"异常"、"劣化"、"大幅下降"、"波动"时调用。
必须提取：dist_name, prod_name, target_date (未提及传昨天)。
返回规则：不要罗列全量数据，只用自然语言严重警告劣化的指标及其降幅；若无异常，告知运行平稳。

### query_energy_param_check（节能参数核查）
查询4G/5G小区节能参数配置核查结果，检查参数是否符合推荐值。
触发条件：用户提及"节能参数"、"参数核查"、"配置检查"、"参数合规"时调用。

表名（系统自动处理，无需Agent干预）：
- {{schema}}.eng_check_result_YYYYMMDD

日期自动计算规则（系统自动处理）：
- 用户未传 check_date 时:
  - 当前时间 < 15:00: 使用昨天日期
  - 当前时间 >= 15:00: 使用今天日期

查询优先级（至少提供一种查询条件）：
1. 如果用户提供 CGI，直接用 CGI 查询（最精确，优先使用）
2. 如果用户提供小区名称，用小区名称查询
3. 否则需要用户提供区县 + 厂商进行批量查询

必须提取：
- network_type: 网络类型，可选 "4g"/"5g"/"all"（未提及默认传 "all"）
- cgi: CGI过滤（可选，优先级最高，格式如 460-00-12345-678）
- cell_name: 小区名称过滤（可选，如"长沙芙蓉区芙蓉广场-HHH-1"）
- dist_name: 区县名称（可选，当未提供 cgi/cell_name 时必填）
- prod_name: 厂商名称（可选，当未提供 cgi/cell_name 时必填）
- check_date: 核查日期（可选，用户未提及时不传，让系统自动计算）

返回规则：工具返回 report_content 直接输出；返回异常记录列表时，总结异常数量和主要问题类型。

## 回答规则
1. 工具返回 success=False 且包含"暂未开放" → 告知建设中；其他错误 → 告知原因，绝不编造。
2. 工具返回 report_content → 直接输出，不二次加工。
3. 工具返回 data 列表 → 提取关键数据总结，不返回原始 JSON。
4. 涉及4G和5G的非对比查询 → 分两次调 summary。对比查询 → 调 freeform。
"""


async def _execute_tool(
    tool_name: str,
    tool_args: dict[str, Any],
    db: Any | None = None,
) -> dict[str, Any]:
    """执行单个工具调用，自动注入 db 上下文。

    Args:
        tool_name: 工具名称。
        tool_args: 工具参数字典（LLM 提供）。
        db: 数据库会话，若工具需要 db 参数则自动注入。

    Returns:
        工具执行结果字典。
    """
    try:
        func = tool_registry.get_tool(tool_name)

        # 检查工具函数签名，判断是否需要 db 参数
        sig = inspect.signature(func)
        params = list(sig.parameters.keys())

        # 如果工具需要 db 参数，但 LLM 没提供，则注入
        if "db" in params and "db" not in tool_args:
            if db is not None:
                tool_args = {**tool_args, "db": db}
            else:
                return {
                    "tool": tool_name,
                    "error": "工具需要数据库上下文，但未提供 db 参数",
                }

        # 执行工具调用
        if inspect.iscoroutinefunction(func):
            result = await func(**tool_args)
        else:
            result = func(**tool_args)

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

    # 计算动态日期
    current_date = datetime.now().strftime("%Y-%m-%d")
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    # 构建增强版 system prompt（动态注入时间 + 追加节能分析指引）
    enhanced_system_prompt = settings.agent_system_prompt + ENERGY_SAVING_PROMPT_APPENDIX.format(
        current_date=current_date,
        yesterday=yesterday,
    )

    # 确保有 system prompt
    if not messages or messages[0].get("role") != "system":
        messages = [
            {"role": "system", "content": enhanced_system_prompt},
            *messages,
        ]
    else:
        # 如果已有 system prompt，追加节能分析指引
        original_content = messages[0]["content"]
        messages[0]["content"] = original_content + ENERGY_SAVING_PROMPT_APPENDIX.format(
            current_date=current_date,
            yesterday=yesterday,
        )
    
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

                # 【核心补丁】创建数据库会话并注入到需要 db 的工具
                results: list[dict[str, Any]] = []
                session_factory = get_session_factory()
                async with session_factory() as db:
                    async with asyncio.TaskGroup() as tg:
                        tasks = [
                            tg.create_task(_execute_tool(name, args, db=db))
                            for name, args in parsed_calls
                        ]
                    results = [t.result() for t in tasks]

                for idx, result in enumerate(results):
                    yield StreamEvent(event_type="tool_result", data=result)

                    tc_id: str = tool_calls[idx].get("id", f"call_{idx}")
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "content": dumps_decimal(result, ensure_ascii=False),
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
