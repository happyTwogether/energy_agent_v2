#!/usr/bin/env python3
"""内网自检：默认完全离线；可显式开启生产会话表的只读结构及权限检查。"""

from __future__ import annotations

import argparse
import ast
import asyncio
import hashlib
from importlib.metadata import version
import json
from pathlib import Path
import sys


class PatchVerificationError(RuntimeError):
    """仅携带本脚本产生、可安全显示的校验信息。"""


def check_files(root: Path, manifest_path: Path) -> None:
    if not root.is_dir():
        raise PatchVerificationError(f"检查目录不存在：{root}；/app 通常是容器内路径，宿主机请使用实际项目路径")
    if not manifest_path.is_file():
        raise PatchVerificationError(f"找不到补丁清单：{manifest_path}；请检查 --root 与完整部署包是否对应")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for relative, expected_hash in manifest["files"].items():
        path = (root / relative).resolve()
        if not path.is_relative_to(root):
            raise PatchVerificationError("补丁清单含越界路径")
        if not path.is_file():
            raise PatchVerificationError(f"缺少补丁文件：{relative}")
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected_hash:
            raise PatchVerificationError(f"文件未完整更新或已被修改：{relative}")
        if path.suffix == ".py":
            ast.parse(path.read_text(encoding="utf-8"), filename=relative)
    actual_version = version("agentscope")
    if actual_version != manifest["agentscope_version"]:
        raise PatchVerificationError(f"AgentScope 版本不匹配：需要 {manifest['agentscope_version']}，当前 {actual_version}")
    print(f"PASS 文件校验与语法检查（{len(manifest['files'])} 个文件，AgentScope {actual_version}）")


