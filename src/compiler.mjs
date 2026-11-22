// Controllable compiler callbacks.
//
// The cache never talks to a real compiler: it calls an injected async callback
// `compile(request) -> { plan, occupiedBytes, guard }`. Two implementations are
// provided so tests can schedule completions deterministically.

/**
 * Deferred compiler. Every call records the request and returns a pending
 * promise; tests decide when (and with which plan / bytes / guard) each call
 * resolves, or make it reject.
 *
 * Identical concurrent requests are expected to be merged *by the cache*, so
 * with a correctly behaving cache the compiler sees one call per coalesced group.
 */
export class DeferredCompiler {
  constructor() {
    this.calls = [];
    this.pending = [];
  }

  get callCount() {
    return this.calls.length;
  }

  compile(request) {
    const entry = {
      request,
      resolve: null,
      reject: null,
      settled: false,
      promise: null,
    };
    entry.promise = new Promise((resolve, reject) => {
      entry.resolve = (result) => {
        if (entry.settled) return;
        entry.settled = true;
        resolve(result);
      };
      entry.reject = (error) => {
        if (entry.settled) return;
        entry.settled = true;
        reject(error);
      };
    });
    this.calls.push(request);
    this.pending.push(entry);
    return entry.promise;
  }

  /** The oldest unresolved compilation. */
  next() {
    return this.pending.find((entry) => !entry.settled) ?? null;
  }

  /** Resolve the oldest unresolved compilation with `result`. */
  resolveNext(result) {
    const entry = this.next();
    if (!entry) throw new Error("no pending compilation to resolve");
    entry.resolve(result);
    return entry;
  }

  /** Reject the oldest unresolved compilation with `error`. */
  rejectNext(error) {
    const entry = this.next();
    if (!entry) throw new Error("no pending compilation to reject");
    entry.reject(error);
    return entry;
  }

  /** All currently unresolved compilations, oldest first. */
  unresolved() {
    return this.pending.filter((entry) => !entry.settled);
  }
}

/**
 * Handler compiler: each request is handled by a registered handler function
 * (or an async one). Useful for straightforward tests that do not need manual
 * promise control. A default handler may be supplied at construction time.
 */
export class HandlerCompiler {
  constructor(handler) {
    if (typeof handler !== "function") {
      throw new TypeError("HandlerCompiler requires a handler function");
    }
    this._handler = handler;
    this.calls = [];
  }

  get callCount() {
    return this.calls.length;
  }

  async compile(request) {
    this.calls.push(request);
    return this._handler(request);
  }
}

/**
 * Build a compile result in the shape expected by the cache.
 * `plan` defaults to a unique object so plan identity is observable in tests.
 */
export function makeResult(request, overrides = {}) {
  return {
    plan: overrides.plan ?? { id: `plan-${Math.random().toString(36).slice(2)}` },
    occupiedBytes: overrides.occupiedBytes ?? 0,
    guard: overrides.guard ?? {
      dims: request.shape.map((dim) => ({ min: dim, max: dim })),
      equal: [],
      divisible: [],
      layout: request.layout,
      dtype: request.dtype,
    },
  };
}
