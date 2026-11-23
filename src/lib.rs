//! Online partition exchange for a range-partitioned relational table.
//!
//! The active table is split into range partitions. Each partition can be
//! exchanged with an independent staging table in two phases:
//!
//! 1. **Prepare** — incrementally (step-wise) validate that every staging row
//!    key falls inside the partition range and that keys are unique. Writes
//!    are still allowed on both sides while a prepare is in progress.
//! 2. **Commit** — re-validates the staging changes that happened after the
//!    validation baseline (the delta), then atomically swaps the data: the
//!    staging rows become the active partition and the old partition rows
//!    become the staging table.
//!
//! Queries opened before/after the exchange are pinned to the partition
//! directory and the per-partition data versions visible at open time, so
//! batched cross-partition reads never mix directories.

mod snapshot;

pub use snapshot::{BatchCursor, QuerySnapshot};

use std::collections::{BTreeMap, HashSet};
use std::sync::{Arc, Mutex};

/// Row key of the relational table.
pub type Key = i64;
/// Identifier of an active-table partition.
pub type PartitionId = u64;
/// Identifier of a staging table.
pub type StagingId = u64;
/// Identifier of a prepare (exchange-intent) record.
pub type PrepareId = u64;

/// Half-open key range `[start, end)` of a partition.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct KeyRange {
    pub start: Key,
    pub end: Key,
}

impl KeyRange {
    pub fn new(start: Key, end: Key) -> Result<KeyRange, ExchangeError> {
        if start >= end {
            return Err(ExchangeError::InvalidRange { start, end });
        }
        Ok(KeyRange { start, end })
    }

    pub fn contains(&self, key: Key) -> bool {
        self.start <= key && key < self.end
    }

    pub fn overlaps(&self, other: &KeyRange) -> bool {
        self.start < other.end && other.start < self.end
    }
}

/// A validation violation found while scanning (or delta-checking) a staging
/// table against a partition range.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Violation {
    /// A staging row key lies outside the target partition range.
    OutOfRange { key: Key, range: KeyRange },
    /// The same key appears more than once in the staging table.
    DuplicateKey { key: Key },
}

/// Why a prepare record is considered stale at commit time.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum StaleReason {
    /// The partition boundaries changed after the prepare was created.
    BoundaryChanged,
    /// The partition was already exchanged by another operation.
    PartitionExchangedByOther,
}

/// Errors returned by the exchange API.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum ExchangeError {
    InvalidRange { start: Key, end: Key },
    RangeOverlaps { range: KeyRange, existing: PartitionId },
    NoSuchPartition(PartitionId),
    NoSuchStaging(StagingId),
    NoSuchPrepare(PrepareId),
    NoPartitionForKey(Key),
    /// Scan step requested while the prepare is not in `Scanning` state.
    PrepareNotScanning(PrepareId),
    /// Commit requested before the validation scan finished.
    PrepareNotValidated(PrepareId),
    /// Commit requested while the prepare holds unresolved violations.
    PrepareFailed(PrepareId, Vec<Violation>),
    /// The prepare record no longer matches the partition it targets.
    StalePrepare { prepare: PrepareId, reason: StaleReason },
    /// Staging rows changed after the validation baseline and the delta
    /// does not validate.
    DeltaValidationFailed { prepare: PrepareId, violations: Vec<Violation> },
}

/// Lifecycle state of a prepare record.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum PrepareState {
    /// Incremental validation scan is in progress.
    Scanning,
    /// Scan finished without violations; a validation baseline was taken.
    Validated,
    /// Scan finished with violations; fix the staging data and restart.
    Failed,
    /// The exchange was committed (commit is idempotently retryable).
    Committed,
}

/// Result of one incremental validation step.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ScanStepResult {
    /// Rows inspected in this step.
    pub scanned: usize,
    /// Whether the scan reached the end of the staging table.
    pub done: bool,
    /// All violations accumulated so far.
    pub violations: Vec<Violation>,
}

/// Proof of a committed exchange.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ExchangeReceipt {
    pub prepare: PrepareId,
    pub partition: PartitionId,
    pub staging: StagingId,
    pub rows_into_partition: usize,
    pub rows_into_staging: usize,
    /// Directory epoch after the swap; new queries observe this directory.
    pub directory_epoch: u64,
}

#[derive(Clone, Debug)]
struct StagingRow {
    key: Key,
    value: String,
    /// Tombstone flag: deletes mark rows dead so that an in-progress scan
    /// cursor (a row index) is never invalidated by shifting entries.
    alive: bool,
}

