//! 局部压实。
//!
//! 压实器只看见被选中段的完整记录，以及未选深层“可能含键”的区间。
//! 处理流程：
//!
//! 1. 收集读者点（所有开放快照序号 + 当前读序号）；
//! 2. 对每个出现过点记录的键，按 reset（Put/Delete/范围墓碑）切分 epoch，
//!    没有任何读者落入的 epoch 整体回收；
//! 3. 存活 epoch 内：已知基值上把读者不可区分的一串追加折叠成一条
//!    [`PointOp::Put`]；未知深层基值上保留原始追加；最后一个读者之后的
//!    尾部追加回收；
//! 4. 范围墓碑按“无读者需要且与深层区间不相交”证明可回收的子区间回收，
//!    其余部分按输出段边界拆分；
//! 5. 每条输出记录附上来源输入记录的证据；所有输入记录要么进入证据、
//!    要么进入回收清单，形成可校验闭环。

use std::collections::{BTreeMap, BTreeSet};

use crate::error::EngineError;
use crate::interval::{merge_ranges, split_at, KeyRange};
use crate::model::{
    validate_segments, DeepLayer, PointOp, RangeTombstone, Record, RecordId, Segment,
};

/// 压实输入。
#[derive(Debug, Clone)]
pub struct CompactionInput {
    /// 被选中参与压实的段（记录完整可见）。
    pub selected: Vec<Segment>,
    /// 未选深层，仅给出可能含键的区间。
    pub deep_layers: Vec<DeepLayer>,
    /// 当前读序号。
    pub read_seq: u64,
    /// 仍开放的快照序号（必须不晚于 `read_seq`）。
    pub open_snapshot_seqs: Vec<u64>,
    /// 输出段切点：按键字节序排列的具体键；`n` 个切点产生 `n+1` 个相邻段。
    pub output_boundaries: Vec<Vec<u8>>,
    /// 额外纳入等价性验证的键（例如只存在于深层区间内的探针键）。
    pub verification_keys: Vec<Vec<u8>>,
}

/// 输出点记录：合成后的记录 + 来源证据。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OutRecord {
    pub record: Record,
    pub evidence: Vec<RecordId>,
}

/// 输出范围墓碑：拆分后的片段 + 来源证据（当前实现每片段恰有一个来源）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OutRange {
    pub tombstone: RangeTombstone,
    pub evidence: Vec<RecordId>,
}

/// 一个压实输出段。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OutputSegment {
    pub depth: usize,
    pub range: KeyRange,
    pub records: Vec<OutRecord>,
    pub tombstones: Vec<OutRange>,
}

/// 单条输入记录被回收的原因。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ReclaimReason {
    /// 序号晚于当前读序号，任何读者都不可见。
    AboveReadSeq { seq: u64, read_seq: u64 },
    /// 所属 epoch 没有任何读者落入；`provably_no_deep_key` 表示已证明
    /// 深层区间不覆盖该键（epoch 内的删除因此确定无需遮蔽旧基值）。
    EpochWithoutReader {
        reset_seq: Option<u64>,
        provably_no_deep_key: bool,
    },
    /// 位于该 epoch 最后一个读者之后、且仍被后续 epoch 的 reset 限定在
    /// 本 epoch 内的事件：本 epoch 的任何读者都观察不到它。
    TailAfterLastReader { last_needed_seq: u64 },
    /// 追加操作数被折叠进序号 `into_seq` 的整值写入。
    FoldedIntoPut { into_seq: u64 },
    /// 范围墓碑的若干子区间被回收；全部子区间被回收即整条回收。
    RangeTombstoneDead {
        dropped_ranges: Vec<KeyRange>,
        provably_no_deep_key: bool,
    },
}

/// 一条回收记录。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ReclaimEntry {
    pub source: RecordId,
    pub reason: ReclaimReason,
}

/// 压实结果。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CompactionOutput {
    pub segments: Vec<OutputSegment>,
    pub reclaimed: Vec<ReclaimEntry>,
}

#[derive(Debug, Clone)]
enum ResetKind {
    Put(RecordId, Vec<Vec<u8>>),
    Delete(RecordId),
    Range(RecordId),
}

