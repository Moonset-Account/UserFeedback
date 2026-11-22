//! 快照压缩：压缩已提交前缀时，必须保留压缩位置的配置，
//! 以及未完成换员的证据（联合开始条目里的旧/新配置）。

use crate::decider::{Decider, StoredEntry};
use crate::types::{Config, LogIndex, Term};

/// 进行中换员的证据。只要换员未完成，快照就必须原样保留它，
/// 这样重放者才能继续执行联合规则，并拒绝在完成前重开新换员。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PendingJoint {
    /// 联合开始条目的下标。
    pub begin_index: LogIndex,
    /// 联合开始条目的任期。
    pub begin_term: Term,
    /// 旧配置。
    pub old: Config,
    /// 新配置。
    pub new: Config,
}

/// 压缩后的日志快照。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Snapshot {
    /// 快照覆盖到的最后一个日志下标（该位置及之前均已提交）。
    pub base_index: LogIndex,
    /// 基址处条目的任期（供后续确认做前缀身份校验）。
    pub base_term: Term,
    /// 压缩位置上的有效配置。
    /// - 压缩时处于旧/新阶段：就是当时生效的稳定配置；
    /// - 压缩时处于联合阶段：稳定配置仍是旧配置，联合信息另存于 `pending_joint`。
    pub config: Config,
    /// 未完成换员的证据；换员已完成或尚未开始时为 `None`。
    pub pending_joint: Option<PendingJoint>,
    /// 压缩后仍保留在内存中的日志尾部（下标均大于 `base_index`）。
    /// 快照必须连同它们一起带走，恢复后才能继续对未提交条目做判定。
    pub trailing_log: Vec<StoredEntry>,
}

impl Decider {
    /// 把 `through_index` 及之前的已提交日志压缩进快照。
    ///
    /// 约束：
    /// - 只能压缩已提交前缀（`through_index <= commit_index`）；
    /// - 压缩位置的配置和未完成换员的证据都会被保留；
    /// - 压缩后日志保留 `through_index` 之后的条目，确认记录原样保留
    ///  （落在基址之前的确认在后续判定中自然失效或被新确认覆盖）。
    pub fn compact(&mut self, through_index: LogIndex) -> Result<Snapshot, crate::decider::DeciderError> {
        if through_index < self.snapshot_base {
            return Err(crate::decider::DeciderError::AlreadyCompacted {
                requested: through_index,
                base: self.snapshot_base,
            });
        }
        let commit_index = self.commit_index();
        if through_index > commit_index {
            return Err(crate::decider::DeciderError::CompactionBeyondCommit {
                requested: through_index,
                commit_index,
            });
        }

        // 压缩位置的稳定配置：默认取当前基线配置；
        // 若完成条目正好位于压缩范围内，新配置此刻已生效。
        let mut config_at = self.base_config.clone();
        for entry in &self.log {
            if entry.index <= through_index {
                if let crate::types::EntryKind::EndJoint { new } = &entry.kind {
                    config_at = new.clone();
                }
            }
        }

        let trailing_log = self.log.clone();
        let snapshot = Snapshot {
            base_index: through_index,
            base_term: self.term_at(through_index).unwrap_or(0),
            config: config_at,
            pending_joint: self.pending_joint.clone(),
            trailing_log,
        };

        // 丢弃被压缩的条目；联合证据与日志尾部由快照接管。
        self.log.retain(|e| e.index > through_index);
        self.snapshot_base = through_index;
        self.base_config = snapshot.config.clone();
        Ok(snapshot)
    }

    /// 从快照恢复一个判定器，再继续追加条目/确认。
    /// `leader_term` 为恢复后的当前领导任期。
    pub fn restore(snapshot: Snapshot, leader_term: Term) -> Self {
        let mut decider = Decider::with_snapshot(
            leader_term,
            snapshot.base_index,
            snapshot.base_term,
            snapshot.config,
        );
        decider.pending_joint = snapshot.pending_joint;
        decider.log = snapshot.trailing_log;
        decider.refresh();
        decider
    }
}
