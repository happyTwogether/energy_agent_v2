#!/usr/bin/env python3
"""采集 OpenAI-compatible 模型连通性证据并生成可转发报告。

生产容器示例：
    python /app/scripts/diagnose_llm.py \
      --base-url http://10.159.228.81:3000/scmpApi/v1 \
      --model qwen-dense-30b-128k \
      --output-dir /app/logs

脚本不输出、不保存 API Key。TCP 不通时不会继续伪造多层重复超时。
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict, dataclass, field
from datetime import datetime
import errno
import hashlib
import json
import os
from pathlib import Path
import platform
import select
import socket
import sys
import time
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit
import uuid

import agentscope
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
IN_PROGRESS_ERRNOS = {
    errno.EINPROGRESS,
    errno.EWOULDBLOCK,
    errno.EALREADY,
    getattr(errno, "WSAEWOULDBLOCK", errno.EWOULDBLOCK),
}


class ProbeFailure(RuntimeError):
    """携带结构化证据的测试失败。"""

    def __init__(self, message: str, observations: dict[str, Any] | None = None):
        super().__init__(message)
        self.observations = observations or {}


@dataclass(slots=True)
class ProbeResult:
    """单个可审计测试的结构化结果。"""

    probe_id: str
    label: str
    layer: str
    status: str
    started_at: str
    ended_at: str
    elapsed_ms: float
    observations: dict[str, Any] = field(default_factory=dict)
    error_type: str = ""
    error_message: str = ""


@dataclass(slots=True)
class Diagnosis:
    """对整个证据包的结论。"""

    category: str
    headline: str
    confirmed: list[str]
    not_proven: list[str]
    provider_actions: list[str]


def now_iso() -> str:
    """返回带时区的 ISO-8601 时间。"""
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def parse_args() -> argparse.Namespace:
    """解析诊断参数。"""
    parser = argparse.ArgumentParser(
        description="采集网络、HTTP、SDK 和业务请求证据。",
    )
    parser.add_argument("--base-url", help="覆盖 LLM_BASE_URL")
    parser.add_argument("--model", help="覆盖 DEFAULT_MODEL")
    parser.add_argument("--prompt", default="长沙市能耗报表")
    parser.add_argument("--connect-timeout", type=float, default=5.0)
    parser.add_argument("--timeout", dest="request_timeout", type=float, default=30.0)
    parser.add_argument("--tcp-attempts", type=int, default=3)
    parser.add_argument("--tcp-interval", type=float, default=1.0)
    parser.add_argument("--container-name", default="energy")
    parser.add_argument("--caller-hostname", help="可选：Docker 宿主机名")
    parser.add_argument("--caller-host-ip", help="可选：宿主机对外源 IP")
    parser.add_argument("--output-dir", default=str(PROJECT_ROOT / "logs"))
    return parser.parse_args()


def load_settings(args: argparse.Namespace) -> Settings:
    """合并项目配置和命令行覆盖。"""
    settings = Settings(_env_file=PROJECT_ROOT / ".env")
    updates: dict[str, Any] = {"llm_timeout_seconds": args.request_timeout}
    if args.base_url:
        updates["llm_base_url"] = args.base_url
    if args.model:
        updates["default_model"] = args.model
    return settings.model_copy(update=updates)


def chat_completions_url(base_url: str) -> str:
    """将 SDK base URL 转为 HTTP 诊断端点。"""
    normalized = base_url.strip().rstrip("/")
    suffix = "/chat/completions"
    return normalized if normalized.endswith(suffix) else normalized + suffix


def parse_target(endpoint: str) -> tuple[str, int, str]:
    """从端点中提取主机、端口和 scheme。"""
    parsed = urlsplit(endpoint)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(f"不支持的 URL scheme: {parsed.scheme}")
    if not parsed.hostname:
        raise ValueError(f"无法解析目标主机: {endpoint}")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return parsed.hostname, port, parsed.scheme


def script_sha256() -> str:
    """计算当前诊断脚本指纹。"""
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def file_sha256(path: Path) -> str:
    """计算证据文件指纹。"""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def active_proxy_names() -> list[str]:
    """只记录已设置的代理变量名，不记录值。"""
    return [name for name in PROXY_ENV_NAMES if os.getenv(name)]


def resolve_addresses(host: str, port: int) -> list[str]:
    """记录操作系统为目标解析出的地址。"""
    addresses = {
        item[4][0]
        for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    }
    return sorted(addresses)


def select_target(host: str, port: int) -> tuple[int, tuple[Any, ...]]:
    """选择第一个 TCP 目标地址。"""
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not addresses:
        raise ValueError(f"目标无可用地址: {host}:{port}")
    family, _, _, _, socket_address = addresses[0]
    return family, socket_address


def route_selected_source(family: int, target: tuple[Any, ...]) -> str:
    """通过 UDP connect 查询路由选择的容器源 IP。"""
    with socket.socket(family, socket.SOCK_DGRAM) as probe:
        probe.connect(target)
        return str(probe.getsockname()[0])


def safe_route_selected_source(family: int, target: tuple[Any, ...]) -> str:
    """路由源 IP 采集失败时保留错误，不中断主测试。"""
    try:
        return route_selected_source(family, target)
    except OSError as exc:
        return f"无法采集：{type(exc).__name__}: {exc}"


def read_default_route() -> dict[str, str]:
    """从 Linux procfs 读取容器默认路由。"""
    route_file = Path("/proc/net/route")
    if not route_file.exists():
        return {"available": "false"}
    for line in route_file.read_text(encoding="utf-8").splitlines()[1:]:
        fields = line.split()
        if len(fields) >= 3 and fields[1] == "00000000":
            gateway = socket.inet_ntoa(bytes.fromhex(fields[2])[::-1])
            return {"available": "true", "interface": fields[0], "gateway": gateway}
    return {"available": "true", "interface": "", "gateway": ""}


def runtime_context(
    args: argparse.Namespace,
    settings: Settings,
    endpoint: str,
    run_id: str,
) -> dict[str, Any]:
    """采集可用于归属测试来源的运行上下文。"""
    host, port, scheme = parse_target(endpoint)
    family, target = select_target(host, port)
    return {
        "hostname": socket.gethostname(),
        "declared_container_name": args.container_name,
        "caller_hostname": args.caller_hostname or "未提供",
        "caller_host_ip": args.caller_host_ip or "未提供",
        "route_selected_container_ip": safe_route_selected_source(family, target),
        "default_route": read_default_route(),
        "target_host": host,
        "target_port": port,
        "target_scheme": scheme,
        "resolved_addresses": resolve_addresses(host, port),
        "endpoint": endpoint,
        "model": settings.default_model,
        "diagnostic_http_header": {"X-Diagnostic-Run-Id": run_id},
        "api_key_present": bool(settings.llm_api_key.get_secret_value().strip()),
        "proxy_env_names": active_proxy_names(),
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "agentscope_version": agentscope.__version__,
        "openai_version": openai.__version__,
        "httpx_version": httpx.__version__,
        "script_sha256": script_sha256(),
        "test_parameters": {
            "connect_timeout_seconds": args.connect_timeout,
            "tcp_attempts": args.tcp_attempts,
            "tcp_interval_seconds": args.tcp_interval,
            "request_timeout_seconds": args.request_timeout,
        },
    }


def errno_name(code: int) -> str:
    """将 errno 转换为可阅读名称。"""
    return errno.errorcode.get(code, f"ERRNO_{code}")


def tcp_result(
    attempt: int,
    status: str,
    started_at: str,
    started_perf: float,
    observations: dict[str, Any],
    error_code: int = 0,
) -> ProbeResult:
    """构造一次 TCP 尝试结果。"""
    message = ""
    if error_code:
        message = f"{errno_name(error_code)}: {os.strerror(error_code)}"
    return ProbeResult(
        probe_id=f"tcp_attempt_{attempt}",
        label=f"TCP 连接尝试 {attempt}",
        layer="TCP",
        status=status,
        started_at=started_at,
        ended_at=now_iso(),
        elapsed_ms=round((time.perf_counter() - started_perf) * 1000, 3),
        observations=observations,
        error_type=errno_name(error_code) if error_code else "",
        error_message=message,
    )


def tcp_connect_attempt(host: str, port: int, timeout: float, attempt: int) -> ProbeResult:
    """执行一次带源 socket 记录的 TCP 建连。"""
    started_at = now_iso()
    started_perf = time.perf_counter()
    family, target = select_target(host, port)
    with socket.socket(family, socket.SOCK_STREAM) as connection:
        connection.setblocking(False)
        connect_code = connection.connect_ex(target)
        source = connection.getsockname()
        observations = {"source_socket": source, "target_socket": target}
        if connect_code == 0:
            return tcp_result(attempt, "PASS", started_at, started_perf, observations)
        if connect_code not in IN_PROGRESS_ERRNOS:
            return tcp_result(
                attempt, "FAIL", started_at, started_perf, observations, connect_code
            )
        _, writable, exceptional = select.select([], [connection], [connection], timeout)
        if not writable and not exceptional:
            return tcp_result(
                attempt, "FAIL", started_at, started_perf, observations, errno.ETIMEDOUT
            )
        socket_error = connection.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
        status = "PASS" if socket_error == 0 else "FAIL"
        return tcp_result(
            attempt, status, started_at, started_perf, observations, socket_error
        )


async def collect_tcp_results(
    host: str,
    port: int,
    attempts: int,
    timeout: float,
    interval: float,
) -> list[ProbeResult]:
    """重复建立 TCP 连接，保留每次独立证据。"""
    results: list[ProbeResult] = []
    for attempt in range(1, attempts + 1):
        result = await asyncio.to_thread(
            tcp_connect_attempt, host, port, timeout, attempt
        )
        results.append(result)
        print_probe(result)
        if attempt < attempts:
            await asyncio.sleep(interval)
    return results


def redact(text: str, secret: str) -> str:
    """移除运行时凭证。"""
    return text.replace(secret, "<redacted>") if secret else text


def scrub_evidence(value: Any, secret: str) -> Any:
    """递归清理原始证据中可能回显的凭证。"""
    if isinstance(value, dict):
        return {key: scrub_evidence(item, secret) for key, item in value.items()}
    if isinstance(value, list):
        return [scrub_evidence(item, secret) for item in value]
    if isinstance(value, tuple):
        return [scrub_evidence(item, secret) for item in value]
    if isinstance(value, str):
        return redact(value, secret)
    return value


def exception_chain(exc: BaseException, secret: str) -> str:
    """输出完整异常链，并移除密钥。"""
    parts: list[str] = []
    current: BaseException | None = exc
    while current is not None:
        message = str(current).strip() or "<no message>"
        parts.append(f"{type(current).__name__}: {redact(message, secret)}")
        current = current.__cause__ or current.__context__
    return " <- ".join(parts)


def build_probe_result(
    probe_id: str,
    label: str,
    layer: str,
    status: str,
    started_at: str,
    started_perf: float,
    observations: dict[str, Any] | None = None,
    error_type: str = "",
    error_message: str = "",
) -> ProbeResult:
    """构造应用层测试结果。"""
    return ProbeResult(
        probe_id=probe_id,
        label=label,
        layer=layer,
        status=status,
        started_at=started_at,
        ended_at=now_iso(),
        elapsed_ms=round((time.perf_counter() - started_perf) * 1000, 3),
        observations=observations or {},
        error_type=error_type,
        error_message=error_message,
    )


async def run_probe(
    probe_id: str,
    label: str,
    layer: str,
    operation: Callable[[], Awaitable[dict[str, Any]]],
    timeout: float,
    secret: str,
) -> ProbeResult:
    """在硬超时内执行应用层测试。"""
    started_at = now_iso()
    started_perf = time.perf_counter()
    try:
        observations = await asyncio.wait_for(operation(), timeout=timeout + 5)
        result = build_probe_result(
            probe_id, label, layer, "PASS", started_at, started_perf, observations
        )
    except Exception as exc:
        observations = getattr(exc, "observations", {})
        result = build_probe_result(
            probe_id,
            label,
            layer,
            "FAIL",
            started_at,
            started_perf,
            observations,
            type(exc).__name__,
            exception_chain(exc, secret),
        )
    print_probe(result)
    return result


def skipped_probe(probe_id: str, label: str, layer: str, reason: str) -> ProbeResult:
    """记录因前置条件失败而未执行的测试。"""
    timestamp = now_iso()
    return ProbeResult(
        probe_id=probe_id,
        label=label,
        layer=layer,
        status="SKIP",
        started_at=timestamp,
        ended_at=timestamp,
        elapsed_ms=0.0,
        observations={"reason": reason},
    )


def raw_payload(model: str, prompt: str, stream: bool) -> dict[str, Any]:
    """构造最小 OpenAI-compatible 请求。"""
    return {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": stream,
    }


def response_preview(payload: Any) -> str:
    """提取不超过 160 字符的响应摘要。"""
    if not isinstance(payload, dict):
        return str(payload)[:160]
    choices = payload.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        message = choices[0].get("message") or choices[0].get("delta")
        if isinstance(message, dict) and message.get("content"):
            return str(message["content"])[:160]
    return json.dumps(payload, ensure_ascii=False)[:160]


def validate_completion(payload: Any) -> None:
    """确认响应是有效的 Chat Completions 结构。"""
    if not isinstance(payload, dict):
        raise ProbeFailure("响应不是 JSON object")
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ProbeFailure("响应缺少 choices", {"preview": response_preview(payload)})


def authorization_headers(settings: Settings, run_id: str) -> dict[str, str]:
    """构造不会被记录到报告的请求头。"""
    return {
        "Accept": "application/json",
        "Authorization": f"Bearer {settings.llm_api_key.get_secret_value()}",
        "Content-Type": "application/json",
        "X-Diagnostic-Run-Id": run_id,
    }


def http_observations(response: httpx.Response) -> dict[str, Any]:
    """记录 HTTP 状态和有界响应体，不记录请求头。"""
    return {
        "http_status": response.status_code,
        "http_version": response.http_version,
        "response_body_preview": response.text[:300],
    }


async def check_raw_non_stream(
    settings: Settings,
    prompt: str,
    run_id: str,
) -> dict[str, Any]:
    """绕过 SDK 执行最小非流式 HTTP 请求。"""
    endpoint = chat_completions_url(settings.llm_base_url)
    timeout = httpx.Timeout(settings.llm_timeout_seconds)
    async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
        response = await client.post(
            endpoint,
            headers=authorization_headers(settings, run_id),
            json=raw_payload(settings.default_model, prompt, False),
        )
    observations = http_observations(response)
    if not response.is_success:
        raise ProbeFailure(f"HTTP {response.status_code}", observations)
    payload = response.json()
    validate_completion(payload)
    observations["preview"] = response_preview(payload)
    return observations


async def consume_raw_stream(
    client: httpx.AsyncClient,
    endpoint: str,
    settings: Settings,
    payload: dict[str, Any],
    run_id: str,
) -> dict[str, Any]:
    """消费原始 SSE 响应。"""
    started = time.perf_counter()
    events = 0
    first_event_ms: float | None = None
    async with client.stream(
        "POST",
        endpoint,
        headers=authorization_headers(settings, run_id),
        json=payload,
    ) as response:
        if not response.is_success:
            body = (await response.aread()).decode(errors="replace")
            raise ProbeFailure(
                f"HTTP {response.status_code}",
                {"http_status": response.status_code, "response_body_preview": body[:300]},
            )
        async for line in response.aiter_lines():
            if line.startswith("data:"):
                events += 1
                first_event_ms = first_event_ms or round(
                    (time.perf_counter() - started) * 1000, 3
                )
    if events == 0:
        raise ProbeFailure("HTTP 成功但未收到 SSE data 事件")
    return {
        "http_status": response.status_code,
        "first_event_ms": first_event_ms,
        "sse_event_count": events,
    }


async def check_raw_stream(
    settings: Settings,
    prompt: str,
    run_id: str,
) -> dict[str, Any]:
    """验证 SSE 流和首个 data 事件。"""
    endpoint = chat_completions_url(settings.llm_base_url)
    timeout = httpx.Timeout(settings.llm_timeout_seconds)
    payload = raw_payload(settings.default_model, prompt, True)
    async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
        return await consume_raw_stream(client, endpoint, settings, payload, run_id)


def collect_blocks(content: list[Any]) -> tuple[str, list[str]]:
    """从 AgentScope 响应中提取文本和工具名。"""
    text = "".join(
        block.text for block in content if isinstance(block, TextBlock)
    )
    tools = [
        block.name for block in content if isinstance(block, ToolCallBlock)
    ]
    return text, tools


async def app_like_kwargs(messages: list[Any]) -> dict[str, Any]:
    """构造与报表路由一致的提示词和工具参数。"""
    messages.insert(0, SystemMsg(name="system", content=AGENT_EXECUTION_PROMPT))
    toolkit = build_toolkit()
    return {
        "tools": await toolkit.get_tool_schemas(),
        "tool_choice": ToolChoice(mode="query_report"),
    }


async def consume_agentscope_stream(response: Any, app_like: bool) -> dict[str, Any]:
    """消费 AgentScope 流并校验内容。"""
    chunks: list[Any] = []
    async for chunk in response:
        chunks.extend(chunk.content)
    text, tools = collect_blocks(chunks)
    if app_like and "query_report" not in tools:
        raise ProbeFailure("未返回 query_report 工具调用", {"tool_calls": tools})
    if not app_like and not text.strip():
        raise ProbeFailure("AgentScope 流式响应无文本")
    return {"preview": text[:160], "tool_calls": tools}


async def check_agentscope(
    settings: Settings,
    prompt: str,
    run_id: str,
    *,
    stream: bool,
    app_like: bool,
) -> dict[str, Any]:
    """通过项目实际模型工厂执行 SDK 请求。"""
    tuned = settings if app_like else settings.model_copy(update={"llm_max_tokens": 32})
    model = build_chat_model(tuned, stream=stream)
    model.max_retries = 0
    model.client_kwargs["max_retries"] = 0
    default_headers = dict(model.client_kwargs.get("default_headers", {}))
    default_headers["X-Diagnostic-Run-Id"] = run_id
    model.client_kwargs["default_headers"] = default_headers
    messages = [UserMsg(name="user", content=prompt)]
    call_kwargs = await app_like_kwargs(messages) if app_like else {}
    response = await model(messages=messages, **call_kwargs)
    if not stream:
        text, tools = collect_blocks(response.content)
        if not text.strip() and not tools:
            raise ProbeFailure("AgentScope 返回空响应")
        return {"preview": text[:160], "tool_calls": tools}
    return await consume_agentscope_stream(response, app_like)


def application_probe_specs(
    settings: Settings,
    prompt: str,
    run_id: str,
) -> list[tuple[str, str, str, Callable[[], Awaitable[dict[str, Any]]]]]:
    """定义 TCP 通过后的递进测试。"""
    return [
        (
            "raw_http_non_stream",
            "最小原始 HTTP 非流式请求",
            "HTTP",
            lambda: check_raw_non_stream(settings, prompt, run_id),
        ),
        (
            "raw_http_stream",
            "最小原始 HTTP SSE 请求",
            "SSE",
            lambda: check_raw_stream(settings, prompt, run_id),
        ),
        (
            "agentscope_non_stream",
            "项目 AgentScope 非流式请求",
            "SDK",
            lambda: check_agentscope(
                settings, prompt, run_id, stream=False, app_like=False
            ),
        ),
        (
            "app_like_report",
            "完整报表提示词与工具请求",
            "APPLICATION",
            lambda: check_agentscope(
                settings, prompt, run_id, stream=True, app_like=True
            ),
        ),
    ]


def skip_remaining_probes(results: list[ProbeResult], failed_layer: str) -> list[ProbeResult]:
    """为未执行的后续层生成可解释 SKIP 记录。"""
    existing = {result.probe_id for result in results}
    all_probes = (
        ("raw_http_stream", "最小原始 HTTP SSE 请求", "SSE"),
        ("agentscope_non_stream", "项目 AgentScope 非流式请求", "SDK"),
        ("app_like_report", "完整报表提示词与工具请求", "APPLICATION"),
    )
    reason = f"前置 {failed_layer} 测试失败，继续执行无法增加有效证据"
    return [
        skipped_probe(probe_id, label, layer, reason)
        for probe_id, label, layer in all_probes
        if probe_id not in existing
    ]


async def collect_application_results(
    settings: Settings,
    prompt: str,
    run_id: str,
) -> list[ProbeResult]:
    """按协议层次执行应用测试。"""
    results: list[ProbeResult] = []
    secret = settings.llm_api_key.get_secret_value()
    specs = application_probe_specs(settings, prompt, run_id)
    for probe_id, label, layer, operation in specs:
        result = await run_probe(
            probe_id,
            label,
            layer,
            operation,
            settings.llm_timeout_seconds,
            secret,
        )
        results.append(result)
        if result.status == "FAIL":
            results.extend(skip_remaining_probes(results, layer))
            break
    return results


def network_failure_diagnosis(tcp_results: list[ProbeResult]) -> Diagnosis:
    """生成 TCP 全失败时的结论。"""
    durations = ", ".join(f"{item.elapsed_ms:.0f}ms" for item in tcp_results)
    return Diagnosis(
        category="NETWORK_CONNECTIVITY_FAILURE",
        headline=(
            "生产容器到模型服务的 TCP 连接失败，"
            "请求未到达 HTTP/模型层。"
        ),
        confirmed=[
            f"{len(tcp_results)} 次 TCP 建连全部失败，耗时为 {durations}。",
            "未收到任何 HTTP 状态码。",
            "该结果与 API Key、模型名、Prompt、AgentScope 或工具 Schema 无关。",
        ],
        not_proven=[
            "不能根据本次结果判定模型推理服务是否正常。",
            "不能根据本次结果判定 OpenAI-compatible 协议是否兼容。",
        ],
        provider_actions=[
            "检查目标端口是否正在监听且绑定到可访问网卡。",
            "检查服务端防火墙、ACL、安全策略和来源 IP 白名单。",
            (
                "按源 IP、目标 IP/3000 端口和报告中的精确时间"
                "查询防火墙或抓包，确认是否收到 SYN。"
            ),
            "对比可用智能体与本容器的真实出口 IP 和网段。",
        ],
    )


def intermittent_network_diagnosis(results: list[ProbeResult]) -> Diagnosis:
    """生成 TCP 时通时断结论。"""
    passed = sum(result.status == "PASS" for result in results)
    return Diagnosis(
        category="INTERMITTENT_NETWORK_CONNECTIVITY",
        headline="TCP 连接存在间歇性，需先处理网络稳定性。",
        confirmed=[f"{len(results)} 次连接中 {passed} 次成功。"],
        not_proven=["在网络稳定前，不能稳定验证模型服务。"],
        provider_actions=["联合网络侧检查丢包、ACL 和负载均衡健康状态。"],
    )


def http_failure_diagnosis(failure: ProbeResult) -> Diagnosis:
    """生成 TCP 成功但 HTTP 失败的结论。"""
    status = failure.observations.get("http_status")
    category = "HTTP_API_FAILURE"
    if status in {401, 403}:
        category = "AUTHORIZATION_FAILURE"
    elif status == 404:
        category = "ENDPOINT_CONFIGURATION_FAILURE"
    elif isinstance(status, int) and status >= 500:
        category = "PROVIDER_GATEWAY_FAILURE"
    return Diagnosis(
        category=category,
        headline=f"TCP 可达，但最小 HTTP 请求失败（status={status}）。",
        confirmed=["TCP 建连成功。", f"HTTP 测试失败：{failure.error_message}"],
        not_proven=["尚未验证流式、SDK 和完整业务请求。"],
        provider_actions=[
            "根据 HTTP 状态码检查鉴权、路径、模型名和网关日志。"
        ],
    )


def layer_failure_diagnosis(category: str, failure: ProbeResult) -> Diagnosis:
    """生成协议、SDK 或完整请求层的结论。"""
    return Diagnosis(
        category=category,
        headline=f"{failure.layer} 层失败，前置网络/HTTP 证据已通过。",
        confirmed=[f"首个失败测试：{failure.label}。", failure.error_message],
        not_proven=["需要结合失败层的响应和提供方日志继续定位。"],
        provider_actions=[f"根据 run_id 查询 {failure.layer} 层请求日志。"],
    )


def success_diagnosis() -> Diagnosis:
    """生成全链路未复现故障的结论。"""
    return Diagnosis(
        category="NO_FAILURE_REPRODUCED",
        headline="本次网络、HTTP、SSE、SDK 和报表请求均通过。",
        confirmed=["当前时间窗未复现连接故障。"],
        not_proven=["不能排除历史时间窗的间歇性故障。"],
        provider_actions=["对比原失败时间窗的网关、模型和负载日志。"],
    )


def configuration_incomplete_diagnosis() -> Diagnosis:
    """生成 TCP 通过但缺少模型凭证的结论。"""
    return Diagnosis(
        category="CONFIGURATION_INCOMPLETE",
        headline="TCP 可达，但未注入 LLM_API_KEY，模型 HTTP 请求未执行。",
        confirmed=["TCP 建连成功。", "运行环境未提供 LLM_API_KEY。"],
        not_proven=["未验证鉴权、模型推理、SSE 和 AgentScope 兼容性。"],
        provider_actions=["由部署侧安全注入 API Key 后重新执行。"],
    )


def diagnose(tcp_results: list[ProbeResult], app_results: list[ProbeResult]) -> Diagnosis:
    """根据最深成功层和首个失败层归类。"""
    tcp_passes = sum(result.status == "PASS" for result in tcp_results)
    if tcp_passes == 0:
        return network_failure_diagnosis(tcp_results)
    if tcp_passes < len(tcp_results):
        return intermittent_network_diagnosis(tcp_results)
    if app_results and all(result.status == "SKIP" for result in app_results):
        return configuration_incomplete_diagnosis()
    failures = [result for result in app_results if result.status == "FAIL"]
    if not failures:
        return success_diagnosis()
    failure = failures[0]
    if failure.layer == "HTTP":
        return http_failure_diagnosis(failure)
    if failure.layer == "SSE":
        return layer_failure_diagnosis("STREAM_PROTOCOL_FAILURE", failure)
    if failure.layer == "SDK":
        return layer_failure_diagnosis("SDK_COMPATIBILITY_FAILURE", failure)
    return layer_failure_diagnosis("APPLICATION_REQUEST_FAILURE", failure)


def print_probe(result: ProbeResult) -> None:
    """输出适合终端截图的单行证据。"""
    source = result.observations.get("source_socket", "")
    error = f" {result.error_type}" if result.error_type else ""
    print(
        f"[{result.status}] {result.probe_id} layer={result.layer} "
        f"elapsed={result.elapsed_ms:.0f}ms source={source}{error}",
        flush=True,
    )


def print_context(run_id: str, started_at: str, context: dict[str, Any]) -> None:
    """在测试前输出可识别的来源和目标。"""
    print("=" * 72)
    print("模型连通性证据采集")
    print(f"run_id={run_id}")
    print(f"started_at={started_at}")
    print(f"container_hostname={context['hostname']}")
    print(f"declared_container_name={context['declared_container_name']}")
    print(f"caller_hostname={context['caller_hostname']}")
    print(f"container_source_ip={context['route_selected_container_ip']}")
    print(f"caller_host_ip={context['caller_host_ip']}")
    print(f"target={context['target_host']}:{context['target_port']}")
    print(f"endpoint={context['endpoint']}")
    print(
        "diagnostic_http_header_if_http_runs="
        f"{context['diagnostic_http_header']}"
    )
    print(f"proxy_env_names={context['proxy_env_names'] or 'none'}")
    print("=" * 72, flush=True)


def build_bundle(
    run_id: str,
    started_at: str,
    context: dict[str, Any],
    results: list[ProbeResult],
    diagnosis: Diagnosis,
    secret: str,
) -> dict[str, Any]:
    """构造不包含密钥的原始证据包。"""
    return {
        "schema_version": 1,
        "run_id": run_id,
        "started_at": started_at,
        "ended_at": now_iso(),
        "context": context,
        "results": scrub_evidence(
            [asdict(result) for result in results],
            secret,
        ),
        "diagnosis": asdict(diagnosis),
    }


def markdown_table_row(values: list[Any]) -> str:
    """构造 Markdown 表格行并清理换行。"""
    cells = [str(value).replace("\n", " ").replace("|", "\\|") for value in values]
    return "| " + " | ".join(cells) + " |"


def render_tcp_table(results: list[dict[str, Any]]) -> list[str]:
    """渲染 TCP 逐次建连证据。"""
    lines = [
        "| 次数 | 开始时间 | 结果 | 源 socket | 目标 socket | 耗时 | 错误 |",
        "|---:|---|---|---|---|---:|---|",
    ]
    tcp_results = [item for item in results if item["layer"] == "TCP"]
    for index, item in enumerate(tcp_results, start=1):
        observations = item["observations"]
        lines.append(
            markdown_table_row(
                [
                    index,
                    item["started_at"],
                    item["status"],
                    observations.get("source_socket", ""),
                    observations.get("target_socket", ""),
                    f"{item['elapsed_ms']:.0f}ms",
                    item["error_message"],
                ]
            )
        )
    return lines


def render_layer_table(results: list[dict[str, Any]]) -> list[str]:
    """渲染 HTTP/SDK/业务层证据。"""
    lines = [
        "| 测试 | 层级 | 结果 | 耗时 | HTTP 状态 | 错误/跳过原因 |",
        "|---|---|---|---:|---:|---|",
    ]
    for item in results:
        if item["layer"] == "TCP":
            continue
        observations = item["observations"]
        explanation = item["error_message"] or observations.get("reason", "")
        lines.append(
            markdown_table_row(
                [
                    item["label"],
                    item["layer"],
                    item["status"],
                    f"{item['elapsed_ms']:.0f}ms",
                    observations.get("http_status", ""),
                    explanation,
                ]
            )
        )
    return lines


def render_markdown(bundle: dict[str, Any]) -> str:
    """生成可直接转发给模型提供方的人读报告。"""
    context = bundle["context"]
    diagnosis = bundle["diagnosis"]
    lines = [
        "# 模型服务连通性证据报告",
        "",
        f"- **Run ID**: `{bundle['run_id']}`",
        f"- **测试时间**: `{bundle['started_at']}` 至 `{bundle['ended_at']}`",
        f"- **执行容器 hostname**: `{context['hostname']}`",
        f"- **声明的容器名**: `{context['declared_container_name']}`",
        f"- **Docker 宿主机名**: `{context['caller_hostname']}`",
        f"- **容器路由选择源 IP**: `{context['route_selected_container_ip']}`",
        f"- **宿主机对外源 IP**: `{context['caller_host_ip']}`",
        f"- **容器默认路由**: `{context['default_route']}`",
        f"- **目标解析地址**: `{context['resolved_addresses']}`",
        f"- **目标**: `{context['target_host']}:{context['target_port']}`",
        f"- **HTTP Endpoint**: `{context['endpoint']}`",
        f"- **模型**: `{context['model']}`",
        (
            "- **HTTP 追踪请求头（仅 HTTP 测试执行时发送）**: "
            f"`{context['diagnostic_http_header']}`"
        ),
        f"- **测试参数**: `{context['test_parameters']}`",
        f"- **代理环境变量**: `{context['proxy_env_names'] or 'none'}`",
        "",
        "## 结论",
        "",
        f"**{diagnosis['headline']}**",
        f"\n归类：`{diagnosis['category']}`",
        "",
        "### 已确认",
        "",
        *[f"- {item}" for item in diagnosis["confirmed"]],
        "",
        "### 本次证据不能证明",
        "",
        *[f"- {item}" for item in diagnosis["not_proven"]],
        "",
        "## TCP 逐次建连证据",
        "",
        *render_tcp_table(bundle["results"]),
        "",
        "## 各协议层测试",
        "",
        *render_layer_table(bundle["results"]),
        "",
        "## 请模型提供方协查",
        "",
        *[f"- {item}" for item in diagnosis["provider_actions"]],
        "",
        "## 完整性信息",
        "",
        f"- 诊断脚本 SHA-256: `{context['script_sha256']}`",
        f"- Python: `{context['python_version']}`",
        f"- AgentScope: `{context['agentscope_version']}`",
        f"- OpenAI SDK: `{context['openai_version']}`",
        f"- HTTPX: `{context['httpx_version']}`",
        "- 报告不包含 API Key。",
        "",
    ]
    return "\n".join(lines)


def write_evidence_files(
    output_dir: Path,
    bundle: dict[str, Any],
) -> tuple[Path, Path, Path]:
    """保存报告、JSON 原始证据和 SHA-256 清单。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    stem = f"llm-evidence-{stamp}-{bundle['run_id'][:8]}"
    markdown_path = output_dir / f"{stem}.md"
    json_path = output_dir / f"{stem}.json"
    checksum_path = output_dir / f"{stem}.sha256"
    markdown_path.write_text(render_markdown(bundle), encoding="utf-8")
    json_path.write_text(
        json.dumps(bundle, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    checksum_path.write_text(
        f"{file_sha256(markdown_path)}  {markdown_path.name}\n"
        f"{file_sha256(json_path)}  {json_path.name}\n",
        encoding="utf-8",
    )
    markdown_path.chmod(0o600)
    json_path.chmod(0o600)
    checksum_path.chmod(0o600)
    return markdown_path, json_path, checksum_path


def print_conclusion(
    diagnosis: Diagnosis,
    markdown_path: Path,
    json_path: Path,
    checksum_path: Path,
) -> None:
    """输出适合截图的简明结论和证据路径。"""
    print("\n" + "=" * 72)
    print(f"诊断分类: {diagnosis.category}")
    print(f"结论: {diagnosis.headline}")
    print("已确认:")
    for item in diagnosis.confirmed:
        print(f"  - {item}")
    print("不能证明:")
    for item in diagnosis.not_proven:
        print(f"  - {item}")
    print(f"可转发 Markdown 报告: {markdown_path}")
    print(f"JSON 原始证据: {json_path}")
    print(f"SHA-256 校验清单: {checksum_path}")
    print("=" * 72)


def skipped_application_results(reason: str) -> list[ProbeResult]:
    """生成全部应用层未执行的记录。"""
    probes = (
        ("raw_http_non_stream", "最小原始 HTTP 非流式请求", "HTTP"),
        ("raw_http_stream", "最小原始 HTTP SSE 请求", "SSE"),
        ("agentscope_non_stream", "项目 AgentScope 非流式请求", "SDK"),
        ("app_like_report", "完整报表提示词与工具请求", "APPLICATION"),
    )
    return [skipped_probe(*probe, reason) for probe in probes]


def build_application_results(
    tcp_passes: int,
    context: dict[str, Any],
) -> list[ProbeResult] | None:
    """根据前置条件构造跳过记录，None 表示可继续。"""
    if tcp_passes == 0:
        return skipped_application_results(
            "TCP 前置条件失败，HTTP/模型层未被测试"
        )
    if not context["api_key_present"]:
        return skipped_application_results("TCP 通过，但未注入 LLM_API_KEY")
    return None


async def async_main(args: argparse.Namespace) -> int:
    """执行证据采集并保存报告。"""
    if args.tcp_attempts < 1:
        raise SystemExit("--tcp-attempts 必须大于等于 1")
    settings = load_settings(args)
    if not settings.llm_base_url.strip():
        raise SystemExit("缺少 LLM_BASE_URL")
    endpoint = chat_completions_url(settings.llm_base_url)
    run_id = str(uuid.uuid4())
    started_at = now_iso()
    context = runtime_context(args, settings, endpoint, run_id)
    print_context(run_id, started_at, context)
    tcp_results = await collect_tcp_results(
        context["target_host"],
        context["target_port"],
        args.tcp_attempts,
        args.connect_timeout,
        args.tcp_interval,
    )
    tcp_passes = sum(result.status == "PASS" for result in tcp_results)
    app_results = build_application_results(tcp_passes, context)
    if app_results is None:
        app_results = await collect_application_results(settings, args.prompt, run_id)
    results = tcp_results + app_results
    diagnosis = diagnose(tcp_results, app_results)
    bundle = build_bundle(
        run_id,
        started_at,
        context,
        results,
        diagnosis,
        settings.llm_api_key.get_secret_value(),
    )
    markdown_path, json_path, checksum_path = write_evidence_files(
        Path(args.output_dir),
        bundle,
    )
    print_conclusion(diagnosis, markdown_path, json_path, checksum_path)
    return 0 if diagnosis.category == "NO_FAILURE_REPRODUCED" else 1


def main() -> int:
    """命令行入口。"""
    try:
        return asyncio.run(async_main(parse_args()))
    except KeyboardInterrupt:
        print("\n已取消诊断。", file=sys.stderr)
        return 130
    except (OSError, ValueError) as exc:
        print(f"无法启动诊断: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
