//! 可重放的日志提交判定器。
//!
//! 判定器只接收四类输入：领导任期、带任期的日志条目、各副本的匹配前缀确认、
//! 换员提议。所有判定都是确定性的纯函数式状态转移，可随时用同一批输入重放。

use std::collections::BTreeMap;
use std::fmt;

use crate::report::{
    AckState, CommitRule, EffectiveConfig, EntryReport, Evaluation, GroupQuorum,
};
use crate::snapshot::PendingJoint;
use crate::types::{Ack, Config, Entry, EntryKind, LogIndex, NodeId, Term};

/// 判定器可识别的结构性错误（数据本身矛盾，无法构成合法重放）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum DeciderError {
    /// 配置为空，无法定义多数。
    EmptyConfig,
    /// 追加条目的任期比日志尾部任期更旧。
    TermRegression { append: Term, last: Term },
    /// 没有进行中的换员，却追加了换员完成条目。
    EndJointWithoutBegin,
    /// 已有未完成的换员，不能在完成条目提交前重开新换员。
    JointInProgress,
    /// 完成条目的新配置与开始条目声明的新配置不一致。
    ConfigMismatch { expected: Config, got: Config },
    /// 确认落在快照基址之前，前缀身份已无法校验。
    PrefixBeforeSnapshot { prefix: LogIndex, base: LogIndex },
    /// 确认的下标超出当前日志长度。
    UnknownLogIndex { prefix: LogIndex, last: LogIndex },
    /// 压缩目标不超过已有快照基址。
    AlreadyCompacted { requested: LogIndex, base: LogIndex },
    /// 只能压缩已提交前缀。
    CompactionBeyondCommit {
        requested: LogIndex,
        commit_index: LogIndex,
    },
    /// 试图截断已提交日志。
    TruncateCommitted { from: LogIndex, commit_index: LogIndex },
    /// 截断起点不超过快照基址。
    TruncateBeforeSnapshot { from: LogIndex, base: LogIndex },
}

impl fmt::Display for DeciderError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            DeciderError::EmptyConfig => write!(f, "配置不能为空"),
            DeciderError::TermRegression { append, last } => {
                write!(f, "任期回退：追加条目任期 {append} 早于日志尾部任期 {last}")
            }
            DeciderError::EndJointWithoutBegin => {
                write!(f, "缺少换员开始条目，不能追加完成条目")
            }
            DeciderError::JointInProgress => {
                write!(f, "换员尚未完成，不能重开新的换员提议")
            }
            DeciderError::ConfigMismatch { expected, got } => {
                write!(f, "完成条目配置 {got} 与开始条目声明的 {expected} 不一致")
            }
            DeciderError::PrefixBeforeSnapshot { prefix, base } => {
                write!(f, "确认前缀 {prefix} 已被快照（基址 {base}）压缩，无法校验")
            }
            DeciderError::UnknownLogIndex { prefix, last } => {
                write!(f, "确认前缀 {prefix} 超出日志末尾 {last}")
            }
            DeciderError::AlreadyCompacted { requested, base } => {
                write!(f, "压缩位置 {requested} 不超过快照基址 {base}")
            }
            DeciderError::CompactionBeyondCommit {
                requested,
                commit_index,
            } => write!(
                f,
                "压缩位置 {requested} 超过提交边界 {commit_index}，未提交日志不能压缩"
            ),
            DeciderError::TruncateCommitted { from, commit_index } => {
                write!(f, "截断起点 {from} 已提交（提交边界 {commit_index}）")
            }
            DeciderError::TruncateBeforeSnapshot { from, base } => {
                write!(f, "截断起点 {from} 不超过快照基址 {base}")
            }
        }
    }
}

impl std::error::Error for DeciderError {}

/// 带位置的日志条目（内部存储形式）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StoredEntry {
    pub index: LogIndex,
    pub term: Term,
    pub kind: EntryKind,
}

struct JointBounds {
    begin_index: LogIndex,
    end_index: Option<LogIndex>,
    old: Config,
    new: Config,
}

