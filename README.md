# WebSocket 接收生命周期模块（C++17）

解决“WebSocket 接收器异步派发分片后，关闭流程留下已排队缓冲、或继续派发过期
消息”的问题。模块用**代次令牌 + move-only 缓冲句柄 + 可记录任务队列**把接收
生命周期做成可验证的状态机，并配套无外部依赖的测试，用事件日志证明：

- 每个获取的缓冲**最终恰好释放一次**（双重释放直接 `abort`）；
- 关闭后**不再派发任何新消息**（入口拦截 + 派发前代次校验双重保险）；
- 重连后新连接**独立接收完整消息**，旧代残留任务只清理不派发。

## 目录结构

```
include/ws_receiver.h    模块契约：事件日志、缓冲池、任务队列、Sink、Receiver
src/ws_receiver.cpp      实现
tests/test_lifecycle.cpp 自包含测试（7 个情形，147 项断言）
CMakeLists.txt           构建（无 cmake 时可直接调用 clang++，见下）
```

## 构建与运行

无第三方依赖，仅需 C++17 编译器：

```bash
# 方式一：直接编译
clang++ -std=c++17 -Iinclude -Wall -Wextra -Wpedantic -Wshadow -Wconversion \
        src/ws_receiver.cpp tests/test_lifecycle.cpp -o test_lifecycle
./test_lifecycle

# 方式二：CMake
cmake -S . -B build && cmake --build build && ctest --test-dir build --output-on-failure
```

测试会逐情形打印事件日志表（`seq / gen / action / detail`），末尾输出
`checks=N failures=0` 与 `RESULT: PASS`。

## 核心抽象与不变量

### 1. `BufferPool` —— 可记录、强制恰好一次释放

- 固定容量，`acquire()` 返回 **move-only** 的 `Buffer` 句柄；池耗尽返回空句柄
  并记录 `pool.exhausted`，不产生任何缓冲入账。
- 句柄析构必定归还池（RAII），所有 `acquire/release` 写入事件日志。
- 池内部用 `slotLive_` 跟踪每个缓冲是否在外；对同一 id 重复释放直接
  `std::abort()`（有专门测试验证 `SIGABRT`），把“恰好一次”变成强制不变量。

释放路径只有三类，全部经句柄析构收敛：

| 路径 | 触发 | 日志原因 |
|------|------|----------|
| 正常派发结束 | 片段任务执行完，`buf` 析构 | `fragment-task-end` |
| 排队残留清理 | `clearQueued()`/队列析构，任务连带 `buf` 析构 | `fragment-task-end` |
| 关闭后到达 | 在 `acquire` **之前**入口拒绝 | 无缓冲，无需释放 |

### 2. `TaskQueue` —— 任务队列替身

单线程 FIFO、**手动排空**（`runOne / drainAll / clearQueued`），入站、执行、
清理全部记录事件，使测试能精确制造“关闭先于/晚于回调”的时序。真实环境中可
替换为线程池/事件循环，`Receiver` 只依赖 `post` 语义。

### 3. `Receiver` —— 接收状态机与代次令牌

状态机：`Running -> Closing -> Closed`，`beginShutdown()` 幂等。

- 每个入站任务捕获入站时刻的**代次令牌 `gen`**；派发前校验
  `gen == generation_ && enterCallback()`。关闭（本代失效）或重连（代次 +1）
  后，过期任务只会记录 `queue.discard-closed` / `queue.discard-stale`，
  **绝不派发**，但其缓冲照常随任务析构释放。
- `pendingCallbacks_` 只统计**已开始执行**的在途回调（排队任务不计入），
  因此清理队列不会污染计数，关闭时计数总能归零。
  - 关闭时无在途回调：立即 `Closed`；
  - 关闭时仍有在途回调：进入 `Closing`，最后一个 `endCallback()` 落定 `Closed`。
- `reconnect()` 要求先 `Closed`：清理旧代残留、代次 +1、回到 `Running`，
  新连接独立拼装完整消息。

### 4. 本地类型化事件

`onFragment`（片段到达）、`onMessageComplete`（消息完成）、
`beginShutdown`（开始关闭）、`endCallback`（异步回调结束）四类，
全部通过 `EventLog` 留痕；`Sink` 只接收通过代次校验的事件，并按 `messageId`
累积片段、在完成时产出拼装好的完整消息。

## 测试情形与日志证据

| 情形 | 构造的交错 | 关键断言 |
|------|-----------|----------|
| 1 关闭先于回调 | 一片段在途、一片段排队时关闭 | 排队任务 `drop-queued` 且缓冲释放；关闭后到达事件被拒、不获取缓冲；`acquire==release` |
| 2 回调先于关闭 | 回调完整结束后再关闭 | `callback.end -> pool.release -> shutdown.begin -> closed` 顺序 |
| 2b Closing 中间态 | 在途回调计数=1 时关闭 | 进入 `Closing` 而非 `Closed`，最后 `endCallback` 才落定 |
| 3 正常完成 | 多片段 + 完成，多条消息 | 按序派发、拼装正确、多消息不串台 |
| 4 重复关闭 | 连续三次 `beginShutdown` | 仅一次 `shutdown.begin/closed`，其余记 `shutdown.duplicate` |
| 5 重连独立接收 | 关闭→`reconnect()`→新消息 | 代次 1→2；新代收到完整消息，旧代零派发 |
| 5b 旧代令牌拦截 | 手工注入重连后才执行的旧代任务 | 记 `discard-stale`，不派发，缓冲仍释放 |
| 6 池耗尽（补充） | 容量 2 的池连续入站 | 耗尽拒绝不泄漏，槽位释放后可复用 |

每个情形结束都执行 `checkPoolBalanced()`：`totalAcquired == totalReleased`、
`outstanding == 0`、每个槽位都已归还。

## 设计边界

- 任务队列替身是单线程确定性模型；`Receiver` 本身不内置锁，接入多线程执行器
  时，应由执行器保证任务回调串行化，或在 `Receiver` 外层加同步。
- 双重释放、`endCallback` 下溢、非 `Closed` 状态调用 `reconnect` 均按致命错误
  `abort` 处理，宁可快速失败也不破坏生命周期不变量。
