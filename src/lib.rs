//! A small row-versioned relational table kernel.
//!
//! The table keeps columns and rows separate:
//!
//! * A column has a stable internal id and a stable physical slot. Slots are
//!   never reused, even when a column is dropped and another column with the
//!   same name is created later.
//! * Every row stores the layout version it was written with, plus the raw
//!   physical slots it actually contains. Adding a column changes only the
//!   schema; existing rows are left byte-for-byte (slot-for-slot) untouched.
//! * Reading a column whose slot is not present in a row falls back to the
//!   default that was recorded when that column was *added*. Later changes to
//!   the insertion default do not change the value seen by such a row.
//! * Inserting a row writes the current layout. Columns omitted from the
//!   insert use the insertion default *at insertion time*; an explicit `None`
//!   is stored as an explicit SQL NULL and is never replaced by a default.
//! * [`Table::compact_row`] rewrites exactly one row into the current layout.

use std::collections::BTreeMap;
use std::collections::HashMap;
use std::collections::HashSet;
use std::error::Error;
use std::fmt;

/// Primary-key type used by [`Table`].
pub type Key = i64;

/// Stable internal identity of a column.
///
/// Dropping a column and adding one with the same name creates a new
/// `ColumnId`; identities are never recycled.
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct ColumnId(u64);

impl ColumnId {
    /// Returns the numeric value of this identity (useful for diagnostics).
    pub fn get(self) -> u64 {
        self.0
    }
}

/// Public snapshot of a column's metadata.
///
/// `insert_default` is used for new inserts that omit the column.
/// `added_default` is frozen when the column is added and is the value read
/// by rows written before the column existed.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ColumnInfo {
    id: ColumnId,
    name: String,
    slot: usize,
    insert_default: Option<i64>,
    added_default: Option<i64>,
    added_version: u64,
    live: bool,
}

impl ColumnInfo {
    /// Stable internal identity.
    pub fn id(&self) -> ColumnId {
        self.id
    }

    /// Column name.
    pub fn name(&self) -> &str {
        &self.name
    }

    /// Physical slot index used by row encodings.
    ///
    /// Slot indices are monotonically assigned and never reused.
    pub fn slot(&self) -> usize {
        self.slot
    }

    /// Default used by new inserts that omit this column.
    pub fn insert_default(&self) -> Option<i64> {
        self.insert_default
    }

    /// Default captured when the column was added.
    ///
    /// Rows encoded before `added_version` read this value if their raw slot
    /// vector does not reach this column's slot.
    pub fn added_default(&self) -> Option<i64> {
        self.added_default
    }

    /// Layout version at which this column was added.
    pub fn added_version(&self) -> u64 {
        self.added_version
    }

    /// Whether the column still belongs to the current schema.
    pub fn is_live(&self) -> bool {
        self.live
    }
}

/// Errors returned by table operations.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum TableError {
    /// A live column with this name already exists.
    DuplicateColumn(String),
    /// No live column has this name.
    UnknownColumn(String),
    /// A row with this primary key already exists.
    DuplicateKey(Key),
    /// No row has this primary key.
    UnknownKey(Key),
    /// The same column was supplied more than once to one insert.
    DuplicateValue(String),
}

impl fmt::Display for TableError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            TableError::DuplicateColumn(name) => {
                write!(f, "column already exists: {name}")
            }
            TableError::UnknownColumn(name) => {
                write!(f, "unknown or dropped column: {name}")
            }
            TableError::DuplicateKey(key) => write!(f, "row already exists: {key}"),
            TableError::UnknownKey(key) => write!(f, "no such row: {key}"),
            TableError::DuplicateValue(name) => {
                write!(f, "column supplied more than once in insert: {name}")
            }
        }
    }
}

impl Error for TableError {}

#[derive(Clone, Debug)]
struct RowData {
    /// Layout version this row was written with.
    version: u64,
    /// Raw physical field slots. `slots[slot]` is:
    /// * absent (index >= `len`) — the column did not exist when written and
    ///   the frozen add-time default applies;
    /// * `Some(value)` — a stored integer;
    /// * `None` — a stored explicit SQL NULL.
    slots: Vec<Option<i64>>,
}

/// A borrowed view of one row.
#[derive(Debug)]
pub struct RowView<'a> {
    table: &'a Table,
    key: Key,
    row: &'a RowData,
}

impl<'a> RowView<'a> {
    /// Primary key of the row.
    pub fn key(&self) -> Key {
        self.key
    }

    /// Encoding/layout version stored with the row.
    pub fn encoded_version(&self) -> u64 {
        self.row.version
    }

