//! 局部压实库（local compaction）。
//!
//! 模型概述见 [`model`]，读取语义见 [`read`]，压实入口见 [`compaction`]，
//! 前后等价性验证见 [`verify`]，可回放小盘面见 [`fixture`]。

pub mod compaction;
pub mod error;
pub mod fixture;
pub mod interval;
pub mod model;
pub mod read;
pub mod verify;

pub use compaction::{compact, CompactionInput, CompactionOutput, ReclaimEntry, ReclaimReason};
pub use error::EngineError;
pub use interval::{Bound, KeyRange};
pub use model::{
    DeepLayer, PointOp, RangeTombstone, Record, RecordId, Segment, Value,
};
pub use read::{read_symbolic, read_with_deep_values, DeepAssignment, SymbolicValue};
pub use verify::{equivalence_report, VerificationReport};
