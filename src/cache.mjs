// Compilation cache layer for an inference service.
//
// Responsibilities:
//   1. Reuse a cached plan only when the model version matches AND every shape
//      guard clause passes (ranges, inter-dimension equality/divisibility,
//      layout and dtype). A max-only comparison is never performed.
//   2. Coalesce identical concurrent compile requests into one compiler call.
//      The produced plan is handed back only to waiters whose requests it
//      actually covers; uncovered waiters take the generic path.
//   3. Pin plans while calls are in flight (pinned plans are immune to
//      eviction) and unpin on release().
//   4. On model-version switch, old compilations are never inserted into the
//      new cache; a compilation that was already started keeps its original
//      plan until the caller releases it.
//   5. When the byte budget is approached, evict unpinned entries (LRU); if
//      the result still does not fit, return an explicit generic handle.

import { guardMatches, normalizeGuard, normalizeShape, requestKey } from "./guards.mjs";

const GENERIC_REASONS = Object.freeze({
  GUARD_MISMATCH: "guard-mismatch",
  STALE_VERSION: "stale-version",
  CAPACITY: "capacity",
});

function validateRequest(request) {
  if (request === null || typeof request !== "object") {
    throw new TypeError("request must be an object");
  }
  if (typeof request.modelVersion !== "string" || request.modelVersion.length === 0) {
    throw new TypeError("request.modelVersion must be a non-empty string");
  }
  normalizeShape(request.shape);
  if (request.layout !== undefined && typeof request.layout !== "string") {
    throw new TypeError("request.layout must be a string when provided");
  }
  if (request.dtype !== undefined && typeof request.dtype !== "string") {
    throw new TypeError("request.dtype must be a string when provided");
  }
}

function validateCompileResult(result, rank) {
  if (result === null || typeof result !== "object") {
    throw new TypeError("compile result must be { plan, occupiedBytes, guard }");
  }
  if (!Number.isInteger(result.occupiedBytes) || result.occupiedBytes < 0) {
    throw new RangeError("compile result.occupiedBytes must be a non-negative integer");
  }
  if (result.plan === undefined || result.plan === null) {
    throw new TypeError("compile result.plan is required");
  }
  return normalizeGuard(result.guard ?? {}, rank);
}

export class CompilationCache {
  /**
   * @param {object} options
   * @param {(request: object) => Promise<{plan: *, occupiedBytes: number, guard: object}>} options.compile
   *        Controlled compiler callback.
   * @param {number} options.maxBytes  Total occupancy budget in bytes.
   * @param {string} [options.modelVersion]  Initial model version.
   * @param {(info: object) => void} [options.onGeneric]  Observability hook for generic fallbacks.
   */
  constructor(options) {
    if (options === null || typeof options !== "object") {
      throw new TypeError("CompilationCache requires options");
    }
    if (typeof options.compile !== "function") {
      throw new TypeError("options.compile must be an async compiler callback");
    }
    if (!Number.isInteger(options.maxBytes) || options.maxBytes <= 0) {
      throw new RangeError("options.maxBytes must be a positive integer");
    }
    this._compile = options.compile;
    this._maxBytes = options.maxBytes;
    this._modelVersion = options.modelVersion ?? "v1";
    this._onGeneric = options.onGeneric ?? null;

    this._entries = new Map(); // id -> entry
    this._inflight = new Map(); // requestKey -> job
    this._nextId = 1;
    // Monotonic logical clock for deterministic LRU ordering.
    this._clock = 0;

    // Diagnostics counters.
    this._stats = {
      compileCalls: 0,
      cacheHits: 0,
      coalesced: 0,
      evictions: 0,
      compileErrors: 0,
      generic: {
        [GENERIC_REASONS.GUARD_MISMATCH]: 0,
        [GENERIC_REASONS.STALE_VERSION]: 0,
        [GENERIC_REASONS.CAPACITY]: 0,
      },
    };
  }

  get modelVersion() {
    return this._modelVersion;
  }

  get maxBytes() {
    return this._maxBytes;
  }

  /** Sum of occupiedBytes over all retained (including pinned) entries. */
  get occupiedBytes() {
    let total = 0;
    for (const entry of this._entries.values()) total += entry.occupiedBytes;
    return total;
  }

