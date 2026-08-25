"""生产节电查询索引脚本契约测试。"""

from pathlib import Path


INDEX_SQL = Path("db/indexes/2026-08-25_energy_query_indexes.sql")


def test_energy_query_index_script_is_concurrent_idempotent_and_complete():
    sql = INDEX_SQL.read_text(encoding="utf-8")
    normalized = " ".join(sql.lower().split())

    assert "begin" not in normalized
    assert "commit" not in normalized
    assert normalized.count("create index concurrently if not exists") == 6
    for table_and_columns in (
        "jd_cell_expansion_day (cgi, stat_time)",
        "jd_cell_constriction_day (cgi, stat_time)",
        "jd_cell_detail_hour_nr (cgi, stat_time, hours)",
        "jd_cell_pre_hour_busy (cgi, stat_time)",
        "eng_check_result (cgi, check_time)",
        "jd_cell_around (cgi, around_cgi)",
    ):
        assert table_and_columns in normalized
