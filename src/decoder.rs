//! The streaming logical replication decoder.
//!
//! See the crate-level documentation and `README.md` for the full contract.
//! The decoder is intentionally single-threaded; concurrency is exercised by
//! feeding interleaved fragments from different transactions, which is the
//! actual interleaving boundary of a replication stream.

use std::collections::{BTreeMap, BTreeSet};

use crate::error::{DecoderError, FeedError};
use crate::model::{
    Batch, BlockReason, CommittedTransaction, DecodedChange, FragmentNo, LogRecord, Lsn,
    RawChange, ResolvedValue, TableSchema, TxnId,
};
use crate::registry::{PendingSchemas, SchemaRegistry};

/// One row change with the log position it arrived at. The containing
/// `Vec` preserves original log order, which is the required output order.
#[derive(Debug, Clone)]
struct OrderedChange {
    change: RawChange,
    lsn: Lsn,
}

/// An open (not yet finalized) transaction assembled from interleaved
/// fragments.
#[derive(Debug, Default)]
struct InProgress {
    fragments: BTreeSet<FragmentNo>,
    pending_schemas: PendingSchemas,
    changes: Vec<OrderedChange>,
}

/// A commit that is waiting to be published in commit-lsn order.
#[derive(Debug)]
enum Ready {
    Decoded(CommittedTransaction),
    Blocked(BlockReason),
}

/// Decoder for a stream of [`LogRecord`]s.
///
/// Construction:
/// * [`Decoder::new`] starts reading from the beginning of the retained log.
/// * [`Decoder::resume`] rebuilds decoder state after a restart, replaying log
///   records from a position at or before the last acknowledged position.
#[derive(Debug)]
pub struct Decoder {
    in_progress: BTreeMap<TxnId, InProgress>,
    /// Committed transactions keyed by commit position (publish order).
    ready: BTreeMap<Lsn, Ready>,
    /// Finalized (committed or aborted) transactions; late fragments are
    /// rejected against this set.
    finalized: BTreeSet<TxnId>,
    registry: SchemaRegistry,
    /// Positions of every commit already seen, used to reject duplicate
    /// commit positions.
    commit_lsns: BTreeSet<Lsn>,
    /// Batch size in transactions.
    batch_size: usize,
    /// Batch currently being filled, sealed on flush or once full.
    open_batch: Vec<CommittedTransaction>,
    next_batch_number: u64,
    /// Sealed but not yet acknowledged batches, keyed by batch number.
    sealed: BTreeMap<u64, Batch>,
    /// Highest acknowledged batch number.
    acknowledged: u64,
    /// Highest acknowledged log position; transactions committing at or
    /// before it on replay are consumed silently.
    confirmed_lsn: Lsn,
}

impl Decoder {
    /// Create a decoder that batches up to `batch_size` transactions.
    pub fn new(batch_size: usize) -> Result<Self, DecoderError> {
        if batch_size == 0 {
            return Err(DecoderError::InvalidBatchSize { size: batch_size });
        }
        Ok(Self {
            in_progress: BTreeMap::new(),
            ready: BTreeMap::new(),
            finalized: BTreeSet::new(),
            registry: SchemaRegistry::new(),
            commit_lsns: BTreeSet::new(),
            batch_size,
            open_batch: Vec::new(),
            next_batch_number: 1,
            sealed: BTreeMap::new(),
            acknowledged: 0,
            confirmed_lsn: 0,
        })
    }

    /// Re-create a decoder after a restart.
    ///
    /// * `confirmed_lsn` — position of the last acknowledged transaction;
    ///   replayed commits at or below it are consumed without being
    ///   re-published.
    /// * `acknowledged_batch` — highest batch number already acknowledged.
    /// * `next_batch` — number to assign to the first new batch; must be
    ///   greater than `acknowledged_batch` so unacknowledged batch numbering
    ///   is preserved deterministically.
    pub fn resume(
        batch_size: usize,
        confirmed_lsn: Lsn,
        acknowledged_batch: u64,
        next_batch: u64,
    ) -> Result<Self, DecoderError> {
        if batch_size == 0 {
            return Err(DecoderError::InvalidBatchSize { size: batch_size });
        }
        if next_batch <= acknowledged_batch {
            return Err(DecoderError::InconsistentResume {
                acknowledged: acknowledged_batch,
                next_batch,
            });
        }
        Ok(Self {
            in_progress: BTreeMap::new(),
            ready: BTreeMap::new(),
            finalized: BTreeSet::new(),
            registry: SchemaRegistry::new(),
            commit_lsns: BTreeSet::new(),
            batch_size,
            open_batch: Vec::new(),
            next_batch_number: next_batch,
            sealed: BTreeMap::new(),
            acknowledged: acknowledged_batch,
            confirmed_lsn,
        })
    }

