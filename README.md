# Version object lifecycle module

An in-memory, deterministic model of a versioned object store.  Each object key
owns an ordered list of versions; a version may be a normal data record or a
delete marker, may carry a retention deadline and independent holds, and (for
data records) may be archived and temporarily made readable through an
archive read lease.

Everything is stdlib-only and pure Python 3.11.  All time-sensitive calls take
an explicit `now`, so retention boundaries, lease boundaries and
plan/execute interleavings are exactly reproducible.

## Rules implemented

| Rule | Behaviour |
| --- | --- |
| Ordered versions | Records get a monotonically increasing per-key `sequence`. Order never changes. |
| Default read | Resolves against the newest record only. Never falls back. |
| Delete marker | Newest marker makes the default read `DELETED` and shadows every older version. Version-ID reads still resolve older records. |
| Archive | An archived data record reads `ARCHIVED` unless it has an active read lease, in which case it reads `VISIBLE` for the lease window only. `archived` does **not** silently fall back to an older version. |
| Retention | A version is removable only when `now >= retain_until` (or no deadline). |
| Holds | Independent hold IDs; **all** must be released. |
| Read lease | A lease active at `now` blocks removal. |
| Delete-marker guard | A delete marker is retained past its deadline while clearing it would make an older **data** version the newest record (and therefore the default read result). A marker that is not newest, a marker with only older markers behind it, or a lone marker may be cleared: none of those exposes a data version. |
| Expiry scan | Produces a deterministic, batched plan of currently destroyable versions with stable item IDs. |
| Execution | Re-adjudicates every item against **live** state; nothing is trusted from the plan snapshot. |
| Receipts | One immutable receipt per plan item, replayable by item ID; executing the same item again replays rather than re-removes. |

### Boundary conventions

* Retention: due at exactly `now == retain_until` (inclusive).
* Lease: active for `granted_at <= now < expires_at`; inactive at the expiry
  instant.
* Item IDs: `<plan_id>#item-<4-digit index>`; batch IDs:
  `<plan_id>#batch-<4-digit index>`.  Item order is `(key, sequence)`, batches
  are contiguous slices of that order.
* Blocker priority when several rules apply: `retention_not_reached` ->
  `held` -> `lease_active` -> `delete_marker_guard`.

## Public API

Package `version_lifecycle`:

```python
from version_lifecycle import VersionedStore

store = VersionedStore()

# mutation
store.put_version(key, version_id, *, retain_until=None, holds=None,
                  archived=False, lease=None) -> VersionRecord
store.put_delete_marker(key, version_id, *, retain_until=None, holds=None)
store.archive(key, version_id) / store.unarchive(key, version_id)
store.set_retention(key, version_id, retain_until)   # None clears it
store.add_hold(key, version_id, hold_id)
store.release_hold(key, version_id, hold_id)
store.grant_lease(key, version_id, lease_id, *, granted_at, expires_at)
store.revoke_lease(key, version_id)
store.remove_version(key, version_id, *, now)       # direct, raises if blocked

# queries
store.read(key, *, now, version_id=None) -> ReadResult      # .state, .reason
store.versions(key) -> tuple[VersionRecord, ...]            # oldest first
store.version(key, version_id) -> VersionRecord | None
store.get_version(key, version_id) -> VersionRecord
store.keys() / store.has_key(key)
store.removal_blocker(key, version_id, *, now) -> RetentionReason | None
store.is_destroyable(key, version_id, *, now) -> bool
store.destroyable(*, now) -> DestroyableReport               # .by_key, .version_ids
store.destroyable_version_ids(key, *, now) -> tuple[str, ...]
store.snapshot(key, *, now) -> KeySnapshot                   # read + destroyable

# expiry
store.scan_expiry(*, now, batch_size=100, keys=None) -> ExpiryPlan
store.plan(plan_id) -> ExpiryPlan
store.execute_plan(plan_or_id, *, now, batch_ids=None) -> ExecutionReport
store.replay(plan_or_id, *, batch_ids=None) -> ExecutionReport   # no mutation
store.receipt(item_id) / store.get_receipt(item_id) / store.replay_item(item_id)
store.receipt_log()
```