#[derive(Debug, Clone)]
struct ResetItem {
    seq: u64,
    kind: ResetKind,
}

#[derive(Debug, Clone)]
struct AppendItem {
    seq: u64,
    id: RecordId,
    elem: Vec<u8>,
}

#[derive(Debug, Clone)]
struct Epoch {
    reset: Option<ResetItem>,
    appends: Vec<AppendItem>,
}

impl Epoch {
    fn start_seq(&self) -> Option<u64> {
        match (&self.reset, self.appends.first()) {
            (Some(r), _) => Some(r.seq),
            (None, Some(a)) => Some(a.seq),
            (None, None) => None,
        }
    }
}

/// 合成输出记录使用的合成 id 区间，避免与输入 id 混淆。
const SYNTHETIC_ID_BASE: u64 = 1 << 32;

struct Compiler {
    shadow: Vec<KeyRange>,
    served: Vec<u64>,
    read_seq: u64,
    next_id: u64,
    out_records: Vec<OutRecord>,
    reclaimed: Vec<ReclaimEntry>,
    /// 范围墓碑 id -> 仍需要该墓碑的点键集合。
    rt_needed_keys: BTreeMap<RecordId, BTreeSet<Vec<u8>>>,
    evidenced: BTreeSet<RecordId>,
}

impl Compiler {
    fn synth_id(&mut self) -> RecordId {
        let id = RecordId(SYNTHETIC_ID_BASE + self.next_id);
        self.next_id += 1;
        id
    }

    fn shadow_contains(&self, key: &[u8]) -> bool {
        self.shadow.iter().any(|r| r.contains(key))
    }

    fn shadow_disjoint(&self, range: &KeyRange) -> bool {
        self.shadow.iter().all(|r| r.is_disjoint(range))
    }

    fn reclaim(&mut self, id: RecordId, reason: ReclaimReason) {
        self.reclaimed.push(ReclaimEntry { source: id, reason });
    }

    fn emit(&mut self, record: Record, evidence: Vec<RecordId>) {
        for e in &evidence {
            self.evidenced.insert(*e);
        }
        self.out_records.push(OutRecord { record, evidence });
    }

    /// 对单个键做 epoch 切分与折叠。
    fn compile_key(&mut self, key: &[u8], events: Vec<KeyEvent>) {
        // 1) 晚于读序号的记录直接回收。
        let mut visible: Vec<KeyEvent> = Vec::new();
        for ev in events {
            if ev.seq > self.read_seq {
                self.reclaim(
                    ev.primary_id(),
                    ReclaimReason::AboveReadSeq { seq: ev.seq, read_seq: self.read_seq },
                );
            } else {
                visible.push(ev);
            }
        }
        visible.sort_by_key(|e| e.seq);

        // 2) 切 epoch。
        let mut epochs: Vec<Epoch> = Vec::new();
        for ev in visible {
            match ev.kind {
                KeyEventKind::Put(id, value) => {
                    epochs.push(Epoch {
                        reset: Some(ResetItem { seq: ev.seq, kind: ResetKind::Put(id, value) }),
                        appends: Vec::new(),
                    });
                }
                KeyEventKind::Delete(id) => {
                    epochs.push(Epoch {
                        reset: Some(ResetItem { seq: ev.seq, kind: ResetKind::Delete(id) }),
                        appends: Vec::new(),
                    });
                }
                KeyEventKind::Range(id) => {
                    epochs.push(Epoch {
                        reset: Some(ResetItem { seq: ev.seq, kind: ResetKind::Range(id) }),
                        appends: Vec::new(),
                    });
                    self.rt_needed_keys
                        .entry(id)
                        .or_default()
                        .insert(key.to_vec()); // 先登记，未命中读者时再撤销
                }
                KeyEventKind::Append(id, elem) => match epochs.last_mut() {
                    Some(ep) => ep.appends.push(AppendItem { seq: ev.seq, id, elem }),
                    None => epochs.push(Epoch {
                        reset: None,
                        appends: vec![AppendItem { seq: ev.seq, id, elem }],
                    }),
                },
            }
        }

        // 3) 逐 epoch 判断读者并合成。
        let reset_seqs: Vec<Option<u64>> =
            epochs.iter().map(|e| e.reset.as_ref().map(|r| r.seq)).collect();
        for (idx, epoch) in epochs.iter().enumerate() {
            let Some(start) = epoch.start_seq() else { continue };
            let end = reset_seqs[idx + 1..].iter().flatten().copied().next();

            let readers: Vec<u64> = self
                .served
                .iter()
                .copied()
                .filter(|p| *p >= start && end.map_or(true, |e| *p < e))
                .collect();

            if readers.is_empty() {
                self.drop_epoch(key, epoch);
                // 撤销该 epoch 内范围墓碑对本键的“需要”登记。
                if let Some(r) = &epoch.reset {
                    if let ResetKind::Range(id) = r.kind {
                        if let Some(set) = self.rt_needed_keys.get_mut(&id) {
                            set.remove(key);
                        }
                    }
                }
                continue;
            }

            self.keep_epoch(key, epoch, &readers);
        }
    }

