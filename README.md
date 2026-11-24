# realtimeadmit

一个小规模、精确穷举的实时准入调度库，使用 Go 1.20.2 和标准库实现。输入最多 8 个请求；库会穷举组批、两条通道、等待时机、阶段分支、场景相关性和取消揭示边界，返回收益最高且可保证截止期的准入集合、可由虚拟时钟执行的分支调度、最坏余量，以及拒绝固定集合时的结构化分支依据。

## 复现步骤

环境要求：

```bash
go version
# 应显示 go version go1.20.2 ...
```

在本目录运行真实测试：

```bash
go test -count=1 ./...
```

可选的竞态检查：

```bash
go test -race -count=1 ./...
```

格式化检查：

```bash
gofmt -l .
```

该命令无输出表示格式正确。

## 核心语义

- 时间、释放时间、截止时间和批耗时均为整数虚拟时间。
- 截止期包含边界：`completion <= deadline` 即余量非负；恰好完成时余量为 `0`，仍算可保证。
- 有两条对称、可并行的模拟执行通道。
- 只有相同 `Shape` 且处于相同 `Stage` 的就绪请求能进入同一批。
- `Durations[BatchCost{Stage, Shape, Size}]` 给出**确切批大小**的整数耗时。若各 shape 共用耗时表，可用 `Shape: "*"`（或空字符串）作为回退键。
- 批次一旦开始不可抢占；取消在批次运行中揭示时，也只能设置 pending 状态，当前批仍运行到完成。
- 每个请求先执行 stage 1。stage 1 完成后，是否进入 stage 2 由 `Scenario.Continue` 的联合结果决定；未标记的 ID 表示 stage 1 后提前结束。
- `Scenario` 是允许的有限联合结果。不会把不同场景中的最坏情况任意笛卡尔组合；因此两个最坏情况若从不出现在同一场景中，可以利用这种相关性。
- `Cancellation.RevealAt` 是取消信息的揭示边界。驱动在到达该虚拟时间前不会返回或使用该取消。
- 默认开启严格取消信息屏障：求解器会加入“已揭示前缀相同、但未来取消不发生”的认知影子分支。因此，若一个集合只有提前看到未来取消才可行，`Admit` 不会虚报可接单。`Options.DisableCancellationBarrier` 只用于测试“全知/oracle”对照，不应作为安全准入选项。
- 最坏余量是所有真实请求、所有公开场景中的最小 `Deadline - CompletionAt`。严格屏障产生的 shadow 场景只是防提前窥视的证明分支，不计入 `MarginReport.ByScenario`。

## 公开 API

### 输入类型

- `Request{ID, Release, Deadline, Shape, Profit, Committed}`
  - `Committed=true` 表示既有承诺，任何成功准入方案都必须包含并保证。
  - `Profit` 是新请求被准入时的收益；求解器最大化新准入请求的总收益。
- `BatchCost{Stage, Shape, Size}` 与 `Problem.Durations`
- `Scenario{Name, Continue, Cancellations}`
- `Cancellation{RequestID, RevealAt}`
- `Options{AdmissionTime, DisableCancellationBarrier}`

规模与校验：

- `len(Problem.Requests) <= 8`
- 公开场景最多 256 个；内部认知场景上限为 1024。
- 每个请求必须有对应 shape 的 stage 1 单请求耗时。
- 非法 ID、重复 ID、负时间、非正耗时等通过 `Validate` / `Admit` 的 `error` 返回。
- 时间上不可行不属于输入格式错误；会返回 `Feasible=false` 和 `Witness`。

### 准入与固定集合判定

```go
func Admit(p Problem) (*Result, error)
func AdmitWithOptions(p Problem, opts Options) (*Result, error)

func EvaluateSet(p Problem, ids []string) (*Verdict, error)
func EvaluateSetWithOptions(p Problem, ids []string, opts Options) (*Verdict, error)
func EvaluateCandidates(p Problem, candidateIDs []string) (*Verdict, error)

func VerifyWitness(w *Witness) error
```

- `Admit`：保证所有 `Committed` 请求，穷举候选子集并选择总收益最高者；同收益时选择最坏余量更大的方案。
- `EvaluateSet`：精确判定给定固定集合。
- `EvaluateCandidates`：判定所有 committed 请求加上给定候选 ID。
- `Result` / `Verdict` 包含：
  - `Feasible`
  - `Accepted` / `NewlyAccepted` / `RejectedCandidates`
  - `TotalProfit`
  - `OptimalMargin`
  - `Margins.ByScenario` / `Margins.ByRequest`
  - `Plan`
  - `Witness`

