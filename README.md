# causal-dispatch

三处机房（多个来源 / source）发布的事件经不同链路汇到同一总线。本 crate 实现接收端的
**因果派发**：接收端按来源维护连续进度，只放出自源下一个序号且所有依赖都已满足的消息；
同一时刻有多条可选时按键 `(来源名, 序号)` 的字典序取最小者，每放出一条都重新判定。
断点同时保留各来源的连续进度和仍在等待的消息。

- 工具链：Rust **1.95.0**（`rustc 1.95.0` / `cargo 1.95.0`），edition 2021
- 依赖：**无**。只有标准库（`std::collections::{BTreeMap, btree_map}`、`std::error::Error`、`std::fmt`）
- 无网络访问、无外部服务、无凭据

---

## 语义

一条消息由 `(source, seq, deps, payload)` 组成：

| 字段 | 含义 |
| --- | --- |
| `source` | 来源名，非空 |
| `seq` | 本源序号，从 `1` 开始递增 |
| `deps` | `BTreeMap<String, Seq>`：对每个点名的来源，派发本消息前该来源**必须已经派发到**这个序号（含该序号）；未点名的来源要求为 `0` |
| `payload` | 业务负载，派发器不解释 |

一条消息可被派发（dispatchable）当且仅当：

1. 它是**自己来源的下一个序号**：`progress(source) + 1 == seq`；
2. 对 `deps` 中每一项 `(s, n)`：`progress(s) >= n`。

每次 `receive` 结束后都会尽可能多地放行消息：每放出一条就重新判定，直到没有可放行的为止。
多条同时可放行时，取 `MessageId = (source, seq)` 字典序最小者。`MessageId` 的 `Ord` 是派生
实现，先比 `source`（UTF-8 字节序，因此 `"Z" < "a"`、`"dc-10" < "dc-2"`），再比 `seq`。

### 去重与断点

- 已派发过的序号（`seq <= progress(source)`）或已在等待集合中的同一 `(source, seq)` 视为重复投递：
  计入 `ignored_duplicates`，不改变任何状态，第一个副本胜出。
- `checkpoint()` 产出 `Checkpoint<T>`：各来源连续进度 + 仍在等待的消息 + 两个计数器。
  `CausalDispatcher::restore(checkpoint)` 重建出完全相同状态的接收端；同一个 checkpoint 可恢复多次，
  得到彼此独立、状态相同的接收端。
- 因为每次 `receive` 结束前都已排空，checkpoint 总是在静止状态下取得，恢复后不会遗漏可派发消息。

---

## 公开 API

```rust
pub type Seq = u64;

pub struct MessageId { pub source: String, pub seq: Seq }   // Ord: (source, seq)
impl Display for MessageId                                   // "source/seq"

pub struct Message<T> {
    pub source: String,
    pub seq: Seq,
    pub deps: BTreeMap<String, Seq>,
    pub payload: T,
}
impl<T> Message<T> {
    pub fn new(source: impl Into<String>, seq: Seq, payload: T) -> Self;
    pub fn depends_on(self, source: impl Into<String>, seq: Seq) -> Self; // 同源多次调用取较大要求
    pub fn id(&self) -> MessageId;
}

pub enum DispatchError {
    EmptySource,
    EmptyDependencySource,
    ZeroSequence { source: String },
    SelfDependencyNotInPast { source: String, seq: Seq, required: Seq },
}
impl Display for DispatchError
impl Error for DispatchError

pub struct Checkpoint<T>;   // 字段私有，只能由 checkpoint() 产生
impl<T> Checkpoint<T> {
    pub fn progress(&self, source: &str) -> Seq;
    pub fn progressed(&self) -> &BTreeMap<String, Seq>;
    pub fn waiting_ids(&self) -> impl Iterator<Item = &MessageId>;
    pub fn pending_len(&self) -> usize;
    pub fn dispatched_count(&self) -> u64;
    pub fn ignored_duplicates(&self) -> u64;
}

pub struct CausalDispatcher<T>;
impl<T> CausalDispatcher<T> {
    pub fn new() -> Self;                       // 另有 Default
    pub fn receive(&mut self, message: Message<T>)
        -> Result<Vec<Message<T>>, DispatchError>;
    pub fn progress(&self, source: &str) -> Seq;
    pub fn progressed(&self) -> &BTreeMap<String, Seq>;
    pub fn waiting(&self) -> btree_map::Values<'_, MessageId, Message<T>>;
    pub fn waiting_ids(&self) -> btree_map::Keys<'_, MessageId, Message<T>>;
    pub fn pending_len(&self) -> usize;
    pub fn is_pending(&self, source: &str, seq: Seq) -> bool;
    pub fn dispatched_count(&self) -> u64;
    pub fn ignored_duplicates(&self) -> u64;
    pub fn checkpoint(&self) -> Checkpoint<T> where T: Clone;
    pub fn restore(checkpoint: Checkpoint<T>) -> Self;
}
```

