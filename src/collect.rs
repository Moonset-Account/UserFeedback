//! Marking to a least fixed point and compaction.

use crate::model::{Entry, Heap, HeapError, Object, RefSite, Slot, Table};

/// Summary counters describing one collection.
///
/// All counts refer to the *input* heap unless stated otherwise.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct Stats {
    /// Number of slots in the input heap, holes included.
    pub slots_before: usize,
    /// Number of vacant slots in the input heap.
    pub holes_before: usize,
    /// Number of occupied objects reachable after marking.
    pub live_objects: usize,
    /// Number of occupied objects that were not reachable.
    pub reclaimed_objects: usize,
    /// Number of roots dropped because they named a dead or vacant slot.
    pub roots_dropped: usize,
    /// Number of table entries dropped from *surviving* holders because their
    /// key was dead or their value slot was vacant.
    pub entries_dropped: usize,
    /// Number of references held by the input heap whose target slot was
    /// vacant. Such references are erased by compaction.
    pub dangling_references: usize,
}

/// Result of [`collect`].
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Collection {
    /// The compacted heap: live objects only, in old-slot order, occupying
    /// slots `0..live_objects`.
    pub heap: Heap,
    /// Old slot -> new slot. `None` marks a slot that did not survive.
    pub relocation: Vec<Option<Slot>>,
    /// New slot -> old slot.
    pub reverse_relocation: Vec<Slot>,
    /// Input-heap statistics.
    pub stats: Stats,
}

impl Collection {
    /// Map an old slot to its new location.
    pub fn relocated(&self, old: Slot) -> Option<Slot> {
        self.relocation.get(old).copied().flatten()
    }

    /// Map a new slot back to its old location.
    pub fn original(&self, new: Slot) -> Option<Slot> {
        self.reverse_relocation.get(new).copied()
    }
}

/// Collect garbage and compact the heap.
///
/// Marking starts from the roots. A strong reference from a live object makes
/// its target live. A table entry on a live holder makes its value live only
/// when the entry's key is live; values are then scanned like any other live
/// object, so a chain of tables is followed to its fixed point. Objects whose
/// keys are supported only by each other's entry values are therefore never
/// marked.
///
/// Compaction preserves the relative order of surviving objects, drops every
/// reference that points at a dead or vacant slot, drops every table entry
/// whose key did not survive, and rewrites all remaining references through
/// the relocation map.
///
/// # Errors
///
/// Returns [`HeapError`] if a root or any reference (strong target, key or
/// value) lies outside the heap. References to *vacant* in-bounds slots are
/// not an error; they simply do not survive.
pub fn collect(heap: &Heap) -> Result<Collection, HeapError> {
    heap.validate()?;

    let slots_before = heap.len();
    let holes_before = (0..slots_before).filter(|&s| !heap.is_occupied(s)).count();
    let live = mark(heap);

    let live_objects = live.iter().filter(|&&l| l).count();
    let reclaimed_objects = slots_before - holes_before - live_objects;

    // Old slot -> new slot, in old-slot order.
    let mut relocation = vec![None; slots_before];
    let mut reverse_relocation = Vec::with_capacity(live_objects);
    for old in 0..slots_before {
        if live[old] {
            relocation[old] = Some(reverse_relocation.len());
            reverse_relocation.push(old);
        }
    }

    let new_slot = |old: Slot| -> Option<Slot> { relocation.get(old).copied().flatten() };

    let mut entries_dropped = 0usize;
    let mut new_slots: Vec<Option<Object>> = Vec::with_capacity(live_objects);
    for &old in &reverse_relocation {
        let object = heap
            .slot(old)
            .expect("marked objects are occupied by construction");

        // Strong references: keep live targets only, mapped to new slots.
        let strong: Vec<Slot> = object.strong.iter().filter_map(|&t| new_slot(t)).collect();

        let mut tables = Vec::with_capacity(object.tables.len());
        for table in &object.tables {
            let mut entries = Vec::with_capacity(table.entries.len());
            for entry in &table.entries {
                match (new_slot(entry.key), new_slot(entry.value)) {
                    (Some(key), Some(value)) => entries.push(Entry::new(key, value)),
                    _ => entries_dropped += 1,
                }
            }
            tables.push(Table { entries });
        }

        new_slots.push(Some(Object { strong, tables }));
    }

    let mut roots = Vec::with_capacity(heap.roots().len());
    for &root in heap.roots() {
        if let Some(new_root) = new_slot(root) {
            roots.push(new_root);
        }
    }
    let roots_dropped = heap.roots().len() - roots.len();

    let mut new_heap = Heap::from_slots(new_slots);
    new_heap.set_roots(roots);

    Ok(Collection {
        heap: new_heap,
        relocation,
        reverse_relocation,
        stats: Stats {
            slots_before,
            holes_before,
            live_objects,
            reclaimed_objects,
            roots_dropped,
            entries_dropped,
            dangling_references: heap.dangling_reference_sites().len(),
        },
    })
}

