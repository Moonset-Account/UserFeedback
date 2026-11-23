# Generation-Aware Service Connection Pool

A dependency-free connection pool for service clients, built on Node.js 24
ECMAScript modules and the standard library only. Its core job is to keep
serving traffic correctly while backend instances are **rolled over by
generation**, including every awkward asynchronous interleaving.

## Why this pool exists

A service registry announces instances as discovery events
`{ name, generation, address, maxConnections }`. When the same instance name is
announced with a newer generation, the previous generation must retire:

- connections already lent to in-flight sessions keep working until the
  session returns them, and are then closed;
- idle connections of the retired generation are closed immediately;
- a connect that was started but has not finished yet still occupies that
  generation's connection budget; if it succeeds after retirement it is closed
  and **never lent to a new request**;
- a new generation's connection is never lost just because the waiter that
  originally requested it timed out or was cancelled — it stays available for
  the next borrower of the same generation.

## Guarantees

1. **Generation isolation.** A retired generation never lends again. New
   borrowers are only ever assigned active generations, even when two
   generations share the same `address`.
2. **Slot accounting while dialing.** A connect that has started but not yet
   succeeded reserves one slot against its generation's `maxConnections`.
3. **Strict FIFO allocation.** Requests are handled in entry order. The pump
   skips requests whose deadline already passed (without serving them) and
   parks all later requests behind the oldest request that cannot be served.
   A request that already owns an in-flight dial is "being prepared" and does
   not block other requests from using free capacity on other generations.
4. **Independent deadlines.** Every borrow accepts its own relative timeout
   and/or absolute deadline, plus an optional `AbortSignal`.
5. **Single settlement.** A borrow resolves or rejects exactly once. The
   deadline timer, abort signal, connect success and connect failure all race
   through one `settled` flag.
6. **Idempotent returns.** Returning the same lease twice is a no-op; one
   physical return can wake at most one waiter.
7. **Deterministic controls.** Discovery, the connect factory and the clock
   are all injected, so rollover/dial/deadline interleavings are fully
   reproducible in tests without real timers or network calls.

## Public API

```js
import { ConnectionPool, ManualClock } from './src/index.mjs';

const clock = new ManualClock(0);            // controllable virtual clock
const pool = new ConnectionPool({
  clock,                                     // { now, setTimeout, clearTimeout }
  connect: async (address, discovery) => {  // controllable connect factory
    // discovery: { name, generation, address, maxConnections }
    return myTransport.connect(address);    // returned object should have close()
  },
});
```

### `discover(event)`

Feed a discovery event `{ name, generation, address, maxConnections }`.

- Re-announcing the same `(name, generation)` is a no-op heartbeat.
- An event older than the newest known generation for the name is ignored.
- A newer generation for an existing name retires the current one.
- Returns `{ key, retired, ignored, duplicate }`.

### `borrow(options?) -> Promise<Lease>`

Options (all optional):

- `timeoutMs` — relative deadline measured from the clock's `now()`.
- `deadline` — absolute epoch-ms deadline; combined with `timeoutMs` by taking
  the earliest.
- `signal` — an `AbortSignal` for explicit cancellation.

A `Lease` resolves to:

```js
{
  seq,                       // global borrow sequence number
  connection,                // the raw object your connect() returned
  connectionId,              // pool-assigned connection id
  instanceName, generation, address,
  isReturned,                // boolean
  return(),                  // idempotent; returns true the first time
}
```

Rejections are typed: `BorrowTimeoutError`, `BorrowAbortedError`,
`PoolClosedError` (all extend `PoolError`). A failed connect does not itself
reject a borrow; the slot is released and the request retries while its
deadline remains valid.

### `stats()`

Returns a read-only snapshot:

```js
{
  closed,
  queueDepth,        // requests with no connection and no in-flight dial
  pendingWaiters,    // requests whose dial is currently running
  totalWaiters,
  generations: [{
    key, name, generation, address, maxConnections, active, retireReason,
    borrowedCount, idleCount, pendingConnects, activeConnections,
    availableCapacity,
    totalConnects, totalConnectFailures, totalClosed, totalBorrowed,
  }],
  closeEvents: [{    // every real close() the pool performed
    connectionId, key, name, generation, address, reason, at, closeError,
  }],
}
```

Close reasons include `generation-retired-idle`,
`generation-retired-inflight`, `returned-retired`, `returned-closed-pool` and
`pool-close`. A connection without a `close()` method, or one whose `close()`
throws, is still accounted for; the throw is captured in `closeError` rather
than propagated.

### `close()`

Stops accepting work, rejects every queued waiter with `PoolClosedError`,
closes all idle connections, and closes in-flight connections as soon as they
resolve. Borrowed connections close when returned.

### Clocks

- `ManualClock` — integer-ms virtual clock. `advance(ms)` fires due timers in
  due-time order (insertion order breaks ties); `tick(ms)` advances and then
  drains the microtask queue. `pendingTimerCount` exposes leaks.
- `systemClock` — adapter over the real `Date.now()` / `setTimeout` for
  production use, exposing the same interface.

## Reproduce

Requires Node.js 24.15.0. No dependencies are installed.

```bash
node --version                       # v24.15.0
node --test tests/clock.test.mjs \
             tests/pool-normal.test.mjs \
             tests/pool-deadline.test.mjs \
             tests/pool-generation.test.mjs \
             tests/pool-edge.test.mjs
# or, equivalently:
npm test
```

## Verification scope

All checks are real `node:test` assertions against the running pool with
injected fakes — there are no canned/pre-computed demo outputs.

| File | Scenario classes covered |
| --- | --- |
| `tests/clock.test.mjs` | Manual clock ordering, zero-delay and self-rescheduling timers, clearing, validation, microtask draining |
| `tests/pool-normal.test.mjs` | Borrow/return and idle reuse, `maxConnections` cap with FIFO queue, duplicate return, connect failure retry, borrow-before-discovery, event validation and idempotency, `close()` behavior |
| `tests/pool-deadline.test.mjs` | Pre-expired and queued deadlines, skipping an expired head, relative+absolute deadline combination, abort (including pre-aborted and racing success), timeout racing connect failure, strict head-of-line blocking |
| `tests/pool-generation.test.mjs` | Same-address rollover accounting (borrows/connects/closes per generation), immediate idle close, late in-flight success/failure after retirement, dial surviving waiter timeout/cancel, multiple instances, rollover under load, `close()` during in-flight dials |
| `tests/pool-edge.test.mjs` | Missing/throwing `close()`, different-address rollover, new-generation dial surviving its original waiter's timeout, old-generation dial never lent across generations, queue partition stats, timer/handle cleanup |

Key acceptance interleavings that are explicitly asserted:

- two generations at the **same address** keep independent borrow, connect and
  close counters;
- an in-flight connect that resolves **after** instance retirement is closed
  and never lent;
- a new-generation connection remains allocatable **even though the original
  waiter timed out** before the dial resolved;
- one borrow settles exactly once, and a repeated `return()` cannot wake a
  second waiter;
- timeout/abort/close paths leave no pending clock timers.

## Layout

```
src/
  index.mjs     public exports
  pool.mjs      ConnectionPool
  clock.mjs     ManualClock, systemClock
  errors.mjs    typed error hierarchy
tests/
  helpers.mjs   controllable connect fakes and microtask flush utilities
  *.test.mjs    node:test suites
```
