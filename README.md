# dataflow — 引用计数数据流执行内核与小图缓冲配置器

纯 Go 1.20 标准库实现，无任何第三方依赖。一块输入可被多个下游引用，
**只有最后一个引用被释放时，共享池名额才归还**。

本仓库从空工作区建立，`go.mod` 声明 `go 1.20`，在 **go1.20.2 darwin/arm64**
上用标准库工具链完成全部验证。

---

## 1. 执行模型（与题意的对应）

- **算子（Op）**：执行输入给定的有限、直线型指令序列（无循环，PC 单调前进）。
- **四类指令**（`InstrKind`）：
  | 指令 | 构造函数 | 语义 |
  |---|---|---|
  | 取块 `take` | `TakeInstr(edge, into)` | 从输入边 FIFO 队首取一个引用；**边槽立即归还**，引用转为算子持有，引用计数不变 |
  | 申请输出槽 `alloc` | `AllocInstr(as)` | 从共享池申请一个物理块名额，新建物理块，初始 `refs=1` 由本算子持有 |
  | 发布 `publish` | `PublishInstr(h, targets...)` | **原子地**检查并一次获得所有目标边各 1 个空闲 FIFO 槽；任一目标边满则全部不获取、不入队；成功后每条边入队同一物理块的一个引用，`refs` 按目标数增加 |
  | 释放引用 `release` | `ReleaseInstr(h)` | 删除一个持有引用；`refs` 归零时删除物理块并**归还共享池名额**，事件中 `Last=true` |
- **边（Edge）**：带编号 `EdgeID` 的 FIFO，容量 `Config.EdgeCaps[id]`，占用 = 队列长度。
- **共享池**：容量 `Config.Pool`，占用 = 存活物理块数。每个物理块始终占 1 个池名额，直到最后引用释放。
- **阻塞等待的资源**（`Wait.Resource`）：
  - `pool_slot`：申请输出槽但共享池满；
  - `edge_slot`：发布时目标边 FIFO 满（扇出被几条满边阻塞就报几个 `Wait`）；
  - `edge_item`：取块时输入边为空，等待上游产物。
- 阻塞的 `Step` **不改变任何状态**（有专门的前后快照相等断言）。

### 为什么 DAG 也会死锁

资源依赖环不等于图拓扑环。测试中的无环链 `A -1-> B -2-> C`，
`B`/`C` 都“先申请输出块、后消费输入”：

- `pool=1`：`C` 先 `alloc` 占住唯一池名额，再等边 2 的输入 → 一步即全局卡死
  （`Verify` 找到的**最短轨迹长度 1**；另有 `B,B,B` 的三步卡死轨迹）。
- `pool=2`：搜索还发现一条手工分析容易漏掉的 **4 步交错死锁**
  `A(alloc,publish,release), C(alloc)`：`A` 的块停在边 1、`C` 占第二个池名额，
  `B` 无法在消费边 1 之前 alloc，`C` 又在等 `B` 的边 2 输出。
- `pool=3`（等于全图 Alloc 指令总数）：`Verify` 遍历全部交错（76 个规范状态）
  确认任意调度都能跑完。

---

## 2. 公开 API

