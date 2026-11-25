# framemux — 多路复用消息连接的发送端

一个消息代理连接在同一条字节流上复用多个逻辑通道。本包实现该连接的**发送端**
（外加一个用于自测与对端模拟的流式重组器），满足：

- 同一通道的每条消息严格按 **方法帧 → 内容头帧 → 若干正文帧** 的顺序写出；
- 不同通道的整帧可以穿插，同一消息的帧不会被其它通道拆开；
- 持续有积压的通道按环形轮转（round-robin）轮流获得“一整个帧”的发送机会；
- 可按通道**暂停**新消息：只有方法帧已经开始的那条消息允许收尾，其余排队等待；
- 可运行时调整**最大正文帧长**：已开始封装/写出的帧保持原边界，新长度只作用于
  尚未开始封装的正文帧；
- 可在**整帧之间**插入心跳，心跳绝不会截断一个在写的帧；
- 传输回调每次最多收到 `WriteLimit` 个字节；
- 接收端按字节流重组，恢复出的每条消息与原文逐字节一致。

## 线上帧格式

每个帧统一为：

```
octet  frame type        1=method 2=header 3=body 8=heartbeat
short  channel id        大端；心跳固定在通道 0
long   payload size      大端，负载字节数
...    payload
octet  frame end marker  固定 0xCE
```

固定开销为 `HeaderSize = 8` 字节。方法帧负载 4 字节（class-id + method-id），
内容头负载 22 字节（class-id、weight、body-size、flags、message-id），
心跳帧负载为空。

## 公开 API

### 发送端 `Conn`

| 符号 | 说明 |
| --- | --- |
| `Dial(transport Transport, cfg Config) *Conn` | 建立连接并启动唯一的写 goroutine |
| `(*Conn).Open(id uint16) error` | 注册逻辑通道；重复打开是 no-op |
| `(*Conn).Publish(id uint16, m Message) error` | 在通道上排队一条消息（正文会被防御性拷贝） |
| `(*Conn).Pause(id uint16) error` | 暂停通道的新消息；在途消息仍可写完 |
| `(*Conn).Resume(id uint16) error` | 解除暂停 |
| `(*Conn).SetFrameMax(n int) error` | 调整正文帧最大负载；`n<=0` 返回 `ErrFrameMax` |
| `(*Conn).QueueHeartbeat() error` | 在下一个整帧边界插入一个心跳 |
| `(*Conn).Idle(ctx context.Context) bool` | 等待全部排队/在途消息与心跳写完；ctx 取消返回 false |
| `(*Conn).Stats() Stats` | 连接与各通道状态快照（暂停标志、排队数、在途标志等） |
| `(*Conn).Close() error` | 排空剩余工作后停止写 goroutine，回传传输错误 |
| `(*Conn).Err() error` / `Done() <-chan struct{}` | 故障与退出观测 |

数据类型：`Transport func(b []byte) error`、`Config{FrameMax, WriteLimit}`、
`Message{Method, Header, Body, ID}`、`Method`、`Header`、`Frame`、`Stats`、
`ChannelStats`。哨兵错误：`ErrClosed`、`ErrUnknownChannel`、`ErrFrameMax`。

### 接收端 `Receiver`（重组器）

| 符号 | 说明 |
| --- | --- |
| `NewReceiver(onMessage func(ch uint16, m Message), onFrame func(Frame)) *Receiver` | 构造重组器 |
| `(*Receiver).Write(p []byte) (int, error)` | 喂入任意切分的字节流；半帧自动缓冲 |
| `(*Receiver).Pending() int` | 当前为凑齐一帧而缓冲的字节数 |

重组器只做**防御性**校验：帧结束标记、方法/头帧负载长度、帧序（方法→头→正文）、
正文不超出/不短少于头帧声明的 body-size、心跳必须在通道 0 且无负载、未知帧类型。
发现违规即返回对应哨兵错误（如 `ErrBodyOverflow`、`ErrHeaderOrder`）并停止处理。