struct PartitionState {
    range: KeyRange,
    /// Copy-on-write row store; a query pins the `Arc` it saw at open time.
    data: Arc<BTreeMap<Key, String>>,
    /// Bumped whenever the partition boundaries are redefined.
    boundary_gen: u64,
    /// Bumped whenever this partition is exchanged.
    exchange_epoch: u64,
}

struct StagingState {
    rows: Vec<StagingRow>,
    /// Bumped on every mutation; the validation baseline is a version.
    version: u64,
    /// `(version, key)` of every mutation, used for delta validation.
    change_log: Vec<(u64, Key)>,
}

impl StagingState {
    fn mutate<F: FnOnce(&mut Vec<StagingRow>) -> R, R>(&mut self, key: Key, f: F) -> R {
        self.version += 1;
        self.change_log.push((self.version, key));
        f(&mut self.rows)
    }
}

struct PrepareRecord {
    partition: PartitionId,
    staging: StagingId,
    boundary_gen: u64,
    exchange_epoch: u64,
    cursor: usize,
    seen: HashSet<Key>,
    violations: Vec<Violation>,
    state: PrepareState,
    /// Staging version at the moment the scan completed successfully.
    baseline_version: Option<u64>,
    receipt: Option<ExchangeReceipt>,
}

#[derive(Default)]
struct Inner {
    partitions: BTreeMap<PartitionId, PartitionState>,
    partition_order: Vec<PartitionId>,
    stagings: BTreeMap<StagingId, StagingState>,
    prepares: BTreeMap<PrepareId, PrepareRecord>,
    next_id: u64,
    directory_epoch: u64,
}

impl Inner {
    fn alloc_id(&mut self) -> u64 {
        self.next_id += 1;
        self.next_id
    }
}

/// The database: one range-partitioned active table plus any number of
/// independent staging tables.
pub struct Database {
    inner: Mutex<Inner>,
}

impl Default for Database {
    fn default() -> Self {
        Self::new()
    }
}

impl Database {
    pub fn new() -> Self {
        Database {
            inner: Mutex::new(Inner::default()),
        }
    }

    /// Create the active table from sorted, non-overlapping `(start, end)`
    /// ranges. Returns the partition ids in range order.
    pub fn create_active_table(
        &self,
        ranges: &[(Key, Key)],
    ) -> Result<Vec<PartitionId>, ExchangeError> {
        let mut inner = self.inner.lock().unwrap();
        let mut ids = Vec::with_capacity(ranges.len());
        let mut built: Vec<(PartitionId, KeyRange)> = Vec::with_capacity(ranges.len());
        for &(start, end) in ranges {
            let range = KeyRange::new(start, end)?;
            for &(existing_id, existing) in &built {
                if range.overlaps(&existing) {
                    return Err(ExchangeError::RangeOverlaps {
                        range,
                        existing: existing_id,
                    });
                }
            }
            let id = inner.alloc_id();
            built.push((id, range));
            ids.push(id);
        }
        built.sort_by_key(|&(_, range)| range.start);
        for (id, range) in built {
            inner.partitions.insert(
                id,
                PartitionState {
                    range,
                    data: Arc::new(BTreeMap::new()),
                    boundary_gen: 0,
                    exchange_epoch: 0,
                },
            );
            inner.partition_order.push(id);
        }
        inner.directory_epoch += 1;
        Ok(ids)
    }

    /// Create an empty independent staging table.
    pub fn create_staging_table(&self) -> StagingId {
        let mut inner = self.inner.lock().unwrap();
        let id = inner.alloc_id();
        inner.stagings.insert(
            id,
            StagingState {
                rows: Vec::new(),
                version: 0,
                change_log: Vec::new(),
            },
        );
        id
    }

    /// Insert (or replace) a row in the active table. Allowed at any time,
    /// including while a prepare is in progress.
    pub fn insert_active(&self, key: Key, value: &str) -> Result<(), ExchangeError> {
        let mut inner = self.inner.lock().unwrap();
        let partition_id = inner
            .partition_order
            .iter()
            .copied()
            .find(|id| inner.partitions[id].range.contains(key))
            .ok_or(ExchangeError::NoPartitionForKey(key))?;
        let partition = inner.partitions.get_mut(&partition_id).unwrap();
        let mut data = (*partition.data).clone();
        data.insert(key, value.to_string());
        partition.data = Arc::new(data);
        Ok(())
    }

