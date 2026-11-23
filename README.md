# mirror-recovery：A/B 消息源切换的分区位点恢复模块

## 1. 解决的问题

消费者从来源日志 **A** 切到消息镜像 **B**，之后还要能切回 A（并可再次切到 B）。
模块按**分区**维护来源位点（A 的 offset）与镜像位点（B 的到达 offset）之间的
换算关系，保证跨切换过程中消息**不丢、不重**（至少一次 + 连续确认去重）。

题面约束与对应处理：

| 题面约束 | 模块处理 |
| --- | --- |
| A 每条消息有来源位点与稳定身份 | `MirrorEvent::Mirrored { source_offset, id, .. }`，身份存入 `id_index` |
| B 的实际位点由到达顺序决定 | 事件自带 `b_offset`，模块不重新编号，仅按到达顺序接收 |
| B 可混入 B 侧消息 | `MirrorEvent::Native`，独立于来源前沿确认，不影响来源位点换算 |
| 同一 A 消息可能被重复镜像 | 同身份同位点 → `IngestOutcome::Duplicate`，首个副本获胜 |
| A 可报告明确裁剪区间 | `report_truncation`，区间有序合并；drain 时自动折叠跳过 |
| 尚未镜像与明确裁剪需分别处理 | 裁剪可直接折叠；未镜像位点形成 `Gap`（待补区间），drain 停止 |
| 切换时从最后确认的 A 位点后读取 | `ensure_partition(partition, initial_frontier)` |
| 按来源位点发给消费者 | drain 严格按 `frontier + 1` 递进，B 到达顺序不影响投递顺序 |
| 碰到第一个无法证明存在或裁剪的位点停止并给出应补区间 | `DrainReport.gap: Option<Gap>`，`Gap.to` 指向下一份已知证据 |
| B 侧确认只能推进已读到且连续的前缀 | `ack_mirrored` 拒绝未投递/乱序确认；`fold` 只推进连续前缀 |
| 重开重放与切回 A 共用同一前沿 | 快照持久化 `frontier`；`switch_back_ack` 与 B 侧确认操作同一状态 |

## 2. 工作模型

每个分区维护一个**连续确认前沿 `frontier`**：所有 `<= frontier` 的来源位点，
要么已经投递给消费者并确认，要么属于已报告的裁剪区间。

drain（`RecoveryManager::drain`）的扫描规则，从 `frontier + 1` 开始逐位点判断：

1. **有镜像实体**：投递给消费者，位点进入 `pending`（在途，等待确认）。
2. **命中裁剪区间且无实体**：折叠该区间内连续的无实体位点，前沿推进，记录到
   `skipped_truncations`，继续扫描。
3. **既无实体也未裁剪**：停止，返回 `Gap { from, to }`。
   - `from` 是第一个无法证明存在或裁剪的位点；
   - `to` 是其后第一份已知证据（镜像实体或裁剪区间起点）的位点，`None` 表示开放结尾。
4. 扫描被在途消息或投递预算阻断时，会通过 `peek_gap_behind_pending` 越过在途段
   向前探查，把更远处的缺口一并报告，状态置为 `DrainStatus::Gap`；
   若没有缺口则为 `DrainStatus::AwaitingAck`。

证据优先级：**镜像实体 > 裁剪报告**。即使位点落在已报告的裁剪区间内，只要 B 中
存在该消息的实体，就必须投递给消费者，绝不因裁剪报告静默丢弃。

确认规则：

- `ack_mirrored` 只接受已投递（在途）的来源位点；未投递返回
  `RecoveryError::NotDelivered`。确认先入账 `acked` 集合，再由 `fold` 把连续的
  已确认前缀与裁剪区间并入前沿。
- `switch_back_ack`（切回 A 后使用）要求确认位点严格等于 `frontier + 1`，
  乱序返回 `RecoveryError::OutOfOrderAck`；确认后同样自动折叠后续裁剪区间。
