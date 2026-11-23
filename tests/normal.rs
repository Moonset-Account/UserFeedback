//! Normal-path scenarios: interleaving, ordering, aborts, batching.
mod common;

use common::{abort, commit, row, schema};
use logical_decoder::{Decoder, RowOp, Scalar};

#[test]
fn interleaved_fragments_assemble_in_original_change_order() {
    let mut decoder = Decoder::new(10).unwrap();
    let records = vec![
        // Shared schema committed by a dedicated DDL transaction.
        schema(1, 99, 1, 9, 1, vec![(1, "k", "int"), (2, "v", "text")]),
        commit(2, 99, 1),
        row(3, 1, 1, 9, 1, RowOp::Insert, vec![(1, Scalar::Int(1)), (2, Scalar::Text("a".into()))]),
        row(4, 2, 1, 9, 1, RowOp::Insert, vec![(1, Scalar::Int(2)), (2, Scalar::Text("b".into()))]),
        // Second fragment of txn 1 interleaved after txn 2's fragment.
        row(5, 1, 2, 9, 1, RowOp::Update, vec![(1, Scalar::Int(1)), (2, Scalar::Text("a2".into()))]),
        row(6, 2, 2, 9, 1, RowOp::Delete, vec![(1, Scalar::Int(2))]),
        // Txn 2 commits first even though txn 1 started first.
        commit(7, 2, 2),
        commit(8, 1, 2),
    ];
    decoder.feed_all(records).unwrap();

    let batch = decoder.next_batch().unwrap();
    assert_eq!(batch.number, 1);
    assert_eq!(batch.transactions.len(), 3);
    // Commit-lsn ordering: ddl 99, then txn 2 (lsn 7) before txn 1 (lsn 8).
    assert_eq!(batch.transactions[0].txn_id, 99);
    assert_eq!(batch.transactions[1].txn_id, 2);
    assert_eq!(batch.transactions[2].txn_id, 1);

    let txn1 = &batch.transactions[2];
    assert_eq!(txn1.commit_lsn, 8);
    assert_eq!(txn1.changes.len(), 2);
    assert_eq!(txn1.changes[0].lsn, 3);
    assert_eq!(txn1.changes[0].op, RowOp::Insert);
    assert_eq!(txn1.changes[1].lsn, 5);
    assert_eq!(txn1.changes[1].op, RowOp::Update);
    assert_eq!(
        txn1.changes[1].columns[1].value,
        Some(Scalar::Text("a2".into()))
    );
}

#[test]
fn aborted_transaction_and_its_schemas_are_discarded() {
    let mut decoder = Decoder::new(10).unwrap();
    let records = vec![
        schema(1, 1, 1, 9, 1, vec![(1, "k", "int")]),
        row(2, 1, 2, 9, 1, RowOp::Insert, vec![(1, Scalar::Int(1))]),
        abort(3, 1),
        // Txn 2 uses the same table/version number: the aborted declaration
        // must not have leaked into the committed registry.
        schema(4, 2, 1, 9, 1, vec![(1, "k", "int"), (2, "note", "text")]),
        row(5, 2, 2, 9, 1, RowOp::Insert, vec![(1, Scalar::Int(9)), (2, Scalar::Text("ok".into()))]),
        commit(6, 2, 2),
    ];
    decoder.feed_all(records).unwrap();

    assert_eq!(decoder.open_transaction_count(), 0);
    let batch = decoder.next_batch().unwrap();
    assert_eq!(batch.transactions.len(), 1);
    assert_eq!(batch.transactions[0].txn_id, 2);
    assert_eq!(batch.transactions[0].changes[0].columns.len(), 2);
}

#[test]
fn batches_seal_at_configured_size_and_partial_batch_on_flush() {
    let mut decoder = Decoder::new(2).unwrap();
    decoder
        .feed(schema(1, 1, 1, 9, 1, vec![(1, "k", "int")]))
        .unwrap();
    decoder.feed(commit(2, 1, 1)).unwrap();
    // Four more DML txns: together with the DDL txn that makes five ready
    // transactions, i.e. two full batches (size 2) plus one partial batch.
    for (i, txn_id) in [2u64, 3, 4, 5].iter().enumerate() {
        let base = 3 + i as u64 * 2;
        decoder
            .feed(row(
                base,
                *txn_id,
                1,
                9,
                1,
                RowOp::Insert,
                vec![(1, Scalar::Int(*txn_id as i64))],
            ))
            .unwrap();
        decoder.feed(commit(base + 1, *txn_id, 1)).unwrap();
    }

    // pending_batches (like next_batch) seals the trailing partial batch so
    // every ready transaction is observable; batches keep their numbers.
    let sealed = decoder.pending_batches();
    assert_eq!(sealed.len(), 3);
    assert_eq!(sealed[0].number, 1);
    assert_eq!(sealed[0].transactions.len(), 2);
    assert_eq!(sealed[1].number, 2);
    assert_eq!(sealed[1].transactions.len(), 2);
    assert_eq!(sealed[2].number, 3);
    assert_eq!(sealed[2].transactions.len(), 1);
    assert_eq!(sealed[2].transactions[0].txn_id, 5);

    // An explicit flush changes nothing once everything is sealed.
    decoder.flush();
    assert_eq!(decoder.pending_batches().len(), 3);

    // Redelivery keeps the original number and content.
    let redelivered = decoder.next_batch().unwrap();
    assert_eq!(redelivered, sealed[0]);
}