根包 `dataflow`（见 [go doc](#复现步骤) 或源码注释）：

### 建模
- `OpID`（string）、`EdgeID`（int，**边号**）、`Handle`（算子局部句柄）、`BlockID{Origin, Index}`。
  直线程序中每条 Alloc 指令最多执行一次，因此“Alloc 的算子 + 指令下标”就是跨交错、
  跨重放稳定的物理块身份。
- `Graph{Ops []OpID; Edges []Edge}`、`Edge{ID, From, To}`。
- `Instr` 与构造器 `TakeInstr / AllocInstr / PublishInstr / ReleaseInstr`。
- `Config{Pool int; EdgeCaps map[EdgeID]int}`。

### 逐步驱动（最多 5 个算子，`MaxOperators=5`）
- `NewDriver(g, programs, cfg) (*Driver, error)`：校验图、程序（句柄 def/use、
  边端点/生产者/消费者、扇出目标去重等）与配置；超过 5 个算子返回 `*ProgramError`。
- `(*Driver).Step(op) (*Record, error)`：执行该算子下一条可执行指令；
  阻塞时返回 `*BlockedError`（用 `AsBlocked` 或 `errors.As` 取出 `Waits`），状态不变；
  已完成返回 `ErrOperatorDone`。
- `(*Driver).StepAny() (*Record, error)`：按算子声明顺序执行第一个使能动作；
  无动作且未完成时返回可 `errors.Is(err, ErrNoEnabledAction)` 的错误（含全部 blockers）。
- 观察：`Enabled() []Action`、`Blockers() []Blocker`、`AllDone()`、`OperatorDone`、
  `PoolUsed()`、`RefCount(BlockID)`、`Holders(BlockID) map[OpID]int`、
  `Queue(EdgeID)`、`EdgeUsed(EdgeID)`、`Trace() []Record`、`CheckInvariants()`。
- 并发：`Driver` 内置互斥锁，`RunConcurrent(ctx, d)` 为每个算子起一个 goroutine
  真实并发驱动；互斥锁把并发调用线性化为 `Verify` 检查过的某条交错。

### 轨迹中间还原持有者
- `(*Driver).Export() Snapshot`：深拷贝、可 JSON 序列化的状态（PC、句柄、各 FIFO、
  每块 refs 与持有者多重集）。
- `NewFromSnapshot(g, programs, Snapshot)`：从快照重建并**校验记账不变量**，
  损坏快照会被拒绝。
- `(*Driver).Restore(Snapshot)`：原地恢复。
- `Replay(g, programs, cfg, trace, upto) (*Driver, error)`：重放轨迹前 `upto` 条记录，
  并核对指令下标与事件块身份；`upto` 为负表示全部。可在轨迹任意中间点得到 `Holders`。

### 全交错验证与配置器
- `Verify(g, programs, cfg) (*Verdict, error)`：对所有合法算子交错做 BFS
  （规范状态去重）。直线程序使每条完整运行恰好执行 `sum len(program)` 步，
  因此“不存在可达的未完成但无使能动作的状态”等价于
  **所有能推进的调度都能跑完**。非鲁棒时 `Verdict.Deadlock` 是 BFS 给出的
  **全局最短卡死轨迹**、终端快照和每个 `Blocker` 等待的 `[]Wait` 资源。
- `Configure(ConfigRequest) (*ConfigResult, error)`：
  - `Pool IntRange`、每边 `Edges map[EdgeID]IntRange`（或 `DefaultEdge`）给出允许容量范围；
  - `Budget *int`：可选的总名额上限 `Pool + Σ edgeCap ≤ Budget`；
  - `CostBudget *int64`：可选的加权成本上限（覆盖“预算即加权成本”的解读）；
  - `Weights{Pool, EdgeDefault, Edge}`：目标
    `WPool*Pool + Σ_e WEdge[e]*cap_e`，`nil` 权重默认 1，允许显式 0；
  - 枚举所有预算内组合并逐个 `Verify`；
  - `Feasible=true` 时 `Best` 为最优鲁棒配置；比较键由 `CompareConfigs` 定义：
    **加权成本升序 → 按边号升序的边容量字典序（第一个不同的边，容量小者胜）
    → 池容量小者胜**；
  - 没有任何鲁棒组合时 `Feasible=false`，`Diagnostic` 取所有候选中**长度最短的
    卡死轨迹**（等长再按同一比较键选配置），`Attempts` 保留每个候选的成本/状态数/
    最短死锁长度；
  - 预算连最小容量组合都放不下时返回 `*NoCandidateError`（含 `MinRequired`）；
    加权预算低于最小成本返回 `*CostBudgetError`。

---

## 3. 复现步骤

要求 Go 1.20.2（`go version` 应显示 `go1.20.2 ...`）。无需联网、无需安装依赖。

```bash
cd <本工作区根目录>

# 主验收命令（题目要求）
go test ./...

# 详细列出全部用例与 Example 的真实输出比对
go test -v ./...

# 带全部 vet 检查
go test -vet=all -count=1 ./...

# 真实并发交错的竞态检测（已实际执行并通过）
go test -race -count=1 ./...

# 重复执行排除并发用例抖动（已实际执行 5 次并通过）
go test -count=5 ./...

# 查看文档
go doc .
```

可运行示例（其 `// Output:` 由 `go test` 真实执行比对，不是手写预置输出）：

- `ExampleConfigure`：选出 `pool=3, edge1=1, edge2=1, cost=5`；
- `ExampleVerify_deadlock`：打印 `pool=1` 的一步最短死锁及 A/B/C 等待的资源；
- `ExampleDriver_Step`：观察 fanout 后 `refs=2 pool=1`、C1 释放后仍 `refs=1 pool=1`、
  C2 最后释放 `last=true pool=0`。

---

## 4. 验证范围（均为真实执行结果）

最近一次 `go test -v ./...`：**26 个顶层测试 + 5 个表驱动子用例 + 3 个 Example 全部 PASS**；
`go test -race -count=1 ./...`、`go test -vet=all -count=1 ./...`、
`go test -count=5 ./...` 均 `ok dataflow`；`gofmt -l .` 无输出。

| 类别 | 覆盖点（测试） |
|---|---|
| 正常路径 / 核心引用计数 | `TestFanoutLastReaderReturnsPool`：双下游 fanout、取块立即还边槽、C1 释放后池仍占用、最后读者 C2 释放才还池，含逐步真实断言 |
| 扇出原子性 | `TestAtomicFanoutAllOrNothing`：满边阻塞时等待精确报为 `edge_slot(edge=1)`、状态/PC 不变、边 2 无部分入队；腾槽后两边一次获得 |
| 边界 | `TestPoolSlotBlocking`（pool cap=0/1）、`TestZeroCapacityEdgeIsPermanentBackpressure`（边 cap=0 且 Verify 判死）、`TestEmptyProgramsFinishImmediately`、`TestDoneOperatorAndUnknownOp`、`TestDriverMaxFiveOperators`（第 6 个算子被驱动接口拒绝，而 Verify 仍可分析） |
| 预算边界 | `TestConfigureBudgetBoundaries`（预算 2 < 最小 3 报 `NoCandidateError`；预算 3 仅 1 候选且不可行；预算 4 共 4 候选仍不可行；预算 5 得到鲁棒 `(3,1,1)`；无预算 12 候选）、`TestCostBudgetBoundary`（加权预算边界、负权重拒绝）、`TestDefaultEdgeRange` |
| 优化与并列 | `TestConfigurePicksMinimalRobustPool`、`TestConfigureWeightedTradeoff`、`TestCompareConfigsEdgeNumberTieBreak`（等成本交叉向量按边号 1 先比）、`TestConfigureTieBreakEndToEnd`（零边权重制造成本并列；边声明顺序故意打乱，仍按数字边号选字典序最小） |
| 不可行诊断 | `TestConfigureInfeasibleReturnsShortestTrace`：4 个候选全部非鲁棒、诊断为长度 1 的 C 先 alloc 轨迹、3 个 blocker 资源类型正确、诊断配置为并列最优 `(1,1,1)`；轨迹可 Replay 到同一卡死态 |
| 交错 / 死锁 | `TestDAGScriptedDeadlockPrefix`（B,B,B 三步卡死与三类等待）、`TestVerifyFindsShortestDeadlock`（BFS 最短长度 1 + 资源）、`TestPoolTwoAlsoDeadlocks`（真实搜索发现的长度 4 隐蔽交错）、`TestPoolThreeRobustAllInterleavings`（76 状态全交错鲁棒 + 同一坏前缀可跑完 12 条指令）、`TestFanoutRobustEveryInterleaving` |
| 真实并发交错 | `TestConcurrentDriversCompleteUnderRobustConfig`（60 轮每算子独立 goroutine）、`TestConcurrentFanoutRefcountStress`（40 轮共享块引用计数压力）；每轮断言 `AllDone`、`CheckInvariants`（refs == 队引用 + 持有数、队长 ≤ cap、池占用 ≤ cap）且池无泄漏；`-race` 通过 |
| 故障/状态恢复 | `TestRestoreHoldersFromTraceMiddle`：轨迹中点 holders 精确为 `{C1:1}`；Replay、JSON 导出/导入重建、原地 Restore 三路得到相等状态，跑完同一后缀后与原驱动收敛；`TestReplayRejectsForeignTrace`（篡改下标被拒）、`TestSnapshotRejectsCorruption`（refs 造假被不变量拒绝） |
| 输入校验 | `TestInvalidPrograms`：跨属主取边、错误生产者发布、未绑定句柄、重复绑定、fanout 目标重复，均返回 `*ProgramError` |

### 明确未做 / 限制

- 未实现循环/回边指令：题面指令序列是有限直线序列；因此不存在活锁，BFS 状态空间有限。
- 配置器按“小图”设计为穷举容量笛卡尔积与交错状态；没有做符号求解或大规模图扩展。
- 未访问任何网络、未安装依赖；全部数据为测试内本地合成图。
- 没有独立运行裸 `go vet ./...`（该命令在当前执行环境的权限策略下未获批准）；
  等价地已实际运行带完整 vet 检查的 `go test -vet=all ./...` 并通过。
