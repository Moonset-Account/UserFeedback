# 办公接入网可信地址绑定判定模块

基于 Go 1.20.2 的单模块判定引擎（module `officeaccess`），消费交换机端口事件、
可信分配回执与设备自报地址声明，输出每个端口的有效绑定和触发隔离的记录。
仅使用 Go 标准库（`net/netip` 做网段/地址校验，`sort` 保证输出确定性）。

## 实际入口

无独立 `main` 程序，判定能力以库形式提供，入口均在 `engine.go`：

| 入口 | 作用 |
| --- | --- |
| `NewEngine() *Engine` | 创建空的单线程判定引擎 |
| `(*Engine).HandlePortEvent(PortEvent) error` | 消费端口接入/离开事件（`port_up` / `port_down`） |
| `(*Engine).HandleAllocation(Allocation) error` | 消费可信分配回执；`Released=true` 表示释放 |
| `(*Engine).HandleClaim(Claim) (Decision, error)` | 判定设备自报声明是否可入绑定表 |
| `(*Engine).Snapshot() Snapshot` | 返回确定性快照：`PortBindings` / `Isolations` / `RejectedClaims` |

事件必须按发生顺序喂入；引擎非并发安全。`go test ./...` 为唯一验证入口。

## 核心判定规则

1. **三元组匹配才入表**：声明的 `(Session, Segment, Address)` 必须存在当前已确认
   分配，且该会话仍是声明端口的当前可信接入会话，否则按原因码拒绝
   （`session_not_current_on_port` / `no_matching_confirmed_allocation`）。
2. **同网段地址同时只归一个会话**：两个经确认的分配在相同 `(Segment, Address)`
   重叠时，该地址进入隔离，双方声明均以 `address_isolated` 拒绝；其他地址继续正常
   绑定。隔离重新发生时，该地址上既有的有效绑定会被踢出，重叠解除并重新声明后才可
   再次入表。
3. **隔离按轮次记录**：同一地址每次“重叠→解除→再次重叠”各产生一条
   `IsolationRecord`；同一轮次内重复确认不产生重复记录；历史记录不回改。
4. **换端口与迟到释放保护**：分配回执带 `ReceiptID` 标识分配实例。释放只在回执 ID
   与当前确认实例一致时生效；换端口后到达的旧回执释放、被接管端口旧会话的释放均为
   空操作，不能删除新绑定。旧端口的迟到 `port_down` 同样被忽略。
5. **会话下线联动**：会话离开或端口被接管时，拆除其会话/端口映射、绑定与确认分配，
   并重算相关地址的隔离状态；若重叠随之消失，活跃隔离解除（历史记录保留）。
6. **输入校验**：网段必须是合法 CIDR、地址必须合法且包含在网段内；格式错误返回
   `ErrInvalidPortEvent` / `ErrInvalidReceipt` / `ErrInvalidClaim`，且不改变引擎状态。

## 验证范围（`engine_test.go`，Go 内置 testing，真实断言）

| 测试 | 覆盖类别 | 关键断言 |
| --- | --- | --- |
| `TestAcceptedClaimsBuildBindings` | 可接受结果 | 多地址三元组匹配全部入表、重复声明幂等、快照排序稳定 |
| `TestRejectedClaimsAndInterleavedArrival` | 应被阻止 + 交错 | 分配先于端口到达时拒绝、端口接入后可绑定；非当前会话/未确认地址/网段不匹配均拒绝 |
| `TestOverlappingAllocationsIsolateAffectedAddressOnly` | 交叉约束 + 边界 | 重叠地址隔离且其他地址可用；一方释放后解除；再隔离产生第二条记录并踢出旧绑定 |
| `TestPortSwitchLateReleaseCannotRemoveNewBinding` | 边界/交错 | 换端口后旧回执释放与迟到 `port_down` 均不能删除新绑定；当前回执释放才生效 |
| `TestDisplacedSessionReleaseDoesNotTouchNewSessionBinding` | 应被阻止 | 端口被新会话接管后，旧会话的迟到释放不能影响新会话绑定 |
| `TestIsolationClearsWhenOneOverlappingSessionDetaches` | 交错 | 隔离中一方会话下线，活跃隔离解除、剩余方可绑定、历史记录保留 |
| `TestMalformedInputsAreRejectedWithoutStateChange` | 输入边界 | 非法事件类型/网段/越界地址/缺字段全部报错且状态不变 |

## 本地核验

```bash
gofmt -w engine.go engine_test.go
go test ./... -count=1 -v
```

当前工作区已用 Go 1.20.2 验证：7 个测试全部通过（`go test ./...` 退出码 0）。
`go vet` 不在本次批准的命令范围内，未执行。
