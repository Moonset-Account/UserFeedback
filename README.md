# Adapter hot-swap: version loading + pinned sessions

A dependency-free Node.js library for hot-swapping one adapter version
underneath a live inference service. A session that has started producing
output keeps the weights it opened with forever; new sessions pick up the
newest published version.

Implemented with the Node.js standard library only (ECMAScript modules,
`node:test`). No packages are installed.

## Requirements

- Node.js **24.15.0** (the version used to develop and verify this code)
- No dependencies, no build step, no network access

## Reproduce

From a fresh, empty workspace:

```sh
node --version          # v24.15.0
node --test 'tests/*.test.mjs'
node demo.mjs           # end-to-end hot-swap walkthrough
```

`npm test` is wired to `node --test` in `package.json` (which auto-discovers
`tests/*.test.mjs`); the explicit glob above is equivalent and is the form
used for the verification record.

## What it guarantees

1. **One version installed at a time.** Exactly one `AdapterVersion` is
   published; a second load cannot start while one is in flight.
2. **Atomic slot admission.** Each version manifest declares its weight
   blocks and the number of slots it needs. `beginLoad` reserves *all*
   declared slots in one step. If they do not fit, the load never starts and
   the published version keeps serving.
3. **All-or-nothing publication.** A version becomes visible to sessions
   only after every declared block has been delivered. Partial loads are
   never published.
4. **Serving continues during a load.** Sessions opened mid-load bind to the
   already-published version.
5. **Failure frees exactly its own resources.** An aborted or invalid load
   releases its own reservation; the published version and its slots are
   untouched.
6. **Late chunks cannot leak into a retry.** Every load attempt has an id.
   Chunks are accepted only for the registry's current attempt; a chunk
   stamped with a dead attempt id is rejected with `StaleChunkError` before
   any state is touched, so it can never complete or corrupt a later retry.
7. **Retired versions are reclaimed on the last pin.** When a new version
   publishes, the old one is retired. Its slots are released only when the
   last session that captured it ends. Until then its frozen weights remain
   readable.
8. **Sessions never mix generations.** A session captures one immutable
   version object at `openSession()` and scores every input from that
   object's frozen integer weights.

### How mixing is made visible

Weights are deterministic integers derived from `(version, block, index)`:
`v * 100 + block * 10 + index`. Local scoring is a per-block integer dot
product:

```
score = Σ_blocks  dot(weights[block], input)
```

The returned breakdown `blocks: [{ blockId, weights, contribution }]` exposes
which weights produced each term. Pure v1 and pure v2 scores differ by ~100
per unit of input; a hypothetical mix of the two generations would fall
between them and show block weights from two different leading digits. The
session-pinning tests and `demo.mjs` use exactly this to show the absence of
mixing.

## Public API

All exports are available from `src/index.mjs`.

### `new AdapterRegistry({ slots })`

Creates a registry with a fixed pool of `slots` weight-chunk slots.
`slots` must be a positive integer.

| Member | Meaning |
| --- | --- |
| `published` | The currently published `AdapterVersion`, or `null`. |
| `publishedVersion` | Numeric version of the published adapter, or `null`. |
| `publishedGeneration` | Publish-generation index (a version number may be republished). |
| `activeAttempt` | The in-flight `LoadAttempt`, or `null`. |
| `pool` | The `SlotPool` (see `capacity`, `used`, `free`). |
| `sessionCount` | Number of open sessions. |
| `retiredVersions` | `[{ version, generation, pins }]` for retired versions still held by pinned sessions. |
| `pinsFor(adapterVersion)` | Number of sessions pinning a given version object. |
| `events` | Read-only event trace (see below). |
| `publishLog()` | `[{ version, generation }]` in publish order. |
| `beginLoad(manifest)` | Admit a load; returns a `LoadAttempt`. Throws `SlotExhaustedError`, `LoadInFlightError`, or a manifest `TypeError`/`Error`. |
| `deliver(attemptId, blockId, weights)` | Deliver a chunk by attempt id. |
| `abortLoad(attemptId, reason?)` | Abort the current attempt; returns `false` for a non-current id. |
| `openSession()` | Open a `Session` pinned to the currently published version. Throws if nothing is published. |

### Version manifest

```js
const manifest = {
  version: 3,        // non-negative integer
  blocks: [0, 2, 5], // non-empty, unique non-negative integer block ids
  slotCount: 3,      // positive integer >= blocks.length
};
```

`beginLoad` validates the manifest, sorts the block ids, and reserves
`slotCount` slots atomically. A manifest declaring fewer slots than it has
blocks is rejected before any slot is taken.

### `LoadAttempt`

Returned by `beginLoad`. Holds the reservation while loading.

| Member | Meaning |
| --- | --- |
| `id` | Attempt id; chunks must carry it. |
| `version`, `manifest`, `blockIds` | Declared load identity (block ids ascending). |
| `status` | `'loading'`, `'published'`, or `'aborted'`. |
| `abortReason` | Reason string when aborted. |
| `dim` | Vector dimension established by the first chunk, or `null`. |
| `receivedBlockIds` | Blocks received so far, ascending. |
| `isComplete` | Whether every declared block has arrived. |
| `deliver(blockId, weights)` | Deliver one chunk (integer weights, consistent dimension). The last chunk publishes. |
| `abort(reason?)` | Abort and release slots. |

