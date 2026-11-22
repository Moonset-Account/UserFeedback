//! # membership-commit
//!
//! 可重放的 Raft 联合共识（joint consensus）日志提交判定器。
//!
//! ## 建模的规则
//!
//! 1. 换员分两步：换员开始条目 `C_old,new` 与换员完成条目 `C_new`。
//! 2. 换员开始条目、以及联合阶段的所有新条目，必须**分别**拿到旧组多数
//!    和新组多数；两组的共有成员可以分别计入两边。
//! 3. 完成条目仍在联合规则下判定，只有它提交后新组才启用；
//!    此后旧组的确认不能再推动新条目。
//! 4. 领导只能凭**本任期**且满足规则的条目推进提交边界，并同时提交其前缀；
//!    旧任期条目只能随本任期条目搭车提交。
//! 5. 换任期后未提交尾部可以截断替换；确认必须同时匹配前缀下标与条目任期，
//!    旧任期迟到确认会被记为 `TermMismatch`，不计入多数。
//! 6. 快照只能压缩已提交前缀，并保留压缩位置的配置与未完成换员的证据。
//! 7. 完成条目确认前重开换员提议会被拒绝。
//!
//! 整个判定器没有内部随机性与隐藏状态：同一批操作重放任意次，
//! [`Evaluation`] 结果逐字节相同。

pub mod decider;
pub mod report;
pub mod snapshot;
pub mod types;

pub use decider::{Decider, DeciderError, StoredEntry};
pub use report::{AckState, CommitRule, EffectiveConfig, EntryReport, Evaluation, GroupQuorum};
pub use snapshot::{PendingJoint, Snapshot};
pub use types::{Ack, Config, Entry, EntryKind, LogIndex, NodeId, Proposal, Term};

/// 一次重放中的单步输入。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ReplayOp {
    /// 追加一条带任期的日志条目（普通 / 开始 / 完成由条目类型决定）。
    Append(Entry),
    /// 换员提议：在当前日志尾部追加换员开始条目。
    Propose(Proposal),
    /// 记录一个副本的匹配前缀确认。
    Ack(Ack),
    /// 换任期后截断未提交尾部（从该下标起丢弃）。
    Truncate(LogIndex),
}

/// 把一串输入确定性地重放进一个新判定器，并返回最终判定结果。
///
/// 任何一步出现结构性错误都会立即返回，错误中带有所在步骤的序号，
/// 便于定位重放分歧点。
pub fn replay(
    leader_term: Term,
    initial_config: Config,
    ops: &[ReplayOp],
) -> Result<Evaluation, ReplayError> {
    let mut decider =
        Decider::new(leader_term, initial_config).map_err(|e| ReplayError { step: 0, source: e })?;
    for (i, op) in ops.iter().enumerate() {
        let step = i + 1;
        match op {
            ReplayOp::Append(entry) => {
                decider
                    .append_entry(entry.clone())
                    .map_err(|source| ReplayError { step, source })?;
            }
            ReplayOp::Propose(proposal) => {
                decider
                    .propose(proposal.term, proposal.new.clone())
                    .map_err(|source| ReplayError { step, source })?;
            }
            ReplayOp::Ack(ack) => {
                decider
                    .record_ack(ack.clone())
                    .map_err(|source| ReplayError { step, source })?;
            }
            ReplayOp::Truncate(from) => {
                decider
                    .truncate_uncommitted(*from)
                    .map_err(|source| ReplayError { step, source })?;
            }
        }
    }
    Ok(decider.evaluate())
}

/// 重放过程中某一步失败。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ReplayError {
    /// 出错步骤（从 1 开始；0 表示构造判定器时失败）。
    pub step: usize,
    /// 具体错误。
    pub source: DeciderError,
}

impl std::fmt::Display for ReplayError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "重放第 {} 步失败：{}", self.step, self.source)
    }
}

impl std::error::Error for ReplayError {}
