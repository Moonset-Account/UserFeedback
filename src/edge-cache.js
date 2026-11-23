import { TimeoutError, UpstreamError } from './errors.js';
import { buildVariantKey, normalizeHeaders, parseVary, varyEquals } from './vary.js';

/**
 * Edge cache with conditional revalidation.
 *
 * Cached entities carry:
 *   - ETag and parsed Vary field list
 *   - originTime: the timestamp the entity was produced/refreshed at origin
 *   - freshMs / staleMs: freshness lifetime and the window in which the
 *     entity is allowed to be served stale
 *
 * Two requests share a coalesced upstream operation only when they select
 * the same variant AND validate the same entity version (ETag). Each
 * waiter keeps an independent deadline; an expired waiter falls back to
 * its own stale allowance without disturbing the other waiters.
 */
export class EdgeCache {
  /**
   * @param {object} options
   * @param {(req: object) => Promise<object>} options.origin
   *   Upstream call. Receives { url, headers } and must resolve to
   *   { status, headers, body }.
   * @param {() => number} [options.now] injectable clock (defaults to Date.now)
   * @param {(fn: Function, ms: number) => unknown} [options.setTimeout]
   *   injectable scheduler (defaults to the global setTimeout)
   * @param {(handle: unknown) => void} [options.clearTimeout]
   *   injectable scheduler cancellation (defaults to the global clearTimeout)
   */
  constructor({
    origin,
    now = () => Date.now(),
    setTimeout: schedule = globalThis.setTimeout,
    clearTimeout: cancel = globalThis.clearTimeout,
  } = {}) {
    if (typeof origin !== 'function') throw new TypeError('origin must be a function');
    this.#origin = origin;
    this.#now = now;
    this.#setTimeout = schedule;
    this.#clearTimeout = cancel;

    /** @type {Map<string, string[]>} url -> parsed Vary fields */
    this.#urlVary = new Map();
    /** @type {Map<string, Map<string, object>>} url -> variantKey -> entry */
    this.#index = new Map();
    /** @type {Map<string, object>} coalescing key -> inflight operation */
    this.#inflight = new Map();
    this.#versionCounter = 0;
    this.#stats = {
      hits: 0,
      misses: 0,
      revalidations: 0,
      coalesced: 0,
      fullFetches: 0,
      notModified: 0,
      superseded: 0,
      staleServed: 0,
      timeouts: 0,
      upstreamFailures: 0,
      varyRebuilds: 0,
    };
  }

  #origin;
  #now;
  #setTimeout;
  #clearTimeout;
  #urlVary;
  #index;
  #inflight;
  #versionCounter;
  #fullFetchCounter = 0;
  #stats;

  /**
   * Fetch a URL through the cache.
   *
   * @param {object} options
   * @param {string} options.url
   * @param {Headers|Map|Record|Array} [options.headers]
   * @param {boolean} [options.forceRefresh] bypass the cache lookup and run
   *   an unconditional upstream fetch whose 200 is stored
   * @param {number} [options.deadlineMs] individual waiting deadline
   * @param {number|boolean} [options.maxStaleMs] extra stale allowance
   *   (true = unlimited; never extends the entity's own stale window)
   * @returns {Promise<object>} { status, headers, body, fromCache,
   *   revalidated, stale, etag, variantKey, servedAt }
   */
  async fetch(options = {}) {
    const {
      url,
      headers,
      forceRefresh = false,
      deadlineMs,
      maxStaleMs = 0,
    } = options;
    if (!url || typeof url !== 'string') throw new TypeError('url is required');
    const requestHeaders = normalizeHeaders(headers);
    const allowStale = maxStaleMs === true ? Infinity : Number(maxStaleMs) || 0;

    let entry = null;
    let variantKey = null;
    if (!forceRefresh) {
      const found = this.#lookup(url, requestHeaders);
      if (found) { entry = found.entry; variantKey = found.variantKey; }
    }

    // No cached entity at all (or forced refresh): unconditional fetch.
    if (!entry) {
      this.#stats.misses += 1;
      return this.#runFullFetch(url, requestHeaders, allowStale, deadlineMs);
    }

    if (this.#isFresh(entry)) {
      this.#stats.hits += 1;
      return this.#entryResponse(entry, { fromCache: true, stale: false });
    }

    // Stale entry: conditional revalidation, coalesced per variant+ETag.
    this.#stats.revalidations += 1;
    return this.#runRevalidation({
      url,
      requestHeaders,
      entry,
      variantKey,
      allowStale,
      deadlineMs,
    });
  }

  // ---------------------------------------------------------------- lookup

  #lookup(url, requestHeaders) {
    const variants = this.#index.get(url);
    if (!variants) return null;
    const varyFields = this.#urlVary.get(url) ?? [];
    // Vary: * means variant selection is not expressible via request
    // headers: a stored response can never be reused by another request.
    if (varyFields.includes('*')) return null;
    const variantKey = buildVariantKey(varyFields, requestHeaders);
    const entry = variants.get(variantKey);
    if (!entry) return null;
    return { entry, variantKey };
  }

  #isFresh(entry) {
    // An entity is fresh while its age has not exceeded its freshness
    // lifetime (age == freshMs is still fresh).
    return this.#now() - entry.originTime <= entry.freshMs;
  }

  /**
   * Age beyond the freshness lifetime at which the entity stops being
   * servable stale. Never exceeds the entity's own stale window.
   */
  #effectiveStaleLimit(entry, allowStale) {
    const extra = allowStale === true ? Infinity : allowStale;
    return entry.freshMs + Math.min(entry.staleMs, extra);
  }

  #canServeStale(entry, allowStale) {
    // Stale serving ends exactly once age exceeds the effective window.
    return this.#now() - entry.originTime <= this.#effectiveStaleLimit(entry, allowStale);
  }

  #entryResponse(entry, { fromCache, stale, revalidated = false }) {
    return {
      status: 200,
      headers: { ...entry.responseHeaders },
      body: entry.body,
      fromCache,
      revalidated,
      stale,
      etag: entry.etag,
      variantKey: entry.variantKey,
      servedAt: this.#now(),
    };
  }

  // ------------------------------------------------------------- storage

  #buildEntry({ url, variantKey, etag, varyFields, responseHeaders, body, originTime, requestHeaders }) {
    const { freshMs, staleMs } = readFreshnessWindows(responseHeaders);
    return {
      version: ++this.#versionCounter,
      url,
      variantKey,
      etag: etag ?? null,
      varyFields,
      // The request headers that selected this variant; needed to rebuild
      // the index when a later response changes the Vary field list.
      requestHeaders,
      responseHeaders,
      body,
      originTime,
      freshMs,
      staleMs,
    };
  }

  /**
   * Store a brand-new entity from a 200.
   *
   * When the Vary field list is unchanged the other variants are kept and
   * only the target key is replaced. When the Vary list changes, the whole
   * index is rebuilt from each stored entry's request headers and old
   * index aliases are removed.
   */
  #storeFromFull(url, requestHeaders, responseHeaders, body, originTime) {
    const varyFields = readVary(responseHeaders);
    const variantKey = buildVariantKey(varyFields, requestHeaders);
    const entry = this.#buildEntry({
      url, variantKey, etag: readEtag(responseHeaders), varyFields,
      responseHeaders, body, originTime, requestHeaders,
    });

    let variants = this.#index.get(url);
    const previousVary = this.#urlVary.get(url);
    if (!variants) {
      variants = new Map();
      this.#index.set(url, variants);
    }

    if (!previousVary || varyEquals(previousVary, varyFields)) {
      // Same indexing scheme: keep every other variant.
      variants.set(variantKey, entry);
      this.#urlVary.set(url, varyFields);
      return entry;
    }

    // The Vary scheme changed: rebuild the index once, reindexing all
    // surviving entries from their own request headers.
    const survivors = [...variants.values()];
    variants.clear();
    for (const oldEntry of survivors) {
      const key = buildVariantKey(varyFields, oldEntry.requestHeaders);
      const reindexed = { ...oldEntry, varyFields, variantKey: key };
      if (key === variantKey && oldEntry.version > entry.version) {
        // A newer entity already occupies the target key: it stays.
        variants.set(key, reindexed);
      } else if (!variants.has(key)) {
        variants.set(key, reindexed);
      }
    }
    variants.set(variantKey, entry);
    this.#urlVary.set(url, varyFields);
    return entry;
  }

  // -------------------------------------------------------- inflight core

  #joinOrCreate(coalescingKey, createOp) {
    const existing = this.#inflight.get(coalescingKey);
    if (existing) {
      this.#stats.coalesced += 1;
      return { op: existing, joined: true };
    }
    const op = createOp();
    op.waiters = [];
    op.settled = false;
    this.#inflight.set(coalescingKey, op);
    return { op, joined: false };
  }

  #attachWaiter(op, { deadlineMs, allowStale, entry, requestHeaders, onTimeout }) {
    const waiter = {
      allowStale,
      deadlineMs,
      requestHeaders,
      staleEntry: entry,
      settled: false,
      resolve: null,
      reject: null,
      timer: null,
      onTimeout,
    };
    const promise = new Promise((resolve, reject) => {
      waiter.resolve = resolve;
      waiter.reject = reject;
    });
    if (Number.isFinite(deadlineMs) && deadlineMs >= 0) {
      waiter.timer = this.#setTimeout(() => {
        if (waiter.settled || op.settled) return;
        waiter.settled = true;
        this.#stats.timeouts += 1;
        onTimeout();
        if (waiter.staleEntry && this.#canServeStale(waiter.staleEntry, waiter.allowStale)) {
          this.#stats.staleServed += 1;
          waiter.resolve(this.#entryResponse(waiter.staleEntry, { fromCache: true, stale: true }));
        } else {
          waiter.reject(new TimeoutError(`deadline of ${deadlineMs}ms elapsed before upstream responded`));
        }
      }, deadlineMs);
    }
    op.waiters.push(waiter);
    return { waiter, promise };
  }

  #settleWaiters(op, buildResult, buildError) {
    op.settled = true;
    for (const waiter of op.waiters) {
      if (waiter.settled) continue; // individual deadline already expired
      waiter.settled = true;
      if (waiter.timer) this.#clearTimeout(waiter.timer);
      if (buildError) {
        // Per-waiter stale judgment; serving stale never extends validity.
        if (waiter.staleEntry && this.#canServeStale(waiter.staleEntry, waiter.allowStale)) {
          this.#stats.staleServed += 1;
          waiter.resolve(this.#entryResponse(waiter.staleEntry, { fromCache: true, stale: true }));
        } else {
          waiter.reject(buildError(waiter));
        }
      } else {
        try {
          waiter.resolve(buildResult(waiter));
        } catch (error) {
          waiter.reject(error);
        }
      }
    }
  }

  #drainWaiter(op, waiter) {
    const index = op.waiters.indexOf(waiter);
    if (index >= 0) op.waiters.splice(index, 1);
  }

  // ----------------------------------------------------------- full fetch

  #runFullFetch(url, requestHeaders, allowStale, deadlineMs) {
    // Unconditional fetches have no entity version to validate, so the
    // same-variant/same-ETag coalescing rule can never apply: every cold
    // miss or forced refresh gets its own upstream operation.
    const coalescingKey = `full:${url}#${++this.#fullFetchCounter}`;
    const op = {
      kind: 'full',
      url,
      requestHeaders,
      initiatingHeaders: requestHeaders,
      waiters: [],
      settled: false,
    };
    this.#inflight.set(coalescingKey, op);
    const holder = {};
    const { promise } = this.#attachWaiter(op, {
      deadlineMs,
      allowStale,
      entry: null,
      requestHeaders,
      onTimeout: () => this.#drainWaiter(op, holder.waiter),
    });
    holder.waiter = op.waiters[op.waiters.length - 1];

    this.#stats.fullFetches += 1;
    this.#callOrigin(url, requestHeaders)
      .then((response) => this.#settleFull(coalescingKey, op, response))
      .catch((error) => this.#failOp(coalescingKey, op, error));
    return promise;
  }

  #settleFull(coalescingKey, op, response) {
    if (op.settled) return;
    this.#inflight.delete(coalescingKey);
    if (response.status >= 200 && response.status < 300) {
      // Storage happens exactly once, even if every waiter's individual
      // deadline already expired and drained them from the operation.
      const originTime = this.#now();
      const stored = this.#storeFromFull(
        op.url,
        op.initiatingHeaders,
        { ...(response.headers ?? {}) },
        response.body,
        originTime,
      );
      this.#settleWaiters(op, (waiter) => {
        const found = this.#lookup(op.url, waiter.requestHeaders);
        return this.#entryResponse(found ? found.entry : stored, {
          fromCache: false,
          stale: false,
        });
      }, null);
      return;
    }
    this.#stats.upstreamFailures += 1;
    const error = new UpstreamError(`origin returned status ${response.status}`, {
      code: 'UPSTREAM_NON_2XX',
    });
    this.#settleWaiters(op, null, () => error);
  }

  #failOp(coalescingKey, op, cause) {
    if (op.settled) return;
    this.#inflight.delete(coalescingKey);
    this.#stats.upstreamFailures += 1;
    const error = cause instanceof UpstreamError
      ? cause
      : new UpstreamError('origin request failed', { cause });
    this.#settleWaiters(op, null, () => error);
  }

  // --------------------------------------------------------- revalidation

  #runRevalidation({ url, requestHeaders, entry, variantKey, allowStale, deadlineMs }) {
    // Coalescing requires same variant AND same entity version (ETag).
    const coalescingKey = `reval:${url}|${variantKey}|${entry.etag ?? 'no-etag'}`;
    const { op, joined } = this.#joinOrCreate(coalescingKey, () => ({
      kind: 'revalidate',
      url,
      variantKey,
      etag: entry.etag,
      baseVersion: entry.version,
      baseOriginTime: entry.originTime,
      varyFields: [...entry.varyFields],
    }));
    // The Vary rebuild on a 304 must use the initiating request's headers.
    if (!joined) op.initiatingHeaders = requestHeaders;

    const { waiter, promise } = this.#attachWaiter(op, {
      deadlineMs,
      allowStale,
      entry,
      requestHeaders,
      onTimeout: () => this.#drainWaiter(op, waiter),
    });

    if (!joined) {
      const conditionalHeaders = [...requestHeaders];
      if (op.etag) conditionalHeaders.push(['if-none-match', op.etag]);
      this.#callOrigin(url, conditionalHeaders)
        .then((response) => this.#settleRevalidation(coalescingKey, op, response))
        .catch((error) => this.#failOp(coalescingKey, op, error));
    }
    return promise;
  }

  #settleRevalidation(coalescingKey, op, response) {
    if (op.settled) return;
    this.#inflight.delete(coalescingKey);

    if (response.status >= 500 && response.status < 600) {
      this.#stats.upstreamFailures += 1;
      const error = new UpstreamError(`origin returned status ${response.status}`, {
        code: 'UPSTREAM_5XX',
      });
      this.#settleWaiters(op, null, () => error);
      return;
    }

    if (response.status >= 200 && response.status < 300) {
      this.#handleRevalidate200(op, response);
      return;
    }

    if (response.status === 304) {
      this.#handleRevalidate304(op, response);
      return;
    }

    this.#stats.upstreamFailures += 1;
    const error = new UpstreamError(`origin returned unexpected status ${response.status}`, {
      code: 'UPSTREAM_UNEXPECTED_STATUS',
    });
    this.#settleWaiters(op, null, () => error);
  }

  #handleRevalidate200(op, response) {
    const originTime = this.#now();
    const responseHeaders = { ...(response.headers ?? {}) };
    // Store exactly once from the initiating request, independent of how
    // many individual waiters (if any) are still attached.
    const stored = this.#storeFromFull(
      op.url,
      op.initiatingHeaders,
      responseHeaders,
      response.body,
      originTime,
    );
    if (stored.version > op.baseVersion) this.#stats.superseded += 1;
    this.#settleWaiters(op, (waiter) => {
      const found = this.#lookup(op.url, waiter.requestHeaders);
      return this.#entryResponse(found ? found.entry : stored, {
        fromCache: false,
        stale: false,
      });
    }, null);
  }

  #handleRevalidate304(op, response) {
    const responseHeaders = { ...(response.headers ?? {}) };
    const variants = this.#index.get(op.url);
    const current = variants ? variants.get(op.variantKey) : null;

    // A newer 200 already replaced the entity this 304 validates.
    // The entity *version* is identified by its ETag: the late 304 must
    // never make an old ETag fresh again while a newer one is current.
    if (!current || current.etag !== op.etag) {
      this.#stats.superseded += 1;
      this.#settleWaiters(op, (waiter) => {
        const fresh = this.#lookup(op.url, waiter.requestHeaders);
        if (fresh) {
          return this.#entryResponse(fresh.entry, {
            fromCache: true,
            stale: !this.#isFresh(fresh.entry),
          });
        }
        if (waiter.staleEntry && this.#canServeStale(waiter.staleEntry, waiter.allowStale)) {
          this.#stats.staleServed += 1;
          return this.#entryResponse(waiter.staleEntry, { fromCache: true, stale: true });
        }
        throw new UpstreamError('304 could not be applied: validated entity was superseded', {
          code: 'STALE_304',
        });
      }, null);
      return;
    }

    const newVary = parseVary(responseHeaders.vary ?? responseHeaders.Vary);
    const varyChanged = newVary.length > 0 && !varyEquals(newVary, op.varyFields);

    // Refresh the validated entity in place. Origin time is renewed only
    // because the origin just confirmed this exact entity version.
    const windows = readFreshnessWindows(responseHeaders);
    const refreshed = {
      ...current,
      version: ++this.#versionCounter,
      etag: readEtag(responseHeaders) ?? current.etag,
      originTime: this.#now(),
      freshMs: windows.freshMs !== undefined ? windows.freshMs : current.freshMs,
      staleMs: windows.staleMs !== undefined ? windows.staleMs : current.staleMs,
      responseHeaders: { ...current.responseHeaders, ...responseHeaders },
    };

    if (!varyChanged) {
      variants.set(op.variantKey, refreshed);
      this.#stats.notModified += 1;
      this.#settleWaiters(op, () => this.#entryResponse(refreshed, {
        fromCache: true, revalidated: true, stale: false,
      }), null);
      return;
    }

    // New Vary field: rebuild the index using the *initiating* request's
    // headers and remove all old-scheme aliases. A newer entity already
    // sitting at the target key stays available.
    this.#stats.varyRebuilds += 1;
    this.#stats.notModified += 1;
    const targetKey = buildVariantKey(newVary, op.initiatingHeaders);
    const occupant = variants.get(targetKey);

    for (const oldKey of [...variants.keys()]) variants.delete(oldKey);
    if (occupant && occupant.etag !== op.etag) {
      // A newer (different-ETag) entity at the target key stays available.
      variants.set(targetKey, occupant);
      this.#urlVary.set(op.url, newVary);
      this.#stats.superseded += 1;
      this.#settleWaiters(op, (waiter) => {
        if (occupant.variantKey === targetKey) {
          return this.#entryResponse(occupant, {
            fromCache: true,
            stale: !this.#isFresh(occupant),
          });
        }
        const again = this.#lookup(op.url, waiter.requestHeaders);
        if (again) {
          return this.#entryResponse(again.entry, {
            fromCache: true,
            stale: !this.#isFresh(again.entry),
          });
        }
        if (waiter.staleEntry && this.#canServeStale(waiter.staleEntry, waiter.allowStale)) {
          this.#stats.staleServed += 1;
          return this.#entryResponse(waiter.staleEntry, { fromCache: true, stale: true });
        }
        throw new UpstreamError('304 rebuilt Vary index but no entity matches this request', {
          code: 'VARY_REBUILD_NO_ENTITY',
        });
      }, null);
      return;
    }

    refreshed.varyFields = newVary;
    refreshed.variantKey = targetKey;
    variants.set(targetKey, refreshed);
    this.#urlVary.set(op.url, newVary);
    this.#settleWaiters(op, () => this.#entryResponse(refreshed, {
      fromCache: true, revalidated: true, stale: false,
    }), null);
  }

  #callOrigin(url, headers) {
    try {
      return Promise.resolve(this.#origin({ url, headers }));
    } catch (error) {
      return Promise.reject(error);
    }
  }

  // ------------------------------------------------------ test/inspection

  /** Snapshot of counters (hits, coalesced, failures, ...). */
  getStats() {
    return { ...this.#stats };
  }

  /** Number of variant entries currently stored for a URL. */
  variantCount(url) {
    return this.#index.get(url)?.size ?? 0;
  }

  /** Current parsed Vary fields registered for a URL. */
  getVaryFields(url) {
    return [...(this.#urlVary.get(url) ?? [])];
  }

  /** Read-only view of a stored entry, or null. */
  getEntry(url, requestHeaders) {
    const found = this.#lookup(url, normalizeHeaders(requestHeaders));
    return found ? { ...found.entry } : null;
  }

  /** Number of currently coalesced upstream operations. */
  inflightCount() {
    return this.#inflight.size;
  }
}

// ---------------------------------------------------------- header helpers

function readEtag(headers) {
  return lookupHeader(headers, 'etag');
}

function readVary(headers) {
  return parseVary(lookupHeader(headers, 'vary'));
}

/** Case-insensitive response-header lookup. */
function lookupHeader(headers, name) {
  if (headers[name] !== undefined) return headers[name];
  const lower = name.toLowerCase();
  for (const key of Object.keys(headers)) {
    if (key.toLowerCase() === lower) return headers[key];
  }
  return undefined;
}

/**
 * Read freshness windows. Supports explicit synthetic headers used by the
 * test harness (x-cache-fresh-ms / x-cache-stale-ms) and falls back to
 * Cache-Control max-age / stale-while-revalidate.
 */
function readFreshnessWindows(headers) {
  const numeric = (value) => {
    if (value === undefined || value === null) return undefined;
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : undefined;
  };
  let freshMs = numeric(lookupHeader(headers, 'x-cache-fresh-ms'));
  let staleMs = numeric(lookupHeader(headers, 'x-cache-stale-ms'));

  const cacheControl = lookupHeader(headers, 'cache-control');
  if (cacheControl) {
    const maxAge = /(?:^|,\s*)max-age=(\d+)/i.exec(cacheControl);
    if (maxAge && freshMs === undefined) freshMs = Number(maxAge[1]);
    const swr = /stale-while-revalidate=(\d+)/i.exec(cacheControl);
    if (swr && staleMs === undefined) staleMs = Number(swr[1]);
  }
  return {
    freshMs: freshMs ?? 0,
    staleMs: staleMs ?? 0,
  };
}
