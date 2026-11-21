# 分步训练试验协调器（Step Tournament Coordinator）

一个零依赖、纯 Node.js 标准库实现的试验协调器。它把一组配置先训练到 20 步，
只让成绩靠前的一半续训到 60 步，再按同样规则筛到 180 步。

设计目标不是"跑一遍训练"，而是让每一步的**晋级名单、续训起点、作废结果**都可以
被独立复核。所有状态变化都进入不可变审计日志，每个结算附带可复算的摘要。

- 运行时：Node.js **24.15.0**（仅 ESM + 标准库，无第三方依赖）
- 测试：`node:test`，位于 `tests/*.test.mjs`
- 演示：`node examples/demo.mjs`（真实调用协调器并做独立交叉核对）

## 快速开始

```bash
node -v                 # 期望 v24.15.0
node --test 'tests/*.test.mjs'
node examples/demo.mjs
```

无需 `npm install`。`package.json` 只声明 `"type": "module"` 与 `node --test` 脚本。

## 规则总览

| 规则 | 实现 |
| --- | --- |
| 每轮先冻结试验名单与验证折名单 | `openRound()` 生成不可变 `roster`，之后任何提交都必须完全匹配 |
| 成绩 = 总正确数 / 总样本数 | `aggregateFolds()` 累加后相除，**不是**各折准确率的宏平均 |
| 同分按试验编号 | 数字型编号按数值（`2` < `10`），其余按码位序，稳定且无浮点 |
| 保留数 = 向下折半，至少一个 | `retentionCount(n) = max(1, floor(n/2))` |
| 重开未结算试验：旧尝试结果与 Checkpoint 全部作废 | `reopenExperiment()` 立即置 `superseded`；旧结果不进入排名，旧 checkpoint 摘要不能再提交 |
| 迟到的分数不参与 | 结算后一切提交/重开都抛 `ROUND_CLOSED`，并写入审计日志 |
| 续训指令引用获选尝试的 Checkpoint，只给新增步数 | `continuation.commands[i] = { experimentId, attemptId, checkpoint, additionalSteps }`，没有绝对目标步数 |
| 两折相差十倍时晋级与续训起点可独立核对 | 排名行携带各折原始 `correct/samples`；`auditSettlement()` 用独立代码复算 |

## 公开 API

### `new Tournament({ stages? })`

`stages` 默认 `[20, 60, 180]`，必须严格递增的正整数。

### `tournament.openRound({ experiments?, folds })`

冻结下一轮的名单并开始接收提交。

- 第 1 轮：必须给 `experiments`（字符串数组，非空、无重复）和 `folds`。
- 后续轮：`experiments` 可省略；若给出，必须与上一轮晋级名单**集合相同**，
  内部会重排为晋级顺序。只允许上一轮的晋级者参赛。
- `folds` 可省略，此时沿用上一轮已验证的折名单；给出时会重新校验。
  - 形式：`['fold-a', 'fold-b']` 或 `[{ name, samples }]`。
  - 折样本数一旦在某轮确定，后续轮不得改变（`FOLD_SAMPLE_CONFLICT`）。
- 返回冻结的 roster：`{ round, trainedTo, folds, experimentIds }`。
- 到达最后一轮后再调用 → `TOURNAMENT_COMPLETE`。

### `tournament.submitAttempt({ experimentId, attemptId?, folds, checkpoint, continuedFrom? })`

提交某试验本轮的最终结果。

- `folds`: `[{ name, correct, samples }]`，必须恰好覆盖冻结折名单（顺序无关）。
- `checkpoint`: `{ path, step, digest }`。`step` 必须等于本轮目标步数；`digest` 必填，
  它是 checkpoint 的内容身份，用来识别"改名重交的旧 checkpoint"。
- `continuedFrom`（第 2 轮起必填）：`{ attemptId, path, step, digest }`，
  必须精确等于上一轮为**该试验**生成的续训指令中的 checkpoint。
- 返回冻结的回执，含 `correct`、`samples`、`score`、`scoreRatio`（约分后的精确分数串）。

### `tournament.reopenExperiment({ experimentId, reason?, attemptId? })`

重启尚未结算的试验。调用后当前尝试立即作废：

- 它的各折结果不再参与排名（列在 `settlement.disqualifiedAttempts`）。
- 它的 checkpoint 摘要不可再次提交（`SUPERSEDED_CHECKPOINT`），换路径也不行。
- 新尝试必须用新的 `attemptId`；在重新提交前该试验无法参与结算。

### `tournament.settleRound({ requireAllSubmissions = true })`

结算本轮并生成续训指令。

- 默认要求"收齐"：任一试验当前没有有效提交则抛 `ROUND_INCOMPLETE`。
  传 `{ requireAllSubmissions: false }` 可显式结算部分轮次，缺席者记入
  `excludedExperiments`，永不晋级。
