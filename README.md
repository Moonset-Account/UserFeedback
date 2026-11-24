# regaccess — 事务化寄存器访问模块

Go 1.20.2 实现，零第三方依赖。模块用合成设备事件、指令尝试（`Attempt`）、
提交凭据（`Receipt`）和只读重放器（`Replayer`）模拟：

- 读完即清的事件位；
- 多个配置字段必须随同一条指令一起提交；
- 提交前私有的暂存视图与设备外部可见状态并存；
- 提交、撤回（`Abort` / `Rollback`）和提交幂等；
- 已提交指令按原始顺序重放当时读到的结果；
- 读取后、提交前同一位又产生新事件时，只确认本次读取观察到的旧事件代际。

## 核心语义

### 事件代际

仅有一个 bit 无法区分“读取时看到的旧事件”和“延迟提交期间设备重新置起的新事件”。
因此每条合成事件线维护两个单调代际：

- `latest`：设备最新事件代际，每次 `Device.Raise` 都加一，即使该位当前仍有效；
- `ackedTo`：已经提交确认到的代际前沿。

`Attempt.ReadEvents` 观察当前比本尝试 `acks[bit]` 更新的事件，并把“将确认到的代际”
记录在尝试内部。提交时只推进到当时观察到的代际。例如：

```text
Raise(DataReady)                 latest=1, ackedTo=0
ReadEvents() 观察到 generation=1  pending ack through 1
Raise(DataReady)                 latest=2, ackedTo=0
Commit()                         ackedTo=1, latest=2
PeekEvents()                     仍可见 DataReady，generation=2
```

因此旧事件被结束，提交前新置起的事件不会被误清。

### 暂存状态与外部状态

`Attempt` 维护私有的寄存器影子值、读记录、写记录和待确认事件代际：

- `ReadRegister` / `ReadField` 优先读本尝试的暂存值，实现 read-your-writes；
- `Device.ReadRegister` / `Device.PeekEvents` 始终读取外部已提交状态；
- `Commit` 在同一把设备锁内应用全部事件确认和寄存器写，外部只能看到提交前或提交后的完整状态；
- `Abort` 丢弃影子值、写记录、读记录和待确认事件确认，设备不受影响。

### 指令重放

`Session` 以指令 ID 保存已提交 `Receipt`。提交后即使设备寄存器被改写、事件产生新代际，
`Session.Replay(id)` 返回的 `Replayer` 仍按原顺序给出原始读取结果。重放不访问设备、
不确认事件、不重复写寄存器。

已提交 ID 不能再次 `Begin`；同一 `Attempt.Commit` 重复调用会返回同一凭据。
撤回后的 ID 没有提交凭据，因此可以作为新尝试重新开始。

## 公开 API 概览

完整文档可运行 `go doc ./...` 查看。

### 合成设备

- `NewDevice() *Device`
- `(*Device).Raise(events EventMask)` / `RaiseEvent(bit EventMask)`：注入合成事件并产生新代际
- `(*Device).PeekEvents() EventRead`：非破坏性读取外部当前事件
- `(*Device).EventProgress(bit) (ackedTo, latest uint64, ok bool)`：检查事件代际
- `(*Device).ReadRegister(addr uint16) uint32`
- `(*Device).SetRegister(addr uint16, value uint32)`：测试 setup / 外部设备改写入口
- `(*Device).RegisterSnapshot(addrs ...uint16) map[uint16]uint32`：同一把锁内读取多寄存器快照

### 指令会话与尝试

- `NewSession(device *Device) *Session`；`nil` 时自动构建设备
- `(*Session).Device() *Device`
- `(*Session).Begin(instructionID string) (*Attempt, error)`
- `(*Session).Receipt(id string) (*Receipt, error)`
- `(*Session).Replay(id string) (*Replayer, error)`

`Attempt` 方法：

- `ID() string`
- `ReadEvents() (EventRead, error)`
- `ReadRegister(addr uint16) (uint32, error)`
- `ReadField(field Field) (uint32, error)`
- `StageRegister(addr uint16, value uint32) error`
- `StageField(field Field, value uint32) error`
- `StageConfig(config Config) error`：原子地暂存 `ENABLE`、`MODE`、`SPEED`、`TAG` 字段组
- `StagedRegister(addr uint16) (uint32, bool)`
- `PendingEventAcks() []EventAck`
- `Commit() (*Receipt, error)`
- `Abort() error` / `Rollback() error`

### 重放与记录

- `Receipt.ID()`、`Sequence()`、`Reads()`、`Writes()`、`EventAcks()`、`EventReads()`
- `Receipt.NewReplayer() *Replayer`
- `(*Replayer).ReadEvents() (EventRead, error)`
- `(*Replayer).ReadRegister(addr uint16) (uint32, error)`
- `(*Replayer).ReadField(field Field) (uint32, error)`
- `Cursor()`、`Remaining()`、`Receipt()`

地址或读取类型与原记录不符时返回包装 `ErrReplayMismatch` 的错误，且不消费当前记录；
记录耗尽后返回 `ErrReplayExhausted`。

