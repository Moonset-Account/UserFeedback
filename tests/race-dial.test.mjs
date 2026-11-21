import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import { raceDial, AllAddressesFailedError } from '../src/race-dial.js';
import { FakeClock, flushMicrotasks } from './helpers/fake-clock.js';
import { FakeDial, makeConnection, makeFailingCloseConnection } from './helpers/fake-dial.js';

const D = 250;
const V6 = ['2001:db8::1', '2001:db8::2', '2001:db8::3'];
const V4 = ['192.0.2.1', '192.0.2.2'];

function setup({ ipv6 = V6, ipv4 = V4, delayMs = D, dial = null, clock = null } = {}) {
  const fakeClock = clock ?? new FakeClock();
  const fakeDial = dial ?? new FakeDial(fakeClock);
  const promise = raceDial({
    ipv6,
    ipv4,
    delayMs,
    dial: fakeDial.fn,
    clock: fakeClock,
  });
  return { fakeClock, fakeDial, promise };
}

// Race promise helper that also drains pending microtask reactions.
async function settleRace(promise) {
  await flushMicrotasks();
  return promise;
}

describe('normal scenarios', () => {
  test('first address dials immediately at t0, before any timer elapses', async () => {
    const { fakeDial, fakeClock, promise } = setup();

    assert.equal(fakeDial.count, 1);
    assert.equal(fakeDial.at(0).address, V6[0]);
    assert.equal(fakeDial.at(0).family, 'ipv6');
    assert.equal(fakeDial.at(0).dialedAt, 0);
    assert.equal(fakeClock.pendingTimers(), 1);

    // Advancing by less than D opens no further dial.
    fakeClock.advance(D - 1);
    await flushMicrotasks();
    assert.equal(fakeDial.count, 1);
    assert.equal(fakeClock.now(), D - 1);

    fakeDial.at(0).resolve();
    const result = await settleRace(promise);
    assert.equal(result.address, V6[0]);
    assert.equal(result.family, 'ipv6');
    assert.equal(result.dialedAt, 0);
    assert.equal(result.connection, fakeDial.at(0).connection);
  });

  test('first success wins and cancels every other in-flight attempt', async () => {
    const { fakeClock, fakeDial, promise } = setup();

    fakeClock.advance(D); // timer fires -> second dial (ipv4-1)
    await flushMicrotasks();
    assert.equal(fakeDial.count, 2);
    assert.equal(fakeDial.at(1).address, V4[0]);

    fakeDial.at(0).resolve(); // first-dialed IPv6 wins
    const result = await settleRace(promise);

    assert.equal(result.address, V6[0]);
    assert.equal(fakeDial.at(1).signal.aborted, true);
    assert.equal(fakeDial.pending().length, 0);
    assert.equal(fakeClock.pendingTimers(), 0);
    // Winning connection is handed over, not closed.
    assert.equal(fakeDial.at(0).connection.closed, false);
  });

  test('with no winner, timed dials open at most one more every D ms', async () => {
    const { fakeClock, fakeDial } = setup({
      ipv6: ['a', 'b', 'c', 'd', 'e'],
      ipv4: ['1', '2', '3'],
    });

    fakeClock.advance(D);
    await flushMicrotasks();
    assert.equal(fakeDial.count, 2);
    assert.deepEqual(
      fakeDial.attempts.map((a) => a.dialedAt),
      [0, D],
    );

    fakeClock.advance(D - 1);
    await flushMicrotasks();
    assert.equal(fakeDial.count, 2); // still inside the interval

    fakeClock.advance(1); // t = 2D: timer due, but both slots are full -> skip
    await flushMicrotasks();
    assert.equal(fakeDial.count, 2);
    assert.equal(fakeDial.at(1).dialedAt, D);

    // A failed attempt frees a slot immediately; the next candidate dials at
    // once and the timer re-times from this instant.
    fakeDial.at(0).reject(new Error('a down'));
    await flushMicrotasks();
    assert.equal(fakeDial.count, 3);
    assert.equal(fakeDial.at(2).dialedAt, 2 * D);

    // The skipped tick had re-armed at 2D; with two slots full again the tick
    // at 3D is skipped as well, keeping the in-flight count at the cap of two.
    fakeClock.advance(D);
    await flushMicrotasks();
    assert.equal(fakeDial.count, 3);
    assert.equal(fakeDial.pending().length, 2);
  });

  test('failure frees a slot immediately and re-times the next timed dial', async () => {
    const { fakeClock, fakeDial } = setup({
      ipv6: ['a', 'b', 'c', 'd'],
      ipv4: ['1', '2'],
    });

    fakeClock.advance(D); // attempts: a(t0), 1(tD)
    await flushMicrotasks();
    assert.equal(fakeDial.count, 2);

    fakeClock.advance(100); // t = D+100, no timer due yet
    fakeDial.at(0).reject(new Error('boom-a'));
    await flushMicrotasks();

    // Immediate replacement dial, started at t = D+100.
    assert.equal(fakeDial.count, 3);
    assert.equal(fakeDial.at(2).address, 'b');
    assert.equal(fakeDial.at(2).dialedAt, D + 100);
    assert.equal(fakeDial.pending().length, 2); // b and 1

    // The old schedule (due at 2D) must be superseded: nothing new at 2D.
    fakeClock.advance(D - 100 - 1); // t = 2D - 1
    await flushMicrotasks();
    assert.equal(fakeDial.count, 3);

    fakeClock.advance(1); // t = 2D, still nothing (re-timed to 2D+100)
    await flushMicrotasks();
    assert.equal(fakeDial.count, 3);

    fakeClock.advance(100); // t = 2D+100, re-timed timer fires
    await flushMicrotasks();
    // Slots are full (b and 1), so the tick is skipped by the two-dial cap.
    assert.equal(fakeDial.count, 3);
    assert.equal(fakeDial.pending().length, 2);

    // Failing an in-flight attempt frees a slot: the next candidate dials
    // immediately at the same instant, without waiting for another D window.
    fakeDial.at(1).reject(new Error('boom-1'));
    await flushMicrotasks();
    assert.equal(fakeDial.count, 4);
    assert.equal(fakeDial.at(3).dialedAt, 2 * D + 100);
    assert.equal(fakeDial.pending().length, 2);
  });

  test('concurrency never exceeds two; full timer ticks are skipped, failures replace', async () => {
    const { fakeClock, fakeDial } = setup({
      ipv6: ['a', 'b', 'c'],
      ipv4: ['1', '2', '3'],
    });

    fakeClock.advance(D); // a(t0), 1(tD) — two slots full
    await flushMicrotasks();
    assert.equal(fakeDial.pending().length, 2);

    fakeClock.advance(D); // 2D tick: both slots full -> tick skipped, re-armed
    await flushMicrotasks();
    assert.equal(fakeDial.count, 2);

    fakeClock.advance(50); // 2D+50: one attempt fails
    fakeDial.at(1).reject(new Error('boom-1'));
    await flushMicrotasks();

    assert.equal(fakeDial.count, 3); // immediate replacement b
    assert.equal(fakeDial.at(2).dialedAt, 2 * D + 50);
    assert.equal(fakeDial.pending().length, 2); // a and b: slots full again

    // At 3D+50 the re-timed timer fires, but both slots are still full, so the
    // tick is skipped and no fourth dial opens (the two-connection cap holds).
    fakeClock.advance(D); // t = 3D+50
    await flushMicrotasks();
    assert.equal(fakeDial.count, 3);
    assert.equal(fakeDial.pending().length, 2);

    // A failure frees a slot; the next candidate starts immediately and the
    // timer is re-timed from that instant instead of waiting out the interval.
    fakeDial.at(0).reject(new Error('boom-a'));
    await flushMicrotasks();
    assert.equal(fakeDial.count, 4);
    // Selector order a,1,b,2,c,3 -> after a,1,b the next candidate is 2.
    assert.equal(fakeDial.at(3).address, '2');
    assert.equal(fakeDial.at(3).dialedAt, 3 * D + 50);
    // No moment ever had more than two attempts in flight.
    assert.ok(fakeDial.pending().length <= 2);
  });
});