Value objects (all frozen dataclasses): `VersionRecord`, `ArchiveLease`,
`ReadResult`, `PlanItem`, `ExpiryBatch`, `ExpiryPlan`, `ExecutionReceipt`,
`ExecutionReport`, `KeySnapshot`, `DestroyableReport`.

Enums: `VersionKind`, `ReadState` (`VISIBLE`, `DELETED`, `ARCHIVED`, `MISSING`),
`ReceiptStatus` (`REMOVED`, `RETAINED`, `STALE_VERSION`, `MISSING_VERSION`,
`MISSING_KEY`), `RetentionReason`.

Errors: `LifecycleError(ValueError)` with subclasses `UnknownKeyError`,
`UnknownVersionError`, `UnknownPlanError`, `UnknownReceiptError`,
`RetentionBlockedError` (carries `.key`, `.version_id`, `.reason`).

Receipt semantics:

* `REMOVED` - re-checked and removed now.
* `RETAINED` - re-checked and now blocked; `reason` is the exact
  `RetentionReason` value.
* `STALE_VERSION` - same version ID now has a different sequence (removed and
  recreated); nothing is deleted.
* `MISSING_VERSION` / `MISSING_KEY` - target already gone.
* Replaying an item ID returns the original receipt unchanged
  (`ExecutionReport.removed_now_count == 0`).

## Reproduce

Requires Python 3.11 (developed and verified on 3.11.6).  No third-party
packages, no installation step.

```bash
cd <workspace root>

# full suite, discovered from the workspace root
python3 -m unittest discover -s tests -t . -v
# same tests via default discovery
python3 -m unittest discover -v

# readable state-transition trace for the headline scenario
python3 -m examples.timeline_demo
```

Last verified run: `python3 -m unittest discover -s tests -t . -v` -> 90 tests,
`OK` (Python 3.11.6, macOS).  The `-v` line count and the demo output in the
task report come from that same run; timings printed there are the unittest
elapsed time only, not a benchmark.

## Verification scope

`tests/` (unittest, discovered from the workspace root):

| File | Covers |
| --- | --- |
| `test_reads.py` | default read, newest-wins, delete-marker shadowing, version-ID reads, archive/lease visibility boundaries, lease validity rules. |
| `test_retention.py` | retention boundary (inclusive), holds (single/multiple/release), lease blocking, blocker priority, delete-marker guard (expired marker, archived older record, only-record-left, non-newest marker, marker shadowed by another marker), destroyable reports, direct removal. |
| `test_expiry.py` | plan contents/order/batching/item IDs, batch selection and retry, live re-adjudication after new hold / retention extension / new lease / removal / key removal / version recreation, frozen retained receipts, idempotent replay by item ID, receipt lookup, stacked-marker interleavings. |
| `test_scenarios.py` | the full required timeline (archived restore lease + retention-held old version + expired delete marker on top) with visibility and destroyable list checked after every state transition; plan/execute interleavings; reversed batch order; concurrent batch execution from 8 threads. |
| `test_api.py` | argument validation (non-finite times, empty IDs, duplicate holds), error types, mutation invariants (archive/unarchive/revoke/retention on marker), ordering guarantees. |

Not covered / out of scope:

* No persistence, durability, on-disk format, or crash recovery - the store is
  in memory only and rebuilt per test.
* No wall-clock or scheduler: every operation takes `now` explicitly.
* Concurrency is only in-process threads sharing one store (the lock test); no
  multi-process or network coordination is modelled or tested.
* No real cloud object store, credentials, or network access is used anywhere.
* Timings/throughput are not measured; there are no performance assertions.
