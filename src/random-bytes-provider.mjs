/**
 * Random byte provider for the credential issuer.
 *
 * The provider does not generate randomness itself: the caller injects an
 * already-initialized system random source. The provider adds the pieces the
 * issuer needs on top of that source:
 *
 *  - a readiness state per *process generation* (a process that was forked
 *    from a parent must not reuse the parent's readiness),
 *  - a shared, lazy initialization per generation (concurrent requests in one
 *    generation ride on a single initialization; a failed initialization is
 *    reported to every waiter of that generation and is retried by the next
 *    request),
 *  - per-request cancellation (aborting one waiter never affects siblings,
 *    the initialization, or the generation),
 *  - protection against late callbacks from a superseded generation.
 *
 * Injected source contract
 * ------------------------
 * {
 *   randomBytes(size: number, context: object): Uint8Array, // required
 *   initialize?(context: object): void | Promise<void>,     // optional
 * }
 *
 * `initialize` is the per-generation activation hook. A plain object exposing
 * only `randomBytes` (e.g. `{ randomBytes: (n) => crypto.randomBytes(n) }`) is
 * a valid source: the provider then completes initialization on a microtask.
 *
 * `context` passed to both hooks is frozen and contains:
 *   { generation, attempt, initializationId, requestId, label }
 * `initializationId` is the provider-wide ordinal of the successful
 * initialization the bytes came from; it is `null` in `initialize` (the
 * ordinal only exists once initialization succeeds) and is also available in
 * `randomBytes` so a test double can attribute every read.
 */

import {
  RandomBytesGenerationSupersededError,
  RandomBytesInitializationError,
  RandomBytesProviderClosedError,
  RandomBytesReadError,
  RandomBytesRequestAbortedError,
} from './errors.mjs';

const INITIAL_GENERATION = 1;

function assertSize(size) {
  if (typeof size !== 'number' || !Number.isSafeInteger(size)) {
    throw new TypeError('random byte request size must be a safe integer');
  }
  if (size < 1) {
    throw new RangeError('random byte request size must be at least 1');
  }
}

function assertSource(source) {
  if (source === null || typeof source !== 'object') {
    throw new TypeError('random source must be an object');
  }
  if (typeof source.randomBytes !== 'function') {
    throw new TypeError('random source must expose randomBytes(size)');
  }
  if (source.initialize !== undefined && typeof source.initialize !== 'function') {
    throw new TypeError('random source initialize must be a function when present');
  }
}

