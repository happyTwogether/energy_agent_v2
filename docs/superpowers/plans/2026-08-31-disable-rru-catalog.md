# Disable RRU/AAU Catalog Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将两张 RRU/AAU 资产表从当前自助查询目录中移除，同时保留未来重新开放时可复用的通用查询引擎能力。

**Architecture:** 表级开放范围继续由 `config/business_data_catalog.yaml` 唯一控制；移除表配置和关系后，目录预热、模糊搜索和规划器都不再看到 RRU/AAU 资产。不删除 `rru_item` 粒度和 `one_to_many_latest_snapshot` 关系类型，避免将业务范围收缩变成无关的引擎重构。

**Tech Stack:** Python 3.11、Pytest、PyYAML、SQLAlchemy Core、Markdown。

## Global Constraints

- 当前授权目录从 14 张表缩减为 12 张表。
- 四张日报表继续显式登记 54、34、63、38 个字段，合计 189 个。
- 不向 `information_schema.columns` 请求 `lte_nrm_inventoryunitrru` 或 `sa_nrm_inventoryunitrru` 的元数据。
- 保留查询引擎中的 `rru_item` 和 `one_to_many_latest_snapshot` 通用能力。
- 不暂存、修改或提交无关的 `.superpowers/` 目录。

---

### Task 1: 收缩活跃目录并同步运维契约

**Files:**
- Modify: `tests/test_business_catalog.py`
- Modify: `tests/fixtures/business_catalog_rows.py`
- Modify: `tests/test_business_relationships.py`
- Modify: `tests/test_business_query_service.py`
- Modify: `config/business_data_catalog.yaml`
- Modify: `app/self_service/service.py`
- Modify: `docs/operations/business-data-self-service.md`
- Modify: `docs/superpowers/specs/2026-08-27-offline-schema-on-demand-query-design.md`
- Modify: `docs/superpowers/plans/2026-08-31-lean-business-data-tool.md`

**Interfaces:**
- Consumes: `BusinessCatalogStore.get_or_load(session)` 返回的 `CatalogSnapshot.tables` 和 `CatalogSnapshot.relationships`。
- Produces: 只含 12 张当前开放表的目录快照；`FakeSession.requested_table_names` 记录元数据 SQL 的 `table_names` 参数以便验证预热范围。

- [x] **Step 1: 写失败测试**

修改 `FakeSession` 保存每次 `execute` 收到的 `params["table_names"]`，并用下面的行为断言取代“加载并搜索 RRU 表”旧测试：

```python
@pytest.mark.asyncio
async def test_catalog_excludes_deferred_rru_inventory_tables(
    policy_path: Path,
    settings: Settings,
) -> None:
    session = FakeSession()
    store = BusinessCatalogStore(policy_path=policy_path, settings=settings)

    snapshot = await store.get_or_load(session)

    assert "lte_nrm_inventoryunitrru" not in snapshot.tables
    assert "sa_nrm_inventoryunitrru" not in snapshot.tables
    assert "lte_detail_to_rru" not in snapshot.relationships
    assert "nr_detail_to_rru" not in snapshot.relationships
    assert "lte_nrm_inventoryunitrru" not in session.requested_table_names
    assert "sa_nrm_inventoryunitrru" not in session.requested_table_names
```

将字典字段数测试改为只断言四张日报表的 `54/34/63/38`，并断言活跃配置中没有两张 RRU/AAU 表。

- [x] **Step 2: 运行测试并验证 RED**

Run: `PYTHONPATH=. .venv/bin/pytest tests/test_business_catalog.py -q`

Expected: FAIL，失败原因是当前 YAML 仍包含两张 RRU/AAU 表及关系，元数据请求也仍包含它们。

- [x] **Step 3: 实现最小目录变更**

从 `config/business_data_catalog.yaml` 删除 `lte_nrm_inventoryunitrru`、`sa_nrm_inventoryunitrru`、`lte_detail_to_rru` 和 `nr_detail_to_rru`。从测试元数据夹具删除两张表的虚拟字段，删除只验证 RRU 关系展开的测试；不修改 `app/self_service/models.py` 或 `app/self_service/query.py` 中的通用粒度/关系实现。

- [x] **Step 4: 同步用户可见文案和项目文档**

从 `app/self_service/service.py` 的可查主题提示中删除“RRU/AAU资产”，并删除 `tests/test_business_query_service.py` 中把 RRU 文案写死的旧断言。从部署 SQL 删除两行 `GRANT SELECT`，将目录说明更新为“四张日报表 189 个字段”，删除 AAU 序列号验收问题。在旧设计和实施计划顶部增加 2026-08-31 范围变更提示并链接新规格，避免读者误认 RRU 表仍在当前目录中。

- [x] **Step 5: 运行聚焦测试并验证 GREEN**

Run: `PYTHONPATH=. .venv/bin/pytest tests/test_business_catalog.py tests/test_business_relationships.py tests/test_business_query.py tests/test_business_query_service.py -q`

Expected: PASS；目录快照、元数据请求和文档对当前范围的表述一致。

- [x] **Step 6: 运行全量验证**

Run: `PYTHONPATH=. .venv/bin/pytest -q`

Expected: 全部通过，不出现新的 error 或 warning。

- [x] **Step 7: 提交实现**

```bash
git add config/business_data_catalog.yaml app/self_service/service.py tests/test_business_catalog.py tests/fixtures/business_catalog_rows.py tests/test_business_relationships.py tests/test_business_query_service.py docs/operations/business-data-self-service.md docs/superpowers/specs/2026-08-27-offline-schema-on-demand-query-design.md docs/superpowers/plans/2026-08-31-lean-business-data-tool.md docs/superpowers/plans/2026-08-31-disable-rru-catalog.md
git diff --cached --check
git diff --cached
git commit -m "chore(data): defer RRU asset catalog"
```
