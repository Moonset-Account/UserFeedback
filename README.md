# config-center-kernel

An in-memory configuration-center kernel for prefix subscriptions. It provides:

- **One consistent snapshot** of every key under a prefix at a single revision.
- **Transaction-by-transaction deltas** afterwards, where each delivered
  transaction contains *all* of its matching changes at once (never split across
  batches), including explicit delete markers.
- **A pause window** between taking the snapshot and pulling the first batch of
  changes, so a client can take as long as it needs to apply the snapshot.
- **Cursor-based reconnects**: the client brings back the last global revision it
  consumed; the subscription resumes from there even if intervening revisions only
  touched other prefixes.
- **A reset path**: when the requested history has already been trimmed, the
  subscription emits a reset marker together with a full snapshot at one and the
  same revision, then continues incrementally from there.

No dependencies. Node.js 20+ (uses `structuredClone`, `node:test`).

## Quick start

```js
import { createConfigCenter } from './src/index.js';

const center = createConfigCenter({ maxHistory: 256 });

center.commit([
  { key: 'app/db/host', value: 'db-1' },
  { key: 'app/db/port', value: 5432 },
  { key: 'app/db/user', value: 'svc' },
]);

const sub = center.subscribe({ prefix: 'app/db' });

// 1. Snapshot — revision 1, consistent, already materialised.
const snapshot = sub.snapshot();

// ... the client may pause here for arbitrarily long ...

// 2. Another writer commits while the snapshot is in flight.
center.commit([{ key: 'app/db/host', value: 'db-2' }]);
center.commit([
  { key: 'app/db/host', value: 'db-3' },
  { key: 'app/db/user', deleted: true },
]);

// 3. First incremental batch: everything after snapshot.revision.
const batch = sub.poll();
// {
//   type: 'changes',
//   since: 1,
//   revision: 3,
//   transactions: [
//     { revision: 2, changes: [{ key: 'app/db/host', deleted: false, value: 'db-2' }] },
//     { revision: 3, changes: [
//         { key: 'app/db/host', deleted: false, value: 'db-3' },
//         { key: 'app/db/user', deleted: true },
//       ] },
//   ],
// }
```

## API

### `createConfigCenter(options?)`

| option       | default   | meaning                                                              |
| ------------ | --------- | -------------------------------------------------------------------- |
| `maxHistory` | `1024`    | number of transactions retained for incremental reads; `Infinity` keeps all |
| `separator`  | `'/'`     | path separator for prefix matching; `''` means raw string prefix      |
| `clone`      | `structuredClone` | value cloner used on every ingress/egress                    |

Returned center:

| member                       | description                                                         |
| ---------------------------- | ------------------------------------------------------------------- |
| `revision`                   | highest committed global revision (0 before the first commit)        |
| `trimmedThrough`             | highest revision whose transaction can no longer be replayed         |
| `historySize`                | number of retained transactions                                      |
| `separator`                  | normalised separator                                                 |
| `commit(changes)`            | atomically apply a transaction, returns `{ revision, changes }`       |
| `snapshot(prefix)`           | `{ revision, prefix, entries: [{ key, value }] }`                    |
| `subscribe(options)`         | create a prefix subscription (see below)                              |

### Commits

A commit is an array of writes. It consumes exactly **one** global revision, no
matter how many keys it touches.

```js
center.commit([
  { key: 'app/a', value: 1 },
  { key: 'app/b', deleted: true },
]);
```

- Every write is recorded as intent, even if it does not change the current value
  or deletes a key that is not present. This keeps the log faithful to what the
  writer asked for.
- A key repeated inside one transaction collapses to its final write; a
  transaction is atomic and intermediate states are not observable.
- Invalid payloads (missing `value` without `deleted: true`, both supplied, empty
  or non-string key) reject the whole transaction and leave state untouched.

### `center.subscribe({ prefix, since? })`

- `prefix` is normalised by dropping trailing separators. An empty prefix matches
  every key.
- `since` is the last global revision the client consumed. Omit it for a fresh
  subscriber.

The subscription exposes:

| member      | description                                                             |
| ----------- | ----------------------------------------------------------------------- |
| `snapshot()`| consistent snapshot **and** sets the cursor to its revision; only valid on a fresh subscription that has not polled yet |
| `poll()`    | next batch starting after the cursor; advances the cursor to `revision`  |
| `cursor`    | last consumed revision, or `null` before the first read                  |
| `close()`   | makes further reads fail                                                 |