    /// Delete a row from the active table; returns whether it existed.
    pub fn delete_active(&self, key: Key) -> Result<bool, ExchangeError> {
        let mut inner = self.inner.lock().unwrap();
        let partition_id = inner
            .partition_order
            .iter()
            .copied()
            .find(|id| inner.partitions[id].range.contains(key))
            .ok_or(ExchangeError::NoPartitionForKey(key))?;
        let partition = inner.partitions.get_mut(&partition_id).unwrap();
        let mut data = (*partition.data).clone();
        let removed = data.remove(&key).is_some();
        partition.data = Arc::new(data);
        Ok(removed)
    }

    /// Append a row to a staging table. Staging tables are heaps: duplicate
    /// keys are physically allowed here and are rejected by validation.
    pub fn staging_insert(
        &self,
        staging: StagingId,
        key: Key,
        value: &str,
    ) -> Result<(), ExchangeError> {
        let mut inner = self.inner.lock().unwrap();
        let staging = inner
            .stagings
            .get_mut(&staging)
            .ok_or(ExchangeError::NoSuchStaging(staging))?;
        staging.mutate(key, |rows| {
            rows.push(StagingRow {
                key,
                value: value.to_string(),
                alive: true,
            });
        });
        Ok(())
    }

    /// Delete every staging row with this key (tombstones). Returns how many
    /// live rows were removed.
    pub fn staging_delete(&self, staging: StagingId, key: Key) -> Result<usize, ExchangeError> {
        let mut inner = self.inner.lock().unwrap();
        let staging = inner
            .stagings
            .get_mut(&staging)
            .ok_or(ExchangeError::NoSuchStaging(staging))?;
        Ok(staging.mutate(key, |rows| {
            let mut removed = 0;
            for row in rows.iter_mut() {
                if row.alive && row.key == key {
                    row.alive = false;
                    removed += 1;
                }
            }
            removed
        }))
    }

    /// Live staging rows in physical order (verification helper).
    pub fn staging_rows(&self, staging: StagingId) -> Result<Vec<(Key, String)>, ExchangeError> {
        let inner = self.inner.lock().unwrap();
        let staging = inner
            .stagings
            .get(&staging)
            .ok_or(ExchangeError::NoSuchStaging(staging))?;
        Ok(staging
            .rows
            .iter()
            .filter(|row| row.alive)
            .map(|row| (row.key, row.value.clone()))
            .collect())
    }

    /// Current rows of an active partition, sorted by key (verification
    /// helper).
    pub fn partition_rows(&self, partition: PartitionId) -> Result<Vec<(Key, String)>, ExchangeError> {
        let inner = self.inner.lock().unwrap();
        let partition = inner
            .partitions
            .get(&partition)
            .ok_or(ExchangeError::NoSuchPartition(partition))?;
        Ok(partition
            .data
            .iter()
            .map(|(&k, v)| (k, v.clone()))
            .collect())
    }

    /// Current directory epoch; bumped by table creation, boundary changes
    /// and exchanges.
    pub fn directory_epoch(&self) -> u64 {
        self.inner.lock().unwrap().directory_epoch
    }

    /// Open a prepare record for exchanging `partition` with `staging`.
    /// The record pins the current boundary generation and exchange epoch so
    /// that a stale prepare cannot be committed later.
    pub fn prepare_exchange(
        &self,
        partition: PartitionId,
        staging: StagingId,
    ) -> Result<PrepareId, ExchangeError> {
        let mut inner = self.inner.lock().unwrap();
        let (boundary_gen, exchange_epoch) = {
            let partition_state = inner
                .partitions
                .get(&partition)
                .ok_or(ExchangeError::NoSuchPartition(partition))?;
            (partition_state.boundary_gen, partition_state.exchange_epoch)
        };
        if !inner.stagings.contains_key(&staging) {
            return Err(ExchangeError::NoSuchStaging(staging));
        }
        let id = inner.alloc_id();
        inner.prepares.insert(
            id,
            PrepareRecord {
                partition,
                staging,
                boundary_gen,
                exchange_epoch,
                cursor: 0,
                seen: HashSet::new(),
                violations: Vec::new(),
                state: PrepareState::Scanning,
                baseline_version: None,
                receipt: None,
            },
        );
        Ok(id)
    }

