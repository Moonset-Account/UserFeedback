# flowctl — 交换机共享缓冲链路流控模型

Go 1.20 实现的确定性、无外部依赖的交换机缓冲 / PFC 风格流控模型。每个控制流由
`(入口端口 Port, 优先级 Priority)` 唯一标识；所有优先级共享同一块字节缓冲。

模型覆盖题面要求的：

- 每轮输入若干到达帧（字节数、入口端口、优先级）和出口可发字节；
- 按流维护观测占用、暂停状态和在途预留；
- 到达字节核销本流的在途预留；
- 根据高水位发送 pause，根据本流低水位和共享池在途余量发送 resume；
- 完整记录确实无法放入的整帧，不做部分接收；
- 支持多入口、多优先级、暂停 / 恢复 / 到达 / 出口发送交错；
- 提供默认物理容量准入和可选的预留保护准入。

## 运行方法

环境为 `go version go1.20.2 darwin/arm64`，但 `go.mod` 只声明 Go 1.20 语言版本。

```bash
go test ./...
```

查看详细测试：

```bash
go test ./... -v
```

覆盖率：

```bash
go test -cover ./...
```

没有第三方依赖，也不需要联网或安装依赖。

## 核心记账

设：

- `U`：所有流的实际观测占用之和 `Used`；
- `R_f`：流 `f` 尚未被到达字节核销的在途预留；
- `R = Σ R_f`：全部已承诺在途量；
- `C`：`Config.Capacity` 共享缓冲容量。

共享池在途余量为：

```text
M = C - U - R
```

`M` 可以为负；负值表示多个暂停承诺已经超额预订共享池，`Snapshot.Unbacked`
会给出未被容量覆盖的承诺量 `max(0, U+R-C)`。

### 暂停

当未暂停流的观测占用满足：

```text
Used_f >= PauseThreshold
```

控制器立即发出 `pause` 指令，并按入口端口取在途字节数 `I_p`：

```text
R_f += I_p
R   += I_p
```

`I_p` 来自 `Config.InFlightByPort[p]`，未配置时使用 `Config.InFlight`。
同一流已处于暂停状态时不会重复发送 pause。

### 到达与核销

完整帧被接收后：

```text
Used_f += L
U      += L
writeoff = min(L, R_f)
R_f     -= writeoff
R       -= writeoff
```

因此，在没有出口发送和新增非预留占用时，`Used_f + R_f` 保持不变。核销只作用于
帧所属的 `(入口, 优先级)` 流，不会跨端口或跨优先级核销。

### 恢复

暂停流只有同时满足以下条件才发出 `resume`：

```text
Used_f <= ResumeThreshold
M      >= I_p
```

边界语义为：

- 暂停：`>= PauseThreshold`；
- 恢复低水位：`<= ResumeThreshold`；
- 共享在途余量：`>= InFlight[p]`；
- 整帧接收：`Used + L <= Capacity`。

当前实现采用保守的全局口径：`M` 已扣除包括本流剩余预留在内的全部承诺。也就是说，
如果本流仍有旧预留，恢复检查仍要求扣除所有预留后另有完整 `I_p` 余量。
`FlowSnapshot.AvailableToFlow = M + R_f` 另外给出“不触碰其他流预留时本流可用的
记账空间”，便于调用方采用其他调度策略。

出口发送只减少实际占用：

```text
Used_f -= Sent
U      -= Sent
```

出口发送不核销在途预留；题面规定预留由该流后续到达字节核销。

## 丢帧与准入策略

帧是不可分割的。若：

```text
Used + L > Capacity
```

则整帧不接收，`Used` 和 `Reservation` 均不变，并记录 `DroppedFrame`：

- `frame_too_large`：`L > Capacity`；
- `buffer_full`：帧没有超过整个缓冲，但超过当前物理空闲空间。

`Config.Admission` 支持两种策略：

- `PhysicalOnly`（默认）：只按真实共享缓冲容量判断，严格对应“确实放不下”。
  预留仍用于 pause/resume 决策和余量计算，但不阻止其他流使用当前物理空闲空间；
  超额共享预订最终会通过真实 `buffer_full` 丢帧暴露。
- `ProtectReservations`：除物理容量外，还保护已有预留。对帧长 `L`、本流预留
  `R_f` 和全局余量 `M`，帧中超过本流预留的部分必须满足：

  ```text
  max(0, L-R_f) <= max(0, M)
  ```

  否则记录原因为 `reserved_headroom_exhausted` 的整帧拒绝。该策略不会让无预留流
  占用已承诺给其他在途帧的共享余量。

## 出口字节归属

题面只给出“出口可发字节”，没有给出出口调度规则。API 提供两种方式：

1. `Controller.Drain(maxBytes)` / `Round.EgressAvailable`：使用确定性分配顺序：
   - `DrainKeyAsc`（默认）：端口升序，再优先级升序；
   - `DrainPriorityDesc`：优先级数值降序（高优先级先发送），再端口升序。
2. `Controller.Depart(Departure{...})` 或 `Round.Departures`：显式指定从哪个
   `(入口, 优先级)` 流发送多少字节。测试低水位和交错顺序时建议使用这种精确方式。

`Round.Order` 支持：

- `EgressFirst`（默认零值）：先用本轮出口预算释放缓冲，再处理到达；
- `ArrivalsFirst`：先处理到达和即时 pause，再出口发送并 reconcile resume。

更细的交错顺序可以直接依次调用 `Arrive`、`Depart/Drain`、`ReconcileResumes`。

