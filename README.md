# rangedeleg — 范围写委托协调器

一个共享文件服务的**范围写委托**协调器。客户端持有某字节区间的委托时，可在该区间内缓存写入；协调器负责在文件需要缩短时**先收回委托**、保留截断点以下应写回的数据、丢弃截断点以上内容，并在收回确认后禁止旧委托继续写。所有状态持久化到状态目录，控制器重开后继续追踪收回与截断，并向调用方报告截断何时生效。

## 环境

- 工具链：`go1.20.2`（`go version` 已确认为 `go1.20.2 darwin/arm64`）。
- 仅使用 Go 标准库，无外部依赖，无网络访问。

> 说明：`go.mod` 使用 `go 1.20` 指令。Go 1.20 工具链不接受 `go 1.20.2` 形式（会报 `invalid go version '1.20.2': must match format 1.23`，见“验证记录”）。工具链本身仍是题目规定的 1.20.2；`go` 指令只是语言版本行，不改变工具链版本。

## 公开 API

包路径：`example.com/rangedeleg`，源文件 `coordinator.go`。

| 符号 | 说明 |
| --- | --- |
| `Open(dir string) (*Coordinator, error)` | 打开或创建状态目录中的协调器；加载持久化状态。 |
| `(*Coordinator) Close() error` | 关闭；之后所有操作返回 `ErrClosed`（重复 `Close` 幂等）。 |
| `(*Coordinator) Grant(client string, off, length int64) (Delegation, error)` | 授予 `[off, off+length)` 委托。同一客户端同一区间在撤销后重新授予时**复用 DelegationID 并递增 Generation**。 |
| `(*Coordinator) Delegation(id DelegationID) (Delegation, bool)` | 委托快照。 |
| `(*Coordinator) Writeback(client string, ref DelegationRef, commit CommitID, off int64, data []byte) (WritebackResult, error)` | 结算一段写回。段可被分段/重复/乱序投递；按 `(委托, 代次, 提交标识, 偏移, 长度)` 只结算一次。若存在待收回的截断，则在截断点处切分：点以下应用，点及以上丢弃并在 `WritebackResult.Discarded` 报告。 |
| `(*Coordinator) ConfirmRecall(client string, ref DelegationRef) error` | 客户端确认交回委托；该代次进入 `revoked`，不再可写。最后一个待确认项到达时截断生效。 |
| `(*Coordinator) Truncate(size int64) (*TruncationHandle, error)` | 请求缩短到 `size`。立即返回句柄；**先对相交委托发出收回**，在所有确认到达前截断未生效（读者仍见旧长度/旧字节）。 |
| `(*Coordinator) Extend(size int64) error` | 扩展文件；新增区间读为全零。存在待生效截断时返回 `ErrTruncationPending`。 |
| `(*Coordinator) Read(off int64, n int) ([]byte, error)` / `Length() int64` / `Snapshot() []byte` | 读取当前可见镜像。 |
| `(*Coordinator) TruncationStatus(seq uint64) (TruncationStatus, bool)` / `PendingTruncations()` | 查询截断状态 / 未生效截断列表。 |
| `(*Coordinator) WaitTruncation(ctx, seq) (TruncationStatus, error)` | 阻塞直到截断生效或 ctx 结束；跨 `Open` 重开仍有效。 |
| `(*TruncationHandle) Status()` / `Wait(ctx)` | 针对单个截断的状态与等待。 |

### 关键语义

- **收回先行**：`Truncate` 找到与丢弃区 `[target, 当前长度)` 相交的 `active` 委托，置为 `recalling` 并登记待确认项；确认齐备后才把长度降到 `target`。
- **截断未生效时**：`Length`/`Read` 仍返回旧值；跨截断点的写回被切分为 `Accepted`（`< target`）与 `Discarded`（`>= target`）。
- **收回确认后**：该代次 `revoked`；任何写回（包括重放旧回执）返回 `ErrDelegationRevoked`，即使之后 `Extend` 也不得复活原字节。
- **扩展补零**：`Extend` 用全零填充新增区间，且仅在无待生效截断时允许。
- **幂等结算**：同一 `(委托, 代次, 提交标识, 偏移, 长度)` 的重复段返回 `Duplicate=true` 且不重复应用；同键不同内容返回 `ErrConflict`。
- **重开续追踪**：状态（委托、已结算账本、截断队列、文件镜像）落盘于 `dir/state.json`（原子重命名写入），`Open` 后继续处理待确认收回与队列中的截断。
- **多次缩短**：未生效的截断目标须严格递减（否则 `ErrTruncationOrder`）；队列中每个目标成为队首时才针对其区间发出收回。
- **授权代次**：`Writeback` 必须携带当前 `Generation`，代次不符返回 `ErrStaleGeneration`，避免旧回执冒充新委托。