    fn drop_epoch(&mut self, key: &[u8], epoch: &Epoch) {
        let no_deep = !self.shadow_contains(key);
        let reset_seq = epoch.reset.as_ref().map(|r| r.seq);
        if let Some(r) = &epoch.reset {
            match &r.kind {
                ResetKind::Put(id, _) | ResetKind::Delete(id) => {
                    self.reclaim(
                        *id,
                        ReclaimReason::EpochWithoutReader {
                            reset_seq,
                            provably_no_deep_key: no_deep,
                        },
                    );
                }
                ResetKind::Range(_) => {} // 范围墓碑统一在墓碑阶段记账
            }
        }
        for a in &epoch.appends {
            self.reclaim(
                a.id,
                ReclaimReason::EpochWithoutReader {
                    reset_seq,
                    provably_no_deep_key: no_deep,
                },
            );
        }
    }

    /// 存活 epoch 的折叠合成。
    fn keep_epoch(&mut self, key: &[u8], epoch: &Epoch, readers: &[u64]) {
        let last_reader = *readers.last().unwrap();

        // 基值是否已知：非 Range/Delete 复位（Put 给值），或基 epoch 且深层无此键。
        let base_known = match &epoch.reset {
            Some(r) => matches!(r.kind, ResetKind::Put(..)),
            None => !self.shadow_contains(key),
        };

        // 先处理复位记录。
        if let Some(r) = &epoch.reset {
            match &r.kind {
                ResetKind::Put(id, value) => {
                    // group0 的追加可能直接并入这条 Put，先登记原始值占位。
                    let new_id = self.synth_id();
                    self.emit(
                        Record {
                            id: new_id,
                            key: key.to_vec(),
                            seq: r.seq,
                            op: PointOp::Put(value.clone()),
                        },
                        vec![*id],
                    );
                }
                ResetKind::Delete(id) => {
                    let new_id = self.synth_id();
                    self.emit(
                        Record {
                            id: new_id,
                            key: key.to_vec(),
                            seq: r.seq,
                            op: PointOp::Delete,
                        },
                        vec![*id],
                    );
                }
                ResetKind::Range(_) => {
                    // 范围墓碑本身在墓碑阶段输出，这里不产生点删除。
                }
            }
        }

        // 追加按读者分组：组号 = epoch 内严格早于该追加的读者数。
        let total_readers = readers.len();
        let mut groups: BTreeMap<usize, Vec<&AppendItem>> = BTreeMap::new();
        for a in &epoch.appends {
            if a.seq > last_reader {
                // 本 epoch 最后一个读者之后才出现：没有读者能观察到。
                self.reclaim(
                    a.id,
                    ReclaimReason::TailAfterLastReader { last_needed_seq: last_reader },
                );
                continue;
            }
            let index = readers.iter().filter(|p| **p < a.seq).count();
            debug_assert!(index < total_readers);
            groups.entry(index).or_default().push(a);
        }

        // 未知基值：保留每条追加，不做任何折叠（折叠会假定基值形态）。
        if !base_known {
            for group in groups.values() {
                for a in group {
                    let new_id = self.synth_id();
                    self.emit(
                        Record {
                            id: new_id,
                            key: key.to_vec(),
                            seq: a.seq,
                            op: PointOp::Append(a.elem.clone()),
                        },
                        vec![a.id],
                    );
                }
            }
            return;
        }

        // 已知基值：逐组重放，折叠为 Put。
        let mut state: Vec<Vec<u8>> = match &epoch.reset {
            Some(r) => match &r.kind {
                ResetKind::Put(_, v) => v.clone(),
                ResetKind::Delete(_) | ResetKind::Range(_) => Vec::new(),
            },
            None => Vec::new(),
        };

        for (group_index, group) in &groups {
            let evidence: Vec<RecordId> = group.iter().map(|a| a.id).collect();
            for a in group {
                state.push(a.elem.clone());
            }
            let first_seq = group.first().unwrap().seq;

            // group0 且复位本身是 Put：直接扩展那条 Put，不新增记录。
            if *group_index == 0 {
                if let Some(r) = &epoch.reset {
                    if let ResetKind::Put(put_id, _) = &r.kind {
                        let slot = self
                            .out_records
                            .iter_mut()
                            .find(|o| {
                                o.record.key.as_slice() == key
                                    && o.record.seq == r.seq
                                    && o.evidence.contains(put_id)
                            })
                            .expect("put reset record emitted above");
                        slot.record = Record {
                            id: slot.record.id,
                            key: key.to_vec(),
                            seq: r.seq,
                            op: PointOp::Put(state.clone()),
                        };
                        slot.evidence.extend(evidence.iter().copied());
                        for e in &evidence {
                            self.evidenced.insert(*e);
                        }
                        for a in group {
                            self.reclaim(a.id, ReclaimReason::FoldedIntoPut { into_seq: r.seq });
                        }
                        continue;
                    }
                }
            }

            // 其余组合成为一条新的整值写入。
            let new_id = self.synth_id();
            self.emit(
                Record {
                    id: new_id,
                    key: key.to_vec(),
                    seq: first_seq,
                    op: PointOp::Put(state.clone()),
                },
                evidence.clone(),
            );
            for a in group {
                self.reclaim(a.id, ReclaimReason::FoldedIntoPut { into_seq: first_seq });
            }
        }
    }
}

