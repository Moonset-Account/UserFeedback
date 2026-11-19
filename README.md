# 句柄表（handletable）

通过小整数句柄访问私有对象的并发安全句柄表。句柄由 **槽号 + 代数 +
对象类别** 打包为一个 `uint64`，槽号位于最低位，保证句柄是紧凑的小
整数；调用方不持有对象指针，只能通过短时借用访问对象。

## 入口与文件

| 文件 | 作用 |
| --- | --- |
| `go.mod` | 模块 `handletable`，Go 1.20 |
| `handletable.go` | 句柄表全部实现，无第三方依赖 |
| `handletable_test.go` | Go 内置 `testing` 验收用例（真实断言） |

### 对外 API（实际入口）

- `New(slotCount int, genBits uint) (*Table, error)`：建表，`genBits`
  测试中允许设为 2。
- `(*Table).Register(obj any, kind uint8) (Handle, error)`：登记对象，
  返回句柄。
- `(*Table).Borrow(h Handle, wantKind uint8) (any, func(), error)`：
  按类别借用对象，返回对象与幂等的 `release` 归还函数；另有
  `(*Table).Return(h)` 显式归还。
- `(*Table).Close(h Handle) error`：关闭单个对象。
- `(*Table).CloseTable()`：关闭整张表。
- `(*Table).Encode / Decode`：句柄字段打包/拆解；`Inspect` 提供槽
  状态只读快照（主要供测试与诊断）。

### 可区分的结果（哨兵错误，用 `errors.Is` 判定）

| 错误 | 含义 | 性质 |
| --- | --- | --- |
| `ErrStale` | 句柄过期：槽号越界、代数不匹配或槽已封存 | 拒绝 |
| `ErrWrongKind` | 句柄有效但对象类别不匹配 | 拒绝 |
| `ErrClosed` | 对象或整表已关闭，新借用被拒绝 | 拒绝 |
| `ErrNoSlot` | 暂时没有可用槽（均被占用/排空），归还后可恢复 | 暂时 |
| `ErrExhausted` | 所有槽均已封存，表永久耗尽 | 永久 |
| `ErrNotBorrowed` | 归还一个当前未借用的槽 | 拒绝 |

## 关键语义（对应题面交叉约束）

1. **关闭即拒绝新借用**：`Close` 后新 `Borrow` 立即得到 `ErrClosed`；
   已经开始的借用不受影响，可正常完成。
2. **最后一个借用归还前禁止复用**：仍有在途借用时槽进入 `draining`
   状态，期间注册返回 `ErrNoSlot`；最后一次归还后才回收为 `free`。
3. **代数回绕封存**：每次槽复用代数按模 `2^genBits` 递增；若下一次
   复用将回绕到该槽曾经发出过的代数（两位位宽下即 gen 3 → 0），该槽
   永久封存（`sealed`）。
4. **优先使用其他槽**：单个槽封存不影响其他槽，注册时先选全新槽、
   再选可回收槽；只有全部槽封存才返回 `ErrExhausted`。
5. **过期/类别/无槽可区分**：解析顺序为 先判过期，再判类别，最后判
   关闭；`ErrNoSlot`（暂时性）与 `ErrExhausted`（永久性）严格分离。

## 验证范围与命令

```
go test ./...        # 全部 12 个验收用例
go test -race ./...  # 竞态检测（已通过）
gofmt -l .           # 格式检查（无输出）
```

用例覆盖三类场景：

- **可接受结果**：`TestAcceptableBorrowLifecycle`（登记→借用→归还→
  关闭完整链路）、`TestHandlesAreSmallIntegers`（小整数句柄）。
- **应被阻止/拒绝**：`TestWrongKindRejected`（类别错误）、
  `TestStaleHandleRejected`（越界/旧代数/关闭后句柄）、
  `TestErrorsAreDistinguishable`（六个哨兵错误互不匹配）、
  `TestReleaseIdempotent`、`TestNewValidation`。
- **边界与交错**：
  - `TestTemporaryNoSlotThenRecovery`：暂时无槽与恢复；
  - `TestCloseDrainingBlocksReuseUntilLastReturn`：关闭排空、在途借用
    可完成、最后归还前禁止复用；
  - `TestSealOnGenerationWrapTwoBits`：两位代数回绕封存与耗尽；
  - `TestSealPrefersOtherSlots`：封存时优先使用其他槽，且封存+占用
    交错时返回暂时性 `ErrNoSlot` 而非耗尽；
  - `TestCloseTableRejectsNewWork`：整表关闭拒绝新工作、在途借用仍可
    归还。

本地 Go 版本：go1.20.2 darwin/arm64。全部测试与竞态检测通过。