  /**
   * Switch the active model version.
   * Unpinned entries from the previous version are removed immediately; entries
   * still pinned by in-flight calls stay retained (and keep counting against
   * the budget) until released. Nothing from an old-version compilation can be
   * inserted after this point.
   */
  setModelVersion(version) {
    if (typeof version !== "string" || version.length === 0) {
      throw new TypeError("model version must be a non-empty string");
    }
    if (version === this._modelVersion) return;
    this._modelVersion = version;
    for (const [id, entry] of this._entries) {
      if (entry.modelVersion !== version && entry.pins === 0) {
        this._entries.delete(id);
      }
    }
  }

  /**
   * Acquire a plan for a request.
   * @returns {Promise<object>} handle { status, plan, release(), cached, reason?, entryId?, error? }
   *   - status "cached":  a reused or freshly compiled cached plan
   *   - status "generic": explicit generic execution path; plan may be a
   *                        one-shot plan or null
   * A compiler failure rejects the returned promise; callers may retry, which
   * starts a fresh compilation (failed jobs leave no cache state behind).
   */
  async acquire(request) {
    validateRequest(request);
    if (request.modelVersion !== this._modelVersion) {
      throw new RangeError(
        `request modelVersion "${request.modelVersion}" does not match active version "${this._modelVersion}"`,
      );
    }

    // 1. Look for a current-version entry whose full guard matches. Most
    //    recently used first so overlapping guards resolve deterministically.
    const hit = this.#findUsable(request);
    if (hit) {
      hit.lastUsed = ++this._clock;
      hit.pins += 1;
      this._stats.cacheHits += 1;
      return this.#cachedHandle(hit);
    }

    // 2. Coalesce identical concurrent requests onto the running job. Each
    //    waiter still gets its own handle (and its own pin) when it settles.
    const key = requestKey(request);
    const existing = this._inflight.get(key);
    if (existing) {
      this._stats.coalesced += 1;
      const waiter = this.#makeWaiter(request);
      existing.waiters.push(waiter);
      return waiter.promise;
    }

    // 3. Start a fresh compilation and fan the result out to all waiters.
    const job = { key, request, waiters: [this.#makeWaiter(request)] };
    this._inflight.set(key, job);
    this.#runCompile(job);
    return job.waiters[0].promise;
  }

  #makeWaiter(request) {
    const waiter = { request, resolve: null, reject: null, promise: null };
    waiter.promise = new Promise((resolve, reject) => {
      waiter.resolve = resolve;
      waiter.reject = reject;
    });
    return waiter;
  }

  async #runCompile(job) {
    this._stats.compileCalls += 1;
    let result;
    try {
      result = await this._compile(job.request);
    } catch (error) {
      if (this._inflight.get(job.key) === job) this._inflight.delete(job.key);
      this._stats.compileErrors += 1;
      // Failure recovery: no state is retained; every waiter sees the rejection
      // and can retry, coalescing again into a fresh compilation.
      for (const waiter of job.waiters) waiter.reject(error);
      return;
    }
    if (this._inflight.get(job.key) === job) this._inflight.delete(job.key);
    this.#finishCompile(job, result);
  }

  #finishCompile(job, result) {
    const rank = job.request.shape.length;
    let guard;
    try {
      guard = validateCompileResult(result, rank);
    } catch (error) {
      this._stats.compileErrors += 1;
      for (const waiter of job.waiters) waiter.reject(error);
      return;
    }

    // Version switch while the compiler was running: never populate the new
    // cache with an old-version plan. Every waiter keeps a one-shot handle to
    // the original plan until it calls release(); no pin is needed because the
    // plan is not stored anywhere.
    if (job.request.modelVersion !== this._modelVersion) {
      for (const waiter of job.waiters) {
        this._stats.generic[GENERIC_REASONS.STALE_VERSION] += 1;
        this.#emitGeneric(waiter.request, GENERIC_REASONS.STALE_VERSION, result.plan);
        waiter.resolve(this.#genericHandle(result.plan, GENERIC_REASONS.STALE_VERSION));
      }
      return;
    }

    // Partition waiters by whether the new guard actually covers them.
    const covered = [];
    const uncovered = [];
    for (const waiter of job.waiters) {
      if (guardMatches(guard, waiter.request.shape, waiter.request)) covered.push(waiter);
      else uncovered.push(waiter);
    }

    if (covered.length === 0) {
      for (const waiter of job.waiters) {
        this._stats.generic[GENERIC_REASONS.GUARD_MISMATCH] += 1;
        this.#emitGeneric(waiter.request, GENERIC_REASONS.GUARD_MISMATCH, result.plan);
        waiter.resolve(this.#genericHandle(result.plan, GENERIC_REASONS.GUARD_MISMATCH));
      }
      return;
    }

    const entry = this.#tryInsert({
      modelVersion: job.request.modelVersion,
      plan: result.plan,
      occupiedBytes: result.occupiedBytes,
      guard,
      shape: Object.freeze([...job.request.shape]),
      key: job.key,
    });

