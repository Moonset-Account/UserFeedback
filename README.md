# seatledger — 离线分支商业软件席位授权簿

总部预先把总并发上限分配给各分支，离线分支只能从**本地可用份额**签出席位；
分支之间通过**冻结 → 验证入账 → 确认**的三段式协议转拨席位，全程可持久化、可恢复、可审计。

## 实际入口

| 文件 | 内容 |
|---|---|
| `ledger.go` | 核心授权簿 `Ledger`：签出/签还、转拨发起/接收/拒绝/确认、超时保护、快照 |
| `store.go` | 事件日志 `Store`（JSONL 追加 + fsync）与 `ReadPrefix` 前缀恢复（容忍撕裂尾行） |
| `audit.go` | 全局审计 `Audit`：展开所有分支，逐份解释席位状态并校验总量守恒 |
| `ledger_test.go` | 验收测试（Go 内置 `testing`，全部真实断言） |

关键 API：

- `Open(name, key, store)` — 打开/恢复分支账本（`store` 为 nil 时纯内存）
- `Allocate / Checkout / Checkin` — 总部发行、离线签出/签还（仅动用本地可用份额）
- `InitiateTransfer(to, n)` — 来源端**先冻结**份额并签发唯一 `TransferRecord`（HMAC-SHA256 签名）
- `ReceiveTransfer(rec, keys)` — 接收端**验证来源签名**后入账，返回签名 `Ack`；重复投递幂等
- `RejectTransfer(rec, keys)` / `ApplyRejection(rej, keys)` — 接收端拒绝，来源端凭签名拒绝书解冻
- `ConfirmTransfer(ack, keys)` — 来源端凭回执确认，份额永久转出；重复回执幂等
- `AckFor / RejectionFor` — 确认丢失时**重送**回执/拒绝书
- `AbortTransfer(id)` — 显式拒绝单方超时撤销：冻结份额只能由对端签名的 Ack/Rejection 了结，
  保证两端永不同时拥有同一份席位
- `Audit(ledgers...)` — 展开全部分支：每份席位归于 使用中 / 可用 / 在途（含"已入账待确认"
  交叉引用），`OK` 当且仅当解释总量等于初始发行量

## 运行验证

```sh
go test ./...
```

## 验证范围（测试 ↔ 题面约束）

| 测试 | 覆盖约束 |
|---|---|
| `TestCheckoutRespectsLocalShare` | 可接受结果：离线签出仅用本地份额；边界：签出恰好全部可用份额；越界 1 份即拒绝 |
| `TestTransferLifecycle` | 可接受结果：完整转拨生命周期；冻结中、已入账待确认、确认后各阶段总量守恒 |
| `TestTransferBoundaryAndFrozenNotCheckoutable` | 边界/交错：冻结恰好全部可用份额；超额/零额转拨拒绝；冻结份额不可签出 |
| `TestDuplicateAndOutOfOrderExchange` | 重复与乱序：记录乱序到达、重复投递只入账一次、回执乱序/重复确认幂等、确认丢失重送 |
| `TestForgedAndTamperedRecordsRejected` | 应被阻止：篡改数量、伪造签名、错投他端、伪造回执、无密钥验证——均拒绝且状态不变 |
| `TestTimeoutCannotDoubleOwnSeats` | 交错场景：来源端等待超时后单方撤销被拒、冻结份额不可动用；迟到记录仍只入账一次，两端不同时持有 |
| `TestRejectionReturnsSeatsExactlyOnce` | 拒绝路径：接收端拒绝后来源端解冻恰好一次；已拒绝记录永不可再入账 |
| `TestRecoveryFromPersistedPrefix` | 任一端从已落盘事件前缀恢复，恢复后继续交换重复记录无双重效果，重送回执完成转拨 |
| `TestRecoveryIgnoresTornTail` | 崩溃撕裂的尾行被忽略，仅回放完整前缀，恢复后账本可继续工作 |
| `TestGlobalAuditExplainsEverySeat` | 三分支下席位同时处于使用中/可用/在途/已入账待确认，逐份解释且总量等于初始发行量 |

## 设计要点

- **无双重占有**：转拨一旦发起，来源端份额即冻结且不可单方回收；只有接收端签名的
  `Ack`（确认转出）或 `Rejection`（解冻退回）能了结在途份额。等待超时不产生任何状态变更。
- **可验证记录**：`TransferRecord`/`Ack`/`Rejection` 均用签发方密钥做 HMAC-SHA256，
  接收方经 `KeyRegistry` 验签，篡改、伪造、错投一律拒绝。
- **幂等交换**：所有消息按唯一转拨 ID 去重，重复或乱序投递安全；确认丢失可由接收端
  从本地状态重签重送。
- **事件溯源恢复**：每次状态变更先落盘（JSONL + fsync）再应用；恢复时回放已落盘前缀，
  撕裂尾行自动截断。
- **总量守恒审计**：在途份额按"接收端是否已入账"归属唯一一端，全局展开时每份席位
  恰好被解释一次，总量恒等于总部初始发行量。