    /// Raw physical field slots exactly as stored.
    ///
    /// The slice may be shorter than the current layout; a column whose slot
    /// index is outside this slice is read through its frozen add-time
    /// default. Use [`RowView::value`] for the logical value.
    pub fn raw_slots(&self) -> &'a [Option<i64>] {
        &self.row.slots
    }

    /// Reads one logical column value.
    ///
    /// Returns `Ok(None)` for a stored explicit NULL (or an add-time default
    /// of NULL) and `Err(TableError::UnknownColumn)` for an unknown/dropped
    /// column name.
    pub fn value(&self, name: &str) -> Result<Option<i64>, TableError> {
        let column = self.table.live_column(name)?;
        Ok(resolve_value(column, self.row))
    }

    /// Reads every live column in schema (creation) order.
    pub fn values(&self) -> Vec<(&'a str, Option<i64>)> {
        self.table
            .columns
            .iter()
            .filter(|column| column.live)
            .map(|column| (column.name.as_str(), resolve_value(column, self.row)))
            .collect()
    }
}

/// In-memory row-versioned table.
///
/// The table intentionally has no third-party dependencies and no storage
/// backend; it models the encoding/versioning semantics of a table kernel.
#[derive(Debug, Default)]
pub struct Table {
    columns: Vec<ColumnInfo>,
    live_by_name: HashMap<String, usize>,
    rows: BTreeMap<Key, RowData>,
    next_column_id: u64,
    next_slot: usize,
    schema_version: u64,
}

impl Table {
    /// Creates a table with no columns and no rows.
    pub fn new() -> Self {
        Self::default()
    }

    /// Current layout/schema version.
    ///
    /// `add_column` and `drop_column` advance it. Changing an insertion
    /// default does not, because it does not change the physical layout.
    pub fn schema_version(&self) -> u64 {
        self.schema_version
    }

    /// Number of current physical slots (including slots of dropped columns).
    pub fn slot_count(&self) -> usize {
        self.next_slot
    }

    /// Number of rows.
    pub fn row_count(&self) -> usize {
        self.rows.len()
    }

