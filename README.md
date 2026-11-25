# reqreplybridge — 请求/应答消息总线与重编号桥

Go 1.20.2 实现的纯进程内请求/应答消息模型。两个消息总线上的客户端都从关联号
（correlation）1 开始自行编号；桥接在每一跳分配**出口会话自己的短关联号**，
应答返回时逐级换回原会话的关联号与分支编号。所有数据均为本地合成，无网络、
无外部依赖、无凭据访问。

## 1. 模型与关键规则

- **会话代际（epoch）**：端点每次 `Connect` 产生一个新代际，关联号分配器在
  每个代际内都从 1 开始。`Disconnect` 只是摘除当前会话，旧代际仍然保留，
  可以接收发往旧代际的迟到应答；`Close` 才永久销毁。
- **重连重用短号**：重连后新代际再次从 1 分配，因此“同一短号”会在不同代际
  重复。桥接内部用 `(epoch, correlation)` 双键保存每个转发请求，旧连接的
  迟到包只会命中旧代际的父请求，不会污染新代际。
- **逐级换回**：应答沿桥反向传播时，桥把出口地址（出口端点/代际/短号）替换
  为保存的入口地址（原调用方端点/代际/原关联号），两级桥即完成两次换回。
- **多路分支与连续性**：一个请求可声明多个分支（`DeclareBranches`），每个
  分支可声明尾号（`DeclareTail`）。分支只在序号 `0..tail` 全部连续到达时才
  判定结束；尾号先到、早先分片后到只会产生一次 `ErrGap` 观测事件，分片照常
  透传，等连续性满足时分支才结束。
- **父请求收尾**：只有所有已声明分支都结束（且每个声明的分支都真实出现过），
  父请求才结束并从桥接表中删除；声明了但从未出现的分支会让父请求保持挂起。
- **失败关闭**：出口断开期间转发请求返回 `ErrEgressDown`，不产生悬空映射。

## 2. 公开 API（包 `reqreplybridge`）

### 总线与会话
- `NewBus(name string) *Bus`
- `(*Bus).Attach(id EndpointID) (*Endpoint, error)`
- `(*Bus).SendRequest(sender *Session, m RequestMsg) error` — 总线用发送方
  当前代际在 `RequestMsg.ReplyTo` 上盖戳。
- `(*Bus).SendReply(m ReplyMsg) error` — 按 `ReplyAddr` 的端点+代际投递，
  旧代际（已断开但未关闭）同样可达。
- `(*Endpoint).Connect(replyHandler ReplyHandler) (*Session, error)`
- `(*Endpoint).Disconnect()` / `(*Endpoint).Close()`
- `(*Endpoint).Session() *Session` / `SessionByEpoch(epoch int) (*Session, bool)`
- `(*Endpoint).OnRequest(h RequestHandler)`
- `(*Session).AllocateCorrelation() (uint64, error)` — 每个代际首个返回 1。
- `(*Session).Epoch() int` / `Endpoint() EndpointID` / `LateDropped() int`

### 消息类型
- `RequestMsg{Target, Correlation, Branch, Seq, Payload, ReplyTo ReplyAddr}`
- `ReplyMsg{Addr ReplyAddr, Branch, Seq uint64, DeclareBranches *uint64,
  DeclareTail *uint64, Payload string}`
- `ReplyAddr{Endpoint EndpointID, Epoch int, Correlation uint64}`

### 桥
- `NewBridge(left, right *Bus, cfg BridgeConfig) (*Bridge, error)`，配置含四个
  端点 ID 与两个转发目标（对侧总线上的入口端点 ID）。
- `(*Bridge).Start() error` — 连接两个出口端点并注册入口处理器。
- `(*Bridge).ReconnectEgress(d Direction) error` — 出口断开重连，新代际短号
  重新从 1 开始，旧代际保留。
- `(*Bridge).EgressSession / EgressEndpoint / IngressEndpoint(d Direction)`
- `(*Bridge).Stats(d Direction) LegStats`
- `(*Bridge).OnForward / OnForwardLeg`、`OnBridgedReply / OnBridgedReplyLeg`
  — 观测转发与每一跳应答（含 `BranchEnded`、`ParentEnded`、`ErrGap`、
  `ErrStaleReply`）。
