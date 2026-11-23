//! Pinned query snapshots for cross-partition batched reads.

use crate::{Key, KeyRange, PartitionId};
use std::collections::BTreeMap;
use std::sync::Arc;

/// One partition as pinned by a [`QuerySnapshot`].
pub(crate) struct PartSnapshot {
    pub partition: PartitionId,
    pub range: KeyRange,
    pub data: Arc<BTreeMap<Key, String>>,
}

/// A cross-partition query pinned to the partition directory and the
/// per-partition data versions that were visible when it was opened.
///
/// All batches fetched through this snapshot come from the same directory
/// epoch and the same pinned data versions, no matter how the table is
/// written or exchanged in the meantime.
pub struct QuerySnapshot {
    directory_epoch: u64,
    parts: Vec<PartSnapshot>,
}

/// Cursor for batched fetching across the partitions of a snapshot.
#[derive(Clone, Debug, Default)]
pub struct BatchCursor {
    part_idx: usize,
    next_key: Option<Key>,
    done: bool,
}

impl QuerySnapshot {
    pub(crate) fn new(directory_epoch: u64, parts: Vec<PartSnapshot>) -> Self {
        QuerySnapshot {
            directory_epoch,
            parts,
        }
    }

    /// Directory epoch this query is pinned to.
    pub fn directory_epoch(&self) -> u64 {
        self.directory_epoch
    }

    /// Number of partitions in the pinned directory.
    pub fn partition_count(&self) -> usize {
        self.parts.len()
    }

    /// A fresh cursor scanning every partition in range order.
    pub fn cursor(&self) -> BatchCursor {
        BatchCursor::default()
    }

    /// Fetch the next batch of up to `limit` rows across partitions,
    /// advancing `cursor`. Returns fewer than `limit` rows (possibly none)
    /// once the pinned data is exhausted.
    pub fn fetch_batch(&self, cursor: &mut BatchCursor, limit: usize) -> Vec<(Key, String)> {
        let mut out = Vec::new();
        if cursor.done || limit == 0 {
            return out;
        }
        while out.len() < limit && cursor.part_idx < self.parts.len() {
            let part = &self.parts[cursor.part_idx];
            let from = cursor.next_key.unwrap_or(part.range.start).max(part.range.start);
            for (&key, value) in part.data.range(from..part.range.end) {
                if out.len() == limit {
                    break;
                }
                out.push((key, value.clone()));
                cursor.next_key = Some(key.saturating_add(1));
            }
            if out.len() < limit {
                cursor.part_idx += 1;
                cursor.next_key = None;
            }
        }
        if cursor.part_idx >= self.parts.len() {
            cursor.done = true;
        }
        out
    }

    /// Whether `cursor` has walked past the last pinned partition.
    pub fn is_exhausted(&self, cursor: &BatchCursor) -> bool {
        cursor.done
    }

    /// All pinned rows whose key falls into `[start, end)`, in key order.
    pub fn rows_in_range(&self, start: Key, end: Key) -> Vec<(Key, String)> {
        let mut out = Vec::new();
        for part in &self.parts {
            let from = start.max(part.range.start);
            let to = end.min(part.range.end);
            if from >= to {
                continue;
            }
            for (&key, value) in part.data.range(from..to) {
                out.push((key, value.clone()));
            }
        }
        out
    }

    /// All pinned rows across every partition, in key order.
    pub fn all_rows(&self) -> Vec<(Key, String)> {
        self.rows_in_range(Key::MIN, Key::MAX)
    }
}
