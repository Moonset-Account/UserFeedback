import assert from 'node:assert/strict';
import { describe, it } from 'node:test';
import { EdgeCache } from '../src/edge-cache.js';
import { createClock, flushMicrotasks } from './helpers/clock.js';
import { createControlledOrigin, originResponse } from './helpers/controlled-origin.js';

/**
 * Acceptance scenario from the task statement:
 *
 *  1. Two-path reads with and without the same request header (Vary).
 *  2. One concurrent 200 while a conditional revalidation is in flight.
 *  3. A late 304 that validates the old entity version.
 *  4. Verify selected variant, response body and subsequent cache hits.
 */
describe('acceptance: two-path reads, concurrent 200 and late 304', () => {
  const clock = createClock();

  it('keeps variants and bodies correct end to end', async () => {
    const controlled = createControlledOrigin();
    const cache = new EdgeCache({ origin: controlled.origin, now: clock.now, setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout });
    const url = 'https://edge.example.test/edge-page';

    // ---- Path 1: request with the Vary header ---------------------------
    const enHeaders = { 'accept-language': 'en' };
    const pEn = cache.fetch({ url, headers: enHeaders });
    await flushMicrotasks();
    assert.equal(controlled.callCount(), 1);
    controlled.respondNext(originResponse({
      body: 'hello', etag: '"en-v1"', vary: 'Accept-Language',
      freshMs: 100, staleMs: 500,
    }));
    const enFirst = await pEn;
    assert.equal(enFirst.body, 'hello');
    assert.equal(enFirst.etag, '"en-v1"');

    // ---- Path 2: request without the header -----------------------------
    const pDef = cache.fetch({ url, headers: {} });
    await flushMicrotasks();
    assert.equal(controlled.callCount(), 2, 'the two header paths are distinct variants');
    controlled.respondNext(originResponse({
      // Long freshness lifetime: this variant must stay fresh for the
      // whole scenario so it remains an undisturbed cache hit later.
      body: 'default-hello', etag: '"def-v1"', vary: 'Accept-Language',
      freshMs: 100_000, staleMs: 500,
    }));
    const defFirst = await pDef;
    assert.equal(defFirst.body, 'default-hello');
    assert.notEqual(enFirst.variantKey, defFirst.variantKey);
    assert.equal(cache.variantCount(url), 2);

    // ---- Go stale and start a conditional revalidation ------------------
    clock.advance(150);
    const enReval = cache.fetch({ url, headers: enHeaders });
    await flushMicrotasks();
    assert.equal(controlled.callCount(), 3);
    const revalCall = controlled.calls[2];
    assert.equal(
      revalCall.request.headers.find(([h]) => h === 'if-none-match')?.[1],
      '"en-v1"',
      'revalidation carries the initiating entity ETag',
    );

    // ---- A concurrent unconditional 200 returns a newer entity ----------
    const enFull = cache.fetch({ url, headers: enHeaders, forceRefresh: true });
    await flushMicrotasks();
    assert.equal(controlled.callCount(), 4);
    const fullCall = controlled.calls[3];
    assert.equal(
      fullCall.request.headers.find(([h]) => h === 'if-none-match'),
      undefined,
      'full fetch must be unconditional',
    );
    fullCall.gate.resolve(originResponse({
      body: 'hello-v2', etag: '"en-v2"', vary: 'Accept-Language',
      freshMs: 1000, staleMs: 500,
    }));
    const fullOut = await enFull;
    assert.equal(fullOut.body, 'hello-v2');
    assert.equal(fullOut.etag, '"en-v2"');

    // ---- The late 304 validates the OLD entity and must not win ---------
    revalCall.gate.resolve(originResponse({ status: 304, body: null }));
    const revalOut = await enReval;
    assert.equal(revalOut.body, 'hello-v2', 'late 304 cannot resurrect the old body');
    assert.equal(revalOut.etag, '"en-v2"');
    assert.equal(cache.getEntry(url, enHeaders).etag, '"en-v2"');
    assert.ok(cache.getStats().superseded >= 1);

    // ---- Subsequent hits: variants and bodies stay correct --------------
    const enHit = await cache.fetch({ url, headers: enHeaders });
    const defHit = await cache.fetch({ url, headers: {} });
    assert.equal(enHit.fromCache, true);
    assert.equal(enHit.stale, false);
    assert.equal(enHit.body, 'hello-v2');
    assert.equal(defHit.fromCache, true);
    assert.equal(defHit.body, 'default-hello', 'the other variant is untouched');
    assert.equal(controlled.callCount(), 4, 'both reads are cache hits');

    // A different language is yet another variant and reaches the origin.
    const frPending = cache.fetch({ url, headers: { 'accept-language': 'fr' } });
    await flushMicrotasks();
    assert.equal(controlled.callCount(), 5);
    controlled.respondNext(originResponse({
      body: 'bonjour', etag: '"fr-v1"', vary: 'Accept-Language',
      freshMs: 100, staleMs: 500,
    }));
    const frOut = await frPending;
    assert.equal(frOut.body, 'bonjour');
    assert.equal(cache.variantCount(url), 3);
  });

  it('coalesces same-variant same-ETag waits but keeps their deadlines separate', async () => {
    const controlled = createControlledOrigin();
    const cache = new EdgeCache({ origin: controlled.origin, now: clock.now, setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout });
    const url = 'https://edge.example.test/coalesce';
    const headers = { 'x-tenant': 'acme' };

    const seed = cache.fetch({ url, headers });
    await flushMicrotasks();
    controlled.respondNext(originResponse({
      body: 'tenant', etag: '"t1"', vary: 'X-Tenant', freshMs: 10, staleMs: 500,
    }));
    await seed;
    clock.advance(50);

    const waiterA = cache.fetch({ url, headers, deadlineMs: 25, maxStaleMs: 500 });
    const waiterB = cache.fetch({ url, headers, deadlineMs: 80, maxStaleMs: 500 });
    const otherVariant = cache.fetch({ url, headers: { 'x-tenant': 'globex' } });
    await flushMicrotasks();
    assert.equal(controlled.callCount(), 3, 'same variant coalesced, other variant is separate');
    const revalCall = controlled.calls[1];
    const globexCall = controlled.calls[2];
    assert.equal(revalCall.request.headers.find(([h]) => h === 'if-none-match')?.[1], '"t1"');
    assert.equal(globexCall.request.headers.find(([h]) => h === 'if-none-match'), undefined);

    // Waiter A's own deadline expires first and is served stale.
    await clock.tick(30);
    const a = await waiterA;
    assert.equal(a.stale, true);
    assert.equal(a.body, 'tenant');
    assert.equal(cache.inflightCount(), 2);

    // Other variant resolves independently via its own full-fetch gate.
    globexCall.gate.resolve(originResponse({
      body: 'other', etag: '"g1"', vary: 'X-Tenant', freshMs: 100, staleMs: 500,
    }));
    const other = await otherVariant;
    assert.equal(other.body, 'other');

    // Shared revalidation returns 304; waiter B still gets the fresh result.
    revalCall.gate.resolve(originResponse({ status: 304, body: null }));
    await flushMicrotasks();
    const b = await waiterB;
    assert.equal(b.revalidated, true);
    assert.equal(b.stale, false);
    assert.equal(b.body, 'tenant');
    assert.equal(cache.getStats().coalesced, 1);
    assert.equal(controlled.callCount(), 3);
  });
});
