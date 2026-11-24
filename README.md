# 行模式识别算子（Row Pattern Recognition Operator）

面向**分区有序 SQL 输入流**的行模式识别算子，语义对齐 SQL `MATCH_RECOGNIZE`
的核心规则。纯 Rust 1.95.0 标准库实现，无任何第三方依赖。

## 1. 模式语言

模式通过 `row_pattern_recognizer::Pattern` 以编程方式构造（不含 SQL 文本解析器）：

| 构造 | 含义 |
| --- | --- |
| `Pattern::var(id)` | 模式变量，谓词决定当前行是否可被该变量捕获 |
| `Pattern::concat(vec![...])` | 按书写顺序连接 |
| `Pattern::alt(vec![...])` | 选择，**按书写顺序优先** |
| `Pattern::repeat(child, min, max, greed)` | 有限（`Some(m)`）或开放（`None`）重复 |
| `Pattern::optional / star / plus` | `?` / `*` / `+` 语法糖 |

`Greed::Greedy` 在仍可能完成匹配时优先再迭代一次；`Greed::Lazy` 优先立即停止。
有界重复在编译期完全展开为有限 NFA；无界重复生成显式环状态。

## 2. 谓词与三值逻辑

谓词实现 `RowSource<R>`  trait：

```rust
fn predicate(&self, variable: usize, current: &R, last_capture: Option<&R>) -> Tribool;
```

* `current` 是候选行；`last_capture` 是**同一变量**在本次匹配中最近一次捕获的行；
* 变量尚未捕获时引用为 `None`，对应 SQL `NULL`，任何比较得到
  `Tribool::Unknown`（而不是 false）；
* 只有 `Tribool::True` 允许消费边被采用，`False` 与 `Unknown` 都拒绝该行；
* 提供 `cmp_options` / `eq_options` 辅助函数实现可空值的 SQL 比较。

## 3. 胜负选择（不是最长匹配）

模式被编译为 Thompson NFA（`src/nfa.rs`）。边的顺序直接编码优先级：

* 选择分支按书写顺序链接；
* 贪婪重复的回路边排在退出边之前，懒惰重复相反；
* 每个线程记录所选边序号序列 `rank`，按**字典序**比较；
* 完整匹配取最小 `rank`。该规则同时实现"最早起点优先"（起点由驱动器按序
  启动）和"分支/重复书写顺序优先"，不会把所有模式退化为最长匹配。

## 4. 流式推进、跨批状态与输出时机

`Recognizer::push_row(partition, row)` 逐行喂入，`end_partition()` 关闭分区。

* 每个起点（run）保留全部仍可能胜出的线程及其各自的捕获；
* **有界模式**：最长长度 `L` 行已被观察、或所有线程死亡后，胜者已不可能被
  后续输入改变，此时才输出；
* **无界模式**：只能在分区结束时定胜负，期间不输出匹配；
* 临时成功但未决的匹配不会提前输出（例如 `A B*` 在一个 B 后已可完成，但
  更多 B 仍可能延长贪婪线程并改变已有行的变量归属）；
* 分区键变化时自动关闭旧分区、打开新分区；
* 输出 `Event`：
  * `PartitionStart` / `PartitionEnd(PartitionStats)`：分组边界与捕获统计；
  * `Matched(Match)`：起止行、每行所属变量（`RowCapture`）、每变量捕获计数、
    是否空匹配；
  * `UnmatchedRow { row_index }`：已确定不可能再被任何匹配覆盖的行。

## 5. 匹配后跳过（AFTER MATCH SKIP）

* `SkipMode::PastRow`：跳到整段匹配之后；空匹配不消费行，推进起点一行；
* `SkipMode::ToVariable(var)`：跳到该变量末次捕获行；目标变量未捕获、或
  目标不能使起点向前移动时，回退为推进起点一行。

## 6. 历史裁剪与快照恢复

* 行历史只保留未决分支仍可能读取的行；run 定胜负后按新起点裁剪
  （`retained_row_count()` 可观测）；
* 无界模式在分区结束前保留全量历史（这是正确性的必要代价，文档化限制）；
* `snapshot()` / `restore(snapshot)` 完整保存与恢复所有未决线程（含未决
  贪婪分支）、行缓冲和分区统计。

