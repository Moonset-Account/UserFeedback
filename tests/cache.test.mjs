import { test } from "node:test";
import assert from "node:assert/strict";

import { CompilationCache, GENERIC_REASONS } from "../src/cache.mjs";
import { DeferredCompiler, makeResult } from "../src/compiler.mjs";

const tick = () => new Promise((resolve) => setImmediate(resolve));

const request = (shape, overrides = {}) => ({
  modelVersion: overrides.modelVersion ?? "v1",
  shape,
  layout: overrides.layout ?? "row-major",
  dtype: overrides.dtype ?? "float32",
});

const broadGuard = (ranges, extra = {}) => ({
  dims: ranges.map(([min, max]) => ({ min, max })),
  equal: extra.equal ?? [],
  divisible: extra.divisible ?? [],
  layout: extra.layout ?? "row-major",
  dtype: extra.dtype ?? "float32",
});

const plan = (name) => ({ name });

const makeCache = (maxBytes = 1000, compiler = new DeferredCompiler(), options = {}) =>
  new CompilationCache({ compile: (req) => compiler.compile(req), maxBytes, ...options });

// ---------------------------------------------------------------------------
// Normal reuse paths
// ---------------------------------------------------------------------------

test("compiled plan is cached and reused without a second compilation", async () => {
  const compiler = new DeferredCompiler();
  const cache = makeCache(1000, compiler);

  const first = cache.acquire(request([4, 4]));
  await tick();
  compiler.resolveNext(makeResult(compiler.calls[0], { plan: plan("p1"), occupiedBytes: 10 }));
  const h1 = await first;
  assert.equal(h1.status, "cached");
  assert.deepEqual(h1.plan, plan("p1"));

  const h2 = await cache.acquire(request([4, 4]));
  assert.equal(h2.status, "cached");
  assert.equal(h2.entryId, h1.entryId);
  assert.equal(compiler.callCount, 1);

  const snap = cache.snapshot();
  assert.equal(snap.stats.compileCalls, 1);
  assert.equal(snap.stats.cacheHits, 1);
  assert.equal(snap.occupiedBytes, 10);
  h1.release();
  h2.release();
});

test("guard ranges allow reuse of different shapes inside the same region", async () => {
  const compiler = new DeferredCompiler();
  const cache = makeCache(1000, compiler);

  const first = cache.acquire(request([2, 2]));
  await tick();
  compiler.resolveNext(
    makeResult(compiler.calls[0], {
      plan: plan("region"),
      occupiedBytes: 10,
      guard: broadGuard([
        [1, 8],
        [1, 8],
      ]),
    }),
  );
  await first;

  const inside = await cache.acquire(request([7, 3]));
  assert.equal(inside.status, "cached");
  assert.deepEqual(inside.plan, plan("region"));
  assert.equal(compiler.callCount, 1);

  const outside = cache.acquire(request([9, 1]));
  await tick();
  assert.equal(compiler.callCount, 2);
  compiler.resolveNext(makeResult(compiler.calls[1], { plan: plan("p2"), occupiedBytes: 5 }));
  const hOutside = await outside;
  assert.equal(hOutside.status, "cached");
  assert.deepEqual(hOutside.plan, plan("p2"));
  inside.release();
  hOutside.release();
});

test("equality guard cannot be bypassed by per-dimension maxima", async () => {
  const compiler = new DeferredCompiler();
  const cache = makeCache(1000, compiler);

  const first = cache.acquire(request([4, 4]));
  await tick();
  compiler.resolveNext(
    makeResult(compiler.calls[0], {
      plan: plan("square"),
      occupiedBytes: 10,
      guard: broadGuard(
        [
          [1, 16],
          [1, 16],
        ],
        { equal: [[0, 1]] },
      ),
    }),
  );
  await first;

  // Each dim is within [1, 16] on its own, but 4 !== 8: reuse must be refused.
  const mismatched = cache.acquire(request([4, 8]));
  await tick();
  assert.equal(compiler.callCount, 2);
  compiler.resolveNext(makeResult(compiler.calls[1], { plan: plan("rect"), occupiedBytes: 7 }));
  const h = await mismatched;
  assert.deepEqual(h.plan, plan("rect"));

  // A genuinely square request still reuses the first plan.
  const square = await cache.acquire(request([8, 8]));
  assert.deepEqual(square.plan, plan("square"));
  assert.equal(compiler.callCount, 2);
  h.release();
  square.release();
});

