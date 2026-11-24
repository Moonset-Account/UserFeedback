# Rebalancer 协调器

本项目实现一个仅使用 Go 标准库的分区再均衡协调器。输入至多 16 个不可拆分区及其权重、消费者容量、可订阅集合、故障域和原持有人，输出：

1. 精确最优终局归属表；
2. 满足在途权重和同族可用性约束的最少轮次移交计划；
3. 可逐轮重放的动作清单、可消费副本数和最终负载；
4. 换主后的安全恢复策略，以及成员离开/重新加入分支的最坏额外轮次；
5. 同日志前缀下双重授予造成双重消费的可执行反例。

## 环境与复现

- Go：`go1.20.2 darwin/arm64`
- 依赖：无第三方依赖，仅使用标准库（`math/big`、`math/rand`、`sort`、`testing` 等）
- 无网络访问、无真实账户或系统目标，全部数据为测试内合成数据。

```sh
go test ./... -v
go test ./... -count=2
```

最近一次验证结果：15 个测试全部通过；`-count=2` 重复运行通过，结果确定。16 分区完整求解、调度和重放约 0.9 秒（测试预算为 2 秒）。

## 公开 API

### 输入模型（`model.go`）

- `const MaxPartitions = 16`
- `type Partition struct`：分区号、权重、数据族、故障域、原持有人 `Origin`（-1 表示无原持有人）、每个消费者的订阅布尔集。
- `type Consumer struct`：消费者号、容量、故障域。
- `type Problem struct`：分区、消费者和 `InFlightCap`（单批移交中允许同时处于 RELEASED 状态的总权重上限）。
- `type Assignment []int`：按分区号索引的消费者归属表。
- `(*Problem).Validate() error`：校验分区数量、正权重/容量、订阅、稠密 ID 等结构条件。
- `(*Problem).MaxNormalizedLoad(Assignment) *big.Rat`
- `(*Problem).MigrationWeight(Assignment) int`

### 精确优化（`solve.go`）

- `type Target struct`：终局表、精确最大归一化负载、迁移权重、可达目标排名。
- `(*Problem).Solve() (Target, error)`：全局最优。
- `(*Problem).SolveFixed(fixed map[int]int) (Target, error)`：固定已完成移交的分区后再优化。
- `(*Problem).SolveReachable() (Target, *Plan, error)`：若最优终局不能安全移交，则按严格目标顺序选择下一个可安全到达的解。
- 哨兵错误：`ErrInfeasible`、`ErrNoReachableAssignment`。

目标按以下字典序严格比较，全部使用 `math/big.Rat` 精确分数：

1. 最小化 `max_c load[c] / capacity[c]`；
2. 最小化迁出原持有人的分区总权重；
3. 按分区号最小化归属表字典序。

实现采用两阶段精确搜索：先在所有可实现的归一化负载候选分数上，用带硬容量上界的可行性 DFS 找到最小可行最大负载；再在该负载下用分支限界 DFS 最小化迁移权重并解析字典序平局。

### 移交计划（`schedule.go`）

- `type Grant struct`：分区、目标/来源消费者、世代号。
- `type Round struct`：一轮中的 `Revokes`、可观察的 `Releases` 和 `Grants`。
- `type Plan struct`：目标归属和有序轮次。
- `(*Problem).StartPhases(Assignment) []byte`
- `(*Problem).Schedule(Assignment) (*Plan, error)`
- `(*Problem).ScheduleFrom(Assignment, []byte) (*Plan, error)`

协议阶段：

- `STABLE-AT-SOURCE`：旧持有人仍可消费；
- `RELEASED`：撤销已发出、带世代释放回执已成为协调器可观察状态，新授予尚未发出；
- `STABLE-AT-TARGET`：新持有人可消费。

一个分区的 RELEASED 状态至少跨越一轮，因此它自己的撤销和授予不会出现在同一轮。BFS 保证最少轮次，并在每个轮边界强制：

- RELEASED 分区总权重不超过 `InFlightCap`；
- 每个数据族至少保留一个可消费镜像。

### 动作重放（`replay.go`）

- `type RoundSnapshot struct`：阶段、当前持有人、每族可消费副本数、在途权重、各消费者负载。
- `type ReplayResult struct`：所有轮边界快照、最终归属和精确最终最大负载。
- `(*Problem).Replay(plan *Plan, seed int64) (*ReplayResult, error)`
- `(*Problem).ReplayFrom(plan *Plan, start []byte, seed int64) (*ReplayResult, error)`：从换主恢复重建的显式阶段向量开始重放续跑计划。

`seed=0` 按规范顺序投递回执；其他种子会确定性打乱每轮回执顺序，模拟乱序到达。协调器只有在观察到匹配分区和世代的释放回执后才授予。重放独立从输入重建持有人和负载，而不是信任计划的终局。

