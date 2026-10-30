-- v3：框架会话工作状态。原有 jd_agent.agent_conversations 不变。
-- 使用管理员/表所有者账号在 DB_NAME 指定的数据库执行，不使用应用账号。
-- 应用默认 DB_AUTO_CREATE_TABLES=false；已有问答表不删除、不重建。
CREATE TABLE IF NOT EXISTS jd_agent.agent_session_states (
    conversation_id VARCHAR(64) PRIMARY KEY,
    user_id VARCHAR(128) NOT NULL,
    state JSON NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS ix_jd_agent_agent_session_states_user_id
    ON jd_agent.agent_session_states (user_id);
