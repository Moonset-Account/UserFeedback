import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  VirtualClock,
  RealClock,
  MockBackend,
  LoadGenerator,
} from '../src/index.js';

/**
 * Build a fully wired virtual-time rig: the generator's completion handler is
 * handed to the mock backend, so completions flow back through the generator
 * at clock-controlled instants.
 */
function buildRig({ concurrency, queueCapacity, arrivals, serviceTime, slowFirst = 0, slowDuration }) {
  const clock = new VirtualClock();
  let generator;
  const backend = new MockBackend({
    serviceTime,
    slowFirst,
    slowDuration,
    clock,
    onComplete: (record) => generator.completionHandler(record),
  });
  generator = new LoadGenerator({ clock, backend, concurrency, queueCapacity, arrivals });
  generator.start();
  clock.run();
  return { clock, backend, generator };
}

const byId = (records) => Object.fromEntries(records.map((r) => [r.id, r]));

test('acceptance: first two requests slowed then backend recovers; arrival curve and later ids/times are untouched', () => {
  const arrivals = [
    { time: 0, id: 'A' },
    { time: 1, id: 'B' },
    { time: 2, id: 'C' },
    { time: 3, id: 'D' },
    { time: 4, id: 'E' },
  ];

  const { generator, backend } = buildRig({
    concurrency: 2,
    queueCapacity: 2,
    arrivals,
    serviceTime: 6,
    slowFirst: 2,
    slowDuration: 20,
  });

  // All five arrivals were offered to the generator; four completed, one drop.
  assert.equal(generator.completed.length, 4);
  assert.equal(generator.dropped.length, 1);
  assert.equal(backend.dispatchedCount, 4);

  const done = byId(generator.completed);

  // The slowdown only touched the first two *dispatched* requests.
  assert.equal(done.A.serviceMs, 20);
  assert.equal(done.B.serviceMs, 20);
  assert.equal(done.C.serviceMs, 6);
  assert.equal(done.D.serviceMs, 6);

  // Scheduled times are preserved verbatim — the arrival curve never moved.
  assert.deepEqual(
    generator.completed.map((r) => [r.id, r.scheduledAt]),
    [
      ['A', 0],
      ['B', 1],
      ['C', 2],
      ['D', 3],
    ],
  );
  assert.deepEqual(
    generator.dropped.map((r) => [r.id, r.scheduledAt, r.reason]),
    [['E', 4, 'queue-full']],
  );

  // Dispatch/completion timeline: the two slow requests finish at 20/21,
  // queued items are picked up exactly when slots release.
  assert.deepEqual(
    generator.completed.map((r) => [r.id, r.dispatchedAt, r.completedAt]),
    [
      ['A', 0, 20],
      ['B', 1, 21],
      ['C', 20, 26],
      ['D', 21, 27],
    ],
  );

  // Queue time is independently checkable from the three recorded instants.
  assert.equal(done.A.queueMs, 0);
  assert.equal(done.B.queueMs, 0);
  assert.equal(done.C.queueMs, 18); // dispatched 20, scheduled/arrived 2
  assert.equal(done.D.queueMs, 18); // dispatched 21, scheduled/arrived 3
  for (const r of generator.completed) {
    assert.equal(r.queueMs, r.dispatchedAt - r.scheduledAt);
    assert.equal(r.serviceMs, r.completedAt - r.dispatchedAt);
    assert.equal(r.arrivedAt, r.scheduledAt, 'open loop: arrival happens at the planned instant');
  }

  // The dropped item is recorded at its planned instant with full diagnostics.
  const drop = generator.dropped[0];
  assert.equal(drop.arrivedAt, 4);
  assert.equal(drop.inFlight, 2);
  assert.equal(drop.queued, 2);
  assert.equal(drop.queueCapacity, 2);

  // No pending virtual events left after the run.
  assert.equal(generator.inFlight, 0);
  assert.equal(generator.queuedCount, 0);
});

