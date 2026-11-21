# probe — 节点代理的探测状态机

一个事件驱动的探测状态机，用于节点代理根据探针结果控制单个容器实例能否接流量。

## 需求映射

| 题面要求 | 实现 |
|---|---|
| 新实例先只发启动探针 | `Dispatch(Startup)` 在启动成功后返回不可用；启动成功前 `Dispatch(Readiness)` / `Dispatch(Liveness)` 返回 `ok=false` |
| 一次启动成功后才开放就绪和存活探针 | `apply` 处理启动成功时置 `startupDone=true`，此后就绪/存活才可派发 |
| 就绪连续成功两次才接流量 | `ReadinessSuccessesToServe = 2`；连续成功计数达到 2 才 `ready=true` |
| 一次就绪失败就摘流 | 任意就绪失败立即 `ready=false` |
| 启动或存活各自连续失败三次重重启 | `FailureRestartThreshold = 3`，按族独立计数 |
| 每类探针从 1 顺次派号 | `Dispatch` 按族分配 `Seq`，从 1 开始 |
| 完成可乱序，同类按派发顺序生效 | `Deliver` 先缓冲乱序回执，用 `nextApply` 按序号顺序依次应用 |
| 一批同时触发重启与就绪变化，只发布重启和必要的摘流 | `Deliver` 检测到重启后抑制就绪事件，仅返回 `TrafficDisabled`（若原本在接流量）加 `Restart` |
| 新实例从头探测，旧实例迟到回执不得提前接流量 | 重启递增 `Instance` 并重置全部状态；`Deliver` 丢弃 `Instance` 不匹配的回执 |

## 公开 API

包名 `probe`（模块 `probe`，Go 1.20）。

### 类型

- `type ProbeType uint8` — 探针族：`Startup`、`Readiness`、`Liveness`，带 `String()`。
- `type Probe struct { Instance uint64; Type ProbeType; Seq uint64 }` — 一次派发。
- `type Receipt struct { Instance uint64; Type ProbeType; Seq uint64; Success bool }` — 一次回执。
- `type EventKind uint8` — `TrafficDisabled`、`TrafficEnabled`、`Restart`，带 `String()`。
- `type Event struct { Kind EventKind; Instance uint64 }` — 对外发布事件。`TrafficEnabled`/`TrafficDisabled` 的 `Instance` 为状态变化的实例；`Restart` 的 `Instance` 为新实例代次。
- `type Snapshot struct { ... }` — 只读状态快照。
- `type Controller struct` — 状态机本体，并发安全（内部 `sync.Mutex`）。

### 函数与方法

- `NewController() *Controller` — 创建初始实例（代次从 1 开始）。
- `(*Controller) Instance() uint64`
- `(*Controller) Ready() bool`
- `(*Controller) StartupDone() bool`
- `(*Controller) Snapshot() Snapshot`
- `(*Controller) Dispatch(t ProbeType) (Probe, bool)` — 派发下一条探针；当前族不可用时 `ok=false`。
- `(*Controller) Deliver(receipts ...Receipt) []Event` — 原子地送入一批回执，返回该批产生的对外事件。

### `Deliver` 的事件语义

- 无重启时：最多返回一个流量事件，表示该批的**净**就绪变化（`TrafficEnabled` 或 `TrafficDisabled`）；净变化为无则返回 `nil`。
- 有重启时：返回 `TrafficDisabled`（仅当批次开始时实例在接流量）+ `Restart`，该批中的就绪变化被抑制。
- 被忽略的回执：`Instance` 不匹配当前代次、`Type` 非法、`Seq==0`、`Seq` 从未派发、`Seq` 已应用（重复）。

## 复现步骤

在空工作区中仅使用本地已安装的 Go 1.20.2 与标准库，无需网络与依赖：

```sh
cd <workspace>
go version          # 期望 go1.20.2
go test ./...       # 功能测试
go test -race ./... # 竞态检测
```

## 验证范围

`probe_test.go` 覆盖以下场景（正常 / 边界 / 交错），均含真实断言：

- 正常：启动门控、就绪两次连续成功接流、就绪一次失败摘流。
- 边界：就绪失败重置连续计数；启动失败计数被成功重置；存活成功重置失败计数；未接流量时存活三连败只发重启；未知/重复/非法序号回执被忽略；序号快照。
- 交错（乱序）：就绪回执乱序但按派发顺序生效（`success,failure,success` 不接流）；启动回执乱序；并发投递下仍正确接流。
- 重启交互：一批同时含「就绪转接流」与「触达重启」时只发布重启；批次开始时在接流量则额外发布一次摘流；同批净就绪变化折叠为单个事件。
- 新实例隔离：上一实例的迟到启动/就绪回执不会让新实例提前接流、不会标记启动完成、不会污染就绪连续计数；新实例各类探针从 1 重新派号。

### 未验证 / 未包含

- 本实现只提供状态机与纯逻辑，不含真实探针调度、网络探测、容器重启的实际执行或时序循环；这些属于调用方职责。
- 未包含基于真实 pod/容器运行时或真实网络目标的集成测试；测试全部为本地合成的回执输入。
- 未做基准测试或长时间稳定性测试。
