# pppframe — 双向 PPP FCS 成帧适配器

模块名：`pppframe`  
Go：`go 1.20`（已用 `go version go1.20.2 darwin/arm64` 实测）  
依赖：仅 Go 标准库，不需要联网或安装第三方依赖。

本包实现异步串行链路上的类 PPP/HDLC 成帧判断：

- 定界符：`0x7e`（`Flag`）
- 转义前缀：`0x7d`（`Escape`），后继字节与 `0x20` 异或还原
- 校验：16 位 PPP FCS，反射多项式 `0x8408`，初值 `0xffff`，结果取反
- 控制映射：PPP ACCM，`ControlMap` 是 32 位映射
- 输入可任意分批；`0x7d` 与后继字节可以落在两次读取中
- 逐帧校验 FCS，只有完整且校验通过的 payload 才放入 `Report.Frames`
- 连续定界符只计数，不会作为空 payload 上交
- 支持接收映射、发送映射分别确认，并按指定方向在帧边界生效
- 半帧、复位、坏 FCS、短帧、非法转义、超长帧等交错情况均有测试

这里的“有效载荷”指定界符、转义和解 FCS 之后的任意字节。包不额外添加或解释 PPP Address/Control/Protocol 字段；如果链路要求 `ff 03 ...`，调用方可将其作为 payload 前缀放入。

## 帧格式

```text
0x7e | byte-stuff(payload || FCS-low || FCS-high) | 0x7e
```

- FCS 只覆盖 payload。
- `0x7e`、`0x7d` 始终转义。
- `ControlMap` 中置位的 `0x00..0x1f` 控制字节在发送时转义。
- FCS 字节与普通字节一样，也按映射和特殊字符规则转义。

## 公开 API 概览

### 映射和方向

```go
type ControlMap uint32

const (
    ControlMapNone    ControlMap = 0x00000000
    ControlMapAll     ControlMap = 0xffffffff
    DefaultControlMap            = ControlMapAll
)

func MapFor(bytes ...byte) ControlMap
func (m ControlMap) Has(b byte) bool
func (m *ControlMap) Set(b byte)
func (m *ControlMap) Clear(b byte)

type Direction uint8
const (
    DirectionReceive Direction = 1 << iota // 1
    DirectionSend                           // 2
    DirectionBoth                           // 3
)

type Maps struct {
    Send    ControlMap
    Receive ControlMap
}
```

### FCS 与编码

```go
func FCS16(p []byte) uint16                 // 返回在线上传输的取反后 FCS
func VerifyFCS(payloadAndFCS []byte) bool   // 含两个小端 FCS 字节

func EncodeFrame(payload []byte, sendMap ControlMap) []byte
func AppendFrame(dst []byte, payload []byte, sendMap ControlMap) []byte
func Encode(payload []byte, sendMap ControlMap) []byte
func AppendEmptyDelimiter(dst []byte) []byte
```

### 分批接收状态机

```go
type Receiver
func NewReceiver(receiveMap ControlMap, opts ...Option) *Receiver

func (r *Receiver) Feed(chunk []byte) Report
func (r *Receiver) Reset() (DroppedFrame, bool)

func (r *Receiver) ReceiveMap() ControlMap
func (r *Receiver) PendingReceiveMap() (ControlMap, bool)
func (r *Receiver) UpdateReceiveMap(m ControlMap) bool

func (r *Receiver) Stats() Stats
func (r *Receiver) Drops() []DroppedFrame
```

`Feed` 返回：

```go
type Report struct {
    Frames            [][]byte         // 本批完成且 FCS 有效的 payload
    Drops             []DroppedFrame  // 本批完成的丢帧及原因
    EmptyDelimiters   int             // 本批中的空定界符数量
    DiscardedControls []byte          // 按当前接收映射丢弃的裸控制字节
    Resyncs           int             // 在本批中遇到的重同步定界符
}
```

丢帧原因：

```go
DropShortFrame     // 解转义后不足两个 FCS 字节
DropFCS            // FCS-16 校验失败
DropInvalidEscape  // 0x7d 后直接遇到定界符等非法转义边界
DropOversized      // 超过 WithMaxPayload 限制
DropReset          // 帧未结束时调用 Reset
DropBareControl    // 仅 BareControlRejectFrame 策略下立即丢帧
```