describe('boundary scenarios', () => {
  test('all addresses fail: failures are reported in dial start order', async () => {
    const { fakeClock, fakeDial, promise } = setup({
      ipv6: ['a', 'b'],
      ipv4: ['1'],
    });

    fakeClock.advance(D);
    await flushMicrotasks();
    assert.equal(fakeDial.count, 2); // a, 1

    // Reject out of notification order: second-started attempt fails first.
    fakeDial.at(1).reject(Object.assign(new Error('e1'), { tag: 'e1' }));
    await flushMicrotasks();
    assert.equal(fakeDial.count, 3); // b started immediately

    fakeDial.at(2).reject(Object.assign(new Error('e2'), { tag: 'e2' }));
    fakeDial.at(0).reject(Object.assign(new Error('e0'), { tag: 'e0' }));

    await assert.rejects(settleRace(promise), (err) => {
      assert.ok(err instanceof AllAddressesFailedError);
      assert.equal(err.failures.length, 3);
      assert.deepEqual(
        err.failures.map((f) => f.address),
        ['a', '1', 'b'],
      );
      assert.deepEqual(
        err.failures.map((f) => f.error.tag),
        ['e0', 'e1', 'e2'],
      );
      assert.deepEqual(
        err.failures.map((f) => f.sequence),
        [0, 1, 2],
      );
      return true;
    });
  });

  test('empty candidate lists reject immediately with an empty failure list', async () => {
    const clock = new FakeClock();
    const fakeDial = new FakeDial(clock);
    const promise = raceDial({ ipv6: [], ipv4: [], delayMs: D, dial: fakeDial.fn, clock });

    await assert.rejects(promise, (err) => {
      assert.ok(err instanceof AllAddressesFailedError);
      assert.deepEqual(err.failures, []);
      return true;
    });
    assert.equal(fakeDial.count, 0);
    assert.equal(clock.pendingTimers(), 0);
  });

  test('same settling batch: earlier-dialed success wins even if its reaction arrives later', async () => {
    const { fakeClock, fakeDial, promise } = setup({
      ipv6: ['a', 'b'],
      ipv4: ['1'],
    });
    fakeClock.advance(D); // a(0), 1(D)
    await flushMicrotasks();

    // Notifications arrive reversed relative to start order within one batch.
    fakeDial.at(1).resolve();
    fakeDial.at(0).resolve();

    const result = await settleRace(promise);
    assert.equal(result.address, 'a'); // seq 0 beats seq 1
    assert.equal(fakeDial.at(1).connection.closed, true);
    assert.equal(fakeDial.at(0).connection.closed, false);
  });

  test('success observed after cancellation is closed and never returned', async () => {
    // A dial that ignores AbortSignal: its promise only settles when the
    // simulated remote answers, even after the dialer canceled the attempt.
    const clock = new FakeClock();
    const deferreds = new Map();
    const dial = (address) => {
      const conn = makeConnection(address);
      const d = {};
      d.promise = new Promise((resolve, reject) => {
        d.resolve = () => resolve(conn);
        d.reject = reject;
      });
      d.connection = conn;
      deferreds.set(address, d);
      return d.promise;
    };
    const promise = raceDial({
      ipv6: ['a', 'b'],
      ipv4: ['1'],
      delayMs: D,
      dial,
      clock,
    });
    clock.advance(D); // a(0), 1(D)
    await flushMicrotasks();

    deferreds.get('a').resolve(); // a wins -> attempt 1 is canceled
    const result = await settleRace(promise);
    assert.equal(result.address, 'a');

    // The "remote" answers anyway after cancellation: must be closed.
    deferreds.get('1').resolve();
    await flushMicrotasks();
    assert.equal(deferreds.get('1').connection.closed, true);
    assert.equal(deferreds.get('1').connection.closeCount, 1);
    assert.equal(result.connection.closed, false);
  });

  test('late rejection after the race is finalized does not change the result', async () => {
    const { fakeClock, fakeDial, promise } = setup({
      ipv6: ['a'],
      ipv4: ['1'],
    });
    fakeClock.advance(D);
    await flushMicrotasks();
    fakeDial.at(0).resolve();
    const result = await settleRace(promise);
    assert.equal(result.address, 'a');

    fakeDial.at(1).forceReject(new Error('late failure'));
    await flushMicrotasks();
    assert.equal(result.address, 'a'); // unchanged
  });

  test('delayMs of 0 still dials one per timer tick with max two concurrent', async () => {
    const { fakeClock, fakeDial, promise } = setup({
      ipv6: ['a', 'b', 'c', 'd'],
      ipv4: ['1'],
      delayMs: 0,
    });
    assert.equal(fakeDial.count, 1); // immediate dial, no synchronous burst

    fakeClock.advance(0);
    await flushMicrotasks();
    assert.equal(fakeDial.count, 2);

    fakeClock.advance(0);
    await flushMicrotasks();
    assert.equal(fakeDial.count, 2); // two slots full: tick skipped

    // Failures free slots and immediately replace; each failure re-times the
    // (zero-delay) timer, so the whole candidate list is drained this way.
    fakeDial.at(1).reject(new Error('x'));
    await flushMicrotasks();
    assert.equal(fakeDial.count, 3); // immediate replacement

    fakeDial.at(0).reject(new Error('y'));
    await flushMicrotasks();
    assert.equal(fakeDial.count, 4);

    fakeDial.at(2).reject(new Error('z'));
    await flushMicrotasks();
    assert.equal(fakeDial.count, 5); // all five candidates have now been dialed
    assert.deepEqual(
      fakeDial.attempts.map((a) => a.address),
      ['a', '1', 'b', 'c', 'd'],
    );

    fakeDial.at(3).reject(new Error('x'));
    fakeDial.at(4).reject(new Error('x'));
    // All candidates are dialed; the last in-flight rejection finalizes the
    // race and clears any remaining timer.
    await assert.rejects(settleRace(promise), (err) => {
      assert.ok(err instanceof AllAddressesFailedError);
      assert.deepEqual(
        err.failures.map((f) => f.address),
        ['a', '1', 'b', 'c', 'd'],
      );
      return true;
    });
    assert.equal(fakeClock.pendingTimers(), 0);
  });

  test('synchronous throw from dial is treated as a failed attempt and replaced', async () => {
    const clock = new FakeClock();
    const calls = [];
    const fakeDial = new FakeDial(clock);
    const throwingDial = (address, family, signal) => {
      calls.push(address);
      if (address === 'a') throw new Error('sync boom');
      return fakeDial.fn(address, family, signal);
    };
    const promise = raceDial({
      ipv6: ['a', 'b'],
      ipv4: ['1'],
      delayMs: D,
      dial: throwingDial,
      clock,
    });

    assert.deepEqual(calls, ['a']);
    await flushMicrotasks();
    // The failed first slot is immediately replaced by the next candidate.
    assert.equal(fakeDial.count, 1);
    assert.equal(fakeDial.at(0).address, '1'); // alternating selector: ipv4 next
    assert.equal(fakeDial.at(0).dialedAt, 0);

    clock.advance(D);
    await flushMicrotasks();
    assert.equal(fakeDial.at(1).address, 'b');

    fakeDial.at(0).reject(new Error('e1'));
    fakeDial.at(1).reject(new Error('e2'));
    await assert.rejects(settleRace(promise), (err) => {
      assert.ok(err instanceof AllAddressesFailedError);
      assert.deepEqual(
        err.failures.map((f) => f.address),
        ['a', '1', 'b'],
      );
      assert.equal(err.failures[0].error.message, 'sync boom');
      return true;
    });
  });

  test('at the same instant, returned results are processed before the due timer', async () => {
    const { fakeClock, fakeDial } = setup({
      ipv6: ['a', 'b', 'c'],
      ipv4: ['1', '2'],
    });
    clockTickScenario: {
      // a(0), 1(D). At 2D both the second timer is due and attempt 0 fails.
      fakeClock.advance(D);
      await flushMicrotasks();
      assert.equal(fakeDial.count, 2);

      fakeDial.at(0).reject(new Error('a fails at 2D')); // reaction queued first
      fakeClock.advance(D); // timer callback is dispatched after the rejection
      await flushMicrotasks();

      // Rejection replacement started b; the stale timer generation must not
      // open yet another dial at the same instant.
      assert.equal(fakeDial.count, 3);
      assert.equal(fakeDial.at(2).address, 'b');
      assert.equal(fakeDial.at(2).dialedAt, 2 * D);
      assert.equal(fakeDial.pending().length, 2); // 1 and b
      break clockTickScenario;
    }
  });

  test('a success landing in the same tick as a due timer wins and clears the timer', async () => {
    const { fakeClock, fakeDial, promise } = setup({
      ipv6: ['a', 'b'],
      ipv4: ['1', '2'],
    });
    fakeClock.advance(D); // a(0), 1(D)
    await flushMicrotasks();

    fakeDial.at(1).resolve(); // success notification queued before timer dispatch
    fakeClock.advance(D); // timer due at 2D, but a winner now exists
    await flushMicrotasks();

    const result = await settleRace(promise);
    assert.equal(result.address, '1');
    assert.equal(fakeDial.count, 2); // no third dial
    assert.equal(fakeClock.pendingTimers(), 0);
    assert.equal(fakeDial.at(0).signal.aborted, true);
  });

  test('invalid parameters are rejected with TypeError synchronously', () => {
    const clock = new FakeClock();
    const fakeDial = new FakeDial(clock);
    assert.throws(
      () => raceDial({ ipv6: ['a'], delayMs: -1, dial: fakeDial.fn, clock }),
      TypeError,
    );
    assert.throws(
      () => raceDial({ ipv6: ['a'], delayMs: NaN, dial: fakeDial.fn, clock }),
      TypeError,
    );
    assert.throws(
      () => raceDial({ ipv6: ['a'], delayMs: D, dial: 'nope', clock }),
      TypeError,
    );
    assert.throws(
      () => raceDial({ ipv6: ['a'], delayMs: D, dial: fakeDial.fn, clock: null }),
      TypeError,
    );
  });

  test('a connection whose close() rejects does not affect the race outcome', async () => {
    const clock = new FakeClock();
    const dial = (address) => Promise.resolve(makeFailingCloseConnection(address));
    const promise = raceDial({
      ipv6: ['a', 'b'],
      ipv4: [],
      delayMs: D,
      dial,
      clock,
    });
    clock.advance(D);
    await flushMicrotasks();
    const result = await promise;
    assert.equal(result.address, 'a');
    // Give the best-effort close rejection a microtask to be swallowed.
    await flushMicrotasks();
    assert.equal(result.connection.label, 'a');
  });

  test('default real clock integration smoke test (no injection)', async () => {
    const dial = (address) => {
      if (address === 'slow-v6') {
        return new Promise((resolve, reject) => {
          // Fails after a short delay, exercising the immediate-replace path.
          setTimeout(() => reject(new Error('v6 down')), 20);
        });
      }
      return Promise.resolve(makeConnection(address));
    };
    const result = await raceDial({
      ipv6: ['slow-v6'],
      ipv4: ['192.0.2.9'],
      delayMs: 10,
      dial,
    });
    assert.equal(result.family, 'ipv4');
    assert.equal(result.address, '192.0.2.9');
  });
});

