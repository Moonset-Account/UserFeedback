# 智能卡 PIN 校验状态机（双页持久化）

使用 Go 1.20.2 实现，仅依赖标准库。状态机通过 `PageReadWriter` 接口挂接任意持久页存储，并用两个交替写入、带世代号和 SHA-256 校验的页面实现崩溃恢复。

## 公开 API

核心接口和类型：

```go
type PageReadWriter interface {
    ReadPage(pageID int) ([]byte, error)
    WritePage(pageID int, data []byte) error
}

func NewMemoryPageStore() *MemoryPageStore

type Request struct {
    SessionID string // 卡片会话号
    Attempt   uint64 // 该会话内单调增大的尝试号
    PIN       string
}

type Decision struct {
    Status             Status // OK / BAD_PIN / LOCKED / CONFLICT / REJECTED
    SessionID          string
    Attempt            uint64
    Failures           int
    MaxFailures        int
    AttemptsRemaining  int
    Locked             bool
    Replayed           bool
}

type CardStatus struct { /* CardID、Failures、Locked、Generation、ActivePage 等 */ }

type StateMachine struct{ /* ... */ }

func New(store PageReadWriter, verifier PINVerifier, opts ...Option) (*StateMachine, error)
func Open(store PageReadWriter, verifier PINVerifier, opts ...Option) (*StateMachine, error)
func NewStateMachine(store PageReadWriter, verifier PINVerifier, opts ...Option) (*StateMachine, error)

func (m *StateMachine) Verify(req Request) (Decision, error)
func (m *StateMachine) Status() (CardStatus, error)

func StaticPINVerifier(expectedPIN string) PINVerifier
func WithCardID(cardID string) Option
func WithMaxFailures(n int) Option // 默认 3
```

主要错误哨兵可用 `errors.Is` 判断：

- `ErrAttemptConflict`：同一会话和尝试号被换成不同内容；
- `ErrStaleAttempt`：使用了没有决定记录、但不高于会话高水位的旧尝试号；
- `ErrInvalidRequest`：空会话号、尝试号为 0 或非法配置；
- `ErrConfigMismatch`：重开时的 `CardID` 或 `MaxFailures` 与持久页不一致；
- `ErrNoRecoverableState`、`ErrAmbiguousPages`：双页均不可安全恢复。

普通 PIN 结果（错误 PIN、锁卡）通过 `Decision` 返回且错误为 `nil`；内容复用、参数错误和持久化故障通过 Go `error` 返回。

## 最小示例

```go
package main

import (
    "fmt"
    "pinstate"
)

func main() {
    store := pinstate.NewMemoryPageStore()
    verifier := pinstate.StaticPINVerifier("123456")

    machine, _ := pinstate.New(store, verifier)

    d, _ := machine.Verify(pinstate.Request{
        SessionID: "reader-session-1",
        Attempt:   1,
        PIN:       "000000",
    })
    fmt.Println(d.Status, d.Failures, d.AttemptsRemaining)
    // BAD_PIN 1 2

    // 断电并丢弃内存对象后，用同一持久页重新打开。
    reopened, _ := pinstate.Open(store, verifier)
    d, _ = reopened.Verify(pinstate.Request{
        SessionID: "reader-session-1",
        Attempt:   1,
        PIN:       "000000",
    })
    fmt.Println(d.Replayed, d.Status)
    // true BAD_PIN
}
```

## 关键状态与崩溃一致性

持久快照同时包含：

1. 卡片级 `Failures` 和 `Locked`；
2. 每个会话的高水位；
3. 每个已处理尝试号的请求内容指纹；
4. 该尝试号对应的、可重放的 `Decision`。

这些字段放在同一个 JSON 快照中，再封入带 `Magic`、格式版本、物理页号、单调 `Generation` 和 SHA-256 校验的页信封。两个页面（页 0、页 1）交替写入：

```text
新快照 generation=N+1 写入当前非活动页
写入完整成功后，该页才成为活动页
重启时读取两页，忽略空白/校验失败页，选择 generation 最大的有效页
```

因此一个完整的单页写入就是状态提交边界。错误次数和它所影响的对外决定在同一次页写入中持久化；`Verify` 只有在页写入成功后才返回该决定。

请求处理顺序为：

1. 校验 `SessionID`、`Attempt`；
2. 若 `(SessionID, Attempt)` 已有记录：
   - 内容指纹相同：返回保存的原决定，并标记 `Replayed=true`；
   - 内容指纹不同：返回 `StatusConflict` 和 `ErrAttemptConflict`，不覆盖原记录；
3. 若尝试号未知但不高于会话高水位，返回 `ErrStaleAttempt`；
4. 计算 PIN 转换：
   - 未锁卡且 PIN 正确：`Failures=0`；
   - 未锁卡且 PIN 错误：增加错误次数，达到 3 时锁卡；
   - 已锁卡：直接返回 `LOCKED`，不调用验证器、不清零；
