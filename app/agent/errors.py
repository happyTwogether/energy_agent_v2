"""Agent 运行时异常。"""


PUBLIC_INTERNAL_ERROR = "服务器内部错误，请稍后重试"
PUBLIC_MODEL_ERROR = "模型服务暂时不可用，请稍后重试"
PUBLIC_TOOL_ERROR = "工具执行失败，请稍后重试"
PUBLIC_MAX_ITERS_ERROR = "Agent 已达到最大执行步数，请简化问题后重试"
PUBLIC_INTERRUPTED_ERROR = "Agent 执行已中断，请重试"


class AgentRuntimeError(RuntimeError):
    """Agent 运行时基础异常。"""


class AgentPublicError(AgentRuntimeError):
    """已完成脱敏，可安全返回客户端的 Agent 错误。"""


class AgentConfigurationError(AgentRuntimeError):
    """Agent 配置不完整或不合法。"""
