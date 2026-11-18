# dedup — 加密隧道密钥换代期间的抗重放去重模块

面向隧道接收端：密钥换代重叠期内会同时收到两代均已通过完整性校验的报文，
本模块按 **(安全关联 SA, 密钥代次 generation)** 各维护一个有界滑动序号窗口，
对报文做首次判定。

## 语义

- 窗口内首次出现的乱序报文：**接受**（`Accept` 返回 `true`）。
- 已接收过的报文（重放/重复）：**拒绝**。
- 序号落后窗口（`highest - seq >= WindowSize`）的报文：**拒绝**。
- `Rotate` 之后，旧代在配置的 `Overlap` 重叠期内仍可推进自己的窗口；
  到达重叠截止时刻（含）起旧代报文一律拒绝。两代窗口完全独立，
  同一序号在两代中分别判定，互不影响。
- 所有方法并发安全：两个线程提交同一报文时仅一个观察到接受。
- `Snapshot`/`Restore` 可导出并重载窗口状态；重载后仍能拒绝快照前
  已处理的序号，并继续执行旧代的重叠截止期限。

## 实际入口

源码：`dedup.go`（包 `dedup`，模块 `dedup`，无第三方依赖）。

| API | 作用 |
| --- | --- |
| `NewFilter(Config{WindowSize, Overlap, Now})` | 构建过滤器；`Now` 可注入时钟便于测试 |
| `AddSA(sa, gen)` | 注册安全关联及其初始密钥代次 |
| `Rotate(sa, newGen)` | 换代；旧代进入重叠期，新代使用独立新窗口 |
| `Accept(sa, gen, seq) bool` | 核心判定：首次且在窗口内且在代次有效期内才返回 `true` |
| `Received(sa, gen, seq) bool` | 只读查询窗口内某序号是否已记录 |
| `Snapshot()` / `Restore(cfg, snap)` | 状态导出 / 重载（深拷贝，含重叠截止时间） |

## 验证范围

测试入口：`dedup_test.go`，使用 Go 内置 `testing`，执行真实断言。

```
go test ./... -count=1 -v
go test ./... -count=1 -race   # 并发场景经 race detector 验证
```

覆盖的交叉约束（12 个用例全部通过）：

- **可接受结果**：顺序报文、窗口内首次乱序报文、大跳变后的新报文
  （`TestAcceptsFirstTimeInOrderAndOutOfOrder`、`TestLargeJumpClearsWindow`）。
- **应被拒绝结果**：重复报文、未知 SA/代次、重叠期外旧代报文
  （`TestRejectsDuplicate`、`TestUnknownSAAndGenerationRejected`、
  `TestOldGenerationBoundedByOverlap`）。
- **边界/交错场景**：
  - 窗口最老边缘（`diff == size-1` 接受、`diff == size` 拒绝）及窗口
    滑动后原窗口内序号被挤出（`TestWindowBoundary`）；
  - 重叠期截止时刻恰好到期即拒绝、新代不受旧代过期影响、同一序号
    在两代独立判定（`TestOldGenerationBoundedByOverlap`）；
  - 32 线程提交同一报文仅 1 次接受、8 线程交错重复提交 500 个序号
    恰好各接受一次（`TestConcurrentSamePacketSingleAccept`、
    `TestConcurrentInterleavedDuplicates`，均经 `-race`）；
  - 快照重载后仍拒绝已处理序号、窗口边界与旧代重叠期限保持有效、
    快照为深拷贝不受后续流量污染（`TestSnapshotRestoreStillRejectsProcessed`、
    `TestSnapshotIsDeepCopy`、`TestRestoreWindowSizeMismatch`）。
