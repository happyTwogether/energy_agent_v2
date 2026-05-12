"""提示词管理模块。"""

from app.prompts.energy_saving import ENERGY_SAVING_INTENT_PROMPT, ENERGY_SAVING_SUMMARY_PROMPT
from app.prompts.sql_generation import SQL_GENERATION_PROMPT

__all__ = [
    "ENERGY_SAVING_INTENT_PROMPT",
    "ENERGY_SAVING_SUMMARY_PROMPT",
    "SQL_GENERATION_PROMPT",
]
