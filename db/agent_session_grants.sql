-- 在 DB_NAME 指定数据库中，由 schema/表所有者或管理员执行。
-- 只需将下面 YOUR_APP_DB_USER 改为实际 DB_USER；不在这里填写密码。
-- 先执行 agent_session_state.sql；不会给应用账号 CREATE、DROP 或全库权限。
DO $session_grants$
DECLARE
    app_role text := 'YOUR_APP_DB_USER';
BEGIN
    IF app_role = 'YOUR_APP_DB_USER' THEN
        RAISE EXCEPTION '请先把 YOUR_APP_DB_USER 改为实际 DB_USER';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = app_role) THEN
        RAISE EXCEPTION '应用数据库角色不存在';
    END IF;
    IF to_regclass('jd_agent.agent_conversations') IS NULL OR to_regclass('jd_agent.agent_session_states') IS NULL THEN
        RAISE EXCEPTION '会话表尚未就绪，请先完成建表，不要删除原有问答表';
    END IF;
    EXECUTE format('GRANT USAGE ON SCHEMA jd_agent TO %I', app_role);
    EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE jd_agent.agent_conversations, jd_agent.agent_session_states TO %I', app_role);
END;
$session_grants$;