`receive` 返回的 `Vec` 就是本次收件后新放行的消息，顺序即调用者应交付给下游的顺序；
若其中有多条，后面的可能依赖前面的（每放一条都重新判定）。

**错误语义**：`receive` 对畸形消息（空来源、序号 `0`、依赖了空来源名、或要求自己来源达到
`>= seq`）返回 `DispatchError`，且**完全不改变接收端状态**。

---

## 复现步骤

```sh
cd <本题工作区>
cargo test --offline
```

期望（本机实测，见下节）：

- `tests/dispatch.rs`：28 个集成测试全部通过
- `src/lib.rs` 文档示例（doctest）：1 个通过
- 单元测试目标：0 个（本 crate 无 `#[cfg(test)]` 模块，逻辑全部由集成测试覆盖）

构建/运行均使用 `--offline`，不访问网络。

---

## 验证范围

`tests/dispatch.rs` 覆盖三类场景：

**正常（normal）**

- 单来源按序交付；先到的大序号（`2` 先于 `1`）等待，`1` 到达后一次放出连续段 `1,2`
- 多来源互不阻塞，各自推进
- 声明的依赖阻止派发，满足后放行
- 已派发消息的重复投递被忽略（不同 payload 也一样）
- 等待中的重复投递保留第一个副本
- 依赖已满足的消息到达即派发

**边界（boundary）**

- 自身序号缺口：`3,4` 先到、`2` 未到时不放行；补上 `1` 后整段 `1..4` 放出
- 同时可派发时按 `(来源名, 序号)` 字典序：`dc-a/2` 先于 `dc-b/1`
- 来源名相同再比序号：`alpha/1 < alpha/2 < mike/1 < mike/2 < zulu/1`
- 字节序边界：`"Z" < "a"`、`"dc-10" < "dc-2"`
- 依赖的序号是**闭区间**（要求 `dc-a/2` 已派发，仅收到不算）
- 依赖未知来源会一直等待，直到该来源出现并推进
- 循环依赖（`a1` 等 `b1`、`b1` 等 `a1`）不派发、不丢消息、不死循环
- 自依赖未来序号 / 空来源 / 序号 `0` / 空依赖来源：返回错误且状态不变
- `depends_on` 对同一来源多次调用取**较大**（更严）要求
- `MessageId` 排序与 `Display`
- 空接收端 checkpoint 可恢复

**交错（interleaved）**

- 三来源链式依赖 `a2 -> b1 -> c1`，一次收件内级联放出
- 每放一条重新判定：字典序更小但受阻的消息被跳过，先放能放的
- 断点保留进度与等待消息；重建后继续完成同一条因果链
- 断点在等待消息仍在收件端时取得，恢复后不丢消息
- 同一 checkpoint 恢复两次得到等价、独立的状态
- 重启后重送旧消息不再派发，进度得以延续
- 20 条三来源乱序交错投递，最终各来源严格升序、无残留
- **题面场景**：`B` 等 `A2`、`C` 等 `B1` 时，先收 `C` 和 `B` → 取断点 → 重开 →
  再送一遍 `C`、`B`（无派发）→ 补上 `A1`、`A2`，调用者按
  `A1, A2, B1, C1` 接到完整次序 → 再整套重送，`dispatched_count` 不变、只增加
  `ignored_duplicates`

### 实测结果

```
$ cargo test --offline
running 0 tests          (unit test target: ok)
running 28 tests         (tests/dispatch.rs: ok, 28 passed)
running 1 test           (doctest: ok, 1 passed)
```

### 未执行 / 范围外

- 未运行 `cargo clippy`、`cargo fmt --check`、`cargo doc`：本环境未授权调用这些子命令，因此
  不做格式/静态检查方面的声明。
- 未做性能/压测与大规模基准；`drain_ready` 每次扫描等待集合，复杂度为
  `O(n^2)` 量级的朴素实现，未针对大 backlog 优化。
- 未涉及真实网络、真实机房链路、持久化介质或进程崩溃恢复；"断点"是进程内的值快照，
  序列化（如写入磁盘或跨进程传输）不在本实现范围内（`Checkpoint` 未实现 `serde`）。
- 未验证异步/并发投递；接收端是单线程 `&mut self` 状态机，调用方需自行保证串行化。
- 无第三方依赖、未安装任何依赖、未访问任何真实账户或系统。