export function createRandomBytesProvider({ source, lifecycle } = {}) {
  assertSource(source);
  if (lifecycle !== undefined) {
    if (lifecycle === null || typeof lifecycle.onWorkingCopy !== 'function') {
      throw new TypeError('lifecycle adapter must expose onWorkingCopy(listener)');
    }
  }

  let closed = false;
  let generationCounter = INITIAL_GENERATION;
  let requestCounter = 0;
  let successfulInitializations = 0;

  /** @type {object[]} all generation records, oldest first, for diagnostics */
  const generations = [];

  function createGenerationRecord(generation) {
    const record = {
      generation,
      state: 'idle', // idle | initializing | ready | failed | superseded | closed
      attempt: 0,
      initializationId: null,
      error: null,
      waiters: new Map(), // requestId -> waiter
      active: true,
      ignoredLateSuccesses: 0,
      ignoredLateFailures: 0,
      supersededBy: null,
      supersedeReason: null,
    };
    generations.push(record);
    return record;
  }

  let current = createGenerationRecord(generationCounter);

  function settleWaiter(waiter, outcome, value) {
    if (waiter.settled) return false;
    waiter.settled = true;
    detachAbort(waiter);
    if (outcome === 'resolve') {
      waiter.resolve(value);
    } else {
      waiter.reject(value);
    }
    return true;
  }

  function detachAbort(waiter) {
    if (waiter.signal && waiter.abortHandler) {
      waiter.signal.removeEventListener('abort', waiter.abortHandler);
    }
    waiter.signal = null;
    waiter.abortHandler = null;
  }

  function attachAbort(waiter, generationRecord) {
    const { signal } = waiter;
    if (!signal) return;
    const abortHandler = () => {
      if (waiter.settled) return;
      generationRecord.waiters.delete(waiter.id);
      settleWaiter(waiter, 'reject', new RandomBytesRequestAbortedError(signal.reason));
    };
    waiter.abortHandler = abortHandler;
    signal.addEventListener('abort', abortHandler, { once: true });
  }

  function readFromSource(generationRecord, waiter) {
    const context = Object.freeze({
      generation: generationRecord.generation,
      attempt: generationRecord.attempt,
      initializationId: generationRecord.initializationId,
      requestId: waiter.id,
      label: waiter.label,
    });
    let produced;
    try {
      produced = source.randomBytes(waiter.size, context);
    } catch (cause) {
      throw new RandomBytesReadError({
        generation: generationRecord.generation,
        requestId: waiter.id,
        cause,
      });
    }
    if (!(produced instanceof Uint8Array)) {
      throw new RandomBytesReadError({
        generation: generationRecord.generation,
        requestId: waiter.id,
        cause: new TypeError('random source randomBytes() must return a Uint8Array'),
      });
    }
    if (produced.byteLength !== waiter.size) {
      throw new RandomBytesReadError({
        generation: generationRecord.generation,
        requestId: waiter.id,
        cause: new RangeError(
          `random source returned ${produced.byteLength} bytes for a ${waiter.size} byte request`,
        ),
      });
    }
    // Copy so a source that reuses an internal buffer cannot mutate handed-out
    // credential material after the fact.
    const copy = new Uint8Array(produced.byteLength);
    copy.set(produced);
    return copy;
  }

  function deliver(waiter, generationRecord) {
    try {
      settleWaiter(waiter, 'resolve', readFromSource(generationRecord, waiter));
    } catch (error) {
      settleWaiter(waiter, 'reject', error);
    }
  }

  function flushWaiters(generationRecord) {
    const pending = [...generationRecord.waiters.values()];
    generationRecord.waiters.clear();
    for (const waiter of pending) {
      if (waiter.settled) continue;
      deliver(waiter, generationRecord);
    }
  }

  function isCurrentAttempt(generationRecord, attempt) {
    return (
      !closed &&
      generationRecord.active &&
      current === generationRecord &&
      generationRecord.state === 'initializing' &&
      generationRecord.attempt === attempt
    );
  }

  function handleInitializationSuccess(generationRecord, attempt) {
    if (!isCurrentAttempt(generationRecord, attempt)) {
      // A late success from a superseded generation (or a superseded attempt)
      // must not mark the new generation ready nor wake its waiters.
      generationRecord.ignoredLateSuccesses += 1;
      return;
    }
    successfulInitializations += 1;
    generationRecord.initializationId = successfulInitializations;
    generationRecord.state = 'ready';
    flushWaiters(generationRecord);
  }

  function handleInitializationFailure(generationRecord, attempt, cause) {
    if (!isCurrentAttempt(generationRecord, attempt)) {
      generationRecord.ignoredLateFailures += 1;
      return;
    }
    const error = new RandomBytesInitializationError({
      generation: generationRecord.generation,
      attempt,
      cause,
    });
    generationRecord.state = 'failed';
    generationRecord.error = error;
    const pending = [...generationRecord.waiters.values()];
    generationRecord.waiters.clear();
    for (const waiter of pending) {
      // Same error instance for every waiter of the generation.
      settleWaiter(waiter, 'reject', error);
    }
  }

  function startInitialization(generationRecord) {
    generationRecord.state = 'initializing';
    generationRecord.attempt += 1;
    generationRecord.error = null;
    const { attempt } = generationRecord;
    const context = Object.freeze({
      generation: generationRecord.generation,
      attempt,
      initializationId: null,
    });

    let result;
    try {
      result =
        typeof source.initialize === 'function' ? source.initialize(context) : undefined;
    } catch (cause) {
      handleInitializationFailure(generationRecord, attempt, cause);
      return;
    }

    Promise.resolve(result).then(
      () => handleInitializationSuccess(generationRecord, attempt),
      (cause) => handleInitializationFailure(generationRecord, attempt, cause),
    );
  }

  /**
   * Open a new process generation. Called automatically when the lifecycle
   * adapter reports a working copy; also usable directly.
   *
   * Pending waiters of the previous generation are rejected because their
   * bytes would come from an initialization that belongs to the parent
   * process; pending initialization callbacks of that generation are ignored
   * when they settle.
   */
  function beginGeneration({ reason = 'working-copy' } = {}) {
    if (closed) return current.generation;
    generationCounter += 1;
    const previous = current;
    previous.active = false;
    previous.supersededBy = generationCounter;
    previous.supersedeReason = reason;
    previous.state = 'superseded';

    const supersededError = new RandomBytesGenerationSupersededError({
      previousGeneration: previous.generation,
      generation: generationCounter,
      reason,
    });
    const pending = [...previous.waiters.values()];
    previous.waiters.clear();
    for (const waiter of pending) {
      settleWaiter(waiter, 'reject', supersededError);
    }

    current = createGenerationRecord(generationCounter);
    current.supersedeReason = reason;
    return current.generation;
  }

  function getRandomBytes(size, options = {}) {
    assertSize(size);
    if (options === null || typeof options !== 'object') {
      throw new TypeError('random byte request options must be an object');
    }
    const { signal, label } = options;
    if (signal !== undefined && signal !== null && typeof signal.addEventListener !== 'function') {
      throw new TypeError('random byte request signal must be an AbortSignal');
    }

    requestCounter += 1;
    const waiter = {
      id: requestCounter,
      size,
      label: label ?? null,
      signal: signal ?? null,
      abortHandler: null,
      settled: false,
      resolve: null,
      reject: null,
    };
    const promise = new Promise((resolve, reject) => {
      waiter.resolve = resolve;
      waiter.reject = reject;
    });
    waiter.promise = promise;

    if (closed) {
      settleWaiter(waiter, 'reject', new RandomBytesProviderClosedError());
      return promise;
    }

    if (waiter.signal?.aborted) {
      // Cancelled before any work started: no waiter is registered and no
      // generation is initialized on behalf of a cancelled request.
      settleWaiter(waiter, 'reject', new RandomBytesRequestAbortedError(waiter.signal.reason));
      return promise;
    }

    const generationRecord = current;

    if (generationRecord.state === 'ready') {
      deliver(waiter, generationRecord);
      return promise;
    }

    generationRecord.waiters.set(waiter.id, waiter);
    attachAbort(waiter, generationRecord);

    if (generationRecord.state === 'idle' || generationRecord.state === 'failed') {
      // A failed generation is retried by the next request, using a new
      // attempt number so stale callbacks stay ignored.
      startInitialization(generationRecord);
    }
    // 'initializing': ride the in-flight initialization.
    return promise;
  }

  function close() {
    if (closed) return;
    closed = true;
    if (unsubscribe) unsubscribe();
    for (const generationRecord of generations) {
      generationRecord.active = false;
      const pending = [...generationRecord.waiters.values()];
      generationRecord.waiters.clear();
      for (const waiter of pending) {
        settleWaiter(waiter, 'reject', new RandomBytesProviderClosedError());
      }
      if (generationRecord.state !== 'superseded') {
        generationRecord.state = 'closed';
      }
    }
  }

  function describe(generationRecord) {
    return {
      generation: generationRecord.generation,
      state: generationRecord.state,
      attempt: generationRecord.attempt,
      initializationId: generationRecord.initializationId,
      pendingRequests: generationRecord.waiters.size,
      ignoredLateSuccesses: generationRecord.ignoredLateSuccesses,
      ignoredLateFailures: generationRecord.ignoredLateFailures,
      error: generationRecord.error,
    };
  }

  let unsubscribe = null;
  if (lifecycle) {
    unsubscribe = lifecycle.onWorkingCopy(() => {
      beginGeneration({ reason: 'working-copy' });
    });
  }

  return {
    getRandomBytes,
    request: getRandomBytes,
    beginGeneration,
    close,
    dispose: close,

    get generation() {
      return current.generation;
    },

    get closed() {
      return closed;
    },

    get status() {
      return {
        closed,
        generation: current.generation,
        state: current.state,
        attempt: current.attempt,
        pendingRequests: current.waiters.size,
        initializationId: current.initializationId,
        lastError: current.error,
      };
    },

    get history() {
      return generations.map(describe);
    },

    generationStatus(generation) {
      const found = generations.find((record) => record.generation === generation);
      return found ? describe(found) : null;
    },
  };
}
