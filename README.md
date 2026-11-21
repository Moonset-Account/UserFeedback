# readymux — 逻辑事件驱动的就绪多路复用器

`readymux` 是平台运行时使用的本地基础设施组件（Go 1.20，纯标准库，无外部依赖、无网络、
无 OS 文件描述符）。它把"资源是否可读/可写"这类就绪事实抽象为逻辑事件：资源侧用
`SetReady` 报告当前状态，调用方用 `Poll` 在限额内取走就绪通知。

## 实际入口

| 入口 | 位置 | 说明 |
| --- | --- | --- |
| `readymux.New()` / `var m readymux.Mux` | `mux.go:162` | 创建多路复用器；零值可直接使用 |
| `(*Mux).Add(Subscription)` | `mux.go:171` | 登记资源 ID、读写关注掩码、Level/Edge 模式、可选 OneShot |
| `(*Mux).SetReady(id, Events)` | `mux.go:226` | 资源侧报告绝对就绪状态（读位/写位） |
| `(*Mux).Poll(maxEvents)` | `mux.go:290` | 一次最多取 `maxEvents` 条事件；`<=0` 不消费 |
| `(*Mux).Rearm(id)` | `mux.go:262` | 一次性订阅重新布防，并重新观察当前状态 |
| `(*Mux).Remove(id)` | `mux.go:204` | 撤销资源；编号可立即复用，旧注册的待取条目失效 |

测试入口：`mux_test.go`（外部测试包 `readymux_test`），全部使用 Go 内置 `testing`。

## 行为约定

- **关注掩码**：`Interest` 必须包含 `Readable` 和/或 `Writable`；事件返回
  `就绪状态 ∩ 关注掩码`。空掩码、未知位、非法模式都会被拒绝（`ErrInvalidInterest`、
  `ErrInvalidMode`、`ErrInvalidEvents`），未知 ID 返回 `ErrNotRegistered`，重复登记返回
  `ErrDuplicateID`。
- **水平触发（Level）**：只要资源持续就绪，每次轮询都会报告。报告后该订阅被追加到就绪
  队列尾部，因此常驻就绪资源不会饿死后面登记的订阅；同一次 `Poll` 中每个注册最多出现
  一次。
- **边沿触发（Edge）**：只把"从非就绪变为就绪"的位（且落在关注掩码内）锁存并交付一次；
  重复报告同一状态不会产生事件，轮询前多次翻转会合并为一次通知。
- **一次性订阅（OneShot）**：首次交付后自动停用，期间 `SetReady` 只更新状态、不产生事件。
  `Rearm` 重新布防时会重新读取当前状态，因此即使资源"已经就绪"也会再次得到通知
  （Level 与 Edge 行为一致）。
- **撤销与编号复用**：`Remove(id)` 后同一编号可立即 `Add` 新对象。队列中的待取条目携带
  每次注册独有的 generation 戳，新注册的 generation 不同，旧条目在 `Poll` 中被静默丢弃，
  绝不会通知给新对象。
- **并发**：所有方法用同一把互斥锁保护，可被多个生产者/轮询 goroutine 并发调用。

## 验证范围

运行：

```
go test ./... -count=1
```

`mux_test.go` 覆盖题面交叉约束：

| 测试 | 覆盖点 |
| --- | --- |
| `TestLevelReportsWhileReady` | 可接受结果：水平触发持续就绪时反复报告；关注掩码不满足时静默 |
| `TestLevelInterestMask` | 掩码裁剪：就绪读写双位、只关注写时只返回 `writable` |
| `TestEdgeOnlyNotifiesNewTransitions` | 边沿只通知新进入就绪的位；重复状态与新翻转 |
| `TestEdgeCoalescesBeforePoll` | 边界：轮询前快速翻转合并为一次通知 |
| `TestLevelFairness` | 题面场景：读端 `10` 常驻可读、写端 `20` 反复切换，多次 `Poll(1)` 核对顺序与掩码；随后 `Poll(10)` 核对整体轮转且不重复 |
| `TestOneShotLevelRearmReobserves` | 一次性订阅停用后不再通知；`Rearm` 后"已经就绪"仍得到通知，并再次自动停用 |
| `TestOneShotEdgeRearmReobserves` | 边沿模式下的一次性重新布防同样重新观察当前状态 |
| `TestRemoveThenReuseIDDiscardsStaleEntries` | 撤销后立即复用编号，旧注册遗留的待取条目不会通知新对象（应被阻止的结果） |
| `TestRemoveThenReuseIDWithLiveQueue` | 同一次轮询里"旧代条目不交付 + 新代条目正常交付"的交错场景 |
| `TestRemoveDropsQueuedLevelEntry` | 撤销后该 ID 上所有操作返回 `ErrNotRegistered`，队列条目惰性失效 |
| `TestRejectedInputs` | 拒绝项：重复 ID、空/越界关注掩码、非法模式、越界就绪位、未知 ID；被拒绝的 Add 不占用编号 |
| `TestPollLimitBoundaries` | 限额边界：`0`/负数不消费；限额大于就绪数时单个水平订阅在一次轮询内不重复 |
| `TestEventsString` / `ExampleMux` | 事件与模式的字符串表示 |
| `TestZeroValueAndConcurrentUse` | 零值可用性；并发 `SetReady` 与 `Poll` 不产生越界 ID 或越界掩码 |

验收包含：至少一个可接受结果（水平触发持续报告、边沿新位通知）、至少一个应被阻止/拒绝
的结果（撤销后复用编号时的陈旧条目、各类非法输入），以及影响两者的边界/交错场景
（轮询前翻转合并、限额轮询中的公平轮转、撤销与复用同批交错）。

## 环境与限制

- 已执行：`go test ./... -count=1 -v`（全部通过）、`go test ./... -race -count=1`（通过）、
  `gofmt -l .`（无输出）、`go version` → go1.20.2。
- 本次会话中 `go vet ./...` 与组合命令被权限策略拒绝，未执行；未绕过权限。
- 全部产物位于当前工作区，仅使用本地合成夹具（内存中的资源 ID 与就绪位），
  不涉及外部目标、真实负载或网络连接。
