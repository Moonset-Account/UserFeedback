//! Data model for the slot-addressed heap.

use std::collections::BTreeSet;
use std::fmt;

/// Index of an object in the heap, i.e. a source-level reference.
pub type Slot = usize;

/// A weak-key table entry: `key` and `value` are slot indices.
///
/// The entry keeps `value` alive only while the table's holder and `key` are
/// both alive.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct Entry {
    pub key: Slot,
    pub value: Slot,
}

impl Entry {
    pub const fn new(key: Slot, value: Slot) -> Self {
        Self { key, value }
    }
}

/// A weak-key (ephemeron) table held by an object.
///
/// `entries` is treated as an unordered set of `(key, value)` pairs. Duplicate
/// pairs are permitted and preserved; the collector never depends on the order
/// of entries within a table, only on their membership.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Table {
    pub entries: Vec<Entry>,
}

impl Table {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn with_entry(mut self, key: Slot, value: Slot) -> Self {
        self.entries.push(Entry::new(key, value));
        self
    }

    pub fn with_entries<I>(mut self, entries: I) -> Self
    where
        I: IntoIterator<Item = Entry>,
    {
        self.entries.extend(entries);
        self
    }

    /// Drop entries whose key is not in `live`, preserving relative order.
    pub fn retain_live_keys(&mut self, live: impl Fn(Slot) -> bool) {
        self.entries.retain(|entry| live(entry.key));
    }
}

/// An object occupying a heap slot.
///
/// References are addressed by *slot index*. Strong references to vacant
/// slots, to slots owned by dead objects, and duplicate references are all
/// permitted; only references to live objects survive compaction.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Object {
    pub strong: Vec<Slot>,
    pub tables: Vec<Table>,
}

impl Object {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn with_strong(mut self, target: Slot) -> Self {
        self.strong.push(target);
        self
    }

    pub fn with_strongs<I>(mut self, targets: I) -> Self
    where
        I: IntoIterator<Item = Slot>,
    {
        self.strong.extend(targets);
        self
    }

    pub fn with_table(mut self, table: Table) -> Self {
        self.tables.push(table);
        self
    }

    /// Visit every slot this object refers to as a *value* candidate:
    /// strong targets plus the values of all table entries.
    ///
    /// Keys are deliberately excluded: they are not reached until the table's
    /// holder and the key itself are proven live.
    pub fn visit_value_targets(&self, mut f: impl FnMut(Slot)) {
        for &target in &self.strong {
            f(target);
        }
        for table in &self.tables {
            for entry in &table.entries {
                f(entry.value);
            }
        }
    }
}

/// A possible reference that compaction must rewrite, reported by
/// [`Heap::dead_reference_sites`] and [`Heap::dangling_reference_sites`].
///
/// Sites are expressed in *old* slot coordinates.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub enum RefSite {
    /// `heap[from].strong[..]` contains a reference at index `index`.
    Strong { from: Slot, index: usize },
    /// `heap[from].tables[table].entries[index].value` is a reference.
    TableValue {
        from: Slot,
        table: usize,
        index: usize,
    },
    /// `heap[from].tables[table].entries[index].key` is a reference.
    TableKey {
        from: Slot,
        table: usize,
        index: usize,
    },
}

/// Errors produced while validating or collecting a heap.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum HeapError {
    /// A root index is outside the heap.
    RootOutOfBounds { slot: Slot, len: usize },
    /// A strong reference, key or value is outside the heap.
    ReferenceOutOfBounds { site: RefSite, len: usize },
}

impl fmt::Display for HeapError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            HeapError::RootOutOfBounds { slot, len } => {
                write!(f, "root slot {slot} is out of bounds for heap of length {len}")
            }
            HeapError::ReferenceOutOfBounds { site, len } => {
                write!(f, "reference {site:?} is out of bounds for heap of length {len}")
            }
        }
    }
}

impl std::error::Error for HeapError {}

/// A slot-addressed heap: `slots[i]` is `None` for a hole.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Heap {
    slots: Vec<Option<Object>>,
    roots: Vec<Slot>,
}

impl Heap {
    pub fn new() -> Self {
        Self::default()
    }

    /// Build a heap from slots, with no roots yet.
    pub fn from_slots<I>(slots: I) -> Self
    where
        I: IntoIterator<Item = Option<Object>>,
    {
        Self {
            slots: slots.into_iter().collect(),
            roots: Vec::new(),
        }
    }

