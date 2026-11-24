# 整数字节时间出口调度器（egress）

本工作区从空目录建立，只使用 Go 1.20.2 标准库。模块按“一个整数字节占用一个 tick”建模，提供：

- 周期门控表与门切换保护带；
- 快车（fast）/普通（normal）两条 FIFO 队列；
- 普通帧在合法非末片边界暂停、续发原帧，后到普通帧不能插队；
- 按字典序优化：快车按期数 → 普通按期数 → 切片开销；
- 迟到帧仍按 FIFO 发完；永远放不下最小合法非末片时返回 `ErrUnsendable`；
- 具体片段、每片/总占用字节和每帧完成拍；
- 未知相位/备用门表故障恢复下的共同前缀在线策略、至多两次硬件时间戳观测、分支时机；
- 两个相似门表的“提前分片 vs 继续发送”复核器。

## 时间与合法性约定

这些约定把题面中的“拍”落实为可执行整数规则：

- 片段占用半开区间 `[Start, End)`，其中  
  `End = Start + Overhead + Data`。
- 帧按期条件为 `Completion <= Deadline`，完成拍取 `End`。
- `GateTable.Entries` 是持续时间序列，`GateEntry.Gates` 可用位掩码：
  `egress.GateFast`、`egress.GateNormal`、`egress.GateBoth`、`egress.GateClosed`。
- `Guard` 是本类门关闭前必须预留的保护字节。片段合法条件为：
  `End + Guard <= GateClose`。
- 非末片数据长度必须 `>= Config.MinNonLast`；末片是帧剩余的任意正整数，可以小于 `MinNonLast`。
- 周期表通过取模展开；平移整数个周期不会逐百万次展开表，因此百万周期前缀测试很快。

## 公开 API

核心离线 API：

```go
plan, err := egress.Schedule(egress.Config{
    Table:      table,          // *egress.GateTable
    Frames:     frames,         // []egress.Frame
    MinNonLast: 1,
    Overhead:   2,
})
```

主要返回类型：

- `Plan.Fragments []Fragment`：按时间排列的具体片段；
- `Fragment.Start/End/Data/Overhead/Bytes/Final`；
- `Plan.Frames` 与 `Plan.FrameByID`：完成拍、是否按期、片段数、数据/开销/占用字节；
- `Plan.FastOnTime/NormalOnTime/OverheadBytes/OccupiedBytes/Makespan`。

错误可用：

```go
var ue *egress.UnsendableError
errors.As(err, &ue)
errors.Is(err, egress.ErrUnsendable)
```

候选片段复核 API：

```go
ev, err := egress.CheckFragments(cfg, candidateFragments)
// ev.Valid()、ev.Violations、ev.GateMisses、ev.FastOnTime ...
```

在线策略 API：

```go
policy, err := egress.BuildPolicy(egress.PolicyConfig{
    Scenarios:     []egress.Scenario{{Name: "A", Table: tableA}, ...},
    Frames:        frames,
    MinNonLast:    1,
    Overhead:      2,
    Probes:        []egress.Probe{{ReadAt: 2}, {ReadAt: 3}}, // 最多 2 次
    CommitFastIDs: []string{"F1"},
})
err = policy.Verify()
planA, err := policy.ScenarioPlan("A")
```

`Scenarios` 中可以放主门表和备用门表，也可以放同一周期表的不同 `Origin` 相位。`Probe.Stamps` 可直接给硬件时间戳；留空时用 `GateTable.NextEdgeTimestamp` 生成下一门沿时间戳。

`PolicyNode` 的关键值：

- `Kind = "action"`：该观测历史下所有候选相位必须共用的一个发送决定；
- `Kind = "wait"`：空闲到观测点；
- `Kind = "observe"`：按硬件时间戳分成 `Branches`；
- `Observation.LatchedAt` 是时间戳锁存拍，`BranchAt` 是当前片段结束后的首个可分支片界；
- `Kind = "leaf"`：相位已唯一识别，后缀使用精确离线优化器。

题面要求的双表复核样例也已公开：

```go
report, err := egress.ReviewAmbiguousChoices()
// report.ContinueInA.FirstMiss.GateClose == 5
// report.SplitEarlyInB.FirstMiss.GateClose == 10
```

也可直接调用 `egress.StandardAmbiguityCase()` 取得两个表、四个/单个复核帧和两组候选片段。

## 复现步骤

环境：

```bash
go version
# go version go1.20.2 darwin/arm64
```

在本工作区根目录运行：

```bash
go test ./... -count=1
```

本次实现完成后的真实验证结果（耗时会随机器小幅变化）：

