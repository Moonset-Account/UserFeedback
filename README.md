# 热键切块的全外连接执行算子（Rust，纯标准库）

## 背景与目标

全外连接中，少数「热键」（行数极大的等值键）可能占满单个执行节点的内存。
本项目实现一个把热键**两侧分别切块**、再以**块对（左块 × 右块）**为单位调度
任务的执行算子：

- 输入：两侧行 ID、等值键、以及比较左右两行的附加条件（residual predicate）；
- 每个任务只持有给定行数上限的一个左块和一个右块，单任务输入行数有硬上界；
- 块对之间无顺序依赖，可以**乱序完成**；
- 同一块对的失败尝试可以被**新尝试取代**，迟到/过期回调一律丢弃；
- 只采纳**已确认尝试**产出的匹配对与逐行命中记录，每对匹配只输出一次；
- 块中当前没命中的行，只有在**所有可能包含配对的块都确认后**，才能作为未匹配行输出；
- 空键（`key == None`）不参与块对，各自直接走未匹配路径。

仅使用 Rust 1.95.0 标准库，无第三方依赖。

## 目录结构

```
Cargo.toml
src/
  lib.rs       #  crate 入口与公开类型再导出
  row.rs       #  Row<K,V> 输入行、JoinOut<K,V> 全外连接输出
  block.rs     #  partition() 切块函数与 Block（含切块单元测试）
  operator.rs  #  FullOuterJoin 算子：块对状态机、回调确认、补空终态、Metrics
  executor.rs  #  SimulatedNode：对块对做真实嵌套循环连接 + 附加条件
tests/
  hotkey_scenario.rs       # 题面主场景：3x3、最后块对才命中、失败回调迟到
  edge_and_recovery.rs     # 空键/单侧键/空输入/失败重试/重复成功/容量边界
  randomized_interleave.rs # 200 个固定种子的随机交错压力测试（对照参考实现）
```

## 公开 API

### 输入与输出（`src/row.rs`）

- `Row<K, V> { id: u64, key: Option<K>, value: V }`：输入行；`key=None` 即空键。
- `JoinOut<K, V>`：
  - `Matched { key, left_id, left_value, right_id, right_value }`
  - `LeftOnly { key, left_id, left_value }`（右侧补空）
  - `RightOnly { key, right_id, right_value }`（左侧补空）
  - `left_id()` / `right_id()`：便捷取值。

### 切块（`src/block.rs`）

- `partition(rows, cap) -> Vec<Vec<Row>>`：按输入顺序切块，每块至多 `cap` 行，
  `cap` 必须为正（否则 panic），空输入返回空向量。
- `Block<K, V, Id> { id, rows }`，`len()` / `is_empty()`。

### 算子（`src/operator.rs`）

- `FullOuterJoin::<K, V>::new(block_cap)`：创建算子，`block_cap` 为单块行数上限。
- `load(&mut self, left, right)`：载入两侧输入（只能一次）。内部完成：
  空键直接补空输出；按等值键分组并切块；同键生成全部左×右块对；
  单侧键立即终态补空。
- `schedule(max_tasks) -> Vec<Task>`：派发至多 N 个待派发块对
  （首次派发或失败后重新派发），每个 `Task` 自带两个块的行数据与尝试号。
- `retry_pair(pair) -> Task`：对失败后回到待派发状态的块对重新派发。
- `supersede_pair(pair) -> Task`：对**在途**块对发起取代性新尝试，旧尝试立即作废。
- `on_success(PairResult) -> CallbackStatus`：成功回调；过期返回 `Stale` 并丢弃。
- `on_failure(pair, attempt) -> CallbackStatus`：失败回调；采纳后块对回到待派发，
  过期失败同样丢弃。
- `drain_output() -> Vec<JoinOut>`：取走当前已产出的结果（匹配对在块对确认时产出，
  补空行在块终态时产出）。
- `is_done()`：所有块对均已确认。
- `metrics() -> Metrics`：派发尝试数、确认数、采纳/过期回调数、
  **单任务输入行数峰值 `peak_task_input_rows`**、各类输出计数、空键行数。
- 辅助查询：`pair_count()`、`left_block_count()`、`right_block_count()`、
  `next_attempt_of()`、`pair_blocks()`、`pair_is_unsent()`。
- ID 类型：`LeftBlockId`、`RightBlockId`、`PairId`、`AttemptId`；
  回报类型 `PairResult { pair, attempt, matches, left_hit_rows, right_hit_rows }`；
  回调状态 `CallbackStatus::{Accepted, Stale}`。

### 执行节点（`src/executor.rs`）

- `SimulatedNode::new(residual)`：`residual: Fn(&Row, &Row) -> bool` 是附加条件。
- `node.run(&task) -> PairResult`：对任务持有的两个块做真实嵌套循环扫描，
  先防御性校验两侧非空且等值键相同，再应用附加条件；输出去重匹配对与逐行命中记录。