### 双向 Adapter

```go
type Adapter
func NewAdapter(out io.Writer, opts ...Option) *Adapter

func (a *Adapter) SendFrame(payload []byte) (int, error)
func (a *Adapter) Write(p []byte) (int, error) // 每次 Write 是一帧
func (a *Adapter) BeginFrame() *FrameWriter

func (a *Adapter) FeedSerial(chunk []byte) Report
func (a *Adapter) Reset() (DroppedFrame, bool)

func (a *Adapter) ConfirmDirectionMap(dir Direction, m ControlMap) (bool, error)
func (a *Adapter) UpdateConfirmedMap(dir Direction, m ControlMap) (bool, error)
func (a *Adapter) ConfirmedMaps() Maps
func (a *Adapter) PendingReceiveMap() (ControlMap, bool)
func (a *Adapter) SetSendMap(m ControlMap)
func (a *Adapter) SetReceiveMap(m ControlMap) bool
```

`FrameWriter` 允许把一帧的上层 payload 分半写入，最后 `Finish`/`Close`：

```go
fw := adapter.BeginFrame()
_, _ = fw.Write(firstHalf)
_, _ = fw.Write(secondHalf)
wireBytes, err := fw.Finish()
```

发送映射在 `BeginFrame` 时固定；帧未完成期间确认的新发送映射不会改变当前半成帧，只影响之后的帧。

### 选项

```go
func WithMaps(Maps) Option
func WithSendMap(ControlMap) Option
func WithReceiveMap(ControlMap) Option
func WithMaxPayload(n int) Option       // n <= 0 表示不限制
func WithBareControlPolicy(BareControlPolicy) Option
```

默认最大 payload 为 `DefaultMaxPayloadSize = 1 << 20`，另外允许两个 FCS 字节。

## 关键行为说明

### 接收映射中的未转义字节

题面规定：当前接收映射中且**未转义**到达的控制字节先丢弃；如果它以 `0x7d xx` 转义序列到达，则还原后属于 payload。

默认策略是 `BareControlDiscardByte`：

1. 该裸字节不进入解转义数据；
2. 不参与增量 FCS；
3. 记录到 `Report.DiscardedControls` 和 `DroppedFrame.DiscardedControls`；
4. 若两端映射不一致，发送端 FCS 包含该字节而接收端 FCS 不包含，帧边界处通常得到 `DropFCS`。

如果部署策略要求“看到裸映射控制字节立即丢帧”，可使用：

```go
r := pppframe.NewReceiver(m,
    pppframe.WithBareControlPolicy(pppframe.BareControlRejectFrame))
```

此时原因是 `DropBareControl`，接收端从下一个 `0x7e` 重新同步。另有 `BareControlAcceptByte` 仅供互操作测试。

### 映射协商的帧边界

- 发送方向：新确认映射用于下一次 `SendFrame` 或下一次 `BeginFrame`；已经开始的 `FrameWriter` 继续使用创建时的快照。
- 接收方向：
  - 当前不在帧中间时，`UpdateReceiveMap` 立即生效并返回 `true`；
  - 正在组帧或等待复位后重同步时，映射进入 pending 状态并返回 `false`；
  - 到下一个 flag 帧边界时自动应用 pending 映射。
- `ConfirmDirectionMap(DirectionSend, m)` 不会修改接收映射，反之亦然。

### 复位和重同步

帧未结束时调用：

```go
d, ok := receiver.Reset()
```

若已有部分帧内容，`ok == true` 且 `d.Reason == DropReset`。随后所有非定界符字节都忽略，直到下一个 `0x7e`；该 flag 作为重同步点消费，之后的字节进入新帧。

### 连续空定界符

接收端把 flag 视为边界。启动后的第一个 flag 只负责 hunt/sync，不计为空帧；之后如果两个 flag 之间没有任何字节，后一个 flag 完成一个空定界符并递增 `Report.EmptyDelimiters`/`Stats.EmptyDelimiters`。空定界符不会进入 `Frames`，也不是丢帧。

