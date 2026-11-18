# logrot — 固定容量本地审计日志轮转模块

终端分配给本地审计日志的空间固定（`Config.CapacityBytes`）。断线期间部分安全事件可能尚未确认归档，本模块用本地分段接收新事件与归档确认，并在空间不足时按保留策略轮转清理。

## 实际入口

模块 `logrot`（Go 1.20），源码 `logrot.go`，测试 `logrot_test.go`。

| 入口 | 作用 |
| --- | --- |
| `New(Config, now func() time.Time) *Store` | 创建存储；时钟可注入，便于确定性测试 |
| `(*Store).Append(Event) error` | 接收新事件；超段阈值先轮转，容量不足先回收再返回可重试的 `*NoSpaceError`（`Retryable() == true`）；事件超过段上限返回不可重试的 `ErrEventTooLarge` |
| `(*Store).ConfirmArchive(segID, digest string, eventIDs ...string) error` | 归档确认，**绑定段摘要**；摘要不匹配返回 `ErrDigestMismatch` 且不确认任何事件 |
| `(*Store).Resegment(segID string, splitAt int) ([]string, error)` | 段内事件重新分段；新段生成新摘要，旧确认（含已记录的确认）不沿用 |
| `(*Store).Cleanup(now) []string` | 清理可删除段：无未确认安全事件且保留期（`Config.Retention`）已结束；普通（无安全事件）过期段优先 |
| `(*Store).Snapshot() Snapshot` / `Recover(Config, now, Snapshot) *Store` | 崩溃快照与恢复；按日志（create/commit/activate/seal/pending_delete/delete）区分已提交新段与待删除旧段 |

保留规则：
- 含**未确认安全事件**的段永不清理；
- 已确认事件只有在**保留期结束后**才随段清理（边界：`now == SealedAt + Retention` 可清理）；
- 空间不足回收时，**普通过期段优先**于含已确认安全事件的段。

恢复规则（模拟轮转中断）：
- 日志有 create 但无 commit 的段 → 丢弃；
- 最后一条 activate 记录指向的段成为活跃段；已提交但未封存的老活跃段 → 恢复时封存；
- 日志记录为 pending_delete 的段 → 完成删除。

## 验证范围

`go test ./...`（全部真实断言，白盒测试位于同包）：

1. `TestAppendConfirmCleanupAccepted` — 可接受路径：接收→轮转封存→按摘要确认→保留期结束清理；含保留期边界（差 1ns 不清理，恰到期清理）。
2. `TestConfirmWithWrongDigestRejected` — 应被拒绝路径：错误摘要确认返回 `ErrDigestMismatch` 且不生效，未确认安全事件使段超保留期仍保留；未封存段/不存在段的确认亦被拒绝。
3. `TestResegmentInvalidatesOldConfirmation` — 交错场景：重新分段后旧摘要确认被拒绝、旧段确认不沿用，须按新摘要重新确认后才可清理。
4. `TestNoSpaceRetryableUntilConfirmedAndRetained` — 交错场景：未确认安全事件导致容量不足，返回可重试 `*NoSpaceError`；确认后保留期未满仍拒绝，到期后重试成功并腾挪空间。
5. `TestOrdinaryExpiredReclaimedFirst` — 回收顺序：普通过期段先于（更老的）含已确认安全事件的段被腾挪。
6. `TestCapacityBoundary` — 容量边界：恰好填满接受，超 1 字节可重试拒绝，超段上限不可重试拒绝。
7. `TestRecoverInterruptedRotation` — 轮转在 activate 后、seal 前中断：恢复保留已提交新段为活跃段并封存旧段，恢复后可继续写入。
8. `TestRecoverInterruptedDelete` — 删除在 pending_delete 后中断：恢复完成删除，已提交活跃段不受影响。
9. `TestRecoverUncommittedSegmentDropped` — 提交前中断：未提交段被丢弃，活跃指针回退，存储继续可用。

注：`go vet ./...` 未在本次环境中获批执行；编译与全部断言由 `go test ./...` 验证通过。
