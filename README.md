# sender-recovery

事件序列驱动的传输栈**发送侧恢复模块**。调用方按线上顺序投递事件（发送/重传、累计确认、
选择性确认、SACK 撤销、丢失标记、重复接收证据、超时、进入恢复期），模块在每个事件之后
返回下一段可重传范围、**按不同字节计**的在途字节数和当前拥塞窗口。

仅使用 Rust 1.95.0 标准库，无第三方依赖。

## 目录结构

| 文件 | 职责 |
| --- | --- |
| `src/seq.rs` | 32 位回绕序号与单调 64 位平面空间的互转（半圈判据）、跨回绕边界切段 |
| `src/intervals.rs` | 有序合并的半开区间集合，提供插入、删除、截断、并/交/差集与逐字节覆盖判定 |
| `src/recovery.rs` | 恢复状态机：恢复轮、尝试号证据、SACK 撤销、超时阻断、降窗撤回 |
| `tests/recovery_flow.rs` | 端到端事件序列集成测试（常规、边界、交错场景） |

## 核心语义

1. **序号空间**：线路上序号按 `u32` 模 `2^32` 回绕；内部一律展开为单调 `u64`。
   任何活跃窗口和任何单段长度必须小于半圈（`2^31` 字节），违反时返回
   `RecoveryError::RangeTooLarge` / `RecoveryError::InvalidCwnd`。
2. **在途字节**：已发送但既未被累计确认、也未被选择性确认的**不同字节**。
   被标记丢失、被撤销或被重传的字节在被确认前始终计入在途，重传不会重复计数。
3. **重传候选**：`(lost ∪ revoked) − sacked`，始终取最低序号的连续段；
   重传允许只覆盖原段的任意子区间（见 `limited_next_retransmit_flat`）。
4. **恢复轮**：`EnterRecovery` 保存进入时的拥塞窗口、未确认边界（`entry_una`）
   与恢复边界（进入时已发送的最高字节 `recovery_point`），并应用调用方声明的降窗。
   降窗必须严格小于当前窗口，轮号不可复用。
5. **尝试号证据**：首次发送固定为尝试号 `FIRST_ATTEMPT = 1`；每次重传必须使用全新
   尝试号。重复接收证据按尝试号记录，只与同一尝试号实际发送的字节取交集，因此
   **重叠区间的证据不能跨轮借用**。
6. **SACK 撤销**：`Revoke` 只对尚未累计确认的已 SACK 字节生效，撤销部分回到 `revoked`
   集合并重新成为待修复字节；再次 SACK 可将其移出修复队列。
7. **降窗撤回三条件（同时满足）**：
   - 本轮全部重传字节都有**匹配尝试号**的重复接收证据；
   - 累计确认已达到恢复边界（`una >= recovery_point`）；
   - 本轮活跃期间没有超时事件。
   嵌套恢复轮按 LIFO 撤回，每次撤回恢复该轮进入时保存的窗口。
8. **超时**：`Timeout` 给当前活跃轮打上粘性阻断标志，并把所有未 SACK 的在途字节
   标记为丢失。模块不自行改窗或开轮——降窗时机与轮次由调用方通过事件声明。

## 公开 API（摘要）

```rust
// 构造与事件投递
RecoverySender::new(isn: u32, initial_cwnd: u64) -> Result<Self, RecoveryError>
RecoverySender::apply(&mut self, event: Event) -> Result<Snapshot, RecoveryError>

// 事件
enum Event {
    Send(SendEvent),                 // 含 start/end/attempt
    CumAck(u32),
    Sack(Vec<WireRange>),
    Revoke(Vec<WireRange>),
    Loss(Vec<WireRange>),
    Dup(DupEvent),                   // 含 start/end/attempt
    Timeout,
    EnterRecovery { round: u32, reduced_cwnd: u64, reason: Reason },
}

// 每事件后的观测
struct Snapshot {
    una: u32,
    in_flight: u64,                  // 不同字节数
    cwnd: u64,
    next_retransmit: Vec<WireRange>, // 最低序号连续段，跨回绕点自动切段
    active_round: Option<u32>,
    rounds: BTreeMap<u32, RoundView>,
    attempts: BTreeMap<u32, AttemptView>,
    first_sent / sacked / lost / revoked: Vec<WireRange>,
}

// 分段方式可由调用方改变后逐字节核对
RecoverySender::next_retransmit_flat(&self) -> Option<(u64, u64)>
RecoverySender::limited_next_retransmit_flat(&self, max_bytes: u64) -> Option<(u64, u64)>
```