### 状态机摘要

每个块对处于三种状态之一：

```
Unsent ──dispatch──▶ InFlight(a) ──on_success(a)──▶ Confirmed(a)   # 终态
                        │  ▲
            on_failure(a)  └──dispatch（retry/supersede，尝试号 a→a+1）
                        ▼
                     Unsent
```

回调携带的尝试号与当前在途尝试号不一致、或块对已确认时，回调记为 `Stale` 丢弃，
不改变任何状态与输出。

## 复现步骤

```bash
# 工具链：rustc/cargo 1.95.0
cargo test --offline
```

无外部依赖、无需联网，`--offline` 可直接运行。也可以单独运行：

```bash
cargo test --offline --test hotkey_scenario       # 题面主场景
cargo test --offline --test edge_and_recovery     # 边界与故障恢复
cargo test --offline --test randomized_interleave # 200 种子随机交错
cargo test --offline --lib                        # 切块单元测试
```

## 验证范围（均为真实执行，非预置输出）

### 1. 题面主场景 `hot_key_three_by_three_with_late_failure_and_final_pair_hit`

- 同一个热键两侧各 6 行、块容量 2 → **左右各三块、9 个块对**；
- 附加条件为「两侧载荷相等」，右侧载荷故意打乱，使命中分散在不同块对；
- 左行 6 唯一可能的命中位于块对 P8=(L2,R2)，且 P8 被安排为**全局最后确认**；
- 先乱序确认其余 8 个块对，期间断言行 6、右行 105 **从未出现在任何输出中**
  （不能提前补空）；
- 对悬而未决的 P8 调用 `supersede_pair` 发起新尝试（attempt 1 → 2），
  随后投递旧尝试**迟到的失败回调**，断言返回 `Stale` 且状态不变；
- 新尝试真实执行（`SimulatedNode::run`）成功，命中 (6,105)，P8 确认后 L2/R2 终态；
- 最终输出与独立的朴素参考全外连接实现**按集合逐行相等**：
  5 个匹配对、1 个左补空（行 3，载荷 30 无配对）、1 个右补空（行 102，载荷 99 无配对）；
- 指标断言：派发 10 次（9 首次 + 1 取代）、确认 9 对、过期回调 1 次、采纳失败 0 次；
- **每个任务**断言左右块各不超过 2 行、`input_rows() ≤ 2*cap = 4`，
  且 `Metrics.peak_task_input_rows == 4`。

### 2. 边界与故障恢复（`tests/edge_and_recovery.rs`，6 个测试）

- 空键行 `load` 时立即补空，不产生块对，即使载荷相等也不匹配；
- 单侧键：无共同键则块对数为 0、无需调度即完成，所有行补空；键不同即使载荷相同也不匹配；
- 空输入：无任务、无输出、峰值为 0；
- 失败回调被采纳后块对重置，`retry_pair` 拿到新尝试号；旧尝试的迟到成功回报被丢弃；
- 同一块对重复成功回报：第二次按过期处理，匹配对只输出一次；
- 容量边界：7×5 行、cap=3 切成 3×2 块，每个任务输入行数 ≤ 6。

### 3. 随机交错（`tests/randomized_interleave.rs`，200 个固定种子）

每个种子随机生成 0–11 行的两侧数据（小键空间制造热键、约 1/6 空键、重复载荷制造
多命中与未命中）与块容量 1–4，驱动过程中随机：

- 乱序派发与确认块对；
- 注入被采纳的失败并重新派发；
- 对在途块对发起取代性重试，旧尝试作为「僵尸」之后以成功或失败形式迟到，断言全部 `Stale`；
- 每个任务都断言块行数与总输入行数不超上界。

每个种子最终输出都与朴素参考实现（全量嵌套循环 + 全外连接补空）按集合完全一致，
并校验 `Metrics` 各项计数。测试末尾还断言 200 个种子整体上**确实触发过**失败回调、
过期回调与重试派发，避免这些分支形同虚设。

### 4. 切块单元测试（`src/block.rs`，5 个测试）

空输入、整除、有余数、cap=1、cap=0 拒绝。

## 明确的设计约束与未覆盖项

- 算子负责切块、块对编排与状态机；行的比较计算放在 `SimulatedNode`，
  测试中的失败通过回调编排注入，节点本身不模拟网络故障。
- 算子保留全部块与行归档用于补空输出；内存上界的验收对象是**单个任务持有的输入块**
  （`Task::input_rows` 与 `Metrics.peak_task_input_rows`），这正是题面要求每个任务
  只允许持有给定行数输入块的含义。
- 未实现跨进程/线程的真实分布式调度与持久化；回调由单线程测试驱动按脚本/随机顺序投递，
  足以确定性地覆盖乱序、重试、取代与过期交错。
