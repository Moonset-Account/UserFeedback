/**
 * Deterministic, in-memory test origin.
 *
 * Every origin call is a deferred "gate": the test decides exactly when
 * each call resolves and what it returns, so concurrency interleavings
 * (a concurrent 200 overtaking a late 304, independent deadlines, ...)
 * can be driven without real timers or real network access.
 */
export function createControlledOrigin() {
  const calls = [];
  const pending = [];

  function origin(request) {
    const call = {
      id: calls.length + 1,
      request,
      gate: createGate(),
    };
    calls.push(call);
    pending.push(call);
    return call.gate.promise;
  }

  return {
    origin,
    /** All calls ever made, in order. */
    calls,
    /** Calls that have not yet been resolved. */
    pending,
    /** The oldest unresolved call (throws if the plan is exhausted). */
    next() {
      // Calls can also be settled directly through their gate; drop any
      // already settled entries so the FIFO queue stays consistent.
      while (pending.length && (pending[0].gate.resolved || pending[0].gate.rejected)) {
        pending.shift();
      }
      const call = pending.shift();
      if (!call) throw new Error('no pending origin call: test plan is exhausted');
      return call;
    },
    /** Resolve the oldest unresolved call. */
    respondNext(response) {
      this.next().gate.resolve(response);
    },
    /** Reject the oldest unresolved call (simulated network failure). */
    failNext(error = new Error('simulated network failure')) {
      this.next().gate.reject(error);
    },
    /** Number of calls made so far. */
    callCount() {
      return calls.length;
    },
  };
}

/** A manually resolved promise with externally visible state. */
export function createGate() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return {
    promise,
    resolved: false,
    rejected: false,
    resolve(value) {
      this.resolved = true;
      resolve(value);
    },
    reject(error) {
      this.rejected = true;
      reject(error);
    },
  };
}

/**
 * Build an origin response carrying the freshness headers the cache
 * understands. `vary` may be a string or an array of header names.
 */
export function originResponse({
  status = 200,
  body,
  etag,
  vary,
  freshMs = 1000,
  staleMs = 0,
  extraHeaders = {},
}) {
  const headers = {
    'x-cache-fresh-ms': String(freshMs),
    'x-cache-stale-ms': String(staleMs),
    ...extraHeaders,
  };
  if (etag) headers.etag = etag;
  if (vary) headers.vary = Array.isArray(vary) ? vary.join(', ') : vary;
  return { status, headers, body };
}