### 寄存器字段

内置寄存器：

- `RegControl = 0x04`
- `RegTag = 0x08`

内置字段：

- `FieldEnable`：`RegControl` bit 0
- `FieldMode`：`RegControl` bits 1..3
- `FieldSpeed`：`RegControl` bits 4..7
- `FieldTag`：`RegTag` bits 0..7

也可以构造自己的 `Field{Name, Register, LSB, Width}`，并用
`Field.Validate`、`Field.Encode`、`Field.Decode` 处理 32 位字段。

错误哨兵包括 `ErrInvalidArgument`、`ErrInstructionCommitted`、`ErrUnknownInstruction`、
`ErrAttemptCommitted`、`ErrAttemptAborted`、`ErrInvalidField`、`ErrInvalidValue`、
`ErrReplayMismatch`、`ErrReplayExhausted`，均可用 `errors.Is` 判断。

## 典型流程

```go
dev := regaccess.NewDevice()
session := regaccess.NewSession(dev)

dev.Raise(regaccess.EventDataReady)

tx, err := session.Begin("configure-and-clear")
if err != nil {
    panic(err)
}

events, err := tx.ReadEvents() // 观察 generation=1，并暂存 through=1 的确认
if err != nil {
    panic(err)
}

if err := tx.StageConfig(regaccess.Config{
    Enabled: true,
    Mode:    5,
    Speed:   10,
    Tag:     0x5A,
}); err != nil {
    panic(err)
}

// 交错：提交前设备又置起同一位，产生 generation=2。
if events.Has(regaccess.EventDataReady) {
    dev.Raise(regaccess.EventDataReady)
}

receipt, err := tx.Commit() // 只确认 generation=1；generation=2 仍可见
if err != nil {
    panic(err)
}

replay, err := session.Replay(receipt.ID())
if err != nil {
    panic(err)
}
original, err := replay.ReadEvents() // 拿回 generation=1 的原始结果
if err != nil {
    panic(err)
}
_ = original
```

撤回时调用 `tx.Abort()` 或 `tx.Rollback()`；所有暂存写入和事件确认都会丢弃。

## 运行方法

要求 Go 1.20 或更新版本。无需下载依赖。

```bash
go test ./...
```

显示逐个测试：

```bash
go test -v -count=1 ./...
```

数据竞争检测：

```bash
go test -race -count=1 ./...
```

覆盖率摘要：

```bash
go test -count=1 -cover ./...
```

## 本地合成数据验证范围

`regaccess_test.go` 全部使用进程内合成 `Device`，不访问网络、真实设备或外部材料。
已覆盖：

1. **正常读完即清**：提交后观察到的事件位消失，代际前沿正确推进。
2. **暂存读取视图**：同一尝试第二次读事件时看到自己待提交的清除结果，但外部设备在提交前仍可见旧事件。
3. **题面关键同 bit 交错**：读取旧事件后、提交前再次 `Raise` 同一位；提交只确认旧代际，新事件保持可见且代际为 2，随后可由新尝试独立读取并清除。
4. **不同事件位交错**：读取 A 后置起 B；提交只清 A，B 保留。
5. **空读取边界**：第一次没有事件时不会留下任何 ack；之后置起的事件在提交后仍存在。
6. **多字段同指令提交**：`StageConfig` 暂存四个字段，提交前外部寄存器不变，提交后两个寄存器和字段值正确；字段读改写保留无关高位。
7. **非法字段边界**：超宽字段值和非法 `Field` 返回可 `errors.Is` 分类的错误；整组配置校验先于修改，不留下半组暂存。
8. **撤回语义**：`Abort` 丢弃配置写和事件确认，外部寄存器/事件不变；撤回后提交返回 `ErrAttemptAborted`；撤回 ID 可重新开始。
9. **重放原始读取**：提交后改写寄存器并注入 generation=2 事件，重放仍返回原始事件代际、原始寄存器值和暂存字段读值；错误地址不消费记录，耗尽返回正确哨兵错误；重复重放器互相独立。
10. **提交幂等**：同一 `Attempt.Commit` 重复调用返回同一 `Receipt`；已提交 ID 不能再次 `Begin`；未知 ID 重放失败。
11. **多尝试交错**：慢尝试读取 generation=1 后，快尝试先提交 generation=1，随后新事件成为 generation=2；慢尝试的旧 ack 不会清掉 generation=2。另验证两个尝试对同一寄存器不同字段的提交按字段掩码合并。
12. **并发与多寄存器原子快照**：48 个 goroutine 分别提交成对寄存器，并发读取器通过 `RegisterSnapshot` 只能观察到“整对为零”或“整对为提交值”，未观察到撕裂；该测试也在 `-race` 下通过。

实际复核命令：

- `go test -count=1 ./...`：通过。
- `go test -race -count=1 ./...`：通过。
- `go test -v -count=1 ./...`：13 个测试函数全部 `PASS`。
- `go test -count=1 -cover ./...`：通过并输出语句覆盖率摘要。