    /// Primary keys in ascending order.
    pub fn keys(&self) -> impl Iterator<Item = Key> + '_ {
        self.rows.keys().copied()
    }

    /// Live columns in schema order.
    pub fn columns(&self) -> impl Iterator<Item = &ColumnInfo> {
        self.columns.iter().filter(|column| column.live)
    }

    /// All columns ever created (including dropped ones) in creation order.
    pub fn all_columns(&self) -> impl Iterator<Item = &ColumnInfo> {
        self.columns.iter()
    }

    /// Looks up a live column by name.
    pub fn column(&self, name: &str) -> Option<&ColumnInfo> {
        self.live_by_name
            .get(name)
            .map(|index| &self.columns[*index])
    }

    /// Adds a nullable integer column.
    ///
    /// `added_default` is frozen as the value that older rows will read for
    /// this column, and also becomes the initial insertion default. No row is
    /// rewritten. Adding a dropped name again is allowed and creates a fresh
    /// identity and physical slot.
    pub fn add_column(
        &mut self,
        name: impl Into<String>,
        added_default: Option<i64>,
    ) -> Result<ColumnId, TableError> {
        let name = name.into();
        if self.live_by_name.contains_key(&name) {
            return Err(TableError::DuplicateColumn(name));
        }

        let id = ColumnId(self.next_column_id);
        self.next_column_id += 1;
        self.schema_version += 1;

        let column = ColumnInfo {
            id,
            name: name.clone(),
            slot: self.next_slot,
            insert_default: added_default,
            added_default,
            added_version: self.schema_version,
            live: true,
        };
        self.next_slot += 1;
        self.columns.push(column);
        self.live_by_name.insert(name, self.columns.len() - 1);
        Ok(id)
    }

    /// Drops a live column.
    ///
    /// Existing rows keep their physical slots (the old values remain in the
    /// raw encoding). The slot and identity are retired, so a later column
    /// with the same name can never read the old physical value.
    pub fn drop_column(&mut self, name: &str) -> Result<ColumnId, TableError> {
        let index = *self
            .live_by_name
            .get(name)
            .ok_or_else(|| TableError::UnknownColumn(name.to_string()))?;
        self.columns[index].live = false;
        self.live_by_name.remove(name);
        self.schema_version += 1;
        Ok(self.columns[index].id)
    }

    /// Changes the default used by future inserts that omit `name`.
    ///
    /// Existing rows, including rows that have never been compacted and thus
    /// still read the frozen add-time default, are unaffected. Returns the
    /// previous insertion default.
    pub fn set_insert_default(
        &mut self,
        name: &str,
        default: Option<i64>,
    ) -> Result<Option<i64>, TableError> {
        let index = *self
            .live_by_name
            .get(name)
            .ok_or_else(|| TableError::UnknownColumn(name.to_string()))?;
        let previous = self.columns[index].insert_default;
        self.columns[index].insert_default = default;
        Ok(previous)
    }

    /// Inserts a row in the current layout.
    ///
    /// `values` is a list of `(column name, value)` pairs. A column that is
    /// absent from the list uses its current insertion default. A pair with
    /// `None` stores an explicit NULL and therefore never uses a default.
    ///
    /// Validation is completed before the row is inserted, so a failed insert
    /// leaves the table unchanged.
    pub fn insert(
        &mut self,
        key: Key,
        values: &[(&str, Option<i64>)],
    ) -> Result<(), TableError> {
        if self.rows.contains_key(&key) {
            return Err(TableError::DuplicateKey(key));
        }

        // Validate names and duplicates, and record which slots were supplied.
        let mut provided: Vec<Option<Option<i64>>> = vec![None; self.next_slot];
        let mut seen: HashSet<&str> = HashSet::with_capacity(values.len());
        for &(name, value) in values {
            if !seen.insert(name) {
                return Err(TableError::DuplicateValue(name.to_string()));
            }
            let index = *self
                .live_by_name
                .get(name)
                .ok_or_else(|| TableError::UnknownColumn(name.to_string()))?;
            let slot = self.columns[index].slot;
            provided[slot] = Some(value);
        }

        let mut slots = vec![None; self.next_slot];
        for column in self.columns.iter().filter(|column| column.live) {
            slots[column.slot] = match provided[column.slot] {
                Some(value) => value,
                None => column.insert_default,
            };
        }

        self.rows.insert(
            key,
            RowData {
                version: self.schema_version,
                slots,
            },
        );
        Ok(())
    }

    /// Inserts a row using only current insertion defaults.
    pub fn insert_defaults(&mut self, key: Key) -> Result<(), TableError> {
        self.insert(key, &[])
    }

    /// Returns a borrowed view of a row.
    pub fn get_row(&self, key: Key) -> Option<RowView<'_>> {
        self.rows.get(&key).map(|row| RowView {
            table: self,
            key,
            row,
        })
    }

    /// Reads one logical value.
    ///
    /// * `Ok(None)` — no such row.
    /// * `Ok(Some(value))` — the row's value for the column; the inner
    ///   `Option` is the stored/defaulted value (NULL represented as `None`).
    /// * `Err(TableError::UnknownColumn)` — the column is not in the schema.
    pub fn value(&self, key: Key, name: &str) -> Result<Option<Option<i64>>, TableError> {
        let column = self.live_column(name)?;
        Ok(self
            .rows
            .get(&key)
            .map(|row| resolve_value(column, row)))
    }

    /// Reads all live columns of a row in schema order.
    ///
    /// Returns `None` when the row does not exist.
    pub fn row_values(&self, key: Key) -> Option<Vec<(&str, Option<i64>)>> {
        self.get_row(key).map(|row| row.values())
    }

    /// Raw physical slots of a row, or `None` when the row does not exist.
    pub fn raw_slots(&self, key: Key) -> Option<&[Option<i64>]> {
        self.rows.get(&key).map(|row| row.slots.as_slice())
    }

    /// Encoding/layout version stored on a row, or `None` when absent.
    pub fn encoded_version(&self, key: Key) -> Option<u64> {
        self.rows.get(&key).map(|row| row.version)
    }

    /// Rewrites exactly one row into the current layout.
    ///
    /// All live columns are materialised into their stable slots (including
    /// values that were previously read through a frozen add-time default).
    /// Slots belonging to dropped columns are cleared. No other row is read or
    /// modified.
    ///
    /// Logically, every live column value is unchanged. Returns the new row
    /// version. The row's new version equals [`Table::schema_version`].
    pub fn compact_row(&mut self, key: Key) -> Result<u64, TableError> {
        let (version, slots) = {
            let row = self
                .rows
                .get(&key)
                .ok_or(TableError::UnknownKey(key))?;
            let mut slots = vec![None; self.next_slot];
            for column in self.columns.iter().filter(|column| column.live) {
                slots[column.slot] = resolve_value(column, row);
            }
            (self.schema_version, slots)
        };

        let row = self
            .rows
            .get_mut(&key)
            .expect("row existence was checked above");
        row.version = version;
        row.slots = slots;
        Ok(version)
    }

    fn live_column(&self, name: &str) -> Result<&ColumnInfo, TableError> {
        self.column(name)
            .ok_or_else(|| TableError::UnknownColumn(name.to_string()))
    }
}

fn resolve_value(column: &ColumnInfo, row: &RowData) -> Option<i64> {
    match row.slots.get(column.slot) {
        // The slot exists: Some(_) is a stored value and Some(None) is an
        // explicit NULL.
        Some(value) => *value,
        // The slot predates this row: the row has never seen the column, so
        // it reads the default frozen at add-column time.
        None => column.added_default,
    }
}
