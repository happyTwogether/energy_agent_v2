"""完全离线自然语言业务数据自服务工具。"""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.self_service.service import get_business_data_query_service

TOOL_DESCRIPTION = (
    "唯一通用数据查询入口，使用自然语言读取授权物理字段和确定性计算指标。"
    "适用：仅查询字段值或指标值，以及单/多小区明细、多表记录、统计、趋势、"
    "分组、比较、排序、Top N和导出；最多使用3张有审核关系的表，不接受SQL。"
    "不适用：直接生成节电空间、扩展收缩决策、异常诊断、参数合规结论或固定报告。"
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