#[derive(Debug, Clone)]
enum KeyEventKind {
    Put(RecordId, Vec<Vec<u8>>),
    Delete(RecordId),
    Append(RecordId, Vec<u8>),
    Range(RecordId),
}

#[derive(Debug, Clone)]
struct KeyEvent {
    seq: u64,
    kind: KeyEventKind,
}

impl KeyEvent {
    fn primary_id(&self) -> RecordId {
        match &self.kind {
            KeyEventKind::Put(id, _)
            | KeyEventKind::Delete(id)
            | KeyEventKind::Append(id, _)
            | KeyEventKind::Range(id) => *id,
        }
    }
}

fn events_for_key(key: &[u8], selected: &[Segment]) -> Vec<KeyEvent> {
    let mut events = Vec::new();
    for seg in selected {
        for r in seg.records.iter().filter(|r| r.key.as_slice() == key) {
            let kind = match &r.op {
                PointOp::Put(v) => KeyEventKind::Put(r.id, v.clone()),
                PointOp::Delete => KeyEventKind::Delete(r.id),
                PointOp::Append(e) => KeyEventKind::Append(r.id, e.clone()),
            };
            events.push(KeyEvent { seq: r.seq, kind });
        }
        for t in seg.tombstones.iter().filter(|t| t.range.contains(key)) {
            events.push(KeyEvent { seq: t.seq, kind: KeyEventKind::Range(t.id) });
        }
    }
    events
}