### 恢复与成员变化（`recovery.go`）

- `type GenerationReport struct`：消费者稍后报告的最高观察世代。
- `type DurableRound struct`：持久轮次及其撤销。
- `type RecoveryPlan struct`：重建的起始阶段、不可区分分区、续跑计划和额外轮次。
- `(*Problem).Recover(durable []DurableRound, reports []GenerationReport, target Assignment) (*RecoveryPlan, error)`
- `type MembershipOutcome struct`：成员变化分支、可行性、目标、计划、额外轮次和陈旧声明数。
- `(*Problem).EvaluateLeave(leaving int, phases []byte, currentHolder []int, remaining []int) (*MembershipOutcome, error)`
- `(*Problem).EvaluateRejoin(returning int, phases []byte, currentHolder []int, staleClaims []int) (*MembershipOutcome, error)`
- `WorstCaseExtraRounds(branches []*MembershipOutcome) (*MembershipOutcome, error)`

新主只信任持久轮次和消费者报告。若某撤销可能未持久化、旧持有人和 RELEASED 状态不可区分，则保守视为旧持有人仍可消费并在下一轮以新世代重新撤销；已完成移交通过固定持有人按现状继承。

### 双重消费反例（`fence.go`）

- `type SplitBrainResult struct`
- `SimulateSplitBrain(partition, oldHolder, newHolder int, sharedGeneration int64, oldStillServes bool) (*SplitBrainResult, error)`

两个具有相同日志前缀的现场发出同一项授予时，无 broker 世代栅栏的世界会让旧持有人和新持有人同时消费；带栅栏的世界拒绝未严格递增的陈旧授予。反例会明确指出不安全世界中产生第二个消费者的现场（`site-B`）。

## 测试覆盖与验收场景

| 测试 | 场景类型 | 真实断言 |
| --- | --- | --- |
| `TestNormalOptimalAssignment` | 正常 | 手算最优归属、精确负载 1/1、零迁移、零轮计划、族/域约束 |
| `TestMigrationTwoRoundsAndReplay` | 正常 + 交错 | 最优负载/迁移、最少轮次、同轮授予+撤销流水、顺序与乱序回执快照一致 |
| `TestInFlightCapSerializes` | 边界 + 交错 | cap 触发串行、cap 不足时不可达、中间轮明确包含 1 授予 + 1 撤销 |
| `TestScaleSixteenPartitions` | 规模边界 | 16 分区、4 族 4 域完整求解/调度/重放，独立校验每轮安全不变量和 2 秒预算 |
| `TestFallbackToNextReachable` | 故障/安全边界 | 最优终局不可安全移交时自动选择下一个可达解，并验证排名和重放终局 |
| `TestZeroRoundsWhenAlreadyOptimal` | 边界 | 已最优时 0 轮、初始快照仍有可消费副本 |
| `TestExactFractionComparison` | 正常 | 不等容量下精确断言最大负载为 2/3，避免浮点误差 |
| `TestLexicographicTieBreak` | 边界平局 | 负载和迁移完全平局时按分区号选字典序最小表，重复求解确定 |
| `TestValidation` | 输入边界 | 0/17 分区、零权重、无订阅者、族内故障域冲突等错误路径 |
| `TestRecoverAfterLeaderSwap` | 故障恢复 | 换主后无报告/有世代报告/空持久日志三种分支，验证安全阶段和额外轮次 |
| `TestMemberLeaveBranches` | 成员离开 | 无幸存订阅者时不可行；可行分支继承现状；孤儿分区至少需要授予轮 |
| `TestMemberRejoinAndWorstCase` | 成员重加入 + 分支比较 | 陈旧声明增加栅栏轮次，比较所有允许分支的最坏额外轮次 |
| `TestRecoverValidation` | 恢复边界 | 未知消费者报告返回 `ErrInvalidInput` |
| `TestSplitBrainDoubleConsumptionCounterExample` | 双重消费反例 | 无栅栏世界断言双重消费且定位到 `site-B`，栅栏世界断言不双重消费 |
| `TestSplitBrainValidation` | 反例边界 | 同持有人、非正世代等非法输入被拒绝 |

## 验证范围和限制

- 已验证：正常路径、容量/在途边界、族可用性边界、乱序回执、最少轮次 BFS、不可达时回退、换主恢复、成员离开/重加入、同日志前缀双重消费反例、16 分区规模。
- 所有验收均由 `go test` 执行真实功能测试产生，没有用预置演示输出替代断言。
- 未执行：真实网络、真实 broker、真实消费者进程或外部系统集成；这些按题目约束不在本地合成测试范围内。
