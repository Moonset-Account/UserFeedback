//! 判定输出报告类型。

use std::collections::BTreeSet;

use crate::types::{EntryKind, LogIndex, NodeId, Term};

/// 单个条目适用的提交规则。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CommitRule {
    /// 旧配置阶段：仅需旧组多数。
    OldOnly,
    /// 联合阶段：旧组多数 **和** 新组多数必须分别满足。
    Joint,
    /// 新配置阶段：仅需新组多数。
    NewOnly,
}

/// 当前时刻对外可见的有效配置。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum EffectiveConfig {
    /// 只有旧配置生效（换员条目尚未提交）。
    Old(crate::types::Config),
    /// 联合共识进行中：换员开始条目已提交、完成条目尚未提交。
    Joint {
        old: crate::types::Config,
        new: crate::types::Config,
    },
    /// 新配置已生效（完成条目已提交）。
    New(crate::types::Config),
}

impl EffectiveConfig {
    /// 是否处于联合阶段（新旧两套多数同时生效）。
    pub fn is_joint(&self) -> bool {
        matches!(self, EffectiveConfig::Joint { .. })
    }
}

/// 单个副本确认相对于某条目的状态。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AckState {
    /// 确认覆盖该条目，且前缀任期与本地日志匹配。
    Matching,
    /// 确认的下标覆盖该条目，但前缀任期对不上（旧任期迟到确认 / 分叉）。
    TermMismatch,
    /// 确认的前缀还没有到达该下标。
    Behind,
    /// 该副本根本不属于该条目的投票配置，无权计入。
    NonVoter,
}

/// 一个配置组的多数满足情况。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct GroupQuorum {
    /// 组内成员（确定性排序）。
    pub group: Vec<NodeId>,
    /// 多数需要的确认数。
    pub needed: usize,
    /// 实际匹配的确认数。
    pub matched: usize,
    /// 多数是否已经满足。
    pub satisfied: bool,
    /// 仍缺少的成员：组内尚未给出匹配确认的副本。
    pub missing: Vec<NodeId>,
}

/// 单条日志的判定明细。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EntryReport {
    /// 日志下标。
    pub index: LogIndex,
    /// 条目任期。
    pub term: Term,
    /// 条目类型（含联合配置内容）。
    pub kind: EntryKind,
    /// 适用的提交规则。
    pub rule: CommitRule,
    /// 旧组多数情况（联合阶段与旧阶段有意义）。
    pub old_group: Option<GroupQuorum>,
    /// 新组多数情况（联合阶段与新阶段有意义）。
    pub new_group: Option<GroupQuorum>,
    /// 该条目是否已经提交（被提交边界覆盖）。
    pub committed: bool,
    /// 规则上是否满足多数（提交的必要条件，但还需领导任期与前缀约束）。
    pub rule_satisfied: bool,
    /// 因不在本领导任期而无法由当前领导直接推进（旧任期条目只能随前缀提交）。
    pub blocked_by_leader_term: bool,
    /// 每个副本相对该条目的确认状态。
    pub ack_states: Vec<(NodeId, AckState)>,
}

impl EntryReport {
    /// 该条目若要提交，所有配置组中仍缺哪些成员（去重并集，确定性排序）。
    pub fn missing_members(&self) -> Vec<NodeId> {
        let mut all = BTreeSet::new();
        if let Some(g) = &self.old_group {
            all.extend(g.missing.iter().cloned());
        }
        if let Some(g) = &self.new_group {
            all.extend(g.missing.iter().cloned());
        }
        all.into_iter().collect()
    }
}

/// 一次重放判定的完整结果。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Evaluation {
    /// 判定时使用的领导任期。
    pub leader_term: Term,
    /// 当前提交边界（已提交的最高下标；无提交时为快照基址，通常为 0）。
    pub commit_index: LogIndex,
    /// 当前有效配置。
    pub effective_config: EffectiveConfig,
    /// 是否存在未完成的换员（开始已追加、完成未提交），快照必须保留其证据。
    pub pending_membership_change: bool,
    /// 逐条判定明细，按下标升序。
    pub entries: Vec<EntryReport>,
}