/// 压实入口。
pub fn compact(input: &CompactionInput) -> Result<CompactionOutput, EngineError> {
    validate_segments(&input.selected, &input.deep_layers)?;

    // 输出切点必须严格递增。
    let mut boundaries = input.output_boundaries.clone();
    boundaries.sort();
    if boundaries.windows(2).any(|w| w[0] == w[1]) {
        return Err(EngineError::NonAdjacentBoundaries);
    }
    for s in &input.open_snapshot_seqs {
        if *s > input.read_seq {
            return Err(EngineError::SnapshotAfterRead {
                snapshot: *s,
                read_seq: input.read_seq,
            });
        }
    }

    let mut served: Vec<u64> = input
        .open_snapshot_seqs
        .iter()
        .copied()
        .chain(std::iter::once(input.read_seq))
        .collect();
    served.sort();
    served.dedup();

    let shadow = merge_ranges(
        &input
            .deep_layers
            .iter()
            .flat_map(|d| d.key_ranges.clone())
            .collect::<Vec<_>>(),
    );

    let mut compiler = Compiler {
        shadow,
        served,
        read_seq: input.read_seq,
        next_id: 0,
        out_records: Vec::new(),
        reclaimed: Vec::new(),
        rt_needed_keys: BTreeMap::new(),
        evidenced: BTreeSet::new(),
    };

    // 所有出现点记录的键（含验证探针键中已有点记录的，去重）。
    let mut point_keys: BTreeSet<Vec<u8>> = BTreeSet::new();
    for seg in &input.selected {
        for r in &seg.records {
            point_keys.insert(r.key.clone());
        }
    }

    for key in &point_keys {
        let events = events_for_key(key, &input.selected);
        compiler.compile_key(key, events);
    }

    // 范围墓碑阶段：子区间存活性证明与输出边界拆分。
    let all_tombstones: Vec<RangeTombstone> = input
        .selected
        .iter()
        .flat_map(|s| s.tombstones.iter().cloned())
        .collect();

    let mut out_ranges: Vec<OutRange> = Vec::new();
    for tomb in all_tombstones {
        if tomb.seq > input.read_seq {
            compiler.reclaim(
                tomb.id,
                ReclaimReason::AboveReadSeq { seq: tomb.seq, read_seq: input.read_seq },
            );
            continue;
        }

        let cuts: Vec<Vec<u8>> = point_keys.iter().cloned().collect();
        let pieces = split_at(&tomb.range, &cuts);
        let needed = compiler.rt_needed_keys.get(&tomb.id).cloned().unwrap_or_default();

        let mut surviving: Vec<KeyRange> = Vec::new();
        let mut dropped: Vec<KeyRange> = Vec::new();
        for piece in pieces {
            let contains_needed_key = match &piece.start {
                Some(k) => needed.contains(k),
                None => false,
            };
            if contains_needed_key {
                surviving.push(piece);
            } else if compiler.shadow_disjoint(&piece) {
                dropped.push(piece);
            } else {
                // 与深层区间相交但本键无读者：保守保留，绝不误删。
                surviving.push(piece);
            }
        }

        if !dropped.is_empty() {
            compiler.reclaim(
                tomb.id,
                ReclaimReason::RangeTombstoneDead {
                    dropped_ranges: dropped.clone(),
                    provably_no_deep_key: true,
                },
            );
        }
        if surviving.is_empty() {
            continue;
        }
        let surviving_merged = merge_ranges(&surviving);

        // 按输出段边界拆分。
        for segment_range in split_at(&KeyRange::universe(), &boundaries) {
            for part in surviving_merged.iter().filter_map(|r| r.intersect(&segment_range)) {
                out_ranges.push(OutRange {
                    tombstone: RangeTombstone {
                        id: compiler.synth_id(),
                        seq: tomb.seq,
                        range: part,
                    },
                    evidence: vec![tomb.id],
                });
                compiler.evidenced.insert(tomb.id);
            }
        }
    }

    // 组织输出段。
    let segment_ranges = split_at(&KeyRange::universe(), &boundaries);
    let mut segments: Vec<OutputSegment> = segment_ranges
        .into_iter()
        .enumerate()
        .map(|(depth, range)| OutputSegment {
            depth,
            range,
            records: Vec::new(),
            tombstones: Vec::new(),
        })
        .collect();

    for out in compiler.out_records {
        let seg = segments
            .iter_mut()
            .find(|s| s.range.contains(out.record.key.as_slice()))
            .expect("key must belong to exactly one output segment");
        seg.records.push(out);
    }
    for out in out_ranges {
        let seg = segments
            .iter_mut()
            .find(|s| !s.range.is_disjoint(&out.tombstone.range))
            .expect("tombstone part already clipped to one segment");
        seg.tombstones.push(out);
    }
    for seg in &mut segments {
        seg.records.sort_by_key(|o| o.record.seq);
        seg.tombstones.sort_by_key(|o| o.tombstone.seq);
    }

    let output = CompactionOutput {
        segments,
        reclaimed: compiler.reclaimed,
    };

    check_evidence_closure(input, &output, &compiler.evidenced)?;
    Ok(output)
}

