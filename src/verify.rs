//! 压实前后的等价性验证。
//!
//! 对所有开放快照序号与当前读序号、以及全部相关键（点记录键 + 探针键）：
//!
//! - 先比较符号读取结果：任何允许的深层旧值下都相等的两个符号值，
//!   其符号表示必然相等（同一未知基值 + 同一后缀，或同一具体列表）；
//! - 再枚举验证点上所有未知基值的具体指派（不存在 / 若干候选列表），
//!   比较具体读取结果。
//!
//! 这样既覆盖“允许的深层旧值都得到相同结果”的题目要求，也能在小盘面
//! 上做到真正的穷举而不是抽样。

use std::collections::BTreeSet;

use crate::compaction::{
    check_evidence_closure, output_as_segments, CompactionInput, CompactionOutput,
};
use crate::error::EngineError;
use crate::model::Value;
use crate::read::{
    read_symbolic, read_with_deep_values, DeepAssignment, ReadStack, SymbolicValue,
};

/// 单个验证点上的一次不一致。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Mismatch {
    pub read_seq: u64,
    pub key: Vec<u8>,
    pub before: SymbolicValue,
    pub after: SymbolicValue,
}

/// 验证报告。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct VerificationReport {
    /// 比较的读点数量（开放快照 + 当前读序号，去重）。
    pub read_points: Vec<u64>,
    /// 比较的键数量。
    pub keys_checked: usize,
    /// 具体穷举的深层基值指派数量。
    pub assignments_exhausted: usize,
    /// 发现的不一致；为空即通过。
    pub mismatches: Vec<Mismatch>,
    /// 证据闭环是否通过。
    pub evidence_closure_ok: bool,
}

impl VerificationReport {
    pub fn is_ok(&self) -> bool {
        self.mismatches.is_empty() && self.evidence_closure_ok
    }
}

fn verification_keys(input: &CompactionInput) -> Vec<Vec<u8>> {
    let mut keys: BTreeSet<Vec<u8>> = input.verification_keys.iter().cloned().collect();
    for seg in &input.selected {
        for r in &seg.records {
            keys.insert(r.key.clone());
        }
    }
    keys.into_iter().collect()
}

fn read_points(input: &CompactionInput) -> Vec<u64> {
    let mut points: BTreeSet<u64> = input.open_snapshot_seqs.iter().copied().collect();
    points.insert(input.read_seq);
    points.into_iter().collect()
}

/// 收集各读点上引用到的未知基值 `(depth, key)`。
fn unknown_bases(
    stack: &ReadStack<'_>,
    keys: &[Vec<u8>],
    points: &[u64],
) -> Result<Vec<(usize, Vec<u8>)>, EngineError> {
    let mut all = BTreeSet::new();
    for p in points {
        for key in keys {
            if let SymbolicValue::UnknownBase { depth, key: k, .. } =
                read_symbolic(stack, key, *p)?
            {
                all.insert((depth, k));
            }
        }
    }
    Ok(all.into_iter().collect())
}

/// 穷举候选具体值：不存在、空列表、一到两个不同元素的非空列表。
fn candidate_values() -> Vec<Value> {
    vec![
        Value::Absent,
        Value::Present(vec![]),
        Value::Present(vec![b"deep-x".to_vec()]),
        Value::Present(vec![b"deep-y".to_vec(), b"deep-z".to_vec()]),
    ]
}

fn enumerate_assignments(bases: &[(usize, Vec<u8>)]) -> Vec<DeepAssignment> {
    let candidates = candidate_values();
    let mut results = vec![DeepAssignment::new()];
    for (depth, key) in bases {
        let mut next = Vec::new();
        for partial in &results {
            for value in &candidates {
                let mut assignment = partial.clone();
                assignment.set(*depth, key.clone(), value.clone());
                next.push(assignment);
            }
        }
        results = next;
    }
    results
}

