# 多版本键值存储（Dynamo 风格向量时钟）

两个存储节点断开时可各自更新同一对象。本实现提供对象的多版本读取、
带读取上下文的写入与删除，以及节点间状态合并。

## 实现入口

- `versioned_store.py`
  - `VectorClock`：向量时钟，支持 `dominates` / `dominates_or_equal` /
    `is_concurrent` / `merge`。
  - `VersionedStore(node_id)`：单节点存储。
    - `get(key) -> (values, context)`：多版本读取，并发值并列返回，
      同时返回读取上下文（向量时钟）。
    - `put(key, value, context)` / `delete(key, context)`：带读取上下文的
      写入/删除。只替代上下文覆盖（调用者确实读过）的版本；与上下文并发
      的版本保留为兄弟。删除生成墓碑版本，参与因果比较。
    - `merge_from(other)` / `merge_snapshot(snapshot)`：合并另一节点或一份
      快照的状态；取版本并集后剪除被严格支配的版本，被覆盖的旧值不会从
      迟到的旧副本复活。
    - `has_concurrent_delete(key)`：识别墓碑与存活版本并存的并发删除。

## 测试入口

```bash
python3 -m unittest discover -s tests -v
```

运行时为 Python 3.11.6，全部用例执行真实断言。

## 验证范围（tests/test_versioned_store.py，11 个用例）

- 可接受结果：分区两节点并发写同一对象，合并后两个值并列读出
  （`test_concurrent_writes_both_returned_after_merge`）。
- 应被阻止的结果：
  - 过时上下文写入不得替代未读过的并发版本
    （`test_stale_context_does_not_replace_unread_concurrent_version`）；
  - 被覆盖/已删除的旧值不得从迟到旧副本复活
    （`test_repeated_exchanges_in_different_orders`、
    `test_stale_snapshot_does_not_revive_after_delete`、
    `test_delete_wins_over_old_value_no_resurrection`）。
- 边界/交错场景：
  - 上下文只覆盖部分兄弟时只替代读过的版本
    （`test_partial_context_replaces_only_versions_actually_read`）；
  - 一边删除已读版本、另一边并发改出新值，合并后新值可读且能识别并发删除
    （`test_concurrent_delete_and_write_merge_keeps_new_value`）；
  - 客户端读全冲突再写一次收敛为单值
    （`test_client_read_all_conflicts_then_write_converges`）；
  - 三节点按不同顺序反复交换状态不复活旧值
    （`test_repeated_exchanges_in_different_orders`）；
  - 向量时钟支配/并发关系的单元断言（`TestVectorClock`）。
