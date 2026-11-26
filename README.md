# barrier — 内存检查点屏障管理组件

`barrier` 是一个仅依赖 Go 标准库的内存屏障管理器。它用于协调多个异步作业等待的检查点：不同负责方可以提交 prepare 回执，管理器按运行（run）、分组门槛、镜像/代理关系、授权状态和交班后的有效参与者集合计算屏障是否可释放，并返回可释放的组与仍缺少的证明请求。

- Go：`go 1.20`（开发与验证使用 `go1.20.2`）
- 模块名：`barrier`
- 无第三方依赖、无网络访问、无持久化

## 核心语义

1. **Run 隔离**：每个 `RunConfig` 维护独立的参与者、分组、关系、屏障、回执和发布记录。
2. **分组门槛**：每个 `GroupConfig.Threshold` 表示该组需要的**不同逻辑责任身份数**；`BarrierConfig.Groups` 可以为单个屏障提供分组门槛覆盖。
3. **多组共同满足**：一个屏障的所有 required groups 都可释放时，屏障才 `Ready`。
4. **代理去重**：
   - 直接回执的责任身份是 `SubmitterID`；
   - 代理回执必须设置 `SubjectID` 为被代理主体，并匹配有效 `Grant`；
   - 同一屏障、同一分组内，代理回执与主体自己的回执只计算一次。
5. **镜像去重**：有效的 `DeclareMirror` 边构成等价类，直接回执按镜像等价类的最小 ID 作为 `LogicalID` 去重。撤销镜像关系后，待发布屏障会按新关系复算。
6. **授权撤销**：
   - `RevokeParticipant`：参与者退出有效集合，其未发布证明不再计数；
   - `RevokeProxy`：代理授权失效，对应代理回执不再计数；
   - `RevokeMirror`：镜像等价关系失效，回执可重新按独立身份计算。
7. **交班/Handoff**：`HandOver` 会把交出者从当前分组成员集合中替换为继任者，并将交出者置为 `handed_over`。交班不会自动转移代理授权，避免权限隐式扩散。
8. **发布边界与来源保留**：
   - `Evaluate` 是只读复算，不发布成功；
   - `PublishGroup` 原子发布组门槛证明；`TryReleaseBarrier` / `ReleaseBarrier` / `WaitForRelease` 原子发布屏障；
   - 已发布的 `GroupReleaseRecord` / `ReleaseRecord` 不可变，保留全部有效来源回执、提交者、主体、授权 ID、时间和拓扑版本；
   - 尚未发布的组或屏障在撤销/交班后按当前有效集合复算，可能从“当前满足”变回“缺少证明”；
   - 已单独发布的组证书会作为不可变来源继续支撑屏障发布。
9. **可操作的缺失证明**：`Evaluation.ReleasableGroups` 给出当前可释放组；`Evaluation.Missing` 和 `GroupStatus.Missing` 给出缺少数量、是否不可能达到门槛、等待的逻辑身份，以及每个身份当前可接受的直接提交者和授权代理提交者。

## 公开 API

### 构造与配置

```go
func NewManager(opts ...Option) *Manager
func NewBarrierManager(opts ...Option) *Manager // 同义构造器
func NewCoordinator(opts ...Option) *Coordinator // Coordinator 是 Manager 的类型别名
func WithClock(now func() time.Time) Option

func (m *Manager) CreateRun(cfg RunConfig) (*RunInfo, error)
func (m *Manager) GetRun(id RunID) (*RunInfo, error)
```

主要配置类型：`RunConfig`、`ParticipantConfig`、`GroupConfig`。

### 关系、授权、撤销与交班

```go
func (m *Manager) DeclareMirror(runID RunID, a, b ParticipantID) (*MirrorRelation, error)
func (m *Manager) RevokeMirror(runID RunID, relationID RelationID, reason string) (*MirrorRelation, error)

func (m *Manager) AuthorizeProxy(auth ProxyAuthorization) (*Grant, error)
func (m *Manager) RevokeProxy(runID RunID, grantID GrantID, reason string) (*Grant, error)

func (m *Manager) RevokeParticipant(runID RunID, pid ParticipantID, reason string) (*ParticipantInfo, error)

func (m *Manager) HandOver(req HandOverRequest) (*HandOverRecord, error)
func (m *Manager) Handoff(req HandoffRequest) (*HandoffRecord, error) // 常用拼写别名
func (m *Manager) HandOff(req HandOffRequest) (*HandOffRecord, error)
```

