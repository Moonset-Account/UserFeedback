# stackwalk — 合成栈回溯模块

读取合成寄存器、分段栈内存与紧凑展开表，逐帧恢复调用链。Rust 1.95.0，仅标准库，无第三方依赖。

## 公开 API（`src/lib.rs`）

| 项 | 说明 |
| --- | --- |
| `Registers { ip, sp, seg }` | 合成寄存器组：指令指针、栈指针、所在栈段 |
| `StackSeg::{Main, Alt}` | 栈段标识：主栈 / 异步事件备用栈 |
| `StackSegment` | 一段栈内存，`[base, base+len)` 半开区间合法；`write_u64` / `write_saved_context` 构造合成数据 |
| `Memory::new(segments)` / `read_u64(seg, addr)` | 分段栈内存；每次取数校验对应栈段边界，越界返回 `MemFault` |
| `UnwindRule::Normal { cfa_offset }` | 普通帧：`caller_sp = sp + cfa_offset`，返回地址在 `[caller_sp - 8]`；查表定位前一条调用指令用 `ra - 1` |
| `UnwindRule::Async { ip_off, sp_off, seg_off }` | 异步事件帧：当前栈段 `sp + 各偏移` 处保存被打断的 ip / sp / 段编号；跨帧精确恢复，**不做** `ra - 1` |
| `UnwindEntry { start, end, rule }` | 展开表条目，指令半开范围 `[start, end)` |
| `Unwinder::new(&mem, &table).walk(&start)` | 执行回溯，返回 `WalkResult` |
| `WalkResult { frames, stop }` | 可确认的帧列表 + 首次停止原因 |
| `StopReason` | `EndOfChain` / `NoUnwindInfo` / `MemoryFault` / `Cycle` / `AsyncDepthExceeded` / `InvalidSavedSegment` / `MaxFrames` |

## 展开规则要点

- 普通调用的返回地址指向调用指令的**下一条**指令，因此查表时用 `ra - 1` 定位前一条调用指令；栈段不变。
- 异步事件帧保存了一整份被打断的上下文（可能位于另一段备用栈），跨帧时精确恢复被打断的 ip / sp / 栈段，不套用 `ra - 1`。
- 每次取数（返回地址槽、保存的上下文槽）都校验对应栈段边界，越界即以 `MemoryFault` 停止。
- 重复的 `(ip, sp)` 组合判定为回路，以 `Cycle` 停止。
- 最多支持两级嵌套异步帧，第三级以 `AsyncDepthExceeded { depth: 3 }` 停止。

## 复现步骤

```sh
cargo test --offline
```

## 验证范围（`tests/unwind.rs`，10 个测试，全部真实执行）

正常路径：
- `normal_call_chain_to_end` — 三级普通调用链，顶端返回地址为 0，`EndOfChain` 结束。

边界场景（题目指定）：
- `async_event_ip_on_range_boundary` — 异步事件保存的被打断 ip 恰为后一范围起点（0x200）：跨异步帧精确恢复、不做 `ra - 1`，命中后一范围的普通帧规则，并核对栈段从 Alt 切回 Main。
- `return_address_on_next_range_start` — 普通返回地址恰为后一范围起点（0x410）：用 `ra - 1` 命中前一范围的规则；若错用 `ra` 直接查表会选中后一范围规则并走向 `NoUnwindInfo`，测试通过 `EndOfChain` 断言核对规则选择。

交错 / 嵌套场景：
- `two_level_nested_async_interleaved` — 普通帧与异步帧交替的两级嵌套异步链，跨两次栈段切换后精确恢复主流程现场，`EndOfChain` 结束。
- `third_nested_async_exceeds_depth` — 第三级嵌套异步帧触发 `AsyncDepthExceeded { depth: 3 }`。

故障 / 停止场景：
- `repeated_ip_sp_combination_is_a_cycle` — `(ip, sp)` 重复，`Cycle` 停止。
- `read_outside_segment_bounds_stops_with_fault` — 普通帧返回地址槽越出 Main 段，`MemoryFault`。
- `async_context_read_outside_alt_segment_faults` — 异步上下文槽越出 Alt 段，`MemoryFault`。
- `ip_without_unwind_info_stops` — ip 不在任何展开范围，`NoUnwindInfo`。
- `invalid_saved_segment_stops` — 异步帧保存的段编号非法，`InvalidSavedSegment`。

未执行的测试：无。
