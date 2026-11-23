import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  ConnectionPool,
  BorrowTimeoutError,
} from '../src/pool.mjs';
import { ManualClock } from '../src/clock.mjs';
import {
  assertRejects,
  closesFor,
  controllableConnect,
  flushMicrotasks,
  flushUntil,
  genSnapshot,
} from './helpers.mjs';

const genEvent = (name, generation, address, maxConnections) => ({
  name, generation, address, maxConnections,
});

test('edge: a connection without close() still retires without throwing', async () => {
  const clock = new ManualClock();
  const { connect, calls } = controllableConnect();
  const pool = new ConnectionPool({ clock, connect });
  pool.discover(genEvent('svc', 1, 'a', 1));

  // Override the fake's connection with a plain object lacking close().
  const p = pool.borrow();
  await flushMicrotasks();
  const bare = { kind: 'no-close-method' };
  calls[0].resolve(bare);
  const lease = await p;
  assert.equal(lease.connection, bare);

  pool.discover(genEvent('svc', 2, 'a', 1));
  // Returning to a retired generation must still record a close event and
  // never throw even though raw.close is absent.
  lease.return();
  const events = closesFor(pool.stats(), 'svc', 1);
  assert.equal(events.length, 1);
  assert.equal(events[0].reason, 'returned-retired');
  assert.equal(events[0].closeError, null);
});

test('edge: close() throwing is captured, does not crash the pool', async () => {
  const clock = new ManualClock();
  const { connect, calls } = controllableConnect();
  const pool = new ConnectionPool({ clock, connect });
  pool.discover(genEvent('svc', 1, 'a', 1));

  const p = pool.borrow();
  await flushMicrotasks();
  const throwing = {
    close() {
      throw new Error('boom');
    },
  };
  calls[0].resolve(throwing);
  const lease = await p;

  pool.discover(genEvent('svc', 2, 'a', 1));
  lease.return(); // must not propagate the close() error
  const events = closesFor(pool.stats(), 'svc', 1);
  assert.equal(events.length, 1);
  assert.match(events[0].closeError, /boom/);
});

test('rollover to a DIFFERENT address: new dials target the new address only', async () => {
  const clock = new ManualClock();
  const { connect, calls } = controllableConnect();
  const pool = new ConnectionPool({ clock, connect });
  pool.discover(genEvent('svc', 1, 'old-addr', 1));

  const heldP = pool.borrow();
  await flushMicrotasks();
  calls[0].resolve();
  const held = await heldP;
  assert.equal(held.address, 'old-addr');

  pool.discover(genEvent('svc', 2, 'new-addr', 1));
  const freshP = pool.borrow();
  await flushMicrotasks();
  assert.equal(calls[1].address, 'new-addr');
  assert.equal(calls[1].discovery.generation, 2);
  calls[1].resolve();
  const fresh = await freshP;
  assert.equal(fresh.address, 'new-addr');
  assert.equal(fresh.generation, 2);

  held.return();
  fresh.return();
  const oldSnap = genSnapshot(pool.stats(), 'svc', 1);
  assert.equal(oldSnap.totalClosed, 1);
  const newSnap = genSnapshot(pool.stats(), 'svc', 2);
  assert.equal(newSnap.totalClosed, 0);
});

test('interleave: new-generation dial stays allocatable after its original waiter times out', async () => {
  const clock = new ManualClock();
  const { connect, calls } = controllableConnect();
  const pool = new ConnectionPool({ clock, connect });

  // Gen 1 fully occupied by a held session.
  pool.discover(genEvent('svc', 1, 'a', 1));
  const heldP = pool.borrow();
  await flushMicrotasks();
  calls[0].resolve();
  const held = await heldP;

  // A waiter queues; rollover happens, so its dial is started on gen 2.
  const impatient = pool.borrow({ timeoutMs: 100 });
  pool.discover(genEvent('svc', 2, 'a', 1));
  await flushUntil(() => calls.length === 2);
  assert.equal(calls[1].discovery.generation, 2);

  // Original waiter times out BEFORE the gen-2 dial resolves.
  await clock.tick(100);
  await assertRejects(impatient, BorrowTimeoutError);

  // Gen-2 dial resolves late: the connection must not be destroyed; it
  // becomes idle and is handed to a fresh borrower on the same new gen.
  calls[1].resolve();
  await flushMicrotasks();
  const newSnap = genSnapshot(pool.stats(), 'svc', 2);
  assert.equal(newSnap.idleCount, 1);
  assert.equal(newSnap.totalClosed, 0);

  const later = await pool.borrow();
  assert.equal(later.generation, 2);
  assert.equal(later.address, 'a');
  later.return();
  held.return();
});

