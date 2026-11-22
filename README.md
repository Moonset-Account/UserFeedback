# 可靠传输发送端重传状态模块

为可靠传输发送端实现的纯状态机库，专门处理“旧包已经判丢并安排重传，
确认却在重传前赶到”这类交错。仅使用 **Rust 1.95.0 标准库**，无任何
第三方依赖，所有数据均为本地合成数据，不访问网络、账户或外部系统。

## 建模与不变量

- **两个独立包号空间**：[`SpaceId::Handshake`] 与 [`SpaceId::AppData`]
  各自编号包号、各自维护丢失时限与计时；两个空间可以同时使用相同包号，
  互不干扰。
- **流数据按字节区间标识**：[`StreamRange`] 是 `(流号, start, end)`
  半开区间；同一区间允许出现在多个（重传）承载包中。
- **在途只占用一次**：包发送时增加在途字节/包数，离开在途（在途确认或
  判丢）时只减少一次；判丢后的迟到确认**不再**扣减在途，也不影响其他
  在途包。
- **字节完成一次**：流字节在任意一个承载包被确认后即进入完成集合；
  后续其他承载包的确认、判丢都不改变这一事实，同一区间在待发计划中
  也只计费一次。
- **迟到确认撤重传**：判丢包的确认到达时，其各字节区间上**尚未发送**
  的重传计划被撤去（同时从所有空间的待发计划中删除已完成字节）。
- **关闭空间的边界**：[`Sender::discard_space`] 只清除该空间自身的包、
  在途占用与计时；存续空间完全不动。仍需在存续空间完成的数据由调用方
  在 `still_needed` 参数中明确列出，已完成部分自动剔除。
- **时钟单调**：模拟时钟只能前进，回退事件返回
  [`SenderError::TimeWentBackwards`]；校验失败的事件不推进时钟、不产生
  任何状态变更。

## 公开 API

| 类型 / 方法 | 作用 |
| --- | --- |
| `SpaceId` | 包号空间枚举：`Handshake` / `AppData`，`SpaceId::ALL` 给出规范顺序 |
| `StreamRange::new(stream_id, start, end)` | 构造半开字节区间并校验 |
| `IntervalSet` | 有序、同流不相交区间集合：插入合并、删除切分、并集、覆盖判定、字节计数 |
| `SentPacket` | 一条发送记录（空间、包号、发送时刻、在途字节、承载帧区间） |
| `AckRanges::new` / `from_pairs` | 按空间给出的确认包号闭区间，自动排序归并并校验 |
| `Sender::new(hs_loss, app_loss)` | 建立状态机，按空间设置丢失时限 |
| `Sender::on_sent(SentPacket)` | 录入发送；重传包发出时从待发计划扣除对应区间 |
| `Sender::on_ack(space, ack, t)` | 处理确认：在途确认离开在途；判丢包迟到确认撤重传但不动在途 |
| `Sender::on_timeout(t)` / `advance(t)` | 推进模拟时钟，对超时在途包判丢并生成重传计划 |
| `Sender::discard_space(space, t, still_needed)` | 关闭一个空间，仅撤销该空间，并转移调用方明确列出的未完成数据 |
| `Sender::next_timeout()` | 下次计时时刻（最早的判丢 deadline），无在途包时为 `None` |
| `Sender::pending_ranges(space)` / `all_pending_ranges()` | 待发区间（已完成字节自动剔除） |
| `Sender::completed_ranges()` | 已完成流字节集合 |
| `Sender::accounts()` | 按空间计费快照 [`SpaceAccount`]（在途、累计发送/确认/判丢/迟到确认、待发字节等） |
| `Sender::packet_status(space, pn)` | 单包生命周期状态 `InFlight/Lost/Acked/AckedAfterLoss` |
| `Event` + `Sender::handle` | 以事件流方式驱动，便于组织交错叙事 |
| `SenderError` | 非法区间、非法 ACK、重复包号、空间已关闭、时钟回退 |

判丢规则是简单的时间阈值：在途包在 `time_sent + 该空间 loss_time`
时刻之后仍在途即判丢；时限可通过 `set_loss_time` 动态调整，
`next_timeout` 随之重新计算。

## 目录结构

```
Cargo.toml              # 无 [dependencies]，零第三方依赖
rust-toolchain.toml     # 固定 channel = "1.95.0"
src/interval.rs         # StreamRange 与 IntervalSet（含 6 个单元测试）
src/lib.rs              # Sender 状态机、空间计费、事件与错误类型
tests/sender.rs         # 11 个集成测试，覆盖正常/边界/交错验收场景
examples/interleaving.rs# 判丢后确认迟到的可运行示例
```

## 复现步骤

```bash
# 工具链：rustc 1.95.0 / cargo 1.95.0（仓库内 rust-toolchain.toml 已固定）
cargo test --offline            # 17 个真实测试（6 单元 + 11 集成）
cargo test --offline --locked   # 与 Cargo.lock 一致的可重复离线构建
cargo run --offline --example interleaving
```

`Cargo.lock` 中只有本包自身，没有任何外部依赖；`--offline` 全程不联网。

## 验证范围（以真实测试为准）

集成测试 `tests/sender.rs`：

1. `two_spaces_same_number_half_ack_remaining_and_inflight` —— 题面指定核对：
   两个空间同号、半段先确认，逐步核对剩余待发字节与在途增减，再重传收尾。
2. `spurious_ack_cancels_unsent_retransmit_and_keeps_other_inflight` ——
   判丢后、重传未发时确认迟到：撤待发计划、其他在途包计数不变、重复确认幂等。
3. `spurious_ack_after_retransmit_sent_keeps_retransmit_inflight` ——
   重传已发后旧包确认才迟到：重传包继续在途且只扣一次。
4. `same_range_in_multiple_carriers_completes_once` —— 同一区间多个承载包：
   字节完成一次、待发只计费一次；单包迟到确认即撤整段计划。
5. `discard_space_clears_only_that_space_and_transfers_still_needed` ——
   关闭空间只清本空间，`still_needed` 转入存续空间，关闭后拒绝新包、忽略 ACK。
6. `discard_transfers_only_uncompleted_piece` —— `still_needed` 中已完成字节
   不转移，只转移未完成部分。
7. `per_space_loss_times_and_timer_updates` —— 按空间独立时限、动态调整后
   `next_timeout` 更新。
8. `ack_ranges_merge_unknown_numbers_ignored` —— ACK 区间归并、未知包号忽略、
   缺口包保留在途。
9. `validation_and_clock_monotonicity` —— 非法区间/ACK、重复包号、时钟回退、
   跨空间同号合法性。
10. `event_driven_full_interleaving` —— 事件流驱动的完整交错叙事。
11. `repeated_loss_partial_and_spurious_acks_keep_invariants` —— 多轮重传丢失、
    部分确认与迟到确认交织，逐步核对待发/完成/在途不变量与累计计费口径。

单元测试 `src/interval.rs` 覆盖区间合并、缺口与多流隔离、删除切分、
覆盖判定与非法区间。

## 明确未覆盖 / 未实现的部分

- 不实现网络 I/O、拥塞控制、PTO/探测包、帧打包与分包策略；本模块只负责
  发送端的记录、判丢、确认交错与计费状态。
- 判丢采用单一时间阈值，不建模包号阈值或抖动。
- 示例程序仅用于人工观察，功能正确性以 `cargo test` 的真实断言为准，
  不以打印输出替代测试。
