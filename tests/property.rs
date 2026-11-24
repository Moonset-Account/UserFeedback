//! 随机交错属性测试与边界场景测试。
//!
//! 这些测试不是预置输出的演示：每轮用确定性 PRNG 现场合成盘面，
//! 真实执行压实，再由 `equivalence_report` 在全部开放快照/当前读序号、
//! 全部相关键、以及所有允许的深层旧值指派上做前后对比。

use std::collections::BTreeSet;

use local_compaction::interval::KeyRange;
use local_compaction::model::{DeepLayer, PointOp, RangeTombstone, Record, RecordId, Segment};
use local_compaction::{
    compact, equivalence_report, CompactionInput, ReclaimReason,
};

/// 确定性 xorshift64，保证测试可回放、可复现。
struct Rng(u64);

impl Rng {
    fn next_u64(&mut self) -> u64 {
        let mut x = self.0;
        x ^= x << 13;
        x ^= x >> 7;
        x ^= x << 17;
        self.0 = x;
        x
    }
    fn below(&mut self, n: u64) -> usize {
        (self.next_u64() % n) as usize
    }
    fn key(&mut self, pool: &[Vec<u8>]) -> Vec<u8> {
        pool[self.below(pool.len() as u64)].clone()
    }
    fn elem(&mut self) -> Vec<u8> {
        format!("e{}", self.below(6)).into_bytes()
    }
}

fn key_pool(n: usize) -> Vec<Vec<u8>> {
    (0..n).map(|i| format!("k{i:02}").into_bytes()).collect()
}

/// 一轮随机盘面。
fn random_case(seed: u64) -> CompactionInput {
    let mut rng = Rng(seed ^ 0x9e37_79b9_7f4a_7c15);
    let key_count = 2 + rng.below(4);
    let pool = key_pool(key_count);

    let read_seq = 40 + rng.below(40) as u64;
    let snap_a = 1 + rng.below((read_seq / 2).max(1)) as u64;
    let snap_b = snap_a + 1 + rng.below((read_seq - snap_a).max(1)) as u64;

    // 随机点记录：序号严格递增，操作在四种语义间交错。
    let mut records = Vec::new();
    let mut id = 1u64;
    let event_count = 4 + rng.below(8);
    let mut seq = 1u64;
    for _ in 0..event_count {
        seq += 1 + rng.below(6) as u64;
        let key = rng.key(&pool);
        let op = match rng.below(5) {
            0 => PointOp::Put((0..rng.below(3)).map(|_| rng.elem()).collect()),
            1 => PointOp::Delete,
            2 => PointOp::Append(rng.elem()),
            3 => PointOp::Put(vec![]), // 空列表写入
            _ => PointOp::Append(rng.elem()),
        };
        records.push(Record { id: RecordId(id), key, seq, op });
        id += 1;
    }

    // 随机范围墓碑：从键池里选两个不同键构成半开区间。
    let mut tombstones = Vec::new();
    if key_count >= 2 {
        for _ in 0..rng.below(2) {
            let mut a = rng.below(key_count as u64);
            let mut b = rng.below(key_count as u64);
            if a == b {
                b = (b + 1) % key_count;
            }
            if a > b {
                std::mem::swap(&mut a, &mut b);
            }
            seq += 1 + rng.below(5) as u64;
            tombstones.push(RangeTombstone {
                id: RecordId(id),
                seq,
                range: KeyRange::new(Some(pool[a].clone()), Some(pool[b].clone())).unwrap(),
            });
            id += 1;
        }
    }

    // 深层区间：覆盖键池的若干随机前缀，制造未知基值。
    // cut 取 1..=key_count：cut==key_count 时终点取 +∞，避免越界。
    let cut = 1 + rng.below(key_count as u64);
    let deep_ranges = if cut == 0 {
        vec![]
    } else if cut as usize == key_count {
        vec![KeyRange::new(Some(pool[0].clone()), None).unwrap()]
    } else {
        vec![KeyRange::new(Some(pool[0].clone()), Some(pool[cut as usize].clone())).unwrap()]
    };
    let deep_layers = if deep_ranges.is_empty() {
        vec![]
    } else {
        vec![DeepLayer { depth: 9, key_ranges: deep_ranges }]
    };

    // 输出边界：选 0~2 个中间键（只从内部键 pool[1..key_count-1] 中选）。
    let mut boundaries: Vec<Vec<u8>> = Vec::new();
    if key_count >= 3 {
        let inner = &pool[1..key_count - 1];
        for _ in 0..rng.below(2) {
            let b = inner[rng.below(inner.len() as u64)].clone();
            boundaries.push(b);
        }
    }
    boundaries.sort();
    boundaries.dedup();

    // 额外探针：一定包含一个被深层覆盖、但没有任何点记录的键时，
    // 直接用键池中的键即可（验证键集合自动含全部点键）。
    let verification_keys = pool.clone();

    let segment = Segment::new(0, records, tombstones).unwrap();
    CompactionInput {
        selected: vec![segment],
        deep_layers,
        read_seq,
        open_snapshot_seqs: vec![snap_a, snap_b],
        output_boundaries: boundaries,
        verification_keys,
    }
}