### 屏障、回执、评估与发布

```go
func (m *Manager) DeclareBarrier(runID RunID, cfg BarrierConfig) (*Evaluation, error)

func (m *Manager) SubmitReceipt(receipt Receipt) (*SubmitReceiptResult, error)
func (m *Manager) SubmitPrepare(receipt Receipt) (*SubmitReceiptResult, error) // 领域别名

func (m *Manager) Evaluate(runID RunID, barrierID BarrierID) (*Evaluation, error)

func (m *Manager) PublishGroup(runID RunID, barrierID BarrierID, groupID GroupID) (*PublishGroupOutcome, error)
func (m *Manager) TryReleaseBarrier(runID RunID, barrierID BarrierID) (*ReleaseOutcome, error)
func (m *Manager) ReleaseBarrier(runID RunID, barrierID BarrierID) (*ReleaseRecord, error)
func (m *Manager) WaitForRelease(ctx context.Context, runID RunID, barrierID BarrierID) (*ReleaseRecord, error)

func (m *Manager) GetRelease(runID RunID, barrierID BarrierID) (*ReleaseRecord, error)
func (m *Manager) PollRun(runID RunID) (*PollResult, error)
```

错误判断使用 `errors.Is`，稳定 sentinel 包括：

- `ErrRunNotFound`、`ErrBarrierNotFound`、`ErrParticipantNotFound`、`ErrGroupNotFound`
- `ErrInvalidConfig`、`ErrInvalidRequest`、`ErrConflict`
- `ErrUnauthorized`、`ErrRevoked`、`ErrParticipantInactive`
- `ErrCheckpointMismatch`、`ErrGroupNotRequired`、`ErrNotMember`
- `ErrBarrierReleased`、`ErrBarrierNotReady`

显式 `ReleaseBarrier` 在门槛不满足时返回 `*NotReadyError`，其中携带当前 `*Evaluation`。

## 快速示例

下面的可运行示例也保存在 `example_test.go` 中，会由 `go test` 校验输出。

```go
package main

import (
	"fmt"

	"barrier"
)

func main() {
	m := barrier.NewManager()
	_, err := m.CreateRun(barrier.RunConfig{
		ID: "run-42",
		Participants: []barrier.ParticipantConfig{
			{ID: "a"}, {ID: "b"}, {ID: "d"},
		},
		Groups: []barrier.GroupConfig{
			{ID: "signers", Threshold: 2, Members: []barrier.ParticipantID{"a", "b"}},
			{ID: "witnesses", Threshold: 1, Members: []barrier.ParticipantID{"d"}},
		},
	})
	if err != nil {
		panic(err)
	}

	initial, err := m.DeclareBarrier("run-42", barrier.BarrierConfig{
		ID:           "cp-7",
		CheckpointID: "checkpoint-7",
	})
	if err != nil {
		panic(err)
	}
	fmt.Printf("initial ready=%v missingGroups=%d\n", initial.Ready, len(initial.Missing))

	for _, r := range []barrier.Receipt{
		{RunID: "run-42", BarrierID: "cp-7", CheckpointID: "checkpoint-7", GroupID: "signers", SubmitterID: "a", Proof: "synthetic-a"},
		{RunID: "run-42", BarrierID: "cp-7", CheckpointID: "checkpoint-7", GroupID: "signers", SubmitterID: "b", Proof: "synthetic-b"},
		{RunID: "run-42", BarrierID: "cp-7", CheckpointID: "checkpoint-7", GroupID: "witnesses", SubmitterID: "d", Proof: "synthetic-d"},
	} {
		if _, err := m.SubmitReceipt(r); err != nil {
			panic(err)
		}
	}

	out, err := m.TryReleaseBarrier("run-42", "cp-7")
	if err != nil {
		panic(err)
	}
	fmt.Printf("release ready=%v sources=%d\n", out.Released, len(out.Release.SourceReceipts))

	// Output:
	// initial ready=false missingGroups=2
	// release ready=true sources=3
}
```

代理回执示例：

