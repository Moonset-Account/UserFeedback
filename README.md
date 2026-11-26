# segmentmerge

一个用 Go 1.20 实现的、可由模拟段驱动的相邻数据段合并/拆回组件。它不依赖第三方库，也不访问网络；所有验证数据均为本地合成数据。

## 解决的问题

网卡的不同硬件队列可能乱序回报段。组件同时维护两种顺序：

1. **连接内接收编号（`RecvID`）顺序**：用于乱序缓存、驱动确认和丢号跳过。
2. **字节序号范围（`SeqStart` / `SeqEnd`，左闭右开）**：用于判断数据是否真正相邻。

只有同一个连接、字节序号连续、协议标记/非结束标志位/选项一致的段才会进入同一聚合。显式标记丢失的接收编号可以让发布前沿跳过该编号，但会在丢失位置切开聚合，绝不把丢失编号两侧的段合并到同一组。

## 公开 API

核心类型和方法：

```go
m := segmentmerge.New(segmentmerge.Config{
    MaxBytes:            4096, // 0 表示默认 64KiB；负数表示不限长度
    FirstReceiveID:      1,    // 首个连接内接收编号；0 也是合法起点
    RequireConfirmation: true, // true 时 Submit 后还需驱动确认
})

err := m.Submit(segmentmerge.Segment{
    ConnID:    "conn-1",
    RecvID:    1,
    SeqStart:  1000,
    SeqEnd:    1005, // 必须等于 SeqStart + len(Data)
    Data:      []byte("hello"),
    Timestamp: time.Now(),
    Protocol:  "IPv4",
    Flags:     0,
    Options:   segmentmerge.Options{"dscp": "cs1"},
    End:       false,
})

_ = m.Confirm("conn-1", 1)        // 确认单个接收编号
_ = m.ConfirmThrough("conn-1", 9) // 累计确认已回报的编号；不会虚构未知编号
_ = m.MarkLost("conn-1", 2)       // 唯一允许跳过编号的方式，并形成聚合边界

aggs := m.Drain()      // 取出并清空已立即封组的聚合
_ = m.Flush("conn-1")  // 强制封上该连接当前开放的前缀组
m.FlushAll()           // 强制封上所有开放组
aggs = m.Drain()
```

`Segment` 的结束标记可以用 `End: true`，也可以设置 `Flags: segmentmerge.FlagEnd`。`FlagEnd` 是边界控制位，不参与“标记是否一致”的比较；其他 `Flags` 位、`Protocol` 和 `Options` 必须一致。

`Drain` 返回的每个 `*Aggregate` 包含：

- 连接 ID、接收编号首尾、字节序号首尾；
- 合并后的 `Data`；
- `Boundaries`：每个原始段的接收编号、在聚合中的偏移/长度、序号范围和原始时间戳；
- `Reason`：`capacity`、`end_flag`、`sequence_discontinuity`、`attributes_mismatch`、`receive_id_lost` 或 `flush`。

排障时可拆回原始段：

```go
parts := agg.Split() // 返回独立副本，修改结果不会污染 Aggregate
err := agg.Validate() // 校验 Data、边界、序号、时间戳索引和保留原件是否一致
```

其他辅助方法包括 `Aggregate.Len()`、`Aggregate.SegmentCount()` 和 `Merger.NextReceiveID(conn)`。`Merger` 的方法由互斥锁保护，可被模拟多个硬件队列的 goroutine 并发调用。

## 发布与封组规则

- 乱序 `Submit` 会先缓存；发布前沿停在第一个未满足条件的接收编号。
- 默认 `RequireConfirmation: false`：成功 `Submit` 即视为该编号已确认。
- `RequireConfirmation: true`：必须再调用 `Confirm` 或 `ConfirmThrough`。即使后段的字节序号已经连续，也必须等前面的接收编号确认后才能封组/发布。
- 未知接收编号不会被 `ConfirmThrough` 自动视为确认或丢失；只有显式 `MarkLost` 才能跳过。
- 丢失编号会强制封住前一个聚合，丢失编号之后的段从新组开始。
- 下一段会导致总长度超过 `MaxBytes` 时，立即封住旧组并以该段开启新组；总长度恰好达到 `MaxBytes` 时也立即封组。
- 看到 `End`/`FlagEnd` 后，先把该段加入当前组，再立即封组；后续段从新组开始。
- 序号不连续或属性不一致时，旧组以对应原因封组，当前段开启新组。
- `Flush`/`FlushAll` 只封住已经形成的连续前缀，不会跳过未确认、未到达或未标记丢失的编号。
- `Submit` 会复制 payload 和 options；`Split` 也返回副本，避免调用方缓冲区别名影响排障结果。

## 运行方法

要求 Go 1.20.2 或兼容的 Go 1.20.x 工具链。

```bash
go test ./...
go test -race ./...
```

查看详细用例：

```bash
go test -count=1 -v ./...
```

本仓库没有第三方依赖，也不需要联网下载模块。

## 已验证范围

`merger_test.go` 使用本地合成段覆盖了以下正常、边界和交错场景：

- 顺序合并、聚合字节拼接、时间戳/偏移/长度/序号边界保留，以及 `Split()` 后与原始段一致；
- 接收编号按 `3,2,1` 等乱序回报后恢复为接收编号顺序；
- 显式确认开关、提前确认后段、累计确认，以及“后段已到但前段未确认不能发布”；
- 接收编号缺失时阻塞；显式 `MarkLost` 后跳过并切开聚合；
- 先标记丢失、后收到同编号段时返回 `ErrReceiveLost`；对已收到的编号调用 `MarkLost` 返回 `ErrAlreadyReceived`；
- 长度上限恰好达到、下一段会溢出、旧组立即封组/新组开启、单段超过上限被拒绝；
- `End`/`FlagEnd` 立即封组且后续段另开新组；
- 协议、Flags、Options 不一致时切组；`nil` 与空 Options 视为相同；
- 字节序号不连续时切组；
- 两个连接、两个接收编号空间交错回报时互不串组；
- `Flush` 不跳过接收编号缺口，缺口处理后缓存后段仍可发布；
- 调用方在 `Submit` 后修改底层字节/Map，以及修改第一次 `Split()` 结果，均不会污染组件或后续拆分；
- 空连接 ID、反向序号、序号范围与 payload 长度不一致、重复接收编号等错误；
- 64 个 goroutine 模拟硬件队列并发乱序提交，配合 `-race` 验证锁保护和最终顺序。

本机实际执行结果：

- `go test -count=1 ./...`：通过；
- `go test -race ./...`：通过。

## 非目标与限制

- 这是模拟/排障组件，不连接真实网卡、不抓包、不发送或访问网络目标。
- 不自动检测丢号；丢号必须由模拟驱动或上层驱动显式调用 `MarkLost`。
- 序号使用 `uint64` 且测试数据不涉及 TCP 32 位序号回绕。
- 不实现定时刷新；开放组等待后续相邻段，模拟结束时可调用 `FlushAll`。
- 超过 `MaxBytes` 的单段被视为配置/输入错误，而不是静默截断，以保证原始段边界可完整拆回。