#[test]
fn random_interleavings_always_preserve_all_allowed_reads() {
    for seed in 0..600u64 {
        let input = random_case(seed);
        let output = compact(&input).unwrap_or_else(|e| panic!("seed {seed}: {e}"));
        let report = equivalence_report(&input, &output)
            .unwrap_or_else(|e| panic!("seed {seed}: {e}"));
        assert!(
            report.is_ok(),
            "seed {seed} produced mismatch: {:?}",
            report.mismatches
        );
        assert!(report.assignments_exhausted >= 1);
        // 输出段数 = 边界数 + 1。
        assert_eq!(output.segments.len(), input.output_boundaries.len() + 1);

        // 输入记录全集。
        let mut input_ids: BTreeSet<u64> = BTreeSet::new();
        for seg in &input.selected {
            input_ids.extend(seg.records.iter().map(|r| r.id.0));
            input_ids.extend(seg.tombstones.iter().map(|t| t.id.0));
        }
        // 输出证据引用的输入 id 与回收 id。
        let mut evidenced: BTreeSet<u64> = BTreeSet::new();
        for seg in &output.segments {
            for o in &seg.records {
                assert!(!o.evidence.is_empty(), "seed {seed}: point output without evidence");
                for e in &o.evidence {
                    assert!(input_ids.contains(&e.0), "seed {seed}: dangling evidence {}", e.0);
                    evidenced.insert(e.0);
                }
            }
            for o in &seg.tombstones {
                assert!(!o.evidence.is_empty(), "seed {seed}: range output without evidence");
                for e in &o.evidence {
                    assert!(input_ids.contains(&e.0));
                    evidenced.insert(e.0);
                }
            }
        }
        let reclaimed: BTreeSet<u64> = output.reclaimed.iter().map(|r| r.source.0).collect();
        // 闭环：证据与回收的并集恰好覆盖全部输入记录。
        // 注意二者允许相交——被折叠的追加既留在合成 Put 的证据链里（可追溯
        // 推导来源），又出现在回收清单里（物理操作数已被折叠消除），这是
        // “证据（从何推导）”与“回收（为何不再独立存在）”两个正交维度。
        assert_eq!(
            evidenced.union(&reclaimed).copied().collect::<BTreeSet<u64>>(),
            input_ids,
            "seed {seed}: evidence/reclaim does not cover all input records"
        );
        // 既进证据又进回收清单的记录，其回收原因只能是“折叠”或“部分
        // 范围墓碑回收”——两者都保留了可追溯的存活输出，仅消除冗余部分。
        for entry in &output.reclaimed {
            if evidenced.contains(&entry.source.0) {
                assert!(
                    matches!(
                        entry.reason,
                        ReclaimReason::FoldedIntoPut { .. }
                            | ReclaimReason::RangeTombstoneDead { .. }
                    ),
                    "seed {seed}: evidenced record reclaimed with incompatible reason {:?}",
                    entry.reason
                );
            }
        }

        // 回收原因必须是已知合法类别。
        for entry in &output.reclaimed {
            let _ = match &entry.reason {
                ReclaimReason::AboveReadSeq { .. }
                | ReclaimReason::EpochWithoutReader { .. }
                | ReclaimReason::TailAfterLastReader { .. }
                | ReclaimReason::FoldedIntoPut { .. }
                | ReclaimReason::RangeTombstoneDead { .. } => {}
            };
        }
    }
}

