import { test } from 'node:test';
import assert from 'node:assert/strict';
import { ManualClock, systemClock } from '../src/clock.mjs';

test('ManualClock: starts at configured time and advances manually', () => {
  const clock = new ManualClock(100);
  assert.equal(clock.now(), 100);
  clock.advance(50);
  assert.equal(clock.now(), 150);
});

test('ManualClock: timers only fire when the clock passes their due time, in order', () => {
  const clock = new ManualClock();
  const fired = [];
  clock.setTimeout(() => fired.push('late'), 100);
  clock.setTimeout(() => fired.push('early'), 10);
  clock.setTimeout(() => fired.push('fifty'), 50);

  clock.advance(20);
  assert.deepEqual(fired, ['early']);

  clock.advance(80);
  assert.deepEqual(fired, ['early', 'fifty', 'late']);
});

test('ManualClock: zero-delay timers fire on the next advance', () => {
  const clock = new ManualClock(5);
  let ran = false;
  clock.setTimeout(() => {
    ran = true;
  }, 0);
  assert.equal(ran, false);
  clock.advance(0);
  assert.equal(ran, true);
});

test('ManualClock: clearTimeout prevents firing', () => {
  const clock = new ManualClock();
  let count = 0;
  const t = clock.setTimeout(() => {
    count += 1;
  }, 10);
  clock.clearTimeout(t);
  clock.advance(100);
  assert.equal(count, 0);
  assert.equal(clock.pendingTimerCount, 0);
});

test('ManualClock: due-time ties keep insertion order', () => {
  const clock = new ManualClock();
  const order = [];
  clock.setTimeout(() => order.push(1), 5);
  clock.setTimeout(() => order.push(2), 5);
  clock.setTimeout(() => order.push(3), 5);
  clock.advance(5);
  assert.deepEqual(order, [1, 2, 3]);
});

test('ManualClock: a timer rescheduling itself eventually terminates', () => {
  const clock = new ManualClock();
  let remaining = 3;
  const recur = () => {
    remaining -= 1;
    if (remaining > 0) clock.setTimeout(recur, 10);
  };
  clock.setTimeout(recur, 10);
  // Each advance only fires timers registered before it began, so a
  // self-rescheduling timer needs one advance per generation.
  clock.advance(10);
  clock.advance(10);
  clock.advance(10);
  assert.equal(remaining, 0);
  assert.equal(clock.pendingTimerCount, 0);
});

test('ManualClock: rejects invalid construction and advance arguments', () => {
  assert.throws(() => new ManualClock(-1), TypeError);
  assert.throws(() => new ManualClock(1.5), TypeError);
  const clock = new ManualClock();
  assert.throws(() => clock.advance(-1), TypeError);
  assert.throws(() => clock.advance(1.5), TypeError);
});

test('ManualClock.tick: drains promise microtasks after moving the clock', async () => {
  const clock = new ManualClock();
  let microtaskRan = false;
  clock.setTimeout(() => {
    Promise.resolve().then(() => {
      microtaskRan = true;
    });
  }, 10);
  await clock.tick(10);
  assert.equal(microtaskRan, true);
});

test('systemClock exposes the same interface shape', () => {
  assert.equal(typeof systemClock.now, 'function');
  assert.equal(typeof systemClock.setTimeout, 'function');
  assert.equal(typeof systemClock.clearTimeout, 'function');
  const t = systemClock.setTimeout(() => {}, 1000);
  systemClock.clearTimeout(t);
});