    /// Advance the validation scan by at most `max_rows` staging rows.
    /// Scanning is step-wise: callers choose the batch size and may interleave
    /// other work (including writes) between steps.
    pub fn prepare_scan_step(
        &self,
        prepare: PrepareId,
        max_rows: usize,
    ) -> Result<ScanStepResult, ExchangeError> {
        let mut inner = self.inner.lock().unwrap();
        let (partition_id, staging_id) = {
            let record = inner
                .prepares
                .get(&prepare)
                .ok_or(ExchangeError::NoSuchPrepare(prepare))?;
            if record.state != PrepareState::Scanning {
                return Err(ExchangeError::PrepareNotScanning(prepare));
            }
            (record.partition, record.staging)
        };
        let range = inner.partitions[&partition_id].range;
        let staging = inner.stagings.get(&staging_id).unwrap();
        // Snapshot the physical rows for this step. Rows appended by
        // concurrent inserts become visible to later steps; tombstoned rows
        // keep their slots so the cursor stays valid.
        let rows: Vec<StagingRow> = staging.rows.to_vec();
        let staging_version = staging.version;

        let record = inner.prepares.get_mut(&prepare).unwrap();
        let end = (record.cursor + max_rows).min(rows.len());
        for row in &rows[record.cursor..end] {
            if !row.alive {
                continue;
            }
            if !range.contains(row.key) {
                record.violations.push(Violation::OutOfRange {
                    key: row.key,
                    range,
                });
            }
            if !record.seen.insert(row.key) {
                record.violations.push(Violation::DuplicateKey { key: row.key });
            }
        }
        let scanned = end - record.cursor;
        record.cursor = end;
        let done = record.cursor == rows.len();
        if done {
            if record.violations.is_empty() {
                record.state = PrepareState::Validated;
                record.baseline_version = Some(staging_version);
            } else {
                record.state = PrepareState::Failed;
            }
        }
        Ok(ScanStepResult {
            scanned,
            done,
            violations: record.violations.clone(),
        })
    }

    /// Restart the validation scan of a prepare record (retry after fixing
    /// the staging data). Resets the cursor, the seen-set and the recorded
    /// violations; the baseline is taken again when the scan completes.
    pub fn prepare_restart(&self, prepare: PrepareId) -> Result<(), ExchangeError> {
        let mut inner = self.inner.lock().unwrap();
        let record = inner
            .prepares
            .get_mut(&prepare)
            .ok_or(ExchangeError::NoSuchPrepare(prepare))?;
        if record.state == PrepareState::Committed {
            return Err(ExchangeError::PrepareNotScanning(prepare));
        }
        record.cursor = 0;
        record.seen.clear();
        record.violations.clear();
        record.state = PrepareState::Scanning;
        record.baseline_version = None;
        Ok(())
    }

    pub fn prepare_state(&self, prepare: PrepareId) -> Result<PrepareState, ExchangeError> {
        let inner = self.inner.lock().unwrap();
        inner
            .prepares
            .get(&prepare)
            .map(|record| record.state.clone())
            .ok_or(ExchangeError::NoSuchPrepare(prepare))
    }