/// 日志提交判定器。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Decider {
    pub(crate) leader_term: Term,
    /// 快照基址：`<= snapshot_base` 的日志已被压缩且视为已提交。
    pub(crate) snapshot_base: LogIndex,
    /// 快照基址处条目的任期。
    pub(crate) base_term: Term,
    /// 当前稳定配置（换员完成后会推进为新配置）。
    pub(crate) base_config: Config,
    /// 保留在内存中的日志（下标均大于快照基址，连续递增）。
    pub(crate) log: Vec<StoredEntry>,
    /// 各副本最近一次匹配前缀确认。
    pub(crate) acks: BTreeMap<NodeId, Ack>,
    /// 未完成换员的证据（快照后仅靠它恢复联合规则）。
    pub(crate) pending_joint: Option<PendingJoint>,
    /// 最近一次刷新得到的提交边界。
    pub(crate) cached_commit: LogIndex,
}

impl Decider {
    /// 从零创建判定器。`initial_config` 为换员前的旧组配置。
    pub fn new(leader_term: Term, initial_config: Config) -> Result<Self, DeciderError> {
        if initial_config.is_empty() {
            return Err(DeciderError::EmptyConfig);
        }
        Ok(Self {
            leader_term,
            snapshot_base: 0,
            base_term: 0,
            base_config: initial_config,
            log: Vec::new(),
            acks: BTreeMap::new(),
            pending_joint: None,
            cached_commit: 0,
        })
    }

    /// 从快照基址信息创建（供快照恢复使用）。
    pub(crate) fn with_snapshot(
        leader_term: Term,
        base_index: LogIndex,
        base_term: Term,
        config: Config,
    ) -> Self {
        Self {
            leader_term,
            snapshot_base: base_index,
            base_term,
            base_config: config,
            log: Vec::new(),
            acks: BTreeMap::new(),
            pending_joint: None,
            cached_commit: base_index,
        }
    }

    // ----- 基本访问器 -------------------------------------------------

    /// 当前领导任期。
    pub fn leader_term(&self) -> Term {
        self.leader_term
    }

    /// 快照基址。
    pub fn snapshot_base(&self) -> LogIndex {
        self.snapshot_base
    }

    /// 当前日志（不含已压缩部分）。
    pub fn entries(&self) -> &[StoredEntry] {
        &self.log
    }

    /// 日志最后一个下标（含快照基址）。
    pub fn last_index(&self) -> LogIndex {
        self.log
            .last()
            .map(|e| e.index)
            .unwrap_or(self.snapshot_base)
    }

    /// 日志最后一个条目的任期。
    pub fn last_term(&self) -> Term {
        self.log.last().map(|e| e.term).unwrap_or(self.base_term)
    }

    /// 读取某下标语义上的条目任期（快照基址用 `base_term`）。
    pub fn term_at(&self, index: LogIndex) -> Option<Term> {
        if index == self.snapshot_base {
            return Some(self.base_term);
        }
        self.log.iter().find(|e| e.index == index).map(|e| e.term)
    }

    /// 当前提交边界（会先按现有输入重新判定一次）。
    pub fn commit_index(&mut self) -> LogIndex {
        self.refresh();
        self.cached_commit
    }

    /// 是否存在未完成的换员。
    pub fn has_active_joint(&self) -> bool {
        self.joint_bounds().is_some_and(|b| {
            // 开始条目存在，且完成条目尚未提交
            b.end_index.is_none_or(|end| end > self.cached_commit)
        })
    }

    // ----- 日志追加 ---------------------------------------------------

    /// 追加任意条目（下标自动取日志末尾 + 1）。
    ///
    /// 换员类条目在此统一校验：开始条目要求当前没有进行中的换员；
    /// 完成条目要求存在开始条目、尚无完成条目，且新配置与开始条目一致。
    pub fn append_entry(&mut self, entry: Entry) -> Result<LogIndex, DeciderError> {
        self.validate_term(entry.term)?;
        let index = self.last_index() + 1;
        match &entry.kind {
            EntryKind::Normal => {}
            EntryKind::BeginJoint { old, new } => {
                if old.is_empty() || new.is_empty() {
                    return Err(DeciderError::EmptyConfig);
                }
                if self.has_active_joint() {
                    return Err(DeciderError::JointInProgress);
                }
                if *old != self.base_config {
                    return Err(DeciderError::ConfigMismatch {
                        expected: self.base_config.clone(),
                        got: old.clone(),
                    });
                }
            }
            EntryKind::EndJoint { new } => {
                let bounds = self
                    .joint_bounds()
                    .ok_or(DeciderError::EndJointWithoutBegin)?;
                if bounds.end_index.is_some() {
                    return Err(DeciderError::JointInProgress);
                }
                if *new != bounds.new {
                    return Err(DeciderError::ConfigMismatch {
                        expected: bounds.new,
                        got: new.clone(),
                    });
                }
            }
        }
        self.log.push(StoredEntry {
            index,
            term: entry.term,
            kind: entry.kind.clone(),
        });
        if let EntryKind::BeginJoint { old, new } = &entry.kind {
            self.pending_joint = Some(PendingJoint {
                begin_index: index,
                begin_term: entry.term,
                old: old.clone(),
                new: new.clone(),
            });
        }
        Ok(index)
    }