async def check_agent() -> None:
    from agentscope.credential import CredentialBase
    from agentscope.message import TextBlock, ThinkingBlock, ToolCallBlock
    from agentscope.model import ChatModelBase, ChatResponse, ChatUsage
    from agentscope.tool import Toolkit
    from pydantic import BaseModel

    from app.agent.dify_events import DifyEventAdapter
    from app.agent.messages import parse_request_messages
    from app.agent.runtime import build_energy_agent
    from app.agent.tool_specs import EnergyToolSpec
    from app.agent.toolkit import EnergyFunctionTool
    from app.core.config import Settings
    from app.services.query_scope import ToolRequestContext, normalize_tool_scope

    scoped = normalize_tool_scope(
        "analyze_batch_cells_energy", {"dist_name": "全网"},
        ToolRequestContext("湘潭市的节电空间"),
    )
    if scoped.get("dist_name") != "湘潭市":
        raise PatchVerificationError("地区约束自检失败")
    print("PASS 湘潭市范围约束")

    class LocalModel(ChatModelBase):
        class Parameters(BaseModel):
            pass

        def __init__(self, thinking_calls):
            super().__init__(
                credential=CredentialBase(), model="offline-selfcheck",
                parameters=self.Parameters(), stream=False, max_retries=0,
            )
            self.calls = 0
            self.thinking_calls = thinking_calls

        async def _call_api(self, *args, **kwargs):
            self.calls += 1
            if self.calls <= self.thinking_calls:
                blocks = [ThinkingBlock(thinking="自检中")]
            elif self.calls == self.thinking_calls + 1:
                blocks = [ToolCallBlock(id="local-report", name="query_report", input="{}")]
            else:
                blocks = [TextBlock(text="错误：多调用了一次模型")]
            return ChatResponse(
                content=blocks, is_last=True,
                usage=ChatUsage(input_tokens=1, output_tokens=1, time=0),
            )

    class LocalSession:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *args):
            return False

    async def local_report(db):
        return {"success": True, "report_content": "离线自检报告"}

    for budget, thinking_calls in ((1, 0), (5, 3)):
        toolkit = Toolkit(tools=[EnergyFunctionTool(
            spec=EnergyToolSpec(
                name="query_report", description="离线自检", handler=local_report,
                input_schema={"type": "object", "properties": {}},
            ), session_factory=LocalSession,
        )])
        model = LocalModel(thinking_calls)
        parsed = parse_request_messages([{"role": "user", "content": "生成能耗报表"}])
        agent = build_energy_agent(
            "offline-check", parsed, model=model, toolkit=toolkit,
            settings=Settings(_env_file=None, agent_max_steps=budget),
        )
        adapter = DifyEventAdapter(conversation_id="offline", message_id="m", task_id="t", created_at=1)
        async for _ in adapter.stream(agent.reply_stream(parsed.current_user)):
            pass
        if (
            not adapter.result.completed or adapter.result.answer != "离线自检报告"
            or model.calls != thinking_calls + 1
        ):
            raise PatchVerificationError(f"末轮报告返回失败：budget={budget}")
    print("PASS 单步预算及第五步工具成功后的报告返回")

    from unittest.mock import patch
    from app.tools.batch_energy_tool import TOOL_INPUT_SCHEMA

    executed = []

    async def local_analysis(db, **arguments):
        executed.append(arguments)
        return {"success": True, "report_content": "任务条件自检报告"}

    spec = EnergyToolSpec(name="analyze_batch_cells_energy", description="离线分析", handler=local_analysis, input_schema=TOOL_INPUT_SCHEMA)

    class BatchModel(LocalModel):
        async def _call_api(self, *args, **kwargs):
            self.calls += 1
            return ChatResponse(content=[ToolCallBlock(id="local-analysis", name="analyze_batch_cells_energy", input='{"dist_name":"全网"}')], is_last=True)

    from app.agent.context import snapshot_agent_state
    checkpoint = None
    with patch("app.agent.toolkit.TOOL_SPECS", (spec,)), patch("app.agent.toolkit._new_session", return_value=LocalSession()):
        for question, expected_calls in (("湘潭市的节电空间", 1), ("株洲市的呢？", 0), ("中等扩展", 0)):
            model = BatchModel(0)
            parsed = parse_request_messages([
                {"role": "system", "content": "", "agent_state": checkpoint},
                {"role": "user", "content": question},
            ])
            agent = build_energy_agent("offline-memory", parsed, model=model, settings=Settings(_env_file=None))
            adapter = DifyEventAdapter(conversation_id="offline", message_id="m", task_id="t", created_at=1)
            async for _ in adapter.stream(agent.reply_stream(parsed.current_user)):
                pass
            if not adapter.result.completed or model.calls != expected_calls:
                raise PatchVerificationError("连续追问自检失败")
            checkpoint = {"version": (checkpoint["version"] if checkpoint else 0) + 1, "state": snapshot_agent_state(agent.state)}
            if checkpoint["state"]["tasks_context"]["tasks"][-1]["state"] != "completed":
                raise PatchVerificationError("框架任务完成状态未保存")
    if [item.get("dist_name") for item in executed] != ["湘潭市", "株洲市", "株洲市"] or executed[-1].get("expansion_level") != "moderate":
        raise PatchVerificationError("连续追问条件继承失败")
    print("PASS 三轮任务快照恢复（湘潭→株洲→中等扩展，后两轮不请求模型）")

    from app.agent.toolkit import GENERAL_GUIDANCE_TEXT
    model = LocalModel(0)
    parsed = parse_request_messages([{"role": "user", "content": "你好！"}])
    agent = build_energy_agent("offline-guidance", parsed, model=model, settings=Settings(_env_file=None))
    adapter = DifyEventAdapter(conversation_id="offline", message_id="m", task_id="t", created_at=1)
    async for _ in adapter.stream(agent.reply_stream(parsed.current_user)):
        pass
    if not adapter.result.completed or adapter.result.answer != GENERAL_GUIDANCE_TEXT or model.calls:
        raise PatchVerificationError("问候语仍请求了远程模型或未正常返回")
    print("PASS 问候语复用原生工具返回，远程模型调用0次")

    class InvalidArgumentsModel(LocalModel):
        async def _call_api(self, model_name, messages, **kwargs):
            self.calls += 1
            for message in messages:
                for block in getattr(message, "content", []):
                    if isinstance(block, ToolCallBlock):
                        json.loads(block.input)  # 错误参数若进入历史，本次校验必须失败。
            arguments = "\n\n" if self.calls == 1 else '{"dist_name":"湘潭市"}'
            return ChatResponse(content=[ToolCallBlock(id=f"check-{self.calls}", name="analyze_batch_cells_energy", input=arguments)], is_last=True)

    before = len(executed)
    model = InvalidArgumentsModel(0)
    parsed = parse_request_messages([{"role": "user", "content": "分析湘潭市的节电空间"}])
    with patch("app.agent.toolkit.TOOL_SPECS", (spec,)), patch("app.agent.toolkit._new_session", return_value=LocalSession()):
        agent = build_energy_agent("offline-arguments", parsed, model=model, settings=Settings(_env_file=None))
        adapter = DifyEventAdapter(conversation_id="offline", message_id="m", task_id="t", created_at=1)
        async for _ in adapter.stream(agent.reply_stream(parsed.current_user)):
            pass
    if not adapter.result.completed or model.calls != 2 or len(executed) != before + 1:
        raise PatchVerificationError("无效工具参数干净重试或单次执行检查失败")
    print("PASS 无效原生参数不进入历史，干净重试一次后只执行一次工具")

    class MarkupModel(LocalModel):
        async def _call_api(self, *args, **kwargs):
            return ChatResponse(content=[TextBlock(text="<tool_call><function=query_report></tool_call>")], is_last=True)

    parsed = parse_request_messages([{"role": "user", "content": "湘潭市的节电空间"}])
    agent = build_energy_agent("offline-protocol", parsed, model=MarkupModel(0), toolkit=Toolkit(), settings=Settings(_env_file=None))
    adapter = DifyEventAdapter(conversation_id="offline", message_id="m", task_id="t", created_at=1)
    payloads = [json.loads(item["data"]) async for item in adapter.stream(agent.reply_stream(parsed.current_user))]
    if adapter.result.completed or adapter.result.answer or payloads[-1].get("code") != "model_protocol_error":
        raise PatchVerificationError("工具协议隔离失败")
    print("PASS 工具调用标记不能作为正文返回")

    from agentscope.message import AssistantMsg, UserMsg
    from agentscope.model import StructuredResponse
    from app.agent.context import restore_agent_state

    class SummaryModel(LocalModel):
        async def count_tokens(self, messages, tools=None, **kwargs):
            return sum(len(message.get_text_content() or "") for message in messages) // 4

        async def generate_structured_output(self, messages, structured_model, **kwargs):
            return StructuredResponse(content={key: "离线摘要自检" for key in structured_model["properties"]})

    model = SummaryModel(0)
    model.context_size = 10000
    agent = build_energy_agent("offline-summary", parsed, model=model, toolkit=Toolkit(), settings=Settings(_env_file=None))
    agent.state.context = [AssistantMsg(name="energy_agent", content="历史结果" * 1500), UserMsg(name="user", content="解释结果")] * 6
    await agent.compress_context()
    if "离线摘要自检" not in agent.state.summary:
        raise PatchVerificationError("框架原生摘要未触发")
    restored = restore_agent_state(snapshot_agent_state(agent.state), "offline-summary")
    if restored.summary != agent.state.summary:
        raise PatchVerificationError("框架摘要未能恢复")
    print("PASS 框架原生压缩、AgentState 序列化与摘要恢复")


