//! 键值引擎的数据模型。
//!
//! # 语义约定
//!
//! - 所有记录携带全局唯一、单调推进的序号（sequence）。读取某读序号 `r`
//!   时，只有 `seq <= r` 的记录可见。
//! - 层按 `depth` 排列：depth 越小越新。同 depth 的点记录按序号排序。
//! - 值是元素（`Vec<u8>`）组成的列表：
//!   - [`PointOp::Put`] 整值写入，写入空列表仍表示键**存在**；
//!   - [`PointOp::Delete`] 删除，键变为不存在；
//!   - [`PointOp::Append`] 向列表末尾追加一个元素；对不存在的键追加时
//!     以空列表起步（即产生一个单元素列表，键变为存在）；
//!   - [`RangeTombstone`] 是半开键区间上的删除，对区间内每个键等价于
//!     在其序号处执行一次点删除。

use crate::error::EngineError;
use crate::interval::KeyRange;

/// 输入记录的标识。压实证据用它回指输入记录。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct RecordId(pub u64);

/// 一次点操作。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum PointOp {
    /// 整值写入。空 `Vec` 也表示键存在（与“不存在”严格区分）。
    Put(Vec<Vec<u8>>),
    /// 点删除：键重置为不存在。
    Delete,
    /// 向列表末尾追加一个元素。
    Append(Vec<u8>),
}

/// 一条点记录：某个键上、某个序号的一次操作。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Record {
    pub id: RecordId,
    pub key: Vec<u8>,
    pub seq: u64,
    pub op: PointOp,
}

/// 范围墓碑：在 `range` 覆盖的每个键上、序号 `seq` 处的一次删除。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RangeTombstone {
    pub id: RecordId,
    pub seq: u64,
    pub range: KeyRange,
}

/// 一个有序段。`depth` 决定层间新旧关系（小者更新）。
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Segment {
    pub depth: usize,
    pub records: Vec<Record>,
    pub tombstones: Vec<RangeTombstone>,
}

impl Segment {
    /// 构造段并对内部记录按 (seq) 排序，同时做基础校验。
    pub fn new(
        depth: usize,
        records: Vec<Record>,
        tombstones: Vec<RangeTombstone>,
    ) -> Result<Self, EngineError> {
        let mut seg = Self { depth, records, tombstones };
        seg.records.sort_by_key(|r| r.seq);
        seg.tombstones.sort_by_key(|t| t.seq);
        Ok(seg)
    }
}

/// 未选入本次压实的深层：只知道它“可能在 `key_ranges` 内持有键”，
/// 旧基值的具体内容对压实器未知。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DeepLayer {
    /// 层深度；必须大于所有被选中段的 depth。
    pub depth: usize,
    /// 该层可能含键的并集区间列表。
    pub key_ranges: Vec<KeyRange>,
}

/// 读取得到的具体值。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Value {
    /// 键不存在（最近一次有效操作是删除，或从未出现）。
    Absent,
    /// 键存在，携带完整列表（可能为空列表）。
    Present(Vec<Vec<u8>>),
}

impl Value {
    /// 是否存在。注意空列表写入仍然存在。
    pub fn exists(&self) -> bool {
        matches!(self, Value::Present(_))
    }
}

/// 对选中段做通用结构校验（序号唯一、深度唯一、区间非空）。
pub(crate) fn validate_segments(
    selected: &[Segment],
    deep: &[DeepLayer],
) -> Result<(), EngineError> {
    let mut depths: Vec<usize> = Vec::new();
    let mut seqs = std::collections::HashSet::new();

    for seg in selected {
        if depths.contains(&seg.depth) {
            return Err(EngineError::DuplicateDepth(seg.depth));
        }
        depths.push(seg.depth);
        for r in &seg.records {
            if !seqs.insert(r.seq) {
                return Err(EngineError::DuplicateSequence(r.seq));
            }
        }
        for t in &seg.tombstones {
            if !seqs.insert(t.seq) {
                return Err(EngineError::DuplicateSequence(t.seq));
            }
        }
    }
    for d in deep {
        if depths.contains(&d.depth) {
            return Err(EngineError::DuplicateDepth(d.depth));
        }
        depths.push(d.depth);
        for r in &d.key_ranges {
            // 触发一次构造式校验。
            KeyRange::new(r.start.clone(), r.end.clone())?;
        }
    }
    Ok(())
}
