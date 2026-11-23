import assert from 'node:assert/strict';
import { describe, it } from 'node:test';
import { EdgeCache } from '../src/edge-cache.js';
import { createClock, flushMicrotasks } from './helpers/clock.js';
import { createControlledOrigin, originResponse } from './helpers/controlled-origin.js';

const URL = 'https://edge.example.test/item';

describe('edge cases: wildcard Vary, Cache-Control and header casing', () => {
  const clock = createClock();

  it('Vary: * makes every request its own variant', async () => {
    const controlled = createControlledOrigin();
    const cache = new EdgeCache({
      origin: controlled.origin, now: clock.now,
      setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout,
    });

    const first = cache.fetch({ url: URL, headers: { 'x-foo': 'a' } });
    await flushMicrotasks();
    controlled.calls[0].gate.resolve(originResponse({
      body: 'one', etag: '"w1"', vary: '*', freshMs: 1000, staleMs: 100,
    }));
    await first;

    // Identical-looking request headers still select a distinct wildcard variant.
    const second = cache.fetch({ url: URL, headers: { 'x-foo': 'a' } });
    await flushMicrotasks();
    assert.equal(controlled.callCount(), 2, 'wildcard Vary never reuses a variant');
    controlled.calls[1].gate.resolve(originResponse({
      body: 'two', etag: '"w2"', vary: '*', freshMs: 1000, staleMs: 100,
    }));
    const out = await second;
    assert.equal(out.body, 'two');
    assert.deepEqual(cache.getVaryFields(URL), ['*']);
  });

  it('reads freshness windows from Cache-Control when synthetic headers are absent', async () => {
    const controlled = createControlledOrigin();
    const cache = new EdgeCache({
      origin: controlled.origin, now: clock.now,
      setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout,
    });

    const seed = cache.fetch({ url: URL });
    await flushMicrotasks();
    controlled.calls[0].gate.resolve({
      status: 200,
      headers: {
        etag: '"cc1"',
        'cache-control': 'max-age=100, stale-while-revalidate=50',
      },
      body: 'cc-body',
    });
    await seed;

    clock.advance(100); // still fresh at the boundary
    const freshHit = await cache.fetch({ url: URL });
    assert.equal(freshHit.fromCache, true);
    assert.equal(freshHit.stale, false);
    assert.equal(controlled.callCount(), 1);

    clock.advance(40); // age 140: stale, within 100 + 50
    const reval = cache.fetch({ url: URL, maxStaleMs: 50 });
    await flushMicrotasks();
    controlled.calls[1].gate.resolve({ status: 503, headers: {}, body: 'boom' });
    const stale = await reval;
    assert.equal(stale.stale, true);
    assert.equal(stale.body, 'cc-body');

    clock.advance(20); // age 160: beyond the stale window
    const reval2 = cache.fetch({ url: URL, maxStaleMs: 50 });
    await flushMicrotasks();
    controlled.calls[2].gate.resolve({ status: 503, headers: {}, body: 'boom' });
    await assert.rejects(reval2, /UPSTREAM_FAILURE|origin/);
  });

  it('accepts mixed-case header names for Vary, ETag and freshness headers', async () => {
    const controlled = createControlledOrigin();
    const cache = new EdgeCache({
      origin: controlled.origin, now: clock.now,
      setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout,
    });

    const seed = cache.fetch({ url: URL, headers: { 'X-Tenant': 'Acme' } });
    await flushMicrotasks();
    controlled.calls[0].gate.resolve({
      status: 200,
      headers: {
        ETag: '"case-1"',
        VARY: 'X-Tenant',
        'X-Cache-Fresh-Ms': '1000',
        'X-Cache-Stale-Ms': '100',
      },
      body: 'case-body',
    });
    await seed;
    assert.deepEqual(cache.getVaryFields(URL), ['x-tenant']);

    const lower = await cache.fetch({ url: URL, headers: { 'x-tenant': 'Acme' } });
    assert.equal(lower.fromCache, true, 'header name casing must not split variants');
    assert.equal(lower.etag, '"case-1"');
    assert.equal(controlled.callCount(), 1);
  });
});