test("divisibility guard rejects non-multiple shapes inside the ranges", async () => {
  const compiler = new DeferredCompiler();
  const cache = makeCache(1000, compiler);

  const first = cache.acquire(request([8, 4]));
  await tick();
  compiler.resolveNext(
    makeResult(compiler.calls[0], {
      plan: plan("div"),
      occupiedBytes: 10,
      guard: broadGuard(
        [
          [1, 32],
          [1, 32],
        ],
        { divisible: [{ numerator: 0, denominator: 1 }] },
      ),
    }),
  );
  await first;

  const ok = await cache.acquire(request([12, 6]));
  assert.deepEqual(ok.plan, plan("div"));

  const bad = cache.acquire(request([7, 4]));
  await tick();
  assert.equal(compiler.callCount, 2);
  compiler.resolveNext(makeResult(compiler.calls[1], { plan: plan("other"), occupiedBytes: 4 }));
  const hBad = await bad;
  assert.deepEqual(hBad.plan, plan("other"));
  ok.release();
  hBad.release();
});

test("layout and dtype are part of the guard", async () => {
  const compiler = new DeferredCompiler();
  const cache = makeCache(1000, compiler);

  const first = cache.acquire(request([4], { layout: "tiled", dtype: "float16" }));
  await tick();
  compiler.resolveNext(makeResult(compiler.calls[0], { plan: plan("tiled16"), occupiedBytes: 10 }));
  await first;

  const same = await cache.acquire(request([4], { layout: "tiled", dtype: "float16" }));
  assert.deepEqual(same.plan, plan("tiled16"));
  assert.equal(compiler.callCount, 1);

  for (const variant of [
    request([4], { layout: "row-major", dtype: "float16" }),
    request([4], { layout: "tiled", dtype: "float32" }),
  ]) {
    const attempt = cache.acquire(variant);
    await tick();
    compiler.resolveNext(makeResult(compiler.calls.at(-1), { plan: plan(`p-${compiler.callCount}`) }));
    const h = await attempt;
    assert.notDeepEqual(h.plan, plan("tiled16"));
    h.release();
  }
  assert.equal(compiler.callCount, 3);
  same.release();
});

// ---------------------------------------------------------------------------
// Coalescing
// ---------------------------------------------------------------------------

test("identical concurrent requests are merged into one compilation and get separate handles", async () => {
  const compiler = new DeferredCompiler();
  const cache = makeCache(1000, compiler);

  const p1 = cache.acquire(request([4, 4]));
  const p2 = cache.acquire(request([4, 4]));
  const p3 = cache.acquire(request([4, 4]));
  await tick();

  assert.equal(compiler.callCount, 1);
  assert.equal(cache.snapshot().inflight, 1);
  assert.equal(cache.snapshot().stats.coalesced, 2);

  compiler.resolveNext(makeResult(compiler.calls[0], { plan: plan("shared"), occupiedBytes: 30 }));
  const [h1, h2, h3] = await Promise.all([p1, p2, p3]);

  for (const h of [h1, h2, h3]) {
    assert.equal(h.status, "cached");
    assert.deepEqual(h.plan, plan("shared"));
    assert.equal(h.entryId, h1.entryId);
  }
  // Each waiter holds its own pin on the shared entry.
  assert.equal(cache.snapshot().entries[0].pins, 3);

  h1.release();
  h2.release();
  assert.equal(cache.snapshot().entries[0].pins, 1);
  h3.release();
  assert.equal(cache.snapshot().entries[0].pins, 0);
});