/// 比较压实前后在全部开放快照、当前读序号、全部相关键上的读取结果。
pub fn equivalence_report(
    input: &CompactionInput,
    output: &CompactionOutput,
) -> Result<VerificationReport, EngineError> {
    let points = read_points(input);
    let keys = verification_keys(input);
    let post_segments = output_as_segments(output);

    let before = ReadStack {
        segments: &input.selected,
        deep_layers: &input.deep_layers,
    };
    let after = ReadStack {
        segments: &post_segments,
        deep_layers: &input.deep_layers,
    };

    let mut mismatches = Vec::new();

    // 1) 符号比较。
    for p in &points {
        for key in &keys {
            let b = read_symbolic(&before, key, *p)?;
            let a = read_symbolic(&after, key, *p)?;
            if a != b {
                mismatches.push(Mismatch {
                    read_seq: *p,
                    key: key.clone(),
                    before: b,
                    after: a,
                });
            }
        }
    }

    // 2) 具体指派穷举（未知基值较少时完全笛卡尔积；数量由调用场景控制）。
    let bases = unknown_bases(&before, &keys, &points)?;
    let assignments = enumerate_assignments(&bases);
    for assignment in &assignments {
        for p in &points {
            for key in &keys {
                let b = read_with_deep_values(&before, key, *p, assignment)?;
                let a = read_with_deep_values(&after, key, *p, assignment)?;
                if a != b {
                    // 回填符号形式便于报告阅读。
                    let bs = read_symbolic(&before, key, *p)?;
                    let as_ = read_symbolic(&after, key, *p)?;
                    mismatches.push(Mismatch {
                        read_seq: *p,
                        key: key.clone(),
                        before: bs,
                        after: as_,
                    });
                }
            }
        }
    }

    let closure_ok = check_evidence_closure(input, output, &collect_evidenced(output)).is_ok();

    Ok(VerificationReport {
        read_points: points,
        keys_checked: keys.len(),
        assignments_exhausted: assignments.len(),
        mismatches,
        evidence_closure_ok: closure_ok,
    })
}

fn collect_evidenced(output: &CompactionOutput) -> BTreeSet<crate::model::RecordId> {
    let mut set = BTreeSet::new();
    for seg in &output.segments {
        for o in &seg.records {
            set.extend(o.evidence.iter().copied());
        }
        for o in &seg.tombstones {
            set.extend(o.evidence.iter().copied());
        }
    }
    set
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::interval::KeyRange;
    use crate::model::{PointOp, Record, RecordId, Segment};

    fn k(s: &str) -> Vec<u8> {
        s.as_bytes().to_vec()
    }
    fn e(s: &str) -> Vec<u8> {
        s.as_bytes().to_vec()
    }

    #[test]
    fn report_passes_for_trivial_compaction() {
        let seg = Segment::new(
            0,
            vec![
                Record { id: RecordId(1), key: k("k"), seq: 1, op: PointOp::Put(vec![e("a")]) },
                Record { id: RecordId(2), key: k("k"), seq: 2, op: PointOp::Append(e("b")) },
            ],
            vec![],
        )
        .unwrap();
        let input = CompactionInput {
            selected: vec![seg],
            deep_layers: vec![],
            read_seq: 5,
            open_snapshot_seqs: vec![1, 3],
            output_boundaries: vec![],
            verification_keys: vec![],
        };
        let output = crate::compaction::compact(&input).unwrap();
        let report = equivalence_report(&input, &output).unwrap();
        assert!(report.is_ok(), "{report:?}");
        assert_eq!(report.read_points, vec![1, 3, 5]);
    }

    #[test]
    fn exhaustive_deep_assignments_covered() {
        let deep = crate::model::DeepLayer {
            depth: 7,
            key_ranges: vec![KeyRange::new(Some(k("a")), Some(k("z"))).unwrap()],
        };
        let seg = Segment::new(
            0,
            vec![Record {
                id: RecordId(1),
                key: k("k"),
                seq: 4,
                op: PointOp::Append(e("tail")),
            }],
            vec![],
        )
        .unwrap();
        let input = CompactionInput {
            selected: vec![seg],
            deep_layers: vec![deep],
            read_seq: 8,
            open_snapshot_seqs: vec![3, 6],
            output_boundaries: vec![],
            verification_keys: vec![k("probe")],
        };
        let output = crate::compaction::compact(&input).unwrap();
        let report = equivalence_report(&input, &output).unwrap();
        assert!(report.is_ok(), "{report:?}");
        // 两个未知基值（k 与 probe）各 4 个候选 => 16 个指派。
        assert_eq!(report.assignments_exhausted, 16);
        assert_eq!(report.keys_checked, 2);
    }
}