## 关键设计与正确性要点

- **单写 goroutine + 条件变量**：所有传输调用都在 `pump` 中发生，`Transport`
  永不并发。控制面（Pause/SetFrameMax/…）与写者通过一把互斥锁协作。
- **整帧轮转**：`order` 环形数组 + `cursor`，每发出一个整帧就把游标移过该通道，
  因此所有持续积压的通道公平地轮流得到整帧机会。
- **在途帧不可变**：`encapsulateLocked` 只负责按当前 `frameMax` 封装下一帧并保存为
  `pendingFrame`；`pump` 随后把这块已固定的字节完整写完。即便 `SetFrameMax` 在
  传输分片之间发生，也只影响之后封装的帧，正在写的帧边界不变。
- **暂停语义**：`nextReadyLocked` 对暂停通道只放行 `inFlight != nil`（方法帧已开始）
  的消息；尚未开始的队列消息保持不动，其它通道照常发送。
- **心跳边界**：心跳计数只在 pump 处理完一个完整帧后才被消费并封装，因此天然落在
  整帧之间。
- **小字节回调**：`writeAllLocked` 按 `WriteLimit` 切片调用传输，并在回调期间释放
  锁，避免慢传输阻塞控制调用。
- **正文拷贝**：`Publish` 复制 body 并以实际长度回填 `Header.BodySize`，调用方之后
  修改原切片不会影响线上字节。

## 运行方法

需要 Go 1.20.2（仓库 `go.mod` 声明 `go 1.20`），无任何外部依赖，纯标准库。

```sh
go test ./...            # 运行全部测试
go test -race ./...      # 带竞态检测
go test -run TestPause -v
gofmt -l .               # 格式检查（应为空输出）
```

## 验证范围（本地合成数据）

测试全部使用进程内合成的内存传输与重组器，不访问网络、不读取凭据、不安装依赖。

| 测试 | 覆盖点 |
| --- | --- |
| `TestBasicRoundTrip` | 空正文/单帧/多帧/不可整除正文；逐字节重组一致；回调不超过 WriteLimit |
| `TestRoundRobin` | 3 通道持续积压时严格按通道轮转整帧，且每条消息帧序为 method/header/body×3 |
| `TestPauseOnlyFinishesInFlight` | 方法帧在途时暂停：该消息写完、后续消息排队；暂停前未开始则整条不动；其它通道不受影响；Resume 后全部正确重组 |
| `TestFrameMaxChange` | 在正文帧写到一半时改限：在写帧保持旧边界，其后帧按新限拆分；消息间改限；非法参数报错 |
| `TestHeartbeatBetweenFrames` | 帧写出中途排队心跳，心跳在整帧边界出现、通道 0、空负载，不破坏消息重组 |
| `TestConcurrentInterleaving` | 6 通道多 goroutine 并发发布/心跳/暂停恢复，150 条消息全部逐字节还原、通道内不重复 |
| `TestDefensiveCopy` | Publish 后篡改原 body 不影响线上字节 |
| `TestTransportErrorStopsWriter` | 传输报错后写者退出、`Close/Err` 回传该错误、后续发布返回 `ErrClosed` |
| `TestUnknownChannelAndCloseAPI` | 未知通道、重复 Open、关闭后 API 的错误路径 |
| `TestReceiverMalformedStream` | 逐字节喂入下的坏结束标记、错误负载长度、帧序错乱、正文溢出/欠收、非法心跳、未知帧类型 |
| `TestIdleTimeout` | 暂停通道仍有队列时 `Idle` 超时返回 false，恢复后正确排空 |
| `TestIdleWaitsForHeartbeatBytes` | 心跳已封装但字节在途时 `Idle` 必须等待，不得提前判定空闲 |
| `TestEmptyBodyMessage` | 零长度正文只有方法帧+头帧即可完成一条消息 |

测试已实际执行 `go test -race -count=1 ./...`、重复多轮的普通/竞态运行，全部通过。
