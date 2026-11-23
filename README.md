# msgdelivery — 延迟投递、租约续约与重投模块

一个纯标准库、无外部依赖的 Go 模块，实现消息代理的投递核心：

- 每条消息有**计划可见时间**（`VisibleAt`）、**最晚存活时间**（`ExpiresAt`）和**尝试上限**（`MaxAttempts`）。
- 同一队列按 **(可见时间, 首次入队顺序)** 领出。
- 领出生成带**尝试号**的回执（receipt）；**续约只能延长当前租期**，且不超过最晚存活时间。
- 租期到点后按**指数退避**重新可见、可再领。
- **旧回执即使先于新回执到达也不能确认新租期**（尝试号栅栏）。
- 存活时间与租期**同时到点时先到期**，转入终态只记一次。
- 提供**可重放持久化记录**（NDJSON 日志），支持两种崩溃窗口的恢复：
  1. 领出已记录但响应未发；
  2. 确认已记录但连接断开。
- 调用方可用**请求 ID 重取同一次领出结果**，并查询**已过期的回执**与**实际完成的尝试数**。

## 环境与约定

- 语言/工具链：**Go 1.20.2**（`go.mod` 声明 `go 1.20`），仅标准库。
- 时间模型：所有时间点均为调用方传入的**整数毫秒**，引擎内部不读取墙钟，因此行为完全可复现。
- 模块路径：`example.com/msgdelivery`，包名 `delivery`。

## 公开 API

| 符号 | 说明 |
| --- | --- |
| `NewEngine(Options) (*Engine, error)` | 创建引擎，并写入首条 `config` 日志记录。 |
| `NewEngineFromJournal([]Record) (*Engine, error)` | 从完整记录流重建引擎；校验序号、记录合法性与状态机不变量。 |
| `LoadJournal(path)` / `(*Engine).SaveJournal(path)` | 从文件读取 / 写入 NDJSON 日志（权限 `0600`）。 |
| `(*Engine).Enqueue(MessageSpec, now) error` | 入队；拒绝重复 ID、非法字段、以及入队时已过期。 |
| `(*Engine).Claim(queue, requestID, now) (Receipt, error)` | 领出下一条消息；**相同 `requestID` 返回原始回执**（幂等重取）。 |
| `(*Engine).Renew(receiptID, extend, now) (Receipt, error)` | 延长当前租期，封顶于 `ExpiresAt`；不缩短、不复活已失效租期。 |
| `(*Engine).Ack(receiptID, requestID, now) (AckResult, error)` | 确认；按尝试号栅栏；相同 `requestID` 幂等，重放后仍可重试。 |
| `(*Engine).Sweep(now)` | 将全部消息推进到 `now` 的有效状态，终态转换只记录一次。 |
| `(*Engine).Message(id, now) (MessageView, bool)` | 查询消息有效视图（含 `CompletedAttempts`）。 |
| `(*Engine).Receipt(id, now) (ReceiptView, bool)` | 按回执 ID 查询，含 `Status`/`Expired`/`Superseded`。 |
| `(*Engine).LookupClaim(requestID) (Receipt, bool)` | 纯查询：按请求 ID 重取领出结果，不推进时间。 |
| `(*Engine).LookupAck(requestID) (AckResult, bool)` | 纯查询：按请求 ID 重取确认结果。 |
| `(*Engine).Journal() []Record` | 返回日志深拷贝。 |
| `EncodeJournal` / `DecodeJournal` | NDJSON 编解码。 |

关键类型：`Options`（`LeaseDuration`/`BackoffBase`/`BackoffMax`）、`MessageSpec`、`Receipt`、`AckResult`、`ReceiptView`、`MessageView`、`Record`、`State`（`pending`/`leased`/`acked`/`expired`/`dead`）。

哨兵错误（用 `errors.Is` 判断）：`ErrInvalidOptions`、`ErrInvalidMessage`、`ErrInvalidRequest`、`ErrInvalidExtension`、`ErrDuplicateMessage`、`ErrMessageExpired`、`ErrMessageDead`、`ErrMessageAcked`、`ErrNoMessage`、`ErrUnknownReceipt`、`ErrStaleReceipt`、`ErrLeaseExpired`、`ErrRequestConflict`、`ErrCorruptJournal`。

## 语义要点

- **领出顺序**：仅比较所选队列；先按 `visibleAt` 升序，相同时按首次入队序号升序。不同队列互不影响。
- **回执**：`Receipt{ReceiptID, MessageID, Queue, Attempt, IssuedAt, LeaseUntil}`，`Attempt` 从 1 递增。
- **续约**：新租期 = `min(旧租期 + extend, ExpiresAt)`；若已封顶则为无操作（不报错、不缩短、不追加记录）。
- **租期失效**：`now >= LeaseUntil` 且未到 `ExpiresAt` 时，完成一次尝试（`CompletedAttempts += 1`）；若已达尝试上限进入 `dead`，否则按 `backoff(attempt)` 计算新可见时间：`LeaseUntil + backoff(attempt)`，其中 `backoff` 为自 `BackoffBase` 起每级翻倍、封顶 `BackoffMax`。
- **栅栏**：确认与续约都要求 `receipt.Attempt == 当前尝试号` 且是该消息的当前回执；否则返回 `ErrStaleReceipt`。旧回执即使先到也只会失败，不会改变新租期。
- **同时到点**：`Sweep` 对每条消息先判 `now >= ExpiresAt`，因此存活时间优先于租期结束。终态消息不再参与后续 sweep，故 `expired`/`dead`/`acked` 只会产生一条终态记录。
- **幂等**：`Claim` 按 `requestID` 去重；`Ack` 按 `requestID` 去重，且对已确认消息的直接重复确认也返回原始结果。
- **持久化与恢复**：每次状态转换先追加 `Record`（自增 `Seq`）再在内存应用；`NewEngineFromJournal` 逐条重放并校验。日志首条必须是 `config`。

