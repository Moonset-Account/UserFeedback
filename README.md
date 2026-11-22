# rngflow —— 可重算、可确认点恢复的随机流分配器

## 1. 要解决的问题

训练中一次前向可能因激活重算而再次执行，同一批样本也可能被调度到不同 rank；
一次执行还可能被拆成多段、回包乱序，部分批次可能提前取消。本库为
`(固定实验种子, 全局步, 样本ID, 算子路径, 逻辑调用序号, 抽样用途)` 分配
**可重复的整数抽样序列**，并满足：

| 题面要求 | 设计保证 |
| --- | --- |
| 同一逻辑调用重试 / 重算得到相同序列 | 所有随机字是坐标的纯函数（计数器模式），无全局可变 RNG 状态 |
| 实际 rank、回包顺序、执行分段不影响取值 | 这些物理因素**不进入**流坐标，类型上无从影响输出 |
| 不同用途、不同调用序号各自推进 | `purpose` 与 `logical_call` 是流坐标字段；`CallProgress` 按用途持有独立游标 |
| 接受前可拒绝任意多个候选随机字 | 每个位置带独立的候选序号 `attempt`，`reject()` 只推进它 |
| 跳过一行 / 取消部分批次不改变其他位置的值 | 位置 `p` 的值只由 `(key, p, attempt_p)` 决定，与其他位置是否执行无关 |
| 从确认点恢复逻辑调用取值进度 | `Checkpoint` 持久化每位置拒绝数，`StreamCursor::restore` 恢复 |
| 从明确逻辑位置重新计算掩码 | `recompute_mask_bit` / `recompute_word` 是无状态纯函数，只需确认点里的候选序号 |

## 2. 设计原理：计数器模式（counter mode）

每个 64 位随机字由下式纯函数给出：

```text
word = mix64(fold([base(key), position, attempt]))
base = fold([seed, global_step, sample_id, path_hash, logical_call, purpose])
```

- `mix64`：SplitMix64 风格的双射混合（`u64 -> u64`），不同计数器输入不会塌缩；
- `fold`：逐字段混合并混入字段下标，避免字段排列产生相同折叠；
- `path_hash = FNV-1a_64(operator_path)`：算子路径在构造坐标时即折叠为 `u64`；
- `position`：该用途流中的逻辑抽样位置；
- `attempt`：该位置接受前已拒绝的候选数（确认点唯一需要持久化的进度）。

因此重算不需要重放历史抽样，也不需要共享可变状态：确认点只保存
“每个位置接受时的候选序号”，掩码即可在任意时刻、任意线程、任意 rank 上重建。

## 3. 目录结构

```text
.
├── Cargo.toml              # 纯标准库，无第三方依赖；rust-version = "1.95.0"
├── README.md
├── src
│   ├── lib.rs              # 库入口与公开 re-export，#![deny(unsafe_code)]
│   ├── rng.rs              # 混合原语、FNV-1a、StreamKey、candidate_word
│   ├── cursor.rs           # StreamCursor / CallProgress / ProgressCursor / 掩码重算
│   └── checkpoint.rs       # Checkpoint 及确定性小端二进制编解码、校验错误
└── tests
    ├── acceptance.rs       # 重算/取消/换rank/分段/并发交错端到端验收
    └── cursor_segments.rs  # 分段推进 + 确认点对接
```

## 4. 公开 API

### `rngflow::rng`

| 项 | 说明 |
| --- | --- |
| `struct Purpose(u64)` | 抽样用途；`Purpose::named("dropout")` 由名字派生，`Purpose::number(n)` 直接编号；内置 `Purpose::DROPOUT` / `Purpose::ROUTING`（编译期 FNV-1a 常量） |
| `struct StreamKey` | 流坐标 `{seed, global_step, sample_id, operator_path(哈希), logical_call, purpose}`；`StreamKey::new(...)` 接受字符串路径，`with_path_hash(...)` 接受预折叠哈希；`stream_id()` / `call_id()` 生成稳定标识 |
| `fn candidate_word(&StreamKey, position, attempt) -> u64` | 计数器模式随机字，`const fn`，纯函数 |
| `fn word_at(...) -> u64` | 同上的别名入口 |
| `fn mix64(u64) -> u64` / `fn fnv1a_64(&[u8]) -> u64` / `fn fold_many(&[u64]) -> u64` | 确定性原语（均为 `const fn`） |

