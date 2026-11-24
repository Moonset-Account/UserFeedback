# eventbuffer — 事件代理共享消息缓冲

用 Go 1.20.2（仅标准库，零外部依赖）实现的线程安全消息缓冲组件，模拟事件代理
向多个订阅者派送同一份消息时的字节额度与生命周期管理。

## 设计语义

1. **负载只存一份**：`Publish` 时复制一次负载字节，驻留期间所有订阅者的投递
   记录（`Delivery`）都指向同一个 `Message` 对象，不复制负载。
2. **每订阅独立在途额度**：每个队列有自己的 `quota`（字节）。`Deliver`/
   `Dispatch` 在派送**前**逐订阅检查 `inFlight + msg.Size <= quota`，额度不足
   返回 `ErrQuotaExceeded`，且不生成投递记录、不占用任何额度、不增加消息引用。
3. **确认/取消各自回收**：`Ack` 与 `Cancel`（`Nack` 为别名）只回收该投递所属
   队列自身占用的在途字节，不影响其他队列；重复结算返回 `ErrAlreadySettled`，
   不会重复回收。
4. **最后一个有效引用结束后才释放**：每条消息维护在途引用计数 `refs`，每成功
   派送一次加一，每次结算减一。`refs > 0` 时 `Reclaim` 不得释放该消息；归零后
   `Reclaim` 物理删除消息、回收其字节，并返回合并后的连续 ID 范围（闭区间）与
   新的缓冲占用量。仍在途消息前后的可释放消息会形成独立范围（“空洞”）。
5. **更名/同名重订**：旧队列进入 `replaced` 状态（不再接收新派送），新队列
   获得全新 ID、代数（generation）与独立额度；旧投递记录保留在旧队列上，
   旧的 `Ack`/`Cancel` 仍正确归到旧投递并回收旧队列额度。
6. **慢订阅退出**：`Unsubscribe` 只关闭队列（`closed` 状态），不自动取消其在途
   投递；旧投递仍可结算。当慢订阅的在途全部结算后，快订阅早已确认的消息会立即
   整体变为可释放，`Reclaim` 给出范围与新的缓冲占用量。

并发模型：单个 `sync.Mutex` 保护全部记账状态，所有公开方法均为线程安全。

## 公开 API

| 签名 | 说明 |
| --- | --- |
| `NewBuffer() *Buffer` | 创建空缓冲 |
| `Publish(payload []byte) *Message` | 负载复制一次后写入缓冲，返回消息（`ID`/`Size`/`Payload()`/`Refs()`） |
| `Subscribe(name string, quota int) *QueueSnapshot` | 新建订阅队列；同名重订会取代旧队列并发放全新额度 |
| `Rename(q *QueueSnapshot, newName string, quota int) (*QueueSnapshot, error)` | 更名：旧队列转 `replaced`，新名字下建立独立额度的新队列 |
| `Unsubscribe(q *QueueSnapshot) error` | 关闭队列；旧投递仍可结算 |
| `QueueByName(name string) (*QueueSnapshot, error)` | 按名字取当前活跃队列快照 |
| `Queue(id int) (*QueueSnapshot, error)` | 按 ID 取任意状态队列快照 |
| `Deliver(q *QueueSnapshot, msg *Message) (*Delivery, error)` | 单订阅派送，派送前检查该订阅额度 |
| `Dispatch(msg *Message) []DispatchResult` | 派送给全部活跃订阅，逐订阅返回 `Delivery` 或错误 |
| `Ack(d *Delivery) error` | 确认，回收所属队列在途字节并结束一个消息引用 |
| `Cancel(d *Delivery) error` / `Nack(d *Delivery) error` | 取消，回收规则同确认 |
| `Reclaim() ([]ReleasableRange, int)` | 释放所有 `refs==0` 的消息，返回连续 ID 范围与新占用字节数 |
| `Releasable() ([]ReleasableRange, int)` | 只读观察当前可释放范围与占用量，不删除消息 |
| `Occupancy() int` | 当前驻留消息总字节数 |
| `Inflight(q *QueueSnapshot) (int, error)` | 指定队列当前在途字节 |
| `Snapshot() []QueueSnapshot` | 全部队列（含 replaced/closed）状态快照 |

错误哨兵：`ErrUnknownSubscription`、`ErrNameInUse`、`ErrQuotaExceeded`、
`ErrAlreadySettled`，均可用 `errors.Is` 判断。

## 运行方法

```bash
go test ./...            # 常规测试
go test -race ./...      # 竞态检测
go test -v -cover ./...  # 详细输出与覆盖率
```

无需联网、无需安装任何依赖（模块仅声明 `go 1.20`）。

## 本地验证范围（合成数据，无真实目标/网络）

全部测试在 `buffer_test.go` 中，使用进程内合成字节切片：

1. `TestPayloadStoredOnceAndSharedAcrossSubscribers` — 负载发布时复制、多订阅
   共享同一消息对象、每订阅独立在途额度与缓冲占用。
2. `TestQuotaCheckedPerSubscriberBeforeDispatch` — 派送前额度拦截不留痕、单订阅
   额满不影响其他订阅、结算后可重派、`Dispatch` 成功/失败交错结果。
3. `TestAckAndCancelReclaimOnlyOwnQuota` — 确认/取消只回收本队列额度、引用计数
   联动、重复结算幂等、nil 防御。
4. `TestMessageHeldUntilLastReferenceSettles` — 三个订阅持有同一消息，前两个
   结算后仍驻留，最后一个引用结束后才可释放。
5. `TestReclaimRangesWithHolesAndOccupancy` — 在途消息制造“空洞”时返回多段
   范围、占用量精确（100→60→0）、`Releasable` 只读语义、连续 ID 合并。
6. `TestResubscribeSameNameFreshQuotaAndOldDeliverySettlement` — 同名重订新队列
   全新额度、旧队列 `replaced` 拒收新派送、旧确认归旧投递且互不干扰。
7. `TestRenameIssuesFreshQueueAndKeepsOldDeliveries` — 更名生成新队列、旧投递
   仍可结算、同名/占用名/陈旧句柄的错误路径、其他队列不受影响。
8. `TestSlowSubscriberLeavesFastAckedMessagesReclaimableAfterSettle` — 题面重点：
   快订阅全部确认、慢订阅退订后旧投递仍有效，慢订阅在途全部取消后消息立即整体
   可释放，范围 `[0,2]`、占用归 0。
9. `TestZeroByteMessagesAndZeroQuota` — 0 字节消息不耗额度、零额度队列拒收
   非零消息等边界。
10. `TestConcurrentDispatchSettleReclaim` — 4 队列 × 200 消息并发派送/确认/
    取消/回收，竞态检测下运行；后台协程持续校验内部记账不变量，最终校验回收
    范围恰好覆盖全部消息且无重复回收。
11. `TestReclaimedMessageRejectsNewDelivery` — 已回收消息拒绝再次派送/派发。
12. `TestNilAndDefensiveInputs` — nil 参数、未知 ID、陈旧句柄等防御路径。

白盒不变量检查（`checkInvariants`，在锁内核对）覆盖：`order` 与 `messages`
一致且升序、`occupancy` 等于驻留大小之和、每条消息 `refs` 等于其在途投递数、
每队列在途字节/数量与投递记录一致且不超额度、名字表只指向活跃队列。

实测结果：`go test -race -count=3 ./...` 全部通过；语句覆盖率 97% 以上。
未覆盖语句仅为正常 API 路径不可达的防御性钳制（记账非负保护、永不删除的
投递记录查表失败分支），无真实行为缺口。
