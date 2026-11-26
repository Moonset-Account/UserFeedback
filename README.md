# relaygate — 休眠中继首批记录归属判定

`relaygate` 是一个只处理**已校验证据**的 Go 接收判定模块。它根据：

- 连接世代（`ConnectionGeneration`）
- 对端启动号（`PeerStartNumber`）
- 保活证明序号（`KeepaliveProof.Sequence`）
- 每条证明中明确列出的方向与租用窗口
- 延迟到达的唤醒确认及其精确范围

判断每条记录当前是否可以交给下游，并给出归属会话、使用的证明序号/窗口，以及仍缺少或不匹配的证据。

模块不做签名、证书、身份或可信时间源校验；这些必须由调用方在进入模块前完成。模块也不访问网络、真实目标或凭据，只使用本地合成证据。

## 运行环境

- Go：题目环境中的 `go version go1.20.2 darwin/arm64`
- `go.mod` 声明 `go 1.20`
- 仅使用 Go 标准库，无第三方依赖

## 快速示例

```go
package main

import (
    "fmt"
    "time"

    "relaygate"
)

func main() {
    base := time.Date(2026, 11, 27, 0, 0, 0, 0, time.UTC)
    session := relaygate.SessionKey{
        ConnectionGeneration: 7,
        PeerStartNumber:      3,
    }

    proof := relaygate.KeepaliveProof{
        Session:  session,
        Sequence: 4,
        Grants: []relaygate.DirectionGrant{{
            Direction: relaygate.Uplink,
            Window: relaygate.LeaseWindow{
                NotBefore: base,
                ExpiresAt: base.Add(10 * time.Minute), // 半开区间
            },
        }},
    }

    gate := relaygate.New()
    _, _ = gate.SubmitKeepalive(proof)

    record := relaygate.Record{
        ID:        "first-uplink-record",
        Session:   session,
        Direction: relaygate.Uplink,
        TrustedAt: base.Add(2 * time.Minute), // 必须来自可信入口时钟
    }

    before := gate.Admit(record)[0]
    fmt.Println(before.Status) // pending；MissingEvidence 含 missing_wake_confirmation

    ack := relaygate.ConfirmDirections(proof, relaygate.Uplink)
    _, _ = gate.SubmitWakeConfirmation(ack)

    after, _ := gate.Verdict(record.ID)
    fmt.Println(after.Status, after.Deliverable) // admitted true
}
```

可运行的示例断言见 `example_test.go`。

## 判定规则

### 1. 会话坐标必须精确匹配

记录中的 `SessionKey{ConnectionGeneration, PeerStartNumber}` 必须与保活证明和唤醒确认完全一致。世代相同但启动号不同，或启动号相同但世代不同，都不会归属到已有证明；判定中会补充对应的 mismatch issue 和 `missing_keepalive_proof`。

### 2. 租用窗口是半开区间

`LeaseWindow.Contains(t)` 使用：

```text
[NotBefore, ExpiresAt)
```

因此：

- `t == NotBefore`：窗口开始，允许；
- `NotBefore < t < ExpiresAt`：允许；
- `t == ExpiresAt`：已过期，拒绝；
- `t < NotBefore`：租约尚未开始，拒绝。

记录时间使用 `Record.TrustedAt`。该字段应由可信入口时钟或已校验的可信时间证明产生，不能直接相信记录负载中的自报时间。

### 3. 唤醒确认可以延迟或早于证明到达

`Adjudicator` 保存记录和证据。先调用 `Admit` 时，记录会得到 `pending` 结论和缺少的证据；之后调用 `SubmitWakeConfirmation` 或 `SubmitKeepalive` 会自动重新评估已知记录，并通过 `SubmitResult.Changed` 返回发生变化的结论。

确认也可以先于证明提交；当精确匹配的证明到达后，记录可自动转为 `admitted`。

### 4. 确认范围限定为证明中明确列出的“方向 + 窗口”

有效授权必须同时满足：

1. 引用相同的 `SessionKey`；
2. 引用相同的保活证明序号；
3. 方向相同；
4. 窗口的 `NotBefore` 和 `ExpiresAt` 与证明中的授权**精确相等**。

