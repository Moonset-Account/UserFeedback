//! Interleaving scenarios: schema evolution while old-format transactions
//! stay open, DDL/DML inside one transaction, three-way fragment interleaving.
mod common;

use common::{commit, row, schema};
use logical_decoder::{Decoder, RowOp, Scalar};

#[test]
fn old_format_txn_decodes_with_its_referenced_version_after_schema_update() {
    let mut decoder = Decoder::new(10).unwrap();
    // v1 schema committed.
    decoder
        .feed(schema(
            1,
            10,
            1,
            5,
            1,
            vec![(1, "name", "text"), (2, "age", "int")],
        ))
        .unwrap();
    decoder.feed(commit(2, 10, 1)).unwrap();

    // Old txn opens and writes against v1 while still uncommitted.
    decoder
        .feed(row(
            3,
            20,
            1,
            5,
            1,
            RowOp::Insert,
            vec![(1, Scalar::Text("ada".into())), (2, Scalar::Int(36))],
        ))
        .unwrap();

    // Newer DDL txn commits v2: rename id 1, drop id 2, create same-named
    // "age" with a fresh id 3.
    decoder
        .feed(schema(
            4,
            30,
            1,
            5,
            2,
            vec![(1, "label", "text"), (3, "age", "int")],
        ))
        .unwrap();
    // New txn using v2 interleaves before the old txn commits.
    decoder
        .feed(row(
            5,
            40,
            1,
            5,
            2,
            RowOp::Insert,
            vec![(1, Scalar::Text("grace".into())), (3, Scalar::Int(29))],
        ))
        .unwrap();
    decoder.feed(commit(6, 30, 1)).unwrap();
    decoder.feed(commit(7, 40, 1)).unwrap();

    // Old txn finally commits; it must still decode against v1.
    decoder
        .feed(row(
            8,
            20,
            2,
            5,
            1,
            RowOp::Update,
            vec![(1, Scalar::Text("ada2".into())), (2, Scalar::Int(37))],
        ))
        .unwrap();
    decoder.feed(commit(9, 20, 2)).unwrap();

    let batch = decoder.next_batch().unwrap();
    // Commit order: 10 (v1 ddl), 30 (v2 ddl), 40 (v2), 20 (v1 late).
    let txn_ids: Vec<_> = batch.transactions.iter().map(|t| t.txn_id).collect();
    assert_eq!(txn_ids, vec![10, 30, 40, 20]);

    let old = batch
        .transactions
        .iter()
        .find(|t| t.txn_id == 20)
        .unwrap();
    assert_eq!(old.schema_versions, vec![(5, 1)]);
    assert_eq!(old.changes.len(), 2);
    assert_eq!(old.changes[0].schema_version, 1);
    assert_eq!(old.changes[0].columns[1].name, "age");
    assert_eq!(old.changes[0].columns[1].column_id, 2);
    assert_eq!(old.changes[0].columns[1].value, Some(Scalar::Int(36)));
    assert_eq!(old.changes[1].columns[1].value, Some(Scalar::Int(37)));

    let new = batch
        .transactions
        .iter()
        .find(|t| t.txn_id == 40)
        .unwrap();
    assert_eq!(new.changes[0].schema_version, 2);
    assert_eq!(new.changes[0].columns[1].column_id, 3);
    assert_eq!(new.changes[0].columns[1].name, "age");
}

#[test]
fn ddl_and_dml_interleaved_inside_one_transaction_resolve_on_commit() {
    let mut decoder = Decoder::new(10).unwrap();
    // Row change in fragment 2 references the schema declared in fragment 1.
    decoder
        .feed(schema(1, 7, 1, 4, 1, vec![(1, "id", "int")]))
        .unwrap();
    decoder
        .feed(row(
            2,
            7,
            2,
            4,
            1,
            RowOp::Insert,
            vec![(1, Scalar::Int(77))],
        ))
        .unwrap();
    decoder.feed(commit(3, 7, 2)).unwrap();

    let batch = decoder.next_batch().unwrap();
    assert_eq!(
        batch.transactions[0].changes[0].columns[0].value,
        Some(Scalar::Int(77))
    );
    assert_eq!(decoder.retained_schema_count(), 1);
}

#[test]
fn three_transactions_interleave_across_fragments_and_reassemble() {
    let mut decoder = Decoder::new(10).unwrap();
    // A dedicated DDL transaction commits the shared schema before any DML.
    decoder
        .feed(schema(1, 1, 1, 9, 1, vec![(1, "k", "int")]))
        .unwrap();
    decoder.feed(commit(2, 1, 1)).unwrap();
    // Round-robin fragments across txns 10, 20, 30, each with 3 fragments.
    for fragment in 1u32..=3 {
        for txn_id in [10u64, 20, 30] {
            let lsn = 10 + u64::from(fragment) * 10 + txn_id;
            decoder
                .feed(row(
                    lsn,
                    txn_id,
                    fragment,
                    9,
                    1,
                    RowOp::Insert,
                    vec![(1, Scalar::Int(txn_id as i64 * 10 + i64::from(fragment)))],
                ))
                .unwrap();
        }
    }
    // Commits arrive in an order different from transaction start order.
    decoder.feed(commit(100, 30, 3)).unwrap();
    decoder.feed(commit(101, 10, 3)).unwrap();
    decoder.feed(commit(102, 20, 3)).unwrap();

    let batch = decoder.next_batch().unwrap();
    assert_eq!(
        batch.transactions.iter().map(|t| t.txn_id).collect::<Vec<_>>(),
        vec![1, 30, 10, 20]
    );
    for txn in batch
        .transactions
        .iter()
        .filter(|t| t.txn_id != 1)
    {
        assert_eq!(txn.changes.len(), 3);
        // Original log order preserved inside the transaction.
        let lsns: Vec<_> = txn.changes.iter().map(|c| c.lsn).collect();
        assert_eq!(lsns, lsns.iter().copied().collect::<Vec<_>>());
    }
}
