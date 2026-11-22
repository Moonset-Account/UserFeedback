# 故障场景协调模块（faultcoordinator）

隔离演练环境中的故障场景协调器。输入是模拟服务的**冗余拓扑、依赖能力需求、
演练要占用的实例集合与持续时间**，协调器在容量底线约束下完成演练准入、
等待排队、代次化健康恢复核对、外部模拟故障撤回与剩余缺口报告。

仅使用 Go 标准库，Go 版本固定为 **1.20.2**，无任何外部依赖。

## 1. 领域模型与公开 API

全部类型定义见 `types.go`，协调逻辑见 `coordinator.go`，包名 `faultcoordinator`。

### 1.1 拓扑输入

| 类型 | 含义 |
| --- | --- |
| `Topology` | 聚合 `Dependencies`、`Instances`、`RequiredPaths` 的拓扑定义 |
| `Dependency` | 共享下游能力，含 `ID` 与总 `Capacity` |
| `Instance` | 可被演练占用的实例，含若干 `Route`（路由到哪些依赖、各消耗多少 `Load`） |
| `RequiredPath` | 必须始终满足的要求路径：`Floor` 底线 + 覆盖的 `DependencyIDs`，短板语义 |

容量判定：对每条要求路径覆盖的每个依赖，要求

```
该依赖总容量 - 外部故障累计削减 - 当前在持影响预算 >= 路径底线 Floor
```

任一依赖不满足即整条路径不满足。

### 1.2 协调器方法

| 方法 | 行为 |
| --- | --- |
| `NewCoordinator(Topology) (*Coordinator, error)` | 校验拓扑（重复 ID、未知引用、非正负载、负底线等）并创建协调器，模拟时钟从固定起点开始 |
| `Submit(Drill) (DrillSnapshot, error)` | 提交演练；满足全部底线与实例互斥则立即准入，否则进入 **FIFO 等待队列**（队头阻塞） |
| `Exit(drillID) (DrillSnapshot, error)` | ACTIVE 演练主动退出，转为 RECOVERING；**影响预算继续持有** |
| `Advance(d) (time.Time, []string)` | 推进模拟时钟；到达计划退出时间的 ACTIVE 演练自动退出（规则同 `Exit`） |
| `ReceiveReceipt(Receipt) (ReceiptResult, error)` | 处理健康恢复证据，返回 `ACCEPTED/EXPIRED/CANCELED/DUPLICATE/UNKNOWN` 结论 |
| `InjectExternalFault(ExternalFault) (FaultResult, error)` | 在演练间插入外部模拟故障；跌破底线时选最少撤回集，无法恢复时给剩余缺口 |
| `Snapshot() Snapshot` | 整体状态只读快照（各状态演练、逐依赖剩余容量、外部削减量、下代次） |
| `Drill(id) (DrillSnapshot, bool)` | 单个演练状态快照 |
| `AuditLog() []AuditEvent` | 单调序号的完整审计记录 |
| `Now() time.Time` | 当前模拟时钟 |

### 1.3 演练生命周期与代次规则

```
WAITING ──准入──▶ ACTIVE ──Exit/AutoExit──▶ RECOVERING ──匹配代次健康回执──▶ RETURNED
                      │                           │
                      └──────外部故障撤回──────────┴──▶ RECOVERING(revoked，代次关闭)
```

- 每次准入分配单调递增的**代次（generation）**，从 1 开始。
- 演练退出只释放“运行态”，**不释放影响预算**；只有收到属于本次代次的
  `ReceiptHealthy` 回执才归还预算、释放实例，并顺带尝试 FIFO 放行等待者。
- 回执核对规则（各自保留可核审计记录）：
  - **过期/超前代次** → `EXPIRED`，不归还；
  - **取消回执** `ReceiptCanceled` → `CANCELED`，登记不归还；
  - 同一代次首份回执之后的任何回执 → `DUPLICATE`，锁定不产生副作用；
  - 未知演练 → `UNKNOWN`，仅记录；
  - 被撤回演练的代次已关闭，旧代次健康回执同样按 `EXPIRED` 处理。
- `RETURNED` 的演练 ID 可重新提交，重新准入获得新代次。

### 1.4 外部故障与最少撤回

- `InjectExternalFault` 先把削减量计入依赖，再检查全部要求路径。
- 未跌破：直接吸收，不撤回。
- 跌破：按集合大小从小到大枚举 ACTIVE 演练组合，取**第一个恢复可行性的
  最少撤回集合**；同数量时按 `(Priority 升序, DrillID 升序)` 的字典序选择
  （即同数量优先撤回低优先级，优先级相同按演练 ID 定序）。
