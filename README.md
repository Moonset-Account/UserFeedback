# cow-gc — 写时复制存储的块回收模块

固定块数的写时复制（copy-on-write）块存储与回收模块。块可引用其他块形成树，
快照根、读者固定（pin）和写入中的暂存预留共同决定块的存活；回收只复用
不再被任何根、读者或暂存写入引用的块，并给出每个不可回收块的原因。
仅使用 Rust 1.95.0 标准库，无第三方依赖。

## 公开 API（`src/lib.rs`）

类型：`Store`、`Block`、`BlockId` / `WriteId` / `ReaderId`、`Error`、
`Reason`、`GcReport`、`RecoveryReport`。

### 写入路径（预留 → 写内容 → 发布根）
- `Store::new(capacity)` — 创建固定块数的空存储。
- `begin_write() -> WriteId` — 开启一次写入事务。
- `reserve(txn) -> Result<BlockId>` — 预留最小编号的空闲物理块；满则 `OutOfSpace`。
- `write_block(txn, block, data, refs)` — 向本事务预留的块写内容；`refs` 只能指向
  已发布块或本事务自留块，否则 `InvalidRef`。
- `publish(txn, name, root)` — 原子发布：该次预留全部转为已用，`name` 指向新根。
- `cancel(txn)` — 取消写入，只释放该次实际预留；**幂等**，重复取消不会二次释放；
  已提交的写入返回 `TxnCommitted`。

### 快照与读者
- `open_reader(snapshot) -> ReaderId` — 打开读者并固定当前根；删除快照名后旧读者
  仍能完成读取。
- `read_block(reader, block)` / `reader_root(reader)` / `close_reader(reader)`。
- `delete_snapshot(name)` — 只删除名字，不动读者固定的块。
- `snapshots()` — 当前已发布根。

### 回收与可分配状态
- `gc() -> GcReport` — 回收所有不再被引用的已用块（活动写入的预留块绝不动）。
- `allocatable() -> GcReport` — 不修改状态地查看：可分配的空闲块列表，以及每个
  被占用块的持有原因（`Reason::SnapshotRoot` / `ReachableFromSnapshot` /
  `ReaderPin` / `ReachableFromReader` / `ReservedByWrite` / `StagingRoot`）。

### 持久化与恢复
- `checkpoint() -> Vec<u8>` — 序列化全部状态（块、根、事务、读者固定）。
- `Store::recover(bytes) -> (Store, RecoveryReport)` — 恢复并分类：
  `published`（已发布根）、`staging_writes`（可继续的暂存写入）、
  `orphaned_reservations`（无主预留，恢复时归还空闲池）。
  恢复时做结构校验（引用不出界、不指向空闲块、根/读者指向已用块等）。

## 复现步骤

```sh
cargo test --offline    # Rust 1.95.0，无第三方依赖，离线即可
```

## 验证范围（全部以真实测试执行为准，14 个集成测试全部通过）

正常路径
- `write_publish_read_roundtrip`：预留→写→发布→读回。
- `reader_survives_snapshot_deletion`：删名后旧读者完成读取；读者关闭后块才可回收。
- `cancel_releases_only_own_reservations`：取消只释放本次预留，其他写入不受影响。
- `gc_reports_held_reasons`：输出可分配状态与逐块不可回收原因。

边界
- `zero_capacity_store`：容量为 0 时预留报 `OutOfSpace`。
- `exhausted_store_recovers_after_cancel`：写满后新写入失败，取消后精确释放并可再分配。
- `invalid_operations_are_rejected`：越界/他人块引用、未写内容发布、取消已提交事务、
  非法快照名等全部报错。
- `corrupt_checkpoint_is_rejected`：垃圾字节与截断检查点被拒绝。

恢复 / 故障语义
- `recovery_classifies_published_staging_and_orphaned`：区分已发布根与可继续暂存写入，
  恢复后暂存写入可继续发布。
- `recovery_returns_orphaned_reservations_to_free_pool`：事务记录已取消而块仍标记预留
  的崩溃窗口，恢复时识别为无主预留并归还空闲池。
- `cancel_remains_idempotent_across_recovery` / `duplicate_cancel_is_idempotent_no_double_free`：
  取消事件重复处理（含跨恢复重放）不会二次释放。

交错场景
- `interleaved_writes_and_gc`：两个并发写入一提交一取消，删旧快照后 GC 只回收不再
  共享的旧根，共享子块保留。
- `acceptance_snapshot_withdrawn_reader_open_shared_child_two_checkpoints`（题面验收序列）：
  快照被撤下、旧读者未结束、共享子块被新写引用；在两个持久化切点重开——
  切点 1（暂存写入在途、读者打开）核对旧读结果与零回收；切点 2（新根已发布、
  读者已关闭）核对首个可复用块为旧根块 1，且下一次预留精确复用它。

未执行项：无。以上测试均已实际运行通过（`cargo test --offline`：14 passed）。