## 复现步骤

在空工作区执行（无需 `go mod tidy`、无网络）：

```sh
go version                 # 期望 go1.20.2 ...
gofmt -l .                 # 期望无输出
go test -count=1 ./...     # 期望 ok
go test -race -count=1 ./... # 竞态检测
go test -v -count=1 ./...  # 逐项结果
```

## 验证范围

以下均为**真实执行的测试**（见下方运行记录），覆盖正常、边界与交错场景：

| 测试 | 场景类型 | 覆盖点 |
| --- | --- | --- |
| `TestNormalWritebackSegmentsOutOfOrderAndDuplicated` | 正常 | 批分多段、乱序、重复段只结算一次；同提交标识不同偏移视为不同段。 |
| `TestDuplicateWithDifferentContentsConflicts` | 边界 | 同键不同内容 → `ErrConflict`，不覆盖已结算数据。 |
| `TestWritebackValidation` | 边界 | 空段、越界（前/后）、错客户端、未知 ID、过期代次；末字节边界允许。 |
| `TestTruncatePreservesBelowPointAndDiscardsAbove` | 正常 | 跨截断点批次切分；点下保留、点上丢弃；整段在点上丢弃；确认后长度生效。 |
| `TestRevokedGenerationCannotWriteAndExtensionReadsZero` | 正常+边界 | 确认后旧代次禁止写；重放旧回执被拒；扩展区间读零；重新授予换代，旧回执不能冒充。 |
| `TestTwoIntersectingRangesShrinkThenExtend` | **交错（题目要求）** | 两个相交待写区间 + 一次缩短后随即扩展：核对保留字节与读者可见长度、重复段、逐段结算、扩展补零、旧回执不复活。 |
| `TestReopenContinuesRecallAndTruncation` | 故障恢复 | `Close` 后 `Open` 继续追踪待确认收回与截断队列；重开后结算切分写回；`WaitTruncation` 报告生效时刻。 |
| `TestTruncationOrderingAndPendingGuards` | 边界 | 非缩短、负数、队列目标递减、待生效时禁止扩展/越界授予；队列顺次生效。 |
| `TestConcurrentWritebacksSettleOnceInterleaved` | 并发交错 | 多 goroutine 三段投递同一批，仅两段为重复，最终字节正确。 |
| `TestClosedCoordinator` | 边界 | 关闭后各操作返回 `ErrClosed`。 |
| `TestZeroLengthAndReadBounds` | 边界 | 零长授予/读取、越界读。 |

未覆盖/未执行：多进程并发写同一状态目录（本设计为单写者“控制器”）；崩溃于 `rename` 前后的一致性压力测试（`rename` 提供原子替换，但未做断电注入）；大规模性能基准。

## 验证记录（真实运行输出）

环境：`go version go1.20.2 darwin/arm64`。

- `go test -count=1 ./...` → `ok  	example.com/rangedeleg	0.795s`
- `go test -race -count=1 ./...` → `ok  	example.com/rangedeleg	0.875s`
- `go test -v -count=1 ./...` → 11 个测试（含子测试）全部 `PASS`，见下表。

逐项 PASS（与上表一一对应）：`TestNormalWritebackSegmentsOutOfOrderAndDuplicated`、`TestDuplicateWithDifferentContentsConflicts`、`TestWritebackValidation`（6 子测试）、`TestTruncatePreservesBelowPointAndDiscardsAbove`、`TestRevokedGenerationCannotWriteAndExtensionReadsZero`、`TestTwoIntersectingRangesShrinkThenExtend`、`TestReopenContinuesRecallAndTruncation`、`TestTruncationOrderingAndPendingGuards`、`TestConcurrentWritebacksSettleOnceInterleaved`、`TestClosedCoordinator`、`TestZeroLengthAndReadBounds`。

关于 `go.mod`：曾以 `go 1.20.2` 写入，`go test` 报
`go: errors parsing go.mod: ... invalid go version '1.20.2': must match format 1.23`，
故改为 `go 1.20`；工具链仍为 1.20.2。

## 文件

- `coordinator.go` — 实现。
- `coordinator_test.go` — 测试（真实断言，无预置输出）。
- `go.mod` — `module example.com/rangedeleg`，`go 1.20`。
