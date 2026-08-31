# 暂停 RRU/AAU 数据自服务设计

## 目标

当前阶段不对业务人员开放 `lte_nrm_inventoryunitrru` 和 `sa_nrm_inventoryunitrru` 两张 RRU/AAU 资产表，避免目录搜索和自然语言规划器召回当前暂不使用的资产数据。

## 方案

- 从 `config/business_data_catalog.yaml` 删除两张 RRU/AAU 表的授权配置。
- 删除 `lte_detail_to_rru` 和 `nr_detail_to_rru` 两条目录关系。
- 目录启动时不再向 `information_schema` 请求两张资产表的字段元数据。
- 删除部署文档中的 RRU/AAU `GRANT SELECT` 和验收示例。
- 删除仅用于开放这两张表的测试元数据和行为断言。
- 保留查询引擎中的 `one_to_many_latest_snapshot` 和 `rru_item` 通用能力；它们没有目录表和关系时不可被规划器使用，以后重新开放时可以恢复配置而无需重写 SQL 引擎。

## 变更后的运行行为

- 当前授权目录由 14 张表缩减为 12 张表。
- 四张日报表的 189 个字段保持不变。
- 查询 RRU、AAU、资产序列号等主题时，目录不再返回这两张资产表候选。
- 其他日报、工参、节电扩展/收缩、过程证据和参数核查查询不受影响。

## 验证

- 目录快照中不包含两张 RRU/AAU 表和关系。
- 元数据 SQL 的请求参数不包含两张资产表。
- 数据字典中的四张日报表仍精确保持 54、34、63、38 个字段。
- 全量离线测试通过。
