# distinctagg —— 支持多组集的 DISTINCT 聚合器

批计算引擎使用的内存内 + 落盘 DISTINCT 聚合器（Go 1.20.2，仅标准库，无外部依赖）。

## 功能与题面要求的对应

| 题面要求 | 实现位置 | 验证测试 |
| --- | --- | --- |
| 输入带稳定行序号 | `Row.ID`（`config.go`），成员以 `map[int64]struct{}` 累计 | 全部测试核对 `MemberRowIDs` |
| 分组键集合（多组集） | `Config.GroupingSets`，每行同时进入全部组集，组间以组集编号前缀隔离 | `TestFixtureDistinctAggregation` 的“组集来源”段 |
| 多个各自独立的 DISTINCT 输入列组合 | `Config.Distincts`，每组为每个 spec 维护独立去重集合 | `TestNullArgExcluded`、夹具 |
| 未选择的列与原本值为 NULL 的列区分 | `OutputRow.SelectedBitmap`：选中列即便值为 NULL 位图仍为 true，未选中列恒为 false | 夹具中 `nullRegion` 组的位图断言 |
| DISTINCT 任一参数为 NULL 则该行不计入该组合 | `ingestRows` 中 `nullArg` 短路；成员与其他组合不受影响 | `TestNullArgExcluded`、夹具行 3/8 |
| 内存预算到点分成多个落盘段 | 每行写入前检查估算内存，到点即整体溢写为一个 gob 段并清空 | `TestCrossSegmentDedup`、夹具 |
| 同一去重值跨段/跨分区不重复计算 | 合并阶段对编码值做 map 并集，重复值只计一次 | `TestCrossSegmentDedup`、`TestCrossPartitionDedup` |
| 分区重做只接受获选尝试的整组片段 | 片段先写 `staging/attempt-N/`，提交时一次原子 rename 到 `committed/attempt-N/`；合并只扫描 committed | `TestRetryDiscardsUncommittedSegments`、`TestSegmentStoreCommitAtomicity` |
| 已写完未确认的旧片段不污染结果 | 失败尝试的暂存目录整体 abort 删除；编号单调递增，获选尝试之外永不被读取 | 同上，且夹具断言 `BytesWritten > SpilledBytes` |
| 无数据时对空组集产出计数为零的一行 | `mergeAndBuild` 的空结果分支 | `TestEmptyInputZeroRow`（nil 分区与空分区两种入口） |
| 半途溢写失败夹具 | `FaultHook` 三个注入点：写前/写后未确认/提交前 | 夹具在 `PhaseAfterWrite` 注入一次失败 |

## 公开 API

- `Value` / `NullValue` / `StringValue` / `IntValue` / `FloatValue` / `BoolValue`：带类型标记与显式 NULL 状态的标量值。
- `Row{ID int64, Values []Value}`：带稳定行序号的输入记录。
- `GroupingSet{Name string, KeyColumns []int}`：组集；`KeyColumns` 为空即空组集。
- `DistinctSpec{Name string, ArgColumns []int}`：独立 DISTINCT 组合。
- `Config{GroupingSets, Distincts, MemoryBudgetBytes, MaxAttempts, WorkDir, FaultHook}`。
- `New(cfg Config) (*Engine, error)`：校验配置并构造引擎；`(*Engine).Close()` 清理自建临时目录。
- `(*Engine).Run(ctx, partitions [][]Row) (*Result, error)`：分区下标即分区编号，分区并发处理，失败自动重做。
- `Result.Rows []OutputRow`（按 `(SetID, 组键)` 确定性排序）与 `Result.Stats`。
- `OutputRow`：`SetName`、`GroupValues`、`SelectedBitmap`、`DistinctCounts`、`MemberRowIDs`、`Count(specName)`。
- `(*Result).FindRow(setName, group...)`：按组集名与原始组键值查找输出行。
- `Stats`：`Partitions / AttemptsTotal / Retries / SpillSegments / BytesWritten / SpilledBytes / PeakLiveBytes / PeakLiveExceeded`。
- `SpillPhase`（`PhaseBeforeWrite / PhaseAfterWrite / PhaseBeforeCommit`）与 `FaultHook`：故障注入点，仅测试需要。

## 落盘目录布局与两阶段提交

```
<WorkDir>/p<partition>/
  staging/attempt-N/seg-K.gob    # 未确认片段，任何合并都不会读取
  committed/attempt-N/seg-K.gob  # 获选尝试的整组片段，合并的唯一来源
```

- 尝试编号在分区内单调递增；一次尝试的所有段先全部落 staging。
- `commitAttempt` 用一次 `os.Rename` 原子切换整个尝试目录；`abortAttempt` 递归删除暂存目录。
- 合并只调用 `committedSegments(winner)`，因此旧尝试/未确认片段在结构上不可能被读到。

## 内存预算语义

预算采用确定性的固定开销估算（`mem.go`），不依赖 Go 运行时的真实堆行为：

- 每行写入**前**检查估算内存，达到预算即把整份状态溢写为一段并清空；
- 一行会同时进入全部组集与 DISTINCT 组合，因此峰值可能短暂超过预算，
  但超出量不超过单行最大增量（测试用 `maxRowGrowth` 独立计算容差），下一行开始前状态必已清空；
- `Stats.PeakLiveExceeded` 显式报告峰值是否严格超过预算。

## 复现步骤

```sh
# 环境要求：go1.20.2（本仓库 go.mod 声明 go 1.20，无外部依赖）
go version

# 全部功能/边界/故障恢复/并发测试
go test ./...

# 综合夹具的详细统计输出
go test -run TestFixtureDistinctAggregation -v ./...

# 并发交错与数据竞争检查
go test -race -count=1 ./...
```

## 验证范围

`go test -race -count=1 ./...` 实际执行并通过的测试：

1. `TestFixtureDistinctAggregation` —— 题面指定综合夹具：
   同一行同时进入三种组集（空组集 / region / region+product）、两项独立 DISTINCT、
   跨分区重复值、一次“段已写完但未确认”的半途溢写失败；断言输出计数、组集来源、
   位图 NULL 语义、重做编号与预算统计（含 `BytesWritten > SpilledBytes`）。
2. `TestEmptyInputZeroRow` —— 无数据（nil 分区列表与空分区两种入口）时空组集零计数行。
3. `TestNullArgExcluded` —— NULL 参数跳过对应 DISTINCT 组合但不影响成员与其他组合。
4. `TestCrossSegmentDedup` —— 极小预算强制多段，跨段重复值只计一次。
5. `TestCrossPartitionDedup` —— 三分区重复值只计一次，成员行号取并集。
6. `TestRetryDiscardsUncommittedSegments` —— 失败后重做，committed 下仅留获选尝试。
7. `TestAllAttemptsFail` —— 持续故障时重做上限生效、错误上抛、无暂存残留。
8. `TestSegmentStoreCommitAtomicity` —— 存储层两阶段提交的直接单元测试。
9. `TestContextCancellation` —— 上下文取消快速失败。
10. `TestConcurrentPartitionsRace` —— 4 分区并发处理，配合 `-race` 检查竞争。
11. `TestInvalidConfig` / `TestRowWidthMismatch` —— 配置与行宽边界校验。

所有断言均针对引擎真实执行结果；测试只使用本地合成数据与临时目录，
不访问网络、不读取凭据、不安装任何依赖。
