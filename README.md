# 死信回投模块（dead-letter re-injection）

按题面实现的 Go 1.20.2 模块。队列中的账单事件按客户顺序处理；某条消息进入死信后，
仍卡在它前面的消费组可以从原位置恢复，已经做出**不可撤销跳过**决定的组则把修复内容
送待复核来源，并保留此前已确认的进度。回投接口按业务修复 ID 幂等，重试沿用同一份
决定；其他客户的消息继续走正常投递路径。

## 目录结构

```
go.mod
reinject/
  types.go        # 领域类型与三个副作用端口接口
  engine.go       # 回投决策引擎（含幂等与在途去重）
  fakes_test.go   # 本地合成数据：内存进度存储、假 Broker、假待复核来源
  engine_test.go  # 正常/边界/交错/并发/失败场景测试
README.md
```

无外部依赖，仅使用 Go 标准库。

## 决策规则

对一次回投请求中的每个消费组，引擎读取该组在消息键（`Key`）上的确认进度：

| 组的进度状态 | 动作 | 语义 |
| --- | --- | --- |
| 从未消费该键，或已确认序号 `< SeqInKey`，且无跳过标记 | `ResumeInPlace` | 在原键内序号位置重投修复后的消息，组从原位恢复 |
| 在 `SeqInKey` 上有不可撤销跳过标记 | `RouteToReview` | 修复内容送待复核来源，不回退已确认进度 |
| 已确认序号 `>= SeqInKey` 但无显式跳过标记 | `RouteToReview` | 视为已越过该位置的隐式跳过，同样送待复核，保留进度 |

判定输入对应题面四要素：

- **消息标识**：`DeadLetter.MessageID`，修复请求中的 ID 必须与之匹配；
- **键内序号**：`DeadLetter.Key` + `DeadLetter.SeqInKey`，所有进度判断都限定在同一键内；
- **分组确认记录**：`ProgressReader.Progress` 返回的 `AckProgress`（已确认序号、
  是否有过确认、不可撤销跳过标记集合）；
- **修复内容**：`Repair.RepairedPayload`，按 `Repair.RepairID` 标识一次业务修复。

## 幂等与重试

- 同一 `RepairID` 的第一次调用执行副作用并持久化 `Decision`；
- 之后的调用（包括在首次调用尚未结束时并发到达的重试）直接返回已记录的决定，
  不重复执行任何副作用。并发去重通过“先登记在途修复、再执行副作用”的保留机制实现；
- 在途调用失败时保留被释放，后续重试可以重新执行，直到成功并落库一份决定；
- 不同 `RepairID` 即使针对同一条死信，也是相互独立的业务决定。

## 公开 API

### 类型（`reinject/types.go`）

- `MessageID`、`Group`：消息标识与消费组名的具名字符串类型。
- `DeadLetter`：死信消息（`MessageID`、`Key`、`SeqInKey`、`CustomerID`、`OriginalPayload`）。
- `Repair`：业务修复（`RepairID`、`MessageID`、`CustomerID`、`RepairedPayload`）。
- `ActionKind`：`ResumeInPlace`（原位恢复）或 `RouteToReview`（送待复核），含 `String()`。
- `GroupAction`：单个消费组的决定及投递所用数据。
- `Decision`：一次修复的完整决定，提供 `ActionFor(Group)` 查询。
- `AckProgress`：分组确认记录，提供 `BeforePosition(seq)` 与 `HasIrrevocableSkip(seq)`。
- 哨兵错误：`ErrEmptyRepairID`、`ErrMessageMismatch`、`ErrCustomerMismatch`、
  `ErrEmptyDeadLetter`、`ErrNilProgress`、`ErrNilBroker`、`ErrNilReview`。

### 端口接口（由使用方实现）

- `ProgressReader`：`Progress(ctx, group, key) (AckProgress, error)`，只读确认进度。
- `Broker`：
  - `ReinstateAt(ctx, group, id, key, seq, payload)`：在原位置为某组重投；
  - `Deliver(ctx, id, key, seq, customerID, payload)`：其他客户消息的正常投递路径。
- `ReviewSource`：`SendForReview(ctx, group, id, key, seq, customerID, payload)`，
  接收无法原位恢复的修复内容。

### 引擎（`reinject/engine.go`）

- `NewEngine(progress, broker, review) (*Engine, error)`：装配三个端口，nil 端口返回哨兵错误。
- `(*Engine).Redeliver(ctx, dead, repair, groups) (Decision, error)`：核心回投接口。
- `(*Engine).StoredDecision(repairID) (Decision, bool)`：查询已落库的决定。
- `(*Engine).RecordedRepairIDs() []string`：已记录的修复 ID（诊断/测试用）。

### 本地合成适配器（`fakes_test.go`，测试包内）

- `MemoryProgress`：内存确认进度存储，`Set` / `MarkSkip` 写入，永不回退进度；
- `FakeBroker`：记录 `ReinstateAt` 与 `Deliver` 调用，支持注入一次性失败；
- `FakeReview`：记录 `SendForReview` 调用，支持注入一次性失败。

## 运行方法

```bash
go test ./...
go test ./... -race -count=3
go test ./... -v
go vet ./...
gofmt -l .
```

## 验证范围（全部为本地合成数据，无网络、无真实目标）

实测通过的场景：

1. **正常分组路由**：A 组停在序号 2（死信在序号 3）→ 原位恢复一次；
   B 组对序号 3 有不可撤销跳过且已确认到序号 5 → 修复送待复核一次。
2. **位置边界**：从未消费键、确认序号恰为 `seq-1`、`seq=0` 且已确认序号 0、
   显式跳过标记、确认序号越过死信位置（隐式跳过）五种边界。
3. **重试幂等**：同 `RepairID` 多次重试（含更换组列表、传空列表）返回完全相同的
   决定，副作用各只发生一次；不同 `RepairID` 互相独立。
4. **进度保留**：跳过组的已确认序号与跳过标记在回投前后不变，模块从不回退进度。
5. **其他客户继续投递**：修复与其他客户（及同客户后续序号）的正常 `Deliver`
   交错执行，正常消息不被重投或送复核。
6. **多条死信交错**：同键序号 3 与序号 7 两笔修复交错进行，各自决定独立、
   重试仍返回原决定，中间的正常投递不丢失。
7. **并发**：32 个相同修复并发调用，副作用恰好一次、只落库一份决定（`-race` 下验证）；
   在途调用失败时等待方收敛，最终仍只产生一次成功副作用。
8. **防御性校验**：空修复 ID、死信 ID 为空、消息 ID 不匹配、客户不匹配、
   nil 端口均在副作用前返回哨兵错误。
9. **副作用失败**：Broker / 待复核来源一次性失败时不落库决定，重试可成功；
   上下文取消时不产生副作用、不落库。
10. **其他**：重复组名去重、动作顺序确定性、载荷深拷贝（外部修改不污染引擎状态）、
    `ExampleEngine` 可执行示例。

## 安全边界

仅实现题面规定的防御性判断（标识/客户一致性校验、进度只读、幂等去重）。
不包含任何利用、入侵、攻击或规避载荷；不读取凭据、不访问网络、不安装依赖。