## 7. 公开 API 一览

```rust
Pattern, Greed, Repeat                         // 模式 AST
Tribool, cmp_options, eq_options               // 三值逻辑
RowSource<R>, ClosureSource<F>                 // 谓词接口与闭包适配器
Recognizer::<P, R, S>::new(pattern, n, skip, source)
Recognizer::push_row(partition, row) -> Vec<Event>
Recognizer::end_partition() -> Vec<Event>
Recognizer::snapshot() / restore(snapshot)
Recognizer::retained_row_count() / next_row_index()
Event::{PartitionStart, Matched, UnmatchedRow, PartitionEnd}
Match, RowCapture, PartitionStats, SkipMode, BuildError
```

## 8. 复现步骤

```bash
cargo test --offline
```

工作区从空目录建立，`Cargo.toml` 锁定 `rust-version = "1.95.0"` 且无
`[dependencies]`。测试使用本地合成数据（`tests/common/mod.rs` 中的 `Row`），
不访问任何网络、账户或真实系统目标。

## 9. 验证范围

### 单元测试（`src/**` 内 `#[cfg(test)]`）

* 三值逻辑真值表与可空比较（`tribool.rs`）；
* 模式长度上下界、可空性（`pattern.rs`）；
* NFA 边顺序、贪婪环优先级、可空 `*` 的 epsilon 断环、有界展开
  （`nfa.rs`）；
* 非法变量与反转重复边界的构造错误、贪婪延长、有序选择、谓词读取上次
  捕获（`recognizer.rs`）。

### 集成测试（`tests/`，全部基于真实算子输出，无预置演示数据）

`tests/batching.rs` —— **切批不变性**（题面硬性验收点，8 个测试）：

* 对同一批行以 `1..=n+1` 的所有切批方式喂入，决策序列必须逐事件相同；
* 覆盖贪婪 `*`、有界贪婪/懒惰重复、有序选择、带上次捕获谓词的 `+`、
  嵌套选择（`(A|B){1,4}`）；
* 内含独立的深度优先参考匹配器（`reference_match`，直接在 AST 上回溯，
  与 NFA 实现相互独立），交叉核对每个匹配的终点与逐行变量归属；
* 贪婪有界重复在最长路径无法完成时回退到较短的完整匹配（rank 决胜，
  而非盲目保留最长跨度）；
* "先暂时匹配成功、下一行改变变量归属"场景：断言未决期间不产生任何
  `Matched` 事件，定胜负后一次输出完整跨度，并与参考匹配器一致。

`tests/streaming.rs` —— 流式与恢复语义（12 个测试）：

* 有界 run 在胜者不可能改变前不输出；
* 历史裁剪上限、无界模式保留全量历史至分区结束；
* 快照/克隆/恢复后未决贪婪分支走出相同胜者；
* `SKIP PAST ROW`、`SKIP TO variable`（含重叠重启）、目标未捕获回退；
* 分区边界事件与分区统计重置（顺序双分区 + 分区键切换）；
* 空跨度匹配（`A*` 的尾部空匹配、统计计数、全失败行下恰好一个空匹配并正常终止）；
* 三值逻辑边界：谓词返回 `Unknown` 与 `False` 一样拒绝该行；
* 线程提前死亡时立即定胜负并逐行上报未匹配行。

### 测试总量

`cargo test --offline` 共运行 **31 个测试**（库单元 11 + `batching` 8 +
`streaming` 12），全部通过、零编译警告，无跳过（ignored）测试。

### 明确的范围限制（未做的事）

* 不包含 SQL 文本语法解析器，模式以 AST 构造；
* 无界重复的回溯缩短（贪婪匹配在后续连接失败时回退到较短迭代）由 NFA
  排名线程完整支持，但参考匹配器中的无界重复仅验证尾部位置的最大/零
  迭代两种情形；
* 不实现 SUBSET、导航函数（`FIRST`/`LAST`/`RUNNING`/`FINAL`）等度量输出，
  算子输出逐行变量归属与捕获计数，度量可由调用方基于事件自行聚合；
* 所有测试均在本工作区内执行；未运行的测试会在提交说明中明确标注
  （当前无跳过的测试）。
