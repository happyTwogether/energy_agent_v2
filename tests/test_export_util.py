"""Excel 导出工具测试。"""

import openpyxl

from app.utils import export_util


def test_export_sheets_creates_named_worksheets(tmp_path, monkeypatch):
    """多类原始证据写入同一工作簿的独立工作表。"""
    monkeypatch.setattr(export_util, "EXPORT_DIR", str(tmp_path))

    url = export_util.export_sheets_to_excel(
        {
            "小区汇总": [{"cgi": "a"}],
            "扩展明细": [
                {
                    "cgi": "a",
                    "hour_detail": [{"hour": 22, "low_flow_pct": 90.0}],
                }
            ],
            "空工作表": [],
            "收缩明细": [{"cgi": "a", "prb_increase": 10.0}],
        },
        prefix="batch_analysis",
    )

    workbook_path = next(tmp_path.glob("*.xlsx"))
    workbook = openpyxl.load_workbook(workbook_path, read_only=True)
    assert workbook.sheetnames == ["小区汇总", "扩展明细", "收缩明细"]
    assert '"low_flow_pct": 90.0' in workbook["扩展明细"]["B2"].value
    assert workbook["收缩明细"]["B2"].value == 10.0
    assert url is not None