    /// Commit the exchange. Retrying a committed prepare returns the original
    /// receipt. A prepare whose partition boundaries changed, or whose
    /// partition was exchanged by another operation, is rejected as stale.
    /// Staging mutations after the validation baseline are delta-validated
    /// here; on delta violations the prepare stays `Validated` so the data
    /// can be fixed and the commit retried.
    pub fn commit_exchange(&self, prepare: PrepareId) -> Result<ExchangeReceipt, ExchangeError> {
        let mut inner = self.inner.lock().unwrap();
        let (partition_id, staging_id) = {
            let record = inner
                .prepares
                .get(&prepare)
                .ok_or(ExchangeError::NoSuchPrepare(prepare))?;
            // Idempotent retry of an already-committed prepare.
            if let Some(receipt) = &record.receipt {
                return Ok(receipt.clone());
            }
            (record.partition, record.staging)
        };

        let partition = inner
            .partitions
            .get(&partition_id)
            .ok_or(ExchangeError::NoSuchPartition(partition_id))?;
        let record = inner.prepares.get(&prepare).unwrap();
        if partition.boundary_gen != record.boundary_gen {
            return Err(ExchangeError::StalePrepare {
                prepare,
                reason: StaleReason::BoundaryChanged,
            });
        }
        if partition.exchange_epoch != record.exchange_epoch {
            return Err(ExchangeError::StalePrepare {
                prepare,
                reason: StaleReason::PartitionExchangedByOther,
            });
        }
        match record.state {
            PrepareState::Scanning => return Err(ExchangeError::PrepareNotValidated(prepare)),
            PrepareState::Failed => {
                return Err(ExchangeError::PrepareFailed(
                    prepare,
                    record.violations.clone(),
                ))
            }
            PrepareState::Committed => unreachable!("receipt handled above"),
            PrepareState::Validated => {}
        }
        let baseline = record.baseline_version.unwrap();
        let range = partition.range;

        // Delta validation: re-check every staging key that changed after the
        // validation baseline against the *current* staging contents.
        let staging = inner.stagings.get(&staging_id).unwrap();
        if staging.version != baseline {
            let changed: HashSet<Key> = staging
                .change_log
                .iter()
                .filter(|(version, _)| *version > baseline)
                .map(|&(_, key)| key)
                .collect();
            if !changed.is_empty() {
                let mut counts: BTreeMap<Key, usize> = BTreeMap::new();
                for row in staging.rows.iter().filter(|row| row.alive) {
                    *counts.entry(row.key).or_insert(0) += 1;
                }
                let mut violations = Vec::new();
                for &key in &changed {
                    let live = counts.get(&key).copied().unwrap_or(0);
                    // A key whose rows were all deleted again needs no check.
                    if live > 0 && !range.contains(key) {
                        violations.push(Violation::OutOfRange { key, range });
                    }
                    if live > 1 {
                        violations.push(Violation::DuplicateKey { key });
                    }
                }
                if !violations.is_empty() {
                    return Err(ExchangeError::DeltaValidationFailed { prepare, violations });
                }
            }
        }

        // Atomic swap: staging rows become the partition, the old partition
        // rows become the staging table.
        let staging = inner.stagings.get_mut(&staging_id).unwrap();
        let mut new_partition_data = BTreeMap::new();
        for row in staging.rows.iter().filter(|row| row.alive) {
            new_partition_data.insert(row.key, row.value.clone());
        }
        let rows_into_partition = new_partition_data.len();

        let partition = inner.partitions.get_mut(&partition_id).unwrap();
        let old_partition_data = (*partition.data).clone();
        let rows_into_staging = old_partition_data.len();

        partition.data = Arc::new(new_partition_data);
        partition.exchange_epoch += 1;

        let staging = inner.stagings.get_mut(&staging_id).unwrap();
        staging.rows = old_partition_data
            .into_iter()
            .map(|(key, value)| StagingRow {
                key,
                value,
                alive: true,
            })
            .collect();
        staging.version += 1;
        staging.change_log.clear();

        inner.directory_epoch += 1;
        let receipt = ExchangeReceipt {
            prepare,
            partition: partition_id,
            staging: staging_id,
            rows_into_partition,
            rows_into_staging,
            directory_epoch: inner.directory_epoch,
        };
        let record = inner.prepares.get_mut(&prepare).unwrap();
        record.state = PrepareState::Committed;
        record.receipt = Some(receipt.clone());
        Ok(receipt)
    }

    /// Redefine a partition's boundaries. Bumps the boundary generation so
    /// that outstanding prepare records for this partition become stale.
    pub fn redefine_partition_range(
        &self,
        partition: PartitionId,
        start: Key,
        end: Key,
    ) -> Result<(), ExchangeError> {
        let mut inner = self.inner.lock().unwrap();
        let range = KeyRange::new(start, end)?;
        for (&other_id, other) in &inner.partitions {
            if other_id != partition && range.overlaps(&other.range) {
                return Err(ExchangeError::RangeOverlaps {
                    range,
                    existing: other_id,
                });
            }
        }
        let partition_state = inner
            .partitions
            .get_mut(&partition)
            .ok_or(ExchangeError::NoSuchPartition(partition))?;
        partition_state.range = range;
        partition_state.boundary_gen += 1;
        let mut order: Vec<(Key, PartitionId)> = inner
            .partition_order
            .iter()
            .map(|id| (inner.partitions[id].range.start, *id))
            .collect();
        order.sort();
        inner.partition_order = order.into_iter().map(|(_, id)| id).collect();
        inner.directory_epoch += 1;
        Ok(())
    }

    /// Open a cross-partition query. The snapshot pins the partition
    /// directory and the per-partition data versions visible right now;
    /// batched fetches through it never observe later writes or exchanges.
    pub fn open_query(&self) -> QuerySnapshot {
        let inner = self.inner.lock().unwrap();
        let parts = inner
            .partition_order
            .iter()
            .map(|id| {
                let partition = &inner.partitions[id];
                snapshot::PartSnapshot {
                    partition: *id,
                    range: partition.range,
                    data: Arc::clone(&partition.data),
                }
            })
            .collect();
        QuerySnapshot::new(inner.directory_epoch, parts)
    }
}
