# blockdev — 带写缓冲的虚拟块设备

在“只支持按连续块写入 + 一次持久化屏障”的后端之上，实现一层易失写缓冲（write-back
cache）。调用方可以交错发起普通写、读、全卷刷盘、以及要求本次写持久化的请求；另提供掉电
重开入口丢弃全部易失状态。

Go 1.20.2，无第三方依赖。

## 实际入口

| 入口 | 位置 | 语义 |
| --- | --- | --- |
| `NewBackend(blocks, blockSize)` | `backend.go` | 构造零填充后端；参数非正返回 `ErrInvalidGeometry` |
| `(*Backend).WriteRange(start, data)` | `backend.go` | 仅接受整块、范围内、单段连续的写入；只进入易失 pending 区，记入轨迹 |
| `(*Backend).Barrier()` | `backend.go` | 唯一持久化屏障：把全部 pending 落盘，记入轨迹 |
| `(*Backend).PowerCycle()` | `backend.go` | 丢弃 pending（未过屏障的写），持久内容不动 |
| `(*Backend).Durable()` / `ReadBlock` / `Trace` / `PendingBlocks` / `Barriers` / `ResetTrace` | `backend.go` | 观测入口：持久快照、单块读、操作轨迹、pending 块、屏障计数 |
| `Open(backend)` | `device.go` | 给后端挂一个空缓存，得到设备 |
| `(*Device).Write(start, data)` | `device.go` | 普通写：只进缓存并标脏；同块后写覆盖前写 |
| `(*Device).Read(start, count)` | `device.go` | 读最新值：缓存优先，未命中回落到持久层 |
| `(*Device).Flush()` | `device.go` | 全卷刷盘：所有脏块合并成极大连续范围下发，末尾一个屏障 |
| `(*Device).WriteDurable(start, data)` | `device.go` | 单次写持久化：只提交本请求覆盖的块 + 一个屏障，其余脏块保持缓冲 |
| `(*Device).PowerCycle()` | `device.go` | 掉电重开：清空缓存、脏集合与后端 pending |
| `(*Device).DirtyBlocks()` | `device.go` | 观测入口：当前脏块（升序） |

所有设备访问以整块为单位。非整块长度返回 `ErrUnaligned`，越界返回 `ErrOutOfRange`；
被拒绝的操作不改变设备或后端状态，也不进入轨迹。

## 交叉约束

- 缓存内同块后写覆盖前写；`Read` 始终返回最新值，且缓冲区中不保留已持久化的副本，
  因此持久化后的读直接落到真实盘面内容。
- `Flush` 把**全部**脏块按连续范围合并（极大连续段，每段一次 `WriteRange`），最后只发
  **一个** `Barrier`；无脏块时不产生任何后端操作。
- `WriteDurable` 只提交本请求覆盖的块：合并范围严格限制在该请求内，其余早期脏块原样留
  在缓冲；随后仅这些被覆盖的块出缓冲并转清。
- `PowerCycle` 丢弃设备缓存、脏集合与后端未过屏障的 pending；只有已过屏障的内容存活。

## 验证

测试入口为真实断言的 Go 内置 `testing` 用例：

```
go test ./... -race
```

`device_test.go`：

- `TestWriteBufferOverwriteAndReadLatest` — 可接受路径：缓冲写立即可读、同块覆盖、
  刷盘前后端的持久内容仍是原值。
- `TestFlushCoalescesContiguousRanges` — 两个不相邻脏区刷盘合并为两段 `WriteRange`
  加一个 `Barrier`；空刷盘不产生后端操作。
- `TestWriteDurableKeepsOtherDirtyRegionVolatile` — **核心掉电演练**：写脏两个不相邻区
  域，只持久化其中一处后掉电；被持久化区保留新值，另一处读回原盘内容；轨迹证明合并范围
  只覆盖本次请求。
- `TestWriteDurableCommitsOnlyCoveredBlocksInsideDirtyRun` — 边界/交错：单个连续脏段中
  只持久化中间一块，其上下仍留脏，后续全卷刷盘在干净空洞处断成两段。
- `TestInterleavedWritesReadsFlushDurable` — 交错写/读/全卷刷盘/单次写持久化逐步核对
  最新值，掉电后回落到已刷盘状态。
- `TestRejectsUnalignedAndOutOfRange` — 应被拒绝的结果：非整块、空、负起点、越界、跨尾
  的写与非法读均被拒，且不触碰后端、不标脏。
- `TestWriteDurableSupersedesBufferedValue` — 单次写持久化覆盖缓冲中的同块旧值，邻块仍
  易失。

`backend_test.go`：

- `TestBackendWriteRangeValidationAndTrace` — 后端只接受整块、范围内、单段写入，轨迹逐
  条精确匹配。
- `TestBackendBarrierThenPowerCycle` — 屏障前不持久、掉电丢弃未过屏障写、越过屏障的内
  容存活。
- `TestFlushDropsCacheSoReadsSeeDurableContent` — 刷盘后不留干净副本，读直达盘面。
- `TestConcurrentAccessIsSafe` — 并发读写安全（配合 `-race`）。

## 本地变异核对（仅记录，未并入产物）

为确认用例不是空转，临时注入两类缺陷并重跑，均被用例判负后已恢复源码：

1. `WriteDurable` 顺带提交其余脏块 → `TestWriteDurableKeepsOtherDirtyRegionVolatile`、
   `TestWriteDurableCommitsOnlyCoveredBlocksInsideDirtyRun` 失败（轨迹中多出范围）。
2. `PowerCycle` 保留易失缓存 → 上述掉电用例及 `TestInterleaved…`、
   `TestWriteDurableSupersedesBufferedValue` 失败（掉电后读到缓冲值）。

最终 `go test ./... -race` 通过。

## 范围

仅本地基础设施组件：无外部目标、无网络、无真实负载，验证只用普通项目单元测试与本地合成
夹具。