```text
ok  	egress	...
ok  	egress	...   # go test -race ./... -count=1
ok  	egress	... coverage: 78.0% of statements
```

最后一条覆盖率命令为：

```bash
go test ./... -cover -count=1
```

## 测试与题面场景对应关系

`offline_test.go`

- `TestScheduleFourShortFramesAvoidsCounterproductiveExtraSlice`  
  四个短帧 `N1/N2/F1/F2` 的正常交错路径。优化器得到：
  `N1[0,3)` → `F1[3,6)` → `F2[6,9)` → `N1[9,13)` → `N2[13,16)`，
  快车 2/2、普通 2/2 按期。反事实地把 `N1` 多切一片成 `1+1+1` 后，
  `F1/F2/N1/N2` 的完成拍分别变为 `9/12/15/18`，全部错过测试截止拍
  `6/9/13/16`，真实断言“多切一片同时耽误两边”。
- `TestMillionPeriodIdlePrefixIsPureTranslation`  
  把所有到达/截止拍平移 `1,000,000 × Period`，逐片段断言除时间整体平移外字段不变，完成拍也只平移同一常数。
- `TestUnsendableWhenNoMinimumLegalFragmentFits`  
  覆盖零数据容量、整帧放不下且窗口小于最小非末片、以及小于最小值的合法末片。
- `TestGuardBandForbidsFinishingIntoSwitch`  
  精确断言 `End+Guard <= Close` 的边界。
- `TestSharedFastWindowIsAlsoUsableByNormal` 与 `TestOriginModelsUnknownPhaseOffset`  
  明确共享窗口由 `GateBoth` 表达，并用 `GateTable.Origin` 表示未知相位平移。

`policy_test.go`

- `TestBuildPolicySharesPrefixThenBranches`  
  两个相位共用 `N1[0,3)`，在 tick 3 按不同下一门沿时间戳分支；承诺快车 `F1` 在两相位都于 6 按期，两个普通帧也都按期（最坏普通按期数为 2）。若空等到观测点再发快车和整个 `N1`，`N1` 会在 11 完成并错过截止拍 10，最坏普通按期数降为 1；因此共用提前分片是实际优化结果。
- `TestTwoTimestampObservationsGraduallyNarrowPhase`  
  三个候选相位先由第一次观测缩成 `2+1`，共同发送 `F1[3,6)` 后，第二次观测再区分剩余两个相位。
- `TestTimestampLatchedInsideUnwithdrawableFragment`  
  第一次观测在 tick 2 锁存，但根片段 `[0,3)` 已上链，因此 `LatchedAt=2`、`BranchAt=3`；片段不撤回，只在片界分支。
- `TestPolicyRejectsMoreThanTwoObservations`  
  第三次观测返回错误。
- `TestAmbiguousTwoTableReviewer`  
  两个表在 tick 3 前都只表现为“普通门开”。继续发送的 `[0,6)` 在 A 相位撞上 tick 5 门闭；提前分片方案 `[0,3)` + `[7,11)` 在 A 合法，却在 B 相位因第二片开销把所需长度变成 4，撞上 B 的 tick 10 门闭。

`scheduling_test.go`

- `TestLateFramesAreStillSentAndNormalFIFOHolds`  
  所有截止拍均不可能时，仍完整发送三帧且断言 `N2` 不出现在 `N1` 完成前。
- `TestConcurrentSchedulesDoNotShareMutableState`  
  24 个 goroutine 并发出调度；已用 `go test -race ./...` 验证。

## 算法范围

离线 `Schedule` 使用带记忆状态 DP。状态只包含两条 FIFO 队首下标、当前帧剩余字节和当前时间；对“一小段到达预约”会枚举合法末片和所有整数非末片长度，因此返回给定语义下的字典序最优解。状态数超过默认上限时返回 `StateLimitError`，不会静默给出未验证计划。

在线 `BuildPolicy` 在观测前要求同一动作在所有仍可行情景中都满足门约束；观测值相同的情景保持同一节点和同一决定，观测值不同才生成分支。唯一情景后切换到精确离线 DP。多情景且观测已耗尽时，继续搜索所有情景共同合法的动作，并按“承诺快车全部按期 → 最坏普通按期数最大 → 最坏快车/开销/完成和 tie-break”选择。

## 未做的事

- 未访问网络、真实硬件或外部系统；“硬件时间戳”由 `Probe.Stamps` 或 `GateTable.NextEdgeTimestamp` 的纯整数模型表示。
- 未安装依赖；没有第三方模块。
- 未声称运行过题目工作区之外的隐藏验证器；以上通过状态均来自本目录真实 `go test` 执行。
