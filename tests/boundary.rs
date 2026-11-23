//! Boundary and malformed-input scenarios.
mod common;

use common::{commit, row, schema};
use logical_decoder::{Decoder, DecoderError, FeedError, RowOp, Scalar};

#[test]
fn empty_committed_transaction_is_published_without_changes() {
    let mut decoder = Decoder::new(10).unwrap();
    // A transaction whose only fragment carries an empty schema.
    decoder.feed(schema(1, 1, 1, 9, 1, vec![])).unwrap();
    decoder.feed(commit(2, 1, 1)).unwrap();

    let batch = decoder.next_batch().unwrap();
    assert_eq!(batch.transactions.len(), 1);
    assert!(batch.transactions[0].changes.is_empty());
}

#[test]
fn null_values_and_unknown_column_ids_are_reported_not_dropped() {
    let mut decoder = Decoder::new(10).unwrap();
    decoder
        .feed(schema(1, 1, 1, 9, 1, vec![(1, "k", "int"), (2, "v", "text")]))
        .unwrap();
    decoder
        .feed(row(
            2,
            1,
            2,
            9,
            1,
            RowOp::Insert,
            vec![(1, Scalar::Null), (99, Scalar::Int(7))],
        ))
        .unwrap();
    decoder.feed(commit(3, 1, 2)).unwrap();

    let change = &decoder.next_batch().unwrap().transactions[0].changes[0];
    assert_eq!(change.columns[0].value, Some(Scalar::Null));
    assert_eq!(change.columns[1].value, None); // not carried
    assert_eq!(change.unresolved.get(&99), Some(&Scalar::Int(7)));
}

#[test]
fn invalid_fragment_and_late_fragment_are_rejected_same_fragment_appends() {
    let mut decoder = Decoder::new(10).unwrap();

    let err = decoder
        .feed(row(1, 1, 0, 9, 1, RowOp::Insert, vec![]))
        .unwrap_err();
    assert!(matches!(err, FeedError::InvalidFragmentNumber { .. }));

    decoder
        .feed(schema(1, 1, 1, 9, 1, vec![(1, "k", "int")]))
        .unwrap();
    // A second record on the same fragment number is allowed and appended in
    // log order (a fragment can carry several records).
    decoder
        .feed(row(2, 1, 1, 9, 1, RowOp::Insert, vec![(1, Scalar::Int(7))]))
        .unwrap();
    decoder.feed(commit(3, 1, 1)).unwrap();

    let batch = decoder.next_batch().unwrap();
    assert_eq!(batch.transactions[0].changes.len(), 1);
    assert_eq!(
        batch.transactions[0].changes[0].columns[0].value,
        Some(Scalar::Int(7))
    );

    // Records arriving after finalization are still rejected.
    let err = decoder
        .feed(row(4, 1, 2, 9, 1, RowOp::Insert, vec![]))
        .unwrap_err();
    assert!(matches!(err, FeedError::FragmentAfterFinalize { .. }));

    let err = decoder.feed(commit(5, 1, 1)).unwrap_err();
    assert!(matches!(err, FeedError::AlreadyFinalized { .. }));
}

#[test]
fn duplicate_commit_lsn_is_rejected() {
    let mut decoder = Decoder::new(10).unwrap();
    decoder
        .feed(row(1, 1, 1, 9, 1, RowOp::Insert, vec![]))
        .unwrap();
    decoder
        .feed(row(2, 2, 1, 9, 1, RowOp::Insert, vec![]))
        .unwrap();
    decoder.feed(commit(10, 1, 1)).unwrap();
    let err = decoder.feed(commit(10, 2, 1)).unwrap_err();
    assert!(matches!(
        err,
        FeedError::DuplicateCommitLsn { lsn: 10, .. }
    ));
}

#[test]
fn zero_batch_size_and_inconsistent_resume_are_rejected() {
    assert!(matches!(
        Decoder::new(0),
        Err(DecoderError::InvalidBatchSize { .. })
    ));
    assert!(matches!(
        Decoder::resume(2, 100, 3, 3),
        Err(DecoderError::InconsistentResume { .. })
    ));
}

#[test]
fn acknowledgement_is_cumulative_and_cannot_go_backwards() {
    let mut decoder = Decoder::new(1).unwrap();
    decoder
        .feed(schema(1, 1, 1, 9, 1, vec![(1, "k", "int")]))
        .unwrap();
    let txns = [1u64, 2, 3];
    for (i, txn_id) in txns.iter().enumerate() {
        // txn 1 already owns fragment 1 (its schema), so use fragment 2.
        let fragment = if *txn_id == 1 { 2 } else { 1 };
        decoder
            .feed(row(
                2 + i as u64,
                *txn_id,
                fragment,
                9,
                1,
                RowOp::Insert,
                vec![],
            ))
            .unwrap();
    }
    decoder.feed(commit(10, 1, 2)).unwrap();
    decoder.feed(commit(11, 2, 1)).unwrap();
    decoder.feed(commit(12, 3, 1)).unwrap();

    // Unacknowledged batches are retained with stable numbers and content;
    // next_batch repeatedly redelivers the lowest-numbered one.
    let b1 = decoder.next_batch().unwrap();
    let b1_again = decoder.next_batch().unwrap();
    assert_eq!(b1, b1_again);
    let all = decoder.pending_batches();
    let numbers: Vec<u64> = all.iter().map(|b| b.number).collect();
    assert_eq!(numbers, vec![1, 2, 3]);

    assert!(decoder.acknowledge(0).is_err());
    decoder.acknowledge(2).unwrap();
    assert_eq!(decoder.acknowledged_batch(), 2);
    assert_eq!(decoder.confirmed_lsn(), 11);
    assert!(matches!(
        decoder.acknowledge(1),
        Err(DecoderError::AckOutOfOrder { .. })
    ));
    assert!(matches!(
        decoder.acknowledge(9),
        Err(DecoderError::UnknownBatch { .. })
    ));
    // After acking 1..=2 only batch 3 remains retained.
    assert_eq!(
        decoder.pending_batches().iter().map(|b| b.number).collect::<Vec<_>>(),
        vec![3]
    );
    decoder.acknowledge(3).unwrap();
    assert_eq!(decoder.confirmed_lsn(), 12);
    assert!(decoder.pending_batches().is_empty());
}
