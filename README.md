# backuprecovery — 数据库检查点低成本恢复规划库

## 解决的问题

恢复数据库检查点时，目标页的完整基础页与字节补丁分散在多个归档包中。每个归档包有
**固定开包成本**，包内每个条目有独立的**读取成本**。若按页各自选择最便宜来源，会
重复支付开包成本。本库在以下约束下求**全局最低总成本**方案：

- 页号可被回收后重新分配，同一页号的不同**世代（generation）**严格区分；
- 补丁只适用于**世代、长度、完整前像摘要**三者全部匹配的内容；
- 另一世代的同内容页不能作为补丁起点；
- 硬上限：最多 12 个归档包、每条来源链最多 8 个版本（含基础页）；
- 同价时先比读取条目数（更少者胜），再比合法操作串的字典序；
- 读取中途报告归档包损坏后，保留已校验中间页与已付开包费，重算**最小追加成本**。

## 公开 API

| 符号 | 说明 |
| --- | --- |
| `Digest(content) []byte` | SHA-256 内容摘要 |
| `ApplyPatch(patch, before) ([]byte, error)` | 真实应用字节补丁，校验前像摘要/长度、Hunk 区间与旧字节、后像摘要 |
| `NewEngine(targets, packages, patches) (*Engine, error)` | 构造期完整校验输入并建立索引 |
| `(*Engine).Plan() (*Plan, error)` | 返回全局最低成本恢复方案 |
| `(*Engine).Recover(progress, corruption) (*RecoveryPlan, error)` | 损坏后基于保留进度重算最小追加成本 |
| `NewExecutor(engine) *Executor` | 方案执行器，逐条真实读取并校验 |
| `(*Executor).Execute(plan)` | 完整执行方案 |
| `(*Executor).ExecuteUntil(plan, corruption)` | 执行到指定损坏点，返回损坏前快照 |
| `NewRecoveryExecutor(engine, progress)` | 注入保留页/已开包/已读条目的追加执行器 |
| `(*Executor).ExecuteRecovery(rec, corruption)` | 执行损坏恢复追加方案 |
| `ImpossibleError` / `MissingBeforeImage` | 不可能目标及缺失前像明细（含“存在另一世代同内容页”标记） |
| `CorruptionError` / `ExecProgress` / `VerifiedPage` | 损坏事件、损坏前进度与已校验页 |

### 核心数据模型

- `Target{PageID, Generation, Digest, Length}`：目标页目录与摘要；
- `BaseImage{PageID, Generation, Digest, Content}`：完整基础页；
- `Patch{ID, PageID, Generation, BeforeDigest, BeforeLength, AfterDigest, Hunks}`：
  带前像/后像摘要的字节补丁，`Hunk{Offset, Old, New}` 支持变长替换，区间不得重叠；
- `Package{ID, OpenCost, Entries}`，`Entry{ID, Base|PatchID, ReadCost}`；
- `Plan` 给出每页 `SourceChain`（`base`/`patch`/`retain` 步骤链）、总成本/开包/读取
  明细、去重条目数、包子集与**合法操作串**（Kahn 字典序最小拓扑序）。

## 算法

1. **建模**：页身份 = `(页号, 世代)`，内容身份 = SHA-256 摘要。补丁按
   `(页号, 世代) -> 后像摘要 -> 补丁列表` 建立入边索引；基础页与补丁条目记录全部
   归档位置（同一制品可在多个包中）。
2. **来源链枚举**：对每个目标从后像摘要反向 DFS，沿入边补丁追溯到基础页或已校验的
   保留页；深度超过 8 个版本即剪枝。每条候选链在 `buildChain` 中转正后**逐条真实
   应用补丁并重算摘要**，任何校验失败的链直接丢弃。
