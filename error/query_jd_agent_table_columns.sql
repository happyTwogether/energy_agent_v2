-- 查询节电扩展、节电收缩及邻区关系表的字段结构。
SELECT table_name, column_name, data_type
FROM information_schema.columns
WHERE table_schema = 'jd_agent'
  AND table_name IN (
    'jd_cell_expansion_day',
    'jd_cell_constriction_day',
    'jd_cell_around'
  )
ORDER BY table_name, ordinal_position;
