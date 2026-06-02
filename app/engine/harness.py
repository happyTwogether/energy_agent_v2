"""
Harness 管控引擎 — 借鉴 Hermes Agent 的 Harness Engineering 思想。

在 Agent 循环的每个关键节点插入拦截检查，实现：
- Token 预算管控
- 循环检测
- 步骤计数
- 可扩展的 pre/post hook 链
"""

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable

from app.core.logging import get_logger

logger = get_logger("harness")

# —— 常量 ——
DEFAULT_MAX_STEPS = 8
DEFAULT_TOKEN_BUDGET = 12000
TOKEN_WARN_RATIO = 0.8  # 达到 80% 预算时触发压缩

# 每步估算 token 消耗（粗略）
EST_TOKENS_PER_LLM_CALL = 800
EST_TOKENS_PER_TOOL_RESULT = 500


@dataclass
class HarnessState:
    """Harness 跟踪的运行时状态。"""

    step_count: int = 0
    estimated_tokens: int = 0
    start_time: float = field(default_factory=time.time)
    tool_call_history: list[str] = field(default_factory=list)
    rejected_actions: list[str] = field(default_factory=list)


class HarnessEngine:
    """管控引擎，在 Agent 状态机每个转换点插入拦截。

    借鉴 Hermes 的核心设计：每步执行前后都有拦截点，
    确保 Agent 在可控范围内运行。
    """

    def __init__(
        self,
        max_steps: int = DEFAULT_MAX_STEPS,
        token_budget: int = DEFAULT_TOKEN_BUDGET,
    ) -> None:
        self.max_steps = max_steps
        self.token_budget = token_budget
        self._pre_hooks: list[Callable[["HarnessState", dict[str, Any]], Awaitable[bool]]] = []
        self._post_hooks: list[Callable[["HarnessState", dict[str, Any]], Awaitable[None]]] = []
        self.state = HarnessState()

    def reset(self) -> None:
        """重置状态，每次新请求调用。"""
        self.state = HarnessState()

    def add_pre_hook(self, hook: Callable[..., Awaitable[bool]]) -> None:
        """添加执行前拦截钩子。返回 False 则阻止执行。"""
        self._pre_hooks.append(hook)

    def add_post_hook(self, hook: Callable[..., Awaitable[None]]) -> None:
        """添加执行后钩子（审计、日志等）。"""
        self._post_hooks.append(hook)

    # ── 步骤管理 ──

    def can_continue(self) -> bool:
        """检查是否可以继续下一轮循环。"""
        return self.state.step_count < self.max_steps

    def step(self) -> None:
        """记录一个步骤。"""
        self.state.step_count += 1
        self.state.estimated_tokens += EST_TOKENS_PER_LLM_CALL
        logger.debug(
            "Harness step %d/%d, estimated_tokens=%d/%d",
            self.state.step_count,
            self.max_steps,
            self.state.estimated_tokens,
            self.token_budget,
        )

    # ── Token 预算 ──

    def consume_tool_result(self, result_chars: int) -> None:
        """记录工具返回结果的 token 消耗。"""
        estimated = max(result_chars // 2, EST_TOKENS_PER_TOOL_RESULT)
        self.state.estimated_tokens += estimated

    def is_token_exhausted(self) -> bool:
        """Token 预算是否耗尽。"""
        return self.state.estimated_tokens >= self.token_budget

    def should_compress(self) -> bool:
        """是否应该触发上下文压缩（借鉴 Hermes 的相对阈值触发）。"""
        return self.state.estimated_tokens >= int(self.token_budget * TOKEN_WARN_RATIO)

    # ── 循环检测 ──

    def record_tool_call(self, tool_name: str) -> bool:
        """记录工具调用，检测是否陷入循环（连续3次相同调用）。

        Returns:
            True 如果检测到循环，False 如果正常。
        """
        self.state.tool_call_history.append(tool_name)
        if len(self.state.tool_call_history) >= 3:
            last_three = self.state.tool_call_history[-3:]
            if len(set(last_three)) == 1:
                logger.warning("检测到循环调用: %s (连续%d次)", tool_name, len(last_three))
                return True
        return False

    # ── 拦截链 ──

    async def run_pre_hooks(self, context: dict[str, Any]) -> bool:
        """执行所有 pre-hook，任何一个返回 False 则阻止。"""
        for hook in self._pre_hooks:
            try:
                if not await hook(self.state, context):
                    return False
            except Exception as exc:
                logger.error("pre-hook 执行异常: %s", exc)
        return True

    async def run_post_hooks(self, context: dict[str, Any]) -> None:
        """执行所有 post-hook。"""
        for hook in self._post_hooks:
            try:
                await hook(self.state, context)
            except Exception as exc:
                logger.error("post-hook 执行异常: %s", exc)

    # ── 超时 ──

    def elapsed_seconds(self) -> float:
        """返回从请求开始到当前的耗时（秒）。"""
        return time.time() - self.state.start_time

    # ── 状态快照 ──

    def snapshot(self) -> dict[str, Any]:
        """返回当前状态快照，用于日志和调试。"""
        return {
            "step_count": self.state.step_count,
            "max_steps": self.max_steps,
            "estimated_tokens": self.state.estimated_tokens,
            "token_budget": self.token_budget,
            "elapsed_seconds": round(self.elapsed_seconds(), 2),
            "recent_tool_calls": self.state.tool_call_history[-5:],
        }
