# watchcache — 可嵌入的资源监听缓存

面向控制面的进程内资源监听缓存（in-process watch cache）。源端按单调递增的
整数修订号提交一批对象变化，缓存提供标签分页查询、订阅事件流、书签确认与
断线恢复，全部仅依赖 Go 标准库，Go 版本要求 1.20。

## 语义模型

1. **整批原子提交**：`Commit(Batch{Revision, Changes})` 在一把写锁内折叠
   同批同名的多次变更、切换查询快照，再向各订阅者扇出。同批事件共享同一
   修订号，批末投递一个 `Bookmark`；查询侧要么看到整批之前、要么看到整批
   之后，看不到半批。
2. **分页固定修订号**：首次 `List` 固定当前修订号，续翻令牌
   （base64 编码的不透明 token）内嵌该修订号、offset 与选择器，后续页
   始终从同一修订号的历史快照读取。修订号被压缩时返回
   `ErrRevisionTooOld`，明确要求从头重列。
3. **标签进出筛选**：按批内折叠后的净状态判定——
   - 净进入筛选：发 `Put`；
   - 净离开筛选（含真实删除）：发携带旧对象的 `Delete`；
   - 同 UID 停留且有变化：发 `Put`；同名换 UID：先 `Delete`（旧值）后 `Put`；
   - 批内反复横跳但净状态不变：不发事件。
4. **书签即确认水位**：`Ack(rev)` 要求 rev 是已投递过的书签修订号，
   确认后已确认镜像（mirror）应用截至该修订的全部事件，`Bookmark()`
   返回该水位。书签表示此前整批变化都已被该订阅者确认。
5. **有界队列溢出**：订阅者队列容量固定，提交扇出采用非阻塞写入；
   装不下立即把订阅者标记为 stale 并换队列世代，不阻塞提交。下次 `Recv`
   时用「最新筛选快照 vs 已确认镜像」做差量对账：新增发 Put、消失发
   带旧值 Delete、同名 UID 不同发 Delete+Put，末尾补发当前修订号书签。
6. **镜像压缩 → 强制重列**：`CompactMirror()` 模拟本地已确认镜像被
   清理，此后 `Reconnect`/对账直接返回 `ErrRevisionTooOld`，调用方必须
   重新全量列取，缓存不会猜测一个可能错误的镜像。
7. **断线恢复**：`Disconnect()` 丢弃所有未确认投递但保留书签与镜像；
   `Reconnect()` 优先从保留历史逐批回放，历史已不覆盖书签修订号时
   退化为差量对账，镜像也不可用时返回重列错误。

## 公开 API

| 符号 | 说明 |
| --- | --- |
| `New(Config) *Cache` | 构造缓存；`HistoryBatches`/`DefaultPageSize`/`DefaultQueueDepth` 零值回落默认（32/10/16） |
| `Cache.Commit(Batch) error` | 原子提交一批变化；修订号非递增返回 `ErrStaleRevision` |
| `Cache.List(ListOptions) (ListResult, error)` | 标签分页查询，令牌固定快照修订号 |
| `Cache.SnapshotAt(rev, sel) ([]Object, error)` | 任意保留修订号的完整查询，用于与订阅者镜像做一致性比对 |
| `Cache.Watch(WatchOptions) (Watcher, error)` | 创建订阅者，初始 backlog 为当前筛选快照 + 当前修订号书签 |
| `Cache.CurrentRevision() Revision` | 当前修订号 |
| `Watcher.Recv(ctx) (Event, error)` | 读取下一条事件；stale 时先就地对账；断线后阻塞到重连/关闭 |
| `Watcher.Ack(rev) error` | 确认整批，推进镜像；非法修订号返回 `ErrInvalidAck` |
| `Watcher.Bookmark() Revision` | 已确认修订号（0 = 未确认任何批次） |
| `Watcher.Mirror() []Object` | 已确认镜像的深拷贝（按名排序） |
| `Watcher.Disconnect()` / `Reconnect(ctx)` | 模拟断线 / 从书签续接 |
| `Watcher.CompactMirror() error` | 仅断线态可调用，之后重连必得 `ErrRevisionTooOld` |
| `Watcher.Close() error` | 释放订阅者 |

