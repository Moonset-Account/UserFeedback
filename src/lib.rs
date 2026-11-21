//! A small, dependency-free ephemeron-aware compacting garbage collector.
//!
//! The heap is a vector of *slots*. Each slot is either vacant (a hole) or
//! contains an [`Object`]. An object has
//!
//! * `strong`: ordinary strong references to other slots, and
//! * `tables`: weak-key (ephemeron) tables it holds.
//!
//! A table [`Entry`] `(key, value)` is retained only when the object holding
//! the table is alive **and** `key` is alive. In that case `value` becomes
//! alive too; strong references reachable from `value` are followed, which can
//! in turn make keys of other tables alive. The process is iterated to its
//! least fixed point.
//!
//! [`collect`] marks live objects, compacts them into contiguous new slots
//! while preserving the old slot order, rewrites roots, strong references and
//! surviving table entries, and returns the old-slot to new-slot
//! [`relocation`](Collection::relocation) map.
//!
//! # Example
//!
//! ```
//! use ephemeron_compactor::{collect, Entry, Heap, Object, Table};
//!
//! let mut heap = Heap::new();
//! // slot 0: a live holder with one ephemeron entry
//! heap.push(Object::new().with_table(Table::new().with_entry(1, 2)));
//! // slot 1: a key that is kept alive by the root below
//! heap.push(Object::new());
//! // slot 2: the value, alive only through the table entry
//! heap.push(Object::new());
//! // slot 3: a root object strongly referencing the key
//! heap.push(Object::new().with_strong(1));
//! heap.set_roots(vec![0, 3]);
//!
//! let collected = collect(&heap).unwrap();
//! assert_eq!(collected.stats.live_objects, 4);
//! assert_eq!(collected.heap.roots(), &[0, 3]);
//! assert_eq!(
//!     collected.heap.object(0).unwrap().tables[0].entries,
//!     vec![Entry::new(1, 2)],
//! );
//! ```

mod collect;
mod model;

pub use collect::{collect, reference_sites, Collection, Stats};
pub use model::{Entry, Heap, HeapError, Object, RefSite, Slot, Table};
