//! Fine-grained interleaving stress: records of many transactions alternate
//! on every single feed call, including schema evolution mid-stream.
mod common;

use common::{commit, row, schema};
use logical_decoder::{Decoder, RowOp, Scalar};

#[test]
fn fine_grained_alternation_across_many_transactions_reassembles_correctly() {
    let mut decoder = Decoder::new(4).unwrap();

    // Baseline schema v1 committed first.
    decoder
        .feed(schema(1, 100, 1, 7, 1, vec![(1, "id", "int"), (2, "name", "text")]))
        .unwrap();
    decoder.feed(commit(2, 100, 1)).unwrap();

    // Five DML transactions, each with 4 fragments, interleaved round-robin.
    let txns = [10u64, 20, 30, 40, 50];
    let mut lsn = 10;
    for fragment in 1u32..=4 {
        for txn_id in txns {
            lsn += 1;
            decoder
                .feed(row(
                    lsn,
                    txn_id,
                    fragment,
                    7,
                    1,
                    if fragment == 1 { RowOp::Insert } else { RowOp::Update },
                    vec![
                        (1, Scalar::Int(txn_id as i64)),
                        (2, Scalar::Text(format!("{txn_id}-{fragment}"))),
                    ],
                ))
                .unwrap();
        }
    }

    // A v2 schema is committed while all five transactions stay open.
    lsn += 1;
    decoder
        .feed(schema(
            lsn,
            200,
            1,
            7,
            2,
            vec![(1, "id", "int"), (2, "label", "text"), (3, "tag", "text")],
        ))
        .unwrap();
    lsn += 1;
    decoder.feed(commit(lsn, 200, 1)).unwrap();

    // Commits arrive in reverse start order; all five still reference v1.
    let mut commit_positions = Vec::new();
    for txn_id in txns.iter().rev() {
        lsn += 1;
        commit_positions.push((lsn, *txn_id));
        decoder.feed(commit(lsn, *txn_id, 4)).unwrap();
    }

    // Batch size 4: seven ready transactions (two DDL plus five DML) produce
    // a full batch of 4 and a trailing partial batch of 3 after flush.
    let first = decoder.next_batch().unwrap();
    assert_eq!(first.number, 1);
    assert_eq!(first.transactions.len(), 4);
    decoder.flush();
    let pending = decoder.pending_batches();
    assert_eq!(pending.len(), 2);
    assert_eq!(pending[1].number, 2);
    assert_eq!(pending[1].transactions.len(), 3);

    // Every DML transaction kept 4 changes in original log order and still
    // decodes against v1 despite v2 being committed in the meantime.
    let all: Vec<_> = pending
        .iter()
        .flat_map(|b| b.transactions.iter())
        .chain(first.transactions.iter())
        .collect();
    for txn_id in txns {
        let txn = all.iter().find(|t| t.txn_id == txn_id).unwrap();
        assert_eq!(txn.schema_versions, vec![(7, 1)]);
        assert_eq!(txn.changes.len(), 4);
        let lsns: Vec<_> = txn.changes.iter().map(|c| c.lsn).collect();
        assert_eq!(lsns, lsns.iter().copied().collect::<Vec<_>>());
        for change in &txn.changes {
            assert_eq!(change.columns.len(), 2); // v1 shape, not v2's 3 columns
            assert_eq!(change.columns[1].name, "name");
        }
    }

    // Commit-lsn order is strictly respected.
    let commit_lsns: Vec<u64> = all.iter().map(|t| t.commit_lsn).collect();
    assert_eq!(commit_lsns, commit_lsns.iter().copied().collect::<Vec<_>>());
}