/// Compute the live set to the least fixed point of the ephemeron rules.
///
/// The traversal is a single worklist over object births. Whenever an object
/// becomes live it is both a possible *holder* (its table entries whose key is
/// already live promote their values) and a possible *key* (entries in already
/// live holders promote their values). Either arrival order is therefore
/// handled, and no iteration to a global fixed point is required.
fn mark(heap: &Heap) -> Vec<bool> {
    let len = heap.len();

    // For every key slot, the (holder, value) pairs that depend on it.
    let mut dependents: Vec<Vec<(Slot, Slot)>> = vec![Vec::new(); len];
    for holder in 0..len {
        let Some(object) = heap.slot(holder) else {
            continue;
        };
        for table in &object.tables {
            for entry in &table.entries {
                if entry.key < len {
                    dependents[entry.key].push((holder, entry.value));
                }
            }
        }
    }

    let mut live = vec![false; len];
    let mark_slot = |slot: Slot, live: &mut Vec<bool>, stack: &mut Vec<Slot>| {
        if slot < live.len() && heap.is_occupied(slot) && !live[slot] {
            live[slot] = true;
            stack.push(slot);
        }
    };

    let mut stack: Vec<Slot> = Vec::new();
    for &root in heap.roots() {
        mark_slot(root, &mut live, &mut stack);
    }

    while let Some(slot) = stack.pop() {
        let Some(object) = heap.slot(slot) else {
            continue;
        };

        // Ordinary strong references.
        for &target in &object.strong {
            mark_slot(target, &mut live, &mut stack);
        }

        // This object just became live, so it is now a live table holder.
        // Promote values whose key is already live; entries whose key becomes
        // live later are promoted from the key side below.
        for table in &object.tables {
            for entry in &table.entries {
                if live.get(entry.key).copied().unwrap_or(false) {
                    mark_slot(entry.value, &mut live, &mut stack);
                }
            }
        }

        // This object just became live, so it may be the missing key for a
        // table held by an already live object.
        for &(holder, value) in &dependents[slot] {
            if live[holder] {
                mark_slot(value, &mut live, &mut stack);
            }
        }
    }

    live
}

/// Every reference site in a collected heap, in new-slot coordinates. Mostly
/// useful in tests and diagnostics.
pub fn reference_sites(heap: &Heap) -> Vec<RefSite> {
    let mut sites = Vec::new();
    for slot in 0..heap.len() {
        let Some(object) = heap.slot(slot) else {
            continue;
        };
        for index in 0..object.strong.len() {
            sites.push(RefSite::Strong { from: slot, index });
        }
        for (table, t) in object.tables.iter().enumerate() {
            for index in 0..t.entries.len() {
                sites.push(RefSite::TableValue {
                    from: slot,
                    table,
                    index,
                });
                sites.push(RefSite::TableKey {
                    from: slot,
                    table,
                    index,
                });
            }
        }
    }
    sites
}
