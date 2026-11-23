# Dual-version backend store

A small, Kubernetes-style module that exposes **one backend resource through two
API versions** sharing a single canonical state:

| View | Addresses items by | Sees | Writes |
| --- | --- | --- | --- |
| **spec** (canonical) | immutable backend **id** | `name`, `address`, `weight`, `drain` + per-field versions | any field, conditionally |
| **legacy** | mutable **name**, resolved through a per-item **credential** | `name`, `address` | `name`/`address` only |

Both views write through the same transaction boundary, so every write is
atomic and immediately visible to the other version.

* standard library only, Python 3.11.6
* no network, no external state: everything lives in one in-memory store

---

## Public API

All public names are importable from the `dualversion` package.

### Errors

```python
from dualversion import DualVersionError, ValidationError, WriteConflict
```

* `ValidationError(DualVersionError, ValueError)` — malformed payload or a field
  constraint violation. Raised **before** any mutation, so it is always atomic.
* `WriteConflict(DualVersionError)` — an optimistic-concurrency check failed and
  **nothing was written**. Attributes:
  * `reason` — `"field_modified"`, `"target_deleted"`,
    `"idempotency_key_reuse"`, or `"multiple_conflicts"` (mixed batch);
  * `targets` — tuple of mappings, each with `id` and `reason`, plus `field` for
    field conflicts;
  * `target_ids` — sorted, de-duplicated conflicting backend ids;
  * `resource_version` — the store version at rejection time;
  * `to_dict()` — JSON-ready form.

### `BackendStore(secret=None, *, history_limit=500)`

`secret` is an optional ≥ 16-byte key used to sign and encrypt legacy
credentials. Supply a stable secret if credentials must survive a restart or be
shared between instances; otherwise a random key is generated. `history_limit`
caps the mutation log returned by `history()`.

#### Canonical (spec) view

| Call | Returns |
| --- | --- |
| `spec_get()` | `{"resourceVersion", "items": [...], "order": [id, ...]}` |
| `spec_get(backend_id)` | same shape for one item; `KeyError` if unknown |
| `spec_add(name, address, weight=100, drain=False, *, idempotency_key=None)` | `{"operation", "status", "id", "item", "resourceVersion", "replayed"}` |
| `spec_write(backend_id, changes, expected_versions=None, *, force=False, idempotency_key=None)` | `{"status": "updated"/"unchanged", "changed": [...], "item", ...}` |
| `spec_delete(backend_id, expected_versions=None, *, idempotency_key=None)` | `{"status": "deleted", "item": <last snapshot>, ...}` |

Each `item` is `{id, name, address, weight, drain, versions: {name, address, weight, drain}, createdResourceVersion}`.
`spec_write`/`spec_delete` require an `expected_versions` entry for every field
whose value actually changes (unless `force=True`); a stale entry raises
`WriteConflict`.

#### Legacy view

| Call | Returns |
| --- | --- |
| `legacy_get()` | `{"resourceVersion", "items": [{name, address, credential}, ...]}` |
| `legacy_write(credential, changes, *, idempotency_key=None)` | `{"status": "renamed"/"updated"/"unchanged", "name", "address", "credential", "changed", "previousName"?, ...}` |
| `legacy_delete(credential, *, idempotency_key=None)` | `{"status": "deleted", "name", "credential", ...}` |
| `legacy_commit(items, deleted_credentials=(), *, replace=True, idempotency_key=None)` | `{"status", "changed", "orderChanged", "items", "deleted", "summary", "resourceVersion", "replayed"}` |

`legacy_commit` items are `{name, address, credential?}`: a `credential` marks
an existing backend (rename and/or address edit), an item without one creates a
new backend with default `weight=100, drain=False`. `deleted_credentials` lists
explicit deletions. `replace=True` treats the submission as the whole list
(fixes order, prunes unlisted backends); `replace=False` merges (keeps existing
order and unlisted backends, appends creations). `summary` counts
`created/renamed/updated/unchanged/deletedExplicit/deletedPruned`.

#### Introspection

* `history(since_resource_version=None)` — applied mutations oldest-first;
  replays are not recorded.
* `resource_version` — current store version.

### Credentials

A legacy item's `credential` is an opaque `lv1.<blob>.<mac>` token. It is
deterministic for a given store and encodes, *encrypted and authenticated*:

1. the immutable canonical backend **id** — so identity survives renames and a
   name swap cannot move `weight`/`drain` between backends;
