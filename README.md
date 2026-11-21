# h2sender — HTTP/2 发送端流控调度组件

纯本地、无网络依赖的 HTTP/2 发送端流控模拟器。多个流排队待发 DATA 帧时，
组件按流编号升序轮询可发流，每轮每个流最多产出一个非空 DATA 帧，帧大小同时
受四项约束：

1. 该流的待发字节数；
2. 对端公布的最大帧长（`SETTINGS_MAX_FRAME_SIZE`，合法区间 16384..16777215）；
3. 连接级流控窗口余额（单独维护）；
4. 流级流控窗口余额（允许为负）。

## 实际入口

模块：`h2sender`（见 `go.mod`，Go 1.20，无第三方依赖）。
实现文件：`sender.go`；验收测试：`sender_test.go`。

| 入口 | 作用 |
| --- | --- |
| `NewSender(Config) (*Sender, error)` | 构造发送端；零值字段回落到 RFC 7540 默认值（初始窗口 65535、最大帧长 16384），非法配置直接报错 |
| `(*Sender).OpenStream(id)` / `CloseStream(id)` | 打开/关闭流；新流窗口取当前初始窗口设置 |
| `(*Sender).Enqueue(id, length)` | 排队 `length` 字节待发数据（只记账，不持有真实负载） |
| `(*Sender).ApplyInitialWindowDelta(delta)` | 初始流窗口设置按**差值**作用到全部活动流，流余额允许变负；连接余额不受影响 |
| `(*Sender).UpdateStreamWindow(id, inc)` | 流级 WINDOW_UPDATE，**只恢复该流** |
| `(*Sender).UpdateConnectionWindow(inc)` | 连接级 WINDOW_UPDATE，只恢复连接余额 |
| `(*Sender).SetMaxFrameSize(size)` | 更新最大帧长，带区间校验 |
| `(*Sender).Flush() []Frame` | 执行完整轮询调度，返回本次产出的非空 DATA 帧序列（`StreamID/Length/Remaining`）；无帧可发时返回空切片 |
| `(*Sender).Balances() (connWindow, []StreamBalance)` | 输出连接余额及各流的窗口余额、待发字节（按流编号升序） |
| `(*Sender).StreamBalance(id)` / `ConnectionWindow()` 等 | 单项状态查询 |

关键语义：

- **轮询规则**：每轮按流编号升序扫描，每流最多一帧；一轮无任何帧产出即结束，
  说明所有流都被待发数据或某个窗口卡住。
- **负余额**：初始窗口下调时按差值直接扣减活动流余额，可为负；负余额流在
  `Flush` 中被跳过，直到后续增量把余额恢复到大于 0。
- **作用域隔离**：流级增量只进对应流的余额，连接级增量只进连接余额，
  初始窗口差值不碰连接余额、也不作用于已关闭流。
- **不重发**：字节一旦随帧产出即从待发队列和两个窗口中扣减，重复 `Flush`
  不会再次产出。
- **防御性校验**：非法增量（≤0 或 >2³¹−1）、窗口溢出（>2³¹−1）、未知/已关闭
  流、非法帧长与非法配置均通过哨兵错误（`ErrInvalidIncrement`、
  `ErrWindowOverflow`、`ErrUnknownStream`、`ErrInvalidMaxFrameSize` 等）拒绝。

## 验证范围与结果

仅使用 Go 内置 `testing`，全部为真实断言（逐帧序列、余额、错误哨兵），
无外部目标、无网络、无真实帧字节。执行：

```
go test -v ./...          # 6 个测试函数全部通过
go test -count=2 -race ./...  # 重复运行 + 竞态检测，通过
gofmt -l .                # 无输出，格式合规
```

覆盖的交叉约束（题面要求的三类场景均含）：

| 测试 | 类型 | 覆盖点 |
| --- | --- | --- |
| `TestRoundRobinAndFrameLimits` | 可接受结果 | 三流按编号轮询、每流每轮至多一帧、16384 帧长切分、连接/流余额扣减、余额快照排序、空队列不重发 |
| `TestInitialWindowDeltaCanGoNegativeAndBlocks` | 应被阻止 | 题面数值例：余额 2 下调 8 → −6，再补 5 → −1 仍被阻止；其他有额度的流照常发送；余额为 0 时同样不发；新流取新初始设置 |
| `TestScopedWindowUpdatesAndConnectionInterleave` | 交错 + 拒绝 | 连接窗口为 0 时流增量无效；流级/连接级增量作用域互不串扰；未知流、非法增量、窗口溢出均被拒绝 |
| `TestZeroBoundaryMaxFrameAndRejectedInputs` | 边界 | 窗口精确归零、16385 字节切成 16384+1、最大帧长上下界拒绝 |
| `TestNoRetransmissionAndStreamLifecycle` | 交错 + 拒绝 | 流窗口与连接窗口交替恢复，每字节恰好发送一次；关闭流拒绝排队/增量；差值不作用于关闭流 |
| `TestConfigValidation` | 应被拒绝 | 构造期非法帧长/窗口配置拒绝；默认值回落 |

说明：本环境下 `go vet ./...` 被工作区权限策略拦截，未实际执行；
`go test` 已包含完整编译与类型检查，全部通过。
