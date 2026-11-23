//! Error types produced while feeding log records or acknowledging batches.

use crate::model::{FragmentNo, Lsn, TxnId};

/// Errors caused by malformed or contradictory input records.
#[derive(Debug, Clone, PartialEq, Eq)]
#[allow(dead_code)] // DuplicateFragment documents a rejected strict policy
pub enum FeedError {
    /// Fragment numbers start at 1; zero is illegal.
    InvalidFragmentNumber {
        txn_id: TxnId,
        fragment: FragmentNo,
    },
    /// Reserved for decoders configured with strict per-fragment delivery;
    /// the default policy treats repeated records on the same fragment as
    /// appends, so this variant is not emitted by [`crate::Decoder`].
    DuplicateFragment {
        txn_id: TxnId,
        fragment: FragmentNo,
    },
    /// A fragment arrived after the transaction had already committed or
    /// aborted.
    FragmentAfterFinalize {
        txn_id: TxnId,
        fragment: FragmentNo,
    },
    /// A schema declared twice inside one transaction for the same table
    /// and version with conflicting contents.
    ConflictingSchemaDeclaration {
        txn_id: TxnId,
        table_id: u64,
        schema_version: u32,
    },
    /// A schema declaration lists the same column id more than once.
    DuplicateColumnInSchema {
        txn_id: TxnId,
        table_id: u64,
        schema_version: u32,
        column_id: u64,
    },
    /// Two different transactions committed at the same log position.
    DuplicateCommitLsn {
        lsn: Lsn,
        txn_id: TxnId,
    },
    /// A commit/abort marker was fed for a transaction already finalized.
    AlreadyFinalized { txn_id: TxnId },
    /// A schema declaration inside a fragment conflicts with a schema of the
    /// same table/version already committed globally.
    ConflictingCommittedSchema {
        txn_id: TxnId,
        table_id: u64,
        schema_version: u32,
    },
}

impl std::fmt::Display for FeedError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            FeedError::InvalidFragmentNumber { txn_id, fragment } => write!(
                f,
                "transaction {txn_id} uses illegal fragment number {fragment} (fragments start at 1)"
            ),
            FeedError::DuplicateFragment { txn_id, fragment } => {
                write!(f, "duplicate fragment {fragment} for transaction {txn_id}")
            }
            FeedError::FragmentAfterFinalize { txn_id, fragment } => write!(
                f,
                "fragment {fragment} arrived after transaction {txn_id} was finalized"
            ),
            FeedError::ConflictingSchemaDeclaration {
                txn_id,
                table_id,
                schema_version,
            } => write!(
                f,
                "transaction {txn_id} declares conflicting schemas for table {table_id} version {schema_version}"
            ),
            FeedError::DuplicateColumnInSchema {
                txn_id,
                table_id,
                schema_version,
                column_id,
            } => write!(
                f,
                "schema for table {table_id} version {schema_version} in transaction {txn_id} lists column id {column_id} twice"
            ),
            FeedError::DuplicateCommitLsn { lsn, txn_id } => {
                write!(f, "transaction {txn_id} commits at lsn {lsn} already used by another transaction")
            }
            FeedError::AlreadyFinalized { txn_id } => {
                write!(f, "transaction {txn_id} is already committed or aborted")
            }
            FeedError::ConflictingCommittedSchema {
                txn_id,
                table_id,
                schema_version,
            } => write!(
                f,
                "transaction {txn_id} redeclares already committed schema table {table_id} version {schema_version} with different contents"
            ),
        }
    }
}

impl std::error::Error for FeedError {}

/// Errors from acknowledgement and decoder construction.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum DecoderError {
    /// The acknowledged batch number is below the highest already
    /// acknowledged batch number (acknowledgement is cumulative and may not
    /// move backwards).
    AckOutOfOrder { requested: u64, highest: u64 },
    /// No batch with that number exists on this decoder.
    UnknownBatch { number: u64 },
    /// `batch_size` must be at least 1.
    InvalidBatchSize { size: usize },
    /// A decoder resumed with a next batch number that is not greater than
    /// the number of already acknowledged batches.
    InconsistentResume {
        acknowledged: u64,
        next_batch: u64,
    },
}

impl std::fmt::Display for DecoderError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            DecoderError::AckOutOfOrder { requested, highest } => write!(
                f,
                "cannot acknowledge batch {requested}: batch {highest} or higher is already acknowledged"
            ),
            DecoderError::UnknownBatch { number } => {
                write!(f, "batch {number} does not exist")
            }
            DecoderError::InvalidBatchSize { size } => {
                write!(f, "batch size {size} is invalid; it must be at least 1")
            }
            DecoderError::InconsistentResume {
                acknowledged,
                next_batch,
            } => write!(
                f,
                "resume state inconsistent: next batch {next_batch} must be greater than acknowledged batch {acknowledged}"
            ),
        }
    }
}

impl std::error::Error for DecoderError {}
