-- PostgreSQL 生产索引：每条语句需在非事务会话中独立执行。
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_expansion_cgi_stat_time
    ON jd_agent.jd_cell_expansion_day (cgi, stat_time);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_constriction_cgi_stat_time
    ON jd_agent.jd_cell_constriction_day (cgi, stat_time);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_detail_hour_nr_cgi_stat_time_hours
    ON jd_agent.jd_cell_detail_hour_nr (cgi, stat_time, hours);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_pre_hour_busy_cgi_stat_time
    ON jd_agent.jd_cell_pre_hour_busy (cgi, stat_time);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_eng_check_result_cgi_check_time
    ON energysavingrules.eng_check_result (cgi, check_time);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_cell_around_cgi_around_cgi
    ON jd_agent.jd_cell_around (cgi, around_cgi);
