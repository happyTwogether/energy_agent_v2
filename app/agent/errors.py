"""Agent 运行时异常。"""


PUBLIC_INTERNAL_ERROR = "服务器内部错误，请稍后重试"
PUBLIC_MODEL_ERROR = "模型服务暂时不可用，请稍后重试"
PUBLIC_MODEL_TIMEOUT_ERROR = "模型服务响应超时，本次请求未完成，请稍后重试或联系管理员检查模型服务"
PUBLIC_TOOL_ERROR = "工具执行失败，请稍后重试"
PUBLIC_MAX_ITERS_ERROR = "本次分析未能在执行限制内完成，请稍后重试；管理员可查看模型与工具调用日志"
PUBLIC_INTERRUPTED_ERROR = "Agent 执行已中断，请重试"
PUBLIC_MODEL_PROTOCOL_ERROR = "模型未按工具协议返回有效结果，本次查询未完成，请重试"


class AgentRuntimeError(RuntimeError):
    """Agent 运行时基础异常。"""


class AgentPublicError(AgentRuntimeError):
    """已完成脱敏，可安全返回客户端的 Agent 错误。"""

    status_code = 500
    code = "agent_error"


class ConversationNotFound(AgentPublicError):
    status_code = 404
    code = "conversation_not_found"

    def __init__(self):
        super().__init__("会话不存在或不属于当前用户")


class ConversationConflict(AgentPublicError):
    status_code = 409
    code = "conversation_conflict"

    def __init__(self):
        super().__init__("会话已被另一请求更新，请重新加载后再试")


class AgentConfigurationError(AgentRuntimeError):
    """Agent 配置不完整或不合法。"""


class ModelDeadlineExceeded(AgentRuntimeError):
    """所有模型调用累计等待超出本次请求预算。"""


class ModelProtocolError(AgentRuntimeError):
    """模型输出不符合执行协议，不得直接展示或进入工具历史。"""


class ToolArgumentsProtocolError(ModelProtocolError):
    """原生工具调用参数不是 JSON 对象，不得进入会话历史或执行工具。"""

    def __init__(self, tool_name: str, argument_chars: int, reason: str):
        super().__init__("模型工具参数格式无效")
        self.tool_name = tool_name
        self.argument_chars = argument_chars
        self.reason = reason
