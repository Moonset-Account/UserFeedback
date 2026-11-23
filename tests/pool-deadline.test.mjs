import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  ConnectionPool,
  BorrowAbortedError,
  BorrowTimeoutError,
} from '../src/pool.mjs';
import { ManualClock } from '../src/clock.mjs';
import {
  assertRejects,
  controllableConnect,
  flushMicrotasks,
  genSnapshot,
} from './helpers.mjs';

const EVENT = (overrides = {}) => ({
  name: 'svc',
  generation: 1,
  address: 'addr-1',
  maxConnections: 1,
  ...overrides,
});

test('deadline: an already-expired request is rejected without dialling', async () => {
  const clock = new ManualClock(1000);
  const { connect, calls } = controllableConnect();
  const pool = new ConnectionPool({ clock, connect });
  pool.discover(EVENT());

  await assertRejects(
    pool.borrow({ deadline: 500 }),
    BorrowTimeoutError,
    'already expired',
  );
  assert.equal(calls.length, 0);
  assert.equal(pool.stats().queueDepth, 0);
});

test('deadline: queued waiter times out when no connection frees in time', async () => {
  const clock = new ManualClock();
  const { connect, calls } = controllableConnect();
  const pool = new ConnectionPool({ clock, connect });
  pool.discover(EVENT());

  const heldP = pool.borrow();
  const queuedP = pool.borrow({ timeoutMs: 100 });
  await flushMicrotasks();
  calls[0].resolve();
  const held = await heldP;

  await clock.tick(99);
  assert.equal(pool.stats().queueDepth, 1, 'still queued before deadline');

  await clock.tick(1);
  await assertRejects(queuedP, BorrowTimeoutError, 'timed out');
  assert.equal(pool.stats().queueDepth, 0);

  held.return();
});

test('deadline: pump skips an expired head and serves the next valid waiter', async () => {
  const clock = new ManualClock();
  const { connect, calls } = controllableConnect();
  const pool = new ConnectionPool({ clock, connect });
  pool.discover(EVENT({ maxConnections: 1 }));

  const heldP = pool.borrow();
  const shortP = pool.borrow({ timeoutMs: 10 });
  const longP = pool.borrow({ timeoutMs: 1000 });
  await flushMicrotasks();
  calls[0].resolve();
  const held = await heldP;

  // Expire only the head waiter; the second one keeps waiting.
  await clock.tick(10);
  await assertRejects(shortP, BorrowTimeoutError);
  assert.equal(pool.stats().queueDepth, 1);

  // Freeing the connection serves the surviving waiter directly.
  held.return();
  const next = await longP;
  assert.equal(next.seq, 3, 'the surviving (originally third) waiter wins');
  next.return();
});

test('deadline: absolute deadline and relative timeout combine to the earliest', async () => {
  const clock = new ManualClock(500);
  const { connect } = controllableConnect();
  const pool = new ConnectionPool({ clock, connect });
  pool.discover(EVENT());

  // No capacity ever frees; timeout 50ms beats absolute deadline at 600.
  const p = pool.borrow({ timeoutMs: 50, deadline: 600 });
  await flushMicrotasks();
  // The single slot is taken by this waiter's own in-flight dial.
  assert.equal(pool.stats().pendingWaiters, 1);
  assert.equal(pool.stats().queueDepth, 0);
  await clock.tick(49);
  assert.equal(pool.stats().pendingWaiters, 1);
  await clock.tick(1);
  await assertRejects(p, BorrowTimeoutError);
});

test('abort: signal cancellation rejects exactly once and clears the timer', async () => {
  const clock = new ManualClock();
  const { connect, calls } = controllableConnect();
  const pool = new ConnectionPool({ clock, connect });
  pool.discover(EVENT());

  const heldP = pool.borrow();
  const ac = new AbortController();
  const cancelledP = pool.borrow({ timeoutMs: 1000, signal: ac.signal });
  await flushMicrotasks();
  calls[0].resolve();
  const held = await heldP;

  ac.abort(new Error('caller went away'));
  const err = await assertRejects(cancelledP, BorrowAbortedError);
  assert.equal(err.cause.message, 'caller went away');
  assert.equal(clock.pendingTimerCount, 0, 'deadline timer must be cleared on abort');

  held.return();
});

test('abort: pre-aborted signal rejects immediately', async () => {
  const clock = new ManualClock();
  const { connect, calls } = controllableConnect();
  const pool = new ConnectionPool({ clock, connect });
  pool.discover(EVENT());
  const ac = new AbortController();
  ac.abort();
  await assertRejects(pool.borrow({ signal: ac.signal }), BorrowAbortedError);
  assert.equal(calls.length, 0);
});

test('settle once: abort racing a successful lend cannot settle twice', async () => {
  const clock = new ManualClock();
  const { connect, calls } = controllableConnect();
  const pool = new ConnectionPool({ clock, connect });
  pool.discover(EVENT());

  const ac = new AbortController();
  const p = pool.borrow({ signal: ac.signal });
  await flushMicrotasks();

  // Resolve the dial and abort before the fulfillment microtask runs.
  calls[0].resolve();
  ac.abort();
  let outcome;
  try {
    outcome = await p;
  } catch (err) {
    outcome = err;
  }
  assert.ok(outcome, 'promise settled exactly one way');

  if (outcome instanceof Error) {
    assert.ok(outcome instanceof BorrowAbortedError);
  } else {
    assert.equal(outcome.isReturned, false);
    const id = outcome.connectionId;
    outcome.return();
    // The pooled connection must remain healthy for later borrowers.
    const again = await pool.borrow();
    assert.equal(again.connectionId, id);
    again.return();
  }
});

test('settle once: timeout racing connect failure leaves no dangling slot', async () => {
  const clock = new ManualClock();
  const { connect, calls } = controllableConnect();
  const pool = new ConnectionPool({ clock, connect });
  pool.discover(EVENT());

  const p = pool.borrow({ timeoutMs: 10 });
  await flushMicrotasks();
  calls[0].reject(new Error('network down'));
  await clock.tick(10);

  await assertRejects(p, BorrowTimeoutError);
  assert.equal(pool.stats().queueDepth, 0);

  // Capacity must be fully released: a fresh borrow can dial immediately.
  const p2 = pool.borrow();
  await flushMicrotasks();
  assert.equal(calls.length, 2);
  calls[1].resolve();
  const lease = await p2;
  const snap = genSnapshot(pool.stats(), 'svc', 1);
  assert.equal(snap.pendingConnects, 0);
  assert.equal(snap.borrowedCount, 1);
  lease.return();
});

test('queue: later waiters remain blocked while the head cannot be served', async () => {
  const clock = new ManualClock();
  const { connect, calls } = controllableConnect();
  const pool = new ConnectionPool({ clock, connect });
  pool.discover(EVENT({ maxConnections: 1 }));

  const heldP = pool.borrow();
  const headP = pool.borrow({ timeoutMs: 100 });
  const tailP = pool.borrow({ timeoutMs: 1000 });
  await flushMicrotasks();
  calls[0].resolve();
  const held = await heldP;

  // Even with a long deadline, the tail cannot jump over the blocked head.
  assert.equal(calls.length, 1);
  assert.equal(pool.stats().queueDepth, 2);

  await clock.tick(100);
  await assertRejects(headP, BorrowTimeoutError);

  // Now the tail becomes head and still waits for the held connection.
  held.return();
  const tail = await tailP;
  assert.equal(tail.seq, 3);
  tail.return();
});
