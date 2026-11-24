//! 可回放的小盘面（tabletop）。
//!
//! 盘面刻意包含题目点名的全部要素：
//!
//! - **跨段墓碑**：范围墓碑 `[n0, n9)` 在输出切点 `n4` 处被拆成两个输出段；
//! - **两份开放快照**：序号 18 与 42，外加当前读序号 60；
//! - **中间追加**：两份快照之间对列表键 `a-list` 的多次追加，用于演示折叠；
//! - 删除后追加重新产生键、空列表写入仍存在、对缺失键追加从空列表起步；
//! - 深层未知基值（`a-unknown`、`a-shadowed` 与两个探针键），验证时穷举
//!   所有允许的旧值；
//! - 可证明死亡的删除/墓碑/操作数回收，以及与深层相交而必须保守保留的墓碑；
//! - 序号晚于当前读序号的记录直接回收。
//!
//! 所有输入均为本地合成数据，不访问任何外部目标。

use std::collections::BTreeSet;

use crate::compaction::{
    compact, output_as_segments, CompactionInput, CompactionOutput, ReclaimReason,
};
use crate::error::EngineError;
use crate::interval::KeyRange;
use crate::model::{DeepLayer, PointOp, RangeTombstone, Record, RecordId, Segment};
use crate::read::{read_symbolic, ReadStack, SymbolicValue};
use crate::verify::{equivalence_report, VerificationReport};

fn k(s: &str) -> Vec<u8> {
    s.as_bytes().to_vec()
}
fn e(s: &str) -> Vec<u8> {
    s.as_bytes().to_vec()
}
fn rid(n: u64) -> RecordId {
    RecordId(n)
}
fn pr(id: u64, key: &str, seq: u64, op: PointOp) -> Record {
    Record { id: rid(id), key: k(key), seq, op }
}

/// 小盘面的完整定义与期望行为。
#[derive(Debug, Clone)]
pub struct Tabletop {
    pub input: CompactionInput,
    /// 两份开放快照序号与当前读序号。
    pub snapshot_a: u64,
    pub snapshot_b: u64,
    pub read_seq: u64,
    /// 输出段切点。
    pub boundaries: Vec<Vec<u8>>,
}

impl Tabletop {
    /// 构造固定盘面。记录序号、键名都是确定性的。
    pub fn build() -> Self {
        // ---- 段 1（较新，depth 0）----
        let seg1 = Segment::new(
            0,
            vec![
                // a-list：整值写入 -> 快照间多次追加 -> 折叠素材
                pr(1, "a-list", 10, PointOp::Put(vec![e("v0")])),
                pr(2, "a-list", 15, PointOp::Append(e("v1"))), // 快照 A=18 可见
                pr(3, "a-list", 25, PointOp::Append(e("v2"))), // 快照 B=42 可见
                pr(4, "a-list", 50, PointOp::Append(e("v3"))), // 仅读序号 60 可见
                pr(5, "a-list", 55, PointOp::Append(e("v4"))),
                // a-unknown：深层未知基值上的追加，禁止折叠
                pr(6, "a-unknown", 12, PointOp::Append(e("u1"))),
                pr(7, "a-unknown", 30, PointOp::Append(e("u2"))),
                // a-reborn：删除后追加，重新产生（删除发生在快照 A 之前）
                pr(8, "a-reborn", 11, PointOp::Put(vec![e("old")])),
                pr(9, "a-reborn", 16, PointOp::Delete),
                pr(10, "a-reborn", 35, PointOp::Append(e("new"))),
                // a-empty：空列表写入仍然存在
                pr(11, "a-empty", 22, PointOp::Put(vec![])),
                pr(12, "a-empty", 52, PointOp::Append(e("x"))),
                // n2-hit：落在范围墓碑内的点键
                pr(18, "n2-hit", 13, PointOp::Put(vec![e("hit-a")])),
                pr(19, "n2-hit", 56, PointOp::Append(e("hit-b"))),
                // 晚于当前读序号：任何读者都不可见，必须回收
                pr(22, "a-list", 65, PointOp::Append(e("future"))),
            ],
            vec![
                // 跨输出切点 n4 的墓碑：覆盖 n0..n9，切点 n4 落在中间
                RangeTombstone {
                    id: rid(20),
                    seq: 40,
                    range: KeyRange::new(Some(k("n0")), Some(k("n9"))).unwrap(),
                },
            ],
        )
        .unwrap();

        // ---- 段 2（较老，depth 1）----
        let seg2 = Segment::new(
            1,
            vec![
                // a-dead：删除之后被整值覆盖，且深层不覆盖 -> 整个早期 epoch 可回收
                pr(13, "a-dead", 5, PointOp::Put(vec![e("first")])),
                pr(14, "a-dead", 6, PointOp::Delete),
                pr(15, "a-dead", 7, PointOp::Put(vec![e("second")])),
                // a-shadowed：删除负责遮蔽深层旧值，必须保留
                pr(16, "a-shadowed", 8, PointOp::Delete),
                pr(17, "a-shadowed", 45, PointOp::Put(vec![e("fresh")])),
            ],
            vec![
                // 完全死亡的墓碑：区间内无点键、与所有深层区间不相交
                RangeTombstone {
                    id: rid(21),
                    seq: 9,
                    range: KeyRange::new(Some(k("0")), Some(k("1"))).unwrap(),
                },
            ],
        )
        .unwrap();

        // ---- 未选深层：只给出可能含键的区间，旧值未知 ----
        let deep1 = DeepLayer {
            depth: 5,
            key_ranges: vec![
                // 只覆盖 a-unknown；a-reborn 刻意留在深层覆盖之外，
                // 使其“删除 => 确定不存在”的语义可被断言。
                KeyRange::new(Some(k("a-u")), Some(k("a-v"))).unwrap(),
                // 覆盖 a-shadowed
                KeyRange::new(Some(k("a-r")), Some(k("a-s"))).unwrap(),
                // 覆盖墓碑内探针 n5-probe（n2-hit 是已知点键，不依赖旧值）
                KeyRange::new(Some(k("n2-hit")), Some(k("n9"))).unwrap(),
            ],
        };
        let deep2 = DeepLayer {
            depth: 8,
            // 更老的层，覆盖墓碑之外的探针 z-probe-out
            key_ranges: vec![KeyRange::new(
                Some(k("z-probe-out")),
                Some(k("z-probe-ouz")),
            )
            .unwrap()],
        };

        let boundaries = vec![k("n4")];

        let input = CompactionInput {
            selected: vec![seg1, seg2],
            deep_layers: vec![deep1, deep2],
            read_seq: 60,
            open_snapshot_seqs: vec![18, 42],
            output_boundaries: boundaries.clone(),
            verification_keys: vec![k("n5-probe"), k("z-probe-out")],
        };

        Self { input, snapshot_a: 18, snapshot_b: 42, read_seq: 60, boundaries }
    }

