//! Shared builders for integration tests.
#![allow(dead_code)]

use logical_decoder::{ColumnSpec, LogRecord, RawChange, RowOp, Scalar, TableSchema};
use std::collections::BTreeMap;

pub type Lsn = u64;
pub type TxnId = u64;
pub type FragmentNo = u32;

pub fn schema(
    lsn: Lsn,
    txn_id: TxnId,
    fragment: FragmentNo,
    table_id: u64,
    version: u32,
    columns: Vec<(u64, &str, &str)>,
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
                    name: name.into(),
                    data_type: ty.into(),
                })
                .collect(),
        },
    }
}

pub fn row(
    lsn: Lsn,
    txn_id: TxnId,
    fragment: FragmentNo,
    table_id: u64,
    version: u32,
    op: RowOp,
    values: Vec<(u64, Scalar)>,
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

pub fn commit(lsn: Lsn, txn_id: TxnId, last_fragment: FragmentNo) -> LogRecord {
    LogRecord::Commit {
        lsn,
        txn_id,
        last_fragment,
    }
}

pub fn abort(lsn: Lsn, txn_id: TxnId) -> LogRecord {
    LogRecord::Abort { lsn, txn_id }
}