    if (!entry) {
      // Budget exhausted even after evicting every unpinned entry: explicit
      // generic execution. The freshly compiled plan may still run once.
      for (const waiter of job.waiters) {
        this._stats.generic[GENERIC_REASONS.CAPACITY] += 1;
        this.#emitGeneric(waiter.request, GENERIC_REASONS.CAPACITY, result.plan);
        waiter.resolve(this.#genericHandle(result.plan, GENERIC_REASONS.CAPACITY));
      }
      return;
    }

    for (const waiter of covered) {
      entry.pins += 1;
      waiter.resolve(this.#cachedHandle(entry));
    }
    for (const waiter of uncovered) {
      this._stats.generic[GENERIC_REASONS.GUARD_MISMATCH] += 1;
      this.#emitGeneric(waiter.request, GENERIC_REASONS.GUARD_MISMATCH, result.plan);
      waiter.resolve(this.#genericHandle(result.plan, GENERIC_REASONS.GUARD_MISMATCH));
    }
  }

  /**
   * Attempt to insert an entry, evicting unpinned LRU entries as needed.
   * The existing cache is only mutated when the new entry is known to fit,
   * so an oversized result never partially destroys the cache.
   */
  #tryInsert(details) {
    const removable = [];
    for (const entry of this._entries.values()) {
      if (entry.modelVersion === this._modelVersion && entry.pins === 0) {
        removable.push(entry);
      }
    }
    removable.sort((a, b) => a.lastUsed - b.lastUsed);

    let available = this._maxBytes - this.occupiedBytes;
    const victims = [];
    for (const candidate of removable) {
      if (available >= details.occupiedBytes) break;
      victims.push(candidate);
      available += candidate.occupiedBytes;
    }
    if (available < details.occupiedBytes) return null;

    for (const victim of victims) {
      this._entries.delete(victim.id);
      this._stats.evictions += 1;
    }

    const entry = {
      id: this._nextId++,
      modelVersion: details.modelVersion,
      plan: details.plan,
      occupiedBytes: details.occupiedBytes,
      guard: details.guard,
      shape: details.shape,
      key: details.key,
      pins: 0,
      lastUsed: ++this._clock,
    };
    this._entries.set(entry.id, entry);
    return entry;
  }

  #findUsable(request) {
    let best = null;
    for (const entry of this._entries.values()) {
      if (entry.modelVersion !== request.modelVersion) continue;
      if (!guardMatches(entry.guard, request.shape, request)) continue;
      if (best === null || entry.lastUsed > best.lastUsed) best = entry;
    }
    return best;
  }

  #cachedHandle(entry) {
    let released = false;
    return {
      status: "cached",
      cached: true,
      reason: null,
      entryId: entry.id,
      plan: entry.plan,
      release: () => {
        if (released) return;
        released = true;
        entry.pins -= 1;
        // A retained old-version entry whose last caller went away after a
        // version switch is dropped instead of lingering in the new cache.
        if (entry.pins === 0 && entry.modelVersion !== this._modelVersion) {
          this._entries.delete(entry.id);
        }
      },
    };
  }

  #genericHandle(plan, reason) {
    return {
      status: "generic",
      cached: false,
      reason,
      entryId: null,
      plan,
      release: () => {},
    };
  }

  #emitGeneric(request, reason, plan) {
    if (this._onGeneric) {
      this._onGeneric({ request, reason, hasPlan: plan !== null && plan !== undefined });
    }
  }

  /** Read-only diagnostics snapshot for tests and operators. */
  snapshot() {
    return {
      modelVersion: this._modelVersion,
      maxBytes: this._maxBytes,
      occupiedBytes: this.occupiedBytes,
      entryCount: this._entries.size,
      inflight: this._inflight.size,
      stats: {
        compileCalls: this._stats.compileCalls,
        cacheHits: this._stats.cacheHits,
        coalesced: this._stats.coalesced,
        evictions: this._stats.evictions,
        compileErrors: this._stats.compileErrors,
        generic: { ...this._stats.generic },
      },
      entries: [...this._entries.values()].map((entry) => ({
        id: entry.id,
        modelVersion: entry.modelVersion,
        occupiedBytes: entry.occupiedBytes,
        pins: entry.pins,
        shape: [...entry.shape],
        key: entry.key,
      })),
    };
  }
}

export { GENERIC_REASONS };