- B 侧原生消息通过 `ack_native` 独立确认，不参与来源前沿。

## 3. 公开 API

### 类型（`src/lib.rs`）

- `Offset = u64`、`Partition = u32`、`MessageId = String`
- `TruncatedRange { from, to }`：闭区间，构造器 `new` 校验 `from <= to`
- `MirrorEvent::Mirrored { b_offset, source_offset, id, payload }`
- `MirrorEvent::Native { b_offset, payload }`
- `IngestOutcome`：`Accepted | Duplicate | Replayed | Conflict`
- `Delivery { kind, b_offset, source_offset, id, payload }`、`DeliveryKind`
- `Gap { from, to: Option<Offset> }`、`DrainStatus::{Gap, AwaitingAck}`
- `DrainReport { partition, frontier, delivered, gap, skipped_truncations, status }`
- `AckOutcome::{AlreadyConfirmed, Advanced}`、`RecoveryError`
- `RecoverySnapshot` / `PartitionSnapshot` / `PendingMirrored`

### `RecoveryManager`（单线程所有者）

| 方法 | 作用 |
| --- | --- |
| `new()` | 创建空管理器 |
| `ensure_partition(p, initial_frontier)` | 建立分区；幂等，拒绝前沿回退 |
| `ingest(p, MirrorEvent)` | 写入 B 侧事件，返回归类结果 |
| `report_truncation(p, TruncatedRange)` | 登记裁剪区间（自动有序合并） |
| `drain(p, max_deliveries)` | 投递可证明消息并给出待补区间 |
| `ack_mirrored(p, source_offset)` | B 侧确认镜像消息（仅连续前缀可推进） |
| `ack_native(p, b_offset)` | 确认 B 侧原生消息 |
| `switch_back_ack(p, source_offset)` | 切回 A 后的连续确认 |
| `frontier(p)` / `truncations(p)` / `partitions()` | 只读查询 |
| `snapshot()` / `RecoveryManager::restore(..)` | 快照导出与恢复 |

### `ThreadSafeRecovery`

`Mutex<RecoveryManager>` 的线程安全包装，方法集与 `RecoveryManager` 对齐，
用于多线程交错的 ingest/drain/ack 场景。

### 快照文本格式（`src/snapshot.rs`）

```text
MIRROR-RECOVERY-SNAPSHOT-V1
P <partition> <frontier>
T <partition> <from> <to>
M <partition> <b_offset> <source_offset> <hex-id> <hex-payload>
N <partition> <b_offset> <hex-payload>
I <partition> <b_offset> <hex-payload>
K <partition> <b_offset>
END
```

- `M`：已投递未确认的在途镜像（重开后按 at-least-once 重投）
- `N`：已接收未投递的 B 侧原生消息；`I`：在途原生消息；`K`：已确认原生位点
- 身份与负载均为小写十六进制编码，任意字节可安全落盘
- 编解码入口：`mirror_recovery::snapshot::encode / decode`

**重开语义**：快照只持久化权威状态（前沿、裁剪、在途消息、原生消息集合）。
非在途的来源镜像缓冲不持久化，由 B 原始日志重放回填；重放事件中来源位点
不高于前沿的，统一被 `ingest` 判定为 `Replayed` 而不会再次投递。

## 4. 复现步骤

环境要求：Rust 1.95.0（工具链已在 `rust-toolchain` 约束之外通过
`Cargo.toml` 的 `rust-version = "1.95.0"` 声明），无第三方依赖。

```bash
# 离线运行全部测试（无网络访问、无依赖下载）
cargo test --offline

# 只运行题面核心的两次切换场景
cargo test --offline --test two_switch_scenario

# 边界与故障恢复
cargo test --offline --test boundary_and_recovery

# 并发交错
cargo test --offline --test concurrent_interleaving

# release 模式复核
cargo test --offline --release
```

## 5. 验证范围

所有结论均由真实测试断言得出，未使用任何预置的演示输出。

