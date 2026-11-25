# condmodel — 确定性协程锁/条件变量模型

本包用纯状态机实现协程运行时中的**可重入互斥锁**和**条件变量**。它不创建真实
goroutine、定时器或线程；外部确定性调度器通过事件决定下一步推进哪个协程，因此
所有交错都可以稳定复现。

运行环境按题面使用 Go 1.20.2；`go.mod` 声明 `go 1.20`，无第三方依赖。

## 核心状态机

```text
holding(owner=T, depth=N)
  -- Wait(T) --> cond_waiting + owner="" + depth=0
                    |
                    | 第一个到达的 signal/broadcast/timeout/cancel
                    v
             lock_queued_once
             （在互斥锁 FIFO 队尾恰好插入一张票，restore_depth=N）
                    |
                    | GrantNext()，且锁空闲、队首是 T
                    v
             owner=T, depth=N, wait_granted_not_returned
                    |
                    | FinishWait(T)
                    v
             Wait 返回，调用方仍持有恢复后的 N 层锁
```

关键不变量：

- 同一持有者可重复 `Lock`，深度递增；每次 `Unlock` 只释放一层。
- 深度为 `N` 时调用 `Wait`，会在一个转换中清空 `owner/depth`，即一次释放全部
  `N` 层，而不是模拟 `N` 次解锁。
- 唤醒后进入互斥锁队列时保存 `restore_depth=N`；`GrantNext` 重新给锁时直接恢复
  原深度。
- 重新拿到锁和 `Wait` 真正返回被拆成两个可观察步骤：
  `GrantNext` 后已经是 owner/原深度，但 `FinishWait` 前 Wait 尚未返回。
- 每个 Wait 尝试只接受**第一个**终结唤醒结果：`signal`、`broadcast`、`timeout`
  或 `cancel`。之后到达的竞争事件只得到 `late_event_ignored`，不会改变原因，也
  不会第二次进入锁队列。
- `Signal`/`Broadcast` 没有当前等待者时直接丢失，不保存“通知许可”给后来的
  Wait。
- 条件队列和互斥锁队列均为 FIFO。广播按条件队列顺序把等待者追加到已有互斥锁
  队尾。
- 取消是任务级粘性状态，接近 `context.Context`：等待中首次取消会选定 `cancel`
  结果；已经被通知/超时选定后，取消只记录取消请求，不篡改该次 Wait 的结果。
  若取消在 Wait 前已到达，新 Wait 仍走完“全部释放 → 入锁队一次 → 重新拿锁 →
  持锁返回”的路径。

## 公开 API

### 构造与事件推进

- `NewScheduler() *Scheduler` / `New() *Scheduler`
- `Dispatch(Event) (Step, error)`：同步推进一个事件，返回事件后的快照。
- `Post(...Event)`：向确定性 FIFO 输入队列投递事件。
- `RunOne() (Step, error, bool)`：处理最早投递的一个事件。
- `RunPosted() ([]Step, error)`：按 FIFO 处理当前所有已投递事件。
- `StepCount() int`
- `History() []Step`：每一步都包含 `After Snapshot`。

事件类型包括：

```go
EventLock
EventUnlock
EventGrant
EventWait
EventFinishWait
EventSignal
EventBroadcast
EventTimeout
EventCancel
EventRegister
```

### 锁与条件变量便捷方法

```go
Lock(task ID) (Result, error)
Unlock(task ID) (Result, error)
GrantNext() (Result, error)

Wait(task ID) (Result, error)
FinishWait(task ID) (Result, error)

Signal() (Result, error)
Broadcast() (Result, error)

Timeout(waitID int) (Result, error)
TimeoutTask(task ID) (Result, error)
Cancel(task ID) (Result, error)
CancelWait(waitID int) (Result, error)
```

`Unlock` 只释放一层且不会偷偷把队首变成 owner；测试/调度器需要再调用
`GrantNext` 推进入锁队。这样通知、超时、取消可以在“已唤醒但尚未重新拿锁”的
窗口中显式交错。

### 每一步可观察状态

