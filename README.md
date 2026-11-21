# partitionlog — 按偏移读取的事务性分区日志

本地内存实现的消息代理单分区日志组件（Go 1.20.2，仅依赖标准库），
满足以下语义：

- **普通消息立即提交**：`Append` 成功即占用连续偏移并对读取可见。
- **事务性追加**：`Begin` → 多次 `AppendTxn` → `Commit`/`Abort`，事务消息
  在结束前占用偏移但不可读。
- **LSO 读取截断**：读取上界为最早未结束事务的第一条消息偏移（LSO）。
  即使读取起点在该消息之后也会被截断，事务之后的普通消息同样等待。
- **撤销不压缩偏移**：撤销消息与撤销/提交控制标记都保留在原偏移上，读取时
  过滤但不返回；返回的 `NextOffset` 是过滤之后的真正下一读取位置，跨越多条
  连续已撤销消息（甚至本次一条数据都不返回）也会正常推进，客户端不会卡死。
- **代次接管（fencing）**：`Takeover` 先撤销该生产者全部遗留事务再抬升代次；
  旧代次之后的迟到提交、撤销、追加、开启事务全部返回 `ErrFenced` 且不写入。
- **小页串联一致性**：按任意页大小（含 1）分页连续读取的串联结果，与
  `maxRecords<=0` 一次读完的结果在消息体、偏移和最终游标上逐条一致。

## 入口与 API

包路径：`partitionlog`（见 `go.mod`，无第三方依赖）。核心类型与方法均在
`partition.go`：

| API | 作用 |
| --- | --- |
| `New() *Partition` | 创建空分区 |
| `Append(payload) int64` | 追加普通消息，立即提交，返回偏移 |
| `Begin(txnID, producer, epoch) error` | 开启事务（代次过期返回 `ErrFenced`，并发事务返回 `ErrTxnOpen`） |
| `AppendTxn(txnID, epoch, payload) (int64, error)` | 事务内追加 |
| `Commit(txnID, epoch) error` / `Abort(txnID, epoch) error` | 统一提交或撤销，写控制标记并结束事务 |
| `Takeover(producer, newEpoch) ([]string, error)` | 新代次接管：先撤销遗留事务，返回被撤销事务 ID |
| `Read(startOffset, maxRecords) (records, nextOffset, error)` | 按偏移读取已提交数据；`maxRecords<=0` 不限制条数 |
| `LastStableOffset() int64` / `LogEndOffset() int64` | 观测 LSO / LEO |

错误哨兵：`ErrUnknownTxn`、`ErrTxnOpen`、`ErrFenced`、`ErrBadOffset`
（配合 `errors.Is` 使用）。所有方法带互斥锁，可并发调用。

## 设计要点

- 日志是一个只追加的 `entries` 切片，切片下标即偏移，偏移永不复用。
- 条目分三类：数据、提交标记、撤销标记。控制标记占偏移但不对外返回。
- LSO = 所有未结束事务首条消息偏移的最小值；无消息事务不参与阻塞。
- 读取只扫描 `[startOffset, LSO)`，过滤撤销数据与控制标记，
  `NextOffset` 始终按物理偏移推进。
- 代次同时记录在生产者级（`epochs`）与事务级（`txnState.epoch`）；
  校验时代次检查先于事务状态检查，保证接管后的迟到请求一律判为 fencing。

## 验证范围与运行方式

测试全部在 `partition_test.go`，使用 Go 内置 `testing`，全部为真实断言
（无占位测试）。覆盖：

| 测试 | 覆盖的约束 |
| --- | --- |
| `TestImmediateAppendAndBasicRead` | 普通消息立即提交；页大小 1 分页与一次读完一致；末端重试；负偏移拒绝 |
| `TestOpenTransactionBlocksLSO` | **应被阻止的结果**：未结束事务在首条消息处截断 LSO，起点在其后也读不到，后续普通消息等待；提交后放行 |
| `TestAbortedMessagesKeepOffsetsAndAreSkipped` | 撤销消息占偏移不返回；**边界场景**：整段全撤销时 `NextOffset` 仍推进到真正位置，后续消息可继续消费 |
| `TestInterleavedTransactionsPaging` | **交错场景**：两个事务交错追加、先后结束（含一提交一撤销），页大小 1/2 串联与一次读完逐条一致 |
| `TestTakeoverFencesStaleEpoch` | 接管先撤销遗留事务；旧代次迟到提交/撤销/追加/Begin 全部 `ErrFenced` 且无写入；新代次正常工作 |
| `TestTakeoverWithoutOpenTransaction` | 无遗留事务的接管；低代次 Begin 被 fencing |
| `TestTransactionLifecycleErrors` | 未知/重复结束事务、并发事务等拒绝路径 |
| `TestEmptyTransactionDoesNotBlockLSO` | 无消息事务不阻塞普通消息可见性 |
| `TestPayloadDefensiveCopy` | 输入与返回字节切片的防御性拷贝 |

运行：

```sh
go test ./...            # 验收测试
go test -race ./...      # 竞态检测（本地已通过）
gofmt -l .               # 格式检查（本地无输出）
```

本地环境：darwin/arm64，go1.20.2。`go vet ./...` 在本次环境中未获执行
授权，因此未运行；`go test ./...` 与 `go test -race ./...` 均通过。

## 范围声明

本组件为纯本地、纯内存的基础设施功能组件，不涉及持久化、网络、外部目标
或真实消息负载；所有夹具均为测试内合成数据。
