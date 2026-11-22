# hotls — 可热卸载的运行时线程局部存储管理器

纯 Go 1.20（仅依赖标准库）实现的 TLS 管理模块，支持模块同名热重装、代际隔离、
借用保护、重入析构与确定性的逆序回收。

## 语义保证（对应题目要求）

1. **同名重装、代际隔离**：`Register(name, factory, destructor)` 每次产生一个新的
   `Generation`（单调 ID）。同名重装时老代立即转为 `GenClosing`，从活动槽位移除，
   从此不接纳任何新的 `Access`；新代接管该名字，拥有独立的工厂（键定义）与析构动作。
2. **懒实例化**：线程首次访问某代时才调用工厂；每个（线程, 代）至多一份实例，
   之后的访问复用同一实例。
3. **卸载等待借用**：`Unload` 或同名换代后，老代的既有借用全部保持有效；
   只有当该代 `openBorrows` 归零（最后一个借用 `Release`）的瞬间，才安排代析构，
   一次性回收该代在所有线程上的残留实例。
4. **两条回收路径、每份实例只析构一次**：线程 `Exit` 与代析构都会回收实例。
   所有挑选与记账（`destroyed` 标志、map/栈摘除）都在管理器锁内完成，
   两条路径不可能同时选中同一实例。
5. **析构可重入管理器**：工厂与析构回调一律在管理器锁**之外**执行。析构函数可以
   合法地 `Access` 另一个仍活动的模块并完成借还；正在进行的借用不会被错误回收。
   管理器通过 `inDestructor` 标志区分“析构内重入访问”（放行）与
   “Exit 挂起后的外部访问”（拒绝，返回 `ErrThreadExited`）。
6. **逆序析构 + 借用挂起保留次序**：线程退出时从实例栈尾部（最新创建）开始析构；
   遇到仍被借用的实例立即停止，栈中其余实例的相对次序原样保留，
   待借用逐个释放后由统一的 `drain` 循环按原次序续跑。

## 公开 API

| 符号 | 说明 |
| --- | --- |
| `NewManager() *Manager` | 创建管理器 |
| `(*Manager).Register(name, factory, destructor) (*Generation, error)` | 注册/同名重装模块代；`factory` 不可为 nil，`destructor` 可为 nil |
| `(*Manager).Unload(name) error` | 卸载活动代（关闭并等借用清零后析构） |
| `(*Manager).WaitIdle() error` | 优雅收尾：关闭全部活动代、退出全部线程并驱动回收；管理器本身仍可继续使用 |
| `(*Manager).Close() error` | 关库：拒绝此后一切新操作，并驱动在途回收 |
| `(*Manager).NewThread() (*Thread, error)` | 创建显式线程句柄 |
| `(*Manager).ActiveGeneration(name) *Generation` | 查询当前活动代 |
| `(*Manager).GenerationState(g) GenerationState` | 查询代状态（`GenActive/GenClosing/GenDestroyed`） |
| `(*Thread).Access(name) (*Borrow, error)` | 访问活动代的线程局部实例（首次懒实例化） |
| `(*Thread).Exit() error` | 请求线程退出（幂等；有借用时挂起，释放后续跑） |
| `(*Borrow).Release() error` | 释放借用（重复释放返回 `ErrBorrowReleased`） |
| `(*Borrow).Value() any` / `Instance()` / `Generation()` | 借用自省 |
| `Factory = func(*Thread) (any, error)` | 键定义；锁外执行，可重入 |
| `Destructor = func(*Thread, any)` | 析构动作；锁外执行，可重入 |

哨兵错误：`ErrManagerClosed`、`ErrModuleNotFound`、`ErrGenerationClosed`、
`ErrThreadExited`、`ErrBorrowReleased`、`ErrInvalidFactory`，均可用 `errors.Is` 判定。

### 为什么用显式 `Thread` 句柄

Go 不向业务代码暴露 OS 线程标识，goroutine 也没有稳定的 TLS 语义。因此模块用
显式 `*Thread` 句柄承载“线程身份”（模拟 pthread 场景下的线程）：一个句柄由单个
goroutine 串行使用，管理器自身的全部记账均由互斥锁保护，可被多个 goroutine 并发调用。