test('interleave: old-generation late dial never reaches a new-generation waiter', async () => {
  const clock = new ManualClock();
  const { connect, calls } = controllableConnect();
  const pool = new ConnectionPool({ clock, connect });
  pool.discover(genEvent('svc', 1, 'a', 2));

  const heldP = pool.borrow();
  const queuedP = pool.borrow();
  await flushMicrotasks();
  assert.equal(calls.length, 2);
  calls[0].resolve();
  const held = await heldP;

  // Rollover while call[1] (gen 1) is still pending. The waiter retries
  // onto gen 2 and gets a second dial.
  pool.discover(genEvent('svc', 2, 'a', 2));
  await flushUntil(() => calls.length === 3);
  const gen2Dial = calls[2];
  assert.equal(gen2Dial.discovery.generation, 2);

  // The stale gen-1 dial resolves and must be closed, never lent.
  calls[1].resolve();
  await flushMicrotasks();
  const oldCloses = closesFor(pool.stats(), 'svc', 1);
  assert.equal(oldCloses.some((e) => e.reason === 'generation-retired-inflight'), true);

  // The queued waiter is fulfilled exclusively by the gen-2 dial.
  gen2Dial.resolve();
  const queued = await queuedP;
  assert.equal(queued.generation, 2);
  queued.return();
  held.return();
});

test('stats: pendingWaiters and queueDepth partition every unsettled waiter', async () => {
  const clock = new ManualClock();
  const { connect, calls } = controllableConnect();
  const pool = new ConnectionPool({ clock, connect });
  pool.discover(genEvent('svc', 1, 'a', 1));

  const w1 = pool.borrow();
  const w2 = pool.borrow();
  const w3 = pool.borrow();
  await flushMicrotasks();

  const s = pool.stats();
  assert.equal(s.totalWaiters, 3);
  assert.equal(s.pendingWaiters, 1, 'one dial in flight for the head');
  assert.equal(s.queueDepth, 2, 'two waiters have no slot yet');

  calls[0].resolve();
  const lease1 = await w1;
  // w1 now holds the only connection; w2/w3 remain purely queued.
  const s2 = pool.stats();
  assert.equal(s2.pendingWaiters, 0);
  assert.equal(s2.queueDepth, 2);

  // Releasing the connection lends it to w2 (no new dial needed).
  lease1.return();
  const lease2 = await w2;
  assert.equal(lease2.seq, 2);
  const s3 = pool.stats();
  assert.equal(s3.queueDepth, 1, 'only w3 is still waiting');

  lease2.return();
  const lease3 = await w3;
  lease3.return();
  assert.equal(pool.stats().totalWaiters, 0);
});

test('cleanup: abort and timeout paths leave no pending clock timers', async () => {
  const clock = new ManualClock();
  const { connect, calls } = controllableConnect();
  const pool = new ConnectionPool({ clock, connect });
  pool.discover(genEvent('svc', 1, 'a', 1));

  // One timeout-based borrow and one abort-based borrow queue behind a
  // held connection; both must clear their deadline timer on settlement.
  const heldP = pool.borrow();
  await flushMicrotasks();
  calls[0].resolve();
  const held = await heldP;

  const ac = new AbortController();
  const timed = pool.borrow({ timeoutMs: 50 });
  const aborted = pool.borrow({ timeoutMs: 50, signal: ac.signal });
  await flushMicrotasks();
  assert.ok(clock.pendingTimerCount >= 2);

  await clock.tick(50);
  await assertRejects(timed, BorrowTimeoutError);
  ac.abort();
  await assertRejects(aborted, Error);

  assert.equal(clock.pendingTimerCount, 0, 'no deadline timers remain');
  held.return();
});

test('cleanup: closing the pool drains every queued waiter and timer', async () => {
  const clock = new ManualClock();
  const { connect } = controllableConnect();
  const pool = new ConnectionPool({ clock, connect });
  pool.discover(genEvent('svc', 1, 'a', 1));

  const waiters = [];
  for (let i = 0; i < 3; i += 1) {
    waiters.push(pool.borrow({ timeoutMs: 1000 }));
  }
  await flushMicrotasks();
  pool.close();

  for (const w of waiters) {
    // eslint-disable-next-line no-await-in-loop
    await assertRejects(w, Error);
  }
  assert.equal(pool.stats().totalWaiters, 0);
  assert.equal(clock.pendingTimerCount, 0);
});
