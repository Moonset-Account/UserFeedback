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

test('same address rollover: two generations split borrow/connect/close accounting', async () => {
  const clock = new ManualClock();
  const { connect, calls } = controllableConnect();
  const pool = new ConnectionPool({ clock, connect });

  // Generation 1, capacity 2, identical address for both generations.
  pool.discover(genEvent('svc', 1, 'host:9999', 2));
  const p1 = pool.borrow();
  const p2 = pool.borrow();
  await flushMicrotasks();
  calls[0].resolve();
  calls[1].resolve();
  const [l1, l2] = await Promise.all([p1, p2]);

  // Rollover to generation 2 at the SAME address while two sessions are out.
  pool.discover(genEvent('svc', 2, 'host:9999', 2));

  const oldSnap = genSnapshot(pool.stats(), 'svc', 1);
  assert.equal(oldSnap.active, false);
  assert.equal(oldSnap.borrowedCount, 2, 'old loans keep serving borrowed sessions');
  assert.equal(oldSnap.idleCount, 0);
  assert.equal(oldSnap.totalConnects, 2);
  assert.equal(oldSnap.totalClosed, 0, 'nothing closes while sessions are still out');

  // New generation starts with zero usage and its own capacity budget.
  const newSnap = genSnapshot(pool.stats(), 'svc', 2);
  assert.equal(newSnap.active, true);
  assert.equal(newSnap.totalConnects, 0);
  assert.equal(newSnap.availableCapacity, 2);

  // New borrowers must never touch the old generation.
  const p3 = pool.borrow();
  const p4 = pool.borrow();
  await flushMicrotasks();
  assert.equal(calls.length, 4, 'two fresh dials on the new generation');
  assert.deepEqual(
    calls.slice(2).map((c) => c.discovery.generation),
    [2, 2],
  );
  calls[2].resolve();
  calls[3].resolve();
  const [l3, l4] = await Promise.all([p3, p4]);
  assert.equal(l3.generation, 2);
  assert.equal(l4.generation, 2);

  // Returning old-generation leases closes them; they never re-enter the pool.
  l1.return();
  l2.return();
  const oldCloses = closesFor(pool.stats(), 'svc', 1);
  assert.equal(oldCloses.length, 2);
  assert.deepEqual(oldCloses.map((e) => e.reason), [
    'returned-retired',
    'returned-retired',
  ]);
  assert.equal(l1.connection.closed, true);
  assert.equal(l2.connection.closed, true);

  // New-generation returns stay pooled and reusable.
  l3.return();
  const reused = await pool.borrow();
  assert.equal(reused.connectionId, l3.connectionId);
  reused.return();
  l4.return();

  const finalOld = genSnapshot(pool.stats(), 'svc', 1);
  const finalNew = genSnapshot(pool.stats(), 'svc', 2);
  assert.equal(finalOld.totalBorrowed, 2);
  assert.equal(finalOld.totalConnects, 2);
  assert.equal(finalOld.totalClosed, 2);
  assert.equal(finalNew.totalBorrowed, 3); // p3, p4 + reuse
  assert.equal(finalNew.totalConnects, 2);
  assert.equal(finalNew.totalClosed, 0);
  assert.equal(finalNew.idleCount, 2);
});

test('retirement: idle connections close immediately with real close() calls', async () => {
  const clock = new ManualClock();
  const { connect, calls } = controllableConnect();
  const pool = new ConnectionPool({ clock, connect });
  pool.discover(genEvent('svc', 1, 'a', 2));

  const p1 = pool.borrow();
  const p2 = pool.borrow();
  await flushMicrotasks();
  calls[0].resolve();
  calls[1].resolve();
  const [l1, l2] = await Promise.all([p1, p2]);

  l1.return(); // idle now
  const idleConn = l1.connection;
  assert.equal(idleConn.closed, false);

  pool.discover(genEvent('svc', 2, 'a', 2));
  assert.equal(idleConn.closed, true, 'idle conn closed at retirement');

  const events = closesFor(pool.stats(), 'svc', 1);
  assert.equal(events.length, 1);
  assert.equal(events[0].reason, 'generation-retired-idle');

  l2.return();
  const events2 = closesFor(pool.stats(), 'svc', 1);
  assert.equal(events2.length, 2);
  assert.equal(events2[1].reason, 'returned-retired');
});

