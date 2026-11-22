# membership-commit — 可重放的联合共识日志提交判定器

针对 Raft 联合共识（joint consensus）换员过程的确定性提交判定库。
场景：旧组 `{A,B,C}` 切换到新组 `{C,D,E}`；收到 A、B、C 的确认时，
联合阶段仍不能提交，因为新组多数（C/D/E 中至少 2 票）没有满足。

纯 Rust 1.95.0 + 标准库实现，**无任何第三方依赖**，可离线构建与测试。

## 建模的判定规则

1. **两步换员**：先追加换员开始条目 `C_old,new`，再追加换员完成条目 `C_new`。
2. **双重多数**：换员开始条目与联合阶段的所有新条目（含完成条目），必须
   **分别**拿到旧组多数和新组多数；共有成员（如 C）在两组里分别计入。
   仅满足“并集上的多数”不够（测试 `union_majority_is_not_joint_majority`）。
3. **配置切换时点**：完成条目在联合规则下提交后新组才启用；此后旧组的确认
   对新条目没有投票权（`AckState::NonVoter`），不能再推动提交。
4. **领导任期约束**：领导只能凭**本任期**且满足规则的条目推进提交边界，
   并同时提交其前缀；旧任期条目只能随本任期条目搭车提交。
5. **确认身份校验**：确认必须同时匹配前缀下标和该下标处的条目任期。
   旧任期迟到确认允许记录，但标记为 `AckState::TermMismatch`，不计入多数。
6. **尾部替换**：换任期后可截断未提交尾部重写；已提交日志与快照受保护。
7. **快照压缩**：只能压缩已提交前缀；快照保留压缩位置的配置、未完成换员的
   证据（`PendingJoint`）以及压缩后的日志尾部，恢复后联合规则继续生效。
8. **禁止重开**：完成条目确认前重开新换员提议返回 `DeciderError::JointInProgress`。
9. **可重放**：判定器无随机量、无隐藏状态，同一批 `ReplayOp` 重放任意次，
   `Evaluation` 结果逐字节相同（`PartialEq` 可直接断言）。

## 公开 API

### 核心类型（`membership_commit::types`）

| 类型 | 说明 |
| --- | --- |
| `Config` | 成员配置；`majority()` 返回 `floor(n/2)+1`，`intersection()` 求共有成员 |
| `Entry` / `EntryKind` | 带任期日志条目：`Normal`、`BeginJoint { old, new }`、`EndJoint { new }` |
| `Ack` | 副本确认：`from`、`prefix_index`、`prefix_term`（前缀身份） |
| `Proposal` | 换员提议：`term` + 目标 `new` 配置 |

### 判定器（`membership_commit::Decider`）

```rust
let mut d = Decider::new(leader_term, Config::new(["A","B","C"]))?;
d.append_normal(2)?;                       // 普通条目，下标自动分配
d.propose(2, Config::new(["C","D","E"]))?; // 换员开始条目（重开会被拒）
d.append_normal(2)?;                       // 联合阶段条目
d.append_end_joint(2)?;                    // 换员完成条目（配置自动对齐）
d.record_ack(Ack { from: "A".into(), prefix_index: 3, prefix_term: 2 })?;
d.truncate_uncommitted(2)?;                // 换任期后替换未提交尾部
let ev: Evaluation = d.evaluate();         // 全量判定
let snap = d.compact(ev.commit_index)?;    // 压缩已提交前缀
let restored = Decider::restore(snap, 3);  // 从快照恢复
```

### 判定输出（`membership_commit::report`）

- `Evaluation`：`leader_term`、`commit_index`（提交位点）、`effective_config`
  （`Old` / `Joint { old, new }` / `New`）、`pending_membership_change`、逐条 `EntryReport`。
- `EntryReport`：适用规则 `CommitRule`（`OldOnly`/`Joint`/`NewOnly`）、
  旧组与新组各自的 `GroupQuorum`（`needed`/`matched`/`satisfied`/`missing`）、
  是否提交、是否被领导任期挡住、每个副本的 `AckState`
  （`Matching`/`TermMismatch`/`Behind`/`NonVoter`），以及 `missing_members()` 缺口汇总。

### 快照（`membership_commit::snapshot`）

`Snapshot { base_index, base_term, config, pending_joint, trailing_log }`，
其中 `pending_joint: Option<PendingJoint>` 就是未完成换员必须保留的证据。

### 整体重放（`membership_commit::{replay, ReplayOp}`）

`ReplayOp::{Append, Propose, Ack, Truncate}` 描述一串输入；
`replay(leader_term, initial_config, ops)` 一步不漏地重放并返回 `Evaluation`，
失败时 `ReplayError { step, source }` 指出分歧步骤。

## 复现步骤

```bash
rustc --version   # rustc 1.95.0
cargo test --offline
```

无外部依赖、无网络访问；`Cargo.lock` 仅含本 crate。

## 验证范围

`tests/commit_decider.rs` 共 10 个真实断言测试（无预置输出、无 mock 网络）：

| 测试 | 类别 | 验收点 |
| --- | --- | --- |
| `union_majority_is_not_joint_majority` | 交错一 | 并集多数不够；新旧两组分别计票；共有成员 C 两边计入；缺口为 D、E；补 D 后立即放行；提交位点与有效配置逐段核对 |
| `stale_old_term_ack_cannot_advance_commit` | 交错二 | 旧任期确认不能直接推进提交；错任期确认标 `TermMismatch` 不计票；本任期条目满足规则后旧前缀搭车提交；新组启用后旧组确认标 `NonVoter`，新组多数到位才继续 |
| `reopen_before_completion_is_rejected` | 交错三 | 完成条目确认前后两次重开均被拒；压缩已提交前缀后快照保留 `PendingJoint`；从快照恢复后联合规则继续、重开仍被拒、补齐双方多数后完成换员 |
| `full_membership_change_happy_path` | 正常路径 | 五条目跨旧/联合/新三阶段的规则归属、配置切换与新组独立计票 |
| `majority_boundary_one_vote_short` | 边界 | 3 人组 2 票、4 人组 3 票门槛，差一票不提交，缺口成员列表正确 |
| `single_node_and_empty_config` | 边界 | 单节点一票即多数；空配置构造即报错 |
| `uncommitted_tail_replacement_after_term_change` | 故障恢复 | 截断未提交尾部、旧确认失效、截断已提交日志被拒、截断带走开始条目后证据清除且可重新提议 |
| `snapshot_compaction_rules` | 故障恢复 | 未提交位置禁止压缩；完成后压缩快照配置为新组且无悬挂证据；恢复后新日志按新组规则判定 |
| `replay_is_deterministic_and_equivalent` | 可重放 | 同一输入重放 6 次结果完全相等；`replay` 与分步 API 结果一致 |
| `structural_errors` | 负面输入 | 任期回退、无开始条目的完成条目、完成条目配置不一致均被拒绝 |

### 明确不在验证范围内的内容

- 真实网络、RPC、持久化磁盘与时钟：本库只消费“日志 + 确认”的抽象输入。
- 选举、心跳、流量转发与客户端协议：仅实现日志提交判定与配置切换。
- 未执行的测试不存在；上表全部测试均由 `cargo test --offline` 实际运行通过。
