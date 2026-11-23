//! # Logical replication decoder
//!
//! This crate turns a stream of position-tagged logical replication log
//! records into ordered batches of fully decoded, committed transactions.
//!
//! ## Input model
//!
//! Every [`LogRecord`] carries a log position (`lsn`) and belongs to a
//! transaction. Transaction payloads are split into fragments numbered from
//! 1; fragments of different transactions may be interleaved arbitrarily in
//! the stream. Records are one of:
//!
//! * [`LogRecord::Schema`] — a versioned table schema declaration carried in
//!   a fragment. Columns have stable [`ColumnId`](model::ColumnId) identities
//!   that survive renames; dropping a column and later creating a
//!   same-named one yields a new identity.
//! * [`LogRecord::RowChange`] — a row change referencing the schema version
//!   that was current when it was written, with values keyed by column id.
//! * [`LogRecord::Commit`] — finalizes a transaction and carries its last
//!   fragment number; a complete transaction owns every fragment
//!   `1..=last_fragment`.
//! * [`LogRecord::Abort`] — discards every fragment of the transaction.
//!
//! ## Decoding and ordering rules
//!
//! * Only complete, committed transactions are published. Aborted
//!   transactions are dropped, including their pending schema declarations.
//! * Row changes are decoded against the exact schema version they
//!   reference. Schema versions are retained for the decoder's whole life,
//!   so an old-format transaction that stays open while a newer schema is
//!   committed still decodes correctly afterwards.
//! * Transactions are published in commit-position order; row changes keep
//!   their original log order inside each transaction.
//! * A commit that cannot be decoded becomes a *hole* (see [`BlockReason`]):
//!   either missing fragments or a missing referenced schema version. The
//!   hole reports the missing schema/version and the positions involved, and
//!   no transaction committing after it may be published until the hole is
//!   resolved (typically by re-opening the decoder earlier in the log).
//!
//! ## Batches, acknowledgement and restart
//!
//! Transactions are handed out as numbered [`Batch`]es of a fixed size.
//! Downstream acknowledges whole batches cumulatively via
//! [`Decoder::acknowledge`]; unacknowledged batches keep their original
//! number and content. On restart, build the decoder with
//! [`Decoder::resume`] from a position at or before the confirmed position
//! and replay the log: already confirmed commits are consumed silently, and
//! re-derived batches continue with the same numbering.
//!
//! The implementation depends only on the Rust standard library.

pub mod decoder;
pub mod error;
pub mod model;
pub mod registry;