test('rejected path: slots full and zero-capacity queue record drops without shifting later arrivals', () => {
  const arrivals = [
    { time: 0, id: 'A' },
    { time: 1, id: 'B' },
    { time: 2, id: 'C' },
  ];

  const { generator } = buildRig({
    concurrency: 1,
    queueCapacity: 0,
    arrivals,
    serviceTime: 10,
  });

  assert.equal(generator.completed.length, 1);
  assert.equal(generator.dropped.length, 2);

  const done = byId(generator.completed);
  assert.deepEqual(done.A, {
    id: 'A',
    scheduledAt: 0,
    arrivedAt: 0,
    dispatchedAt: 0,
    completedAt: 10,
    serviceMs: 10,
    queueMs: 0,
    payload: undefined,
  });

  // Both rejected arrivals keep their original scheduled times — nothing was
  // postponed to t=10 when the slot frees up.
  assert.deepEqual(
    generator.dropped.map((r) => [r.id, r.scheduledAt, r.arrivedAt]),
    [
      ['B', 1, 1],
      ['C', 2, 2],
    ],
  );
  for (const d of generator.dropped) {
    assert.equal(d.reason, 'queue-full');
    assert.equal(d.queueCapacity, 0);
    assert.equal(d.inFlight, 1);
  }
});

test('boundary interleave: same-instant slot release serves the queued head before new arrivals', () => {
  // One slot, one queue slot. At t=10 three things coincide:
  //   - A's completion (priority 0) frees the slot and immediately dispatches B
  //   - arrivals C then D (priority 1, insertion order) run afterwards
  // C finds an empty queue and is admitted; D finds the queue full and drops.
  const arrivals = [
    { time: 0, id: 'A' },
    { time: 5, id: 'B' },
    { time: 10, id: 'C' },
    { time: 10, id: 'D' },
  ];

  const { generator } = buildRig({
    concurrency: 1,
    queueCapacity: 1,
    arrivals,
    serviceTime: 10,
  });

  assert.deepEqual(generator.dropped.map((r) => r.id), ['D']);
  assert.deepEqual(generator.completed.map((r) => r.id), ['A', 'B', 'C']);

  const done = byId(generator.completed);
  assert.deepEqual(
    [done.A.dispatchedAt, done.A.completedAt],
    [0, 10],
  );
  assert.deepEqual(
    [done.B.dispatchedAt, done.B.completedAt],
    [10, 20],
    'queued head is picked up by the release before the t=10 arrivals',
  );
  assert.deepEqual(
    [done.C.scheduledAt, done.C.dispatchedAt, done.C.completedAt],
    [10, 20, 30],
  );
  assert.equal(done.C.queueMs, 10);

  // Under arrival-first ordering C would have been the drop instead of D;
  // this pins the required release-before-arrival semantics at the boundary.
  assert.equal(generator.dropped[0].scheduledAt, 10);
  assert.equal(generator.dropped[0].queued, 1, 'C already occupies the queue when D is evaluated');
});

test('boundary interleave: zero wait with queue capacity 0 still admits when release and arrival share a timestamp', () => {
  const arrivals = [
    { time: 0, id: 'A' },
    { time: 10, id: 'B' },
  ];

  const { generator } = buildRig({
    concurrency: 1,
    queueCapacity: 0,
    arrivals,
    serviceTime: 10,
  });

  // B arrives exactly when A completes; the completion runs first, so B gets
  // the freed slot immediately instead of being dropped.
  assert.equal(generator.dropped.length, 0);
  assert.deepEqual(generator.completed.map((r) => r.id), ['A', 'B']);
  const done = byId(generator.completed);
  assert.equal(done.B.queueMs, 0);
  assert.deepEqual([done.B.scheduledAt, done.B.dispatchedAt], [10, 10]);
});

test('arrivals with equal timestamps are evaluated in declaration order against the bounded queue', () => {
  const arrivals = [
    { time: 0, id: 'A' },
    { time: 0, id: 'B' },
    { time: 0, id: 'C' },
    { time: 0, id: 'D' },
  ];

  const { generator } = buildRig({
    concurrency: 2,
    queueCapacity: 1,
    arrivals,
    serviceTime: 5,
  });

  assert.deepEqual(generator.completed.map((r) => r.id), ['A', 'B', 'C']);
  assert.deepEqual(generator.dropped.map((r) => r.id), ['D']);
  // All four arrival events fired at the same planned instant.
  assert.equal(generator.dropped[0].arrivedAt, 0);
});