    /// 追加一条普通条目。
    pub fn append_normal(&mut self, term: Term) -> Result<LogIndex, DeciderError> {
        self.append_entry(Entry::normal(term))
    }

    /// 换员提议：校验当前没有进行中的换员，然后追加换员开始条目并留下证据。
    ///
    /// 在完成条目按联合规则提交之前重开新提议将被拒绝（交错场景三）。
    pub fn propose(
        &mut self,
        term: Term,
        new_config: Config,
    ) -> Result<LogIndex, DeciderError> {
        if new_config.is_empty() {
            return Err(DeciderError::EmptyConfig);
        }
        if self.has_active_joint() {
            return Err(DeciderError::JointInProgress);
        }
        let old = self.base_config.clone();
        self.append_entry(Entry::begin_joint(term, old, new_config))
    }

    /// 追加换员完成条目；只有在换员进行中、且新配置与开始条目一致时才允许。
    pub fn append_end_joint(&mut self, term: Term) -> Result<LogIndex, DeciderError> {
        let bounds = self
            .joint_bounds()
            .ok_or(DeciderError::EndJointWithoutBegin)?;
        if bounds.end_index.is_some() {
            return Err(DeciderError::JointInProgress);
        }
        let new = bounds.new.clone();
        self.append_entry(Entry::end_joint(term, new))
    }

    /// 换任期后替换未提交尾部：截断 `from_index` 起的日志。
    ///
    /// 已提交日志和快照受保护；被截断条目的旧确认保留在确认表中，
    /// 但会因前缀任期或下标对不上而在后续判定中自动失效。
    pub fn truncate_uncommitted(
        &mut self,
        from_index: LogIndex,
    ) -> Result<(), DeciderError> {
        self.refresh();
        if from_index <= self.snapshot_base {
            return Err(DeciderError::TruncateBeforeSnapshot {
                from: from_index,
                base: self.snapshot_base,
            });
        }
        if from_index <= self.cached_commit {
            return Err(DeciderError::TruncateCommitted {
                from: from_index,
                commit_index: self.cached_commit,
            });
        }
        self.log.retain(|e| e.index < from_index);
        // 若联合开始条目被截断，未完成换员的证据同步作废。
        if let Some(pending) = &self.pending_joint {
            if pending.begin_index >= from_index {
                self.pending_joint = None;
            }
        }
        // 截断后提交边界不可能前移，但缓存仍按新日志重算一次。
        self.refresh();
        Ok(())
    }

    // ----- 确认收集 ---------------------------------------------------

    /// 记录一个副本的匹配前缀确认。
    ///
    /// 前缀任期与本地日志不一致的确认（旧任期迟到确认）**允许记录**，
    /// 但在判定时只会得到 [`AckState::TermMismatch`]，不能计入多数。
    /// 快照基址处的确认可凭快照保留的 `base_term` 校验。
    pub fn record_ack(&mut self, ack: Ack) -> Result<(), DeciderError> {
        if ack.prefix_index < self.snapshot_base {
            return Err(DeciderError::PrefixBeforeSnapshot {
                prefix: ack.prefix_index,
                base: self.snapshot_base,
            });
        }
        if ack.prefix_index > self.last_index() {
            return Err(DeciderError::UnknownLogIndex {
                prefix: ack.prefix_index,
                last: self.last_index(),
            });
        }
        self.acks.insert(ack.from.clone(), ack);
        Ok(())
    }

    // ----- 判定 -------------------------------------------------------