    pub fn len(&self) -> usize {
        self.slots.len()
    }

    pub fn is_empty(&self) -> bool {
        self.slots.is_empty()
    }

    pub fn slot(&self, slot: Slot) -> Option<&Object> {
        self.slots.get(slot).and_then(Option::as_ref)
    }

    /// Alias for [`Heap::slot`].
    pub fn object(&self, slot: Slot) -> Option<&Object> {
        self.slot(slot)
    }

    pub fn is_occupied(&self, slot: Slot) -> bool {
        self.slot(slot).is_some()
    }

    pub fn slots(&self) -> impl Iterator<Item = Option<&Object>> {
        self.slots.iter().map(Option::as_ref)
    }

    pub fn roots(&self) -> &[Slot] {
        &self.roots
    }

    pub fn set_roots<I>(&mut self, roots: I)
    where
        I: IntoIterator<Item = Slot>,
    {
        self.roots = roots.into_iter().collect();
    }

    /// Append an object, returning its slot.
    pub fn push(&mut self, object: Object) -> Slot {
        self.slots.push(Some(object));
        self.slots.len() - 1
    }

    /// Append a hole, returning its slot.
    pub fn push_hole(&mut self) -> Slot {
        self.slots.push(None);
        self.slots.len() - 1
    }

    /// Overwrite a slot, extending the heap with holes if needed.
    pub fn set_slot(&mut self, slot: Slot, object: Option<Object>) {
        if slot >= self.slots.len() {
            self.slots.resize(slot + 1, None);
        }
        self.slots[slot] = object;
    }

    /// Validate every root and every reference against the heap bounds.
    pub fn validate(&self) -> Result<(), HeapError> {
        let len = self.slots.len();
        for &root in &self.roots {
            if root >= len {
                return Err(HeapError::RootOutOfBounds { slot: root, len });
            }
        }
        self.for_each_reference_site(|site, target| {
            if target >= len {
                Err(HeapError::ReferenceOutOfBounds { site, len })
            } else {
                Ok(())
            }
        })
    }

    fn for_each_reference_site(
        &self,
        mut f: impl FnMut(RefSite, Slot) -> Result<(), HeapError>,
    ) -> Result<(), HeapError> {
        for (from, object) in self.slots.iter().enumerate() {
            let Some(object) = object else { continue };
            for (index, &target) in object.strong.iter().enumerate() {
                f(RefSite::Strong { from, index }, target)?;
            }
            for (table_index, table) in object.tables.iter().enumerate() {
                for (index, entry) in table.entries.iter().enumerate() {
                    f(
                        RefSite::TableValue {
                            from,
                            table: table_index,
                            index,
                        },
                        entry.value,
                    )?;
                    f(
                        RefSite::TableKey {
                            from,
                            table: table_index,
                            index,
                        },
                        entry.key,
                    )?;
                }
            }
        }
        Ok(())
    }

    /// Report every reference (strong, key or value) whose target slot is
    /// vacant. Useful for auditing a heap before collection.
    pub fn dangling_reference_sites(&self) -> Vec<RefSite> {
        let mut sites = Vec::new();
        let len = self.slots.len();
        let _ = self.for_each_reference_site(|site, target| {
            if target >= len || self.slots[target].is_none() {
                sites.push(site);
            }
            Ok(())
        });
        sites.sort_unstable();
        sites.dedup();
        sites
    }

    /// Report every strong reference or table *value* held by a *dead* object
    /// that points at a *live* object. Such references are erased by
    /// compaction; the report makes the rewiring boundary explicit.
    ///
    /// Table keys are excluded: a dead holder is erased wholesale, so its keys
    /// neither keep anything alive nor need rewriting.
    pub fn dead_reference_sites(&self, live: &BTreeSet<Slot>) -> Vec<RefSite> {
        let mut sites = Vec::new();
        let _ = self.for_each_reference_site(|site, target| {
            if let RefSite::Strong { from, .. } | RefSite::TableValue { from, .. } = site {
                if !live.contains(&from) && live.contains(&target) {
                    sites.push(site);
                }
            }
            Ok(())
        });
        sites.sort_unstable();
        sites.dedup();
        sites
    }
}
