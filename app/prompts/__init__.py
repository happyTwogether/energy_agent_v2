"""提示词管理模块。"""

from app.prompts.energy_saving import (
    AGENT_EXECUTION_PROMPT,
    SYNTHESIS_GENERAL,
    SYNTHESIS_MAP,
    get_synthesis_prompt,
)

__all__ = [
    "AGENT_EXECUTION_PROMPT",
    "SYNTHESIS_GENERAL",
    "SYNTHESIS_MAP",
    "get_synthesis_prompt",
]