#[test]
fn random_outputs_partition_keyspace_without_gaps() {
    for seed in 1000..1150u64 {
        let input = random_case(seed);
        let output = compact(&input).unwrap();
        // 相邻段首尾相接。
        for w in output.segments.windows(2) {
            assert_eq!(w[0].range.end, w[1].range.start);
        }
        assert!(output.segments.first().unwrap().range.start.is_none());
        assert!(output.segments.last().unwrap().range.end.is_none());

        // 每个点记录都落在且仅落在一个段内。
        let all_output_keys: BTreeSet<&Vec<u8>> = output
            .segments
            .iter()
            .flat_map(|s| s.records.iter().map(|o| &o.record.key))
            .collect();
        for key in all_output_keys {
            let owners = output
                .segments
                .iter()
                .filter(|s| s.range.contains(key))
                .count();
            assert_eq!(owners, 1);
        }
    }
}

/// 边界场景：读序号早于一切记录 => 全部记录都应回收，输出为空但仍等价。
#[test]
fn read_seq_before_any_record_reclaims_everything() {
    let seg = Segment::new(
        0,
        vec![
            Record { id: RecordId(1), key: b"k1".to_vec(), seq: 5, op: PointOp::Put(vec![b"a".to_vec()]) },
            Record { id: RecordId(2), key: b"k1".to_vec(), seq: 6, op: PointOp::Append(b"b".to_vec()) },
        ],
        vec![RangeTombstone {
            id: RecordId(3),
            seq: 7,
            range: KeyRange::new(Some(b"a".to_vec()), Some(b"z".to_vec())).unwrap(),
        }],
    )
    .unwrap();
    let input = CompactionInput {
        selected: vec![seg],
        deep_layers: vec![],
        read_seq: 4,
        open_snapshot_seqs: vec![2, 3],
        output_boundaries: vec![],
        verification_keys: vec![],
    };
    let output = compact(&input).unwrap();
    assert!(output.segments.iter().all(|s| s.records.is_empty() && s.tombstones.is_empty()));
    let reclaimed: BTreeSet<u64> = output.reclaimed.iter().map(|r| r.source.0).collect();
    assert_eq!(reclaimed, BTreeSet::from([1, 2, 3]));
    let report = equivalence_report(&input, &output).unwrap();
    assert!(report.is_ok());
}

/// 边界场景：空列表写入在所有读者点上都保持“存在”，不会被折叠成删除。
#[test]
fn empty_list_put_survives_snapshot_boundaries() {
    let seg = Segment::new(
        0,
        vec![
            Record { id: RecordId(1), key: b"k".to_vec(), seq: 3, op: PointOp::Put(vec![]) },
            Record { id: RecordId(2), key: b"k".to_vec(), seq: 9, op: PointOp::Append(b"x".to_vec()) },
        ],
        vec![],
    )
    .unwrap();
    let input = CompactionInput {
        selected: vec![seg],
        deep_layers: vec![],
        read_seq: 12,
        open_snapshot_seqs: vec![5, 10],
        output_boundaries: vec![],
        verification_keys: vec![],
    };
    let output = compact(&input).unwrap();
    let report = equivalence_report(&input, &output).unwrap();
    assert!(report.is_ok());

    // 快照 5：空列表写入仍然存在；快照 10：追加后为 [x]。
    use local_compaction::read::{read_symbolic, ReadStack};
    use local_compaction::SymbolicValue;
    let post: Vec<Segment> = local_compaction::compaction::output_as_segments(&output);
    let stack = ReadStack { segments: &post, deep_layers: &[] };
    assert_eq!(read_symbolic(&stack, b"k", 5).unwrap(), SymbolicValue::Concrete(vec![]));
    assert_eq!(
        read_symbolic(&stack, b"k", 10).unwrap(),
        SymbolicValue::Concrete(vec![b"x".to_vec()])
    );
}

