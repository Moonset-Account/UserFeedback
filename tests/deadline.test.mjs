import assert from 'node:assert/strict';
import { describe, it } from 'node:test';
import { EdgeCache } from '../src/edge-cache.js';
import { TimeoutError } from '../src/errors.js';
import { createClock, flushMicrotasks } from './helpers/clock.js';
import { createControlledOrigin, originResponse } from './helpers/controlled-origin.js';

const URL = 'https://edge.example.test/item';

/** Attach a no-op rejection handler so timer-driven rejects stay handled. */
function observe(promise) {
  promise.catch(() => {});
  return promise;
}

describe('independent waiter deadlines', () => {
  const clock = createClock();

  it('each waiter times out independently without cancelling the shared call', async () => {
    const controlled = createControlledOrigin();
    const cache = new EdgeCache({ origin: controlled.origin, now: clock.now, setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout });

    const seed = cache.fetch({ url: URL });
    await flushMicrotasks();
    controlled.calls[0].gate.resolve(originResponse({
      body: 'v1', etag: '"e1"', freshMs: 10, staleMs: 5000,
    }));
    await seed;
    clock.advance(50);

    observe(cache.fetch({ url: URL, deadlineMs: 20, maxStaleMs: 5000 }));
    const slow = observe(cache.fetch({ url: URL, deadlineMs: 100, maxStaleMs: 5000 }));
    await flushMicrotasks();
    assert.equal(controlled.callCount(), 2, 'coalesced onto one origin call');

    // Fast waiter deadline fires: stale fallback, upstream call continues.
    await clock.tick(25);
    // The fast promise already resolved via the timer; re-fetch its result
    // through the recorded waiter outcome by awaiting a fresh microtask.
    await flushMicrotasks();
    assert.equal(cache.getStats().timeouts, 1);
    assert.equal(cache.inflightCount(), 1, 'the shared upstream op is still running');

    // The origin finally answers; the slow waiter gets a real revalidation.
    controlled.calls[1].gate.resolve(originResponse({ status: 304, body: null }));
    const slowOut = await slow;
    assert.equal(slowOut.revalidated, true);
    assert.equal(slowOut.stale, false);
    assert.equal(slowOut.body, 'v1');
    assert.equal(controlled.callCount(), 2);
    assert.equal(cache.inflightCount(), 0);
  });

  it('serves stale to an expired waiter while its stale window allows it', async () => {
    const controlled = createControlledOrigin();
    const cache = new EdgeCache({ origin: controlled.origin, now: clock.now, setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout });

    const seed = cache.fetch({ url: URL });
    await flushMicrotasks();
    controlled.calls[0].gate.resolve(originResponse({
      body: 'v1', etag: '"e1"', freshMs: 10, staleMs: 5000,
    }));
    await seed;
    clock.advance(50);

    const fast = observe(cache.fetch({ url: URL, deadlineMs: 20, maxStaleMs: 5000 }));
    await flushMicrotasks();
    await clock.tick(25);
    const fastOut = await fast;
    assert.equal(fastOut.stale, true);
    assert.equal(fastOut.body, 'v1');
    assert.equal(cache.getStats().timeouts, 1);

    // The still-running call settles afterwards without touching the waiter.
    controlled.calls[1].gate.resolve(originResponse({ status: 304, body: null }));
    await flushMicrotasks();
    assert.equal(cache.inflightCount(), 0);
  });

  it('a deadline with no stale allowance rejects with TimeoutError', async () => {
    const controlled = createControlledOrigin();
    const cache = new EdgeCache({ origin: controlled.origin, now: clock.now, setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout });

    const seed = cache.fetch({ url: URL });
    await flushMicrotasks();
    // Stale window of zero: a stale entity can never be served.
    controlled.calls[0].gate.resolve(originResponse({
      body: 'v1', etag: '"e1"', freshMs: 10, staleMs: 0,
    }));
    await seed;
    clock.advance(50);

    const reval = observe(cache.fetch({ url: URL, deadlineMs: 30 }));
    await flushMicrotasks();
    await clock.tick(35);
    await assert.rejects(reval, (error) => error instanceof TimeoutError);

    // The still-running call settles afterwards without touching the waiter.
    controlled.calls[1].gate.resolve(originResponse({ status: 304, body: null }));
    await flushMicrotasks();
    assert.equal(cache.inflightCount(), 0);
  });

  it('a late origin response does not override an already expired waiter', async () => {
    const controlled = createControlledOrigin();
    const cache = new EdgeCache({ origin: controlled.origin, now: clock.now, setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout });

    const seed = cache.fetch({ url: URL });
    await flushMicrotasks();
    controlled.calls[0].gate.resolve(originResponse({
      body: 'v1', etag: '"e1"', freshMs: 10, staleMs: 5000,
    }));
    await seed;
    clock.advance(50);

    const reval = observe(cache.fetch({ url: URL, deadlineMs: 20, maxStaleMs: 5000 }));
    await flushMicrotasks();
    await clock.tick(25);
    const timedOut = await reval;
    assert.equal(timedOut.stale, true);
    assert.equal(timedOut.etag, '"e1"');

    // After the timeout a 200 arrives: it still stores the new entity for
    // future hits, but cannot resolve the expired waiter again.
    controlled.calls[1].gate.resolve(originResponse({
      body: 'v2', etag: '"e2"', freshMs: 100, staleMs: 5000,
    }));
    await flushMicrotasks();
    const entry = cache.getEntry(URL);
    assert.equal(entry.etag, '"e2"');

    const hit = await cache.fetch({ url: URL });
    assert.equal(hit.body, 'v2');
    assert.equal(hit.fromCache, true);
  });

  it('deadlines on full fetches apply while no entity exists yet', async () => {
    const controlled = createControlledOrigin();
    const cache = new EdgeCache({ origin: controlled.origin, now: clock.now, setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout });

    const fetch = observe(cache.fetch({ url: URL, deadlineMs: 40 }));
    await flushMicrotasks();
    await clock.tick(45);
    await assert.rejects(fetch, TimeoutError);

    // The origin call remains in flight and its late response is cached.
    assert.equal(cache.inflightCount(), 1);
    controlled.calls[0].gate.resolve(originResponse({ body: 'late', etag: '"x"', freshMs: 100 }));
    await flushMicrotasks();
    const hit = await cache.fetch({ url: URL });
    assert.equal(hit.body, 'late');
    assert.equal(hit.fromCache, true);
  });
});
