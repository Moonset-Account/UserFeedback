//! 范围墓碑与输出边界的专项测试：多边界拆分、完全死亡回收、
//! 多墓碑序号交错、以及与深层区间相交时的保守保留。

use std::collections::BTreeSet;

use local_compaction::interval::KeyRange;
use local_compaction::model::{DeepLayer, PointOp, RangeTombstone, Record, RecordId, Segment};
use local_compaction::{compact, equivalence_report, CompactionInput};

fn k(s: &str) -> Vec<u8> {
    s.as_bytes().to_vec()
}

#[test]
fn tombstone_splits_across_multiple_output_boundaries() {
    let seg = Segment::new(
        0,
        vec![Record {
            id: RecordId(1),
            key: k("k-mid"),
            seq: 5,
            op: PointOp::Delete,
        }],
        vec![RangeTombstone {
            id: RecordId(2),
            seq: 8,
            range: KeyRange::new(Some(k("a")), Some(k("z"))).unwrap(),
        }],
    )
    .unwrap();

    let input = CompactionInput {
        selected: vec![seg],
        deep_layers: vec![],
        read_seq: 20,
        open_snapshot_seqs: vec![10],
        output_boundaries: vec![k("g"), k("m"), k("s")],
        verification_keys: vec![],
    };
    let output = compact(&input).unwrap();

    // 墓碑需要覆盖点键 k-mid（其删除在墓碑之前，墓碑之后无写入，
    // 读者 10/20 看到的删除可由点删除承载；墓碑在该点之外与深层不相交，
    // 因此只有包含点键的最小子区间存活并被三个输出边界切成多片）。
    let ranges: Vec<KeyRange> = output
        .segments
        .iter()
        .flat_map(|s| s.tombstones.iter().map(|o| o.tombstone.range.clone()))
        .collect();
    // 存活区间 [k-mid, z) 被边界 m、s 切开（g 在其左侧不影响）。
    let joined: Vec<(Option<Vec<u8>>, Option<Vec<u8>>)> =
        ranges.iter().map(|r| (r.start.clone(), r.end.clone())).collect();
    assert!(
        joined.contains(&(Some(k("k-mid")), Some(k("m"))))
            || joined.contains(&(Some(k("k-mid")), Some(k("s"))))
            || joined.contains(&(Some(k("k-mid")), Some(k("z"))))
    );
    // 至少跨过两个输出段。
    let depths: BTreeSet<usize> = output
        .segments
        .iter()
        .filter(|s| !s.tombstones.is_empty())
        .map(|s| s.depth)
        .collect();
    assert!(depths.len() >= 2);

    let report = equivalence_report(&input, &output).unwrap();
    assert!(report.is_ok());
}

#[test]
fn fully_dead_tombstone_is_entirely_reclaimed() {
    // 墓碑区间内无点键，且没有任何深层区间相交。
    let seg = Segment::new(
        0,
        vec![Record {
            id: RecordId(1),
            key: k("outside"),
            seq: 3,
            op: PointOp::Put(vec![b"v".to_vec()]),
        }],
        vec![RangeTombstone {
            id: RecordId(2),
            seq: 4,
            range: KeyRange::new(Some(k("0")), Some(k("1"))).unwrap(),
        }],
    )
    .unwrap();
    let input = CompactionInput {
        selected: vec![seg],
        deep_layers: vec![],
        read_seq: 10,
        open_snapshot_seqs: vec![6],
        output_boundaries: vec![],
        verification_keys: vec![],
    };
    let output = compact(&input).unwrap();
    assert!(output.segments.iter().all(|s| s.tombstones.is_empty()));
    let ids: BTreeSet<u64> = output.reclaimed.iter().map(|r| r.source.0).collect();
    assert!(ids.contains(&2));
    assert!(equivalence_report(&input, &output).unwrap().is_ok());
}

#[test]
fn overlapping_deep_range_forces_tombstone_retention() {
    // 深层区间与墓碑相交：即便没有点键需要，墓碑也必须保守保留以遮蔽旧值。
    let deep = DeepLayer {
        depth: 7,
        key_ranges: vec![KeyRange::new(Some(k("n")), Some(k("p"))).unwrap()],
    };
    let seg = Segment::new(
        0,
        vec![],
        vec![RangeTombstone {
            id: RecordId(9),
            seq: 5,
            range: KeyRange::new(Some(k("a")), Some(k("z"))).unwrap(),
        }],
    )
    .unwrap();
    let input = CompactionInput {
        selected: vec![seg],
        deep_layers: vec![deep],
        read_seq: 12,
        open_snapshot_seqs: vec![8],
        output_boundaries: vec![],
        verification_keys: vec![k("n-probe")],
    };
    let output = compact(&input).unwrap();
    let kept = output
        .segments
        .iter()
        .flat_map(|s| s.tombstones.iter())
        .any(|o| o.evidence.contains(&RecordId(9)));
    assert!(kept, "tombstone must be retained over the unknown deep key");
    let report = equivalence_report(&input, &output).unwrap();
    assert!(report.is_ok());
}

#[test]
fn multiple_tombstones_interleaved_with_point_records() {
    let seg = Segment::new(
        0,
        vec![
            Record { id: RecordId(1), key: k("k1"), seq: 2, op: PointOp::Put(vec![b"a".to_vec()]) },
            Record { id: RecordId(2), key: k("k2"), seq: 7, op: PointOp::Append(b"x".to_vec()) },
        ],
        vec![
            RangeTombstone {
                id: RecordId(3),
                seq: 4,
                range: KeyRange::new(Some(k("k0")), Some(k("k5"))).unwrap(),
            },
            RangeTombstone {
                id: RecordId(4),
                seq: 9,
                range: KeyRange::new(Some(k("k1")), Some(k("k3"))).unwrap(),
            },
        ],
    )
    .unwrap();
    let input = CompactionInput {
        selected: vec![seg],
        deep_layers: vec![],
        read_seq: 15,
        open_snapshot_seqs: vec![3, 6, 10],
        output_boundaries: vec![k("k2")],
        verification_keys: vec![],
    };
    let output = compact(&input).unwrap();
    let report = equivalence_report(&input, &output).unwrap();
    assert!(report.is_ok(), "{:?}", report.mismatches);
    // 输出段数 = 边界数 + 1。
    assert_eq!(output.segments.len(), 2);
}
