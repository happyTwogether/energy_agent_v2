#!/usr/bin/env python3
"""分层诊断 OpenAI-compatible 模型请求。

使用示例：
    LLM_API_KEY='...' \
    LLM_BASE_URL='http://10.159.228.81:3000/scmpApi/v1' \
    DEFAULT_MODEL='qwen-dense-30b-128k' \
    .venv/bin/python scripts/diagnose_llm.py --timeout 30

请在与生产服务相同的容器或主机中运行，不要把 API Key 写入本文件。
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import socket
import ssl
import sys
import time
from typing import Awaitable, Callable
from urllib.parse import urlsplit

import httpx
import openai


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agentscope.message import SystemMsg, TextBlock, ToolCallBlock, UserMsg
from agentscope.tool import ToolChoice

from app.agent.model import build_chat_model
from app.agent.toolkit import build_toolkit
from app.core.config import Settings
from app.prompts.energy_saving import AGENT_EXECUTION_PROMPT


PROXY_ENV_NAMES = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
)


@dataclass(slots=True)
class CaseResult:
    """单个诊断阶段的结果。"""

    name: str
    ok: bool
    elapsed_seconds: float
    details: list[str] = field(default_factory=list)
    error: str = ""


def parse_args() -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(
        description="对比原始 HTTP 与项目 AgentScope 模型请求。",
    )
    parser.add_argument("--base-url", help="覆盖 LLM_BASE_URL")
    parser.add_argument("--model", help="覆盖 DEFAULT_MODEL")
    parser.add_argument("--prompt", default="长沙市能耗报表")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument(
        "--quick",
        action="store_true",
        help="只测 TCP/TLS、原始非流式和 AgentScope 非流式",
    )
    return parser.parse_args()


def load_settings(args: argparse.Namespace) -> Settings:
    """读取项目 .env/进程环境，再应用命令行覆盖。"""
    settings = Settings(_env_file=PROJECT_ROOT / ".env")
    updates = {
        "llm_timeout_seconds": args.timeout,
    }
    if args.base_url:
        updates["llm_base_url"] = args.base_url
    if args.model:
        updates["default_model"] = args.model
    return settings.model_copy(update=updates)


def chat_completions_url(base_url: str) -> str:
    """将 SDK base URL 转为原始 HTTP 诊断端点。"""
    normalized = base_url.strip().rstrip("/")
    suffix = "/chat/completions"
    return normalized if normalized.endswith(suffix) else normalized + suffix


def exception_chain(exc: BaseException) -> str:
    """输出异常类型和完整 cause 链。"""
    parts: list[str] = []
    current: BaseException | None = exc
    while current is not None:
        message = str(current).strip() or "<no message>"
        parts.append(f"{type(current).__name__}: {message}")
        current = current.__cause__ or current.__context__
    return " <- ".join(parts)


async def run_case(
    name: str,
    operation: Callable[[], Awaitable[list[str]]],
    timeout: float,
) -> CaseResult:
    """在硬超时边界内执行一个诊断阶段。"""
    started = time.perf_counter()
    try:
        details = await asyncio.wait_for(operation(), timeout=timeout + 5)
        elapsed = time.perf_counter() - started
        return CaseResult(name, True, elapsed, details=details)
    except Exception as exc:
        elapsed = time.perf_counter() - started
        return CaseResult(name, False, elapsed, error=exception_chain(exc))


async def check_transport(endpoint: str, timeout: float) -> list[str]:
    """验证 TCP，HTTPS 时同时验证 TLS 握手。"""
    parsed = urlsplit(endpoint)
    host = parsed.hostname
    if not host:
        raise ValueError(f"无法从 URL 解析主机: {endpoint}")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    ssl_context = ssl.create_default_context() if parsed.scheme == "https" else None
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(host, port, ssl=ssl_context),
        timeout=timeout,
    )
    del reader
    peer = writer.get_extra_info("peername")
    writer.close()
    await writer.wait_closed()
    return [f"peer={peer}", f"scheme={parsed.scheme}"]


def raw_payload(model: str, prompt: str, stream: bool) -> dict[str, object]:
    """构造与用户 curl 等价的最小请求。"""
    return {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": stream,
    }


def response_preview(payload: object) -> str:
    """提取安全、有界的响应摘要。"""
    if not isinstance(payload, dict):
        return str(payload)[:160]
    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        choice = choices[0]
        if isinstance(choice, dict):
            message = choice.get("message") or choice.get("delta")
            if isinstance(message, dict):
                content = message.get("content")
                if content:
                    return str(content)[:160]
    return json.dumps(payload, ensure_ascii=False)[:160]


def validate_completion_payload(payload: object) -> None:
    """确认非流式响应至少包含一个 choice。"""
    if not isinstance(payload, dict):
        raise RuntimeError("响应不是 JSON object")
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise RuntimeError(f"响应缺少 choices: {response_preview(payload)}")


async def check_raw_http(
    settings: Settings,
    prompt: str,
    *,
    stream: bool,
    trust_env: bool,
) -> list[str]:
    """绕过 SDK，直接请求 OpenAI-compatible HTTP 端点。"""
    endpoint = chat_completions_url(settings.llm_base_url)
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {settings.llm_api_key.get_secret_value()}",
        "Content-Type": "application/json",
    }
    payload = raw_payload(settings.default_model, prompt, stream)
    timeout = httpx.Timeout(settings.llm_timeout_seconds)
    async with httpx.AsyncClient(timeout=timeout, trust_env=trust_env) as client:
        if stream:
            return await read_raw_stream(client, endpoint, headers, payload)
        response = await client.post(endpoint, headers=headers, json=payload)
    body = response.text
    if not response.is_success:
        raise RuntimeError(f"HTTP {response.status_code}: {body[:500]}")
    response_payload = response.json()
    validate_completion_payload(response_payload)
    return [
        f"http_status={response.status_code}",
        f"preview={response_preview(response_payload)}",
    ]


async def read_raw_stream(
    client: httpx.AsyncClient,
    endpoint: str,
    headers: dict[str, str],
    payload: dict[str, object],
) -> list[str]:
    """消费 SSE 流并记录首个 data 事件的时间。"""
    started = time.perf_counter()
    first_event_ms: float | None = None
    event_count = 0
    preview = ""
    async with client.stream("POST", endpoint, headers=headers, json=payload) as response:
        if not response.is_success:
            body = (await response.aread()).decode(errors="replace")
            raise RuntimeError(f"HTTP {response.status_code}: {body[:500]}")
        async for line in response.aiter_lines():
            if not line.startswith("data:"):
                continue
            if first_event_ms is None:
                first_event_ms = (time.perf_counter() - started) * 1000
            event_count += 1
            data = line[5:].strip()
            if data and data != "[DONE]" and not preview:
                preview = response_preview(json.loads(data))
    if event_count == 0:
        raise RuntimeError("HTTP 成功但未收到任何 SSE data 事件")
    return [
        f"http_status={response.status_code}",
        f"first_event_ms={first_event_ms}",
        f"sse_events={event_count}",
        f"preview={preview[:160]}",
    ]


def collect_blocks(content: list[object]) -> tuple[str, list[str]]:
    """从 AgentScope 响应中提取文本和工具名。"""
    text = "".join(
        block.text for block in content if isinstance(block, TextBlock)
    )
    tools = [
        block.name for block in content if isinstance(block, ToolCallBlock)
    ]
    return text, tools


async def check_agentscope(
    settings: Settings,
    prompt: str,
    *,
    stream: bool,
    app_like: bool,
) -> list[str]:
    """通过项目实际 model factory 请求模型。"""
    tuned = settings
    if not app_like:
        tuned = settings.model_copy(update={"llm_max_tokens": 32})
    model = build_chat_model(tuned, stream=stream)
    # 生产代码还会受 OpenAI SDK 内层默认重试影响；诊断时必须关闭，
    # 否则单个阶段的耗时无法对应到一次网络请求。
    model.max_retries = 0
    model.client_kwargs["max_retries"] = 0
    messages = [UserMsg(name="user", content=prompt)]
    call_kwargs: dict[str, object] = {}
    if app_like:
        messages.insert(0, SystemMsg(name="system", content=AGENT_EXECUTION_PROMPT))
        toolkit = build_toolkit()
        call_kwargs["tools"] = await toolkit.get_tool_schemas()
        call_kwargs["tool_choice"] = ToolChoice(mode="query_report")
    started = time.perf_counter()
    response = await model(messages=messages, **call_kwargs)
    if not stream:
        text, tools = collect_blocks(response.content)
        if not text.strip() and not tools:
            raise RuntimeError("AgentScope 返回了空响应")
        return [f"preview={text[:160]}", f"tool_calls={tools}"]
    chunks = []
    first_chunk_ms: float | None = None
    async for chunk in response:
        if first_chunk_ms is None:
            first_chunk_ms = (time.perf_counter() - started) * 1000
        chunks.extend(chunk.content)
    text, tools = collect_blocks(chunks)
    if app_like and "query_report" not in tools:
        raise RuntimeError(f"未按要求返回 query_report 工具调用: {tools}")
    if not app_like and not text.strip():
        raise RuntimeError("AgentScope 流式响应没有文本")
    return [
        f"first_chunk_ms={first_chunk_ms}",
        f"preview={text[:160]}",
        f"tool_calls={tools}",
    ]


def print_configuration(settings: Settings) -> None:
    """输出不包含凭证的有效配置。"""
    proxy_names = [name for name in PROXY_ENV_NAMES if os.getenv(name)]
    print("有效配置（API Key 不显示）")
    print(f"  base_url: {settings.llm_base_url}")
    print(f"  endpoint: {chat_completions_url(settings.llm_base_url)}")
    print(f"  model: {settings.default_model}")
    print(f"  timeout: {settings.llm_timeout_seconds}s")
    print(f"  app_agentscope_retries: {settings.llm_max_retries}")
    print(f"  openai_sdk_default_retries: {openai.DEFAULT_MAX_RETRIES}")
    print("  diagnostic_retries: agentscope=0, openai_sdk=0")
    print(f"  app_max_tokens: {settings.llm_max_tokens}")
    print(f"  proxy_env: {proxy_names or 'none'}")
    if settings.llm_base_url.rstrip("/").endswith("/chat/completions"):
        print("  WARNING: LLM_BASE_URL 应是 SDK base，不应包含 /chat/completions")


def print_result(result: CaseResult) -> None:
    """打印单个阶段的可复制结果。"""
    status = "PASS" if result.ok else "FAIL"
    print(f"[{status}] {result.name} ({result.elapsed_seconds:.3f}s)")
    for detail in result.details:
        print(f"       {detail}")
    if result.error:
        print(f"       {result.error}")


def diagnose(results: list[CaseResult]) -> str:
    """根据分层结果给出首要排查方向。"""
    states = {result.name: result.ok for result in results}
    if not states.get("01_tcp_tls", False):
        return "TCP/TLS 失败：优先查网络路由、端口、防火墙或 TLS 证书。"
    if states.get("03_raw_no_proxy") and not states.get("02_raw_non_stream"):
        return "绕过环境代理后成功：问题在 HTTP(S)_PROXY/NO_PROXY 配置。"
    if not states.get("02_raw_non_stream", False):
        return (
            "最小原始 HTTP 也失败：问题在网关/模型服务或当前运行环境，"
            "不在 AgentScope 业务代码。"
        )
    if not states.get("05_agentscope_non_stream", False):
        return (
            "原始 HTTP 成功而 AgentScope 失败：聚焦 SDK 参数兼容、"
            "代理继承和客户端超时。"
        )
    if "06_agentscope_stream" in states and not states["06_agentscope_stream"]:
        return (
            "非流式成功但流式失败：聚焦 SSE/反向代理缓冲和 "
            "stream_options 兼容。"
        )
    if "07_app_like_report" in states and not states["07_app_like_report"]:
        return (
            "基础调用成功但报表请求失败：聚焦工具 Schema、"
            "tool_choice、长系统提示词或上下文限制。"
        )
    return (
        "全部通过：模型当前可用；继续对比生产容器的有效环境变量、"
        "并发压力和失败时间窗。"
    )


async def async_main(args: argparse.Namespace) -> int:
    """执行分层诊断。"""
    settings = load_settings(args)
    if not settings.llm_api_key.get_secret_value().strip():
        raise SystemExit("缺少 LLM_API_KEY，请通过环境变量安全注入。")
    if not settings.llm_base_url.strip():
        raise SystemExit("缺少 LLM_BASE_URL。")
    print_configuration(settings)
    endpoint = chat_completions_url(settings.llm_base_url)
    cases = build_cases(settings, args, endpoint)
    results: list[CaseResult] = []
    for name, operation in cases:
        result = await run_case(name, operation, settings.llm_timeout_seconds)
        results.append(result)
        print_result(result)
    print(f"\n诊断结论：{diagnose(results)}")
    required_results = [
        result for result in results if result.name != "03_raw_no_proxy"
    ]
    return 0 if all(result.ok for result in required_results) else 1


def build_cases(
    settings: Settings,
    args: argparse.Namespace,
    endpoint: str,
) -> list[tuple[str, Callable[[], Awaitable[list[str]]]]]:
    """按从网络到项目请求的顺序构造诊断阶段。"""
    cases = [
        ("01_tcp_tls", lambda: check_transport(endpoint, args.timeout)),
        (
            "02_raw_non_stream",
            lambda: check_raw_http(settings, args.prompt, stream=False, trust_env=True),
        ),
    ]
    if any(os.getenv(name) for name in PROXY_ENV_NAMES):
        cases.append(
            (
                "03_raw_no_proxy",
                lambda: check_raw_http(settings, args.prompt, stream=False, trust_env=False),
            ),
        )
    if not args.quick:
        cases.append(
            (
                "04_raw_stream",
                lambda: check_raw_http(settings, args.prompt, stream=True, trust_env=True),
            ),
        )
    cases.append(
        (
            "05_agentscope_non_stream",
            lambda: check_agentscope(settings, args.prompt, stream=False, app_like=False),
        ),
    )
    if not args.quick:
        cases.extend(
            [
                (
                    "06_agentscope_stream",
                    lambda: check_agentscope(settings, args.prompt, stream=True, app_like=False),
                ),
                (
                    "07_app_like_report",
                    lambda: check_agentscope(settings, args.prompt, stream=True, app_like=True),
                ),
            ],
        )
    return cases


def main() -> int:
    """命令行入口。"""
    try:
        return asyncio.run(async_main(parse_args()))
    except KeyboardInterrupt:
        print("\n已取消诊断。", file=sys.stderr)
        return 130
    except (socket.gaierror, ValueError) as exc:
        print(f"配置或地址错误: {exception_chain(exc)}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