test("non-identical concurrent requests compile separately and never share a plan blindly", async () => {
  const compiler = new DeferredCompiler();
  const cache = makeCache(1000, compiler);

  const p1 = cache.acquire(request([4, 4]));
  const p2 = cache.acquire(request([8, 8]));
  await tick();
  assert.equal(compiler.callCount, 2);
  assert.equal(cache.snapshot().inflight, 2);

  compiler.resolveNext(makeResult(compiler.calls[0], { plan: plan("a"), occupiedBytes: 10 }));
  compiler.resolveNext(makeResult(compiler.calls[1], { plan: plan("b"), occupiedBytes: 10 }));
  const [h1, h2] = await Promise.all([p1, p2]);
  assert.deepEqual(h1.plan, plan("a"));
  assert.deepEqual(h2.plan, plan("b"));
  assert.notEqual(h1.entryId, h2.entryId);
  h1.release();
  h2.release();
});

test("a coalesced compilation that rejects leaves no state and retries compile fresh", async () => {
  const compiler = new DeferredCompiler();
  const cache = makeCache(1000, compiler);

  const p1 = cache.acquire(request([4, 4]));
  const p2 = cache.acquire(request([4, 4]));
  await tick();
  assert.equal(compiler.callCount, 1);

  compiler.rejectNext(new Error("compiler unavailable"));
  await assert.rejects(p1, /compiler unavailable/);
  await assert.rejects(p2, /compiler unavailable/);
  assert.equal(cache.snapshot().inflight, 0);
  assert.equal(cache.snapshot().stats.compileErrors, 1);
  assert.equal(cache.snapshot().entryCount, 0);

  // Retry after recovery: a fresh compilation starts.
  const retry = cache.acquire(request([4, 4]));
  await tick();
  assert.equal(compiler.callCount, 2);
  compiler.resolveNext(makeResult(compiler.calls[1], { plan: plan("recovered"), occupiedBytes: 12 }));
  const h = await retry;
  assert.deepEqual(h.plan, plan("recovered"));
  assert.equal(cache.snapshot().stats.compileCalls, 2);
  h.release();
});

// ---------------------------------------------------------------------------
// Eviction, pinning and capacity
// ---------------------------------------------------------------------------

test("LRU unpinned entries are evicted when the budget is approached", async () => {
  const compiler = new DeferredCompiler();
  const cache = makeCache(100, compiler);

  async function compileAt(shape, name, bytes) {
    const pending = cache.acquire(request(shape));
    await tick();
    compiler.resolveNext(makeResult(compiler.calls.at(-1), { plan: plan(name), occupiedBytes: bytes }));
    const h = await pending;
    h.release();
    return h;
  }

  const a = await compileAt([1, 1], "a", 40);
  const b = await compileAt([2, 2], "b", 30);
  // Touch a again so b becomes the LRU entry.
  const touchA = await cache.acquire(request([1, 1]));
  assert.equal(touchA.entryId, a.entryId);
  touchA.release();

  const before = cache.snapshot();
  assert.equal(before.occupiedBytes, 70);
  assert.equal(before.stats.cacheHits, 1);

  // 70 + 50 > 100: evicting b (30) makes room; a must survive because it is MRU.
  await compileAt([3, 3], "c", 50);
  const after = cache.snapshot();
  assert.equal(after.entryCount, 2);
  assert.equal(after.occupiedBytes, 90);
  assert.equal(after.stats.evictions, 1);
  const ids = after.entries.map((e) => e.id);
  assert.ok(ids.includes(a.entryId));
  assert.ok(!ids.includes(b.entryId));
});