## 公开 API 概览

核心类型：

```go
type Bytes int64
type PortID int
type Priority int

type Key struct {
    Port     PortID
    Priority Priority
}

type Frame struct {
    Bytes    Bytes
    Port     PortID
    Priority Priority
}

type Config struct {
    Capacity        Bytes
    PauseThreshold  Bytes
    ResumeThreshold Bytes
    InFlight        Bytes
    InFlightByPort  map[PortID]Bytes
    Admission       AdmissionPolicy // PhysicalOnly 默认，或 ProtectReservations
    DrainOrder      DrainOrder      // DrainKeyAsc 默认，或 DrainPriorityDesc
}
```

主要方法：

```go
func New(cfg Config) (*Controller, error)

func (c *Controller) Arrive(f Frame) (*ArrivalResult, error)
func (c *Controller) Depart(d Departure) (Bytes, error)
func (c *Controller) Drain(max Bytes) (*DrainResult, error)

func (c *Controller) CheckResume(k Key) ResumeEligibility
func (c *Controller) ResumeIfEligible(k Key) (*Directive, bool)
func (c *Controller) ReconcileResumes() []Directive

func (c *Controller) Step(r Round) (*RoundResult, error)

func (c *Controller) Snapshot() Snapshot
func (c *Controller) Flow(k Key) (FlowSnapshot, bool)
func (c *Controller) Drops() []DroppedFrame
func (c *Controller) Events() []Event
func (c *Controller) Pauses() []Directive
func (c *Controller) Resumes() []Directive
func (c *Controller) CheckInvariants() error
```

`RoundResult` 包含每个到达结果、出口分配、本轮新事件和轮末 `Snapshot`。
`Event` / `Directive` / `DroppedFrame` 均带 JSON tag，便于后续输出为 JSONL 或
接入外部观测程序。

## 示例

`example_test.go` 中的 `ExampleController_Step` 会被 `go test` 实际执行：

```go
package main

import (
    "fmt"
    "flowctl"
)

func main() {
    c, _ := flowctl.New(flowctl.Config{
        Capacity:        12,
        PauseThreshold:  6,
        ResumeThreshold: 2,
        InFlight:        2,
    })

    c.Step(flowctl.Round{
        Arrivals: []flowctl.Frame{{Bytes: 6, Port: 1, Priority: 0}},
    })

    ar, _ := c.Arrive(flowctl.Frame{Bytes: 2, Port: 1, Priority: 0})
    fmt.Println(ar.WrittenOff) // 2

    key := flowctl.Key{Port: 1, Priority: 0}
    c.Depart(flowctl.Departure{Key: key, Bytes: 6})
    d, ok := c.ResumeIfEligible(key)
    fmt.Println(ok, d.Occupancy, d.SharedMargin) // true 2 10
}
```

## 已实现的验证范围

`flowctl_test.go` 和 `example_test.go` 使用本地合成数据覆盖：

1. 配置校验：非正容量、阈值顺序、`ResumeThreshold < PauseThreshold`、负数在途量；
2. 暂停高水位边界 `Used == PauseThreshold`；
3. pause 后按端口配置记录在途预留；
4. 到达字节全额 / 部分核销本流预留；
5. 核销不跨端口、不跨优先级；
6. 已暂停流不重复发送 pause；
7. 恢复低水位边界 `Used == ResumeThreshold`；
8. 共享池余量不足时阻止 resume，余量恰等于在途量时允许 resume；
9. 整帧恰好填满缓冲、下一帧真实溢出；
10. 大于整缓冲的帧记录为 `frame_too_large`；
11. 部分空闲不足时整帧拒绝，不发生部分接收；
12. `ProtectReservations` 下无预留流不能占用其他流的预留余量；
13. 多入口、多优先级 pause / write-off / drain / resume 交错；
14. 同一入口不同优先级分别获得各自的在途预留；
15. `InFlight = 0` 的恢复边界；
16. 默认 key 顺序和严格优先级出口分配；
17. `ArrivalsFirst` 与 `EgressFirst` 的确定性事件顺序；
18. resume 后再次到达高水位、残留预留先核销再追加新承诺的周期；
19. 非法帧和超额 departure 在 `Step` 中不改变状态；
20. 1000 次确定性伪随机合成交错操作后的记账不变量：
    - 全局 `Used` 等于各流占用之和；
    - 全局 `Reserved` 等于各流预留之和；
    - `0 <= Used <= Capacity`；
    - 所有流预留非负；
    - `SharedMargin == Capacity-Used-Reserved`；
    - `PhysicalOnly` 模式下每条 drop 均满足物理容量确实不足。

测试不读取凭据、不联网、不访问真实目标，也不安装依赖。

## 实测结果

以下命令均在当前工作区实际执行通过：

```text
$ go test ./... -count=1
ok  	flowctl	...

$ go test -cover ./... -count=1
ok  	flowctl	...	coverage: 90.3% of statements
```

详细模式包含 16 个测试函数（其中配置校验含 7 个子测试）和 1 个可执行 Example，全部 PASS。
确定性合成交错测试最后一次实测输出的汇总形态为：

```text
synthetic run: used=90 reserved=0 drops=428 pauses=86 resumes=84 events=1962
```

其中 drops 较多是因为该压测脚本故意以高达 40 字节的随机帧持续冲击 100 字节缓冲；
每条 drop 都有断言证明其在 `PhysicalOnly` 口径下确实超过物理容量，且记账不变量在
每一步后均通过检查。