`WakeConfirmation.Directions` 可以只选择证明中列出的方向，窗口直接取证明中的精确窗口；`WakeConfirmation.Grants` 也可以显式给出必须与证明精确相等的“方向 + 窗口”对。`ConfirmDirections(proof, directions...)` 会安全复制证明中这些方向对应的原始窗口。若确认中自行给出更宽或不同的窗口，该范围不会生效，结论为：

```text
pending / confirmation_does_not_cover_exact_direction_and_window
```

`Directions` 和 `Grants` 都为空表示不确认任何方向，绝不是“确认全部方向”。

### 5. 重复保活不会延长有效期

相同 `Session + Sequence` 且授权集合相同的保活证明按幂等重复处理，返回 `SubmitDuplicate`，不会修改已存窗口。

相同 `Session + Sequence` 但授权或窗口不同的证明被视为不可变证据冲突：

- `SubmitKeepalive` 返回可由 `errors.Is(err, relaygate.ErrConflict)` 识别的 `*ConflictError`；
- 返回状态为 `SubmitConflict`；
- 原证明和原窗口保持不变；
- 不会用冲突证明延长 `ExpiresAt`。

更高序号的新证明可以携带新的显式窗口；这不是“重复保活”。

### 6. 方向独立，单边接管不中断另一条有效方向

授权和确认均按 `(Session, Sequence, Direction, Window)` 匹配。新序号或新确认只影响其中明确覆盖的方向，不会全局撤销另一个方向。

例如，上行方向由序号 2 的新证明/确认接管时，只要下行记录的时间仍落在序号 1 的旧窗口内，且旧确认明确覆盖下行，下行记录仍归属序号 1 并可交付。

当存在多个可用证明时，默认优先选择更高序号；调用方也可以设置 `Record.KeepaliveSequence` 将记录精确绑定到某个证明序号。

## 结论结构

每条输入记录都有一个同序的 `Verdict`：

```go
type Verdict struct {
    RecordID        string
    Status          Status           // admitted | pending | denied | conflict
    Deliverable     bool             // 仅 admitted 时为 true
    Attribution     *Attribution     // 归属会话、方向、序号、窗口和依据
    MissingEvidence []MissingEvidence
    Issues          []Issue
}
```

主要缺少证据代码：

| Code | 含义 |
| --- | --- |
| `missing_trusted_time` | 记录没有可信入口时间 |
| `missing_keepalive_proof` | 找不到匹配会话或指定序号的保活证明 |
| `missing_direction_grant` | 证明没有明确列出该方向 |
| `missing_wake_confirmation` | 租约时间有效，但还没有对应序号的唤醒确认 |
| `confirmation_does_not_cover_exact_direction_and_window` | 已有确认，但未精确覆盖所需方向和窗口 |
| `no_lease_window_covers_trusted_time` | 有方向窗口，但没有窗口覆盖记录的可信时间 |

`Issues` 还会给出 `lease_expired`、`lease_not_started`、`connection_generation_mismatch`、`peer_start_number_mismatch`、无效方向/时间和记录 ID 冲突等具体原因。记录 `ID` 是可选关联键；为空时仍会在当次 `Admit` 结果中给出结论，但不会被状态器留存和后续自动刷新。

## 公开 API

核心类型：

- `SessionKey`
- `LeaseWindow`
- `Direction`：`Uplink`、`Downlink`
- `DirectionGrant`
- `KeepaliveProof`
- `WakeConfirmation`
- `Record`
- `Verdict`、`Attribution`
- `MissingEvidence`、`Issue`
- 状态/原因常量：`Status*`、`Need*`、`Issue*`、`Basis*`

核心函数和方法：

```go
func New(opts ...Option) *Adjudicator
func WithWakeConfirmationRequired(required bool) Option

func (a *Adjudicator) SubmitKeepalive(proof KeepaliveProof) (SubmitResult, error)
func (a *Adjudicator) SubmitWakeConfirmation(confirmation WakeConfirmation) (SubmitResult, error)

func (a *Adjudicator) Admit(records ...Record) []Verdict
func (a *Adjudicator) Refresh() []Verdict

func (a *Adjudicator) Verdict(recordID string) (Verdict, bool)
func (a *Adjudicator) Verdicts() []Verdict
func (a *Adjudicator) Pending() []Verdict
func (a *Adjudicator) Admitted() []Verdict

func ConfirmDirections(proof KeepaliveProof, directions ...Direction) WakeConfirmation
func SequencePtr(sequence uint64) *uint64
```