### 5.1 题面核心场景（`tests/two_switch_scenario.rs`，1 个集成测试）

构造 B **先收到高位点（9、10）再收到低位点（3、4）**、中间夹裁剪区间
`6..=8`、且存在未镜像位点 5 的场景，完整走两次切换：

1. **A→B**：drain 严格按来源位点先投 3 再投 4，B 到达顺序不影响投递顺序；
   位点 5 无证据时停止并返回 `Gap { from: 5, to: Some(6) }`，裁剪区间不被提前折叠。
2. 补齐位点 5 后，drain 投递 5、折叠裁剪 `6..=8`、投递 9、10，开放缺口从 11 开始。
3. **故障重开**：快照落盘再恢复，重放 B 原始日志，所有已确认事件判定为
   `Replayed`，无重复投递；新到消息 11 正常消费。
4. **B→A**：`switch_back_ack` 严格连续确认 12、13，乱序确认 13 被拒绝；
   前沿与 B 阶段共用。
5. **再次 A→B**：已确认的 12 重放为 `Replayed`；新数据 14、15 投递，
   裁剪 16 折叠，缺口 18 被正确报告并在补齐后继续，最终前沿 19。
6. 全局断言 B 侧投递集合恰好为 `{3,4,5,9,10,11,14,15,17,18,19}`，
   裁剪位点 `6,7,8,16` 从未作为消息投递，且任何来源位点不会投递两次。

### 5.2 边界与故障恢复（`tests/boundary_and_recovery.rs`，15 个测试）

- 未知分区的所有操作均报错；非法裁剪区间（`from > to`）被拒绝
- 空镜像：返回开放缺口、不投递
- 裁剪区间相邻/重叠自动合并；裁剪区直接在前沿前时无缺口折叠
- 裁剪区内存在镜像实体时实体优先投递，不被静默丢弃
- 同身份不同位点、同位点不同身份的证据冲突被隔离（`Conflict`）
- 确认未投递/仅缓冲的消息被拒绝；重复确认幂等
- 在途消息阻断重复投递，且远处缺口仍可被报告
- 快照往返：在途镜像重投（at-least-once）、同会话不重发；原生消息
  未确认重投、已确认重放丢弃、负载经十六进制编解码无损保留
- 分区之间前沿独立；`ensure_partition` 幂等且拒绝前沿回退
- `u64::MAX` 边界折叠不发生死循环
- 切回 A 的确认自动折叠其后的裁剪区间

### 5.3 并发交错（`tests/concurrent_interleaving.rs`，1 个测试）

4 线程经 `ThreadSafeRecovery` 交错执行：两个竞争消费者 drain+ack、
一个重放线程、一个并发裁剪报告线程。断言：

- 两个消费者的投递集合互斥，合并后恰好覆盖全部非裁剪位点（1..=200 去掉 50..=59）
- 前沿单调推进到 200，最终 drain 无重复投递
- 重放线程实际观察到 `Replayed` 路径，重复镜像在并发下仍被识别

### 5.4 快照编解码单元测试（`src/snapshot.rs`，2 个测试）

- 全部 256 个字节值的十六进制往返、空负载处理
- 损坏输入（错误头、缺 END、尾随记录、未知记录类型、奇数长度 hex、
  非法 UTF-8 身份、重复分区/位点等）全部返回 `RecoveryError::Snapshot`

## 6. 范围与限制

- 仅使用 Rust 1.95.0 标准库，无第三方依赖，支持完全离线构建测试。
- 模块只负责位点换算与恢复状态机，不包含真实网络 I/O、消息存储或
  A/B 代理实现；所有输入由调用方以 `MirrorEvent` 喂入。
- 投递语义为 **at-least-once**：正常会话内靠在途集合防重，进程崩溃重开后
  未确认的在途消息会重新投递，消费者需自行做幂等处理。
- 所有测试数据均为本地合成，不访问任何真实消息系统、账户或网络目标。