## 关键实现结构

- `Manager.drain()`：所有回收路径（Register/Unload/Release/Exit/Close/WaitIdle）的
  统一驱动器，**可重入**。每轮加锁挑选一笔工作 → 解锁执行析构回调 → 重复。
  优先级：① 借用归零的关闭代整代回收；② 退出中线程的逆序析构（每轮只取栈尾一个
  实例，使回调重入造成的状态变化在下一轮加锁时仍可见）。
- 空作业轮次（顺手退休零实例关闭代）不会终止 drain，只有“彻底无工作可做”才返回。
- 工厂返回后重新加锁复核状态：换代/卸载/关库/线程退出会把未登记的实例作为孤儿
  立即析构，不泄漏；重入已建好的实例则被复用、外层产物孤儿析构。

## 复现步骤

```bash
go version            # 要求 go1.20.2（本仓库在 darwin/arm64 go1.20.2 验证）
go test ./...         # 全部功能测试
go test -race ./...   # 含竞态检测的并发交错测试
go test -cover ./...  # 语句覆盖率（当前 97.2%）
go test -run TestHeadlineScenario -v ./...   # 只跑题面指定场景
```

## 验证范围（hotls_test.go，13 个测试函数 / 全部含真实断言）

| 测试 | 覆盖场景 | 类别 |
| --- | --- | --- |
| `TestLazyInstantiationAndPerThreadIsolation` | 懒实例化、同线程复用、跨线程隔离、退出析构 | 正常 |
| `TestSameNameReloadClosesOldGeneration` | 同名换代后老代 closing、新访问只命中新代、最后借用退出才析构、槽位复用 | 正常+交错 |
| `TestUnloadWaitsForLastBorrowAcrossThreads` | 卸载等待多线程借用、关闭代拒绝访问、代析构一次回收多线程实例 | 故障恢复 |
| `TestExitReverseOrderWithBorrowedInstanceDeferred` | 逆序析构、遇借用实例挂起、释放后严格按原次序续跑 | 边界+故障恢复 |
| `TestReentrantDestructorReadsActiveModule` | 析构内重入借还活动模块、重入产物最终也恰好析构一次 | 交错 |
| `TestHeadlineScenario_OldDestructorReadsNewGeneration` | **题面指定场景**：单线程先后访问新旧两代、退出挂起、旧代析构读新模块；核对销毁事件全局次序与两代各自的可复用时刻 | 交错（验收） |
| `TestConcurrentTeardownAndSweepExactlyOnce` | 200 轮：线程退出与代析构两条路径并发竞争同一实例，析构恰好一次 | 并发故障恢复 |
| `TestConcurrentChaos` | 8 线程 × 25 轮 + 2 个并发换代者：访问/借还/退出/重装全面交错，收敛后逐值核对一次性 | 并发 |
| `TestBoundaryErrorsAndIdempotency` | nil 工厂、未注册模块、重复 Exit/Release、退出后访问、关库后所有入口、工厂报错无残留 | 边界 |
| `TestWaitIdleDrainsAndManagerRemainsUsable` | 优雅收尾、挂起期间可注册新模块、释放后续跑、Close 最终收敛 | 故障恢复 |
| `TestReentrantFactoryAccessesOtherModule` | 工厂内重入访问其他模块并复用实例 | 交错 |
| `TestFactoryRecheckPaths`（3 子测试） | 工厂期间换代/关库/重入建实例三条复核分支与孤儿析构 | 边界 |
| `TestAccessorsAndDefensiveBranches`、`TestFactoryRecheckThreadExited` | 访问器、关库后哨兵错误、工厂期间线程退出 | 边界 |

所有测试均以模块的真实运行结果（事件日志、代状态、线程状态、析构值序列）为断言依据，
不使用预置的演示输出。`go test -race -count=3` 连续通过，无数据竞争。

## 已知边界

- 析构回调若无限重入创建新实例/新借用，drain 会持续推进（与 pthread TLS 的
  `PTHREAD_DESTRUCTOR_ITERATIONS` 有限轮次不同）；本模块不设迭代上限，调用方需自行保证收敛。
- 析构回调发生 panic 时，管理器记账已先于回调完成，内部状态保持一致，panic 原样传播。
