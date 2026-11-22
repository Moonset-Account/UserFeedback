//! 核心数据类型。
//!
//! 整个判定器是确定性的纯状态机：相同的输入序列无论重放多少次，
//! 都会得到完全相同的提交位点、有效配置和缺口报告。

use std::fmt;

/// 领导任期（Raft term），单调递增，0 为非法值。
pub type Term = u64;
/// 日志下标，从 1 开始连续编号。
pub type LogIndex = u64;

/// 副本标识。这里用字符串别名，允许 `'A'`、`"node-7"` 等命名。
pub type NodeId = String;

/// 一组成员配置（投票者集合）。
#[derive(Debug, Clone, PartialEq, Eq, Hash, Default)]
pub struct Config {
    /// 配置中的全部副本，按插入顺序保存（内部判定只依赖集合语义）。
    voters: Vec<NodeId>,
}

impl Config {
    /// 用成员列表构造配置；重复成员会被去重。
    pub fn new<I, S>(members: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: Into<NodeId>,
    {
        let mut voters = Vec::new();
        for m in members {
            let node = m.into();
            if !voters.contains(&node) {
                voters.push(node);
            }
        }
        Self { voters }
    }

    /// 配置是否包含某副本。
    pub fn contains(&self, node: &str) -> bool {
        self.voters.iter().any(|n| n == node)
    }

    /// 配置中的成员数量。
    pub fn len(&self) -> usize {
        self.voters.len()
    }

    /// 空配置（仅用于内部默认值，业务上不允许）。
    pub fn is_empty(&self) -> bool {
        self.voters.is_empty()
    }

    /// 成员视图（保持稳定顺序，便于确定性输出）。
    pub fn members(&self) -> &[NodeId] {
        &self.voters
    }

    /// 该配置的多数门槛：`floor(n/2) + 1`。空配置的多数定义为 1（即不可达）。
    pub fn majority(&self) -> usize {
        self.voters.len() / 2 + 1
    }

    /// 两个配置的交集（共有成员），顺序以本配置为准。
    pub fn intersection(&self, other: &Config) -> Vec<NodeId> {
        self.voters
            .iter()
            .filter(|n| other.contains(n))
            .cloned()
            .collect()
    }
}

impl fmt::Display for Config {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{{{}}}", self.voters.join(","))
    }
}

impl<const N: usize> From<[&str; N]> for Config {
    fn from(value: [&str; N]) -> Self {
        Config::new(value)
    }
}

/// 日志条目类型。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum EntryKind {
    /// 普通业务条目。
    Normal,
    /// 换员开始条目（`C_old,new`）：自该条目起进入联合阶段，
    /// 必须同时拿到旧组多数与新组多数才可提交。
    BeginJoint { old: Config, new: Config },
    /// 换员完成条目（`C_new`）：在联合规则下提交后，新配置才正式生效。
    EndJoint { new: Config },
}

impl EntryKind {
    pub fn is_normal(&self) -> bool {
        matches!(self, EntryKind::Normal)
    }
}

/// 带任期的日志条目。下标由它在日志中的位置决定，不存放在条目内部。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Entry {
    /// 条目所属任期。
    pub term: Term,
    /// 条目内容（普通 / 换员开始 / 换员完成）。
    pub kind: EntryKind,
}

impl Entry {
    /// 构造普通条目。
    pub fn normal(term: Term) -> Self {
        Self {
            term,
            kind: EntryKind::Normal,
        }
    }

    /// 构造换员开始条目。
    pub fn begin_joint(term: Term, old: Config, new: Config) -> Self {
        Self {
            term,
            kind: EntryKind::BeginJoint { old, new },
        }
    }

    /// 构造换员完成条目。
    pub fn end_joint(term: Term, new: Config) -> Self {
        Self {
            term,
            kind: EntryKind::EndJoint { new },
        }
    }
}

/// 副本对“已持久化的匹配前缀”的确认。
///
/// 只有当本副本在 `prefix_index` 处的日志条目任期确实等于 `prefix_term`
/// 时，这个确认才匹配（Raft 的日志匹配性质）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Ack {
    /// 发出确认的副本。
    pub from: NodeId,
    /// 已持久化日志前缀的最后一个下标。
    pub prefix_index: LogIndex,
    /// 该下标处条目的任期（用于前缀身份校验）。
    pub prefix_term: Term,
}

/// 换员提议：把当前（旧）配置切换为 `new` 配置。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Proposal {
    /// 提议所在的领导任期。
    pub term: Term,
    /// 目标新配置。
    pub new: Config,
}
