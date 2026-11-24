//! 按序号恢复键值的读取语义。
//!
//! 同一读序号下，可见记录按序号回放；序号相同的点记录与范围墓碑在合法
//! 输入中不会出现（序号全局唯一）。深层未选段的旧值是未知符号，
//! [`read_symbolic`] 以 [`SymbolicValue`] 表达，[`read_with_deep_values`]
//! 在给定具体指派后求值。

use std::collections::BTreeMap;

use crate::error::EngineError;
use crate::model::{DeepLayer, PointOp, Segment, Value};
#[cfg(test)]
use crate::model::{RangeTombstone, Record};

/// 回放过程中的符号值。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SymbolicValue {
    /// 不存在。
    Absent,
    /// 完全由已选段记录决定的具体列表（含空列表写入）。
    Concrete(Vec<Vec<u8>>),
    /// 解析到某个深层旧基值，其后又追加了 `suffix` 个（按顺序的）元素。
    /// 两个符号值相等当且仅当来自同一层同一键、且后缀完全一致。
    UnknownBase {
        depth: usize,
        key: Vec<u8>,
        suffix: Vec<Vec<u8>>,
    },
}

impl SymbolicValue {
    /// 是否存在（空列表也为存在）。
    pub fn exists(&self) -> bool {
        !matches!(self, SymbolicValue::Absent)
    }
}

/// 深层旧基值的具体指派：`(depth, key) -> Absent 或具体列表`。
#[derive(Debug, Clone, Default)]
pub struct DeepAssignment {
    values: BTreeMap<(usize, Vec<u8>), Value>,
}

impl DeepAssignment {
    pub fn new() -> Self {
        Self::default()
    }

    /// 指派某层某键的旧值。
    pub fn set(&mut self, depth: usize, key: Vec<u8>, value: Value) {
        self.values.insert((depth, key), value);
    }

    fn get(&self, depth: usize, key: &[u8]) -> Option<&Value> {
        self.values.get(&(depth, key.to_vec()))
    }
}

#[derive(Debug, Clone)]
enum EventKind {
    Point(PointOp),
    RangeDelete,
}

#[derive(Debug, Clone)]
struct Event {
    seq: u64,
    depth: usize,
    kind: EventKind,
}

/// 可供读取的层叠结构：若干已知段 + 若干只含键区间的深层。
/// depth 较小者较新。
#[derive(Debug, Clone)]
pub struct ReadStack<'a> {
    pub segments: &'a [Segment],
    pub deep_layers: &'a [DeepLayer],
}

fn collect_events(key: &[u8], segments: &[Segment]) -> Vec<Event> {
    let mut events = Vec::new();
    for seg in segments {
        for r in seg.records.iter().filter(|r| r.key.as_slice() == key) {
            events.push(Event {
                seq: r.seq,
                depth: seg.depth,
                kind: EventKind::Point(r.op.clone()),
            });
        }
        for t in seg.tombstones.iter().filter(|t| t.range.contains(key)) {
            events.push(Event {
                seq: t.seq,
                depth: seg.depth,
                kind: EventKind::RangeDelete,
            });
        }
    }
    // 序号全局唯一；depth 仅作稳定次序的兜底。
    events.sort_by(|a, b| a.seq.cmp(&b.seq).then(a.depth.cmp(&b.depth)));
    events
}

fn initial_symbolic(key: &[u8], deep_layers: &[DeepLayer]) -> SymbolicValue {
    // depth 最小（最新）且覆盖该键的深层胜出，模拟层叠遮蔽。
    let mut winner: Option<&DeepLayer> = None;
    for layer in deep_layers {
        let covers = layer.key_ranges.iter().any(|r| r.contains(key));
        if covers && winner.map_or(true, |w| layer.depth < w.depth) {
            winner = Some(layer);
        }
    }
    match winner {
        Some(layer) => SymbolicValue::UnknownBase {
            depth: layer.depth,
            key: key.to_vec(),
            suffix: Vec::new(),
        },
        None => SymbolicValue::Absent,
    }
}