test('in-flight connect resolves after retirement: closed, never lent to anyone', async () => {
  const clock = new ManualClock();
  const { connect, calls } = controllableConnect();
  const pool = new ConnectionPool({ clock, connect });
  pool.discover(genEvent('svc', 1, 'a', 3));

  const heldP = pool.borrow();
  const inflightP = pool.borrow();
  await flushMicrotasks();
  assert.equal(calls.length, 2);
  calls[0].resolve();
  const held = await heldP;

  // Second dial still pending when generation 2 arrives.
  pool.discover(genEvent('svc', 2, 'a', 3));

  const oldSnap = genSnapshot(pool.stats(), 'svc', 1);
  assert.equal(oldSnap.pendingConnects, 1, 'in-flight dial still reserves its slot');

  // The queued waiter is retried on the new generation immediately.
  await flushMicrotasks();
  assert.equal(calls.length, 3);
  assert.equal(calls[2].discovery.generation, 2);

  // Old dial resolves late: connection must be closed, not pooled or lent.
  calls[1].resolve();
  await flushMicrotasks();
  const oldAfter = genSnapshot(pool.stats(), 'svc', 1);
  assert.equal(oldAfter.pendingConnects, 0);
  assert.equal(oldAfter.activeConnections, 1, 'only the held session remains');
  const lateCloses = closesFor(pool.stats(), 'svc', 1).filter(
    (e) => e.reason === 'generation-retired-inflight',
  );
  assert.equal(lateCloses.length, 1);

  calls[2].resolve();
  const inflight = await inflightP;
  assert.equal(inflight.generation, 2, 'waiter was served by the new generation');
  inflight.return();
  held.return();
});

test('in-flight connect fails after retirement: slot frees, waiter served by new gen', async () => {
  const clock = new ManualClock();
  const { connect, calls } = controllableConnect();
  const pool = new ConnectionPool({ clock, connect });
  pool.discover(genEvent('svc', 1, 'a', 1));

  const waiterP = pool.borrow();
  await flushMicrotasks();
  pool.discover(genEvent('svc', 2, 'a', 1));
  await flushMicrotasks();
  assert.equal(calls.length, 2, 'retry dial started on gen 2');

  calls[0].reject(new Error('old gen vanished'));
  await flushMicrotasks();

  const oldSnap = genSnapshot(pool.stats(), 'svc', 1);
  assert.equal(oldSnap.pendingConnects, 0);
  assert.equal(oldSnap.totalConnectFailures, 1);

  calls[1].resolve();
  const lease = await waiterP;
  assert.equal(lease.generation, 2);
  lease.return();
});

test('waiter times out before its dial resolves: connection stays usable for next borrower', async () => {
  const clock = new ManualClock();
  const { connect, calls } = controllableConnect();
  const pool = new ConnectionPool({ clock, connect });
  pool.discover(genEvent('svc', 1, 'a', 2));

  const p = pool.borrow({ timeoutMs: 10 });
  await flushMicrotasks();
  await clock.tick(10);
  await assertRejects(p, BorrowTimeoutError);

  // Dial lands after the original waiter has gone.
  calls[0].resolve();
  await flushMicrotasks();

  const snap = genSnapshot(pool.stats(), 'svc', 1);
  assert.equal(snap.idleCount, 1, 'new connection is pooled, not wasted');
  assert.equal(snap.pendingConnects, 0);
  assert.equal(snap.totalConnects, 1);

  const next = await pool.borrow();
  assert.equal(next.generation, 1);
  next.return();
});