test("pinned entries cannot be evicted; callers keep their original plan", async () => {
  const compiler = new DeferredCompiler();
  const cache = makeCache(100, compiler);

  const p1 = cache.acquire(request([4, 4]));
  await tick();
  compiler.resolveNext(makeResult(compiler.calls[0], { plan: plan("pinned"), occupiedBytes: 60 }));
  const held = await p1; // deliberately not released -> pin stays

  const p2 = cache.acquire(request([5, 5]));
  await tick();
  compiler.resolveNext(makeResult(compiler.calls[1], { plan: plan("new"), occupiedBytes: 60 }));
  const h2 = await p2;

  // 60 used + 60 needed > 100 and the only resident entry is pinned: generic path.
  assert.equal(h2.status, "generic");
  assert.equal(h2.reason, GENERIC_REASONS.CAPACITY);
  assert.deepEqual(h2.plan, plan("new")); // one-shot plan still available
  assert.equal(cache.snapshot().occupiedBytes, 60);
  assert.equal(cache.snapshot().stats.evictions, 0);

  // The held plan is untouched.
  const still = await cache.acquire(request([4, 4]));
  assert.deepEqual(still.plan, plan("pinned"));
  assert.equal(still.entryId, held.entryId);
  still.release();
  held.release();
  h2.release(); // no-op for generic handles
});

test("a result larger than the whole budget takes the generic path without poisoning the cache", async () => {
  const compiler = new DeferredCompiler();
  const cache = makeCache(50, compiler);

  const small = cache.acquire(request([1, 1]));
  await tick();
  compiler.resolveNext(makeResult(compiler.calls[0], { plan: plan("small"), occupiedBytes: 20 }));
  const hSmall = await small;
  hSmall.release();

  const big = cache.acquire(request([9, 9]));
  await tick();
  compiler.resolveNext(makeResult(compiler.calls[1], { plan: plan("huge"), occupiedBytes: 80 }));
  const hBig = await big;

  assert.equal(hBig.status, "generic");
  assert.equal(hBig.reason, GENERIC_REASONS.CAPACITY);
  const snap = cache.snapshot();
  assert.equal(snap.entryCount, 1);
  assert.equal(snap.occupiedBytes, 20);
  assert.equal(snap.stats.evictions, 0); // failed insertion must not evict first
});

test("an entry fitting exactly after eviction is still cached", async () => {
  const compiler = new DeferredCompiler();
  const cache = makeCache(100, compiler);

  async function compileAt(shape, name, bytes) {
    const pending = cache.acquire(request(shape));
    await tick();
    compiler.resolveNext(makeResult(compiler.calls.at(-1), { plan: plan(name), occupiedBytes: bytes }));
    const h = await pending;
    h.release();
  }

  await compileAt([1], "a", 40);
  await compileAt([2], "b", 30);
  // Free space is exactly 30: a 40-byte entry forces one LRU eviction (a).
  await compileAt([3], "c", 40);
  const snap = cache.snapshot();
  assert.equal(snap.entryCount, 2);
  assert.equal(snap.occupiedBytes, 70);
  assert.equal(snap.stats.evictions, 1);
});

// ---------------------------------------------------------------------------
// Model version switching
// ---------------------------------------------------------------------------

test("version switch drops unpinned old entries and never admits old compilations", async () => {
  const compiler = new DeferredCompiler();
  const cache = makeCache(1000, compiler);

  const first = cache.acquire(request([4, 4]));
  await tick();
  compiler.resolveNext(makeResult(compiler.calls[0], { plan: plan("v1-plan"), occupiedBytes: 20 }));
  const h = await first;
  h.release();
  assert.equal(cache.snapshot().entryCount, 1);

  cache.setModelVersion("v2");
  assert.equal(cache.snapshot().entryCount, 0);
  assert.equal(cache.modelVersion, "v2");

  // A request still carrying the old version is refused up front.
  await assert.rejects(cache.acquire(request([4, 4], { modelVersion: "v1" })), /does not match active version/);

  const v2 = cache.acquire(request([4, 4], { modelVersion: "v2" }));
  await tick();
  assert.equal(compiler.callCount, 2);
  compiler.resolveNext(makeResult(compiler.calls[1], { plan: plan("v2-plan"), occupiedBytes: 25 }));
  const h2 = await v2;
  assert.deepEqual(h2.plan, plan("v2-plan"));
  assert.equal(cache.snapshot().entries[0].modelVersion, "v2");
  h2.release();
});