    /// Feed one log record.
    pub fn feed(&mut self, record: LogRecord) -> Result<(), FeedError> {
        match record {
            LogRecord::Schema {
                txn_id,
                fragment,
                schema,
                ..
            } => {
                let txn = self.open_fragment(txn_id, fragment)?;
                txn.pending_schemas.add(txn_id, schema)?;
                Ok(())
            }
            LogRecord::RowChange {
                lsn,
                txn_id,
                fragment,
                change,
            } => {
                let txn = self.open_fragment(txn_id, fragment)?;
                txn.changes.push(OrderedChange { change, lsn });
                Ok(())
            }
            LogRecord::Commit {
                lsn,
                txn_id,
                last_fragment,
            } => self.commit(txn_id, lsn, last_fragment),
            LogRecord::Abort { txn_id, .. } => self.abort(txn_id),
        }
    }

    /// Feed many records in order. Stops at the first error.
    pub fn feed_all<I>(&mut self, records: I) -> Result<(), FeedError>
    where
        I: IntoIterator<Item = LogRecord>,
    {
        for record in records {
            self.feed(record)?;
        }
        Ok(())
    }

    /// Get or create the open transaction for a fragment, validating fragment
    /// numbering. A fragment may carry several records (a schema declaration
    /// plus row changes), so repeated records on the same fragment are
    /// appended; the fragment set only tracks which fragment numbers exist
    /// for the completeness check against `last_fragment`.
    fn open_fragment(
        &mut self,
        txn_id: TxnId,
        fragment: FragmentNo,
    ) -> Result<&mut InProgress, FeedError> {
        if fragment == 0 {
            return Err(FeedError::InvalidFragmentNumber { txn_id, fragment });
        }
        if self.finalized.contains(&txn_id) {
            return Err(FeedError::FragmentAfterFinalize { txn_id, fragment });
        }
        let txn = self.in_progress.entry(txn_id).or_default();
        txn.fragments.insert(fragment);
        Ok(txn)
    }

    fn abort(&mut self, txn_id: TxnId) -> Result<(), FeedError> {
        if self.finalized.contains(&txn_id) {
            return Err(FeedError::AlreadyFinalized { txn_id });
        }
        // An abort for a transaction with no retained fragments is a no-op:
        // the decoder may have started reading after it finished, and abort
        // records must remain harmless.
        self.in_progress.remove(&txn_id);
        self.finalized.insert(txn_id);
        Ok(())
    }