```go
type Snapshot struct {
    Step      int
    Owner     ID
    Depth     int
    LockQueue []LockEntry  // 互斥锁 FIFO；含 Task/RestoreDepth/WaitID/Sequence
    CondQueue []WaitEntry  // 条件变量 FIFO；含 SavedDepth/Reason/入队次数等
    Tasks     []TaskEntry
}

func (s *Scheduler) Snapshot() Snapshot
func (s *Scheduler) WaitAttempt(waitID int) (WaitInfo, bool)
```

`History()[i].After` 保存第 `i+1` 个事件后的相同快照。`WaitAttempt` 可核对单次
等待的终结原因、`LockEnqueueCount`、各转换步数和当前锁队位置。

## 两个等待者、广播与持锁取消的验证序列

核心回归测试是
`TestTwoWaitersBroadcastOrderAndCancelWhileHolding`，序列如下：

1. `A` 加锁到深度 3，调用 `Wait(A)`：一次释放 3 层，进入条件队首。
2. `B` 同样加锁到深度 3，调用 `Wait(B)`：条件队为 `[A B]`。
3. `M` 加锁后 `Broadcast()`：`Woken=[A B]`，互斥锁队为 `[A(depth=3) B(depth=3)]`，
   `M` 仍持锁。
4. `M.Unlock()` 后 `GrantNext()`：`A` 成为 owner，深度恢复为 3，锁队剩 `[B]`。
5. 在 `A` 已持恢复锁但 `FinishWait(A)` 前调用 `Cancel(A)`：结果为
   `late_event_ignored`，广播原因不变，锁队仍只有 `[B]`，`A` 的入锁次数仍为 1。
6. `FinishWait(A)` 返回 `broadcast` 且保持深度 3；随后用三次 `Unlock` 释放。
7. 再次 `GrantNext()`：`B` 恢复深度 3，`FinishWait(B)` 返回 `broadcast`。

另外两个取消测试分别覆盖：

- `TestCancelWinsAndReturnsWhileHoldingRestoredDepth`：取消先于通知/超时终结
  Wait；之后的 timeout/signal/broadcast/cancel 均不能改变结果或重复入队，最终
  以 `cancel` 结果持深度 3 的锁返回。
- `TestCancellationRequestedWhileHoldingBeforeWaitStillRestoresDepth`：持锁时、
  Wait 前已请求取消；该次 Wait 仍一次释放 3 层、只入锁队一次，并在重新拿锁后
  持深度 3 返回取消结果。

## 运行方法

```bash
go version          # 开发复核使用 go1.20.2 darwin/arm64
go test ./...
```

可选的确定性/竞态复核：

```bash
go test -race ./...
go test -count=20 ./...
```

## 实际验证范围

已在当前工作区实际执行：

- `gofmt -w .`：所有 Go 文件已格式化，`gofmt -l .` 无输出。
- `go test ./...`：通过，输出 `ok condmodel`。
- `go test -race ./...`：通过，输出 `ok condmodel`。
- `go test -count=20 ./...`：连续 20 次通过，输出 `ok condmodel`。

测试全部使用本地合成任务 ID（如 `a`、`b`、`broadcaster`），不访问网络、真实
凭据或外部服务。覆盖范围包括：

- 可重入加锁/逐层解锁和非 owner 解锁错误；
- 深度 3 Wait 一次释放全部层；
- 广播后两个等待者的条件队顺序、锁队顺序和深度恢复；
- 重新拿锁后、Wait 返回前的持锁取消；
- 取消先获胜时必须重新拿锁并持锁返回；
- signal/broadcast/timeout/cancel 四类首个事件获胜及后续竞争事件；
- 每个 Wait 恰好一次互斥锁入队；
- 空队列通知丢失、不向后来等待者发放预存许可；
- `Signal` 只唤醒条件队首，其余等待者保持不变；
- 旧 Wait 的超时事件按 WaitID 隔离，不会终结后来的新 Wait；
- 广播追加在已有普通互斥锁等待者之后的全局 FIFO 顺序；
- `Post/RunOne/RunPosted/History/Snapshot` 的确定性逐步推进；
- 空任务、重复注册、非 owner Wait/Unlock、未拿锁完成 Wait、未知 Wait ID 等
  无效操作。

未执行/未声称的检查：当前环境中单独运行 `go vet ./...` 需要额外授权，因此本
次没有把 `go vet` 结果记为已验证；普通 `go test` 会执行 Go 测试流程内置的部分
vet 检查。
