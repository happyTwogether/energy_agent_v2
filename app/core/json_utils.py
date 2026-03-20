"""JSON 工具模块。

提供支持 Decimal、date、datetime 类型的 JSON 序列化工具。
"""

import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any


class DecimalEncoder(json.JSONEncoder):
    """支持 Decimal、date、datetime 类型的 JSON 编码器。"""

    def default(self, obj: Any) -> Any:
        if isinstance(obj, Decimal):
            return float(obj)
        if isinstance(obj, (date, datetime)):
            return obj.isoformat()
        return super().default(obj)


def dumps_decimal(obj: Any, **kwargs: Any) -> str:
    """使用 DecimalEncoder 序列化对象为 JSON 字符串。

    Args:
        obj: 要序列化的对象。
        **kwargs: 传递给 json.dumps 的其他参数。

    Returns:
        JSON 字符串。
    """
    return json.dumps(obj, cls=DecimalEncoder, **kwargs)