### `rngflow::cursor`

| 项 | 说明 |
| --- | --- |
| `StreamCursor::new(key)` / `restore(key, next_position, rejections)` | 新建 / 从确认点恢复单条用途流 |
| `peek_word()` | 查看当前候选随机字，不推进 |
| `reject()` | 拒绝当前候选，候选序号加一，位置不变；接受前可调用任意多次 |
| `accept() -> u64` | 接受当前候选，记录拒绝数并前进到下一逻辑位置 |
| `accept_if(max_attempts, predicate) -> Option<u64>` | 谓词驱动的拒绝采样，含防无限循环上限 |
| `accepted_word(position)` / `progress()` / `next_position()` | 重算已接受值 / 导出 `(next_position, rejections)` |
| `CallProgress` | 一个逻辑调用下按用途区分的独立流集合；`stream(Purpose)`、`snapshot()`、`restore(...)` |
| `ProgressCursor` | 在 `CallProgress` 上的分段执行入口，`from_progress` 可从确认点接管 |
| `recompute_word(key, position, accepted_attempt) -> u64` | 无状态重算接受的随机字（`const fn`） |
| `recompute_mask_bit(key, position, accepted_attempt, keep_prob) -> bool` | 无状态重算掩码位：`word <= keep_prob * u64::MAX` 为保留；`keep_prob` 限定 `[0,1]` |

### `rngflow::checkpoint`

| 项 | 说明 |
| --- | --- |
| `Checkpoint::new()` / `record_cursor(&StreamCursor)` / `record_progress(...)` | 建立并写入流进度（按 `stream_id` 索引） |
| `encode_to_vec() -> Vec<u8>` | 确定性小端二进制：魔数 `RNGF` + 版本 + 各流坐标与拒绝历史 |
| `Checkpoint::decode(&[u8]) -> Result<Checkpoint, CheckpointError>` | 完整校验解码：魔数、版本、长度、重复流、进度自洽性 |
| `StreamRecord::restore_cursor()` | 从一条确认点记录重建内存游标 |
| `CheckpointError` | `BadLength / BadMagic / UnsupportedVersion / BadStreamCount / DuplicateStream / InconsistentProgress` |

### 最小用法

```rust
use rngflow::rng::{Purpose, StreamKey};
use rngflow::{Checkpoint, StreamCursor, recompute_mask_bit};

let key = StreamKey::new(/*seed*/ 42, /*step*/ 7, /*sample*/ 99,
                         "blocks/3/dropout", /*call*/ 0, Purpose::DROPOUT);
let mut cursor = StreamCursor::new(key);
let word = cursor.accept_if(64, |w| w >> 60 != 0xF).unwrap(); // 拒绝采样

let mut cp = Checkpoint::new();
cp.record_cursor(&cursor);                       // 打确认点
let bytes = cp.encode_to_vec();                  // 持久化
let restored = Checkpoint::decode(&bytes).unwrap();
let mut cursor = restored.get_key(&key).unwrap().restore_cursor(); // 恢复

// 无状态重算掩码：只需确认点里的候选序号
let (_, rejections) = cursor.progress();
let bit = recompute_mask_bit(&key, 0, rejections[0], 0.3);
```

## 5. 复现步骤

环境：Rust 1.95.0（本机 `cargo 1.95.0` / `rustc 1.95.0`），无第三方依赖，
全程离线，不访问网络、不安装依赖。