```go
_, _ = m.AuthorizeProxy(barrier.ProxyAuthorization{
	RunID:       "run-1",
	ProxyID:     "proxy-1",
	PrincipalID: "p1",
	Groups:      []barrier.GroupID{"g"},
})

_, _ = m.SubmitReceipt(barrier.Receipt{
	RunID:        "run-1",
	BarrierID:    "cp-1",
	CheckpointID: "checkpoint-1",
	GroupID:      "g",
	SubmitterID:  "proxy-1",
	SubjectID:    "p1", // 关键：按主体 p1 去重，而不是按 proxy-1 多算一票
	Proof:        "synthetic-proxy-proof",
})
```

## 运行与验证

在当前工作区执行：

```sh
go test ./...
go test -race ./...
go test -cover ./...
go test -v ./...
```

本次实现中实际执行并通过：

- `go test ./...`：通过；
- `go test -race ./...`：通过，未报告数据竞争；
- `go test -cover ./...`：语句覆盖率约 `80.0%`。

测试只使用本地合成参与者、分组、回执和 proof 字符串，不访问网络或真实凭据。

## 已覆盖的正常、边界与交错场景

- **正常多组流程**：两个分组分别收齐门槛；验证初始缺失、部分回执后的 missing count、可释放组、最终发布记录和 `PollRun`。
- **代理/主体去重**：代理回执与主体回执只形成一个 `Contribution`，但两条来源回执都保留；第三个独立主体才能满足门槛。
- **镜像去重**：两个镜像的直接回执只计一个逻辑身份；撤销镜像关系后按两个独立身份复算。
- **不可能门槛**：两人分组门槛为 2，但两人是有效镜像时，评估返回 `Impossible=true` 和明确原因。
- **交班复算**：`a,b` 已满足但未发布时，`a -> d` 交班后旧票失效，评估只剩 `b`，并给出缺失证明；`d` 补票后才发布，最终来源为 `b,d`。
- **已发布组保留**：组证书发布后再交班，当前票数可能不足，但 `PublishedRelease` 与 `a,b` 来源不变，并继续支撑屏障发布。
- **已发布屏障不可变**：发布后撤销参与者并交班，`GetRelease` 与 release ID、来源记录不变，重复发布返回 `AlreadyReleased`。
- **授权撤销**：撤销 proxy grant 后，旧代理回执进入 `InvalidReceipts`（`grant_revoked`）且不再计数；主体直接补票可恢复。
- **参与者撤销**：撤销后待发布屏障返回 `NotReadyError`，错误中携带最新 `Evaluation`；其他有效成员补票后可发布。
- **幂等与冲突**：相同 `ReceiptID` 和载荷返回 `Duplicate=true` 与原顺序；同一 ID 改用不同 proof 返回 `ErrConflict`。
- **输入/授权边界**：checkpoint 不匹配、非成员直接提交、无授权代理、未知 run、非法阈值、重复参与者、阈值超过成员数等。
- **ID 幂等边界**：自定义 `ReceiptID` 与生成 ID 命名空间碰撞时会跳到未占用 ID，后续幂等重试仍定位到原始回执。
- **并发等待/发布交错**：16 个 waiter 与 45 个重复/并发提交者竞争，所有 waiter 得到同一个 `ReleaseID`；通过 race detector。
- **发布与交班线性化交错**：60 组 `TryReleaseBarrier` 与 `HandOver` 并发执行。若发布先获得管理器锁，发布来源固定为 `a,b`；若交班先执行，发布必须等待 `c` 补票，来源为 `b,c`。
- **Context 取消**：已取消的 Wait 返回 `context.Canceled`；取消后的新 Wait 仍能被后续回执正常唤醒。
- **时钟注入**：`WithClock` 可固定评估和发布时间，便于确定性合成测试。
- **运行隔离与门槛覆盖**：相同 ID 的不同 run 状态独立；单屏障阈值覆盖只影响指定屏障。
- **可运行示例**：`ExampleManager` 由 `go test` 校验。

## 适用范围与限制

这是题面要求的**内存、声明式授权判断组件**：它验证管理器内的运行配置、成员关系、代理授权、checkpoint 绑定和有效状态，但不验证外部签名、证书链或真实身份系统。`Receipt.Proof` 是不透明的合成证明引用/材料字段；重启后内存状态丢失。这些限制符合本题的本地实现与测试范围。
