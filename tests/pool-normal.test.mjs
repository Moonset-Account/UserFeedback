import { test } from 'node:test';
import assert from 'node:assert/strict';
import { ConnectionPool, PoolClosedError } from '../src/pool.mjs';
import { ManualClock } from '../src/clock.mjs';
import {
  FakeConnection,
  assertRejects,
  controllableConnect,
  flushMicrotasks,
  genSnapshot,
  immediateConnect,
} from './helpers.mjs';

const EVENT = (overrides = {}) => ({
  name: 'svc',
  generation: 1,
  address: 'addr-1',
  maxConnections: 2,
  ...overrides,
});

test('borrow: immediate connect, return, then idle reuse without a new dial', async () => {
  const clock = new ManualClock();
  const { connect, calls } = immediateConnect();
  const pool = new ConnectionPool({ clock, connect });
  pool.discover(EVENT());

  const lease1 = await pool.borrow();
  assert.equal(lease1.instanceName, 'svc');
  assert.equal(lease1.generation, 1);
  assert.equal(lease1.address, 'addr-1');
  assert.equal(lease1.isReturned, false);
  assert.equal(calls.length, 1);

  lease1.return();
  assert.equal(lease1.isReturned, true);

  const lease2 = await pool.borrow();
  assert.equal(lease2.connectionId, lease1.connectionId, 'idle connection must be reused');
  assert.equal(calls.length, 1, 'no new connect on reuse');
  lease2.return();

  const stats = pool.stats();
  const snap = genSnapshot(stats, 'svc', 1);
  assert.equal(snap.totalConnects, 1);
  assert.equal(snap.totalBorrowed, 2);
  assert.equal(snap.idleCount, 1);
  assert.equal(snap.borrowedCount, 0);
});

test('borrow: maxConnections caps concurrent connects; waiters queue FIFO', async () => {
  const clock = new ManualClock();
  const { connect, calls } = controllableConnect();
  const pool = new ConnectionPool({ clock, connect });
  pool.discover(EVENT({ maxConnections: 2 }));

  const p1 = pool.borrow();
  const p2 = pool.borrow();
  const p3 = pool.borrow();
  // Dials are dispatched on the microtask queue; flush before observing.
  await flushMicrotasks();
  assert.equal(calls.length, 2, 'only maxConnections dials start');
  assert.equal(pool.stats().queueDepth, 1);

  // Resolve the two dials; the third waiter must still be waiting because
  // both connections are lent out.
  calls[0].resolve();
  calls[1].resolve();
  const [l1, l2] = await Promise.all([p1, p2]);
  assert.equal(pool.stats().queueDepth, 1);

  // Returning one connection hands it straight to the queued third request.
  l1.return();
  const l3 = await p3;
  assert.equal(l3.connectionId, l1.connectionId);
  l2.return();
  l3.return();
});

test('return: duplicate return is a no-op and never serves a second waiter', async () => {
  const clock = new ManualClock();
  const { connect, calls } = controllableConnect();
  const pool = new ConnectionPool({ clock, connect });
  pool.discover(EVENT({ maxConnections: 1 }));

  const lease = await (async () => {
    const p = pool.borrow();
    await flushMicrotasks();
    calls[0].resolve();
    return p;
  })();

  const waiting = pool.borrow();
  await flushMicrotasks();
  assert.equal(pool.stats().queueDepth, 1);

  assert.equal(lease.return(), true);
  const secondLease = await waiting;
  assert.equal(secondLease.connectionId, lease.connectionId);

  // The original lease is now consumed; returning again changes nothing.
  assert.equal(lease.return(), false);
  assert.equal(lease.return(), false);
  assert.equal(lease.isReturned, true);

  // Pool still has exactly one idle connection, not two phantom slots.
  secondLease.return();
  const snap = genSnapshot(pool.stats(), 'svc', 1);
  assert.equal(snap.idleCount, 1);
  assert.equal(snap.totalBorrowed, 2);
});

test('connect: a failed dial frees the slot and the pump retries on the same gen', async () => {
  const clock = new ManualClock();
  let attempt = 0;
  const calls = [];
  const connect = (address, discovery) => {
    const n = ++attempt;
    calls.push({ address, discovery, n });
    if (n === 1) return Promise.reject(new Error('dial refused'));
    return Promise.resolve(new FakeConnection(address));
  };
  const pool = new ConnectionPool({ clock, connect });
  pool.discover(EVENT());

  const lease = await pool.borrow();
  assert.equal(attempt, 2, 'first dial failed, second succeeded');
  const snap = genSnapshot(pool.stats(), 'svc', 1);
  assert.equal(snap.totalConnects, 1);
  assert.equal(snap.totalConnectFailures, 1);
  assert.equal(snap.pendingConnects, 0);
  lease.return();
});

test('borrow before any discovery: request stays queued, served on later discover', async () => {
  const clock = new ManualClock();
  const { connect, calls } = controllableConnect();
  const pool = new ConnectionPool({ clock, connect });

  const p = pool.borrow();
  assert.equal(pool.stats().queueDepth, 1);
  assert.equal(calls.length, 0);

  pool.discover(EVENT());
  await flushMicrotasks();
  assert.equal(calls.length, 1, 'discovery triggers a dial for the queued waiter');
  calls[0].resolve();
  const lease = await p;
  assert.equal(lease.generation, 1);
  lease.return();
});

test('discover: malformed events are rejected and change no state', () => {
  const clock = new ManualClock();
  const pool = new ConnectionPool({ clock, connect: async () => new FakeConnection('x') });
  assert.throws(() => pool.discover({ ...EVENT(), name: '' }), TypeError);
  assert.throws(() => pool.discover({ ...EVENT(), generation: -1 }), TypeError);
  assert.throws(() => pool.discover({ ...EVENT(), maxConnections: 0 }), TypeError);
  assert.throws(() => pool.discover({ ...EVENT(), address: '' }), TypeError);
  assert.equal(pool.stats().generations.length, 0);
});

test('discover: identical re-announcement is idempotent; stale gen is ignored', () => {
  const clock = new ManualClock();
  const pool = new ConnectionPool({ clock, connect: async () => new FakeConnection('x') });
  const first = pool.discover(EVENT({ generation: 2 }));
  assert.deepEqual(first, { key: 'svc#2', retired: null, ignored: false, duplicate: false });

  const again = pool.discover(EVENT({ generation: 2 }));
  assert.equal(again.duplicate, true);

  const stale = pool.discover(EVENT({ generation: 1 }));
  assert.equal(stale.ignored, true);
  const gens = pool.stats().generations.filter((g) => g.active);
  assert.equal(gens.length, 1);
  assert.equal(gens[0].generation, 2);
});

test('close: rejects queued waiters and closes idle connections', async () => {
  const clock = new ManualClock();
  const { connect, calls } = controllableConnect();
  const pool = new ConnectionPool({ clock, connect });
  pool.discover(EVENT({ maxConnections: 1 }));

  const held = await (async () => {
    const p = pool.borrow();
    await flushMicrotasks();
    calls[0].resolve();
    return p;
  })();
  const queued = pool.borrow();
  pool.close();

  await assertRejects(queued, PoolClosedError);
  await assertRejects(pool.borrow(), PoolClosedError);
  assert.throws(() => pool.discover(EVENT()), PoolClosedError);

  // A borrowed connection is only closed when it comes home.
  const closesBefore = pool.stats().closeEvents.length;
  held.return();
  const events = pool.stats().closeEvents;
  assert.equal(events.length, closesBefore + 1);
  assert.equal(events.at(-1).reason, 'returned-closed-pool');
  assert.equal(held.connection.closed, true);
});
