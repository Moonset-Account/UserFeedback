import assert from 'node:assert/strict';
import { describe, it } from 'node:test';
import { EdgeCache } from '../src/edge-cache.js';
import { createClock, flushMicrotasks } from './helpers/clock.js';
import { createControlledOrigin, originResponse } from './helpers/controlled-origin.js';

const URL = 'https://edge.example.test/item';

describe('conditional revalidation: 304 and Vary rebuilds', () => {
  const clock = createClock();

  it('refreshes only the entity named at initiation and renews its origin time', async () => {
    const controlled = createControlledOrigin();
    const cache = new EdgeCache({ origin: controlled.origin, now: clock.now, setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout });

    const seed = cache.fetch({ url: URL });
    await flushMicrotasks();
    controlled.respondNext(originResponse({
      body: 'v1', etag: '"e1"', freshMs: 100, staleMs: 100,
    }));
    await seed;
    const originalTime = cache.getEntry(URL).originTime;

    clock.advance(150); // stale
    const reval = cache.fetch({ url: URL });
    await flushMicrotasks();
    controlled.respondNext(originResponse({
      status: 304, body: null, freshMs: 100, staleMs: 100,
    }));
    const out = await reval;

    assert.equal(out.status, 200);
    assert.equal(out.revalidated, true);
    assert.equal(out.body, 'v1', '304 keeps the cached body');
    assert.equal(out.etag, '"e1"');
    const refreshed = cache.getEntry(URL);
    assert.ok(refreshed.originTime > originalTime, '304 renews the origin time');

    // The renewed entity is fresh again and hits without an origin call.
    const hit = await cache.fetch({ url: URL });
    assert.equal(hit.fromCache, true);
    assert.equal(hit.stale, false);
    assert.equal(controlled.callCount(), 2);
  });

  it('a late 304 cannot make the superseded old entity fresh again', async () => {
    const controlled = createControlledOrigin();
    const cache = new EdgeCache({ origin: controlled.origin, now: clock.now, setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout });

    const seed = cache.fetch({ url: URL });
    await flushMicrotasks();
    controlled.respondNext(originResponse({
      body: 'old-body', etag: '"e1"', freshMs: 10, staleMs: 500,
    }));
    await seed;
    clock.advance(50);

    // Revalidation of "e1" starts and stays pending.
    const lateReval = cache.fetch({ url: URL });
    await flushMicrotasks();
    assert.equal(controlled.callCount(), 2);
    const revalCall = controlled.calls[1];

    // Meanwhile a full 200 lands a newer entity.
    const full = cache.fetch({ url: URL, forceRefresh: true });
    await flushMicrotasks();
    assert.equal(controlled.callCount(), 3);
    const fullCall = controlled.calls[2];
    fullCall.gate.resolve(originResponse({
      body: 'new-body', etag: '"e2"', freshMs: 1000, staleMs: 500,
    }));
    await full;
    const newOriginTime = cache.getEntry(URL).originTime;

    // The pending call finally answers 304 for the old ETag.
    revalCall.gate.resolve(originResponse({ status: 304, body: null }));
    const out = await lateReval;

    const entry = cache.getEntry(URL);
    assert.equal(entry.etag, '"e2"', 'old ETag must not be restored');
    assert.equal(entry.body, 'new-body');
    assert.equal(entry.originTime, newOriginTime, 'the newer entity origin time is untouched');
    assert.equal(out.body, 'new-body');
    assert.ok(cache.getStats().superseded >= 1);

    // Following reads keep hitting the newer entity.
    const hit = await cache.fetch({ url: URL });
    assert.equal(hit.etag, '"e2"');
    assert.equal(hit.fromCache, true);
    assert.equal(controlled.callCount(), 3);
  });

  it('rebuilds the Vary index from the initiating headers and removes old aliases', async () => {
    const controlled = createControlledOrigin();
    const cache = new EdgeCache({ origin: controlled.origin, now: clock.now, setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout });
    const headers = { 'x-tenant': 'acme' };

    const seed = cache.fetch({ url: URL, headers });
    await flushMicrotasks();
    controlled.respondNext(originResponse({
      body: 'tenant-body', etag: '"e1"', freshMs: 10, staleMs: 500,
    }));
    await seed;
    assert.deepEqual(cache.getVaryFields(URL), []);
    assert.equal(cache.variantCount(URL), 1);
    clock.advance(50);

    const reval = cache.fetch({ url: URL, headers });
    await flushMicrotasks();
    controlled.respondNext(originResponse({
      status: 304, body: null, vary: 'X-Tenant', freshMs: 100, staleMs: 500,
    }));
    const out = await reval;

    assert.equal(out.revalidated, true);
    assert.equal(out.body, 'tenant-body');
    assert.deepEqual(cache.getVaryFields(URL), ['x-tenant']);
    assert.equal(cache.variantCount(URL), 1, 'the old empty-Vary alias was removed');

    // The rebuilt index is keyed on the initiating request's headers:
    // the same headers hit, different headers are a new variant.
    const same = await cache.fetch({ url: URL, headers: { 'x-tenant': 'acme' } });
    assert.equal(same.fromCache, true);
    assert.equal(same.body, 'tenant-body');

    const other = cache.fetch({ url: URL, headers: { 'x-tenant': 'globex' } });
    await flushMicrotasks();
    assert.equal(controlled.callCount(), 3, 'new variant needs its own origin call');
    controlled.respondNext(originResponse({
      body: 'globex-body', etag: '"g1"', vary: 'X-Tenant', freshMs: 100, staleMs: 500,
    }));
    const otherOut = await other;
    assert.equal(otherOut.body, 'globex-body');
    assert.equal(cache.variantCount(URL), 2);
    assert.equal(cache.getStats().varyRebuilds, 1);
  });

  it('keeps a newer entity already present at the rebuilt target key', async () => {
    const controlled = createControlledOrigin();
    const cache = new EdgeCache({ origin: controlled.origin, now: clock.now, setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout });
    const headers = { 'x-tenant': 'acme' };

    // Old entity stored with empty Vary.
    const seed = cache.fetch({ url: URL, headers });
    await flushMicrotasks();
    controlled.respondNext(originResponse({
      body: 'old-body', etag: '"e1"', freshMs: 10, staleMs: 500,
    }));
    await seed;
    clock.advance(50);

    // Revalidation with a new Vary field stays pending.
    const reval = cache.fetch({ url: URL, headers });
    await flushMicrotasks();
    const revalCall = controlled.calls[1];

    // A concurrent full fetch already returns an entity using the new Vary
    // scheme, landing exactly at the key the rebuild will target.
    const full = cache.fetch({ url: URL, forceRefresh: true, headers });
    await flushMicrotasks();
    const fullCall = controlled.calls[2];
    fullCall.gate.resolve(originResponse({
      body: 'new-body', etag: '"e2"', vary: 'X-Tenant', freshMs: 100, staleMs: 500,
    }));
    await full;

    // Late 304 for "e1" carrying the new Vary field.
    revalCall.gate.resolve(originResponse({
      status: 304, body: null, vary: 'X-Tenant',
    }));
    const out = await reval;

    assert.deepEqual(cache.getVaryFields(URL), ['x-tenant']);
    const entry = cache.getEntry(URL, headers);
    assert.equal(entry.etag, '"e2"', 'the newer target entity stays available');
    assert.equal(entry.body, 'new-body');
    assert.equal(out.body, 'new-body');
    assert.equal(cache.variantCount(URL), 1);
  });

  it('a concurrent 200 replaces the entity and later readers hit the new body', async () => {
    const controlled = createControlledOrigin();
    const cache = new EdgeCache({ origin: controlled.origin, now: clock.now, setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout });

    const seed = cache.fetch({ url: URL });
    await flushMicrotasks();
    controlled.respondNext(originResponse({
      body: 'v1', etag: '"e1"', freshMs: 10, staleMs: 500,
    }));
    await seed;
    clock.advance(50);

    const reval = cache.fetch({ url: URL });
    const forced = cache.fetch({ url: URL, forceRefresh: true });
    await flushMicrotasks();
    assert.equal(controlled.callCount(), 3);
    const revalCall = controlled.calls[1];
    const fullCall = controlled.calls[2];

    // The 200 resolves first.
    fullCall.gate.resolve(originResponse({
      body: 'v2', etag: '"e2"', freshMs: 100, staleMs: 500,
    }));
    const forcedOut = await forced;
    assert.equal(forcedOut.body, 'v2');

    // Then the revalidation call answers 200 too (origin changed mid-flight).
    revalCall.gate.resolve(originResponse({
      body: 'v3', etag: '"e3"', freshMs: 100, staleMs: 500,
    }));
    const revalOut = await reval;
    assert.equal(revalOut.body, 'v3');

    const hit = await cache.fetch({ url: URL });
    assert.equal(hit.body, 'v3');
    assert.equal(hit.etag, '"e3"');
    assert.equal(hit.fromCache, true);
    assert.equal(controlled.callCount(), 3);
  });
});
