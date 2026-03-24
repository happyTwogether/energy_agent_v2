"""
Agent 运行时核心模块。

实现 ReAct Loop 骨架，协调 LLM 推理与工具调用，
以 AsyncGenerator 方式逐步产出 StreamEvent，驱动 SSE 流式输出。
"""

import asyncio
import inspect
import json
from datetime import datetime, timedelta
from typing import Any, AsyncGenerator, AsyncIterator

from app.core.config import get_settings
from app.core.json_utils import dumps_decimal
from app.core.logging import get_logger
from app.models.schemas import StreamEvent
from app.services.database import get_session_factory
from app.services.llm_client import LLMError, get_llm_client
from app.tools.registry import tool_registry

logger = get_logger("agent_runner")

_IDLE_TIMEOUT_SEC = 30  # 连续 30 秒无新 chunk 才认为流挂起


async def _stream_with_idle_timeout(
    agen: AsyncIterator[dict[str, Any]],
) -> AsyncGenerator[dict[str, Any], None]:
    """对流式生成器施加逐 chunk 空闲超时。

    仅当两个相邻 chunk 间隔超过 _IDLE_TIMEOUT_SEC 才抛出 asyncio.TimeoutError，
    活跃流无论总时长多长都不会被中断。
    """
    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize=1)
    done_event = asyncio.Event()
    exc_holder: list[BaseException] = []

    async def _producer() -> None:
        try:
            async for chunk in agen:
                await queue.put(chunk)
        except asyncio.CancelledError:
            raise
        except BaseException as e:
            exc_holder.append(e)
        finally:
            done_event.set()
            await queue.put(None)

    producer_task = asyncio.create_task(_producer())
    try:
        while True:
            try:
                chunk = await asyncio.wait_for(queue.get(), timeout=_IDLE_TIMEOUT_SEC)
            except asyncio.TimeoutError:
                raise

            if chunk is None:
                break
            yield chunk

        if exc_holder:
            raise exc_holder[0]
    finally:
        producer_task.cancel()
        try:
            await producer_task
        except asyncio.CancelledError:
            pass
        await agen.aclose()


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
- export_excel: 是否导出Excel。触发条件：用户明确要求"导出Excel"、"下载数据"；或查询结果超过50条；或使用 lte_cell_detail / nr_cell_detail 模板时。设为 true 可生成Excel下载链接。

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
参数提取规则（禁止追问）：
- dist_name: 提取地市。若用户未明确提及特定地市，直接默认传 "全网"，禁止追问。
- prod_name: 提取厂家。若用户未明确提及特定厂家，直接默认传 "全网"，禁止追问。
- target_date: 提取目标诊断日期 (YYYY-MM-DD)，未提及传昨天。
- export_excel: 是否导出Excel。触发条件：用户明确要求"导出Excel"、"下载数据"；或检测结果数据量超过50条。设为 true 可生成Excel下载链接。
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
3. 否则需要用户提供地市或厂家进行批量查询

必须提取：
- cgi: CGI过滤（可选，优先级最高，格式如 460-00-12345-678）
- cell_name: 小区名称过滤（可选，如"长沙芙蓉区芙蓉广场-HHH-1"）
- dist_name: 地市名称（可选，当未提供 cgi/cell_name 时必填）
- prod_name: 厂家名称（可选，当未提供 cgi/cell_name 时必填）
- check_date: 核查日期（可选，用户未提及时不传，让系统自动计算）
- export_excel: 是否导出Excel。触发条件：用户明确要求"导出Excel"、"下载数据"；或核查结果数据量超过50条。设为 true 可生成Excel下载链接。

返回规则：
1. 工具返回 report_content 直接输出；返回异常记录列表时，总结异常数量和主要问题类型。
2. 工具返回 download_url时 ，不直接输出report_content，进行总结，提供下载链接并告知用户可以点击下载完整报告。


### analyze_single_cell_energy（单小区节电深度分析）
触发条件：用户询问具体某个小区的"节电分析"、"休眠扩展"、"节能收缩"、"高负荷"等详细情况。
必须提取参数：
- cgi: 小区全球标识 (必须提取或通过小区名转换，格式 460-00-xxx-xxx)
- analysis_target: 根据用户意图选择 "all"(全面分析), "expansion"(只问扩展/低业务), "constriction"(只问收缩/负面影响), "load"(只问负荷)。
返回规则：工具返回 report_content 后，直接原样输出，不要自行删减。

### analyze_batch_cells_energy（批量小区节电诊断）
触发条件：用户输入地市、区县、厂家，要求进行"批量核查"、"批量分析"。
必须提取参数：
- dist_name: 区县名称 (必须提取！若用户只说了地市没说区县，必须追问具体区县，否则数据量过大导致崩溃)
- prod_name: 厂家名称 (可选，如华为、中兴。若未提及传 "全网")
返回规则：工具返回 report_content 后，直接原样输出，绝对不要自行删减或总结，确保包含 Excel 下载链接。

