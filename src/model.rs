//! Domain types for the logical replication decoder.

use std::collections::BTreeMap;

/// Log sequence number / log position. Monotonically increases in the log.
pub type Lsn = u64;
/// Transaction identifier. Unique for every transaction observed by a decoder.
pub type TxnId = u64;
/// Identity of a table.
pub type TableId = u64;
/// Schema version number used by a table.
pub type SchemaVersion = u32;
/// Fragment number inside a transaction, numbered from 1.
pub type FragmentNo = u32;
/// Stable identity of a column. Survives renames; recreated same-named
/// columns get a fresh identity.
pub type ColumnId = u64;

/// Scalar column value carried by a row change.
#[derive(Debug, Clone, PartialEq)]
pub enum Scalar {
    Null,
    Bool(bool),
    Int(i64),
    Real(f64),
    Text(String),
    Bytes(Vec<u8>),
}

/// Kind of a row change.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RowOp {
    Insert,
    Update,
    Delete,
}

/// Declaration of one column inside a table schema version.
///
/// The [`ColumnId`] is the stable identity; the name is only display metadata
/// and may change between schema versions without the identity changing.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ColumnSpec {
    pub id: ColumnId,
    pub name: String,
    pub data_type: String,
}

/// One version of a table's schema.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TableSchema {
    pub table_id: TableId,
    pub version: SchemaVersion,
    /// Columns in schema declaration order; identities are unique.
    pub columns: Vec<ColumnSpec>,
}

impl TableSchema {
    /// Look up a column by its stable identity.
    pub fn column(&self, id: ColumnId) -> Option<&ColumnSpec> {
        self.columns.iter().find(|c| c.id == id)
    }
}

/// One raw row change as it appears in the log.
///
/// Values are keyed by stable [`ColumnId`], referencing the schema version
/// that was current when the row change was written.
#[derive(Debug, Clone, PartialEq)]
pub struct RawChange {
    pub table_id: TableId,
    pub schema_version: SchemaVersion,
    pub op: RowOp,
    pub values: BTreeMap<ColumnId, Scalar>,
}

/// A single record read from the replication log.
#[derive(Debug, Clone, PartialEq)]
pub enum LogRecord {
    /// A table schema declaration, carried inside a transaction fragment.
    Schema {
        lsn: Lsn,
        txn_id: TxnId,
        fragment: FragmentNo,
        schema: TableSchema,
    },
    /// A row change carried inside a transaction fragment.
    RowChange {
        lsn: Lsn,
        txn_id: TxnId,
        fragment: FragmentNo,
        change: RawChange,
    },
    /// Transaction commit. `last_fragment` is the number of the last
    /// fragment the transaction wrote (a complete transaction owns every
    /// fragment from 1 through `last_fragment`).
    Commit {
        lsn: Lsn,
        txn_id: TxnId,
        last_fragment: FragmentNo,
    },
    /// Transaction abort/rollback. All of its fragments must be discarded.
    Abort { lsn: Lsn, txn_id: TxnId },
}

impl LogRecord {
    /// Log position of the record.
    pub fn lsn(&self) -> Lsn {
        match self {
            LogRecord::Schema { lsn, .. }
            | LogRecord::RowChange { lsn, .. }
            | LogRecord::Commit { lsn, .. }
            | LogRecord::Abort { lsn, .. } => *lsn,
        }
    }

    /// Owning transaction of the record.
    pub fn txn_id(&self) -> TxnId {
        match self {
            LogRecord::Schema { txn_id, .. }
            | LogRecord::RowChange { txn_id, .. }
            | LogRecord::Commit { txn_id, .. }
            | LogRecord::Abort { txn_id, .. } => *txn_id,
        }
    }

    /// Fragment number, or `None` for commit/abort markers.
    pub fn fragment(&self) -> Option<FragmentNo> {
        match self {
            LogRecord::Schema { fragment, .. } | LogRecord::RowChange { fragment, .. } => {
                Some(*fragment)
            }
            LogRecord::Commit { .. } | LogRecord::Abort { .. } => None,
        }
    }
}

/// A row change decoded against the schema version it referenced.
#[derive(Debug, Clone, PartialEq)]
pub struct DecodedChange {
    /// Log position of the underlying row change record.
    pub lsn: Lsn,
    pub op: RowOp,
    pub table_id: TableId,
    /// The schema version used to decode the change (as referenced on write).
    pub schema_version: SchemaVersion,
    /// One entry per declared column, in schema declaration order.
    /// `None` means the row change did not carry that column.
    pub columns: Vec<ResolvedValue>,
    /// Values carried by the log whose column id does not exist in the
    /// referenced schema version (kept so no data is silently dropped).
    pub unresolved: BTreeMap<ColumnId, Scalar>,
}

/// A column value resolved against a concrete column declaration.
#[derive(Debug, Clone, PartialEq)]
pub struct ResolvedValue {
    pub column_id: ColumnId,
    pub name: String,
    pub data_type: String,
    pub value: Option<Scalar>,
}

/// A fully decoded, committed transaction.
#[derive(Debug, Clone, PartialEq)]
pub struct CommittedTransaction {
    pub txn_id: TxnId,
    /// Position of the commit record; defines publish order.
    pub commit_lsn: Lsn,
    /// Schema versions this transaction depends on, sorted and deduplicated.
    pub schema_versions: Vec<(TableId, SchemaVersion)>,
    /// Row changes in their original log order across fragments.
    pub changes: Vec<DecodedChange>,
}

/// Why a committed transaction cannot be decoded. Both kinds are permanent
/// for the current decoder session and act as ordering holes: nothing at a
/// higher commit position may be published past them.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum BlockReason {
    /// The transaction committed but fragments are missing (the decoder
    /// started reading inside the transaction or log data was lost).
    Incomplete {
        txn_id: TxnId,
        commit_lsn: Lsn,
        last_fragment: FragmentNo,
        missing: Vec<FragmentNo>,
    },
    /// A row change references a table schema version that is not known.
    /// This can be healed by re-opening the decoder from an earlier log
    /// position so the missing schema declaration is re-read.
    MissingSchema {
        txn_id: TxnId,
        commit_lsn: Lsn,
        table_id: TableId,
        schema_version: SchemaVersion,
        /// Position of the first row change that referenced the gap.
        referenced_at: Lsn,
    },
}

/// A batch of published transactions. Batches are handed out with strictly
/// increasing numbers; an unacknowledged batch keeps that number and content
/// until it is acknowledged or the decoder is dropped.
#[derive(Debug, Clone, PartialEq)]
pub struct Batch {
    pub number: u64,
    pub transactions: Vec<CommittedTransaction>,
}