    fn commit(
        &mut self,
        txn_id: TxnId,
        commit_lsn: Lsn,
        last_fragment: FragmentNo,
    ) -> Result<(), FeedError> {
        if self.finalized.contains(&txn_id) {
            return Err(FeedError::AlreadyFinalized { txn_id });
        }
        if !self.commit_lsns.insert(commit_lsn) {
            return Err(FeedError::DuplicateCommitLsn {
                lsn: commit_lsn,
                txn_id,
            });
        }

        // Unknown transaction: no fragments retained. It can never be
        // complete in this session, so it becomes an ordering hole rather
        // than silently disappearing.
        let Some(txn) = self.in_progress.remove(&txn_id) else {
            self.finalized.insert(txn_id);
            let missing: Vec<FragmentNo> = (1..=last_fragment).collect();
            self.ready.insert(
                commit_lsn,
                Ready::Blocked(BlockReason::Incomplete {
                    txn_id,
                    commit_lsn,
                    last_fragment,
                    missing,
                }),
            );
            return Ok(());
        };

        // Completeness: every fragment 1..=last_fragment must be present.
        let mut missing = Vec::new();
        for fragment in 1..=last_fragment {
            if !txn.fragments.contains(&fragment) {
                missing.push(fragment);
            }
        }
        if !missing.is_empty() {
            self.finalized.insert(txn_id);
            self.ready.insert(
                commit_lsn,
                Ready::Blocked(BlockReason::Incomplete {
                    txn_id,
                    commit_lsn,
                    last_fragment,
                    missing,
                }),
            );
            return Ok(());
        }

        // Publish the transaction's own schema declarations first so its row
        // changes can resolve DML and DDL interleaved inside it.
        let InProgress {
            pending_schemas,
            changes,
            ..
        } = txn;
        if let Err(mut err) = pending_schemas.publish_all(&mut self.registry) {
            // Restore the owning transaction id on registry-level errors.
            if let FeedError::ConflictingCommittedSchema { txn_id: slot, .. } = &mut err {
                *slot = txn_id;
            }
            return Err(err);
        }

        // Decode every row change against the version it referenced.
        let mut decoded = Vec::with_capacity(changes.len());
        let mut schema_versions: BTreeSet<(u64, u32)> = BTreeSet::new();
        for ordered in changes {
            schema_versions.insert((ordered.change.table_id, ordered.change.schema_version));
            match self.decode_change(txn_id, commit_lsn, &ordered) {
                Ok(change) => decoded.push(change),
                Err(reason) => {
                    self.finalized.insert(txn_id);
                    self.ready.insert(commit_lsn, Ready::Blocked(reason));
                    return Ok(());
                }
            }
        }

        let transaction = CommittedTransaction {
            txn_id,
            commit_lsn,
            schema_versions: schema_versions.into_iter().collect(),
            changes: decoded,
        };
        self.finalized.insert(txn_id);
        self.ready
            .insert(commit_lsn, Ready::Decoded(transaction));
        Ok(())
    }

    /// Decode one raw change. Resolution uses the globally committed
    /// registry, which already contains the committing transaction's own
    /// pending declarations; either way the exact version referenced on
    /// write is used.
    fn decode_change(
        &self,
        txn_id: TxnId,
        commit_lsn: Lsn,
        ordered: &OrderedChange,
    ) -> Result<DecodedChange, BlockReason> {
        let raw = &ordered.change;
        let schema = self
            .registry
            .get(raw.table_id, raw.schema_version)
            .ok_or(BlockReason::MissingSchema {
                txn_id,
                commit_lsn,
                table_id: raw.table_id,
                schema_version: raw.schema_version,
                referenced_at: ordered.lsn,
            })?;
        Ok(decode_against_schema(ordered, schema))
    }

    /// Move everything publishable (in commit-lsn order, stopping at the
    /// first hole) into sealed batches. Already confirmed positions are
    /// consumed silently so replay does not re-publish them.
    fn pump(&mut self) {
        loop {
            // A hole at or below the confirmed position has already been
            // published in an earlier incarnation and is skipped on replay.
            let Some((&lsn, _)) = self.ready.iter().next() else {
                break;
            };
            if lsn <= self.confirmed_lsn {
                self.ready.remove(&lsn);
                continue;
            }
            let Some((_, ready)) = self.ready.first_key_value() else {
                break;
            };
            match ready {
                Ready::Blocked(_) => break,
                Ready::Decoded(_) => {
                    let (_, ready) = self.ready.pop_first().expect("entry just observed");
                    let Ready::Decoded(transaction) = ready else {
                        unreachable!("variant matched above")
                    };
                    self.open_batch.push(transaction);
                    if self.open_batch.len() >= self.batch_size {
                        self.seal_open_batch();
                    }
                }
            }
        }
    }

    fn seal_open_batch(&mut self) {
        if self.open_batch.is_empty() {
            return;
        }
        // On replay, batch numbers up to `acknowledged` belong to already
        // confirmed batches that will never reappear; skip those slots so
        // re-derived unacknowledged batches keep their original numbers.
        if self.next_batch_number <= self.acknowledged {
            self.next_batch_number = self.acknowledged + 1;
        }
        let number = self.next_batch_number;
        self.next_batch_number += 1;
        let transactions = std::mem::take(&mut self.open_batch);
        self.sealed.insert(
            number,
            Batch {
                number,
                transactions,
            },
        );
    }

    /// Seal the partial open batch, if any. Full batches seal themselves; use
    /// this when the upstream log is temporarily drained.
    pub fn flush(&mut self) {
        self.pump();
        self.seal_open_batch();
    }