## 回答规则
1. 工具返回 success=False 且包含"暂未开放" → 告知建设中；其他错误 → 告知原因，绝不编造。
2. 工具返回 report_content → 直接输出，不二次加工。
3. 工具返回 data 列表 → 提取关键数据总结，不返回原始 JSON。
4. 涉及4G和5G的非对比查询 → 分两次调 summary。对比查询 → 调 freeform。
5. 工具返回包含 download_url → 必须用 Markdown 链接格式输出，如 [点击下载 Excel 报告](download_url)，禁止纯文本输出 URL。
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

            collected_content = ""
            collected_tool_calls: dict[int, dict] = {}
            finish_reason: str | None = None
            _stream_fallback_needed = False

            try:
                async for chunk in _stream_with_idle_timeout(
                    get_llm_client().stream_chat(
                        messages=messages,
                        tools=tools or None,
                    )
                ):
                        choices = chunk.get("choices") or []
                        if not choices:
                            continue
                        choice = choices[0]
                        finish_reason = choice.get("finish_reason") or finish_reason
                        delta = choice.get("delta", {})

                        if delta.get("content"):
                            token_text = delta["content"]
                            collected_content += token_text
                            yield StreamEvent(event_type="token", data=token_text)

                        for tc_delta in (delta.get("tool_calls") or []):
                            idx = tc_delta["index"]
                            if idx not in collected_tool_calls:
                                collected_tool_calls[idx] = {
                                    "id": "",
                                    "type": "function",
                                    "function": {"name": "", "arguments": ""},
                                }
                            if tc_delta.get("id"):
                                collected_tool_calls[idx]["id"] = tc_delta["id"]
                            fn = tc_delta.get("function") or {}
                            if fn.get("name"):
                                collected_tool_calls[idx]["function"]["name"] = fn["name"]
                            if fn.get("arguments"):
                                collected_tool_calls[idx]["function"]["arguments"] += fn["arguments"]
            except asyncio.TimeoutError:
                # 30秒无新 chunk → DeepSeek 流式 API 挂起，降级为非流式
                logger.warning("流式调用挂起（第 %d 步，已等待30秒），降级为非流式调用", steps)
                _stream_fallback_needed = True
            except LLMError as exc:
                # 流式中途连接断开 → 同样降级，比报错更可靠
                logger.warning("流式调用中断（第 %d 步）: %s，降级为非流式调用", steps, exc)
                _stream_fallback_needed = True

            if _stream_fallback_needed:
                try:
                    fallback_resp = await get_llm_client().chat(
                        messages=messages, tools=tools or None
                    )
                    fb_choice = (fallback_resp.get("choices") or [{}])[0]
                    finish_reason = fb_choice.get("finish_reason")
                    fb_msg = fb_choice.get("message") or {}
                    collected_content = fb_msg.get("content") or collected_content
                    fb_tool_calls = fb_msg.get("tool_calls") or []
                    if fb_tool_calls:
                        collected_tool_calls = {i: tc for i, tc in enumerate(fb_tool_calls)}
                    logger.info("降级非流式调用成功（第 %d 步）: finish=%s", steps, finish_reason)
                except LLMError as retry_exc:
                    logger.error("降级非流式调用也失败（第 %d 步）: %s", steps, retry_exc)
                    yield StreamEvent(event_type="error", data=str(retry_exc))
                    return

            tool_calls: list[dict[str, Any]] | None = (
                list(collected_tool_calls.values()) if collected_tool_calls else None
            )
            assistant_message: dict[str, Any] = {
                "role": "assistant",
                "content": collected_content or None,
            }
            if tool_calls:
                assistant_message["tool_calls"] = tool_calls

            messages.append(assistant_message)

            if finish_reason == "stop" and not tool_calls:
                logger.info("Agent 最终回答（第 %d 步）: %s", steps, collected_content[:200])
                yield StreamEvent(event_type="final_answer", data=collected_content)
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
                    final_collected = ""
                    _max_step_fallback = False
                    try:
                        async for chunk in _stream_with_idle_timeout(
                            get_llm_client().stream_chat(
                                messages=messages,
                                tools=None,
                            )
                        ):
                            choices = chunk.get("choices") or []
                            if not choices:
                                continue
                            delta = choices[0].get("delta", {})
                            if delta.get("content"):
                                token_text = delta["content"]
                                final_collected += token_text
                                yield StreamEvent(event_type="token", data=token_text)
                    except (asyncio.TimeoutError, LLMError) as exc:
                        logger.warning("最大步数强制汇总流式失败: %s，降级为非流式", exc)
                        _max_step_fallback = True
                    if _max_step_fallback:
                        try:
                            fb = await get_llm_client().chat(messages=messages, tools=None)
                            fb_content = ((fb.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
                            final_collected = fb_content or final_collected
                        except LLMError:
                            pass
                    yield StreamEvent(event_type="final_answer", data=final_collected or "已达到最大推理步数，请重新提问。")
                    return

                continue

            if collected_content:
                logger.info("Agent 返回文本回答（第 %d 步）", steps)
                yield StreamEvent(event_type="final_answer", data=collected_content)
                return

        logger.warning("Agent 循环结束，未产生最终回答")
        yield StreamEvent(event_type="final_answer", data="已达到最大推理步数，请重新提问。")

    except Exception as exc:
        logger.error("Agent 运行异常: %s", exc, exc_info=True)
        yield StreamEvent(event_type="error", data=f"Agent 运行异常: {exc}")
