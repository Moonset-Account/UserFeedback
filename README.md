# causalcodec

一个仅依赖 Go 标准库的分布式事件总线因果上下文编解码模块。它把同一次投递的错误来源、致因事件、多条因果路径、阶段替代原因和已协商的上下文锚点编码到固定字节预算内，并允许接收端在缺少某个精确锚点版本时保留其余可独立恢复的分支。

环境按题目要求使用 `go1.20.2`，`go.mod` 中声明 `go 1.20`。

## 核心模型

```text
Report
├── Delivery{ID, EventID, Source}   # 投递 ID、致因事件、错误来源
├── Causes[]Cause                    # 按 ID 去重的原因表
└── Paths[]Path                      # 同一 EventID 的一条或多条路径
    └── Segments[]Segment            # 有序阶段
        ├── CauseIDs[]               # 同一阶段的多个替代原因；第一个为主原因
        ├── Anchor                   # 已传给对端的精确上下文锚点
        ├── Omitted[]                # 被裁剪原因的轻量身份/优先级
        └── Gap                      # 显式断链标记，绝不会被猜测补全
```

关键语义：

- 相同 `Cause.ID` 在二进制原因表中只存一次，多个路径和阶段通过 `uint16` 索引引用。
- `Priority` 越大越重要。可先设置 `Codec.MinPriority`，再由 `MaxBytes` 按最低优先级继续裁剪。
- 某个阶段的原因全部被裁剪且没有锚点时，会生成 `GapPriority` 断链，而不是伪造原因。
- 锚点键是完整的 `Name + Version + SHA-256 Digest`。本地存在同名但版本或摘要不同的锚点时不会被使用。
- 锚点缺失只让对应分支/片段产生 `GapMissingAnchor`；其他长度分帧的路径和同一路径中锚点前后的内联片段仍会返回。
- `Report.Extend` 可以把后续到达的诊断原因接到某个 gap 后；原 gap 保留为 `[EXTENDED]` 愈合标记，便于显示“断开—扩展”的对应关系。

## 公开 API

最常用的入口：

```go
func Encode(report Report) ([]byte, error)
func Decode(data []byte, anchors AnchorResolver) (*Report, error)

type Codec struct {
    MaxBytes    int             // 0=DefaultMaxBytes(4096)，负数=不限制
    MinPriority Priority
    Anchors     AnchorResolver  // 仅 Decode 使用
}

func NewCodec(maxBytes int) *Codec
func (c *Codec) Encode(report Report) (*Packet, error)
func (c *Codec) Decode(data []byte) (*Report, error)
```

锚点：

```go
type AnchorRef struct {
    Name    string
    Version string
    Digest  string // 推荐由 NewAnchor / DigestContext 生成
}

type AnchorStore struct{ /* concurrency-safe */ }

func NewAnchorStore() *AnchorStore
func NewAnchor(name, version string, ctx Context) (*Anchor, error)
func ValidateContext(ctx Context) error
func DigestContext(ctx Context) (string, error)

func (s *AnchorStore) Put(anchor *Anchor) error
func (s *AnchorStore) PutContext(name, version string, ctx Context) (*Anchor, error)
func (s *AnchorStore) ResolveAnchor(ref AnchorRef) (*Anchor, error)
func (s *AnchorStore) HasExact(ref AnchorRef) bool
```

显示与扩展：

```go
func Display(report *Report) string
func (o DisplayOptions) Display(report *Report) string

func (r *Report) Extend(ext Extension) (*Report, error)
func (r *Report) Cause(id string) (Cause, bool)
func (r *Report) Gaps() []Gap
func (r *Report) OpenGaps() []Gap
func (r Report) Clone() *Report
```

可通过 `errors.Is` / `errors.As` 识别的错误：

- `ErrInvalidReport`
- `ErrInvalidAnchorRef`
- `ErrAnchorNotFound`
- `ErrAnchorDigestMismatch`
- `ErrMalformedPacket`
- `ErrBudgetExceeded`
- `ErrGapNotFound`

部分解码时，`Codec.Decode` 会同时返回非空 `*Report` 和 `*PartialDecodeError`；其中可包含若干 `*BranchError`，底层可继续 `errors.Is(err, ErrAnchorNotFound)`。

## 线格式与固定预算

数据包布局：

```text
"CCX1" | uint8 version | uint32 body_len | uint32 crc32(body) | body
```

`body` 内依次为：

1. `Delivery` 与 `Source`
2. 去重后的 `Cause` 表
3. 多个 `uint32 length + path block`

每个 path block 独立解析；单个路径中的锚点错误不会阻止其他路径返回。段内保留原因索引、裁剪原因的紧凑 ID/优先级/原因码、锚点引用和 gap 类型。

编码预算过程：

1. 校验必填事件、路径 ID、原因引用和同 ID 冲突定义。
2. 对重复 `Cause.ID` 去重。
3. 先应用 `MinPriority`。
4. 实际编码并检查 `len(packet) <= MaxBytes`。
5. 超预算时，删除当前保留原因中优先级最低者；同优先级按 ID 稳定排序。
6. 被删原因转为 `OmittedCause`；若段内已无原因/锚点，则生成显式 gap。
7. 若所有可删原因都已删除仍超预算，返回 `*BudgetError`，不会返回超长包。

