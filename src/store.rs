use std::collections::BTreeMap;

use crate::interval::{Interval, IntervalSet};

/// Identifier of an input column batch stream.
pub type ColumnId = u64;

/// Class of a predicate bit for a column.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Kind {
    /// Rows where the predicate evaluated to true.
    True,
    /// Rows where the predicate outcome is unknown (e.g. null).
    Unknown,
}

/// One versioned correction to an interval set.
#[derive(Clone, Copy, Debug)]
struct Delta {
    version: u64,
    interval: Interval,
    /// `true` adds the interval, `false` removes it.
    insert: bool,
}

#[derive(Default)]
struct ColumnDeltas {
    true_deltas: Vec<Delta>,
    unknown_deltas: Vec<Delta>,
}

/// Versioned store of compressed interval inputs.
///
/// Every correction bumps the store version by one and is appended, never
/// mutated in place, so any past version can be reconstructed by replaying
/// the deltas with `version <= pinned`. Reconstruction cost is proportional
/// to the number of stored segments.
#[derive(Default)]
pub struct Store {
    version: u64,
    visibility: Vec<Delta>,
    columns: BTreeMap<ColumnId, ColumnDeltas>,
}

impl Store {
    pub fn new() -> Self {
        Store::default()
    }

    /// Current (latest) version. A query opened now pins this version.
    pub fn current_version(&self) -> u64 {
        self.version
    }

    fn bump(&mut self) -> u64 {
        self.version += 1;
        self.version
    }

    /// Correct the visibility mask: add (`insert = true`) or remove
    /// (`insert = false`) a half-open interval of visible rows.
    pub fn correct_visibility(&mut self, interval: Interval, insert: bool) -> u64 {
        let version = self.bump();
        self.visibility.push(Delta { version, interval, insert });
        version
    }

    /// Correct one column's predicate bits: add or remove an interval of
    /// `kind` rows. Columns may arrive in arbitrarily many batches with
    /// misaligned segment boundaries; each batch is one or more corrections.
    pub fn correct_column(
        &mut self,
        column: ColumnId,
        kind: Kind,
        interval: Interval,
        insert: bool,
    ) -> u64 {
        let version = self.bump();
        let entry = self.columns.entry(column).or_default();
        let deltas = match kind {
            Kind::True => &mut entry.true_deltas,
            Kind::Unknown => &mut entry.unknown_deltas,
        };
        deltas.push(Delta { version, interval, insert });
        version
    }

    /// Reconstruct the visibility set as of `version`.
    pub(crate) fn visibility_at(&self, version: u64) -> IntervalSet {
        replay(&self.visibility, version)
    }

    /// Reconstruct one column's `(true, unknown)` sets as of `version`.
    /// Columns with no corrections yield empty sets (absent bits are false).
    pub(crate) fn column_at(&self, column: ColumnId, version: u64) -> (IntervalSet, IntervalSet) {
        match self.columns.get(&column) {
            Some(entry) => (
                replay(&entry.true_deltas, version),
                replay(&entry.unknown_deltas, version),
            ),
            None => (IntervalSet::empty(), IntervalSet::empty()),
        }
    }
}

fn replay(deltas: &[Delta], version: u64) -> IntervalSet {
    let mut set = IntervalSet::empty();
    for d in deltas {
        if d.version > version {
            continue;
        }
        let single = IntervalSet::from_intervals([d.interval]);
        set = if d.insert { set.union(&single) } else { set.subtract(&single) };
    }
    set
}
