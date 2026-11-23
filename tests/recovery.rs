//! Restart/replay, acknowledgement and unacknowledged-batch retention.
mod common;

use common::{commit, row, schema};
use logical_decoder::{Decoder, RowOp, Scalar};

#[test]
fn restart_replay_skips_confirmed_commits_and_continues_batch_numbers() {
    // First incarnation: publish two batches, only acknowledge batch 1.
    let mut first = Decoder::new(1).unwrap();
    first
        .feed(schema(1, 1, 1, 9, 1, vec![(1, "k", "int")]))
        .unwrap();
    for (i, txn_id) in [1u64, 2, 3].iter().enumerate() {
        first
            .feed(row(
                2 + i as u64,
                *txn_id,
                1,
                9,
                1,
                RowOp::Insert,
                vec![(1, Scalar::Int(*txn_id as i64))],
            ))
            .unwrap();
    }
    first.feed(commit(10, 1, 1)).unwrap();
    first.feed(commit(11, 2, 1)).unwrap();
    first.feed(commit(12, 3, 1)).unwrap();

    let sealed = first.pending_batches();
    assert_eq!(sealed.len(), 3);
    let b1 = sealed[0].clone();
    let b2 = sealed[1].clone();
    assert_eq!(b1.transactions[0].txn_id, 1);
    assert_eq!(b2.number, 2);
    first.acknowledge(1).unwrap();
    assert_eq!(first.confirmed_lsn(), 10);

    // Restart from a position before the confirmed point and replay the
    // log. The first re-derived unacknowledged batch must get its original
    // number (2), so the decoder resumes numbering at acknowledged+1.
    let mut restarted = Decoder::resume(1, 10, 1, 2).unwrap();
    restarted
        .feed_all(vec![
            schema(1, 1, 1, 9, 1, vec![(1, "k", "int")]),
            row(2, 1, 1, 9, 1, RowOp::Insert, vec![(1, Scalar::Int(1))]),
            row(3, 2, 1, 9, 1, RowOp::Insert, vec![(1, Scalar::Int(2))]),
            row(4, 3, 1, 9, 1, RowOp::Insert, vec![(1, Scalar::Int(3))]),
            commit(10, 1, 1),
            commit(11, 2, 1),
            commit(12, 3, 1),
        ])
        .unwrap();

    // Batch 1 (lsn 10) is at/below confirmed_lsn and consumed silently.
    // The previously unacknowledged batches 2 and 3 come back with their
    // original numbers and identical content.
    let replayed = restarted.pending_batches();
    assert_eq!(replayed.len(), 2);
    assert_eq!(replayed[0].number, 2);
    assert_eq!(replayed[0].transactions[0].txn_id, 2);
    assert_eq!(replayed[0], b2);
    assert_eq!(replayed[1].number, 3);
    assert_eq!(replayed[1].transactions[0].txn_id, 3);
    assert_eq!(restarted.next_batch_number(), 4);

    // Acknowledging the replayed batches advances the confirmed position.
    restarted.acknowledge(3).unwrap();
    assert_eq!(restarted.confirmed_lsn(), 12);
}

#[test]
fn restart_replay_can_heal_a_missing_schema_hole() {
    // First incarnation starts too late: the schema declaration is missing.
    let mut first = Decoder::new(10).unwrap();
    first
        .feed(row(
            2,
            1,
            1,
            9,
            1,
            RowOp::Insert,
            vec![(1, Scalar::Int(1))],
        ))
        .unwrap();
    first.feed(commit(3, 1, 1)).unwrap();
    assert!(first.first_hole().is_some());

    // Restart earlier in the log so the schema declaration is re-read.
    let mut restarted = Decoder::resume(10, 0, 0, 1).unwrap();
    restarted
        .feed_all(vec![
            schema(1, 1, 1, 9, 1, vec![(1, "k", "int")]),
            row(2, 1, 1, 9, 1, RowOp::Insert, vec![(1, Scalar::Int(1))]),
            commit(3, 1, 1),
        ])
        .unwrap();
    let batch = restarted.next_batch().unwrap();
    assert_eq!(batch.transactions[0].txn_id, 1);
    assert!(restarted.holes().is_empty());
}

#[test]
fn unacknowledged_batches_keep_number_and_content_until_ack() {
    let mut decoder = Decoder::new(1).unwrap();
    decoder
        .feed(schema(1, 1, 1, 9, 1, vec![(1, "k", "int")]))
        .unwrap();
    decoder
        .feed(row(
            2,
            1,
            1,
            9,
            1,
            RowOp::Insert,
            vec![(1, Scalar::Int(1))],
        ))
        .unwrap();
    decoder.feed(commit(3, 1, 1)).unwrap();
    decoder
        .feed(row(
            4,
            2,
            1,
            9,
            1,
            RowOp::Insert,
            vec![(1, Scalar::Int(2))],
        ))
        .unwrap();
    decoder.feed(commit(5, 2, 1)).unwrap();

    let sealed = decoder.pending_batches();
    assert_eq!(sealed.len(), 2);
    let b1 = &sealed[0];
    let b2 = &sealed[1];
    assert_eq!(b1.number, 1);
    assert_eq!(b2.number, 2);
    assert_ne!(b1.transactions[0], b2.transactions[0]);

    // Repeated redelivery returns the same number and identical content.
    let redelivered = decoder.next_batch().unwrap();
    assert_eq!(redelivered, *b1);

    // A third transaction continues the numbering without disturbing the
    // two retained batches.
    decoder
        .feed(row(
            6,
            3,
            1,
            9,
            1,
            RowOp::Insert,
            vec![(1, Scalar::Int(3))],
        ))
        .unwrap();
    decoder.feed(commit(7, 3, 1)).unwrap();
    let sealed = decoder.pending_batches();
    assert_eq!(sealed.iter().map(|b| b.number).collect::<Vec<_>>(), vec![1, 2, 3]);

    // Cumulative acknowledgement of 3 also retires 1 and 2.
    decoder.acknowledge(3).unwrap();
    assert_eq!(decoder.acknowledged_batch(), 3);
    assert_eq!(decoder.confirmed_lsn(), 7);
    assert!(decoder.pending_batches().is_empty());
}

#[test]
fn replay_of_aborted_and_open_transactions_does_not_republish() {
    let mut decoder = Decoder::resume(5, 10, 1, 2).unwrap();
    // Already confirmed commit at lsn 10 is replayed and dropped silently.
    decoder
        .feed(schema(1, 1, 1, 9, 1, vec![(1, "k", "int")]))
        .unwrap();
    decoder
        .feed(row(
            2,
            1,
            1,
            9,
            1,
            RowOp::Insert,
            vec![(1, Scalar::Int(1))],
        ))
        .unwrap();
    decoder.feed(commit(10, 1, 1)).unwrap();
    // Aborted txn never reaches the output.
    decoder
        .feed(row(
            11,
            2,
            1,
            9,
            1,
            RowOp::Insert,
            vec![(1, Scalar::Int(2))],
        ))
        .unwrap();
    decoder.feed(common::abort(12, 2)).unwrap();
    // New commit past the confirmed point becomes batch 2.
    decoder
        .feed(row(
            13,
            3,
            1,
            9,
            1,
            RowOp::Insert,
            vec![(1, Scalar::Int(3))],
        ))
        .unwrap();
    decoder.feed(commit(14, 3, 1)).unwrap();

    let batch = decoder.next_batch().unwrap();
    assert_eq!(batch.number, 2);
    assert_eq!(batch.transactions.len(), 1);
    assert_eq!(batch.transactions[0].txn_id, 3);
}
