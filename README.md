# multipathwrite — 多路径块卷写入协调器

客户端侧写入协调器：同一块设备通过多条路径访问时，协调器负责稳定写入
ID、重叠写的提交顺序、路径断开后的故障切换查询，以及刷盘栅栏语义。

仅依赖 Go 1.20.2 标准库，无外部依赖。

## 公开 API

### 类型与状态

| 名称 | 说明 |
| --- | --- |
| `WriteID` | 协调器分配的稳定写入 ID；故障后重发沿用同一 ID |
| `WriteStatus` | `StatusNotStarted` / `StatusWritten` / `StatusUnknown` 三态 |
| `PathInfo` | 每个路径响应都携带 `DeviceID`（设备身份）与 `Generation`（路径世代） |
| `WriteResult` / `QueryResult` / `ReadResult` / `FlushResult` | 各操作响应，均含产生响应的 `PathInfo` |
| `Path` 接口 | 设备适配层：`Info / Broken / Write / Query / Read / Flush` |
| `ErrPathDisconnected` | 路径传输断开的哨兵错误 |

### 协调器 `Coordinator`

- `NewCoordinator(paths []Path, openPath func() Path) *Coordinator`
  初始路径集合 + 可选的新路径工厂（同设备新世代）。
- `Write(ctx, offset, data) (WriteID, WriteResult, error)`
  分配稳定 ID；不相交区间并行，重叠区间严格按提交顺序串行。
- `Read(ctx, offset, buf) (ReadResult, error)`
  路径断开自动切换到同设备其他/新路径。
- `Flush(ctx) (FlushResult, error)`
  仅当本次调用**之前**的所有写入都已确定且持久、且设备确认刷盘后才返回；
  调用之后排队的写入不属于本次快照，可以继续准备/执行。

### 模拟设备 `SimDevice`（测试用设备适配层）

`NewSimDevice(id, size)` 创建共享内存设备，`NewPath()` 挂载一条新世代路径。
故障注入钩子：

- `Gate(id)`：阻塞指定写入的落盘，直到关闭 gate（用于顺序/交错断言）。
- `LoseReceipt(id)`：写入落盘后路径断开、回执丢失。
- `MarkUnknown(id)` / `ResolveUnknown(id)`：查询进入/退出暂时未知态。
- `FailFirstPath(id)`：首次发送在到达设备前断链，模拟未开始。
- 观测：`AttemptCount`、`HasStarted`、`ApplyOrder`、`Snapshot`。

## 协调规则与故障恢复

1. **稳定 ID 与三态查询**：每条写分配单调 `WriteID`。路径断开导致结果未知时，
   协调器先在**同一设备的新路径**上 `Query(id)`：
   - `StatusWritten`：已落盘，**不再重发**，直接采纳新路径为结果；
   - `StatusNotStarted`：从未落盘，以**原 ID 重新发送**；
   - `StatusUnknown`：退避后在新路径继续查询；在确定之前，该区间的后续重叠
     操作一直被挡住（对应记录永不 finish，`awaitPriors` 持续等待）。
2. **顺序与并行**：按区间 `[offset, end)` 判断重叠。不相交写入立即并行；
   重叠写入等待所有更早的重叠记录 `decided` 后才发送，保证提交顺序。
3. **刷盘栅栏**：`Flush` 对调用时刻的记录快照逐个等待 `decided && durable`，
   然后在健康路径上发 `Flush`；之后新提交的写入不阻塞本次刷盘确认。
4. **路径世代**：每次 `NewPath()` 世代号 +1，所有响应回带设备 ID 与世代号，
   测试据此断言“确实切换到了同设备的新路径”。

## 复现步骤

```bash
go version            # 要求 go1.20.2（本机已验证 darwin/arm64）
go test ./... -count=1 -v
go test ./... -race -count=3
gofmt -l .            # 无输出即格式干净
```

## 测试与验证范围（9 个真实断言测试）

| 测试 | 场景类别 | 真实断言内容 |
| --- | --- | --- |
| `TestNormalWriteReadFlushAndPathIdentity` | 正常 | 写/读/刷盘内容正确，响应携带设备 ID 与世代 1 |
| `TestAdjacentWritesRunInParallel` | 边界 | 半卷相邻写 `[0,4)`/`[4,8)` 同时在途（gate 同步），并行落盘 |
| `TestOverlappingWritesKeepSubmissionOrder` | 交错 | A 未落盘时 B 发送数保持 0；严格应用顺序 1→2→3；C 等 B |
| `TestHalfRangeWriteLostReceiptThenFailoverReadFlushAndProgress` | **题面验收序列** | 覆盖一半区间的写落盘后丢回执：仅发送 1 次（不重发）、路径 1 断链、世代 2 确认、切换后读到 `ABCD`、不相交的后半写继续推进、刷盘在世代 2 确认、整卷 `ABCDEFGH` |
| `TestNotStartedWriteIsResentWithSameID` | 故障恢复 | 首次断链未到达设备 → 同 ID 第 2 次发送成功，世代 2 确认 |
| `TestUnknownBlocksOverlappingRangeButDisjointWriteProceeds` | 故障+交错 | 未知期间重叠写 B 发送数持续为 0，不相交写 C 正常完成；`ResolveUnknown` 后 B 解锁且只发一次 |
| `TestFlushWaitsForPriorWritesWhileLaterWritesPrepare` | 刷盘栅栏 | 前置写未决时刷盘不确认；刷盘排队期间后续不相交写可完成；前置写持久后刷盘才确认 |
| `TestPathQueryReportsAllThreeStatusesWithIdentity` | 适配层契约 | 未开始/未知/已写入三态及解析后状态，响应均带身份 |
| `TestValidationAndExhaustedPathExhausted` | 边界/错误 | 空写、负偏移、越界、空读被拒；全部路径耗尽且无工厂时上下文超时返回而非挂死 |

所有时序断言均通过 gate + 轮询条件（`waitFor` / `assertCountStays` 在时间窗内
持续校验）实现，不依赖预置输出；`go test -race -count=3` 通过，无数据竞争。

## 未执行项

- `go vet ./...`：本环境中该命令需要额外审批，未执行；`go test` 的编译与
  竞态检测已覆盖类型检查与并发安全。
- 未访问任何真实网络目标/账户，全部数据为进程内合成的 `SimDevice`。