数据类型：`Object{Name, UID, Labels, Value}`（`Clone()` 深拷贝）、
`Change{Object, Deleted}`、`Batch`、`Event{Type, Revision, Object}`
（`EventPut`/`EventDelete`/`EventBookmark`）、`LabelSelector`（等值匹配）、
`ListOptions`/`ListResult`、`WatchOptions`。

哨兵错误：`ErrStaleRevision`、`ErrRevisionTooOld`（需重列）、
`ErrInvalidContinuation`、`ErrInvalidAck`、`ErrWatcherClosed`、
`ErrActiveWatcher`，均可用 `errors.Is` 判断。

## 复现步骤

```bash
go version          # 要求 go1.20.2（本工作区实测版本）
go test ./...       # 功能测试
go test -race ./... # 带竞态检测的同一组测试
go test -run TestHalfBatchDisconnectResume -v .   # 单独跑某个场景
```

无外部依赖，`go.mod` 只声明 module 与 go 版本；所有数据均为测试内
合成数据，不访问网络、磁盘账户或真实系统目标。

## 验证范围

| 测试 | 类型 | 覆盖的题面要求 |
| --- | --- | --- |
| `TestPaginationPinnedAcrossSameNameRebuild` | 交错 | 分页中途同名删除并以新 UID 重建；续翻仍读固定修订号快照，新查询才见重建结果 |
| `TestSameRevisionLabelFlipsFoldToNetTransition` | 交错 | 同一修订号内多次改标按净状态折叠；进入发 Put、离开发带旧值 Delete、横跳无事件 |
| `TestHalfBatchDisconnectResume` | 故障恢复 | 可暂停读取器只消费半批即断线；重连从书签整批重放，镜像与 `SnapshotAt` 完整查询一致且无重复投递 |
| `TestQueueOverflowDeltaReconcile` | 故障恢复/边界 | 有界队列溢出后差量对账；同名换 UID 发旧值 Delete + 新值 Put |
| `TestHistoryCompactedReconcileAndMirrorRelist` | 故障恢复/边界 | 历史被压缩走差量；已确认镜像也被压缩时明确返回 `ErrRevisionTooOld` 要求重列 |
| `TestExpiredContinuationRequiresRelist` | 边界 | 过期分页令牌要求重列；选择器不匹配/乱码令牌返回 `ErrInvalidContinuation` |
| `TestSnapshotAtAcrossBatches` | 正常/边界 | 多批次同对象、删除重建、窗口外不变对象的时间旅行快照逐修订正确 |
| `TestCommitOrderingAndAckSemantics` | 正常/边界 | 修订号单调校验、非法/重复 Ack、断线态 Recv 阻塞与重连恢复 |
| `TestConcurrentCommitsAndWatchers` | 并发交错 | 多订阅者 + 高频随机提交（含换 UID/改标/删除），race 检测下每个追平后的镜像等于完整查询 |
| `TestDefensiveCopies` | 边界 | 入参/返回值深拷贝隔离，外部变更不污染缓存 |

所有断言均针对真实运行结果（事件序列、修订号、镜像内容与
`SnapshotAt` 查询结果的逐项比对），没有使用预置输出或演示脚本替代。

## 设计要点与限制

- 锁序固定为 `Cache.mu → watcher.mu`；`Recv` 检测到 stale 时先释放
  watcher 锁再取缓存锁对账，避免反向加锁。
- 历史快照通过「保留窗口基线 + 正向重放」重建，窗口外修订号一律显式
  报错而非返回近似结果。
- 队列溢出不阻塞提交路径，代价是订阅者恢复时丢失中间事件细节、
  只保证最终镜像与当前查询一致（K8s watch cache 同款取舍）。
- 标签选择器仅实现等值条件（`map[string]string` 语义），不支持
  集合运算；对象值 `Value` 为不透明字节串，缓存不解释内容。
- 历史批次与镜像都在进程内存中，进程重启等价于镜像被压缩，调用方
  需走全量重列。