test("pinned old-version entries survive the switch and disappear on release", async () => {
  const compiler = new DeferredCompiler();
  const cache = makeCache(1000, compiler);

  const old = cache.acquire(request([4, 4]));
  await tick();
  compiler.resolveNext(makeResult(compiler.calls[0], { plan: plan("old"), occupiedBytes: 30 }));
  const held = await old;

  cache.setModelVersion("v2");
  let snap = cache.snapshot();
  assert.equal(snap.entryCount, 1);
  assert.equal(snap.entries[0].pins, 1);
  assert.equal(snap.occupiedBytes, 30);

  // The retained old entry cannot serve new-version requests.
  const fresh = cache.acquire(request([4, 4], { modelVersion: "v2" }));
  await tick();
  assert.equal(compiler.callCount, 2);
  compiler.resolveNext(makeResult(compiler.calls[1], { plan: plan("new"), occupiedBytes: 30 }));
  const hFresh = await fresh;
  assert.deepEqual(hFresh.plan, plan("new"));
  assert.equal(cache.snapshot().entryCount, 2);

  held.release();
  snap = cache.snapshot();
  assert.equal(snap.entryCount, 1);
  assert.deepEqual(snap.entries[0].modelVersion, "v2");
  hFresh.release();
});

// ---------------------------------------------------------------------------
// Required acceptance scenario: two concurrent compilations, one switches
// version before returning. Checks selected plans, compile count and bytes.
// ---------------------------------------------------------------------------

test("interleaving: concurrent compilation finishing after a version switch is generic and never cached", async () => {
  const compiler = new DeferredCompiler();
  const generics = [];
  const cache = makeCache(1000, compiler, {
    onGeneric: (info) => generics.push(info),
  });

  // Two concurrent v1 compilations for two *different* requests.
  const p1 = cache.acquire(request([4, 4]));
  const p2 = cache.acquire(request([8, 8]));
  await tick();
  assert.equal(compiler.callCount, 2);
  assert.equal(cache.snapshot().inflight, 2);

  // First compilation completes normally on v1 and is cached.
  compiler.resolveNext(makeResult(compiler.calls[0], { plan: plan("v1-a"), occupiedBytes: 40 }));
  const h1 = await p1;
  assert.equal(h1.status, "cached");
  assert.deepEqual(h1.plan, plan("v1-a"));
  assert.equal(cache.snapshot().occupiedBytes, 40);

  // Switch the model while the second compilation is still running.
  cache.setModelVersion("v2");
  // The cached v1 entry is pinned by h1, so it is retained but invisible to v2.
  assert.equal(cache.snapshot().entryCount, 1);

  // A new v2 request compiles separately and does not join the stale v1 job.
  const p3 = cache.acquire(request([8, 8], { modelVersion: "v2" }));
  await tick();
  assert.equal(compiler.callCount, 3);
  assert.equal(cache.snapshot().inflight, 2);

  // Now the old v1 compilation finishes: its plan must not enter the v2 cache.
  compiler.resolveNext(makeResult(compiler.calls[1], { plan: plan("v1-b"), occupiedBytes: 60 }));
  const h2 = await p2;
  assert.equal(h2.status, "generic");
  assert.equal(h2.cached, false);
  assert.equal(h2.reason, GENERIC_REASONS.STALE_VERSION);
  assert.deepEqual(h2.plan, plan("v1-b")); // caller still holds the original plan ...
  h2.release(); // ... until it releases; nothing changes in the cache

  const mid = cache.snapshot();
  assert.equal(mid.occupiedBytes, 40); // 60-byte stale plan was never admitted
  assert.equal(mid.stats.generic[GENERIC_REASONS.STALE_VERSION], 1);
  assert.deepEqual(
    mid.entries.map((e) => e.modelVersion),
    ["v1"],
  );

  // The v2 compilation finishes and is cached under the new version.
  compiler.resolveNext(makeResult(compiler.calls[2], { plan: plan("v2-b"), occupiedBytes: 25 }));
  const h3 = await p3;
  assert.equal(h3.status, "cached");
  assert.deepEqual(h3.plan, plan("v2-b"));

  const end = cache.snapshot();
  assert.equal(end.stats.compileCalls, 3); // exactly three compiler calls, no stale caching
  assert.equal(end.occupiedBytes, 65); // pinned v1 40 + new v2 25
  assert.deepEqual(
    end.entries.map((e) => `${e.modelVersion}:${e.occupiedBytes}`).sort(),
    ["v1:40", "v2:25"],
  );

  // Releasing the held v1 plan finally removes the old entry.
  h1.release();
  h3.release();
  const finalSnap = cache.snapshot();
  assert.deepEqual(
    finalSnap.entries.map((e) => e.modelVersion),
    ["v2"],
  );
  assert.equal(finalSnap.occupiedBytes, 25);
  assert.equal(generics.filter((g) => g.reason === GENERIC_REASONS.STALE_VERSION).length, 1);
});