错误：

```go
var ErrConflict = errors.New("relaygate: conflicting evidence")

type ConflictError struct {
    Evidence string
    Session  SessionKey
    Sequence uint64
}
```

`Adjudicator` 使用互斥锁保护内存状态，可并发调用；状态不持久化，重启后需要重新提交已校验证据。

## 运行方法

在当前工作区执行：

```bash
go test ./...
```

建议同时运行竞态检测：

```bash
go test -race ./...
```

格式化：

```bash
gofmt -w *.go
```

## 已执行的本地验证

以下命令均已在本题工作区实际执行：

```text
go test ./...
ok  	relaygate

go test -race ./...
ok  	relaygate
```

`go test ./... -v` 中的 16 个测试函数和 1 个 `Example` 全部通过；边界表的 6 个子用例全部通过。

验证范围与题面要求的对应关系：

| 题面要求 | 实测位置 |
| --- | --- |
| 正常双方向记录可归属并交付 | `TestHappyPathAdmitsEachExplicitDirection` |
| 窗口起点、到期点、到期前一纳秒、窗口前后边界 | `TestLeaseWindowBoundariesAreHalfOpen` |
| 唤醒确认延迟到达，到达后刷新原记录 | `TestWakeConfirmationCanBeDelayedAndIsScoped` |
| 确认只覆盖证明列出的方向/精确窗口，不能扩大窗口或创造方向 | `TestConfirmationCannotExpandProofScope` |
| 重复保活幂等且不延长窗口；同序号冲突被拒绝并保留原窗口 | `TestDuplicateKeepaliveDoesNotExtendWindow` |
| 单边接管不中断另一条仍有效方向；新范围未确认也不能隐式接管 | `TestOneDirectionTakeoverDoesNotInterruptOtherValidDirection` |
| 连接世代和对端启动号共同决定会话 | `TestConnectionGenerationAndPeerStartMustMatch` |
| 保活序号精确绑定、缺失序号报告和隐式选择 | `TestKeepaliveSequenceBindingAndSelection` |
| 记录、证明、部分确认、剩余确认的交错到达 | `TestEvidenceAndConfirmationCanInterleaveWithRecords` |
| 确认先于证明到达的交错顺序 | `TestConfirmationCanArriveBeforeProof` |
| 每条输入记录都有结论；空 ID 仍判定但不留存，无效方向/时间被拒绝并给出缺失证据 | `TestAdmitReturnsVerdictForEveryInputAndRejectsInvalidMetadata` |
| 仅用方向选择器确认时，窗口仍严格取自证明 | `TestConfirmationDirectionSelectorUsesExactProofWindow` |
| 记录 ID 复用且元数据不同产生冲突，不覆盖原结论 | `TestReusingRecordIDWithDifferentMetadataIsConflict` |
| 可关闭唤醒确认策略，仅用于明确不需要确认的部署 | `TestWithoutWakeConfirmationPolicyAdmitsOnProofAlone` |
| 非法证据在入库前报错 | `TestInvalidEvidenceIsRejectedBeforeStorage` |
| 并发提交和判定在 `-race` 下通过 | `TestAdjudicatorSupportsConcurrentUniqueSessions` |
| README 核心延迟确认示例可执行 | `ExampleAdjudicator_delayedConfirmation` |

所有测试数据均由固定时间、合成 `SessionKey`、合成证明序号和本地结构构造；没有联网、读取凭据、访问真实目标或安装依赖。

## 非目标与限制

- 不验证密码学签名、证书、MAC 或时间证明本身。
- 不提供持久化、复制或时钟同步。
- 不生成、执行或测试任何利用、入侵、攻击或规避载荷。
- 窗口采用绝对 `time.Time` 和半开区间；若部署需要闭区间端点，应在调用前转换为对应 `ExpiresAt`。
- 当前实现只内置 `uplink` / `downlink` 两个方向；`Direction` 是字符串类型，扩展方向时需要同步调整校验策略。
