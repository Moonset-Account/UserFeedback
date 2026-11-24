//! 库内统一错误类型。纯标准库，故自行实现 `Error`。

use std::error::Error;
use std::fmt;

use crate::interval::Bound;

/// 输入不合法或压实/读取过程中违反不变量时返回的错误。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum EngineError {
    /// 半开区间起点不小于终点（`None` 表示 ±∞）。
    InvalidRange { start: Bound, end: Bound },
    /// 段深度重复（同一深度只能有一个段/层）。
    DuplicateDepth(usize),
    /// 记录序号在同一压实输入内不唯一。
    DuplicateSequence(u64),
    /// 开放快照序号晚于当前读序号。
    SnapshotAfterRead { snapshot: u64, read_seq: u64 },
    /// 压实输入的输出边界不单调（起点必须等于上一段终点）。
    NonAdjacentBoundaries,
    /// 记录引用了不存在的证据来源（内部不变量被破坏）。
    MissingEvidence(u64),
    /// 具体读取时，某个深层未知基值没有给出指派。
    MissingDeepValue { depth: usize, key: Vec<u8> },
    /// 输入记录既未出现在输出证据中、也未进入回收清单（闭环不变量被破坏）。
    UnaccountedRecord(u64),
}

impl fmt::Display for EngineError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            EngineError::InvalidRange { start, end } => {
                write!(f, "invalid half-open range [{start:?}, {end:?})")
            }
            EngineError::DuplicateDepth(d) => write!(f, "duplicate segment depth {d}"),
            EngineError::DuplicateSequence(s) => {
                write!(f, "duplicate record sequence {s}")
            }
            EngineError::SnapshotAfterRead { snapshot, read_seq } => write!(
                f,
                "open snapshot sequence {snapshot} is later than read sequence {read_seq}"
            ),
            EngineError::NonAdjacentBoundaries => {
                write!(f, "output segment boundaries are not adjacent and ordered")
            }
            EngineError::MissingEvidence(id) => {
                write!(f, "output record references unknown input record {id}")
            }
            EngineError::MissingDeepValue { depth, key } => write!(
                f,
                "no concrete value supplied for unknown deep base at depth {depth}, key {key:?}"
            ),
            EngineError::UnaccountedRecord(id) => write!(
                f,
                "input record {id} is neither evidenced in output nor reclaimed"
            ),
        }
    }
}

impl Error for EngineError {}