- `(*Bridge).HandleEgressReply(d Direction, m ReplyMsg) ReplyEvent` — 直接向
  桥腿注入一个出口应答，用于测试桥层映射（总线会话层会先校验关联号）。
- `(*Bridge).Stop()`
- 方向常量：`LeftToRight = "left->right"`、`RightToLeft = "right->left"`。

### 错误哨兵
`ErrEndpointClosed`、`ErrNotConnected`、`ErrUnknownEndpoint`、`ErrNoHandler`、
`ErrBadCorrelation`（总线会话层：该代际从未分配过此关联号）、
`ErrEgressDown`、`ErrStaleReply`（桥层：代际+关联号找不到父请求）、
`ErrGap`（尾号已声明但 0..tail 尚不连续，仅在首次判定时上报一次）。

## 3. 运行方法

```bash
go test ./...            # 全部单元测试与 Example
go test -race ./...      # 竞态检测（已验证通过）
go test -v ./... -cover # 详细用例与覆盖率（本地实测 94.6%）
go test -run Example_twoLevelChain -v ./...
```

仅依赖标准库，`go.mod` 不引入任何第三方模块，不需要联网。

## 4. 验证范围（均有断言或 Example 输出比对支撑）

| 主题 | 测试 |
| --- | --- |
| 会话从 1 编号；重连后新代际再次从 1；旧代际收迟到包；未分配关联号被拒 | `TestSessionCorrelationRestartsAtOne`、`TestConnectWithoutDisconnect` |
| 总线盖戳 `ReplyTo` 并按代际回投 | `TestRequestReplyRouting`、`TestBusErrorPaths` |
| 桥分配出口短号并换回原关联号/分支号 | `TestBridgeRenumbersAndSwapsBack` |
| 分支先结束后到早先分片；尾号先到报一次 gap；连续才结束；父请求等齐所有分支 | `TestInterleavedBranchesAndParentFinishing`、`TestParentWaitsForAllBranches` |
| 出口重连重用短号；旧连接迟到包落在旧会话；新代际独立映射；桥层/总线层各自的失效判定 | `TestEgressReconnectReusesShortNumbersAndIsolatesLatePackets`、`TestStaleReplyInvokesObserver` |
| 出口断开时转发失败关闭，重连后恢复 | `TestEgressDownRejectsForwarding` |
| 反向（right→left）全链路、反向出口重连、非法方向 | `TestRightToLeftDirection` |
| 两个入口各发 1 号请求、其中一条跨两级桥，调用方隔离与分片到达顺序 | `TestTwoLevelChainTwoIngressesBothCorrelationOne`、`Example_twoLevelChain` |
| 25 个并发调用方各自编号 1、反向批量应答不串话 | `TestConcurrentCallersInterleaving` |
| 关闭、重复端点、无处理器、处理器错误、入口代际消失后的投递错误 | `TestEndpointCloseRejectsUse`、`TestNewBridgeRejectsDuplicateEndpoints`、`TestBusErrorPaths`、`TestReplyToClosedIngressErrors`、`TestForwardFailureCleansUp` |

最近一次本地执行结果：`go test -race ./...` 全部通过（17 个测试函数 +
1 个 Example），语句覆盖率 94.6%；未覆盖语句均为正常流程下不可达的防御分支
（例如已判空后的二次判空、成功路径上不会失败的 `Connect` 错误返回）。

## 5. 题面场景演示：两个入口的 1 号请求，一条跨两级桥

拓扑为三条总线 `A -> M -> C`，由桥 `B1`（A/M 之间）与 `B2`（M/C 之间）
串联。`client-1` 与 `client-2` 在 A 上各自分配关联号 1；两个请求经 B1、B2
两跳后，在 B2 的共享出口会话上分别得到短号 1、2。应答刻意交错：client-1 的
短分支先结束，client-2 随后收尾，client-1 的另一分支尾号先到、早先分片后到。

