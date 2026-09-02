# 完全离线业务数据自服务部署说明

## 工作原理

业务人员仍从智能体自然语言入口提问。Toolkit 只暴露一个通用数据入口 `query_business_data`：无论单小区字段、网络汇总指标、分组 Top N 还是 Excel 导出，都由该工具受控执行。节电空间、异常诊断、参数核查和固定报告仍由专业工具负责。

应用启动时使用独立只读账号读取授权表的字段元数据并缓存；请求期间内部模型只生成结构化计划，不生成 SQL。程序校验表、字段、白名单关系和结果粒度后，用 SQLAlchemy 构造一条参数化业务查询。

查询最多使用 3 张授权表、2 条白名单关系，并最多展开一种一对多明细。自由 SQL 始终禁用。

## 物理字段与计算指标

- 物理字段来自 `config/business_data_catalog.yaml`，只能查询数据库预热后证实存在的白名单字段。
- 计算指标来自 `app/self_service/metrics.py`，注册表固定来源字段、单位、粒度、Python 公式和分组聚合口径。大模型只选指标 ID，不生成公式。
- 比例和平均时长在分组查询中按“聚合分子 ÷ 聚合分母”计算，不对每行比例再求平均。

## 创建最小权限账号

以下 SQL 使用 `.env.example` 中的 Schema：`energy_monthreport`、`jd_agent`、`energysavingrules`。若部署环境的 `DB_SCHEMA`、`DB_SCHEMA_AGENT`、`DB_SCHEMA_RULE` 配置不同，DBA 必须先替换为实际值并逐表核对。

```sql
\prompt 'energy_agent_reader password: ' reader_password
CREATE ROLE energy_agent_reader LOGIN PASSWORD :'reader_password';
GRANT CONNECT ON DATABASE agent_db TO energy_agent_reader;
GRANT USAGE ON SCHEMA energy_monthreport, jd_agent, energysavingrules
TO energy_agent_reader;
GRANT SELECT ON TABLE
  energy_monthreport.lte_report_day_detail,
  energy_monthreport.nr_report_day_detail,
  energy_monthreport.lte_report_day_collect,
  energy_monthreport.nr_report_day_collect,
  energy_monthreport.lte_fix_prm,
  energy_monthreport.nr_fix_prm,
  jd_agent.jd_cell_expansion_day,
  jd_agent.jd_cell_constriction_day,
  jd_agent.jd_cell_detail_hour_nr,
  jd_agent.jd_cell_pre_hour_busy,
  jd_agent.jd_cell_around,
  energysavingrules.eng_check_result
TO energy_agent_reader;
ALTER ROLE energy_agent_reader SET default_transaction_read_only = on;
ALTER ROLE energy_agent_reader SET statement_timeout = '10s';
```

密码不得写入仓库、镜像或日志。通过部署环境注入：

```dotenv
SELF_SERVICE_ENABLED=true
SELF_SERVICE_DATABASE_URL=postgresql+asyncpg://energy_agent_reader:实际密码@数据库地址:5432/agent_db
SELF_SERVICE_QUERY_TIMEOUT_MS=10000
SELF_SERVICE_DEFAULT_LIMIT=50
SELF_SERVICE_MAX_LIMIT=500
SELF_SERVICE_EXPORT_MAX_ROWS=10000
```

## 完全离线依赖

- 复用现有 `LLM_BASE_URL`、`LLM_API_KEY` 和 `DEFAULT_MODEL` 指向的内网 OpenAI-compatible 模型。
- 不需要 Embedding 模型、向量数据库、Metabase 或任何外网服务。
- 内网镜像需安装 `requirements.txt` 中固定的 `PyYAML==6.0.3`；本次改造不新增依赖。

## 授权目录维护

授权表和关系位于 `config/business_data_catalog.yaml`。数据库新增字段不会在运行中自动开放：

当前目录已按《节能平台能耗日报数据字典-20251113》显式登记四张日报表 189 个字段。RRU/AAU 资产表当前未开放。规划器只会看到问题命中的少量字段及其中文名、业务说明、类型和单位。

1. DBA 确认字段不包含敏感信息；
2. 在目录 `fields` 中显式补充英文名、中文名、业务说明、别名和单位；数据库 comment 只用于 DBA 核对，不会自动开放字段；
3. 如需多表查询，明确关联键、时间规则、基数、默认聚合和允许粒度；
4. 增加目录、关系和 SQL 构造测试；
5. 重启应用刷新目录快照。

禁止把会话表、审计表、账号表或密钥配置表加入授权目录。

## 启动检查

启动日志应满足：

- `information_schema.columns` 只在应用启动时读取一次；
- 缺失授权表输出 warning，错误关系不进入快照；
- 目录预热失败不会影响原有专业工具启动；
- 每次自助查询日志只有一次 planner 调用和一次最终业务 SELECT；
- 日志记录表名、关系 ID、粒度、字段、行数和分段耗时，不记录 SQL、连接 URL 或完整筛选值。

## 预发布验收问题

```text
查5G小区460-00-2539193-71在2026年8月24日的深度休眠时长和深度休眠开关
查2026年8月24日各5G地市的流量，按流量从高到低取前5名
查长沙市5G网络最近7天的能耗趋势并生成折线图
查小区460-00-2539193-71的扩展时段、需收缩时段和周边小区距离
分析小区460-00-2539193-71的节电空间
同时按逐小时和每个邻区展开
查询会话表所有内容
```

前四题取数阶段只允许调用 `query_business_data`，第三题随后调用 `generate_chart`，第五题调用 `analyze_single_cell_energy`。双明细轴、未授权表和无白名单关系必须在执行 SQL 前被拒绝。