Protocol violations (`DuplicateChunkError`, `UnknownBlockError`, wrong
dimension, non-integer weights) abort the attempt and release its slots. A
`StaleChunkError` does not touch the current attempt.

### `Session`

Pinned to one immutable `AdapterVersion` for its whole life.

| Member | Meaning |
| --- | --- |
| `id` | Session id. |
| `version`, `generation` | Pinned numeric version and publish generation. |
| `closed`, `scoreCount` | Status counters. |
| `adapter` | The pinned `AdapterVersion` (frozen). |
| `score(input)` | Returns `{ sessionId, generation, version, score, blocks }`. |
| `end()` | Release the pin; returns `false` if already closed. |

A closed session throws on `score`. Ending a session more than once is a
harmless no-op.

### `AdapterVersion`

Immutable published weights. `version`, `generation`, `blockIds`, `dim`,
`slotCount`, `hasBlock(id)`, `block(id)` (frozen array), and
`score(input)` → `{ version, score, blocks }`.

### Scoring helpers

- `dot(weights, input)` — integer-weighted dot product; throws `RangeError`
  on length mismatch.
- `scoreVersion(blocks, blockIds, input)` — sums per-block dot products and
  returns `{ score, blocks }`.

### `SlotPool`

Fixed-capacity reservation table: `capacity`, `used`, `free`,
`isReserved(owner)`, `reserve(owner, count)` (atomic),
`release(owner)`, `transfer(from, to)`.

### Errors

`SlotExhaustedError` (`needed`, `free`), `LoadInFlightError`,
`StaleChunkError`, `DuplicateChunkError`, `UnknownBlockError`.

### Events

`registry.events` records, in order: `load-started`, `load-aborted`,
`version-published`, `version-retired`, `version-reclaimed`, `session-ended`.

## Layout

```
src/
  index.mjs      public exports
  registry.mjs   AdapterRegistry + Session (lifecycle, pins, reclaim)
  versions.mjs   AdapterVersion (immutable) + LoadAttempt
  slots.mjs      SlotPool (atomic reservations)
  manifest.mjs   manifest validation + slot accounting
  scoring.mjs    integer dot product + per-block scoring
  errors.mjs     error classes + input validation
tests/
  scoring.test.mjs   dot/score arithmetic and input validation
  slots.test.mjs     atomic reservations, transfer, manifest rules
  registry.test.mjs  load/publish/abort/stale-chunk/slot-reclaim lifecycle
  sessions.test.mjs  cross-publish pinning, interleaving, async delivery
  property.test.mjs  seeded randomized interleaving invariants
  support/fixtures.mjs
demo.mjs           end-to-end hot-swap walkthrough
```

## Test coverage by scenario

**Normal**
- Full delivery publishes atomically; sessions score correct values.
- Blocks delivered out of order still publish in ascending block order.
- Single-block / single-dimension versions.
- A version declaring more slots than blocks.

**Boundary**
- Empty/duplicate/negative manifest fields; slot count below block count.
- Reservation shortfall reports `needed`/`free`; capacity and count bounds.
- Scoring before any publish; closed sessions; dimension mismatches;
  non-finite inputs; non-integer weights.
- Zero-session replacement reclaims immediately; double `end()` is a no-op.

**Interleaved**
- One session scores twice across a publish → identical result, old version.
- A session opened mid-load pins the pre-load version; new sessions after the
  publish get the new version.
- Three generations coexist (v1 pinned, v2 pinned, v3 current) with correct
  per-generation scores and slot accounting.
- A late chunk from an aborted attempt is rejected and cannot complete a
  retry; after the retry loads cleanly, only its own weights are published.
- Retired versions stay pinned until the last session ends, then slots are
  reclaimed; the event trace records the full sequence.
- Async chunk arrival with interleaved scoring and `setImmediate`.
- Seeded randomized run (60 seeds × 200 steps) mixing starts, partial
  deliveries, aborts, replayed stale chunks, session opens, scoring and
  ends, asserting after every step that slots are conserved
  (`used + free === slots`), that no load publishes partially, that stale
  chunks never publish, and that every open session's scored weights carry
  its pinned version's leading digit.

**Not covered (by design)**
- No tests were run against a real network, model server, GPU, or external
  storage. Everything is local, in-memory, synthetic integer data.
- Concurrency here means single-threaded async interleaving, not worker
  threads or multi-process coordination.
- No persistence, eviction policy beyond pin-based reclamation, or metrics
  export.

## Verification record

Command actually run:

```sh
node --test 'tests/*.test.mjs'
```

Result: **47 tests, 47 pass, 0 fail** on Node.js v24.15.0
(`duration_ms` ≈ 207 ms). `node demo.mjs` also ran end to end; its output
showed session A scoring `422` before and after the swap while session B
scored `822`, with per-block weights `100,101 / 110,111` for A and
`200,201 / 210,211` for B.

The demo prints a live walkthrough derived from the actual library; it is
not a canned expected-output fixture. Tests assert values computed
independently in `tests/support/fixtures.mjs`, not recorded output.