    /// 执行压实并返回输出。
    pub fn compact(&self) -> Result<CompactionOutput, EngineError> {
        compact(&self.input)
    }

    /// 运行完整等价性验证（符号比较 + 深层旧值穷举 + 证据闭环）。
    pub fn verify(&self, output: &CompactionOutput) -> Result<VerificationReport, EngineError> {
        equivalence_report(&self.input, output)
    }

    /// 压实前的符号读取结果，用于人工回放对照。
    pub fn replay_before(&self) -> Result<Vec<(u64, String, SymbolicValue)>, EngineError> {
        let stack = ReadStack {
            segments: &self.input.selected,
            deep_layers: &self.input.deep_layers,
        };
        let keys = Self::watched_keys();
        let points = [self.snapshot_a, self.snapshot_b, self.read_seq];
        let mut rows = Vec::new();
        for p in points {
            for key in &keys {
                rows.push((p, key.clone(), read_symbolic(&stack, k(key).as_slice(), p)?));
            }
        }
        Ok(rows)
    }

    /// 压实后的符号读取结果。
    pub fn replay_after(
        &self,
        output: &CompactionOutput,
    ) -> Result<Vec<(u64, String, SymbolicValue)>, EngineError> {
        let segments = output_as_segments(output);
        let stack = ReadStack {
            segments: &segments,
            deep_layers: &self.input.deep_layers,
        };
        let keys = Self::watched_keys();
        let points = [self.snapshot_a, self.snapshot_b, self.read_seq];
        let mut rows = Vec::new();
        for p in points {
            for key in &keys {
                rows.push((p, key.clone(), read_symbolic(&stack, k(key).as_slice(), p)?));
            }
        }
        Ok(rows)
    }

    fn watched_keys() -> Vec<String> {
        vec![
            "a-list".to_string(),
            "a-unknown".to_string(),
            "a-reborn".to_string(),
            "a-empty".to_string(),
            "a-dead".to_string(),
            "a-shadowed".to_string(),
            "n2-hit".to_string(),
            "n5-probe".to_string(),
            "z-probe-out".to_string(),
        ]
    }

    /// 回收清单中包含的来源记录 id 集合。
    pub fn reclaimed_ids(output: &CompactionOutput) -> BTreeSet<u64> {
        output.reclaimed.iter().map(|r| r.source.0).collect()
    }

