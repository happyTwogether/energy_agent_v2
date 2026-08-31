"""完全离线自然语言业务数据自服务工具。"""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.self_service.service import get_business_data_query_service

TOOL_DESCRIPTION = (
    "使用自然语言查询授权业务表中的任意字段。适用于现有专业工具未覆盖的通用数据查询；"
    "支持最多3张授权表按审核关系筛选、排序、分组和基础聚合，不接受SQL。"
)
TOOL_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "question": {
            "type": "string",
            "description": "用户当前完整的数据查询问题，不要改写成SQL",
        },
        "export_excel": {
            "type": "boolean",
            "description": "是否导出Excel",
            "default": False,
        },
    },
    "required": ["question"],
}


async def query_business_data(
    db: AsyncSession,
    question: str,
    export_excel: bool = False,
) -> dict[str, Any]:
    normalized_question = question.strip()
    if not normalized_question:
        message = "请说明需要查询的业务数据。"
        return {
            "success": False,
            "error": message,
            "clarification_required": True,
        }
    return await get_business_data_query_service().query(
        db=db,
        question=normalized_question,
        export_excel=export_excel,
    )
