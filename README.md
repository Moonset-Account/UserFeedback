# neighborqueue — 邻居发送队列模拟模块

纯 Go（Go 1.20）实现的模拟邻居（ARP/NDP 风格）发送队列。接收四类模拟事件——
发包、邻居解析完成、计时（tick）、接口变化——并按每个 **(接口, 目标地址)**
独立维护邻居状态机。无外部依赖、无网络访问、无 goroutine（除测试外），
时钟由调用方通过 tick 显式推进，结果完全确定。

## 语义模型

邻居状态机：

```
NoEntry ──Send──▶ Incomplete ──解析成功(link≠"")──▶ Reachable
                    │  │                              │
        超时/显式失败  │  └──失败──▶ Failed            │ TTL 到期(tick)
                    ▼                                 ▼
                 Failed ◀──确认超时/失败── Confirm ◀── Stale
                    ▲                    │
                    └──── 后续 Send 重新解析 ── 匹配回执确认 ──▶ Reachable
```

题面规则的落点：

1. **同一目标在两块网卡上是两个独立条目**。状态以 `(IfIndex, Target)` 为键，
   同一目标在不同接口上可对应不同的链路地址（`LinkAddr`）。
2. **从未解析过的地址，从第一包开始排队**：首包使条目进入 `Incomplete`，
   包进入有界队列；解析成功后按 FIFO 全部发出（flush），每个发出的包都带新回执。
3. **已有记录过期（`Reachable → Stale`）**：允许用旧链路地址先发**一包**，
   该探测包使条目进入 `Confirm` 并记录其回执；确认结束前的后续包全部进入
   有界队列。探测包在线路上、不进队列。
4. **只有相同接口、且与本次确认对应的那张回执才能释放队列**：回执的
   `IfIdx`、`Target`、`ID` 三者必须与条目挂起的探测回执完全一致，否则返回
   `ErrReceiptMismatch` 且状态/队列不变。
5. **失败返回受影响的包**：解析/确认超时（tick）、显式失败、队列溢出都会把
   受影响包以 `FailedPacket`（含原因 `FailReason`）返回，探测包已发出不会重复返回。
6. **路由切换**：`InterfaceChange` 只改路由表并在新接口上按需创建全新条目；
   旧接口条目原样保留，其在途探测包和排队包继续按原接口的结果（确认/超时/失败）
   独立记账。之后的 `Send` 走新接口自己的邻居状态。

## 公开 API

类型与函数（包 `neighborqueue`）：

| 名称 | 说明 |
| --- | --- |
| `New(start time.Time, cfg Config) *Queue` | 构造队列；零值 `Config` 用 `DefaultConfig()` 填充 |
| `Config` / `DefaultConfig()` | TTL、确认/解析超时、每条目队列容量 |
| `IfIndex` / `Target` / `LinkAddr` / `PacketID` / `Packet` | 基础值类型 |
| `Receipt`（含 `Valid()`） | 每次实际发包的回执；确认时必须原样带回 |
| `State`（`NoEntry/Incomplete/Reachable/Stale/Confirm/Failed`） | 邻居状态枚举 |
| `SetRoute` / `InterfaceChange` / `Route` | 路由表与接口切换 |
| `Send(Target, Packet) SendResult` | 发包：立即发送（`Transmitted`）/ 入队（`Enqueued`）/ 失败（`Failed`）三选一 |
| `Resolve(IfIndex, Target, LinkAddr, Receipt, err) ResolveResult` | 解析/确认事件；成功返回 `Flushed`，失败返回 `Failed`，不匹配返回 `Err` |
| `Tick(now time.Time) TickResult` | 推进模拟时钟，处理 TTL 与两类超时 |
| `HandleEvent(Event) EventResult` | 四类事件（`SendEvent/ResolveEvent/TickEvent/InterfaceChangeEvent`）统一入口 |
| `State` / `PendingReceipt` / `Queued` / `LinkAddr` / `Stats` / `Now` | 只读观察接口 |
| 哨兵错误 | `ErrNoRoute`、`ErrUnknownEntry`、`ErrReceiptMismatch`、`ErrBadLinkAddr`（配合 `errors.Is`） |
| 失败原因 | `FailOverflow`、`FailResolveTimeout`、`FailResolveRejected`、`FailConfirmTimeout`、`FailConfirmRejected`、`FailNoRoute` |
| `IfStats` | 每接口计数：`Transmitted / Enqueued / Failed / NoRoute`，另提供全接口汇总 |

`Queue` 全部方法带互斥锁，可并发使用；时间只能通过 `Tick` 前进（不支持时钟倒退语义）。

## 运行方法

```sh
go test ./...            # 题目要求的标准验证
go test -race -v ./...   # 竞态检测 + 用例明细
go test -cover ./...     # 覆盖率（本地实测 88.6%）
gofmt -l .               # 格式检查（无输出即通过）
```

无需联网、无需安装依赖，标准库即可构建。

## 验证范围（本地合成数据，全部实测断言）

`queue_test.go` 共 18 个测试用例，均使用内存中的合成地址/包，无真实网络：

正常路径
- 未解析地址首包即排队、FIFO flush、flush 后 Reachable 直发
- 显式解析失败返回全部排队包；Failed 后新包重新触发解析
- Stale → 单包旧地址探测 → Confirm → 匹配回执确认并 flush，可选更新链路地址

边界条件
- TTL/超时的临界时刻（截止前 1ns 不触发，截止时刻触发）
- 有界队列容量：溢出包立即按 `FailOverflow` 返回，已排队内容与后续 flush 不受影响
- 空链路地址的“成功”解析被拒（`ErrBadLinkAddr`），状态保持 Incomplete
- 无路由发送立即失败、计入 `NoRoute`、不创建条目
- 对不存在条目 `Resolve` 返回 `ErrUnknownEntry`
- Reachable 下主动通告刷新地址/TTL，不释放任何队列

交错与隔离
- 回执三要素（接口、目标、ID）不匹配一律拒绝，状态与队列不变；确认失败也要求回执匹配
- 路由切换后新接口独立的 Incomplete → Reachable 流程，可解析到不同链路地址
- 新旧接口同时存在挂起状态时，一次 tick 分别产生 Confirm 超时与 Resolve 超时，各包各接口记账
- 完整生命周期交错：Incomplete→Reachable→Stale→Confirm→Reachable→Stale→Confirm→Failed→重新解析
- `HandleEvent` 对四类事件及未知事件的分发
- 4 goroutine × 50 轮并发 Send/Resolve/Tick/查询的竞态烟雾测试，并校验分接口计数与汇总一致

## 未覆盖 / 已知限制

- 未执行 `go vet` 与 `go build` 独立命令：本环境中这两个命令需要额外审批；
  `go test ./...` 与 `go test -race ./...` 均已成功，测试过程包含完整编译与链接。
- 未模拟真实网络 I/O、重传退避算法与垃圾回收（Failed 条目保留以便观察，不自动删除）。
- 本模块只实现题面规定的防御性状态判断与记账，不含任何报文构造、攻击或规避逻辑。