fn apply_symbolic(state: &mut SymbolicValue, kind: &EventKind) {
    match kind {
        EventKind::Point(PointOp::Put(v)) => *state = SymbolicValue::Concrete(v.clone()),
        EventKind::Point(PointOp::Delete) | EventKind::RangeDelete => {
            *state = SymbolicValue::Absent
        }
        EventKind::Point(PointOp::Append(elem)) => match state {
            SymbolicValue::Absent => {
                *state = SymbolicValue::Concrete(vec![elem.clone()]);
            }
            SymbolicValue::Concrete(list) => list.push(elem.clone()),
            SymbolicValue::UnknownBase { suffix, .. } => suffix.push(elem.clone()),
        },
    }
}

/// 在读序号 `read_seq` 下恢复 `key` 的符号值。
/// 序号大于 `read_seq` 的记录不可见。
pub fn read_symbolic(
    stack: &ReadStack<'_>,
    key: &[u8],
    read_seq: u64,
) -> Result<SymbolicValue, EngineError> {
    let mut state = initial_symbolic(key, stack.deep_layers);
    for event in collect_events(key, stack.segments) {
        if event.seq > read_seq {
            break;
        }
        apply_symbolic(&mut state, &event.kind);
    }
    Ok(state)
}

fn resolve(sym: &SymbolicValue, assignment: &DeepAssignment) -> Result<Value, EngineError> {
    match sym {
        SymbolicValue::Absent => Ok(Value::Absent),
        SymbolicValue::Concrete(v) => Ok(Value::Present(v.clone())),
        SymbolicValue::UnknownBase { depth, key, suffix } => {
            match assignment.get(*depth, key) {
                Some(Value::Absent) => Ok(Value::Present(suffix.clone())),
                Some(Value::Present(base)) => {
                    let mut v = base.clone();
                    v.extend(suffix.iter().cloned());
                    Ok(Value::Present(v))
                }
                None => Err(EngineError::MissingDeepValue {
                    depth: *depth,
                    key: key.clone(),
                }),
            }
        }
    }
}

/// 给定深层旧值指派后的具体读取。
pub fn read_with_deep_values(
    stack: &ReadStack<'_>,
    key: &[u8],
    read_seq: u64,
    assignment: &DeepAssignment,
) -> Result<Value, EngineError> {
    let sym = read_symbolic(stack, key, read_seq)?;
    resolve(&sym, assignment)
}

/// 收集一次读取所依赖的全部 `(depth, key)` 未知基值，供验证枚举使用。
pub fn referenced_unknown_bases(
    stack: &ReadStack<'_>,
    keys: &[Vec<u8>],
    read_seq: u64,
) -> Result<Vec<(usize, Vec<u8>)>, EngineError> {
    let mut out = Vec::new();
    for key in keys {
        if let SymbolicValue::UnknownBase { depth, key: k, .. } =
            read_symbolic(stack, key, read_seq)?
        {
            let entry = (depth, k);
            if !out.contains(&entry) {
                out.push(entry);
            }
        }
    }
    Ok(out)
}

/// 仅用于内部测试的便捷构造器。
#[cfg(test)]
fn rec(id: u64, key: &str, seq: u64, op: PointOp) -> Record {
    Record { id: crate::model::RecordId(id), key: key.as_bytes().to_vec(), seq, op }
}