### 分支调度

`Plan.Root` 是递归的 `PlanNode`：

- `Start *BatchAction`：当前时刻启动一个不可抢占批次，然后沿 `Then` 处理同一时刻的第二次启动或后续决策。
- `Branches []PlanBranch`：等待到下一个揭示边界，并按 `Observation` 选择分支。
- `Leaf *Leaf`：该分支完工，记录每个请求的完成时间和该路径余量。

`BatchAction` 包含通道、阶段、shape、排序后的请求 ID、开始时间、耗时和完成时间。

### 虚拟时钟驱动

```go
func NewVirtualClock(start int) *VirtualClock
func (c *VirtualClock) Now() int
func (c *VirtualClock) AdvanceTo(t int) error
func (c *VirtualClock) Advance(d int) error

func NewVirtualDriver(p Problem, plan *Plan, scenarioName string) (*VirtualDriver, error)
func (d *VirtualDriver) Step() (bool, error)
func (d *VirtualDriver) Run() (*RunReport, error)
func (d *VirtualDriver) RevealedCancellations() []string

func ValidatePlan(p Problem, plan *Plan) (*MarginReport, error)
```

驱动不使用真实时间、goroutine 或 `sleep`。它按虚拟时间启动批次、记录并行区间、在完成时揭示 stage 1 联合结果、只在 `RevealAt` 边界揭示取消，并校验：

- 批次大小和耗时表一致；
- 请求不会早于 release 启动；
- 取消的请求不会在揭示后启动；
- 运行中批次不会被抢占；
- 每个公开场景都能匹配唯一观测分支；
- 所有完工时间满足截止期。

### 拒绝依据

`Witness.Root` 是结构化的强制失败证明：

- 叶子可能给出具体请求、截止期和不可更改的批次完成时间；
- `Observation` / `Child` 给出对抗性联合结果或取消分支；
- `DecisionProofs` 枚举该信息状态下每个合法批次启动和 idle/wait 决策，并附上该决策失败的对抗分支。

由于求解器是穷举的，若所有决策都有失败证明，则从该信息状态出发不存在可保证截止期的调度。

## 已验证范围

当前 `*_test.go` 包含以下真实断言，而不是预置演示输出：

1. `TestCorrelatedWorstCasesCanBeExploited`
   - A、B 不会同时进入最坏的 stage 2；
   - 求解器接受 A+B，并在 `A-only`、`B-only` 两个场景中由虚拟驱动真实执行；
   - 断言收益、零最坏余量、X/A/B 的实际完工时间。
2. `TestUncorrelatedBothWorstCaseIsRejectedWithWitness`
   - 加入“A、B 同时继续”的场景后，同一集合必须被拒绝；
   - 验证 witness 结构。
3. `TestHiddenCancellationIsNotUsedForAdmission`
   - 构造 A+B+C 无取消时总工作量超过双通容量、A 在 t=2 取消后可行的例子；
   - 安全 `Admit` 只接受一个新请求；
   - `EvaluateSet(A,B,C)` 拒绝并给出 witness；
   - 关闭屏障的 oracle 对照会过度接受 B+C，用来证明提前看到取消确实会造成虚报；
   - 虚拟驱动断言取消只在 t=2 揭示。
4. `TestBatchingSameShapeAndParallelChannels`
   - 断言优化器选择 A+B 同 shape stage 1 批，而不是两个串行单批；
   - 驱动验证并行通道、完工时间和正余量。
5. `TestCancellationBoundaryInterleavesWithNonpreemptibleBatch`
   - t=1 揭示 A 的取消，但 A 的长批次 `[0,4]` 不被抢占；
   - B 的 stage 2 `[2,4]` 在另一通道启动并与 A 的批次尾部交错/并行。
6. `TestSequentialCancellationBoundariesRemainHiddenUntilRevealed`
   - 验证 t=1、t=2 两个取消边界按顺序揭示，且运行中批次仍不可抢占。
7. 边界与常规测试：
   - 空问题；
   - 1 个和恰好 8 个请求；
   - 9 个请求返回校验错误；
   - 截止时间相等时零余量可行；
   - 最高收益选择；
   - `"*"` 通配耗时表；
   - 不可行请求的 witness 含 `Completion > Deadline` 的强制冲突；
   - 虚拟时钟拒绝倒退。

`go test -count=1 ./...` 和 `go test -race -count=1 ./...` 已在 Go 1.20.2 / darwin-arm64 上实际运行通过。未运行外部依赖、真实网络目标或系统服务；本库无第三方依赖。