describe('interleaving scenarios', () => {
  test('real dial sequence follows IPv6-first alternation, then drains one family', async () => {
    const { fakeClock, fakeDial, promise } = setup({
      ipv6: ['a', 'b', 'c'],
      ipv4: ['1'],
    });

    // Full dial order from the selector: a, 1, b, c.
    const sequence = [];
    for (const attempt of fakeDial.attempts) sequence.push(attempt.address);

    fakeClock.advance(D); // 1 starts
    await flushMicrotasks();
    sequence.push(fakeDial.at(1).address);

    fakeDial.at(0).reject(new Error('a')); // b starts immediately at D
    await flushMicrotasks();
    sequence.push(fakeDial.at(2).address);
    assert.equal(fakeDial.at(2).dialedAt, D);

    // Slots are full again (1 and b), so the timer at 2D is skipped; rejecting
    // both in-flight attempts frees slots and c starts immediately at 2D.
    fakeClock.advance(D);
    await flushMicrotasks();
    assert.equal(fakeDial.count, 3); // skipped tick opened nothing
    assert.equal(fakeClock.now(), 2 * D);

    fakeDial.at(1).reject(new Error('1'));
    fakeDial.at(2).reject(new Error('b'));
    await flushMicrotasks();
    sequence.push(fakeDial.at(3).address);
    assert.equal(fakeDial.at(3).dialedAt, 2 * D);

    assert.deepEqual(sequence, ['a', '1', 'b', 'c']);
    assert.deepEqual(
      fakeDial.attempts.map((a) => a.family),
      ['ipv6', 'ipv4', 'ipv6', 'ipv6'],
    );

    fakeDial.at(3).reject(new Error('c'));
    await assert.rejects(settleRace(promise), AllAddressesFailedError);
  });

  test('alternating selection across repeated failures with the two-slot cap', async () => {
    const { fakeClock, fakeDial, promise } = setup({
      ipv6: ['v6a', 'v6b'],
      ipv4: ['v4a', 'v4b', 'v4c'],
    });
    // Expected selector order: v6a, v4a, v6b, v4b, v4c.

    fakeClock.advance(D); // v4a starts at D
    await flushMicrotasks();
    assert.equal(fakeDial.at(1).address, 'v4a');

    fakeDial.at(0).reject(new Error('x')); // v6b immediately
    await flushMicrotasks();
    assert.equal(fakeDial.at(2).address, 'v6b');

    fakeDial.at(1).reject(new Error('x')); // v4b immediately at D
    await flushMicrotasks();
    assert.equal(fakeDial.at(3).address, 'v4b');
    assert.equal(fakeDial.at(3).dialedAt, D);

    // Slots are full (v6b, v4b); the timer due at 2D is skipped and re-armed.
    fakeClock.advance(D);
    await flushMicrotasks();
    assert.equal(fakeDial.count, 4); // skipped tick opened nothing

    // Failing one in-flight attempt frees a slot and starts v4c immediately,
    // since v4c is the only remaining candidate.
    fakeDial.at(2).reject(new Error('x'));
    await flushMicrotasks();
    assert.equal(fakeDial.at(4).address, 'v4c');
    assert.equal(fakeDial.at(4).dialedAt, 2 * D);

    assert.ok(fakeDial.attempts.every((a) => a.dialedAt !== null));
    assert.deepEqual(
      fakeDial.attempts.map((a) => a.address),
      ['v6a', 'v4a', 'v6b', 'v4b', 'v4c'],
    );

    for (const a of fakeDial.attempts.slice(3)) a.reject(new Error('x'));
    await assert.rejects(settleRace(promise), (err) => {
      assert.deepEqual(
        err.failures.map((f) => f.address),
        ['v6a', 'v4a', 'v6b', 'v4b', 'v4c'],
      );
      return true;
    });
  });

  test('IPv6-only and IPv4-only inputs are consumed in input order', async () => {
    const clock6 = new FakeClock();
    const dial6 = new FakeDial(clock6);
    const p6 = raceDial({ ipv6: ['a', 'b', 'c'], ipv4: [], delayMs: D, dial: dial6.fn, clock: clock6 });
    clock6.advance(D);
    await flushMicrotasks();
    assert.deepEqual(dial6.attempts.map((a) => a.address), ['a', 'b']);
    dial6.at(0).reject(new Error('x'));
    await flushMicrotasks();
    assert.equal(dial6.at(2).address, 'c');
    dial6.at(1).reject(new Error('x'));
    dial6.at(2).reject(new Error('x'));
    await assert.rejects(settleRace(p6), (err) => {
      assert.deepEqual(err.failures.map((f) => f.address), ['a', 'b', 'c']);
      return true;
    });

    const clock4 = new FakeClock();
    const dial4 = new FakeDial(clock4);
    const p4 = raceDial({ ipv6: [], ipv4: ['1', '2'], delayMs: D, dial: dial4.fn, clock: clock4 });
    assert.equal(dial4.at(0).family, 'ipv4');
    clock4.advance(D);
    await flushMicrotasks();
    dial4.at(0).resolve();
    const result = await settleRace(p4);
    assert.equal(result.address, '1');
    assert.equal(dial4.at(1).signal.aborted, true);
  });

  test('winner from IPv4 after IPv6 candidates have failed preserves dial metadata', async () => {
    const { fakeClock, fakeDial, promise } = setup({
      ipv6: ['v6-1', 'v6-2'],
      ipv4: ['v4-1', 'v4-2'],
    });
    fakeClock.advance(D); // v6-1(t0), v4-1(tD)
    await flushMicrotasks();
    fakeDial.at(0).reject(new Error('v6-1 down')); // v6-2 starts immediately at tD
    await flushMicrotasks();
    assert.equal(fakeDial.at(2).address, 'v6-2');
    assert.equal(fakeDial.at(2).dialedAt, D);

    // Slots are full again (v4-1, v6-2), so the 2D timer tick is skipped.
    fakeClock.advance(D);
    await flushMicrotasks();
    assert.equal(fakeDial.count, 3);

    // v6-2 failing frees a slot; the last candidate v4-2 dials immediately.
    fakeDial.at(2).reject(new Error('v6-2 down'));
    await flushMicrotasks();
    assert.equal(fakeDial.at(3).address, 'v4-2');
    assert.equal(fakeDial.at(3).dialedAt, 2 * D);

    fakeDial.at(1).resolve(); // v4-1 wins
    const result = await settleRace(promise);

    assert.equal(result.address, 'v4-1');
    assert.equal(result.family, 'ipv4');
    assert.equal(result.dialedAt, D);
    assert.equal(fakeDial.at(3).signal.aborted, true);
  });
});