    /// Return the next sealed, unacknowledged batch, filling batches from
    /// any newly ready transactions first.
    ///
    /// The batch is *retained* by the decoder until it is acknowledged:
    /// calling this method repeatedly returns the same batch number and
    /// content, which models downstream redelivery of an unacknowledged
    /// batch. Returns `None` when nothing is publishable (including while a
    /// hole blocks the stream).
    pub fn next_batch(&mut self) -> Option<Batch> {
        self.pump();
        self.seal_open_batch();
        self.sealed.values().next().cloned()
    }

    /// All sealed, unacknowledged batches in batch number order. The
    /// returned batches stay retained until acknowledged.
    pub fn pending_batches(&mut self) -> Vec<Batch> {
        self.pump();
        self.seal_open_batch();
        self.sealed.values().cloned().collect()
    }

    /// Acknowledge that every transaction in `number` and all lower batches
    /// has been durably processed. Acknowledgement is cumulative.
    pub fn acknowledge(&mut self, number: u64) -> Result<(), DecoderError> {
        if number <= self.acknowledged {
            return Err(DecoderError::AckOutOfOrder {
                requested: number,
                highest: self.acknowledged,
            });
        }
        if !self.sealed.contains_key(&number) {
            return Err(DecoderError::UnknownBatch { number });
        }
        let retired: Vec<u64> = self
            .sealed
            .keys()
            .copied()
            .filter(|batch_number| *batch_number <= number)
            .collect();
        let mut confirmed = self.confirmed_lsn;
        for batch_number in retired {
            let batch = self
                .sealed
                .remove(&batch_number)
                .expect("key collected from the same map");
            if let Some(last) = batch.transactions.last() {
                confirmed = confirmed.max(last.commit_lsn);
            }
        }
        self.acknowledged = number;
        self.confirmed_lsn = confirmed;
        Ok(())
    }

    /// The first ordering hole, if any. Nothing with a higher commit position
    /// is published while a hole exists.
    pub fn first_hole(&mut self) -> Option<BlockReason> {
        self.pump();
        self.ready.values().find_map(|ready| match ready {
            Ready::Blocked(reason) => Some(reason.clone()),
            Ready::Decoded(_) => None,
        })
    }

    /// All holes currently retained, in commit-lsn order.
    pub fn holes(&mut self) -> Vec<BlockReason> {
        self.pump();
        self.ready
            .values()
            .filter_map(|ready| match ready {
                Ready::Blocked(reason) => Some(reason.clone()),
                Ready::Decoded(_) => None,
            })
            .collect()
    }

    /// Number of transactions still open (neither committed nor aborted).
    pub fn open_transaction_count(&self) -> usize {
        self.in_progress.len()
    }

    /// Highest log position covered by acknowledged batches.
    pub fn confirmed_lsn(&self) -> Lsn {
        self.confirmed_lsn
    }

    /// Highest acknowledged batch number (0 before the first acknowledgement).
    pub fn acknowledged_batch(&self) -> u64 {
        self.acknowledged
    }

    /// Number that the next sealed batch will carry.
    pub fn next_batch_number(&self) -> u64 {
        self.next_batch_number
    }

    /// Number of committed schema versions currently retained.
    pub fn retained_schema_count(&self) -> usize {
        self.registry.len()
    }
}

/// Resolve a raw change against one concrete schema version.
fn decode_against_schema(ordered: &OrderedChange, schema: &TableSchema) -> DecodedChange {
    let raw = &ordered.change;
    let mut columns = Vec::with_capacity(schema.columns.len());
    let mut used: BTreeSet<u64> = BTreeSet::new();
    for spec in &schema.columns {
        let value = raw.values.get(&spec.id).cloned();
        if value.is_some() {
            used.insert(spec.id);
        }
        columns.push(ResolvedValue {
            column_id: spec.id,
            name: spec.name.clone(),
            data_type: spec.data_type.clone(),
            value,
        });
    }
    let unresolved = raw
        .values
        .iter()
        .filter(|(id, _)| !used.contains(id))
        .map(|(id, value)| (*id, value.clone()))
        .collect();

    DecodedChange {
        lsn: ordered.lsn,
        op: raw.op,
        table_id: raw.table_id,
        schema_version: raw.schema_version,
        columns,
        unresolved,
    }
}
