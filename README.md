# ephemeron-compactor

A dependency-free Rust 1.95.0 library that performs **ephemeron-aware marking
and compaction** on a slot-addressed object heap.

The heap is an array of slots, some of which may be holes. Objects hold ordinary
strong references and one or more *weak-key tables*. Collecting the heap marks
objects to the least fixed point of the ephemeron rule, then compacts the
survivors into contiguous new slots in old-slot order, rewrites every surviving
reference and returns the old→new relocation map.

## Model

| Concept | Type | Meaning |
| --- | --- | --- |
| Slot | `usize` | Index into the input heap. |
| Reference | `Slot` | A strong reference, table key or table value. |
| `Entry { key, value }` | `ephemeron_compactor::Entry` | One weak-key table record. |
| `Table { entries }` | `ephemeron_compactor::Table` | Table held by one object; order is irrelevant. |
| `Object { strong, tables }` | `ephemeron_compactor::Object` | One heap object. |
| `Heap` | `ephemeron_compactor::Heap` | `Vec<Option<Object>>` plus a root list. |

Reachability rules:

1. A root naming an occupied slot makes that object live.
2. A strong reference from a live object makes its target live.
3. An entry `(key -> value)` on a **live holder** makes `value` live **only
   when `key` is live**. A value is not marked before its key is.
4. Values are scanned like any other live object, so strong references inside
   them can make further keys live. Marking iterates until nothing changes.

Rule 3 is what prevents two keys from being kept alive solely by each other's
entry values: neither key is live, so neither value is ever promoted, and the
mutual support never bootstraps.

## Public API

```rust
pub fn collect(heap: &Heap) -> Result<Collection, HeapError>;

pub struct Collection {
    pub heap: Heap,                          // compacted output
    pub relocation: Vec<Option<Slot>>,       // old slot -> new slot
    pub reverse_relocation: Vec<Slot>,       // new slot -> old slot
    pub stats: Stats,
}

pub struct Stats {
    pub slots_before: usize,
    pub holes_before: usize,
    pub live_objects: usize,
    pub reclaimed_objects: usize,
    pub roots_dropped: usize,
    pub entries_dropped: usize,
    pub dangling_references: usize,
}
```

Builders and inspection helpers:

* `Heap::new`, `Heap::from_slots`, `push`, `push_hole`, `set_slot`, `set_roots`,
  `roots`, `len`, `slot`/`object`, `is_occupied`, `slots`.
* `Heap::validate`, `Heap::dangling_reference_sites`, `Heap::dead_reference_sites`.
* `Object::new().with_strong(s).with_strongs(it).with_table(t)`.
* `Table::new().with_entry(k, v).with_entries(it)`, `Table::retain_live_keys`.
* `Collection::relocated(old)`, `Collection::original(new)`.
* `ephemeron_compactor::reference_sites(&Heap)`.

Errors are `HeapError::{RootOutOfBounds, ReferenceOutOfBounds}`; every root,
strong target, key and value is bounds-checked before collection. References to
in-bounds **holes** are not errors — they simply do not survive and are counted
in `Stats::dangling_references`.

Guarantees of `collect`:

* Survivors appear in old-slot order in slots `0..live_objects`; no holes.
* Strong references, table keys and table values in the output are all
  rewritten through `relocation`; references to dead or vacant slots are
  erased.
* Entries whose key did not survive are removed from surviving holders and
  counted in `stats.entries_dropped`.
* Root order and duplicates are preserved; roots naming dead/vacant slots are
  dropped and counted.
* Duplicate `(key, value)` pairs and duplicate strong references are preserved,
  not deduplicated.

## Example

```rust
use ephemeron_compactor::{collect, Entry, Heap, Object, Table};

let mut heap = Heap::new();
heap.push(Object::new().with_table(Table::new().with_entry(1, 2))); // slot 0: holder
heap.push(Object::new());                                           // slot 1: key
heap.push(Object::new());                                           // slot 2: value
heap.push(Object::new().with_strong(1));                            // slot 3: root keeping the key alive
heap.set_roots([0, 3]);

let out = collect(&heap).unwrap();
assert_eq!(out.stats.live_objects, 4);
assert_eq!(out.heap.roots(), &[0, 3]);
assert_eq!(out.heap.object(0).unwrap().tables[0].entries, vec![Entry::new(1, 2)]);
```

## Reproduction

Requires exactly Rust 1.95.0. No dependencies, no network access.

```sh
rustc --version            # must print 1.95.0
cargo test --offline
```

`cargo test --offline` builds the library and runs `tests/basic.rs`,
`tests/ephemeron.rs`, `tests/scenarios.rs` and the crate-level doctest. The
command used for the results recorded here:

```sh
cargo test --offline
```

## Test coverage (actual, verified)

All of the following were executed via `cargo test --offline`; every test
passed (41 tests: 18 + 15 + 7 integration tests plus 1 doctest).

### `tests/basic.rs` — 18 tests

Structure and compaction: empty heap; all-holes heap; hole closing with order
preservation; reference rewriting through relocation; dead-object references to
live objects erased; dead/vacant roots dropped and counted; dangling references
erased and counted; duplicate strong references preserved; rooted vs. dead
cycles; self-reference; relocation helper consistency; out-of-bounds strong
reference, table key, table value and root each rejected; builder slot
extension; duplicate entry pairs preserved; `dead_reference_sites` reporting
dead holders that point at live objects; `Table::retain_live_keys`;
`Object::visit_value_targets` visiting strong refs and values but not keys.

### `tests/ephemeron.rs` — 15 tests

Semantics: value live when holder and key live; value dead when key unrooted;
value dead when holder unrooted; value's strong reference promoting another
table's key; that same chain with a leading hole so all reference kinds are
rewritten; three-link chain propagation; two keys mutually supported only via
each other's values both reclaimed (same table and cross-table variants); weak
table not retaining its key through a back-reference; self-keyed entry; entry
whose value is its own holder; chained holders resolved regardless of slot
order; **feeding the two tables' records in opposite order yields identical
relocation, roots, survivor counts and surviving-entry sets**; holes inside a
table (dead key) and between holders, with surviving entry rewritten through
the relocation map.

### `tests/scenarios.rs` — 7 tests

End-to-end: the full two-table chain with its head root; **withdrawing the root
that named the chain head K1**, which reclaims five objects and all four
entries; idempotent re-collection; a second collection after a root is dropped
in the compacted heap; simultaneous rewriting of strong refs, keys and values;
`stats` accounting across every input slot; and `reverse_relocation` inverting
`relocation` for all survivors.

## Verification scope and limits

* Verified: all of the above tests ran under `cargo test --offline` on Rust
  1.95.0 and passed. Behavior is deterministic; no randomness or timing is
  involved.
* Not verified / not claimed:
  * No performance, concurrency (the crate is single-threaded and not
    `Sync`-oriented), or memory-usage guarantees are measured.
  * No fuzzing or property-testing was run; the input-order invariance and
    mutual-support properties are covered by targeted cases, not a generator.
  * `cargo clippy` was not run in this environment (it required an approval the
    session did not grant); `cargo test` produced no compiler warnings.
  * The collector is a pure function over an in-memory model; it is not wired
    to any real runtime, allocator or language binding.
* No network, accounts, system state or external data were used; all inputs are
  synthetic heaps defined in the tests.