3. **全局最优**：枚举全部包子集（≤12 → 至多 4096 个），在每个子集内为各目标选可行
   链，成本 = ∑开包费（只付一次）+ ∑去重条目读取费；取全局最小。同价依次按条目数、
   合法操作串字典序、包子集掩码决胜。
4. **损坏恢复**：损坏包永久排除；已校验页成为零成本 RETAIN 起点；已开包费与已读条目
   费视为沉没成本，复用时不再计费；在剩余输入上重新执行完整最优算法，输出追加成本、
   累计成本与追加操作串。无绕行路径时再次返回 `ImpossibleError` 并列出缺失前像。

## 复现步骤

```bash
go version          # 期望 go1.20.2（go.mod 声明 go 1.20，无第三方依赖）
go test ./... -count=1 -v
go test -race ./... -count=1
```

## 验证范围

全部为真实功能测试（无演示预置输出替代断言），共 19 个用例：

| 分类 | 用例 | 验收点 |
| --- | --- | --- |
| 正常路径 | `TestPlanOptimalSharedOpenCost` | 多目标共享开包费的全局最优成本；独立校验器逐条应用补丁重算摘要；操作串拓扑合法 |
| 正常路径 | `TestExecutorActuallyRunsPlan` | 执行器真实读取/打补丁，实付成本与方案一致，终态摘要/长度匹配目标 |
| 同价决胜 | `TestTieBreakFewerEntries` | 同价选条目更少的链 |
| 同价决胜 | `TestTieBreakOperationOrder` | 条目数相同选字典序最小合法操作串，且 base 先于 patch |
| 世代隔离 | `TestGenerationIsolation` | 另一世代同内容页不能作起点；`HasOtherGeneration=true` |
| 不可能目标 | `TestImpossibleMissingBeforeImage` | 列出缺失前像摘要，无误报 |
| 补丁守卫 | `TestPatchGuardConditions` | 前像摘要/长度不符、Hunk 旧字节不符、后像摘要声明错误均拒绝 |
| 输入校验 | `TestValidationRejectsBadInputs` | 空目标、摘要不符、非法条目、未知补丁、负成本、超 12 包均拒绝 |
| 损坏恢复 | `TestCorruptionMidPackageKeepsProgress` | 条目级损坏保留已校验页与已付开包费，追加成本最优并真实执行成功 |
| 损坏恢复 | `TestCorruptionAtOpenTime` | 开包即损坏时开包费未支付，整体绕行后全部目标满足 |
| 损坏恢复 | `TestCorruptionMakesRecoveryImpossible` | 无绕行路径时返回 `ImpossibleError` 并列出缺失前像 |
| 损坏恢复 | `TestCorruptionRetainedPageIsAlreadyTarget` | 已完成目标以单步 RETAIN 零追加成本保留 |
| 边界 | `TestMaxVersionsBoundary` | 8 版本可行且独立校验；9 版本被拒 |
| 边界 | `TestMaxPackagesBoundary` | 12 包可行、13 包构造期拒绝 |
| 边界 | `TestLengthChangingAndMultiHunkPatch` | 变长补丁、多 Hunk 补丁真实应用与长度核对 |
| 边界 | `TestCheapestAcrossDuplicateLocations` | 同制品跨包时计入开包费选全局最便宜位置 |
| 边界 | `TestOverlappingHunksRejected` | 重叠 Hunk 构造期拒绝 |
| 边界 | `TestEmptyHunkPatch` | 零 Hunk 补丁可应用；等价时选更短的基础页直链 |
| 并发交错 | `TestConcurrentPlanning` | 16 goroutine × 25 轮并发规划/损坏重算/追加执行，结果确定一致（`-race` 通过） |

### 未执行项说明

- `go vet ./...` 在当前沙箱权限策略下需要额外审批，未执行；代码已通过 `gofmt`、
  `go test` 与 `go test -race`。
- 未使用任何第三方依赖，全部基于 Go 1.20 标准库（`crypto/sha256`、`sort`、`sync` 等）。