/// 闭环校验：每个输入 id 要么出现在某条输出证据中，要么被回收；
/// 证据引用的 id 必须真实存在于输入中。
pub fn check_evidence_closure(
    input: &CompactionInput,
    output: &CompactionOutput,
    evidenced: &BTreeSet<RecordId>,
) -> Result<(), EngineError> {
    let mut input_ids: BTreeSet<RecordId> = BTreeSet::new();
    for seg in &input.selected {
        for r in &seg.records {
            input_ids.insert(r.id);
        }
        for t in &seg.tombstones {
            input_ids.insert(t.id);
        }
    }

    let mut used: BTreeSet<RecordId> = evidenced.iter().copied().collect();
    for seg in &output.segments {
        for o in &seg.records {
            for e in &o.evidence {
                if !input_ids.contains(e) {
                    return Err(EngineError::MissingEvidence(e.0));
                }
            }
            used.extend(o.evidence.iter().copied());
        }
        for o in &seg.tombstones {
            for e in &o.evidence {
                if !input_ids.contains(e) {
                    return Err(EngineError::MissingEvidence(e.0));
                }
            }
            used.extend(o.evidence.iter().copied());
        }
    }
    for entry in &output.reclaimed {
        if !input_ids.contains(&entry.source) {
            return Err(EngineError::MissingEvidence(entry.source.0));
        }
        used.insert(entry.source);
    }
    for id in &input_ids {
        if !used.contains(id) {
            return Err(EngineError::UnaccountedRecord(id.0));
        }
    }
    Ok(())
}

