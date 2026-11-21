# rowtable — 关系引擎的 MVCC 内存行表

以主键读取的内存行表，支持开始事务、读写、删除、提交、回滚与垃圾回收（GC）。
纯本地基础设施组件，无网络、无外部依赖，仅使用 Go 标准库（Go 1.20）。

## 实际入口

| 文件 | 内容 |
| --- | --- |
| `table.go` | 全部实现：`Table` / `Txn` / `Version` |
| `table_test.go` | 验收测试（Go 内置 `testing`） |
| `go.mod` | 模块定义（`module rowtable`，`go 1.20`） |

### 公开 API

- `NewTable() *Table` — 创建空行表。
- `(*Table).Begin() *Txn` — 开始事务，快照固定为当前已提交的最新状态。
- `(*Txn).Get(key) ([]byte, bool)` — 按主键读：先看自己的未提交修改，再看快照时已提交的版本。
- `(*Txn).Put(key, value)` / `(*Txn).Delete(key)` — 写 / 删，提交前仅本事务可见。
- `(*Txn).Commit() error` — 提交；快照建立后同一行已被他人提交则返回 `ErrConflict`（首提交者胜出），事务作废。
- `(*Txn).Rollback()` — 回滚，丢弃未提交修改并释放读视图。
- `(*Table).GC()` — 回收所有活动读视图与“此刻新开的事务”都看不到的旧提交版本。
- `(*Table).Versions(key) []uint64` — 查看该键当前保留的已提交版本号（升序）。
- `(*Table).CommitSeq() uint64` / `(*Txn).Snapshot() uint64` — 查看全局提交序号 / 事务快照序号。

## 并发与回收语义

- **快照隔离**：读事务只能看到开始时已提交的版本，另可看到自己的修改。
- **冲突拒绝**：并发写（含写 vs 删除）同一行时，后提交的一方若其快照建立后该行已被别人提交，则提交被拒绝（`ErrConflict`）。
- **GC 保留集**：版本 v 被保留，当且仅当存在某个活动读视图或“此刻新开事务”的视图 s，使 v 是 s 下可见的最新版本（`v.CommitID <= s < 下一版本.CommitID`）。回收不改变任何活动视图或新事务读到的值 / 删除状态。

## 验证范围（`go test ./...`，全部真实断言）

- `TestBasicTxnLifecycle` — 可接受结果：写/读/删/提交/回滚闭环；已关闭事务拒绝操作。
- `TestSnapshotIsolationAndOwnWrites` — 读事务固定在开始时的版本；自己的未提交修改仅自己可见。
- `TestWriteConflictFirstCommitterWins` — 应被拒绝的结果：并发写同一行，后提交方被 `ErrConflict` 拒绝；交错边界：写不同键互不冲突、快照在对方提交之后则不冲突。
- `TestDeleteWriteConflict` — 并发删除与并发写同一行同样冲突。
- `TestGCReleasesVersionsAsReadersFinish` — 两个长读事务夹着一次删除和一次原键重建：回收后三版本保留且各视图读值正确；结束 R1 释放原始值版本，结束 R2 释放删除墓碑版本。
- `TestGCKeepsLatestTombstoneForNewTxns` — 最新版本为墓碑时，新事务在回收后仍读到删除状态。
- `TestGCWithSharedSnapshot` — 多视图共享同一快照序号时按计数保留，逐一结束后才释放。

## 运行

```sh
go test ./...        # 或 go test ./... -v
```

备注：本次交付仅在工作区内构建与核验；`go vet` 未获执行权限，未运行，测试通过 `go test ./...` 验证。
