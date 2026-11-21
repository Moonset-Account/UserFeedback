# drr — 按字节计费的差额轮转调度器

Go 1.20.2，纯标准库，无外部依赖。

## 功能

多条 FIFO 出口队列，每队有独立轮转量（quantum）。调度器按调用方指定的
队列顺序轮转：

- 每次轮到非空队列，余额增加一次轮转量；
- 余额足够时连续发送队首包（包可以比一整轮额度还大，余额跨轮累积）；
- 离开仍非空的队列保留余额，队列清空则余额归零；
- 下游每次只接收给定数量的包：`Next(n)` 最多发出 n 个；额度尚足被迫暂停时，
  下次调用从原队原余额继续（不重复发放额度），因此一次取十个与连续十次各取
  一个得到相同序列；
- 队列排空后再进包，从零额度重新开始轮转；
- 每个发出的包以 `Event{QueueID, PacketID, Remaining}` 返回，调用方可逐次看到
  队号、包号及剩余额度。

## 实际入口

- `scheduler.go`
  - `NewScheduler(order []string, quanta map[string]int) (*Scheduler, error)` —
    按调用方给定轮转顺序构建；拒绝空顺序、重复队列、缺失/非正轮转量。
  - `(*Scheduler).Enqueue(queueID string, p Packet) error` — 入队；拒绝未知
    队列与非正包长。
  - `(*Scheduler).Next(n int) []Event` — 最多发出 n 个包，状态跨调用保留，
    可断点续传。

## 验证范围（`scheduler_test.go`，`go test ./...` 全部通过）

- `TestSchedulerByteAccountingSequence` — 可接受结果：双队列不同轮转量、
  超过单轮额度的大包，逐事件断言队号/包号/剩余额度的完整序列。
- `TestSchedulerRejectsInvalidInput` — 应被拒绝的结果：空顺序、重复队列、
  缺失/零/负轮转量、未知队列入队、零/负包长。
- `TestSchedulerBatchEqualsOneByOne` — 交错场景：一次取 7 个与连续 7 次各取
  1 个序列完全一致。
- `TestSchedulerResumeKeepsBalance` — 边界：额度尚足被迫暂停后从原队原余额
  继续（剩余 700 而非 1700）。
- `TestSchedulerRefilledQueueStartsFromZero` — 边界：队列排空后再进包从零
  额度开始。
- `TestSchedulerPacketLargerThanQuantum` — 边界：大于单轮额度的包在单次
  `Next` 内跨多轮累积后发出。
- `TestSchedulerEmptyAndZeroLimit` — 边界：空调度器与 `Next(0)`/负数限额
  不发出任何包且不丢包。

## 运行

```sh
go test ./...
```