```bash
cargo test --offline          # 单元测试 + 两个集成测试目标，共 23 个测试
cargo build --offline         # 库构建
```

## 6. 验证范围（以真实测试为准）

### 6.1 单元测试（`src/` 内，17 个，均已执行通过）

- **正常路径**：随机字纯度与位置/候选敏感性；不同坐标派生不同流；用途独立推进；
  拒绝采样接受值与重算值一致；掩码重算与在线游标逐元素一致；
  确认点编解码往返、空确认点往返、编码确定性。
- **边界**：FNV-1a 标准测试向量（空串/单字符/`foobar`）；`mix64` 万级输入无碰撞
  与固定回归向量；`position × attempt` 各 200 格计数器槽无碰撞；
  掩码 `keep_prob = 0.0 / 1.0` 边界；恒假谓词达到上限返回 `None` 且进度不前进；
  `u64` 溢出使用 `checked_add` 触发 panic。
- **故障/非法输入**：恢复自不自洽进度（拒绝记录条数 ≠ `next_position`）panic；
  确认点截断、魔数错、版本错、尾部多余字节、空输入、手工构造的非法进度均被拒绝。

### 6.2 端到端验收（`tests/acceptance.rs`，5 个，均已执行通过）

1. `recompute_after_rank_swap_and_segmentation_matches_elementwise`
   参考执行 vs. 崩溃后重试 vs. 换 rank + 三段执行 + 段间确认点恢复，
   对 40 个掩码位**逐元素核对**；另含样本乱序回包 `[5,1,4,0,2]` 后按 ID 归位核对；
   崩溃点选在确认点之后一个**确实发生候选拒绝**的位置，验证拒绝路径可重放。
2. `early_cancellation_of_batches_leaves_other_positions_untouched`
   整体取消样本 1、3，批内提前取消样本 2（只接受 12/40 个位置）；
   验证其他样本逐元素不变、部分样本前缀不变、取消样本补跑后仍与参考一致。
3. `concurrent_recompute_from_one_checkpoint_is_interleaving_safe`
   8 个真实 OS 线程并发、按交错位置区间从同一确认点只读重算，
   逐位置核对掩码位与随机字（真实线程调度交错，非模拟）。
4. `independent_branches_keep_separate_sampling_boundaries`
   同调用下 routing 抽 15 个不占用 dropout 的位置编号；routing 中途取消不改变
   dropout 的值；不同调用序号 / 样本 / 用途在同一确认点中互不覆盖。
5. `checkpoint_survives_encoding_round_trip_under_fault_injection`
   18 个位置后注入“故障”，确认点编码 → 解码 → 恢复 → 继续抽样，
   与从未中断的游标逐值、逐拒绝历史比对。

### 6.3 分段专项（`tests/cursor_segments.rs`，1 个，已执行通过）

`ProgressCursor` 第一段推进后落确认点，第二段恢复后继续，与一次跑完的游标
逐位置比对接受值与完整进度元组。

### 6.4 未执行 / 未覆盖项（明确标注）

- 未执行 `cargo build --release` 与 `cargo clippy`：当前会话中这两个命令未获
  执行授权；已验证的是 `cargo test --offline` 的 debug 构建（无警告）与全部测试。
- 未做跨进程持久化介质（文件/数据库）的集成：确认点只定义到确定性字节编码，
  写入何种介质由调用方决定；测试覆盖的是内存字节往返。
- 未提供加密学强度声明：`mix64` 是确定性 PRNG 混合，面向可重复抽样，不是 CSPRNG。

## 7. 约束与安全边界

- 纯 Rust 标准库，`Cargo.toml` 无 `[dependencies]`；`#![deny(unsafe_code)]`。
- 全部测试数据为本地合成的坐标与掩码，不访问真实网络目标、账户或系统，
  不读取凭据，不包含任何攻击 / 漏洞利用 / 规避载荷。
- 进度结构对 `u64` 溢出采用 `checked_add` 显式 panic，不静默回绕。