test('waiter cancels while dial pending: resolved connection joins idle pool', async () => {
  const clock = new ManualClock();
  const { connect, calls } = controllableConnect();
  const pool = new ConnectionPool({ clock, connect });
  pool.discover(genEvent('svc', 1, 'a', 2));

  const ac = new AbortController();
  const p = pool.borrow({ signal: ac.signal, timeoutMs: 1000 });
  await flushMicrotasks();
  ac.abort();
  await assertRejects(p, Error);

  calls[0].resolve();
  await flushMicrotasks();
  const snap = genSnapshot(pool.stats(), 'svc', 1);
  assert.equal(snap.idleCount, 1);
  assert.equal(snap.totalClosed, 0, 'cancellation does not kill an otherwise healthy connection');
});

test('multiple instances: capacity and waiters are scheduled across generations', async () => {
  const clock = new ManualClock();
  const { connect, calls } = controllableConnect();
  const pool = new ConnectionPool({ clock, connect });
  pool.discover(genEvent('alpha', 1, 'addr-a', 1));
  pool.discover(genEvent('beta', 1, 'addr-b', 1));

  const [a, b, c] = [pool.borrow(), pool.borrow(), pool.borrow()];
  assert.equal(await flushUntil(() => calls.length === 2), true);
  assert.equal(calls.length, 2, 'one dial per instance, third request queues');
  assert.equal(pool.stats().queueDepth, 1);

  calls[0].resolve();
  calls[1].resolve();
  const [la, lb] = await Promise.all([a, b]);
  assert.deepEqual([la.instanceName, lb.instanceName].sort(), ['alpha', 'beta']);

  la.return();
  const lc = await c;
  assert.equal(lc.instanceName, 'alpha');
  lb.return();
  lc.return();
});

test('rollover under load: every queued waiter is eventually served by the new generation', async () => {
  const clock = new ManualClock();
  const { connect, calls } = controllableConnect();
  const pool = new ConnectionPool({ clock, connect });
  pool.discover(genEvent('svc', 1, 'a', 1));

  const heldP = pool.borrow();
  const waiters = [];
  for (let i = 0; i < 3; i += 1) waiters.push(pool.borrow({ timeoutMs: 5000 }));
  await flushMicrotasks();
  calls[0].resolve();
  await heldP;
  assert.equal(pool.stats().queueDepth, 3);

  // Rollover with a larger capacity; old held session still out.
  pool.discover(genEvent('svc', 2, 'a', 3));
  await flushMicrotasks();
  assert.equal(calls.length, 4, 'three dials started for the three waiters');

  for (let i = 1; i < calls.length; i += 1) calls[i].resolve();
  const leases = await Promise.all(waiters);
  assert.ok(leases.every((l) => l.generation === 2));

  const held = await heldP;
  held.return();
  leases.forEach((l) => l.return());

  const oldSnap = genSnapshot(pool.stats(), 'svc', 1);
  assert.equal(oldSnap.totalClosed, 1, 'the single old conn closed on return');
  const newSnap = genSnapshot(pool.stats(), 'svc', 2);
  assert.equal(newSnap.totalConnects, 3);
  assert.equal(pool.stats().queueDepth, 0);
});

test('close() while dials are in flight: late resolutions close instead of leaking', async () => {
  const clock = new ManualClock();
  const { connect, calls } = controllableConnect();
  const pool = new ConnectionPool({ clock, connect });
  pool.discover(genEvent('svc', 1, 'a', 2));

  const p1 = pool.borrow();
  const p2 = pool.borrow();
  await flushMicrotasks();
  pool.close();
  await assertRejects(p1, Error);
  await assertRejects(p2, Error);

  calls[0].resolve();
  calls[1].resolve();
  await flushMicrotasks();

  const events = pool.stats().closeEvents;
  assert.equal(events.length, 2, 'both late connections were closed');
  assert.ok(events.every((e) => e.reason === 'pool-close'));
});