async def check_local_storage() -> None:
    """仅使用 Python 自带 SQLite 内存库验证仓储隔离和原文保留。"""
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session
    from app.services.database import Base, load_conversation, save_conversation, load_agent_state
    from app.agent.errors import AgentPublicError

    class LocalDb:
        def __init__(self, session):
            self.session = session

        async def execute(self, statement):
            return self.session.execute(statement)

        async def commit(self):
            self.session.commit()

        async def rollback(self):
            self.session.rollback()

    engine = create_engine("sqlite://")
    try:
        with engine.connect() as connection:
            connection.execute(text("ATTACH DATABASE ':memory:' AS jd_agent"))
            Base.metadata.create_all(connection)
            connection.commit()
            with Session(connection) as session:
                db = LocalDb(session)
                original = [{"role": "user", "content": str(index)} for index in range(24)]
                await save_conversation(db, "local-check", "test-owner", original, agent_state={"summary": "本地摘要"})
                if len(await load_conversation(db, "local-check", "test-owner")) != 24:
                    raise PatchVerificationError("原文保留自检失败")
                try:
                    await load_conversation(db, "local-check", "test-other")
                except AgentPublicError:
                    pass
                else:
                    raise PatchVerificationError("会话用户隔离自检失败")
                if (await load_agent_state(db, "local-check", "test-owner"))["state"]["summary"] != "本地摘要":
                    raise PatchVerificationError("独立状态存储自检失败")
    finally:
        engine.dispose()
    print("PASS 本地内存 SQL：用户隔离、超过20条原文保留、独立摘要存储")