test("interleaving: a coalesced group switched mid-flight returns generic one-shot plans to every waiter", async () => {
  const compiler = new DeferredCompiler();
  const cache = makeCache(1000, compiler);

  const p1 = cache.acquire(request([4, 4]));
  const p2 = cache.acquire(request([4, 4]));
  await tick();
  assert.equal(compiler.callCount, 1);
  assert.equal(cache.snapshot().stats.coalesced, 1);

  cache.setModelVersion("v2");
  compiler.resolveNext(makeResult(compiler.calls[0], { plan: plan("stale"), occupiedBytes: 30 }));
  const [h1, h2] = await Promise.all([p1, p2]);

  for (const h of [h1, h2]) {
    assert.equal(h.status, "generic");
    assert.equal(h.reason, GENERIC_REASONS.STALE_VERSION);
    assert.deepEqual(h.plan, plan("stale"));
  }
  assert.equal(cache.snapshot().entryCount, 0);
  assert.equal(cache.snapshot().occupiedBytes, 0);
  h1.release();
  h2.release();
});

// ---------------------------------------------------------------------------
// Guard coverage of produced plans and overlapping cached guards
// ---------------------------------------------------------------------------

test("a newly compiled plan serves only requests its guard covers", async () => {
  const compiler = new DeferredCompiler();
  const cache = makeCache(1000, compiler);

  // The compiler returns a guard narrower than the request: only dim 1 is allowed.
  const pending = cache.acquire(request([4, 4]));
  await tick();
  compiler.resolveNext(
    makeResult(compiler.calls[0], {
      plan: plan("narrow"),
      occupiedBytes: 10,
      guard: broadGuard([
        [1, 1],
        [1, 8],
      ]),
    }),
  );
  const h = await pending;
  assert.equal(h.status, "generic");
  assert.equal(h.reason, GENERIC_REASONS.GUARD_MISMATCH);
  assert.deepEqual(h.plan, plan("narrow"));
  assert.equal(cache.snapshot().entryCount, 0); // uncovered result is not cached
});

test("a coalesced group whose produced guard covers nobody fans generic handles out to every waiter", async () => {
  const compiler = new DeferredCompiler();
  const cache = makeCache(1000, compiler);

  const p1 = cache.acquire(request([4, 4]));
  const p2 = cache.acquire(request([4, 4]));
  const p3 = cache.acquire(request([4, 4]));
  await tick();
  assert.equal(compiler.callCount, 1);
  assert.equal(cache.snapshot().stats.coalesced, 2);

  compiler.resolveNext(
    makeResult(compiler.calls[0], {
      plan: plan("too-narrow"),
      occupiedBytes: 10,
      guard: broadGuard([
        [9, 10],
        [9, 10],
      ]),
    }),
  );
  const handles = await Promise.all([p1, p2, p3]);
  for (const h of handles) {
    assert.equal(h.status, "generic");
    assert.equal(h.reason, GENERIC_REASONS.GUARD_MISMATCH);
    assert.deepEqual(h.plan, plan("too-narrow"));
  }
  const snap = cache.snapshot();
  assert.equal(snap.entryCount, 0);
  assert.equal(snap.occupiedBytes, 0);
  assert.equal(snap.stats.generic[GENERIC_REASONS.GUARD_MISMATCH], 3);
  for (const h of handles) h.release();
});