/// 交错场景：存活 epoch 内、最后一个读者之后的追加必须被回收，
/// 且压实前后在所有读者点上保持等价（回归测试：随机盘面曾触发该路径 panic）。
///
/// 构造：读者点为快照 5、快照 10、当前读序号 20。
/// - epoch1：Put(1)、Append(4)、Append(8)，随后被 Delete(12) 闭合；
///   epoch1 的读者只有 5 和 10，因此 Append(8) 之后、Delete 之前若还有
///   追加（seq=11），它落在 epoch1 最后读者(10)之后，必须走 tail 回收。
/// - epoch2：Delete(12) 与 Append(16)，读者 20 可见。
#[test]
fn tail_append_inside_observed_epoch_is_reclaimed_safely() {
    let seg = Segment::new(
        0,
        vec![
            Record { id: RecordId(1), key: b"k".to_vec(), seq: 1, op: PointOp::Put(vec![b"a".to_vec()]) },
            Record { id: RecordId(2), key: b"k".to_vec(), seq: 4, op: PointOp::Append(b"b".to_vec()) },
            Record { id: RecordId(3), key: b"k".to_vec(), seq: 8, op: PointOp::Append(b"c".to_vec()) },
            // epoch1 最后读者(10)之后、闭合 reset(12)之前：tail 回收目标。
            Record { id: RecordId(6), key: b"k".to_vec(), seq: 11, op: PointOp::Append(b"tail".to_vec()) },
            Record { id: RecordId(4), key: b"k".to_vec(), seq: 12, op: PointOp::Delete },
            Record { id: RecordId(5), key: b"k".to_vec(), seq: 16, op: PointOp::Append(b"q".to_vec()) },
        ],
        vec![],
    )
    .unwrap();
    let input = CompactionInput {
        selected: vec![seg],
        deep_layers: vec![],
        read_seq: 20,
        open_snapshot_seqs: vec![5, 10],
        output_boundaries: vec![],
        verification_keys: vec![],
    };
    let output = compact(&input).unwrap();
    let report = equivalence_report(&input, &output).unwrap();
    assert!(report.is_ok(), "{:?}", report.mismatches);

    let reclaimed: BTreeSet<u64> = output.reclaimed.iter().map(|r| r.source.0).collect();
    assert!(
        reclaimed.contains(&6),
        "append at seq 11 is after the epoch's last reader and must be reclaimed"
    );
    let tail_entry = output
        .reclaimed
        .iter()
        .find(|e| e.source.0 == 6)
        .expect("tail reclaim entry exists");
    assert!(matches!(
        tail_entry.reason,
        ReclaimReason::TailAfterLastReader { last_needed_seq: 10 }
    ));

    // 闭环：每个输入 id 要么进入证据，要么被回收。
    for id in 1..=6u64 {
        let in_evidence = output
            .segments
            .iter()
            .flat_map(|s| s.records.iter())
            .any(|o| o.evidence.contains(&RecordId(id)));
        assert!(in_evidence || reclaimed.contains(&id));
    }

    // 读者点 10 的值在压实前后一致（tail 追加不可见，故为 [a,b,c]）。
    use local_compaction::read::{read_symbolic, ReadStack};
    use local_compaction::SymbolicValue;
    let post = local_compaction::compaction::output_as_segments(&output);
    let stack = ReadStack { segments: &post, deep_layers: &[] };
    assert_eq!(
        read_symbolic(&stack, b"k", 10).unwrap(),
        SymbolicValue::Concrete(vec![b"a".to_vec(), b"b".to_vec(), b"c".to_vec()])
    );
    // 读序号 20：删除后由 Append(16) 重新产生。
    assert_eq!(
        read_symbolic(&stack, b"k", 20).unwrap(),
        SymbolicValue::Concrete(vec![b"q".to_vec()])
    );
}

/// 边界场景：输入校验拒绝重复序号与非法快照。
#[test]
fn invalid_inputs_are_rejected() {
    let dup = Segment::new(
        0,
        vec![
            Record { id: RecordId(1), key: b"k".to_vec(), seq: 5, op: PointOp::Delete },
            Record { id: RecordId(2), key: b"k".to_vec(), seq: 5, op: PointOp::Delete },
        ],
        vec![],
    )
    .unwrap();
    let input = CompactionInput {
        selected: vec![dup],
        deep_layers: vec![],
        read_seq: 10,
        open_snapshot_seqs: vec![],
        output_boundaries: vec![],
        verification_keys: vec![],
    };
    assert!(compact(&input).is_err());

    let good = Segment::new(
        0,
        vec![Record { id: RecordId(1), key: b"k".to_vec(), seq: 5, op: PointOp::Delete }],
        vec![],
    )
    .unwrap();
    let bad_snapshot = CompactionInput {
        selected: vec![good],
        deep_layers: vec![],
        read_seq: 5,
        open_snapshot_seqs: vec![9],
        output_boundaries: vec![],
        verification_keys: vec![],
    };
    assert!(compact(&bad_snapshot).is_err());
}