/// 把压实输出还原成普通段，供读取/验证复用。
pub fn output_as_segments(output: &CompactionOutput) -> Vec<Segment> {
    output
        .segments
        .iter()
        .map(|s| Segment {
            depth: s.depth,
            records: s.records.iter().map(|o| o.record.clone()).collect(),
            tombstones: s.tombstones.iter().map(|o| o.tombstone.clone()).collect(),
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::RecordId;

    fn k(s: &str) -> Vec<u8> {
        s.as_bytes().to_vec()
    }
    fn e(s: &str) -> Vec<u8> {
        s.as_bytes().to_vec()
    }
    fn rid(n: u64) -> RecordId {
        RecordId(n)
    }

    fn point(id: u64, key: &str, seq: u64, op: PointOp) -> Record {
        Record { id: rid(id), key: k(key), seq, op }
    }

    #[test]
    fn folds_append_chain_between_readers() {
        let seg = Segment::new(
            0,
            vec![
                point(1, "k", 1, PointOp::Put(vec![e("a")])),
                point(2, "k", 2, PointOp::Append(e("b"))),
                point(3, "k", 4, PointOp::Append(e("c"))),
                point(4, "k", 8, PointOp::Append(e("d"))),
            ],
            vec![],
        )
        .unwrap();
        let input = CompactionInput {
            selected: vec![seg],
            deep_layers: vec![],
            read_seq: 10,
            open_snapshot_seqs: vec![3, 7],
            output_boundaries: vec![],
            verification_keys: vec![],
        };
        let out = compact(&input).unwrap();
        let recs = &out.segments[0].records;
        // 读者点 seq=3 看到 [a,b]；seq=7 看到 [a,b,c]；seq=10 看到 [a,b,c,d]。
        // 三条读者边界 => 三条合成 Put，序号分别取 1（并入原 Put）、4、8。
        let seqs: Vec<u64> = recs.iter().map(|o| o.record.seq).collect();
        assert_eq!(seqs, vec![1, 4, 8]);
        match &recs[0].record.op {
            PointOp::Put(v) => assert_eq!(v.as_slice(), [e("a"), e("b")]),
            _ => panic!(),
        }
        match &recs[2].record.op {
            PointOp::Put(v) => assert_eq!(v.as_slice(), [e("a"), e("b"), e("c"), e("d")]),
            _ => panic!(),
        }
        // 证据：第一条 Put 同时引用原始 Put(1) 与被折叠追加(2)。
        assert_eq!(recs[0].evidence, vec![rid(1), rid(2)]);
        let reclaimed_ids: BTreeSet<u64> =
            out.reclaimed.iter().map(|r| r.source.0).collect();
        assert!(reclaimed_ids.contains(&2));
        assert!(reclaimed_ids.contains(&3));
        assert!(reclaimed_ids.contains(&4));
    }

    #[test]
    fn unobserved_epoch_is_reclaimed_when_deep_disjoint() {
        let seg = Segment::new(
            0,
            vec![
                point(1, "k", 1, PointOp::Put(vec![e("a")])),
                point(2, "k", 2, PointOp::Delete),
                point(3, "k", 3, PointOp::Put(vec![e("b")])),
            ],
            vec![],
        )
        .unwrap();
        let input = CompactionInput {
            selected: vec![seg],
            deep_layers: vec![],
            read_seq: 5,
            open_snapshot_seqs: vec![4], // 只读最后一个 epoch
            output_boundaries: vec![],
            verification_keys: vec![],
        };
        let out = compact(&input).unwrap();
        let recs = &out.segments[0].records;
        assert_eq!(recs.len(), 1);
        assert_eq!(recs[0].record.seq, 3);
        let reasons: Vec<&ReclaimReason> = out.reclaimed.iter().map(|r| &r.reason).collect();
        assert!(reasons.iter().any(|r| matches!(
            r,
            ReclaimReason::EpochWithoutReader { reset_seq: Some(1), .. }
        )));
        assert!(reasons.iter().any(|r| matches!(
            r,
            ReclaimReason::EpochWithoutReader { reset_seq: Some(2), .. }
        )));
    }

    #[test]
    fn unknown_base_keeps_raw_appends() {
        let deep = DeepLayer {
            depth: 9,
            key_ranges: vec![KeyRange::new(Some(k("a")), Some(k("z"))).unwrap()],
        };
        let seg = Segment::new(
            0,
            vec![
                point(1, "k", 1, PointOp::Append(e("a"))),
                point(2, "k", 2, PointOp::Append(e("b"))),
            ],
            vec![],
        )
        .unwrap();
        let input = CompactionInput {
            selected: vec![seg],
            deep_layers: vec![deep],
            read_seq: 5,
            open_snapshot_seqs: vec![],
            output_boundaries: vec![],
            verification_keys: vec![],
        };
        let out = compact(&input).unwrap();
        let recs = &out.segments[0].records;
        assert_eq!(recs.len(), 2);
        assert!(matches!(recs[0].record.op, PointOp::Append(_)));
        assert!(matches!(recs[1].record.op, PointOp::Append(_)));
        assert!(out.reclaimed.is_empty());
    }

    #[test]
    fn evidence_closure_rejects_dangling_and_unaccounted_records() {
        let seg = Segment::new(
            0,
            vec![point(1, "k", 1, PointOp::Put(vec![e("a")]))],
            vec![],
        )
        .unwrap();
        let input = CompactionInput {
            selected: vec![seg],
            deep_layers: vec![],
            read_seq: 5,
            open_snapshot_seqs: vec![],
            output_boundaries: vec![],
            verification_keys: vec![],
        };
        let good = compact(&input).unwrap();
        let mut evidenced = BTreeSet::new();
        for s in &good.segments {
            for o in &s.records {
                evidenced.extend(o.evidence.iter().copied());
            }
        }
        // 正常输出通过闭环。
        assert!(check_evidence_closure(&input, &good, &evidenced).is_ok());

        // 证据引用了输入中不存在的 id => MissingEvidence。
        let mut bad = good.clone();
        bad.segments[0].records[0].evidence = vec![rid(999)];
        assert!(matches!(
            check_evidence_closure(&input, &bad, &evidenced),
            Err(EngineError::MissingEvidence(999))
        ));

        // 回收了输入中不存在的 id => MissingEvidence。
        let mut bad2 = good.clone();
        bad2.reclaimed.push(ReclaimEntry {
            source: rid(888),
            reason: ReclaimReason::AboveReadSeq { seq: 888, read_seq: 5 },
        });
        assert!(matches!(
            check_evidence_closure(&input, &bad2, &evidenced),
            Err(EngineError::MissingEvidence(888))
        ));
    }
}