    /// 全量判定：刷新提交边界，返回提交位点、有效配置与逐条缺口明细。
    pub fn evaluate(&mut self) -> Evaluation {
        self.refresh();
        let bounds = self.joint_bounds();
        let entries = self
            .log
            .iter()
            .map(|e| self.entry_report(e, &bounds))
            .collect();

        let effective_config = self.effect_config(&bounds);

        Evaluation {
            leader_term: self.leader_term,
            commit_index: self.cached_commit,
            effective_config,
            pending_membership_change: self.pending_joint.is_some(),
            entries,
        }
    }

    /// 当前有效配置（不改动状态）。
    pub fn effective_config(&mut self) -> EffectiveConfig {
        self.refresh();
        let bounds = self.joint_bounds();
        self.effect_config(&bounds)
    }

    // ----- 内部辅助 ---------------------------------------------------

    fn validate_term(&self, term: Term) -> Result<(), DeciderError> {
        if term < self.last_term() {
            return Err(DeciderError::TermRegression {
                append: term,
                last: self.last_term(),
            });
        }
        Ok(())
    }

    /// 汇总联合阶段边界：优先用快照保留的证据，再从日志中查找。
    fn joint_bounds(&self) -> Option<JointBounds> {
        if let Some(pending) = &self.pending_joint {
            let end_index = self
                .log
                .iter()
                .find(|e| {
                    matches!(&e.kind, EntryKind::EndJoint { new } if *new == pending.new)
                })
                .map(|e| e.index);
            return Some(JointBounds {
                begin_index: pending.begin_index,
                end_index,
                old: pending.old.clone(),
                new: pending.new.clone(),
            });
        }
        let begin = self.log.iter().find(|e| {
            matches!(e.kind, EntryKind::BeginJoint { .. })
        })?;
        let (old, new) = match &begin.kind {
            EntryKind::BeginJoint { old, new } => (old.clone(), new.clone()),
            _ => unreachable!(),
        };
        let end_index = self
            .log
            .iter()
            .find(|e| matches!(&e.kind, EntryKind::EndJoint { new: end_new } if *end_new == new))
            .map(|e| e.index);
        Some(JointBounds {
            begin_index: begin.index,
            end_index,
            old,
            new,
        })
    }

    /// 某下标适用的提交规则。
    fn rule_at(&self, index: LogIndex, bounds: &Option<JointBounds>) -> CommitRule {
        match bounds {
            None => CommitRule::OldOnly,
            Some(b) => {
                if index < b.begin_index {
                    CommitRule::OldOnly
                } else if b.end_index.is_none_or(|end| index <= end) {
                    // 联合开始条目、联合阶段新条目、联合完成条目都受联合规则约束。
                    CommitRule::Joint
                } else {
                    CommitRule::NewOnly
                }
            }
        }
    }

    /// 判定单个副本相对某下标的确认状态。
    fn ack_state_at(&self, node: &str, index: LogIndex) -> AckState {
        let Some(ack) = self.acks.get(node) else {
            return AckState::Behind;
        };
        if ack.prefix_index < index {
            return AckState::Behind;
        }
        match self.term_at(ack.prefix_index) {
            Some(term) if term == ack.prefix_term => AckState::Matching,
            Some(_) => AckState::TermMismatch,
            None => AckState::TermMismatch,
        }
    }

    /// 计算一个配置组在某下标上的多数满足情况。
    fn group_quorum(&self, group: &Config, index: LogIndex) -> GroupQuorum {
        let mut matched = 0usize;
        let mut missing = Vec::new();
        for node in group.members() {
            if self.ack_state_at(node, index) == AckState::Matching {
                matched += 1;
            } else {
                missing.push(node.clone());
            }
        }
        let needed = group.majority();
        GroupQuorum {
            group: group.members().to_vec(),
            needed,
            matched,
            satisfied: matched >= needed,
            missing,
        }
    }

