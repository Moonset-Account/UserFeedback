# 线程运行时信号派发模型（sigdispatch）

一个纯用户态、不接触宿主机 OS 信号机制的 Go 信号派发模型，用 Go 1.20.2
标准库实现，无任何外部依赖。模型模拟 POSIX.1-2008 的核心信号语义，
线程由 goroutine 表示，所有状态由一把互斥锁保护，可并发使用。

## 语义模型

1. **投递目标**
   - `Runtime.SendProcess`：投到整个进程，进入进程级待处理集合。
   - `Thread.SendThread`：只投给指定线程，进入该线程私有待处理集合。
   - 进程与每个线程各持有一个独立的 `pendingSet`，互不串队列。
2. **合并与排队**
   - 普通信号（编号 `< RTMin`，即 `< 32`）：同号只保留**第一份**未投递
     载荷，后续同号信号在首份被取走前直接丢弃。
   - 实时信号（编号 `>= RTMin`，即 `>= 32`）：同号按到达顺序全部排队
     （FIFO），逐份投递。
3. **显式取信号（`Thread.Take` / `Wait`）**
   - 在线程私有集合与进程集合中，选**未屏蔽的最小信号号**。
   - 同号时线程私有项优先于进程项。
   - 进程项被任一线程取走即从进程集合删除，保证只交给一个线程。
   - 被屏蔽的信号号整体跳过；所有候选都被屏蔽时 `Take` 返回 `ok=false`，
     `Wait` 阻塞等待新信号（条件变量唤醒）。
4. **处理器与掩码（`Thread.Dispatch` / `WaitAndDispatch`）**
   - 进入处理器前临时屏蔽：信号自身 + `Handler.Mask` 声明的附加信号。
   - 其他信号保持未屏蔽，可在处理器内递归 `Dispatch` 形成嵌套帧。
   - 掩码按信号引用计数压栈/出栈，处理器即使 panic 也会恢复上一层掩码。
5. **载荷标识**：每次投递分配单调递增的 `Payload.Seq`，可以准确核对
   普通重复信号最终留下的是哪一份载荷、实时队列里还排着哪几份。

## 公开 API

包路径：`sigmodel/sigdispatch`（module 名 `sigmodel`）

| 标识符 | 说明 |
| --- | --- |
| `type Sig int` | 信号号；`Sig.IsRT()` 判断是否实时信号 |
| `const RTMin Sig = 32` | 实时信号起始编号 |
| `type Payload struct{ Seq, Code int; Data any }` | 信号载荷，`Seq` 为到达序号 |
| `type HandlerFunc func(*Thread, Sig, Payload)` | 处理器函数类型 |
| `type Handler struct{ Fn HandlerFunc; Mask []Sig }` | 处理器及附加屏蔽掩码 |
| `type PendingView struct{ Standard map[Sig]Payload; Realtime map[Sig][]Payload }` | 待处理集合快照 |
| `func NewRuntime() *Runtime` | 新建运行时 |
| `(*Runtime).SetHandler(Sig, Handler)` | 安装/替换处理器 |
| `(*Runtime).NewThread() *Thread` | 新建线程（空掩码） |
| `(*Runtime).SendProcess(Sig, int, any) Payload` | 向进程投递信号 |
| `(*Runtime).ProcessPending() PendingView` | 进程待处理快照 |
| `(*Thread).ID() int` / `Exit()` | 线程 ID / 退出（私有待处理丢弃） |
| `(*Thread).SendThread(Sig, int, any) Payload` | 向该线程投递信号 |
| `(*Thread).Block(...Sig)` / `Unblock(...Sig)` | 引用计数式屏蔽/解除 |
| `(*Thread).Blocked(Sig) bool` / `Mask() []Sig` | 掩码查询 |
| `(*Thread).Pending() PendingView` | 线程私有待处理快照 |
| `(*Thread).Take() (Sig, Payload, bool)` | 非阻塞显式取一次信号 |
| `(*Thread).Wait() (Sig, Payload)` | 阻塞直到可取并取走 |
| `(*Thread).Dispatch() bool` | 取一个信号并运行处理器（含临时掩码） |
| `(*Thread).WaitAndDispatch() (Sig, Payload)` | 等待后运行处理器 |

## 复现步骤

要求：本机 Go 1.20.2（本仓库即在 `go1.20.2 darwin/arm64` 下验证）。

```bash
go test -race -count=1 -v ./...   # 全量功能 + 竞态检测
go test -count=5 ./...            # 多轮重复，观察并发稳定性
```

如需固定工具链，可用 `GOTOOLCHAIN=local go test ./...` 禁止自动下载。

## 验证范围（全部为真实断言，无预置演示输出）

`sigdispatch/sigdispatch_test.go` 共 12 个测试，覆盖：

- 正常场景
  - `TestStandardSignalCoalescesAndKeepsFirstPayload`：普通同号 3 份只留
    第一份，取走后重复份不可再投递。
  - `TestRealtimeSignalsQueueEveryOccurrenceInOrder`：实时同号 4 份全部
    保留并按到达顺序逐份取出。
  - `TestStandardAndRealtimePendingAreIndependent`：两类队列独立计数。
- 边界场景
  - `TestThreadPrivateQueueIsIndependent`：线程定向信号不泄漏进进程队列。
  - `TestTakeChoosesSmallestUnblockedAndPrivateWinsOnTie`：最小未屏蔽号
    选择、屏蔽跳过、同号私有项优先、解除屏蔽后可投递。
  - `TestProcessSignalGoesToExactlyOneThread`：进程信号仅一个线程消费。
  - `TestBlockedThreadLeavesProcessSignalForAnotherThread`：屏蔽线程取不到、
    另一线程可取。
  - `TestDispatchWithoutHandlerStillConsumesAndRestoresMask`：无处理器也
    消费信号且掩码不泄漏。
- 交错/嵌套场景
  - `TestHandlerMaskNestingAndRestoration`：处理器内自身信号与声明掩码被
    屏蔽、无关信号可嵌套；内层掩码为并集，返回后逐层恢复；处理器内新到
    的自身信号恢复后可投递。
  - `TestTwoThreadsDifferentMasksFullScenario`（题面主场景）：两线程分别
    屏蔽不同号；核对普通重复信号留下的具体载荷（按 `Seq`/`Data` 断言）、
    实时信号剩余份数（3→2→1→0），以及屏蔽项对 A 不可见、对 B 可消费。
- 并发场景
  - `TestConcurrentRealtimeDeliveryAcrossThreads`：4 个 goroutine 线程经
    `Wait` 阻塞接收共 100 个实时信号，恰好投递 100 次、队列清空。
  - `TestConcurrentStandardSendsCoalesceToSingleItem`：50 个并发普通投递
    合并为恰好 1 项。

以上测试均已在本地用 `go test -race -count=1` 与 `-count=5` 实际执行通过；
不存在未执行的测试。

## 目录结构

```
go.mod
README.md
sigdispatch/
    runtime.go           # Runtime、投递、选择规则
    pending.go           # 待处理集合（合并/排队/快照）
    thread.go            # Thread、掩码引用计数、处理器嵌套
    sigdispatch_test.go  # 12 个真实断言测试
```

## 建模边界

- 这是线程运行时内部的派发模型，不注册、不拦截、不发送任何宿主机 OS
  信号，全部数据为本地合成数据。
- 线程由 goroutine 扮演；`Wait` 用条件变量阻塞，信号到达时广播唤醒。
- 线程 `Exit` 后其私有待处理信号丢弃（不回并进程队列），与题面"独立队列"
  一致。
