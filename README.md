# online-partition-exchange

关系表的在线分区交换（Rust 1.95.0，仅标准库，无第三方依赖）。

活动表按范围分列（range partition），每个分区可与一张独立的暂存表做两阶段交换：
**准备（prepare）** 阶段分步校验暂存表的键范围与唯一性，**确认（commit）** 阶段处理校验基线之后的改动并原子交换数据。准备期间活动表与暂存表两侧都允许写入；已经打开的跨分区查询固定在打开时点的分区目录与可见版本上，分批取数不会混用目录。

## 构建与验证

```sh
cargo test --offline
```

工具链：rustc/cargo 1.95.0。全部验证为真实执行的集成测试（`tests/exchange_tests.rs`，8 个用例），无预置输出替代。

## 公开 API（`src/lib.rs` / `src/snapshot.rs`）

| 项 | 说明 |
| --- | --- |
| `Database::new` | 空库。 |
| `create_active_table(&[(start, end)]) -> Vec<PartitionId>` | 用有序不重叠的半开区间 `[start, end)` 创建活动表分区。 |
| `create_staging_table() -> StagingId` | 创建独立暂存表（堆结构，物理上允许重复键，由校验拒绝）。 |
| `insert_active(key, value)` / `delete_active(key)` | 活动表写入；准备期间任意时刻可用。 |
| `staging_insert(staging, key, value)` / `staging_delete(staging, key)` | 暂存表写入；删除为墓碑式，不打乱进行中的扫描游标。 |
| `prepare_exchange(partition, staging) -> PrepareId` | 打开准备记录，固定当前边界代际与交换纪元。 |
| `prepare_scan_step(prepare, max_rows) -> ScanStepResult` | 分步校验：每步最多扫描 `max_rows` 行，检查键落在分区范围内且无重复；步间可交错任意写入。扫描完成且无违例时记录校验基线（暂存表版本）。 |
| `prepare_restart(prepare)` | 修正暂存数据后重试同一准备记录（重置游标/违例/基线）。 |
| `prepare_state(prepare) -> PrepareState` | `Scanning / Validated / Failed / Committed`。 |
| `commit_exchange(prepare) -> ExchangeReceipt` | 确认交换：①已提交记录幂等重试，返回原回执；②边界已变（`StaleReason::BoundaryChanged`）或分区已被另一操作交换（`PartitionExchangedByOther`）的旧准备拒绝为 `StalePrepare`；③对基线之后的暂存改动做增量校验（越界/重复，已删除的键豁免），失败则保持 `Validated` 可修正后重试；④通过后原子交换——暂存行成为活动分区，旧分区行成为暂存表，目录纪元 +1。 |
| `redefine_partition_range(partition, start, end)` | 修改分区边界（递增边界代际，使未决准备失效）。 |
| `open_query() -> QuerySnapshot` | 打开跨分区查询，固定打开时点的目录纪元与各分区数据版本。 |
| `QuerySnapshot::fetch_batch(&mut cursor, limit)` | 分批取数；所有批次都来自同一固定目录与版本。 |
| `QuerySnapshot::rows_in_range / all_rows` | 固定快照上的范围/全量读取。 |
| `partition_rows / staging_rows / directory_epoch` | 验证辅助读取。 |

错误类型：`ExchangeError`（含 `StalePrepare`、`DeltaValidationFailed`、`PrepareFailed`、`PrepareNotValidated` 等）；违例类型：`Violation::{OutOfRange, DuplicateKey}`。

## 测试覆盖（验收场景 ↔ 用例）

| 场景 | 用例 |
| --- | --- |
| 分步校验中插入越界行 → 修正 → 重扫 → 交换；核对暂存表、活动表、交换前后两个打开时点的查询 | `stepwise_scan_out_of_range_insert_fix_then_exchange` |
| 交换交错于分批取数中途，已打开查询不混用目录；交换后的迟写入不渗入旧快照 | `batched_query_pinned_across_interleaved_exchange` |
| 边界修改后旧准备被拒绝 | `stale_prepare_rejected_after_boundary_change` |
| 分区被另一操作交换后旧准备被拒绝；已提交准备幂等重试 | `stale_prepare_rejected_after_other_exchange` |
| 基线之后的越界/重复插入在确认时被增量校验拦截；修正后同一准备重试成功 | `commit_delta_validates_post_baseline_changes` |
| 扫描检出暂存表物理重复键 | `scan_detects_duplicate_keys` |
| 边界：空暂存表交换、未完成的扫描不能确认 | `boundary_cases_empty_staging_and_unfinished_scan` |
| 非法区间定义与无归属键写入被拒绝 | `invalid_ranges_and_orphan_keys_rejected` |

## 设计要点

- **MVCC 固定**：分区数据为写时复制的 `Arc<BTreeMap>`；查询打开时克隆各分区的 `Arc`，之后任何写入/交换都不影响已打开查询。
- **原子可见**：交换在单次锁内完成数据互换并递增目录纪元，新查询一次性看到新目录。
- **分步校验的正确性**：暂存删除用墓碑实现，扫描游标（行下标）不会因删除而错位；扫描期间追加的行会被后续步骤看到；基线之后的一切改动由确认时的增量校验兜底。
- **准备记录可重试**：扫描失败可 `prepare_restart` 重扫；确认因增量违例失败后保持 `Validated` 可修正重试；已提交的确认幂等返回原回执。
- **旧准备失效**：准备记录固定 `(boundary_gen, exchange_epoch)`，边界变更或另一交换都会使二者失配，确认时以 `StalePrepare` 拒绝。

## 验证范围说明

- 并发模型为单进程内 `Mutex` 串行化 + 快照隔离；测试以交错时序（扫描步间、批次间、基线后）复现并发效果，未使用多线程压力测试。
- 未覆盖项：多活动表、持久化崩溃恢复、网络协议层。这些不在题面范围内。