test("overlapping guards resolve to the most recently used matching entry", async () => {
  const compiler = new DeferredCompiler();
  const cache = makeCache(1000, compiler);

  async function compile(shape, name, guard) {
    const pending = cache.acquire(request(shape));
    await tick();
    compiler.resolveNext(makeResult(compiler.calls.at(-1), { plan: plan(name), occupiedBytes: 10, guard }));
    const h = await pending;
    h.release();
    return h;
  }

  // wide and narrow each have an exclusive shape and overlap on [4, 20].
  const wide = await compile(
    [2, 2],
    "wide",
    broadGuard([
      [1, 16],
      [1, 24],
    ]),
  );
  const narrow = await compile(
    [20, 24],
    "narrow",
    broadGuard([
      [4, 30],
      [20, 30],
    ]),
  );

  // [4, 20] matches both; narrow was inserted later (MRU) so it wins.
  const h1 = await cache.acquire(request([4, 20]));
  assert.equal(h1.entryId, narrow.entryId);
  assert.deepEqual(h1.plan, plan("narrow"));
  h1.release();

  // Touching wide through its exclusive shape flips the MRU ordering.
  const h2 = await cache.acquire(request([2, 2]));
  assert.equal(h2.entryId, wide.entryId);
  h2.release();
  const h3 = await cache.acquire(request([4, 20]));
  assert.equal(h3.entryId, wide.entryId);
  assert.deepEqual(h3.plan, plan("wide"));
  h3.release();
});

test("malformed compiler results reject waiters and keep the cache untouched", async () => {
  const compiler = new DeferredCompiler();
  const cache = makeCache(1000, compiler);

  const p1 = cache.acquire(request([4, 4]));
  const p2 = cache.acquire(request([4, 4]));
  await tick();
  compiler.resolveNext({ plan: plan("bad"), occupiedBytes: -5, guard: {} });
  await assert.rejects(p1, RangeError);
  await assert.rejects(p2, RangeError);
  assert.equal(cache.snapshot().entryCount, 0);
  assert.equal(cache.snapshot().inflight, 0);
  assert.equal(cache.snapshot().stats.compileErrors, 1);
});

test("double release is idempotent and generic handles release as no-ops", async () => {
  const compiler = new DeferredCompiler();
  const cache = makeCache(50, compiler);

  const p = cache.acquire(request([4, 4]));
  await tick();
  compiler.resolveNext(makeResult(compiler.calls[0], { plan: plan("x"), occupiedBytes: 80 }));
  const generic = await p;
  assert.equal(generic.status, "generic");
  generic.release();
  generic.release();
  assert.equal(cache.snapshot().entryCount, 0);

  const ok = cache.acquire(request([1, 1]));
  await tick();
  compiler.resolveNext(makeResult(compiler.calls[1], { plan: plan("ok"), occupiedBytes: 10 }));
  const h = await ok;
  h.release();
  h.release();
  assert.equal(cache.snapshot().entries[0].pins, 0);
});

test("constructor and request validation reject bad input", async () => {
  assert.throws(() => new CompilationCache({ maxBytes: 10 }), TypeError);
  const compiler = new DeferredCompiler();
  const cache = makeCache(100, compiler);
  // acquire is async: validation failures surface as rejected promises.
  await assert.rejects(cache.acquire({ modelVersion: "v1", shape: [0] }), RangeError);
  await assert.rejects(cache.acquire({ shape: [1] }), TypeError);
  assert.throws(() => cache.setModelVersion(""), TypeError);
});
