import assert from 'node:assert/strict';
import { describe, it } from 'node:test';
import { EdgeCache } from '../src/edge-cache.js';
import { UpstreamError } from '../src/errors.js';
import { createClock, flushMicrotasks } from './helpers/clock.js';
import { createControlledOrigin, originResponse } from './helpers/controlled-origin.js';

const URL = 'https://edge.example.test/item';

describe('upstream failure and stale serving', () => {
  const clock = createClock();

  it('serves the old entity on a 5xx while each waiter stale window allows it', async () => {
    const controlled = createControlledOrigin();
    const cache = new EdgeCache({ origin: controlled.origin, now: clock.now, setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout });

    const seed = cache.fetch({ url: URL });
    await flushMicrotasks();
    controlled.respondNext(originResponse({
      body: 'v1', etag: '"e1"', freshMs: 100, staleMs: 200,
    }));
    await seed;
    clock.advance(150); // age 150: stale, within fresh 100 + stale 200 = 300

    const reval = cache.fetch({ url: URL, maxStaleMs: 500 });
    await flushMicrotasks();
    controlled.respondNext(originResponse({ status: 503, body: 'boom' }));
    const out = await reval;

    assert.equal(out.stale, true);
    assert.equal(out.fromCache, true);
    assert.equal(out.body, 'v1');
    assert.equal(cache.getStats().staleServed, 1);
    assert.equal(cache.getStats().upstreamFailures, 1);

    // Serving stale never extends validity: originTime is unchanged.
    const entry = cache.getEntry(URL);
    assert.equal(entry.originTime, 1_000_000);
    clock.advance(300); // age 450 > 300: stale window exhausted
    const again = cache.fetch({ url: URL, maxStaleMs: 500 });
    await flushMicrotasks();
    controlled.respondNext(originResponse({ status: 503, body: 'boom' }));
    await assert.rejects(again, (error) => error instanceof UpstreamError);
  });

  it('per-waiter allowance decides independently for one shared failure', async () => {
    const controlled = createControlledOrigin();
    const cache = new EdgeCache({ origin: controlled.origin, now: clock.now, setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout });

    const seed = cache.fetch({ url: URL });
    await flushMicrotasks();
    controlled.respondNext(originResponse({
      body: 'v1', etag: '"e1"', freshMs: 100, staleMs: 300,
    }));
    await seed;
    clock.advance(250); // age 250; window 100 + min(300, allowance)

    // Waiter A asks for only 50ms of extra staleness -> max age 250, not enough.
    // Waiter B asks for unlimited staleness -> max age 400, still servable.
    const a = cache.fetch({ url: URL, maxStaleMs: 50 });
    const b = cache.fetch({ url: URL, maxStaleMs: true });
    await flushMicrotasks();
    assert.equal(controlled.callCount(), 2, 'coalesced onto one upstream call');

    controlled.respondNext(originResponse({ status: 500, body: 'boom' }));
    const [resultA, resultB] = await Promise.allSettled([a, b]);
    assert.equal(resultA.status, 'rejected');
    assert.ok(resultA.reason instanceof UpstreamError);
    assert.equal(resultB.status, 'fulfilled');
    assert.equal(resultB.value.stale, true);
    assert.equal(resultB.value.body, 'v1');
  });

  it('the waiter allowance never extends beyond the entity own stale window', async () => {
    const controlled = createControlledOrigin();
    const cache = new EdgeCache({ origin: controlled.origin, now: clock.now, setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout });

    const seed = cache.fetch({ url: URL });
    await flushMicrotasks();
    controlled.respondNext(originResponse({
      body: 'v1', etag: '"e1"', freshMs: 100, staleMs: 50,
    }));
    await seed;
    clock.advance(200); // age 200; entity window ends at 150

    const reval = cache.fetch({ url: URL, maxStaleMs: true });
    await flushMicrotasks();
    controlled.respondNext(originResponse({ status: 502, body: 'boom' }));
    await assert.rejects(reval, UpstreamError);
  });

  it('surfaces network errors and falls back to stale when allowed', async () => {
    const controlled = createControlledOrigin();
    const cache = new EdgeCache({ origin: controlled.origin, now: clock.now, setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout });

    const seed = cache.fetch({ url: URL });
    await flushMicrotasks();
    controlled.respondNext(originResponse({
      body: 'v1', etag: '"e1"', freshMs: 100, staleMs: 200,
    }));
    await seed;
    clock.advance(120);

    const withStale = cache.fetch({ url: URL, maxStaleMs: 500 });
    await flushMicrotasks();
    controlled.failNext(new Error('connection reset'));
    const out = await withStale;
    assert.equal(out.stale, true);
    assert.equal(out.body, 'v1');

    clock.advance(300); // past the entity window
    const withoutStale = cache.fetch({ url: URL });
    await flushMicrotasks();
    controlled.failNext(new Error('connection reset'));
    await assert.rejects(withoutStale, UpstreamError);
  });

  it('evaluates staleness on exact boundaries (age compared to the windows)', async () => {
    const controlled = createControlledOrigin();
    const cache = new EdgeCache({ origin: controlled.origin, now: clock.now, setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout });

    const seed = cache.fetch({ url: URL });
    await flushMicrotasks();
    controlled.respondNext(originResponse({
      body: 'v1', etag: '"e1"', freshMs: 100, staleMs: 100,
    }));
    await seed;

    // Exactly at the freshness boundary: still fresh (strict <).
    clock.advance(100);
    const atBoundary = cache.fetch({ url: URL });
    assert.equal((await atBoundary).stale, false);
    assert.equal(controlled.callCount(), 1);

    clock.advance(99); // age 199: stale, within window ending at 200
    const reval = cache.fetch({ url: URL, maxStaleMs: 100 });
    await flushMicrotasks();
    controlled.calls[1].gate.resolve(originResponse({ status: 503 }));
    const staleOut = await reval;
    assert.equal(staleOut.stale, true);

    clock.advance(1); // age 200: still exactly at the stale window edge -> servable
    const reval2 = cache.fetch({ url: URL, maxStaleMs: 100 });
    await flushMicrotasks();
    controlled.calls[2].gate.resolve(originResponse({ status: 503 }));
    const edgeOut = await reval2;
    assert.equal(edgeOut.stale, true, 'age == window edge is still servable');

    clock.advance(1); // age 201: window exhausted
    const reval3 = cache.fetch({ url: URL, maxStaleMs: 100 });
    await flushMicrotasks();
    controlled.calls[3].gate.resolve(originResponse({ status: 503 }));
    await assert.rejects(reval3, UpstreamError);
  });
});
