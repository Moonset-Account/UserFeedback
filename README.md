# shutdowncoord — 容器组停机协调器

一个确定性、可重启、并发安全的 Go 库，负责把「停机请求 + 容器依赖图 + 带实例代次的钩子/退出事件」
转换成有序的动作流：**停止接收输入 → 执行停机钩子 → 请求退出 → 强制结束**。

仅使用 Go 标准库，`go.mod` 声明 `go 1.20`，已在 Go 1.20.2 上验证。

## 语义模型

- **排空边** `Edge{Consumer, Upstream}`：`Consumer` 正在读取 `Upstream` 的缓冲。
  `Consumer` 停止接收新输入后仍可能继续读上游缓冲，因此 `Upstream` 只有在
  `Consumer` **同时满足**「报告已读完」（`DrainReport`）与「已退出」（`ExitEvent`）之后，
  才被允许进入关闭阶段（执行停机钩子）。
- **并行**：互不依赖的分支各自推进；`advance` 每轮都跑到不动点，顺序由拓扑中的实例顺序决定。
- **实例代次**：所有事件和动作都携带 `InstanceGen`。代次不匹配的事件被忽略，代次参与动作 ID，
  因此动作在重启后仍是同一身份。
- **自发退出**：任何阶段收到 `ExitEvent` 都直接记为 `exited`。钩子返回只在实例处于
  `hook_running` 阶段时生效——**晚回的钩子不会把实例拉回处理中**，也不会生成 `request_exit`。
- **共享宽限截止**：`Start(now)` 把截止时间设为 `now + grace`，全组共用一个。到点后（`Tick` 或任意
  `Handle` 触发）对仍未退出的实例发 `force_kill`，并记录尚未完成排空的边（`IncompleteEdges`）。
- **重启**：每次输入后都把快照写入 `Store`。重启的协调器从快照恢复，`Start` 变成 no-op，
  `Resume()` 只重放**未确认且仍然相关**的动作；已 `ActionAck` 的动作永不重复发出。

## 公开 API

```go
type ContainerID string
type InstanceGen uint64
type Instance struct { Container ContainerID; Gen InstanceGen }
type Edge struct { Consumer, Upstream ContainerID }
type Topology struct { Instances []Instance; Edges []Edge }

type ActionKind string // "stop_accepting" | "execute_hook" | "request_exit" | "force_kill"
type Action struct { ID string; Kind ActionKind; Container ContainerID; Gen InstanceGen; Reason string }

// 事件
type DrainReport struct { Container ContainerID; Gen InstanceGen }
type HookReturn  struct { Container ContainerID; Gen InstanceGen }
type ExitEvent   struct { Container ContainerID; Gen InstanceGen }
type ActionAck   struct { ActionID string }

type Phase string // pending | draining | hook_running | exit_requested | exited | force_killed

type Clock interface { Now() time.Time }
type Store interface {
    Load() (Snapshot, bool, error)
    Save(Snapshot) error
}
type MemoryStore struct{ /* 并发安全的内存实现 */ }

func New(topo Topology, grace time.Duration, clock Clock, store Store) (*Coordinator, error)
    // store 中有快照则恢复（快照的拓扑与 grace 优先）；否则校验并采用参数。
    // 校验项：空拓扑、空/重复 ID、未知端点、自环、重复边、依赖环、非正 grace。

func (c *Coordinator) Start(now time.Time) []Action
    // 首次调用：设定共享截止时间，对全部实例发 stop_accepting，再推进钩子。重复调用返回 nil。

func (c *Coordinator) Handle(ev Event) []Action
    // 处理一个事件并返回其引发的动作；若时钟已过截止时间，同时执行 finalize。

func (c *Coordinator) Tick(now time.Time) []Action
    // 由定时器驱动截止时间；到点则 finalize，未到点或已 finalize 返回 nil。

func (c *Coordinator) Resume() []Action
    // 重启后取回未确认且仍相关的动作（不会重放已确认动作）。

func (c *Coordinator) Phase(id ContainerID) (Phase, bool)
func (c *Coordinator) State(id ContainerID) (InstanceState, bool)
func (c *Coordinator) Deadline() time.Time
func (c *Coordinator) Finalized() bool
func (c *Coordinator) IncompleteEdges() []EdgeFailure
```

`Action.ID` 形如 `execute_hook/<container>/<gen>`，对同一 (动作类型, 容器, 代次) 稳定，
执行器可据此做幂等投递。

## 复现步骤