pub use decoder::Decoder;
pub use error::{DecoderError, FeedError};
pub use model::{
    Batch, BlockReason, ColumnId, ColumnSpec, CommittedTransaction, DecodedChange, FragmentNo,
    LogRecord, Lsn, RawChange, ResolvedValue, RowOp, Scalar, SchemaVersion, TableId, TableSchema,
    TxnId,
};

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::BTreeMap;

    fn schema_decl(
        lsn: Lsn,
        txn_id: TxnId,
        fragment: FragmentNo,
        table_id: TableId,
        version: SchemaVersion,
        columns: Vec<(ColumnId, &str, &str)>,
    ) -> LogRecord {
        LogRecord::Schema {
            lsn,
            txn_id,
            fragment,
            schema: TableSchema {
                table_id,
                version,
                columns: columns
                    .into_iter()
                    .map(|(id, name, ty)| ColumnSpec {
                        id,
                        name: name.to_string(),
                        data_type: ty.to_string(),
                    })
                    .collect(),
            },
        }
    }

    fn row(
        lsn: Lsn,
        txn_id: TxnId,
        fragment: FragmentNo,
        table_id: TableId,
        version: SchemaVersion,
        op: RowOp,
        values: Vec<(ColumnId, Scalar)>,
    ) -> LogRecord {
        LogRecord::RowChange {
            lsn,
            txn_id,
            fragment,
            change: RawChange {
                table_id,
                schema_version: version,
                op,
                values: BTreeMap::from_iter(values),
            },
        }
    }

    #[test]
    fn schema_registry_keeps_old_versions_for_late_readers() {
        let mut registry = registry::SchemaRegistry::new();
        let v1 = TableSchema {
            table_id: 7,
            version: 1,
            columns: vec![ColumnSpec {
                id: 1,
                name: "name".into(),
                data_type: "text".into(),
            }],
        };
        let v2 = TableSchema {
            table_id: 7,
            version: 2,
            columns: vec![
                ColumnSpec {
                    id: 1,
                    name: "label".into(),
                    data_type: "text".into(),
                },
                ColumnSpec {
                    id: 2,
                    name: "score".into(),
                    data_type: "int".into(),
                },
            ],
        };
        registry.publish(v1.clone()).unwrap();
        registry.publish(v2.clone()).unwrap();

        // Identity 1 survives the rename; both versions remain resolvable.
        assert_eq!(registry.get(7, 1).unwrap(), &v1);
        assert_eq!(
            registry.get(7, 2).unwrap().column(1).unwrap().name,
            "label"
        );
        assert!(registry.publish(v1).is_ok()); // idempotent re-declaration
    }

    #[test]
    fn pending_schemas_reject_duplicate_column_ids() {
        let mut pending = registry::PendingSchemas::new();
        let schema = TableSchema {
            table_id: 1,
            version: 1,
            columns: vec![
                ColumnSpec {
                    id: 5,
                    name: "a".into(),
                    data_type: "int".into(),
                },
                ColumnSpec {
                    id: 5,
                    name: "b".into(),
                    data_type: "int".into(),
                },
            ],
        };
        assert_eq!(
            pending.add(42, schema).unwrap_err(),
            FeedError::DuplicateColumnInSchema {
                txn_id: 42,
                table_id: 1,
                schema_version: 1,
                column_id: 5,
            }
        );
    }

    #[test]
    fn decode_resolves_by_stable_identity_and_preserves_order() {
        let mut decoder = Decoder::new(10).unwrap();
        // DDL transaction: version 1 then version 2 (rename + drop/recreate).
        decoder
            .feed(schema_decl(
                1,
                100,
                1,
                1,
                1,
                vec![(1, "name", "text"), (2, "age", "int")],
            ))
            .unwrap();
        decoder
            .feed(LogRecord::Commit {
                lsn: 2,
                txn_id: 100,
                last_fragment: 1,
            })
            .unwrap();
        decoder
            .feed(schema_decl(
                3,
                101,
                1,
                1,
                2,
                vec![(1, "label", "text"), (3, "age", "int")],
            ))
            .unwrap();
        decoder
            .feed(LogRecord::Commit {
                lsn: 4,
                txn_id: 101,
                last_fragment: 1,
            })
            .unwrap();

        // Old-format transaction references version 1 after v2 is committed.
        decoder
            .feed(row(
                5,
                200,
                1,
                1,
                1,
                RowOp::Insert,
                vec![(1, Scalar::Text("alice".into())), (2, Scalar::Int(30))],
            ))
            .unwrap();
        // New-format transaction references version 2; column 3 is a fresh
        // identity even though it reuses the name "age".
        decoder
            .feed(row(
                6,
                201,
                1,
                1,
                2,
                RowOp::Insert,
                vec![(1, Scalar::Text("bob".into())), (3, Scalar::Int(4))],
            ))
            .unwrap();
        decoder
            .feed(LogRecord::Commit {
                lsn: 7,
                txn_id: 200,
                last_fragment: 1,
            })
            .unwrap();
        decoder
            .feed(LogRecord::Commit {
                lsn: 8,
                txn_id: 201,
                last_fragment: 1,
            })
            .unwrap();

        // All four commits (two DDL, two DML) are published in commit order.
        let first = decoder.next_batch().unwrap().transactions;
        assert_eq!(first.len(), 4);
        assert_eq!(
            first.iter().map(|t| t.txn_id).collect::<Vec<_>>(),
            vec![100, 101, 200, 201]
        );
        let old = &first[2].changes[0];
        assert_eq!(old.schema_version, 1);
        assert_eq!(old.columns[0].name, "name");
        assert_eq!(
            old.columns[1],
            ResolvedValue {
                column_id: 2,
                name: "age".into(),
                data_type: "int".into(),
                value: Some(Scalar::Int(30)),
            }
        );
        let new = &first[3].changes[0];
        assert_eq!(new.schema_version, 2);
        assert_eq!(new.columns[0].name, "label");
        assert_eq!(new.columns[1].column_id, 3);
        assert!(new.columns[1].value.is_some());
    }
}