async def check_database_schema() -> None:
    from app.services.database import get_session_factory, verify_session_storage
    from app.agent.errors import AgentConfigurationError
    try:
        async with get_session_factory()() as db:
            await verify_session_storage(db)
    except AgentConfigurationError as exc:
        raise PatchVerificationError(str(exc)) from exc
    print("PASS 生产会话表结构只读检查（未读取问答内容或写入数据）")


async def check_database_permissions() -> None:
    from app.services.database import get_session_factory, inspect_session_permissions
    async with get_session_factory()() as db:
        checks = await inspect_session_permissions(db)
    failed = False
    keys = ("table_exists", "schema_usage", "can_select", "can_insert", "can_update", "can_delete")
    for item in checks:
        allowed = all(item[key] for key in keys)
        print(("PASS " if allowed else "FAIL ") + item["table"] + " " + json.dumps({key: bool(item[key]) for key in keys}, ensure_ascii=False))
        failed = failed or not allowed
    if failed:
        raise PatchVerificationError("缺表或权限不足；请由管理员建表并授予上述具体权限，无需授予应用账号 CREATE")
    print("PASS 会话表 ACL 权限检查（未试写数据，不代替真实写入及行级策略验收）")


async def check_startup_recovery() -> None:
    """使用真实 HTTP 生命周期复现连接失败与后台恢复，不连接生产数据库。"""
    import httpx
    import main as application
    from unittest.mock import patch
    from app.core.config import Settings

    with (
        patch.object(application, "get_settings", return_value=Settings(_env_file=None, self_service_enabled=False)),
        patch.object(application, "init_db", side_effect=[TimeoutError("offline-check"), None]),
        patch("app.api.routes.get_session_factory") as sessions,
    ):
        app = application.create_app()
        async with application.lifespan(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://selfcheck") as client:
                if (await client.get("/")).status_code != 200:
                    raise PatchVerificationError("连接失败时页面未能提供服务")
                if (await client.get("/api/v1/health")).status_code != 503:
                    raise PatchVerificationError("数据库未就绪时健康状态错误")
                response = await client.post("/api/v1/chat/stream", json={"query": "湘潭市的节电空间", "user": "offline-owner"})
                if response.status_code != 503 or sessions.called:
                    raise PatchVerificationError("数据库未就绪时错误地进入了业务查询")
                status = app.state.database_readiness
                # 等待已有后台任务的默认重试，无需人工调用 check 或重启服务。
                await asyncio.wait_for(status.ready_event.wait(), timeout=8)
                if (await client.get("/api/v1/health")).status_code != 200:
                    raise PatchVerificationError("数据库恢复后业务就绪状态未恢复")
    print("PASS 启动连接失败不退出、未就绪请求503、后台自动恢复200")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--check-database-schema", action="store_true", help="额外连接配置的业务数据库，只读检查会话表结构")
    parser.add_argument("--check-database-permissions", action="store_true", help="只读检查当前账号的会话表 SELECT/INSERT/UPDATE/DELETE 权限")
    args = parser.parse_args()
    root = args.root.resolve()
    manifest_path = args.manifest or root / "patch-manifest.json"
    sys.path.insert(0, str(root))
    try:
        check_files(root, manifest_path)
        asyncio.run(check_agent())
        asyncio.run(check_local_storage())
        asyncio.run(check_startup_recovery())
        if args.check_database_schema:
            asyncio.run(check_database_schema())
        if args.check_database_permissions:
            asyncio.run(check_database_permissions())
    except PatchVerificationError as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        # 不输出异常内容，避免导入/配置/依赖异常携带环境信息或凭证。
        print(f"FAIL 自检失败（{type(exc).__name__}）；请核对文件清单、依赖及所选检查项。", file=sys.stderr)
        return 1
    print("PASS 自检完成；真实模型、业务查询和服务恢复仍需实际请求验收。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