真正的空 payload 帧仍包含两个 FCS 字节（空 payload 的 FCS 为 `0x0000`，按映射可能被转义），因此会产生一个长度为 0 的有效 `Frames` 元素，可与裸连续 flag 区分。

## 本地运行方法

在当前工作区执行：

```bash
go test ./...
```

可选：

```bash
go test -race -count=1 ./...
go test -cover -count=1 ./...
go test -v -count=1 ./...
```

不需要运行外部服务，也不访问网络。

## 已执行的复核

最终实际执行：

```text
$ go version
go version go1.20.2 darwin/arm64

$ go test -v -count=1 ./...
--- PASS: TestFCS16KnownVectorAndIndependentImplementation
--- PASS: TestEncodeEscapesMappedAndFramingBytes
--- PASS: TestAllByteValuesRoundTripOneByteAtATime
--- PASS: TestEverySplitKeepsEscapeSequenceAcrossReads
--- PASS: TestReceiveMapDiscardsBareByteButAcceptsEscapedValue
--- PASS: TestFCSCorruptionShortAndInvalidEscapeReasons
--- PASS: TestResetBeforeFrameEndResyncsAtNextFlag
--- PASS: TestConsecutiveEmptyDelimitersAreIgnored
--- PASS: TestOversizedFrameDropsAndResyncs
--- PASS: TestReceiveMapConfirmationAppliesOnlyAtFrameBoundary
--- PASS: TestAdapterDirectionalMapsAndHalfFramePeerExchange
--- PASS: TestIntegratedHalfFramesMapNegotiationAndEmptyDelimiters
--- PASS: TestEmptyPayloadFrameIsDistinctFromEmptyDelimiter
--- PASS: TestFrameWriterSnapshotsSendMapUntilBoundary
--- PASS: TestAdapterNilOutputAndShortWriter
PASS
ok  	pppframe	0.268s
```

另外实际执行：

```text
$ go test -race -count=1 ./...
ok  	pppframe	0.440s

$ go test -cover -count=1 ./...
ok  	pppframe	0.414s	coverage: 80.7% of statements
```

## 测试覆盖的正常、边界和交错情况

- FCS-16 标准检查值 `0x906e`，并用独立逐位实现对随机数据交叉核对。
- FCS 小端输出和固定 good residual `0xf0b8` 的往返验证。
- `0x00..0xff` 所有字节在多种 ACCM 下的编码、转义和解码。
- 每次一个字节喂入完整帧，验证分批状态和跨批转义。
- 对完整线序的每个切分点分别切成两半，验证任意半帧切分，尤其是 `0x7d` 与后继分离。
- 映射内字节：
  - 转义形式必须还原为 payload；
  - 未转义形式按题面丢弃，并在映射不一致时观察到 `DropFCS`；
  - 严格策略下验证 `DropBareControl` 和下一 flag 重同步。
- FCS 损坏、短帧、`Escape + Flag` 非法转义的原因断言。
- 帧中间 `Reset()`、垃圾字节、下一定界符后恢复有效帧。
- 连续 flag 序列的空定界符计数，以及空 payload 有效帧与裸 flag 的区别。
- 超长帧立即记录 `DropOversized`，并在后续 flag 后恢复。
- 接收映射在半帧中间更新时必须 pending，旧映射完成当前帧，边界后新映射生效。
- 发送/接收方向独立更新；非法 `Direction` 返回 `ErrInvalidDirection`。
- 双向 Adapter 合成回环：A 发半帧给 B，B 解出 payload；B 回复，A 分半接收。
- 集成用例同时交错：连续空定界符、半帧、帧中确认新接收映射、FCS 坏帧、恢复后新映射帧和尾部空定界符。
- `FrameWriter` 分半写 payload，并断言发送映射快照只在下一帧更新。
- nil 输出返回 `ErrNoOutput`；短写/零写触发 `io.ErrShortWrite`；重复关闭返回 `ErrFrameClosed`。

## 安全与验证边界

本次实现和测试只使用进程内合成字节、`bytes.Buffer` 和测试 writer；没有联网、没有访问真实串行设备或凭据、没有上传材料，也没有安装依赖。内容仅包含题面要求的防御性成帧、校验、丢弃原因和重同步判断，不包含利用、入侵、攻击或规避载荷。