    /// 按原因分类的回收统计。
    pub fn reclaim_reason_counts(output: &CompactionOutput) -> Vec<(&'static str, usize)> {
        let mut folded = 0;
        let mut epoch = 0;
        let mut tail = 0;
        let mut tomb = 0;
        let mut above = 0;
        for entry in &output.reclaimed {
            match entry.reason {
                ReclaimReason::FoldedIntoPut { .. } => folded += 1,
                ReclaimReason::EpochWithoutReader { .. } => epoch += 1,
                ReclaimReason::TailAfterLastReader { .. } => tail += 1,
                ReclaimReason::RangeTombstoneDead { .. } => tomb += 1,
                ReclaimReason::AboveReadSeq { .. } => above += 1,
            }
        }
        vec![
            ("folded_into_put", folded),
            ("epoch_without_reader", epoch),
            ("tail_after_last_reader", tail),
            ("range_tombstone_dead", tomb),
            ("above_read_seq", above),
        ]
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::Value;

    #[test]
    fn tabletop_equivalence_holds_for_all_snapshots_and_deep_values() {
        let table = Tabletop::build();
        let output = table.compact().unwrap();
        let report = table.verify(&output).unwrap();
        assert!(report.is_ok(), "mismatches: {:?}", report.mismatches);
        assert_eq!(report.read_points, vec![18, 42, 60]);
        // 未知基值：a-unknown、n5-probe（deep1）与 z-probe-out（deep2），
        // 每个 4 个候选具体值 => 4^3 = 64 个指派全部穷举。
        assert_eq!(report.assignments_exhausted, 64);
        assert_eq!(report.keys_checked, 9);
    }

    #[test]
    fn tombstone_20_splits_across_output_boundary() {
        let table = Tabletop::build();
        let output = table.compact().unwrap();
        let mut parts = Vec::new();
        for seg in &output.segments {
            for t in &seg.tombstones {
                if t.evidence.contains(&rid(20)) {
                    parts.push((seg.depth, t.tombstone.range.clone()));
                }
            }
        }
        assert_eq!(parts.len(), 2, "tombstone [n0,n9) must split at boundary n4");
        // 存活区间从点键 n2-hit 开始（其左侧区间既无点键需要、又与深层不相交，
        // 已被证明回收），再被输出边界 n4 切开。
        assert_eq!(parts[0].0, 0);
        assert_eq!(parts[0].1, KeyRange::new(Some(k("n2-hit")), Some(k("n4"))).unwrap());
        assert_eq!(parts[1].0, 1);
        assert_eq!(parts[1].1, KeyRange::new(Some(k("n4")), Some(k("n9"))).unwrap());
        // 跨输出边界的拆分结果（depth 0 与 depth 1 各一片）确实存在。
        let depths: BTreeSet<usize> = parts.iter().map(|(d, _)| *d).collect();
        assert!(depths.contains(&0) && depths.contains(&1));
    }

    #[test]
    fn dead_tombstone_is_reclaimed_but_shadowed_one_is_kept() {
        let table = Tabletop::build();
        let output = table.compact().unwrap();
        let ids = Tabletop::reclaimed_ids(&output);
        assert!(ids.contains(&21), "tombstone on [0,1) is provably dead");

        let kept_20 = output
            .segments
            .iter()
            .flat_map(|s| s.tombstones.iter())
            .any(|t| t.evidence.contains(&rid(20)));
        assert!(kept_20);
    }

    #[test]
    fn list_append_chain_folds_but_unknown_base_appends_stay_raw() {
        let table = Tabletop::build();
        let output = table.compact().unwrap();

        let list_records: Vec<_> = output
            .segments
            .iter()
            .flat_map(|s| s.records.iter())
            .filter(|o| o.record.key == k("a-list"))
            .collect();
        // 三个读点 => 三条整值写入；原始追加全部进入证据或回收。
        assert_eq!(list_records.len(), 3);
        for o in &list_records {
            assert!(matches!(o.record.op, PointOp::Put(_)));
        }
        match &list_records[0].record.op {
            PointOp::Put(v) => assert_eq!(v.as_slice(), [e("v0"), e("v1")]),
            _ => panic!(),
        }
        match &list_records[2].record.op {
            PointOp::Put(v) => assert_eq!(
                v.as_slice(),
                [e("v0"), e("v1"), e("v2"), e("v3"), e("v4")]
            ),
            _ => panic!(),
        }
        // 晚于读序号的未来追加被回收。
        assert!(Tabletop::reclaimed_ids(&output).contains(&22));

        let unknown_records: Vec<_> = output
            .segments
            .iter()
            .flat_map(|s| s.records.iter())
            .filter(|o| o.record.key == k("a-unknown"))
            .collect();
        assert_eq!(unknown_records.len(), 2);
        assert!(unknown_records
            .iter()
            .all(|o| matches!(o.record.op, PointOp::Append(_))));
    }

    #[test]
    fn reborn_and_empty_present_semantics_preserved() {
        let table = Tabletop::build();
        let before = table.replay_before().unwrap();
        let output = table.compact().unwrap();
        let after = table.replay_after(&output).unwrap();
        assert_eq!(before, after);

        let find = |rows: &[(u64, String, SymbolicValue)], p: u64, key: &str| {
            rows.iter()
                .find(|(rp, rk, _)| *rp == p && rk == key)
                .map(|(_, _, v)| v.clone())
                .unwrap()
        };
        // 快照 A 时 a-reborn 已删除（深层不覆盖该键 => 确定不存在）。
        assert_eq!(find(&after, 18, "a-reborn"), SymbolicValue::Absent);
        // 快照 B 时由追加重新产生（对缺失键追加 => 空列表起步）。
        assert_eq!(
            find(&after, 42, "a-reborn"),
            SymbolicValue::Concrete(vec![e("new")])
        );
        // 空列表写入仍然存在。
        assert!(find(&after, 42, "a-empty").exists());
        assert_eq!(find(&after, 42, "a-empty"), SymbolicValue::Concrete(vec![]));
    }

    #[test]
    fn provably_dead_delete_is_reclaimed_shadowed_delete_is_kept() {
        let table = Tabletop::build();
        let output = table.compact().unwrap();
        let ids = Tabletop::reclaimed_ids(&output);
        // a-dead 的早期 epoch 没有任何读者（所有读点 >= 7），且深层不覆盖。
        assert!(ids.contains(&13));
        assert!(ids.contains(&14));
        // a-shadowed 的删除要遮蔽 deep1 的未知旧值（快照 A/B 都在删除之后、
        // 新 Put(45) 之前），必须保留。
        assert!(!ids.contains(&16));
        let kept_16 = output
            .segments
            .iter()
            .flat_map(|s| s.records.iter())
            .any(|o| o.evidence.contains(&rid(16)));
        assert!(kept_16);
    }

    #[test]
    fn every_output_record_carries_input_evidence() {
        let table = Tabletop::build();
        let output = table.compact().unwrap();
        for seg in &output.segments {
            for o in &seg.records {
                assert!(!o.evidence.is_empty());
                for e in &o.evidence {
                    assert!(e.0 < 100, "evidence must reference input records, got {e:?}");
                }
            }
            for o in &seg.tombstones {
                assert!(!o.evidence.is_empty());
            }
        }
    }

    #[test]
    fn concrete_replay_matches_symbolic_expectations() {
        let table = Tabletop::build();
        let output = table.compact().unwrap();
        let segments = output_as_segments(&output);
        let stack = ReadStack {
            segments: &segments,
            deep_layers: &table.input.deep_layers,
        };
        let mut assignment = crate::read::DeepAssignment::new();
        assignment.set(5, k("a-unknown"), Value::Absent);
        assignment.set(5, k("a-shadowed"), Value::Present(vec![e("stale")]));
        assignment.set(5, k("n5-probe"), Value::Absent);
        assignment.set(8, k("z-probe-out"), Value::Present(vec![e("deep-only")]));

        // 深层旧值不存在：追加从空列表起步。
        let v =
            crate::read::read_with_deep_values(&stack, b"a-unknown", 60, &assignment).unwrap();
        assert_eq!(v, Value::Present(vec![e("u1"), e("u2")]));

        // 墓碑 20 删除了 n5-probe 的深层旧值，之后无追加 => 不存在。
        let v =
            crate::read::read_with_deep_values(&stack, b"n5-probe", 60, &assignment).unwrap();
        assert_eq!(v, Value::Absent);

        // 墓碑区间之外的深层键不受影响。
        let v = crate::read::read_with_deep_values(
            &stack,
            b"z-probe-out",
            60,
            &assignment,
        )
        .unwrap();
        assert_eq!(v, Value::Present(vec![e("deep-only")]));

        // n2-hit：墓碑删除后由追加重新产生。
        let v =
            crate::read::read_with_deep_values(&stack, b"n2-hit", 60, &assignment).unwrap();
        assert_eq!(v, Value::Present(vec![e("hit-b")]));
    }
}
