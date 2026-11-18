# 泄露处置演练编排器（drillorchestrator）

一个**纯本地、全模拟**的处置演练编排器：根据动作前置条件、互斥约束和模拟执行回执，
向值班员给出当前可执行动作。编排器不触碰任何真实基础设施——执行结果以 `Receipt`
（模拟回执）形式由值班员或测试注入，仅用于防御性演练与验证。

## 实际入口

- 模块：`drillorchestrator`（Go 1.20，见 `go.mod`），包 `orchestrator`，实现位于 `orchestrator.go`。
- `NewOrchestrator(plan Plan) (*Orchestrator, error)`：加载并校验演练计划
  （空事件号、重复动作、未知/自引用前置、依赖环均被拒绝）。
- `o.Decisions()` / `o.AvailableActions()`：面向值班员的当前可执行动作及理由。
- `o.SubmitReceipt(rc Receipt) ReceiptOutcome`：处理一条模拟执行回执。
  - 前置未满足、互斥冲突、未知动作、缺回执号 → 拒绝（不消耗回执号）。
  - 相同 `ReceiptID` 重复投递 → 标记 `Duplicate`，状态不变，**不会触发第二次回退**。
  - 失败回执 → 仅回退**可逆且已完成**、且不被其他成功分支依赖的依赖链
    （被成功分支共享的防护继续生效，记 `RETAINED`）；**不可逆**动作保留记录并
    转人工（记 `ESCALATION`，状态保持 `SUCCEEDED` 生效中）。
- `o.DecisionLog()` / `o.Trace(actionID)` / `o.Escalations()` / `o.StateOf(id)`：
  追溯与审计。每条日志携带事件号；回退/保留/转人工记录携带 `Cause=failure-of:<动作>`
  与触发回执号，回退后重新开放的动作在 `Decision.Reasons` 中引用原失败动作与事件号。

## 演练剧本（测试夹具）

`enable-backup-access`（启用备用接入）→ `isolate-terminal`（隔离终端）、
`suspend-identity-issuance`（暂停身份发放）→ `freeze-tokens`、`revoke-keys`（不可逆），
另有互斥组 `comms`：`public-disclosure` 与 `silent-containment`。

## 验证范围（`orchestrator_test.go`，`go test ./...` 全部通过）

- 可接受结果：`TestHappyPathSequencedExecution` —— 按依赖顺序完成全部动作，
  可执行列表逐步推进，日志均带事件号。
- 应被阻止/拒绝的结果：
  - `TestReceiptRejectedWhenPreconditionsUnmet` —— 前置未满足被拒绝；边界：被拒绝的
    回执号不被消耗，前置满足后同一回执号可被接受。
  - `TestMutexSiblingBlocked` —— 互斥组一方成功后另一方不可执行且回执被拒绝。
  - `TestUnknownActionAndMissingReceiptIDRejected` —— 未知动作、缺回执号被拒绝。
- 边界/交错场景：
  - `TestFailureRollsBackOnlyUnsharedReversibleBranches` —— 失败只回退未被共享的
    可逆分支（`isolate-terminal`），被成功分支 `freeze-tokens` 依赖的防护继续生效。
  - `TestRollbackDecisionsTraceableToOriginalIncident` —— 回退/保留记录与重新开放的
    决策均可追溯到原失败动作、回执号与事件号。
  - `TestIrreversibleDependencyEscalatedToManual` —— 不可逆依赖保留记录并转人工，
    其上游可逆动作因仍被依赖而保留。
  - `TestDuplicateFailureReceiptDoesNotRollbackTwice` —— 重复失败回执不触发二次回退。
  - `TestDuplicateSuccessReceiptDoesNotResurrectRolledBackAction` —— 重复成功回执不会
    把已回退动作复活；演练可用新回执继续推进。
  - `TestPlanValidation` —— 计划校验（环、未知前置、重复 ID 等）。

## 运行

```sh
go test ./...
```