#### Snapshot-then-pause

`snapshot()` does not depend on transient state: it materialises a fresh array at
the current head, so it stays valid while later commits happen. That is what makes
the following sequence safe:

1. `snapshot()` → base revision `R0`.
2. Arbitrary delay; other writers commit.
3. `poll()` → transactions `R0+1 … head`.

A client that only has the revision (for example one reconnecting) does the same
without step 1, by passing `since: R0` to `subscribe()`.

#### Poll result shapes

```js
// First read on a fresh subscription (no `since`).
{ type: 'snapshot', reason: 'initial', revision, prefix, snapshot, transactions: [] }

// History needed by the cursor was trimmed, or the cursor is ahead of the head.
{ type: 'reset', reason: 'history-trimmed' | 'unknown-revision',
  revision, since, prefix, snapshot, transactions: [] }

// Incremental batch (possibly empty when nothing matched).
{ type: 'changes', reason: null, revision, since, prefix, snapshot: null,
  transactions: [{ revision, changes }] }
```

Guarantees:

- `snapshot.revision === revision` for `snapshot` and `reset` results — the reset
  marker and the full snapshot describe the same commit.
- Every `{ revision, changes }` entry holds a whole transaction. All matching
  changes of that transaction are present; a multi-key commit is never split. If a
  transaction touched no key under the prefix, it is omitted from `transactions`
  but its revision still advances the cursor.
- Deletions surface as `{ key, deleted: true }`; writes as
  `{ key, deleted: false, value }`.
- The cursor always moves to `revision`, even for an empty `changes` batch. This is
  how a subscriber that watched only `app` keeps moving while `other` churns.

#### Reconnect contract

Persist `batch.revision` after applying a batch. On reconnect:

```js
const sub = center.subscribe({ prefix: 'app', since: savedRevision });
const next = sub.poll();
if (next.type === 'reset') {
  // Discard local state, install next.snapshot at next.revision, continue.
} else {
  // Apply next.transactions in order.
}
```

If the saved revision is older than `trimmedThrough`, the result is a `reset`
carrying a full snapshot at the current revision. If it is ahead of the current
head (for example the client talks to a restored/rolled-back center), the result
is also a `reset`, so a cursor can never silently point into the future.

#### Worked example: two successors for one key

With history still intact, a client that read a snapshot and then reconnects can
always separate the initial value from the changes that followed:

```js
center.commit([{ key: 'app/k', value: 'v0' }]);          // revision 1
const snap = center.snapshot('app');                     // v0 @ 1

center.commit([{ key: 'app/k', value: 'v1' }]);          // revision 2
center.commit([{ key: 'app/k', value: 'v2' }]);          // revision 3

const sub = center.subscribe({ prefix: 'app', since: snap.revision });
const batch = sub.poll();
// transactions: [{ revision: 2, changes: [{ key: 'app/k', value: 'v1' }] },
//               { revision: 3, changes: [{ key: 'app/k', value: 'v2' }] }]
```

Replaying `snap` followed by both transactions yields `v2` while still showing
`v0 → v1 → v2` as distinct steps.

## Design notes

- **Global revisions, prefix filtering later.** A commit occupies one revision in
  a single global stream. Subscriptions filter by prefix at read time, so cursors
  are meaningful across prefixes and a quiet prefix never blocks a cursor.
- **Snapshot is the boundary.** Snapshots are rebuilt from the live map and read
  `headRevision` at the same synchronous step, so the snapshot and its revision
  are always consistent with each other.
- **Values are cloned on the way in and out.** Callers cannot mutate stored state
  through a retained reference, and a snapshot handed out earlier never observes
  later commits.
- **Trim is a single watermark.** `trimmedThrough` only ever moves forward; a
  cursor strictly below it cannot be replayed, which is exactly the condition for
  a reset (`cursor === trimmedThrough` is still replayable).
- **Atomic commits.** Values are cloned before any mutation, so a failing clone
  cannot leave a half-applied transaction.

## Tests

`test/subscription.test.js` covers snapshot consistency, pause-then-catch-up,
multi-key atomic delivery, delete visibility, cross-prefix cursor advancement,
reset on trimming, the boundary revision, repeated keys, value isolation, prefix
semantics, and validation.

Run with:

```sh
npm test   # node --test
```

> **Not run.** These tests were authored as part of this source-only delivery and
> have **not** been executed. No build, test run, or verification was performed,
> and no results are claimed.
