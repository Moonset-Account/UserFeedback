# io-slot-runtime

异步输入输出运行时模块：以「槽位 + 代次」管理请求生命周期，并对借给消费者的缓冲做精确的容量核算。Rust 1.95.0，仅标准库，无第三方依赖。

## 生命周期模型

- 请求占用可复用的**槽位（slot）**，每次获取槽位时**代次（generation）**递增，句柄 `RequestHandle { slot, generation }` 可核对。
- 一次请求可产生**多段完成**：`complete` 每段把一个缓冲借给消费者（`Event::Completion`，容量 −1）。
- **取消回执可能先于最终完成**：`cancel` 之后槽位进入 `Cancelled`，底层随后送回的缓冲被**回收**（`Event::Reclaimed`），不交给消费者、容量不变。
- 已交出的缓冲**只有消费者归还才释放**（`return_buffer` → `Event::Returned`，容量 +1）。重复的完成、归还、取消、最终完成一律忽略（`Event::DuplicateIgnored`），不多加余额。
- 槽位只有在**最终完成已收到**（`finalize`）后才允许复用；此后旧代次事件（`Event::StaleIgnored`）不会推进占用该槽位的新请求。
- 容量变化可追溯到事件日志：容量只在 `Completion`（借出）时减少、在 `Returned`（实际归还）时增加。

## 公开 API

| 项 | 说明 |
| --- | --- |
| `IoRuntime::new(num_slots, num_buffers)` | 创建运行时；槽位全部空闲、代次为 0，缓冲全部可用 |
| `submit() -> Option<RequestHandle>` | 获取空闲槽位并递增代次；无空闲槽位返回 `None` |
| `complete(handle, buffer)` | 设备送达一段完成：Active 借出 / Cancelled 回收 / 旧代次或重复忽略 |
| `cancel(handle)` | 记录取消回执，Active → Cancelled |
| `finalize(handle)` | 最终完成，槽位转为 Free，允许复用 |
| `return_buffer(buffer)` | 消费者归还缓冲；仅实际借出的归还会增加容量 |
| `run_script(&[Step]) -> Vec<Option<RequestHandle>>` | 按输入脚本（各阶段与设备事件）顺序执行 |
| `available_capacity()` | 当前可用缓冲数 |
| `slot_state(slot)` / `slot_generation(slot)` | 查询槽位状态与当前代次 |
| `is_loaned(buffer)` | 查询缓冲是否借出 |
| `log() -> &[Event]` | 可见事件日志（完成、归还、回收、槽位获取等） |

类型：`RequestHandle`、`SlotState`（`Free`/`Active`/`Cancelled`）、`Event`、`Step`。

## 复现步骤

```sh
cargo test --offline
```

工具链：rustc/cargo 1.95.0。无第三方依赖，不需要网络。

## 验证范围（tests/lifecycle.rs，7 个测试，全部已实际执行）

- `normal_multi_segment_lifecycle` — 正常路径：多段完成、消费者归还、最终完成后槽位复用且代次可核对。
- `cancel_then_late_device_segment_while_consumer_holds_buffer` — 题面核心交错：取消回执先于最终完成，底层随后送达的最后一段被回收，消费者仍持有先前缓冲并在之后归还。
- `stale_generation_events_do_not_advance_new_request` — 槽位复用后，旧代次的完成/取消/最终完成均被忽略，不推进新请求。
- `duplicates_never_add_balance` — 重复的完成、归还、取消、最终完成均不改变余额与状态。
- `boundary_conditions` — 边界：槽位耗尽、越界槽位/缓冲 id、错误代次、未最终完成前不可复用。
- `capacity_changes_are_traceable_to_actual_events` — 容量变化逐条对应日志中的 `Completion`/`Returned`；回收与重复事件不动余额。
- `interleaved_script_across_slots` — 通过 `Step` 脚本交错驱动两个槽位上的请求：取消、回收、归还、复用与代次递增交叉验证。

未执行的测试：无。
