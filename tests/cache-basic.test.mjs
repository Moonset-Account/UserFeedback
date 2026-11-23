import assert from 'node:assert/strict';
import { describe, it } from 'node:test';
import { EdgeCache } from '../src/edge-cache.js';
import { createClock, flushMicrotasks } from './helpers/clock.js';
import { createControlledOrigin, originResponse } from './helpers/controlled-origin.js';

const URL = 'https://edge.example.test/item';

describe('basic cache behaviour and Vary variants', () => {
  const clock = createClock();

  it('populates the cache on miss and serves fresh hits without the origin', async () => {
    const controlled = createControlledOrigin();
    const cache = new EdgeCache({ origin: controlled.origin, now: clock.now, setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout });

    const pending = cache.fetch({ url: URL, headers: { accept: 'json' } });
    await flushMicrotasks();
    assert.equal(controlled.callCount(), 1);
    controlled.respondNext(originResponse({
      body: 'v1', etag: 'W/"v1"', vary: 'Accept', freshMs: 100, staleMs: 50,
    }));
    const first = await pending;

    assert.equal(first.fromCache, false);
    assert.equal(first.body, 'v1');
    assert.equal(first.etag, 'W/"v1"');

    const second = await cache.fetch({ url: URL, headers: { accept: 'json' } });
    assert.equal(second.fromCache, true);
    assert.equal(second.stale, false);
    assert.equal(second.body, 'v1');
    assert.equal(controlled.callCount(), 1, 'fresh hit must not call the origin');
    assert.equal(cache.getStats().hits, 1);
  });

  it('splits reads into two variants with and without the Vary header', async () => {
    const controlled = createControlledOrigin();
    const cache = new EdgeCache({ origin: controlled.origin, now: clock.now, setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout });

    // First path: request carrying Accept-Language: en
    const p1 = cache.fetch({ url: URL, headers: { 'accept-language': 'en' } });
    await flushMicrotasks();
    controlled.respondNext(originResponse({
      body: 'english', etag: '"en-1"', vary: 'Accept-Language', freshMs: 100, staleMs: 50,
    }));
    const english = await p1;

    // Second path: request without the Vary header
    const p2 = cache.fetch({ url: URL, headers: {} });
    await flushMicrotasks();
    assert.equal(controlled.callCount(), 2, 'missing Vary header selects a different variant');
    const call2 = controlled.calls[1];
    assert.equal(call2.request.headers.find(([h]) => h === 'accept-language'), undefined);
    controlled.respondNext(originResponse({
      body: 'default', etag: '"def-1"', vary: 'Accept-Language', freshMs: 100, staleMs: 50,
    }));
    const fallback = await p2;

    assert.notEqual(english.variantKey, fallback.variantKey);
    assert.equal(cache.variantCount(URL), 2);

    // Subsequent reads keep selecting the correct variant and hit.
    const hitEn = await cache.fetch({ url: URL, headers: { 'accept-language': 'en' } });
    const hitDef = await cache.fetch({ url: URL, headers: {} });
    assert.equal(hitEn.body, 'english');
    assert.equal(hitDef.body, 'default');
    assert.equal(hitEn.fromCache, true);
    assert.equal(hitDef.fromCache, true);
    assert.equal(controlled.callCount(), 2);
  });

  it('coalesces concurrent requests for the same variant and entity version', async () => {
    const controlled = createControlledOrigin();
    const cache = new EdgeCache({ origin: controlled.origin, now: clock.now, setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout });

    const seed = cache.fetch({ url: URL, headers: { 'x-tenant': 'a' } });
    await flushMicrotasks();
    controlled.respondNext(originResponse({
      body: 'seed', etag: '"e1"', vary: 'X-Tenant', freshMs: 10, staleMs: 100,
    }));
    await seed;

    clock.advance(50); // stale but inside the stale window

    const r1 = cache.fetch({ url: URL, headers: { 'x-tenant': 'a' } });
    const r2 = cache.fetch({ url: URL, headers: { 'x-tenant': 'a' } });
    await flushMicrotasks();
    assert.equal(controlled.callCount(), 2, 'the two revalidations share one origin call');
    const revalCall = controlled.calls[1];
    assert.equal(revalCall.request.headers.find(([h]) => h === 'if-none-match')?.[1], '"e1"');

    controlled.respondNext(originResponse({ status: 304, body: null }));
    const [out1, out2] = await Promise.all([r1, r2]);
    assert.equal(out1.revalidated, true);
    assert.equal(out2.revalidated, true);
    assert.equal(out1.body, 'seed');
    assert.equal(cache.getStats().coalesced, 1);
    assert.equal(controlled.callCount(), 2);
  });

  it('does NOT coalesce requests validating different entity versions', async () => {
    const controlled = createControlledOrigin();
    const cache = new EdgeCache({ origin: controlled.origin, now: clock.now, setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout });

    const seed = cache.fetch({ url: URL });
    await flushMicrotasks();
    controlled.respondNext(originResponse({
      body: 'v1', etag: '"e1"', freshMs: 10, staleMs: 200,
    }));
    await seed;
    clock.advance(50);

    // First revalidation starts against "e1" (call #2).
    const r1 = cache.fetch({ url: URL });
    await flushMicrotasks();
    assert.equal(controlled.callCount(), 2);
    const revalE1 = controlled.calls[1];
    assert.equal(revalE1.request.headers.find(([h]) => h === 'if-none-match')?.[1], '"e1"');

    // A forced full fetch (call #3) lands a new entity "e2" while #2 is pending.
    const forced = cache.fetch({ url: URL, forceRefresh: true });
    await flushMicrotasks();
    assert.equal(controlled.callCount(), 3);
    const fullCall = controlled.calls[2];
    assert.equal(fullCall.request.headers.find(([h]) => h === 'if-none-match'), undefined);
    fullCall.gate.resolve(originResponse({
      body: 'v2', etag: '"e2"', freshMs: 10, staleMs: 200,
    }));
    await forced;

    // A stale reader now validates the current "e2" (call #4); it is not
    // coalesced with the still-pending "e1" revalidation.
    clock.advance(50);
    const r3 = cache.fetch({ url: URL });
    await flushMicrotasks();
    assert.equal(controlled.callCount(), 4);
    const revalE2 = controlled.calls[3];
    assert.equal(revalE2.request.headers.find(([h]) => h === 'if-none-match')?.[1], '"e2"');

    // The original call answers a late 304 for the superseded "e1".
    revalE1.gate.resolve(originResponse({ status: 304, body: null }));
    const late = await r1;
    revalE2.gate.resolve(originResponse({ status: 304, body: null }));
    const current = await r3;

    // The late 304 must not resurrect the old entity.
    const entry = cache.getEntry(URL);
    assert.equal(entry.etag, '"e2"');
    assert.equal(late.body, 'v2');
    assert.equal(current.body, 'v2');
    assert.ok(cache.getStats().superseded >= 1);
    assert.equal(cache.inflightCount(), 0);
  });
});