2. the **versions** of `name` and `address` at read time — so a legacy writer
   can tell whether a visible field changed underneath it, while still merging
   edits to fields it never saw.

Callers must treat it as a blob and hand it back unchanged. Tampered, truncated
or foreign-secret tokens raise `ValidationError`.

### Idempotency

Every mutating call accepts `idempotency_key`. Replaying the same key with the
same payload returns the original result with `"replayed": true` and does not
apply anything twice (this is how a replayed delete of an already-deleted
target still reports success). Reusing a key with a different payload raises
`WriteConflict(reason="idempotency_key_reuse")`. A rejected request never
poisons its key: a later corrected request with that key can still succeed.

---

## Semantics worth knowing

* **Identity vs. name.** Spec writes and deletes target ids. Legacy writes and
  deletes target the credential, resolved to an id. Renaming therefore never
  retargets a legacy operation, and swapping two names leaves canonical
  attributes attached to their original ids.
* **Field-level concurrency.** Only fields whose value actually changes are
  version-checked. A spec write to `weight` does not block a concurrent legacy
  write to `address`, and vice versa; a legacy write is unaffected by a
  concurrent `weight`/`drain` change (fields it cannot see).
* **Conflict on the same visible field.** If two writers change the same visible
  field from the same observation, the second raises `WriteConflict` with
  `reason="field_modified"`, the affected `field`, and the target `id`.
  Conflict detection is value-based: writing the value that already holds is a
  no-op (`status="unchanged"`), never a conflict, so replaying an
  already-applied request is safe.
* **Deleted targets.** A write or delete against a vanished backend raises
  `WriteConflict(reason="target_deleted")` with that id. The exception is an
  idempotent replay of the very request that deleted it.
* **Batch atomicity.** `legacy_commit` validates the whole payload and every
  concurrency check before mutating. One stale item, missing target, repeated
  credential, or duplicate final name means the entire batch is a no-op and
  `resourceVersion` does not advance.

---

## Reproduction

Requirements: Python 3.11.6, standard library only. No installation step; run
from the workspace root.

```bash
# Full test suite (discovered from the workspace root)
python3 -m unittest discover -s tests -t . -v

# Equivalent, using default discovery
python3 -m unittest -v

# Live walkthrough (prints real API output, nothing pre-recorded)
python3 -m dualversion.demo
```

`tests/` is a package (`tests/__init__.py`) so both invocation styles work and
relative imports resolve.

---

## Verification scope

Test files and what each covers:

| File | Coverage |
| --- | --- |
| `tests/test_validation.py` | field constraints, unknown fields, legacy write restrictions, expected-version validation, commit payload validation, credential format/tamper/foreign-secret, idempotency-key validation |
| `tests/test_spec_view.py` | add/read/write/delete, per-field versions, conditional conflicts, multi-field and partial conflicts, force, snapshot isolation |
| `tests/test_legacy_view.py` | legacy visibility, opaque/deterministic credentials, rename keeps identity & attributes, address edits, stale visible writes, invisible-field merge, rename-safe delete, target-deleted |
| `tests/test_identity_and_commit.py` | name swaps keep weights on ids, swap via list-only client, swap then stale credential, replace vs. merge commit, explicit delete, pruning, order rewrite, commit atomicity & mixed conflicts, repeated-credential rejection |
| `tests/test_concurrency_interleaving.py` | spec/spec, spec/legacy, legacy/legacy races on the same and different fields, delete-during-edit, threaded single-winner races, batch atomicity and `resourceVersion` stability on failure |
| `tests/test_idempotency_roundtrip.py` | replay of add/write/delete/commit, key reuse with different payload, failed-request key reuse, spec→legacy→spec and legacy→spec→legacy round trips, history ordering |
| `tests/test_rounds.py` | eight-round read/write/replay scenario asserting per-item preserved / overwritten / deleted outcomes, mixed-conflict batch rollback, and no-op replay behaviour |
| `tests/test_boundaries.py` | empty store reads/commits, empty replace vs. merge, weight/name/address limits, trimming, unicode names, delete-then-recreate id change, history limit, shared vs. per-instance secrets |

Last verified run (Python 3.11.6):

```
$ python3 -m unittest -q
Ran 115 tests in 0.020s
OK
```

Not covered / explicitly out of scope: durable persistence, multi-process
coordination, HTTP/JSON transport and authentication, and performance at scale.
Correctness under real parallel load is demonstrated by the threaded
single-winner tests, not by a load test.