5. 将计数、锁状态、尝试指纹和决定作为同一个双页快照提交；
6. 提交成功后才对外返回决定。

如果页写入返回错误但实际可能已落盘，提交逻辑会立即重新读取双页并同步内存状态，避免同进程重试时基于旧状态重复计数。真实断电测试则直接丢弃 `StateMachine`，只用同一个 `PageReadWriter` 调 `Open` 重启。

## 运行方法

工作区没有第三方依赖，也不需要联网：

```bash
go test ./...
go test -v ./...
go test -race ./...

# 只运行穷举断电边界测试
go test -run 'TestAllSinglePageWriteBoundaries' -v
```

开发和实测使用：

```text
go version go1.20.2 darwin/arm64
```

## 已用本地合成数据验证的范围

`pinstate_test.go` 和 `crash_test.go` 全部使用进程内合成页存储及固定 PIN 验证器，不访问真实智能卡、凭据、网络或外部服务。

已覆盖：

- 正确 PIN 在未锁卡时清零，之后错误次数重新累计；
- 连续三次错误 PIN 后 `Failures=3`、剩余次数为 0、`Locked=true`；
- 锁卡后紧随的正确 PIN 仍返回 `LOCKED`，计数保持 3；
- 同请求在进程内重发和断电重启后重发均取回原决定；
- 同一会话和尝试号更换 PIN/内容会得到 `ErrAttemptConflict`，且原请求之后仍可重放原决定；
- 单调尝试号、旧的未知尝试号、`Attempt=0`、空会话号；
- 多个会话交错请求时，会话尝试记录相互独立，但卡片错误次数和锁状态共享；
- 切换到全新会话以及再次断电重启后，仍保持 `Failures=3` 和锁卡；
- `CardID` / `MaxFailures` 配置与持久状态不一致时拒绝打开。

核心断电矩阵 `TestAllSinglePageWriteBoundariesLastWrongThenCorrect` 对以下边界逐一重启：

| 边界 | 持久页状态 |
| --- | --- |
| `b=0` | 第一次页写入前断电，无持久决定 |
| `b=1` | 第 1 次错误快照写入后断电，`Failures=1` |
| `b=2` | 第 2 次错误快照写入后断电，`Failures=2` |
| `b=3` | 最后一次错误快照写入后断电，`Failures=3`、已锁卡 |
| `b=4` | 随后的正确尝试决定快照写入后断电，仍为 `Failures=3`、已锁卡 |

每个边界都执行：

1. 注入断电错误；
2. 丢弃内存状态并从双页重新 `Open`；
3. 断言恢复出的错误次数/锁状态与已完成写入数一致；
4. 用相同 `SessionID`、`Attempt`、PIN 重发原请求；
5. 断言已提交的请求返回 `Replayed=true` 和上次决定；
6. 完成“三次错误 + 紧随正确 PIN”的完整序列；
7. 断言最终世代号为 4、物理活动页按 `0,1,0,1` 交替；
8. 切换新会话并连续发送 20 个额外猜测，全部仍是 `LOCKED` 且 `Failures=3`，证明额度没有被多用。

`TestChangedContentAtEveryDurableAttemptBoundaryConflicts` 还在第 1～4 次完整页写入后的每个边界验证：把刚使用的尝试号换成不同内容必然冲突，而原内容仍可重放。

`TestDualPageRecoversValidPageAndFailsClosedWhenBothCorrupt` 翻转页内容制造校验失败：一页损坏时从另一新世代有效页恢复；两页都损坏时返回 `ErrNoRecoverableState`，不会静默清零或解锁。

实际执行结果：

```text
go test ./...        # ok pinstate
go test -race ./...  # ok pinstate
```

## 适用边界和限制

- 测试覆盖的是题面要求的“单页写入边界”：完整页写入前或后的断电。页信封带 SHA-256，可发现页内撕裂/损坏；若两页都没有有效校验，状态机安全失败并拒绝继续。
- `MemoryPageStore` 是合成存储；生产适配器应把页 ID 0/1 映射到两块持久页，并满足完整页写入及读取一致性契约。
- `StateMachine` 用互斥锁保证单进程内安全；如果多个进程或读卡器组件可同时写同一组页，需要在 `PageReadWriter` 适配器或系统层提供跨进程互斥。
- 状态中不保存原始 PIN，只保存请求内容的 SHA-256 指纹用于判断“内容是否改变”。若持久页可能被攻击者完整读取，低熵 PIN 的指纹可能遭到字典猜测；应把持久页视为安全敏感数据，或由安全元件/设备密钥进一步保护。这不属于当前题面要求的真实攻击利用。