test('functional service-time policy and payload propagation', () => {
  const arrivals = [
    { time: 0, id: 'A', payload: { weight: 1 } },
    { time: 1, id: 'B', payload: { weight: 10 } },
  ];

  const { generator } = buildRig({
    concurrency: 2,
    queueCapacity: 0,
    arrivals,
    serviceTime: (request) => request.payload.weight * 2,
  });

  const done = byId(generator.completed);
  assert.equal(done.A.serviceMs, 2);
  assert.equal(done.B.serviceMs, 20);
  assert.deepEqual(done.B.payload, { weight: 10 });
});

test('input validation rejects unusable configurations before any event fires', () => {
  const clock = new VirtualClock();
  const backend = new MockBackend({
    serviceTime: 1,
    clock,
    onComplete: () => {},
  });
  const base = { clock, backend, concurrency: 1, queueCapacity: 0, arrivals: [{ time: 0 }] };

  assert.throws(() => new LoadGenerator({ ...base, concurrency: 0 }), /concurrency/);
  assert.throws(() => new LoadGenerator({ ...base, concurrency: 1.5 }), /concurrency/);
  assert.throws(() => new LoadGenerator({ ...base, queueCapacity: -1 }), /queueCapacity/);
  assert.throws(
    () => new LoadGenerator({ ...base, arrivals: [{ time: 2 }, { time: 1 }] }),
    /earlier than a previous arrival/,
  );
  assert.throws(
    () => new LoadGenerator({ ...base, arrivals: [{ time: 0, id: 'x' }, { time: 1, id: 'x' }] }),
    /duplicate arrival id/,
  );
  assert.throws(
    () => new LoadGenerator({ ...base, arrivals: [{ time: -1 }] }),
    /finite non-negative/,
  );
  assert.throws(
    () => new MockBackend({ serviceTime: -1, clock, onComplete: () => {} }),
    /serviceTime/,
  );
  assert.throws(
    () => new MockBackend({ serviceTime: 1, slowFirst: 1, clock, onComplete: () => {} }),
    /slowDuration/,
  );
});

test('real clock smoke: open-loop drops and completion accounting hold with actual timers', async () => {
  const clock = new RealClock();
  // Arrival times live in the clock's own time domain (performance.now()),
  // so anchor the schedule to the current instant.
  const t0 = clock.now();
  let generator;
  const backend = new MockBackend({
    serviceTime: 5,
    clock,
    onComplete: (record) => generator.completionHandler(record),
  });
  generator = new LoadGenerator({
    clock,
    backend,
    concurrency: 1,
    queueCapacity: 0,
    arrivals: [
      { time: t0, id: 'A' },
      { time: t0 + 2, id: 'B' },
      { time: t0 + 4, id: 'C' },
    ],
  });
  generator.start();
  await clock.idle();

  assert.equal(generator.completed.length, 1);
  assert.equal(generator.dropped.length, 2);
  assert.deepEqual(generator.dropped.map((r) => r.id), ['B', 'C']);
  const only = generator.completed[0];
  assert.equal(only.id, 'A');
  // Real timers fire with drift: the event order and accounting are exact,
  // the wall-clock instants only need to land within a generous tolerance.
  assert.ok(only.arrivedAt >= t0);
  assert.ok(only.arrivedAt - t0 < 25, `arrival drift too large: ${only.arrivedAt - t0}ms`);
  assert.ok(only.completedAt >= only.dispatchedAt);
  assert.ok(only.completedAt - only.dispatchedAt >= 4);
  // Only virtual time can guarantee zero queue time; under real timers the
  // first dispatch may lag its schedule by a few ms of scheduling drift.
  assert.ok(only.queueMs < 25, `unexpected queue delay: ${only.queueMs}ms`);
  const expectedDropTimes = { B: t0 + 2, C: t0 + 4 };
  for (const drop of generator.dropped) {
    const drift = drop.arrivedAt - expectedDropTimes[drop.id];
    assert.ok(drift >= -25 && drift < 25, `${drop.id} arrival drift out of tolerance: ${drift}ms`);
  }
  assert.equal(clock.pendingCount(), 0);
});