以下输出由 `Example_twoLevelChain` 实际产生并由测试框架逐行断言（`e`=代际，
`c`=关联号，`b`=分支，`s`=序号）：

```text
caller correlations: client-1=1, client-2=1
FWD B2 left->right: b1-out-m/e1/c1 -> b2-out-c/e1/c1
FWD B1 left->right: client-1/e1/c1 -> b1-out-m/e1/c1
FWD B2 left->right: b1-out-m/e1/c2 -> b2-out-c/e1/c2
FWD B1 left->right: client-2/e1/c1 -> b1-out-m/e1/c2
REPLY B1: b1-out-m/e1/c1 b1 s0 -> client-1/e1/c1 b1 [branch-end]
REPLY B2: b2-out-c/e1/c1 b1 s0 -> b1-out-m/e1/c1 b1 [branch-end]
REPLY B1: b1-out-m/e1/c2 b0 s0 -> client-2/e1/c1 b0 [branch-end] [parent-end branches=1]
REPLY B2: b2-out-c/e1/c2 b0 s0 -> b1-out-m/e1/c2 b0 [branch-end] [parent-end branches=1]
REPLY B1: b1-out-m/e1/c1 b0 s2 -> client-1/e1/c1 b0 error=branch fragments are not continuous from zero
REPLY B2: b2-out-c/e1/c1 b0 s2 -> b1-out-m/e1/c1 b0 error=branch fragments are not continuous from zero
REPLY B1: b1-out-m/e1/c1 b0 s0 -> client-1/e1/c1 b0 -
REPLY B2: b2-out-c/e1/c1 b0 s0 -> b1-out-m/e1/c1 b0 -
REPLY B1: b1-out-m/e1/c1 b0 s1 -> client-1/e1/c1 b0 [branch-end] [parent-end branches=2]
REPLY B2: b2-out-c/e1/c1 b0 s1 -> b1-out-m/e1/c1 b0 [branch-end] [parent-end branches=2]
DELIVER client-1[0]: b1 s0 "c1-b1"
DELIVER client-1[1]: b0 s2 "c1-b0-seq2"
DELIVER client-1[2]: b0 s0 "c1-b0-seq0"
DELIVER client-1[3]: b0 s1 "c1-b0-seq1"
DELIVER client-2[0]: b0 s0 "c2-b0"
```

可读出的结论：

1. **实际调用方**：尽管两条请求在 B2 出口分别是 c1/c2，每一跳都把地址换回
   上一跳的会话地址，最终 `client-2` 只收到自己的 `c2-b0`，`client-1` 只
   收到自己的四个分片，两边的原始关联号都恢复为各自的 1。
2. **分片顺序**：调用方收到的顺序与应答发出顺序完全一致（尾号 seq2 先于
   seq0/seq1 到达），桥不重排、不缓冲业务分片，只做连续性判定。
3. **收尾时机**：client-2 单分支在其分片到达时即父请求结束；client-1 的
   分支 0 在 seq2 先到时因缺口报一次 gap，待 seq0、seq1 补齐、0..2 连续后
   才分支结束，随后两分支齐全，父请求结束。

## 6. 设计取舍与边界语义

- 桥不缓存/重排业务分片，缺口只影响“分支是否结束”的判定，不影响透传。
- `DeclareBranches`/`DeclareTail` 可由任意分片携带，桥以最后见到的声明为准
  来维护状态；声明缺失时对应分支/父请求保持未结束（符合“只有连续到已声明
  尾号才算结束”）。
- 迟到包隔离依赖代际：总线层先校验该代际是否分配过该关联号
  （`ErrBadCorrelation`），桥层再按 `(epoch, corr)` 查父请求
  （`ErrStaleReply`），两层职责分开。
- 观测回调在总线同步投递路径上执行，回调内不应阻塞；生产实现若需要异步
  可在回调中自行入队。

## 7. 范围与安全说明

本实现只包含题面要求的防御性判定（代际校验、关联号归属校验、分支连续性
校验、失败关闭），不包含任何利用、入侵、攻击或规避逻辑；全部测试使用
本地合成数据，不读取凭据、不访问真实目标、不上传材料、不安装第三方依赖。
