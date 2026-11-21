# 按页计量的共享内存池（Go 1.20.2）

为两个聚合算子（编号 `0`、`1`）和一个排序算子（编号 `2`）提供共用的、
按页计量的内存池，支持申请、归还、钉住/解除钉住，以及容量不足时的
撤回-溢写（spill）回收。

> 本地基础设施功能组件：仅依赖 Go 标准库，无外部目标、无网络、无真实负载。
> 所有验证均为普通单元测试，使用本地合成输入。

## 构建与验证

```bash
go test ./...            # 验收测试（真实断言）
go test ./... -race      # 带竞态检测
```

要求 Go 1.20.2（见 `go.mod` 的 `go 1.20`）。

## 实际入口（`pool.go`，包 `mempool`）

| 入口 | 作用 |
| --- | --- |
| `NewPool(totalPages int) *Pool` | 创建固定总页数的池 |
| `(*Pool).Acquire(op, needPages) (*Request, error)` | 提交整笔页申请；当场可满足即获准，否则进 FIFO 队列 |
| `(*Pool).Cancel(reqID) error` | 取消等待中的申请；已发出的在途溢写不撤销，回执仍归还实际释放的页 |
| `(*Pool).RespondSpill(spillID, success) error` | 溢写回执：`true` 真正释放页；`false` 该页本轮跳过；重复回执返回 `ErrDuplicateAck`，未知 ID 返回 `ErrUnknownSpill` |
| `(*Pool).Pin(op, page) / Unpin(op, page)` | 钉住页（不可回收/不可归还）与解除钉住 |
| `(*Pool).Release(op, page)` | 归还未钉住、不在溢写中的持有页 |
| `(*Pool).PendingSpills() []SpillRequest` | 轮询在途撤回请求 |
| `WithSpillHandler(func(SpillRequest))` | 可选事件回调，新撤回请求在池锁外派发（测试/执行器接入用） |
| `(*Pool).OwnerOf(page) / OwnedPages(op)` | 实时属主查询（区分"历史拿到过的页"与"当前持有的页"） |
| `(*Pool).Stats() / Validate()` | 容量快照与内部不变量校验 |

申请对象 `*Request` 提供 `Done() <-chan struct{}`、`Status()`、
`GrantedPages()`，分别用于等待落定、查状态、取获准页号快照。

## 关键语义与题面约束的对应

1. **恒等式**：任何时刻 `空闲页数 + 实占页数 == 总页数`。钉住页、
   在途溢写页都算实占；`Validate()` 在每个测试步骤后强制校验。
2. **整笔、按序获准**：申请进 FIFO 队列，只整笔满足；队首未满足时，
   后续申请即使空闲页够用也不得插队（`TestFIFOOrdering`）。
3. **撤回选页规则**：按算子编号 `0→1→2` 升序，逐页选择；
   钉住页、已在溢写的页、本轮失败跳过页均不可选
   （`TestSpillSelectionByOperatorOrder`、`TestPinnedPagesBlockReclaim`）。
4. **回执前仍占容量 / 不许同一页给两笔申请**：发出撤回只标记 `spillID`，
   页仍属原算子；成功回执才转入空闲集。缺口已被在途请求覆盖时不重复发
   撤回（`TestInFlightSpillBlocksAndNoDoublePromise`）。
5. **取消语义**：取消只移除队列项，在途撤回继续有效，其成功回执照常
   归还页并服务后续申请（`TestCancelWithInFlightSpill`）。
6. **手动失败 / 重复回执**：失败页进入该申请本轮 skip 集，
   没有其他可撤回页时申请继续等待；外部容量事件（释放/解钉/成功回执/
   取消/新申请）开启新一轮并清空 skip，失败页可被重试
   （`TestSpillFailureSkipThenRetry`）。重复回执仅首次生效。
7. **并发安全**：池用互斥锁保护，回调在锁外派发；竞态检测 + 并发
   不变量巡检（`TestConcurrentAcquireReleaseInvariant`）。

## 验证范围（`pool_test.go`，共 12 个用例）

- **可接受结果**：`TestHappyPathAcquireRelease`（申请/归还/复用全链路）。
- **应被阻止/拒绝的结果**：
  - 全钉住时申请只能等待、无撤回请求，钉住页禁止归还
    （`TestPinnedPagesBlockReclaim`）；
  - 溢写中页禁止钉住/归还、越权与越界参数拒绝
    （`TestPinReleaseRejectedWhileSpilling`）；
  - 非法参数与超容量申请拒绝（`TestInvalidAndTooLargeRequests`）；
  - 重复/未知回执拒绝（`TestDuplicateAndUnknownAck`）。
- **边界与交错场景**：
  - 在途溢写占容量、不重复许诺、FIFO 不插队、失败跳过与新一轮重试、
    取消后在途溢写仍归还、按算子编号选页、事件回调、并发不变量。

说明：`Request.GrantedPages()` 记录的是申请*最初获准*的页；页被合法
溢写回收后会再分配给其他算子，历史快照不会被改写。因此"同一页不能
同时许给两笔申请"的断言基于**实时属主**（`OwnerOf/OwnedPages` 与
测试助手 `assertNoDoubleOwnership`），而不是比较历史快照。