- 被撤回演练立即释放实例与预算、代次关闭，随后触发等待队列 FIFO 放行。
- 任何组合（含撤回全部 ACTIVE）都无法恢复时，**不做部分撤回**，
  `FaultResult.Recovered=false`，`Infeasibility` 逐路径、逐依赖给出
  `Remaining / Floor / Shortfall` 剩余缺口，归因于该外部故障 ID。
- 所有方法通过同一把互斥锁保护，可并发调用；`TestConcurrentSubmissions`
  在 24 goroutine 并发提交下校验实例互斥与容量底线。

## 2. 题面场景：两个互不重叠实例集合共享下游

测试 `TestSharedDownstreamOrdering`（`coordinator_test.go`）端到端核对
“等待 → 撤回 → 后来放行”的完整次序：

1. 拓扑：依赖 `shared` 容量 20、路径底线 6，每个实例负载 5。
2. D-A 占用实例 `{a1,a2}`（持 10），剩余 10 ≥ 6，**立即准入**（代次 1）。
3. D-B 占用与 A 互不重叠的 `{b1}`（再持 5），叠加后剩余 4 < 6，**进入等待**。
4. 演练之间注入外部故障 F1（`shared` 削减 5），剩余 5 < 6 跌破底线；
   协调器选出最少撤回集 `{D-A}`，D-A 代次关闭、预算与实例立即释放。
5. FIFO 放行 D-B（代次 2），剩余 = 20 − 5 − 5 = 10 ≥ 6，恢复可行。
6. D-A 旧代次健康回执 → `EXPIRED`；D-B 退出 → RECOVERING；
   错误代次回执 → `EXPIRED`；本次代次健康回执 → `ACCEPTED`、预算归还。
7. 审计日志严格断言事件次序：
   `ADMITTED → QUEUED → FAULT_REVOKED → FAULT_APPLIED → ADMITTED →
    RECEIPT_EXPIRED → EXITED → RECEIPT_EXPIRED → RETURNED`，且序号单调。

## 3. 复现步骤

```bash
go version            # 期望 go1.20.2
go test ./...         # 全部功能测试
go test -race ./...   # 带竞态检测
go test -v -count=1 ./...   # 逐用例输出，禁用缓存
gofmt -l .            # 无输出表示格式干净
```

## 4. 验证范围与场景清单

所有结论均来自真实测试断言，不存在用预置演示输出替代功能验证的情况。

| 测试 | 覆盖的题面要求 / 场景类别 |
| --- | --- |
| `TestSharedDownstreamOrdering` | 正常 + 交错：互不重叠实例集合共享下游；等待、故障撤回、后来放行的次序；代次化回执 |
| `TestWaitThenReceiptRelease` | 交错：仅退出不归还；取消/重复/未知回执各自可核且不产生副作用 |
| `TestBoundaryAdmission` | 边界：剩余恰好等于底线时准入，差一单位则等待；故障临界削减 |
| `TestMinimumRevocationAndTieBreak` | 故障恢复：最少撤回集合；同数量按低优先级、再按演练 ID 定序（大小 1 与大小 2 两档） |
| `TestInfeasibleGapReport` | 故障恢复：任何组合都无法恢复时的逐路径/逐依赖剩余缺口，且不做部分撤回 |
| `TestAutoExitAndGeneration` | 边界/交错：时钟推进自动退出、预算继续持有、代次推进、重新提交后旧回执过期 |
| `TestConcurrentSubmissions` | 并发交错：24 goroutine 并发提交后的实例互斥、容量底线与队列计数（race 检测通过） |
| `TestValidationAndGuardPaths` | 边界：非法拓扑/入参校验、重复 ID、状态机守卫（对 WAITING/RECOVERING 调 Exit 等） |
| `TestMultiHopPathShortBoard` | 正常 + 故障：多依赖路径短板语义、单依赖故障后的撤回与放行 |
| `TestReceiptReturnUnblocksFIFO` | 交错：健康回执归还后 FIFO 放行；队头阻塞时后置等待者不得插队 |
| `TestFaultWithWaitersAndRepeatedRevocation` | 故障 + 等待者交错：等待者在撤回后仍受底线约束、连续故障、撤回审计计数 |

### 已知的环境限制（如实记录）

- 本环境中 `go vet ./...` 与 `go build ./...` 的执行被权限策略拦截，未能运行；
  已实际执行并通过的验证为 `go test ./...`、`go test -race -count=1 ./...`
  与 `gofmt -l .`（测试编译本身覆盖了全部包的类型检查与构建）。
- 协调器使用进程内模拟时钟（`Advance`），不接触真实时间、网络或外部系统；
  全部数据均为测试内本地合成数据。
