"""
Prompt 构建器 — 动态拼装 system prompt。

借鉴 Hermes Agent 的动态拼装思想：
ID + SOUL + Tools + Memory → 按需拼装，不注入无关内容。
"""

import json
from pathlib import Path
from typing import Any

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger("prompt_builder")

_TEMPLATES_DIR = Path(__file__).parent / "templates"


def _read_template(name: str) -> str:
    """读取模板文件内容。"""
    path = _TEMPLATES_DIR / name
    if path.exists():
        return path.read_text(encoding="utf-8")
    logger.warning("模板文件不存在: %s", name)
    return ""


class PromptBuilder:
    """System prompt 组装器。

    组装顺序（借鉴 Hermes 的动态拼装链路）：
    1. IDENTITY — Agent 身份定义
    2. SOUL — 行为准则
    3. RULES — 核心规则
    4. TOOLS — 可用工具描述（动态注入）
    5. MEMORY — 用户持久记忆上下文
    6. OUTPUT — 输出格式要求
    """

    def __init__(self) -> None:
        self._identity = _read_template("system/identity.txt")
        self._soul = _read_template("system/soul.txt")
        self._rules = _read_template("system/rules.txt")
        self._output = _read_template("system/output.txt")

    def build(
        self,
        tools: list[dict[str, Any]],
        memory_context: str = "",
        extra_instructions: str = "",
    ) -> str:
        """构建完整的 system prompt。

        Args:
            tools: 工具列表（OpenAI function calling 格式）。
            memory_context: 持久记忆上下文文本。
            extra_instructions: 额外的指令（来自环境变量配置）。

        Returns:
            完整的 system prompt 字符串。
        """
        settings = get_settings()
        parts: list[str] = []

        # 1. 身份定义
        if settings.agent_system_prompt:
            parts.append(settings.agent_system_prompt)
        parts.append(self._identity)

        # 2. 行为准则
        parts.append(self._soul)

        # 3. 核心规则
        parts.append(self._rules)

        # 4. 额外指令
        if extra_instructions:
            parts.append(f"\n## 额外指令\n{extra_instructions}")

        # 5. 工具列表
        if tools:
            parts.append(self._format_tools_section(tools))

        # 6. 记忆上下文
        if memory_context:
            parts.append(f"\n## 用户历史上下文\n{memory_context}")

        # 7. 输出格式
        parts.append(self._output)

        return "\n\n".join(parts)

    @staticmethod
    def _format_tools_section(tools: list[dict[str, Any]]) -> str:
        """格式化工具描述部分。"""
        lines = ["## 可用工具"]
        for tool in tools:
            fn = tool.get("function", {})
            name = fn.get("name", "unknown")
            desc = fn.get("description", "")
            lines.append(f"\n### {name}\n{desc}")
            params = fn.get("parameters", {})
            if params.get("properties"):
                lines.append(f"```json\n{json.dumps(params, ensure_ascii=False, indent=2)}\n```")
        return "\n".join(lines)


# 全局单例
prompt_builder = PromptBuilder()