`WireRange { start, end }` 的 `end` 按模 `2^32` 解释；快照中的线路区间在跨回绕边界处
被切成不跨圈的多个 `WireRange`。需要平面空间逐字节核对时使用 `*_flat()` 访问器
（`flat_una`、`first_sent_flat`、`sacked_flat`、`lost_flat`、`revoked_flat`）。

错误类型见 `RecoveryError`：空区间、非法尝试号、尝试号复用、未知尝试号、无活跃轮、
字节不可重传、非连续首传、超出窗口、累计确认回退、非法窗口、轮号重复、窗口未实际下降。

## 复现步骤

```bash
# 工具链：rustc/cargo 1.95.0，离线、无第三方依赖
cargo test --offline
cargo build --offline --release
```

## 验证范围（真实测试，非演示预置输出）

`cargo test --offline` 共 **28 个测试**：12 个单元测试 + 16 个端到端事件序列测试。

### 单元测试（`src/`，12 个）
- 区间集合：相邻/重叠合并、间隙保留、删除切段、并交差集、累计确认截断；
- 序号回绕：半圈前后映射、跨圈区间展开、半圈违规拒绝、回绕点切段、空区间长度。

### 端到端测试（`tests/recovery_flow.rs`，16 个）

**常规路径**
- `full_fast_recovery_withdraws_window_reduction`：丢失 → 开轮降窗 → 分段重传 →
  部分证据不可撤回 → 跨边界确认 → 窗口恢复；
- `lowest_sequence_first_and_distinct_in_flight`：最低序号优先、重传不重复计在途；
- `snapshot_separates_send_classes`：首传/SACK/本轮实际重传字节分类可区分，
  累计确认只截断活动集、保留尝试历史。

**边界场景**
- `recovery_across_32bit_wraparound`：窗口跨 `2^32` 回绕点的发送、丢失、重传、证据、确认；
- `half_circle_limit_enforced`：半圈长度区间与窗口被拒绝；
- `boundary_must_be_reached_exactly_before_withdrawal`：差一个字节不撤回，下一事件即撤回；
- `zero_retransmission_round_withdraws_on_boundary`：零重传轮在跨边界时撤回；
- `partial_retransmission_leaves_tail_retransmittable`：只重传原段一部分，尾部仍待修复；
- `byte_exact_tracking_under_split_segments_and_partial_dups`：改变分段大小（7/7/6），
  逐字节到达的重复证据仍精确合并，错误尝试号的证据不计入。

**故障恢复与交错场景（验收条件）**
- `timeout_blocks_withdrawal_and_marks_outstanding_lost`：超时排除 SACK 字节后
  标记全部在途字节为丢失，并永久阻断本轮撤回；
- `timeout_then_repair_remains_blocked_from_withdrawal`：超时后再修复、证据齐全
  且跨边界，窗口仍不恢复（粘性阻断）；
- `nested_rounds_keep_attempt_evidence_separate`：嵌套轮 LIFO 撤回、逐级恢复窗口；
- `overlapping_rounds_never_borrow_attempt_evidence`：相同字节区间在不同轮重传时，
  他轮尝试号的证据不能满足本轮，必须拿到本轮尝试号证据才撤回；
- `revoked_sack_returns_to_repair_and_clears_on_reack`：撤销恢复为待修复、重传后
  再 SACK 不产生证据；
- `interleaved_sack_revoke_loss_reconciles_byte_sets`：SACK/撤销/丢失/再 SACK/累计
  确认在重叠区间上交错，字节集合与在途计数逐字节一致；
- `invalid_events_are_rejected`：全部非法输入分支返回对应错误且不破坏状态。

## 设计取舍与边界说明

- 超时事件只负责“标记丢失 + 阻断撤回”；是否开轮、降到多少窗口由调用方用
  `EnterRecovery` 显式声明，符合“降窗时机由事件指定”。
- 没有任何重传字节的恢复轮，其“全部重传字节均有证据”条件平凡成立，跨边界即可撤回；
  该行为由 `zero_retransmission_round_withdraws_on_boundary` 锁定。
- 已撤回的轮保留在 `Snapshot.rounds` 历史中（`withdrawn = true`），用于事后核对；
  `withdrawable` 只对当前最内层且条件齐备的活跃轮为真。
- 所有输入均为调用方在本地合成的序号与事件；模块不执行任何网络、文件或系统访问。