    fn entry_report(&self, entry: &StoredEntry, bounds: &Option<JointBounds>) -> EntryReport {
        let rule = self.rule_at(entry.index, bounds);
        let (old_group, new_group) = match &rule {
            CommitRule::OldOnly => {
                (Some(self.group_quorum(&self.base_config, entry.index)), None)
            }
            CommitRule::Joint => {
                let b = bounds.as_ref().expect("联合规则必然存在联合边界");
                (
                    Some(self.group_quorum(&b.old, entry.index)),
                    Some(self.group_quorum(&b.new, entry.index)),
                )
            }
            CommitRule::NewOnly => {
                let b = bounds.as_ref().expect("新规则必然存在联合边界");
                (None, Some(self.group_quorum(&b.new, entry.index)))
            }
        };

        let rule_satisfied = match &rule {
            CommitRule::OldOnly | CommitRule::NewOnly => old_group
                .as_ref()
                .or(new_group.as_ref())
                .is_some_and(|g| g.satisfied),
            CommitRule::Joint => old_group.as_ref().is_some_and(|g| g.satisfied)
                && new_group.as_ref().is_some_and(|g| g.satisfied),
        };

        // 该条目实际生效的投票者集合。
        let voters: Config = match &rule {
            CommitRule::OldOnly => self.base_config.clone(),
            CommitRule::Joint => {
                let b = bounds.as_ref().expect("联合规则必然存在联合边界");
                let mut members = b.old.members().to_vec();
                for n in b.new.members() {
                    if !members.contains(n) {
                        members.push(n.clone());
                    }
                }
                Config::new(members)
            }
            CommitRule::NewOnly => {
                let b = bounds.as_ref().expect("新规则必然存在联合边界");
                b.new.clone()
            }
        };

        // 汇总所有相关副本：生效投票者 + 实际发过确认的副本。
        let mut nodes: Vec<NodeId> = voters.members().to_vec();
        nodes.extend(self.acks.keys().cloned());
        nodes.sort();
        nodes.dedup();
        let ack_states = nodes
            .into_iter()
            .map(|n| {
                let state = if !voters.contains(&n) {
                    // 不属于该条目生效配置的副本（如启用新组后的旧组成员），
                    // 即使发过确认也没有投票权。
                    AckState::NonVoter
                } else {
                    self.ack_state_at(&n, entry.index)
                };
                (n, state)
            })
            .collect();

        EntryReport {
            index: entry.index,
            term: entry.term,
            kind: entry.kind.clone(),
            rule,
            old_group,
            new_group,
            committed: entry.index <= self.cached_commit,
            rule_satisfied,
            blocked_by_leader_term: entry.term != self.leader_term,
            ack_states,
        }
    }

    fn effect_config(&self, bounds: &Option<JointBounds>) -> EffectiveConfig {
        match bounds {
            None => EffectiveConfig::Old(self.base_config.clone()),
            Some(b) => {
                if self.cached_commit < b.begin_index {
                    // 换员开始条目尚未提交：对外仍是旧配置。
                    EffectiveConfig::Old(b.old.clone())
                } else if b.end_index.is_some_and(|end| self.cached_commit >= end) {
                    // 完成条目已在联合规则下提交：新配置启用。
                    EffectiveConfig::New(b.new.clone())
                } else {
                    EffectiveConfig::Joint {
                        old: b.old.clone(),
                        new: b.new.clone(),
                    }
                }
            }
        }
    }

    /// 按 Raft 规则刷新提交边界，并在换员完成时推进稳定配置。
    pub(crate) fn refresh(&mut self) {
        let bounds = self.joint_bounds();
        let mut commit = self.snapshot_base;
        let mut end_committed: Option<Config> = None;

        for entry in &self.log {
            let rule = self.rule_at(entry.index, &bounds);
            let satisfied = match &rule {
                CommitRule::OldOnly => self
                    .group_quorum(&self.base_config, entry.index)
                    .satisfied,
                CommitRule::Joint => {
                    let b = bounds.as_ref().expect("联合规则必然存在联合边界");
                    self.group_quorum(&b.old, entry.index).satisfied
                        && self.group_quorum(&b.new, entry.index).satisfied
                }
                CommitRule::NewOnly => {
                    let b = bounds.as_ref().expect("新规则必然存在联合边界");
                    self.group_quorum(&b.new, entry.index).satisfied
                }
            };

            // 领导只能凭本任期条目推进提交边界；旧任期条目只能随前缀一起提交。
            let leader_can_advance = entry.term == self.leader_term;
            if satisfied && leader_can_advance {
                commit = entry.index;
            }

            if let EntryKind::EndJoint { new } = &entry.kind {
                if entry.index <= commit {
                    end_committed = Some(new.clone());
                }
            }
        }

        self.cached_commit = commit;
        if let Some(new) = end_committed {
            self.base_config = new;
            self.pending_joint = None;
        }
    }
}