#[cfg(test)]
fn tomb(id: u64, seq: u64, start: Option<&str>, end: Option<&str>) -> RangeTombstone {
    use crate::interval::KeyRange;
    RangeTombstone {
        id: crate::model::RecordId(id),
        seq,
        range: KeyRange::new(
            start.map(|s| s.as_bytes().to_vec()),
            end.map(|s| s.as_bytes().to_vec()),
        )
        .unwrap(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::interval::KeyRange;

    fn elem(s: &str) -> Vec<u8> {
        s.as_bytes().to_vec()
    }

    #[test]
    fn put_delete_append_replay_ordering() {
        let seg = Segment::new(
            0,
            vec![
                rec(1, "k", 1, PointOp::Put(vec![elem("a")])),
                rec(2, "k", 2, PointOp::Append(elem("b"))),
                rec(3, "k", 3, PointOp::Delete),
                rec(4, "k", 4, PointOp::Append(elem("c"))),
            ],
            vec![],
        )
        .unwrap();
        let stack = ReadStack { segments: &[seg], deep_layers: &[] };

        assert_eq!(read_symbolic(&stack, b"k", 1).unwrap(),
            SymbolicValue::Concrete(vec![elem("a")]));
        assert_eq!(read_symbolic(&stack, b"k", 2).unwrap(),
            SymbolicValue::Concrete(vec![elem("a"), elem("b")]));
        // 删除后不存在。
        assert_eq!(read_symbolic(&stack, b"k", 3).unwrap(), SymbolicValue::Absent);
        // 对缺失键追加，以空列表起步。
        assert_eq!(read_symbolic(&stack, b"k", 4).unwrap(),
            SymbolicValue::Concrete(vec![elem("c")]));
        // 超出最大序号的读点与最大序号一致。
        assert_eq!(read_symbolic(&stack, b"k", 99).unwrap(),
            read_symbolic(&stack, b"k", 4).unwrap());
    }

    #[test]
    fn empty_put_is_present_and_range_tombstone_resets() {
        let seg = Segment::new(
            0,
            vec![
                rec(1, "k", 1, PointOp::Put(vec![])),
                rec(2, "k", 2, PointOp::Append(elem("x"))),
                rec(3, "other", 2, PointOp::Put(vec![])),
            ],
            vec![tomb(4, 3, Some("j"), Some("l"))],
        )
        .unwrap();
        let stack = ReadStack { segments: &[seg], deep_layers: &[] };

        assert!(read_symbolic(&stack, b"k", 1).unwrap().exists());
        assert_eq!(read_symbolic(&stack, b"k", 1).unwrap(),
            SymbolicValue::Concrete(vec![]));
        assert_eq!(read_symbolic(&stack, b"k", 3).unwrap(), SymbolicValue::Absent);
        // 区间外的键不受墓碑影响。
        assert_eq!(read_symbolic(&stack, b"other", 3).unwrap(),
            SymbolicValue::Concrete(vec![]));
    }

    #[test]
    fn unknown_deep_base_symbolic_and_assignment() {
        let deep = DeepLayer {
            depth: 5,
            key_ranges: vec![KeyRange::new(Some(b"a".to_vec()), Some(b"z".to_vec())).unwrap()],
        };
        let seg = Segment::new(
            0,
            vec![rec(1, "k", 10, PointOp::Append(elem("tail")))],
            vec![],
        )
        .unwrap();
        let stack = ReadStack { segments: &[seg], deep_layers: &[deep] };

        let sym = read_symbolic(&stack, b"k", 10).unwrap();
        assert_eq!(sym, SymbolicValue::UnknownBase {
            depth: 5,
            key: b"k".to_vec(),
            suffix: vec![elem("tail")],
        });

        let mut absent_base = DeepAssignment::new();
        absent_base.set(5, b"k".to_vec(), Value::Absent);
        assert_eq!(
            read_with_deep_values(&stack, b"k", 10, &absent_base).unwrap(),
            Value::Present(vec![elem("tail")])
        );

        let mut present_base = DeepAssignment::new();
        present_base.set(5, b"k".to_vec(), Value::Present(vec![elem("old")]));
        assert_eq!(
            read_with_deep_values(&stack, b"k", 10, &present_base).unwrap(),
            Value::Present(vec![elem("old"), elem("tail")])
        );

        // 未指派时报错，而不是静默猜测。
        assert!(read_with_deep_values(&stack, b"k", 10, &DeepAssignment::new()).is_err());
    }

    #[test]
    fn newer_deep_layer_shadows_older() {
        let older = DeepLayer {
            depth: 9,
            key_ranges: vec![KeyRange::universe()],
        };
        let newer = DeepLayer {
            depth: 6,
            key_ranges: vec![KeyRange::new(Some(b"k".to_vec()), Some(b"l".to_vec())).unwrap()],
        };
        let stack = ReadStack { segments: &[], deep_layers: &[older, newer] };
        assert_eq!(
            read_symbolic(&stack, b"k", 1).unwrap(),
            SymbolicValue::UnknownBase { depth: 6, key: b"k".to_vec(), suffix: vec![] }
        );
        assert_eq!(
            read_symbolic(&stack, b"z", 1).unwrap(),
            SymbolicValue::UnknownBase { depth: 9, key: b"z".to_vec(), suffix: vec![] }
        );
    }
}