## 复现步骤

在空工作区中：

```sh
# 仅使用本机 Go 1.20.2 与标准库，不安装依赖、不访问网络
go build ./...
go test ./...
go test -race -count=1 ./...
```

## 测试覆盖与验证范围

测试文件 `delivery_test.go`，共 21 个测试，全部基于真实调用与断言（无预置输出替代）。已实际执行：`go test ./...`、`go test -race -count=1 ./...`，全部 PASS。

| 测试 | 覆盖场景 |
| --- | --- |
| `TestNewEngineRejectsInvalidOptions` | 非法租期 / 退避参数。 |
| `TestEnqueueValidation` | 空字段、零尝试、可见时间不早于存活时间、重复 ID、入队即过期。 |
| `TestClaimOrderingUsesVisibleAtThenEnqueueOrder` | 跨队列隔离；可见时间排序 + 入队顺序打破平局；未到可见时间不可领。 |
| `TestClaimReceiptCarriesAttemptNumber` | 回执字段与尝试号；租期中不可重复领出；请求 ID 重取。 |
| `TestRenewExtendsAndCapsAtDeadline` | 续约延长、封顶存活时间、封顶后无操作、未知回执、零延长。 |
| `TestRenewRejectsLapsedLeaseAndExpiredMessage` | 租期到点续约失败；被新回执取代后失败；过期后失败。 |
| `TestLeaseLapseBackoffRedeliveryAndAttemptLimit` | 精确退避时间、旧回执过期、重投尝试号递增、尝试上限 → `dead`，记录条数。 |
| `TestOldReceiptCannotAckOrRenewNewLease` | **旧回执先到**仍被 `ErrStaleReceipt` 拒绝，新租期不被确认；新回执正常确认，完成尝试数为 2。 |
| `TestStaleAckBeforeAnyReclaimFailsOnlyByLease` | 租期失效但尚未重领时，旧确认仅因租期失效失败，消息回到 pending。 |
| `TestDeadlineWinsWhenItCoincidesWithLeaseEndAndExpiresOnce` | **存活时间与租期同刻到点先到期**；反复 sweep 只产生一条 `expire`；到期后确认/领出均失败。 |
| `TestNeverLeasedMessageExpiresOnce` | 从未领出的消息到期且只记一次。 |
| `TestClaimRequestIDReturnsSameReceiptAndDetectsConflicts` | 同请求 ID 返回同一回执（含租期失效后），不新增领出；跨队列冲突。 |
| `TestRecoverWhenClaimRecordedButResponseLost` | **领出已记录、响应未发**：重放后同请求 ID 重取同一回执，无新增领出，租期行为正常。 |
| `TestRecoverWhenAckRecordedButConnectionDropped` | **确认已记录、连接断开**：`LookupAck` 重取结果，重复确认幂等且不追加记录，终态正确，未知回执失败。 |
| `TestAckRequestIDConflictAndExpiredAck` | 确认请求 ID 冲突；到期后确认失败。 |
| `TestExpiredReceiptsAndCompletedAttemptsAreObservable` | 多次重投后三张回执均可查（过期/被取代/已确认），实际完成尝试数为 3。 |
| `TestJournalReplaysToIdenticalState` | 日志序号连续、config 打头；重放后消息视图与日志逐字节一致，二次重放确定性。 |
| `TestJournalFilePersistence` | 文件保存 / 加载，权限与内容非空，重放等价。 |
| `TestCorruptJournalsAreRejected` | 空日志、坏 JSON、缺 config、序号错乱、未知记录、租期后确认、重复终态均被拒绝。 |
| `TestConcurrentClaimsYieldDistinctLeases` | 8 并发工作协程领取 30 条消息，无重复租约、每消息恰好领出与确认一次。 |
| `TestConcurrentSameRequestIDIsIdempotent` | 16 并发同请求 ID 领出 / 确认，结果一致且只记录一次。 |

### 明确未覆盖（未执行）

- 真实网络、真实时钟、进程级崩溃点注入（本模块刻意不读墙钟，崩溃窗口用"日志已落盘 + 新引擎重放"等价模拟）。
- 多进程 / 多节点共享日志的文件锁与并发写（`SaveJournal` 为整文件覆盖写，仅面向单写者）。
- 日志压缩 / 快照与超大日志的流式读取。

## 目录

```
go.mod
delivery.go       实现
delivery_test.go  测试
README.md
```