`Packet.Plan` 记录最终大小、未裁剪大小、引用次数、重复引用数、估算节省字节、保留/删除原因、锚点和 gap ID。

## 运行方法

```bash
go version           # 本工作区实测为 go1.20.2 darwin/arm64
go test -count=1 ./...
go test -race -count=1 ./...

# 可运行合成演示；也可用下面的测试入口看到同一份 main() 输出
go run ./cmd/demo
go test -v ./cmd/demo
```

演示全部使用内存合成数据，不连接真实事件总线、网络服务或凭据。

## 演示输出摘要

`cmd/demo` 构造了同一 `evt-order-42` 的三条路径、同阶段主/替代原因、一个接收端缺少的 `warehouse-db@v2` 锚点，以及一个可用的 `cache-sidecar@v3` 锚点。实际输出中的压缩计划为：

```text
budget=1500 encoded=1406 full_unpruned=1742
cause_references=10 unique_catalog_entries=7 repeated_references=4 duplicate_savings~667B
retained=dispatch-saturated, shared-publish-timeout, broker-leader-alt, quorum-ack-failed
dropped id=cause-db-lock priority=debug reason=below_priority_threshold
dropped id=cause-db-timeout priority=low reason=below_priority_threshold
gap_ids=gap:path-east:3:storage.deep
```

断开后的显示保留高优先级错误和替代原因，并明确标出低优先级链与缺失锚点：

```text
[storage.deep] [CHAIN BREAK/priority_disconnect] lower-priority causes disconnected ...
    gap=gap:path-east:3:storage.deep
    omitted cause-db-timeout (low): below_priority_threshold
    omitted cause-db-lock (debug): below_priority_threshold
[warehouse.context] [CHAIN BREAK/missing_anchor] exact anchor unavailable: warehouse-db@v2#...
    anchor required=warehouse-db@v2#...
...
● [primary/high] InvalidationBacklog ...
    anchor resolved=cache-sidecar@v3#...
```

接收端虽有同名 `warehouse-db@v1`，但错误中会列出该本地锚点且不会用它补链。随后用 `Extend` 接回迟到的存储诊断：

```text
[storage.deep] [EXTENDED] late storage diagnostics reconnected this tail
● [primary/high] ControllerQueueThrottled: nvme controller depth exceeded ...
● [primary/low] DatabaseFsyncTimeout: ...
...
open gaps after extension: 1 (missing exact anchor remains explicit)
```

剩余的一个 open gap 正是仍不可用的精确锚点；扩展不会掩盖它。

## 已执行的验证范围

实测命令与结果：

```text
$ go test -count=1 ./...
ok  	causalcodec	...
ok  	causalcodec/cmd/demo	...

$ go test -race -count=1 ./...
ok  	causalcodec	...
ok  	causalcodec/cmd/demo	...
```

根包 11 个测试覆盖：

- 正常往返：投递 ID、事件 ID、`Source`、多路径、阶段替代原因顺序和原因正文。
- 输入校验边界：缺少事件、引用未知原因、同 ID 原因正文冲突、锚点缺少 digest、锚点上下文内嵌未解析锚点均返回相应错误。
- 重复原因：同一原因被 3 条路径引用时，计划中只有 1 个唯一原因、3 次引用；线包中唯一长正文只出现一次，解码后三条路径均可解析。
- 优先级断链：`MinPriority: PriorityNormal` 裁剪 low/debug，生成 `GapPriority`；显示包含保留的高优先级原因和被裁 ID，但不泄露被裁长正文。
- 固定预算边界：
  - 完整包大小 `N`；
  - `MaxBytes=N` 成功且不裁剪；
  - `MaxBytes=N-1` 成功、不超长且至少裁剪一个原因；
  - 从 `120` 到 `N` 按步长 37 扫描，成功时断言 `len(packet) <= budget`，失败时断言为 `ErrBudgetExceeded`；
  - `MaxBytes=16` 返回 `ErrBudgetExceeded`。
- 扩展：愈合指定 gap、加入新原因、显示扩展结果、不修改原报告；愈合标记和说明可再次编码/解码；未知 gap 和重复扩展均返回 `ErrGapNotFound`。
- 锚点交错：同一路径按“内联片段 → 缺失的 v2 锚点 → 内联片段”排列，解码后三段都符合预期；可用 v3 锚点正常展开；本地同名 v1 的唯一正文不会进入结果或显示。
- 错误指纹：同名同版本但 digest 错误时返回 `ErrAnchorDigestMismatch`，不会解析本地正确正文。
- 畸形包：翻转 CRC 覆盖的尾字节和截断包头均返回 `ErrMalformedPacket`。
- 并发：32 个精确锚点并发读取通过 race detector。
- `cmd/demo/main_test.go` 实际调用 `main()`，确保演示数据和扩展流程可运行。

未覆盖/限制：没有连接真实事件总线或外部服务，没有使用真实凭据，没有联网或安装依赖；数据均为本地合成。该二进制格式带 `CCX1` 版本号，但当前只保证本 Go 包读写，不承诺跨语言规范或长期线格式兼容。SHA-256 digest 用于锚点身份/内容指纹，不提供认证、加密或访问控制。