空工作区（仅本仓库文件）中执行：

```sh
go version          # 期望 go1.20.2
gofmt -l .          # 期望无输出
go vet ./...        # 期望无输出
go test ./...                       # 全部通过
go test -race -count=1 ./...        # 全部通过，无数据竞争
go test -count=1 -cover ./...       # 覆盖率约 93.7%
```

## 验证范围（全部为本机真实测试，无预置输出）

`coordinator_test.go` 中的自动化测试：

| 测试 | 覆盖的场景 |
| --- | --- |
| `TestValidateTopologyErrors` | 8 类非法拓扑 + 非正 grace 被拒绝 |
| `TestParallelBranchesShareUpstreamStuckHook` | **题面指定的交错场景**：A、B 两条分支共享上游 U，分支 B 正常结束，A 的钩子卡住 |
| `TestParallelBranchesShareUpstreamResolvesWhenStuckConsumerExits` | 卡住的分支最终退出后上游继续；其晚回钩子不产生动作 |
| `TestSpontaneousExitBeforeHookReturn` | 钩子返回前自发退出，晚回钩子不把实例拉回处理中 |
| `TestSpontaneousExitWhileStillDraining` | 排空阶段自发退出；之后的 drain/exit 事件与强制结束都正确 |
| `TestStaleGenerationIgnored` | 旧代次事件被忽略，当前代次事件生效 |
| `TestGraceDeadlineForceKillAndIncompleteReport` | 全组共享截止时间、强制结束、未完成边报告、`Tick` 幂等 |
| `TestRestartDoesNotRepeatAckedActions` | 重启后不重复已确认动作，只重放未确认动作；重启后继续推进 |
| `TestRestartPreservesDeadline` | 截止时间跨重启保留 |
| `TestRestartWithinDeadlineWindow` | 截止前重启，到点后只强制结束未退出实例，已确认的 force_kill 不重现 |
| `TestConcurrentIndependentContainers` | 32 个独立容器并发、重复投递事件（配合 `-race`） |
| `TestStopAcceptingHappensForWholeGroupEvenWhenDeepChain` | 深链中全组先统一 stop_accepting，且关闭顺序逐级解锁 |
| `TestInterleavedChainAndBranch` | 链 + 并行分支、乱序输入，不提前关闭、不死锁 |
| `TestStoreErrorsSurfaced` / `TestCorruptSnapshotRejected` | 存储加载错误上抛；7 类损坏快照被拒绝 |
| `TestZeroValueBehavior` | Start 前查询、未知容器事件、空 ack、`Start` 幂等且不移动截止时间 |

**变异验证（手工执行，已还原）**：把「消费者必须已退出」条件放宽为只看 `Drained`，
有 5 个测试失败；把晚回钩子的门禁从「必须处于 hook_running」放宽为「只拒绝 force_killed」，
有 2 个测试失败。说明测试确实约束了核心语义，而非仅覆盖正常路径。

### 题面场景：两条分支共享上游，其中一条在钩子中卡住

`TestParallelBranchesShareUpstreamStuckHook` 的实际断言：

1. `Start` 产生 3 条 `stop_accepting`，全组立即停止接收输入。
2. A、B 分别 `DrainReport` 后各得到 `execute_hook`；此时 U 仍为 `draining`，因为没有任何消费者退出。
3. B 的 `HookReturn` → `request_exit:B`；B 的 `ExitEvent` **不产生任何动作**。
   B 在截止时间到来之前就已 `exited`——正常分支结束于它自己排空并退出之时，
   与卡住的 A 以及上游 U 无关。
4. A 停在 `hook_running`（钩子未返回），因此 U 始终停在 `draining`：
   U 的关闭前提是「A 和 B 都报告已读完且都已退出」，而 A 只完成了前半条。
5. 截止时间到达：对 A、U 发 `force_kill`，**不对已退出的 B 发**；
   `IncompleteEdges()` 恰好返回一条 `A→U`，标记 `ConsumerDrained=true, ConsumerExited=false`。

### 未覆盖 / 明确未执行的部分

- 未执行任何真实容器运行时、进程信号或外部编排系统集成；动作流是纯数据输出，
  如何执行由调用方负责。本测试只验证协调器的决策，不验证「真的 kill 成功」。
- `Store` 仅提供内存实现；文件/数据库后端未实现，也未测试崩机持久化（只测进程内重启）。
- 未做性能/负载测试；并发正确性仅依赖 `-race` 与重复投递测试。
