//! Hole (undecodable commit) scenarios: missing schemas and fragments.
mod common;

use common::{commit, row, schema};
use logical_decoder::{BlockReason, Decoder, RowOp, Scalar};

#[test]
fn missing_schema_hole_reports_gap_and_blocks_later_commits() {
    let mut decoder = Decoder::new(10).unwrap();
    // Txn 1 references table 5 v1 but no schema declaration was ever seen.
    decoder
        .feed(row(
            1,
            1,
            1,
            5,
            1,
            RowOp::Insert,
            vec![(1, Scalar::Int(1))],
        ))
        .unwrap();
    decoder.feed(commit(2, 1, 1)).unwrap();

    // Txn 2 is decodable but commits after the hole.
    decoder
        .feed(schema(3, 2, 1, 8, 1, vec![(1, "id", "int")]))
        .unwrap();
    decoder
        .feed(row(
            4,
            2,
            2,
            8,
            1,
            RowOp::Insert,
            vec![(1, Scalar::Int(2))],
        ))
        .unwrap();
    decoder.feed(commit(5, 2, 2)).unwrap();

    assert!(decoder.next_batch().is_none());
    let hole = decoder.first_hole().expect("a hole must be reported");
    match hole {
        BlockReason::MissingSchema {
            txn_id,
            commit_lsn,
            table_id,
            schema_version,
            referenced_at,
        } => {
            assert_eq!(txn_id, 1);
            assert_eq!(commit_lsn, 2);
            assert_eq!(table_id, 5);
            assert_eq!(schema_version, 1);
            assert_eq!(referenced_at, 1);
        }
        other => panic!("expected missing schema hole, got {other:?}"),
    }
    // The later transaction must not cross the hole.
    assert!(decoder.next_batch().is_none());
}

#[test]
fn incomplete_fragments_hole_lists_every_missing_fragment() {
    let mut decoder = Decoder::new(10).unwrap();
    decoder
        .feed(schema(1, 1, 1, 9, 1, vec![(1, "k", "int")]))
        .unwrap();
    // Fragment 2 never arrives; fragment 3 does.
    decoder
        .feed(row(
            2,
            1,
            3,
            9,
            1,
            RowOp::Insert,
            vec![(1, Scalar::Int(3))],
        ))
        .unwrap();
    decoder.feed(commit(3, 1, 3)).unwrap();

    let hole = decoder.first_hole().unwrap();
    match hole {
        BlockReason::Incomplete {
            txn_id,
            last_fragment,
            missing,
            ..
        } => {
            assert_eq!(txn_id, 1);
            assert_eq!(last_fragment, 3);
            assert_eq!(missing, vec![2]);
        }
        other => panic!("expected incomplete hole, got {other:?}"),
    }
    assert!(decoder.next_batch().is_none());
}

#[test]
fn commit_without_any_retained_fragment_is_a_hole() {
    let mut decoder = Decoder::new(10).unwrap();
    // Decoder starts mid-stream: only a commit record is observed.
    decoder.feed(commit(50, 99, 4)).unwrap();
    let hole = decoder.first_hole().unwrap();
    match hole {
        BlockReason::Incomplete {
            txn_id,
            last_fragment,
            missing,
            ..
        } => {
            assert_eq!(txn_id, 99);
            assert_eq!(last_fragment, 4);
            assert_eq!(missing, vec![1, 2, 3, 4]);
        }
        other => panic!("expected incomplete hole, got {other:?}"),
    }
}

#[test]
fn hole_does_not_clear_when_only_later_transactions_arrive() {
    let mut decoder = Decoder::new(10).unwrap();
    decoder
        .feed(row(1, 1, 1, 5, 1, RowOp::Insert, vec![]))
        .unwrap();
    decoder.feed(commit(2, 1, 1)).unwrap();
    decoder
        .feed(schema(3, 2, 1, 6, 1, vec![(1, "id", "int")]))
        .unwrap();
    decoder.feed(commit(4, 2, 1)).unwrap();
    // Even more traffic cannot move publication past the hole.
    decoder.feed(schema(5, 3, 1, 7, 1, vec![])).unwrap();
    decoder.feed(commit(6, 3, 1)).unwrap();

    let holes = decoder.holes();
    assert_eq!(holes.len(), 1);
    assert!(decoder.next_batch().is_none());
    assert_eq!(decoder.open_transaction_count(), 0);
}

#[test]
fn multiple_holes_are_reported_in_commit_lsn_order() {
    let mut decoder = Decoder::new(10).unwrap();
    // Hole 1: missing schema.
    decoder
        .feed(row(1, 1, 1, 5, 1, RowOp::Insert, vec![]))
        .unwrap();
    decoder.feed(commit(2, 1, 1)).unwrap();
    // Hole 2: incomplete fragments (fragment 1 seen, fragment 2 missing).
    decoder
        .feed(schema(3, 2, 1, 6, 1, vec![(1, "id", "int")]))
        .unwrap();
    decoder.feed(commit(4, 2, 2)).unwrap();

    let holes = decoder.holes();
    assert_eq!(holes.len(), 2);
    assert!(matches!(holes[0], BlockReason::MissingSchema { commit_lsn: 2, .. }));
    assert!(matches!(holes[1], BlockReason::Incomplete { commit_lsn: 4, .. }));
}