- 返回冻结的 settlement：
  - `ranking`：按成绩降序、同分按试验编号；每行含 `rank`、`folds`（原始计数）、
    `correct`、`samples`、`scoreRatio`、`promoted`、`checkpoint`。缺席者 `rank: null` 排在末尾。
  - `promotedExperimentIds`、`retainedCount`、`eligibleCount`、`retentionRule`。
  - `tieGroups`：同分组、同分内晋级者、是否跨越晋级边界。
  - `disqualifiedAttempts`、`excludedExperiments`。
  - `continuation`：`{ round, targetStep, commands[] }`；最后一轮为 `null`。
  - `settlementDigest`：对规范化 JSON body 的 SHA-256。

### `tournament.snapshot()` / `tournament.auditLog` / `tournament.settlements`

冻结的完整状态、按序审计事件（`round_opened`、`attempt_submitted`、
`attempt_rejected`… `attempt_superseded`、`round_settled`）、历次结算。

### 辅助导出

- `verifySettlementDigest(settlement)`：独立重算摘要并比对。
- `computeSettlementDigest(settlement)`：规范化 JSON 的 SHA-256。
- `auditSettlement(settlement)`：**与协调器不共享排名逻辑**的独立审计，返回
  `{ ok, findings[] }`，逐项复算：每行分数、排名顺序、保留配额、晋级名单、
  续训指令的起点/增量/目标。
- `retentionCount`、`compareExperimentIds`、`ratioString`、`aggregateFolds`。

### 错误

所有预期错误抛 `CoordinatorError`，带稳定 `code`。常用：

`INVALID_ROSTER`、`INVALID_STAGES`、`ROUND_STILL_OPEN`、`ROUND_CLOSED`、
`ROUND_INCOMPLETE`、`NO_ELIGIBLE_EXPERIMENTS`、`TOURNAMENT_COMPLETE`、
`ROSTER_MISMATCH`、`STAGE_MISMATCH`、`UNKNOWN_EXPERIMENT`、`INVALID_COUNT`、
`FOLD_SET_MISMATCH`、`FOLD_SAMPLE_MISMATCH`、`FOLD_SAMPLE_CONFLICT`、
`INVALID_CHECKPOINT`、`CHECKPOINT_STEP_MISMATCH`、`MISSING_CONTINUATION_BASE`、
`CONTINUATION_BASE_MISMATCH`、`ATTEMPT_ALREADY_ACTIVE`、`ATTEMPT_ID_MISMATCH`、
`DUPLICATE_ATTEMPT_ID`、`NOTHING_TO_REOPEN`、`SUPERSEDED_CHECKPOINT`。

## 设计要点

**成绩按样本加权。** 两折大小相差十倍时，宏平均会让小折权重被夸大十倍。
`aggregateFolds` 先累加 `correct` 与 `samples` 再相除，保证大折主导。

**同分判定用整数比较。** 排名用交叉相乘（`correct_a * samples_b` vs `correct_b * samples_a`）
的 `BigInt` 比较，`scoreRatio` 用约分后的 `BigInt` 分数串，避免浮点把不同分数判成同分，
或把同分判成不同分。

**作废是永久且可追溯的。** 被重开的尝试保留在 `attemptLog` 与 `snapshot()` 中，
状态为 `superseded`，并记录 `supersededBy` 与原因。旧 checkpoint 靠内容摘要
（而非路径）识别，防止改名后蒙混过关。

**迟到结果无法翻盘。** 结算后 `submitAttempt`/`reopenExperiment` 都直接被拒，
并写入 `submission_rejected` 审计事件，原结算不变。

**续训只给增量。** 指令形如
`{ experimentId, attemptId, checkpoint: { path, step, digest }, additionalSteps }`。
起点由 `checkpoint.step` 表达，目标在 `continuation.targetStep` 统一给出；
`additionalSteps = targetStep - checkpoint.step`，指令本身不含绝对目标。

## 验证范围

测试分三类，共 41 条（`tests/coordinator.test.mjs` 30、`tests/util.test.mjs` 6、
`tests/audit.test.mjs` 5，均已执行且通过）：

- **正常**：6 试验 20→60→180 全程、单试验、每轮保留数检查。
- **边界**：保留数为 1 的上限/下限、空/重复折名单、折尺寸改变、
  checkpoint 步数不符、缺摘要、文件名冲突、部分结算、重复重开。
- **交错**：重开后再提交、结算后迟到高分、重开后被遗忘的试验、
  同分跨晋级边界、续训引用校验、跨轮折尺寸继承、审计日志顺序。

**独立核对**：`auditSettlement()` 只用 settlement 里的原始各折计数重算，
不调用协调器排名代码；`examples/demo.mjs` 在 10:1 折上跑完整流程并逐项比对，
同时验证篡改摘要会被 `verifySettlementDigest` 拒绝。

### 已执行

```
node --test 'tests/*.test.mjs'   # 41 passed, 0 failed
node examples/demo.mjs           # DEMO PASSED
```

### 未覆盖（如实说明）

- 未运行真实训练或真实数据集；所有数为合成数据，验证的是协调与记账逻辑。
- 未做多进程/网络并发；并发提交假设调用方串行调用 API。
- 未验证超过 `Number.MAX_SAFE_INTEGER` 的极端计数组合（单折已校验上界，
  累加溢出会抛 `COUNT_OVERFLOW`）。
- 未对 checkpoint 文件本身做 I/O；`digest` 由调用方提供，协调器只比较字符串。
