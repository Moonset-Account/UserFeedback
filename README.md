# Random byte provider (injected source, per-generation readiness)

A small random-byte layer for the credential issuer. It never creates entropy
itself: the caller injects an already-initialized system random source, and this
layer adds the process-lifecycle semantics the issuer needs on top of it.

## Entry points

| Purpose | Path |
| --- | --- |
| Provider factory | `src/random-bytes-provider.mjs` → `createRandomBytesProvider({ source, lifecycle })` |
| Lifecycle adapter | `src/lifecycle-adapter.mjs` → `createLifecycleAdapter()` |
| Error types | `src/errors.mjs` |
| Public re-exports | `src/index.mjs` |
| Acceptance tests | `tests/*.test.mjs` (run with `node --test "tests/*.test.mjs"` or `npm test`) |
| Test double | `tests/helpers/numbered-random-source.mjs` |
| Mutation sensitivity check | `scripts/mutation-check.mjs` |

Runtime: Node.js 24.15.0 (verified with `node --version`). No dependencies, no
network access, no `npm install` required.

## Injected source contract

```js
const provider = createRandomBytesProvider({
  source: {
    // Required. Must return a Uint8Array of exactly `size` bytes.
    randomBytes(size, context) { /* ... */ },
    // Optional. Per-generation activation hook.
    initialize(context) { /* sync or Promise */ },
  },
  lifecycle, // optional createLifecycleAdapter() instance
});

const bytes = await provider.getRandomBytes(32, { signal, label });
```

`context` is frozen and contains `{ generation, attempt, initializationId,
requestId, label }`. `initialize` receives `initializationId: null` (it does not
exist yet); `randomBytes` receives the id of the successful initialization that
authorized the read, which is what lets a test double attribute every byte.

A bare object such as `{ randomBytes: (n) => crypto.randomBytes(n) }` is valid;
without `initialize`, readiness is completed on a microtask.

## Guaranteed behavior

1. **Per-generation readiness.** Each process generation has its own readiness
   state. When the lifecycle adapter reports a working copy
   (`notifyWorkingCopy()`), the provider opens a new generation and does **not**
   inherit the parent's readiness, even if the parent was already `ready`. The
   next request triggers a fresh initialization.
2. **One shared initialization per generation.** Concurrent requests in one
   generation ride a single initialization; exactly one `initialize` call is
   made, and all waiters are served from it.
3. **Cancellation is per waiter.** Aborting one request's `AbortSignal` rejects
   only that request (`code: 'ABORT_ERR'`, `name: 'AbortError'`). It does not
   reject siblings, does not abort the shared initialization, and does not
   poison the generation for later requests. A request already aborted before
   dispatch starts no initialization at all.
4. **Failure fan-out and retry.** If initialization fails, every same-generation
   waiter receives the **same** `RandomBytesInitializationError` instance
   (`code: 'ERR_RANDOM_BYTES_INIT'`). The next request retries with a new
   attempt number.
5. **No stale wake-ups.** A late success or late failure from a superseded
   generation (or an earlier attempt) is ignored: it cannot mark the new
   generation ready, nor reject or wake its waiters.
6. **Superseded waiters are released.** Requests still pending when their
   generation is superseded are rejected with
   `ERR_RANDOM_BYTES_GENERATION_SUPERSEDED`, because their bytes would have come
   from the parent's initialization.
7. **Read isolation.** A `randomBytes` failure or malformed result rejects only
   that request (`ERR_RANDOM_BYTES_READ`) and leaves the generation `ready`.
   Returned bytes are a private copy, so a source reusing an internal buffer
   cannot mutate handed-out credential material.
8. **Close.** `close()` / `dispose()` rejects pending and future requests
   (`ERR_RANDOM_BYTES_CLOSED`) and unsubscribes from the lifecycle.

## Verification scope

`node --test "tests/*.test.mjs"` runs 15 tests (100 assertions) covering:

- **Acceptable outcomes:** concurrent same-generation requests served from one
  initialization; re-initialization for a new generation; retry after a failed
  initialization; per-reader byte copies.
- **Blocked / rejected outcomes:** aborted waiter; superseded generation waiter;
  failed-init fan-out; single read failure; closed provider; synchronous
  `initialize` throw.
- **Boundary / interleaved case** (`tests/provider-interleaving.test.mjs`): one
  timeline mixing an abort, a mid-flight working-copy supersede, a late parent
  success, a shared child initialization serving two waiters, and a third
  generation — asserting that no read is attributed to a stale generation and
  that every delivered byte traces to the correct successful initialization.

`node scripts/mutation-check.mjs` re-runs the suite against six deliberately
broken copies of the provider (in an OS temp dir, then deletes them) and fails
if any mutation slips through. All six are currently caught, which is the
evidence that the acceptance assertions are load-bearing rather than
tautological.

All fixtures are local and synthetic; no real target, credential, or network
resource is touched.
